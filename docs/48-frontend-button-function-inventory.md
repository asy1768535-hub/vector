# 48 - 前端按钮功能清单

更新日期：2026-08-17

## 文档边界

本文从用户在管理后台实际看到并可点击的按钮、菜单项、标签和行操作出发，说明
当前项目能做什么。它是前端可操作功能的详细清单；页面归属和路由以
[管理后台 UI](./12-admin-ui.md) 为准，完整服务端接口以
[API 完整参考](./13-api-reference.md) 为准。

清单依据以下当前代码生成并人工核对：

- 页面与按钮：`admin-ui/src/views/`、`admin-ui/src/components/`
- 导航与权限：`admin-ui/src/domain_navigation.js`、`admin-ui/src/menu_access.js`
- 前端 API：`admin-ui/src/api.js`
- 后端路由：`app/api/`

“纯前端”表示操作只改变当前页面、路由或下载浏览器生成的文件，不会调用后端；
这类按钮不是假功能。取消、关闭、展开、上一页、下一页和失败后的重新加载采用
统一交互，不在每个弹窗中重复列出。

## 权限速记

| 标记 | 含义 |
| --- | --- |
| 已登录 | 任意有效后台会话 |
| `read` | 对所选知识库有读取权限 |
| `insert` | 对所选知识库有写入或导入权限 |
| `delete` | 对所选知识库有删除文档权限 |
| 库管理 | 对该知识库有精确 `admin`，或是所属组织的 `organization_admin` |
| 超级管理员 | 平台 `is_superuser=true`；不自动绕过客户内容权限 |

## 全局导航

| 按钮或入口 | 用户结果 | 权限 / API |
| --- | --- | --- |
| 左侧各功能菜单 | 进入该用户有权访问的叶子页面 | 纯前端路由；菜单隐藏不替代后端鉴权 |
| 展开/收起侧栏 | 调整工作区宽度 | 纯前端 |
| 账户设置 | 进入个人资料页 | 已登录；纯前端路由 |
| 退出登录 | 清除 Cookie 会话并返回登录页 | 已登录；`POST /auth/jwt/logout` |
| 刷新 / 重试 / 重新加载 | 重新读取当前页面数据并绕过短时缓存 | 使用当前页面原读取 API，不产生新业务对象 |

## 登录

路由：`#/login`

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 登录 | 校验邮箱和密码，建立 Cookie 会话，进入原目标页或默认首页 | `POST /auth/jwt/login` |

项目没有“忘记密码”“手机号登录”“公开注册”按钮。

## 知识使用

### 智能问答

路由：`#/knowledge-use/chat`；进入条件：任一知识库 `read`。

| 按钮或动作 | 用户结果 | API |
| --- | --- | --- |
| 新建会话 | 清空当前对话并准备新问题 | 纯前端；首次发送时由服务端建立会话 |
| 会话历史 | 在窄屏打开或关闭历史列表 | 纯前端 |
| 选择历史会话 | 加载该会话并继续追问 | `GET /chat/conversations/{id}/messages` |
| 加载更早消息 | 向前读取历史消息 | 同上，使用游标参数 |
| 归档 | 从活动会话列表归档该会话 | `POST /chat/conversations/{id}/archive` |
| 删除 | 确认后删除该会话 | `DELETE /chat/conversations/{id}` |
| 图谱辅助检索 | 控制本次问题是否使用知识库允许的图谱关系证据，默认开启 | 随 `POST /chat/stream` 发送 `use_graph` |
| 发送 | 流式生成基于所选知识库的回答和引用 | `POST /chat/stream` |
| 复制回答 / 复制片段 | 写入系统剪贴板 | 纯前端 |
| 查看出处 | 打开引用在源文档中的精确位置 | `GET /libraries/{slug}/documents/{id}/source` |
| 查看知识图谱 / 知识图谱 | 显示引用对应的已发布实体、关系和证据 | graph-context 与 v0.6 published graph query |
| 刷新 | 重新读取可问答知识库 | `GET /chat/libraries` |

