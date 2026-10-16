# HTTP API 契约 v1

实现版本：1.0.0。首版覆盖文档管理、持久化任务状态和检索，默认只监听 `http://127.0.0.1:8000`，不提供鉴权。接口前缀为 `/api/v1`；默认关闭跨域访问，可显式允许指定的本机浏览器 Origin，见下文。

管理页面位于 `/`，静态资源位于 `/assets/`；交互说明位于 `/docs`，机器可读定义位于 `/openapi.json`。页面使用现有 API，无额外前端服务。字段类型、必填性和枚举以运行中同版本 OpenAPI 为准；本文规定调用流程、一致性和错误语义。

## 1. 安装与运行

在项目根目录、`conda rag` 环境运行：

```powershell
# 已有环境可直接安装 API 增量依赖；新环境还需安装解析依赖。
python -m pip install -r requirements.txt -r requirements-api.txt
python -m rag.manage db check

# 默认：MySQL/ES 手动启动，demo 只拉起 API 和 worker；Ctrl+C 停止。
python -m rag.demo

# 可选：拆分运行，不能与 demo 或监督进程同时使用。

# 终端一：HTTP 服务，固定 loopback、8000 端口、单进程。
python -m rag.api

# 终端二：独立常驻 worker，处理文档和自动同步索引。
python -m rag.manage worker run
```

沿用 `.env.mysql`、`.env.elasticsearch` 和 `RAG_STORAGE_ROOT`，环境变量优先于配置文件。DeepSeek 密钥仅由 worker 从 `DEEPSEEK_API_KEY` 读取。处理沿用默认 MinerU 配置、本地模型和 400/50 token 参数；HTTP 客户端不能指定配置文件、模型路径或密钥。

**API 进程的模型前置条件**：上传和显式提交处理会立即运行 `processing_config()`，读取 `config/mineru.yaml`、BGE tokenizer 配置/文件哈希，以及 MinerU 模型目录清单，生成持久化任务身份。因此，API 机器也必须能访问 `D:/WorkSpace/AIModels/BAAI/bge-large-zh-v1.5` 与 `D:/WorkSpace/AIModels/MinerU/models` 等当前配置路径，并与 worker 保持相同的路径、配置、依赖和实现身份。模型/配置缺失时，相应请求返回 `503`，注册与任务不提交。API 不执行 MinerU 推理，但目前不能独立部署成完全不挂载模型的上传节点；管理查询和纯数据库状态查询没有此模型前置条件。dense/hybrid 检索另外需要在 API 进程加载 BGE 权重。

API 启动检查数据库迁移版本，不自动建表或升级；首次部署的迁移步骤见[知识库管理说明](knowledge_base_management.md)。当前不新增数据库迁移，仍为 `0004_index_sync`。ES 和查询模型按需初始化，故障不阻止文档列表和纯数据库任务查询。连接池及检索客户端在进程结束时释放；查询资源在进程内复用，首次初始化和查询编码串行执行。

### 本机运行状态

`GET /api/v1/runtime-status` 只读本机状态文件，不访问 MySQL/ES、不加载模型。返回 `signal`（`unknown/stale/fresh`）、`mode`（`demo/supervised`，信号未知或过期时为 null）、`updated_at`、`supervisor`、`api/worker`、`dependencies` 和 `backup`。`supervisor` 字段兼容表示 launcher 是否存活，不表示一定启用自动恢复。受管进程字段为 `state/restart_count/last_exit_code/next_start_at`；依赖仅返回 `state/checked_at`；备份仅返回状态、起止时间、最近成功时间及固定错误码。demo 不自动备份，`backup` 固定为 null，不混入历史部署模式的备份记录。内部 PID、实例身份、绝对路径和配置不返回。

运行信号每 5 秒更新，超过 15 秒未更新或来自未来时钟为 stale；文件缺失、损坏、身份不匹配为 unknown。未知或过期时进程状态一律 unknown，不能据此判断手动运行的 worker 已停止。身份绑定项目、数据库连接目标及存储根目录。demo 的依赖状态是启动检查快照；可选部署模式每 10 秒探测依赖。`running` 只表示受管进程存活，不证明任务正在推进。API 自身不可用时，请独立运行 `python -m rag.ops status`。详见[本机运行说明](local_management.md)。

