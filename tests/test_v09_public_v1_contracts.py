from __future__ import annotations

import re
import uuid

import pytest
from pydantic import ValidationError

from app.config import Settings, settings, validate_public_api_v1_startup
from app.main import app
from app.schemas.public_v1 import (
    PublicChunkRead,
    PublicGraphRead,
    PublicRetrievalRequest,
    PublicRetrievalResponse,
    PublicScopeSelection,
    PublicSourceRead,
)
from app.services.public_v1_contracts import (
    PublicAPIError,
    public_error_envelope,
    split_public_delta,
)


REQUEST_ID = "0123456789abcdef0123456789abcdef"


def test_public_api_defaults_off_and_requires_authoritative_dependencies():
    assert settings.public_api_v1_enabled is False
    validate_public_api_v1_startup(Settings(_env_file=None))
    with pytest.raises(RuntimeError, match="Public API v1 requires"):
        validate_public_api_v1_startup(
            Settings(_env_file=None, public_api_v1_enabled=True)
        )
    validate_public_api_v1_startup(
        Settings(
            _env_file=None,
            public_api_v1_enabled=True,
            organization_authorization_enabled=True,
            cross_library_compatibility_enabled=True,
            personal_library_scopes_enabled=True,
            federated_retrieval_enabled=True,
            knowledge_catalog_enabled=True,
            graph_catalog_enabled=True,
        )
    )


@pytest.mark.parametrize(
    "value",
    [
        {},
        {"library_slugs": ["alpha"], "scope_id": str(uuid.uuid4())},
        {"library_slugs": ["alpha", "alpha"]},
        {"library_slugs": []},
    ],
)
def test_scope_selection_requires_one_bounded_unique_identity(value):
    with pytest.raises(ValidationError):
        PublicScopeSelection.model_validate(value)


def test_public_requests_are_strict_and_candidate_bound_is_cross_checked():
    valid = PublicRetrievalRequest.model_validate(
        {
            "scope": {"library_slugs": ["alpha", "beta"]},
            "query": "Where is the evidence?",
            "top_k": 5,
            "candidate_k": 10,
        }
    )
    assert valid.scope.library_slugs == ["alpha", "beta"]
    with pytest.raises(ValidationError):
        PublicRetrievalRequest.model_validate(
            {
                "scope": {"library_slugs": ["alpha"]},
                "query": "question",
                "top_k": 10,
                "candidate_k": 5,
            }
        )
    with pytest.raises(ValidationError):
        PublicRetrievalRequest.model_validate(
            {
                "scope": {"library_slugs": ["alpha"]},
                "query": "question",
                "unknown": "forbidden",
            }
        )


def test_retrieval_grounding_requires_contiguous_matching_identity():
    organization_id = uuid.uuid4()
    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    source = PublicSourceRead(
        rank=1,
        library_id=library_id,
        library_slug="alpha",
        library_name="Alpha",
        document_id=document_id,
        document_revision_id=revision_id,
        chunk_id=chunk_id,
        score=0.5,
    )
    chunk = PublicChunkRead(
        rank=1,
        library_id=library_id,
        library_slug="alpha",
        document_id=document_id,
        document_revision_id=revision_id,
        chunk_id=chunk_id,
        content="grounded",
        content_truncated=False,
        score=0.5,
    )
    graph = PublicGraphRead(
        available=False,
        entities=[],
        relations=[],
        documents_examined=0,
        truncated=False,
    )
    response = PublicRetrievalResponse(
        request_id=REQUEST_ID,
        scope={
            "organization_id": organization_id,
            "libraries": [
                {
                    "id": library_id,
                    "organization_id": organization_id,
                    "slug": "alpha",
                    "name": "Alpha",
                    "index_state": "ready",
                }
            ],
        },
        sources=[source],
        chunks=[chunk],
        graph=graph,
    )
    assert response.sources[0].chunk_id == response.chunks[0].chunk_id
    with pytest.raises(ValidationError, match="identity must match"):
        PublicRetrievalResponse(
            request_id=REQUEST_ID,
            scope=response.scope,
            sources=[source],
            chunks=[chunk.model_copy(update={"chunk_id": uuid.uuid4()})],
            graph=graph,
        )


def test_public_errors_are_fixed_and_allowlist_compatibility_details():
    error = PublicAPIError(
        "scope_incompatible",
        status_code=409,
        details=(
            ("beta", ("embedding_profile_mismatch", "RAW PROVIDER ERROR")),
            ("../secret", ("graph_profile_mismatch",)),
        ),
    )
    payload = public_error_envelope(REQUEST_ID, error).model_dump(mode="json")
    assert payload == {
        "error": {
            "code": "scope_incompatible",
            "request_id": REQUEST_ID,
            "message": "The selected knowledge libraries are incompatible.",
            "details": [
                {
                    "library_slug": "beta",
                    "reason_codes": ["embedding_profile_mismatch"],
                }
            ],
        }
    }
    assert "RAW" not in repr(payload)


def test_delta_chunks_are_nonempty_and_bounded():
    chunks = split_public_delta("x" * 5_000)
    assert "".join(chunks) == "x" * 5_000
    assert all(0 < len(chunk) <= 2_048 for chunk in chunks)
    assert split_public_delta("") == ()


def test_openapi_publishes_exact_additive_route_inventory_and_sse_contract():
    paths = app.openapi()["paths"]
    public_paths = {path for path in paths if path.startswith("/api/v1")}
    assert public_paths == {
        "/api/v1/libraries",
        "/api/v1/scopes/validate",
        "/api/v1/libraries/{slug}/documents/{document_id}",
        "/api/v1/libraries/{slug}/entities/{entity_id}",
        "/api/v1/libraries/{slug}/relations/{relation_id}",
        "/api/v1/libraries/{slug}/evidence/{evidence_id}",
        "/api/v1/entities/search",
        "/api/v1/relations/search",
        "/api/v1/retrieval",
        "/api/v1/answers",
        "/api/v1/answers/stream",
    }
    stream = paths["/api/v1/answers/stream"]["post"]["responses"]["200"]
    assert "text/event-stream" in stream["content"]
    schema = app.openapi()["components"]["schemas"]["PublicErrorEnvelope"]
    assert schema["additionalProperties"] is False
    assert re.fullmatch(r"[0-9a-f]{32}", REQUEST_ID)
