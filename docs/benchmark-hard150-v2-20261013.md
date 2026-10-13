# 150题难度演进 v2：2026-10-13

已完成150题开放演进，全部为dev，未重新冻结。旧版150题和33份语料切块快照保持原字节。
政策、技术各75题；基础15、中等45、困难60；拒答、纠错、澄清各10题。135道题面改写，15道基础题面保留。
证据跨度：单单元50、同文档67、跨文档23、无答案证据10。边界题的跨度与难度分开统计。
每题有旧ID、修订前后指纹、来源祖先及本轮审核记录。作者与审核者均为Codex，未声称人工复审。

只试跑预选5道困难题，政策3、技术2。BGE纯向量Top-10、DeepSeek-flash、原提示词和输出预算；千问评委，评分协议v2。未连接Langfuse。

| 原始评委指标 | 5题诊断值 |
|---|---:|
| 全部证据@5 | 60.00% |
| 全部证据@10 | 80.00% |
| 已尝试要点准确率 | 97.46% |
| 要点完整率 | 91.44% |
| 漏答率 | 6.44% |
| 整题要点全对率 | 60.00% |
| 引用支持率 | 93.33% |
| 引用覆盖率 | 95.78% |
| 忠实度 | 100.00% |

**这些是未校准的评委原始分数。** 逐项复核记录了5处语义判定争议，主要涉及并列规则已覆盖但派生要点被判漏答，以及一个统计口径的概括是否充分。
原判定与分数全部保留，争议另行记录；整题全对率不能直接当作已证实的真实全对率。本轮不构成人工校准，不将5题外推150题，也不与旧全量v1均值比较。

运行前按场景固定名单，未按得分选题。初次沙箱网络连接失败，核验出站内容仅为公开法律与技术指南后重试成功。
审核发现2道题的题面未明确覆盖部分金标准要求，已补清题面、更新版本并作废旧结果；最终重跑受影响2题，其余3题经完整评分输入及判定核验后显式迁移到新结果目录。
一次评委引用关联错误按原v2协议恢复，失败记录保留。没有将协议校验失败计作系统答错。

交付位置：

- [题集说明](../data/benchmark/gold150_hard_v2/README.md)
- [逐题题面、参考答案及原文证据](../data/benchmark/gold150_hard_v2/review/reading.md)
- [逐题变更](../data/benchmark/gold150_hard_v2/changes.jsonl)
- [5题完整报告](../data/benchmark/gold150_hard_v2/pilot/report.json)
- [逐要点核对](../data/benchmark/gold150_hard_v2/pilot/point_review.jsonl)
- [评委争议](../data/benchmark/gold150_hard_v2/pilot/judge_disagreements.json)
- [验收记录](../data/benchmark/gold150_hard_v2/acceptance.json)

验证：最终完整回归113项通过，其中7项覆盖演进集加载、哈希异常、查询一致性、本地恢复和评分缓存失效。

核心题集与语料由manifest校验；pilot/有独立产物哈希，避免将结果写回题集指纹形成循环。
当前版本的实际运行位于tmp/gold150_hard_v2_pilot5_final，旧尝试仅为历史诊断，不能混入本轮结果。

复现时使用新结果目录：
```powershell
conda activate rag
python -m rag.benchmark.first_run --release data/benchmark/gold150_hard_v2 --output tmp/gold150_hard_v2_pilot5_next --ids-file data/benchmark/gold150_hard_v2/pilot_ids.jsonl --allow-evolving --local-only --metric-version 2
python -m rag.benchmark verify-release --release data/benchmark/gold150_hard_v2 --allow-evolving
```

金标准SHA-256：3bd942966a5d563d41b8b5dd274015eeb980652fb5ce04edff91090864769011
版本指纹：f81054e42c7dc2fbc94ec85f9955c6a93277c2caca3876ee7b24a88e5396a480
