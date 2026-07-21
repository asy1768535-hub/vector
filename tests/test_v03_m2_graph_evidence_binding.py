from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest


LIB_ID = uuid.uuid4()
OTHER_LIB_ID = uuid.uuid4()
ONTOLOGY_ID = uuid.uuid4()
ENTITY_TYPE_ID = uuid.uuid4()
RELATION_TYPE_ID = uuid.uuid4()
ENTITY_ID = uuid.uuid4()
RELATION_ID = uuid.uuid4()
EVIDENCE_ID = uuid.uuid4()
DOC_ID = uuid.uuid4()
REVISION_ID = uuid.uuid4()
CHUNK_ID = uuid.uuid4()


class FakeDB:
    def __init__(self, *objects):
        self.objects = {(type(obj), obj.id): obj for obj in objects}
        self.added = []
        self.flush_count = 0

    async def get(self, model, object_id):
        return self.objects.get((model, object_id))

    def add(self, obj) -> None:
        self.added.append(obj)
        self.objects[(type(obj), obj.id)] = obj

    async def flush(self) -> None:
        self.flush_count += 1


def _lib(library_id: uuid.UUID = LIB_ID):
    return SimpleNamespace(id=library_id)


def _entity(*, library_id: uuid.UUID = LIB_ID):
    from app.models.entity import Entity

    return Entity(
        id=ENTITY_ID,
        library_id=library_id,
        ontology_version_id=ONTOLOGY_ID,
        entity_type_id=ENTITY_TYPE_ID,
        canonical_name="Alice",
        normalized_name="alice",
        status="active",
        source_type="manual",
    )


def _relation(*, library_id: uuid.UUID = LIB_ID):
    from app.models.knowledge_relation import KnowledgeRelation

    return KnowledgeRelation(
        id=RELATION_ID,
        library_id=library_id,
        ontology_version_id=ONTOLOGY_ID,
        relation_type_id=RELATION_TYPE_ID,
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
        status="active",
        review_status="not_required",
        source_type="manual",
    )


def _evidence(*, library_id: uuid.UUID = LIB_ID, status: str = "active", text_quote: str | None = "Alice evidence"):
    from app.models.evidence_unit import EvidenceUnit

    return EvidenceUnit(
        id=EVIDENCE_ID,
        library_id=library_id,
        document_id=DOC_ID,
        document_revision_id=REVISION_ID,
        evidence_kind="text",
        text_quote=text_quote,
        status=status,
    )


def test_entity_mention_binding_copies_existing_evidence_text_snapshot_fields():
    from app.services import graph_evidence

    evidence = _evidence(text_quote="Alice founded the project.")
    entity = _entity()
    db = FakeDB(evidence, entity)

    row = asyncio.run(
        graph_evidence.create_entity_mention(
            db,
            _lib(),
            entity_id=ENTITY_ID,
            evidence_id=EVIDENCE_ID,
            mention_text="Alice",
            normalized_text="alice",
            chunk_id=CHUNK_ID,
            source_span={"start": 0, "end": 5},
            confidence=0.91,
            source_type="extracted",
        )
    )

    assert row.library_id == LIB_ID
    assert row.entity_id == ENTITY_ID
    assert row.evidence_id == EVIDENCE_ID
    assert row.document_id == DOC_ID
    assert row.document_revision_id == REVISION_ID
    assert row.chunk_id == CHUNK_ID
    assert row.quote_text == "Alice founded the project."
    assert row.evidence_text_snapshot == "Alice founded the project."
    assert row.source_span == {"start": 0, "end": 5}
    assert row.status == "active"
    assert db.added == [row]
    assert db.flush_count == 1
    assert evidence.text_quote == "Alice founded the project."


