"""本地 AI 服务连通性检查：Embedding / Rerank / OCR 一键自检。

复用项目现有能力（app.services.embedding / rerank / ocr），针对**本地**部署做连通性 +
契约校验，逐项打印耗时；任一失败以非零退出码结束，便于部署后 CI / 运维一键确认。

安全/默认行为：
  - 默认**不访问任何云地址**：解析到的 URL 命中已知云服务商（DashScope/阿里云/OpenAI/
    Cohere/Jina/AWS/GCP/Azure 等）时直接判失败并跳过请求，除非显式传 --allow-cloud。
  - **绝不打印 API Key**（仅显示是否携带鉴权头）。

用法：
  python scripts/check_local_ai_services.py                 # 用 .env 里的地址（云地址会被拒）
  python scripts/check_local_ai_services.py --embedding-url http://host:8111/v1/embeddings
  python scripts/check_local_ai_services.py --skip-rerank   # 仅查 embedding + OCR
  python scripts/check_local_ai_services.py --allow-cloud   # 明确允许访问云（备用）
"""
from __future__ import annotations

import argparse
import asyncio
import math
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import settings  # noqa: E402
from app.services import embedding, ocr  # noqa: E402
from app.services import rerank as rerank_svc  # noqa: E402

# 已知云服务商主机片段——命中即默认拒绝（保证“默认不访问云地址”）。
CLOUD_MARKERS = (
    "aliyuncs.com", "dashscope", "openai.com", "cohere.ai", "api.cohere",
    "jina.ai", "amazonaws.com", "googleapis.com", "azure.com", "anthropic.com",
)
EXPECTED_DIM = 1024


@dataclass
class CheckResult:
    name: str
    ok: bool
    elapsed_ms: float
    detail: str


def is_cloud_host(url: str) -> bool:
    """URL 是否指向已知云服务商（按主机名片段匹配，大小写不敏感）。"""
    host = (urlparse(url).hostname or url).lower()
    return any(m in host for m in CLOUD_MARKERS)


def redact(text: str, *secrets: str) -> str:
    """统一脱敏：把文本里出现的任一非空 secret（API Key）替换为 ***，避免随异常打印泄漏。"""
    out = text
    for s in secrets:
        if s:
            out = out.replace(s, "***")
    return out


