from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.schema_lifecycle_read import (
    CurrentOntologyError,
    resolve_current_ontology,
)
from app.services.schema_lifecycle_contracts import SchemaLifecycleCommand


LIBRARY_ID = uuid.UUID("10000000-0000-0000-0000-000000000001")
CURRENT_ID = uuid.UUID("20000000-0000-0000-0000-000000000001")


class _Result:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _DB:
    def __init__(self, value):
        self.value = value
        self.statement = None

    async def execute(self, statement):
        self.statement = statement
        return _Result(self.value)


def _library(**overrides):
    values = {
        "id": LIBRARY_ID,
        "current_ontology_version_id": CURRENT_ID,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ontology(*, library_id=LIBRARY_ID, status="active"):
    return SimpleNamespace(
        id=CURRENT_ID,
        library_id=library_id,
        status=status,
        version_key="ai-discovery",
        version_no=2,
    )


def test_resolve_current_ontology_uses_library_pointer_target():
    ontology = _ontology()
    db = _DB(ontology)

    result = asyncio.run(resolve_current_ontology(db, library=_library()))

    assert result is ontology
    assert "ontology_versions.id" in str(db.statement)
    assert "ontology_versions.library_id" in str(db.statement)


def test_resolve_current_ontology_fails_closed_without_pointer():
    db = _DB(None)

    with pytest.raises(CurrentOntologyError) as exc_info:
        asyncio.run(
            resolve_current_ontology(
                db,
                library=_library(current_ontology_version_id=None),
            )
        )

    assert exc_info.value.code == "current_ontology_missing"


@pytest.mark.parametrize(
    "ontology, expected_code",
    [
        (_ontology(library_id=uuid.UUID("30000000-0000-0000-0000-000000000001")), "current_ontology_invalid"),
        (_ontology(status="disabled"), "current_ontology_invalid"),
        (None, "current_ontology_invalid"),
    ],
)
def test_resolve_current_ontology_rejects_invalid_pointer_target(ontology, expected_code):
    with pytest.raises(CurrentOntologyError) as exc_info:
        asyncio.run(resolve_current_ontology(_DB(ontology), library=_library()))

    assert exc_info.value.code == expected_code


def test_library_current_ontology_pointer_contract_and_migration():
    from app.models.library import Library

    column = Library.__table__.c.current_ontology_version_id
    foreign_key = next(iter(column.foreign_keys))
    migration = Path("alembic/versions/0063_library_current_ontology.py").read_text(
        encoding="utf-8"
    )

    assert column.nullable is True
    assert foreign_key.target_fullname == "ontology_versions.id"
    assert foreign_key.ondelete == "RESTRICT"
    assert foreign_key.name == "fk_lib_current_ontology"
    assert 'revision: str = "0063"' in migration
    assert 'down_revision: Union[str, None] = "0062"' in migration
    assert "current_ontology_version_id" in migration
    assert "ai-exploration" in migration
    assert "ai-draft" in migration
    assert "count(*)" in migration


def test_activation_switches_current_pointer_with_ontology_status():
    from app.services import schema_lifecycle_actions as actions

    old_id = uuid.UUID("20000000-0000-0000-0000-000000000001")
    draft_id = uuid.UUID("20000000-0000-0000-0000-000000000002")
    library = SimpleNamespace(
        id=LIBRARY_ID,
        current_ontology_version_id=old_id,
    )
    old = SimpleNamespace(
        id=old_id,
        library_id=LIBRARY_ID,
        version_key="enterprise",
        status="active",
        origin="user",
        confirmed=True,
        published_at=None,
    )
    draft_version = SimpleNamespace(
        id=draft_id,
        library_id=LIBRARY_ID,
        version_key="enterprise",
        status="draft",
        origin="user",
        confirmed=False,
        published_at=None,
    )
    draft = SimpleNamespace(
        version=draft_version,
        entity_types=[],
        relation_types=[],
        attributes=[],
        constraints=[],
    )

    class _Rows:
        def scalars(self):
            return self

        def all(self):
            return [old, draft_version]

    class _DB:
        async def execute(self, _statement):
            return _Rows()

        async def flush(self):
            return None

    command = SchemaLifecycleCommand(
        library_id=LIBRARY_ID,
        ontology_version_id=draft_id,
        actor_user_id=None,
        action_kind="activate_version",
        target_kind="ontology_version",
        target_id=draft_id,
        expected_state_hash="a" * 64,
        idempotency_key="activate-current-test",
        payload={
            "confirmation": "activate_schema_version",
            "expected_active_version_id": str(old_id),
        },
    )

    async def _run():
        from unittest.mock import AsyncMock, patch

        with (
            patch.object(actions, "_lock_library", new=AsyncMock()),
            patch.object(actions, "_replay", new=AsyncMock(return_value=None)),
            patch.object(actions, "load_schema_version_bundle", new=AsyncMock(side_effect=[draft, draft])),
            patch.object(actions, "schema_version_state_hash", return_value="a" * 64),
            patch.object(actions, "validate_schema_draft_bundle", return_value=SimpleNamespace(valid=True)),
            patch.object(actions, "_record_action", new=AsyncMock(return_value=SimpleNamespace(id=uuid.uuid4()))),
        ):
            return await actions.activate_schema_version(_DB(), library, command)

    result = asyncio.run(_run())

    assert result.bundle.version.status == "active"
    assert old.status == "disabled"
    assert library.current_ontology_version_id == draft_id


def test_ai_discovery_draft_parents_the_current_ontology_at_creation():
    from unittest.mock import AsyncMock, patch

    from app.services.schema_discovery_runs import _create_draft_ontology

    current = _ontology()
    library = _library()

    class _MaxResult:
        def scalar_one(self):
            return 4

    class _DB:
        def __init__(self):
            self.added = []

        async def execute(self, _statement):
            return _MaxResult()

        def add(self, value):
            self.added.append(value)

        async def flush(self):
            return None

    async def _run():
        with patch(
            "app.services.schema_discovery_runs.resolve_current_ontology",
            new=AsyncMock(return_value=current),
        ):
            return await _create_draft_ontology(_DB(), library, "a" * 64)

    draft = asyncio.run(_run())

    assert draft.parent_version_id == current.id
    assert draft.version_key == "ai-discovery"
    assert draft.version_no == 5
