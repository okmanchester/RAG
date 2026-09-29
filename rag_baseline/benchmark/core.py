"""Offline corpus, review, quota and immutable-release operations."""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import re
import shutil
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .quality import (CONTENT_CHECKS, boundary, span, validate_v2, evidence_audit,
                      content_fingerprint)

TYPES = {"fact": 90, "condition": 60, "within_document": 75, "cross_document": 45, "boundary": 30}
DIFFICULTIES = {"easy": 90, "medium": 135, "hard": 45}
BEHAVIORS = {"abstain": 10, "correct_premise": 10, "clarify": 10}
STYLES = {"document": 90, "natural": 180}
REVIEW_CHECKS = ("question", "answer", "evidence", "labels", "duplicates", "corpus_search")
EDITABLE = ("user_input", "reference", "answer_points", "evidence_groups", "category",
            "question_type", "difficulty", "query_style", "expected_behavior",
            "boundary_reason", "clarifying_interpretations", "duplicate_group", "review_checks", "reviewer", "review_notes")
JSON_FIELDS = {"answer_points", "evidence_groups", "review_checks", "clarifying_interpretations"}
V2_EDITABLE = tuple(k for k in EDITABLE if k != "question_type") + (
    "answer_form", "evidence_span", "difficulty_reason", "scope_note", "multi_source_reason", "point_evidence")
V2_JSON_FIELDS = JSON_FIELDS | {"answer_form", "point_evidence"}

# The full immutable manifest also records local-only audits and derived split golds.
# Git checkouts retain this operational core; full-history verification remains explicit.
GOLD150_CORE_FILES = (
    'README.md', 'gold.jsonl', 'documents.jsonl', 'chunks.jsonl', 'families.json',
    'manifest.json', 'split.json', 'dev/queries.jsonl', 'dev/run_template.jsonl',
    'holdout/queries.jsonl', 'holdout/run_template.jsonl',
)


