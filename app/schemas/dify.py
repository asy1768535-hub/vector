"""Dify external knowledge base spec —— 严格按官方字段名。

迁移自 cpwsImportData/services/api.py:128-161，已去掉案件域专属字段。
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RetrievalSetting(BaseModel):
    top_k: int = Field(default=5, ge=1, le=100, description="Maximum number of records returned.")
    score_threshold: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Minimum similarity score (0~1).",
    )

    model_config = {"extra": "ignore"}


class MetadataConditionItem(BaseModel):
    name: list[str] = Field(..., min_length=1, description="Metadata field names (any-of).")
    comparison_operator: str = Field(..., min_length=1)
    # list 用于 in / not in（多值）；其余算子用标量。原先缺 list → in/not in 在 schema 层即被拒。
    value: str | int | float | bool | list | None = None

    model_config = {"extra": "ignore"}


class MetadataConditionGroup(BaseModel):
    logical_operator: str = Field(default="and", description="and / or")
    conditions: list[MetadataConditionItem] = Field(default_factory=list)

    model_config = {"extra": "ignore"}


class DifyRetrievalRequest(BaseModel):
    knowledge_id: str = Field(..., min_length=1, description="library slug")
    query: str = Field(..., min_length=1)
    retrieval_setting: RetrievalSetting = Field(default_factory=RetrievalSetting)
    metadata_condition: MetadataConditionGroup | None = None

    model_config = {"extra": "ignore"}


class DifyRecord(BaseModel):
    content: str
    score: float
    title: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class DifyRetrievalResponse(BaseModel):
    records: list[DifyRecord]
