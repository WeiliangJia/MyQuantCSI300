"""Reprice matched corporate actions using independent per-ten-share terms."""
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pandas as pd

OUT = Path("data/exports/mq300-2024-simulated")
# Cash and shares on the reference-price basis, from implementation notices.
OVERRIDES = {
    ("600050.SH", "2023-12-08"): ("0.0783", "0"),
    ("601318.SH", "2024-07-26"): ("1.4857", "0"),
    ("300308.SZ", "2024-06-06"): ("0.4408015", "0.3918236"),
    ("601899.SH", "2023-12-25"): ("0.05", "0"),
    ("002352.SZ", "2024-11-07"): ("1.3939620", "0"),
    ("300750.SZ", "2024-04-30"): ("5.0147304", "0"),
    ("600803.SH", "2024-08-01"): ("0.9054", "0"),
    ("601318.SH", "2024-10-18"): ("0.9211", "0"),
    ("601898.SH", "2024-08-20"): ("0.555", "0"),
}


def main():
    audit = pd.read_json(OUT / "corporate_action_date_audit.json")
    rows = []
    for row in audit.loc[audit.classification.str.startswith("date_matched")].itertuples(index=False):
        override = OVERRIDES.get((row.ts_code, row.ex_date))
        cash, shares = (
            tuple(map(Decimal, override)) if override else (
                Decimal(format(0 if pd.isna(row.PRETAX_BONUS_RMB) else row.PRETAX_BONUS_RMB, ".12g")) / 10,
                Decimal(format(0 if pd.isna(row.BONUS_IT_RATIO) else row.BONUS_IT_RATIO, ".12g")) / 10,
            )
        )
        prior = Decimal(str(row.previous_close))
        observed = Decimal(str(row.pre_close)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        expected = ((prior - cash) / (1 + shares)).quantize(Decimal(".01"), rounding=ROUND_HALF_UP)
        rows.append({
            "ts_code": row.ts_code, "ex_date": row.ex_date, "factor_date": row.trade_date,
            "prior_close": str(prior), "cash_per_share": str(cash), "added_shares_per_share": str(shares),
            "terms_source": "issuer_notice" if override else "eastmoney_sharebonus",
            "expected_pre_close": str(expected), "observed_pre_close": str(observed),
            "price_difference": str(observed - expected), "matches_cent": observed == expected,
        })
    result = pd.DataFrame(rows)
    result.to_json(OUT / "corporate_action_terms_audit.json", orient="records")
    print(result.groupby([result.ex_date.str[:4], "matches_cent"]).size().to_string())
    print(result.loc[~result.matches_cent].to_string(index=False))


if __name__ == "__main__":
    main()
