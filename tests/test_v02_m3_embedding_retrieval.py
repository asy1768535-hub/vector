from __future__ import annotations

import asyncio
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models.chunk import Chunk
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EvidenceUnit
from app.models.library import Library
from app.schemas.evidence_locator import EvidenceLocatorV1, sha256_text
from app.services import evidence_read
from app.services import retrieval
from app.services.evidence_locator_projection import (
    MAX_LOCATOR_PROJECTION_BYTES,
    project_locator,
)
from app.services.federated_retrieval import _source_projection
from app.schemas.dify import DifyRetrievalRequest, RetrievalSetting
from app.workers.embedder import _build_payload


LIB_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
DOC_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a2")
REV_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a3")
BLOCK_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a4")
CHUNK_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a5")
EVIDENCE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a6")
FILE_ID = uuid.UUID("00000000-0000-0000-0000-0000000000a7")


def _locator(
    *,
    unit_id=CHUNK_ID,
    parent_unit_id=BLOCK_ID,
    quote="quote",
    parser_name="fixture",
    raw_hash="a" * 64,
    hash_text=None,
) -> EvidenceLocatorV1:
    hash_text = quote if hash_text is None else hash_text
    return EvidenceLocatorV1(
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        revision_no=2,
        document_revision_file_id=FILE_ID,
        raw_file_sha256=raw_hash,
        normalized_content_hash="b" * 64,
        unit_id=unit_id,
        parent_unit_id=parent_unit_id,
        unit_kind="chunk",
        ordinal=3,
        parser={"name": parser_name, "version": "v1", "config_hash": "c" * 64},
        source={
            "kind": "pdf",
            "file_name": "fixture.pdf",
            "page": {"start": 2, "end": 2},
            "heading_path": ["Section"],
            "text": {
                "start": 7,
                "end": 12,
                "ranges": [{"start": 7, "end": 12, "sha256": sha256_text(quote)}],
            },
        },
        unit_text_sha256=sha256_text(hash_text),
        quote_sha256=sha256_text(hash_text),
    )


def _library() -> Library:
    return Library(
        id=LIB_ID,
        slug="m3",
        name="M3",
        qdrant_collection="m3",
        embedding_model="fixture",
        embedding_dim=3,
        chunk_size=1000,
        chunk_overlap=0,
        index_state="ready",
    )


def _document() -> Document:
    return Document(
        id=DOC_ID,
        library_id=LIB_ID,
        title="Fixture",
        external_id="fixture",
        doc_metadata={"department": "test"},
        content_hash="d" * 64,
        current_revision=2,
        current_revision_id=REV_ID,
        latest_revision_id=REV_ID,
        status="ready",
    )


def _revision() -> DocumentRevision:
    return DocumentRevision(
        id=REV_ID,
        document_id=DOC_ID,
        library_id=LIB_ID,
        revision_no=2,
        title="Fixture",
        content_hash="b" * 64,
        normalized_text="before quote after",
        parser_name="fixture",
        parser_version="v1",
        chunking_strategy="text",
        chunking_strategy_version="v1",
        status="ready",
    )


def _revision_file() -> SimpleNamespace:
    return SimpleNamespace(
        id=FILE_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        library_id=LIB_ID,
        sha256="a" * 64,
    )


