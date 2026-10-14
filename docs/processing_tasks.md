# 原文件注册与可恢复处理任务

本阶段将 PDF/DOCX 注册、MinerU 解析、表格处理和切块接入 MySQL 管理链路。处理任务只生成不可变 build；版本启用和 Elasticsearch 同步仍由管理命令显式执行。

## 使用

在 `conda rag` 中运行，沿用管理、解析和检索依赖。升级前停止管理写入和 worker，备份数据库及 `RAG_STORAGE_ROOT`，再执行 `python -m rag.manage db upgrade`。迁移 `0003_processing_tasks` 新增两张表，业务表总数为九；不为已有文档创建任务或修改当前版本。

```powershell
# 示例使用现有默认知识库；其它知识库使用实际 ID。
$registered = python -m rag.manage document register --kb-id 1 --raw-root data/raw `
  --source-path "policy/中华人民共和国数据安全法_20210610.docx" | ConvertFrom-Json
$task = python -m rag.manage document process --version-id $registered.version_id | ConvertFrom-Json
python -m rag.manage task show --task-id $task.task_id

# --once 执行全库队列中最早到期的一项，不专门选择上面提交的任务。
python -m rag.manage worker run --once
python -m rag.manage task list --kb-id 1 --status failed --limit 100 --offset 0
python -m rag.manage task show --task-id $task.task_id --limit 100 --offset 0

# 失败原因修复后才能重试；历史尝试保留。
python -m rag.manage task retry --task-id $task.task_id

# 任务 succeeded 后，使用实际返回的 build_id 显式启用。
$result = python -m rag.manage task show --task-id $task.task_id | ConvertFrom-Json
python -m rag.manage document activate --document-id $registered.document_id --build-id $result.build_id
python -m rag.manage index sync --kb-id 1
python -m rag.manage search --kb-id 1 --query "数据安全风险监测" --mode bm25