### 单库检索

路由：`#/knowledge-use/search`；进入条件：任一知识库 `read`。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 搜索 / 重试搜索 | 在所选知识库检索相似片段 | `POST /libraries/{slug}/query` |
| 重置 | 清空查询、结果和本页参数 | 纯前端 |
| 文档详情 | 在新标签打开统一知识资产详情 | 纯前端路由到 `#/knowledge-assets/catalog` |
| 导出结果 | 下载当前命中结果 CSV | 纯前端生成；包含公式注入防护 |

### 联邦检索诊断

路由：`#/knowledge-use/retrieval-test`；进入条件：组织管理员。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 检查兼容性 / 重试 | 检查所选知识库能否放入同一次联邦检索 | `POST /me/library-compatibility/check` |
| 运行检索 | 执行组织内受控检索并显示分库结果、跳过原因和诊断 | `POST /organizations/{organization_id}/retrieval-tests` |
| 重置参数 | 恢复本页默认范围和检索参数 | 纯前端 |

## 知识资产

### 知识资产目录

路由：`#/knowledge-assets/catalog`；进入条件：对客户知识库有有效 `read`。
文档浏览、处理诊断和分类审核统一在此页面，不再存在第二套文档管理或分类页面。

| 按钮或行操作 | 用户结果 | 权限 / API |
| --- | --- | --- |
| 筛选 / 重置 | 按关键词、处理状态、分类状态和标签重读列表 | `read`；catalog documents API |
| 文档标题 / 详情 / 审核 | 打开文档当前修订、处理状态、分类、实体和关系 | `read`；catalog document API |
| 文件导入 | 携带当前知识库进入导入页 | `insert`；纯前端路由 |
| 提交文本 | 新建文本知识文档并异步向量化 | `insert`；`POST /libraries/{slug}/documents` |
| 编辑 / 保存文本覆盖并重新向量化 | 更新正文和元数据并重新入队 | `insert`；`PUT /libraries/{slug}/documents/{id}` |
| 替换导入 / 重新导入并覆盖 | 携带目标文档进入替换上传流程 | `insert`；纯前端路由，提交时调用 import-file |
| 删除 | 软删除文档并异步清理向量 | `delete`；`DELETE /libraries/{slug}/documents/{id}` |
| 打开源文件 / 打开证据源文件 | 下载或打开当前修订文件 | `read`；catalog file access 或 document file API |
| 阅读原文 / 加载更多 | 分段读取规范化全文 | `read`；document full source API |
| 证据 | 打开实体或关系对应的精确 Evidence 原文窗口 | `read`；catalog Evidence API |
| 处理阶段重试 | 在状态围栏允许时重试失败阶段 | 库管理；document processing retry API |
| 修改分类 / 保存 | 调整当前文档有效分类 | 库管理；classification document API |
| 确认建议 / 调整后确认 / 驳回 | 完成待审核分类决策 | 库管理；classification run review API |

### 导入与替换

路由：`#/knowledge-assets/import`；进入条件：任一知识库 `insert`。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 选择文件 / 选择文件夹 | 把支持的文件加入本地上传队列 | 纯前端文件选择和校验 |
| 设置外部文档编号 | 展开或收起外部编号输入 | 纯前端 |
| 移除 / 清空列表 | 从尚未提交的队列移除文件 | 纯前端 |
| 开始上传 | 逐文件上传并跟踪导入、向量化和可选图谱处理 | `POST /libraries/{slug}/import-file` 及任务读取 API |
| 仅重试失败 | 只重新处理失败队列项或服务端可重试任务 | import retry、graph retry/rerun API |
| 替换文档 | 用一个新文件替换指定文档 | import-file，携带 `replace_document_id` |
| 批量提交 | 对已匹配的多个替换项依次提交 | 同上；每个文件独立返回结果 |
| 清除结果 | 清空本次替换结果 | 纯前端 |
| 返回文档管理 | 回到统一知识资产目录 | 纯前端路由 |

