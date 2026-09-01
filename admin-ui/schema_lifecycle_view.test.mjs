import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const view = fs.readFileSync(new URL('./src/views/SchemaLifecycle.js', import.meta.url), 'utf8');
const normalizedView = view.replaceAll('\r\n', '\n');
const app = fs.readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const layout = fs.readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const navigation = fs.readFileSync(new URL('./src/domain_navigation.js', import.meta.url), 'utf8');
const style = fs.readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('Schema lifecycle view exposes the complete bounded workflow', () => {
    for (const token of [
        "'/knowledge-governance/schema'",
        "tab: 'overview'",
        "'entities'", "'relations'", "'attributes'", "'constraints'", "'impact'",
        'cloneSchemaVersion', 'validateSchemaVersion', 'getSchemaImpact',
        'importSchemaFile', '导入文件', '导入全新知识结构文件', 'schema-import',
        'activateSchemaVersion', 'createSchemaEntityType', 'createSchemaRelationType',
        'createSchemaAttribute', 'createSchemaConstraint',
        'updateSchemaItem', 'disableSchemaItem',
        'deleteSchemaDraft', 'disableSchemaVersion',
        '新建草稿', 'selectedVersion.value',
        'expected_version_state_hash', 'expected_active_version_id',
        "confirmation: 'activate_schema_version'",
        "confirmation: 'delete_schema_draft'",
        "confirmation: 'disable_schema_version'",
        '删除草稿', '停用知识结构',
    ]) assert.ok(view.includes(token), `missing ${token}`);
    assert.ok(app.includes("path: 'schema'"));
    assert.ok(app.includes('libraryManagement: true'));
    assert.ok(layout.includes('visibleSidebarGroups'));
    assert.ok(navigation.includes("access: 'schemaLifecycle'"));
});

test('Schema lifecycle view owns stale-response fences and avoids unsafe rendering', () => {
    for (const token of [
        'listRequestSeq', 'detailRequestSeq', 'validationRequestSeq',
        'impactRequestSeq', 'mutationRequestSeq',
        'schemaVersionListMatches', 'schemaVersionDetailMatches', 'schemaImpactMatches',
        'schemaValidationMatches', 'resetWorkspace', 'invalidateVersionScope',
        "item.version_key === detail.data?.version_key", 'router.replace',
        'scope.versionId !== identity.versionId',
    ]) assert.ok(view.includes(token), `missing ${token}`);
    assert.ok(
        normalizedView.indexOf('await loadVersions({ chooseVersion: false })')
            < normalizedView.indexOf('await canonicalRoute();\n                return response;'),
        'mutation refreshes the version list before changing the route',
    );
    assert.match(
        view.slice(view.indexOf('async function runValidation()'), view.indexOf('async function loadImpact()')),
        /await selectTab\('overview'\)/,
        'validation results are made visible from every Schema tab',
    );
    for (const forbidden of ['v-html', 'console.log', 'source_text', 'raw_response', 'storage_url', 'object_key']) {
        assert.equal(view.includes(forbidden), false, `forbidden ${forbidden}`);
    }
});

test('Schema lifecycle layout has stable desktop and narrow constraints', () => {
    assert.ok(style.includes('.schema-lifecycle-workspace'));
    assert.ok(style.includes('.schema-lifecycle-grid'));
    assert.ok(style.includes('@media screen and (max-width: 899px)'));
    assert.ok(style.includes('@media screen and (max-width: 520px)'));
    assert.ok(style.includes('.schema-lifecycle-table-shell'));
    assert.ok(style.includes('overflow-x: auto'));
});

test('knowledge structure page uses non-technical Chinese display copy', () => {
    for (const token of [
        '<h2>知识结构管理</h2>',
        '定义系统需要识别的对象、关系和字段',
        '检查是否可用',
        '设为当前使用',
        '内部标识（英文）',
        '高级属性规则（可选）',
        '高级校验规则（可选）',
        'schemaValueTypeLabel(row.value_type)',
        'schemaDirectionLabel(row.direction)',
        'schemaReviewPolicyLabel(row.default_review_policy)',
        'schemaCardinalityLabel(row.cardinality)',
    ]) assert.ok(view.includes(token), `missing plain-language Schema copy ${token}`);
    for (const token of [
        '知识结构（Schema）管理',
        '结构版本（Ontology）',
        '<dt>版本 ID</dt>',
        '<dt>状态哈希</dt>',
        ':label="item" :value="item"',
        '确认激活 ${detail.data.version_key}',
        '确认删除草稿 ${version.version_key}',
        '确认停用 ${version.version_key}',
    ]) assert.equal(view.includes(token), false, `technical display copy must be removed: ${token}`);
    for (const token of [
        '<el-table-column prop="key" label="内部标识"',
    ]) assert.equal(view.includes(token), false, `technical table column must be hidden: ${token}`);
});
