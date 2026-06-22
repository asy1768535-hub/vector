# 检索效果评测（eval harness）

给检索链路一把**可复现的尺子**：固定评测集 → 跑真实检索 → 输出 `hit@1 / hit@3 / hit@5 / MRR / Recall@K`，并能在一次运行里对比 **rerank 开/关、不同 candidate_k**。换 embedding、调 chunk 参数、接真实 reranker 时，不靠感觉判断效果。

> 评测脚本：[`scripts/eval_retrieval.py`](../scripts/eval_retrieval.py)
> 纯逻辑（命中判定/指标）：[`app/services/eval.py`](../app/services/eval.py)，单测 `tests/test_eval.py`

## 评测集格式（JSONL，每行一条）

| 字段 | 必填 | 说明 |
|---|---|---|
| `query` | ✅ | 检索词 |
| `library` | ⛳ | 库 slug；整条缺省时可用 `run --library <slug>` 兜底 |
| `expected_doc_ids` | ⭐ | 期望命中的文档 id 列表（命中其一即算中） |
| `expected_text` | ⭐ | 期望出现在正文里的片段列表（归一化后子串匹配，大小写/空白不敏感） |
| `note` | ❌ | 备注 |
| `synthetic` | ❌ | 是否合成弱样本（`gen` 生成的为 `true`） |

> ⭐ `expected_doc_ids` 与 `expected_text` **至少给一个**；都给则命中任一即算命中。
> 示例见 [`dataset.example.jsonl`](dataset.example.jsonl)。

**命中口径**：一条检索结果命中 = 它的 `document_id ∈ expected_doc_ids`，**或** 它的正文包含任一 `expected_text` 片段。`hit@k` 看首个命中名次是否 ≤ k；`MRR` = 平均 `1/首命中名次`（整轮不中记 0）；`Recall@K` 仅当给了 `expected_doc_ids` 时有意义。

## 两种评测集来源

- **A 真实人工集（质量高）**：人工写 `query` + 期望命中，放 **`eval/dataset.jsonl`**（脚本默认读它）。
- **B 合成弱集（先跑通流程）**：从库里已有 chunk 反向造样本（query 取自文档片段）。质量弱，仅用于打通链路、做冒烟，不替代 A。

先用 B 跑通，再逐步用 A 替换。

## 前置条件

评测的是**真实管道**，运行时需要：**embedding 服务 + Qdrant + PostgreSQL 均可达**（即正常能检索的环境）。
rerank 对比还需先部署 reranker 并在 `.env` 配好：

```ini
RERANK_ENABLED=true
RERANK_BASE_URL=http://<host>/rerank
RERANK_MODEL=<reranker-model>
```

未配置 reranker 时，`--rerank` 的 rerank 行会自动跳过并提示，dense 评测照常进行。

## 用法

```bash
# B：从某库已有文档造 20 条合成弱评测集
python scripts/eval_retrieval.py gen --library medical --n 20 --out eval/dataset.synthetic.jsonl

# 跑评测，对比 dense vs rerank（默认 --rerank both）
python scripts/eval_retrieval.py run --dataset eval/dataset.synthetic.jsonl

# 仅 rerank、加大召回候选数、并写出 markdown 报告
python scripts/eval_retrieval.py run --dataset eval/dataset.jsonl --rerank on --candidate-k 100 --report eval/report.md

# 条目没写 library 时用兜底库
python scripts/eval_retrieval.py run --dataset eval/dataset.jsonl --library medical
```

输出示例（stdout）：

```
评测集 eval/dataset.synthetic.jsonl：20 条 | top_k=5 | candidate_k=50 | 配置=['dense', 'rerank']

config  hit@1   hit@3   hit@5   MRR    Recall@5  n   err
------  ------  ------  ------  -----  --------  --  ---
dense   70.0%   90.0%   95.0%   0.802  95.0%     20  0
rerank  85.0%   95.0%   95.0%   0.891  95.0%     20  0
```

## 约定

- **真实评测集不入仓库**（含项目/人员等敏感数据）：放仓库外（如桌面 `工地库-评测集/`），用 `--dataset <路径>` 指定。
  即便放回 `eval/`（如默认的 `eval/dataset.jsonl`、合成集 `eval/dataset.synthetic.jsonl`、报告 `eval/report*.md`）也已被 `.gitignore` 忽略，不会误提交。
- 仓库内只保留**通用示例** `eval/dataset.example.jsonl`（占位 id，直接跑会全不中，仅作格式参考）与本说明。
