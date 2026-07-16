from __future__ import annotations

from pathlib import Path

from sqlalchemy import CheckConstraint, Index
from sqlalchemy.dialects.postgresql import JSONB


MIGRATION = Path("alembic/versions/0023_v05_active_graph_publication.py")


def _constraint_names(table, kind) -> set[str]:
    return {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, kind) and constraint.name is not None
    }


def _check_sql(table, name: str) -> str:
    constraint = next(
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == name
    )
    return str(constraint.sqltext).lower()


def _index_names(table) -> set[str]:
    return {index.name for index in table.indexes}


def _index(table, name: str) -> Index:
    return next(index for index in table.indexes if index.name == name)


def _index_where_sql(table, name: str) -> str:
    where = _index(table, name).dialect_options["postgresql"].get("where")
    return str(where).lower()


def _foreign_key(column) -> tuple[str, str | None, str | None]:
    fk = next(iter(column.foreign_keys))
    return fk.target_fullname, fk.ondelete, fk.name


def test_v05_publication_models_are_exported():
    from app.models import GraphPublication, GraphPublicationItem

    assert GraphPublication.__tablename__ == "graph_publications"
    assert GraphPublicationItem.__tablename__ == "graph_publication_items"


def test_graph_publication_contract():
    from app.models.graph_publication import GraphPublication

    table = GraphPublication.__table__
    cols = table.c
    required = (
        "id",
        "library_id",
        "ontology_version_id",
        "status",
        "source_mode",
        "manifest_version",
        "policy_version",
        "policy_snapshot",
        "manifest_hash",
        "idempotency_key",
        "include_drafts",
        "plan_options",
        "parent_publication_id",
        "rollback_target_publication_id",
        "planned_by_user_id",
        "activated_by_user_id",
        "cancelled_by_user_id",
        "superseded_by_publication_id",
        "entity_count",
        "relation_count",
        "blocked_counts",
        "blocked_diagnostics",
        "item_hashes_summary",
        "error_code",
        "error_message",
        "planned_at",
        "activated_at",
        "superseded_at",
        "cancelled_at",
        "failed_at",
        "last_reconciled_at",
        "created_at",
        "updated_at",
    )
    assert set(required) <= set(cols.keys())

    for name in (
        "policy_snapshot",
        "plan_options",
        "blocked_counts",
        "blocked_diagnostics",
        "item_hashes_summary",
    ):
        assert isinstance(cols[name].type, JSONB)
        assert cols[name].nullable is False

    assert cols.status.nullable is False
    assert cols.source_mode.nullable is False
    assert cols.manifest_hash.type.length == 64
    assert cols.idempotency_key.type.length == 128
    assert cols.error_message.type.length == 255
    assert cols.include_drafts.nullable is False
    assert str(cols.include_drafts.server_default.arg).lower() == "false"
    assert cols.entity_count.nullable is False
    assert cols.relation_count.nullable is False

    assert _foreign_key(cols.library_id) == (
        "sys_libraries.id",
        "CASCADE",
        "fk_graph_publications_library",
    )
    assert _foreign_key(cols.ontology_version_id) == (
        "ontology_versions.id",
        "RESTRICT",
        "fk_graph_publications_ontology",
    )
    for name, fk_name in (
        ("parent_publication_id", "fk_graph_publications_parent"),
        ("rollback_target_publication_id", "fk_graph_publications_rollback_target"),
        ("superseded_by_publication_id", "fk_graph_publications_superseded_by"),
    ):
        assert _foreign_key(cols[name]) == ("graph_publications.id", "SET NULL", fk_name)
    for name, fk_name in (
        ("planned_by_user_id", "fk_graph_publications_planned_by"),
        ("activated_by_user_id", "fk_graph_publications_activated_by"),
        ("cancelled_by_user_id", "fk_graph_publications_cancelled_by"),
    ):
        assert _foreign_key(cols[name]) == ("sys_users.id", "SET NULL", fk_name)

    assert {
        "ck_graph_publications_status",
        "ck_graph_publications_source_mode",
        "ck_graph_publications_counts",
    } <= _constraint_names(table, CheckConstraint)
    status_sql = _check_sql(table, "ck_graph_publications_status")
    for value in (
        "planned",
        "activating",
        "active",
        "degraded",
        "superseded",
        "cancelled",
        "failed",
    ):
        assert value in status_sql
    source_sql = _check_sql(table, "ck_graph_publications_source_mode")
    for value in ("initial_seed", "manual_plan", "rollback"):
        assert value in source_sql

    assert {
        "uq_graph_publications_current_scope",
        "uq_graph_publications_reusable_manifest",
        "uq_graph_publications_nonterminal_command",
        "ix_graph_publications_library_ontology_status",
        "ix_graph_publications_library_status_updated",
        "ix_graph_publications_parent",
        "ix_graph_publications_rollback_target",
    } <= _index_names(table)
    assert "active" in _index_where_sql(table, "uq_graph_publications_current_scope")
    assert "degraded" in _index_where_sql(table, "uq_graph_publications_current_scope")
    reusable_where = _index_where_sql(table, "uq_graph_publications_reusable_manifest")
    for value in ("planned", "activating", "active", "degraded"):
        assert value in reusable_where
    command_where = _index_where_sql(table, "uq_graph_publications_nonterminal_command")
    assert "planned" in command_where and "activating" in command_where


