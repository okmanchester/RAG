"""Recursive splitting with exact tokenizer checks and character-level provenance."""
from __future__ import annotations

import hashlib
import json
import re
from functools import lru_cache

from .retrieval import annotate_chunk

DEFAULT_MODEL = "D:/WorkSpace/AIModels/BAAI/bge-large-zh-v1.5"
SEPARATORS = (r"\n\s*\n|\n", r"(?<=[。！？!?])|(?<=\.)\s+", r"(?<=[，,；;：:、])|\s+")


class TokenCounter:
    def __init__(self, model_path=DEFAULT_MODEL):
        from transformers import AutoTokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(model_path, local_files_only=True)

    @lru_cache(maxsize=8192)
    def __call__(self, text: str) -> int:
        return len(self.tokenizer.encode(text, add_special_tokens=False, truncation=False))


def cuts(text: str, pattern: str) -> list[int]:
    return sorted({0, len(text), *(match.end() for match in re.finditer(pattern, text))})


def recursive_units(text: str, fits, level=0, offset=0) -> list[tuple[int, int]]:
    if fits(text):
        return [(offset, offset + len(text))]
    if level < len(SEPARATORS):
        boundaries = cuts(text, SEPARATORS[level])
        result = []
        for a, b in zip(boundaries, boundaries[1:]):
            result.extend(recursive_units(text[a:b], fits, level + 1, offset + a))
        return result
    # Slice original characters, never decode token IDs (which can alter text).
    result, start = [], 0
    while start < len(text):
        low, high, best = 1, len(text) - start, 0
        while low <= high:
            middle = (low + high) // 2
            if fits(text[start:start + middle]):
                best, low = middle, middle + 1
            else:
                high = middle - 1
        if not best:
            raise ValueError("title prefix leaves no space for the next body character")
        result.append((offset + start, offset + start + best))
        start += best
    return result


def overlap_start(text, start, end, next_end, prefix, count, maximum, overlap):
    if overlap == 0:
        return end
    tail = text[start:end]
    for pattern in (*SEPARATORS, None):
        boundaries = cuts(tail, pattern) if pattern else range(len(tail) + 1)
        for relative in boundaries:
            absolute = start + relative
            suffix = text[absolute:end]
            if suffix.strip() and count(suffix) <= overlap and count(prefix + text[absolute:next_end]) <= maximum:
                return absolute
    return end


def source_spans(ranges, start, end, overlap_end):
    spans = []
    for a, b, block in ranges:
        lo, hi = max(a, start), min(b, end)
        if lo >= hi:
            continue
        boundaries = sorted({lo, hi, max(lo, min(hi, overlap_end))})
        for left, right in zip(boundaries, boundaries[1:]):
            span = {"block_id": block["block_id"], **block["source"],
                          "block_char_start": left - a, "block_char_end": right - a,
                          "body_char_start": left - start, "body_char_end": right - start,
                          "is_overlap": right <= overlap_end}
            if block.get("cells"):
                span["cell_spans"] = [{"row": c["row"], "column": c["column"],
                    "rowspan": c["rowspan"], "colspan": c["colspan"],
                    "cell_char_start": max(left - a, c["char_start"]) - c["char_start"],
                    "cell_char_end": min(right - a, c["char_end"]) - c["char_start"]}
                    for c in block["cells"] if max(left - a, c["char_start"]) < min(right - a, c["char_end"])]
            spans.append(span)
    return spans


STRATEGY = "mineru_structure_v2"


