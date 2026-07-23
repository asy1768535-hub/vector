from __future__ import annotations

import asyncio
import io
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from app.config import Settings, settings, validate_classification_decision_startup
from app.models.classification_decision import (
    DocumentClassificationDecision,
    DocumentClassificationDecisionSet,
    DocumentClassificationProposal,
    DocumentClassificationRun,
)
from app.models.classification_taxonomy import ClassificationLabel, ClassificationTaxonomy
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.library import Library
from app.models.organization import Organization
from app.models.user import User
from app.services import classification_decisions as service
from app.services.classification_decision_contracts import (
    CLASSIFICATION_MIN_CONFIDENCE_MICROS,
    CLASSIFICATION_MIN_MARGIN_MICROS,
    ClassificationDecisionError,
    ClassifierProposalInput,
    ManualClassificationSelection,
    RecordClassificationFailureCommand,
    ReviewClassificationRunCommand,
    SubmitClassificationRunCommand,
    classification_failure_identity,
    classification_run_identity,
)
from app.services.classification_policy import (
    KnownLabelPolicyState,
    evaluate_classification_policy,
)


NOW = datetime(2026, 7, 22, 18, 0, tzinfo=timezone.utc)
HASH = "a" * 64
CONFIG_HASH = "b" * 64


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


class _DB:
    def __init__(self, *results):
        self.results = list(results)
        self.added = []
        self.flush_count = 0
        self.commit = AsyncMock()

    async def execute(self, _statement):
        assert self.results, "unexpected query"
        return self.results.pop(0)

    def add(self, value):
        self.added.append(value)

    def add_all(self, values):
        self.added.extend(values)

    async def flush(self):
        self.flush_count += 1


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


def _taxonomy(organization: Organization) -> ClassificationTaxonomy:
    return ClassificationTaxonomy(
        id=uuid.uuid4(),
        organization_id=organization.id,
        version_key="document-category",
        version_no=1,
        status="active",
        created_by_user_id=uuid.uuid4(),
        activated_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )


def _label(taxonomy: ClassificationTaxonomy, key: str, *, status: str = "active"):
    return ClassificationLabel(
        id=uuid.uuid4(),
        taxonomy_version_id=taxonomy.id,
        key=key,
        label=key.title(),
        status=status,
        sort_order=0,
        created_at=NOW,
        updated_at=NOW,
    )


def _document_revision(library: Library) -> tuple[Document, DocumentRevision]:
    document_id, revision_id = uuid.uuid4(), uuid.uuid4()
    document = Document(
        id=document_id,
        library_id=library.id,
        title="Sensitive title",
        content_hash=HASH,
        current_revision=1,
        current_revision_id=revision_id,
        latest_revision_id=revision_id,
        status="ready",
        created_at=NOW,
        updated_at=NOW,
    )
    revision = DocumentRevision(
        id=revision_id,
        document_id=document_id,
        library_id=library.id,
        revision_no=1,
        title="Sensitive title",
        content_hash=HASH,
        parser_name="test",
        parser_version="1",
        chunking_strategy="fixed",
        chunking_strategy_version="1",
        status="ready",
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW,
    )
    return document, revision


def _submission(
    library: Library,
    taxonomy: ClassificationTaxonomy,
    document: Document,
    revision: DocumentRevision,
    proposals: tuple[ClassifierProposalInput, ...],
    enabled: tuple[uuid.UUID, ...],
    *,
    retry_generation: int = 0,
) -> SubmitClassificationRunCommand:
    return SubmitClassificationRunCommand(
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy.id,
        enabled_label_ids=enabled,
        classifier_version="classifier-v1",
        model_provider="local",
        model_name="model-v1",
        model_config_hash=CONFIG_HASH,
        prompt_version="prompt-v1",
        proposals=proposals,
        trigger_type="revision_ready",
        retry_generation=retry_generation,
    )


