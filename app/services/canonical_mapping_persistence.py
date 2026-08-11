"""Append-only persistence boundary for validated canonical mapping results.

This module is deliberately read/write isolated from workers, providers,
candidates, materializers, publication, and API code.  It accepts only an
M0-authoritative input/result pair and never creates or changes a decision.
"""

from __future__ import annotations

import json
import hashlib
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID, uuid5

from sqlalchemy import select
from asyncpg import exceptions as asyncpg_exceptions
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.canonical_mapping import GraphClaimMapping, GraphMappingAuthoritySnapshot
from app.models.claim_decision import GraphClaimDecision
from app.models.document import Document
from app.models.document_revision import DocumentRevision
from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_extraction_unit import GraphExtractionUnit
from app.models.library import Library
from app.models.ontology_version import OntologyVersion
from app.models.raw_claim import GraphRawClaim, GraphRawClaimOccurrence
from app.schemas.canonical_mapping import (
    CANONICAL_MAPPING_UUID_NAMESPACE,
    CLAIM_DECISION_ID_NAMESPACE,
    CanonicalMappingInputV1,
    CanonicalMappingV1,
    DecisionValidationBindingV1,
    FrozenOntologySnapshotV1,
    MappingAuthorizationRegistrySnapshotV1,
    RawClaimMappingSnapshotV1,
    MappingScopeV1,
    canonical_mapping_json,
    canonical_mapping_json_value,
    repository_authorized_canonical_mapping_json,
    semantic_projection_from_claim,
    _validate_decision_projection_against_claim,
    _validate_repository_remap_predecessor,
)
from app.schemas.claim_decision import (
    ClaimDecisionProjectionV1,
    canonical_claim_decision_json,
    claim_decision_fingerprint,
    deterministic_decision_id,
)
from app.schemas.raw_claim import (
    EvidenceReferenceV1,
    RawClaimV1,
    canonical_raw_claim_json,
    stable_evidence_identity,
)
from app.services.raw_claim_persistence import _core_payload, _model_core_payload


_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_LIST_LIMIT = 1000
_MAPPING_UNIQUE_CONSTRAINTS = frozenset(
    {
        "uq_graph_claim_mappings_result_fingerprint",
        "graph_claim_mappings_pkey",
    }
)
_AUTHORITY_UNIQUE_CONSTRAINTS = frozenset(
    {
        "uq_graph_mapping_authority_fingerprint",
        "graph_mapping_authority_snapshots_pkey",
    }
)


class CanonicalMappingPersistenceError(ValueError):
    """Base error for a rejected mapping persistence operation."""


class CanonicalMappingScopeError(CanonicalMappingPersistenceError):
    """A mapping result does not match its immutable source scope."""


class CanonicalMappingConflictError(CanonicalMappingPersistenceError):
    """An immutable mapping identity conflicts with an existing row."""


class CanonicalMappingAuthorityError(CanonicalMappingPersistenceError):
    """The external immutable registry authority cannot be proven."""


class CanonicalMappingRemapBlockedError(CanonicalMappingPersistenceError):
    """A positive remap was not proven by the repository predecessor lookup."""


def _strict_json_int(value: Any, *, field: str) -> None:
    if type(value) is not int:
        raise CanonicalMappingScopeError(f"stored {field} must be a JSON integer")


def _strict_json_number(value: Any, *, field: str) -> None:
    if type(value) not in (int, float) or isinstance(value, bool):
        raise CanonicalMappingScopeError(f"stored {field} must be a finite JSON number")
    if isinstance(value, float) and not math.isfinite(value):
        raise CanonicalMappingScopeError(f"stored {field} must be a finite JSON number")


def _strict_locator_numeric_payload(locator: Any, *, field: str) -> None:
    if locator is None:
        return
    if not isinstance(locator, Mapping):
        raise CanonicalMappingScopeError(f"stored {field} locator is invalid")
    if "revision_no" in locator:
        _strict_json_int(locator["revision_no"], field=f"{field}.revision_no")
    if "ordinal" in locator:
        _strict_json_int(locator["ordinal"], field=f"{field}.ordinal")
    source = locator.get("source")
    if source is None:
        return
    if not isinstance(source, Mapping):
        raise CanonicalMappingScopeError(f"stored {field}.source locator is invalid")
    for name in ("page", "row"):
        span = source.get(name)
        if span is None:
            continue
        if not isinstance(span, Mapping):
            raise CanonicalMappingScopeError(f"stored {field}.source.{name} is invalid")
        for bound in ("start", "end"):
            if bound in span:
                _strict_json_int(span[bound], field=f"{field}.source.{name}.{bound}")
    table = source.get("table")
    if table is not None:
        if not isinstance(table, Mapping):
            raise CanonicalMappingScopeError(f"stored {field}.source.table is invalid")
        if "index" in table:
            _strict_json_int(table["index"], field=f"{field}.source.table.index")
    column = source.get("column")
    if column is not None:
        if not isinstance(column, Mapping):
            raise CanonicalMappingScopeError(f"stored {field}.source.column is invalid")
        for bound in ("start", "end"):
            value = column.get(bound)
            if value is not None and type(value) not in (int, str):
                raise CanonicalMappingScopeError(
                    f"stored {field}.source.column.{bound} has an invalid JSON type"
                )
    text_span = source.get("text")
    if text_span is not None:
        if not isinstance(text_span, Mapping):
            raise CanonicalMappingScopeError(f"stored {field}.source.text is invalid")
        for bound in ("start", "end"):
            if bound in text_span:
                _strict_json_int(text_span[bound], field=f"{field}.source.text.{bound}")
        ranges = text_span.get("ranges")
        if ranges is not None:
            if not isinstance(ranges, list):
                raise CanonicalMappingScopeError(f"stored {field}.source.text.ranges is invalid")
            for index, item in enumerate(ranges):
                if not isinstance(item, Mapping):
                    raise CanonicalMappingScopeError(
                        f"stored {field}.source.text.ranges[{index}] is invalid"
                    )
                for bound in ("start", "end"):
                    if bound in item:
                        _strict_json_int(
                            item[bound],
                            field=f"{field}.source.text.ranges[{index}].{bound}",
                        )
    bbox = source.get("bbox")
    if bbox is not None:
        if not isinstance(bbox, Mapping):
            raise CanonicalMappingScopeError(f"stored {field}.source.bbox is invalid")
        for name in ("x_min", "y_min", "x_max", "y_max", "width", "height"):
            if name in bbox and bbox[name] is not None:
                _strict_json_number(bbox[name], field=f"{field}.source.bbox.{name}")


def _validate_persisted_raw_json_types(
    core: GraphRawClaim,
    occurrence: GraphRawClaimOccurrence,
) -> None:
    """Reject JSON values before RawClaim/Evidence Pydantic coercion.

    This intentionally walks only fields with numeric/boolean meaning in the
    actual contracts.  Qualifier payloads are domain-neutral JSON and must not
    be scanned by field-name heuristics.
    """
    _strict_json_int(core.revision_no, field="raw_claim.revision_no")
    negation = core.negation
    if not isinstance(negation, Mapping) or type(negation.get("value")) is not bool:
        raise CanonicalMappingScopeError("stored raw claim negation.value must be a JSON boolean")
    evidence_refs = occurrence.evidence_refs
    if not isinstance(evidence_refs, list):
        raise CanonicalMappingScopeError("stored raw claim evidence_refs must be a JSON array")
    for index, reference in enumerate(evidence_refs):
        if not isinstance(reference, Mapping):
            raise CanonicalMappingScopeError(f"stored evidence reference {index} is invalid")
        _strict_json_int(reference.get("revision_no"), field=f"evidence_refs[{index}].revision_no")
        _strict_locator_numeric_payload(reference.get("locator"), field=f"evidence_refs[{index}]")
        source_span = reference.get("source_span")
        if source_span is not None:
            if not isinstance(source_span, Mapping):
                raise CanonicalMappingScopeError(f"stored evidence_refs[{index}].source_span is invalid")
            for bound in ("start", "end"):
                if bound in source_span:
                    _strict_json_int(
                        source_span[bound],
                        field=f"evidence_refs[{index}].source_span.{bound}",
                    )
            ranges = source_span.get("ranges")
            if ranges is not None:
                if not isinstance(ranges, list):
                    raise CanonicalMappingScopeError(
                        f"stored evidence_refs[{index}].source_span.ranges is invalid"
                    )
                for range_index, item in enumerate(ranges):
                    if not isinstance(item, Mapping):
                        raise CanonicalMappingScopeError(
                            f"stored evidence_refs[{index}].source_span.ranges[{range_index}] is invalid"
                        )
                    for bound in ("start", "end"):
                        if bound in item:
                            _strict_json_int(
                                item[bound],
                                field=(
                                    f"evidence_refs[{index}].source_span.ranges["
                                    f"{range_index}].{bound}"
                                ),
                            )


