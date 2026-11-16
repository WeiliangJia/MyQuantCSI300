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
    ("000333.SZ", "2024-05-15"): ("2.9791154", "0"),
    ("000408.SZ", "2024-04-19"): ("0.7949780", "0"),
    ("000651.SZ", "2024-08-28"): ("2.3337380", "0"),
    ("000661.SZ", "2024-04-18"): ("4.4736091", "0"),
    ("002049.SZ", "2024-06-24"): ("0.6748809", "0"),
    ("002271.SZ", "2024-05-22"): ("0.584913", "0"),
    ("002352.SZ", "2024-05-14"): ("0.5902123", "0"),
    ("002493.SZ", "2024-06-05"): ("0.0945446", "0"),
    ("002714.SZ", "2024-12-30"): ("0.8246588", "0"),
    ("002812.SZ", "2024-06-03"): ("1.5341249", "0"),
    ("002821.SZ", "2024-06-28"): ("1.7324117", "0"),
    ("002841.SZ", "2024-05-29"): ("0.8438645", "0"),
    ("300274.SZ", "2024-06-13"): ("0.9552535", "0.3959600"),
    ("300896.SZ", "2024-04-29"): ("2.3104823", "0.3978445"),
    ("300919.SZ", "2024-05-29"): ("1.1532717", "0.3976799"),
    ("600050.SH", "2024-10-30"): ("0.0944", "0"),
    ("600585.SH", "2024-06-24"): ("0.95", "0"),
    ("600690.SH", "2024-08-16"): ("0.78683", "0"),
    ("601799.SH", "2024-06-06"): ("1.1920", "0"),
    ("603369.SH", "2024-06-12"): ("0.99", "0"),
    ("603799.SH", "2024-06-17"): ("0.9871", "0"),
    ("603833.SH", "2024-07-09"): ("2.74", "0"),
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
