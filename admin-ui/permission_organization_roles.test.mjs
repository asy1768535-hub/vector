import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { registerHooks } from 'node:module';
import test from 'node:test';
import { nextTick } from './vendor/vue.esm-browser.prod.js';

let instance = 0;
const dataModule = (source) => `data:text/javascript,${encodeURIComponent(source)}`;
const vueUrl = new URL('./vendor/vue.esm-browser.prod.js', import.meta.url).href;
const organization = { membership_id: 'member-a', organization_id: 'org-a', name: '示例组织',
    role: 'organization_admin', status: 'active', can_manage: true };
const library = { slug: 'private', name: '示例库', organization_id: 'org-a' };

async function component(t, overrides = {}) {
    const messages = [];
    const mutations = [];
    const harness = {
        messages,
        async confirm() {},
        api: {
            async listUserPerms() { return []; },
            async listUserOrganizationRoles() { return [{ ...organization }]; },
            async updateOrganizationMember(...args) { mutations.push(args); },
            async grantPerms() { throw new Error('Unexpected explicit permission grant'); },
            async revokePerms() { throw new Error('Unexpected explicit permission revoke'); },
            ...overrides,
        },
    };
    globalThis.__PERMISSION_ROLE_HARNESS__ = harness;
    const stubs = new Map([
        ['vue', dataModule(`export * from ${JSON.stringify(vueUrl)}; export const onMounted = () => {};`)],
        ['element-plus', dataModule(`
            const h = () => globalThis.__PERMISSION_ROLE_HARNESS__;
            export const ElMessage = Object.fromEntries(['success','warning','error'].map(k => [k, m => h().messages.push([k,m])]));
            export const ElMessageBox = { confirm: (...a) => h().confirm(...a) };
        `)],
        ['../api.js', dataModule(`
            const h = () => globalThis.__PERMISSION_ROLE_HARNESS__.api;
            export const listUsers = async () => [];
            export const listLibraries = async () => [];
            ${['listUserPerms','listUserOrganizationRoles','updateOrganizationMember','grantPerms','revokePerms']
                .map(name => `export const ${name} = (...args) => h().${name}(...args);`).join('\n')}
        `)],
    ]);
    const hooks = registerHooks({ resolve(specifier, context, nextResolve) {
        if (context.parentURL?.includes('/views/Permissions.js') && stubs.has(specifier)) {
            return { url: stubs.get(specifier), shortCircuit: true };
        }
        return nextResolve(specifier, context);
    } });
    t.after(() => { hooks.deregister(); delete globalThis.__PERMISSION_ROLE_HARNESS__; });
    const { default: Permissions } = await import(`./src/views/Permissions.js?roles-test=${++instance}`);
    const view = Permissions.setup();
    view.libs.value = [library, { ...library, slug: 'other', organization_id: 'org-b' }];
    view.users.value = [{ id: 'user-a', is_superuser: false, is_active: true }];
    view.selectedUser.value = 'user-a';
    await nextTick();
    await new Promise(resolve => setImmediate(resolve));
    return { view, harness, mutations };
}

test('organization-admin read is visible without granting explicit permissions and stays in its organization', async t => {
    const { view } = await component(t);
    assert.match(view.permissionRemark({}, library), /组织管理员/);
    assert.equal(view.permissionRemark({}, { ...library, organization_id: 'org-b' }), '无访问权限');
    assert.equal(view.matrix.private.read, false);
    assert.equal(view.hasChanges(), false);
});

test('saving a role freezes the membership and expected state and restores explicit-only access after demotion', async t => {
    let role = 'organization_admin';
    const { view, mutations } = await component(t, {
        async listUserOrganizationRoles() { return [{ ...organization, role }]; },
        async updateOrganizationMember(...args) { mutations.push(args); role = args[2].role; },
    });
    const row = view.organizationRoles.value[0];
    row.draftRole = 'member';
    await view.saveOrganizationRole(row);
    assert.deepEqual(mutations, [['org-a', 'member-a', {
        expected_role: 'organization_admin', expected_status: 'active', role: 'member', status: 'active',
    }]]);
    assert.equal(view.permissionRemark({}, library), '无访问权限');
    assert.equal(view.hasChanges(), false);
});

