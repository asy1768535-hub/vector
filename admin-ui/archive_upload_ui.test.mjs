import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { ALLOWED_EXTENSIONS, validateFile } from './src/import_ui.js';

const importSrc = readFileSync(new URL('./src/views/Import.js', import.meta.url), 'utf8');
const apiSrc = readFileSync(new URL('./src/api.js', import.meta.url), 'utf8');

test('ZIP uses the existing file upload entry instead of a separate archive action', () => {
    assert.equal(ALLOWED_EXTENSIONS.has('.zip'), true);
    assert.equal(validateFile({ name: '资料包.zip', size: 1024 }).valid, true);
    assert.doesNotMatch(importSrc, /选择压缩包|uploadImportArchive|archiveInput/);
    assert.doesNotMatch(apiSrc, /\/import-archives/);
});
