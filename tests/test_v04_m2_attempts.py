from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from app.services.graph_extraction_attempts import (
    AttemptCompletion,
    AttemptStateError,
    create_pending_attempt,
    finalize_attempt,
)


UNIT_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
SNAPSHOT_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
ATTEMPT_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
CLAIM_TOKEN = uuid.UUID("40000000-0000-0000-0000-000000000001")
REQUEST_HASH = "a" * 64


class _Result:
    def __init__(self, *, rows=None, scalar=None, rowcount=None):
        self.rows = list(rows or [])
        self.scalar = scalar
        self.rowcount = rowcount

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar_one_or_none(self):
        return self.scalar


class _Transaction:
    def __init__(self, db):
        self.db = db

    async def __aenter__(self):
        self.db.transaction_entries += 1
        return self

    async def __aexit__(self, exc_type, _exc, _tb):
        if exc_type is None:
            self.db.commits += 1
        else:
            self.db.rollbacks += 1
        return False


class FakeDB:
    def __init__(self, results):
        self.results = list(results)
        self.statements = []
        self.added = []
        self.transaction_entries = 0
        self.commits = 0
        self.rollbacks = 0

    def begin(self):
        return _Transaction(self)

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.results, "unexpected attempt query"
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = ATTEMPT_ID


def _unit(*, attempts=1):
    return SimpleNamespace(
        id=UNIT_ID,
        status="processing",
        claim_token=CLAIM_TOKEN,
        model_attempt_count=attempts,
    )


def _create(db, **kwargs):
    return asyncio.run(
        create_pending_attempt(
            db,
            unit_id=kwargs.pop("unit_id", UNIT_ID),
            context_snapshot_id=kwargs.pop("context_snapshot_id", SNAPSHOT_ID),
            claim_token=kwargs.pop("claim_token", CLAIM_TOKEN),
            request_payload_hash=kwargs.pop("request_payload_hash", REQUEST_HASH),
            max_attempts=kwargs.pop("max_attempts", 3),
            **kwargs,
        )
    )


def _finalize(db, completion):
    return asyncio.run(
        finalize_attempt(
            db,
            attempt_id=ATTEMPT_ID,
            claim_token=CLAIM_TOKEN,
            completion=completion,
        )
    )


def test_create_pending_attempt_locks_unit_allocates_monotonic_number_and_budget():
    unit = _unit(attempts=1)
    db = FakeDB(
        [
            _Result(rows=[unit]),
            _Result(scalar=SNAPSHOT_ID),
            _Result(scalar=2),
        ]
    )
    attempt = _create(db)

    assert attempt.id == ATTEMPT_ID
    assert attempt.extraction_unit_id == UNIT_ID
    assert attempt.context_snapshot_id == SNAPSHOT_ID
    assert attempt.attempt_no == 3
    assert attempt.claim_token == CLAIM_TOKEN
    assert attempt.request_status == "pending"
    assert attempt.request_payload_hash == REQUEST_HASH
    assert unit.model_attempt_count == 2
    assert db.transaction_entries == 1
    assert db.commits == 1
    assert db.rollbacks == 0
    assert "FOR UPDATE" in str(db.statements[0]).upper()


def test_create_pending_attempt_rejects_missing_live_claim_and_exhausted_budget():
    missing_claim_db = FakeDB([_Result(rows=[])])
    with pytest.raises(AttemptStateError, match="live claim"):
        _create(missing_claim_db)
    assert missing_claim_db.rollbacks == 1

    exhausted_db = FakeDB([_Result(rows=[_unit(attempts=3)])])
    with pytest.raises(AttemptStateError, match="budget"):
        _create(exhausted_db)
    assert exhausted_db.rollbacks == 1


def test_create_pending_attempt_rejects_wrong_or_purged_context_snapshot():
    db = FakeDB([_Result(rows=[_unit()]), _Result(scalar=None)])
    with pytest.raises(AttemptStateError, match="Context Snapshot"):
        _create(db)


def test_create_pending_attempt_validates_hash_and_positive_budget_before_transaction():
    db = FakeDB([])
    with pytest.raises(ValueError, match="SHA-256"):
        _create(db, request_payload_hash="not-a-hash")
    with pytest.raises(ValueError, match="max_attempts"):
        _create(db, max_attempts=0)
    assert db.transaction_entries == 0


def test_finalize_attempt_is_one_fenced_update_with_all_result_fields():
    db = FakeDB([_Result(rowcount=1)])
    completion = AttemptCompletion(
        request_status="succeeded",
        parse_status="valid",
        latency_ms=125,
        provider_request_id="provider-1",
        raw_response='{"entities":[]}',
        parsed_response={"entities": [], "relations": []},
        input_token_count=11,
        output_token_count=7,
        finish_reason="stop",
    )
    assert _finalize(db, completion) is True
    assert db.commits == 1

    statement = db.statements[0]
    sql = str(statement.compile(dialect=postgresql.dialect())).lower()
    params = statement.compile(dialect=postgresql.dialect()).params
    assert "update extraction_raw_output_attempts" in sql
    assert "request_status" in sql and "pending" in params.values()
    assert "exists" in sql
    assert "graph_extraction_units.status" in sql
    assert "lease_expires_at" in sql
    assert 125 in params.values()
    assert "provider-1" in params.values()
    assert 11 in params.values() and 7 in params.values()


def test_finalize_attempt_returns_false_when_claim_or_lease_is_lost_and_is_terminal_safe():
    db = FakeDB([_Result(rowcount=0)])
    completion = AttemptCompletion(
        request_status="timeout",
        latency_ms=300,
        parse_error="provider timed out",
    )
    assert _finalize(db, completion) is False
    sql = str(db.statements[0].compile(dialect=postgresql.dialect())).lower()
    assert "request_status" in sql
    assert "claim_token" in sql
    assert db.commits == 1


@pytest.mark.parametrize(
    "completion",
    [
        AttemptCompletion(request_status="succeeded", latency_ms=1),
        AttemptCompletion(
            request_status="timeout",
            parse_status="invalid_json",
            latency_ms=1,
        ),
        AttemptCompletion(request_status="succeeded", parse_status="valid", latency_ms=-1),
    ],
)
def test_finalize_attempt_rejects_invalid_terminal_parse_contract(completion):
    db = FakeDB([])
    with pytest.raises(ValueError):
        _finalize(db, completion)
    assert db.transaction_entries == 0


def test_finalize_attempt_sanitizes_and_bounds_parse_error_fields():
    db = FakeDB([_Result(rowcount=1)])
    completion = AttemptCompletion(
        request_status="succeeded",
        parse_status="invalid_schema",
        latency_ms=9,
        parse_error="bad\x00schema " + "x" * 2000,
        provider_request_id="r" * 400,
        finish_reason="f" * 100,
    )
    assert _finalize(db, completion) is True
    params = db.statements[0].compile(dialect=postgresql.dialect()).params.values()
    strings = [value for value in params if isinstance(value, str)]
    assert any(value.startswith("bad schema") and len(value) == 512 for value in strings)
    assert "r" * 255 in strings
    assert "f" * 64 in strings
