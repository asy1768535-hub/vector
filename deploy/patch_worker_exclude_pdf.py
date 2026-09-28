"""Patch a single worker image to defer PDF work when explicitly enabled."""

from __future__ import annotations

import sys
from pathlib import Path


worker = sys.argv[1]
target = Path(f"/app/app/workers/{worker}.py")
source = target.read_text(encoding="utf-8")

if worker == "importer":
    old_claim = """                  AND (
                    LOWER(file_name) NOT LIKE '%.doc'
                    OR conversion_sha256 IS NOT NULL
                  )
                ORDER BY created_at
"""
    new_claim = """                  AND (
                    LOWER(file_name) NOT LIKE '%.doc'
                    OR conversion_sha256 IS NOT NULL
                  )
                  AND (
                    NOT :exclude_pdf
                    OR LOWER(file_name) NOT LIKE '%.pdf'
                  )
                ORDER BY created_at
"""
    old_params = """            "max_attempts": settings.import_worker_max_attempts,
            "limit": limit,
            "worker_id": worker_id,
"""
    new_params = """            "max_attempts": settings.import_worker_max_attempts,
            "limit": limit,
            "worker_id": worker_id,
            "exclude_pdf": os.getenv("WORKER_EXCLUDE_PDF", "").strip().lower()
            in {"1", "true", "yes", "on"},
"""
elif worker == "embedder":
    old_claim = """            SELECT id FROM embedding_jobs
            WHERE status = 'pending'
              AND attempt_count < :max_attempts
            ORDER BY created_at
"""
    new_claim = """            SELECT j.id
            FROM embedding_jobs j
            JOIN documents d ON d.id = j.document_id
            WHERE j.status = 'pending'
              AND j.attempt_count < :max_attempts
              AND (
                NOT :exclude_pdf
                OR (
                    LOWER(COALESCE(d.source_path, '')) NOT LIKE '%.pdf'
                    AND LOWER(COALESCE(d.title, '')) NOT LIKE '%.pdf'
                )
              )
            ORDER BY j.created_at
"""
    old_params = """        {"limit": limit, "worker_id": worker_id, "max_attempts": settings.embed_worker_max_attempts},
"""
    new_params = """        {
            "limit": limit,
            "worker_id": worker_id,
            "max_attempts": settings.embed_worker_max_attempts,
            "exclude_pdf": os.getenv("WORKER_EXCLUDE_PDF", "").strip().lower()
            in {"1", "true", "yes", "on"},
        },
"""
else:
    raise SystemExit(f"unsupported worker: {worker}")

for old, new, label in (
    (old_claim, new_claim, "claim query"),
    (old_params, new_params, "claim parameters"),
):
    if source.count(old) != 1:
        raise SystemExit(f"expected {worker} {label} exactly once")
    source = source.replace(old, new)

target.write_text(source, encoding="utf-8")