@dataclass(frozen=True, slots=True)
class PersistedMappingRegistrySnapshot:
    """Pure result of a caller-owned immutable registry authority lookup.

    The repository never treats the M0 snapshot hash as external proof.  A
    future authority loader must obtain this value from immutable persistence;
    this boundary only checks that the returned projection and its source
    identity match the result being persisted.
    """

    snapshot: MappingAuthorizationRegistrySnapshotV1
    authority_id: UUID
    authority_fingerprint: str


_MISSING_AUTHORITY_ID = UUID("00000000-0000-0000-0000-000000000059")
_MISSING_AUTHORITY_FINGERPRINT = "0" * 64
_AUTHORITY_SCHEMA_VERSION = "mapping_authority_snapshot_v1"
_AUTHORITY_SOURCE_TABLE = "graph_extraction_jobs"
_AUTHORITY_SOURCE_VERSION = "ontology_snapshot_v1"


def _authority_scope(snapshot: MappingAuthorizationRegistrySnapshotV1) -> MappingScopeV1:
    return snapshot.scope


def _authority_fingerprint(
    *,
    snapshot: MappingAuthorizationRegistrySnapshotV1,
    source_table: str,
    source_id: UUID,
    source_kind: str,
    source_key: str,
    source_version: str,
    source_hash: str,
) -> str:
    payload = {
        "authority_schema_version": _AUTHORITY_SCHEMA_VERSION,
        "scope": snapshot.scope.model_dump(mode="json"),
        "ontology_snapshot_hash": snapshot.ontology_snapshot_hash,
        "registry_snapshot_hash": snapshot.registry_snapshot_hash,
        "registry_snapshot": snapshot.model_dump(mode="json"),
        "source_table": source_table,
        "source_id": str(source_id),
        "source_kind": source_kind,
        "source_key": source_key,
        "source_version": source_version,
        "source_hash": source_hash,
    }
    return hashlib.sha256(canonical_mapping_json_value(payload).encode("utf-8")).hexdigest()


def _scope_from_snapshot(snapshot: MappingAuthorizationRegistrySnapshotV1) -> MappingScopeV1:
    return MappingScopeV1.model_validate(snapshot.scope.model_dump(mode="json"))


def deterministic_mapping_authority_id(
    *,
    source_id: UUID,
    source_hash: str,
    registry_snapshot_hash: str,
) -> UUID:
    """Derive the authority row id from the repository-owned source projection."""
    return uuid5(
        CANONICAL_MAPPING_UUID_NAMESPACE,
        f"mapping_authority_v1:{source_id}:{source_hash}:{registry_snapshot_hash}",
    )


def _repository_authority_material(
    job: GraphExtractionJob,
    *,
    ontology_version_id: UUID,
    ontology_contract_version: str,
    expected_scope: MappingScopeV1 | None = None,
) -> tuple[MappingAuthorizationRegistrySnapshotV1, str, UUID, str, str, str, str]:
    """Load the only real authority root: the immutable job ontology snapshot.

    A caller may supply a pure snapshot as a convenience, but a real session
    accepts it only when it exactly matches this persisted projection.
    """
    persisted = getattr(job, "ontology_snapshot", None)
    source_hash = getattr(job, "ontology_snapshot_hash", None)
    if (
        not isinstance(persisted, Mapping)
        or not isinstance(source_hash, str)
        or _SHA256.fullmatch(source_hash) is None
        or source_hash == "0" * 64
    ):
        raise CanonicalMappingAuthorityError("repository ontology authority is missing")
    registry_payload = persisted.get("authorization_registry_snapshot")
    frozen_payload = persisted.get("frozen_ontology")
    if not isinstance(registry_payload, Mapping) or not isinstance(frozen_payload, Mapping):
        raise CanonicalMappingAuthorityError("repository ontology authority projection is incomplete")
    try:
        registry = MappingAuthorizationRegistrySnapshotV1.model_validate(
            json.loads(canonical_mapping_json_value(registry_payload))
        )
        frozen = FrozenOntologySnapshotV1.model_validate(
            json.loads(canonical_mapping_json_value(frozen_payload))
        )
    except Exception as exc:
        raise CanonicalMappingAuthorityError("repository ontology authority projection is invalid") from exc
    scope = registry.scope
    if (
        not _same_uuid(scope.job_id, job.id)
        or not _same_uuid(scope.library_id, job.library_id)
        or not _same_uuid(scope.document_id, job.document_id)
        or not _same_uuid(scope.document_revision_id, job.document_revision_id)
        or not _same_uuid(job.ontology_version_id, ontology_version_id)
        or frozen.ontology_version_id != ontology_version_id
        or frozen.ontology_contract_version != ontology_contract_version
        or frozen.ontology_snapshot_hash != source_hash
        or registry.ontology_snapshot_hash != source_hash
        or registry.registry_snapshot_hash in (None, "0" * 64)
    ):
        raise CanonicalMappingAuthorityError("repository ontology authority scope conflicts")
    if expected_scope is not None and scope != expected_scope:
        raise CanonicalMappingAuthorityError("repository ontology authority scope does not match the mapping")
    return (
        registry,
        _AUTHORITY_SOURCE_TABLE,
        job.id,
        "graph_extraction_job",
        str(job.id),
        _AUTHORITY_SOURCE_VERSION,
        source_hash,
    )


class MappingRegistryAuthorityLoader(Protocol):
    async def load(
        self,
        db: AsyncSession,
        *,
        scope: MappingScopeV1,
        ontology_version_id: UUID,
        registry_snapshot_hash: str,
    ) -> PersistedMappingRegistrySnapshot | None: ...


RegistryAuthorityLoader = Callable[
    [AsyncSession, MappingScopeV1, UUID, str],
    Awaitable[PersistedMappingRegistrySnapshot | None],
]


@dataclass(frozen=True, slots=True)
class CanonicalMappingWriteResult:
    mapping: CanonicalMappingV1
    mapping_created: bool


def _same_uuid(left: Any, right: UUID) -> bool:
    return left == right or str(left) == str(right)


def _scope(result: CanonicalMappingV1) -> MappingScopeV1:
    return MappingScopeV1(
        library_id=result.library_id,
        document_id=result.document_id,
        document_revision_id=result.document_revision_id,
        revision_no=result.revision_no,
        job_id=result.job_id,
        extraction_unit_id=result.extraction_unit_id,
        claim_id=result.claim_id,
        extraction_occurrence_id=result.extraction_occurrence_id,
    )


def _authoritative_result(
    authoritative_input: CanonicalMappingInputV1,
    result: CanonicalMappingV1,
) -> CanonicalMappingV1:
    if not isinstance(authoritative_input, CanonicalMappingInputV1):
        raise TypeError("canonical mapping persistence requires authoritative input")
    if not isinstance(result, CanonicalMappingV1):
        raise TypeError("canonical mapping persistence requires CanonicalMappingV1")
    try:
        payload = canonical_mapping_json(authoritative_input, result)
        return CanonicalMappingV1.model_validate(
            json.loads(canonical_mapping_json_value(json.loads(payload)))
        )
    except Exception as exc:
        raise CanonicalMappingPersistenceError(
            "canonical mapping result failed authoritative validation"
        ) from exc


