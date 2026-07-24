# 12 · 管理后台功能说明

## 文档边界

本文档是当前管理后台的功能契约，说明每个页面的用途、访问条件、主要操作、
重要状态和后端 API 能力。它不限定配色、排版、组件库或具体视觉设计。

管理后台是 `admin-ui/` 下的零构建 Vue 3 SPA，由 FastAPI 挂载到
`/console/`。运行时依赖全部位于 `admin-ui/vendor/`，不需要 Vite、npm
构建或公网 CDN。

入口：

```text
http://<host>:8100/console/
```

## 身份与权限

后台使用同源 Cookie 会话。启动和登录后读取：

- `GET /users/me`：当前用户。
- `GET /me/permissions`：用户对各知识库的有效动作权限。
- `GET /me/organizations`：用户的组织身份。

权限分为四类：

| 权限 | 适用能力 |
|---|---|
| 已登录用户 | 账户资料、修改密码、自己的 API Key |
| Library 动作 | `read`、`insert`、`delete`、`admin` |
| Organization 管理员 | 联邦检索诊断和组织级管理能力 |
| 平台超级管理员 | 用户、权限、库配置、运维和审计 |

平台超级管理员身份本身不代表可以读取客户知识内容。知识目录、知识图谱、
Schema 和分类审核继续依据有效 Library/Organization 权限判断。

会话失效或任一请求返回 `401` 时，前端清空用户、权限和组织状态，跳到登录页，
并记录原地址以便重新登录后返回。

## 导航结构

后台将 19 个页面入口整合为 9 个功能域。侧栏只显示当前用户有权访问的
7 个业务域；账户设置从顶部用户菜单进入。

| 功能域 | 规范路由 | 子页面 |
|---|---|---|
| 登录 | `#/login` | 登录 |
| 知识使用 | `#/knowledge-use/*` | 智能问答、单库检索、联邦检索诊断 |
| 知识资产 | `#/knowledge-assets/*` | 文档、知识目录、导入与替换 |
| 知识治理 | `#/knowledge-governance/*` | 知识图谱、Schema 管理、分类审核 |
| 用户与权限 | `#/users-permissions/*` | 用户管理、权限矩阵 |
| 库管理 | `#/libraries` | 库配置 |
| 运维中心 | `#/operations-center/*` | 概览、服务与队列、任务 |
| 审计中心 | `#/audit-center/*` | 操作审计、问答审计 |
| 账户设置 | `#/account/*` | 个人资料、API Key |

每个功能域打开时跳到该用户可访问的第一个子页面。默认首页优先级为：

1. 平台超级管理员进入运维概览。
2. 有知识使用权限时进入智能问答或该域第一个可访问页面。
3. 只有资产或治理权限时进入对应域第一个可访问页面。
4. 没有业务权限时进入个人资料。

17 个旧地址继续作为兼容入口，保留 query 和 hash 后跳到规范路由：

```text
#/chat                    -> #/knowledge-use/chat
#/search                  -> #/knowledge-use/search
#/retrieval-test          -> #/knowledge-use/retrieval-test
#/documents               -> #/knowledge-assets/documents
#/catalog                 -> #/knowledge-assets/catalog
#/import                  -> #/knowledge-assets/import
#/knowledge-graph         -> #/knowledge-governance/graph
#/schema-lifecycle        -> #/knowledge-governance/schema
#/classification-review   -> #/knowledge-governance/classification
#/users                   -> #/users-permissions/users
#/permissions             -> #/users-permissions/permissions
#/dashboard               -> #/operations-center/overview
#/operations              -> #/operations-center/status
#/jobs                    -> #/operations-center/jobs
#/audit                   -> #/audit-center/operations
#/chat-logs               -> #/audit-center/chat
#/api-keys                -> #/account/api-keys
```

## 1. 登录

路由：`#/login`

用途：建立后台 Cookie 会话。

功能：

- 输入企业邮箱和密码。
- 调用 `POST /auth/jwt/login`，请求体为 form-urlencoded。
- 登录成功后刷新用户、权限和组织信息。
- 返回登录前地址；没有原地址时按默认首页规则进入后台。

