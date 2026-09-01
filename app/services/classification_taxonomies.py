from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from app.models.classification_taxonomy import (
    ClassificationLabel,
    ClassificationTaxonomy,
    LibraryClassificationLabel,
)
from app.models.library import Library
from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.services import audit_log
from app.services.classification_taxonomy_contracts import (
    MAX_LABELS_PER_TAXONOMY,
    ActivateTaxonomyCommand,
    ClassificationTaxonomyError,
    CopyTaxonomyVersionCommand,
    CreateClassificationLabelCommand,
    CreateTaxonomyCommand,
    ReplaceLibraryClassificationLabelsCommand,
    UpdateClassificationLabelCommand,
    UpdateTaxonomyDraftCommand,
    validate_version_no,
)


@dataclass(frozen=True, slots=True)
class LibraryClassificationSelection:
    library: Library
    taxonomy: ClassificationTaxonomy
    labels: tuple[ClassificationLabel, ...]


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _require_admin_scope(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    lock: bool,
) -> Organization:
    organization_statement = select(Organization).where(
        Organization.id == organization_id,
        Organization.status == "active",
    )
    if lock:
        organization_statement = organization_statement.with_for_update()
    organization = (await db.execute(organization_statement)).scalars().first()
    if organization is None:
        raise ClassificationTaxonomyError("classification_scope_not_found")
    membership_statement = select(OrganizationMembership.id).where(
        OrganizationMembership.organization_id == organization_id,
        OrganizationMembership.user_id == actor_user_id,
        OrganizationMembership.role == "organization_admin",
        OrganizationMembership.status == "active",
    )
    if lock:
        membership_statement = membership_statement.with_for_update()
    membership_id = (await db.execute(membership_statement)).scalar_one_or_none()
    if membership_id is None:
        raise ClassificationTaxonomyError("classification_admin_forbidden")
    return organization


async def lock_classification_admin_scope(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    lock: bool,
) -> Organization:
    return await _require_admin_scope(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        lock=lock,
    )