async def _load_registry_authority(
    db: AsyncSession,
    *,
    result: CanonicalMappingV1,
    authority_id: UUID | None,
    loader: MappingRegistryAuthorityLoader | RegistryAuthorityLoader | None,
) -> PersistedMappingRegistrySnapshot:
    scope = _scope(result)
    loaded: PersistedMappingRegistrySnapshot | None = None
    if authority_id is not None:
        try:
            row = await db.get(GraphMappingAuthoritySnapshot, authority_id)
        except Exception as exc:
            raise CanonicalMappingAuthorityError(
                "immutable registry authority lookup failed"
            ) from exc
        if row is None:
            raise CanonicalMappingAuthorityError("immutable registry authority row is missing")
        if not isinstance(authority_id, UUID):
            raise CanonicalMappingAuthorityError("immutable registry authority id is invalid")
        job = await db.get(GraphExtractionJob, result.job_id)
        if job is None:
            raise CanonicalMappingAuthorityError("immutable registry authority source job is missing")
        try:
            snapshot = MappingAuthorizationRegistrySnapshotV1.model_validate(
                json.loads(canonical_mapping_json_value(row.registry_snapshot))
            )
        except Exception as exc:
            raise CanonicalMappingAuthorityError(
                "immutable registry authority row projection is invalid"
            ) from exc
        (
            repository_snapshot,
            source_table,
            source_id,
            source_kind,
            source_key,
            source_version,
            source_hash,
        ) = _repository_authority_material(
            job,
            ontology_version_id=result.ontology_version_id,
            ontology_contract_version=result.ontology_contract_version,
            expected_scope=snapshot.scope,
        )
        if repository_snapshot != snapshot:
            raise CanonicalMappingAuthorityError(
                "immutable registry authority is not bound to the repository source"
            )
        expected_fingerprint = _authority_fingerprint(
            snapshot=snapshot,
            source_table=source_table,
            source_id=source_id,
            source_kind=source_kind,
            source_key=source_key,
            source_version=source_version,
            source_hash=source_hash,
        )
        expected_authority_id = deterministic_mapping_authority_id(
            source_id=source_id,
            source_hash=source_hash,
            registry_snapshot_hash=snapshot.registry_snapshot_hash or "",
        )
        if row.authority_id != expected_authority_id or row.authority_fingerprint != expected_fingerprint:
            raise CanonicalMappingAuthorityError("immutable registry authority fingerprint mismatch")
        row_scope = MappingScopeV1(
            library_id=row.library_id,
            document_id=row.document_id,
            document_revision_id=row.document_revision_id,
            revision_no=row.revision_no,
            job_id=row.job_id,
            extraction_unit_id=row.extraction_unit_id,
            claim_id=row.claim_id,
            extraction_occurrence_id=row.extraction_occurrence_id,
        )
        if (
            row_scope != snapshot.scope
            or row.registry_snapshot_hash != snapshot.registry_snapshot_hash
            or not _same_uuid(row.ontology_version_id, result.ontology_version_id)
            or row.ontology_snapshot_hash != result.ontology_snapshot_hash
            or row.ontology_contract_version != result.ontology_contract_version
            or row.source_table != source_table
            or row.source_id != source_id
            or row.source_kind != source_kind
            or row.source_key != source_key
            or row.source_version != source_version
            or row.source_hash != source_hash
        ):
            raise CanonicalMappingAuthorityError("immutable registry authority row scope conflicts")
        loaded = PersistedMappingRegistrySnapshot(
            snapshot=snapshot,
            authority_id=row.authority_id,
            authority_fingerprint=row.authority_fingerprint,
        )
    elif not isinstance(db, AsyncSession) and loader is not None:
        # Legacy DB-free fake repositories retain the old projection adapter.
        # A real AsyncSession must use authority_id and the immutable table.
        try:
            if hasattr(loader, "load"):
                loaded = await loader.load(
                    db,
                    scope=scope,
                    ontology_version_id=result.ontology_version_id,
                    registry_snapshot_hash=result.authorization_registry_snapshot.registry_snapshot_hash,
                )
            else:
                loaded = await loader(
                    db,
                    scope,
                    result.ontology_version_id,
                    result.authorization_registry_snapshot.registry_snapshot_hash,
                )
        except Exception as exc:
            raise CanonicalMappingAuthorityError(
                "immutable registry authority lookup failed"
            ) from exc
    else:
        raise CanonicalMappingAuthorityError(
            "authority_id is required for a real persistence session"
        )
    if not isinstance(loaded, PersistedMappingRegistrySnapshot):
        raise CanonicalMappingAuthorityError("immutable registry authority projection is invalid")
    if not isinstance(loaded.authority_id, UUID) or not _SHA256.fullmatch(
        loaded.authority_fingerprint
    ):
        raise CanonicalMappingAuthorityError(
            "immutable registry authority identity is invalid"
        )
    expected = result.authorization_registry_snapshot
    if loaded.snapshot != expected:
        raise CanonicalMappingAuthorityError(
            "immutable registry authority does not match the mapping result"
        )
    if loaded.snapshot.registry_snapshot_hash != expected.registry_snapshot_hash:
        raise CanonicalMappingAuthorityError(
            "immutable registry authority hash does not match the mapping result"
        )
    if loaded.snapshot.scope != scope:
        raise CanonicalMappingAuthorityError("immutable registry authority scope does not match the mapping result")
    if loaded.snapshot.ontology_snapshot_hash != result.ontology_snapshot_hash:
        raise CanonicalMappingAuthorityError("immutable registry authority ontology does not match the mapping result")
    return loaded


