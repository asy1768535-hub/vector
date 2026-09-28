"""Synthetic-only tests; no production originals, IDs, databases or providers."""
from dataclasses import replace
import json
import uuid

import httpx
import pytest

from app.services.pdf_coverage import create_pdf_coverage_report
from deploy.verify_pdf_resource_recovery import (
    RecoveryEvidence,
    classify_recovery_evidence,
    load_recovery_scope,
    verify_revision_vectors,
)


@pytest.fixture
def complete_evidence():
    return RecoveryEvidence(
        import_status='succeeded', document_status='ready', revision_status='ready',
        embedding_states=('done',), source_available=True, current_revision_present=True,
        import_revision_matches=True, revision_stable=True, chunk_count=2,
        chunk_pages_valid=True, expected_pages_match=True, vectors='matched',
        coverage=create_pdf_coverage_report(status='complete', total_pages=2, processed_pages=[1, 2]))


def test_complete_uses_current_embedding_not_direct_link(complete_evidence):
    # Direct import-to-embedding linkage is intentionally not a success criterion.
    assert classify_recovery_evidence(complete_evidence)[0] == 'complete'


@pytest.mark.parametrize(('changes', 'bucket', 'reason'), [
    ({'import_status': 'failed'}, 'failed', 'import_failed'),
    ({'import_status': 'processing'}, 'processing', 'import_active'),
    ({'import_status': 'cancelled'}, 'unverified', 'import_not_succeeded'),
    ({'revision_stable': False}, 'unverified', 'current_revision_changed'),
    ({'source_available': False}, 'unverified', 'source_unavailable_or_changed'),
    ({'current_revision_present': False}, 'unverified', 'current_revision_unresolved'),
    ({'import_revision_matches': False}, 'unverified', 'current_revision_unresolved'),
    ({'document_status': 'failed'}, 'failed', 'publication_failed'),
    ({'embedding_states': ()}, 'unverified', 'current_embedding_missing'),
    ({'embedding_states': ('failed',)}, 'failed', 'current_embedding_failed'),
    ({'embedding_states': ('processing',)}, 'processing', 'current_embedding_active'),
    ({'document_status': 'pending'}, 'unverified', 'publication_not_ready'),
    ({'chunk_count': 0}, 'unverified', 'current_chunks_missing'),
    ({'vectors': 'mismatch'}, 'failed', 'current_vector_mismatch'),
    ({'vectors': 'unverified'}, 'unverified', 'vectors_unverified'),
    ({'expected_pages_match': False}, 'unverified', 'original_page_count_mismatch'),
    ({'chunk_pages_valid': False}, 'unverified', 'chunk_page_coverage_unresolved'),
])
def test_no_false_success(complete_evidence, changes, bucket, reason):
    assert classify_recovery_evidence(replace(complete_evidence, **changes)) == (bucket, reason)


def test_ready_is_not_full_content_proof(complete_evidence):
    partial = create_pdf_coverage_report(status='partial', total_pages=2, processed_pages=[1],
        unprocessed_visual_pages=[2], skipped_visual_block_count=1, reasons=['visual_content_without_ocr'])
    assert classify_recovery_evidence(replace(complete_evidence, coverage=partial)) == (
        'incomplete', 'unprocessed_visual_content')
    unknown = create_pdf_coverage_report(status='unknown', total_pages=None)
    assert classify_recovery_evidence(replace(complete_evidence, coverage=unknown)) == (
        'unverified', 'coverage_unknown')


@pytest.fixture
def snapshot(tmp_path):
    ids = [str(uuid.UUID(int=index + 1)) for index in range(338)]
    rows = [{'job_id': ids[index], 'kind': ('pixels', 'ocr_pages', 'total_pages')[index % 3],
             'pages': index + 1, 'source_hash_verified': True, 'sha256': 'a' * 64}
            for index in range(36)]
    (tmp_path / 'job_ids.json').write_text(json.dumps(ids))
    (tmp_path / 'recovery_inventory.json').write_text(json.dumps(rows))
    return tmp_path, ids, rows


def test_scope_matches_existing_canary_selection(snapshot):
    directory, ids, _ = snapshot
    assert load_recovery_scope(directory, 'all')[0] == ids
    assert load_recovery_scope(directory, 'canary-small')[0] == ids[:2]
    assert load_recovery_scope(directory, 'canary-long')[0] == ids[2:3]


