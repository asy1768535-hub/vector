"""可复现的合成 PDF 质检夹具生成器与元数据清单。

覆盖 docs/roadmap/pdf-cascading-quality-gating-plan-20260922.zh-CN.md T4 要求的全部典型情形：
1. 原生文字 (native text)
2. 简单双语扫描件 (clean bilingual scan)
3. 表格扫描件 (table scan with digits & currency/percentage symbols)
4. 三栏扫描件 (three-column layout risk)
5. 低清模糊扫描件 (low confidence risk)
6. 无字扫描件 (blank image / incomplete OCR)
7. 多页扫描件 (multi-page scan)
8. 损坏 PDF (corrupted PDF bytes)
9. 超限 PDF (resource/page limit exceeded)
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFilter, ImageFont
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

FIXTURES_DIR = Path(__file__).resolve().parent


def _get_font(prefer_zh: bool = True, size: int = 24) -> ImageFont.ImageFont:
    """获取可用字体，优先考虑跨平台兼容性。"""
    candidates = []
    if prefer_zh:
        candidates.extend([
            "C:/Windows/Fonts/msyh.ttc",
            "C:/Windows/Fonts/simsun.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
            "/System/Library/Fonts/PingFang.ttc",
        ])
    candidates.extend([
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/calibri.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/Library/Fonts/Arial.ttf",
    ])
    for font_path in candidates:
        if os.path.exists(font_path):
            try:
                return ImageFont.truetype(font_path, size)
            except Exception:
                continue
    return ImageFont.load_default()


def generate_case_1_native() -> bytes:
    """Case 1: 纯数字原生矢量文字 PDF，无内嵌位图。"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    text = c.beginText()
    text.setTextOrigin(50, 720)
    text.setFont("Helvetica", 12)
    lines = [
        "Native text PDF document containing pure digital vector text without embedded raster images.",
        "This document is used to verify deterministic parser routing and preflight checks.",
        "Preflight analysis must detect sufficient native text characters on this page.",
        "The low_text flag must evaluate to False and has_visual_content must evaluate to False.",
        "Routing selects native_or_rapidocr with native_text_sufficient reason.",
        "Local candidate quality inspector will reject non-OCR candidate with incomplete_or_non_ocr.",
    ]
    for line in lines:
        text.textLine(line)
    c.drawText(text)
    c.showPage()
    c.save()
    return buf.getvalue()


def generate_case_2_clean_bilingual() -> bytes:
    """Case 2: 高质量简单双语单栏扫描件，文字清晰无数字、公式或分栏。"""
    font_zh = _get_font(prefer_zh=True, size=28)
    img = Image.new("RGB", (1200, 1600), color="white")
    draw = ImageDraw.Draw(img)
    lines = [
        "人工智能技术在当今数字化社会发挥着关键作用",
        "计算机视觉和自然语言处理是人工智能的核心领域",
        "深度学习算法通过多层神经网络实现复杂的特征学习",
        "知识库系统通过结构化数据组织实现精确的知识检索",
        "现代软件架构强调模块化和解耦设计以提升系统健壮性",
        "Software engineering emphasizes modularity and decoupled architecture",
        "Natural language processing enables computers to understand human language",
    ]
    y = 120
    for line in lines:
        draw.text((100, y), line, fill="black", font=font_zh)
        y += 85
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


def generate_case_3_table_scan() -> bytes:
    """Case 3: 包含表格、阿拉伯数字和结构化货币/百分比符号的扫描件。"""
    font_zh = _get_font(prefer_zh=True, size=24)
    img = Image.new("RGB", (1200, 1000), color="white")
    draw = ImageDraw.Draw(img)
    lines = [
        "财务统计报表和季度销售数据分析",
        "产品分类 季度收入 增长比例 市场份额",
        "电子设备 收入总额 占比 百分之五十",
        "办公用品 基础单价 浮动 收益统计表",
        "Widget Price $50 Growth 10% Share 25%",
        "Gadget Price $80 Growth 15% Share 40%",
    ]
    y = 100
    for line in lines:
        draw.text((100, y), line, fill="black", font=font_zh)
        y += 80
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


def generate_case_4_three_column_scan() -> bytes:
    """Case 4: 三栏水平横向分离的扫描件，触发多栏版面风险。"""
    font_en = _get_font(prefer_zh=False, size=30)
    img = Image.new("RGB", (1800, 1200), color="white")
    draw = ImageDraw.Draw(img)
    col1 = [
        "Software engineering architecture",
        "High cohesion and low coupling",
        "Modular design aids maintenance",
        "Service discovery and registry",
        "Idempotent API interface design",
    ]
    col2 = [
        "Natural language understanding",
        "Large language model reasoning",
        "Semantic vector representations",
        "Knowledge graph enhancement",
        "Accurate information retrieval",
    ]
    col3 = [
        "Continuous integration delivery",
        "Automated testing deployment",
        "Distributed performance tracing",
        "Root cause latency analysis",
        "Chaos engineering resilience",
    ]
    y_start = 150
    line_h = 80
    for i in range(len(col1)):
        y = y_start + i * line_h
        draw.text((100, y), col1[i], fill="black", font=font_en)
        draw.text((700, y), col2[i], fill="black", font=font_en)
        draw.text((1300, y), col3[i], fill="black", font=font_en)
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