支持类型：`.txt`、`.md`、`.markdown`、`.json`、`.csv`、`.pdf`、`.docx`、
`.xlsx`。文件接收成功不代表立即可检索，必须等待 Embedding Worker 完成。

## 知识治理

### 知识图谱

路由：`#/knowledge-governance/graph`；进入条件：有效 `read`。

| 按钮或标签 | 用户结果 | 权限 / API |
| --- | --- | --- |
| 图谱 | 检索和浏览实体、关系、证据及关联文档 | `read`；Organization Graph Catalog API |
| 检索 / 重置 / 翻页 | 按组织、知识库、类型和关键词读取实体或关系 | `read`；graph catalog search API |
| 实体名 / 关系名 / 关联项 | 打开实体或关系详情 | `read`；graph catalog detail API |
| 证据 / 来源文档 | 打开统一知识资产详情或 Evidence 窗口 | `read`；catalog APIs |
| 新增实体 / 新增关系 / 提交审核 | 新建待治理事实 | `insert`；graph-governance entity/relation API |
| 修正 / 新增别名 / 提交审核 | 创建可审核的事实修正或别名动作 | `insert`；graph-governance command API |
| 通过 / 驳回 / 取消 | 处理待审核治理动作或待审核关系 | 库管理；governance decision/cancel/review API |
| 停用 / 恢复 | 改变实体、关系或别名的治理状态 | 库管理；governance state command API |
| 合并 / 确认合并 | 把重复实体合并到目标实体 | 库管理；entity merge API |
| 重新抽取 | 对关联图谱抽取任务重新运行 | 库管理；v0.4 graph extraction rerun API |
| 发布记录 | 查看活动和历史 Publication | 库管理；v0.5 graph publications API |
| 生成预览 / 清除预览 | 计算或丢弃本地发布计划预览 | 库管理；预览读取服务端计划，清除为纯前端 |
| 生成待发布版本 | 提交发布计划并创建 Publication | 库管理；graph-governance publication plan API |
| 激活 / 取消 / 回滚到此版本 | 管理 Publication 生命周期 | 库管理；v0.5 activate/cancel/rollback API |
| 搜索实体 / 选择实体 / 继续加载 | 选择已发布图谱探查种子 | `read`；graph catalog search API |
| 适配画布 / 重新布局 / 图谱设置 | 调整当前图谱显示 | 纯前端 |
| 展开关联 / 返回全景 | 对选中实体执行有界图谱探查或返回全景 | `read`；v0.6 query，返回全景为纯前端 |
| 查看 / 编辑 Schema | 携带当前知识库进入 Schema 页面 | 库管理；纯前端路由 |

## 用户与权限

### 用户管理

路由：`#/users-permissions/users`；进入条件：超级管理员。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 新建用户 / 创建 | 创建账号，可同时授予初始知识库权限 | `POST /admin/users`，必要时调用权限 API |
| 编辑资料 / 保存 | 修改邮箱、用户名或显示名称 | `PATCH /admin/users/{id}` |
| 启用开关 | 启用或禁用账号 | `PATCH /admin/users/{id}` |
| 设为 / 取消超级管理员 | 修改平台超级管理员标记 | `PATCH /admin/users/{id}` |
| 重置密码 / 重置 | 设置该用户的新密码 | `POST /admin/users/{id}/reset-password` |
| 软删除 | 禁用并软删除非当前用户 | `DELETE /admin/users/{id}` |

### 权限矩阵

路由：`#/users-permissions/permissions`；进入条件：超级管理员。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 保存权限 | 计算当前矩阵与初始快照的差异，只提交新增和撤销项 | `PUT /admin/permissions`、`DELETE /admin/permissions` |
| 重置 | 丢弃未保存勾选，恢复服务端快照 | 纯前端 |

