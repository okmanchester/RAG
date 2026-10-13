# benchmark评分修复记录

原15道持续评分失败题全部恢复；最终150/150题全部适用指标完成。

详见 [最终全量报告](../data/benchmark/gold150_hard_v2/full_runs/20261013_004/README.md)、[核验记录](../data/benchmark/gold150_hard_v2/full_runs/20261013_004/verification.json)、[语义分歧](../data/benchmark/gold150_hard_v2/full_runs/20261013_004/semantic_disagreements.json)。


- 修复句号后引用绑定；不同原子事实可共用回答句子，重复同一事实仍校验。
- 评委选择片段ID，程序生成原文、引用号及context_id；不删除空格、标点或数字来放宽支持。
- 按要点、边界、引用分别校验、分别出分；失败指标保留缺失及错误，不填0或1。
- 增加实际引用范围、精确候选提示及引用位置评估覆盖检查；重复事实在不同引用位置分别评估。
- 缓存绑定题集、语料、回答、schema、rubric及源码身份；显式复用模型选择时重新定位原文并重算指标，保留来源清单。完整复用可离线执行，无需模型客户端。
- 记录逐请求题目ID、结束原因、用量及重试。

## 运行与来源

题目schema v2及指标v2保持不变。评委子协议为 `source_fragments_v1`，具体版本由schema、rubric、修复提示及源码哈希区分；默认 `--judge-protocol fragments`，旧协议可显式选择 `verbatim`。

未重新生成回答：150条回答与Top-10检索材料逐字沿用 `20261013_001`；BGE纯向量Top-10、DeepSeek-flash生成配置保持原运行身份。本次DeepSeek调用0次，Langfuse未访问，题库仍evolving。千问配置未变，已有120题忠实度经过身份校验复用。

先真实验证全部原15道失败题，再全量重评150条保存回答。`20261013_002`与`003`为诊断过程；最终在本目录统一重算，合法内容指标保留，只修复无效引用组。模型选择来源与重算步骤均在 `execution.json`、`evaluation/manifest.json`、逐题provenance及历史中记录。

金标准SHA256：`3bd942966a5d563d41b8b5dd274015eeb980652fb5ce04edff91090864769011`。

题集版本SHA256：`f81054e42c7dc2fbc94ec85f9955c6a93277c2caca3876ee7b24a88e5396a480`。

## 最终指标

按题求均值；“有效/适用”是实际评分分母。

| 指标 | 均值 | 有效/适用 |
| --- | ---: | ---: |
| 全部证据命中率 Top-10 | 72.50% | 120/120 |
| 全部证据命中率 Top-5 | 55.83% | 120/120 |
| 证据组召回率 Top-10 | 83.32% | 120/120 |
| 证据组召回率 Top-5 | 69.33% | 120/120 |
| 已尝试要点准确率 | 95.78% | 120/120 |
| 要点完整率 | 85.81% | 120/120 |
| 漏答率 | 11.12% | 120/120 |
| 整题要点全对率 | 59.17% | 120/120 |
| 引用支持率 | 96.32% | 120/120 |
| 引用覆盖率 | 95.55% | 120/120 |
| 忠实度 | 94.62% | 120/120 |
| 边界行为正确率 | 100.00% | 30/30 |

源码/判定协议及评分覆盖发生变化，这些分数不能解释成RAG质量提升，也不与旧v1或不同分母均值直接比较。基于这份全部开放的开发集，未做盲测或人工校准。

## 验证与文件

- 78项评分、身份、哈希异常、独立指标、引用覆盖、缓存重算及断点恢复回归检查通过，日志见 `tests.log`。
- `verification.json`：150题来源、片段位置、身份、指标和分组重算校验；642个引用位置覆盖；旧原始运行380个文件哈希未变。
- `report.json`、`evaluation/scores.jsonl`、`evaluation/items/`：全部指标、逐题判定与错误记录。
- `runs.jsonl`、`items/`：原150题实际回答、检索材料、用量与耗时。
- `evaluation/fragments/`、`fragment_bindings`：片段原文与NFC/LF Unicode字符坐标，可从原始文本按声明的规范化方式重建。
- `validation15_source_audit.jsonl`、`semantic_disagreements.json`：15题离线核对及实际发现的语义分歧。
- `usage_summary.json`、`request_provenance.jsonl`：修复过程真实用量、逐请求题目及结束原因；复用不会伪记成新模型调用。
- `dataset_snapshot/`、`implementation_snapshot/`、`artifacts_sha256.json`：本次输入与实现快照、文件SHA256清单。

本次未保留一次性题库修改脚本。
