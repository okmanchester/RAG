# 表格处理与 MinerU 跨页合并审计

审计日期：2026-10-09。使用本地 conda `rag`，MinerU **4.0.10**、docvortex **0.5.11**、RTX 4060 Laptop GPU。对当前 `data/raw/technical` 的 5 份文档共 15 个 PDF 物理页重新执行本地解析，没有复用旧冒烟解析。参数沿用生产配置：Standard、自动 OCR、关闭额外图像描述、关闭 LLM 辅助后处理，串行运行。

## 结论

**原生表格级跨页拼接可用，但不能据此认定跨页单元格与行已经正确合并。当前系统还没有接入原生的续表关系。**

本次 17 个物理表格片段包含 9 个续表边界。MinerU 正确识别这些边界，在默认原生渲染中形成 8 张逻辑表；所选独立表保持分开。当前系统仍把同一批片段作为 17 个独立表组，生成 28 个表格 chunk，跨页表格 chunk 为 0。独立表组数量与代码证据共同表明系统没有采用续表关系；单独的“跨页 chunk 为 0”并不足以证明未合并，因为 token 预算也可能导致按页分块。

5 次解析的产物校验、来源字符覆盖、400-token 预算和确定性切块重放均通过。这些检查证明程序没有丢失**已解析文字**，不能证明 OCR、表格行列、语义关系或跨页单元格正确。

## 实测样本与原生结果

页码为 PDF 物理页，从 1 开始；不采用页面印刷页码。

| 样本 | 页码 | 物理片段 → 原生逻辑表 | 原生表行数（含表头） | 核对结果 |
| --- | --- | --- | --- | --- |
| 工业互联网平台监测分析指南 | 9–13 | 5 → 1 | 35 | 四个续接边界识别正确，重复表头去除；第 12 页一条二级指标被错误拆为两行，合并后仍错误 |
| 工业互联网平台服务商评价方法 | 13–14 | 2 → 1 | 38 | 续表识别、重复表头去除、7 列位置正常；跨页重复的“4／服务成效”保留为两个局部 rowspan，没有统一成一个跨页 rowspan |
| 工业互联网平台解决方案分类方法 | 11–13 | 4 → 3 | 51、21、11 | 表 1 的跨页编码连续，重复表头去除；表 2、表 3 保持独立，没有按相同列数误合并 |
| 人工智能加速芯片安全功能技术规范 | 23–24 | 2 → 1 | 36 | 4 列结构和续表正常，5.3 d)→6.3.4、5.3 e)→6.3.5 对应正确；“增强级／固件安全”的 rowspan 仍按分页分段 |
| 网络数据标签标识技术要求 | 28–30 | 4 → 2 | 6、12 | B.1、B.2 各自续接正确且互不混合；B.2 的时间戳单元格跨页续接失败 |

原生 Markdown 对复杂合并单元格使用 HTML，对标签示例使用管道 Markdown。审计同时检查两种格式，不能仅统计 `<table>` 标签。`native_merged_tables.json` 来自安装版本的原生默认渲染计划，并核对其表数与实际 `markdown.md` 一致。

### 明确的跨页单元格错误

标签文档表 B.2 的 PDF 第 29 页最后一行和第 30 页第一行属于同一个时间戳记录。原生输出为：

```text
时间戳 | 24 | 20251015083015000000 | 表示2025年10月15日上午8点30分
       |    | 86400                | 15秒开始传输，时间间隔为1天。
```

正确的示例数据应在同一单元格中续接为 `2025101508301500000086400`，说明也应续接，最终逻辑表应少一行。当前原生输出只是把两张表连接起来，没有完成这个单元格级操作。两页 Middle JSON 均未提供 `cell_merge`。

另有表内文字顺序问题，例如同表“数据源主体”的说明被解析为“本甲。次数据流转过程中的数据源主体是”。这是单页文字排序错误，不应归因为跨页合并。

### 复杂表格中的单页行分割错误

监测表 PDF 第 12 页顶部二级指标“**不同行业分类公有云工业 APP 日均调用次数**”在原页是一个单元格，被原生 HTML 拆成两行：

