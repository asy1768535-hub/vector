"""Reconcile historical failed processing chains that a later upload replaced."""

from __future__ import annotations

import argparse
import asyncio
import json

from sqlalchemy import text

from app.db import async_session_factory


_CANDIDATES = """
WITH candidates AS (
    SELECT f.id, f.embedding_job_id, f.document_revision_id
    FROM document_import_jobs AS f
    WHERE f.requested_by_user_id IS NOT NULL
      AND (
          f.status = 'failed'
          OR (
              f.status = 'processing'
              AND f.current_stage IN ('embedding', 'graph')
              AND (
                  EXISTS (
                      SELECT 1 FROM embedding_jobs AS e
                      WHERE e.id = f.embedding_job_id AND e.status = 'failed'
                  )
                  OR EXISTS (
                      SELECT 1 FROM graph_extraction_jobs AS g
                      WHERE g.document_revision_id = f.document_revision_id
                        AND g.status = 'failed'
                  )
              )
          )
      )
      AND EXISTS (
          SELECT 1
          FROM document_import_jobs AS s
          WHERE s.library_id = f.library_id
            AND s.requested_by_user_id = f.requested_by_user_id
            AND s.replace_document_id IS NOT DISTINCT FROM f.replace_document_id
            AND s.status = 'succeeded'
            AND s.created_at > f.created_at
            AND (
                (
                    f.relative_path IS NOT NULL
                    AND s.relative_path = f.relative_path
                    AND s.file_name = f.file_name
                )
                OR (
                    f.relative_path IS NULL
                    AND f.external_id IS NOT NULL
                    AND s.relative_path IS NULL
                    AND s.external_id = f.external_id
                )
                OR (
                    f.relative_path IS NULL
                    AND f.external_id IS NULL
                    AND s.relative_path IS NULL
                    AND s.external_id IS NULL
                    AND s.file_name = f.file_name
                )
            )
      )
)
"""

_DRY_RUN = _CANDIDATES + """
SELECT
    (SELECT count(*) FROM candidates) AS import_jobs,
    (SELECT count(DISTINCT c.embedding_job_id)
     FROM candidates AS c
     JOIN embedding_jobs AS e ON e.id = c.embedding_job_id
     WHERE e.status = 'failed') AS embedding_jobs,
    (SELECT count(DISTINCT g.id)
     FROM graph_extraction_jobs AS g
     WHERE g.status = 'failed'
       AND g.document_revision_id IN (
           SELECT c.document_revision_id
           FROM candidates AS c
           WHERE c.document_revision_id IS NOT NULL
       )) AS graph_jobs
"""

_APPLY = _CANDIDATES + """
, cancelled_imports AS (
    UPDATE document_import_jobs AS job
    SET status = 'cancelled', worker_id = NULL, claimed_at = NULL, finished_at = NOW()
    FROM candidates AS c
    WHERE job.id = c.id
    RETURNING job.id
), superseded_embeddings AS (
    UPDATE embedding_jobs AS job
    SET status = 'superseded', worker_id = NULL, claimed_at = NULL, finished_at = NOW()
    WHERE job.status = 'failed'
      AND job.id IN (
          SELECT c.embedding_job_id
          FROM candidates AS c
          WHERE c.embedding_job_id IS NOT NULL
      )
    RETURNING job.id
), cancelled_graphs AS (
    UPDATE graph_extraction_jobs AS job
    SET status = 'cancelled', finished_at = NOW()
    WHERE job.status = 'failed'
      AND job.document_revision_id IN (
          SELECT c.document_revision_id
          FROM candidates AS c
          WHERE c.document_revision_id IS NOT NULL
      )
    RETURNING job.id
)
SELECT
    (SELECT count(*) FROM cancelled_imports) AS import_jobs,
    (SELECT count(*) FROM superseded_embeddings) AS embedding_jobs,
    (SELECT count(*) FROM cancelled_graphs) AS graph_jobs
"""


async def main(apply: bool) -> None:
    async with async_session_factory() as db:
        result = await db.execute(text(_APPLY if apply else _DRY_RUN))
        row = result.mappings().one()
        if apply:
            await db.commit()
    print(json.dumps({"mode": "apply" if apply else "dry_run", **row}, sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.apply))
