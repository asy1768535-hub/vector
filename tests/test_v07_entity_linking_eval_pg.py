from __future__ import annotations

import asyncio
import os

import pytest

from eval.entity_linking.runtime import preflight_live_dependencies


REQUIRED_ENV = (
    "VECTOR_KB_PG_TEST_DSN",
    "QDRANT_URL",
    "EMBEDDING_BASE_URL",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIM",
)
MISSING = tuple(name for name in REQUIRED_ENV if not os.getenv(name))

pytestmark = pytest.mark.skipif(
    bool(MISSING),
    reason="BLOCKED: real PostgreSQL/Qdrant/Embedding environment is required; skip is not acceptance",
)


def test_v07_real_postgresql_qdrant_embedding_preflight_is_non_skipped():
    result = asyncio.run(preflight_live_dependencies())
    assert result["status"] == "passed"
    assert result["postgresql"] == "passed"
    assert result["qdrant"] == "passed"
    assert result["embedding"] == "passed"
    assert result["pg_trgm"] == "passed"
    assert len(result["pg_cluster_fingerprint_sha256"]) == 64
    assert len(result["qdrant_fingerprint_sha256"]) == 64
    assert len(result["embedding_fingerprint_sha256"]) == 64
