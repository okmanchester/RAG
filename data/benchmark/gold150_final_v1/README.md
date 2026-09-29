# 150题金标准 v1

状态：已冻结。完整gold.jsonl包含150题，政策/技术各75题。
dev/为120题调优集（政策/技术各60题）；holdout/为30题留出集（各15题）。
只将对应queries.jsonl交给被测系统；运行模板默认not_run，不表示系统已运行。
子目录是同一发布的划分文件，不是独立release；评测--release始终指向本目录。

冻结授权：PLEASE IMPLEMENT THIS PLAN: 冻结150题金标准 v1，划分120题调优集与30题留出集；全池重新划分，比例平衡，新增64题尚未用于调参。

138题沿用正式审核，另外12题由用户授权接纳已有逐题agent审核；未声称独立人工复审。
原始输入、审批接纳及来源资料在review_provenance/，归档索引记录原路径及SHA-256。
review/是新版本审核归档；历史原始对象保持原样。

固定种子20260929，全池重新分层，共享证据块不跨集合，困难/澄清单例留在dev。
本版留出集有20道旧公开题，详情见manifest及逐题freeze_provenance；这是从本次冻结起停止参与调参的留出集，不声称从未见过的盲测集。新增64题此前未用于调参。
日常调优只使用dev；关键版本固定配置后才评holdout，不根据其逐题失败迭代参数。

在conda rag环境中，从仓库根目录运行：

```powershell
python -m rag_baseline.benchmark evaluate --release data/benchmark/gold150_final_v1 --runs tmp/dev_runs.jsonl --output tmp/dev_eval
python -m rag_baseline.benchmark evaluate --release data/benchmark/gold150_final_v1 --runs tmp/holdout_runs.jsonl --output tmp/holdout_eval --split holdout
```

所有发布文件由manifest校验哈希；任何修订必须新发版本，禁止覆盖本目录。
