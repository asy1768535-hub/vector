from __future__ import annotations

import asyncio

from app.workers import embedder, importer


class _EmptyClaimResult:
    def __iter__(self):
        return iter(())

    def all(self):
        return []


class _ClaimDb:
    def __init__(self) -> None:
        self.statement = None
        self.params = None

    async def execute(self, statement, params):
        self.statement = statement
        self.params = params
        return _EmptyClaimResult()

    async def commit(self) -> None:
        return None


def test_import_worker_excludes_pdf_jobs_when_requested(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "1")
    db = _ClaimDb()

    assert asyncio.run(importer._claim_jobs(db, "test-worker", 3)) == []
    assert db.params["exclude_pdf"] is True
    assert "LOWER(file_name) NOT LIKE '%.pdf'" in str(db.statement)


def test_embedding_worker_excludes_pdf_jobs_when_requested(monkeypatch) -> None:
    monkeypatch.setenv("WORKER_EXCLUDE_PDF", "true")
    db = _ClaimDb()

    assert asyncio.run(embedder._claim_jobs(db, "test-worker", 3)) == []
    assert db.params["exclude_pdf"] is True
    assert "LOWER(COALESCE(d.source_path, '')) NOT LIKE '%.pdf'" in str(db.statement)
