"""可复算的 PDF 识别链路逐阶段耗时与实际导入链路测量脚本。

明确区分两类测量：
1. 独立分段基准测试（Component Micro-benchmarks）：
   - preflight: 独立执行 pdf_preflight.preflight_pdf
   - page_render: 独立执行 pdf_extract._render_page_png
   - rapidocr: 独立执行 ocr_service.ocr_image_blocks
   - quality_inspector: 独立执行 pdf_quality_inspector.inspect_local_pdf_candidate
2. 实际导入链路执行（Integrated Pipeline Execution）：
   - build_candidate: 完整执行 pdf_extract.build_pdf_source（包含渲染、OCR、标准化、分块 chunking 与段落组装）
   - inspector_verdict: 质检规则判定与原因
   - cascade_final_selection: 级联最终路由
   - pipeline_local_overhead: 质检驳回时实际导入链路承受的本地候选额外耗时
3. MinerU 耗时与质量：
   - 严格标记为 "unverified"（未验证），严禁使用模拟耗时或未授权远端调用。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import statistics
from types import SimpleNamespace
from typing import Any

workspace_root = Path.cwd()
if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))

from app.config import settings
from app.services import (
    ocr as ocr_service,
    pdf_extract,
    pdf_preflight,
    pdf_quality_inspector,
    pdf_routing,
)


def profile_single_file(
    file_path: Path,
    category: str,
    sample_index: int,
) -> dict[str, Any]:
    file_bytes = file_path.read_bytes()
    sha256 = hashlib.sha256(file_bytes).hexdigest().lower()
    file_size = len(file_bytes)

    library = SimpleNamespace(
        slug="pdf_routing",
        chunk_size=800,
        chunk_overlap=80,
        ocr_enabled=True,
    )

    record: dict[str, Any] = {
        "sample_index": sample_index,
        "category": category,
        "file_name": file_path.name,
        "file_size_bytes": file_size,
        "sha256": sha256,
        "micro_benchmarks": {},
        "pipeline_execution": {},
        "mineru": {
            "latency_seconds": "unverified",
            "quality_status": "unverified",
            "notes": "本地未启动 MinerU 容器服务；按安全边界严禁调用远端/部署服务，亦严禁使用模拟值替代",
        },
        "errors": {},
    }

    # =========================================================================
    # 1. 独立分段基准测量 (Component Micro-benchmarks)
    # =========================================================================

    # 1.1 独立预检 preflight
    t0 = time.perf_counter()
    pref_res = None
    try:
        min_chars = getattr(settings, "pdf_ocr_min_text_chars", 128)
        pref_res = pdf_preflight.preflight_pdf(file_bytes, min_text_chars=min_chars)
        t_pref = time.perf_counter() - t0
        record["micro_benchmarks"]["preflight_seconds"] = round(t_pref, 4)
    except Exception as exc:
        record["micro_benchmarks"]["preflight_seconds"] = round(time.perf_counter() - t0, 4)
        record["errors"]["preflight"] = f"{type(exc).__name__}: {exc}"

    # 1.2 独立页面渲染 page_render
    t0 = time.perf_counter()
    png_bytes = None
    try:
        render_dpi = getattr(settings, "pdf_ocr_render_dpi", 150)
        png_bytes = pdf_extract._render_page_png(file_bytes, page_index=0, dpi=render_dpi)
        t_rend = time.perf_counter() - t0
        record["micro_benchmarks"]["render_seconds"] = round(t_rend, 4)
        record["micro_benchmarks"]["rendered_png_bytes"] = len(png_bytes) if png_bytes else 0
    except Exception as exc:
        record["micro_benchmarks"]["render_seconds"] = round(time.perf_counter() - t0, 4)
        record["errors"]["render"] = f"{type(exc).__name__}: {exc}"

    # 1.3 独立 RapidOCR 推理
    t0 = time.perf_counter()
    ocr_raw = None
    if png_bytes:
        try:
            ocr_raw = ocr_service.ocr_image_blocks(png_bytes)
            t_ocr = time.perf_counter() - t0
            record["micro_benchmarks"]["rapidocr_seconds"] = round(t_ocr, 4)
            ocr_text, ocr_blocks = pdf_extract.ocr_result_text_and_blocks(ocr_raw)
            confidences = [
                float(b["confidence"])
                for b in ocr_blocks
                if isinstance(b.get("confidence"), (int, float))
            ]
            record["micro_benchmarks"]["ocr_summary"] = {
                "text_length": len(ocr_text),
                "blocks_count": len(ocr_blocks),
                "median_confidence": round(float(statistics.median(confidences)), 4) if confidences else 0.0,
                "min_confidence": round(min(confidences), 4) if confidences else 0.0,
            }
        except Exception as exc:
            record["micro_benchmarks"]["rapidocr_seconds"] = round(time.perf_counter() - t0, 4)
            record["errors"]["rapidocr"] = f"{type(exc).__name__}: {exc}"
    else:
        record["micro_benchmarks"]["rapidocr_seconds"] = 0.0
        record["errors"]["rapidocr"] = "no_png_rendered"

    # =========================================================================
    # 2. 实际导入链路测量 (Integrated Pipeline Execution)
    # =========================================================================

    # 2.1 候选解析完整构建 build_pdf_source (涵盖渲染、OCR、分块、段落与元数据)
    t0 = time.perf_counter()
    candidate = None
    try:
        candidate = pdf_extract.build_pdf_source(
            file_bytes,
            chunk_size=library.chunk_size,
            chunk_overlap=library.chunk_overlap,
            ocr_enabled=True,
            ocr=ocr_service.ocr_image_blocks,
            min_text_chars=getattr(settings, "pdf_ocr_min_text_chars", 128),
            render_dpi=getattr(settings, "pdf_ocr_render_dpi", 150),
            max_ocr_pages=getattr(settings, "pdf_ocr_max_pages", 50),
            preflight_report=pref_res,
        )
        t_cand = time.perf_counter() - t0
        record["pipeline_execution"]["build_candidate_seconds"] = round(t_cand, 4)
        record["pipeline_execution"]["chunks_count"] = len(candidate.get("chunks", []))
    except Exception as exc:
        record["pipeline_execution"]["build_candidate_seconds"] = round(time.perf_counter() - t0, 4)
        record["errors"]["build_candidate"] = f"{type(exc).__name__}: {exc}"

    # 2.2 质量门禁检查 inspect_local_pdf_candidate
    t0 = time.perf_counter()
    verdict = None
    if candidate:
        try:
            verdict = pdf_quality_inspector.inspect_local_pdf_candidate(candidate)
            t_insp = time.perf_counter() - t0
            record["micro_benchmarks"]["inspector_seconds"] = round(t_insp, 4)
            record["pipeline_execution"]["inspector_verdict"] = verdict
        except Exception as exc:
            record["micro_benchmarks"]["inspector_seconds"] = round(time.perf_counter() - t0, 4)
            record["errors"]["inspector"] = f"{type(exc).__name__}: {exc}"
    else:
        record["micro_benchmarks"]["inspector_seconds"] = 0.0
        record["pipeline_execution"]["inspector_verdict"] = {
            "accept": False,
            "reason": "candidate_extraction_failed",
            "notes": record["errors"].get("build_candidate", "candidate_none"),
        }

    # 2.3 最终路由与实际导入链路额外本地开销计算
    # 在实际导入链路 (import_parsing.py) 中：
    # 如果 candidate 被 accept，则直接返回 candidate（本地快路耗时 = preflight + build_candidate + inspector）
    # 如果 candidate 被 reject，则必须回退调用 MinerU
    # 此时对被驳回文档而言，实际导入链路所遭受的额外无效等待 = build_candidate + inspector
    is_accepted = bool(verdict and verdict.get("accept"))
    final_selection = "native_or_rapidocr" if is_accepted else "mineru"
    build_time = record["pipeline_execution"].get("build_candidate_seconds", 0.0)
    insp_time = record["micro_benchmarks"].get("inspector_seconds", 0.0)

    record["pipeline_execution"]["cascade_final_selection"] = final_selection
    record["pipeline_execution"]["is_local_bypass"] = is_accepted
    record["pipeline_execution"]["rejected_local_overhead_seconds"] = (
        0.0 if is_accepted else round(build_time + insp_time, 4)
    )

    return record


def run_profiling(output_dir: Path) -> dict[str, Any]:
    print("=== 开始运行 PDF 识别链路可复算性能剖析 ===")
    manifest_path = Path("output/pdf-understanding-eval-20260918/m0-manifest.json")
    baseline_dir = Path(r"D:\Program Files (x86)\OmniDocBench_images_to_pdf")
    fallback_dir = Path(r"D:\word\pdf文档测试\OmniDocBench_images_to_pdf")
    if not baseline_dir.is_dir() and fallback_dir.is_dir():
        baseline_dir = fallback_dir

    baseline_manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {}
    )
    baseline_hashes = set()
    baseline_records = []

    print(f"\n1. 测量基准 10 份样本 (来源: {baseline_dir})")
    for s in baseline_manifest.get("samples", []):
        fpath = baseline_dir / s["file_name"]
        if fpath.is_file():
            baseline_hashes.add(s["sha256"].lower())
            rec = profile_single_file(fpath, category="baseline", sample_index=s["index"])
            baseline_records.append(rec)
            mb = rec["micro_benchmarks"]
            pe = rec["pipeline_execution"]
            print(
                f"  [{s['index']:02d}] {s['file_name'][:28]:<28} | "
                f"pref: {mb.get('preflight_seconds', 0):.3f}s | "
                f"rend: {mb.get('render_seconds', 0):.3f}s | "
                f"ocr: {mb.get('rapidocr_seconds', 0):.3f}s | "
                f"build_cand: {pe.get('build_candidate_seconds', 0):.3f}s | "
                f"route: {pe.get('cascade_final_selection')} ({pe['inspector_verdict'].get('reason')})"
            )

    # 2. 探索性 21 样本
    exp_root = Path(r"D:\word\pdf文档测试")
    exp_files = sorted(exp_root.rglob("*.pdf"))
    unique_exp_files = []
    for pf in exp_files:
        sha = hashlib.sha256(pf.read_bytes()).hexdigest().lower()
        if sha not in baseline_hashes:
            unique_exp_files.append((pf, sha))

    print(f"\n2. 测量探索性 21 份样本 (来源: {exp_root}, 去重后 {len(unique_exp_files)} 份)")
    exploratory_records = []
    for idx, (pf, sha) in enumerate(unique_exp_files, 1):
        rec = profile_single_file(pf, category="exploratory", sample_index=idx)
        exploratory_records.append(rec)
        mb = rec["micro_benchmarks"]
        pe = rec["pipeline_execution"]
        reason = pe["inspector_verdict"].get("reason", "-")
        print(
            f"  [{idx:02d}] {pf.name[:28]:<28} | "
            f"pref: {mb.get('preflight_seconds', 0):.3f}s | "
            f"rend: {mb.get('render_seconds', 0):.3f}s | "
            f"ocr: {mb.get('rapidocr_seconds', 0):.3f}s | "
            f"build_cand: {pe.get('build_candidate_seconds', 0):.3f}s | "
            f"route: {pe.get('cascade_final_selection')} ({reason})"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    report_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "meta": {
            "total_samples": len(baseline_records) + len(exploratory_records),
            "baseline_count": len(baseline_records),
            "exploratory_count": len(exploratory_records),
            "mineru_status": "unverified",
            "optimal_conclusion": "unverified",
        },
        "baseline_10_samples": baseline_records,
        "exploratory_21_samples": exploratory_records,
    }

    out_json = output_dir / "stage_timing_report.json"
    out_json.write_text(json.dumps(report_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[已导出 JSON 数据] {out_json}")

    # 生成 Markdown 报告
    md_lines = []
    md_lines.append("# PDF 识别链路独立分段与实际导入链路耗时测量报告\n")
    md_lines.append(f"> 生成时间: {report_data['timestamp']} | 测量样本总数: 31 份（基准 10 份 + 探索 21 份）\n")
    md_lines.append("## 说明与安全边界")
    md_lines.append("1. **独立分段测试（Micro-benchmarks）**：分别独立触发预检、渲染、RapidOCR 与质检函数，度量各单点算法组件的执行墙钟。")
    md_lines.append("2. **实际导入链路执行（Pipeline Execution）**：调用 `build_pdf_source` 完整候选构建（含文本归一化、语义切片 chunking 与段落元数据组装），度量实际导入链路中若开启级联门禁所承受的本地候选生成耗时及驳回开销。")
    md_lines.append("3. **MinerU 质量与耗时**：**严格标记为未验证**。当前环境未启动 MinerU 容器，禁止远程调用，亦严禁伪造模拟值。")
    md_lines.append("4. **“最优”结论状态**：**未验证**。在缺乏真实 MinerU 端到端时延、服务成本与结构化准确率对比数据前，不作任何系统性能“最优”或质量“最优”的定性推断。\n")

    def format_table(title: str, records: list[dict[str, Any]]) -> str:
        t_lines = [f"### {title}"]
        t_lines.append("| 序号 | 文件名 | 体积 (B) | 独立预检 (s) | 独立渲染 (s) | 独立 RapidOCR (s) | 独立质检 (s) | 实际候选构建 (s) | 门禁判定原因 | 最终路由 | 驳回额外耗时 (s) | MinerU 状态 |")
        t_lines.append("| :---: | :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- | :---: | :---: | :---: |")
        for r in records:
            mb = r["micro_benchmarks"]
            pe = r["pipeline_execution"]
            reason = pe["inspector_verdict"].get("reason", "-")
            overhead = pe.get("rejected_local_overhead_seconds", 0.0)
            t_lines.append(
                f"| {r['sample_index']:02d} | `{r['file_name']}` | {r['file_size_bytes']:,} | "
                f"{mb.get('preflight_seconds', 0):.4f} | {mb.get('render_seconds', 0):.4f} | "
                f"{mb.get('rapidocr_seconds', 0):.4f} | {mb.get('inspector_seconds', 0):.4f} | "
                f"{pe.get('build_candidate_seconds', 0):.4f} | `{reason}` | `{pe.get('cascade_final_selection')}` | "
                f"{overhead:.4f} | 未验证 |"
            )
        return "\n".join(t_lines)

    md_lines.append(format_table("1. 基准样本（Baseline，共 10 份）", baseline_records))
    md_lines.append("\n")
    md_lines.append(format_table("2. 探索性样本（Exploratory，来自 D:\\word\\pdf文档测试，共 21 份独立文件）", exploratory_records))

    out_md = output_dir / "stage_timing_report.md"
    out_md.write_text("\n".join(md_lines), encoding="utf-8")
    print(f"[已导出 Markdown 报告] {out_md}")

    return report_data


if __name__ == "__main__":
    out_dir = Path("output/pdf-cascade-eval-20260922")
    run_profiling(out_dir)
