from __future__ import annotations

import json
import math
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.schemas.evidence_locator import EvidenceLocatorV1, ParserProvenanceV1, SourceLocatorV1, TextSpanV1, sha256_text
from app.schemas.raw_claim import (
    MAX_JSON_BYTES,
    MAX_JSON_DEPTH,
    MAX_JSON_NODES,
    RawClaimV1,
    canonical_raw_claim_json,
    content_scoped_claim_fingerprint,
    extraction_occurrence_fingerprint,
)
from app.services.graph_extraction_parser import parse_graph_extraction_output


LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
DOCUMENT_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
REVISION_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
UNIT_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")
EVIDENCE_ID = uuid.UUID("60000000-0000-0000-0000-000000000001")
BLOCK_ID = uuid.UUID("70000000-0000-0000-0000-000000000001")
CHUNK_ID = uuid.UUID("80000000-0000-0000-0000-000000000001")
HASH = "a" * 64


def _locator() -> EvidenceLocatorV1:
    text = "A supported statement."
    return EvidenceLocatorV1(
        document_id=DOCUMENT_ID,
        document_revision_id=REVISION_ID,
        revision_no=1,
        document_revision_file_id=uuid.UUID("90000000-0000-0000-0000-000000000001"),
        raw_file_sha256=HASH,
        normalized_content_hash=HASH,
        unit_id=EVIDENCE_ID,
        unit_kind="chunk",
        ordinal=0,
        parser=ParserProvenanceV1(name="fixture", version="v1", config_hash=HASH),
        source=SourceLocatorV1(
            kind="text",
            file_name="fixture.txt",
            text=TextSpanV1(start=0, end=len(text)),
        ),
        unit_text_sha256=sha256_text(text),
        quote_sha256=sha256_text(text),
    )


def _claim(**changes) -> RawClaimV1:
    values = {
        "claim_id": uuid.UUID("a0000000-0000-0000-0000-000000000001"),
        "library_id": LIBRARY_ID,
        "document_id": DOCUMENT_ID,
        "document_revision_id": REVISION_ID,
        "revision_no": 1,
        "job_id": JOB_ID,
        "extraction_unit_id": UNIT_ID,
        "source_mention": {
            "local_id": "source-1",
            "surface": "Source entity",
            "entity_type_hint": "source_type",
            "evidence_ref": "evidence-1",
        },
        "raw_predicate": "supports",
        "target_mention": {
            "local_id": "target-1",
            "surface": "Target entity",
            "entity_type_hint": "target_type",
            "evidence_ref": "evidence-1",
        },
        "surface_direction": "source_to_target",
        "negation": {"value": False, "evidence_ref": "evidence-1"},
        "modality": {"value": "asserted", "evidence_ref": "evidence-1"},
        "qualifiers": [{"key": "confidence_note", "value": {"source": "document"}, "evidence_ref": "evidence-1"}],
        "valid_time": {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-12-31T00:00:00Z",
            "evidence_ref": "evidence-1",
        },
        "effective_time": {"start": "2026-01-01T00:00:00Z", "end": None, "evidence_ref": "evidence-1"},
        "evidence_refs": [
            {
                "ref_id": "evidence-1",
                "evidence_id": EVIDENCE_ID,
                "library_id": LIBRARY_ID,
                "document_id": DOCUMENT_ID,
                "document_revision_id": REVISION_ID,
                "revision_no": 1,
                "job_id": JOB_ID,
                "extraction_unit_id": UNIT_ID,
                "unit_id": EVIDENCE_ID,
                "chunk_id": CHUNK_ID,
                "block_id": BLOCK_ID,
                "quote_sha256": sha256_text("A supported statement."),
                "unit_text_sha256": sha256_text("A supported statement."),
                "source_span": {"start": 0, "end": 22},
                "locator": _locator(),
            }
        ],
        "extractor_version": "extractor-v1",
        "prompt_version": "prompt-v1",
        "model_provider": "fixture-provider",
        "model_name": "fixture-model",
        "model_config_hash": HASH,
        "prompt_content_hash": HASH,
        "parser_version": "parser-v1",
        "normalization_rule_version": "normalization-v1",
        "ontology_snapshot_hash": HASH,
        "extraction_occurrence_id": uuid.UUID("a1000000-0000-0000-0000-000000000001"),
    }
    values.update(changes)
    return RawClaimV1(**values)


