# MinerU 结构化解析与切块

在 `conda rag` 中使用 MinerU 4.0.10 本地解析 PDF/DOCX，再生成可直接 embedding 的结构切块。PDF 使用 Standard、自动 OCR；DOCX 使用 Flash 原生解析。Torch 小模型与 llama.cpp 模型位置由 [AGENTS.md](AGENTS.md) 指定，项目配置在 `config/mineru.yaml`。

模型已准备完成时，解析设为 `local`，缺失模型明确报错。关闭远程 VLM、LLM 辅助后处理和额外图像描述；文档及推理串行执行，不自动降低档位。模型运行需要在模型目录创建临时锁文件。

## 运行

```powershell
conda activate rag
python -m pip install -r requirements.txt

# 本次验收：只运行代表性样本和自包含扫描页
python -m rag.smoke
python -m unittest discover -s tests -v

# 后续全量重建（本次任务不执行）
python -m rag.chunk --input data/raw --output data/processed/mineru_v1
python -m rag.verify --output data/processed/mineru_v1

# 修改切块参数后复用保存的解析结果，不再进行模型推理
python -m rag.chunk --input data/raw --output data/processed/mineru_v1 --reuse-parsed
```

`--include` 接收输入目录内的相对文件路径；`--pages 1-2` 只限制 PDF，DOCX 仍读取全文。无页范围时解析全部 PDF 页。运行报告记录输入选择与页范围，冒烟结果不作为全量语料。

默认 BGE tokenizer 为 `D:/WorkSpace/AIModels/BAAI/bge-large-zh-v1.5`，仅加载本地 tokenizer。默认 `--max-tokens 400 --overlap-tokens 50`，标题计入 400 token，总计不包含特殊 token。

## 切块规则

- 文档、章节为硬边界。优先采用 MinerU 标题类型与层级，中文及数字编号规则补充缺失结构；条款正文保留为正文。
- 正文按段落、条款、列表合并，超长时按句子、分句、字符切分；同章节相邻正文组最多重叠 50 token。
- 表格、公式、代码和图片文字独立成组。表格保存完整 HTML，并按物理单元格及行线性化，保留 rowspan/colspan；不重复展开合并单元格。
- 大表按行切块，重复明确识别的表头、表题；表头依据 HTML 的 th/thead，或首行全部为已知列名（至少两列），不默认把首行当表头。单行超限先按单元格拆分，再拆分超长单元格。重复上下文单独记录来源，不参与正文重组。表头/表题本身超出上下文预算时完整保留在正文，记录 `table_context_omitted_budget`。
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

来源包含 MinerU 类型、原始块索引及 JSON pointer；PDF `page` 从 1 开始，`page_idx` 从 0 开始。DOCX `content_index` 是内容位置，不是实际页码。字符区间均为左闭右开，指向规范化块；表格另有行列、合并跨度和单元格内字符区间。

`sources` 描述正文及 overlap，`context_sources` 描述重复表头/表题在最终 `text` 中的位置；`title_prefix_char_count` 标记标题前缀。图片通过 `asset_refs` 与文档下的解析目录关联。

`doc_id` 由相对输入路径及文件哈希生成；chunk ID 包含解析适配版本、切块版本、参数、内容和来源。保存解析结果的切块重放应完全一致；重新执行模型推理不保证字节一致。

只有 `retrieval_enabled == true` 的块进入 embedding：

```python
from pathlib import Path
from rag.retrieval import iter_retrieval_chunks

for chunk in iter_retrieval_chunks(Path("data/processed/mineru_v1/chunks.jsonl")):
    text = chunk["text"]
```

单文档失败记录原因并继续。失败、损坏输入、无可用正文或模型/资源不足时返回非零状态。解析缓存仅在输入身份、页范围及保存文件校验全部一致时复用；中断产生的不完整缓存必须重新解析。验证也检查报告哈希，拒绝混合或修改的产物。

## 通用评测

旧解析数据、旧金标准、旧评测运行及缓存已清理。本次不重建题库，不调用外部评测服务；没有当前可用的冻结评测发布。

通用生成、审核、冻结、检索及评分代码保留。相关命令必须显式指定 `--corpus` 或 `--release`，不自动恢复旧发布：

```powershell
python scripts/build_benchmark_kg.py --corpus data/processed/mineru_v1 --mode smoke
python scripts/generate_benchmark.py --help
python -m rag.benchmark --help
python -m rag.benchmark.first_run --help
```

评测依赖单独保留在 `requirements-benchmark.txt`。未来生成新题库时重新审核新来源位置；embedding 与后续回答仍使用本地 BGE、DeepSeek-flash，密钥从环境变量读取。

冒烟验证解析和切块可运行、预算与来源可校验；不代表 33 份文档已全部处理，也不证明检索或回答质量提升。