备份元数据独立验证，损坏时只返回 `backup=null`；未知 supervisor 值收敛为 `unknown`，不使有效 api/worker 信号降级。默认 API 在启动时固定运行身份，修改数据库/存储配置后需重启。日志目录不可写不改变业务错误 JSON 或 API 启动结果。

`RAG_API_MAX_UPLOAD_BYTES` 默认 `104857600`（100 MiB），要求为正整数；仅文档上传路由的 multipart 请求使用此值加 `1048576` 字节作为总请求体上限，预留 multipart 开销，文件本身仍受 100 MiB 限制。其它请求使用独立的 `RAG_API_MAX_JSON_BYTES`，默认 `65536`（64 KiB），同样要求为正整数。两类限制既检查 Content-Length，也累计实际接收字节。上传暂存及 multipart 磁盘 spool 位于 `tmp/`，请求完成或失败后清理；发布原文件进入现有不可变存储。

浏览器页面可与 API 同源托管或通过开发代理访问；Vite 等开发服务器也可以直接跨源调用。直接调用时，启动 API 前显式配置精确的 Origin 白名单：

```powershell
$env:RAG_API_BROWSER_ORIGINS='http://localhost:5173,http://127.0.0.1:5173'
python -m rag.api
```

只允许 `http/https` 的 `localhost`、`127.0.0.1` 或 `[::1]` Origin，按 scheme、主机和端口区分；不能使用 `*`、`null`、外部域名或带路径的 URL。允许跨域方法为 GET/POST/PUT/PATCH，请求头为 Content-Type，暴露 Location 响应头，不启用 cookies/credentials。浏览器来源不匹配会在业务执行前返回 `403`；设置白名单前请确认对应端口上的页面是可信的。同源开发代理若保留前端 Origin，也需把该 Origin 加入白名单。

