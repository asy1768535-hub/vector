# 待发布修复的正式环境发布记录（2026-10-07）

用户在本窗口明确授权“没有问题的话就上线”。沿用[本地候选验收](pending-fix-candidate-review-20261006.md)，本次发布 Office/Excel、重建、Chat 取证与引用三个精确候选。PDF 真实质量未闭环，本次不发布其候选、不启用级联；多库检索不动。不是整个脏工作树发布。

## 切换前结论：可发布这三个候选

- 即时 API、Importer、Embedder 容器身份、镜像和已保存基线一致，ready，RestartCount=0。生产只读查询：导入、embedding、旧重建的 pending/processing/preparing/running 均为零。迁移 current 为0076；没有执行迁移。
- 原三个候选归档成员、manifest/impact字节、payload与当前源码及记录SHA一致。合并后 API11文件、Importer2文件、Embedder2文件；其余源码严格保持当前生产值。
- Importer和Embedder包含既有容器可写层源码，不能只拿基础镜像覆盖。捕获并保留这些正在运行的源码，未从本机 dirty checkout 补入其他文件。候选与当前生产全 app/admin-ui 文件树差异严格等于本次白名单；已有PDF、Qdrant、配置与其他修复没有被回退。
- 基于当前镜像构建派生镜像，正式 Linux/Python3.12.14 中验证：Importer106 passed/2 API专属hook deselected；Embedder64 passed；API357 passed/2既有弃用警告。PG16.14使用隔离内部网络、无对外端口、全合成测试库；测试没有生产凭据。测试实例与网络已清理。
- 本机七前端模块85 passed，无失败、跳过或取消；全工作树命令级`core.safecrlf=false diff --check`通过。
- 本次没有修改业务函数；沿用各包已有 graph impact。重建编排/消费/收口和Chat共享上下文/位置投影存在HIGH影响，已据此验证普通发布隔离、当前版本、权限负例、公开回答、流式和历史契约。全脏工作树历史CRITICAL不作为干净审查。

执行入口与结构化结果在本机忽略任务[发布任务](../../.trellis/tasks/10-07-pending-fixes-release/prd.md)，`build-result.json`记录实际候选镜像和合成回归结果。服务器运行秘密仅在服务器内存读取和比较，不写入文档或本机文件。

## 切换与恢复契约

切换前再次核验当前容器身份、全源码树、空队列和旧容器名称无冲突。短时先停止API以阻止旧版本创建任务，再按Importer→Embedder→API切换。重试时每个原容器保留为`vector-kb-<role>-release-rollback-pending-fixes-20261007-r2`，不删除。原环境值、命令、用户、工作目录、标签、挂载、网络、资源限制和重启策略保持一致；显式保留各角色原有内部地址，防止入口网关缓存解析失效。内部地址值不写入文档或本机证据。

失败自动停止并保留失败容器、恢复所有原角色容器；原生产数据不做迁移或批量修改。代码回退不能抹除在正常运行中已经完成的业务任务。完成后核验ready、源码、前端HTTP哈希、未鉴权Chat拒绝、进程稳定和无关项目容器身份。

## 剩余验收边界

本次不额外调用付费模型/OCR/MinerU，不重解析历史，不发起生产重建。真实问题答案质量、登录后的用户浏览器、PDF人工真值/真实provider质量与性能/持久队列仍未验证。合成回归和部署健康不代表这些质量平面通过。

## 发布结果

第一次切换的外部ready检查未通过，自动回退；三个原容器ID均恢复，随后入口ready。失败候选容器保留为`vector-kb-<role>-release-failed-pending-fixes-20261007-r1`，不删除。新API日志显示startup complete、无Traceback；入口网关配置使用固定名称转发且无resolver/变量转发。重试保留原内部地址，候选业务源码及镜像不变；最终以实际`deployment.json`和独立复核回写为准。原Worker在停止等待期限后退出137，前后队列为空，不能称其全部优雅退出。

重试已发布，随后独立只读审计`audit_deployment.py` exit0：三个角色running、RestartCount=0、启动后error/Traceback信号0；全源码树、环境值及运行配置与预期一致，原容器均停止保留。入口ready，database/embedding/initialization/migrations/object_storage/Qdrant/rerank全部ok；两个Chat静态文件HTTP哈希匹配，未登录Chat conversations返回401。迁移仍0076，活动重建/embedding/import均0，隔离测试PG已移除；无关项目容器身份/镜像/状态保持。

| 角色 | 已发布镜像 | 本次替换文件 |
| --- | --- | --- |
| API | `vector-kb-app:pending-fixes-20261007-r1-api` | 11 |
| Importer | `vector-kb-app:pending-fixes-20261007-r1-importer` | 2 |
| Embedder | `vector-kb-app:pending-fixes-20261007-r1-embedder` | 2 |

结构化证据：[发布结果](../../.trellis/tasks/10-07-pending-fixes-release/deployment.json)、[独立复核](../../.trellis/tasks/10-07-pending-fixes-release/post-release-audit.json)、[首轮回退](../../.trellis/tasks/10-07-pending-fixes-release/attempt-1-rollback.json)。本机与服务器实际执行的三个编排脚本SHA已核对；初始传输包与后续单独更新脚本的历史区分保留，不把初始包冒充最终编排版本。

后续回退必须先在服务器内存记录当前角色内部地址，再停止并保留新容器、恢复上述r2原容器名，确保恢复角色仍使用对应地址（必要时重新连接原Docker网络）后启动并核验ready/源码。入口缺动态解析，不能只依赖同名容器获得正确转发。r1的自动恢复已实测；未来r2回退尚未再次触发。保留失败容器及所有旧回退容器，不自动清理。
