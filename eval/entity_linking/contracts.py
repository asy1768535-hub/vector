from __future__ import annotations

import hashlib
import json
import math
import re
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_validator

from app.services.graph_canonical import canonical_graph_json_v1


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
GIT_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
LOGICAL_KEY_RE = re.compile(r"^[a-z][a-z0-9-]{0,79}$")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
DATABASE_ID_RE = re.compile(r"^vkt_v07_el_eval_[a-z0-9_]{1,80}$")
UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$")

Sha256 = Annotated[str, Field(pattern=SHA256_RE.pattern)]
GitCommit = Annotated[str, Field(pattern=GIT_COMMIT_RE.pattern)]
LogicalKey = Annotated[str, Field(pattern=LOGICAL_KEY_RE.pattern)]
RunId = Annotated[str, Field(pattern=RUN_ID_RE.pattern)]
DatabaseId = Annotated[str, Field(pattern=DATABASE_ID_RE.pattern)]
UtcTimestamp = Annotated[str, Field(pattern=UTC_RE.pattern)]

CATEGORY_ORDER = (
    "exact-canonical",
    "case-whitespace-normalization",
    "prefix-suffix-omission",
    "abbreviation-like-overlap",
    "word-order-token-overlap",
    "close-name-negative",
    "same-score-ambiguity",
    "same-name-cross-type-ambiguity",
    "unpublished-cross-scope",
    "not-found",
    "one-hop-evidence-utility",
    "two-hop-evidence-utility",
)
STRATUM_ORDER = (
    "one-hop-cjk",
    "one-hop-latin-mixed",
    "two-hop-cjk",
    "two-hop-latin-mixed",
)
EXTERNAL_DISTRIBUTION_VERSIONS = (
    ("alembic", "1.18.4"),
    ("asyncpg", "0.31.0"),
    ("fastapi-users-db-sqlalchemy", "7.0.0"),
    ("httpx", "0.28.1"),
    ("psycopg2-binary", "2.9.12"),
    ("pydantic", "2.13.4"),
    ("pydantic-settings", "2.14.1"),
    ("pytest", "9.1.0"),
    ("pytest-asyncio", "1.4.0"),
    ("python-dotenv", "1.2.2"),
    ("ruff", "0.15.17"),
    ("sqlalchemy", "2.0.51"),
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class ArtifactRef(StrictModel):
    repository_relative_path: str = Field(min_length=1, max_length=240)
    canonical_sha256: Sha256
    exact_file_sha256: Sha256


class LibraryRecord(StrictModel):
    library_key: LogicalKey
    slug: LogicalKey
    name: str = Field(min_length=1, max_length=128)
    qdrant_collection_key: LogicalKey
    embedding_model: Literal["bge-m3"]
    embedding_dim: Literal[1024]
    retrieval_mode: Literal["dense", "hybrid"]


class OntologyRecord(StrictModel):
    ontology_key: LogicalKey
    library_key: LogicalKey
    version_key: LogicalKey
    version_no: int = Field(ge=1)
    status: Literal["active"]


class EntityTypeRecord(StrictModel):
    entity_type_key: LogicalKey
    library_key: LogicalKey
    ontology_key: LogicalKey
    key: LogicalKey
    label: str = Field(min_length=1, max_length=128)
    status: Literal["active"]


class RelationTypeRecord(StrictModel):
    relation_type_key: LogicalKey
    library_key: LogicalKey
    ontology_key: LogicalKey
    key: LogicalKey
    label: str = Field(min_length=1, max_length=128)
    direction: Literal["directed", "undirected"]
    requires_evidence: bool
    status: Literal["active"]


class DocumentRecord(StrictModel):
    document_key: LogicalKey
    library_key: LogicalKey
    title: str = Field(min_length=1, max_length=256)
    external_id: str = Field(min_length=1, max_length=128)
    status: Literal["ready"]


class RevisionRecord(StrictModel):
    revision_key: LogicalKey
    document_key: LogicalKey
    revision_no: int = Field(ge=1)
    status: Literal["ready"]
    is_current: bool


class ChunkRecord(StrictModel):
    chunk_key: LogicalKey
    document_key: LogicalKey
    revision_key: LogicalKey
    seq: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=4000)


