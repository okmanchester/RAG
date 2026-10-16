# 本机文档管理：轻量 demo 与可选部署

默认使用轻量 demo：Windows、`conda rag`、当前本地模型，MySQL/Elasticsearch 由使用者手动启动。保留完整管理页面、上传、版本、后台任务及检索，不替换数据库或处理管线。页面/API 只监听 `127.0.0.1:8000`，不提供远程访问或鉴权。

## 默认：按需运行 demo

已有环境：先手动启动 MySQL/ES，再在项目根目录运行：

```powershell
conda activate rag
python -m pip install -r requirements.txt -r requirements-api.txt   # 首次
python -m rag.demo
```

入口只检查 MySQL、数据库迁移、Elasticsearch 和 API 端口，再启动 API 与 worker。检查失败即退出，不启动 Docker Desktop、不执行 Compose、不自动升级数据库、不加载模型做预热、不注册自启、不自动备份或轮换文件。任一子进程退出，整组停止并返回非零退出码；没有自动重启和等待依赖恢复的循环。修复依赖后手动重跑即可。

运行后打开 http://127.0.0.1:8000/。页面显示「演示模式」，备份区域显示「演示模式不自动备份」。依赖状态只记录启动检查结果；进程存活状态每 5 秒更新，不代表任务一定正在推进。`python -m rag.ops status` 可只读查看同一份状态。

停止用当前终端 Ctrl+C，或在另一终端运行 `python -m rag.demo --stop`。此停止入口不访问 Windows 计划任务；没有 demo 运行时返回 `not_running`。它会终止 API、worker 及其原生子进程，共享 15 秒退出等待预算。清理失败会返回非零退出码。中断的处理任务仍按原有租约机制在下次运行后恢复。

demo 与可选监督进程共用 `tmp/runtime/supervisor.lock`，避免同时启动两组服务。不要另开 `rag.api` 或 worker。demo 不自动停止手动启动的 MySQL/ES，也不修改现有计划任务；若此前安装过自启，请先显式执行一次 `python -m rag.ops uninstall`，再按 demo 方式运行。

## 手动准备和停止依赖

沿用 `.env.mysql`、`.env.elasticsearch`、本地模型及 `DEEPSEEK_API_KEY`，环境变量优先。首次配置凭据见[知识库管理](knowledge_base_management.md)和[管理检索](managed_search.md)。使用现有 Docker Compose 时，先自己打开 Docker Desktop，再显式运行：

```powershell
docker compose --env-file .env.mysql -f compose.yaml up -d
docker compose --env-file .env.elasticsearch -f compose.elasticsearch.yaml up -d
python -m rag.manage db check
# 仅首次建库或确实需要升级时，显式执行：
# python -m rag.manage db upgrade
python -m rag.demo
```

首次使用空数据库时，显式迁移后通过 `python -m rag.manage kb create --code demo --name 演示知识库` 创建知识库。上传/解析仍需本地 MinerU 模型及配置要求的 DeepSeek 密钥；demo 期间会处理现有排队任务。dense/hybrid 检索按需加载 BGE，BM25 无需查询向量。

演示结束先停止 demo；如需释放容器资源，再手动运行下面两条命令。使用 `stop` 保留数据库、索引卷和全部文档，不使用 `down -v`：

```powershell
docker compose --env-file .env.mysql -f compose.yaml stop
docker compose --env-file .env.elasticsearch -f compose.elasticsearch.yaml stop
```

现有 Compose 的 `restart: unless-stopped` 沿用不变；显式停止后不会随 Docker 引擎恢复而自动启动，下一次演示时再次手动 `up -d`。2026-10-15 的卸载和前台监督记录见[上一轮运行方式记录](local_management_demo_20261015.md)，其中 `supervise` 是历史方案，当前默认入口为 `rag.demo`。本次实现范围与验证边界见[2026-10-16 轻量 demo 记录](local_management_demo_20261016.md)。

## 日常操作

先运行 `python -m rag.demo`，打开 http://127.0.0.1:8000/，选择知识库。上传 PDF/DOCX 时填写相对逻辑路径，例如 `policy/数据安全法.docx`。相同路径表示同一文档，内容变化产生新的原文件版本；相同内容及处理身份复用任务，不自动重试历史失败任务。

操作顺序：上传 → 查询处理任务 → 在文档详情启用 ready build → 等待自动索引同步 → 检索。处理成功不等于启用，也不等于已可检索；空 build 不能启用。启用历史 build 即版本回退。停用后立即过滤检索材料，后台索引随后收敛。