async def create_or_get_mapping_authority_snapshot(
    db: AsyncSession,
    snapshot: MappingAuthorizationRegistrySnapshotV1 | None = None,
    *,
    job_id: UUID,
    ontology_version_id: UUID,
    ontology_contract_version: str,
    authority_id: UUID | None = None,
    source_kind: str | None = None,
    source_key: str | None = None,
    source_version: str | None = None,
    source_hash: str | None = None,
) -> PersistedMappingRegistrySnapshot:
    """Register the repository-owned registry projection for one job.

    Real sessions never accept a caller-owned registry manifest or source
    hash as authority.  They are reconstructed from the immutable job
    ontology snapshot and the optional value is only an equality assertion.
    """
    if not isinstance(db, AsyncSession):
        raise CanonicalMappingAuthorityError(
            "real authority registration requires an AsyncSession"
        )
    if not isinstance(job_id, UUID):
        raise TypeError("job_id must be a UUID")
    job = await db.get(GraphExtractionJob, job_id)
    if job is None:
        raise CanonicalMappingAuthorityError("authority source job is missing")
    (
        repository_snapshot,
        repository_source_table,
        repository_source_id,
        repository_source_kind,
        repository_source_key,
        repository_source_version,
        repository_source_hash,
    ) = _repository_authority_material(
        job,
        ontology_version_id=ontology_version_id,
        ontology_contract_version=ontology_contract_version,
    )
    if snapshot is not None:
        try:
            supplied_snapshot = MappingAuthorizationRegistrySnapshotV1.model_validate(
                json.loads(canonical_mapping_json_value(snapshot.model_dump(mode="json")))
            )
        except Exception as exc:
            raise CanonicalMappingAuthorityError("caller authority projection is invalid") from exc
        if supplied_snapshot != repository_snapshot:
            raise CanonicalMappingAuthorityError(
                "caller authority projection is not the repository snapshot"
            )
    snapshot = repository_snapshot
    expected_authority_id = deterministic_mapping_authority_id(
        source_id=repository_source_id,
        source_hash=repository_source_hash,
        registry_snapshot_hash=snapshot.registry_snapshot_hash or "",
    )
    if authority_id is None:
        authority_id = expected_authority_id
    if authority_id != expected_authority_id:
        raise CanonicalMappingAuthorityError("authority id is not repository-derived")
    supplied_source = {
        "source_kind": source_kind,
        "source_key": source_key,
        "source_version": source_version,
        "source_hash": source_hash,
    }
    expected_source = {
        "source_kind": repository_source_kind,
        "source_key": repository_source_key,
        "source_version": repository_source_version,
        "source_hash": repository_source_hash,
    }
    if any(value is not None and value != expected_source[field] for field, value in supplied_source.items()):
        raise CanonicalMappingAuthorityError("caller authority provenance is not repository-derived")
    scope = snapshot.scope
    parents = (
        await db.get(Library, scope.library_id),
        await db.get(Document, scope.document_id),
        await db.get(DocumentRevision, scope.document_revision_id),
        job,
        await db.get(GraphExtractionUnit, scope.extraction_unit_id),
        await db.get(GraphRawClaim, scope.claim_id),
    )
    ontology = await db.get(OntologyVersion, ontology_version_id)
    if any(row is None for row in parents) or ontology is None:
        raise CanonicalMappingAuthorityError("authority scope dependency is missing")
    revision = parents[2]
    job = parents[3]
    unit = parents[4]
    claim = parents[5]
    if (
        not _same_uuid(revision.library_id, scope.library_id)
        or not _same_uuid(revision.document_id, scope.document_id)
        or revision.revision_no != scope.revision_no
        or not _same_uuid(job.library_id, scope.library_id)
        or not _same_uuid(job.document_id, scope.document_id)
        or not _same_uuid(job.document_revision_id, scope.document_revision_id)
        or not _same_uuid(job.id, job_id)
        or not _same_uuid(job.ontology_version_id, ontology_version_id)
        or not _same_uuid(unit.job_id, scope.job_id)
        or not _same_uuid(unit.library_id, scope.library_id)
        or not _same_uuid(unit.document_revision_id, scope.document_revision_id)
        or not _same_uuid(claim.library_id, scope.library_id)
        or not _same_uuid(claim.document_id, scope.document_id)
        or not _same_uuid(claim.document_revision_id, scope.document_revision_id)
        or claim.revision_no != scope.revision_no
        or not _same_uuid(ontology.id, ontology_version_id)
        or not _same_uuid(ontology.library_id, scope.library_id)
        or getattr(ontology, "ontology_snapshot_hash", snapshot.ontology_snapshot_hash)
        != snapshot.ontology_snapshot_hash
    ):
        raise CanonicalMappingAuthorityError("authority scope dependency conflicts")
    occurrence = (
        await db.execute(
            select(GraphRawClaimOccurrence).where(
                GraphRawClaimOccurrence.extraction_occurrence_id == scope.extraction_occurrence_id
            )
        )
    ).scalar_one_or_none()
    if occurrence is None or not _same_uuid(occurrence.claim_id, scope.claim_id) or not _same_uuid(occurrence.job_id, scope.job_id) or not _same_uuid(occurrence.extraction_unit_id, scope.extraction_unit_id):
        raise CanonicalMappingAuthorityError("authority occurrence scope conflicts")
    fingerprint = _authority_fingerprint(
        snapshot=snapshot,
        source_table=repository_source_table,
        source_id=repository_source_id,
        source_kind=repository_source_kind,
        source_key=repository_source_key,
        source_version=repository_source_version,
        source_hash=repository_source_hash,
    )
    row = await db.get(GraphMappingAuthoritySnapshot, authority_id)
    if row is not None:
        if (
            row.authority_fingerprint != fingerprint
            or row.registry_snapshot != snapshot.model_dump(mode="json")
            or row.library_id != scope.library_id
            or row.document_id != scope.document_id
            or row.document_revision_id != scope.document_revision_id
            or row.job_id != scope.job_id
            or row.extraction_unit_id != scope.extraction_unit_id
            or row.claim_id != scope.claim_id
            or row.extraction_occurrence_id != scope.extraction_occurrence_id
            or row.source_table != repository_source_table
            or row.source_id != repository_source_id
            or row.source_kind != repository_source_kind
            or row.source_key != repository_source_key
            or row.source_version != repository_source_version
            or row.source_hash != repository_source_hash
        ):
            raise CanonicalMappingAuthorityError("authority id already contains a different immutable projection")
        return PersistedMappingRegistrySnapshot(snapshot, row.authority_id, row.authority_fingerprint)
    row = GraphMappingAuthoritySnapshot(
        authority_id=authority_id,
        authority_fingerprint=fingerprint,
        authority_schema_version=_AUTHORITY_SCHEMA_VERSION,
        library_id=scope.library_id,
        document_id=scope.document_id,
        document_revision_id=scope.document_revision_id,
        revision_no=scope.revision_no,
        job_id=scope.job_id,
        extraction_unit_id=scope.extraction_unit_id,
        claim_id=scope.claim_id,
        extraction_occurrence_id=scope.extraction_occurrence_id,
        ontology_version_id=ontology_version_id,
        ontology_snapshot_hash=snapshot.ontology_snapshot_hash,
        ontology_contract_version=ontology_contract_version,
        registry_snapshot_hash=snapshot.registry_snapshot_hash,
        registry_snapshot=snapshot.model_dump(mode="json"),
        source_table=repository_source_table,
        source_id=repository_source_id,
        source_kind=repository_source_kind,
        source_key=repository_source_key,
        source_version=repository_source_version,
        source_hash=repository_source_hash,
    )
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        if not _is_exact_pg_unique_violation(exc, _AUTHORITY_UNIQUE_CONSTRAINTS):
            raise
        existing = await db.get(GraphMappingAuthoritySnapshot, authority_id)
        if existing is None or existing.authority_fingerprint != fingerprint:
            raise CanonicalMappingAuthorityError("authority insert violated an immutable constraint") from exc
        row = existing
    return PersistedMappingRegistrySnapshot(snapshot, row.authority_id, row.authority_fingerprint)


def _rebuild_raw_claim_from_rows(
    core: GraphRawClaim,
    occurrence: GraphRawClaimOccurrence,
) -> RawClaimV1 | None:
    """Reconstruct the complete RawClaim from its immutable core/occurrence rows."""
    required_occurrence_fields = (
        "extractor_version",
        "prompt_version",
        "model_provider",
        "model_name",
        "model_config_hash",
        "prompt_content_hash",
        "parser_version",
        "normalization_rule_version",
        "ontology_snapshot_hash",
        "evidence_refs",
    )
    if any(not hasattr(occurrence, field) for field in required_occurrence_fields):
        return None
    _validate_persisted_raw_json_types(core, occurrence)
    try:
        occurrence_references = tuple(
            EvidenceReferenceV1.model_validate(reference)
            for reference in occurrence.evidence_refs
        )
        evidence_identities = [stable_evidence_identity(reference) for reference in occurrence_references]
        if len(evidence_identities) != len(set(evidence_identities)):
            raise CanonicalMappingScopeError("stored raw claim evidence identities are duplicated")
        evidence_ref_by_identity = dict(zip(evidence_identities, (reference.ref_id for reference in occurrence_references)))

        def restore_evidence_ref(value: Any) -> Any:
            if not isinstance(value, dict):
                return value
            restored = dict(value)
            ref_id = restored.get("evidence_ref")
            if ref_id is not None:
                restored["evidence_ref"] = evidence_ref_by_identity.get(ref_id, ref_id)
            return restored

        source_mention = restore_evidence_ref(core.source_mention)
        target_mention = restore_evidence_ref(core.target_mention)
        negation = restore_evidence_ref(core.negation)
        modality = restore_evidence_ref(core.modality)
        qualifiers = [restore_evidence_ref(item) for item in (core.qualifiers or [])]
        valid_time = restore_evidence_ref(core.valid_time)
        effective_time = restore_evidence_ref(core.effective_time)
    except Exception as exc:
        raise CanonicalMappingScopeError("stored raw claim evidence projection is invalid") from exc
    payload = {
        "claim_schema_version": core.claim_schema_version,
        "claim_id": core.id,
        "library_id": core.library_id,
        "document_id": core.document_id,
        "document_revision_id": core.document_revision_id,
        "revision_no": core.revision_no,
        "job_id": occurrence.job_id,
        "extraction_unit_id": occurrence.extraction_unit_id,
        "source_mention": source_mention,
        "raw_predicate": core.raw_predicate,
        "target_mention": target_mention,
        "surface_direction": core.surface_direction,
        "negation": negation,
        "modality": modality,
        "qualifiers": qualifiers,
        "valid_time": valid_time,
        "effective_time": effective_time,
        "evidence_refs": [reference.model_dump(mode="json") for reference in occurrence_references],
        "extractor_version": occurrence.extractor_version,
        "prompt_version": occurrence.prompt_version,
        "model_provider": occurrence.model_provider,
        "model_name": occurrence.model_name,
        "model_config_hash": occurrence.model_config_hash,
        "prompt_content_hash": occurrence.prompt_content_hash,
        "parser_version": occurrence.parser_version,
        "normalization_rule_version": occurrence.normalization_rule_version,
        "ontology_snapshot_hash": occurrence.ontology_snapshot_hash,
        "content_scoped_claim_fingerprint": core.content_scoped_claim_fingerprint,
        "extraction_occurrence_id": occurrence.extraction_occurrence_id,
        "extraction_occurrence_fingerprint": occurrence.extraction_occurrence_fingerprint,
    }
    try:
        rebuilt = RawClaimV1.model_validate(payload)
        if _model_core_payload(core) != _core_payload(rebuilt):
            raise CanonicalMappingScopeError("raw claim core projection conflicts")
        if occurrence.evidence_refs != rebuilt.model_dump(mode="json")["evidence_refs"]:
            raise CanonicalMappingScopeError("raw claim occurrence evidence conflicts")
        return rebuilt
    except CanonicalMappingScopeError:
        raise
    except Exception as exc:
        raise CanonicalMappingScopeError("stored raw claim projection is invalid") from exc


