"""Cross-check the local factor ledger against Eastmoney's ex-date calendar."""
from pathlib import Path

import pandas as pd
import requests

from myquant.config import get_settings
from myquant.data.archive import RawArchiver
from reconcile_suspended_dividend_2024 import huadian_inputs

KEY = ["ts_code", "trade_date"]
OUT = Path("data/exports/mq300-2024-simulated")
URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"


def calendar():
    params = {
        "reportName": "RPT_SHAREBONUS_DET", "columns": "ALL",
        "pageSize": 500, "source": "WEB", "client": "WEB",
        "sortColumns": "EX_DIVIDEND_DATE,SECUCODE,REPORT_DATE", "sortTypes": "1,1,1",
        "filter": "(EX_DIVIDEND_DATE>='2023-12-01')(EX_DIVIDEND_DATE<'2025-01-01')",
    }
    archive = RawArchiver(get_settings().raw_archive_path)
    frames = []
    with requests.Session() as session:
        page = 1
        while True:
            query = {**params, "pageNumber": page}
            response = session.get(URL, params=query, timeout=20)
            response.raise_for_status()
            result = response.json()["result"]
            frame = pd.DataFrame(result["data"])
            archive.archive("sharebonus_calendar", query, frame, source="eastmoney")
            frames.append(frame)
            if page == 1:
                count, pages = result["count"], result["pages"]
            if (result["count"], result["pages"]) != (count, pages):
                raise ValueError("Calendar changed during pagination")
            if page == pages:
                break
            page += 1
    events = pd.concat(frames, ignore_index=True)
    if len(events) != count or events.duplicated(["SECUCODE", "EX_DIVIDEND_DATE", "REPORT_DATE"]).any():
        raise ValueError("Calendar pagination has gaps or overlapping rows")
    return events


def main():
    factors = pd.read_json(OUT / "factors.json", dtype={"trade_date": str})
    status = pd.read_json(OUT / "confirmed_suspensions.json", dtype={"trade_date": str})
    adjusted, dividends = huadian_inputs(factors)
    status_keys = set(status[KEY].itertuples(index=False, name=None))
    if not set(dividends[KEY].itertuples(index=False, name=None)) <= status_keys:
        raise ValueError("Reviewed dividend date is not covered by confirmed suspension evidence")
    adjusted.to_json(OUT / "replay_adjusted_bars.json", orient="records", double_precision=15)
    dividends.to_json(OUT / "replay_suspended_dividends.json", orient="records", double_precision=15)
    events = calendar()
    events = events.rename(columns={"SECUCODE": "ts_code", "EX_DIVIDEND_DATE": "trade_date"})
    events.trade_date = events.trade_date.str[:10]
    if not events.trade_date.between("2023-12-01", "2024-12-31").all():
        raise ValueError("Calendar returned an out-of-range date")
    scope = pd.concat([factors[KEY], status[KEY]]).drop_duplicates()
    scoped = events.merge(scope, on=KEY)
    scoped.to_json(OUT / "independent_dividend_calendar.json", orient="records")
    changes = factors.loc[factors.factor_step.ne(1), KEY + ["previous_close", "pre_close", "factor_step"]]
    # The independent calendar uses ex-date; the original factor ledger uses resume date.
    matches = scoped.copy()
    huadian = matches.ts_code.eq("600027.SH") & matches.trade_date.eq("2024-07-25")
    matches["ex_date"] = matches.trade_date
    matches.loc[huadian, "trade_date"] = "2024-08-02"
    audit = matches.merge(changes, on=KEY, how="outer", indicator=True)
    audit["classification"] = audit["_merge"].map({
        "left_only": "event_without_factor_change",
        "right_only": "factor_change_without_dividend_calendar_event",
        "both": "date_matched_terms_not_yet_verified",
    }).astype(str)
    anchors = factors.loc[factors.previous_trade_date.isna(), KEY].assign(is_anchor=True)
    audit = audit.merge(anchors, on=KEY, how="left", validate="many_to_one")
    audit.loc[audit.is_anchor.eq(True) & audit["_merge"].eq("left_only"), "classification"] = (
        "event_on_factor_anchor_requires_prior_quote_review"
    )
    audit.drop(columns="_merge").to_json(OUT / "corporate_action_date_audit.json", orient="records")
    print("Calendar rows:", len(events), "Scoped events:", len(scoped))
    print(audit.groupby([audit.trade_date.str[:4], "classification"], observed=True).size().to_string())
    print("Annual NAV blocked: unresolved dates, terms, and independent rights-issue coverage.")


if __name__ == "__main__":
    main()
