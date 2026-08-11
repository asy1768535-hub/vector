"""DB-free, evidence-bound CanonicalMappingV1 contract.

This module is intentionally independent from worker, provider, ORM,
candidate, materializer, and publication code.  It validates an explicit
mapping proposal against an immutable RawClaimV1 and a frozen ontology
snapshot; it never infers a canonical relation from a surface predicate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, StrictBool, StrictInt, field_validator, model_validator

from app.schemas._strict_datetime import canonical_datetime_string, strict_datetime
from app.schemas.evidence_locator import UnitKind
from app.schemas.claim_decision import (
    ClaimDecisionProjectionV1,
    CLAIM_DECISION_ID_NAMESPACE,
    MappingCandidateProposalV1,
    SchemaExtensionCandidateProposalV1,
    canonical_claim_decision_json,
    deterministic_decision_id,
    claim_decision_fingerprint,
)
from app.schemas.raw_claim import (
    ClaimMentionV1,
    DirectionV1,
    EvidenceReferenceV1,
    RawClaimV1,
    canonical_raw_claim_json,
    stable_evidence_identity,
)


CanonicalMappingInputSchema = Literal["canonical_mapping_input_v1"]
CanonicalMappingSchema = Literal["canonical_mapping_v1"]
CanonicalDirectionV1 = Literal["source_to_target", "target_to_source", "undirected"]
CanonicalDirection = CanonicalDirectionV1
MappingOutcome = Literal["mapped", "ambiguous", "blocked", "rejected"]
EndpointTransform = Literal["identity", "swap"]
PredicateTransform = Literal["identity", "inverse", "symmetric"]
MappingDecisionKind = Literal["mapping_candidate", "schema_extension_candidate"]
MappingSourceKind = Literal["raw_claim", "decision_projection", "explicit_proposal"]
MappingActorKind = Literal["system", "human", "external"]
EntityLinkStatus = Literal["resolved", "candidate", "unresolved", "rejected"]
SemanticStatus = Literal["preserved", "ambiguous", "blocked"]
AuthorizationRegistrySchemaVersion = Literal["mapping_authorization_registry_v1"]
AuthorizationRegistryEntrySchemaVersion = Literal["mapping_authorization_registry_entry_v1"]

MappingReasonCode = Literal[
    "unknown_predicate",
    "unknown_source_type",
    "unknown_target_type",
    "unknown_direction",
    "ambiguous_mapping",
    "ambiguous_endpoint",
    "ontology_relation_not_allowed",
    "ontology_snapshot_mismatch",
    "evidence_missing",
    "evidence_invalid",
    "scope_mismatch",
    "no_explicit_mapping",
    "unsupported_negation",
    "unsupported_modality",
    "unsupported_qualifier",
    "unsupported_valid_time",
    "unsupported_effective_time",
    "mapper_error",
]

EvidenceBindingVersion = Literal["evidence_validation_v1"]
SemanticProjectionVersion = Literal["canonical_mapping_semantics_v1"]
RemapReasonCode = Literal["ontology_refresh", "mapper_refresh", "human_correction", "evidence_correction"]

CANONICAL_MAPPING_INPUT_SCHEMA_VERSION = "canonical_mapping_input_v1"
CANONICAL_MAPPING_SCHEMA_VERSION = "canonical_mapping_v1"
CANONICAL_MAPPING_SEMANTIC_VERSION = "canonical_mapping_semantics_v1"
EVIDENCE_VALIDATION_BINDING_VERSION = "evidence_validation_v1"

# Re-export the production decision namespace for the canonical mapping
# boundary; its source of truth lives with the decision projection schema.
CANONICAL_MAPPING_UUID_NAMESPACE = UUID("2cb2c2a1-5d36-5b6b-9c3e-21a96e8b7f40")

MAX_MAPPING_STRING_LENGTH = 256
MAX_MAPPING_KEY_LENGTH = 128
MAX_MAPPING_EVIDENCE_REFS = 32
MAX_ONTOLOGY_TYPES = 256
MAX_ONTOLOGY_RELATIONS = 256
MAX_ONTOLOGY_CONSTRAINTS = 1024
MAX_AUTHORIZATION_REGISTRY_ENTRIES = 256
MAX_CANONICAL_MAPPING_JSON_BYTES = 128 * 1024
MAX_CANONICAL_MAPPING_JSON_DEPTH = 8
MAX_CANONICAL_MAPPING_JSON_NODES = 16384

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_TOKEN_PARTS = frozenset(
    {"secret", "password", "credential", "apikey", "dsn", "storagepath", "objectkey", "rawfile"}
)


class CanonicalMappingContractError(ValueError):
    """Stable error for a rejected explicit mapping proposal."""


class _MappingModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        revalidate_instances="always",
        validate_default=True,
    )


def _bounded_string(value: Any, *, field: str, limit: int = MAX_MAPPING_STRING_LENGTH) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    normalized = value.strip()
    if not normalized or len(normalized) > limit or "\x00" in normalized:
        raise ValueError(f"{field} must be non-empty and bounded")
    return normalized


def _bounded_token(value: Any, *, field: str, limit: int = MAX_MAPPING_STRING_LENGTH) -> str:
    normalized = _bounded_string(value, field=field, limit=limit)
    compact = re.sub(r"[^a-z0-9]", "", normalized.casefold())
    if "/" in normalized or "\\" in normalized or ".." in normalized:
        raise ValueError(f"{field} must not contain a path")
    if any(part in compact for part in _SENSITIVE_TOKEN_PARTS):
        raise ValueError(f"{field} contains a sensitive identity token")
    return normalized


def _sha256(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 hex digest")
    return value


def deterministic_mapping_result_id(result_fingerprint: str) -> UUID:
    """Derive the append-only predecessor/result id from its fingerprint."""
    normalized = _sha256(result_fingerprint, field="mapping_result_fingerprint")
    return uuid5(CANONICAL_MAPPING_UUID_NAMESPACE, f"canonical_mapping_result_v1:{normalized}")


def _revalidate_nested(value: Any, model_type: Any, *, field: str) -> Any:
    """Rebuild a nested contract model from canonical JSON before trusting it."""
    if value is None:
        return None
    payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
    try:
        return model_type.model_validate(json.loads(_canonical_json(payload)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} failed canonical nested validation") from exc


def _computed_model_fingerprint(value: BaseModel, *, exclude: str) -> str:
    payload = value.model_dump(mode="json", exclude={exclude})
    return _hash_json(payload)


def _validate_model_fingerprint(value: str | None, expected: str, *, field: str) -> str:
    if value is not None and value != expected:
        raise ValueError(f"{field} does not match its immutable model content")
    return expected


def _validate_json_value(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> Any:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > MAX_CANONICAL_MAPPING_JSON_NODES or depth > MAX_CANONICAL_MAPPING_JSON_DEPTH:
        raise ValueError("mapping JSON value exceeds its bound")
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) is int:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("mapping JSON value must be finite")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("mapping JSON object keys must be strings")
            result[key] = _validate_json_value(item, depth=depth + 1, nodes=nodes)
        return result
    if isinstance(value, (list, tuple)):
        return [_validate_json_value(item, depth=depth + 1, nodes=nodes) for item in value]
    raise ValueError("mapping value must be JSON-safe")


def _canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    validated = _validate_json_value(value)
    try:
        encoded = json.dumps(
            validated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("mapping value must be bounded JSON") from exc
    if len(encoded.encode("utf-8")) > MAX_CANONICAL_MAPPING_JSON_BYTES:
        raise ValueError("mapping JSON exceeds the bounded size limit")
    return encoded


def canonical_mapping_json_value(value: Any) -> str:
    """Validate and serialize a bounded JSON-safe value deterministically."""
    return _canonical_json(value)


def _hash_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _schema_hash(value: Any) -> str:
    """Hash the complete generated schema without contract payload bounds."""
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# Assigned from the actual Pydantic JSON schemas after both contract classes
# are defined.  The default factories below intentionally resolve at instance
# creation time, after these values are frozen.
MAPPING_SCHEMA_HASH = ""
CANONICAL_SCHEMA_HASH = ""


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mapping_schema_hash() -> str:
    if not MAPPING_SCHEMA_HASH:
        raise RuntimeError("canonical mapping schema hash has not been frozen")
    return MAPPING_SCHEMA_HASH


def _canonical_schema_hash() -> str:
    if not CANONICAL_SCHEMA_HASH:
        raise RuntimeError("canonical result schema hash has not been frozen")
    return CANONICAL_SCHEMA_HASH


def _normalized_refs(value: Any, *, field: str = "evidence_ref_ids") -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list or tuple")
    refs = tuple(_bounded_string(item, field="evidence_ref_id", limit=64) for item in value)
    if not refs or len(refs) > MAX_MAPPING_EVIDENCE_REFS or len(set(refs)) != len(refs):
        raise ValueError(f"{field} must be non-empty, unique, and bounded")
    return tuple(sorted(refs))


def _optional_refs(value: Any) -> tuple[str, ...] | None:
    if value is None:
        return None
    return _normalized_refs(value)


def _normalized_bounded_tokens(value: Any, *, field: str, limit: int = MAX_MAPPING_KEY_LENGTH) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ValueError(f"{field} must be a list or tuple")
    tokens = tuple(_bounded_string(item, field=field, limit=limit) for item in value)
    if not tokens or len(tokens) != len(set(tokens)):
        raise ValueError(f"{field} must be non-empty and unique")
    return tuple(sorted(tokens))


def _validate_schema_hash(value: Any, *, expected: str, field: str) -> str:
    normalized = _sha256(value, field=field)
    if normalized != expected:
        raise ValueError(f"{field} does not match the frozen contract")
    return normalized


def _reject_duplicate_json_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("raw claim JSON contains a duplicate object key")
        result[key] = item
    return result


def _require_native_integer(value: Mapping[str, Any], field: str, *, label: str) -> None:
    if field in value and type(value[field]) is not int:
        raise ValueError(f"raw claim {label} must be a native JSON integer")


def _require_native_number(value: Mapping[str, Any], field: str, *, label: str) -> None:
    if field in value and (isinstance(value[field], bool) or not isinstance(value[field], (int, float))):
        raise ValueError(f"raw claim {label} must be a native JSON number")


def _reject_coercible_mapping_numeric_types(value: Any) -> None:
    """Validate only the concrete numeric fields in the RawClaim evidence schema."""
    if not isinstance(value, Mapping):
        return
    _require_native_integer(value, "revision_no", label="revision_no")
    evidence_refs = value.get("evidence_refs")
    if not isinstance(evidence_refs, list):
        return

    def validate_text_span(span: Any, *, label: str) -> None:
        if not isinstance(span, Mapping):
            return
        _require_native_integer(span, "start", label=f"{label}.start")
        _require_native_integer(span, "end", label=f"{label}.end")
        ranges = span.get("ranges")
        if isinstance(ranges, list):
            for index, text_range in enumerate(ranges):
                if isinstance(text_range, Mapping):
                    _require_native_integer(text_range, "start", label=f"{label}.ranges[{index}].start")
                    _require_native_integer(text_range, "end", label=f"{label}.ranges[{index}].end")

    def validate_source(source: Any, *, label: str) -> None:
        if not isinstance(source, Mapping):
            return
        for field in ("page", "row"):
            span = source.get(field)
            if isinstance(span, Mapping):
                _require_native_integer(span, "start", label=f"{label}.{field}.start")
                _require_native_integer(span, "end", label=f"{label}.{field}.end")
        validate_text_span(source.get("text"), label=f"{label}.text")
        table = source.get("table")
        if isinstance(table, Mapping):
            _require_native_integer(table, "index", label=f"{label}.table.index")
        column = source.get("column")
        if isinstance(column, Mapping):
            for field in ("start", "end"):
                if field in column and isinstance(column[field], bool):
                    raise ValueError(f"raw claim {label}.column.{field} must not be a boolean")
        bbox = source.get("bbox")
        if isinstance(bbox, Mapping):
            for field in ("x_min", "y_min", "x_max", "y_max", "width", "height"):
                _require_native_number(bbox, field, label=f"{label}.bbox.{field}")

    for index, reference in enumerate(evidence_refs):
        if not isinstance(reference, Mapping):
            continue
        reference_label = f"evidence_refs[{index}]"
        _require_native_integer(reference, "revision_no", label=f"{reference_label}.revision_no")
        validate_text_span(reference.get("source_span"), label=f"{reference_label}.source_span")
        locator = reference.get("locator")
        if not isinstance(locator, Mapping):
            continue
        _require_native_integer(locator, "revision_no", label=f"{reference_label}.locator.revision_no")
        _require_native_integer(locator, "ordinal", label=f"{reference_label}.locator.ordinal")
        validate_source(locator.get("source"), label=f"{reference_label}.locator.source")


class EndpointTypeBindingV1(_MappingModel):
    source_type_key: str | None = Field(default=None, max_length=MAX_MAPPING_KEY_LENGTH)
    target_type_key: str | None = Field(default=None, max_length=MAX_MAPPING_KEY_LENGTH)

    @field_validator("source_type_key", "target_type_key", mode="before")
    @classmethod
    def normalize_type_keys(cls, value: Any, info) -> str | None:
        return None if value is None else _bounded_string(value, field=info.field_name, limit=MAX_MAPPING_KEY_LENGTH)


class OntologyRelationConstraintV1(_MappingModel):
    relation_key: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    source_type_key: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    target_type_key: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    direction: CanonicalDirectionV1

    @field_validator("relation_key", "source_type_key", "target_type_key", mode="before")
    @classmethod
    def normalize_keys(cls, value: Any, info) -> str:
        return _bounded_string(value, field=info.field_name, limit=MAX_MAPPING_KEY_LENGTH)


def _ontology_content_payload(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    else:
        payload = dict(value)
    payload.pop("ontology_snapshot_hash", None)
    return payload


def _ontology_content_hash(value: Any) -> str:
    return _hash_json(_ontology_content_payload(value))


class FrozenOntologySnapshotV1(_MappingModel):
    ontology_version_id: UUID
    ontology_snapshot_hash: str
    ontology_contract_version: str = Field(min_length=1, max_length=64)
    entity_type_keys: tuple[str, ...] = Field(min_length=1, max_length=MAX_ONTOLOGY_TYPES)
    relation_type_keys: tuple[str, ...] = Field(min_length=1, max_length=MAX_ONTOLOGY_RELATIONS)
    constraints: tuple[OntologyRelationConstraintV1, ...] = Field(max_length=MAX_ONTOLOGY_CONSTRAINTS)

    @field_validator("ontology_snapshot_hash")
    @classmethod
    def validate_snapshot_hash(cls, value: str) -> str:
        return _sha256(value, field="ontology_snapshot_hash")

    @field_validator("ontology_contract_version", mode="before")
    @classmethod
    def normalize_contract_version(cls, value: Any) -> str:
        return _bounded_token(value, field="ontology_contract_version", limit=64)

    @field_validator("entity_type_keys", "relation_type_keys", mode="before")
    @classmethod
    def normalize_key_sets(cls, value: Any, info) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{info.field_name} must be a list or tuple")
        bound = MAX_ONTOLOGY_TYPES if info.field_name == "entity_type_keys" else MAX_ONTOLOGY_RELATIONS
        if not value or len(value) > bound:
            raise ValueError(f"{info.field_name} exceeds its bound")
        keys = tuple(_bounded_string(item, field=info.field_name, limit=MAX_MAPPING_KEY_LENGTH) for item in value)
        if len(keys) != len(set(keys)):
            raise ValueError(f"{info.field_name} must be unique")
        return tuple(sorted(keys))

    @field_validator("constraints", mode="after")
    @classmethod
    def normalize_constraints(cls, value: tuple[OntologyRelationConstraintV1, ...]) -> tuple[OntologyRelationConstraintV1, ...]:
        keys = [
            (item.relation_key, item.source_type_key, item.target_type_key, item.direction)
            for item in value
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("ontology constraints must be unique")
        return tuple(sorted(value, key=lambda item: (item.relation_key, item.source_type_key, item.target_type_key, item.direction)))

    @model_validator(mode="after")
    def validate_constraint_references(self) -> FrozenOntologySnapshotV1:
        entity_types = set(self.entity_type_keys)
        relation_types = set(self.relation_type_keys)
        if any(
            item.relation_key not in relation_types
            or item.source_type_key not in entity_types
            or item.target_type_key not in entity_types
            for item in self.constraints
        ):
            raise ValueError("ontology constraint references an undeclared type or relation")
        expected_hash = _ontology_content_hash(self)
        if self.ontology_snapshot_hash != expected_hash:
            raise ValueError("ontology_snapshot_hash does not match the frozen snapshot content")
        return self

    @classmethod
    def from_content(
        cls,
        *,
        ontology_version_id: UUID,
        ontology_contract_version: str,
        entity_type_keys: tuple[str, ...] | list[str],
        relation_type_keys: tuple[str, ...] | list[str],
        constraints: tuple[OntologyRelationConstraintV1, ...] | list[OntologyRelationConstraintV1],
    ) -> FrozenOntologySnapshotV1:
        normalized_entities = tuple(
            sorted(_bounded_string(item, field="entity_type_keys", limit=MAX_MAPPING_KEY_LENGTH) for item in entity_type_keys)
        )
        normalized_relations = tuple(
            sorted(_bounded_string(item, field="relation_type_keys", limit=MAX_MAPPING_KEY_LENGTH) for item in relation_type_keys)
        )
        normalized_constraint_items = [
            item if isinstance(item, OntologyRelationConstraintV1) else OntologyRelationConstraintV1.model_validate(item)
            for item in constraints
        ]
        normalized_constraints = tuple(
            sorted(
                normalized_constraint_items,
                key=lambda item: (item.relation_key, item.source_type_key, item.target_type_key, item.direction),
            )
        )
        payload = {
            "ontology_version_id": str(ontology_version_id),
            "ontology_contract_version": _bounded_token(
                ontology_contract_version,
                field="ontology_contract_version",
                limit=64,
            ),
            "entity_type_keys": normalized_entities,
            "relation_type_keys": normalized_relations,
            "constraints": [item.model_dump(mode="json") for item in normalized_constraints],
        }
        snapshot_hash = _hash_json(payload)
        return cls(
            ontology_version_id=ontology_version_id,
            ontology_snapshot_hash=snapshot_hash,
            ontology_contract_version=ontology_contract_version,
            entity_type_keys=normalized_entities,
            relation_type_keys=normalized_relations,
            constraints=normalized_constraints,
        )


class MapperProvenanceV1(_MappingModel):
    # mapper_algorithm_* are the frozen semantic producer identity.  The
    # mapper_key/version names remain as compatibility aliases for the M0
    # fixture vocabulary and are normalized to the same values.
    mapper_algorithm_key: str | None = Field(default=None, max_length=MAX_MAPPING_KEY_LENGTH)
    mapper_algorithm_version: str | None = Field(default=None, max_length=MAX_MAPPING_KEY_LENGTH)
    mapper_key: str | None = Field(default=None, max_length=MAX_MAPPING_KEY_LENGTH)
    mapper_version: str | None = Field(default=None, max_length=MAX_MAPPING_KEY_LENGTH)
    mapper_version_hash: str
    model_provider: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    model_version_hash: str
    prompt_version: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    prompt_content_hash: str
    config_version: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    config_hash: str
    provenance_fingerprint: str | None = None

    @field_validator(
        "mapper_algorithm_key",
        "mapper_algorithm_version",
        "mapper_key",
        "mapper_version",
        "model_provider",
        "prompt_version",
        "config_version",
        mode="before",
    )
    @classmethod
    def normalize_provenance_tokens(cls, value: Any, info) -> str | None:
        return None if value is None else _bounded_token(value, field=info.field_name, limit=MAX_MAPPING_KEY_LENGTH)

    @field_validator(
        "mapper_version_hash",
        "model_version_hash",
        "prompt_content_hash",
        "config_hash",
        "provenance_fingerprint",
    )
    @classmethod
    def validate_provenance_hashes(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @model_validator(mode="after")
    def normalize_algorithm_identity(self) -> MapperProvenanceV1:
        algorithm_key = self.mapper_algorithm_key or self.mapper_key
        algorithm_version = self.mapper_algorithm_version or self.mapper_version
        if algorithm_key is None or algorithm_version is None:
            raise ValueError("mapper algorithm identity is required")
        if self.mapper_algorithm_key not in (None, algorithm_key) or self.mapper_key not in (None, algorithm_key):
            raise ValueError("mapper algorithm key aliases do not match")
        if self.mapper_algorithm_version not in (None, algorithm_version) or self.mapper_version not in (
            None,
            algorithm_version,
        ):
            raise ValueError("mapper algorithm version aliases do not match")
        object.__setattr__(self, "mapper_algorithm_key", algorithm_key)
        object.__setattr__(self, "mapper_algorithm_version", algorithm_version)
        object.__setattr__(self, "mapper_key", algorithm_key)
        object.__setattr__(self, "mapper_version", algorithm_version)
        expected = _computed_model_fingerprint(self, exclude="provenance_fingerprint")
        object.__setattr__(
            self,
            "provenance_fingerprint",
            _validate_model_fingerprint(self.provenance_fingerprint, expected, field="provenance_fingerprint"),
        )
        return self


class MappingSourceProvenanceV1(_MappingModel):
    source_kind: MappingSourceKind
    source_key: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    source_version: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    source_hash: str
    source_precedence: StrictInt = Field(default=1, ge=1, le=16)
    provenance_fingerprint: str | None = None

    @field_validator("source_key", "source_version", mode="before")
    @classmethod
    def normalize_source_tokens(cls, value: Any, info) -> str:
        return _bounded_token(value, field=info.field_name, limit=MAX_MAPPING_KEY_LENGTH)

    @field_validator("source_hash", "provenance_fingerprint")
    @classmethod
    def validate_source_hash(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @model_validator(mode="after")
    def validate_source_precedence(self) -> MappingSourceProvenanceV1:
        expected = {"raw_claim": 1, "decision_projection": 2, "explicit_proposal": 3}[self.source_kind]
        if self.source_precedence != expected:
            raise ValueError("source precedence does not match source kind")
        expected_fingerprint = _computed_model_fingerprint(self, exclude="provenance_fingerprint")
        object.__setattr__(
            self,
            "provenance_fingerprint",
            _validate_model_fingerprint(
                self.provenance_fingerprint,
                expected_fingerprint,
                field="provenance_fingerprint",
            ),
        )
        return self


class MappingActorProvenanceV1(_MappingModel):
    actor_kind: MappingActorKind
    actor_key: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    actor_precedence: StrictInt | None = Field(default=None, ge=1, le=3)
    provenance_fingerprint: str | None = None

    @field_validator("actor_key", mode="before")
    @classmethod
    def normalize_actor_key(cls, value: Any) -> str:
        return _bounded_token(value, field="actor_key", limit=MAX_MAPPING_KEY_LENGTH)

    @model_validator(mode="after")
    def validate_actor_precedence(self) -> MappingActorProvenanceV1:
        expected = {"system": 1, "external": 2, "human": 3}[self.actor_kind]
        if self.actor_precedence not in (None, expected):
            raise ValueError("actor precedence does not match actor kind")
        object.__setattr__(self, "actor_precedence", expected)
        expected_fingerprint = _computed_model_fingerprint(self, exclude="provenance_fingerprint")
        object.__setattr__(
            self,
            "provenance_fingerprint",
            _validate_model_fingerprint(
                self.provenance_fingerprint,
                expected_fingerprint,
                field="provenance_fingerprint",
            ),
        )
        return self


class MappingScopeV1(_MappingModel):
    library_id: UUID
    document_id: UUID
    document_revision_id: UUID
    revision_no: StrictInt = Field(ge=1)
    job_id: UUID
    extraction_unit_id: UUID
    claim_id: UUID
    extraction_occurrence_id: UUID


class MappingAuthorizationRegistryEntryV1(_MappingModel):
    """One explicit, content-bound authorization rule for a mapping."""

    registry_entry_version: AuthorizationRegistryEntrySchemaVersion = "mapping_authorization_registry_entry_v1"
    authorization_key: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    authorization_version: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    surface_predicate_sha256: str
    canonical_relation_key_sha256: str
    allowed_endpoint_transforms: tuple[EndpointTransform, ...] = Field(min_length=1)
    allowed_predicate_transforms: tuple[PredicateTransform, ...] = Field(min_length=1)
    allowed_canonical_directions: tuple[CanonicalDirectionV1, ...] = Field(min_length=1)
    allowed_source_type_keys: tuple[str, ...] = Field(min_length=1)
    allowed_target_type_keys: tuple[str, ...] = Field(min_length=1)
    entry_fingerprint: str | None = None

    @field_validator("authorization_key", "authorization_version", mode="before")
    @classmethod
    def normalize_registry_entry_tokens(cls, value: Any, info) -> str:
        return _bounded_token(value, field=info.field_name, limit=MAX_MAPPING_KEY_LENGTH)

    @field_validator("surface_predicate_sha256", "canonical_relation_key_sha256", "entry_fingerprint")
    @classmethod
    def validate_registry_entry_hashes(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @field_validator("allowed_endpoint_transforms", "allowed_predicate_transforms", "allowed_canonical_directions", mode="before")
    @classmethod
    def normalize_registry_entry_enums(cls, value: Any, info) -> tuple[str, ...]:
        return _normalized_bounded_tokens(value, field=info.field_name)

    @field_validator("allowed_source_type_keys", "allowed_target_type_keys", mode="before")
    @classmethod
    def normalize_registry_entry_types(cls, value: Any, info) -> tuple[str, ...]:
        return _normalized_bounded_tokens(value, field=info.field_name, limit=MAX_MAPPING_KEY_LENGTH)

    @model_validator(mode="after")
    def validate_registry_entry(self) -> MappingAuthorizationRegistryEntryV1:
        expected = _computed_model_fingerprint(self, exclude="entry_fingerprint")
        object.__setattr__(
            self,
            "entry_fingerprint",
            _validate_model_fingerprint(self.entry_fingerprint, expected, field="entry_fingerprint"),
        )
        return self


class MappingAuthorizationRegistrySnapshotV1(_MappingModel):
    """Frozen pure-value registry authority supplied by a future repository."""

    registry_snapshot_version: AuthorizationRegistrySchemaVersion = "mapping_authorization_registry_v1"
    scope: MappingScopeV1
    ontology_snapshot_hash: str
    entries: tuple[MappingAuthorizationRegistryEntryV1, ...] = Field(
        min_length=1,
        max_length=MAX_AUTHORIZATION_REGISTRY_ENTRIES,
    )
    registry_snapshot_hash: str | None = None

    @field_validator("ontology_snapshot_hash")
    @classmethod
    def validate_registry_ontology_hash(cls, value: str) -> str:
        return _sha256(value, field="ontology_snapshot_hash")

    @field_validator("entries", mode="after")
    @classmethod
    def sort_registry_entries(
        cls,
        value: tuple[MappingAuthorizationRegistryEntryV1, ...],
    ) -> tuple[MappingAuthorizationRegistryEntryV1, ...]:
        policy_identities = [
            (
                item.authorization_key,
                item.authorization_version,
                item.surface_predicate_sha256,
                item.canonical_relation_key_sha256,
            )
            for item in value
        ]
        if len(policy_identities) != len(set(policy_identities)):
            raise ValueError("authorization registry contains duplicate or conflicting policy identity")
        # The complete entry fingerprint is derived from every policy field.
        # Sorting by it makes the snapshot hash independent of caller order.
        return tuple(sorted(value, key=lambda item: item.entry_fingerprint or ""))

    @field_validator("registry_snapshot_hash")
    @classmethod
    def validate_registry_snapshot_hash(cls, value: str | None) -> str | None:
        return None if value is None else _sha256(value, field="registry_snapshot_hash")

    @model_validator(mode="after")
    def validate_registry_snapshot(self) -> MappingAuthorizationRegistrySnapshotV1:
        object.__setattr__(self, "scope", _revalidate_nested(self.scope, MappingScopeV1, field="registry scope"))
        object.__setattr__(
            self,
            "entries",
            tuple(
                _revalidate_nested(entry, MappingAuthorizationRegistryEntryV1, field="registry entry")
                for entry in self.entries
            ),
        )
        expected = _computed_model_fingerprint(self, exclude="registry_snapshot_hash")
        object.__setattr__(
            self,
            "registry_snapshot_hash",
            _validate_model_fingerprint(
                self.registry_snapshot_hash,
                expected,
                field="registry_snapshot_hash",
            ),
        )
        return self

    @classmethod
    def from_content(
        cls,
        *,
        scope: MappingScopeV1,
        ontology_snapshot_hash: str,
        entries: tuple[MappingAuthorizationRegistryEntryV1, ...] | list[MappingAuthorizationRegistryEntryV1],
    ) -> MappingAuthorizationRegistrySnapshotV1:
        return cls(
            scope=scope,
            ontology_snapshot_hash=ontology_snapshot_hash,
            entries=tuple(entries),
        )

    @property
    def canonical_manifest(self) -> tuple[MappingAuthorizationRegistryEntryV1, ...]:
        """Stable alias for the sorted content manifest used in the hash."""
        return self.entries


class MappingAuthorizationProvenanceV1(_MappingModel):
    """Explicit registry authorization; M0 never resolves this automatically."""

    authorization_key: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    authorization_version: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    registry_snapshot: MappingAuthorizationRegistrySnapshotV1
    registry_hash: str
    authorized_surface_predicate_sha256: str
    authorized_canonical_relation_key_sha256: str
    authorization_fingerprint: str | None = None

    @field_validator("authorization_key", "authorization_version", mode="before")
    @classmethod
    def normalize_authorization_tokens(cls, value: Any, info) -> str:
        return _bounded_token(value, field=info.field_name, limit=MAX_MAPPING_KEY_LENGTH)

    @field_validator(
        "registry_hash",
        "authorized_surface_predicate_sha256",
        "authorized_canonical_relation_key_sha256",
        "authorization_fingerprint",
    )
    @classmethod
    def validate_authorization_hashes(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @model_validator(mode="after")
    def validate_authorization_fingerprint(self) -> MappingAuthorizationProvenanceV1:
        object.__setattr__(
            self,
            "registry_snapshot",
            _revalidate_nested(
                self.registry_snapshot,
                MappingAuthorizationRegistrySnapshotV1,
                field="registry_snapshot",
            ),
        )
        if self.registry_hash != self.registry_snapshot.registry_snapshot_hash:
            raise ValueError("registry_hash does not match the content-bound registry snapshot")
        expected = _computed_model_fingerprint(self, exclude="authorization_fingerprint")
        object.__setattr__(
            self,
            "authorization_fingerprint",
            _validate_model_fingerprint(
                self.authorization_fingerprint,
                expected,
                field="authorization_fingerprint",
            ),
        )
        return self


class MappingRemapProvenanceV1(_MappingModel):
    # The shape is expressible at the contract boundary, but it is not by
    # itself an authority proof.  Only the repository serializer may accept a
    # generation greater than zero after rebuilding the predecessor from an
    # immutable row in the same transaction.
    remap_generation: StrictInt = Field(default=0, ge=0)
    remap_version: StrictInt | None = Field(default=None, ge=0)
    supersedes_mapping_result_id: UUID | None = None
    supersedes_mapping_result_fingerprint: str | None = None
    supersedes_scope: MappingScopeV1 | None = None
    # Generation one must be able to point at the generation-zero result.
    # Positive remaps remain repository-authorized only; this bound merely
    # makes the typed predecessor envelope express the valid sequence.
    prior_remap_generation: StrictInt | None = Field(default=None, ge=0)
    lineage_root_mapping_result_id: UUID | None = None
    lineage_root_mapping_result_fingerprint: str | None = None
    supersedes_lineage_root_mapping_result_id: UUID | None = None
    supersedes_lineage_root_mapping_result_fingerprint: str | None = None
    supersedes_source_precedence: StrictInt | None = Field(default=None, ge=1, le=16)
    supersedes_actor_precedence: StrictInt | None = Field(default=None, ge=1, le=3)
    reason_code: RemapReasonCode
    remap_fingerprint: str | None = None

    @field_validator(
        "supersedes_mapping_result_fingerprint",
        "lineage_root_mapping_result_fingerprint",
        "supersedes_lineage_root_mapping_result_fingerprint",
        "remap_fingerprint",
    )
    @classmethod
    def validate_remap_hashes(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @model_validator(mode="after")
    def validate_remap_link(self) -> MappingRemapProvenanceV1:
        generation = self.remap_generation
        if self.reason_code == "ontology_refresh":
            raise ValueError("ontology_refresh remap is unsupported by the M3 persistence contract")
        if self.remap_version not in (None, generation):
            raise ValueError("remap generation aliases do not match")
        object.__setattr__(self, "remap_version", generation)
        predecessor_fields = (
            self.supersedes_mapping_result_id,
            self.supersedes_mapping_result_fingerprint,
            self.supersedes_scope,
            self.prior_remap_generation,
            self.lineage_root_mapping_result_id,
            self.lineage_root_mapping_result_fingerprint,
            self.supersedes_lineage_root_mapping_result_id,
            self.supersedes_lineage_root_mapping_result_fingerprint,
            self.supersedes_source_precedence,
            self.supersedes_actor_precedence,
        )
        if generation == 0 and any(value is not None for value in predecessor_fields):
            raise ValueError("generation zero remap cannot carry predecessor authority")
        if generation > 0:
            required = (
                self.supersedes_mapping_result_id,
                self.supersedes_mapping_result_fingerprint,
                self.supersedes_scope,
                self.prior_remap_generation,
                self.lineage_root_mapping_result_id,
                self.lineage_root_mapping_result_fingerprint,
                self.supersedes_lineage_root_mapping_result_id,
                self.supersedes_lineage_root_mapping_result_fingerprint,
                self.supersedes_source_precedence,
                self.supersedes_actor_precedence,
            )
            if any(value is None for value in required):
                raise ValueError("positive remap requires a complete predecessor envelope")
            if self.prior_remap_generation != generation - 1:
                raise ValueError("positive remap generation must identify the immediately prior generation")
            if self.supersedes_mapping_result_id != deterministic_mapping_result_id(
                self.supersedes_mapping_result_fingerprint or ""
            ):
                raise ValueError("positive remap predecessor id does not match its fingerprint")
        expected_fingerprint = _computed_model_fingerprint(self, exclude="remap_fingerprint")
        object.__setattr__(
            self,
            "remap_fingerprint",
            _validate_model_fingerprint(self.remap_fingerprint, expected_fingerprint, field="remap_fingerprint"),
        )
        return self


class EvidenceValidationAttestationV1(_MappingModel):
    """A typed verification statement, not an inference from locator presence."""

    attestation_version: Literal["evidence_validation_attestation_v1"] = "evidence_validation_attestation_v1"
    validation_status: Literal["verified"] = "verified"
    validated_at: datetime
    evidence_reference_sha256: str
    locator_sha256: str
    stable_evidence_identity_hash: str
    validation_fingerprint: str | None = None

    @field_validator("validated_at", mode="before")
    @classmethod
    def validate_validated_at(cls, value: Any) -> datetime:
        return strict_datetime(value, field="validated_at")

    @field_validator("evidence_reference_sha256", "locator_sha256", "stable_evidence_identity_hash")
    @classmethod
    def validate_attestation_hashes(cls, value: str, info) -> str:
        return _sha256(value, field=info.field_name)

    @field_validator("validation_fingerprint")
    @classmethod
    def validate_attestation_fingerprint(cls, value: str | None) -> str | None:
        return None if value is None else _sha256(value, field="validation_fingerprint")

    @model_validator(mode="after")
    def validate_attestation(self) -> EvidenceValidationAttestationV1:
        if self.validated_at.tzinfo is None or self.validated_at.utcoffset() is None:
            raise ValueError("evidence attestation validated_at must include an explicit timezone")
        expected = _hash_json(
            {
                "attestation_version": self.attestation_version,
                "validation_status": self.validation_status,
                "evidence_reference_sha256": self.evidence_reference_sha256,
                "locator_sha256": self.locator_sha256,
                "stable_evidence_identity_hash": self.stable_evidence_identity_hash,
            }
        )
        if self.validation_fingerprint not in (None, expected):
            raise ValueError("evidence validation fingerprint does not match the attestation")
        object.__setattr__(self, "validation_fingerprint", expected)
        return self

    @classmethod
    def for_reference(
        cls,
        reference: EvidenceReferenceV1,
        *,
        validated_at: datetime,
    ) -> EvidenceValidationAttestationV1:
        if not isinstance(reference, EvidenceReferenceV1):
            raise TypeError("evidence attestation requires EvidenceReferenceV1")
        if reference.locator is None:
            raise ValueError("evidence attestation requires a locator")
        return cls(
            validated_at=validated_at,
            evidence_reference_sha256=_hash_json(reference.model_dump(mode="json")),
            locator_sha256=_hash_json(reference.locator.model_dump(mode="json")),
            stable_evidence_identity_hash=stable_evidence_identity(reference),
        )


class EvidenceValidationBindingV1(_MappingModel):
    """A verified evidence subset item with stable identity and full scope."""

    binding_version: EvidenceBindingVersion = EVIDENCE_VALIDATION_BINDING_VERSION
    evidence_ref_id: str = Field(min_length=1, max_length=64)
    stable_evidence_identity_hash: str
    library_id: UUID
    document_id: UUID
    document_revision_id: UUID
    revision_no: StrictInt = Field(ge=1)
    job_id: UUID
    extraction_unit_id: UUID
    evidence_id: UUID
    unit_id: UUID
    unit_kind: UnitKind
    ordinal: StrictInt = Field(ge=0)
    quote_sha256: str
    unit_text_sha256: str
    locator_version: Literal["v1"] = "v1"
    provenance_status: Literal["verified"] = "verified"
    source_span_hash: str | None = None
    evidence_reference_sha256: str
    attestation: EvidenceValidationAttestationV1
    binding_fingerprint: str | None = None

    @field_validator("evidence_ref_id", mode="before")
    @classmethod
    def normalize_ref_id(cls, value: Any) -> str:
        return _bounded_string(value, field="evidence_ref_id", limit=64)

    @field_validator(
        "stable_evidence_identity_hash",
        "quote_sha256",
        "unit_text_sha256",
        "evidence_reference_sha256",
        "binding_fingerprint",
    )
    @classmethod
    def validate_binding_hashes(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @field_validator("source_span_hash")
    @classmethod
    def validate_optional_span_hash(cls, value: str | None) -> str | None:
        return None if value is None else _sha256(value, field="source_span_hash")

    @model_validator(mode="after")
    def validate_attested_identity(self) -> EvidenceValidationBindingV1:
        if self.attestation.stable_evidence_identity_hash != self.stable_evidence_identity_hash:
            raise ValueError("evidence binding identity does not match its attestation")
        if self.attestation.evidence_reference_sha256 != self.evidence_reference_sha256:
            raise ValueError("evidence binding reference hash does not match its attestation")
        if self.provenance_status != self.attestation.validation_status:
            raise ValueError("evidence binding provenance status does not match its attestation")
        expected = _hash_json(_evidence_binding_identity_payload(self))
        object.__setattr__(
            self,
            "binding_fingerprint",
            _validate_model_fingerprint(self.binding_fingerprint, expected, field="binding_fingerprint"),
        )
        return self

    @classmethod
    def from_reference(
        cls,
        reference: EvidenceReferenceV1,
        *,
        attestation: EvidenceValidationAttestationV1 | None = None,
    ) -> EvidenceValidationBindingV1:
        if not isinstance(reference, EvidenceReferenceV1):
            raise TypeError("evidence binding requires EvidenceReferenceV1")
        if reference.locator is None:
            raise ValueError("evidence binding requires a verified locator")
        if attestation is None or attestation.validation_status != "verified":
            raise ValueError("evidence binding requires an explicit verified attestation")
        expected_attestation = EvidenceValidationAttestationV1.for_reference(
            reference,
            validated_at=attestation.validated_at,
        )
        if attestation != expected_attestation:
            raise ValueError("evidence attestation does not match the complete reference")
        source_span_hash = _hash_json(reference.source_span) if reference.source_span is not None else None
        locator = reference.locator
        return cls(
            evidence_ref_id=reference.ref_id,
            stable_evidence_identity_hash=stable_evidence_identity(reference),
            library_id=reference.library_id,
            document_id=reference.document_id,
            document_revision_id=reference.document_revision_id,
            revision_no=reference.revision_no,
            job_id=reference.job_id,
            extraction_unit_id=reference.extraction_unit_id,
            evidence_id=reference.evidence_id,
            unit_id=reference.unit_id,
            unit_kind=locator.unit_kind,
            ordinal=locator.ordinal,
            quote_sha256=reference.quote_sha256,
            unit_text_sha256=reference.unit_text_sha256,
            locator_version=locator.locator_version,
            source_span_hash=source_span_hash,
            evidence_reference_sha256=_hash_json(reference.model_dump(mode="json")),
            attestation=attestation,
        )


def _evidence_binding_identity_payload(value: EvidenceValidationBindingV1) -> dict[str, Any]:
    payload = value.model_dump(mode="json", exclude={"binding_fingerprint"})
    attestation = payload.get("attestation")
    if isinstance(attestation, dict):
        attestation.pop("validated_at", None)
    return payload


class RawClaimEvidenceSnapshotV1(_MappingModel):
    """Full-content evidence identity needed to re-check a result offline."""

    evidence_ref_id: str = Field(min_length=1, max_length=64)
    stable_evidence_identity_hash: str
    evidence_reference_sha256: str
    quote_sha256: str
    unit_text_sha256: str
    locator_sha256: str | None = None
    source_span_hash: str | None = None
    snapshot_fingerprint: str | None = None

    @field_validator(
        "stable_evidence_identity_hash",
        "evidence_reference_sha256",
        "quote_sha256",
        "unit_text_sha256",
        "locator_sha256",
        "source_span_hash",
        "snapshot_fingerprint",
    )
    @classmethod
    def validate_evidence_snapshot_hashes(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @model_validator(mode="after")
    def validate_snapshot_fingerprint(self) -> RawClaimEvidenceSnapshotV1:
        expected = _computed_model_fingerprint(self, exclude="snapshot_fingerprint")
        object.__setattr__(
            self,
            "snapshot_fingerprint",
            _validate_model_fingerprint(self.snapshot_fingerprint, expected, field="snapshot_fingerprint"),
        )
        return self

    @classmethod
    def from_reference(cls, reference: EvidenceReferenceV1) -> RawClaimEvidenceSnapshotV1:
        return cls(
            evidence_ref_id=reference.ref_id,
            stable_evidence_identity_hash=stable_evidence_identity(reference),
            evidence_reference_sha256=_hash_json(reference.model_dump(mode="json")),
            quote_sha256=reference.quote_sha256,
            unit_text_sha256=reference.unit_text_sha256,
            locator_sha256=(
                _hash_json(reference.locator.model_dump(mode="json")) if reference.locator is not None else None
            ),
            source_span_hash=_hash_json(reference.source_span) if reference.source_span is not None else None,
        )


class CanonicalSemanticProjectionV1(_MappingModel):
    """Hash-only semantic preservation required before mapping can be mapped."""

    semantic_projection_version: SemanticProjectionVersion = CANONICAL_MAPPING_SEMANTIC_VERSION
    semantic_projection_fingerprint: str | None = None
    negation_value: StrictBool
    negation_evidence_identity_hash: str | None = None
    modality_present: StrictBool
    modality_value_hash: str | None = None
    modality_evidence_identity_hash: str | None = None
    qualifier_present: StrictBool
    qualifier_set_hash: str
    qualifier_evidence_identity_hashes: tuple[str, ...] = ()
    valid_time_present: StrictBool
    valid_time_hash: str | None = None
    valid_time_evidence_identity_hash: str | None = None
    effective_time_present: StrictBool
    effective_time_hash: str | None = None
    effective_time_evidence_identity_hash: str | None = None
    evidence_ref_ids: tuple[str, ...] = ()
    evidence_identity_hashes: tuple[str, ...] = ()

    @field_validator(
        "semantic_projection_fingerprint",
        "negation_evidence_identity_hash",
        "modality_value_hash",
        "modality_evidence_identity_hash",
        "qualifier_set_hash",
        "valid_time_hash",
        "valid_time_evidence_identity_hash",
        "effective_time_hash",
        "effective_time_evidence_identity_hash",
    )
    @classmethod
    def validate_semantic_hashes(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @field_validator("qualifier_evidence_identity_hashes", "evidence_identity_hashes", mode="before")
    @classmethod
    def normalize_identity_hashes(cls, value: Any, info) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError(f"{info.field_name} must be a list or tuple")
        hashes = tuple(_sha256(item, field=info.field_name) for item in value)
        if len(hashes) != len(set(hashes)):
            raise ValueError(f"{info.field_name} must be unique")
        return tuple(sorted(hashes))

    @field_validator("evidence_ref_ids", mode="before")
    @classmethod
    def normalize_semantic_refs(cls, value: Any) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("evidence_ref_ids must be a list or tuple")
        refs = tuple(_bounded_string(item, field="evidence_ref_id", limit=64) for item in value)
        if len(refs) != len(set(refs)):
            raise ValueError("semantic evidence refs must be unique")
        return tuple(sorted(refs))

    @model_validator(mode="after")
    def validate_semantic_presence(self) -> CanonicalSemanticProjectionV1:
        if self.modality_present != (self.modality_value_hash is not None):
            raise ValueError("modality presence does not match its hash")
        if self.valid_time_present != (self.valid_time_hash is not None):
            raise ValueError("valid time presence does not match its hash")
        if self.effective_time_present != (self.effective_time_hash is not None):
            raise ValueError("effective time presence does not match its hash")
        if self.qualifier_present and not self.qualifier_set_hash:
            raise ValueError("qualifier presence requires a qualifier hash")
        payload = self.model_dump(mode="json")
        supplied = payload.pop("semantic_projection_fingerprint")
        expected = _hash_json(payload)
        if supplied not in (None, expected):
            raise ValueError("semantic projection fingerprint does not match the projection")
        object.__setattr__(self, "semantic_projection_fingerprint", expected)
        return self


def _semantic_reference_token(claim: RawClaimV1, ref_id: str | None) -> str | None:
    if ref_id is None:
        return None
    by_id = {reference.ref_id: reference for reference in claim.evidence_refs}
    reference = by_id.get(ref_id)
    if reference is None:
        raise ValueError("claim semantic evidence reference is missing")
    return stable_evidence_identity(reference)


def _semantic_payload(claim: RawClaimV1) -> dict[str, Any]:
    def with_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(value)
        payload["evidence_ref"] = _semantic_reference_token(claim, payload.get("evidence_ref"))
        return payload

    qualifiers = [with_evidence(item.model_dump(mode="json")) for item in claim.qualifiers]
    qualifiers.sort(key=_canonical_json)
    return {
        "negation": with_evidence(claim.negation.model_dump(mode="json")),
        "modality": with_evidence(claim.modality.model_dump(mode="json")),
        "qualifiers": qualifiers,
        "valid_time": with_evidence(claim.valid_time.model_dump(mode="json")) if claim.valid_time else None,
        "effective_time": with_evidence(claim.effective_time.model_dump(mode="json"))
        if claim.effective_time
        else None,
    }


def semantic_projection_from_claim(claim: RawClaimV1) -> CanonicalSemanticProjectionV1:
    """Create a non-text semantic projection without mutating the raw claim."""
    if not isinstance(claim, RawClaimV1):
        raise TypeError("claim must be a validated RawClaimV1")
    # RawClaimV1 is frozen only at its top level.  Rebuild it from canonical
    # JSON so nested qualifier/semantic mutations cannot reuse an old claim
    # fingerprint at this trusted contract boundary.
    claim = RawClaimV1.model_validate(json.loads(canonical_raw_claim_json(claim)))
    payload = _semantic_payload(claim)
    semantic_ref_ids = tuple(
        sorted(
            {
                ref_id
                for ref_id in (
                    claim.negation.evidence_ref,
                    claim.modality.evidence_ref,
                    *(item.evidence_ref for item in claim.qualifiers),
                    *(item.evidence_ref for item in (claim.valid_time, claim.effective_time) if item is not None),
                )
                if ref_id is not None
            }
        )
    )
    identity_hashes = tuple(
        sorted(
            {
                token
                for token in (
                    _semantic_reference_token(claim, claim.negation.evidence_ref),
                    _semantic_reference_token(claim, claim.modality.evidence_ref),
                    *(
                        _semantic_reference_token(claim, item.evidence_ref)
                        for item in claim.qualifiers
                    ),
                    *(
                        _semantic_reference_token(claim, item.evidence_ref)
                        for item in (claim.valid_time, claim.effective_time)
                        if item is not None
                    ),
                )
                if token is not None
            }
        )
    )
    projection = CanonicalSemanticProjectionV1(
        negation_value=claim.negation.value,
        negation_evidence_identity_hash=_semantic_reference_token(claim, claim.negation.evidence_ref),
        modality_present=claim.modality.value is not None,
        modality_value_hash=_hash_json(payload["modality"]["value"]) if claim.modality.value is not None else None,
        modality_evidence_identity_hash=_semantic_reference_token(claim, claim.modality.evidence_ref),
        qualifier_present=bool(claim.qualifiers),
        qualifier_set_hash=_hash_json(payload["qualifiers"]),
        qualifier_evidence_identity_hashes=tuple(
            sorted(
                {
                    token
                    for token in (
                        _semantic_reference_token(claim, item.evidence_ref) for item in claim.qualifiers
                    )
                    if token is not None
                }
            )
        ),
        valid_time_present=claim.valid_time is not None,
        valid_time_hash=_hash_json(payload["valid_time"]) if claim.valid_time else None,
        valid_time_evidence_identity_hash=_semantic_reference_token(
            claim, claim.valid_time.evidence_ref if claim.valid_time else None
        ),
        effective_time_present=claim.effective_time is not None,
        effective_time_hash=_hash_json(payload["effective_time"]) if claim.effective_time else None,
        effective_time_evidence_identity_hash=_semantic_reference_token(
            claim, claim.effective_time.evidence_ref if claim.effective_time else None
        ),
        evidence_ref_ids=semantic_ref_ids,
        evidence_identity_hashes=identity_hashes,
    )
    return projection


class EntityLinkResolverProvenanceV1(_MappingModel):
    resolver_key: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    resolver_version: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    resolver_version_hash: str
    config_hash: str
    provenance_fingerprint: str | None = None

    @field_validator("resolver_key", "resolver_version", mode="before")
    @classmethod
    def normalize_resolver_tokens(cls, value: Any, info) -> str:
        return _bounded_token(value, field=info.field_name, limit=MAX_MAPPING_KEY_LENGTH)

    @field_validator("resolver_version_hash", "config_hash", "provenance_fingerprint")
    @classmethod
    def validate_resolver_hashes(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @model_validator(mode="after")
    def validate_provenance_fingerprint(self) -> EntityLinkResolverProvenanceV1:
        expected = _computed_model_fingerprint(self, exclude="provenance_fingerprint")
        object.__setattr__(
            self,
            "provenance_fingerprint",
            _validate_model_fingerprint(self.provenance_fingerprint, expected, field="provenance_fingerprint"),
        )
        return self


class EntityLinkDecisionV1(_MappingModel):
    mention_local_id: str = Field(min_length=1, max_length=128)
    status: EntityLinkStatus
    confidence: float | None = None
    entity_id: UUID | None = None
    entity_candidate_key_hash: str | None = None
    resolver_provenance: EntityLinkResolverProvenanceV1
    link_decision_fingerprint: str | None = None

    @field_validator("entity_candidate_key_hash", "link_decision_fingerprint")
    @classmethod
    def validate_candidate_hash(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @field_validator("mention_local_id", mode="before")
    @classmethod
    def normalize_link_mention_id(cls, value: Any) -> str:
        return _bounded_string(value, field="mention_local_id", limit=128)

    @field_validator("confidence", mode="before")
    @classmethod
    def validate_link_confidence(cls, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("entity link confidence must be finite")
        if not 0 <= float(value) <= 1:
            raise ValueError("entity link confidence must be between 0 and 1")
        return float(value)

    @model_validator(mode="after")
    def validate_link_shape(self) -> EntityLinkDecisionV1:
        if self.status == "resolved":
            if self.entity_id is None or self.entity_candidate_key_hash is not None or self.confidence is None:
                raise ValueError("resolved entity link requires an entity id and confidence")
        elif self.status == "candidate":
            if self.entity_candidate_key_hash is None or self.entity_id is not None or self.confidence is None:
                raise ValueError("candidate entity link requires a candidate hash and confidence")
        elif self.entity_id is not None or self.entity_candidate_key_hash is not None or self.confidence is not None:
            raise ValueError("unresolved or rejected entity link cannot carry an identity or confidence")
        expected = _computed_model_fingerprint(self, exclude="link_decision_fingerprint")
        object.__setattr__(
            self,
            "link_decision_fingerprint",
            _validate_model_fingerprint(
                self.link_decision_fingerprint,
                expected,
                field="link_decision_fingerprint",
            ),
        )
        return self


class EndpointResolutionAttestationV1(_MappingModel):
    """Complete pure-value authority projection for one raw mention link."""

    attestation_version: Literal["endpoint_resolution_v1"] = "endpoint_resolution_v1"
    mention_role: Literal["source", "target"]
    mention_local_id: str = Field(min_length=1, max_length=128)
    mention_surface_sha256: str
    entity_type_key: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    entity_link: EntityLinkDecisionV1
    evidence_ref_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_MAPPING_EVIDENCE_REFS)
    scope: MappingScopeV1
    attestation_fingerprint: str | None = None

    @field_validator("mention_local_id", "entity_type_key", mode="before")
    @classmethod
    def normalize_resolution_tokens(cls, value: Any, info) -> str:
        return _bounded_string(
            value,
            field=info.field_name,
            limit=128 if info.field_name == "mention_local_id" else MAX_MAPPING_KEY_LENGTH,
        )

    @field_validator("mention_surface_sha256", "attestation_fingerprint")
    @classmethod
    def validate_resolution_hashes(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @field_validator("evidence_ref_ids", mode="before")
    @classmethod
    def normalize_resolution_refs(cls, value: Any) -> tuple[str, ...]:
        return _normalized_refs(value, field="endpoint resolution evidence_ref_ids")

    @model_validator(mode="after")
    def validate_resolution_attestation(self) -> EndpointResolutionAttestationV1:
        object.__setattr__(self, "scope", _revalidate_nested(self.scope, MappingScopeV1, field="resolution scope"))
        object.__setattr__(
            self,
            "entity_link",
            _revalidate_nested(self.entity_link, EntityLinkDecisionV1, field="entity link"),
        )
        if self.entity_link.mention_local_id != self.mention_local_id:
            raise ValueError("endpoint resolution entity link mention does not match the attestation")
        expected = _computed_model_fingerprint(self, exclude="attestation_fingerprint")
        object.__setattr__(
            self,
            "attestation_fingerprint",
            _validate_model_fingerprint(
                self.attestation_fingerprint,
                expected,
                field="attestation_fingerprint",
            ),
        )
        return self


class CanonicalEndpointV1(_MappingModel):
    role: Literal["source", "target"]
    mention_local_id: str = Field(min_length=1, max_length=128)
    entity_type_key: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    entity_link: EntityLinkDecisionV1
    resolution_attestation: EndpointResolutionAttestationV1
    endpoint_fingerprint: str | None = None

    @field_validator("mention_local_id", "entity_type_key", mode="before")
    @classmethod
    def normalize_endpoint_strings(cls, value: Any, info) -> str:
        return _bounded_string(
            value,
            field=info.field_name,
            limit=128 if info.field_name == "mention_local_id" else MAX_MAPPING_KEY_LENGTH,
        )

    @model_validator(mode="after")
    def validate_link_subject(self) -> CanonicalEndpointV1:
        object.__setattr__(
            self,
            "entity_link",
            _revalidate_nested(self.entity_link, EntityLinkDecisionV1, field="endpoint entity link"),
        )
        object.__setattr__(
            self,
            "resolution_attestation",
            _revalidate_nested(
                self.resolution_attestation,
                EndpointResolutionAttestationV1,
                field="endpoint resolution attestation",
            ),
        )
        if self.entity_link.mention_local_id != self.mention_local_id:
            raise ValueError("entity link must refer to the same endpoint mention")
        if self.resolution_attestation.mention_local_id != self.mention_local_id:
            raise ValueError("endpoint resolution attestation must refer to the same mention")
        if self.resolution_attestation.entity_type_key != self.entity_type_key:
            raise ValueError("endpoint resolution attestation type does not match the endpoint")
        if self.resolution_attestation.entity_link != self.entity_link:
            raise ValueError("endpoint resolution attestation link does not match the endpoint")
        expected = _computed_model_fingerprint(self, exclude="endpoint_fingerprint")
        object.__setattr__(
            self,
            "endpoint_fingerprint",
            _validate_model_fingerprint(self.endpoint_fingerprint, expected, field="endpoint_fingerprint"),
        )
        return self


class CanonicalMappingProposalV1(_MappingModel):
    """Explicit relation proposal; no endpoint or predicate inference is done."""

    canonical_relation_key: str = Field(min_length=1, max_length=MAX_MAPPING_KEY_LENGTH)
    canonical_direction: CanonicalDirectionV1
    canonical_source_endpoint: CanonicalEndpointV1
    canonical_target_endpoint: CanonicalEndpointV1
    endpoint_transform: EndpointTransform
    predicate_transform: PredicateTransform
    authorization_provenance: MappingAuthorizationProvenanceV1
    proposal_fingerprint: str | None = None

    @field_validator("canonical_relation_key", mode="before")
    @classmethod
    def normalize_relation_key(cls, value: Any) -> str:
        return _bounded_string(value, field="canonical_relation_key", limit=MAX_MAPPING_KEY_LENGTH)

    @model_validator(mode="after")
    def validate_endpoint_roles(self) -> CanonicalMappingProposalV1:
        if self.canonical_source_endpoint.role != "source" or self.canonical_target_endpoint.role != "target":
            raise ValueError("canonical endpoint roles must be source and target")
        expected = _computed_model_fingerprint(self, exclude="proposal_fingerprint")
        object.__setattr__(
            self,
            "proposal_fingerprint",
            _validate_model_fingerprint(self.proposal_fingerprint, expected, field="proposal_fingerprint"),
        )
        return self


# Compatibility name for the M0 proposal vocabulary; no production caller
# should use this alias before the M1 builder is approved.
CanonicalRelationProposalV1 = CanonicalMappingProposalV1


class RawClaimMappingSnapshotV1(_MappingModel):
    """Deep, text-free claim facts retained for result re-validation."""

    snapshot_version: Literal["raw_claim_mapping_snapshot_v1"] = "raw_claim_mapping_snapshot_v1"
    claim_id: UUID
    library_id: UUID
    document_id: UUID
    document_revision_id: UUID
    revision_no: StrictInt = Field(ge=1)
    job_id: UUID
    extraction_unit_id: UUID
    extraction_occurrence_id: UUID
    claim_content_scoped_fingerprint: str
    extraction_occurrence_fingerprint: str
    raw_predicate_sha256: str
    surface_direction: DirectionV1
    source_mention_local_id: str = Field(min_length=1, max_length=128)
    target_mention_local_id: str = Field(min_length=1, max_length=128)
    source_mention_surface_sha256: str
    target_mention_surface_sha256: str
    source_mention_evidence_ref: str = Field(min_length=1, max_length=64)
    target_mention_evidence_ref: str = Field(min_length=1, max_length=64)
    evidence_ref_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_MAPPING_EVIDENCE_REFS)
    evidence_identity_hashes: tuple[str, ...] = Field(min_length=1, max_length=MAX_MAPPING_EVIDENCE_REFS)
    evidence_snapshots: tuple[RawClaimEvidenceSnapshotV1, ...] = Field(
        min_length=1,
        max_length=MAX_MAPPING_EVIDENCE_REFS,
    )
    semantic_projection_fingerprint: str
    claim_json_sha256: str
    snapshot_fingerprint: str | None = None

    @field_validator(
        "claim_content_scoped_fingerprint",
        "extraction_occurrence_fingerprint",
        "raw_predicate_sha256",
        "source_mention_surface_sha256",
        "target_mention_surface_sha256",
        "semantic_projection_fingerprint",
        "claim_json_sha256",
        "snapshot_fingerprint",
    )
    @classmethod
    def validate_snapshot_hashes(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @field_validator("source_mention_local_id", "target_mention_local_id", mode="before")
    @classmethod
    def normalize_snapshot_ids(cls, value: Any, info) -> str:
        return _bounded_string(value, field=info.field_name, limit=128)

    @field_validator("source_mention_evidence_ref", "target_mention_evidence_ref", mode="before")
    @classmethod
    def normalize_snapshot_refs(cls, value: Any, info) -> str:
        return _bounded_string(value, field=info.field_name, limit=64)

    @field_validator("evidence_ref_ids", mode="before")
    @classmethod
    def normalize_snapshot_evidence_refs(cls, value: Any) -> tuple[str, ...]:
        return _normalized_refs(value, field="evidence_ref_ids")

    @field_validator("evidence_identity_hashes", mode="before")
    @classmethod
    def normalize_snapshot_identity_hashes(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("evidence_identity_hashes must be a list or tuple")
        hashes = tuple(_sha256(item, field="evidence_identity_hash") for item in value)
        if not hashes or len(hashes) != len(set(hashes)):
            raise ValueError("evidence identities must be unique and non-empty")
        return tuple(sorted(hashes))

    @field_validator("evidence_snapshots", mode="after")
    @classmethod
    def sort_snapshot_evidence(
        cls,
        value: tuple[RawClaimEvidenceSnapshotV1, ...],
    ) -> tuple[RawClaimEvidenceSnapshotV1, ...]:
        return tuple(sorted(value, key=lambda item: item.evidence_ref_id))

    @classmethod
    def from_claim(
        cls,
        claim: RawClaimV1,
        *,
        semantic_projection: CanonicalSemanticProjectionV1,
    ) -> RawClaimMappingSnapshotV1:
        if not isinstance(claim, RawClaimV1):
            raise TypeError("claim snapshot requires RawClaimV1")
        canonical = canonical_raw_claim_json(claim)
        evidence_identities = tuple(sorted(stable_evidence_identity(reference) for reference in claim.evidence_refs))
        evidence_snapshots = tuple(
            RawClaimEvidenceSnapshotV1.from_reference(reference) for reference in claim.evidence_refs
        )
        return cls(
            claim_id=claim.claim_id,
            library_id=claim.library_id,
            document_id=claim.document_id,
            document_revision_id=claim.document_revision_id,
            revision_no=claim.revision_no,
            job_id=claim.job_id,
            extraction_unit_id=claim.extraction_unit_id,
            extraction_occurrence_id=claim.extraction_occurrence_id,
            claim_content_scoped_fingerprint=claim.content_scoped_claim_fingerprint or "",
            extraction_occurrence_fingerprint=claim.extraction_occurrence_fingerprint or "",
            raw_predicate_sha256=_text_sha256(claim.raw_predicate),
            surface_direction=claim.surface_direction,
            source_mention_local_id=claim.source_mention.local_id,
            target_mention_local_id=claim.target_mention.local_id,
            source_mention_surface_sha256=_text_sha256(claim.source_mention.surface),
            target_mention_surface_sha256=_text_sha256(claim.target_mention.surface),
            source_mention_evidence_ref=claim.source_mention.evidence_ref,
            target_mention_evidence_ref=claim.target_mention.evidence_ref,
            evidence_ref_ids=tuple(reference.ref_id for reference in claim.evidence_refs),
            evidence_identity_hashes=evidence_identities,
            evidence_snapshots=evidence_snapshots,
            semantic_projection_fingerprint=semantic_projection.semantic_projection_fingerprint or "",
            claim_json_sha256=_hash_json(json.loads(canonical)),
        )

    @model_validator(mode="after")
    def validate_snapshot_evidence(self) -> RawClaimMappingSnapshotV1:
        snapshot_ids = tuple(item.evidence_ref_id for item in self.evidence_snapshots)
        if snapshot_ids != self.evidence_ref_ids:
            raise ValueError("claim evidence snapshot ids do not match the claim evidence subset")
        if tuple(sorted(item.stable_evidence_identity_hash for item in self.evidence_snapshots)) != self.evidence_identity_hashes:
            raise ValueError("claim evidence snapshot identities do not match the claim")
        expected = _computed_model_fingerprint(self, exclude="snapshot_fingerprint")
        object.__setattr__(
            self,
            "snapshot_fingerprint",
            _validate_model_fingerprint(self.snapshot_fingerprint, expected, field="snapshot_fingerprint"),
        )
        return self


class DecisionValidationBindingV1(_MappingModel):
    """Safe decision identity binding derived from the full M3A projection."""

    decision_schema_version: Literal["claim_decision_projection_v1"]
    decision_id: UUID
    decision_fingerprint: str
    decision_version: StrictInt = Field(ge=1)
    decision_kind: MappingDecisionKind
    status: Literal["pending"]
    reason_code: str = Field(min_length=1, max_length=64)
    library_id: UUID
    document_id: UUID
    document_revision_id: UUID
    revision_no: StrictInt = Field(ge=1)
    job_id: UUID
    extraction_unit_id: UUID
    claim_id: UUID
    extraction_occurrence_id: UUID | None = None
    decision_payload_sha256: str
    binding_fingerprint: str | None = None

    @field_validator("decision_fingerprint", "decision_payload_sha256", "binding_fingerprint")
    @classmethod
    def validate_decision_hashes(cls, value: str | None, info) -> str | None:
        return None if value is None else _sha256(value, field=info.field_name)

    @classmethod
    def from_projection(
        cls,
        projection: ClaimDecisionProjectionV1,
        *,
        job_id: UUID,
        extraction_unit_id: UUID,
    ) -> DecisionValidationBindingV1:
        return cls(
            decision_schema_version=projection.decision_schema_version,
            decision_id=projection.decision_id,
            decision_fingerprint=projection.decision_fingerprint or "",
            decision_version=projection.decision_version,
            decision_kind=projection.decision_kind,
            status=projection.status,
            reason_code=projection.reason_code,
            library_id=projection.library_id,
            document_id=projection.document_id,
            document_revision_id=projection.document_revision_id,
            revision_no=projection.revision_no,
            job_id=job_id,
            extraction_unit_id=extraction_unit_id,
            claim_id=projection.claim_id,
            extraction_occurrence_id=projection.extraction_occurrence_id,
            decision_payload_sha256=_hash_json(json.loads(canonical_claim_decision_json(projection))),
        )

    @model_validator(mode="after")
    def validate_binding_fingerprint(self) -> DecisionValidationBindingV1:
        expected = _computed_model_fingerprint(self, exclude="binding_fingerprint")
        object.__setattr__(
            self,
            "binding_fingerprint",
            _validate_model_fingerprint(self.binding_fingerprint, expected, field="binding_fingerprint"),
        )
        return self


def _validate_decision_projection_against_claim(
    decision: ClaimDecisionProjectionV1,
    claim: RawClaimV1,
) -> ClaimDecisionProjectionV1:
    validated = ClaimDecisionProjectionV1.model_validate(
        json.loads(canonical_claim_decision_json(decision))
    )
    expected_fingerprint = claim_decision_fingerprint(validated)
    if validated.decision_fingerprint != expected_fingerprint:
        raise ValueError("decision fingerprint does not match the complete projection")
    expected_id = deterministic_decision_id(CLAIM_DECISION_ID_NAMESPACE, expected_fingerprint)
    if validated.decision_id != expected_id:
        raise ValueError("decision id does not match the production decision namespace")
    if (
        validated.library_id != claim.library_id
        or validated.document_id != claim.document_id
        or validated.document_revision_id != claim.document_revision_id
        or validated.revision_no != claim.revision_no
        or validated.claim_id != claim.claim_id
        or validated.extraction_occurrence_id not in (None, claim.extraction_occurrence_id)
    ):
        raise ValueError("decision projection crosses the RawClaim scope")

    proposal = validated.proposal
    claim_evidence = {reference.ref_id for reference in claim.evidence_refs}
    if validated.decision_kind == "mapping_candidate":
        if not isinstance(proposal, MappingCandidateProposalV1):
            raise ValueError("mapping decision requires a mapping proposal")
        if (
            proposal.raw_predicate != claim.raw_predicate
            or proposal.source_mention != claim.source_mention
            or proposal.target_mention != claim.target_mention
            or proposal.surface_direction != claim.surface_direction
        ):
            raise ValueError("decision proposal does not match the RawClaim")
        required = {claim.source_mention.evidence_ref, claim.target_mention.evidence_ref}
        if not required.issubset(set(proposal.evidence_ref_ids)):
            raise ValueError("decision proposal omits an endpoint evidence reference")
    else:
        if not isinstance(proposal, SchemaExtensionCandidateProposalV1):
            raise ValueError("schema extension decision requires an extension proposal")
        if (
            proposal.raw_predicate != claim.raw_predicate
            or proposal.surface_direction != claim.surface_direction
            or proposal.source_endpoint.local_id != claim.source_mention.local_id
            or proposal.source_endpoint.surface != claim.source_mention.surface
            or proposal.source_endpoint.entity_type_hint != claim.source_mention.entity_type_hint
            or proposal.target_endpoint.local_id != claim.target_mention.local_id
            or proposal.target_endpoint.surface != claim.target_mention.surface
            or proposal.target_endpoint.entity_type_hint != claim.target_mention.entity_type_hint
        ):
            raise ValueError("schema extension proposal does not match the RawClaim")
        if claim.source_mention.evidence_ref not in proposal.source_endpoint.evidence_ref_ids:
            raise ValueError("schema extension proposal omits the source evidence reference")
        if claim.target_mention.evidence_ref not in proposal.target_endpoint.evidence_ref_ids:
            raise ValueError("schema extension proposal omits the target evidence reference")
        if not set(proposal.source_endpoint.evidence_ref_ids + proposal.target_endpoint.evidence_ref_ids).issubset(
            claim_evidence
        ):
            raise ValueError("schema extension endpoint evidence crosses the RawClaim")
    if not set(proposal.evidence_ref_ids).issubset(claim_evidence):
        raise ValueError("decision proposal evidence crosses the RawClaim")
    return validated


def _load_strict_raw_claim_payload(value: bytes | str | Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """Load raw claim JSON without allowing RawClaimV1 coercion to erase types."""
    if isinstance(value, RawClaimV1):
        raise TypeError("authoritative mapping ingress requires raw claim JSON, not RawClaimV1")
    if isinstance(value, bytes):
        try:
            decoded: Any = json.loads(
                value.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_object_pairs,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("raw claim JSON is invalid") from exc
    elif isinstance(value, str):
        try:
            decoded = json.loads(value, object_pairs_hook=_reject_duplicate_json_object_pairs)
        except json.JSONDecodeError as exc:
            raise ValueError("raw claim JSON is invalid") from exc
    elif isinstance(value, Mapping):
        decoded = dict(value)
    else:
        raise TypeError("authoritative mapping ingress requires bytes, str, or a JSON mapping")
    if not isinstance(decoded, Mapping):
        raise ValueError("raw claim JSON must contain an object")
    normalized = _validate_json_value(dict(decoded))
    if not isinstance(normalized, dict):  # pragma: no cover - guarded above.
        raise ValueError("raw claim JSON must contain an object")
    if len(_canonical_json(normalized).encode("utf-8")) > 64 * 1024:
        raise ValueError("raw claim JSON exceeds the bounded size limit")
    negation = normalized.get("negation")
    if not isinstance(negation, Mapping) or type(negation.get("value")) is not bool:
        raise ValueError("claim.negation.value must be a native JSON boolean")
    _reject_coercible_mapping_numeric_types(normalized)
    canonical_payload = _canonical_json(normalized)
    return json.loads(canonical_payload), canonical_payload


class CanonicalMappingInputV1(_MappingModel):
    _authoritative_raw_claim_json: str | None = PrivateAttr(default=None)
    _authoritative_claim_object_id: int | None = PrivateAttr(default=None)

    schema_version: CanonicalMappingInputSchema = CANONICAL_MAPPING_INPUT_SCHEMA_VERSION
    mapping_schema_hash: str = Field(default_factory=_mapping_schema_hash)
    canonical_schema_hash: str = Field(default_factory=_canonical_schema_hash)
    raw_claim_authority_json: str
    raw_claim_authority_sha256: str
    claim: RawClaimV1
    claim_snapshot: RawClaimMappingSnapshotV1 | None = None
    claim_id: UUID
    library_id: UUID
    document_id: UUID
    document_revision_id: UUID
    revision_no: StrictInt = Field(ge=1)
    job_id: UUID
    extraction_unit_id: UUID
    extraction_occurrence_id: UUID
    surface_raw_predicate: str = Field(min_length=1, max_length=256)
    surface_direction: DirectionV1
    source_mention: ClaimMentionV1
    target_mention: ClaimMentionV1
    endpoint_type_binding: EndpointTypeBindingV1
    source_endpoint_resolution: EndpointResolutionAttestationV1 | None = None
    target_endpoint_resolution: EndpointResolutionAttestationV1 | None = None
    verified_evidence: tuple[EvidenceReferenceV1, ...] = Field(min_length=1, max_length=MAX_MAPPING_EVIDENCE_REFS)
    evidence_bindings: tuple[EvidenceValidationBindingV1, ...] = Field(
        min_length=1,
        max_length=MAX_MAPPING_EVIDENCE_REFS,
    )
    verified_evidence_ref_ids: tuple[str, ...] | None = None
    frozen_ontology: FrozenOntologySnapshotV1
    ontology_version_id: UUID
    ontology_snapshot_hash: str
    ontology_contract_version: str
    authorization_registry_snapshot: MappingAuthorizationRegistrySnapshotV1
    authorization_provenance: MappingAuthorizationProvenanceV1 | None = None
    decision: ClaimDecisionProjectionV1 | None = None
    decision_binding: DecisionValidationBindingV1 | None = None
    decision_id: UUID | None = None
    decision_fingerprint: str | None = None
    decision_kind: MappingDecisionKind | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_non_boolean_raw_semantics(cls, value: Any) -> Any:
        """Reject coercible raw JSON before RawClaimV1 sees it."""
        if not isinstance(value, Mapping):
            return value
        claim = value.get("claim")
        if not isinstance(claim, Mapping):
            return value
        negation = claim.get("negation")
        if isinstance(negation, Mapping) and "value" in negation and type(negation["value"]) is not bool:
            raise ValueError("claim.negation.value must be a native JSON boolean")
        return value

    @field_validator("raw_claim_authority_json", mode="before")
    @classmethod
    def normalize_raw_claim_authority_json(cls, value: Any) -> str:
        _, canonical_payload = _load_strict_raw_claim_payload(value)
        return canonical_payload

    @field_validator("raw_claim_authority_sha256")
    @classmethod
    def validate_raw_claim_authority_sha256(cls, value: str) -> str:
        return _sha256(value, field="raw_claim_authority_sha256")

    @field_validator("mapping_schema_hash")
    @classmethod
    def validate_mapping_schema_hash(cls, value: str) -> str:
        return _validate_schema_hash(value, expected=_mapping_schema_hash(), field="mapping_schema_hash")

    @field_validator("canonical_schema_hash")
    @classmethod
    def validate_canonical_schema_hash(cls, value: str) -> str:
        return _validate_schema_hash(value, expected=_canonical_schema_hash(), field="canonical_schema_hash")

    @field_validator("surface_raw_predicate", mode="before")
    @classmethod
    def normalize_surface_predicate(cls, value: Any) -> str:
        return _bounded_string(value, field="surface_raw_predicate", limit=256)

    @field_validator("verified_evidence_ref_ids", mode="before")
    @classmethod
    def normalize_selected_refs(cls, value: Any) -> tuple[str, ...] | None:
        return _optional_refs(value)

    @field_validator("ontology_snapshot_hash")
    @classmethod
    def validate_input_snapshot_hash(cls, value: str) -> str:
        return _sha256(value, field="ontology_snapshot_hash")

    @field_validator("ontology_contract_version", mode="before")
    @classmethod
    def normalize_ontology_contract_version(cls, value: Any) -> str:
        return _bounded_token(value, field="ontology_contract_version", limit=64)

    @field_validator("decision_fingerprint")
    @classmethod
    def validate_decision_fingerprint(cls, value: str | None) -> str | None:
        return None if value is None else _sha256(value, field="decision_fingerprint")

    @field_validator("verified_evidence", mode="after")
    @classmethod
    def sort_verified_evidence(cls, value: tuple[EvidenceReferenceV1, ...]) -> tuple[EvidenceReferenceV1, ...]:
        return tuple(sorted(value, key=lambda reference: reference.ref_id))

    @field_validator("evidence_bindings", mode="after")
    @classmethod
    def sort_evidence_bindings(cls, value: tuple[EvidenceValidationBindingV1, ...]) -> tuple[EvidenceValidationBindingV1, ...]:
        return tuple(sorted(value, key=lambda binding: (binding.evidence_ref_id, binding.stable_evidence_identity_hash)))

    @model_validator(mode="after")
    def validate_claim_scope_and_evidence(self) -> CanonicalMappingInputV1:
        # Rebuild from canonical JSON at every validation boundary.  This is
        # deliberate: RawClaimV1 is frozen shallowly, while qualifier values
        # are bounded JSON objects that can still be mutated by a caller.
        authority_payload, authority_json = _load_strict_raw_claim_payload(self.raw_claim_authority_json)
        authority_claim = RawClaimV1.model_validate(authority_payload)
        if authority_json != self.raw_claim_authority_json:
            raise ValueError("raw claim authority JSON must be canonical")
        if self.raw_claim_authority_sha256 != _text_sha256(authority_json):
            raise ValueError("raw claim authority hash does not match the canonical payload")
        claim_json = canonical_raw_claim_json(self.claim)
        claim = RawClaimV1.model_validate(json.loads(claim_json))
        if canonical_raw_claim_json(authority_claim) != claim_json:
            raise ValueError("raw claim authority does not match the RawClaim")
        object.__setattr__(self, "claim", claim)
        object.__setattr__(
            self,
            "claim_snapshot",
            _revalidate_nested(self.claim_snapshot, RawClaimMappingSnapshotV1, field="claim_snapshot"),
        )
        object.__setattr__(
            self,
            "endpoint_type_binding",
            _revalidate_nested(self.endpoint_type_binding, EndpointTypeBindingV1, field="endpoint_type_binding"),
        )
        object.__setattr__(
            self,
            "source_mention",
            _revalidate_nested(self.source_mention, ClaimMentionV1, field="source_mention"),
        )
        object.__setattr__(
            self,
            "target_mention",
            _revalidate_nested(self.target_mention, ClaimMentionV1, field="target_mention"),
        )
        object.__setattr__(
            self,
            "source_endpoint_resolution",
            _revalidate_nested(
                self.source_endpoint_resolution,
                EndpointResolutionAttestationV1,
                field="source endpoint resolution",
            ),
        )
        object.__setattr__(
            self,
            "target_endpoint_resolution",
            _revalidate_nested(
                self.target_endpoint_resolution,
                EndpointResolutionAttestationV1,
                field="target endpoint resolution",
            ),
        )
        object.__setattr__(
            self,
            "verified_evidence",
            tuple(
                _revalidate_nested(reference, EvidenceReferenceV1, field="verified_evidence")
                for reference in self.verified_evidence
            ),
        )
        object.__setattr__(
            self,
            "evidence_bindings",
            tuple(
                _revalidate_nested(binding, EvidenceValidationBindingV1, field="evidence_bindings")
                for binding in self.evidence_bindings
            ),
        )
        object.__setattr__(
            self,
            "frozen_ontology",
            _revalidate_nested(self.frozen_ontology, FrozenOntologySnapshotV1, field="frozen_ontology"),
        )
        object.__setattr__(
            self,
            "authorization_registry_snapshot",
            _revalidate_nested(
                self.authorization_registry_snapshot,
                MappingAuthorizationRegistrySnapshotV1,
                field="authorization registry snapshot",
            ),
        )
        object.__setattr__(
            self,
            "authorization_provenance",
            _revalidate_nested(
                self.authorization_provenance,
                MappingAuthorizationProvenanceV1,
                field="authorization_provenance",
            ),
        )
        object.__setattr__(
            self,
            "decision",
            _revalidate_nested(self.decision, ClaimDecisionProjectionV1, field="decision"),
        )
        object.__setattr__(
            self,
            "decision_binding",
            _revalidate_nested(self.decision_binding, DecisionValidationBindingV1, field="decision_binding"),
        )
        semantic_projection = semantic_projection_from_claim(claim)
        expected_snapshot = RawClaimMappingSnapshotV1.from_claim(
            claim,
            semantic_projection=semantic_projection,
        )
        if self.claim_snapshot not in (None, expected_snapshot):
            raise ValueError("claim mapping snapshot does not match the RawClaim")
        object.__setattr__(self, "claim_snapshot", expected_snapshot)
        expected = {
            "claim_id": claim.claim_id,
            "library_id": claim.library_id,
            "document_id": claim.document_id,
            "document_revision_id": claim.document_revision_id,
            "revision_no": claim.revision_no,
            "job_id": claim.job_id,
            "extraction_unit_id": claim.extraction_unit_id,
            "extraction_occurrence_id": claim.extraction_occurrence_id,
            "surface_raw_predicate": claim.raw_predicate,
            "surface_direction": claim.surface_direction,
            "source_mention": claim.source_mention,
            "target_mention": claim.target_mention,
        }
        for field, expected_value in expected.items():
            if getattr(self, field) != expected_value:
                raise ValueError(f"canonical mapping input {field} does not match the RawClaim")
        if self.ontology_version_id != self.frozen_ontology.ontology_version_id:
            raise ValueError("ontology version does not match the frozen snapshot")
        if self.ontology_snapshot_hash != self.frozen_ontology.ontology_snapshot_hash:
            raise ValueError("ontology snapshot hash does not match the frozen snapshot")
        if self.ontology_contract_version != self.frozen_ontology.ontology_contract_version:
            raise ValueError("ontology contract version does not match the frozen snapshot")
        if claim.ontology_snapshot_hash is not None and claim.ontology_snapshot_hash != self.ontology_snapshot_hash:
            raise ValueError("RawClaim ontology snapshot hash does not match the frozen snapshot")

        expected_scope = MappingScopeV1(
            library_id=claim.library_id,
            document_id=claim.document_id,
            document_revision_id=claim.document_revision_id,
            revision_no=claim.revision_no,
            job_id=claim.job_id,
            extraction_unit_id=claim.extraction_unit_id,
            claim_id=claim.claim_id,
            extraction_occurrence_id=claim.extraction_occurrence_id,
        )
        if self.authorization_registry_snapshot.scope != expected_scope:
            raise ValueError("authorization registry scope does not match the RawClaim")
        if self.authorization_registry_snapshot.ontology_snapshot_hash != self.ontology_snapshot_hash:
            raise ValueError("authorization registry ontology hash does not match the frozen ontology")
        if self.authorization_provenance is not None:
            if self.authorization_provenance.registry_snapshot != self.authorization_registry_snapshot:
                raise ValueError("authorization provenance is not bound to the registry snapshot")
            if self.authorization_provenance.registry_hash != self.authorization_registry_snapshot.registry_snapshot_hash:
                raise ValueError("authorization provenance registry hash does not match the snapshot")

        endpoint_verified_ids = set(self.verified_evidence_ref_ids or ())
        endpoint_binding_ids = {binding.evidence_ref_id for binding in self.evidence_bindings}
        if not endpoint_verified_ids:
            endpoint_verified_ids = endpoint_binding_ids

        def validate_endpoint_resolution(
            value: EndpointResolutionAttestationV1 | None,
            *,
            role: Literal["source", "target"],
            mention: ClaimMentionV1,
            type_key: str | None,
        ) -> None:
            if type_key is None:
                if value is not None:
                    raise ValueError(f"{role} endpoint resolution must be absent when its type is unknown")
                return
            if value is None:
                raise ValueError(f"{role} endpoint resolution authority is required")
            if (
                value.mention_role != role
                or value.mention_local_id != mention.local_id
                or value.mention_surface_sha256 != _text_sha256(mention.surface)
                or value.entity_type_key != type_key
                or value.scope != expected_scope
                or mention.evidence_ref not in value.evidence_ref_ids
            ):
                raise ValueError(f"{role} endpoint resolution does not match the RawClaim scope or mention")
            if not set(value.evidence_ref_ids).issubset({reference.ref_id for reference in claim.evidence_refs}):
                raise ValueError(f"{role} endpoint resolution evidence crosses the RawClaim")
            if not set(value.evidence_ref_ids).issubset(endpoint_verified_ids & endpoint_binding_ids):
                raise ValueError(f"{role} endpoint resolution evidence is not in the verified evidence subset")

        validate_endpoint_resolution(
            self.source_endpoint_resolution,
            role="source",
            mention=claim.source_mention,
            type_key=self.endpoint_type_binding.source_type_key,
        )
        validate_endpoint_resolution(
            self.target_endpoint_resolution,
            role="target",
            mention=claim.target_mention,
            type_key=self.endpoint_type_binding.target_type_key,
        )

        evidence_by_id = {reference.ref_id: reference for reference in claim.evidence_refs}
        verified_by_id = {reference.ref_id: reference for reference in self.verified_evidence}
        binding_by_id = {binding.evidence_ref_id: binding for binding in self.evidence_bindings}
        if len(verified_by_id) != len(self.verified_evidence) or len(binding_by_id) != len(self.evidence_bindings):
            raise ValueError("verified evidence references must be unique")
        if set(verified_by_id) != set(binding_by_id) or not set(binding_by_id).issubset(evidence_by_id):
            raise ValueError("typed evidence bindings do not match the RawClaim subset")
        selected_ids = self.verified_evidence_ref_ids or tuple(sorted(binding_by_id))
        if set(selected_ids) != set(binding_by_id):
            raise ValueError("verified evidence reference ids do not match typed bindings")
        object.__setattr__(self, "verified_evidence_ref_ids", tuple(sorted(selected_ids)))
        required_mention_refs = {claim.source_mention.evidence_ref, claim.target_mention.evidence_ref}
        if not required_mention_refs.issubset(set(selected_ids)):
            raise ValueError("verified evidence must include source and target mention references")
        for ref_id, binding in binding_by_id.items():
            reference = evidence_by_id.get(ref_id)
            if reference is None or reference.locator is None:
                raise ValueError("canonical mapping requires a verified locator for every binding")
            expected_binding = EvidenceValidationBindingV1.from_reference(
                reference,
                attestation=binding.attestation,
            )
            if binding != expected_binding:
                raise ValueError("typed evidence binding identity or scope does not match the RawClaim")

        for type_key in (self.endpoint_type_binding.source_type_key, self.endpoint_type_binding.target_type_key):
            if type_key is not None and type_key not in self.frozen_ontology.entity_type_keys:
                raise ValueError("endpoint type is not declared by the frozen ontology")
        if self.authorization_provenance is not None and not isinstance(
            self.authorization_provenance,
            MappingAuthorizationProvenanceV1,
        ):
            raise ValueError("authorization provenance is invalid")
        decision_fields = (self.decision_id, self.decision_fingerprint, self.decision_kind)
        if self.decision is None and any(value is not None for value in decision_fields):
            raise ValueError("decision fields require the complete decision projection")
        if self.decision is None and self.decision_binding is not None:
            raise ValueError("decision binding requires the complete decision projection")
        if self.decision is not None:
            decision = _validate_decision_projection_against_claim(self.decision, claim)
            expected_binding = DecisionValidationBindingV1.from_projection(
                decision,
                job_id=claim.job_id,
                extraction_unit_id=claim.extraction_unit_id,
            )
            if self.decision_binding not in (None, expected_binding):
                raise ValueError("decision binding does not match the complete projection")
            expected_identity = {
                "decision_id": decision.decision_id,
                "decision_fingerprint": decision.decision_fingerprint,
                "decision_kind": decision.decision_kind,
            }
            for field, expected_value in expected_identity.items():
                supplied = getattr(self, field)
                if supplied is not None and supplied != expected_value:
                    raise ValueError(f"{field} does not match the complete decision projection")
            object.__setattr__(self, "decision", decision)
            object.__setattr__(self, "decision_binding", expected_binding)
            object.__setattr__(self, "decision_id", decision.decision_id)
            object.__setattr__(self, "decision_fingerprint", decision.decision_fingerprint)
            object.__setattr__(self, "decision_kind", decision.decision_kind)
        _canonical_json(self.model_dump(mode="json"))
        return self

    @classmethod
    def from_raw_claim_json(
        cls,
        raw_claim: bytes | str | Mapping[str, Any],
        *,
        frozen_ontology: FrozenOntologySnapshotV1,
        endpoint_type_binding: EndpointTypeBindingV1,
        source_endpoint_resolution: EndpointResolutionAttestationV1 | None,
        target_endpoint_resolution: EndpointResolutionAttestationV1 | None,
        authorization_registry_snapshot: MappingAuthorizationRegistrySnapshotV1,
        verified_evidence_ref_ids: tuple[str, ...] | list[str] | None = None,
        evidence_attestations: Mapping[str, EvidenceValidationAttestationV1] | None = None,
        authorization_provenance: MappingAuthorizationProvenanceV1 | None = None,
        decision: ClaimDecisionProjectionV1 | None = None,
    ) -> CanonicalMappingInputV1:
        payload, authority_json = _load_strict_raw_claim_payload(raw_claim)
        claim = RawClaimV1.model_validate(payload)
        if evidence_attestations is None:
            raise ValueError("explicit evidence attestations are required")
        selected = set(verified_evidence_ref_ids or (reference.ref_id for reference in claim.evidence_refs))
        verified = tuple(reference for reference in claim.evidence_refs if reference.ref_id in selected)
        bindings = tuple(
            EvidenceValidationBindingV1.from_reference(
                reference,
                attestation=evidence_attestations.get(reference.ref_id),
            )
            for reference in verified
        )
        if any(reference.ref_id not in evidence_attestations for reference in verified):
            raise ValueError("every verified evidence reference requires an attestation")
        input_value = cls(
            claim=claim,
            raw_claim_authority_json=authority_json,
            raw_claim_authority_sha256=_text_sha256(authority_json),
            claim_id=claim.claim_id,
            library_id=claim.library_id,
            document_id=claim.document_id,
            document_revision_id=claim.document_revision_id,
            revision_no=claim.revision_no,
            job_id=claim.job_id,
            extraction_unit_id=claim.extraction_unit_id,
            extraction_occurrence_id=claim.extraction_occurrence_id,
            surface_raw_predicate=claim.raw_predicate,
            surface_direction=claim.surface_direction,
            source_mention=claim.source_mention,
            target_mention=claim.target_mention,
            endpoint_type_binding=endpoint_type_binding,
            source_endpoint_resolution=source_endpoint_resolution,
            target_endpoint_resolution=target_endpoint_resolution,
            verified_evidence=verified,
            evidence_bindings=bindings,
            verified_evidence_ref_ids=verified_evidence_ref_ids,
            frozen_ontology=frozen_ontology,
            ontology_version_id=frozen_ontology.ontology_version_id,
            ontology_snapshot_hash=frozen_ontology.ontology_snapshot_hash,
            ontology_contract_version=frozen_ontology.ontology_contract_version,
            authorization_registry_snapshot=authorization_registry_snapshot,
            authorization_provenance=authorization_provenance,
            decision=decision,
        )
        object.__setattr__(input_value, "_authoritative_raw_claim_json", authority_json)
        object.__setattr__(input_value, "_authoritative_claim_object_id", id(input_value.claim))
        return input_value


def _require_authoritative_input(value: CanonicalMappingInputV1) -> None:
    if not isinstance(value, CanonicalMappingInputV1):
        raise TypeError("authoritative_input must be a CanonicalMappingInputV1")
    if value._authoritative_raw_claim_json is None:
        raise TypeError("authoritative_input must come from raw claim JSON ingress")
    if value._authoritative_claim_object_id != id(value.claim):
        raise CanonicalMappingContractError("authoritative_claim_object_mismatch")
    if value.raw_claim_authority_json != value._authoritative_raw_claim_json:
        raise CanonicalMappingContractError("authoritative_raw_claim_authority_mismatch")
    authority_payload, authority_json = _load_strict_raw_claim_payload(value.raw_claim_authority_json)
    if authority_json != value.raw_claim_authority_json:
        raise CanonicalMappingContractError("authoritative_raw_claim_json_noncanonical")
    if value.raw_claim_authority_sha256 != _text_sha256(authority_json):
        raise CanonicalMappingContractError("authoritative_raw_claim_hash_mismatch")
    authority_claim = RawClaimV1.model_validate(authority_payload)
    claim_json = canonical_raw_claim_json(value.claim)
    if canonical_raw_claim_json(authority_claim) != claim_json:
        raise CanonicalMappingContractError("authoritative_raw_claim_snapshot_mismatch")
    expected_snapshot = RawClaimMappingSnapshotV1.from_claim(
        authority_claim,
        semantic_projection=semantic_projection_from_claim(authority_claim),
    )
    if value.claim_snapshot != expected_snapshot:
        raise CanonicalMappingContractError("authoritative_claim_snapshot_mismatch")
    expected_scope = MappingScopeV1(
        library_id=authority_claim.library_id,
        document_id=authority_claim.document_id,
        document_revision_id=authority_claim.document_revision_id,
        revision_no=authority_claim.revision_no,
        job_id=authority_claim.job_id,
        extraction_unit_id=authority_claim.extraction_unit_id,
        claim_id=authority_claim.claim_id,
        extraction_occurrence_id=authority_claim.extraction_occurrence_id,
    )
    input_scope = MappingScopeV1(
        library_id=value.library_id,
        document_id=value.document_id,
        document_revision_id=value.document_revision_id,
        revision_no=value.revision_no,
        job_id=value.job_id,
        extraction_unit_id=value.extraction_unit_id,
        claim_id=value.claim_id,
        extraction_occurrence_id=value.extraction_occurrence_id,
    )
    if input_scope != expected_scope:
        raise CanonicalMappingContractError("authoritative_scope_mismatch")
    if (
        value.surface_raw_predicate != authority_claim.raw_predicate
        or value.surface_direction != authority_claim.surface_direction
        or value.source_mention != authority_claim.source_mention
        or value.target_mention != authority_claim.target_mention
    ):
        raise CanonicalMappingContractError("authoritative_claim_surface_mismatch")
    evidence_by_id = {reference.ref_id: reference for reference in authority_claim.evidence_refs}
    selected_ids = set(value.verified_evidence_ref_ids or ())
    actual_refs = {reference.ref_id: reference for reference in value.verified_evidence}
    actual_bindings = {binding.evidence_ref_id: binding for binding in value.evidence_bindings}
    if set(actual_refs) != selected_ids or set(actual_bindings) != selected_ids:
        raise CanonicalMappingContractError("authoritative_evidence_subset_mismatch")
    for ref_id, reference in actual_refs.items():
        if evidence_by_id.get(ref_id) != reference:
            raise CanonicalMappingContractError("authoritative_evidence_reference_mismatch")
        binding = actual_bindings[ref_id]
        expected_binding = EvidenceValidationBindingV1.from_reference(
            evidence_by_id[ref_id],
            attestation=binding.attestation,
        )
        if binding != expected_binding:
            raise CanonicalMappingContractError("authoritative_evidence_binding_mismatch")


def _rebuild_authoritative_input(value: CanonicalMappingInputV1) -> CanonicalMappingInputV1:
    _require_authoritative_input(value)
    rebuilt = CanonicalMappingInputV1.model_validate(
        json.loads(_canonical_json(value.model_dump(mode="json")))
    )
    object.__setattr__(rebuilt, "_authoritative_raw_claim_json", value._authoritative_raw_claim_json)
    object.__setattr__(rebuilt, "_authoritative_claim_object_id", id(rebuilt.claim))
    _require_authoritative_input(rebuilt)
    return rebuilt


def _attempt_payload(
    *,
    mapping_schema_hash: str,
    canonical_schema_hash: str,
    raw_claim_authority_sha256: str,
    mapping_version: int,
    library_id: UUID,
    document_id: UUID,
    document_revision_id: UUID,
    revision_no: int,
    job_id: UUID,
    extraction_unit_id: UUID,
    claim_id: UUID,
    claim_content_scoped_fingerprint: str,
    extraction_occurrence_id: UUID,
    extraction_occurrence_fingerprint: str,
    surface_raw_predicate: str,
    surface_direction: DirectionV1,
    endpoint_type_binding: EndpointTypeBindingV1,
    source_endpoint_resolution: EndpointResolutionAttestationV1 | None,
    target_endpoint_resolution: EndpointResolutionAttestationV1 | None,
    evidence_bindings: tuple[EvidenceValidationBindingV1, ...],
    claim_snapshot: RawClaimMappingSnapshotV1,
    frozen_ontology: FrozenOntologySnapshotV1,
    ontology_version_id: UUID,
    ontology_snapshot_hash: str,
    ontology_contract_version: str,
    authorization_registry_snapshot: MappingAuthorizationRegistrySnapshotV1,
    semantic_projection: CanonicalSemanticProjectionV1,
    provenance: MapperProvenanceV1,
    authorization_provenance: MappingAuthorizationProvenanceV1 | None,
    decision_binding: DecisionValidationBindingV1 | None,
    decision_id: UUID | None,
    decision_fingerprint: str | None,
    decision_kind: MappingDecisionKind | None,
) -> dict[str, Any]:
    return {
        "mapping_schema_hash": mapping_schema_hash,
        "canonical_schema_hash": canonical_schema_hash,
        "raw_claim_authority_sha256": raw_claim_authority_sha256,
        "mapping_version": mapping_version,
        "scope": {
            "library_id": str(library_id),
            "document_id": str(document_id),
            "document_revision_id": str(document_revision_id),
            "revision_no": revision_no,
            "job_id": str(job_id),
            "extraction_unit_id": str(extraction_unit_id),
        },
        "claim_id": str(claim_id),
        "claim_content_scoped_fingerprint": claim_content_scoped_fingerprint,
        "extraction_occurrence_id": str(extraction_occurrence_id),
        "extraction_occurrence_fingerprint": extraction_occurrence_fingerprint,
        "surface_raw_predicate": surface_raw_predicate,
        "surface_direction": surface_direction,
        "endpoint_type_binding": endpoint_type_binding.model_dump(mode="json"),
        "source_endpoint_resolution": (
            source_endpoint_resolution.model_dump(mode="json") if source_endpoint_resolution is not None else None
        ),
        "target_endpoint_resolution": (
            target_endpoint_resolution.model_dump(mode="json") if target_endpoint_resolution is not None else None
        ),
        "evidence_bindings": [
            {
                **item.model_dump(mode="json", exclude={"attestation": {"validated_at"}}),
                "attestation": item.attestation.model_dump(mode="json", exclude={"validated_at"}),
            }
            for item in evidence_bindings
        ],
        "claim_snapshot": claim_snapshot.model_dump(mode="json"),
        "frozen_ontology": frozen_ontology.model_dump(mode="json"),
        "ontology_version_id": str(ontology_version_id),
        "ontology_snapshot_hash": ontology_snapshot_hash,
        "ontology_contract_version": ontology_contract_version,
        "authorization_registry_snapshot": authorization_registry_snapshot.model_dump(mode="json"),
        "semantic_projection_fingerprint": semantic_projection.semantic_projection_fingerprint,
        "provenance": provenance.model_dump(mode="json"),
        "authorization_provenance": (
            authorization_provenance.model_dump(mode="json") if authorization_provenance is not None else None
        ),
        "decision_binding": decision_binding.model_dump(mode="json") if decision_binding is not None else None,
        "decision_id": str(decision_id) if decision_id is not None else None,
        "decision_fingerprint": decision_fingerprint,
        "decision_kind": decision_kind,
    }


def _attempt_payload_from_input(
    value: CanonicalMappingInputV1,
    provenance: MapperProvenanceV1,
    mapping_version: int,
) -> dict[str, Any]:
    return _attempt_payload(
        mapping_schema_hash=value.mapping_schema_hash,
        canonical_schema_hash=value.canonical_schema_hash,
        raw_claim_authority_sha256=value.raw_claim_authority_sha256,
        mapping_version=mapping_version,
        library_id=value.library_id,
        document_id=value.document_id,
        document_revision_id=value.document_revision_id,
        revision_no=value.revision_no,
        job_id=value.job_id,
        extraction_unit_id=value.extraction_unit_id,
        claim_id=value.claim_id,
        claim_content_scoped_fingerprint=value.claim.content_scoped_claim_fingerprint or "",
        extraction_occurrence_id=value.extraction_occurrence_id,
        extraction_occurrence_fingerprint=value.claim.extraction_occurrence_fingerprint or "",
        surface_raw_predicate=value.surface_raw_predicate,
        surface_direction=value.surface_direction,
        endpoint_type_binding=value.endpoint_type_binding,
        source_endpoint_resolution=value.source_endpoint_resolution,
        target_endpoint_resolution=value.target_endpoint_resolution,
        evidence_bindings=value.evidence_bindings,
        claim_snapshot=value.claim_snapshot,
        frozen_ontology=value.frozen_ontology,
        ontology_version_id=value.ontology_version_id,
        ontology_snapshot_hash=value.ontology_snapshot_hash,
        ontology_contract_version=value.ontology_contract_version,
        authorization_registry_snapshot=value.authorization_registry_snapshot,
        semantic_projection=semantic_projection_from_claim(value.claim),
        provenance=provenance,
        authorization_provenance=value.authorization_provenance,
        decision_binding=value.decision_binding,
        decision_id=value.decision_id,
        decision_fingerprint=value.decision_fingerprint,
        decision_kind=value.decision_kind,
    )


def _attempt_payload_from_result(value: CanonicalMappingV1) -> dict[str, Any]:
    return _attempt_payload(
        mapping_schema_hash=value.mapping_schema_hash,
        canonical_schema_hash=value.canonical_schema_hash,
        raw_claim_authority_sha256=value.raw_claim_authority_sha256,
        mapping_version=value.mapping_version,
        library_id=value.library_id,
        document_id=value.document_id,
        document_revision_id=value.document_revision_id,
        revision_no=value.revision_no,
        job_id=value.job_id,
        extraction_unit_id=value.extraction_unit_id,
        claim_id=value.claim_id,
        claim_content_scoped_fingerprint=value.claim_content_scoped_fingerprint,
        extraction_occurrence_id=value.extraction_occurrence_id,
        extraction_occurrence_fingerprint=value.extraction_occurrence_fingerprint,
        surface_raw_predicate=value.surface_raw_predicate,
        surface_direction=value.surface_direction,
        endpoint_type_binding=value.endpoint_type_binding,
        source_endpoint_resolution=value.source_endpoint_resolution,
        target_endpoint_resolution=value.target_endpoint_resolution,
        evidence_bindings=value.evidence_bindings,
        claim_snapshot=value.claim_snapshot,
        frozen_ontology=value.frozen_ontology,
        ontology_version_id=value.ontology_version_id,
        ontology_snapshot_hash=value.ontology_snapshot_hash,
        ontology_contract_version=value.ontology_contract_version,
        authorization_registry_snapshot=value.authorization_registry_snapshot,
        semantic_projection=value.semantic_projection,
        provenance=value.provenance,
        authorization_provenance=value.authorization_provenance,
        decision_binding=value.decision_binding,
        decision_id=value.decision_id,
        decision_fingerprint=value.decision_fingerprint,
        decision_kind=value.decision_kind,
    )


def _result_payload(value: CanonicalMappingV1, *, attempt_fingerprint: str) -> dict[str, Any]:
    payload = value.model_dump(mode="json")
    for field in (
        "mapping_attempt_id",
        "mapping_attempt_fingerprint",
        "mapping_result_id",
        "mapping_result_fingerprint",
        "created_at",
    ):
        payload.pop(field, None)
    for binding in payload.get("evidence_bindings", []):
        attestation = binding.get("attestation")
        if isinstance(attestation, dict):
            attestation.pop("validated_at", None)
    payload["mapping_attempt_fingerprint"] = attempt_fingerprint
    return payload


class CanonicalMappingV1(_MappingModel):
    schema_version: CanonicalMappingSchema = CANONICAL_MAPPING_SCHEMA_VERSION
    mapping_schema_hash: str = Field(default_factory=_mapping_schema_hash)
    canonical_schema_hash: str = Field(default_factory=_canonical_schema_hash)
    raw_claim_authority_sha256: str
    mapping_attempt_id: UUID | None = None
    mapping_attempt_fingerprint: str | None = None
    mapping_result_id: UUID | None = None
    mapping_result_fingerprint: str | None = None
    mapping_version: StrictInt = Field(ge=1)
    library_id: UUID
    document_id: UUID
    document_revision_id: UUID
    revision_no: StrictInt = Field(ge=1)
    job_id: UUID
    extraction_unit_id: UUID
    claim_id: UUID
    claim_snapshot: RawClaimMappingSnapshotV1
    claim_content_scoped_fingerprint: str
    extraction_occurrence_id: UUID
    extraction_occurrence_fingerprint: str
    surface_raw_predicate: str = Field(min_length=1, max_length=256)
    surface_direction: DirectionV1
    endpoint_type_binding: EndpointTypeBindingV1
    source_endpoint_resolution: EndpointResolutionAttestationV1 | None = None
    target_endpoint_resolution: EndpointResolutionAttestationV1 | None = None
    evidence_bindings: tuple[EvidenceValidationBindingV1, ...] = Field(
        min_length=1,
        max_length=MAX_MAPPING_EVIDENCE_REFS,
    )
    source_evidence_ref_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_MAPPING_EVIDENCE_REFS)
    target_evidence_ref_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_MAPPING_EVIDENCE_REFS)
    mapping_evidence_ref_ids: tuple[str, ...] = Field(min_length=1, max_length=MAX_MAPPING_EVIDENCE_REFS)
    verified_evidence_identity_hashes: tuple[str, ...] = Field(
        min_length=1,
        max_length=MAX_MAPPING_EVIDENCE_REFS,
    )
    semantic_projection: CanonicalSemanticProjectionV1
    frozen_ontology: FrozenOntologySnapshotV1
    ontology_version_id: UUID
    ontology_snapshot_hash: str
    ontology_contract_version: str
    authorization_registry_snapshot: MappingAuthorizationRegistrySnapshotV1
    provenance: MapperProvenanceV1
    source_provenance: MappingSourceProvenanceV1
    actor_provenance: MappingActorProvenanceV1
    remap_provenance: MappingRemapProvenanceV1 | None = None
    authorization_provenance: MappingAuthorizationProvenanceV1 | None = None
    decision_binding: DecisionValidationBindingV1 | None = None
    decision_id: UUID | None = None
    decision_fingerprint: str | None = None
    decision_kind: MappingDecisionKind | None = None
    outcome: MappingOutcome
    canonical_relation_key: str | None = Field(default=None, max_length=MAX_MAPPING_KEY_LENGTH)
    canonical_direction: CanonicalDirectionV1 | None = None
    canonical_source_endpoint: CanonicalEndpointV1 | None = None
    canonical_target_endpoint: CanonicalEndpointV1 | None = None
    endpoint_transform: EndpointTransform | None = None
    predicate_transform: PredicateTransform | None = None
    mapping_confidence: float | None = None
    reason_code: MappingReasonCode | None = None
    semantic_status: SemanticStatus = "preserved"
    created_at: datetime

    @field_validator("created_at", mode="before")
    @classmethod
    def validate_created_at(cls, value: Any) -> datetime:
        return strict_datetime(value, field="created_at")

    @field_validator("mapping_schema_hash")
    @classmethod
    def validate_mapping_schema_hash(cls, value: str) -> str:
        return _validate_schema_hash(value, expected=_mapping_schema_hash(), field="mapping_schema_hash")

    @field_validator("canonical_schema_hash")
    @classmethod
    def validate_canonical_schema_hash(cls, value: str) -> str:
        return _validate_schema_hash(value, expected=_canonical_schema_hash(), field="canonical_schema_hash")

    @field_validator(
        "mapping_attempt_fingerprint",
        "mapping_result_fingerprint",
        "claim_content_scoped_fingerprint",
        "extraction_occurrence_fingerprint",
        "raw_claim_authority_sha256",
        "ontology_snapshot_hash",
        "decision_fingerprint",
    )
    @classmethod
    def validate_mapping_hashes(cls, value: str | None, info) -> str | None:
        optional = {"mapping_attempt_fingerprint", "mapping_result_fingerprint", "decision_fingerprint"}
        return None if value is None and info.field_name in optional else _sha256(value, field=info.field_name)

    @field_validator("surface_raw_predicate", mode="before")
    @classmethod
    def normalize_output_predicate(cls, value: Any) -> str:
        return _bounded_string(value, field="surface_raw_predicate", limit=256)

    @field_validator("ontology_contract_version", mode="before")
    @classmethod
    def normalize_output_ontology_contract_version(cls, value: Any) -> str:
        return _bounded_token(value, field="ontology_contract_version", limit=64)

    @field_validator("source_evidence_ref_ids", "target_evidence_ref_ids", "mapping_evidence_ref_ids", mode="before")
    @classmethod
    def normalize_output_refs(cls, value: Any, info) -> tuple[str, ...]:
        return _normalized_refs(value, field=info.field_name)

    @field_validator("verified_evidence_identity_hashes", mode="before")
    @classmethod
    def normalize_output_identity_hashes(cls, value: Any) -> tuple[str, ...]:
        if not isinstance(value, (list, tuple)):
            raise ValueError("verified_evidence_identity_hashes must be a list or tuple")
        hashes = tuple(_sha256(item, field="verified_evidence_identity_hash") for item in value)
        if not hashes or len(hashes) != len(set(hashes)):
            raise ValueError("verified evidence identities must be unique and non-empty")
        return tuple(sorted(hashes))

    @field_validator("canonical_relation_key", mode="before")
    @classmethod
    def normalize_output_relation_key(cls, value: Any) -> str | None:
        if value is None:
            return None
        return _bounded_string(value, field="canonical_relation_key", limit=MAX_MAPPING_KEY_LENGTH)

    @field_validator("mapping_confidence", mode="before")
    @classmethod
    def validate_confidence(cls, value: Any) -> float | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("mapping_confidence must be finite")
        if not 0 <= float(value) <= 1:
            raise ValueError("mapping_confidence must be between 0 and 1")
        return float(value)

    @field_validator("evidence_bindings", mode="after")
    @classmethod
    def sort_output_bindings(cls, value: tuple[EvidenceValidationBindingV1, ...]) -> tuple[EvidenceValidationBindingV1, ...]:
        return tuple(sorted(value, key=lambda binding: (binding.evidence_ref_id, binding.stable_evidence_identity_hash)))

    @model_validator(mode="after")
    def validate_result_and_identity(self) -> CanonicalMappingV1:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must include an explicit timezone")
        object.__setattr__(
            self,
            "claim_snapshot",
            _revalidate_nested(self.claim_snapshot, RawClaimMappingSnapshotV1, field="claim_snapshot"),
        )
        object.__setattr__(
            self,
            "endpoint_type_binding",
            _revalidate_nested(self.endpoint_type_binding, EndpointTypeBindingV1, field="endpoint_type_binding"),
        )
        object.__setattr__(
            self,
            "source_endpoint_resolution",
            _revalidate_nested(
                self.source_endpoint_resolution,
                EndpointResolutionAttestationV1,
                field="source endpoint resolution",
            ),
        )
        object.__setattr__(
            self,
            "target_endpoint_resolution",
            _revalidate_nested(
                self.target_endpoint_resolution,
                EndpointResolutionAttestationV1,
                field="target endpoint resolution",
            ),
        )
        object.__setattr__(
            self,
            "evidence_bindings",
            tuple(
                _revalidate_nested(binding, EvidenceValidationBindingV1, field="evidence_bindings")
                for binding in self.evidence_bindings
            ),
        )
        object.__setattr__(
            self,
            "semantic_projection",
            _revalidate_nested(self.semantic_projection, CanonicalSemanticProjectionV1, field="semantic_projection"),
        )
        object.__setattr__(
            self,
            "frozen_ontology",
            _revalidate_nested(self.frozen_ontology, FrozenOntologySnapshotV1, field="frozen_ontology"),
        )
        object.__setattr__(
            self,
            "authorization_registry_snapshot",
            _revalidate_nested(
                self.authorization_registry_snapshot,
                MappingAuthorizationRegistrySnapshotV1,
                field="authorization registry snapshot",
            ),
        )
        object.__setattr__(
            self,
            "provenance",
            _revalidate_nested(self.provenance, MapperProvenanceV1, field="provenance"),
        )
        object.__setattr__(
            self,
            "source_provenance",
            _revalidate_nested(self.source_provenance, MappingSourceProvenanceV1, field="source_provenance"),
        )
        object.__setattr__(
            self,
            "actor_provenance",
            _revalidate_nested(self.actor_provenance, MappingActorProvenanceV1, field="actor_provenance"),
        )
        object.__setattr__(
            self,
            "remap_provenance",
            _revalidate_nested(self.remap_provenance, MappingRemapProvenanceV1, field="remap_provenance"),
        )
        object.__setattr__(
            self,
            "authorization_provenance",
            _revalidate_nested(
                self.authorization_provenance,
                MappingAuthorizationProvenanceV1,
                field="authorization_provenance",
            ),
        )
        object.__setattr__(
            self,
            "decision_binding",
            _revalidate_nested(self.decision_binding, DecisionValidationBindingV1, field="decision_binding"),
        )
        object.__setattr__(
            self,
            "canonical_source_endpoint",
            _revalidate_nested(
                self.canonical_source_endpoint,
                CanonicalEndpointV1,
                field="canonical_source_endpoint",
            ),
        )
        object.__setattr__(
            self,
            "canonical_target_endpoint",
            _revalidate_nested(
                self.canonical_target_endpoint,
                CanonicalEndpointV1,
                field="canonical_target_endpoint",
            ),
        )
        if self.ontology_version_id != self.frozen_ontology.ontology_version_id:
            raise ValueError("result ontology version does not match the frozen snapshot")
        if self.ontology_snapshot_hash != self.frozen_ontology.ontology_snapshot_hash:
            raise ValueError("result ontology snapshot hash does not match the frozen snapshot")
        if self.ontology_contract_version != self.frozen_ontology.ontology_contract_version:
            raise ValueError("result ontology contract version does not match the frozen snapshot")
        snapshot = self.claim_snapshot
        expected_scope = MappingScopeV1(
            library_id=self.library_id,
            document_id=self.document_id,
            document_revision_id=self.document_revision_id,
            revision_no=self.revision_no,
            job_id=self.job_id,
            extraction_unit_id=self.extraction_unit_id,
            claim_id=self.claim_id,
            extraction_occurrence_id=self.extraction_occurrence_id,
        )
        if (
            self.authorization_registry_snapshot.scope != expected_scope
            or self.authorization_registry_snapshot.ontology_snapshot_hash != self.ontology_snapshot_hash
        ):
            raise ValueError("result authorization registry is not bound to the mapping scope")
        if (
            self.claim_id != snapshot.claim_id
            or self.library_id != snapshot.library_id
            or self.document_id != snapshot.document_id
            or self.document_revision_id != snapshot.document_revision_id
            or self.revision_no != snapshot.revision_no
            or self.job_id != snapshot.job_id
            or self.extraction_unit_id != snapshot.extraction_unit_id
            or self.extraction_occurrence_id != snapshot.extraction_occurrence_id
            or self.claim_content_scoped_fingerprint != snapshot.claim_content_scoped_fingerprint
            or self.extraction_occurrence_fingerprint != snapshot.extraction_occurrence_fingerprint
            or _text_sha256(self.surface_raw_predicate) != snapshot.raw_predicate_sha256
            or self.surface_direction != snapshot.surface_direction
        ):
            raise ValueError("canonical mapping result is not bound to its claim snapshot")
        if self.semantic_projection.semantic_projection_fingerprint != snapshot.semantic_projection_fingerprint:
            raise ValueError("semantic projection does not match the claim snapshot")
        decision_fields = (self.decision_id, self.decision_fingerprint, self.decision_kind)
        if any(value is not None for value in decision_fields) and not all(value is not None for value in decision_fields):
            raise ValueError("decision id, fingerprint, and kind must be supplied together")
        if any(value is not None for value in decision_fields) != (self.decision_binding is not None):
            raise ValueError("decision identity requires its validated projection binding")
        if self.decision_binding is not None:
            binding = self.decision_binding
            if (
                binding.decision_id != self.decision_id
                or binding.decision_fingerprint != self.decision_fingerprint
                or binding.decision_kind != self.decision_kind
                or binding.library_id != self.library_id
                or binding.document_id != self.document_id
                or binding.document_revision_id != self.document_revision_id
            or binding.revision_no != self.revision_no
                or binding.job_id != self.job_id
                or binding.extraction_unit_id != self.extraction_unit_id
                or binding.claim_id != self.claim_id
                or binding.extraction_occurrence_id not in (None, self.extraction_occurrence_id)
            ):
                raise ValueError("decision binding crosses the mapping scope")

        bindings_by_id = {binding.evidence_ref_id: binding for binding in self.evidence_bindings}
        if len(bindings_by_id) != len(self.evidence_bindings):
            raise ValueError("mapping evidence bindings must be unique")
        binding_ids = set(bindings_by_id)
        if set(self.mapping_evidence_ref_ids) != binding_ids:
            raise ValueError("mapping evidence ids do not match typed bindings")
        expected_identity_hashes = tuple(sorted(binding.stable_evidence_identity_hash for binding in self.evidence_bindings))
        if self.verified_evidence_identity_hashes != expected_identity_hashes:
            raise ValueError("mapping evidence identity hashes do not match typed bindings")
        if not set(self.source_evidence_ref_ids).issubset(binding_ids) or not set(self.target_evidence_ref_ids).issubset(binding_ids):
            raise ValueError("mapping evidence omits an endpoint evidence reference")
        if self.source_evidence_ref_ids != (snapshot.source_mention_evidence_ref,) or self.target_evidence_ref_ids != (
            snapshot.target_mention_evidence_ref,
        ):
            raise ValueError("mapping endpoint evidence refs do not match the claim snapshot")
        if not set(binding_ids).issubset(set(snapshot.evidence_ref_ids)):
            raise ValueError("mapping evidence refs are not present in the claim snapshot")
        if not set(expected_identity_hashes).issubset(set(snapshot.evidence_identity_hashes)):
            raise ValueError("mapping evidence identities are not present in the claim snapshot")
        snapshot_evidence = {item.evidence_ref_id: item for item in snapshot.evidence_snapshots}
        for ref_id, binding in bindings_by_id.items():
            expected_evidence = snapshot_evidence.get(ref_id)
            if expected_evidence is None or (
                expected_evidence.stable_evidence_identity_hash != binding.stable_evidence_identity_hash
                or expected_evidence.evidence_reference_sha256 != binding.evidence_reference_sha256
                or expected_evidence.quote_sha256 != binding.quote_sha256
                or expected_evidence.unit_text_sha256 != binding.unit_text_sha256
                or expected_evidence.locator_sha256 != binding.attestation.locator_sha256
                or expected_evidence.source_span_hash != binding.source_span_hash
            ):
                raise ValueError("mapping evidence binding does not match the full claim evidence")
        for binding in self.evidence_bindings:
            if (
                binding.library_id != self.library_id
                or binding.document_id != self.document_id
                or binding.document_revision_id != self.document_revision_id
                or binding.revision_no != self.revision_no
                or binding.job_id != self.job_id
                or binding.extraction_unit_id != self.extraction_unit_id
            ):
                raise ValueError("mapping evidence binding crosses result scope")

        semantic_unsupported = _unsupported_semantic_reasons(self.semantic_projection)
        allowed_outcomes = _ALLOWED_OUTCOMES_BY_REASON.get(self.reason_code, set())
        if self.outcome not in allowed_outcomes:
            raise ValueError("mapping outcome and reason_code are incompatible")
        if self.outcome == "mapped":
            if self.decision_kind == "schema_extension_candidate":
                raise ValueError("schema extension decisions cannot produce mapped results")
            if self.reason_code is not None or self.mapping_confidence is None or self.semantic_status != "preserved":
                raise ValueError("mapped_result_fields_invalid")
            if semantic_unsupported:
                raise ValueError("mapped result cannot discard claim semantics")
            canonical_fields = (
                self.canonical_relation_key,
                self.canonical_direction,
                self.canonical_source_endpoint,
                self.canonical_target_endpoint,
                self.endpoint_transform,
                self.predicate_transform,
            )
            if any(value is None for value in canonical_fields):
                raise ValueError("mapped result requires a complete canonical relation")
            if self.canonical_direction == "unknown":  # pragma: no cover - Literal rejects this first.
                raise ValueError("canonical direction cannot be unknown")
            if self.authorization_provenance is None:
                raise ValueError("mapped result requires explicit authorization provenance")
            _validate_result_proposal(self)
        else:
            canonical_fields = (
                self.canonical_relation_key,
                self.canonical_direction,
                self.canonical_source_endpoint,
                self.canonical_target_endpoint,
                self.endpoint_transform,
                self.predicate_transform,
            )
            if any(value is not None for value in canonical_fields):
                raise ValueError("non-mapped result cannot contain a canonical relation")
            if self.outcome in {"blocked", "rejected"} and self.mapping_confidence is not None:
                raise ValueError("blocked or rejected result cannot contain confidence")
            if self.outcome == "ambiguous" and self.semantic_status == "preserved" and semantic_unsupported:
                raise ValueError("unsupported claim semantics must remain ambiguous or blocked")
            if semantic_unsupported and self.reason_code not in semantic_unsupported:
                raise ValueError("unsupported claim semantics require an explicit unsupported reason")

        _validate_remap_result_shape(self)

        expected_attempt = _hash_json(_attempt_payload_from_result(self))
        expected_attempt_id = uuid5(CANONICAL_MAPPING_UUID_NAMESPACE, f"canonical_mapping_attempt_v1:{expected_attempt}")
        if self.mapping_attempt_fingerprint not in (None, expected_attempt):
            raise ValueError("mapping attempt fingerprint does not match the immutable input")
        if self.mapping_attempt_id not in (None, expected_attempt_id):
            raise ValueError("mapping attempt id does not match the attempt fingerprint")
        object.__setattr__(self, "mapping_attempt_fingerprint", expected_attempt)
        object.__setattr__(self, "mapping_attempt_id", expected_attempt_id)

        expected_result = _hash_json(_result_payload(self, attempt_fingerprint=expected_attempt))
        expected_result_id = uuid5(CANONICAL_MAPPING_UUID_NAMESPACE, f"canonical_mapping_result_v1:{expected_result}")
        if self.mapping_result_fingerprint not in (None, expected_result):
            raise ValueError("mapping result fingerprint does not match the immutable result")
        if self.mapping_result_id not in (None, expected_result_id):
            raise ValueError("mapping result id does not match the result fingerprint")
        object.__setattr__(self, "mapping_result_fingerprint", expected_result)
        object.__setattr__(self, "mapping_result_id", expected_result_id)
        _canonical_json(self.model_dump(mode="json"))
        return self


# Freeze hashes from the actual contract schemas, rather than descriptive
# prose.  Any field/enum/boundary change therefore changes the wire hash.
MAPPING_SCHEMA_HASH = _schema_hash(CanonicalMappingInputV1.model_json_schema())
CANONICAL_SCHEMA_HASH = _schema_hash(CanonicalMappingV1.model_json_schema())


_AMBIGUOUS_REASONS = frozenset(
    {
        "unknown_direction",
        "ambiguous_mapping",
        "ambiguous_endpoint",
        "unsupported_negation",
        "unsupported_modality",
        "unsupported_qualifier",
        "unsupported_valid_time",
        "unsupported_effective_time",
    }
)
_BLOCKED_REASONS = frozenset(
    {
        "unknown_predicate",
        "unknown_source_type",
        "unknown_target_type",
        "ontology_snapshot_mismatch",
        "evidence_missing",
        "evidence_invalid",
        "scope_mismatch",
        "no_explicit_mapping",
        "unsupported_negation",
        "unsupported_modality",
        "unsupported_qualifier",
        "unsupported_valid_time",
        "unsupported_effective_time",
        "mapper_error",
    }
)
_REJECTED_REASONS = frozenset({"ontology_relation_not_allowed"})
_ALLOWED_OUTCOMES_BY_REASON: dict[str | None, set[str]] = {None: {"mapped"}}
for _reason in _AMBIGUOUS_REASONS:
    _ALLOWED_OUTCOMES_BY_REASON.setdefault(_reason, set()).add("ambiguous")
for _reason in _BLOCKED_REASONS:
    _ALLOWED_OUTCOMES_BY_REASON.setdefault(_reason, set()).add("blocked")
_ALLOWED_OUTCOMES_BY_REASON["ontology_relation_not_allowed"] = {"rejected"}

_SEMANTIC_STATUS_BY_OUTCOME: dict[str, SemanticStatus] = {
    "mapped": "preserved",
    "ambiguous": "ambiguous",
    "blocked": "blocked",
    "rejected": "preserved",
}


def _unsupported_semantic_reasons(projection: CanonicalSemanticProjectionV1) -> frozenset[str]:
    reasons: set[str] = set()
    if projection.negation_value:
        reasons.add("unsupported_negation")
    if projection.modality_present:
        reasons.add("unsupported_modality")
    if projection.qualifier_present:
        reasons.add("unsupported_qualifier")
    if projection.valid_time_present:
        reasons.add("unsupported_valid_time")
    if projection.effective_time_present:
        reasons.add("unsupported_effective_time")
    return frozenset(reasons)


def _validate_authorization_registry_membership(
    *,
    registry: MappingAuthorizationRegistrySnapshotV1,
    authorization: MappingAuthorizationProvenanceV1,
    scope: MappingScopeV1,
    ontology_snapshot_hash: str,
    surface_raw_predicate: str,
    canonical_relation_key: str,
    endpoint_transform: EndpointTransform,
    predicate_transform: PredicateTransform,
    canonical_direction: CanonicalDirectionV1,
    source_type_key: str,
    target_type_key: str,
) -> None:
    if authorization.registry_snapshot != registry:
        raise CanonicalMappingContractError("mapping_authorization_registry_mismatch")
    if authorization.registry_hash != registry.registry_snapshot_hash:
        raise CanonicalMappingContractError("mapping_authorization_registry_hash_mismatch")
    if registry.scope != scope:
        raise CanonicalMappingContractError("mapping_authorization_scope_mismatch")
    if registry.ontology_snapshot_hash != ontology_snapshot_hash:
        raise CanonicalMappingContractError("mapping_authorization_ontology_mismatch")
    surface_hash = _text_sha256(surface_raw_predicate)
    relation_hash = _text_sha256(canonical_relation_key)
    if (
        authorization.authorized_surface_predicate_sha256 != surface_hash
        or authorization.authorized_canonical_relation_key_sha256 != relation_hash
    ):
        raise CanonicalMappingContractError("mapping_authorization_hash_mismatch")
    matches = [
        entry
        for entry in registry.entries
        if (
            entry.authorization_key == authorization.authorization_key
            and entry.authorization_version == authorization.authorization_version
            and entry.surface_predicate_sha256 == surface_hash
            and entry.canonical_relation_key_sha256 == relation_hash
        )
    ]
    if len(matches) != 1:
        raise CanonicalMappingContractError("mapping_authorization_not_in_registry")
    entry = matches[0]
    if (
        endpoint_transform not in entry.allowed_endpoint_transforms
        or predicate_transform not in entry.allowed_predicate_transforms
        or canonical_direction not in entry.allowed_canonical_directions
        or source_type_key not in entry.allowed_source_type_keys
        or target_type_key not in entry.allowed_target_type_keys
    ):
        raise CanonicalMappingContractError("mapping_authorization_transform_or_type_not_allowed")


def _validate_endpoint_authority_pair(
    *,
    source_endpoint: CanonicalEndpointV1,
    target_endpoint: CanonicalEndpointV1,
    endpoint_transform: EndpointTransform,
    source_resolution: EndpointResolutionAttestationV1 | None,
    target_resolution: EndpointResolutionAttestationV1 | None,
) -> None:
    expected_source = source_resolution if endpoint_transform == "identity" else target_resolution
    expected_target = target_resolution if endpoint_transform == "identity" else source_resolution
    if expected_source is None or expected_target is None:
        raise CanonicalMappingContractError("endpoint_resolution_authority_missing")
    if (
        source_endpoint.resolution_attestation != expected_source
        or target_endpoint.resolution_attestation != expected_target
    ):
        raise CanonicalMappingContractError("endpoint_resolution_authority_mismatch")


def _validate_result_proposal(value: CanonicalMappingV1) -> None:
    """Re-run the M0 proposal checks from the text-free claim snapshot."""
    authorization = value.authorization_provenance
    if authorization is None:
        raise CanonicalMappingContractError("mapping_authorization_missing")
    if authorization.authorized_surface_predicate_sha256 != _text_sha256(value.surface_raw_predicate):
        raise CanonicalMappingContractError("mapping_authorization_predicate_mismatch")
    if value.canonical_relation_key is None:
        raise CanonicalMappingContractError("canonical_relation_missing")
    if authorization.authorized_canonical_relation_key_sha256 != _text_sha256(value.canonical_relation_key):
        raise CanonicalMappingContractError("mapping_authorization_relation_mismatch")
    if value.canonical_relation_key not in value.frozen_ontology.relation_type_keys:
        raise CanonicalMappingContractError("ontology_relation_not_allowed")

    source_type = value.endpoint_type_binding.source_type_key
    target_type = value.endpoint_type_binding.target_type_key
    if source_type is None:
        raise CanonicalMappingContractError("unknown_source_type")
    if target_type is None:
        raise CanonicalMappingContractError("unknown_target_type")
    if value.endpoint_transform == "identity":
        expected_source_id = value.claim_snapshot.source_mention_local_id
        expected_target_id = value.claim_snapshot.target_mention_local_id
        expected_source_type = source_type
        expected_target_type = target_type
    else:
        expected_source_id = value.claim_snapshot.target_mention_local_id
        expected_target_id = value.claim_snapshot.source_mention_local_id
        expected_source_type = target_type
        expected_target_type = source_type
    source_endpoint = value.canonical_source_endpoint
    target_endpoint = value.canonical_target_endpoint
    assert source_endpoint is not None and target_endpoint is not None
    if source_endpoint.role != "source" or target_endpoint.role != "target":
        raise CanonicalMappingContractError("canonical_endpoint_role_mismatch")
    if (
        source_endpoint.mention_local_id != expected_source_id
        or target_endpoint.mention_local_id != expected_target_id
        or source_endpoint.entity_type_key != expected_source_type
        or target_endpoint.entity_type_key != expected_target_type
    ):
        raise CanonicalMappingContractError("endpoint_transform_mismatch")
    if source_endpoint.entity_link.status not in {"resolved", "candidate"}:
        raise CanonicalMappingContractError("source_endpoint_unresolved")
    if target_endpoint.entity_link.status not in {"resolved", "candidate"}:
        raise CanonicalMappingContractError("target_endpoint_unresolved")
    _validate_endpoint_authority_pair(
        source_endpoint=source_endpoint,
        target_endpoint=target_endpoint,
        endpoint_transform=value.endpoint_transform,
        source_resolution=value.source_endpoint_resolution,
        target_resolution=value.target_endpoint_resolution,
    )

    direction = value.canonical_direction
    if direction is None:
        raise CanonicalMappingContractError("canonical_direction_missing")
    if value.surface_direction == "unknown":
        raise CanonicalMappingContractError("unknown_direction_cannot_be_mapped")
    if value.predicate_transform == "identity":
        if direction != value.surface_direction:
            raise CanonicalMappingContractError("predicate_transform_direction_mismatch")
    elif value.predicate_transform == "inverse":
        if direction != _inverse_direction(value.surface_direction):
            raise CanonicalMappingContractError("predicate_inverse_direction_mismatch")
    elif direction != "undirected":
        raise CanonicalMappingContractError("symmetric_predicate_requires_undirected_direction")

    _validate_authorization_registry_membership(
        registry=value.authorization_registry_snapshot,
        authorization=authorization,
        scope=MappingScopeV1(
            library_id=value.library_id,
            document_id=value.document_id,
            document_revision_id=value.document_revision_id,
            revision_no=value.revision_no,
            job_id=value.job_id,
            extraction_unit_id=value.extraction_unit_id,
            claim_id=value.claim_id,
            extraction_occurrence_id=value.extraction_occurrence_id,
        ),
        ontology_snapshot_hash=value.ontology_snapshot_hash,
        surface_raw_predicate=value.surface_raw_predicate,
        canonical_relation_key=value.canonical_relation_key,
        endpoint_transform=value.endpoint_transform,
        predicate_transform=value.predicate_transform,
        canonical_direction=direction,
        source_type_key=expected_source_type,
        target_type_key=expected_target_type,
    )

    if not any(
        constraint.relation_key == value.canonical_relation_key
        and constraint.source_type_key == expected_source_type
        and constraint.target_type_key == expected_target_type
        and constraint.direction == direction
        for constraint in value.frozen_ontology.constraints
    ):
        raise CanonicalMappingContractError("ontology_relation_not_allowed")


def _validate_remap_result_shape(value: CanonicalMappingV1) -> None:
    remap = value.remap_provenance
    if remap is None:
        return
    generation = remap.remap_generation
    if generation is None:
        raise CanonicalMappingContractError("remap_generation_missing")
    if generation == 0:
        return
    if remap.supersedes_scope != MappingScopeV1(
        library_id=value.library_id,
        document_id=value.document_id,
        document_revision_id=value.document_revision_id,
        revision_no=value.revision_no,
        job_id=value.job_id,
        extraction_unit_id=value.extraction_unit_id,
        claim_id=value.claim_id,
        extraction_occurrence_id=value.extraction_occurrence_id,
    ):
        raise CanonicalMappingContractError("remap_predecessor_scope_mismatch")
    if remap.lineage_root_mapping_result_id is None or remap.lineage_root_mapping_result_fingerprint is None:
        raise CanonicalMappingContractError("remap_lineage_root_missing")
    if remap.supersedes_lineage_root_mapping_result_id is None or remap.supersedes_lineage_root_mapping_result_fingerprint is None:
        raise CanonicalMappingContractError("remap_predecessor_lineage_root_missing")


def _validate_repository_remap_predecessor(
    value: CanonicalMappingV1,
    predecessor: CanonicalMappingV1,
) -> None:
    """Validate a remap only after the repository rebuilt its predecessor row.

    This is a pure typed check.  The caller remains responsible for proving
    that ``predecessor`` came from an immutable, scoped database row.
    """
    remap = value.remap_provenance
    if remap is None or remap.remap_generation == 0:
        if predecessor is not None:
            raise CanonicalMappingContractError("generation_zero_cannot_supersede_predecessor")
        return
    predecessor = CanonicalMappingV1.model_validate(
        json.loads(_canonical_json(predecessor.model_dump(mode="json")))
    )
    predecessor_scope = MappingScopeV1(
        library_id=predecessor.library_id,
        document_id=predecessor.document_id,
        document_revision_id=predecessor.document_revision_id,
        revision_no=predecessor.revision_no,
        job_id=predecessor.job_id,
        extraction_unit_id=predecessor.extraction_unit_id,
        claim_id=predecessor.claim_id,
        extraction_occurrence_id=predecessor.extraction_occurrence_id,
    )
    value_scope = MappingScopeV1(
        library_id=value.library_id,
        document_id=value.document_id,
        document_revision_id=value.document_revision_id,
        revision_no=value.revision_no,
        job_id=value.job_id,
        extraction_unit_id=value.extraction_unit_id,
        claim_id=value.claim_id,
        extraction_occurrence_id=value.extraction_occurrence_id,
    )
    if value_scope != predecessor_scope or remap.supersedes_scope != predecessor_scope:
        raise CanonicalMappingContractError("remap_predecessor_scope_mismatch")
    if remap.prior_remap_generation != (
        predecessor.remap_provenance.remap_generation if predecessor.remap_provenance else 0
    ):
        raise CanonicalMappingContractError("remap_generation_gap")
    if remap.supersedes_mapping_result_id != predecessor.mapping_result_id:
        raise CanonicalMappingContractError("remap_predecessor_id_mismatch")
    if remap.supersedes_mapping_result_fingerprint != predecessor.mapping_result_fingerprint:
        raise CanonicalMappingContractError("remap_predecessor_fingerprint_mismatch")
    expected_root_id = (
        predecessor.remap_provenance.lineage_root_mapping_result_id
        if predecessor.remap_provenance and predecessor.remap_provenance.lineage_root_mapping_result_id
        else predecessor.mapping_result_id
    )
    expected_root_fingerprint = (
        predecessor.remap_provenance.lineage_root_mapping_result_fingerprint
        if predecessor.remap_provenance and predecessor.remap_provenance.lineage_root_mapping_result_fingerprint
        else predecessor.mapping_result_fingerprint
    )
    if (
        remap.lineage_root_mapping_result_id != expected_root_id
        or remap.lineage_root_mapping_result_fingerprint != expected_root_fingerprint
        or remap.supersedes_lineage_root_mapping_result_id != expected_root_id
        or remap.supersedes_lineage_root_mapping_result_fingerprint != expected_root_fingerprint
    ):
        raise CanonicalMappingContractError("remap_lineage_root_mismatch")
    if remap.supersedes_source_precedence != predecessor.source_provenance.source_precedence:
        raise CanonicalMappingContractError("remap_predecessor_source_precedence_mismatch")
    if remap.supersedes_actor_precedence != predecessor.actor_provenance.actor_precedence:
        raise CanonicalMappingContractError("remap_predecessor_actor_precedence_mismatch")
    if value.source_provenance.source_precedence < predecessor.source_provenance.source_precedence:
        raise CanonicalMappingContractError("remap_source_precedence_regression")
    if value.actor_provenance.actor_precedence < predecessor.actor_provenance.actor_precedence:
        raise CanonicalMappingContractError("remap_actor_precedence_regression")
    if value.mapping_schema_hash != predecessor.mapping_schema_hash or value.canonical_schema_hash != predecessor.canonical_schema_hash:
        raise CanonicalMappingContractError("remap_schema_authority_mismatch")
    if value.claim_content_scoped_fingerprint != predecessor.claim_content_scoped_fingerprint:
        raise CanonicalMappingContractError("remap_claim_content_mismatch")
    if value.extraction_occurrence_fingerprint != predecessor.extraction_occurrence_fingerprint:
        raise CanonicalMappingContractError("remap_occurrence_mismatch")
    if value.decision_binding != predecessor.decision_binding or value.decision_id != predecessor.decision_id or value.decision_fingerprint != predecessor.decision_fingerprint or value.decision_kind != predecessor.decision_kind:
        raise CanonicalMappingContractError("remap_decision_binding_mismatch")
    if remap.remap_generation > 0 and value.remap_provenance is None:
        raise CanonicalMappingContractError("remap_provenance_missing")


def build_canonical_mapping(
    input_value: CanonicalMappingInputV1,
    *,
    provenance: MapperProvenanceV1,
    source_provenance: MappingSourceProvenanceV1,
    actor_provenance: MappingActorProvenanceV1,
    outcome: MappingOutcome,
    created_at: datetime,
    mapping_version: int = 1,
    proposal: CanonicalMappingProposalV1 | None = None,
    reason_code: MappingReasonCode | None = None,
    mapping_confidence: float | None = None,
    semantic_status: SemanticStatus = "preserved",
    remap_provenance: MappingRemapProvenanceV1 | None = None,
) -> CanonicalMappingV1:
    """M0 pure validated contract factory; this is not an M1 mapper."""
    # Frozen Pydantic instances are not an authority boundary.  Rebuild the
    # input from canonical JSON, but retain the raw-ingress binding.
    input_value = _rebuild_authoritative_input(input_value)
    if not isinstance(provenance, MapperProvenanceV1):
        raise TypeError("provenance must be a validated MapperProvenanceV1")
    if not isinstance(source_provenance, MappingSourceProvenanceV1):
        raise TypeError("source_provenance must be a validated MappingSourceProvenanceV1")
    if not isinstance(actor_provenance, MappingActorProvenanceV1):
        raise TypeError("actor_provenance must be a validated MappingActorProvenanceV1")
    provenance = _revalidate_nested(provenance, MapperProvenanceV1, field="provenance")
    source_provenance = _revalidate_nested(
        source_provenance,
        MappingSourceProvenanceV1,
        field="source_provenance",
    )
    actor_provenance = _revalidate_nested(actor_provenance, MappingActorProvenanceV1, field="actor_provenance")
    if proposal is not None:
        if not isinstance(proposal, CanonicalMappingProposalV1):
            raise TypeError("proposal must be a validated CanonicalMappingProposalV1")
        proposal = _revalidate_nested(proposal, CanonicalMappingProposalV1, field="proposal")
    if remap_provenance is not None:
        if not isinstance(remap_provenance, MappingRemapProvenanceV1):
            raise TypeError("remap_provenance must be a validated MappingRemapProvenanceV1")
        remap_provenance = _revalidate_nested(
            remap_provenance,
            MappingRemapProvenanceV1,
            field="remap_provenance",
        )
        if remap_provenance.remap_generation > 0:
            raise CanonicalMappingContractError(
                "positive remap requires repository predecessor authority"
            )
    if isinstance(mapping_version, bool) or not isinstance(mapping_version, int) or mapping_version < 1:
        raise CanonicalMappingContractError("mapping_version_invalid")
    if outcome not in {"mapped", "ambiguous", "blocked", "rejected"}:
        raise CanonicalMappingContractError("mapping_outcome_invalid")
    semantic_projection = semantic_projection_from_claim(input_value.claim)

    result = CanonicalMappingV1(
        raw_claim_authority_sha256=input_value.raw_claim_authority_sha256,
        mapping_version=mapping_version,
        library_id=input_value.library_id,
        document_id=input_value.document_id,
        document_revision_id=input_value.document_revision_id,
        revision_no=input_value.revision_no,
        job_id=input_value.job_id,
        extraction_unit_id=input_value.extraction_unit_id,
        claim_id=input_value.claim_id,
        claim_snapshot=input_value.claim_snapshot,
        claim_content_scoped_fingerprint=input_value.claim.content_scoped_claim_fingerprint or "",
        extraction_occurrence_id=input_value.extraction_occurrence_id,
        extraction_occurrence_fingerprint=input_value.claim.extraction_occurrence_fingerprint or "",
        surface_raw_predicate=input_value.surface_raw_predicate,
        surface_direction=input_value.surface_direction,
        endpoint_type_binding=input_value.endpoint_type_binding,
        source_endpoint_resolution=input_value.source_endpoint_resolution,
        target_endpoint_resolution=input_value.target_endpoint_resolution,
        evidence_bindings=input_value.evidence_bindings,
        source_evidence_ref_ids=(input_value.claim.source_mention.evidence_ref,),
        target_evidence_ref_ids=(input_value.claim.target_mention.evidence_ref,),
        mapping_evidence_ref_ids=input_value.verified_evidence_ref_ids or (),
        verified_evidence_identity_hashes=tuple(
            sorted(binding.stable_evidence_identity_hash for binding in input_value.evidence_bindings)
        ),
        semantic_projection=semantic_projection,
        frozen_ontology=input_value.frozen_ontology,
        ontology_version_id=input_value.ontology_version_id,
        ontology_snapshot_hash=input_value.ontology_snapshot_hash,
        ontology_contract_version=input_value.ontology_contract_version,
        authorization_registry_snapshot=input_value.authorization_registry_snapshot,
        provenance=provenance,
        source_provenance=source_provenance,
        actor_provenance=actor_provenance,
        remap_provenance=remap_provenance,
        authorization_provenance=(
            proposal.authorization_provenance if proposal is not None else input_value.authorization_provenance
        ),
        decision_binding=input_value.decision_binding,
        decision_id=input_value.decision_id,
        decision_fingerprint=input_value.decision_fingerprint,
        decision_kind=input_value.decision_kind,
        outcome=outcome,
        canonical_relation_key=proposal.canonical_relation_key if proposal else None,
        canonical_direction=proposal.canonical_direction if proposal else None,
        canonical_source_endpoint=proposal.canonical_source_endpoint if proposal else None,
        canonical_target_endpoint=proposal.canonical_target_endpoint if proposal else None,
        endpoint_transform=proposal.endpoint_transform if proposal else None,
        predicate_transform=proposal.predicate_transform if proposal else None,
        mapping_confidence=mapping_confidence,
        reason_code=reason_code,
        semantic_status=semantic_status,
        created_at=created_at,
    )
    _require_authoritative_input(input_value)
    return _validate_result_against_authoritative_input(input_value, result)


def _inverse_direction(direction: DirectionV1) -> CanonicalDirectionV1:
    if direction == "source_to_target":
        return "target_to_source"
    if direction == "target_to_source":
        return "source_to_target"
    raise CanonicalMappingContractError("unknown_direction_cannot_be_transformed")


def _validate_proposal(input_value: CanonicalMappingInputV1, proposal: CanonicalMappingProposalV1) -> None:
    proposal = _revalidate_nested(proposal, CanonicalMappingProposalV1, field="proposal")
    ontology = input_value.frozen_ontology
    source_type = input_value.endpoint_type_binding.source_type_key
    target_type = input_value.endpoint_type_binding.target_type_key
    if source_type is None or target_type is None:
        raise CanonicalMappingContractError("endpoint_type_missing")
    if proposal.canonical_relation_key not in ontology.relation_type_keys:
        raise CanonicalMappingContractError("ontology_relation_not_allowed")
    if proposal.authorization_provenance is None:
        raise CanonicalMappingContractError("mapping_authorization_missing")
    if input_value.authorization_provenance != proposal.authorization_provenance:
        raise CanonicalMappingContractError("mapping_authorization_not_bound_to_input")
    if proposal.authorization_provenance.authorized_surface_predicate_sha256 != _text_sha256(
        input_value.surface_raw_predicate
    ):
        raise CanonicalMappingContractError("mapping_authorization_predicate_mismatch")
    if proposal.authorization_provenance.authorized_canonical_relation_key_sha256 != _text_sha256(
        proposal.canonical_relation_key
    ):
        raise CanonicalMappingContractError("mapping_authorization_relation_mismatch")

    if proposal.endpoint_transform == "identity":
        expected_source_id = input_value.source_mention.local_id
        expected_target_id = input_value.target_mention.local_id
        expected_source_type = source_type
        expected_target_type = target_type
    else:
        expected_source_id = input_value.target_mention.local_id
        expected_target_id = input_value.source_mention.local_id
        expected_source_type = target_type
        expected_target_type = source_type
    if (
        proposal.canonical_source_endpoint.mention_local_id != expected_source_id
        or proposal.canonical_target_endpoint.mention_local_id != expected_target_id
    ):
        raise CanonicalMappingContractError("endpoint_transform_mismatch")
    if (
        proposal.canonical_source_endpoint.entity_type_key != expected_source_type
        or proposal.canonical_target_endpoint.entity_type_key != expected_target_type
    ):
        raise CanonicalMappingContractError("canonical_endpoint_type_mismatch")
    if proposal.canonical_source_endpoint.entity_link.status not in {"resolved", "candidate"}:
        raise CanonicalMappingContractError("source_endpoint_unresolved")
    if proposal.canonical_target_endpoint.entity_link.status not in {"resolved", "candidate"}:
        raise CanonicalMappingContractError("target_endpoint_unresolved")
    if (
        proposal.canonical_source_endpoint.entity_link.mention_local_id
        != proposal.canonical_source_endpoint.mention_local_id
        or proposal.canonical_target_endpoint.entity_link.mention_local_id
        != proposal.canonical_target_endpoint.mention_local_id
    ):
        raise CanonicalMappingContractError("endpoint_entity_link_mismatch")
    if (
        proposal.canonical_source_endpoint.role != "source"
        or proposal.canonical_target_endpoint.role != "target"
    ):
        raise CanonicalMappingContractError("canonical_endpoint_role_mismatch")
    _validate_endpoint_authority_pair(
        source_endpoint=proposal.canonical_source_endpoint,
        target_endpoint=proposal.canonical_target_endpoint,
        endpoint_transform=proposal.endpoint_transform,
        source_resolution=input_value.source_endpoint_resolution,
        target_resolution=input_value.target_endpoint_resolution,
    )

    if proposal.predicate_transform == "identity":
        if input_value.surface_direction == "unknown" or proposal.canonical_direction != input_value.surface_direction:
            raise CanonicalMappingContractError("predicate_transform_direction_mismatch")
    elif proposal.predicate_transform == "inverse":
        if input_value.surface_direction == "unknown" or proposal.canonical_direction != _inverse_direction(input_value.surface_direction):
            raise CanonicalMappingContractError("predicate_inverse_direction_mismatch")
    elif proposal.canonical_direction != "undirected":
        raise CanonicalMappingContractError("symmetric_predicate_requires_undirected_direction")

    _validate_authorization_registry_membership(
        registry=input_value.authorization_registry_snapshot,
        authorization=proposal.authorization_provenance,
        scope=MappingScopeV1(
            library_id=input_value.library_id,
            document_id=input_value.document_id,
            document_revision_id=input_value.document_revision_id,
            revision_no=input_value.revision_no,
            job_id=input_value.job_id,
            extraction_unit_id=input_value.extraction_unit_id,
            claim_id=input_value.claim_id,
            extraction_occurrence_id=input_value.extraction_occurrence_id,
        ),
        ontology_snapshot_hash=input_value.ontology_snapshot_hash,
        surface_raw_predicate=input_value.surface_raw_predicate,
        canonical_relation_key=proposal.canonical_relation_key,
        endpoint_transform=proposal.endpoint_transform,
        predicate_transform=proposal.predicate_transform,
        canonical_direction=proposal.canonical_direction,
        source_type_key=expected_source_type,
        target_type_key=expected_target_type,
    )

    if not any(
        constraint.relation_key == proposal.canonical_relation_key
        and constraint.source_type_key == expected_source_type
        and constraint.target_type_key == expected_target_type
        and constraint.direction == proposal.canonical_direction
        for constraint in ontology.constraints
    ):
        raise CanonicalMappingContractError("ontology_relation_not_allowed")


def _canonical_mapping_datetime_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize only schema-owned datetime fields; leave dynamic claim JSON untouched."""
    if "created_at" in payload:
        payload["created_at"] = canonical_datetime_string(payload["created_at"], field="created_at")
    decision = payload.get("decision")
    if isinstance(decision, dict) and "created_at" in decision:
        decision["created_at"] = canonical_datetime_string(decision["created_at"], field="decision.created_at")
    for binding in payload.get("evidence_bindings", ()):
        if isinstance(binding, dict):
            attestation = binding.get("attestation")
            if isinstance(attestation, dict) and "validated_at" in attestation:
                attestation["validated_at"] = canonical_datetime_string(
                    attestation["validated_at"],
                    field="validated_at",
                )
    return payload


