from __future__ import annotations

import asyncio
import operator
import uuid
from types import SimpleNamespace

from app.models.canonical_entity import CanonicalEntity
from app.models.entity import Entity
from app.models.entity_mention import EntityMention
from app.models.evidence_unit import EvidenceUnit
from app.models.knowledge_relation import KnowledgeRelation
from app.models.relation_evidence import RelationEvidence


LIB_ID = uuid.uuid4()
OTHER_LIB_ID = uuid.uuid4()
ONTOLOGY_ID = uuid.uuid4()
ENTITY_TYPE_ID = uuid.uuid4()
RELATION_TYPE_ID = uuid.uuid4()
ENTITY_ID = uuid.uuid4()
RELATION_ID = uuid.uuid4()
DOC_ID = uuid.uuid4()
OTHER_DOC_ID = uuid.uuid4()
REVISION_ID = uuid.uuid4()
OTHER_REVISION_ID = uuid.uuid4()
EVIDENCE_ID = uuid.uuid4()
OTHER_EVIDENCE_ID = uuid.uuid4()


class FakeScalarResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return FakeScalarResult(self._rows)


class FakeDB:
    def __init__(self, *objects):
        self.objects = {}
        self.flush_count = 0
        for obj in objects:
            self.add_existing(obj)

    def add_existing(self, obj) -> None:
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        self.objects[(type(obj), obj.id)] = obj

    async def get(self, model, object_id):
        return self.objects.get((model, object_id))

    async def execute(self, stmt):
        model = stmt.column_descriptions[0]["entity"]
        rows = [obj for (obj_model, _), obj in self.objects.items() if obj_model is model]
        for criterion in stmt._where_criteria:
            if criterion.operator is not operator.eq:
                raise AssertionError(f"unsupported fake criterion: {criterion}")
            column_name = criterion.left.name
            expected = criterion.right.value
            rows = [row for row in rows if getattr(row, column_name) == expected]
        return FakeResult(rows)

    async def flush(self) -> None:
        self.flush_count += 1


def _lib(library_id: uuid.UUID = LIB_ID):
    return SimpleNamespace(id=library_id, qdrant_collection="collection")


def _canonical() -> CanonicalEntity:
    return CanonicalEntity(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        canonical_name="Alice",
        normalized_name="alice",
        status="active",
    )


def _entity(
    *,
    status: str = "active",
    entity_id: uuid.UUID = ENTITY_ID,
    canonical_entity_id: uuid.UUID | None = None,
) -> Entity:
    return Entity(
        id=entity_id,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        entity_type_id=ENTITY_TYPE_ID,
        canonical_entity_id=canonical_entity_id,
        canonical_name="Alice",
        normalized_name="alice",
        status=status,
        source_type="manual",
    )


def _relation(*, relation_id: uuid.UUID = RELATION_ID, status: str = "active") -> KnowledgeRelation:
    return KnowledgeRelation(
        id=relation_id,
        library_id=LIB_ID,
        ontology_version_id=ONTOLOGY_ID,
        relation_type_id=RELATION_TYPE_ID,
        source_entity_id=uuid.uuid4(),
        target_entity_id=uuid.uuid4(),
        status=status,
        review_status="not_required",
        source_type="manual",
    )


def _evidence(
    *,
    evidence_id: uuid.UUID = EVIDENCE_ID,
    library_id: uuid.UUID = LIB_ID,
    document_id: uuid.UUID = DOC_ID,
    revision_id: uuid.UUID = REVISION_ID,
    status: str = "active",
) -> EvidenceUnit:
    return EvidenceUnit(
        id=evidence_id,
        library_id=library_id,
        document_id=document_id,
        document_revision_id=revision_id,
        evidence_kind="chunk",
        text_quote="Alice belongs to Engineering.",
        status=status,
    )


def _mention(
    *,
    evidence_id: uuid.UUID = EVIDENCE_ID,
    document_id: uuid.UUID = DOC_ID,
    revision_id: uuid.UUID = REVISION_ID,
    status: str = "active",
) -> EntityMention:
    return EntityMention(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        entity_id=ENTITY_ID,
        evidence_id=evidence_id,
        document_id=document_id,
        document_revision_id=revision_id,
        mention_text="Alice",
        source_type="manual",
        status=status,
    )


def _relation_evidence(
    *,
    relation_id: uuid.UUID = RELATION_ID,
    evidence_id: uuid.UUID = EVIDENCE_ID,
    document_id: uuid.UUID = DOC_ID,
    revision_id: uuid.UUID = REVISION_ID,
    status: str = "active",
) -> RelationEvidence:
    return RelationEvidence(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        relation_id=relation_id,
        evidence_id=evidence_id,
        document_id=document_id,
        document_revision_id=revision_id,
        support_type="supports",
        status=status,
    )