```text
第 24 行第 6 列：不同行业分类公有云工业
第 25 行第 6 列：APP日均调用次数
```

其说明同时被拆开，单位出现不一致。默认跨页渲染保留这两行，没有修复上游的行分割错误。这个问题发生在单页内部，不能只通过修复页边界合并解决。

## 当前系统的处理方法

流程：`MinerU parse → 保存 Middle JSON／原生 Markdown／模型结果 → adapt_middle → build_sections → chunk_document → verify`。

1. [parsers.py](D:/WorkSpace/Projects/rag/rag/parsers.py) 的 `TableHTML` 读取物理单元格，保留 `rowspan`、`colspan` 和行列位置，每行用 ` | ` 连接，完整 HTML 保存在首行块。优点是原始结构可追溯，不会重复展开合并单元格。
2. `adapt_middle()` 将每个页面内的 table 容器作为独立 `group_id`，没有读取 table 根块的 `continues_prev` 或 `cell_merge`。页码、JSON pointer 和单元格区间保留，但同一张跨页表没有统一逻辑身份。
3. [splitter.py](D:/WorkSpace/Projects/rag/rag/splitter.py) 按章节和 `group_id` 分组；表格与正文分开，优先按行，超长行再按单元格及文字拆分。表格不使用正文 overlap，默认最终文本最多 400 token。
4. 表题和识别出的表头作为重复上下文；表头只认 `<th>`／`<thead>`，或首行每个单元格都属于固定词表。预算不足时完整表头保留在 body，告警提示重复上下文被省略。
5. [verify.py](D:/WorkSpace/Projects/rag/rag/verify.py) 检查保存结果的一致性、来源覆盖、资产、预算和重放；没有与原页比对的跨页表格质量检查。

### 原生合并与项目配置的真实关系

本地源码证据来自安装版本，不用其他版本的在线说明代替：

- `docvortex/postprocess/pages.py` 调用 `merge_table()`。
- `docvortex/content/table/document.py` 只在相邻页、页边界结构等条件匹配时，给后表写入 `continues_prev=True`，Middle JSON 保留各页片段。
- `docvortex/render/_internal/common/planner.py` 在默认渲染时调用 `merge_table_content()`，完成 HTML 拼接和重复表头移除。
- `docvortex/content/table/content.py` 根据 `cell_merge` 进行边界单元格文字续接；没有标志时不能期待自动完成所有语义单元格续接。
- `mineru/backend/postprocess/table_merge/llm_cell_merge.py` 的 LLM 辅助功能只处理已标记的续表边界，返回逐列 0／1 标志，不是原生表格级续接的总开关。

[mineru.yaml](D:/WorkSpace/Projects/rag/config/mineru.yaml) 关闭 `cross_page_table_cell_merge`。因此本次仍有原生表格级拼接，但没有外部 LLM 辅助单元格判断。此外项目 `configure()` 也硬编码设置该功能为 False，**只修改 YAML 为 True 并不会启用它**。本次没有调用 DeepSeek，也没有评估打开该功能后的效果。

## 优化优先级

### P1：统一跨页表身份，保留真实来源

适配层消费 `continues_prev`，为续表链建立稳定 `table_id`，同时保留各页原始片段、局部行号及来源。切块使用统一表题与表头，允许在预算内跨页组合；重复表头作为上下文处理并记录来源。

不建议直接把合并后的 Markdown 当作唯一入库来源：默认合并结果沿用首表外层元数据，会弱化后续页的单元格来源。更合适的是“逻辑表视图 + 不变的物理来源”。章节仍应保持硬边界。

### P1：让 chunk 自带列语义与合并单元格上下文

当前 17 个片段中 15 个有可见表头，系统只识别 5 个（均来自监测表）。其他 HTML 全部使用 `<td>`，列名如“重点采集项／权重／编码意义／测评方法／字段”不在词表中。

