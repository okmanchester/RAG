# 自动索引同步验收：2026-10-14

已实现事务排队、知识库级合并、统一串行 worker、租约恢复、有限重试、暂停/恢复和索引回滚保护。文档处理完成仍生成历史 build，显式启用后自动同步。使用与升级规则见 [自动索引同步](auto_index_sync.md)。

## 验证结果

| 验证 | 结果 |
| --- | --- |
| 当前树完整回归，启用隔离 MySQL/ES 与本地 BGE | 279 项全部通过，无跳过，277.984 秒；日志 `tmp/index_sync_review_full_tests.log` |
| 首次完整回归（历史快照），启用隔离 MySQL/ES 测试 | 263 项通过；日志 `tmp/index_sync_full_tests.log` |
| 首次自动同步专项（历史快照） | 40 项通过，1 项本地 BGE 验收默认跳过；日志 `tmp/index_sync_final_tests.log` |
| 本地 BGE 独立验收 | 1 项通过；最终复验日志 `tmp/index_sync_bge_final_tests.log` |
| 最终原检索与模式选择回归 | 36 项通过；日志 `tmp/index_sync_search_final_tests.log` |

首次完整回归之后增加了实际索引进程终止、迁移初始化/历史保护、索引心跳故障、发布后管理变更、未确认 alias 响应及候选 generation 审计测试，均在后续专项验证中通过。历史行记录各阶段实际执行结果，不代表当前树的用例数，也不将多轮重叠测试相加。

复核前当前树为 277 项；本轮补充两个停用知识库回滚校验测试后为 279 项，已重新全量执行。完整回归中本地 BGE 的新报告为 `tmp/index_sync_bge_acceptance_206e4455ca2c/report.json`。

验证覆盖激活与排队共同回滚、重复变更去重、批量只排一次、调度公平性、运行期间变更/暂停、租约失效阻止发布、进程终止恢复、临时错误重试耗尽、永久错误直接失败、ES 回滚暂停和隔离备份恢复。

alias 发布后、数据库提交前中断的注入测试通过：下一次执行检查实际 alias，直接完成核对，不重新 embedding。发布后立即提交的管理变更仍保持 pending，已同步修订号低于新的目标修订号，随后收敛。

## 本地 BGE 小样本

使用合成单文档、每 build 一个可检索块，在隔离真实 MySQL/ES 中运行本地 `bge-large-zh-v1.5`，1024 维归一化向量；没有运行 MinerU、LLM 或全量 benchmark。

旧 build 1 同步后启用 build 2，等待期间查询不返回旧 build；统一 worker 成功后返回 build 2，`sync_lag=false`。再次核对复用当前 generation，没有重新生成向量。该测试验证同步链路和真实向量接口，不代表检索质量评测。

报告：`tmp/index_sync_bge_acceptance_f497da3f063d/report.json`。测试约 24.615 秒，核心验收流程约 21.672 秒。模型权重 SHA256：`bf84a56fb045c24e090495195584a3922c3e4204107f0bba8b79c11f67a207f2`。

## 本地知识库升级

升级前只读确认没有处理任务、没有运行中的数据库 worker。持有 worker 及知识库索引命名锁，完成 MySQL 和本地存储备份，再升级到 `0004_index_sync`。

- 备份：`tmp/mysql_before_index_sync_20261014/`，37 个文件及哈希清单。
- MySQL 8.4.11，十一张业务表，会话 UTC。
- 知识库 1：33 个有效 build、1,308 个可检索块；初始自动核对 succeeded，目标与已同步修订号均为 1。
- 复用原 generation `ba519cfbbabb45f09b111ce446fba1df`，保留原默认模式 bm25，没有重新 embedding，`sync_lag=false`；BM25 查询返回 10 条上下文。
- 升级和核对报告：`tmp/index_sync_upgrade_report.json`、`tmp/index_sync_live_reconciliation.json`。

只执行了一次 worker 核对，未安装自动启动或保持常驻进程。后续自动处理依赖在 `conda rag` 中手动启动 `python -m rag.manage worker run` 并保持运行。

## 手动同步兼容性复核

采纳旧入口兼容性意见：手动 `index sync` 对 disabled/archived 知识库仍返回 `RESOURCE_DISABLED`，拒绝前不领取任务、不创建 generation、不切换 alias；自动同步继续发布空索引。预检查后发生并发停用时，再次校验状态，手动尝试记为 superseded，自动目标保持 pending。

补充停用/归档且有现存索引、停用且缺失索引、预检查后并发停用三项集成测试。更新后的自动同步专项共 44 项：43 项通过，1 项本地 BGE 验收按默认开关跳过。日志 `tmp/index_sync_manual_compatibility.log`。

## 回滚校验与 worker 复核

停用/归档知识库返回空有效语料是设计行为，隐藏当前 build 的异常不阻碍空索引发布；历史 generation 的回滚通过 ESStore 独立验证身份、ready/只读状态、记录数量与内容哈希。本轮真实 ES 篡改测试证明：停用及归档时仍拒绝损坏目标，alias 和暂停状态不变。另验证隐藏当前 build 异常时，自动同步可清空投影，回滚仍校验指定历史 generation。删除有效语料选择末尾不可达的停用分支。

`ProcessingService.run` 保留 processing-only 兼容入口，内部传递原 service 实例到统一调度器；共享任务循环、互斥和错误处理，保留旧的单次返回字段。原互斥、单次执行、故障持久化失败后退出等处理任务测试在本轮全量回归中通过。

## 产物检查

运行产物均位于已忽略的 tmp，没有写入 benchmark release 或复制 dataset_snapshot。clean-artifacts 已执行 dry-run，不删除文件；实施时发现 18 个白名单候选，预计可回收约 1.92 MiB，报告位于 `tmp/index_sync_artifact_cleanup.json`。Git 对象约 2.34 MiB，Codex refs 未新增堆积。兼容性复核后再次 dry-run：20 个候选，约 1.97 MiB，报告位于 `tmp/index_sync_manual_compatibility_cleanup.json`，没有删除文件。

本轮全量复核结束后 dry-run：22 个候选，约 2.07 MiB，报告 `tmp/index_sync_review_cleanup.json`，没有删除文件。
