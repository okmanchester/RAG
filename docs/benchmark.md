# 固定 RAG Benchmark（题集 schema v2）

长期目标为 **300 道审核题：240 dev + 60 holdout**。当前已冻结 **86道公开dev金标准**：旧30题在 `data/benchmark/trial30_final_v1`，新增56题在 `data/benchmark/batch56_final_v1`。两个版本保持独立，与彼此无已知同核心重复；新增56题含28道政策题、28道技术题。审核及冻结来源为用户明确授权的Codex工作，不冒称用户亲自逐题复审；这不代表300题正式配额已经完成。

定稿阅读入口是 `trial30_final_v1/review/reading.md`，金标准是 `gold.jsonl`，填写完成的表格在 `review/review.csv`。被测系统只接收 `queries.jsonl`；复制 `run_template.jsonl` 到发布目录外并填写真实输出。模板默认 `error=not_run`，不能当作系统已经运行。现有两道边界题分别为拒答、澄清，本试审集不衡量纠正错误前提能力。

新增56题的定稿阅读入口是 `batch56_final_v1/review/reading.md`，包含55道普通题和1道纠错题。它的 `review_provenance/` 归档了冻结前输入、修订记录、Claude复核意见处理、去重及全库边界核查。4道重复候选未进入金标准。与旧30题合计包含拒答、澄清、纠错各1题，边界样本量只适合诊断。

发布包含固定语料快照及每个文件的哈希校验；任何修订都应发布新版本。`freeze-trial` 专用于30道已审核试审题；`freeze-reviewed` 冻结显式授权的任意已审核批次，须全dev，禁止holdout，并可用旧发布集检查跨版本重复；原 `freeze` 的300题约束保持不变。`.gitattributes` 为两套发布目录禁用换行转换，Git存储字节须与冻结哈希一致。

本轮冻结记录的用户授权原文：把这可用的56题冻结为金标准，和之前已冻结的30题一起纳入版本控制。

```powershell
python -m rag_baseline.benchmark freeze-reviewed --items data/benchmark/batch_001_review60/revision_v2/approved.jsonl --manifest data/benchmark/batch_001_review60/revision_v2/manifest.json --families data/benchmark/batch56_final_v1/families.json --output data/benchmark/batch56_final_v2 --authorization "用户明确授权的本次冻结指令" --reference-releases data/benchmark/trial30_final_v1
```

输出目录必须尚不存在；该示例仅说明入口，冻结实际版本应使用该版本真实授权、来源和完整审核资料。已发布版本可直接通过 `load_release` 和 `evaluate --release` 加载，复制运行模板到发布目录外后填写真实结果。

## 环境与范围

使用 `conda rag`，依赖见 `requirements-benchmark.txt`；兼容代码按本地 Ragas **0.4.3** 验证。读取已生成 KG，不重新构图。生成模型为 `deepseek-flash`，密钥读取 `DEEPSEEK_API_KEY`；评委为北京接口的 `qwen3.7-plus-2026-05-26`，读取 `DASHSCOPE_API_KEY`。密钥不得写入题集、日志或版本控制。

以当前解析文本快照为依据，不测试 OCR、图片、多轮对话或联网。生成时参考摘要相似/实体交集关系，以文档族轮换控制覆盖；使用三个 Ragas synthesizer 的显式 scenario 接口生成单跳和多跳初稿，再抽取逐字原文证据。场景抽样由项目控制，不采用 Ragas 默认等权分布。边界题可由独立生成或明确记录来源的人工/agent编写候选提供，均需人工审核。

## 试审操作

本轮 v3 已根据 v2 审核意见修订，并完成用户委托的最终审核。`data/benchmark/trial_v3/review/review.csv` 已填写，原表备份为 `review_before_delegated_signoff.csv`；`reviewed.jsonl` 和 `finalization_audit.jsonl` 分别保存已审核记录及逐题依据。后续阅读以 `trial30_final_v1/review/reading.md` 为准。v2、`trial_v1` 及 `pilot_*` 保留历史，原始文件、解析快照、chunk 和 KG 未改动。以下审核操作说明供后续新批次使用，不需要重审已定稿的30题。

`trial_v3/submitted_review.csv` 是收到的审核表原样副本，`revisions.jsonl` 记录提交时已修改的字段及本轮处理，`README.md` 说明复审重点。复现本轮处理使用 `scripts/apply_trial_review.py`，必须指定尚不存在的输出目录。证据质量提示 `heading_only` 与 `table_layout_check` 应在 `source_quality` 项确认，不新增同名勾选字段。