def _rebuild_decision_from_row(
    row: GraphClaimDecision,
    claim: RawClaimV1,
) -> ClaimDecisionProjectionV1:
    """Rebuild and fully bind a persisted decision to its RawClaim."""
    try:
        payload = {
            "decision_id": row.decision_id,
            "decision_fingerprint": row.decision_fingerprint,
            "decision_schema_version": row.decision_schema_version,
            "decision_version": row.decision_version,
            "library_id": row.library_id,
            "document_id": row.document_id,
            "document_revision_id": row.document_revision_id,
            "revision_no": row.revision_no,
            "claim_id": row.claim_id,
            "extraction_occurrence_id": row.extraction_occurrence_id,
            "decision_kind": row.decision_kind,
            "status": row.status,
            "reason_code": row.reason_code,
            "created_by_kind": row.created_by_kind,
            "producer_key": row.producer_key,
            "producer_version": row.producer_version,
            "created_at": row.created_at,
            "proposal": row.proposal,
        }
        projection = ClaimDecisionProjectionV1.model_validate(payload)
        projection = ClaimDecisionProjectionV1.model_validate(
            json.loads(canonical_claim_decision_json(projection))
        )
        expected_fingerprint = claim_decision_fingerprint(projection)
        expected_id = deterministic_decision_id(CLAIM_DECISION_ID_NAMESPACE, expected_fingerprint)
        if projection.decision_fingerprint != expected_fingerprint or projection.decision_id != expected_id:
            raise CanonicalMappingScopeError("stored decision identity is not deterministic")
        return _validate_decision_projection_against_claim(projection, claim)
    except Exception as exc:
        raise CanonicalMappingScopeError("stored decision projection is invalid") from exc


async def _require_scope_dependencies(
    db: AsyncSession,
    result: CanonicalMappingV1,
    authoritative_input: CanonicalMappingInputV1 | None = None,
    repository_predecessor: CanonicalMappingV1 | None = None,
) -> CanonicalMappingInputV1:
    scope = _scope(result)
    library = await db.get(Library, result.library_id)
    document = await db.get(Document, result.document_id)
    revision = await db.get(DocumentRevision, result.document_revision_id)
    job = await db.get(GraphExtractionJob, result.job_id)
    unit = await db.get(GraphExtractionUnit, result.extraction_unit_id)
    claim = await db.get(GraphRawClaim, result.claim_id)
    ontology = await db.get(OntologyVersion, result.ontology_version_id)
    if any(row is None for row in (library, document, revision, job, unit, claim, ontology)):
        raise CanonicalMappingScopeError("canonical mapping scope dependency is missing")

    if (
        not _same_uuid(document.library_id, scope.library_id)
        or not _same_uuid(revision.library_id, scope.library_id)
        or not _same_uuid(revision.document_id, scope.document_id)
        or revision.revision_no != scope.revision_no
        or not _same_uuid(job.library_id, scope.library_id)
        or not _same_uuid(job.document_id, scope.document_id)
        or not _same_uuid(job.document_revision_id, scope.document_revision_id)
        or not _same_uuid(job.ontology_version_id, result.ontology_version_id)
        or job.ontology_snapshot_hash != result.ontology_snapshot_hash
        or not _same_uuid(unit.job_id, scope.job_id)
        or not _same_uuid(unit.library_id, scope.library_id)
        or not _same_uuid(unit.document_revision_id, scope.document_revision_id)
        or not _same_uuid(claim.library_id, scope.library_id)
        or not _same_uuid(claim.document_id, scope.document_id)
        or not _same_uuid(claim.document_revision_id, scope.document_revision_id)
        or claim.revision_no != scope.revision_no
        or claim.content_scoped_claim_fingerprint != result.claim_content_scoped_fingerprint
        or not _same_uuid(ontology.library_id, scope.library_id)
        or not _same_uuid(ontology.id, result.ontology_version_id)
    ):
        raise CanonicalMappingScopeError("canonical mapping scope dependency conflicts")

    occurrence = (
        await db.execute(
            select(GraphRawClaimOccurrence).where(
                GraphRawClaimOccurrence.extraction_occurrence_id
                == result.extraction_occurrence_id
            )
        )
    ).scalar_one_or_none()
    if occurrence is None:
        raise CanonicalMappingScopeError("canonical mapping occurrence is missing")
    if (
        not _same_uuid(occurrence.claim_id, result.claim_id)
        or not _same_uuid(occurrence.job_id, result.job_id)
        or not _same_uuid(occurrence.extraction_unit_id, result.extraction_unit_id)
        or occurrence.extraction_occurrence_fingerprint
        != result.extraction_occurrence_fingerprint
    ):
        raise CanonicalMappingScopeError(
            "canonical mapping occurrence crosses the result scope"
        )

    rebuilt_claim = _rebuild_raw_claim_from_rows(claim, occurrence)
    if rebuilt_claim is None:
        raise CanonicalMappingScopeError("stored raw claim projection is incomplete")
    expected_raw_json = canonical_raw_claim_json(rebuilt_claim)
    expected_raw_hash = hashlib.sha256(expected_raw_json.encode("utf-8")).hexdigest()
    if expected_raw_hash != result.raw_claim_authority_sha256:
        raise CanonicalMappingScopeError("raw claim authority fingerprint conflicts")
    expected_snapshot = RawClaimMappingSnapshotV1.from_claim(
        rebuilt_claim,
        semantic_projection=semantic_projection_from_claim(rebuilt_claim),
    )
    if result.claim_snapshot != expected_snapshot:
        raise CanonicalMappingScopeError("raw claim snapshot conflicts with stored dependency")
    if (
        rebuilt_claim.content_scoped_claim_fingerprint != result.claim_content_scoped_fingerprint
        or rebuilt_claim.extraction_occurrence_fingerprint != result.extraction_occurrence_fingerprint
    ):
        raise CanonicalMappingScopeError("raw claim fingerprints conflict with stored dependency")

    stored_decision: ClaimDecisionProjectionV1 | None = None
    if result.decision_id is None:
        if result.decision_binding is not None:
            raise CanonicalMappingScopeError("decision binding exists without a decision")
    else:
        decision = await db.get(GraphClaimDecision, result.decision_id)
        if decision is None or result.decision_binding is None:
            raise CanonicalMappingScopeError("canonical mapping decision is missing")
        binding = result.decision_binding
        if (
            not _same_uuid(decision.decision_id, binding.decision_id)
            or decision.decision_fingerprint != binding.decision_fingerprint
            or decision.decision_kind != binding.decision_kind
            or not _same_uuid(decision.library_id, scope.library_id)
            or not _same_uuid(decision.document_id, scope.document_id)
            or not _same_uuid(decision.document_revision_id, scope.document_revision_id)
            or decision.revision_no != scope.revision_no
            or not _same_uuid(decision.claim_id, result.claim_id)
            or decision.extraction_occurrence_id not in (None, result.extraction_occurrence_id)
        ):
            raise CanonicalMappingScopeError("canonical mapping decision crosses the result scope")
        required_decision_fields = (
            "decision_schema_version",
            "decision_version",
            "status",
            "reason_code",
            "created_by_kind",
            "producer_key",
            "producer_version",
            "created_at",
            "proposal",
        )
        if any(not hasattr(decision, field) for field in required_decision_fields):
            raise CanonicalMappingScopeError("stored decision projection is incomplete")
        stored_decision = _rebuild_decision_from_row(decision, rebuilt_claim)
        expected_binding = DecisionValidationBindingV1.from_projection(
            stored_decision,
            job_id=result.job_id,
            extraction_unit_id=result.extraction_unit_id,
        )
        if binding != expected_binding:
            raise CanonicalMappingScopeError("decision binding conflicts with the stored projection")
        decision_json = canonical_claim_decision_json(stored_decision)
        decision_hash = hashlib.sha256(decision_json.encode("utf-8")).hexdigest()
        if decision_hash != binding.decision_payload_sha256:
            raise CanonicalMappingScopeError("decision payload fingerprint conflicts with the stored row")

    try:
        rebuilt_input = CanonicalMappingInputV1.from_raw_claim_json(
            expected_raw_json,
            frozen_ontology=result.frozen_ontology,
            endpoint_type_binding=result.endpoint_type_binding,
            source_endpoint_resolution=result.source_endpoint_resolution,
            target_endpoint_resolution=result.target_endpoint_resolution,
            authorization_registry_snapshot=result.authorization_registry_snapshot,
            verified_evidence_ref_ids=tuple(result.mapping_evidence_ref_ids),
            evidence_attestations={
                binding.evidence_ref_id: binding.attestation
                for binding in result.evidence_bindings
            },
            authorization_provenance=result.authorization_provenance,
            decision=stored_decision,
        )
    except Exception as exc:
        raise CanonicalMappingScopeError(
            "stored canonical mapping input projection is invalid"
        ) from exc
    if authoritative_input is not None and rebuilt_input != authoritative_input:
        raise CanonicalMappingScopeError("stored dependencies do not match authoritative input")
    try:
        if result.remap_provenance is not None and result.remap_provenance.remap_generation > 0:
            if repository_predecessor is None:
                raise CanonicalMappingRemapBlockedError(
                    "repository predecessor is required for positive remap validation"
                )
            repository_authorized_canonical_mapping_json(
                rebuilt_input,
                result,
                predecessor=repository_predecessor,
            )
        else:
            _authoritative_result(rebuilt_input, result)
    except Exception as exc:
        raise CanonicalMappingScopeError(
            "stored canonical mapping semantic projection is invalid"
        ) from exc
    return rebuilt_input


