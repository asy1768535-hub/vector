from __future__ import annotations

import hashlib
import json
import math
import os
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.graph_canonical import canonical_graph_json_v1


_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_SECRET_PATTERNS = (
    re.compile(r"\bbearer\s+\S+", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
)
_FORBIDDEN_ARTIFACT_KEYS = {
    "api_key",
    "authorization",
    "canonical_name",
    "content",
    "context",
    "context_text",
    "document_block_id",
    "document_id",
    "document_revision_id",
    "error_message",
    "evidence_id",
    "evidence_text_snapshot",
    "headers",
    "include_properties",
    "normalized_name",
    "plan_json",
    "properties",
    "prompt",
    "quote_text",
    "raw_response",
    "request_body",
    "runtime_uuid",
    "seed_name",
    "source_text",
    "sql",
    "stack_trace",
    "text_quote",
}
_ALLOWED_DIRECTIONS = ("outbound", "inbound", "both")
_ALLOWED_PUBLICATION_STATES = ("active", "degraded", "superseded", "partial")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_key(value: str) -> str:
    if not _KEY_RE.fullmatch(value):
        raise ValueError("must be a lowercase evaluation key")
    return value


def _validate_sha256(value: str) -> str:
    if not _SHA256_RE.fullmatch(value):
        raise ValueError("must be a lowercase SHA-256 digest")
    return value


def _require_utc(value: datetime, *, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None or value.utcoffset().total_seconds() != 0:
        raise ValueError(f"{label} must be UTC")
    return value


def canonical_json_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_graph_json_v1(value).encode("utf-8")).hexdigest()


def file_sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _canonical_json_bytes(value: Any) -> bytes:
    return (canonical_graph_json_v1(value) + "\n").encode("utf-8")


def _read_canonical_json(path: Path) -> tuple[Any, bytes]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read graph retrieval Eval JSON: {path.name}") from exc
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid graph retrieval Eval JSON: {path.name}") from exc
    if payload != _canonical_json_bytes(value):
        raise ValueError(f"{path.name} must use canonical JSON bytes")
    return value, payload


def _read_canonical_jsonl(path: Path) -> tuple[tuple[Any, ...], bytes]:
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read graph retrieval Eval JSONL: {path.name}") from exc
    if not payload.endswith(b"\n") or b"\r\n" in payload or payload.endswith(b"\n\n"):
        raise ValueError(f"{path.name} must use canonical LF-terminated JSONL")
    values: list[Any] = []
    for line in payload[:-1].split(b"\n"):
        if not line:
            raise ValueError(f"{path.name} contains a blank JSONL record")
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"{path.name} contains invalid JSONL") from exc
        if line != canonical_graph_json_v1(value).encode("utf-8"):
            raise ValueError(f"{path.name} must use canonical JSONL records")
        values.append(value)
    return tuple(values), payload


class GraphRetrievalGoldLibrary(_StrictModel):
    library_key: str
    slug: str = Field(min_length=1, max_length=128)

    _library_key = field_validator("library_key")(_validate_key)


class GraphRetrievalGoldOntology(_StrictModel):
    ontology_key: str
    library_key: str
    version_key: str

    _ontology_key = field_validator("ontology_key")(_validate_key)
    _library_key = field_validator("library_key")(_validate_key)
    _version_key = field_validator("version_key")(_validate_key)


class GraphRetrievalGoldEntityType(_StrictModel):
    type_key: str
    ontology_key: str
    label: str = Field(min_length=1, max_length=255)

    _type_key = field_validator("type_key")(_validate_key)
    _ontology_key = field_validator("ontology_key")(_validate_key)


class GraphRetrievalGoldRelationType(_StrictModel):
    type_key: str
    ontology_key: str
    label: str = Field(min_length=1, max_length=255)
    direction: Literal["directed", "undirected"]

    _type_key = field_validator("type_key")(_validate_key)
    _ontology_key = field_validator("ontology_key")(_validate_key)


class GraphRetrievalGoldEvidence(_StrictModel):
    evidence_key: str
    library_key: str
    document_key: str
    revision_key: str
    block_key: str | None = None
    evidence_kind: str = Field(min_length=1, max_length=32)
    page_start: int | None = Field(default=None, ge=1)
    page_end: int | None = Field(default=None, ge=1)
    source_start: int | None = Field(default=None, ge=0)
    source_end: int | None = Field(default=None, ge=0)

    _evidence_key = field_validator("evidence_key")(_validate_key)
    _library_key = field_validator("library_key")(_validate_key)
    _document_key = field_validator("document_key")(_validate_key)
    _revision_key = field_validator("revision_key")(_validate_key)

    @field_validator("block_key")
    @classmethod
    def _block_key_is_valid(cls, value: str | None) -> str | None:
        return _validate_key(value) if value is not None else None

    @model_validator(mode="after")
    def _ranges_are_valid(self) -> GraphRetrievalGoldEvidence:
        if self.page_start is not None and self.page_end is not None and self.page_end < self.page_start:
            raise ValueError("page_end must not precede page_start")
        if (
            self.source_start is not None
            and self.source_end is not None
            and self.source_end < self.source_start
        ):
            raise ValueError("source_end must not precede source_start")
        return self


