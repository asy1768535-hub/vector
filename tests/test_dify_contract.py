"""Dify external knowledge base spec 契约测试。

仅验证 Pydantic 请求/响应模型对 spec 中字段名严格匹配——
不需要 DB 或 Qdrant 真实存在。
"""
from __future__ import annotations

import json

import pytest

from app.schemas.dify import (
    DifyRecord,
    DifyRetrievalRequest,
    DifyRetrievalResponse,
)


def test_request_accepts_minimal_dify_payload():
    body = {
        "knowledge_id": "medical",
        "query": "高血压怎么办",
    }
    req = DifyRetrievalRequest.model_validate(body)
    assert req.knowledge_id == "medical"
    assert req.query == "高血压怎么办"
    assert req.retrieval_setting.top_k == 5
    assert req.retrieval_setting.score_threshold == 0.0
    assert req.metadata_condition is None


def test_request_accepts_full_dify_payload():
    body = {
        "knowledge_id": "legal",
        "query": "侵权责任",
        "retrieval_setting": {"top_k": 10, "score_threshold": 0.3},
        "metadata_condition": {
            "logical_operator": "and",
            "conditions": [
                {"name": ["title"], "comparison_operator": "contains", "value": "合同"},
            ],
        },
    }
    req = DifyRetrievalRequest.model_validate(body)
    assert req.retrieval_setting.top_k == 10
    assert req.metadata_condition is not None
    assert req.metadata_condition.logical_operator == "and"
    assert req.metadata_condition.conditions[0].name == ["title"]


def test_request_ignores_unknown_fields():
    body = {
        "knowledge_id": "x",
        "query": "hi",
        "weird_extra_field": 42,  # 不该报错
    }
    req = DifyRetrievalRequest.model_validate(body)
    assert req.knowledge_id == "x"


def test_request_rejects_empty_query():
    with pytest.raises(Exception):
        DifyRetrievalRequest.model_validate({"knowledge_id": "x", "query": ""})


def test_response_serializes_dify_record_shape():
    resp = DifyRetrievalResponse(records=[
        DifyRecord(content="abc", score=0.87, title="t", metadata={"k": "v"})
    ])
    payload = json.loads(resp.model_dump_json())
    assert "records" in payload
    assert payload["records"][0] == {"content": "abc", "score": 0.87, "title": "t", "metadata": {"k": "v"}}


def test_response_default_metadata_is_empty_dict():
    rec = DifyRecord(content="x", score=0.5)
    assert rec.title == ""
    assert rec.metadata == {}
