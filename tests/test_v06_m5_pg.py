from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.graph_retrieval_eval import (
    assert_sanitized_graph_retrieval_artifact,
    load_graph_retrieval_eval_dataset,
)
from app.services.graph_retrieval_eval_runtime import (
    GRAPH_RETRIEVAL_EVAL_SAMPLES,
    build_graph_retrieval_calibration_artifact,
    collect_graph_retrieval_eval_run,
    create_graph_retrieval_eval_database,
    drop_graph_retrieval_eval_database,
    graph_retrieval_eval_database_exists,
    graph_retrieval_eval_uuid,
    seed_graph_retrieval_eval_dataset,
    upgrade_graph_retrieval_eval_database,
)


ROOT = Path(__file__).resolve().parents[1]
ADMIN_DSN = os.getenv("VECTOR_KB_PG_TEST_DSN")

pytestmark = pytest.mark.skipif(
    not ADMIN_DSN,
    reason="Set VECTOR_KB_PG_TEST_DSN to a disposable PostgreSQL admin DSN for v0.6 M5.",
)


async def _seed_second_database(dsn: str, dataset):
    engine = create_async_engine(dsn, pool_size=4, max_overflow=2)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    try:
        return await seed_graph_retrieval_eval_dataset(Session, dataset)
    finally:
        await engine.dispose()


def test_v06_m5_isolated_correctness_performance_and_cleanup():
    assert ADMIN_DSN is not None
    dataset = load_graph_retrieval_eval_dataset(
        repository_root=ROOT,
        manifest_path=ROOT / "eval/graph_retrieval/manifests/release_v1.json",
    )
    suffix = uuid.uuid4().hex[:8]
    first_name = f"vkt_v06_m5_eval_accept_{suffix}"
    second_name = f"vkt_v06_m5_eval_determinism_{suffix}"
    first_dsn = None
    second_dsn = None
    material = None
    first_seed_entity = None
    try:
        first_dsn = asyncio.run(
            create_graph_retrieval_eval_database(ADMIN_DSN, first_name)
        )
        upgrade_graph_retrieval_eval_database(ROOT, first_dsn)
        material = asyncio.run(collect_graph_retrieval_eval_run(first_dsn, dataset))
        first_seed_entity = graph_retrieval_eval_uuid("entity", "ent-010")

        assert material.correctness.passed_cases == 48
        assert material.correctness.failed_cases == 0
        assert len(material.correctness.response_hashes) == 48
        assert len(material.correctness.canonical_response_set_sha256) == 64
        assert material.query_state_unchanged is True
        assert material.timeout_rollback_reused is True
        assert material.disabled_compatibility_passed is True
        assert set(material.performance.latency) == {
            "one-hop-evidence-off",
            "one-hop-evidence-on",
            "two-hop-evidence-off",
            "two-hop-evidence-on",
            "high-degree-relation-truncation",
            "high-degree-node-truncation",
        }
        assert all(
            stats.sample_count == GRAPH_RETRIEVAL_EVAL_SAMPLES
            for stats in material.performance.latency.values()
        )
        assert set(material.performance.explain) == {
            "current-publication",
            "membership-items",
            "membership-entities",
            "membership-relations",
            "seed-id",
            "traversal",
            "fact-support",
            "evidence-locator",
        }

        second_dsn = asyncio.run(
            create_graph_retrieval_eval_database(ADMIN_DSN, second_name)
        )
        upgrade_graph_retrieval_eval_database(ROOT, second_dsn)
        second_seed = asyncio.run(_seed_second_database(second_dsn, dataset))
        assert second_seed.uuid_by_logical["ent-010"] == first_seed_entity
        assert second_seed.logical_by_uuid[str(first_seed_entity)] == "ent-010"
    finally:
        if first_dsn is not None:
            asyncio.run(drop_graph_retrieval_eval_database(ADMIN_DSN, first_name))
        if second_dsn is not None:
            asyncio.run(drop_graph_retrieval_eval_database(ADMIN_DSN, second_name))

    assert not asyncio.run(graph_retrieval_eval_database_exists(ADMIN_DSN, first_name))
    assert not asyncio.run(graph_retrieval_eval_database_exists(ADMIN_DSN, second_name))
    assert material is not None
    started = datetime(2026, 7, 16, 1, tzinfo=timezone.utc)
    artifact = build_graph_retrieval_calibration_artifact(
        run_id="m5-pg-synthetic-artifact",
        database_id=first_name,
        started_at=started,
        finished_at=started + timedelta(minutes=1),
        code_commit="a" * 40,
        implementation_tree_sha256="b" * 64,
        dataset=dataset,
        material=material,
        database_cleanup_succeeded=True,
    )
    assert artifact.status == "passed"
    assert_sanitized_graph_retrieval_artifact(artifact.model_dump(mode="json"))