def _run(
    library: Library,
    taxonomy: ClassificationTaxonomy,
    document: Document,
    revision: DocumentRevision,
    *,
    status: str = "pending_review",
) -> DocumentClassificationRun:
    return DocumentClassificationRun(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=revision.content_hash,
        taxonomy_version_id=taxonomy.id,
        enabled_label_set_hash="c" * 64,
        policy_version="classification-policy-v1",
        min_confidence_micros=900_000,
        min_margin_micros=150_000,
        max_secondary_labels=8,
        classifier_version="classifier-v1",
        model_provider="local",
        model_name="model-v1",
        model_config_hash=CONFIG_HASH,
        prompt_version="prompt-v1",
        input_fingerprint="d" * 64,
        idempotency_key="e" * 64,
        generation_no=1,
        retry_generation=0,
        trigger_type="revision_ready",
        status=status,
        reason_codes=[],
        created_at=NOW,
        updated_at=NOW,
        finished_at=NOW,
    )


def test_0035_orm_migration_and_offline_sql_are_reversible():
    assert DocumentClassificationRun.__table__.columns.error_code.type.length == 64
    assert DocumentClassificationProposal.__table__.columns.proposed_label.type.length == 160
    assert DocumentClassificationDecisionSet.__table__.columns.source_run_id.nullable
    checks = {
        item.name
        for item in DocumentClassificationDecisionSet.__table__.constraints
        if isinstance(item, CheckConstraint)
    }
    assert "ck_document_classification_decision_sets_source_shape" in checks
    indexes = {item.name for item in DocumentClassificationDecisionSet.__table__.indexes}
    assert "uq_document_classification_decision_sets_effective_revision" in indexes
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_heads() == ["0040"]
    assert script.get_revision("0035").down_revision == "0034"
    upgrade = _offline("upgrade", "0034:0035")
    downgrade = _offline("downgrade", "0035:0034")
    for table in (
        "document_classification_runs",
        "document_classification_proposals",
        "document_classification_decision_sets",
        "document_classification_decisions",
    ):
        assert f"create table {table}" in upgrade
        assert f"drop table {table}" in downgrade
    assert "where lifecycle = 'effective'" in upgrade
    assert downgrade.index("drop table document_classification_decisions") < downgrade.index(
        "drop table document_classification_decision_sets"
    )
    assert "update documents" not in upgrade + downgrade
    assert "delete from" not in upgrade + downgrade


def test_feature_defaults_off_and_requires_taxonomy_and_organization_authorization():
    assert settings.classification_decision_enabled is False
    validate_classification_decision_startup(Settings())
    for values in (
        {"classification_decision_enabled": True},
        {
            "classification_decision_enabled": True,
            "organization_authorization_enabled": True,
        },
        {
            "classification_decision_enabled": True,
            "classification_taxonomy_enabled": True,
        },
    ):
        with pytest.raises(RuntimeError, match="Classification decisions require"):
            validate_classification_decision_startup(Settings(**values))
    validate_classification_decision_startup(
        Settings(
            classification_decision_enabled=True,
            organization_authorization_enabled=True,
            classification_taxonomy_enabled=True,
        )
    )


def test_contracts_are_strict_canonical_and_idempotent():
    organization = _organization()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _document_revision(library)
    primary, secondary = _label(taxonomy, "primary"), _label(taxonomy, "secondary")
    proposals = (
        ClassifierProposalInput("secondary", 900_000, label_id=secondary.id),
        ClassifierProposalInput("primary", 900_000, label_id=primary.id),
    )
    command = _submission(
        library,
        taxonomy,
        document,
        revision,
        proposals,
        (primary.id, secondary.id),
    )
    assert [item.role for item in command.proposals] == ["primary", "secondary"]
    identity = classification_run_identity(command)
    assert classification_run_identity(command) == identity
    retried = _submission(
        library,
        taxonomy,
        document,
        revision,
        proposals,
        (primary.id, secondary.id),
        retry_generation=1,
    )
    assert classification_run_identity(retried).input_fingerprint == identity.input_fingerprint
    assert classification_run_identity(retried).idempotency_key != identity.idempotency_key
    unknown = ClassifierProposalInput(
        "primary", 900_000, proposed_key="  TAX-RISK  ", proposed_label=" Tax   Risk "
    )
    assert unknown.proposed_key == "tax-risk" and unknown.proposed_label == "Tax Risk"
    for score in (-1, 1_000_001, True, 0.5):
        with pytest.raises(ClassificationDecisionError) as exc_info:
            ClassifierProposalInput("primary", score, label_id=primary.id)
        assert exc_info.value.code == "classification_score_invalid"