@pytest.mark.parametrize("fixture_domain", ["asset", "legal", "medical", "ordinary"])
@pytest.mark.parametrize("direction", ["source_to_target", "target_to_source", "undirected", "unknown"])
def test_raw_claim_supports_dynamic_domain_fixtures_and_all_directions(fixture_domain, direction):
    claim = _claim(surface_direction=direction, raw_predicate=f"{fixture_domain}_predicate")
    assert claim.surface_direction == direction
    assert claim.raw_predicate == f"{fixture_domain}_predicate"


def test_unknown_direction_is_preserved_without_canonical_conversion():
    claim = _claim(surface_direction="unknown")
    assert claim.surface_direction == "unknown"
    assert not hasattr(claim, "canonical_relation_type")


def test_negation_modality_qualifier_and_time_are_dynamic():
    claim = _claim(
        negation={"value": True, "evidence_ref": "evidence-1"},
        modality={"value": "alleged", "evidence_ref": "evidence-1"},
        qualifiers=[{"key": "free_domain_key", "value": [1, {"nested": True}], "evidence_ref": "evidence-1"}],
    )
    assert claim.negation.value is True
    assert claim.modality.value == "alleged"
    assert claim.valid_time is not None


@pytest.mark.parametrize(
    "field, value",
    [
        ("raw_predicate", ""),
        ("raw_predicate", "x" * 257),
        ("source_mention", {"local_id": "source-1", "surface": "x" * 513, "evidence_ref": "ctx-1"}),
        ("surface_direction", "reverse_unknown"),
        ("model_config_hash", "not-a-hash"),
    ],
)
def test_invalid_scalar_contract_is_rejected(field, value):
    with pytest.raises((ValidationError, ValueError)):
        _claim(**{field: value})


@pytest.mark.parametrize("scope_field", ["library_id", "document_id", "document_revision_id", "job_id", "extraction_unit_id"])
def test_cross_scope_evidence_is_rejected(scope_field):
    reference = _claim().evidence_refs[0].model_dump()
    reference[scope_field] = uuid.uuid4()
    with pytest.raises(ValidationError, match="scope|identity"):
        _claim(evidence_refs=[reference])


def test_evidence_refs_must_be_unique_and_all_referenced_fields_must_resolve():
    reference = _claim().evidence_refs[0].model_dump()
    with pytest.raises(ValidationError, match="unique"):
        _claim(evidence_refs=[reference, reference])


@pytest.mark.parametrize(
    "field, value",
    [
        ("source_mention", {"local_id": "source-1", "surface": "Source entity", "evidence_ref": "missing"}),
        ("target_mention", {"local_id": "target-1", "surface": "Target entity", "evidence_ref": "missing"}),
        ("negation", {"value": False, "evidence_ref": "missing"}),
        ("modality", {"value": "asserted", "evidence_ref": "missing"}),
        ("qualifiers", [{"key": "status", "value": "asserted", "evidence_ref": "missing"}]),
        ("valid_time", {"start": "2026-01-01T00:00:00Z", "end": None, "evidence_ref": "missing"}),
        ("effective_time", {"start": "2026-01-01T00:00:00Z", "end": None, "evidence_ref": "missing"}),
    ],
)
def test_every_evidence_bearing_field_rejects_dangling_ref(field, value):
    with pytest.raises(ValidationError, match="dangling"):
        _claim(**{field: value})


def test_multiple_evidence_refs_can_be_referenced_within_one_scope():
    first = _claim().evidence_refs[0].model_dump()
    second = {
        **first,
        "ref_id": "evidence-2",
        "evidence_id": uuid.UUID("60000000-0000-0000-0000-000000000002"),
        "unit_id": uuid.UUID("60000000-0000-0000-0000-000000000002"),
        "chunk_id": None,
        "block_id": None,
        "locator": None,
    }
    claim = _claim(
        source_mention={"local_id": "source-1", "surface": "Source entity", "evidence_ref": "evidence-1"},
        target_mention={"local_id": "target-1", "surface": "Target entity", "evidence_ref": "evidence-2"},
        negation={"value": False, "evidence_ref": "evidence-2"},
        modality={"value": "asserted", "evidence_ref": "evidence-1"},
        qualifiers=[{"key": "confidence_note", "value": {"source": "document"}, "evidence_ref": "evidence-2"}],
        valid_time={
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-12-31T00:00:00Z",
            "evidence_ref": "evidence-1",
        },
        effective_time={"start": "2026-01-01T00:00:00Z", "end": None, "evidence_ref": "evidence-2"},
        evidence_refs=[first, second],
    )
    assert {reference.ref_id for reference in claim.evidence_refs} == {"evidence-1", "evidence-2"}


