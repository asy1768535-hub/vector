"""Corpus-level discovery of a business Schema for a new graph Library.

The JSON contract in this module is deliberately separate from the business
Schema.  The protocol tells the model how to serialize its answer; it does not
enumerate which entity or relation names are legal.
"""

from __future__ import annotations

import json
import asyncio
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.models.attribute_definition import ATTRIBUTE_OWNER_ENTITY_TYPE, ATTRIBUTE_OWNER_RELATION_TYPE
from app.models.ontology_version import OntologyVersion
from app.config import settings
from app.services import ontology
from app.services.graph_extraction_provider import GraphExtractionProviderError
from app.services.token_budget import (
    estimate_chat_request_tokens,
    estimate_serialized_tokens,
    estimate_text_tokens,
    split_text_to_token_budget,
)


log = logging.getLogger(__name__)


class SchemaDiscoveryAttribute(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=128)
    description: str = Field(min_length=1, max_length=512)
    value_type: str = Field(default="string", min_length=1, max_length=32)
    required: bool = False


class SchemaDiscoveryEntityType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=32)
    attributes: list[SchemaDiscoveryAttribute] = Field(default_factory=list)


class SchemaDiscoveryRelationType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=1, max_length=128)
    label: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1, max_length=1000)
    aliases: list[str] = Field(default_factory=list, max_length=32)
    direction: Literal["directed", "undirected"] = "directed"
    attributes: list[SchemaDiscoveryAttribute] = Field(default_factory=list)


class SchemaDiscoveryConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_type_key: str = Field(min_length=1, max_length=128)
    relation_type_key: str = Field(min_length=1, max_length=128)
    target_type_key: str = Field(min_length=1, max_length=128)
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"] | None = (
        "many_to_many"
    )


class SchemaDiscoveryPayload(BaseModel):
    """Protocol Schema: the only shape the discovery model must return."""

    model_config = ConfigDict(extra="forbid")

    entity_types: list[SchemaDiscoveryEntityType] = Field(default_factory=list)
    relation_types: list[SchemaDiscoveryRelationType] = Field(default_factory=list)
    constraints: list[SchemaDiscoveryConstraint] = Field(default_factory=list)


class ConceptInventoryRelationHint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=255)
    source_hint: str = Field(min_length=1, max_length=255)
    target_hint: str = Field(min_length=1, max_length=255)
    direction_hint: str = Field(min_length=1, max_length=64)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)


class ConceptInventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1, max_length=255)
    proposed_entity_type: str = Field(min_length=1, max_length=128)
    aliases: list[str] = Field(default_factory=list, max_length=32)
    identifiers: list[str] = Field(default_factory=list, max_length=32)
    proposed_relations: list[ConceptInventoryRelationHint] = Field(
        default_factory=list, max_length=32
    )
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    confidence: float = Field(ge=0, le=1)
    ambiguity: list[str] = Field(default_factory=list, max_length=16)


class ConceptInventoryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    concepts: list[ConceptInventoryItem] = Field(default_factory=list, max_length=512)


@dataclass(frozen=True, slots=True)
class DiscoveryText:
    key: str
    text: str


@dataclass(frozen=True, slots=True)
class SchemaDiscoveryBatch:
    key: str
    texts: tuple[DiscoveryText, ...]
    estimated_input_tokens: int
    estimated_total_tokens: int


@dataclass(frozen=True, slots=True)
class BusinessSchemaDraft:
    """Business Schema proposal; ``confirmed`` is intentionally explicit."""

    entity_types: tuple[SchemaDiscoveryEntityType, ...]
    relation_types: tuple[SchemaDiscoveryRelationType, ...]
    constraints: tuple[SchemaDiscoveryConstraint, ...]
    source_hash: str
    concept_inventory: tuple[ConceptInventoryItem, ...] = ()
    trace: dict[str, Any] = field(default_factory=dict)
    status: Literal["ai_draft"] = "ai_draft"
    confirmed: bool = False

    def as_protocol_payload(self) -> dict[str, Any]:
        return SchemaDiscoveryPayload(
            entity_types=list(self.entity_types),
            relation_types=list(self.relation_types),
            constraints=list(self.constraints),
        ).model_dump(mode="json")


class SchemaDiscoveryProvider(Protocol):
    async def extract(self, messages: list[dict[str, str]]): ...


_DISCOVERY_SYSTEM_PROMPT = """You discover a business Schema from untrusted document excerpts.
Return only JSON matching the requested protocol shape. Invent no facts not supported
by the excerpts. Propose domain-specific entity types, relation types, directions,
source_type -> relation_type -> target_type constraints, and only attributes that
are repeatedly useful or explicitly required. There is no fixed allowlist of names.
Use stable lowercase snake_case identifiers for every key. Write all user-facing
labels and descriptions in Simplified Chinese. Use the exact same key spelling in
definitions and constraints.
Use the most specific stable concept supported by the corpus: do not collapse
distinct concepts into an umbrella type when they have different relation roles,
attributes, lifecycle behavior, or textual definitions. Keep an umbrella type only
when the excerpts do not support a defensible distinction. Give each type a
description that explains the evidence-based distinction from neighboring types.
Do not optimize for the fewest types. It is a Schema defect when named concepts
with distinct evidence-backed roles are placed in one broad type merely to reduce
the Schema size.
When a named referent appears with a contextual qualifier in one excerpt and
without it in another, first determine from the evidence whether both forms name
the same referent. If they do, keep one identity and treat the observed form as an
evidence-backed alias; do not create a second type solely for a wording qualifier.
Different relation mentions alone do not prove that the referents are different.
Keep separate types only when the excerpts support distinct objects or distinct
concept definitions, attributes, lifecycle, or roles.
Choose relation direction from the semantic roles stated in the excerpts. Do not
create an inverse relation merely to make a constraint fit, and do not use a vague
association when the excerpts support a more specific relation. If two relation
types would have the same meaning and endpoint behavior, keep one stable relation
type unless the corpus clearly distinguishes them.
Derive each relation key from the evidence-backed predicate meaning. Keep a stable
predicate across synthesis, protocol repair, and semantic refinement unless the
excerpts provide a real semantic distinction. Do not rename a predicate merely to
make a constraint legal, and do not invent endpoint-specific compounds when the
source expresses the same relation.
For every constraint, verify that the source type can represent the grammatical
subject performing or owning the relation and the target type can represent its
object; revise the constraint when the excerpts contradict that assignment.
Check the relation key's voice against that assignment: an active verb puts its
agent in source, while a passive or ``*_by`` form puts the affected object in
source and the agent in target. Do not pair a predicate name and endpoint order
that express opposite directions.
Every source_type_key and target_type_key must reference a key already defined in
entity_types. Every relation_type_key must reference a key already defined in
relation_types. Never use undeclared endpoint types such as string_literal, text, or
string. If a place, identifier, approval, contract, or similar concept must participate
in a relation, define a corresponding entity type. Ordinary attribute values must not
be modeled as relation constraints. direction must be directed or undirected.
cardinality must be one_to_one, one_to_many, many_to_one, or many_to_many.
Do not return entities, relations, database IDs, or extraction facts."""


