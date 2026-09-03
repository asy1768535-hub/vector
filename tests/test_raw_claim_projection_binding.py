from __future__ import annotations

import asyncio
import importlib.util
import inspect
import uuid
from types import SimpleNamespace
from pathlib import Path

from app.models.graph_candidates import GraphRelationCandidate
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.models.raw_claim_projection_binding import GraphRawClaimProjectionBinding
from app.schemas.evidence_locator import TextSpanV1
from app.schemas.raw_claim import EvidenceReferenceV1
from app.schemas.shadow_extraction import AnchoredShadowClaimV1, ShadowProjectionContextV1
from app.services.raw_claim_projection_binding import (
    RawClaimProjectionAnchor,
    binding_fingerprint_v1,
    create_or_get_raw_claim_projection_binding,
    load_shadow_projection_anchors,
    projection_anchor_for_shadow_claim,
)
from app.services import raw_claim_projection_binding as projection_binding
from app.services.raw_claim_shadow_builder import build_raw_claim_from_shadow
from app.schemas.shadow_raw_response import ShadowExtractionProvenanceV1, ShadowRawResponseV1


LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000701")
DOCUMENT_ID = uuid.UUID("20000000-0000-0000-0000-000000000701")
REVISION_ID = uuid.UUID("30000000-0000-0000-0000-000000000701")
JOB_ID = uuid.UUID("40000000-0000-0000-0000-000000000701")
UNIT_ID = uuid.UUID("50000000-0000-0000-0000-000000000701")
EVIDENCE_ID = uuid.UUID("60000000-0000-0000-0000-000000000701")
CANDIDATE_A_ID = uuid.UUID("70000000-0000-0000-0000-000000000701")
CANDIDATE_B_ID = uuid.UUID("70000000-0000-0000-0000-000000000702")
CLAIM_ID = uuid.UUID("80000000-0000-0000-0000-000000000701")
OCCURRENCE_ID = uuid.UUID("90000000-0000-0000-0000-000000000701")
HASH = "a" * 64


def _response(*, evidence: str = "e1") -> ShadowRawResponseV1:
    return ShadowRawResponseV1(
        source_mention={
            "local_id": "source-1",
            "surface": "Source",
            "entity_type_hint": "organization",
            "evidence_ref": evidence,
        },
        surface_raw_predicate="supports",
        target_mention={
            "local_id": "target-1",
            "surface": "Target",
            "entity_type_hint": "organization",
            "evidence_ref": evidence,
        },
        surface_direction="source_to_target",
        negation={"value": False, "evidence_ref": evidence},
        modality={"value": "asserted", "evidence_ref": evidence},
        qualifiers=[],
        valid_time=None,
        effective_time=None,
        evidence_ref_keys=[evidence],
    )


def _provenance() -> ShadowExtractionProvenanceV1:
    return ShadowExtractionProvenanceV1(
        library_id=LIBRARY_ID,
        document_id=DOCUMENT_ID,
        document_revision_id=REVISION_ID,
        revision_no=1,
        job_id=JOB_ID,
        extraction_unit_id=UNIT_ID,
        extractor_version="shadow-v1",
        prompt_version="shadow-prompt-v1",
        model_provider="fixture",
        model_name="fixture",
        model_config_hash=HASH,
        prompt_content_hash=HASH,
        parser_version="parser-v1",
        normalization_rule_version="normalization-v1",
        ontology_snapshot_hash=HASH,
    )


def _evidence() -> dict[str, EvidenceReferenceV1]:
    return {
        "e1": EvidenceReferenceV1(
            ref_id="e1",
            evidence_id=EVIDENCE_ID,
            library_id=LIBRARY_ID,
            document_id=DOCUMENT_ID,
            document_revision_id=REVISION_ID,
            revision_no=1,
            job_id=JOB_ID,
            extraction_unit_id=UNIT_ID,
            unit_id=EVIDENCE_ID,
            chunk_id=None,
            block_id=None,
            quote_sha256=HASH,
            unit_text_sha256=HASH,
            source_span=TextSpanV1(start=0, end=7),
        )
    }


