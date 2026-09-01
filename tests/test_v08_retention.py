from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import CheckConstraint, UniqueConstraint


MIGRATION = Path("alembic/versions/0027_v08_revision_retention.py")


def _constraint_sql(table, name: str) -> str:
    constraint = next(item for item in table.constraints if item.name == name)
    assert isinstance(constraint, CheckConstraint)
    return " ".join(str(constraint.sqltext).lower().split())


def test_retention_orm_and_migration_contract_match():
    from app.models.library import Library
    from app.models.revision_retention import RevisionRetentionRecord

    library_columns = Library.__table__.columns
    assert {
        "revision_retention_enabled",
        "revision_retention_days",
        "revision_retention_notice_days",
    } <= set(library_columns.keys())
    assert "between 30 and 60" in _constraint_sql(
        Library.__table__, "ck_lib_revision_retention_policy"
    )

    columns = RevisionRetentionRecord.__table__.columns
    expected = {
        "library_id",
        "document_id",
        "document_revision_id",
        "replacement_revision_id",
        "revision_file_id",
        "reason",
        "status",
        "policy_version",
        "retention_days",
        "notice_days",
        "replacement_ready_at",
        "cleanup_eligible_at",
        "cleanup_not_before",
        "notice_at",
        "notice_recorded_at",
        "block_code",
        "impact_snapshot",
        "impact_hash",
        "idempotency_key",
        "deadline_changed_by_user_id",
        "deadline_changed_at",
        "hold_reason_code",
        "held_by_user_id",
        "held_at",
    }
    assert expected <= set(columns.keys())
    assert "processing" in _constraint_sql(
        RevisionRetentionRecord.__table__, "ck_revision_retention_status"
    )
    assert "octet_length(impact_snapshot::text) <= 8192" in _constraint_sql(
        RevisionRetentionRecord.__table__, "ck_revision_retention_impact"
    )
    assert "status = 'held'" in _constraint_sql(
        RevisionRetentionRecord.__table__, "ck_revision_retention_hold_shape"
    )
    unique = {
        item.name
        for item in RevisionRetentionRecord.__table__.constraints
        if isinstance(item, UniqueConstraint)
    }
    assert unique >= {
        "uq_revision_retention_revision_file",
        "uq_revision_retention_idempotency",
    }

    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0027"' in migration
    assert 'down_revision: Union[str, None] = "0026"' in migration
    assert "revision_retention_records" in migration
    for name in expected:
        assert f'"{name}"' in migration
    assert "DELETE FROM" not in migration


def test_retention_startup_is_default_off_and_enabled_limits_fail_closed():
    from app.config import Settings, validate_revision_retention_startup

    default = Settings(_env_file=None)
    assert default.revision_retention_enabled is False
    validate_revision_retention_startup(
        Settings(
            _env_file=None,
            revision_retention_enabled=False,
            revision_retention_batch_size=0,
        )
    )
    with pytest.raises(RuntimeError, match="batch size"):
        validate_revision_retention_startup(
            Settings(
                _env_file=None,
                revision_retention_enabled=True,
                revision_retention_batch_size=0,
            )
        )
    with pytest.raises(RuntimeError, match="Evidence sample"):
        validate_revision_retention_startup(
            Settings(
                _env_file=None,
                revision_retention_enabled=True,
                revision_retention_impact_evidence_sample=101,
            )
        )


def test_existing_library_admin_contract_can_configure_retention_policy():
    from pydantic import ValidationError

    from app.api.admin_libraries import create_library, update_library
    from app.schemas.admin import LibraryCreate, LibraryRead, LibraryUpdate

    created = LibraryCreate(slug="retention_lib", name="Retention")
    assert created.revision_retention_enabled is False
    assert created.revision_retention_days == 60
    assert created.revision_retention_notice_days == 7
    update = LibraryUpdate(
        revision_retention_enabled=True,
        revision_retention_days=45,
        revision_retention_notice_days=10,
    )
    assert update.revision_retention_days == 45
    with pytest.raises(ValidationError):
        LibraryUpdate(
            revision_retention_days=30,
            revision_retention_notice_days=30,
        )
    assert "revision_retention_days" in LibraryRead.model_fields
    create_source = inspect.getsource(create_library)
    update_source = inspect.getsource(update_library)
    for field in (
        "revision_retention_enabled",
        "revision_retention_days",
        "revision_retention_notice_days",
    ):
        assert field in create_source
        assert field in update_source


