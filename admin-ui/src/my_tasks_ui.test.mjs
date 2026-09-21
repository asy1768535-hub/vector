import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

test('my tasks uses only the current user task APIs and provides task recovery actions', () => {
    const view = readFileSync(new URL('./views/MyTasks.js', import.meta.url), 'utf8');
    const api = readFileSync(new URL('./api.js', import.meta.url), 'utf8');

    assert.match(view, /<h2>我的任务<\/h2>/);
    assert.match(view, /api\.listPersonalImportTasks/);
    assert.match(view, /api\.getPersonalImportTaskSummary/);
    assert.match(view, /api\.retryPersonalImportTask/);
    assert.match(view, /v-model="selectedLibrarySlug"/);
    assert.match(view, /v-model="statusFilter"/);
    assert.match(view, /<el-pagination/);
    assert.match(view, /@current-change="changePage"/);
    assert.match(view, /APP_PATHS\.myFiles/);
    assert.match(view, /查看文件/);
    assert.match(view, /异常文件也在这里统一显示/);
    assert.match(view, /失败原因/);
    assert.match(view, /duplicate_source/);
    assert.match(view, /已跳过：库内已有相同文件/);
    assert.match(api, /\/me\/import-tasks\?/);
    assert.match(api, /\/me\/import-task-summary\?/);
    assert.match(api, /library_slug/);
    assert.match(api, /status/);
    assert.match(api, /page/);
});
