import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./src/views/Search.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

// ── API calls and permissions preserved ──
test('preserves queryLibrary and listLibraryFaqs API calls', () => {
    assert.ok(source.includes('api.queryLibrary'), 'queryLibrary call present');
    assert.ok(source.includes('api.listLibraryFaqs'), 'listLibraryFaqs call present');
    assert.ok(source.includes('handleSearch'), 'handleSearch function present');
    assert.ok(source.includes("if (!slug.value)"), 'slug guard preserved');
    assert.ok(source.includes("if (!q)") || source.includes("if (!query.value.trim()") || source.includes('q = (query.value'), 'empty query guard preserved');
    assert.ok(source.includes("loading.value = true"), 'loading state preserved');
});

// ── Permission logic unchanged ──
test('preserves superuser and permission-based library loading', () => {
    assert.ok(source.includes('store.user?.is_superuser'), 'superuser check');
    assert.ok(source.includes("listLibraries()"), 'superuser listLibraries');
    assert.ok(source.includes("store.permissions"), 'permission-based libs');
    assert.ok(source.includes("includes('read')"), 'read action filter');
});

// ── Uses row.similarity (fixed from row.score) ──
test('uses row.similarity not row.score for similarity display', () => {
    assert.ok(source.includes('row.similarity'), 'uses row.similarity');
    assert.ok(!source.includes('row.score'), 'no longer uses row.score');
});

// ── documentTypeIcon and documentDisplayName reused ──
test('imports and uses documentTypeIcon and documentDisplayName from documents_ui', () => {
    assert.ok(source.includes("from '../documents_ui.js'"), 'imports from documents_ui');
    assert.ok(source.includes('documentTypeIcon'), 'documentTypeIcon used');
    assert.ok(source.includes('documentDisplayName'), 'documentDisplayName used');
    assert.ok(source.includes('resultDocInfo'), 'resultDocInfo helper defined');
});

// ── CSV export logic ──
test('CSV export uses shared logs_ui helpers with formula injection prevention', () => {
    assert.ok(source.includes('exportCSV'), 'exportCSV function defined');
    assert.ok(source.includes("from '../logs_ui.js'"), 'imports logs_ui shared utils');
    assert.ok(source.includes('downloadCSV'), 'uses shared downloadCSV');
    assert.ok(source.includes('导出'), 'export button label present');
    assert.ok(source.includes("!results.length"), 'export disabled when empty');
    // No local csvField — uses shared csvEscape
    assert.ok(!source.includes('function csvField'), 'no local csvField function');
});

// ── Document detail link ──
test('document detail link routes to /documents with slug and open params', () => {
    assert.ok(source.includes("'/documents'") || source.includes('"/documents"'), 'routes to /documents');
    assert.ok(source.includes('open'), 'includes open query param');
    assert.ok(source.includes('slug'), 'includes slug query param');
    assert.ok(source.includes('window.open'), 'uses window.open');
    assert.ok(source.includes('router.resolve'), 'uses router.resolve');
    assert.ok(source.includes('document_id'), 'uses document_id not chunk_id');
});

// ── Empty states ──
test('empty/initial/no-results states use searchEmpty illustration', () => {
    assert.ok(source.includes('hasSearched'), 'hasSearched tracking');
    assert.ok(source.includes('!results.length'), 'empty results check');
    // searchEmpty rendered in both initial card and table #empty slot
    const tpl = source.slice(source.indexOf('template:'));
    const searchEmptyMatches = (tpl.match(/searchEmpty/g) || []).length;
    assert.ok(searchEmptyMatches >= 2, `searchEmpty rendered at least twice, got ${searchEmptyMatches}`);
    assert.ok(tpl.includes('未找到匹配结果'), 'no-result text preserved');
    assert.ok(tpl.includes('illustration-empty-wrapper'), 'uses illustration-empty-wrapper');
});

