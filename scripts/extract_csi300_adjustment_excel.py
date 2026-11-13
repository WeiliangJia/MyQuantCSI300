"""Create a reviewed CSI 300 adjustment manifest from an official Excel annex.

Historic SSE adjustment annexes use the first two worksheets for additions and
removals.  This script deliberately validates that structure, filters only
index code 000300, and emits a small auditable CSV for the official importer.
"""
from __future__ import annotations

import argparse
import csv
from pathlib import Path

import pandas as pd


def _code(value: object) -> str:
    """Normalize an Excel code while preserving its leading zeroes."""
    text = str(value).strip()
    if text.endswith(".0"):
        text = text[:-2]
    if not text.isdigit():
        return ""
    return text.zfill(6)


def read_csi300_codes(path: Path, sheet: str) -> list[str]:
    frame = pd.read_excel(path, sheet_name=sheet, dtype=str).fillna("")
    if frame.shape[1] < 4:
        raise ValueError(f"{path.name} sheet {sheet!r} does not have the expected four columns")
    index_codes = frame.iloc[:, 0].map(_code)
    security_codes = frame.loc[index_codes == "000300"].iloc[:, 2].map(_code).tolist()
    if not security_codes or any(not code for code in security_codes):
        raise ValueError(f"{path.name} sheet {sheet!r} has no valid CSI 300 security codes")
    if len(security_codes) != len(set(security_codes)):
        raise ValueError(f"{path.name} sheet {sheet!r} has duplicate CSI 300 security codes")
    return security_codes


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("workbook", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--announcement-date", required=True)
    parser.add_argument("--effective-date", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--expected-pairs", type=int, required=True)
    args = parser.parse_args()

    workbook = pd.ExcelFile(args.workbook)
    if len(workbook.sheet_names) < 2:
        raise ValueError(f"{args.workbook.name} must have addition and removal worksheets")
    added = read_csi300_codes(args.workbook, workbook.sheet_names[0])
    removed = read_csi300_codes(args.workbook, workbook.sheet_names[1])
    if len(added) != len(removed):
        raise ValueError(f"Unbalanced adjustment: {len(added)} additions and {len(removed)} removals")
    if len(added) != args.expected_pairs:
        raise ValueError(f"Expected {args.expected_pairs} pairs, found {len(added)}")
    if set(added).intersection(removed):
        raise ValueError("A security cannot be both added and removed in one adjustment")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["index_code", "ts_code", "action", "effective_date", "announcement_date", "source_url"],
        )
        writer.writeheader()
        for code in removed:
            writer.writerow({"index_code": "000300.SH", "ts_code": code, "action": "REMOVE", "effective_date": args.effective_date, "announcement_date": args.announcement_date, "source_url": args.source_url})
        for code in added:
            writer.writerow({"index_code": "000300.SH", "ts_code": code, "action": "ADD", "effective_date": args.effective_date, "announcement_date": args.announcement_date, "source_url": args.source_url})
    print(f"Validated {len(added)} add/remove pairs and wrote {len(added) * 2} events: {args.output}")


if __name__ == "__main__":
    main()
