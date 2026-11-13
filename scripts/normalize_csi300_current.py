"""Normalize the official current CSI 300 constituent and close-weight files.

The source files are the public ``000300cons.xls`` and
``000300closeweight.xls`` downloads linked from the CSI 300 index detail page.
Outputs under ``data/official`` and ``data/reference/official_membership`` are
intentionally ignored by Git under the project's data publication policy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd


CODE_COLUMN = "成份券代码Constituent Code"
NAME_COLUMN = "成份券名称Constituent Name"
NAME_EN_COLUMN = "成份券英文名称Constituent Name(Eng)"
EXCHANGE_COLUMN = "交易所Exchange"
DATE_COLUMN = "日期Date"
WEIGHT_COLUMN = "权重(%)weight"

EXCHANGE_SUFFIX = {
    "上海证券交易所": "SH",
    "深圳证券交易所": "SZ",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date_value(frame: pd.DataFrame, label: str) -> str:
    values = frame[DATE_COLUMN].dropna().astype(str).str.replace(r"\.0$", "", regex=True).unique()
    if len(values) != 1 or not values[0].isdigit() or len(values[0]) != 8:
        raise ValueError(f"{label} must contain exactly one YYYYMMDD date; got {values.tolist()}")
    return pd.Timestamp(values[0]).date().isoformat()


def _normalize_code(code: object, exchange: object) -> str:
    raw = str(code).strip().replace(".0", "").zfill(6)
    if len(raw) != 6 or not raw.isdigit():
        raise ValueError(f"Invalid constituent code: {code!r}")
    suffix = EXCHANGE_SUFFIX.get(str(exchange).strip())
    if suffix is None:
        raise ValueError(f"Unsupported exchange: {exchange!r}")
    return f"{raw}.{suffix}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("constituents", type=Path)
    parser.add_argument("weights", type=Path)
    parser.add_argument("output_csv", type=Path)
    parser.add_argument("baseline_csv", type=Path)
    parser.add_argument("metadata_json", type=Path)
    parser.add_argument("--constituents-url", required=True)
    parser.add_argument("--weights-url", required=True)
    args = parser.parse_args()

    constituents = pd.read_excel(args.constituents, dtype=str)
    weights = pd.read_excel(args.weights, dtype=str)

    required_constituent = {DATE_COLUMN, CODE_COLUMN, NAME_COLUMN, NAME_EN_COLUMN, EXCHANGE_COLUMN}
    required_weight = required_constituent | {WEIGHT_COLUMN}
    missing_constituent = required_constituent - set(constituents.columns)
    missing_weight = required_weight - set(weights.columns)
    if missing_constituent or missing_weight:
        raise ValueError(
            f"Missing columns: constituents={sorted(missing_constituent)}, weights={sorted(missing_weight)}"
        )

    membership_date = _date_value(constituents, "constituents")
    weight_date = _date_value(weights, "weights")
    constituents["ts_code"] = [
        _normalize_code(code, exchange)
        for code, exchange in zip(constituents[CODE_COLUMN], constituents[EXCHANGE_COLUMN], strict=True)
    ]
    weights["ts_code"] = [
        _normalize_code(code, exchange)
        for code, exchange in zip(weights[CODE_COLUMN], weights[EXCHANGE_COLUMN], strict=True)
    ]
    weights["weight_pct"] = pd.to_numeric(weights[WEIGHT_COLUMN], errors="raise")

    if len(constituents) != 300 or constituents["ts_code"].nunique() != 300:
        raise ValueError("Constituent file must contain exactly 300 unique securities")
    if len(weights) != 300 or weights["ts_code"].nunique() != 300:
        raise ValueError("Weight file must contain exactly 300 unique securities")
    if set(constituents["ts_code"]) != set(weights["ts_code"]):
        raise ValueError("Constituent and weight files contain different security sets")
    if weights["weight_pct"].isna().any() or (weights["weight_pct"] <= 0).any():
        raise ValueError("Weights must be complete and strictly positive")

    weight_sum_pct = float(weights["weight_pct"].sum())
    if abs(weight_sum_pct - 100.0) > 0.05:
        raise ValueError(f"Rounded weight sum is outside tolerance: {weight_sum_pct:.6f}%")

    acquired_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    constituents_hash = _sha256(args.constituents)
    weights_hash = _sha256(args.weights)
    source_revision = hashlib.sha256(f"{constituents_hash}:{weights_hash}".encode()).hexdigest()

    weight_fields = weights[["ts_code", "weight_pct"]]
    normalized = constituents.merge(weight_fields, on="ts_code", how="inner", validate="one_to_one")
    normalized = normalized.rename(
        columns={
            NAME_COLUMN: "security_name",
            NAME_EN_COLUMN: "security_name_en",
            EXCHANGE_COLUMN: "exchange",
        }
    )
    normalized["index_code"] = "000300.SH"
    normalized["index_name"] = "沪深300"
    normalized["membership_as_of_date"] = membership_date
    normalized["weight_as_of_date"] = weight_date
    normalized["weight"] = normalized["weight_pct"] / 100.0
    normalized["source"] = "csindex_official_download"
    normalized["source_url_constituents"] = args.constituents_url
    normalized["source_url_weights"] = args.weights_url
    normalized["source_sha256_constituents"] = constituents_hash
    normalized["source_sha256_weights"] = weights_hash
    normalized["available_at"] = acquired_at
    normalized["ingested_at"] = acquired_at
    normalized["source_revision"] = source_revision
    normalized = normalized[
        [
            "index_code",
            "index_name",
            "ts_code",
            "security_name",
            "security_name_en",
            "exchange",
            "membership_as_of_date",
            "weight_as_of_date",
            "weight",
            "weight_pct",
            "source",
            "source_url_constituents",
            "source_url_weights",
            "source_sha256_constituents",
            "source_sha256_weights",
            "available_at",
            "ingested_at",
            "source_revision",
        ]
    ].sort_values("ts_code")

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    args.baseline_csv.parent.mkdir(parents=True, exist_ok=True)
    args.metadata_json.parent.mkdir(parents=True, exist_ok=True)
    normalized.to_csv(args.output_csv, index=False, encoding="utf-8-sig")
    normalized[["ts_code"]].assign(as_of_date=membership_date).to_csv(
        args.baseline_csv, index=False, encoding="utf-8-sig"
    )

    metadata = {
        "index_code": "000300.SH",
        "membership_as_of_date": membership_date,
        "weight_as_of_date": weight_date,
        "row_count": int(len(normalized)),
        "unique_code_count": int(normalized["ts_code"].nunique()),
        "weight_sum_pct": weight_sum_pct,
        "weight_rounding_difference_pct": weight_sum_pct - 100.0,
        "constituents_sha256": constituents_hash,
        "weights_sha256": weights_hash,
        "source_revision": source_revision,
        "constituents_url": args.constituents_url,
        "weights_url": args.weights_url,
        "acquired_at": acquired_at,
    }
    args.metadata_json.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(metadata, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