**本机浏览器访问边界**：所有请求先校验 Host，仅接受上述 loopback 主机，非本机域名和无效/重复 Host 返回 `400 INVALID_HOST`，避免将 DNS 重绑定域名当成本机 API。存在 Origin 时，必须与请求同源或命中显式白名单；`null`、无效/重复 Origin 和其它来源返回 `403 ORIGIN_NOT_ALLOWED`，因此 CORS 简单 multipart 请求也不能在响应被浏览器拦截之前偷偷创建任务。无 Origin 且 Fetch Metadata 标为跨源的浏览器请求同样拒绝；无浏览器来源头的 CLI/服务调用保持可用。CORS 本身只控制浏览器响应访问，不承担这些副作用拦截职责，参见 [Starlette 中间件说明](https://github.com/kludex/starlette/blob/main/docs/middleware.md)。这些检查不是用户身份认证；能在本机运行并自行构造请求的程序仍可访问，不支持据此直接开放到内网或公网。

API 依赖保留 `starlette==1.7.0`：这是与 FastAPI 组合验证的版本，上传体积限制中断请求流时，依赖其 multipart 解析器对异常路径关闭临时文件的行为。FastAPI 的宽泛下界不能固定这一行为；升级时需同时复验上传中断和清理，而非单独移除锁定。

`GET /health` 返回 `200 {"status":"ok"}`，只表示进程能响应，不表示数据库、ES、模型或 worker 健康。启动命令默认关闭 HTTP access log；应用故障日志只记录固定错误码。

## 2. 公共约定

- 请求与响应使用 UTF-8；上传使用 multipart，其余有请求体的操作使用 `application/json`，也接受 `application/*+json`。
- JSON 请求拒绝未知字段。提交处理和重试接口允许不带请求体，或传空对象 `{}` / `null`。
- 数据库 ID 使用 JSON 正整数，范围 `1..18446744073709551615`；generation、SHA256 和来源 ID 使用字符串。客户端需保持整数精度。
- 时间统一为 UTC ISO 8601，例如 `2026-10-15T01:02:03.000000Z`；可空时间明确返回 `null`。
- 成功直接返回资源或结果对象，无额外 `data` 包装。列表采用 `{total, limit, offset, items}`，默认 `limit=100, offset=0`，要求 `1 <= limit <= 1000, offset >= 0`。
- 列表按 ID 升序；build 按 build ID 升序；处理尝试按 ordinal 升序；索引尝试按 ordinal 降序。分页查询不固定跨请求快照。
- 错误统一为 `{"error_code":"固定错误码","error":"固定提示"}`，不返回 traceback、原始校验输入、SQL、凭据或底层异常正文。
- HTTP DTO 不公开处理配置、执行 token、租约、存储绝对路径、完整文档/build JSON 或索引内部 manifest。

## 3. 文档管理

以下路径均相对于 `/api/v1`。

| 方法与路径 | 输入 | 成功响应 |
|---|---|---|
| `GET /knowledge-bases` | 可选 `status`；分页 | `200 Page<KnowledgeBase>` |
| `GET /knowledge-bases/{kb_id}/documents` | 可选 `status`；分页 | `200 Page<Document>` |
| `POST /knowledge-bases/{kb_id}/documents` | multipart，见下文 | `202` 或 `200 UploadResult` |
| `GET /documents/{document_id}` | 无 | `200 DocumentDetail` |
| `GET /documents/{document_id}/versions` | 分页 | `200 Page<Version>` |
| `GET /documents/{document_id}/builds` | 分页 | `200 Page<Build>` |
| `PUT /documents/{document_id}/active-build` | `{"build_id":5}` | `200 ActivationResult` |
| `PATCH /documents/{document_id}/status` | `{"status":"active"}` 或 `disabled` | `200 Document` 加 `sync_target` |

列表的 `status` 支持 `active / disabled / archived`；省略时返回全部状态。首版不提供知识库创建、归档、删除和原文件下载 API。

### 上传及身份

multipart 必须且只能包含一个 `file` 文件和一个 `source_path` 字段。`source_path` 为知识库内的相对逻辑路径，例如 `policy/数据安全法.docx`，最大 1024 字符；扩展名只接受 PDF、DOCX，不区分扩展名大小写。文件不允许为空。实际解析合法性由 worker 检查，上传接口不执行解析。

逻辑路径统一 Unicode NFC、正斜杠，消除 `.` 和重复分隔符，保留路径大小写；拒绝绝对路径、盘符、`..`、NUL 和目录越界。multipart 文件名不参与文档身份，不作为存储路径。

- 同一知识库、同一路径、同一内容哈希：复用原文件版本。
- 同一路径、新内容哈希：新建原文件版本，保持文档 ID。
- 路径变化：视为新文档，即使内容相同。
- 原文件版本与当前处理配置指纹：唯一确定处理任务。任务已失败时不会因重复上传而自动重试。

知识库和已有文档必须 active。文件发布后，文档/版本注册和任务创建在同一数据库事务中提交；数据库失败时一并回滚。文件存储与数据库没有跨系统事务，允许留下未引用的不可变文件。

待处理或运行中的任务返回 `202`；复用 succeeded/failed 任务返回 `200`。两者均返回 `Location: /api/v1/processing-tasks/{task_id}`，响应字段如下：

```json
{
  "document_id": 1,
  "version_id": 2,
  "source_sha256": "原文件64位SHA256",
  "active_build_id": null,
  "task_id": 3,
  "status": "pending",
  "build_id": null,
  "reused_version": false,
  "reused_task": false
}
```

`active_build_id` 是当前启用结果；`build_id` 是本次任务结果，未完成时通常为空。上传和处理成功均不自动启用。

### 管理响应字段

| 类型 | 字段 |
|---|---|
| `KnowledgeBase` | `id, code, name, description, status, created_at, updated_at` |
| `Document` | `id, knowledge_base_id, source_path, title, category, status, active_build_id, created_at, updated_at` |
| `DocumentDetail` | Document 字段，加 `active_chunk_count, active_eligible_chunk_count` |
| `Version` | `id, document_id, source_sha256, original_filename, format, file_size, created_at` |
| `Build` | `id, document_version_id, legacy_doc_id, output_fingerprint, status, created_at, chunk_count, is_active, can_activate` |

原文件版本是不可变输入；build 是某次处理产生的不可变结果。build 状态为 `ready / failed / empty`；只有归属正确、ready、非空且切块完整的 build 可启用。`is_active` 表示是否为文档当前指针，`can_activate` 描述 build 的可用性；文档启停是独立状态。

启用支持回退历史 build，不修改历史结果，也不自动将 disabled 文档改为 active。启用结果为：

```json
{
  "document_id": 1,
  "previous_build_id": null,
  "active_build_id": 5,
  "sync_target": {
    "kb_id": 1,
    "desired_revision": 2,
    "status": "pending",
    "paused": false
  }
}
```

启用和状态变化与索引目标在同一事务中提交；响应不等待 embedding 或索引发布。重复设置同一 build、分类或状态不重复递增修订号。停用保留版本及当前指针，后续查询按 MySQL 状态过滤；重新启用文档仍使用保留的当前 build。

## 4. 任务状态与重试

| 方法与路径 | 输入 | 成功响应 |
|---|---|---|
| `POST /versions/{version_id}/processing-tasks` | 无配置参数 | `202` 或 `200 TaskSubmission` |
| `GET /knowledge-bases/{kb_id}/processing-tasks` | 可选 `status`；分页 | `200 Page<ProcessingTask>` |
| `GET /processing-tasks/{task_id}` | 尝试记录分页 | `200 ProcessingDetail` |
| `POST /processing-tasks/{task_id}/retry` | 无 | `202 {task_id,status}` |
| `GET /knowledge-bases/{kb_id}/index-status` | `db_only=true` 默认；尝试记录分页 | `200 IndexStatus` |
| `POST /knowledge-bases/{kb_id}/index-sync/retry` | 无 | `202 {kb_id,sync_task}` |

### 文档处理

TaskSubmission 返回 `task_id, status, build_id, reused`，状态码及 Location 与上传相同。该入口用于上传之外显式提交，以及处理配置或实现变化后重新提交；使用服务器当前配置。

处理状态为 `pending / running / succeeded / failed`，阶段为 `validation / parsing / chunking / publishing / persisting`。阶段只描述最近持久化的处理位置，不表示百分比。pending 可能正在等待自动重试，可结合 `scheduled_at` 判断。

ProcessingTask 返回 `id, document_version_id, status, stage, build_id, attempt_count, cycle_attempts, scheduled_at, finished_at, created_at, updated_at, error_code, error`。

ProcessingDetail 另返回 `build_status, can_activate, attempts`。未生成 build 时，前两项为 `null, false`；空文档可以处理 succeeded，但产生 empty build，不能启用。`attempts` 是分页对象，记录字段为 `id, task_id, ordinal, status, stage, started_at, finished_at, error_code, error`，尝试状态还支持 `interrupted`。

### 索引同步

索引任务按知识库合并，不为每次启用创建独立索引任务 ID。

IndexStatus 返回 `kb_id, sync_task, attempts, index_checked, sync_lag, generation, state, chunk_count`。

- 默认 `db_only=true`：只查 MySQL，不连接 ES、不加载模型；`index_checked=false`，顶层 `sync_lag/generation/state/chunk_count` 均为 `null`。
- `db_only=false`：读取实际 ES alias 与元数据，比较其语料指纹与当前 MySQL 选择，返回 `index_checked=true` 和实际 `sync_lag`。无 alias 时返回 `state="missing", generation=null, chunk_count=0, sync_lag=true`。ES 故障返回 `503`，不会静默降级为数据库模式。
- `sync_task.generation` 是数据库最后确认同步的一代；与顶层实际 alias 的 generation 含义不同。
- `sync_lag=false` 只描述观察时的语料选择一致性，不保证 worker 正在运行，也不替代查询模型身份校验。

SyncTask 字段为 `knowledge_base_id, desired_revision, applied_revision, status, paused, scheduled_at, attempt_count, cycle_attempts, generation, error_code, error, finished_at, created_at, updated_at`。`desired_revision` 为目标，`applied_revision` 为最后确认完成的修订号；不能只凭修订号推导实际 alias 一致性。暂停是独立标志，重试不解除暂停。

索引 `attempts` 的字段为 `id, knowledge_base_id, ordinal, target_revision, status, manual, generation, started_at, finished_at, error_code, error`，尝试状态支持 `running / succeeded / failed / interrupted / superseded / deferred`。

两类任务均仅允许对 failed 状态人工重试，其它状态返回 `409`；人工重试重置本周期失败预算，保留累计尝试和历史结果。瞬时故障沿用最多三次执行、间隔 30/120 秒的自动重试规则。详情见[处理任务说明](processing_tasks.md)和[自动索引同步](auto_index_sync.md)。

## 5. 检索及来源引用

`POST /knowledge-bases/{kb_id}/search`：

```json
{
  "query": "数据安全风险监测有哪些要求？",
  "top_k": 10,
  "mode": "hybrid",
  "category": "policy",
  "structure_type": "body"
}
```

只有 `query` 必填且不得全为空白；`top_k` 为整数，默认 10，范围 1–50。可选 `mode` 为 `dense / bm25 / hybrid`，省略或 null 使用当前 generation 的默认模式；筛选条件精确匹配，非空白字符串。接口不生成 LLM 回答。

成功返回 `200`，包括 `kb_id, generation, mode, sync_lag, contexts`。示例：

```json
{
  "kb_id": 1,
  "generation": "0123456789abcdef0123456789abcdef",
  "mode": "hybrid",
  "sync_lag": false,
  "contexts": [{
    "id": "mysql:4",
    "mysql_chunk_id": 4,
    "document_id": 1,
    "version_id": 2,
    "build_id": 5,
    "legacy_chunk_id": "上游切块ID",
    "doc_id": "上游文档ID",
    "document_sha256": "原文件64位SHA256",
    "source_path": "policy/数据安全法.pdf",
    "title": "数据安全法",
    "heading_path": ["第二章", "风险监测"],
    "text": "可直接作为检索上下文的切块文本",
    "score": 0.03278688524590164,
    "ranks": {"dense": 1, "bm25": 1},
    "structure_type": "body",
    "sources": [{
      "block_id": 1,
      "page": 2,
      "block_char_start": 0,
      "block_char_end": 16
    }],
    "derived_sources": [],
    "asset_refs": []
  }]
}
```

`sources` 延续现有统一来源视图，合并物理正文、上下文和派生映射引用；`derived_sources` 保留完整派生操作及 source_refs。来源对象允许不同结构的附加字段，例如 body 区间、表格行列或 DOCX 位置，未记录的信息不补造。PDF 的 `page` 从 1 开始；DOCX 的 `content_index` 是内容位置，不是物理页码；字符区间为左闭右开。`asset_refs` 是来源资产元数据，本轮没有资产下载接口。

`score` 取决于检索模式，不能跨模式直接比较；hybrid 使用现有 RRF，`ranks` 保存各路排名。没有结果时 `contexts=[]`，不以空结果代替基础设施故障。

查询固定一代索引，候选按 MySQL 当前有效 build 过滤，正文和来源从 MySQL 还原，返回前再次检查当前有效状态。切换 build 或停用后，后续查询不会因旧 alias 仍存在而返回被替换材料。新 build 尚未进入索引时，可以返回不足或空结果并带 `sync_lag=true`；不会回退到 release。并发一致性以查询最后一次 MySQL 检查为观察点，不承诺撤回随后发生变更前已经返回的数据。

知识库停用/归档返回 `409 RESOURCE_DISABLED`；缺少索引返回 `503 INDEX_NOT_FOUND`；MySQL/ES 不可用明确返回 `503`；查询模型与索引身份不匹配返回 `409 INDEX_MODEL_MISMATCH`。

## 6. HTTP 状态与错误码

| HTTP | error_code | 含义 |
|---|---|---|
| 400 | `INVALID_JSON` | JSON 语法错误 |
| 400 | `INVALID_REQUEST` | multipart 损坏、part 数量超限或无效 Content-Length 等 |
| 400 | `INVALID_HOST / INVALID_CORS_PREFLIGHT` | 非本机或无效 Host；CORS 预检请求不满足允许的方法/请求头 |
| 403 | `ORIGIN_NOT_ALLOWED` | 浏览器来源未允许或跨源请求缺少可信 Origin；业务操作尚未执行 |
| 404 | `RECORD_NOT_FOUND` | 资源不存在 |
| 404 / 405 | `NOT_FOUND` / `METHOD_NOT_ALLOWED` | 路由不存在或方法不支持 |
| 409 | `RESOURCE_DISABLED` | 知识库或文档不允许当前操作 |
| 409 | `BUILD_OWNERSHIP / BUILD_NOT_READY / BUILD_INCOMPLETE / NO_ACTIVE_BUILD` | build 归属或准备状态冲突 |
| 409 | `TASK_RETRY_NOT_ALLOWED / INDEX_SYNC_NOT_FAILED` | 当前状态不能人工重试 |
| 409 | `PROCESSING_IDENTITY_CHANGED / INDEX_MODEL_MISMATCH` | 处理或检索身份不兼容 |
| 409 | `INDEX_BUSY / WORKER_BUSY / KB_CODE_EXISTS` | 受控资源冲突；保留既有服务错误映射 |
| 413 | `UPLOAD_TOO_LARGE` | 文件或请求体超限 |
| 415 | `UNSUPPORTED_MEDIA_TYPE / INVALID_DOCUMENT_FORMAT` | 请求内容类型或逻辑路径扩展名不支持 |
| 422 | `INVALID_REQUEST / EMPTY_UPLOAD` | 字段、枚举、范围、未知 JSON 字段、multipart 字段组合或空文件错误 |
| 422 | `INVALID_PATH / UNSAFE_PATH / PATH_TOO_LONG / PATH_ESCAPE` | 逻辑路径无效 |
| 422 | 其它 `INVALID_*` | 服务层参数校验错误；下列服务器配置错误除外 |
| 503 | `DATABASE_ERROR` | 数据库操作失败 |
| 503 | `SEARCH_UNAVAILABLE / SEARCH_ERROR` | ES 连接或请求失败 |
| 503 | `INDEX_NOT_FOUND / INDEX_NOT_READY / INDEX_CORRUPT` | 检索投影缺失或不可用 |
| 503 | `PROCESSING_MODELS_MISSING / PROCESSING_KEY_REQUIRED / PROCESSING_TEMPORARY` | 本地模型、密钥或执行依赖不可用 |
| 503 | `INVALID_PROCESSING_CONFIG / INVALID_SEARCH_CONFIG / ELASTIC_PASSWORD_REQUIRED / SCHEMA_OUTDATED` | 服务器配置或数据库版本不可用 |
| 500 | `FILE_SYSTEM_ERROR / INTERNAL_ERROR` 等 | 存储或未分类内部错误，提示经过脱敏 |

任务查询中的 `error_code/error` 描述持久化任务结果；查询本身成功时 HTTP 仍为 `200`。客户端应按错误码和任务状态决策，不依赖英文提示文本，也不要对所有错误无限重试。

## 7. 完整调用流程

下面使用 PowerShell；`curl.exe` 避免与旧版 PowerShell 的别名混淆。知识库需预先通过 CLI 创建或导入。

```powershell
$base = 'http://127.0.0.1:8000/api/v1'
$kbId = 1 # 也可从 GET /knowledge-bases 选择实际 ID。

# 上传只排队，处理过程由独立 worker 执行。
$uploaded = curl.exe -sS -X POST "$base/knowledge-bases/$kbId/documents" `
  -F 'source_path=policy/sample.pdf' -F 'file=@data/raw/policy/sample.pdf' | ConvertFrom-Json

# 每两秒轮询示例：真实客户端应处理非 2xx 和自己的等待超时。
do {
  $task = Invoke-RestMethod "$base/processing-tasks/$($uploaded.task_id)"
  if ($task.status -in @('pending', 'running')) { Start-Sleep -Seconds 2 }
} while ($task.status -in @('pending', 'running'))
if ($task.status -ne 'succeeded' -or -not $task.can_activate) {
  throw '检查任务失败原因或 empty build；修复后再重试/重新提交。'
}

# 显式启用 build：此响应仍不表示新内容已经可以检索。
$activated = Invoke-RestMethod -Method Put "$base/documents/$($uploaded.document_id)/active-build" `
  -ContentType 'application/json' -Body (@{build_id=$task.build_id} | ConvertTo-Json)

# 显式读取 ES 发布状态；默认数据库模式的 sync_lag 是 null。
do {
  $index = Invoke-RestMethod "$base/knowledge-bases/$kbId/index-status?db_only=false"
  if ($index.sync_task.status -eq 'failed') { throw '检查索引失败原因，修复后调用 index-sync/retry。' }
  if ($index.sync_task.paused) { throw '索引同步已暂停，使用现有 CLI 恢复。' }
  if ($index.sync_lag) { Start-Sleep -Seconds 2 }
} while ($index.sync_lag)

$queryBody = @{query='数据安全风险监测'; top_k=5} | ConvertTo-Json
$result = Invoke-RestMethod -Method Post "$base/knowledge-bases/$kbId/search" `
  -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes($queryBody))
