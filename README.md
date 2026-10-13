# MinerU 结构化解析与切块

在 `conda rag` 中使用 MinerU 4.0.10 本地解析 PDF/DOCX，再生成可直接 embedding 的结构切块。PDF 使用 Standard、自动 OCR；DOCX 使用 Flash 原生解析。Torch 小模型与 llama.cpp 模型位置由 [AGENTS.md](AGENTS.md) 指定，项目配置在 `config/mineru.yaml`。

模型已准备完成时，解析设为 `local`，缺失模型明确报错。关闭远程 VLM、LLM 标题分级和额外图像描述；启用 DeepSeek-flash 辅助跨页表格单元格判断，密钥从 `DEEPSEEK_API_KEY` 读取。文档、本地推理及 LLM 请求串行执行，不自动降低档位。模型运行需要在模型目录创建临时锁文件。

`llm_aided.features.cross_page_table_cell_merge` 控制 MinerU 原生判断，设为 `false` 可关闭；此项维持原配置。项目适配层现在读取 `continues_prev`，在保留物理片段的同时建立逻辑表。原生 `cell_merge` 作为候选证据，不能直接决定文字拼接。项目后处理默认使用规则，`table_processing.llm_enabled: true` 才启用带表头、父锚点和前后记录的独立边界判断；请求串行、超时 45 秒，失败或冲突保留片段并告警。原生 `enable_thinking: null` 保持供应商默认；项目边界请求显式使用 `thinking: {type: disabled}`。

## 运行

```powershell
conda activate rag
python -m pip install -r requirements.txt

# 本次验收：只运行代表性样本和自包含扫描页
python -m rag.smoke
python -m unittest discover -s tests -v

# 对同一组复杂表格重新解析，记录原生 LLM 边界提示与逐列判断
python scripts/audit_tables.py --output data/processed/table_audit_deepseek_20261009 --trace-llm

# 后续全量重建（本次任务不执行）
python -m rag.chunk --input data/raw --output data/processed/mineru_v1
python -m rag.verify --output data/processed/mineru_v1

# 修改切块参数后复用保存的解析结果，不再进行模型推理
python -m rag.chunk --input data/raw --output data/processed/mineru_v1 --reuse-parsed

# 从已有五组原生缓存重建隔离的逻辑表、语义切块和验证结果
python scripts/rebuild_table_audit.py
# 修改后处理后，只刷新该派生目录（不重跑 MinerU）
python scripts/rebuild_table_audit.py --refresh

# 本地 BGE 的 15 道冻结表格问题对照；索引和结果写入隔离目录
python scripts/evaluate_table_retrieval.py --output data/processed/table_retrieval_new

# 可选：真实 LLM A/B，需要授权向 DeepSeek 发送边界上下文
python scripts/rebuild_table_audit.py --llm --output data/processed/table_repair_llm_new
```

`--include` 接收输入目录内的相对文件路径；`--pages 1-2` 只限制 PDF，DOCX 仍读取全文。无页范围时解析全部 PDF 页。运行报告记录输入选择与页范围，冒烟结果不作为全量语料。

默认 BGE tokenizer 为 `D:/WorkSpace/AIModels/BAAI/bge-large-zh-v1.5`，仅加载本地 tokenizer。默认 `--max-tokens 400 --overlap-tokens 50`，标题计入 400 token，总计不包含特殊 token。

## 切块规则

- 文档、章节为硬边界。优先采用 MinerU 标题类型与层级，中文及数字编号规则补充缺失结构；条款正文保留为正文。
- 正文按段落、条款、列表合并，超长时按句子、分句、字符切分；同章节相邻正文组最多重叠 50 token。
- 表格、公式、代码和图片文字独立成组。表格保存完整 HTML，并按物理单元格及行线性化，保留 rowspan/colspan；不重复展开合并单元格。
- 续表只在相邻物理页、相同章节、连续表格块、兼容列结构／表题／表头且有 `continues_prev` 时归入同一逻辑表。表头组合 HTML 标记、列名和续页重复结构识别，支持多层路径；同名“单位／更新频率／指标解释”区分一级、二级指标。
- 表格 `text` 使用“列名：值”，继承父指标；正文 `body` 仍完整保留物理文本。数据块通过字段标签携带表头语义，避免再重复整行表头。大表优先按行，超长行按单元格和文字拆分；最终 `text` 严格计入 400 token，表格没有 overlap。预算不足时告警省略上下文，原文仍在 `body`。
- 相同父值的 rowspan 延续统一逻辑锚点，不重复拼接名称；明确的记录续行拼接已有数据和说明。空键、相同列数或原生标志均不足以单独证明应当续接。不把“长度(B)”当作示例字符数。
- 原生结果没有单元格文字坐标时，不猜测重排。可通过 `--table-geometry <json>` 提供与输入 SHA256 绑定的单元格分段坐标，完整覆盖物理字符且位于同一单元格时才按行、列重排；异常几何和源文疑似结构错误进入复核。
- 公式保留 LaTeX；超长结构标记 `structure_split`。表格、公式、代码和图片文字均不使用正文 overlap。
- 保存图片和图表资产、图题及脚注，不额外生成描述。无文字资产保留审计记录，不生成空文本 chunk。
- 目录保留切块但排除检索；页眉、页脚、页码及边栏保留审计记录并排除检索；页面脚注参与内容切块。未知类型告警并保留可读取文字。

## 输出接口：schema v2

