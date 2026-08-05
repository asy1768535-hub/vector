import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
    compatibilityConflicts,
    compatibilityLibraryReadiness,
    compatibilityReasonLabel,
    formatRetrievalNumber,
    librariesForOrganization,
    organizationAdminMemberships,
    retrievalScopeKey,
    retrievalSourceLocation,
    validateRetrievalTest,
} from './src/retrieval_test_ui.js';

const view = readFileSync(new URL('./src/views/RetrievalTest.js', import.meta.url), 'utf8');
const api = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');
const app = readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const layout = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const menu = readFileSync(new URL('./src/menu_access.js', import.meta.url), 'utf8');
const navigation = readFileSync(new URL('./src/domain_navigation.js', import.meta.url), 'utf8');
const store = readFileSync(new URL('./src/store.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('filters exact Organization administrators without platform-role inference', () => {
    const rows = organizationAdminMemberships([
        { organization_id: 'org-a', role: 'member', name: 'A member' },
        { organization_id: 'org-b', role: 'organization_admin', name: 'B' },
        { organization_id: 'org-b', role: 'organization_admin', name: 'duplicate' },
        { organization_id: 'org-c', role: 'admin', name: 'wrong role' },
    ]);
    assert.deepEqual(rows, [
        { organization_id: 'org-b', role: 'organization_admin', name: 'B' },
    ]);
});

test('intersects readable Libraries with the exact Organization identity', () => {
    const rows = librariesForOrganization([
        { organization_id: 'org-a', library_slug: 'a', library_name: 'A', actions: ['read'] },
        { organization_id: 'org-a', library_slug: 'write', actions: ['insert'] },
        { organization_id: 'org-b', library_slug: 'b', library_name: 'B', actions: ['read'] },
        { library_slug: 'legacy', actions: ['read'] },
    ], 'org-a');
    assert.deepEqual(rows, [{ slug: 'a', name: 'A' }]);
});

test('scope key preserves exact Library order', () => {
    assert.notEqual(
        retrievalScopeKey('org-a', ['first', 'second']),
        retrievalScopeKey('org-a', ['second', 'first']),
    );
    assert.equal(retrievalScopeKey('', ['first']), '');
});

test('validates strict retrieval bounds and candidate relationship', () => {
    const valid = {
        organizationId: 'org-a',
        librarySlugs: ['legal'],
        query: 'termination',
        topK: 10,
        candidateK: 30,
        scoreThreshold: 0.2,
    };
    assert.equal(validateRetrievalTest(valid), '');
    assert.match(validateRetrievalTest({ ...valid, librarySlugs: [] }), /知识库/);
    assert.match(validateRetrievalTest({ ...valid, query: '   ' }), /检索内容/);
    assert.match(validateRetrievalTest({ ...valid, candidateK: 5 }), /候选数/);
    assert.match(validateRetrievalTest({ ...valid, scoreThreshold: 2 }), /阈值/);
});

test('maps known compatibility reasons and bounds structured conflicts', () => {
    assert.equal(compatibilityReasonLabel('embedding_profile_mismatch'), '向量化配置不一致');
    assert.equal(compatibilityReasonLabel('library_index_unready'), '知识库索引未就绪');
    assert.equal(compatibilityReasonLabel('future_reason'), 'future_reason');
    const projected = compatibilityConflicts({
        body: {
            detail: {
                code: 'federated_scope_incompatible',
                incompatibilities: [
                    { library_slug: 'legal', reason_codes: ['retrieval_profile_mismatch'] },
                    { library_slug: { secret: true }, reason_codes: ['provider secret', 2] },
                ],
                provider_trace: 'must not render',
            },
        },
    });
    assert.deepEqual(projected, [
        { librarySlug: 'legal', reasons: ['检索策略不一致'] },
    ]);
});

test('diagnostics links back to single-library search and restores route scope', () => {
    assert.ok(view.includes('RetrievalModeSwitch'));
    assert.ok(view.includes("route.query.libraries"));
    assert.ok(view.includes(':library-slugs="librarySlugs"'));
});

test('projects per-Library text readiness without graph inference', () => {
    assert.deepEqual(
        compatibilityLibraryReadiness({
            index_state: 'ready',
            embedding_ready: true,
            retrieval_ready: false,
            graph_ready: true,
        }),
        [
            { key: 'index', label: '索引', ready: true },
            { key: 'embedding', label: 'Embedding', ready: true },
            { key: 'retrieval', label: '检索策略', ready: false },
        ],
    );
});

test('formats diagnostics and source locations defensively', () => {
    assert.equal(formatRetrievalNumber(1 / 61), '0.016393');
    assert.equal(formatRetrievalNumber(null), '—');
    assert.equal(retrievalSourceLocation({ page: 12, title_path: ['第三章', '第十二条'] }), '第 12 页 · 第三章 / 第十二条');
    assert.equal(retrievalSourceLocation({ document_revision: 3 }), 'Revision v3');
});

test('wires Organization auth state, route, menu, and strict APIs', () => {
    assert.match(store, /organizations:\s*\[\]/);
    assert.ok(store.includes("fetch('/me/organizations'"));
    assert.match(menu, /organizationAdmin/);
    assert.match(menu, /role\s*===\s*'organization_admin'/);
    assert.match(app, /path:\s*'retrieval-test'/);
    assert.match(app, /organizationAdmin:\s*true/);
    assert.match(app, /canAccessOrganizationRoute/);
    assert.match(layout, /visibleSidebarGroups/);
    assert.match(navigation, /access:\s*'retrievalTest'/);
    assert.match(navigation, /path:\s*APP_PATHS\.retrievalTest/);
    for (const token of [
        'listMyOrganizations',
        'checkLibraryCompatibility',
        'runOrganizationRetrievalTest',
    ]) assert.ok(api.includes(`export const ${token}`), `missing ${token}`);
    assert.ok(api.includes('/me/library-compatibility/check'));
    assert.ok(api.includes('/retrieval-tests'));
});

test('view preserves exact compatibility and retrieval request boundaries', () => {
    for (const token of [
        'librariesForOrganization',
        'compatibilityRequestSeq',
        'retrievalRequestSeq',
        'retrievalScopeKey',
        'api.checkLibraryCompatibility',
        'api.runOrganizationRetrievalTest',
        "channels: ['text']",
        'content_truncated',
        'total_elapsed_ms',
        'rewrite_sources',
    ]) assert.ok(view.includes(token), `missing view contract ${token}`);
    assert.ok(!view.includes('api.listLibraries'), 'must not use platform-admin Library list');
    assert.ok(!view.includes('v-html'), 'diagnostic content must use escaped Vue text');
    assert.ok(!view.includes('console.'), 'queries, excerpts, and errors must not be logged');
    assert.doesNotMatch(view, /style="[^"]*"/, 'view must not use inline styles');
});

test('diagnostic styles are compact, responsive, and internally bounded', () => {
    for (const token of [
        '.retrieval-test-workspace',
        '.retrieval-test-controls',
        '.retrieval-test-scope',
        '.retrieval-test-results-shell',
        '.retrieval-test-hit-detail',
        '@media screen and (max-width: 1199px)',
        '@media screen and (max-width: 899px)',
    ]) assert.ok(css.includes(token), `missing CSS token ${token}`);
    assert.match(css, /\.retrieval-test-results-shell\s*\{[^}]*overflow-x:\s*auto/s);
});