`trial_v2/revisions.jsonl` 保存逐题来源 ID、原候选哈希及修订说明；`review/evidence_audit.jsonl` 独立记录证据的文档哈希、block、字符区间和质量提示。修订脚本 `scripts/revise_benchmark_trial.py` 可从历史试审集重现修订，必须指定尚不存在的输出目录。

修订后有 28 道普通题、2 道边界题，0 道点名文件题。0010 已取消缺乏充分反证的纠错标签，后续需补充可靠的 `correct_premise` 候选，不通过错标凑配额。

审核 CSV 采用 UTF-8 BOM，JSON 单元格必须保留有效 JSON。只编辑这些内容：

- `review_status`：`pending`、`approved`、`rejected`、`needs_revision`。
- 问题、参考答案、`answer_points`（必要要点的 JSON 数组）。
- `answer_form`（多标签 JSON 数组）、`evidence_span`、难度及依据、风格、主领域、预期行为、边界解释、`clarifying_interpretations`。
- `scope_note`（问题范围）、`multi_source_reason`（多来源不可替代性）、`point_evidence`（按必要要点顺序列出支持它的证据组 ID 数组）。
- `evidence_groups`：可以缩短原文证据或补充其他有效来源，但字符区间与原文必须一致。
- `duplicate_group`：语义相同或相同核心证据/答案的变体填相同组名。
- `reviewer`、`review_notes` 和 `review_checks`。

不要修改 `id`、`candidate_hash`。`machine_notes` 只提供辅助建议，不能作为通过依据。公式前缀有 CSV 防注入转义，导入时会还原。

`review_checks` 是人工确认项，不会自动勾选。例如可回答题完成全部审核后填写：

```json
{"question":true,"answer":true,"evidence":true,"labels":true,"duplicates":true,"corpus_search":false,"scope":true,"point_support":true,"completeness":true,"source_quality":true,"multi_source":true}
```

边界题还必须把 `corpus_search` 设为 `true`：除检查阅读稿中的全库混合检索结果，还应按题意检查其他可能含答案的原文。检索未命中不能证明语料无答案。`clarify` 必须有两种实质解释和一个明确澄清问句，不能把“是否想知道全文”当成必要澄清。

审核应核对实际标签和难度，不能按 `generation_targets` 直接通过，也不能因为当前 RAG 答错就删除合格题。答案要点应包含边界条件、例外；不同表述及替代证据可以同样正确。

新增五项人工检查分别确认范围、逐要点支持、完整性、来源质量和多来源必要性；单来源题的 `multi_source` 表示确认无需多来源。跨页表格须核对表头与列对应，标题只可证明场景/类别存在，前言中的定义可作为实质证据。机器预审不会勾选这些项目。

生成和 CSV 导入共用客观内容守卫，阻止参考文献单独作答、损坏公式、元数据导航、文件提示错标及部分可识别的范围错误。语义预审单独检查支持性、完整性、适用范围和任务关联性，但它不是语义正确性的证明；人工确认仍是冻结前提。修改内容后，旧语义预审作废，导入结果重新记录证据审核。阅读稿不会跟随手工编辑 CSV 自动变化：导入后应导出到新审核目录，三种格式才能同步。

```powershell
conda activate rag
python -m rag_baseline.benchmark import-review --items data/benchmark/trial_v3/candidates.jsonl --review data/benchmark/trial_v3/review/review.csv --output data/benchmark/trial_v3/reviewed.jsonl
python -m rag_baseline.benchmark validate --items data/benchmark/trial_v3/reviewed.jsonl
python -m rag_baseline.benchmark export-review --items data/benchmark/trial_v3/reviewed.jsonl --output data/benchmark/trial_v3/review_after_import
```

审核导入不会覆盖已有历史文件；后续修改使用新文件名，确认后再整理为各批次的 `reviewed.jsonl`。导入后的修改项重新进行原文验证，未经完整人工勾选不能标为 approved。

## 配额、补题与冻结

新版不使用混合维度的 `question_type`。历史标签保存在来源记录及 `generation_targets` 中，不能据此推定最终标签。

| 字段 | 取值及含义 |
|---|---|
| `answer_form` | 可多选 `fact`、`condition`、`procedure`、`comparison`、`synthesis` |
| `evidence_span` | `single_unit`：一个完整条款、枚举或表格单元；`within_document`：同文档多个必要单元；`cross_document`：跨文档；`none`：无答案证据 |
| `expected_behavior` | `answer`、`abstain`、`correct_premise`、`clarify` |
| `difficulty` | 普通题 `easy/medium/hard`，边界题 JSON 为 `null`、CSV 留空；必须写 `difficulty_reason` |

