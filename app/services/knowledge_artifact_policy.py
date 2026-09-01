from __future__ import annotations

import hashlib
from dataclasses import dataclass

from app.config import Settings, settings
from app.models.document_revision import DocumentRevision
from app.models.knowledge_artifact_job import KnowledgeArtifactJob
from app.services.knowledge_artifact_provider import (
    OPENAI_COMPATIBLE_PROVIDER_NAME,
)
from app.services.knowledge_artifacts import (
    ArtifactContractError,
    canonical_artifact_json_v1,
    generation_input_fingerprint_v1,
)


SUPERSEDED_ERROR_CODES = {
    "historical_revision",
    "revision_not_ready",
    "revision_content_changed",
    "job_identity_stale",
    "job_input_stale",
}
CANCELLED_ERROR_CODES = {
    "knowledge_artifact_runtime_disabled",
    "library_deleted",
    "document_deleted",
    "library_artifact_auto_disabled",
    "library_artifact_type_disabled",
    "external_model_disabled",
    "library_external_llm_disabled",
    "library_external_model_disabled",
    "security_allowlist_empty",
    "security_level_missing",
    "security_level_denied",
    "provider_config_invalid",
    "provider_credential_missing",
}


class KnowledgeArtifactRuntimeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class GenerationSpec:
    artifact_type: str
    contract_version: str
    extractor_version: str
    generation_mode: str
    model_provider: str | None
    model_name: str | None
    model_config_hash: str | None


def fail_artifact_runtime(code: str, message: str) -> None:
    raise KnowledgeArtifactRuntimeError(code, message)


def normalize_knowledge_artifact_security_levels(value: object) -> list[str]:
    if not isinstance(value, list):
        raise ValueError("knowledge artifact security levels must be an array")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("each knowledge artifact security level must be a string")
        item = item.strip()
        if not item:
            raise ValueError("knowledge artifact security level must not be empty")
        if len(item) > 64:
            raise ValueError("knowledge artifact security level is too long")
        normalized.append(item)
    return sorted(set(normalized))


def validate_enqueue_scope(
    *,
    library,
    document,
    revision,
    auto_trigger: bool,
    config: Settings = settings,
) -> None:
    if not config.knowledge_artifact_runtime_enabled:
        fail_artifact_runtime(
            "knowledge_artifact_runtime_disabled",
            "knowledge artifact runtime is disabled",
        )
    if getattr(library, "deleted_at", None) is not None:
        fail_artifact_runtime("library_deleted", "Library is deleted")
    if auto_trigger and not getattr(library, "knowledge_artifact_auto_enabled", False):
        fail_artifact_runtime(
            "library_artifact_auto_disabled",
            "Library automatic artifacts are disabled",
        )
    if document.library_id != library.id or revision.library_id != library.id:
        fail_artifact_runtime(
            "scope_mismatch", "document and revision must belong to the Library"
        )
    if revision.document_id != document.id:
        fail_artifact_runtime(
            "scope_mismatch", "revision must belong to the document"
        )
    if getattr(document, "deleted_at", None) is not None:
        fail_artifact_runtime("document_deleted", "document is deleted")
    if document.current_revision_id != revision.id:
        fail_artifact_runtime(
            "historical_revision", "only the current Revision may produce artifacts"
        )
    if document.status != "ready" or revision.status != "ready":
        fail_artifact_runtime(
            "revision_not_ready", "current document Revision must be ready"
        )


def _model_config_hash(config: Settings) -> str:
    identity = {
        "provider": OPENAI_COMPATIBLE_PROVIDER_NAME,
        "base_url": config.graph_extraction_base_url,
        "model": config.graph_extraction_model,
        "prompt_version": config.knowledge_artifact_prompt_version,
        "max_source_chars": config.knowledge_artifact_model_max_source_chars,
        "temperature": 0,
        "response_format": "json_object",
    }
    return hashlib.sha256(
        canonical_artifact_json_v1(identity).encode("utf-8")
    ).hexdigest()