@pytest.mark.parametrize('problem', ['duplicate_id', 'outside_snapshot', 'unverified_hash',
                                     'duplicate_inventory', 'invalid_pages', 'invalid_kind'])
def test_snapshot_rejects_unsafe_inputs(snapshot, problem):
    directory, ids, rows = snapshot
    if problem == 'duplicate_id':
        ids[-1] = ids[0]
    elif problem == 'outside_snapshot':
        rows[0]['job_id'] = str(uuid.UUID(int=9999))
    elif problem == 'unverified_hash':
        rows[0]['source_hash_verified'] = False
    elif problem == 'duplicate_inventory':
        rows[1] = rows[0]
    elif problem == 'invalid_pages':
        rows[0]['pages'] = 2001
    else:
        rows[0]['kind'] = 'unknown'
    (directory / 'job_ids.json').write_text(json.dumps(ids))
    (directory / 'recovery_inventory.json').write_text(json.dumps(rows))
    with pytest.raises(ValueError):
        load_recovery_scope(directory, 'all')


@pytest.mark.asyncio
async def test_vector_scroll_checks_all_pages_and_avoids_text():
    identity = {'library_id': 'library', 'document_id': 'document', 'document_revision_id': 'revision'}
    requests = []

    def respond(request):
        body = json.loads(request.content)
        requests.append(body)
        assert request.method == 'POST'
        assert request.url.path == '/collections/test/points/scroll'
        assert body['with_vector'] is False
        assert set(body['with_payload']) == {*identity, 'chunk_id'}
        assert body['filter'] == {'must': [{'key': key, 'match': {'value': value}}
                                           for key, value in identity.items()]}
        second = body.get('offset') == 'next'
        return httpx.Response(200, json={'result': {
            'points': [{'id': 'point2' if second else 'point1',
                        'payload': {**identity, 'chunk_id': 'two' if second else 'one'}}],
            'next_page_offset': None if second else 'next'}})

    async with httpx.AsyncClient(base_url='https://example.invalid', transport=httpx.MockTransport(respond)) as client:
        assert await verify_revision_vectors(client, 'test', identity, {'one', 'two'}) == 'matched'
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize('problem', ['missing', 'wrong_revision', 'duplicate_chunk', 'extra_chunk', 'duplicate_point'])
async def test_vector_mismatch_is_never_success(problem):
    identity = {'library_id': 'library', 'document_id': 'document', 'document_revision_id': 'revision'}
    points = [{'id': 'p1', 'payload': {**identity, 'chunk_id': 'one'}},
              {'id': 'p2', 'payload': {**identity, 'chunk_id': 'two'}}]
    if problem == 'missing':
        points.pop()
    elif problem == 'wrong_revision':
        points[1]['payload']['document_revision_id'] = 'old-revision'
    elif problem == 'duplicate_chunk':
        points[1]['payload']['chunk_id'] = 'one'
    elif problem == 'extra_chunk':
        points[1]['payload']['chunk_id'] = 'unexpected'
    else:
        points[1]['id'] = 'p1'
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'result': {'points': points, 'next_page_offset': None}}))
    async with httpx.AsyncClient(base_url='https://example.invalid', transport=transport) as client:
        assert await verify_revision_vectors(client, 'test', identity, {'one', 'two'}) == 'mismatch'


@pytest.mark.asyncio
async def test_empty_nonterminal_page_is_unverified():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'result': {'points': [], 'next_page_offset': 'again'}}))
    async with httpx.AsyncClient(base_url='https://example.invalid', transport=transport) as client:
        assert await verify_revision_vectors(client, 'test', {'document_id': 'doc'}, {'one'}) == 'unverified'