def canonical_mapping_input_json(value: CanonicalMappingInputV1) -> str:
    """Serialize an input only after revalidating its raw authority binding."""
    if not isinstance(value, CanonicalMappingInputV1):
        raise TypeError("authoritative_input must be a CanonicalMappingInputV1")
    input_value = _rebuild_authoritative_input(value)
    return _canonical_json(_canonical_mapping_datetime_payload(input_value.model_dump(mode="json")))


def _validate_result_semantics_against_authoritative_input(
    input_value: CanonicalMappingInputV1,
    result: CanonicalMappingV1,
) -> CanonicalMappingV1:
    """Apply the single input-aware status/reason/proposal gate."""
    expected_projection = semantic_projection_from_claim(input_value.claim)
    if result.semantic_projection != expected_projection:
        raise CanonicalMappingContractError("authoritative_semantic_projection_mismatch")
    allowed_outcomes = _ALLOWED_OUTCOMES_BY_REASON.get(result.reason_code, set())
    if result.outcome not in allowed_outcomes:
        raise CanonicalMappingContractError("authoritative_mapping_outcome_reason_invalid")
    expected_status = _SEMANTIC_STATUS_BY_OUTCOME[result.outcome]
    if result.semantic_status != expected_status:
        raise CanonicalMappingContractError("authoritative_semantic_status_invalid")

    unsupported = _unsupported_semantic_reasons(expected_projection)
    if result.outcome == "mapped":
        if input_value.decision_kind == "schema_extension_candidate":
            raise CanonicalMappingContractError("schema_extension_decision_cannot_be_mapped")
        if input_value.surface_direction == "unknown":
            raise CanonicalMappingContractError("unknown_direction_cannot_be_mapped")
        if input_value.endpoint_type_binding.source_type_key is None:
            raise CanonicalMappingContractError("unknown_source_type")
        if input_value.endpoint_type_binding.target_type_key is None:
            raise CanonicalMappingContractError("unknown_target_type")
        if unsupported:
            raise CanonicalMappingContractError("claim_semantics_not_losslessly_mappable")
        if result.authorization_provenance is None:
            raise CanonicalMappingContractError("mapping_authorization_missing")
        proposal = CanonicalMappingProposalV1.model_validate(
            {
                "canonical_relation_key": result.canonical_relation_key,
                "canonical_direction": result.canonical_direction,
                "canonical_source_endpoint": result.canonical_source_endpoint.model_dump(mode="json"),
                "canonical_target_endpoint": result.canonical_target_endpoint.model_dump(mode="json"),
                "endpoint_transform": result.endpoint_transform,
                "predicate_transform": result.predicate_transform,
                "authorization_provenance": result.authorization_provenance.model_dump(mode="json"),
            }
        )
        _validate_proposal(input_value, proposal)
        return result

    if result.reason_code in {"unknown_direction", "unknown_source_type", "unknown_target_type", "unknown_predicate"}:
        if result.reason_code == "unknown_direction" and input_value.surface_direction != "unknown":
            raise CanonicalMappingContractError("unknown_direction_requires_unknown_surface")
        if result.reason_code == "unknown_source_type" and input_value.endpoint_type_binding.source_type_key is not None:
            raise CanonicalMappingContractError("unknown_source_type_requires_missing_type")
        if result.reason_code == "unknown_target_type" and input_value.endpoint_type_binding.target_type_key is not None:
            raise CanonicalMappingContractError("unknown_target_type_requires_missing_type")
        if result.reason_code == "unknown_predicate" and input_value.surface_raw_predicate in input_value.frozen_ontology.relation_type_keys:
            raise CanonicalMappingContractError("unknown_predicate_conflicts_with_ontology")
    if unsupported and result.reason_code not in unsupported:
        raise CanonicalMappingContractError("claim_semantic_reason_required")
    if result.reason_code in unsupported and result.semantic_status == "preserved":
        raise CanonicalMappingContractError("unsupported_semantics_cannot_be_preserved")
    return result


