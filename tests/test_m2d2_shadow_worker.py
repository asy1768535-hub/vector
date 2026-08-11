from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.services import graph_claim_shadow_worker as shadow_worker
from app.services.graph_extraction_worker import (
    GraphExtractionProcessResult,
    PreparedGraphExtractionUnit,
)
from app.schemas.shadow_extraction import (
    ShadowProviderResponseV1,
    ShadowValidationIssueV1,
)
from app.services.shadow_extraction_provider import ShadowProviderRunError
from app.services.graph_extraction_provider import GraphExtractionProviderError
from app.config import settings
from tests.test_raw_claim import _claim


JOB_ID = UUID("40000000-0000-0000-0000-000000000001")
UNIT_ID = UUID("50000000-0000-0000-0000-000000000001")


def _prepared(*, shadow_enabled: bool) -> PreparedGraphExtractionUnit:
    return PreparedGraphExtractionUnit(
        unit_id=UNIT_ID,
        job_id=JOB_ID,
        context_snapshot_id=UUID("60000000-0000-0000-0000-000000000001"),
        claim_token=UUID("70000000-0000-0000-0000-000000000001"),
        messages=[{"role": "system", "content": "canonical"}, {"role": "user", "content": "canonical"}],
        model_config_snapshot={},
        model_config_hash="a" * 64,
        library_id=UUID("90000000-0000-0000-0000-000000000001"),
        shadow_enabled=shadow_enabled,
    )


class _AsyncSessionFactory:
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _model, _library_id):
            return SimpleNamespace(
                claim_graph_shadow_policy="enabled",
                graph_extraction_enabled=True,
                external_llm_enabled=True,
            )

    def __call__(self):
        return self._Session()


class _LibrarySessionFactory:
    def __init__(self, library):
        self.library = library

    class _Session:
        def __init__(self, library):
            self.library = library

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, _model, _library_id):
            return self.library

    def __call__(self):
        return self._Session(self.library)


class _StatisticsSession:
    def __init__(self):
        self.job = SimpleNamespace(statistics={})

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def begin(self):
        return self

    async def get(self, _model, _job_id, **_kwargs):
        return self.job

    async def flush(self):
        return None


class _StatisticsSessionFactory:
    def __init__(self):
        self.session = _StatisticsSession()

    def __call__(self):
        return self.session


@pytest.mark.asyncio
async def test_shadow_provider_receives_independent_messages_and_explicit_surface_claim(monkeypatch):
    request = SimpleNamespace(messages=({"role": "user", "content": "shadow"},))
    provenance = object()
    evidence = {"e0": object()}
    response = ShadowProviderResponseV1(
        content='{"claims": []}',
        finish_reason="stop",
        input_token_count=4,
        output_token_count=2,
        latency_ms=7,
    )
    provider = SimpleNamespace(extract=AsyncMock(return_value=response))
    run_result = SimpleNamespace(
        claims=(),
        telemetry=SimpleNamespace(
            latency_ms=7,
            input_token_count=4,
            output_token_count=2,
        ),
    )
    persist = AsyncMock(
        return_value=shadow_worker.ShadowRunSummary(status="succeeded")
    )
    stats = AsyncMock()
    monkeypatch.setattr(
        shadow_worker,
        "_build_request",
        AsyncMock(return_value=(request, evidence, provenance)),
    )
    monkeypatch.setattr(shadow_worker, "run_shadow_extraction", AsyncMock(return_value=run_result))
    monkeypatch.setattr(shadow_worker, "_persist_claims", persist)
    monkeypatch.setattr(shadow_worker, "record_shadow_statistics", stats)

    result = await shadow_worker.run_shadow_after_canonical(
        _AsyncSessionFactory(), prepared=_prepared(shadow_enabled=True), provider=provider
    )

    assert result.status == "succeeded"
    shadow_worker.run_shadow_extraction.assert_awaited_once()
    assert persist.await_args.args[1] == ()
    stats.assert_awaited_once()
    assert provider.extract.await_count == 0


