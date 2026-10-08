"""Local MinerU 4 adapter; normalized text points into saved Middle JSON."""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path

PARSER_VERSION = "mineru_middle_v2"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config/mineru.yaml"
AUXILIARY = {"header", "footer", "page_number", "aside_text"}
HEADER_LABELS = {"名称", "说明", "分类", "序号", "单位", "指标", "一级指标", "二级指标",
                 "项目", "更新频率", "指标解释", "要求", "内容", "标准", "备注"}


def normalize(text: str) -> str:
    return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip()
                     for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")).strip()


def inline_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(inline_text(x) for x in value)
    if isinstance(value, dict):
        content = inline_text(value.get("content", ""))
        return "$" + content + "$" if value.get("type") == "equation_inline" else content
    raise ValueError(f"unsupported text payload: {type(value).__name__}")


class TableHTML(HTMLParser):
    """Preserve physical cells and spans without duplicating merged text."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows, self.row, self.cell = [], None, None
        self.in_head, self.depth = False, 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "table":
            self.depth += 1
        if self.depth > 1:
            if self.cell is not None and tag in {"tr", "br"}:
                self.cell["parts"].append("\n")
            return
        if tag == "thead":
            self.in_head = True
        elif tag == "tr":
            self.row = []
        elif tag in {"td", "th"}:
            if self.row is None:
                self.row = []
            self.cell = {"parts": [], "rowspan": max(1, int(attrs.get("rowspan", 1))),
                         "colspan": max(1, int(attrs.get("colspan", 1))),
                         "is_header": tag == "th" or self.in_head}
        elif self.cell is not None and tag in {"br", "p", "div"}:
            self.cell["parts"].append("\n")

    def handle_endtag(self, tag):
        if tag == "table":
            self.depth -= 1
            return
        if self.depth > 1:
            return
        if tag in {"td", "th"} and self.cell is not None:
            self.cell["text"] = normalize("".join(self.cell.pop("parts")))
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "thead":
            self.in_head = False

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["parts"].append(data)

    def positioned_rows(self):
        occupied = set()
        for row_num, row in enumerate(self.rows, 1):
            col, parts, offset, cells = 1, [], 0, []
            for cell in row:
                while (row_num, col) in occupied:
                    col += 1
                if parts:
                    offset += 3
                start = offset
                parts.append(cell["text"])
                offset += len(cell["text"])
                cells.append({**cell, "row": row_num, "column": col,
                              "char_start": start, "char_end": offset})
                for r in range(row_num, row_num + cell["rowspan"]):
                    for c in range(col, col + cell["colspan"]):
                        occupied.add((r, c))
                col += cell["colspan"]
            yield row_num, " | ".join(parts), cells


def adapt_middle(middle: dict, suffix: str) -> tuple[list[dict], dict]:
    if middle.get("schema") != "docvortex.middle" or middle.get("schema_version") != "2.0":
        raise ValueError("expected MinerU 4 docvortex.middle schema 2.0")
    blocks, warnings = [], []

    def add(value, typ, source, **extra):
        text = value if typ == "table_row" else normalize(value)
        reason = extra.pop("exclusion_reason", None)
        if not text and reason is None:
            reason = "nontext_asset" if extra.get("assets") else "empty_content"
        blocks.append({"block_id": len(blocks) + 1, "text": text, "kind": typ,
                       "source": source, "heading_level": extra.pop("heading_level", None),
                       "structure_type": extra.pop("structure_type", "body"),
                       "exclusion_reason": reason, **extra})

    def visit(node, source, pointer, group=None, toc=False):
        typ = node.get("type", "unknown")
        source = {**source, "mineru_type": typ, "pointer": pointer}
        if node.get("bbox") is not None:
            source["bbox"] = node["bbox"]
        assets = [node["image_path"]] if node.get("image_path") else []
        common = {"assets": assets, "is_toc": toc}
        if group:
            common["group_id"] = group
        if typ in {"list", "index", "image", "chart", "table", "code"}:
            children = node.get("content", [])
            if not isinstance(children, list):
                raise ValueError(f"invalid container at {pointer}")
            for i, child in enumerate(children):
                visit(child, source, f"{pointer}/content/{i}", group or pointer, toc or typ == "index")
        elif typ == "table_body":
            html = node.get("content", "")
            parser = TableHTML()
            parser.feed(html)
            parser.close()
            rows = list(parser.positioned_rows())
            if not rows:
                warnings.append(f"table_structure_unavailable:{pointer}")
                add(normalize(re.sub(r"<[^>]*>", " ", html)), "table_row", source,
                    structure_type="table", raw_html=html, **common)
            for row_num, text, cells in rows:
                explicit_header = bool(cells) and all(c["is_header"] for c in cells)
                label_header = row_num == 1 and len(cells) >= 2 and all(c["text"] in HEADER_LABELS for c in cells)
                add(text, "table_row", {**source, "row": row_num}, cells=cells,
                    is_table_header=explicit_header or label_header,
                    header_detection="html" if explicit_header else "column_labels" if label_header else None,
                    raw_html=html if row_num == 1 else None, structure_type="table", **common)
        elif typ in {"doc_title", "paragraph_title"}:
            add(inline_text(node.get("content", [])), "paragraph", source,
                heading_level=node.get("level"), is_title=typ == "doc_title", **common)
        elif typ in AUXILIARY:
            add(inline_text(node.get("content", [])), "paragraph", source,
                exclusion_reason=typ, **common)
        elif typ in {"text", "ref_text", "page_footnote"}:
            add(inline_text(node.get("content", [])), "paragraph", source, **common)
        elif typ in {"image_body", "chart_body"}:
            add(inline_text(node.get("content", "")), typ, source,
                structure_type="image", **common)
        elif typ in {"equation", "code_body", "algorithm_body"}:
            add(inline_text(node.get("content", "")), typ, source,
                structure_type="equation" if typ == "equation" else "code", **common)
        elif typ.endswith(("_caption", "_footnote")):
            structure = "table" if typ.startswith("table_") else "code" if typ.startswith("code_") else "image"
            add(inline_text(node.get("content", [])), typ, source,
                structure_type=structure, is_table_caption=typ == "table_caption", **common)
        else:
            warnings.append(f"unknown_block_type:{typ}:{pointer}")
            add(inline_text(node.get("content", "")), typ, source, structure_type="unknown", **common)

    pages = middle.get("pages", [])
    for pi, page in enumerate(pages):
        for bi, node in enumerate(page.get("blocks", [])):
            source = {"page_idx": page["page_idx"], "mineru_index": node.get("index", bi)}
            source["page" if suffix == ".pdf" else "content_index"] = page["page_idx"] + 1 if suffix == ".pdf" else len(blocks) + 1
            visit(node, source, f"/pages/{pi}/blocks/{bi}")
    group_assets = {}
    for block in blocks:
        if block.get("group_id"):
            group_assets.setdefault(block["group_id"], set()).update(block["assets"])
    for block in blocks:
        if block.get("group_id"):
            block["assets"] = sorted(group_assets[block["group_id"]])
    return blocks, {"parser": "mineru", "parser_adapter": PARSER_VERSION,
                    "parser_version": middle.get("metadata", {}).get("producer", {}).get("version"),
                    "parse_tier": middle.get("extensions", {}).get("mineru", {}).get("tier"),
                    "parse_mode": middle.get("extensions", {}).get("mineru", {}).get("parse_mode"),
                    "page_count": len(pages) if suffix == ".pdf" else None,
                    "parsed_page_numbers": [p["page_idx"] + 1 for p in pages] if suffix == ".pdf" else [],
                    "is_full_document": middle.get("is_full_document", True),
                    "empty_pages": [p["page_idx"] + 1 for p in pages if not p.get("blocks")] if suffix == ".pdf" else [],
                    "parser_warnings": warnings}


def configure(config_path=DEFAULT_CONFIG):
    """Set process-local settings before import; never edit the user's configuration."""
    import yaml
    settings = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    model_root = Path(settings["model"]["base_dir"])
    if not model_root.is_dir():
        raise FileNotFoundError(f"MinerU models missing: {model_root}")
    os.environ.update({"MINERU_CONFIG": str(Path(config_path).resolve()),
        "MINERU_MODEL_BASE_DIR": str(model_root), "MINERU_MODEL_SOURCE": "local",
        "MINERU_MODEL_SMALL_BACKEND": "torch", "MINERU_MODEL_VLM_ENGINE": "llama-cpp",
        "MINERU_MODEL_VLM_SERVER_URL": "", "MINERU_MODEL_VLM_MAX_CONCURRENCY": "1",
        "MINERU_LLM_AIDED_FEATURES_TITLE_LEVELING": "false",
        "MINERU_LLM_AIDED_FEATURES_CROSS_PAGE_TABLE_CELL_MERGE": "false"})
    if importlib.metadata.version("mineru") != "4.0.10":
        raise RuntimeError("this adapter requires mineru==4.0.10")
    from mineru.config import config
    config.model.base_dir, config.model.source, config.model.small_backend = str(model_root), "local", "torch"
    config.model.vlm.engine, config.model.vlm.server_url, config.model.vlm.max_concurrency = "llama-cpp", "", 1
    config.llm_aided.features.title_leveling = False
    config.llm_aided.features.cross_page_table_cell_merge = False
    return settings


