import assert from 'node:assert/strict';
import fs from 'node:fs';
import test from 'node:test';

const view = fs.readFileSync(new URL('./src/views/SchemaLifecycle.js', import.meta.url), 'utf8');
const app = fs.readFileSync(new URL('./src/app.js', import.meta.url), 'utf8');
const layout = fs.readFileSync(new URL('./src/views/Layout.js', import.meta.url), 'utf8');
const style = fs.readFileSync(new URL('./style.css', import.meta.url), 'utf8');

test('Schema lifecycle view exposes the complete bounded workflow', () => {
    for (const token of [
        "'/schema-lifecycle'",
        "tab: 'overview'",
        "'entities'", "'relations'", "'attributes'", "'constraints'", "'impact'",
        'cloneSchemaVersion', 'validateSchemaVersion', 'getSchemaImpact',
        'activateSchemaVersion', 'createSchemaEntityType', 'createSchemaRelationType',
        'createSchemaAttribute', 'createSchemaConstraint',
        'updateSchemaItem', 'disableSchemaItem',
        'expected_version_state_hash', 'expected_active_version_id',
        "confirmation: 'activate_schema_version'",
    ]) assert.ok(view.includes(token), `missing ${token}`);
    assert.ok(app.includes("path: 'schema-lifecycle'"));
    assert.ok(app.includes('libraryManagement: true'));
    assert.ok(layout.includes('access.schemaLifecycle'));
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
        view.indexOf('await loadVersions({ chooseVersion: false })')
            < view.indexOf('await canonicalRoute();\n                return response;'),
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