重要状态：字段校验、登录中、凭据错误、服务不可用。

## 2. 知识使用

### 智能问答

路由：`#/knowledge-use/chat`

权限：任一 Library `read`；平台超级管理员沿用现有问答入口规则。

用途：围绕一个知识库进行多轮问答，并查看回答的引用来源。

功能：

- 选择知识库、调整检索数量并发送问题。
- 流式显示 Markdown 回答和引用。
- 查看会话历史、继续会话、新建、归档或删除会话。
- 打开引用片段和完整来源文档。
- 刷新可用知识库。

API：`/chat/libraries`、`/chat/conversations*`、`POST /chat/stream`、
`/libraries/{slug}/documents/{document_id}/source*`。

### 单库检索

路由：`#/knowledge-use/search`

权限：任一 Library `read`。

用途：在一个知识库内执行直接检索，检查命中片段和相似度。

功能：

- 选择知识库、输入关键词、设置最大返回数。
- 搜索、重置和导出结果。
- 查看命中文档、片段位置和完整来源。

API：`POST /libraries/{slug}/query` 和文档来源读取接口。

### 联邦检索诊断

路由：`#/knowledge-use/retrieval-test`

权限：Organization 管理员。

用途：在一个组织的多个知识库之间检查兼容性并运行受控检索诊断。

功能：

- 选择组织和 Library 范围。
- 输入查询文本，配置模式、数量和诊断参数。
- 检查所选 Library 是否兼容。
- 运行诊断、重置参数并查看分库结果、跳过原因和固定错误。

API：`POST /me/library-compatibility/check`、
`POST /organizations/{organization_id}/retrieval-tests`。

## 3. 知识资产

### 文档

路由：`#/knowledge-assets/documents`

权限：任一 Library `read`；写入、删除按钮继续按对应动作控制。

用途：管理知识库中的文档、处理状态和原始内容。

功能：

- 选择知识库，按关键词、状态、类型和日期筛选文档。
- 查看文档统计、列表、元数据、处理任务和完整来源。
- 下载原始文件。
- 有 `insert` 时提交文本、编辑元数据、进入文件导入。
- 有 `delete` 时删除文档。
- 刷新当前 Library 数据。

API：`/libraries/{slug}/documents*`、`/libraries/{slug}/stats`、
文档 source、jobs 和 file 下载接口。

### 知识目录

路由：`#/knowledge-assets/catalog`

权限：有效 Library `read`，不使用平台超级管理员绕过客户内容权限。

用途：以只读方式检查文档当前版本、处理阶段、分类、图谱知识和原文证据。

功能：

- 按文档状态、分类状态和分类标签筛选。
- 使用游标前后翻页并打开文档详情。
- 查看文件、处理阶段、失败状态、分类结果、实体和关系。
- 从实体或关系打开精确 Evidence 原文窗口。
- 对允许重试的处理阶段执行带状态围栏的重试。

API：`/libraries/{slug}/catalog/documents*`、catalog Evidence、文件访问和
document processing/retry 接口。

### 导入与替换

路由：`#/knowledge-assets/import`

权限：任一 Library `insert`。

用途：批量上传受支持文件，新增内容或替换已有文档。

功能：

- 选择目标知识库和“新增/替换”模式。
- 拖放或选择文件，校验格式、大小、数量和批内重复。
- 替换模式下匹配或指定已有文档。
- 上传、查看逐文件进度、成功、冲突和失败结果。

API：`POST /libraries/{slug}/import-file` 及文档列表接口。

## 4. 知识治理

### 知识图谱

路由：`#/knowledge-governance/graph`

权限：有效 Library `read`；提交事实需要 `insert`；审核和发布需要
Organization 管理员或精确 Library `admin`。

用途：检索有证据支撑的实体/关系，处理治理动作，管理 Publication，并执行
有界的已发布图谱探查。

功能：

- 在 1 到 20 个 Library 范围内检索实体和关系。
- 查看详情、别名、关联关系、文档和 Evidence。
- 提交或修正实体/关系；管理者可停用、恢复、合并和重新抽取。
- 在待审核页通过、驳回或取消治理动作。
- 预览、创建、激活、取消和回滚 Publication。
- 选择最多 4 个已发布实体，按跳数、方向、关系类型和数量上限探查图谱。

