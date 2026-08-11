from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from app.schemas.claim_shadow_raw_gold import RawGoldFixtureV1, RawGoldAnnotationV1
from app.schemas.claim_shadow_replay_artifact import (
    ClaimShadowReplayArtifactV1,
    ClaimShadowReplayArtifactV2,
    RedactedDecisionV1,
    RedactedEvidenceRefV2,
    RedactedOccurrenceV1,
    RedactedQualifierV2,
    RedactedRawClaimV2,
    RedactedTimeIntervalV2,
    ReplayScopeV1,
    scope_id_for,
    claim_shadow_v2_hash_text,
)
from app.services.claim_shadow_raw_scorer import RawGoldScoringError, score_claim_shadow_raw_gold


def _h(kind: str, value: str) -> str:
    return claim_shadow_v2_hash_text(kind, value)


def _scope(domain: str, revision_no: int = 1) -> ReplayScopeV1:
    library = _h("library_id", f"{domain}:library")
    document = _h("document_id", f"{domain}:document")
    revision = _h("revision_id", f"{domain}:revision:{revision_no}")
    return ReplayScopeV1(
        scope_id=scope_id_for(
            library_id_sha256=library,
            document_id_sha256=document,
            revision_id_sha256=revision,
            revision_no=revision_no,
        ),
        library_id_sha256=library,
        document_id_sha256=document,
        revision_id_sha256=revision,
        revision_no=revision_no,
    )


def _claim(domain: str, scope: ReplayScopeV1, *, suffix: str = "") -> RedactedRawClaimV2:
    evidence = (
        RedactedEvidenceRefV2(
            scope_id=scope.scope_id,
            evidence_ref_sha256=_h("evidence_identity", f"{domain}:evidence:0"),
            quote_sha256=hashlib.sha256(f"{domain}:quote".encode()).hexdigest(),
            unit_text_sha256=hashlib.sha256(f"{domain}:unit".encode()).hexdigest(),
            locator_present=True,
            locator_valid=True,
            source_span_valid=True,
        ),
        RedactedEvidenceRefV2(
            scope_id=scope.scope_id,
            evidence_ref_sha256=_h("evidence_identity", f"{domain}:evidence:1"),
            quote_sha256=hashlib.sha256(f"{domain}:quote-1".encode()).hexdigest(),
            unit_text_sha256=hashlib.sha256(f"{domain}:unit-1".encode()).hexdigest(),
            locator_present=True,
            locator_valid=True,
            source_span_valid=True,
        ),
    )
    return RedactedRawClaimV2(
        scope_id=scope.scope_id,
        claim_id_sha256=_h("claim_id", f"{domain}:claim:{suffix or 'one'}"),
        content_fingerprint=_h("content", f"{domain}:content:{suffix or 'one'}"),
        raw_predicate_sha256=_h("raw_predicate", f"{domain}:supports"),
        source_mention_id_sha256=_h("mention_local_id", f"{domain}:source"),
        source_surface_sha256=_h("mention_surface", f"{domain}:source surface"),
        source_type_hint_present=True,
        source_type_hint_sha256=_h("entity_type_hint", f"{domain}:source-type"),
        source_evidence_ref_sha256=evidence[0].evidence_ref_sha256,
        target_mention_id_sha256=_h("mention_local_id", f"{domain}:target"),
        target_surface_sha256=_h("mention_surface", f"{domain}:target surface"),
        target_type_hint_present=True,
        target_type_hint_sha256=_h("entity_type_hint", f"{domain}:target-type"),
        target_evidence_ref_sha256=evidence[0].evidence_ref_sha256,
        direction="source_to_target",
        negation=False,
        negation_evidence_ref_sha256=evidence[0].evidence_ref_sha256,
        modality_value_sha256=_h("modality_value", f"{domain}:asserted"),
        modality_evidence_ref_sha256=evidence[1].evidence_ref_sha256,
        qualifiers=(
            RedactedQualifierV2(
                key_sha256=_h("qualifier_key", f"{domain}:basis"),
                value_sha256=_h("qualifier_value", f"{domain}:documented"),
                evidence_ref_sha256=evidence[1].evidence_ref_sha256,
            ),
        ),
        valid_time=RedactedTimeIntervalV2(
            start_sha256=_h("time_value", "2026-01-01"),
            end_sha256=_h("time_value", "2026-12-31"),
            evidence_ref_sha256=evidence[1].evidence_ref_sha256,
        ),
        effective_time=RedactedTimeIntervalV2(
            start_sha256=_h("time_value", "2026-01-01T00:00:00Z"),
            evidence_ref_sha256=evidence[1].evidence_ref_sha256,
        ),
        evidence_refs=evidence,
    )


