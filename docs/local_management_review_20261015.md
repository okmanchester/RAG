# 本机运行保障审查修复：2026-10-15

采纳六项问题及其余建议，并已部署到现有本机服务。没有新增数据库迁移或鉴权；默认监听范围保持 loopback。

## 修复

| 审查项 | 最终行为 |
|---|---|
| 日志初始化/写入抛异常 | `start_logging` 和 `event` 均兜底普通异常；磁盘不可写、序列化、处理器写入或轮换失败不会改变 API 错误 JSON，也不阻止 API 启动。库函数调用不隐式打开日志文件，只有进程入口初始化文件日志。 |
| stop/uninstall 等待不足 | 停止时取消正在运行的 provision 命令树，阻止继续执行后续 Compose 命令；受管进程同时终止，共享 15 秒退出等待。CLI 等待上限为 45 秒。uninstall 即使 stop 报错也执行任务移除，并保留非零退出码。 |
| 任务事件归属缺失 | 处理任务在领取事务内解析实际 kb_id，事件复用领取身份；处理事件记录 kind/kb_id/task_id/attempt，索引事件记录 kind/kb_id/revision/attempt，不虚构索引 task_id。CLI 任务输出同步修复。 |
| 备份 JSON 类型错误 | 备份信号单独解析及 DTO 校验；数组、字符串、数字、null 或无效字段只使 backup=null，不丢弃有效进程信号。 |
| supervisor 未收敛 | 未知值归为 unknown，api/worker 的有效存活状态保留。未知状态响应统一由 RuntimeStatus 默认值生成。 |
| Windows 导入污染 | Windows ctypes 导入和结构定义放入平台守卫；监督模块不在导入期加载 Windows 子进程实现。跨平台状态/备份测试仍可收集。 |
| 重复计算身份 | 默认 API 生命周期从已加载 Settings 固定 expected_identity，状态请求不重读配置；无跨配置的全局缓存。 |
| 重复重建运行概况 DOM | 仅在实际展示的信号、进程/依赖状态或备份摘要变化时重建，不因每轮 heartbeat 时间变化重建。 |
| 两层备份锁误报失败 | 计划与手动备份共用同一把锁，在写入任何状态前取得；锁竞争返回 busy，不覆盖旧状态、不记录 backup_failed。 |
| 静默跳过未知存储布局 | 只排除明确约定的发布暂存项；未知根目录、对象名、类型或符号链接显式报 BACKUP_LAYOUT，新增布局必须同步修改备份支持。 |
| 子进程先运行后加入作业 | CREATE_SUSPENDED → AssignProcessToJobObject → ResumeThread；恢复执行前已归属作业。[Windows 挂起创建说明](https://learn.microsoft.com/en-us/windows/win32/procthread/suspending-thread-execution)。stop 等待超时保留 job 句柄，允许再次终止/等待。 |

## 运行日志真实性

原实现确实会把普通单测的合成 worker 事件写入默认运行日志。这是实现缺陷，不能把这些行作为真实业务任务证据。

修复后，库内 event 没有显式配置的 sink 时直接返回；真实 CLI 子进程测试显式传入独立 RAG_RUNTIME_ROOT。不是通过识别测试进程名称来关闭日志。生产入口仍初始化独立角色日志，文件日志故障只损失遥测。

单独执行 worker 调度单测、以及执行整轮 338 项回归前后，原运行中 worker 日志 SHA256 均保持一致。整轮证据为 `tmp/ops_review_log_isolation.json`：

```text
before = A77B21E38C72B3B6D130D199367E5F839535D0E42F566CE7EFF281A0B2039B70
after  = A77B21E38C72B3B6D130D199367E5F839535D0E42F566CE7EFF281A0B2039B70
```

部署时先确认业务库无未完成处理任务，再停止旧服务，将旧文件原样移入 `tmp/runtime/logs/quarantined/review_20261015/worker.jsonl`；manifest 记录上述哈希、20,191 字节及不可信原因。没有删除旧事件，也没有把旧记录重新分类为生产事件。新 worker 从新日志文件开始；部署后的空闲运行没有任务事件，符合实际无任务的状态。

## 验证

| 验证 | 结果 |
|---|---|
| 完整回归 | **338 项全部通过，无失败、错误或跳过，287.533 秒**；包含真实 MySQL、ES 和本地 BGE；`tmp/ops_review_full_tests.log` |
| 最终审查专项 | **13 项全部通过，6.099 秒**；`tmp/ops_review_targeted_tests.log`，与完整回归不相加 |
| HTTP 契约 | 26 项通过，覆盖生命周期身份固定、启动失败资源释放及错误边界；完整回归亦覆盖 |
| 长命令取消 | 实际运行 120 秒占位子进程，取消后 5 秒内返回；实际监督进程在 provision 期间收到 stop，12 秒内退出并释放锁，没有等到命令超时 |
| Windows 作业生命周期 | 原生式后代回收、等待超时后保留句柄并重试通过；部署后的真实 API/worker 使用新创建流程正常运行 |
| 页面回归 | 5 项浏览器检查通过，0 页面异常；`tmp/ops_review_browser/report.json` |
| 正式在线备份 | `D:\RagBackups\rag-20261015T065814Z-9941c31b` 成功；使用统一锁和严格布局校验，没有停止服务 |

13 项专项包括日志初始化失败仍可启动 API、日志写入抛异常仍返回 DATABASE_ERROR JSON、元数据隔离、字面量收敛、固定身份、无隐式日志、卸载失败清理、备份锁冲突、未知布局拒绝、非 Windows 导入、命令取消和作业句柄重试。

非 Windows 导入使用隔离解释器模拟平台并检查未导入 ctypes.wintypes；本轮没有运行真实 Linux 全量测试。没有重新执行 MinerU 文档解析或 DeepSeek 请求，不把历史真实文档验收称为本轮模型重跑。

## 数据与产物

业务库部署前没有未完成处理任务；实现身份变化仍要求旧身份任务重新提交。现有文档/build 和 benchmark release 未改写。

按 clean-artifacts 技能执行 dry-run，24 个候选、2,290,447 字节，删除 0 个文件；`tmp/ops_review_cleanup.json`。本轮新增的一个 Codex checkpoint ref 按 AGENTS.md 清理并执行 git gc，capture 和业务分支保留。
