from __future__ import annotations

import asyncio
import re
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest
from asyncpg import exceptions as asyncpg_exceptions
from sqlalchemy.exc import IntegrityError

from app.models.canonical_mapping import GraphClaimMapping
from app.models.claim_decision import GraphClaimDecision
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.schemas.claim_decision import MappingCandidateProposalV1, deterministic_decision_id
from app.schemas.canonical_mapping import CLAIM_DECISION_ID_NAMESPACE, CanonicalMappingV1, MappingScopeV1
from app.services.canonical_mapping_persistence import (
    CanonicalMappingAuthorityError,
    CanonicalMappingPersistenceError,
    CanonicalMappingRemapBlockedError,
    CanonicalMappingScopeError,
    PersistedMappingRegistrySnapshot,
    _is_identity_unique_violation,
    _rebuild_decision_from_row,
    _row_from_result,
    create_or_get_canonical_mapping,
    get_canonical_mapping,
    list_canonical_mappings,
)
from app.services.raw_claim_persistence import _new_core
from app.services.claim_decision_builder import build_claim_decision_projection
from tests.test_canonical_mapping import CREATED_AT, HASH, _build, _input


_CANONICAL_MAPPING_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "alembic"
    / "versions"
    / "0059_canonical_mapping_persistence.py"
)


def test_0059_jsonb_operator_precedence_and_downgrade_order_are_explicit():
    source = _CANONICAL_MAPPING_MIGRATION.read_text(encoding="utf-8")
    path = r"(?:[A-Za-z_][A-Za-z0-9_.]*)(?:(?:->>|->|#>>|#>)\s*'[^']+')+"
    forbidden = {
        "JSONB path before subtraction": rf"(?<!\()\b{path}\s+-\s+",
        "JSONB path before concatenation": rf"(?<!\()\b{path}\s*\|\|",
        "JSONB path before IS DISTINCT FROM": rf"(?<!\()\b{path}\s+IS\s+DISTINCT\s+FROM",
        "JSONB path before cast": rf"(?<!\()\b{path}\s*::",
        "JSONB path directly after IS DISTINCT FROM": (
            rf"IS\s+DISTINCT\s+FROM\s+(?!\()\b{path}"
        ),
        "JSONB path directly after CASE": (
            rf"\bCASE\s+(?:WHEN\s+)?(?!\()\b{path}"
        ),
    }
    for description, pattern in forbidden.items():
        assert re.search(pattern, source) is None, description

    assert "(item - 'attestation'::text) ||" in source
    assert "(binding_item->'attestation') - 'validated_at'::text" in source
    assert "(projection->'source_evidence_ref_ids') ||" in source
    assert "(projection->'target_evidence_ref_ids') ||" in source

    trigger_function_pairs = (
        (
            "DROP TRIGGER IF EXISTS graph_extraction_jobs_scope_guard",
            "DROP FUNCTION IF EXISTS graph_extraction_jobs_scope_guard()",
        ),
        (
            "DROP TRIGGER IF EXISTS graph_claim_mappings_insert_guard",
            "DROP FUNCTION IF EXISTS graph_claim_mappings_insert_guard()",
        ),
        (
            "DROP TRIGGER IF EXISTS graph_mapping_authority_insert_guard",
            "DROP FUNCTION IF EXISTS graph_mapping_authority_insert_guard()",
        ),
        (
            "DROP TRIGGER IF EXISTS graph_extraction_jobs_ontology_snapshot_immutable_guard",
            "DROP FUNCTION IF EXISTS graph_extraction_jobs_ontology_snapshot_immutable_guard()",
        ),
    )
    for trigger_sql, function_sql in trigger_function_pairs:
        assert source.index(trigger_sql) < source.index(function_sql)

    assert source.index(
        "DROP FUNCTION IF EXISTS graph_mapping_semantic_evidence_bound(jsonb,jsonb)"
    ) < source.index("DROP FUNCTION IF EXISTS graph_mapping_semantic_ref_ids(jsonb)")
    assert source.index("DROP FUNCTION IF EXISTS graph_mapping_json_sha256(jsonb)") < source.index(
        "DROP FUNCTION IF EXISTS graph_mapping_canonical_json(jsonb)"
    )