def test_policy_exact_boundaries_and_manual_precedence():
    first, second, secondary = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    known = {
        value: KnownLabelPolicyState(value, active=True, enabled=True)
        for value in (first, second, secondary)
    }
    exact = (
        ClassifierProposalInput("primary", CLASSIFICATION_MIN_CONFIDENCE_MICROS, label_id=first),
        ClassifierProposalInput(
            "primary",
            CLASSIFICATION_MIN_CONFIDENCE_MICROS - CLASSIFICATION_MIN_MARGIN_MICROS,
            label_id=second,
        ),
        ClassifierProposalInput("secondary", CLASSIFICATION_MIN_CONFIDENCE_MICROS, label_id=secondary),
    )
    passed = evaluate_classification_policy(
        exact, known_labels=known, has_manual_decision_protection=False
    )
    assert passed.run_status == "auto_applied"
    assert passed.proposal_statuses == ("auto_selected", "not_selected", "auto_selected")
    blocked = evaluate_classification_policy(
        exact, known_labels=known, has_manual_decision_protection=True
    )
    assert blocked.run_status == "blocked_manual"
    below_margin = (
        exact[0],
        ClassifierProposalInput(
            "primary",
            exact[1].confidence_micros + 1,
            label_id=second,
        ),
    )
    reviewed = evaluate_classification_policy(
        below_margin, known_labels=known, has_manual_decision_protection=False
    )
    assert reviewed.run_status == "pending_review"
    assert reviewed.run_reason_codes == ("primary_margin_below_threshold",)


@pytest.mark.parametrize(
    ("proposals", "known", "reason"),
    [
        (
            (ClassifierProposalInput("primary", 899_999, label_id=uuid.uuid4()),),
            "enabled",
            "primary_confidence_below_threshold",
        ),
        (
            (
                ClassifierProposalInput(
                    "primary", 900_000, proposed_key="unknown", proposed_label="Unknown"
                ),
            ),
            "none",
            "unknown_label",
        ),
        (
            (ClassifierProposalInput("primary", 900_000, label_id=uuid.uuid4()),),
            "disabled",
            "label_disabled",
        ),
        (
            (ClassifierProposalInput("primary", 900_000, label_id=uuid.uuid4()),),
            "not_enabled",
            "label_not_enabled",
        ),
    ],
)
def test_policy_routes_any_invalid_generation_entirely_to_review(proposals, known, reason):
    label_id = proposals[0].label_id
    states = {}
    if label_id is not None and known != "none":
        states[label_id] = KnownLabelPolicyState(
            label_id,
            active=known != "disabled",
            enabled=known == "enabled",
        )
    result = evaluate_classification_policy(
        proposals,
        known_labels=states,
        has_manual_decision_protection=False,
    )
    assert result.run_status == "pending_review"
    assert reason in result.run_reason_codes
    assert set(result.proposal_statuses) == {"pending_review"}


def test_duplicate_and_excess_secondaries_never_partially_apply():
    primary = uuid.uuid4()
    secondary_ids = tuple(uuid.uuid4() for _ in range(9))
    proposals = (
        ClassifierProposalInput("primary", 950_000, label_id=primary),
        ClassifierProposalInput("secondary", 950_000, label_id=primary),
        *(ClassifierProposalInput("secondary", 950_000, label_id=value) for value in secondary_ids),
    )
    states = {
        value: KnownLabelPolicyState(value, active=True, enabled=True)
        for value in (primary, *secondary_ids)
    }
    result = evaluate_classification_policy(
        proposals, known_labels=states, has_manual_decision_protection=False
    )
    assert result.run_status == "pending_review"
    assert result.run_reason_codes == ("duplicate_label", "secondary_limit_exceeded")
    assert set(result.proposal_statuses) == {"pending_review"}


