from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.graph_extraction_job import GraphExtractionJob
from app.models.library import Library
from app.services.graph_extraction_auto_publication import (
    GraphExtractionAutoPublicationError,
    auto_publish_graph_extraction_job,
)
from app.services.graph_publication_activation import GraphPublicationActivationError
from app.services.graph_publication_planner import GraphPublicationPlanError


JOB_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
LIBRARY_ID = uuid.UUID("20000000-0000-0000-0000-000000000002")
ONTOLOGY_ID = uuid.UUID("30000000-0000-0000-0000-000000000003")
USER_ID = uuid.UUID("40000000-0000-0000-0000-000000000004")


class _ScalarResult:
    def __init__(self, value=None) -> None:
        self.value = value

    def scalars(self):
        return self

    def first(self):
        return self.value


class _Session:
    def __init__(self, *, job=None, library=None, existing=None) -> None:
        self.job = job
        self.library = library
        self.existing = existing
        self.commits = 0
        self.rollbacks = 0
        self.execute_count = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def get(self, model, _row_id):
        if model is GraphExtractionJob:
            return self.job
        if model is Library:
            return self.library
        return None

    async def execute(self, _statement):
        self.execute_count += 1
        return _ScalarResult(self.existing)

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1


class _SessionFactory:
    def __init__(self, *sessions) -> None:
        self.sessions = list(sessions)

    def __call__(self):
        return self.sessions.pop(0)


def _job(*, status="succeeded", counts=None):
    return SimpleNamespace(
        id=JOB_ID,
        library_id=LIBRARY_ID,
        ontology_version_id=ONTOLOGY_ID,
        requested_by=USER_ID,
        execution_mode="production",
        status=status,
        statistics={
            "materialization": counts
            if counts is not None
            else {
                "entity_count": 1,
                "entity_mention_count": 1,
                "relation_count": 0,
                "relation_evidence_count": 0,
            }
        },
    )


def _library():
    return SimpleNamespace(id=LIBRARY_ID, deleted_at=None)


def _publication(*, status="planned", publication_id=None):
    return SimpleNamespace(
        id=publication_id or uuid.uuid4(),
        status=status,
        manifest_hash="a" * 64,
    )


@pytest.mark.parametrize("job_status", ["succeeded", "partially_succeeded"])
def test_auto_publish_activates_full_and_partial_success(job_status):
    job = _job(status=job_status)
    publication = _publication()
    planning_session = _Session(job=job, library=_library())
    sessions = _SessionFactory(planning_session, _Session())
    plan = AsyncMock(
        return_value=SimpleNamespace(
            publication=publication,
            manifest_hash=publication.manifest_hash,
        )
    )
    activate = AsyncMock(
        return_value=SimpleNamespace(publication=publication)
    )

    with (
        patch(
            "app.services.graph_extraction_auto_publication.plan_graph_publication",
            plan,
        ),
        patch(
            "app.services.graph_extraction_auto_publication.activate_graph_publication",
            activate,
        ),
    ):
        result = asyncio.run(
            auto_publish_graph_extraction_job(sessions, job_id=JOB_ID)
        )

    assert result.outcome == "activated"
    assert result.publication_id == publication.id
    assert planning_session.commits == 1
    assert plan.await_args.kwargs["include_drafts"] is True
    assert plan.await_args.kwargs["requested_by_user_id"] == USER_ID
    assert activate.await_args.kwargs["activated_by_user_id"] == USER_ID


def test_auto_publish_skips_job_without_materialized_effect():
    session = _Session(
        job=_job(
            counts={
                "entity_count": 0,
                "entity_mention_count": 0,
                "relation_count": 0,
                "relation_evidence_count": 0,
            }
        )
    )

    result = asyncio.run(
        auto_publish_graph_extraction_job(_SessionFactory(session), job_id=JOB_ID)
    )

    assert result.outcome == "skipped"
    assert session.execute_count == 0


def test_auto_publish_is_idempotent_after_publication_succeeds():
    publication = _publication(status="superseded")
    session = _Session(job=_job(), existing=publication)
    plan = AsyncMock()
    activate = AsyncMock()

    with (
        patch(
            "app.services.graph_extraction_auto_publication.plan_graph_publication",
            plan,
        ),
        patch(
            "app.services.graph_extraction_auto_publication.activate_graph_publication",
            activate,
        ),
    ):
        result = asyncio.run(
            auto_publish_graph_extraction_job(
                _SessionFactory(session),
                job_id=JOB_ID,
            )
        )

    assert result.outcome == "already_published"
    assert result.publication_id == publication.id
    plan.assert_not_awaited()
    activate.assert_not_awaited()


def test_auto_publish_replans_when_parent_publication_changes():
    first = _publication()
    second = _publication()
    sessions = _SessionFactory(
        _Session(job=_job(), library=_library()),
        _Session(),
        _Session(job=_job(), library=_library()),
        _Session(),
    )
    plan = AsyncMock(
        side_effect=[
            SimpleNamespace(publication=first, manifest_hash=first.manifest_hash),
            SimpleNamespace(publication=second, manifest_hash=second.manifest_hash),
        ]
    )
    activate = AsyncMock(
        side_effect=[
            GraphPublicationActivationError(
                "publication_parent_changed", "current publication changed"
            ),
            SimpleNamespace(publication=second),
        ]
    )

    with (
        patch(
            "app.services.graph_extraction_auto_publication.plan_graph_publication",
            plan,
        ),
        patch(
            "app.services.graph_extraction_auto_publication.activate_graph_publication",
            activate,
        ),
    ):
        result = asyncio.run(
            auto_publish_graph_extraction_job(sessions, job_id=JOB_ID)
        )

    assert result.outcome == "activated"
    assert result.publication_id == second.id
    assert plan.await_count == 2
    assert activate.await_count == 2


def test_auto_publish_failure_does_not_mutate_materialized_job():
    job = _job(status="partially_succeeded")
    original_statistics = job.statistics.copy()
    session = _Session(job=job, library=_library())

    with patch(
        "app.services.graph_extraction_auto_publication.plan_graph_publication",
        new=AsyncMock(
            side_effect=GraphPublicationPlanError(
                "publication_failed", "publication failed"
            )
        ),
    ):
        with pytest.raises(GraphExtractionAutoPublicationError) as error:
            asyncio.run(
                auto_publish_graph_extraction_job(
                    _SessionFactory(session),
                    job_id=JOB_ID,
                )
            )

    assert error.value.code == "publication_failed"
    assert job.status == "partially_succeeded"
    assert job.statistics == original_statistics
    assert session.rollbacks == 1
