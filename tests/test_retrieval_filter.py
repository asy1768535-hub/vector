"""验证 Dify metadata_condition → Qdrant filter 的映射逻辑。"""
from __future__ import annotations

import pytest

from app.schemas.dify import MetadataConditionGroup, MetadataConditionItem
from app.services.retrieval import FilterError, _build_qdrant_filter


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


def test_not_contains_maps_to_nested_must_not():
    group = MetadataConditionGroup(
        logical_operator="and",
        conditions=[MetadataConditionItem(name=["title"], comparison_operator="not contains", value="草稿")],
    )
    out = _build_qdrant_filter(group)
    assert out == {"must": [{"must_not": [{"key": "title", "match": {"text": "草稿"}}]}]}


def test_not_in_maps_to_match_except():
    group = MetadataConditionGroup(
        logical_operator="and",
        conditions=[MetadataConditionItem(name=["status"], comparison_operator="not in", value=["a", "b"])],
    )
    out = _build_qdrant_filter(group)
    assert out == {"must": [{"key": "status", "match": {"except": ["a", "b"]}}]}


def test_in_maps_to_match_any():
    group = MetadataConditionGroup(
        logical_operator="and",
        conditions=[MetadataConditionItem(name=["status"], comparison_operator="in", value=["a", "b"])],
    )
    out = _build_qdrant_filter(group)
    assert out == {"must": [{"key": "status", "match": {"any": ["a", "b"]}}]}


def test_unsupported_operator_raises():
    """#8：未支持算子不再被静默丢弃，而是抛 FilterError（上层转 422）。"""
    group = MetadataConditionGroup(
        logical_operator="and",
        conditions=[MetadataConditionItem(name=["title"], comparison_operator="unknown-op", value="x")],
    )
    with pytest.raises(FilterError):
        _build_qdrant_filter(group)


def test_in_with_non_list_raises():
    group = MetadataConditionGroup(
        logical_operator="and",
        conditions=[MetadataConditionItem(name=["status"], comparison_operator="in", value="not-a-list")],
    )
    with pytest.raises(FilterError):
        _build_qdrant_filter(group)


def test_none_returns_none():
    assert _build_qdrant_filter(None) is None
