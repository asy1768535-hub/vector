from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

from app.services.graph_extraction_cache import (
    load_cached_graph_extraction_payload,
    normalize_cached_graph_extraction_payload,
)


def _payload() -> dict:
    return {"entities": [], "relations": []}


def test_cached_payload_normalization_accepts_single_and_batch_attempt_shapes():
    assert normalize_cached_graph_extraction_payload(_payload()) is not None
    assert normalize_cached_graph_extraction_payload({"batch_key": "u0", **_payload()}) is not None
    assert normalize_cached_graph_extraction_payload({"entities": "invalid"}) is None
    assert normalize_cached_graph_extraction_payload(None) is None


def test_cache_lookup_is_library_and_security_scoped():
    result = MagicMock()
    result.scalars.return_value.all.return_value = [_payload()]
    db = AsyncMock()
    db.execute.return_value = result
    library_id = uuid.uuid4()

    payload = asyncio.run(
        load_cached_graph_extraction_payload(
            db,
            cache_key="a" * 64,
            library_id=library_id,
            security_level="internal",
            visibility_scope="organization",
            current_unit_id=uuid.uuid4(),
        )
    )

    assert payload is not None
    statement = db.execute.await_args.args[0]
    compiled = statement.compile(
        dialect=postgresql.dialect(),
        compile_kwargs={"render_postcompile": True},
    )
    sql = str(compiled)
    assert "graph_extraction_jobs.library_id" in sql
    assert "document_revisions.security_level" in sql
    assert "document_revisions.visibility_scope" in sql
    assert library_id in compiled.params.values()
    assert "internal" in compiled.params.values()
    assert "organization" in compiled.params.values()
