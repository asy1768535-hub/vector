from __future__ import annotations

import difflib
import json
import re
import unicodedata
import uuid
from dataclasses import dataclass

from pydantic import ValidationError

from app.config import Settings, settings
from app.schemas.taxonomy_bootstrap import TaxonomyBootstrapLlmOutput

from app.services.classification_runtime_contracts import canonical_sha256
from app.services.classification_taxonomy_contracts import (
    MAX_LABELS_PER_TAXONOMY,
    ClassificationTaxonomyError,
    normalize_description,
    normalize_display_label,
    normalize_stable_key,
)


BOOTSTRAP_SOURCE_TYPES = (
    "builtin_template",
    "admin_import",
    "llm_proposal",
)
MAX_BOOTSTRAP_WARNINGS = 50
MAX_LLM_BOOTSTRAP_LABELS = 50
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class TaxonomyBootstrapError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str = "Taxonomy bootstrap operation failed",
    ) -> None:
        self.code = code[:64]
        super().__init__(message[:255])


def fail_bootstrap(code: str, message: str) -> None:
    raise TaxonomyBootstrapError(code, message)


def _require_uuid(value: uuid.UUID, code: str = "bootstrap_identity_invalid") -> None:
    if not isinstance(value, uuid.UUID):
        fail_bootstrap(code, "taxonomy bootstrap identity is invalid")


def _normalize_bounded_text(
    value: str,
    *,
    maximum: int,
    code: str,
) -> str:
    normalized = (
        " ".join(unicodedata.normalize("NFKC", value).split())
        if isinstance(value, str)
        else ""
    )
    if not normalized or len(normalized) > maximum:
        fail_bootstrap(code, "taxonomy bootstrap text is invalid")
    return normalized


def _normalize_version(value: str) -> str:
    return _normalize_bounded_text(
        value,
        maximum=64,
        code="bootstrap_source_version_invalid",
    )


def require_error_code(value: str) -> str:
    if not isinstance(value, str) or not _ERROR_CODE_RE.fullmatch(value):
        fail_bootstrap("bootstrap_error_code_invalid", "bootstrap error code is invalid")
    return value


@dataclass(frozen=True, slots=True)
class DraftLabelInput:
    key: str
    label: str
    description: str | None = None
    parent_key: str | None = None
    sort_order: int = 0
    status: str = "active"

    def __post_init__(self) -> None:
        try:
            object.__setattr__(self, "key", normalize_stable_key(self.key))
            object.__setattr__(self, "label", normalize_display_label(self.label))
            object.__setattr__(
                self,
                "description",
                normalize_description(self.description),
            )
            if self.parent_key is not None:
                object.__setattr__(
                    self,
                    "parent_key",
                    normalize_stable_key(self.parent_key),
                )
        except ClassificationTaxonomyError as exc:
            raise TaxonomyBootstrapError(exc.code) from exc
        if (
            isinstance(self.sort_order, bool)
            or not isinstance(self.sort_order, int)
            or not 0 <= self.sort_order <= 1_000_000
        ):
            fail_bootstrap(
                "classification_sort_order_invalid",
                "taxonomy bootstrap sort order is invalid",
            )
        if self.status not in {"active", "disabled"}:
            fail_bootstrap(
                "classification_label_status_invalid",
                "taxonomy bootstrap label status is invalid",
            )


@dataclass(frozen=True, slots=True)
class TaxonomyBootstrapWarning:
    code: str
    left_key: str
    right_key: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "left_key": self.left_key,
            "right_key": self.right_key,
        }


@dataclass(frozen=True, slots=True)
class NormalizedTaxonomyDraft:
    labels: tuple[DraftLabelInput, ...]
    warnings: tuple[TaxonomyBootstrapWarning, ...]
    payload_hash: str


def _canonical_label(label: DraftLabelInput) -> dict[str, object]:
    return {
        "key": label.key,
        "label": label.label,
        "description": label.description,
        "parent_key": label.parent_key,
        "sort_order": label.sort_order,
        "status": label.status,
    }