class EvidenceRecord(StrictModel):
    evidence_key: LogicalKey
    library_key: LogicalKey
    document_key: LogicalKey
    revision_key: LogicalKey
    chunk_key: LogicalKey
    locator_start: int = Field(ge=0)
    locator_end: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_locator(self) -> EvidenceRecord:
        if self.locator_end <= self.locator_start:
            raise ValueError("locator_end must be greater than locator_start")
        return self


class EntityRecord(StrictModel):
    entity_key: LogicalKey
    library_key: LogicalKey
    ontology_key: LogicalKey
    entity_type_key: LogicalKey
    canonical_name: str = Field(min_length=1, max_length=512)
    normalized_name: str = Field(min_length=1, max_length=512)
    status: Literal["published", "unpublished"]
    support_evidence_keys: tuple[LogicalKey, ...]
    privacy_canary_keys: tuple[LogicalKey, ...]


class RelationRecord(StrictModel):
    relation_key: LogicalKey
    library_key: LogicalKey
    ontology_key: LogicalKey
    relation_type_key: LogicalKey
    source_entity_key: LogicalKey
    target_entity_key: LogicalKey
    status: Literal["published", "unpublished"]
    support_evidence_keys: tuple[LogicalKey, ...]
    privacy_canary_keys: tuple[LogicalKey, ...]


class PublicationRecord(StrictModel):
    publication_key: LogicalKey
    library_key: LogicalKey
    ontology_key: LogicalKey
    status: Literal["active", "superseded", "failed"]
    entity_keys: tuple[LogicalKey, ...]
    relation_keys: tuple[LogicalKey, ...]
    scenario: Literal["healthy-primary", "healthy-negative", "degraded", "partial", "superseded"]


class PrivacyCanaryRecord(StrictModel):
    canary_key: LogicalKey
    key_marker: str = Field(min_length=4, max_length=96)
    value_marker: str = Field(min_length=4, max_length=96)
    target_rows: tuple[LogicalKey, ...]


class GoldDataset(StrictModel):
    schema_version: Literal["entity-linking-gold-v1"]
    dataset_id: Literal["feasibility-v1"]
    uuid_namespace: str
    libraries: tuple[LibraryRecord, ...]
    ontologies: tuple[OntologyRecord, ...]
    entity_types: tuple[EntityTypeRecord, ...]
    relation_types: tuple[RelationTypeRecord, ...]
    documents: tuple[DocumentRecord, ...]
    revisions: tuple[RevisionRecord, ...]
    chunks: tuple[ChunkRecord, ...]
    evidence: tuple[EvidenceRecord, ...]
    entities: tuple[EntityRecord, ...]
    relations: tuple[RelationRecord, ...]
    publications: tuple[PublicationRecord, ...]
    privacy_canaries: tuple[PrivacyCanaryRecord, ...]


class MentionRecord(StrictModel):
    input_index: int = Field(ge=0, le=9)
    text: str = Field(min_length=1, max_length=512)
    entity_type_key: LogicalKey | None
    gold_status: Literal["linkable", "ambiguous", "unlinkable"]
    gold_entity_key: LogicalKey | None
    gold_ambiguous_entity_keys: tuple[LogicalKey, ...]
    negative_entity_keys: tuple[LogicalKey, ...]
    is_exact_control: bool
    script: Literal["cjk", "latin", "mixed"]

    @model_validator(mode="after")
    def validate_gold_shape(self) -> MentionRecord:
        if self.gold_status == "linkable":
            if self.gold_entity_key is None or self.gold_ambiguous_entity_keys:
                raise ValueError("linkable mention requires exactly one gold Entity")
        elif self.gold_status == "ambiguous":
            if self.gold_entity_key is not None or len(self.gold_ambiguous_entity_keys) < 2:
                raise ValueError("ambiguous mention requires at least two candidates")
        elif self.gold_entity_key is not None or self.gold_ambiguous_entity_keys:
            raise ValueError("unlinkable mention cannot select an Entity")
        return self


class GoldUtilityRecord(StrictModel):
    evidence_keys: tuple[LogicalKey, ...]
    relation_keys: tuple[LogicalKey, ...]
    node_keys: tuple[LogicalKey, ...]
    max_evidence_budget: Literal[10]
    hop: Literal[1, 2]


