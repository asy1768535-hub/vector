from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.evidence_locator import SourceLocatorV1, TextSpanV1, sha256_text
from app.schemas.raw_claim import EvidenceLocatorClaimRefV1, RawClaimV1, TimeIntervalV1
from app.schemas.claim_shadow_replay_artifact import (
    ClaimShadowReplayArtifactV2,
    RedactedRawClaimV2,
    canonical_claim_shadow_replay_v2_json,
    redact_raw_claim_v2,
    replay_scope_v2_from_raw_claim,
)
from app.services.claim_shadow_replay import ClaimShadowReplayLoadError, load_claim_shadow_replay_artifact


LIBRARY_ID = uuid.UUID("b0000000-0000-0000-0000-000000000001")
DOCUMENT_ID = uuid.UUID("b0000000-0000-0000-0000-000000000002")
REVISION_ID = uuid.UUID("b0000000-0000-0000-0000-000000000003")
JOB_ID = uuid.UUID("b0000000-0000-0000-0000-000000000004")
UNIT_ID = uuid.UUID("b0000000-0000-0000-0000-000000000005")
EVIDENCE_IDS = (
    uuid.UUID("b0000000-0000-0000-0000-000000000006"),
    uuid.UUID("b0000000-0000-0000-0000-000000000007"),
)
HASH = "a" * 64


def _evidence(index: int, *, locator: bool) -> dict[str, object]:
    evidence_id = EVIDENCE_IDS[index]
    text = f"Evidence statement {index}"
    value: dict[str, object] = {
        "ref_id": f"evidence-{index}",
        "evidence_id": evidence_id,
        "library_id": LIBRARY_ID,
        "document_id": DOCUMENT_ID,
        "document_revision_id": REVISION_ID,
        "revision_no": 1,
        "job_id": JOB_ID,
        "extraction_unit_id": UNIT_ID,
        "unit_id": evidence_id,
        "quote_sha256": sha256_text(text),
        "unit_text_sha256": sha256_text(text),
        "source_span": {"start": 0, "end": len(text)},
    }
    if locator:
        value["locator"] = EvidenceLocatorClaimRefV1(
            locator_version="v1",
            document_id=DOCUMENT_ID,
            document_revision_id=REVISION_ID,
            revision_no=1,
            unit_id=evidence_id,
            unit_kind="chunk",
            ordinal=index,
            quote_sha256=sha256_text(text),
            unit_text_sha256=sha256_text(text),
            source=SourceLocatorV1(
                kind="text",
                file_name="fixture.txt",
                text=TextSpanV1(start=0, end=len(text)),
            ),
        )
    return value


def _claim(**changes: object) -> RawClaimV1:
    values: dict[str, object] = {
        "claim_id": uuid.UUID("b1000000-0000-0000-0000-000000000001"),
        "library_id": LIBRARY_ID,
        "document_id": DOCUMENT_ID,
        "document_revision_id": REVISION_ID,
        "revision_no": 1,
        "job_id": JOB_ID,
        "extraction_unit_id": UNIT_ID,
        "source_mention": {
            "local_id": "asset-source",
            "surface": "Medical device Alpha",
            "entity_type_hint": "device",
            "evidence_ref": "evidence-0",
        },
        "raw_predicate": "is alleged to support",
        "target_mention": {
            "local_id": "asset-target",
            "surface": "Legal treatment plan",
            "entity_type_hint": "treatment",
            "evidence_ref": "evidence-0",
        },
        "surface_direction": "unknown",
        "negation": {"value": True, "evidence_ref": "evidence-0"},
        "modality": {"value": "alleged", "evidence_ref": "evidence-1"},
        "qualifiers": [
            {"key": "population", "value": {"value": "patients"}, "evidence_ref": "evidence-1"},
            {"key": "date_note", "value": "2026-08-07", "evidence_ref": "evidence-1"},
        ],
        "valid_time": {
            "start": "2026-01-01T00:00:00Z",
            "end": "2026-12-31T00:00:00Z",
            "evidence_ref": "evidence-1",
        },
        "effective_time": {
            "start": "2026-01-01T08:00:00+08:00",
            "end": None,
            "evidence_ref": "evidence-1",
        },
        "evidence_refs": [_evidence(0, locator=True), _evidence(1, locator=True)],
        "extractor_version": "extractor-v1",
        "prompt_version": "prompt-v1",
        "model_provider": "fixture-provider",
        "model_name": "fixture-model",
        "model_config_hash": HASH,
        "prompt_content_hash": HASH,
        "parser_version": "parser-v1",
        "normalization_rule_version": "normalization-v1",
        "ontology_snapshot_hash": HASH,
        "extraction_occurrence_id": uuid.UUID("b1000000-0000-0000-0000-000000000002"),
    }
    values.update(changes)
    return RawClaimV1.model_validate(values)


