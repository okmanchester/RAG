"""Text-only parsers. All source indices are one-based."""
from __future__ import annotations

import re
from pathlib import Path


def normalize(text: str) -> str:
    return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip()
                     for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()


def formula_text_regions(path: Path, pages: list[dict]) -> dict:
    """Read only existing PDF text in regions omitted by Markdown as formulas."""
    regions = [(page["metadata"]["page_number"], i, box["bbox"])
               for page in pages for i, box in enumerate(page.get("page_boxes", []))
               if box["class"] == "formula"]
    if not regions:
        return {}
    import pymupdf
    result = {}
    with pymupdf.open(path) as document:
        for page, index, bbox in regions:
            # Overlayed glyphs used to simulate bold are deduplicated by
            # identical text + rounded geometry, not by repeated prose lines.
            words = document[page - 1].get_text("words", clip=pymupdf.Rect(bbox), sort=True)
            seen, lines = set(), {}
            for x0, y0, x1, y1, word, *_ in words:
                key = (word, round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1))
                if key not in seen:
                    seen.add(key)
                    lines.setdefault(round(y0, 1), []).append((x0, word))
            text = normalize("\n".join(" ".join(word for _, word in sorted(line))
                                       for _, line in sorted(lines.items())))
            if text:
                result[(page, index)] = text
    return result


def parse_pdf(path: Path) -> tuple[list[dict], dict]:
    import pymupdf4llm

    pages = pymupdf4llm.to_markdown(
        str(path), page_chunks=True, use_ocr=False, force_ocr=False,
        write_images=False, embed_images=False, header=True, footer=True,
        show_progress=False,
    )
    blocks, empty_pages, rescued_headings, filtered_edges, recovered_formulas = [], [], [], [], []
    formula_regions = formula_text_regions(path, pages)
    for page in pages:
        number = page["metadata"]["page_number"]
        text = page["text"]
        # The layout model occasionally classifies a real chapter title at a
        # page edge as a footer. Preserve explicit numbered headings there.
        from .structure import heading_level
        replacements = []
        for index, box in enumerate(page.get("page_boxes", [])):
            formula = formula_regions.get((number, index))
            if formula and not text[slice(*box["pos"])].strip():
                a, b = box["pos"]
                replacements.append((a, b, formula + "\n\n"))
                recovered_formulas.append({"page": number, "bbox": box["bbox"], "text": formula})
            if box["class"] not in ("page-header", "page-footer"):
                continue
            a, b = box["pos"]
            value = re.sub(r"^#{1,6}\s*", "", normalize(text[a:b]))
            candidate = {"text": value, "kind": "paragraph", "heading_level": None}
            if heading_level(candidate) is not None:
                rescued_headings.append({"page": number, "text": value})
            else:
                filtered_edges.append({"page": number, "class": box["class"], "text": value})
                replacements.append((a, b, ""))
        for a, b, replacement in sorted(replacements, reverse=True):
            text = text[:a] + replacement + text[b:]
        if not text.strip():
            empty_pages.append(number)
        # Keep Markdown tables as row units and headings separate from paragraphs.
        for part in re.split(r"\n\s*\n", text):
            lines = part.strip().splitlines()
            groups = lines if any(line.lstrip().startswith(("|", "#")) for line in lines) else [part]
            for group in groups:
                value = normalize(group)
                if not value:
                    continue
                match = re.match(r"^(#{1,6})\s+(.+?)\s*#*\s*$", value)
                kind = "table_row" if value.startswith("|") else "paragraph"
                level = len(match[1]) if match else None
                if match:
                    value = match[2].strip()
                blocks.append({"block_id": len(blocks) + 1, "text": value,
                               "kind": kind, "heading_level": level,
                               "source": {"page": number}})
    return blocks, {"parser": "pymupdf4llm", "page_count": len(pages), "empty_pages": empty_pages,
                    "rescued_edge_headings": rescued_headings, "filtered_edges": filtered_edges,
                    "recovered_formula_text": recovered_formulas}


def parse_docx(path: Path) -> tuple[list[dict], dict]:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph
    from docx.oxml.ns import qn

    doc = Document(path)
    blocks = []

    def add(text, kind, source, level=None, title=False):
        text = normalize(text)
        if text:
            blocks.append({"block_id": len(blocks) + 1, "text": text, "kind": kind,
                           "heading_level": level, "is_title": title, "source": source})

    def cell_text(cell):
        parts = []
        for item in cell.iter_inner_content():
            if isinstance(item, Paragraph):
                parts.append(item.text)
            elif isinstance(item, Table):
                for row in item.rows:
                    seen, values = set(), []
                    for nested in row.cells:
                        if nested._tc not in seen:
                            seen.add(nested._tc)
                            values.append(cell_text(nested))
                    parts.append(" | ".join(values))
        return "\n".join(parts)

    for index, item in enumerate(doc.iter_inner_content(), 1):
        if isinstance(item, Paragraph):
            style = item.style
            name = style.name if style else ""
            match = re.search(r"(?:Heading|标题)\s*(\d+)", name, re.I)
            level = int(match[1]) if match else None
            # Outline levels can live on the paragraph or its inherited style.
            nodes = [item._p]
            visited = set()
            while style is not None and style.style_id not in visited:
                visited.add(style.style_id)
                nodes.append(style.element)
                style = style.base_style
            if level is None:
                for node in nodes:
                    values = node.xpath("./w:pPr/w:outlineLvl")
                    if values:
                        outline = int(values[0].get(qn("w:val")))
                        level = outline + 1 if outline < 9 else None
                        break
            add(item.text, "paragraph", {"content_index": index}, level,
                name.lower() == "title" or name == "标题")
        elif isinstance(item, Table):
            for row_index, row in enumerate(item.rows, 1):
                seen, values = set(), []
                for cell in row.cells:
                    if cell._tc not in seen:
                        seen.add(cell._tc)
                        values.append(cell_text(cell))
                add(" | ".join(values), "table_row",
                    {"content_index": index, "table_index": index, "row": row_index})
    return blocks, {"parser": "python-docx", "page_count": None, "empty_pages": []}


def parse(path: Path) -> tuple[list[dict], dict]:
    return parse_pdf(path) if path.suffix.lower() == ".pdf" else parse_docx(path)
