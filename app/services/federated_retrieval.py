from __future__ import annotations

import logging
import math
import time
import uuid
from dataclasses import replace
from typing import Any, Sequence

from app.config import settings
from app.models.user import User
from app.schemas.dify import DifyRetrievalRequest, RetrievalSetting
from app.services.federated_retrieval_contracts import (
    FEDERATED_CONTENT_EXCERPT_CHARACTERS,
    FEDERATED_TITLE_CHARACTERS,
    FederatedHit,
    FederatedLibraryExecution,
    FederatedLibraryTiming,
    FederatedRetrievalCommand,
    FederatedRetrievalError,
    FederatedRetrievalResult,
    FederatedSourceProjection,
)
from app.services.library_compatibility import assess_library_compatibility
from app.services.library_compatibility_contracts import FEDERATED_RRF_K
from app.services.evidence_locator_projection import validate_projection
from app.services.retrieval import run_retrieval


log = logging.getLogger(__name__)
_REWRITE_SOURCES = ("original", "rule", "llm")


def _elapsed_ms(start: float) -> int:
    return max(0, round((time.perf_counter() - start) * 1_000))


def _bounded_identifier(value: Any, *, limit: int = 512) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, uuid.UUID)):
        return None
    normalized = str(value).strip()
    return normalized[:limit] if normalized else None


def _bounded_integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        normalized = float(value)
    except (TypeError, ValueError):
        return None
    return normalized if math.isfinite(normalized) else None


