# 五项修复候选核验（2026-10-06）

> 状态更新（2026-10-07）：Office/Excel、重建和 Chat 已获授权并发布，详见[正式发布记录](pending-fix-release-20261007.md)及[当前状态](deployment-gap-status-20261007.md)。下文保留 10 月 6 日候选验证与当时授权边界，不能据此将已发布项重新报为未上线。

唯一阶段计划：[修复计划](../roadmap/pending-fixes-goal-plan-20261005.zh-CN.md)。本记录只证明列出的源码与本机检查，不证明正式镜像、真实模型质量或生产生效。多库检索不修改，工作树无关成果保留，未暂存/提交。

## 当前候选

| 项目 | 精确运行范围 | 本轮新鲜证据 / 历史补充 | 状态 / 未验证 |
| --- | --- | --- | --- |
| S1 Office / S2 Excel | API、Importer各preflight/xlsx两文件 | 本轮合约四模块140 passed；此前七模块207及两角色overlay108/106为历史补充 | 包/source/hash重新核对通过；正式Linux镜像、真实处理烟测未验证 |
| S3 重建 | API仅rebuild；Embedder为rebuild/embedder两文件 | 本轮binding/external_guard22 passed，PG16.14专项11 passed；此前完整正式源码API16/Embedder64为历史补充 | 包/source/hash重新核对通过；正式镜像、真实provider/Qdrant、在途任务及大库性能未验证 |
| S4 PDF | 已有共同解析入口与本地质量/评估修复 | 本轮entry_consistency/quality/evaluate/routing/preflight/mineru六模块114 passed；此前228为历史证据 | 本地契约通过；人工真值、真实OCR/MinerU、AB/BA质量/成本/尾延迟、MinIO持久化队列未验证；级联未启用 |
| S5 问答 | API六个Python文件及Chat/citation两个前端文件 | 本轮十四后端模块224 passed含15本机PG；候选前端七模块85 passed；真实浏览器10项交互及342/1280宽度核验通过 | 更新8文件候选包；合成浏览器已核验，原七题/独立真实答案、真实鉴权浏览器、性能及正式镜像未验证 |

各测试集互有重叠，不累加成全仓库通过数。完整API源码导入是本机Python进程，静态HTTP为TestClient，模型/检索外部边界仍替身；不把这些当真实用户浏览器或生产provider验收。

## 依赖与影响

正式API、Importer、Embedder关键依赖均openpyxl3.1.5/SQLAlchemy2.0.52/asyncpg0.31.0。最新API/Embedder只读核验Python3.12.14、running、RestartCount0；本机测试Python3.14.5。当前本机uv未找到3.12.14 Windows发行包，未用其他补丁版冒充匹配，未安装默认解释器或修改注册表/PATH。

S3完整源码初次误按API入口导入Embedder角色，发现缺pdf_coverage；正式该角色find_spec也为false。它的实际Embedder入口导入正常；API/Importer不应复用此角色源码或镜像作为完整API/解析运行时。共享trial测试模块会导入Importer，所以完整Embedder角色测试不包含该跨角色模块；get_worker_target_config与正式Embedder的AST完全相同，前轮角色依赖overlay另覆盖25个配置用例。两次失败与边界均保留，不填假的模块或生产测试旁路。

正式API保留旧Worker文件没有影响S3 API验证：图谱eligibility/get_worker_target_config调用方为Worker自身，原始API/rebuild/main/ingest文字导入补查无Embedder引用；正式API完整源码与只改rebuild的HTTP/PG服务回归均通过。精确角色范围优先于把所有角色统一到本机dirty checkout。

GitNexus完整原始图变更为72已跟踪文件、431符号、143流程、CRITICAL；数组数量与summary一致，无error/partial/truncated标志。风险包含其他窗口成果，未作为干净审查或发布清单。S3 payload21符号逐项影响，多处HIGH涉及普通/重建共用准备、激活、发布和消费；S5单列30已跟踪payload符号与17未跟踪新Owner符号，47项影响均已取得。build_context、_messages、可信位置绑定/标签为HIGH，涉及普通Chat、流式、历史和共享公开回答，相关公开回答契约回归已执行，真实生成仍未验收。

UNKNOWN另补实际FastAPI decorator注册、Vue setup回调、prompt读取与两guard构造处，未以零调用方断言未使用。S1/S2 row renderer HIGH影响XLS/XLSX及Chat计算，相关解析/SQL计算回归已覆盖。源提取边界仍是图谱能力限制，不宣称完整证明所有动态运行。

## 可审阅包与恢复

