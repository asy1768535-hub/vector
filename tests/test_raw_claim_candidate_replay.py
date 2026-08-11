from __future__ import annotations

import json
import uuid

import pytest

from app.schemas.evidence_locator import TextSpanV1, sha256_text
from app.schemas.raw_claim import RawClaimV1
from app.services.raw_claim_candidate_replay import (
    measure_replay_latency,
    replay_raw_relation_candidate_boundary,
)


LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
DOCUMENT_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
REVISION_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
UNIT_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
EVIDENCE_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
EVIDENCE_ID_2 = uuid.UUID("60000000-0000-0000-0000-000000000002")
HASH = "a" * 64


def _claim(
    *,
    revision_id: uuid.UUID = REVISION_ID,
    revision_no: int = 1,
    job_id: uuid.UUID = JOB_ID,
    unit_id: uuid.UUID = UNIT_ID,
    occurrence_id: uuid.UUID = uuid.UUID("a1000000-0000-0000-0000-000000000001"),
    model_name: str = "fixture-model",
    direction: str = "source_to_target",
    negated: bool = False,
    modality: str = "asserted",
    valid_time: dict[str, str | None] | None = None,
    raw_predicate: str = "supports",
    multi_evidence: bool = False,
) -> RawClaimV1:
    quote = "Source supports target."
    quote_2 = "The target is listed in the same record."
    source_ref = "e1"
    target_ref = "e2" if multi_evidence else "e1"
    evidence_refs = [
        {
            "ref_id": "e1",
            "evidence_id": EVIDENCE_ID,
            "library_id": LIBRARY_ID,
            "document_id": DOCUMENT_ID,
            "document_revision_id": revision_id,
            "revision_no": revision_no,
            "job_id": job_id,
            "extraction_unit_id": unit_id,
            "unit_id": EVIDENCE_ID,
            "quote_sha256": sha256_text(quote),
            "unit_text_sha256": sha256_text(quote),
            "source_span": TextSpanV1(start=0, end=len(quote)),
        }
    ]
    if multi_evidence:
        evidence_refs.append(
            {
                "ref_id": "e2",
                "evidence_id": EVIDENCE_ID_2,
                "library_id": LIBRARY_ID,
                "document_id": DOCUMENT_ID,
                "document_revision_id": revision_id,
                "revision_no": revision_no,
                "job_id": job_id,
                "extraction_unit_id": unit_id,
                "unit_id": EVIDENCE_ID_2,
                "quote_sha256": sha256_text(quote_2),
                "unit_text_sha256": sha256_text(quote_2),
                "source_span": TextSpanV1(start=0, end=len(quote_2)),
            }
        )
    return RawClaimV1(
        claim_id=uuid.UUID("a0000000-0000-0000-0000-000000000001"),
        library_id=LIBRARY_ID,
        document_id=DOCUMENT_ID,
        document_revision_id=revision_id,
        revision_no=revision_no,
        job_id=job_id,
        extraction_unit_id=unit_id,
        source_mention={
            "local_id": "source-1",
            "surface": "Source entity",
            "entity_type_hint": "source_type",
            "evidence_ref": source_ref,
        },
        raw_predicate=raw_predicate,
        target_mention={
            "local_id": "target-1",
            "surface": "Target entity",
            "entity_type_hint": "target_type",
            "evidence_ref": target_ref,
        },
        surface_direction=direction,
        negation={"value": negated, "evidence_ref": "e1"},
        modality={"value": modality, "evidence_ref": target_ref},
        qualifiers=[{"key": "basis", "value": "fixture", "evidence_ref": target_ref}],
        valid_time=valid_time
        or {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-12-31T00:00:00Z",
            "evidence_ref": "e1",
        },
        effective_time={"start": "2026-01-01T00:00:00Z", "end": None, "evidence_ref": "e1"},
        evidence_refs=evidence_refs,
        extractor_version="extractor-v1",
        prompt_version="prompt-v1",
        model_provider="fixture-provider",
        model_name=model_name,
        model_config_hash=HASH,
        prompt_content_hash=HASH,
        parser_version="parser-v1",
        normalization_rule_version="normalization-v1",
        ontology_snapshot_hash=HASH,
        extraction_occurrence_id=occurrence_id,
    )