def _gold(claim: RedactedRawClaimV2, *, unknown_predicate: bool = False) -> RawGoldAnnotationV1:
    return RawGoldAnnotationV1(
        gold_claim_id_sha256=_h("gold_id", claim.claim_id_sha256),
        claim=claim,
        unknown_predicate=unknown_predicate,
        unknown_source_endpoint=not claim.source_type_hint_present,
        unknown_target_endpoint=not claim.target_type_hint_present,
    )


def _gold_fixture(rows: list[RawGoldAnnotationV1]) -> RawGoldFixtureV1:
    return RawGoldFixtureV1(
        fixture_id_sha256=_h("fixture_id", "synthetic-raw-gold"),
        annotations=tuple(rows),
    )


def _artifact(
    claims: list[RedactedRawClaimV2],
    scopes: list[ReplayScopeV1],
    *,
    decisions: tuple[RedactedDecisionV1, ...] = (),
    status: str = "success",
) -> ClaimShadowReplayArtifactV2:
    occurrences = tuple(
        RedactedOccurrenceV1(
            scope_id=claim.scope_id,
            occurrence_id_sha256=_h("occurrence_id", f"{claim.claim_id_sha256}:{index}"),
            claim_id_sha256=claim.claim_id_sha256,
            occurrence_fingerprint=_h("occurrence", f"{claim.claim_id_sha256}:{index}"),
            extractor_version="shadow-builder-v1",
            parser_version="raw-claim-v1",
            model_version="fixture-model",
            prompt_version="prompt-v1",
            config_version="config-v1",
        )
        for index, claim in enumerate(claims)
    )
    return ClaimShadowReplayArtifactV2(
        artifact_id_sha256=_h("artifact_id", "synthetic-artifact"),
        run_id_sha256=_h("run_id", "synthetic-run"),
        provider_key_sha256=_h("provider", "fixture"),
        model_key_sha256=_h("model", "fixture"),
        config_sha256=_h("config", "fixture"),
        prompt_sha256=_h("prompt", "fixture"),
        artifact_producer_version="artifact-v2",
        schema_producer_version="raw-claim-v1",
        projection_producer_version="decision-v1",
        created_at=datetime(2026, 8, 7, tzinfo=timezone.utc),
        status=status,
        completed_stages=("request", "provider", "parse"),
        scopes=tuple(scopes),
        raw_claims=tuple(claims),
        occurrences=occurrences,
        decisions=decisions,
        aggregate_counts=(),
        metrics=(
            {
                "input_token_count": 10,
                "output_token_count": 5,
                "latency_ms": 10,
                "provider_call_count": 1,
                "raw_claim_count": len(claims),
                "occurrence_count": len(occurrences),
                "decision_count": len(decisions),
            }
            if status == "success"
            else None
        ),
        failure=(
            None
            if status == "success"
            else {
                "stage": "provider",
                "error_code": "provider_timeout",
                "finish_reason": "timeout",
                "response_sha256": None,
                "observed_claim_count": 0,
                "truncated": False,
            }
        ),
    )


@pytest.mark.parametrize("domain", ["asset", "legal", "medical"])
def test_perfect_three_domain_fixture_scores_all_available_fields(domain: str) -> None:
    scope = _scope(domain)
    claim = _claim(domain, scope)
    score = score_claim_shadow_raw_gold(_artifact([claim], [scope]), _gold_fixture([_gold(claim)]))
    assert score.overall.raw_relation_precision.value == 1
    assert score.overall.raw_relation_recall.value == 1
    assert score.overall.source_endpoint_accuracy.value == 1
    assert score.overall.target_endpoint_accuracy.value == 1
    assert score.overall.evidence_accuracy.value == 1
    assert score.overall.locator_accuracy.value == 1
    assert score.overall.negation_accuracy.value == 1
    assert score.overall.modality_accuracy.value == 1
    assert score.overall.qualifier_accuracy.value == 1
    assert score.overall.valid_time_accuracy.value == 1
    assert score.overall.effective_time_accuracy.value == 1
    assert score.overall.canonicalization_coverage.value is None
    assert score.overall.canonicalization_coverage.unavailable_reason == "no_canonical_mapping_observation"