def _row_from_result(
    result: CanonicalMappingV1,
    *,
    authority: PersistedMappingRegistrySnapshot | None = None,
) -> GraphClaimMapping:
    projection = result.model_dump(mode="json")
    remap = result.remap_provenance
    return GraphClaimMapping(
        mapping_result_id=result.mapping_result_id,
        mapping_attempt_id=result.mapping_attempt_id,
        mapping_attempt_fingerprint=result.mapping_attempt_fingerprint,
        mapping_result_fingerprint=result.mapping_result_fingerprint,
        mapping_schema_version=result.schema_version,
        mapping_schema_hash=result.mapping_schema_hash,
        canonical_schema_hash=result.canonical_schema_hash,
        raw_claim_authority_sha256=result.raw_claim_authority_sha256,
        mapping_version=result.mapping_version,
        authority_id=authority.authority_id if authority is not None else _MISSING_AUTHORITY_ID,
        authority_fingerprint=(
            authority.authority_fingerprint if authority is not None else _MISSING_AUTHORITY_FINGERPRINT
        ),
        library_id=result.library_id,
        document_id=result.document_id,
        document_revision_id=result.document_revision_id,
        revision_no=result.revision_no,
        job_id=result.job_id,
        extraction_unit_id=result.extraction_unit_id,
        claim_id=result.claim_id,
        claim_content_scoped_fingerprint=result.claim_content_scoped_fingerprint,
        extraction_occurrence_id=result.extraction_occurrence_id,
        extraction_occurrence_fingerprint=result.extraction_occurrence_fingerprint,
        surface_raw_predicate=result.surface_raw_predicate,
        surface_direction=result.surface_direction,
        ontology_version_id=result.ontology_version_id,
        ontology_snapshot_hash=result.ontology_snapshot_hash,
        ontology_contract_version=result.ontology_contract_version,
        decision_id=result.decision_id,
        decision_fingerprint=result.decision_fingerprint,
        decision_kind=result.decision_kind,
        outcome=result.outcome,
        reason_code=result.reason_code,
        semantic_status=result.semantic_status,
        canonical_relation_key=result.canonical_relation_key,
        canonical_direction=result.canonical_direction,
        endpoint_transform=result.endpoint_transform,
        predicate_transform=result.predicate_transform,
        mapping_confidence=result.mapping_confidence,
        remap_generation=remap.remap_generation if remap else 0,
        supersedes_mapping_result_id=remap.supersedes_mapping_result_id if remap else None,
        supersedes_mapping_result_fingerprint=(
            remap.supersedes_mapping_result_fingerprint if remap else None
        ),
        lineage_root_mapping_result_id=remap.lineage_root_mapping_result_id if remap else None,
        lineage_root_mapping_result_fingerprint=(
            remap.lineage_root_mapping_result_fingerprint if remap else None
        ),
        evidence_bindings=projection["evidence_bindings"],
        source_evidence_ref_ids=projection["source_evidence_ref_ids"],
        target_evidence_ref_ids=projection["target_evidence_ref_ids"],
        mapping_evidence_ref_ids=projection["mapping_evidence_ref_ids"],
        result_projection=projection,
        created_at=result.created_at,
    )


