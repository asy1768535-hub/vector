import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile, readdir, stat } from 'node:fs/promises';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const __dirname = dirname(fileURLToPath(import.meta.url));

// ── documentTypeIcon mapping ──────────────────────────────
import { documentType, documentTypeIcon } from './src/documents_ui.js';

const EXPECTED_MAPPING = [
    { row: { title: 'report.pdf' },       type: 'pdf',  asset: './assets/file-types/pdf.svg' },
    { row: { title: 'memo.docx' },        type: 'word', asset: './assets/file-types/docx.svg' },
    { row: { title: 'sheet.xlsx' },       type: 'excel', asset: './assets/file-types/xlsx.svg' },
    { row: { title: 'readme.md' },        type: 'markdown', asset: './assets/file-types/md.svg' },
    { row: { title: 'notes.txt' },        type: 'text', asset: './assets/file-types/txt.svg' },
    { row: { title: 'data.json' },        type: 'json', asset: './assets/file-types/json.svg' },
    { row: { title: 'export.csv' },       type: 'csv',  asset: './assets/file-types/csv.svg' },
    { row: { title: 'unknown.bin' },      type: 'other', asset: null },
    { row: { title: 'noext' },            type: 'other', asset: null },
    { row: { title: 'old.doc' },          type: 'word', asset: './assets/file-types/docx.svg' },
    { row: { title: 'legacy.xls' },       type: 'excel', asset: './assets/file-types/xlsx.svg' },
    { row: { title: 'README.MARKDOWN' },  type: 'markdown', asset: './assets/file-types/md.svg' },
];

test('documentTypeIcon returns asset path or null for all types', () => {
    for (const { row, asset } of EXPECTED_MAPPING) {
        assert.equal(documentTypeIcon(row), asset, `${JSON.stringify(row)} -> ${asset} (got ${documentTypeIcon(row)})`);
    }
});

test('same type always returns same asset path', () => {
    const r1 = documentTypeIcon({ title: 'a.pdf' });
    const r2 = documentTypeIcon({ title: 'b.pdf' });
    const r3 = documentTypeIcon({ title: 'c.PDF' });
    assert.equal(r1, r2);
    assert.equal(r2, r3);
    assert.equal(r1, './assets/file-types/pdf.svg');
});

// ── SVG asset files ───────────────────────────────────────
const ASSETS_DIR = join(__dirname, 'assets', 'file-types');
const EXPECTED_FILES = ['pdf.svg', 'docx.svg', 'xlsx.svg', 'md.svg', 'txt.svg', 'json.svg', 'csv.svg'];

test('all 7 SVG asset files exist and have reasonable size', async () => {
    for (const f of EXPECTED_FILES) {
        const fp = join(ASSETS_DIR, f);
        assert.ok(existsSync(fp), `${f} missing`);
        const s = await stat(fp);
        assert.ok(s.size > 200, `${f} too small (${s.size} bytes)`);
        assert.ok(s.size < 5000, `${f} too large (${s.size} bytes)`);
    }
});

test('SVG assets contain no external URLs', async () => {
    for (const f of EXPECTED_FILES) {
        const fp = join(ASSETS_DIR, f);
        const content = await readFile(fp, 'utf8');
        // xmlns is allowed
        const body = content.replace(/xmlns="[^"]*"/g, '');
        assert.equal(body.includes('http:'), false, `${f} contains external http:`);
        assert.equal(body.includes('https:'), false, `${f} contains external https:`);
        assert.equal(body.includes('cdn'), false, `${f} references CDN`);
        assert.ok(content.includes('<svg'), `${f} missing <svg>`);
        assert.ok(content.includes('viewBox'), `${f} missing viewBox`);
    }
});

test('all 7 files present and no extras in asset dir', async () => {
    const files = (await readdir(ASSETS_DIR)).filter(f => f.endsWith('.svg'));
    assert.equal(files.length, 7, 'expected exactly 7 SVG files');
});

// ── No old doc:* icons left ──────────────────────────────
const iconsSource = await readFile(new URL('./src/icons.js', import.meta.url), 'utf8');

test('icons.js no longer contains doc:* icon definitions', () => {
    assert.equal(iconsSource.includes("'doc:pdf'"), false, 'doc:pdf still in icons.js');
    assert.equal(iconsSource.includes("'doc:word'"), false, 'doc:word still in icons.js');
    assert.equal(iconsSource.includes("'doc:excel'"), false, 'doc:excel still in icons.js');
    assert.equal(iconsSource.includes("'doc:markdown'"), false, 'doc:markdown still in icons.js');
    assert.equal(iconsSource.includes("'doc:text'"), false, 'doc:text still in icons.js');
    assert.equal(iconsSource.includes("'doc:other'"), false, 'doc:other still in icons.js');
});

// ── Template uses img + local-icon fallback ───────────────
const source = await readFile(new URL('./src/views/Documents.js', import.meta.url), 'utf8');

test('Documents.js uses img for known types and local-icon fallback', () => {
    assert.ok(source.includes('<img v-if="documentTypeIcon(row)"'), 'missing img tag');
    assert.ok(source.includes('mdi:file-document-outline'), 'missing generic icon fallback');
    assert.ok(source.includes('aria-hidden="true"'), 'missing aria-hidden');
    assert.ok(source.includes('alt=""'), 'missing empty alt');
});

// ── Filter includes JSON/CSV ─────────────────────────────
test('Documents.js filter has JSON and CSV options', () => {
    assert.ok(source.includes('label="JSON"'), 'missing JSON filter option');
    assert.ok(source.includes('label="CSV"'), 'missing CSV filter option');
    assert.ok(source.includes('value="json"'), 'missing json filter value');
    assert.ok(source.includes('value="csv"'), 'missing csv filter value');
});

// ── Import accept updated ─────────────────────────────────
const importSource = await readFile(new URL('./src/views/Import.js', import.meta.url), 'utf8');

test('Import.js accept removed .doc and added .markdown', () => {
    // .doc should no longer be in accept
    const acceptMatch = importSource.match(/accept="([^"]+)"/);
    assert.ok(acceptMatch, 'accept attribute missing');
    const accept = acceptMatch[1];
    assert.ok(accept.includes('.markdown'), 'accept missing .markdown');
    assert.equal(accept.includes('.doc,'), false, '.doc still in accept');
    assert.equal(accept.includes(',.doc"'), false, '.doc still in accept');
});

// ── CSS ───────────────────────────────────────────────────
const css = await readFile(new URL('./style.css', import.meta.url), 'utf8');

test('CSS defines documents-file-icon with object-fit for img', () => {
    assert.match(css, /\.documents-file-icon\s*\{[\s\S]*?object-fit:\s*contain/);
});
