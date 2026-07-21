from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.auth.backend import current_active_user
from app.config import Settings, settings, validate_entity_linking_startup
from app.db import get_db
from app.main import app
from app.models.library import Library
from app.models.user import User
from app.schemas.v07_entity_linking import (
    EntityLinkingCandidateRead,
    EntityLinkingCounts,
    EntityLinkingPublicationRead,
    EntityLinkingResolveRequest,
    EntityLinkingResolveResponse,
    EntityLinkingResult,
)
from app.services import entity_linking
from app.services import graph_retrieval
from app.services.entity_linking_scorer import (
    EntityLinkingCandidate,
    ScoredEntityLinkingCandidate,
    boundary_omission_micros,
    character_bigram_dice_micros,
    decide_scored_candidates,
    ordered_abbreviation_micros,
    resolve_mention,
    score_normalized_pair,
    stable_candidate_key,
    substring_containment_micros,
    token_jaccard_micros,
)
from app.services.graph_normalization import normalize_graph_name_v1
from app.services.entity_linking_observability import (
    EntityLinkingObservation,
    assert_sanitized_entity_linking_observation,
    entity_linking_metric_samples,
)
from eval.entity_linking import reference_scorer


LIBRARY_ID = uuid.UUID("71000000-0000-0000-0000-000000000001")
ONTOLOGY_ID = uuid.UUID("71000000-0000-0000-0000-000000000002")
PUBLICATION_ID = uuid.UUID("71000000-0000-0000-0000-000000000003")
TYPE_ID = uuid.UUID("71000000-0000-0000-0000-000000000004")
USER_ID = uuid.UUID("71000000-0000-0000-0000-000000000005")


def _candidate(
    index: int,
    name: str,
    *,
    type_key: str = "organization",
) -> EntityLinkingCandidate:
    return EntityLinkingCandidate(
        entity_id=uuid.UUID(f"72000000-0000-0000-0000-{index:012d}"),
        item_hash=f"{index:x}" * 64,
        entity_type_id=TYPE_ID,
        entity_type_key=type_key,
        entity_type_label=type_key.title(),
        canonical_name=name,
        normalized_name=name.strip().casefold(),
    )


def _request_payload(**overrides):
    payload = {
        "ontology_version_id": str(ONTOLOGY_ID),
        "expected_publication_id": str(PUBLICATION_ID),
        "mentions": [{"text": "Acme", "entity_type_key": "organization"}],
        "max_candidates_per_mention": 5,
    }
    payload.update(overrides)
    return payload


def _library() -> Library:
    return Library(id=LIBRARY_ID, slug="v07", name="v07")


def _response() -> EntityLinkingResolveResponse:
    candidate = EntityLinkingCandidateRead(
        entity_id=uuid.UUID("72000000-0000-0000-0000-000000000001"),
        item_hash="a" * 64,
        entity_type_id=TYPE_ID,
        entity_type_key="organization",
        entity_type_label="Organization",
        canonical_name="Acme",
        score_micros=1_000_000,
    )
    result = EntityLinkingResult(
        input_index=0,
        status="linked",
        method="exact_canonical",
        selected=candidate,
    )
    return EntityLinkingResolveResponse(
        contract_version="v1",
        policy_version="entity-linking-policy-v2",
        publication=EntityLinkingPublicationRead(
            id=PUBLICATION_ID,
            ontology_version_id=ONTOLOGY_ID,
            manifest_version="v1",
            manifest_hash="b" * 64,
            ontology_schema_hash="c" * 64,
            activated_at=datetime(2026, 7, 20, tzinfo=UTC),
        ),
        results=[result],
        counts=EntityLinkingCounts(
            mentions=1,
            linked_exact=1,
            linked_lexical=0,
            ambiguous=0,
            not_found=0,
            candidates=0,
        ),
    )


def _user(*, superuser: bool = True) -> User:
    return User(
        id=USER_ID,
        email="v07@example.com",
        is_superuser=superuser,
        is_active=True,
    )


def _client(db, *, superuser: bool = True) -> TestClient:
    async def override_db():
        return db

    async def override_user():
        return _user(superuser=superuser)

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[current_active_user] = override_user
    return TestClient(app)


def _clear_overrides() -> None:
    app.dependency_overrides.clear()