def schema_version(items):
    versions = {i.get("schema_version", 1) for i in items}
    if not versions <= {1, 2} or len(versions) > 1:
        raise ValueError("Mixed/unsupported schemas; explicitly revise legacy candidates first")
    return next(iter(versions), 2)


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def file_hash(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8-sig").splitlines() if line.strip()]


def write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_jsonl(path, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text("".join(json.dumps(x, ensure_ascii=False, allow_nan=False) + "\n" for x in rows), encoding="utf-8")
    temporary.replace(path)


def normalize(text):
    return re.sub(r"[\W_]+", "", text).lower()


def grams(text, width=4):
    text = normalize(text)
    return {text[i:i + width] for i in range(max(0, len(text) - width + 1))}


def similarity(a, b):
    a, b = grams(a), grams(b)
    return len(a & b) / len(a | b) if a and b else 0.0


class Corpus:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.documents = {d["doc_id"]: d for d in read_jsonl(self.directory / "documents.jsonl")}
        self.blocks = {(d["doc_id"], b["block_id"]): b for d in self.documents.values() for b in d["blocks"]}
        all_chunks = read_jsonl(self.directory / "chunks.jsonl")
        if any(type(c.get("retrieval_enabled")) is not bool for c in all_chunks):
            raise ValueError("Corpus contains chunks without retrieval flags")
        self.chunks = {c["chunk_id"]: c for c in all_chunks if c["retrieval_enabled"]}

    def identity(self):
        return {name: file_hash(self.directory / name) for name in ("documents.jsonl", "chunks.jsonl")}

    def family_report(self):
        """Conservative automatic families; suspicious pairs remain visible to reviewer."""
        docs = list(self.documents.values())
        parent = {d["doc_id"]: d["doc_id"] for d in docs}
        def root(x):
            while x != parent[x]:
                x = parent[x]
            return x
        bodies = {d["doc_id"]: grams("".join(b["text"] for b in d["blocks"] if b.get("role") == "body"), 8) for d in docs}
        pairs = []
        for i, a in enumerate(docs):
            for b in docs[i + 1:]:
                aa, bb = bodies[a["doc_id"]], bodies[b["doc_id"]]
                containment = len(aa & bb) / min(len(aa), len(bb)) if aa and bb else 0
                exact = a["sha256"] == b["sha256"]
                if exact or containment >= .65:
                    merge = exact or containment >= .9
                    pairs.append({"a": a["doc_id"], "b": b["doc_id"], "containment": containment, "merged": merge})
                    if merge:
                        parent[root(b["doc_id"])] = root(a["doc_id"])
        return {"family_by_doc": {k: root(k) for k in parent}, "suspected_pairs": pairs,
                "documents": [{"doc_id": d["doc_id"], "source_path": d["source_path"], "category": d["category"]} for d in docs],
                "review_status": "pending", "reviewer": "", "notes": "", "coverage_exclusions": {}}

    def evidence(self, doc_id, block_id, quote, chunk_id=None):
        block = self.blocks[(doc_id, block_id)]
        start = block["text"].find(quote)
        if not quote.strip() or start < 0:
            raise ValueError(f"Evidence is not an exact source substring: {doc_id}/{block_id}")
        doc = self.documents[doc_id]
        return {"doc_id": doc_id, "document_sha256": doc["sha256"], "source_path": doc["source_path"],
                "block_id": block_id, "block_char_start": start, "block_char_end": start + len(quote),
                "text": quote, "location": block["source"], "chunk_id": chunk_id,
                "heading_path": self.chunks.get(chunk_id, {}).get("heading_path", [])}

    def validate_evidence(self, e):
        doc = self.documents[e["doc_id"]]
        block = self.blocks[(e["doc_id"], e["block_id"])]
        start, end = e["block_char_start"], e["block_char_end"]
        if not isinstance(e["text"], str) or not e["text"].strip():
            raise ValueError("Evidence text must be substantive")
        if not (type(start) is int and type(end) is int and 0 <= start < end <= len(block["text"])):
            raise ValueError("Invalid evidence interval")
        if (doc["sha256"] != e["document_sha256"] or doc["source_path"] != e["source_path"]
                or block["text"][start:end] != e["text"] or block["source"] != e["location"]):
            raise ValueError("Evidence/source mismatch")


def validate_item(item, corpus, approved=False):
    for field in ("id", "user_input", "reference"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ValueError(f"Missing {field}")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", item["id"]):
        raise ValueError("Unsafe question ID")
    version = schema_version([item])
    if item["category"] not in ("policy", "technical") or (version == 1 and item["question_type"] not in TYPES):
        raise ValueError("Invalid category/type")
    is_boundary = boundary(item) if version == 2 else item["question_type"] == "boundary"
    if version == 1 and item["difficulty"] not in (["boundary"] if is_boundary else DIFFICULTIES):
        raise ValueError("Invalid difficulty")
    if item["expected_behavior"] not in (BEHAVIORS if is_boundary else ["answer"]):
        raise ValueError("Invalid expected behavior")
    if item["query_style"] not in STYLES:
        raise ValueError("Invalid query style")
    points = item["answer_points"]
    if not points or any(not isinstance(p, str) or not p.strip() for p in points):
        raise ValueError("Answer points required")
    groups = item["evidence_groups"]
    if not groups and item["expected_behavior"] in ("answer", "correct_premise"):
        raise ValueError("Answer/correction requires evidence")
    group_ids = [g["id"] for g in groups]
    if len(group_ids) != len(set(group_ids)):
        raise ValueError("Duplicate evidence group ID")
    for g in groups:
        if not g["alternatives"]:
            raise ValueError("Evidence group has no alternatives")
        for bundle in g["alternatives"]:
            if not bundle:
                raise ValueError("Empty evidence bundle")
            for e in bundle:
                corpus.validate_evidence(e)
    signatures = [digest(g["alternatives"]) for g in groups]
    if len(signatures) != len(set(signatures)):
        raise ValueError("Repeated evidence is not independent multi-hop evidence")
    source_docs = {e["doc_id"] for g in groups for bundle in g["alternatives"] for e in bundle}
    if span(item) in ("within_document", "cross_document") and len(groups) < 2:
        raise ValueError("Multi-hop requires >=2 necessary evidence groups")
    if span(item) in ("within_document", "single_unit") and len(source_docs) != 1:
        raise ValueError("Within-document question must use one document")
    if span(item) == "cross_document" and len(source_docs) < 2:
        raise ValueError("Cross-document question requires distinct documents")
    if span(item) == "cross_document":
        if any(all(any(all(e["doc_id"] == doc_id for e in bundle) for bundle in g["alternatives"]) for g in groups)
               for doc_id in source_docs):
            raise ValueError("Cross-document evidence can be satisfied by one document")
    if version == 2:
        validate_v2(item, corpus, approved)
    if is_boundary and not item.get("boundary_reason", "").strip():
        raise ValueError("Boundary reason required")
    if item["expected_behavior"] == "clarify" and approved:
        if len(item.get("clarifying_interpretations", [])) < 2 or not re.search(r"[?？]", item["reference"]):
            raise ValueError("Clarification requires two interpretations and an explicit question")
    if approved:
        if item.get("review_status") != "approved" or not item.get("reviewer", "").strip():
            raise ValueError("Human approval and reviewer required")
        required = REVIEW_CHECKS if is_boundary else REVIEW_CHECKS[:-1]
        if any(item.get("review_checks", {}).get(k) is not True for k in required):
            raise ValueError("All applicable human review checks must be true")
        if is_boundary and not item.get("corpus_search"):
            raise ValueError("Boundary question requires recorded corpus search")


def export_review(items, directory, corpus):
    version = schema_version(items)
    editable = V2_EDITABLE if version == 2 else EDITABLE
    json_fields = V2_JSON_FIELDS if version == 2 else JSON_FIELDS
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / "review.csv"
    if csv_path.exists():
        raise FileExistsError(f"Refusing to overwrite human review: {csv_path}")
    fields = ["id", "candidate_hash", "review_status", "machine_notes", *editable]
    with csv_path.open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fields)
        writer.writeheader()
        for item in items:
            row = {k: item.get(k, [] if k == "clarifying_interpretations" else "") for k in fields}
            row["candidate_hash"] = digest(item)
            notes = item.get("boundary_precheck", item.get("machine_notes", ""))
            row["machine_notes"] = json.dumps(notes, ensure_ascii=False) if notes else ""
            row["review_status"] = item.get("review_status", "pending")
            row["review_checks"] = item.get("review_checks") or {k: False for k in (*REVIEW_CHECKS, *(CONTENT_CHECKS if version == 2 else ()))}
            if version == 1:
                row["review_checks"] = {k: False for k in REVIEW_CHECKS}
            for k in json_fields:
                row[k] = json.dumps(row[k], ensure_ascii=False)
            if row.get("difficulty") is None:
                row["difficulty"] = ""
            # Prevent spreadsheet formula execution when opening untrusted text.
            for k, v in row.items():
                if isinstance(v, str) and v.startswith(("=", "+", "-", "@", "\t", "\r")):
                    row[k] = "'" + v
            writer.writerow(row)
    lines = ["# Benchmark 试审阅读稿", "", "状态统计：" + json.dumps(dict(Counter(i.get("review_status", "pending") for i in items)), ensure_ascii=False),
             "CSV 是可编辑审核入口；本稿展示导出时的题目与证据。编辑 CSV 后须重新导入、导出以同步阅读稿。",
             "修改题目后须同步修改答案、要点和证据。不能因系统答错而删题。",
             "evidence_groups: 组间 AND，alternatives 间 OR，每个 alternative 内片段 AND。", ""]
    for item in items:
        label = ",".join(item["answer_form"]) + " / " + item["evidence_span"] if version == 2 else item["question_type"]
        lines += [f"## {item['id']}", f"{item['category']} / {label} / {item['difficulty']} / {item['expected_behavior']}",
                  "", item["user_input"], "", "参考答案：" + item["reference"], "", "必要要点："]
        if item.get("boundary_precheck"):
            lines += ["", "机器预审（不能代替人工）：" + json.dumps(item["boundary_precheck"], ensure_ascii=False), ""]
        lines += [f"- {p}" for p in item["answer_points"]]
        lines += ["", "审核状态：" + item.get("review_status", "pending"), "题面风格：" + item["query_style"]]
        if item.get("reviewer"):
            lines += ["审核人/方式：" + item["reviewer"], "审核结论：" + item.get("review_notes", "")]
        if version == 2:
            audit = evidence_audit(item, corpus)
            lines += ["难度依据：" + item["difficulty_reason"], "问题范围：" + item["scope_note"],
                      "多证据必要性：" + item.get("multi_source_reason", ""),
                      "要点对应证据组：" + json.dumps(item["point_evidence"], ensure_ascii=False),
                      "修订说明：" + item.get("revision_note", ""),
                      "证据检查提示（仍须人工确认）：" + json.dumps(sorted({f for r in audit["records"] for f in r["flags"]}), ensure_ascii=False)]
        lines += ["", "边界说明：" + item.get("boundary_reason", ""), "", "证据："]
        for group in item["evidence_groups"]:
            for ai, bundle in enumerate(group["alternatives"], 1):
                for e in bundle:
                    lines += [f"- {group['id']} / alternative {ai} / {e['source_path']} / block {e['block_id']} / {e['location']}",
                              "", "> " + e["text"].replace("\n", "\n> "), ""]
        if item.get("corpus_search"):
            lines += ["全库检索结果（仅辅助核验，不能证明全库无答案）："]
            for hit in item["corpus_search"]:
                c = corpus.chunks[hit["chunk_id"]]
                lines += [f"- {c['source_path']} / {c['chunk_id']} / score={hit['score']:.4f}",
                          "", "> " + c["text"].replace("\n", "\n> "), ""]
    (directory / "reading.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    if version == 2:
        write_jsonl(directory / "evidence_audit.jsonl", [{"id": i["id"], **evidence_audit(i, corpus)} for i in items])


def import_review(items, csv_path, corpus):
    version = schema_version(items)
    editable = V2_EDITABLE if version == 2 else EDITABLE
    json_fields = V2_JSON_FIELDS if version == 2 else JSON_FIELDS
    originals = {i["id"]: i for i in items}
    result, seen = [], set()
    with Path(csv_path).open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            for k, v in row.items():
                if isinstance(v, str) and len(v) > 1 and v[0] == "'" and v[1] in "=+-@\t\r":
                    row[k] = v[1:]
            identifier = row["id"]
            if identifier in seen or identifier not in originals:
                raise ValueError("Duplicate/unknown review ID")
            seen.add(identifier)
            original = originals[identifier]
            if row["candidate_hash"] != digest(original):
                raise ValueError(f"Stale review sheet: {identifier}")
            status = row["review_status"]
            if status not in ("pending", "approved", "rejected", "needs_revision"):
                raise ValueError("Invalid review status")
            item = copy.deepcopy(original)
            for k in editable:
                item[k] = json.loads(row[k]) if k in json_fields else row[k]
            if version == 2:
                item["difficulty"] = item["difficulty"] or None
                if content_fingerprint(item) != content_fingerprint(original):
                    item.pop("semantic_precheck", None)
                    item.pop("boundary_precheck", None)
                    if isinstance(item.get("machine_notes"), dict) and "semantic_precheck" in item["machine_notes"]:
                        item.pop("machine_notes")
                item["evidence_audit"] = evidence_audit(item, corpus)
            item.update(review_status=status, reviewed_at=now(), original_candidate_hash=digest(original))
            validate_item(item, corpus, approved=status == "approved")
            result.append(item)
    if seen != set(originals):
        raise ValueError("Review sheet must contain every candidate, including rejected rows")
    return result


def duplicate_clusters(items):
    parent = list(range(len(items)))
    def root(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    def signature(item):
        return sorted((e["doc_id"], e["block_id"], e["block_char_start"], e["block_char_end"])
                      for g in item["evidence_groups"] for e in g["alternatives"][0])
    evidence = [signature(i) for i in items]
    links = []
    for i, a in enumerate(items):
        for j in range(i):
            b = items[j]
            manual = bool(a.get("duplicate_group")) and a["duplicate_group"] == b.get("duplicate_group")
            same_question = normalize(a["user_input"]) == normalize(b["user_input"])
            near_question = similarity(a["user_input"], b["user_input"]) >= .65
            same_evidence = bool(evidence[i]) and evidence[i] == evidence[j]
            near_answer = similarity("".join(a["answer_points"]), "".join(b["answer_points"])) >= .7
            if manual or same_question or near_question or (same_evidence and near_answer):
                parent[root(i)] = root(j)
                links.append([a["id"], b["id"]])
    groups = defaultdict(list)
    for i in range(len(items)):
        groups[root(i)].append(i)
    return list(groups.values()), links


def quotas(items, split=None):
    rows = [i for i in items if split is None or i.get("split") == split]
    if schema_version(items) == 2:
        return {"total": len(rows), "category": dict(Counter(i["category"] for i in rows)),
                "answer_form": dict(Counter(f for i in rows for f in i["answer_form"])),
                "evidence_span": dict(Counter(i["evidence_span"] for i in rows)),
                "difficulty": dict(Counter(i["difficulty"] for i in rows if not boundary(i))),
                "query_style": dict(Counter(i["query_style"] for i in rows)),
                "expected_behavior": dict(Counter(i["expected_behavior"] for i in rows if boundary(i)))}
    return {"total": len(rows), "category": dict(Counter(i["category"] for i in rows)),
            "question_type": dict(Counter(i["question_type"] for i in rows)),
            "difficulty": dict(Counter(i["difficulty"] for i in rows if i["question_type"] != "boundary")),
            "query_style": dict(Counter(i["query_style"] for i in rows if i["question_type"] != "boundary")),
            "expected_behavior": dict(Counter(i["expected_behavior"] for i in rows if i["question_type"] == "boundary"))}


def validate_quotas(items):
    version = schema_version(items)
    for split, fraction in (("dev", .8), ("holdout", .2)):
        q = quotas(items, split)
        if version == 2:
            if (q["total"] != int(300*fraction) or q["category"] != {k: int(150*fraction) for k in ("policy", "technical")}
                    or q["expected_behavior"] != {k: int(v*fraction) for k, v in BEHAVIORS.items()}
                    or q["query_style"].get("document", 0) > int(30*fraction)):
                raise ValueError(f"Unmet v2 quotas/style cap for {split}: {q}")
            continue
        expected = {"total": int(300 * fraction), "category": {"policy": int(150 * fraction), "technical": int(150 * fraction)},
                    "question_type": {k: int(v * fraction) for k, v in TYPES.items()},
                    "difficulty": {k: int(v * fraction) for k, v in DIFFICULTIES.items()},
                    "query_style": {k: int(v * fraction) for k, v in STYLES.items()},
                    "expected_behavior": {k: int(v * fraction) for k, v in BEHAVIORS.items()}}
        if q != expected:
            raise ValueError(f"Unmet quotas for {split}: {q}; expected {expected}")


def select_release(items, families):
    """MILP selects exact marginal quotas; no approximate or silent fallback."""
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, milp
    from scipy.sparse import lil_matrix
    version = schema_version(items)
    approved = [copy.deepcopy(i) for i in items if i.get("review_status") == "approved"]
    if len(approved) < 300:
        raise ValueError(f"Only {len(approved)} approved candidates; need at least 300")
    n = len(approved)
    constraints = []
    def add(indices, low, high):
        constraints.append((list(indices), low, high))
    for s, fraction in enumerate((.8, .2)):
        add(range(s*n, (s+1)*n), int(300*fraction), int(300*fraction))
        targets = {"category": {"policy": 150, "technical": 150}, "question_type": TYPES,
                   "difficulty": DIFFICULTIES, "query_style": STYLES, "expected_behavior": BEHAVIORS}
        if version == 2:
            targets = {"category": {"policy": 150, "technical": 150}, "expected_behavior": BEHAVIORS}
            add([s*n+i for i, row in enumerate(approved) if row["query_style"] == "document"], 0, int(30*fraction))
        for field, values in targets.items():
            for value, count in values.items():
                idx = [s*n+i for i, row in enumerate(approved) if row[field] == value
                       and not (field == "query_style" and row["question_type"] == "boundary")]
                add(idx, int(count*fraction), int(count*fraction))
    for i, row in enumerate(approved):
        add([i, n+i], 0, 1)
        if row.get("pilot"):
            add([n+i], 0, 0)
    groups, _ = duplicate_clusters(items)
    approved_index = {row["id"]: i for i, row in enumerate(approved)}
    for group in groups:
        ids = [approved_index[items[i]["id"]] for i in group if items[i]["id"] in approved_index]
        # One representative per duplicate group is stricter than keeping a group in one split.
        add(ids + [n+i for i in ids], 0, 1)
        if any(items[i].get("pilot") for i in group):
            add([n+i for i in ids], 0, 0)
    mapping = families["family_by_doc"]
    excluded = set(families.get("coverage_exclusions", {}))
    for family in set(mapping.values()) - excluded:
        ids = [i for i, row in enumerate(approved) if family in evidence_families(row, mapping)]
        add(ids + [n+i for i in ids], 1, np.inf)
    matrix = lil_matrix((len(constraints), 2*n))
    for index, (cols, _, _) in enumerate(constraints):
        matrix[index, cols] = 1
    rng = np.random.default_rng(20260916)
    result = milp(rng.uniform(0, 1, 2*n), integrality=np.ones(2*n), bounds=Bounds(0, 1),
                  constraints=LinearConstraint(matrix.tocsr(), [x[1] for x in constraints], [x[2] for x in constraints]),
                  options={"time_limit": 120})
    if not result.success:
        raise ValueError(f"Cannot satisfy quotas/coverage/duplicate constraints; supplement reviewed candidates: {result.message}")
    selected = []
    for s, split in enumerate(("dev", "holdout")):
        for i, row in enumerate(approved):
            if result.x[s*n+i] > .5:
                selected.append(dict(row, split=split))
    validate_quotas(selected)
    return selected


def evidence_families(item, mapping):
    return {mapping[e["doc_id"]] for g in item["evidence_groups"] for bundle in g["alternatives"] for e in bundle}


def freeze(items, corpus, families, output, manifest):
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Immutable release already exists: {output}")
    if families.get("review_status") != "approved" or not families.get("reviewer", "").strip():
        raise ValueError("Document families require human confirmation before freezing")
    mapping = families["family_by_doc"]
    if (set(mapping) != set(corpus.documents) or any(v not in corpus.documents for v in mapping.values())
            or any(mapping[v] != v for v in mapping.values())):
        raise ValueError("Invalid family mapping")
    exclusions = families.get("coverage_exclusions", {})
    if not set(exclusions) <= set(mapping.values()) or any(not str(v).strip() for v in exclusions.values()):
        raise ValueError("Coverage exclusions require valid family IDs and reasons")
    if manifest["corpus"] != corpus.identity():
        raise ValueError("Corpus changed since generation")
    identifiers = [i["id"] for i in items]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Duplicate candidate IDs")
    for item in items:
        if item.get("review_status") == "approved":
            validate_item(item, corpus, approved=True)
            if span(item) == "cross_document" and len(evidence_families(item, mapping)) < 2:
                raise ValueError("Cross-document question uses duplicate document versions")
    selected = select_release(items, families)
    groups, links = duplicate_clusters(selected)
    if any(len(g) > 1 for g in groups):
        raise ValueError("Duplicate release questions")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".freeze-", dir=output.parent))
    try:
        write_jsonl(temporary / "gold.jsonl", selected)
        write_json(temporary / "families.json", families)
        for name in ("documents.jsonl", "chunks.jsonl"):
            shutil.copyfile(corpus.directory / name, temporary / name)
        write_json(temporary / "manifest.json", {**manifest, "status": "frozen", "frozen_at": now(),
                   "release": output.name, "gold_sha256": file_hash(temporary / "gold.jsonl"),
                   "families_sha256": file_hash(temporary / "families.json"),
                   "quotas": {s: quotas(selected, s) for s in ("dev", "holdout")}, "duplicate_links": links})
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary)
        raise


def load_release(directory, *, verify_history=False):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != "frozen" or file_hash(directory / "gold.jsonl") != manifest["gold_sha256"]:
        raise ValueError("Release is not frozen or gold was modified")
    for name, expected in manifest["corpus"].items():
        if file_hash(directory / name) != expected:
            raise ValueError("Frozen corpus was modified")
    if file_hash(directory / "families.json") != manifest["families_sha256"]:
        raise ValueError("Frozen family mapping was modified")
    artifacts = manifest.get("artifacts_sha256", {})
    is_gold150 = manifest.get('dataset_kind') == 'gold150'
    if is_gold150 and set(GOLD150_CORE_FILES) - {'manifest.json'} - set(artifacts):
        raise ValueError('Missing Gold150 core artifact hashes')
    for name, expected in artifacts.items():
        artifact = Path(name)
        if not artifact.parts or artifact.is_absolute() or ".." in artifact.parts:
            raise ValueError("Unsafe release artifact path")
        local_only = artifact.parts[0] in ('review', 'review_provenance') or name in (
            'dev/gold.jsonl', 'holdout/gold.jsonl')
        if is_gold150 and local_only and not verify_history:
            continue
        if not (directory / artifact).is_file():
            raise ValueError(f'Missing frozen artifact: {name}')
        if file_hash(directory / artifact) != expected:
            raise ValueError(f"Frozen artifact was modified: {name}")
    return read_jsonl(directory / "gold.jsonl"), manifest


def freeze_trial(items, corpus, families, output, manifest):
    """Freeze exactly 30 reviewed pilot items as dev; full-release quotas stay intact."""
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Immutable release already exists: {output}")
    if len(items) != 30 or len({i['id'] for i in items}) != 30 or schema_version(items) != 2:
        raise ValueError("Trial finalization requires exactly 30 distinct schema-v2 items")
    if manifest['corpus'] != corpus.identity():
        raise ValueError("Corpus changed since review")
    mapping = families['family_by_doc']
    if (set(mapping) != set(corpus.documents) or any(v not in mapping or mapping[v] != v for v in mapping.values())):
        raise ValueError("Invalid family mapping")
    if families.get('review_status') != 'approved' or not families.get('reviewer', '').strip():
        raise ValueError("Trial document families need an attributed review")
    selected = []
    for row in items:
        validate_item(row, corpus, approved=True)
        if not row.get('pilot') or row.get('split') not in (None, 'dev'):
            raise ValueError("A public trial must contain pilot items and cannot include holdout")
        if span(row) == 'cross_document' and len(evidence_families(row, mapping)) < 2:
            raise ValueError("Cross-document question uses duplicate document versions")
        selected.append(dict(copy.deepcopy(row), split='dev'))
    if sum(i['query_style'] == 'document' for i in selected) > 3:
        raise ValueError("Trial document-hint cap is 10 percent")
    groups, links = duplicate_clusters(selected)
    if any(len(g) > 1 for g in groups):
        raise ValueError("Duplicate trial questions")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.freeze-trial-', dir=output.parent))
    try:
        write_jsonl(temporary/'gold.jsonl', selected)
        write_json(temporary/'families.json', families)
        for name in ('documents.jsonl', 'chunks.jsonl'):
            shutil.copyfile(corpus.directory/name, temporary/name)
        export_review(selected, temporary/'review', corpus)
        write_jsonl(temporary/'queries.jsonl', [{'id':i['id'], 'user_input':i['user_input']} for i in selected])
        write_jsonl(temporary/'run_template.jsonl', [
            {'id':i['id'], 'response':'', 'retrieved_contexts':[], 'citations':[],
             'run_config':{'pipeline_version':'REPLACE_WITH_ACTUAL_RUN_CONFIG'}, 'error':'not_run'} for i in selected])
        (temporary/'README.md').write_text(
            '# 30题试审集定稿\n\n'
            '状态：已定稿。gold.jsonl 为唯一金标准；review/reading.md 与 review/review.csv 是已填写的审核归档。\n'
            '审核方式见每题 reviewer 和 approval_provenance；受用户委托的 agent 审核不表述为用户逐题亲自审核。\n\n'
            '全部30题属于dev，不能进入holdout。本版本是独立试审集，不代表300题正式配额已完成。\n'
            '边界题仅含abstain与clarify，不报告correct_premise能力；小样本切片仅供诊断。\n\n'
            '将queries.jsonl交给被测系统。不要向被测系统提供gold.jsonl、参考答案或证据。\n'
            '复制run_template.jsonl到发布目录外，填入真实回答、实际检索上下文、引用及运行配置，成功后将error改为null。\n'
            '模板中的not_run表示尚未运行，不是模型评测结果。未提供sources时，只按同一文档哈希下的证据文本匹配。\n\n'
            '在conda rag环境、仓库根目录运行（将tmp/actual_runs.jsonl替换为真实运行结果）：\n\n'
            '```powershell\n'
            f'python -m rag_baseline.benchmark evaluate --release {output.as_posix()} --runs tmp/actual_runs.jsonl --output tmp/trial30_retrieval_eval --retrieval-only\n'
            f'python -m rag_baseline.benchmark evaluate --release {output.as_posix()} --runs tmp/actual_runs.jsonl --output tmp/trial30_full_eval\n'
            '```\n\n'
            '完整评测需配置DASHSCOPE_API_KEY并完成人工/委托校准；数据定稿不表示评委已校准或RAG系统已通过评测。\n'
            '发布文件均有哈希校验；纠错时发布新版本，不覆盖本目录。\n', encoding='utf-8')
        artifacts = {p.relative_to(temporary).as_posix():file_hash(p) for p in temporary.rglob('*') if p.is_file()}
        write_json(temporary/'manifest.json', {**manifest, 'status':'frozen', 'dataset_kind':'reviewed_trial',
            'schema_version':2, 'release':output.name, 'frozen_at':now(), 'item_count':30, 'split_policy':'all_dev_no_holdout',
            'full_benchmark_quotas_applied':False, 'gold_sha256':file_hash(temporary/'gold.jsonl'),
            'families_sha256':file_hash(temporary/'families.json'), 'artifacts_sha256':artifacts,
            'quotas':{'dev':quotas(selected,'dev'), 'holdout':quotas(selected,'holdout')}, 'duplicate_links':links})
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary)
        raise


