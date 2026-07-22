from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import Text, and_, cast, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import aliased

from app.config import Settings, settings
from app.models.document import Document
from app.models.document_revision import DocumentRevision, REVISION_STATUS_READY
from app.models.document_revision_file import DocumentRevisionFile
from app.models.entity_mention import EntityMention
from app.models.evidence_unit import EvidenceUnit
from app.models.graph_publication import GraphPublication
from app.models.graph_publication_item import GraphPublicationItem
from app.models.library import Library
from app.models.relation_evidence import RelationEvidence
from app.models.revision_retention import (
    RETENTION_POLICY_VERSION,
    RETENTION_REASON_REPLACEMENT_READY,
    RevisionRetentionRecord,
)
from app.services import audit_log


_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CURRENT_PUBLICATION_STATUSES = ("active", "degraded")
_CURRENT_ITEM_STATUSES = ("active", "degraded")


class RevisionRetentionError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code[:64]
        super().__init__(message[:255])


@dataclass(frozen=True, slots=True)
class RevisionRetentionImpact:
    active_entity_mentions: int
    active_relation_evidence: int
    current_publication_items: int
    dependency_evidence_ids: tuple[uuid.UUID, ...]

    @property
    def has_dependencies(self) -> bool:
        return (
            self.active_entity_mentions > 0
            or self.active_relation_evidence > 0
            or self.current_publication_items > 0
        )

    def payload(self) -> dict[str, object]:
        return {
            "active_entity_mentions": self.active_entity_mentions,
            "active_relation_evidence": self.active_relation_evidence,
            "current_publication_items": self.current_publication_items,
            "dependency_evidence_ids": [
                str(value) for value in self.dependency_evidence_ids
            ],
        }


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    record: RevisionRetentionRecord | None
    created: bool
    code: str


@dataclass(frozen=True, slots=True)
class MaintenanceResult:
    created_record_ids: tuple[uuid.UUID, ...]
    notice_record_ids: tuple[uuid.UUID, ...]
    refreshed_record_ids: tuple[uuid.UUID, ...]


def _now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        raise RevisionRetentionError("invalid_time", "retention time must be timezone-aware")
    return current


def _policy(library: Library) -> tuple[int, int]:
    retention_days = library.revision_retention_days
    notice_days = library.revision_retention_notice_days
    if (
        isinstance(retention_days, bool)
        or isinstance(notice_days, bool)
        or not isinstance(retention_days, int)
        or not isinstance(notice_days, int)
        or not 30 <= retention_days <= 60
        or not 1 <= notice_days <= 14
        or notice_days >= retention_days
    ):
        raise RevisionRetentionError(
            "retention_policy_invalid", "Library retention policy is invalid"
        )
    return retention_days, notice_days


def _canonical_impact(impact: RevisionRetentionImpact) -> tuple[dict[str, object], str]:
    payload = impact.payload()
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > 8_192:
        raise RevisionRetentionError(
            "retention_impact_too_large", "retention impact is too large"
        )
    return payload, hashlib.sha256(encoded).hexdigest()


