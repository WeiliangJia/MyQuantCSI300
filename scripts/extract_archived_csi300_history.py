"""Extract reviewed CSI 300 events from locally archived official HTML notices.

The script reads immutable JSON responses already archived from the official
CSIndex website.  It writes local-only review artifacts; no constituent list is
embedded in the repository source.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup


CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
DATE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
MONTH_DAY_RE = re.compile(r"(?<!年)(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")

# Dates explicitly stated by the notice or by its linked official listing event.
# These are metadata only; security lists continue to come from archived pages.
EFFECTIVE_DATE_OVERRIDES: dict[str, str] = {
    "6575": "2007-01-04",
    "1585": "2007-07-02",
    "5419": "2008-01-02",
    "47": "2007-12-26",
    "38": "2008-03-18",
    "20": "2008-07-01",
    "892": "2009-01-05",
    "1089": "2009-07-01",
    "1205": "2010-01-04",
    "1385": "2010-07-01",
    "6150": "2011-01-04",
}
MANUAL_ANNOUNCEMENT_IDS = {"92"}
DUPLICATE_ANNOUNCEMENT_IDS = {"907"}


def _clean(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _rows(table: object) -> list[list[str]]:
    return [
        [
            _clean(cell.get_text(" ", strip=True))
            for cell in row.find_all(["td", "th"], recursive=False)
        ]
        for row in table.find_all("tr")
        if row.find_all(["td", "th"], recursive=False)
    ]


def _security_codes(values: list[str]) -> list[str]:
    codes: list[str] = []
    for value in values:
        codes.extend(CODE_RE.findall(value))
    return [code for code in codes if code != "000300"]


def _parse_pair_table(rows: list[list[str]]) -> list[dict[str, str]]:
    header_text = " ".join(" ".join(row) for row in rows[:3])
    add_labels = [label for label in ("调入", "新进", "纳入") if label in header_text]
    remove_labels = [label for label in ("调出", "剔除", "删除") if label in header_text]
    if not add_labels or not remove_labels:
        return []

    first_header = " ".join(rows[0]) if rows else ""
    if not any(label in first_header for label in (*add_labels, *remove_labels)) and len(rows) > 1:
        first_header = " ".join(rows[1])
    add_position = min(first_header.find(label) for label in add_labels if label in first_header)
    remove_position = min(
        first_header.find(label) for label in remove_labels if label in first_header
    )
    add_first = add_position < remove_position
    pairs: list[dict[str, str]] = []
    for row in rows:
        codes = _security_codes(row)
        if len(codes) != 2 or len(row) < 4:
            continue
        positions = [i for i, value in enumerate(row) if CODE_RE.fullmatch(value)]
        if len(positions) != 2:
            continue
        left, right = positions
        left_name = row[left + 1] if left + 1 < len(row) else ""
        right_name = row[right + 1] if right + 1 < len(row) else ""
        if add_first:
            added, added_name, removed, removed_name = codes[0], left_name, codes[1], right_name
        else:
            removed, removed_name, added, added_name = codes[0], left_name, codes[1], right_name
        pairs.append(
            {
                "removed": removed,
                "removed_name": removed_name,
                "added": added,
                "added_name": added_name,
            }
        )
    return pairs


def _parse_index_rows(rows: list[list[str]]) -> list[dict[str, str]]:
    if len(rows) < 2:
        return []
    first_header = " ".join(rows[0])
    add_labels = [label for label in ("调入", "新进", "纳入") if label in first_header]
    remove_labels = [label for label in ("调出", "剔除", "删除") if label in first_header]
    if not add_labels or not remove_labels:
        return []
    add_first = min(first_header.find(label) for label in add_labels) < min(
        first_header.find(label) for label in remove_labels
    )
    column_header = rows[1]
    pairs: list[dict[str, str]] = []
    active_csi300 = False
    for row in rows[2:]:
        row_label = row[0].replace(" ", "") if row else ""
        if row_label in {"000300", "沪深300", "沪深300指数"}:
            active_csi300 = True
        elif active_csi300 and CODE_RE.fullmatch(row_label):
            pass
        else:
            active_csi300 = False
            continue
        if not row:
            continue
        code_positions = [
            i for i, value in enumerate(row) if CODE_RE.fullmatch(value) and value != "000300"
        ]
        if len(code_positions) != 2:
            continue
        left, right = code_positions
        offset = len(row) - len(column_header)

        def security_name(position: int) -> str:
            header_position = position - offset
            if header_position + 1 < len(column_header) and "名称" in column_header[header_position + 1]:
                return row[position + 1]
            if header_position > 0 and "名称" in column_header[header_position - 1]:
                return row[position - 1]
            return ""

        left_name = security_name(left)
        right_name = security_name(right)
        if add_first:
            added, added_name, removed, removed_name = row[left], left_name, row[right], right_name
        else:
            removed, removed_name, added, added_name = row[left], left_name, row[right], right_name
        pairs.append(
            {
                "removed": removed,
                "removed_name": removed_name,
                "added": added,
                "added_name": added_name,
            }
        )
    return pairs


def extract_pairs(content: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(content, "html.parser")
    specific_candidates: list[list[dict[str, str]]] = []
    generic_candidates: list[list[dict[str, str]]] = []
    for table in soup.find_all("table"):
        if table.find("table") is not None:
            continue
        rows = _rows(table)
        specific = _parse_index_rows(rows)
        if specific:
            specific_candidates.append(specific)
            continue
        generic = _parse_pair_table(rows)
        if generic:
            generic_candidates.append(generic)
    if specific_candidates:
        return max(specific_candidates, key=len)
    if not generic_candidates:
        return []
    # Historic regular notices list CSI 300 first, followed by other index
    # families (often CSI 100 and CSI 500).  Choosing the largest table can
    # silently select CSI 500, so preserve official document order.
    return generic_candidates[0]


def extract_effective_date(content: str, announcement_date: str, announcement_id: str) -> str:
    if announcement_id in EFFECTIVE_DATE_OVERRIDES:
        return EFFECTIVE_DATE_OVERRIDES[announcement_id]
    text = _clean(BeautifulSoup(content, "html.parser").get_text(" ", strip=True))
    announced = date.fromisoformat(announcement_date)
    candidates = {
        date(int(year), int(month), int(day))
        for year, month, day in DATE_RE.findall(text)
        if date(int(year), int(month), int(day)) >= announced
    }
    for month, day in MONTH_DAY_RE.findall(text):
        year = announced.year
        value = date(year, int(month), int(day))
        if value < announced:
            value = date(year + 1, int(month), int(day))
        candidates.add(value)
    future = sorted(value for value in candidates if value > announced)
    if future:
        return future[0].isoformat()
    if announced in candidates:
        return announced.isoformat()
    raise ValueError(f"No effective date found for announcement {announcement_id}")


def extract_baseline(content: str, as_of_date: str) -> list[dict[str, str]]:
    soup = BeautifulSoup(content, "html.parser")
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in soup.find_all("tr"):
        cells = [_clean(cell.get_text(" ", strip=True)) for cell in row.find_all(["td", "th"])]
        for i, value in enumerate(cells):
            if not CODE_RE.fullmatch(value) or value in seen:
                continue
            seen.add(value)
            rows.append(
                {
                    "ts_code": value,
                    "security_name": cells[i + 1] if i + 1 < len(cells) else "",
                    "as_of_date": as_of_date,
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--details-dir",
        type=Path,
        default=Path("data/official/csindex_announcements/details"),
    )
    parser.add_argument("--start", default="2005-01-01")
    parser.add_argument("--end", default="2010-12-31")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "data/reference/official_membership/.local_csi300_manual_events_2005_2010.json"
        ),
    )
    parser.add_argument(
        "--baseline-output",
        type=Path,
        default=Path("data/reference/official_membership/csi300_baseline_official_20050701.csv"),
    )
    args = parser.parse_args()

    events: dict[str, dict[str, object]] = {}
    files = sorted(args.details_dir.glob("*.json"))
    selected = [path for path in files if args.start <= path.name[:10] <= args.end]
    for position, path in enumerate(selected, start=1):
        payload = json.loads(path.read_text(encoding="utf-8"))
        announcement_id = str(payload["id"])
        publish_date = payload["publishDate"]
        title = payload["title"]
        if announcement_id in {"86", "6773"}:
            print(f"[{position}/{len(selected)}] id={announcement_id} baseline notice skipped")
            continue
        if announcement_id in MANUAL_ANNOUNCEMENT_IDS:
            print(
                f"[{position}/{len(selected)}] id={announcement_id} "
                "conditional multi-date notice deferred to local manual review"
            )
            continue
        if announcement_id in DUPLICATE_ANNOUNCEMENT_IDS:
            print(
                f"[{position}/{len(selected)}] id={announcement_id} duplicate archived payload skipped"
            )
            continue
        pairs = extract_pairs(payload["content"])
        if not pairs:
            raise ValueError(f"No CSI 300 pairs parsed from {path.name}: {title}")
        effective_date = extract_effective_date(payload["content"], publish_date, announcement_id)
        event_type = "SPECIAL" if len(pairs) <= 4 else "REGULAR"
        events[f"{publish_date}-{announcement_id}"] = {
            "event_type": event_type,
            "effective_date": effective_date,
            "announcement_date": publish_date,
            "announcement_id": announcement_id,
            "source_url": f"https://www.csindex.com.cn/#/about/newsDetail?id={announcement_id}",
            "pairs": pairs,
        }
        print(
            f"[{position}/{len(selected)}] id={announcement_id} announced={publish_date} "
            f"effective={effective_date} pairs={len(pairs)} type={event_type} status=PASS"
        )

    baseline_path = args.details_dir / "2005-07-22_6773.json"
    baseline_payload = json.loads(baseline_path.read_text(encoding="utf-8"))
    baseline = extract_baseline(baseline_payload["content"], "2005-07-01")
    if len(baseline) != 300 or len({row["ts_code"] for row in baseline}) != 300:
        raise ValueError(f"Official 2005-07-01 baseline must have 300 unique codes; got {len(baseline)}")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")
    args.baseline_output.write_text(
        "ts_code,security_name,as_of_date\n"
        + "".join(
            f'{row["ts_code"]},{row["security_name"]},2005-07-01\n' for row in baseline
        ),
        encoding="utf-8-sig",
    )
    print(
        f"[complete] events={len(events)} baseline_members={len(baseline)} "
        f"events_output={args.output} baseline_output={args.baseline_output}"
    )


if __name__ == "__main__":
    main()