class GraphRetrievalGoldEntity(_StrictModel):
    entity_key: str
    ontology_key: str
    entity_type_key: str
    canonical_name: str = Field(min_length=1, max_length=512)
    normalized_name: str = Field(min_length=1, max_length=512)
    source_type: Literal["manual", "imported", "extracted"] = "manual"
    confidence: float | None = Field(default=None, ge=0, le=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    support_evidence_keys: tuple[str, ...] = ()

    _entity_key = field_validator("entity_key")(_validate_key)
    _ontology_key = field_validator("ontology_key")(_validate_key)
    _type_key = field_validator("entity_type_key")(_validate_key)

    @field_validator("support_evidence_keys")
    @classmethod
    def _supports_are_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            _validate_key(key)
        if tuple(sorted(value)) != value:
            raise ValueError("Entity support Evidence keys must be sorted")
        return value


class GraphRetrievalGoldRelation(_StrictModel):
    relation_key: str
    ontology_key: str
    relation_type_key: str
    source_entity_key: str
    target_entity_key: str
    source_type: Literal["manual", "imported", "extracted"] = "manual"
    confidence: float | None = Field(default=None, ge=0, le=1)
    properties: dict[str, Any] = Field(default_factory=dict)
    support_evidence_keys: tuple[str, ...] = ()

    _relation_key = field_validator("relation_key")(_validate_key)
    _ontology_key = field_validator("ontology_key")(_validate_key)
    _type_key = field_validator("relation_type_key")(_validate_key)
    _source_key = field_validator("source_entity_key")(_validate_key)
    _target_key = field_validator("target_entity_key")(_validate_key)

    @field_validator("support_evidence_keys")
    @classmethod
    def _supports_are_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            _validate_key(key)
        if tuple(sorted(value)) != value:
            raise ValueError("Relation support Evidence keys must be sorted")
        return value


class GraphRetrievalPrivacyCanary(_StrictModel):
    canary_id: str
    fact_key: str
    key_marker: str = Field(min_length=1, max_length=128)
    value_marker: str = Field(min_length=1, max_length=128)

    _canary_id = field_validator("canary_id")(_validate_key)
    _fact_key = field_validator("fact_key")(_validate_key)


class GraphRetrievalScenarioPublication(_StrictModel):
    publication_key: str
    library_key: str
    ontology_key: str
    state: Literal["active", "degraded", "superseded", "partial"]

    _publication_key = field_validator("publication_key")(_validate_key)
    _library_key = field_validator("library_key")(_validate_key)
    _ontology_key = field_validator("ontology_key")(_validate_key)


class GraphRetrievalEvalGold(_StrictModel):
    schema_version: Literal["graph-retrieval-gold-v1"]
    dataset_id: Literal["release-v1"]
    libraries: tuple[GraphRetrievalGoldLibrary, ...]
    ontologies: tuple[GraphRetrievalGoldOntology, ...]
    entity_types: tuple[GraphRetrievalGoldEntityType, ...]
    relation_types: tuple[GraphRetrievalGoldRelationType, ...]
    evidence: tuple[GraphRetrievalGoldEvidence, ...]
    entities: tuple[GraphRetrievalGoldEntity, ...]
    relations: tuple[GraphRetrievalGoldRelation, ...]
    privacy_canaries: tuple[GraphRetrievalPrivacyCanary, ...]
    scenario_publications: tuple[GraphRetrievalScenarioPublication, ...]

    @model_validator(mode="after")
    def _references_and_order_are_valid(self) -> GraphRetrievalEvalGold:
        collections = {
            "Library": [row.library_key for row in self.libraries],
            "Ontology": [row.ontology_key for row in self.ontologies],
            "Entity Type": [row.type_key for row in self.entity_types],
            "Relation Type": [row.type_key for row in self.relation_types],
            "Evidence": [row.evidence_key for row in self.evidence],
            "Entity": [row.entity_key for row in self.entities],
            "Relation": [row.relation_key for row in self.relations],
            "privacy canary": [row.canary_id for row in self.privacy_canaries],
            "publication": [row.publication_key for row in self.scenario_publications],
        }
        for label, values in collections.items():
            if values != sorted(values) or len(values) != len(set(values)):
                raise ValueError(f"{label} keys must be sorted and unique")

        library_keys = set(collections["Library"])
        ontology_by_key = {row.ontology_key: row for row in self.ontologies}
        entity_type_by_key = {row.type_key: row for row in self.entity_types}
        relation_type_by_key = {row.type_key: row for row in self.relation_types}
        evidence_by_key = {row.evidence_key: row for row in self.evidence}
        entity_by_key = {row.entity_key: row for row in self.entities}
        fact_keys = set(entity_by_key) | set(collections["Relation"])
        for ontology in self.ontologies:
            if ontology.library_key not in library_keys:
                raise ValueError("Ontology references an unknown Library")
        for row in (*self.entity_types, *self.relation_types):
            if row.ontology_key not in ontology_by_key:
                raise ValueError("Type references an unknown Ontology")
        for evidence in self.evidence:
            if evidence.library_key not in library_keys:
                raise ValueError("Evidence references an unknown Library")
        for entity in self.entities:
            entity_type = entity_type_by_key.get(entity.entity_type_key)
            if entity_type is None or entity_type.ontology_key != entity.ontology_key:
                raise ValueError("Entity references an unknown or cross-Ontology Entity Type")
            if any(key not in evidence_by_key for key in entity.support_evidence_keys):
                raise ValueError("Entity references unknown Evidence")
        for relation in self.relations:
            relation_type = relation_type_by_key.get(relation.relation_type_key)
            if relation_type is None or relation_type.ontology_key != relation.ontology_key:
                raise ValueError("Relation references an unknown or cross-Ontology Relation Type")
            source = entity_by_key.get(relation.source_entity_key)
            target = entity_by_key.get(relation.target_entity_key)
            if source is None or target is None or source.ontology_key != relation.ontology_key or target.ontology_key != relation.ontology_key:
                raise ValueError("Relation references unknown or cross-Ontology endpoints")
            if any(key not in evidence_by_key for key in relation.support_evidence_keys):
                raise ValueError("Relation references unknown Evidence")
        for canary in self.privacy_canaries:
            if canary.fact_key not in fact_keys:
                raise ValueError("privacy canary references an unknown fact")
        for publication in self.scenario_publications:
            ontology = ontology_by_key.get(publication.ontology_key)
            if ontology is None or ontology.library_key != publication.library_key:
                raise ValueError("publication references an invalid scope")

        ontology_counts: dict[str, int] = {}
        for ontology in self.ontologies:
            ontology_counts[ontology.library_key] = ontology_counts.get(ontology.library_key, 0) + 1
        if len(self.libraries) < 2 or max(ontology_counts.values(), default=0) < 2:
            raise ValueError("release gold requires two Libraries and a two-Ontology Library")
        if {row.direction for row in self.relation_types} != {"directed", "undirected"}:
            raise ValueError("release gold requires directed and undirected Relation Types")
        if len(self.entities) < 40 or len(self.relations) < 40 or len(self.evidence) < 20:
            raise ValueError("release gold is below required fact minimums")
        if len(self.privacy_canaries) < 12:
            raise ValueError("release gold requires at least 12 property privacy canaries")

        outgoing_counts: dict[str, int] = {}
        directed_edges: set[tuple[str, str]] = set()
        successors: dict[str, set[str]] = {}
        self_loop = False
        for relation in self.relations:
            outgoing_counts[relation.source_entity_key] = (
                outgoing_counts.get(relation.source_entity_key, 0) + 1
            )
            relation_type = relation_type_by_key[relation.relation_type_key]
            if relation.source_entity_key == relation.target_entity_key:
                self_loop = True
            if relation_type.direction == "directed":
                edge = (relation.source_entity_key, relation.target_entity_key)
                directed_edges.add(edge)
                successors.setdefault(edge[0], set()).add(edge[1])
        if max(outgoing_counts.values(), default=0) <= 200:
            raise ValueError("release gold requires a greater-than-200 high-degree source")
        if not self_loop:
            raise ValueError("release gold requires a self-loop")
        if not any((target, source) in directed_edges for source, target in directed_edges):
            raise ValueError("release gold requires a directed cycle")
        has_diamond = any(
            len(
                {
                    destination
                    for middle in middle_nodes
                    for destination in successors.get(middle, set())
                }
            )
            and any(
                sum(destination in successors.get(middle, set()) for middle in middle_nodes) >= 2
                for destination in entity_by_key
            )
            for middle_nodes in successors.values()
            if len(middle_nodes) >= 2
        )
        if not has_diamond:
            raise ValueError("release gold requires a duplicate-path diamond")
        duplicate_name_types: dict[str, set[str]] = {}
        for entity in self.entities:
            duplicate_name_types.setdefault(entity.normalized_name, set()).add(
                entity.entity_type_key
            )
        if not any(len(type_keys) >= 2 for type_keys in duplicate_name_types.values()):
            raise ValueError("release gold requires same-name Entities with different types")
        return self


class GraphRetrievalEvalSeed(_StrictModel):
    entity_key: str | None = None
    canonical_name: str | None = Field(default=None, min_length=1, max_length=512)
    entity_type_key: str | None = None

    @model_validator(mode="after")
    def _selector_is_exact(self) -> GraphRetrievalEvalSeed:
        if (self.entity_key is None) == (self.canonical_name is None):
            raise ValueError("Eval seed requires exactly one selector")
        if self.entity_key is not None:
            _validate_key(self.entity_key)
            if self.entity_type_key is not None:
                raise ValueError("entity_type_key requires canonical_name")
        if self.entity_type_key is not None:
            _validate_key(self.entity_type_key)
        return self


class GraphRetrievalEvalRequest(_StrictModel):
    ontology_key: str
    expected_publication: bool = True
    seeds: tuple[GraphRetrievalEvalSeed, ...] = Field(min_length=1, max_length=10)
    direction: Literal["outbound", "inbound", "both"] = "both"
    relation_type_keys: tuple[str, ...] = ()
    max_hops: int = Field(default=1, ge=0, le=2)
    max_nodes: int = Field(default=100, ge=1, le=100)
    max_relations: int = Field(default=200, ge=1, le=200)
    include_evidence_locators: bool = True

    _ontology_key = field_validator("ontology_key")(_validate_key)

    @field_validator("relation_type_keys")
    @classmethod
    def _relation_types_are_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            _validate_key(key)
        if tuple(sorted(value)) != value or len(value) != len(set(value)):
            raise ValueError("Relation Type keys must be sorted and unique")
        return value

    @model_validator(mode="after")
    def _limits_cover_seeds(self) -> GraphRetrievalEvalRequest:
        if self.max_nodes < len(self.seeds):
            raise ValueError("max_nodes must cover all seeds")
        return self


class GraphRetrievalEvalTruncation(_StrictModel):
    nodes: bool = False
    relations: bool = False
    evidence: bool = False


class GraphRetrievalEvalExpected(_StrictModel):
    status_code: Literal[200, 404, 409, 422, 503, 504]
    detail: str | None = None
    seed_entity_keys: tuple[str, ...] = ()
    candidate_entity_keys: tuple[str, ...] = ()
    node_keys: tuple[str, ...] = ()
    relation_keys: tuple[str, ...] = ()
    relation_evidence: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    truncated: GraphRetrievalEvalTruncation = Field(default_factory=GraphRetrievalEvalTruncation)

    @model_validator(mode="after")
    def _success_and_error_shapes_are_separate(self) -> GraphRetrievalEvalExpected:
        if self.status_code == 200:
            if self.detail is not None or not self.seed_entity_keys or not self.node_keys:
                raise ValueError("successful expected result requires seed/node gold and no detail")
        elif self.detail is None or self.node_keys or self.relation_keys or self.relation_evidence:
            raise ValueError("error expected result requires detail and no graph gold")
        return self


class GraphRetrievalEvalCase(_StrictModel):
    schema_version: Literal["graph-retrieval-case-v1"]
    case_id: str
    categories: tuple[str, ...] = Field(min_length=1)
    scenario_publication_key: str
    request: GraphRetrievalEvalRequest
    expected: GraphRetrievalEvalExpected

    _case_id = field_validator("case_id")(_validate_key)
    _publication_key = field_validator("scenario_publication_key")(_validate_key)

    @field_validator("categories")
    @classmethod
    def _categories_are_sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for key in value:
            _validate_key(key)
        if tuple(sorted(value)) != value or len(value) != len(set(value)):
            raise ValueError("case categories must be sorted and unique")
        return value


class GraphRetrievalEvalFileReference(_StrictModel):
    path: str
    canonical_sha256: str
    file_sha256: str

    _canonical_hash = field_validator("canonical_sha256")(_validate_sha256)
    _file_hash = field_validator("file_sha256")(_validate_sha256)

    @field_validator("path")
    @classmethod
    def _path_is_scoped(cls, value: str) -> str:
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or path.parts[:2] != ("eval", "graph_retrieval"):
            raise ValueError("artifact path must be scoped under eval/graph_retrieval")
        if path.suffix not in {".json", ".jsonl"}:
            raise ValueError("artifact path must name JSON or JSONL")
        return value


class GraphRetrievalEvalDatasetCounts(_StrictModel):
    entities: int = Field(ge=0)
    relations: int = Field(ge=0)
    evidence: int = Field(ge=0)
    cases: int = Field(ge=0)


class GraphRetrievalPerformanceFixture(_StrictModel):
    generator_version: Literal["graph-retrieval-perf-generator-v1"]
    entities: Literal[6000]
    relations: Literal[4000]
    high_degree_relations: int = Field(ge=250, le=1000)
    two_hop_nodes: int = Field(ge=101, le=1000)
    evidence_mode: Literal["selected-facts"]
    namespace_version: Literal["graph-retrieval-eval-uuid-v1"]


class GraphRetrievalEvalManifest(_StrictModel):
    schema_version: Literal["graph-retrieval-eval-manifest-v1"]
    dataset_id: Literal["release-v1"]
    synthetic: Literal[True]
    gold: GraphRetrievalEvalFileReference
    cases: GraphRetrievalEvalFileReference
    ordered_case_ids: tuple[str, ...] = Field(min_length=48)
    counts: GraphRetrievalEvalDatasetCounts
    category_minimums: dict[str, int]
    category_counts: dict[str, int]
    uuid_namespace_version: Literal["graph-retrieval-eval-uuid-v1"]
    response_canonicalization_version: Literal["graph-retrieval-response-canonical-v1"]
    performance_fixture: GraphRetrievalPerformanceFixture
    evaluation_config_version: Literal["graph-retrieval-eval-config-v1"]
    evaluation_config_sha256: str

    _config_hash = field_validator("evaluation_config_sha256")(_validate_sha256)

    @model_validator(mode="after")
    def _manifest_counts_are_sufficient(self) -> GraphRetrievalEvalManifest:
        if self.counts.entities < 40 or self.counts.relations < 40 or self.counts.evidence < 20 or self.counts.cases < 48:
            raise ValueError("release manifest is below required dataset minimums")
        if any(count < 1 for count in self.category_minimums.values()):
            raise ValueError("category minimums must be positive")
        if set(self.category_counts) != set(self.category_minimums):
            raise ValueError("category count keys must match minimum keys")
        if any(self.category_counts[key] < value for key, value in self.category_minimums.items()):
            raise ValueError("category count is below its minimum")
        if tuple(sorted(self.ordered_case_ids)) != self.ordered_case_ids:
            raise ValueError("manifest case IDs must be sorted")
        return self


@dataclass(frozen=True, slots=True)
class LoadedGraphRetrievalEvalDataset:
    manifest: GraphRetrievalEvalManifest
    gold: GraphRetrievalEvalGold
    cases: tuple[GraphRetrievalEvalCase, ...]
    manifest_path: Path
    gold_path: Path
    case_path: Path
    dataset_manifest_sha256: str
    dataset_content_sha256: str
    evaluation_config_sha256: str
    category_counts: Mapping[str, int]

    def validate_case_references(self, case: GraphRetrievalEvalCase) -> None:
        entity_keys = {row.entity_key for row in self.gold.entities}
        relation_keys = {row.relation_key for row in self.gold.relations}
        evidence_keys = {row.evidence_key for row in self.gold.evidence}
        ontology_keys = {row.ontology_key for row in self.gold.ontologies}
        relation_type_keys = {row.type_key for row in self.gold.relation_types}
        publication_keys = {row.publication_key for row in self.gold.scenario_publications}
        if case.scenario_publication_key not in publication_keys:
            raise ValueError("case references an unknown publication")
        if case.request.ontology_key not in ontology_keys:
            raise ValueError("case references an unknown Ontology")
        for seed in case.request.seeds:
            if seed.entity_key is not None and seed.entity_key not in entity_keys:
                raise ValueError("case seed references an unknown Entity")
        if any(key not in relation_type_keys for key in case.request.relation_type_keys):
            raise ValueError("case references an unknown Relation Type")
        expected = case.expected
        for key in (*expected.seed_entity_keys, *expected.candidate_entity_keys, *expected.node_keys):
            if key not in entity_keys:
                raise ValueError("case expected result references an unknown Entity")
        if any(key not in relation_keys for key in expected.relation_keys):
            raise ValueError("case expected result references an unknown Relation")
        for relation_key, supports in expected.relation_evidence.items():
            if relation_key not in relation_keys or any(key not in evidence_keys for key in supports):
                raise ValueError("case expected result references unknown Relation Evidence")


def _resolve_scoped_path(root: Path, value: str) -> Path:
    root = root.resolve()
    unresolved = root / PurePosixPath(value)
    current = unresolved
    while current != root:
        if current.is_symlink():
            raise ValueError("Eval path must not traverse a symlink")
        if root not in current.parents:
            break
        current = current.parent
    path = unresolved.resolve()
    if path == root or root not in path.parents:
        raise ValueError("Eval path escapes repository root")
    return path


def _assert_reference(reference: GraphRetrievalEvalFileReference, value: Any, payload: bytes) -> None:
    if canonical_json_sha256(value) != reference.canonical_sha256:
        raise ValueError("Eval reference canonical SHA-256 mismatch")
    if file_sha256(payload) != reference.file_sha256:
        raise ValueError("Eval reference file SHA-256 mismatch")


def load_graph_retrieval_eval_dataset(
    *,
    repository_root: Path,
    manifest_path: Path,
) -> LoadedGraphRetrievalEvalDataset:
    manifest_value, manifest_bytes = _read_canonical_json(manifest_path)
    root = repository_root.resolve()
    resolved_manifest = manifest_path.resolve()
    if root not in resolved_manifest.parents:
        raise ValueError("Eval manifest must be inside the repository")
    manifest = GraphRetrievalEvalManifest.model_validate(manifest_value)
    gold_path = _resolve_scoped_path(root, manifest.gold.path)
    case_path = _resolve_scoped_path(root, manifest.cases.path)
    gold_value, gold_bytes = _read_canonical_json(gold_path)
    case_values, case_bytes = _read_canonical_jsonl(case_path)
    _assert_reference(manifest.gold, gold_value, gold_bytes)
    _assert_reference(manifest.cases, list(case_values), case_bytes)
    gold = GraphRetrievalEvalGold.model_validate(gold_value)
    cases = tuple(GraphRetrievalEvalCase.model_validate(value) for value in case_values)
    case_ids = tuple(row.case_id for row in cases)
    if case_ids != tuple(sorted(case_ids)) or len(case_ids) != len(set(case_ids)):
        raise ValueError("Eval case IDs must be sorted and unique")
    if case_ids != manifest.ordered_case_ids:
        raise ValueError("manifest ordered case IDs do not match case source")
    category_counts = {
        key: sum(key in row.categories for row in cases)
        for key in manifest.category_minimums
    }
    if category_counts != manifest.category_counts:
        raise ValueError("manifest category counts do not match cases")
    counts = GraphRetrievalEvalDatasetCounts(
        entities=len(gold.entities),
        relations=len(gold.relations),
        evidence=len(gold.evidence),
        cases=len(cases),
    )
    if counts != manifest.counts:
        raise ValueError("manifest dataset counts do not match sources")
    config_value = {
        "schema_version": manifest.evaluation_config_version,
        "response_canonicalization_version": manifest.response_canonicalization_version,
        "performance_fixture": manifest.performance_fixture.model_dump(mode="json"),
        "requests": [
            {"case_id": row.case_id, "request": row.request.model_dump(mode="json")}
            for row in cases
        ],
    }
    evaluation_config_sha256 = canonical_json_sha256(config_value)
    if evaluation_config_sha256 != manifest.evaluation_config_sha256:
        raise ValueError("manifest evaluation config SHA-256 mismatch")
    dataset_content_sha256 = canonical_json_sha256(
        {
            "gold_schema_version": gold.schema_version,
            "gold_content_sha256": canonical_json_sha256(gold_value),
            "case_schema_version": "graph-retrieval-case-v1",
            "case_content_sha256": canonical_json_sha256(list(case_values)),
        }
    )
    loaded = LoadedGraphRetrievalEvalDataset(
        manifest=manifest,
        gold=gold,
        cases=cases,
        manifest_path=resolved_manifest,
        gold_path=gold_path,
        case_path=case_path,
        dataset_manifest_sha256=canonical_json_sha256(manifest_value),
        dataset_content_sha256=dataset_content_sha256,
        evaluation_config_sha256=evaluation_config_sha256,
        category_counts=category_counts,
    )
    for case in cases:
        loaded.validate_case_references(case)
    if manifest_bytes != _canonical_json_bytes(manifest.model_dump(mode="json")):
        raise ValueError("manifest Pydantic representation is not canonical")
    return loaded


class GraphRetrievalEvalRate(_StrictModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def _value_matches_counts(self) -> GraphRetrievalEvalRate:
        expected = None if self.denominator == 0 else self.numerator / self.denominator
        if self.numerator > self.denominator or self.value != expected:
            raise ValueError("rate value must exactly match numerator/denominator")
        return self


def rate(numerator: int, denominator: int) -> GraphRetrievalEvalRate:
    return GraphRetrievalEvalRate(
        numerator=numerator,
        denominator=denominator,
        value=None if denominator == 0 else numerator / denominator,
    )


class GraphRetrievalCaseScore(_StrictModel):
    seed_correct: int = Field(ge=0)
    seed_total: int = Field(ge=0)
    expected_nodes: int = Field(ge=0)
    returned_expected_nodes: int = Field(ge=0)
    expected_relations: int = Field(ge=0)
    returned_expected_relations: int = Field(ge=0)
    returned_relations: int = Field(ge=0)
    expected_evidence_pairs: int = Field(ge=0)
    returned_expected_evidence_pairs: int = Field(ge=0)
    deterministic_equal: int = Field(ge=0)
    deterministic_total: int = Field(ge=0)
    scope_safe: int = Field(ge=0)
    scope_total: int = Field(ge=0)
    privacy_clean: int = Field(ge=0)
    privacy_total: int = Field(ge=0)
    property_leak_count: int = Field(ge=0)
    publication_membership_failures: int = Field(ge=0)


class GraphRetrievalMetricReport(_StrictModel):
    seed_exact_accuracy: GraphRetrievalEvalRate
    node_recall: GraphRetrievalEvalRate
    relation_recall: GraphRetrievalEvalRate
    relation_precision: GraphRetrievalEvalRate
    evidence_coverage: GraphRetrievalEvalRate
    determinism: GraphRetrievalEvalRate
    scope_safety: GraphRetrievalEvalRate
    property_privacy: GraphRetrievalEvalRate
    property_leak_count: int = Field(ge=0)
    publication_membership_failures: int = Field(ge=0)

    def passes_hard_gates(self) -> bool:
        required = (
            self.seed_exact_accuracy,
            self.node_recall,
            self.relation_recall,
            self.relation_precision,
            self.evidence_coverage,
            self.determinism,
            self.scope_safety,
            self.property_privacy,
        )
        return (
            all(value.denominator > 0 and value.value == 1.0 for value in required)
            and self.property_leak_count == 0
            and self.publication_membership_failures == 0
        )


def build_graph_retrieval_metric_report(
    scores: Iterable[GraphRetrievalCaseScore],
) -> GraphRetrievalMetricReport:
    rows = tuple(scores)

    def total(field: str) -> int:
        return sum(getattr(row, field) for row in rows)

    return GraphRetrievalMetricReport(
        seed_exact_accuracy=rate(total("seed_correct"), total("seed_total")),
        node_recall=rate(total("returned_expected_nodes"), total("expected_nodes")),
        relation_recall=rate(total("returned_expected_relations"), total("expected_relations")),
        relation_precision=rate(total("returned_expected_relations"), total("returned_relations")),
        evidence_coverage=rate(
            total("returned_expected_evidence_pairs"), total("expected_evidence_pairs")
        ),
        determinism=rate(total("deterministic_equal"), total("deterministic_total")),
        scope_safety=rate(total("scope_safe"), total("scope_total")),
        property_privacy=rate(total("privacy_clean"), total("privacy_total")),
        property_leak_count=total("property_leak_count"),
        publication_membership_failures=total("publication_membership_failures"),
    )


class GraphRetrievalLatencyStats(_StrictModel):
    sample_count: int = Field(ge=1)
    p50_us: int = Field(ge=0)
    p95_us: int = Field(ge=0)
    max_us: int = Field(ge=0)

    @model_validator(mode="after")
    def _percentiles_are_ordered(self) -> GraphRetrievalLatencyStats:
        if not (self.p50_us <= self.p95_us <= self.max_us):
            raise ValueError("latency percentiles must be ordered")
        return self


def nearest_rank_latency(duration_ns: Sequence[int]) -> GraphRetrievalLatencyStats:
    if not duration_ns or any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in duration_ns):
        raise ValueError("latency samples must be non-negative integer nanoseconds")
    values = sorted(math.ceil(value / 1000) for value in duration_ns)

    def percentile(fraction: float) -> int:
        return values[math.ceil(fraction * len(values)) - 1]

    return GraphRetrievalLatencyStats(
        sample_count=len(values),
        p50_us=percentile(0.50),
        p95_us=percentile(0.95),
        max_us=values[-1],
    )


def _canonicalize_runtime_value(value: Any, logical_id_by_uuid: Mapping[str, str], *, key: str | None = None) -> Any:
    if key == "activated_at":
        if not isinstance(value, str):
            raise ValueError("activated_at must be an ISO UTC string")
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("activated_at must be an ISO UTC string") from exc
        _require_utc(parsed, label="activated_at")
        return "dataset-activation-v1"
    if isinstance(value, dict):
        if any(str(child_key).casefold() in {"properties", "include_properties"} for child_key in value):
            raise ValueError("canonical response contains forbidden properties")
        return {
            child_key: _canonicalize_runtime_value(child, logical_id_by_uuid, key=child_key)
            for child_key, child in value.items()
        }
    if isinstance(value, list):
        return [_canonicalize_runtime_value(child, logical_id_by_uuid) for child in value]
    if isinstance(value, str) and _UUID_RE.fullmatch(value):
        logical = logical_id_by_uuid.get(value.lower()) or logical_id_by_uuid.get(value)
        if logical is None:
            raise ValueError("canonical response contains an unknown runtime UUID")
        return logical
    return value


def canonical_graph_retrieval_response(
    *,
    status_code: int,
    body: Mapping[str, Any],
    logical_id_by_uuid: Mapping[str, str],
) -> dict[str, Any]:
    if not isinstance(status_code, int) or isinstance(status_code, bool):
        raise ValueError("status_code must be an integer")
    mapped = _canonicalize_runtime_value(dict(body), logical_id_by_uuid)
    return {
        "schema_version": "graph-retrieval-response-canonical-v1",
        "status_code": status_code,
        "body": mapped,
    }


def canonical_graph_retrieval_response_hash(
    *,
    status_code: int,
    body: Mapping[str, Any],
    logical_id_by_uuid: Mapping[str, str],
) -> str:
    return canonical_json_sha256(
        canonical_graph_retrieval_response(
            status_code=status_code,
            body=body,
            logical_id_by_uuid=logical_id_by_uuid,
        )
    )


def assert_sanitized_graph_retrieval_artifact(
    value: Any,
    *,
    forbidden_values: Iterable[str] = (),
) -> None:
    canaries = tuple(item.casefold() for item in forbidden_values if item)

    def visit(item: Any, path: str) -> None:
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError(f"non-finite artifact value at {path}")
            return
        if isinstance(item, str):
            if len(item) > 1024:
                raise ValueError(f"unbounded artifact text at {path}")
            lowered = item.casefold()
            if _UUID_RE.search(item) or re.search(r"https?://", item, re.IGNORECASE):
                raise ValueError(f"runtime identifier or URL in artifact at {path}")
            if any(pattern.search(item) for pattern in _SECRET_PATTERNS):
                raise ValueError(f"secret-like artifact value at {path}")
            if any(canary in lowered for canary in canaries):
                raise ValueError(f"privacy canary leaked into artifact at {path}")
            return
        if isinstance(item, list) or isinstance(item, tuple):
            for index, child in enumerate(item):
                visit(child, f"{path}[{index}]")
            return
        if isinstance(item, dict):
            for child_key, child in item.items():
                if not isinstance(child_key, str):
                    raise ValueError(f"non-text artifact key at {path}")
                if (
                    path == "$"
                    and child_key == "evidence_id"
                    and child == "release_evidence_v1"
                ):
                    continue
                if child_key.casefold() in _FORBIDDEN_ARTIFACT_KEYS:
                    raise ValueError(f"forbidden artifact field: {path}.{child_key}")
                visit(child, f"{path}.{child_key}")
            return
        raise ValueError(f"unsupported artifact value at {path}")

    visit(value, "$")


class GraphRetrievalExplainStats(_StrictModel):
    scan_rows_observed: int = Field(ge=0)
    plan_rows_observed: int = Field(ge=0)
    shared_hit_blocks: int = Field(ge=0)
    shared_read_blocks: int = Field(ge=0)
    temp_read_blocks: int = Field(ge=0)
    temp_written_blocks: int = Field(ge=0)
    plan_shape_sha256: str

    _shape_hash = field_validator("plan_shape_sha256")(_validate_sha256)


class GraphRetrievalEvalThresholds(_StrictModel):
    seed_exact_accuracy: float = Field(ge=1.0, le=1.0)
    node_recall: float = Field(ge=1.0, le=1.0)
    relation_recall: float = Field(ge=1.0, le=1.0)
    relation_precision: float = Field(ge=1.0, le=1.0)
    evidence_coverage: float = Field(ge=1.0, le=1.0)
    determinism: float = Field(ge=1.0, le=1.0)
    scope_safety: float = Field(ge=1.0, le=1.0)
    property_privacy: float = Field(ge=1.0, le=1.0)
    max_property_leaks: Literal[0]
    max_membership_failures: Literal[0]
    max_p95_us: dict[str, int]
    max_scan_rows: dict[str, int]

    @field_validator("max_p95_us")
    @classmethod
    def _p95_thresholds_are_bounded(cls, value: dict[str, int]) -> dict[str, int]:
        if not value or any(not isinstance(item, int) or isinstance(item, bool) or item < 1 or item > 2_400_000 for item in value.values()):
            raise ValueError("p95 thresholds must be positive and at most 2.4 seconds")
        return value

    @field_validator("max_scan_rows")
    @classmethod
    def _scan_thresholds_are_positive(cls, value: dict[str, int]) -> dict[str, int]:
        if not value or any(not isinstance(item, int) or isinstance(item, bool) or item < 1 for item in value.values()):
            raise ValueError("scan thresholds must be positive integers")
        return value


class GraphRetrievalCaseResponseHash(_StrictModel):
    case_id: str
    status_code: int = Field(ge=100, le=599)
    canonical_sha256: str

    _case_id = field_validator("case_id")(_validate_key)
    _hash = field_validator("canonical_sha256")(_validate_sha256)


class GraphRetrievalEvalResultArtifact(_StrictModel):
    schema_version: Literal["graph-retrieval-eval-result-v1"]
    run_id: str
    phase: Literal["calibration", "post-freeze"]
    status: Literal["passed", "failed"]
    started_at: datetime
    finished_at: datetime
    code_commit: str
    implementation_tree_sha256: str
    alembic_head: Literal["0023"]
    database_id: str
    environment_fingerprint_sha256: str
    dataset_id: Literal["release-v1"]
    dataset_counts: GraphRetrievalEvalDatasetCounts
    dataset_manifest_sha256: str
    dataset_content_sha256: str
    evaluation_config_sha256: str
    contract_version: Literal["v1"]
    normalization_version: Literal["normalize_graph_name_v1"]
    response_canonicalization_version: Literal["graph-retrieval-response-canonical-v1"]
    metrics: GraphRetrievalMetricReport
    performance: dict[str, GraphRetrievalLatencyStats]
    explain: dict[str, GraphRetrievalExplainStats]
    candidate_thresholds: GraphRetrievalEvalThresholds | None = None
    case_counts: dict[str, int]
    stable_error_code_counts: dict[str, int]
    case_response_hashes: tuple[GraphRetrievalCaseResponseHash, ...]
    canonical_response_set_sha256: str
    policy_id: Literal["release_policy_v1"] | None = None
    policy_canonical_sha256: str | None = None
    policy_file_sha256: str | None = None
    database_cleanup_succeeded: bool

    _run_id = field_validator("run_id")(_validate_key)
    _tree_hash = field_validator("implementation_tree_sha256")(_validate_sha256)
    _environment_hash = field_validator("environment_fingerprint_sha256")(_validate_sha256)
    _manifest_hash = field_validator("dataset_manifest_sha256")(_validate_sha256)
    _content_hash = field_validator("dataset_content_sha256")(_validate_sha256)
    _config_hash = field_validator("evaluation_config_sha256")(_validate_sha256)
    _response_hash = field_validator("canonical_response_set_sha256")(_validate_sha256)

    @field_validator("code_commit")
    @classmethod
    def _commit_is_full_sha1(cls, value: str) -> str:
        if not _SHA1_RE.fullmatch(value):
            raise ValueError("code_commit must be a full lowercase Git SHA-1")
        return value

    @field_validator("database_id")
    @classmethod
    def _database_is_eval_scoped(cls, value: str) -> str:
        if len(value) > 63 or not re.fullmatch(r"vkt_v06_m5_eval_[a-z0-9_]+", value):
            raise ValueError("database_id must use the vkt_v06_m5_eval_ prefix")
        return value

    @field_validator("policy_canonical_sha256", "policy_file_sha256")
    @classmethod
    def _optional_hash_is_valid(cls, value: str | None) -> str | None:
        return _validate_sha256(value) if value is not None else None

    @model_validator(mode="after")
    def _artifact_is_consistent(self) -> GraphRetrievalEvalResultArtifact:
        _require_utc(self.started_at, label="started_at")
        _require_utc(self.finished_at, label="finished_at")
        if self.finished_at < self.started_at:
            raise ValueError("finished_at must not precede started_at")
        policy_values = (self.policy_id, self.policy_canonical_sha256, self.policy_file_sha256)
        if self.phase == "calibration":
            if self.candidate_thresholds is None or any(value is not None for value in policy_values):
                raise ValueError("calibration requires candidates and no policy binding")
        elif self.candidate_thresholds is not None or any(value is None for value in policy_values):
            raise ValueError("post-freeze result requires policy binding and no candidates")
        if tuple(row.case_id for row in self.case_response_hashes) != tuple(
            sorted(row.case_id for row in self.case_response_hashes)
        ):
            raise ValueError("case response hashes must be sorted")
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in (*self.case_counts.values(), *self.stable_error_code_counts.values())):
            raise ValueError("artifact counts must be non-negative integers")
        if self.status == "passed":
            if (
                not self.metrics.passes_hard_gates()
                or self.case_counts.get("failed") != 0
                or not self.database_cleanup_succeeded
                or not self.performance
                or not self.explain
            ):
                raise ValueError("passed artifact does not satisfy hard gates")
        if self.candidate_thresholds is not None:
            for name, stats in self.performance.items():
                threshold = self.candidate_thresholds.max_p95_us.get(name)
                if threshold is None or not (stats.p95_us <= threshold <= 2_400_000):
                    raise ValueError("candidate p95 threshold does not cover calibration")
            for name, stats in self.explain.items():
                threshold = self.candidate_thresholds.max_scan_rows.get(name)
                if threshold is None or threshold < stats.scan_rows_observed:
                    raise ValueError("candidate scan threshold does not cover calibration")
        return self


