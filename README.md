# RAG 文档 Chunking Baseline v1

固定 RAG benchmark 的候选生成、人工审核、冻结和评测入口见 [docs/benchmark.md](docs/benchmark.md)。依赖单独固定在 `requirements-benchmark.txt`，不会改变原切分流程。

文本解析 → 结构识别 → recursive chunking。PDF 使用 PyMuPDF4LLM，DOCX 使用 python-docx；不做 OCR、embedding、检索或 LLM 调用。

## 本次数据集结果

33 份文档（24 PDF、9 DOCX）全部处理成功，生成 **1,390 个 chunks**。最终文本长度 26–400 tokens，平均 266.34；overlap 最大 50 tokens。17 项测试通过，全量重放核对了 5,303 个来源区间，chunk ID 和内容一致。

15 个 PDF 页面没有可提取文本，已记录并跳过。抽查详情与限制见 `data/processed/baseline_v1/quality_review.md`；完整统计见同目录 `report.json` 和 `verification.json`。原始数据、派生产物和临时检查文件已在 `.gitignore` 中排除。

## 运行

在项目根目录执行：

```powershell
conda activate rag
python -m pip install -r requirements.txt
python -m rag_baseline.chunk --input data/raw --output data/processed/baseline_v1
python -m rag_baseline.verify --output data/processed/baseline_v1
python -m unittest discover -s tests -v
```

当前 `rag` 环境已具备运行依赖；测试使用标准库 unittest。`requirements.txt` 固定直接依赖，实际 Python 和依赖版本记录在报告中。

默认 tokenizer 路径：`D:/WorkSpace/AIModels/BAAI/bge-large-zh-v1.5`。仅加载本地 tokenizer，不加载模型权重、不访问模型服务、不读取 API key。可用 `--model-path` 指定其他本地路径。默认 `--max-tokens 400 --overlap-tokens 50`；计数不包含模型特殊 token。本地 BGE 模型最大输入为 512，因此默认文本预算为后续模型输入留有余量。

每次运行按相对路径排序处理所有 PDF/DOCX，覆盖指定输出目录中的三个主产物。其他文件不处理。文档失败后继续处理剩余文档；失败或无可提取文本的文档会使命令退出码为 1。参数错误、模型缺失和输入为空也返回 1。

## 切分规则

- 文档与章节为硬边界，页码为来源信息；同章节可跨 PDF 页。
- 优先保留段落、条款、表格行；超长时按句子、分句、字符逐级拆分。使用原始字符切片，避免 token 解码改变文字。
- `text = 文档标题 + 换行分隔的章节路径 + 空行 + body`，整体最多 400 tokens；每次合并重新计数，不做静默截断。
- overlap 从前一个 chunk 正文尾部选择连续完整单元，最多 50 tokens。自然边界不足时细化至字符边界；空间不足可缩小或归零。每个新 chunk 都包含新正文。
- 标题使用解析结果、Word 样式／大纲和中文、数字、附录编号规则，兼容 PDF 多余的编号空格及 Markdown 项目符号。含正文的条款不是标题；目录（含“目次”）保留文字并隔离标题状态。
- 文档标题优先匹配开头的明确标题与文件名；排除发文序号、导航和“中华人民共和国国家标准”等通用标签，失败时使用文件名。
- PDF 仅删除解析器标记的页眉页脚；其中符合显式编号规则的章节标题会保留，并记录在 `rescued_edge_headings`。被过滤内容记录为 `filtered_edges`，便于审计。
- 对解析器标为公式、Markdown 未输出的区域，使用 PyMuPDF 按该区域读取已有文本层，并在 `recovered_formula_text` 记录；不做 OCR 或公式识别。这也可补回被误判为公式的章节标题。
- DOCX 按段落／表格的原顺序读取，表格按行线性化，合并单元格在每行内去重；嵌套表格文字保留。无真实页码，使用从 1 开始的内容块和行索引。
- 标题前缀耗尽预算时，该文档失败并记录原因。短章节保留，标题无正文且未由后续章节继承时保留为单独内容。

## 输出接口

所有 JSONL 均为 UTF-8，每行一个对象。schema v1。