矩阵当前可配置动作是 `read`、`insert`、`delete`。库管理能力来自精确
Library `admin` 或组织管理员身份，不在这个三列矩阵中授予。前端隐藏不替代
服务端 Casbin 和组织授权检查。

## 库管理

### 库配置

路由：`#/libraries`；进入条件：超级管理员。

| 按钮或行操作 | 用户结果 | API |
| --- | --- | --- |
| 新建知识库 / 创建 | 创建服务端分配 slug 的知识库 | `POST /admin/libraries` |
| 详情 | 查看列表已返回的完整配置 | 不重复请求详情 API |
| 编辑 / 保存 | 修改分片、Embedding、检索、OCR、图谱等配置 | `PATCH /admin/libraries/{slug}` |
| 打开 Schema 管理 | 携带当前知识库进入 Schema 页面 | 纯前端路由 |
| 测试 Embedding | 验证该库 Embedding 配置 | `POST /admin/libraries/{slug}/test-embedding` |
| 重建 Collection | 启动三阶段向量集合重建 | `POST /admin/libraries/{slug}/rebuild-collection` |
| 软删除 | 软删除知识库并异步清理资源 | `DELETE /admin/libraries/{slug}` |
| 常用问题 / 管理常用问题 | 打开该库 FAQ 管理 | `GET /admin/libraries/{slug}/faqs` |
| FAQ 新增 / 保存 / 删除 | 创建、修改或删除常用问题 | FAQ POST/PATCH/DELETE API |

### Schema 管理

路由：`#/knowledge-governance/schema`；侧栏归属“库管理”；进入条件：库管理。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 导入文件 / 导入草稿 | 从受控 JSON 文件创建 Schema 草稿 | schema import-file API |
| 新建草稿 / 克隆草稿 | 从活动版本复制可编辑草稿 | schema clone API |
| 校验 | 检查草稿结构和发布条件 | schema validate API |
| 影响分析 / 刷新 | 查看草稿激活对现有图谱的影响 | schema impact API |
| 激活 | 经过状态围栏激活草稿 | schema activate API |
| 删除草稿 | 删除未被事实、任务或 Publication 引用的草稿 | schema delete-draft API |
| 停用知识结构 | 停用活动 Schema，但保留历史绑定 | schema disable API |
| 实体类型/关系类型/属性/约束：新增、编辑、停用 | 修改草稿中的四类 Schema 项 | schema item create/update/disable API |

## 运维中心

### 概览

路由：`#/operations-center/overview`；进入条件：超级管理员。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 刷新 / 重试失败项 | 重新读取库、健康、服务状态和审计摘要 | libraries、health、operations status、audit APIs |
| 查看更多 | 进入操作审计 | 纯前端路由 |
| 查看任务队列 | 进入任务页 | 纯前端路由 |
| 查看全部 | 进入库配置页 | 纯前端路由 |

### 服务与队列

路由：`#/operations-center/status`；进入条件：超级管理员。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 刷新 / 重新加载 | 读取 API、Worker、Outbox、任务和重建进度 | `GET /admin/operations/status` |

页面还会每 30 秒自动刷新；自动刷新不是额外按钮。

### 任务

路由：`#/operations-center/jobs`；进入条件：超级管理员。

| 按钮或菜单 | 用户结果 | API |
| --- | --- | --- |
| 重置筛选 / 高级筛选 | 调整当前任务列表查询条件 | 纯前端，随后读取 monitor API |
| 查看详情 | 打开任务错误、重试能力和底层任务标识 | 纯前端，数据来自当前列表 |
| 重试 / 重试所选 | 按任务类型调用统一批量重试合同 | `POST /admin/jobs/monitor/retry` |
| 重试全部失败向量任务 | 按当前知识库范围重置失败向量任务 | `POST /admin/jobs/reset-failed` |

