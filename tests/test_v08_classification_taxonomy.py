from __future__ import annotations

import asyncio
import io
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from app.config import Settings, settings, validate_classification_taxonomy_startup
from app.models.classification_taxonomy import (
    ClassificationLabel,
    ClassificationTaxonomy,
    LibraryClassificationLabel,
)
from app.models.library import Library
from app.models.organization import Organization
from app.models.user import User
from app.services import classification_taxonomies as service
from app.services import classification_jobs


NOW = datetime(2026, 7, 22, 16, 0, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows=(), scalar=None):
        self.rows = list(rows)
        self.scalar = scalar

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.rows[0] if self.rows else None

    def scalar_one(self):
        return self.scalar

    def scalar_one_or_none(self):
        return self.scalar


class _DB:
    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.commit = AsyncMock()
        self.flush_count = 0

    async def execute(self, _statement):
        assert self.results, "unexpected query"
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    def add_all(self, values):
        self.added.extend(values)

    async def flush(self):
        self.flush_count += 1
        return None


def _offline(command_name: str, revision: str) -> str:
    output = io.StringIO()
    config = Config("alembic.ini", output_buffer=output)
    if command_name == "upgrade":
        alembic_command.upgrade(config, revision, sql=True)
    else:
        alembic_command.downgrade(config, revision, sql=True)
    return " ".join(output.getvalue().lower().split())


def _organization() -> Organization:
    return Organization(
        id=uuid.uuid4(),
        slug="example",
        name="Example",
        deployment_profile="hosted",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _user() -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        hashed_password="hash",
        is_active=True,
        is_superuser=False,
        is_verified=True,
        created_at=NOW,
    )


def _library(organization: Organization) -> Library:
    return Library(
        id=uuid.uuid4(),
        organization_id=organization.id,
        slug="legal",
        name="Legal",
        embedding_model="bge-m3",
        embedding_dim=1024,
        qdrant_collection="legal",
        index_state="ready",
        created_at=NOW,
    )


def _taxonomy(
    organization: Organization,
    *,
    status: str = "draft",
    version_no: int = 1,
) -> ClassificationTaxonomy:
    return ClassificationTaxonomy(
        id=uuid.uuid4(),
        organization_id=organization.id,
        version_key="document-category",
        version_no=version_no,
        status=status,
        description="Categories",
        created_by_user_id=uuid.uuid4(),
        activated_at=NOW if status != "draft" else None,
        created_at=NOW,
        updated_at=NOW,
    )


def _label(
    taxonomy: ClassificationTaxonomy,
    key: str,
    *,
    parent_id: uuid.UUID | None = None,
    status: str = "active",
    sort_order: int = 0,
) -> ClassificationLabel:
    return ClassificationLabel(
        id=uuid.uuid4(),
        taxonomy_version_id=taxonomy.id,
        key=key,
        label=key.title(),
        parent_label_id=parent_id,
        status=status,
        sort_order=sort_order,
        created_at=NOW,
        updated_at=NOW,
    )


def test_0034_orm_migration_and_offline_sql_are_reversible():
    assert ClassificationTaxonomy.__table__.columns.version_key.type.length == 64
    assert ClassificationLabel.__table__.columns.label.type.length == 160
    assert LibraryClassificationLabel.__table__.columns.ordinal.nullable is False
    taxonomy_checks = {
        item.name
        for item in ClassificationTaxonomy.__table__.constraints
        if isinstance(item, CheckConstraint)
    }
    label_checks = {
        item.name
        for item in ClassificationLabel.__table__.constraints
        if isinstance(item, CheckConstraint)
    }
    assert "ck_classification_taxonomies_activation_shape" in taxonomy_checks
    assert "ck_classification_labels_parent_not_self" in label_checks
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0052"]
    assert script.get_revision("0034").down_revision == "0033"
    upgrade = _offline("upgrade", "0033:0034")
    downgrade = _offline("downgrade", "0034:0033")
    assert "create table classification_taxonomies" in upgrade
    assert "create table classification_labels" in upgrade
    assert "create table library_classification_labels" in upgrade
    assert "where status = 'active'" in upgrade
    assert downgrade.index("drop table library_classification_labels") < downgrade.index(
        "drop table classification_labels"
    )
    assert downgrade.index("drop table classification_labels") < downgrade.index(
        "drop table classification_taxonomies"
    )
    assert "update sys_" not in upgrade + downgrade
    assert "delete from" not in upgrade + downgrade


def test_feature_defaults_off_and_requires_organization_authorization():
    assert settings.classification_taxonomy_enabled is False
    validate_classification_taxonomy_startup(Settings())
    with pytest.raises(RuntimeError, match="Organization authorization"):
        validate_classification_taxonomy_startup(
            Settings(classification_taxonomy_enabled=True)
        )
    validate_classification_taxonomy_startup(
        Settings(
            classification_taxonomy_enabled=True,
            organization_authorization_enabled=True,
        )
    )


