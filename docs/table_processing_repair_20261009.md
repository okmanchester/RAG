# 表格逻辑视图、语义切块与回归结果

日期：2026-10-09。环境：conda `rag`；MinerU 4.0.10／docvortex 0.5.11；本地 BGE-large-zh-v1.5。复用 DeepSeek 审计保存的原生解析缓存，未重跑 MinerU 推理，未覆盖原有审计、全量语料或索引。

## 实施结果

新增项目内逻辑表后处理，消费 `continues_prev`，以相邻物理页、章节、表题、表头、列宽和连续块顺序约束续表链。物理 HTML、单元格、块 ID、页码、JSON pointer 和字符区间保持可追溯。

表头结合 HTML 标签、列名与续页结构识别，支持多层路径；重复的单位、更新频率和指标解释按一级／二级指标区分。rowspan／colspan 映射到实际锚点，相同父值延续统一逻辑跨度，保留每页的来源组。

表格 chunk 的 `body` 保留完整物理文本，`text` 使用“列名：值”及父指标。数据块通过字段标签携带列语义，不再重复整行表头；长行按单元格及文字切分，最终 embedding 文本严格限制 400 token。`derived_sources` 记录复制、拼接、继承和重排的来源；原生缓存与后处理判断独立保存。

时间戳规则采用日期前缀、间隔后缀、未结束说明、边界空键和下一条记录共同提供的证据，将第 29／30 页的数据续接为 `2025101508301500000086400`，说明同步拼接。规则没有把“长度(B)”等同于字符串字符数，也不根据空键或原生数组单独合并记录。