class EvaluationCase(StrictModel):
    schema_version: Literal["entity-linking-case-v1"]
    case_id: LogicalKey
    split: Literal["calibration", "release"]
    entity_family_keys: tuple[LogicalKey, ...]
    mention_family_keys: tuple[LogicalKey, ...]
    relation_template_family_key: LogicalKey
    categories: tuple[str, ...]
    utility_stratum: Literal["one-hop-cjk", "one-hop-latin-mixed", "two-hop-cjk", "two-hop-latin-mixed"]
    scenario_publication_key: LogicalKey
    question: str = Field(min_length=1, max_length=1000)
    mentions: tuple[MentionRecord, ...] = Field(min_length=1, max_length=10)
    gold_utility: GoldUtilityRecord

    @model_validator(mode="after")
    def validate_case_shape(self) -> EvaluationCase:
        if tuple(row.input_index for row in self.mentions) != tuple(range(len(self.mentions))):
            raise ValueError("mention input_index must be contiguous")
        category_positions = [CATEGORY_ORDER.index(value) for value in self.categories]
        if category_positions != sorted(set(category_positions)):
            raise ValueError("categories must be unique and in frozen order")
        expected_hop = 1 if self.utility_stratum.startswith("one-hop") else 2
        if self.gold_utility.hop != expected_hop:
            raise ValueError("utility stratum/hop mismatch")
        return self


class FeatureExpected(StrictModel):
    character_bigram_dice_micros: int = Field(ge=0, le=1_000_000)
    token_jaccard_micros: int = Field(ge=0, le=1_000_000)
    substring_containment_micros: int = Field(ge=0, le=1_000_000)
    score_micros: int = Field(ge=0, le=1_000_000)


class FeatureCase(StrictModel):
    case_id: LogicalKey
    left: str = Field(min_length=1)
    right: str = Field(min_length=1)
    expected: FeatureExpected


class CandidateFixture(StrictModel):
    entity_id: str
    entity_key: LogicalKey
    entity_type_key: LogicalKey
    canonical_name: str
    normalized_name: str


class DecisionExpected(StrictModel):
    status: Literal["linked", "ambiguous", "not_found"]
    method: Literal["exact_canonical", "lexical_v1"] | None
    selected_entity_key: LogicalKey | None
    candidate_entity_keys: tuple[LogicalKey, ...]


class DecisionCase(StrictModel):
    case_id: LogicalKey
    mention_text: str
    entity_type_key: LogicalKey | None
    min_score_micros: int = Field(ge=0, le=1_000_000)
    min_margin_micros: int = Field(ge=0, le=1_000_000)
    candidates: tuple[CandidateFixture, ...]
    candidate_scores_micros: tuple[int, ...] | None
    expected: DecisionExpected

    @model_validator(mode="after")
    def validate_scores(self) -> DecisionCase:
        if self.candidate_scores_micros is not None and len(self.candidate_scores_micros) != len(
            self.candidates
        ):
            raise ValueError("candidate score vector length mismatch")
        return self


class OrderingCase(StrictModel):
    case_id: LogicalKey
    mention_text: str
    candidates: tuple[CandidateFixture, ...]
    expected_entity_keys: tuple[LogicalKey, ...]


class CategoryPredicateCase(StrictModel):
    case_id: LogicalKey
    predicate: str
    expected: bool
    mention_text: str
    canonical_name: str
    gold_status: Literal["linkable", "ambiguous", "unlinkable"]
    exact_match_count: int = Field(ge=0)
    presented_scores_micros: tuple[int, ...]
    entity_type_key: LogicalKey | None
    candidate_type_keys: tuple[LogicalKey, ...]
    negative_entity_keys: tuple[LogicalKey, ...]
    hop: Literal[1, 2]
    expected_categories: tuple[str, ...]


class ConformanceDataset(StrictModel):
    schema_version: Literal["entity-linking-scorer-conformance-v1"]
    algorithm_version: Literal["lexical-score-v1"]
    normalization_version: Literal["normalize_graph_name_v1"]
    rounding_version: Literal["integer-half-up-v1"]
    feature_cases: tuple[FeatureCase, ...]
    decision_cases: tuple[DecisionCase, ...]
    ordering_cases: tuple[OrderingCase, ...]
    category_predicate_cases: tuple[CategoryPredicateCase, ...]


