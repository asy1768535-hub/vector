from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.services.graph_extraction_context import (
    ContextBuildError,
    build_context_snapshot,
)


LIB_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
DOC_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")
REV_ID = uuid.UUID("30000000-0000-0000-0000-000000000001")
JOB_ID = uuid.UUID("40000000-0000-0000-0000-000000000001")
UNIT_ID = uuid.UUID("50000000-0000-0000-0000-000000000001")


class _Result:
    def __init__(self, rows):
        self.rows = list(rows)

    def scalars(self):
        return self

    def first(self):
        return self.rows[0] if self.rows else None

    def all(self):
        return list(self.rows)


class FakeDB:
    def __init__(self, *, objects: dict, query_rows: list[list[object]]):
        self.objects = objects
        self.query_rows = list(query_rows)
        self.added = []
        self.execute_count = 0

    async def get(self, model, object_id):
        return self.objects.get((model, object_id))

    async def execute(self, _statement):
        self.execute_count += 1
        assert self.query_rows, "unexpected context query"
        return _Result(self.query_rows.pop(0))

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid.uuid4()


def _id(prefix: int, suffix: int) -> uuid.UUID:
    return uuid.UUID(f"{prefix:08x}-0000-0000-0000-{suffix:012x}")


def _chunk(
    suffix: int,
    seq: int,
    text: str,
    *,
    title_path=None,
    block_id=None,
    evidence_id=None,
):
    return SimpleNamespace(
        id=_id(0x60000000, suffix),
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        block_id=block_id,
        evidence_id=evidence_id,
        seq=seq,
        text=text,
        chunk_kind="text",
        page_start=1,
        page_end=1,
        title_path=title_path,
        source_start=seq * 10,
        source_end=seq * 10 + len(text),
        position={"line": seq + 1},
        chunk_metadata={"seq": seq},
    )


def _evidence(suffix: int, *, status="active", library_id=LIB_ID):
    return SimpleNamespace(
        id=_id(0x70000000, suffix),
        library_id=library_id,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        status=status,
    )


def _fixture(*, metadata=None, center_direct_status="active"):
    direct_center_block_id = _id(0x80000000, 1)
    previous_block_id = _id(0x80000000, 2)
    next_primary_block_id = _id(0x80000000, 3)

    center_primary = _evidence(1, status=center_direct_status)
    center_extra = _evidence(2)
    center_inactive = _evidence(3, status="stale")
    previous_evidence = _evidence(4)
    next_evidence = _evidence(5)
    other_evidence = _evidence(6)

    previous = _chunk(1, 1, "previous", block_id=previous_block_id)
    center = _chunk(
        2,
        2,
        "center fact",
        title_path=["Policy"],
        block_id=direct_center_block_id,
        evidence_id=center_primary.id,
    )
    following = _chunk(3, 3, "next", evidence_id=next_evidence.id)
    other_heading = _chunk(
        4,
        4,
        "different heading",
        title_path=["Appendix"],
        evidence_id=other_evidence.id,
    )
    chunks = [previous, center, following, other_heading]

    direct_center_block = SimpleNamespace(
        id=direct_center_block_id,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        title_path=["Wrong direct path"],
        block_kind="paragraph",
    )
    previous_block = SimpleNamespace(
        id=previous_block_id,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        title_path=["Policy"],
        block_kind="paragraph",
    )
    next_primary_block = SimpleNamespace(
        id=next_primary_block_id,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        title_path=["Policy"],
        block_kind="paragraph",
    )
    chunk_blocks = [
        SimpleNamespace(
            chunk_id=following.id,
            document_block_id=next_primary_block_id,
            document_revision_id=REV_ID,
            seq=0,
        )
    ]
    links = [
        SimpleNamespace(
            chunk_id=center.id,
            evidence_id=center_extra.id,
            document_revision_id=REV_ID,
            seq=0,
        ),
        SimpleNamespace(
            chunk_id=center.id,
            evidence_id=center_inactive.id,
            document_revision_id=REV_ID,
            seq=1,
        ),
        SimpleNamespace(
            chunk_id=previous.id,
            evidence_id=previous_evidence.id,
            document_revision_id=REV_ID,
            seq=0,
        ),
    ]
    evidence = [
        center_primary,
        center_extra,
        center_inactive,
        previous_evidence,
        next_evidence,
        other_evidence,
    ]

    revision = SimpleNamespace(
        id=REV_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        title="Policy document",
        document_metadata=metadata,
    )
    document = SimpleNamespace(
        id=DOC_ID,
        library_id=LIB_ID,
        title="Fallback title",
        doc_metadata={"legacy": True},
        deleted_at=None,
    )
    job = SimpleNamespace(
        id=JOB_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        ontology_snapshot={
            "entity_types": [{"key": "department"}],
            "relation_types": [{"key": "responsible_for"}],
        },
        ontology_snapshot_hash="a" * 64,
        context_policy_version="context-v1",
    )
    active_center_id = center_primary.id if center_direct_status == "active" else center_extra.id
    unit = SimpleNamespace(
        id=UNIT_ID,
        job_id=JOB_ID,
        library_id=LIB_ID,
        document_revision_id=REV_ID,
        center_chunk_id=center.id,
        center_evidence_id=active_center_id,
    )
    objects = {
        (DocumentRevision, REV_ID): revision,
        (Document, DOC_ID): document,
        (Chunk, center.id): center,
    }
    query_rows = [
        [],
        chunks,
        chunk_blocks,
        [direct_center_block, previous_block, next_primary_block],
        links,
        evidence,
    ]
    return FakeDB(objects=objects, query_rows=query_rows), job, unit