def test_document_revision_replacement_marks_old_revision_graph_evidence_stale_and_relation_if_no_active_evidence():
    from app.services import graph_evidence

    canonical = _canonical()
    entity = _entity(canonical_entity_id=canonical.id)
    mention = _mention()
    relation = _relation()
    relation_evidence = _relation_evidence()
    db = FakeDB(canonical, entity, mention, relation, relation_evidence)

    result = asyncio.run(
        graph_evidence.mark_document_revision_graph_evidence_stale(
            db,
            _lib(),
            document_revision_id=REVISION_ID,
        )
    )

    assert mention.status == "stale"
    assert relation_evidence.status == "stale"
    assert relation.status == "stale"
    assert entity.status == "active"
    assert entity.canonical_entity_id == canonical.id
    assert canonical.status == "active"
    assert result.stale_entity_mentions == 1
    assert result.stale_relation_evidence == 1
    assert result.stale_relations == 1
    assert db.flush_count == 1


def test_relation_remains_active_when_other_active_relation_evidence_survives_revision_replacement():
    from app.services import graph_evidence

    relation = _relation()
    stale_candidate = _relation_evidence(evidence_id=EVIDENCE_ID, revision_id=REVISION_ID)
    survivor = _relation_evidence(evidence_id=OTHER_EVIDENCE_ID, revision_id=OTHER_REVISION_ID)
    db = FakeDB(relation, stale_candidate, survivor)

    result = asyncio.run(
        graph_evidence.mark_document_revision_graph_evidence_stale(
            db,
            _lib(),
            document_revision_id=REVISION_ID,
        )
    )

    assert stale_candidate.status == "stale"
    assert survivor.status == "active"
    assert relation.status == "active"
    assert result.stale_relation_evidence == 1
    assert result.stale_relations == 0


def test_document_delete_marks_graph_evidence_stale_without_deleting_entity():
    from app.services import graph_evidence

    canonical = _canonical()
    entity = _entity(canonical_entity_id=canonical.id)
    mention = _mention(document_id=DOC_ID)
    other_doc_mention = _mention(document_id=OTHER_DOC_ID, revision_id=OTHER_REVISION_ID)
    relation = _relation()
    relation_evidence = _relation_evidence(document_id=DOC_ID)
    db = FakeDB(canonical, entity, mention, other_doc_mention, relation, relation_evidence)

    result = asyncio.run(
        graph_evidence.mark_document_graph_evidence_stale(
            db,
            _lib(),
            document_id=DOC_ID,
        )
    )

    assert mention.status == "stale"
    assert other_doc_mention.status == "active"
    assert relation_evidence.status == "stale"
    assert relation.status == "stale"
    assert entity.status == "active"
    assert entity.canonical_entity_id == canonical.id
    assert canonical.status == "active"
    assert result.stale_entity_mentions == 1
    assert result.stale_relation_evidence == 1
    assert result.stale_relations == 1


def test_document_delete_isolates_stale_projection_evidence_and_keeps_other_projection_active():
    from app.services import graph_evidence

    canonical = _canonical()
    stale_projection = _entity(canonical_entity_id=canonical.id)
    active_projection = _entity(
        entity_id=uuid.uuid4(),
        canonical_entity_id=canonical.id,
    )
    active_projection.ontology_version_id = uuid.uuid4()
    stale_mention = _mention(document_id=DOC_ID)
    active_mention = EntityMention(
        id=uuid.uuid4(),
        library_id=LIB_ID,
        entity_id=active_projection.id,
        evidence_id=OTHER_EVIDENCE_ID,
        document_id=OTHER_DOC_ID,
        document_revision_id=OTHER_REVISION_ID,
        mention_text="Alice",
        source_type="manual",
        status="active",
    )
    db = FakeDB(
        canonical,
        stale_projection,
        active_projection,
        stale_mention,
        active_mention,
    )

    result = asyncio.run(
        graph_evidence.mark_document_graph_evidence_stale(
            db,
            _lib(),
            document_id=DOC_ID,
        )
    )

    assert result.stale_entity_mentions == 1
    assert stale_mention.status == "stale"
    assert active_mention.status == "active"
    assert stale_projection.status == "active"
    assert active_projection.status == "active"
    assert stale_projection.canonical_entity_id == canonical.id
    assert active_projection.canonical_entity_id == canonical.id
    assert canonical.status == "active"


def test_document_delete_handles_legacy_entity_without_canonical_mapping():
    from app.services import graph_evidence

    legacy_entity = _entity()
    mention = _mention(document_id=DOC_ID)
    db = FakeDB(legacy_entity, mention)

    result = asyncio.run(
        graph_evidence.mark_document_graph_evidence_stale(
            db,
            _lib(),
            document_id=DOC_ID,
        )
    )

    assert result.stale_entity_mentions == 1
    assert mention.status == "stale"
    assert legacy_entity.canonical_entity_id is None