def test_0059_typed_sql_guards_are_exact_and_timestamp_strict():
    source = _CANONICAL_MAPPING_MIGRATION.read_text(encoding="utf-8")

    required_fragments = (
        "CREATE FUNCTION graph_mapping_json_exact_keys(value jsonb, expected_keys text[])",
        "CREATE FUNCTION graph_mapping_validate_timestamp_text(value jsonb)",
        "NOT graph_mapping_validate_timestamp_text(projection->'created_at')",
        "NOT graph_mapping_validate_timestamp_text(\n                           binding_item->'attestation'->'validated_at'",
        "canonical mapping occurrence evidence identities are duplicated",
        "canonical mapping endpoint evidence is not in the verified binding subset",
        "canonical mapping typed projection contains an unknown or missing key",
        "jsonb_typeof(projection->'source_provenance'->'source_precedence') <> 'number'",
        "jsonb_typeof(projection->'actor_provenance'->'actor_precedence') <> 'number'",
        "OLD.ontology_snapshot->>'ontology_version_id') = OLD.ontology_version_id::text",
        "NEW.ontology_version_id IS NOT DISTINCT FROM OLD.ontology_version_id",
        "DROP FUNCTION IF EXISTS graph_mapping_validate_timestamp_text(jsonb)",
        "DROP FUNCTION IF EXISTS graph_mapping_json_exact_keys(jsonb,text[])",
    )
    for fragment in required_fragments:
        assert fragment in source, fragment

    assert "timestamp_value::timestamptz" in source
    assert "RETURN isfinite(parsed);" in source
    assert "(Z|[+-]([01][0-9]|2[0-3]):[0-5][0-9])" in source

    scope_fragments = (
        "CREATE FUNCTION graph_extraction_jobs_scope_guard()",
        "NEW.library_id IS DISTINCT FROM OLD.library_id",
        "FROM ontology_versions",
        "ontology_library_id IS DISTINCT FROM NEW.library_id",
        "CREATE TRIGGER graph_extraction_jobs_scope_guard",
        "BEFORE INSERT OR UPDATE OF library_id, ontology_version_id, ontology_snapshot, ontology_snapshot_hash",
        "DROP TRIGGER IF EXISTS graph_extraction_jobs_scope_guard",
        "DROP FUNCTION IF EXISTS graph_extraction_jobs_scope_guard()",
    )
    for fragment in scope_fragments:
        assert fragment in source, fragment


def _restore_fixture_evidence_ref(value: str | None, references: list[dict[str, str]]) -> str | None:
    if value is None:
        return None
    logical_matches = [item["ref_id"] for item in references if item["ref_id"] == value]
    if logical_matches:
        return value
    stable_matches = [item["ref_id"] for item in references if item["stable_identity"] == value]
    return stable_matches[0] if len(stable_matches) == 1 else None


def test_0059_source_bound_evidence_sql_normalizes_every_claim_path():
    source = _CANONICAL_MAPPING_MIGRATION.read_text(encoding="utf-8")

    assert "WHEN EXISTS (" in source
    assert "SELECT COUNT(*)" in source
    assert ") = 1 THEN (" in source
    assert "ELSE NULL" in source
    for field in (
        "source_mention",
        "target_mention",
        "negation",
        "modality",
        "valid_time",
        "effective_time",
    ):
        assert (
            f"graph_mapping_restore_evidence_ref(\n"
            f"                        claim_row.{field}, occurrence_row.evidence_refs\n"
            f"                    )"
        ) in source or (
            f"graph_mapping_restore_evidence_ref(\n"
            f"                             claim_row.{field}, occurrence_row.evidence_refs\n"
            f"                         )"
        ) in source
    assert "graph_mapping_restore_qualifiers(" in source

    forbidden_direct_comparisons = (
        "endpoint_ref.value = claim_row.source_mention->>'evidence_ref'",
        "endpoint_ref.value = claim_row.target_mention->>'evidence_ref'",
        "selected_ref.value = claim_row.source_mention->>'evidence_ref'",
        "selected_ref.value = claim_row.target_mention->>'evidence_ref'",
    )
    for fragment in forbidden_direct_comparisons:
        assert fragment not in source


