# 实施计划：关系型问答的图谱增强检索

## 前置门

- [ ] 用户在看到最终规划摘要后，另发一条明确消息批准实施。
- [ ] `docs/superpowers/plans/2026-08-03-ocr-schema-quality-gates.md` Phase 2、Phase 3 未完成时，只允许实现和部署 `off/shadow`，不得启用答案融合。
- [ ] 记录当前工作树状态和本任务文件 allowlist，保留所有无关修改。
- [ ] 检查 GitNexus 索引新鲜度；对下面每个拟修改函数、类或方法逐一运行 upstream impact。HIGH/CRITICAL 先报告并等待确认。

## 实施顺序

1. [ ] 先写关系意图门、固定原因码、DTO 与纯排序/去重测试，再实现最小服务代码。
2. [ ] 将 `chat_graph_context` 的 Chunk/Evidence/Publication 种子解析提取为可复用多切片 helper，保持现有引用图谱 endpoint 契约和测试不变。
3. [ ] 新增 `chat_graph_augmentation` 编排：库模式判断、一次一跳查询、严格超时、Evidence 当前性回查、排序、去重、预算和 fail-open。
4. [ ] 给聊天 schema 增加严格、有限的 `ChatGraphEvidence`、`graph_augmented` 和 `graph_evidence` 加法字段。
5. [ ] 添加 Library 的 `graph_assisted_chat_mode` 与 ChatMessage 的持久化字段，编写 additive Alembic 迁移；旧值分别为 `off`、`false/[]`。
6. [ ] 更新 chat history 的保存、恢复和管理审计读取，保证刷新后完整重现图谱增强状态。
7. [ ] 抽取流式/非流式共用的回答准备逻辑，将同一 augmentation 结果传给 answer prompt、响应和持久化；验证异常路径只回退 RAG。
8. [ ] 接入影子指标与脱敏日志；证明 shadow 不改变 prompt、答案、sources 或用户可见字段。
9. [ ] 更新知识库编辑页的 `off|shadow|enabled` 控件；质量门未满足时后端拒绝或运维流程禁止 `enabled`。
10. [ ] 更新 Chat 消息状态和展示：正文下方徽标、独立关系证据、原文定位、历史恢复，并复用现有图谱弹窗。
11. [ ] 在测试知识库运行 shadow 样本，记录触发率、种子率、关系有效率、Evidence 成功率、P50/P95 额外延迟和降级分布。
12. [ ] 质量门完成后，先对测试库启用 `enabled`，人工核对关系问答；确认无误再逐库灰度。

## 必测场景

- 纯函数：中英文关系问法、否定/非关系问法、排序稳定性、关系/Evidence 去重、预算裁剪、原因码。
- 后端：多 RAG 切片解析同一 Publication、跨库隔离、degraded/changed Publication、无 seed/无 relation、Evidence stale/missing、超时和内部异常。
- 契约：非流式响应、SSE 首个 sources 事件、历史恢复、管理日志均包含一致的新字段；旧 source 语义不变。
- 持久化：旧消息默认 false/[]，新消息刷新后保留图谱标识与证据，失败回答不误标增强。
- Prompt：shadow 完全不注入；enabled 只注入回查成功的关系；直接原文区与关系证据区明确分隔。
- 前端：只在实际增强时显示徽标；关系证据位于普通引用前；点击原文定位和现有图谱弹窗可用；空/失败/shadow 不出现空壳 UI。
- 回归：现有聊天、引用定位、引用图谱、v0.6 graph retrieval、Publication invariant、Library 编辑权限和迁移链测试。

## 验证命令

具体测试文件以实现时的项目规范和影响分析结果为准，至少执行：

```powershell
.\.venv\Scripts\python.exe -m pytest <新增聊天图谱测试> <现有chat_graph_context测试> <v0.6相关测试>
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\python.exe -m ruff check app tests
node <新增前端测试>.test.mjs
node <现有Chat相关测试>.test.mjs
.\.venv\Scripts\alembic.exe heads
git diff --check
```

浏览器验证桌面和窄屏：普通问题、增强问题、shadow、图谱超时、历史刷新、关系证据定位、打开图谱以及控制台无错误。

## 风险点与回滚

- `app/api/chat.py` 同时承载流式/非流式和持久化，影响面可能较高；必须先做 impact，并优先抽共享准备结果而非复制分支。
- `chat_graph_context` 已被引用图谱 endpoint 使用；提取 helper 后必须保留现有契约回归。
- Library 和 ChatMessage 需要迁移；迁移必须 additive、默认值兼容旧数据、先在测试库升级验证。
- 最快回滚路径是全局 kill switch 或将 Library 模式设为 `off`。不要删除新增历史字段，也不要回写 Publication。

## 提交前检查

- [ ] 所有需求 AC1-AC9 均有自动化测试或明确浏览器证据。
- [ ] 质量门未完成时没有任何生产 Library 处于 `enabled`。
- [ ] 全量测试、Ruff、迁移链、前端测试、浏览器验证和 `git diff --check` 通过。
- [ ] 运行 `gitnexus_detect_changes()`，确认只影响已批准符号与执行流；异常影响先处理，不提交。
- [ ] 暂存时只包含正式代码、迁移、测试和规划文档，不包含临时脚本、样本输出或无关历史修改。