class CountsRecord(StrictModel):
    questions: int = Field(ge=200)
    mentions: int = Field(ge=300)
    linkable_mentions: int = Field(ge=240)
    ambiguous_unlinkable_mentions: int = Field(ge=60)
    exact_canonical_mentions: int = Field(ge=40)
    cjk_non_exact_linkable_mentions: int = Field(ge=40)
    latin_mixed_non_exact_linkable_mentions: int = Field(ge=40)
    libraries: int = Field(ge=2)
    ontologies: int = Field(ge=2)
    entities: int = Field(ge=1)
    relations: int = Field(ge=1)
    evidence: int = Field(ge=1)
    documents: int = Field(ge=1)
    chunks: int = Field(ge=1)
    calibration_cases: int = Field(ge=1)
    release_cases: int = Field(ge=1)


class MinimumCountsRecord(StrictModel):
    relation_oriented_questions: Literal[200]
    linkable_mentions: Literal[240]
    ambiguous_unlinkable_mentions: Literal[60]
    exact_canonical_controls: Literal[40]
    cjk_non_exact_linkable_mentions: Literal[40]
    latin_mixed_non_exact_linkable_mentions: Literal[40]
    libraries: Literal[2]
    ontologies: Literal[2]
    release_category_cases: Literal[20]
    release_stratum_questions: Literal[20]


class CategoryCounts(RootModel[dict[str, int]]):
    @model_validator(mode="after")
    def validate_keys(self) -> CategoryCounts:
        if set(self.root) != set(CATEGORY_ORDER) or any(value < 0 for value in self.root.values()):
            raise ValueError("category counts must use exact frozen keys")
        return self


class StratumCounts(RootModel[dict[str, int]]):
    @model_validator(mode="after")
    def validate_keys(self) -> StratumCounts:
        if set(self.root) != set(STRATUM_ORDER) or any(value < 0 for value in self.root.values()):
            raise ValueError("stratum counts must use exact frozen keys")
        return self


class Threshold(StrictModel):
    min_score_micros: Literal[850000, 880000, 900000, 920000, 950000]
    min_margin_micros: Literal[80000, 100000, 120000, 150000, 200000]
    candidate_floor_micros: Literal[500000]
    max_candidates: Literal[10]


class ControlConfig(StrictModel):
    exact_only_normalizer: Literal["normalize_graph_name_v1"]
    dense_retrieval_mode: Literal["dense"]
    hybrid_retrieval_mode: Literal["hybrid"]
    top_k: Literal[10]
    score_threshold_micros: Literal[0]
    metadata_condition: None
    source_config: None
    rerank_enabled: Literal[False]
    query_rewrite_enabled: Literal[False]
    query_rewrite_llm_enabled: Literal[False]
    embedding_model: Literal["bge-m3"]
    embedding_dimension: Literal[1024]
    hybrid_candidate_k: Literal[50]
    hybrid_rrf_k: Literal[60]
    hybrid_keyword_threshold_micros: Literal[300000]
    hybrid_keyword_title_boost_micros: Literal[1500000]
    hybrid_keyword_external_id_boost_micros: Literal[2000000]
    enable_revision_id_visibility: Literal[False]


class MetricConfig(StrictModel):
    version: Literal["entity-linking-metrics-v1"]
    required_mentions_all_or_nothing: Literal[True]
    evidence_top_k: Literal[10]
    rate_rounding: Literal["integer-half-up-v1"]
    best_control_tie: Literal["dense"]


class ThresholdGrid(StrictModel):
    min_score_micros: tuple[Literal[850000, 880000, 900000, 920000, 950000], ...]
    min_margin_micros: tuple[Literal[80000, 100000, 120000, 150000, 200000], ...]
    candidate_floor_micros: Literal[500000]
    max_candidates: Literal[10]


class BootstrapConfig(StrictModel):
    algorithm_version: Literal["paired-stratified-bootstrap-v1"]
    prng: Literal["python-random-mt19937"]
    seed: Literal[2026071707]
    replicates: Literal[10000]
    strata: tuple[str, ...]
    lower_index: Literal[249]
    upper_index: Literal[9749]


class PerformanceFixture(StrictModel):
    generator_version: Literal["entity-linking-performance-fixture-v1"]
    publication_total_items: Literal[10000]
    publication_entities: Literal[6000]
    publication_relations: Literal[4000]
    mention_count: Literal[10]
    mention_mix: Literal["2 exact, 3 high-similarity, 3 ambiguous, 2 not-found"]
    warmup_count: Literal[5]
    sample_count: Literal[30]
    timeout_micros: Literal[2000000]
    max_candidates_per_mention: Literal[10]


class FileHashRecord(StrictModel):
    repository_relative_path: str
    exact_file_sha256: Sha256


