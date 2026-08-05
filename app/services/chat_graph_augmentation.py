from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, settings
from app.models.chunk import Chunk
from app.models.document import Document
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence
from app.schemas.chat import ChatGraphEvidence
from app.schemas.dify import DifyRecord
from app.services import chat_graph_context


log = logging.getLogger(__name__)
MAX_CHAT_GRAPH_CHUNKS = 5
MAX_CHAT_GRAPH_EVIDENCE = 8

_RELATION_TERMS = (
    "关系", "关联", "负责", "属于", "隶属", "参与", "依赖", "影响", "包含",
    "组成", "连接", "上下游", "管理", "审批", "执行", "监督", "适用于", "引用",
    "related", "belong", "depend", "responsible", "manage", "contain", "affect",
)
_RELATION_LABELS = {
    "related_to": "关联", "relates_to": "关联", "belongs_to": "属于",
    "part_of": "属于", "has_part": "包含", "contains": "包含", "requires": "需要",
    "depends_on": "依赖", "references": "引用", "referenced_by": "被引用",
    "manages": "管理", "managed_by": "由其管理", "responsible_for": "负责",
    "approves": "审批", "approved_by": "由其审批", "implements": "执行",
    "implemented_by": "由其执行", "supervises": "监督", "applies_to": "适用于",
    "produces": "产生", "precedes": "先于", "follows": "后于",
}


@dataclass(frozen=True, slots=True)
class ChatGraphAugmentation:
    records: tuple[DifyRecord, ...] = ()
    reason_code: str = "disabled"
    candidate_count: int = 0
    latency_ms: int = 0

    @property
    def context_chars(self) -> int:
        return sum(len(row.title) + len(row.content) + 16 for row in self.records)


def is_relationship_query(query: str) -> bool:
    normalized = " ".join((query or "").lower().split())
    return bool(normalized) and any(term in normalized for term in _RELATION_TERMS)


def _chunk_ids(records: Sequence[object]) -> tuple[uuid.UUID, ...]:
    out: list[uuid.UUID] = []
    for record in records:
        value = (getattr(record, "metadata", None) or {}).get("chunk_id")
        try:
            chunk_id = uuid.UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            continue
        if chunk_id not in out:
            out.append(chunk_id)
        if len(out) >= MAX_CHAT_GRAPH_CHUNKS:
            break
    return tuple(out)


def _relation_label(key: str, label: str) -> str:
    if any("\u4e00" <= char <= "\u9fff" for char in label):
        return label
    return _RELATION_LABELS.get(key.lower().replace("-", "_"), "关联")


async def _hydrate_relation_evidence(
    db: AsyncSession,
    library: Library,
    graph,
    retrieved_chunk_ids: Sequence[uuid.UUID],
) -> list[ChatGraphEvidence]:
    relation_ids = [row.id for row in graph.relations]
    evidence_ids = [locator.evidence_id for row in graph.relations for locator in row.evidence]
    if not relation_ids or not evidence_ids:
        return []
    rows = (
        await db.execute(
            select(
                RelationEvidence.relation_id,
                RelationEvidence.evidence_id,
                RelationEvidence.document_id,
                RelationEvidence.document_revision_id,
                RelationEvidence.chunk_id,
                RelationEvidence.quote_text,
                RelationEvidence.evidence_text_snapshot,
                Chunk.text,
                Chunk.page_start,
                Chunk.page_end,
                Document.title,
                Document.display_name,
            )
            .join(
                Chunk,
                and_(
                    Chunk.id == RelationEvidence.chunk_id,
                    Chunk.library_id == library.id,
                    Chunk.document_revision_id == RelationEvidence.document_revision_id,
                ),
            )
            .join(
                Document,
                and_(
                    Document.id == RelationEvidence.document_id,
                    Document.library_id == library.id,
                    Document.deleted_at.is_(None),
                    Document.status == "ready",
                    Document.current_revision_id == RelationEvidence.document_revision_id,
                ),
            )
            .where(
                RelationEvidence.library_id == library.id,
                RelationEvidence.relation_id.in_(relation_ids),
                RelationEvidence.evidence_id.in_(evidence_ids),
                RelationEvidence.status == "active",
                RelationEvidence.support_type.in_(("supports", "source")),
            )
        )
    ).all()
    row_by_pair = {(row.relation_id, row.evidence_id): row for row in rows}
    node_by_id = {node.id: node for node in graph.nodes}
    chunk_rank = {chunk_id: rank for rank, chunk_id in enumerate(retrieved_chunk_ids)}
    candidates: list[tuple[tuple, ChatGraphEvidence]] = []
    for relation in graph.relations:
        source = node_by_id.get(relation.source_entity_id)
        target = node_by_id.get(relation.target_entity_id)
        if source is None or target is None:
            continue
        for locator in relation.evidence:
            row = row_by_pair.get((relation.id, locator.evidence_id))
            if row is None or row.chunk_id is None:
                continue
            content = (row.quote_text or row.evidence_text_snapshot or row.text or "").strip()
            if not content:
                continue
            evidence = ChatGraphEvidence(
                publication_id=graph.publication.id,
                relation_id=relation.id,
                source_entity_id=source.id,
                source_entity_name=source.canonical_name,
                relation_type_key=relation.relation_type.key,
                relation_label=_relation_label(relation.relation_type.key, relation.relation_type.label),
                target_entity_id=target.id,
                target_entity_name=target.canonical_name,
                evidence_id=row.evidence_id,
                document_id=row.document_id,
                document_revision_id=row.document_revision_id,
                chunk_id=row.chunk_id,
                title=row.display_name or row.title or "",
                page_start=locator.page_start or row.page_start,
                page_end=locator.page_end or row.page_end,
                content=content[:4000],
            )
            rank = (
                chunk_rank.get(row.chunk_id, len(chunk_rank)),
                -(relation.confidence or 0.0),
                str(relation.id),
                str(row.evidence_id),
            )
            candidates.append((rank, evidence))

    selected: list[ChatGraphEvidence] = []
    seen_relations: set[uuid.UUID] = set()
    for _rank, evidence in sorted(candidates, key=lambda item: item[0]):
        if evidence.relation_id in seen_relations:
            continue
        seen_relations.add(evidence.relation_id)
        selected.append(evidence)
        if len(selected) >= MAX_CHAT_GRAPH_EVIDENCE:
            break
    return selected


