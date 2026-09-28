"""Unit and regression tests for scripts/evaluate_pdf_cascade.py.

Verifies:
1. Remote exception handling in single run and metric tracking.
2. Partial round failures (cannot drop failed rounds from wall-clock, fails criteria).
3. All round failures (wall-clock retained, fails criteria).
4. Missing must-reject samples (Criterion 2 fails, no empty set auto-pass).
5. Must-reject selection enforcement (must strictly be 'mineru', not just != native_or_rapidocr).
6. Content assertions for Sample 2 (table amounts/fractions) and Sample 8 (3-column reading order).
7. Criterion 4 strict zero-tolerance efficiency evaluation and simulated mode rejection.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scripts.evaluate_pdf_cascade import (
    _evaluate_criteria,
    _run_single_case,
    _summarize_target,
    screen_exploratory_samples,
    verify_real_samples_quality_and_content,
    verify_sample_2_content,
    verify_sample_8_content,
)


# ---------------------------------------------------------------------------
# 1. 远端异常与单次运行计时测试
# ---------------------------------------------------------------------------

def test_run_single_case_captures_remote_exception_and_measures_duration():
    """远端 MinerU 抛出异常时，_run_single_case 必须捕获错误并保留正向测量耗时，不可崩溃或漏记。"""
    pdf_bytes = b"%PDF-1.4 test bytes"
    library = MagicMock(slug="pdf_routing", chunk_size=800, chunk_overlap=80, ocr_enabled=True)

    with (
        patch("app.services.pdf_preflight.preflight_pdf") as mock_pref,
        patch("scripts.evaluate_pdf_cascade.build_pdf_import_source", side_effect=ConnectionError("MinerU server down")),
    ):
        mock_pref.return_value = {
            "contract_version": "pdf-preflight-v1",
            "status": "complete",
            "total_pages": 1,
            "page_count_known": True,
            "page_limit_exceeded": False,
            "image_limit_exceeded": False,
            "has_mixed_content": False,
            "pages": [{"page": 1, "native_text_chars": 0, "embedded_image_count": 1, "has_visual_content": True, "low_text": True}],
            "unknown_reason": None,
        }
        res = _run_single_case(
            pdf_bytes=pdf_bytes,
            gate_enabled=True,
            library=library,
            mineru_mode="real",
        )

    assert res["error"] is not None
    assert "ConnectionError" in res["error"]
    assert res["parsed"] is None
    # 墙钟时间必须真实测得且严格大于 0
    assert "total_wall_clock_seconds" in res["timing"]
    assert res["timing"]["total_wall_clock_seconds"] > 0.0


def test_run_single_case_captures_preflight_exception():
    """预检阶段抛出异常时，必须同样被捕获并测得正向耗时。"""
    pdf_bytes = b"bad bytes"
    library = MagicMock()

    with patch("app.services.pdf_preflight.preflight_pdf", side_effect=ValueError("Corrupt header")):
        res = _run_single_case(
            pdf_bytes=pdf_bytes,
            gate_enabled=False,
            library=library,
        )

    assert res["error"] is not None
    assert "ValueError" in res["error"]
    assert res["timing"]["total_wall_clock_seconds"] > 0.0


# ---------------------------------------------------------------------------
# 2. 部分轮次失败与全部轮次失败耗时与准则判定测试
# ---------------------------------------------------------------------------

def test_partial_round_failure_retains_wall_clock_and_fails_criteria():
    """部分轮次失败：绝不从均值中剔除失败轮次以 0 秒计入，且必须导致准则 2 与准则 4 失败。"""
    target = {
        "id": "real_02",
        "name": "sample_02_math.pdf",
        "is_real": True,
        "sample_index": 2,
        "category": "table_math",
    }

    # 3 轮测试：轮次 1 成功 (0.6s)，轮次 2 远端异常 (0.3s)，轮次 3 成功 (0.6s)
    runs_off = [
        {"timing": {"total_wall_clock_seconds": 0.5}, "counts": {"mineru_calls": 1}, "routing": {"selection": "mineru"}, "error": None, "parsed": {"text": "ok"}},
        {"timing": {"total_wall_clock_seconds": 0.5}, "counts": {"mineru_calls": 1}, "routing": {"selection": "mineru"}, "error": None, "parsed": {"text": "ok"}},
        {"timing": {"total_wall_clock_seconds": 0.5}, "counts": {"mineru_calls": 1}, "routing": {"selection": "mineru"}, "error": None, "parsed": {"text": "ok"}},
    ]
    runs_on = [
        {"timing": {"total_wall_clock_seconds": 0.6}, "counts": {"mineru_calls": 1, "candidate_calls": 1}, "routing": {"selection": "mineru"}, "inspector": {"accept": False}, "error": None, "parsed": {"text": "ok"}},
        {"timing": {"total_wall_clock_seconds": 0.3}, "counts": {"mineru_calls": 0, "candidate_calls": 1}, "routing": {}, "inspector": {"accept": False}, "error": "RemoteApiError: 502", "parsed": None},
        {"timing": {"total_wall_clock_seconds": 0.6}, "counts": {"mineru_calls": 1, "candidate_calls": 1}, "routing": {"selection": "mineru"}, "inspector": {"accept": False}, "error": None, "parsed": {"text": "ok"}},
    ]

    summary = _summarize_target(target, runs_off, runs_on)

    assert summary["has_unexpected_error"] is True
    assert summary["status"] == "failed"
    assert summary["must_reject_passed"] is False
    assert any("第 2 轮抛出非预期错误" in v for v in summary["must_reject_violations"])

    # 验证失败轮次真实耗时纳入均值计算，绝不以 0 秒计入或剔除：(0.6 + 0.3 + 0.6) / 3 = 0.5s
    assert pytest.approx(summary["avg_wall_clock_on"], 0.001) == 0.5
    # 存在异常时正文清空，防止错误通过内容核验
    assert summary["normalized_text"] == ""

    # 评估准则判定
    eval_results = [summary]
    # 补足其他 must_reject 样本的正常结果
    for idx in (1, 5, 6, 8):
        eval_results.append({
            "id": f"real_{idx:02d}",
            "name": f"sample_{idx:02d}.pdf",
            "is_real": True,
            "sample_index": idx,
            "has_unexpected_error": False,
            "must_reject_passed": True,
            "must_reject_violations": [],
            "cascade_selection": "mineru",
            "avg_wall_clock_off": 0.5,
            "avg_wall_clock_on": 0.4,
            "mineru_calls_off": 1,
            "mineru_calls_on": 1,
        })

    criteria, decision = _evaluate_criteria(
        eval_results=eval_results,
        real_samples_status="verified",
        independent_30_pages_status="unverified",
        tests_passed=True,
        passed_count=190,
        mineru_mode="real",
    )

    # 准则 2 必须失败（Sample 2 部分轮次失败）
    assert criteria["criterion_2_must_reject_samples_no_erroneous_pass"]["passed"] is False
    # 准则 4 必须失败（存在非预期解析失败）
    assert criteria["criterion_4_wall_clock_and_mineru_call_reduction"]["passed"] is False
    assert "存在 1 个样本发生非预期解析失败" in criteria["criterion_4_wall_clock_and_mineru_call_reduction"]["details"]
    assert decision["gate_enabled"] is False


def test_all_rounds_failure_retains_wall_clock_and_fails_criteria():
    """全部轮次失败：耗时完整保留，准则 2 与 4 均判定失败。"""
    target = {
        "id": "real_08",
        "name": "sample_08_columns.pdf",
        "is_real": True,
        "sample_index": 8,
        "category": "three_columns",
    }
    runs_off = [
        {"timing": {"total_wall_clock_seconds": 0.4}, "counts": {"mineru_calls": 1}, "routing": {"selection": "mineru"}, "error": None, "parsed": {"text": "ok"}},
    ]
    runs_on = [
        {"timing": {"total_wall_clock_seconds": 0.25}, "counts": {"mineru_calls": 0}, "routing": {}, "inspector": {}, "error": "TimeoutError: remote timeout", "parsed": None},
        {"timing": {"total_wall_clock_seconds": 0.25}, "counts": {"mineru_calls": 0}, "routing": {}, "inspector": {}, "error": "TimeoutError: remote timeout", "parsed": None},
    ]

    summary = _summarize_target(target, runs_off, runs_on)
    assert summary["has_unexpected_error"] is True
    assert summary["must_reject_passed"] is False
    assert pytest.approx(summary["avg_wall_clock_on"], 0.001) == 0.25

    criteria, decision = _evaluate_criteria(
        eval_results=[summary],
        real_samples_status="verified",
        independent_30_pages_status="unverified",
        tests_passed=True,
        passed_count=190,
        mineru_mode="real",
    )
    assert criteria["criterion_2_must_reject_samples_no_erroneous_pass"]["passed"] is False
    assert criteria["criterion_4_wall_clock_and_mineru_call_reduction"]["passed"] is False


# ---------------------------------------------------------------------------
# 3. 缺失样本测试（无空集合自动通过）
# ---------------------------------------------------------------------------

def test_missing_must_reject_samples_fails_criterion_2():
    """若真实十样本未完整覆盖 Sample 1, 2, 5, 6, 8，准则 2 严禁自动通过。"""
    # 仅提供 Sample 1, 2, 5，缺少 6 和 8
    eval_results = []
    for idx in (1, 2, 5):
        eval_results.append({
            "id": f"real_{idx:02d}",
            "name": f"sample_{idx:02d}.pdf",
            "is_real": True,
            "sample_index": idx,
            "has_unexpected_error": False,
            "must_reject_passed": True,
            "must_reject_violations": [],
            "cascade_selection": "mineru",
            "avg_wall_clock_off": 0.5,
            "avg_wall_clock_on": 0.4,
            "mineru_calls_off": 1,
            "mineru_calls_on": 1,
        })

    criteria, _ = _evaluate_criteria(
        eval_results=eval_results,
        real_samples_status="verified",
        independent_30_pages_status="unverified",
        tests_passed=True,
        passed_count=190,
        mineru_mode="simulated",
    )

    assert criteria["criterion_2_must_reject_samples_no_erroneous_pass"]["passed"] is False
    notes = criteria["criterion_2_must_reject_samples_no_erroneous_pass"]["notes"]
    assert "真实硬门样本覆盖不全" in notes
    assert "缺失 [6, 8]" in notes


def test_real_samples_unverified_status_fails_criterion_1_and_2():
    """真实样本状态为 unverified 时，准则 1 和准则 2 均必须失败。"""
    criteria, _ = _evaluate_criteria(
        eval_results=[],
        real_samples_status="unverified",
        independent_30_pages_status="unverified",
        tests_passed=True,
        passed_count=190,
        mineru_mode="simulated",
    )
    assert criteria["criterion_1_real_and_30p_samples_available"]["passed"] is False
    assert criteria["criterion_2_must_reject_samples_no_erroneous_pass"]["passed"] is False


# ---------------------------------------------------------------------------
# 4. 必须升档样本选型严格性测试（仅判断 != native_or_rapidocr 不足以判通过）
# ---------------------------------------------------------------------------

def test_must_reject_selection_requires_explicit_mineru():
    """如果 Sample 1/2/5/6/8 的选型为 None、unknown 或其他非 mineru 值，准则 2 必须判失败。"""
    eval_results = []
    for idx in (1, 2, 5, 6, 8):
        eval_results.append({
            "id": f"real_{idx:02d}",
            "name": "sample_02_math.pdf" if idx == 2 else f"sample_{idx:02d}.pdf",
            "is_real": True,
            "sample_index": idx,
            "has_unexpected_error": False,
            "must_reject_passed": True,
            "must_reject_violations": [],
            # Sample 2 选型异常为 None（既不是 native_or_rapidocr 也不是 mineru）
            "cascade_selection": None if idx == 2 else "mineru",
            "avg_wall_clock_off": 0.5,
            "avg_wall_clock_on": 0.4,
            "mineru_calls_off": 1,
            "mineru_calls_on": 1,
        })

    criteria, _ = _evaluate_criteria(
        eval_results=eval_results,
        real_samples_status="verified",
        independent_30_pages_status="unverified",
        tests_passed=True,
        passed_count=190,
        mineru_mode="simulated",
    )

    assert criteria["criterion_2_must_reject_samples_no_erroneous_pass"]["passed"] is False
    assert any("sample_02_math.pdf" in b for b in criteria["criterion_2_must_reject_samples_no_erroneous_pass"]["erroneous_bypasses"])


# ---------------------------------------------------------------------------
# 5. 真实样本内容断言测试（Sample 2 表格数字与选项分式、Sample 8 三栏阅读顺序）
# ---------------------------------------------------------------------------

def test_verify_sample_2_content_passes_on_valid_benchmark():
    """Sample 2 基准文本断言全量通过。"""
    sample_text = (
        "Birthday Money | Birthday Money\n"
        "Value of Bill | Number of bills\n"
        "$5 | 5\n$10 | 3\n$20 | 2\n$50 | 1\n"
        "F $\\frac{5}{22}$\nH $\\frac{5}{11}$\n"
        "c $\\frac{5}{17}$\nD $\\frac{7}{17}$\n"
    )
    res = verify_sample_2_content(sample_text)
    assert res["verified"] is True
    assert all(res["checks"].values())


def test_verify_sample_2_content_fails_on_missing_or_corrupt_data():
    """Sample 2 缺失面额对应或选项分式时断言失败。"""
    # 缺少 C=5/17
    bad_text = (
        "$5 | 5\n$10 | 3\n$20 | 2\n$50 | 1\n"
        "H $\\frac{5}{11}$\nD $\\frac{7}{17}$\n"
    )
    res = verify_sample_2_content(bad_text)
    assert res["verified"] is False
    assert res["checks"]["fraction_c_5_17"] is False


def test_verify_sample_2_content_fails_on_misaligned_numbers_and_options():
    """Sample 2 错位反例：文本包含全部必要数字、面额和选项分式，但行与选项错位时严格判定失败。"""
    misaligned_text = (
        "PART 1 Multiple Choice\n"
        "1. Sancho picked up a handful of coins... What fraction were nickels?\n"
        "A $\\frac{2}{17}$\n"
        "B $\\frac{3}{17}$\n"
        "c $\\frac{7}{17}$\n"  # 错位：应为 5/17
        "D $\\frac{5}{17}$\n"  # 错位：应为 7/17
        "\n"
        "6. Birthday Money table:\n"
        "Birthday Money | Birthday Money\n"
        "Value of Bill | Number of bills\n"
        "$5 | 3\n"   # 错位：应为 5
        "$10 | 5\n"  # 错位：应为 3
        "$20 | 1\n"  # 错位：应为 2
        "$50 | 2\n"  # 错位：应为 1
        "F $\\frac{5}{11}$\n"  # 错位：应为 5/22
        "H $\\frac{5}{22}$\n"  # 错位：应为 5/11
        "G 3/11\n"
        "J 8/11\n"
    )
    res = verify_sample_2_content(misaligned_text)
    assert res["verified"] is False
    assert res["checks"]["bill_5_row_is_5"] is False
    assert res["checks"]["bill_10_row_is_3"] is False
    assert res["checks"]["bill_20_row_is_2"] is False
    assert res["checks"]["bill_50_row_is_1"] is False
    assert res["checks"]["option_h_is_5_11"] is False
    assert res["checks"]["option_c_is_5_17"] is False
    assert res["checks"]["option_d_is_7_17"] is False

def test_verify_sample_8_content_passes_on_ordered_columns():
    """Sample 8 五个锚点严格递增自然阅读顺序通过。"""
    ordered_text = (
        "The regulation provides that all other use, absent statutory...\n"
        "(1) All mineral deposits in the lands so patented...\n"
        "Termination of Preparation of the Environmental Impact Statement...\n"
        "SUPPLEMENTARY INFORMATION: Pursuant to the National...\n"
        "Agency Information Collection Activities; Pollution Prevention and Control...\n"
    )
    res = verify_sample_8_content(ordered_text)
    assert res["verified"] is True


def test_verify_sample_8_content_fails_on_out_of_order_anchors():
    """Sample 8 锚点顺序错乱（例如跨栏交错）时断言失败。"""
    disordered_text = (
        "Termination of Preparation of the Environmental Impact Statement...\n"
        "The regulation provides that all other use, absent statutory...\n"
        "(1) All mineral deposits in the lands so patented...\n"
        "SUPPLEMENTARY INFORMATION: Pursuant to the National...\n"
        "Agency Information Collection Activities; Pollution Prevention and Control...\n"
    )
    res = verify_sample_8_content(disordered_text)
    assert res["verified"] is False
    assert "未按自然阅读顺序排列" in res["details"]


def test_verify_real_samples_quality_and_content_marks_unverified_and_fails_on_error():
    """十样本质量核验表：不支持的模型能力标 unverified，模拟模式标 historical_benchmark，仅路由放行标 route_verified，发生错误样本标 failed。"""
    eval_results = [
        {"is_real": True, "sample_index": 1, "name": "01_flowchart.pdf", "normalized_text": "flowchart", "has_unexpected_error": False},
        {"is_real": True, "sample_index": 2, "name": "02_math.pdf", "normalized_text": "$5 | 5\n$10 | 3\n$20 | 2\n$50 | 1\nH 5/11\nc 5/17\nd 7/17", "has_unexpected_error": False},
        {"is_real": True, "sample_index": 5, "name": "05_pinyin.pdf", "normalized_text": "pinyin", "has_unexpected_error": False},
        {"is_real": True, "sample_index": 8, "name": "08_columns.pdf", "normalized_text": "text", "has_unexpected_error": True, "unexpected_errors": ["RemoteError"]},
        {"is_real": True, "sample_index": 9, "name": "09_presentation.pdf", "normalized_text": "clean text", "has_unexpected_error": False, "cascade_selection": "native_or_rapidocr"},
    ]

    # 1. 模拟模式：Sample 2 必须标为 historical_benchmark，不可标为本次识别正确 (verified: False)
    assertions_sim = verify_real_samples_quality_and_content(eval_results, mineru_mode="simulated")
    assert assertions_sim["sample_01"]["status"] == "unverified"
    assert assertions_sim["sample_02"]["status"] == "historical_benchmark"
    assert assertions_sim["sample_02"]["verified"] is False
    assert "历史基准" in assertions_sim["sample_02"]["notes"]
    assert assertions_sim["sample_05"]["status"] == "unverified"
    assert assertions_sim["sample_08"]["status"] == "failed"
    assert assertions_sim["sample_08"]["verified"] is False
    assert assertions_sim["sample_09"]["status"] == "route_verified"
    assert assertions_sim["sample_09"]["verified"] is False
    assert "仅证明路由决策" in assertions_sim["sample_09"]["notes"]

    # 2. 真实模式：Sample 2 可标为 verified: True
    assertions_real = verify_real_samples_quality_and_content(eval_results, mineru_mode="real")
    assert assertions_real["sample_02"]["status"] == "verified"
    assert assertions_real["sample_02"]["verified"] is True
    assert assertions_real["sample_09"]["status"] == "route_verified"


def test_screen_exploratory_samples_basic(tmp_path):
    """测试探索性样本离线筛查逻辑：自动去重旧样本、执行本地质检并统计耗时。"""
    import json
    exp_dir = tmp_path / "exp"
    exp_dir.mkdir()
    sample_file = exp_dir / "test_exploratory.pdf"
    sample_file.write_bytes(b"%PDF-1.4 dummy exploratory content")

    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps({"samples": [{"file_name": "old.pdf", "sha256": "fakehash"}]}), encoding="utf-8")

    with (
        patch("app.services.pdf_preflight.preflight_pdf", return_value={"status": "complete"}),
        patch("app.services.pdf_extract.build_pdf_source", return_value={"text": "local text"}),
        patch("app.services.pdf_quality_inspector.inspect_local_pdf_candidate", return_value={"accept": False, "rejection_reasons": ["low_ocr_confidence"]}),
    ):
        result = screen_exploratory_samples(exp_dir, manifest_file)

    assert result["status"] == "completed"
    assert result["summary"]["total_screened_count"] == 1
    assert result["summary"]["escalate_count"] == 1
    assert result["summary"]["bypass_count"] == 0
    assert result["summary"]["parse_error_count"] == 0
    assert len(result["samples"]) == 1
    assert result["samples"][0]["verdict"] == "escalate"
    assert result["samples"][0]["rejection_reasons"] == ["low_ocr_confidence"]

# ---------------------------------------------------------------------------
# 6. 准则 4 严格效率验收（模拟模式必败、零容差、非预期错误必败）
# ---------------------------------------------------------------------------

def test_criterion_4_fails_automatically_in_simulated_mode():
    """模拟 MinerU 模式下准则 4 必须自动失败，严禁用于端到端耗时与效率验收。"""
    eval_results = [{
        "is_real": False,
        "name": "synth_01.pdf",
        "has_unexpected_error": False,
        "avg_wall_clock_off": 1.0,
        "avg_wall_clock_on": 0.5,
        "mineru_calls_off": 1,
        "mineru_calls_on": 0,
    }]
    criteria, _ = _evaluate_criteria(
        eval_results=eval_results,
        real_samples_status="verified",
        independent_30_pages_status="verified",
        tests_passed=True,
        passed_count=190,
        mineru_mode="simulated",
    )
    assert criteria["criterion_4_wall_clock_and_mineru_call_reduction"]["passed"] is False
    assert "不可作为端到端效率与耗时验收证据" in criteria["criterion_4_wall_clock_and_mineru_call_reduction"]["details"]


def test_criterion_4_strict_zero_tolerance_in_real_mode():
    """真实模式下，总耗时超过 V1 或 P95 超过 1.10x 均严格判失败（无额外 5% 容差）。"""
    # Case A: total_time_on > total_time_off (1.001s > 1.000s)
    eval_results_a = [{
        "is_real": True,
        "sample_index": 9,
        "name": "sample_09.pdf",
        "has_unexpected_error": False,
        "avg_wall_clock_off": 1.000,
        "avg_wall_clock_on": 1.001,
        "mineru_calls_off": 1,
        "mineru_calls_on": 0,
    }]
    crit_a, _ = _evaluate_criteria(
        eval_results=eval_results_a,
        real_samples_status="verified",
        independent_30_pages_status="verified",
        tests_passed=True,
        passed_count=190,
        mineru_mode="real",
        must_reject_indices=set(),
    )
    assert crit_a["criterion_4_wall_clock_and_mineru_call_reduction"]["passed"] is False

    # Case B: P95 ratio > 1.10 (1.11x)
    eval_results_b = [{
        "is_real": True,
        "sample_index": 9,
        "name": "sample_09.pdf",
        "has_unexpected_error": False,
        "avg_wall_clock_off": 1.0,
        "avg_wall_clock_on": 0.5,
        "mineru_calls_off": 1,
        "mineru_calls_on": 0,
    }]
    # 模拟单个样本时 p95_ratio 为 on/off = 0.5 <= 1.10，通过；如果 on 为 1.15 则超标
    eval_results_b[0]["avg_wall_clock_on"] = 1.15
    crit_b, _ = _evaluate_criteria(
        eval_results=eval_results_b,
        real_samples_status="verified",
        independent_30_pages_status="verified",
        tests_passed=True,
        passed_count=190,
        mineru_mode="real",
        must_reject_indices=set(),
    )
    assert crit_b["criterion_4_wall_clock_and_mineru_call_reduction"]["passed"] is False