@pytest.mark.asyncio
async def test_shadow_call_adapter_passes_request_messages_to_provider(monkeypatch):
    request = SimpleNamespace(messages=({"role": "user", "content": "shadow"},))
    provider = SimpleNamespace(
        extract=AsyncMock(
            return_value=SimpleNamespace(
                content='{"claims": []}',
                finish_reason="stop",
                input_token_count=1,
                output_token_count=1,
                latency_ms=1,
            )
        )
    )
    captured = {}

    async def fake_run(request_value, callable_value, **_kwargs):
        captured["messages"] = request_value.messages
        await callable_value(request_value)
        return SimpleNamespace(
            claims=(),
            telemetry=SimpleNamespace(
                latency_ms=1,
                input_token_count=1,
                output_token_count=1,
            ),
        )

    monkeypatch.setattr(
        shadow_worker,
        "_build_request",
        AsyncMock(return_value=(request, {}, object())),
    )
    monkeypatch.setattr(shadow_worker, "run_shadow_extraction", fake_run)
    monkeypatch.setattr(
        shadow_worker,
        "_persist_claims",
        AsyncMock(return_value=shadow_worker.ShadowRunSummary(status="succeeded")),
    )
    monkeypatch.setattr(shadow_worker, "record_shadow_statistics", AsyncMock())

    await shadow_worker.run_shadow_after_canonical(
        _AsyncSessionFactory(), prepared=_prepared(shadow_enabled=True), provider=provider
    )

    assert captured["messages"] == request.messages
    provider.extract.assert_awaited_once_with(list(request.messages))


@pytest.mark.asyncio
async def test_shadow_call_applies_output_budget_only_to_provider_clone(monkeypatch):
    request = SimpleNamespace(
        messages=({"role": "user", "content": "shadow"},),
        limits=SimpleNamespace(max_output_tokens=321),
    )

    class BudgetProvider:
        def __init__(self, *, budget=999):
            self.budgets = []
            self.calls = 0
            self.canonical_budget = budget

        def with_output_budget(self, value):
            self.budgets.append(value)
            self.clone = BudgetProvider(budget=value)
            return self.clone

        async def extract(self, _messages):
            self.calls += 1
            return SimpleNamespace(
                content='{"claims": []}', finish_reason="stop",
                input_token_count=1, output_token_count=1, latency_ms=1,
            )

    provider = BudgetProvider()

    async def fake_run(request_value, callback, **_kwargs):
        await callback(request_value)
        return SimpleNamespace(
            claims=(),
            telemetry=SimpleNamespace(latency_ms=1, input_token_count=1, output_token_count=1),
        )

    monkeypatch.setattr(
        shadow_worker, "_build_request",
        AsyncMock(return_value=(request, {}, object())),
    )
    monkeypatch.setattr(shadow_worker, "run_shadow_extraction", fake_run)
    monkeypatch.setattr(
        shadow_worker, "_persist_claims",
        AsyncMock(return_value=shadow_worker.ShadowRunSummary(status="succeeded")),
    )
    monkeypatch.setattr(shadow_worker, "record_shadow_statistics", AsyncMock())

    await shadow_worker.run_shadow_after_canonical(
        _AsyncSessionFactory(), prepared=_prepared(shadow_enabled=True), provider=provider
    )

    assert provider.budgets == [321]
    assert provider.calls == 0
    assert provider.canonical_budget == 999
    assert provider.clone.calls == 1


@pytest.mark.asyncio
async def test_dispatch_recheck_skips_when_current_library_is_disabled(monkeypatch):
    request = SimpleNamespace(messages=(), limits=SimpleNamespace(max_output_tokens=321))
    provider = SimpleNamespace(extract=AsyncMock())
    stats = AsyncMock()
    build_request = AsyncMock(return_value=(request, {}, object()))
    monkeypatch.setattr(
        shadow_worker, "_build_request", build_request,
    )
    monkeypatch.setattr(shadow_worker, "record_shadow_statistics", stats)
    prepared = SimpleNamespace(
        job_id=JOB_ID,
        library_id=UUID("90000000-0000-0000-0000-000000000001"),
    )
    library = SimpleNamespace(
        claim_graph_shadow_policy="disabled",
        graph_extraction_enabled=True,
        external_llm_enabled=True,
    )

    result = await shadow_worker.run_shadow_after_canonical(
        _LibrarySessionFactory(library), prepared=prepared, provider=provider
    )

    assert result.status == "skipped"
    assert result.reason == "shadow_disabled_at_dispatch"
    build_request.assert_not_awaited()
    provider.extract.assert_not_awaited()
    stats.assert_awaited_once()
    assert stats.await_args.kwargs["summary"].reason == "shadow_disabled_at_dispatch"


