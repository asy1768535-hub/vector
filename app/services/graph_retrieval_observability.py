from __future__ import annotations

import logging
import math
import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.v06_graph_retrieval import GraphRetrievalErrorCode
from app.services.graph_canonical import canonical_graph_json_v1


GraphRetrievalResultCode = Literal[
    "success",
    "seed_not_found",
    "seed_ambiguous",
    "publication_changed",
    "relation_type_not_found",
    "graph_retrieval_disabled",
    "graph_retrieval_invalid_request",
    "graph_retrieval_limit_exceeded",
    "graph_retrieval_internal_error",
    "graph_publication_unavailable",
    "graph_publication_invariant_failed",
    "graph_retrieval_timeout",
]
GraphRetrievalMetricName = Literal[
    "graph_retrieval_requests_total",
    "graph_retrieval_duration_seconds",
    "graph_retrieval_nodes_returned",
    "graph_retrieval_relations_returned",
    "graph_retrieval_truncated_total",
    "graph_retrieval_publication_recheck_failures_total",
]


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class GraphRetrievalObservation(_StrictFrozenModel):
    event: Literal["graph_retrieval.completed"] = "graph_retrieval.completed"
    request_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    result_code: GraphRetrievalResultCode
    status_code: int = Field(ge=100, le=599)
    library_id: uuid.UUID | None = None
    ontology_version_id: uuid.UUID | None = None
    publication_id: uuid.UUID | None = None
    max_hops: int | None = Field(default=None, ge=0, le=2)
    include_evidence_locators: bool | None = None
    node_count: int = Field(default=0, ge=0, le=100)
    relation_count: int = Field(default=0, ge=0, le=200)
    evidence_locator_count: int = Field(default=0, ge=0, le=6000)
    truncated_nodes: bool = False
    truncated_relations: bool = False
    truncated_evidence: bool = False
    duration_us: int = Field(ge=0)

    @model_validator(mode="after")
    def _success_has_publication(self) -> GraphRetrievalObservation:
        if self.result_code == "success":
            if (
                self.status_code != 200
                or self.library_id is None
                or self.ontology_version_id is None
                or self.publication_id is None
            ):
                raise ValueError("success observation requires resolved scope and publication")
        elif any(
            value != 0
            for value in (
                self.node_count,
                self.relation_count,
                self.evidence_locator_count,
            )
        ):
            raise ValueError("error observation must not contain graph counts")
        return self


class GraphRetrievalMetricSample(_StrictFrozenModel):
    event: Literal["graph_retrieval.metric"] = "graph_retrieval.metric"
    name: GraphRetrievalMetricName
    value: float = Field(ge=0)
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("value")
    @classmethod
    def _value_is_finite(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("metric value must be finite")
        return value

    @model_validator(mode="after")
    def _labels_match_metric(self) -> GraphRetrievalMetricSample:
        expected = {
            "graph_retrieval_requests_total": {"result_code"},
            "graph_retrieval_duration_seconds": {
                "result_code",
                "hop_class",
                "evidence_mode",
            },
            "graph_retrieval_nodes_returned": set(),
            "graph_retrieval_relations_returned": set(),
            "graph_retrieval_truncated_total": {"dimension"},
            "graph_retrieval_publication_recheck_failures_total": {"result_code"},
        }[self.name]
        if set(self.labels) != expected:
            raise ValueError("metric labels do not match the frozen allowlist")
        return self


class GraphRetrievalObservationSink(Protocol):
    def emit(
        self,
        observation: GraphRetrievalObservation,
        metric_samples: Sequence[GraphRetrievalMetricSample],
    ) -> None: ...


def graph_retrieval_metric_samples(
    observation: GraphRetrievalObservation,
) -> tuple[GraphRetrievalMetricSample, ...]:
    hop_class = (
        "unknown" if observation.max_hops is None else f"{observation.max_hops}-hop"
    )
    evidence_mode = (
        "unknown"
        if observation.include_evidence_locators is None
        else "on"
        if observation.include_evidence_locators
        else "off"
    )
    samples = [
        GraphRetrievalMetricSample(
            name="graph_retrieval_requests_total",
            value=1,
            labels={"result_code": observation.result_code},
        ),
        GraphRetrievalMetricSample(
            name="graph_retrieval_duration_seconds",
            value=observation.duration_us / 1_000_000,
            labels={
                "result_code": observation.result_code,
                "hop_class": hop_class,
                "evidence_mode": evidence_mode,
            },
        ),
        GraphRetrievalMetricSample(
            name="graph_retrieval_nodes_returned",
            value=observation.node_count,
        ),
        GraphRetrievalMetricSample(
            name="graph_retrieval_relations_returned",
            value=observation.relation_count,
        ),
    ]
    for dimension, truncated in (
        ("nodes", observation.truncated_nodes),
        ("relations", observation.truncated_relations),
        ("evidence", observation.truncated_evidence),
    ):
        if truncated:
            samples.append(
                GraphRetrievalMetricSample(
                    name="graph_retrieval_truncated_total",
                    value=1,
                    labels={"dimension": dimension},
                )
            )
    if observation.result_code in {
        "publication_changed",
        "graph_publication_invariant_failed",
    }:
        samples.append(
            GraphRetrievalMetricSample(
                name="graph_retrieval_publication_recheck_failures_total",
                value=1,
                labels={"result_code": observation.result_code},
            )
        )
    return tuple(samples)


class CanonicalLogGraphRetrievalObservationSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self._logger = logger or logging.getLogger("app.graph_retrieval")

    def emit(
        self,
        observation: GraphRetrievalObservation,
        metric_samples: Sequence[GraphRetrievalMetricSample],
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


DEFAULT_GRAPH_RETRIEVAL_OBSERVATION_SINK = CanonicalLogGraphRetrievalObservationSink()


def emit_graph_retrieval_observation(
    observation: GraphRetrievalObservation,
    *,
    sink: GraphRetrievalObservationSink = DEFAULT_GRAPH_RETRIEVAL_OBSERVATION_SINK,
) -> None:
    try:
        samples = graph_retrieval_metric_samples(observation)
        sink.emit(observation, samples)
    except Exception:
        return


def graph_retrieval_error_result_code(
    code: GraphRetrievalErrorCode,
) -> GraphRetrievalResultCode:
    return code


def assert_sanitized_graph_retrieval_observation(
    observation: GraphRetrievalObservation,
    metric_samples: Sequence[GraphRetrievalMetricSample],
    *,
    forbidden_values: Sequence[str] = (),
) -> None:
    allowed_observation_keys = set(GraphRetrievalObservation.model_fields)
    payloads: list[Mapping[str, object]] = [observation.model_dump(mode="json")]
    payloads.extend(sample.model_dump(mode="json") for sample in metric_samples)
    if set(payloads[0]) != allowed_observation_keys:
        raise ValueError("observation fields do not match the frozen allowlist")
    encoded = "\n".join(canonical_graph_json_v1(value) for value in payloads)
    lowered = encoded.casefold()
    for marker in forbidden_values:
        if marker and marker.casefold() in lowered:
            raise ValueError("private marker leaked into graph retrieval observation")
    if re.search(
        r'"(properties|canonical_name|seed_name|source_text|request_body|sql|exception)"',
        encoded,
        re.IGNORECASE,
    ):
        raise ValueError("private field leaked into graph retrieval observation")