例如服务商表的第二个 chunk 从“新技术融合水平”开始，仅重复表题，没有列名；“促进模式创新情况”也不重复它所在的“服务成效／经济效益／必选”合并单元格上下文。行列跨度保存在元数据中，但 embedding 输入不会读取这些元数据。

建议组合重复表头一致性、列结构和局部语义判断来识别表头，并支持多层表头。在派生的 embedding 文本中补充 `列名:值` 和继承的行头／父指标；原始 body 仍保持物理文本不重复，新增上下文通过 `context_sources` 指向真实来源。这样既保留审计能力，也避免单行文字脱离含义。

### P1：区分续表、续行与续单元格

建立独立边界判断，不把所有同列数表格或同名字段直接拼接。先检查相邻页、续表身份、表头、列结构与边界空单元格，再按列处理文字接续。时间戳等固定长度字段可提供确定性证据；不确定时保留片段并告警。

可进一步做 LLM 辅助的受控 A/B，但需先让配置真实生效，并记录逐列合并决策、前后结果和来源。当前样本不能证明打开 LLM 就能修复全部问题；同页的错误拆行与文字排序仍需单独处理。

### P2：增加结构质量检查与真实回归样本

检查表宽、有效列数、rowspan 覆盖、边界空键行、固定长度字段、重复表头、父指标继承和文字异常。对异常表或页做定点重解析／人工复核，避免无条件重跑全量。

将本次 5 组样本作为冻结回归集，分别检查续表识别、独立表不误合并、边界单元格续接、列映射、chunk 上下文及来源正确性。本次已有 17 项独立预期检查，13 项通过、4 项失败；失败对应已发现的质量问题，不是运行失败。

### P2：减少重复上下文占用预算

首个表格 chunk 的表题、表头同时出现在 prefix 和 body 中。可只重复首块未包含的上下文，或明确采用可重放的派生文本视图，保留来源覆盖。优先修复语义完整性，再决定是否调整表格 token 预算；当前没有检索质量证据支持直接提高 400-token 上限。

## 可复查产物

- [审计汇总](D:/WorkSpace/Projects/rag/data/processed/table_audit_20261009/audit_summary.json)：各样本表数、行数、表头与 chunk 统计。
- [质量检查](D:/WorkSpace/Projects/rag/data/processed/table_audit_20261009/quality_checks.json)：独立于保存校验的预期和实际值。
- [首次成功解析日志](D:/WorkSpace/Projects/rag/data/processed/table_audit_20261009/run.log)：真实模型解析与验证过程；单样本耗时包含切块和验证，不能视为纯推理基准。
- [监测表对照](D:/WorkSpace/Projects/rag/data/processed/table_audit_20261009/monitoring/review/tables.html)、[服务商表对照](D:/WorkSpace/Projects/rag/data/processed/table_audit_20261009/provider/review/tables.html)、[分类编码表对照](D:/WorkSpace/Projects/rag/data/processed/table_audit_20261009/classification/review/tables.html)、[芯片表对照](D:/WorkSpace/Projects/rag/data/processed/table_audit_20261009/chip/review/tables.html)、[标签表对照](D:/WorkSpace/Projects/rag/data/processed/table_audit_20261009/labels/review/tables.html)：原生逻辑表与逐页物理 HTML。
- 每个样本的 `parsed/` 保存未经项目修改的 MinerU 原生 Markdown、Middle JSON、结构化结果和模型输出；`review/` 保存原页 PNG、表格单元格清单与当前 chunk 文本。
- [审计脚本](D:/WorkSpace/Projects/rag/scripts/audit_tables.py)：再次实际解析运行 `python scripts/audit_tables.py`；仅刷新对照运行 `python scripts/audit_tables.py --inspect-only`。需使用 conda rag；真实解析需要模型目录临时锁文件的写权限。私有渲染计划接口仅供本次固定版本审计，不应作为未经封装的生产接口。

本次新增审计脚本、报告及隔离产物；未修改生产代码、默认参数或旧冒烟数据。没有执行全量文档重建、检索评测或 LLM 合并实验。本次 9 个续表边界全部识别正确，只适用于所选样本，不代表所有 33 份文档的整体准确率。
