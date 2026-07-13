from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.models.chunk import Chunk
from app.models.chunk_links import ChunkBlock, ChunkEvidence
from app.models.document import Document
from app.models.document_block import DocumentBlock
from app.models.document_revision import DocumentRevision
from app.models.evidence_unit import EVIDENCE_STATUS_ACTIVE, EvidenceUnit
from app.models.extraction_context_snapshot import ExtractionContextSnapshot


class ContextBuildError(ValueError):
    pass


@dataclass(frozen=True)
class _ChunkInfo:
    chunk: Any
    effective_title_path: list[Any]
    title_path_source: str
    title_block: Any | None
    evidence_ids: tuple[Any, ...]
    primary_evidence_id: Any | None


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _same_scope(value: Any, *, library_id: Any, document_id: Any, revision_id: Any) -> bool:
    return (
        value.library_id == library_id
        and value.document_id == document_id
        and value.document_revision_id == revision_id
    )


def _nonempty_path(value: Any) -> list[Any] | None:
    if isinstance(value, list) and value:
        return list(value)
    return None


def _title_info(
    chunk: Any,
    *,
    blocks_by_id: dict[Any, Any],
    primary_block_by_chunk: dict[Any, Any],
) -> tuple[list[Any], str, Any | None]:
    direct_block = blocks_by_id.get(chunk.block_id) if chunk.block_id else None
    linked_block = primary_block_by_chunk.get(chunk.id)

    chunk_path = _nonempty_path(chunk.title_path)
    if chunk_path is not None:
        return chunk_path, "chunk", direct_block or linked_block

    direct_path = _nonempty_path(getattr(direct_block, "title_path", None))
    if direct_path is not None:
        return direct_path, "direct_block", direct_block

    linked_path = _nonempty_path(getattr(linked_block, "title_path", None))
    if linked_path is not None:
        return linked_path, "chunk_block", linked_block
    return [], "none", direct_block or linked_block


def _evidence_info(
    chunk: Any,
    *,
    evidence_by_id: dict[Any, Any],
    links_by_chunk: dict[Any, list[Any]],
) -> tuple[tuple[Any, ...], Any | None]:
    active_ids: list[Any] = []
    direct = evidence_by_id.get(chunk.evidence_id) if chunk.evidence_id else None
    if direct is not None:
        active_ids.append(direct.id)

    for link in links_by_chunk.get(chunk.id, []):
        evidence = evidence_by_id.get(link.evidence_id)
        if evidence is not None and evidence.id not in active_ids:
            active_ids.append(evidence.id)

    primary = direct.id if direct is not None else (active_ids[0] if active_ids else None)
    return tuple(active_ids), primary


def _context_chunk(info: _ChunkInfo, context_ref: str) -> dict[str, Any]:
    chunk = info.chunk
    block = info.title_block
    return {
        "context_ref": context_ref,
        "role": "current" if context_ref == "c0" else "neighbor",
        "text": chunk.text,
        "title_path": info.effective_title_path,
        "chunk_kind": chunk.chunk_kind,
        "block_kind": getattr(block, "block_kind", None),
        "page_start": chunk.page_start,
        "page_end": chunk.page_end,
        "source_start": chunk.source_start,
        "source_end": chunk.source_end,
        "position": chunk.position,
    }