硬约束保留总数 300（240 dev / 60 holdout）、政策/技术各 150（各集合 120/30）、三类边界行为各 10（8/2）。**点名文件是上限而非配额：dev 至多 24 题，holdout 至多 6 题，包含边界题。**证据分布、答案形式和难度只作候选覆盖目标和评测切片，不再作为冻结的等式约束。

首次试审后再扩展候选。不同批次名会改变确定性抽样，避免每批重复抽相同节点。通过 `--target-span`、`--target-form` 和 `--target-category/difficulty/style/behavior` 指定候选目标；旧 `--target-type` 只保留为抽样兼容选项。最终标签由实际题目及证据确定，不能按序号分配。默认自然风格，不凑文件点名上限。

```powershell
python scripts/generate_benchmark.py --output data/benchmark/batch_001 --size 60 --full-batch --reviewed data/benchmark/trial_v2/reviewed.jsonl
python scripts/generate_benchmark.py --output data/benchmark/batch_001 --size 60 --full-batch --reviewed data/benchmark/trial_v2/reviewed.jsonl --retry-failed
```

恢复时参数、语料、依赖、提示词、schema、证据审核策略及实现文件指纹必须一致，否则新建批次目录。KG 节点只是起点，标注上下文扩展同条枚举、邻接断句和表格表头，引用仍指向原始 block 和字符区间。成功题不会重复调用；初稿、标注、修订和语义预审分别保存。失败保留历史，接口重试计入 HTTP 调用日志。已导出的审核表不会被恢复过程覆盖。

```powershell
python -m rag_baseline.benchmark export-review --items data/benchmark/batch_001/candidates.jsonl --output data/benchmark/batch_001/review_complete
python -m rag_baseline.benchmark pool --batches data/benchmark/trial_v2 data/benchmark/batch_001 --output data/benchmark/pool_001
```

每个来源批次均须已有 `reviewed.jsonl`。批次可使用修订后的生成提示词，来源配置完整保留；语料和评委协议必须一致。`pool --candidates` 可合并待审核候选；`--screened-only` 只在候选合并时使用，排除有明确反证的边界题并留下排除清单。

冻结前审核 `families.json`：核对 `documents` 与 `suspected_pairs`，修订 `family_by_doc`；代表文档必须映射到自身。重复发布的文档归入同一族。无法提供有效独立内容的文档族可以在 `coverage_exclusions` 中写明排除原因，不能无理由漏掉语料。确认后填写 `review_status=approved` 和 `reviewer`。

```powershell
python -m rag_baseline.benchmark freeze --items data/benchmark/pool_001/reviewed.jsonl --manifest data/benchmark/pool_001/manifest.json --families data/benchmark/pool_001/families.json --output data/benchmark/benchmark_v1
```

冻结使用整数规划，满足总量、领域、边界行为、文件提示上限、文档族覆盖和去重约束；不满足时明确失败并继续补题，不用改标签解决。每组重复问题最多选一个代表。试审题及其已知近重复题不能进入 holdout。语义去重仍依赖人工 `duplicate_group`，自动字符相似不能发现所有同义改写。

v1 的读取、校验与历史评测保持兼容；v2 批次禁止与 v1 混池或混合冻结。v1 历史规则不会悄悄变成 v2 规则；迁移必须产出带来源的新候选并重新审核。

发布目录保存 `gold.jsonl`、语料快照、文档族和版本清单。禁止覆盖已有发布目录；评测时核验文件哈希。原始PDF/DOCX不复制，发布保存对应原文哈希和解析文本；不代表重新解析任意版本都能重现同样结果。

## 金标准及运行接口

证据与 chunk ID 解耦，以 `doc_id + document_sha256 + block_id + block_char_start/end` 定位固定解析块，区间左闭右开。页码或 DOCX 内容位置保存在 `location`。重新切分应保留这些来源映射；重新解析导致 block 改变时，需显式映射或升级数据版本。

`evidence_groups` 的组之间为 AND；一个组的 `alternatives` 之间为 OR；每个 alternative 内的片段为 AND。例如：

```json
[{"id":"g1","alternatives":[["片段A","片段B"],["等价片段C"]]}]
```

实际片段是含文本和来源的对象。生成上下文不是全部相关证据；未标注的召回文本不自动算错误，因此检索不计算“不在金标准即不相关”的精确率。

运行结果为 UTF-8 JSONL，每道题一行：

