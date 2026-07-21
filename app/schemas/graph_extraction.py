from __future__ import annotations

from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ExtractionEvidenceClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_ref: str = Field(min_length=1, max_length=32)
    quote: str = Field(min_length=1)


class ExtractedEntityPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    local_id: str = Field(min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=512)
    entity_type_key: str = Field(min_length=1, max_length=128)
    aliases: list[str] = Field(default_factory=list)
    properties: dict[str, Any] = Field(default_factory=dict)
    external_mapping_hints: list[dict[str, str]] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    evidence: list[ExtractionEvidenceClaim] = Field(min_length=1)


class ExtractedRelationPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_local_id: str = Field(min_length=1, max_length=128)
    relation_type_key: str = Field(min_length=1, max_length=128)
    target_local_id: str = Field(min_length=1, max_length=128)
    properties: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(ge=0, le=1)
    evidence: list[ExtractionEvidenceClaim] = Field(min_length=1)


class GraphExtractionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: list[ExtractedEntityPayload] = Field(default_factory=list)
    relations: list[ExtractedRelationPayload] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_local_references(self) -> Self:
        local_ids = [entity.local_id for entity in self.entities]
        if len(local_ids) != len(set(local_ids)):
            raise ValueError("entity local_id values must be unique within one Unit")

        declared = set(local_ids)
        for relation in self.relations:
            if (
                relation.source_local_id not in declared
                or relation.target_local_id not in declared
            ):
                raise ValueError(
                    "relation endpoint must reference an entity declared within the same Unit"
                )
        return self
