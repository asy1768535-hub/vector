# PDF 识别效率：单页扫描件后验路由实施说明

日期：2026-09-22
状态：**可直接实施；代码、性能收益、目标环境均未验收**。
执行记录：`.trellis/tasks/09-22-pdf-cascading-quality-gating/`。当前 M2/V1 的路由与授权边界仍以 [原规划](pdf-understanding-improvement-plan-20260918.zh-CN.md) 和 [决策索引](../decisions.md) 为准。本说明只授权本地实现与测试；不包含生产配置、部署或扩大 MinerU 白名单。

## 交付定义

首版只处理**已被 V1 选为 MinerU 的单页扫描 PDF**：先运行一次 RapidOCR，只有候选结果通过下面的严格规则才使用本地结果；其他情况仍整份使用 MinerU。V1 已选本地、预检不完整、多页、混合 PDF、OCR 不可用的路径保持原行为。每份文档的正文和切片只来自一个解析器。

目标是减少常规扫描课件的 MinerU 请求及解析墙钟时间，同时对表格数值、选项关系、低清文字、多栏顺序和无字图像保持保守。实现完成的定义是：默认关闭的新 gate、有确定性单元回归、两种导入入口回归和一份可复算的配对评估。是否开启 gate 由第 6 节的机械判定决定，不能用“代码已写”代替收益证据。

## 1. 开工前的准确位置

1. 执行 `git status --short`，保留工作树现有未提交/未跟踪成果；按根 `AGENTS.md` 阅读续接文档。改每个函数/类前，运行 `node .gitnexus/run.cjs impact "<symbol>" --direction upstream --repo .`；`HIGH/CRITICAL` 先报告，`UNKNOWN` 再做文本确认。不得未经授权换分支、建 worktree、暂存或提交无关文件。
2. 阅读 `app/services/import_parsing.py::build_pdf_import_source`、`app/services/pdf_routing.py::choose_pdf_route`、`app/services/pdf_extract.py::build_pdf_source`、`app/services/ocr.py::ocr_image_blocks` 及 `tests/test_pdf_routing.py`、`tests/test_pdf_extract.py`、`tests/test_mineru_pdf.py`。以当前代码为准：V1 预检与 MinerU 的配置加知识库白名单授权必须先行；`pdf-routing-v1` 的 reasons 是封闭枚举，不能加入质量原因。
3. 现有十个真实样本都由图片生成且均为单页；Sample 8 是三栏阅读顺序反例，Sample 9/10 是常规课件正例。其原件在被 Git 忽略的本机目录，不能假设别的工作树有。把可提交的**合成** PDF 放在 `tests/fixtures/pdf_quality_cases/`；真实样本仅从显式本机目录读取并先核对 SHA-256，缺失时不伪造真实验收结果。

## 2. 固定路由算法

在 `app/config.py` 新增 `pdf_quality_cascade_enabled: bool = False`。gate 关闭时 `build_pdf_import_source` 的调用顺序、异常和路由记录须与 V1 相同。gate 打开时按下面顺序执行，不留运行时自由裁量：

```text
preflight = preflight_pdf(data)                   # 现有有界预检
route = choose_pdf_route(preflight, authorized)  # 现有双重授权
if route.selection != mineru:
    return 当前本地路径
if not (preflight.status == complete
        and preflight.total_pages == 1
        and not preflight.has_mixed_content
        and preflight.pages[0].low_text
        and preflight.pages[0].has_visual_content
        and ocr_enabled and ocr_service.is_available()):
    return 当前 MinerU 路径
candidate = build_pdf_source(..., ocr=ocr_image_blocks, preflight_report=preflight)
verdict = inspect_local_pdf_candidate(candidate)
if verdict.accept:
    final_route = validate_pdf_routing_decision(
        {**route, selection: native_or_rapidocr, needs_review: False})
    return candidate + attach_pdf_routing_unit(final_route)
return 当前 MinerU 路径 + attach_pdf_routing_unit(route)
```

`candidate` 使用**同一次**本地解析产物，不得再调用第二次 OCR。即使同步入口传入 `structured_ocr=False`，gate 的候选解析仍统一使用 `ocr_image_blocks`，使质检有坐标/置信度且同步、异步用同一判断；gate 关闭时不改变该参数语义。调用 MinerU 时不得附带本地候选正文、切片或 OCR 结构单元。

本地候选返回空文本、`PdfExtractError`、`PdfOcrUnavailableError` 或 OCR 无法提供有效 blocks 时视为**不可放行**，沿用原已授权 MinerU 路径并记录有界原因；不把异常文本或原 PDF 内容写进日志。预检未知/损坏/超限仍走原本地路径，不因本 gate 触发远端。MinerU 的超时、版本、响应错误继续显式失败，不能以未通过的候选静默成功。资源限制与现有 PDF/OCR/MinerU 限额原样生效；不捕获 `KeyboardInterrupt`/`SystemExit`。

只改最终 `selection` 和 `needs_review`，保留 V1 的预检 `reasons`，并用现有校验器验证后附加；**不改** `pdf-routing-v1`/`pdf-coverage-v1` 的字段或枚举。质量原因用于指标日志和评估文件，代码内部用固定枚举；这次不建立新的持久化契约。

