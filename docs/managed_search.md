# MySQL 与 Elasticsearch 检索

MySQL 保存权威文档、不可变 build、切块和完整来源。ES 是可从 MySQL 重建的检索投影。现有 release/NumPy 评测入口继续保留；面向当前知识库的查询使用 `rag.manage search`。

## 部署

在 `conda rag` 中安装检索依赖，MySQL 按原管理说明启动并迁移：

```powershell
python -m pip install -r requirements-search.txt
python -m rag.manage db check
if (-not (Test-Path .env.elasticsearch)) { Copy-Item .env.elasticsearch.example .env.elasticsearch }
# 新环境填写独立的 ELASTIC_PASSWORD，不能保留示例密码。
docker compose --env-file .env.elasticsearch -f compose.elasticsearch.yaml up -d --wait
```

ES 使用独立 Compose 项目 `rag-search`，不改变现有 MySQL 项目。镜像固定为官方 Docker Hub 的 Elasticsearch 9.5.5 及摘要，官方 Python 客户端为 9.5.1。单节点 Basic、每代索引一个主分片和零副本、JVM heap 1 GiB、容器上限 3 GiB，端口仅绑定 `127.0.0.1:9200`。启用认证，HTTP 明文限于本机；远程连接必须 HTTPS，可配置 `ELASTICSEARCH_CA_CERTS`，不关闭证书验证。

本次实施已生成被忽略的 `.env.elasticsearch` 随机凭据，已有文件不会被覆盖。凭据不放入 URL、日志或 Git。环境变量优先于配置文件；自定义端口时同时修改 `ELASTICSEARCH_PORT` 和 `ELASTICSEARCH_URL`。已有 ES 卷上的密码变更需同步更新 ES 用户，重建容器不会自动更改账号密码。不要使用 `down -v` 进行日常重启。

## 同步与查询

```powershell
python -m rag.manage index sync --kb-id 1
python -m rag.manage index status --kb-id 1
python -m rag.manage search --kb-id 1 --query "政策有哪些条件？"
python -m rag.manage search --kb-id 1 --query "指标更新频率" --mode hybrid --category technical
# generation 从 index status 的 generations 中选择。
python -m rag.manage index rollback --kb-id 1 --generation <32位generation>
```

全局 `--env-file` 和 `--es-env-file` 放在子命令之前。支持 `--top-k`（1–50）、`--category`、`--structure-type`，返回 UTF-8 JSON；失败返回固定错误码和非零退出码，不回显底层异常文本。

有效语料必须满足知识库、文档均 active，采用归属正确、ready 且切块完整的 `active_build_id`，仅索引 `retrieval_enabled=true` 的块。在 REPEATABLE READ 事务内取得 build 清单，之后分页读取不可变切块；模型推理期间不持有文档行锁。不复制 release 或 `dataset_snapshot/`。

每次同步构建独立 generation。ES `_id` 是 MySQL chunk 主键，索引包含知识库、文档、build、legacy 身份、文本、类型、payload 哈希和向量。来源 payload 留在 MySQL。索引命名包含数据库连接身份摘要，避免不同数据库恰好具有相同主键时共用 alias。

逐项检查 bulk 结果，刷新后核对数量及每条检索记录的身份、正文和哈希；只有完成核验并封为只读的 generation 才能发布。alias 是实际发布状态的权威记录。同步失败保留现有 alias，失败候选记录固定错误码；进程中断可能留下 building 候选，不能回滚到它。alias 请求响应中断时先读取实际 alias 来确认是否已发布。

同一知识库的同步和索引回滚通过 MySQL 命名锁互斥。旧代保留供回滚；首期不自动删除任何 generation。manifest 记录 build 清单、语料/模型/检索指纹、数量及构建时间。

## 状态与一致性

查询先读取 MySQL 当前有效 build，作为两路 ES 检索的共同过滤条件；两路固定读取同一个物理 generation。候选正文及引用来源从 MySQL 还原，返回前重新检查当前状态和 build。引用上下文使用 `mysql:<chunk主键>`，同时保留 `legacy_chunk_id/doc_id`、原文件 SHA256、物理/上下文/派生来源和资产引用。

