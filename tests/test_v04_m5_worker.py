from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy.dialects import postgresql

from app.models.graph_extraction_job import GraphExtractionJob
from app.services.graph_extraction_worker import (
    claim_graph_extraction_unit,
    lock_live_graph_extraction_claim,
    mark_claimed_unit_terminal,
    recover_stale_graph_extraction_units,
    renew_graph_extraction_unit_lease,
)


NOW = datetime(2026, 7, 14, 8, 0, 0, tzinfo=timezone.utc)
UNIT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
CLAIM_TOKEN = uuid.UUID("30000000-0000-0000-0000-000000000001")


class _Begin:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Result:
    def __init__(self, rows=(), *, rowcount=0):
        self.rows = list(rows)
        self.rowcount = rowcount

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None


class FakeDB:
    def __init__(self, *, results=(), objects=None):
        self.results = list(results)
        self.objects = objects or {}
        self.statements = []
        self.flush_count = 0

    def begin(self):
        return _Begin()

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.results, "unexpected database statement"
        return self.results.pop(0)

    async def get(self, model, object_id, **_kwargs):
        return self.objects.get((model, object_id))

    async def flush(self):
        self.flush_count += 1


def _unit():
    return SimpleNamespace(
        id=UNIT_ID,
        job_id=JOB_ID,
        status="queued",
        model_attempt_count=0,
        retryable=False,
        worker_id=None,
        claim_token=None,
        claimed_at=None,
        lease_expires_at=None,
        error_code=None,
        error_message=None,
        started_at=None,
    )


def _job():
    return SimpleNamespace(
        id=JOB_ID,
        status="queued",
        current_stage="preparing",
        started_at=None,
        error_code=None,
        error_message=None,
    )


def test_claim_uses_skip_locked_and_sets_random_fenced_lease():
    unit = _unit()
    job = _job()
    db = FakeDB(
        results=[_Result([unit])],
        objects={(GraphExtractionJob, JOB_ID): job},
    )

    claimed = asyncio.run(
        claim_graph_extraction_unit(
            db,
            worker_id="worker-1",
            lease_seconds=180,
            max_attempts=3,
            now=NOW,
        )
    )

    claim_sql = str(db.statements[0].compile(dialect=postgresql.dialect())).upper()
    assert "FOR UPDATE" in claim_sql
    assert "SKIP LOCKED" in claim_sql
    assert claimed is unit
    assert unit.status == "processing"
    assert unit.worker_id == "worker-1"
    assert isinstance(unit.claim_token, uuid.UUID)
    assert unit.claim_token != CLAIM_TOKEN
    assert unit.claimed_at == NOW
    assert unit.lease_expires_at == NOW + timedelta(seconds=180)
    assert job.status == "processing"
    assert job.current_stage == "building_context"
    assert db.flush_count == 1


def test_claim_returns_none_without_mutation_when_queue_is_empty():
    db = FakeDB(results=[_Result()])
    assert (
        asyncio.run(
            claim_graph_extraction_unit(
                db,
                worker_id="worker-1",
                lease_seconds=180,
                max_attempts=3,
                now=NOW,
            )
        )
        is None
    )
    assert db.flush_count == 0


def test_lease_renewal_is_fenced_and_refuses_lost_or_expired_claims():
    success_db = FakeDB(results=[_Result(rowcount=1)])
    renewed = asyncio.run(
        renew_graph_extraction_unit_lease(
            success_db,
            unit_id=UNIT_ID,
            claim_token=CLAIM_TOKEN,
            lease_seconds=180,
            now=NOW,
        )
    )
    assert renewed is True
    sql = str(success_db.statements[0]).lower()
    assert "claim_token" in sql
    assert "lease_expires_at" in sql
    assert "status" in sql

    lost_db = FakeDB(results=[_Result(rowcount=0)])
    assert (
        asyncio.run(
            renew_graph_extraction_unit_lease(
                lost_db,
                unit_id=UNIT_ID,
                claim_token=CLAIM_TOKEN,
                lease_seconds=180,
                now=NOW,
            )
        )
        is False
    )


def test_stale_recovery_abandons_attempts_then_fails_or_requeues_units():
    db = FakeDB(
        results=[
            _Result(rowcount=2),
            _Result(rowcount=1),
            _Result(rowcount=1),
        ]
    )

    result = asyncio.run(
        recover_stale_graph_extraction_units(
            db,
            max_attempts=3,
            now=NOW,
        )
    )

    assert result.abandoned_attempt_count == 2
    assert result.failed_unit_count == 1
    assert result.requeued_unit_count == 1
    attempt_sql = str(db.statements[0]).lower()
    failed_sql = str(db.statements[1]).lower()
    requeue_sql = str(db.statements[2]).lower()
    assert "update extraction_raw_output_attempts" in attempt_sql
    assert "update graph_extraction_units" in failed_sql
    assert "update graph_extraction_units" in requeue_sql
    assert "model_attempt_count" in failed_sql
    for statement in db.statements[1:]:
        values = statement.compile().params.values()
        assert None in values
    assert "failed" in db.statements[1].compile().params.values()
    assert "queued" in db.statements[2].compile().params.values()


def test_live_claim_lock_and_terminal_write_both_require_the_same_token():
    unit = _unit()
    unit.status = "processing"
    unit.claim_token = CLAIM_TOKEN
    unit.lease_expires_at = NOW + timedelta(seconds=180)
    lock_db = FakeDB(results=[_Result([unit])])

    locked = asyncio.run(
        lock_live_graph_extraction_claim(
            lock_db,
            unit_id=UNIT_ID,
            claim_token=CLAIM_TOKEN,
            now=NOW,
        )
    )
    assert locked is unit
    lock_sql = str(lock_db.statements[0]).lower()
    assert "for update" in lock_sql
    assert "claim_token" in lock_sql
    assert "lease_expires_at" in lock_sql

    terminal_db = FakeDB(results=[_Result(rowcount=1)])
    assert asyncio.run(
        mark_claimed_unit_terminal(
            terminal_db,
            unit_id=UNIT_ID,
            claim_token=CLAIM_TOKEN,
            status="succeeded",
            now=NOW,
        )
    )
    terminal_values = terminal_db.statements[0].compile().params.values()
    assert "succeeded" in terminal_values
    assert CLAIM_TOKEN in terminal_values
    assert None in terminal_values

    lost_db = FakeDB(results=[_Result(rowcount=0)])
    assert not asyncio.run(
        mark_claimed_unit_terminal(
            lost_db,
            unit_id=UNIT_ID,
            claim_token=CLAIM_TOKEN,
            status="failed",
            retryable=True,
            error_code="provider_timeout",
            now=NOW,
        )
    )
