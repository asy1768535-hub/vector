from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from app.config import (
    Settings,
    validate_public_api_operations_startup,
)
from app.models.public_api_operations import (
    PublicAPIAnswerLease,
    PublicAPIRateWindow,
    PublicAPIRequestRecord,
)
from app.models.user import User
from app.services.organization_authorization import (
    bind_credential_organization,
    credential_organization_scope,
)
from app.services.public_api_operations_contracts import (
    PUBLIC_OPERATION_RECORD_FIELDS,
    PUBLIC_ENDPOINT_KEYS,
    PublicAdmissionScope,
    PublicOperationContext,
    PublicOperationRecord,
)


ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "alembic" / "versions" / "0040_v09_public_api_operations.py"
NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)
REQUEST_ID = "0123456789abcdef0123456789abcdef"


def _enabled_settings(**overrides) -> Settings:
    values = {
        "public_api_v1_enabled": True,
        "organization_authorization_enabled": True,
        "public_api_operations_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _offline(command_name: str, revision: str) -> str:
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    if command_name == "upgrade":
        alembic_command.upgrade(config, revision, sql=True)
    else:
        alembic_command.downgrade(config, revision, sql=True)
    return " ".join(output.getvalue().lower().split())


def test_operations_defaults_are_inert_and_dependencies_fail_closed():
    defaults = Settings(_env_file=None)
    assert defaults.public_api_operations_enabled is False
    assert defaults.public_api_limits_enabled is False
    assert defaults.public_api_limit_private_organizations is False
    validate_public_api_operations_startup(defaults)

    with pytest.raises(RuntimeError, match="operations require"):
        validate_public_api_operations_startup(
            Settings(_env_file=None, public_api_operations_enabled=True)
        )
    with pytest.raises(RuntimeError, match="limits require"):
        validate_public_api_operations_startup(
            Settings(_env_file=None, public_api_limits_enabled=True)
        )
    with pytest.raises(RuntimeError, match="Private Organization limits require"):
        validate_public_api_operations_startup(
            Settings(_env_file=None, public_api_limit_private_organizations=True)
        )
    validate_public_api_operations_startup(
        _enabled_settings(public_api_limits_enabled=True)
    )
    validate_public_api_operations_startup(
        _enabled_settings(
            public_api_limits_enabled=True,
            public_api_limit_private_organizations=True,
        )
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("public_api_organization_requests_per_minute", 0, "request limits"),
        ("public_api_api_key_requests_per_minute", 1_000_001, "request limits"),
        ("public_api_organization_concurrent_answers", 0, "concurrent answer"),
        ("public_api_api_key_concurrent_answers", 10_001, "concurrent answer"),
        ("public_api_answer_max_seconds", 0, "answer timeout"),
        ("public_api_answer_lease_seconds", 329, "answer lease"),
        ("public_api_request_retention_days", 0, "request retention"),
        ("public_api_cleanup_batch_size", 10_001, "cleanup batch"),
        ("public_api_cleanup_interval_seconds", 59, "cleanup interval"),
    ],
)
def test_invalid_operational_bounds_fail_even_while_disabled(field, value, message):
    with pytest.raises(RuntimeError, match=message):
        validate_public_api_operations_startup(
            Settings(_env_file=None, **{field: value})
        )