文档或知识库停用、切换到另一 build 后，后续查询不会返回被替换的旧材料。新 build 在显式同步完成后可检索；等待期间结果可不足或为空，`sync_lag=true` 表示索引 build 清单与当前 MySQL 选择不同。构建期间发生管理变更也按这一规则处理，不将完成的旧快照声称为最新内容。

索引回滚只改变 alias，不改变 MySQL 当前 build；回滚索引若没有当前 build，查询仍为空并报告滞后。恢复某个历史 build 后，如果当前索引中已有该 build，无需重新 embedding 即可检索。并发管理修改以查询中最后一次数据库检查为状态观察点，不承诺返回后发生的修改能撤回已经返回的材料。

MySQL 或 ES 不可用时明确失败；不自动读取 release 或绕过当前状态。向量查询还校验查询模型指纹与索引模型一致。`index status` 可检查停用知识库；`search` 拒绝停用知识库。

## 检索与离线对照

初始默认是 `dense`：本地 BGE 1024 维归一化 float32，沿用原查询指令，ES 使用 `dense_vector index=false` 与精确 cosine `script_score`。这一步用于迁移一致性验证；规模扩大后再根据延迟实测决定是否引入 ANN。

`bm25` 使用内置 cjk 分析器检索原 `text`；`hybrid` 两路各取前 50 条，以等权 RRF、常数 60 融合，按 MySQL chunk 去重。结果保留两路排名，不直接相加不可比的 BM25/cosine 分数。融合在应用侧完成，不依赖 ES 原生 RRF。

```powershell
python scripts/evaluate_managed_retrieval.py --release data/benchmark/gold150_hard_v2 `
  --kb-id 1 --output tmp/es_retrieval_comparison --allow-evolving --promote-best
