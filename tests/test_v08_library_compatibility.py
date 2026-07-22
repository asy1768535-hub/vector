from __future__ import annotations

import asyncio
import io
import math
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from alembic import command as alembic_command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import CheckConstraint

from app.config import (
    Settings,
    settings,
    validate_library_compatibility_startup,
)
from app.models.attribute_definition import AttributeDefinition
from app.models.entity_type import EntityType
from app.models.graph_publication import GraphPublication
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.organization import Organization
from app.models.organization_membership import OrganizationMembership
from app.models.relation_type import RelationType
from app.models.relation_type_constraint import RelationTypeConstraint
from app.models.user import User
from app.services import library_embedding_compatibility as embedding_service
from app.services import library_graph_compatibility as graph_service
from app.services import library_compatibility as service
from app.services import library_retrieval_compatibility as retrieval_service
from app.services.library_compatibility_contracts import (
    EMBEDDING_PROBE_CONTRACT_VERSION,
    LibraryCompatibilityError,
    LibraryIncompatibility,
)
from app.services.organization_authorization import OrganizationAccess


NOW = datetime(2026, 7, 22, 23, tzinfo=timezone.utc)


class _Result:
    def __init__(self, rows=()):
        self.rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self.rows)


class _DB:
    def __init__(self, *rows):
        self.rows = list(rows)
        self.statements = []
        self.commit_count = 0

    async def execute(self, statement):
        self.statements.append(statement)
        assert self.rows, "unexpected database query"
        return _Result(self.rows.pop(0))

    async def commit(self):
        self.commit_count += 1


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
        slug=f"org-{uuid.uuid4().hex[:8]}",
        name="Organization",
        deployment_profile="hosted",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _user(*, superuser: bool = False) -> User:
    return User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4().hex}@example.com",
        hashed_password="hash",
        is_active=True,
        is_superuser=superuser,
        is_verified=True,
        created_at=NOW,
    )


def _library(
    organization: Organization,
    *,
    slug: str | None = None,
    verified: bool = True,
) -> Library:
    library = Library(
        id=uuid.uuid4(),
        organization_id=organization.id,
        slug=slug or f"lib-{uuid.uuid4().hex[:8]}",
        name="Library",
        embedding_model="bge-m3",
        embedding_dim=2,
        vector_distance="cosine",
        embedding_base_url="https://embedding.internal/v1",
        chunk_size=1000,
        chunk_overlap=120,
        retrieval_mode="dense",
        qdrant_collection=f"lib_{uuid.uuid4().hex[:8]}",
        index_state="ready",
        created_at=NOW,
    )
    if verified:
        library.embedding_probe_contract_version = EMBEDDING_PROBE_CONTRACT_VERSION
        library.embedding_probe_model = library.embedding_model
        library.embedding_probe_dimension = library.embedding_dim
        library.embedding_probe_endpoint_sha256 = embedding_service.endpoint_sha256(library)
        library.embedding_probe_fingerprint = "a" * 64
        library.embedding_probe_verified_at = NOW
    return library


def _ontology(library: Library) -> OntologyVersion:
    return OntologyVersion(
        id=uuid.uuid4(),
        library_id=library.id,
        version_key="enterprise",
        version_no=1,
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )


def _schema_rows(library: Library, ontology: OntologyVersion):
    person = EntityType(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        key="person",
        label="Person",
        description="An individual",
        properties_schema={"type": "object"},
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    company = EntityType(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        key="company",
        label="Company",
        description="A legal entity",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    invests = RelationType(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        key="invests_in",
        label="Invests In",
        description="Investment relation",
        direction="directed",
        requires_evidence=True,
        default_review_policy="pending_review",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    constraint = RelationTypeConstraint(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        relation_type_id=invests.id,
        source_entity_type_id=person.id,
        target_entity_type_id=company.id,
        cardinality="many_to_many",
        requires_review=True,
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    attribute = AttributeDefinition(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        owner_kind="entity_type",
        owner_type_id=person.id,
        key="name",
        label="Name",
        value_type="string",
        required=True,
        indexed=True,
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    return [person, company], [invests], [constraint], [attribute]


def _publication(library: Library, ontology: OntologyVersion, *, status="active"):
    return GraphPublication(
        id=uuid.uuid4(),
        library_id=library.id,
        ontology_version_id=ontology.id,
        status=status,
        source_mode="initial_seed",
        manifest_version="v1",
        policy_version="v1",
        policy_snapshot={"manifest_version": "v1", "policy_version": "v1"},
        manifest_hash="b" * 64,
        idempotency_key=f"publication-{uuid.uuid4()}",
        created_at=NOW,
        updated_at=NOW,
        planned_at=NOW,
    )


def test_0032_orm_migration_and_offline_sql_are_exactly_reversible():
    columns = Library.__table__.columns
    for name in (
        "embedding_probe_contract_version",
        "embedding_probe_model",
        "embedding_probe_dimension",
        "embedding_probe_endpoint_sha256",
        "embedding_probe_fingerprint",
        "embedding_probe_verified_at",
    ):
        assert columns[name].nullable is True
    constraint = next(
        item
        for item in Library.__table__.constraints
        if item.name == "ck_sys_libraries_embedding_probe_snapshot"
    )
    assert isinstance(constraint, CheckConstraint)
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    migration = script.get_revision("0032")
    assert migration is not None and migration.down_revision == "0031"
    next_migration = script.get_revision("0033")
    assert next_migration is not None and next_migration.down_revision == "0032"
    taxonomy_migration = script.get_revision("0034")
    assert taxonomy_migration is not None and taxonomy_migration.down_revision == "0033"
    assert script.get_heads() == ["0035"]
    upgrade = _offline("upgrade", "0031:0032")
    downgrade = _offline("downgrade", "0032:0031")
    assert upgrade.count("add column embedding_probe_") == 6
    assert "add constraint ck_sys_libraries_embedding_probe_snapshot" in upgrade
    assert downgrade.count("drop column embedding_probe_") == 6
    assert "drop constraint ck_sys_libraries_embedding_probe_snapshot" in downgrade
    assert "delete from" not in upgrade + downgrade
    assert "update sys_libraries" not in upgrade + downgrade


def test_compatibility_defaults_off_and_requires_organization_authorization():
    assert settings.cross_library_compatibility_enabled is False
    validate_library_compatibility_startup(Settings())
    with pytest.raises(RuntimeError, match="Organization authorization"):
        validate_library_compatibility_startup(
            Settings(cross_library_compatibility_enabled=True)
        )
    validate_library_compatibility_startup(
        Settings(
            cross_library_compatibility_enabled=True,
            organization_authorization_enabled=True,
        )
    )


def test_probe_fingerprint_is_deterministic_and_rejects_invalid_vectors():
    vectors = [[0.125, -0.0], [1, -2.5]]
    first = embedding_service.canonical_probe_fingerprint(
        vectors,
        model="bge-m3",
        expected_dimension=2,
    )
    second = embedding_service.canonical_probe_fingerprint(
        [[0.1250000001, 0], [1.0, -2.5]],
        model="bge-m3",
        expected_dimension=2,
    )
    assert first == second
    assert len(first) == 64
    with pytest.raises(LibraryCompatibilityError) as exc_info:
        embedding_service.canonical_probe_fingerprint(
            [[0.1], [0.2]], model="bge-m3", expected_dimension=2
        )
    assert exc_info.value.code == "embedding_probe_dimension_mismatch"
    with pytest.raises(LibraryCompatibilityError) as exc_info:
        embedding_service.canonical_probe_fingerprint(
            [[math.nan, 0], [0, 0]], model="bge-m3", expected_dimension=2
        )
    assert exc_info.value.code == "embedding_probe_value_invalid"


def test_embedding_verification_stores_bounded_snapshot_and_audit(monkeypatch):
    organization = _organization()
    library = _library(organization, verified=False)
    actor = _user()
    db = _DB()
    audit = AsyncMock()
    monkeypatch.setattr(
        embedding_service.embedding,
        "embed_texts",
        AsyncMock(return_value=[[0, 1], [1, 0]]),
    )
    monkeypatch.setattr(embedding_service.audit_log, "record", audit)
    snapshot = asyncio.run(
        embedding_service.verify_library_embedding_profile(
            db,
            library=library,
            actor_user_id=actor.id,
            at=NOW,
        )
    )
    assert snapshot.verified_at == NOW
    assert library.embedding_probe_fingerprint == snapshot.probe_fingerprint
    assert (
        library.embedding_probe_endpoint_sha256
        == embedding_service.endpoint_sha256(library)
    )
    assert db.commit_count == 0
    audit_target = audit.await_args.args[3]
    assert "endpoint" not in repr(audit_target).lower()
    assert "vector" not in repr(audit_target).lower()
    assert "api_key" not in repr(audit_target).lower()

    assert embedding_service.build_embedding_profile(library).ready is True
    library.embedding_base_url = "https://replacement.internal/v1"
    stale = embedding_service.build_embedding_profile(library)
    assert stale.ready is False
    assert stale.reason_code == "embedding_verification_stale"


def test_retrieval_profile_changes_for_bound_library_policy(monkeypatch):
    monkeypatch.setattr(settings, "query_rewrite_enabled", False)
    monkeypatch.setattr(settings, "query_rewrite_llm_enabled", False)
    monkeypatch.setattr(settings, "rerank_enabled", False)
    organization = _organization()
    first = _library(organization)
    second = _library(organization)
    assert retrieval_service.build_retrieval_profile(
        first
    ).fingerprint == retrieval_service.build_retrieval_profile(second).fingerprint
    second.chunk_overlap += 1
    assert retrieval_service.build_retrieval_profile(
        first
    ).fingerprint != retrieval_service.build_retrieval_profile(second).fingerprint


def test_schema_payload_ignores_uuid_and_row_order_but_binds_semantics():
    organization = _organization()
    first_library = _library(organization)
    second_library = _library(organization)
    first_ontology = _ontology(first_library)
    second_ontology = _ontology(second_library)
    second_ontology.version_key = "customer-local-version-name"
    first_rows = _schema_rows(first_library, first_ontology)
    second_rows = _schema_rows(second_library, second_ontology)
    first_payload = graph_service.canonical_schema_payload(first_ontology, *first_rows)
    second_payload = graph_service.canonical_schema_payload(
        second_ontology,
        list(reversed(second_rows[0])),
        *second_rows[1:],
    )
    assert first_payload == second_payload
    second_rows[0][0].description = "A materially different meaning"
    changed_payload = graph_service.canonical_schema_payload(
        second_ontology, *second_rows
    )
    assert first_payload != changed_payload


def test_graph_profile_binds_publication_policy_snapshot(monkeypatch):
    monkeypatch.setattr(settings, "graph_retrieval_enabled", True)
    organization = _organization()
    libraries = (_library(organization), _library(organization))
    ontologies = tuple(_ontology(library) for library in libraries)
    schema_rows = tuple(
        _schema_rows(library, ontology)
        for library, ontology in zip(libraries, ontologies)
    )
    publications = [
        _publication(library, ontology)
        for library, ontology in zip(libraries, ontologies)
    ]
    publications[1].policy_snapshot["require_entity_evidence"] = True
    db = _DB(
        ontologies,
        [row for rows in schema_rows for row in rows[0]],
        [row for rows in schema_rows for row in rows[1]],
        [row for rows in schema_rows for row in rows[2]],
        [row for rows in schema_rows for row in rows[3]],
        publications,
    )
    profiles = asyncio.run(graph_service.build_graph_profiles(db, libraries))
    assert profiles[libraries[0].id].ready is True
    assert profiles[libraries[1].id].ready is True
    assert profiles[libraries[0].id].fingerprint != profiles[libraries[1].id].fingerprint


def test_graph_profiles_use_bounded_queries_and_semantic_content(monkeypatch):
    monkeypatch.setattr(settings, "graph_retrieval_enabled", True)
    organization = _organization()
    libraries = (_library(organization), _library(organization))
    ontologies = tuple(_ontology(library) for library in libraries)
    schema_rows = tuple(
        _schema_rows(library, ontology)
        for library, ontology in zip(libraries, ontologies)
    )
    entities = [row for rows in schema_rows for row in rows[0]]
    relations = [row for rows in schema_rows for row in rows[1]]
    constraints = [row for rows in schema_rows for row in rows[2]]
    attributes = [row for rows in schema_rows for row in rows[3]]
    publications = [
        _publication(library, ontology)
        for library, ontology in zip(libraries, ontologies)
    ]
    db = _DB(ontologies, entities, relations, constraints, attributes, publications)
    profiles = asyncio.run(graph_service.build_graph_profiles(db, libraries))
    assert len(db.statements) == 6
    assert profiles[libraries[0].id].ready is True
    assert profiles[libraries[0].id].fingerprint == profiles[libraries[1].id].fingerprint

    degraded_db = _DB(
        [ontologies[0]],
        schema_rows[0][0],
        schema_rows[0][1],
        schema_rows[0][2],
        schema_rows[0][3],
        [_publication(libraries[0], ontologies[0], status="degraded")],
    )
    degraded = asyncio.run(
        graph_service.build_graph_profiles(degraded_db, (libraries[0],))
    )
    assert degraded[libraries[0].id].ready is False
    assert (
        degraded[libraries[0].id].reason_code == "graph_publication_unhealthy"
    )


def test_text_assessment_does_not_load_graph_and_returns_ordered_reasons(monkeypatch):
    monkeypatch.setattr(settings, "query_rewrite_enabled", False)
    monkeypatch.setattr(settings, "query_rewrite_llm_enabled", False)
    monkeypatch.setattr(settings, "rerank_enabled", False)
    organization = _organization()
    user = _user()
    first = _library(organization)
    second = _library(organization)
    second.index_state = "rebuilding"
    accesses = tuple(
        OrganizationAccess(
            organization_id=organization.id,
            membership_id=uuid.uuid4(),
            role="member",
            library=library,
            action="read",
        )
        for library in (first, second)
    )
    authorize = AsyncMock(return_value=accesses)
    monkeypatch.setattr(service, "resolve_library_selection", authorize)
    db = _DB()
    assessment = asyncio.run(
        service.assess_library_compatibility(
            db,
            user=user,
            library_slugs=(first.slug, second.slug),
            channels=("text",),
        )
    )
    assert assessment.compatible is False
    assert assessment.incompatibilities[0].library_slug == second.slug
    assert assessment.incompatibilities[0].reason_codes == ("library_index_unready",)
    assert db.statements == []
    authorize.assert_awaited_once()


def test_assessment_rejects_invalid_service_input_before_authorization(monkeypatch):
    authorize = AsyncMock()
    monkeypatch.setattr(service, "resolve_library_selection", authorize)
    with pytest.raises(LibraryCompatibilityError) as exc_info:
        asyncio.run(
            service.assess_library_compatibility(
                _DB(),
                user=_user(),
                library_slugs=("duplicate", "duplicate"),
                channels=("text",),
            )
        )
    assert exc_info.value.code == "compatibility_request_invalid"
    authorize.assert_not_awaited()


def test_assessment_reports_cross_organization_without_hiding_authorized_rows(monkeypatch):
    monkeypatch.setattr(settings, "query_rewrite_enabled", False)
    monkeypatch.setattr(settings, "query_rewrite_llm_enabled", False)
    monkeypatch.setattr(settings, "rerank_enabled", False)
    first_organization = _organization()
    second_organization = _organization()
    first = _library(first_organization)
    second = _library(second_organization)
    accesses = (
        OrganizationAccess(
            organization_id=first_organization.id,
            membership_id=uuid.uuid4(),
            role="member",
            library=first,
            action="read",
        ),
        OrganizationAccess(
            organization_id=second_organization.id,
            membership_id=uuid.uuid4(),
            role="member",
            library=second,
            action="read",
        ),
    )
    monkeypatch.setattr(
        service,
        "resolve_library_selection",
        AsyncMock(return_value=accesses),
    )
    assessment = asyncio.run(
        service.assess_library_compatibility(
            _DB(),
            user=_user(),
            library_slugs=(first.slug, second.slug),
            channels=("text",),
        )
    )
    assert assessment.compatible is False
    assert assessment.incompatibilities == (
        LibraryIncompatibility(second.slug, ("organization_mismatch",)),
    )


def test_batch_selection_denies_platform_superuser_without_library_read(monkeypatch):
    from app.services import organization_authorization as authorization

    organization = _organization()
    library = _library(organization)
    user = _user(superuser=True)
    membership = OrganizationMembership(
        id=uuid.uuid4(),
        organization_id=organization.id,
        user_id=user.id,
        role="member",
        status="active",
        created_at=NOW,
        updated_at=NOW,
    )
    db = _DB([(library, membership, organization)])
    monkeypatch.setattr(settings, "organization_authorization_enabled", True)
    monkeypatch.setattr(authorization, "has_permission", lambda *_: False)
    with pytest.raises(authorization.OrganizationAuthorizationError):
        asyncio.run(
            authorization.resolve_library_selection(
                db,
                user=user,
                library_slugs=(library.slug,),
            )
        )