@pytest.mark.parametrize(
    ("case_name", "endpoint_transform", "predicate_transform", "occurrence_id"),
    (
        ("ordinary", "identity", "identity", "occurrence-1"),
        ("swap-inverse", "swap", "inverse", "occurrence-1"),
        ("distinct-endpoint-refs", "identity", "identity", "occurrence-2"),
        ("optional-occurrence", "identity", "identity", None),
    ),
    ids=("ordinary", "swap-inverse", "distinct-endpoint-refs", "optional-occurrence"),
)
def test_0059_evidence_ref_fixture_is_scope_bound_for_mapping_shapes(
    case_name: str,
    endpoint_transform: str,
    predicate_transform: str,
    occurrence_id: str | None,
):
    del case_name
    references = [
        {"ref_id": "source-ref", "stable_identity": "stable-source"},
        {"ref_id": "target-ref", "stable_identity": "stable-target"},
    ]
    source_ref = _restore_fixture_evidence_ref("stable-source", references)
    target_ref = _restore_fixture_evidence_ref("stable-target", references)

    assert (source_ref, target_ref) == ("source-ref", "target-ref")
    if endpoint_transform == "swap":
        assert (target_ref, source_ref) == ("target-ref", "source-ref")
    if predicate_transform == "inverse":
        assert endpoint_transform == "swap"
    assert occurrence_id is None or occurrence_id.startswith("occurrence-")
    assert _restore_fixture_evidence_ref("stable-from-another-scope", references) is None
    assert _restore_fixture_evidence_ref(
        "duplicate-stable",
        references
        + [
            {"ref_id": "other-ref", "stable_identity": "duplicate-stable"},
            {"ref_id": "second-other-ref", "stable_identity": "duplicate-stable"},
        ],
    ) is None


def _db_error(
    *,
    sqlstate: str,
    constraint_name: str | None,
    real_driver: bool = False,
) -> IntegrityError:
    if real_driver:
        if sqlstate != "23505":
            raise ValueError("the asyncpg unique violation fixture has a fixed SQLSTATE")
        original = asyncpg_exceptions.UniqueViolationError("duplicate key")
        original.constraint_name = constraint_name
    else:
        original = SimpleNamespace(
            sqlstate=sqlstate,
            pgcode=None,
            constraint_name=constraint_name,
            diag=None,
        )
    return IntegrityError("INSERT", {}, original)


def test_identity_unique_violation_requires_postgres_code_and_exact_constraint():
    assert _is_identity_unique_violation(
        _db_error(
            sqlstate="23505",
            constraint_name="uq_graph_claim_mappings_result_fingerprint",
            real_driver=True,
        )
    )
    assert _is_identity_unique_violation(
        _db_error(
            sqlstate="23505",
            constraint_name="graph_claim_mappings_pkey",
            real_driver=True,
        )
    )
    for error in (
        _db_error(
            sqlstate="23505",
            constraint_name="other_unique_constraint",
            real_driver=True,
        ),
        _db_error(sqlstate="23505", constraint_name="other_unique_constraint"),
        _db_error(sqlstate="23503", constraint_name="graph_claim_mappings_pkey"),
        _db_error(sqlstate="23514", constraint_name="graph_claim_mappings_pkey"),
        _db_error(sqlstate="23505", constraint_name=None),
    ):
        assert not _is_identity_unique_violation(error)

    raw_driver_error = RuntimeError("fake driver error")
    raw_driver_error.sqlstate = "23505"
    raw_driver_error.constraint_name = "graph_claim_mappings_pkey"
    wrapped = _db_error(sqlstate="23505", constraint_name=None)
    wrapped.__cause__ = raw_driver_error
    assert not _is_identity_unique_violation(wrapped)

    raw_driver_error.constraint_name = "other_unique_constraint"
    assert not _is_identity_unique_violation(wrapped)