@pytest.mark.asyncio
async def test_dispatch_missing_library_scope_skips_before_request_or_provider(monkeypatch):
    request = SimpleNamespace(messages=(), limits=SimpleNamespace(max_output_tokens=321))
    provider = SimpleNamespace(extract=AsyncMock())
    build_request = AsyncMock(return_value=(request, {}, object()))
    stats = AsyncMock()
    monkeypatch.setattr(shadow_worker, "_build_request", build_request)
    monkeypatch.setattr(shadow_worker, "record_shadow_statistics", stats)
    prepared = SimpleNamespace(job_id=JOB_ID, library_id=None)

    result = await shadow_worker.run_shadow_after_canonical(
        _AsyncSessionFactory(), prepared=prepared, provider=provider
    )

    assert result.status == "skipped"
    assert result.reason == "shadow_library_scope_missing_at_dispatch"
    build_request.assert_not_awaited()
    provider.extract.assert_not_awaited()
    stats.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("global_enabled", "policy", "graph_enabled", "llm_enabled", "expected"),
    [
        (False, "inherit", True, True, False),
        (False, "enabled", True, True, True),
        (True, "inherit", True, True, True),
        (True, "disabled", True, True, False),
        (True, "enabled", False, True, False),
        (True, "enabled", True, False, False),
    ],
)
async def test_dispatch_recheck_resolves_current_library_matrix(
    monkeypatch, global_enabled, policy, graph_enabled, llm_enabled, expected
):
    original = settings.graph_claim_shadow_enabled
    settings.graph_claim_shadow_enabled = global_enabled
    try:
        library = SimpleNamespace(
            claim_graph_shadow_policy=policy,
            graph_extraction_enabled=graph_enabled,
            external_llm_enabled=llm_enabled,
        )
        prepared = SimpleNamespace(
            library_id=UUID("91000000-0000-0000-0000-000000000001")
        )
        assert await shadow_worker._shadow_enabled_at_dispatch(
            _LibrarySessionFactory(library), prepared
        ) is expected
    finally:
        settings.graph_claim_shadow_enabled = original