class ScopeSchemaHash(StrictModel):
    logical_scope_id: LogicalKey
    ontology_schema_hash: Sha256


class ExternalDistributionRecord(StrictModel):
    distribution_name: Literal[
        "alembic",
        "asyncpg",
        "fastapi-users-db-sqlalchemy",
        "httpx",
        "psycopg2-binary",
        "pydantic",
        "pydantic-settings",
        "python-dotenv",
        "pytest",
        "pytest-asyncio",
        "ruff",
        "sqlalchemy",
    ]
    exact_version: str

    @model_validator(mode="after")
    def validate_paired_version(self) -> ExternalDistributionRecord:
        expected = dict(EXTERNAL_DISTRIBUTION_VERSIONS)[self.distribution_name]
        if self.exact_version != expected:
            raise ValueError("external distribution version mismatch")
        return self


def _validate_external_distribution_records(
    records: tuple[ExternalDistributionRecord, ...],
) -> None:
    observed = tuple((record.distribution_name, record.exact_version) for record in records)
    if observed != EXTERNAL_DISTRIBUTION_VERSIONS:
        raise ValueError("external distribution records must match the exact ordered set")


class FeasibilityManifest(StrictModel):
    schema_version: Literal["entity-linking-feasibility-manifest-v1"]
    dataset_id: Literal["feasibility-v1"]
    g2_approval_commit: GitCommit
    g2_specification_tree_sha256: Sha256
    gold_ref: ArtifactRef
    cases_ref: ArtifactRef
    conformance_ref: ArtifactRef
    canonicalization_version: Literal["canonical-graph-json-v1"]
    normalization_version: Literal["normalize_graph_name_v1"]
    algorithm_version: Literal["lexical-score-v1"]
    rounding_version: Literal["integer-half-up-v1"]
    category_predicate_version: Literal["entity-linking-category-predicates-v1"]
    ordered_case_ids: tuple[LogicalKey, ...]
    ordered_calibration_case_ids: tuple[LogicalKey, ...]
    ordered_release_case_ids: tuple[LogicalKey, ...]
    counts: CountsRecord
    minimum_counts: MinimumCountsRecord
    release_category_counts: CategoryCounts
    release_stratum_counts: StratumCounts
    entity_family_split_hash: Sha256
    mention_family_split_hash: Sha256
    relation_template_family_split_hash: Sha256
    logical_scope_schema_hashes: tuple[ScopeSchemaHash, ...]
    ontology_schema_set_hash: Sha256
    control_config: ControlConfig
    metric_config: MetricConfig
    threshold_grid: ThresholdGrid
    bootstrap_config: BootstrapConfig
    performance_fixture: PerformanceFixture
    dependency_closure_algorithm: Literal["app-python-ast-import-closure-v1"]
    dependency_root_modules: tuple[str, ...]
    accepted_dependency_closure_records: tuple[FileHashRecord, ...]
    accepted_dependency_closure_sha256: Sha256
    accepted_dependency_closure_path_count: Literal[63]
    external_distribution_identity_version: Literal["external-distribution-identity-v1"]
    external_distribution_records: tuple[ExternalDistributionRecord, ...] = Field(
        min_length=12, max_length=12
    )
    external_distribution_set_sha256: Sha256

    @model_validator(mode="after")
    def validate_external_distributions(self) -> FeasibilityManifest:
        _validate_external_distribution_records(self.external_distribution_records)
        records = [record.model_dump(mode="json") for record in self.external_distribution_records]
        if canonical_sha256(records) != self.external_distribution_set_sha256:
            raise ValueError("external distribution set hash mismatch")
        return self


class ReducedRational(StrictModel):
    numerator: int
    denominator: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_reduced(self) -> ReducedRational:
        if math.gcd(abs(self.numerator), self.denominator) != 1:
            raise ValueError("rational must be reduced")
        if self.numerator == 0 and self.denominator != 1:
            raise ValueError("zero must be represented as 0/1")
        return self


class RateMetric(StrictModel):
    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    value_micros: int | None = Field(default=None, ge=0, le=1_000_000)

    @model_validator(mode="after")
    def validate_rate(self) -> RateMetric:
        if self.denominator == 0:
            if self.numerator != 0 or self.value_micros is not None:
                raise ValueError("empty rate must be 0/0/null")
        else:
            expected = (2 * self.numerator * 1_000_000 + self.denominator) // (2 * self.denominator)
            if self.numerator > self.denominator or self.value_micros != expected:
                raise ValueError("rate value invariant failed")
        return self