def _as_record(evidence: ChatGraphEvidence) -> DifyRecord:
    relation = (
        f"{evidence.source_entity_name} - {evidence.relation_label} - "
        f"{evidence.target_entity_name}"
    )
    return DifyRecord(
        title=f"知识图谱关系：{relation}",
        content=(
            "知识图谱关联证据：仅在与直接检索证据一致时使用；如有冲突，以直接原文为准。\n"
            f"关系：{relation}\n原文证据：{evidence.content}"
        ),
        score=1.0,
        metadata={"chat_graph_evidence": evidence.model_dump(mode="json")},
    )


async def prepare_chat_graph_augmentation(
    db: AsyncSession,
    library: Library,
    query: str,
    records: Sequence[object],
    *,
    config: Settings = settings,
) -> ChatGraphAugmentation:
    started = time.monotonic()
    mode = getattr(library, "graph_assisted_chat_mode", "off") or "off"
    reason = "disabled"
    candidates: list[ChatGraphEvidence] = []
    try:
        if (
            not config.graph_retrieval_enabled
            or not getattr(library, "graph_extraction_enabled", False)
            or mode == "off"
        ):
            return ChatGraphAugmentation(reason_code=reason)
        if not is_relationship_query(query):
            return ChatGraphAugmentation(reason_code="not_relationship_query")
        chunk_ids = _chunk_ids(records)
        if not chunk_ids:
            return ChatGraphAugmentation(reason_code="no_chunk_ids")
        async with asyncio.timeout(config.graph_retrieval_timeout_seconds):
            result = await chat_graph_context.query_chat_graph_for_chunks(
                db, library, chunk_ids, config=config,
            )
            if result.graph is None:
                reason = "no_seeds"
            elif not result.graph.relations:
                reason = "no_relations"
            else:
                candidates = await _hydrate_relation_evidence(db, library, result.graph, chunk_ids)
                reason = "augmented" if candidates else "invalid_evidence"
    except TimeoutError:
        reason = "timeout"
    except chat_graph_context.ChatGraphContextServiceError as exc:
        reason = exc.code
    except Exception:
        reason = "internal_error"
        log.exception("chat graph augmentation failed: library_id=%s", library.id)

    latency_ms = int((time.monotonic() - started) * 1000)
    log.info(
        "chat_graph_augmentation: library_id=%s mode=%s reason=%s candidates=%d latency_ms=%d",
        library.id, mode, reason, len(candidates), latency_ms,
    )
    if mode != "enabled" or not config.chat_graph_answer_enabled or not candidates:
        return ChatGraphAugmentation(
            reason_code=reason, candidate_count=len(candidates), latency_ms=latency_ms,
        )
    return ChatGraphAugmentation(
        records=tuple(_as_record(row) for row in candidates),
        reason_code=reason,
        candidate_count=len(candidates),
        latency_ms=latency_ms,
    )
