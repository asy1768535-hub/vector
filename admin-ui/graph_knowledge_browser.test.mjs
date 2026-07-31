import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const browser = fs.readFileSync(
    new URL('./src/components/GraphKnowledgeBrowser.js', import.meta.url),
    'utf8',
);
const parent = fs.readFileSync(
    new URL('./src/views/GraphGovernance.js', import.meta.url),
    'utf8',
);
const helper = fs.readFileSync(
    new URL('./src/graph_governance_ui.js', import.meta.url),
    'utf8',
);
const css = fs.readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('uses one default browser with three button-switched display modes', () => {
    for (const token of [
        "const mode = ref('directory')",
        'value="directory"',
        'value="content"',
        'value="graph"',
        "v-if=\"mode === 'directory'\"",
        "v-else-if=\"mode === 'content'\"",
        'class="graph-browser-map"',
        '实体目录',
        '实体详情',
        '关系网络',
    ]) assert.ok(browser.includes(token), `missing switched browser token ${token}`);
    assert.ok(parent.includes('GraphKnowledgeBrowser'));
    assert.ok(parent.includes('name="browse"'));
    assert.ok(!parent.includes('<el-tab-pane label="实体管理" name="entities"'));
    assert.ok(!parent.includes('<el-tab-pane label="关系管理" name="relations"'));
    assert.ok(!parent.includes('<el-tab-pane label="待审核" name="review"'));
    assert.ok(helper.includes("'browse'"));
    assert.match(helper, /GRAPH_TABS\.has\(requestedTab\) \? requestedTab : 'browse'/);
});

test('reuses strict catalog detail and published traversal contracts', () => {
    for (const token of [
        'api.searchGraphEntities',
        'graphEntityPageMatches',
        'explorationSeedSearchRowValid',
        'api.getGraphEntity',
        'graphEntityDetailMatches',
        'api.queryPublishedGraph',
        'graphTraversalResponseMatches',
        'expected_publication_id',
        'include_evidence_locators: true',
        'directorySeq',
        'detailSeq',
        'graphSeq',
        'scopeIdentity()',
    ]) assert.ok(browser.includes(token), `missing strict browser boundary ${token}`);
});

test('diagnoses graph readiness from documents, extraction jobs, entities, and relations', () => {
    for (const token of [
        'api.listDocuments',
        'api.listGraphExtractions',
        'api.searchGraphRelations',
        'graphRelationPageMatches',
        'overviewStatus',
        'graph-browser-pipeline',
        '文档来源',
        '图谱事实',
        '发布状态',
        'publishedEntityCount',
        '个正在处理',
        '尚未创建图谱抽取任务',
        '前往导入并开启图谱',
        "emit('open-import')",
    ]) assert.ok(browser.includes(token), `missing graph readiness diagnostic ${token}`);
    assert.ok(parent.includes(':can-write="canWrite"'));
    assert.ok(parent.includes('@open-import="openGraphImport"'));
    for (const label of ['图谱', '发布记录', '高级探查']) {
        assert.ok(parent.includes('label="' + label + '"'), 'missing graph workspace label ' + label);
    }
});

test('edits entities and relations from the unified graph workspace', () => {
    assert.ok(browser.includes("'edit-entity'"));
    assert.ok(browser.includes("emit('edit-entity', detail.data)"));
    assert.ok(browser.includes('@click="editEntity"'));
    assert.ok(browser.includes('修改实体'));
    assert.ok(parent.includes('@edit-entity="editBrowserEntity"'));
    assert.ok(parent.includes('@open-relation="loadRelationDetail"'));
    assert.ok(parent.includes("if (scope.tab === 'browse') explorerRefreshKey.value += 1"));
    assert.match(parent, /canWrite && \['browse', 'entities'\]\.includes\(scope\.tab\)/);
    assert.match(parent, /canWrite && \['browse', 'relations'\]\.includes\(scope\.tab\)/);
});

test('keeps content bounded and reuses existing drill-down surfaces', () => {
    for (const token of [
        'graphPropertyRows',
        "emit('open-relation'",
        "emit('open-evidence'",
        "emit('open-document'",
        '<graph-canvas',
        '@open-entity="selectGraphNode"',
    ]) assert.ok(browser.includes(token), `missing browser drill-down ${token}`);
    for (const forbidden of [
        'v-html', 'console.', 'source_text', 'raw_response', 'storage_url',
        'object_key', 'prompt', 'api_key', 'window.open',
    ]) assert.equal(browser.includes(forbidden), false, `forbidden browser token ${forbidden}`);
    assert.doesNotMatch(browser, /style="[^"]*"/);
});

test('provides stable responsive dimensions without simultaneous panels', () => {
    for (const token of [
        '.graph-browser',
        '.graph-browser-mode-switch',
        '.graph-browser-pipeline',
        '.graph-browser-directory',
        '.graph-browser-content',
        '.graph-browser-map',
        '@media screen and (max-width: 899px)',
        '@media screen and (max-width: 520px)',
    ]) assert.ok(css.includes(token), `missing graph browser CSS ${token}`);
    assert.match(css, /\.graph-browser-mode-switch \.el-radio-button__inner\s*\{[^}]*min-width:\s*118px/s);
    assert.match(css, /\.graph-browser-map \.graph-explorer-canvas\s*\{[^}]*min-height:\s*500px/s);
});
