from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, RootModel, model_validator

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


def _validate_utc_timestamp(value: str) -> str:
    datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    return value


UtcTimestamp = Annotated[
    str,
    Field(pattern=UTC_RE.pattern),
    AfterValidator(_validate_utc_timestamp),
]

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
GATE_ID_ORDER = (
    "wrong-auto-link-count",
    "auto-link-precision",
    "exact-regression-accuracy",
    "scope-safety",
    "property-privacy",
    "privacy-leak-count",
    "publication-membership-failures",
    "ambiguous-unlinkable-false-auto-links",
    "candidate-recall-at-5",
    "overall-link-recall",
    "non-exact-link-recall",
    "link-recall-gain-vs-exact",
    "abstention-accuracy",
    "evidence-recall-gain",
    "complete-support-gain",
    "evidence-precision-regression",
    "any-gold-gain",
    "bootstrap-ci-lower",
    "linker-p95",
    "link-graph-p95",
    "link-graph-ratio",
    "timeout-count",
    "sql-statement-budget",
    "per-mention-sql",
    "projection-rows",
    "deterministic-response-hash",
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
    schema_version: Literal["entity-linking-gold-v2"]
    dataset_id: Literal["feasibility-v3"]
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
    schema_version: Literal["entity-linking-case-v2"]
    case_id: LogicalKey
    split: Literal["calibration", "release"]
    cohort: Literal["safety", "utility"]
    entity_family_keys: tuple[LogicalKey, ...]
    mention_family_keys: tuple[LogicalKey, ...]
    relation_template_family_key: LogicalKey
    phrase_family_key: LogicalKey
    decoy_family_keys: tuple[LogicalKey, ...]
    decoy_chunk_keys: tuple[LogicalKey, ...]
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
        if self.cohort == "utility" and any(
            mention.gold_status != "linkable" for mention in self.mentions
        ):
            raise ValueError("utility cases require only linkable mentions")
        return self


class FeatureExpected(StrictModel):
    character_bigram_dice_micros: int = Field(ge=0, le=1_000_000)
    token_jaccard_micros: int = Field(ge=0, le=1_000_000)
    substring_containment_micros: int = Field(ge=0, le=1_000_000)
    boundary_omission_micros: int = Field(ge=0, le=1_000_000)
    ordered_abbreviation_micros: int = Field(ge=0, le=1_000_000)
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
    method: Literal["exact_canonical", "lexical_v2"] | None
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
    schema_version: Literal["entity-linking-scorer-conformance-v2"]
    algorithm_version: Literal["lexical-score-v2"]
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
    calibration_safety_cases: Literal[40]
    calibration_utility_cases: Literal[40]
    release_safety_cases: Literal[40]
    release_utility_cases: Literal[80]


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
    decoys_per_utility_case: Literal[12]


class CohortCounts(StrictModel):
    safety: int = Field(ge=0)
    utility: int = Field(ge=0)


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
    generator_version: Literal["entity-linking-performance-fixture-v2"]
    publication_total_items: Literal[10000]
    publication_entities: Literal[6000]
    publication_relations: Literal[4000]
    linker_scenario: Literal["linker-mixed-10"]
    linker_mention_count: Literal[10]
    linker_mention_mix: Literal["2 exact, 3 high-similarity, 3 ambiguous, 2 not-found"]
    link_graph_scenario: Literal["link-graph-linked-10"]
    link_graph_mention_count: Literal[10]
    link_graph_mention_mix: Literal["2 exact, 4 boundary-omission, 4 ordered-abbreviation"]
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
    schema_version: Literal["entity-linking-feasibility-manifest-v2"]
    dataset_id: Literal["feasibility-v3"]
    g2_approval_commit: GitCommit
    g2_specification_tree_sha256: Sha256
    gold_ref: ArtifactRef
    cases_ref: ArtifactRef
    conformance_ref: ArtifactRef
    canonicalization_version: Literal["canonical-graph-json-v1"]
    normalization_version: Literal["normalize_graph_name_v1"]
    algorithm_version: Literal["lexical-score-v2"]
    rounding_version: Literal["integer-half-up-v1"]
    category_predicate_version: Literal["entity-linking-category-predicates-v2"]
    ordered_case_ids: tuple[LogicalKey, ...]
    ordered_calibration_case_ids: tuple[LogicalKey, ...]
    ordered_release_case_ids: tuple[LogicalKey, ...]
    ordered_calibration_utility_case_ids: tuple[LogicalKey, ...] = Field(
        min_length=40, max_length=40
    )
    ordered_release_utility_case_ids: tuple[LogicalKey, ...] = Field(
        min_length=80, max_length=80
    )
    calibration_cohort_counts: CohortCounts
    release_cohort_counts: CohortCounts
    counts: CountsRecord
    minimum_counts: MinimumCountsRecord
    release_category_counts: CategoryCounts
    release_stratum_counts: StratumCounts
    entity_family_split_hash: Sha256
    mention_family_split_hash: Sha256
    relation_template_family_split_hash: Sha256
    phrase_family_split_hash: Sha256
    decoy_family_split_hash: Sha256
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
    utility_question_execution_coverage: RateMetric
    non_exact_correct_auto_link_count: int = Field(ge=0)
    selection_eligible: bool

    @model_validator(mode="after")
    def validate_selection_eligibility(self) -> GridResult:
        expected = bool(
            self.wrong_auto_link_count == 0
            and self.ambiguous_unlinkable_false_auto_link_count == 0
            and self.privacy_leak_count == 0
            and self.publication_membership_failures == 0
            and self.auto_link_precision.value_micros == 1_000_000
            and self.exact_regression_accuracy.value_micros == 1_000_000
            and self.scope_safety.value_micros == 1_000_000
            and self.property_privacy.value_micros == 1_000_000
            and self.utility_question_execution_coverage.value_micros is not None
            and self.utility_question_execution_coverage.value_micros >= 800_000
        )
        if self.selection_eligible != expected:
            raise ValueError("grid selection eligibility invariant failed")
        return self


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

    @model_validator(mode="after")
    def validate_percentiles(self) -> LatencySummary:
        ordered = sorted(self.samples_us)
        if (self.p50_us, self.p95_us, self.max_us) != (
            ordered[14],
            ordered[28],
            ordered[-1],
        ):
            raise ValueError("latency percentile invariant failed")
        return self


class Performance(StrictModel):
    linker_scenario: Literal["linker-mixed-10"]
    link_graph_scenario: Literal["link-graph-linked-10"]
    control_scenario: Literal["link-graph-linked-10"]
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
    linker_graph_execution_count: Literal[0]
    link_graph_execution_count: Literal[30]

    @model_validator(mode="after")
    def validate_independent_scenarios(self) -> Performance:
        if self.linker.samples_us == self.link_graph.samples_us:
            raise ValueError("linker and link-graph samples must be independently measured")
        return self


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

    @model_validator(mode="after")
    def validate_quantile(self) -> QdrantCollectionConfig:
        if self.scalar_quantile != ReducedRational(numerator=99, denominator=100):
            raise ValueError("qdrant scalar quantile mismatch")
        return self


class QdrantIdentity(StrictModel):
    identity_version: Literal["entity-linking-qdrant-identity-v1"]
    endpoint_origin_version: Literal["service-origin-v1"]
    endpoint_origin_sha256: Sha256
    server_version: str = Field(pattern=r"^[0-9A-Za-z][0-9A-Za-z._+-]{0,63}$")
    collection_config: QdrantCollectionConfig
    collection_config_sha256: Sha256
    qdrant_fingerprint_sha256: Sha256

    @model_validator(mode="after")
    def validate_hashes(self) -> QdrantIdentity:
        config = self.collection_config.model_dump(mode="json")
        projection = self.model_dump(mode="json", exclude={"qdrant_fingerprint_sha256"})
        if self.collection_config_sha256 != canonical_sha256(
            config
        ) or self.qdrant_fingerprint_sha256 != canonical_sha256(projection):
            raise ValueError("qdrant identity hash mismatch")
        return self


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
    vector_encoding_version: Literal["ternary-deadzone-0.005-v1"]
    probe_vector_sha256: Sha256
    embedding_fingerprint_sha256: Sha256

    @model_validator(mode="after")
    def validate_fingerprint(self) -> EmbeddingIdentity:
        projection = self.model_dump(mode="json", exclude={"embedding_fingerprint_sha256"})
        if self.embedding_fingerprint_sha256 != canonical_sha256(projection):
            raise ValueError("embedding identity hash mismatch")
        return self


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
        projection = self.model_dump(mode="json", exclude={"environment_fingerprint_sha256"})
        if self.environment_fingerprint_sha256 != canonical_sha256(projection):
            raise ValueError("environment fingerprint mismatch")
        return self


class CaseResponseHash(StrictModel):
    case_id: LogicalKey
    input_count: int = Field(ge=1, le=10)
    candidate_logical_response_sha256: Sha256
    exact_only_logical_response_sha256: Sha256
    dense_evidence_set_sha256: Sha256
    hybrid_evidence_set_sha256: Sha256


class CalibrationArtifact(StrictModel):
    schema_version: Literal["entity-linking-eval-result-v2"]
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
        "max-question-execution-coverage",
        "tie-non-exact-coverage",
        "tie-higher-score",
        "tie-higher-margin",
        "no-valid-candidate",
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

    @model_validator(mode="after")
    def validate_artifact_invariants(self) -> CalibrationArtifact:
        selected = self.candidate_thresholds is not None
        if selected != (self.status == "passed"):
            raise ValueError("calibration status/threshold invariant failed")
        if not selected and self.selection_reason != "no-valid-candidate":
            raise ValueError("calibration no-go reason mismatch")
        if selected and self.selection_reason == "no-valid-candidate":
            raise ValueError("calibration selected reason mismatch")
        if self.finished_at <= self.started_at:
            raise ValueError("calibration timestamp order invalid")
        if (
            self.pg_cluster_fingerprint_sha256 != self.environment.pg_cluster_fingerprint_sha256
            or self.qdrant_fingerprint_sha256 != self.environment.qdrant.qdrant_fingerprint_sha256
            or self.embedding_fingerprint_sha256 != self.environment.embedding.embedding_fingerprint_sha256
            or self.environment_fingerprint_sha256 != self.environment.environment_fingerprint_sha256
        ):
            raise ValueError("calibration environment identity mismatch")
        if tuple(row.grid_index for row in self.grid_results) != tuple(range(25)):
            raise ValueError("calibration grid order invalid")
        threshold_pairs = tuple(
            (row.thresholds.min_score_micros, row.thresholds.min_margin_micros) for row in self.grid_results
        )
        if len(set(threshold_pairs)) != 25:
            raise ValueError("calibration grid threshold duplicate")
        responses = [row.model_dump(mode="json") for row in self.ordered_response_hashes]
        if self.canonical_response_set_sha256 != canonical_sha256(responses):
            raise ValueError("calibration response set hash mismatch")
        return self


class PolicyApprovalPayload(StrictModel):
    schema_version: Literal["entity-linking-policy-approval-v2"]
    calibration_ref: ArtifactRef
    approved_thresholds: Threshold
    approved_by: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._:/#-]{0,127}$",
    )
    approved_at: UtcTimestamp
    approval_reference: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._:/#-]{0,255}$",
    )