class _Scalars:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _Result:
    def __init__(self, rows=(), *, scalar=None):
        self.rows = list(rows)
        self.scalar = scalar

    def scalars(self):
        return _Scalars(self.rows)

    def scalar_one(self):
        return self.scalar

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return self.rows


class _Nested:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Db:
    def __init__(self, results, library=None):
        self.results = list(results)
        self.library = library
        self.statements = []
        self.added = []

    async def execute(self, statement):
        self.statements.append(statement)
        return self.results.pop(0)

    async def get(self, model, row_id):
        del model, row_id
        return self.library

    def begin_nested(self):
        return _Nested()

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        return None


def _scope(now: datetime):
    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    old_revision_id = uuid.uuid4()
    replacement_revision_id = uuid.uuid4()
    file_id = uuid.uuid4()
    document = SimpleNamespace(
        id=document_id,
        library_id=library_id,
        current_revision_id=replacement_revision_id,
        deleted_at=None,
    )
    library = SimpleNamespace(
        id=library_id,
        lifecycle_mode="managed",
        deleted_at=None,
        revision_retention_enabled=True,
        revision_retention_days=60,
        revision_retention_notice_days=7,
    )
    old_revision = SimpleNamespace(
        id=old_revision_id,
        library_id=library_id,
        document_id=document_id,
        status="superseded",
    )
    replacement = SimpleNamespace(
        id=replacement_revision_id,
        library_id=library_id,
        document_id=document_id,
        status="ready",
        published_at=now,
        finished_at=now,
        created_at=now - timedelta(minutes=1),
    )
    revision_file = SimpleNamespace(
        id=file_id,
        library_id=library_id,
        document_id=document_id,
        document_revision_id=old_revision_id,
    )
    return SimpleNamespace(
        library_id=library_id,
        document_id=document_id,
        old_revision_id=old_revision_id,
        replacement_revision_id=replacement_revision_id,
        document=document,
        library=library,
        old_revision=old_revision,
        replacement=replacement,
        revision_file=revision_file,
    )


def _impact_results(*, mentions=0, relations=0, publication_items=0, evidence_ids=()):
    return [
        _Result(scalar=mentions),
        _Result(scalar=relations),
        _Result(scalar=publication_items),
        _Result(evidence_ids),
    ]


def test_impact_projection_is_bounded_canonical_and_contains_no_source_content():
    from app.config import Settings
    from app.services.revision_retention import (
        _canonical_impact,
        project_revision_retention_impact,
    )

    evidence_id = uuid.uuid4()
    db = _Db(_impact_results(mentions=1, relations=2, publication_items=3, evidence_ids=[evidence_id]))
    impact = asyncio.run(
        project_revision_retention_impact(
            db,
            library_id=uuid.uuid4(),
            document_revision_id=uuid.uuid4(),
            config=Settings(
                _env_file=None,
                revision_retention_impact_evidence_sample=20,
            ),
        )
    )
    payload, digest = _canonical_impact(impact)
    assert impact.has_dependencies is True
    assert payload == {
        "active_entity_mentions": 1,
        "active_relation_evidence": 2,
        "current_publication_items": 3,
        "dependency_evidence_ids": [str(evidence_id)],
    }
    assert len(digest) == 64
    serialized = repr(payload).lower()
    for forbidden in ("quote", "text", "url", "secret", "prompt", "content"):
        assert forbidden not in serialized
    assert all("limit" in str(stmt).lower() for stmt in db.statements[-1:])


