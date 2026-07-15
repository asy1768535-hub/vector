from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr, ValidationError

from app.config import settings
from app.services.graph_extraction_eval import (
    GraphEvalClassification,
    GraphEvalDatasetCounts,
    GraphEvalMetricReport,
    GraphEvalRate,
    GraphEvalRunArtifact,
    load_graph_eval_dataset,
)
from app.services.graph_extraction_eval_runtime import (
    eval_uuid,
    require_release_worktree_clean,
    run_eval_workers,
    validate_eval_database_name,
    validate_real_run_environment,
    write_eval_artifact,
)


ROOT = Path(__file__).resolve().parents[1]
SMOKE = ROOT / "eval/graph_extraction/manifests/development_smoke_v1.json"


def _rate(value: float = 1.0) -> GraphEvalRate:
    return GraphEvalRate(numerator=1, denominator=1, value=value)


def _classification() -> GraphEvalClassification:
    return GraphEvalClassification(
        true_positive=1,
        false_positive=0,
        false_negative=0,
        precision=_rate(),
        recall=_rate(),
    )


def _artifact(run_id: str = "mock-run-1") -> GraphEvalRunArtifact:
    now = datetime(2026, 7, 14, tzinfo=timezone.utc)
    metrics = GraphEvalMetricReport(
        entity=_classification(),
        relation=_classification(),
        json_parse_rate=_rate(),
        schema_valid_rate=_rate(),
        invalid_evidence_rate=GraphEvalRate(numerator=0, denominator=1, value=0.0),
        ambiguous_evidence_count=0,
        candidate_duplicate_rate=GraphEvalRate(numerator=0, denominator=2, value=0.0),
        cross_revision_evidence_count=0,
        eval_formal_write_count=0,
    )
    return GraphEvalRunArtifact(
        schema_version="graph-extraction-eval-result-v1",
        run_id=run_id,
        phase="mock",
        status="passed",
        real_provider=False,
        started_at=now,
        finished_at=now,
        code_commit="a" * 40,
        alembic_head="0022",
        database_name="vkt_m6_eval_mock_1",
        dataset_id="development-smoke-v1",
        dataset_counts=GraphEvalDatasetCounts(
            documents=10, units=40, entities=50, relations=40
        ),
        dataset_manifest_sha256="b" * 64,
        dataset_content_sha256="c" * 64,
        evaluation_config_hash="d" * 64,
        model_provider="mock",
        model_name="mock",
        component_versions={"prompt_version": "v1"},
        job_ids=(uuid.UUID("10000000-0000-0000-0000-000000000001"),),
        job_status_counts={"succeeded": 1},
        unit_status_counts={"succeeded": 1},
        attempt_status_counts={"succeeded": 1},
        model_attempt_count=1,
        real_model_call_count=0,
        provider_request_id_count=1,
        provider_request_id_sha256="e" * 64,
        metrics=metrics,
        stable_error_code_counts={},
    )


def test_eval_uuid_is_stable_scoped_and_requires_non_empty_parts():
    assert eval_uuid("release-v1", "library") == uuid.UUID(
        "65a3c877-8d2a-59a7-aa6d-a632af02cd9e"
    )
    assert eval_uuid("release-v1", "library") != eval_uuid("release-v2", "library")
    with pytest.raises(ValueError):
        eval_uuid("release-v1", "")


@pytest.mark.parametrize(
    "value",
    [
        "postgres",
        "vkt_m6_eval_BAD",
        "vkt_m6_eval_",
        "vkt_m6_eval_a-b",
        "vkt_m6_eval_" + "a" * 60,
    ],
)
def test_eval_database_name_is_strictly_scoped(value: str):
    with pytest.raises(ValueError):
        validate_eval_database_name(value)
    assert validate_eval_database_name("vkt_m6_eval_release_01") == (
        "vkt_m6_eval_release_01"
    )