class FrozenPolicy(StrictModel):
    schema_version: Literal["entity-linking-policy-v2"]
    policy_version: Literal["entity-linking-policy-v2"]
    algorithm_version: Literal["lexical-score-v2"]
    normalization_version: Literal["normalize_graph_name_v1"]
    g2_approval_commit: GitCommit
    g2_specification_tree_sha256: Sha256
    calibration_ref: ArtifactRef
    approved_thresholds: Threshold
    approval_payload_sha256: Sha256
    dataset_manifest_ref: ArtifactRef
    dataset_content_sha256: Sha256
    evaluation_config_sha256: Sha256
    ontology_schema_set_hash: Sha256
    code_commit: GitCommit
    evaluation_tree_sha256: Sha256
    accepted_dependency_closure_sha256: Sha256
    reference_scorer_sha256: Sha256
    external_distribution_set_sha256: Sha256
    control_config_sha256: Sha256
    environment_fingerprint_sha256: Sha256
    pg_cluster_fingerprint_sha256: Sha256
    qdrant_fingerprint_sha256: Sha256
    embedding_fingerprint_sha256: Sha256
    approved_by: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._:/#-]{0,127}$",
    )
    approved_at: UtcTimestamp
    approval_reference: str = Field(
        min_length=1,
        max_length=256,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._:/#-]{0,255}$",
    )