def test_submit_auto_applies_and_never_commits(monkeypatch):
    organization, actor = _organization(), _user()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _document_revision(library)
    primary, secondary = _label(taxonomy, "primary"), _label(taxonomy, "secondary")
    command = _submission(
        library,
        taxonomy,
        document,
        revision,
        (
            ClassifierProposalInput("primary", 950_000, label_id=primary.id),
            ClassifierProposalInput("secondary", 910_000, label_id=secondary.id),
        ),
        (primary.id, secondary.id),
    )
    command = replace(command, requested_by_user_id=actor.id)
    monkeypatch.setattr(service, "_lock_current_document", AsyncMock(return_value=(document, revision)))
    monkeypatch.setattr(service, "_load_library", AsyncMock(return_value=library))
    monkeypatch.setattr(
        service,
        "_lock_taxonomy_and_enabled_labels",
        AsyncMock(
            return_value=(
                taxonomy,
                (primary, secondary),
                {primary.id: primary, secondary.id: secondary},
            )
        ),
    )
    monkeypatch.setattr(service, "_effective_set", AsyncMock(return_value=None))
    monkeypatch.setattr(service, "_latest_decision_set", AsyncMock(return_value=None))
    decision_set = SimpleNamespace(id=uuid.uuid4())
    replacement = AsyncMock(return_value=(decision_set, ()))
    monkeypatch.setattr(service, "_replace_decision_set", replacement)
    audit = AsyncMock()
    monkeypatch.setattr(service.audit_log, "record", audit)
    db = _DB(_Result(rows=()), _Result(scalar=0))
    result = asyncio.run(service.submit_classification_run(db, command))
    assert result.run.status == "auto_applied"
    assert [item.status for item in result.proposals] == ["auto_selected", "auto_selected"]
    assert replacement.await_args.kwargs["primary"] == (primary.id, 950_000)
    assert replacement.await_args.kwargs["secondaries"] == ((secondary.id, 910_000),)
    target = audit.await_args.args[3]
    assert "Sensitive title" not in str(target)
    assert "model_name" not in target and "input_fingerprint" not in target
    db.commit.assert_not_awaited()


def test_manual_effective_set_blocks_model_rerun(monkeypatch):
    organization = _organization()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _document_revision(library)
    primary = _label(taxonomy, "primary")
    manual = DocumentClassificationDecisionSet(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        taxonomy_version_id=taxonomy.id,
        source="manual",
        lifecycle="effective",
        generation_no=1,
        reviewed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    command = _submission(
        library,
        taxonomy,
        document,
        revision,
        (ClassifierProposalInput("primary", 950_000, label_id=primary.id),),
        (primary.id,),
    )
    monkeypatch.setattr(service, "_lock_current_document", AsyncMock(return_value=(document, revision)))
    monkeypatch.setattr(service, "_load_library", AsyncMock(return_value=library))
    monkeypatch.setattr(
        service,
        "_lock_taxonomy_and_enabled_labels",
        AsyncMock(return_value=(taxonomy, (primary,), {primary.id: primary})),
    )
    monkeypatch.setattr(service, "_effective_set", AsyncMock(return_value=manual))
    monkeypatch.setattr(
        service,
        "_normalize_effective_set_for_scope",
        AsyncMock(return_value=manual),
    )
    replace = AsyncMock()
    monkeypatch.setattr(service, "_replace_decision_set", replace)
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())
    result = asyncio.run(
        service.submit_classification_run(_DB(_Result(rows=()), _Result(scalar=1)), command)
    )
    assert result.run.status == "blocked_manual"
    assert result.decision_set is None
    replace.assert_not_awaited()


def test_manual_removal_marker_blocks_automatic_reclassification(monkeypatch):
    organization = _organization()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _document_revision(library)
    primary = _label(taxonomy, "primary")
    removed = DocumentClassificationDecisionSet(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        taxonomy_version_id=taxonomy.id,
        source="manual",
        lifecycle="removed",
        generation_no=2,
        reviewed_at=NOW,
        removed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    command = _submission(
        library,
        taxonomy,
        document,
        revision,
        (ClassifierProposalInput("primary", 950_000, label_id=primary.id),),
        (primary.id,),
    )
    monkeypatch.setattr(
        service,
        "_lock_current_document",
        AsyncMock(return_value=(document, revision)),
    )
    monkeypatch.setattr(service, "_load_library", AsyncMock(return_value=library))
    monkeypatch.setattr(
        service,
        "_lock_taxonomy_and_enabled_labels",
        AsyncMock(return_value=(taxonomy, (primary,), {primary.id: primary})),
    )
    monkeypatch.setattr(service, "_effective_set", AsyncMock(return_value=None))
    monkeypatch.setattr(
        service,
        "_latest_decision_set",
        AsyncMock(return_value=removed),
    )
    replace = AsyncMock()
    monkeypatch.setattr(service, "_replace_decision_set", replace)
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())
    result = asyncio.run(
        service.submit_classification_run(
            _DB(_Result(rows=()), _Result(scalar=2)), command
        )
    )
    assert result.run.status == "blocked_manual"
    assert result.decision_set is None
    replace.assert_not_awaited()


