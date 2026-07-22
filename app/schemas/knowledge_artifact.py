from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


GenerationMode = Literal["deterministic", "model"]


class SummaryPayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["summary-v1"] = "summary-v1"
    summary: str = Field(min_length=1, max_length=16_000)
    generation_mode: GenerationMode
    source_character_count: int = Field(ge=0, le=2_147_483_647)
    truncated: bool

    @field_validator("summary")
    @classmethod
    def reject_blank_summary(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("summary must not be blank")
        return value


class OutlineItemV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    level: int = Field(ge=1, le=6)
    title: str = Field(min_length=1, max_length=512)
    path: list[str] = Field(min_length=1, max_length=6)

    @field_validator("title")
    @classmethod
    def reject_blank_title(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("outline title must not be blank")
        return value

    @field_validator("path")
    @classmethod
    def validate_path_segments(cls, value: list[str]) -> list[str]:
        if any(not segment.strip() or len(segment) > 512 for segment in value):
            raise ValueError("outline path segments must be nonblank and at most 512 characters")
        return value

    @model_validator(mode="after")
    def validate_path_target(self) -> Self:
        if self.path[-1] != self.title:
            raise ValueError("outline path must end with the item title")
        return self


class OutlinePayloadV1(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["outline-v1"] = "outline-v1"
    generation_mode: GenerationMode
    items: list[OutlineItemV1] = Field(min_length=1, max_length=256)