def _render_context(
    *,
    center: _ChunkInfo,
    previous: list[_ChunkInfo],
    following: list[_ChunkInfo],
    document_title: str | None,
    document_metadata: dict[str, Any] | None,
    ontology_snapshot: dict[str, Any],
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    referenced = [("c0", center)]
    referenced.extend((f"p{index}", item) for index, item in enumerate(previous, 1))
    referenced.extend((f"n{index}", item) for index, item in enumerate(following, 1))

    context_json = {
        "document": {
            "title": document_title,
            "metadata": document_metadata,
        },
        "effective_title_path": center.effective_title_path,
        "ontology": ontology_snapshot,
        "chunks": [_context_chunk(item, ref) for ref, item in referenced],
    }
    context_mapping = {
        ref: {
            "chunk_id": str(item.chunk.id),
            "primary_evidence_id": str(item.primary_evidence_id),
            "evidence_ids": [str(value) for value in item.evidence_ids],
            "role": "current" if ref == "c0" else "neighbor",
        }
        for ref, item in referenced
    }
    return context_json, _canonical_json(context_json), context_mapping


async def build_context_snapshot(
    db,
    *,
    job,
    unit,
    previous_chunks: int,
    next_chunks: int,
    max_context_chars: int,
) -> ExtractionContextSnapshot:
    if previous_chunks < 0 or next_chunks < 0 or max_context_chars <= 0:
        raise ContextBuildError("context limits must be non-negative and budget must be positive")

    existing_result = await db.execute(
        select(ExtractionContextSnapshot).where(
            ExtractionContextSnapshot.extraction_unit_id == unit.id
        )
    )
    existing = existing_result.scalars().first()
    if existing is not None:
        if existing.purged_at is not None:
            raise ContextBuildError("context snapshot was purged and cannot be rebuilt")
        return existing

    if (
        unit.job_id != job.id
        or unit.library_id != job.library_id
        or unit.document_revision_id != job.document_revision_id
    ):
        raise ContextBuildError("Job and Unit context scope does not match")

    revision = await db.get(DocumentRevision, job.document_revision_id)
    document = await db.get(Document, job.document_id)
    center_chunk = await db.get(Chunk, unit.center_chunk_id)
    if revision is None or document is None or center_chunk is None:
        raise ContextBuildError("context source row is missing")
    if (
        revision.library_id != job.library_id
        or revision.document_id != job.document_id
        or document.library_id != job.library_id
        or document.id != job.document_id
        or getattr(document, "deleted_at", None) is not None
        or not _same_scope(
            center_chunk,
            library_id=job.library_id,
            document_id=job.document_id,
            revision_id=job.document_revision_id,
        )
    ):
        raise ContextBuildError("context source scope does not match the Job")

    chunk_result = await db.execute(
        select(Chunk)
        .where(
            Chunk.library_id == job.library_id,
            Chunk.document_id == job.document_id,
            Chunk.document_revision_id == job.document_revision_id,
        )
        .order_by(Chunk.seq.asc(), Chunk.id.asc())
    )
    chunks = [
        value
        for value in chunk_result.scalars().all()
        if _same_scope(
            value,
            library_id=job.library_id,
            document_id=job.document_id,
            revision_id=job.document_revision_id,
        )
    ]
    chunks_by_id = {value.id: value for value in chunks}
    if center_chunk.id not in chunks_by_id:
        raise ContextBuildError("center Chunk is not in the Job revision")

    chunk_ids = list(chunks_by_id)
    chunk_block_result = await db.execute(
        select(ChunkBlock)
        .where(
            ChunkBlock.chunk_id.in_(chunk_ids),
            ChunkBlock.document_revision_id == job.document_revision_id,
        )
        .order_by(ChunkBlock.chunk_id.asc(), ChunkBlock.seq.asc(), ChunkBlock.document_block_id.asc())
    )
    chunk_blocks = [
        value
        for value in chunk_block_result.scalars().all()
        if value.chunk_id in chunks_by_id
        and value.document_revision_id == job.document_revision_id
    ]

    block_ids = {value.document_block_id for value in chunk_blocks}
    block_ids.update(value.block_id for value in chunks if value.block_id is not None)
    block_result = await db.execute(
        select(DocumentBlock).where(
            DocumentBlock.id.in_(block_ids),
            DocumentBlock.library_id == job.library_id,
            DocumentBlock.document_id == job.document_id,
            DocumentBlock.document_revision_id == job.document_revision_id,
        )
    )
    blocks_by_id = {
        value.id: value
        for value in block_result.scalars().all()
        if _same_scope(
            value,
            library_id=job.library_id,
            document_id=job.document_id,
            revision_id=job.document_revision_id,
        )
    }
    primary_block_by_chunk: dict[Any, Any] = {}
    for link in sorted(
        chunk_blocks,
        key=lambda value: (str(value.chunk_id), value.seq, str(value.document_block_id)),
    ):
        block = blocks_by_id.get(link.document_block_id)
        if block is not None:
            primary_block_by_chunk.setdefault(link.chunk_id, block)

    link_result = await db.execute(
        select(ChunkEvidence)
        .where(
            ChunkEvidence.chunk_id.in_(chunk_ids),
            ChunkEvidence.document_revision_id == job.document_revision_id,
        )
        .order_by(ChunkEvidence.chunk_id.asc(), ChunkEvidence.seq.asc(), ChunkEvidence.evidence_id.asc())
    )
    links = [
        value
        for value in link_result.scalars().all()
        if value.chunk_id in chunks_by_id
        and value.document_revision_id == job.document_revision_id
    ]
    links_by_chunk: dict[Any, list[Any]] = {}
    for link in links:
        links_by_chunk.setdefault(link.chunk_id, []).append(link)

    evidence_ids = {value.evidence_id for value in links}
    evidence_ids.update(value.evidence_id for value in chunks if value.evidence_id is not None)
    evidence_result = await db.execute(
        select(EvidenceUnit).where(
            EvidenceUnit.id.in_(evidence_ids),
            EvidenceUnit.library_id == job.library_id,
            EvidenceUnit.document_id == job.document_id,
            EvidenceUnit.document_revision_id == job.document_revision_id,
            EvidenceUnit.status == EVIDENCE_STATUS_ACTIVE,
        )
    )
    evidence_by_id = {
        value.id: value
        for value in evidence_result.scalars().all()
        if value.status == EVIDENCE_STATUS_ACTIVE
        and _same_scope(
            value,
            library_id=job.library_id,
            document_id=job.document_id,
            revision_id=job.document_revision_id,
        )
    }

    info_by_chunk: dict[Any, _ChunkInfo] = {}
    for chunk in chunks:
        path, source, title_block = _title_info(
            chunk,
            blocks_by_id=blocks_by_id,
            primary_block_by_chunk=primary_block_by_chunk,
        )
        active_ids, primary_id = _evidence_info(
            chunk,
            evidence_by_id=evidence_by_id,
            links_by_chunk=links_by_chunk,
        )
        info_by_chunk[chunk.id] = _ChunkInfo(
            chunk=chunk,
            effective_title_path=path,
            title_path_source=source,
            title_block=title_block,
            evidence_ids=active_ids,
            primary_evidence_id=primary_id,
        )

    center = info_by_chunk[center_chunk.id]
    if center.primary_evidence_id is None:
        raise ContextBuildError("center Chunk has no active Evidence")
    if center.primary_evidence_id != unit.center_evidence_id:
        raise ContextBuildError("Unit center Evidence does not match deterministic primary Evidence")

    eligible = [
        value
        for value in info_by_chunk.values()
        if value.chunk.id != center.chunk.id
        and value.primary_evidence_id is not None
        and value.effective_title_path == center.effective_title_path
    ]
    previous = sorted(
        (value for value in eligible if value.chunk.seq < center.chunk.seq),
        key=lambda value: (center.chunk.seq - value.chunk.seq, str(value.chunk.id)),
    )[:previous_chunks]
    following = sorted(
        (value for value in eligible if value.chunk.seq > center.chunk.seq),
        key=lambda value: (value.chunk.seq - center.chunk.seq, str(value.chunk.id)),
    )[:next_chunks]

    document_metadata = revision.document_metadata
    document_title = revision.title or document.title

    def render():
        return _render_context(
            center=center,
            previous=previous,
            following=following,
            document_title=document_title,
            document_metadata=document_metadata,
            ontology_snapshot=job.ontology_snapshot,
        )

    context_json, context_text, context_mapping = render()
    while len(context_text) > max_context_chars and (previous or following):
        candidates = []
        if previous:
            item = previous[-1]
            candidates.append((center.chunk.seq - item.chunk.seq, 0, "previous"))
        if following:
            item = following[-1]
            candidates.append((item.chunk.seq - center.chunk.seq, 1, "following"))
        _, _, side = max(candidates)
        (previous if side == "previous" else following).pop()
        context_json, context_text, context_mapping = render()

    if len(context_text) > max_context_chars and document_metadata is not None:
        document_metadata = None
        context_json, context_text, context_mapping = render()
    if len(context_text) > max_context_chars:
        raise ContextBuildError("current Chunk context exceeds max_context_chars")

    referenced = [center, *previous, *following]
    selected_block_ids: list[str] = []
    for info in referenced:
        if info.title_block is not None:
            block_id = str(info.title_block.id)
            if block_id not in selected_block_ids:
                selected_block_ids.append(block_id)

    hash_payload = {
        "context_json": context_json,
        "context_mapping": context_mapping,
        "context_policy_version": job.context_policy_version,
        "ontology_snapshot_hash": job.ontology_snapshot_hash,
    }
    snapshot = ExtractionContextSnapshot(
        job_id=job.id,
        extraction_unit_id=unit.id,
        center_chunk_id=center.chunk.id,
        center_evidence_id=center.primary_evidence_id,
        previous_chunk_ids=[str(value.chunk.id) for value in previous],
        next_chunk_ids=[str(value.chunk.id) for value in following],
        block_ids=selected_block_ids,
        title_path_source=center.title_path_source,
        ontology_snapshot_hash=job.ontology_snapshot_hash,
        context_mapping=context_mapping,
        context_policy_version=job.context_policy_version,
        context_hash=_sha256(hash_payload),
        context_char_count=len(context_text),
        context_json=context_json,
        context_text=context_text,
        document_metadata=document_metadata,
        chunk_title_path=list(center.chunk.title_path) if center.chunk.title_path is not None else None,
        block_title_path=(
            list(center.title_block.title_path)
            if center.title_block is not None and center.title_block.title_path is not None
            else None
        ),
        effective_title_path=center.effective_title_path,
    )
    db.add(snapshot)
    await db.flush()
    return snapshot
