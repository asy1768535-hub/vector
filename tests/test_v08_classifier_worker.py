from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, patch

import pytest
from pydantic import SecretStr

from app.config import Settings
from app.models.classification_job import DocumentClassificationJob
from app.services.classification_decisions import ClassificationRunResult
from app.services.classification_decision_contracts import (
    ClassifierProposalInput,
    ClassificationDecisionError,
)
from app.services.classification_jobs import (
    ClaimedClassificationJob,
    ClassificationJobRecoveryResult,
)
from app.services.classification_provider import ClassificationProviderError
from app.services.classification_runtime_contracts import ClassificationRuntimeError
from app.services.classification_worker import (
    PreparedClassificationJob,
    call_classification_provider_with_lease_renewal,
    prepare_classification_job,
    process_classification_job,
    publish_classification_failure,
    publish_classification_success,
    record_recovered_classification_failure,
    run_classification_worker,
)


NOW = datetime(2026, 7, 22, 21, 0, tzinfo=timezone.utc)


def _settings(**values) -> Settings:
    defaults = {
        "organization_authorization_enabled": True,
        "classification_taxonomy_enabled": True,
        "classification_decision_enabled": True,
        "classification_runtime_enabled": True,
        "classification_external_model_enabled": True,
        "classification_api_key": SecretStr("test-key"),
    }
    return Settings(_env_file=None, **{**defaults, **values})


def _prepared() -> PreparedClassificationJob:
    primary_id, secondary_id = uuid.uuid4(), uuid.uuid4()
    return PreparedClassificationJob(
        job_id=uuid.uuid4(),
        claim_token=uuid.uuid4(),
        library_id=uuid.uuid4(),
        document_id=uuid.uuid4(),
        document_revision_id=uuid.uuid4(),
        revision_content_hash="a" * 64,
        taxonomy_version_id=uuid.uuid4(),
        enabled_label_ids=(primary_id, secondary_id),
        enabled_label_set_hash="b" * 64,
        classifier_version="document-classifier-v1",
        model_provider="deepseek",
        model_name="deepseek-v4-pro",
        model_config_hash="c" * 64,
        prompt_version="classification-prompt-v1",
        retry_generation=0,
        trigger_type="revision_ready",
        requested_by_user_id=uuid.uuid4(),
        labels_by_key={"contract": primary_id, "urgent": secondary_id},
        messages=[{"role": "user", "content": "bounded source"}],
    )


def _processing_job(prepared: PreparedClassificationJob) -> DocumentClassificationJob:
    return DocumentClassificationJob(
        id=prepared.job_id,
        library_id=prepared.library_id,
        document_id=prepared.document_id,
        document_revision_id=prepared.document_revision_id,
        revision_content_hash=prepared.revision_content_hash,
        taxonomy_version_id=prepared.taxonomy_version_id,
        enabled_label_set_hash=prepared.enabled_label_set_hash,
        classifier_version=prepared.classifier_version,
        model_provider=prepared.model_provider,
        model_name=prepared.model_name,
        model_config_hash=prepared.model_config_hash,
        prompt_version=prepared.prompt_version,
        input_fingerprint="d" * 64,
        idempotency_key="e" * 64,
        retry_generation=prepared.retry_generation,
        trigger_type="revision_ready",
        status="processing",
        attempt_count=1,
        claim_token=prepared.claim_token,
        claimed_by="worker",
        lease_expires_at=NOW + timedelta(minutes=3),
        last_heartbeat_at=NOW,
        requested_by_user_id=prepared.requested_by_user_id,
        created_at=NOW,
        updated_at=NOW,
        started_at=NOW,
    )


class _Transaction:
    def __init__(self, factory):
        self.factory = factory

    async def __aenter__(self):
        self.factory.depth += 1
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.factory.depth -= 1
        return False


class _Session:
    def __init__(self, factory):
        self.factory = factory
        self.flush = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    def begin(self):
        return _Transaction(self.factory)


class _Factory:
    def __init__(self):
        self.depth = 0
        self.sessions = []

    def __call__(self):
        session = _Session(self)
        self.sessions.append(session)
        return session


