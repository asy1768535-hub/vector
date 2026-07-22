from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass

from pydantic import ValidationError

from app.config import Settings, settings
from app.models.classification_job import DocumentClassificationJob
from app.schemas.classifier import ClassifierOutputV1
from app.services.classification_decision_contracts import (
    ClassifierProposalInput,
    ClassificationDecisionError,
)
from app.services.classification_taxonomy_contracts import (
    ClassificationTaxonomyError,
    normalize_stable_key,
)


CLASSIFICATION_JOB_INPUT_VERSION = "classification-job-input-v1"
CLASSIFICATION_PROVIDER_NAME = "deepseek"
CLASSIFICATION_PROVIDER_BASE_URL = "https://api.deepseek.com/v1"
CLASSIFICATION_PROVIDER_MODEL = "deepseek-v4-pro"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class ClassificationRuntimeError(ValueError):
    def __init__(self, code: str, message: str = "Classification runtime failed") -> None:
        self.code = code[:64]
        super().__init__(message[:255])


def fail_classification_runtime(code: str, message: str) -> None:
    raise ClassificationRuntimeError(code, message)


def canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ClassificationRuntimeError(
            "classification_identity_invalid",
            "classification identity is not canonical JSON",
        ) from exc


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def classification_model_config_hash(config: Settings = settings) -> str:
    return canonical_sha256(
        {
            "provider": CLASSIFICATION_PROVIDER_NAME,
            "model": config.classification_model,
            "prompt_version": config.classification_prompt_version,
            "max_source_chars": config.classification_model_max_source_chars,
            "temperature": 0,
            "response_format": "json_object",
            "output_contract": "classifier-output-v1",
        }
    )


@dataclass(frozen=True, slots=True)
class ClassificationJobIdentity:
    enabled_label_set_hash: str
    model_config_hash: str
    input_fingerprint: str
    idempotency_key: str


def classification_job_identity(
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    revision_content_hash: str,
    taxonomy_version_id: uuid.UUID,
    enabled_label_ids: tuple[uuid.UUID, ...],
    retry_generation: int,
    config: Settings = settings,
) -> ClassificationJobIdentity:
    identities = (
        library_id,
        document_id,
        document_revision_id,
        taxonomy_version_id,
    )
    if any(not isinstance(value, uuid.UUID) for value in identities):
        fail_classification_runtime(
            "classification_identity_invalid",
            "classification scope identity is invalid",
        )
    if not isinstance(revision_content_hash, str) or not _SHA256_RE.fullmatch(
        revision_content_hash
    ):
        fail_classification_runtime(
            "classification_hash_invalid",
            "classification Revision hash is invalid",
        )
    if (
        any(not isinstance(value, uuid.UUID) for value in enabled_label_ids)
        or not enabled_label_ids
        or len(enabled_label_ids) > 500
        or len(set(enabled_label_ids)) != len(enabled_label_ids)
    ):
        fail_classification_runtime(
            "classification_label_selection_invalid",
            "classification enabled label identity is invalid",
        )
    if isinstance(retry_generation, bool) or not isinstance(retry_generation, int) or retry_generation < 0:
        fail_classification_runtime(
            "classification_retry_generation_invalid",
            "classification retry generation is invalid",
        )
    enabled_hash = canonical_sha256([str(value) for value in enabled_label_ids])
    model_hash = classification_model_config_hash(config)
    fingerprint = canonical_sha256(
        {
            "contract_version": CLASSIFICATION_JOB_INPUT_VERSION,
            "library_id": str(library_id),
            "document_id": str(document_id),
            "document_revision_id": str(document_revision_id),
            "revision_content_hash": revision_content_hash,
            "taxonomy_version_id": str(taxonomy_version_id),
            "enabled_label_set_hash": enabled_hash,
            "classifier_version": config.classification_classifier_version,
            "model_provider": CLASSIFICATION_PROVIDER_NAME,
            "model_name": config.classification_model,
            "model_config_hash": model_hash,
            "prompt_version": config.classification_prompt_version,
        }
    )
    return ClassificationJobIdentity(
        enabled_label_set_hash=enabled_hash,
        model_config_hash=model_hash,
        input_fingerprint=fingerprint,
        idempotency_key=canonical_sha256(
            {
                "input_fingerprint": fingerprint,
                "retry_generation": retry_generation,
            }
        ),
    )