API：Organization Graph Catalog、Library graph-governance、v0.5
graph-publications、v0.6 published graph query 和 Catalog Evidence 接口。

### Schema 管理

路由：`#/knowledge-governance/schema`

权限：Organization 管理员或精确 Library `admin`。

用途：管理一个 Library 的 Ontology/Schema 版本和发布生命周期。

功能：

- 选择 Library 和 Schema 版本。
- 查看概览、实体类型、关系类型、属性、约束和影响分析。
- 从活动版本克隆草稿。
- 在草稿中新增、编辑或停用四类 Schema 项。
- 校验草稿、查看影响并确认激活。

API：`/libraries/{slug}/schema-lifecycle/versions*` 及其 validate、impact、
clone、activate 和 item 命令。

### 分类审核

路由：`#/knowledge-governance/classification`

权限：Organization 管理员或精确 Library `admin`。

用途：审核自动分类提议并形成当前有效分类决策。

功能：

- 选择可管理的知识库并加载待处理队列。
- 查看分类提议、置信度、原因和来源文档。
- 接受提议、调整主/次分类后提交，或驳回。
- 上一页、下一页和重新加载。

API：`GET /libraries/{slug}/classifications/reviews`、
`POST /libraries/{slug}/classifications/runs/{run_id}/review`。

## 5. 用户与权限

### 用户管理

路由：`#/users-permissions/users`

权限：平台超级管理员。

用途：管理系统用户身份和平台角色。

功能：筛选和分页、新建用户、编辑资料、启停账号、切换超级管理员、重置密码、
删除用户，以及创建时授权知识库。

API：`/admin/users*`、`/admin/users/{id}/reset-password` 和权限接口。

### 权限矩阵

路由：`#/users-permissions/permissions`

权限：平台超级管理员。

用途：按用户配置各 Library 的 `read`、`insert`、`delete`、`admin` 权限。

功能：选择用户、加载权限矩阵、勾选动作、计算差异、保存或重置。

API：`GET/PUT/DELETE /admin/permissions`。

## 6. 库管理

路由：`#/libraries`

权限：平台超级管理员。

用途：管理 Library 的基础配置、检索模式和处理能力。

功能：

- 按状态和检索模式筛选，包括查看已删除 Library。
- 新建、编辑和软删除 Library。
- 配置名称、分片参数、embedding、检索和图谱处理选项。
- 测试 embedding 配置、重建向量集合。
- 管理 Library FAQ。

API：`/admin/libraries*`、rebuild、embedding test 和
`/admin/libraries/{slug}/faqs*`。

## 7. 运维中心

### 概览

路由：`#/operations-center/overview`

权限：平台超级管理员。

用途：汇总知识库、任务、服务健康和最近活动。

功能：刷新统计，查看服务状态、任务概况、最近审计活动和最近创建的知识库，
并跳到对应详情页。

API：`/admin/libraries`、`/admin/jobs/stats`、`/admin/operations/status`、
`/admin/audit-log`、`/health`。

### 服务与队列

路由：`#/operations-center/status`

权限：平台超级管理员。

用途：监控 API、Embedding Worker、Cleanup Worker 和后台队列。

功能：手动刷新、30 秒自动刷新；查看实例、心跳、任务/Outbox 统计和重建进度。

API：`GET /admin/operations/status`。

### 任务

路由：`#/operations-center/jobs`

权限：平台超级管理员。

用途：检查和处理后台任务。

功能：按状态和 Library 筛选、分页、查看错误摘要、重试单个任务、重置失败任务。

API：`/admin/jobs`、`/admin/jobs/stats`、retry 和 reset-failed 接口。

## 8. 审计中心

### 操作审计

路由：`#/audit-center/operations`

权限：平台超级管理员。

用途：追踪关键管理操作。

功能：按时间、动作、操作者和目标类型筛选；展开详情；刷新和导出 CSV。

API：`GET /admin/audit-log`。

### 问答审计

路由：`#/audit-center/chat`

权限：平台超级管理员。

用途：检查用户检索和问答行为及引用效果。