def _anchors() -> tuple[RawClaimProjectionAnchor, ...]:
    return (
        RawClaimProjectionAnchor(
            projection_ref="p0",
            graph_relation_candidate_id=CANDIDATE_A_ID,
            allowed_evidence_ref_keys=frozenset({"e1"}),
        ),
    )


def _anchor(projection_ref: str, evidence_ref_keys: set[str]) -> RawClaimProjectionAnchor:
    return RawClaimProjectionAnchor(
        projection_ref=projection_ref,
        graph_relation_candidate_id=(
            CANDIDATE_A_ID if projection_ref == "p0" else CANDIDATE_B_ID
        ),
        allowed_evidence_ref_keys=frozenset(evidence_ref_keys),
    )


def _response_with_evidence_refs(*evidence_ref_keys: str) -> ShadowRawResponseV1:
    if not evidence_ref_keys:
        raise ValueError("fixture requires at least one evidence ref")
    response = _response(evidence=evidence_ref_keys[0])
    if len(evidence_ref_keys) == 1:
        return response
    payload = response.model_dump(mode="json")
    payload.update(
        qualifiers=[
            {"key": f"evidence-{index}", "value": "fixture", "evidence_ref": ref_key}
            for index, ref_key in enumerate(evidence_ref_keys[1:], start=1)
        ],
        evidence_ref_keys=list(evidence_ref_keys),
    )
    return ShadowRawResponseV1.model_validate(payload)


class _Result:
    def __init__(self, rows=()):
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
    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False


class _BindingDb:
    def __init__(self, candidate, evidence_rows):
        self.candidate = candidate
        self.candidates = [candidate]
        self.evidence_rows = list(evidence_rows)
        self.existing = None
        self.bindings = []
        self.job = SimpleNamespace(
            id=JOB_ID,
            library_id=LIBRARY_ID,
            ontology_version_id=uuid.UUID("a0000000-0000-0000-0000-000000000701"),
        )
        self.unit = SimpleNamespace(id=UNIT_ID, job_id=JOB_ID)

    async def get(self, model, key):
        if model is GraphRelationCandidate:
            return self.candidate if key == self.candidate.id else None
        if model is GraphExtractionJob:
            return self.job if key == self.job.id else None
        return None

    async def execute(self, statement):
        entity = statement.column_descriptions[0]["entity"]
        if entity is GraphRelationCandidate:
            return _Result(self.candidates)
        if entity is projection_binding.GraphRelationCandidateEvidence:
            return _Result(self.evidence_rows)
        if entity is GraphRawClaimProjectionBinding:
            return _Result(() if self.existing is None else (self.existing,))
        raise AssertionError(f"unexpected query entity: {entity}")

    def begin_nested(self):
        return _Nested()

    def add(self, row):
        self.bindings.append(row)

    async def flush(self):
        if self.bindings[-1].id is None:
            self.bindings[-1].id = uuid.uuid4()


def _candidate(**updates):
    value = {
        "id": CANDIDATE_A_ID,
        "job_id": JOB_ID,
        "library_id": LIBRARY_ID,
        "ontology_version_id": uuid.UUID("a0000000-0000-0000-0000-000000000701"),
        "purged_at": None,
        "status": "pending",
        "candidate_key": "candidate-a",
    }
    value.update(updates)
    return SimpleNamespace(**value)


def _candidate_evidence(candidate, *, evidence_id=EVIDENCE_ID):
    return SimpleNamespace(
        candidate_id=candidate.id,
        job_id=JOB_ID,
        extraction_unit_id=UNIT_ID,
        purged_at=None,
        validation_status="valid",
        resolved_evidence_id=evidence_id,
        resolved_document_id=DOCUMENT_ID,
        resolved_document_revision_id=REVISION_ID,
        resolved_chunk_id=None,
        resolved_block_id=None,
        resolved_source_span={"start": 0, "end": 7},
    )