def build_classification_job(
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    revision_content_hash: str,
    taxonomy_version_id: uuid.UUID,
    enabled_label_ids: tuple[uuid.UUID, ...],
    trigger_type: str,
    retry_generation: int = 0,
    rerun_of_job_id: uuid.UUID | None = None,
    requested_by_user_id: uuid.UUID | None = None,
    config: Settings = settings,
) -> DocumentClassificationJob:
    if trigger_type not in {"revision_ready", "manual", "retry", "repair"}:
        fail_classification_runtime(
            "classification_trigger_invalid",
            "classification trigger is invalid",
        )
    identity = classification_job_identity(
        library_id=library_id,
        document_id=document_id,
        document_revision_id=document_revision_id,
        revision_content_hash=revision_content_hash,
        taxonomy_version_id=taxonomy_version_id,
        enabled_label_ids=enabled_label_ids,
        retry_generation=retry_generation,
        config=config,
    )
    return DocumentClassificationJob(
        id=uuid.uuid4(),
        library_id=library_id,
        document_id=document_id,
        document_revision_id=document_revision_id,
        revision_content_hash=revision_content_hash,
        taxonomy_version_id=taxonomy_version_id,
        enabled_label_set_hash=identity.enabled_label_set_hash,
        classifier_version=config.classification_classifier_version,
        model_provider=CLASSIFICATION_PROVIDER_NAME,
        model_name=config.classification_model,
        model_config_hash=identity.model_config_hash,
        prompt_version=config.classification_prompt_version,
        input_fingerprint=identity.input_fingerprint,
        idempotency_key=identity.idempotency_key,
        retry_generation=retry_generation,
        trigger_type=trigger_type,
        status="queued",
        attempt_count=0,
        rerun_of_job_id=rerun_of_job_id,
        requested_by_user_id=requested_by_user_id,
    )


def parse_classifier_output(
    content: str,
    *,
    labels_by_key: dict[str, uuid.UUID],
) -> tuple[ClassifierProposalInput, ...]:
    if not isinstance(content, str) or len(content) > 100_000:
        fail_classification_runtime(
            "provider_invalid_output",
            "classification provider output is invalid",
        )
    try:
        payload = json.loads(content)
        value = ClassifierOutputV1.model_validate(payload)
    except (json.JSONDecodeError, ValidationError, TypeError, ValueError) as exc:
        raise ClassificationRuntimeError(
            "provider_invalid_output",
            "classification provider output is invalid",
        ) from exc

    proposals: list[ClassifierProposalInput] = []
    try:
        for role, candidates in (
            ("primary", value.primary_candidates),
            ("secondary", value.secondary_candidates),
        ):
            for candidate in candidates:
                if candidate.label_key is not None:
                    key = normalize_stable_key(candidate.label_key)
                    label_id = labels_by_key.get(key)
                    if label_id is None:
                        fail_classification_runtime(
                            "provider_unknown_label_key",
                            "classification provider returned an unknown known-label key",
                        )
                    proposal = ClassifierProposalInput(
                        role=role,
                        confidence_micros=candidate.confidence_micros,
                        label_id=label_id,
                    )
                else:
                    proposal = ClassifierProposalInput(
                        role=role,
                        confidence_micros=candidate.confidence_micros,
                        proposed_key=candidate.proposed_key,
                        proposed_label=candidate.proposed_label,
                    )
                proposals.append(proposal)
        return tuple(proposals)
    except (ClassificationDecisionError, ClassificationTaxonomyError) as exc:
        raise ClassificationRuntimeError(
            "provider_invalid_output",
            "classification provider output does not match governance",
        ) from exc


def classification_messages(
    source_text: str,
    *,
    enabled_labels: tuple[tuple[str, str, str | None], ...],
) -> list[dict[str, str]]:
    labels = [
        {
            "key": key,
            "label": label,
            "description": description,
        }
        for key, label, description in enabled_labels
    ]
    instructions = (
        "Classify the document using the supplied selectable labels. Return strict JSON "
        "with primary_candidates (1..5) and secondary_candidates (0..8). Each item must "
        "contain integer confidence_micros and either label_key or proposed_key plus "
        "proposed_label. Do not add any other fields."
    )
    user_payload = canonical_json(
        {
            "selectable_labels": labels,
            "document_text": source_text,
        }
    )
    return [
        {"role": "system", "content": instructions},
        {"role": "user", "content": user_payload},
    ]