class PostFreezeArtifact(StrictModel):
    schema_version: Literal["entity-linking-eval-result-v2"]
    phase: Literal["post_freeze_release"]
    status: Literal["passed", "no_go"]
    ordinal: Literal[1, 2, 3]
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
    grid_results: None
    candidate_thresholds: None
    selection_reason: None
    metrics: Metrics
    category_counts: CategoryCounts
    stratum_counts: StratumCounts
    performance: Performance
    ordered_response_hashes: tuple[CaseResponseHash, ...] = Field(min_length=1)
    canonical_response_set_sha256: Sha256
    database_created: Literal[True]
    database_cleanup_succeeded: Literal[True]
    qdrant_collection_created: Literal[True]
    qdrant_cleanup_succeeded: Literal[True]
    policy_ref: ArtifactRef

    @model_validator(mode="after")
    def validate_artifact_invariants(self) -> PostFreezeArtifact:
        if self.finished_at <= self.started_at:
            raise ValueError("post-freeze timestamp order invalid")
        if (
            self.pg_cluster_fingerprint_sha256 != self.environment.pg_cluster_fingerprint_sha256
            or self.qdrant_fingerprint_sha256 != self.environment.qdrant.qdrant_fingerprint_sha256
            or self.embedding_fingerprint_sha256 != self.environment.embedding.embedding_fingerprint_sha256
            or self.environment_fingerprint_sha256 != self.environment.environment_fingerprint_sha256
        ):
            raise ValueError("post-freeze environment identity mismatch")
        responses = [row.model_dump(mode="json") for row in self.ordered_response_hashes]
        if self.canonical_response_set_sha256 != canonical_sha256(responses):
            raise ValueError("post-freeze response set hash mismatch")
        return self


