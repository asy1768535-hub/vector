from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.api import admin_libraries
from app.auth.backend import current_superuser
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.user import User
from app.schemas.admin import LibraryRead


def _custom_source_config(table: str = "custom_docs") -> dict[str, object]:
    return {
        "db_name": "vector_kb",
        "table": table,
        "key_field": "doc_id",
        "key_column": "doc_id",
        "text_column": "body",
        "key_type": "bigint",
    }


def _superuser() -> User:
    return User(id=uuid.uuid4(), email="su@example.com", is_superuser=True, is_active=True)


def test_library_read_exposes_fail_closed_graph_extraction_defaults():
    lib = Library(
        id=uuid.uuid4(),
        slug="safe_defaults",
        name="Safe Defaults",
        embedding_model="bge-m3",
        embedding_dim=1024,
        vector_distance="cosine",
        chunk_size=1000,
        chunk_overlap=120,
        retrieval_mode="dense",
        qdrant_collection="lib_safe_defaults",
        lifecycle_mode="managed",
        index_state="ready",
        graph_extraction_enabled=False,
        external_llm_enabled=False,
        graph_extraction_allowed_security_levels=[],
        knowledge_artifact_auto_enabled=False,
        summary_artifact_enabled=False,
        outline_artifact_enabled=False,
        knowledge_artifact_external_model_enabled=False,
        knowledge_artifact_allowed_security_levels=[],
        created_at=datetime(2026, 7, 10, tzinfo=timezone.utc),
    )

    result = LibraryRead.model_validate(lib)

    assert result.graph_extraction_enabled is False
    assert result.external_llm_enabled is False
    assert result.graph_extraction_allowed_security_levels == []
    assert result.knowledge_artifact_auto_enabled is False
    assert result.summary_artifact_enabled is False
    assert result.outline_artifact_enabled is False
    assert result.knowledge_artifact_external_model_enabled is False
    assert result.knowledge_artifact_allowed_security_levels == []


def test_create_library_duplicate_slug_returns_clear_chinese_409():
    su = _superuser()
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("duplicate slug")))
    db.rollback = AsyncMock()

    async def _ov_su():
        return su

    async def _ov_db():
        return db

    app.dependency_overrides[current_superuser] = _ov_su
    app.dependency_overrides[get_db] = _ov_db
    try:
        resp = TestClient(app).post(
            "/admin/libraries",
            json={"slug": "dup_lib", "name": "重复库"},
        )
        assert resp.status_code == 409
        assert resp.json()["detail"] == "库唯一ID已存在，请更换后重试"
        db.rollback.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


def test_create_library_defaults_source_enrichment_on():
    body = admin_libraries.LibraryCreate(slug="default_lib", name="默认开启")

    with patch.object(admin_libraries.qdrant, "ensure_collection", new=AsyncMock()), \
         patch.object(admin_libraries.audit_log, "record", new=AsyncMock()):
        db = AsyncMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.add = MagicMock()

        lib = asyncio.run(admin_libraries.create_library(body, _superuser(), db))

    assert lib.source_config is not None
    assert lib.source_config["table"] == "default_lib"
    assert lib.source_config["key_field"] == "text_id"
    db.add.assert_called_once()


def test_create_library_can_disable_source_enrichment():
    body = admin_libraries.LibraryCreate(
        slug="disabled_lib",
        name="显式关闭",
        source_enrichment_enabled=False,
    )

    with patch.object(admin_libraries.qdrant, "ensure_collection", new=AsyncMock()), \
         patch.object(admin_libraries.audit_log, "record", new=AsyncMock()):
        db = AsyncMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.add = MagicMock()

        lib = asyncio.run(admin_libraries.create_library(body, _superuser(), db))

    assert lib.source_config is None


def test_update_library_switches_source_enrichment_off():
    existing = Library(
        slug="toggle_lib",
        name="开到关",
        embedding_model="bge-m3",
        embedding_dim=1024,
        vector_distance="cosine",
        chunk_size=1000,
        chunk_overlap=120,
        qdrant_collection="lib_toggle_lib",
        source_config=admin_libraries.source_enrichment.conventional_config("toggle_lib"),
        index_state="ready",
        created_by=uuid.uuid4(),
    )
    row = MagicMock()
    row.scalar_one_or_none.return_value = existing
    db = AsyncMock()
    db.execute = AsyncMock(return_value=row)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    body = admin_libraries.LibraryUpdate(source_enrichment_enabled=False)

    with patch.object(admin_libraries.audit_log, "record", new=AsyncMock()):
        lib = asyncio.run(admin_libraries.update_library("toggle_lib", body, _superuser(), db))

    assert lib.source_config is None


