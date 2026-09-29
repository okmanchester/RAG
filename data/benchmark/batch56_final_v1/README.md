# 56题已审核批次金标准

状态：已冻结。gold.jsonl为本版本唯一金标准；review/reading.md及review/review.csv为审核归档。
审核方式见review_provenance和approval_provenance；用户授权冻结不冒称用户逐题复审。
冻结授权原文：把这可用的56题冻结为金标准，和之前已冻结的30题一起纳入版本控制

全部56题为公开dev，pilot标志记录已公开使用，不能作为holdout。本批次不应用300题正式集配额，也不代表正式集已完成。
previous_frozen_releases记录旧金标准及其哈希；本版不覆盖旧版。

只将queries.jsonl交给被测系统。复制run_template.jsonl到发布目录外填写真实结果，not_run不表示系统已运行。评测时验证全部发布哈希。

review_provenance保存冻结前56题和指定审核资料；历史修订日志可能含已排除题，实际评测仅使用gold.jsonl。

在conda rag环境、仓库根目录运行：

```powershell
python -m rag_baseline.benchmark evaluate --release data/benchmark/batch56_final_v1 --runs tmp/actual_runs.jsonl --output tmp/batch56_final_v1_retrieval_eval --retrieval-only
```

小样本切片仅供诊断；完整评测需评委配置及校准。纠错时发布新版本，禁止覆盖此目录。
