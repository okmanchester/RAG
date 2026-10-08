"""Benchmark-only source eligibility and content checks; never changes the corpus."""
from __future__ import annotations

import re
import unicodedata

SCHEMA_VERSION = 2
POLICY_VERSION = "benchmark-evidence-v2.1"
ANSWER_FORMS = ("fact", "condition", "procedure", "comparison", "synthesis")
EVIDENCE_SPANS = ("single_unit", "within_document", "cross_document", "none")
CONTENT_CHECKS = ("scope", "point_support", "completeness", "source_quality", "multi_source")


def compact(text):
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def boundary(item):
    return item.get("expected_behavior", "answer") != "answer"


def span(item):
    if item.get("schema_version", 1) == 2:
        return item["evidence_span"]
    return item.get("question_type", "fact")


def named_document(question, corpus):
    q = compact(question)
    if "《" in q or re.search(r"(?:本|该|这个)(?:文件|办法|指南)|第[一二三四五六七八九十百\d]+[章节条]", q):
        return True
    # Titles are matched independently of introductory phrasing ("根据"/"在…中").
    for d in corpus.documents.values():
        name = d["source_path"].replace("\\", "/").rsplit("/", 1)[-1].rsplit(".", 1)[0]
        name = re.split(r"_", name)[0]
        name = re.sub(r"[（(].*?[）)]", "", name)
        names = {name, name.removeprefix("中华人民共和国")}
        if "——" in name:
            names.add(name.split("——", 1)[0])
        if any(len(compact(n)) >= 6 and compact(n) in q for n in names):
            return True
    return False


def block_flags(corpus, doc_id, block_id):
    """Use the actual preceding heading, not a potentially stale heading_path."""
    blocks = corpus.documents[doc_id]["blocks"]
    pos = next(i for i, b in enumerate(blocks) if b["block_id"] == block_id)
    block = blocks[pos]
    text = compact(block["text"])
    heading = next((compact(b["text"]).strip("*# ") for b in reversed(blocks[:pos+1])
                    if b.get("role") in ("heading", "document_title")), "")
    flags = []
    if re.search(r"参考文献|规范性引用文件|^目[次录]$", heading):
        flags.append("reference_or_navigation")
    if re.search(r"起草单位|起草人|本文件按照|本文件由.*(?:提出|归口)", text):
        flags.append("drafting_metadata")
    if ("=" in text and (any(unicodedata.category(c) == "Co" for c in text)
                          or "<sup>" in text or "<sub>" in text)):
        flags.append("damaged_formula")
    if "|" in text:
        flags.append("table_layout_check")
    if block.get("role") in ("heading", "document_title") or re.match(r"^[\uf000-\uf8ff]*应用场景\d+", text):
        flags.append("heading_only")
    return flags


def expanded_blocks(task, corpus):
    """Expand complete articles/lists and immediate cross-page context, not chapters."""
    selected = {}
    for cid in task["chunk_ids"]:
        c = corpus.chunks[cid]
        selected.setdefault(c["doc_id"], set()).update(s["block_id"] for s in [*c["sources"], *c.get("context_sources", [])])
    result = []
    for doc_id, bids in selected.items():
        doc = corpus.documents[doc_id]
        blocks = doc["blocks"]
        included = set()
        article = re.compile(r"^\s*(?:\*\*)?第[一二三四五六七八九十百零〇\d]+条")
        for p, b in enumerate(blocks):
            if b["block_id"] not in bids:
                continue
            included.update(range(max(0, p-1), min(len(blocks), p+2)))
            starts = [j for j in range(p+1) if article.match(blocks[j]["text"])]
            if starts:
                lo = starts[-1]
                hi = next((j for j in range(p+1, len(blocks)) if article.match(blocks[j]["text"])
                           or blocks[j].get("role") == "heading"), len(blocks))
                # Guard against accidental chapter-wide expansion in prose documents.
                if hi-lo <= 35 and not any(x.get("role") == "heading" for x in blocks[lo+1:p+1]):
                    included.update(range(lo, hi))
            if "|" in b["text"]:
                for step in (-1, 1):
                    j = p+step
                    while (0 <= j < len(blocks) and "|" in blocks[j]["text"]
                           and (not b.get("group_id") or blocks[j].get("group_id") == b["group_id"])):
                        included.add(j)
                        j += step
            list_item = re.compile(r"^\s*(?:-\s+\S|(?:-\s*)?(?:[（(][一二三四五六七八九十\d]+[）)]|[a-z\d]+\s*[）)]))")
            if list_item.match(b["text"]):
                for step in (-1, 1):
                    j = p+step
                    while 0 <= j < len(blocks) and abs(j-p) <= 35 and list_item.match(blocks[j]["text"]):
                        included.add(j)
                        j += step
        for p in sorted(included):
            b = blocks[p]
            if b.get("role") in {"excluded", "toc"}:
                continue
            result.append({"doc_id": doc_id, "block_id": b["block_id"],
                           "title": doc.get("title", doc["source_path"]), "text": b["text"],
                           "quality_flags": block_flags(corpus, doc_id, b["block_id"])})
    return result


def content_fingerprint(item):
    from .core import digest
    return digest({k: item.get(k) for k in ("user_input", "reference", "answer_points", "evidence_groups",
                  "answer_form", "evidence_span", "expected_behavior", "difficulty", "difficulty_reason",
                  "scope_note", "multi_source_reason", "point_evidence", "query_style",
                  "boundary_reason", "clarifying_interpretations")})