def test_commands_normalize_stable_keys_and_reject_bad_selections():
    organization, actor = _organization(), _user()
    command = service.CreateTaxonomyCommand(
        organization_id=organization.id,
        actor_user_id=actor.id,
        version_key="  DOCUMENT-CATEGORY  ",
        version_no=1,
        description="  Default   categories  ",
    )
    assert command.version_key == "document-category"
    assert command.description == "Default categories"
    with pytest.raises(service.ClassificationTaxonomyError) as exc_info:
        service.ReplaceLibraryClassificationLabelsCommand(
            organization_id=organization.id,
            actor_user_id=actor.id,
            library_id=uuid.uuid4(),
            taxonomy_id=uuid.uuid4(),
            expected_taxonomy_updated_at=NOW,
            expected_label_ids=(),
            label_ids=(),
        )
    assert exc_info.value.code == "classification_label_selection_invalid"


def test_graph_validation_rejects_cycles_and_active_child_of_disabled_parent():
    organization = _organization()
    taxonomy = _taxonomy(organization)
    first, second = _label(taxonomy, "first"), _label(taxonomy, "second")
    first.parent_label_id = second.id
    second.parent_label_id = first.id
    with pytest.raises(service.ClassificationTaxonomyError) as cycle:
        service._validate_label_graph((first, second), require_active=True)
    assert cycle.value.code == "classification_parent_cycle"
    first.parent_label_id = None
    first.status = "disabled"
    second.parent_label_id = first.id
    with pytest.raises(service.ClassificationTaxonomyError) as disabled:
        service._validate_label_graph((first, second), require_active=True)
    assert disabled.value.code == "classification_parent_disabled"


def test_create_taxonomy_is_draft_audited_content_free_and_never_commits(monkeypatch):
    organization, actor = _organization(), _user()
    monkeypatch.setattr(service, "_require_admin_scope", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(service.audit_log, "record", audit)
    db = _DB(_Result(scalar=None))
    taxonomy = asyncio.run(
        service.create_taxonomy(
            db,
            service.CreateTaxonomyCommand(
                organization_id=organization.id,
                actor_user_id=actor.id,
                version_key="document-category",
                version_no=1,
                description="Sensitive category semantics",
            ),
        )
    )
    assert taxonomy.status == "draft"
    target = audit.await_args.args[3]
    assert target["taxonomy_id"] == str(taxonomy.id)
    assert "description" not in target and "version_key" not in target
    db.commit.assert_not_awaited()


def test_active_taxonomy_cannot_be_edited_and_service_never_commits(monkeypatch):
    organization, actor = _organization(), _user()
    taxonomy = _taxonomy(organization, status="active")
    monkeypatch.setattr(service, "_require_admin_scope", AsyncMock())
    monkeypatch.setattr(service, "_load_taxonomy", AsyncMock(return_value=taxonomy))
    db = _DB()
    with pytest.raises(service.ClassificationTaxonomyError) as exc_info:
        asyncio.run(
            service.update_taxonomy_draft(
                db,
                service.UpdateTaxonomyDraftCommand(
                    organization_id=organization.id,
                    actor_user_id=actor.id,
                    taxonomy_id=taxonomy.id,
                    expected_status="active",
                    expected_updated_at=taxonomy.updated_at,
                    description="Changed",
                ),
            )
        )
    assert exc_info.value.code == "classification_taxonomy_immutable"
    db.commit.assert_not_awaited()


def test_copy_version_remaps_parent_ids_and_preserves_stable_keys(monkeypatch):
    organization, actor = _organization(), _user()
    source = _taxonomy(organization, status="active")
    parent = _label(source, "finance", sort_order=1)
    child = _label(source, "tax", parent_id=parent.id, sort_order=2)
    monkeypatch.setattr(service, "_require_admin_scope", AsyncMock())
    monkeypatch.setattr(service, "_load_taxonomy", AsyncMock(return_value=source))
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())
    db = _DB(_Result(rows=(parent, child)), _Result(scalar=1))
    copied = asyncio.run(
        service.copy_taxonomy_version(
            db,
            service.CopyTaxonomyVersionCommand(
                organization_id=organization.id,
                actor_user_id=actor.id,
                source_taxonomy_id=source.id,
                expected_source_status="active",
                expected_source_updated_at=source.updated_at,
            ),
        )
    )
    copied_labels = [item for item in db.added if isinstance(item, ClassificationLabel)]
    by_key = {item.key: item for item in copied_labels}
    assert copied.status == "draft" and copied.version_no == 2
    assert copied.parent_version_id == source.id
    assert by_key["tax"].parent_label_id == by_key["finance"].id
    assert by_key["finance"].id != parent.id
    assert db.flush_count == 3
    db.commit.assert_not_awaited()