| 产物 | 内容 |
| --- | --- |
| `documents.jsonl` | 原始输入哈希、规范化块、章节、排除原因、解析档位及资产清单 |
| `chunks.jsonl` | `body`、最终 embedding 输入 `text`、结构类型、标题路径、token、重叠与来源 |
| `report.json` | 选择范围、版本指纹、产物哈希、文档状态、统计及告警 |
| `verification.json` | 原始解析适配校验、来源覆盖、资产完整性及确定性重放结果 |
| `parsed/<doc_id>/` | MinerU Markdown、Middle JSON、结构化内容、模型输出（如有）和图片 |
| `tables/<doc_id>/table_decisions.json` | 后处理配置、输入身份、边界输入及经校验的判断；与原生解析缓存分开 |

来源包含 MinerU 类型、原始块索引及 JSON pointer；PDF `page` 从 1 开始，`page_idx` 从 0 开始。DOCX `content_index` 是内容位置，不是实际页码。字符区间均为左闭右开，指向规范化块；表格另有行列、合并跨度和单元格内字符区间。

`sources` 描述物理 `body` 及 overlap，`context_sources` 描述重复列名、表题和父字段在 `text` 中的位置；`derived_sources` 将派生文本区间映射到一个或多个物理来源，记录 copy／concat／extend_parent／reorder／inherit／label 操作。`title_prefix_char_count` 标记标题前缀。图片通过 `asset_refs` 与文档下的解析目录关联。

文档新增 `table_views`，记录逻辑身份、物理片段、列路径、逻辑记录、锚点、来源及逐边界操作。表格 chunk 带 `table_id`、`table_view_version`。schema 保持 v2，解析适配和切块策略更新为 v3；旧原生 v2 缓存满足现有哈希及配置校验时可复用，但旧派生结果需重新切块。

`doc_id` 由相对输入路径及文件哈希生成；chunk ID 包含解析适配版本、切块版本、参数、内容和来源。保存解析结果的切块重放应完全一致；重新执行模型推理不保证字节一致。

只有 `retrieval_enabled == true` 的块进入 embedding：

```python
from pathlib import Path
from rag.retrieval import iter_retrieval_chunks

for chunk in iter_retrieval_chunks(Path("data/processed/mineru_v1/chunks.jsonl")):
    text = chunk["text"]
```

单文档失败记录原因并继续。失败、损坏输入、无可用正文或模型/资源不足时返回非零状态。解析缓存仅在输入身份、页范围、LLM 开关与模型配置及保存文件校验全部一致时复用；中断产生的不完整缓存必须重新解析。旧的、不含 LLM 配置身份的缓存需要重解析。密钥不写入缓存或报告；一致的已保存解析可在没有密钥时重放切块。验证也检查报告哈希，拒绝混合或修改的产物。

项目 LLM 判断启用时，`--reuse-parsed` 只重放已保存的同配置判断；显式添加 `--refresh-table-decisions` 才重新请求。验证不会调用网络。几何输入格式为 `{"documents":[{"source_path":"technical/sample.pdf","source_sha256":"…","cells":[{"block_id":3,"column":2,"cell_bbox":[0,0,1,1],"segments":[{"block_char_start":4,"block_char_end":6,"bbox":[0.1,0.1,0.2,0.2]}]}]}]}`；坐标为页面归一化坐标，字符区间指向规范化块。

本次实现与有界回归见 [表格修复结果](docs/table_processing_repair_20261009.md)。五组样本存在两处待复核内容；本地检索对照仍有一题 Top-5 原始证据召回回退，不能据此覆盖旧全量索引。

经具体出站授权完成的单次 [真实 LLM A/B](docs/table_processing_llm_ab_20261009.md) 未通过时间戳续接验收，Top-10 列上下文完整率由规则方案的 14/15 降为 13/15；项目边界分类器继续默认关闭，实验产物保存在隔离目录。

## 通用评测

当前评测集为 `data/benchmark/gold150_hard_v2`，从旧150题金标准的本地备份演进而来。沿用原始33份文档与切块快照，政策、技术各75题；基础15题、中等45题、困难60题，拒答、纠错、澄清各10题。全部为开发集，原冻结决定已撤销，本版未重新冻结。题集、查询文件、证据审核及版本指纹可校验；审核方式为Codex离线来源审核，不声称人工复审。

使用 `--allow-evolving` 显式评测演进集，`--local-only` 运行本地流程而不连接Langfuse。初次5题验证见 [难度演进与5题验证](docs/benchmark-hard150-v2-20261013.md)，原全量运行见 [历史全量结果](docs/benchmark-full150-v2-20261013.md)。评分流程现已修复：原15题全部恢复，150/150题的适用指标完成率100%，见 [评分修复与最终结果](docs/benchmark-scoring-fix-v2-20261013.md)。默认 `--judge-protocol fragments` 使用可回溯片段，按指标独立校验并检查每个引用位置的评估覆盖；`--reuse-judgments-from` 显式校验模型判定来源后重算，`--reuse-faithfulness-from` 校验并复用忠实度，完整复用可离线运行。自动评委仍有已记录的语义分歧，结果保留未校准诊断标记；不同题目、协议或分母的均值不直接比较。

通用生成、审核、冻结、检索及评分代码保留。相关命令必须显式指定 `--corpus` 或 `--release`，不自动恢复旧发布：

```powershell
python scripts/build_benchmark_kg.py --corpus data/processed/mineru_v1 --mode smoke
python scripts/generate_benchmark.py --help
python -m rag.benchmark --help
python -m rag.benchmark.first_run --help
```

评测依赖单独保留在 `requirements-benchmark.txt`。新题库证据已按原始快照重新审核；embedding 与回答仍使用本地 BGE、DeepSeek-flash，评委沿用千问，密钥从环境变量读取。

冒烟验证解析和切块可运行、预算与来源可校验；不代表 33 份文档已全部处理，也不证明检索或回答质量提升。
