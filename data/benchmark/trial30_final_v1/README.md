# 30题试审集定稿

状态：已定稿。gold.jsonl 为唯一金标准；review/reading.md 与 review/review.csv 是已填写的审核归档。
审核方式见每题 reviewer 和 approval_provenance；受用户委托的 agent 审核不表述为用户逐题亲自审核。

全部30题属于dev，不能进入holdout。本版本是独立试审集，不代表300题正式配额已完成。
边界题仅含abstain与clarify，不报告correct_premise能力；小样本切片仅供诊断。

将queries.jsonl交给被测系统。不要向被测系统提供gold.jsonl、参考答案或证据。
复制run_template.jsonl到发布目录外，填入真实回答、实际检索上下文、引用及运行配置，成功后将error改为null。
模板中的not_run表示尚未运行，不是模型评测结果。未提供sources时，只按同一文档哈希下的证据文本匹配。

在conda rag环境、仓库根目录运行（将tmp/actual_runs.jsonl替换为真实运行结果）：

```powershell
python -m rag_baseline.benchmark evaluate --release data/benchmark/trial30_final_v1 --runs tmp/actual_runs.jsonl --output tmp/trial30_retrieval_eval --retrieval-only
python -m rag_baseline.benchmark evaluate --release data/benchmark/trial30_final_v1 --runs tmp/actual_runs.jsonl --output tmp/trial30_full_eval
```

完整评测需配置DASHSCOPE_API_KEY并完成人工/委托校准；数据定稿不表示评委已校准或RAG系统已通过评测。
发布文件均有哈希校验；纠错时发布新版本，不覆盖本目录。