def test_v07_request_is_strict_bounded_and_rejects_normalized_empty_text():
    valid = EntityLinkingResolveRequest.model_validate(_request_payload())
    assert valid.max_candidates_per_mention == 5

    for payload in (
        _request_payload(extra="forbidden"),
        _request_payload(mentions=[{"text": "\u3000\t\n"}]),
        _request_payload(mentions=[{"text": "x" * 513}]),
        _request_payload(mentions=[{"text": "Acme", "unknown": True}]),
        _request_payload(max_candidates_per_mention=11),
    ):
        with pytest.raises(ValidationError):
            EntityLinkingResolveRequest.model_validate(payload)


def test_v07_scorer_uses_integer_features_and_exact_ambiguity_abstains():
    assert character_bigram_dice_micros("aa", "aa") == 1_000_000
    assert score_normalized_pair("alpha beta", "beta alpha") == 1_000_000

    decision = resolve_mention(
        "Acme",
        (_candidate(1, "Acme"), _candidate(2, "Acme", type_key="team")),
        entity_type_key=None,
        min_score_micros=950_000,
        min_margin_micros=200_000,
        candidate_floor_micros=500_000,
        max_candidates=10,
    )
    assert decision.status == "ambiguous"
    assert decision.method is None
    assert [row.candidate.entity_type_key for row in decision.candidates] == [
        "organization",
        "team",
    ]


def test_v07_scorer_applies_type_filter_margin_and_stable_uuid_tie_break():
    candidates = (
        _candidate(2, "Acme Holdings"),
        _candidate(1, "Acme Holdings"),
        _candidate(3, "Acme", type_key="team"),
    )
    ambiguous = resolve_mention(
        "Acme Holding",
        candidates,
        entity_type_key="organization",
        min_score_micros=800_000,
        min_margin_micros=200_000,
        candidate_floor_micros=500_000,
        max_candidates=10,
    )
    assert ambiguous.status == "ambiguous"
    assert [row.candidate.entity_id for row in ambiguous.candidates[:2]] == [
        candidates[1].entity_id,
        candidates[0].entity_id,
    ]

    exact = resolve_mention(
        "Acme",
        candidates,
        entity_type_key="team",
        min_score_micros=950_000,
        min_margin_micros=200_000,
        candidate_floor_micros=500_000,
        max_candidates=10,
    )
    assert exact.status == "linked"
    assert exact.method == "exact_canonical"
    assert exact.selected is not None
    assert exact.selected.candidate.entity_type_key == "team"


def _production_candidate(value: dict[str, str]) -> EntityLinkingCandidate:
    return EntityLinkingCandidate(
        entity_id=uuid.UUID(value["entity_id"]),
        item_hash=hashlib.sha256(value["entity_id"].encode("ascii")).hexdigest(),
        entity_type_id=TYPE_ID,
        entity_type_key=value["entity_type_key"],
        entity_type_label=value["entity_type_key"],
        canonical_name=value["canonical_name"],
        normalized_name=value["normalized_name"],
    )


def _reference_candidate(value: dict[str, str]) -> reference_scorer.EntityCandidate:
    return reference_scorer.EntityCandidate(**value)


def _decision_identity(decision) -> tuple[object, ...]:
    selected = decision.selected
    selected_value = (
        (str(selected.candidate.entity_id).lower(), selected.score_micros)
        if selected
        else None
    )
    candidates = tuple(
        (str(row.candidate.entity_id).lower(), row.score_micros)
        for row in decision.candidates
    )
    return decision.status, decision.method, selected_value, candidates


def _reference_decision_identity(decision) -> tuple[object, ...]:
    selected = decision.selected
    selected_value = (
        (selected.candidate.entity_id.lower(), selected.features.score_micros)
        if selected
        else None
    )
    candidates = tuple(
        (row.candidate.entity_id.lower(), row.features.score_micros)
        for row in decision.candidates
    )
    return decision.status, decision.method, selected_value, candidates


