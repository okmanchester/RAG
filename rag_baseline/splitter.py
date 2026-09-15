"""Recursive splitting with exact tokenizer checks and character-level provenance."""
from __future__ import annotations

import hashlib
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
            spans.append({"block_id": block["block_id"], **block["source"],
                          "block_char_start": left - a, "block_char_end": right - a,
                          "body_char_start": left - start, "body_char_end": right - start,
                          "is_overlap": right <= overlap_end})
    return spans


def chunk_document(document, sections, count, maximum=400, overlap=50):
    chunks = []
    for section in sections:
        toc_block_ids = {b["block_id"] for b in section["blocks"] if b.get("role") == "toc"}
        prefix = "\n".join([document["title"], *section["heading_path"]]) + "\n\n"
        if count(prefix) >= maximum:
            raise ValueError(f"section {section['section_id']}: title prefix exhausts token budget")
        ranges, parts, position = [], [], 0
        for block in section["blocks"]:
            if parts:
                parts.append("\n\n")
                position += 2
            text = block["text"]
            ranges.append((position, position + len(text), block))
            parts.append(text)
            position += len(text)
        text = "".join(parts)
        fits = lambda body: count(prefix + body) <= maximum
        units = []
        # Start from source paragraphs / table rows, including their separators.
        for i, (a, b, _) in enumerate(ranges):
            end = ranges[i + 1][0] if i + 1 < len(ranges) else b
            units.extend(recursive_units(text[a:end], fits, offset=a))
        cursor, start, previous_end, section_chunks = 0, 0, 0, []
        while cursor < len(units):
            end = units[cursor][1]
            if not fits(text[start:end]):
                raise AssertionError("overlap exceeded available budget")
            cursor += 1
            while cursor < len(units) and fits(text[start:units[cursor][1]]):
                end = units[cursor][1]
                cursor += 1
            body = text[start:end]
            # Whitespace-only tails belong to their preceding chunk; they normally
            # fit because the BGE tokenizer ignores whitespace.
            if not body.strip():
                raise ValueError("empty body after splitting")
            ordinal = len(chunks) + 1
            identity = f"{document['doc_id']}:{section['section_id']}:{start}:{end}:{maximum}:{overlap}:{prefix}{body}"
            chunk = {"chunk_id": hashlib.sha256(identity.encode()).hexdigest()[:24],
                     "doc_id": document["doc_id"], "chunk_index": ordinal,
                     "source_path": document["source_path"], "category": document["category"],
                     "title": document["title"], "section_id": section["section_id"],
                     "heading_path": section["heading_path"],
                     "heading_block_ids": section["heading_block_ids"],
                     "body": body, "text": prefix + body, "token_count": count(prefix + body),
                     "body_token_count": count(body),
                     "overlap_token_count": count(text[start:previous_end]),
                     "overlap_char_count": previous_end - start,
                     "section_char_start": start, "section_char_end": end,
                     "sources": source_spans(ranges, start, end, previous_end)}
            annotate_chunk(chunk, toc_block_ids)
            chunks.append(chunk)
            section_chunks.append(chunk)
            previous_end = end
            if cursor < len(units):
                start = overlap_start(text, start, end, units[cursor][1], prefix,
                                      count, maximum, overlap)
        # These checks run on every real document, not only in unit tests.
        reconstructed = "".join(c["body"][c["overlap_char_count"]:] for c in section_chunks)
        if reconstructed != text:
            raise AssertionError("chunking lost or reordered section text")
    if any(c["token_count"] > maximum or c["overlap_token_count"] > overlap for c in chunks):
        raise AssertionError("token budget violated")
    return chunks