def test_locator_round_trip_is_identity_checked_and_sensitive_fields_are_not_serialized():
    claim = _claim()
    payload = json.loads(canonical_raw_claim_json(claim))
    locator = payload["evidence_refs"][0]["locator"]
    assert locator["unit_id"] == str(EVIDENCE_ID)
    assert "raw_file_sha256" not in locator
    assert "normalized_content_hash" not in locator
    assert "text_quote" not in payload
    assert "storage_path" not in json.dumps(payload)

    bad_locator = _locator().model_copy(update={"document_revision_id": uuid.uuid4()})
    with pytest.raises(ValidationError, match="identity"):
        _claim(evidence_refs=[{**claim.evidence_refs[0].model_dump(), "locator": bad_locator}])


def test_evidence_reference_requires_a_locator_or_typed_source_span():
    evidence = _claim().evidence_refs[0].model_dump()
    evidence.update({"locator": None, "source_span": None})
    with pytest.raises(ValidationError, match="locator or source span"):
        _claim(evidence_refs=[evidence])


def test_locator_and_source_span_must_not_conflict():
    evidence = _claim().evidence_refs[0].model_dump()
    evidence["source_span"] = {"start": 1, "end": 22}
    with pytest.raises(ValidationError, match="conflicts"):
        _claim(evidence_refs=[evidence])


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, {1: "non-string-key"}])
def test_unsafe_qualifier_values_are_rejected(value):
    with pytest.raises((ValidationError, ValueError)):
        _claim(qualifiers=[{"key": "unsafe", "value": value}])


def test_sensitive_source_span_fields_are_rejected():
    with pytest.raises((ValidationError, ValueError)):
        _claim(evidence_refs=[{**_claim().evidence_refs[0].model_dump(), "source_span": {"text": "secret"}}])


@pytest.mark.parametrize(
    "sensitive_key",
    ["quote", "raw_file_sha256", "rawFileSha256", "storage-path", "objectKey", "normalizedContentHash", "nested"],
)
def test_sensitive_qualifier_keys_are_rejected_recursively(sensitive_key):
    value = {sensitive_key: "secret"} if sensitive_key != "nested" else {"outer": {"quote": "secret"}}
    with pytest.raises((ValidationError, ValueError)):
        _claim(qualifiers=[{"key": "safe_key", "value": value, "evidence_ref": "evidence-1"}])


@pytest.mark.parametrize("sensitive_key", ["storage_path", "storage-path", "objectKey", "rawFileSha256", "normalizedContentHash"])
def test_sensitive_qualifier_key_is_rejected(sensitive_key):
    with pytest.raises((ValidationError, ValueError)):
        _claim(qualifiers=[{"key": sensitive_key, "value": "secret", "evidence_ref": "evidence-1"}])


def test_locator_unit_kind_uses_the_typed_unit_kind_contract():
    evidence = _claim().evidence_refs[0].model_dump()
    evidence["locator"]["unit_kind"] = "not-a-unit-kind"
    with pytest.raises(ValidationError):
        _claim(evidence_refs=[evidence])


def test_dynamic_value_depth_count_and_bytes_are_bounded():
    deep: object = "leaf"
    for _ in range(MAX_JSON_DEPTH + 1):
        deep = {"nested": deep}
    with pytest.raises(ValidationError, match="depth"):
        _claim(qualifiers=[{"key": "deep", "value": deep}])

    with pytest.raises(ValidationError, match="node"):
        _claim(qualifiers=[{"key": "many", "value": list(range(MAX_JSON_NODES + 1))}])

    with pytest.raises((ValidationError, ValueError), match="size"):
        _claim(qualifiers=[{"key": "large", "value": "x" * MAX_JSON_BYTES}])