def select_generation_spec(
    *,
    library,
    revision,
    artifact_type: str,
    source_character_count: int,
    config: Settings = settings,
) -> GenerationSpec:
    if not config.knowledge_artifact_runtime_enabled:
        fail_artifact_runtime(
            "knowledge_artifact_runtime_disabled",
            "knowledge artifact runtime is disabled",
        )
    if artifact_type == "outline":
        if not getattr(library, "outline_artifact_enabled", False):
            fail_artifact_runtime(
                "library_artifact_type_disabled", "Outline artifacts are disabled"
            )
        contract_version = "outline-v1"
        extractor_version = config.knowledge_artifact_outline_extractor_version
    elif artifact_type == "summary":
        if not getattr(library, "summary_artifact_enabled", False):
            fail_artifact_runtime(
                "library_artifact_type_disabled", "Summary artifacts are disabled"
            )
        contract_version = "summary-v1"
        extractor_version = config.knowledge_artifact_summary_extractor_version
    else:
        fail_artifact_runtime(
            "unsupported_artifact_type", "artifact type is not supported"
        )
    if source_character_count <= 0:
        fail_artifact_runtime("artifact_source_empty", "Artifact source is empty")
    if not config.knowledge_artifact_external_model_enabled:
        fail_artifact_runtime(
            "external_model_disabled", "external artifact model is disabled globally"
        )
    if not getattr(library, "external_llm_enabled", False):
        fail_artifact_runtime(
            "library_external_llm_disabled",
            "Library external LLM access is disabled",
        )
    if not getattr(library, "knowledge_artifact_external_model_enabled", False):
        fail_artifact_runtime(
            "library_external_model_disabled",
            "Library external artifact model is disabled",
        )
    allowed = getattr(library, "knowledge_artifact_allowed_security_levels", None)
    if not isinstance(allowed, list) or not allowed:
        fail_artifact_runtime(
            "security_allowlist_empty", "Library artifact security allowlist is empty"
        )
    security_level = getattr(revision, "security_level", None)
    if not isinstance(security_level, str) or not security_level.strip():
        fail_artifact_runtime(
            "security_level_missing", "Revision security level is required"
        )
    if security_level.strip() not in allowed:
        fail_artifact_runtime(
            "security_level_denied", "Revision security level is not allowed"
        )
    if (
        not config.graph_extraction_base_url.strip()
        or not config.graph_extraction_model.strip()
    ):
        fail_artifact_runtime(
            "provider_config_invalid", "artifact model endpoint and model are required"
        )
    if not config.graph_extraction_api_key.get_secret_value().strip():
        fail_artifact_runtime(
            "provider_credential_missing", "artifact provider credential is missing"
        )
    return GenerationSpec(
        artifact_type=artifact_type,
        contract_version=contract_version,
        extractor_version=extractor_version,
        generation_mode="model",
        model_provider=OPENAI_COMPATIBLE_PROVIDER_NAME,
        model_name=config.graph_extraction_model,
        model_config_hash=_model_config_hash(config),
    )


def require_current_job_identity(
    job: KnowledgeArtifactJob,
    *,
    revision: DocumentRevision,
    spec: GenerationSpec,
) -> None:
    current_identity = (
        spec.contract_version,
        spec.extractor_version,
        spec.generation_mode,
        spec.model_provider,
        spec.model_name,
        spec.model_config_hash,
    )
    stored_identity = (
        job.contract_version,
        job.extractor_version,
        job.generation_mode,
        job.model_provider,
        job.model_name,
        job.model_config_hash,
    )
    if current_identity != stored_identity:
        fail_artifact_runtime(
            "job_identity_stale", "knowledge artifact Job identity is no longer active"
        )
    try:
        expected_fingerprint = generation_input_fingerprint_v1(
            library_id=job.library_id,
            document_id=job.document_id,
            document_revision_id=job.document_revision_id,
            revision_content_hash=revision.content_hash,
            artifact_type=job.artifact_type,
            contract_version=job.contract_version,
            extractor_version=job.extractor_version,
            generation_mode=job.generation_mode,
            model_provider=job.model_provider,
            model_name=job.model_name,
            model_config_hash=job.model_config_hash,
        )
    except ArtifactContractError as exc:
        raise KnowledgeArtifactRuntimeError(
            "job_identity_stale", "knowledge artifact Job identity is no longer valid"
        ) from exc
    if expected_fingerprint != job.input_fingerprint:
        fail_artifact_runtime(
            "job_input_stale", "knowledge artifact Job input fingerprint is stale"
        )
