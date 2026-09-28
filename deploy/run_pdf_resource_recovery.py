"""One bounded pass over an exact authorized snapshot; run in recovery image."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select

from app.config import settings
from app.db import async_session_factory
from app.models.document_import_job import DocumentImportJob
from app.models.file_resource import FileResource
from app.services.import_uploads import retry_job
from app.workers import importer


def select_jobs(inventory, scope):
    kinds = ('pixels', 'ocr_pages', 'total_pages')
    canaries = [min((r for r in inventory if r['kind'] == kind), key=lambda r: r['pages'])
                for kind in kinds]
    canary_ids = {r['job_id'] for r in canaries}
    if scope == 'canary-small':
        return canaries[:2]
    if scope == 'canary-long':
        return canaries[2:]
    return [r for r in inventory if r['job_id'] not in canary_ids]


async def run(scope, directory):
    directory = Path(directory)
    allowed = {uuid.UUID(v) for v in json.loads((directory / 'job_ids.json').read_text())}
    inventory = json.loads((directory / 'recovery_inventory.json').read_text())
    exclude, target, _ = importer.get_worker_target_config()
    if exclude or target is None or not settings.pdf_resource_recovery_enabled:
        raise RuntimeError('Recovery worker configuration invalid')
    if not (len(allowed) == 338 and len(inventory) == 36):
        raise RuntimeError('Unexpected recovery snapshot size')
    worker_id = f'{socket.gethostname()}-recovery-{os.getpid()}'
    audit_path = Path(os.environ.get('PDF_RECOVERY_AUDIT_DIR', str(directory))) / f'recovery-{scope}.jsonl'
    for record in select_jobs(inventory, scope):
        job_id = uuid.UUID(record['job_id'])
        if job_id not in allowed or not record.get('source_hash_verified'):
            raise RuntimeError('Job outside verified snapshot')
        async with async_session_factory() as db:
            async with db.begin():
                job = (await db.execute(select(DocumentImportJob).where(
                    DocumentImportJob.id == job_id,
                    DocumentImportJob.library_id == target,
                ).with_for_update())).scalar_one()
                if job.status == 'succeeded':
                    continue
                # One recovery attempt per original failure snapshot, never a retry loop.
                if job.status != 'failed' or job.current_stage != 'parsing' or job.attempt_count != record['attempt_count']:
                    raise RuntimeError('Job state changed since recovery snapshot')
                resource = await db.get(FileResource, job.file_resource_id)
                if resource is None or resource.storage_status != 'available' or resource.sha256 != record['sha256']:
                    raise RuntimeError('Original source identity changed')
                await retry_job(db, job=job)
                # Same transition as importer claim, while holding the exact job lock.
                job.status = 'processing'
                job.current_stage = 'validating'
                job.worker_id = worker_id
                job.claimed_at = datetime.now(timezone.utc)
                job.attempt_count += 1
        started = datetime.now(timezone.utc)
        outcome = {'job_id': str(job_id), 'kind': record['kind'], 'pages': record['pages'],
                   'started_at': started.isoformat(), 'worker_id': worker_id}
        try:
            await importer._process_claimed_job(job_id)
            outcome['processing_returned'] = True
        except Exception as exc:
            await importer._mark_failed(job_id, exc)
            outcome['error_type'] = type(exc).__name__
        outcome['finished_at'] = datetime.now(timezone.utc).isoformat()
        with audit_path.open('a') as stream:
            stream.write(json.dumps(outcome) + '\n')
        print(json.dumps({'event': 'recovery_attempt_finished', 'kind': record['kind'],
                          'error_type': outcome.get('error_type')}), flush=True)
        if 'error_type' in outcome:
            # A canary failure blocks expansion; inspect the new cause first.
            raise RuntimeError('Recovery attempt failed; inspect server-side evidence')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--scope', choices=['canary-small', 'canary-long', 'remaining'], required=True)
    parser.add_argument('--evidence-dir', required=True)
    args = parser.parse_args()
    asyncio.run(run(args.scope, args.evidence_dir))
