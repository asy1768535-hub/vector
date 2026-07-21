from __future__ import annotations

import asyncio
import inspect
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import CheckConstraint


MIGRATION = Path("alembic/versions/0028_v08_revision_cleanup.py")


def _constraint_sql(table, name: str) -> str:
    constraint = next(item for item in table.constraints if item.name == name)
    assert isinstance(constraint, CheckConstraint)
    return " ".join(str(constraint.sqltext).lower().split())


def test_cleanup_orm_and_migration_contract_match():
    from app.models.document_revision_file import DocumentRevisionFile
    from app.models.revision_retention import RevisionRetentionRecord

    file_columns = DocumentRevisionFile.__table__.columns
    assert {"lifecycle_status", "deleted_at", "delete_verified_at"} <= set(
        file_columns.keys()
    )
    assert "available" in _constraint_sql(
        DocumentRevisionFile.__table__,
        "ck_document_revision_files_lifecycle_status",
    )
    assert "delete_verified_at >= deleted_at" in _constraint_sql(
        DocumentRevisionFile.__table__,
        "ck_document_revision_files_lifecycle_shape",
    )

    record_columns = RevisionRetentionRecord.__table__.columns
    expected = {
        "attempt_count",
        "available_at",
        "worker_id",
        "claim_token",
        "claimed_at",
        "lease_expires_at",
        "finished_at",
        "last_error_code",
    }
    assert expected <= set(record_columns.keys())
    for constraint_name in (
        "ck_revision_retention_attempt_count",
        "ck_revision_retention_claim_shape",
        "ck_revision_retention_available_shape",
        "ck_revision_retention_finished_shape",
        "ck_revision_retention_error_shape",
    ):
        assert _constraint_sql(RevisionRetentionRecord.__table__, constraint_name)
    assert {
        "ix_revision_retention_cleanup_due",
        "ix_revision_retention_cleanup_lease",
    } <= {index.name for index in RevisionRetentionRecord.__table__.indexes}

    migration = MIGRATION.read_text(encoding="utf-8")
    assert 'revision: str = "0028"' in migration
    assert 'down_revision: Union[str, None] = "0027"' in migration
    for name in expected | {"lifecycle_status", "deleted_at", "delete_verified_at"}:
        assert f'"{name}"' in migration
    assert "DELETE FROM" not in migration


def test_cleanup_startup_is_default_off_and_enabled_dependencies_fail_closed():
    from app.config import Settings, validate_revision_cleanup_startup

    default = Settings(_env_file=None)
    assert default.revision_cleanup_enabled is False
    validate_revision_cleanup_startup(
        Settings(
            _env_file=None,
            revision_cleanup_enabled=False,
            revision_cleanup_batch_size=0,
            revision_cleanup_lease_seconds=0,
            revision_cleanup_max_attempts=0,
        )
    )
    with pytest.raises(RuntimeError, match="retention governance"):
        validate_revision_cleanup_startup(
            Settings(
                _env_file=None,
                revision_cleanup_enabled=True,
                revision_file_storage_enabled=True,
            )
        )
    with pytest.raises(RuntimeError, match="revision file storage"):
        validate_revision_cleanup_startup(
            Settings(
                _env_file=None,
                revision_cleanup_enabled=True,
                revision_retention_enabled=True,
            )
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("revision_cleanup_batch_size", 0, "batch size"),
        ("revision_cleanup_batch_size", 101, "batch size"),
        ("revision_cleanup_lease_seconds", 29, "lease"),
        ("revision_cleanup_lease_seconds", 3_601, "lease"),
        ("revision_cleanup_max_attempts", 0, "max attempts"),
        ("revision_cleanup_max_attempts", 101, "max attempts"),
    ],
)
def test_cleanup_enabled_limits_are_bounded(field, value, message):
    from app.config import Settings, validate_revision_cleanup_startup

    values = {
        "revision_cleanup_enabled": True,
        "revision_retention_enabled": True,
        "revision_file_storage_enabled": True,
        field: value,
    }
    with pytest.raises(RuntimeError, match=message):
        validate_revision_cleanup_startup(Settings(_env_file=None, **values))


NOW = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)


def _config(**overrides):
    from app.config import Settings

    values = {
        "revision_cleanup_enabled": True,
        "revision_retention_enabled": True,
        "revision_file_storage_enabled": True,
        "revision_cleanup_lease_seconds": 300,
        "revision_cleanup_max_attempts": 3,
        **overrides,
    }
    return Settings(_env_file=None, **values)