def test_entity_mention_binding_rejects_nonexistent_cross_library_and_inactive_evidence():
    from app.services import graph_evidence

    entity = _entity()

    with pytest.raises(LookupError, match="evidence not found"):
        asyncio.run(
            graph_evidence.create_entity_mention(
                FakeDB(entity),
                _lib(),
                entity_id=ENTITY_ID,
                evidence_id=EVIDENCE_ID,
                mention_text="Alice",
            )
        )

    with pytest.raises(LookupError, match="evidence not found"):
        asyncio.run(
            graph_evidence.create_entity_mention(
                FakeDB(entity, _evidence(library_id=OTHER_LIB_ID)),
                _lib(),
                entity_id=ENTITY_ID,
                evidence_id=EVIDENCE_ID,
                mention_text="Alice",
            )
        )

    with pytest.raises(LookupError, match="evidence not found"):
        asyncio.run(
            graph_evidence.create_entity_mention(
                FakeDB(entity, _evidence(status="stale")),
                _lib(),
                entity_id=ENTITY_ID,
                evidence_id=EVIDENCE_ID,
                mention_text="Alice",
            )
        )


def test_entity_mention_binding_rejects_cross_library_entity():
    from app.services import graph_evidence

    with pytest.raises(LookupError, match="entity not found"):
        asyncio.run(
            graph_evidence.create_entity_mention(
                FakeDB(_entity(library_id=OTHER_LIB_ID), _evidence()),
                _lib(),
                entity_id=ENTITY_ID,
                evidence_id=EVIDENCE_ID,
                mention_text="Alice",
            )
        )


def test_relation_evidence_binding_copies_existing_evidence_text_snapshot_fields():
    from app.services import graph_evidence

    evidence = _evidence(text_quote="Alice belongs to Engineering.")
    relation = _relation()
    db = FakeDB(evidence, relation)

    row = asyncio.run(
        graph_evidence.create_relation_evidence(
            db,
            _lib(),
            relation_id=RELATION_ID,
            evidence_id=EVIDENCE_ID,
            support_type="supports",
            chunk_id=CHUNK_ID,
            source_span={"predicate": [6, 16]},
            confidence=0.88,
        )
    )

    assert row.library_id == LIB_ID
    assert row.relation_id == RELATION_ID
    assert row.evidence_id == EVIDENCE_ID
    assert row.document_id == DOC_ID
    assert row.document_revision_id == REVISION_ID
    assert row.chunk_id == CHUNK_ID
    assert row.quote_text == "Alice belongs to Engineering."
    assert row.evidence_text_snapshot == "Alice belongs to Engineering."
    assert row.support_type == "supports"
    assert row.source_span == {"predicate": [6, 16]}
    assert row.status == "active"
    assert db.added == [row]
    assert db.flush_count == 1


def test_relation_evidence_binding_rejects_nonexistent_cross_library_and_inactive_evidence():
    from app.services import graph_evidence

    relation = _relation()

    with pytest.raises(LookupError, match="evidence not found"):
        asyncio.run(
            graph_evidence.create_relation_evidence(
                FakeDB(relation),
                _lib(),
                relation_id=RELATION_ID,
                evidence_id=EVIDENCE_ID,
            )
        )

    with pytest.raises(LookupError, match="evidence not found"):
        asyncio.run(
            graph_evidence.create_relation_evidence(
                FakeDB(relation, _evidence(library_id=OTHER_LIB_ID)),
                _lib(),
                relation_id=RELATION_ID,
                evidence_id=EVIDENCE_ID,
            )
        )

    with pytest.raises(LookupError, match="evidence not found"):
        asyncio.run(
            graph_evidence.create_relation_evidence(
                FakeDB(relation, _evidence(status="deleted")),
                _lib(),
                relation_id=RELATION_ID,
                evidence_id=EVIDENCE_ID,
            )
        )


def test_relation_evidence_binding_rejects_cross_library_relation():
    from app.services import graph_evidence

    with pytest.raises(LookupError, match="relation not found"):
        asyncio.run(
            graph_evidence.create_relation_evidence(
                FakeDB(_relation(library_id=OTHER_LIB_ID), _evidence()),
                _lib(),
                relation_id=RELATION_ID,
                evidence_id=EVIDENCE_ID,
            )
        )