def test_v07_production_scorer_is_bit_for_bit_conformant_with_frozen_reference():
    payload = json.loads(
        Path("eval/entity_linking/conformance_v2.json").read_text(encoding="utf-8")
    )
    for case in payload["feature_cases"]:
        left = normalize_graph_name_v1(case["left"])
        right = normalize_graph_name_v1(case["right"])
        reference = reference_scorer.score_normalized_pair(left, right)
        bigram = character_bigram_dice_micros(left, right)
        assert bigram == (
            reference.character_bigram_dice_micros
        )
        assert token_jaccard_micros(left, right) == (
            reference.token_jaccard_micros
        )
        assert substring_containment_micros(left, right) == (
            reference.substring_containment_micros
        )
        assert boundary_omission_micros(left, right) == reference.boundary_omission_micros
        assert ordered_abbreviation_micros(
            left,
            right,
            bigram_micros=bigram,
        ) == reference.ordered_abbreviation_micros
        assert score_normalized_pair(left, right) == reference.score_micros

    for case in payload["ordering_cases"]:
        reference_candidates = [_reference_candidate(row) for row in case["candidates"]]
        production_candidates = [_production_candidate(row) for row in case["candidates"]]
        reference_order = sorted(
            (
                reference_scorer.score_candidate(case["mention_text"], row)
                for row in reference_candidates
            ),
            key=reference_scorer.stable_candidate_key,
        )
        production_order = sorted(
            (
                ScoredEntityLinkingCandidate(
                    row,
                    score_normalized_pair(
                        normalize_graph_name_v1(case["mention_text"]),
                        row.normalized_name,
                    ),
                )
                for row in production_candidates
            ),
            key=stable_candidate_key,
        )
        assert [str(row.candidate.entity_id).lower() for row in production_order] == [
            row.candidate.entity_id.lower() for row in reference_order
        ]

    for case in payload["decision_cases"]:
        reference_candidates = [_reference_candidate(row) for row in case["candidates"]]
        production_candidates = [_production_candidate(row) for row in case["candidates"]]
        reference_scored = [
            reference_scorer.ScoredCandidate(
                candidate,
                reference_scorer.FeatureScores(0, 0, 0, 0, 0, score),
            )
            for candidate, score in zip(
                reference_candidates,
                case["candidate_scores_micros"],
                strict=True,
            )
        ]
        production_scored = [
            ScoredEntityLinkingCandidate(candidate, score)
            for candidate, score in zip(
                production_candidates,
                case["candidate_scores_micros"],
                strict=True,
            )
        ]
        reference = reference_scorer.decide_scored_candidates(
            reference_scored,
            min_score_micros=case["min_score_micros"],
            min_margin_micros=case["min_margin_micros"],
        )
        production = decide_scored_candidates(
            production_scored,
            min_score_micros=case["min_score_micros"],
            min_margin_micros=case["min_margin_micros"],
        )
        assert _decision_identity(production) == _reference_decision_identity(reference)


def test_v07_response_invariants_and_privacy():
    payload = _response().model_dump(mode="json")
    assert "mentions" not in payload
    assert "text" not in str(payload)
    assert set(payload["results"][0]["selected"]) == {
        "entity_id",
        "item_hash",
        "entity_type_id",
        "entity_type_key",
        "entity_type_label",
        "canonical_name",
        "score_micros",
    }

    with pytest.raises(ValidationError):
        EntityLinkingResult(input_index=0, status="linked", method="lexical_v1")
    with pytest.raises(ValidationError):
        EntityLinkingResolveResponse(
            **(_response().model_dump() | {"counts": {"mentions": 1}})
        )


def test_v07_observation_and_metrics_use_only_frozen_fields_and_labels():
    response = _response()
    observation = EntityLinkingObservation(
        request_id="a" * 32,
        result_code="success",
        status_code=200,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        publication_id=PUBLICATION_ID,
        mention_count=response.counts.mentions,
        linked_exact_count=response.counts.linked_exact,
        linked_lexical_count=response.counts.linked_lexical,
        ambiguous_count=response.counts.ambiguous,
        not_found_count=response.counts.not_found,
        candidate_count=response.counts.candidates,
        duration_us=1234,
    )
    samples = entity_linking_metric_samples(observation)
    assert_sanitized_entity_linking_observation(
        observation,
        samples,
        forbidden_values=("must-never-echo", "secret-sql-parameter"),
    )
    assert {sample.name for sample in samples} == {
        "entity_linking_requests_total",
        "entity_linking_duration_seconds",
        "entity_linking_results_total",
        "entity_linking_candidates_returned",
    }
    assert all(
        key in {"result_code", "outcome"}
        for sample in samples
        for key in sample.labels
    )


