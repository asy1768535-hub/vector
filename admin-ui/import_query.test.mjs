import test from 'node:test';
import assert from 'node:assert/strict';
import { resolveImportEntry } from './src/import_query.js';

const libraries = [{ slug: 'hr' }, { slug: 'product' }];

test('accepts an allowed library and supported mode', () => {
    assert.deepEqual(resolveImportEntry({ library: 'product', mode: 'replace' }, libraries), {
        slug: 'product', mode: 'replace',
    });
});

test('defaults mode to add and rejects an unavailable library', () => {
    assert.deepEqual(resolveImportEntry({ library: 'secret', mode: 'other' }, libraries, 'hr'), {
        slug: 'hr', mode: 'add',
    });
});

test('falls back to first available library then null', () => {
    assert.deepEqual(resolveImportEntry({}, libraries), { slug: 'hr', mode: 'add' });
    assert.deepEqual(resolveImportEntry({ library: 'hr' }, []), { slug: null, mode: 'add' });
});
