import assert from 'node:assert/strict';
import test from 'node:test';

import { parsingCoverageMessage } from './src/catalog_ui.js';

test('reports confirmed blank pages separately from unconfirmed and failed visual pages', () => {
    const message = parsingCoverageMessage({
        status: 'partial',
        total_pages: 5,
        processed_pages: [1, 5],
        confirmed_blank_pages: [2],
        unprocessed_visual_pages: [3, 4],
        page_assessments: [
            { page: 3, status: 'uncertain', reason: 'inspection_unavailable' },
            { page: 4, status: 'failed', reason: 'render_failed' },
        ],
        skipped_visual_block_count: 2,
        reasons: [],
    });
    assert.match(message, /确认空白页：2（1页）/);
    assert.match(message, /待确认视觉内容页：3/);
    assert.match(message, /处理异常页：4/);
    assert.doesNotMatch(message, /文字识别正确/);
});

test('complete coverage with blank pages explicitly is not a claim of text accuracy', () => {
    const message = parsingCoverageMessage({
        status: 'complete',
        total_pages: 3,
        processed_pages: [1, 3],
        confirmed_blank_pages: [2],
    });
    assert.match(message, /共3页/);
    assert.match(message, /确认空白页：2（1页）/);
    assert.match(message, /不代表文字识别准确/);
});

test('reports human-reviewed no-effective pages separately from automatic findings', () => {
    const message = parsingCoverageMessage({
        status: 'complete',
        total_pages: 3,
        processed_pages: [1],
        confirmed_blank_pages: [3],
        no_effective_content_pages: [2],
        page_assessments: [
            { page: 2, status: 'no_effective_content', classification_source: 'human_review' },
        ],
    });
    assert.match(message, /确认空白页：3/);
    assert.match(message, /人工复核无有效可提取内容页：2/);
    assert.doesNotMatch(message, /自动判定无有效可提取内容页/);
});

test('keeps legacy copy without blank-page extension', () => {
    assert.equal(parsingCoverageMessage({ status: 'complete', total_pages: 2 }), '已处理全部 2 页');
    assert.equal(parsingCoverageMessage({ status: 'partial', unprocessed_visual_pages: [2] }), '视觉未覆盖页：2');
});
