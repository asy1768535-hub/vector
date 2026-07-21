from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from app.models.library import Library


CompatibilityChannel = Literal["text", "graph"]
VALID_CHANNELS = ("text", "graph")
COMPATIBILITY_CONTRACT_VERSION = "library-compatibility-v1"
EMBEDDING_PROFILE_VERSION = "embedding-v1"
EMBEDDING_PROBE_CONTRACT_VERSION = "embedding-probe-v1"
RETRIEVAL_PROFILE_VERSION = "retrieval-v1"
GRAPH_PROFILE_VERSION = "graph-v1"
PROBE_TEXTS = (
    "vector knowledge compatibility probe alpha 2026",
    "vector knowledge compatibility probe beta evidence graph",
)
REASON_ORDER = (
    "organization_mismatch",
    "library_index_unready",
    "embedding_verification_missing",
    "embedding_verification_stale",
    "embedding_profile_mismatch",
    "retrieval_profile_mismatch",
    "graph_channel_disabled",
    "graph_ontology_missing",
    "graph_ontology_ambiguous",
    "graph_schema_invalid",
    "graph_publication_missing",
    "graph_publication_unhealthy",
    "graph_profile_mismatch",
)


class LibraryCompatibilityError(RuntimeError):
    def __init__(self, code: str, message: str = "Library compatibility operation failed") -> None:
        self.code = code[:64]
        super().__init__(message[:255])


@dataclass(frozen=True, slots=True)
class CompatibilityFingerprint:
    contract_version: str
    fingerprint: str | None
    ready: bool
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class LibraryCompatibilityProfile:
    library: Library
    embedding: CompatibilityFingerprint
    retrieval: CompatibilityFingerprint
    graph: CompatibilityFingerprint | None


@dataclass(frozen=True, slots=True)
class LibraryIncompatibility:
    library_slug: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CompatibilityAssessment:
    organization_id: uuid.UUID
    channels: tuple[CompatibilityChannel, ...]
    profiles: tuple[LibraryCompatibilityProfile, ...]
    compatible: bool
    incompatibilities: tuple[LibraryIncompatibility, ...]


@dataclass(frozen=True, slots=True)
class EmbeddingVerificationSnapshot:
    contract_version: str
    model: str
    dimension: int
    endpoint_sha256: str
    probe_fingerprint: str
    verified_at: datetime