def _validate_result_against_authoritative_input(
    input_value: CanonicalMappingInputV1,
    result: CanonicalMappingV1,
    *,
    allow_repository_remap: bool = False,
) -> CanonicalMappingV1:
    """Validate a result against the complete authoritative input snapshot."""
    _require_authoritative_input(input_value)
    input_value = _rebuild_authoritative_input(input_value)
    result = CanonicalMappingV1.model_validate(
        json.loads(_canonical_json(result.model_dump(mode="json")))
    )
    if (
        result.remap_provenance is not None
        and result.remap_provenance.remap_generation > 0
        and not allow_repository_remap
    ):
        raise CanonicalMappingContractError(
            "positive remap requires repository predecessor authority"
        )

    exact_fields = (
        "mapping_schema_hash",
        "canonical_schema_hash",
        "raw_claim_authority_sha256",
        "library_id",
        "document_id",
        "document_revision_id",
        "revision_no",
        "job_id",
        "extraction_unit_id",
        "claim_id",
        "claim_snapshot",
        "claim_content_scoped_fingerprint",
        "extraction_occurrence_id",
        "extraction_occurrence_fingerprint",
        "surface_raw_predicate",
        "surface_direction",
        "endpoint_type_binding",
        "source_endpoint_resolution",
        "target_endpoint_resolution",
        "evidence_bindings",
        "source_evidence_ref_ids",
        "target_evidence_ref_ids",
        "mapping_evidence_ref_ids",
        "verified_evidence_identity_hashes",
        "semantic_projection",
        "frozen_ontology",
        "ontology_version_id",
        "ontology_snapshot_hash",
        "ontology_contract_version",
        "authorization_registry_snapshot",
        "authorization_provenance",
        "decision_binding",
        "decision_id",
        "decision_fingerprint",
        "decision_kind",
    )
    expected_values = {
        "mapping_schema_hash": input_value.mapping_schema_hash,
        "canonical_schema_hash": input_value.canonical_schema_hash,
        "raw_claim_authority_sha256": input_value.raw_claim_authority_sha256,
        "library_id": input_value.library_id,
        "document_id": input_value.document_id,
        "document_revision_id": input_value.document_revision_id,
        "revision_no": input_value.revision_no,
        "job_id": input_value.job_id,
        "extraction_unit_id": input_value.extraction_unit_id,
        "claim_id": input_value.claim_id,
        "claim_snapshot": input_value.claim_snapshot,
        "claim_content_scoped_fingerprint": input_value.claim.content_scoped_claim_fingerprint,
        "extraction_occurrence_id": input_value.extraction_occurrence_id,
        "extraction_occurrence_fingerprint": input_value.claim.extraction_occurrence_fingerprint,
        "surface_raw_predicate": input_value.surface_raw_predicate,
        "surface_direction": input_value.surface_direction,
        "endpoint_type_binding": input_value.endpoint_type_binding,
        "source_endpoint_resolution": input_value.source_endpoint_resolution,
        "target_endpoint_resolution": input_value.target_endpoint_resolution,
        "evidence_bindings": input_value.evidence_bindings,
        "source_evidence_ref_ids": (input_value.claim.source_mention.evidence_ref,),
        "target_evidence_ref_ids": (input_value.claim.target_mention.evidence_ref,),
        "mapping_evidence_ref_ids": input_value.verified_evidence_ref_ids or (),
        "verified_evidence_identity_hashes": tuple(
            sorted(binding.stable_evidence_identity_hash for binding in input_value.evidence_bindings)
        ),
        "semantic_projection": semantic_projection_from_claim(input_value.claim),
        "frozen_ontology": input_value.frozen_ontology,
        "ontology_version_id": input_value.ontology_version_id,
        "ontology_snapshot_hash": input_value.ontology_snapshot_hash,
        "ontology_contract_version": input_value.ontology_contract_version,
        "authorization_registry_snapshot": input_value.authorization_registry_snapshot,
        "authorization_provenance": input_value.authorization_provenance,
        "decision_binding": input_value.decision_binding,
        "decision_id": input_value.decision_id,
        "decision_fingerprint": input_value.decision_fingerprint,
        "decision_kind": input_value.decision_kind,
    }
    for field in exact_fields:
        if getattr(result, field) != expected_values[field]:
            raise CanonicalMappingContractError(f"authoritative_{field}_mismatch")

    _validate_result_semantics_against_authoritative_input(input_value, result)

    expected_attempt = _hash_json(
        _attempt_payload_from_input(input_value, result.provenance, result.mapping_version)
    )
    if result.mapping_attempt_fingerprint != expected_attempt:
        raise CanonicalMappingContractError("authoritative_mapping_attempt_mismatch")
    expected_attempt_id = uuid5(
        CANONICAL_MAPPING_UUID_NAMESPACE,
        f"canonical_mapping_attempt_v1:{expected_attempt}",
    )
    if result.mapping_attempt_id != expected_attempt_id:
        raise CanonicalMappingContractError("authoritative_mapping_attempt_id_mismatch")
    expected_result = _hash_json(_result_payload(result, attempt_fingerprint=expected_attempt))
    if result.mapping_result_fingerprint != expected_result:
        raise CanonicalMappingContractError("authoritative_mapping_result_mismatch")
    expected_result_id = deterministic_mapping_result_id(expected_result)
    if result.mapping_result_id != expected_result_id:
        raise CanonicalMappingContractError("authoritative_mapping_result_id_mismatch")
    return result


