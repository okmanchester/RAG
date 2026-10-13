# 150题金标准难度演进 v2

状态：开放演进，未重新冻结。全部150题为dev，不设留出或盲测。

政策、技术各75题；基础15、中等45、困难60；拒答、纠错、澄清各10题。
沿用v1原始33份语料及切块快照，语料字节未修改。旧冻结决定已撤销，v1仅作历史归档。
审核方式：Codex离线来源审核，不声称人工审核，旧审批不继承。

只向被测系统提供dev/queries.jsonl；gold.jsonl及review/属于评测侧材料。
修订后必须重新校验并刷新manifest版本指纹，旧评测结果不再适用；不允许静默复用旧结果目录。

真实试跑限pilot_ids.jsonl中的5道困难题，政策3、技术2；名单在运行前确定。
5题结果仅用于诊断，不能外推150题均值；旧v1评分均值不与新v2样本直接比较。

本地运行：
```powershell
python -m rag.benchmark.first_run --release data/benchmark/gold150_hard_v2 --output tmp/gold150_hard_v2_pilot5 --ids-file data/benchmark/gold150_hard_v2/pilot_ids.jsonl --local-only --allow-evolving --metric-version 2
```