async def check_embedding(*, base_url: str, model: str, dim: int, api_key: str, allow_cloud: bool) -> CheckResult:
    t0 = time.perf_counter()
    if not base_url:
        return CheckResult("embedding", False, 0.0, "未配置 base_url（请设 EMBEDDING_BASE_URL 为本地地址）")
    if is_cloud_host(base_url) and not allow_cloud:
        return CheckResult("embedding", False, 0.0, f"拒绝云地址 host={urlparse(base_url).hostname}（如确需用 --allow-cloud）")
    # 非云地址绝不携带 settings 里的 Key——本地服务无需鉴权，也避免把云 Key 发到本地端点。
    # 注意用空串而非 None：embed_texts 里 api_key=None 会回退 settings.embedding_api_key（反而会泄漏）；
    # 空串为 falsy → 不发 Authorization 头。
    eff_key = api_key if is_cloud_host(base_url) else ""
    try:
        vectors = await embedding.embed_texts(
            ["本地服务连通性检查 1", "本地服务连通性检查 2"],
            model=model, base_url=base_url, api_key=eff_key,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        n = len(vectors)
        d = len(vectors[0]) if n else 0
        ok = n == 2 and d == dim == EXPECTED_DIM
        detail = f"model={model} 返回向量数={n} 维度={d}（期望 {dim}）"
        if d != EXPECTED_DIM:
            detail += f" — 维度非 {EXPECTED_DIM}"
        return CheckResult("embedding", ok, elapsed, detail)
    except Exception as exc:  # noqa: BLE001
        msg = redact(str(exc), api_key, settings.embedding_api_key)[:200]
        return CheckResult("embedding", False, (time.perf_counter() - t0) * 1000, msg)


def _valid_ranking(results, n_docs: int) -> tuple[bool, str]:
    """校验 rerank 结果结构与排序合法性：[(int idx, float score), ...]。"""
    if not isinstance(results, list) or not results:
        return False, "结果为空或非列表"
    seen = set()
    prev = None
    for item in results:
        if not (isinstance(item, tuple) and len(item) == 2):
            return False, "结果项结构非 (index, score)"
        idx, score = item
        if not isinstance(idx, int) or not (0 <= idx < n_docs):
            return False, f"index 越界或非法: {idx}"
        if idx in seen:
            return False, f"index 重复: {idx}"
        seen.add(idx)
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            return False, "score is not finite"
        if prev is not None and score > prev:
            return False, "分数未按降序排列"
        prev = score
    return True, f"返回 {len(results)} 条、index 合法、按分降序"


async def check_rerank(*, base_url: str, model: str, api_key: str, allow_cloud: bool) -> CheckResult:
    t0 = time.perf_counter()
    if not (base_url and model):
        return CheckResult("rerank", False, 0.0, "未配置（设 RERANK_BASE_URL / RERANK_MODEL，或 --skip-rerank）")
    if is_cloud_host(base_url) and not allow_cloud:
        return CheckResult("rerank", False, 0.0, f"拒绝云地址 host={urlparse(base_url).hostname}（如确需用 --allow-cloud）")
    # 非云地址绝不携带 settings Key（空串 → 不发 Authorization 头；None 会回退 settings 反而泄漏）。
    # provider：本地强制 standard；云地址（已 --allow-cloud）按配置 RERANK_PROVIDER，
    # 这样 DashScope 备用检查能真正发 DashScope 的 input/parameters 格式。
    cloud = is_cloud_host(base_url)
    eff_key = api_key if cloud else ""
    eff_provider = settings.rerank_provider or "standard"
    docs = ["猫是一种宠物。", "向量数据库用于相似度检索。", "今天的天气很好。"]
    try:
        results = await rerank_svc.rerank(
            "什么是向量数据库", docs, top_n=len(docs),
            model=model, base_url=base_url, api_key=eff_key, provider=eff_provider,
        )
        elapsed = (time.perf_counter() - t0) * 1000
        ok, detail = _valid_ranking(results, len(docs))
        return CheckResult("rerank", ok, elapsed, detail)
    except Exception as exc:  # noqa: BLE001
        msg = redact(str(exc), api_key, settings.rerank_api_key, settings.embedding_api_key)[:200]
        return CheckResult("rerank", False, (time.perf_counter() - t0) * 1000, msg)


def check_ocr() -> CheckResult:
    t0 = time.perf_counter()
    if not ocr.is_available():
        return CheckResult("ocr", False, 0.0, 'RapidOCR 未安装（pip install -e ".[ocr]"）')
    try:
        ocr._get_engine()  # 本地初始化引擎（首次构造较重）
        return CheckResult("ocr", True, (time.perf_counter() - t0) * 1000, "RapidOCR 本地初始化成功")
    except Exception as exc:  # noqa: BLE001
        return CheckResult("ocr", False, (time.perf_counter() - t0) * 1000, str(exc)[:200])


async def run_checks(args) -> list[CheckResult]:
    results: list[CheckResult] = [
        await check_embedding(
            base_url=args.embedding_url, model=args.embedding_model,
            dim=args.dim, api_key=settings.embedding_api_key, allow_cloud=args.allow_cloud,
        ),
    ]
    if not args.skip_rerank:
        rr_key = settings.rerank_api_key or settings.embedding_api_key
        results.append(await check_rerank(
            base_url=args.rerank_url, model=args.rerank_model,
            api_key=rr_key, allow_cloud=args.allow_cloud,
        ))
    if not args.skip_ocr:
        results.append(check_ocr())
    return results


def _parse_args(argv=None):
    p = argparse.ArgumentParser(description="本地 AI 服务连通性检查（默认不访问云地址）")
    p.add_argument("--embedding-url", default=settings.embedding_base_url)
    p.add_argument("--embedding-model", default=settings.embedding_model)
    p.add_argument("--dim", type=int, default=settings.embedding_dim)
    p.add_argument("--rerank-url", default=settings.rerank_base_url)
    p.add_argument("--rerank-model", default=settings.rerank_model)
    p.add_argument("--skip-rerank", action="store_true", help="跳过 rerank 检查")
    p.add_argument("--skip-ocr", action="store_true", help="跳过 OCR 检查")
    p.add_argument("--allow-cloud", action="store_true", help="允许访问云地址（默认拒绝）")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = _parse_args(argv)
    results = asyncio.run(run_checks(args))
    print("==== 本地 AI 服务检查 ====")
    for r in results:
        flag = "OK  " if r.ok else "FAIL"
        print(f"[{flag}] {r.name:10} {r.elapsed_ms:7.1f}ms  {r.detail}")
    failed = [r.name for r in results if not r.ok]
    if failed:
        print(f"\n失败项: {', '.join(failed)}")
        return 1
    print("\n全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