def _build(db, job, unit, **kwargs):
    return asyncio.run(
        build_context_snapshot(
            db,
            job=job,
            unit=unit,
            previous_chunks=kwargs.pop("previous_chunks", 1),
            next_chunks=kwargs.pop("next_chunks", 1),
            max_context_chars=kwargs.pop("max_context_chars", 20_000),
            **kwargs,
        )
    )


def test_context_uses_title_precedence_stable_neighbors_and_all_active_evidence():
    db, job, unit = _fixture(metadata={"owner": "HR"})
    snapshot = _build(db, job, unit)

    assert snapshot.title_path_source == "chunk"
    assert snapshot.effective_title_path == ["Policy"]
    assert snapshot.previous_chunk_ids == [str(_id(0x60000000, 1))]
    assert snapshot.next_chunk_ids == [str(_id(0x60000000, 3))]
    assert list(snapshot.context_mapping) == ["c0", "p1", "n1"]
    assert snapshot.context_mapping["c0"]["primary_evidence_id"] == str(
        _id(0x70000000, 1)
    )
    assert snapshot.context_mapping["c0"]["evidence_ids"] == [
        str(_id(0x70000000, 1)),
        str(_id(0x70000000, 2)),
    ]
    assert snapshot.context_mapping["p1"]["primary_evidence_id"] == str(
        _id(0x70000000, 4)
    )
    assert snapshot.context_json["chunks"][0]["context_ref"] == "c0"
    assert snapshot.context_json["chunks"][0]["text"] == "center fact"
    assert snapshot.context_hash and len(snapshot.context_hash) == 64


def test_inactive_direct_evidence_falls_back_to_first_active_link():
    db, job, unit = _fixture(center_direct_status="stale")
    snapshot = _build(db, job, unit)

    assert snapshot.center_evidence_id == _id(0x70000000, 2)
    assert snapshot.context_mapping["c0"]["evidence_ids"] == [
        str(_id(0x70000000, 2))
    ]


def test_context_rejects_cross_scope_unit_before_reading_payload_rows():
    db, job, unit = _fixture()
    unit.library_id = uuid.uuid4()

    with pytest.raises(ContextBuildError, match="scope"):
        _build(db, job, unit)
    assert db.execute_count == 1


def test_context_rejects_center_without_active_evidence():
    db, job, unit = _fixture(center_direct_status="stale")
    db.query_rows[-2] = []
    db.query_rows[-1] = [_evidence(1, status="stale")]

    with pytest.raises(ContextBuildError, match="active Evidence"):
        _build(db, job, unit)


def test_budget_removes_neighbors_before_document_metadata():
    no_metadata_db, job, unit = _fixture(metadata=None)
    baseline = _build(
        no_metadata_db,
        job,
        unit,
        previous_chunks=0,
        next_chunks=0,
    )

    metadata_db, job, unit = _fixture(metadata={"large": "x" * 1000})
    snapshot = _build(
        metadata_db,
        job,
        unit,
        previous_chunks=0,
        next_chunks=0,
        max_context_chars=baseline.context_char_count,
    )
    assert snapshot.document_metadata is None
    assert snapshot.context_char_count <= baseline.context_char_count

    full_db, job, unit = _fixture(metadata=None)
    full = _build(full_db, job, unit)
    trimmed_db, job, unit = _fixture(metadata=None)
    trimmed = _build(
        trimmed_db,
        job,
        unit,
        max_context_chars=full.context_char_count - 1,
    )
    assert len(trimmed.previous_chunk_ids) + len(trimmed.next_chunk_ids) == 1
    assert trimmed.document_metadata is None


def test_center_context_overflow_fails_instead_of_truncating():
    db, job, unit = _fixture(metadata=None)
    with pytest.raises(ContextBuildError, match="exceeds"):
        _build(
            db,
            job,
            unit,
            previous_chunks=0,
            next_chunks=0,
            max_context_chars=20,
        )


def test_frozen_ontology_does_not_consume_document_context_budget():
    db, job, unit = _fixture(metadata=None)
    job.ontology_snapshot = {
        "relation_constraints": [
            {
                "relation_type_id": str(_id(0x80000000, index)),
                "source_entity_type_id": str(_id(0x81000000, index)),
                "target_entity_type_id": str(_id(0x82000000, index)),
            }
            for index in range(128)
        ]
    }

    snapshot = _build(
        db,
        job,
        unit,
        previous_chunks=0,
        next_chunks=0,
        max_context_chars=2_000,
    )

    assert "ontology" not in snapshot.context_json
    assert snapshot.ontology_snapshot_hash == job.ontology_snapshot_hash
    assert snapshot.context_char_count <= 2_000


def test_context_hash_is_deterministic_for_identical_inputs():
    first_db, first_job, first_unit = _fixture(metadata={"b": 2, "a": 1})
    second_db, second_job, second_unit = _fixture(metadata={"a": 1, "b": 2})
    first = _build(first_db, first_job, first_unit)
    second = _build(second_db, second_job, second_unit)

    assert first.context_hash == second.context_hash
    assert first.context_text == second.context_text


def test_existing_snapshot_is_idempotent_but_purged_snapshot_is_rejected():
    existing = SimpleNamespace(id=uuid.uuid4(), purged_at=None)
    db = FakeDB(objects={}, query_rows=[[existing]])
    job = SimpleNamespace(id=JOB_ID)
    unit = SimpleNamespace(id=UNIT_ID)
    assert _build(db, job, unit) is existing
    assert db.execute_count == 1

    purged = SimpleNamespace(id=uuid.uuid4(), purged_at=object())
    db = FakeDB(objects={}, query_rows=[[purged]])
    with pytest.raises(ContextBuildError, match="purged"):
        _build(db, job, unit)