| 包 | 内容 | 记录 |
| --- | --- | --- |
| [Office/Excel叠加包](../../.trellis/tasks/10-05-pending-fixes-goal/office-excel-candidate-20261006.tar.gz) | 4个运行payload+manifest/impacts，共6普通文件 | [manifest](../../.trellis/tasks/10-05-pending-fixes-goal/office-excel-candidate/manifest.json)；本机依赖核验完成，尚未部署 |
| [重建角色叠加包](../../.trellis/tasks/10-05-pending-fixes-goal/rebuild-candidate-20261006.tar.gz) | API1+Embedder2运行payload及manifest/impacts，共5普通文件 | [manifest](../../.trellis/tasks/10-05-pending-fixes-goal/rebuild-full-source/manifest.json)；取代上一版API也携带Worker的准备范围，Linux/真实运行待验收 |
| [问答叠加包](../../.trellis/tasks/10-05-pending-fixes-goal/chat-candidate-20261006.tar.gz) | 6个Python+2前端运行payload及manifest/impacts，共10普通文件 | [manifest](../../.trellis/tasks/10-05-pending-fixes-goal/chat-full-source/manifest.json)；真实回答与浏览器待验收 |

包只含白名单源码与元数据，不包含完整源码基线、测试、环境文件、凭据或业务数据；成员路径非绝对、无父级跳转，每个payload SHA与候选一致。私有源码基线仅保留在本机忽略任务空间。发布前必须即时重核正式image/container/source身份并构建实际镜像，不能用旧manifest掩盖别的窗口后续发布。

S1/S2按Importer→API逐角色切换；S3按Embedder→API，切换前须只读审查在途任务与旧operation快照兼容；S5仅API。每项保留原容器配置及镜像，异常恢复该角色原容器并核验健康/hash。没有迁移，不自动重解析、生产重建或开启PDF级联；问答历史保留原版本含义，不覆盖旧回答制造成功。

## 真实验收缺口与授权

S5未找到独立集在实现前冻结的证据，此为已发生的规划偏差；后置独立审查必须标明时间，不伪造S0。建议下一步先在已有服务器私有范围绑定原七题及原件/revision/片段哈希，对未知字段只暂停该项真值成功判定；再由未参与规则调试的审查者建立八个同类用例，覆盖六类问题并单列基线/候选结果。真实回放上限沿用原题17次加独立8题各基线/候选16次，共33次；不是必须耗尽的次数，不扩展原provider、库权限或12,000字符证据预算。样本未绑定前不调用模型，也不把测试原样改写算独立收益。

Linux临时候选构建/运行与模型回放需要具体授权，正式切换另核定；原件、文档revision、解析块、chunks和向量应保持hash/数量不变。PDF真实OCR/MinerU及AB/BA另列样本、调用预算和授权，不能借问答验收批准启用级联。Office/Excel已提出的具体部署确认尚未收到回答；本轮无生产上传、构建、重启、数据库写入或额外provider调用。

## 本轮命令入口

```powershell
.\.venv\Scripts\python.exe .trellis/tasks/10-05-pending-fixes-goal/verify_rebuild_full_source.py api
.\.venv\Scripts\python.exe .trellis/tasks/10-05-pending-fixes-goal/verify_rebuild_full_source.py embedder
.\.venv\Scripts\python.exe .trellis/tasks/10-05-pending-fixes-goal/verify_chat_full_source.py
.\.venv\Scripts\python.exe .trellis/tasks/10-05-pending-fixes-goal/verify_chat_static_api.py
node --test <本机候选admin-ui内七个记录的测试模块>
node .trellis/tasks/10-05-pending-fixes-goal/read_graph_change_report.mjs chat
git diff --check
```

PG命令必须仅设置loopback、`*_test`独立库DSN并正常关闭本任务测试实例。当前PG16/PG18隔离实例均已停止。目标保持active，所有候选及未完成项仍按主计划继续，未执行部署或缩小五项总范围。

## 已批准续接目标：本地候选验收完成

本轮执行[已批准目标](../../.pi/goal/续接五项修复-完成本地候选验收-20261006-0522.md)。六条本地验收均已满足；五项总任务仍保留真实质量、正式镜像及生产发布缺口，未扩展为生产授权。结构化证据与截图见[验收结果](../../.trellis/tasks/10-05-pending-fixes-goal/browser-acceptance/result.json)。此前段落中的“浏览器未验证”是当时状态，本轮仅关闭合成 API 边界下的本机交互缺口。

| 合约标准 | 结果 | 本轮观察 |
| --- | --- | --- |
| 1 Office/Excel | met | 四个指定模块140 passed、exit0：非法容器/加密拒绝、异常值保留、正常类型/稀疏内容和预算、原生调用无串扰 |
| 2 重建 | met | binding/external_guard22 passed；独立PG16.14 revision专项11 passed，绑定/恢复/消费/并发/过期及普通发布隔离契约通过 |
| 3 PDF | met | 指定三模块加routing/preflight/mineru三模块114 passed；真实合成入口、授权/未知/资源边界通过，真实质量与队列仍单列未验证 |
| 4 问答 | met | 十四后端模块224 passed，PG用例实际连接合成库而非skip；七候选前端模块85 passed，无skip/fail |
| 5 浏览器 | met | 实际候选组件、API/SSE解析器与合成响应；普通展示、分段流式、历史、可信PDF/Excel位置、缺号与来源点击10项通过，最终交互期错误0 |
| 6 交付 | met | 三包白名单/普通成员/安全路径、压缩包内manifest/impact及全部payload与当前源码SHA匹配；Ruff、compileall、Node语法与diff-check通过，任务/计划回写 |