```json
{
  "id": "题目ID",
  "response": "实际回答……",
  "retrieved_contexts": [{
    "id": "ctx-1",
    "text": "实际送入回答模型的检索文本",
    "doc_id": "固定文档ID",
    "document_sha256": "原文SHA256",
    "sources": [{"block_id": 12,"block_char_start": 0,"block_char_end": 40}]
  }],
  "citations": [{"claim":"回答中的原文主张","context_ids":["ctx-1"]}],
  "run_config": {"pipeline_version":"baseline-001","top_k":10,"embedding":"...","reranker":"...","answer_model":"...","prompt_sha256":"..."},
  "error": null
}
```

上下文按排序后的实际顺序提供，不得用金标准文本冒充检索结果。`sources` 可省略，此时仅进行同一文档哈希下的完整证据文本匹配；跨新 chunk 合并证据需要来源区间。区间内容必须真的出现在对应检索文本中。回答错误用 `error` 字符串记录，无引用时用空数组，不假造引用关系。

## 评测与稳定性

```powershell
python -m rag_baseline.benchmark evaluate --release data/benchmark/benchmark_v1 --runs tmp/run_001.jsonl --output data/benchmark/evals/run_001 --retrieval-only
python -m rag_baseline.benchmark evaluate --release data/benchmark/benchmark_v1 --runs tmp/run_001.jsonl --output data/benchmark/evals/run_001_full
python -m rag_baseline.benchmark export-ragas --release data/benchmark/benchmark_v1 --runs tmp/run_001.jsonl --output tmp/ragas_run_001.jsonl
python -m rag_baseline.benchmark compare --left data/benchmark/evals/run_001_full --right data/benchmark/evals/run_002_full --output tmp/comparison.json
```

默认只评 dev。重要版本显式 `--split holdout`，不在日常调优中查看或迭代其失败题。每轮保存全部逐题输出，配置变化须用新目录；失败重试使用 `--retry-failed`。

指标分开报告，无单一总分：

- `evidence_recall@5/@10`：必要证据组命中比例；`all_evidence@5/@10`：是否找齐全部组。
- `answer_point_accuracy`：已尝试回答的必要要点中，正确计1、部分正确计0.5、错误计0；未涉及不进该分母，全未涉及记0。
- `answer_point_completeness`：上述正确得分除以全部必要要点数；遗漏记0。
- `faithfulness`：Ragas 0.4.3 `metrics.collections.Faithfulness`。无上下文或无法评分时显式保留失败/不可用状态，不伪造数值。
- `citation_support`：被明确引用的主张中，有有效引用且证据支持的比例；无引用记0。`citation_coverage`：回答事实主张中被有效引用支持的比例。
- `boundary_behavior_accuracy`：边界行为是否正确；三类分别报告，不混入普通召回率。

每项给出 `mean_scored`、`scored_count`、`eligible_count`、`completion_rate`，并保留 missing/run_error/judge_error。均值是成功评分样本的均值，必须结合完成率读，失败不能静默抹掉。少于30题的切片标记为仅诊断。

配对比较只允许相同题集、评委、评分协议和依赖，报告每题差值的 bootstrap 95% 区间及未配对ID；区间以当前固定题集为条件，问题相关性可能使它偏乐观，不代表线上真实流量置信区间。

评委启动先验证结构化输出，关闭联网及思考，temperature=0，不降级到其他模型。固定参数不保证完全确定性。在线评分前应先完成人工校准：

```powershell
python -m rag_baseline.benchmark calibration-template --release data/benchmark/benchmark_v1 --output tmp/calibration.jsonl
# 人工补齐90个回答变体、引用和human_metrics，并逐条填写human_reviewer、review_status=approved
python -m rag_baseline.benchmark calibrate --release data/benchmark/benchmark_v1 --labels tmp/calibration.jsonl --output data/benchmark/calibration_v1
```

校准从 dev 固定取30题，每题正确/部分正确/错误三个版本。模板里的参考答案只是草稿，不能跳过人工标注。三个独立评分轮次不使用跨轮LLM缓存；恢复已完成轮次只复用该轮检查点。报告每个指标相对人工标签的误差、三次范围和失败数量；差异需人工分析，不自动认定评委可靠。

更换评委、评分提示词或依赖后，重评旧基线。纠正已冻结的标注必须发布新版本并同步重评，新语料不改写 v1。

## 验证

```powershell
python -m unittest discover -s tests -v
```

测试覆盖原文错配、切分后区间拼接和替代证据、审核确认与陈旧表格、配额/去重/留出约束、冻结完整性、生成恢复、HTTP限流重试与用量、评分缺失和引用校验。测试使用模拟接口，不需要真实密钥。

## 质量改进的小步验证

30题修订历史的缺陷回填、真实回归命令与10场景实验见 [最小质量闭环](benchmark-quality-loop.md)。这批实验只作dev诊断，未推广试验提示词，也不计入300题配额。