def test_update_library_switches_source_enrichment_on_from_empty():
    existing = Library(
        slug="reenable_lib",
        name="关到开",
        embedding_model="bge-m3",
        embedding_dim=1024,
        vector_distance="cosine",
        chunk_size=1000,
        chunk_overlap=120,
        qdrant_collection="lib_reenable_lib",
        source_config=None,
        index_state="ready",
        created_by=uuid.uuid4(),
    )
    row = MagicMock()
    row.scalar_one_or_none.return_value = existing
    db = AsyncMock()
    db.execute = AsyncMock(return_value=row)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    body = admin_libraries.LibraryUpdate(source_enrichment_enabled=True)

    with patch.object(admin_libraries.audit_log, "record", new=AsyncMock()):
        lib = asyncio.run(admin_libraries.update_library("reenable_lib", body, _superuser(), db))

    assert lib.source_config is not None
    assert lib.source_config["table"] == "reenable_lib"
    assert lib.source_config["key_field"] == "text_id"


def test_source_enrichment_disabled_conflicts_with_non_empty_source_config():
    custom = _custom_source_config()

    create_body = admin_libraries.LibraryCreate(
        slug="conflict_lib",
        name="冲突",
        source_enrichment_enabled=False,
        source_config=custom,
    )
    try:
        asyncio.run(admin_libraries.create_library(create_body, _superuser(), AsyncMock()))
    except admin_libraries.HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("create_library should reject disabled enrichment with source_config")

    existing = Library(
        slug="conflict_lib",
        name="冲突",
        embedding_model="bge-m3",
        embedding_dim=1024,
        vector_distance="cosine",
        chunk_size=1000,
        chunk_overlap=120,
        qdrant_collection="lib_conflict_lib",
        source_config=None,
        index_state="ready",
        created_by=uuid.uuid4(),
    )
    row = MagicMock()
    row.scalar_one_or_none.return_value = existing
    db = AsyncMock()
    db.execute = AsyncMock(return_value=row)
    update_body = admin_libraries.LibraryUpdate(source_enrichment_enabled=False, source_config=custom)

    try:
        asyncio.run(admin_libraries.update_library("conflict_lib", update_body, _superuser(), db))
    except admin_libraries.HTTPException as exc:
        assert exc.status_code == 400
    else:
        raise AssertionError("update_library should reject disabled enrichment with source_config")


def test_custom_source_config_still_works_for_create_and_update():
    custom = _custom_source_config("external_docs")
    create_body = admin_libraries.LibraryCreate(
        slug="custom_lib",
        name="自定义",
        source_config=custom,
    )

    with patch.object(admin_libraries.qdrant, "ensure_collection", new=AsyncMock()), \
         patch.object(admin_libraries.audit_log, "record", new=AsyncMock()):
        db = AsyncMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.add = MagicMock()
        lib = asyncio.run(admin_libraries.create_library(create_body, _superuser(), db))

    assert lib.source_config == custom

    existing = Library(
        slug="custom_lib",
        name="自定义",
        embedding_model="bge-m3",
        embedding_dim=1024,
        vector_distance="cosine",
        chunk_size=1000,
        chunk_overlap=120,
        qdrant_collection="lib_custom_lib",
        source_config=None,
        index_state="ready",
        created_by=uuid.uuid4(),
    )
    row = MagicMock()
    row.scalar_one_or_none.return_value = existing
    db = AsyncMock()
    db.execute = AsyncMock(return_value=row)
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    update_custom = _custom_source_config("updated_docs")
    update_body = admin_libraries.LibraryUpdate(source_config=update_custom)

    with patch.object(admin_libraries.audit_log, "record", new=AsyncMock()):
        lib = asyncio.run(admin_libraries.update_library("custom_lib", update_body, _superuser(), db))

    assert lib.source_config == update_custom