def _artifact(claim: RawClaimV1) -> ClaimShadowReplayArtifactV2:
    record = redact_raw_claim_v2(claim)
    scope = replay_scope_v2_from_raw_claim(claim)
    return ClaimShadowReplayArtifactV2(
        artifact_id_sha256=hashlib.sha256(b"v2-artifact").hexdigest(),
        run_id_sha256=hashlib.sha256(b"v2-run").hexdigest(),
        provider_key_sha256=hashlib.sha256(b"provider").hexdigest(),
        model_key_sha256=hashlib.sha256(b"model").hexdigest(),
        config_sha256=hashlib.sha256(b"config").hexdigest(),
        prompt_sha256=hashlib.sha256(b"prompt").hexdigest(),
        artifact_producer_version="artifact-v2",
        schema_producer_version="raw-claim-v1",
        projection_producer_version="decision-v1",
        created_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        status="success",
        completed_stages=("request", "provider", "parse", "write"),
        scopes=(scope,),
        raw_claims=(record,),
        occurrences=(),
        decisions=(),
        aggregate_counts=(),
        metrics={
            "input_token_count": 10,
            "output_token_count": 5,
            "latency_ms": 25,
            "provider_call_count": 1,
            "raw_claim_count": 1,
            "occurrence_count": 0,
            "decision_count": 0,
        },
        failure=None,
    )


def test_v2_redaction_is_deterministic_and_contains_no_sensitive_text() -> None:
    claim = _claim()
    first = redact_raw_claim_v2(claim)
    second = redact_raw_claim_v2(claim)
    assert first == second
    encoded = json.dumps(first.model_dump(mode="json"), sort_keys=True)
    for marker in (
        "Medical device Alpha",
        "Legal treatment plan",
        "patients",
        "2026-08-07",
        "2026-01-01",
        "is alleged to support",
        str(LIBRARY_ID),
        str(REVISION_ID),
    ):
        assert marker not in encoded


def test_v2_nullable_semantic_evidence_refs_are_valid_and_deterministic() -> None:
    claim = _claim(
        negation={"value": True, "evidence_ref": None},
        modality={"value": "alleged", "evidence_ref": None},
    )

    first = redact_raw_claim_v2(claim)
    second = redact_raw_claim_v2(claim)

    assert first == second
    assert first.negation_evidence_ref_sha256 is None
    assert first.modality_evidence_ref_sha256 is None
    encoded = json.dumps(first.model_dump(mode="json"), sort_keys=True)
    assert "is alleged to support" not in encoded
    assert "Medical device Alpha" not in encoded


def test_v2_nullable_and_bound_semantic_evidence_refs_can_mix() -> None:
    record = redact_raw_claim_v2(
        _claim(
            negation={"value": True, "evidence_ref": None},
            modality={"value": "alleged", "evidence_ref": "evidence-1"},
        )
    )

    assert record.negation_evidence_ref_sha256 is None
    assert record.modality_evidence_ref_sha256 in {
        reference.evidence_ref_sha256 for reference in record.evidence_refs
    }