class SignedGain(StrictModel):
    control_used: Literal["dense", "hybrid", "exact-only"]
    candidate_value_micros: int = Field(ge=0, le=1_000_000)
    control_value_micros: int = Field(ge=0, le=1_000_000)
    gain_micros: int = Field(ge=-1_000_000, le=1_000_000)
    required_gain_micros: int = Field(ge=-1_000_000, le=1_000_000)
    comparison: Literal["ge"]
    passed: bool

    @model_validator(mode="after")
    def validate_gain(self) -> SignedGain:
        gain = self.candidate_value_micros - self.control_value_micros
        if self.gain_micros != gain or self.passed != (gain >= self.required_gain_micros):
            raise ValueError("gain invariant failed")
        return self


class GridResult(StrictModel):
    grid_index: int = Field(ge=0, le=24)
    thresholds: Threshold
    wrong_auto_link_count: int = Field(ge=0)
    ambiguous_unlinkable_false_auto_link_count: int = Field(ge=0)
    privacy_leak_count: int = Field(ge=0)
    publication_membership_failures: int = Field(ge=0)
    auto_link_precision: RateMetric
    link_recall: RateMetric
    non_exact_link_recall: RateMetric
    candidate_recall_at_5: RateMetric
    abstention_accuracy: RateMetric
    exact_regression_accuracy: RateMetric
    scope_safety: RateMetric
    property_privacy: RateMetric
    non_exact_correct_auto_link_count: int = Field(ge=0)
    selection_eligible: bool


class IntrinsicMetrics(StrictModel):
    auto_link_precision: RateMetric
    link_recall: RateMetric
    non_exact_link_recall: RateMetric
    candidate_recall_at_5: RateMetric
    abstention_accuracy: RateMetric
    exact_regression_accuracy: RateMetric
    scope_safety: RateMetric
    determinism: RateMetric
    property_privacy: RateMetric
    exact_only_link_recall: RateMetric
    link_recall_gain_vs_exact_only: SignedGain
    wrong_auto_link_count: int = Field(ge=0)
    ambiguous_unlinkable_false_auto_link_count: int = Field(ge=0)
    publication_membership_failures: int = Field(ge=0)
    privacy_leak_count: int = Field(ge=0)


class UtilityMetrics(StrictModel):
    evidence_recall_at_10: RateMetric
    evidence_precision_at_10: RateMetric
    complete_support_set_coverage: RateMetric
    question_with_any_gold_evidence: RateMetric
    relation_fact_recall: RateMetric
    node_recall: RateMetric


class BootstrapResult(StrictModel):
    algorithm_version: Literal["paired-stratified-bootstrap-v1"]
    prng: Literal["python-random-mt19937"]
    seed: Literal[2026071707]
    replicates: Literal[10000]
    strata: tuple[str, ...]
    best_control: Literal["dense", "hybrid"]
    point_estimate_micros: ReducedRational
    lower_index: Literal[249]
    upper_index: Literal[9749]
    ci_lower_micros: ReducedRational
    ci_upper_micros: ReducedRational
    replicate_statistics_sha256: Sha256
    passed: bool

    @model_validator(mode="after")
    def validate_passed(self) -> BootstrapResult:
        if self.passed != (self.ci_lower_micros.numerator > 0):
            raise ValueError("bootstrap passed invariant failed")
        return self


class UtilityGains(StrictModel):
    evidence_recall_gain: SignedGain
    complete_support_set_coverage_gain: SignedGain
    evidence_precision_regression: SignedGain
    question_with_any_gold_evidence_gain: SignedGain
    bootstrap: BootstrapResult


class Metrics(StrictModel):
    intrinsic: IntrinsicMetrics
    candidate_utility: UtilityMetrics
    dense_utility: UtilityMetrics
    hybrid_utility: UtilityMetrics
    utility_gains: UtilityGains


class LatencySummary(StrictModel):
    warmup_count: Literal[5]
    sample_count: Literal[30]
    samples_us: tuple[int, ...] = Field(min_length=30, max_length=30)
    p50_us: int = Field(ge=0)
    p95_us: int = Field(ge=0)
    max_us: int = Field(ge=0)