def _scope(*, status="queued", managed_snapshot=True, lifecycle_status="available"):
    from app.services.revision_cleanup import LockedCleanupScope

    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    old_revision_id = uuid.uuid4()
    replacement_revision_id = uuid.uuid4()
    revision_file_id = uuid.uuid4()
    record = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        document_id=document_id,
        document_revision_id=old_revision_id,
        replacement_revision_id=replacement_revision_id,
        revision_file_id=revision_file_id,
        status=status,
        cleanup_not_before=NOW - timedelta(minutes=1),
        attempt_count=0,
        available_at=NOW - timedelta(seconds=1) if status in {"queued", "failed"} else None,
        worker_id=None,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        finished_at=None,
        last_error_code="provider_delete_failed" if status == "failed" else None,
        block_code=None,
    )
    document = SimpleNamespace(
        id=document_id,
        library_id=library_id,
        current_revision_id=replacement_revision_id,
        deleted_at=None,
    )
    revision_file = SimpleNamespace(
        id=revision_file_id,
        library_id=library_id,
        document_id=document_id,
        document_revision_id=old_revision_id,
        lifecycle_status=lifecycle_status,
        deleted_at=None,
        delete_verified_at=None,
        managed_snapshot=managed_snapshot,
        storage_provider="local",
        endpoint_ref="primary",
        bucket=None,
        object_key=f"libraries/{library_id}/objects/{'a' * 64}.txt",
        object_version=None,
        etag=None,
        immutability_mode="content_hash",
    )
    return LockedCleanupScope(
        library=SimpleNamespace(
            id=library_id,
            deleted_at=None,
            lifecycle_mode="managed",
            revision_retention_enabled=True,
        ),
        record=record,
        document=document,
        old_revision=SimpleNamespace(
            id=old_revision_id,
            library_id=library_id,
            document_id=document_id,
            status="superseded",
        ),
        replacement_revision=SimpleNamespace(
            id=replacement_revision_id,
            library_id=library_id,
            document_id=document_id,
            status="ready",
        ),
        revision_file=revision_file,
    )


def _patch_scope(monkeypatch, scope, *, dependencies=False):
    from app.services import revision_cleanup

    async def lock(*args, **kwargs):
        return scope

    async def dependency_check(*args, **kwargs):
        return dependencies

    monkeypatch.setattr(revision_cleanup, "lock_cleanup_scope", lock)
    monkeypatch.setattr(revision_cleanup, "cleanup_has_dependencies", dependency_check)


def test_claim_establishes_token_lease_and_revision_file_fence(monkeypatch):
    from app.services.revision_cleanup import claim_revision_cleanup

    scope = _scope()
    _patch_scope(monkeypatch, scope)
    claim = asyncio.run(
        claim_revision_cleanup(
            object(),
            record_id=scope.record.id,
            worker_id="cleanup-1",
            at=NOW,
            config=_config(),
        )
    )
    assert claim is not None
    assert claim.claim_token == scope.record.claim_token
    assert claim.attempt_count == 1
    assert scope.record.status == "processing"
    assert scope.record.available_at is None
    assert scope.record.lease_expires_at == NOW + timedelta(seconds=300)
    assert scope.revision_file.lifecycle_status == "deleting"


def test_expired_claim_rotates_token_and_stale_claim_cannot_finish(monkeypatch):
    from app.services.revision_cleanup import (
        RevisionCleanupError,
        claim_revision_cleanup,
        complete_revision_cleanup,
    )

    scope = _scope(status="processing", lifecycle_status="deleting")
    old_token = uuid.uuid4()
    scope.record.attempt_count = 1
    scope.record.worker_id = "old-worker"
    scope.record.claim_token = old_token
    scope.record.claimed_at = NOW - timedelta(minutes=10)
    scope.record.lease_expires_at = NOW - timedelta(seconds=1)
    _patch_scope(monkeypatch, scope)
    new_claim = asyncio.run(
        claim_revision_cleanup(
            object(),
            record_id=scope.record.id,
            worker_id="new-worker",
            at=NOW,
            config=_config(),
        )
    )
    assert new_claim is not None
    assert new_claim.claim_token != old_token
    stale = SimpleNamespace(**{**new_claim.__dict__, "claim_token": old_token}) if hasattr(new_claim, "__dict__") else None
    if stale is None:
        from dataclasses import replace

        stale = replace(new_claim, claim_token=old_token)
    with pytest.raises(RevisionCleanupError, match="authoritative"):
        asyncio.run(
            complete_revision_cleanup(
                object(),
                claim=stale,
                at=NOW,
                config=_config(),
            )
        )