async def _load_taxonomy(
    db,
    *,
    organization_id: uuid.UUID,
    taxonomy_id: uuid.UUID,
    lock: bool = False,
) -> ClassificationTaxonomy:
    statement = select(ClassificationTaxonomy).where(
        ClassificationTaxonomy.id == taxonomy_id,
        ClassificationTaxonomy.organization_id == organization_id,
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    taxonomy = (await db.execute(statement)).scalars().first()
    if taxonomy is None:
        raise ClassificationTaxonomyError("classification_taxonomy_not_found")
    return taxonomy


async def _load_label(
    db,
    *,
    taxonomy_id: uuid.UUID,
    label_id: uuid.UUID,
    lock: bool = False,
) -> ClassificationLabel:
    statement = select(ClassificationLabel).where(
        ClassificationLabel.id == label_id,
        ClassificationLabel.taxonomy_version_id == taxonomy_id,
    )
    if lock:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    label = (await db.execute(statement)).scalars().first()
    if label is None:
        raise ClassificationTaxonomyError("classification_label_not_found")
    return label


def _require_taxonomy_fence(
    taxonomy: ClassificationTaxonomy,
    *,
    expected_status: str,
    expected_updated_at: datetime,
) -> None:
    if taxonomy.status != expected_status or taxonomy.updated_at != expected_updated_at:
        raise ClassificationTaxonomyError("classification_taxonomy_state_changed")


def _require_draft(taxonomy: ClassificationTaxonomy) -> None:
    if taxonomy.status != "draft":
        raise ClassificationTaxonomyError("classification_taxonomy_immutable")


def _validate_label_graph(
    labels: tuple[ClassificationLabel, ...],
    *,
    require_active: bool,
) -> None:
    if len(labels) > MAX_LABELS_PER_TAXONOMY:
        raise ClassificationTaxonomyError("classification_label_limit_exceeded")
    by_id = {label.id: label for label in labels}
    if len(by_id) != len(labels) or len({label.key for label in labels}) != len(labels):
        raise ClassificationTaxonomyError("classification_label_conflict")
    if require_active and not any(label.status == "active" for label in labels):
        raise ClassificationTaxonomyError("classification_taxonomy_empty")
    for label in labels:
        parent_id = label.parent_label_id
        if parent_id is None:
            continue
        parent = by_id.get(parent_id)
        if parent is None:
            raise ClassificationTaxonomyError("classification_parent_scope_invalid")
        if require_active and label.status == "active" and parent.status != "active":
            raise ClassificationTaxonomyError("classification_parent_disabled")
    for label in labels:
        seen: set[uuid.UUID] = set()
        current = label
        while current.parent_label_id is not None:
            if current.id in seen:
                raise ClassificationTaxonomyError("classification_parent_cycle")
            seen.add(current.id)
            parent = by_id.get(current.parent_label_id)
            if parent is None:
                raise ClassificationTaxonomyError("classification_parent_scope_invalid")
            current = parent


async def list_taxonomies(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
) -> tuple[ClassificationTaxonomy, ...]:
    await _require_admin_scope(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        lock=False,
    )
    return tuple(
        (
            await db.execute(
                select(ClassificationTaxonomy)
                .where(ClassificationTaxonomy.organization_id == organization_id)
                .order_by(
                    ClassificationTaxonomy.version_key,
                    ClassificationTaxonomy.version_no.desc(),
                    ClassificationTaxonomy.id,
                )
            )
        )
        .scalars()
        .all()
    )


async def list_classification_labels(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    taxonomy_id: uuid.UUID,
) -> tuple[ClassificationLabel, ...]:
    await _require_admin_scope(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        lock=False,
    )
    await _load_taxonomy(
        db,
        organization_id=organization_id,
        taxonomy_id=taxonomy_id,
    )
    return tuple(
        (
            await db.execute(
                select(ClassificationLabel)
                .where(ClassificationLabel.taxonomy_version_id == taxonomy_id)
                .order_by(
                    ClassificationLabel.sort_order,
                    ClassificationLabel.key,
                    ClassificationLabel.id,
                )
            )
        )
        .scalars()
        .all()
    )


async def create_taxonomy(
    db,
    command: CreateTaxonomyCommand,
) -> ClassificationTaxonomy:
    await _require_admin_scope(
        db,
        organization_id=command.organization_id,
        actor_user_id=command.actor_user_id,
        lock=True,
    )
    existing = (
        await db.execute(
            select(ClassificationTaxonomy.id).where(
                ClassificationTaxonomy.organization_id == command.organization_id,
                ClassificationTaxonomy.version_key == command.version_key,
                ClassificationTaxonomy.version_no == command.version_no,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ClassificationTaxonomyError("classification_taxonomy_conflict")
    taxonomy = ClassificationTaxonomy(
        id=uuid.uuid4(),
        organization_id=command.organization_id,
        version_key=command.version_key,
        version_no=command.version_no,
        status="draft",
        description=command.description,
        created_by_user_id=command.actor_user_id,
    )
    db.add(taxonomy)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ClassificationTaxonomyError("classification_taxonomy_conflict") from exc
    await audit_log.record(
        db,
        command.actor_user_id,
        "classification.taxonomy_create",
        {
            "organization_id": str(command.organization_id),
            "taxonomy_id": str(taxonomy.id),
            "version_no": taxonomy.version_no,
            "status": taxonomy.status,
        },
    )
    return taxonomy


async def update_taxonomy_draft(
    db,
    command: UpdateTaxonomyDraftCommand,
) -> ClassificationTaxonomy:
    await _require_admin_scope(
        db,
        organization_id=command.organization_id,
        actor_user_id=command.actor_user_id,
        lock=True,
    )
    taxonomy = await _load_taxonomy(
        db,
        organization_id=command.organization_id,
        taxonomy_id=command.taxonomy_id,
        lock=True,
    )
    _require_taxonomy_fence(
        taxonomy,
        expected_status=command.expected_status,
        expected_updated_at=command.expected_updated_at,
    )
    _require_draft(taxonomy)
    if taxonomy.description == command.description:
        return taxonomy
    taxonomy.description = command.description
    taxonomy.updated_at = _now()
    await audit_log.record(
        db,
        command.actor_user_id,
        "classification.taxonomy_update",
        {
            "organization_id": str(command.organization_id),
            "taxonomy_id": str(taxonomy.id),
            "description_changed": True,
        },
    )
    return taxonomy


async def create_classification_label(
    db,
    command: CreateClassificationLabelCommand,
) -> ClassificationLabel:
    await _require_admin_scope(
        db,
        organization_id=command.organization_id,
        actor_user_id=command.actor_user_id,
        lock=True,
    )
    taxonomy = await _load_taxonomy(
        db,
        organization_id=command.organization_id,
        taxonomy_id=command.taxonomy_id,
        lock=True,
    )
    _require_draft(taxonomy)
    if taxonomy.updated_at != command.expected_taxonomy_updated_at:
        raise ClassificationTaxonomyError("classification_taxonomy_state_changed")
    count = (
        await db.execute(
            select(func.count(ClassificationLabel.id)).where(
                ClassificationLabel.taxonomy_version_id == taxonomy.id
            )
        )
    ).scalar_one()
    if count >= MAX_LABELS_PER_TAXONOMY:
        raise ClassificationTaxonomyError("classification_label_limit_exceeded")
    conflict = (
        await db.execute(
            select(ClassificationLabel.id).where(
                ClassificationLabel.taxonomy_version_id == taxonomy.id,
                ClassificationLabel.key == command.key,
            )
        )
    ).scalar_one_or_none()
    if conflict is not None:
        raise ClassificationTaxonomyError("classification_label_conflict")
    if command.parent_label_id is not None:
        await _load_label(
            db,
            taxonomy_id=taxonomy.id,
            label_id=command.parent_label_id,
            lock=True,
        )
    label = ClassificationLabel(
        id=uuid.uuid4(),
        taxonomy_version_id=taxonomy.id,
        key=command.key,
        label=command.label,
        description=command.description,
        parent_label_id=command.parent_label_id,
        sort_order=command.sort_order,
        status=command.status,
    )
    db.add(label)
    taxonomy.updated_at = _now()
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ClassificationTaxonomyError("classification_label_conflict") from exc
    await audit_log.record(
        db,
        command.actor_user_id,
        "classification.label_create",
        {
            "organization_id": str(command.organization_id),
            "taxonomy_id": str(taxonomy.id),
            "label_id": str(label.id),
            "status": label.status,
        },
    )
    return label


async def update_classification_label(
    db,
    command: UpdateClassificationLabelCommand,
) -> ClassificationLabel:
    await _require_admin_scope(
        db,
        organization_id=command.organization_id,
        actor_user_id=command.actor_user_id,
        lock=True,
    )
    taxonomy = await _load_taxonomy(
        db,
        organization_id=command.organization_id,
        taxonomy_id=command.taxonomy_id,
        lock=True,
    )
    _require_draft(taxonomy)
    if taxonomy.updated_at != command.expected_taxonomy_updated_at:
        raise ClassificationTaxonomyError("classification_taxonomy_state_changed")
    label = await _load_label(
        db,
        taxonomy_id=taxonomy.id,
        label_id=command.label_id,
        lock=True,
    )
    if label.updated_at != command.expected_updated_at:
        raise ClassificationTaxonomyError("classification_label_state_changed")
    if command.parent_label_id == label.id:
        raise ClassificationTaxonomyError("classification_parent_cycle")
    labels = tuple(
        (
            await db.execute(
                select(ClassificationLabel)
                .where(ClassificationLabel.taxonomy_version_id == taxonomy.id)
                .order_by(ClassificationLabel.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    conflict = next(
        (
            item
            for item in labels
            if item.id != label.id and item.key == command.key
        ),
        None,
    )
    if conflict is not None:
        raise ClassificationTaxonomyError("classification_label_conflict")
    if command.parent_label_id is not None and all(
        item.id != command.parent_label_id for item in labels
    ):
        raise ClassificationTaxonomyError("classification_parent_scope_invalid")
    changed = any(
        (
            label.key != command.key,
            label.label != command.label,
            label.description != command.description,
            label.parent_label_id != command.parent_label_id,
            label.sort_order != command.sort_order,
            label.status != command.status,
        )
    )
    if not changed:
        return label
    label.key = command.key
    label.label = command.label
    label.description = command.description
    label.parent_label_id = command.parent_label_id
    label.sort_order = command.sort_order
    label.status = command.status
    _validate_label_graph(labels, require_active=False)
    now = _now()
    label.updated_at = now
    taxonomy.updated_at = now
    await audit_log.record(
        db,
        command.actor_user_id,
        "classification.label_update",
        {
            "organization_id": str(command.organization_id),
            "taxonomy_id": str(taxonomy.id),
            "label_id": str(label.id),
            "status": label.status,
        },
    )
    return label


async def copy_taxonomy_version(
    db,
    command: CopyTaxonomyVersionCommand,
) -> ClassificationTaxonomy:
    await _require_admin_scope(
        db,
        organization_id=command.organization_id,
        actor_user_id=command.actor_user_id,
        lock=True,
    )
    source = await _load_taxonomy(
        db,
        organization_id=command.organization_id,
        taxonomy_id=command.source_taxonomy_id,
        lock=True,
    )
    _require_taxonomy_fence(
        source,
        expected_status=command.expected_source_status,
        expected_updated_at=command.expected_source_updated_at,
    )
    if source.status == "draft":
        raise ClassificationTaxonomyError("classification_source_not_published")
    source_labels = tuple(
        (
            await db.execute(
                select(ClassificationLabel)
                .where(ClassificationLabel.taxonomy_version_id == source.id)
                .order_by(ClassificationLabel.sort_order, ClassificationLabel.key)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    highest = (
        await db.execute(
            select(func.max(ClassificationTaxonomy.version_no)).where(
                ClassificationTaxonomy.organization_id == command.organization_id,
                ClassificationTaxonomy.version_key == source.version_key,
            )
        )
    ).scalar_one()
    next_version_no = int(highest or 0) + 1
    validate_version_no(next_version_no)
    taxonomy = ClassificationTaxonomy(
        id=uuid.uuid4(),
        organization_id=command.organization_id,
        version_key=source.version_key,
        version_no=next_version_no,
        status="draft",
        parent_version_id=source.id,
        description=source.description if command.description is None else command.description,
        created_by_user_id=command.actor_user_id,
    )
    db.add(taxonomy)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise ClassificationTaxonomyError("classification_taxonomy_conflict") from exc
    remapped = {label.id: uuid.uuid4() for label in source_labels}
    copies = tuple(
        ClassificationLabel(
            id=remapped[label.id],
            taxonomy_version_id=taxonomy.id,
            key=label.key,
            label=label.label,
            description=label.description,
            parent_label_id=None,
            sort_order=label.sort_order,
            status=label.status,
        )
        for label in source_labels
    )
    db.add_all(copies)
    try:
        await db.flush()
        copied_by_id = {copy.id: copy for copy in copies}
        for source_label in source_labels:
            if source_label.parent_label_id is not None:
                copied_by_id[remapped[source_label.id]].parent_label_id = remapped[
                    source_label.parent_label_id
                ]
        await db.flush()
    except IntegrityError as exc:
        raise ClassificationTaxonomyError("classification_taxonomy_conflict") from exc
    await audit_log.record(
        db,
        command.actor_user_id,
        "classification.taxonomy_copy",
        {
            "organization_id": str(command.organization_id),
            "source_taxonomy_id": str(source.id),
            "taxonomy_id": str(taxonomy.id),
            "version_no": taxonomy.version_no,
            "label_count": len(copies),
        },
    )
    return taxonomy


async def activate_taxonomy(
    db,
    command: ActivateTaxonomyCommand,
) -> ClassificationTaxonomy:
    await _require_admin_scope(
        db,
        organization_id=command.organization_id,
        actor_user_id=command.actor_user_id,
        lock=True,
    )
    taxonomy = await _load_taxonomy(
        db,
        organization_id=command.organization_id,
        taxonomy_id=command.taxonomy_id,
        lock=True,
    )
    _require_taxonomy_fence(
        taxonomy,
        expected_status=command.expected_status,
        expected_updated_at=command.expected_updated_at,
    )
    _require_draft(taxonomy)
    labels = tuple(
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
    _validate_label_graph(labels, require_active=True)
    prior = (
        await db.execute(
            select(ClassificationTaxonomy)
            .where(
                ClassificationTaxonomy.organization_id == command.organization_id,
                ClassificationTaxonomy.status == "active",
                ClassificationTaxonomy.id != taxonomy.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    now = _now()
    if prior is not None:
        prior.status = "disabled"
        prior.updated_at = now
        await db.flush()
    taxonomy.status = "active"
    taxonomy.activated_by_user_id = command.actor_user_id
    taxonomy.activated_at = now
    taxonomy.updated_at = now
    library_ids = select(Library.id).where(
        Library.organization_id == command.organization_id
    )
    await db.execute(
        delete(LibraryClassificationLabel).where(
            LibraryClassificationLabel.library_id.in_(library_ids)
        )
    )
    from app.services.classification_jobs import (
        cancel_organization_classification_jobs_for_taxonomy_change,
    )

    cancelled_jobs = await cancel_organization_classification_jobs_for_taxonomy_change(
        db,
        organization_id=command.organization_id,
        now=now,
    )
    await audit_log.record(
        db,
        command.actor_user_id,
        "classification.taxonomy_activate",
        {
            "organization_id": str(command.organization_id),
            "taxonomy_id": str(taxonomy.id),
            "prior_taxonomy_id": str(prior.id) if prior is not None else None,
            "active_label_count": sum(label.status == "active" for label in labels),
            "cancelled_classification_jobs": cancelled_jobs,
        },
    )
    return taxonomy


async def get_library_classification_selection(
    db,
    *,
    organization_id: uuid.UUID,
    actor_user_id: uuid.UUID,
    library_id: uuid.UUID,
) -> LibraryClassificationSelection:
    await _require_admin_scope(
        db,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        lock=False,
    )
    library = (
        await db.execute(
            select(Library).where(
                Library.id == library_id,
                Library.organization_id == organization_id,
                Library.deleted_at.is_(None),
            )
        )
    ).scalars().first()
    if library is None:
        raise ClassificationTaxonomyError("classification_library_not_found")
    taxonomy = (
        await db.execute(
            select(ClassificationTaxonomy).where(
                ClassificationTaxonomy.organization_id == organization_id,
                ClassificationTaxonomy.status == "active",
            )
        )
    ).scalars().first()
    if taxonomy is None:
        raise ClassificationTaxonomyError("classification_active_taxonomy_not_found")
    labels = tuple(
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
                )
                .order_by(LibraryClassificationLabel.ordinal)
            )
        )
        .scalars()
        .all()
    )
    return LibraryClassificationSelection(library, taxonomy, labels)


async def replace_library_classification_labels(
    db,
    command: ReplaceLibraryClassificationLabelsCommand,
) -> LibraryClassificationSelection:
    await _require_admin_scope(
        db,
        organization_id=command.organization_id,
        actor_user_id=command.actor_user_id,
        lock=True,
    )
    library = (
        await db.execute(
            select(Library)
            .where(
                Library.id == command.library_id,
                Library.organization_id == command.organization_id,
                Library.deleted_at.is_(None),
            )
            .with_for_update()
        )
    ).scalars().first()
    if library is None:
        raise ClassificationTaxonomyError("classification_library_not_found")
    taxonomy = (
        await db.execute(
            select(ClassificationTaxonomy)
            .where(
                ClassificationTaxonomy.id == command.taxonomy_id,
                ClassificationTaxonomy.organization_id == command.organization_id,
                ClassificationTaxonomy.status == "active",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalars().first()
    if taxonomy is None:
        raise ClassificationTaxonomyError("classification_active_taxonomy_not_found")
    if taxonomy.updated_at != command.expected_taxonomy_updated_at:
        raise ClassificationTaxonomyError("classification_taxonomy_state_changed")
    selected = tuple(
        (
            await db.execute(
                select(ClassificationLabel)
                .where(
                    ClassificationLabel.taxonomy_version_id == taxonomy.id,
                    ClassificationLabel.id.in_(command.label_ids),
                    ClassificationLabel.status == "active",
                )
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    by_id = {label.id: label for label in selected}
    if len(by_id) != len(command.label_ids):
        raise ClassificationTaxonomyError("classification_label_selection_invalid")
    labels = tuple(by_id[label_id] for label_id in command.label_ids)
    current = tuple(
        (
            await db.execute(
                select(LibraryClassificationLabel)
                .where(LibraryClassificationLabel.library_id == library.id)
                .order_by(LibraryClassificationLabel.ordinal)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    current_ids = tuple(binding.label_id for binding in current)
    if current_ids != command.expected_label_ids:
        raise ClassificationTaxonomyError("classification_library_selection_state_changed")
    if current_ids == command.label_ids:
        return LibraryClassificationSelection(library, taxonomy, labels)
    await db.execute(
        delete(LibraryClassificationLabel).where(
            LibraryClassificationLabel.library_id == library.id
        )
    )
    db.add_all(
        LibraryClassificationLabel(
            id=uuid.uuid4(),
            library_id=library.id,
            taxonomy_version_id=taxonomy.id,
            label_id=label.id,
            ordinal=ordinal,
        )
        for ordinal, label in enumerate(labels)
    )
    from app.services.classification_jobs import (
        cancel_library_classification_jobs_for_policy_change,
    )

    cancelled_jobs = await cancel_library_classification_jobs_for_policy_change(
        db,
        library_id=library.id,
        error_code="library_classification_labels_changed",
    )
    await audit_log.record(
        db,
        command.actor_user_id,
        "classification.library_labels_replace",
        {
            "organization_id": str(command.organization_id),
            "library_id": str(library.id),
            "taxonomy_id": str(taxonomy.id),
            "previous_count": len(current_ids),
            "label_count": len(labels),
            "cancelled_classification_jobs": cancelled_jobs,
        },
    )
    return LibraryClassificationSelection(library, taxonomy, labels)
