import assert from 'node:assert/strict';
import test from 'node:test';
import { registerHooks } from 'node:module';
import { readFileSync } from 'node:fs';

let instance = 0;
const dataModule = source => `data:text/javascript,${encodeURIComponent(source)}`;
const vueUrl = new URL('./vendor/vue.esm-browser.prod.js', import.meta.url).href;
const key = (overrides = {}) => ({ id: 'key-a', name: 'example', key_prefix: 'vk_example',
    organization_id: 'org-a', revoked_at: null, expires_at: null, ...overrides });
const plain = 'test-only-secret-for-clipboard-1234';

async function component(t, overrides = {}) {
    const harness = { copied: [], messages: [], revoked: [], refreshes: [],
        async confirm() {},
        api: {
            async listApiKeys(force) { harness.refreshes.push(force); return [key()]; },
            async createApiKey() { return { ...key(), plaintext_key: plain }; },
            async revokeApiKey(id) { harness.revoked.push(id); },
            ...overrides,
        },
    };
    globalThis.__KEY_VISIBILITY_TEST__ = harness;
    const stubs = new Map([
        ['vue', dataModule(`export * from ${JSON.stringify(vueUrl)}; export const onMounted = () => {};`)],
        ['element-plus', dataModule(`
            const h = () => globalThis.__KEY_VISIBILITY_TEST__;
            export const ElMessage = Object.fromEntries(['success','warning','error'].map(k => [k,m => h().messages.push([k,m])]));
            export const ElMessageBox = { confirm: (...a) => h().confirm(...a) };
        `)],
        ['../api.js', dataModule(`const h = () => globalThis.__KEY_VISIBILITY_TEST__.api;
            ${['listApiKeys','createApiKey','revokeApiKey'].map(n => `export const ${n} = (...a) => h().${n}(...a);`).join('\n')}`)],
        ['../copy_text.js', dataModule(`export async function copyTextToClipboard(value) { globalThis.__KEY_VISIBILITY_TEST__.copied.push(value); }`)],
        ['../store.js', dataModule('export const store = { organizations: [] };')],
    ]);
    const hooks = registerHooks({ resolve(specifier, context, nextResolve) {
        if (context.parentURL?.includes('/views/ApiKeys.js') && stubs.has(specifier)) {
            return { url: stubs.get(specifier), shortCircuit: true };
        }
        return nextResolve(specifier, context);
    } });
    t.after(() => { hooks.deregister(); delete globalThis.__KEY_VISIBILITY_TEST__; });
    const { default: ApiKeys } = await import(`./src/views/ApiKeys.js?visibility-test=${++instance}`);
    return { view: ApiKeys.setup(), harness, template: ApiKeys.template };
}

test('revoked keys are excluded from rows, pagination and statistics', async t => {
    const rows = [...Array.from({ length: 12 }, (_, i) => key({ id: String(i), revoked_at: '2026-01-01' })), key()];
    const { view } = await component(t, { async listApiKeys() { return rows; } });
    await view.load();
    assert.deepEqual(view.visibleKeys.value.map(row => row.id), ['key-a']);
    assert.equal(view.pagination.value.total, 1);
    assert.equal(view.stats.value.total, 1);
    assert.equal(view.stats.value.revoked, 0);
    assert.equal(view.showPagination.value, false);
});

test('an all-revoked response presents the successful empty state', async t => {
    const { view } = await component(t, { async listApiKeys() { return [key({ revoked_at: '2026-01-01' })]; } });
    await view.load();
    assert.equal(view.keysReadState.value, 'empty');
    assert.deepEqual(view.visibleKeys.value, []);
});

test('revocation removes the row and clears its secret even when refresh fails', async t => {
    const { view, harness } = await component(t);
    view.dialog.name = 'example';
    await view.submit();
    harness.api.listApiKeys = async force => { harness.refreshes.push(force); throw new Error('offline'); };
    await view.revoke(key());
    assert.deepEqual(harness.revoked, ['key-a']);
    assert.deepEqual(view.visibleKeys.value, []);
    assert.equal(view.result.plaintext, '');
    assert.equal(view.result.open, false);
    assert.equal(harness.refreshes.at(-1), true);
    assert.equal(view.keysReadState.value, 'refresh-error');
});

test('cancelled or failed revocation preserves the row', async t => {
    const { view, harness } = await component(t);
    await view.load();
    harness.confirm = async () => { throw 'cancel'; };
    await view.revoke(key());
    assert.deepEqual(harness.revoked, []);
    harness.confirm = async () => {};
    harness.api.revokeApiKey = async () => { throw new Error('rejected'); };
    await view.revoke(key());
    assert.deepEqual(view.visibleKeys.value.map(row => row.id), ['key-a']);
});