def test_locator_projection_is_deterministic_bounded_and_content_free():
    locator = _locator()
    first = project_locator(locator, chunk_id=CHUNK_ID, block_id=BLOCK_ID, evidence_id=EVIDENCE_ID)
    second = project_locator(locator, chunk_id=CHUNK_ID, block_id=BLOCK_ID, evidence_id=EVIDENCE_ID)

    assert first == second
    assert len(json.dumps(first, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= MAX_LOCATOR_PROJECTION_BYTES
    unsigned = {
        key: value
        for key, value in first.items()
        if key not in {"locator_hash", "projection_hash"}
    }
    assert first["projection_hash"] == sha256_text(
        json.dumps(
            unsigned,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    assert first["locator_hash"] != first["projection_hash"]
    assert first["document_revision_file_id"] == str(FILE_ID)
    assert "raw_file_sha256" not in first
    assert "quote_sha256" not in first
    assert "parser" not in first
    assert "quality" not in first
    assert "quote" not in json.dumps(first, ensure_ascii=False)


def test_new_embedding_payload_adds_projection_without_removing_legacy_fields():
    library = _library()
    document = _document()
    locator = _locator()
    chunk = Chunk(
        id=CHUNK_ID,
        document_id=DOC_ID,
        library_id=LIB_ID,
        document_revision_id=REV_ID,
        block_id=BLOCK_ID,
        evidence_id=EVIDENCE_ID,
        seq=3,
        chunk_kind="text",
        text="quote",
        token_count=5,
        chunk_metadata={"evidence_locator_v1": locator.model_dump(mode="json")},
    )

    payload = _build_payload(
        library,
        document,
        chunk,
        job=SimpleNamespace(document_revision_id=REV_ID, document_revision_no=2),
        revision=_revision(),
    )

    assert payload["text"] == "quote"
    assert payload["department"] == "test"
    projection = payload["evidence_locator_v1_projection"]
    assert projection["chunk_id"] == str(CHUNK_ID)
    assert projection["source"]["kind"] == "pdf"
    assert "evidence_locator_v1" not in projection
    assert "raw_file_sha256" not in projection
    assert len(json.dumps(projection, ensure_ascii=False).encode("utf-8")) <= MAX_LOCATOR_PROJECTION_BYTES


def test_full_locator_hash_changes_without_changing_projection_hash():
    first = project_locator(_locator(), chunk_id=CHUNK_ID, block_id=BLOCK_ID)
    changed_locator = _locator(
        parser_name="other",
        raw_hash="d" * 64,
        hash_text="other quote",
    )
    changed = project_locator(changed_locator, chunk_id=CHUNK_ID, block_id=BLOCK_ID)
    assert first["projection_hash"] == changed["projection_hash"]
    assert first["locator_hash"] != changed["locator_hash"]


def test_malformed_locator_does_not_break_legacy_embedding_payload():
    chunk = Chunk(
        id=CHUNK_ID,
        document_id=DOC_ID,
        library_id=LIB_ID,
        document_revision_id=REV_ID,
        seq=3,
        chunk_kind="text",
        text="quote",
        token_count=5,
        chunk_metadata={"evidence_locator_v1": {"document_id": "malformed"}},
    )
    payload = _build_payload(
        _library(),
        _document(),
        chunk,
        job=SimpleNamespace(document_revision_id=REV_ID, document_revision_no=2),
        revision=_revision(),
    )
    assert "evidence_locator_v1_projection" not in payload
    assert payload["text"] == "quote"


def _read_db(*, include_file: bool):
    evidence = EvidenceUnit(
        id=EVIDENCE_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        document_block_id=BLOCK_ID,
        evidence_kind="chunk",
        source_start=0,
        source_end=5,
        text_quote="quote",
        status="active",
        evidence_metadata={
            "evidence_locator_v1": _locator(
                unit_id=EVIDENCE_ID,
                quote="quote",
            ).model_dump(mode="json")
        },
    )
    db = AsyncMock()

    async def get_model(model, ident):
        if model is EvidenceUnit and ident == EVIDENCE_ID:
            return evidence
        if model is Document and ident == DOC_ID:
            return _document()
        if model is DocumentRevision and ident == REV_ID:
            return _revision()
        if include_file and ident == FILE_ID:
            return _revision_file()
        return None

    db.get = AsyncMock(side_effect=get_model)
    return db


def test_evidence_read_prefers_verified_locator_and_hides_raw_file_identity():
    detail = asyncio.run(
        evidence_read.get_evidence_detail(
            _read_db(include_file=True), _library(), EVIDENCE_ID,
        )
    )

    assert detail.source_start == 7
    assert detail.source_end == 12
    assert detail.page_start == 2
    assert detail.position["kind"] == "pdf"
    metadata = detail.evidence_metadata or {}
    assert "evidence_locator_v1" not in metadata
    projection = metadata["evidence_locator_v1_projection"]
    assert "raw_file_sha256" not in projection
    assert "storage_path" not in json.dumps(metadata)


def test_verified_locator_legacy_call_output_is_unchanged_when_unit_text_is_omitted():
    db = _read_db(include_file=True)

    async def load():
        evidence = await db.get(EvidenceUnit, EVIDENCE_ID)
        return await evidence_read._verified_locator(
            db,
            evidence.evidence_metadata["evidence_locator_v1"],
            document=_document(),
            revision=_revision(),
            unit_id=EVIDENCE_ID,
            parent_unit_id=BLOCK_ID,
            quote="quote",
        ), await evidence_read._verified_locator(
            db,
            evidence.evidence_metadata["evidence_locator_v1"],
            document=_document(),
            revision=_revision(),
            unit_id=EVIDENCE_ID,
            parent_unit_id=BLOCK_ID,
            quote="quote",
            unit_text="quote",
        )

    legacy, explicit = asyncio.run(load())
    assert legacy is not None and explicit is not None
    assert legacy.model_dump(mode="json") == explicit.model_dump(mode="json")


def test_verified_locator_accepts_quote_substring_of_full_chunk_unit_text():
    quote = "quote"
    full_chunk = "before quote after"
    locator = _locator(unit_id=EVIDENCE_ID, quote=quote, hash_text=full_chunk).model_copy(
        update={"quote_sha256": sha256_text(quote)}
    )
    evidence = EvidenceUnit(
        id=EVIDENCE_ID,
        library_id=LIB_ID,
        document_id=DOC_ID,
        document_revision_id=REV_ID,
        document_block_id=BLOCK_ID,
        evidence_kind="chunk",
        text_quote=quote,
        status="active",
        evidence_metadata={"evidence_locator_v1": locator.model_dump(mode="json")},
    )
    db = AsyncMock()
    db.get = AsyncMock(side_effect=lambda model, ident: _revision_file() if ident == FILE_ID else None)

    async def load():
        return await evidence_read._verified_locator(
            db,
            evidence.evidence_metadata["evidence_locator_v1"],
            document=_document(),
            revision=_revision(),
            unit_id=EVIDENCE_ID,
            parent_unit_id=BLOCK_ID,
            quote=quote,
            unit_text=full_chunk,
        )

    verified = asyncio.run(load())
    assert verified is not None
    assert verified.quote_sha256 == sha256_text(quote)
    assert verified.unit_text_sha256 == sha256_text(full_chunk)


def test_missing_revision_file_falls_back_to_legacy_evidence_fields():
    detail = asyncio.run(
        evidence_read.get_evidence_detail(
            _read_db(include_file=False), _library(), EVIDENCE_ID,
        )
    )

    assert detail.source_start == 0
    assert detail.source_end == 5
    assert detail.page_start is None
    assert detail.evidence_metadata is None


def test_chunk_source_prefers_verified_locator_and_falls_back_when_file_is_missing():
    locator = _locator()
    chunk = Chunk(
        id=CHUNK_ID,
        document_id=DOC_ID,
        library_id=LIB_ID,
        document_revision_id=REV_ID,
        block_id=BLOCK_ID,
        seq=3,
        chunk_kind="text",
        text="quote",
        token_count=5,
        source_start=0,
        source_end=5,
        chunk_metadata={"evidence_locator_v1": locator.model_dump(mode="json")},
    )

    async def get_model(model, ident):
        if model is Chunk:
            return chunk
        if model is Document:
            return _document()
        if model is DocumentRevision:
            return _revision()
        if ident == FILE_ID:
            return _revision_file()
        return None

    db = AsyncMock()
    db.get = AsyncMock(side_effect=get_model)
    source = asyncio.run(evidence_read.get_chunk_source(db, _library(), CHUNK_ID))
    assert source.source_start == 7
    assert source.source_end == 12
    assert source.page_start == 2

    async def get_missing_file(model, ident):
        result = await get_model(model, ident)
        return None if ident == FILE_ID else result

    db.get = AsyncMock(side_effect=get_missing_file)
    fallback = asyncio.run(evidence_read.get_chunk_source(db, _library(), CHUNK_ID))
    assert fallback.source_start == 0
    assert fallback.source_end == 5
    assert fallback.page_start is None


def test_federated_projection_uses_validated_locator_projection_without_leaking_file_data():
    projection = project_locator(
        _locator(), chunk_id=CHUNK_ID, block_id=BLOCK_ID, evidence_id=EVIDENCE_ID,
    )
    source = _source_projection({
        "document_id": str(DOC_ID),
        "document_revision_id": str(REV_ID),
        "document_revision": 2,
        "chunk_id": str(CHUNK_ID),
        "page": 99,
        "title_path": ["forged"],
        "evidence_locator_v1_projection": projection,
        "raw_file_sha256": "secret",
    })
    assert source.document_id == str(DOC_ID)
    assert source.document_revision_id == str(REV_ID)
    assert source.document_revision == 2
    assert source.chunk_id == str(CHUNK_ID)
    assert source.page == 2
    assert source.title_path == ("Section",)


def test_federated_projection_identity_conflict_keeps_legacy_metadata():
    projection = project_locator(
        _locator(), chunk_id=CHUNK_ID, block_id=BLOCK_ID, evidence_id=EVIDENCE_ID,
    )
    source = _source_projection({
        "document_id": "forged",
        "document_revision_id": "forged",
        "document_revision": 99,
        "chunk_id": "forged",
        "page": 99,
        "title_path": ["legacy"],
        "evidence_locator_v1_projection": projection,
    })
    assert source.document_id == "forged"
    assert source.document_revision_id == "forged"
    assert source.document_revision == 99
    assert source.chunk_id == "forged"
    assert source.page == 99
    assert source.title_path == ("legacy",)


def test_retrieval_omits_malformed_projection_and_keeps_valid_projection():
    valid = project_locator(
        _locator(), chunk_id=CHUNK_ID, block_id=BLOCK_ID, evidence_id=EVIDENCE_ID,
    )
    hits = [
        {
            "id": str(CHUNK_ID),
            "score": 0.9,
            "payload": {
                "document_id": str(DOC_ID),
                "document_revision_id": str(REV_ID),
                "document_revision": 2,
                "chunk_id": str(CHUNK_ID),
                "text": "quote",
                "title": "Fixture",
                "evidence_locator_v1_projection": valid,
            },
        },
        {
            "id": "legacy",
            "score": 0.8,
            "payload": {
                "document_id": str(DOC_ID),
                "chunk_id": "legacy",
                "text": "legacy",
                "title": "Legacy",
                "evidence_locator_v1_projection": {"locator_version": "v1"},
            },
        },
    ]

    async def search(*_args, **_kwargs):
        return hits

    async def embed_one(*_args, **_kwargs):
        return [0.1, 0.2, 0.3]

    with patch.object(retrieval.qdrant, "search", new=search), \
         patch.object(retrieval.embedding, "embed_one", new=embed_one), \
         patch.object(retrieval.rerank_svc, "rank_candidates", new=AsyncMock(return_value=([0, 1], {}))):
        response = asyncio.run(
            retrieval.run_retrieval(
                collection="m3",
                embedding_model="fixture",
                embedding_base_url=None,
                request=DifyRetrievalRequest(
                    knowledge_id="m3",
                    query="quote",
                    retrieval_setting=RetrievalSetting(top_k=2, score_threshold=0),
                ),
            )
        )

    assert response.records[0].metadata["evidence_locator_v1_projection"] == valid
    assert "evidence_locator_v1_projection" not in response.records[1].metadata


def test_projection_with_self_consistent_unknown_field_is_rejected():
    projection = project_locator(_locator(), chunk_id=CHUNK_ID)
    projection["unknown_secret"] = "must not leave this process"
    projection["projection_hash"] = sha256_text(
        json.dumps(
            {
                key: value
                for key, value in projection.items()
                if key not in {"locator_hash", "projection_hash"}
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )

    from app.services.evidence_locator_projection import validate_projection

    assert validate_projection(projection) is None


def test_projection_identity_conflict_is_rejected_without_db_lookup():
    from app.services.evidence_locator_projection import validate_projection

    projection = project_locator(_locator(), chunk_id=CHUNK_ID)
    assert validate_projection(
        projection,
        expected_identity={
            "document_id": str(DOC_ID),
            "document_revision_id": str(REV_ID),
            "document_revision": 99,
            "chunk_id": str(CHUNK_ID),
        },
    ) is None


def test_projection_strict_schema_rejects_bool_unknown_and_noncanonical_ids():
    from app.services.evidence_locator_projection import validate_projection

    projection = project_locator(_locator(), chunk_id=CHUNK_ID)
    for key, value in (
        ("revision_no", True),
        ("document_id", str(DOC_ID).upper()),
        ("unknown", "value"),
    ):
        candidate = dict(projection)
        candidate[key] = value
        unsigned = {
            item_key: item_value
            for item_key, item_value in candidate.items()
            if item_key not in {"locator_hash", "projection_hash"}
        }
        candidate["projection_hash"] = sha256_text(
            json.dumps(unsigned, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )
        assert validate_projection(candidate) is None


def test_invalid_revision_and_unit_hashes_fall_back_to_scalar_evidence():
    db = _read_db(include_file=True)
    evidence = asyncio.run(db.get(EvidenceUnit, EVIDENCE_ID))
    raw = evidence.evidence_metadata["evidence_locator_v1"]
    for field, value in (
        ("normalized_content_hash", "e" * 64),
        ("raw_file_sha256", "f" * 64),
        ("unit_text_sha256", sha256_text("wrong")),
    ):
        candidate = dict(raw)
        candidate[field] = value
        evidence.evidence_metadata = {"evidence_locator_v1": candidate}
        detail = asyncio.run(
            evidence_read.get_evidence_detail(db, _library(), EVIDENCE_ID)
        )
        assert detail.source_start == 0
        assert detail.source_end == 5
        assert detail.page_start is None


def test_missing_quote_cannot_verify_locator_and_falls_back_to_scalar_evidence():
    db = _read_db(include_file=True)
    evidence = asyncio.run(db.get(EvidenceUnit, EVIDENCE_ID))
    evidence.text_quote = None
    detail = asyncio.run(
        evidence_read.get_evidence_detail(db, _library(), EVIDENCE_ID)
    )
    assert detail.source_start == 0
    assert detail.source_end == 5
    assert detail.page_start is None
