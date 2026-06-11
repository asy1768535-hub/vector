"""验证 Dify metadata_condition → Qdrant filter 的映射逻辑。"""
from __future__ import annotations

from app.schemas.dify import MetadataConditionGroup, MetadataConditionItem
from app.services.retrieval import _build_qdrant_filter


def test_eq_maps_to_match_value():
    group = MetadataConditionGroup(
        logical_operator="and",
        conditions=[MetadataConditionItem(name=["title"], comparison_operator="=", value="合同")],
    )
    out = _build_qdrant_filter(group)
    assert out == {"must": [{"key": "title", "match": {"value": "合同"}}]}


def test_contains_maps_to_text_match():
    group = MetadataConditionGroup(
        logical_operator="and",
        conditions=[MetadataConditionItem(name=["title"], comparison_operator="contains", value="合同")],
    )
    out = _build_qdrant_filter(group)
    assert out == {"must": [{"key": "title", "match": {"text": "合同"}}]}


def test_or_logical_operator_uses_should():
    group = MetadataConditionGroup(
        logical_operator="or",
        conditions=[
            MetadataConditionItem(name=["title"], comparison_operator="=", value="A"),
            MetadataConditionItem(name=["author"], comparison_operator="=", value="B"),
        ],
    )
    out = _build_qdrant_filter(group)
    assert "should" in out
    assert len(out["should"]) == 2


def test_unsupported_operator_is_dropped():
    group = MetadataConditionGroup(
        logical_operator="and",
        conditions=[
            MetadataConditionItem(name=["title"], comparison_operator="unknown-op", value="x"),
            MetadataConditionItem(name=["title"], comparison_operator="=", value="ok"),
        ],
    )
    out = _build_qdrant_filter(group)
    # 未支持算子被跳过，保留 = 那条
    assert len(out["must"]) == 1
    assert out["must"][0]["match"] == {"value": "ok"}


def test_none_returns_none():
    assert _build_qdrant_filter(None) is None