页面每 2 秒轮询当前工作区，后台标签页每 10 秒；通常只查数据库，完成后再核验真实 ES 状态，也可点击“核验实际索引”。任务等待超过 60 秒时展示等待时间和运行状态提示，不把等待推断为失败。暂停同步不会因“重试”自动解除，需通过现有 CLI 恢复。

任务页可查看尝试历史和固定错误码。普通失败使用“重试”；`PROCESSING_IDENTITY_CHANGED` 使用“重新提交”。上传超时、断连时先查文档/任务，确认服务是否已经提交，再决定是否上传。

检索支持默认模式、BM25、dense 和 hybrid。引用展示文件、标题路径、PDF 物理页码或 DOCX 内容位置；DOCX 内容位置不当作页码。展开引用查看完整来源、版本、build 和 SHA256。页面不生成回答、不提供原文件在线预览。

## 可选：注册开机自启（计划任务）

demo 不需要本节。原部署能力保留为可选：`python -m rag.ops supervise` 会自动启动 Docker/Compose、恢复进程并自动备份，不能作为轻量 demo 入口；需要登录自启、每分钟恢复检查或无人值守每日备份时才显式注册以下任务。先停止 demo，两种入口不能同时运行。

```powershell
conda activate rag
python -m pip install -r requirements-api.txt
python -m rag.manage db check
python -m rag.ops install   # 可选：注册当前用户登录启动、周期恢复和每日备份
python -m rag.ops start
python -m rag.ops status
```

安装当前用户的 `RAG-Local-Supervisor` 和 `RAG-Local-Backup` 两个计划任务，采用交互登录身份，无需存储账号密码。Supervisor 登录启动，同时每分钟触发恢复检查，运行中 IgnoreNew；只有一个监督进程持有 OS 文件锁。重装会更新、启用任务，不迁移数据库。

监督进程按需隐藏启动 Docker Desktop，再执行现有 Compose，数据库迁移检查通过后启动独立 API、worker。异常退出的子进程递增延迟重启，最长 60 秒；存活 5 分钟后重置连续失败退避。监督进程本身退出由计划任务周期触发恢复。当前已存在数据库/索引卷不会重建或清空。

```powershell
python -m rag.ops stop       # 先禁用监督计划任务，再终止受管进程树
python -m rag.ops start      # 重新启用并启动监督任务
python -m rag.ops uninstall  # 停止并删除两个计划任务，保留数据、日志和备份；不停止 MySQL/ES 容器
```

停止不会等待当前文档完成；子进程先以 CREATE_SUSPENDED 创建，加入 Windows Job Object 后才恢复执行，避免原生渲染子进程在作业归属建立前逃出。Job Object 在监督进程崩溃时也回收子进程树；等待退出超时会保留句柄以供重试。应用不从状态文件读取 PID 杀进程。中断任务保持 running，原租约到期（最长约 120 秒）且新 worker 领取后，历史尝试记为 interrupted 并重执行；可能重新解析或重复外部请求，不保证恰好执行一次。

收到停止请求后先取消正在运行的 provision 命令及其子进程，不继续等待 120 秒的 Compose 超时；受管进程同时发出终止请求，共享 15 秒等待预算。CLI 最多等 45 秒确认监督锁释放。uninstall 即使遇到停机异常也会执行计划任务移除，并以非零退出码报告停机失败，不假装服务已经停止。

`rag.ops stop` 会持续禁用监督任务，包括后续登录，直到显式 `start` 或重装。独立备份任务不因此禁用；要停用全部调度使用 `uninstall`。任务已卸载时 `start` 报 `RUNTIME_COMMAND_FAILED`，`stop` 容忍任务不存在并继续停机；权限不足或任务服务故障仍明确报错。demo 使用 `rag.demo --stop`，不经过任务计划程序。**任何 launcher 运行时都不要另开第二份 API/worker。**

状态文件在 `tmp/runtime/status.json`，每 5 秒原子更新，15 秒过期。依赖检查每 10 秒运行、每项最多 2 秒，不加载模型。状态信号缺失、过期、损坏或属于别的配置时，只能说无法确认受管进程；手动运行的 worker 不纳入判断。备份元数据独立校验，损坏时只丢弃备份信号；未知 supervisor 字段收敛为 unknown，不覆盖有效进程状态。API 在启动时固定运行身份，不在每次状态轮询时重读配置，修改配置后应重启。

