from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.models.classification_decision import (
    DocumentClassificationDecision,
    DocumentClassificationDecisionSet,
    DocumentClassificationProposal,
    DocumentClassificationRun,
)
from app.models.classification_taxonomy import (
    ClassificationLabel,
    ClassificationTaxonomy,
    LibraryClassificationLabel,
)
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.models.organization import Organization
from app.models.user import User
from app.services import audit_log
from app.services.classification_decision_contracts import (
    CLASSIFICATION_MAX_SECONDARY_LABELS,
    CLASSIFICATION_MIN_CONFIDENCE_MICROS,
    CLASSIFICATION_MIN_MARGIN_MICROS,
    CLASSIFICATION_POLICY_VERSION,
    ManualClassificationSelection,
    RecordClassificationFailureCommand,
    RemoveManualClassificationCommand,
    ReviewClassificationRunCommand,
    SetManualClassificationCommand,
    SubmitClassificationRunCommand,
    ClassificationDecisionError,
    classification_failure_identity,
    classification_run_identity,
)
from app.services.classification_policy import (
    KnownLabelPolicyState,
    evaluate_classification_policy,
)
from app.services.organization_authorization import (
    OrganizationAuthorizationError,
    authorize_library_management,
)


@dataclass(frozen=True, slots=True)
class ClassificationRunResult:
    run: DocumentClassificationRun
    proposals: tuple[DocumentClassificationProposal, ...]
    decision_set: DocumentClassificationDecisionSet | None
    decisions: tuple[DocumentClassificationDecision, ...]


@dataclass(frozen=True, slots=True)
class EffectiveClassification:
    document: Document
    revision: DocumentRevision
    state: str
    decision_set: DocumentClassificationDecisionSet | None
    decisions: tuple[tuple[DocumentClassificationDecision, ClassificationLabel], ...]
    latest_run: DocumentClassificationRun | None


@dataclass(frozen=True, slots=True)
class ClassificationReviewItem:
    run: DocumentClassificationRun
    proposals: tuple[
        tuple[DocumentClassificationProposal, ClassificationLabel | None], ...
    ]


