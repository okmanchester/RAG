本地开发、测试环境：conda rag
本地模型位置：
  Embedding: D:/WorkSpace/AIModels/BAAI/bge-large-zh-v1.5
  MinerU: D:/WorkSpace/AIModels/MinerU/models
LLM：DeepSeek-flash
apikey：本地环境变量读取 DEEPSEEK_API_KEY

## 产物与版本管理约定

改动或运行 benchmark 前必读，避免运行产物污染版本管理：

- 运行产物只落两处：`tmp/`，或 `data/benchmark/**/full_runs/`（均已在 `.gitignore` 忽略）。**绝不写进被跟踪的 release 目录**（`gold150_hard_v2/` 顶层、`pilot/`）；`pilot/` 只留 `report.json`、`point_review.jsonl`、`judge_disagreements.json` 三份结论。
- 新产出一类目录后，先确认 `git check-ignore -v <path>` 命中；未命中就先补 `.gitignore`（按目录名加 `data/benchmark/**/<name>/` glob，不要逐条列绝对路径）。
- **`.gitignore` 不是最后一道防线**：Codex CLI 每轮把整个工作区做成 tree 快照，写到 `refs/codex/turn-diffs/checkpoints/*`——**被 ignore 的产物照样进 `.git`**。跑大批量评测前后用 `git for-each-ref refs/codex` 检查，发现堆积就清 ref 并 `git gc --prune=now`。
- **数据文件行尾必须锁死**：本机 `core.autocrlf=true`，若不锁行尾，入库的是 LF 而 manifest 记的是 CRLF，clone 到别的机器上 manifest SHA 校验必然失败。`data/benchmark/**` 下的数据/金标准文件一律由 `.gitattributes` 的 `-text -whitespace` 保护，新增 release 时确认规则覆盖到。
- **入库的只有「人可读的结论」**：`docs/*.md`、题库核心文件。原始产物（`items/`、`scores.jsonl`、`fragments/`、`dataset_snapshot/`、`outbound_payloads_*.jsonl`）不入库。
- 不要在 run 目录里复制 release 文件（`dataset_snapshot/` 就是冗余副本）；改为在 `execution.json` / `artifacts_sha256.json` 里记录 release 路径 + SHA256。
- 临时目录用 `with tempfile.TemporaryDirectory(...)`，不要裸 `mkdtemp(dir="tmp")` 后不清理。
- 每轮评测结束跑 `/clean-artifacts` 清理可重建产物（默认 dry-run）。