def _raw_claim_and_occurrence():
    raw_claim = build_raw_claim_from_shadow(
        _response(),
        verified_evidence=_evidence(),
        provenance=_provenance(),
        id_namespace=uuid.UUID("00000000-0000-0000-0000-000000000701"),
    )
    return (
        SimpleNamespace(id=raw_claim.claim_id, library_id=raw_claim.library_id),
        SimpleNamespace(
            extraction_occurrence_id=raw_claim.extraction_occurrence_id,
            job_id=raw_claim.job_id,
            extraction_unit_id=raw_claim.extraction_unit_id,
        ),
        raw_claim,
    )


def _run(coro):
    return asyncio.run(coro)


def _load_migration():
    path = Path(__file__).parents[1] / "alembic" / "versions" / "0070_raw_claim_projection_binding.py"
    spec = importlib.util.spec_from_file_location("raw_claim_projection_binding_0070", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_binding_model_is_append_only_and_retains_candidate_lineage() -> None:
    table = GraphRawClaimProjectionBinding.__table__

    assert table.name == "graph_raw_claim_projection_bindings"
    assert {
        "raw_claim_id",
        "raw_claim_occurrence_id",
        "graph_relation_candidate_id",
        "binding_contract_version",
        "binding_method",
        "binding_fingerprint",
    }.issubset(table.c.keys())
    candidate_fk = next(iter(table.c.graph_relation_candidate_id.foreign_keys))
    assert candidate_fk.ondelete == "RESTRICT"
    assert any(
        set(constraint.columns.keys())
        == {"raw_claim_occurrence_id", "graph_relation_candidate_id"}
        for constraint in table.constraints
        if hasattr(constraint, "columns")
    )


def test_opaque_anchor_only_binds_an_explicit_ref_with_matching_evidence() -> None:
    valid = AnchoredShadowClaimV1(projection_ref="p0", claim=_response())
    assert projection_anchor_for_shadow_claim(valid, anchors=_anchors()) == _anchors()[0]

    assert projection_anchor_for_shadow_claim(
        AnchoredShadowClaimV1(projection_ref=None, claim=_response()), anchors=_anchors()
    ) is None
    assert projection_anchor_for_shadow_claim(
        AnchoredShadowClaimV1(projection_ref="unknown", claim=_response()), anchors=_anchors()
    ) is None
    mismatched = _response(evidence="e2")
    assert projection_anchor_for_shadow_claim(
        AnchoredShadowClaimV1(projection_ref="p0", claim=mismatched), anchors=_anchors()
    ) is None


def test_projection_anchor_requires_one_server_compatible_candidate() -> None:
    one_evidence_claim = _response_with_evidence_refs("e1")
    two_evidence_claim = _response_with_evidence_refs("e1", "e2")

    unique = (_anchor("p0", {"e1"}), _anchor("p1", {"e2"}))
    assert projection_anchor_for_shadow_claim(
        AnchoredShadowClaimV1(projection_ref="p0", claim=one_evidence_claim), anchors=unique
    ) == unique[0]

    exact_overlap = (_anchor("p0", {"e1"}), _anchor("p1", {"e1"}))
    assert projection_anchor_for_shadow_claim(
        AnchoredShadowClaimV1(projection_ref="p0", claim=one_evidence_claim),
        anchors=exact_overlap,
    ) is None

    overlapping = (_anchor("p0", {"e1", "e2"}), _anchor("p1", {"e1"}))
    assert projection_anchor_for_shadow_claim(
        AnchoredShadowClaimV1(projection_ref="p1", claim=one_evidence_claim),
        anchors=overlapping,
    ) is None
    assert projection_anchor_for_shadow_claim(
        AnchoredShadowClaimV1(projection_ref="p0", claim=two_evidence_claim),
        anchors=overlapping,
    ) == overlapping[0]

    assert projection_anchor_for_shadow_claim(
        AnchoredShadowClaimV1(projection_ref="p1", claim=one_evidence_claim), anchors=unique
    ) is None


def test_projection_ref_never_changes_raw_claim_identity() -> None:
    first = build_raw_claim_from_shadow(
        AnchoredShadowClaimV1(projection_ref="p0", claim=_response()).claim,
        verified_evidence=_evidence(),
        provenance=_provenance(),
        id_namespace=uuid.UUID("00000000-0000-0000-0000-000000000701"),
    )
    second = build_raw_claim_from_shadow(
        AnchoredShadowClaimV1(projection_ref="p1", claim=_response()).claim,
        verified_evidence=_evidence(),
        provenance=_provenance(),
        id_namespace=uuid.UUID("00000000-0000-0000-0000-000000000701"),
    )

    assert first.content_scoped_claim_fingerprint == second.content_scoped_claim_fingerprint
    assert first.extraction_occurrence_fingerprint == second.extraction_occurrence_fingerprint


def test_binding_fingerprint_dedupes_one_occurrence_candidate_pair_only() -> None:
    first = binding_fingerprint_v1(
        raw_claim_id=CLAIM_ID,
        raw_claim_occurrence_id=OCCURRENCE_ID,
        graph_relation_candidate_id=CANDIDATE_A_ID,
    )
    replay = binding_fingerprint_v1(
        raw_claim_id=CLAIM_ID,
        raw_claim_occurrence_id=OCCURRENCE_ID,
        graph_relation_candidate_id=CANDIDATE_A_ID,
    )
    other_candidate = binding_fingerprint_v1(
        raw_claim_id=CLAIM_ID,
        raw_claim_occurrence_id=OCCURRENCE_ID,
        graph_relation_candidate_id=CANDIDATE_B_ID,
    )

    assert first == replay
    assert first != other_candidate


def test_shadow_projection_context_is_bounded_and_does_not_expose_candidate_uuid() -> None:
    context = ShadowProjectionContextV1(
        projection_ref="p0", allowed_evidence_ref_keys=("e1",)
    )
    assert context.model_dump(mode="json") == {
        "projection_ref": "p0",
        "allowed_evidence_ref_keys": ["e1"],
    }
    assert str(CANDIDATE_A_ID) not in context.model_dump_json()


def test_projection_anchor_loading_and_binding_replay_are_evidence_scoped() -> None:
    candidate = _candidate()
    db = _BindingDb(candidate, [_candidate_evidence(candidate)])
    anchors = _run(
        load_shadow_projection_anchors(
            db,
            job=db.job,
            occurrence=db.unit,
            evidence_by_ref=_evidence(),
        )
    )

    assert anchors == _anchors()
    claim, occurrence, raw_claim = _raw_claim_and_occurrence()
    binding = _run(
        create_or_get_raw_claim_projection_binding(
            db,
            claim=claim,
            occurrence=occurrence,
            source_claim=raw_claim,
            anchor=anchors[0],
        )
    )
    assert binding is db.bindings[0]
    assert binding.graph_relation_candidate_id == CANDIDATE_A_ID

    db.existing = binding
    replay = _run(
        create_or_get_raw_claim_projection_binding(
            db,
            claim=claim,
            occurrence=occurrence,
            source_claim=raw_claim,
            anchor=anchors[0],
        )
    )
    assert replay is binding
    assert len(db.bindings) == 1


def test_same_occurrence_preserves_distinct_candidate_bindings() -> None:
    first_candidate = _candidate()
    db = _BindingDb(first_candidate, [_candidate_evidence(first_candidate)])
    claim, occurrence, raw_claim = _raw_claim_and_occurrence()
    first = _run(
        create_or_get_raw_claim_projection_binding(
            db,
            claim=claim,
            occurrence=occurrence,
            source_claim=raw_claim,
            anchor=_anchors()[0],
        )
    )
    second_candidate = _candidate(id=CANDIDATE_B_ID, candidate_key="candidate-b")
    db.candidate = second_candidate
    db.evidence_rows = [_candidate_evidence(second_candidate)]
    second = _run(
        create_or_get_raw_claim_projection_binding(
            db,
            claim=claim,
            occurrence=occurrence,
            source_claim=raw_claim,
            anchor=RawClaimProjectionAnchor(
                projection_ref="p1",
                graph_relation_candidate_id=CANDIDATE_B_ID,
                allowed_evidence_ref_keys=frozenset({"e1"}),
            ),
        )
    )

    assert first is not None and second is not None
    assert {row.graph_relation_candidate_id for row in db.bindings} == {
        CANDIDATE_A_ID,
        CANDIDATE_B_ID,
    }


def test_binding_service_rejects_ineligible_candidates_and_out_of_context_evidence() -> None:
    invalid_candidates = (
        _candidate(job_id=uuid.uuid4()),
        _candidate(library_id=uuid.uuid4()),
        _candidate(ontology_version_id=uuid.uuid4()),
        _candidate(status="rejected"),
        _candidate(status="superseded"),
        _candidate(purged_at=object()),
    )
    claim, occurrence, raw_claim = _raw_claim_and_occurrence()
    anchor = _anchors()[0]
    for candidate in invalid_candidates:
        db = _BindingDb(candidate, [_candidate_evidence(candidate)])
        assert _run(
            create_or_get_raw_claim_projection_binding(
                db,
                claim=claim,
                occurrence=occurrence,
                source_claim=raw_claim,
                anchor=anchor,
            )
        ) is None
        assert db.bindings == []

    candidate = _candidate()
    db = _BindingDb(candidate, [_candidate_evidence(candidate, evidence_id=uuid.uuid4())])
    assert _run(
        create_or_get_raw_claim_projection_binding(
            db,
            claim=claim,
            occurrence=occurrence,
            source_claim=raw_claim,
            anchor=anchor,
        )
    ) is None
    assert db.bindings == []


def test_raw_claim_layers_remain_unbound_without_a_valid_explicit_anchor() -> None:
    raw = _response().model_copy(update={"surface_raw_predicate": "different wording"})
    assert projection_anchor_for_shadow_claim(
        AnchoredShadowClaimV1(projection_ref=None, claim=raw), anchors=_anchors()
    ) is None
    assert projection_anchor_for_shadow_claim(
        AnchoredShadowClaimV1(projection_ref="not-issued", claim=raw), anchors=_anchors()
    ) is None
    assert not {
        "graph_relation_candidate_id",
        "relation_type_id",
        "source_entity_id",
        "target_entity_id",
        "ontology_version_id",
    } & set(GraphRawClaim.__table__.c.keys())
    assert "graph_relation_candidate_id" not in GraphRawClaimOccurrence.__table__.c
    assert "Fact" not in inspect.getsource(projection_binding)


def test_0070_migration_owns_restrict_retention_immutable_guard_and_no_backfill() -> None:
    migration = _load_migration()
    source = inspect.getsource(migration)

    assert migration.revision == "0070"
    assert migration.down_revision == "0069"
    assert "graph_raw_claim_projection_bindings" in source
    assert "ondelete=\"RESTRICT\"" in source
    assert "immutable_guard" in source
    assert "INSERT INTO" not in source

    operations = []
    migration.op = SimpleNamespace(
        create_table=lambda *args, **kwargs: operations.append(("create_table", args[0])),
        create_index=lambda *args, **kwargs: operations.append(("create_index", args[0])),
        execute=lambda statement: operations.append(("execute", str(statement))),
        drop_index=lambda *args, **kwargs: operations.append(("drop_index", args[0])),
        drop_table=lambda *args, **kwargs: operations.append(("drop_table", args[0])),
    )
    migration.upgrade()
    migration.downgrade()
    assert ("create_table", "graph_raw_claim_projection_bindings") in operations
    assert ("drop_table", "graph_raw_claim_projection_bindings") in operations