## 3. 质量检查器的精确定义

新增 `app/services/pdf_quality_inspector.py`：纯函数 `inspect_local_pdf_candidate(source: Mapping[str, Any]) -> PdfLocalQualityVerdict`，结果只有 `accept: bool` 和单个 `reason`。不得调用 OCR、渲染、网络、数据库或切片器。按下列顺序返回第一个失败原因；任何字段缺失、类型异常、非有限数或越界都拒绝：

| 顺序 | 接受条件 | 失败 reason |
| --- | --- | --- |
| 1 | `coverage.status == complete`，恰一页 `segments`，`chunks` 非空，该页 `quality.extraction_mode == ocr` 且 `visual_content_unparsed == False` | `incomplete_or_non_ocr` |
| 2 | 页文本有 80–20,000 个 Unicode 字母（含汉字）；没有 U+FFFD、C0/C1 控制字符（换行/制表符除外） | `insufficient_or_corrupt_text` |
| 3 | 恰由该页 `quality.ocr_blocks` 提供 5–128 个非空块；每块有有限、非负的 `[x0,y0,x1,y1]`、`x1>x0`、`y1>y0`，以及 `[0,1]` 内的有限 confidence；按块顺序拼接的文本与页文本去空白后相同 | `invalid_ocr_evidence` |
| 4 | confidence 中位数 `>=0.90`，最低值 `>=0.65` | `low_confidence` |
| 5 | 页文本无阿拉伯数字、`$ % / \\ = + × ÷ < >`、分数字符或选择题标记 `A.`/`B.`/`C.`/`D.`（忽略大小写）；普通项目符号 `-` 可保留 | `structured_content_risk` |
| 6 | 所有块的文本至少 2 个字母或汉字；同一水平行不存在横向分离的两个正文块（定义见下） | `layout_risk` |
| 全部通过 | 使用本地候选 | `accepted` |

第 6 条的“同一水平行横向分离”按所有块对计算：两块各含至少 5 个字母/汉字；它们的 y 投影重叠 `>= min(两块高度) / 2`；两块 x 投影互不相交，水平间隙 `>= 2 × 全页有效块高度中位数`。满足任一对即判 `layout_risk`。它是保守的多栏/表格疑点，误升档只损失效率；不能作为多栏正确性的证明。不得把样本 ID、文件名或哈希写入运行时规则。所有阈值在本首版固定，调阈值需新一轮配对评估，不能为了让黄金样本通过而临时改规则。

这些规则只为**可靠的简单文字扫描件**放行。它们不能证明数学答案、流程箭头或图像语义正确，所以包含此类信号的候选一律升档；MinerU 自身在 Sample 1/3/4/5/6 也有已知错误，本任务不宣称修复这些错误。

## 4. 必须按序完成的代码切片

| 顺序 | 修改位置 | 具体交付与最小回归 |
| --- | --- | --- |
| T1 | `app/services/pdf_quality_inspector.py`；新 `tests/test_pdf_quality_inspector.py` | 先写红测再实现上表每一分支；用纯构造的 source 测缺字段、NaN/无穷、临界阈值、双栏同行、表格符号和简单双语正文。不读真实 PDF。 |
| T2 | `app/config.py`、`app/services/import_parsing.py`；`tests/test_pdf_routing.py`、`tests/test_mineru_pdf.py` | gate 关闭逐项保持 V1；打开时单页扫描且高质量候选本地直通，其余单页升 MinerU，多页/混合/未知/未授权原行为。断言解析器调用次数、最终 `selection`、正文只来自一方、MinerU 失败不回退。覆盖 bytes 与 Path。 |
| T3 | `tests/test_query_import_api.py` 的同步 PDF 入口、`tests/test_import_pipeline.py` 的 Path 解析入口，以及 `tests/test_importer_inbox_1a.py` 的 Worker 调用边界 | 对同一单页夹具分别覆盖同步 API 与 Worker 经过的解析入口，比较最终路由、正文、页码、结构单元和 coverage；gate 开/关各跑一次。同步 `structured_ocr=False` 仍走同一候选规则。若现有 Worker 测试只替身化 `parse_import_file`，新增一条经过真实解析函数的窄集成测试。 |
| T4 | `tests/fixtures/pdf_quality_cases/` 和一份评估脚本/说明 | 提交可分发的原生文字、简单双语扫描、表格、三栏、低清、无字、多页、损坏/超限合成夹具或生成器。评估脚本输出 JSON/Markdown 到忽略目录 `output/`，不写原始 PDF 或正文。真实十样本仅显式传目录时运行，缺失则标 `unverified`。 |
| T5 | 配对评估报告 | 在相同解析器版本、配置、机器、并发和输入顺序下运行 gate off/on，采用 AB/BA 交替顺序，至少 3 组稳态；另用新本地进程记录首次 OCR 调用，MinerU 冷态不可观测则标 `unverified`。评估脚本用 `perf_counter` 包裹现有阶段入口，记录总解析墙钟、预检、本地候选（含切片）、MinerU（含切片）时间、请求数及质量断言，不为计时修改生产行为。报告每份样本、P50/P95、总时间、升档前额外成本；禁止用 2026-09-18 的远端耗时当新基线。 |