def test_provider_call_runs_without_transaction_and_returns_before_renewal():
    factory = _Factory()
    claimed = ClaimedClassificationJob(uuid.uuid4(), uuid.uuid4())

    class Provider:
        async def generate(self, _messages):
            assert factory.depth == 0
            return SimpleNamespace(content="{}")

    result = asyncio.run(
        call_classification_provider_with_lease_renewal(
            factory,
            claimed=claimed,
            provider=Provider(),
            messages=[{"role": "user", "content": "source"}],
            renew_seconds=30,
            lease_seconds=180,
        )
    )
    assert result.content == "{}" and factory.depth == 0
    assert not factory.sessions


def test_provider_call_cancels_when_lease_renewal_loses_claim():
    factory = _Factory()
    claimed = ClaimedClassificationJob(uuid.uuid4(), uuid.uuid4())

    class Provider:
        async def generate(self, _messages):
            await asyncio.sleep(10)

    with patch(
        "app.services.classification_worker.renew_classification_lease",
        new=AsyncMock(return_value=False),
    ) as renew:
        with pytest.raises(ClassificationRuntimeError) as exc_info:
            asyncio.run(
                call_classification_provider_with_lease_renewal(
                    factory,
                    claimed=claimed,
                    provider=Provider(),
                    messages=[{"role": "user", "content": "source"}],
                    renew_seconds=0.001,
                    lease_seconds=180,
                )
            )
    assert exc_info.value.code == "classification_claim_lost"
    renew.assert_awaited_once()
    assert factory.depth == 0


def test_success_publication_calls_governance_once_and_links_exact_run():
    prepared = _prepared()
    job = _processing_job(prepared)
    run = SimpleNamespace(id=uuid.uuid4())
    result = ClassificationRunResult(run, (), None, ())
    factory = _Factory()
    proposals = (
        ClassifierProposalInput(
            "primary",
            950_000,
            label_id=prepared.enabled_label_ids[0],
        ),
    )
    with (
        patch(
            "app.services.classification_worker._lock_final_scope",
            new=AsyncMock(return_value=job),
        ),
        patch(
            "app.services.classification_worker.submit_classification_run",
            new=AsyncMock(return_value=result),
        ) as submit,
    ):
        run_id = asyncio.run(
            publish_classification_success(
                factory,
                prepared=prepared,
                proposals=proposals,
                now=NOW,
                config=_settings(),
            )
        )
    assert run_id == run.id
    assert job.status == "succeeded" and job.result_run_id == run.id
    assert job.claim_token is None and job.finished_at == NOW
    command = submit.await_args.args[1]
    assert command.proposals == proposals
    assert command.enabled_label_ids == prepared.enabled_label_ids
    assert command.retry_generation == prepared.retry_generation
    assert factory.depth == 0


def test_failure_publication_records_only_stable_code_and_links_failure_run():
    prepared = _prepared()
    job = _processing_job(prepared)
    run = SimpleNamespace(id=uuid.uuid4())
    result = ClassificationRunResult(run, (), None, ())
    factory = _Factory()
    with (
        patch(
            "app.services.classification_worker._lock_final_scope",
            new=AsyncMock(return_value=job),
        ),
        patch(
            "app.services.classification_worker.record_classification_failure",
            new=AsyncMock(return_value=result),
        ) as record,
    ):
        run_id = asyncio.run(
            publish_classification_failure(
                factory,
                prepared=prepared,
                error_code="provider_timeout",
                now=NOW,
                config=_settings(),
            )
        )
    assert run_id == run.id
    assert job.status == "failed" and job.result_run_id == run.id
    assert job.error_code == "provider_timeout"
    command = record.await_args.args[1]
    assert command.error_code == "provider_timeout"
    assert not hasattr(command, "raw_error")