class _Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalar_one_or_none(self):
        if len(self.rows) > 1:
            raise AssertionError("fixture query unexpectedly returned multiple rows")
        return self.rows[0] if self.rows else None

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _Nested:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is not None:
            self.db.pending.clear()
        return False


class _Db:
    def __init__(self, input_value):
        claim = _new_core(input_value.claim)
        self.rows = {
            Library: {input_value.library_id: SimpleNamespace(id=input_value.library_id)},
            Document: {
                input_value.document_id: SimpleNamespace(
                    id=input_value.document_id, library_id=input_value.library_id
                )
            },
            DocumentRevision: {
                input_value.document_revision_id: SimpleNamespace(
                    id=input_value.document_revision_id,
                    library_id=input_value.library_id,
                    document_id=input_value.document_id,
                    revision_no=input_value.revision_no,
                )
            },
            GraphExtractionJob: {
                input_value.job_id: SimpleNamespace(
                    id=input_value.job_id,
                    library_id=input_value.library_id,
                    document_id=input_value.document_id,
                    document_revision_id=input_value.document_revision_id,
                    ontology_version_id=input_value.ontology_version_id,
                    ontology_snapshot_hash=input_value.ontology_snapshot_hash,
                )
            },
            GraphExtractionUnit: {
                input_value.extraction_unit_id: SimpleNamespace(
                    id=input_value.extraction_unit_id,
                    job_id=input_value.job_id,
                    library_id=input_value.library_id,
                    document_revision_id=input_value.document_revision_id,
                )
            },
            GraphRawClaim: {input_value.claim_id: claim},
            GraphRawClaimOccurrence: {
                input_value.extraction_occurrence_id: SimpleNamespace(
                    extraction_occurrence_id=input_value.extraction_occurrence_id,
                    claim_id=input_value.claim_id,
                    job_id=input_value.job_id,
                    extraction_unit_id=input_value.extraction_unit_id,
                    extractor_version=input_value.claim.extractor_version,
                    prompt_version=input_value.claim.prompt_version,
                    model_provider=input_value.claim.model_provider,
                    model_name=input_value.claim.model_name,
                    model_config_hash=input_value.claim.model_config_hash,
                    prompt_content_hash=input_value.claim.prompt_content_hash,
                    parser_version=input_value.claim.parser_version,
                    normalization_rule_version=input_value.claim.normalization_rule_version,
                    ontology_snapshot_hash=input_value.claim.ontology_snapshot_hash,
                    evidence_refs=input_value.claim.model_dump(mode="json")["evidence_refs"],
                    extraction_occurrence_fingerprint=input_value.claim.extraction_occurrence_fingerprint,
                )
            },
            OntologyVersion: {
                input_value.ontology_version_id: SimpleNamespace(
                    id=input_value.ontology_version_id,
                    library_id=input_value.library_id,
                )
            },
            GraphClaimDecision: {},
            GraphClaimMapping: {},
        }
        for field in ("library_id", "document_id", "document_revision_id"):
            setattr(claim, field, UUID(str(getattr(claim, field))))
        self.pending = []

    async def get(self, model, key):
        return self.rows.get(model, {}).get(key)

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        params = statement.compile().params
        rows = list(self.rows.get(entity, {}).values())
        for key, value in params.items():
            if key.startswith("mapping_result_id"):
                rows = [row for row in rows if row.mapping_result_id == value]
            elif key.startswith("mapping_result_fingerprint"):
                rows = [row for row in rows if row.mapping_result_fingerprint == value]
            elif key.startswith("library_id"):
                rows = [row for row in rows if row.library_id == value]
            elif key.startswith("document_revision_id"):
                rows = [row for row in rows if row.document_revision_id == value]
            elif key.startswith("claim_id"):
                rows = [row for row in rows if row.claim_id == value]
            elif key.startswith("extraction_occurrence_id"):
                rows = [row for row in rows if row.extraction_occurrence_id == value]
            elif key.startswith("outcome"):
                rows = [row for row in rows if row.outcome == value]
        return _Result(rows)

    def add(self, row):
        self.pending.append(row)

    def begin_nested(self):
        return _Nested(self)

    async def flush(self):
        for row in self.pending:
            self.rows.setdefault(type(row), {})[row.mapping_result_id] = row
        self.pending.clear()