def test_dependency_recheck_blocks_claim_and_clears_queue_state(monkeypatch):
    from app.services.revision_cleanup import claim_revision_cleanup

    scope = _scope()
    _patch_scope(monkeypatch, scope, dependencies=True)
    claim = asyncio.run(
        claim_revision_cleanup(
            object(),
            record_id=scope.record.id,
            worker_id="cleanup-1",
            at=NOW,
            config=_config(),
        )
    )
    assert claim is None
    assert scope.record.status == "blocked"
    assert scope.record.block_code == "active_graph_dependency"
    assert scope.record.available_at is None
    assert scope.revision_file.lifecycle_status == "available"


def test_failed_delete_keeps_deleting_fence_and_schedules_bounded_retry(monkeypatch):
    from app.services.revision_cleanup import claim_revision_cleanup, fail_revision_cleanup

    scope = _scope()
    _patch_scope(monkeypatch, scope)
    config = _config()
    claim = asyncio.run(
        claim_revision_cleanup(
            object(),
            record_id=scope.record.id,
            worker_id="cleanup-1",
            at=NOW,
            config=config,
        )
    )
    monkeypatch.setattr("app.services.revision_cleanup.audit_log.record", AsyncMock())
    result = asyncio.run(
        fail_revision_cleanup(
            object(),
            claim=claim,
            error_code="provider_delete_failed",
            at=NOW,
            config=config,
        )
    )
    assert result.status == "failed"
    assert scope.record.last_error_code == "provider_delete_failed"
    assert scope.record.available_at > NOW
    assert scope.record.claim_token is None
    assert scope.revision_file.lifecycle_status == "deleting"


@pytest.mark.parametrize(
    ("managed_snapshot", "expected_lifecycle"),
    [(True, "deleted"), (False, "released")],
)
def test_completion_records_managed_delete_or_external_release(
    monkeypatch,
    managed_snapshot,
    expected_lifecycle,
):
    from app.services.revision_cleanup import claim_revision_cleanup, complete_revision_cleanup

    scope = _scope(managed_snapshot=managed_snapshot)
    _patch_scope(monkeypatch, scope)
    config = _config()
    claim = asyncio.run(
        claim_revision_cleanup(
            object(),
            record_id=scope.record.id,
            worker_id="cleanup-1",
            at=NOW,
            config=config,
        )
    )
    audit = AsyncMock()
    monkeypatch.setattr("app.services.revision_cleanup.audit_log.record", audit)
    result = asyncio.run(
        complete_revision_cleanup(
            object(),
            claim=claim,
            at=NOW,
            config=config,
        )
    )
    assert result.status == "cleaned"
    assert scope.record.finished_at == NOW
    assert scope.revision_file.lifecycle_status == expected_lifecycle
    assert scope.revision_file.deleted_at == NOW
    assert (scope.revision_file.delete_verified_at == NOW) is managed_snapshot
    assert audit.await_count == 1


def test_final_dependency_drift_records_deleted_bytes_but_blocks_governance(monkeypatch):
    from app.services.revision_cleanup import claim_revision_cleanup, complete_revision_cleanup

    scope = _scope()
    _patch_scope(monkeypatch, scope)
    config = _config()
    claim = asyncio.run(
        claim_revision_cleanup(
            object(),
            record_id=scope.record.id,
            worker_id="cleanup-1",
            at=NOW,
            config=config,
        )
    )
    _patch_scope(monkeypatch, scope, dependencies=True)
    result = asyncio.run(
        complete_revision_cleanup(
            object(),
            claim=claim,
            at=NOW,
            config=config,
        )
    )
    assert result.status == "blocked"
    assert result.code == "active_graph_dependency"
    assert scope.record.finished_at is None
    assert scope.revision_file.lifecycle_status == "deleted"
    assert scope.revision_file.delete_verified_at == NOW

    from app.services.revision_cleanup import reconcile_terminal_revision_file

    assert reconcile_terminal_revision_file(scope) is False
    scope.record.status = "eligible"
    assert reconcile_terminal_revision_file(scope) is True
    assert scope.record.status == "cleaned"
    assert scope.record.finished_at == NOW


