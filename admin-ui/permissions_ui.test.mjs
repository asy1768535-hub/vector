import * as assert from "node:assert";
import { describe, it } from "node:test";
import { readFileSync } from "node:fs";

const source = readFileSync(new URL('./src/views/Permissions.js', import.meta.url), 'utf8');
const css = readFileSync(new URL('./style.css', import.meta.url), 'utf8');

describe("Permissions.js 可靠性", () => {
    it("存在 permissionsReady 标志", () => {
        assert.ok(source.includes('permissionsReady'), 'must have permissionsReady');
    });

    it("切换用户时立即清空 matrix/initial 并置 ready=false", () => {
        // 必须在 watch(selectedUser) 中：先 delete 再置 false
        const watchBlock = source.slice(source.indexOf('watch(selectedUser'));
        const delMatrix = watchBlock.indexOf('delete matrix[slug]');
        const delInitial = watchBlock.indexOf('delete initial[slug]');
        const setReady = watchBlock.indexOf('permissionsReady.value = false');
        assert.ok(delMatrix < setReady, 'must delete matrix BEFORE setting ready=false in watch(selectedUser)');
        assert.ok(delInitial < setReady, 'must delete initial BEFORE setting ready=false');
    });

    it("success 分支置 permissionsReady = true", () => {
        // reloadUserPerms 中成功后置 true
        assert.ok(source.includes('permissionsReady.value = true'), 'must set ready=true on success');
    });

    it("failure 分支保持 permissionsReady = false 并设 permissionsError = true", () => {
        assert.ok(source.includes('permissionsError.value = true'), 'must set error=true on failure');
        // catch 中不应 set ready=true
        const catchPos = source.lastIndexOf('catch');
        const afterCatch = source.slice(catchPos, catchPos + 600);
        assert.ok(!afterCatch.includes('permissionsReady.value = true'), 'catch must NOT set ready=true');
    });

    it("加载失败时显示错误状态，不展示可编辑表格", () => {
        // permissionsError 状态下不可操作
        assert.ok(source.includes('permissionsError'), 'permissionsError flag exists');
        assert.ok(source.includes('权限加载失败'), 'error message displayed');
    });

    it("表格仅在 ready=true 时传入 paged.rows", () => {
        assert.ok(source.includes('selectedUser && permissionsReady'), 'table guarded by permissionsReady');
    });

    it("权限未就绪时不传入可编辑数据", () => {
        // loading 状态表格使用空数据和 v-loading
        assert.ok(source.includes(':data="[]"') || source.includes('v-loading="true"'), 'loading state uses empty data');
    });

    it("无变更时保存按钮禁用", () => {
        assert.ok(source.includes('!hasChanges()'), 'save disabled when no changes');
    });

    it("保存按钮同时依赖 permissionsReady", () => {
        assert.ok(source.includes('!permissionsReady'), 'save disabled when not ready');
    });

    it("重置按钮同时依赖 permissionsReady", () => {
        assert.ok(source.includes('!permissionsReady'), 'reset disabled when not ready');
    });

    it("保存部分失败后重新加载服务端权限", () => {
        const catchBlock = source.slice(source.lastIndexOf('catch'));
        assert.ok(catchBlock.includes('reloadUserPerms()'), 'catch must reload perms on partial failure');
    });

    it("保留 fetchSeq 竞态保护", () => {
        assert.ok(source.includes('fetchSeq'), 'fetchSeq preserved');
        assert.ok(source.includes('seq !== fetchSeq'), 'race guard checks fetchSeq');
    });
});

describe("Permissions.js 模板回归", () => {
    it("模板使用 permissions-workspace 等专用类", () => {
        for (const c of ['permissions-workspace', 'permissions-header', 'permissions-toolbar', 'permissions-table-card']) {
            assert.ok(source.includes(c), `template must use ${c}`);
        }
    });

    it("标题使用 sidebar:permission 图标", () => {
        assert.ok(source.includes('sidebar:permission'), 'title icon');
        assert.ok(source.includes('permissions-title-icon'), 'title icon CSS class');
    });

    it("权限工具区使用低权重管理提示", () => {
        assert.ok(source.includes('permissions-config-hint'), 'quiet management hint class');
        assert.ok(!source.includes('permissions-tools-hint'), 'hint removed from matrix tools row');
        assert.ok(source.includes('可为该用户配置各知识库的读取、写入、删除权限'), 'management hint text');
        assert.ok(!source.includes('permissions-legend--read'), 'old read legend removed');
        assert.ok(!source.includes('permissions-legend--insert'), 'old insert legend removed');
        assert.ok(!source.includes('permissions-legend--delete'), 'old delete legend removed');
    });

    it("零内联 style 属性", () => {
        const tpl = source.slice(source.indexOf('template:'));
        assert.equal((tpl.match(/\sstyle="/g) || []).length, 0, 'zero inline style');
    });

    it("搜索、ID、名称、权限摘要和分页均保留", () => {
        assert.ok(source.includes('permissions-search'), 'search');
        assert.ok(source.includes('库唯一ID'), 'slug column');
        assert.ok(source.includes('名称'), 'name column');
        assert.ok(source.includes('权限摘要'), 'summary column');
        assert.ok(source.includes('permissions-pagination'), 'pagination');
    });

    it("超管提示使用 CSS 类", () => {
        assert.ok(source.includes('permissions-super-warning-body'), 'super warning body class');
    });
});

describe("Permissions CSS 去重", () => {
    it("permissions-user-info 仅定义一次", () => {
        const matches = [...css.matchAll(/\.permissions-user-info\s*\{/g)];
        assert.equal(matches.length, 1, 'permissions-user-info defined exactly once');
    });
});

console.log('permissions_ui test passed');