def test_process_success_parses_provider_and_publishes_without_open_transaction():
    prepared = _prepared()
    claimed = ClaimedClassificationJob(prepared.job_id, prepared.claim_token)
    factory = _Factory()
    content = json.dumps(
        {
            "primary_candidates": [
                {"label_key": "contract", "confidence_micros": 950_000}
            ],
            "secondary_candidates": [
                {"label_key": "urgent", "confidence_micros": 910_000}
            ],
        }
    )

    class Provider:
        async def generate(self, _messages):
            assert factory.depth == 0
            return SimpleNamespace(content=content)

    with (
        patch(
            "app.services.classification_worker.prepare_classification_job",
            new=AsyncMock(return_value=prepared),
        ),
        patch(
            "app.services.classification_worker.publish_classification_success",
            new=AsyncMock(return_value=uuid.uuid4()),
        ) as publish,
    ):
        result = asyncio.run(
            process_classification_job(
                factory,
                claimed=claimed,
                provider=Provider(),
                config=_settings(),
            )
        )
    assert result.outcome == "succeeded"
    proposals = publish.await_args.kwargs["proposals"]
    assert [(item.role, item.label_id) for item in proposals] == [
        ("primary", prepared.labels_by_key["contract"]),
        ("secondary", prepared.labels_by_key["urgent"]),
    ]
    assert factory.depth == 0


@pytest.mark.parametrize(
    ("error", "expected_code"),
    [
        (
            ClassificationProviderError("timeout", "safe", latency_ms=1),
            "provider_timeout",
        ),
        (
            ClassificationRuntimeError("provider_invalid_output", "safe"),
            "provider_invalid_output",
        ),
    ],
)
def test_process_failure_calls_sanitized_governance_failure(error, expected_code):
    prepared = _prepared()
    claimed = ClaimedClassificationJob(prepared.job_id, prepared.claim_token)
    if isinstance(error, ClassificationProviderError):
        provider = SimpleNamespace(generate=AsyncMock(side_effect=error))
        parse_patch = patch(
            "app.services.classification_worker.parse_classifier_output"
        )
    else:
        provider = SimpleNamespace(generate=AsyncMock(return_value=SimpleNamespace(content="{}")))
        parse_patch = patch(
            "app.services.classification_worker.parse_classifier_output",
            side_effect=error,
        )
    with (
        patch(
            "app.services.classification_worker.prepare_classification_job",
            new=AsyncMock(return_value=prepared),
        ),
        parse_patch,
        patch(
            "app.services.classification_worker.publish_classification_failure",
            new=AsyncMock(return_value=uuid.uuid4()),
        ) as publish_failure,
    ):
        result = asyncio.run(
            process_classification_job(
                _Factory(),
                claimed=claimed,
                provider=provider,
                config=_settings(),
            )
        )
    assert result.outcome == "failed" and result.error_code == expected_code
    assert publish_failure.await_args.kwargs["error_code"] == expected_code


def test_disabled_auto_trigger_does_not_open_session():
    from app.services.classification_jobs import enqueue_ready_revision_classification

    calls = []

    def factory():
        calls.append(True)
        raise AssertionError("disabled trigger must not open a session")

    result = asyncio.run(
        enqueue_ready_revision_classification(
            library_id=uuid.uuid4(),
            document_id=uuid.uuid4(),
            revision_id=uuid.uuid4(),
            session_factory=factory,
            config=Settings(_env_file=None),
        )
    )
    assert result == () and calls == []


def test_one_shot_worker_compensates_then_drains_new_job():
    factory = _Factory()
    claimed = ClaimedClassificationJob(uuid.uuid4(), uuid.uuid4())
    metadata = {}
    with (
        patch(
            "app.services.classification_worker.recover_stale_classification_jobs",
            new=AsyncMock(return_value=ClassificationJobRecoveryResult(0, 0)),
        ),
        patch(
            "app.services.classification_worker.claim_classification_job",
            new=AsyncMock(side_effect=[None, claimed, None]),
        ),
        patch(
            "app.services.classification_worker.list_unrecorded_attempt_failure_job_ids",
            new=AsyncMock(return_value=()),
        ),
        patch(
            "app.services.classification_worker.compensate_ready_revision_classifications",
            new=AsyncMock(side_effect=[1, 0]),
        ) as compensate,
        patch(
            "app.services.classification_worker.process_classification_job",
            new=AsyncMock(return_value=SimpleNamespace(outcome="succeeded")),
        ) as process,
    ):
        asyncio.run(
            run_classification_worker(
                watch=False,
                metadata=metadata,
                session_factory=factory,
                config=_settings(),
            )
        )
    assert compensate.await_count == 2
    process.assert_awaited_once()
    assert metadata["compensated"] == 1
    assert metadata["claimed"] == 1
    assert metadata["succeeded"] == 1