def _projection_from_row(row: GraphClaimMapping) -> CanonicalMappingV1:
    try:
        projection = json.loads(canonical_mapping_json_value(row.result_projection))
        result = CanonicalMappingV1.model_validate(projection)
    except Exception as exc:
        raise CanonicalMappingPersistenceError(
            "stored canonical mapping projection is invalid"
        ) from exc
    scalar_fields = {
        "mapping_result_id": "mapping_result_id",
        "mapping_attempt_id": "mapping_attempt_id",
        "mapping_attempt_fingerprint": "mapping_attempt_fingerprint",
        "mapping_result_fingerprint": "mapping_result_fingerprint",
        "mapping_version": "mapping_version",
        "mapping_schema_hash": "mapping_schema_hash",
        "canonical_schema_hash": "canonical_schema_hash",
        "raw_claim_authority_sha256": "raw_claim_authority_sha256",
        "library_id": "library_id",
        "document_id": "document_id",
        "document_revision_id": "document_revision_id",
        "revision_no": "revision_no",
        "job_id": "job_id",
        "extraction_unit_id": "extraction_unit_id",
        "claim_id": "claim_id",
        "claim_content_scoped_fingerprint": "claim_content_scoped_fingerprint",
        "extraction_occurrence_id": "extraction_occurrence_id",
        "extraction_occurrence_fingerprint": "extraction_occurrence_fingerprint",
        "surface_raw_predicate": "surface_raw_predicate",
        "surface_direction": "surface_direction",
        "ontology_version_id": "ontology_version_id",
        "ontology_snapshot_hash": "ontology_snapshot_hash",
        "ontology_contract_version": "ontology_contract_version",
        "decision_id": "decision_id",
        "decision_fingerprint": "decision_fingerprint",
        "decision_kind": "decision_kind",
        "outcome": "outcome",
        "reason_code": "reason_code",
        "semantic_status": "semantic_status",
        "canonical_relation_key": "canonical_relation_key",
        "canonical_direction": "canonical_direction",
        "endpoint_transform": "endpoint_transform",
        "predicate_transform": "predicate_transform",
        "mapping_confidence": "mapping_confidence",
        "created_at": "created_at",
    }
    for row_field, result_field in scalar_fields.items():
        if getattr(row, row_field) != getattr(result, result_field):
            raise CanonicalMappingPersistenceError(
                "stored canonical mapping scalar projection conflicts"
            )
    expected_remap_generation = result.remap_provenance.remap_generation if result.remap_provenance else 0
    if getattr(row, "remap_generation", 0) != expected_remap_generation:
        raise CanonicalMappingPersistenceError("stored canonical mapping scalar projection conflicts")
    if getattr(row, "mapping_schema_version", result.schema_version) != result.schema_version:
        raise CanonicalMappingPersistenceError("stored canonical mapping schema version conflicts")
    expected_json_fields = {
        "evidence_bindings": [item.model_dump(mode="json") for item in result.evidence_bindings],
        "source_evidence_ref_ids": list(result.source_evidence_ref_ids),
        "target_evidence_ref_ids": list(result.target_evidence_ref_ids),
        "mapping_evidence_ref_ids": list(result.mapping_evidence_ref_ids),
    }
    for field, expected in expected_json_fields.items():
        if getattr(row, field) != expected:
            raise CanonicalMappingPersistenceError("stored canonical mapping JSON projection conflicts")
    remap = result.remap_provenance
    if (
        getattr(row, "supersedes_mapping_result_id", None)
        != (remap.supersedes_mapping_result_id if remap else None)
        or getattr(row, "supersedes_mapping_result_fingerprint", None)
        != (remap.supersedes_mapping_result_fingerprint if remap else None)
        or getattr(row, "lineage_root_mapping_result_id", None)
        != (remap.lineage_root_mapping_result_id if remap else None)
        or getattr(row, "lineage_root_mapping_result_fingerprint", None)
        != (remap.lineage_root_mapping_result_fingerprint if remap else None)
    ):
        raise CanonicalMappingPersistenceError("stored canonical mapping remap projection conflicts")
    return result


async def _validate_stored_row_dependencies(
    db: AsyncSession,
    row: GraphClaimMapping,
    result: CanonicalMappingV1,
    *,
    authority: PersistedMappingRegistrySnapshot | None = None,
) -> CanonicalMappingV1:
    """Rebind a stored row to authority and all immutable source projections."""
    if isinstance(db, AsyncSession) and authority is None:
        source_job = await db.get(GraphExtractionJob, result.job_id)
        if source_job is None or source_job.ontology_snapshot_hash != result.ontology_snapshot_hash:
            raise CanonicalMappingScopeError("stored mapping source scope conflicts")
        authority = await _load_registry_authority(
            db,
            result=result,
            authority_id=row.authority_id,
            loader=None,
        )
        if row.authority_fingerprint != authority.authority_fingerprint:
            raise CanonicalMappingAuthorityError("stored mapping authority fingerprint conflicts")
    if authority is not None and getattr(row, "authority_fingerprint", authority.authority_fingerprint) != authority.authority_fingerprint:
        raise CanonicalMappingAuthorityError("stored mapping authority fingerprint conflicts")
    remap = result.remap_provenance
    _validate_m3_remap_reason(result)
    predecessor_row: GraphClaimMapping | None = None
    predecessor: CanonicalMappingV1 | None = None
    if remap is not None and remap.remap_generation > 0:
        if authority is None:
            raise CanonicalMappingRemapBlockedError("positive remap authority is unavailable")
        predecessor_row = await _find_predecessor(db, result, for_update=False)
        predecessor = _projection_from_row(predecessor_row)
        await _validate_stored_row_dependencies(
            db,
            predecessor_row,
            predecessor,
            authority=authority,
        )
        _validate_remap_authority_continuity(result, predecessor_row, predecessor, authority)
        try:
            _validate_repository_remap_predecessor(result, predecessor)
        except Exception as exc:
            raise CanonicalMappingRemapBlockedError(
                "stored positive remap predecessor validation failed"
            ) from exc
    await _require_scope_dependencies(db, result, repository_predecessor=predecessor)
    return result


async def _find_identity(
    db: AsyncSession,
    result: CanonicalMappingV1,
) -> tuple[GraphClaimMapping | None, GraphClaimMapping | None]:
    by_id = await db.get(GraphClaimMapping, result.mapping_result_id)
    by_fingerprint = (
        await db.execute(
            select(GraphClaimMapping).where(
                GraphClaimMapping.mapping_result_fingerprint == result.mapping_result_fingerprint,
                GraphClaimMapping.library_id == result.library_id,
                GraphClaimMapping.document_revision_id == result.document_revision_id,
            )
        )
    ).scalar_one_or_none()
    return by_id, by_fingerprint


async def _find_predecessor(
    db: AsyncSession,
    result: CanonicalMappingV1,
    *,
    for_update: bool = False,
) -> GraphClaimMapping:
    remap = result.remap_provenance
    if remap is None or remap.remap_generation == 0:
        raise CanonicalMappingRemapBlockedError("positive remap predecessor is missing")
    try:
        predecessor_id = remap.supersedes_mapping_result_id
        predecessor_fingerprint = remap.supersedes_mapping_result_fingerprint
    except AttributeError as exc:
        raise CanonicalMappingRemapBlockedError("positive remap envelope is incomplete") from exc
    if predecessor_id is None or predecessor_fingerprint is None:
        raise CanonicalMappingRemapBlockedError("positive remap predecessor identity is missing")
    statement = select(GraphClaimMapping).where(
        GraphClaimMapping.mapping_result_id == predecessor_id,
        GraphClaimMapping.mapping_result_fingerprint == predecessor_fingerprint,
        GraphClaimMapping.library_id == result.library_id,
        GraphClaimMapping.document_id == result.document_id,
        GraphClaimMapping.document_revision_id == result.document_revision_id,
        GraphClaimMapping.revision_no == result.revision_no,
        GraphClaimMapping.job_id == result.job_id,
        GraphClaimMapping.extraction_unit_id == result.extraction_unit_id,
        GraphClaimMapping.claim_id == result.claim_id,
        GraphClaimMapping.extraction_occurrence_id == result.extraction_occurrence_id,
    )
    if for_update and isinstance(db, AsyncSession):
        statement = statement.with_for_update()
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        raise CanonicalMappingRemapBlockedError("repository predecessor row is missing or out of scope")
    return row


def _is_exact_pg_unique_violation(
    exc: IntegrityError,
    allowed_constraints: frozenset[str],
) -> bool:
    if not isinstance(exc, IntegrityError):
        return False
    pending: list[Any] = [exc]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, asyncpg_exceptions.UniqueViolationError):
            diagnostic = getattr(current, "diag", None)
            constraint_name = getattr(diagnostic, "constraint_name", None) or getattr(
                current, "constraint_name", None
            )
            if current.sqlstate == "23505" and constraint_name in allowed_constraints:
                return True
        pending.extend(
            child
            for child in (
                getattr(current, "orig", None),
                getattr(current, "__cause__", None),
            )
            if child is not None and id(child) not in seen
        )
    return False


def _is_identity_unique_violation(exc: IntegrityError) -> bool:
    return _is_exact_pg_unique_violation(exc, _MAPPING_UNIQUE_CONSTRAINTS)


