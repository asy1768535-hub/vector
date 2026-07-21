"""Query Rewrite 第一版：规范化 + 同义词/简称扩展（不接 LLM / Agent / Hybrid）。

目标：提升 Dense 检索在「简称 / 同义词 / 条款号 / 口语问法」下的召回。
做法纯规则 + 配置词典，零外部依赖、可离线、可单测：

  normalize_query(q)            表面形式规范化（全角→半角、去多空格、标点/书名号、条款号/文号）
  expand_query(q, synonym_map)  基于词典做同义词/简称替换，返回 ≤max 个 query（必含原始 q）
  load_synonyms(path)           读取并缓存 JSON 词典（缺文件/格式错 → 空字典，不阻断检索）

检索侧（app/services/retrieval.py）在 settings.query_rewrite_enabled=True 时调用本模块，
对多个 query 分别 embedding + 召回后按 chunk_id 合并去重；关闭时检索逻辑完全不变。
"""
from __future__ import annotations

import json
import logging
import re

from app.config import settings

log = logging.getLogger(__name__)


# ── 标点 / 标记规范化 ──────────────────────────────────────────────────────
# 书名号、引号、各式括号统一成 dense 召回友好的表面形式（书名号/引号去掉，
# 中文方/六角/全角方括号统一成半角 []）。全角 ASCII（含 （） 数字字母）另由
# _fullwidth_to_halfwidth 处理，不放这里。
_PUNCT_MAP = {
    "《": "", "》": "", "〈": "", "〉": "",
    "「": "", "」": "", "『": "", "』": "",
    "【": "[", "】": "]", "〔": "[", "〕": "]", "［": "[", "］": "]",
}

# 第 N 条/款/项/号/章/节：去掉「第」与序号、序号与单位之间的多余空格
_CLAUSE_RE = re.compile(r"第\s*([0-9一二三四五六七八九十百千零两]+)\s*(条|款|项|号|章|节)")
# 文号方括号内去空格：[ 2020 ] → [2020]
_DOCNO_BRACKET_RE = re.compile(r"\[\s*(\d+)\s*\]")
# 文号尾「N 号」去空格：5 号 → 5号
_DOCNO_TAIL_RE = re.compile(r"(\d+)\s*号")
_WS_RE = re.compile(r"\s+")


def _fullwidth_to_halfwidth(s: str) -> str:
    """全角 → 半角：全角空格(U+3000)→普通空格；全角 ASCII 区(U+FF01–FF5E)→对应半角。"""
    out: list[str] = []
    for ch in s:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def normalize_query(query: str) -> str:
    """规范化 query 的表面形式，不改变语义。

    步骤：全角→半角 → 标点/书名号/括号规范化 → 条款号/文号格式规范化 → 去多余空格。
    用于：召回前统一表面形式，并作为同义词扩展的基底。
    """
    if not query:
        return ""
    s = _fullwidth_to_halfwidth(query)
    s = "".join(_PUNCT_MAP.get(ch, ch) for ch in s)
    s = _CLAUSE_RE.sub(lambda m: f"第{m.group(1)}{m.group(2)}", s)
    s = _DOCNO_BRACKET_RE.sub(lambda m: f"[{m.group(1)}]", s)
    s = _DOCNO_TAIL_RE.sub(lambda m: f"{m.group(1)}号", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def expand_query(
    query: str,
    synonym_map: dict[str, list[str]] | None,
    *,
    max_queries: int | None = None,
) -> list[str]:
    """同义词/简称扩展：返回最多 max_queries 个 query。

    保证：
      - 结果第一项恒为**原始 query**（不破坏调用方输入，便于「原 query 必保留」）；
      - 若规范化结果与原始不同，追加规范化形式；
      - 在规范化基底上做词典替换（term 命中即把 term 换成每个同义词/简称）；
      - 去重、按发现序、截断到 max_queries（默认取 settings，落到 3~4 量级）。
    """
    cap = max_queries if max_queries is not None else settings.query_rewrite_max_queries
    cap = max(1, cap)

    original = query if query is not None else ""
    out: list[str] = [original]                 # 原始 query 必含
    if len(out) >= cap:
        return out[:cap]

    base = normalize_query(query)
    if base and base not in out:
        out.append(base)
        if len(out) >= cap:
            return out[:cap]

    pivot = base or original                     # 在规范化基底上做替换
    for term, syns in (synonym_map or {}).items():
        if not term or term not in pivot:
            continue
        for syn in syns or []:
            cand = pivot.replace(term, syn)
            if cand and cand not in out:
                out.append(cand)
                if len(out) >= cap:
                    return out[:cap]
    return out[:cap]


# ── 词典加载（按路径缓存；缺文件/格式错不阻断检索）──────────────────────────
_SYN_CACHE: dict[str, dict[str, list[str]]] = {}


def _coerce_synonyms(data: object, path: str) -> dict[str, list[str]]:
    """把 JSON 容错成 dict[str, list[str]]：值是字符串视为单元素列表，非字符串项丢弃。"""
    if not isinstance(data, dict):
        log.warning("query_synonyms: %s 顶层不是对象，忽略", path)
        return {}
    clean: dict[str, list[str]] = {}
    for term, syns in data.items():
        if not isinstance(term, str):
            continue
        if term.startswith("_"):        # 下划线开头视为注释/元信息（如 "_comment"），跳过
            continue
        if isinstance(syns, str):
            syns = [syns]
        if not isinstance(syns, list):
            continue
        vals = [s for s in syns if isinstance(s, str) and s]
        if vals:
            clean[term] = vals
    return clean


def load_synonyms(path: str | None = None, *, use_cache: bool = True) -> dict[str, list[str]]:
    """读取同义词词典。缺文件 / 解析失败 → 返回空字典（检索退化为仅规范化，不报错）。"""
    p = path or settings.query_rewrite_synonyms_path
    if use_cache and p in _SYN_CACHE:
        return _SYN_CACHE[p]
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        result = _coerce_synonyms(data, p)
    except FileNotFoundError:
        log.info("query_synonyms: 词典文件不存在(%s)，Query Rewrite 仅做规范化", p)
        result = {}
    except (OSError, ValueError) as exc:
        log.warning("query_synonyms: 读取/解析失败(%s): %s，按空词典处理", p, exc)
        result = {}
    if use_cache:
        _SYN_CACHE[p] = result
    return result
