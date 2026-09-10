import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
    myFilesAncestorPaths,
    myFilesBreadcrumbs,
    myFilesExpandedFolderPaths,
    myFilesPageCount,
    selectableUploadLibraries,
} from './my_files_ui.js';

test('my tasks builds breadcrumbs from the server directory path', () => {
    assert.deepEqual(myFilesBreadcrumbs('/项目甲/合同'), [
        { name: '我的任务', path: '' },
        { name: '项目甲', path: '/项目甲' },
        { name: '合同', path: '/项目甲/合同' },
    ]);
});

test('my tasks expands only the current directory and its ancestors', () => {
    assert.deepEqual(myFilesExpandedFolderPaths('/项目甲/合同'), [
        '/项目甲',
        '/项目甲/合同',
    ]);
});

test('my tasks reloads the parent directory branches for a deep link', () => {
    assert.deepEqual(myFilesAncestorPaths('/项目甲/合同'), ['', '/项目甲']);

    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');
    assert.match(view, /loadPersonalTaskTreePath/);
    assert.match(view, /await loadPersonalTaskTreePath\(personalTaskPath\.value\)/);
});

test('my files shows only knowledge libraries where uploads are allowed', () => {
    assert.deepEqual(selectableUploadLibraries([
        { library_slug: 'legal', library_name: '法务资料', actions: ['read', 'insert'] },
        { library_slug: 'archive', library_name: '只读归档', actions: ['read'] },
    ]), [
        { value: 'legal', label: '法务资料' },
    ]);
});

test('my files page count covers both folders and files without loading them all', () => {
    assert.equal(myFilesPageCount(2, 101, 50), 3);
    assert.equal(myFilesPageCount(30, 30, 50), 2);
    assert.equal(myFilesPageCount(0, 0, 50), 1);
});

test('my tasks replaces the old file-only page with the task browser', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');

    assert.match(view, /<h2>我的任务<\/h2>/);
    assert.match(view, /api\.listPersonalImportTaskFiles/);
    assert.match(view, /api\.getPersonalImportTaskSummary\('all'/);
    assert.match(view, /scope:\s*'all'/);
    assert.match(view, /retryPersonalTask/);
    assert.match(view, /personalTaskTreeFolders/);
    assert.doesNotMatch(view, /api\.listPersonalFiles/);
    assert.doesNotMatch(view, /最近 30 天|taskScope/);
});

test('my tasks exposes a knowledge library selector', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');

    assert.match(view, /v-model="selectedLibrarySlug"/);
    assert.match(view, /placeholder="选择知识库"/);
});

test('my tasks requests only the selected directory page and opens available files', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');

    assert.match(view, /path:\s*personalTaskPath\.value/);
    assert.match(view, /page:\s*personalTaskPage\.value \+ 1/);
    assert.match(view, /openPersonalTaskFile/);
    assert.match(view, /document:\s*task\.document_id/);
    assert.match(view, /path:\s*APP_PATHS\.catalog/);
    for (const column of ['文件', '知识库', '处理进度', '提交时间']) {
        assert.ok(view.includes(`<el-table-column label="${column}"`), `missing task column ${column}`);
    }
});

test('my tasks collapses an active folder and preserves its location for document details', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');

    assert.match(view, /personalTaskExpandedPaths/);
    assert.match(view, /personalTaskExpandedPaths\.value\.has\(folder\.path\)/);
    assert.match(view, /expandedPaths\.delete\(folder\.path\)/);
    assert.match(view, /from:\s*'my-tasks'/);
    assert.match(view, /taskPath:\s*personalTaskPath\.value/);
    assert.match(view, /route\.query\.library/);
    assert.match(view, /route\.query\.path/);
});

