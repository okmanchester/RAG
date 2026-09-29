# data/benchmark 目录地图

本目录保存固定 RAG Benchmark 从语料到冻结发布的全部产物。**目录名即路径契约**：多个脚本和
manifest 的 `source_batch` 字段硬引用这里的目录名，不要重命名或搬移已有目录。

当前状态：完整150题已冻结为 `gold150_final_v1/`，政策/技术各75题；
调优集 `dev/` 为120题（各60题），留出集 `holdout/` 为30题（各15题）。
新版本是当前唯一完整评测入口，旧30题 `trial30_final_v1/` 和56题 `batch56_final_v1/`
保持原冻结字节作为历史发布，不与新版本重复累计。下面批次过程的“未冻结”等状态为历史记录。
Git仅追踪v1的11个核心文件；完整审核和形成历史继续在本地保留。默认加载只校验核心，完整历史校验需显式 `--include-history`。
新64题待审集位于 `batch_002_review64/`（政策、技术各32题），原始生成记录在 `batch_002_64/`
及 `batch_002_supplement_01/`。全部待人工审核，通过后才计入金标准；新的dev/holdout划分尚未确定。
流程说明见 `docs/benchmark.md`。

这64题已完成逐题AI审核：建议通过12题、需修订42题、建议排除10题。用户随后明确授权修订42题，
结果追加在 `batch_002_review64/revision_v2/`，42题均通过本轮授权修订自检，未冻结。
最新修订阅读入口为 `revision_v2/review_after_import/reading.md`，逐字段差异在 `revision_v2/revisions.jsonl`。
`revision_v2/full_batch64.jsonl` 合入42题，另外22题保持原对象；10道建议排除题仍不可选入最终池。
本轮 `approved` 表示Codex授权修订自检通过，不冒称用户逐题审阅或独立人工复审。

用户随后委托不使用ragas补齐剩余10题，结果在 `batch_002_authored10/`：政策4题、技术6题，
直接按固定文档编写并完成同一agent自检，无ragas导入或外部模型调用。
新增阅读入口为 `batch_002_authored10/review_after_import/reading.md`。
`batch_002_authored10/pool150.jsonl` 合并旧86题、审核保留12题、修订42题和新10题，政策/技术各75题；
不是冻结发布。保留12题源对象的pending及agent_review.approved，不擅自更改历史状态；原10道排除题未选入。
新10题与参考140题无自动重复链、无共享证据block，并按核心任务自检；现有冻结金标准仍86题。

新增10题随后完成第二轮原文复审，最新入口在 `batch_002_authored10/review_v2/`：
4题原内容保留、2题实质修订、4题标签/评分/措辞优化，修订后10题均approved。
主要修正敏感信息题的同意处理前提、补齐网购退货实施条例规则；该退货题改为cross_document。
最新题稿是 `review_v2/review_after_import/reading.md`，逐题报告在 `review_v2/report.md`，
前后差异在 `review_v2/revisions.jsonl`；`review_v2/pool150.jsonl`替换末10题，原140对象保持不变。
首版文件保留。本轮仍为同一Codex agent复审，未使用ragas，未冻结。

## 一、三层结构

| 层 | 目录 | 性质 | 入库 | 可删？ |
|---|---|---|---|---|
| 上游 | `knowledge_graph*.json`、`kg_build/`、`ragas_cache/` | 中间产物 | 否 | 可重建 |
| 链路 | `pilot_*/`、`trial_v1..v3/` | 历史批次 | 否（本地留存） | **不可删** |
| 发布 | `trial30_final_v1/`、`batch56_final_v1/` | 冻结金标准，共86题 | **是** | 不可删 |

## 二、上游：语料与知识图谱

| 路径 | 说明 |
|---|---|
| `../../data/raw/` | 原始 PDF/DOCX，只读上游，不在本目录 |
| `../../data/processed/` | 解析快照（`documents.jsonl` / `chunks.jsonl`） |
| `knowledge_graph.json`（74MB） | 全量 KG，由 `scripts/build_benchmark_kg.py` 产出 |
| `knowledge_graph.smoke.json`（746KB） | 冒烟用小型 KG |
| `kg_build/full/`、`kg_build/smoke/` | 构图断点（checkpoint 约 71MB），支持断点续跑 |
| `ragas_cache/cache.db` | ragas 调用缓存 |
| `_token_probe.json` | KG 构建的 token 实测探针，无代码引用 |

这一层全部可重建：从 `data/processed` 重跑 `build_benchmark_kg.py` 即可。已按 `.gitignore` 忽略。

**注意**：`knowledge_graph.json` 必须留在本目录顶层。`scripts/apply_trial_review.py` 与
`scripts/revise_benchmark_trial.py` 用 `glob('data/benchmark/*graph*')` 做保护文件校验，
移走会导致校验失败。

