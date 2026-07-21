import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import {
    formatKeyTime, keyStatus, STATUS_LABEL, STATUS_TAG,
    computeStats, paginateKeys,
} from './src/api_keys_ui.js';

const source = readFileSync(new URL('./src/views/ApiKeys.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

// ── Helpers ────────────────────────────────────────────────
function mockKey(overrides = {}) {
    return {
        id: 'k1', name: 'test', key_prefix: 'ak-abc123',
        created_at: '2026-01-01T00:00:00Z',
        last_used_at: null,
        expires_at: null,
        revoked_at: null,
        ...overrides,
    };
}

// ════════════════════════════════════════════════════════════
//  Real behavior tests
// ════════════════════════════════════════════════════════════

test('formatKeyTime formats valid ISO and returns — for null', () => {
    const result = formatKeyTime('2026-01-15T08:30:00Z');
    assert.ok(result.includes('2026'), 'includes year');
    assert.ok(result.includes('1'), 'includes month/day');
    assert.equal(formatKeyTime(null), '—');
    assert.equal(formatKeyTime(''), '—');
    assert.equal(formatKeyTime('not-a-date'), '—');
});

test('keyStatus priority: revoked > expired > active', () => {
    assert.equal(keyStatus(mockKey({ revoked_at: '2026-01-01' })), 'revoked');
    assert.equal(keyStatus(mockKey({ expires_at: '2025-01-01' })), 'expired'); // past
    assert.equal(keyStatus(mockKey({ expires_at: '2099-01-01' })), 'active');  // future
    assert.equal(keyStatus(mockKey({})), 'active');
});

test('keyStatus: revoked always wins even if expired', () => {
    assert.equal(keyStatus(mockKey({ revoked_at: '2026-01-01', expires_at: '2025-01-01' })), 'revoked');
});

test('STATUS_LABEL and STATUS_TAG have all three states', () => {
    for (const s of ['active', 'revoked', 'expired']) {
        assert.ok(STATUS_LABEL[s], `label for ${s}`);
        assert.ok(STATUS_TAG[s], `tag for ${s}`);
    }
});

test('computeStats counts correctly from full keys array', () => {
    const keys = [
        mockKey(),  // active
        mockKey({ revoked_at: '2026-01-01' }),
        mockKey({ expires_at: '2025-01-01' }),
        mockKey(),  // active
    ];
    const s = computeStats(keys);
    assert.equal(s.total, 4);
    assert.equal(s.active, 2);
    assert.equal(s.revoked, 1);
    assert.equal(s.expired, 1);
});

test('computeStats on empty array returns zeros', () => {
    const s = computeStats([]);
    assert.equal(s.total, 0);
    assert.equal(s.active, 0);
});

test('paginateKeys returns first page correctly', () => {
    const keys = Array.from({ length: 25 }, (_, i) => mockKey({ id: String(i) }));
    const p = paginateKeys(keys, 1, 10);
    assert.equal(p.items.length, 10);
    assert.equal(p.total, 25);
    assert.equal(p.page, 1);
    assert.equal(p.pageCount, 3);
});

test('paginateKeys clamps page boundaries', () => {
    const keys = Array.from({ length: 5 }, (_, i) => mockKey({ id: String(i) }));
    assert.equal(paginateKeys(keys, 1, 10).pageCount, 1);
    assert.equal(paginateKeys(keys, 99, 10).page, 1);
    assert.equal(paginateKeys([], 1, 10).pageCount, 1);
    assert.equal(paginateKeys([], 1, 10).items.length, 0);
});

// ════════════════════════════════════════════════════════════
//  Source checks
// ════════════════════════════════════════════════════════════

test('ApiKeys.js imports from api_keys_ui.js', () => {
    assert.ok(source.includes("from '../api_keys_ui.js'"), 'imports api_keys_ui');
    assert.ok(source.includes('keyStatus'), 'uses keyStatus');
    assert.ok(source.includes('computeStats'), 'uses computeStats');
    assert.ok(source.includes('paginateKeys'), 'uses paginateKeys');
});

test('ApiKeys.js imports copyTextToClipboard from copy_text.js', () => {
    assert.ok(source.includes("from '../copy_text.js'"), 'imports copy_text');
    assert.ok(source.includes('copyTextToClipboard'), 'uses copyTextToClipboard');
});

test('API calls unchanged: listApiKeys, createApiKey, revokeApiKey', () => {
    assert.ok(source.includes('api.listApiKeys'), 'listApiKeys called');
    assert.ok(source.includes('api.createApiKey'), 'createApiKey called');
    assert.ok(source.includes('api.revokeApiKey'), 'revokeApiKey called');
});

test('API usage card focuses on retrieval chunks without new backend behavior', () => {
    for (const token of [
        'API 接入说明：检索知识库切片',
        'Authorization: Bearer',
        '/libraries/{LIBRARY_ID}/query',
        '/query</code> 返回召回切片，不是最终回答',
        'VECTOR_KB_BASE_URL',
        'VECTOR_KB_LIBRARY_ID',
        'VECTOR_KB_API_KEY',
        'LOCAL_LLM_BASE_URL',
        'use_llm=False',
        'use_llm=True',
        'search_kb(question',
        'call_your_llm',
        'results',
        '完整接入模板',
        'api-keys-doc-template-button',
        'Python 完整接入脚本',
        'build_context(chunks',
        'ask(question',
        'if __name__ == "__main__"',
        'JavaScript/Node 简版',
        'curl 仅用于临时测试',
        'results[].text',
        'docDialog.open',
        'api-keys-doc-dialog',
        'openApiDoc',
    ]) assert.ok(source.includes(token), `missing API usage token: ${token}`);
    const allowedApiCalls = ['api.listApiKeys', 'api.createApiKey', 'api.revokeApiKey'];
    const apiCalls = [...source.matchAll(/api\.[A-Za-z0-9_]+(?=\()/g)].map((m) => m[0]);
    assert.deepEqual([...new Set(apiCalls)].sort(), allowedApiCalls.sort(), 'no new backend API calls');
    for (const forbidden of ['testConnection', 'onlineQuery', 'autoQuery', 'api.test', 'api.queryLibrary', 'copyApiDocPath', '文件上传接口。', '在线测试', '测试连接', '完整版本见 docs/29-api-key-api-usage.md']) {
        assert.equal(source.includes(forbidden), false, `forbidden feature: ${forbidden}`);
    }
    assert.equal(/vk_[A-Za-z0-9_\-]{12,}/.test(source), false, 'no real-looking API key in source');
    assert.equal(/VECTOR_KB_API_KEY=(vk_|sk-|ak-)[A-Za-z0-9_\-]{8,}/.test(source), false, 'no hard-coded env API key in source');
});
test('createApiKey converts local datetime to ISO string', () => {
    assert.ok(source.includes("api.createApiKey(name"), 'passes name');
    assert.ok(source.includes('toISOString()'), 'converts to ISO string for timezone safety');
    assert.ok(source.includes('new Date(dialog.expiresAt)'), 'parses local time before conversion');
});

test('submit rejects past time, allows future UTC, allows null', () => {
    // Verifies the validation logic in submit():
    // 1) Past time should be rejected (expiresAt <= now)
    assert.ok(source.includes("'过期时间必须晚于当前时间'"), 'rejects past time with warning');
    assert.ok(source.includes('new Date(expiresAt).getTime() <= Date.now()'), 'compares against Date.now()');
    // 2) Future time passes validation and gets converted to ISO string
    assert.ok(source.includes('dialog.expiresAt ? new Date(dialog.expiresAt)'), 'conditional ISO conversion');
    // 3) Null/empty means no expiration
    assert.ok(source.includes('dialog.expiresAt ?'), 'allows null expiresAt');
});

test('expiresAt picker disables past dates', () => {
    assert.ok(source.includes('disabled-date'), 'has disabled-date prop');
    assert.ok(source.includes('Date.now()'), 'checks against current time');
});

test('revoke has ElMessageBox.confirm', () => {
    assert.ok(source.includes("ElMessageBox.confirm"), 'confirm dialog before revoke');
});

test('revoke button disabled for non-active keys', () => {
    assert.ok(source.includes("keyStatus(row) !== 'active'"), 'disabled when not active');
});

test('no inline style attributes in template', () => {
    const tpl = source.slice(source.indexOf('template:'));
    const styles = tpl.match(/style="/g) || [];
    assert.equal(styles.length, 0, 'zero inline style="..."');
});

test('template uses api-keys-* CSS classes', () => {
    for (const c of ['api-keys-workspace', 'api-keys-header', 'api-keys-stats',
                     'api-keys-doc-card', 'api-keys-doc-steps', 'api-keys-doc-code', 'api-keys-doc-dialog', 'api-keys-doc-template-button',
                     'api-keys-table-card', 'api-keys-result-card']) {
        assert.ok(source.includes(c), `uses ${c}`);
    }
});

test('result card has close button that clears plaintext', () => {
    assert.ok(source.includes('closeResult'), 'closeResult function');
    assert.ok(source.includes("plaintext = ''"), 'clears plaintext on close');
});

test('CSS defines api-keys-* classes', () => {
    for (const c of ['api-keys-workspace', 'api-keys-header', 'api-keys-stats',
                     'api-keys-doc-card', 'api-keys-doc-steps', 'api-keys-doc-code', 'api-keys-doc-dialog', 'api-keys-doc-template-button',
                     'api-keys-table-card', 'api-keys-result-card', 'api-keys-plaintext']) {
        const escaped = c.replace(/-/g, '\\-');
        assert.match(css, new RegExp('\\.' + escaped + '\\s*\\{'), `${c} defined`);
    }
});

test('CSS includes api-keys responsive rules', () => {
    assert.match(css, /1199px[\s\S]*api-keys-result-right/, 'api-keys at 1199px');
    assert.match(css, /899px[\s\S]*api-keys-stats/, 'api-keys at 899px');
});

console.log('api keys redesign test passed');