def test_manual_removal_projects_unclassified_over_older_pending_run(monkeypatch):
    organization = _organization()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _document_revision(library)
    pending = _run(library, taxonomy, document, revision)
    removed = DocumentClassificationDecisionSet(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        taxonomy_version_id=taxonomy.id,
        source="manual",
        lifecycle="removed",
        generation_no=2,
        reviewed_at=NOW,
        removed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    pending.created_at = NOW.replace(hour=18)
    monkeypatch.setattr(service, "_effective_set", AsyncMock(return_value=None))
    monkeypatch.setattr(
        service,
        "_latest_decision_set",
        AsyncMock(return_value=removed),
    )
    value = asyncio.run(
        service.get_effective_classification(
            _DB(_Result(rows=(document,)), _Result(rows=(revision,)), _Result(rows=(pending,))),
            library_id=library.id,
            document_id=document.id,
        )
    )
    assert value.state == "unclassified"
    assert value.decision_set is None and value.latest_run is pending


def test_failure_record_is_idempotent_sanitized_and_content_free(monkeypatch):
    organization = _organization()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _document_revision(library)
    label = _label(taxonomy, "primary")
    command = RecordClassificationFailureCommand(
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        revision_content_hash=HASH,
        taxonomy_version_id=taxonomy.id,
        enabled_label_ids=(label.id,),
        classifier_version="classifier-v1",
        model_provider="local",
        model_name="model-v1",
        model_config_hash=CONFIG_HASH,
        prompt_version="prompt-v1",
        trigger_type="revision_ready",
        error_code="provider_timeout",
    )
    assert classification_failure_identity(command) == classification_failure_identity(command)
    with pytest.raises(ClassificationDecisionError) as exc_info:
        replace(command, error_code="timeout: secret endpoint")
    assert exc_info.value.code == "classification_error_code_invalid"
    monkeypatch.setattr(service, "_lock_current_document", AsyncMock(return_value=(document, revision)))
    monkeypatch.setattr(service, "_load_library", AsyncMock(return_value=library))
    monkeypatch.setattr(
        service,
        "_lock_taxonomy_and_enabled_labels",
        AsyncMock(return_value=(taxonomy, (label,), {label.id: label})),
    )
    audit = AsyncMock()
    monkeypatch.setattr(service.audit_log, "record", audit)
    db = _DB(_Result(rows=()), _Result(scalar=2))
    result = asyncio.run(service.record_classification_failure(db, command))
    assert result.run.status == "failed" and result.run.error_code == "provider_timeout"
    assert result.proposals == () and result.decision_set is None
    assert audit.await_args.args[3]["error_code"] == "provider_timeout"
    assert "Sensitive title" not in str(audit.await_args.args[3])
    db.commit.assert_not_awaited()


def test_review_accept_creates_manual_decision_and_fences_state(monkeypatch):
    organization, actor = _organization(), _user()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _document_revision(library)
    primary, secondary = _label(taxonomy, "primary"), _label(taxonomy, "secondary")
    run = _run(library, taxonomy, document, revision)
    proposals = (
        DocumentClassificationProposal(
            id=uuid.uuid4(),
            run_id=run.id,
            label_id=primary.id,
            role="primary",
            rank=0,
            confidence_micros=850_000,
            status="pending_review",
            reason_codes=["primary_confidence_below_threshold"],
            created_at=NOW,
            updated_at=NOW,
        ),
        DocumentClassificationProposal(
            id=uuid.uuid4(),
            run_id=run.id,
            label_id=secondary.id,
            role="secondary",
            rank=0,
            confidence_micros=930_000,
            status="pending_review",
            reason_codes=[],
            created_at=NOW,
            updated_at=NOW,
        ),
    )
    monkeypatch.setattr(service, "_require_library_management", AsyncMock(return_value=library))
    monkeypatch.setattr(service, "_lock_current_document", AsyncMock(return_value=(document, revision)))
    monkeypatch.setattr(
        service,
        "_lock_taxonomy_and_enabled_labels",
        AsyncMock(return_value=(taxonomy, (primary, secondary), {primary.id: primary, secondary.id: secondary})),
    )
    monkeypatch.setattr(service, "_effective_set", AsyncMock(return_value=None))
    decision_set = SimpleNamespace(id=uuid.uuid4())
    replacement = AsyncMock(return_value=(decision_set, ()))
    monkeypatch.setattr(service, "_replace_decision_set", replacement)
    monkeypatch.setattr(service.audit_log, "record", AsyncMock())
    db = _DB(_Result(rows=(run,)), _Result(rows=(run,)), _Result(rows=proposals))
    result = asyncio.run(
        service.review_classification_run(
            db,
            ReviewClassificationRunCommand(
                library_id=library.id,
                actor_user_id=actor.id,
                run_id=run.id,
                expected_run_status="pending_review",
                expected_effective_decision_set_id=None,
                action="accept",
            ),
        )
    )
    assert result.run.status == "manual_applied"
    assert [item.status for item in result.proposals] == ["accepted", "accepted"]
    assert replacement.await_args.kwargs["source"] == "manual"
    assert replacement.await_args.kwargs["primary"] == (primary.id, None)
    db.commit.assert_not_awaited()
    with pytest.raises(ClassificationDecisionError) as stale:
        service._fence_effective_set(None, uuid.uuid4())
    assert stale.value.code == "classification_decision_state_changed"


def test_review_list_batches_document_effective_decisions_and_enabled_labels(
    monkeypatch,
):
    organization, actor = _organization(), _user()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _document_revision(library)
    primary = _label(taxonomy, "primary")
    run = _run(library, taxonomy, document, revision)
    proposal = DocumentClassificationProposal(
        id=uuid.uuid4(),
        run_id=run.id,
        label_id=primary.id,
        role="primary",
        rank=0,
        confidence_micros=850_000,
        status="pending_review",
        reason_codes=["primary_confidence_below_threshold"],
        created_at=NOW,
        updated_at=NOW,
    )
    decision_set = DocumentClassificationDecisionSet(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        taxonomy_version_id=taxonomy.id,
        source="manual",
        lifecycle="effective",
        generation_no=1,
        reviewed_by_user_id=actor.id,
        reviewed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    decision = DocumentClassificationDecision(
        id=uuid.uuid4(),
        decision_set_id=decision_set.id,
        label_id=primary.id,
        role="primary",
        ordinal=0,
        created_at=NOW,
    )
    monkeypatch.setattr(
        service,
        "_require_library_management",
        AsyncMock(return_value=library),
    )
    db = _DB(
        _Result(scalar=1),
        _Result(rows=((run, document),)),
        _Result(rows=(taxonomy,)),
        _Result(rows=(primary,)),
        _Result(rows=((proposal, primary),)),
        _Result(rows=(decision_set,)),
        _Result(rows=((decision, primary),)),
    )

    page = asyncio.run(
        service.list_classification_review_runs(
            db,
            library_id=library.id,
            actor_user_id=actor.id,
            limit=20,
            offset=0,
        )
    )

    assert page.total == 1
    assert page.taxonomy is taxonomy
    assert page.available_labels == (primary,)
    assert page.items[0].document is document
    assert page.items[0].effective_decision_set is decision_set
    assert page.items[0].effective_decisions == ((decision, primary),)
    assert page.items[0].proposals == ((proposal, primary),)
    assert db.results == []


def test_empty_review_list_still_returns_current_enabled_labels(monkeypatch):
    organization, actor = _organization(), _user()
    library, taxonomy = _library(organization), _taxonomy(organization)
    label = _label(taxonomy, "primary")
    monkeypatch.setattr(
        service,
        "_require_library_management",
        AsyncMock(return_value=library),
    )
    db = _DB(
        _Result(scalar=0),
        _Result(rows=()),
        _Result(rows=(taxonomy,)),
        _Result(rows=(label,)),
    )

    page = asyncio.run(
        service.list_classification_review_runs(
            db,
            library_id=library.id,
            actor_user_id=actor.id,
        )
    )

    assert page.items == () and page.total == 0
    assert page.taxonomy is taxonomy and page.available_labels == (label,)
    assert db.results == []


def test_second_reviewer_observes_stable_run_state_change(monkeypatch):
    organization, actor = _organization(), _user()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _document_revision(library)
    initially_pending = _run(library, taxonomy, document, revision)
    locked_after_first_reviewer = _run(
        library,
        taxonomy,
        document,
        revision,
        status="manual_applied",
    )
    locked_after_first_reviewer.id = initially_pending.id
    monkeypatch.setattr(
        service,
        "_require_library_management",
        AsyncMock(return_value=library),
    )
    monkeypatch.setattr(
        service,
        "_lock_current_document",
        AsyncMock(return_value=(document, revision)),
    )
    db = _DB(
        _Result(rows=(initially_pending,)),
        _Result(rows=(locked_after_first_reviewer,)),
    )
    with pytest.raises(ClassificationDecisionError) as state_changed:
        asyncio.run(
            service.review_classification_run(
                db,
                ReviewClassificationRunCommand(
                    library_id=library.id,
                    actor_user_id=actor.id,
                    run_id=initially_pending.id,
                    expected_run_status="pending_review",
                    expected_effective_decision_set_id=None,
                    action="reject",
                ),
            )
        )
    assert state_changed.value.code == "classification_run_state_changed"
    db.commit.assert_not_awaited()


def test_replace_decision_set_supersedes_before_new_effective_flush():
    organization = _organization()
    library, taxonomy = _library(organization), _taxonomy(organization)
    document, revision = _document_revision(library)
    label = _label(taxonomy, "primary")
    current = DocumentClassificationDecisionSet(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        taxonomy_version_id=taxonomy.id,
        source="manual",
        lifecycle="effective",
        generation_no=1,
        reviewed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    db = _DB(_Result(scalar=1))
    new_set, decisions = asyncio.run(
        service._replace_decision_set(
            db,
            document=document,
            revision=revision,
            taxonomy=taxonomy,
            source="manual",
            source_run=None,
            actor_user_id=uuid.uuid4(),
            primary=(label.id, None),
            secondaries=(),
            current=current,
        )
    )
    assert current.lifecycle == "superseded" and current.superseded_at is not None
    assert new_set.lifecycle == "effective" and new_set.generation_no == 2
    assert len(decisions) == 1 and decisions[0].role == "primary"
    assert db.flush_count == 3
    db.commit.assert_not_awaited()

    removal_db = _DB(_Result(scalar=2))
    removed, removed_decisions = asyncio.run(
        service._replace_decision_set(
            removal_db,
            document=document,
            revision=revision,
            taxonomy=taxonomy,
            source="manual",
            source_run=None,
            actor_user_id=uuid.uuid4(),
            primary=None,
            secondaries=(),
            current=new_set,
            remove=True,
        )
    )
    assert new_set.lifecycle == "superseded"
    assert removed.lifecycle == "removed" and removed.removed_at is not None
    assert removed.supersedes_decision_set_id == new_set.id
    assert removed_decisions == () and removal_db.flush_count == 2
    removal_db.commit.assert_not_awaited()


def test_manual_selection_rejects_duplicate_or_out_of_subset_labels():
    label = uuid.uuid4()
    with pytest.raises(ClassificationDecisionError) as duplicate:
        ManualClassificationSelection(label, (label,))
    assert duplicate.value.code == "classification_label_selection_invalid"
    taxonomy = _taxonomy(_organization())
    enabled = (_label(taxonomy, "enabled"),)
    selection = ManualClassificationSelection(uuid.uuid4())
    with pytest.raises(ClassificationDecisionError) as outside:
        service._validate_selection_enabled(selection, enabled)
    assert outside.value.code == "classification_label_selection_invalid"


def test_effective_manual_set_is_superseded_when_taxonomy_scope_changes():
    organization = _organization()
    library = _library(organization)
    old_taxonomy = _taxonomy(organization)
    new_taxonomy = _taxonomy(organization)
    document, revision = _document_revision(library)
    current = DocumentClassificationDecisionSet(
        id=uuid.uuid4(),
        library_id=library.id,
        document_id=document.id,
        document_revision_id=revision.id,
        taxonomy_version_id=old_taxonomy.id,
        source="manual",
        lifecycle="effective",
        generation_no=1,
        reviewed_at=NOW,
        created_at=NOW,
        updated_at=NOW,
    )
    enabled = (_label(new_taxonomy, "current"),)
    db = _DB()
    normalized = asyncio.run(
        service._normalize_effective_set_for_scope(
            db,
            current=current,
            taxonomy=new_taxonomy,
            enabled=enabled,
        )
    )
    assert normalized is None
    assert current.lifecycle == "superseded" and current.superseded_at is not None
    assert db.flush_count == 1
    db.commit.assert_not_awaited()