def generate_case_5_low_confidence_scan() -> bytes:
    """Case 5: 低对比度加高斯模糊的低清扫描件，触发低置信度门控。"""
    font_zh = _get_font(prefer_zh=True, size=20)
    img = Image.new("RGB", (1200, 1400), color="white")
    draw = ImageDraw.Draw(img)
    lines = [
        "机器学习算法需要充足的高质量标注样本以获得较好表现",
        "深度神经网络模型具备极强的特征自动提取与表征能力",
        "自然语言处理技术可以帮助用户快速理解非结构化文本",
        "现代分布式数据库系统采用多副本机制保障数据的高可靠",
        "微服务架构设计提升了大规模软件系统的可维护性与扩展性",
        "云原生应用通过容器化编排实现快速部署与资源弹性伸缩",
        "分布式高并发服务需要考虑流量削峰与限流降级保护策略",
        "大规模集群自动化运维依赖标准化的监控告警度量体系",
    ]
    y = 100
    for line in lines:
        draw.text((100, y), line, fill=(130, 130, 130), font=font_zh)
        y += 60
    img = img.filter(ImageFilter.GaussianBlur(radius=1.8))
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


def generate_case_6_blank_scan() -> bytes:
    """Case 6: 纯白空白无字扫描件，触发无有效文本/块数不足。"""
    img = Image.new("RGB", (1200, 1600), color="white")
    buf = io.BytesIO()
    img.save(buf, format="PDF")
    return buf.getvalue()


def generate_case_7_multi_page_scan() -> bytes:
    """Case 7: 两页扫描件，超出单页级联后验路由适用范围。"""
    font_zh = _get_font(prefer_zh=True, size=28)

    def _make_page(content_lines: list[str]) -> Image.Image:
        p_img = Image.new("RGB", (1200, 1600), color="white")
        p_draw = ImageDraw.Draw(p_img)
        y = 100
        for l in content_lines:
            p_draw.text((100, y), l, fill="black", font=font_zh)
            y += 80
        return p_img

    p1_lines = [
        "第一页文档内容关于分布式系统基础原理与实践指南",
        "服务注册与服务发现机制保障微服务拓扑结构动态适应",
        "分布式一致性协议包括选举与日志复制状态机状态同步",
        "负载均衡算法涵盖轮询权重加权一致性哈希等多种实现",
        "健康检查与心跳监测机制及时剔除故障节点避免流量倾斜",
    ]
    p2_lines = [
        "第二页文档内容探讨高可用数据库系统设计核心挑战",
        "多副本复制与分布式共识保障跨数据中心容灾容损能力",
        "读写分离架构结合分布式缓存有效降低主节点并发压力",
        "自动化故障切换与恢复流程显著缩短系统平均恢复时间",
        "全局唯一分布式序列号生成策略满足高吞吐与有序需求",
    ]
    img1 = _make_page(p1_lines)
    img2 = _make_page(p2_lines)
    buf = io.BytesIO()
    img1.save(buf, format="PDF", save_all=True, append_images=[img2])
    return buf.getvalue()


def generate_case_8_corrupted() -> bytes:
    """Case 8: 损坏的畸形 PDF 字节流。"""
    return b"%PDF-1.4\n%corrupted non-conforming truncated binary stream that fails regular pdf parsing"


def generate_case_9_resource_limit() -> bytes:
    """Case 9: 6 页扫描 PDF（可用于测试超限场景）。"""
    font_zh = _get_font(prefer_zh=True, size=24)
    pages = []
    for i in range(6):
        img = Image.new("RGB", (800, 1000), color="white")
        draw = ImageDraw.Draw(img)
        draw.text((80, 100), f"多页扫描文档第 {i + 1} 页测试内容", fill="black", font=font_zh)
        pages.append(img)
    buf = io.BytesIO()
    pages[0].save(buf, format="PDF", save_all=True, append_images=pages[1:])
    return buf.getvalue()


