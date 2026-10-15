# 自动索引同步

MySQL 是文档状态和当前 build 的权威来源。管理变更与待同步目标在同一事务提交，常驻 worker 自动构建并发布 ES generation。启用命令不等待 embedding；等待期间可能缺少新内容，查询继续过滤已替换或停用的材料，并返回 `sync_lag`。

## 升级与运行

在 `conda rag` 安装原解析及 `requirements-search.txt` 依赖。升级前停止管理写入、worker 和手动索引操作，备份 MySQL 与 `RAG_STORAGE_ROOT`，再运行：

```powershell
python -m rag.manage db upgrade
python -m rag.manage db check
# 默认 all，前台常驻；Ctrl+C 停止。
python -m rag.manage worker run
```

迁移 `0004_index_sync` 新增 `index_sync_states`、`index_sync_attempts`，共十一张业务表。已有知识库初始化为待核对，新知识库在创建事务内初始化；首次执行若现有 alias 的语料、模型、检索指纹一致且记录校验通过，直接确认完成，不重复 embedding。

`worker run --kind all|processing|index` 默认 `all`，两类都有到期任务时交替执行；每次只执行一个任务，避免两类后台模型任务同时运行。`--kind processing` 可用于未安装检索依赖的处理环境。所有模式共用原数据库 worker 命名锁，不能分别启动两个 worker。索引配置、客户端及模型只在索引任务执行时加载；配置错误使该索引任务失败，不妨碍后续文档任务。

`--once` 执行一个到期任务；成功或 idle 返回 0，失败、延期或仍未完成返回 1。常驻模式输出任务类型、知识库/任务 ID、状态和固定错误码。空闲轮询 5 秒；数据库故障或无法持久化执行状态时退出，重新启动后恢复租约。没有安装 Windows 自动启动或进程守护，需要保持 worker 常驻。

本轮修改管理源码，会改变已有处理任务的配置身份。旧任务记录与成功结果保留；未完成任务可能报告 `PROCESSING_IDENTITY_CHANGED`，需重新执行 `document process` 按新身份提交，不改写旧任务快照。更换本地模型或执行代码后应重启 worker，并显式同步确认新模型身份。

## 触发与状态

文档版本启用/回退、导入批量启用、知识库或文档 active/disabled/archived 切换均触发同步。版本启用同时更新分类，分类变化也计入目标。一次批量启用只递增一次知识库目标修订号；重复设置相同 build、分类或状态不重复排队。原文件注册、build 生成、知识库名称/描述更新不触发同步，处理完成仍需要显式启用。

首次变更延迟 5 秒，后续变更合并且不延后原定时间；执行期间变更递增目标，但不覆盖正在运行的执行标识和租约。发布前发现目标变化或自动同步已暂停，保留封存候选、记为 superseded 并立即追赶最新目标。暂停期间只记录变更，不领取任务。

```powershell
python -m rag.manage index status --kb-id 1
# 不连接 ES，也不加载检索依赖；可在 ES 故障时检查队列。
python -m rag.manage index status --kb-id 1 --db-only --limit 20 --offset 0
python -m rag.manage index pause --kb-id 1
python -m rag.manage index resume --kb-id 1
# 仅 failed 可人工重试；不会解除暂停。
python -m rag.manage index retry --kb-id 1
```

激活及状态修改返回 `sync_target`（知识库、目标修订号、状态、暂停标志）。`index status` 保留原索引字段，新增 `sync_task` 与分页 `attempts`；`--db-only` 只返回知识库、同步任务和尝试记录，不声称确认 ES 当前发布状态。`desired_revision` 是目标，`applied_revision` 是最后确认完成的修订号，`generation` 是最后成功同步的一代；实际发布情况仍以 alias 为准。

任务状态 `pending/running/succeeded/failed`，暂停是独立标志。尝试状态另包含 `interrupted/superseded/deferred`。目标变化后失败任务重新排队；人工 retry 重置当前重试周期但保留累计尝试。status 中不返回执行 token，不保存异常正文、凭据或原始请求 payload。

## 一致性、故障与回滚

队列目标与有效 build 清单在同一个 REPEATABLE READ 快照读取。复用全量构建、逐项 bulk 检查、内容/身份/数量校验和只读封存；自动同步也支持停用知识库的空索引，查询继续拒绝停用知识库。新 generation 默认 dense，其它模式继续通过原检索评测门槛选择；不自动继承旧代的模式晋升。