def test_scorer_revalidates_v2_instance_and_mapping_identically() -> None:
    scope = _scope("asset")
    claim = _claim("asset", scope)
    artifact = _artifact([claim], [scope])
    gold = _gold_fixture([_gold(claim)])

    object_score = score_claim_shadow_raw_gold(artifact, gold)
    mapping_score = score_claim_shadow_raw_gold(artifact.model_dump(mode="json"), gold)

    assert object_score == mapping_score
    assert object_score.model_dump(mode="json") == mapping_score.model_dump(mode="json")


def test_missing_extra_and_zero_denominator_metrics_are_explicit() -> None:
    scope = _scope("asset")
    claim = _claim("asset", scope)
    missing = score_claim_shadow_raw_gold(_artifact([], [scope]), _gold_fixture([_gold(claim)]))
    assert missing.overall.raw_relation_precision.value is None
    assert missing.overall.raw_relation_precision.unavailable_reason == "no_predicted_claims"
    assert missing.overall.raw_relation_recall.value == 0
    extra = score_claim_shadow_raw_gold(_artifact([claim], [scope]), _gold_fixture([]))
    assert extra.overall.raw_relation_precision.value == 0
    assert extra.overall.raw_relation_recall.value is None
    assert extra.overall.raw_relation_recall.unavailable_reason == "no_gold_claims"
    assert extra.overall.source_endpoint_accuracy.value is None
    assert extra.overall.source_endpoint_accuracy.unavailable_reason == "no_anchor_matches"


@pytest.mark.parametrize(
    ("field", "value", "score_field"),
    [
        ("raw_predicate_sha256", _h("raw_predicate", "wrong"), "raw_relation_precision"),
        ("source_surface_sha256", _h("mention_surface", "wrong"), "source_endpoint_accuracy"),
        ("target_surface_sha256", _h("mention_surface", "wrong"), "target_endpoint_accuracy"),
        ("direction", "target_to_source", "surface_direction_accuracy"),
        ("negation", True, "negation_accuracy"),
        ("modality_value_sha256", _h("modality_value", "planned"), "modality_accuracy"),
        (
            "qualifiers",
            (
                RedactedQualifierV2(
                    key_sha256=_h("qualifier_key", "wrong"),
                    value_sha256=_h("qualifier_value", "wrong"),
                    evidence_ref_sha256=None,
                ),
            ),
            "qualifier_accuracy",
        ),
        (
            "valid_time",
            RedactedTimeIntervalV2(
                start_sha256=_h("time_value", "2027-01-01"),
                evidence_ref_sha256=_h("evidence_identity", "asset:evidence:1"),
            ),
            "valid_time_accuracy",
        ),
        (
            "effective_time",
            RedactedTimeIntervalV2(
                start_sha256=_h("time_value", "2027-01-01T00:00:00Z"),
                evidence_ref_sha256=_h("evidence_identity", "asset:evidence:1"),
            ),
            "effective_time_accuracy",
        ),
    ],
)
def test_wrong_claim_semantics_are_not_fuzzy_matched(field: str, value: object, score_field: str) -> None:
    scope = _scope("asset")
    gold_claim = _claim("asset", scope)
    predicted = gold_claim.model_copy(update={field: value})
    score = score_claim_shadow_raw_gold(
        _artifact([predicted], [scope]),
        _gold_fixture([_gold(gold_claim)]),
    )
    assert getattr(score.overall, score_field).value == 0