def test_evidence_unit_invalidation_marks_bound_graph_rows_stale():
    from app.services import graph_evidence

    evidence = _evidence(status="deleted")
    mention = _mention(evidence_id=EVIDENCE_ID)
    relation = _relation()
    relation_evidence = _relation_evidence(evidence_id=EVIDENCE_ID)
    db = FakeDB(evidence, mention, relation, relation_evidence)

    result = asyncio.run(
        graph_evidence.mark_evidence_unit_graph_evidence_stale(
            db,
            _lib(),
            evidence_id=EVIDENCE_ID,
        )
    )

    assert mention.status == "stale"
    assert relation_evidence.status == "stale"
    assert relation.status == "stale"
    assert result.stale_entity_mentions == 1
    assert result.stale_relation_evidence == 1
    assert result.stale_relations == 1


def test_active_read_helpers_filter_stale_deleted_and_non_active_rows_by_default():
    from app.services import graph_evidence

    active_mention = _mention(status="active")
    stale_mention = _mention(status="stale")
    deleted_mention = _mention(status="deleted")
    active_evidence = _relation_evidence(status="active")
    stale_evidence = _relation_evidence(status="stale")
    deleted_evidence = _relation_evidence(status="deleted")
    active_relation = _relation(status="active")
    stale_relation = _relation(relation_id=uuid.uuid4(), status="stale")
    deleted_relation = _relation(relation_id=uuid.uuid4(), status="deleted")
    pending_relation = _relation(relation_id=uuid.uuid4(), status="pending_review")
    db = FakeDB(
        active_mention,
        stale_mention,
        deleted_mention,
        active_evidence,
        stale_evidence,
        deleted_evidence,
        active_relation,
        stale_relation,
        deleted_relation,
        pending_relation,
    )

    mentions = asyncio.run(graph_evidence.list_active_entity_mentions(db, _lib()))
    evidence_rows = asyncio.run(graph_evidence.list_active_relation_evidence(db, _lib()))
    relations = asyncio.run(graph_evidence.list_active_knowledge_relations(db, _lib()))

    assert mentions == [active_mention]
    assert evidence_rows == [active_evidence]
    assert relations == [active_relation]


def test_cleanup_enqueue_delete_document_marks_graph_stale_before_outbox(monkeypatch):
    from app.services import cleanup
    from app.services import graph_evidence
    from app.services import graph_extraction_purge

    calls: list[str] = []

    async def fake_stale(db, library, *, document_id):
        assert document_id == DOC_ID
        calls.append("graph")

    async def fake_enqueue(db, **kwargs):
        assert kwargs["document_id"] == DOC_ID
        calls.append("outbox")

    async def fake_purge(db, *, library_id, document_id):
        assert library_id == LIB_ID
        assert document_id == DOC_ID
        calls.append("purge")

    monkeypatch.setattr(graph_evidence, "mark_document_graph_evidence_stale", fake_stale)
    monkeypatch.setattr(
        graph_extraction_purge,
        "purge_document_graph_extraction_payloads",
        fake_purge,
    )
    monkeypatch.setattr(cleanup, "_enqueue", fake_enqueue)

    asyncio.run(cleanup.enqueue_delete_document(object(), _lib(), DOC_ID))

    assert calls == ["graph", "purge", "outbox"]


def test_cleanup_enqueue_delete_document_revision_marks_graph_stale_before_outbox(monkeypatch):
    from app.services import cleanup
    from app.services import graph_evidence
    from app.services import graph_extraction_purge

    calls: list[str] = []

    async def fake_stale(db, library, *, document_revision_id):
        assert document_revision_id == REVISION_ID
        calls.append("graph")

    async def fake_enqueue(db, **kwargs):
        assert kwargs["document_id"] == DOC_ID
        assert kwargs["payload"] == {"document_revision_id": str(REVISION_ID)}
        calls.append("outbox")

    async def fake_purge(db, *, library_id, document_revision_id):
        assert library_id == LIB_ID
        assert document_revision_id == REVISION_ID
        calls.append("purge")

    monkeypatch.setattr(graph_evidence, "mark_document_revision_graph_evidence_stale", fake_stale)
    monkeypatch.setattr(
        graph_extraction_purge,
        "purge_revision_graph_extraction_payloads",
        fake_purge,
    )
    monkeypatch.setattr(cleanup, "_enqueue", fake_enqueue)

    asyncio.run(cleanup.enqueue_delete_document_revision(object(), _lib(), DOC_ID, REVISION_ID))

    assert calls == ["graph", "purge", "outbox"]