# 前台常驻 worker；Ctrl+C 停止，未完成任务由租约恢复。
python -m rag.manage worker run
```

注册要求 PDF/DOCX 的相对逻辑路径，使用现有 Unicode NFC、正斜杠、路径大小写及目录边界规则。原文件按内容 SHA256 存储；同一路径同内容复用版本，同一路径新内容生成版本，路径变化视为新文档。注册及提交要求知识库和文档 active，不会修改当前 build、标题或分类。

`document process` 支持 `--model-path`、`--mineru-config`、`--max-tokens`、`--overlap-tokens`。默认采用现有本地 BGE tokenizer、MinerU 配置、400/50 token 预算及整份文档解析，不增加局部页、外部表格几何或重放参数。当前配置启用跨页表格单元格合并辅助，可能调用 DeepSeek-flash；项目表格边界分类默认关闭。密钥仅从执行进程的 `DEEPSEEK_API_KEY` 读取。

## 状态、身份与重试

任务状态为 `pending / running / succeeded / failed`，阶段为 `validation / parsing / chunking / publishing / persisting`。`task show` 返回任务信息、分页尝试记录，以及完成后的 `build_status/can_activate`。列表和尝试分页沿用 1–1000 条限制。空文档生成 `empty` build，任务 succeeded，但不能启用。

原文件版本与配置指纹确定唯一任务。重复提交返回同一任务，包括已成功或已失败任务；失败任务用 `task retry` 重新排队。配置、tokenizer 文件、处理实现或依赖变化生成不同任务。提交保存非敏感配置，执行核验提交身份并使用配置快照；不会将旧任务悄悄改用新的配置。缺少模型直接报告 `PROCESSING_MODELS_MISSING`，身份不同报告 `PROCESSING_IDENTITY_CHANGED`，需要新提交。

身份记录包含处理源码 SHA256、包版本、tokenizer 配置 SHA256，以及 MinerU 模型文件清单的大小与纳秒修改时间。源码范围覆盖解析/切块管线，以及管理层的编排、服务、存储、数据访问、模型、配置和错误模块。身份格式为 v2；旧格式的成功任务和历史 build 保留，旧格式未完成任务执行时报告 `PROCESSING_IDENTITY_CHANGED`，使用 `document process` 按新身份重新提交，不改写原任务记录。**大型 MinerU 权重没有逐次计算内容 SHA256**；大小和修改时间都不变的原位篡改不在当前身份检测保证内。任务及 build 元数据、CLI 错误均不保存或回显 API key、底层异常正文或请求 payload。

一次重试周期对可传播的网络超时、429/临时服务错误、数据库断连及锁冲突最多执行三次，重试间隔 30 秒、120 秒。其它错误立即 failed。人工重试重置该周期计数，累计尝试序号不重置。进程中断将尝试标为 interrupted，并重新执行该文档；不承诺重用半成品或外部调用仅一次。MinerU 内部已有的 LLM 重试和规则回退保持原有行为，不会被任务层改写。

`worker run --once` 成功或无任务返回 0，处理失败、延期重试或未完成返回 1。常驻模式逐项输出简短 JSON，单篇失败后继续队列；空闲轮询间隔 5 秒。数据库不可用等基础设施故障会使进程退出，租约过期后由重新启动的 worker 恢复；本阶段未添加系统服务或自动启动配置。

## 一致性与存储

同一数据库的模型 worker 用独立连接的 MySQL 命名锁互斥，重复启动返回 `WORKER_BUSY`。领取任务使用短事务及 `FOR UPDATE SKIP LOCKED`；推理期间不持有文档行锁。租约使用数据库 UTC 时钟，120 秒有效、每 15 秒续期。阶段和最终写入校验执行标识及租约，失效执行不能提交结果。运行中停用文档不会自动启用它；已开始处理允许留下历史 build。

心跳遇到瞬时错误时额外重试两次，间隔 1 秒、2 秒；确认 `TASK_LEASE_LOST` 时立即停止续租。瞬时重试耗尽或其它心跳错误由主路径在后续阶段边界处理：租约仍有效且数据库可写时保存失败尝试，按已有任务重试策略延期或结束任务。无法写入失败状态时 worker 退出，不继续领取其它任务，原任务等待租约回收；不会把一次瞬时续租失败直接当作已经失去租约。

执行目录位于 `tmp/processing-*`，使用 `TemporaryDirectory` 正常清理；异常终止可能留下目录。原文件及校验完成的处理资产进入已有 `RAG_STORAGE_ROOT/raw` 和 `snapshots`，不新增 release 副本。资产发布后，build、全部 chunks、任务成功状态及尝试结果共同提交；事务失败可能留下未引用的不可变资产，不产生可见的半成品 build。

任务保留独立审计记录，不创建伪造的 `import_runs`。既有快照导入复用同一 build 写入逻辑，其快照 JSON、build 指纹、版本激活和导入明细语义保持兼容。

## 备份、恢复与验证

备份前停止管理写入和 worker，沿用既有数据库与完整本地存储备份。恢复到隔离数据库时重绑原文件与资产路径，清除任务租约；原 running 任务成为 pending，原 running 尝试成为 interrupted。已成功结果保留，源数据库不变，恢复后需核验模型身份并手动启动 worker。存在任何任务记录时拒绝降级删除任务表，回退使用升级前备份。

```powershell
python -m unittest discover -s tests -v
$env:RAG_RUN_MYSQL_TESTS="1"
python -m unittest discover -s tests -p test_processing_mysql.py -v
Remove-Item Env:RAG_RUN_MYSQL_TESTS
```

集成测试使用自动创建、清理的真实 MySQL 数据库，覆盖注册/提交/领取并发、续租、旧执行保护、进程终止、三次重试、空文档、资产与存储错误、提交回滚、CLI、迁移及备份恢复。运行产物仅进入已忽略的 `tmp/`；每轮验收结束执行 `clean-artifacts` dry-run，不默认删除文件。
