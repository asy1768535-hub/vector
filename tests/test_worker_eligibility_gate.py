"""#6 步骤4：Worker embed 前资格门控——旧 revision 的 job 应直接 superseded，不调 embedding。"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

from app.workers import embedder


def test_stale_revision_job_superseded_without_embedding():
    lib = NS(id=uuid.uuid4(), index_state="ready", active_rebuild_operation_id=None, deleted_at=None)
    doc = NS(id=uuid.uuid4(), current_revision=2, deleted_at=None)   # 当前已是 rev 2
    job = NS(id=uuid.uuid4(), library_id=lib.id, document_id=doc.id,
             document_revision=1, rebuild_operation_id=None)          # 本 job 是旧 rev 1

    async def fake_get(model, _id):
        return {embedder.Library: lib, embedder.Document: doc}.get(model)

    db = MagicMock()
    db.get = AsyncMock(side_effect=fake_get)
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    with patch.object(embedder.embedding, "embed_texts", new_callable=AsyncMock) as embed:
        asyncio.run(embedder._process_job(db, job))
        embed.assert_not_called()                 # 关键：旧 revision 不浪费 embedding

    # 标了 superseded（一次 update + commit）
    assert db.execute.await_count >= 1
    db.commit.assert_awaited()
