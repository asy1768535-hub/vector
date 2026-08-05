from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

import pytest

from app.api import admin_libraries
from app.auth.backend import current_superuser
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.user import User
from app.schemas.admin import LibraryRead


_ORIGINAL_ATTACH_ACTIVE_CLASSIFICATION_LABELS = (
    admin_libraries._attach_active_classification_labels
)


@pytest.fixture(autouse=True)
def _stub_classification_label_attachment():
    with patch.object(
        admin_libraries,
        "_attach_active_classification_labels",
        new=AsyncMock(),
    ) as attach:
        yield attach

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
    assert result.graph_extraction_build_mode == "standard"
    assert result.external_llm_enabled is False
    assert result.graph_extraction_allowed_security_levels == []
    assert result.knowledge_artifact_auto_enabled is False
    assert result.summary_artifact_enabled is False
    assert result.outline_artifact_enabled is False
    assert result.knowledge_artifact_external_model_enabled is False
    assert result.knowledge_artifact_allowed_security_levels == []


def test_create_library_active_duplicate_name_returns_clear_chinese_409():
    su = _superuser()
    db = AsyncMock()
    db.add = MagicMock()
    db.flush = AsyncMock(side_effect=IntegrityError("stmt", {}, Exception("duplicate slug")))
    db.rollback = AsyncMock()
    history = MagicMock()
    history.all.return_value = [("dup_lib", None)]
    db.execute = AsyncMock(return_value=history)

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
        assert resp.json()["detail"] == "已有同名知识库；如需重建，请先删除当前活动库"
        db.rollback.assert_awaited_once()
    finally:
        app.dependency_overrides.clear()


def test_create_library_reuses_deleted_name_with_generation_slug():
    body = admin_libraries.LibraryCreate(slug="library_medical", name="医学知识库")
    deleted_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    history = MagicMock()
    history.all.return_value = [("library_medical", deleted_at)]
    no_collision = MagicMock()
    no_collision.scalar_one_or_none.return_value = None

    db = AsyncMock()
    db.add = MagicMock()
    db.execute = AsyncMock(side_effect=[history, no_collision])
    db.flush = AsyncMock(
        side_effect=[IntegrityError("stmt", {}, Exception("duplicate slug")), None]
    )
    db.rollback = AsyncMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    with patch.object(admin_libraries.qdrant, "ensure_collection", new=AsyncMock()), \
         patch.object(admin_libraries.audit_log, "record", new=AsyncMock()) as record:
        lib = asyncio.run(admin_libraries.create_library(body, _superuser(), db))

    assert lib.slug == "library_medical__r2"
    assert lib.qdrant_collection == "lib_library_medical__r2"
    assert lib.source_config["table"] == "library_medical__r2"
    assert record.await_args.args[3]["creation_generation"] == 2
    assert record.await_args.args[3]["requested_slug"] == "library_medical"


def test_create_library_defaults_source_enrichment_on(_stub_classification_label_attachment):
    body = admin_libraries.LibraryCreate(slug="default_lib", name="默认开启")
    assert body.ocr_enabled is True
    assert body.docx_table_aware is True

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
    assert lib.knowledge_artifact_auto_enabled is True
    assert lib.summary_artifact_enabled is True
    assert lib.outline_artifact_enabled is True
    assert lib.classification_auto_enabled is True
    assert lib.classification_external_model_enabled is True
    assert lib.classification_allowed_security_levels == ["internal"]
    _stub_classification_label_attachment.assert_awaited_once_with(db, lib)
    db.add.assert_called_once()


def test_attach_active_classification_labels_binds_enabled_labels_in_order():
    organization_id = uuid.uuid4()
    taxonomy = MagicMock(id=uuid.uuid4())
    labels = [
        MagicMock(id=uuid.uuid4(), sort_order=10, key="first"),
        MagicMock(id=uuid.uuid4(), sort_order=20, key="second"),
    ]
    taxonomy_result = MagicMock()
    taxonomy_result.scalar_one_or_none.return_value = taxonomy
    labels_result = MagicMock()
    labels_result.scalars.return_value.all.return_value = labels
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[taxonomy_result, labels_result])
    db.add_all = MagicMock()
    library = Library(
        id=uuid.uuid4(),
        organization_id=organization_id,
        slug="classified",
        name="Classified",
        embedding_model="bge-m3",
        embedding_dim=1024,
        vector_distance="cosine",
        chunk_size=1000,
        chunk_overlap=120,
        retrieval_mode="dense",
        qdrant_collection="lib_classified",
    )

    asyncio.run(_ORIGINAL_ATTACH_ACTIVE_CLASSIFICATION_LABELS(db, library))

    bindings = list(db.add_all.call_args.args[0])
    assert [item.library_id for item in bindings] == [library.id, library.id]
    assert [item.taxonomy_version_id for item in bindings] == [taxonomy.id, taxonomy.id]
    assert [item.label_id for item in bindings] == [labels[0].id, labels[1].id]
    assert [item.ordinal for item in bindings] == [0, 1]