GateId = Literal[
    "wrong-auto-link-count",
    "auto-link-precision",
    "exact-regression-accuracy",
    "scope-safety",
    "property-privacy",
    "privacy-leak-count",
    "publication-membership-failures",
    "ambiguous-unlinkable-false-auto-links",
    "candidate-recall-at-5",
    "overall-link-recall",
    "non-exact-link-recall",
    "link-recall-gain-vs-exact",
    "abstention-accuracy",
    "evidence-recall-gain",
    "complete-support-gain",
    "evidence-precision-regression",
    "any-gold-gain",
    "bootstrap-ci-lower",
    "linker-p95",
    "link-graph-p95",
    "link-graph-ratio",
    "timeout-count",
    "sql-statement-budget",
    "per-mention-sql",
    "projection-rows",
    "deterministic-response-hash",
]


class GateDecision(StrictModel):
    gate_id: GateId
    value_type: Literal["integer", "rational", "boolean", "sha256"]
    comparison: Literal["eq", "ge", "le", "gt", "all_equal"]
    observed_integer: int | None
    threshold_integer: int | None
    observed_rational: ReducedRational | None
    threshold_rational: ReducedRational | None
    observed_boolean: bool | None
    threshold_boolean: bool | None
    observed_sha256: Sha256 | None
    threshold_sha256: Sha256 | None
    passed: bool

    @model_validator(mode="after")
    def validate_typed_pair_and_result(self) -> GateDecision:
        pairs = {
            "integer": (self.observed_integer, self.threshold_integer),
            "rational": (self.observed_rational, self.threshold_rational),
            "boolean": (self.observed_boolean, self.threshold_boolean),
            "sha256": (self.observed_sha256, self.threshold_sha256),
        }
        if any(
            (left is None) != (kind != self.value_type) or (right is None) != (kind != self.value_type)
            for kind, (left, right) in pairs.items()
        ):
            raise ValueError("gate typed pair invariant failed")
        observed, threshold = pairs[self.value_type]
        if self.value_type == "integer":
            if self.comparison not in {"eq", "ge", "le"}:
                raise ValueError("integer gate comparison invalid")
            expected = {
                "eq": observed == threshold,
                "ge": observed >= threshold,
                "le": observed <= threshold,
            }[self.comparison]
        elif self.value_type == "rational":
            if self.comparison not in {"eq", "ge", "le", "gt"}:
                raise ValueError("rational gate comparison invalid")
            assert isinstance(observed, ReducedRational)
            assert isinstance(threshold, ReducedRational)
            left = observed.numerator * threshold.denominator
            right = threshold.numerator * observed.denominator
            expected = {
                "eq": left == right,
                "ge": left >= right,
                "le": left <= right,
                "gt": left > right,
            }[self.comparison]
        elif self.value_type == "boolean":
            if self.comparison != "eq":
                raise ValueError("boolean gate comparison invalid")
            expected = observed == threshold
        else:
            if self.comparison not in {"eq", "all_equal"}:
                raise ValueError("sha256 gate comparison invalid")
            expected = observed == threshold
        if self.passed != expected:
            raise ValueError("gate decision result mismatch")
        return self


class OrdinalArtifactRef(StrictModel):
    ordinal: Literal[1, 2, 3]
    run_id: RunId
    artifact_ref: ArtifactRef


