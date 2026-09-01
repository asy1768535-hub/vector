from __future__ import annotations

from typing import Literal


ProcessingStage = Literal["summary", "outline", "classification", "graph"]
PROCESSING_STAGES: tuple[ProcessingStage, ...] = (
    "summary",
    "outline",
    "classification",
    "graph",
)

SAFE_STAGE_ERROR_CODES = frozenset(
    {
        "attempt_limit_exceeded",
        "cancelled_by_user",
        "claim_lost",
        "historical_revision",
        "invalid_provider_json",
        "invalid_provider_payload",
        "job_identity_stale",
        "job_input_stale",
        "lease_expired",
        "lease_expired_attempt_limit",
        "provider_http_error",
        "provider_network_error",
        "provider_timeout",
        "revision_content_changed",
        "revision_not_ready",
        "security_level_denied",
        "security_level_missing",
    }
)


class DocumentProcessingError(RuntimeError):
    def __init__(self, code: str, message: str = "Document processing request failed") -> None:
        self.code = code[:64]
        super().__init__(message[:255])


def safe_stage_error_code(value: object) -> str | None:
    if value is None:
        return None
    code = str(value).strip()
    if code in SAFE_STAGE_ERROR_CODES:
        return code
    return "stage_failed"


def retry_rejection_code(value: object) -> str:
    code = str(value or "").strip()
    if code in {
        "historical_revision",
        "job_identity_stale",
        "job_input_stale",
        "classification_job_identity_stale",
        "classification_revision_stale",
        "classification_taxonomy_state_changed",
        "classification_label_set_state_changed",
    }:
        return "processing_job_stale"
    if code in {
        "knowledge_artifact_runtime_disabled",
        "library_artifact_type_disabled",
        "classification_runtime_disabled",
        "classification_external_model_disabled",
        "graph_extraction_disabled",
        "library_graph_disabled",
        "library_external_llm_disabled",
        "library_external_model_disabled",
        "library_classification_model_disabled",
        "security_allowlist_empty",
        "security_level_missing",
        "security_level_denied",
    }:
        return "processing_stage_unavailable"
    if code in {
        "job_not_retryable",
        "classification_job_not_retryable",
        "no_retryable_units",
    }:
        return "processing_job_not_retryable"
    return "processing_retry_rejected"
