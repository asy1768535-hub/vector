# 待发布问题修复计划

> 状态更新（2026-10-07）：Office/Excel、重建、Chat 已发布；PDF 真实质量及自动收尾仍待验收，多库检索不动。详见[发布核对与剩余项](../quality/deployment-gap-status-20261007.md)。下面各阶段记录保留原时点，历史“无新增部署”不代表当前仍未发布。

2026-10-05：用户授权制定简单规划，并用目标模式推进修复。目标是逐项得到经过验证的发布候选；发布、迁移、生产重建及付费模型回放分别核定授权。多库检索保持不动。

## 阶段目标与用户流程

文件上传 → 格式检查 → 解析与向量化 → 文档版本绑定 → 受权限保护的取证与问答。先解决已有回归，再补缺少的验证，避免把本地代码、测试通过和生产上线混为一谈。

## 当前真相与 Owner

起始基线（历史）：当时相关测试81 passed / 1 failed；Office非零CFB头标识错误放行，Office/Excel/重建候选未上线，PDF级联关闭且API未同步，问答改进尚未实施。此段保留起始核验，不替代下方当前进度或发布时的重新核验。

当前进度：已批准的「续接五项修复：完成本地候选验收」六项标准均已满足。新鲜Office/Excel140 passed；重建22常规与11真实本机PG；PDF六模块114 passed；问答十四后端模块224 passed及七候选前端模块85 passed。实际候选浏览器10项交互及342/1280宽度检查通过，发现并修复引用弹窗裁切；三包成员及SHA与当前源码核对通过，问答包仅更新原8文件中的Chat组件。详情见[本轮核验](../quality/pending-fix-candidate-review-20261006.md#已批准续接目标本地候选验收完成)。本地续接目标completed；五项总任务的S4/S5真实质量、正式镜像及发布仍in_progress，级联不开启，无新增部署。

工作树已有大量用户成果，保留原分支和全部无关改动；不清理、批量暂存或提交。执行记录：[本机任务](../../.trellis/tasks/10-05-pending-fixes-goal/implement.md)。

## 阶段控制

- schema: sliver-stage/v2
- stage_id: pending-fixes-goal-20261005
- primary_route: 开发执行
- delivery_kind: implementation
- task_depth: D2
- materialization_trigger: [cross_owner_drift, ordered_nonclosable_transition]
- risk_lanes: [identity_permission, privacy_secret_data, public_contract_compatibility, resource_reliability]
- evidence_mode: test
- test_level: T2
- route_evidence_kind: null
- effect_class: local_reversible
- operational_mode: planned
- scope_authorization: confirmed: 用户要求对已列五类问题制定规划并用目标模式修复；仅本地修复和验证，不扩展生产发布授权
- authorization_substage: S5-chat
- product_decision: not_required: 保留已有产品、权限和单主解析器边界，无新架构或 provider
- active_substage: S5-chat
- result_status: in_progress
- truth_writeback: pending
- migration_state: not_applicable
- evidence_refs: [app/api/chat.py, app/services/chat_answer.py, app/services/chat_evidence.py, app/schemas/chat.py, app/services/chat_history.py, app/services/evidence_locator_projection.py, admin-ui/src/views/Chat.js, admin-ui/src/chat_citations.js, tests/test_chat_api.py, tests/test_chat_answer.py, tests/test_chat_evidence.py, tests/test_chat_evidence_api.py, tests/test_chat_evidence_pg.py, admin-ui/chat_evidence_sources.test.mjs]

## 调研决策

- research_status: not_required: 项目内已有明确失败用例与解析、取证 Owner；复用现有代码和测试，不引入新外部方案

参考既有 [PDF 策略](pdf-understanding-improvement-plan-20260918.zh-CN.md)及[问答规划](chat-evidence-reliability-plan-20260929.zh-CN.md)。沿用有界解析、文档级单主结果、文件范围取证、可信来源绑定；不复制另一套检索或解析框架。历史记录不改写，新授权及证据在本计划和当前任务中记录。

## 范围与非目标

范围是五项修复及候选验收。新改动放在所属服务 Owner，API 保持薄适配，文档版本与位置使用可信元数据。

不修改多库检索、权限模型、数据库结构，不重解析生产原件、不执行生产重建，不自动开启 PDF 级联，不增加模型验证调用或无界工具循环。历史引用位置增强可以延期，核心取证和实时来源不得延期。恢复路径为精确撤销本任务新增差异，不覆盖原有用户改动；生产发布另生成叠加包及容器回退方案。

## 子阶段计划

| 子阶段 | 结果 | Owner | 完成标准 | 验证 | 不触碰 |
| --- | --- | --- | --- | --- | --- |
| S1-office | 修复异常文件放行及兼容路由 | `import_upload_preflight.py` | 非法头拒绝；合法文件、加密拒绝、格式路由不退化 | 预检、上传与解析；已有失败 T1，新增行为 T2 | 无生产上传 |
| S2-excel | 核验异常数值兼容，收敛全局修改 | `xlsx_extract.py` | 异常原文保留；正常数字不变；其他调用不受永久补丁影响 | 真实合成工作簿、错误/资源边界，T2 | 无全局永久补丁 |
| S3-rebuild | 完成重建版本绑定 | `rebuild.py`、Embedder | prepare 持久化 current 快照；恢复不漂移；内容号与索引代数分开；实际消费不触发内容发布/清理；同库并发及旧在途任务安全拒绝 | 版本、恢复、消费、状态回归及独立 PG 锁验证，T2 | 无生产重建 |
| S4-pdf | M2/V1 双入口验收与差异核验 | 共同解析入口、既有 PDF 任务 | 正文、结构、定位、覆盖一致；资源/授权失败保守处理 | T1/T2 回归，真实质量/性能 T3 | 级联保持关闭直至实测过门槛 |
| S5-chat | 通用文件取证、结构补齐、来源约束 | 既有问答规划 S0–S4 | 必要证据实际入模；歧义、缺失、预算缺口明确；来源、字段、时态不过度推断 | T2 确定性测试及 T3 独立同类实测；鉴权、历史、流式回归 | 多库检索、生产模型回放另核授权 |

每项记录通过、失败和未验证证据后更新当前子阶段；目标模式持续推进已授权的本地工作。只有完成相应发布核验的候选才列为可部署，不能把仅写规划的项目打包上线。

## 测试、安全与影响

修改函数前做实时 GitNexus upstream impact；HIGH/CRITICAL 先报告，UNKNOWN 补充文本确认。索引过期先刷新。Office 触及不可信文件输入，保留资源上限与安全拒绝；重建触及 revision/任务状态，确认新旧字段关系；Chat 保留库和文件夹范围及来源身份。敏感样本只在既有私有范围核验，不复制进文档或测试。

## 验证方法

先运行最小因果测试：`pytest -q tests/test_import_upload_preflight_1b.py tests/test_xlsx_extract.py tests/test_rebuild_external_guard.py`。新增稳定行为先复现失败再最小实现。随后按调用链增加必要回归、编译和 `git diff --check`。PDF/Chat 的真实服务、模型、浏览器效果单列，不用 mock 或历史通过数代替。

执行前通过结构门禁；完成后回写实际文件、命令、输出及发布缺口。提交前另做完整 GitNexus 图变更分析；本目标不自动提交。

## 停止条件与未验证

数据迁移、额外模型成本、生产写入或扩展公开契约出现时，先完成可审阅方案再处理具体授权。相同问题三次修复失败则重新定位 Owner 与边界。PDF 独立质量/性能、MinIO上传排队及真实持久化链路、Chat 原失败题和独立同类真实生成、浏览器及生产发布均尚未验证；不会以本地通过冒充完成。

## 实施回写

- actual_result: S1–S4本地成果保留。S5有界文件定位、每文件一次既有检索、当前版本/PG正文绑定、真实表格组补齐和必要预算已接入Chat普通/流式两入口，也用于全库主题的命中组。歧义直接澄清不调模型；缺口进入生成范围说明。图谱来源使用实际入模正文，指定文件限制图谱文件范围；普通主题保留其他有权限图谱可选证据。可信页/行位置及编号进入上下文和实时来源，历史从保存顺序/图谱编号恢复普通来源编号；页面混排不丢片段、空缺编号不生成可点击引用。可信完整表格计算和所列Markdown边界已补齐，Chat生成校验非法编号并明确失败，不增加模型调用。
- changed_owners: S5修改`app/api/chat.py`、`app/schemas/chat.py`、`app/services/chat_answer.py`、`chat_history.py`、`evidence_locator_projection.py`、`admin-ui/src/views/Chat.js`及`chat_citations.js`，新增`chat_evidence.py`与取证/API/PG/前端回归；既有API和历史测试夹具明确其单元边界。仅新增来源可选字段，无数据库表或迁移。S1–S4Owner成果保留，多库检索与生产配置不动。
- fresh_evidence: S5十四模块224 passed/2既有弃用警告，包含15真实本地PG（实际HTTP普通/流式生成、落库和历史、非法编号失败状态、主题补齐、不可信/文件夹外来源拒绝及真实XLSX保存单元格计算）；计算专门模块22项确定性用例和3项PG用例通过。生成模块65项通过，包含新增26项普通/流式Markdown边界；八个相关Python文件compileall、Ruff E9/F及全工作树diff-check exit0。前轮七前端模块65 passed，本轮未改UI，不冒充本轮浏览器或前端重跑。S4十模块228 passed为此前阶段证据。各集互有重叠，不相加为全仓库通过数。
- plan_deviation: S5子任务额度中断后父任务接续。整合新增API基线5 RED，位置/编号4 RED、页面混排4 RED、历史恢复2 RED、编号检查10 RED均修复至GREEN。真实PG新增主题完整组RED后补齐；身份/文件夹负例2 RED暴露新Owner的未绑定原样返回分支，删除该分支后拒绝。原API单元夹具使用不合法opaque ID，现显式替换取证边界，不在生产保留测试兼容旁路；真实SQL及HTTP另测。保留既有生成协议，非法编号校验仅Chat显式启用，公开回答默认行为保留并做兼容回归。
- remaining_risk: S5富Markdown编号边界、原七题及独立同类真实生成收益和浏览器卡片仍待验收。计算只使用已保存可信完整XLSX/XLS/DOCX单元格；缺快照、未求值公式、含糊字段或混合单位明确保留缺口；不证明PDF/CSV、DOCX合并单元格、复杂表头或业务条件分组计算。历史位置未持久化，明确留空，未用当前位置冒充旧来源。PG测试观察同事务消息时间相同时历史UUID排序可能非对话先后，本轮不宣称顺序问题已修复。目标相同PG16.14本机隔离回归已通过26项；仍不证明生产Linux/真实provider/大库性能。S4真实质量/效率、MinIO链路仍未验收，无新增部署。
- next_substage: 继续S5-chat候选核验与生产依赖差异排除，准备有界真实生成/浏览器验收；先给出具体范围与成本，再核定额外回放授权。既有历史规划中的成本与生产授权不由新本地实现自动扩大。
- git_checkpoint: 未暂存、未提交；保留原工作树，无关成果不动。

S2 本地修复已移除永久 openpyxl 补丁；异常数值、日期/公式缓存保留原文且不影响独立调用；修复50连续空行导致的后续数据静默遗漏。新增用例先观察失败，修复后子任务63 passed，父任务Office/Excel/重建字段/provenance/evidence五模块132 passed。本地openpyxl 3.1.5、编译及链接检查通过；生产依赖和真实文件尚未验收，详见任务记录。S1扩大archive集的既有夹具缺口本轮已补齐，最新Office/Excel相关七模块206 passed；这仍不是全仓库验证。

S3 的真实PG证据来自单独初始化的本机测试实例及逐用例隔离schema，只含合成数据。快照/激活、两种Worker gate、成功与失败的版本漂移、两编排者互斥、网络失败恢复及目标过期拒绝、未发布文档保留、取消后解锁、重建完成后旧普通任务停止写入均已验证。后一个场景现场观察upsert两次，修复后仅重建版本写入；普通publication过期分支另验证不改或清理已保留内容。全量模型create_all存在无关长约束名缺口，本套仅建本Owner与FK依赖表，不证明全仓库迁移通过。

S4 本地修复已完成：bool四参数、预检/正文隐私/尾延迟和调用统计新增回归均有RED→GREEN。父任务真实合成原生/扫描PDF×gate两态，使用真实预检、提取、渲染、分块、MinerU返回映射和parser-unit校验；替身OCR/远端响应及DB/存储边界，不依赖真实Provider。同步HTTP只验证local storage路径，Worker至摄入前；不证明正式MinIO队列、持久化或真实质量。独立人工真值、实际MinerU和AB/BA性能未测前不启用级联，不重做已归档P1–P3。

S5第一版预算在取证结果前固定为指定文件3份、每组8片、本轮20源、上下文12000字符或更严格配置，不新增循环检索/模型验证。全库主题仍调用一次既有检索，在其命中真实当前版本表格组内补齐，不把Top-K当作全库穷举。身份未绑定或越过文件夹范围的来源拒绝；外部库不虚构文件版本能力。历史定位可选增强不得阻塞核心取证，不能用当前页码冒充旧revision来源；该阶段及真实模型验收仍未完成，尚未发布。

2026-10-06 数值实现前冻结：只在可信完整表格组内复用已保存单元格快照，不从自由正文或OCR猜列，不把公式字符串当计算结果。每组最多读取2048单元格、32字段，计算说明最多2048字符并计入本轮证据字符预算；超限明确说明，不偷偷改已有3/8/20/12000上限。按坐标排除重复/冲突，按字段和原单位分别计算十进制合计及明确标为数据行数/字段去重值数的计数；不把行数冒充业务对象数，不跨表合并不同单位，不含原有合计行再重复相加。仅对实际完整入模的组输出计算结果，缺行、未求值公式或不可靠字段只保留局部事实及具体缺口。

2026-10-06 候选补核验：未闭合inline代码在结束时按正文重新检查，代码结束必须匹配完整反引号长度；fence限于有效行首、关闭行和同种字符，波浪线fence保留。嵌套普通括号中的真实代码/链接不误拒，非法数字完整确认前不释放。26项普通/逐字符流式回归先16 RED，再嵌套4 RED，最终GREEN；不宣称实现完整GFM解析器。计算另有备注字段“合计”误排整行的因果RED，改为只按首个标签列识别并回显被排除汇总行，普通字段不参与排除。

发布依赖只读核验：正式API、Importer、Embedder均openpyxl 3.1.5/SQLAlchemy 2.0.52/asyncpg 0.31.0，正式PG16.14。本机原隔离PG18.4之外，新增独立便携PG16.14，匹配版本回归已验证，两个测试实例均已停止。本地Docker守护进程不可用，未自动启动或操作原服务。正式三个角色源码并不一致：API缺PDF质检及新Chat取证模块，Importer已有PDF质检；API的parser-unit/locator/DOCX与本机不同，Importer相关三文件与本机一致；evidence_write_path三角色一致。源码与哈希只保存在本机任务中；S1/S2已逐角色建立精确两文件候选，S3–S5不能把当前全文件或dirty checkout直接视为精确发布包。

全工作树图变更此前CLI检查固定展示15符号/10流程。本轮通过已安装GitNexus官方LocalBackend获取完整原始结果，最新72个已跟踪文件、431符号、143流程及CRITICAL；数组长度与summary匹配，无error/partial/truncated标志。该结果包含其他窗口已有权限/多库检索等差异，不是本目标精确发布清单，不能宣称图审查干净，也不将未跟踪新增Owner视为已由git diff全部覆盖。S1/S2发布payload另有逐符号impact与UNKNOWN补证。隔离测试PG已停止，原本机和正式服务保持原状态；无提交或新增部署。

## 2026-10-06 Office / Excel 发布候选与匹配版本补核验

- S2补发现并修复真实合成XLSX中的资源与数据丢失：全列样式空行不再扩展成巨量None网格；不信任过小dimension遗漏晚行；实际超界行仍拒绝；非有限数值及越界日期保留原文。变长双迭代器曾造成无缓存公式位置丢失，已有provenance回归现场RED后补齐None坐标并通过。均为工作簿局部处理，无openpyxl全局补丁。
- 最终七模块207 passed（preflight、xlsx、upload inbox、archive、importer inbox、parser provenance、locator）；Ruff E9/F、compileall及全工作树diff-check exit0。不同测试集互有重叠，不累加为全仓库通过数。
- 两角色精确候选各只含preflight/xlsx两个运行文件，保留各自生产其余解析/上传依赖；API overlay108 passed，Importer106 passed/2 deselected。Importer排除两项其生产源码没有的API完成上传hook，最初未筛选92 passed/2 failed如实保留；未补生产测试旁路。每角色一项pytest已导入插件警告，不是解析警告。
- 实际既有上传路由：`.et`、`.wps`及无后缀仅保存原件；`.xls/.xlsx/.doc/.docx`才进入正文处理。预检兼容不等于别名具备全文解析，不扩大原件保存规则。Word错扩展名仍拒绝。
- 从官方发行包提取独立PG16.14，不安装服务、不改系统PATH；仅本机合成测试库。rebuild_revision_pg、chat_evidence_pg、chat_table_calculations_pg三模块26 passed；最终Excel修复后计算PG3项再验证通过并停止实例。本机Windows/Python3.14.5仍不等于正式Linux/Python镜像。
- GitNexus最新完整图431符号/143流程；row renderer为HIGH8，涉及XLS/XLSX与Chat快照计算，已报告并覆盖相关回归。parse_cell UNKNOWN由openpyxl parse_row动态调用确认；预算/物理行上限UNKNOWN由实际guard引用补证，没有把零caller当作低风险。
- [本机候选manifest](../../.trellis/tasks/10-05-pending-fixes-goal/office-excel-candidate/manifest.json)和[发布叠加包](../../.trellis/tasks/10-05-pending-fixes-goal/office-excel-candidate-20261006.tar.gz)已生成并验证6个普通文件成员：4个运行payload、manifest、impacts。仅源码，不含凭据、环境文件或数据。发布前须重新核对容器/镜像/source哈希，构建实际候选镜像并核验运行、健康及切换后烟测；当前没有上传、构建或重启生产容器。
- 回退：按Importer→API顺序分别切换；保留原容器及配置，每步失败恢复该角色原容器并核对健康/哈希。无迁移，不重解析历史文档。部署授权须针对此候选核定，旧前端发布授权未扩大。
- S5原独立同类集未找到实现前冻结证据，明确列为规划偏差，不能事后伪造S0或把训练过的合成回归叫独立验收。原题绑定/原件真值、独立审查及有界真实模型回放另核授权；本轮未调用额外provider。目标仍active，S5 in_progress，真实质量和生产镜像验收仍待完成。

## 2026-10-06 重建生产依赖补核验

只读取得正式API/Embedder的模型、ingest、projection、embedding、Qdrant及两Owner源码；模型和ingest与本机归一化相同。两角色均保留各自Qdrant/projection，仅替换重建/Embedder候选后，七个服务/Worker模块分别89 passed，含11个真实本机PG16.14用例；模型和向量网络仍为边界替身。测试结束停止隔离PG。

初次把完整API外部库拒绝模块放入overlay，两个角色均在collection失败：本机app.main带入另一个S5 Chat改动，要求生产旧projection没有的新helper。未增加生产兼容旁路或上传Chat；缩小overlay声明为服务/Worker七模块，该API全入口仍未验证。完整API镜像必须独立证明，不能把排除模块后的通过数称为全部API通过。

[重建候选manifest](../../.trellis/tasks/10-05-pending-fixes-goal/rebuild-candidate/manifest.json)为`service_worker_overlay_verified_not_deployment_ready`。正式API的Embedder源码比正式Embedder旧，API全文件候选包含更多既有Worker对齐差异，尚须逐hunk收敛发布范围。API Qdrant存在批量写入差异但本候选保留其生产版本；projection仅缺S5新增helper，本候选不携带该项。当前未上传或构建该候选。

21个payload符号影响已完整记录，_prepare/_activate/_resume_state/_retry_jobs/try_finalize及eligibility/_process_job/普通发布等多处HIGH，涉及普通摄入及重建共用路径，已报告。目标仍active；下一步先收敛API角色Worker对齐范围及完整API运行依赖，随后真实provider、PDF/Chat质量与浏览器按具体授权验收。

## 2026-10-06 完整正式源码候选收敛

本轮进度和精确包以[候选核验记录](../quality/pending-fix-candidate-review-20261006.md)为当前依据。保留前段失败历史：S3新范围已收敛为API仅rebuild、Embedder为rebuild/embedder；正式两角色356/352个Python源分别保留其余代码，本机API完整入口及16项HTTP/服务/PG回归通过，Embedder实际入口与64项六模块回归通过。API不再携带额外Worker对齐改动。Embedder不含PDF coverage且不作为API/Importer运行时，跨角色导入失败保留记录；未为通过测试补生产旁路。

S5保留正式API其余源码及原样式/其余脚本，只叠加六个Python和两个前端文件；十四模块224 passed含15项PG16.14，正式前端依赖下七模块85 passed；真实源码TestClient的两个Chat脚本及CSS三处HTTP200/hash一致。47个payload符号含17个未跟踪Owner显式impact；共享上下文、prompt与可信位置HIGH，UNKNOWN补动态注册/读取确认。八文件叠加包已生成，不含基线、测试、环境或业务数据。

目标仍S5 in_progress/pending。正式Python3.12.14与本机3.14.5不一致；uv当前无3.12.14 Windows下载，不用其他版本冒充。Linux镜像、原七题与独立真实生成、性能和浏览器、S4人工真值/OCR/MinerU/ABBA/MinIO仍未验证。Office/Excel发布问题未获答复，本轮继续本地准备，无新增部署或provider调用；PG测试实例均已停止。

## 本地续接目标完成；真实验收继续保留

本轮按[用户批准合约](../../.pi/goal/续接五项修复-完成本地候选验收-20261006-0522.md)执行，六条本地验收均met。证据、命令、失败恢复、包SHA和格式边界集中在[候选核验记录](../quality/pending-fix-candidate-review-20261006.md)及[本机验收JSON](../../.trellis/tasks/10-05-pending-fixes-goal/browser-acceptance/result.json)，不将各组结果累加为全仓库验收。

新增业务差异仅Chat引用片段弹窗宽度一行：旧342px视口中620px弹窗裁掉关闭按钮，新宽度受视口约束，因果浏览器RED→GREEN。实际候选Chat/API/SSE代码与合成响应覆盖普通展示、分段流式、历史、PDF页/Excel行位置及缺号引用；缺失历史定位保持空，不用当前页码补造。保存快照缺失的mjs依赖由项目已有本机vendor补齐，因此不是正式镜像或真实鉴权/provider验收。修改前UID impact LOW3，UNKNOWN继续补证；未宣称全工作树图审查干净。

隔离PG16.14用例只含合成数据及逐用例schema，正常停止；原10个PG进程全部保留。任务保留可复核PG入口、浏览器夹具、截图和结果。问答包仍8运行payload/10成员，424其余候选源码SHA不变；Office/Excel与重建包哈希保持原样。Ruff、编译/Node语法及命令级diff-check通过，未暂存/提交，多库检索和其他成果不动。

剩余未验证仍为真实PDF人工真值/OCR/MinerU/ABBA/MinIO队列、原七题与后置独立真实生成、正式Linux/Python3.12.14/Provider/Qdrant/在途兼容及真实鉴权浏览器。它们属于五项总任务的后续范围，不阻塞已批准的本地候选目标完成；生产发布、迁移、重建及额外模型调用均未执行。

## 2026-10-07 三个精确候选已发布

本窗口收到明确上线授权，S1/S2 Office/Excel、S3重建版本绑定、S5问答取证与引用已发布，详见[正式发布记录](../quality/pending-fix-release-20261007.md)。正式Linux/Python3.12.14与PG16.14合成回归、精确源码树、运行配置、入口ready、静态哈希/401及独立复核通过；原角色容器保留。首轮入口解析失败自动回退，保留各角色原地址后重试通过，不隐去失败历史。生产切换时在途旧任务/操作计数为0，未据此声称任意在途兼容场景全部实测。

S4 PDF真实质量/性能/队列、原七题及后置独立真实模型回答、真实登录浏览器继续未验证。未发布PDF候选、未启用级联，未迁移、未重解析历史或发起生产重建，未额外调用模型/OCR/MinerU。多库检索与其他未提交成果不动；五项总任务不因本次三包上线被标作全部完成。