def _title_path(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(
        item.strip()[:200]
        for item in value[:16]
        if isinstance(item, str) and item.strip()
    )


def _rewrite_sources(metadata: dict[str, Any]) -> tuple[str, ...]:
    raw = metadata.get("rewrite_source")
    if not isinstance(raw, (list, tuple)):
        return ()
    values = {item for item in raw if isinstance(item, str)}
    return tuple(source for source in _REWRITE_SOURCES if source in values)


def _source_projection(metadata: dict[str, Any]) -> FederatedSourceProjection:
    locator_projection = (
        validate_projection(
            metadata.get("evidence_locator_v1_projection"),
            expected_identity={
                "document_id": metadata.get("document_id"),
                "document_revision_id": metadata.get("document_revision_id"),
                "document_revision": metadata.get("document_revision"),
                "document_revision_no": metadata.get("document_revision_no"),
                "chunk_id": metadata.get("chunk_id"),
            },
        )
        if settings.enable_evidence_locator_read
        else None
    )
    locator_source = (
        locator_projection.get("source")
        if isinstance(locator_projection, dict)
        and isinstance(locator_projection.get("source"), dict)
        else {}
    )
    page_span = locator_source.get("page")
    page = page_span.get("start") if isinstance(page_span, dict) else metadata.get("page")
    if page is None:
        page = metadata.get("page_no", metadata.get("page_number"))
    return FederatedSourceProjection(
        document_id=_bounded_identifier(
            (locator_projection or {}).get("document_id") or metadata.get("document_id")
        ),
        document_revision_id=_bounded_identifier(
            (locator_projection or {}).get("document_revision_id")
            or metadata.get("document_revision_id")
        ),
        document_revision=_bounded_integer(
            (locator_projection or {}).get("revision_no")
            or metadata.get("document_revision")
        ),
        chunk_id=_bounded_identifier(
            (locator_projection or {}).get("chunk_id") or metadata.get("chunk_id")
        ),
        seq=_bounded_integer(metadata.get("seq")),
        page=_bounded_integer(page),
        title_path=_title_path(
            locator_source.get("heading_path")
            if locator_source.get("heading_path") is not None
            else metadata.get("title_path")
        ),
        external_id=_bounded_identifier(metadata.get("external_id")),
        vector_score=_finite_float(metadata.get("vector_score")),
        rerank_score=_finite_float(metadata.get("rerank_score")),
        local_rrf_score=_finite_float(metadata.get("rrf_score")),
        dense_rank=_bounded_integer(metadata.get("dense_rank")),
        keyword_rank=_bounded_integer(metadata.get("keyword_rank")),
        rewrite_sources=_rewrite_sources(metadata),
    )


def fuse_library_records(
    executions: Sequence[FederatedLibraryExecution],
    *,
    top_k: int,
) -> tuple[FederatedHit, ...]:
    if isinstance(top_k, bool) or top_k < 1:
        raise FederatedRetrievalError("federated_request_invalid")
    pending: list[tuple[FederatedHit, tuple[int, int, str, str]]] = []
    for selection_index, execution in enumerate(executions):
        library = execution.profile.library
        for local_rank, record in enumerate(execution.records, start=1):
            metadata = record.metadata if isinstance(record.metadata, dict) else {}
            source = _source_projection(metadata)
            hit_identity = source.chunk_id or source.document_id or f"rank:{local_rank}"
            local_score = _finite_float(record.score) or 0.0
            content = record.content or ""
            hit = FederatedHit(
                rank=0,
                fusion_score=1.0 / (FEDERATED_RRF_K + local_rank),
                library_id=library.id,
                library_slug=library.slug,
                library_name=library.name,
                local_rank=local_rank,
                local_score=local_score,
                title=(record.title or "")[:FEDERATED_TITLE_CHARACTERS],
                content_excerpt=content[:FEDERATED_CONTENT_EXCERPT_CHARACTERS],
                content_truncated=len(content) > FEDERATED_CONTENT_EXCERPT_CHARACTERS,
                source=source,
            )
            pending.append(
                (
                    hit,
                    (selection_index, local_rank, str(library.id), hit_identity),
                )
            )
    pending.sort(key=lambda item: (-item[0].fusion_score, *item[1]))
    return tuple(
        replace(hit, rank=rank)
        for rank, (hit, _stable_key) in enumerate(pending[:top_k], start=1)
    )


async def run_federated_retrieval(
    db,
    *,
    user: User,
    command: FederatedRetrievalCommand,
) -> FederatedRetrievalResult:
    started = time.perf_counter()
    assessment = await assess_library_compatibility(
        db,
        user=user,
        library_slugs=command.library_slugs,
        channels=("text",),
    )
    if assessment.organization_id != command.organization_id or any(
        profile.library.organization_id != command.organization_id
        for profile in assessment.profiles
    ):
        raise FederatedRetrievalError("federated_scope_forbidden")
    if not assessment.compatible:
        raise FederatedRetrievalError(
            "federated_scope_incompatible",
            incompatibilities=assessment.incompatibilities,
        )

    executions: list[FederatedLibraryExecution] = []
    timings: list[FederatedLibraryTiming] = []
    for profile in assessment.profiles:
        library = profile.library
        branch_started = time.perf_counter()
        try:
            response = await run_retrieval(
                collection=library.qdrant_collection,
                embedding_model=library.embedding_model,
                embedding_base_url=library.embedding_base_url,
                request=DifyRetrievalRequest(
                    knowledge_id=library.slug,
                    query=command.query,
                    retrieval_setting=RetrievalSetting(
                        top_k=command.candidate_k,
                        score_threshold=command.score_threshold,
                    ),
                ),
                source_config=library.source_config,
                rerank_enabled=library.rerank_enabled,
                retrieval_mode=library.retrieval_mode,
                db=db,
                library=library,
            )
        except Exception as exc:
            raise FederatedRetrievalError("federated_branch_failed") from exc
        elapsed_ms = _elapsed_ms(branch_started)
        records = tuple(response.records[: command.candidate_k])
        executions.append(FederatedLibraryExecution(profile, records, elapsed_ms))
        timings.append(FederatedLibraryTiming(library.slug, len(records), elapsed_ms))

    hits = fuse_library_records(executions, top_k=command.top_k)
    total_elapsed_ms = _elapsed_ms(started)
    log.info(
        "federated retrieval completed: libraries=%s library_count=%s hits=%s elapsed_ms=%s",
        list(command.library_slugs),
        len(command.library_slugs),
        len(hits),
        total_elapsed_ms,
    )
    return FederatedRetrievalResult(
        assessment=assessment,
        hits=hits,
        timings=tuple(timings),
        total_elapsed_ms=total_elapsed_ms,
    )