def normalize_taxonomy_draft(
    labels: tuple[DraftLabelInput, ...],
    *,
    minimum: int = 1,
    maximum: int = MAX_LABELS_PER_TAXONOMY,
) -> NormalizedTaxonomyDraft:
    if (
        not isinstance(labels, tuple)
        or isinstance(minimum, bool)
        or isinstance(maximum, bool)
        or not isinstance(minimum, int)
        or not isinstance(maximum, int)
        or minimum < 1
        or maximum < minimum
        or maximum > MAX_LABELS_PER_TAXONOMY
        or not minimum <= len(labels) <= maximum
        or any(not isinstance(label, DraftLabelInput) for label in labels)
    ):
        fail_bootstrap(
            "bootstrap_label_count_invalid",
            "taxonomy bootstrap label count is invalid",
        )
    ordered = tuple(sorted(labels, key=lambda item: (item.sort_order, item.key)))
    by_key = {label.key: label for label in ordered}
    if len(by_key) != len(ordered):
        fail_bootstrap(
            "bootstrap_label_duplicate",
            "taxonomy bootstrap label keys must be unique",
        )
    display_identities = [label.label.casefold() for label in ordered]
    if len(set(display_identities)) != len(display_identities):
        fail_bootstrap(
            "bootstrap_label_duplicate",
            "taxonomy bootstrap display labels must be unique",
        )
    for label in ordered:
        if label.parent_key is None:
            continue
        parent = by_key.get(label.parent_key)
        if parent is None or parent.key == label.key:
            fail_bootstrap(
                "bootstrap_parent_invalid",
                "taxonomy bootstrap parent is outside the draft",
            )
        if label.status == "active" and parent.status != "active":
            fail_bootstrap(
                "bootstrap_parent_disabled",
                "active bootstrap labels require an active parent",
            )
    for label in ordered:
        seen: set[str] = set()
        current = label
        while current.parent_key is not None:
            if current.key in seen:
                fail_bootstrap(
                    "bootstrap_parent_cycle",
                    "taxonomy bootstrap label parents contain a cycle",
                )
            seen.add(current.key)
            current = by_key[current.parent_key]

    warnings: list[TaxonomyBootstrapWarning] = []
    for index, left in enumerate(ordered):
        left_identity = left.label.casefold()
        for right in ordered[index + 1 :]:
            right_identity = right.label.casefold()
            if min(len(left_identity), len(right_identity)) < 4:
                continue
            ratio = difflib.SequenceMatcher(
                None,
                left_identity,
                right_identity,
                autojunk=False,
            ).ratio()
            if ratio >= 0.9:
                warnings.append(
                    TaxonomyBootstrapWarning(
                        "near_duplicate_label",
                        left.key,
                        right.key,
                    )
                )
                if len(warnings) >= MAX_BOOTSTRAP_WARNINGS:
                    break
        if len(warnings) >= MAX_BOOTSTRAP_WARNINGS:
            break
    return NormalizedTaxonomyDraft(
        ordered,
        tuple(warnings),
        canonical_sha256([_canonical_label(label) for label in ordered]),
    )


@dataclass(frozen=True, slots=True)
class ApplyBuiltinTemplateCommand:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    request_id: uuid.UUID
    template_key: str
    version_key: str = "document-category"
    description: str | None = None

    def __post_init__(self) -> None:
        for value in (self.organization_id, self.actor_user_id, self.request_id):
            _require_uuid(value)
        try:
            object.__setattr__(self, "template_key", normalize_stable_key(self.template_key))
            object.__setattr__(self, "version_key", normalize_stable_key(self.version_key))
            object.__setattr__(self, "description", normalize_description(self.description))
        except ClassificationTaxonomyError as exc:
            raise TaxonomyBootstrapError(exc.code) from exc


@dataclass(frozen=True, slots=True)
class ImportTaxonomyBootstrapCommand:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    request_id: uuid.UUID
    source_name: str
    source_version: str
    labels: tuple[DraftLabelInput, ...]
    version_key: str = "document-category"
    description: str | None = None

    def __post_init__(self) -> None:
        for value in (self.organization_id, self.actor_user_id, self.request_id):
            _require_uuid(value)
        object.__setattr__(
            self,
            "source_name",
            _normalize_bounded_text(
                self.source_name,
                maximum=160,
                code="bootstrap_source_name_invalid",
            ),
        )
        object.__setattr__(self, "source_version", _normalize_version(self.source_version))
        try:
            object.__setattr__(self, "version_key", normalize_stable_key(self.version_key))
            object.__setattr__(self, "description", normalize_description(self.description))
        except ClassificationTaxonomyError as exc:
            raise TaxonomyBootstrapError(exc.code) from exc
        normalize_taxonomy_draft(self.labels)