class GraphRetrievalEvalPolicy(_StrictModel):
    schema_version: Literal["graph-retrieval-release-policy-v1"]
    policy_id: Literal["release_policy_v1"]
    contract_version: Literal["v1"]
    normalization_version: Literal["normalize_graph_name_v1"]
    response_canonicalization_version: Literal["graph-retrieval-response-canonical-v1"]
    implementation_tree_sha256: str
    dataset_manifest_sha256: str
    dataset_content_sha256: str
    evaluation_config_sha256: str
    environment_fingerprint_sha256: str
    calibration: GraphRetrievalEvalFileReference
    thresholds: GraphRetrievalEvalThresholds
    properties_exposed: Literal[False]
    approved_by: str = Field(min_length=1, max_length=128)
    approved_at: datetime
    approval_reference: str = Field(min_length=1, max_length=255)

    _tree_hash = field_validator("implementation_tree_sha256")(_validate_sha256)
    _manifest_hash = field_validator("dataset_manifest_sha256")(_validate_sha256)
    _content_hash = field_validator("dataset_content_sha256")(_validate_sha256)
    _config_hash = field_validator("evaluation_config_sha256")(_validate_sha256)
    _environment_hash = field_validator("environment_fingerprint_sha256")(_validate_sha256)

    @field_validator("approved_by", "approval_reference")
    @classmethod
    def _approval_text_is_safe(cls, value: str) -> str:
        value = value.strip()
        if not value or any(pattern.search(value) for pattern in _SECRET_PATTERNS):
            raise ValueError("approval metadata is blank or secret-like")
        return value

    @field_validator("approved_at")
    @classmethod
    def _approval_is_utc(cls, value: datetime) -> datetime:
        return _require_utc(value, label="approved_at")


