"""Inventory announcement titles by replay ticker; a title scan never clears the NAV gate."""
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

OUT = Path("data/exports/mq300-2024-simulated")
START, END = "2023-12-01", "2024-12-31"
URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
TOPICS = re.compile(
    r"换股|股份转换|拆细|拆股|股份拆分|股票分割|缩股|股份合并|"
    r"实物分[派红]|非现金分[派红]|以股.{0,8}分[派红]|配股|"
    r"吸收合并|合并实施|权益分派实施|分红派息实施"
)


def scan(code, destination):
    params = dict(stock_list=code[:6], begin_time=START, end_time=END, sr=-1,
                  page_size=100, page_index=1, ann_type="A", client_source="web", f_node=0, s_node=0)
    rows = {}
    total = None
    try:
        with requests.Session() as session:
            while True:
                response = session.get(URL, params=params, timeout=12)
                response.raise_for_status()
                payload = response.json()["data"]
                if total is None:
                    total = int(payload["total_hits"])
                if total <= 0 or int(payload["total_hits"]) != total or not payload["list"]:
                    raise ValueError("Empty or unstable announcement index")
                for row in payload["list"]:
                    key = row["art_code"]
                    day = row["notice_date"][:10]
                    if (key in rows or not START <= day <= END
                            or not any(s["stock_code"] == code[:6] for s in row["codes"])):
                        raise ValueError("Duplicate or out-of-scope announcement")
                    rows[key] = dict(date=day, title=row["title"], art_code=key,
                                     url=f"https://data.eastmoney.com/notices/detail/{code[:6]}/{key}.html")
                if len(rows) >= total:
                    if len(rows) != total:
                        raise ValueError("Unexpected announcement count")
                    break
                params["page_index"] += 1
        state, error = "index_scanned_body_not_verified", None
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        state, error = "uncovered_or_partial", type(exc).__name__
    record = dict(ts_code=code, from_date=START, through_date=END, source=URL, state=state,
                  error=error, expected=total, collected=len(rows),
                  notices=list(rows.values()), candidates=[r for r in rows.values() if TOPICS.search(r["title"])])
    (destination / f"{code}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")


def main():
    codes = sorted({row["ts_code"] for name in ("factors", "confirmed_suspensions", "anomalies")
                    for row in json.loads((OUT / f"{name}.json").read_text(encoding="utf-8"))})
    destination = OUT / "notice_index"
    destination.mkdir(exist_ok=True)
    pending = []
    for code in codes:
        path = destination / f"{code}.json"
        if path.exists():
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record["state"], record["from_date"], record["through_date"], record["source"]) == (
                "index_scanned_body_not_verified", START, END, URL,
            ):
                continue
        pending.append(code)
    if "--summarize-only" not in sys.argv:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for i, _ in enumerate(pool.map(lambda code: scan(code, destination), pending), 1):
                if i % 25 == 0:
                    print(f"Checked {i}/{len(pending)} pending tickers", flush=True)
    summary = []
    for code in codes:
        path = destination / f"{code}.json"
        if not path.exists():
            summary.append(dict(ts_code=code, from_date=START, through_date=END, source=URL,
                                state="not_scanned", expected=None, collected=0, candidates=[]))
            continue
        row = json.loads(path.read_text(encoding="utf-8"))
        row["candidates"] = [notice for notice in row.pop("notices") if TOPICS.search(notice["title"])]
        summary.append(row)
    (OUT / "notice_coverage.json").write_text(json.dumps(summary, ensure_ascii=False), encoding="utf-8")
    print(f"Inventoried {len(summary)} tickers; NAV gate remains closed pending evidence review.")


if __name__ == "__main__":
    main()