def _validate_m3_remap_reason(result: CanonicalMappingV1) -> None:
    remap = result.remap_provenance
    if (
        remap is not None
        and getattr(remap, "reason_code", None) == "ontology_refresh"
    ):
        raise CanonicalMappingRemapBlockedError(
            "ontology_refresh remap is unsupported by the M3 persistence contract"
        )


def _validate_remap_authority_continuity(
    result: CanonicalMappingV1,
    predecessor: GraphClaimMapping,
    predecessor_projection: CanonicalMappingV1,
    authority: PersistedMappingRegistrySnapshot,
) -> None:
    remap = result.remap_provenance
    if remap is None or remap.remap_generation == 0:
        raise CanonicalMappingRemapBlockedError("positive remap provenance is required")
    _validate_m3_remap_reason(result)
    previous_authority_id = getattr(predecessor, "authority_id", None)
    previous_authority_fingerprint = getattr(predecessor, "authority_fingerprint", None)
    if previous_authority_id != authority.authority_id or previous_authority_fingerprint != authority.authority_fingerprint:
        raise CanonicalMappingAuthorityError("remap registry authority is not continuous")
    if result.authorization_registry_snapshot != predecessor_projection.authorization_registry_snapshot:
        raise CanonicalMappingAuthorityError("remap registry snapshot changed without authority refresh")
    if (
        result.ontology_version_id != predecessor_projection.ontology_version_id
        or result.ontology_snapshot_hash != predecessor_projection.ontology_snapshot_hash
        or result.ontology_contract_version != predecessor_projection.ontology_contract_version
    ):
        raise CanonicalMappingAuthorityError("remap ontology authority changed without ontology refresh")


def _coalesce_identity_rows(
    by_id: GraphClaimMapping | None,
    by_fingerprint: GraphClaimMapping | None,
) -> GraphClaimMapping | None:
    if (
        by_id is not None
        and by_fingerprint is not None
        and by_id.mapping_result_id != by_fingerprint.mapping_result_id
    ):
        raise CanonicalMappingConflictError(
            "mapping result id and fingerprint identify different rows"
        )
    return by_id or by_fingerprint


async def create_or_get_canonical_mapping(
    db: AsyncSession,
    authoritative_input: CanonicalMappingInputV1,
    result: CanonicalMappingV1,
    *,
    authority_id: UUID | None = None,
    registry_snapshot_loader: MappingRegistryAuthorityLoader | RegistryAuthorityLoader | None = None,
) -> CanonicalMappingWriteResult:
    """Validate and insert/reuse one immutable mapping result.

    The caller owns the surrounding transaction and must commit explicitly.
    Real sessions require a persisted authority id.  A positive remap is
    accepted only after a scoped, row-locked predecessor is rebuilt and
    validated in this transaction.
    """
    if not isinstance(result, CanonicalMappingV1):
        raise TypeError("canonical mapping persistence requires CanonicalMappingV1")
    remap = result.remap_provenance
    _validate_m3_remap_reason(result)
    if remap is None:
        remap_generation = 0
    else:
        remap_generation = remap.remap_generation
    authority = await _load_registry_authority(
        db,
        result=result,
        authority_id=authority_id,
        loader=registry_snapshot_loader,
    )
    predecessor_row: GraphClaimMapping | None = None
    predecessor_projection: CanonicalMappingV1 | None = None
    if remap_generation > 0:
        predecessor_row = await _find_predecessor(db, result, for_update=True)
        predecessor_projection = _projection_from_row(predecessor_row)
        await _validate_stored_row_dependencies(
            db,
            predecessor_row,
            predecessor_projection,
            authority=authority,
        )
        _validate_remap_authority_continuity(result, predecessor_row, predecessor_projection, authority)
        try:
            payload = repository_authorized_canonical_mapping_json(
                authoritative_input,
                result,
                predecessor=predecessor_projection,
            )
            validated = CanonicalMappingV1.model_validate(json.loads(payload))
        except Exception as exc:
            raise CanonicalMappingRemapBlockedError("repository remap validation failed") from exc
    else:
        validated = _authoritative_result(authoritative_input, result)
    await _require_scope_dependencies(
        db,
        validated,
        authoritative_input,
        repository_predecessor=predecessor_projection,
    )
    by_id, by_fingerprint = await _find_identity(db, validated)
    existing = _coalesce_identity_rows(by_id, by_fingerprint)
    if existing is not None:
        stored = _projection_from_row(existing)
        if stored.mapping_result_fingerprint != validated.mapping_result_fingerprint:
            raise CanonicalMappingConflictError(
                "existing mapping result conflicts with the requested identity"
            )
        await _validate_stored_row_dependencies(db, existing, stored, authority=authority)
        if getattr(existing, "authority_id", authority.authority_id) != authority.authority_id:
            raise CanonicalMappingAuthorityError("existing mapping authority conflicts")
        return CanonicalMappingWriteResult(stored, False)

    row = _row_from_result(validated, authority=authority)
    try:
        async with db.begin_nested():
            db.add(row)
            await db.flush()
    except IntegrityError as exc:
        if not _is_identity_unique_violation(exc):
            raise CanonicalMappingPersistenceError("mapping insert failed closed") from exc
        refreshed_by_id, refreshed_by_fingerprint = await _find_identity(db, validated)
        existing = _coalesce_identity_rows(refreshed_by_id, refreshed_by_fingerprint)
        if existing is None:
            raise CanonicalMappingPersistenceError("mapping identity race did not yield a row") from exc
        stored = _projection_from_row(existing)
        if stored.mapping_result_fingerprint != validated.mapping_result_fingerprint:
            raise CanonicalMappingConflictError(
                "existing mapping result conflicts with the requested identity"
            ) from exc
        await _validate_stored_row_dependencies(db, existing, stored, authority=authority)
        if getattr(existing, "authority_id", authority.authority_id) != authority.authority_id:
            raise CanonicalMappingAuthorityError("existing mapping authority conflicts")
        return CanonicalMappingWriteResult(stored, False)
    return CanonicalMappingWriteResult(validated, True)


async def get_canonical_mapping(
    db: AsyncSession,
    *,
    library_id: UUID,
    document_revision_id: UUID,
    mapping_result_id: UUID,
) -> CanonicalMappingV1 | None:
    row = (
        await db.execute(
            select(GraphClaimMapping).where(
                GraphClaimMapping.library_id == library_id,
                GraphClaimMapping.document_revision_id == document_revision_id,
                GraphClaimMapping.mapping_result_id == mapping_result_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    result = _projection_from_row(row)
    return await _validate_stored_row_dependencies(db, row, result)


async def list_canonical_mappings(
    db: AsyncSession,
    *,
    library_id: UUID,
    document_revision_id: UUID,
    claim_id: UUID | None = None,
    extraction_occurrence_id: UUID | None = None,
    outcome: str | None = None,
    limit: int = 1000,
) -> list[CanonicalMappingV1]:
    if type(limit) is not int or limit < 1 or limit > _MAX_LIST_LIMIT:
        raise ValueError("canonical mapping list limit is out of bounds")
    statement = select(GraphClaimMapping).where(
        GraphClaimMapping.library_id == library_id,
        GraphClaimMapping.document_revision_id == document_revision_id,
    )
    if claim_id is not None:
        statement = statement.where(GraphClaimMapping.claim_id == claim_id)
    if extraction_occurrence_id is not None:
        statement = statement.where(
            GraphClaimMapping.extraction_occurrence_id == extraction_occurrence_id
        )
    if outcome is not None:
        if outcome not in {"mapped", "ambiguous", "blocked", "rejected"}:
            raise ValueError("canonical mapping outcome is invalid")
        statement = statement.where(GraphClaimMapping.outcome == outcome)
    statement = statement.order_by(
        GraphClaimMapping.created_at,
        GraphClaimMapping.mapping_result_id,
    ).limit(limit)
    rows = (await db.execute(statement)).scalars().all()
    results: list[CanonicalMappingV1] = []
    for row in rows:
        result = _projection_from_row(row)
        results.append(await _validate_stored_row_dependencies(db, row, result))
    return results