class _RegistryLoader:
    def __init__(self, input_value):
        self.input_value = input_value
        self.calls = 0

    async def load(self, _db, *, scope, ontology_version_id, registry_snapshot_hash):
        self.calls += 1
        expected_scope = MappingScopeV1(
            library_id=self.input_value.library_id,
            document_id=self.input_value.document_id,
            document_revision_id=self.input_value.document_revision_id,
            revision_no=self.input_value.revision_no,
            job_id=self.input_value.job_id,
            extraction_unit_id=self.input_value.extraction_unit_id,
            claim_id=self.input_value.claim_id,
            extraction_occurrence_id=self.input_value.extraction_occurrence_id,
        )
        assert scope == expected_scope
        assert ontology_version_id == self.input_value.ontology_version_id
        assert registry_snapshot_hash == self.input_value.authorization_registry_snapshot.registry_snapshot_hash
        return PersistedMappingRegistrySnapshot(
            snapshot=self.input_value.authorization_registry_snapshot,
            authority_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
            authority_fingerprint=HASH,
        )


def _run(coro):
    return asyncio.run(coro)


def test_retry_reuses_immutable_row_and_first_created_at_wins():
    input_value = _input()
    first_result = _build(input_value, created_at=CREATED_AT)
    later_result = _build(input_value, created_at=CREATED_AT + timedelta(days=1))
    db = _Db(input_value)
    loader = _RegistryLoader(input_value)

    first = _run(
        create_or_get_canonical_mapping(
            db, input_value, first_result, registry_snapshot_loader=loader
        )
    )
    second = _run(
        create_or_get_canonical_mapping(
            db, input_value, later_result, registry_snapshot_loader=loader
        )
    )

    assert first.mapping_created is True
    assert second.mapping_created is False
    assert first.mapping.created_at == CREATED_AT
    assert second.mapping.created_at == CREATED_AT
    assert first.mapping.mapping_result_fingerprint == second.mapping.mapping_result_fingerprint
    assert loader.calls == 2

    read = _run(
        get_canonical_mapping(
            db,
            library_id=input_value.library_id,
            document_revision_id=input_value.document_revision_id,
            mapping_result_id=first.mapping.mapping_result_id,
        )
    )
    assert read is not None
    assert read.mapping_result_id == first.mapping.mapping_result_id


def test_scoped_list_is_deterministic_and_does_not_cross_revision():
    input_value = _input()
    db = _Db(input_value)
    loader = _RegistryLoader(input_value)
    result = _build(input_value)
    _run(
        create_or_get_canonical_mapping(
            db, input_value, result, registry_snapshot_loader=loader
        )
    )
    assert len(
        _run(
            list_canonical_mappings(
                db,
                library_id=input_value.library_id,
                document_revision_id=input_value.document_revision_id,
            )
        )
    ) == 1
    assert not _run(
        list_canonical_mappings(
            db,
            library_id=input_value.library_id,
            document_revision_id=uuid.uuid4(),
        )
    )
    with pytest.raises(ValueError, match="limit"):
        _run(
            list_canonical_mappings(
                db,
                library_id=input_value.library_id,
                document_revision_id=input_value.document_revision_id,
                limit=True,
            )
        )


