from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr
from sqlalchemy.dialects import postgresql

from app.models.graph_extraction_job import GraphExtractionJob
from app.services.graph_extraction_provider import (
    GraphExtractionProviderError,
    ProviderResponse,
)
from app.services.graph_extraction_worker import (
    GraphExtractionProcessResult,
    GraphExtractionWorkerError,
    PreparedGraphExtractionUnit,
    StaleUnitRecoveryResult,
    _configured_provider,
    claim_graph_extraction_unit,
    lock_live_graph_extraction_claim,
    mark_claimed_unit_terminal,
    process_graph_extraction_unit,
    recover_stale_graph_extraction_units,
    renew_graph_extraction_unit_lease,
    run_graph_extraction_worker,
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

    def all(self):
        return list(self.rows)


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

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False


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
    job = _job()
    db = FakeDB(
        results=[
            _Result([JOB_ID]),
            _Result(rowcount=2),
            _Result(rowcount=1),
            _Result(rowcount=1),
            _Result([("failed", 1), ("queued", 1)]),
        ]
        ,
        objects={(GraphExtractionJob, JOB_ID): job},
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
    attempt_sql = str(db.statements[1]).lower()
    failed_sql = str(db.statements[2]).lower()
    requeue_sql = str(db.statements[3]).lower()
    assert "update extraction_raw_output_attempts" in attempt_sql
    assert "update graph_extraction_units" in failed_sql
    assert "update graph_extraction_units" in requeue_sql
    assert "model_attempt_count" in failed_sql
    for statement in db.statements[2:4]:
        values = statement.compile().params.values()
        assert None in values
    assert "failed" in db.statements[2].compile().params.values()
    assert "queued" in db.statements[3].compile().params.values()
    assert job.counts["failed"] == 1
    assert job.counts["queued"] == 1
    assert job.status == "processing"


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


def _prepared():
    return PreparedGraphExtractionUnit(
        unit_id=UNIT_ID,
        job_id=JOB_ID,
        context_snapshot_id=uuid.uuid4(),
        claim_token=CLAIM_TOKEN,
        messages=[{"role": "user", "content": "context"}],
        model_config_snapshot={
            "provider": "deepseek",
            "base_url": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-pro",
            "timeout_seconds": 10.0,
        },
        model_config_hash="a" * 64,
    )


def _provider_response(content: str) -> ProviderResponse:
    return ProviderResponse(
        content=content,
        provider_request_id="mock-request",
        raw_response=content,
        request_payload_hash="b" * 64,
        input_token_count=1,
        output_token_count=2,
        latency_ms=3,
        finish_reason="stop",
    )


class _SessionFactory:
    def __init__(self):
        self.sessions = []

    def __call__(self):
        session = FakeDB()
        self.sessions.append(session)
        return session


def test_configured_provider_builds_deepseek_adapter_without_http(monkeypatch):
    from app.services import graph_extraction_worker as worker

    monkeypatch.setattr(
        worker.settings,
        "graph_extraction_api_key",
        SecretStr("DEEPSEEK-TEST-KEY"),
    )
    with patch.object(worker, "OpenAICompatibleGraphExtractor") as adapter:
        configured = _configured_provider(_prepared())

    assert configured is adapter.return_value
    adapter.assert_called_once_with(
        base_url="https://api.deepseek.com/v1",
        model="deepseek-v4-pro",
        api_key="DEEPSEEK-TEST-KEY",
        timeout_seconds=10.0,
    )


def test_configured_provider_rejects_retired_dashscope_snapshot_before_http(
    monkeypatch,
):
    from app.services import graph_extraction_worker as worker

    prepared = _prepared()
    prepared.model_config_snapshot.update(
        {
            "provider": "dashscope",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus",
        }
    )
    monkeypatch.setattr(
        worker.settings,
        "graph_extraction_api_key",
        SecretStr("DEEPSEEK-TEST-KEY"),
    )
    with (
        patch.object(worker, "OpenAICompatibleGraphExtractor") as adapter,
        pytest.raises(GraphExtractionWorkerError) as exc,
    ):
        _configured_provider(prepared)

    assert exc.value.code == "provider_config_retired"
    adapter.assert_not_called()


def test_retired_dashscope_job_is_cancelled_without_provider_http(monkeypatch):
    from app.services import graph_extraction_worker as worker

    prepared = _prepared()
    prepared.model_config_snapshot.update(
        {
            "provider": "dashscope",
            "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "model": "qwen-plus",
        }
    )
    sessions = _SessionFactory()
    attempt = SimpleNamespace(id=uuid.uuid4())
    monkeypatch.setattr(
        worker.settings,
        "graph_extraction_api_key",
        SecretStr("DEEPSEEK-TEST-KEY"),
    )
    with (
        patch(
            "app.services.graph_extraction_worker._prepare_graph_extraction_unit",
            new=AsyncMock(return_value=prepared),
        ),
        patch(
            "app.services.graph_extraction_worker._preflight_provider_call",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_extraction_worker.create_pending_attempt",
            new=AsyncMock(return_value=attempt),
        ),
        patch(
            "app.services.graph_extraction_worker._finish_claim_after_error",
            new=AsyncMock(return_value=True),
        ) as finish,
        patch.object(
            worker.OpenAICompatibleGraphExtractor,
            "extract",
            new=AsyncMock(),
        ) as provider_http,
    ):
        result = asyncio.run(
            process_graph_extraction_unit(
                sessions,
                unit_id=UNIT_ID,
                claim_token=CLAIM_TOKEN,
            )
        )

    assert result == GraphExtractionProcessResult(
        "cancelled",
        "provider_config_retired",
    )
    finish.assert_awaited_once()
    assert finish.await_args.kwargs["status"] == "cancelled"
    assert finish.await_args.kwargs["error_code"] == "provider_config_retired"
    assert finish.await_args.kwargs["abandon_pending"] is True
    provider_http.assert_not_awaited()


def test_orchestration_calls_provider_outside_transactions_and_persists_valid_payload():
    prepared = _prepared()
    attempt = SimpleNamespace(id=uuid.uuid4())
    provider = AsyncMock()
    provider.extract.return_value = _provider_response(
        '{"entities":[],"relations":[]}'
    )
    sessions = _SessionFactory()

    with (
        patch(
            "app.services.graph_extraction_worker._prepare_graph_extraction_unit",
            new=AsyncMock(return_value=prepared),
        ),
        patch(
            "app.services.graph_extraction_worker._preflight_provider_call",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_extraction_worker.create_pending_attempt",
            new=AsyncMock(return_value=attempt),
        ) as create_attempt,
        patch(
            "app.services.graph_extraction_worker.finalize_attempt",
            new=AsyncMock(return_value=True),
        ) as finalize,
        patch(
            "app.services.graph_extraction_worker._persist_candidate_result",
            new=AsyncMock(return_value=True),
        ) as persist,
    ):
        result = asyncio.run(
            process_graph_extraction_unit(
                sessions,
                unit_id=UNIT_ID,
                claim_token=CLAIM_TOKEN,
                provider=provider,
                lease_seconds=180,
                renew_seconds=30,
                max_attempts=3,
            )
        )

    assert result.outcome == "succeeded"
    assert result.ready_for_materialization is True
    provider.extract.assert_awaited_once_with(prepared.messages)
    create_attempt.assert_awaited_once()
    completion = finalize.await_args.kwargs["completion"]
    assert completion.request_status == "succeeded"
    assert completion.parse_status == "valid"
    persist.assert_awaited_once()


@pytest.mark.parametrize("category", ["timeout", "network_error", "http_error"])
def test_provider_failure_finalizes_attempt_and_fails_unit_without_candidate_write(
    category,
):
    prepared = _prepared()
    attempt = SimpleNamespace(id=uuid.uuid4())
    provider = AsyncMock()
    provider.extract.side_effect = GraphExtractionProviderError(
        category,
        "provider failed",
        latency_ms=50,
    )
    sessions = _SessionFactory()

    with (
        patch(
            "app.services.graph_extraction_worker._prepare_graph_extraction_unit",
            new=AsyncMock(return_value=prepared),
        ),
        patch(
            "app.services.graph_extraction_worker._preflight_provider_call",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_extraction_worker.create_pending_attempt",
            new=AsyncMock(return_value=attempt),
        ),
        patch(
            "app.services.graph_extraction_worker.finalize_attempt",
            new=AsyncMock(return_value=True),
        ) as finalize,
        patch(
            "app.services.graph_extraction_worker._finish_claim_after_error",
            new=AsyncMock(return_value=True),
        ) as finish,
        patch(
            "app.services.graph_extraction_worker._persist_candidate_result",
            new=AsyncMock(),
        ) as persist,
    ):
        result = asyncio.run(
            process_graph_extraction_unit(
                sessions,
                unit_id=UNIT_ID,
                claim_token=CLAIM_TOKEN,
                provider=provider,
                max_attempts=3,
            )
        )

    assert result.outcome == "failed"
    completion = finalize.await_args.kwargs["completion"]
    assert completion.request_status == category
    finish.assert_awaited_once()
    persist.assert_not_awaited()


def test_invalid_json_is_a_protocol_failure_but_candidate_review_is_unit_success():
    prepared = _prepared()
    attempt = SimpleNamespace(id=uuid.uuid4())
    provider = AsyncMock()
    provider.extract.return_value = _provider_response("not-json")
    sessions = _SessionFactory()

    with (
        patch(
            "app.services.graph_extraction_worker._prepare_graph_extraction_unit",
            new=AsyncMock(return_value=prepared),
        ),
        patch(
            "app.services.graph_extraction_worker._preflight_provider_call",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_extraction_worker.create_pending_attempt",
            new=AsyncMock(return_value=attempt),
        ),
        patch(
            "app.services.graph_extraction_worker.finalize_attempt",
            new=AsyncMock(return_value=True),
        ) as finalize,
        patch(
            "app.services.graph_extraction_worker._finish_claim_after_error",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "app.services.graph_extraction_worker._persist_candidate_result",
            new=AsyncMock(),
        ) as persist,
    ):
        result = asyncio.run(
            process_graph_extraction_unit(
                sessions,
                unit_id=UNIT_ID,
                claim_token=CLAIM_TOKEN,
                provider=provider,
                max_attempts=3,
            )
        )

    assert result.outcome == "failed"
    completion = finalize.await_args.kwargs["completion"]
    assert completion.request_status == "succeeded"
    assert completion.parse_status == "invalid_json"
    persist.assert_not_awaited()


def test_lost_lease_discards_provider_output_before_attempt_or_candidate_finalize():
    prepared = _prepared()
    attempt = SimpleNamespace(id=uuid.uuid4())
    sessions = _SessionFactory()
    with (
        patch(
            "app.services.graph_extraction_worker._prepare_graph_extraction_unit",
            new=AsyncMock(return_value=prepared),
        ),
        patch(
            "app.services.graph_extraction_worker._preflight_provider_call",
            new=AsyncMock(),
        ),
        patch(
            "app.services.graph_extraction_worker.create_pending_attempt",
            new=AsyncMock(return_value=attempt),
        ),
        patch(
            "app.services.graph_extraction_worker._call_provider_with_lease_renewal",
            new=AsyncMock(
                return_value=(
                    _provider_response('{"entities":[],"relations":[]}'),
                    True,
                )
            ),
        ),
        patch(
            "app.services.graph_extraction_worker.finalize_attempt",
            new=AsyncMock(),
        ) as finalize,
        patch(
            "app.services.graph_extraction_worker._persist_candidate_result",
            new=AsyncMock(),
        ) as persist,
    ):
        result = asyncio.run(
            process_graph_extraction_unit(
                sessions,
                unit_id=UNIT_ID,
                claim_token=CLAIM_TOKEN,
                provider=AsyncMock(),
            )
        )

    assert result.outcome == "lost_lease"
    finalize.assert_not_awaited()
    persist.assert_not_awaited()


def test_watch_entrypoint_delegates_to_active_worker_with_m5_heartbeat(monkeypatch):
    from app.services import graph_extraction_worker as worker_service
    from app.services import heartbeat
    from app.workers import graph_extractor

    monkeypatch.setattr(graph_extractor.settings, "graph_extraction_enabled", True)
    monkeypatch.setattr(graph_extractor, "validate_graph_extraction_startup", lambda _: None)
    monkeypatch.setattr(heartbeat, "make_instance_id", lambda: "worker-instance")
    monkeypatch.setattr(heartbeat, "heartbeat_loop", AsyncMock())
    monkeypatch.setattr(heartbeat, "beat", AsyncMock(return_value=True))
    active = AsyncMock()
    monkeypatch.setattr(worker_service, "run_graph_extraction_worker", active)

    asyncio.run(graph_extractor.run(watch=True))

    active.assert_awaited_once()
    metadata = active.await_args.kwargs["metadata"]
    assert metadata == {
        "watch": True,
        "mode": "active_worker",
        "milestone": "M5",
    }


def test_worker_materializes_only_when_processing_marks_job_ready():
    unit = _unit()
    unit.claim_token = CLAIM_TOKEN
    sessions = _SessionFactory()
    metadata = {}
    with (
        patch(
            "app.services.graph_extraction_worker.recover_stale_graph_extraction_units",
            new=AsyncMock(return_value=StaleUnitRecoveryResult(0, 0, 0)),
        ),
        patch(
            "app.services.graph_extraction_worker.claim_graph_extraction_unit",
            new=AsyncMock(side_effect=[unit, None]),
        ),
        patch(
            "app.services.graph_extraction_worker.process_graph_extraction_unit",
            new=AsyncMock(
                return_value=GraphExtractionProcessResult(
                    "succeeded",
                    ready_for_materialization=True,
                )
            ),
        ),
        patch(
            "app.services.graph_extraction_materializer.materialize_graph_extraction_job",
            new=AsyncMock(
                return_value=SimpleNamespace(already_materialized=False)
            ),
        ) as materialize,
    ):
        asyncio.run(
            run_graph_extraction_worker(
                watch=False,
                metadata=metadata,
                session_factory=sessions,
            )
        )

    materialize.assert_awaited_once_with(sessions, job_id=JOB_ID)
    assert metadata["claimed"] == 1
    assert metadata["succeeded"] == 1