def freeze_reviewed(items, corpus, families, output, manifest, authorization,
                    reference_releases=(), review_artifacts=()):
    """Freeze an explicitly authorized reviewed batch as public dev only.

    Does not select candidates or relax the separate 300-item release contract.
    Existing releases are verified and used for cross-release deduplication.
    """
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Immutable release already exists: {output}")
    if not isinstance(authorization, str) or not authorization.strip():
        raise ValueError("Reviewed batch freeze requires recorded user authorization")
    if not items or schema_version(items) != 2 or len({i['id'] for i in items}) != len(items):
        raise ValueError("Reviewed batch requires distinct nonempty schema-v2 items")
    if manifest['corpus'] != corpus.identity():
        raise ValueError("Corpus changed since review")
    mapping = families['family_by_doc']
    if (set(mapping) != set(corpus.documents) or any(v not in mapping or mapping[v] != v for v in mapping.values())):
        raise ValueError("Invalid family mapping")
    if families.get('review_status') != 'approved' or not families.get('reviewer', '').strip():
        raise ValueError("Document families need an attributed review")
    artifacts = [Path(p) for p in review_artifacts]
    if len({p.name for p in artifacts}) != len(artifacts) or any(not p.is_file() for p in artifacts):
        raise ValueError("Review artifacts must be existing files with distinct basenames")
    artifact_paths = {p.name:f'review_provenance/{p.name}' for p in artifacts}
    selected = []
    for row in items:
        validate_item(row, corpus, approved=True)
        if row.get('split') not in (None, 'dev'):
            raise ValueError("Public reviewed batches cannot include holdout")
        if span(row) == 'cross_document' and len(evidence_families(row, mapping)) < 2:
            raise ValueError("Cross-document question uses duplicate document versions")
        frozen_row = dict(copy.deepcopy(row), split='dev', pilot=True,
            freeze_provenance={'method':'user_authorized_reviewed_batch',
                'authorization':authorization, 'source_item_sha256':digest(row),
                'public_dev_only':True, 'human_per_item_review_claimed':False})
        for field, key in (('review_provenance','audit_file'), ('approval_provenance','audit_file'),
                           ('corpus_search_provenance','current_search_file')):
            provenance = frozen_row.get(field, {})
            if provenance.get(key) in artifact_paths:
                provenance[key] = artifact_paths[provenance[key]]
        selected.append(frozen_row)
    if sum(i['query_style'] == 'document' for i in selected) * 10 > len(selected):
        raise ValueError("Reviewed batch document-hint cap is 10 percent")
    previous, references = [], []
    for directory in reference_releases:
        directory = Path(directory)
        rows, source_manifest = load_release(directory)
        if source_manifest['corpus'] != manifest['corpus']:
            raise ValueError("Reference release corpus differs from reviewed batch")
        if source_manifest.get('judge') != manifest.get('judge'):
            raise ValueError("Reference release judge differs from reviewed batch")
        previous.extend(rows)
        references.append({'release':directory.as_posix(), 'item_count':len(rows),
            'gold_sha256':source_manifest['gold_sha256'], 'manifest_sha256':file_hash(directory/'manifest.json')})
    combined = previous + selected
    if len({i['id'] for i in combined}) != len(combined):
        raise ValueError("Duplicate IDs across reviewed batch and reference releases")
    _, all_links = duplicate_clusters(combined)
    selected_ids = {i['id'] for i in selected}
    links = [pair for pair in all_links if any(identifier in selected_ids for identifier in pair)]
    if links:
        raise ValueError(f"Duplicate reviewed questions, including reference releases: {links}")
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.freeze-reviewed-', dir=output.parent))
    try:
        write_jsonl(temporary/'gold.jsonl', selected)
        write_json(temporary/'families.json', families)
        for name in ('documents.jsonl', 'chunks.jsonl'):
            shutil.copyfile(corpus.directory/name, temporary/name)
        export_review(selected, temporary/'review', corpus)
        write_jsonl(temporary/'queries.jsonl', [{'id':i['id'], 'user_input':i['user_input']} for i in selected])
        write_jsonl(temporary/'run_template.jsonl', [
            {'id':i['id'], 'response':'', 'retrieved_contexts':[], 'citations':[],
             'run_config':{'pipeline_version':'REPLACE_WITH_ACTUAL_RUN_CONFIG'}, 'error':'not_run'} for i in selected])
        write_jsonl(temporary/'review_provenance/source_approved.jsonl', items)
        for path in artifacts:
            if path.name == 'source_approved.jsonl':
                raise ValueError("Reserved review artifact name")
            shutil.copyfile(path, temporary/'review_provenance'/path.name)
        count = len(selected)
        (temporary/'README.md').write_text(
            f'# {count}题已审核批次金标准\n\n'
            '状态：已冻结。gold.jsonl为本版本唯一金标准；review/reading.md及review/review.csv为审核归档。\n'
            '审核方式见review_provenance和approval_provenance；用户授权冻结不冒称用户逐题复审。\n'
            f'冻结授权原文：{authorization}\n\n'
            f'全部{count}题为公开dev，pilot标志记录已公开使用，不能作为holdout。'
            '本批次不应用300题正式集配额，也不代表正式集已完成。\n'
            'previous_frozen_releases记录旧金标准及其哈希；本版不覆盖旧版。\n\n'
            '只将queries.jsonl交给被测系统。复制run_template.jsonl到发布目录外填写真实结果，'
            'not_run不表示系统已运行。评测时验证全部发布哈希。\n\n'
            f'review_provenance保存冻结前{count}题和指定审核资料；'
            '历史修订日志可能含已排除题，实际评测仅使用gold.jsonl。\n\n'
            '在conda rag环境、仓库根目录运行：\n\n'
            '```powershell\n'
            f'python -m rag_baseline.benchmark evaluate --release {output.as_posix()} --runs tmp/actual_runs.jsonl --output tmp/{output.name}_retrieval_eval --retrieval-only\n'
            '```\n\n'
            '小样本切片仅供诊断；完整评测需评委配置及校准。纠错时发布新版本，禁止覆盖此目录。\n', encoding='utf-8')
        hashes = {p.relative_to(temporary).as_posix():file_hash(p) for p in temporary.rglob('*') if p.is_file()}
        write_json(temporary/'manifest.json', {**manifest, 'status':'frozen', 'dataset_kind':'reviewed_dev_batch',
            'schema_version':2, 'release':output.name, 'frozen_at':now(), 'item_count':count,
            'split_policy':'all_dev_no_holdout', 'full_benchmark_quotas_applied':False, 'no_freeze_performed':False,
            'freeze_authorization':authorization, 'source_items_sha256':digest(items),
            'selection':{'method':'all_reviewed_items_in_supplied_file', 'count':count},
            'gold_sha256':file_hash(temporary/'gold.jsonl'), 'families_sha256':file_hash(temporary/'families.json'),
            'artifacts_sha256':hashes, 'previous_frozen_releases':references,
            'quotas':{'dev':quotas(selected,'dev'), 'holdout':quotas(selected,'holdout')}, 'duplicate_links':links})
        temporary.rename(output)
    except BaseException:
        shutil.rmtree(temporary)
        raise
