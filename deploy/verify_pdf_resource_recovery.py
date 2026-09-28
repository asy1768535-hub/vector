"""Read-only verification for the authorized PDF backlog; details stay on server.

Run as a module in an approved isolated recovery environment. PostgreSQL is
read-only, Qdrant uses metadata-only scroll, and stdout contains counts only.
No retry, queue mutation, source reading, or publication is performed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import quote

import httpx
from sqlalchemy import bindparam, text

from app.services.pdf_coverage import (
    PDF_COVERAGE_UNIT_KEY,
    PdfCoverageReportV1,
    pdf_coverage_from_document_blocks,
)

Bucket = Literal['complete', 'incomplete', 'processing', 'failed', 'unverified']
BUCKETS = ('complete', 'incomplete', 'processing', 'failed', 'unverified')
MAX_CHUNKS = 100_000


@dataclass(frozen=True)
class RecoveryEvidence:
    import_status: str
    document_status: str | None
    revision_status: str | None
    embedding_states: tuple[str, ...]
    source_available: bool
    current_revision_present: bool
    import_revision_matches: bool
    revision_stable: bool
    coverage: PdfCoverageReportV1
    chunk_count: int
    chunk_pages_valid: bool
    expected_pages_match: bool
    vectors: str


def classify_recovery_evidence(evidence: RecoveryEvidence) -> tuple[Bucket, str]:
    if evidence.import_status == 'failed':
        return 'failed', 'import_failed'
    if evidence.import_status in {'queued', 'processing', 'pending'}:
        return 'processing', 'import_active'
    if evidence.import_status != 'succeeded':
        return 'unverified', 'import_not_succeeded'
    if not evidence.revision_stable:
        return 'unverified', 'current_revision_changed'
    if not evidence.source_available:
        return 'unverified', 'source_unavailable_or_changed'
    if not evidence.current_revision_present or not evidence.import_revision_matches:
        return 'unverified', 'current_revision_unresolved'
    if evidence.document_status == 'failed' or evidence.revision_status == 'failed':
        return 'failed', 'publication_failed'
    if 'done' not in evidence.embedding_states:
        if {'pending', 'processing'} & set(evidence.embedding_states):
            return 'processing', 'current_embedding_active'
        if 'failed' in evidence.embedding_states:
            return 'failed', 'current_embedding_failed'
        return 'unverified', 'current_embedding_missing'
    if evidence.document_status != 'ready' or evidence.revision_status != 'ready':
        return 'unverified', 'publication_not_ready'
    if evidence.chunk_count == 0:
        return 'unverified', 'current_chunks_missing'
    if evidence.vectors == 'mismatch':
        return 'failed', 'current_vector_mismatch'
    if evidence.coverage['status'] == 'partial' or evidence.coverage['unprocessed_visual_pages']:
        return 'incomplete', 'unprocessed_visual_content'
    if not evidence.expected_pages_match:
        return 'unverified', 'original_page_count_mismatch'
    if evidence.coverage['status'] != 'complete':
        return 'unverified', 'coverage_unknown'
    if not evidence.chunk_pages_valid:
        return 'unverified', 'chunk_page_coverage_unresolved'
    if evidence.vectors != 'matched':
        return 'unverified', 'vectors_unverified'
    return 'complete', 'publication_and_coverage_verified'


def load_recovery_scope(directory: Path, scope: str) -> tuple[list[str], dict[str, dict]]:
    allowed = [str(uuid.UUID(value)) for value in json.loads((directory / 'job_ids.json').read_text())]
    inventory = json.loads((directory / 'recovery_inventory.json').read_text())
    if len(allowed) != 338 or len(set(allowed)) != 338 or len(inventory) != 36:
        raise ValueError('Unexpected snapshot size or duplicate IDs')
    indexed = {}
    for row in inventory:
        key = str(uuid.UUID(row['job_id']))
        if key not in allowed or key in indexed or row.get('source_hash_verified') is not True:
            raise ValueError('Unverified or duplicate inventory entry')
        if row['kind'] not in {'pixels', 'ocr_pages', 'total_pages'}:
            raise ValueError('Unknown recovery class')
        if type(row['pages']) is not int or not 1 <= row['pages'] <= 2000:
            raise ValueError('Invalid original page count')
        indexed[key] = row
    canaries = [min((row for row in indexed.values() if row['kind'] == kind),
                    key=lambda row: row['pages']) for kind in ('pixels', 'ocr_pages', 'total_pages')]
    if scope == 'all':
        return allowed, indexed
    if scope not in {'canary-small', 'canary-long'}:
        raise ValueError('Unknown verification scope')
    selected = canaries[:2] if scope == 'canary-small' else canaries[2:]
    return [str(uuid.UUID(row['job_id'])) for row in selected], indexed


async def verify_revision_vectors(client: httpx.AsyncClient, collection: str,
                                  identity: dict[str, str], chunk_ids: set[str]) -> str:
    """Check current-revision points; retained historical revisions are out of scope."""
    if not chunk_ids or len(chunk_ids) > MAX_CHUNKS:
        return 'unverified'
    seen_chunks: set[str] = set()
    seen_points: set[str] = set()
    seen_offsets: set[str] = set()
    offset = None
    for _ in range(len(chunk_ids) + 1):
        body = {
            'filter': {'must': [{'key': key, 'match': {'value': value}}
                                for key, value in identity.items()]},
            'limit': 256, 'with_vector': False,
            'with_payload': [*identity, 'chunk_id'],
        }
        if offset is not None:
            body['offset'] = offset
        response = await client.post(f'/collections/{quote(collection, safe="")}/points/scroll', json=body)
        response.raise_for_status()
        result = response.json()['result']
        for point in result['points']:
            payload = point.get('payload') or {}
            chunk_id = payload.get('chunk_id')
            point_id = str(point['id'])
            if (any(payload.get(key) != value for key, value in identity.items())
                    or chunk_id not in chunk_ids or chunk_id in seen_chunks or point_id in seen_points):
                return 'mismatch'
            seen_chunks.add(chunk_id)
            seen_points.add(point_id)
        offset = result.get('next_page_offset')
        if offset is None:
            return 'matched' if seen_chunks == chunk_ids else 'mismatch'
        if not result['points'] or str(offset) in seen_offsets:
            return 'unverified'
        seen_offsets.add(str(offset))
    return 'unverified'


async def verify_pdf_resource_recovery(directory: Path, audit_dir: Path, scope: str) -> dict:
    from app.config import settings
    from app.db import async_session_factory, get_engine

    selected, inventory = load_recovery_scope(directory, scope)
    target = uuid.UUID(os.environ['WORKER_TARGET_LIBRARY_ID'])
    # Suppress SQL/HTTP logging: query IDs, URLs and credentials must not reach stdout.
    import logging
    logging.disable(logging.CRITICAL)
    get_engine().echo = False
    counts = Counter({bucket: 0 for bucket in BUCKETS})
    reasons: Counter[str] = Counter()
    details = []
    headers = {'api-key': settings.qdrant_api_key} if settings.qdrant_api_key else {}
    query = text('''
        SELECT i.id::text AS job_id, i.status AS import_status, i.last_error,
               i.document_id::text AS document_id,
               i.document_revision_id::text AS import_revision_id,
               i.embedding_job_id IS NULL AS direct_embedding_missing,
               d.current_revision_id::text AS revision_id, d.status AS document_status,
               r.status AS revision_status, f.storage_status AS source_status, f.sha256,
               l.qdrant_collection AS collection, d.deleted_at IS NOT NULL AS deleted
        FROM document_import_jobs i
        LEFT JOIN documents d ON d.id=i.document_id AND d.library_id=i.library_id
        LEFT JOIN document_revisions r ON r.id=d.current_revision_id
             AND r.document_id=d.id AND r.library_id=i.library_id
        LEFT JOIN file_resources f ON f.id=i.file_resource_id AND f.library_id=i.library_id
        JOIN sys_libraries l ON l.id=i.library_id
        WHERE i.library_id=:target AND i.id IN :ids
    ''').bindparams(bindparam('ids', expanding=True))
    async with async_session_factory() as db:
        async with db.begin():
            await db.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            await db.execute(text("SET LOCAL statement_timeout = '15s'"))
            rows = (await db.execute(query, {'target': target, 'ids': [uuid.UUID(v) for v in selected]})).mappings().all()
            if {row['job_id'] for row in rows} != set(selected):
                raise ValueError('Snapshot jobs missing or outside the target library')
            for row in rows:
                item = dict(row)
                params = {'revision': uuid.UUID(row['revision_id']) if row['revision_id'] else None,
                          'document': uuid.UUID(row['document_id']) if row['document_id'] else None, 'target': target}
                embeds = (await db.execute(text('''SELECT status, last_error FROM embedding_jobs
                    WHERE library_id=:target AND document_id=:document AND document_revision_id=:revision'''), params)).mappings().all()
                item['embedding_states'] = tuple(embed['status'] for embed in embeds)
                item['embedding_errors'] = [embed['last_error'] for embed in embeds if embed['last_error']]
                chunks = (await db.execute(text('''SELECT id::text AS id, page_start, page_end FROM chunks
                    WHERE library_id=:target AND document_id=:document AND document_revision_id=:revision
                    ORDER BY seq LIMIT 100001'''), params)).mappings().all()
                if len(chunks) > MAX_CHUNKS:
                    raise ValueError('Chunk verification budget exceeded')
                item['chunks'] = [dict(chunk) for chunk in chunks]
                values = (await db.execute(text('''SELECT content->'parser_unit'->'value' AS report,
                    content->'parser_unit'->>'unit_kind' AS unit_kind,
                    content->'parser_unit'->>'source_kind' AS source_kind
                    FROM document_blocks WHERE library_id=:target AND document_id=:document
                    AND document_revision_id=:revision AND content->'parser_unit'->>'unit_key'=:unit_key
                    LIMIT 2'''), {**params, 'unit_key': PDF_COVERAGE_UNIT_KEY})).mappings().all()
                blocks = [{'content': {'parser_unit': {'unit_key': PDF_COVERAGE_UNIT_KEY,
                           'unit_kind': value['unit_kind'], 'source_kind': value['source_kind'],
                           'value': value['report']}}} for value in values]
                item['coverage'] = pdf_coverage_from_document_blocks(blocks if len(blocks) == 1 else ())
                details.append(item)
    async with httpx.AsyncClient(base_url=settings.qdrant_url.rstrip('/'), headers=headers,
                                timeout=httpx.Timeout(15, connect=5), follow_redirects=False) as client:
        for item in details:
            vectors = 'unverified'
            if item['import_status'] == 'succeeded' and item['revision_id'] and item['chunks']:
                identity = {'library_id': str(target), 'document_id': item['document_id'],
                            'document_revision_id': item['revision_id']}
                try:
                    vectors = await verify_revision_vectors(client, item['collection'], identity,
                                                            {chunk['id'] for chunk in item['chunks']})
                except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
                    item['vector_error_type'] = type(exc).__name__
            item['vectors'] = vectors
    async with async_session_factory() as db:
        async with db.begin():
            await db.execute(text('SET TRANSACTION READ ONLY'))
            await db.execute(text("SET LOCAL statement_timeout = '15s'"))
            fresh = (await db.execute(query, {'target': target, 'ids': [uuid.UUID(v) for v in selected]})).mappings().all()
    current = {row['job_id']: row for row in fresh}
    for item in details:
        newer = current.get(item['job_id'])
        stable = newer is not None and all(newer[key] == item[key] for key in
                 ('revision_id', 'import_revision_id', 'document_id', 'import_status',
                  'document_status', 'revision_status', 'deleted', 'source_status', 'sha256'))
        coverage = item['coverage']
        pages: set[int] = set()
        page_bounds_ok = True
        for chunk in item['chunks']:
            start, end = chunk['page_start'], chunk['page_end']
            if type(start) is not int or type(end) is not int or not 1 <= start <= end <= (coverage['total_pages'] or 0):
                page_bounds_ok = False
            else:
                pages.update(range(start, end + 1))
        original = inventory.get(item['job_id'])
        evidence = RecoveryEvidence(
            import_status=item['import_status'], document_status=item['document_status'],
            revision_status=item['revision_status'], embedding_states=item['embedding_states'],
            source_available=item['source_status'] == 'available' and (not original or original['sha256'] == item['sha256']),
            current_revision_present=bool(item['revision_id']) and not item['deleted'],
            import_revision_matches=item['import_revision_id'] in (None, item['revision_id']),
            revision_stable=stable, coverage=coverage, chunk_count=len(item['chunks']),
            chunk_pages_valid=page_bounds_ok and set(coverage['processed_pages']).issubset(pages),
            expected_pages_match=original is None or original['pages'] == coverage['total_pages'], vectors=item['vectors'])
        bucket, reason = classify_recovery_evidence(evidence)
        counts[bucket] += 1
        reasons[reason] += 1
        item.update(bucket=bucket, reason=reason, revision_stable=stable, chunk_page_coverage=sorted(pages),
                    missing_chunk_pages=sorted(set(coverage['processed_pages']) - pages))
        item.pop('collection')
    summary = {'scope': scope, 'total': len(selected), 'counts': dict(counts), 'reasons': dict(reasons),
               'direct_embedding_missing': sum(item['direct_embedding_missing'] for item in details),
               'text_accuracy': 'not_reviewed', 'database_mode': 'read_only'}
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    # Caller provides an existing server audit directory; never overwrite evidence.
    with (audit_dir / f'verification-{scope}-{stamp}.json').open('x', encoding='utf-8') as stream:
        json.dump({'checked_at': stamp, 'summary': summary, 'jobs': details}, stream, ensure_ascii=False, indent=2)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--evidence-dir', type=Path, required=True)
    parser.add_argument('--audit-dir', type=Path, required=True)
    parser.add_argument('--scope', choices=['all', 'canary-small', 'canary-long'], default='all')
    args = parser.parse_args()
    try:
        result = asyncio.run(verify_pdf_resource_recovery(args.evidence_dir, args.audit_dir, args.scope))
    except Exception as exc:
        print(json.dumps({'verification': 'blocked', 'error_type': type(exc).__name__}))
        raise SystemExit(1) from None
    print(json.dumps(result))