def test_missing_registry_authority_fails_closed_before_insert():
    input_value = _input()
    db = _Db(input_value)
    with pytest.raises(CanonicalMappingAuthorityError, match="authority"):
        _run(create_or_get_canonical_mapping(db, input_value, _build(input_value)))
    assert not db.rows[GraphClaimMapping]


def test_cross_scope_dependency_is_rejected_without_mutation():
    input_value = _input()
    db = _Db(input_value)
    db.rows[GraphExtractionUnit][input_value.extraction_unit_id].job_id = uuid.uuid4()
    with pytest.raises(CanonicalMappingScopeError, match="scope"):
        _run(
            create_or_get_canonical_mapping(
                db,
                input_value,
                _build(input_value),
                registry_snapshot_loader=_RegistryLoader(input_value),
            )
        )
    assert not db.rows[GraphClaimMapping]


def test_decision_projection_is_a_read_only_scoped_dependency():
    input_value = _input(decision=True)
    decision = input_value.decision
    assert decision is not None
    db = _Db(input_value)
    db.rows[GraphClaimDecision][decision.decision_id] = SimpleNamespace(
        **decision.model_dump(mode="python")
    )
    result = _build(input_value)
    stored = _run(
        create_or_get_canonical_mapping(
            db,
            input_value,
            result,
            registry_snapshot_loader=_RegistryLoader(input_value),
        )
    )
    assert stored.mapping.decision_id == decision.decision_id

    db.rows[GraphClaimDecision][decision.decision_id].claim_id = uuid.uuid4()
    with pytest.raises(CanonicalMappingScopeError, match="decision"):
        _run(
            create_or_get_canonical_mapping(
                db,
                input_value,
                result,
                registry_snapshot_loader=_RegistryLoader(input_value),
            )
        )


@pytest.mark.parametrize("forged_field", ["raw_predicate", "evidence_ref_ids"])
def test_recomputed_decision_identity_still_requires_claim_bound_semantics(forged_field):
    input_value = _input(decision=True)
    decision = input_value.decision
    assert decision is not None
    payload = decision.model_dump(mode="json")
    if forged_field == "raw_predicate":
        payload["proposal"]["raw_predicate"] = "forged-surface"
    else:
        payload["proposal"]["evidence_ref_ids"] = ["forged-evidence"]
    payload["decision_id"] = str(uuid.uuid4())
    payload["decision_fingerprint"] = None
    draft = type(decision).model_validate(payload)
    payload["decision_id"] = str(
        deterministic_decision_id(CLAIM_DECISION_ID_NAMESPACE, draft.decision_fingerprint)
    )
    forged = type(decision).model_validate(payload)
    row = SimpleNamespace(**forged.model_dump(mode="python"))
    with pytest.raises(CanonicalMappingScopeError, match="decision"):
        _rebuild_decision_from_row(row, input_value.claim)


@pytest.mark.parametrize("construction", ["model_copy", "model_construct"])
def test_mapping_rebuild_rejects_forged_decision_id(construction):
    input_value = _input(decision=True)
    decision = input_value.decision
    assert decision is not None
    if construction == "model_copy":
        forged = decision.model_copy(update={"decision_id": uuid.uuid4()})
    else:
        payload = decision.model_dump(mode="python")
        payload["decision_id"] = uuid.uuid4()
        payload["proposal"] = decision.proposal
        forged = type(decision).model_construct(**payload)
    row = SimpleNamespace(**forged.model_dump(mode="python"))

    with pytest.raises(CanonicalMappingScopeError, match="decision"):
        _rebuild_decision_from_row(row, input_value.claim)


