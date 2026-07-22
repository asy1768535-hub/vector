from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictBaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ClassifierCandidateV1(StrictBaseModel):
    label_key: str | None = Field(default=None, min_length=1, max_length=64)
    proposed_key: str | None = Field(default=None, min_length=1, max_length=64)
    proposed_label: str | None = Field(default=None, min_length=1, max_length=160)
    confidence_micros: int = Field(strict=True, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def _target_shape(self):
        known = self.label_key is not None
        unknown = self.proposed_key is not None or self.proposed_label is not None
        if known == unknown:
            raise ValueError("candidate must contain exactly one target shape")
        if unknown and (self.proposed_key is None or self.proposed_label is None):
            raise ValueError("unknown candidate requires key and display label")
        return self


class ClassifierOutputV1(StrictBaseModel):
    primary_candidates: list[ClassifierCandidateV1] = Field(min_length=1, max_length=5)
    secondary_candidates: list[ClassifierCandidateV1] = Field(
        default_factory=list,
        max_length=8,
    )

    @model_validator(mode="after")
    def _unique_targets(self):
        identities = [
            (item.label_key or item.proposed_key or "").strip().casefold()
            for item in (*self.primary_candidates, *self.secondary_candidates)
        ]
        if len(set(identities)) != len(identities):
            raise ValueError("candidate targets must be unique")
        return self
