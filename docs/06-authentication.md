# 06 · 认证系统

## 双通道

一个 User 模型，两种凭据：

| 通道 | 谁用 | Header / Cookie | Backend 实现 |
|---|---|---|---|
| **JWT cookie** | Web 后台（浏览器） | Set-Cookie `vk_session` (HttpOnly, SameSite=Lax) | fastapi-users 内置 `JWTStrategy + CookieTransport` |
| **API Key (Bearer)** | Dify / 外部应用 / 脚本 | `Authorization: Bearer vk_xxx…` | 自定义 `APIKeyStrategy + BearerTransport` |

两个 backend 都解到同一个 `User` ORM 对象，下游权限校验逻辑统一。

## 数据模型

### `sys_users`（fastapi-users base + 扩展）

```python
class User(SQLAlchemyBaseUserTableUUID, Base):
    __tablename__ = "sys_users"

    # fastapi-users 自带：id(uuid), email, hashed_password,
    #                    is_active, is_superuser, is_verified

    # 扩展字段：
    username: Optional[str]      # 选填，唯一
    display_name: Optional[str]
    created_at: datetime
    deleted_at: Optional[datetime]
```

### `sys_api_keys`

```
id           UUID  PK
user_id      UUID  → sys_users(id) CASCADE
name         str   人类可读标签
key_prefix   str   明文前 12 字符（vk_ + 9 chars），用于审计 / DB 查找
key_hash     str   bcrypt 哈希
expires_at   datetime?
last_used_at datetime?
revoked_at   datetime?
created_at   datetime
```

> **明文 key 永不入库**。生成时返回一次，DB 只存 bcrypt 哈希 + 前 12 字符。

## 登录流程（cookie）

```
浏览器 → POST /auth/jwt/login   (form-urlencoded: username=email&password=xxx)
                                ↓
                       fastapi-users 验证密码
                                ↓
                  签发 JWT，设置 Set-Cookie: vk_session=<jwt>
                                ↓
浏览器 ← 200 / 204
```

后续所有请求自动带 cookie，FastAPI Depends 链解出 User。

### 注销

```
POST /auth/jwt/logout    → Set-Cookie 清空
```

## API Key 流程（Bearer）

### 签发

```
登录用户 → POST /me/api-keys  body={name:"dify-prod"}
                ↓
        生成 vk_<token_urlsafe(32)>
                ↓
        bcrypt.hashpw + 写入 sys_api_keys
                ↓
返回 {id, name, key_prefix:"vk_abcdefghi", plaintext_key:"vk_………", …}
                                                ^^ 仅此一次
```

### 调用

```
Dify → POST /retrieval  Authorization: Bearer vk_abcdefghi…
                ↓
       APIKeyStrategy.read_token:
         prefix = token[:12]
         candidates = SELECT FROM sys_api_keys
                      WHERE key_prefix=? AND revoked_at IS NULL
         for c in candidates:
             if bcrypt.checkpw(token, c.key_hash):
                 UPDATE last_used_at=now()
                 return user_manager.get(c.user_id)
         return None  → 401
```

> 同一 prefix 理论上极不可能碰撞（46 比特随机），实践上每个 prefix 几乎总是只对应 1 条候选。

### 撤销

```
登录用户 → DELETE /me/api-keys/{id}
                ↓
       UPDATE sys_api_keys SET revoked_at=now()
                ↓
立即失效（下次调用就走 candidates=[] 路径，返回 None → 401）
```

## bcrypt 直接使用，不走 passlib

历史原因：`passlib + bcrypt 5.x` 不兼容（passlib 自 2020 起停维护）。本项目用 `bcrypt` 库直调：

```python
# app/auth/api_key.py
def hash_api_key(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

def verify_api_key(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode(), hashed.encode())
    except Exception:
        return False
```

> bcrypt 有 72 字节明文上限。我们的 key 长度 ~46 字符（`vk_` + 43 base64 char），安全。

## Depends 链

```python
# app/auth/backend.py
fastapi_users = FastAPIUsers[User, uuid.UUID](
    get_user_manager,
    [cookie_backend, api_key_backend],   # 两个 backend 都能解
)

current_active_user = fastapi_users.current_user(active=True)
current_superuser   = fastapi_users.current_user(active=True, superuser=True)
```

使用：

```python
@router.get("/me/api-keys")
async def list_keys(user: User = Depends(current_active_user)): ...

@router.post("/admin/users")
async def create_user(actor: User = Depends(current_superuser)): ...
```

## 密码 / Key 安全要点

| 项 | 实现 |
|---|---|
| 密码哈希 | fastapi-users 内置（pwdlib + bcrypt） |
| API Key 哈希 | `bcrypt.hashpw` |
| Cookie 安全 | `HttpOnly` + `SameSite=Lax`，HTTPS 上把 `COOKIE_SECURE=true` |
| Key 仅返回一次 | 创建/轮换响应里有明文；列表 / 详情接口绝不返回明文 |
| 库存在性枚举防御 | 无权用户调 `/retrieval` 不存在的 `knowledge_id` 也返 `403`（不是 `404`） |

## 相关接口

| 路径 | 方法 | 用途 |
|---|---|---|
| `/auth/jwt/login` | POST | form 登录 |
| `/auth/jwt/logout` | POST | 注销 |
| `/auth/register` | POST | 注册（生产可在 `app/auth/routes.py` 注释掉禁用） |
| `/auth/forgot-password` | POST | 申请重置 |
| `/auth/reset-password` | POST | 用 token 重置密码 |
| `/users/me` | GET / PATCH | 当前用户信息 |
| `/me/api-keys` | GET / POST / DELETE | 自助 Key |
| `/admin/users` | POST | superuser 创建用户（含 `is_superuser` 参数） |
| `/admin/users/{id}` | PATCH / DELETE | 启停 / 设超管 / 软删 |

更多见 [13 API 参考](./13-api-reference.md)。