def test_schedule_freezes_policy_and_is_idempotent_with_document_first_locking():
    from app.config import Settings
    from app.services.revision_retention import schedule_revision_retention

    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    scope = _scope(now)
    db = _Db(
        [
            _Result([scope.document]),
            _Result([scope.old_revision]),
            _Result([scope.replacement]),
            _Result([scope.revision_file]),
            _Result([]),
            *_impact_results(),
        ],
        scope.library,
    )
    config = Settings(_env_file=None, revision_retention_enabled=True)
    result = asyncio.run(
        schedule_revision_retention(
            db,
            library_id=scope.library_id,
            document_id=scope.document_id,
            document_revision_id=scope.old_revision_id,
            replacement_revision_id=scope.replacement_revision_id,
            at=now,
            config=config,
        )
    )
    assert result.created is True
    assert result.code == "retention_created"
    row = result.record
    assert row is not None and row in db.added
    assert row.status == "scheduled"
    assert row.cleanup_not_before == now + timedelta(days=60)
    assert "replacement_ready_at" not in inspect.signature(
        schedule_revision_retention
    ).parameters
    assert row.notice_at == row.cleanup_not_before - timedelta(days=7)
    assert row.impact_snapshot["dependency_evidence_ids"] == []
    sql = [str(stmt).lower() for stmt in db.statements[:5]]
    assert "from documents" in sql[0]
    assert "from document_revisions" in sql[1]
    assert "from document_revisions" in sql[2]
    assert "from document_revision_files" in sql[3]
    assert "from revision_retention_records" in sql[4]

    duplicate_db = _Db(
        [
            _Result([scope.document]),
            _Result([scope.old_revision]),
            _Result([scope.replacement]),
            _Result([scope.revision_file]),
            _Result([row]),
        ],
        scope.library,
    )
    duplicate = asyncio.run(
        schedule_revision_retention(
            duplicate_db,
            library_id=scope.library_id,
            document_id=scope.document_id,
            document_revision_id=scope.old_revision_id,
            replacement_revision_id=scope.replacement_revision_id,
            at=now,
            config=config,
        )
    )
    assert duplicate.record is row
    assert duplicate.created is False
    assert duplicate.code == "retention_exists"
    assert duplicate_db.added == []


def _refresh_db(scope, row, *, publication_items: int, evidence_ids=()):
    scope_row = SimpleNamespace(
        document_id=scope.document_id,
        library_id=scope.library_id,
    )
    return _Db(
        [
            _Result([scope_row]),
            _Result([scope.document]),
            _Result([row]),
            _Result([scope.old_revision]),
            _Result([scope.revision_file]),
            *_impact_results(
                publication_items=publication_items,
                evidence_ids=evidence_ids,
            ),
            _Result([scope.replacement]),
        ],
        scope.library,
    )


def test_refresh_blocks_on_publication_dependency_then_recovers_to_eligible():
    from app.config import Settings
    from app.services.revision_retention import refresh_revision_retention

    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    scope = _scope(now)
    record_id = uuid.uuid4()
    row = SimpleNamespace(
        id=record_id,
        library_id=scope.library_id,
        document_id=scope.document_id,
        document_revision_id=scope.old_revision_id,
        replacement_revision_id=scope.replacement_revision_id,
        revision_file_id=scope.revision_file.id,
        status="scheduled",
        cleanup_not_before=now + timedelta(days=60),
        hold_reason_code=None,
        held_by_user_id=None,
        held_at=None,
        block_code=None,
    )
    config = Settings(_env_file=None, revision_retention_enabled=True)
    evidence_id = uuid.uuid4()
    blocked_db = _refresh_db(
        scope,
        row,
        publication_items=1,
        evidence_ids=[evidence_id],
    )
    blocked = asyncio.run(
        refresh_revision_retention(
            blocked_db,
            record_id=record_id,
            at=now + timedelta(days=61),
            config=config,
        )
    )
    assert blocked.status == "blocked"
    assert blocked.block_code == "active_graph_dependency"
    assert blocked.impact_snapshot["dependency_evidence_ids"] == [str(evidence_id)]

    eligible_db = _refresh_db(scope, row, publication_items=0)
    eligible = asyncio.run(
        refresh_revision_retention(
            eligible_db,
            record_id=record_id,
            at=now + timedelta(days=61),
            config=config,
        )
    )
    assert eligible.status == "eligible"
    assert eligible.block_code is None


