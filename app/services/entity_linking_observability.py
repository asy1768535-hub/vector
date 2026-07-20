from __future__ import annotations

import logging
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.v07_entity_linking import EntityLinkingErrorCode
from app.services.graph_canonical import canonical_graph_json_v1


EntityLinkingResultCode = Literal[
    "success",
    "library_not_found",
    "publication_changed",
    "entity_type_not_found",
    "entity_linking_disabled",
    "entity_linking_invalid_request",
    "entity_linking_limit_exceeded",
    "entity_linking_internal_error",
    "graph_publication_unavailable",
    "graph_publication_invariant_failed",
    "entity_linking_timeout",
]
EntityLinkingMetricName = Literal[
    "entity_linking_requests_total",
    "entity_linking_duration_seconds",
    "entity_linking_results_total",
    "entity_linking_candidates_returned",
    "entity_linking_publication_recheck_failures_total",
]
EntityLinkingOutcome = Literal[
    "linked_exact",
    "linked_lexical",
    "ambiguous",
    "not_found",
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EntityLinkingObservation(_StrictFrozenModel):
    event: Literal["entity_linking.completed"] = "entity_linking.completed"
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    result_code: EntityLinkingResultCode
    status_code: int = Field(ge=100, le=599)
    library_id: uuid.UUID | None = None
    ontology_version_id: uuid.UUID | None = None
    publication_id: uuid.UUID | None = None
    mention_count: int = Field(default=0, ge=0, le=10)
    linked_exact_count: int = Field(default=0, ge=0, le=10)
    linked_lexical_count: int = Field(default=0, ge=0, le=10)
    ambiguous_count: int = Field(default=0, ge=0, le=10)
    not_found_count: int = Field(default=0, ge=0, le=10)
    candidate_count: int = Field(default=0, ge=0, le=100)
    duration_us: int = Field(ge=0)
    publication_recheck_failed: bool = False

    @model_validator(mode="after")
    def _validate_result_shape(self) -> EntityLinkingObservation:
        output_count = (
            self.linked_exact_count
            + self.linked_lexical_count
            + self.ambiguous_count
            + self.not_found_count
        )
        if self.result_code == "success":
            if (
                self.status_code != 200
                or self.library_id is None
                or self.ontology_version_id is None
                or self.publication_id is None
                or self.mention_count == 0
                or output_count != self.mention_count
            ):
                raise ValueError("success observation requires complete bounded counts")
        elif output_count != 0 or self.candidate_count != 0:
            raise ValueError("error observation must not contain result counts")
        return self


class EntityLinkingMetricSample(_StrictFrozenModel):
    event: Literal["entity_linking.metric"] = "entity_linking.metric"
    name: EntityLinkingMetricName
    value: float = Field(ge=0)
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("value")
    @classmethod
    def _value_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metric value must be finite")
        return value

    @model_validator(mode="after")
    def _labels_match_metric(self) -> EntityLinkingMetricSample:
        expected = {
            "entity_linking_requests_total": {"result_code"},
            "entity_linking_duration_seconds": {"result_code"},
            "entity_linking_results_total": {"outcome"},
            "entity_linking_candidates_returned": set(),
            "entity_linking_publication_recheck_failures_total": {"result_code"},
        }[self.name]
        if set(self.labels) != expected:
            raise ValueError("metric labels do not match the frozen allowlist")
        return self


class EntityLinkingObservationSink(Protocol):
    def emit(
        self,
        observation: EntityLinkingObservation,
        metric_samples: Sequence[EntityLinkingMetricSample],
    ) -> None: ...


def entity_linking_metric_samples(
    observation: EntityLinkingObservation,
) -> tuple[EntityLinkingMetricSample, ...]:
    samples = [
        EntityLinkingMetricSample(
            name="entity_linking_requests_total",
            value=1,
            labels={"result_code": observation.result_code},
        ),
        EntityLinkingMetricSample(
            name="entity_linking_duration_seconds",
            value=observation.duration_us / 1_000_000,
            labels={"result_code": observation.result_code},
        ),
        EntityLinkingMetricSample(
            name="entity_linking_candidates_returned",
            value=observation.candidate_count,
        ),
    ]
    for outcome, value in (
        ("linked_exact", observation.linked_exact_count),
        ("linked_lexical", observation.linked_lexical_count),
        ("ambiguous", observation.ambiguous_count),
        ("not_found", observation.not_found_count),
    ):
        samples.append(
            EntityLinkingMetricSample(
                name="entity_linking_results_total",
                value=value,
                labels={"outcome": outcome},
            )
        )
    if observation.publication_recheck_failed:
        samples.append(
            EntityLinkingMetricSample(
                name="entity_linking_publication_recheck_failures_total",
                value=1,
                labels={"result_code": observation.result_code},
            )
        )
    return tuple(samples)


class CanonicalLogEntityLinkingObservationSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("app.entity_linking")

    def emit(
        self,
        observation: EntityLinkingObservation,
        metric_samples: Sequence[EntityLinkingMetricSample],
    ) -> None:
        self._logger.info(
            "%s",
            canonical_graph_json_v1(observation.model_dump(mode="json")),
        )
        for sample in metric_samples:
            self._logger.info(
                "%s",
                canonical_graph_json_v1(sample.model_dump(mode="json")),
            )


DEFAULT_ENTITY_LINKING_OBSERVATION_SINK = CanonicalLogEntityLinkingObservationSink()


def emit_entity_linking_observation(
    observation: EntityLinkingObservation,
    *,
    sink: EntityLinkingObservationSink = DEFAULT_ENTITY_LINKING_OBSERVATION_SINK,
) -> None:
    try:
        sink.emit(observation, entity_linking_metric_samples(observation))
    except Exception:
        return


def entity_linking_error_result_code(
    code: EntityLinkingErrorCode,
) -> EntityLinkingResultCode:
    return code


def assert_sanitized_entity_linking_observation(
    observation: EntityLinkingObservation,
    metric_samples: Sequence[EntityLinkingMetricSample],
    *,
    forbidden_values: Sequence[str] = (),
) -> None:
    payloads: list[Mapping[str, object]] = [observation.model_dump(mode="json")]
    payloads.extend(sample.model_dump(mode="json") for sample in metric_samples)
    if set(payloads[0]) != set(EntityLinkingObservation.model_fields):
        raise ValueError("observation fields do not match the frozen allowlist")
    encoded = "\n".join(canonical_graph_json_v1(value) for value in payloads)
    lowered = encoded.casefold()
    for marker in forbidden_values:
        if marker and marker.casefold() in lowered:
            raise ValueError("private marker leaked into entity linking observation")
    if re.search(
        r'"(mention|canonical_name|entity_id|item_hash|score|properties|alias|source_text|request_body|sql|exception)"',
        encoded,
        re.IGNORECASE,
    ):
        raise ValueError("private field leaked into entity linking observation")