test('organization-role read failure disables all editing instead of presenting missing roles as ordinary membership', async t => {
    const { view } = await component(t, { async listUserOrganizationRoles() { throw new Error('role read unavailable'); } });
    assert.equal(view.permissionsReady.value, false);
    assert.equal(view.permissionsError.value, true);
});

test('non-manageable roles cannot produce a mutation', async t => {
    const { view, mutations } = await component(t, {
        async listUserOrganizationRoles() { return [{ ...organization, can_manage: false }]; },
    });
    const row = view.organizationRoles.value[0];
    row.draftRole = 'member';
    await view.saveOrganizationRole(row);
    assert.deepEqual(mutations, []);
});

test('a late role response cannot overwrite a subsequently selected user', async t => {
    let release;
    const late = new Promise(resolve => { release = resolve; });
    const { view } = await component(t, {
        async listUserOrganizationRoles(userId) { return userId === 'user-a' ? late : []; },
    });
    view.selectedUser.value = 'user-b';
    await nextTick();
    await new Promise(resolve => setImmediate(resolve));
    release([{ ...organization }]);
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(view.organizationRoles.value, []);
    assert.equal(view.permissionsReady.value, true);
});

test('template presents role settings and inherited reads separately from explicit permissions', () => {
    const source = readFileSync(new URL('./src/views/Permissions.js', import.meta.url), 'utf8');
    assert.match(source, /保存组织角色/);
    assert.match(source, /组织管理员自动读取本组织全部知识库/);
    assert.match(source, /:model-value="matrix\[row.slug\].read \|\| organizationRead\(row\)"/);
});

test('canceling a role change preserves saved role and produces no request', async t => {
    const { view, harness, mutations } = await component(t);
    harness.confirm = async () => { throw 'cancel'; };
    const row = view.organizationRoles.value[0];
    row.draftRole = 'member';
    await view.saveOrganizationRole(row);
    assert.deepEqual(mutations, []);
    assert.equal(view.organizationRead(library), true);
    assert.equal(view.saving.value, false);
});

test('state conflict reloads authority and explains the failure without leaving stale inherited access', async t => {
    let role = 'organization_admin';
    const { view, harness } = await component(t, {
        async listUserOrganizationRoles() { return [{ ...organization, role }]; },
        async updateOrganizationMember() {
            role = 'member';
            throw { status: 409, body: { detail: 'organization_membership_state_changed' } };
        },
    });
    const row = view.organizationRoles.value[0];
    row.draftRole = 'member';
    await view.saveOrganizationRole(row);
    assert.equal(view.organizationRead(library), false);
    assert.equal(view.saving.value, false);
    assert.match(harness.messages.find(([kind]) => kind === 'error')[1], /其他管理员修改/);
});

test('explicit read survives role demotion without being added or revoked by role saving', async t => {
    let role = 'organization_admin';
    const { view } = await component(t, {
        async listUserPerms() { return [{ library_slug: 'private', actions: ['read'] }]; },
        async listUserOrganizationRoles() { return [{ ...organization, role }]; },
        async updateOrganizationMember(_organization, _membership, body) { role = body.role; },
    });
    view.organizationRoles.value[0].draftRole = 'member';
    await view.saveOrganizationRole(view.organizationRoles.value[0]);
    assert.equal(view.permissionRemark(view.matrix.private, library), '仅查询');
    assert.equal(view.hasChanges(), false);
});

test('organization-role API uses cookie credentials and submits the guarded membership route', async () => {
    const api = await import('./src/api.js');
    const originalFetch = globalThis.fetch;
    const requests = [];
    globalThis.fetch = async (path, options) => {
        requests.push({ path, options });
        return new Response(JSON.stringify([]), { status: 200, headers: { 'Content-Type': 'application/json' } });
    };
    try {
        await api.listUserOrganizationRoles('user/a');
        await api.updateOrganizationMember('org/a', 'member/a', {
            expected_role: 'member', expected_status: 'active', role: 'organization_admin', status: 'active',
        });
    } finally { globalThis.fetch = originalFetch; }
    assert.equal(requests[0].path, '/admin/permissions/users/user%2Fa/organizations');
    assert.equal(requests[0].options.credentials, 'include');
    assert.equal(requests[1].path, '/organizations/org%2Fa/members/member%2Fa');
    assert.equal(requests[1].options.method, 'PATCH');
    assert.equal(JSON.parse(requests[1].options.body).expected_role, 'member');
});
