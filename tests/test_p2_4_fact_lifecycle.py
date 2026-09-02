from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.services.fact_lifecycle import (
    _facts_have_functional_conflict,
    _has_functional_source_contract,
    _lock_fact,
    _lock_functional_scope,
    _status_from_assertions,
    derive_assertion_lifecycle_status,
    is_effectively_supported_assertion,
    logical_fact_conflict_status,
    reconcile_resolved_fact_decision,
    time_overlap_v1,
)
from app.services.graph_relation_fact_resolution import _logical_fact_lock_key


def _assertion(**changes):
    values = {
        "status": "active",
        "id": uuid.uuid4(),
        "polarity": "affirmed",
        "modality": "confirmed",
        "asserted_value": None,
        "valid_time": None,
        "effective_time": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def _evidence(*, status="active", support_type="supports"):
    return SimpleNamespace(status=status, support_type=support_type)


def test_assertion_lifecycle_keeps_active_when_any_bridged_evidence_is_active():
    assert derive_assertion_lifecycle_status(
        "active", [_evidence(status="stale"), _evidence()]
    ) == "active"


def test_assertion_lifecycle_marks_only_fully_stale_bridged_sources_stale():
    assert derive_assertion_lifecycle_status(
        "active", [_evidence(status="stale"), _evidence(status="deleted")]
    ) == "stale"


def test_unbridged_legacy_and_rejected_assertions_are_conservative():
    assert derive_assertion_lifecycle_status("active", []) == "active"
    assert derive_assertion_lifecycle_status("rejected", [_evidence()]) == "rejected"


def test_assertion_lifecycle_derivation_is_idempotent():
    evidence = [_evidence(status="stale")]
    status = derive_assertion_lifecycle_status("active", evidence)
    assert derive_assertion_lifecycle_status(status, evidence) == status


def test_current_decision_can_reactivate_a_superseded_assertion_with_live_source():
    assert derive_assertion_lifecycle_status(
        "superseded", [_evidence()], allow_reactivation=True
    ) == "active"
    assert derive_assertion_lifecycle_status(
        "superseded", [_evidence(status="stale")], allow_reactivation=True
    ) == "superseded"


def test_effective_support_requires_active_supports_evidence():
    assertion = _assertion()
    assert is_effectively_supported_assertion(assertion, [_evidence()])
    assert not is_effectively_supported_assertion(assertion, [_evidence(support_type="contradicts")])
    assert not is_effectively_supported_assertion(_assertion(status="stale"), [_evidence()])


def test_time_overlap_is_conservative_for_unknown_or_incompatible_dimensions():
    assert time_overlap_v1(None, None, None, None) is True
    assert time_overlap_v1({"kind": "instant", "value": "2025-01-01"}, None, None, None) is None
    assert time_overlap_v1(
        {"kind": "instant", "value": "2025-01-01"},
        {"kind": "instant", "value": "2025-01-01T00:00:00Z"},
        None,
        None,
    ) is None


def test_time_overlap_uses_closed_intervals_and_rejects_non_overlap():
    assert time_overlap_v1(
        {"kind": "range", "from": "2025-01-01", "to": "2025-01-31"},
        {"kind": "instant", "value": "2025-01-31"},
        None,
        None,
    ) is True
    assert time_overlap_v1(
        {"kind": "range", "from": "2025-01-01", "to": "2025-01-31"},
        {"kind": "range", "from": "2025-02-01", "to": "2025-02-28"},
        None,
        None,
    ) is False


def test_measurement_conflict_requires_distinct_values_and_factual_overlap():
    twenty = _assertion(asserted_value={"kind": "decimal", "unit": "ratio", "value": "0.2"})
    thirty = _assertion(asserted_value={"kind": "decimal", "unit": "ratio", "value": "0.3"})
    assert logical_fact_conflict_status("measurement_slot", [twenty, thirty]) == "conflicted"
    assert logical_fact_conflict_status("measurement_slot", [twenty, twenty]) == "active"
    assert logical_fact_conflict_status("measurement_slot", [_assertion(modality="planned"), thirty]) == "active"


def test_measurement_conflict_clears_for_stale_or_temporally_distinct_assertions():
    twenty = _assertion(asserted_value={"kind": "decimal", "unit": "ratio", "value": "0.2"})
    thirty = _assertion(asserted_value={"kind": "decimal", "unit": "ratio", "value": "0.3"})
    evidence = {twenty.id: [_evidence()], thirty.id: [_evidence()]}
    assert _status_from_assertions("measurement_slot", [twenty, thirty], evidence) == "conflicted"

    thirty.status = "stale"
    assert _status_from_assertions("measurement_slot", [twenty, thirty], evidence) == "active"

    historic = _assertion(
        asserted_value={"kind": "decimal", "unit": "ratio", "value": "0.3"},
        valid_time={"kind": "instant", "value": "2026-01-01"},
    )
    twenty.valid_time = {"kind": "instant", "value": "2025-01-01"}
    assert logical_fact_conflict_status("measurement_slot", [twenty, historic]) == "active"
    assert logical_fact_conflict_status(
        "measurement_slot",
        [twenty, _assertion(asserted_value=historic.asserted_value)],
    ) == "active"


def test_polarity_conflict_needs_factual_modalities_and_time_overlap():
    affirmed = _assertion()
    negated = _assertion(polarity="negated")
    assert logical_fact_conflict_status("state_fact", [affirmed, negated]) == "conflicted"
    assert logical_fact_conflict_status("state_fact", [affirmed, _assertion(polarity="unknown")]) == "active"


def test_completed_and_confirmed_are_factual_but_other_modalities_do_not_conflict():
    confirmed = _assertion(asserted_value={"kind": "decimal", "value": "0.2"})
    completed = _assertion(
        modality="completed",
        asserted_value={"kind": "decimal", "value": "0.3"},
    )
    assert logical_fact_conflict_status("measurement_slot", [confirmed, completed]) == "conflicted"
    for modality in ("planned", "possible", "expected", "unknown"):
        assert logical_fact_conflict_status(
            "measurement_slot",
            [confirmed, _assertion(modality=modality, asserted_value=completed.asserted_value)],
        ) == "active"


def test_fact_status_clears_conflict_and_becomes_inactive_when_support_is_lost():
    assertion = _assertion()
    assert _status_from_assertions("state_fact", [assertion], {assertion.id: [_evidence()]}) == "active"
    assertion.status = "stale"
    assert _status_from_assertions("state_fact", [assertion], {assertion.id: [_evidence(status="stale")]}) == "inactive"
    assertion.status = "active"
    assert _status_from_assertions("state_fact", [assertion], {assertion.id: []}) == "active"
    assert _status_from_assertions(
        "state_fact",
        [assertion],
        {assertion.id: [_evidence(support_type="contradicts")]},
    ) == "inactive"


def test_non_overlapping_polarity_does_not_conflict():
    affirmed = _assertion(valid_time={"kind": "instant", "value": "2025-01-01"})
    negated = _assertion(
        polarity="negated",
        valid_time={"kind": "instant", "value": "2026-01-01"},
    )
    assert logical_fact_conflict_status("state_fact", [affirmed, negated]) == "active"


def test_functional_conflict_requires_each_assertion_to_have_a_functional_contract():
    left = _assertion()
    right = _assertion()
    with patch(
        "app.services.fact_lifecycle._has_functional_source_contract",
        new=AsyncMock(side_effect=[True, True]),
    ):
        assert asyncio.run(
            _facts_have_functional_conflict(
                None,
                library_id=uuid.uuid4(),
                left_assertions=[left],
                right_assertions=[right],
            )
        )
    with patch(
        "app.services.fact_lifecycle._has_functional_source_contract",
        new=AsyncMock(side_effect=[True, False]),
    ):
        assert not asyncio.run(
            _facts_have_functional_conflict(
                None,
                library_id=uuid.uuid4(),
                left_assertions=[left],
                right_assertions=[right],
            )
        )


def test_functional_conflict_requires_business_time_overlap():
    left = _assertion(valid_time={"kind": "instant", "value": "2025-01-01"})
    right = _assertion(valid_time={"kind": "instant", "value": "2026-01-01"})
    with patch(
        "app.services.fact_lifecycle._has_functional_source_contract",
        new=AsyncMock(side_effect=[True, True]),
    ):
        assert not asyncio.run(
            _facts_have_functional_conflict(
                None,
                library_id=uuid.uuid4(),
                left_assertions=[left],
                right_assertions=[right],
            )
        )


def test_functional_cardinality_is_source_directional_and_fails_closed_when_missing():
    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    library_id = uuid.uuid4()
    relation = SimpleNamespace(
        id=uuid.uuid4(),
        library_id=library_id,
        ontology_version_id=uuid.uuid4(),
        relation_type_id=uuid.uuid4(),
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
    )
    source = SimpleNamespace(entity_type_id=uuid.uuid4())
    target = SimpleNamespace(entity_type_id=uuid.uuid4())
    assertion = _assertion(knowledge_relation_id=relation.id)

    async def has_contract(rows):
        db = SimpleNamespace(
            get=AsyncMock(side_effect=[relation, source, target]),
            execute=AsyncMock(return_value=_Result(rows)),
        )
        return await _has_functional_source_contract(db, library_id=library_id, assertion=assertion)

    assert asyncio.run(has_contract([SimpleNamespace(cardinality="many_to_one")]))
    assert not asyncio.run(has_contract([SimpleNamespace(cardinality="one_to_many")]))
    assert not asyncio.run(has_contract([SimpleNamespace(cardinality="many_to_many")]))
    assert not asyncio.run(has_contract([]))


def test_postgresql_locks_are_fact_and_functional_scope_local():
    class _Db:
        def __init__(self):
            self.calls = []

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement, params):
            self.calls.append((str(statement), params["lock_key"]))

    db = _Db()
    library_id = uuid.uuid4()
    first = SimpleNamespace(
        subject_canonical_entity_id=uuid.uuid4(),
        stable_predicate_identity_id=uuid.uuid4(),
        identity_qualifiers={},
        identity_policy_version="v1",
    )
    second = SimpleNamespace(**{**first.__dict__, "subject_canonical_entity_id": uuid.uuid4()})

    asyncio.run(_lock_fact(db, library_id, "a" * 64))
    asyncio.run(_lock_fact(db, library_id, "b" * 64))
    asyncio.run(_lock_functional_scope(db, library_id, first))
    asyncio.run(_lock_functional_scope(db, library_id, second))

    assert db.calls[0][1] == _logical_fact_lock_key(library_id, "a" * 64)
    assert db.calls[0][1] != db.calls[1][1]
    assert db.calls[2][1] != db.calls[3][1]