class OrdinalResponseHash(StrictModel):
    ordinal: Literal[1, 2, 3]
    run_id: RunId
    canonical_response_set_sha256: Sha256


class RunGateDecision(StrictModel):
    ordinal: Literal[1, 2, 3]
    run_id: RunId
    artifact_ref: ArtifactRef
    gate_decisions: tuple[GateDecision, ...] = Field(min_length=26, max_length=26)
    all_passed: bool

    @model_validator(mode="after")
    def validate_decisions(self) -> RunGateDecision:
        if tuple(row.gate_id for row in self.gate_decisions) != GATE_ID_ORDER:
            raise ValueError("run gate decision order invalid")
        if self.all_passed != all(row.passed for row in self.gate_decisions):
            raise ValueError("run all-passed invariant failed")
        return self


class IdentityTimeCleanupDecision(StrictModel):
    g2_approval_commit_valid: bool
    g2_specification_tree_match: bool
    artifact_reference_hashes_match: bool
    code_identities_match: bool
    dependency_closures_match: bool
    external_distribution_sets_match: bool
    dataset_identities_match: bool
    control_identities_match: bool
    environment_fingerprints_match: bool
    pg_cluster_fingerprints_match: bool
    qdrant_fingerprints_match: bool
    embedding_fingerprints_match: bool
    run_ids_unique: bool
    database_ids_unique: bool
    ordinals_exact: bool
    policy_after_calibration: bool
    runs_after_policy: bool
    runs_finish_after_start: bool
    all_database_cleanup_succeeded: bool
    all_qdrant_cleanup_succeeded: bool
    all_databases_live_absent_same_cluster: bool
    all_qdrant_collections_live_absent: bool
    canonical_response_sets_equal: bool
    privacy_scans_passed: bool
    protected_paths_zero_drift: bool
    openapi_has_no_v07: bool
    alembic_head_is_0023: bool
    mandatory_live_tests_non_skipped: bool


class ReleaseEvidence(StrictModel):
    schema_version: Literal["entity-linking-release-evidence-v2"]
    status: Literal["passed", "no_go"]
    g2_approval_commit: GitCommit
    g2_specification_tree_sha256: Sha256
    dataset_manifest_ref: ArtifactRef
    dataset_content_sha256: Sha256
    evaluation_config_sha256: Sha256
    ontology_schema_set_hash: Sha256
    code_commit: GitCommit
    evaluation_tree_sha256: Sha256
    accepted_dependency_closure_sha256: Sha256
    reference_scorer_sha256: Sha256
    external_distribution_set_sha256: Sha256
    control_config_sha256: Sha256
    environment_fingerprint_sha256: Sha256
    pg_cluster_fingerprint_sha256: Sha256
    qdrant_fingerprint_sha256: Sha256
    embedding_fingerprint_sha256: Sha256
    calibration_ref: ArtifactRef
    policy_ref: ArtifactRef
    post_freeze_refs: tuple[OrdinalArtifactRef, ...] = Field(min_length=3, max_length=3)
    canonical_response_set_sha256_by_ordinal: tuple[OrdinalResponseHash, ...] = Field(
        min_length=3, max_length=3
    )
    run_gate_decisions: tuple[RunGateDecision, ...] = Field(min_length=3, max_length=3)
    identity_time_cleanup_decisions: IdentityTimeCleanupDecision
    final_hard_and_decision: Literal["GO_ELIGIBLE", "NO_GO"]

    @model_validator(mode="after")
    def validate_release_decision(self) -> ReleaseEvidence:
        expected_ordinals = (1, 2, 3)
        arrays = (
            self.post_freeze_refs,
            self.canonical_response_set_sha256_by_ordinal,
            self.run_gate_decisions,
        )
        if any(tuple(row.ordinal for row in rows) != expected_ordinals for rows in arrays):
            raise ValueError("release ordinal order invalid")
        expected_runs = tuple(row.run_id for row in self.post_freeze_refs)
        if any(tuple(row.run_id for row in rows) != expected_runs for rows in arrays[1:]):
            raise ValueError("release run reference mismatch")
        decisions = self.identity_time_cleanup_decisions.model_dump(mode="json")
        eligible = all(row.all_passed for row in self.run_gate_decisions) and all(decisions.values())
        if (self.status == "passed") != eligible or (
            self.final_hard_and_decision == "GO_ELIGIBLE"
        ) != eligible:
            raise ValueError("release final decision invariant failed")
        return self


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
