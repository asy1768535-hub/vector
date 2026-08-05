from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.models.graph_extraction_job import GraphExtractionJob
from app.models.graph_publication import GraphPublication
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
        return self.value[0] if isinstance(self.value, list) and self.value else self.value

    def all(self):
        if self.value is None:
            return []
        return self.value if isinstance(self.value, list) else [self.value]


class _Session:
    def __init__(
        self,
        *,
        job=None,
        library=None,
        existing=None,
        execute_results=None,
        publications=None,
    ) -> None:
        self.job = job
        self.library = library
        self.existing = existing
        self.execute_results = list(execute_results or [])
        self.publications = dict(publications or {})
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
        if model is not GraphPublication:
            return None
        return self.publications.get(_row_id)

    async def execute(self, _statement):
        self.execute_count += 1
        value = self.execute_results.pop(0) if self.execute_results else self.existing
        return _ScalarResult(value)

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
                "relation_count": 1,
                "relation_evidence_count": 1,
                "publishable_entity_candidate_count": 2,
                "publishable_relation_count": 1,
                "publishable_relation_evidence_count": 1,
            }
        },
    )


def _library():
    return SimpleNamespace(id=LIBRARY_ID, deleted_at=None)


def _publication(
    *,
    status="planned",
    publication_id=None,
    relation_count=1,
    entity_count=1,
    parent=None,
    entity_hashes=None,
    relation_hashes=None,
):
    return SimpleNamespace(
        id=publication_id or uuid.uuid4(),
        status=status,
        manifest_hash="a" * 64,
        relation_count=relation_count,
        entity_count=entity_count,
        parent_publication_id=parent.id if parent is not None else None,
        item_hashes_summary={
            "entity_hashes": entity_hashes or ["entity-new"],
            "relation_hashes": relation_hashes or ["relation-new"],
        },
    )


def _publication_items(publication, *, entity=None, relation=None):
    entity = entity or {"entity": "entity-new"}
    relation = relation or {"relation": "relation-new"}
    return [
        *[
            SimpleNamespace(
                publication_id=publication.id,
                item_kind="entity",
                entity_id=uuid.uuid5(uuid.NAMESPACE_URL, f"entity:{key}"),
                relation_id=None,
                item_hash=value,
            )
            for key, value in entity.items()
        ],
        *[
            SimpleNamespace(
                publication_id=publication.id,
                item_kind="relation",
                entity_id=None,
                relation_id=uuid.uuid5(uuid.NAMESPACE_URL, f"relation:{key}"),
                item_hash=value,
            )
            for key, value in relation.items()
        ],
    ]


@pytest.mark.parametrize("job_status", ["succeeded", "partially_succeeded"])
def test_auto_publish_activates_full_and_partial_success(job_status):
    job = _job(status=job_status)
    publication = _publication()
    planning_session = _Session(job=job, library=_library())
    activation_session = _Session(
        job=job,
        execute_results=[_publication_items(publication)],
    )
    sessions = _SessionFactory(planning_session, activation_session)
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
    assert activation_session.commits == 1
    assert job.statistics["publication"]["outcome"] == "activated"
    plan.assert_awaited_once()
    assert plan.await_args.kwargs["include_drafts"] is False
    assert job.statistics["publication"]["diff"] == {
        "entity": {"added": 1, "retained": 0, "changed": 0, "removed": 0},
        "relation": {"added": 1, "retained": 0, "changed": 0, "removed": 0},
    }
    assert plan.await_args.kwargs["include_drafts"] is False
    assert plan.await_args.kwargs["requested_by_user_id"] == USER_ID
    assert activate.await_args.kwargs["activated_by_user_id"] == USER_ID


def test_auto_publish_keeps_entity_only_candidates_without_publication():
    session = _Session(
        job=_job(
            counts={
                "entity_count": 0,
                "entity_mention_count": 0,
                "relation_count": 0,
                "relation_evidence_count": 0,
                "publishable_entity_candidate_count": 0,
                "publishable_relation_count": 0,
                "publishable_relation_evidence_count": 0,
                "pending_entity_candidate_count": 1,
                "outcome": "entities_only",
            }
        )
    )

    result = asyncio.run(
        auto_publish_graph_extraction_job(_SessionFactory(session), job_id=JOB_ID)
    )

    assert result.outcome == "skipped"
    assert session.execute_count == 2
    assert session.commits == 1
    assert session.job.statistics["publication"]["failure_reason"] == "no_valid_relation"
    assert session.job.statistics["publication"]["current_graph_unchanged"] is True


