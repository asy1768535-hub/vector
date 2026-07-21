from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.api import library_compatibility as compatibility_api
from app.auth.backend import current_active_user, current_cookie_user
from app.config import settings
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.organization import Organization
from app.models.user import User
from app.services.library_compatibility_contracts import (
    CompatibilityAssessment,
    CompatibilityFingerprint,
    EmbeddingVerificationSnapshot,
    LibraryCompatibilityProfile,
    LibraryIncompatibility,
)


NOW = datetime(2026, 7, 22, 23, tzinfo=timezone.utc)


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


def _library(organization: Organization) -> Library:
    slug_suffix = uuid.uuid4().hex[:8].translate(str.maketrans("0123456789", "abcdefghij"))
    return Library(
        id=uuid.uuid4(),
        organization_id=organization.id,
        slug=f"lib_{slug_suffix}",
        name="Library",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection=f"lib_{uuid.uuid4().hex[:8]}",
        index_state="ready",
        created_at=NOW,
    )


def test_compatibility_routes_are_mounted_and_default_off():
    user = _user()
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with patch.object(settings, "cross_library_compatibility_enabled", False):
            check = TestClient(app).post(
                "/me/library-compatibility/check",
                json={"library_slugs": ["library"], "channels": ["text"]},
            )
            verify = TestClient(app).post(
                "/admin/libraries/library/verify-embedding-profile"
            )
        assert check.status_code == 404
        assert verify.status_code == 404
        paths = set(app.openapi()["paths"])
        assert "/me/library-compatibility/check" in paths
        assert "/admin/libraries/{slug}/verify-embedding-profile" in paths
    finally:
        app.dependency_overrides.clear()


def test_compatibility_check_returns_only_authorized_profile_projection():
    user = _user()
    organization = _organization()
    first = _library(organization)
    second = _library(organization)
    profiles = tuple(
        LibraryCompatibilityProfile(
            library=library,
            embedding=CompatibilityFingerprint("embedding-v1", "a" * 64, True),
            retrieval=CompatibilityFingerprint("retrieval-v1", "b" * 64, True),
            graph=None,
        )
        for library in (first, second)
    )
    assessment = CompatibilityAssessment(
        organization_id=organization.id,
        channels=("text",),
        profiles=profiles,
        compatible=False,
        incompatibilities=(
            LibraryIncompatibility(second.slug, ("library_index_unready",)),
        ),
    )
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "cross_library_compatibility_enabled", True),
            patch.object(
                compatibility_api,
                "assess_library_compatibility",
                new=AsyncMock(return_value=assessment),
            ) as assess,
        ):
            response = TestClient(app).post(
                "/me/library-compatibility/check",
                json={
                    "library_slugs": [first.slug, second.slug],
                    "channels": ["text"],
                },
            )
        assert response.status_code == 200
        body = response.json()
        assert body["organization_id"] == str(organization.id)
        assert body["reference_library_slug"] == first.slug
        assert [row["library_slug"] for row in body["libraries"]] == [
            first.slug,
            second.slug,
        ]
        assert body["incompatibilities"] == [
            {
                "library_slug": second.slug,
                "reason_codes": ["library_index_unready"],
            }
        ]
        assert "embedding_base_url" not in response.text
        assess.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


def test_embedding_verification_commits_bounded_response():
    user = _user()
    organization = _organization()
    library = _library(organization)
    db = AsyncMock()
    snapshot = EmbeddingVerificationSnapshot(
        contract_version="embedding-probe-v1",
        model=library.embedding_model,
        dimension=library.embedding_dim,
        endpoint_sha256="c" * 64,
        probe_fingerprint="d" * 64,
        verified_at=NOW,
    )
    app.dependency_overrides[current_cookie_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: db
    try:
        with (
            patch.object(settings, "cross_library_compatibility_enabled", True),
            patch.object(
                compatibility_api,
                "load_active_library",
                new=AsyncMock(return_value=library),
            ),
            patch.object(
                compatibility_api,
                "authorize_library_management",
                new=AsyncMock(),
            ),
            patch.object(
                compatibility_api,
                "verify_library_embedding_profile",
                new=AsyncMock(return_value=snapshot),
            ),
        ):
            response = TestClient(app).post(
                f"/admin/libraries/{library.slug}/verify-embedding-profile"
            )
        assert response.status_code == 200
        body = response.json()
        assert body["probe_fingerprint"] == "d" * 64
        assert "endpoint" not in body
        db.commit.assert_awaited_once()
        db.refresh.assert_awaited_once_with(library)
    finally:
        app.dependency_overrides.clear()


def test_compatibility_request_rejects_duplicate_scope_before_service():
    user = _user()
    assess = AsyncMock()
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "cross_library_compatibility_enabled", True),
            patch.object(
                compatibility_api,
                "assess_library_compatibility",
                new=assess,
            ),
        ):
            response = TestClient(app).post(
                "/me/library-compatibility/check",
                json={"library_slugs": ["same", "same"], "channels": ["text"]},
            )
        assert response.status_code == 422
        assess.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


def test_compatibility_request_rejects_malformed_library_slug_before_service():
    user = _user()
    assess = AsyncMock()
    app.dependency_overrides[current_active_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    try:
        with (
            patch.object(settings, "cross_library_compatibility_enabled", True),
            patch.object(
                compatibility_api,
                "assess_library_compatibility",
                new=assess,
            ),
        ):
            response = TestClient(app).post(
                "/me/library-compatibility/check",
                json={"library_slugs": ["not-a-library"], "channels": ["text"]},
            )
        assert response.status_code == 422
        assess.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()