@pytest.mark.asyncio
async def test_build_request_rejects_snapshot_from_another_job():
    library_id = UUID("92000000-0000-0000-0000-000000000001")
    snapshot = SimpleNamespace(
        purged_at=None,
        job_id=UUID("93000000-0000-0000-0000-000000000001"),
        extraction_unit_id=UNIT_ID,
    )
    unit = SimpleNamespace(
        job_id=JOB_ID,
        library_id=library_id,
        document_revision_id=UUID("94000000-0000-0000-0000-000000000001"),
    )
    job = SimpleNamespace(
        id=JOB_ID,
        library_id=library_id,
        document_id=UUID("95000000-0000-0000-0000-000000000001"),
        document_revision_id=unit.document_revision_id,
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def get(self, model, _key):
            return {
                "ExtractionContextSnapshot": snapshot,
                "GraphExtractionUnit": unit,
                "GraphExtractionJob": job,
            }[model.__name__]

    with pytest.raises(shadow_worker.ShadowAdapterError) as error:
        await shadow_worker._build_request(
            lambda: Session(),
            prepared=SimpleNamespace(
                context_snapshot_id=UUID("96000000-0000-0000-0000-000000000001"),
                unit_id=UNIT_ID,
                job_id=JOB_ID,
            ),
        )
    assert error.value.reason == "worker_scope_mismatch"


def test_shadow_limits_use_frozen_job_context_and_output_budget():
    limits = shadow_worker._shadow_limits_from_job(
        SimpleNamespace(
            model_config_snapshot={
                "context_window_tokens": 32_768,
                "max_output_tokens": 8_000,
            }
        )
    )

    assert limits.max_request_tokens == 24_512
    assert limits.max_output_tokens == 8_000
    assert limits.max_request_tokens + limits.max_output_tokens + 256 == 32_768

    defaulted = shadow_worker._shadow_limits_from_job(
        SimpleNamespace(
            model_config_snapshot={
                "context_window_tokens": 32_768,
                "max_output_tokens": None,
            }
        )
    )
    assert defaulted.max_request_tokens == 24_512
    assert defaulted.max_output_tokens == 8_000
    assert defaulted.max_request_tokens + defaulted.max_output_tokens + 256 == 32_768

    clamped = shadow_worker._shadow_limits_from_job(
        SimpleNamespace(
            model_config_snapshot={
                "context_window_tokens": 65_536,
                "max_output_tokens": 65_536,
            }
        )
    )
    assert clamped.max_request_tokens == 256
    assert clamped.max_output_tokens == 32_256
    assert clamped.max_request_tokens + clamped.max_output_tokens + 256 == 32_768

    with pytest.raises(shadow_worker.ShadowAdapterError) as error:
        shadow_worker._shadow_limits_from_job(
            SimpleNamespace(
                model_config_snapshot={
                    "context_window_tokens": 8_192,
                    "max_output_tokens": None,
                }
            )
        )
    assert error.value.reason == "shadow_model_budget_invalid"

    with pytest.raises(shadow_worker.ShadowAdapterError) as error:
        shadow_worker._shadow_limits_from_job(
            SimpleNamespace(model_config_snapshot={"max_output_tokens": 8_000})
        )
    assert error.value.reason == "shadow_model_budget_invalid"


@pytest.mark.parametrize("invalid_output", [True, 0, -1, "sk-secret-output-budget"])
def test_shadow_limits_reject_invalid_output_budget_without_leaking(invalid_output):
    with pytest.raises(shadow_worker.ShadowAdapterError) as error:
        shadow_worker._shadow_limits_from_job(
            SimpleNamespace(
                model_config_snapshot={
                    "context_window_tokens": 32_768,
                    "max_output_tokens": invalid_output,
                }
            )
        )

    assert error.value.reason == "shadow_model_budget_invalid"
    assert "sk-secret-output-budget" not in str(error.value)


@pytest.mark.parametrize("invalid_context", [True, 0, -1, "sk-secret-context-budget"])
def test_shadow_limits_reject_invalid_context_budget_without_leaking(invalid_context):
    with pytest.raises(shadow_worker.ShadowAdapterError) as error:
        shadow_worker._shadow_limits_from_job(
            SimpleNamespace(
                model_config_snapshot={
                    "context_window_tokens": invalid_context,
                    "max_output_tokens": 8_000,
                }
            )
        )

    assert error.value.reason == "shadow_model_budget_invalid"
    assert "sk-secret-context-budget" not in str(error.value)


@pytest.mark.asyncio
async def test_shadow_hook_runs_after_canonical_persist_and_cannot_change_result(monkeypatch):
    prepared = _prepared(shadow_enabled=True)
    attempt = SimpleNamespace(id=UUID("80000000-0000-0000-0000-000000000002"))
    provider_response = SimpleNamespace(
        content='{"entities": [], "relations": []}',
        finish_reason="stop",
        provider_request_id=None,
        raw_response="{}",
        input_token_count=1,
        output_token_count=1,
        latency_ms=1,
    )
    events = []

    monkeypatch.setattr(
        "app.services.graph_extraction_worker._prepare_graph_extraction_unit",
        AsyncMock(return_value=prepared),
    )
    monkeypatch.setattr(
        "app.services.graph_extraction_worker._load_prepared_cache",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.graph_extraction_worker.create_pending_attempt",
        AsyncMock(return_value=attempt),
    )
    monkeypatch.setattr("app.services.graph_extraction_worker._preflight_provider_call", AsyncMock())
    monkeypatch.setattr(
        "app.services.graph_extraction_worker._call_provider_with_lease_renewal",
        AsyncMock(return_value=(provider_response, False)),
    )
    monkeypatch.setattr(
        "app.services.graph_extraction_worker.finalize_attempt",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "app.services.graph_extraction_worker.parse_graph_extraction_output",
        lambda _content: SimpleNamespace(model_dump=lambda **_kwargs: {}),
    )

    async def persist(*_args, **_kwargs):
        events.append("persist")
        return GraphExtractionProcessResult("succeeded")

    monkeypatch.setattr(
        "app.services.graph_extraction_worker._persist_payload_process_result",
        persist,
    )
    shadow = AsyncMock(side_effect=RuntimeError("unexpected shadow failure"))
    monkeypatch.setattr(
        "app.services.graph_claim_shadow_worker.run_shadow_after_canonical", shadow
    )
    stats = AsyncMock()
    monkeypatch.setattr("app.services.graph_claim_shadow_worker.record_shadow_statistics", stats)

    from app.services.graph_extraction_worker import process_graph_extraction_unit

    result = await process_graph_extraction_unit(
        _AsyncSessionFactory(),
        unit_id=UNIT_ID,
        claim_token=prepared.claim_token,
        provider=SimpleNamespace(),
    )

    assert result.outcome == "succeeded"
    assert events == ["persist"]
    shadow.assert_awaited_once()
    stats.assert_awaited_once()
    assert stats.await_args.kwargs["summary"].reason == "shadow_internal_error"


@pytest.mark.asyncio
async def test_shadow_provider_failure_is_stable_and_does_not_raise(monkeypatch):
    request = SimpleNamespace(messages=())
    telemetry = SimpleNamespace(latency_ms=3, input_token_count=None, output_token_count=None)
    monkeypatch.setattr(shadow_worker, "_build_request", AsyncMock(return_value=(request, {}, object())))
    monkeypatch.setattr(
        shadow_worker,
        "run_shadow_extraction",
        AsyncMock(
            side_effect=ShadowProviderRunError(
                "truncated_response", telemetry=telemetry
            )
        ),
    )
    stats = AsyncMock()
    monkeypatch.setattr(shadow_worker, "record_shadow_statistics", stats)

    result = await shadow_worker.run_shadow_after_canonical(
        _AsyncSessionFactory(), prepared=_prepared(shadow_enabled=True), provider=object()
    )

    assert result.status == "failed"
    assert result.reason == "truncated_response"
    stats.assert_awaited_once()
    assert stats.await_args.kwargs["attempted"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "status", "expected"),
    [
        ("network_error", None, "network"),
        ("http_error", 401, "auth"),
        ("http_error", 429, "rate_limit"),
        ("http_error", 503, "http"),
    ],
)
async def test_worker_provider_adapter_maps_stable_failure_tokens(monkeypatch, category, status, expected):
    request = SimpleNamespace(
        messages=(),
        limits=SimpleNamespace(max_output_tokens=321),
        request_hash="a" * 64,
        evidence_ref_keys=frozenset(),
    )
    monkeypatch.setattr(
        shadow_worker,
        "_build_request",
        AsyncMock(return_value=(request, {}, object())),
    )

    provider = SimpleNamespace(
        extract=AsyncMock(
            side_effect=GraphExtractionProviderError(
                category,
                "secret endpoint response must not persist",
                latency_ms=3,
                status_code=status,
            )
        )
    )

    async def no_retry(call, **_kwargs):
        return await call()

    monkeypatch.setattr(shadow_worker, "call_graph_extraction_provider", no_retry)
    monkeypatch.setattr(shadow_worker, "record_shadow_statistics", AsyncMock())

    result = await shadow_worker.run_shadow_after_canonical(
        _AsyncSessionFactory(), prepared=_prepared(shadow_enabled=True), provider=provider
    )

    assert result.status == "failed"
    assert result.reason == "provider_error"
    assert result.provider_error_category == expected
    assert result.http_status == status
    assert result.retry_count == 0
    assert "secret endpoint" not in str(result)