@dataclass(frozen=True, slots=True)
class PrepareLlmTaxonomyBootstrapCommand:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    request_id: uuid.UUID
    sample_revision_ids: tuple[uuid.UUID, ...]
    version_key: str = "document-category"
    description: str | None = None

    def __post_init__(self) -> None:
        for value in (self.organization_id, self.actor_user_id, self.request_id):
            _require_uuid(value)
        if (
            not isinstance(self.sample_revision_ids, tuple)
            or not 1 <= len(self.sample_revision_ids) <= 20
            or len(set(self.sample_revision_ids)) != len(self.sample_revision_ids)
            or any(not isinstance(value, uuid.UUID) for value in self.sample_revision_ids)
        ):
            fail_bootstrap(
                "bootstrap_sample_selection_invalid",
                "taxonomy bootstrap sample selection is invalid",
            )
        try:
            object.__setattr__(self, "version_key", normalize_stable_key(self.version_key))
            object.__setattr__(self, "description", normalize_description(self.description))
        except ClassificationTaxonomyError as exc:
            raise TaxonomyBootstrapError(exc.code) from exc


def require_sha256(value: str, code: str = "bootstrap_hash_invalid") -> str:
    if not isinstance(value, str) or not _HASH_RE.fullmatch(value):
        fail_bootstrap(code, "taxonomy bootstrap hash is invalid")
    return value


@dataclass(frozen=True, slots=True)
class LlmTaxonomyProposal:
    description: str | None
    draft: NormalizedTaxonomyDraft


def taxonomy_bootstrap_model_config_hash(config: Settings = settings) -> str:
    return canonical_sha256(
        {
            "provider": "deepseek",
            "model": config.classification_model,
            "prompt_version": config.classification_taxonomy_bootstrap_prompt_version,
            "max_source_chars": config.classification_taxonomy_bootstrap_max_source_chars,
            "temperature": 0,
            "response_format": "json_object",
            "output_contract": "taxonomy-bootstrap-output-v1",
        }
    )


def taxonomy_bootstrap_messages(
    samples: tuple[tuple[str, str], ...],
) -> list[dict[str, str]]:
    if (
        not isinstance(samples, tuple)
        or not samples
        or len(samples) > 20
        or any(
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(source, str)
            or not source.strip()
            for title, source in samples
        )
    ):
        fail_bootstrap(
            "bootstrap_sample_selection_invalid",
            "taxonomy bootstrap samples are invalid",
        )
    instructions = (
        "Propose a concise enterprise document taxonomy from the authorized samples. "
        "Return strict JSON with optional description and labels (3..50). Each label "
        "must contain key, label, optional description, optional parent_key, and integer "
        "sort_order. Keys must be lowercase ASCII stable identifiers. Do not add fields."
    )
    payload = json.dumps(
        {
            "authorized_document_samples": [
                {"title": title, "text": source} for title, source in samples
            ]
        },
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": payload},
    ]


def parse_llm_taxonomy_bootstrap_output(content: str) -> LlmTaxonomyProposal:
    if not isinstance(content, str) or len(content) > 100_000:
        fail_bootstrap(
            "provider_invalid_output",
            "taxonomy bootstrap provider output is invalid",
        )
    try:
        payload = json.loads(content)
        parsed = TaxonomyBootstrapLlmOutput.model_validate(payload)
        description = normalize_description(parsed.description)
        labels = tuple(
            DraftLabelInput(
                key=item.key,
                label=item.label,
                description=item.description,
                parent_key=item.parent_key,
                sort_order=item.sort_order,
                status="active",
            )
            for item in parsed.labels
        )
        draft = normalize_taxonomy_draft(
            labels,
            minimum=3,
            maximum=MAX_LLM_BOOTSTRAP_LABELS,
        )
    except (
        json.JSONDecodeError,
        ValidationError,
        ClassificationTaxonomyError,
        TaxonomyBootstrapError,
        TypeError,
        ValueError,
    ) as exc:
        raise TaxonomyBootstrapError(
            "provider_invalid_output",
            "taxonomy bootstrap provider output is invalid",
        ) from exc
    return LlmTaxonomyProposal(description=description, draft=draft)