def test_self_consistent_non_mapped_reason_cannot_cross_authoritative_read_boundaries():
    input_value = _input()
    mapped = _build(input_value)
    payload = mapped.model_dump(mode="json")
    payload.update(
        outcome="blocked",
        reason_code="unknown_predicate",
        semantic_status="blocked",
        mapping_confidence=None,
        canonical_relation_key=None,
        canonical_direction=None,
        canonical_source_endpoint=None,
        canonical_target_endpoint=None,
        endpoint_transform=None,
        predicate_transform=None,
    )
    for field in ("mapping_attempt_id", "mapping_attempt_fingerprint", "mapping_result_id", "mapping_result_fingerprint"):
        payload[field] = None
    forged = CanonicalMappingV1.model_validate(payload)
    db = _Db(input_value)
    authority = PersistedMappingRegistrySnapshot(
        snapshot=input_value.authorization_registry_snapshot,
        authority_id=uuid.UUID("eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"),
        authority_fingerprint=HASH,
    )
    db.rows[GraphClaimMapping][forged.mapping_result_id] = _row_from_result(
        forged,
        authority=authority,
    )

    with pytest.raises(CanonicalMappingScopeError, match="semantic|projection"):
        _run(
            get_canonical_mapping(
                db,
                library_id=input_value.library_id,
                document_revision_id=input_value.document_revision_id,
                mapping_result_id=forged.mapping_result_id,
            )
        )
    with pytest.raises(CanonicalMappingScopeError, match="semantic|projection"):
        _run(
            list_canonical_mappings(
                db,
                library_id=input_value.library_id,
                document_revision_id=input_value.document_revision_id,
            )
        )
    with pytest.raises(CanonicalMappingPersistenceError, match="authoritative"):
        _run(
            create_or_get_canonical_mapping(
                db,
                input_value,
                forged,
                registry_snapshot_loader=_RegistryLoader(input_value),
            )
        )


def test_positive_remap_cannot_be_persisted():
    input_value = _input()
    result = _build(input_value)
    object.__setattr__(result, "remap_provenance", SimpleNamespace(remap_generation=1))
    with pytest.raises(CanonicalMappingPersistenceError):
        _run(
            create_or_get_canonical_mapping(
                _Db(input_value),
                input_value,
                result,
                registry_snapshot_loader=_RegistryLoader(input_value),
            )
        )


@pytest.mark.parametrize("generation", [0, 1])
def test_ontology_refresh_remap_cannot_enter_m3_persistence(generation):
    input_value = _input()
    result = _build(input_value)
    object.__setattr__(
        result,
        "remap_provenance",
        SimpleNamespace(remap_generation=generation, reason_code="ontology_refresh"),
    )
    with pytest.raises(CanonicalMappingRemapBlockedError, match="ontology_refresh"):
        _run(
            create_or_get_canonical_mapping(
                _Db(input_value),
                input_value,
                result,
                registry_snapshot_loader=_RegistryLoader(input_value),
            )
        )


def test_optional_decision_occurrence_rebuild_preserves_null_identity():
    base_input = _input()
    claim = base_input.claim
    decision = build_claim_decision_projection(
        claim,
        decision_kind="mapping_candidate",
        reason_code="unknown_predicate",
        proposal=MappingCandidateProposalV1(
            raw_predicate=claim.raw_predicate,
            source_mention=claim.source_mention,
            target_mention=claim.target_mention,
            surface_direction=claim.surface_direction,
            evidence_ref_ids=[reference.ref_id for reference in claim.evidence_refs],
        ),
        decision_version=1,
        created_by_kind="system",
        producer_key="m3-null-occurrence",
        producer_version="m3-v1",
        created_at=CREATED_AT,
        id_namespace=CLAIM_DECISION_ID_NAMESPACE,
        extraction_occurrence_id=None,
    )
    input_value = _input(decision_projection=decision)
    decision = input_value.decision
    assert decision is not None
    assert decision.extraction_occurrence_id is None
    row = SimpleNamespace(**decision.model_dump(mode="python"))

    rebuilt = _rebuild_decision_from_row(row, input_value.claim)

    assert rebuilt.extraction_occurrence_id is None
    assert rebuilt.decision_id == decision.decision_id
    assert rebuilt.decision_fingerprint == decision.decision_fingerprint