@pytest.mark.asyncio
async def test_worker_provider_adapter_classifies_invalid_envelope_without_leak(monkeypatch):
    request = SimpleNamespace(
        messages=(),
        limits=SimpleNamespace(max_output_tokens=8_000),
        request_hash="a" * 64,
        evidence_ref_keys=frozenset(),
    )
    monkeypatch.setattr(
        shadow_worker,
        "_build_request",
        AsyncMock(return_value=(request, {}, object())),
    )
    provider = SimpleNamespace(
        extract=AsyncMock(
            return_value=SimpleNamespace(
                content=None,
                finish_reason="stop",
                input_token_count=404,
                output_token_count=971,
                latency_ms=9_170,
            )
        )
    )

    async def no_retry(call, **_kwargs):
        return await call()

    monkeypatch.setattr(shadow_worker, "call_graph_extraction_provider", no_retry)
    monkeypatch.setattr(shadow_worker, "record_shadow_statistics", AsyncMock())

    result = await shadow_worker.run_shadow_after_canonical(
        _AsyncSessionFactory(),
        prepared=_prepared(shadow_enabled=True),
        provider=provider,
    )

    assert result.status == "failed"
    assert result.reason == "provider_error"
    assert result.provider_error_category == "invalid_envelope"
    assert result.http_status == 200
    assert "content" not in str(result)