@pytest.mark.parametrize(
    "start",
    [
        "2026-01-01",
        "2026-01-01 00:00:00Z",
        "2026-01-01T00:00:00+0800",
        "2026-01-01T00:00:00,123Z",
    ],
)
def test_v2_redaction_revalidates_nested_time_before_hashing(start: str) -> None:
    forged_interval = TimeIntervalV1.model_construct(
        start=start,
        end=None,
        evidence_ref="evidence-1",
    )
    forged_claim = _claim().model_copy(update={"valid_time": forged_interval})

    with pytest.raises(ValueError, match="raw claim contract invalid"):
        redact_raw_claim_v2(forged_claim)


def test_v2_redaction_rejects_stale_fingerprints_after_nested_replacement() -> None:
    forged_interval = TimeIntervalV1.model_construct(
        start="2027-01-01T00:00:00Z",
        end="2027-12-31T00:00:00Z",
        evidence_ref="evidence-1",
    )
    forged_claim = _claim().model_copy(update={"valid_time": forged_interval})

    with pytest.raises(ValueError, match="raw claim contract invalid"):
        redact_raw_claim_v2(forged_claim)


def test_v2_rejects_unknown_non_null_nullable_evidence_hash() -> None:
    payload = redact_raw_claim_v2(
        _claim(
            negation={"value": True, "evidence_ref": None},
            modality={"value": "alleged", "evidence_ref": None},
        )
    ).model_dump(mode="json")
    payload["negation_evidence_ref_sha256"] = hashlib.sha256(b"dangling").hexdigest()

    with pytest.raises(ValidationError, match="dangling evidence"):
        RedactedRawClaimV2.model_validate(payload)


@pytest.mark.parametrize("value", [123, True, "not-a-sha256"])
def test_v2_rejects_invalid_nullable_evidence_hash(value: object) -> None:
    payload = redact_raw_claim_v2(_claim()).model_dump(mode="json")
    payload["modality_evidence_ref_sha256"] = value

    with pytest.raises(ValidationError):
        RedactedRawClaimV2.model_validate(payload)


def test_v2_distinguishes_endpoint_and_claim_semantics_without_local_id_truth() -> None:
    base = redact_raw_claim_v2(_claim())
    assert base.source_surface_sha256 != redact_raw_claim_v2(
        _claim(source_mention={**_claim().source_mention.model_dump(), "surface": "Different source"})
    ).source_surface_sha256
    assert base.target_surface_sha256 != redact_raw_claim_v2(
        _claim(target_mention={**_claim().target_mention.model_dump(), "surface": "Different target"})
    ).target_surface_sha256
    assert base.raw_predicate_sha256 != redact_raw_claim_v2(
        _claim(raw_predicate="is planned to support")
    ).raw_predicate_sha256
    assert base.source_type_hint_present is True
    absent = redact_raw_claim_v2(
        _claim(source_mention={**_claim().source_mention.model_dump(), "entity_type_hint": None})
    )
    assert absent.source_type_hint_present is False
    assert absent.source_type_hint_sha256 is None
    target_absent = redact_raw_claim_v2(
        _claim(target_mention={**_claim().target_mention.model_dump(), "entity_type_hint": None})
    )
    assert target_absent.target_type_hint_present is False
    assert target_absent.target_type_hint_sha256 is None
    assert base.source_evidence_ref_sha256 == base.target_evidence_ref_sha256
    assert base.negation is True
    assert redact_raw_claim_v2(_claim(negation={"value": False, "evidence_ref": "evidence-0"})).negation is False
    assert base.modality_value_sha256 != redact_raw_claim_v2(
        _claim(modality={"value": "planned", "evidence_ref": "evidence-1"})
    ).modality_value_sha256
    assert base.valid_time != redact_raw_claim_v2(
        _claim(
            valid_time={
                "start": "2027-01-01T00:00:00Z",
                "end": "2027-12-31T00:00:00Z",
                "evidence_ref": "evidence-1",
            }
        )
    ).valid_time
    assert base.effective_time != redact_raw_claim_v2(
        _claim(effective_time={"start": "2026-01-02T08:00:00+08:00", "end": None, "evidence_ref": "evidence-1"})
    ).effective_time
    assert base.qualifiers == redact_raw_claim_v2(_claim(qualifiers=list(reversed(_claim().qualifiers)))).qualifiers
    assert base.qualifiers != redact_raw_claim_v2(
        _claim(qualifiers=[{"key": "population", "value": {"value": "clinicians"}, "evidence_ref": "evidence-1"}])
    ).qualifiers