| 文件 | 内容 |
| --- | --- |
| `documents.jsonl` | 文档 ID、相对来源路径、分类、输入 SHA-256、状态、标题、解析器信息、规范化文本块、章节及来源位置 |
| `chunks.jsonl` | 每个 chunk 的 `body`、最终 embedding 输入 `text`、标题路径、token 数、实际 overlap、稳定 ID 和来源区间 |
| `report.json` | 参数、依赖与代码版本指纹、tokenizer 文件哈希、每文档状态、空文本页、告警和长度分布 |
| `verification.json` | 独立校验命令产出；对保存的解析块重放结构识别和切分，核对 ID、完整内容及来源 |

后续 embedding 应直接读取 **`chunks.jsonl` 的 `text` 字段**，不要再次拼接标题。`body` 用于检查正文和重叠。

### 检索排除标记

目录 chunks 保留原文、ID 和来源，增加以下字段：

- `content_type`：`toc` 表示目录相关内容，`body` 表示其他内容。
- `retrieval_enabled`：目录为 `false`，其他为 `true`。
- `retrieval_exclusion_reasons`：目录为 `["table_of_contents"]`，其他为空列表。

按已有目录块角色、来源（含 overlap）和精确目录标题识别，不因正文提及“目录”或标题包含“产品目录”“行业分类指导目录”而排除。此标记不改变切分边界或物理删除记录。当前 82 个目录 chunks 占记录数 5.90%、token 数 6.96%，剩余 **1,308 个**允许进入检索链路。逐文档统计与排除 ID 见 `report.json` 的 `retrieval_filter`。

新运行 chunking 会自动标记；既有产物可原地补标记，不重新解析或切分：

```powershell
python -m rag_baseline.retrieval --output data/processed/baseline_v1
python -m rag_baseline.verify --output data/processed/baseline_v1
```

后续 embedding／建索引统一使用过滤入口：

```python
from pathlib import Path
from rag_baseline.retrieval import iter_retrieval_chunks

for chunk in iter_retrieval_chunks(Path("data/processed/baseline_v1/chunks.jsonl")):
    text = chunk["text"]  # 仅包含 retrieval_enabled == True 的记录
    # embedding / 写入索引
```

读取入口对缺少标记的旧记录报错，避免未经筛选进入索引。未来检索模块也应使用相同标记；若使用全量索引，查询必须过滤 `retrieval_enabled == true`。当前项目尚无向量库或检索实现。

- `doc_id` 由相对来源路径和文件内容 SHA-256 生成；`chunk_id` 包含文档 ID、章节、字符范围、参数和文本。相同输入及实现得到相同结果；变更输入或切分会改变相关 ID。
- `heading_block_ids` 指向文档解析块中的继承标题；文档标题来源块的 `role` 为 `document_title`，文件名回退见告警。
- `section_char_start/end` 是章节拼接正文中的左闭右开字符区间。正文块以两个换行连接。
- `sources` 指向原规范化文本块：`block_char_start/end` 是块内左闭右开区间，`body_char_start/end` 是当前 chunk 正文区间。PDF 的 `page` 从 1 开始；DOCX 的 `content_index` 从 1 开始，表格另附 `table_index`（所在内容块索引）及 `row`。
- `is_overlap` 标识来自上一 chunk 的文本；`overlap_char_count` 便于去重重组，`overlap_token_count` 为其实际 token 数。拼接产生的换行无单独来源区间。

## 验证与限制

每个成功文档都执行章节正文逐字重组检查。独立校验额外确认全部解析块都被正文、标题或继承标题覆盖，并核对来源字符区间及重放结果。

重放验证从保存的解析块开始，不代表不同 PDF 解析器版本会产生相同文本。运行指纹用于识别这种差异。

PDF 标题、阅读顺序和表格文字质量仍受解析器影响；编号标题和目录边界为保守规则。图片、扫描件、图示信息不转写。空文本页可能是空白页或图片页，不自动认定为扫描件。DOCX 文本框、修订记录、页眉页脚不在本版提取范围。保留正文中的网站导航等文字，不做复杂清洗或跨文档去重。
