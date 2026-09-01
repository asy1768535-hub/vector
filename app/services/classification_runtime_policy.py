from __future__ import annotations

from app.config import Settings, settings
from app.services.classification_runtime_contracts import (
    classification_provider_name,
    fail_classification_runtime,
)


SUPERSEDED_CLASSIFICATION_CODES = {
    "historical_revision",
    "revision_not_ready",
    "revision_content_changed",
    "classification_taxonomy_state_changed",
    "classification_label_set_state_changed",
    "classification_revision_stale",
    "classification_revision_unavailable",
    "classification_job_identity_stale",
}
CANCELLED_CLASSIFICATION_CODES = {
    "classification_runtime_disabled",
    "classification_external_model_disabled",
    "library_deleted",
    "document_deleted",
    "library_classification_auto_disabled",
    "library_external_llm_disabled",
    "library_classification_model_disabled",
    "security_allowlist_empty",
    "security_level_missing",
    "security_level_denied",
    "provider_config_invalid",
    "provider_credential_missing",
}


def normalize_classification_security_levels(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("classification security levels must be an array")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("each classification security level must be a string")
        item = item.strip()
        if not item or len(item) > 64:
            raise ValueError("classification security level is invalid")
        normalized.append(item)
    return sorted(set(normalized))


def validate_classification_scope(
    *,
    library,
    document,
    revision,
    auto_trigger: bool,
    config: Settings = settings,
) -> None:
    if not config.classification_runtime_enabled:
        fail_classification_runtime(
            "classification_runtime_disabled",
            "classification runtime is disabled",
        )
    if not config.classification_external_model_enabled:
        fail_classification_runtime(
            "classification_external_model_disabled",
            "classification model is disabled globally",
        )
    if getattr(library, "deleted_at", None) is not None:
        fail_classification_runtime("library_deleted", "Library is deleted")
    if auto_trigger and not getattr(library, "classification_auto_enabled", False):
        fail_classification_runtime(
            "library_classification_auto_disabled",
            "Library automatic classification is disabled",
        )
    if document.library_id != library.id or revision.library_id != library.id:
        fail_classification_runtime(
            "scope_mismatch",
            "document and Revision must belong to the Library",
        )
    if revision.document_id != document.id:
        fail_classification_runtime(
            "scope_mismatch",
            "Revision must belong to the document",
        )
    if getattr(document, "deleted_at", None) is not None:
        fail_classification_runtime("document_deleted", "document is deleted")
    if document.current_revision_id != revision.id:
        fail_classification_runtime(
            "historical_revision",
            "only the current Revision may be classified",
        )
    if document.status != "ready" or revision.status != "ready":
        fail_classification_runtime(
            "revision_not_ready",
            "current document Revision must be ready",
        )
    provider_name = classification_provider_name(config)
    if provider_name == "deepseek" and not getattr(library, "external_llm_enabled", False):
        fail_classification_runtime(
            "library_external_llm_disabled",
            "Library external LLM access is disabled",
        )
    if not getattr(library, "classification_external_model_enabled", False):
        fail_classification_runtime(
            "library_classification_model_disabled",
            "Library external classification is disabled",
        )
    allowed = getattr(library, "classification_allowed_security_levels", None)
    if not isinstance(allowed, list) or not allowed:
        fail_classification_runtime(
            "security_allowlist_empty",
            "Library classification security allowlist is empty",
        )
    security_level = getattr(revision, "security_level", None)
    if not isinstance(security_level, str) or not security_level.strip():
        fail_classification_runtime(
            "security_level_missing",
            "Revision security level is required",
        )
    if security_level.strip() not in allowed:
        fail_classification_runtime(
            "security_level_denied",
            "Revision security level is not allowed",
        )
    classification_provider_name(config)
    if not config.classification_api_key.get_secret_value().strip():
        fail_classification_runtime(
            "provider_credential_missing",
            "classification provider credential is missing",
        )
