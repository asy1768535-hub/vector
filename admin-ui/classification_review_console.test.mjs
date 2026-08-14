import assert from 'node:assert/strict';
import test from 'node:test';

import {
    classificationReviewErrorKind,
    classificationReviewErrorMessage,
    formatReviewConfidence,
    initialReviewSelection,
    reviewCanAccept,
    reviewPageMatches,
    reviewProposalLabel,
    reviewReasonLabel,
    validateReviewSelection,
} from './src/classification_review_ui.js';
import {
    listClassificationReviews,
    reviewClassificationRun,
} from './src/api.js';

const labelA = { id: 'label-a', taxonomy_version_id: 'taxonomy-a', label: '合同' };
const labelB = { id: 'label-b', taxonomy_version_id: 'taxonomy-a', label: '财务' };
const page = {
    taxonomy_version_id: 'taxonomy-a',
    available_labels: [labelA, labelB],
    limit: 20,
    offset: 0,
    total: 1,
    items: [],
};
const run = {
    id: 'run-a',
    library_id: 'library-a',
    document_id: 'document-a',
    document_revision_id: 'revision-a',
    taxonomy_version_id: 'taxonomy-a',
    status: 'pending_review',
    effective_decisions: [],
    proposals: [
        { role: 'primary', rank: 0, label_id: 'label-a', label: '合同' },
        { role: 'secondary', rank: 0, label_id: 'label-b', label: '财务' },
    ],
};

test('accept requires one known proposal set from the current enabled taxonomy', () => {
    assert.equal(reviewCanAccept(run, page), true);
    assert.equal(reviewCanAccept({ ...run, taxonomy_version_id: 'old' }, page), false);
    assert.equal(reviewCanAccept({
        ...run,
        proposals: [{ role: 'primary', proposed_key: 'unknown', proposed_label: '未知' }],
    }, page), false);
    assert.equal(reviewCanAccept({
        ...run,
        proposals: [...run.proposals, { role: 'primary', label_id: 'label-b', label: '财务' }],
    }, page), false);
});
test('initial selection keeps only current unique enabled proposal labels', () => {
    assert.deepEqual(initialReviewSelection(run, page), {
        primaryLabelId: 'label-a',
        secondaryLabelIds: ['label-b'],
    });
    assert.deepEqual(initialReviewSelection({
        ...run,
        proposals: [
            { role: 'primary', label_id: 'old' },
            { role: 'secondary', label_id: 'label-b' },
            { role: 'secondary', label_id: 'label-b' },
        ],
    }, page), { primaryLabelId: '', secondaryLabelIds: ['label-b'] });
});

test('adjustment validation enforces enabled, unique, bounded roles', () => {
    assert.equal(validateReviewSelection('label-a', ['label-b'], page.available_labels), '');
    assert.match(validateReviewSelection('', [], page.available_labels), /主分类/);
    assert.match(validateReviewSelection('label-a', ['label-a'], page.available_labels), /同时/);
    assert.match(validateReviewSelection('label-a', ['missing'], page.available_labels), /启用范围/);
    assert.match(validateReviewSelection('label-a', Array(9).fill('label-b'), page.available_labels), /8/);
});

test('page identity and display projections are bounded', () => {
    assert.equal(reviewPageMatches({ ...page, items: [run] }, { limit: 20, offset: 0 }), true);
    assert.equal(reviewPageMatches({ ...page, items: [run], offset: 20 }, { limit: 20, offset: 0 }), false);
    assert.equal(reviewPageMatches({ ...page, items: [{ ...run, effective_decisions: null }] }, { limit: 20, offset: 0 }), false);
    assert.equal(reviewPageMatches({
        ...page,
        available_labels: [{ ...labelA, taxonomy_version_id: 'foreign-taxonomy' }],
    }, { limit: 20, offset: 0 }), false);
    assert.equal(reviewPageMatches({
        ...page,
        available_labels: [labelA, labelA],
    }, { limit: 20, offset: 0 }), false);
    assert.equal(formatReviewConfidence(850_000), '85.0%');
    assert.equal(formatReviewConfidence(1.5), '—');
    assert.equal(reviewProposalLabel({ proposed_label: '新分类' }), '新分类');
    assert.equal(reviewReasonLabel('provider secret with spaces'), '需要人工确认');
});

test('errors depend only on status classes', () => {
    assert.equal(classificationReviewErrorKind({ status: 409, body: { raw: 'secret' } }), 'conflict');
    assert.match(classificationReviewErrorMessage('conflict'), /重新加载/);
    assert.match(classificationReviewErrorMessage('unavailable'), /暂未启用/);
});

test('review API uses exact list query and fenced mutation body', async () => {
    const calls = [];
    const priorFetch = globalThis.fetch;
    globalThis.fetch = async (url, options = {}) => {
        calls.push({ url, options });
        return new Response(JSON.stringify({ ok: true }), {
            status: 200,
            headers: { 'content-type': 'application/json' },
        });
    };
    try {
        await listClassificationReviews('legal', { limit: 20, offset: 40, ignored: 'no' });
        await reviewClassificationRun('legal', 'run-a', {
            expected_run_status: 'pending_review',
            expected_effective_decision_set_id: null,
            action: 'reject',
            primary_label_id: null,
            secondary_label_ids: [],
        });
    } finally {
        globalThis.fetch = priorFetch;
    }
    assert.equal(calls[0].url, '/libraries/legal/classifications/reviews?limit=20&offset=40');
    assert.equal(calls[0].options.credentials, 'include');
    assert.equal(calls[1].url, '/libraries/legal/classifications/runs/run-a/review');
    assert.equal(calls[1].options.method, 'POST');
    assert.deepEqual(JSON.parse(calls[1].options.body), {
        expected_run_status: 'pending_review',
        expected_effective_decision_set_id: null,
        action: 'reject',
        primary_label_id: null,
        secondary_label_ids: [],
    });
});