_CONCEPT_INVENTORY_SYSTEM_PROMPT = """You inventory concepts from untrusted document excerpts before Schema design.
This is the concept inventory protocol used as input to Schema synthesis.
Return one compact JSON object and nothing else: no Markdown, prose, code fences,
or comments. The top-level object must contain `concepts`, an array. Every array
item must contain exactly these protocol fields: `label` (string),
`proposed_entity_type` (string), `aliases` (array of strings), `identifiers`
(array of strings), `proposed_relations` (array of objects), `evidence_refs`
(array of excerpt-key strings), `confidence` (JSON number from 0 to 1), and
`ambiguity` (array of strings). Every proposed relation object must contain
`label`, `source_hint`, `target_hint`, `direction_hint`, and `evidence_refs`;
never encode a relation hint as a bare string. Record only concepts, identifiers,
relation hints, and evidence references supported by the excerpts. Do not
materialize entities or relations and do not infer facts outside the source text.
Keep proposed types descriptive and domain-neutral; do not use a fixed vocabulary
or a domain allowlist. Use the excerpt keys as evidence_refs. Emit each concept
once across the excerpts, merging all evidence_refs, aliases, identifiers, and
relation-hint evidence for that concept. Keep aliases and identifiers only when
the excerpts provide them. Do not repeat a full relation hint under every related
concept when one evidence-backed hint is sufficient.
"""


class _RepairableSchemaValidationError(ValueError):
    def __init__(self, errors: Iterable[str]) -> None:
        self.errors = tuple(errors)
        super().__init__("; ".join(self.errors))


def _schema_output_budget(
    *,
    stage: str,
    context_window_tokens: int,
    configured_output_tokens: int | None,
    input_tokens: int,
    expected_output_tokens: int,
    previous_schema_tokens: int | None = None,
) -> int | None:
    safety_tokens = 256
    available = context_window_tokens - input_tokens - safety_tokens
    if available < max(128, expected_output_tokens):
        details = (
            f"input_tokens={input_tokens} "
            f"expected_output_tokens={expected_output_tokens} "
            f"context_window_tokens={context_window_tokens}"
        )
        if previous_schema_tokens is not None:
            details += f" previous_schema_tokens={previous_schema_tokens}"
        raise ValueError(f"schema discovery {stage} budgets exceed model context window ({details})")
    return None if configured_output_tokens is None else min(configured_output_tokens, available)


def estimate_schema_tokens(value: Any) -> int:
    """Conservative token estimate for local models without a tokenizer."""

    return estimate_serialized_tokens(value, model_name="Qwen")


def _split_text(item: DiscoveryText, max_tokens: int) -> list[DiscoveryText]:
    if estimate_schema_tokens({"text": item.text}) <= max_tokens:
        return [item]
    parts = split_text_to_token_budget(item.text, max(1, max_tokens - estimate_text_tokens(item.key) - 32), model_name="Qwen")
    return [
        DiscoveryText(f"{item.key}-{index + 1}", part)
        for index, part in enumerate(parts)
    ]


