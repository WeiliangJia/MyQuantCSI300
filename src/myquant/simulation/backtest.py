from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from myquant.simulation.broker import PaperBroker


@dataclass(frozen=True)
class BacktestResult:
    nav: pd.DataFrame
    fills: pd.DataFrame


def is_weekly_signal_day(trade_dates: pd.Series) -> pd.Series:
    dates = pd.to_datetime(trade_dates)
    return dates.dt.to_period("W-FRI") != dates.shift(-1).dt.to_period("W-FRI")


class WeeklyOpenBacktester:
    """Turns pre-approved weekly target weights into point-in-time daily paper fills."""

    def __init__(self, broker: PaperBroker) -> None:
        self.broker = broker

    def run(
        self,
        bars: pd.DataFrame,
        targets: pd.DataFrame,
        corporate_actions: pd.DataFrame | None = None,
    ) -> BacktestResult:
        market = bars.copy()
        market["trade_date"] = pd.to_datetime(market["trade_date"])
        market = market.sort_values(["trade_date", "ts_code"])
        if "volume" in market:
            market["adv_volume"] = market.groupby("ts_code")["volume"].transform(
                lambda values: values.shift(1).rolling(20, min_periods=5).mean()
            )
        targets = targets.copy()
        targets["execution_date"] = pd.to_datetime(targets["execution_date"])
        actions = corporate_actions.copy() if corporate_actions is not None else pd.DataFrame()
        if not actions.empty:
            actions["ex_date"] = pd.to_datetime(actions["ex_date"])
        fills: list[dict[str, object]] = []
        nav_rows: list[dict[str, object]] = []
        latest_targets: pd.Series = pd.Series(dtype=float)
        for day, daily in market.groupby("trade_date", sort=True):
            date_string = day.strftime("%Y-%m-%d")
            self.broker.settle(date_string)
            if not actions.empty:
                for action in actions.loc[actions["ex_date"] == day].itertuples():
                    if action.action_type == "cash_dividend":
                        self.broker.apply_cash_dividend(action.ts_code, float(action.cash_per_share))
            scheduled = targets.loc[targets["execution_date"] == day]
            if not scheduled.empty:
                latest_targets = scheduled.set_index("ts_code")["target_weight"]
                self._rebalance(date_string, daily, latest_targets, fills)
            closes = daily.set_index("ts_code")["close"].to_dict()
            equity = self.broker.cash + self.broker.market_value(closes)
            nav_rows.append({"trade_date": day, "nav": equity, "cash": self.broker.cash, "market_value": equity - self.broker.cash})
        return BacktestResult(pd.DataFrame(nav_rows), pd.DataFrame(fills))

    def _rebalance(self, trade_date: str, daily: pd.DataFrame, targets: pd.Series, fills: list[dict[str, object]]) -> None:
        closes = daily.set_index("ts_code")["open"].to_dict()
        equity = self.broker.cash + self.broker.market_value(closes)
        all_codes = set(targets.index) | set(self.broker.positions)
        market_rows = daily.set_index("ts_code")
        for code in sorted(all_codes):
            if code not in market_rows.index:
                continue
            row = market_rows.loc[code]
            desired = int(np.floor(equity * float(targets.get(code, 0.0)) / row["open"] / 100) * 100)
            current = self.broker.position_quantity(code)
            delta = desired - current
            if delta == 0:
                continue
            side = "BUY" if delta > 0 else "SELL"
            locked = self._locked_at_limit(row, side)
            fill = self.broker.submit(
                trade_date, code, side, abs(delta), float(row["open"]), float(row.get("adv_volume", row.get("volume", 0))),
                bool(row.get("suspended", False)), locked,
            )
            fills.append({"trade_date": trade_date, **fill.__dict__})

    @staticmethod
    def _locked_at_limit(row: pd.Series, side: str) -> bool:
        if bool(row.get("locked_limit", False)):
            return True
        open_price = float(row["open"])
        is_single_price = float(row.get("high", open_price)) == float(row.get("low", open_price)) == open_price
        if side == "BUY" and pd.notna(row.get("up_limit")):
            return is_single_price and open_price >= float(row["up_limit"])
        if side == "SELL" and pd.notna(row.get("down_limit")):
            return is_single_price and open_price <= float(row["down_limit"])
        return False