async def project_revision_retention_impact(
    db,
    *,
    library_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    evidence_sample_limit: int | None = None,
    config: Settings = settings,
) -> RevisionRetentionImpact:
    limit = evidence_sample_limit or config.revision_retention_impact_evidence_sample
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise RevisionRetentionError(
            "retention_impact_limit_invalid", "retention impact limit is invalid"
        )
    mention_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(EntityMention)
                .where(
                    EntityMention.library_id == library_id,
                    EntityMention.document_revision_id == document_revision_id,
                    EntityMention.status == "active",
                )
            )
        ).scalar_one()
    )
    relation_count = int(
        (
            await db.execute(
                select(func.count())
                .select_from(RelationEvidence)
                .where(
                    RelationEvidence.library_id == library_id,
                    RelationEvidence.document_revision_id == document_revision_id,
                    RelationEvidence.status == "active",
                )
            )
        ).scalar_one()
    )
    support_join = GraphPublicationItem.support_evidence_ids.op("?")(
        cast(EvidenceUnit.id, Text)
    )
    publication_filters = (
        EvidenceUnit.library_id == library_id,
        EvidenceUnit.document_revision_id == document_revision_id,
        GraphPublication.status.in_(_CURRENT_PUBLICATION_STATUSES),
        GraphPublicationItem.status.in_(_CURRENT_ITEM_STATUSES),
    )
    publication_count = int(
        (
            await db.execute(
                select(func.count(func.distinct(GraphPublicationItem.id)))
                .select_from(GraphPublicationItem)
                .join(
                    GraphPublication,
                    GraphPublication.id == GraphPublicationItem.publication_id,
                )
                .join(EvidenceUnit, support_join)
                .where(*publication_filters)
            )
        ).scalar_one()
    )
    dependency_ids = tuple(
        (
            await db.execute(
                select(EvidenceUnit.id)
                .select_from(EvidenceUnit)
                .join(GraphPublicationItem, support_join)
                .join(
                    GraphPublication,
                    GraphPublication.id == GraphPublicationItem.publication_id,
                )
                .where(*publication_filters)
                .distinct()
                .order_by(EvidenceUnit.id)
                .limit(limit)
            )
        ).scalars().all()
    )
    return RevisionRetentionImpact(
        active_entity_mentions=mention_count,
        active_relation_evidence=relation_count,
        current_publication_items=publication_count,
        dependency_evidence_ids=dependency_ids,
    )


async def _locked_one(db, model, row_id: uuid.UUID):
    return (
        await db.execute(select(model).where(model.id == row_id).with_for_update())
    ).scalars().first()