def test_real_run_requires_explicit_process_credentials_and_matching_confirmation(
    monkeypatch,
):
    loaded = load_graph_eval_dataset(repository_root=ROOT, manifest_path=SMOKE)
    monkeypatch.delenv("VECTOR_KB_M6_EVAL_ADMIN_DSN", raising=False)
    monkeypatch.delenv("GRAPH_EXTRACTION_API_KEY", raising=False)
    with pytest.raises(ValueError, match="confirmation"):
        validate_real_run_environment(
            loaded=loaded,
            database_name="vkt_m6_eval_smoke_1",
            confirmation="wrong",
            workers=1,
            phase="development-smoke",
        )
    with pytest.raises(ValueError, match="ADMIN_DSN"):
        validate_real_run_environment(
            loaded=loaded,
            database_name="vkt_m6_eval_smoke_1",
            confirmation="development-smoke-v1",
            workers=1,
            phase="development-smoke",
        )


def _configure_real_deepseek(monkeypatch) -> None:
    monkeypatch.setenv("VECTOR_KB_M6_EVAL_ADMIN_DSN", "postgresql://test")
    monkeypatch.setenv("GRAPH_EXTRACTION_API_KEY", "DEEPSEEK-TEST-KEY")
    monkeypatch.setattr(
        settings,
        "graph_extraction_api_key",
        SecretStr("DEEPSEEK-TEST-KEY"),
    )
    monkeypatch.setattr(settings, "graph_extraction_enabled", True)
    monkeypatch.setattr(settings, "graph_extraction_auto_trigger_enabled", False)
    monkeypatch.setattr(
        settings, "graph_extraction_base_url", "https://api.deepseek.com/v1"
    )
    monkeypatch.setattr(settings, "graph_extraction_model", "deepseek-chat")


def test_real_run_accepts_only_frozen_deepseek_environment(monkeypatch):
    loaded = load_graph_eval_dataset(repository_root=ROOT, manifest_path=SMOKE)
    _configure_real_deepseek(monkeypatch)

    assert validate_real_run_environment(
        loaded=loaded,
        database_name="vkt_m6_eval_smoke_2",
        confirmation="development-smoke-v1",
        workers=2,
        phase="development-smoke",
    ) == "postgresql://test"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("graph_extraction_model", "qwen-plus"),
        (
            "graph_extraction_base_url",
            "https://dashscope.aliyuncs.com/compatible-mode/v1",
        ),
        ("graph_extraction_base_url", "https://api.deepseek.com/v1/"),
    ],
)
def test_real_run_rejects_retired_dashscope_environment(monkeypatch, name, value):
    loaded = load_graph_eval_dataset(repository_root=ROOT, manifest_path=SMOKE)
    _configure_real_deepseek(monkeypatch)
    monkeypatch.setattr(settings, name, value)

    with pytest.raises(ValueError, match="DeepSeek"):
        validate_real_run_environment(
            loaded=loaded,
            database_name="vkt_m6_eval_smoke_2",
            confirmation="development-smoke-v1",
            workers=2,
            phase="development-smoke",
        )


def test_failed_dashscope_artifact_remains_valid_historical_evidence():
    path = (
        ROOT
        / "eval/graph_extraction/results/development-smoke-v1-20260714-01.json"
    )
    artifact = GraphEvalRunArtifact.model_validate_json(path.read_text(encoding="utf-8"))

    assert artifact.status == "failed"
    assert artifact.model_provider == "dashscope"
    assert artifact.model_name == "qwen-plus"


def test_run_artifact_rejects_inconsistent_real_call_count():
    payload = _artifact().model_dump(mode="json")
    payload["real_model_call_count"] = 1
    with pytest.raises(ValidationError, match="real_model_call_count"):
        GraphEvalRunArtifact.model_validate(payload)


