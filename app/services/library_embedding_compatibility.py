from __future__ import annotations

import hashlib
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Sequence

from app.config import settings
from app.models.library import Library
from app.services import audit_log, embedding
from app.services.graph_canonical import canonical_graph_value_hash_v1
from app.services.library_compatibility_contracts import (
    EMBEDDING_PROBE_CONTRACT_VERSION,
    EMBEDDING_PROFILE_VERSION,
    PROBE_TEXTS,
    CompatibilityFingerprint,
    EmbeddingVerificationSnapshot,
    LibraryCompatibilityError,
)


HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def effective_embedding_endpoint(library: Library) -> str:
    return (library.embedding_base_url or settings.embedding_base_url).strip()


def endpoint_sha256(library: Library) -> str:
    return _sha256_text(effective_embedding_endpoint(library))


def canonical_probe_fingerprint(
    vectors: Sequence[Sequence[float]],
    *,
    model: str,
    expected_dimension: int,
) -> str:
    if len(vectors) != len(PROBE_TEXTS):
        raise LibraryCompatibilityError("embedding_probe_shape_invalid")
    canonical_vectors: list[list[str]] = []
    for vector in vectors:
        if len(vector) != expected_dimension:
            raise LibraryCompatibilityError("embedding_probe_dimension_mismatch")
        canonical_vector: list[str] = []
        for raw_value in vector:
            if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
                raise LibraryCompatibilityError("embedding_probe_value_invalid")
            value = float(raw_value)
            if not math.isfinite(value):
                raise LibraryCompatibilityError("embedding_probe_value_invalid")
            if value == 0:
                value = 0.0
            canonical_vector.append(f"{value:.8f}")
        canonical_vectors.append(canonical_vector)
    return canonical_graph_value_hash_v1(
        {
            "contract_version": EMBEDDING_PROBE_CONTRACT_VERSION,
            "model": model,
            "probe_texts": list(PROBE_TEXTS),
            "vectors": canonical_vectors,
        }
    )


async def verify_library_embedding_profile(
    db,
    *,
    library: Library,
    actor_user_id: uuid.UUID,
    at: datetime | None = None,
) -> EmbeddingVerificationSnapshot:
    try:
        vectors = await embedding.embed_texts(
            PROBE_TEXTS,
            model=library.embedding_model,
            base_url=library.embedding_base_url,
        )
    except Exception as exc:
        raise LibraryCompatibilityError("embedding_probe_failed") from exc
    fingerprint = canonical_probe_fingerprint(
        vectors,
        model=library.embedding_model,
        expected_dimension=library.embedding_dim,
    )
    verified_at = at or datetime.now(timezone.utc)
    endpoint_hash = endpoint_sha256(library)
    library.embedding_probe_contract_version = EMBEDDING_PROBE_CONTRACT_VERSION
    library.embedding_probe_model = library.embedding_model
    library.embedding_probe_dimension = library.embedding_dim
    library.embedding_probe_endpoint_sha256 = endpoint_hash
    library.embedding_probe_fingerprint = fingerprint
    library.embedding_probe_verified_at = verified_at
    await audit_log.record(
        db,
        actor_user_id,
        "library.embedding_profile_verify",
        {
            "library_id": str(library.id),
            "contract_version": EMBEDDING_PROBE_CONTRACT_VERSION,
            "model": library.embedding_model,
            "dimension": library.embedding_dim,
            "probe_fingerprint": fingerprint,
        },
    )
    return EmbeddingVerificationSnapshot(
        contract_version=EMBEDDING_PROBE_CONTRACT_VERSION,
        model=library.embedding_model,
        dimension=library.embedding_dim,
        endpoint_sha256=endpoint_hash,
        probe_fingerprint=fingerprint,
        verified_at=verified_at,
    )


def build_embedding_profile(library: Library) -> CompatibilityFingerprint:
    snapshot = (
        library.embedding_probe_contract_version,
        library.embedding_probe_model,
        library.embedding_probe_dimension,
        library.embedding_probe_endpoint_sha256,
        library.embedding_probe_fingerprint,
        library.embedding_probe_verified_at,
    )
    if all(value is None for value in snapshot):
        return CompatibilityFingerprint(
            EMBEDDING_PROFILE_VERSION,
            None,
            False,
            "embedding_verification_missing",
        )
    fresh = (
        all(value is not None for value in snapshot)
        and library.embedding_probe_contract_version == EMBEDDING_PROBE_CONTRACT_VERSION
        and library.embedding_probe_model == library.embedding_model
        and library.embedding_probe_dimension == library.embedding_dim
        and library.embedding_probe_endpoint_sha256 == endpoint_sha256(library)
        and isinstance(library.embedding_probe_fingerprint, str)
        and HASH_PATTERN.fullmatch(library.embedding_probe_fingerprint) is not None
    )
    if not fresh:
        return CompatibilityFingerprint(
            EMBEDDING_PROFILE_VERSION,
            None,
            False,
            "embedding_verification_stale",
        )
    fingerprint = canonical_graph_value_hash_v1(
        {
            "contract_version": EMBEDDING_PROFILE_VERSION,
            "model": library.embedding_probe_model,
            "probe_fingerprint": library.embedding_probe_fingerprint,
            "dimension": library.embedding_probe_dimension,
            "distance": library.vector_distance,
            "normalization": "provider_output_v1",
            "input_contract": "plain_text_utf8_v1",
        }
    )
    return CompatibilityFingerprint(EMBEDDING_PROFILE_VERSION, fingerprint, True)
