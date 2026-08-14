from __future__ import annotations

import json
import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from fastapi import Response

from app.api import health as health_api
from app.api.health import _payload
from app.config import Settings, validate_supported_deployment_startup
from app.services.deployment_bootstrap import DeploymentBootstrapError
from scripts.bootstrap_admin import _validate_admin_email
from scripts.deployment_backup_manifest import (
    BackupManifestError,
    create_manifest,
    verify_manifest,
)


def _supported_settings(**overrides) -> Settings:
    values = {
        "deployment_profile": "private",
        "app_debug": False,
        "allow_public_registration": False,
        "jwt_secret": "x" * 48,
        "db_password": "not-a-default",
        "cookie_secure": True,
        "organization_authorization_enabled": True,
        "revision_file_storage_enabled": True,
        "document_storage_provider": "local",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def test_supported_private_profile_accepts_secure_configuration() -> None:
    validate_supported_deployment_startup(_supported_settings())


def test_bootstrap_admin_rejects_email_that_user_schema_cannot_serialize() -> None:
    assert _validate_admin_email("admin@example.com") == "admin@example.com"
    with pytest.raises(DeploymentBootstrapError, match="administrator email is invalid"):
        _validate_admin_email("admin@local.invalid")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("app_debug", True, "APP_DEBUG"),
        ("allow_public_registration", True, "public registration"),
        ("jwt_secret", "please-change-me-in-env", "JWT_SECRET"),
        ("db_password", "", "DB_PASSWORD"),
        ("cookie_secure", False, "COOKIE_SECURE"),
        ("organization_authorization_enabled", False, "organization authorization"),
        ("revision_file_storage_enabled", False, "revision file storage"),
    ],
)
def test_supported_profile_rejects_unsafe_configuration(
    field: str, value: object, message: str
) -> None:
    with pytest.raises(RuntimeError, match=message):
        validate_supported_deployment_startup(_supported_settings(**{field: value}))


def test_hosted_profile_requires_remote_https_dependencies() -> None:
    with pytest.raises(RuntimeError, match="remote document storage"):
        validate_supported_deployment_startup(
            _supported_settings(deployment_profile="hosted")
        )
    with pytest.raises(RuntimeError, match="QDRANT_URL must use HTTPS"):
        validate_supported_deployment_startup(
            _supported_settings(
                deployment_profile="hosted",
                document_storage_provider="minio",
            )
        )


