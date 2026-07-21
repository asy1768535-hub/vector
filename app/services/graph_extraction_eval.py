from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.graph_canonical import canonical_graph_json_v1
from app.services.graph_normalization import normalize_graph_name_v1


_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_ARTIFACT_KEYS = {
    "aliases",
    "api_key",
    "authorization",
    "canonical_name",
    "context",
    "context_json",
    "context_text",
    "error_message",
    "headers",
    "messages",
    "parse_error",
    "parsed_response",
    "prompt",
    "provider_request_id",
    "provider_request_ids",
    "quote",
    "quote_text",
    "raw_response",
    "source_text",
    "text",
    "title",
}
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bbearer\s+\S+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
)
_UUID_TEXT_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}\b"
)
_URL_TEXT_RE = re.compile(r"https?://", re.IGNORECASE)
_FORBIDDEN_DATASET_KEYS = {"api_key", "authorization", "headers", "cookie"}
_ENTERPRISE_ENTITY_TYPE_KEYS = frozenset(
    {
        "person",
        "department",
        "position",
        "policy",
        "process",
        "project",
        "product",
        "customer",
        "document",
        "term",
    }
)
_ENTERPRISE_RELATION_CONSTRAINT_GROUPS = {
    "belongs_to": (("person", "position"), ("department",)),
    "responsible_for": (
        ("person", "department"),
        ("project", "process", "policy", "product"),
    ),
    "applies_to": (("policy",), ("department", "position", "person")),
    "constrains": (("policy",), ("process", "project")),
    "depends_on": (("process",), ("policy", "process")),
    "references": (("policy", "document"), ("policy", "document")),
    "approves": (("person", "position", "department"), ("process",)),
    "owns": (("department",), ("product", "project")),
    "related_to": (
        tuple(sorted(_ENTERPRISE_ENTITY_TYPE_KEYS)),
        tuple(sorted(_ENTERPRISE_ENTITY_TYPE_KEYS)),
    ),
}
ENTERPRISE_EVAL_RELATION_CONSTRAINTS = frozenset(
    (relation_type, source_type, target_type)
    for relation_type, (source_types, target_types) in (
        _ENTERPRISE_RELATION_CONSTRAINT_GROUPS.items()
    )
    for source_type in source_types
    for target_type in target_types
) | frozenset(
    {
        ("reports_to", "person", "person"),
        ("reports_to", "position", "position"),
    }
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_key(value: str) -> str:
    if not _KEY_RE.fullmatch(value):
        raise ValueError("must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    return value


class GraphEvalUnit(_StrictModel):
    unit_key: str
    text: str = Field(min_length=1)

    _unit_key = field_validator("unit_key")(_validate_key)

    @field_validator("text")
    @classmethod
    def _non_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Unit text must not be blank")
        return value


class GraphEvalGoldEntity(_StrictModel):
    gold_id: str
    canonical_name: str = Field(min_length=1)
    entity_type_key: str
    evidence_unit_keys: tuple[str, ...] = Field(min_length=1)
    aliases: tuple[str, ...] = ()

    _gold_id = field_validator("gold_id")(_validate_key)
    _entity_type_key = field_validator("entity_type_key")(_validate_key)

    @field_validator("canonical_name")
    @classmethod
    def _canonical_name_is_meaningful(cls, value: str) -> str:
        if not normalize_graph_name_v1(value):
            raise ValueError("canonical_name must not normalize to empty")
        return value

    @field_validator("evidence_unit_keys")
    @classmethod
    def _evidence_keys_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            _validate_key(key)
        if len(value) != len(set(value)):
            raise ValueError("evidence_unit_keys must be unique")
        return value

    @field_validator("aliases")
    @classmethod
    def _aliases_are_unique_and_non_empty(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = [normalize_graph_name_v1(item) for item in value]
        if any(not item for item in normalized):
            raise ValueError("aliases must not normalize to empty")
        if len(normalized) != len(set(normalized)):
            raise ValueError("aliases must be unique after normalization")
        return value


class GraphEvalGoldRelation(_StrictModel):
    gold_id: str
    source_gold_id: str
    relation_type_key: str
    target_gold_id: str
    evidence_unit_keys: tuple[str, ...] = Field(min_length=1)

    _gold_id = field_validator("gold_id")(_validate_key)
    _source_gold_id = field_validator("source_gold_id")(_validate_key)
    _relation_type_key = field_validator("relation_type_key")(_validate_key)
    _target_gold_id = field_validator("target_gold_id")(_validate_key)

    @field_validator("evidence_unit_keys")
    @classmethod
    def _evidence_keys_are_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            _validate_key(key)
        if len(value) != len(set(value)):
            raise ValueError("evidence_unit_keys must be unique")
        return value


class GraphEvalDocument(_StrictModel):
    schema_version: Literal["graph-extraction-eval-document-v1"]
    document_key: str
    title: str = Field(min_length=1)
    security_level: Literal["internal"]
    units: tuple[GraphEvalUnit, ...] = Field(min_length=1)
    gold_entities: tuple[GraphEvalGoldEntity, ...] = Field(min_length=1)
    gold_relations: tuple[GraphEvalGoldRelation, ...] = Field(min_length=1)

    _document_key = field_validator("document_key")(_validate_key)

    @model_validator(mode="after")
    def _references_are_local_and_unique(self) -> GraphEvalDocument:
        unit_keys = [row.unit_key for row in self.units]
        entity_ids = [row.gold_id for row in self.gold_entities]
        relation_ids = [row.gold_id for row in self.gold_relations]
        if len(unit_keys) != len(set(unit_keys)):
            raise ValueError("Unit keys must be unique inside a document")
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("Entity gold IDs must be unique inside a document")
        if len(relation_ids) != len(set(relation_ids)):
            raise ValueError("Relation gold IDs must be unique inside a document")
        unit_key_set = set(unit_keys)
        entity_id_set = set(entity_ids)
        for entity in self.gold_entities:
            if not set(entity.evidence_unit_keys) <= unit_key_set:
                raise ValueError("Entity Evidence must reference a local Unit")
        for relation in self.gold_relations:
            if not set(relation.evidence_unit_keys) <= unit_key_set:
                raise ValueError("Relation Evidence must reference a local Unit")
            if relation.source_gold_id not in entity_id_set:
                raise ValueError("Relation source must reference a local Entity")
            if relation.target_gold_id not in entity_id_set:
                raise ValueError("Relation target must reference a local Entity")
        return self


class GraphEvalDatasetMinimums(_StrictModel):
    documents: int = Field(ge=1)
    units: int = Field(ge=1)
    entities: int = Field(ge=1)
    relations: int = Field(ge=1)


class GraphEvalManifest(_StrictModel):
    schema_version: Literal["graph-extraction-eval-manifest-v1"]
    dataset_id: str
    source_file: str
    synthetic: Literal[True]
    document_keys: tuple[str, ...] = Field(min_length=1)
    minimums: GraphEvalDatasetMinimums

    _dataset_id = field_validator("dataset_id")(_validate_key)

    @field_validator("source_file")
    @classmethod
    def _source_file_is_safe_and_relative(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.suffix not in {".json", ".jsonl"}:
            raise ValueError("source_file must be a safe relative JSON/JSONL path")
        return value

    @field_validator("document_keys")
    @classmethod
    def _document_keys_are_sorted_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            _validate_key(key)
        if tuple(sorted(value)) != value or len(value) != len(set(value)):
            raise ValueError("document_keys must be sorted and unique")
        return value


class GraphEvalDatasetCounts(_StrictModel):
    documents: int = Field(ge=0)
    units: int = Field(ge=0)
    entities: int = Field(ge=0)
    relations: int = Field(ge=0)


class GraphEvalRate(_StrictModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None

    @model_validator(mode="after")
    def _value_matches_counts(self) -> GraphEvalRate:
        expected = None if self.denominator == 0 else self.numerator / self.denominator
        if self.numerator > self.denominator:
            raise ValueError("numerator must not exceed denominator")
        if expected is None:
            if self.value is not None:
                raise ValueError("zero denominator must have a null value")
        elif self.value is None or not math.isclose(
            self.value, expected, rel_tol=0.0, abs_tol=1e-15
        ):
            raise ValueError("rate value does not match numerator and denominator")
        return self


class GraphEvalClassification(_StrictModel):
    true_positive: int = Field(ge=0)
    false_positive: int = Field(ge=0)
    false_negative: int = Field(ge=0)
    precision: GraphEvalRate
    recall: GraphEvalRate


class GraphEvalMetricReport(_StrictModel):
    entity: GraphEvalClassification
    relation: GraphEvalClassification
    json_parse_rate: GraphEvalRate
    schema_valid_rate: GraphEvalRate
    invalid_evidence_rate: GraphEvalRate
    ambiguous_evidence_count: int = Field(ge=0)
    candidate_duplicate_rate: GraphEvalRate
    cross_revision_evidence_count: int = Field(ge=0)
    eval_formal_write_count: int = Field(ge=0)


class GraphEvalRunArtifact(_StrictModel):
    schema_version: Literal["graph-extraction-eval-result-v1"]
    run_id: str
    phase: Literal["development-smoke", "calibration", "post-freeze", "mock"]
    status: Literal["passed", "failed"]
    real_provider: bool
    started_at: datetime
    finished_at: datetime
    code_commit: str
    alembic_head: Literal["0022"]
    database_name: str
    dataset_id: str
    dataset_counts: GraphEvalDatasetCounts
    dataset_manifest_sha256: str
    dataset_content_sha256: str
    evaluation_config_hash: str
    model_provider: str
    model_name: str
    component_versions: dict[str, str]
    job_ids: tuple[uuid.UUID, ...]
    job_status_counts: dict[str, int]
    unit_status_counts: dict[str, int]
    attempt_status_counts: dict[str, int]
    model_attempt_count: int = Field(ge=0)
    real_model_call_count: int = Field(ge=0)
    provider_request_id_count: int = Field(ge=0)
    provider_request_id_sha256: str | None
    metrics: GraphEvalMetricReport
    stable_error_code_counts: dict[str, int]
    policy_id: str | None = None
    policy_sha256: str | None = None

    _run_id = field_validator("run_id")(_validate_key)
    _dataset_id = field_validator("dataset_id")(_validate_key)

    @field_validator(
        "dataset_manifest_sha256",
        "dataset_content_sha256",
        "evaluation_config_hash",
    )
    @classmethod
    def _required_hash_is_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("must be a lowercase SHA-256 digest")
        return value

    @field_validator("code_commit")
    @classmethod
    def _commit_is_full_sha1(cls, value: str) -> str:
        if not re.fullmatch(r"[0-9a-f]{40}", value):
            raise ValueError("code_commit must be a full lowercase Git SHA-1")
        return value

    @field_validator("database_name")
    @classmethod
    def _database_name_is_eval_scoped(cls, value: str) -> str:
        if len(value) > 63 or not re.fullmatch(r"vkt_m6_eval_[a-z0-9_]+", value):
            raise ValueError("database_name must use the vkt_m6_eval_ prefix")
        return value

    @field_validator(
        "job_status_counts",
        "unit_status_counts",
        "attempt_status_counts",
        "stable_error_code_counts",
    )
    @classmethod
    def _counts_are_non_negative(cls, value: dict[str, int]) -> dict[str, int]:
        if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in value.values()):
            raise ValueError("status and error counts must be non-negative integers")
        return value

    @model_validator(mode="after")
    def _artifact_is_internally_consistent(self) -> GraphEvalRunArtifact:
        if self.started_at.tzinfo is None or self.finished_at.tzinfo is None:
            raise ValueError("run timestamps must be timezone-aware")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        if self.model_attempt_count != sum(self.attempt_status_counts.values()):
            raise ValueError("model_attempt_count must equal Attempt status counts")
        expected_real_calls = self.model_attempt_count if self.real_provider else 0
        if self.real_model_call_count != expected_real_calls:
            raise ValueError("real_model_call_count is inconsistent with Provider mode")
        if self.provider_request_id_count > self.model_attempt_count:
            raise ValueError("Provider request ID count exceeds Attempt count")
        if self.provider_request_id_count:
            if self.provider_request_id_sha256 is None or not _SHA256_RE.fullmatch(
                self.provider_request_id_sha256
            ):
                raise ValueError("Provider request ID digest is required")
        elif self.provider_request_id_sha256 is not None:
            raise ValueError("empty Provider request ID population must not have a digest")
        if (self.policy_id is None) != (self.policy_sha256 is None):
            raise ValueError("policy_id and policy_sha256 must appear together")
        if self.policy_sha256 is not None and not _SHA256_RE.fullmatch(
            self.policy_sha256
        ):
            raise ValueError("policy_sha256 must be a lowercase SHA-256 digest")
        if self.phase == "post-freeze" and self.policy_id is None:
            raise ValueError("post-freeze results require a frozen policy")
        if self.status == "passed":
            if set(self.job_status_counts) != {"succeeded"}:
                raise ValueError("passed runs require only succeeded Jobs")
            if set(self.unit_status_counts) != {"succeeded"}:
                raise ValueError("passed runs require only succeeded Units")
            if self.metrics.cross_revision_evidence_count:
                raise ValueError("passed runs cannot contain cross-revision Evidence")
            if self.metrics.eval_formal_write_count:
                raise ValueError("passed runs cannot contain Eval formal writes")
        return self


class GraphEvalPolicyThresholds(_StrictModel):
    entity_precision: float = Field(ge=0.85, le=1.0)
    entity_recall: float = Field(ge=0.75, le=1.0)
    relation_precision: float = Field(ge=0.85, le=1.0)
    relation_recall: float = Field(ge=0.70, le=1.0)


class GraphEvalPolicy(_StrictModel):
    schema_version: Literal["graph-extraction-eval-policy-v1"]
    policy_id: Literal["eval_policy_v1"]
    dataset_manifest_sha256: str
    dataset_content_sha256: str
    evaluation_config_hash: str
    calibration_result_path: str
    calibration_result_sha256: str
    thresholds: GraphEvalPolicyThresholds
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    approval_reference: str = Field(min_length=1)

    @field_validator(
        "dataset_manifest_sha256",
        "dataset_content_sha256",
        "evaluation_config_hash",
        "calibration_result_sha256",
    )
    @classmethod
    def _hash_is_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("must be a lowercase SHA-256 digest")
        return value

    @field_validator("calibration_result_path")
    @classmethod
    def _calibration_path_is_scoped(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.suffix != ".json"
            or path.parts[:3] != ("eval", "graph_extraction", "results")
        ):
            raise ValueError("calibration result must be under eval/graph_extraction/results")
        return value

    @field_validator("approved_by", "approval_reference")
    @classmethod
    def _approval_text_is_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("approval metadata must not be blank")
        return value

    @field_validator("approved_at")
    @classmethod
    def _approval_time_is_utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("approved_at must be timezone-aware")
        if value.utcoffset().total_seconds() != 0:
            raise ValueError("approved_at must be UTC")
        return value


class GraphEvalEvidenceReference(_StrictModel):
    path: str
    sha256: str

    @field_validator("path")
    @classmethod
    def _path_is_repository_scoped_json(cls, value: str) -> str:
        path = PurePosixPath(value)
        if (
            path.is_absolute()
            or ".." in path.parts
            or path.suffix != ".json"
            or path.parts[:2] != ("eval", "graph_extraction")
        ):
            raise ValueError("release evidence reference must be scoped JSON")
        return value

    @field_validator("sha256")
    @classmethod
    def _sha_is_sha256(cls, value: str) -> str:
        if not _SHA256_RE.fullmatch(value):
            raise ValueError("release evidence reference requires a SHA-256 digest")
        return value


class GraphEvalReleaseEvidence(_StrictModel):
    schema_version: Literal["graph-extraction-release-evidence-v1"]
    evidence_id: Literal["release_evidence_v1"]
    calibration: GraphEvalEvidenceReference
    policy: GraphEvalEvidenceReference
    post_freeze_runs: tuple[
        GraphEvalEvidenceReference,
        GraphEvalEvidenceReference,
        GraphEvalEvidenceReference,
    ]

    @model_validator(mode="after")
    def _selected_paths_are_distinct(self) -> GraphEvalReleaseEvidence:
        paths = [
            self.calibration.path,
            self.policy.path,
            *(row.path for row in self.post_freeze_runs),
        ]
        if len(paths) != len(set(paths)):
            raise ValueError("release evidence references must be distinct")
        return self


EntityEvalKey: TypeAlias = tuple[str, str, str]
RelationEvalKey: TypeAlias = tuple[str, EntityEvalKey, str, EntityEvalKey]


@dataclass(frozen=True, slots=True)
class GraphEvalEntityPrediction:
    document_key: str
    entity_type_key: str
    canonical_name: str


@dataclass(frozen=True, slots=True)
class GraphEvalRelationPrediction:
    document_key: str
    source_entity_type_key: str
    source_canonical_name: str
    relation_type_key: str
    target_entity_type_key: str
    target_canonical_name: str
    directed: bool = True


@dataclass(frozen=True, slots=True)
class GraphEvalAttemptMetric:
    request_status: str
    parse_status: str | None


@dataclass(frozen=True, slots=True)
class LoadedGraphEvalDataset:
    manifest: GraphEvalManifest
    documents: tuple[GraphEvalDocument, ...]
    counts: GraphEvalDatasetCounts
    dataset_manifest_sha256: str
    dataset_content_sha256: str


def canonical_graph_eval_hash(value: Any) -> str:
    payload = canonical_graph_json_v1(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _artifact_sha256_variants(payload: bytes) -> frozenset[str]:
    normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return frozenset(
        (hashlib.sha256(payload).hexdigest(), hashlib.sha256(normalized).hexdigest())
    )


def _normalized_artifact_sha256(payload: bytes) -> str:
    normalized = payload.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(normalized).hexdigest()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load graph Eval JSON: {path.name}") from exc


def _load_jsonl(path: Path) -> list[Any]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot load graph Eval JSONL: {path.name}") from exc
    values: list[Any] = []
    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            raise ValueError(f"blank graph Eval JSONL line {line_number}")
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"invalid graph Eval JSONL at line {line_number}"
            ) from exc
    return values


def _assert_synthetic_dataset_value(value: Any) -> None:
    def visit(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"dataset key at {path} must be text")
                if key.strip().casefold() in _FORBIDDEN_DATASET_KEYS:
                    raise ValueError(f"forbidden dataset field: {path}.{key}")
                visit(child, f"{path}.{key}")
            return
        if isinstance(item, list):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
            return
        if isinstance(item, str):
            if _UUID_TEXT_RE.search(item):
                raise ValueError(f"UUID is forbidden in synthetic dataset at {path}")
            if _URL_TEXT_RE.search(item):
                raise ValueError(f"URL is forbidden in synthetic dataset at {path}")
            for pattern in _SECRET_VALUE_PATTERNS:
                if pattern.search(item):
                    raise ValueError(f"secret-like value in synthetic dataset at {path}")

    visit(value, "$dataset")


def _validate_ontology_contract(documents: tuple[GraphEvalDocument, ...]) -> None:
    relation_types = {
        relation_type for relation_type, _, _ in ENTERPRISE_EVAL_RELATION_CONSTRAINTS
    }
    for document in documents:
        entities = {row.gold_id: row for row in document.gold_entities}
        for entity in document.gold_entities:
            if entity.entity_type_key not in _ENTERPRISE_ENTITY_TYPE_KEYS:
                raise ValueError(
                    f"unknown Entity type {entity.entity_type_key} in {document.document_key}"
                )
        for relation in document.gold_relations:
            if relation.relation_type_key not in relation_types:
                raise ValueError(
                    f"unknown Relation type {relation.relation_type_key} "
                    f"in {document.document_key}"
                )
            source = entities[relation.source_gold_id]
            target = entities[relation.target_gold_id]
            constraint = (
                relation.relation_type_key,
                source.entity_type_key,
                target.entity_type_key,
            )
            if constraint not in ENTERPRISE_EVAL_RELATION_CONSTRAINTS:
                raise ValueError(
                    "Relation violates enterprise ontology constraint: "
                    f"{document.document_key}/{relation.gold_id}"
                )


def load_graph_eval_dataset(
    *,
    repository_root: Path,
    manifest_path: Path,
) -> LoadedGraphEvalDataset:
    root = repository_root.resolve()
    resolved_manifest = manifest_path.resolve()
    if root not in resolved_manifest.parents:
        raise ValueError("manifest must be inside the repository")
    manifest_payload = _load_json(resolved_manifest)
    _assert_synthetic_dataset_value(manifest_payload)
    manifest = GraphEvalManifest.model_validate(manifest_payload)
    source_path = (root / manifest.source_file).resolve()
    if root not in source_path.parents or not source_path.is_file():
        raise ValueError("manifest source_file is missing or outside the repository")
    source_payload = (
        _load_jsonl(source_path)
        if source_path.suffix == ".jsonl"
        else [_load_json(source_path)]
    )
    _assert_synthetic_dataset_value(source_payload)
    source_documents = tuple(
        GraphEvalDocument.model_validate(row) for row in source_payload
    )
    source_keys = tuple(row.document_key for row in source_documents)
    if source_keys != tuple(sorted(source_keys)) or len(source_keys) != len(
        set(source_keys)
    ):
        raise ValueError("source documents must be sorted and unique")
    by_key = {row.document_key: row for row in source_documents}
    missing = set(manifest.document_keys) - set(by_key)
    if missing:
        raise ValueError(f"manifest references missing documents: {sorted(missing)}")
    documents = tuple(by_key[key] for key in manifest.document_keys)
    _validate_ontology_contract(documents)
    counts = GraphEvalDatasetCounts(
        documents=len(documents),
        units=sum(len(row.units) for row in documents),
        entities=sum(len(row.gold_entities) for row in documents),
        relations=sum(len(row.gold_relations) for row in documents),
    )
    minimums = manifest.minimums
    for field in ("documents", "units", "entities", "relations"):
        if getattr(counts, field) < getattr(minimums, field):
            raise ValueError(f"dataset does not meet minimum {field} count")
    if manifest.dataset_id == "development-smoke-v1" and counts.documents != 10:
        raise ValueError("Development Smoke must select exactly 10 documents")
    if manifest.dataset_id == "release-v1" and counts.documents < 30:
        raise ValueError("Release Eval must select at least 30 documents")
    manifest_value = manifest.model_dump(mode="json")
    content_value = [row.model_dump(mode="json") for row in documents]
    return LoadedGraphEvalDataset(
        manifest=manifest,
        documents=documents,
        counts=counts,
        dataset_manifest_sha256=canonical_graph_eval_hash(manifest_value),
        dataset_content_sha256=canonical_graph_eval_hash(content_value),
    )


def entity_eval_key(
    document_key: str,
    entity_type_key: str,
    canonical_name: str,
) -> EntityEvalKey:
    return (
        _validate_key(document_key),
        _validate_key(entity_type_key),
        normalize_graph_name_v1(canonical_name),
    )


def relation_eval_key(
    document_key: str,
    source_key: EntityEvalKey,
    relation_type_key: str,
    target_key: EntityEvalKey,
    *,
    directed: bool,
) -> RelationEvalKey:
    document_key = _validate_key(document_key)
    relation_type_key = _validate_key(relation_type_key)
    if source_key[0] != document_key or target_key[0] != document_key:
        raise ValueError("Relation endpoints must belong to the same document")
    if not directed and target_key < source_key:
        source_key, target_key = target_key, source_key
    return (document_key, source_key, relation_type_key, target_key)


def gold_entity_keys(documents: tuple[GraphEvalDocument, ...]) -> list[EntityEvalKey]:
    return [
        entity_eval_key(document.document_key, entity.entity_type_key, entity.canonical_name)
        for document in documents
        for entity in document.gold_entities
    ]


def gold_relation_keys(
    documents: tuple[GraphEvalDocument, ...],
    *,
    undirected_relation_types: frozenset[str] = frozenset(),
) -> list[RelationEvalKey]:
    keys: list[RelationEvalKey] = []
    for document in documents:
        entities = {entity.gold_id: entity for entity in document.gold_entities}
        for relation in document.gold_relations:
            source = entities[relation.source_gold_id]
            target = entities[relation.target_gold_id]
            keys.append(
                relation_eval_key(
                    document.document_key,
                    entity_eval_key(
                        document.document_key,
                        source.entity_type_key,
                        source.canonical_name,
                    ),
                    relation.relation_type_key,
                    entity_eval_key(
                        document.document_key,
                        target.entity_type_key,
                        target.canonical_name,
                    ),
                    directed=relation.relation_type_key
                    not in undirected_relation_types,
                )
            )
    return keys


def prediction_entity_keys(
    rows: tuple[GraphEvalEntityPrediction, ...],
) -> list[EntityEvalKey]:
    return [
        entity_eval_key(row.document_key, row.entity_type_key, row.canonical_name)
        for row in rows
    ]


def prediction_relation_keys(
    rows: tuple[GraphEvalRelationPrediction, ...],
) -> list[RelationEvalKey]:
    return [
        relation_eval_key(
            row.document_key,
            entity_eval_key(
                row.document_key,
                row.source_entity_type_key,
                row.source_canonical_name,
            ),
            row.relation_type_key,
            entity_eval_key(
                row.document_key,
                row.target_entity_type_key,
                row.target_canonical_name,
            ),
            directed=row.directed,
        )
        for row in rows
    ]


def rate(numerator: int, denominator: int) -> GraphEvalRate:
    if numerator < 0 or denominator < 0 or numerator > denominator:
        raise ValueError("invalid rate counts")
    return GraphEvalRate(
        numerator=numerator,
        denominator=denominator,
        value=None if denominator == 0 else numerator / denominator,
    )


def classify(gold: list[Any], predicted: list[Any]) -> GraphEvalClassification:
    gold_set = set(gold)
    predicted_set = set(predicted)
    true_positive = len(gold_set & predicted_set)
    false_positive = len(predicted_set - gold_set)
    false_negative = len(gold_set - predicted_set)
    return GraphEvalClassification(
        true_positive=true_positive,
        false_positive=false_positive,
        false_negative=false_negative,
        precision=rate(true_positive, true_positive + false_positive),
        recall=rate(true_positive, true_positive + false_negative),
    )


def build_metric_report(
    *,
    gold_entities: list[EntityEvalKey],
    predicted_entities: list[EntityEvalKey],
    gold_relations: list[RelationEvalKey],
    predicted_relations: list[RelationEvalKey],
    attempts: tuple[GraphEvalAttemptMetric, ...],
    relation_schema_statuses: tuple[str | None, ...],
    evidence_statuses: tuple[str, ...],
    cross_revision_evidence_count: int,
    eval_formal_write_count: int,
) -> GraphEvalMetricReport:
    parsed = sum(
        row.request_status == "succeeded" and row.parse_status == "valid"
        for row in attempts
    )
    schema_valid = sum(status == "valid" for status in relation_schema_statuses)
    invalid_evidence = sum(status == "invalid" for status in evidence_statuses)
    ambiguous_evidence = sum(status == "ambiguous" for status in evidence_statuses)
    all_candidate_keys: list[Any] = [
        ("entity", row) for row in predicted_entities
    ] + [("relation", row) for row in predicted_relations]
    duplicate_count = len(all_candidate_keys) - len(set(all_candidate_keys))
    return GraphEvalMetricReport(
        entity=classify(gold_entities, predicted_entities),
        relation=classify(gold_relations, predicted_relations),
        json_parse_rate=rate(parsed, len(attempts)),
        schema_valid_rate=rate(schema_valid, len(relation_schema_statuses)),
        invalid_evidence_rate=rate(invalid_evidence, len(evidence_statuses)),
        ambiguous_evidence_count=ambiguous_evidence,
        candidate_duplicate_rate=rate(duplicate_count, len(all_candidate_keys)),
        cross_revision_evidence_count=cross_revision_evidence_count,
        eval_formal_write_count=eval_formal_write_count,
    )


def assert_sanitized_eval_artifact(value: Any) -> None:
    def visit(item: Any, path: str) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError(f"artifact key at {path} must be text")
                normalized = key.strip().casefold()
                if normalized in _FORBIDDEN_ARTIFACT_KEYS:
                    raise ValueError(f"forbidden artifact field: {path}.{key}")
                visit(child, f"{path}.{key}")
            return
        if isinstance(item, (list, tuple)):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
            return
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"non-finite artifact value at {path}")
        if isinstance(item, str):
            for pattern in _SECRET_VALUE_PATTERNS:
                if pattern.search(item):
                    raise ValueError(f"secret-like artifact value at {path}")

    visit(value, "$artifact")
    canonical_graph_json_v1(value)


@dataclass(frozen=True, slots=True)
class LoadedGraphEvalPolicy:
    policy: GraphEvalPolicy
    calibration: GraphEvalRunArtifact
    policy_sha256: str


def load_graph_eval_policy(
    *,
    repository_root: Path,
    policy_path: Path,
) -> LoadedGraphEvalPolicy:
    root = repository_root.resolve()
    resolved_policy = policy_path.resolve()
    if root not in resolved_policy.parents:
        raise ValueError("Eval Policy must be inside the repository")

    payload = _load_json(resolved_policy)
    policy = GraphEvalPolicy.model_validate(payload)
    assert_sanitized_eval_artifact(policy.model_dump(mode="json"))

    calibration_path = (root / policy.calibration_result_path).resolve()
    if root not in calibration_path.parents:
        raise ValueError("calibration result must be inside the repository")
    try:
        calibration_bytes = calibration_path.read_bytes()
    except OSError as exc:
        raise ValueError("cannot load frozen calibration result") from exc
    if policy.calibration_result_sha256 not in _artifact_sha256_variants(calibration_bytes):
        raise ValueError("calibration result SHA-256 does not match Eval Policy")

    calibration = GraphEvalRunArtifact.model_validate_json(calibration_bytes)
    if (
        calibration.phase != "calibration"
        or calibration.status != "passed"
        or not calibration.real_provider
        or calibration.policy_id is not None
    ):
        raise ValueError("Eval Policy requires a passed pre-policy real calibration")
    if calibration.dataset_manifest_sha256 != policy.dataset_manifest_sha256:
        raise ValueError("calibration dataset manifest hash does not match Eval Policy")
    if calibration.dataset_content_sha256 != policy.dataset_content_sha256:
        raise ValueError("calibration dataset content hash does not match Eval Policy")
    if calibration.evaluation_config_hash != policy.evaluation_config_hash:
        raise ValueError("calibration config hash does not match Eval Policy")
    if policy.approved_at <= calibration.finished_at:
        raise ValueError("Eval Policy approval must follow calibration completion")

    try:
        policy_sha256 = _normalized_artifact_sha256(resolved_policy.read_bytes())
    except OSError as exc:
        raise ValueError("cannot hash Eval Policy") from exc
    return LoadedGraphEvalPolicy(policy, calibration, policy_sha256)


@dataclass(frozen=True, slots=True)
class LoadedGraphEvalReleaseEvidence:
    evidence: GraphEvalReleaseEvidence
    evidence_sha256: str
    policy: LoadedGraphEvalPolicy
    calibration: GraphEvalRunArtifact
    post_freeze_runs: tuple[
        GraphEvalRunArtifact,
        GraphEvalRunArtifact,
        GraphEvalRunArtifact,
    ]


def _load_evidence_reference(
    *,
    repository_root: Path,
    reference: GraphEvalEvidenceReference,
) -> tuple[Path, bytes]:
    path = (repository_root / reference.path).resolve()
    if repository_root not in path.parents:
        raise ValueError("release evidence reference is outside the repository")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError("cannot load release evidence reference") from exc
    if reference.sha256 not in _artifact_sha256_variants(payload):
        raise ValueError(f"release evidence SHA-256 mismatch: {reference.path}")
    return path, payload


def _assert_selected_release_run(
    artifact: GraphEvalRunArtifact,
    *,
    policy: LoadedGraphEvalPolicy,
) -> None:
    if (
        artifact.status != "passed"
        or not artifact.real_provider
        or artifact.dataset_id != "release-v1"
        or artifact.model_provider != "deepseek"
        or artifact.model_name != "deepseek-v4-pro"
        or artifact.real_model_call_count < 100
    ):
        raise ValueError("selected Release run is not a passed real DeepSeek run")
    if artifact.dataset_manifest_sha256 != policy.policy.dataset_manifest_sha256:
        raise ValueError("selected Release run manifest hash does not match policy")
    if artifact.dataset_content_sha256 != policy.policy.dataset_content_sha256:
        raise ValueError("selected Release run content hash does not match policy")
    if artifact.evaluation_config_hash != policy.policy.evaluation_config_hash:
        raise ValueError("selected Release run config hash does not match policy")
    if artifact.metrics.cross_revision_evidence_count:
        raise ValueError("selected Release run contains cross-revision Evidence")
    if artifact.metrics.eval_formal_write_count:
        raise ValueError("selected Release run contains Eval formal writes")


def _assert_post_freeze_release_run(
    artifact: GraphEvalRunArtifact,
    *,
    policy: LoadedGraphEvalPolicy,
) -> None:
    _assert_selected_release_run(artifact, policy=policy)
    if artifact.phase != "post-freeze":
        raise ValueError("selected post-freeze artifact has the wrong phase")
    if (
        artifact.policy_id != policy.policy.policy_id
        or artifact.policy_sha256 != policy.policy_sha256
    ):
        raise ValueError("selected post-freeze artifact does not bind the policy")
    if artifact.job_status_counts != {
        "succeeded": artifact.dataset_counts.documents
    }:
        raise ValueError("selected post-freeze artifact requires succeeded Jobs")
    if artifact.unit_status_counts != {"succeeded": artifact.dataset_counts.units}:
        raise ValueError("selected post-freeze artifact requires succeeded Units")

    thresholds = policy.policy.thresholds
    rate_gates = (
        (artifact.metrics.json_parse_rate.value, 0.99),
        (artifact.metrics.schema_valid_rate.value, 0.95),
        (artifact.metrics.entity.precision.value, thresholds.entity_precision),
        (artifact.metrics.entity.recall.value, thresholds.entity_recall),
        (artifact.metrics.relation.precision.value, thresholds.relation_precision),
        (artifact.metrics.relation.recall.value, thresholds.relation_recall),
    )
    if any(value is None or value < floor for value, floor in rate_gates):
        raise ValueError("selected post-freeze artifact is below a frozen threshold")


def load_graph_eval_release_evidence(
    *,
    repository_root: Path,
    evidence_path: Path,
) -> LoadedGraphEvalReleaseEvidence:
    root = repository_root.resolve()
    resolved_evidence = evidence_path.resolve()
    expected_evidence = (root / "eval/graph_extraction/release_evidence_v1.json").resolve()
    if resolved_evidence != expected_evidence:
        raise ValueError("release evidence must use the frozen v1 repository path")
    try:
        evidence_bytes = resolved_evidence.read_bytes()
    except OSError as exc:
        raise ValueError("cannot load graph Eval release evidence") from exc
    evidence = GraphEvalReleaseEvidence.model_validate_json(evidence_bytes)
    assert_sanitized_eval_artifact(evidence.model_dump(mode="json"))

    expected_policy_path = "eval/graph_extraction/eval_policy_v1.json"
    if evidence.policy.path != expected_policy_path:
        raise ValueError("release evidence must select eval_policy_v1")
    policy_path, _policy_bytes = _load_evidence_reference(
        repository_root=root,
        reference=evidence.policy,
    )
    policy = load_graph_eval_policy(repository_root=root, policy_path=policy_path)
    if (
        evidence.policy.sha256 not in _artifact_sha256_variants(_policy_bytes)
        or policy.policy_sha256 not in _artifact_sha256_variants(_policy_bytes)
    ):
        raise ValueError("release evidence policy SHA-256 is inconsistent")

    if evidence.calibration.path != policy.policy.calibration_result_path:
        raise ValueError("release evidence selected the wrong calibration path")
    calibration_path, calibration_bytes = _load_evidence_reference(
        repository_root=root,
        reference=evidence.calibration,
    )
    if evidence.calibration.sha256 != policy.policy.calibration_result_sha256:
        raise ValueError("release evidence selected the wrong calibration SHA-256")
    calibration = GraphEvalRunArtifact.model_validate_json(calibration_bytes)
    if calibration_path.name != f"{calibration.run_id}.json":
        raise ValueError("calibration result filename does not match its run ID")
    _assert_selected_release_run(calibration, policy=policy)
    if calibration.phase != "calibration" or calibration.policy_id is not None:
        raise ValueError("release evidence calibration must predate the policy")

    post_freeze_rows: list[GraphEvalRunArtifact] = []
    for reference in evidence.post_freeze_runs:
        if PurePosixPath(reference.path).parts[:3] != (
            "eval",
            "graph_extraction",
            "results",
        ):
            raise ValueError("post-freeze result must be under the results directory")
        run_path, run_bytes = _load_evidence_reference(
            repository_root=root,
            reference=reference,
        )
        artifact = GraphEvalRunArtifact.model_validate_json(run_bytes)
        if run_path.name != f"{artifact.run_id}.json":
            raise ValueError("post-freeze result filename does not match its run ID")
        _assert_post_freeze_release_run(artifact, policy=policy)
        post_freeze_rows.append(artifact)

    if len({row.run_id for row in post_freeze_rows}) != 3:
        raise ValueError("post-freeze run IDs must be distinct")
    if len({row.database_name for row in post_freeze_rows}) != 3:
        raise ValueError("post-freeze database names must be distinct")
    selected = [calibration, *post_freeze_rows]
    if len({row.dataset_manifest_sha256 for row in selected}) != 1:
        raise ValueError("selected Release manifest hashes are inconsistent")
    if len({row.dataset_content_sha256 for row in selected}) != 1:
        raise ValueError("selected Release content hashes are inconsistent")
    if len({row.evaluation_config_hash for row in selected}) != 1:
        raise ValueError("selected Release config hashes are inconsistent")

    return LoadedGraphEvalReleaseEvidence(
        evidence=evidence,
        evidence_sha256=_normalized_artifact_sha256(evidence_bytes),
        policy=policy,
        calibration=calibration,
        post_freeze_runs=(
            post_freeze_rows[0],
            post_freeze_rows[1],
            post_freeze_rows[2],
        ),
    )