本首版**不重构预检与 `pdf_extract` 的重复扫描**：先用 T5 量出其占比，避免把优化和路由行为改在同一切片。技术归因校正：`preflight_pdf` 使用 `pypdf` 读取文字和图片元数据，并没有 PDFium 渲染上下文可复用；多页 PDFium 页面栅格化句柄复用属于后续独立测量事项。删除任何未经新鲜真实配对测量支持的“MinerU 通常 2–4 秒”等历史经验假设；耗时基线必须以当前环境真实配对测量为准。前置版式快筛和队列水位调度只能列为待验证假设，不能列为已确定的实施方案。若真实配对数据显示预检加重复文字/图片扫描占本地候选耗时 `>=10%`，另开后续切片共享有界临时文本/元数据；保留 V1 外部报告和资源语义，并重新跑本表 T2–T5。重复扫描耗时占比在未有内部探针测量前标记为 `unverified`，不得以此断言无需去重。本条给出触发条件，不阻塞 T1–T5。

## 5. 黄金回归与人工断言

| 样本 | gate 打开时的最低要求 |
| --- | --- |
| 1 流程图 | 本地不得作为完整关系证据；若升 MinerU，其错误箭头仍标未解决。 |
| 2 数学题 | 必须升档；逐行核对面额表格行 `$5/$10/$20/$50` 对应 `5/3/2/1`，答案 `5/11`，选项 `C=5/17` 与 `D=7/17`；严禁跨行错位匹配（需建立错位包含全部数字的反例检验）；模拟 MinerU 读取的历史解析结果只能标为“历史基准”，不能标为本次识别正确。 |
| 3/4 方格图 | 不得把空本地候选作为成功结果；若 MinerU 产生描述，记录其已知计数错误，不能声称图形理解已通过。 |
| 5/6 低清拼音及汉字 | 不得本地直通；分别记录声调、字形和连线缺失，MinerU 既有错漏不能写成已修复。 |
| 7 无字照片 | 不得因 OCR 空串推断纯图或生成无来源描述。 |
| 8 三栏英文公告 | 必须升档；五个锚点应按左栏→中栏→右栏→下一公告排列。 |
| 9/10 常规课件 | 逐项检查正文锚点；只有实际通过第 3 节规则才直通本地，不能为节省 GPU 绕过规则。Sample 9 本地直通仅证明路由放行决策，不证明全文识别完整。 |

另用可提交合成夹具覆盖真实原生 PDF、多页扫描、混合、旋转、损坏和资源上限。Sample 2/8 是“错误放行”硬门；Sample 9/10 是效率正例，不保证在当前固定阈值下放行。真实文件及人工标注若缺失，报告必须写明未验证，不能声称十样本通过。本地离线扫描样本（如 `D:\word\pdf文档测试` 中剔除旧样本后的 21 份单页图片 PDF）仅作为有限探索性筛查，不可宣称满足“独立 30+ 页”发布门槛，保持 `pdf_quality_cascade_enabled=False`。

## 6. 验收、停用和交接

本地实现验收：`.\.venv\Scripts\python.exe -m pytest -q tests/test_pdf_quality_inspector.py tests/test_pdf_routing.py tests/test_pdf_extract.py tests/test_mineru_pdf.py tests/test_pdf_quality_fixtures.py tests/test_query_import_api.py tests/test_import_pipeline.py tests/test_importer_inbox_1a.py tests/test_evaluate_pdf_cascade.py`（9 个测试套件，当前 256 项自动化测试全绿）；`git diff --check`；提交前运行 `node .gitnexus/run.cjs detect-changes --scope all --repo .`，若 `partial/truncated` 重跑。只报告本次新鲜结果。无需新数据库迁移；默认 gate 为 false。

**允许在隔离环境开启 gate 的判定**（均需满足）：真实十样本及独立、未参与规则设计的至少 30 页人工核验样本均可用且哈希匹配；Sample 1/2/5/6/8 无错误本地放行；同步/异步和资源/授权负例全通过；配对总解析时间不高于 V1，MinerU 调用数至少减少 1 次，P95 不超过 V1 的 1.10 倍。未满足任何一项，保持 gate 关闭，报告失败项和下一条本地修复命令，不放宽质量门槛。生产启用、白名单扩张另需目标环境证据与明确授权。

如果出现未授权/预检未知却调用 MinerU、关键错误放行、覆盖虚报、单份文档混用两方正文、资源上限绕过，立即回退本 gate 为 false 并保留 V1；不采用未通过质检的 OCR 候选。交接记录写明 Git 状态、改动文件、命令与输出、真实样本可用性、性能表、未验证项。`IMAGE_ASSET` 零切片成功和 `PARSE_SUSPICIOUS` 检索门控需另行设计，因为现有 ingest 会拒绝零切片，V1 的路由/覆盖契约也没有该状态。