@pytest.mark.asyncio
@pytest.mark.parametrize('change', [None, 'source_status', 'sha256', 'unit_kind', 'source_kind'])
async def test_readonly_orchestration_and_stability(snapshot, monkeypatch, change):
    from types import SimpleNamespace
    import logging
    from app.config import settings
    from app.models.library import Library
    from app import db as app_db
    from deploy import verify_pdf_resource_recovery as verifier

    directory, ids, _ = snapshot
    target = uuid.UUID(int=2000)
    rows = []
    chunks_by_revision = {}
    for index, job_id in enumerate(ids[:2]):
        revision = str(uuid.UUID(int=3000 + index))
        rows.append({'job_id': job_id, 'import_status': 'succeeded',
            'last_error': 'SYNTHETIC_PRIVATE_DETAIL', 'document_id': str(uuid.UUID(int=4000 + index)),
            'import_revision_id': revision if index else None, 'direct_embedding_missing': index == 0,
            'revision_id': revision, 'document_status': 'ready', 'revision_status': 'ready',
            'source_status': 'available', 'sha256': 'a' * 64, 'collection': 'test', 'deleted': False})
        chunks_by_revision[revision] = [{'id': str(uuid.UUID(int=5000 + index * 10 + page)),
            'page_start': page, 'page_end': page} for page in range(1, index + 2)]
    statements = []
    sessions = []

    class Result:
        def __init__(self, values):
            self.values = values

        def mappings(self):
            return self

        def all(self):
            return self.values

    class Session:
        def __init__(self):
            self.number = len(sessions)
            self.read_only = False
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def begin(self):
            return self

        async def execute(self, statement, params=None):
            sql = str(statement).strip()
            statements.append(sql)
            if sql.startswith('SET TRANSACTION'):
                assert 'READ ONLY' in sql
                self.read_only = True
                return Result([])
            assert self.read_only
            if sql.startswith('SET LOCAL statement_timeout'):
                return Result([])
            assert sql.startswith('SELECT')
            assert params['target'] == target
            assert ':target' in sql
            if 'FROM document_import_jobs' in sql:
                assert f'JOIN {Library.__tablename__} l' in sql
                assert set(params['ids']) == {uuid.UUID(value) for value in ids[:2]}
                result = [dict(row) for row in rows]
                if self.number == 1 and change in {'source_status', 'sha256'}:
                    result[0][change] = 'changed'
                return Result(result)
            revision = str(params['revision'])
            assert 'document_revision_id=:revision' in sql
            if 'FROM embedding_jobs' in sql:
                return Result([{'status': 'done', 'last_error': None}])
            if 'FROM chunks' in sql:
                return Result(chunks_by_revision[revision])
            assert 'FROM document_blocks' in sql
            count = len(chunks_by_revision[revision])
            value = {'report': create_pdf_coverage_report(status='complete', total_pages=count,
                processed_pages=list(range(1, count + 1))), 'unit_kind': 'structured_unit', 'source_kind': 'pdf'}
            if revision == rows[0]['revision_id'] and change in {'unit_kind', 'source_kind'}:
                value[change] = 'not_pdf_coverage'
            return Result([value])

    def respond(request):
        body = json.loads(request.content)
        assert body['with_vector'] is False
        identity = {part['key']: part['match']['value'] for part in body['filter']['must']}
        assert set(identity) == {'library_id', 'document_id', 'document_revision_id'}
        points = [{'id': chunk['id'], 'payload': {**identity, 'chunk_id': chunk['id']}}
                  for chunk in chunks_by_revision[identity['document_revision_id']]]
        return httpx.Response(200, json={'result': {'points': points, 'next_page_offset': None}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(respond)))
    monkeypatch.setattr(app_db, 'async_session_factory', Session)
    monkeypatch.setattr(app_db, 'get_engine', lambda: SimpleNamespace(echo=False))
    monkeypatch.setattr(logging, 'disable', lambda level: None)
    monkeypatch.setenv('WORKER_TARGET_LIBRARY_ID', str(target))
    monkeypatch.setattr(settings, 'qdrant_url', 'https://example.invalid')
    monkeypatch.setattr(settings, 'qdrant_api_key', '')
    summary = await verifier.verify_pdf_resource_recovery(directory, directory, 'canary-small')
    assert summary['counts']['complete'] == (2 if change is None else 1)
    assert summary['counts']['unverified'] == (0 if change is None else 1)
    assert summary['direct_embedding_missing'] == 1
    assert sum(summary['counts'].values()) == 2
    assert 'SYNTHETIC_PRIVATE_DETAIL' not in json.dumps(summary)
    assert not any(value in json.dumps(summary) for value in ids)
    report = json.loads(next(directory.glob('verification-*.json')).read_text())
    assert len(report['jobs']) == 2
    assert report['jobs'][0]['last_error'] == 'SYNTHETIC_PRIVATE_DETAIL'
    assert all(session.read_only for session in sessions)
    assert len(sessions) == 2
    assert statements
