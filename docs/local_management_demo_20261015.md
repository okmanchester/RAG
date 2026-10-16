# 本机卸载监督计划任务并改为演示模式：2026-10-15

> 历史记录提示（2026-10-16）：本文记录的 `supervise` 前台方式已被 `python -m rag.demo` 取代；下文保留 2026-10-15 当时的操作和验证结果。`Disable` 分支的错误处理也已进一步修正，详见[轻量 demo 实现与验收记录](local_management_demo_20261016.md)；当前操作方式以[本机运行说明](local_management.md)为准。

本机已卸载 `RAG-Local-Supervisor` 与 `RAG-Local-Backup` 两个计划任务，改为按需在前台运行 `python -m rag.ops supervise`。没有改动数据库、模型配置、备份数据或文档数据，迁移版本仍为 `0004_index_sync`。唯一的代码改动是 `scripts/runtime_tasks.ps1` 的 `Disable` 分支（见下）。运行方式与行为边界见[本机运行说明](local_management.md)。

## 与既有记录的关系

[验收记录](local_management_acceptance_20261015.md)第 16 行的「最终保留已启用的两个计划任务」是 2026-10-15 验收当时的机器状态。该文档的回归、真实文档链路、页面行为和备份结论不因本次卸载变化；本文只更新「当前部署状态」这一项，不重跑完整回归、不重跑 MinerU/DeepSeek，也不声称监督质量有任何变化。历史文档均未改写。

## 卸载

```powershell
conda activate rag
python -m rag.ops uninstall    # 内部顺序：Disable 计划任务 → 停止受管进程 → Uninstall 移除计划任务
```

输出 `{"status": "uninstalled"}`，退出码 0。卸载前运行中的受管进程：

| 进程 | PID | 命令行 |
|---|---|---|
| supervisor | 9896 | `D:\miniconda3\envs\rag\pythonw.exe -m rag.ops supervise` |
| API | 12008 | `D:\miniconda3\envs\rag\python.exe -m rag.api` |
| worker | 25024 | `D:\miniconda3\envs\rag\python.exe -m rag.manage worker run` |

## 代码改动：`Disable` 分支容忍任务不存在

`rag/ops/__main__.py:155` 的 `stop` 会无条件先禁用 `RAG-Local-Supervisor` 计划任务，而 `scripts/runtime_tasks.ps1` 的 `Disable` 分支原本没有容忍「任务不存在」，配合该脚本第 6 行的 `$ErrorActionPreference='Stop'`，`Disable-ScheduledTask` 抛 `CimException` 并在到达 `exit 0` 之前终止脚本。`task()` 于是在 `rag/ops/__main__.py:102-103` 抛 `RUNTIME_COMMAND_FAILED`，用户只会看到 "runtime command failed; inspect runtime status and logs"，被引向错误方向。

卸载计划任务后这正是最常见的动作，因此给该分支加上与同文件 `Uninstall` 分支 `Unregister-ScheduledTask` 一致的 `-ErrorAction SilentlyContinue`。复现新旧语义：

```powershell
# 旧分支：exit 1（未到达 exit 0）
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; Disable-ScheduledTask -TaskName 'RAG-Local-Supervisor' | Out-Null; exit 0"
# 新分支：exit 0
powershell -NoProfile -Command "$ErrorActionPreference='Stop'; Disable-ScheduledTask -TaskName 'RAG-Local-Supervisor' -ErrorAction SilentlyContinue | Out-Null; exit 0"
```

实测：旧分支 `exit=1`，新分支 `exit=0`。`Start` 分支未改动：没有任务可启用时应明确报错，改用 `install`。

`tests/` 对 `runtime_tasks.ps1` 零覆盖（计划任务注册在测试中全程被 mock），因此本次不为一处 ps1 参数新增解析式测试。

## 验证

### 卸载后

| 检查 | 命令 | 实际 |
|---|---|---|
| 计划任务已移除 | `Get-ScheduledTask -TaskName 'RAG-Local-*' -ErrorAction SilentlyContinue` | 无输出 |
| 受管进程已回收 | `Get-CimInstance Win32_Process` 过滤 `rag.ops\|rag.api\|rag.manage` | 空集 |
| 状态信号（15 秒内） | `python -m rag.ops status` | `fresh` + `supervisor=stopped` + api/worker `stopped`（`finally` 写入的终态快照） |
| 状态信号（15 秒后） | `python -m rag.ops status` | `stale` + `supervisor=unknown`，`dependencies` 清空为 `{}`、`backup` 为 `null` |
| 容器未被停止 | `docker ps` | `rag-mysql-1`、`rag-search-elasticsearch-1` 均 `Up (healthy)` |
| `stop` 已可用 | 未安装状态下 `python -m rag.ops stop` | `{"status": "not_running"}`，退出码 0（修复前为 `RUNTIME_COMMAND_FAILED`） |

`stale` 时状态文件不提供依赖信息，确认容器是否存活应使用 `docker ps`。

### 演示前台路径

前台运行 `python -m rag.ops supervise`，另一侧观察：

| 检查 | 实际 |
|---|---|
| `python -m rag.ops status` | `fresh` + `supervisor=running`，api/worker `running`，docker/mysql/elasticsearch 均 `available` |
| `curl http://127.0.0.1:8000/health` | `{"status":"ok"}` |

容器此前已在运行，因此从启动到就绪约 20 秒内完成；冷启动时主要等待 Docker Desktop 自身启动和 `compose up -d`（单条超时上限 120 秒）。

随后停止与锁释放：

| 检查 | 实际 |
|---|---|
| `python -m rag.ops stop` | `{"status": "stopped"}`，退出码 0 |
| 受管进程 | 空集；前台 `supervise` 进程以退出码 0 结束 |
| 锁已释放 | 立即重新运行 `python -m rag.ops supervise` 正常进入循环（`supervisor=running`），未报 `RUNTIME_BUSY`；再次 `stop` 同样返回 `stopped` |
| 容器 | 两次 `stop` 后 `docker ps` 仍是两行 `Up`——停止不涉及容器 |

`/health` 只表示进程存活，不代表 MySQL、ES 或模型可用；完整信号看 `/api/v1/runtime-status` 或 `rag.ops status`。

## 未执行、需要真实登录才能确认的部分

本轮**没有注销或重启 Windows**。「不再随登录自启」的依据是 `RAG-Local-Supervisor` 与 `RAG-Local-Backup` 已从任务计划库移除；「Docker Desktop 不再被拉起」的依据是本次登录链条（开机 21:39:02 → supervisor 21:39:13 → `Docker Desktop.exe` 21:39:24，父进程为 supervisor）已随任务移除而消失，再加上 `HKCU\Software\Microsoft\Windows\CurrentVersion\Run` 中该启动项在 `Explorer\StartupApproved\Run` 里记录为禁用（首字节 `03`）。该启动项是 2026 年 8 月安装 Docker Desktop 时留下的残留，与 RAG 的计划任务无关，本次未改动它。

二者都需要一次真实登录才能直接观察。重启后应核对：任务库中无 `RAG-Local-*`、任务管理器无 `pythonw.exe -m rag.ops supervise`、托盘无自动启动的 Docker Desktop。

## 未改变的部分

业务数据库、`D:\RagBackups` 下的备份（`rag-20261015T042458Z-62937e5d`、`rag-20261015T065814Z-9941c31b`）、`tmp/runtime/logs/` 日志、benchmark release 与 `.gitattributes` 均未改动。截至本文，本机没有运行中的监督进程，前台页面与 API 未启动，MySQL 与 ES 容器仍在运行。
