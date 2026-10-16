# 轻量 demo 实现与验收记录：2026-10-16

默认运行入口由前台 `python -m rag.ops supervise` 调整为 `python -m rag.demo`。按使用者确认的范围，保留完整管理页面，由使用者手动启动 MySQL/Elasticsearch。本文记录代码改动及本轮验证结果，不表示已在本机启动真实 demo 或重新完成真实文档处理验收。

当前操作方式见[本机运行说明](local_management.md)，上一轮卸载计划任务的事实见[2026-10-15 记录](local_management_demo_20261015.md)。旧记录中的 `supervise` 前台方式及 `Disable` 错误处理属于当时实现，已由本次改动接续。

## 入口与功能范围

手动准备好 MySQL/ES 和已有配置后，在项目根目录运行：

```powershell
conda activate rag
python -m rag.demo
```

打开 http://127.0.0.1:8000/。当前终端 Ctrl+C 停止，或在另一终端执行：

```powershell
conda activate rag
python -m rag.demo --stop
```

| 项目 | 本次行为 |
|---|---|
| 管理与检索 | 保留上传、文档版本、处理任务、启用/回退、索引同步及检索页面，沿用原处理管线 |
| 启动检查 | 只读检查 MySQL、迁移状态、ES 和 API 端口；失败直接退出 |
| 受管进程 | 启动 API 与 worker；任一子进程退出即停止整组并返回非零退出码 |
| 停止与互斥 | 复用 Windows Job Object 及 `supervisor.lock`，回收受管子进程树；demo 与监督入口互斥 |
| 依赖与数据准备 | 使用者手动启动/停止 MySQL、ES；首次迁移和创建知识库显式执行 |
| 自动化边界 | demo 不启动 Docker/Compose、不注册计划任务、不迁移数据库、不预热模型、不自动备份、不自动重启 |

原 `rag.ops` 监督部署能力仍保留为可选入口。demo 不修改已有计划任务；已有自启的机器需要显式卸载。demo 停止也不停止 MySQL/ES。Compose 的 `restart: unless-stopped` 配置保持原样，演示后需要释放资源时应手动停止容器，具体命令见当前运行说明。

## 状态契约与页面

- `RuntimeStatus` 新增可空 `mode` 字段，取值为 `demo`、`supervised` 或 `null`；有效旧监督信号兼容识别为 `supervised`，无法确认运行身份时为 `null`。
- demo 运行状态每 5 秒更新，依赖状态记录启动时检查结果；进程存活不能证明任务持续推进。
- 管理页面显示「演示模式」，备份区域显示「演示模式不自动备份」。demo 状态不读取历史备份信号，避免将旧备份展示为本次演示结果。
- 契约细节见 [HTTP API 契约](http_api_contract.md)，实现涉及 `rag/demo.py`、`rag/ops/state.py`、`rag/api/schemas.py` 和 `rag/api/web/app.js`。

## 计划任务脚本修正

上一轮 `scripts/runtime_tasks.ps1` 的 `Disable` 分支使用 `-ErrorAction SilentlyContinue`，会同时吞掉权限不足等真实错误。本次改为先以 `-ErrorAction Stop` 枚举并匹配根目录下的监督任务：成功查询且任务不存在时才视为无需禁用；任务存在则执行禁用，权限或任务服务故障继续报错。

`rag.demo --stop` 直接使用停止协议，不经过计划任务操作。因此，轻量 demo 的停止不依赖任务计划程序。

## 验证结果与边界

以下为本轮实现阶段已完成的验证；补写本文时未重新启动服务或重复执行测试。

| 验证项 | 结果与范围 |
|---|---|
| 回归测试 | conda `rag` 中运行 `test_demo`、`test_ops_review`、`test_runtime`、`test_http_api`，共 61 项通过，其中新增 demo 测试 13 项 |
| 生命周期 | 覆盖启动检查失败、部分启动失败、子进程退出、Ctrl+C、停止信号、互斥与清理失败；多数分支使用模拟依赖 |
| 跨进程停止 | 以隔离 Python 辅助进程替代 API/worker，实际调用 `python -m rag.demo --stop`，验证两进程回收及锁释放；未启动真实业务服务 |
| 计划任务错误处理 | 在真实 PowerShell 进程中替换相关 cmdlet，覆盖任务不存在、存在、权限不足和任务服务故障；未修改真实计划任务 |
| 状态与页面 | 验证 demo/监督状态兼容、历史备份隔离；Node VM 执行实际页面渲染函数检查两种模式，未进行完整浏览器验收 |
| 静态与入口检查 | Python 语法、PowerShell 解析、JavaScript 语法及 `git diff --check` 通过；`rag.demo --help` 和无人运行时 `--stop` 通过，后者返回 `not_running` |
| 本机只读核对 | 验收结束时未发现 RAG 计划任务及 `rag.demo/ops/api/manage` 入口进程；这是当时快照 |
| 产物检查 | 已运行 clean-artifacts dry-run，仅报告可重建产物，未删除文件 |

本轮未启动真实 MySQL/ES 业务链路、MinerU、BGE 或 DeepSeek，未执行真实上传、解析、索引和检索的完整验收，也未执行 Windows 注销/重启验证。此前的[真实文档验收记录](local_management_acceptance_20261015.md)保留为历史证据，不等同于新入口已通过真实文档验收。

本轮没有升级业务数据库、改写业务数据、重新配置模型或密钥，也没有重新安装计划任务。数据库、索引卷及既有备份沿用原配置；旧记录中的容器运行状态不作为今天的运行状态结论。