功能：按 Library、用户、时间、改写和引用状态筛选；展开问题、回答、错误和来源；
刷新和导出 CSV。

API：`GET /admin/chat-logs` 和 Library 名称读取接口。

## 9. 账户设置

### 个人资料

路由：`#/account/profile`

权限：已登录用户。

用途：维护自己的资料和密码，并查看当前访问范围摘要。

功能：修改用户名/显示名称、查看组织和 Library 权限、修改密码。修改密码成功后
退出当前会话并要求重新登录。

API：`PATCH /users/me`、`POST /auth/jwt/logout`。

### API Key

路由：`#/account/api-keys`

权限：已登录用户。

用途：管理自己的 API 访问密钥。

功能：查看状态和使用时间、创建可选过期时间的 Key、仅一次显示并复制明文、
撤销 Key。

API：`GET/POST /me/api-keys`、`DELETE /me/api-keys/{id}`。

## 历史能力映射

该表用于证明 v0.9 的 19 项历史能力没有因整合丢失。个人资料是账户设置中
补充的当前用户能力，不额外占用侧栏入口。

| v0.9 历史页面 | 当前功能域 | 处理方式 |
|---|---|---|
| 登录 | 登录 | 保留 |
| 智能问答 | 知识使用 | 域内子页面 |
| 数据检索 | 知识使用 | 域内子页面 |
| 检索诊断 | 知识使用 | 组织管理员子页面 |
| 文档 | 知识资产 | 资产列表、详情和写操作 |
| 知识目录 | 知识资产 | 只读目录、Evidence 和修订 |
| 导入数据 | 知识资产 | 文件导入与替换流程 |
| 知识图谱 | 知识治理 | 独立治理子模块 |
| Schema 管理 | 知识治理 | 独立 Schema 子模块 |
| 分类审核 | 知识治理 | 独立分类子模块 |
| 用户管理 | 用户与权限 | 用户生命周期子页面 |
| 权限矩阵 | 用户与权限 | 授权子页面 |
| 库管理 | 库管理 | 保留 |
| 概览 | 运维中心 | 域内概览 |
| 任务监控 | 运维中心 | 任务明细子页面 |
| 运行状态 | 运维中心 | 服务与队列子页面 |
| 审计日志 | 审计中心 | 操作审计子页面 |
| 问答日志 | 审计中心 | 问答审计子页面 |
| 我的 API Key | 账户设置 | 安全凭据子页面 |

## 重复能力归属

| 可能重复的能力 | 唯一事实归属 | 其他功能域如何使用 |
|---|---|---|
| Organization/Library 可用范围 | 身份与权限投影 | 各页面复用，不保存第二份授权事实 |
| 文档、修订、来源和 Evidence | 知识资产 | 问答、检索、图谱、分类和审计跳回统一详情 |
| 文档处理阶段 | 知识目录 | 文档页只展示摘要，重试继续使用目录状态围栏 |
| 后台任务事实 | 运维中心 | 文档详情只展示当前文档的任务上下文 |
| 用户授权写入 | 用户与权限 | 库管理只读取相关配置，不复制授权规则 |
| 服务与队列状态 | 运维中心 | 其他页面只显示当前操作所需的局部错误 |
| 图谱、Schema、分类状态机 | 知识治理中的独立子模块 | 共享范围，但不合并命令、状态哈希或冲突处理 |
| API Key 与个人资料 | 账户设置 | 不再作为独立侧栏业务域 |

## 维护约束

- 新页面必须先确定归属功能域和叶子权限，不直接增加新的一级侧栏入口。
- 菜单隐藏不替代路由守卫和后端鉴权。
- Library 上下文在知识资产子页面间切换时必须保留；文档页使用 `slug`，
  其他资产页使用 `library`。
- 外部书签可能仍使用旧地址，删除兼容重定向前必须经过明确迁移。
- 新 API 调用统一添加到 `admin-ui/src/api.js`，继续使用 Cookie、固定错误映射和
  请求字段 allowlist。
- 客户内容权限、状态哈希、Publication Manifest、幂等键和 `409` 处理以服务端
  返回为准，前端不得放宽或自动重放冲突命令。
