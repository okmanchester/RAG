# data/benchmark 目录地图

本目录保存固定 RAG Benchmark 从语料到冻结发布的全部产物。**目录名即路径契约**：多个脚本和
manifest 的 `source_batch` 字段硬引用这里的目录名，不要重命名或搬移已有目录。

当前状态：30 题试审集已定稿，发布在 `trial30_final_v1/`（全部 dev）。长期目标 300 题
（240 dev + 60 holdout）尚未开始。流程说明见 `docs/benchmark.md`。

## 一、三层结构

| 层 | 目录 | 性质 | 入库 | 可删？ |
|---|---|---|---|---|
| 上游 | `knowledge_graph*.json`、`kg_build/`、`ragas_cache/` | 中间产物 | 否 | 可重建 |
| 链路 | `pilot_*/`、`trial_v1..v3/` | 历史批次 | 否（本地留存） | **不可删** |
| 发布 | `trial30_final_v1/` | ★冻结发布 | **是** | 不可删 |

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

## 四、发布：`trial30_final_v1/`（★ 唯一入库内容）

| 文件 | 说明 |
|---|---|
| `gold.jsonl` | 30 题金标准（`queries.jsonl` + 证据 + 答案要点 + 标签） |
| `queries.jsonl` | **只把这份交给被测系统** |
| `run_template.jsonl` | 运行结果模板，默认 `error=not_run`，**不代表系统已运行** |
| `documents.jsonl`、`chunks.jsonl` | 固定语料快照（约 5.4MB），评测复现所需 |
| `families.json` | 文档族，去重与覆盖约束用 |
| `manifest.json` | 版本清单：语料哈希、评委配置、来源批次、保护文件哈希 |
| `review/review.csv` | 填写完成的审核表（30 题 `approved`） |
| `review/reading.md` | 人工阅读入口（后续阅读以这份为准） |

发布目录禁止覆盖，任何修订都应发布新版本。评测时核验文件哈希。

## 五、常用入口

```powershell
conda activate rag

# 全量测试
python -m unittest discover -s tests -v

# 校验发布集
python -m rag_baseline.benchmark validate --items data/benchmark/trial30_final_v1/gold.jsonl

# 评测（默认只评 dev）
python -m rag_baseline.benchmark evaluate --release data/benchmark/trial30_final_v1 --runs tmp/run_001.jsonl --output tmp/eval_001

# 下一批生成（注意：不同批次名会改变确定性抽样）
python scripts/generate_benchmark.py --output data/benchmark/batch_001 --size 60 --full-batch --reviewed data/benchmark/trial_v3/reviewed.jsonl
```

## 六、版本控制策略

`.gitignore` 的划分依据就是上面的三层结构：

- **入库**：`trial30_final_v1/`（发布集）、`quality_loop_v1/`（小批质量实验）+ 本文件。
- **忽略**：上游中间产物（可重建）、链路历史批次（本地留存）。

`.gitignore` 中每一段都带注释说明归属与理由，调整策略时按段增删即可。

## 七、最小质量闭环

`quality_loop_v1/` 保存5个历史场景与5个新场景的20次前后对照，不是正式题集。结果见 [实验记录](quality_loop_v1/README.md)，缺陷回填与使用方法见 [复盘](../../docs/benchmark-quality-loop.md)。提示词v5未证明整体改善，默认保持v4；仅保留真实验证的无序列表上下文修复。11个历史案例和1个新发现的列表输入随测试夹具入库，无需本地历史目录即可跑回归。