## 三、链路：从原生 ragas 到定稿的完整修改过程

按时间顺序，每一批的来源记录在其 `manifest.json` 的 `source_batch` 字段里。

| 目录 | 阶段 | 关键文件 |
|---|---|---|
| `pilot_v1/` | **原生 ragas 首次生成**（未修正） | `drafts/*.json` 30 条原始初稿；`candidates.jsonl` 过守卫后；`summary.json` token 账 |
| `pilot_v2/` | 改提示词后重新生成 | `candidates.jsonl` 29 条；`failures.json` 1 题失败 |
| `pilot_v2_supplement/` | 补题 | `candidates.jsonl` 1 条 |
| `pilot_review_v1/` | 对 `pilot_v2` 的语义预审 | `candidates_checked.jsonl`（题面与 v2 重合 29/30） |
| `pilot_boundary_abstain/` | 边界题候选：拒答 | `candidates.jsonl` 1 条 |
| `pilot_boundary_authored/` | 边界题候选：人工/agent 编写 | `candidates.jsonl` 2 条 |
| `pilot_boundary_clarify/` | 边界题候选：澄清 | `candidates.jsonl` 1 条 |
| `trial_v1/` | 首批试审集 | `machine_excluded.jsonl` 机器剔除 2 题；`review/review.csv` |
| `trial_v2/` | 修订试审集 v2 | `revisions.jsonl` 逐题修订说明；`review/evidence_audit.jsonl` 证据台账 |
| `trial_v3/` | v3 已审核 | `reviewed.jsonl` 定稿记录；`finalization_audit.jsonl` 逐题依据；`revisions.jsonl` 7 处修订；`submitted_review.csv` 收到的审核表原件 |
| `batch_001/`、`batch_001_supplement_*/` | 新60题生成及补题历史 | 原始场景、初稿、失败、证据及调用记录 |
| `batch_001_review60/` | 候选归并、首次审核及v2修订 | `revision_v2/approved.jsonl`含本次冻结的56题；另4题重复排除 |
| `batch_002_64/`、`batch_002_supplement_01/` | 新64题原始生成及1题补题 | 原始任务、初稿、标注、机器预审和调用记录；65题全部生成成功 |
| `batch_002_review64/` | 下一批人工待审集 | 排除1道旧题同核心重复后64题；全部pending；`review/reading.md`、`review/review.csv`及去重线索 |
| `batch_002_review64/agent_review_v1/` | 原64题逐题AI审核 | 建议通过12、需修订42、建议排除10；人工状态未改变 |
| `batch_002_review64/revision_v2/` | 用户授权42题修订及自检 | 42题approved自检候选；`review_after_import/reading.md`、`revisions.jsonl`及整批合并视图；未冻结 |
| `batch_002_authored10/` | 用户委托直接按文档补齐10题 | 政策4/技术6，未使用ragas；`approved.jsonl`、逐题自检依据及`pool150.jsonl`合并阅读池；未冻结 |
| `batch_002_authored10/review_v2/` | 新10题第二轮原文复审及修订 | 4题保留、2题实质修订、4题优化，最终10题approved；最新阅读稿、逐字段差异及150题池；未冻结 |

**为什么这层不可删**：它是「原生 ragas 生成 → 人工 + AI 审核」的唯一证据链。

- `pilot_v1/drafts/` 是唯一一份未经任何修正的原始产出，是缺陷样本的来源；
  可作为独立缺陷来源；与最终题配对须核对真实来源，不能按相同序号配对。
- `trial_v2` 与 `trial_v3` 的 `revisions.jsonl` 记录了每处修订的理由，是缺陷分类与
  提示词改进的依据。
- `trial30_final_v1/manifest.json` 的 `source_batch` 指向 `data\benchmark\trial_v2`，
  移走会让发布集的溯源链悬空。

> ⚠️ 这层已在 `.gitignore` 中，`git clean -fdx` 会**永久删除**它们（`pilot_v1/drafts/`
> 是 API 调用的产物，不可从代码重建）。如果希望改为入库，删掉 `.gitignore` 里对应的
> 那几行即可。

## 四、发布：完整150题与历史金标准

| 版本 | 题数 | 划分 | 状态 |
|---|---|---|---|
| `gold150_final_v1/` | 150（政策75、技术75） | dev120 / holdout30 | 当前完整金标准；全池确定性分层划分 |
| `trial30_final_v1/` | 30 | 全dev | 保持原冻结文件字节 |
| `batch56_final_v1/` | 56（政策28、技术28） | 全dev | 用户授权冻结；与旧30题去重后独立发布 |

