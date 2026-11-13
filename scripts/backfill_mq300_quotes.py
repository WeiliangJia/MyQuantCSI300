"""Fill the local MQ300 missing-quote queue with unadjusted BaoStock data."""
from pathlib import Path

import numpy as np
import pandas as pd

from myquant.config import get_settings
from myquant.data.archive import RawArchiver
from myquant.data.free_adapters import BaoStockClient


def main():
    folder = Path("data/exports/mq300-2024-simulated")
    tasks = pd.read_json(folder / "backfill_quote.json", dtype={"trade_date": str})
    archive = RawArchiver(get_settings().raw_archive_path)
    client = BaoStockClient()
    result = []
    for code, rows in tasks.groupby("ts_code"):
        start, end = rows.trade_date.min(), rows.trade_date.max()
        try:
            bars = client.daily_bars(code, start, end)
            bars = bars.loc[bars.ts_code.eq(code) & bars.trade_date.isin(rows.trade_date)]
            fields = ["open", "close", "pre_close", "volume"]
            bars = bars.loc[(bars[fields].gt(0) & np.isfinite(bars[fields])).all(axis=1)]
            if bars.trade_date.duplicated().any():
                raise ValueError("Duplicate provider dates")
            path = None
            if not bars.empty:
                path, _ = archive.archive(
                    "daily", {"ts_code": code, "start": start, "end": end, "purpose": "mq300_gap"},
                    bars, source="baostock",
                )
            covered = set(bars.trade_date)
            for row in rows.to_dict("records"):
                found = row["trade_date"] in covered
                result.append({**row, "result": "recovered" if found else "no_valid_quote",
                               "archive_path": str(path) if found else None})
            print(f"{code}: {len(bars)}/{len(rows)}", flush=True)
        except Exception as exc:
            result.extend({**row, "result": "provider_error", "error": type(exc).__name__}
                          for row in rows.to_dict("records"))
            print(f"{code}: {type(exc).__name__}", flush=True)
    report = pd.DataFrame(result)
    report.to_json(folder / "quote_backfill_results.json", orient="records")
    print(report.result.value_counts().to_dict())


if __name__ == "__main__":
    main()