@pytest.mark.asyncio
async def test_worker_provider_adapter_counts_retry_and_keeps_success_telemetry(monkeypatch):
    request = SimpleNamespace(
        messages=(),
        limits=SimpleNamespace(max_output_tokens=321, max_claims=32),
        request_hash="a" * 64,
        evidence_ref_keys=frozenset(),
    )
    monkeypatch.setattr(
        shadow_worker,
        "_build_request",
        AsyncMock(return_value=(request, {}, object())),
    )
    response = ShadowProviderResponseV1(
        content='{"claims": []}',
        finish_reason="stop",
        input_token_count=5,
        output_token_count=2,
        latency_ms=4,
    )
    provider = SimpleNamespace(
        extract=AsyncMock(
            side_effect=[
                GraphExtractionProviderError("http_error", "temporary", latency_ms=1, status_code=503),
                response,
            ]
        )
    )

    async def one_retry(call, **_kwargs):
        try:
            return await call()
        except GraphExtractionProviderError:
            return await call()

    monkeypatch.setattr(shadow_worker, "call_graph_extraction_provider", one_retry)
    monkeypatch.setattr(
        shadow_worker,
        "_persist_claims",
        AsyncMock(return_value=shadow_worker.ShadowRunSummary(status="succeeded")),
    )
    monkeypatch.setattr(
        shadow_worker,
        "_persist_decisions",
        AsyncMock(return_value=shadow_worker.ShadowRunSummary(status="succeeded")),
    )
    stats = AsyncMock()
    monkeypatch.setattr(shadow_worker, "record_shadow_statistics", stats)

    result = await shadow_worker.run_shadow_after_canonical(
        _AsyncSessionFactory(), prepared=_prepared(shadow_enabled=True), provider=provider
    )

    assert result.status == "succeeded"
    assert result.retry_count == 1
    assert result.final_outcome == "success"
    assert provider.extract.await_count == 2
    recorded = stats.await_args.kwargs["summary"]
    assert recorded.retry_count == 1
    assert recorded.response_sha256 is not None


@pytest.mark.asyncio
async def test_shadow_statistics_persist_only_bounded_additive_telemetry():
    factory = _StatisticsSessionFactory()
    response_hash = "b" * 64

    await shadow_worker.record_shadow_statistics(
        factory,
        job_id=JOB_ID,
        summary=shadow_worker.ShadowRunSummary(
            status="failed",
            reason="provider_error",
            provider_error_category="auth",
            http_status=401,
            retry_count=2,
            final_outcome="failed",
            response_sha256=response_hash,
            finish_reason="secret endpoint detail",
            parse_category="schema_extra",
            validation_error_count=2,
            validation_issues=(
                ShadowValidationIssueV1(
                    path="claims.0.source_mention",
                    error_type="model_type",
                    count=2,
                ),
            ),
        ),
        attempted=1,
    )

    stats = factory.session.job.statistics["claim_shadow"]
    assert stats["provider_error_category_counts"] == {"auth": 1}
    assert stats["provider_http_status_counts"] == {"401": 1}
    assert stats["provider_final_outcome_counts"] == {"failed": 1}
    assert stats["parse_category_counts"] == {"schema_extra": 1}
    assert stats["finish_reason_counts"] == {"unknown": 1}
    assert stats["response_sha256s"] == [response_hash]
    assert stats["retry_count_total"] == 2
    assert stats["retry_count_max"] == 2
    assert stats["validation_error_count"] == 2
    assert stats["validation_error_path_type_counts"] == {
        "claims.0.source_mention|model_type": 2
    }
    assert "secret endpoint detail" not in str(stats)


