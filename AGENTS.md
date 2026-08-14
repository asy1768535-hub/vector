<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **vectorDatabase** (35965 symbols, 64683 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first. If GitNexus cannot run because its runtime, parser dependencies, or MCP tools are unavailable, use the documented fallback below.

## Always Do

- **MUST run impact analysis before editing any symbol.** When GitNexus is available, run `gitnexus_impact({target: "symbolName", direction: "upstream"})`; otherwise complete the documented unavailable-tool fallback. Report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST check affected scope before committing.** Run `gitnexus_detect_changes()` when available; otherwise complete the documented unavailable-tool fallback checks.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When GitNexus is available and you are exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When GitNexus is available and you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## Fallback When GitNexus Is Unavailable

GitNexus is development tooling, not a runtime dependency. Its failure must not require adding parser or native-build dependencies to this project, and must not block scoped business work when a manual safety review is possible.

Use this fallback only after one documented GitNexus attempt fails. Report the command and error to the user, then:

1. Before editing a function, class, or method, locate its definition and direct callers with `rg` or `git grep`, inspect the relevant API/service/worker/test flow, and report the estimated blast radius and risk level.
2. Treat shared services, public APIs, database models/migrations, worker orchestration, authentication, and publication paths as HIGH risk unless the inspected call sites show otherwise. Warn the user before editing HIGH or CRITICAL risk code.
3. Before committing, run `git diff --name-only`, `git diff --check`, focused tests for every affected flow, and manually report the changed symbols and expected execution paths. This is the fallback for `gitnexus_detect_changes()`.
4. Record that GitNexus was unavailable in the final report. Stop using the fallback once GitNexus is healthy again.

## Never Do

- NEVER edit a function, class, or method without first completing either `gitnexus_impact` or the documented unavailable-tool fallback.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running either `gitnexus_detect_changes()` or the documented unavailable-tool fallback checks.

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/vectorDatabase/context` | Codebase overview, check index freshness |
| `gitnexus://repo/vectorDatabase/clusters` | All functional areas |
| `gitnexus://repo/vectorDatabase/processes` | All execution flows |
| `gitnexus://repo/vectorDatabase/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
