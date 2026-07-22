from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Literal

from app.services.classification_taxonomy_contracts import (
    ClassificationTaxonomyError,
    normalize_display_label,
    normalize_stable_key,
)


CLASSIFICATION_POLICY_VERSION = "classification-policy-v1"
CLASSIFICATION_INPUT_CONTRACT_VERSION = "classification-input-v1"
CLASSIFICATION_MIN_CONFIDENCE_MICROS = 900_000
CLASSIFICATION_MIN_MARGIN_MICROS = 150_000
CLASSIFICATION_MAX_SECONDARY_LABELS = 8
CLASSIFICATION_MAX_PROPOSALS = 50
CLASSIFICATION_REASON_ORDER = (
    "missing_primary",
    "unknown_label",
    "label_disabled",
    "label_not_enabled",
    "duplicate_label",
    "primary_confidence_below_threshold",
    "primary_margin_below_threshold",
    "secondary_confidence_below_threshold",
    "secondary_limit_exceeded",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ERROR_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


class ClassificationDecisionError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str = "Classification decision operation failed",
    ) -> None:
        self.code = code[:64]
        super().__init__(message[:255])


def _uuid(value: uuid.UUID) -> uuid.UUID:
    if not isinstance(value, uuid.UUID):
        raise ClassificationDecisionError("classification_identity_invalid")
    return value


def _optional_uuid(value: uuid.UUID | None) -> uuid.UUID | None:
    if value is not None:
        _uuid(value)
    return value


def _bounded_identity(value: str, *, maximum: int = 128) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not normalized or len(normalized) > maximum:
        raise ClassificationDecisionError("classification_identity_invalid")
    return normalized


