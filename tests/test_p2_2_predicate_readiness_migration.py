from __future__ import annotations

import io
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.fact_foundation import StablePredicateIdentity


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0067_p2_2_historical_predicate_readiness.py"


def _offline_upgrade() -> str:
    output = io.StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    command.upgrade(config, "0066:0067", sql=True)
    return output.getvalue().lower()


def _offline_downgrade() -> str:
    output = io.StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    command.downgrade(config, "0067:0066", sql=True)
    return output.getvalue().lower()


def test_0067_downgrades_only_0066_historical_predicate_scaffolds():
    migration = MIGRATION.read_text(encoding="utf-8").lower()

    for clause in (
        "update stable_predicate_identities as predicate",
        "set resolution_status = 'pending'",
        "predicate.namespace = 'legacy.relation_type.' || mapping.relation_type_id::text",
        "predicate.contract_version = 'legacy_v1'",
        "predicate.identity_policy_version = 'legacy_v1'",
        "predicate.temporal_class = 'state_fact'",
        "predicate.resolution_status = 'resolved'",
        "mapping.mapping_status = 'active'",
        "mapping.library_id = predicate.library_id",
        "mapping.stable_predicate_identity_id = predicate.id",
    ):
        assert clause in migration

    assert "update stable_predicate_mappings" not in migration
    assert "set temporal_class" not in migration
    assert "logical_facts" not in migration
    assert "fact_assertions" not in migration
    assert "fact_resolution_decisions" not in migration


def test_0067_downgrade_only_restores_the_same_pending_scaffold_contract():
    migration = MIGRATION.read_text(encoding="utf-8").lower()
    downgrade = migration.split("def downgrade", maxsplit=1)[1]

    assert "set resolution_status = 'resolved'" in downgrade
    assert "predicate.resolution_status = 'pending'" in downgrade
    assert "predicate.identity_policy_version = 'legacy_v1'" in downgrade
    assert "mapping.mapping_status = 'active'" in downgrade


def test_explicitly_resolved_nonlegacy_predicates_are_outside_0067_scope():
    migration = MIGRATION.read_text(encoding="utf-8").lower()

    assert "predicate.identity_policy_version = 'legacy_v1'" in migration
    assert "predicate.contract_version = 'legacy_v1'" in migration


def test_new_stable_predicates_default_to_pending_readiness():
    column = StablePredicateIdentity.__table__.c.resolution_status

    assert column.default is not None
    assert column.default.arg == "pending"
    assert column.server_default is not None
    assert str(column.server_default.arg) == "pending"


def test_0067_offline_upgrade_and_downgrade_emit_targeted_sql():
    upgrade = _offline_upgrade()
    downgrade = _offline_downgrade()

    assert "update stable_predicate_identities as predicate" in upgrade
    assert "set resolution_status = 'pending'" in upgrade
    assert "set resolution_status = 'resolved'" in downgrade
    assert "predicate.resolution_status = 'pending'" in downgrade


def test_0072_is_the_only_alembic_head():
    config = Config(str(ROOT / "alembic.ini"))

    assert ScriptDirectory.from_config(config).get_heads() == ["0073"]