class _DeleteAdapter:
    provider = "local"
    endpoint_ref = "primary"
    bucket = None

    def __init__(self, *, error=None, transaction_active=None):
        self.error = error
        self.transaction_active = transaction_active
        self.calls = []

    async def delete(self, object_key, object_version):
        if self.transaction_active is not None:
            assert self.transaction_active() is False
        self.calls.append((object_key, object_version))
        if self.error is not None:
            raise self.error


def test_managed_delete_uses_exact_locator_and_missing_object_is_success(monkeypatch):
    from app.services.object_storage_contracts import ObjectStorageError
    from app.services.revision_cleanup import claim_revision_cleanup
    from app.services.revision_cleanup_runner import execute_revision_cleanup_claim

    scope = _scope()
    _patch_scope(monkeypatch, scope)
    claim = asyncio.run(
        claim_revision_cleanup(
            object(),
            record_id=scope.record.id,
            worker_id="cleanup-1",
            at=NOW,
            config=_config(),
        )
    )
    adapter = _DeleteAdapter(
        error=ObjectStorageError("object_not_found", "already absent")
    )
    outcome = asyncio.run(
        execute_revision_cleanup_claim(
            claim,
            adapter_factory=lambda config: adapter,
            config=_config(),
        )
    )
    assert outcome == "deleted"
    assert adapter.calls == [(claim.locator.object_key, claim.locator.object_version)]


def test_external_reference_release_never_constructs_or_calls_adapter(monkeypatch):
    from app.services.revision_cleanup import claim_revision_cleanup
    from app.services.revision_cleanup_runner import execute_revision_cleanup_claim

    scope = _scope(managed_snapshot=False)
    _patch_scope(monkeypatch, scope)
    claim = asyncio.run(
        claim_revision_cleanup(
            object(),
            record_id=scope.record.id,
            worker_id="cleanup-1",
            at=NOW,
            config=_config(),
        )
    )

    def forbidden(_config):
        raise AssertionError("external reference must not construct an adapter")

    assert (
        asyncio.run(
            execute_revision_cleanup_claim(
                claim,
                adapter_factory=forbidden,
                config=_config(),
            )
        )
        == "released"
    )


def test_revision_file_read_rejects_every_non_available_lifecycle():
    from app.services.object_storage_contracts import ObjectStorageError
    from app.services.revision_files import revision_file_access_from_row

    for lifecycle in ("deleting", "deleted", "released"):
        row = _scope(lifecycle_status=lifecycle).revision_file
        row.file_name = "source.txt"
        row.content_type = "text/plain"
        row.size_bytes = 1
        row.sha256 = "a" * 64
        with pytest.raises(ObjectStorageError) as exc_info:
            revision_file_access_from_row(row)
        assert exc_info.value.code == "revision_file_unavailable"


def test_download_maps_retired_revision_file_to_generic_not_found(monkeypatch):
    from fastapi import HTTPException

    from app.api.documents import download_document_file
    from app.config import settings

    scope = _scope(lifecycle_status="deleted")
    scope.revision_file.file_name = "source.txt"
    scope.revision_file.content_type = "text/plain"
    scope.revision_file.size_bytes = 1
    scope.revision_file.sha256 = "a" * 64
    db = AsyncMock()
    db.get = AsyncMock(return_value=scope.document)
    monkeypatch.setattr(settings, "revision_file_storage_enabled", True)
    monkeypatch.setattr(
        "app.api.documents.current_revision_file",
        AsyncMock(return_value=scope.revision_file),
    )
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            download_document_file(
                scope.document.id,
                lib=SimpleNamespace(id=scope.library.id),
                db=db,
            )
        )
    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "stored document file is unavailable"
    assert db.rollback.await_count == 1


