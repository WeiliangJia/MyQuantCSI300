from __future__ import annotations

import re
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from io import StringIO

import requests

import pandas as pd


def to_ts_code(code: str) -> str:
    """Convert a 6-digit, BaoStock or already-normalized Chinese ticker to `000001.SZ`."""
    code = code.strip().upper()
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
        return code
    if re.fullmatch(r"(SH|SZ|BJ)\.\d{6}", code):
        market, symbol = code.split(".")
        return f"{symbol}.{market}"
    if not re.fullmatch(r"\d{6}", code):
        raise ValueError(f"Unsupported mainland stock code: {code}")
    return f"{code}.SH" if code.startswith(("5", "6", "9")) else f"{code}.SZ"


def to_baostock_code(ts_code: str) -> str:
    symbol, market = to_ts_code(ts_code).split(".")
    return f"{market.lower()}.{symbol}"


def normalize_baostock_daily(frame: pd.DataFrame) -> pd.DataFrame:
    required = {"date", "code", "open", "high", "low", "close", "volume", "amount"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"BaoStock daily response missing fields: {sorted(missing)}")
    result = frame.rename(columns={"date": "trade_date", "code": "ts_code", "preclose": "pre_close"}).copy()
    result["ts_code"] = result["ts_code"].map(to_ts_code)
    for column in ("open", "high", "low", "close", "pre_close", "volume", "amount", "turn", "pctChg", "isST"):
        if column in result:
            result[column] = pd.to_numeric(result[column], errors="coerce")
    # BaoStock volume is shares, which is the project's internal convention.
    return result


def normalize_akshare_daily(frame: pd.DataFrame, ts_code: str) -> pd.DataFrame:
    mapping = {
        "日期": "trade_date", "开盘": "open", "最高": "high", "最低": "low", "收盘": "close",
        "成交量": "volume", "成交额": "amount", "换手率": "turnover_rate", "涨跌幅": "pct_change",
    }
    result = frame.rename(columns=mapping).copy()
    required = {"trade_date", "open", "high", "low", "close", "volume", "amount"}
    missing = required.difference(result.columns)
    if missing:
        raise ValueError(f"AKShare daily response missing fields: {sorted(missing)}")
    result["ts_code"] = to_ts_code(ts_code)
    for column in required.difference({"trade_date"}):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    # Eastmoney's `成交量` is in shares for stock_zh_a_hist.
    return result


def normalize_akshare_free_float_daily(frame: pd.DataFrame, ts_code: str) -> pd.DataFrame:
    """Normalise AKShare's Sina daily outstanding-share history.

    ``outstanding_share`` is a freely available floating-share proxy.  The
    result is deliberately labelled as a model input, not a published CSI
    free-float adjustment factor or official constituent weight.
    """
    result = frame.rename(columns={"date": "trade_date"}).copy()
    required = {"trade_date", "close", "outstanding_share"}
    missing = required.difference(result.columns)
    if missing:
        raise ValueError(f"AKShare free-float response missing fields: {sorted(missing)}")
    result["ts_code"] = to_ts_code(ts_code)
    for column in ("close", "outstanding_share"):
        result[column] = pd.to_numeric(result[column], errors="coerce")
    result["circ_mv"] = result["close"] * result["outstanding_share"]
    return result[["trade_date", "ts_code", "close", "outstanding_share", "circ_mv"]]