def pdf_health(path, page_range):
    """Probe requested pages with the same PDFium runtime used by MinerU.

    MinerU may replace broken pages with empty placeholders. Detect them before
    inference, so a partial result cannot be mistaken for a successful document.
    """
    from docvortex.document.pdf import PDFDocument
    from docvortex.document.pdf.pdfium import get_loadable_pdfium_page_indices
    from docvortex.document.page_range import parse_page_range
    data = Path(path).read_bytes()
    with PDFDocument(data) as doc:
        page_count = doc.page_count
    selected = parse_page_range(page_range, page_count)
    if not selected:
        raise ValueError("PDF page selection is empty")
    _, broken = get_loadable_pdfium_page_indices(data, min(selected), max(selected))
    damaged = sorted(set(broken) & set(selected))
    if damaged:
        raise ValueError(f"damaged PDF pages: {[p + 1 for p in damaged]}")
    return page_count, selected


def parse(path: Path, *, artifact_dir: Path, reuse=False, page_range="", config_path=DEFAULT_CONFIG):
    path, artifact_dir = Path(path), Path(artifact_dir)
    if path.suffix.lower() not in {".pdf", ".docx"}:
        raise ValueError(f"unsupported input format: {path.suffix}")
    source_page_count, selected_pages = None, None
    identity = {"source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source_format": path.suffix.lower(), "page_range": page_range,
                "parser_adapter": PARSER_VERSION, "mineru_version": "4.0.10",
                "image_analysis": False, "pdf_tier": "standard", "ocr_mode": "auto"}
    marker = artifact_dir / "parse_manifest.json"
    if reuse:
        saved = json.loads(marker.read_text(encoding="utf-8")) if marker.is_file() else {}
        if saved.get("identity") != identity or not saved.get("files_sha256"):
            raise ValueError("saved parsing identity mismatch or incomplete parsing; reparse required")
        for name, sha in saved["files_sha256"].items():
            target = (artifact_dir / name).resolve()
            if artifact_dir.resolve() not in target.parents or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != sha:
                raise ValueError(f"saved parsing checksum mismatch: {name}")
        if path.suffix.lower() == ".pdf":
            source_page_count, selected_pages = pdf_health(path, page_range)
    else:
        configure(config_path)
        if path.suffix.lower() == ".pdf":
            source_page_count, selected_pages = pdf_health(path, page_range)
        from mineru.parser import parse as mineru_parse
        from mineru.config import VlmConfig
        from mineru.parser.writer import FileBasedDataWriter
        marker.unlink(missing_ok=True)
        result = mineru_parse(path, tier="standard" if path.suffix.lower() == ".pdf" else "flash",
                              ocr_mode="auto", image_analysis=False,
                              page_range=page_range if path.suffix.lower() == ".pdf" else "",
                              vlm_config=VlmConfig(engine="llama-cpp", server_url="", max_concurrency=1))
        artifact_dir.mkdir(parents=True, exist_ok=True)
        result.save(FileBasedDataWriter(str(artifact_dir)))
        checksums = {p.relative_to(artifact_dir).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in sorted(artifact_dir.rglob("*")) if p.is_file() and p != marker}
        marker.write_text(json.dumps({"identity": identity, "files_sha256": checksums},
                                    ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    middle = json.loads((artifact_dir / "middle_json.json").read_text(encoding="utf-8"))
    blocks, metadata = adapt_middle(middle, path.suffix.lower())
    if selected_pages is not None and [p["page_idx"] for p in middle["pages"]] != selected_pages:
        raise ValueError("MinerU result does not cover all requested PDF pages")
    metadata["source_page_count"] = source_page_count
    for block in blocks:
        for asset in block.get("assets", []):
            target = (artifact_dir / asset).resolve()
            if artifact_dir.resolve() not in target.parents or not target.is_file():
                raise ValueError(f"missing/unsafe saved asset: {asset}")
    metadata["artifacts"] = artifact_dir.as_posix()
    return blocks, metadata
