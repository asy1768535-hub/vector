from __future__ import annotations

import io
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0068_p2_2_stable_predicate_resolution_policy.py"


def _offline_upgrade() -> str:
    output = io.StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    command.upgrade(config, "0067:0068", sql=True)
    return output.getvalue().lower()


def _offline_downgrade() -> str:
    output = io.StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    command.downgrade(config, "0068:0067", sql=True)
    return output.getvalue().lower()


def test_0068_migration_adds_policy_and_conservative_resolved_downgrade():
    migration = MIGRATION.read_text(encoding="utf-8").lower()

    for clause in (
        "resolution_policy",
        "postgresql.jsonb",
        "ck_stable_predicate_identities_resolution_policy_json",
        "ck_stable_predicate_identities_resolved_requires_policy",
        "update stable_predicate_identities",
        "set resolution_status = 'pending'",
        "where resolution_status = 'resolved'",
        "resolution_policy is null",
    ):
        assert clause in migration


def test_0068_leaves_0067_pending_legacy_scaffolds_unchanged():
    migration = MIGRATION.read_text(encoding="utf-8").lower()

    assert "where resolution_status = 'resolved'" in migration
    assert "identity_policy_version = 'legacy_v1'" not in migration
    assert "contract_version = 'legacy_v1'" not in migration


def test_0068_offline_upgrade_and_downgrade_emit_expected_schema_sql():
    upgrade = _offline_upgrade()
    downgrade = _offline_downgrade()

    assert "add column resolution_policy jsonb" in upgrade
    assert "ck_stable_predicate_identities_resolution_policy_json" in upgrade
    assert "ck_stable_predicate_identities_resolved_requires_policy" in upgrade
    assert "set resolution_status = 'pending'" in upgrade
    assert "drop constraint ck_stable_predicate_identities_resolved_requires_policy" in downgrade
    assert "drop constraint ck_stable_predicate_identities_resolution_policy_json" in downgrade
    assert "drop column resolution_policy" in downgrade


def test_0071_is_the_only_alembic_head():
    config = Config(str(ROOT / "alembic.ini"))

    assert ScriptDirectory.from_config(config).get_heads() == ["0071"]
