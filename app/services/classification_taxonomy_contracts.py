from __future__ import annotations

import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime

from app.models.classification_taxonomy import (
    CLASSIFICATION_LABEL_STATUSES,
    TAXONOMY_STATUSES,
)


MAX_LABELS_PER_TAXONOMY = 500
_STABLE_KEY_RE = re.compile(r"^[a-z](?:[a-z0-9_-]{0,62}[a-z0-9])?$")


class ClassificationTaxonomyError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str = "Classification taxonomy operation failed",
    ) -> None:
        self.code = code[:64]
        super().__init__(message[:255])


def normalize_stable_key(value: str) -> str:
    normalized = (
        unicodedata.normalize("NFKC", value).strip().casefold()
        if isinstance(value, str)
        else ""
    )
    if not _STABLE_KEY_RE.fullmatch(normalized):
        raise ClassificationTaxonomyError("classification_key_invalid")
    return normalized


def normalize_display_label(value: str) -> str:
    normalized = (
        " ".join(unicodedata.normalize("NFKC", value).split())
        if isinstance(value, str)
        else ""
    )
    if not normalized or len(normalized) > 160:
        raise ClassificationTaxonomyError("classification_label_invalid")
    return normalized


def normalize_description(value: str | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ClassificationTaxonomyError("classification_description_invalid")
    normalized = " ".join(unicodedata.normalize("NFKC", value).split())
    if not normalized:
        return None
    if len(normalized) > 1000:
        raise ClassificationTaxonomyError("classification_description_invalid")
    return normalized


def _require_uuid(
    value: uuid.UUID,
    code: str = "classification_identity_invalid",
) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise ClassificationTaxonomyError(code)
    return value


def _require_expected_time(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ClassificationTaxonomyError("classification_expected_state_invalid")
    return value


def _require_choice(value: str, choices: tuple[str, ...], code: str) -> str:
    if value not in choices:
        raise ClassificationTaxonomyError(code)
    return value


def validate_version_no(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 1 <= value <= 2_147_483_647
    ):
        raise ClassificationTaxonomyError("classification_version_invalid")
    return value


def _require_sort_order(value: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 1_000_000
    ):
        raise ClassificationTaxonomyError("classification_sort_order_invalid")
    return value


def _require_id_selection(
    values: tuple[uuid.UUID, ...],
    *,
    allow_empty: bool,
) -> tuple[uuid.UUID, ...]:
    if (
        not isinstance(values, tuple)
        or (not allow_empty and not values)
        or len(values) > MAX_LABELS_PER_TAXONOMY
        or len(set(values)) != len(values)
        or any(not isinstance(value, uuid.UUID) for value in values)
    ):
        raise ClassificationTaxonomyError("classification_label_selection_invalid")
    return values


@dataclass(frozen=True, slots=True)
class CreateTaxonomyCommand:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    version_key: str
    version_no: int
    description: str | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.organization_id)
        _require_uuid(self.actor_user_id)
        object.__setattr__(self, "version_key", normalize_stable_key(self.version_key))
        validate_version_no(self.version_no)
        object.__setattr__(self, "description", normalize_description(self.description))


@dataclass(frozen=True, slots=True)
class UpdateTaxonomyDraftCommand:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    taxonomy_id: uuid.UUID
    expected_status: str
    expected_updated_at: datetime
    description: str | None

    def __post_init__(self) -> None:
        _require_uuid(self.organization_id)
        _require_uuid(self.actor_user_id)
        _require_uuid(self.taxonomy_id)
        _require_choice(
            self.expected_status,
            TAXONOMY_STATUSES,
            "classification_status_invalid",
        )
        _require_expected_time(self.expected_updated_at)
        object.__setattr__(self, "description", normalize_description(self.description))


@dataclass(frozen=True, slots=True)
class CopyTaxonomyVersionCommand:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    source_taxonomy_id: uuid.UUID
    expected_source_status: str
    expected_source_updated_at: datetime
    description: str | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.organization_id)
        _require_uuid(self.actor_user_id)
        _require_uuid(self.source_taxonomy_id)
        _require_choice(
            self.expected_source_status,
            TAXONOMY_STATUSES,
            "classification_status_invalid",
        )
        _require_expected_time(self.expected_source_updated_at)
        object.__setattr__(self, "description", normalize_description(self.description))


