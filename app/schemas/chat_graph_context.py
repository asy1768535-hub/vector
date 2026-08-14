from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.v06_graph_retrieval import GraphRetrievalQueryResponse


class _StrictChatGraphContextModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ChatGraphContextResponse(_StrictChatGraphContextModel):
    contract_version: Literal["v1"]
    chunk_id: uuid.UUID
    exact_fact_count: int = Field(ge=0)
    exact_seed_count: int = Field(ge=0, le=10)
    exact_seeds_truncated: bool
    graph: GraphRetrievalQueryResponse | None = None

    @model_validator(mode="after")
    def validate_empty_state(self) -> ChatGraphContextResponse:
        if self.graph is None and (self.exact_fact_count or self.exact_seed_count):
            raise ValueError("empty graph context cannot declare matched facts or seeds")
        if self.graph is not None and self.exact_seed_count != self.graph.counts.seeds:
            raise ValueError("exact_seed_count must match graph seed count")
        return self


ChatGraphContextErrorCode = Literal[
    "citation_chunk_not_found",
    "graph_retrieval_disabled",
    "publication_changed",
    "graph_retrieval_limit_exceeded",
    "graph_retrieval_internal_error",
    "graph_publication_unavailable",
    "graph_publication_invariant_failed",
    "graph_retrieval_timeout",
]


class ChatGraphContextErrorResponse(_StrictChatGraphContextModel):
    detail: ChatGraphContextErrorCode
