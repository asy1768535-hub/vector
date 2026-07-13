from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from app.api import admin_libraries
from app.models.library import Library
from app.models.user import User
from app.schemas.admin import LibraryUpdate
from app.services.graph_extraction_safety import (
    cancel_library_jobs_for_safety_change,
    normalize_allowed_security_levels,
)


NOW = datetime(2026, 7, 10, 8, 0, 0, tzinfo=timezone.utc)


def _superuser() -> User:
    return User(
        id=uuid.uuid4(),
        email="admin@example.com",
        is_superuser=True,
        is_active=True,
    )


def _library() -> Library:
    return Library(
        id=uuid.uuid4(),
        slug="safety_lib",
        name="Safety",
        embedding_model="bge-m3",
        embedding_dim=1024,
        vector_distance="cosine",
        chunk_size=1000,
        chunk_overlap=120,
        retrieval_mode="dense",
        qdrant_collection="lib_safety_lib",
        lifecycle_mode="managed",
        index_state="ready",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
        graph_extraction_allowed_security_levels=["internal"],
    )


def test_allowed_security_levels_are_stripped_deduplicated_and_sorted():
    assert normalize_allowed_security_levels(
        [" restricted ", "internal", "internal"]
    ) == ["internal", "restricted"]
    assert normalize_allowed_security_levels([]) == []


@pytest.mark.parametrize(
    "value",
    ["internal", {"internal": True}, [""], ["   "], [1], [None]],
)
def test_allowed_security_levels_reject_non_array_or_invalid_elements(value):
    with pytest.raises(ValueError, match="security level"):
        normalize_allowed_security_levels(value)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("graph_extraction_enabled", None),
        ("external_llm_enabled", None),
        ("graph_extraction_allowed_security_levels", None),
        ("graph_extraction_allowed_security_levels", "internal"),
    ],
)
def test_graph_safety_patch_rejects_explicit_null_and_non_array(field, value):
    with pytest.raises(ValidationError):
        LibraryUpdate(**{field: value})


def test_cancel_library_jobs_for_safety_change_abandons_attempts_then_units_then_jobs():
    db = AsyncMock()
    db.execute = AsyncMock(
        side_effect=[
            MagicMock(rowcount=4),
            MagicMock(rowcount=3),
            MagicMock(rowcount=2),
        ]
    )

    cancelled = asyncio.run(
        cancel_library_jobs_for_safety_change(
            db,
            library_id=uuid.uuid4(),
            job_error_code="security_allowlist_changed",
            now=NOW,
        )
    )

    assert cancelled == 2
    assert db.execute.await_count == 3
    attempt_stmt = db.execute.await_args_list[0].args[0]
    unit_stmt = db.execute.await_args_list[1].args[0]
    job_stmt = db.execute.await_args_list[2].args[0]
    attempt_sql = str(attempt_stmt).lower()
    unit_sql = str(unit_stmt).lower()
    job_sql = str(job_stmt).lower()
    assert "update extraction_raw_output_attempts" in attempt_sql
    assert "request_status" in attempt_sql
    assert "abandoned" in attempt_stmt.compile().params.values()
    assert "unit_cancelled" in attempt_stmt.compile().params.values()
    assert "update graph_extraction_units" in unit_sql
    for field in ("worker_id", "claim_token", "claimed_at", "lease_expires_at"):
        assert field in unit_sql
    assert "cancelled" in unit_stmt.compile().params.values()
    assert "unit_cancelled" in unit_stmt.compile().params.values()
    assert "update graph_extraction_jobs" in job_sql
    assert "error_code" in job_sql
    assert "cancelled" in job_stmt.compile().params.values()
    assert "security_allowlist_changed" in job_stmt.compile().params.values()
    db.commit.assert_not_awaited()


def test_admin_patch_normalizes_allowlist_cancels_and_audits_opt_out():
    lib = _library()
    row = MagicMock()
    row.scalar_one_or_none.return_value = lib
    db = AsyncMock()
    db.execute = AsyncMock(return_value=row)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    body = LibraryUpdate(
        external_llm_enabled=False,
        graph_extraction_allowed_security_levels=["restricted", "internal", "internal"],
    )

    with patch.object(
        admin_libraries.graph_extraction_safety,
        "cancel_library_jobs_for_safety_change",
        new=AsyncMock(return_value=2),
    ) as cancel:
        result = asyncio.run(
            admin_libraries.update_library("safety_lib", body, _superuser(), db)
        )

    assert result.external_llm_enabled is False
    assert result.graph_extraction_allowed_security_levels == ["internal", "restricted"]
    cancel.assert_awaited_once_with(
        db,
        library_id=lib.id,
        job_error_code="library_opt_out",
    )
    audit = db.add.call_args.args[0]
    assert audit.action == "library.update"
    assert audit.target["external_llm_enabled"] is False
    assert audit.target["graph_extraction_allowed_security_levels"] == [
        "internal",
        "restricted",
    ]
    assert audit.target["cancelled_graph_extraction_jobs"] == 2
    db.commit.assert_awaited_once()


def test_admin_patch_changed_allowlist_cancels_with_specific_reason():
    lib = _library()
    row = MagicMock()
    row.scalar_one_or_none.return_value = lib
    db = AsyncMock()
    db.execute = AsyncMock(return_value=row)
    db.add = MagicMock()
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    body = LibraryUpdate(graph_extraction_allowed_security_levels=["restricted"])

    with patch.object(
        admin_libraries.graph_extraction_safety,
        "cancel_library_jobs_for_safety_change",
        new=AsyncMock(return_value=1),
    ) as cancel:
        asyncio.run(admin_libraries.update_library("safety_lib", body, _superuser(), db))

    cancel.assert_awaited_once_with(
        db,
        library_id=lib.id,
        job_error_code="security_allowlist_changed",
    )
    audit = db.add.call_args.args[0]
    assert audit.target["cancelled_graph_extraction_jobs"] == 1