def test_time_interval_rejects_reverse_order_and_naive_datetime():
    with pytest.raises(ValidationError, match="after"):
        _claim(
            valid_time={
                "start": "2026-12-31T00:00:00Z",
                "end": "2026-01-01T00:00:00Z",
            }
        )
    with pytest.raises(ValidationError, match="timezone"):
        _claim(valid_time={"start": "2026-01-01T00:00:00", "end": None})


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01",
        "2026-01-01 00:00:00Z",
        "2026-01-01T00:00:00+0800",
        "2026-01-01T00:00:00,123Z",
        True,
        0,
        1.0,
    ],
)
def test_nested_time_interval_rejects_permissive_datetime_ingress(value):
    with pytest.raises(ValidationError):
        _claim(valid_time={"start": value, "end": None, "evidence_ref": "evidence-1"})


def test_occurrence_id_is_required_and_random_occurrence_id_is_not_hashed():
    values = _claim().model_dump()
    values.pop("extraction_occurrence_id")
    values.pop("extraction_occurrence_fingerprint")
    with pytest.raises(ValidationError):
        RawClaimV1(**values)

    first = _claim()
    second = _claim(extraction_occurrence_id=uuid.uuid4())
    assert extraction_occurrence_fingerprint(first) == extraction_occurrence_fingerprint(second)


def test_timezone_aware_datetimes_are_canonicalized_to_utc_z():
    local_time = _claim(effective_time={"start": "2026-01-01T08:00:00+08:00", "end": None, "evidence_ref": "evidence-1"})
    utc_time = _claim(effective_time={"start": "2026-01-01T00:00:00Z", "end": None, "evidence_ref": "evidence-1"})
    assert local_time.effective_time.start == "2026-01-01T00:00:00Z"
    assert local_time.effective_time == utc_time.effective_time
    assert content_scoped_claim_fingerprint(local_time) == content_scoped_claim_fingerprint(utc_time)

    local_interval = _claim(
        valid_time={
            "start": "2026-01-01T08:00:00+08:00",
            "end": "2026-01-02T08:00:00+08:00",
            "evidence_ref": "evidence-1",
        }
    )
    utc_interval = _claim(
        valid_time={
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-01-02T00:00:00Z",
            "evidence_ref": "evidence-1",
        }
    )
    native_interval = _claim(
        valid_time={
            "start": datetime(2026, 1, 1, tzinfo=UTC),
            "end": datetime(2026, 1, 2, tzinfo=UTC),
            "evidence_ref": "evidence-1",
        }
    )
    assert local_interval.valid_time == utc_interval.valid_time == native_interval.valid_time
    assert content_scoped_claim_fingerprint(local_interval) == content_scoped_claim_fingerprint(utc_interval)
    assert extraction_occurrence_fingerprint(local_interval) == extraction_occurrence_fingerprint(utc_interval)


def test_schema_version_is_serialized_and_rejects_unknown_versions():
    claim = _claim()
    assert claim.claim_schema_version == "raw_claim_v1"
    assert json.loads(canonical_raw_claim_json(claim))["claim_schema_version"] == "raw_claim_v1"
    with pytest.raises(ValidationError):
        _claim(claim_schema_version="raw_claim_v2")


def test_evidence_ref_renames_are_stable_but_evidence_assignment_changes_are_not():
    claim = _claim()
    renamed_reference = {**claim.evidence_refs[0].model_dump(), "ref_id": "renamed"}
    renamed = _claim(
        source_mention={"local_id": "source-1", "surface": "Source entity", "entity_type_hint": "source_type", "evidence_ref": "renamed"},
        target_mention={"local_id": "target-1", "surface": "Target entity", "entity_type_hint": "target_type", "evidence_ref": "renamed"},
        negation={"value": False, "evidence_ref": "renamed"},
        modality={"value": "asserted", "evidence_ref": "renamed"},
        qualifiers=[{"key": "confidence_note", "value": {"source": "document"}, "evidence_ref": "renamed"}],
        valid_time={
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-12-31T00:00:00Z",
            "evidence_ref": "renamed",
        },
        effective_time={"start": "2026-01-01T00:00:00Z", "end": None, "evidence_ref": "renamed"},
        evidence_refs=[renamed_reference],
    )
    assert content_scoped_claim_fingerprint(claim) == content_scoped_claim_fingerprint(renamed)

    second_reference = {
        **claim.evidence_refs[0].model_dump(),
        "ref_id": "evidence-2",
        "evidence_id": uuid.UUID("60000000-0000-0000-0000-000000000002"),
        "unit_id": uuid.UUID("60000000-0000-0000-0000-000000000002"),
        "chunk_id": None,
        "block_id": None,
        "locator": None,
    }
    assigned_a = _claim(
        target_mention={"local_id": "target-1", "surface": "Target entity", "entity_type_hint": "target_type", "evidence_ref": "evidence-2"},
        evidence_refs=[claim.evidence_refs[0].model_dump(), second_reference],
    )
    assigned_b = _claim(
        source_mention={"local_id": "source-1", "surface": "Source entity", "entity_type_hint": "source_type", "evidence_ref": "evidence-2"},
        target_mention={"local_id": "target-1", "surface": "Target entity", "entity_type_hint": "target_type", "evidence_ref": "evidence-1"},
        evidence_refs=[claim.evidence_refs[0].model_dump(), second_reference],
    )
    assert content_scoped_claim_fingerprint(assigned_a) != content_scoped_claim_fingerprint(assigned_b)