def chunk_document(document, sections, count, maximum=400, overlap=50):
    if maximum <= 0 or not 0 <= overlap < maximum:
        raise ValueError("require max-tokens > overlap-tokens >= 0")
    chunks = []
    for section in sections:
        title_prefix = "\n".join([document["title"], *section["heading_path"]]) + "\n\n"
        if count(title_prefix) >= maximum:
            raise ValueError(f"section {section['section_id']}: title prefix exhausts token budget")
        # Split each section into consecutive structural groups. Tables, code,
        # formulas and image annotations never mingle with neighboring prose.
        groups, section_position = [], 0
        for block in section["blocks"]:
            structure = block.get("structure_type", "body")
            key = (structure, block.get("group_id", block["block_id"])) if structure != "body" else ("body",)
            if not groups or groups[-1]["key"] != key:
                groups.append({"key": key, "blocks": [], "start": section_position})
            groups[-1]["blocks"].append(block)
            section_position += len(block["text"]) + 2
        for group_index, group in enumerate(groups, 1):
            structure, blocks = group["key"][0], group["blocks"]
            ranges, parts, position = [], [], 0
            for b in blocks:
                if parts:
                    parts.append("\n\n")
                    position += 2
                ranges.append((position, position + len(b["text"]), b))
                parts.append(b["text"])
                position += len(b["text"])
            text = "".join(parts)
            headers = [b for b in blocks if b.get("is_table_header") or b.get("is_table_caption")]
            table_context = "\n".join(b["text"] for b in headers) if structure == "table" else ""
            prefix = title_prefix
            context_sources = []
            context_omitted = False
            if table_context:
                candidate = prefix + table_context + "\n\n"
                # Leave at least one real body character. Oversized context is
                # not truncated: its complete text remains in the body stream.
                if count(candidate) <= maximum - 8:
                    prefix = candidate
                    offset = len(title_prefix)
                    for b in headers:
                        context_sources.append({"block_id": b["block_id"], **b["source"],
                            "block_char_start": 0, "block_char_end": len(b["text"]),
                            "text_char_start": offset, "text_char_end": offset + len(b["text"]),
                            "is_repeated_context": True})
                        offset += len(b["text"]) + 1
                else:
                    context_omitted = True
            fits = lambda body: count(prefix + body) <= maximum
            units = []
            for i, (a, b, block) in enumerate(ranges):
                end = ranges[i + 1][0] if i + 1 < len(ranges) else b
                # Large rows split first on cell boundaries; recursive text
                # splitting handles a single cell that still exceeds budget.
                if structure == "table" and not fits(text[a:end]) and block.get("cells"):
                    boundaries = sorted({a, end, *(a + c["char_start"] for c in block["cells"])})
                    for left, right in zip(boundaries, boundaries[1:]):
                        units.extend(recursive_units(text[left:right], fits, offset=left))
                else:
                    units.extend(recursive_units(text[a:end], fits, offset=a))
            cursor, start, previous_end, group_chunks = 0, 0, 0, []
            while cursor < len(units):
                end = units[cursor][1]
                if not fits(text[start:end]):
                    raise AssertionError("overlap exceeded available budget")
                cursor += 1
                while cursor < len(units) and fits(text[start:units[cursor][1]]):
                    end = units[cursor][1]
                    cursor += 1
                body = text[start:end]
                if not body.strip():
                    raise ValueError("empty body after splitting")
                spans = source_spans(ranges, start, end, previous_end)
                split_ids = [b["block_id"] for a, z, b in ranges
                             if max(a, start) < min(z, end) and (start > a or end < z)]
                assets = sorted({asset for b in blocks for asset in b.get("assets", [])})
                identity = {"strategy": STRATEGY, "parser": document.get("parser_adapter", "mineru_middle_v2"),
                    "doc_id": document["doc_id"], "section": section["section_id"], "group": group_index,
                    "start": start, "end": end, "maximum": maximum, "overlap": overlap,
                    "text": prefix + body, "sources": spans, "context_sources": context_sources}
                chunk = {"schema_version": 2, "chunk_id": hashlib.sha256(json.dumps(identity, ensure_ascii=False,
                    sort_keys=True).encode()).hexdigest()[:24], "doc_id": document["doc_id"],
                    "chunk_index": len(chunks) + 1, "source_path": document["source_path"],
                    "category": document["category"], "title": document["title"],
                    "section_id": section["section_id"], "heading_path": section["heading_path"],
                    "heading_block_ids": section["heading_block_ids"], "structure_type": structure,
                    "group_index": group_index, "body": body, "text": prefix + body,
                    "title_prefix_char_count": len(title_prefix), "token_count": count(prefix + body),
                    "body_token_count": count(body), "overlap_token_count": count(text[start:previous_end]),
                    "overlap_char_count": previous_end - start,
                    "section_char_start": group["start"] + start, "section_char_end": group["start"] + end,
                    "sources": spans, "context_sources": context_sources,
                    "structure_split": bool(split_ids), "split_block_ids": split_ids,
                    "warnings": ["table_context_omitted_budget"] if context_omitted else [],
                    "asset_refs": [{"path": asset, "artifact_dir": document.get("artifacts")} for asset in assets]}
                annotate_chunk(chunk, {b["block_id"] for b in blocks if b.get("role") == "toc"})
                chunks.append(chunk)
                group_chunks.append(chunk)
                previous_end = end
                if cursor < len(units):
                    start = (overlap_start(text, start, end, units[cursor][1], prefix, count, maximum, overlap)
                             if structure == "body" else end)
            reconstructed = "".join(c["body"][c["overlap_char_count"]:] for c in group_chunks)
            if reconstructed != text:
                raise AssertionError("chunking lost or reordered structural text")
    if any(c["token_count"] > maximum or c["overlap_token_count"] > overlap for c in chunks):
        raise AssertionError("token budget violated")
    return chunks