@dataclass(frozen=True, slots=True)
class CreateClassificationLabelCommand:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    taxonomy_id: uuid.UUID
    expected_taxonomy_updated_at: datetime
    key: str
    label: str
    description: str | None = None
    parent_label_id: uuid.UUID | None = None
    sort_order: int = 0
    status: str = "active"

    def __post_init__(self) -> None:
        _require_uuid(self.organization_id)
        _require_uuid(self.actor_user_id)
        _require_uuid(self.taxonomy_id)
        _require_expected_time(self.expected_taxonomy_updated_at)
        if self.parent_label_id is not None:
            _require_uuid(self.parent_label_id)
        object.__setattr__(self, "key", normalize_stable_key(self.key))
        object.__setattr__(self, "label", normalize_display_label(self.label))
        object.__setattr__(self, "description", normalize_description(self.description))
        _require_sort_order(self.sort_order)
        _require_choice(
            self.status,
            CLASSIFICATION_LABEL_STATUSES,
            "classification_label_status_invalid",
        )


@dataclass(frozen=True, slots=True)
class UpdateClassificationLabelCommand:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    taxonomy_id: uuid.UUID
    label_id: uuid.UUID
    expected_taxonomy_updated_at: datetime
    expected_updated_at: datetime
    key: str
    label: str
    description: str | None
    parent_label_id: uuid.UUID | None
    sort_order: int
    status: str

    def __post_init__(self) -> None:
        _require_uuid(self.organization_id)
        _require_uuid(self.actor_user_id)
        _require_uuid(self.taxonomy_id)
        _require_uuid(self.label_id)
        _require_expected_time(self.expected_taxonomy_updated_at)
        _require_expected_time(self.expected_updated_at)
        if self.parent_label_id is not None:
            _require_uuid(self.parent_label_id)
        object.__setattr__(self, "key", normalize_stable_key(self.key))
        object.__setattr__(self, "label", normalize_display_label(self.label))
        object.__setattr__(self, "description", normalize_description(self.description))
        _require_sort_order(self.sort_order)
        _require_choice(
            self.status,
            CLASSIFICATION_LABEL_STATUSES,
            "classification_label_status_invalid",
        )


@dataclass(frozen=True, slots=True)
class ActivateTaxonomyCommand:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    taxonomy_id: uuid.UUID
    expected_status: str
    expected_updated_at: datetime

    def __post_init__(self) -> None:
        _require_uuid(self.organization_id)
        _require_uuid(self.actor_user_id)
        _require_uuid(self.taxonomy_id)
        _require_choice(
            self.expected_status,
            TAXONOMY_STATUSES,
            "classification_status_invalid",
        )
        _require_expected_time(self.expected_updated_at)


@dataclass(frozen=True, slots=True)
class ReplaceLibraryClassificationLabelsCommand:
    organization_id: uuid.UUID
    actor_user_id: uuid.UUID
    library_id: uuid.UUID
    taxonomy_id: uuid.UUID
    expected_taxonomy_updated_at: datetime
    expected_label_ids: tuple[uuid.UUID, ...]
    label_ids: tuple[uuid.UUID, ...]

    def __post_init__(self) -> None:
        _require_uuid(self.organization_id)
        _require_uuid(self.actor_user_id)
        _require_uuid(self.library_id)
        _require_uuid(self.taxonomy_id)
        _require_expected_time(self.expected_taxonomy_updated_at)
        _require_id_selection(self.expected_label_ids, allow_empty=True)
        _require_id_selection(self.label_ids, allow_empty=False)