### 新发现并修复的弹窗裁切

真实342px视口中引用弹窗固定620px，关闭按钮右边界620px，超出屏幕。新增浏览器断言在旧代码明确RED。仅改`Chat.js`的引用弹窗宽度为`min(620px, calc(100vw - 24px))`；342px视口宽318px、左右12/330px，关闭按钮可见；1280px iframe真实布局视口保持620px，均GREEN。未新增CSS、主题、业务API或发布文件。最终截图保存在上述任务证据目录。

修改前GitNexus函数UID`Function:admin-ui/src/views/Chat.js:setup.openCitationChunk@326:8`取得LOW、3直接引用、1受影响流程；调用方为点击、键盘及setup。同名返回属性与新任务夹具UNKNOWN已按Vue回调及实际浏览器导入文本补证。保存的431符号/143流程/CRITICAL为此前脏工作树分析，没有冒充本轮完整干净审查；本轮未提交。

浏览器夹具初版缺生产快照未保存的`.mjs` vendor，改由项目已有本机vendor解析；不证明正式镜像内依赖。普通回答展示通过夹具适配器，因为现有页面发送入口仅SSE；流式与历史使用候选原API代码及实际fetch/ReadableStream边界。夹具等待关闭动画完成，并检查可见弹窗，避免快速脚本点击造成虚假通过。控制台历史仅保留最初依赖缺失的一条旧错误，最终加载及交互没有新增错误。没有调用真实模型、业务API或加载业务原件。

### 本轮可复核入口

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/test_import_upload_preflight_1b.py tests/test_archive_uploads.py tests/test_xlsx_extract.py tests/test_m1_parser_provenance.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_rebuild_revision_binding.py tests/test_rebuild_external_guard.py
.\.venv\Scripts\python.exe -m pytest -q tests/test_pdf_entry_consistency.py tests/test_pdf_quality_inspector.py tests/test_evaluate_pdf_cascade.py tests/test_pdf_routing.py tests/test_pdf_preflight.py tests/test_mineru_pdf.py
.\.venv\Scripts\python.exe .trellis/tasks/10-05-pending-fixes-goal/run_local_pg_acceptance.py
git -c core.safecrlf=false diff --check
```

PG实际成功执行的是会话scratch中的同内容脚本；上方持久化副本已逐字节核对。仅loopback、`goal_resume_test`合成库、逐用例隔离schema；PG16.14正常停止exit0、端口不监听，原10个PG进程全部保留。初始宿主RPC超时和原生命令启动等待均曾失败，隔离daemon标准输出后恢复；没有把失败运行当通过。相关弃用警告是Starlette/httpx及Alembic配置，未改无关依赖。原始diff命令出现LF/CRLF告警，命令级`core.safecrlf=false`检查exit0，没有更改Git配置。

打开[浏览器夹具](../../.trellis/tasks/10-05-pending-fixes-goal/browser-acceptance/index.html)，执行`import('./run-interactions.mjs').then(m => m.runInteractions())`；包复核为`import('./verify-packages.mjs').then(m => m.verifyPackages())`。七个Node模块名及后端十四模块在任务记录与PG脚本中固定，均在本轮实际执行。

### 精确包与恢复

Office/Excel包SHA仍`b7eb7202d088867a6bd7e7605d55623044367e37bb8847bb81b96c52f9710aff`，6成员/4payload；重建仍`2225f20b16e982d571110ed6b840b72b7b8a0e85455dd933924c3988a8212dec`，5/3；问答更新为`cb2943a88c6d595326fca81f222fb54f8e0119e485fd73670f503852e7ce807e`，10/8。旧问答SHA只作为历史保留，新包以当前manifest与上述验收JSON为准。问答其余424个候选源码SHA保持原样；编辑工具统一原混合换行，归一化文本对照旧包确认仅增加一行宽度修改。

本轮恢复只需将新增宽度属性还原为620px并重新生成对应候选；不得整文件checkout覆盖已有问答成果。生产回退仍按前述角色保留旧容器方案，本轮没有上传、构建、切换、重启或生产写入。真实模型/PDF质量、原件真值、MinIO/Provider链路、正式Linux/Python3.12.14及真实鉴权浏览器依旧未验证；后续另行核定具体验收范围。本地续接目标完成，五项总任务继续保留这些缺口。

## 2026-10-07 生产发布更新

用户本窗口另行授权后，三个精确候选已上线，详见[正式发布记录](pending-fix-release-20261007.md)。此前“未部署/正式Linux未验证”属于历史状态；本轮正式Linux/Python3.12.14及隔离PG16.14下Importer106、Embedder64、API357项回归通过，前端85通过。发布首次入口失败后实际自动回退，保留各角色原内部地址重试后ready、源码/运行配置、静态哈希、401和独立审计通过。旧容器保留。PDF候选及多库检索未发布；真实回答质量、登录浏览器和PDF真实质量/性能仍未验证，不由部署健康代替。
