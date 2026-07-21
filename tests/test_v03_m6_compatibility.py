from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.schemas.dify import DifyRecord, DifyRetrievalResponse
from app.workers.embedder import _build_payload


def test_v03_m6_does_not_change_dify_retrieval_response_shape():
    response = DifyRetrievalResponse(
        records=[
            DifyRecord(
                content="body",
                score=0.91,
                title="title",
                metadata={
                    "document_id": "doc-1",
                    "chunk_id": "chunk-1",
                    "document_revision": 2,
                    "document_revision_id": "rev-1",
                    "evidence_id": "ev-1",
                },
            )
        ]
    )

    payload = response.model_dump()

    assert set(payload) == {"records"}
    assert set(payload["records"][0]) == {"content", "score", "title", "metadata"}
    assert "entities" not in payload["records"][0]
    assert "relations" not in payload["records"][0]
    assert "graph" not in payload["records"][0]


def test_v03_m6_does_not_change_qdrant_payload_shape():
    library_id = uuid.uuid4()
    document_id = uuid.uuid4()
    chunk_id = uuid.uuid4()
    revision_id = uuid.uuid4()
    evidence_id = uuid.uuid4()

    payload = _build_payload(
        SimpleNamespace(id=library_id),
        SimpleNamespace(
            id=document_id,
            title="Doc",
            external_id="ext-1",
            current_revision=3,
            doc_metadata={"department": "legal"},
            visibility_scope="internal",
            security_level="normal",
        ),
        SimpleNamespace(
            id=chunk_id,
            seq=4,
            text="chunk text",
            document_revision_id=revision_id,
            evidence_id=evidence_id,
            block_id=None,
            chunk_kind="text",
            page_start=None,
            page_end=None,
            title_path=None,
            source_start=None,
            source_end=None,
            position=None,
        ),
        job=SimpleNamespace(
            document_revision=3,
            document_revision_id=revision_id,
            document_revision_no=3,
        ),
    )

    assert payload["library_id"] == str(library_id)
    assert payload["document_id"] == str(document_id)
    assert payload["chunk_id"] == str(chunk_id)
    assert payload["text"] == "chunk text"
    assert payload["title"] == "Doc"
    assert payload["document_revision"] == 3
    assert payload["document_revision_no"] == 3
    assert payload["document_revision_id"] == str(revision_id)
    assert payload["evidence_id"] == str(evidence_id)
    assert "entities" not in payload
    assert "relations" not in payload
    assert "graph" not in payload
    assert "ontology_version_id" not in payload
    assert "relation_type_id" not in payload