项目 LLM 边界分类器已实现：输入包含表头、视觉父锚点、前后各最多两条记录和字段约束；输出区分新记录、续行、父字段延续及不确定。结构不支持、接口失败、模型与确定性证据冲突时保留原片段并告警。判断与输入身份保存，离线重放不请求网络；失配的判断缓存明确拒绝。请求从环境变量读取密钥，不保存请求头、原始响应或推理内容。JSON 输出和思考开关采用 [DeepSeek 官方接口](https://api-docs.deepseek.com/api/create-chat-completion)。

## 五组缓存回归

| 样本 | 物理片段 | 项目逻辑表 | 逻辑行数（含表头） |
| --- | ---: | ---: | --- |
| 监测分析 | 5 | 1 | 35 |
| 服务商评价 | 2 | 1 | 38 |
| 解决方案分类 | 4 | 3 | 51／21／11 |
| 芯片安全 | 2 | 1 | 36 |
| 标签标识 | 4 | 2 | 6／11 |

17 个物理片段形成 8 个逻辑表；15 个可见物理表头全部识别。B.2 的时间戳续行消除，逻辑行数从 12 降为 11。跨页父字段不重复拼接名称；分类表 1／2／3 和 B.1／B.2 保持独立。

隔离输出共 61 个 chunk，其中表格 49 个、包含跨页物理来源的表格 chunk 9 个；最终文本最大 400 token。五份文档均通过原生保存校验、来源覆盖、逻辑值重建、派生文本映射和确定性重放；6 项有界质量检查通过。

完整本地测试集 **106 项通过**，其中新增表格测试覆盖冻结五组样本、多层表头、独立表反例、章节／缺页边界、空键新记录、超长单元格、几何重排与父字段延续的组合、错误／失配 LLM 判断、请求失败、无密钥离线重放及判断产物篡改。

Windows 沙箱内的 asyncio 内部 socketpair 阻塞了最初的完整测试；最终测试在允许 localhost 的执行环境中完成，运行前清空 DeepSeek 密钥并关闭 Ragas 遥测，外部 API 使用 mock。

## 本地检索对照

冻结 15 道开发问题，对五份文档全部可检索 chunk 使用同一个本地 BGE 模型、查询指令和余弦排序。证据完整率要求相关物理字符来源全部召回；列上下文完整率还要求值及其列名出现在有对应来源的检索文本中。后者是结构代理指标，不是回答正确率。

| 指标 | 修复前 | 最终规则方案 |
| --- | ---: | ---: |
| Top-1 原始证据完整率 | 6/15（40.0%） | 8/15（53.3%） |
| Top-5 原始证据完整率 | 14/15（93.3%） | 13/15（86.7%） |
| Top-10 原始证据完整率 | 14/15（93.3%） | 14/15（93.3%） |
| Top-1 列上下文完整率 | 5/15（33.3%） | 8/15（53.3%） |
| Top-5 列上下文完整率 | 11/15（73.3%） | 13/15（86.7%） |
| Top-10 列上下文完整率 | 11/15（73.3%） | 14/15（93.3%） |

Top-5 回退题是 `monitoring_new_modes`：目标记录的解释较长，纯向量检索未把完整记录排入前五。逐题改善与回退均已记录。移除重复表头及调整短字段／说明的展示顺序减少了冗余，但没有消除该回退。因此新 chunk 和索引仅在隔离目录验收，**尚未替换全量生产索引**。本次没有执行回答模型评测，结果不能推断全部 33 份文档的准确率。

## 待完成项

1. **监测表第 12 页源文结构待复核。** 原图本身有横线及“个／次”两个单位，保留两行和 `table_row_structure_requires_review`。原审计脚本中强制拼成一个指标的失败断言已改为待复核。
2. **标签表第 29 页文字顺序待复核。** 保存的模型结果只有表级坐标，缺少单元格文字位置。保留 `table_text_order_requires_review`。已实现可选 `--table-geometry` 接口：输入绑定原文件 SHA256，只有完整覆盖原单元格字符、坐标位于该单元格内的分段才可重排；不凭语义重写原文。
真实 LLM A/B 已在取得具体出站授权后完成一次：9 个请求、7 个可校验返回、2 个 `ValueError`，最终仅采用 2 个新记录判断。LLM 方案未完成时间戳续接，质量检查 4 项通过、2 项失败，Top-10 列上下文完整率从规则的 14/15 降为 13/15。项目 LLM 继续默认关闭；完整结果见[真实 A/B 报告](/D:/WorkSpace/Projects/rag/docs/table_processing_llm_ab_20261009.md)。此前沙箱的 `URLError` 与审批拒绝均不计为真实模型效果实验。

## 复查与运行

- [最终逻辑表回归汇总](D:/WorkSpace/Projects/rag/data/processed/table_repair_20261009/audit_summary.json)
- [质量检查及复核队列](D:/WorkSpace/Projects/rag/data/processed/table_repair_20261009/quality_checks.json)
- [最终逐题检索对照](D:/WorkSpace/Projects/rag/data/processed/table_retrieval_final_20261009/retrieval_comparison.json)
- [冻结问题与来源](D:/WorkSpace/Projects/rag/data/processed/table_retrieval_final_20261009/questions.json)
- [最终测试日志](D:/WorkSpace/Projects/rag/tmp/table_tests_final_all.log)

每个样本的 `documents.jsonl` 包含 `table_views`；`tables/<doc_id>/table_decisions.json` 保存配置与判断身份。检索对照目录中的 `baseline/`、`rules/` 各有语料、向量矩阵和索引身份，记录 chunk、文档及模型权重哈希。

```powershell
conda activate rag
python scripts/rebuild_table_audit.py --refresh
python scripts/evaluate_table_retrieval.py --output data/processed/table_retrieval_new
python -m unittest discover -s tests
# 取得出站授权后，在新目录执行真实 LLM A/B
python scripts/rebuild_table_audit.py --llm --output data/processed/table_repair_llm_new
```

schema 保持 v2，适配器与切块策略更新为 v3。现有原生 v2 缓存通过配置与哈希校验后可复用；旧派生语料需重建，新版本验证器会明确拒绝旧切块策略。