def _policy_payload() -> dict[str, object]:
    sha = "a" * 64
    artifact_ref = {
        "repository_relative_path": "eval/entity_linking/results/calibration.json",
        "canonical_sha256": sha,
        "exact_file_sha256": sha,
    }
    return {
        "schema_version": "entity-linking-policy-v2",
        "policy_version": "entity-linking-policy-v2",
        "algorithm_version": "lexical-score-v2",
        "normalization_version": "normalize_graph_name_v1",
        "g2_approval_commit": "a" * 40,
        "g2_specification_tree_sha256": sha,
        "calibration_ref": artifact_ref,
        "approved_thresholds": {
            "min_score_micros": 920_000,
            "min_margin_micros": 120_000,
            "candidate_floor_micros": 500_000,
            "max_candidates": 10,
        },
        "approval_payload_sha256": sha,
        "dataset_manifest_ref": artifact_ref,
        "dataset_content_sha256": sha,
        "evaluation_config_sha256": sha,
        "ontology_schema_set_hash": sha,
        "code_commit": "b" * 40,
        "evaluation_tree_sha256": sha,
        "accepted_dependency_closure_sha256": sha,
        "reference_scorer_sha256": sha,
        "external_distribution_set_sha256": sha,
        "control_config_sha256": sha,
        "environment_fingerprint_sha256": sha,
        "pg_cluster_fingerprint_sha256": sha,
        "qdrant_fingerprint_sha256": sha,
        "embedding_fingerprint_sha256": sha,
        "approved_by": "v0.7-reviewer",
        "approved_at": "2026-07-20T12:00:00.000000Z",
        "approval_reference": "approval-record-v1",
    }


