from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any
import pytest

from app.workers import embedder, importer

TARGET_LIB = uuid.UUID("2ed27105-9d37-4094-9c07-ec4446f8a56a")
HISTORICAL_LIB = uuid.UUID("c5d69a03-2cda-47fd-a273-18f52746bf7e")


class _MockResult:
    def __init__(self, rows: list[Any] | None = None, rowcount: int = 0) -> None:
        self._rows = rows or []
        self.rowcount = rowcount

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return self._rows


class _MockDb:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.params_list: list[dict[str, Any]] = []

    async def execute(self, statement, params=None):
        self.statements.append(str(statement))
        self.params_list.append(params or {})
        return _MockResult()

    async def commit(self) -> None:
        return None


def test_importer_startup_failfast_when_missing_target_library_id(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.delenv("WORKER_TARGET_LIBRARY_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        importer.get_worker_target_config()
    assert exc.value.code == 1


def test_importer_startup_failfast_when_invalid_target_library_id(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", "123-not-a-valid-uuid")
    with pytest.raises(SystemExit) as exc:
        importer.get_worker_target_config()
    assert exc.value.code == 1


def test_importer_startup_failfast_when_both_switches_missing(monkeypatch) -> None:
    """试用模式下两个开关同时漏配时，必须报错退出 exit(1)，杜绝无限制全局消费。"""
    monkeypatch.delenv("WORKER_EXCLUDE_PDF", raising=False)
    monkeypatch.delenv("WORKER_TARGET_LIBRARY_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        importer.get_worker_target_config()
    assert exc.value.code == 1


def test_importer_startup_failfast_when_target_configured_but_exclude_pdf_enabled(monkeypatch) -> None:
    """目标库已配置但 WORKER_EXCLUDE_PDF=1 时属于配置冲突歧义，必须报错退出 exit(1)。"""
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "1")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", str(TARGET_LIB))
    with pytest.raises(SystemExit) as exc:
        importer.get_worker_target_config()
    assert exc.value.code == 1

def test_embedder_startup_failfast_when_missing_target_library_id(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.delenv("WORKER_TARGET_LIBRARY_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        embedder.get_worker_target_config()
    assert exc.value.code == 1


def test_embedder_startup_failfast_when_invalid_target_library_id(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", "invalid-uuid-xyz")
    with pytest.raises(SystemExit) as exc:
        embedder.get_worker_target_config()
    assert exc.value.code == 1


def test_embedder_startup_failfast_when_both_switches_missing(monkeypatch) -> None:
    """试用模式下两个开关同时漏配时，Embedder 必须报错退出 exit(1)。"""
    monkeypatch.delenv("WORKER_EXCLUDE_PDF", raising=False)
    monkeypatch.delenv("WORKER_TARGET_LIBRARY_ID", raising=False)
    with pytest.raises(SystemExit) as exc:
        embedder.get_worker_target_config()
    assert exc.value.code == 1


def test_embedder_startup_failfast_when_target_configured_but_exclude_pdf_enabled(monkeypatch) -> None:
    """目标库已配置但 WORKER_EXCLUDE_PDF=1 时属于配置冲突歧义，Embedder 必须报错退出 exit(1)。"""
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "1")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", str(TARGET_LIB))
    with pytest.raises(SystemExit) as exc:
        embedder.get_worker_target_config()
    assert exc.value.code == 1

def test_importer_trial_claims_only_target_library_pdf(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", str(TARGET_LIB))
    db = _MockDb()

    assert asyncio.run(importer._claim_jobs(db, "test-trial-importer", 5)) == []
    assert len(db.statements) == 1
    sql = db.statements[0]
    params = db.params_list[0]

    # 1. 验证目标库硬过滤与参数绑定
    assert "library_id = :target_library_id" in sql
    assert params["target_library_id"] == TARGET_LIB
    assert params["exclude_pdf"] is False

    # 2. 验证仅领取 PDF：必须包含 LIKE '%.pdf'，且不包含全表扫描退化
    assert "LOWER(file_name) LIKE '%.pdf'" in sql
    assert ":target_library_id IS NULL" not in sql


def test_importer_trial_ignores_docx_xlsx_in_same_target_library(monkeypatch) -> None:
    """
    同一目标库内上传 docx/xlsx 时：
    试用 Worker 因为限定了 LOWER(file_name) LIKE '%.pdf'，绝对不会申领该 docx/xlsx；
    而常驻 Worker（WORKER_EXCLUDE_PDF=1）因仅排除 PDF，会正常申领该 docx/xlsx。
    """
    # 试用 Worker 视角：SQL 包含 LIKE '%.pdf'，非 PDF 文件被排斥
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", str(TARGET_LIB))
    trial_db = _MockDb()
    asyncio.run(importer._claim_jobs(trial_db, "trial-worker", 5))
    trial_sql = trial_db.statements[0]
    assert "LOWER(file_name) LIKE '%.pdf'" in trial_sql

    # 常驻 Worker 视角：WORKER_EXCLUDE_PDF=1，无目标库绑定，只排斥 PDF，接管 docx/xlsx
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "1")
    monkeypatch.delenv("WORKER_TARGET_LIBRARY_ID", raising=False)
    baseline_db = _MockDb()
    asyncio.run(importer._claim_jobs(baseline_db, "baseline-worker", 5))
    baseline_sql = baseline_db.statements[0]
    assert "LOWER(file_name) NOT LIKE '%.pdf'" in baseline_sql
    assert "target_library_id" not in baseline_db.params_list[0]


def test_embedder_trial_claims_only_target_library_pdf(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", str(TARGET_LIB))
    db = _MockDb()

    assert asyncio.run(embedder._claim_jobs(db, "test-trial-embedder", 5)) == []
    assert len(db.statements) == 1
    sql = db.statements[0]
    params = db.params_list[0]

    assert "j.library_id = :target_library_id" in sql
    assert params["target_library_id"] == TARGET_LIB
    assert "LIKE '%.pdf'" in sql
    assert "NOT LIKE '%.pdf'" not in sql


def test_embedder_trial_ignores_docx_xlsx_in_same_target_library(monkeypatch) -> None:
    """
    在同一目标库内存在 docx/xlsx 的 embedding_jobs 时，
    试用 Embedder 只领 PDF，常驻 Embedder 接管 docx/xlsx。
    """
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", str(TARGET_LIB))
    trial_db = _MockDb()
    asyncio.run(embedder._claim_jobs(trial_db, "trial-embedder", 5))
    trial_sql = trial_db.statements[0]
    assert "j.library_id = :target_library_id" in trial_sql
    assert "LIKE '%.pdf'" in trial_sql

    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "1")
    monkeypatch.delenv("WORKER_TARGET_LIBRARY_ID", raising=False)
    baseline_db = _MockDb()
    asyncio.run(embedder._claim_jobs(baseline_db, "baseline-embedder", 5))
    baseline_sql = baseline_db.statements[0]
    assert "NOT LIKE '%.pdf'" in baseline_sql
    assert "target_library_id" not in baseline_db.params_list[0]


def test_historical_task_isolation_across_workers(monkeypatch) -> None:
    """
    历史测试库 c5d69a03 积压的 338 个 PDF 任务隔离性验证：
    1. 常驻 Worker 设置 WORKER_EXCLUDE_PDF=1，因排除 PDF 而绝不申领；
    2. 试用 Worker 设置 WORKER_TARGET_LIBRARY_ID=2ed27105，因限定库 UUID 而绝不申领；
    两端均无法碰触历史积压任务。
    """
    # 1. 试用 Worker 不触碰历史库
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", str(TARGET_LIB))
    trial_db = _MockDb()
    asyncio.run(importer._claim_jobs(trial_db, "trial-importer", 5))
    assert trial_db.params_list[0]["target_library_id"] == TARGET_LIB
    assert trial_db.params_list[0]["target_library_id"] != HISTORICAL_LIB

    # 2. 常驻 Worker 排除 PDF
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "1")
    monkeypatch.delenv("WORKER_TARGET_LIBRARY_ID", raising=False)
    baseline_db = _MockDb()
    asyncio.run(importer._claim_jobs(baseline_db, "baseline-importer", 5))
    assert baseline_db.params_list[0]["exclude_pdf"] is True
    assert "LOWER(file_name) NOT LIKE '%.pdf'" in baseline_db.statements[0]


def test_stale_job_reset_strictly_scoped_to_target_library(monkeypatch) -> None:
    """
    测试超时重置仅针对 target_library_id 发生，消除全库副作用。
    """
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", str(TARGET_LIB))

    importer_db = _MockDb()
    asyncio.run(importer._reset_stale_jobs(importer_db))
    for stmt in importer_db.statements:
        assert "AND library_id = :target_library_id" in stmt
    for params in importer_db.params_list:
        assert params["target_library_id"] == TARGET_LIB

    embedder_db = _MockDb()
    asyncio.run(embedder._reset_stale_jobs(embedder_db))
    for stmt in embedder_db.statements:
        assert "AND library_id = :target_library_id" in stmt
    for params in embedder_db.params_list:
        assert params["target_library_id"] == TARGET_LIB


def test_importer_trial_isolates_historical_tasks_by_created_at(monkeypatch) -> None:
    """当配置了 WORKER_TASK_MIN_CREATED_AT 时，仅申领和重置指定时间戳之后的新任务，隔离历史积压。"""
    cutoff = "2026-09-24T00:00:00Z"
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", str(HISTORICAL_LIB))
    monkeypatch.setenv("WORKER_TASK_MIN_CREATED_AT", cutoff)

    importer_db = _MockDb()
    asyncio.run(importer._claim_jobs(importer_db, "trial-importer", 5))
    assert "AND created_at >= :min_created_at" in importer_db.statements[0]
    assert importer_db.params_list[0]["min_created_at"] is not None

    reset_db = _MockDb()
    asyncio.run(importer._reset_stale_jobs(reset_db))
    for stmt in reset_db.statements:
        assert "AND created_at >= :min_created_at" in stmt


def test_embedder_trial_isolates_historical_tasks_by_created_at(monkeypatch) -> None:
    """Embedder 在配置 WORKER_TASK_MIN_CREATED_AT 时同样追加时间戳过滤。"""
    cutoff = "2026-09-24T00:00:00Z"
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", str(HISTORICAL_LIB))
    monkeypatch.setenv("WORKER_TASK_MIN_CREATED_AT", cutoff)

    embedder_db = _MockDb()
    asyncio.run(embedder._claim_jobs(embedder_db, "trial-embedder", 5))
    assert "AND j.created_at >= :min_created_at" in embedder_db.statements[0]
    assert embedder_db.params_list[0]["min_created_at"] is not None

    reset_db = _MockDb()
    asyncio.run(embedder._reset_stale_jobs(reset_db))
    for stmt in reset_db.statements:
        assert "AND created_at >= :min_created_at" in stmt


def test_worker_fails_fast_on_invalid_min_created_at(monkeypatch) -> None:
    """非法的时间戳格式必须 fail-fast 退出 exit(1)。"""
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "0")
    monkeypatch.setenv("WORKER_TARGET_LIBRARY_ID", str(TARGET_LIB))
    monkeypatch.setenv("WORKER_TASK_MIN_CREATED_AT", "not-a-valid-timestamp")
    with pytest.raises(SystemExit) as exc1:
        importer.get_worker_target_config()
    assert exc1.value.code == 1

    with pytest.raises(SystemExit) as exc2:
        embedder.get_worker_target_config()
    assert exc2.value.code == 1