class _Scalars:
    def __init__(self, rows):
        self.rows = list(rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class _Result:
    def __init__(self, rows=(), first=None):
        self.rows = list(rows)
        self.first_value = first

    def scalars(self):
        return _Scalars(self.rows)

    def first(self):
        return self.first_value


def test_cleanup_scope_lock_order_starts_with_library_before_owned_rows():
    from app.models.document import Document
    from app.models.document_revision import DocumentRevision
    from app.models.document_revision_file import DocumentRevisionFile
    from app.models.library import Library
    from app.models.revision_retention import RevisionRetentionRecord
    from app.services.revision_cleanup import lock_cleanup_scope

    scope = _scope()
    results = [
        _Result(first=SimpleNamespace(library_id=scope.library.id)),
        _Result([scope.library]),
        _Result([scope.record]),
        _Result([scope.document]),
        _Result([scope.old_revision, scope.replacement_revision]),
        _Result([scope.revision_file]),
    ]

    class Db:
        def __init__(self):
            self.statements = []

        async def execute(self, statement):
            self.statements.append(statement)
            return results.pop(0)

    db = Db()
    assert asyncio.run(lock_cleanup_scope(db, scope.record.id)) is not None
    models = []
    for statement in db.statements:
        descriptions = statement.column_descriptions
        entity = descriptions[0].get("entity") if descriptions else None
        models.append(entity)
    assert models == [
        RevisionRetentionRecord,
        Library,
        RevisionRetentionRecord,
        Document,
        DocumentRevision,
        DocumentRevisionFile,
    ]
    assert "FOR UPDATE" not in str(db.statements[0]).upper()
    assert all("FOR UPDATE" in str(stmt).upper() for stmt in db.statements[1:])


def test_publication_fence_rejects_retiring_support_evidence():
    from app.models.graph_publication import GraphPublication
    from app.models.graph_publication_item import GraphPublicationItem
    from app.services.graph_publication_activation import (
        GraphPublicationActivationError,
        _reject_retiring_support_evidence,
    )

    evidence_id = uuid.uuid4()
    publication = GraphPublication(id=uuid.uuid4(), library_id=uuid.uuid4())
    item = GraphPublicationItem(support_evidence_ids=[str(evidence_id)])

    class Db:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Result(first=SimpleNamespace(id=uuid.uuid4()))

    db = Db()
    with pytest.raises(GraphPublicationActivationError) as exc_info:
        asyncio.run(_reject_retiring_support_evidence(db, publication, [item]))
    assert exc_info.value.code == "publication_evidence_retiring"
    for status in ("queued", "processing", "cleaned"):
        assert status in str(db.statement.compile().params.values())
    assert "document_revision_files" in str(db.statement).lower()
    assert "lifecycle_status" in str(db.statement).lower()


def test_runner_places_object_io_between_database_transactions(monkeypatch):
    from app.services import revision_cleanup_runner as runner
    from app.services.revision_cleanup import RevisionCleanupResult

    scope = _scope()
    scope.record.status = "processing"
    scope.record.attempt_count = 1
    scope.record.claim_token = uuid.uuid4()
    scope.record.worker_id = "cleanup-1"
    scope.record.claimed_at = NOW
    scope.record.lease_expires_at = NOW + timedelta(minutes=5)
    scope.revision_file.lifecycle_status = "deleting"
    from app.services.revision_cleanup import cleanup_claim_from_scope

    claim = cleanup_claim_from_scope(scope)
    transaction_depth = 0

    class Transaction:
        async def __aenter__(self):
            nonlocal transaction_depth
            transaction_depth += 1

        async def __aexit__(self, exc_type, exc, traceback):
            nonlocal transaction_depth
            transaction_depth -= 1

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        def begin(self):
            return Transaction()

        async def rollback(self):
            return None

    async def queued(*args, **kwargs):
        return (scope.record.id,)

    async def candidates(*args, **kwargs):
        return (scope.record.id,)

    async def claimed(*args, **kwargs):
        return claim

    async def completed(*args, **kwargs):
        return RevisionCleanupResult(scope.record.id, "cleaned", "deleted")

    monkeypatch.setattr(runner, "queue_eligible_revision_cleanups", queued)
    monkeypatch.setattr(runner, "cleanup_candidate_record_ids", candidates)
    monkeypatch.setattr(runner, "claim_revision_cleanup", claimed)
    monkeypatch.setattr(runner, "complete_revision_cleanup", completed)
    adapter = _DeleteAdapter(transaction_active=lambda: transaction_depth > 0)
    result = asyncio.run(
        runner.run_revision_cleanup_batch(
            lambda: Session(),
            worker_id="cleanup-1",
            at=NOW,
            adapter_factory=lambda config: adapter,
            config=_config(revision_cleanup_batch_size=1),
        )
    )
    assert transaction_depth == 0
    assert result.cleaned_record_ids == (scope.record.id,)
    assert result.failed_record_ids == ()


def test_cleanup_service_does_not_delete_evidence_or_revision_rows():
    from app.services import revision_cleanup

    source = inspect.getsource(revision_cleanup)
    assert ".delete(" not in source
    for model_name in ("EvidenceUnit", "EntityMention", "RelationEvidence", "Chunk"):
        assert model_name not in source
