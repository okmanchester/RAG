"""Local, bounded MinerU smoke suite. Does not build a full corpus or call an LLM."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

from .chunk import run
from .verify import verify


def scanned_sample(directory):
    from PIL import Image, ImageDraw, ImageFont
    directory.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (1600, 2200), "white")
    draw = ImageDraw.Draw(image)
    font_path = Path("C:/Windows/Fonts/msyh.ttc")
    font = ImageFont.truetype(str(font_path), 46)
    for y, text in [(180, "扫描测试文档"), (350, "SMOKE OCR 2026"),
                    (500, "企业应当定期开展数据安全检查。"), (650, "检查完成后保存记录，并确认整改结果。")]:
        draw.text((150, y), text, fill="black", font=font)
    path = directory / "scan.pdf"
    image.save(path, "PDF", resolution=150)
    return path


def smoke(input_dir=Path("data/raw"), output=Path("data/processed/mineru_smoke")):
    input_dir, output = Path(input_dir), Path(output)
    output.mkdir(parents=True, exist_ok=True)
    scan_root = output / "scan_input"
    scanned_sample(scan_root)
    # Explicit bounded selection: 2 text pages, 2 pages of a real large table,
    # one complete native Word document, one image-only page with known text.
    cases = [
        ("text_pdf", input_dir, "technical/工业数据分类分级指南（试行）.pdf", "1-2"),
        ("table_pdf", input_dir, "technical/工业互联网平台监测分析指南.pdf", "8-9"),
        ("docx", input_dir, "policy/中华人民共和国数据安全法_20210610.docx", ""),
        ("scan_pdf", scan_root, "scan.pdf", ""),
    ]
    results = []
    for name, root, relative, pages in cases:
        started = time.monotonic()
        target = output / name
        try:
            code = run(root, target, include=[relative], page_range=pages)
            verified = verify(target)
            if code or not verified["batch_success"]:
                raise RuntimeError("smoke sample did not complete successfully")
            original = (target / "chunks.jsonl").read_bytes()
            replay_code = run(root, target, include=[relative], page_range=pages, reuse_parsed=True)
            if replay_code or (target / "chunks.jsonl").read_bytes() != original:
                raise AssertionError("cached parsing rechunk changed complete chunk records")
            verified = verify(target)
            chunks = [json.loads(line) for line in original.decode("utf-8").splitlines()]
            if name == "table_pdf" and not any(c["structure_type"] == "table" for c in chunks):
                raise AssertionError("selected technical pages produced no table chunks")
            if name == "scan_pdf":
                text = re.sub(r"\s+", "", "".join(c["body"] for c in chunks)).upper()
                if "SMOKEOCR2026" not in text:
                    raise AssertionError("known text missing from OCR result")
            results.append({"sample": name, "source": relative, "pages": pages or "all",
                            "status": "passed", "seconds": round(time.monotonic() - started, 2),
                            "chunks_sha256": hashlib.sha256(original).hexdigest(),
                            "cached_rechunk": "identical", "verification": verified})
        except Exception as exc:
            results.append({"sample": name, "source": relative, "pages": pages or "all",
                            "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
    report = {"scope": "smoke_only", "full_corpus_processed": False,
              "status": "passed" if all(r["status"] == "passed" for r in results) else "failed",
              "samples": results, "quality_claim": "No retrieval or answer quality claim."}
    (output / "smoke_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if report["status"] == "passed" else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/raw"))
    parser.add_argument("--output", type=Path, default=Path("data/processed/mineru_smoke"))
    args = parser.parse_args()
    return smoke(args.input, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
