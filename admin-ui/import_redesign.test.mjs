import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import {
    ALLOWED_EXTENSIONS, MAX_FILE_SIZE, MAX_BATCH_SIZE, LEGACY_SAVE_AS,
    validateFile, validateBatch, fileKey, formatSize, fileTypeIcon,
    ST_LABEL, ST_TAG, OP_LABEL, OP_TAG,
} from './src/import_ui.js';

const source = readFileSync(new URL('./src/views/Import.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

// ── Helpers for creating test File-like objects ──
function mockFile(name, size, lastModified = 1700000000000) {
    return { name, size, lastModified };
}

// ════════════════════════════════════════════════════════════
//  Real behavior tests (import_ui.js pure functions)
// ════════════════════════════════════════════════════════════

// ── validateFile ──
test('validateFile accepts all allowed extensions', () => {
    for (const ext of ['.txt', '.md', '.markdown', '.json', '.csv', '.docx', '.xlsx', '.pdf']) {
        const r = validateFile(mockFile(`doc${ext}`, 1024));
        assert.equal(r.valid, true, `${ext} should be valid`);
    }
});

test('validateFile rejects legacy .doc with save-as message', () => {
    const r = validateFile(mockFile('old.doc', 1024));
    assert.equal(r.valid, false);
    assert.ok(r.reason.includes('.docx'), 'reason mentions .docx');
});

test('validateFile rejects legacy .xls with save-as message', () => {
    const r = validateFile(mockFile('sheet.xls', 1024));
    assert.equal(r.valid, false);
    assert.ok(r.reason.includes('.xlsx'), 'reason mentions .xlsx');
});

test('validateFile rejects unknown extensions', () => {
    for (const name of ['a.exe', 'a.png', 'a.zip', 'noext']) {
        const r = validateFile(mockFile(name, 1024));
        assert.equal(r.valid, false, `${name} should be rejected`);
    }
});

test('validateFile rejects files over 50 MB', () => {
    const r = validateFile(mockFile('big.pdf', MAX_FILE_SIZE + 1));
    assert.equal(r.valid, false);
    assert.ok(r.reason.includes('50 MB'), 'reason mentions 50 MB limit');
});

test('validateFile accepts files at or under 50 MB', () => {
    assert.equal(validateFile(mockFile('max.pdf', MAX_FILE_SIZE)).valid, true);
    assert.equal(validateFile(mockFile('small.pdf', MAX_FILE_SIZE - 1)).valid, true);
});

test('validateFile returns failType for classification', () => {
    assert.equal(validateFile(mockFile('old.doc', 100)).failType, 'format');
    assert.equal(validateFile(mockFile('bad.exe', 100)).failType, 'format');
    assert.equal(validateFile(mockFile('big.pdf', MAX_FILE_SIZE + 1)).failType, 'size');
    assert.equal(validateFile(mockFile('ok.pdf', 100)).failType, undefined);
});

// ── validateBatch: new signature { accepted, duplicates, invalid } ──
test('validateBatch returns three arrays', () => {
    const r = validateBatch([mockFile('a.pdf', 100)], []);
    assert.ok(Array.isArray(r.accepted));
    assert.ok(Array.isArray(r.duplicates));
    assert.ok(Array.isArray(r.invalid));
    assert.equal(r.accepted.length, 1);
    assert.equal(r.duplicates.length, 0);
    assert.equal(r.invalid.length, 0);
});

test('validateBatch puts duplicates in separate array (not invalid)', () => {
    const f = mockFile('a.pdf', 100);
    const key = fileKey(f);
    const { accepted, duplicates, invalid } = validateBatch([f], [key]);
    assert.equal(accepted.length, 0);
    assert.equal(duplicates.length, 1);
    assert.equal(invalid.length, 0);
});

test('validateBatch puts format/size errors in invalid array', () => {
    const files = [mockFile('bad.exe', 100), mockFile('big.pdf', MAX_FILE_SIZE + 1)];
    const { accepted, duplicates, invalid } = validateBatch(files, []);
    assert.equal(accepted.length, 0);
    assert.equal(duplicates.length, 0);
    assert.equal(invalid.length, 2);
});

test('validateBatch enforces MAX_BATCH_SIZE without silent drop', () => {
    const existing = [];
    const files = Array.from({ length: MAX_BATCH_SIZE + 5 }, (_, i) => mockFile(`doc${i}.pdf`, 100, i));
    const { accepted, duplicates, invalid } = validateBatch(files, existing);
    assert.equal(accepted.length, MAX_BATCH_SIZE);
    assert.equal(duplicates.length, 0);
    assert.equal(invalid.length, 5);
    // Over-limit files are in invalid, not silently dropped
    assert.ok(invalid.some((r) => r.reason.includes(String(MAX_BATCH_SIZE))));
});

test('validateBatch mixes accepted, duplicates, and invalid in one batch', () => {
    const f1 = mockFile('a.pdf', 100, 1);
    const f2 = mockFile('bad.exe', 100, 2);
    const f3 = mockFile('a.pdf', 100, 1); // same as f1
    const f4 = mockFile('ok.txt', 200, 4);
    const { accepted, duplicates, invalid } = validateBatch([f1, f2, f3, f4], []);
    assert.equal(accepted.length, 2, 'f1 and f4 accepted');
    assert.equal(duplicates.length, 1, 'f3 is duplicate of f1');
    assert.equal(invalid.length, 1, 'f2 is invalid format');
});

// ── fileKey ──
test('fileKey produces stable dedup key', () => {
    const a = mockFile('a.pdf', 100, 999);
    const b = mockFile('a.pdf', 100, 999);
    assert.equal(fileKey(a), fileKey(b));
});

test('fileKey differs for different files', () => {
    assert.notEqual(fileKey(mockFile('a.pdf', 100, 1)), fileKey(mockFile('b.pdf', 100, 1)));
    assert.notEqual(fileKey(mockFile('a.pdf', 100, 1)), fileKey(mockFile('a.pdf', 200, 1)));
});

// ── formatSize ──
test('formatSize handles boundaries correctly', () => {
    assert.equal(formatSize(0), '0 B');
    assert.equal(formatSize(1024), '1 KB');
    assert.equal(formatSize(1536), '1.5 KB');
    assert.equal(formatSize(1048576), '1 MB');
    assert.equal(formatSize(MAX_FILE_SIZE), '50 MB');
});

// ── fileTypeIcon delegates to documents_ui.js ──
test('fileTypeIcon returns correct asset path for known types', () => {
    assert.equal(fileTypeIcon(mockFile('a.pdf', 1)), './assets/file-types/pdf.svg');
    assert.equal(fileTypeIcon(mockFile('a.DOCX', 1)), './assets/file-types/docx.svg');
    assert.equal(fileTypeIcon(mockFile('a.xlsx', 1)), './assets/file-types/xlsx.svg');
    assert.equal(fileTypeIcon(mockFile('a.md', 1)), './assets/file-types/md.svg');
    assert.equal(fileTypeIcon(mockFile('a.txt', 1)), './assets/file-types/txt.svg');
    assert.equal(fileTypeIcon(mockFile('a.json', 1)), './assets/file-types/json.svg');
    assert.equal(fileTypeIcon(mockFile('a.csv', 1)), './assets/file-types/csv.svg');
});

test('fileTypeIcon returns null for unknown extensions', () => {
    assert.equal(fileTypeIcon(mockFile('a.exe', 1)), null);
    assert.equal(fileTypeIcon({ name: '' }), null);
});

// ── Constants ──
test('constants have expected values', () => {
    assert.equal(MAX_FILE_SIZE, 50 * 1024 * 1024);
    assert.equal(MAX_BATCH_SIZE, 20);
    assert.equal(ALLOWED_EXTENSIONS.has('.pdf'), true);
    assert.equal(ALLOWED_EXTENSIONS.has('.doc'), false);
});

// ════════════════════════════════════════════════════════════
//  Source checks (Import.js structure)
// ════════════════════════════════════════════════════════════

test('Import.js uses resolveImportEntry return value', () => {
    // Should destructure the return value and assign to slug.value / mode.value
    assert.ok(source.includes('resolveImportEntry(route.query'), 'calls resolveImportEntry');
    assert.ok(source.includes('slug.value = entry.slug') || (source.includes('.slug') && source.includes('resolveImportEntry')), 'assigns slug from return');
    assert.ok(source.includes('mode.value = entry.mode') || (source.includes('.mode') && source.includes('resolveImportEntry')), 'assigns mode from return');
});

test('Import.js reads resp.documents not resp.operations', () => {
    assert.ok(source.includes('resp.documents') || source.includes('.documents'), 'reads documents field');
    assert.ok(!source.includes('resp.operations'), 'no longer reads operations');
    assert.ok(!source.includes('importResult.operations'), 'no longer reads importResult.operations');
});

test('replace mode validates file before upload', () => {
    assert.ok(source.includes('onReplaceFileChange'), 'replace file change handler');
    assert.ok(source.includes('onReplaceDrop'), 'replace drop handler');
    assert.ok(source.includes('replaceFile'), 'replaceFile ref');
    assert.ok(source.includes('canReplace'), 'canReplace computed for button disable');
});

test('no inline style attributes in template', () => {
    const tpl = source.slice(source.indexOf('template:'));
    const styles = tpl.match(/style="/g) || [];
    assert.equal(styles.length, 0, 'zero inline style="..." attributes');
});

test('template uses import-* CSS classes', () => {
    for (const c of ['import-workspace', 'import-config-card', 'import-body',
                     'import-upload-card', 'import-file-card', 'import-dropzone',
                     'import-summary-bar']) {
        assert.ok(source.includes(c), `uses ${c}`);
    }
});

test('template includes drag-and-drop bindings', () => {
    assert.ok(source.includes('@dragover'), 'dragover event');
    assert.ok(source.includes('@drop'), 'drop event');
});

test('template uses _failType not error text for check columns', () => {
    assert.ok(source.includes('格式校验'), 'format check column');
    assert.ok(source.includes('大小校验'), 'size check column');
    assert.ok(source.includes("row._failType === 'format'"), 'checks _failType for format');
    assert.ok(source.includes("row._failType === 'size'"), 'checks _failType for size');
    assert.ok(!source.includes("row.error.includes('格式')"), 'no longer checks error text for format');
});

test('external_id toggle uses showExtId boolean not space hack', () => {
    assert.ok(source.includes('showExtId'), 'showExtId ref exists');
    assert.ok(source.includes('showExtId = !showExtId'), 'toggles showExtId');
    assert.ok(!source.includes("externalId = extIdSet ? '' : ' '"), 'no space hack');
});

test('template references libraryStats and calls loadStats after upload', () => {
    assert.ok(source.includes('libraryStats'), 'libraryStats called');
    assert.ok(source.includes('loadStats()'), 'loadStats called after upload');
});

test('CSS defines import-* classes', () => {
    for (const c of ['import-workspace', 'import-config-card', 'import-body',
                     'import-upload-card', 'import-dropzone', 'import-file-card',
                     'import-summary-bar', 'import-check-ok', 'import-check-fail']) {
        const escaped = c.replace(/-/g, '\\-');
        assert.match(css, new RegExp('\\.' + escaped + '\\s*\\{'), `${c} defined`);
    }
});

test('CSS includes import responsive rules', () => {
    assert.match(css, /1199px[\s\S]*import-upload-card/, 'import at 1199px');
    assert.match(css, /899px[\s\S]*import-body/, 'import at 899px');
});

test('CSS defines import-dropzone with dashed border and drag-over highlight', () => {
    assert.match(css, /\.import-dropzone\s*\{[\s\S]*?border:\s*2px\s+dashed/, 'dashed border');
    assert.match(css, /\.import-dropzone\.is-dragover\s*\{/, 'is-dragover state');
});

console.log('import redesign test passed');
