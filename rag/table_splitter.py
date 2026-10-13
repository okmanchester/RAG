"""Budgeted semantic rendering while partitioning the original physical stream."""
from __future__ import annotations

import hashlib
import json
import re

from .retrieval import annotate_chunk
from .tables import TABLE_VERSION


def slice_refs(refs, start, end):
    result, offset = [], 0
    for source in refs:
        length = source["block_char_end"] - source["block_char_start"]
        left, right = max(start, offset), min(end, offset + length)
        if left < right:
            result.append({**source, "block_char_start": source["block_char_start"] + left - offset,
                           "block_char_end": source["block_char_start"] + right - offset})
        offset += length
    return result


def render_selection(view, ranges, start, end, title_prefix, count, maximum, allow_omission=True):
    blocks = {b["block_id"]: b for _, _, b in ranges}
    intervals = {b["block_id"]: (max(0, start - a), min(len(b["text"]), end - a))
                 for a, z, b in ranges if max(a, start) < min(z, end)}

    def intersects(source):
        bounds = intervals.get(source["block_id"])
        return bounds is not None and max(bounds[0], source["block_char_start"]) < min(bounds[1], source["block_char_end"])

    row_ids = [row for row in view["rows"] if any(b in intervals for b in row["block_ids"])]
    headers = {h["column"]: h for h in view["headers"]}
    first_fragment = view["fragments"][0]
    # Data fields already carry their complete column paths. Rendering the raw
    # header again spends tokens on duplicate labels and dilutes the embedding.
    context_ids = first_fragment["caption_block_ids"] + ([] if row_ids else first_fragment["header_block_ids"])
    context_text = "\n".join(blocks[b]["text"] for b in context_ids if b in blocks)
    context_allowed = bool(context_text) and count(title_prefix + context_text + "\n\n") <= maximum // 2

    def render(mode):
        parts, mappings, context_sources, warnings = [title_prefix], [], [], list(view["warnings"])
        position = len(title_prefix)

        def syntax(text):
            nonlocal position
            parts.append(text)
            position += len(text)

        def value(text, refs, operation="copy", repeated=False, equivalents=None):
            nonlocal position
            if not text:
                return
            begin = position
            syntax(text)
            mappings.append({"text_char_start": begin, "text_char_end": position,
                             "text": text, "operation": operation, "source_refs": refs,
                             "equivalent_sources": equivalents or []})
            offset = begin
            for source in refs:
                size = source["block_char_end"] - source["block_char_start"]
                if repeated or not intersects(source):
                    context_sources.append({**source, "text_char_start": offset,
                                            "text_char_end": offset + size, "is_repeated_context": True})
                offset += size

        if context_allowed and mode == 0:
            for bid in context_ids:
                if bid not in blocks:
                    continue
                block = blocks[bid]
                source = {"block_id": bid, **block["source"], "block_char_start": 0,
                          "block_char_end": len(block["text"])}
                value(block["text"], [source], repeated=bid not in intervals)
                syntax("\n")
        elif context_text:
            warnings.append("table_context_omitted_budget")

        displayed = False
        for row in row_ids:
            cells = [view["anchors"][aid] for aid in row["cell_ids"]]
            selected = {c["anchor_id"] for c in cells if any(intersects(s) for s in c["parts"])}
            if not selected:
                continue
            if displayed:
                syntax("\n")
            displayed = True
            used_labels = set()
            def field_order(cell):
                path = headers.get(cell["column"], {}).get("path", [])
                descriptive = bool(path and any(word in path[-1] for word in ("说明", "解释", "方法", "要求")))
                return descriptive, cell["column"]
            for cell in sorted(cells, key=field_order):
                touched = cell["anchor_id"] in selected
                inherited = not touched and (cell["logical_rowspan"] > 1 or cell["column"] == 1
                             or headers.get(cell["column"], {}).get("path", [""])[-1:] in (["一级指标"], ["二级指标"]))
                if not touched and (not inherited or mode > 0):
                    continue
                if not cell["text"]:
                    continue
                header = headers.get(cell["column"], {"label": f"第{cell['column']}列", "sources": [], "path": []})
                if mode < 2 and header["path"]:
                    path_sources = header["sources"]
                    # Header paths contain exact copied labels plus explicit
                    # generated separators; each copied component is mapped.
                    for i, source in enumerate(path_sources):
                        if i:
                            syntax("／")
                        block = blocks.get(source["block_id"])
                        if block is not None:
                            label = block["text"][source["block_char_start"]:source["block_char_end"]]
                            value(label, [source], "label", repeated=True)
                    label = header["label"]
                    if label in used_labels or "[列" in label:
                        syntax(f"[列{cell['column']}]")
                    used_labels.add(label)
                else:
                    syntax(f"第{cell['column']}列")
                    if mode == 2:
                        warnings.append("table_column_label_omitted_budget")
                        warnings.append("table_context_omitted_budget")
                syntax("：")
                refs = cell["source_groups"][0] if cell["operation"] == "extend_parent" else cell["parts"]
                equivalents = [s for group in cell["source_groups"][1:] for s in group] if cell["operation"] == "extend_parent" else []
                text = cell["text"]
                if touched and (cell["operation"] != "concat" or mode > 0):
                    # Render only the covered portion of an oversized cell.
                    # Concat cells normally render the whole continued value.
                    lo, hi, offset = len(text), 0, 0
                    for source in refs:
                        length = source["block_char_end"] - source["block_char_start"]
                        bounds = intervals.get(source["block_id"])
                        if bounds:
                            a, b = max(bounds[0], source["block_char_start"]), min(bounds[1], source["block_char_end"])
                            if a < b:
                                lo, hi = min(lo, offset + a - source["block_char_start"]), max(hi, offset + b - source["block_char_start"])
                        offset += length
                    if hi > lo:
                        text, refs = text[lo:hi], slice_refs(refs, lo, hi)
                value(text, refs, "inherit" if inherited else cell["operation"], inherited, equivalents)
                syntax("；")
            syntax("\n")
        # Footnotes and header-only slices remain searchable. Physical repeated
        # headers are retained in body even when only the canonical header is rendered.
        for bid, (lo, hi) in intervals.items():
            block = blocks[bid]
            if block.get("cells") and not block.get("is_table_header"):
                continue
            if mode == 0 and context_allowed and (bid in context_ids or block.get("is_table_header")
                                                   or block.get("is_table_caption")):
                continue
            if row_ids and (block.get("is_table_header") or block.get("is_table_caption")):
                continue
            source = {"block_id": bid, **block["source"], "block_char_start": lo, "block_char_end": hi}
            value(block["text"][lo:hi], [source])
            syntax("\n")
        if mode > 0:
            warnings.append("table_parent_context_omitted_budget")
        # A range can cover separators only; map a retained physical character
        # rather than manufacture a source value for it.
        if not mappings:
            for bid, (lo, hi) in intervals.items():
                if hi > lo:
                    source = {"block_id": bid, **blocks[bid]["source"], "block_char_start": lo, "block_char_end": hi}
                    value(blocks[bid]["text"][lo:hi], [source])
                    break
        return "".join(parts), mappings, context_sources, sorted(set(warnings))

    for mode in range(3 if allow_omission else 1):
        rendered = render(mode)
        if count(rendered[0]) <= maximum:
            return rendered
    return rendered