def evidence_audit(item, corpus):
    """Independent, reproducible ledger over immutable source coordinates."""
    records = []
    for g in item["evidence_groups"]:
        for ai, bundle in enumerate(g["alternatives"]):
            for e in bundle:
                records.append({"group_id": g["id"], "alternative": ai+1,
                                **{k: e[k] for k in ("doc_id", "document_sha256", "block_id", "block_char_start", "block_char_end")},
                                "flags": block_flags(corpus, e["doc_id"], e["block_id"])})
    return {"policy_version": POLICY_VERSION, "content_sha256": content_fingerprint(item), "records": records,
            "issues": content_problems(item, corpus), "human_checks_required": list(CONTENT_CHECKS)}


def content_problems(item, corpus):
    """Conservative objective checks. Semantic completeness still needs human review."""
    q = compact(item["user_input"])
    problems = []
    named = named_document(q, corpus)
    if item["query_style"] != ("document" if named else "natural"):
        problems.append("题面文件提示与query_style不一致")
    if re.search(r"发文字号|成文日期|起草单位|标准编号|哪[个项一]+(?:国家)?标准|哪个章节|哪一章|第几章|什么时候.*(?:生效|施行)", q):
        problems.append("不得考查文档元数据或目录导航")
    quotes = []
    for g in item["evidence_groups"]:
        for bundle in g["alternatives"]:
            flags = [block_flags(corpus, e["doc_id"], e["block_id"]) for e in bundle]
            if all(any(f in fs for f in ("reference_or_navigation", "drafting_metadata")) for fs in flags):
                problems.append("参考文献/起草信息不能独立支撑必要证据组")
            if any("damaged_formula" in fs for fs in flags):
                problems.append("损坏公式不可作为正式题目证据")
            if all("heading_only" in fs for fs in flags) and not re.search(r"(?:哪些|列举|举例).*(?:场景|类别)|(?:场景|类别).*(?:哪些|举例)", q):
                problems.append("标题证据只支持场景或类别存在，不能证明具体功能")
            quotes.extend(e["text"] for e in bundle)
    evidence = compact("\n".join(quotes))
    if item["expected_behavior"] == "answer":
        if re.search(r"具体(?:量化|数值|数字)|(?:多少|几)(?:个|家|元|%|天)", q) and "不少于一定数量" in evidence:
            problems.append("概括数量要求不能回答具体量化门槛")
        if re.search(r"(?:哪些|什么).{0,12}材料", q) and "标识相关材料" in evidence and not re.search(r"材料(?:包括|清单)", evidence):
            problems.append("标识相关材料的概括规定不能支持材料清单")
    # Complete an enumerated article when the question requests its measures, not a named subset.
    if re.search(r"可以采取哪些措施|有哪些核心权利", q):
        cited = {(e["doc_id"], e["block_id"]) for g in item["evidence_groups"] for a in g["alternatives"] for e in a}
        for doc_id, bid in cited:
            block = corpus.blocks[(doc_id, bid)]["text"]
            if "可以采取下列措施" not in block:
                continue
            for b in corpus.documents[doc_id]["blocks"]:
                if b["block_id"] <= bid:
                    continue
                if re.match(r"第.+条", b["text"]):
                    break
                if re.match(r"[（(][一二三四五六七八九十]+[）)]", b["text"]) and (doc_id, b["block_id"]) not in cited:
                    problems.append("枚举措施证据不完整，须补齐同条后续项或收窄题意")
    return sorted(set(problems))


def validate_v2(item, corpus, approved=False):
    forms = item.get("answer_form")
    if not isinstance(forms, list) or not forms or len(set(forms)) != len(forms) or not set(forms) <= set(ANSWER_FORMS):
        raise ValueError("Invalid answer_form")
    if item.get("evidence_span") not in EVIDENCE_SPANS:
        raise ValueError("Invalid evidence_span")
    if item.get("question_type") is not None:
        raise ValueError("v2 uses orthogonal labels; question_type belongs in provenance only")
    if item.get("difficulty") not in ([None] if boundary(item) else ["easy", "medium", "hard"]):
        raise ValueError("Invalid v2 difficulty")
    if not item.get("difficulty_reason", "").strip() or not item.get("scope_note", "").strip():
        raise ValueError("Scope and difficulty rationale required")
    groups = item["evidence_groups"]
    if (not groups) != (item["evidence_span"] == "none"):
        raise ValueError("evidence_span none must agree with empty evidence")
    mapping = item.get("point_evidence")
    if not isinstance(mapping, list) or len(mapping) != len(item["answer_points"]):
        raise ValueError("Each answer point needs a point_evidence entry")
    ids = {g["id"] for g in groups}
    if any(not isinstance(m, list) or not set(m) <= ids or (not m and not boundary(item)) for m in mapping):
        raise ValueError("Invalid point_evidence group references")
    if span(item) in ("within_document", "cross_document") and not item.get("multi_source_reason", "").strip():
        raise ValueError("Multiple evidence units require an irreducibility rationale")
    problems = content_problems(item, corpus)
    if problems:
        raise ValueError("Content checks: " + "; ".join(problems))
    if approved:
        if any(item.get("review_checks", {}).get(k) is not True for k in CONTENT_CHECKS):
            raise ValueError("All semantic human review checks must be true")