def test_create_library_grants_creator_all_permissions_when_org_auth_enabled(monkeypatch):
    body = admin_libraries.LibraryCreate(slug="owned_lib", name="创建者权限")
    actor = _superuser()
    mutation = MagicMock()
    mutation.compensate = MagicMock()
    grant = AsyncMock(return_value=mutation)
    monkeypatch.setattr(
        admin_libraries.settings,
        "organization_authorization_enabled",
        True,
    )

    with patch.object(admin_libraries.qdrant, "ensure_collection", new=AsyncMock()), \
         patch.object(admin_libraries.audit_log, "record", new=AsyncMock()), \
         patch.object(
             admin_libraries,
             "grant_platform_library_permissions",
             new=grant,
         ):
        db = AsyncMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.add = MagicMock()

        lib = asyncio.run(admin_libraries.create_library(body, actor, db))

    grant.assert_awaited_once_with(
        db,
        actor_user_id=actor.id,
        target_user_id=actor.id,
        library=lib,
        actions=("read", "insert", "delete", "admin"),
    )
    mutation.compensate.assert_not_called()


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


def test_create_library_persists_graph_extraction_configuration():
    body = admin_libraries.LibraryCreate(
        slug="graph_enabled",
        name="Graph Enabled",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
        graph_extraction_allowed_security_levels=[" internal ", "internal"],
    )

    with patch.object(admin_libraries.qdrant, "ensure_collection", new=AsyncMock()), \
         patch.object(admin_libraries.audit_log, "record", new=AsyncMock()):
        db = AsyncMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.add = MagicMock()

        lib = asyncio.run(admin_libraries.create_library(body, _superuser(), db))

    assert lib.graph_extraction_enabled is True
    assert lib.external_llm_enabled is True
    assert lib.graph_extraction_allowed_security_levels == ["internal"]


def test_create_library_can_seed_active_enterprise_schema():
    body = admin_libraries.LibraryCreate(
        slug="seeded_graph",
        name="Seeded Graph",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
        graph_extraction_allowed_security_levels=["internal"],
        schema_template="enterprise",
    )

    with patch.object(admin_libraries.qdrant, "ensure_collection", new=AsyncMock()), \
         patch.object(admin_libraries.audit_log, "record", new=AsyncMock()), \
         patch.object(
             admin_libraries.graph_seed,
             "seed_enterprise_ontology",
             new=AsyncMock(),
         ) as seed:
        db = AsyncMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.add = MagicMock()

        lib = asyncio.run(admin_libraries.create_library(body, _superuser(), db))

    seed.assert_awaited_once_with(db, lib)


def test_create_library_does_not_seed_schema_by_default():
    body = admin_libraries.LibraryCreate(slug="no_schema", name="No Schema")

    with patch.object(admin_libraries.qdrant, "ensure_collection", new=AsyncMock()), \
         patch.object(admin_libraries.audit_log, "record", new=AsyncMock()), \
         patch.object(
             admin_libraries.graph_seed,
             "seed_enterprise_ontology",
             new=AsyncMock(),
         ) as seed:
        db = AsyncMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.add = MagicMock()

        asyncio.run(admin_libraries.create_library(body, _superuser(), db))

    seed.assert_not_awaited()


def test_create_library_explore_mode_does_not_seed_formal_schema():
    body = admin_libraries.LibraryCreate(
        slug="explore_graph",
        name="Explore Graph",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
        graph_extraction_allowed_security_levels=["internal"],
        schema_mode="explore",
    )

    with patch.object(admin_libraries.qdrant, "ensure_collection", new=AsyncMock()), \
         patch.object(admin_libraries.audit_log, "record", new=AsyncMock()), \
         patch.object(
             admin_libraries.graph_seed,
             "seed_enterprise_ontology",
             new=AsyncMock(),
         ) as seed:
        db = AsyncMock()
        db.flush = AsyncMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        db.add = MagicMock()

        lib = asyncio.run(admin_libraries.create_library(body, _superuser(), db))

    assert lib.schema_mode == "explore"
    assert lib.graph_extraction_enabled is True
    seed.assert_not_awaited()


def test_legacy_graph_create_defaults_to_governed_schema_mode():
    body = admin_libraries.LibraryCreate(
        slug="legacy_graph",
        name="Legacy Graph",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
        graph_extraction_allowed_security_levels=["internal"],
    )

    assert body.schema_mode == "governed"


def test_create_library_rejects_incomplete_graph_extraction_configuration():
    for payload in (
        {
            "slug": "missing_model",
            "name": "Missing Model",
            "graph_extraction_enabled": True,
            "graph_extraction_allowed_security_levels": ["internal"],
        },
        {
            "slug": "missing_level",
            "name": "Missing Level",
            "graph_extraction_enabled": True,
            "external_llm_enabled": True,
        },
    ):
        try:
            admin_libraries.LibraryCreate(**payload)
        except ValueError:
            continue
        raise AssertionError("enabled graph extraction must require model permission and levels")


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