def test_v07_startup_config_is_fail_closed_and_binds_policy(tmp_path: Path):
    config = Settings(_env_file=None)
    validate_entity_linking_startup(config)
    assert config.entity_linking_enabled is False
    assert config.entity_linking_min_score_micros == 920_000
    assert config.entity_linking_min_margin_micros == 120_000

    with pytest.raises(RuntimeError):
        validate_entity_linking_startup(
            Settings(_env_file=None, entity_linking_max_candidates=9)
        )

    with pytest.raises(RuntimeError):
        validate_entity_linking_startup(
            Settings(_env_file=None, entity_linking_enabled=True)
        )

    policy_bytes = (
        json.dumps(
            _policy_payload(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    policy_path = tmp_path / "link_policy_v2.json"
    policy_path.write_bytes(policy_bytes)
    enabled = Settings(
        _env_file=None,
        entity_linking_enabled=True,
        entity_linking_policy_path=str(policy_path),
        entity_linking_policy_sha256=hashlib.sha256(policy_bytes).hexdigest(),
    )
    validate_entity_linking_startup(enabled)
    with pytest.raises(RuntimeError):
        validate_entity_linking_startup(
            enabled.model_copy(update={"entity_linking_policy_sha256": "0" * 64})
        )

    accepted_policy_path = Path("eval/entity_linking/link_policy_v11.json")
    accepted_policy_bytes = accepted_policy_path.read_bytes()
    validate_entity_linking_startup(
        Settings(
            _env_file=None,
            entity_linking_enabled=True,
            entity_linking_policy_path=str(accepted_policy_path),
            entity_linking_policy_sha256=hashlib.sha256(accepted_policy_bytes).hexdigest(),
        )
    )


def test_v07_candidate_projection_is_scoped_and_excludes_private_columns():
    snapshot = graph_retrieval.HealthyGraphSnapshot(
        publication_id=PUBLICATION_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        manifest_version="v1",
        manifest_hash="b" * 64,
        activated_at=datetime(2026, 7, 20, tzinfo=UTC),
        entity_count=1,
        relation_count=0,
    )
    sql = str(
        entity_linking._published_entity_projection(snapshot).compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).lower()
    for required in (
        "graph_publication_items.item_hash",
        "entities.id",
        "entities.entity_type_id",
        "entities.canonical_name",
        "entities.normalized_name",
        "entity_types.key",
        "entity_types.label",
        "graph_publication_items.publication_id",
        "graph_publication_items.library_id",
        "graph_publication_items.ontology_version_id",
    ):
        assert required in sql
    for forbidden in (
        "entities.properties",
        "entities.confidence",
        "entity_alias",
        "entity_mention",
        "evidence",
        "source_text",
    ):
        assert forbidden not in sql


def test_v07_service_runs_bounded_flow_and_rechecks_schema(monkeypatch):
    snapshot = graph_retrieval.HealthyGraphSnapshot(
        publication_id=PUBLICATION_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        manifest_version="v1",
        manifest_hash="b" * 64,
        activated_at=datetime(2026, 7, 20, tzinfo=UTC),
        entity_count=1,
        relation_count=0,
    )
    schema = entity_linking.OntologySchemaIdentity(
        schema_hash="c" * 64,
        entity_types=(
            entity_linking.OntologyEntityTypeIdentity(
                entity_type_id=TYPE_ID,
                key="organization",
                label="Organization",
            ),
        ),
    )
    candidate = _candidate(1, "Acme")
    start = AsyncMock(return_value=snapshot)
    load_schema = AsyncMock(side_effect=(schema, schema))
    load_candidates = AsyncMock(return_value=(candidate,))
    fence = AsyncMock()
    monkeypatch.setattr(graph_retrieval, "load_healthy_graph_snapshot", start)
    monkeypatch.setattr(entity_linking, "load_ontology_schema_identity", load_schema)
    monkeypatch.setattr(entity_linking, "load_published_entity_candidates", load_candidates)
    monkeypatch.setattr(graph_retrieval, "assert_graph_snapshot_still_current", fence)

    request = EntityLinkingResolveRequest.model_validate(_request_payload())
    response = asyncio.run(
        entity_linking.execute_entity_linking(
            AsyncMock(),
            _library(),
            request,
            config=Settings(_env_file=None),
        )
    )

    assert response.results[0].status == "linked"
    assert response.results[0].method == "exact_canonical"
    assert response.publication.ontology_schema_hash == schema.schema_hash
    start.assert_awaited_once()
    load_candidates.assert_awaited_once()
    fence.assert_awaited_once()
    assert load_schema.await_count == 2


def test_v07_service_rejects_unknown_type_before_candidate_projection(monkeypatch):
    snapshot = SimpleNamespace(entity_count=0)
    schema = entity_linking.OntologySchemaIdentity(
        schema_hash="c" * 64,
        entity_types=(),
    )
    candidates = AsyncMock()
    monkeypatch.setattr(
        graph_retrieval,
        "load_healthy_graph_snapshot",
        AsyncMock(return_value=snapshot),
    )
    monkeypatch.setattr(
        entity_linking,
        "load_ontology_schema_identity",
        AsyncMock(return_value=schema),
    )
    monkeypatch.setattr(entity_linking, "load_published_entity_candidates", candidates)

    request = EntityLinkingResolveRequest.model_validate(_request_payload())
    with pytest.raises(entity_linking.EntityLinkingServiceError) as exc_info:
        asyncio.run(
            entity_linking.execute_entity_linking(
                AsyncMock(),
                _library(),
                request,
                config=Settings(_env_file=None),
            )
        )
    assert exc_info.value.code == "entity_type_not_found"
    candidates.assert_not_awaited()


def test_v07_route_is_mounted_once_with_strict_contract():
    from app.api.v07_entity_linking import router as v07_router

    operation = app.openapi()["paths"][
        "/libraries/{slug}/v07/entity-links/resolve"
    ]["post"]
    assert operation["requestBody"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EntityLinkingResolveRequest"
    }
    assert operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/EntityLinkingResolveResponse"}
    assert operation["responses"]["404"]["content"]["application/json"][
        "schema"
    ] == {"$ref": "#/components/schemas/EntityLinkingErrorResponse"}
    assert len(v07_router.routes) == 1


def test_v07_validation_is_sanitized_and_does_not_echo_input(monkeypatch):
    monkeypatch.setattr(settings, "entity_linking_enabled", True)
    db = AsyncMock()
    secret = "must-never-echo"
    try:
        with patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())):
            response = _client(db).post(
                "/libraries/v07/v07/entity-links/resolve",
                json=_request_payload(mentions=[{"text": "Acme", "secret": secret}]),
            )
    finally:
        _clear_overrides()

    assert response.status_code == 422
    assert response.json() == {"detail": "entity_linking_invalid_request"}
    assert secret not in response.text


def test_v07_route_does_not_rewrite_existing_auth_or_permission_errors(monkeypatch):
    monkeypatch.setattr(settings, "entity_linking_enabled", True)
    unauthenticated = TestClient(app).post(
        "/libraries/v07/v07/entity-links/resolve",
        json=_request_payload(),
    )
    assert unauthenticated.status_code == 401

    db = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch("app.deps.has_permission", return_value=False),
        ):
            forbidden = _client(db, superuser=False).post(
                "/libraries/v07/v07/entity-links/resolve",
                json=_request_payload(),
            )
    finally:
        _clear_overrides()
    assert forbidden.status_code == 403
    assert forbidden.json() == {"detail": "forbidden"}


