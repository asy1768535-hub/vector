from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.config import Settings, settings, validate_federated_retrieval_startup
from app.models.library import Library
from app.models.organization import Organization
from app.models.user import User
from app.schemas.dify import DifyRecord, DifyRetrievalResponse
from app.services import federated_retrieval as service
from app.services.federated_retrieval_contracts import (
    FederatedLibraryExecution,
    FederatedRetrievalCommand,
    FederatedRetrievalError,
)
from app.services.library_compatibility_contracts import (
    CompatibilityAssessment,
    CompatibilityFingerprint,
    LibraryCompatibilityProfile,
    LibraryIncompatibility,
)


NOW = datetime(2026, 7, 22, 23, 30, tzinfo=timezone.utc)


class _DB:
    def __init__(self):
        self.commit = AsyncMock()


def _organization() -> Organization:
    return Organization(
        id=uuid.uuid4(),
        slug=f"org-{uuid.uuid4().hex[:8]}",
        name="Organization",
        deployment_profile="hosted",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        hashed_password="hash",
        is_active=True,
        is_superuser=False,
        is_verified=True,
        created_at=NOW,
    )


def _library(organization: Organization, slug: str) -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=organization.id,
        slug=slug,
        name=f"Library {slug}",
        embedding_model="bge-m3",
        embedding_dim=1024,
        vector_distance="cosine",
        chunk_size=1000,
        chunk_overlap=120,
        retrieval_mode="dense",
        qdrant_collection=f"collection_{slug}",
        index_state="ready",
        created_at=NOW,
    )


def _profile(library: Library) -> LibraryCompatibilityProfile:
    return LibraryCompatibilityProfile(
        library=library,
        embedding=CompatibilityFingerprint("embedding-v1", "a" * 64, True),
        retrieval=CompatibilityFingerprint("retrieval-v1", "b" * 64, True),
        graph=None,
    )


def _assessment(
    organization: Organization,
    libraries: tuple[Library, ...],
    *,
    compatible: bool = True,
    incompatibilities: tuple[LibraryIncompatibility, ...] = (),
) -> CompatibilityAssessment:
    return CompatibilityAssessment(
        organization_id=organization.id,
        channels=("text",),
        profiles=tuple(_profile(library) for library in libraries),
        compatible=compatible,
        incompatibilities=incompatibilities,
    )


def _command(organization: Organization, *libraries: Library):
    return FederatedRetrievalCommand(
        organization_id=organization.id,
        library_slugs=tuple(library.slug for library in libraries),
        query="  contract termination  ",
        top_k=3,
        candidate_k=4,
        score_threshold=0.2,
    )


def _records(prefix: str, scores: tuple[float, ...]) -> tuple[DifyRecord, ...]:
    return tuple(
        DifyRecord(
            content=("x" * 4_100 if rank == 1 else f"content-{prefix}-{rank}"),
            score=score,
            title=f"title-{prefix}-{rank}",
            metadata={
                "document_id": f"doc-{prefix}-{rank}",
                "document_revision_id": f"rev-{prefix}-{rank}",
                "document_revision": 2,
                "chunk_id": "shared" if rank == 1 else f"chunk-{prefix}-{rank}",
                "seq": rank,
                "title_path": ["Section", 1, "Clause"],
                "external_id": f"external-{prefix}",
                "vector_score": score,
                "rewrite_source": ["llm", "original", "unknown"],
                "secret": "must-not-leak",
                "source_config": {"dsn": "must-not-leak"},
            },
        )
        for rank, score in enumerate(scores, start=1)
    )


def test_federation_defaults_off_and_requires_both_dependencies():
    assert settings.federated_retrieval_enabled is False
    validate_federated_retrieval_startup(Settings())
    with pytest.raises(RuntimeError, match="Organization authorization"):
        validate_federated_retrieval_startup(Settings(federated_retrieval_enabled=True))
    with pytest.raises(RuntimeError, match="cross-Library compatibility"):
        validate_federated_retrieval_startup(
            Settings(
                federated_retrieval_enabled=True,
                organization_authorization_enabled=True,
            )
        )
    validate_federated_retrieval_startup(
        Settings(
            federated_retrieval_enabled=True,
            organization_authorization_enabled=True,
            cross_library_compatibility_enabled=True,
        )
    )


def test_command_normalizes_query_and_rejects_invalid_candidate_bound():
    organization = _organization()
    library = _library(organization, "legal")
    command = _command(organization, library)
    assert command.query == "contract termination"
    with pytest.raises(FederatedRetrievalError) as exc_info:
        FederatedRetrievalCommand(
            organization.id,
            (library.slug,),
            "query",
            10,
            5,
            0.0,
        )
    assert exc_info.value.code == "federated_request_invalid"