def test_eval_worker_orchestration_reuses_claim_and_process_with_provider_factory():
    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):  # noqa: ARG002
            return False

    class SessionFactory:
        def __call__(self):
            return SessionContext()

    sessions = SessionFactory()
    token = uuid.uuid4()
    unit = SimpleNamespace(id=uuid.uuid4(), claim_token=token)
    provider = object()
    process_result = SimpleNamespace(outcome="succeeded")
    with (
        patch(
            "app.services.graph_extraction_eval_runtime.claim_graph_extraction_unit",
            new=AsyncMock(side_effect=[unit, None]),
        ) as claim,
        patch(
            "app.services.graph_extraction_eval_runtime.process_graph_extraction_unit",
            new=AsyncMock(return_value=process_result),
        ) as process,
        patch(
            "app.services.graph_extraction_eval_runtime._retry_failed_eval_jobs",
            new=AsyncMock(return_value=0),
        ),
    ):
        outcomes = asyncio.run(
            run_eval_workers(
                sessions,
                library_id=uuid.uuid4(),
                workers=1,
                provider_factory=lambda row: provider if row is unit else None,
            )
        )

    assert outcomes == {"succeeded": 1}
    assert claim.await_count == 2
    process.assert_awaited_once_with(
        sessions,
        unit_id=unit.id,
        claim_token=token,
        provider=provider,
    )


def test_run_artifact_rejects_passed_non_terminal_jobs():
    payload = _artifact().model_dump(mode="json")
    payload["job_status_counts"] = {"failed": 1}
    with pytest.raises(ValidationError, match="succeeded Jobs"):
        GraphEvalRunArtifact.model_validate(payload)


def test_post_freeze_artifact_requires_policy():
    payload = _artifact().model_dump(mode="json")
    payload["phase"] = "post-freeze"
    with pytest.raises(ValidationError, match="frozen policy"):
        GraphEvalRunArtifact.model_validate(payload)


def test_artifact_writer_is_atomic_scoped_and_immutable(tmp_path: Path):
    root = tmp_path
    output = root / "eval/graph_extraction/results/mock-run-1.json"
    artifact = _artifact()
    assert write_eval_artifact(
        repository_root=root,
        output_path=output,
        artifact=artifact,
    ) == output.resolve()
    assert json.loads(output.read_text(encoding="utf-8"))["run_id"] == "mock-run-1"
    assert not output.with_suffix(".json.tmp").exists()
    with pytest.raises(FileExistsError, match="immutable"):
        write_eval_artifact(
            repository_root=root,
            output_path=output,
            artifact=artifact,
        )


def test_artifact_writer_rejects_filename_or_directory_mismatch(tmp_path: Path):
    artifact = _artifact()
    with pytest.raises(ValueError, match="filename"):
        write_eval_artifact(
            repository_root=tmp_path,
            output_path=tmp_path / "eval/graph_extraction/results/other.json",
            artifact=artifact,
        )
    with pytest.raises(ValueError, match="directly under"):
        write_eval_artifact(
            repository_root=tmp_path,
            output_path=tmp_path / "elsewhere/mock-run-1.json",
            artifact=artifact,
        )


def test_release_clean_check_allows_only_the_preexisting_user_files():
    def fake_run(args, **_kwargs):
        command = tuple(args[1:])
        if command == ("rev-parse", "HEAD"):
            return SimpleNamespace(stdout="a" * 40 + "\n", returncode=0)
        if command in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
            return SimpleNamespace(stdout="", returncode=0)
        if command == ("ls-files", "--others", "--exclude-standard"):
            return SimpleNamespace(
                stdout="_verify_baseline.py\ndocs/codex-handoff.md\n",
                returncode=0,
            )
        raise AssertionError(command)

    with patch(
        "app.services.graph_extraction_eval_runtime.subprocess.run",
        side_effect=fake_run,
    ):
        commit = require_release_worktree_clean(ROOT)
    assert len(commit) == 40


def test_release_clean_check_rejects_unexpected_untracked_result():
    def fake_run(args, **_kwargs):
        command = tuple(args[1:])
        if command == ("rev-parse", "HEAD"):
            return SimpleNamespace(stdout="a" * 40 + "\n", returncode=0)
        if command in (("diff", "--quiet"), ("diff", "--cached", "--quiet")):
            return SimpleNamespace(stdout="", returncode=0)
        return SimpleNamespace(
            stdout="eval/graph_extraction/results/uncommitted.json\n",
            returncode=0,
        )

    with (
        patch(
            "app.services.graph_extraction_eval_runtime.subprocess.run",
            side_effect=fake_run,
        ),
        pytest.raises(ValueError, match="unexpected untracked"),
    ):
        require_release_worktree_clean(ROOT)