def test_v07_success_emits_allowlisted_observation(monkeypatch):
    monkeypatch.setattr(settings, "entity_linking_enabled", True)
    emitted = Mock()
    db = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v07_entity_linking.entity_linking.execute_entity_linking",
                new=AsyncMock(return_value=_response()),
            ),
            patch(
                "app.api.v07_entity_linking.emit_entity_linking_observation",
                new=emitted,
            ),
        ):
            response = _client(db).post(
                "/libraries/v07/v07/entity-links/resolve",
                json=_request_payload(),
            )
    finally:
        _clear_overrides()
    assert response.status_code == 200
    observation = emitted.call_args.args[0]
    assert observation.result_code == "success"
    assert observation.mention_count == 1
    assert observation.linked_exact_count == 1
    assert observation.publication_recheck_failed is False


def test_v07_disabled_route_calls_no_service(monkeypatch):
    monkeypatch.setattr(settings, "entity_linking_enabled", False)
    service = AsyncMock()
    db = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch("app.api.v07_entity_linking.entity_linking.execute_entity_linking", service),
        ):
            response = _client(db).post(
                "/libraries/v07/v07/entity-links/resolve",
                json=_request_payload(),
            )
    finally:
        _clear_overrides()

    assert response.status_code == 503
    assert response.json() == {"detail": "entity_linking_disabled"}
    service.assert_not_awaited()


@pytest.mark.parametrize(
    ("code", "expected_status"),
    [
        ("publication_changed", 409),
        ("entity_type_not_found", 409),
        ("entity_linking_limit_exceeded", 422),
        ("graph_publication_unavailable", 503),
        ("graph_publication_invariant_failed", 503),
    ],
)
def test_v07_service_errors_are_sanitized_and_rollback(
    monkeypatch,
    code,
    expected_status,
):
    monkeypatch.setattr(settings, "entity_linking_enabled", True)
    service = AsyncMock(side_effect=entity_linking.EntityLinkingServiceError(code))
    db = AsyncMock()
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v07_entity_linking.entity_linking.execute_entity_linking",
                service,
            ),
        ):
            response = _client(db).post(
                "/libraries/v07/v07/entity-links/resolve",
                json=_request_payload(),
            )
    finally:
        _clear_overrides()

    assert response.status_code == expected_status
    assert response.json() == {"detail": code}
    db.rollback.assert_awaited_once()
    db.commit.assert_not_awaited()


def test_v07_timeout_and_unknown_errors_are_sanitized_and_rollback(monkeypatch):
    monkeypatch.setattr(settings, "entity_linking_enabled", True)
    monkeypatch.setattr(settings, "entity_linking_timeout_seconds", 0.001)
    db = AsyncMock()

    async def slow(*_args, **_kwargs):
        await asyncio.sleep(1)

    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v07_entity_linking.entity_linking.execute_entity_linking",
                new=slow,
            ),
        ):
            timeout_response = _client(db).post(
                "/libraries/v07/v07/entity-links/resolve",
                json=_request_payload(),
            )
    finally:
        _clear_overrides()
    assert timeout_response.status_code == 504
    assert timeout_response.json() == {"detail": "entity_linking_timeout"}
    db.rollback.assert_awaited_once()

    db.reset_mock()
    secret = "secret-sql-parameter"
    try:
        with (
            patch("app.deps.load_active_library", new=AsyncMock(return_value=_library())),
            patch(
                "app.api.v07_entity_linking.entity_linking.execute_entity_linking",
                new=AsyncMock(side_effect=RuntimeError(secret)),
            ),
        ):
            unknown_response = _client(db).post(
                "/libraries/v07/v07/entity-links/resolve",
                json=_request_payload(),
            )
    finally:
        _clear_overrides()
    assert unknown_response.status_code == 500
    assert unknown_response.json() == {"detail": "entity_linking_internal_error"}
    assert secret not in unknown_response.text
    db.rollback.assert_awaited_once()