def test_rank_fusion_ignores_raw_scale_preserves_scope_and_bounds_projection():
    organization = _organization()
    first = _library(organization, "legal")
    second = _library(organization, "contracts")
    executions = (
        FederatedLibraryExecution(_profile(first), _records("a", (0.99, 0.98)), 2),
        FederatedLibraryExecution(_profile(second), _records("b", (0.01, 0.001)), 3),
    )
    hits = service.fuse_library_records(executions, top_k=4)
    assert [(hit.library_slug, hit.local_rank) for hit in hits] == [
        ("legal", 1),
        ("contracts", 1),
        ("legal", 2),
        ("contracts", 2),
    ]
    assert hits[0].source.chunk_id == hits[1].source.chunk_id == "shared"
    assert hits[0].library_id != hits[1].library_id
    assert len(hits[0].content_excerpt) == 4_000
    assert hits[0].content_truncated is True
    assert hits[0].source.title_path == ("Section", "Clause")
    assert hits[0].source.rewrite_sources == ("original", "llm")
    assert "secret" not in repr(hits[0].source).lower()
    assert "dsn" not in repr(hits[0].source).lower()


def test_execution_preflights_exact_scope_and_reuses_single_library_retrieval(monkeypatch):
    organization = _organization()
    libraries = (
        _library(organization, "legal"),
        _library(organization, "contracts"),
    )
    assessment = _assessment(organization, libraries)
    assess = AsyncMock(return_value=assessment)

    async def retrieve(**kwargs):
        slug = kwargs["request"].knowledge_id
        prefix = "a" if slug == "legal" else "b"
        return DifyRetrievalResponse(records=list(_records(prefix, (0.9, 0.8))))

    retrieve_mock = AsyncMock(side_effect=retrieve)
    monkeypatch.setattr(service, "assess_library_compatibility", assess)
    monkeypatch.setattr(service, "run_retrieval", retrieve_mock)
    db = _DB()
    command = _command(organization, *libraries)
    result = asyncio.run(
        service.run_federated_retrieval(db, user=_user(), command=command)
    )
    assert [timing.library_slug for timing in result.timings] == ["legal", "contracts"]
    assert [hit.library_slug for hit in result.hits] == ["legal", "contracts", "legal"]
    assess.assert_awaited_once()
    assert assess.await_args.kwargs["library_slugs"] == ("legal", "contracts")
    assert assess.await_args.kwargs["channels"] == ("text",)
    assert [call.kwargs["request"].knowledge_id for call in retrieve_mock.await_args_list] == [
        "legal",
        "contracts",
    ]
    assert all(
        call.kwargs["request"].retrieval_setting.top_k == command.candidate_k
        for call in retrieve_mock.await_args_list
    )
    db.commit.assert_not_awaited()


def test_incompatible_or_cross_organization_scope_executes_no_retrieval(monkeypatch):
    organization = _organization()
    other = _organization()
    first = _library(organization, "legal")
    second = _library(organization, "contracts")
    retrieve = AsyncMock()
    monkeypatch.setattr(service, "run_retrieval", retrieve)
    monkeypatch.setattr(
        service,
        "assess_library_compatibility",
        AsyncMock(
            return_value=_assessment(
                organization,
                (first, second),
                compatible=False,
                incompatibilities=(
                    LibraryIncompatibility(
                        second.slug,
                        ("retrieval_profile_mismatch",),
                    ),
                ),
            )
        ),
    )
    with pytest.raises(FederatedRetrievalError) as exc_info:
        asyncio.run(
            service.run_federated_retrieval(
                _DB(), user=_user(), command=_command(organization, first, second)
            )
        )
    assert exc_info.value.code == "federated_scope_incompatible"
    retrieve.assert_not_awaited()

    second.organization_id = other.id
    monkeypatch.setattr(
        service,
        "assess_library_compatibility",
        AsyncMock(return_value=_assessment(organization, (first, second))),
    )
    with pytest.raises(FederatedRetrievalError) as exc_info:
        asyncio.run(
            service.run_federated_retrieval(
                _DB(), user=_user(), command=_command(organization, first, second)
            )
        )
    assert exc_info.value.code == "federated_scope_forbidden"
    retrieve.assert_not_awaited()


def test_branch_failure_returns_no_result_and_stops_remaining_scope(monkeypatch):
    organization = _organization()
    libraries = (
        _library(organization, "legal"),
        _library(organization, "contracts"),
        _library(organization, "policies"),
    )
    monkeypatch.setattr(
        service,
        "assess_library_compatibility",
        AsyncMock(return_value=_assessment(organization, libraries)),
    )
    retrieve = AsyncMock(
        side_effect=[
            DifyRetrievalResponse(records=list(_records("a", (0.9,)))),
            RuntimeError("provider secret diagnostic"),
        ]
    )
    monkeypatch.setattr(service, "run_retrieval", retrieve)
    with pytest.raises(FederatedRetrievalError) as exc_info:
        asyncio.run(
            service.run_federated_retrieval(
                _DB(), user=_user(), command=_command(organization, *libraries)
            )
        )
    assert exc_info.value.code == "federated_branch_failed"
    assert "provider secret diagnostic" not in str(exc_info.value)
    assert retrieve.await_count == 2