CASES: list[dict[str, Any]] = [
    {
        "id": "01_native_text",
        "file_name": "01_native_text.pdf",
        "generator": generate_case_1_native,
        "category": "native_text",
        "expected_preflight_status": "complete",
        "expected_v1_selection": "native_or_rapidocr",
        "expected_cascade_selection": "native_or_rapidocr",
        "expected_inspector_verdict": {"accept": False, "reason": "incomplete_or_non_ocr"},
        "description": "原生矢量文字单页文档，不走单页扫描后验门控，直接走原生本地解析",
    },
    {
        "id": "02_clean_bilingual_scan",
        "file_name": "02_clean_bilingual_scan.pdf",
        "generator": generate_case_2_clean_bilingual,
        "category": "clean_bilingual_scan",
        "expected_preflight_status": "complete",
        "expected_v1_selection": "mineru",
        "expected_cascade_selection": "native_or_rapidocr",
        "expected_inspector_verdict": {"accept": True, "reason": "accepted"},
        "description": "清晰中英文扫描件，单栏且无数字/表格/公式，级联门控开启时被质检接受直通本地",
    },
    {
        "id": "03_table_scan",
        "file_name": "03_table_scan.pdf",
        "generator": generate_case_3_table_scan,
        "category": "table_scan",
        "expected_preflight_status": "complete",
        "expected_v1_selection": "mineru",
        "expected_cascade_selection": "mineru",
        "expected_inspector_verdict": {"accept": False, "reason": "structured_content_risk"},
        "description": "表格与数字金额扫描件，含阿拉伯数字和 $ % 符号，质检拦截并升档 MinerU",
    },
    {
        "id": "04_three_column_scan",
        "file_name": "04_three_column_scan.pdf",
        "generator": generate_case_4_three_column_scan,
        "category": "three_column_scan",
        "expected_preflight_status": "complete",
        "expected_v1_selection": "mineru",
        "expected_cascade_selection": "mineru",
        "expected_inspector_verdict": {"accept": False, "reason": "layout_risk"},
        "description": "三栏横向分离扫描件，存在同行水平间隙较大的文本块对，质检判定版面风险并升档",
    },
    {
        "id": "05_low_confidence_scan",
        "file_name": "05_low_confidence_scan.pdf",
        "generator": generate_case_5_low_confidence_scan,
        "category": "low_confidence_scan",
        "expected_preflight_status": "complete",
        "expected_v1_selection": "mineru",
        "expected_cascade_selection": "mineru",
        "expected_inspector_verdict": {"accept": False, "reason": "low_confidence"},
        "description": "模糊低对比度扫描件，OCR 置信度中位数或最低值未达阈值，质检拦截并升档",
    },
    {
        "id": "06_blank_scan",
        "file_name": "06_blank_scan.pdf",
        "generator": generate_case_6_blank_scan,
        "category": "blank_scan",
        "expected_preflight_status": "complete",
        "expected_v1_selection": "mineru",
        "expected_cascade_selection": "mineru",
        "expected_inspector_verdict": {"accept": False, "reason": "incomplete_or_non_ocr"},
        "description": "无文字空白扫描件，候选提取抛出空文本异常或块数不足，安全回落并升档 MinerU",
    },
    {
        "id": "07_multi_page_scan",
        "file_name": "07_multi_page_scan.pdf",
        "generator": generate_case_7_multi_page_scan,
        "category": "multi_page_scan",
        "expected_preflight_status": "complete",
        "expected_v1_selection": "mineru",
        "expected_cascade_selection": "mineru",
        "expected_inspector_verdict": {"accept": False, "reason": "incomplete_or_non_ocr"},
        "description": "两页扫描件，预检页数 > 1 不满足单页级联资格，维持 V1 选型直接调用 MinerU",
    },
    {
        "id": "08_corrupted",
        "file_name": "08_corrupted.pdf",
        "generator": generate_case_8_corrupted,
        "category": "corrupted_pdf",
        "expected_preflight_status": "unknown",
        "expected_v1_selection": "native_or_rapidocr",
        "expected_cascade_selection": "native_or_rapidocr",
        "expected_inspector_verdict": {"accept": False, "reason": "incomplete_or_non_ocr"},
        "description": "损坏的 PDF 字节流，预检返回 unknown 且不触发远端 MinerU",
    },
    {
        "id": "09_resource_limit",
        "file_name": "09_resource_limit.pdf",
        "generator": generate_case_9_resource_limit,
        "category": "resource_limit_scan",
        "expected_preflight_status": "complete",
        "expected_v1_selection": "mineru",
        "expected_cascade_selection": "mineru",
        "expected_inspector_verdict": {"accept": False, "reason": "incomplete_or_non_ocr"},
        "description": "6 页扫描件，多页直接维持 V1 MinerU 路由",
    },
]


def generate_all_fixtures(output_dir: Path | None = None) -> dict[str, Any]:
    """生成所有夹具文件并写入清单 manifest.json。"""
    out_dir = output_dir or FIXTURES_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    manifest_samples = []
    for item in CASES:
        pdf_bytes = item["generator"]()
        file_path = out_dir / item["file_name"]
        file_path.write_bytes(pdf_bytes)

        sha256 = hashlib.sha256(pdf_bytes).hexdigest()
        manifest_samples.append({
            "id": item["id"],
            "file_name": item["file_name"],
            "sha256": sha256,
            "bytes": len(pdf_bytes),
            "category": item["category"],
            "expected_preflight_status": item["expected_preflight_status"],
            "expected_v1_selection": item["expected_v1_selection"],
            "expected_cascade_selection": item["expected_cascade_selection"],
            "expected_inspector_verdict": item["expected_inspector_verdict"],
            "description": item["description"],
        })

    manifest = {
        "schema_version": "pdf-quality-synthetic-fixtures-v1",
        "created_at": "2026-09-22T00:00:00Z",
        "generator": "tests/fixtures/pdf_quality_cases/generate_fixtures.py",
        "samples": manifest_samples,
    }

    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    generated_manifest = generate_all_fixtures()
    print(f"Successfully generated {len(generated_manifest['samples'])} fixtures in {FIXTURES_DIR}")
