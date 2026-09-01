import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import { menuAccess } from './src/menu_access.js';

const view = readFileSync(new URL('./src/views/GraphGovernance.js', import.meta.url), 'utf8');
const app = readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const layout = readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const menu = readFileSync(new URL('./src/menu_access.js', import.meta.url), 'utf8');
const navigation = readFileSync(new URL('./src/domain_navigation.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');
const schema = readFileSync(
    new URL('../app/schemas/knowledge_catalog.py', import.meta.url),
    'utf8',
);
const service = readFileSync(
    new URL('../app/services/knowledge_catalog.py', import.meta.url),
    'utf8',
);

test('wires one effective-read route inside the knowledge governance domain', () => {
    assert.match(app, /import GraphGovernance/);
    assert.match(app, /path:\s*'graph'/);
    assert.match(app, /component:\s*GraphGovernance/);
    assert.match(app, /effectivePerm:\s*'read'/);
    assert.match(layout, /visibleSidebarGroups/);
    assert.match(navigation, /icon:\s*'carbon:chart-relationship'/);
    assert.match(navigation, /access:\s*'knowledgeGraph'/);
    assert.match(menu, /knowledgeGraph:\s*acts\.has\('read'\)/);

    assert.equal(menuAccess({ is_superuser: true }, []).knowledgeGraph, false);
    assert.equal(menuAccess({}, [{ actions: ['read'] }]).knowledgeGraph, true);
});

test('keeps one unified graph workspace plus publication history', () => {
    for (const token of [
        'name="browse"',
        "name=\"publications\"",
        'resolveGraphScope',
        'graphRouteQuery',
        ':multiple-limit="20"',
        'scope.organizationId',
        'scope.librarySlugs',
        'entityCursor',
        'relationCursor',
        'advanceGraphCursor',
        'retreatGraphCursor',
        'GraphKnowledgeBrowser',
        '<graph-knowledge-browser',
    ]) assert.ok(view.includes(token), `missing route/directory contract ${token}`);
    assert.equal((view.match(/<el-tab-pane /g) || []).length, 2);
    assert.equal(view.includes('name="explore"'), false);
    assert.ok(!view.includes('<el-tab-pane label="实体管理" name="entities"'));
    assert.ok(!view.includes('<el-tab-pane label="关系管理" name="relations"'));
    assert.ok(!view.includes('<el-tab-pane label="待审核" name="review"'));
});

test('summarizes graph extraction in Chinese without listing source documents', () => {
    for (const token of [
        '抽取状态：',
        '来源文档：',
        '知识结构版本已固定',
        '查看 / 编辑知识结构',
    ]) assert.ok(view.includes(token), `missing graph summary copy ${token}`);
    for (const token of [
        'source set',
        '个 revision',
        'graph-discovery-document-jobs',
        'graphJobState.data.document_jobs',
    ]) assert.equal(view.includes(token), false, `source document detail must stay out of graph page: ${token}`);
});

test('uses plain Chinese for visible graph knowledge-structure copy', () => {
    for (const token of [
        '等待发现 Schema',
        '正在发现 Schema',
        'AI Schema 待确认',
        'Schema discovery',
        '当前知识库的 Schema',
        'Schema 管理',
        '同一 Ontology',
        '<span>Ontology 版本</span>',
        '当前 Ontology 暂无可发布操作',
        '<dt>Ontology</dt>',
        '<el-form-item label="Ontology">',
        '<dt>Revision</dt>',
    ]) assert.equal(view.includes(token), false, `technical display copy must be removed: ${token}`);
});

test('uses strict Graph Catalog responses and independent stale-response fences', () => {
    for (const token of [
        'api.searchGraphEntities',
        'api.searchGraphRelations',
        'api.getGraphEntity',
        'api.getGraphRelation',
        'graphEntityPageMatches',
        'graphRelationPageMatches',
        'graphEntityDetailMatches',
        'graphRelationDetailMatches',
        'entityRequestSeq',
        'relationRequestSeq',
        'entityDetailRequestSeq',
        'relationDetailRequestSeq',
        'evidenceRequestSeq',
        'scopeIdentity()',
    ]) assert.ok(view.includes(token), `missing response fence ${token}`);
    assert.ok(
        view.indexOf('if (previousIdentity !== scopeIdentity())')
            < view.indexOf("path: '/knowledge-governance/graph'"),
        'scope changes invalidate stale drawers before canonical route replacement',
    );
});

test('Evidence reuses Catalog and verifies Library, Evidence, Document, Revision, Chunk and fact', () => {
    for (const token of [
        'api.getCatalogEvidence',
        'graphEvidenceMatches',
        'libraryId:',
        'evidenceId:',
        'documentId:',
        'revisionId:',
        'chunkId:',
        'factId:',
        "path: '/knowledge-assets/catalog'",
    ]) assert.ok(view.includes(token), `missing Evidence contract ${token}`);
    assert.match(schema, /class CatalogEvidenceDetailRead[\s\S]*library_id:\s*uuid\.UUID/);
    assert.match(schema, /class CatalogEvidenceFactRefRead[\s\S]*chunk_id:\s*uuid\.UUID \| None/);
    assert.match(service, /binding\.chunk_id\.label\("chunk_id"\)/);
    assert.match(service, /library_id=library\.id/);
});

test('renders bounded fields without admin Library calls or unsafe content paths', () => {
    for (const token of [
        'graphPropertyRows',
        'aliases_truncated',
        'evidence_truncated',
        'documents_truncated',
        'relations_truncated',
        'model_provider',
        'prompt_version',
    ]) assert.ok(view.includes(token), `missing bounded detail field ${token}`);
    for (const forbidden of [
        'api.listLibraries',
        'v-html',
        'console.',
        'localStorage',
        'storage_url',
        'object_key',
        'signed_url',
        'window.open',
    ]) assert.ok(!view.includes(forbidden), `forbidden view token ${forbidden}`);
    assert.doesNotMatch(view, /style="[^"]*"/, 'view must not use inline style attributes');
});

test('uses a dense internally scrolling responsive work surface', () => {
    for (const token of [
        '.graph-workspace',
        '.graph-scope-inline',
        '.graph-filter-band',
        '.graph-directory',
        '.graph-table-shell',
        '.graph-detail-drawer',
        '.graph-evidence-dialog',
        '@media screen and (max-width: 899px)',
        '@media screen and (max-width: 520px)',
    ]) assert.ok(css.includes(token), `missing graph CSS token ${token}`);
    assert.equal(css.includes('.graph-scope-band'), false);
    assert.match(css, /\.graph-table-shell\s*\{[^}]*overflow-x:\s*auto/s);
    assert.match(css, /\.graph-heading-icon\s*\{[^}]*border-radius:\s*8px/s);
    assert.match(css, /\.graph-detail-drawer\s*\{[^}]*max-width:\s*100vw/s);
});

test('renders entity details in a dialog with a bounded local graph and keeps relation inspector', () => {
    assert.ok(view.includes("'has-inspector': scope.tab === 'browse' && relationDetail.open"));
    assert.equal(view.includes('<Teleport'), false);
    assert.ok(view.includes('<el-dialog v-model="entityDetail.open"'));
    assert.ok(view.includes('class="graph-entity-dialog"'));
    assert.ok(view.includes('entity-preview'));
    assert.ok(view.includes(':selected-seed="entityDetail.data.entity"'));
    assert.ok(view.includes('graph-detail-inspector-header'));
    assert.equal(view.includes('showSelectedRelations'), false);
    assert.ok(view.includes('该实体暂无关系，无法显示局部关系图'));
    assert.equal((view.match(/<h4>属性<\/h4>/g) || []).length, 2);
});

test('closing entity details clears the URL without reloading the graph workspace', () => {
    const applyRoute = view.slice(
        view.indexOf('async function applyRoute()'),
        view.indexOf('const stopRouteWatch'),
    );
    assert.match(applyRoute, /const workspaceChanged = previousIdentity !== scopeIdentity\(\)/);
    assert.match(applyRoute, /if \(!workspaceChanged && \(\(previousEntityId && !scope\.entityId\)/);
    assert.ok(applyRoute.indexOf('previousEntityId && !scope.entityId')
        < applyRoute.indexOf('await loadCurrentPage()'));

    const closeEntity = view.slice(
        view.indexOf('function closeEntityDetail()'),
        view.indexOf('function closeRelationDetail()'),
    );
    assert.match(closeEntity, /router\.replace\(\{/);
    assert.doesNotMatch(closeEntity, /navigate\(/);
});

test('selecting a browse entity keeps the graph workspace in place', () => {
    const selectBrowserEntity = view.slice(
        view.indexOf('function selectBrowserEntity(row)'),
        view.indexOf('async function editBrowserEntity(data)'),
    );
    assert.match(selectBrowserEntity, /browserSelectedEntityId\.value = entityId/);
    assert.doesNotMatch(selectBrowserEntity, /navigate\(/);
    assert.ok(view.includes(':selected-entity-id="browserSelectedEntityId"'));
});

test('keeps organization scope internal for single-organization users', () => {
    assert.ok(view.includes('const showOrganizationSelector = computed(() => organizations.value.length > 1)'));
    assert.ok(view.includes('v-if="showOrganizationSelector" class="graph-scope-control graph-organization-control"'));
    assert.ok(view.includes("'is-single-organization': !showOrganizationSelector"));
    assert.ok(view.includes('scope.organizationId'));
});
