import pandas as pd
import pytest

from myquant.data.free_adapters import (
    OfficialAnnouncementClient,
    normalize_akshare_free_float_daily,
    normalize_akshare_daily,
    normalize_baostock_daily,
    to_baostock_code,
    to_ts_code,
)


def test_code_conversion_supports_baostock_and_standard_codes() -> None:
    assert to_ts_code("sh.600000") == "600000.SH"
    assert to_ts_code("000001") == "000001.SZ"
    assert to_baostock_code("000001.SZ") == "sz.000001"


def test_baostock_daily_normalization() -> None:
    raw = pd.DataFrame([{
        "date": "2024-01-02", "code": "sh.600000", "open": "10", "high": "11", "low": "9",
        "close": "10.5", "preclose": "10", "volume": "1000", "amount": "10000", "turn": "1", "pctChg": "5", "isST": "0",
    }])
    normalized = normalize_baostock_daily(raw)
    assert normalized.loc[0, "ts_code"] == "600000.SH"
    assert normalized.loc[0, "pre_close"] == 10


def test_akshare_daily_normalization() -> None:
    raw = pd.DataFrame([{
        "日期": "2024-01-02", "开盘": 10, "最高": 11, "最低": 9, "收盘": 10.5,
        "成交量": 1000, "成交额": 10000, "换手率": 1.2,
    }])
    normalized = normalize_akshare_daily(raw, "000001.SZ")
    assert normalized.loc[0, "ts_code"] == "000001.SZ"
    assert normalized.loc[0, "turnover_rate"] == 1.2


def test_akshare_free_float_normalization_uses_outstanding_shares() -> None:
    raw = pd.DataFrame([{
        "date": "2024-01-02", "close": 10.5, "outstanding_share": 200_000_000,
    }])
    normalized = normalize_akshare_free_float_daily(raw, "000001.SZ")
    assert normalized.loc[0, "circ_mv"] == 2_100_000_000


def test_official_manifest_rejects_invalid_actions(tmp_path) -> None:
    path = tmp_path / "invalid.csv"
    path.write_text(
        "index_code,ts_code,action,effective_date,announcement_date,source_url\n"
        "000300.SH,600000.SH,SWAP,2024-06-17,2024-05-31,https://example.com\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Invalid membership action"):
        OfficialAnnouncementClient.read_manifest(path)