前端不再保留旧的单任务 `retryJob` 封装；页面统一使用能识别多种任务类型的
monitor retry 合同。

## 审计中心

### 操作审计

路由：`#/audit-center/operations`；进入条件：超级管理员。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 刷新 / 筛选 | 按时间、动作、操作者和目标读取日志 | `GET /admin/audit-log` |
| 查看详情 / 收起详情 | 展示当前日志的结构化详情 | 纯前端 |
| 导出 CSV | 下载当前筛选结果 | 纯前端生成；包含公式注入防护 |

### 问答审计

路由：`#/audit-center/chat`；进入条件：超级管理员。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 查询 / 刷新 / 重置 | 按知识库、用户、时间、改写和引用状态读取日志 | `GET /admin/chat-logs` |
| 查看详情 / 查看来源详情 | 展开问题、回答、错误和引用片段 | 纯前端，数据来自日志响应 |
| 导出 CSV | 下载当前筛选结果 | 纯前端生成；包含公式注入防护 |

## 账户设置

### 个人资料

路由：`#/account/profile`；进入条件：已登录。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 保存资料 | 修改自己的用户名和显示名称 | `PATCH /users/me` |
| 修改密码 | 修改密码，随后退出并要求重新登录 | `PATCH /users/me`、`POST /auth/jwt/logout` |

### API Key

路由：`#/account/api-keys`；进入条件：已登录。

| 按钮 | 用户结果 | API |
| --- | --- | --- |
| 新建 API Key / 生成 | 创建可选过期时间的 API Key | `POST /me/api-keys` |
| 复制到剪贴板 | 复制只显示一次的完整 Key | 纯前端 |
| 我已复制并保存 | 关闭明文结果并从页面状态清除 | 纯前端 |
| 撤销 | 使该 Key 立即不可再认证 | `DELETE /me/api-keys/{id}` |
| 快速接入模板 | 打开当前 API 使用模板 | 纯前端弹窗 |

API Key 管理页创建的是调用项目 HTTP API 的用户凭据。MCP 服务进程应使用专用
服务用户的 Key，不能把 Key 作为 MCP 工具参数传递。

## 没有前端按钮但不是多余的能力

以下接口服务于协议客户端、Worker、运维探针或外部集成，不应该为了“每个 API
都有按钮”而暴露到后台：

| 能力 | 为什么没有前端按钮 |
| --- | --- |
| MCP `/mcp` 与 `search_knowledge`、`list_permissions`、可选 `upload_file` | 由 MCP 客户端自动发现和调用 |
| Public API v1 | 面向受控外部客户端，不使用后台 Cookie 页面 |
| Embedding、导入、图谱抽取和清理 Worker | 后台消费任务；前端只查看状态或发起受控重试 |
| `/health/live`、`/health/ready` | 供进程管理器、负载均衡和监控探测 |
| 外部图谱同步 API | 面向已认证同步源，不是人工录入页面 |
| 文件夹、同步源和 Evidence 基础 API | 作为摄入、同步和证据合同保留，可被非当前页面消费者使用 |

## 前后端 API 清理结论

本次核对删除了 3 个没有任何页面或组件消费者的前端封装：

```text
getLibrary
listFolders
retryJob
```

对应后端路由没有随意删除：Library 详情、Folder 合同和单任务重试均有独立
API 测试或兼容用途。后端路由的废弃必须单独评估外部消费者、版本兼容和迁移
窗口，不能从“当前前端没有按钮”直接推出。

当前规则是：

1. 页面按钮只能调用 `admin-ui/src/api.js` 中的真实接口。
2. 前端导出的 API 封装必须有页面、组件或认证入口消费者。
3. 纯前端按钮必须明确只改变界面、路由、剪贴板或本地导出。
4. 后端专用接口按协议和运行时用途判断，不按按钮数量判断。
5. 新增或删除按钮时，同步更新本文以及对应权限和合同测试。