$result.contexts

# 停用保留历史，但后续查询按当前状态过滤。
Invoke-RestMethod -Method Patch "$base/documents/$($uploaded.document_id)/status" `
  -ContentType 'application/json' -Body '{"status":"disabled"}'

# 仅在对应任务 failed 且原因修复后使用：
# Invoke-RestMethod -Method Post "$base/processing-tasks/<task_id>/retry"
# Invoke-RestMethod -Method Post "$base/knowledge-bases/$kbId/index-sync/retry"
```

**本轮升级已触发处理身份变化**：HTTP API 新增注册/提交事务组合，并调整了 `management/processing.py` 和 `management/service.py`；这两份源码都被纳入 `implementation_sha256`。本轮即使没有数据库迁移，也会使升级前提交的旧处理任务身份与新实现不一致。成功任务和历史 build 保留；旧 pending/running 任务在新进程执行、恢复或重执行时会报告 `PROCESSING_IDENTITY_CHANGED`。升级前停止 API/worker，避免新旧实现混跑；重启后对这些版本调用 `POST /versions/{version_id}/processing-tasks` 按当前配置生成新任务，不改写旧任务身份，也不要用旧任务的 retry 代替重新提交。

## 8. 验证

```powershell
python -m unittest discover -s tests -p test_http_api.py -v

# 专用测试库和 ES namespace 自动创建、清理；不修改现有业务库和索引。
$env:RAG_RUN_MYSQL_TESTS='1'
$env:RAG_RUN_ES_TESTS='1'
python -m unittest discover -s tests -p test_http_api_integration.py -v
Remove-Item Env:RAG_RUN_MYSQL_TESTS
Remove-Item Env:RAG_RUN_ES_TESTS
```

审查修正后验收：25 项 HTTP 契约/边界测试、10 项真实 MySQL/ES HTTP 流程测试和 17 项原管理模块真实 MySQL 测试通过。完整常规回归发现 314 项，207 项通过、107 项按条件跳过；其中已包含上述 25 项 HTTP 契约测试。新增场景覆盖 Host/Origin 拒绝、可选本机 CORS 及错误响应、JSON 独立上限、API 模型缺失时的原子失败。HTTP 集成使用测试解析器和 embedding，未调用真实 LLM、未运行全量 benchmark。它证明 HTTP 编排、事务和状态过滤，不声明真实解析或检索质量提升。