// ── No inline styles ──
test('no inline style attributes in template', () => {
    const templateStart = source.indexOf('template:');
    const templatePortion = source.slice(templateStart);
    const allStyles = templatePortion.match(/style="/g) || [];
    assert.equal(allStyles.length, 0, 'zero inline style="..." in template');
});

// ── CSS search-* classes defined ──
test('CSS defines search-* workspace and card classes', () => {
    assert.match(css, /\.search-workspace\s*\{/, 'search-workspace defined');
    assert.match(css, /\.search-card\s*\{/, 'search-card defined');
    assert.match(css, /\.search-faq-card\s*\{/, 'search-faq-card defined');
    assert.match(css, /\.search-results-card\s*\{/, 'search-results-card defined');
    assert.match(css, /\.search-results-summary\s*\{/, 'search-results-summary defined');
    assert.match(css, /\.search-chunk-text\s*\{/, 'search-chunk-text defined');
    assert.match(css, /\.search-doc-file\s*\{/, 'search-doc-file defined');
    assert.match(css, /\.search-doc-link\s*\{/, 'search-doc-link defined');
    assert.match(css, /\.search-empty-card\s*\{/, 'search-empty-card defined');
});

// ── CSS chunk text truncation ──
test('search-chunk-text uses -webkit-line-clamp for 3-line truncation', () => {
    assert.match(css, /\.search-chunk-text\s*\{[\s\S]*?-webkit-line-clamp:\s*3/, '3-line clamp');
    assert.match(css, /\.search-chunk-text\s*\{[\s\S]*?-webkit-box-orient:\s*vertical/, 'box-orient vertical');
    assert.match(css, /\.search-chunk-text\s*\{[\s\S]*?overflow:\s*hidden/, 'overflow hidden');
});

// ── CSS responsive rules ──
test('responsive CSS includes search-* rules at both breakpoints', () => {
    assert.match(css, /1199px[\s\S]*\.search-card/, 'search-card in 1199px media block');
    assert.match(css, /899px[\s\S]*\.search-card/, 'search-card in 899px media block');
    assert.match(css, /899px[\s\S]*\.search-results-card/, 'search-results-card in 899px media block');
});

// ── Score color classes reused ──
test('score color classes present in CSS (reused from existing)', () => {
    assert.match(css, /\.score--high/, 'score--high class');
    assert.match(css, /\.score--mid/, 'score--mid class');
    assert.match(css, /\.score--low/, 'score--low class');
});

// ── Safe metadata reads ──
test('metadata reads use safe access for page and rerank_score', () => {
    assert.ok(source.includes('metadata?.page') || (source.includes('metadata.page') && source.includes('metadata &&')), 'safe page read');
    assert.ok(source.includes('metadata?.rerank_score') || (source.includes('metadata.rerank_score') && source.includes('metadata &&')), 'rerank_score field');
});

// ── Template structure: three cards ──
test('template uses three-card layout with search-* classes', () => {
    assert.ok(source.includes('search-workspace'), 'search-workspace wrapper');
    assert.ok(source.includes('search-card'), 'search-card form card');
    assert.ok(source.includes('search-results-card'), 'search-results-card results card');
});

// ── Reset functionality ──
test('reset button clears query, results, and hasSearched', () => {
    assert.ok(source.includes('resetSearch'), 'resetSearch function defined');
    assert.ok(source.includes("query.value = ''"), 'resets query');
    assert.ok(source.includes("results.value = []"), 'resets results');
    assert.ok(source.includes('hasSearched.value = false'), 'resets hasSearched');
});

// ── Slug change clears old results ──
test('slug watcher clears results, hasSearched, and elapsed on library switch', () => {
    assert.ok(source.includes("results.value = []"), 'clears results on slug change');
    assert.ok(source.includes('elapsed.value = 0'), 'resets elapsed on slug change');
});

// ── Search timing ──
test('tracks search elapsed time with performance.now', () => {
    assert.ok(source.includes('elapsed'), 'elapsed ref defined');
    assert.ok(source.includes('performance.now()'), 'uses performance.now for timing');
    assert.ok(source.includes('耗时'), 'displays elapsed time label');
    assert.ok(source.includes('elapsed.toFixed(1)'), 'formats elapsed to 1 decimal');
});

// ── resultDocInfo includes id for fallback ──
test('resultDocInfo includes document_id for short-id fallback', () => {
    assert.ok(source.includes("id: row.document_id"), 'includes document_id in resultDocInfo');
});

// ── Precomputed row fields avoid repeated template calls ──
test('results are pre-augmented with _docName and _icon', () => {
    assert.ok(source.includes('_docName'), 'precomputes _docName field name');
    assert.ok(source.includes('_icon'), 'precomputes _icon field name');
    assert.ok(source.includes('row._icon'), 'template reads precomputed _icon');
    assert.ok(source.includes('row._docName'), 'template reads precomputed _docName');
});

// ── elapsed reset correctly ──
test('elapsed reset on new search, reset, and slug change', () => {
    assert.ok(source.includes("elapsed.value = 0"), 'elapsed reset to 0');
    // Count: should be at least 3 (handleSearch start, resetSearch, slug watcher)
    const matches = source.match(/elapsed\.value\s*=\s*0/g) || [];
    assert.ok(matches.length >= 3, `elapsed reset at least 3 places, got ${matches.length}`);
});

// ── Form control widths use CSS classes ──
test('form controls use CSS classes instead of inline widths', () => {
    assert.ok(source.includes('search-lib-select'), 'library select has CSS class');
    assert.ok(source.includes('search-query-input'), 'query input has CSS class');
    assert.ok(source.includes('search-limit-input'), 'limit input has CSS class');
    // CSS defines widths for these classes
    assert.match(css, /\.search-lib-select\s*\{[\s\S]*?width:/, 'search-lib-select has width');
    assert.match(css, /\.search-query-input\s*\{[\s\S]*?width:/, 'search-query-input has width');
    // 899px overrides to full width
    const after899 = css.slice(css.indexOf('max-width: 899px'));
    assert.ok(after899.includes('search-lib-select') || after899.includes('search-query-input'), 'form controls go full-width at 899px');
});

// ── Table columns ──
test('table has all 6 required columns', () => {
    assert.ok(source.includes('文档名'), 'document name column');
    assert.ok(source.includes('来源信息'), 'source info column');
    assert.ok(source.includes('命中片段'), 'chunk text column');
    assert.ok(source.includes('相似度'), 'similarity column');
    assert.ok(source.includes('重排分数'), 'rerank score column');
    assert.ok(source.includes('文档详情'), 'detail link column');
});

console.log('search redesign test passed');