`tmp/runtime/logs/` 下为 API、worker、supervisor 的 JSONL 生命周期日志，每类最多 5 份、每份 10 MiB。只记录时间、任务标识、耗时、退出状态及固定错误码；处理任务按 `kind=processing/kb_id/task_id/attempt` 归属，索引任务按 `kind=index/kb_id/revision/attempt` 归属。受管子进程不保存第三方模型原始 stdout/stderr。模型初始化故障可结合任务错误码和环境检查排查，不自动降低解析档位。

文件日志由 `python -m rag.api`、`python -m rag.manage worker run` 等进程入口显式初始化，库函数调用不自行创建文件。初始化、序列化、写入或轮换失败均不改变业务响应或服务启动结果。测试直接调用库函数不产生默认运行日志；启动真实 CLI 子进程的测试必须传入独立 `RAG_RUNTIME_ROOT`。

初版混有测试事件的历史 worker 日志已隔离到 `tmp/runtime/logs/quarantined/review_20261015/`，不能用作生产运行证据。修复及日志哈希核验见[审查修复记录](local_management_review_20261015.md)。

## 在线备份

demo 不自动备份。以下能力仅由显式备份命令、可选 `supervise` 或已安装的备份计划任务触发。自动模式在北京时间 03:00 后检查漏跑，默认备份到 `D:\RagBackups`，只轮换属于当前配置的最近 7 份成功备份；失败每 5 分钟重试，OS 互斥锁防止并发执行。Windows 每日计划任务使用系统时区，改变时区后需重新核对。

```powershell
python -m rag.ops backup       # 立即执行一次在线备份
python -m rag.ops backup --due # 今天尚未成功且已过 03:00 才执行
```

可通过用户环境变量 `RAG_BACKUP_ROOT` 修改默认目录；`RAG_RUNTIME_ROOT` 可修改状态、日志目录。改变用户环境后需让新登录会话继承变量并重新启动任务。客户端不能通过 HTTP 更改这些路径。

备份不停 API/worker。先完成 InnoDB 事务快照，再枚举并复制已发布原文件和快照，排除发布暂存目录；额外未被数据库引用的文件允许存在。仅允许 `raw/<64hex>`、`snapshots/<64hex>/` 和约定的发布暂存前缀；出现未知存储目录或对象时显式报 `BACKUP_LAYOUT`，新增布局必须同步更新备份代码。校验原文件内容地址、快照声明哈希及完整备份清单后才发布目录。失败保留历史成功备份；不把任意名字或别的配置的目录当作轮换对象。

计划备份和手动备份共用一把互斥锁，在获得锁前不改备份状态。锁竞争返回 busy，保留已有成功或运行状态，不发出 backup_failed。

备份期间可正常上传、处理、启用和检索，但禁止 DDL、文件覆盖、存储删除/GC 和手工破坏性修改。`coordination_enforced=false` 明确这些外部操作尚无共同锁，不能声称已被备份互斥锁约束。将来增加 GC 前必须补共享协调机制。

强制结束备份进程可能留下 `.backup-*` 暂存目录，不算成功备份、不参与轮换。确认没有备份运行后才能人工清理。本机同盘备份仅用于误操作恢复，硬盘故障防护仍需独立介质。

备份包含数据库和不可变本地存储，不包含凭据、模型权重或 ES；ES 可从恢复库重建。旧版备份清单继续支持恢复。恢复只能指定不存在的隔离数据库与新目录：

```powershell
python scripts/backup_knowledge_base.py restore --input D:\RagBackups\<备份名> `
  --database rag_restore_check --storage-root tmp/restore_check
```

隔离恢复演练不影响源服务；恢复真实业务并切换 API/worker 配置时，应先停服、校验、再启动。

## 验证与升级

```powershell
python -m pip install -r requirements-dev.txt  # 仅浏览器验收需要 Playwright
# 使用本机已安装的 Chrome，不下载测试浏览器。
python scripts/accept_local_workflow.py --output tmp/local_acceptance_new

$env:RAG_RUN_MYSQL_TESTS='1'
$env:RAG_RUN_ES_TESTS='1'
$env:RAG_RUN_BGE_TESTS='1'
python -m unittest discover -s tests -v
```

真实验收使用 `rag_accept_*` 临时数据库和独立 ES 命名空间，运行后删除它们；报告、截图和备份演练证据留在 tmp。解析器、切块器和 BGE 都是真实实现；对 DeepSeek 只观察 HTTP 数量，不替换响应。中断恢复验收会提前过期隔离库租约，报告明确标记该故障注入。

数据库迁移仍为 `0004_index_sync`。本次 demo 调整不修改文档处理实现及其配置身份；将来更改处理实现或配置时，升级前停 API/worker，启动后对旧身份的未完成任务重新提交，不用 retry 替代。历史成功 build 保留。