def test_worker_retries_unrecorded_terminal_failure_after_restart():
    factory = _Factory()
    job_id = uuid.uuid4()
    metadata = {}
    with (
        patch(
            "app.services.classification_worker.recover_stale_classification_jobs",
            new=AsyncMock(return_value=ClassificationJobRecoveryResult(0, 0)),
        ),
        patch(
            "app.services.classification_worker.claim_classification_job",
            new=AsyncMock(return_value=None),
        ),
        patch(
            "app.services.classification_worker.list_unrecorded_attempt_failure_job_ids",
            new=AsyncMock(return_value=(job_id,)),
        ),
        patch(
            "app.services.classification_worker.record_recovered_classification_failure",
            new=AsyncMock(return_value=True),
        ) as record,
        patch(
            "app.services.classification_worker.compensate_ready_revision_classifications",
            new=AsyncMock(return_value=0),
        ),
    ):
        asyncio.run(
            run_classification_worker(
                watch=False,
                metadata=metadata,
                session_factory=factory,
                config=_settings(),
            )
        )
    record.assert_awaited_once_with(factory, job_id=job_id, config=ANY)
    assert metadata["recovered_failed"] == 1


def test_recovered_terminal_failure_records_governance_run_once():
    prepared = _prepared()
    job = _processing_job(prepared)
    job.status = "failed"
    job.error_code = "lease_expired_attempt_limit"
    job.error_message = "classification Job lease expired at its attempt limit"
    job.finished_at = NOW
    job.claim_token = None
    job.claimed_by = None
    job.lease_expires_at = None
    job.last_heartbeat_at = None
    document = SimpleNamespace(id=prepared.document_id)
    library = SimpleNamespace(id=prepared.library_id)
    revision = SimpleNamespace(id=prepared.document_revision_id)
    taxonomy = SimpleNamespace(id=prepared.taxonomy_version_id)
    labels = tuple(
        SimpleNamespace(id=label_id)
        for label_id in prepared.enabled_label_ids
    )
    run = SimpleNamespace(id=uuid.uuid4())

    class _Rows:
        def scalars(self):
            return self

        def first(self):
            return document

    class _RecoverySession(_Session):
        async def get(self, model, _identity):
            if model is DocumentClassificationJob:
                return job
            if model.__name__ == "Library":
                return library
            return revision

        async def execute(self, _statement):
            return _Rows()

    class _RecoveryFactory(_Factory):
        def __call__(self):
            session = _RecoverySession(self)
            self.sessions.append(session)
            return session

    factory = _RecoveryFactory()
    with (
        patch("app.services.classification_worker.validate_classification_scope"),
        patch(
            "app.services.classification_worker.lock_classification_scope",
            new=AsyncMock(
                return_value=(taxonomy, labels, {label.id: label for label in labels})
            ),
        ),
        patch(
            "app.services.classification_worker.lock_classification_job",
            new=AsyncMock(return_value=job),
        ),
        patch("app.services.classification_worker._require_current_job_identity"),
        patch(
            "app.services.classification_worker.record_classification_failure",
            new=AsyncMock(return_value=ClassificationRunResult(run, (), None, ())),
        ) as record,
    ):
        recorded = asyncio.run(
            record_recovered_classification_failure(
                factory,
                job_id=job.id,
                now=NOW,
                config=_settings(),
            )
        )
    assert recorded is True and job.result_run_id == run.id
    record.assert_awaited_once()
    command = record.await_args.args[1]
    assert command.error_code == "lease_expired_attempt_limit"
    assert command.document_revision_id == prepared.document_revision_id