def test_swapped_endpoint_direction_and_invalid_locator_are_separate() -> None:
    scope = _scope("legal")
    gold_claim = _claim("legal", scope)
    swapped = gold_claim.model_copy(
        update={
            "source_surface_sha256": gold_claim.target_surface_sha256,
            "target_surface_sha256": gold_claim.source_surface_sha256,
            "direction": "target_to_source",
        }
    )
    invalid_locator = gold_claim.model_copy(
        update={
            "evidence_refs": tuple(
                item.model_copy(update={"locator_valid": False}) for item in gold_claim.evidence_refs
            )
        }
    )
    swapped_score = score_claim_shadow_raw_gold(
        _artifact([swapped], [scope]), _gold_fixture([_gold(gold_claim)])
    )
    assert swapped_score.overall.source_endpoint_accuracy.value == 0
    assert swapped_score.overall.target_endpoint_accuracy.value == 0
    assert swapped_score.overall.surface_direction_accuracy.value == 0
    invalid_score = score_claim_shadow_raw_gold(
        _artifact([invalid_locator], [scope]), _gold_fixture([_gold(gold_claim)])
    )
    assert invalid_score.overall.raw_relation_precision.value == 1
    assert invalid_score.overall.locator_accuracy.value == 0
    wrong_evidence = gold_claim.model_copy(
        update={
            "evidence_refs": (
                gold_claim.evidence_refs[0].model_copy(
                    update={"quote_sha256": hashlib.sha256(b"wrong quote").hexdigest()}
                ),
                gold_claim.evidence_refs[1],
            )
        }
    )
    evidence_score = score_claim_shadow_raw_gold(
        _artifact([wrong_evidence], [scope]), _gold_fixture([_gold(gold_claim)])
    )
    assert evidence_score.overall.raw_relation_precision.value == 1
    assert evidence_score.overall.evidence_accuracy.value == 0


def test_swapped_source_target_evidence_bindings_fail_evidence_accuracy() -> None:
    scope = _scope("asset")
    evidence_bound_claim = _claim("asset", scope).model_copy(
        update={
            "target_evidence_ref_sha256": _claim("asset", scope).evidence_refs[1].evidence_ref_sha256,
        }
    )
    swapped = evidence_bound_claim.model_copy(
        update={
            "source_evidence_ref_sha256": evidence_bound_claim.target_evidence_ref_sha256,
            "target_evidence_ref_sha256": evidence_bound_claim.source_evidence_ref_sha256,
        }
    )
    score = score_claim_shadow_raw_gold(
        _artifact([swapped], [scope]),
        _gold_fixture([_gold(evidence_bound_claim)]),
    )
    assert score.overall.raw_relation_precision.value == 1
    assert score.overall.evidence_accuracy.value == 0


def test_unknown_retention_uses_decision_only_as_diagnostic() -> None:
    scope = _scope("medical")
    gold_claim = _claim("medical", scope)
    unknown_gold = _gold(gold_claim, unknown_predicate=True)
    decision = RedactedDecisionV1(
        scope_id=scope.scope_id,
        decision_id_sha256=_h("decision_id", "unknown"),
        claim_id_sha256=gold_claim.claim_id_sha256,
        occurrence_id_sha256=None,
        decision_fingerprint=_h("decision", "unknown"),
        decision_kind="mapping_candidate",
        status="pending",
        reason_code="unknown_predicate",
    )
    retained = score_claim_shadow_raw_gold(
        _artifact([gold_claim], [scope], decisions=(decision,)),
        _gold_fixture([unknown_gold]),
    )
    assert retained.overall.unknown_predicate_retention.value == 1
    dropped = score_claim_shadow_raw_gold(
        _artifact([gold_claim], [scope]),
        _gold_fixture([unknown_gold]),
    )
    assert dropped.overall.unknown_predicate_retention.value is None
    assert dropped.overall.unknown_predicate_retention.unavailable_reason == "no_unknown_predicate_diagnostic"

    unknown_endpoint_gold = _gold(
        gold_claim.model_copy(
            update={
                "source_type_hint_present": False,
                "source_type_hint_sha256": None,
            }
        )
    )
    unknown_endpoint_predicted = gold_claim
    endpoint_score = score_claim_shadow_raw_gold(
        _artifact([unknown_endpoint_predicted], [scope]),
        _gold_fixture([unknown_endpoint_gold]),
    )
    assert endpoint_score.overall.unknown_endpoint_retention.value == 0

    unknown_direction_gold = _gold(gold_claim.model_copy(update={"direction": "unknown"}))
    direction_score = score_claim_shadow_raw_gold(
        _artifact([gold_claim], [scope]),
        _gold_fixture([unknown_direction_gold]),
    )
    assert direction_score.overall.unknown_direction_retention.value == 0