async def schedule_revision_retention(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    replacement_revision_id: uuid.UUID,
    at: datetime | None = None,
    config: Settings = settings,
) -> ScheduleResult:
    current_time = _now(at)
    if not config.revision_retention_enabled:
        return ScheduleResult(None, False, "retention_disabled")
    document = await _locked_one(db, Document, document_id)
    if document is None or document.library_id != library_id:
        return ScheduleResult(None, False, "document_unavailable")
    library = await db.get(Library, library_id)
    if library is None or library.deleted_at is not None:
        return ScheduleResult(None, False, "library_unavailable")
    if library.lifecycle_mode != "managed" or not library.revision_retention_enabled:
        return ScheduleResult(None, False, "library_retention_disabled")
    if document.deleted_at is not None:
        return ScheduleResult(None, False, "document_deleted")
    if (
        document.current_revision_id != replacement_revision_id
        or replacement_revision_id == document_revision_id
    ):
        return ScheduleResult(None, False, "replacement_not_current")
    old_revision = await _locked_one(db, DocumentRevision, document_revision_id)
    replacement = await _locked_one(db, DocumentRevision, replacement_revision_id)
    if (
        old_revision is None
        or old_revision.library_id != library_id
        or old_revision.document_id != document_id
        or old_revision.status != "superseded"
    ):
        return ScheduleResult(None, False, "revision_not_superseded")
    if (
        replacement is None
        or replacement.library_id != library_id
        or replacement.document_id != document_id
        or replacement.status != REVISION_STATUS_READY
    ):
        return ScheduleResult(None, False, "replacement_not_ready")
    ready_at = _now(
        replacement.published_at
        or replacement.finished_at
        or replacement.created_at
    )
    revision_file = (
        await db.execute(
            select(DocumentRevisionFile)
            .where(
                DocumentRevisionFile.library_id == library_id,
                DocumentRevisionFile.document_id == document_id,
                DocumentRevisionFile.document_revision_id == document_revision_id,
            )
            .with_for_update()
        )
    ).scalars().first()
    if revision_file is None:
        return ScheduleResult(None, False, "revision_file_unavailable")
    existing = (
        await db.execute(
            select(RevisionRetentionRecord)
            .where(RevisionRetentionRecord.revision_file_id == revision_file.id)
            .with_for_update()
        )
    ).scalars().first()
    if existing is not None:
        return ScheduleResult(existing, False, "retention_exists")
    retention_days, notice_days = _policy(library)
    cleanup_not_before = ready_at + timedelta(days=retention_days)
    impact = await project_revision_retention_impact(
        db,
        library_id=library_id,
        document_revision_id=document_revision_id,
        config=config,
    )
    impact_snapshot, impact_hash = _canonical_impact(impact)
    blocked = impact.has_dependencies
    status = (
        "blocked"
        if blocked
        else "eligible"
        if current_time >= cleanup_not_before
        else "scheduled"
    )
    row = RevisionRetentionRecord(
        id=uuid.uuid4(),
        library_id=library_id,
        document_id=document_id,
        document_revision_id=document_revision_id,
        replacement_revision_id=replacement_revision_id,
        revision_file_id=revision_file.id,
        reason=RETENTION_REASON_REPLACEMENT_READY,
        status=status,
        policy_version=RETENTION_POLICY_VERSION,
        retention_days=retention_days,
        notice_days=notice_days,
        replacement_ready_at=ready_at,
        cleanup_eligible_at=ready_at,
        cleanup_not_before=cleanup_not_before,
        notice_at=cleanup_not_before - timedelta(days=notice_days),
        block_code="active_graph_dependency" if blocked else None,
        impact_snapshot=impact_snapshot,
        impact_hash=impact_hash,
        idempotency_key=f"replacement:{revision_file.id}:{replacement_revision_id}",
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError:
        winner = (
            await db.execute(
                select(RevisionRetentionRecord)
                .where(RevisionRetentionRecord.revision_file_id == revision_file.id)
                .with_for_update()
            )
        ).scalars().first()
        if winner is None:
            raise
        return ScheduleResult(winner, False, "retention_exists")
    return ScheduleResult(row, True, "retention_created")


def _set_state(
    row: RevisionRetentionRecord,
    *,
    status: str,
    block_code: str | None = None,
) -> None:
    row.status = status
    row.block_code = block_code if status in {"blocked", "cancelled"} else None
    if status != "held":
        row.hold_reason_code = None
        row.held_by_user_id = None
        row.held_at = None


async def _record_scope(db, record_id: uuid.UUID):
    scope = (
        await db.execute(
            select(
                RevisionRetentionRecord.document_id,
                RevisionRetentionRecord.library_id,
            ).where(RevisionRetentionRecord.id == record_id)
        )
    ).first()
    if scope is None:
        raise RevisionRetentionError("retention_not_found", "retention record was not found")
    document = await _locked_one(db, Document, scope.document_id)
    row = await _locked_one(db, RevisionRetentionRecord, record_id)
    if document is None or row is None or document.library_id != scope.library_id:
        raise RevisionRetentionError("retention_scope_missing", "retention scope is unavailable")
    return document, row


async def refresh_revision_retention(
    db,
    *,
    record_id: uuid.UUID,
    at: datetime | None = None,
    config: Settings = settings,
) -> RevisionRetentionRecord:
    current_time = _now(at)
    document, row = await _record_scope(db, record_id)
    if row.status in {"queued", "processing", "cleaned", "failed", "cancelled"}:
        return row
    library = await db.get(Library, row.library_id)
    old_revision = await _locked_one(db, DocumentRevision, row.document_revision_id)
    revision_file = await _locked_one(db, DocumentRevisionFile, row.revision_file_id)
    impact = await project_revision_retention_impact(
        db,
        library_id=row.library_id,
        document_revision_id=row.document_revision_id,
        config=config,
    )
    row.impact_snapshot, row.impact_hash = _canonical_impact(impact)
    if row.status == "held":
        return row
    if document.deleted_at is not None:
        _set_state(row, status="cancelled", block_code="document_deleted")
        return row
    if library is None or library.deleted_at is not None:
        _set_state(row, status="blocked", block_code="library_unavailable")
        return row
    if library.lifecycle_mode != "managed" or not library.revision_retention_enabled:
        _set_state(row, status="blocked", block_code="library_policy_disabled")
        return row
    if old_revision is None or old_revision.status != "superseded":
        _set_state(row, status="cancelled", block_code="revision_not_superseded")
        return row
    if (
        revision_file is None
        or revision_file.library_id != row.library_id
        or revision_file.document_id != row.document_id
        or revision_file.document_revision_id != row.document_revision_id
    ):
        _set_state(row, status="blocked", block_code="revision_file_unavailable")
        return row
    current_revision_id = document.current_revision_id
    if current_revision_id is None or current_revision_id == row.document_revision_id:
        _set_state(row, status="blocked", block_code="replacement_not_current")
        return row
    replacement = await _locked_one(db, DocumentRevision, current_revision_id)
    if (
        replacement is None
        or replacement.library_id != row.library_id
        or replacement.document_id != row.document_id
        or replacement.status != REVISION_STATUS_READY
    ):
        _set_state(row, status="blocked", block_code="replacement_not_ready")
        return row
    if impact.has_dependencies:
        _set_state(row, status="blocked", block_code="active_graph_dependency")
    elif current_time < row.cleanup_not_before:
        _set_state(row, status="scheduled")
    else:
        _set_state(row, status="eligible")
    return row


def _require_code(value: str, *, field: str) -> str:
    if not isinstance(value, str) or not _CODE.fullmatch(value):
        raise RevisionRetentionError("invalid_command", f"{field} is invalid")
    return value


async def extend_revision_retention_deadline(
    db,
    *,
    record_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    cleanup_not_before: datetime,
    at: datetime | None = None,
) -> RevisionRetentionRecord:
    current_time = _now(at)
    deadline = _now(cleanup_not_before)
    _, row = await _record_scope(db, record_id)
    minimum = row.cleanup_eligible_at + timedelta(days=30)
    maximum = row.cleanup_eligible_at + timedelta(days=60)
    if deadline < row.cleanup_not_before or deadline < minimum or deadline > maximum:
        raise RevisionRetentionError(
            "retention_deadline_invalid", "retention deadline extension is invalid"
        )
    if deadline == row.cleanup_not_before:
        return row
    row.cleanup_not_before = deadline
    row.notice_at = deadline - timedelta(days=row.notice_days)
    if (
        row.notice_recorded_at is not None
        and row.notice_recorded_at < row.notice_at
    ):
        row.notice_recorded_at = None
    row.deadline_changed_by_user_id = actor_user_id
    row.deadline_changed_at = current_time
    if row.status == "eligible":
        _set_state(row, status="scheduled")
    await audit_log.record(
        db,
        actor_user_id,
        "revision_retention.extend",
        {
            "record_id": str(row.id),
            "document_revision_id": str(row.document_revision_id),
            "cleanup_not_before": deadline.isoformat(),
        },
    )
    return row


async def hold_revision_retention(
    db,
    *,
    record_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    reason_code: str,
    at: datetime | None = None,
) -> RevisionRetentionRecord:
    current_time = _now(at)
    reason = _require_code(reason_code, field="hold reason")
    _, row = await _record_scope(db, record_id)
    if row.status in {"queued", "processing", "cleaned", "cancelled"}:
        raise RevisionRetentionError("retention_not_holdable", "retention cannot be held")
    if (
        row.status == "held"
        and row.held_by_user_id == actor_user_id
        and row.hold_reason_code == reason
    ):
        return row
    _set_state(row, status="held")
    row.hold_reason_code = reason
    row.held_by_user_id = actor_user_id
    row.held_at = current_time
    await audit_log.record(
        db,
        actor_user_id,
        "revision_retention.hold",
        {
            "record_id": str(row.id),
            "document_revision_id": str(row.document_revision_id),
            "reason_code": reason,
        },
    )
    return row


async def release_revision_retention_hold(
    db,
    *,
    record_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    at: datetime | None = None,
    config: Settings = settings,
) -> RevisionRetentionRecord:
    _, row = await _record_scope(db, record_id)
    if row.status != "held":
        return row
    _set_state(row, status="scheduled")
    await audit_log.record(
        db,
        actor_user_id,
        "revision_retention.release_hold",
        {
            "record_id": str(row.id),
            "document_revision_id": str(row.document_revision_id),
        },
    )
    return await refresh_revision_retention(
        db, record_id=record_id, at=at, config=config
    )


async def compensate_revision_retention_records(
    db,
    *,
    limit: int,
    at: datetime | None = None,
    config: Settings = settings,
) -> tuple[uuid.UUID, ...]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise RevisionRetentionError("retention_batch_invalid", "retention batch is invalid")
    if not config.revision_retention_enabled:
        return ()
    replacement = aliased(DocumentRevision)
    candidates = (
        await db.execute(
            select(
                DocumentRevision.library_id.label("library_id"),
                DocumentRevision.document_id.label("document_id"),
                DocumentRevision.id.label("document_revision_id"),
                replacement.id.label("replacement_revision_id"),
            )
            .join(Document, Document.id == DocumentRevision.document_id)
            .join(Library, Library.id == DocumentRevision.library_id)
            .join(
                DocumentRevisionFile,
                DocumentRevisionFile.document_revision_id == DocumentRevision.id,
            )
            .join(replacement, replacement.id == Document.current_revision_id)
            .where(
                DocumentRevision.status == "superseded",
                replacement.status == REVISION_STATUS_READY,
                replacement.id != DocumentRevision.id,
                Document.deleted_at.is_(None),
                Library.deleted_at.is_(None),
                Library.lifecycle_mode == "managed",
                Library.revision_retention_enabled.is_(True),
                ~select(RevisionRetentionRecord.id)
                .where(
                    RevisionRetentionRecord.revision_file_id
                    == DocumentRevisionFile.id
                )
                .exists(),
            )
            .order_by(DocumentRevision.id)
            .limit(limit)
        )
    ).all()
    created: list[uuid.UUID] = []
    for candidate in candidates:
        result = await schedule_revision_retention(
            db,
            library_id=candidate.library_id,
            document_id=candidate.document_id,
            document_revision_id=candidate.document_revision_id,
            replacement_revision_id=candidate.replacement_revision_id,
            at=at,
            config=config,
        )
        if result.created and result.record is not None:
            created.append(result.record.id)
    return tuple(created)


async def record_due_retention_notices(
    db,
    *,
    limit: int,
    at: datetime | None = None,
) -> tuple[uuid.UUID, ...]:
    current_time = _now(at)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise RevisionRetentionError("retention_batch_invalid", "retention batch is invalid")
    rows = (
        await db.execute(
            select(RevisionRetentionRecord)
            .where(
                RevisionRetentionRecord.notice_recorded_at.is_(None),
                RevisionRetentionRecord.notice_at <= current_time,
                RevisionRetentionRecord.status.in_(
                    ("scheduled", "eligible", "blocked", "held")
                ),
            )
            .order_by(RevisionRetentionRecord.notice_at, RevisionRetentionRecord.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    for row in rows:
        row.notice_recorded_at = current_time
    return tuple(row.id for row in rows)


async def run_revision_retention_maintenance(
    db,
    *,
    at: datetime | None = None,
    config: Settings = settings,
) -> MaintenanceResult:
    if not config.revision_retention_enabled:
        return MaintenanceResult((), (), ())
    current_time = _now(at)
    limit = config.revision_retention_batch_size
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise RevisionRetentionError("retention_batch_invalid", "retention batch is invalid")
    created = await compensate_revision_retention_records(
        db, limit=limit, at=current_time, config=config
    )
    notices = await record_due_retention_notices(db, limit=limit, at=current_time)
    record_ids = tuple(
        (
            await db.execute(
                select(RevisionRetentionRecord.id)
                .where(
                    or_(
                        and_(
                            RevisionRetentionRecord.status == "scheduled",
                            RevisionRetentionRecord.cleanup_not_before <= current_time,
                        ),
                        RevisionRetentionRecord.status.in_(("eligible", "blocked")),
                    )
                )
                .order_by(
                    RevisionRetentionRecord.updated_at,
                    RevisionRetentionRecord.id,
                )
                .limit(limit)
            )
        ).scalars().all()
    )
    refreshed: list[uuid.UUID] = []
    for record_id in record_ids:
        await refresh_revision_retention(
            db, record_id=record_id, at=current_time, config=config
        )
        refreshed.append(record_id)
    return MaintenanceResult(created, notices, tuple(refreshed))
