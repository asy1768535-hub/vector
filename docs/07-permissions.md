# 07 · 权限系统

## 模型

`(user, library, action)` 三元组，由 **Casbin RBAC** 落到 `casbin_rule` 表。

- `sub` = `str(user.id)`（UUID）
- `obj` = `library:<slug>`（例如 `library:medical`）
- `act` ∈ `{read, insert, delete, admin}`

不写死布尔列，未来加角色 / 用户组 / 资源类型 都不用改代码，只加策略。

## Casbin model.conf

`app/casbin/model.conf`：

```ini
[request_definition]
r = sub, obj, act

[policy_definition]
p = sub, obj, act

[role_definition]
g = _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = (g(r.sub, p.sub) || r.sub == p.sub) && r.obj == p.obj && r.act == p.act
```

含义：
- `r.sub == p.sub` —— 用户 id 直接匹配策略 sub
- `g(r.sub, p.sub)` —— 或者：用户继承了一个角色，角色 sub 匹配策略 sub
- 至少有一条策略匹配则 allow

## 数据流

### 授权

```
管理员 → PUT /admin/permissions
    {user_id, library_slug:"medical", actions:["read","insert"]}
                ↓
        casbin_service.grant():
          for act in actions:
            enforcer.add_policy(str(user_id), "library:medical", act)
                ↓
        casbin_rule 表新增 2 条 p 规则
                ↓
        审计：sys_audit_log 写 action="permission.grant"
```

### 撤销

```
管理员 → DELETE /admin/permissions
    {user_id, library_slug:"medical", actions:["insert"]}
                ↓
        enforcer.remove_policy(sub, obj, "insert")
                ↓
        casbin_rule 删除该行
```

不传 `actions` 字段 → 该 (user, library) 上所有动作全清。

### 检查

每个库级接口走 `require_lib(action)` Depends：

```python
# app/deps.py
def require_lib(action: Literal['read','insert','delete']):
    async def _dep(slug, user=Depends(current_active_user), db=Depends(get_db)):
        lib = await load_active_library(slug, db)
        if lib is None:
            raise HTTPException(403, "forbidden")          # 不暴露库存在性
        if user.is_superuser:
            return lib                                       # 超管直通
        if has_permission(str(user.id), slug, action):     # casbin enforce
            return lib
        raise HTTPException(403, "forbidden")
    return _dep
```

使用：

```python
@router.post("/libraries/{slug}/documents")
async def ingest(body: Req, lib: Library = Depends(require_lib("insert"))):
    ...
```

## 超管直通

`is_superuser=True` 的用户**不查 Casbin 表**，所有库的所有动作都允许。

设计取舍：
- ✅ 不用为每个库手动给超管授权，运维简单
- ✅ 超管被设也意味着信任，单独走代码路径开销最小
- ⚠️ 不要把日常用户设超管；用普通用户 + 按库授权才是正常工作流

## 性能

- Casbin enforcer 是**单例**，启动时一次性把全部 `casbin_rule` 加载入内存
- `enforce()` 是 O(N) 字符串匹配（N = 策略条数）
- 策略条数 = 用户数 × 授权库数 × 动作数。1000 用户 × 50 库 × 3 动作 = 15 万行，内存匹配单次微秒级
- 多副本部署时策略变更：当前每副本独占内存，写策略后立即对该副本可见；其他副本要等下次 `load_policy`
  - 临时方案：在 `/admin/permissions` PUT/DELETE 之后让前端引导用户「等待 30 秒生效」
  - 永久方案：挂 Casbin watcher（Redis Pub/Sub 或 etcd），方案在路线图 P5

## 接口

| 方法 | 路径 | 用途 |
|---|---|---|
| `PUT` | `/admin/permissions` | 授权 / 扩权 |
| `DELETE` | `/admin/permissions` | 撤权（`actions` 字段不传 = 全清） |
| `GET` | `/admin/permissions?user_id=<uuid>` | 反查某用户拥有的 (库, 动作) |
| `GET` | `/admin/permissions/library/{slug}` | 反查授权了某库的所有用户 |
| `GET` | `/me/permissions` | 当前登录用户自查（前端动态菜单用） |

## Request / Response 示例

### 授权

```http
PUT /admin/permissions
Cookie: vk_session=...
Content-Type: application/json

{
  "user_id": "1cb35a3a-…-…",
  "library_slug": "medical",
  "actions": ["read", "insert"]
}
```

响应：

```json
{ "added": [["1cb35a3a-…-…", "library:medical", "read"],
            ["1cb35a3a-…-…", "library:medical", "insert"]] }
```

> 已存在的策略会被静默跳过；只返回真正新增的。

### 反查用户权限

```http
GET /admin/permissions?user_id=1cb35a3a-…-…
```

```json
[
  {"library_slug":"medical","actions":["read","insert"]},
  {"library_slug":"legal","actions":["read"]}
]
```

### 自查

```http
GET /me/permissions
Cookie: vk_session=...
```

同上格式。超管直接返回 `[]`（前端按 `store.user.is_superuser` 判定全部可见）。

## 权限矩阵 UI

后台 → 「权限矩阵」页面：

1. 顶部下拉选用户
2. 表格按库列出，每行 3 个 checkbox（read / insert / delete）
3. 切换勾选 → 在内存缓存
4. 点击「保存变更」→ 前端 diff 当前 vs 初始快照，分别调 PUT / DELETE 提交

实现见 `admin-ui/src/views/Permissions.js`。

## 测试

`tests/test_casbin_model.py` 用临时 policy 文件覆盖：

- 直接策略授权（`sub == p.sub`）
- 未授权用户拒绝
- 库与库之间隔离（`obj` 不匹配）
- 角色继承（`g(user, role)` + role 的策略生效）

不依赖 DB；只验证 `model.conf` 的逻辑正确性。