test('a created key is masked but copies its complete value until saved', async t => {
    const { view, harness } = await component(t);
    view.dialog.name = 'example';
    await view.submit();
    assert.equal(view.maskedPlaintext.value, 'test-onl…1234');
    assert.equal(view.displayKey(key()), 'test-onl…1234');
    assert.equal(view.canCopyKey(key()), true);
    assert.equal(view.canCopyKey(key({ id: 'other-key' })), false);
    assert.equal(view.canCopyKey(key({ expires_at: '2025-01-01' })), false);
    await view.copyPlain();
    assert.deepEqual(harness.copied, [plain]);
    view.closeResult();
    assert.equal(view.maskedPlaintext.value, '');
    await view.copyPlain();
    assert.deepEqual(harness.copied, [plain]);
    assert.equal(view.result.plaintext, '');
    assert.equal(view.canCopyKey(key()), false);
    assert.equal(view.displayKey(key()), 'vk_example…');
});

test('refreshing a revoked newly created key clears the one-time copy', async t => {
    const { view, harness } = await component(t);
    view.dialog.name = 'example';
    await view.submit();
    harness.api.listApiKeys = async () => [key({ revoked_at: '2026-01-01' })];
    await view.load(true);
    await view.copyPlain();
    assert.deepEqual(harness.copied, []);
    assert.equal(view.result.open, false);
});

test('a list response started before revocation cannot restore the removed row', async t => {
    const { view, harness } = await component(t);
    await view.load();
    let release;
    harness.api.listApiKeys = () => new Promise(resolve => { release = resolve; });
    const oldLoad = view.load();
    harness.api.listApiKeys = async () => { throw new Error('offline'); };
    await view.revoke(key());
    release([key()]);
    await oldLoad;
    assert.deepEqual(view.visibleKeys.value, []);
});

test('removing the last key on a page clamps pagination even if refresh fails', async t => {
    const rows = [...Array.from({ length: 10 }, (_, i) => key({ id: String(i) })), key()];
    const { view, harness } = await component(t, { async listApiKeys() { return rows; } });
    await view.load();
    view.page.value = 2;
    harness.api.listApiKeys = async () => { throw new Error('offline'); };
    await view.revoke(key());
    assert.equal(view.page.value, 1);
    assert.equal(view.pagination.value.total, 10);
});

test('template binds only the masked creation value and labels copy accurately', () => {
    const source = readFileSync(new URL('./src/views/ApiKeys.js', import.meta.url), 'utf8');
    const template = source.slice(source.indexOf('template:'));
    assert.match(template, /\{\{ maskedPlaintext \}\}/);
    assert.doesNotMatch(template, /\{\{ result\.plaintext \}\}/);
    assert.match(template, /复制完整密钥/);
    assert.doesNotMatch(template, /stats\.revoked/);
    assert.doesNotMatch(source, /localStorage|sessionStorage/);
});

test('copying MCP guide gives an unfamiliar agent the complete contract without the created secret', async t => {
    const { view, harness } = await component(t);
    view.dialog.name = 'example';
    await view.submit();
    await view.copyMcpInstructions();
    const text = harness.copied.at(-1);
    for (const tool of ['list_libraries', 'list_permissions', 'validate_scope', 'get_document',
        'get_entity', 'get_relation', 'get_evidence', 'search_entities', 'search_relations',
        'retrieve', 'search_knowledge', 'answer', 'upload_file']) {
        assert.ok(text.includes(tool), `missing tool: ${tool}`);
    }
    for (const detail of ['candidate_k', 'next_cursor', 'entity.entity', 'upstream_invalid_response', 'request']) {
        assert.ok(text.includes(detail), `missing contract detail: ${detail}`);
    }
    assert.ok(!text.includes(view.result.plaintext), 'guide must not include the actual key');
});

test('copying HTTP guide includes full routes, SSE completion and evidence rules without the created secret', async t => {
    const { view, harness } = await component(t);
    view.dialog.name = 'example';
    await view.submit();
    await view.copyApiInstructions();
    const text = harness.copied.at(-1);
    for (const path of ['/api/v1/libraries', '/api/v1/scopes/validate',
        '/api/v1/libraries/{slug}/documents/{document_id}', '/api/v1/libraries/{slug}/entities/{entity_id}',
        '/api/v1/libraries/{slug}/relations/{relation_id}', '/api/v1/libraries/{slug}/evidence/{evidence_id}',
        '/api/v1/entities/search', '/api/v1/relations/search', '/api/v1/retrieval',
        '/api/v1/answers', '/api/v1/answers/stream', '/me/permissions']) {
        assert.ok(text.includes(path), `missing route: ${path}`);
    }
    assert.ok(text.includes('只有收到 result 才标记成功'));
    assert.ok(text.includes('不能用 chunk_id 替代'));
    assert.ok(!text.includes(view.result.plaintext), 'guide must not include the actual key');
});