```

对照前校验 MySQL 与 release 的全部可检索 chunk payload、来源哈希、当前索引/模型和 generation；不允许重复 legacy ID 被静默覆盖。共享一次查询 embedding，对照 NumPy、ES dense、BM25、hybrid 的四项 Top-5/10 指标、政策/技术分组、逐题变化和延迟。延迟分开记录共享 embedding；ES 检索耗时包含 MySQL 状态校验，不等同于 ES 服务端耗时。

`--promote-best` 在 dense、BM25、hybrid 三个候选中选择默认，使用选择策略 v2。四项总体指标和政策/技术分组的八项指标都必须完整、合法且分母一致；获选模式的12项指标均不低于其它候选及 NumPy 安全基线，且相对**当前默认模式**至少一项总体指标严格改善。指标存在权衡或证据不足时保留当前默认；当前默认已并列最优时也保留。其它模式并列最优且严格优于当前默认时，按 BM25、dense、hybrid 的顺序选择，优先避免 embedding，其次采用单路检索。

`--promote-hybrid` 保留为 `--promote-best` 的旧 CLI 别名，候选仍包括三种模式。晋升前重新取得命名锁，核验报告内容、推荐结果、generation、语料/模型/检索指纹和当前默认值；评测期间已有默认值变化也会阻止过期晋升。记录决定报告的 SHA256 和选择策略版本。可显式 `--mode dense` 等继续对照；新同步的 generation 初始为 dense，需单独通过门槛。评测结束前再次校验 generation、选择指纹和 release 原始字节。

该工具只做检索和本地评分，不调用 LLM 或 Langfuse。当前150题是开发集，不将对照结果表述为盲测或回答质量提升。原始输出只能放在已忽略的 tmp 或 full_runs，已有输出目录拒绝覆盖。

## 验证与后续

```powershell
python -m unittest discover -s tests -v
# opt-in：自动创建、清理隔离 MySQL 测试库和明确名称的 ES 测试索引。
$env:RAG_RUN_MYSQL_TESTS="1"
$env:RAG_RUN_ES_TESTS="1"
python -m unittest discover -s tests -p test_search_integration.py -v
Remove-Item Env:RAG_RUN_MYSQL_TESTS
Remove-Item Env:RAG_RUN_ES_TESTS
```

覆盖目录排除、跨知识库身份隔离、文档/知识库启停、build 切换、索引回滚、重复同步、缺失/空索引、部分失败、构建期间变更、返回前状态检查、命名锁互斥和 alias 响应中断。每轮验证后执行 clean-artifacts dry-run。

本轮不新增数据库表或迁移，不实现后台自动同步、原文件处理任务、HTTP API、管理页面或物理删除。后续按原文件注册与解析切块任务、持久化重试、自动同步、HTTP API/页面的顺序推进。MySQL 与本地原始文件的备份恢复仍采用既有管理流程；ES 索引可重建，数据库或存储恢复后须重新确认模型并显式同步。

## 实施验收：2026-10-14

- MySQL 8.4.11、迁移 `0002_import_run_items`；当前知识库 33 个有效 build、1,308 个可检索块。ES 9.5.5 健康运行，镜像摘要与 Compose 固定值一致。
- 两代正式索引完成验证和发布，当前 generation 为 `ba519cfbbabb45f09b111ce446fba1df`。同步无滞后，上一代保留；最初按旧门槛启用 hybrid，审核修正后的默认模式见下文。
- 完整回归 183 项通过（包含原 17 项 MySQL 集成和当时 11 项 ES 集成），随后补充真实 bulk 部分失败场景，最终 12 项 ES 集成全部通过。测试库和测试索引已清理。
- 150 题逐题对照完成，ES dense 与 NumPy Top-10 排序逐题一致：150/150。四项指标均有 120/120 个适用题完成评分；其余 30 题为边界行为题，本轮不生成回答或评分边界行为。

| 指标 | NumPy / ES dense | ES BM25 | ES hybrid |
|---|---:|---:|---:|
| 全部证据命中率 Top-5 | 55.83% | 72.50% | 70.00% |
| 全部证据命中率 Top-10 | 72.50% | 85.83% | 83.33% |
| 证据组召回率 Top-5 | 69.33% | 85.33% | 82.25% |
| 证据组召回率 Top-10 | 83.32% | 92.08% | 89.57% |

Hybrid 的政策、技术分组四项指标均优于 NumPy 基线，最初按旧门槛成为本代默认。**该门槛只比较旧基线，会选中已知次优候选，已接受审核并修正为策略 v2。** BM25 在本开发集的全部12项总体/分组指标均不低于其它候选，且四项总体指标均优于当前 hybrid；重新校验已有150题结果后，当前代默认改为 BM25。等权融合尚未优于词法分支，后续排序优化应保留这条对照。

本机检索 median / p95：NumPy 1.33 / 1.76 ms，ES dense 38.95 / 47.37 ms，BM25 36.42 / 47.16 ms，hybrid 49.20 / 60.78 ms；共享查询 embedding 另计，为 51.55 / 272.05 ms。ES 数字包含 MySQL 校验，不代表生产并发性能。

本轮 LLM 调用为 0；release 全部受检查文件字节校验通过。原始报告、逐题结果、来源及实现指纹、产物哈希位于 `tmp/es_retrieval_150_20261014/`，真实测试日志位于 `tmp/es_all_tests.log` 和 `tmp/es_integration_tests.log`。完整对照为开发集诊断，不证明盲测或回答质量。

产物均通过 Git ignore 检查；评测前后 Codex refs 数量及 Git 对象体积未增长。`clean-artifacts` dry-run 报告为 `tmp/es_clean_artifacts.json`，无实际文件删除。

审核修正验收：默认选择策略 v2 的全部检索相关测试 36 项通过，其中真实 MySQL/ES 集成 13 项。覆盖三种模式获选、BM25 超过已改善旧基线的 hybrid、总体/分组权衡、并列、分母不一致、缺失或非法评分、当前默认并列最优、过期推荐及报告/索引身份不匹配。

`tmp/es_mode_selection_20261014/` 保存修正后的决定、原报告路径及 SHA256、源码指纹和执行验收。对原始四路150题结果重新核对全部上下文的 MySQL/release 来源、重算指标，并校验原产物哈希、检索实现、generation、模型和语料身份。BM25 是唯一通过 v2 门槛的候选，当前实际默认已验证为 BM25；本轮新增 embedding/LLM 推理调用均为 0，原评测报告保留历史语义。
