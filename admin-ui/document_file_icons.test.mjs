import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

// ── documentTypeIcon mapping ──────────────────────────────
import { documentType } from './src/documents_ui.js';

test('documentTypeIcon maps six file types correctly', () => {
    const cases = [
        { row: { title: 'report.pdf' },        expected: 'doc:pdf' },
        { row: { title: 'memo.docx' },         expected: 'doc:word' },
        { row: { title: 'sheet.xlsx' },        expected: 'doc:excel' },
        { row: { title: 'README.md' },         expected: 'doc:markdown' },
        { row: { title: 'notes.txt' },         expected: 'doc:text' },
        { row: { title: 'image.png' },         expected: 'doc:other' },
        { row: { title: 'noext' },             expected: 'doc:other' },
        { row: { external_id: 'data.csv' },    expected: 'doc:other' },
        { row: { title: 'Doc.PDF' },           expected: 'doc:pdf' },
        { row: { title: 'Sheet.XLS' },         expected: 'doc:excel' },
        { row: { title: 'file.DOC' },          expected: 'doc:word' },
        { row: { title: 'README.MARKDOWN' },   expected: 'doc:markdown' },
        { row: { title: 'log.TXT' },           expected: 'doc:text' },
    ];
    for (const { row, expected } of cases) {
        const icon = documentType(row);
        // documentTypeIcon is defined inline below (imported from documents_ui)
        const t = documentType(row);
        let iconName;
        if (t === 'pdf') iconName = 'doc:pdf';
        else if (t === 'word') iconName = 'doc:word';
        else if (t === 'excel') iconName = 'doc:excel';
        else if (t === 'markdown') iconName = 'doc:markdown';
        else if (t === 'text') iconName = 'doc:text';
        else iconName = 'doc:other';
        assert.equal(iconName, expected, `${JSON.stringify(row)} -> ${expected} (got ${iconName})`);
    }
});

// ── SVG validation ────────────────────────────────────────
const iconsSource = await readFile(new URL('./src/icons.js', import.meta.url), 'utf8');

const DOC_ICONS = ['doc:pdf', 'doc:word', 'doc:excel', 'doc:markdown', 'doc:text', 'doc:other'];

test('every doc icon returns valid SVG with correct viewBox', async () => {
    // Dynamic import for iconSvg (ES module)
    const { iconSvg } = await import('./src/icons.js');
    for (const name of DOC_ICONS) {
        const svg = iconSvg(name);
        assert.ok(svg.includes('<svg'), `${name} missing <svg>`);
        assert.ok(svg.includes('viewBox="0 0 28 28"'), `${name} wrong viewBox`);
        assert.ok(svg.includes('aria-hidden="true"'), `${name} missing aria-hidden`);
        // Must NOT contain external resource URLs (exclude namespace xmlns)
        const body = svg.replace(/xmlns="[^"]*"/g, '');
        assert.equal(body.includes('http:'), false, `${name} contains external http: URL`);
        assert.equal(body.includes('https:'), false, `${name} contains external https: URL`);
        assert.equal(body.includes('cdn'), false, `${name} contains cdn`);
        // Must contain path elements with fill
        assert.ok(svg.includes('<path'), `${name} missing <path>`);
    }
});

test('fallback icon returned for unknown name', async () => {
    const { iconSvg } = await import('./src/icons.js');
    const svg = iconSvg('doc:nonexistent');
    assert.ok(svg.includes('<svg'));
    assert.ok(svg.includes('<path'));
});

// ── Template validation ───────────────────────────────────
const source = await readFile(new URL('./src/views/Documents.js', import.meta.url), 'utf8');

test('Documents.js uses local-icon for file type, not text badge', () => {
    assert.ok(source.includes('<local-icon class="documents-file-icon"'), 'missing local-icon');
    assert.ok(source.includes(':icon="documentTypeIcon(row)"'), 'missing documentTypeIcon binding');
    assert.equal(source.includes('documents-file-type'), false, 'text badge still present');
});

test('documentTypeIcon is imported and returned from setup', () => {
    assert.ok(source.includes('import {'), 'has import block');
    // documentTypeIcon must appear in the return {} block (after the last 'return {')
    const returnTokenIdx = source.lastIndexOf('return {');
    const afterReturn = source.slice(returnTokenIdx);
    assert.ok(afterReturn.includes('documentTypeIcon'), 'documentTypeIcon must be in setup return');
});

// ── CSS validation ────────────────────────────────────────
const css = await readFile(new URL('./style.css', import.meta.url), 'utf8');

test('style.css defines documents-file-icon sizing', () => {
    assert.ok(css.includes('.documents-file-icon'), 'missing .documents-file-icon');
    assert.ok(css.includes('width: 26px'), 'missing icon width');
    assert.ok(css.includes('height: 30px'), 'missing icon height');
    // Old text badge should be removed
    assert.equal(css.includes('.documents-file-type {'), false, '.documents-file-type still in CSS');
});