@contextmanager
def _requests_default_timeout(timeout_seconds: float):
    """Bound AKShare's upstream calls, which otherwise omit request timeouts."""
    original_get = requests.get

    def bounded_get(*args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("timeout", timeout_seconds)
        return original_get(*args, **kwargs)

    requests.get = bounded_get
    try:
        yield
    finally:
        requests.get = original_get


class BaoStockClient:
    """Free BaoStock adapter, with lazy imports so the core project stays dependency-light."""

    def __init__(self, module: Any | None = None) -> None:
        if module is None:
            try:
                import baostock as module
            except ImportError as error:
                raise RuntimeError("Install free providers with: pip install -e '.[free]'") from error
        self.module = module

    def daily_bars(self, ts_code: str, start_date: str, end_date: str, adjustflag: str = "3") -> pd.DataFrame:
        login = self.module.login()
        if getattr(login, "error_code", "0") != "0":
            raise RuntimeError(f"BaoStock login failed: {getattr(login, 'error_msg', '')}")
        try:
            response = self.module.query_history_k_data_plus(
                to_baostock_code(ts_code),
                "date,code,open,high,low,close,preclose,volume,amount,turn,pctChg,isST",
                start_date=start_date, end_date=end_date, frequency="d", adjustflag=adjustflag,
            )
            if getattr(response, "error_code", "0") != "0":
                raise RuntimeError(f"BaoStock daily query failed: {getattr(response, 'error_msg', '')}")
            rows: list[list[str]] = []
            while response.next():
                rows.append(response.get_row_data())
            return normalize_baostock_daily(pd.DataFrame(rows, columns=response.fields))
        finally:
            self.module.logout()

    def financial_indicators(self, ts_code: str, year: int, quarter: int) -> pd.DataFrame:
        """Fetch free quarterly profitability and cash-flow data with publication/stat dates."""
        login = self.module.login()
        if getattr(login, "error_code", "0") != "0":
            raise RuntimeError(f"BaoStock login failed: {getattr(login, 'error_msg', '')}")
        try:
            frames: list[pd.DataFrame] = []
            for dataset, query in {
                "profit": self.module.query_profit_data,
                "cashflow": self.module.query_cash_flow_data,
            }.items():
                response = query(code=to_baostock_code(ts_code), year=year, quarter=quarter)
                if getattr(response, "error_code", "0") != "0":
                    continue
                rows: list[list[str]] = []
                while response.next():
                    rows.append(response.get_row_data())
                if rows:
                    frame = pd.DataFrame(rows, columns=response.fields)
                    frame["dataset"] = dataset
                    frame["ts_code"] = to_ts_code(ts_code)
                    frames.append(frame)
            return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
        finally:
            self.module.logout()


class AKShareClient:
    """Free AKShare adapter for supplemental daily data and web-derived fund-flow fields."""

    def __init__(self, module: Any | None = None) -> None:
        if module is None:
            try:
                import akshare as module
            except ImportError as error:
                raise RuntimeError("Install free providers with: pip install -e '.[free]'") from error
        self.module = module

    def daily_bars(self, ts_code: str, start_date: str, end_date: str, adjust: str = "") -> pd.DataFrame:
        symbol = to_ts_code(ts_code).split(".")[0]
        frame = self.module.stock_zh_a_hist(
            symbol=symbol, period="daily", start_date=start_date.replace("-", ""),
            end_date=end_date.replace("-", ""), adjust=adjust
        )
        return normalize_akshare_daily(frame, ts_code)

    def daily_free_float_market_value(
        self, ts_code: str, start_date: str, end_date: str, request_timeout_seconds: float = 30.0,
    ) -> pd.DataFrame:
        """Fetch the unadjusted daily shares outstanding needed for a free-float proxy."""
        symbol, market = to_ts_code(ts_code).split(".")
        with _requests_default_timeout(request_timeout_seconds):
            frame = self.module.stock_zh_a_daily(
                symbol=f"{market.lower()}{symbol}",
                start_date=start_date.replace("-", ""),
                end_date=end_date.replace("-", ""),
                adjust="",
            )
        return normalize_akshare_free_float_daily(frame, ts_code)

    def fund_flow(self, ts_code: str) -> pd.DataFrame:
        normalized = to_ts_code(ts_code)
        symbol, market = normalized.split(".")
        market_name = "sh" if market == "SH" else "sz"
        frame = self.module.stock_individual_fund_flow(symbol=symbol, market=market_name)
        result = frame.copy()
        result["ts_code"] = normalized
        return result

    def csi300_constituents(self) -> pd.DataFrame:
        """Return the currently published CSI 300 constituents from CSIndex via AKShare.

        This is only a *current* snapshot.  It is deliberately kept separate from
        the historical interval source so it cannot accidentally introduce
        survivorship bias into an old backtest.
        """
        frame = self.module.index_stock_cons_csindex(symbol="000300").copy()
        code_columns = [column for column in frame.columns if "代码" in str(column)]
        name_columns = [column for column in frame.columns if "名称" in str(column)]
        if not code_columns:
            raise ValueError("AKShare CSI 300 constituent response has no code column")
        constituent_code_columns = [column for column in code_columns if "成分" in str(column)]
        constituent_name_columns = [column for column in name_columns if "成分" in str(column)]
        code_column = next(
            (column for column in code_columns if frame[column].astype(str).str.fullmatch(r"\d{6}").any()),
            constituent_code_columns[0] if constituent_code_columns else code_columns[0],
        )
        if constituent_code_columns:
            code_column = constituent_code_columns[0]
        result = pd.DataFrame({"symbol": frame[code_column].astype(str).str.extract(r"(\d{6})", expand=False)})
        result = result.dropna(subset=["symbol"])
        result["ts_code"] = result["symbol"].map(to_ts_code)
        name_column = constituent_name_columns[0] if constituent_name_columns else (name_columns[0] if name_columns else None)
        result["name"] = frame.loc[result.index, name_column].astype(str) if name_column else None
        return result[["ts_code", "name"]].drop_duplicates("ts_code").sort_values("ts_code")


@dataclass(frozen=True)
class OfficialMembershipEvent:
    index_code: str
    ts_code: str
    action: str
    effective_date: str
    announcement_date: str
    source_url: str


class OfficialAnnouncementClient:
    """Archives official announcements and imports reviewed CSI 300 adjustment manifests.

    PDFs are intentionally not auto-interpreted: a reviewer converts each announcement into a
    small CSV manifest, preserving the source URL and preventing a brittle PDF parser from
    silently corrupting historical membership.
    """

    def archive_url(self, url: str, destination: Path) -> Path:
        request = Request(url, headers={"User-Agent": "MyQuantCSI300/0.1 research"})
        with urlopen(request, timeout=30) as response:
            payload = response.read()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
        return destination

    @staticmethod
    def read_manifest(path: Path) -> list[OfficialMembershipEvent]:
        frame = pd.read_csv(path, dtype=str).fillna("")
        required = {"index_code", "ts_code", "action", "effective_date", "announcement_date", "source_url"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"Official membership manifest missing columns: {sorted(missing)}")
        events = []
        for row in frame.to_dict("records"):
            action = row["action"].upper()
            if action not in {"ADD", "REMOVE"}:
                raise ValueError(f"Invalid membership action: {action}")
            events.append(OfficialMembershipEvent(
                index_code=row["index_code"], ts_code=to_ts_code(row["ts_code"]), action=action,
                effective_date=row["effective_date"], announcement_date=row["announcement_date"], source_url=row["source_url"],
            ))
        return events


class SinaHistoricalConstituentClient:
    """Public historical CSI constituent intervals; retained as non-official provenance."""

    base_url = "https://money.finance.sina.com.cn/corp/view/vII_HistoryComponent.php?page={page}&indexid={index_code}"

    def __init__(self, get: Callable[..., Any] = requests.get) -> None:
        self.get = get

    def history(self, index_code: str = "000300") -> pd.DataFrame:
        first = self._fetch_page(index_code, 1)
        match = re.search(r"共\s*(\d+)\s*页", first)
        pages = int(match.group(1)) if match else 1
        frames = [self._parse_page(first)]
        for page in range(2, pages + 1):
            frames.append(self._parse_page(self._fetch_page(index_code, page)))
        result = pd.concat(frames, ignore_index=True).drop_duplicates()
        result["ts_code"] = result["symbol"].map(to_ts_code)
        result["entry_date"] = pd.to_datetime(result["entry_date"])
        result["exit_date"] = pd.to_datetime(result["exit_date"], errors="coerce")
        return result[["ts_code", "name", "entry_date", "exit_date"]].sort_values(["entry_date", "ts_code"])

    def _fetch_page(self, index_code: str, page: int) -> str:
        response = self.get(self.base_url.format(page=page, index_code=index_code), headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        response.raise_for_status()
        response.encoding = response.apparent_encoding or "gb18030"
        return response.text

    @staticmethod
    def _parse_page(html: str) -> pd.DataFrame:
        tables = pd.read_html(StringIO(html))
        for table in tables:
            if table.shape[1] < 4:
                continue
            # The Sina page renders the column headings as the first *row*, not
            # HTML <th> elements.  Therefore pandas assigns generic/duplicated
            # column names and the header must be detected in the body.
            body = table.iloc[:, :4].copy()
            header_positions = body.index[body.iloc[:, 0].astype(str).str.strip().eq("品种代码")]
            if header_positions.empty:
                continue
            data = body.loc[body.index > header_positions[0]].copy()
            data.columns = ["symbol", "name", "entry_date", "exit_date"]
            data["symbol"] = data["symbol"].astype(str).str.strip().str.replace(r"\.0$", "", regex=True)
            return data.loc[data["symbol"].str.fullmatch(r"\d{6}")]
        raise ValueError("Sina historical-component page did not contain the expected table")