def test_activation_disables_prior_and_clears_library_bindings(monkeypatch):
    organization, actor = _organization(), _user()
    draft = _taxonomy(organization)
    prior = _taxonomy(organization, status="active", version_no=1)
    draft.version_no = 2
    label = _label(draft, "legal")
    monkeypatch.setattr(service, "_require_admin_scope", AsyncMock())
    monkeypatch.setattr(service, "_load_taxonomy", AsyncMock(return_value=draft))
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())
    cancel_jobs = AsyncMock(return_value=2)
    monkeypatch.setattr(
        classification_jobs,
        "cancel_organization_classification_jobs_for_taxonomy_change",
        cancel_jobs,
    )
    db = _DB(_Result(rows=(label,)), _Result(rows=(prior,)), _Result())
    activated = asyncio.run(
        service.activate_taxonomy(
            db,
            service.ActivateTaxonomyCommand(
                organization_id=organization.id,
                actor_user_id=actor.id,
                taxonomy_id=draft.id,
                expected_status="draft",
                expected_updated_at=draft.updated_at,
            ),
        )
    )
    assert activated.status == "active"
    assert activated.activated_by_user_id == actor.id
    assert prior.status == "disabled"
    assert db.flush_count == 1
    cancel_jobs.assert_awaited_once_with(
        db,
        organization_id=organization.id,
        now=draft.activated_at,
    )
    assert not db.results
    db.commit.assert_not_awaited()


def test_library_subset_is_ordered_fenced_and_idempotent(monkeypatch):
    organization, actor = _organization(), _user()
    library = _library(organization)
    taxonomy = _taxonomy(organization, status="active")
    first = _label(taxonomy, "first")
    second = _label(taxonomy, "second")
    monkeypatch.setattr(service, "_require_admin_scope", AsyncMock())
    audit = AsyncMock()
    monkeypatch.setattr(service.audit_log, "record", audit)
    cancel_jobs = AsyncMock(return_value=2)
    monkeypatch.setattr(
        classification_jobs,
        "cancel_library_classification_jobs_for_policy_change",
        cancel_jobs,
    )
    command = service.ReplaceLibraryClassificationLabelsCommand(
        organization_id=organization.id,
        actor_user_id=actor.id,
        library_id=library.id,
        taxonomy_id=taxonomy.id,
        expected_taxonomy_updated_at=taxonomy.updated_at,
        expected_label_ids=(),
        label_ids=(second.id, first.id),
    )
    db = _DB(
        _Result(rows=(library,)),
        _Result(rows=(taxonomy,)),
        _Result(rows=(first, second)),
        _Result(rows=()),
        _Result(),
    )
    selected = asyncio.run(service.replace_library_classification_labels(db, command))
    bindings = [item for item in db.added if isinstance(item, LibraryClassificationLabel)]
    assert [label.id for label in selected.labels] == [second.id, first.id]
    assert [(item.label_id, item.ordinal) for item in bindings] == [
        (second.id, 0),
        (first.id, 1),
    ]
    audit.assert_awaited_once()
    cancel_jobs.assert_awaited_once_with(
        db,
        library_id=library.id,
        error_code="library_classification_labels_changed",
    )
    db.commit.assert_not_awaited()

    current = tuple(bindings)
    idempotent_db = _DB(
        _Result(rows=(library,)),
        _Result(rows=(taxonomy,)),
        _Result(rows=(first, second)),
        _Result(rows=current),
    )
    idempotent = service.ReplaceLibraryClassificationLabelsCommand(
        organization_id=organization.id,
        actor_user_id=actor.id,
        library_id=library.id,
        taxonomy_id=taxonomy.id,
        expected_taxonomy_updated_at=taxonomy.updated_at,
        expected_label_ids=(second.id, first.id),
        label_ids=(second.id, first.id),
    )
    asyncio.run(service.replace_library_classification_labels(idempotent_db, idempotent))
    assert not idempotent_db.added
    assert not idempotent_db.results

    stale_db = _DB(
        _Result(rows=(library,)),
        _Result(rows=(taxonomy,)),
        _Result(rows=(first,)),
        _Result(rows=current),
    )
    stale = service.ReplaceLibraryClassificationLabelsCommand(
        organization_id=organization.id,
        actor_user_id=actor.id,
        library_id=library.id,
        taxonomy_id=taxonomy.id,
        expected_taxonomy_updated_at=taxonomy.updated_at,
        expected_label_ids=(),
        label_ids=(first.id,),
    )
    with pytest.raises(service.ClassificationTaxonomyError) as exc_info:
        asyncio.run(service.replace_library_classification_labels(stale_db, stale))
    assert exc_info.value.code == "classification_library_selection_state_changed"