class Performance(StrictModel):
    linker: LatencySummary
    link_graph: LatencySummary
    dense: LatencySummary
    hybrid: LatencySummary
    sql_statement_count_max: int = Field(ge=0)
    per_mention_sql_count: Literal[0]
    projection_rows: int = Field(ge=0)
    publication_entity_count: int = Field(ge=0, le=10000)
    memory_high_water_bytes: int = Field(ge=0)
    request_timeout_count: int = Field(ge=0)
    timeout_rollback_reused: bool
    embedding_call_count: int = Field(gt=0)
    qdrant_call_count: int = Field(gt=0)


class QdrantCollectionConfig(StrictModel):
    config_version: Literal["entity-linking-qdrant-collection-config-v1"]
    vector_size: Literal[1024]
    distance: Literal["Cosine"]
    shard_number: Literal[1]
    hnsw_m: Literal[16]
    hnsw_ef_construct: Literal[128]
    scalar_type: Literal["int8"]
    scalar_quantile: ReducedRational
    scalar_always_ram: Literal[True]


class QdrantIdentity(StrictModel):
    identity_version: Literal["entity-linking-qdrant-identity-v1"]
    endpoint_origin_version: Literal["service-origin-v1"]
    endpoint_origin_sha256: Sha256
    server_version: str = Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
    collection_config: QdrantCollectionConfig
    collection_config_sha256: Sha256
    qdrant_fingerprint_sha256: Sha256


class QdrantCollection(StrictModel):
    derivation_version: Literal["entity-linking-qdrant-collection-name-v1"]
    phase_token: Literal["cal", "pf1", "pf2", "pf3"]
    run_id_sha256: Sha256
    collection_name: str = Field(pattern=r"^vkt_v07_el_(cal|pf1|pf2|pf3)_[0-9a-f]{12}$")
    collection_name_sha256: Sha256


class EmbeddingIdentity(StrictModel):
    identity_version: Literal["entity-linking-embedding-identity-v1"]
    endpoint_origin_version: Literal["service-origin-v1"]
    endpoint_origin_sha256: Sha256
    model: Literal["bge-m3"]
    dimension: Literal[1024]
    probe_text_sha256: Literal["4caad60c112bd93fda55714c91aef2762a3c5c5c0df2a09dc28e6296800cc61f"]
    vector_encoding_version: Literal["ieee754-binary64-be-v1"]
    probe_vector_sha256: Sha256
    embedding_fingerprint_sha256: Sha256


class EnvironmentRecord(StrictModel):
    schema_version: Literal["entity-linking-environment-v1"]
    python_version: str
    platform_system: str
    platform_release: str
    platform_machine: str
    logical_cpu_count: int = Field(gt=0)
    external_distribution_identity_version: Literal["external-distribution-identity-v1"]
    external_distribution_records: tuple[ExternalDistributionRecord, ...] = Field(
        min_length=12, max_length=12
    )
    external_distribution_set_sha256: Sha256
    pg_cluster_fingerprint_sha256: Sha256
    pg_server_version_num: int = Field(gt=0)
    pg_trgm_version: str
    qdrant: QdrantIdentity
    embedding: EmbeddingIdentity
    evaluation_config_sha256: Sha256
    accepted_dependency_closure_sha256: Sha256
    reference_scorer_sha256: Sha256
    environment_fingerprint_sha256: Sha256

    @model_validator(mode="after")
    def validate_external_distributions(self) -> EnvironmentRecord:
        _validate_external_distribution_records(self.external_distribution_records)
        records = [record.model_dump(mode="json") for record in self.external_distribution_records]
        if canonical_sha256(records) != self.external_distribution_set_sha256:
            raise ValueError("external distribution set hash mismatch")
        return self


class CaseResponseHash(StrictModel):
    case_id: LogicalKey
    input_count: int = Field(ge=1, le=10)
    candidate_logical_response_sha256: Sha256
    exact_only_logical_response_sha256: Sha256
    dense_evidence_set_sha256: Sha256
    hybrid_evidence_set_sha256: Sha256