class GraphRetrievalEvalReleaseEvidence(_StrictModel):
    schema_version: Literal["graph-retrieval-release-evidence-v1"]
    evidence_id: Literal["release_evidence_v1"]
    calibration: GraphRetrievalEvalFileReference
    policy: GraphRetrievalEvalFileReference
    post_freeze_runs: tuple[GraphRetrievalEvalFileReference, ...]

    @field_validator("post_freeze_runs", mode="before")
    @classmethod
    def _requires_three_runs(cls, value: Any) -> Any:
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError("post_freeze_runs must contain exactly 3 items")
        return value

    @model_validator(mode="after")
    def _references_are_distinct(self) -> GraphRetrievalEvalReleaseEvidence:
        paths = [
            self.calibration.path,
            self.policy.path,
            *(row.path for row in self.post_freeze_runs),
        ]
        if len(paths) != len(set(paths)):
            raise ValueError("release evidence references must be distinct")
        return self


@dataclass(frozen=True, slots=True)
class LoadedGraphRetrievalEvalPolicy:
    policy: GraphRetrievalEvalPolicy
    calibration: GraphRetrievalEvalResultArtifact
    policy_canonical_sha256: str
    policy_file_sha256: str


def _load_reference(
    root: Path,
    reference: GraphRetrievalEvalFileReference,
) -> tuple[Path, Any, bytes]:
    path = _resolve_scoped_path(root, reference.path)
    value, payload = _read_canonical_json(path)
    _assert_reference(reference, value, payload)
    return path, value, payload