def test_v2_evidence_flags_and_hashes_distinguish_locator_span_and_evidence_identity() -> None:
    base = redact_raw_claim_v2(_claim())
    no_locator = redact_raw_claim_v2(
        _claim(evidence_refs=[_evidence(0, locator=False), _evidence(1, locator=False)])
    )
    assert base.evidence_refs[0].locator_present is True
    assert base.evidence_refs[0].locator_valid is True
    assert base.evidence_refs[0].source_span_valid is True
    assert no_locator.evidence_refs[0].locator_present is False
    assert no_locator.evidence_refs[0].locator_valid is False
    assert no_locator.evidence_refs[0].source_span_valid is True
    changed_evidence = _evidence(0, locator=False)
    changed_evidence["evidence_id"] = uuid.UUID("b0000000-0000-0000-0000-000000000099")
    changed = redact_raw_claim_v2(
        _claim(evidence_refs=[changed_evidence, _evidence(1, locator=False)])
    )
    changed_by_quote = {item.quote_sha256: item for item in changed.evidence_refs}
    base_by_quote = {item.quote_sha256: item for item in no_locator.evidence_refs}
    first_quote = sha256_text("Evidence statement 0")
    assert changed_by_quote[first_quote].evidence_ref_sha256 != base_by_quote[first_quote].evidence_ref_sha256
    assert changed_by_quote[first_quote].unit_text_sha256 == base_by_quote[first_quote].unit_text_sha256
    changed_quote = _evidence(0, locator=False)
    changed_quote["quote_sha256"] = sha256_text("Different quote")
    quote_changed = redact_raw_claim_v2(
        _claim(evidence_refs=[changed_quote, _evidence(1, locator=False)])
    )
    assert quote_changed.evidence_refs[0].quote_sha256 != base_by_quote[first_quote].quote_sha256


def test_v2_loader_is_discriminated_and_v1_mapping_compatibility_remains() -> None:
    artifact = _artifact(_claim())
    encoded = (canonical_claim_shadow_replay_v2_json(artifact) + "\n").encode()
    loaded = load_claim_shadow_replay_artifact(encoded)
    assert isinstance(loaded.artifact, ClaimShadowReplayArtifactV2)
    assert loaded.artifact.schema_version == "claim_shadow_replay_artifact_v2"
    assert loaded.runtime_metrics is not None

    v1_like = json.loads(encoded)
    v1_like["schema_version"] = "claim_shadow_replay_artifact_v1"
    with pytest.raises(ClaimShadowReplayLoadError, match="artifact_contract_invalid"):
        load_claim_shadow_replay_artifact(v1_like)

    missing_version = json.loads(encoded)
    missing_version.pop("schema_version")
    with pytest.raises(ClaimShadowReplayLoadError, match="artifact_contract_invalid"):
        load_claim_shadow_replay_artifact(missing_version)


def test_v2_rejects_unknown_fields_and_dangling_or_cross_scope_evidence() -> None:
    artifact = _artifact(_claim())
    payload = artifact.model_dump(mode="json")
    payload["raw_claims"][0]["unexpected"] = True
    with pytest.raises(ValidationError):
        ClaimShadowReplayArtifactV2.model_validate(payload)

    payload = artifact.model_dump(mode="json")
    payload["raw_claims"][0]["source_evidence_ref_sha256"] = hashlib.sha256(b"dangling").hexdigest()
    with pytest.raises(ValidationError, match="dangling evidence"):
        ClaimShadowReplayArtifactV2.model_validate(payload)

    payload = artifact.model_dump(mode="json")
    payload["raw_claims"][0]["evidence_refs"][0]["scope_id"] = hashlib.sha256(b"other-scope").hexdigest()
    with pytest.raises(ValidationError, match="crosses revision scope"):
        ClaimShadowReplayArtifactV2.model_validate(payload)
