# 首次 RAG benchmark 冒烟

本入口默认只运行固定的 6 道 dev 题：政策、技术、跨文档和边界题都有覆盖。被测链路只读取 `dev/queries.jsonl` 和冻结的可检索 chunks；金标准仅供独立评测。索引使用本地 BGE，回答使用 DeepSeek-flash，评委使用现有千问配置和 Ragas 0.4.3 `Faithfulness`。

```powershell
conda activate rag
python -m pip install -r requirements-benchmark.txt
python -m rag_baseline.benchmark.first_run --output tmp/benchmark_smoke_001
```

从环境变量读取 `DEEPSEEK_API_KEY`、`DASHSCOPE_API_KEY`；从被 Git 忽略的 `.env` 读取 `LANGFUSE_BASE_URL`、`LANGFUSE_PUBLIC_KEY`、`LANGFUSE_SECRET_KEY`（已有环境变量优先）。运行前进行 Langfuse 认证和发布哈希校验。若原冻结目录不可读，程序从 Git 中按原字节复制 11 个核心文件到 `tmp/benchmark_release/gold150_final_v1` 并校验，不修改原目录。

输出目录中，`selected_ids.jsonl` 是本轮题目，`runs.jsonl` 是符合现有 benchmark 接口的真实检索与回答，`evaluation/scores.jsonl` 和 `evaluation/report.json` 是逐题与汇总评分，`trace_map.json` 关联本地题目与 Langfuse trace，`score_sync.json` 记录指标同步情况；`remote_verification.json` 是从 Langfuse 回读的 trace、分数及隐私检查结果。`items/` 支持恢复：相同运行身份跳过已成功题，失败题重试。索引缓存在 `tmp/benchmark_cache/`，按语料和模型指纹区分。

Langfuse 实验任务仅传题目 ID，返回 ID 与运行状态；trace 记录配置、检索数量、耗时、token 用量和数值分数。题目、检索原文、提示词、回答及评委理由只保存在本地。分数通过有写入确认的 Scores API 提交，使用固定 ID，重复同步不会创建另一组指标。远端校验使用 Observations API v2 与 Scores API v3；若分数仍在建立查询索引，`remote_verification.json` 标为 `pending_score_index`，稍后重跑 `--verify-only`。

本次评委尚未完成人工校准，报告标记为 `uncalibrated_diagnostic`，仅用于链路诊断。`calibration_template.jsonl` 是后续人工标注入口，不能把其中草稿当成人工标签。

需要单独重验已上传的 Langfuse 数据时：

```powershell
python -m rag_baseline.benchmark.first_run --output tmp/benchmark_smoke_001 --verify-only
```

若本地评测已完成而分数尚未同步，运行 `--sync-scores`；它读取已保存的逐题分数并复用原 trace ID。

完整 dev120 在完成冒烟后可用新输出目录运行；首次冒烟不执行这一步，也不读取 holdout 查询：

```powershell
python -m rag_baseline.benchmark.first_run --output tmp/benchmark_dev_001 --full-dev
```

评测子集也可独立重跑：

```powershell
python -m rag_baseline.benchmark evaluate --release tmp/benchmark_release/gold150_final_v1 --runs tmp/benchmark_smoke_001/runs.jsonl --output tmp/benchmark_smoke_001/evaluation --ids-file tmp/benchmark_smoke_001/selected_ids.jsonl
```