def test_graph_publication_item_contract():
    from app.models.graph_publication_item import GraphPublicationItem

    table = GraphPublicationItem.__table__
    cols = table.c
    required = (
        "id",
        "publication_id",
        "library_id",
        "ontology_version_id",
        "item_kind",
        "entity_id",
        "relation_id",
        "item_hash",
        "status",
        "support_evidence_ids",
        "support_counts",
        "fact_snapshot",
        "created_at",
        "updated_at",
    )
    assert set(required) <= set(cols.keys())
    assert "blocked_reason" not in cols

    assert cols.item_kind.nullable is False
    assert cols.item_hash.type.length == 64
    assert cols.status.nullable is False
    for name in ("support_evidence_ids", "support_counts", "fact_snapshot"):
        assert isinstance(cols[name].type, JSONB)
        assert cols[name].nullable is False

    assert _foreign_key(cols.publication_id) == (
        "graph_publications.id",
        "CASCADE",
        "fk_graph_publication_items_publication",
    )
    assert _foreign_key(cols.library_id) == (
        "sys_libraries.id",
        "CASCADE",
        "fk_graph_publication_items_library",
    )
    assert _foreign_key(cols.ontology_version_id) == (
        "ontology_versions.id",
        "RESTRICT",
        "fk_graph_publication_items_ontology",
    )
    assert _foreign_key(cols.entity_id) == (
        "entities.id",
        "RESTRICT",
        "fk_graph_publication_items_entity",
    )
    assert _foreign_key(cols.relation_id) == (
        "knowledge_relations.id",
        "RESTRICT",
        "fk_graph_publication_items_relation",
    )

    assert {
        "ck_graph_publication_items_kind",
        "ck_graph_publication_items_status",
        "ck_graph_publication_items_exact_target",
    } <= _constraint_names(table, CheckConstraint)
    target_sql = _check_sql(table, "ck_graph_publication_items_exact_target")
    assert "entity_id is not null" in target_sql
    assert "relation_id is not null" in target_sql

    assert {
        "uq_graph_publication_items_publication_entity",
        "uq_graph_publication_items_publication_relation",
        "uq_graph_publication_items_publication_hash",
        "ix_graph_publication_items_scope_kind_status",
        "ix_graph_publication_items_entity",
        "ix_graph_publication_items_relation",
    } <= _index_names(table)
    assert "entity_id is not null" in _index_where_sql(
        table, "uq_graph_publication_items_publication_entity"
    )
    assert "relation_id is not null" in _index_where_sql(
        table, "uq_graph_publication_items_publication_relation"
    )


def test_publication_models_do_not_store_sensitive_payload_text():
    from app.models.graph_publication import GraphPublication
    from app.models.graph_publication_item import GraphPublicationItem

    forbidden = {
        "context_json",
        "context_text",
        "raw_response",
        "parsed_response",
        "parse_error",
        "quote_text",
        "evidence_text_snapshot",
        "candidate_payload",
        "raw_payload",
        "api_key",
        "secret",
        "prompt",
    }
    for table in (GraphPublication.__table__, GraphPublicationItem.__table__):
        names = set(table.c.keys())
        assert forbidden.isdisjoint(names)


def test_v05_does_not_add_direct_publication_provenance_to_formal_rows():
    from app.models.entity import Entity
    from app.models.knowledge_relation import KnowledgeRelation

    for table in (Entity.__table__, KnowledgeRelation.__table__):
        assert "published_by_publication_id" not in table.c
        assert "graph_publication_id" not in table.c


def test_v05_publication_config_defaults_and_validation():
    from app.config import Settings, validate_graph_publication_startup

    config = Settings()
    assert config.graph_publication_enabled is False
    assert config.graph_publication_require_entity_evidence is True
    assert config.graph_publication_extracted_entity_min_confidence == 0.85
    assert config.graph_publication_extracted_relation_min_confidence == 0.85
    assert config.graph_publication_max_items_per_run == 10_000
    assert config.graph_publication_policy_version == "v1"
    assert config.graph_publication_manifest_version == "v1"
    validate_graph_publication_startup(config)

    for kwargs in (
        {"graph_publication_extracted_entity_min_confidence": -0.1},
        {"graph_publication_extracted_relation_min_confidence": 1.1},
        {"graph_publication_max_items_per_run": 0},
        {"graph_publication_policy_version": ""},
        {"graph_publication_manifest_version": " "},
    ):
        bad = Settings(**kwargs)
        try:
            validate_graph_publication_startup(bad)
        except RuntimeError:
            pass
        else:  # pragma: no cover - assertion clarity
            raise AssertionError(f"expected invalid graph publication config: {kwargs}")


def test_v05_m1_migration_is_0023_and_contains_publication_schema_only():
    text = MIGRATION.read_text(encoding="utf-8")

    assert 'revision: str = "0023"' in text
    assert 'down_revision: Union[str, None] = "0022"' in text
    for helper_name, table_name in (
        ("_create_graph_publications", "graph_publications"),
        ("_create_graph_publication_items", "graph_publication_items"),
    ):
        assert f"def {helper_name}() -> None:" in text
        assert f'table = "{table_name}"' in text
        assert f'op.drop_table("{table_name}")' in text

    required = (
        "include_drafts",
        "plan_options",
        "blocked_counts",
        "blocked_diagnostics",
        "item_hashes_summary",
        "uq_graph_publications_current_scope",
        "uq_graph_publications_reusable_manifest",
        "uq_graph_publications_nonterminal_command",
        "uq_graph_publication_items_publication_entity",
        "uq_graph_publication_items_publication_relation",
        "uq_graph_publication_items_publication_hash",
        "ck_graph_publication_items_exact_target",
    )
    for needle in required:
        assert needle in text

    forbidden = (
        "blocked_reason",
        "context_text",
        "raw_response",
        "quote_text",
        "evidence_text_snapshot",
        "candidate_payload",
        "CHAT_API_KEY",
        "qdrant",
        "graph retrieval",
    )
    for needle in forbidden:
        assert needle not in text