@pytest.mark.asyncio
async def test_shadow_database_failure_is_stable_and_separate_from_canonical(monkeypatch):
    request = SimpleNamespace(messages=())
    monkeypatch.setattr(shadow_worker, "_build_request", AsyncMock(return_value=(request, {}, object())))
    monkeypatch.setattr(
        shadow_worker,
        "run_shadow_extraction",
        AsyncMock(side_effect=SQLAlchemyError("database unavailable")),
    )
    stats = AsyncMock()
    monkeypatch.setattr(shadow_worker, "record_shadow_statistics", stats)

    result = await shadow_worker.run_shadow_after_canonical(
        _AsyncSessionFactory(), prepared=_prepared(shadow_enabled=True), provider=object()
    )

    assert result.status == "failed"
    assert result.reason == "persistence_failed"
    stats.assert_awaited_once()


@pytest.mark.asyncio
async def test_canonical_cache_hit_skips_shadow_and_keeps_provider_at_zero(monkeypatch):
    prepared = _prepared(shadow_enabled=True)
    monkeypatch.setattr(
        "app.services.graph_extraction_worker._prepare_graph_extraction_unit",
        AsyncMock(return_value=prepared),
    )
    monkeypatch.setattr(
        "app.services.graph_extraction_worker._load_prepared_cache",
        AsyncMock(return_value={"cached": True}),
    )
    persist = AsyncMock(
        return_value=GraphExtractionProcessResult("succeeded", ready_for_materialization=True)
    )
    monkeypatch.setattr(
        "app.services.graph_extraction_worker._persist_payload_process_result",
        persist,
    )
    skip = AsyncMock()
    monkeypatch.setattr("app.services.graph_claim_shadow_worker.record_shadow_skip", skip)
    shadow = AsyncMock()
    monkeypatch.setattr("app.services.graph_claim_shadow_worker.run_shadow_after_canonical", shadow)

    from app.services.graph_extraction_worker import process_graph_extraction_unit

    result = await process_graph_extraction_unit(
        object(), unit_id=UNIT_ID, claim_token=prepared.claim_token
    )

    assert result.outcome == "succeeded"
    persist.assert_awaited_once()
    assert skip.await_count == 1
    assert skip.await_args.kwargs == {"job_id": JOB_ID, "reason": "canonical_cache_hit"}
    assert shadow.await_count == 0


@pytest.mark.asyncio
async def test_canonical_provider_failure_never_enters_shadow(monkeypatch):
    prepared = _prepared(shadow_enabled=True)
    attempt = SimpleNamespace(id=UUID("80000000-0000-0000-0000-000000000001"))
    provider = SimpleNamespace(
        extract=AsyncMock(
            side_effect=GraphExtractionProviderError("timeout", "timeout", latency_ms=1)
        )
    )
    monkeypatch.setattr(
        "app.services.graph_extraction_worker._prepare_graph_extraction_unit",
        AsyncMock(return_value=prepared),
    )
    monkeypatch.setattr(
        "app.services.graph_extraction_worker._load_prepared_cache",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "app.services.graph_extraction_worker.create_pending_attempt",
        AsyncMock(return_value=attempt),
    )
    monkeypatch.setattr("app.services.graph_extraction_worker._preflight_provider_call", AsyncMock())
    monkeypatch.setattr("app.services.graph_extraction_worker.finalize_attempt", AsyncMock(return_value=True))
    monkeypatch.setattr(
        "app.services.graph_extraction_worker._finish_claim_after_error",
        AsyncMock(return_value=True),
    )
    monkeypatch.setattr("app.services.graph_claim_shadow_worker.run_shadow_after_canonical", AsyncMock())

    from app.services.graph_extraction_worker import process_graph_extraction_unit

    result = await process_graph_extraction_unit(
        _AsyncSessionFactory(),
        unit_id=UNIT_ID,
        claim_token=prepared.claim_token,
        provider=provider,
    )

    assert result.outcome == "failed"
    shadow = __import__("app.services.graph_claim_shadow_worker", fromlist=["run_shadow_after_canonical"])
    shadow.run_shadow_after_canonical.assert_not_awaited()