def test_operation_record_is_strict_content_free_and_bounded():
    organization_id = uuid.uuid4()
    user_id = uuid.uuid4()
    library_ids = (uuid.uuid4(), uuid.uuid4())
    record = PublicOperationRecord(
        request_id=REQUEST_ID,
        organization_id=organization_id,
        user_id=user_id,
        endpoint_key="retrieval.search",
        http_method="POST",
        library_ids=library_ids,
        started_at=NOW,
        finished_at=NOW + timedelta(milliseconds=10),
        duration_ms=10,
        http_status=200,
        outcome="completed",
        source_count=2,
        chunk_count=2,
    )
    assert set(PublicOperationRecord.model_fields) == PUBLIC_OPERATION_RECORD_FIELDS
    assert record.library_ids == library_ids

    forbidden = {
        "question",
        "query",
        "answer",
        "prompt",
        "content",
        "chunk_text",
        "evidence_text",
        "document_title",
        "url",
        "object_key",
        "credential",
        "header",
        "ip_address",
        "user_agent",
        "provider_payload",
        "exception",
    }
    assert forbidden.isdisjoint(PUBLIC_OPERATION_RECORD_FIELDS)
    assert set(PUBLIC_ENDPOINT_KEYS) == {
        "libraries.list",
        "scopes.validate",
        "documents.get",
        "entities.get",
        "relations.get",
        "evidence.get",
        "entities.search",
        "relations.search",
        "retrieval.search",
        "answers.create",
        "answers.stream",
    }
    with pytest.raises(ValidationError):
        PublicOperationRecord(**record.model_dump(), question="secret")
    with pytest.raises(ValidationError):
        PublicOperationRecord(
            **record.model_dump(exclude={"library_ids"}),
            library_ids=(library_ids[0], library_ids[0]),
        )
    with pytest.raises(ValidationError):
        PublicOperationRecord(
            **record.model_dump(exclude={"outcome", "error_code"}),
            outcome="completed",
            error_code="upstream_failed",
        )


def test_context_and_admission_keep_only_bounded_identifiers():
    user_id = uuid.uuid4()
    organization_ids = (uuid.uuid4(), uuid.uuid4())
    library_id = uuid.uuid4()
    context = PublicOperationContext(
        request_id=REQUEST_ID,
        endpoint_key="libraries.list",
        http_method="GET",
        user_id=user_id,
        started_at=NOW - timedelta(seconds=1),
    )
    context.bind_scope(organization_ids=organization_ids, library_ids=(library_id,))
    record = context.terminal_record(
        http_status=200,
        outcome="completed",
        finished_at=NOW,
    )
    assert context.organization_ids == tuple(sorted(organization_ids, key=str))
    assert record.organization_id is None
    assert record.library_ids == (library_id,)
    with pytest.raises(ValidationError):
        PublicAdmissionScope(organization_ids=(organization_ids[0],) * 2)


def test_api_key_identity_is_non_secret_and_cookie_scope_remains_compatible():
    user = User(id=uuid.uuid4(), email="member@example.com", hashed_password="hash")
    organization_id = uuid.uuid4()
    key_id = uuid.uuid4()
    bind_credential_organization(user, organization_id)
    cookie_compatible = credential_organization_scope(user)
    assert cookie_compatible is not None
    assert cookie_compatible.organization_id == organization_id
    assert cookie_compatible.api_key_id is None

    bind_credential_organization(user, organization_id, key_id)
    api_key_scope = credential_organization_scope(user)
    assert api_key_scope is not None
    assert api_key_scope.api_key_id == key_id
    assert set(api_key_scope.__dataclass_fields__) == {"organization_id", "api_key_id"}


def test_0040_orm_migration_and_offline_sql_are_exactly_reversible():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_revision("0040").down_revision == "0039"
    assert script.get_revision("0041").down_revision == "0040"

    request_columns = set(PublicAPIRequestRecord.__table__.c.keys())
    assert request_columns == {"id", *PUBLIC_OPERATION_RECORD_FIELDS, "created_at"}
    assert set(PublicAPIRateWindow.__table__.c.keys()) == {
        "scope_kind",
        "scope_id",
        "window_started_at",
        "request_count",
        "created_at",
        "updated_at",
    }
    assert set(PublicAPIAnswerLease.__table__.c.keys()) == {
        "id",
        "request_id",
        "organization_id",
        "api_key_id",
        "endpoint_key",
        "acquired_at",
        "expires_at",
    }

    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0040"' in migration
    assert 'down_revision: Union[str, None] = "0039"' in migration
    for table in (
        "public_api_request_records",
        "public_api_rate_windows",
        "public_api_answer_leases",
    ):
        assert f'op.create_table(\n        "{table}"' in migration
        assert f'op.drop_table("{table}")' in migration

    upgrade = _offline("upgrade", "0039:0040")
    downgrade = _offline("downgrade", "0040:0039")
    for table in (
        "public_api_request_records",
        "public_api_rate_windows",
        "public_api_answer_leases",
    ):
        assert f"create table {table}" in upgrade
        assert f"drop table {table}" in downgrade