def test_a_to_b_to_a_reactivates_current_assertion_and_supersedes_replaced_one():
    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    library_id = uuid.uuid4()
    assertion_a = SimpleNamespace(
        id=uuid.uuid4(), library_id=library_id, logical_fact_id=uuid.uuid4(), status="superseded"
    )
    assertion_b = SimpleNamespace(
        id=uuid.uuid4(), library_id=library_id, logical_fact_id=uuid.uuid4(), status="active"
    )
    old_decision = SimpleNamespace(fact_assertion_id=assertion_b.id)
    decision = SimpleNamespace(
        library_id=library_id,
        status="resolved",
        fact_assertion_id=assertion_a.id,
        supersedes_decision_id=uuid.uuid4(),
    )
    db = SimpleNamespace(
        get=AsyncMock(side_effect=[assertion_a, old_decision, assertion_b]),
        execute=AsyncMock(side_effect=[_Result([_evidence()]), _Result([])]),
    )

    with patch(
        "app.services.fact_lifecycle.recalculate_logical_fact_status",
        new=AsyncMock(),
    ) as recalculate:
        asyncio.run(reconcile_resolved_fact_decision(db, library_id=library_id, decision=decision))

    assert assertion_a.status == "active"
    assert assertion_b.status == "superseded"
    assert {call.kwargs["logical_fact_id"] for call in recalculate.await_args_list} == {
        assertion_a.logical_fact_id,
        assertion_b.logical_fact_id,
    }
