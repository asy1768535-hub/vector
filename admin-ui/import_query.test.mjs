import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveImportEntry } from './src/import_query.js';

const libraries = [{ slug: 'hr' }, { slug: 'product' }];

test('accepts an allowed library and supported mode', () => {
    assert.deepEqual(resolveImportEntry({ library: 'product', mode: 'replace' }, libraries), {
        slug: 'product', mode: 'replace', replaceDocumentId: '', replaceTitle: '',
    });
});

test('accepts replace target query in replace mode', () => {
    assert.deepEqual(resolveImportEntry({
        library: 'product',
        mode: 'replace',
        replaceDocumentId: 'doc-1',
        replaceTitle: '手册.pdf',
    }, libraries), {
        slug: 'product', mode: 'replace', replaceDocumentId: 'doc-1', replaceTitle: '手册.pdf',
    });
});

test('ignores replace target query outside replace mode', () => {
    assert.deepEqual(resolveImportEntry({
        library: 'product',
        mode: 'add',
        replaceDocumentId: 'doc-1',
        replaceTitle: '手册.pdf',
    }, libraries), {
        slug: 'product', mode: 'add', replaceDocumentId: '', replaceTitle: '',
    });
});

test('defaults mode to add and rejects an unavailable library', () => {
    assert.deepEqual(resolveImportEntry({ library: 'secret', mode: 'other' }, libraries, 'hr'), {
        slug: 'hr', mode: 'add', replaceDocumentId: '', replaceTitle: '',
    });
});

test('falls back to first available library then null', () => {
    assert.deepEqual(resolveImportEntry({}, libraries), { slug: 'hr', mode: 'add', replaceDocumentId: '', replaceTitle: '' });
    assert.deepEqual(resolveImportEntry({ library: 'hr' }, []), { slug: null, mode: 'add', replaceDocumentId: '', replaceTitle: '' });
});