停用/归档知识库没有有效 build，语料选择直接返回空清单，不校验隐藏的当前 build，以免其异常阻碍清空检索投影。历史 generation 回滚独立校验目标的身份、ready/只读状态、检索记录数量及内容哈希；该校验不依赖知识库是否启用，损坏目标在暂停或切换 alias 前就被拒绝。

模型推理不持有管理行锁。自动同步、手动同步和回滚共用每知识库命名锁；发布边界持有同步状态行锁，核对目标、暂停、租约和命名锁所有权后切换 alias 并提交完成状态。MySQL 与 ES 没有跨系统原子事务；若 alias 已切换但数据库状态提交中断，重启后校验实际 alias，匹配即确认完成，不重复构建。发布后发生的管理变更会重新排队，不被旧执行覆盖。

租约采用数据库 UTC：有效期 120 秒，每 15 秒续租。瞬时心跳故障重试两次，间隔 1、2 秒；失去执行租约或锁的执行不能确认成功，等待租约回收。过期尝试标为 interrupted 后重新执行。

网络超时、HTTP 408/429/500/502/503/504、数据库断连或临时锁故障最多执行三次，间隔 30、120 秒。ES 请求和 bulk 失败根据结构化状态分类，混有永久 bulk 错误时不自动重试；认证、配置和内容校验错误立即 failed。同知识库索引锁竞争记为 deferred，5 秒后重试，不消耗失败预算。原 ES 客户端和 bulk 内部有限重试保持原有配置。

失败保留原 alias。旧版本不会因为旧 alias 仍存在而重新进入查询；缺少索引继续明确报错。失败/中断/过期候选和旧 generation 保留，首期不自动删除。

```powershell
# 保留同步执行的运维入口，仅支持 active 知识库，强制构建一代；不解除暂停。
python -m rag.manage index sync --kb-id 1
# 先完整校验目标，再持久化暂停并切换 alias；切换失败也保留暂停。
python -m rag.manage index rollback --kb-id 1 --generation <32位generation>
python -m rag.manage index status --kb-id 1
# 恢复后立即追赶 MySQL 当前版本。
python -m rag.manage index resume --kb-id 1
```

ES 索引回滚不改变 MySQL build；回滚会记录新的待核对修订号并暂停，后续管理变更也不自动解除暂停。文档 build 回退仍是普通版本启用，正常自动同步。回滚及手动同步遇到同知识库执行锁竞争返回 INDEX_BUSY，稍后重试。

手动 `index sync` 沿用原入口约束：停用或归档知识库返回 `RESOURCE_DISABLED`，不领取同步任务、不创建 generation、不切换 alias。若预检查后发生并发停用，执行快照再次拒绝手动同步，并保留自动任务为 pending。停用/归档时的空索引由自动任务发布；手动执行 `index sync` 前需先启用知识库。

## 备份恢复与验证

备份前停止管理写入、worker 和手动同步。隔离恢复保留成功处理结果和索引尝试历史，running 尝试记为 interrupted；清除执行 token、租约和旧 namespace 的索引进度，递增待核对目标并排队，保留暂停设置。恢复后启动 worker，暂停的知识库需显式 resume。已有索引尝试记录时禁止降级删除历史，使用升级前备份回退。

```powershell
python -m unittest discover -s tests -v
# opt-in：仅创建/清理专用测试库及索引。
$env:RAG_RUN_MYSQL_TESTS="1"
$env:RAG_RUN_ES_TESTS="1"
python -m unittest discover -s tests -p "test_index_sync*.py" -v
Remove-Item Env:RAG_RUN_MYSQL_TESTS
Remove-Item Env:RAG_RUN_ES_TESTS
```

覆盖事务回滚、去重合并、批量启用、调度公平性、租约恢复、指纹复用、构建期间变更/暂停、alias 发布后 DB 提交中断、ES 回滚、错误分类与重试。验证产物放入已忽略的 tmp，结束运行 clean-artifacts dry-run；不运行全量 benchmark。

真实本地 BGE 验收额外设置 `RAG_RUN_BGE_TESTS=1`，默认跳过；使用合成单文档及隔离真实 MySQL/ES，不调用 MinerU 或 LLM。实施验证及本地库升级结果见 [本轮验收](auto_index_sync_acceptance_20261014.md)。