def test_evidence_array_order_and_qualifier_order_are_canonicalized():
    first = _claim().evidence_refs[0].model_dump()
    second = {
        **first,
        "ref_id": "evidence-2",
        "evidence_id": uuid.UUID("60000000-0000-0000-0000-000000000002"),
        "unit_id": uuid.UUID("60000000-0000-0000-0000-000000000002"),
        "chunk_id": None,
        "block_id": None,
        "locator": None,
    }
    first_claim = _claim(
        evidence_refs=[first, second],
        qualifiers=[
            {"key": "b", "value": 2, "evidence_ref": "evidence-1"},
            {"key": "a", "value": 1, "evidence_ref": "evidence-1"},
        ],
    )
    second_claim = _claim(
        evidence_refs=[second, first],
        qualifiers=[
            {"key": "a", "value": 1, "evidence_ref": "evidence-1"},
            {"key": "b", "value": 2, "evidence_ref": "evidence-1"},
        ],
    )
    assert content_scoped_claim_fingerprint(first_claim) == content_scoped_claim_fingerprint(second_claim)
    assert extraction_occurrence_fingerprint(first_claim) == extraction_occurrence_fingerprint(second_claim)


def test_fingerprints_are_stable_order_independent_and_separate_provenance():
    first = _claim(qualifiers=[{"key": "object", "value": {"b": 2, "a": 1}}])
    second = _claim(qualifiers=[{"key": "object", "value": {"a": 1, "b": 2}}])
    assert content_scoped_claim_fingerprint(first) == content_scoped_claim_fingerprint(second)
    assert extraction_occurrence_fingerprint(first) == extraction_occurrence_fingerprint(second)

    rerun_job = uuid.uuid4()
    rerun_unit = uuid.uuid4()
    rerun_reference = first.evidence_refs[0].model_dump()
    rerun_reference.update({"job_id": rerun_job, "extraction_unit_id": rerun_unit})
    rerun = _claim(
        job_id=rerun_job,
        extraction_unit_id=rerun_unit,
        model_name="other-model",
        qualifiers=[{"key": "object", "value": {"a": 1, "b": 2}}],
        evidence_refs=[rerun_reference],
    )
    assert content_scoped_claim_fingerprint(first) == content_scoped_claim_fingerprint(rerun)
    assert extraction_occurrence_fingerprint(first) != extraction_occurrence_fingerprint(rerun)
    assert first.content_scoped_claim_fingerprint == content_scoped_claim_fingerprint(first)
    assert first.extraction_occurrence_fingerprint == extraction_occurrence_fingerprint(first)


def test_claim_is_extra_forbid_and_has_no_mutable_canonical_status_fields():
    with pytest.raises(ValidationError):
        _claim(mapping_status="pending")
    payload = _claim().model_dump(mode="json")
    assert "mapping_status" not in payload
    assert "publication_status" not in payload
    with pytest.raises(ValidationError):
        _claim(unexpected_field=True)


def test_existing_graph_extraction_contract_parser_remains_unchanged():
    payload = parse_graph_extraction_output(
        '{"entities":[{"local_id":"e1","name":"Entity","entity_type_key":"type",'
        '"aliases":[],"properties":{},"external_mapping_hints":[],"confidence":0.9,'
        '"evidence":[{"context_ref":"ctx-1","quote":"Entity"}]}],"relations":[]}'
    )
    assert payload.entities[0].local_id == "e1"
