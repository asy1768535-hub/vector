import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

import {
    myFilesBreadcrumbs,
    myFilesPageCount,
    selectableUploadLibraries,
} from './my_files_ui.js';

test('my files builds breadcrumbs from the server directory path', () => {
    assert.deepEqual(myFilesBreadcrumbs('/项目甲/合同'), [
        { name: '我的文件', path: '' },
        { name: '项目甲', path: '/项目甲' },
        { name: '合同', path: '/项目甲/合同' },
    ]);
});

test('my files shows only knowledge libraries where uploads are allowed', () => {
    assert.deepEqual(selectableUploadLibraries([
        { library_slug: 'legal', library_name: '法务资料', actions: ['read', 'insert'] },
        { library_slug: 'archive', library_name: '只读归档', actions: ['read'] },
    ]), [
        { value: 'legal', label: '法务资料' },
    ]);
});

test('my files page count covers both folders and files', () => {
    assert.equal(myFilesPageCount(2, 101, 50), 3);
    assert.equal(myFilesPageCount(30, 30, 50), 2);
    assert.equal(myFilesPageCount(0, 0, 50), 1);
});

test('my files loads saved files by library and folder', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');

    assert.match(view, /<h2>我的文件<\/h2>/);
    assert.match(view, /api\.listStoredFiles/);
    assert.match(view, /library:\s*selectedLibrarySlug\.value/);
    assert.match(view, /path:\s*selectedPath\.value/);
    assert.match(view, /page:\s*currentPage\.value/);
    assert.match(view, /document:\s*file\.document_id/);
});

test('my files stays focused on saved originals and folder navigation', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');

    assert.doesNotMatch(view, /异常文件/);
    assert.doesNotMatch(view, /api\.listFailedImportTaskFiles/);
    assert.doesNotMatch(view, /api\.retryPersonalImportTask/);
    assert.doesNotMatch(view, /failedMode/);
});

test('my files preserves its folder as the document-detail return target', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');

    assert.match(view, /from:\s*'my-tasks'/);
    assert.match(view, /taskPath:\s*selectedPath\.value/);
});

test('my files restores the return library and folder on entry', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');

    assert.match(view, /useRoute/);
    assert.match(view, /route\.query\.library/);
    assert.match(view, /route\.query\.path/);
    assert.match(view, /selectedLibrarySlug\.value = returnLibrary/);
    assert.match(view, /selectedPath\.value = returnPath/);
});

test('my files downloads originals through a scoped signed-url API and deletes storage-only files', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');
    const api = readFileSync(new URL('./api.js', import.meta.url), 'utf8');

    assert.match(view, /api\.getStoredFileDownloadUrl\(/);
    assert.match(view, /api\.deleteStoredFile\(/);
    assert.match(
        view,
        /<template v-if="file\.document_id">[\s\S]*?openReplace\(file\)[\s\S]*?<\/template>\s*<el-button text type="danger" size="small" @click\.stop="deleteFile\(file\)">删除<\/el-button>/,
    );
    assert.match(api, /\/me\/stored-files\/\$\{fileResourceId\}\/download/);
    assert.match(api, /\/me\/stored-files\/\$\{fileResourceId\}/);
});

test('my files can select storage-only originals and delete the current folder', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');

    assert.match(view, /const selectedFileResourceIds = ref\(new Set\(\)\)/);
    assert.match(view, /isFileSelected\(file\.file_resource_id\)/);
    assert.match(view, /file\.document_id\s*\?\s*api\.deleteDocument\(selectedLibrarySlug\.value, file\.document_id\)\s*:\s*api\.deleteStoredFile\(selectedLibrarySlug\.value, file\.file_resource_id\)/);
    assert.match(view, /api\.deletePersonalFileFolder\(selectedLibrarySlug\.value, selectedPath\.value\)/);
    assert.match(view, /@click="deleteCurrentFolder"/);
});

test('my files has a knowledge library selector and folder browser', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');

    assert.match(view, /v-model="selectedLibrarySlug"/);
    assert.match(view, /class="my-files-tree"/);
    assert.match(view, /class="my-files-breadcrumb"/);
    assert.match(view, /my-files-file-entry/);
    assert.doesNotMatch(view, /api\.listFolders/);
});

test('my files combines saved originals with knowledge asset metadata', () => {
    const view = readFileSync(new URL('./views/MyFiles.js', import.meta.url), 'utf8');

    assert.match(view, /api\.listCatalogDocuments/);
    assert.match(view, /documentStatusLabel/);
    assert.match(view, /catalogOverallLabel/);
    assert.match(view, /\['search', 'graph'\]/);
    assert.match(view, /search: '向量'/);
    assert.doesNotMatch(view, /\['summary', 'outline', 'classification', 'graph'\]/);
    assert.match(view, /capability\.label \}\}：\{\{ capabilityStateLabel/);
    assert.doesNotMatch(view, /my-files-graph-count/);
    assert.match(view, /@click\.stop="openReplace\(file\)"/);
    assert.match(view, /class="my-files-entry-date"/);
    assert.match(view, />上传时间<\/small>/);
    assert.match(view, /my-files-version">v\{\{ file\.catalog\.revision_no \}\}</);
    assert.doesNotMatch(view, /my-files-entry-uploader|uploader\?\.display_name/);
    assert.match(view, /仅保存，尚未转写\/向量化/);
    assert.doesNotMatch(view, /@click\.stop="deleteFolder\(folder\)"/);
    assert.match(view, /class="my-files-content-tools"/);
    assert.match(view, /删除选中文件/);
});

test('my files keeps dense rows and all desktop actions in one action group', () => {
    const css = readFileSync(new URL('../style.css', import.meta.url), 'utf8');

    assert.match(css, /\.my-files-file-entry\s*\{[^}]*grid-template-columns:\s*24px minmax\(220px, 1\.8fr\) minmax\(145px, \.8fr\) max-content/);
    assert.match(css, /\.my-files-entry-actions\s*\{\s*display:\s*flex/);
    assert.match(css, /\.my-files-entry\s*\{[^}]*border:\s*1px solid var\(--el-border-color-lighter\)[^}]*border-radius:\s*6px/);
    assert.match(css, /\.my-files-entry-actions\s*\{[^}]*border-left:\s*1px solid var\(--el-border-color-lighter\)/);
    assert.match(css, /\.my-files-entry-date\s*\{[^}]*border-left:\s*1px solid var\(--el-border-color-lighter\)/);
    assert.match(css, /\.my-files-entry\s*\.el-button\s*\{[^}]*min-height:\s*24px/);
    assert.match(css, /\.my-files-asset-meta\s*\{[^}]*flex-wrap:\s*nowrap/);
});