def _raw_relation(
    claim: RawClaimV1,
    *,
    relation_type: str = "supports",
    target_local_id: str = "target-1",
    target_entity_local_id: str = "target-1",
    target_type: str = "target_type",
    include_claim_properties: bool = True,
) -> str:
    properties = (
        {
            "surface_direction": claim.surface_direction,
            "negation": claim.negation.model_dump(mode="json"),
            "modality": claim.modality.model_dump(mode="json"),
            "qualifiers": [item.model_dump(mode="json") for item in claim.qualifiers],
            "valid_time": claim.valid_time.model_dump(mode="json") if claim.valid_time else None,
            "effective_time": claim.effective_time.model_dump(mode="json") if claim.effective_time else None,
            "evidence_refs": [item.model_dump(mode="json") for item in claim.evidence_refs],
        }
        if include_claim_properties
        else {}
    )
    return json.dumps(
        {
            "entities": [
                {
                    "local_id": "source-1",
                    "name": claim.source_mention.surface,
                    "entity_type_key": "source_type",
                    "confidence": 0.9,
                    "evidence": [{"context_ref": "e1", "quote": claim.source_mention.surface}],
                },
                {
                    "local_id": target_entity_local_id,
                    "name": claim.target_mention.surface,
                    "entity_type_key": target_type,
                    "confidence": 0.9,
                    "evidence": [{"context_ref": "e1", "quote": claim.target_mention.surface}],
                },
            ],
            "relations": [
                {
                    "source_local_id": "source-1",
                    "relation_type_key": relation_type,
                    "target_local_id": target_local_id,
                    "properties": properties,
                    "confidence": 0.8,
                    "evidence": [{"context_ref": "e1", "quote": "Source supports target."}],
                }
            ],
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


ONTOLOGY_VERSION_ID = uuid.UUID("90000000-0000-0000-0000-000000000001")


def test_known_relation_replay_uses_real_parser_batch_parser_and_aggregation():
    claim = _claim()
    replay = replay_raw_relation_candidate_boundary(
        _raw_relation(claim),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
    )

    assert [stage.name for stage in replay.stages] == [
        "raw_json",
        "parser",
        "batch_parser",
        "occurrence_raw_payload",
        "candidate_aggregation",
        "candidate_validation",
        "candidate_purge",
    ]
    assert replay.stages[1].output_relation_count == 1
    assert replay.stages[2].output_relation_count == 1
    assert replay.occurrence_payload["relation_type_key"] == "supports"
    assert replay.aggregate_properties == replay.occurrence_payload["properties"]
    assert replay.candidate_key is not None
    assert replay.candidate_status == "aggregated"

    assert replay.field_matrix["raw_predicate"]["occurrence"] == "unavailable"
    assert replay.field_matrix["canonical_relation_type_key"]["occurrence"] == "preserved"
    assert replay.endpoint_validation == "matched"
    assert replay.field_matrix["source_surface"]["parser"] == "preserved"
    assert replay.field_matrix["source_surface"]["occurrence"] == "unavailable"
    assert replay.field_matrix["surface_direction"]["candidate"] == "preserved"
    assert replay.field_matrix["provenance"]["candidate"] == "unavailable"


@pytest.mark.parametrize(
    ("direction", "negated", "modality"),
    [("unknown", True, "alleged"), ("target_to_source", False, "planned")],
)
def test_unknown_direction_negation_modality_qualifier_and_time_survive_only_as_opaque_properties(
    direction, negated, modality
):
    claim = _claim(
        direction=direction,
        negated=negated,
        modality=modality,
        valid_time={
            "start": "2026-03-01T00:00:00Z",
            "end": "2026-04-01T00:00:00Z",
            "evidence_ref": "e1",
        },
    )
    replay = replay_raw_relation_candidate_boundary(
        _raw_relation(claim),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
    )
    properties = replay.aggregate_properties
    assert properties["surface_direction"] == direction
    assert properties["negation"]["value"] is negated
    assert properties["modality"]["value"] == modality
    assert properties["qualifiers"]
    assert properties["valid_time"]["start"] == "2026-03-01T00:00:00Z"
    for field in ("surface_direction", "negation", "modality", "qualifiers", "valid_time", "effective_time"):
        assert replay.field_matrix[field]["candidate"] == "preserved"


def test_unknown_relation_is_staged_then_rejected_as_schema_extension_candidate():
    claim = _claim()
    replay = replay_raw_relation_candidate_boundary(
        _raw_relation(claim, relation_type="unknown_predicate"),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
        relation_type_known=False,
    )
    assert replay.stages[2].output_relation_count == 1
    assert replay.occurrence_payload["relation_type_key"] == "unknown_predicate"
    assert replay.candidate_status == "rejected"
    assert replay.candidate_rejection_reason == "schema_extension_candidate"

    routed = replay_raw_relation_candidate_boundary(
        _raw_relation(claim, relation_type="unknown_predicate"),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
        relation_type_known=False,
        allowed_relation_type_keys={"supports"},
    )
    assert routed.candidate_status == "not_persisted"
    assert routed.candidate_rejection_reason == "batch_parser_rejected"


def test_unknown_endpoint_type_reaches_candidate_validation_but_undeclared_endpoint_is_dropped():
    claim = _claim()
    unknown_type = replay_raw_relation_candidate_boundary(
        _raw_relation(claim, target_type="unknown_type"),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
        endpoint_types_known=False,
    )
    assert unknown_type.occurrence_payload is not None
    assert unknown_type.candidate_status == "rejected"
    assert unknown_type.candidate_rejection_reason == "schema_extension_candidate"

    undeclared_endpoint = replay_raw_relation_candidate_boundary(
        _raw_relation(claim, target_local_id="missing-target"),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
    )
    assert undeclared_endpoint.stages[1].status == "rejected"
    assert undeclared_endpoint.stages[2].output_relation_count == 0
    assert undeclared_endpoint.candidate_status == "not_persisted"
    assert undeclared_endpoint.candidate_rejection_reason == "relation_not_emitted_by_batch_parser"


def test_endpoint_local_id_mismatch_is_rejected_before_claim_fingerprint_association():
    claim = _claim()
    replay = replay_raw_relation_candidate_boundary(
        _raw_relation(
            claim,
            target_local_id="other-target",
            target_entity_local_id="other-target",
        ),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
    )
    assert replay.endpoint_validation == "mismatch"
    assert replay.candidate_status == "rejected"
    assert replay.candidate_rejection_reason == "endpoint_local_id_mismatch"
    assert replay.occurrence_payload is None
    assert replay.candidate_key is None
    assert replay.stages[-2].error_code == "endpoint_local_id_mismatch"


def test_surface_raw_predicate_is_unavailable_when_parser_only_receives_canonical_key():
    claim = _claim(raw_predicate="is alleged to support")
    replay = replay_raw_relation_candidate_boundary(
        _raw_relation(claim, relation_type="supports"),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
    )
    assert claim.raw_predicate == "is alleged to support"
    assert replay.occurrence_payload["relation_type_key"] == "supports"
    assert replay.field_matrix["raw_predicate"]["parser"] == "unavailable"
    assert replay.field_matrix["raw_predicate"]["occurrence"] == "unavailable"
    assert replay.field_matrix["canonical_relation_type_key"]["candidate"] == "transformed"


def test_multi_evidence_refs_are_only_opaque_properties_and_are_removed_by_purge():
    claim = _claim(multi_evidence=True)
    replay = replay_raw_relation_candidate_boundary(
        _raw_relation(claim),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
    )
    assert claim.source_mention.evidence_ref == "e1"
    assert claim.target_mention.evidence_ref == "e2"
    assert claim.qualifiers[0].evidence_ref == "e2"
    assert {ref["ref_id"] for ref in replay.aggregate_properties["evidence_refs"]} == {"e1", "e2"}
    assert replay.occurrence_payload["evidence"] == [
        {"context_ref": "e1", "quote": "Source supports target."}
    ]
    assert replay.field_matrix["evidence_refs"]["candidate"] == "preserved"
    assert replay.purge.occurrence_raw_payload is None
    assert replay.purge.candidate_proposed_properties is None


@pytest.mark.parametrize(
    "domain_fixture",
    [
        ("asset", "is assigned to", "equipment_type", "case_type"),
        ("legal", "is alleged to support", "proceeding_type", "claim_type"),
        ("medical", "contains", "record_type", "finding_type"),
        ("ordinary", "is stored near", "item_type", "location_type"),
    ],
)
def test_replay_is_domain_neutral_and_does_not_allowlist_relation_words(domain_fixture):
    _domain, predicate, _source_type, target_type = domain_fixture
    claim = _claim(raw_predicate=predicate)
    raw = _raw_relation(claim, relation_type="observes", target_type=target_type)
    replay = replay_raw_relation_candidate_boundary(
        raw,
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
    )
    assert replay.candidate_status == "aggregated"
    assert replay.occurrence_payload["relation_type_key"] == "observes"
    assert replay.field_matrix["raw_predicate"]["occurrence"] == "unavailable"


def test_scope_and_rerun_provenance_do_not_cross_but_candidate_boundary_lacks_provenance():
    first = _claim()
    rerun = _claim(
        job_id=uuid.UUID("40000000-0000-0000-0000-000000000002"),
        unit_id=uuid.UUID("50000000-0000-0000-0000-000000000002"),
        occurrence_id=uuid.UUID("a1000000-0000-0000-0000-000000000002"),
        model_name="different-model",
    )
    first_replay = replay_raw_relation_candidate_boundary(
        _raw_relation(first, include_claim_properties=False),
        claim=first,
        ontology_version_id=ONTOLOGY_VERSION_ID,
    )
    rerun_replay = replay_raw_relation_candidate_boundary(
        _raw_relation(rerun, include_claim_properties=False),
        claim=rerun,
        ontology_version_id=ONTOLOGY_VERSION_ID,
    )
    assert first.content_scoped_claim_fingerprint == rerun.content_scoped_claim_fingerprint
    assert first.extraction_occurrence_fingerprint != rerun.extraction_occurrence_fingerprint
    assert first_replay.candidate_key == rerun_replay.candidate_key
    assert first_replay.field_matrix["provenance"]["occurrence"] == "unavailable"
    assert first_replay.field_matrix["scope"]["occurrence"] == "transformed"

    other_revision = _claim(
        revision_id=uuid.UUID("30000000-0000-0000-0000-000000000002"),
        revision_no=2,
    )
    other_replay = replay_raw_relation_candidate_boundary(
        _raw_relation(other_revision, include_claim_properties=False),
        claim=other_revision,
        ontology_version_id=ONTOLOGY_VERSION_ID,
    )
    assert other_revision.document_revision_id != first.document_revision_id
    assert other_revision.content_scoped_claim_fingerprint != first.content_scoped_claim_fingerprint
    assert other_replay.candidate_key == first_replay.candidate_key


def test_rejected_candidate_and_failed_batch_have_explicit_purge_retention_result():
    claim = _claim()
    rejected = replay_raw_relation_candidate_boundary(
        _raw_relation(claim, relation_type="unknown_predicate"),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
        relation_type_known=False,
    )
    assert rejected.candidate_status == "rejected"
    assert rejected.purge.occurrence_raw_payload is None
    assert rejected.purge.candidate_proposed_properties is None
    assert rejected.purge.candidate_evidence_quote is None

    failed = replay_raw_relation_candidate_boundary(
        _raw_relation(claim, target_local_id="missing-target"),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
    )
    assert failed.candidate_status == "not_persisted"
    assert failed.purge.occurrence_raw_payload is None


def test_bounded_replay_latency_has_stable_sample():
    claim = _claim()
    result = measure_replay_latency(
        _raw_relation(claim),
        claim=claim,
        ontology_version_id=ONTOLOGY_VERSION_ID,
        samples=20,
    )
    assert result["samples"] == 20
    assert 0 <= result["p50_ms"] <= result["p95_ms"]