def test_entity_only_update_replaces_degraded_publication_when_library_remains_connected():
    job = _job(
        counts={
            "publishable_entity_candidate_count": 0,
            "publishable_relation_count": 0,
            "publishable_relation_evidence_count": 0,
            "pending_entity_candidate_count": 1,
            "outcome": "entities_only",
        }
    )
    degraded = _publication(status="degraded")
    replacement = _publication(relation_count=2)
    planning_session = _Session(
        job=job,
        library=_library(),
        execute_results=[None, degraded],
    )
    activation_session = _Session(job=job)
    plan = AsyncMock(
        return_value=SimpleNamespace(
            publication=replacement,
            manifest_hash=replacement.manifest_hash,
        )
    )
    activate = AsyncMock(return_value=SimpleNamespace(publication=replacement))

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
                _SessionFactory(planning_session, activation_session),
                job_id=JOB_ID,
            )
        )

    assert result.outcome == "activated"
    assert result.publication_id == replacement.id
    plan.assert_awaited_once()
    activate.assert_awaited_once()


def test_entity_only_update_does_not_activate_disconnected_replacement():
    job = _job(
        counts={
            "publishable_entity_candidate_count": 0,
            "publishable_relation_count": 0,
            "publishable_relation_evidence_count": 0,
            "pending_entity_candidate_count": 1,
            "outcome": "entities_only",
        }
    )
    degraded = _publication(status="degraded")
    empty_replacement = _publication(relation_count=0)
    session = _Session(
        job=job,
        library=_library(),
        execute_results=[None, degraded],
    )
    plan = AsyncMock(
        return_value=SimpleNamespace(
            publication=empty_replacement,
            manifest_hash=empty_replacement.manifest_hash,
        )
    )
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

    assert result.outcome == "skipped"
    assert session.rollbacks == 1
    assert session.commits == 1
    assert job.statistics["publication"]["failure_reason"] == "no_valid_relation"
    activate.assert_not_awaited()


def test_auto_publish_is_idempotent_after_publication_succeeds():
    publication = _publication(status="superseded")
    session = _Session(
        job=_job(),
        execute_results=[publication, _publication_items(publication)],
    )
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


def test_auto_publish_failure_preserves_materialization_and_records_reason():
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
    assert job.statistics["materialization"] == original_statistics["materialization"]
    assert job.statistics["publication"]["outcome"] == "failed"
    assert job.statistics["publication"]["failure_reason"] == "publication_failed"
    assert job.statistics["publication"]["current_graph_unchanged"] is True
    assert session.rollbacks == 1
    assert session.commits == 1


def test_auto_publish_records_snapshot_diff_against_parent():
    parent = _publication(
        status="degraded",
        entity_count=3,
        relation_count=2,
    )
    publication = _publication(
        parent=parent,
        entity_count=3,
        relation_count=2,
    )
    parent_items = _publication_items(
        parent,
        entity={"kept": "same", "changed": "old", "removed": "removed"},
        relation={"kept": "same", "removed": "removed"},
    )
    current_items = _publication_items(
        publication,
        entity={"kept": "same", "changed": "new", "added": "added"},
        relation={"kept": "same", "added": "added"},
    )
    job = _job()
    planning_session = _Session(job=job, library=_library())
    activation_session = _Session(
        job=job,
        publications={parent.id: parent},
        execute_results=[current_items + parent_items],
    )
    plan = AsyncMock(
        return_value=SimpleNamespace(
            publication=publication,
            manifest_hash=publication.manifest_hash,
        )
    )
    activate = AsyncMock(return_value=SimpleNamespace(publication=publication))

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
                _SessionFactory(planning_session, activation_session),
                job_id=JOB_ID,
            )
        )

    assert result.outcome == "activated"
    assert job.statistics["publication"]["diff"] == {
        "entity": {"added": 1, "retained": 1, "changed": 1, "removed": 1},
        "relation": {"added": 1, "retained": 1, "changed": 0, "removed": 1},
    }