def _remap_result(previous, *, generation: int, mapping_version: int):
    previous_remap = previous.remap_provenance
    root_id = (
        previous_remap.lineage_root_mapping_result_id
        if previous_remap is not None and previous_remap.lineage_root_mapping_result_id is not None
        else previous.mapping_result_id
    )
    root_fingerprint = (
        previous_remap.lineage_root_mapping_result_fingerprint
        if previous_remap is not None and previous_remap.lineage_root_mapping_result_fingerprint is not None
        else previous.mapping_result_fingerprint
    )
    payload = previous.model_dump(mode="json")
    payload.update(
        mapping_version=mapping_version,
        created_at=(previous.created_at + timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
        mapping_attempt_id=None,
        mapping_attempt_fingerprint=None,
        mapping_result_id=None,
        mapping_result_fingerprint=None,
        remap_provenance={
            "remap_generation": generation,
            "remap_version": generation,
            "supersedes_mapping_result_id": str(previous.mapping_result_id),
            "supersedes_mapping_result_fingerprint": previous.mapping_result_fingerprint,
            "supersedes_scope": {
                "library_id": str(previous.library_id),
                "document_id": str(previous.document_id),
                "document_revision_id": str(previous.document_revision_id),
                "revision_no": previous.revision_no,
                "job_id": str(previous.job_id),
                "extraction_unit_id": str(previous.extraction_unit_id),
                "claim_id": str(previous.claim_id),
                "extraction_occurrence_id": str(previous.extraction_occurrence_id),
            },
            "prior_remap_generation": generation - 1,
            "lineage_root_mapping_result_id": str(root_id),
            "lineage_root_mapping_result_fingerprint": root_fingerprint,
            "supersedes_lineage_root_mapping_result_id": str(root_id),
            "supersedes_lineage_root_mapping_result_fingerprint": root_fingerprint,
            "supersedes_source_precedence": previous.source_provenance.source_precedence,
            "supersedes_actor_precedence": previous.actor_provenance.actor_precedence,
            "reason_code": "mapper_refresh",
        },
    )
    return CanonicalMappingV1.model_validate(payload)


def test_repository_positive_remap_requires_and_reuses_typed_predecessor():
    input_value = _input()
    db = _Db(input_value)
    loader = _RegistryLoader(input_value)
    first = _run(
        create_or_get_canonical_mapping(
            db,
            input_value,
            _build(input_value),
            registry_snapshot_loader=loader,
        )
    )
    remap_one = _remap_result(first.mapping, generation=1, mapping_version=2)
    second = _run(
        create_or_get_canonical_mapping(
            db,
            input_value,
            remap_one,
            registry_snapshot_loader=loader,
        )
    )
    remap_two = _remap_result(second.mapping, generation=2, mapping_version=3)
    third = _run(
        create_or_get_canonical_mapping(
            db,
            input_value,
            remap_two,
            registry_snapshot_loader=loader,
        )
    )
    assert first.mapping.remap_provenance is None
    assert second.mapping.remap_provenance is not None
    assert second.mapping.remap_provenance.remap_generation == 1
    assert third.mapping.remap_provenance is not None
    assert third.mapping.remap_provenance.remap_generation == 2
    assert third.mapping.remap_provenance.supersedes_mapping_result_id == second.mapping.mapping_result_id


def test_stored_projection_tamper_is_not_silently_accepted():
    input_value = _input()
    db = _Db(input_value)
    result = _build(input_value)
    _run(
        create_or_get_canonical_mapping(
            db,
            input_value,
            result,
            registry_snapshot_loader=_RegistryLoader(input_value),
        )
    )
    row = db.rows[GraphClaimMapping][result.mapping_result_id]
    row.result_projection["surface_raw_predicate"] = "tampered"
    with pytest.raises(CanonicalMappingPersistenceError, match="projection"):
        _run(
            get_canonical_mapping(
                db,
                library_id=input_value.library_id,
                document_revision_id=input_value.document_revision_id,
                mapping_result_id=result.mapping_result_id,
            )
        )