def canonical_mapping_json(
    authoritative_input: CanonicalMappingInputV1,
    value: CanonicalMappingV1 | Mapping[str, Any],
) -> str:
    """Serialize only a result proven against its complete authoritative input."""
    _require_authoritative_input(authoritative_input)
    payload = value.model_dump(mode="json") if isinstance(value, CanonicalMappingV1) else value
    mapping = CanonicalMappingV1.model_validate(json.loads(_canonical_json(payload)))
    validated = _validate_result_against_authoritative_input(authoritative_input, mapping)
    return _canonical_json(_canonical_mapping_datetime_payload(validated.model_dump(mode="json")))


def repository_authorized_canonical_mapping_json(
    authoritative_input: CanonicalMappingInputV1,
    value: CanonicalMappingV1,
    *,
    predecessor: CanonicalMappingV1 | None = None,
) -> str:
    """Serialize a result after repository-owned predecessor validation.

    This boundary is intentionally separate from ``canonical_mapping_json``.
    It accepts only a typed result and a typed predecessor projection; the
    persistence repository must rebuild both projections from immutable rows
    and validate their complete scope before calling it.
    """
    if not isinstance(value, CanonicalMappingV1):
        raise TypeError("repository remap serialization requires CanonicalMappingV1")
    validated = _validate_result_against_authoritative_input(
        authoritative_input,
        value,
        allow_repository_remap=True,
    )
    generation = validated.remap_provenance.remap_generation if validated.remap_provenance else 0
    if generation == 0:
        if predecessor is not None:
            raise CanonicalMappingContractError("generation_zero_cannot_supersede_predecessor")
    else:
        if predecessor is None:
            raise CanonicalMappingContractError("repository predecessor projection is required")
        _validate_repository_remap_predecessor(validated, predecessor)
    return _canonical_json(_canonical_mapping_datetime_payload(validated.model_dump(mode="json")))


def canonical_mapping_attempt_fingerprint(
    input_value: CanonicalMappingInputV1,
    *,
    provenance: MapperProvenanceV1,
    mapping_version: int = 1,
) -> str:
    input_value = _rebuild_authoritative_input(input_value)
    if not isinstance(provenance, MapperProvenanceV1):
        raise TypeError("provenance must be a validated MapperProvenanceV1")
    provenance = _revalidate_nested(provenance, MapperProvenanceV1, field="provenance")
    if isinstance(mapping_version, bool) or not isinstance(mapping_version, int) or mapping_version < 1:
        raise CanonicalMappingContractError("mapping_version_invalid")
    return _hash_json(_attempt_payload_from_input(input_value, provenance, mapping_version))


def canonical_mapping_result_fingerprint(
    authoritative_input: CanonicalMappingInputV1,
    value: CanonicalMappingV1,
) -> str:
    if not isinstance(value, CanonicalMappingV1):
        raise TypeError("value must be a validated CanonicalMappingV1")
    validated = CanonicalMappingV1.model_validate(
        json.loads(canonical_mapping_json(authoritative_input, value))
    )
    return validated.mapping_result_fingerprint or ""
