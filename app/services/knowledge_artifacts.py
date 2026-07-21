from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any, Mapping

from pydantic import BaseModel, ValidationError

from app.models.knowledge_artifact import KnowledgeArtifact
from app.models.knowledge_artifact_job import KnowledgeArtifactJob
from app.schemas.knowledge_artifact import OutlinePayloadV1, SummaryPayloadV1
from app.services.graph_canonical import canonical_graph_json_v1


ARTIFACT_CONTRACTS = {
    "summary": ("summary-v1", SummaryPayloadV1),
    "outline": ("outline-v1", OutlinePayloadV1),
}
GENERATION_MODES = {"deterministic", "model"}
TRIGGER_TYPES = {"revision_ready", "manual", "retry", "repair"}
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ArtifactContractError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def canonical_artifact_json_v1(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    try:
        return canonical_graph_json_v1(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactContractError(
            "invalid_canonical_json", "value cannot be represented as canonical JSON"
        ) from exc


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _require_hash(value: str, *, field: str) -> None:
    if not HASH_PATTERN.fullmatch(value):
        raise ArtifactContractError("invalid_hash", f"{field} must be a lowercase SHA-256 hash")


def _require_bounded_text(value: str, *, field: str, max_length: int) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ArtifactContractError(
            "invalid_identity_field",
            f"{field} must be nonblank and at most {max_length} characters",
        )


def _validate_contract(artifact_type: str, contract_version: str) -> type[BaseModel]:
    contract = ARTIFACT_CONTRACTS.get(artifact_type)
    if contract is None:
        raise ArtifactContractError(
            "unsupported_artifact_type", "artifact type is not supported"
        )
    expected_version, payload_model = contract
    if contract_version != expected_version:
        raise ArtifactContractError(
            "unsupported_contract_version", "artifact contract version is not supported"
        )
    return payload_model


def _validate_model_identity(
    generation_mode: str,
    model_provider: str | None,
    model_name: str | None,
    model_config_hash: str | None,
) -> None:
    if generation_mode not in GENERATION_MODES:
        raise ArtifactContractError(
            "invalid_generation_mode", "generation mode is not supported"
        )
    identity = (model_provider, model_name, model_config_hash)
    if generation_mode == "deterministic":
        if any(value is not None for value in identity):
            raise ArtifactContractError(
                "invalid_model_identity",
                "deterministic generation cannot include model identity",
            )
        return
    if any(value is None for value in identity):
        raise ArtifactContractError(
            "invalid_model_identity", "model generation requires complete model identity"
        )
    _require_bounded_text(model_provider or "", field="model_provider", max_length=64)
    _require_bounded_text(model_name or "", field="model_name", max_length=128)
    _require_hash(model_config_hash or "", field="model_config_hash")


def validate_artifact_payload(
    artifact_type: str,
    contract_version: str,
    payload: Mapping[str, Any] | BaseModel,
) -> SummaryPayloadV1 | OutlinePayloadV1:
    payload_model = _validate_contract(artifact_type, contract_version)
    raw_payload = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    try:
        return payload_model.model_validate(raw_payload)
    except ValidationError as exc:
        raise ArtifactContractError(
            "invalid_artifact_payload", "artifact payload does not match its contract"
        ) from exc


def artifact_payload_hash_v1(
    artifact_type: str,
    contract_version: str,
    payload: Mapping[str, Any] | BaseModel,
) -> str:
    validated = validate_artifact_payload(artifact_type, contract_version, payload)
    return _sha256(canonical_artifact_json_v1(validated))


def generation_input_fingerprint_v1(
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    revision_content_hash: str,
    artifact_type: str,
    contract_version: str,
    extractor_version: str,
    generation_mode: str,
    model_provider: str | None = None,
    model_name: str | None = None,
    model_config_hash: str | None = None,
) -> str:
    _require_hash(revision_content_hash, field="revision_content_hash")
    _require_bounded_text(extractor_version, field="extractor_version", max_length=64)
    _validate_model_identity(
        generation_mode, model_provider, model_name, model_config_hash
    )
    identity = {
        "identity_version": "knowledge-artifact-input-v1",
        "library_id": str(library_id),
        "document_id": str(document_id),
        "document_revision_id": str(document_revision_id),
        "revision_content_hash": revision_content_hash,
        "artifact_type": artifact_type,
        "contract_version": contract_version,
        "extractor_version": extractor_version,
        "generation_mode": generation_mode,
        "model_identity": None
        if generation_mode == "deterministic"
        else {
            "provider": model_provider,
            "name": model_name,
            "config_hash": model_config_hash,
        },
    }
    return _sha256(canonical_artifact_json_v1(identity))


def job_idempotency_key_v1(input_fingerprint: str, retry_generation: int) -> str:
    _require_hash(input_fingerprint, field="input_fingerprint")
    if not isinstance(retry_generation, int) or isinstance(retry_generation, bool) or retry_generation < 0:
        raise ArtifactContractError(
            "invalid_retry_generation", "retry generation must be a non-negative integer"
        )
    digest = _sha256(
        canonical_artifact_json_v1(
            {
                "identity_version": "knowledge-artifact-job-v1",
                "input_fingerprint": input_fingerprint,
                "retry_generation": retry_generation,
            }
        )
    )
    return f"knowledge-artifact-job-v1:{digest}"


def build_artifact_job(
    *,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    revision_content_hash: str,
    artifact_type: str,
    contract_version: str,
    extractor_version: str,
    generation_mode: str,
    trigger_type: str,
    model_provider: str | None = None,
    model_name: str | None = None,
    model_config_hash: str | None = None,
    retry_generation: int = 0,
    rerun_of_job_id: uuid.UUID | None = None,
    requested_by_user_id: uuid.UUID | None = None,
) -> KnowledgeArtifactJob:
    _validate_contract(artifact_type, contract_version)
    if trigger_type not in TRIGGER_TYPES:
        raise ArtifactContractError("invalid_trigger_type", "job trigger type is not supported")
    input_fingerprint = generation_input_fingerprint_v1(
        library_id=library_id,
        document_id=document_id,
        document_revision_id=document_revision_id,
        revision_content_hash=revision_content_hash,
        artifact_type=artifact_type,
        contract_version=contract_version,
        extractor_version=extractor_version,
        generation_mode=generation_mode,
        model_provider=model_provider,
        model_name=model_name,
        model_config_hash=model_config_hash,
    )
    idempotency_key = job_idempotency_key_v1(input_fingerprint, retry_generation)
    return KnowledgeArtifactJob(
        id=uuid.uuid4(),
        library_id=library_id,
        document_id=document_id,
        document_revision_id=document_revision_id,
        artifact_type=artifact_type,
        contract_version=contract_version,
        extractor_version=extractor_version,
        generation_mode=generation_mode,
        model_provider=model_provider,
        model_name=model_name,
        model_config_hash=model_config_hash,
        input_fingerprint=input_fingerprint,
        idempotency_key=idempotency_key,
        retry_generation=retry_generation,
        trigger_type=trigger_type,
        status="queued",
        rerun_of_job_id=rerun_of_job_id,
        requested_by_user_id=requested_by_user_id,
    )


def build_artifact(
    *,
    job: KnowledgeArtifactJob,
    library_id: uuid.UUID,
    document_id: uuid.UUID,
    document_revision_id: uuid.UUID,
    artifact_type: str,
    contract_version: str,
    extractor_version: str,
    input_fingerprint: str,
    payload: Mapping[str, Any] | BaseModel,
) -> KnowledgeArtifact:
    if (
        library_id,
        document_id,
        document_revision_id,
    ) != (
        job.library_id,
        job.document_id,
        job.document_revision_id,
    ):
        raise ArtifactContractError(
            "artifact_scope_mismatch", "artifact scope does not match its producing job"
        )
    if artifact_type != job.artifact_type:
        raise ArtifactContractError(
            "artifact_type_mismatch", "artifact type does not match its producing job"
        )
    if contract_version != job.contract_version:
        raise ArtifactContractError(
            "artifact_contract_mismatch", "artifact contract does not match its producing job"
        )
    if extractor_version != job.extractor_version:
        raise ArtifactContractError(
            "artifact_extractor_mismatch", "artifact extractor does not match its producing job"
        )
    if input_fingerprint != job.input_fingerprint:
        raise ArtifactContractError(
            "artifact_input_mismatch", "artifact input does not match its producing job"
        )

    validated = validate_artifact_payload(artifact_type, contract_version, payload)
    if validated.generation_mode != job.generation_mode:
        raise ArtifactContractError(
            "artifact_generation_mode_mismatch",
            "artifact generation mode does not match its producing job",
        )
    payload_json = validated.model_dump(mode="json")
    return KnowledgeArtifact(
        id=uuid.uuid4(),
        job_id=job.id,
        library_id=library_id,
        document_id=document_id,
        document_revision_id=document_revision_id,
        artifact_type=artifact_type,
        contract_version=contract_version,
        extractor_version=extractor_version,
        generation_mode=job.generation_mode,
        model_provider=job.model_provider,
        model_name=job.model_name,
        model_config_hash=job.model_config_hash,
        input_fingerprint=input_fingerprint,
        payload=payload_json,
        payload_hash=_sha256(canonical_artifact_json_v1(payload_json)),
        lifecycle_state="current",
    )
