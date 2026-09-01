from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from unittest.mock import patch

import pytest

from app.config import Settings
from app.models.library import Library
from app.models.user import User
from app.schemas.public_v1 import PublicRetrievalRequest
from app.services import public_v1
from app.services.federated_retrieval_contracts import FederatedRetrievalError
from app.services.knowledge_catalog_contracts import KnowledgeCatalogError
from app.services.public_api_operations import (
    PublicAnswerTimedOut,
    organization_limits_apply,
    record_public_operation,
    release_public_answer_lease,
    run_with_public_answer_timeout,
)
from app.services.public_api_operations_contracts import PublicOperationRecord, utc_now
from app.services.public_api_operations_contracts import PublicOperationContext
from app.services.public_v1_contracts import PublicAPIError


def _config(**overrides) -> Settings:
    values = {
        "public_api_operations_enabled": True,
        "public_api_limits_enabled": True,
        "public_api_answer_max_seconds": 1,
        "public_api_answer_lease_seconds": 31,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_deployment_profile_enforcement_is_explicit_and_default_private_is_unlimited():
    config = _config()
    assert organization_limits_apply("hosted", config=config) is True
    assert organization_limits_apply("private", config=config) is False
    assert (
        organization_limits_apply(
            "private",
            config=_config(public_api_limit_private_organizations=True),
        )
        is True
    )
    assert (
        organization_limits_apply(
            "hosted",
            config=_config(public_api_limits_enabled=False),
        )
        is False
    )


def test_operations_disabled_never_opens_a_recording_session():
    called = False

    def factory():
        nonlocal called
        called = True
        raise AssertionError("session factory must remain inert")

    now = utc_now()
    record = PublicOperationRecord(
        request_id="0123456789abcdef0123456789abcdef",
        user_id="00000000-0000-4000-8000-000000000001",
        endpoint_key="libraries.list",
        http_method="GET",
        started_at=now,
        finished_at=now,
        duration_ms=0,
        http_status=200,
        outcome="completed",
    )
    result = asyncio.run(
        record_public_operation(
            record,
            session_factory=factory,
            config=_config(public_api_operations_enabled=False),
        )
    )
    assert result is False
    assert called is False


def test_answer_timeout_is_bounded_and_cancels_provider_work():
    cancelled = False

    async def provider():
        nonlocal cancelled
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled = True
            raise

    with pytest.raises(PublicAnswerTimedOut):
        asyncio.run(run_with_public_answer_timeout(provider(), config=_config()))
    assert cancelled is True


def test_release_failure_is_content_free_and_does_not_escape(caplog):
    class BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("question=private provider response")

        async def __aexit__(self, *_args):
            return None

    request_id = "fedcba9876543210fedcba9876543210"
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(
            release_public_answer_lease(
                request_id,
                session_factory=BrokenSession,
            )
        )
    assert result is False
    assert request_id in caplog.text
    assert "private" not in caplog.text
    assert "provider response" not in caplog.text
    assert timedelta(seconds=31) > timedelta(seconds=1)


def test_single_resource_admission_occurs_after_authorization_before_catalog():
    events = []
    organization_id = uuid.uuid4()
    library = Library(
        id=uuid.uuid4(),
        organization_id=organization_id,
        slug="alpha",
        name="Alpha",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="ops_order",
    )
    user = User(id=uuid.uuid4(), email="member@example.com", hashed_password="hash")
    context = PublicOperationContext(
        request_id="0123456789abcdef0123456789abcdef",
        endpoint_key="documents.get",
        http_method="GET",
        user_id=user.id,
    )

    async def authorize(*_args, **_kwargs):
        events.append("authorize")
        return library

    async def admit(scope):
        events.append("admit")
        assert scope.organization_ids == (organization_id,)

    async def catalog(*_args, **_kwargs):
        events.append("catalog")
        raise KnowledgeCatalogError("catalog_not_found")

    with (
        patch.object(public_v1, "authorize_public_library", side_effect=authorize),
        patch.object(public_v1, "admit_public_request", side_effect=admit),
        patch.object(public_v1, "get_catalog_document_detail", side_effect=catalog),
        pytest.raises(PublicAPIError) as exc_info,
    ):
        asyncio.run(
            public_v1.get_public_document(
                object(),
                user=user,
                slug="alpha",
                document_id=uuid.uuid4(),
                request_id=context.request_id,
                operation_context=context,
            )
        )
    assert exc_info.value.code == "resource_not_found"
    assert events == ["authorize", "admit", "catalog"]
    assert context.library_ids == (library.id,)


def test_retrieval_admission_occurs_before_external_federation():
    events = []
    organization_id = uuid.uuid4()
    library = Library(
        id=uuid.uuid4(),
        organization_id=organization_id,
        slug="alpha",
        name="Alpha",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="ops_retrieval_order",
    )
    scope = public_v1.ResolvedPublicScope(organization_id, None, (library,))
    user = User(id=uuid.uuid4(), email="member2@example.com", hashed_password="hash")
    body = PublicRetrievalRequest(
        scope={"library_slugs": ["alpha"]},
        query="private question",
    )
    context = PublicOperationContext(
        request_id="fedcba9876543210fedcba9876543210",
        endpoint_key="retrieval.search",
        http_method="POST",
        user_id=user.id,
    )

    async def resolve(*_args, **_kwargs):
        events.append("resolve")
        return scope

    async def admit(_scope):
        events.append("admit")

    async def retrieve(*_args, **_kwargs):
        events.append("federate")
        raise FederatedRetrievalError("federated_branch_failed")

    with (
        patch.object(public_v1, "resolve_public_scope", side_effect=resolve),
        patch.object(public_v1, "admit_public_request", side_effect=admit),
        patch.object(public_v1, "run_federated_retrieval", side_effect=retrieve),
        pytest.raises(PublicAPIError) as exc_info,
    ):
        asyncio.run(
            public_v1.prepare_public_retrieval(
                object(),
                user=user,
                body=body,
                operation_context=context,
            )
        )
    assert exc_info.value.code == "upstream_failed"
    assert events == ["resolve", "admit", "federate"]