@pytest.mark.asyncio
async def test_flag_off_never_writes_shadow_statistics(monkeypatch):
    prepared = _prepared(shadow_enabled=False)
    monkeypatch.setattr(
        "app.services.graph_extraction_worker._prepare_graph_extraction_unit",
        AsyncMock(return_value=prepared),
    )
    monkeypatch.setattr(
        "app.services.graph_extraction_worker._load_prepared_cache",
        AsyncMock(return_value={"cached": True}),
    )
    monkeypatch.setattr(
        "app.services.graph_extraction_worker._persist_payload_process_result",
        AsyncMock(return_value=GraphExtractionProcessResult("succeeded")),
    )
    skip = AsyncMock()
    monkeypatch.setattr("app.services.graph_claim_shadow_worker.record_shadow_skip", skip)

    from app.services.graph_extraction_worker import process_graph_extraction_unit

    await process_graph_extraction_unit(
        object(), unit_id=UNIT_ID, claim_token=prepared.claim_token
    )

    assert skip.await_count == 0


def test_decision_projector_classifies_only_explicit_unknowns():
    claim = _claim(
        raw_predicate="unknown predicate",
        surface_direction="unknown",
        source_mention={
            "local_id": "source-1",
            "surface": "Source entity",
            "entity_type_hint": None,
            "evidence_ref": "evidence-1",
        },
        target_mention={
            "local_id": "target-1",
            "surface": "Target entity",
            "entity_type_hint": None,
            "evidence_ref": "evidence-1",
        },
    )

    decisions = shadow_worker._build_decision_projections(claim)

    assert {(item.decision_kind, item.reason_code) for item in decisions} == {
        ("mapping_candidate", "unknown_predicate"),
        ("mapping_candidate", "unknown_direction"),
        ("schema_extension_candidate", "unknown_source_type"),
        ("schema_extension_candidate", "unknown_target_type"),
    }
    assert all(item.proposal.raw_predicate == "unknown predicate" for item in decisions)
    assert all(
        getattr(item.proposal, "suggested_canonical_key", None) is None
        for item in decisions
        if item.decision_kind == "mapping_candidate"
    )
    assert all("relation_type_key" not in item.model_dump(mode="json") for item in decisions)


def test_decision_projector_does_not_create_decision_without_evidence():
    claim = _claim().model_copy(update={"evidence_refs": ()})

    assert shadow_worker._build_decision_projections(claim) == ()


@pytest.mark.asyncio
async def test_decision_persistence_failure_keeps_shadow_success(monkeypatch):
    request = SimpleNamespace(messages=(), limits=SimpleNamespace(max_output_tokens=321))
    run_result = SimpleNamespace(
        claims=(),
        telemetry=SimpleNamespace(latency_ms=1, input_token_count=1, output_token_count=1),
    )
    monkeypatch.setattr(
        shadow_worker,
        "_build_request",
        AsyncMock(return_value=(request, {}, object())),
    )
    monkeypatch.setattr(shadow_worker, "run_shadow_extraction", AsyncMock(return_value=run_result))
    monkeypatch.setattr(
        shadow_worker,
        "_persist_claims",
        AsyncMock(return_value=shadow_worker.ShadowRunSummary(status="succeeded")),
    )
    decision_persist = AsyncMock(
        return_value=shadow_worker.ShadowRunSummary(
            status="succeeded", reason="decision_persistence_failed"
        )
    )
    monkeypatch.setattr(shadow_worker, "_persist_decisions", decision_persist)
    monkeypatch.setattr(shadow_worker, "record_shadow_statistics", AsyncMock())

    session_factory = _AsyncSessionFactory()
    result = await shadow_worker.run_shadow_after_canonical(
        session_factory, prepared=_prepared(shadow_enabled=True), provider=object()
    )

    assert result.status == "succeeded"
    assert result.reason == "decision_persistence_failed"
    decision_persist.assert_awaited_once_with(session_factory, ())


@pytest.mark.asyncio
async def test_real_decision_persistence_failure_is_best_effort():
    class FailingSessionFactory:
        def __call__(self):
            raise RuntimeError("decision database unavailable")

    claim = _claim(
        raw_predicate="unknown predicate",
        source_mention={
            "local_id": "source-1",
            "surface": "Source entity",
            "entity_type_hint": None,
            "evidence_ref": "evidence-1",
        },
        target_mention={
            "local_id": "target-1",
            "surface": "Target entity",
            "entity_type_hint": None,
            "evidence_ref": "evidence-1",
        },
    )

    result = await shadow_worker._persist_decisions(FailingSessionFactory(), (claim,))

    assert result.status == "succeeded"
    assert result.reason == "decision_persistence_failed"