| 文件 | 说明 |
|---|---|
| `gold.jsonl` | 各版本的金标准（题面、证据、答案要点和标签） |
| `queries.jsonl` | **只把这份交给被测系统** |
| `run_template.jsonl` | 运行结果模板，默认 `error=not_run`，**不代表系统已运行** |
| `documents.jsonl`、`chunks.jsonl` | 固定语料快照（约 5.4MB），评测复现所需 |
| `families.json` | 文档族，去重与覆盖约束用 |
| `manifest.json` | 版本清单：语料哈希、评委配置、来源批次、保护文件哈希 |
| `review/review.csv` | 填写完成的审核表，全部 `approved` |
| `review/reading.md` | 人工阅读入口（后续阅读以这份为准） |
| 新版 `review_provenance/` | 56题冻结前输入、逐题变更、复核意见处理、重复关系、边界核查和历史来源清单 |

发布目录禁止覆盖，任何修订都应发布新版本。评测时核验文件哈希。
`.gitattributes` 禁止Git对冻结目录转换换行，使Windows及其他平台检出后保持相同冻结字节。

完整150题根目录的 `gold.jsonl` 是唯一全量金标准，`dev/` 和 `holdout/` 各有对应gold、queries和run_template。
评测 `--release` 指向 `gold150_final_v1/` 根目录，默认dev；留出集需显式 `--split holdout`。
根目录 `split.json` 记录固定种子20260929、最优分层误差、共享证据题组和全部ID归属。
`review_provenance/` 归档原始150题对象、12题审核接纳台账及按原路径组织的审核来源，索引为 `artifact_index.json`。
`review/`、`review_provenance/`及 `dev/gold.jsonl`、`holdout/gold.jsonl` 副本仅本地保存；分集金标准从完整gold的split字段选取。仅检出Git核心文件即可评测，无需下载审核历史。
30题留出集中20题为旧公开题，10题来自尚未调参的新增64题；清单保留原pilot、历史划分及暴露来源。
从本次冻结起留出集停止参与调参，不声称从未见过的盲测集。现有题池证据覆盖31/32个文档族，缺口如实记录，不改题补配额。

## 五、常用入口

```powershell
conda activate rag

# 全量测试
python -m unittest discover -s tests -v

# 校验发布集
python -m rag_baseline.benchmark verify-release --release data/benchmark/gold150_final_v1
# 本地完整历史另行校验（需全部144个原冻结文件）
python -m rag_baseline.benchmark verify-release --release data/benchmark/gold150_final_v1 --include-history
python -m rag_baseline.benchmark validate --items data/benchmark/gold150_final_v1/gold.jsonl --corpus data/benchmark/gold150_final_v1
python -m rag_baseline.benchmark validate --items data/benchmark/trial30_final_v1/gold.jsonl
python -m rag_baseline.benchmark validate --items data/benchmark/batch56_final_v1/gold.jsonl

# 评测（默认只评 dev）
python -m rag_baseline.benchmark evaluate --release data/benchmark/gold150_final_v1 --runs tmp/dev_runs.jsonl --output tmp/dev_eval
python -m rag_baseline.benchmark evaluate --release data/benchmark/gold150_final_v1 --runs tmp/holdout_runs.jsonl --output tmp/holdout_eval --split holdout

# 下一批生成（注意：不同批次名会改变确定性抽样）
python scripts/generate_benchmark.py --output data/benchmark/batch_001 --size 60 --full-batch --reviewed data/benchmark/trial_v3/reviewed.jsonl
```

## 六、版本控制策略

`.gitignore` 的划分依据就是上面的三层结构：

- **入库**：本文件及v1的11个核心文件：根目录README、gold、documents、chunks、families、manifest、split，以及dev/holdout各自的queries和run_template。
- **仅本地保存**：v1的 `review/`、`review_provenance/`、分集gold副本；KG、缓存、生成/补题/审核/修订/实验历史、旧发布、未来版本及临时文件同样忽略。

`.gitignore` 使用明确核心文件白名单。原始manifest仍记录完整144文件的哈希，文件和题目归属均未改动。`load_release` 对gold150默认验证核心，`verify_history=True`（CLI为 `--include-history`）验证完整历史；其他类型发布仍沿用原有完整校验。新版本需显式添加核心白名单。`.gitattributes` 保留当前v1原始字节，避免自动换行转换改变冻结哈希。

完整本地历史应单独备份；`git clean -fdx` 会删除这些被忽略的档案。新检出环境运行核心测试；需要本地原始审核资料的测试在资料缺失时标记跳过。

`.gitignore` 中每一段都带注释说明归属与理由，调整策略时按段增删即可。

## 七、最小质量闭环

`quality_loop_v1/` 保存5个历史场景与5个新场景的20次前后对照，不是正式题集。结果见 [实验记录](quality_loop_v1/README.md)，缺陷回填与使用方法见 [复盘](../../docs/benchmark-quality-loop.md)。提示词v5未证明整体改善，默认保持v4；仅保留真实验证的无序列表上下文修复。11个历史案例和1个新发现的列表输入随测试夹具入库，无需本地历史目录即可跑回归。