def load_graph_retrieval_eval_policy(
    *,
    repository_root: Path,
    policy_path: Path,
) -> LoadedGraphRetrievalEvalPolicy:
    root = repository_root.resolve()
    expected = (root / "eval/graph_retrieval/release_policy_v1.json").resolve()
    if policy_path.resolve() != expected:
        raise ValueError("release policy must use the frozen repository path")
    policy_value, policy_bytes = _read_canonical_json(expected)
    policy = GraphRetrievalEvalPolicy.model_validate(policy_value)
    assert_sanitized_graph_retrieval_artifact(policy.model_dump(mode="json"))
    calibration_path, calibration_value, _calibration_bytes = _load_reference(root, policy.calibration)
    assert_sanitized_graph_retrieval_artifact(calibration_value)
    calibration = GraphRetrievalEvalResultArtifact.model_validate(calibration_value)
    if calibration_path.name != f"{calibration.run_id}.json":
        raise ValueError("calibration filename must match run_id")
    if calibration.phase != "calibration" or calibration.status != "passed" or calibration.policy_id is not None:
        raise ValueError("release policy requires a passed pre-policy calibration")
    if (
        calibration.implementation_tree_sha256 != policy.implementation_tree_sha256
        or calibration.dataset_manifest_sha256 != policy.dataset_manifest_sha256
        or calibration.dataset_content_sha256 != policy.dataset_content_sha256
        or calibration.evaluation_config_sha256 != policy.evaluation_config_sha256
        or calibration.environment_fingerprint_sha256 != policy.environment_fingerprint_sha256
    ):
        raise ValueError("release policy hashes do not match calibration")
    if policy.approved_at <= calibration.finished_at:
        raise ValueError("release policy approval must follow calibration")
    candidates = calibration.candidate_thresholds
    if candidates is None:
        raise ValueError("calibration is missing candidate thresholds")
    if not (
        set(policy.thresholds.max_p95_us)
        == set(candidates.max_p95_us)
        == set(calibration.performance)
    ):
        raise ValueError("approved p95 scenarios do not match calibration")
    if not (
        set(policy.thresholds.max_scan_rows)
        == set(candidates.max_scan_rows)
        == set(calibration.explain)
    ):
        raise ValueError("approved scan shapes do not match calibration")
    for name, threshold in policy.thresholds.max_p95_us.items():
        stats = calibration.performance.get(name)
        candidate = candidates.max_p95_us.get(name)
        if stats is None or candidate is None or not (stats.p95_us <= threshold <= candidate):
            raise ValueError("approved p95 threshold is outside calibration bounds")
    for name, threshold in policy.thresholds.max_scan_rows.items():
        stats = calibration.explain.get(name)
        candidate = candidates.max_scan_rows.get(name)
        if stats is None or candidate is None or not (stats.scan_rows_observed <= threshold <= candidate):
            raise ValueError("approved scan threshold is outside calibration bounds")
    return LoadedGraphRetrievalEvalPolicy(
        policy=policy,
        calibration=calibration,
        policy_canonical_sha256=canonical_json_sha256(policy_value),
        policy_file_sha256=file_sha256(policy_bytes),
    )