def test_hold_and_extension_commands_are_bounded_audited_and_idempotent():
    from app.services.revision_retention import (
        RevisionRetentionError,
        extend_revision_retention_deadline,
        hold_revision_retention,
    )

    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    scope = _scope(now)
    actor_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        document_revision_id=scope.old_revision_id,
        cleanup_eligible_at=now,
        cleanup_not_before=now + timedelta(days=30),
        notice_days=7,
        notice_at=now + timedelta(days=23),
        notice_recorded_at=None,
        status="scheduled",
        block_code=None,
        hold_reason_code=None,
        held_by_user_id=None,
        held_at=None,
        deadline_changed_by_user_id=None,
        deadline_changed_at=None,
    )
    scope_row = SimpleNamespace(
        document_id=scope.document_id,
        library_id=scope.library_id,
    )

    def command_db():
        return _Db(
            [
                _Result([scope_row]),
                _Result([scope.document]),
                _Result([row]),
            ]
        )

    audit = AsyncMock()
    with patch("app.services.revision_retention.audit_log.record", new=audit):
        extended = asyncio.run(
            extend_revision_retention_deadline(
                command_db(),
                record_id=row.id,
                actor_user_id=actor_id,
                cleanup_not_before=now + timedelta(days=45),
                at=now,
            )
        )
    assert extended.cleanup_not_before == now + timedelta(days=45)
    assert audit.await_count == 1
    row.notice_recorded_at = now + timedelta(days=38)
    audit.reset_mock()
    with patch("app.services.revision_retention.audit_log.record", new=audit):
        extended_again = asyncio.run(
            extend_revision_retention_deadline(
                command_db(),
                record_id=row.id,
                actor_user_id=actor_id,
                cleanup_not_before=now + timedelta(days=50),
                at=now + timedelta(days=40),
            )
        )
    assert extended_again.notice_at == now + timedelta(days=43)
    assert extended_again.notice_recorded_at is None
    assert audit.await_count == 1
    with pytest.raises(RevisionRetentionError):
        asyncio.run(
            extend_revision_retention_deadline(
                command_db(),
                record_id=row.id,
                actor_user_id=actor_id,
                cleanup_not_before=now + timedelta(days=61),
                at=now,
            )
        )

    audit.reset_mock()
    with patch("app.services.revision_retention.audit_log.record", new=audit):
        held = asyncio.run(
            hold_revision_retention(
                command_db(),
                record_id=row.id,
                actor_user_id=actor_id,
                reason_code="legal_hold",
                at=now,
            )
        )
        held_again = asyncio.run(
            hold_revision_retention(
                command_db(),
                record_id=row.id,
                actor_user_id=actor_id,
                reason_code="legal_hold",
                at=now,
            )
        )
    assert held is held_again is row
    assert row.status == "held"
    assert row.hold_reason_code == "legal_hold"
    assert audit.await_count == 1


def test_due_notice_recording_is_bounded_and_idempotent():
    from app.services.revision_retention import record_due_retention_notices

    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    rows = [
        SimpleNamespace(id=uuid.uuid4(), notice_recorded_at=None),
        SimpleNamespace(id=uuid.uuid4(), notice_recorded_at=None),
    ]
    db = _Db([_Result(rows)])
    ids = asyncio.run(record_due_retention_notices(db, limit=2, at=now))
    assert ids == tuple(row.id for row in rows)
    assert all(row.notice_recorded_at == now for row in rows)
    sql = str(db.statements[0]).lower()
    assert "limit" in sql and "for update" in sql


def test_compensation_counts_only_new_records_and_uses_not_exists():
    from app.config import Settings
    from app.services.revision_retention import (
        ScheduleResult,
        compensate_revision_retention_records,
    )

    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    record = SimpleNamespace(id=uuid.uuid4())
    candidate = SimpleNamespace(
        library_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_revision_id=uuid.uuid4(),
        replacement_revision_id=uuid.uuid4(),
        replacement_published_at=now,
        replacement_finished_at=None,
        replacement_created_at=now - timedelta(minutes=1),
    )
    db = _Db([_Result([candidate])])
    schedule = AsyncMock(
        return_value=ScheduleResult(record, True, "retention_created")
    )
    with patch(
        "app.services.revision_retention.schedule_revision_retention",
        new=schedule,
    ):
        created = asyncio.run(
            compensate_revision_retention_records(
                db,
                limit=10,
                at=now,
                config=Settings(_env_file=None, revision_retention_enabled=True),
            )
        )
    assert created == (record.id,)
    assert "not (exists" in str(db.statements[0]).lower()
    schedule.assert_awaited_once()