def _sha256(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not _SHA256_RE.fullmatch(normalized):
        raise ClassificationDecisionError("classification_hash_invalid")
    return normalized


def _score(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 1_000_000:
        raise ClassificationDecisionError("classification_score_invalid")
    return value


def _retry_generation(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ClassificationDecisionError("classification_retry_generation_invalid")
    return value


def _error_code(value: str) -> str:
    normalized = value.strip() if isinstance(value, str) else ""
    if not _ERROR_CODE_RE.fullmatch(normalized):
        raise ClassificationDecisionError("classification_error_code_invalid")
    return normalized


def _label_ids(
    values: tuple[uuid.UUID, ...],
    *,
    allow_empty: bool,
    maximum: int,
) -> tuple[uuid.UUID, ...]:
    if (
        not isinstance(values, tuple)
        or (not allow_empty and not values)
        or len(values) > maximum
        or len(set(values)) != len(values)
        or any(not isinstance(value, uuid.UUID) for value in values)
    ):
        raise ClassificationDecisionError("classification_label_selection_invalid")
    return values


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class ClassifierProposalInput:
    role: Literal["primary", "secondary"]
    confidence_micros: int
    label_id: uuid.UUID | None = None
    proposed_key: str | None = None
    proposed_label: str | None = None

    def __post_init__(self) -> None:
        if self.role not in {"primary", "secondary"}:
            raise ClassificationDecisionError("classification_proposal_role_invalid")
        _score(self.confidence_micros)
        known = self.label_id is not None
        unknown = self.proposed_key is not None or self.proposed_label is not None
        if known == unknown:
            raise ClassificationDecisionError("classification_proposal_target_invalid")
        if known:
            _uuid(self.label_id)
            return
        if self.proposed_key is None or self.proposed_label is None:
            raise ClassificationDecisionError("classification_proposal_target_invalid")
        try:
            key = normalize_stable_key(self.proposed_key)
            label = normalize_display_label(self.proposed_label)
        except ClassificationTaxonomyError as exc:
            raise ClassificationDecisionError(
                "classification_proposal_target_invalid"
            ) from exc
        object.__setattr__(self, "proposed_key", key)
        object.__setattr__(self, "proposed_label", label)

    @property
    def target_sort_key(self) -> str:
        return str(self.label_id) if self.label_id is not None else f"~{self.proposed_key}"


def canonicalize_proposals(
    proposals: tuple[ClassifierProposalInput, ...],
) -> tuple[ClassifierProposalInput, ...]:
    if (
        not isinstance(proposals, tuple)
        or not 1 <= len(proposals) <= CLASSIFICATION_MAX_PROPOSALS
        or any(not isinstance(item, ClassifierProposalInput) for item in proposals)
    ):
        raise ClassificationDecisionError("classification_proposals_invalid")
    return tuple(
        sorted(
            proposals,
            key=lambda item: (
                0 if item.role == "primary" else 1,
                -item.confidence_micros,
                item.target_sort_key,
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class SubmitClassificationRunCommand:
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    revision_content_hash: str
    taxonomy_version_id: uuid.UUID
    enabled_label_ids: tuple[uuid.UUID, ...]
    classifier_version: str
    model_provider: str
    model_name: str
    model_config_hash: str
    prompt_version: str
    proposals: tuple[ClassifierProposalInput, ...]
    trigger_type: Literal["revision_ready", "manual", "retry", "repair"]
    retry_generation: int = 0
    requested_by_user_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.document_id)
        _uuid(self.document_revision_id)
        _uuid(self.taxonomy_version_id)
        object.__setattr__(self, "revision_content_hash", _sha256(self.revision_content_hash))
        _label_ids(self.enabled_label_ids, allow_empty=False, maximum=500)
        object.__setattr__(
            self,
            "classifier_version",
            _bounded_identity(self.classifier_version, maximum=64),
        )
        object.__setattr__(
            self,
            "model_provider",
            _bounded_identity(self.model_provider, maximum=64),
        )
        object.__setattr__(
            self,
            "model_name",
            _bounded_identity(self.model_name, maximum=128),
        )
        object.__setattr__(self, "model_config_hash", _sha256(self.model_config_hash))
        object.__setattr__(
            self,
            "prompt_version",
            _bounded_identity(self.prompt_version, maximum=64),
        )
        object.__setattr__(self, "proposals", canonicalize_proposals(self.proposals))
        if self.trigger_type not in {"revision_ready", "manual", "retry", "repair"}:
            raise ClassificationDecisionError("classification_trigger_invalid")
        _retry_generation(self.retry_generation)
        _optional_uuid(self.requested_by_user_id)


@dataclass(frozen=True, slots=True)
class RecordClassificationFailureCommand:
    library_id: uuid.UUID
    document_id: uuid.UUID
    document_revision_id: uuid.UUID
    revision_content_hash: str
    taxonomy_version_id: uuid.UUID
    enabled_label_ids: tuple[uuid.UUID, ...]
    classifier_version: str
    model_provider: str
    model_name: str
    model_config_hash: str
    prompt_version: str
    trigger_type: Literal["revision_ready", "manual", "retry", "repair"]
    error_code: str
    retry_generation: int = 0
    requested_by_user_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.document_id)
        _uuid(self.document_revision_id)
        _uuid(self.taxonomy_version_id)
        object.__setattr__(self, "revision_content_hash", _sha256(self.revision_content_hash))
        _label_ids(self.enabled_label_ids, allow_empty=False, maximum=500)
        object.__setattr__(
            self,
            "classifier_version",
            _bounded_identity(self.classifier_version, maximum=64),
        )
        object.__setattr__(
            self,
            "model_provider",
            _bounded_identity(self.model_provider, maximum=64),
        )
        object.__setattr__(
            self,
            "model_name",
            _bounded_identity(self.model_name, maximum=128),
        )
        object.__setattr__(self, "model_config_hash", _sha256(self.model_config_hash))
        object.__setattr__(
            self,
            "prompt_version",
            _bounded_identity(self.prompt_version, maximum=64),
        )
        if self.trigger_type not in {"revision_ready", "manual", "retry", "repair"}:
            raise ClassificationDecisionError("classification_trigger_invalid")
        object.__setattr__(self, "error_code", _error_code(self.error_code))
        _retry_generation(self.retry_generation)
        _optional_uuid(self.requested_by_user_id)


@dataclass(frozen=True, slots=True)
class ClassificationRunIdentity:
    enabled_label_set_hash: str
    input_fingerprint: str
    idempotency_key: str


def classification_run_identity(
    command: SubmitClassificationRunCommand,
) -> ClassificationRunIdentity:
    enabled_label_set_hash = _canonical_sha256(
        [str(label_id) for label_id in command.enabled_label_ids]
    )
    input_fingerprint = _canonical_sha256(
        {
            "contract_version": CLASSIFICATION_INPUT_CONTRACT_VERSION,
            "library_id": str(command.library_id),
            "document_id": str(command.document_id),
            "document_revision_id": str(command.document_revision_id),
            "revision_content_hash": command.revision_content_hash,
            "taxonomy_version_id": str(command.taxonomy_version_id),
            "enabled_label_set_hash": enabled_label_set_hash,
            "policy_version": CLASSIFICATION_POLICY_VERSION,
            "min_confidence_micros": CLASSIFICATION_MIN_CONFIDENCE_MICROS,
            "min_margin_micros": CLASSIFICATION_MIN_MARGIN_MICROS,
            "max_secondary_labels": CLASSIFICATION_MAX_SECONDARY_LABELS,
            "classifier_version": command.classifier_version,
            "model_provider": command.model_provider,
            "model_name": command.model_name,
            "model_config_hash": command.model_config_hash,
            "prompt_version": command.prompt_version,
            "proposals": [
                {
                    "role": item.role,
                    "confidence_micros": item.confidence_micros,
                    "label_id": str(item.label_id) if item.label_id else None,
                    "proposed_key": item.proposed_key,
                    "proposed_label": item.proposed_label,
                }
                for item in command.proposals
            ],
        }
    )
    idempotency_key = _canonical_sha256(
        {
            "input_fingerprint": input_fingerprint,
            "retry_generation": command.retry_generation,
        }
    )
    return ClassificationRunIdentity(
        enabled_label_set_hash=enabled_label_set_hash,
        input_fingerprint=input_fingerprint,
        idempotency_key=idempotency_key,
    )


def classification_failure_identity(
    command: RecordClassificationFailureCommand,
) -> ClassificationRunIdentity:
    enabled_label_set_hash = _canonical_sha256(
        [str(label_id) for label_id in command.enabled_label_ids]
    )
    input_fingerprint = _canonical_sha256(
        {
            "contract_version": CLASSIFICATION_INPUT_CONTRACT_VERSION,
            "outcome": "failed",
            "library_id": str(command.library_id),
            "document_id": str(command.document_id),
            "document_revision_id": str(command.document_revision_id),
            "revision_content_hash": command.revision_content_hash,
            "taxonomy_version_id": str(command.taxonomy_version_id),
            "enabled_label_set_hash": enabled_label_set_hash,
            "policy_version": CLASSIFICATION_POLICY_VERSION,
            "min_confidence_micros": CLASSIFICATION_MIN_CONFIDENCE_MICROS,
            "min_margin_micros": CLASSIFICATION_MIN_MARGIN_MICROS,
            "max_secondary_labels": CLASSIFICATION_MAX_SECONDARY_LABELS,
            "classifier_version": command.classifier_version,
            "model_provider": command.model_provider,
            "model_name": command.model_name,
            "model_config_hash": command.model_config_hash,
            "prompt_version": command.prompt_version,
            "error_code": command.error_code,
        }
    )
    idempotency_key = _canonical_sha256(
        {
            "input_fingerprint": input_fingerprint,
            "retry_generation": command.retry_generation,
        }
    )
    return ClassificationRunIdentity(
        enabled_label_set_hash=enabled_label_set_hash,
        input_fingerprint=input_fingerprint,
        idempotency_key=idempotency_key,
    )


@dataclass(frozen=True, slots=True)
class ManualClassificationSelection:
    primary_label_id: uuid.UUID
    secondary_label_ids: tuple[uuid.UUID, ...] = ()

    def __post_init__(self) -> None:
        _uuid(self.primary_label_id)
        _label_ids(
            self.secondary_label_ids,
            allow_empty=True,
            maximum=CLASSIFICATION_MAX_SECONDARY_LABELS,
        )
        if self.primary_label_id in self.secondary_label_ids:
            raise ClassificationDecisionError("classification_label_selection_invalid")


@dataclass(frozen=True, slots=True)
class ReviewClassificationRunCommand:
    library_id: uuid.UUID
    actor_user_id: uuid.UUID
    run_id: uuid.UUID
    expected_run_status: Literal["pending_review", "blocked_manual"]
    expected_effective_decision_set_id: uuid.UUID | None
    action: Literal["accept", "change", "reject"]
    selection: ManualClassificationSelection | None = None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.actor_user_id)
        _uuid(self.run_id)
        _optional_uuid(self.expected_effective_decision_set_id)
        if self.expected_run_status not in {"pending_review", "blocked_manual"}:
            raise ClassificationDecisionError("classification_run_state_invalid")
        if self.action not in {"accept", "change", "reject"}:
            raise ClassificationDecisionError("classification_review_action_invalid")
        if (self.action == "change") != (self.selection is not None):
            raise ClassificationDecisionError("classification_review_selection_invalid")


@dataclass(frozen=True, slots=True)
class SetManualClassificationCommand:
    library_id: uuid.UUID
    actor_user_id: uuid.UUID
    document_id: uuid.UUID
    expected_effective_decision_set_id: uuid.UUID | None
    selection: ManualClassificationSelection

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.actor_user_id)
        _uuid(self.document_id)
        _optional_uuid(self.expected_effective_decision_set_id)
        if not isinstance(self.selection, ManualClassificationSelection):
            raise ClassificationDecisionError("classification_review_selection_invalid")


@dataclass(frozen=True, slots=True)
class RemoveManualClassificationCommand:
    library_id: uuid.UUID
    actor_user_id: uuid.UUID
    document_id: uuid.UUID
    expected_effective_decision_set_id: uuid.UUID | None

    def __post_init__(self) -> None:
        _uuid(self.library_id)
        _uuid(self.actor_user_id)
        _uuid(self.document_id)
        _optional_uuid(self.expected_effective_decision_set_id)