@dataclass(frozen=True, slots=True)
class LoadedGraphRetrievalReleaseEvidence:
    evidence: GraphRetrievalEvalReleaseEvidence
    policy: LoadedGraphRetrievalEvalPolicy
    calibration: GraphRetrievalEvalResultArtifact
    post_freeze_runs: tuple[
        GraphRetrievalEvalResultArtifact,
        GraphRetrievalEvalResultArtifact,
        GraphRetrievalEvalResultArtifact,
    ]


def load_graph_retrieval_release_evidence(
    *,
    repository_root: Path,
    evidence_path: Path,
) -> LoadedGraphRetrievalReleaseEvidence:
    root = repository_root.resolve()
    expected = (root / "eval/graph_retrieval/release_evidence_v1.json").resolve()
    if evidence_path.resolve() != expected:
        raise ValueError("release evidence must use the frozen repository path")
    try:
        evidence_value, _evidence_bytes = _read_canonical_json(expected)
    except ValueError as exc:
        raise ValueError("cannot load graph retrieval release evidence") from exc
    evidence = GraphRetrievalEvalReleaseEvidence.model_validate(evidence_value)
    assert_sanitized_graph_retrieval_artifact(evidence.model_dump(mode="json"))
    policy_path, policy_value, policy_bytes = _load_reference(root, evidence.policy)
    policy = load_graph_retrieval_eval_policy(repository_root=root, policy_path=policy_path)
    if (
        evidence.policy.canonical_sha256 != canonical_json_sha256(policy_value)
        or evidence.policy.file_sha256 != file_sha256(policy_bytes)
    ):
        raise ValueError("release evidence policy hashes are inconsistent")
    calibration_path, calibration_value, _ = _load_reference(root, evidence.calibration)
    calibration = GraphRetrievalEvalResultArtifact.model_validate(calibration_value)
    assert_sanitized_graph_retrieval_artifact(calibration_value)
    if calibration_path.name != f"{calibration.run_id}.json" or calibration != policy.calibration:
        raise ValueError("release evidence selected the wrong calibration")
    rows: list[GraphRetrievalEvalResultArtifact] = []
    for reference in evidence.post_freeze_runs:
        path, value, _ = _load_reference(root, reference)
        artifact = GraphRetrievalEvalResultArtifact.model_validate(value)
        assert_sanitized_graph_retrieval_artifact(value)
        if path.name != f"{artifact.run_id}.json" or artifact.phase != "post-freeze" or artifact.status != "passed":
            raise ValueError("release evidence contains an invalid post-freeze run")
        if (
            artifact.policy_id != policy.policy.policy_id
            or artifact.policy_canonical_sha256 != policy.policy_canonical_sha256
            or artifact.policy_file_sha256 != policy.policy_file_sha256
        ):
            raise ValueError("post-freeze run does not bind the frozen policy")
        if set(artifact.performance) != set(policy.policy.thresholds.max_p95_us) or any(
            stats.p95_us > policy.policy.thresholds.max_p95_us[name]
            for name, stats in artifact.performance.items()
        ):
            raise ValueError("post-freeze run violates frozen p95 thresholds")
        if set(artifact.explain) != set(policy.policy.thresholds.max_scan_rows) or any(
            stats.scan_rows_observed > policy.policy.thresholds.max_scan_rows[name]
            for name, stats in artifact.explain.items()
        ):
            raise ValueError("post-freeze run violates frozen scan thresholds")
        rows.append(artifact)
    if len({row.run_id for row in rows}) != 3 or len({row.database_id for row in rows}) != 3:
        raise ValueError("post-freeze run and database IDs must be distinct")
    selected = (calibration, *rows)
    for field in (
        "implementation_tree_sha256",
        "dataset_manifest_sha256",
        "dataset_content_sha256",
        "evaluation_config_sha256",
        "environment_fingerprint_sha256",
    ):
        if len({getattr(row, field) for row in selected}) != 1:
            raise ValueError(f"release artifacts disagree on {field}")
    if len({row.canonical_response_set_sha256 for row in selected}) != 1:
        raise ValueError("release canonical response-set hashes differ")
    if len(
        {
            tuple(item.case_id for item in row.case_response_hashes)
            for row in selected
        }
    ) != 1:
        raise ValueError("release case ID populations differ")
    return LoadedGraphRetrievalReleaseEvidence(
        evidence=evidence,
        policy=policy,
        calibration=calibration,
        post_freeze_runs=(rows[0], rows[1], rows[2]),
    )


def write_canonical_json_file(path: Path, value: Any) -> None:
    payload = _canonical_json_bytes(value)
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor = os.open(path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        os.close(descriptor)