def chunk_table(document, section, group, group_index, view, count, maximum, overlap, title_prefix, strategy):
    from .splitter import SEPARATORS, source_spans
    blocks, ranges, parts, position = group["blocks"], [], [], 0
    for block in blocks:
        if parts:
            parts.append("\n\n")
            position += 2
        ranges.append((position, position + len(block["text"]), block))
        parts.append(block["text"])
        position += len(block["text"])
    physical = "".join(parts)
    cache = {}
    def rendered(a, b, allow_omission=False):
        key = a, b, allow_omission
        if key not in cache:
            cache[key] = render_selection(view, ranges, a, b, title_prefix, count, maximum, allow_omission)
        return cache[key]
    def fits(a, b):
        return count(rendered(a, b)[0]) <= maximum

    def split_span(a, b, level=0):
        if fits(a, b):
            return [(a, b)]
        if level < len(SEPARATORS):
            boundaries = sorted({a, b, *(a + m.end() for m in re.finditer(SEPARATORS[level], physical[a:b]))})
            return [unit for lo, hi in zip(boundaries, boundaries[1:]) for unit in split_span(lo, hi, level + 1)]
        result = []
        while a < b:
            allow_omission = not fits(a, a + 1)
            low, high, best = a + 1, b, None
            while low <= high:
                middle = (low + high) // 2
                if count(rendered(a, middle, allow_omission)[0]) <= maximum:
                    best, low = middle, middle + 1
                else:
                    high = middle - 1
            if best is None:
                raise ValueError("table title prefix leaves no space for a source character and column label")
            result.append((a, best))
            a = best
        return result

    units = []
    for i, (a, b, block) in enumerate(ranges):
        end = ranges[i + 1][0] if i + 1 < len(ranges) else b
        # Prefer complete logical records, then physical cell boundaries.
        if fits(a, end):
            units.append((a, end))
        else:
            boundaries = sorted({a, end, *(a + c["char_start"] for c in block.get("cells", []))})
            units.extend(unit for lo, hi in zip(boundaries, boundaries[1:]) for unit in split_span(lo, hi))
    chunks, cursor, start = [], 0, 0
    while cursor < len(units):
        end = units[cursor][1]
        cursor += 1
        while cursor < len(units) and fits(start, units[cursor][1]):
            end = units[cursor][1]
            cursor += 1
        body = physical[start:end]
        if not body.strip():
            # Separator-only tails attach to the preceding physical body.
            if not chunks:
                raise ValueError("table contains no physical text")
            previous = chunks[-1]
            previous["body"] += body
            previous["section_char_end"] = group["start"] + end
            previous["body_token_count"] = count(previous["body"])
            start = end
            continue
        text, mappings, context_sources, warnings = rendered(start, end, True)
        spans = source_spans(ranges, start, end, start)
        split_ids = [block["block_id"] for a, b, block in ranges
                     if max(a, start) < min(b, end) and (start > a or end < b)]
        chunk = {"schema_version": 2, "doc_id": document["doc_id"], "source_path": document["source_path"],
                 "category": document["category"], "title": document["title"], "section_id": section["section_id"],
                 "heading_path": section["heading_path"], "heading_block_ids": section["heading_block_ids"],
                 "structure_type": "table", "table_id": view["table_id"], "table_view_version": TABLE_VERSION,
                 "group_index": group_index, "body": body, "text": text,
                 "title_prefix_char_count": len(title_prefix), "token_count": count(text),
                 "body_token_count": count(body), "overlap_token_count": 0, "overlap_char_count": 0,
                 "section_char_start": group["start"] + start, "section_char_end": group["start"] + end,
                 "sources": spans, "context_sources": context_sources, "derived_sources": mappings,
                 "structure_split": bool(split_ids), "split_block_ids": split_ids, "warnings": warnings,
                 "asset_refs": [{"path": asset, "artifact_dir": document.get("artifacts")}
                                for asset in sorted({asset for b in blocks for asset in b.get("assets", [])})]}
        identity = {"strategy": strategy, "maximum": maximum, "overlap": overlap, **chunk}
        chunk["chunk_id"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:24]
        annotate_chunk(chunk, {b["block_id"] for b in blocks if b.get("role") == "toc"})
        chunks.append(chunk)
        start = end
    assert "".join(c["body"] for c in chunks) == physical, "table splitting changed physical text"
    return chunks
