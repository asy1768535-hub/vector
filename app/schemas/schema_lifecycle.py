from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.graph_canonical import canonical_graph_json_v1


_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,127}$")
_IDEMPOTENCY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_VALUE_TYPES = {
    "string",
    "text",
    "integer",
    "number",
    "boolean",
    "date",
    "datetime",
    "enum",
    "json",
}


class StrictSchemaLifecycleModel(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


def _trim(value: str) -> str:
    value = " ".join(value.split())
    if not value:
        raise ValueError("value must not be blank")
    return value


def _key(value: str) -> str:
    value = value.strip()
    if _KEY_RE.fullmatch(value) is None:
        raise ValueError("key must use lowercase letters, numbers, and underscores")
    return value


def _bounded_json_object(value: dict[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    if len(value) > 100:
        raise ValueError("JSON object has too many keys")
    try:
        encoded = canonical_graph_json_v1(value).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("JSON object is invalid") from exc
    if len(encoded) > 65_536:
        raise ValueError("JSON object is too large")
    return value


class SchemaCommandRequest(StrictSchemaLifecycleModel):
    expected_version_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        value = value.strip()
        if _IDEMPOTENCY_RE.fullmatch(value) is None:
            raise ValueError("idempotency_key is invalid")
        return value


class SchemaCloneRequest(SchemaCommandRequest):
    description: str | None = Field(default=None, max_length=2_000)

    _description = field_validator("description")(
        lambda value: _trim(value) if value is not None else None
    )


class SchemaImportEntityType(StrictSchemaLifecycleModel):
    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    properties_schema: dict[str, Any] | None = None

    _key = field_validator("key")(_key)
    _label = field_validator("label")(_trim)
    _description = field_validator("description")(
        lambda value: _trim(value) if value is not None else None
    )
    _properties = field_validator("properties_schema")(_bounded_json_object)


class SchemaImportRelationType(SchemaImportEntityType):
    direction: Literal["directed", "undirected"]
    requires_evidence: bool = True
    default_review_policy: Literal[
        "auto_active", "pending_review", "manual_only"
    ]


class SchemaImportAttribute(StrictSchemaLifecycleModel):
    owner_kind: Literal["entity_type", "relation_type"]
    owner_key: str = Field(min_length=1, max_length=128)
    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    value_type: Literal[
        "string",
        "text",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
        "enum",
        "json",
    ]
    required: bool = False
    enum_values: list[str | int | float | bool] | None = Field(
        default=None, max_length=100
    )
    validation_schema: dict[str, Any] | None = None
    indexed: bool = False

    _owner_key = field_validator("owner_key")(_key)
    _key = field_validator("key")(_key)
    _label = field_validator("label")(_trim)
    _validation = field_validator("validation_schema")(_bounded_json_object)

    @model_validator(mode="after")
    def validate_enum(self):
        if self.value_type == "enum" and not self.enum_values:
            raise ValueError("enum attributes require enum_values")
        if self.value_type != "enum" and self.enum_values is not None:
            raise ValueError("enum_values are allowed only for enum attributes")
        return self


class SchemaImportConstraint(StrictSchemaLifecycleModel):
    relation_type_key: str = Field(min_length=1, max_length=128)
    source_entity_type_key: str = Field(min_length=1, max_length=128)
    target_entity_type_key: str = Field(min_length=1, max_length=128)
    cardinality: Literal[
        "one_to_one", "one_to_many", "many_to_one", "many_to_many"
    ] | None = None
    requires_review: bool = False

    _relation_key = field_validator("relation_type_key")(_key)
    _source_key = field_validator("source_entity_type_key")(_key)
    _target_key = field_validator("target_entity_type_key")(_key)


class SchemaImportRequest(StrictSchemaLifecycleModel):
    version_key: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=2_000)
    entity_types: list[SchemaImportEntityType] = Field(default_factory=list, max_length=500)
    relation_types: list[SchemaImportRelationType] = Field(default_factory=list, max_length=500)
    attributes: list[SchemaImportAttribute] = Field(default_factory=list, max_length=1_000)
    constraints: list[SchemaImportConstraint] = Field(default_factory=list, max_length=1_000)
    idempotency_key: str = Field(min_length=1, max_length=128)

    _version_key = field_validator("version_key")(_key)
    _description = field_validator("description")(
        lambda value: _trim(value) if value is not None else None
    )

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        value = value.strip()
        if _IDEMPOTENCY_RE.fullmatch(value) is None:
            raise ValueError("idempotency_key is invalid")
        return value

    @model_validator(mode="after")
    def validate_payload_size(self):
        try:
            encoded = canonical_graph_json_v1(
                self.model_dump(mode="json", exclude={"idempotency_key"})
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("Schema import payload is invalid") from exc
        if len(encoded) > 65_536:
            raise ValueError("Schema import payload is too large")
        return self


class SchemaImportFileRequest(StrictSchemaLifecycleModel):
    file_name: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=65_536)
    idempotency_key: str = Field(min_length=1, max_length=128)

    @field_validator("file_name")
    @classmethod
    def validate_file_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("file_name must not be blank")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def validate_idempotency_key(cls, value: str) -> str:
        value = value.strip()
        if _IDEMPOTENCY_RE.fullmatch(value) is None:
            raise ValueError("idempotency_key is invalid")
        return value


class SchemaActivationRequest(SchemaCommandRequest):
    expected_active_version_id: uuid.UUID | None
    confirmation: Literal["activate_schema_version"]


class SchemaDraftDeleteRequest(SchemaCommandRequest):
    confirmation: Literal["delete_schema_draft"]


class SchemaVersionDisableRequest(SchemaCommandRequest):
    confirmation: Literal["disable_schema_version"]


class SchemaEntityTypeCreateRequest(SchemaCommandRequest):
    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    properties_schema: dict[str, Any] | None = None

    _key = field_validator("key")(_key)
    _label = field_validator("label")(_trim)
    _description = field_validator("description")(
        lambda value: _trim(value) if value is not None else None
    )
    _properties = field_validator("properties_schema")(_bounded_json_object)


class SchemaEntityTypeUpdateRequest(SchemaCommandRequest):
    label: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2_000)
    properties_schema: dict[str, Any] | None = None

    _label = field_validator("label")(
        lambda value: _trim(value) if value is not None else None
    )
    _description = field_validator("description")(
        lambda value: _trim(value) if value is not None else None
    )
    _properties = field_validator("properties_schema")(_bounded_json_object)

    @model_validator(mode="after")
    def require_change(self):
        if not {"label", "description", "properties_schema"}.intersection(
            self.model_fields_set
        ):
            raise ValueError("at least one Entity Type field is required")
        return self


class SchemaRelationTypeCreateRequest(SchemaEntityTypeCreateRequest):
    direction: Literal["directed", "undirected"]
    requires_evidence: bool = True
    default_review_policy: Literal[
        "auto_active", "pending_review", "manual_only"
    ]


class SchemaRelationTypeUpdateRequest(SchemaEntityTypeUpdateRequest):
    direction: Literal["directed", "undirected"] | None = None
    requires_evidence: bool | None = None
    default_review_policy: Literal[
        "auto_active", "pending_review", "manual_only"
    ] | None = None

    @model_validator(mode="after")
    def require_relation_change(self):
        fields = {
            "label",
            "description",
            "properties_schema",
            "direction",
            "requires_evidence",
            "default_review_policy",
        }
        if not fields.intersection(self.model_fields_set):
            raise ValueError("at least one Relation Type field is required")
        return self


class SchemaAttributeCreateRequest(SchemaCommandRequest):
    owner_kind: Literal["entity_type", "relation_type"]
    owner_type_id: uuid.UUID
    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    value_type: Literal[
        "string",
        "text",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
        "enum",
        "json",
    ]
    required: bool = False
    enum_values: list[str | int | float | bool] | None = Field(
        default=None, max_length=100
    )
    validation_schema: dict[str, Any] | None = None
    indexed: bool = False

    _key = field_validator("key")(_key)
    _label = field_validator("label")(_trim)
    _validation = field_validator("validation_schema")(_bounded_json_object)

    @model_validator(mode="after")
    def validate_enum(self):
        if self.value_type == "enum" and not self.enum_values:
            raise ValueError("enum attributes require enum_values")
        if self.value_type != "enum" and self.enum_values is not None:
            raise ValueError("enum_values are allowed only for enum attributes")
        return self


class SchemaAttributeUpdateRequest(SchemaCommandRequest):
    label: str | None = Field(default=None, min_length=1, max_length=255)
    value_type: Literal[
        "string",
        "text",
        "integer",
        "number",
        "boolean",
        "date",
        "datetime",
        "enum",
        "json",
    ] | None = None
    required: bool | None = None
    enum_values: list[str | int | float | bool] | None = Field(
        default=None, max_length=100
    )
    validation_schema: dict[str, Any] | None = None
    indexed: bool | None = None

    _label = field_validator("label")(
        lambda value: _trim(value) if value is not None else None
    )
    _validation = field_validator("validation_schema")(_bounded_json_object)

    @model_validator(mode="after")
    def validate_change(self):
        fields = {
            "label",
            "value_type",
            "required",
            "enum_values",
            "validation_schema",
            "indexed",
        }
        if not fields.intersection(self.model_fields_set):
            raise ValueError("at least one Attribute field is required")
        effective_type = self.value_type
        if effective_type is not None and effective_type not in _VALUE_TYPES:
            raise ValueError("attribute value_type is invalid")
        if effective_type not in {None, "enum"} and self.enum_values is not None:
            raise ValueError("enum_values are allowed only for enum attributes")
        if effective_type == "enum" and not self.enum_values:
            raise ValueError("enum attributes require enum_values")
        return self


class SchemaConstraintCreateRequest(SchemaCommandRequest):
    relation_type_id: uuid.UUID
    source_entity_type_id: uuid.UUID
    target_entity_type_id: uuid.UUID
    cardinality: Literal[
        "one_to_one", "one_to_many", "many_to_one", "many_to_many"
    ] | None = None
    requires_review: bool = False


class SchemaConstraintUpdateRequest(SchemaCommandRequest):
    cardinality: Literal[
        "one_to_one", "one_to_many", "many_to_one", "many_to_many"
    ] | None = None
    requires_review: bool | None = None

    @model_validator(mode="after")
    def require_change(self):
        if not {"cardinality", "requires_review"}.intersection(self.model_fields_set):
            raise ValueError("at least one Constraint field is required")
        return self


class SchemaItemDisableRequest(SchemaCommandRequest):
    pass


class SchemaEntityTypeRead(StrictSchemaLifecycleModel):
    id: uuid.UUID
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    key: str
    label: str
    description: str | None
    properties_schema: dict[str, Any] | None
    is_seeded: bool
    status: str
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SchemaRelationTypeRead(SchemaEntityTypeRead):
    direction: str
    requires_evidence: bool
    default_review_policy: str


class SchemaAttributeRead(StrictSchemaLifecycleModel):
    id: uuid.UUID
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    owner_kind: str
    owner_type_id: uuid.UUID
    key: str
    label: str
    value_type: str
    required: bool
    enum_values: list[Any] | None
    validation_schema: dict[str, Any] | None
    indexed: bool
    status: str
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SchemaConstraintRead(StrictSchemaLifecycleModel):
    id: uuid.UUID
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    relation_type_id: uuid.UUID
    relation_type_key: str
    source_entity_type_id: uuid.UUID
    source_entity_type_key: str
    target_entity_type_id: uuid.UUID
    target_entity_type_key: str
    cardinality: str | None
    requires_review: bool
    status: str
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class SchemaVersionSummaryRead(StrictSchemaLifecycleModel):
    id: uuid.UUID
    library_id: uuid.UUID
    library_slug: str
    version_key: str
    version_no: int
    status: str
    description: str | None
    origin: str
    confirmed: bool
    parent_version_id: uuid.UUID | None
    published_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entity_type_count: int = Field(ge=0)
    relation_type_count: int = Field(ge=0)
    attribute_count: int = Field(ge=0)
    constraint_count: int = Field(ge=0)


class SchemaVersionDetailRead(SchemaVersionSummaryRead):
    entity_types: list[SchemaEntityTypeRead] = Field(max_length=500)
    relation_types: list[SchemaRelationTypeRead] = Field(max_length=500)
    attributes: list[SchemaAttributeRead] = Field(max_length=1_000)
    constraints: list[SchemaConstraintRead] = Field(max_length=1_000)


class SchemaVersionListRead(StrictSchemaLifecycleModel):
    library_id: uuid.UUID
    library_slug: str
    versions: list[SchemaVersionSummaryRead] = Field(max_length=100)


class SchemaValidationIssueRead(StrictSchemaLifecycleModel):
    code: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    item_kind: Literal[
        "ontology_version",
        "entity_type",
        "relation_type",
        "attribute",
        "constraint",
    ]
    item_id: uuid.UUID | None = None
    field: str | None = Field(default=None, max_length=64)


class SchemaValidationRead(StrictSchemaLifecycleModel):
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    version_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    valid: bool
    issues: list[SchemaValidationIssueRead] = Field(max_length=100)


class SchemaDiffGroupRead(StrictSchemaLifecycleModel):
    added: list[str] = Field(max_length=500)
    removed: list[str] = Field(max_length=500)
    changed: list[str] = Field(max_length=500)


class SchemaReferenceCountsRead(StrictSchemaLifecycleModel):
    source_version_count: int = Field(ge=0)
    draft_version_count: int = Field(ge=0)
    source_current_ids: list[uuid.UUID] = Field(max_length=20)
    draft_current_ids: list[uuid.UUID] = Field(max_length=20)


class SchemaCompatibilityPreviewRead(StrictSchemaLifecycleModel):
    library_id: uuid.UUID
    library_slug: str
    result: Literal["match", "mismatch", "unavailable"]


class SchemaImpactRead(StrictSchemaLifecycleModel):
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    parent_version_id: uuid.UUID | None
    version_state_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    entity_types: SchemaDiffGroupRead
    relation_types: SchemaDiffGroupRead
    attributes: SchemaDiffGroupRead
    constraints: SchemaDiffGroupRead
    extraction_jobs: SchemaReferenceCountsRead
    publications: SchemaReferenceCountsRead
    compatibility: list[SchemaCompatibilityPreviewRead] = Field(max_length=20)
    historical_rows_migrated: Literal[False]
    retrieval_scope_changed: Literal[False]


class SchemaCommandResultRead(StrictSchemaLifecycleModel):
    action_id: uuid.UUID
    reused: bool
    version: SchemaVersionDetailRead


class SchemaVersionDeletionResultRead(StrictSchemaLifecycleModel):
    action_id: uuid.UUID
    reused: bool
    library_id: uuid.UUID
    ontology_version_id: uuid.UUID
    status: Literal["deleted"]