def test_score_is_stable_when_fixture_and_prediction_order_changes() -> None:
    first_scope = _scope("asset")
    second_scope = _scope("asset", 2)
    first = _claim("asset", first_scope)
    second = _claim("asset", second_scope, suffix="second")
    first_score = score_claim_shadow_raw_gold(
        _artifact([first, second], [first_scope, second_scope]),
        _gold_fixture([_gold(first), _gold(second)]),
    )
    reversed_score = score_claim_shadow_raw_gold(
        _artifact([second, first], [second_scope, first_scope]),
        _gold_fixture([_gold(second), _gold(first)]),
    )
    assert first_score == reversed_score


def test_duplicate_occurrence_is_reported_once_for_scoring() -> None:
    scope = _scope("asset")
    claim = _claim("asset", scope)
    artifact = _artifact([claim], [scope])
    duplicate_occurrence = artifact.occurrences[0].model_copy(
        update={
            "occurrence_id_sha256": _h("occurrence_id", "occurrence-two"),
            "occurrence_fingerprint": _h("occurrence", "occurrence-two"),
        }
    )
    artifact = ClaimShadowReplayArtifactV2.model_validate(
        artifact.model_dump(mode="json")
        | {
            "occurrences": (*artifact.model_dump(mode="json")["occurrences"], duplicate_occurrence.model_dump(mode="json")),
            "metrics": {
                **artifact.metrics.model_dump(mode="json"),
                "raw_claim_count": 2,
                "occurrence_count": 2,
            },
        }
    )
    score = score_claim_shadow_raw_gold(
        artifact,
        _gold_fixture([_gold(claim)]),
    )
    assert score.overall.predicted_unique_claim_count == 1
    assert score.overall.occurrence_count == 2
    assert score.overall.duplicate_occurrence_count == 1
    assert score.overall.raw_relation_precision.value == 1


def test_cross_revision_scopes_do_not_match() -> None:
    first_scope = _scope("asset", 1)
    second_scope = _scope("asset", 2)
    first = _claim("asset", first_scope)
    second = _claim("asset", second_scope, suffix="second")
    score = score_claim_shadow_raw_gold(
        _artifact([first], [first_scope, second_scope]),
        _gold_fixture([_gold(first), _gold(second)]),
    )
    assert score.overall.raw_relation_recall.value == 0.5
    by_scope = {row.scope_id: row for row in score.scope_scores}
    assert by_scope[first_scope.scope_id].raw_relation_recall.value == 1
    assert by_scope[second_scope.scope_id].raw_relation_recall.value == 0


def test_ambiguous_gold_anchor_and_v1_or_failed_artifact_are_rejected() -> None:
    scope = _scope("legal")
    first = _claim("legal", scope)
    second = first.model_copy(update={"raw_predicate_sha256": _h("raw_predicate", "other")})
    with pytest.raises(ValidationError, match="ambiguous anchors"):
        _gold_fixture([_gold(first), _gold(second)])

    with pytest.raises(RawGoldScoringError, match="unsupported_schema_for_raw_gold_scoring"):
        score_claim_shadow_raw_gold(
            ClaimShadowReplayArtifactV1.model_validate(
                {
                    "artifact_id_sha256": _h("artifact_id", "v1"),
                    "run_id_sha256": _h("run_id", "v1"),
                    "provider_key_sha256": _h("provider", "v1"),
                    "model_key_sha256": _h("model", "v1"),
                    "config_sha256": _h("config", "v1"),
                    "prompt_sha256": _h("prompt", "v1"),
                    "artifact_producer_version": "artifact-v1",
                    "schema_producer_version": "schema-v1",
                    "projection_producer_version": "projection-v1",
                    "created_at": datetime(2026, 8, 7, tzinfo=timezone.utc),
                    "status": "failed",
                    "completed_stages": (),
                    "scopes": (),
                    "raw_claims": (),
                    "occurrences": (),
                    "decisions": (),
                    "aggregate_counts": (),
                    "metrics": None,
                    "failure": {
                        "stage": "provider",
                        "error_code": "provider_timeout",
                        "finish_reason": "timeout",
                        "response_sha256": None,
                        "observed_claim_count": 0,
                        "truncated": False,
                    },
                }
            ),
            _gold_fixture([]),
        )

    failed_v2 = _artifact([first], [scope], status="failed")
    with pytest.raises(RawGoldScoringError, match="failed_artifact_not_scored"):
        score_claim_shadow_raw_gold(failed_v2, _gold_fixture([_gold(first)]))
