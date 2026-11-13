"""Create a reviewed CSI 300 adjustment manifest from an archived official PDF.

The output remains a CSV review artifact.  It must be checked against the PDF
before importing it into the point-in-time membership-event table.
"""
from __future__ import annotations

import argparse
import csv
import re
from pathlib import Path

from pypdf import PdfReader


def extract_pairs(pdf_path: Path) -> list[tuple[str, str]]:
    text = "\n".join(page.extract_text() or "" for page in PdfReader(pdf_path).pages)
    start = text.index("沪深 300 指数样本调整名单：")
    end = text.index("中证 500 指数样本调整名单：", start)
    section = text[start:end]
    pairs = re.findall(r"(?m)^(\d{6})\s+.+?\s+(\d{6})\s+", section)
    if not pairs:
        raise ValueError("No CSI 300 adjustment pairs were found")
    return pairs


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--announcement-date", required=True)
    parser.add_argument("--effective-date", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--expected-pairs", type=int)
    args = parser.parse_args()

    pairs = extract_pairs(args.pdf)
    if args.expected_pairs is not None and len(pairs) != args.expected_pairs:
        raise ValueError(f"Expected {args.expected_pairs} CSI 300 adjustment pairs, found {len(pairs)}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["index_code", "ts_code", "action", "effective_date", "announcement_date", "source_url"])
        writer.writeheader()
        for removed, added in pairs:
            writer.writerow({"index_code": "000300.SH", "ts_code": removed, "action": "REMOVE", "effective_date": args.effective_date, "announcement_date": args.announcement_date, "source_url": args.source_url})
            writer.writerow({"index_code": "000300.SH", "ts_code": added, "action": "ADD", "effective_date": args.effective_date, "announcement_date": args.announcement_date, "source_url": args.source_url})


if __name__ == "__main__":
    main()