def test_maintenance_is_bounded_and_composes_compensation_notice_and_refresh():
    from app.config import Settings
    from app.services.revision_retention import run_revision_retention_maintenance

    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    created_id, notice_id, refresh_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    db = _Db([_Result([refresh_id])])
    compensate = AsyncMock(return_value=(created_id,))
    notices = AsyncMock(return_value=(notice_id,))
    refresh = AsyncMock(return_value=SimpleNamespace(id=refresh_id))
    config = Settings(
        _env_file=None,
        revision_retention_enabled=True,
        revision_retention_batch_size=25,
    )
    with (
        patch(
            "app.services.revision_retention.compensate_revision_retention_records",
            new=compensate,
        ),
        patch(
            "app.services.revision_retention.record_due_retention_notices",
            new=notices,
        ),
        patch(
            "app.services.revision_retention.refresh_revision_retention",
            new=refresh,
        ),
    ):
        result = asyncio.run(
            run_revision_retention_maintenance(db, at=now, config=config)
        )
    assert result.created_record_ids == (created_id,)
    assert result.notice_record_ids == (notice_id,)
    assert result.refreshed_record_ids == (refresh_id,)
    assert "limit" in str(db.statements[0]).lower()
    assert compensate.await_args.kwargs["limit"] == 25
    assert notices.await_args.kwargs["limit"] == 25
    refresh.assert_awaited_once_with(
        db,
        record_id=refresh_id,
        at=now,
        config=config,
    )

    disabled_db = _Db([])
    disabled = asyncio.run(
        run_revision_retention_maintenance(
            disabled_db,
            at=now,
            config=Settings(_env_file=None, revision_retention_enabled=False),
        )
    )
    assert disabled.created_record_ids == ()
    assert disabled_db.statements == []


def test_release_hold_clears_hold_shape_and_revalidates():
    from app.config import Settings
    from app.services.revision_retention import release_revision_retention_hold

    now = datetime(2026, 7, 22, tzinfo=timezone.utc)
    scope = _scope(now)
    actor_id = uuid.uuid4()
    row = SimpleNamespace(
        id=uuid.uuid4(),
        document_revision_id=scope.old_revision_id,
        status="held",
        block_code=None,
        hold_reason_code="legal_hold",
        held_by_user_id=actor_id,
        held_at=now,
    )
    scope_row = SimpleNamespace(
        document_id=scope.document_id,
        library_id=scope.library_id,
    )
    db = _Db(
        [
            _Result([scope_row]),
            _Result([scope.document]),
            _Result([row]),
        ]
    )
    audit = AsyncMock()
    refresh = AsyncMock(return_value=row)
    config = Settings(_env_file=None, revision_retention_enabled=True)
    with (
        patch("app.services.revision_retention.audit_log.record", new=audit),
        patch(
            "app.services.revision_retention.refresh_revision_retention",
            new=refresh,
        ),
    ):
        released = asyncio.run(
            release_revision_retention_hold(
                db,
                record_id=row.id,
                actor_user_id=actor_id,
                at=now,
                config=config,
            )
        )
    assert released is row
    assert row.status == "scheduled"
    assert row.hold_reason_code is None
    assert row.held_by_user_id is None
    assert row.held_at is None
    audit.assert_awaited_once()
    refresh.assert_awaited_once_with(db, record_id=row.id, at=now, config=config)


def test_replacement_and_worker_integrations_are_post_commit_default_off_and_never_delete():
    from app.services import revision_retention
    from app.workers import cleanup, embedder

    embedder_source = inspect.getsource(embedder._publish_revision_after_qdrant)
    commit = embedder_source.index("await db.commit()")
    schedule = embedder_source.index("await schedule_revision_retention")
    assert commit < schedule
    assert "settings.revision_retention_enabled" in embedder_source

    cleanup_source = inspect.getsource(cleanup.run)
    assert "run_revision_retention_maintenance" in cleanup_source
    assert "settings.revision_retention_enabled" in cleanup_source

    service_source = inspect.getsource(revision_retention)
    for forbidden in (
        "build_object_storage_adapter",
        "verified_revision_file_bytes",
        ".delete(",
        "delete_points",
    ):
        assert forbidden not in service_source
