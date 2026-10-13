"""Logical tables over immutable physical cells, with replayable decisions.

No dependency-private renderer is used. All visible values retain exact ranges
into normalized Middle JSON blocks; synthesized labels are tracked separately.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
import math

TABLE_VERSION = "logical_tables_v1"
LABELS = {
    "名称", "说明", "分类", "序号", "单位", "指标", "一级指标", "二级指标",
    "项目", "更新频率", "指标解释", "要求", "内容", "标准", "备注", "字段",
    "长度", "长度(B)", "示例数据", "编码", "编码名称", "编码意义", "安全要求",
    "条款", "测评方法", "重点采集项", "评价要求", "必选/可选", "权重",
    "指标体系", "数值", "指标名称", "类别", "类型", "基本级", "增强级",
}


def compact(text):
    return re.sub(r"\s+", "", text).replace("（", "(").replace("）", ")")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def physical_fragments(blocks):
    groups = {}
    for block in blocks:
        if block.get("structure_type") == "table":
            info = block.get("table_info", {})
            key = info.get("fragment_id", block.get("group_id", str(block["block_id"])))
            groups.setdefault(key, []).append(block)
    return list(groups.items())


def annotate_headers(blocks):
    """Conservative leading-header detection; never promote an arbitrary row."""
    previous = None
    for _, fragment in physical_fragments(blocks):
        rows = [b for b in fragment if b.get("cells")]
        if not rows:
            continue
        first = rows[0]
        texts = [compact(c["text"]) for c in first["cells"]]
        signature = [(c["column"], c["colspan"], compact(c["text"])) for c in first["cells"]]
        info = first.get("table_info", {})
        repeated = (previous is not None and info.get("continues_prev") is True
                    and first["source"].get("page_idx") == previous[0] + 1
                    and signature == previous[1] and any(t in LABELS for t in texts))
        known = len(texts) >= 2 and all(t in LABELS for t in texts)
        recognized = first.get("is_table_header") or repeated or known
        header_end = first["source"].get("row", 1) - 1
        if recognized:
            header_end = max(c["row"] + c["rowspan"] - 1 for c in first["cells"])
        for row in rows:
            number = row["source"]["row"]
            explicit = row.get("is_table_header") and row.get("header_detection") == "html"
            subordinate = (recognized and number == header_end + 1 and len(row["cells"]) >= 2
                           and all(compact(c["text"]) in LABELS for c in row["cells"]))
            if explicit or number <= header_end or subordinate:
                row["is_table_header"] = True
                row["header_detection"] = ("html" if explicit else "repeated_structure" if repeated
                                           else "column_labels")
                header_end = max(header_end, *(c["row"] + c["rowspan"] - 1 for c in row["cells"]))
            else:
                break
        previous = (first["source"]["page_idx"], signature)


def ref(block, cell=None):
    return {"block_id": block["block_id"], **block["source"],
            "block_char_start": cell["char_start"] if cell else 0,
            "block_char_end": cell["char_end"] if cell else len(block["text"])}


def caption_key(text):
    text = compact(text)
    return re.sub(r"(?:\(续(?:表)?\)|续表)$", "", text)


def make_fragment(key, blocks, section_id):
    rows = [b for b in blocks if b.get("cells")]
    headers = [b for b in rows if b.get("is_table_header")]
    captions = [b for b in blocks if b.get("is_table_caption")]
    anchors, visual_rows, occupied = {}, [], {}
    for block in rows:
        number = block["source"]["row"]
        ids = set(occupied.get(number, {}).values())
        for cell in block["cells"]:
            aid = f"{block['block_id']}:{cell['column']}"
            anchors[aid] = {"anchor_id": aid, "column": cell["column"], "colspan": cell["colspan"],
                            "rowspan": cell["rowspan"], "text": cell["text"],
                            "parts": [ref(block, cell)], "source_groups": [[ref(block, cell)]], "operation": "copy"}
            for r in range(number, number + cell["rowspan"]):
                for col in range(cell["column"], cell["column"] + cell["colspan"]):
                    occupied.setdefault(r, {})[col] = aid
            ids.add(aid)
        visual_rows.append({"block_ids": [block["block_id"]], "physical_row": number,
                            "is_header": bool(block.get("is_table_header")),
                            "cell_ids": sorted(ids, key=lambda a: anchors[a]["column"])})
    return {"fragment_id": key, "section_id": section_id,
            "page_idx": blocks[0]["source"]["page_idx"], "block_ids": [b["block_id"] for b in blocks],
            "header_block_ids": [b["block_id"] for b in headers],
            "caption_block_ids": [b["block_id"] for b in captions],
            "caption": captions[0]["text"] if captions else "",
            "column_count": max((a["column"] + a["colspan"] - 1 for a in anchors.values()), default=0),
            "continues_prev": blocks[0].get("table_info", {}).get("continues_prev") is True,
            "native_cell_merge": blocks[0].get("table_info", {}).get("cell_merge"),
            "anchors": anchors, "rows": visual_rows}


def header_signature(fragment):
    return [[[fragment["anchors"][a]["column"], fragment["anchors"][a]["colspan"],
              compact(fragment["anchors"][a]["text"])] for a in row["cell_ids"]]
            for row in fragment["rows"] if row["is_header"]]


def compatible(previous, current):
    if not current["continues_prev"]:
        return False, None
    if previous["section_id"] != current["section_id"]:
        return False, "table_continuation_section_boundary"
    if current["page_idx"] != previous["page_idx"] + 1:
        return False, "table_continuation_nonadjacent_page"
    if previous["column_count"] != current["column_count"] or not current["column_count"]:
        return False, "table_continuation_column_mismatch"
    left, right = previous["caption"], current["caption"]
    if left and right and caption_key(left) != caption_key(right):
        return False, "table_continuation_caption_mismatch"
    left_head, right_head = header_signature(previous), header_signature(current)
    if left_head and right_head and left_head != right_head:
        return False, "table_continuation_header_mismatch"
    return True, None


def columns(row, anchors):
    result = {}
    for aid in row["cell_ids"]:
        cell = anchors[aid]
        for col in range(cell["column"], cell["column"] + cell["colspan"]):
            result[col] = cell
    return result


def column_headers(fragment):
    result = []
    for col in range(1, fragment["column_count"] + 1):
        path, sources = [], []
        for row in fragment["rows"]:
            if not row["is_header"]:
                continue
            cell = columns(row, fragment["anchors"]).get(col)
            if cell and compact(cell["text"]) not in [compact(x) for x in path]:
                path.append(cell["text"])
                sources.extend(cell["parts"])
        result.append({"column": col, "path": path, "sources": sources})
    # Flat repeated labels belong to the nearest preceding metric group.
    names = Counter(tuple(h["path"]) for h in result if h["path"])
    owner = None
    for header in result:
        path = header["path"]
        if path and compact(path[-1]) in {"一级指标", "二级指标"}:
            owner = header
        elif path and names[tuple(path)] > 1 and owner:
            header["path"] = owner["path"] + path
            header["sources"] = owner["sources"] + header["sources"]
    # Colspan siblings with identical paths still have distinct visual columns.
    labels = Counter(tuple(h["path"]) for h in result)
    for header in result:
        header["label"] = "／".join(header["path"]) or f"第{header['column']}列"
        if header["path"] and labels[tuple(header["path"])] > 1:
            header["label"] += f"[列{header['column']}]"
    return result


def boundary_context(previous, current, headers):
    prev_rows = [r for r in previous["rows"] if not r["is_header"]]
    curr_rows = [r for r in current["rows"] if not r["is_header"]]
    def values(rows, anchors):
        return [[columns(row, anchors).get(col, {}).get("text", "")
                 for col in range(1, current["column_count"] + 1)] for row in rows]
    return {"previous_fragment": previous["fragment_id"], "current_fragment": current["fragment_id"],
            "column_count": current["column_count"], "headers": headers,
            "previous_records": values(prev_rows[-2:], previous["anchors"]),
            "current_records": values(curr_rows[:2], current["anchors"]),
            "previous_cells": [previous["anchors"][a] for a in prev_rows[-1]["cell_ids"]] if prev_rows else [],
            "current_cells": [current["anchors"][a] for a in curr_rows[0]["cell_ids"]] if curr_rows else [],
            "native_cell_merge": current["native_cell_merge"],
            "constraints": ["长度(B)是编码字节长度，不是示例字符串字符数",
                            "只能拼接已有文字，不能补写或改写；原生标志只是候选证据"]}


def rule_decision(context):
    prev, curr = context["previous_cells"], context["current_cells"]
    left = {col: c for c in prev for col in range(c["column"], c["column"] + c["colspan"])}
    right = {col: c for c in curr for col in range(c["column"], c["column"] + c["colspan"])}
    parents = [col for col in sorted(left.keys() & right.keys())
               if left[col]["text"] and compact(left[col]["text"]) == compact(right[col]["text"])
               and (left[col]["rowspan"] > 1 or right[col]["rowspan"] > 1)
               and left[col]["column"] == right[col]["column"]
               and left[col]["colspan"] == right[col]["colspan"]]
    # A date/time prefix + interval suffix + unfinished explanation provides
    # independent evidence. Never compare the printed byte length to len(text).
    values = context["previous_records"][-1] if context["previous_records"] else []
    following = context["current_records"][0] if context["current_records"] else []
    labels = [compact(h["label"]) for h in context["headers"]]
    timestamp = (len(values) == len(following) == 4 and labels[0] == "字段"
                 and labels[2] == "示例数据" and compact(values[0]) == "时间戳"
                 and not following[0] and not following[1]
                 and values[2].isdigit() and following[2].isdigit()
                 and len(values[2]) >= 14 and values[3].endswith("分")
                 and re.match(r"\d+秒", following[3]) is not None
                 and len(context["current_records"]) > 1 and bool(context["current_records"][1][0]))
    if timestamp:
        try:
            datetime.strptime(values[2][:14], "%Y%m%d%H%M%S")
        except ValueError:
            timestamp = False
    if timestamp:
        return {"kind": "continue_row", "concat_columns": [3, 4], "parent_columns": [],
                "basis": "timestamp_prefix_interval_and_sentence_continuation"}
    if parents:
        return {"kind": "extend_parent", "concat_columns": [], "parent_columns": parents,
                "basis": "same_value_and_rowspan_anchor"}
    uncertain = bool(following and not following[0] and any(following))
    return {"kind": "uncertain" if uncertain else "new_record", "concat_columns": [],
            "parent_columns": [], "basis": "insufficient_evidence" if uncertain else "record_key_present"}


def validate_decision(decision, context):
    """Reject model flags that conflict with actual structure or remove data."""
    if not isinstance(decision, dict) or decision.get("kind") not in {
            "new_record", "continue_row", "extend_parent", "uncertain"}:
        raise ValueError("invalid boundary decision kind")
    for name in ("concat_columns", "parent_columns"):
        values = decision.get(name)
        if not isinstance(values, list) or any(type(c) is not int or not 1 <= c <= context["column_count"] for c in values):
            raise ValueError("invalid boundary columns")
        if len(set(values)) != len(values):
            raise ValueError("duplicate boundary columns")
    if set(decision["concat_columns"]) & set(decision["parent_columns"]):
        raise ValueError("conflicting boundary operations")
    rule = rule_decision(context)
    if not set(decision["parent_columns"]) <= set(rule["parent_columns"]):
        raise ValueError("parent extension lacks same-value rowspan evidence")
    kind = decision["kind"]
    if kind in {"new_record", "uncertain"} and (decision["concat_columns"] or decision["parent_columns"]):
        raise ValueError("nonmerge decision contains merge operations")
    if kind == "extend_parent" and (decision["concat_columns"] or not decision["parent_columns"]):
        raise ValueError("invalid parent-only decision")
    if kind == "continue_row":
        if not decision["concat_columns"] or not context["current_records"]:
            raise ValueError("continuation requires nonempty data columns")
        first = context["current_records"][0]
        if first[0]:
            raise ValueError("new record key cannot be merged")
        nonempty = {i + 1 for i, value in enumerate(first) if value}
        if not nonempty <= set(decision["concat_columns"] + decision["parent_columns"]):
            raise ValueError("continuation would leave unmerged nonempty cells")
        if 1 in decision["concat_columns"] or any(not first[c - 1] for c in decision["concat_columns"]):
            raise ValueError("continuation must join actual nonempty data")
        # Inherited parents represent separate child records, not a broken row.
        if context["previous_cells"] and context["previous_cells"][0]["rowspan"] > 1:
            raise ValueError("inherited parent is not a record continuation")
        left = {c["column"]: c for c in context["previous_cells"]}
        right = {c["column"]: c for c in context["current_cells"]}
        for col in decision["concat_columns"]:
            if col not in left or col not in right or left[col]["colspan"] != right[col]["colspan"]:
                raise ValueError("continuation column does not have matching anchors")
    return {"kind": kind, "concat_columns": sorted(decision["concat_columns"]),
            "parent_columns": sorted(decision["parent_columns"]), "basis": str(decision.get("basis", "llm_candidate"))[:128]}


def apply_geometry(fragment, geometry):
    """Reorder only a complete, positioned partition of an existing cell.

    Optional geometry is saved alongside the native result. Table/container
    bboxes alone are insufficient and never trigger text repair.
    """
    warnings = []
    for item in geometry:
        aid = f"{item.get('block_id')}:{item.get('column')}"
        cell = fragment["anchors"].get(aid)
        if cell is None:
            continue
        try:
            source = cell["parts"][0]
            segments = item["segments"]
            bbox = item["cell_bbox"]
            if len(bbox) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in bbox):
                raise ValueError("invalid cell bbox")
            if not 0 <= bbox[0] < bbox[2] <= 1 or not 0 <= bbox[1] < bbox[3] <= 1:
                raise ValueError("cell bbox must use normalized coordinates")
            cursor = source["block_char_start"]
            for segment in sorted(segments, key=lambda s: s["block_char_start"]):
                lo, hi = segment["block_char_start"], segment["block_char_end"]
                box = segment["bbox"]
                if type(lo) is not int or type(hi) is not int or lo != cursor or not lo < hi <= source["block_char_end"]:
                    raise ValueError("geometry must exactly partition the physical cell")
                if (len(box) != 4 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in box)
                        or not bbox[0] <= box[0] < box[2] <= bbox[2]
                        or not bbox[1] <= box[1] < box[3] <= bbox[3]):
                    raise ValueError("segment bbox outside cell")
                cursor = hi
            if not segments or cursor != source["block_char_end"]:
                raise ValueError("incomplete cell geometry")
            tolerance = min(s["bbox"][3] - s["bbox"][1] for s in segments) / 2
            lines = []
            for segment in sorted(segments, key=lambda s: (s["bbox"][1], s["bbox"][0])):
                if not lines or abs(segment["bbox"][1] - lines[-1][0]) > tolerance:
                    lines.append((segment["bbox"][1], []))
                lines[-1][1].append(segment)
            ordered = [s for _, line in lines for s in sorted(line, key=lambda s: s["bbox"][0])]
            original = cell["text"]
            cell["text"] = "".join(original[s["block_char_start"] - source["block_char_start"]:
                                              s["block_char_end"] - source["block_char_start"]] for s in ordered)
            cell["parts"] = [{**source, "block_char_start": s["block_char_start"],
                              "block_char_end": s["block_char_end"]} for s in ordered]
            cell["source_groups"] = [copy.deepcopy(cell["parts"])]
            cell["operation"] = "reorder"
        except (KeyError, TypeError, ValueError, IndexError):
            warnings.append("table_cell_geometry_requires_review")
    return warnings


def structural_warnings(view):
    warnings = []
    unit_cols = [h["column"] for h in view["headers"] if h["path"] and compact(h["path"][-1]) == "单位"]
    description_cols = [h["column"] for h in view["headers"] if h["path"] and compact(h["path"][-1]) in {"说明", "指标解释"}]
    for cell in view["anchors"].values():
        if cell["column"] + cell["colspan"] - 1 > view["column_count"]:
            warnings.append("table_column_overflow")
        if cell["operation"] != "reorder" and re.match(r"^本.{1,3}。次", cell["text"]):
            warnings.append("table_text_order_requires_review")
    for previous, current in zip(view["rows"], view["rows"][1:]):
        left, right = columns(previous, view["anchors"]), columns(current, view["anchors"])
        conflict = any(left.get(c, {}).get("text") and right.get(c, {}).get("text")
                       and left[c]["text"] != right[c]["text"] for c in unit_cols)
        continuation = any(left.get(c, {}).get("text", "").endswith("行业")
                           and right.get(c, {}).get("text", "").startswith("应用，") for c in description_cols)
        if conflict and continuation:
            warnings.append("table_row_structure_requires_review")
    return warnings


def build_table_views(document, sections, decisions=None):
    """Pure derived view. Saved model candidates never trigger network calls."""
    decisions = decisions or {}
    views, previous = [], None
    for section in sections:
        for key, blocks in physical_fragments(section["blocks"]):
            current = make_fragment(key, blocks, section["section_id"])
            geometry_warnings = apply_geometry(current, document.get("table_geometry", []))
            join, warning = compatible(previous, current) if previous else (False, None)
            # Intervening substantive blocks must not be reordered or hidden.
            if join:
                ids = [b["block_id"] for b in section["blocks"]]
                if ids.index(previous["block_ids"][-1]) + 1 != ids.index(current["block_ids"][0]):
                    join, warning = False, "table_continuation_intervening_content"
                canonical = views[-1]
                if (join and canonical["header_signature"] and header_signature(current)
                        and canonical["header_signature"] != header_signature(current)):
                    join, warning = False, "table_continuation_header_mismatch"
                if (join and canonical["caption"] and current["caption"]
                        and caption_key(canonical["caption"]) != caption_key(current["caption"])):
                    join, warning = False, "table_continuation_caption_mismatch"
            if not join:
                ident = {"version": TABLE_VERSION, "doc_id": document["doc_id"], "fragment": key,
                         "section": section["section_id"]}
                view = {"table_id": fingerprint(ident)[:24], "section_id": section["section_id"],
                        "column_count": current["column_count"], "headers": column_headers(current),
                        "header_signature": header_signature(current), "caption": current["caption"],
                        "fragments": [], "rows": [], "anchors": {}, "operations": [],
                        "warnings": [warning] if warning else []}
                views.append(view)
            else:
                view = views[-1]
            view["anchors"].update(copy.deepcopy(current["anchors"]))
            view["warnings"].extend(geometry_warnings)
            data_rows = copy.deepcopy([r for r in current["rows"] if not r["is_header"]])
            if join:
                context = boundary_context(previous, current, view["headers"])
                cid = fingerprint(context)
                candidate = decisions.get(cid)
                chosen = rule_decision(context)
                status = "rule"
                if candidate is not None:
                    try:
                        if candidate.get("status") != "ok":
                            raise ValueError("failed model request")
                        if candidate.get("context_id") != cid or candidate.get("input") != context:
                            raise ValueError("model decision does not match its boundary context")
                        model = validate_decision(candidate["decision"], context)
                        # Explicit disagreement with a supported rule is visible
                        # and cannot silently replace it.
                        if chosen["kind"] != "uncertain" and any(model[k] != chosen[k] for k in (
                                "kind", "concat_columns", "parent_columns")):
                            raise ValueError("model conflicts with deterministic evidence")
                        chosen, status = model, "llm_validated"
                    except (KeyError, TypeError, ValueError):
                        chosen = {"kind": "uncertain", "concat_columns": [], "parent_columns": [],
                                  "basis": "model_failed_invalid_or_conflicting"}
                        status = "rejected"
                operation = {"context_id": cid, "input": context, "decision": chosen, "status": status,
                             "previous_fragment": previous["fragment_id"], "current_fragment": key}
                if chosen["kind"] == "uncertain":
                    view["warnings"].append("table_boundary_requires_review")
                if data_rows and view["rows"]:
                    last, first = view["rows"][-1], data_rows[0]
                    left, right = columns(last, view["anchors"]), columns(first, view["anchors"])
                    aliases = {}
                    for col in chosen["parent_columns"]:
                        a, b = left[col], right[col]
                        if b["anchor_id"] in aliases:
                            continue
                        a["parts"].extend(b["parts"])
                        a["source_groups"].extend(b["source_groups"])
                        a["operation"] = "extend_parent"
                        aliases[b["anchor_id"]] = a["anchor_id"]
                    for col in chosen["concat_columns"]:
                        a, b = left[col], right[col]
                        if b["anchor_id"] in aliases:
                            continue
                        a["text"] += b["text"]
                        a["parts"].extend(b["parts"])
                        a["source_groups"] = [copy.deepcopy(a["parts"])]
                        a["operation"] = "concat"
                        aliases[b["anchor_id"]] = a["anchor_id"]
                    for row in data_rows:
                        row["cell_ids"] = list(dict.fromkeys(aliases.get(a, a) for a in row["cell_ids"]))
                    if chosen["kind"] == "continue_row":
                        last["block_ids"].extend(first["block_ids"])
                        last["cell_ids"] = list(dict.fromkeys(last["cell_ids"] + first["cell_ids"]))
                        # Empty key placeholders have physical provenance but no
                        # independent semantic cell in the continued record.
                        last["cell_ids"] = [a for a in last["cell_ids"] if view["anchors"][a]["text"]]
                        data_rows = data_rows[1:]
                    operation["aliases"] = aliases
                view["operations"].append(operation)
            view["rows"].extend(data_rows)
            view["fragments"].append({k: v for k, v in current.items() if k not in ("anchors", "rows")})
            previous = current
    for view in views:
        used = {aid for row in view["rows"] for aid in row["cell_ids"]}
        view["anchors"] = {a: c for a, c in view["anchors"].items() if a in used}
        for i, row in enumerate(view["rows"], 1):
            row["row_id"] = i
        for aid, cell in view["anchors"].items():
            positions = [r["row_id"] for r in view["rows"] if aid in r["cell_ids"]]
            cell["logical_row"] = min(positions)
            cell["logical_rowspan"] = max(positions) - min(positions) + 1
        view["logical_row_count"] = len(view["rows"]) + len(view["fragments"][0]["header_block_ids"])
        view["warnings"] = sorted(set(view["warnings"] + structural_warnings(view)))
    if decisions:
        contexts = {o["context_id"] for view in views for o in view["operations"]}
        if set(decisions) != contexts:
            raise ValueError("saved table decisions do not cover the actual boundaries; refresh required")
    return views


def validate_views(document, views):
    blocks = {b["block_id"]: b for b in document["blocks"]}
    for view in views:
        for cell in view["anchors"].values():
            texts = []
            for source in cell["parts"]:
                block = blocks[source["block_id"]]
                assert all(source[k] == v for k, v in block["source"].items()), "table source location mismatch"
                lo, hi = source["block_char_start"], source["block_char_end"]
                assert 0 <= lo <= hi <= len(block["text"]), "invalid table source range"
                texts.append(block["text"][lo:hi])
            if cell["operation"] == "extend_parent":
                assert [s for group in cell["source_groups"] for s in group] == cell["parts"], "parent source groups mismatch"
                for group in cell["source_groups"]:
                    value = "".join(blocks[s["block_id"]]["text"][s["block_char_start"]:s["block_char_end"]] for s in group)
                    assert compact(value) == compact(cell["text"]), "parent value mismatch"
            else:
                assert "".join(texts) == cell["text"], "logical cell source reconstruction mismatch"
