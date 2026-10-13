---
name: clean-artifacts
description: 清理 benchmark / 评测运行留下的可重建产物（dataset_snapshot 副本、泄漏的临时目录、过期探针目录、调试 payload 转储、原子写残留），回收磁盘空间。默认 dry-run 只报告；用户要真正删除时才加 --apply。每轮评测或冒烟测试结束后调用。
---

# clean-artifacts

清理一次评测/冒烟运行后堆积的可重建产物。**默认 dry-run**，只报告不删除；只有用户明确要求「执行 / apply / 真的删」时才删除。

## 硬性安全规则

1. **绝不删除 git 已跟踪的文件。** 每个待删目标先跑
   `git ls-files --error-unmatch <path>`（或 `git check-ignore -q <path>`），
   命中即跳过并在报告里标 `SKIP (tracked)`。
2. 只删下面白名单里的模式，不做任何通配之外的删除。
3. 删除前把完整待删清单打印给用户确认，再执行。
4. `dataset_snapshot/` 只有在确认与 release 同 SHA 时才能删：
   ```bash
   sha256sum data/benchmark/<release>/gold.jsonl data/benchmark/<release>/full_runs/*/dataset_snapshot/gold.jsonl
   ```
   任一不同 → 该份跳过。

## 步骤

### 1. 扫描

```bash
# 未被忽略的产物（应接近 0；不为 0 说明 .gitignore 漏了规则）
git status --porcelain -uall | grep '^??'

# 各产物体积
du -sh data/benchmark/*/full_runs tmp 2>/dev/null
du -sh tmp/* 2>/dev/null | sort -rh | head -30

# 运行产物文件数
find data/benchmark/*/full_runs -type f 2>/dev/null | wc -l
```

### 2. 收集待删目标（白名单）

| 模式 | 说明 |
|---|---|
| `data/benchmark/*/full_runs/*/dataset_snapshot/` | release 文件的冗余副本（校验 SHA 后再删） |
| `data/benchmark/*/full_runs/*/outbound_payloads_*.jsonl` | 调试用请求体转储 |
| `data/benchmark/*/full_runs/*/outbound_full150_review.json` | 同上 |
| `tmp/tmp????????/` | 泄漏的 ad-hoc `mkdtemp` 临时目录 |
| `tmp/*probe_*/` | 一次性探针产物（如 `lexical_probe_*`、`term_probe_*`） |
| `tmp/*.log`（除最近一轮 benchmark 日志外） | 旧运行日志 |
| 全仓库 `*.tmp` | `core.write_json/write_jsonl` 的原子写残留 |

对每个候选，先算 `du -sh` 累计，并跑 `git ls-files --error-unmatch` 排除已跟踪项。

### 3. 报告（dry-run）

输出一张表：路径 / 体积 / 判定（`DELETE` / `SKIP (tracked)` / `SKIP (sha mismatch)`），末尾给出「预计回收 X MB」。**到此为止，等用户确认。**

### 4. 执行（仅当用户要求 apply）

```bash
rm -rf <确认过的路径>
```

删完复查：

```bash
du -sh data/benchmark/*/full_runs tmp
find data/benchmark/*/full_runs -type f | wc -l
git status --porcelain -uall | wc -l   # 应仍接近 0
```

## 不要碰

- `tmp/benchmark_cache/` —— 真实向量缓存，重跑会复用，加快了后续评测。
- `data/benchmark/*/full_runs/<run>/` 里的 `README.md`、`report.json`、`execution.json`、`run_manifest.json`、`verification.json`、`artifacts_sha256.json` —— 是 provenance 记录；被其它运行的 `execution.json` 引用时尤其不能删（例如 004 引用了 001/003）。
- `data/benchmark/*/pilot/`、`review/`、`dev/` —— 已跟踪的题库归档。
- `data/benchmark/*/review_provenance/`、`revision_history/` —— 已被忽略但属审核/历史归档，除非用户点名否则不动。

## 先例

- `20261013_002` 是自述「诊断过程」且未被 004 引用的一轮，曾降级为只留元数据（`README.md`/`report.json`/`evaluation/report.json`/`execution.json`/`run_manifest.json`/`verification.json`/`usage_summary.json`/`artifacts_sha256.json`）。降级整轮 run 需要用户明确同意，不属于默认白名单。
