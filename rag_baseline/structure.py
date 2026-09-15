"""Conservative heading recognition; retain all normalized text blocks."""
from __future__ import annotations

import re

CN = "〇零一二三四五六七八九十百千万两0-9"


def plain(text: str) -> str:
    text = re.sub(r"</?(?:mark|b|strong|em|i)>", "", text.replace("**", "")).strip()
    return re.sub(r"^[-+*]\s+", "", text)


def heading_level(block: dict) -> int | None:
    text = plain(block["text"])
    if block["kind"] == "table_row":
        return None
    if re.fullmatch(rf"第\s*[{CN}]+\s*号", text) or "首页" in text or " > " in text:
        return None
    if re.fullmatch(r"[（(](?:资料性|规范性)(?:附录)?[）)]", text):
        return None
    # Numbered articles and list items containing prose must remain body text,
    # even if the PDF layout model has called them headings.
    if re.match(rf"^第\s*[{CN}]+\s*条", text):
        return None
    if len(text) > 100 or re.search(r"[。；;！？!?，,]", text):
        return None
    if re.match(r"^附录\s*[A-Z一二三四五六七八九十]", text):
        return 1
    appendix = re.match(r"^[A-Z](?:\s*\.\s*\d+)+(?:\s+|(?=[\u4e00-\u9fff]))", text)
    if appendix:
        return 2 + appendix[0].count(".")
    if re.match(rf"^第\s*[{CN}]+\s*章", text):
        return 1
    if re.match(rf"^第\s*[{CN}]+\s*节", text):
        return 2
    if re.match(rf"^[{CN}]+\s*[、．]\s*\S", text):
        return 1
    if re.match(rf"^[（(]\s*[{CN}]+\s*[）)]\s*\S", text):
        return 2
    numbered = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", text)
    match = re.match(r"^(\d+(?:\.\d+)*)(?:[.、]?\s+|[、]|(?=[\u4e00-\u9fff]))\S", numbered)
    if match:
        return min(6, 1 + match[1].count("."))
    return block.get("heading_level")


def title_index(blocks: list[dict], fallback: str) -> int | None:
    candidates = []
    compact_fallback = re.sub(r"\s+", "", fallback)
    for index, block in enumerate(blocks[:8]):
        text = plain(block["text"])
        if block["kind"] != "paragraph" or len(text) > 150:
            continue
        if re.search(r"[。；;！？!?]", text) or re.match(r"^(附件|附录)\s*\d*\s*$", text):
            continue
        if re.fullmatch(rf"第\s*[{CN}]+\s*号", text) or "首页" in text or " > " in text:
            continue
        if text in ("中华人民共和国国家标准", "中华人民共和国行业标准"):
            continue
        compact = re.sub(r"\s+", "", text)
        if len(compact) >= 5 and compact in compact_fallback and heading_level(dict(block, heading_level=None)) is None:
            candidates.append((100 + len(compact) / len(compact_fallback), -index, index))
        if block.get("is_title") or block.get("heading_level") == 1:
            # A first chapter heading is not a document title.
            candidate = dict(block, heading_level=None)
            if heading_level(candidate) is None:
                candidates.append((60, -index, index))
        if len(compact) >= 5 and re.search(
            r"(?:法|条例|办法|规定|指南|意见|通知|规划|白皮书|规范|要求|架构|方法)(?:[（(].*[）)])?$", compact
        ) and heading_level(dict(block, heading_level=None)) is None:
            candidates.append((30, -index, index))
    return max(candidates)[2] if candidates else None


def toc_key(text: str) -> str:
    text = re.sub(r"[|\s]", "", plain(text))
    return re.sub(r"[.．·…]{2,}.*$", "", text)


def build_sections(blocks: list[dict], fallback: str) -> tuple[str, list[dict], list[str]]:
    index = title_index(blocks, fallback)
    title = plain(blocks[index]["text"]) if index is not None else fallback
    warnings = [] if index is not None else ["document_title_from_filename"]
    sections, stack, active, in_toc, toc_seen = [], [], None, False, set()

    def new_section(path, headings):
        section = {"section_id": len(sections) + 1, "heading_path": path,
                   "heading_block_ids": headings, "blocks": []}
        sections.append(section)
        return section

    for i, block in enumerate(blocks):
        block["role"] = "body"
        if i == index:
            block["role"] = "document_title"
            continue
        value = plain(block["text"])
        level = heading_level(block) if index is None or i > index else None
        if (level is not None and stack and re.match(r"^附录\s*[A-Z]", stack[-1][1])
                and i > 0 and re.fullmatch(r"[（(](?:资料性|规范性)(?:附录)?[）)]", plain(blocks[i - 1]["text"]))
                and not re.match(r"^[A-Z]\s*\.", value)):
            level = 2
        if re.fullmatch(r"目\s*[录次]|contents|table of contents", value, re.I):
            in_toc, toc_seen, stack = True, set(), []
            active = new_section(["目录"], [])
            warnings.append("toc_boundary_heuristic")
        elif in_toc:
            key = toc_key(value)
            repeated = key in toc_seen and level is not None
            prose = block["kind"] != "table_row" and (len(value) > 120 or bool(re.search(r"[。；！？]", value)))
            if repeated or prose:
                in_toc, stack, active = False, [], None
            else:
                toc_seen.add(key)
        if in_toc:
            block["role"] = "toc"
            active["blocks"].append(block)
            continue
        if level is not None:
            block["role"] = "heading"
            block["resolved_heading_level"] = level
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, value, block["block_id"]))
            active = new_section([entry[1] for entry in stack], [entry[2] for entry in stack])
        else:
            if active is None:
                active = new_section([entry[1] for entry in stack], [entry[2] for entry in stack])
            active["blocks"].append(block)
    # Preserve headings without body as standalone content; they cannot just disappear.
    by_id = {block["block_id"]: block for block in blocks}
    referenced = {ident for section in sections if section["blocks"] for ident in section["heading_block_ids"]}
    for section in sections:
        if not section["blocks"] and section["heading_block_ids"]:
            last = section["heading_block_ids"][-1]
            if last not in referenced:
                section["blocks"] = [by_id[last]]
    sections = [section for section in sections if section["blocks"]]
    if not sections and index is not None:
        sections = [new_section([], [])]
        sections[0]["blocks"] = [blocks[index]]
    if not any(block["role"] == "heading" for block in blocks):
        warnings.append("no_section_headings_detected")
    return title, sections, sorted(set(warnings))
