"""Build and validate the local-only CSI 300 event table from reviewed inputs.

The complete output and the manually reviewed seed are intentionally Git-ignored.
This script contains schemas and validation rules, but no constituent lists.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import pandas as pd

from myquant.data.free_adapters import to_ts_code


EXPECTED_EFFECTIVE_DATES = (
    "2005-07-01",
    "2006-01-01",
    "2006-04-21",
    "2006-04-24",
    "2006-07-03",
    "2006-07-19",
    "2006-08-15",
    "2006-10-30",
    "2007-01-04",
    "2007-01-23",
    "2007-02-26",
    "2007-03-15",
    "2007-04-30",
    "2007-05-18",
    "2007-05-29",
    "2007-07-02",
    "2007-10-23",
    "2007-11-19",
    "2007-12-26",
    "2008-01-02",
    "2008-03-18",
    "2008-07-01",
    "2009-01-05",
    "2009-07-01",
    "2010-01-04",
    "2010-07-01",
    "2010-07-29",
    "2011-01-04",
    "2011-07-01",
    "2011-08-23",
    "2012-01-04",
    "2012-07-02",
    "2013-01-04",
    "2013-07-01",
    "2013-09-18",
    "2013-12-16",
    "2014-06-16",
    "2014-12-15",
    "2015-01-26",
    "2015-05-20",
    "2015-06-15",
    "2015-12-14",
    "2015-12-30",
    "2016-06-13",
    "2016-12-12",
    "2017-02-14",
    "2017-06-12",
    "2017-12-11",
    "2018-06-11",
    "2018-12-17",
    "2019-06-17",
    "2019-12-16",
    "2020-06-15",
    "2020-12-14",
    "2021-06-15",
    "2021-12-13",
    "2022-06-13",
    "2022-12-12",
    "2023-06-12",
    "2023-12-11",
    "2024-06-17",
    "2024-12-16",
    "2025-03-04",
    "2025-06-16",
    "2025-09-05",
    "2025-12-15",
    "2026-06-15",
)

REVIEWED_MANIFESTS = (
    "csi300_201306_adjustment.csv",
    "csi300_201406_adjustment.csv",
    "csi300_201412_adjustment.csv",
    "csi300_201506_adjustment.csv",
    "csi300_201512_adjustment.csv",
    "csi300_201606_adjustment.csv",
    "csi300_201612_adjustment.csv",
    "csi300_201706_adjustment.csv",
    "csi300_201712_adjustment.csv",
    "csi300_201806_adjustment.csv",
    "csi300_201812_adjustment.csv",
    "csi300_202112_adjustment.csv",
    "csi300_202206_adjustment.csv",
    "csi300_202212_adjustment.csv",
    "csi300_202412_adjustment.csv",
    "csi300_202512_adjustment.csv",
    "csi300_202606_adjustment.csv",
)

RAW_DOCUMENTS_BY_EFFECTIVE_DATE = {
    "2013-07-01": "csi300_adjustment_201306.xls",
    "2014-06-16": "csi300_adjustment_201406.xls",
    "2014-12-15": "csi300_adjustment_201412.xls",
    "2015-06-15": "csi300_adjustment_201506.xls",
    "2015-12-14": "csi300_adjustment_201512.xlsx",
    "2015-12-30": "csi300_special_adjustment_20151230.xls",
    "2016-06-13": "csi300_adjustment_201606.xls",
    "2016-12-12": "csi300_adjustment_201612.xlsx",
    "2017-06-12": "csi300_adjustment_201706.xlsx",
    "2017-12-11": "csi300_adjustment_201712.xlsx",
    "2018-06-11": "csi300_adjustment_201806.xlsx",
    "2018-12-17": "csi300_adjustment_201812.xlsx",
    "2021-12-13": "csi300_adjustment_202112.xlsx",
    "2022-06-13": "csi300_adjustment_202206.xlsx",
    "2022-12-12": "csi300_adjustment_202212.xlsx",
    "2024-12-16": "csi300_adjustment_202412.pdf",
    "2025-03-04": "csi300_special_adjustment_202502.xlsx",
    "2025-09-05": "csi300_special_adjustment_202507.xlsx",
    "2025-12-15": "csi300_adjustment_202512.pdf",
    "2026-06-15": "csi300_adjustment_202606.pdf",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _archived_detail_path(raw_dir: Path, announcement_date: str, announcement_id: str) -> Path | None:
    """Locate an immutable official detail archive without exposing its contents."""
    if not announcement_id:
        return None
    candidate = (
        raw_dir
        / "csindex_announcements"
        / "details"
        / f"{announcement_date}_{announcement_id}.json"
    )
    return candidate if candidate.exists() else None


def _announcement_lookup(catalog_path: Path) -> dict[str, dict[str, str]]:
    catalog = pd.read_csv(catalog_path, dtype=str).fillna("")
    selected = catalog.loc[catalog["title"].str.contains("沪深300", na=False)].copy()
    return {
        row.publish_date: {
            "announcement_id": row.announcement_id,
            "source_detail_url": row.detail_url,
        }
        for row in selected.itertuples()
    }


def _load_reviewed_manifests(events_dir: Path) -> pd.DataFrame:
    frames: list[pd.DataFrame] = []
    for name in REVIEWED_MANIFESTS:
        path = events_dir / name
        if not path.exists():
            raise FileNotFoundError(f"Reviewed manifest is missing: {path}")
        frame = pd.read_csv(path, dtype=str).fillna("")
        frame["security_name"] = ""
        frames.append(frame)
    return pd.concat(frames, ignore_index=True)


SPECIAL_ANNOUNCEMENT_IDS = {
    "2882",
    "3453",
    "6271",
    "6802",
    "15546",
    "1006022",
}


def _load_manual_seed(path: Path) -> pd.DataFrame:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, str]] = []
    for period, event in payload.items():
        for pair in event["pairs"]:
            common = {
                "index_code": "000300.SH",
                "effective_date": event["effective_date"],
                "announcement_date": event["announcement_date"],
                "source_url": event["source_url"],
                "announcement_id": str(event["announcement_id"]),
                "effective_date_source_url": event.get("effective_source_url", ""),
                "period": period,
                "event_type": event.get("event_type", ""),
            }
            rows.append(
                common
                | {
                    "ts_code": pair["removed"],
                    "security_name": pair["removed_name"],
                    "action": "REMOVE",
                }
            )
            rows.append(
                common
                | {
                    "ts_code": pair["added"],
                    "security_name": pair["added_name"],
                    "action": "ADD",
                }
            )
    return pd.DataFrame(rows)


def build_event_table(
    events_dir: Path,
    manual_seed: Path | list[Path] | tuple[Path, ...],
    catalog_path: Path,
    raw_dir: Path,
    log=print,
) -> pd.DataFrame:
    manual_paths = [manual_seed] if isinstance(manual_seed, Path) else list(manual_seed)
    events = pd.concat(
        [_load_reviewed_manifests(events_dir), *[_load_manual_seed(path) for path in manual_paths]],
        ignore_index=True,
        sort=False,
    ).fillna("")
    events["ts_code"] = events["ts_code"].map(to_ts_code)
    events["action"] = events["action"].str.upper().str.strip()
    events["effective_date"] = pd.to_datetime(events["effective_date"]).dt.strftime("%Y-%m-%d")
    events["announcement_date"] = pd.to_datetime(events["announcement_date"]).dt.strftime("%Y-%m-%d")

    lookup = _announcement_lookup(catalog_path)
    if "announcement_id" not in events:
        events["announcement_id"] = ""
    if "effective_date_source_url" not in events:
        events["effective_date_source_url"] = ""
    events["source_detail_url"] = events.apply(
        lambda row: row.get("source_url", "")
        or lookup.get(row["announcement_date"], {}).get("source_detail_url", ""),
        axis=1,
    )
    events["announcement_id"] = events.apply(
        lambda row: row["announcement_id"]
        or lookup.get(row["announcement_date"], {}).get("announcement_id", ""),
        axis=1,
    )
    if "event_type" not in events:
        events["event_type"] = ""
    inferred_special = events["announcement_id"].isin(SPECIAL_ANNOUNCEMENT_IDS)
    events["event_type"] = events["event_type"].str.upper().str.strip()
    events.loc[events["event_type"].eq(""), "event_type"] = inferred_special.map(
        {True: "SPECIAL", False: "REGULAR"}
    )
    invalid_event_types = sorted(set(events["event_type"]) - {"REGULAR", "SPECIAL"})
    if invalid_event_types:
        raise ValueError(f"Unsupported event types: {invalid_event_types}")
    events["event_id"] = events.apply(
        lambda row: (
            f"CSI300-{row['effective_date'].replace('-', '')}-{row['event_type']}"
            f"-{row['announcement_id'] or 'UNSPECIFIED'}"
        ),
        axis=1,
    )
    events["source_local_path"] = events["effective_date"].map(
        lambda value: str(raw_dir / RAW_DOCUMENTS_BY_EFFECTIVE_DATE[value])
        if value in RAW_DOCUMENTS_BY_EFFECTIVE_DATE
        else ""
    )
    missing_attachment = events["source_local_path"].eq("")
    events.loc[missing_attachment, "source_local_path"] = events.loc[missing_attachment].apply(
        lambda row: str(path)
        if (
            path := _archived_detail_path(
                raw_dir, row["announcement_date"], row["announcement_id"]
            )
        )
        else "",
        axis=1,
    )
    events["source_document_sha256"] = events["source_local_path"].map(
        lambda value: _sha256(Path(value)) if value and Path(value).exists() else ""
    )
    events["verification_status"] = "VERIFIED_OFFICIAL"

    actual_dates = tuple(sorted(events["effective_date"].unique()))
    if actual_dates != EXPECTED_EFFECTIVE_DATES:
        missing = sorted(set(EXPECTED_EFFECTIVE_DATES) - set(actual_dates))
        unexpected = sorted(set(actual_dates) - set(EXPECTED_EFFECTIVE_DATES))
        raise ValueError(f"Event-date coverage failed: missing={missing}, unexpected={unexpected}")
    if events.duplicated(["event_id", "ts_code", "action"]).any():
        raise ValueError("Duplicate event/action/security rows detected")
    if (pd.to_datetime(events["announcement_date"]) > pd.to_datetime(events["effective_date"])).any():
        raise ValueError("announcement_date cannot follow effective_date")

    for position, effective_date in enumerate(EXPECTED_EFFECTIVE_DATES, start=1):
        group = events.loc[events["effective_date"].eq(effective_date)]
        added = set(group.loc[group["action"].eq("ADD"), "ts_code"])
        removed = set(group.loc[group["action"].eq("REMOVE"), "ts_code"])
        if not added or len(added) != len(removed) or added & removed:
            raise ValueError(
                f"Invalid event {effective_date}: ADD={len(added)}, REMOVE={len(removed)}, "
                f"overlap={sorted(added & removed)}"
            )
        log(
            f"[{position}/{len(EXPECTED_EFFECTIVE_DATES)}] event={group['event_id'].iloc[0]} "
            f"effective_date={effective_date} REMOVE={len(removed)} ADD={len(added)} status=PASS"
        )

    columns = [
        "event_id",
        "event_type",
        "index_code",
        "ts_code",
        "security_name",
        "action",
        "effective_date",
        "announcement_date",
        "announcement_id",
        "source_url",
        "source_detail_url",
        "effective_date_source_url",
        "source_local_path",
        "source_document_sha256",
        "verification_status",
    ]
    action_order = pd.Categorical(events["action"], categories=["REMOVE", "ADD"], ordered=True)
    events = events.assign(_action_order=action_order).sort_values(
        ["effective_date", "_action_order", "ts_code"]
    )
    return events[columns].reset_index(drop=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--events-dir", type=Path, default=Path("data/reference/official_membership")
    )
    parser.add_argument(
        "--manual-seed",
        action="append",
        type=Path,
        default=None,
        help="Local reviewed JSON seed; repeat to combine multiple periods",
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("data/reference/official_membership/official_announcement_catalog.csv"),
    )
    parser.add_argument("--raw-dir", type=Path, default=Path("data/official"))
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(
            "data/reference/official_membership/csi300_events_200507_202606.csv"
        ),
    )
    args = parser.parse_args()
    manual_seeds = args.manual_seed or [
        Path("data/reference/official_membership/.local_csi300_manual_events_2005_2010.json"),
        Path("data/reference/official_membership/.local_csi300_manual_events_2006_multidate.json"),
        Path("data/reference/official_membership/.local_csi300_manual_events_2011_2012.json"),
        Path("data/reference/official_membership/.local_csi300_manual_events_2013_2017.json"),
        Path("data/reference/official_membership/.local_csi300_manual_events_2019_2025.json"),
    ]
    table = build_event_table(
        args.events_dir, manual_seeds, args.catalog, args.raw_dir, log=print
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    table.to_csv(args.output, index=False, encoding="utf-8-sig")
    event_count = table["event_id"].nunique()
    print(
        f"[complete] events={event_count} rows={len(table)} "
        f"first={table['effective_date'].min()} last={table['effective_date'].max()} "
        f"output={args.output}"
    )


if __name__ == "__main__":
    main()