def test_prepare_locks_document_and_scope_before_job():
    prepared = _prepared()
    job = _processing_job(prepared)
    document = SimpleNamespace(id=prepared.document_id)
    library = SimpleNamespace(id=prepared.library_id)
    revision = SimpleNamespace(id=prepared.document_revision_id)
    taxonomy = SimpleNamespace(id=prepared.taxonomy_version_id)
    labels = tuple(
        SimpleNamespace(id=label_id, key=key, label=key.title(), description=None)
        for key, label_id in prepared.labels_by_key.items()
    )
    events = []

    class _Rows:
        def scalars(self):
            return self

        def first(self):
            return document

    class _LockSession(_Session):
        async def get(self, model, identity):
            if model is DocumentClassificationJob:
                events.append("read_job")
                return job
            if model.__name__ == "Library":
                return library
            return revision

        async def execute(self, _statement):
            events.append("lock_document")
            return _Rows()

    class _LockFactory(_Factory):
        def __call__(self):
            session = _LockSession(self)
            self.sessions.append(session)
            return session

    async def lock_scope(*_args, **_kwargs):
        events.append("lock_scope")
        return taxonomy, labels, {label.id: label for label in labels}

    async def lock_job(*_args, **_kwargs):
        events.append("lock_job")
        return job

    with (
        patch("app.services.classification_worker.validate_classification_scope"),
        patch(
            "app.services.classification_worker.load_artifact_source_text",
            new=AsyncMock(return_value="bounded source"),
        ),
        patch(
            "app.services.classification_worker.lock_classification_scope",
            side_effect=lock_scope,
        ),
        patch(
            "app.services.classification_worker.lock_classification_job",
            side_effect=lock_job,
        ),
        patch("app.services.classification_worker._require_current_job_identity"),
    ):
        result = asyncio.run(
            prepare_classification_job(
                _LockFactory(),
                claimed=ClaimedClassificationJob(job.id, prepared.claim_token),
                now=NOW,
                config=_settings(),
            )
        )
    assert result.job_id == job.id
    assert events.index("lock_document") < events.index("lock_scope") < events.index("lock_job")


def test_governance_taxonomy_drift_is_converted_to_runtime_state_change():
    prepared = _prepared()
    job = _processing_job(prepared)
    proposals = (
        ClassifierProposalInput(
            "primary",
            950_000,
            label_id=prepared.enabled_label_ids[0],
        ),
    )
    with (
        patch(
            "app.services.classification_worker._lock_final_scope",
            new=AsyncMock(return_value=job),
        ),
        patch(
            "app.services.classification_worker.submit_classification_run",
            new=AsyncMock(
                side_effect=ClassificationDecisionError(
                    "classification_taxonomy_state_changed"
                )
            ),
        ),
    ):
        with pytest.raises(ClassificationRuntimeError) as exc_info:
            asyncio.run(
                publish_classification_success(
                    _Factory(),
                    prepared=prepared,
                    proposals=proposals,
                    now=NOW,
                    config=_settings(),
                )
            )
    assert exc_info.value.code == "classification_taxonomy_state_changed"


def test_one_shot_entrypoint_emits_stopping_heartbeat_on_failure(monkeypatch):
    from app.services import classification_worker as worker_service
    from app.services import heartbeat
    from app.workers import classifications

    monkeypatch.setattr(classifications.settings, "classification_runtime_enabled", True)
    monkeypatch.setattr(
        classifications,
        "validate_classification_runtime_startup",
        lambda _settings: None,
    )
    beat = AsyncMock()
    monkeypatch.setattr(heartbeat, "beat", beat)
    monkeypatch.setattr(heartbeat, "make_instance_id", lambda: "classifier-test")
    monkeypatch.setattr(
        worker_service,
        "run_classification_worker",
        AsyncMock(side_effect=RuntimeError("worker failed")),
    )
    with pytest.raises(RuntimeError, match="worker failed"):
        asyncio.run(classifications.run(watch=False))
    assert beat.await_count == 2
    assert beat.await_args_list[-1].kwargs["status"] == "stopping"
