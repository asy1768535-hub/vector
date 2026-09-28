<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **vectorDatabase** (28962 symbols, 64793 relationships, 1069 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/vectorDatabase/context` | Codebase overview, check index freshness |
| `gitnexus://repo/vectorDatabase/clusters` | All functional areas |
| `gitnexus://repo/vectorDatabase/processes` | All execution flows |
| `gitnexus://repo/vectorDatabase/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->

# AI 续接约定

## 最小必读集

开始任何项目工作前，按顺序阅读：

1. 本文件；
2. [`docs/AGENT_README.md`](docs/AGENT_README.md)（当前代码、运行和风险基线）；
3. [`docs/current-architecture.md`](docs/current-architecture.md)（当前 owner 与主链路）；
4. [`docs/decisions.md`](docs/decisions.md)（仍有效的设计决策）；
5. 当前任务记录及其链接的规划、测试和 owner 文件；
6. 需要验收时读取 [`docs/quality/verification-matrix.md`](docs/quality/verification-matrix.md)，需要转交时读取 [`docs/handoffs/README.md`](docs/handoffs/README.md)。

当前活跃任务为「PDF 确定性解析策略（M2 收尾与路由 V1）」：
[`docs/roadmap/pdf-understanding-improvement-plan-20260918.zh-CN.md`](docs/roadmap/pdf-understanding-improvement-plan-20260918.zh-CN.md)、
本机 Pi 计划 [`.pi/plan/确定性-pdf-解析策略实施计划-20260920-0017.md`](.pi/plan/确定性-pdf-解析策略实施计划-20260920-0017.md)、
本机 Trellis 任务 [`.trellis/tasks/09-20-deterministic-pdf-parsing/`](.trellis/tasks/09-20-deterministic-pdf-parsing/)。计划或代码存在均不表示已发布。

## 事实、范围与工作树

- 当前 checkout、迁移、测试和目标环境的实时输出优先于 README、历史计划和聊天摘要；缺少证据必须标为 `未验证`。
- 工作树已经包含用户的未提交和未跟踪成果。先运行 `git status --short`；不得 reset、checkout、清理、暂存、覆盖或提交无关文件。
- 不复制 `.env`、凭据、服务器地址、业务数据、对象存储内容或日志到文档、测试或聊天。
- `docs/` 是本项目唯一的可提交开发真源根；不创建平行 `dev-docs/`，不移动或改写历史计划。`.trellis/` 与 `.pi/` 是本机执行/规划空间。
- 需要隔离的实现工作先按 `using-git-worktrees` 检查现有隔离状态；创建 worktree 或更改分支前须取得明确授权。

## 工程与交接规则

- 修改函数、类、方法前必须按上方 GitNexus 规则做 upstream impact；`HIGH`/`CRITICAL` 先报告，`UNKNOWN` 必须用文本搜索继续确认。
- 每次实现先选择最小、因果相关的测试；完成、提交或交接前必须运行新鲜验证，不能以历史通过数代替。
- 后端常用验证：`\.venv\Scripts\python.exe -m pytest -q <target>`；前端：`node admin-ui/<test>.mjs`；文档/代码变更都应执行 `git diff --check`。实际命令以受影响 owner 的现行说明为准。
- 改动架构、迁移、认证/授权、权限、对象删除、外部 provider、feature gate、部署或生产数据时，先补当前证据、影响分析和恢复路径；没有单独授权不得执行外部写入或部署。
- 每个可继续的任务在 `.trellis/tasks/<date>-<slug>/` 保留简短 PRD、设计和实施记录；可提交的入口只在 `docs/` 链接它。转交使用 handoff 模板，必须写明 Git 状态、验证证据、未验证项和下一条安全命令。