class CalibrationArtifact(StrictModel):
    schema_version: Literal["entity-linking-eval-result-v1"]
    phase: Literal["calibration"]
    status: Literal["passed", "no_go"]
    ordinal: None
    run_id: RunId
    database_id: DatabaseId
    started_at: UtcTimestamp
    finished_at: UtcTimestamp
    g2_approval_commit: GitCommit
    g2_specification_tree_sha256: Sha256
    code_commit: GitCommit
    evaluation_tree_sha256: Sha256
    accepted_dependency_closure_sha256: Sha256
    reference_scorer_sha256: Sha256
    external_distribution_set_sha256: Sha256
    dataset_manifest_ref: ArtifactRef
    dataset_content_sha256: Sha256
    evaluation_config_sha256: Sha256
    ontology_schema_set_hash: Sha256
    environment_fingerprint_sha256: Sha256
    environment: EnvironmentRecord
    pg_cluster_fingerprint_sha256: Sha256
    qdrant_fingerprint_sha256: Sha256
    embedding_fingerprint_sha256: Sha256
    qdrant_collection: QdrantCollection
    control_config_sha256: Sha256
    grid_results: tuple[GridResult, ...] = Field(min_length=25, max_length=25)
    candidate_thresholds: Threshold | None
    selection_reason: Literal[
        "max-non-exact-coverage", "tie-higher-score", "tie-higher-margin", "no-valid-candidate"
    ]
    metrics: Metrics
    category_counts: CategoryCounts
    stratum_counts: StratumCounts
    performance: Performance
    ordered_response_hashes: tuple[CaseResponseHash, ...]
    canonical_response_set_sha256: Sha256
    database_created: Literal[True]
    database_cleanup_succeeded: Literal[True]
    qdrant_collection_created: Literal[True]
    qdrant_cleanup_succeeded: Literal[True]
    policy_ref: None


def canonical_sha256(value: Any) -> str:
    def json_value(item: Any) -> Any:
        if isinstance(item, BaseModel):
            return json_value(item.model_dump(mode="json", by_alias=True))
        if isinstance(item, tuple):
            return [json_value(value) for value in item]
        if isinstance(item, list):
            return [json_value(value) for value in item]
        if isinstance(item, dict):
            return {str(key): json_value(value) for key, value in item.items()}
        return item

    return hashlib.sha256(canonical_graph_json_v1(json_value(value)).encode("utf-8")).hexdigest()


def exact_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def parse_json_bytes(payload: bytes) -> Any:
    if payload.startswith(b"\xef\xbb\xbf") or b"\r" in payload:
        raise ValueError("BOM/CRLF forbidden")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("invalid UTF-8") from exc
    return json.loads(
        text,
        object_pairs_hook=_reject_duplicate_keys,
        parse_constant=_reject_constant,
    )


def _resolve_safe(root: Path, path: Path) -> Path:
    if path.is_symlink():
        raise ValueError("symlink forbidden")
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("path escapes repository root")
    return resolved


def load_canonical_json(root: Path, path: Path, model: type[StrictModel]) -> StrictModel:
    resolved = _resolve_safe(root, path)
    payload = resolved.read_bytes()
    if not payload.endswith(b"\n") or payload.endswith(b"\n\n"):
        raise ValueError("canonical JSON file requires one trailing LF")
    value = parse_json_bytes(payload[:-1])
    parsed = model.model_validate(value)
    expected = canonical_graph_json_v1(parsed.model_dump(mode="json", by_alias=True)).encode("utf-8") + b"\n"
    if payload != expected:
        raise ValueError("noncanonical JSON bytes")
    return parsed


def load_canonical_jsonl(root: Path, path: Path, model: type[StrictModel]) -> tuple[StrictModel, ...]:
    resolved = _resolve_safe(root, path)
    payload = resolved.read_bytes()
    if not payload.endswith(b"\n") or b"\r" in payload or b"\n\n" in payload:
        raise ValueError("noncanonical JSONL framing")
    result: list[StrictModel] = []
    expected_lines: list[bytes] = []
    for raw_line in payload.splitlines():
        value = parse_json_bytes(raw_line)
        parsed = model.model_validate(value)
        result.append(parsed)
        expected_lines.append(
            canonical_graph_json_v1(parsed.model_dump(mode="json", by_alias=True)).encode("utf-8")
        )
    if payload != b"\n".join(expected_lines) + b"\n":
        raise ValueError("noncanonical JSONL bytes")
    return tuple(result)


def artifact_ref(root: Path, path: Path, parsed_value: Any) -> ArtifactRef:
    resolved = _resolve_safe(root, path)
    return ArtifactRef(
        repository_relative_path=resolved.relative_to(root.resolve()).as_posix(),
        canonical_sha256=canonical_sha256(parsed_value),
        exact_file_sha256=exact_file_sha256(resolved),
    )
