import pandas as pd

from myquant.data.pit import as_of


def test_as_of_excludes_future_revision() -> None:
    frame = pd.DataFrame(
        {
            "ts_code": ["000001.SZ", "000001.SZ"],
            "metric": ["roe", "roe"],
            "value": [0.10, 0.20],
            "available_at": ["2024-04-30", "2024-08-30"],
        }
    )
    observed = as_of(frame, "2024-05-01", ["ts_code", "metric"])
    assert observed.iloc[0]["value"] == 0.10

