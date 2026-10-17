"""Isolated MySQL ngram / ES retrieval benchmark; no LLM or production writes."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import random
import re
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from elasticsearch.helpers import scan
from sqlalchemy import text

from rag.benchmark.core import Corpus, file_hash, load_release, now, write_json, write_jsonl
from rag.benchmark.evaluation import retrieval_metrics, summarize_scores
from rag.benchmark.first_run import DenseIndex
from rag.management.config import Settings
from rag.management.errors import public_error
from rag.search.config import SearchSettings
from rag.search.elastic import ESStore, rrf
from rag.search.shared import SearchFilters, context, digest, query_embedding
from scripts.backup_knowledge_base import container_sql, database_name

MODES = ("mysql_default", "mysql_no_stopwords", "es_bm25", "es_hybrid")
METRICS = ("evidence_recall@5", "evidence_recall@10", "all_evidence@5", "all_evidence@10")
SEED = 20261016


def require(condition, message):
    if not condition:
        raise ValueError(message)


def output_path(path):
    path = Path(path).resolve()
    allowed = path.is_relative_to(ROOT / "tmp") or (
        path.is_relative_to(ROOT / "data/benchmark") and
        "full_runs" in path.relative_to(ROOT / "data/benchmark").parts)
    require(allowed and subprocess.run(
        ["git", "check-ignore", "-q", str(path)], cwd=ROOT).returncode == 0,
        "output must be an ignored tmp/ or benchmark full_runs/ directory")
    require(not path.exists(), "output already exists")
    return path


def release_hashes(release):
    return {p.relative_to(release).as_posix(): file_hash(p) for p in sorted(release.rglob("*"))
            if p.is_file() and "full_runs" not in p.relative_to(release).parts}


def table_name(name):
    require(name in {"corpus_default", "corpus_no_stopwords", "smoke_default", "smoke_no_stopwords"},
            "table outside benchmark allowlist")
    return name


def mysql_statement(table):
    table = table_name(table)
    return text(f"SELECT id, legacy_chunk_id, MATCH(text) AGAINST(:question IN NATURAL LANGUAGE MODE) AS score "
                f"FROM `{table}` WHERE MATCH(text) AGAINST(:question IN NATURAL LANGUAGE MODE) > 0 "
                "ORDER BY score DESC, legacy_chunk_id ASC, id ASC LIMIT :size")


def mysql_search(connection, table, question, size=10):
    rows = connection.execute(mysql_statement(table), {"question": question, "size": size}).mappings()
    return [{"_id": str(row["id"]), "_score": float(row["score"]),
             "_source": {"legacy_chunk_id": row["legacy_chunk_id"]}} for row in rows]


def build_mysql(connection, table, rows, stopwords):
    table = table_name(table)
    connection.execute(text(f"CREATE TABLE `{table}` (id BIGINT UNSIGNED PRIMARY KEY, "
                            "legacy_chunk_id VARCHAR(128) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE, "
                            "text LONGTEXT NOT NULL) ENGINE=InnoDB "
                            "DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci"))
    connection.execute(text(f"INSERT INTO `{table}` (id,legacy_chunk_id,text) VALUES (:id,:legacy_chunk_id,:text)"), rows)
    connection.commit()  # InnoDB FULLTEXT only sees committed data.
    connection.execute(text(f"SET SESSION innodb_ft_enable_stopword = {int(stopwords)}"))
    connection.execute(text(f"ALTER TABLE `{table}` ADD FULLTEXT INDEX ft_text (text) WITH PARSER ngram"))
    connection.commit()
    saved = connection.execute(text(f"SELECT id, legacy_chunk_id, text FROM `{table}` ORDER BY id")).mappings()
    require([dict(row) for row in saved] == rows, "MySQL corpus round-trip mismatch")
    plan = [dict(row) for row in connection.execute(text("EXPLAIN " + str(mysql_statement(table))),
                                                   {"question": "指标更新频率", "size": 10}).mappings()]
    require(any(row.get("type") == "fulltext" and row.get("key") == "ft_text" for row in plan),
            "MySQL query does not use FULLTEXT")
    config = dict(connection.execute(text(
        "SELECT @@session.innodb_ft_enable_stopword AS stopwords_enabled, "
        "@@global.innodb_ft_server_stopword_table AS server_stopword_table, "
        "@@session.innodb_ft_user_stopword_table AS user_stopword_table")).mappings().one())
    require(bool(config["stopwords_enabled"]) == bool(stopwords), "stopword setting not applied")
    require(not config["server_stopword_table"] and not config["user_stopword_table"],
            "custom stopword tables require an explicitly revised benchmark configuration")
    return {"configuration_at_index_creation": config, "explain": plan,
            "ddl": connection.execute(text(f"SHOW CREATE TABLE `{table}`")).one()[1]}


def validate_hits(hits, by_id):
    ids = [hit["_id"] for hit in hits]
    require(len(ids) == len(set(ids)), "duplicate returned chunk")
    require(all(identifier in by_id and h["_source"]["legacy_chunk_id"] == by_id[identifier]["chunk_id"]
                and np.isfinite(h["_score"]) for identifier, h in zip(ids, hits)), "invalid returned chunk identity/score")


def percentiles(values):
    require(bool(values) and all(np.isfinite(v) and v >= 0 for v in values), "invalid latency samples")
    return {"samples": len(values), "median_ms": float(np.median(values)),
            "p95_ms": float(np.percentile(values, 95))}


def pairwise(items, records):
    pairs = {}
    for left, right in (("mysql_default", "es_bm25"), ("mysql_no_stopwords", "es_bm25"),
                        ("mysql_no_stopwords", "mysql_default"), ("mysql_default", "es_hybrid"),
                        ("mysql_no_stopwords", "es_hybrid")):
        rows = []
        for item, a, b in zip(items, records[left], records[right]):
            if item["expected_behavior"] != "answer":
                continue
            delta = {key: a["metrics"][key] - b["metrics"][key] for key in METRICS}
            rows.append({"id": item["id"], "question": item["user_input"], "category": item["category"],
                         "delta_left_minus_right": delta,
                         "left_top10": [h["chunk_id"] for h in a["hits"]],
                         "right_top10": [h["chunk_id"] for h in b["hits"]],
                         "evidence_locations": [e["location"] for g in item["evidence_groups"]
                                                for alt in g["alternatives"] for e in alt]})
        key = lambda row: tuple(row["delta_left_minus_right"][m] for m in
                                ("evidence_recall@10", "all_evidence@10", "evidence_recall@5", "all_evidence@5"))
        wins = {m: {"left_wins": sum(r["delta_left_minus_right"][m] > 1e-12 for r in rows),
                    "ties": sum(abs(r["delta_left_minus_right"][m]) <= 1e-12 for r in rows),
                    "right_wins": sum(r["delta_left_minus_right"][m] < -1e-12 for r in rows)} for m in METRICS}
        pairs[f"{left}_vs_{right}"] = {"metrics": wins,
            "largest_left_advantages": sorted((r for r in rows if key(r) > (0.,)*4), key=key, reverse=True)[:5],
            "largest_right_advantages": sorted((r for r in rows if key(r) < (0.,)*4), key=key)[:5]}
    return pairs


def markdown_report(report):
    identity = report["identity"]
    lines = ["# MySQL FULLTEXT 与 Elasticsearch 实测", "",
             f"- 时间：{identity['started_at']}；MySQL {identity['mysql']['version']}；ES {identity['es_version']}。",
             f"- 语料：{identity['document_count']} 份文档、{identity['chunk_count']} 个有效切块；"
             f"{identity['question_count']} 题，其中 {identity['answer_count']} 题参与质量评分。",
             "- 全部为开发题；本轮不修改题库，不调用 LLM，不评价最终回答或生产并发性能。", "",
             "| 路径 | 证据组召回@5 | 证据组召回@10 | 全部证据命中@5 | 全部证据命中@10 |",
             "|---|---:|---:|---:|---:|"]
    for mode in MODES:
        metrics = report["reports"][mode]["overall"]["metrics"]
        lines.append("| " + mode + " | " + " | ".join(f"{metrics[m]['mean_scored']:.2%}" for m in METRICS) + " |")
    lines += ["", "## 分组", "", "| 路径／类别 | 证据组召回@5 | 证据组召回@10 | 全部证据命中@5 | 全部证据命中@10 |",
              "|---|---:|---:|---:|---:|"]
    for mode in MODES:
        for category, group in report["reports"][mode]["slices"]["category"].items():
            lines.append(f"| {mode}／{category} | " + " | ".join(f"{group['metrics'][m]['mean_scored']:.2%}" for m in METRICS) + " |")
    lines += ["", "## 延迟", "", "单并发，完整预热一轮后测三轮；单位 ms。仅检索返回候选 ID，不包含业务状态校验或来源还原。",
              "hybrid 核心耗时使用预计算查询向量；另测包含当次查询向量计算的完整耗时。两者独立统计分位数。", "",
              "| 路径 | 样本数 | median | p95 |", "|---|---:|---:|---:|"]
    for mode, timing in report["latency"].items():
        lines.append(f"| {mode} | {timing['samples']} | {timing['median_ms']:.2f} | {timing['p95_ms']:.2f} |")
    lines += ["", "## 逐题差异", "", "胜负按各指标分别统计；最大差异按召回@10、全部命中@10、召回@5、全部命中@5依次排序。"]
    for name, pair in report["pairwise"].items():
        lines += ["", f"### {name}", "", "| 指标 | 左胜 | 平 | 右胜 |", "|---|---:|---:|---:|"]
        for metric, counts in pair["metrics"].items():
            lines.append(f"| {metric} | {counts['left_wins']} | {counts['ties']} | {counts['right_wins']} |")
        for label, field in (("左侧优势最大的题", "largest_left_advantages"), ("右侧优势最大的题", "largest_right_advantages")):
            lines += ["", label + "：", ""]
            for row in pair[field]:
                question = row["question"].replace("\n", " ")
                deltas = ", ".join(f"{m}={v:+.3f}" for m, v in row["delta_left_minus_right"].items())
                lines.append(f"- {row['id']}：{question}（左减右：{deltas}）")
            if not pair[field]:
                lines.append("- 无。")
    lines += ["", "## 配置与边界", "",
              "MySQL：InnoDB、ngram=2、自然语言模式、utf8mb4_0900_ai_ci；默认停用词与关闭停用词分别建表。",
              "ES：单分片、零副本、内置 cjk、默认 BM25；向量 index=false，精确余弦；两路各50条，RRF常数60。",
              "并列按 chunk ID 排序。引擎各自的分词、停用词及评分差异属于本次方案对照，不声称分词算法已对齐。",
              "本轮隔离静态语料，不包含业务过滤、索引更新吞吐、故障恢复或线上负载测试；混合结果不能单独证明 ES 相比 MySQL 的必要性。",
              "原始问题只进入检索；金标准仅供评测评分。原始证据可按 release 路径、SHA256 和 chunk ID 回溯。", ""]
    return "\n".join(lines)


def smoke(connection, store, index):
    rows = [{"id": n, "legacy_chunk_id": f"smoke{n}", "text": value} for n, value in enumerate(
        ("知识库中文检索验证", "deviceStatus 字段含义", "重复正文并列验证", "重复正文并列验证"), 1)]
    output = {}
    for suffix, flag in (("default", True), ("no_stopwords", False)):
        table = "smoke_" + suffix
        output[table] = build_mysql(connection, table, rows, flag)
        require([h["_id"] for h in mysql_search(connection, table, "中文检索")] == ["1"], "Chinese smoke failed")
        require([h["_id"] for h in mysql_search(connection, table, "deviceStatus")] == ["2"], "field smoke failed")
        require([h["_id"] for h in mysql_search(connection, table, "重复正文")] == ["3", "4"], "tie smoke failed")
        require(not mysql_search(connection, table, "龘靐齉齾"), "no-hit smoke failed")
        mysql_search(connection, table, "' OR 1=1; DROP TABLE smoke_default; --")
        require(connection.scalar(text(f"SELECT COUNT(*) FROM `{table}`")) == 4, "parameter binding smoke failed")
    store.create(index, {"model": {"dimensions": 2}, "purpose": "mysql-es-benchmark-smoke"})
    store.bulk(index, [{"chunk_id": r["id"], "kb_id": 1, "build_id": 1,
                        "legacy_chunk_id": r["legacy_chunk_id"], "text": r["text"]} for r in rows])
    store.client.indices.refresh(index=index)
    search = lambda q: store.search(index, q, None, [1], SearchFilters(1), "bm25", 10)
    require([h["_id"] for h in search("中文检索")] == ["1"], "ES Chinese smoke failed")
    require([h["_id"] for h in search("deviceStatus")] == ["2"], "ES field smoke failed")
    require([h["_id"] for h in search("重复正文")] == ["3", "4"], "ES tie smoke failed")
    require(not search("龘靐齉齾"), "ES no-hit smoke failed")
    return {"passed": True, "mysql": output, "checks": ["Chinese", "technical field", "no hits", "tie", "SQL parameters", "FULLTEXT plan"]}


def run(args):
    release, output = Path(args.release).resolve(), output_path(args.output)
    items, _ = load_release(release, allow_evolving=args.allow_evolving)
    corpus = Corpus(release)
    original = release_hashes(release)
    settings = Settings.from_env(args.env_file)
    config = SearchSettings.from_env(args.es_env_file)
    token = uuid.uuid4().hex
    database = "rag_test_fulltext_" + token
    indexes = ["rag-test-fulltext-" + token + suffix for suffix in ("", "-smoke")]
    database_name(settings.user)
    output.mkdir(parents=True)
    identity = {"started_at": now(), "release": release.as_posix(), "release_sha256": original,
                "question_count": len(items), "answer_count": sum(i["expected_behavior"] == "answer" for i in items),
                "document_count": len(corpus.documents), "chunk_count": len(corpus.chunks),
                "database": database, "indexes": indexes, "seed": SEED, "warmup_rounds": 1, "measured_rounds": 3,
                "dataset_kind": "development", "llm_calls": 0, "metric_version": 2,
                "implementation_sha256": {p.relative_to(ROOT).as_posix(): file_hash(p) for p in
                    (Path(__file__), ROOT / "rag/benchmark/core.py", ROOT / "rag/benchmark/evaluation.py",
                     ROOT / "rag/benchmark/first_run.py", ROOT / "rag/search/shared.py", ROOT / "rag/search/elastic.py")}}
    engine = client = connection = None
    database_created = False
    cleanup = {"mysql": "not_created", "es": {}}
    status, report = "running", None
    write_json(output / "execution.json", {"status": status, **identity})
    try:
        print("[SETUP] loading verified local embedding cache/model", flush=True)
        baseline = DenseIndex(release)
        require(set(corpus.chunks) == {c["chunk_id"] for c in baseline.chunks}, "embedding corpus mismatch")
        require(baseline.vectors.shape == (len(baseline.chunks), 1024) and
                np.isfinite(baseline.vectors).all() and
                np.allclose(np.linalg.norm(baseline.vectors, axis=1), 1, atol=1e-4), "invalid embedding cache")
        identity["embedding"] = {"identity": baseline.identity,
                                 "vectors_sha256": file_hash(baseline.cache_dir / "embeddings.npy")}
        client = config.client()
        identity["es_version"] = client.info()["version"]["number"]
        store = ESStore(client, token)
        # Mark ownership before CREATE; exact UUID names make interrupted responses recoverable in finally.
        database_created = True
        container_sql(args.env_file, f"CREATE DATABASE `{database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;\n"
                      f"GRANT ALL PRIVILEGES ON `{database}`.* TO '{settings.user}'@'%';")
        engine = replace(settings, database=database).engine()
        connection = engine.connect()
        identity["mysql"] = dict(connection.execute(text(
            "SELECT VERSION() AS version, @@ngram_token_size AS ngram_token_size, "
            "@@global.innodb_ft_enable_stopword AS global_stopwords_enabled")).mappings().one())
        require(identity["mysql"]["ngram_token_size"] == 2, "this benchmark requires ngram_token_size=2")
        verification = {"smoke": smoke(connection, store, indexes[1])}
        rows = [{"id": n, "legacy_chunk_id": c["chunk_id"], "text": c["text"]}
                for n, c in enumerate(baseline.chunks, 1)]
        by_id = {str(n): c for n, c in enumerate(baseline.chunks, 1)}
        for suffix, flag in (("default", True), ("no_stopwords", False)):
            verification["mysql_" + suffix] = build_mysql(connection, "corpus_" + suffix, rows, flag)
        store.create(indexes[0], {"model": {"dimensions": 1024}, "run_id": token, "purpose": "mysql-es-benchmark"})
        documents = [{"chunk_id": row["id"], "kb_id": 1, "build_id": 1,
                      "legacy_chunk_id": row["legacy_chunk_id"], "text": row["text"], "embedding": vector.tolist()}
                     for row, vector in zip(rows, baseline.vectors)]
        require(store.bulk(indexes[0], documents) == len(rows), "ES bulk count mismatch")
        client.indices.refresh(index=indexes[0])
        # ES 9.x excludes vectors from _source by default; explicitly rehydrate for validation.
        saved_sources = {hit["_id"]: hit["_source"] for hit in scan(client, index=indexes[0],
            query={"query": {"match_all": {}}, "_source": {"exclude_vectors": False}})}
        require(set(saved_sources) == {str(doc["chunk_id"]) for doc in documents}, "ES chunk IDs differ")
        vector_errors = []
        saved = {}
        for document in documents:
            source = dict(saved_sources[str(document["chunk_id"])])
            actual = np.asarray(source.pop("embedding", []), dtype="float32")
            expected = np.asarray(document["embedding"], dtype="float32")
            require(actual.shape == expected.shape and np.isfinite(actual).all() and
                    np.allclose(actual, expected, atol=1e-7, rtol=1e-6), "ES vector round-trip mismatch")
            vector_errors.append(float(np.max(np.abs(actual - expected))))
            saved[str(document["chunk_id"])] = digest(source)
        require(saved == {str(doc["chunk_id"]): digest({k: v for k, v in doc.items() if k != "embedding"})
                          for doc in documents}, "ES text/identity round-trip mismatch")
        verification["es"] = {"round_trip_sha256": digest(saved), "chunk_count": len(saved),
                              "max_vector_abs_error": max(vector_errors),
                              "mapping": dict(client.indices.get_mapping(index=indexes[0])),
                              "settings": dict(client.indices.get_settings(index=indexes[0]))}
        write_json(output / "verification.json", verification)
        write_json(output / "execution.json", {"status": "running", **identity})
        print(f"[SETUP] verified {len(rows)} chunks; embedding all {len(items)} queries locally", flush=True)
        vectors, embedding_samples = {}, []
        for item in items:
            start = time.perf_counter()
            vectors[item["id"]] = query_embedding(baseline.encoder, item["user_input"])
            embedding_samples.append((time.perf_counter() - start) * 1000)

        def search(mode, question, vector):
            if mode.startswith("mysql_"):
                return mysql_search(connection, "corpus_" + mode.removeprefix("mysql_"), question)
            lexical = store.search(indexes[0], question, None, [1], SearchFilters(1), "bm25", 50 if mode == "es_hybrid" else 10)
            if mode == "es_bm25":
                return lexical
            dense = store.search(indexes[0], question, vector, [1], SearchFilters(1), "dense", 50)
            return rrf(dense, lexical)[:10]

        timings = {mode: [] for mode in (*MODES, "es_hybrid_with_embedding", "query_embedding")}
        records = {mode: [] for mode in MODES}
        signatures = {}
        schedule = []
        rng = random.Random(SEED)
        for round_number in range(4):
            order = list(range(len(items)))
            rng.shuffle(order)
            for n in order:
                item = items[n]
                modes = list(MODES)
                if round_number:
                    modes.append("es_hybrid_with_embedding")
                rng.shuffle(modes)
                for mode in modes:
                    start = time.perf_counter()
                    if mode == "es_hybrid_with_embedding":
                        vector = query_embedding(baseline.encoder, item["user_input"])
                        embedding_ms = (time.perf_counter() - start) * 1000
                        hits = search("es_hybrid", item["user_input"], vector)
                    else:
                        hits = search(mode, item["user_input"], vectors[item["id"]])
                    elapsed = (time.perf_counter() - start) * 1000
                    validate_hits(hits, by_id)
                    signature = [hit["_id"] for hit in hits]
                    key = (mode, item["id"])
                    if key in signatures:
                        require(signatures[key] == signature, "ranking changed between measured rounds")
                    else:
                        signatures[key] = signature
                    if round_number:
                        timings[mode].append(elapsed)
                        if mode == "es_hybrid_with_embedding":
                            timings["query_embedding"].append(embedding_ms)
                    if round_number == 0:
                        contexts = [context(by_id[h["_id"]], corpus.documents[by_id[h["_id"]]["doc_id"]]["sha256"]) for h in hits]
                        records[mode].append({"id": item["id"], "status": "complete", "hits": [
                            {"chunk_id": h["_source"]["legacy_chunk_id"], "score": h["_score"], "ranks": h.get("ranks", {})} for h in hits],
                            "metrics": retrieval_metrics(item, contexts, 2)})
                    schedule.append({"round": round_number, "id": item["id"], "mode": mode, "ms": elapsed})
            print(f"[RETRIEVAL] {'warmup' if not round_number else 'measurement ' + str(round_number)} complete: {len(items)} questions", flush=True)
        for mode in MODES:
            indexed = {row["id"]: row for row in records[mode]}
            records[mode] = [indexed[item["id"]] for item in items]
        summaries = {mode: summarize_scores(items, records[mode], retrieval_only=True, metric_version=2) for mode in MODES}
        for mode in MODES:
            for group in [summaries[mode]["overall"], *summaries[mode]["slices"]["category"].values()]:
                for metric in group["metrics"].values():
                    require(metric["eligible_count"] == metric["scored_count"] and metric["completion_rate"] == 1,
                            "incomplete scoring denominator")
            require(len(records[mode]) == len(items), "incomplete question coverage")
            for metric in summaries[mode]["overall"]["metrics"].values():
                require(metric["eligible_count"] == identity["answer_count"], "wrong overall scoring denominator")
            write_jsonl(output / f"{mode}_results.jsonl", records[mode])
        require(release_hashes(release) == original, "release changed during benchmark")
        require(all(file_hash(ROOT / p) == sha for p, sha in identity["implementation_sha256"].items()),
                "implementation changed during benchmark")
        report = {"identity": identity, "reports": summaries, "latency": {m: percentiles(v) for m, v in timings.items()},
                  "pairwise": pairwise(items, records), "initial_query_embedding": percentiles(embedding_samples),
                  "latency_contract": "single concurrency; candidate IDs only; no production guards/hydration; 1 warmup + 3 measured rounds"}
        write_jsonl(output / "latency_samples.jsonl", schedule)
        write_jsonl(output / "point_review.jsonl", [{"id": item["id"], "question": item["user_input"],
                    "metrics": {mode: records[mode][n]["metrics"] for mode in MODES}} for n, item in enumerate(items)])
        write_json(output / "report.json", report)
        (output / "report.md").write_text(markdown_report(report), encoding="utf-8")
        status = "complete"
    except BaseException as exc:
        status = "failed"
        write_json(output / "failure.json", {**public_error(exc), "exception_type": type(exc).__name__,
                                             "check": str(exc) if type(exc) is ValueError else None})
        raise
    finally:
        if connection is not None:
            connection.close()
        if engine is not None:
            engine.dispose()
        cleanup_errors = []
        if client is not None:
            for index in indexes:
                try:
                    require(re.fullmatch(r"rag-test-fulltext-" + token + r"(?:-smoke)?", index), "unsafe ES cleanup")
                    if client.indices.exists(index=index):
                        client.indices.delete(index=index)
                    require(not client.indices.exists(index=index), "ES test index remains")
                    cleanup["es"][index] = "absent"
                except Exception as exc:
                    cleanup_errors.append({"resource": index, **public_error(exc)})
            client.close()
        if database_created:
            try:
                require(database == "rag_test_fulltext_" + token and database != settings.database, "unsafe MySQL cleanup")
                container_sql(args.env_file, f"DROP DATABASE IF EXISTS `{database}`;\n"
                              f"REVOKE ALL PRIVILEGES ON `{database}`.* FROM '{settings.user}'@'%';")
                require(not container_sql(args.env_file, "SELECT SCHEMA_NAME FROM information_schema.SCHEMATA "
                                          f"WHERE SCHEMA_NAME='{database}';"), "MySQL test database remains")
                cleanup["mysql"] = "absent"
            except Exception as exc:
                cleanup_errors.append({"resource": database, **public_error(exc)})
        cleanup["errors"] = cleanup_errors
        write_json(output / "execution.json", {"status": "failed" if cleanup_errors else status,
                                               **identity, "finished_at": now(), "cleanup": cleanup})
        write_json(output / "artifacts_sha256.json", {p.name: file_hash(p) for p in sorted(output.iterdir())
                                                     if p.is_file() and p.name != "artifacts_sha256.json"})
        require(not cleanup_errors, "benchmark resource cleanup failed; inspect execution.json")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--allow-evolving", action="store_true")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env.mysql")
    parser.add_argument("--es-env-file", type=Path, default=ROOT / ".env.elasticsearch")
    args = parser.parse_args()
    try:
        report = run(args)
        print(json.dumps({"output": str(Path(args.output).resolve()), "status": "complete",
                          "latency": report["latency"]}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({**public_error(exc), "exception_type": type(exc).__name__,
                          "check": str(exc) if type(exc) is ValueError else None}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