@dataclass(frozen=True, slots=True)
class ClassificationReviewPage:
    items: tuple[ClassificationReviewItem, ...]
    total: int


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _load_library(db, library_id: uuid.UUID, *, lock: bool) -> Library:
    statement = select(Library).where(
        Library.id == library_id,
        Library.deleted_at.is_(None),
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    library = (await db.execute(statement)).scalars().first()
    if library is None:
        raise ClassificationDecisionError("classification_library_not_found")
    return library


async def _require_library_management(
    db,
    *,
    library_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> Library:
    library = await _load_library(db, library_id, lock=False)
    user = (
        await db.execute(
            select(User).where(
                User.id == actor_user_id,
                User.is_active.is_(True),
                User.deleted_at.is_(None),
            )
        )
    ).scalars().first()
    if user is None:
        raise ClassificationDecisionError("classification_admin_forbidden")
    try:
        await authorize_library_management(db, user=user, library=library)
    except OrganizationAuthorizationError as exc:
        raise ClassificationDecisionError("classification_admin_forbidden") from exc
    return library


async def _lock_current_document(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    revision_id: uuid.UUID | None = None,
    content_hash: str | None = None,
) -> tuple[Document, DocumentRevision]:
    document = (
        await db.execute(
            select(Document)
            .where(
                Document.id == document_id,
                Document.library_id == library_id,
                Document.deleted_at.is_(None),
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if document is None or document.current_revision_id is None:
        raise ClassificationDecisionError("classification_document_not_found")
    if revision_id is not None and document.current_revision_id != revision_id:
        raise ClassificationDecisionError("classification_revision_stale")
    revision = (
        await db.execute(
            select(DocumentRevision)
            .where(
                DocumentRevision.id == document.current_revision_id,
                DocumentRevision.document_id == document.id,
                DocumentRevision.library_id == library_id,
                DocumentRevision.status == "ready",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if revision is None:
        raise ClassificationDecisionError("classification_revision_unavailable")
    if content_hash is not None and revision.content_hash != content_hash:
        raise ClassificationDecisionError("classification_revision_stale")
    return document, revision


async def _lock_organization(db, organization_id: uuid.UUID) -> Organization:
    organization = (
        await db.execute(
            select(Organization)
            .where(
                Organization.id == organization_id,
                Organization.status == "active",
            )
            .with_for_update()
        )
    ).scalars().first()
    if organization is None:
        raise ClassificationDecisionError("classification_scope_not_found")
    return organization


async def _lock_taxonomy_and_enabled_labels(
    db,
    *,
    library: Library,
    expected_taxonomy_id: uuid.UUID | None = None,
    expected_enabled_label_ids: tuple[uuid.UUID, ...] | None = None,
) -> tuple[
    ClassificationTaxonomy,
    tuple[ClassificationLabel, ...],
    dict[uuid.UUID, ClassificationLabel],
]:
    await _lock_organization(db, library.organization_id)
    library = await _load_library(db, library.id, lock=True)
    taxonomy_statement = select(ClassificationTaxonomy).where(
        ClassificationTaxonomy.organization_id == library.organization_id,
        ClassificationTaxonomy.status == "active",
    )
    if expected_taxonomy_id is not None:
        taxonomy_statement = taxonomy_statement.where(
            ClassificationTaxonomy.id == expected_taxonomy_id
        )
    taxonomy = (
        await db.execute(
            taxonomy_statement.with_for_update().execution_options(populate_existing=True)
        )
    ).scalars().first()
    if taxonomy is None:
        raise ClassificationDecisionError("classification_taxonomy_state_changed")
    enabled = tuple(
        (
            await db.execute(
                select(ClassificationLabel)
                .join(
                    LibraryClassificationLabel,
                    LibraryClassificationLabel.label_id == ClassificationLabel.id,
                )
                .where(
                    LibraryClassificationLabel.library_id == library.id,
                    LibraryClassificationLabel.taxonomy_version_id == taxonomy.id,
                    ClassificationLabel.taxonomy_version_id == taxonomy.id,
                    ClassificationLabel.status == "active",
                )
                .order_by(LibraryClassificationLabel.ordinal)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    enabled_ids = tuple(label.id for label in enabled)
    if expected_enabled_label_ids is not None and enabled_ids != expected_enabled_label_ids:
        raise ClassificationDecisionError("classification_label_set_state_changed")
    if not enabled:
        raise ClassificationDecisionError("classification_label_selection_invalid")
    all_labels = tuple(
        (
            await db.execute(
                select(ClassificationLabel)
                .where(ClassificationLabel.taxonomy_version_id == taxonomy.id)
                .order_by(ClassificationLabel.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    return taxonomy, enabled, {label.id: label for label in all_labels}


async def lock_classification_scope(
    db,
    *,
    library: Library,
    expected_taxonomy_id: uuid.UUID | None = None,
    expected_enabled_label_ids: tuple[uuid.UUID, ...] | None = None,
) -> tuple[
    ClassificationTaxonomy,
    tuple[ClassificationLabel, ...],
    dict[uuid.UUID, ClassificationLabel],
]:
    return await _lock_taxonomy_and_enabled_labels(
        db,
        library=library,
        expected_taxonomy_id=expected_taxonomy_id,
        expected_enabled_label_ids=expected_enabled_label_ids,
    )


async def _effective_set(
    db,
    revision_id: uuid.UUID,
    *,
    lock: bool,
) -> DocumentClassificationDecisionSet | None:
    statement = select(DocumentClassificationDecisionSet).where(
        DocumentClassificationDecisionSet.document_revision_id == revision_id,
        DocumentClassificationDecisionSet.lifecycle == "effective",
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(statement)).scalars().first()


async def _latest_decision_set(
    db,
    revision_id: uuid.UUID,
    *,
    lock: bool,
) -> DocumentClassificationDecisionSet | None:
    statement = (
        select(DocumentClassificationDecisionSet)
        .where(DocumentClassificationDecisionSet.document_revision_id == revision_id)
        .order_by(DocumentClassificationDecisionSet.generation_no.desc())
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return (await db.execute(statement)).scalars().first()


async def _normalize_effective_set_for_scope(
    db,
    *,
    current: DocumentClassificationDecisionSet | None,
    taxonomy: ClassificationTaxonomy,
    enabled: tuple[ClassificationLabel, ...],
) -> DocumentClassificationDecisionSet | None:
    if current is None:
        return None
    valid = current.taxonomy_version_id == taxonomy.id
    if valid:
        selected_ids = set(
            (
                await db.execute(
                    select(DocumentClassificationDecision.label_id).where(
                        DocumentClassificationDecision.decision_set_id == current.id
                    )
                )
            ).scalars().all()
        )
        enabled_ids = {label.id for label in enabled}
        valid = bool(selected_ids) and selected_ids.issubset(enabled_ids)
    if valid:
        return current
    now = _now()
    current.lifecycle = "superseded"
    current.superseded_at = now
    current.updated_at = now
    await db.flush()
    return None


def _fence_effective_set(
    current: DocumentClassificationDecisionSet | None,
    expected_id: uuid.UUID | None,
) -> None:
    current_id = current.id if current is not None else None
    if current_id != expected_id:
        raise ClassificationDecisionError("classification_decision_state_changed")


async def _next_decision_generation(db, revision_id: uuid.UUID) -> int:
    highest = (
        await db.execute(
            select(func.max(DocumentClassificationDecisionSet.generation_no)).where(
                DocumentClassificationDecisionSet.document_revision_id == revision_id
            )
        )
    ).scalar_one()
    return int(highest or 0) + 1


async def _replace_decision_set(
    db,
    *,
    document: Document,
    revision: DocumentRevision,
    taxonomy: ClassificationTaxonomy,
    source: str,
    source_run: DocumentClassificationRun | None,
    actor_user_id: uuid.UUID | None,
    primary: tuple[uuid.UUID, int | None] | None,
    secondaries: tuple[tuple[uuid.UUID, int | None], ...],
    current: DocumentClassificationDecisionSet | None,
    remove: bool = False,
) -> tuple[DocumentClassificationDecisionSet, tuple[DocumentClassificationDecision, ...]]:
    now = _now()
    if current is not None:
        current.lifecycle = "superseded"
        current.superseded_at = now
        current.updated_at = now
        await db.flush()
    generation_no = await _next_decision_generation(db, revision.id)
    decision_set = DocumentClassificationDecisionSet(
        id=uuid.uuid4(),
        library_id=document.library_id,
        document_id=document.id,
        document_revision_id=revision.id,
        taxonomy_version_id=taxonomy.id,
        source=source,
        source_run_id=source_run.id if source_run is not None else None,
        lifecycle="removed" if remove else "effective",
        generation_no=generation_no,
        policy_version=source_run.policy_version if source == "model" else None,
        classifier_version=source_run.classifier_version if source == "model" else None,
        model_provider=source_run.model_provider if source == "model" else None,
        model_name=source_run.model_name if source == "model" else None,
        model_config_hash=source_run.model_config_hash if source == "model" else None,
        prompt_version=source_run.prompt_version if source == "model" else None,
        input_fingerprint=source_run.input_fingerprint if source == "model" else None,
        supersedes_decision_set_id=current.id if current is not None else None,
        reviewed_by_user_id=actor_user_id if source == "manual" else None,
        reviewed_at=now if source == "manual" else None,
        removed_at=now if remove else None,
    )
    db.add(decision_set)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ClassificationDecisionError("classification_decision_conflict") from exc
    decisions: list[DocumentClassificationDecision] = []
    if not remove:
        if primary is None:
            raise ClassificationDecisionError("classification_primary_missing")
        decisions.append(
            DocumentClassificationDecision(
                id=uuid.uuid4(),
                decision_set_id=decision_set.id,
                label_id=primary[0],
                role="primary",
                ordinal=0,
                confidence_micros=primary[1],
            )
        )
        decisions.extend(
            DocumentClassificationDecision(
                id=uuid.uuid4(),
                decision_set_id=decision_set.id,
                label_id=label_id,
                role="secondary",
                ordinal=ordinal,
                confidence_micros=confidence,
            )
            for ordinal, (label_id, confidence) in enumerate(secondaries, start=1)
        )
        db.add_all(decisions)
        await db.flush()
    return decision_set, tuple(decisions)


async def _load_result(
    db,
    run: DocumentClassificationRun,
) -> ClassificationRunResult:
    proposals = tuple(
        (
            await db.execute(
                select(DocumentClassificationProposal)
                .where(DocumentClassificationProposal.run_id == run.id)
                .order_by(
                    DocumentClassificationProposal.role,
                    DocumentClassificationProposal.rank,
                )
            )
        )
        .scalars()
        .all()
    )
    decision_set = (
        await db.execute(
            select(DocumentClassificationDecisionSet)
            .where(DocumentClassificationDecisionSet.source_run_id == run.id)
            .order_by(DocumentClassificationDecisionSet.generation_no.desc())
            .limit(1)
        )
    ).scalars().first()
    decisions: tuple[DocumentClassificationDecision, ...] = ()
    if decision_set is not None:
        decisions = tuple(
            (
                await db.execute(
                    select(DocumentClassificationDecision)
                    .where(
                        DocumentClassificationDecision.decision_set_id == decision_set.id
                    )
                    .order_by(DocumentClassificationDecision.ordinal)
                )
            )
            .scalars()
            .all()
        )
    return ClassificationRunResult(run, proposals, decision_set, decisions)


async def submit_classification_run(
    db,
    command: SubmitClassificationRunCommand,
) -> ClassificationRunResult:
    identity = classification_run_identity(command)
    document, revision = await _lock_current_document(
        db,
        library_id=command.library_id,
        document_id=command.document_id,
        revision_id=command.document_revision_id,
        content_hash=command.revision_content_hash,
    )
    existing = (
        await db.execute(
            select(DocumentClassificationRun)
            .where(
                DocumentClassificationRun.idempotency_key == identity.idempotency_key,
                DocumentClassificationRun.library_id == command.library_id,
                DocumentClassificationRun.document_id == command.document_id,
                DocumentClassificationRun.document_revision_id
                == command.document_revision_id,
            )
            .with_for_update()
        )
    ).scalars().first()
    if existing is not None:
        return await _load_result(db, existing)
    library = await _load_library(db, command.library_id, lock=False)
    taxonomy, enabled, all_labels = await _lock_taxonomy_and_enabled_labels(
        db,
        library=library,
        expected_taxonomy_id=command.taxonomy_version_id,
        expected_enabled_label_ids=command.enabled_label_ids,
    )
    referenced_ids = {
        proposal.label_id for proposal in command.proposals if proposal.label_id is not None
    }
    if any(label_id not in all_labels for label_id in referenced_ids):
        raise ClassificationDecisionError("classification_label_scope_invalid")
    enabled_ids = {label.id for label in enabled}
    states = {
        label_id: KnownLabelPolicyState(
            label_id=label_id,
            active=all_labels[label_id].status == "active",
            enabled=label_id in enabled_ids,
        )
        for label_id in referenced_ids
    }
    current = await _effective_set(db, revision.id, lock=True)
    current = await _normalize_effective_set_for_scope(
        db,
        current=current,
        taxonomy=taxonomy,
        enabled=enabled,
    )
    latest_decision = current
    if latest_decision is None:
        latest_decision = await _latest_decision_set(db, revision.id, lock=True)
    evaluation = evaluate_classification_policy(
        command.proposals,
        known_labels=states,
        has_manual_decision_protection=(
            latest_decision is not None
            and latest_decision.source == "manual"
            and latest_decision.lifecycle in {"effective", "removed"}
        ),
    )
    highest_generation = (
        await db.execute(
            select(func.max(DocumentClassificationRun.generation_no)).where(
                DocumentClassificationRun.document_revision_id == revision.id
            )
        )
    ).scalar_one()
    run = DocumentClassificationRun(
        id=uuid.uuid4(),
        library_id=document.library_id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy.id,
        enabled_label_set_hash=identity.enabled_label_set_hash,
        policy_version=CLASSIFICATION_POLICY_VERSION,
        min_confidence_micros=CLASSIFICATION_MIN_CONFIDENCE_MICROS,
        min_margin_micros=CLASSIFICATION_MIN_MARGIN_MICROS,
        max_secondary_labels=CLASSIFICATION_MAX_SECONDARY_LABELS,
        classifier_version=command.classifier_version,
        model_provider=command.model_provider,
        model_name=command.model_name,
        model_config_hash=command.model_config_hash,
        prompt_version=command.prompt_version,
        input_fingerprint=identity.input_fingerprint,
        idempotency_key=identity.idempotency_key,
        generation_no=int(highest_generation or 0) + 1,
        retry_generation=command.retry_generation,
        trigger_type=command.trigger_type,
        status=evaluation.run_status,
        reason_codes=list(evaluation.run_reason_codes),
        requested_by_user_id=command.requested_by_user_id,
        finished_at=_now(),
    )
    db.add(run)
    await db.flush()
    proposal_rows = tuple(
        DocumentClassificationProposal(
            id=uuid.uuid4(),
            run_id=run.id,
            label_id=proposal.label_id,
            proposed_key=proposal.proposed_key,
            proposed_label=proposal.proposed_label,
            role=proposal.role,
            rank=sum(
                1
                for prior in command.proposals[:index]
                if prior.role == proposal.role
            ),
            confidence_micros=proposal.confidence_micros,
            status=evaluation.proposal_statuses[index],
            reason_codes=list(evaluation.proposal_reason_codes[index]),
        )
        for index, proposal in enumerate(command.proposals)
    )
    db.add_all(proposal_rows)
    await db.flush()
    decision_set = None
    decisions: tuple[DocumentClassificationDecision, ...] = ()
    if evaluation.run_status == "auto_applied":
        primary_index = evaluation.selected_primary_index
        if primary_index is None:
            raise ClassificationDecisionError("classification_primary_missing")
        primary_proposal = command.proposals[primary_index]
        if primary_proposal.label_id is None:
            raise ClassificationDecisionError("classification_primary_missing")
        secondary_values = tuple(
            (
                command.proposals[index].label_id,
                command.proposals[index].confidence_micros,
            )
            for index in evaluation.selected_secondary_indices
        )
        if any(label_id is None for label_id, _ in secondary_values):
            raise ClassificationDecisionError("classification_label_selection_invalid")
        decision_set, decisions = await _replace_decision_set(
            db,
            document=document,
            revision=revision,
            taxonomy=taxonomy,
            source="model",
            source_run=run,
            actor_user_id=None,
            primary=(primary_proposal.label_id, primary_proposal.confidence_micros),
            secondaries=tuple(
                (label_id, confidence)
                for label_id, confidence in secondary_values
                if label_id is not None
            ),
            current=current,
        )
    await audit_log.record(
        db,
        command.requested_by_user_id,
        "classification.run_record",
        {
            "library_id": str(document.library_id),
            "document_id": str(document.id),
            "revision_id": str(revision.id),
            "run_id": str(run.id),
            "status": run.status,
            "proposal_count": len(proposal_rows),
            "decision_set_id": str(decision_set.id) if decision_set else None,
        },
    )
    return ClassificationRunResult(run, proposal_rows, decision_set, decisions)


async def record_classification_failure(
    db,
    command: RecordClassificationFailureCommand,
) -> ClassificationRunResult:
    identity = classification_failure_identity(command)
    document, revision = await _lock_current_document(
        db,
        library_id=command.library_id,
        document_id=command.document_id,
        revision_id=command.document_revision_id,
        content_hash=command.revision_content_hash,
    )
    existing = (
        await db.execute(
            select(DocumentClassificationRun)
            .where(
                DocumentClassificationRun.idempotency_key == identity.idempotency_key,
                DocumentClassificationRun.library_id == command.library_id,
                DocumentClassificationRun.document_id == command.document_id,
                DocumentClassificationRun.document_revision_id
                == command.document_revision_id,
            )
            .with_for_update()
        )
    ).scalars().first()
    if existing is not None:
        return await _load_result(db, existing)
    library = await _load_library(db, command.library_id, lock=False)
    taxonomy, _, _ = await _lock_taxonomy_and_enabled_labels(
        db,
        library=library,
        expected_taxonomy_id=command.taxonomy_version_id,
        expected_enabled_label_ids=command.enabled_label_ids,
    )
    highest_generation = (
        await db.execute(
            select(func.max(DocumentClassificationRun.generation_no)).where(
                DocumentClassificationRun.document_revision_id == revision.id
            )
        )
    ).scalar_one()
    run = DocumentClassificationRun(
        id=uuid.uuid4(),
        library_id=document.library_id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy.id,
        enabled_label_set_hash=identity.enabled_label_set_hash,
        policy_version=CLASSIFICATION_POLICY_VERSION,
        min_confidence_micros=CLASSIFICATION_MIN_CONFIDENCE_MICROS,
        min_margin_micros=CLASSIFICATION_MIN_MARGIN_MICROS,
        max_secondary_labels=CLASSIFICATION_MAX_SECONDARY_LABELS,
        classifier_version=command.classifier_version,
        model_provider=command.model_provider,
        model_name=command.model_name,
        model_config_hash=command.model_config_hash,
        prompt_version=command.prompt_version,
        input_fingerprint=identity.input_fingerprint,
        idempotency_key=identity.idempotency_key,
        generation_no=int(highest_generation or 0) + 1,
        retry_generation=command.retry_generation,
        trigger_type=command.trigger_type,
        status="failed",
        reason_codes=[],
        requested_by_user_id=command.requested_by_user_id,
        error_code=command.error_code,
        finished_at=_now(),
    )
    db.add(run)
    await db.flush()
    await audit_log.record(
        db,
        command.requested_by_user_id,
        "classification.run_failed",
        {
            "library_id": str(document.library_id),
            "document_id": str(document.id),
            "revision_id": str(revision.id),
            "run_id": str(run.id),
            "error_code": run.error_code,
        },
    )
    return ClassificationRunResult(run, (), None, ())


def _selection_from_proposals(
    proposals: tuple[DocumentClassificationProposal, ...],
) -> ManualClassificationSelection:
    primaries = sorted(
        (item for item in proposals if item.role == "primary"),
        key=lambda item: item.rank,
    )
    if not primaries or primaries[0].label_id is None:
        raise ClassificationDecisionError("classification_review_selection_invalid")
    secondaries = tuple(
        item.label_id
        for item in sorted(
            (item for item in proposals if item.role == "secondary"),
            key=lambda item: item.rank,
        )
        if item.label_id is not None
    )
    if any(item.label_id is None for item in proposals if item.role == "secondary"):
        raise ClassificationDecisionError("classification_review_selection_invalid")
    return ManualClassificationSelection(
        primary_label_id=primaries[0].label_id,
        secondary_label_ids=secondaries,
    )


def _validate_selection_enabled(
    selection: ManualClassificationSelection,
    enabled: tuple[ClassificationLabel, ...],
) -> None:
    enabled_ids = {label.id for label in enabled}
    selected_ids = {selection.primary_label_id, *selection.secondary_label_ids}
    if not selected_ids.issubset(enabled_ids):
        raise ClassificationDecisionError("classification_label_selection_invalid")


async def review_classification_run(
    db,
    command: ReviewClassificationRunCommand,
) -> ClassificationRunResult:
    library = await _require_library_management(
        db,
        library_id=command.library_id,
        actor_user_id=command.actor_user_id,
    )
    initial = (
        await db.execute(
            select(DocumentClassificationRun).where(
                DocumentClassificationRun.id == command.run_id,
                DocumentClassificationRun.library_id == command.library_id,
            )
        )
    ).scalars().first()
    if initial is None:
        raise ClassificationDecisionError("classification_run_not_found")
    document, revision = await _lock_current_document(
        db,
        library_id=library.id,
        document_id=initial.document_id,
        revision_id=initial.document_revision_id,
        content_hash=initial.revision_content_hash,
    )
    run = (
        await db.execute(
            select(DocumentClassificationRun)
            .where(
                DocumentClassificationRun.id == command.run_id,
                DocumentClassificationRun.library_id == library.id,
                DocumentClassificationRun.document_revision_id == revision.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if run is None:
        raise ClassificationDecisionError("classification_run_not_found")
    if run.status != command.expected_run_status:
        raise ClassificationDecisionError("classification_run_state_changed")
    taxonomy, enabled, _ = await _lock_taxonomy_and_enabled_labels(
        db,
        library=library,
        expected_taxonomy_id=run.taxonomy_version_id,
    )
    await _require_library_management(
        db,
        library_id=library.id,
        actor_user_id=command.actor_user_id,
    )
    current = await _effective_set(db, revision.id, lock=True)
    _fence_effective_set(current, command.expected_effective_decision_set_id)
    proposals = tuple(
        (
            await db.execute(
                select(DocumentClassificationProposal)
                .where(DocumentClassificationProposal.run_id == run.id)
                .order_by(
                    DocumentClassificationProposal.role,
                    DocumentClassificationProposal.rank,
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    now = _now()
    if command.action == "reject":
        run.status = "rejected"
        run.updated_at = now
        for proposal in proposals:
            proposal.status = "rejected"
            proposal.reviewed_by_user_id = command.actor_user_id
            proposal.reviewed_at = now
            proposal.updated_at = now
        await audit_log.record(
            db,
            command.actor_user_id,
            "classification.review_reject",
            {
                "library_id": str(library.id),
                "document_id": str(document.id),
                "revision_id": str(revision.id),
                "run_id": str(run.id),
                "proposal_count": len(proposals),
            },
        )
        return ClassificationRunResult(run, proposals, None, ())
    selection = (
        _selection_from_proposals(proposals)
        if command.action == "accept"
        else command.selection
    )
    if selection is None:
        raise ClassificationDecisionError("classification_review_selection_invalid")
    _validate_selection_enabled(selection, enabled)
    decision_set, decisions = await _replace_decision_set(
        db,
        document=document,
        revision=revision,
        taxonomy=taxonomy,
        source="manual",
        source_run=run,
        actor_user_id=command.actor_user_id,
        primary=(selection.primary_label_id, None),
        secondaries=tuple((label_id, None) for label_id in selection.secondary_label_ids),
        current=current,
    )
    selected_ids = {selection.primary_label_id, *selection.secondary_label_ids}
    for proposal in proposals:
        proposal.status = "accepted" if proposal.label_id in selected_ids else "rejected"
        proposal.reviewed_by_user_id = command.actor_user_id
        proposal.reviewed_at = now
        proposal.updated_at = now
    run.status = "manual_applied"
    run.updated_at = now
    await audit_log.record(
        db,
        command.actor_user_id,
        "classification.review_apply",
        {
            "library_id": str(library.id),
            "document_id": str(document.id),
            "revision_id": str(revision.id),
            "run_id": str(run.id),
            "decision_set_id": str(decision_set.id),
            "decision_count": len(decisions),
        },
    )
    return ClassificationRunResult(run, proposals, decision_set, decisions)


async def set_manual_classification(
    db,
    command: SetManualClassificationCommand,
) -> tuple[DocumentClassificationDecisionSet, tuple[DocumentClassificationDecision, ...]]:
    library = await _require_library_management(
        db,
        library_id=command.library_id,
        actor_user_id=command.actor_user_id,
    )
    document, revision = await _lock_current_document(
        db,
        library_id=library.id,
        document_id=command.document_id,
    )
    taxonomy, enabled, _ = await _lock_taxonomy_and_enabled_labels(db, library=library)
    await _require_library_management(
        db,
        library_id=library.id,
        actor_user_id=command.actor_user_id,
    )
    _validate_selection_enabled(command.selection, enabled)
    current = await _effective_set(db, revision.id, lock=True)
    _fence_effective_set(current, command.expected_effective_decision_set_id)
    decision_set, decisions = await _replace_decision_set(
        db,
        document=document,
        revision=revision,
        taxonomy=taxonomy,
        source="manual",
        source_run=None,
        actor_user_id=command.actor_user_id,
        primary=(command.selection.primary_label_id, None),
        secondaries=tuple(
            (label_id, None) for label_id in command.selection.secondary_label_ids
        ),
        current=current,
    )
    await audit_log.record(
        db,
        command.actor_user_id,
        "classification.manual_set",
        {
            "library_id": str(library.id),
            "document_id": str(document.id),
            "revision_id": str(revision.id),
            "decision_set_id": str(decision_set.id),
            "decision_count": len(decisions),
        },
    )
    return decision_set, decisions


async def remove_manual_classification(
    db,
    command: RemoveManualClassificationCommand,
) -> DocumentClassificationDecisionSet | None:
    library = await _require_library_management(
        db,
        library_id=command.library_id,
        actor_user_id=command.actor_user_id,
    )
    document, revision = await _lock_current_document(
        db,
        library_id=library.id,
        document_id=command.document_id,
    )
    taxonomy, _, _ = await _lock_taxonomy_and_enabled_labels(db, library=library)
    await _require_library_management(
        db,
        library_id=library.id,
        actor_user_id=command.actor_user_id,
    )
    current = await _effective_set(db, revision.id, lock=True)
    _fence_effective_set(current, command.expected_effective_decision_set_id)
    if current is None:
        return None
    removed, _ = await _replace_decision_set(
        db,
        document=document,
        revision=revision,
        taxonomy=taxonomy,
        source="manual",
        source_run=None,
        actor_user_id=command.actor_user_id,
        primary=None,
        secondaries=(),
        current=current,
        remove=True,
    )
    await audit_log.record(
        db,
        command.actor_user_id,
        "classification.manual_remove",
        {
            "library_id": str(library.id),
            "document_id": str(document.id),
            "revision_id": str(revision.id),
            "removed_decision_set_id": str(current.id),
            "marker_decision_set_id": str(removed.id),
        },
    )
    return removed


async def list_classification_review_runs(
    db,
    *,
    library_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    statuses: tuple[str, ...] = ("pending_review", "blocked_manual"),
    limit: int = 50,
    offset: int = 0,
) -> ClassificationReviewPage:
    await _require_library_management(
        db,
        library_id=library_id,
        actor_user_id=actor_user_id,
    )
    allowed = {"pending_review", "blocked_manual"}
    if (
        not statuses
        or len(set(statuses)) != len(statuses)
        or any(status not in allowed for status in statuses)
        or isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= 100
        or isinstance(offset, bool)
        or not isinstance(offset, int)
        or offset < 0
    ):
        raise ClassificationDecisionError("classification_review_query_invalid")
    filters = (
        DocumentClassificationRun.library_id == library_id,
        DocumentClassificationRun.status.in_(statuses),
        Document.current_revision_id == DocumentClassificationRun.document_revision_id,
        Document.deleted_at.is_(None),
    )
    total = int(
        (
            await db.execute(
                select(func.count(DocumentClassificationRun.id))
                .join(Document, Document.id == DocumentClassificationRun.document_id)
                .where(*filters)
            )
        ).scalar_one()
    )
    runs = tuple(
        (
            await db.execute(
                select(DocumentClassificationRun)
                .join(Document, Document.id == DocumentClassificationRun.document_id)
                .where(*filters)
                .order_by(
                    DocumentClassificationRun.created_at,
                    DocumentClassificationRun.id,
                )
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    if not runs:
        return ClassificationReviewPage((), total)
    proposals = tuple(
        (
            await db.execute(
                select(DocumentClassificationProposal, ClassificationLabel)
                .outerjoin(
                    ClassificationLabel,
                    ClassificationLabel.id
                    == DocumentClassificationProposal.label_id,
                )
                .where(
                    DocumentClassificationProposal.run_id.in_(
                        tuple(run.id for run in runs)
                    )
                )
                .order_by(
                    DocumentClassificationProposal.run_id,
                    DocumentClassificationProposal.role,
                    DocumentClassificationProposal.rank,
                )
            )
        ).all()
    )
    by_run: dict[
        uuid.UUID,
        list[tuple[DocumentClassificationProposal, ClassificationLabel | None]],
    ] = {
        run.id: [] for run in runs
    }
    for proposal, label in proposals:
        by_run[proposal.run_id].append((proposal, label))
    return ClassificationReviewPage(
        tuple(
            ClassificationReviewItem(run, tuple(by_run[run.id])) for run in runs
        ),
        total,
    )


async def get_effective_classification(
    db,
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
) -> EffectiveClassification:
    document = (
        await db.execute(
            select(Document).where(
                Document.id == document_id,
                Document.library_id == library_id,
                Document.deleted_at.is_(None),
            )
        )
    ).scalars().first()
    if document is None or document.current_revision_id is None:
        raise ClassificationDecisionError("classification_document_not_found")
    revision = (
        await db.execute(
            select(DocumentRevision).where(
                DocumentRevision.id == document.current_revision_id,
                DocumentRevision.document_id == document.id,
                DocumentRevision.library_id == library_id,
            )
        )
    ).scalars().first()
    if revision is None:
        raise ClassificationDecisionError("classification_revision_unavailable")
    decision_set = await _effective_set(db, revision.id, lock=False)
    latest_decision_set = decision_set
    if latest_decision_set is None:
        latest_decision_set = await _latest_decision_set(db, revision.id, lock=False)
    decisions: tuple[tuple[DocumentClassificationDecision, ClassificationLabel], ...] = ()
    if decision_set is not None:
        decisions = tuple(
            (
                await db.execute(
                    select(DocumentClassificationDecision, ClassificationLabel)
                    .join(
                        ClassificationLabel,
                        ClassificationLabel.id == DocumentClassificationDecision.label_id,
                    )
                    .where(
                        DocumentClassificationDecision.decision_set_id == decision_set.id,
                        ClassificationLabel.taxonomy_version_id
                        == decision_set.taxonomy_version_id,
                    )
                    .order_by(DocumentClassificationDecision.ordinal)
                )
            ).all()
        )
    latest_run = (
        await db.execute(
            select(DocumentClassificationRun)
            .where(
                DocumentClassificationRun.library_id == library_id,
                DocumentClassificationRun.document_id == document.id,
                DocumentClassificationRun.document_revision_id == revision.id,
            )
            .order_by(DocumentClassificationRun.generation_no.desc())
            .limit(1)
        )
    ).scalars().first()
    if decision_set is not None:
        state = "classified"
    elif (
        latest_decision_set is not None
        and latest_decision_set.source == "manual"
        and latest_decision_set.lifecycle == "removed"
        and (
            latest_run is None
            or latest_decision_set.created_at >= latest_run.created_at
        )
    ):
        state = "unclassified"
    elif latest_run is not None and latest_run.status in {
        "pending_review",
        "blocked_manual",
    }:
        state = "pending_review"
    elif latest_run is not None and latest_run.status == "failed":
        state = "failed"
    else:
        state = "unclassified"
    return EffectiveClassification(
        document=document,
        revision=revision,
        state=state,
        decision_set=decision_set,
        decisions=decisions,
        latest_run=latest_run,
    )