def test_0047_enables_default_knowledge_artifacts() -> None:
    migration = Path(
        "alembic/versions/0047_enable_default_knowledge_artifacts.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "0047"' in migration
    assert 'down_revision: Union[str, None] = "0046"' in migration
    assert "knowledge_artifact_auto_enabled = true" in migration
    assert "summary_artifact_enabled = true" in migration
    assert "outline_artifact_enabled = true" in migration
    assert "classification_auto_enabled" not in migration


def test_0048_enables_classification_with_default_taxonomy() -> None:
    migration = Path(
        "alembic/versions/0048_enable_default_classification.py"
    ).read_text(encoding="utf-8")
    assert 'revision: str = "0048"' in migration
    assert 'down_revision: Union[str, None] = "0047"' in migration
    assert "classification_auto_enabled = true" in migration
    assert "classification_external_model_enabled = true" in migration
    assert "classification_allowed_security_levels" in migration
    assert "internal" in migration
    assert "chr(58) || 'general-enterprise' || chr(58)" in migration
    assert "':general-enterprise:v1'" not in migration
    assert "INSERT INTO classification_taxonomies" in migration
    assert "INSERT INTO classification_labels" in migration
    assert "INSERT INTO library_classification_labels" in migration
    assert ")) - 1)::integer" in migration

def test_readiness_payload_contains_only_stable_content_free_codes() -> None:
    payload = _payload({"database": True, "embedding": False})
    assert payload == {
        "status": "not_ready",
        "components": {"database": "ok", "embedding": "fail"},
        "failure_codes": ["embedding_unavailable"],
    }
    serialized = json.dumps(payload)
    assert "http" not in serialized
    assert "model" not in serialized
    assert "exception" not in serialized


def test_health_migration_head_matches_alembic_head() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert health_api._migration_heads() == frozenset(script.get_heads())


def test_supported_deployment_runbook_names_current_head() -> None:
    runbook = Path("docs/39-v0.9-supported-deployment.md").read_text(encoding="utf-8")
    assert "expected release head is `0060`" in runbook
    assert "verify `0060`" in runbook


class _FakeMigrationSession:
    def __init__(self, revisions: list[str] | None = None, error: Exception | None = None):
        self.revisions = revisions or []
        self.error = error

    async def __aenter__(self) -> "_FakeMigrationSession":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def execute(self, statement: object) -> object:
        if self.error is not None:
            raise self.error
        return type("Result", (), {"scalars": lambda _: iter(self.revisions)})()


@pytest.mark.parametrize("revisions", [["0060"]])
def test_check_migrations_accepts_current_head(monkeypatch, revisions: list[str]) -> None:
    monkeypatch.setattr(
        health_api,
        "_migration_heads",
        lambda: frozenset({"0060"}),
    )
    monkeypatch.setattr(
        health_api,
        "async_session_factory",
        lambda: _FakeMigrationSession(revisions=revisions),
    )
    assert asyncio.run(health_api._check_migrations()) is True


@pytest.mark.parametrize("revisions", [["0055"], ["0043"], [], ["0060", "0059"]])
def test_check_migrations_rejects_stale_other_and_empty_heads(
    monkeypatch, revisions: list[str]
) -> None:
    monkeypatch.setattr(
        health_api,
        "_migration_heads",
        lambda: frozenset({"0060"}),
    )
    monkeypatch.setattr(
        health_api,
        "async_session_factory",
        lambda: _FakeMigrationSession(revisions=revisions),
    )
    assert asyncio.run(health_api._check_migrations()) is False


def test_check_migrations_fails_closed_on_query_error(monkeypatch) -> None:
    monkeypatch.setattr(
        health_api,
        "async_session_factory",
        lambda: _FakeMigrationSession(error=RuntimeError("database unavailable")),
    )
    assert asyncio.run(health_api._check_migrations()) is False


def test_liveness_does_not_depend_on_migration_state() -> None:
    assert asyncio.run(health_api.liveness()) == {"status": "live"}


def test_readiness_preserves_other_dependency_states(monkeypatch) -> None:
    async def available() -> bool:
        return True

    async def initialization_unavailable() -> bool:
        return False

    async def rerank_not_configured() -> str:
        return "not_configured"

    monkeypatch.setattr(health_api, "_check_db", available)
    monkeypatch.setattr(health_api, "_check_migrations", available)
    monkeypatch.setattr(health_api, "_check_initialization", initialization_unavailable)
    monkeypatch.setattr(health_api, "_check_qdrant", available)
    monkeypatch.setattr(health_api, "_check_embedding", available)
    monkeypatch.setattr(health_api, "_check_rerank", rerank_not_configured)
    monkeypatch.setattr(health_api, "_check_storage", available)

    response = Response()
    payload = asyncio.run(health_api.readiness(response))
    assert response.status_code == 503
    assert payload == {
        "status": "not_ready",
        "components": {
            "database": "ok",
            "embedding": "ok",
            "initialization": "fail",
            "migrations": "ok",
            "object_storage": "ok",
            "qdrant": "ok",
            "rerank": "not_configured",
        },
        "failure_codes": ["initialization_unavailable", "rerank_unavailable"],
    }


def test_chat_source_migration_marks_preexisting_scores_legacy() -> None:
    migration = Path(
        "alembic/versions/0054_chat_source_display_scores.py"
    ).read_text(encoding="utf-8")
    assert "server_default=\"rrf\"" in migration
    assert "SET score_type = 'legacy'" in migration


def test_legacy_health_shape_is_compatible_and_redacted(monkeypatch) -> None:
    async def available() -> bool:
        return True

    async def unavailable() -> bool:
        return False

    monkeypatch.setattr(health_api, "_check_db", available)
    monkeypatch.setattr(health_api, "_check_migrations", available)
    monkeypatch.setattr(health_api, "_check_initialization", available)
    monkeypatch.setattr(health_api, "_check_qdrant", unavailable)
    monkeypatch.setattr(health_api, "_check_embedding", available)
    monkeypatch.setattr(health_api, "_check_rerank", available)
    monkeypatch.setattr(health_api, "_check_storage", available)
    monkeypatch.setattr(health_api, "_check_ocr", lambda: "off")
    response = Response()
    payload = asyncio.run(health_api.health(response))
    assert response.status_code == 200
    assert payload["status"] == "degraded"
    assert payload["db"] is True
    assert payload["qdrant"] is False
    serialized = json.dumps(payload)
    for forbidden in ("embedding_model", "embedding_error", "http://", "exception"):
        assert forbidden not in serialized


def test_backup_manifest_binds_all_artifacts_and_rejects_tampering(
    tmp_path: Path,
) -> None:
    files = {
        "postgresql": tmp_path / "database.dump",
        "object_inventory": tmp_path / "objects.json",
        "qdrant_inventory": tmp_path / "qdrant.json",
    }
    for role, path in files.items():
        path.write_bytes(f"{role}-content".encode())
    manifest_path = tmp_path / "manifest.json"
    create_manifest(files, manifest_path)
    assert verify_manifest(manifest_path)["schema_version"] == "vector-kb-backup-v1"
    files["object_inventory"].write_text("changed", encoding="utf-8")
    with pytest.raises(BackupManifestError, match="integrity"):
        verify_manifest(manifest_path)


def test_backup_manifest_rejects_incomplete_roles(tmp_path: Path) -> None:
    item = tmp_path / "database.dump"
    item.write_bytes(b"dump")
    with pytest.raises(BackupManifestError, match="required"):
        create_manifest({"postgresql": item}, tmp_path / "manifest.json")