def plan_schema_discovery_batches(
    texts: Iterable[DiscoveryText],
    *,
    context_window_tokens: int = 4096,
    max_output_tokens: int | None = None,
    prompt_tokens: int = 220,
    schema_tokens: int = 280,
    response_format_tokens: int = 96,
    wrapper_tokens: int = 32,
) -> tuple[SchemaDiscoveryBatch, ...]:
    """Pack by serialized-token estimates, including protocol and chat overhead."""

    configured_output_tokens = max_output_tokens or min(8192, context_window_tokens // 2)
    if context_window_tokens < 512 or configured_output_tokens < 128:
        raise ValueError("discovery context and output budgets are too small")
    input_budget = (
        context_window_tokens
        - configured_output_tokens
        - prompt_tokens
        - schema_tokens
        - response_format_tokens
        - wrapper_tokens
    )
    if input_budget < 128:
        raise ValueError("discovery budgets leave no usable input window")
    expanded = [part for item in texts for part in _split_text(item, input_budget)]
    batches: list[SchemaDiscoveryBatch] = []
    current: list[DiscoveryText] = []
    current_tokens = 0
    for item in expanded:
        item_tokens = estimate_schema_tokens({"key": item.key, "text": item.text})
        if current and current_tokens + item_tokens > input_budget:
            batches.append(
                _make_batch(
                    len(batches),
                    current,
                    current_tokens,
                    prompt_tokens,
                    schema_tokens,
                    configured_output_tokens,
                )
            )
            current, current_tokens = [], 0
        current.append(item)
        current_tokens += item_tokens
    if current:
        batches.append(
            _make_batch(
                len(batches),
                current,
                current_tokens,
                prompt_tokens,
                schema_tokens,
                configured_output_tokens,
            )
        )
    return tuple(batches)


def _make_batch(index: int, texts: list[DiscoveryText], input_tokens: int, prompt_tokens: int, schema_tokens: int, max_output_tokens: int):
    return SchemaDiscoveryBatch(
        key=f"discovery-{index + 1}",
        texts=tuple(texts),
        estimated_input_tokens=input_tokens,
        estimated_total_tokens=input_tokens + prompt_tokens + schema_tokens + max_output_tokens,
    )


def build_concept_inventory_messages(batch: SchemaDiscoveryBatch) -> list[dict[str, str]]:
    payload = {
        "response_format": {"type": "json_object"},
        "protocol_schema": {
            "concepts": "array of objects; each object has the exact fields below",
            "concept": {
                "label": "string",
                "proposed_entity_type": "string",
                "aliases": "array of strings",
                "identifiers": "array of strings",
                "proposed_relations": "array of relation objects, never strings",
                "evidence_refs": "array of excerpt-key strings",
                "confidence": "JSON number between 0 and 1",
                "ambiguity": "array of strings",
            },
            "relation_hint": {
                "label": "string",
                "source_hint": "string",
                "target_hint": "string",
                "direction_hint": "string",
                "evidence_refs": "array of excerpt-key strings",
            },
        },
        "excerpts": [{"key": item.key, "text": item.text} for item in batch.texts],
    }
    return [
        {"role": "system", "content": _CONCEPT_INVENTORY_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_concept_inventory_repair_messages(
    batch: SchemaDiscoveryBatch,
    validation_error: str,
) -> list[dict[str, str]]:
    messages = build_concept_inventory_messages(batch)
    messages[0] = {
        "role": "system",
        "content": (
            messages[0]["content"]
            + "\nThe previous inventory response failed strict protocol validation. "
            "Return one complete corrected inventory JSON object, not a patch. "
            "Use only the exact fields listed in the protocol; confidence must be a "
            "JSON number from 0 to 1 and ambiguity must be an array of strings."
        ),
    }
    payload = json.loads(messages[1]["content"])
    payload["repair_instruction"] = (
        "Correct the previous protocol failure and return the complete concepts "
        "array. Do not add explanation, Markdown, or extra fields."
    )
    payload["validation_error"] = validation_error[:500]
    messages[1] = {
        "role": "user",
        "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    }
    return messages


def build_schema_discovery_messages(
    batch: SchemaDiscoveryBatch,
    *,
    concept_inventory: Iterable[ConceptInventoryItem] = (),
) -> list[dict[str, str]]:
    payload = {
            "response_format": {"type": "json_object"},
            "protocol_schema": {
            "entity_types": "array of {key,label,description,attributes}",
            "relation_types": "array of {key,label,description,direction,attributes}",
            "constraints": "array of {source_type_key,relation_type_key,target_type_key,cardinality}",
        },
            "concept_inventory": [item.model_dump(mode="json") for item in concept_inventory],
            "excerpts": [{"key": item.key, "text": item.text} for item in batch.texts],
    }
    return [
        {"role": "system", "content": _DISCOVERY_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_schema_discovery_repair_messages(
    previous_schemas: list[Any], validation_errors: Iterable[str]
) -> list[dict[str, str]]:
    previous_schema: Any = (
        previous_schemas[0]
        if len(previous_schemas) == 1
        else {"batch_outputs": previous_schemas}
    )
    payload = {
        "response_format": {"type": "json_object"},
        "repair_instruction": (
            "Return one complete corrected Schema using the original protocol. "
            "Return the full entity_types, relation_types, and constraints arrays; "
            "do not return a patch and do not silently delete invalid constraints. "
            "This is protocol repair: preserve evidence-backed relation keys, "
            "direction, and endpoint meaning unless the validation error directly "
            "requires a change."
        ),
        "validation_errors": list(validation_errors),
        "previous_schema": previous_schema,
    }
    return [
        {
            "role": "system",
            "content": _DISCOVERY_SYSTEM_PROMPT
            + "\nRepair the previous Schema without inventing a fixed business vocabulary.",
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def build_schema_discovery_refinement_messages(
    texts: Iterable[DiscoveryText], previous_schema: dict[str, Any]
) -> list[dict[str, str]]:
    payload = {
        "response_format": {"type": "json_object"},
        "refinement_instruction": (
            "Return one complete replacement Schema. Split evidence-backed umbrella "
            "types when their roles, attributes, or relation behavior differ. Check "
            "predicate voice and endpoint order. Preserve supported concepts and "
            "return full entity_types, relation_types, and constraints arrays; never "
            "return a patch."
        ),
        "previous_schema": previous_schema,
        "excerpts": [{"key": item.key, "text": item.text} for item in texts],
    }
    return [
        {
            "role": "system",
            "content": (
                "Refine a domain-neutral Schema using the supplied untrusted excerpts. "
                "Return only the protocol JSON. Use lowercase snake_case keys and "
                "Simplified Chinese labels and descriptions. Keep only evidence-backed "
                "types and relations, split distinct concepts, "
                "and make constraint endpoints and predicate voice agree. Preserve "
                "existing evidence-backed relation keys and direction unless the "
                "excerpts clearly support a different meaning; do not rename a "
                "relation just to satisfy a constraint. This is "
                "semantic refinement, not protocol error repair."
            ),
        },
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False, separators=(",", ":"))},
    ]


def _compact_refinement_schema(value: dict[str, Any]) -> dict[str, Any]:
    """Keep role evidence while removing protocol noise from a refinement request."""

    def attributes(rows: Any) -> list[dict[str, Any]]:
        if not isinstance(rows, list):
            return []
        return [
            {
                "key": item.get("key"),
                "description": item.get("description"),
                "value_type": item.get("value_type", "string"),
                "required": bool(item.get("required", False)),
            }
            for item in rows
            if isinstance(item, dict) and isinstance(item.get("key"), str)
        ]

    return {
        "entity_types": [
            {
                "key": row.get("key"),
                "label": row.get("label"),
                "description": row.get("description"),
                "attributes": attributes(row.get("attributes")),
            }
            for row in value.get("entity_types", [])
            if isinstance(row, dict)
        ],
        "relation_types": [
            {
                "key": row.get("key"),
                "label": row.get("label"),
                "description": row.get("description"),
                "direction": row.get("direction", "directed"),
                "attributes": attributes(row.get("attributes")),
            }
            for row in value.get("relation_types", [])
            if isinstance(row, dict)
        ],
        "constraints": [
            row
            for row in value.get("constraints", [])
            if isinstance(row, dict)
        ],
    }


def _normalize_attribute(value: dict[str, Any]) -> dict[str, Any]:
    key = value.get("key", value.get("name", value.get("label", "attribute")))
    description = value.get("description") or f"Attribute observed in the source text: {key}"
    value_type = value.get("value_type", value.get("type", "string"))
    return {
        "key": str(key),
        "description": str(description),
        "value_type": str(value_type),
        "required": bool(value.get("required", False)),
    }


def _normalize_protocol_value(value: Any) -> Any:
    """Normalize common JSON spellings without changing the business vocabulary.

    The protocol remains the only accepted application shape after this boundary.
    This small compatibility layer handles local models that serialize an attribute
    as a name or use equivalent direction/cardinality spellings.
    """

    if not isinstance(value, dict):
        return value
    normalized = dict(value)
    for collection_name in ("entity_types", "relation_types"):
        rows = normalized.get(collection_name)
        if not isinstance(rows, list):
            continue
        converted_rows = []
        for row in rows:
            if not isinstance(row, dict):
                converted_rows.append(row)
                continue
            converted = dict(row)
            attributes = converted.get("attributes")
            if isinstance(attributes, list):
                converted["attributes"] = [
                    {
                        "key": attribute,
                        "description": f"Attribute observed in the source text: {attribute}",
                    }
                    if isinstance(attribute, str)
                    else _normalize_attribute(attribute)
                    if isinstance(attribute, (str, dict))
                    else attribute
                    for attribute in attributes
                ]
            converted_rows.append(converted)
        normalized[collection_name] = converted_rows
    return normalized


def parse_schema_discovery_output(raw_content: str) -> SchemaDiscoveryPayload:
    try:
        value = _normalize_protocol_value(json.loads(raw_content))
        return SchemaDiscoveryPayload.model_validate(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("schema discovery response does not match the protocol Schema") from exc


def parse_concept_inventory_output(raw_content: str) -> ConceptInventoryPayload:
    try:
        return ConceptInventoryPayload.model_validate(json.loads(raw_content))
    except ValidationError as exc:
        errors = (
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        )
        raise ValueError(
            "concept inventory response does not match the protocol: "
            + "; ".join(errors)[:500]
        ) from exc
    except (TypeError, ValueError) as exc:
        raise ValueError("concept inventory response does not match the protocol") from exc


def _merge_concept_inventory(
    items: Iterable[ConceptInventoryItem],
) -> tuple[ConceptInventoryItem, ...]:
    merged: dict[tuple[str, str], ConceptInventoryItem] = {}
    for item in items:
        key = (_semantic_key(item.label), _semantic_key(item.proposed_entity_type))
        current = merged.get(key)
        if current is None:
            merged[key] = item
            continue
        relation_hints = {
            (
                _semantic_key(hint.label),
                _semantic_key(hint.source_hint),
                _semantic_key(hint.target_hint),
                _semantic_key(hint.direction_hint),
            ): hint
            for hint in current.proposed_relations
        }
        for hint in item.proposed_relations:
            hint_key = (
                _semantic_key(hint.label),
                _semantic_key(hint.source_hint),
                _semantic_key(hint.target_hint),
                _semantic_key(hint.direction_hint),
            )
            existing = relation_hints.get(hint_key)
            if existing is None:
                relation_hints[hint_key] = hint
            else:
                relation_hints[hint_key] = existing.model_copy(
                    update={
                        "evidence_refs": list(
                            dict.fromkeys([*existing.evidence_refs, *hint.evidence_refs])
                        )[:32]
                    }
                )
        merged[key] = current.model_copy(
            update={
                "aliases": list(dict.fromkeys([*current.aliases, *item.aliases]))[:32],
                "identifiers": list(
                    dict.fromkeys([*current.identifiers, *item.identifiers])
                )[:32],
                "proposed_relations": list(relation_hints.values())[:32],
                "evidence_refs": list(
                    dict.fromkeys([*current.evidence_refs, *item.evidence_refs])
                )[:32],
                "confidence": max(current.confidence, item.confidence),
                "ambiguity": list(
                    dict.fromkeys([*current.ambiguity, *item.ambiguity])
                )[:16],
            }
        )
    return tuple(merged.values())


def _validate_schema_discovery_value(value: Any) -> SchemaDiscoveryPayload:
    try:
        return SchemaDiscoveryPayload.model_validate(_normalize_protocol_value(value))
    except ValidationError as exc:
        errors = (
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
        )
        raise _RepairableSchemaValidationError(errors) from exc


def _desensitized_validation_error(exc: ValueError) -> str:
    message = re.sub(r"[\r\n\t]+", " ", str(exc)).strip()
    return message[:500] or "Schema validation failed"


def _semantic_key(value: str) -> str:
    value = re.sub(r"[_\-/]+", " ", value.strip().casefold())
    return re.sub(r"\s+", " ", value)


def _aliases(row: SchemaDiscoveryEntityType | SchemaDiscoveryRelationType) -> tuple[str, ...]:
    return tuple(
        _semantic_key(value)
        for value in (row.key, row.label, *row.aliases)
        if isinstance(value, str) and value.strip()
    )


def _find_semantic_match(
    rows: dict[str, SchemaDiscoveryEntityType | SchemaDiscoveryRelationType],
    row: SchemaDiscoveryEntityType | SchemaDiscoveryRelationType,
) -> str | None:
    names = set(_aliases(row))
    for key, current in rows.items():
        if names.intersection(_aliases(current)):
            return key
    return None


def _merge_attributes(rows: Iterable[SchemaDiscoveryAttribute]) -> tuple[SchemaDiscoveryAttribute, ...]:
    merged: dict[str, SchemaDiscoveryAttribute] = {}
    for row in rows:
        merged.setdefault(_semantic_key(row.key), row)
    return tuple(sorted(merged.values(), key=lambda row: _semantic_key(row.key)))


def merge_schema_discovery_payloads(
    payloads: Iterable[SchemaDiscoveryPayload], *, source_hash: str
) -> BusinessSchemaDraft:
    entities: dict[str, SchemaDiscoveryEntityType] = {}
    relations: dict[str, SchemaDiscoveryRelationType] = {}
    constraints: dict[tuple[str, str, str], SchemaDiscoveryConstraint] = {}
    for payload in payloads:
        for row in payload.entity_types:
            key = _find_semantic_match(entities, row) or _semantic_key(row.key or row.label)
            current = entities.get(key)
            entities[key] = row if current is None else current.model_copy(
                update={
                    "aliases": sorted(set([*current.aliases, *row.aliases, row.key, row.label])),
                    "attributes": list(_merge_attributes([*current.attributes, *row.attributes])),
                }
            )
        for row in payload.relation_types:
            key = _find_semantic_match(relations, row) or _semantic_key(row.key or row.label)
            current = relations.get(key)
            if current is None:
                relations[key] = row
            else:
                direction = "directed" if "directed" in {current.direction, row.direction} else "undirected"
                relations[key] = current.model_copy(
                    update={
                        "direction": direction,
                        "aliases": sorted(set([*current.aliases, *row.aliases, row.key, row.label])),
                        "attributes": list(_merge_attributes([*current.attributes, *row.attributes])),
                    }
                )
        for row in payload.constraints:
            constraints[(_semantic_key(row.source_type_key), _semantic_key(row.relation_type_key), _semantic_key(row.target_type_key))] = row
    entity_names = {alias: row.key for row in entities.values() for alias in _aliases(row)}
    relation_names = {alias: row.key for row in relations.values() for alias in _aliases(row)}
    unresolved_constraints = [
        row
        for row in constraints.values()
        if _semantic_key(row.source_type_key) not in entity_names
        or _semantic_key(row.target_type_key) not in entity_names
        or _semantic_key(row.relation_type_key) not in relation_names
    ]
    if unresolved_constraints:
        raise ValueError("AI Schema discovery returned a constraint with an unknown reference")
    valid_constraints = tuple(
        row.model_copy(
            update={
                "source_type_key": entity_names[_semantic_key(row.source_type_key)],
                "relation_type_key": relation_names[_semantic_key(row.relation_type_key)],
                "target_type_key": entity_names[_semantic_key(row.target_type_key)],
            }
        )
        for row in constraints.values()
        if _semantic_key(row.source_type_key) in entity_names
        and _semantic_key(row.target_type_key) in entity_names
        and _semantic_key(row.relation_type_key) in relation_names
    )
    return BusinessSchemaDraft(
        entity_types=tuple(sorted(entities.values(), key=lambda row: _semantic_key(row.key))),
        relation_types=tuple(sorted(relations.values(), key=lambda row: _semantic_key(row.key))),
        constraints=tuple(sorted(valid_constraints, key=lambda row: (_semantic_key(row.relation_type_key), _semantic_key(row.source_type_key), _semantic_key(row.target_type_key)))),
        source_hash=source_hash,
    )


def schema_draft_to_snapshot(draft: BusinessSchemaDraft, *, ontology_version_id: uuid.UUID) -> dict[str, Any]:
    """Create the immutable extraction snapshot without a business allowlist."""

    def attribute_rows(row: SchemaDiscoveryEntityType | SchemaDiscoveryRelationType) -> list[dict[str, Any]]:
        return [
            {
                "key": attribute.key,
                "value_type": attribute.value_type,
                "required": attribute.required,
                "enum_values": None,
                "validation_schema": None,
            }
            for attribute in sorted(row.attributes, key=lambda item: item.key)
        ]

    namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"vector-kb:schema:{ontology_version_id}")
    entity_ids = {row.key: uuid.uuid5(namespace, f"entity:{row.key}") for row in draft.entity_types}
    relation_ids = {row.key: uuid.uuid5(namespace, f"relation:{row.key}") for row in draft.relation_types}
    entity_ids_by_semantic = {_semantic_key(key): value for key, value in entity_ids.items()}
    relation_ids_by_semantic = {_semantic_key(key): value for key, value in relation_ids.items()}
    entity_rows = []
    for row in draft.entity_types:
        entity_rows.append({"id": str(entity_ids[row.key]), "key": row.key, "label": row.label, "description": row.description, "properties_schema": {"type": "object"}, "active_attribute_definitions": attribute_rows(row)})
    relation_rows = []
    for row in draft.relation_types:
        relation_rows.append({"id": str(relation_ids[row.key]), "key": row.key, "label": row.label, "description": row.description, "direction": row.direction, "requires_evidence": True, "default_review_policy": "auto_active", "properties_schema": {"type": "object"}, "active_attribute_definitions": attribute_rows(row)})
    relation_constraints = [
        {
            "relation_type_id": str(relation_ids_by_semantic[_semantic_key(row.relation_type_key)]),
            "source_entity_type_id": str(entity_ids_by_semantic[_semantic_key(row.source_type_key)]),
            "target_entity_type_id": str(entity_ids_by_semantic[_semantic_key(row.target_type_key)]),
            "cardinality": row.cardinality,
            "requires_review": False,
        }
        for row in draft.constraints
    ]
    return {
        "ontology_version_id": str(ontology_version_id),
        "schema_state": "ai_draft",
        "confirmed": False,
        "origin": "ai_discovery",
        "source_hash": draft.source_hash,
        "entity_types": entity_rows,
        "relation_types": relation_rows,
        "relation_constraints": sorted(
            relation_constraints,
            key=lambda row: (
                row["relation_type_id"],
                row["source_entity_type_id"],
                row["target_entity_type_id"],
            ),
        ),
    }


async def _try_semantic_schema_refinement(
    draft: BusinessSchemaDraft,
    texts: tuple[DiscoveryText, ...],
    *,
    provider: SchemaDiscoveryProvider,
    source_hash: str,
    context_window_tokens: int,
    configured_output_tokens: int | None,
    trace: dict[str, Any] | None = None,
) -> BusinessSchemaDraft:
    if trace is not None:
        trace["semantic_refinement"] = {"attempted": True}
    if not hasattr(provider, "with_output_budget"):
        if trace is not None:
            trace["semantic_refinement"] = {"attempted": False}
        return draft
    compact_schema = _compact_refinement_schema(draft.as_protocol_payload())
    refinement_messages = build_schema_discovery_refinement_messages(
        texts,
        compact_schema,
    )
    refinement_input_tokens = estimate_chat_request_tokens(
        refinement_messages,
        response_format={"type": "json_object"},
        reserved_output_tokens=0,
        model_name=settings.graph_extraction_model,
    )
    previous_schema_tokens = estimate_schema_tokens(compact_schema)
    expected_refinement_output_tokens = (
        max(512, previous_schema_tokens + 256)
        if configured_output_tokens is None
        else min(configured_output_tokens, max(512, previous_schema_tokens + 256))
    )
    try:
        refinement_output_budget = _schema_output_budget(
            stage="semantic_refinement",
            context_window_tokens=context_window_tokens,
            configured_output_tokens=configured_output_tokens,
            input_tokens=refinement_input_tokens,
            expected_output_tokens=expected_refinement_output_tokens,
            previous_schema_tokens=previous_schema_tokens,
        )
    except ValueError:
        log.warning("schema semantic refinement skipped because its request exceeds the context window")
        return draft
    refinement_provider = (
        provider
        if refinement_output_budget is None
        else provider.with_output_budget(refinement_output_budget)
    )
    try:
        refinement_response = await _call_discovery_provider_with_retry(
            refinement_provider,
            refinement_messages,
            output_budget=refinement_output_budget,
        )
        if trace is not None:
            trace["semantic_refinement"].update(
                {
                    "request_payload_hash": refinement_response.request_payload_hash,
                    "finish_reason": refinement_response.finish_reason,
                    "input_token_count": refinement_response.input_token_count,
                    "output_token_count": refinement_response.output_token_count,
                }
            )
        if not refinement_response.content.strip() or refinement_response.finish_reason in {
            "length",
            "max_tokens",
        }:
            return draft
        refined_schema = json.loads(refinement_response.content)
        refined_payload = _validate_schema_discovery_value(refined_schema)
        refined_draft = merge_schema_discovery_payloads(
            [refined_payload],
            source_hash=source_hash,
        )
        validate_business_schema_draft(refined_draft)
        return refined_draft
    except (GraphExtractionProviderError, ValueError, json.JSONDecodeError):
        log.warning("schema semantic refinement failed; retaining the validated discovery draft")
        return draft


async def _discover_concept_inventory(
    batches: tuple[SchemaDiscoveryBatch, ...],
    *,
    provider: SchemaDiscoveryProvider,
    context_window_tokens: int,
    configured_output_tokens: int | None,
) -> tuple[tuple[ConceptInventoryItem, ...], list[dict[str, Any]]]:
    """Run the optional provider capability used by production discovery.

    The capability flag keeps older deterministic test providers compatible,
    while the production OpenAI-compatible provider always opts in.
    """

    inventory: list[ConceptInventoryItem] = []
    trace: list[dict[str, Any]] = []
    for batch in batches:
        messages = build_concept_inventory_messages(batch)
        request_tokens = estimate_chat_request_tokens(
            messages,
            response_format={"type": "json_object"},
            reserved_output_tokens=0,
            model_name=settings.graph_extraction_model,
        )
        output_budget = (
            _schema_output_budget(
                stage="concept_inventory",
                context_window_tokens=context_window_tokens,
                configured_output_tokens=None,
                input_tokens=request_tokens,
                expected_output_tokens=512,
            )
            if configured_output_tokens is None
            else min(
                configured_output_tokens,
                max(128, context_window_tokens - request_tokens - 256),
            )
        )
        batch_provider = (
            provider
            if output_budget is None or not hasattr(provider, "with_output_budget")
            else provider.with_output_budget(output_budget)
        )
        parsed = None
        for attempt in range(2):
            response = await _call_discovery_provider_with_retry(
                batch_provider,
                messages,
                output_budget=output_budget,
            )
            trace.append(
                {
                    "request_payload_hash": response.request_payload_hash,
                    "finish_reason": response.finish_reason,
                    "input_token_count": response.input_token_count,
                    "output_token_count": response.output_token_count,
                    "attempt": attempt + 1,
                }
            )
            if response.finish_reason in {"length", "max_tokens"}:
                raise ValueError("concept inventory response was truncated")
            if not response.content.strip():
                raise ValueError("concept inventory returned an empty response")
            try:
                parsed = parse_concept_inventory_output(response.content)
                break
            except ValueError as exc:
                if attempt == 1:
                    raise ValueError("concept inventory response is not valid") from exc
                messages = build_concept_inventory_repair_messages(
                    batch,
                    _desensitized_validation_error(exc),
                )
        if parsed is None:
            raise ValueError("concept inventory response is not valid")
        inventory.extend(parsed.concepts)
    return _merge_concept_inventory(inventory), trace


async def discover_business_schema(
    texts: Iterable[DiscoveryText],
    *,
    provider: SchemaDiscoveryProvider,
    source_hash: str,
    context_window_tokens: int = 4096,
    max_output_tokens: int | None = None,
    unbounded_output: bool = False,
    concept_inventory_enabled: bool = True,
) -> BusinessSchemaDraft:
    source_texts = tuple(texts)
    configured_output_tokens = (
        None
        if unbounded_output
        else (max_output_tokens or settings.graph_extraction_max_output_tokens)
    )
    effective_context_window = context_window_tokens
    planning_budget = min(
        configured_output_tokens or max(256, effective_context_window // 2),
        max(256, effective_context_window // 2),
    )
    batches = plan_schema_discovery_batches(
        source_texts,
        context_window_tokens=effective_context_window,
        max_output_tokens=planning_budget,
    )
    concept_inventory: tuple[ConceptInventoryItem, ...] = ()
    trace: dict[str, Any] = {
        "concept_inventory": [],
        "schema_synthesis": [],
        "semantic_refinement": {"attempted": False},
    }
    if concept_inventory_enabled and getattr(provider, "supports_concept_inventory", False):
        # Inventory needs a wider independent allowance than the historical
        # extraction cap. A provider-specific no-limit request can become an
        # unparseable runaway response, while 8k truncated this real corpus.
        # Reserve one quarter of the context for protocol and input and let
        # the remaining wide budget accommodate a complete inventory.
        inventory_output_tokens = (
            configured_output_tokens
            if configured_output_tokens is not None
            else max(4_096, (effective_context_window * 3) // 4)
        )
        inventory_planning_budget = min(
            inventory_output_tokens or effective_context_window,
            max(256, effective_context_window // 2),
        )
        inventory_batches = plan_schema_discovery_batches(
            source_texts,
            context_window_tokens=effective_context_window,
            max_output_tokens=inventory_planning_budget,
        )
        concept_inventory, inventory_trace = await _discover_concept_inventory(
            inventory_batches,
            provider=provider,
            context_window_tokens=effective_context_window,
            configured_output_tokens=inventory_output_tokens,
        )
        trace["concept_inventory"] = inventory_trace
    payloads = []
    previous_schemas: list[Any] = []
    validation_errors: list[str] = []
    for batch in batches:
        messages = build_schema_discovery_messages(
            batch,
            concept_inventory=concept_inventory,
        )
        request_input_tokens = estimate_chat_request_tokens(
            messages,
            response_format={"type": "json_object"},
            reserved_output_tokens=0,
            model_name=settings.graph_extraction_model,
        )
        output_budget = (
            _schema_output_budget(
                stage="schema_synthesis",
                context_window_tokens=effective_context_window,
                configured_output_tokens=None,
                input_tokens=request_input_tokens,
                expected_output_tokens=512,
            )
            if configured_output_tokens is None
            else min(
                configured_output_tokens,
                max(128, effective_context_window - request_input_tokens - 256),
            )
        )
        batch_provider = (
            provider
            if output_budget is None or not hasattr(provider, "with_output_budget")
            else provider.with_output_budget(output_budget)
        )
        response = await _call_discovery_provider_with_retry(
            batch_provider,
            messages,
            output_budget=output_budget,
        )
        trace["schema_synthesis"].append(
            {
                "request_payload_hash": response.request_payload_hash,
                "finish_reason": response.finish_reason,
                "input_token_count": response.input_token_count,
                "output_token_count": response.output_token_count,
            }
        )
        if not response.content.strip():
            raise ValueError("schema discovery returned an empty response")
        if response.finish_reason in {"length", "max_tokens"}:
            raise ValueError("schema discovery response was truncated")
        try:
            previous_schema = json.loads(response.content)
        except json.JSONDecodeError as exc:
            raise ValueError("schema discovery response is not valid JSON") from exc
        previous_schemas.append(previous_schema)
        try:
            payloads.append(_validate_schema_discovery_value(previous_schema))
        except _RepairableSchemaValidationError as exc:
            validation_errors.extend(exc.errors)
    if not validation_errors:
        try:
            draft = merge_schema_discovery_payloads(payloads, source_hash=source_hash)
            validate_business_schema_draft(draft)
            refined = await _try_semantic_schema_refinement(
                draft,
                source_texts,
                provider=provider,
                source_hash=source_hash,
                context_window_tokens=effective_context_window,
                configured_output_tokens=configured_output_tokens,
                trace=trace,
            )
            return refined.__class__(
                entity_types=refined.entity_types,
                relation_types=refined.relation_types,
                constraints=refined.constraints,
                source_hash=refined.source_hash,
                concept_inventory=concept_inventory,
                trace=trace,
                status=refined.status,
                confirmed=refined.confirmed,
            )
        except ValueError as exc:
            validation_errors.append(_desensitized_validation_error(exc))
    if not previous_schemas:
        raise ValueError(validation_errors[0])

    repair_messages = build_schema_discovery_repair_messages(
        previous_schemas,
        validation_errors,
    )
    previous_schema_tokens = estimate_schema_tokens(
        previous_schemas[0]
        if len(previous_schemas) == 1
        else {"batch_outputs": previous_schemas}
    )
    expected_repair_output_tokens = (
        max(512, previous_schema_tokens + 256)
        if configured_output_tokens is None
        else min(configured_output_tokens, max(512, previous_schema_tokens + 256))
    )
    repair_input_tokens = estimate_chat_request_tokens(
        repair_messages,
        response_format={"type": "json_object"},
        reserved_output_tokens=0,
        model_name=settings.graph_extraction_model,
    )
    repair_output_budget = _schema_output_budget(
        stage="repair",
        context_window_tokens=effective_context_window,
        configured_output_tokens=configured_output_tokens,
        input_tokens=repair_input_tokens,
        expected_output_tokens=expected_repair_output_tokens,
        previous_schema_tokens=previous_schema_tokens,
    )
    repair_provider = (
        provider
        if repair_output_budget is None or not hasattr(provider, "with_output_budget")
        else provider.with_output_budget(repair_output_budget)
    )
    repaired_response = await _call_discovery_provider_with_retry(
        repair_provider,
        repair_messages,
        output_budget=repair_output_budget,
    )
    trace["schema_repair"] = {
        "request_payload_hash": repaired_response.request_payload_hash,
        "finish_reason": repaired_response.finish_reason,
        "input_token_count": repaired_response.input_token_count,
        "output_token_count": repaired_response.output_token_count,
    }
    if not repaired_response.content.strip():
        raise ValueError("schema discovery repair returned an empty response")
    if repaired_response.finish_reason in {"length", "max_tokens"}:
        raise ValueError("schema discovery repair response was truncated")
    try:
        repaired_schema = json.loads(repaired_response.content)
    except json.JSONDecodeError as exc:
        raise ValueError("schema discovery repair response is not valid JSON") from exc
    try:
        repaired_payload = _validate_schema_discovery_value(repaired_schema)
        draft = merge_schema_discovery_payloads([repaired_payload], source_hash=source_hash)
        validate_business_schema_draft(draft)
        refined = await _try_semantic_schema_refinement(
            draft,
            source_texts,
            provider=provider,
            source_hash=source_hash,
            context_window_tokens=effective_context_window,
            configured_output_tokens=configured_output_tokens,
            trace=trace,
        )
        return refined.__class__(
            entity_types=refined.entity_types,
            relation_types=refined.relation_types,
            constraints=refined.constraints,
            source_hash=refined.source_hash,
            concept_inventory=concept_inventory,
            trace=trace,
            status=refined.status,
            confirmed=refined.confirmed,
        )
    except ValueError as exc:
        raise ValueError("schema discovery repair response failed validation") from exc


async def _call_discovery_provider_with_retry(
    provider: SchemaDiscoveryProvider,
    messages: list[dict[str, str]],
    *,
    output_budget: int,
) -> Any:
    for attempt in range(3):
        try:
            log.info(
                "schema discovery batch request attempt=%s output_budget=%s messages=%s",
                attempt + 1,
                output_budget,
                {"roles": [message.get("role") for message in messages], "chars": [len(message.get("content", "")) for message in messages]},
            )
            return await provider.extract(messages)
        except GraphExtractionProviderError as exc:
            retryable = exc.category in {"timeout", "network_error"} or (
                exc.status_code is not None
                and (exc.status_code == 408 or exc.status_code == 409 or exc.status_code == 429 or exc.status_code >= 500)
            )
            if not retryable or attempt >= 2:
                raise
            await asyncio.sleep(0.2 * (attempt + 1))
    raise RuntimeError("schema discovery retry loop exited unexpectedly")


def validate_business_schema_draft(draft: BusinessSchemaDraft) -> None:
    entity_keys = [_semantic_key(row.key) for row in draft.entity_types]
    relation_keys = [_semantic_key(row.key) for row in draft.relation_types]
    if not entity_keys and not relation_keys:
        raise ValueError("AI Schema discovery returned an empty Schema")
    if len(entity_keys) != len(set(entity_keys)):
        raise ValueError("AI Schema discovery returned duplicate entity type keys")
    if len(relation_keys) != len(set(relation_keys)):
        raise ValueError("AI Schema discovery returned duplicate relation type keys")
    entities = set(entity_keys)
    relations = set(relation_keys)
    for constraint in draft.constraints:
        if (
            _semantic_key(constraint.source_type_key) not in entities
            or _semantic_key(constraint.target_type_key) not in entities
            or _semantic_key(constraint.relation_type_key) not in relations
        ):
            raise ValueError("AI Schema discovery returned a constraint with an unknown reference")


async def persist_business_schema_draft(db, *, library, ontology_version: OntologyVersion, draft: BusinessSchemaDraft) -> dict[str, Any]:
    """Persist a discovered draft as runtime rows, preserving its unconfirmed state."""

    if ontology_version.status != "draft":
        raise ValueError("AI Schema draft can only be written to a draft ontology version")
    validate_business_schema_draft(draft)
    snapshot = schema_draft_to_snapshot(draft, ontology_version_id=ontology_version.id)
    entity_rows = {}
    relation_rows = {}
    for row in draft.entity_types:
        entity_id = uuid.UUID(snapshot["entity_types"][len(entity_rows)]["id"])
        entity_rows[row.key] = await ontology.create_entity_type(
            db,
            library,
            ontology_version.id,
            object_id=entity_id,
            key=row.key,
            label=row.label,
            description=row.description,
            properties_schema={"type": "object"},
            is_seeded=False,
            status="draft",
        )
    for row in draft.relation_types:
        relation_id = uuid.UUID(snapshot["relation_types"][len(relation_rows)]["id"])
        relation_rows[row.key] = await ontology.create_relation_type(
            db,
            library,
            ontology_version.id,
            object_id=relation_id,
            key=row.key,
            label=row.label,
            direction=row.direction,
            default_review_policy="auto_active",
            description=row.description,
            properties_schema={"type": "object"},
            is_seeded=False,
            status="draft",
        )
    for row in draft.entity_types:
        for attribute in row.attributes:
            await ontology.create_attribute_definition(
                db,
                library,
                ontology_version.id,
                owner_kind=ATTRIBUTE_OWNER_ENTITY_TYPE,
                owner_type_id=entity_rows[row.key].id,
                key=attribute.key,
                label=attribute.key,
                value_type=attribute.value_type if attribute.value_type in {"string", "text", "integer", "number", "boolean", "date", "datetime", "json"} else "string",
                required=attribute.required,
                status="draft",
            )
    for row in draft.relation_types:
        for attribute in row.attributes:
            await ontology.create_attribute_definition(
                db,
                library,
                ontology_version.id,
                owner_kind=ATTRIBUTE_OWNER_RELATION_TYPE,
                owner_type_id=relation_rows[row.key].id,
                key=attribute.key,
                label=attribute.key,
                value_type=attribute.value_type if attribute.value_type in {"string", "text", "integer", "number", "boolean", "date", "datetime", "json"} else "string",
                required=attribute.required,
                status="draft",
            )
    for row in draft.constraints:
        await ontology.create_relation_type_constraint(
            db,
            library,
            ontology_version.id,
            relation_type_id=relation_rows[row.relation_type_key].id,
            source_entity_type_id=entity_rows[row.source_type_key].id,
            target_entity_type_id=entity_rows[row.target_type_key].id,
            cardinality=row.cardinality,
            status="draft",
        )
    ontology_version.description = "AI-discovered Schema draft; not user-confirmed"
    ontology_version.origin = "ai_discovery"
    ontology_version.confirmed = False
    ontology_version.status = "draft"
    await db.flush()
    return snapshot
