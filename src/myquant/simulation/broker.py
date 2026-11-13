from __future__ import annotations

from dataclasses import dataclass, field

import math


@dataclass(frozen=True)
class CostModel:
    commission_bps: float = 3.0
    minimum_commission: float = 5.0
    sell_stamp_duty_bps: float = 5.0
    sh_transfer_bps: float = 0.01
    buy_slippage_bps: float = 5.0
    sell_slippage_bps: float = 5.0

    def fill_price(self, open_price: float, side: str) -> float:
        bps = self.buy_slippage_bps if side == "BUY" else -self.sell_slippage_bps
        return open_price * (1 + bps / 10_000)

    def fees(self, ts_code: str, side: str, quantity: int, price: float) -> float:
        notional = quantity * price
        commission = max(notional * self.commission_bps / 10_000, self.minimum_commission)
        stamp = notional * self.sell_stamp_duty_bps / 10_000 if side == "SELL" else 0.0
        transfer = notional * self.sh_transfer_bps / 10_000 if ts_code.endswith(".SH") else 0.0
        return commission + stamp + transfer


@dataclass(frozen=True)
class Fill:
    ts_code: str
    side: str
    requested_quantity: int
    filled_quantity: int
    price: float | None
    fees: float
    status: str
    reason: str | None = None


@dataclass
class Position:
    settled: int = 0
    unsettled_lots: list[tuple[str, int]] = field(default_factory=list)

    @property
    def quantity(self) -> int:
        return self.settled + sum(quantity for _, quantity in self.unsettled_lots)

    def settle(self, trade_date: str) -> None:
        newly_settled = [quantity for purchase_date, quantity in self.unsettled_lots if purchase_date < trade_date]
        self.settled += sum(newly_settled)
        self.unsettled_lots = [(d, q) for d, q in self.unsettled_lots if d >= trade_date]


class PaperBroker:
    """A deliberately conservative daily-open A-share paper broker."""

    def __init__(self, cash: float, costs: CostModel | None = None, max_adv_participation: float = 0.01) -> None:
        self.cash = cash
        self.costs = costs or CostModel()
        self.max_adv_participation = max_adv_participation
        self.positions: dict[str, Position] = {}

    def settle(self, trade_date: str) -> None:
        for position in self.positions.values():
            position.settle(trade_date)

    def position_quantity(self, ts_code: str) -> int:
        return self.positions.get(ts_code, Position()).quantity

    def submit(
        self,
        trade_date: str,
        ts_code: str,
        side: str,
        requested_quantity: int,
        open_price: float | None,
        adv_volume: float | None,
        suspended: bool = False,
        locked_limit: bool = False,
    ) -> Fill:
        if requested_quantity <= 0:
            return Fill(ts_code, side, requested_quantity, 0, None, 0.0, "REJECTED", "non_positive_quantity")
        if suspended or open_price is None or open_price <= 0:
            return Fill(ts_code, side, requested_quantity, 0, None, 0.0, "REJECTED", "suspended_or_missing_open")
        if locked_limit:
            return Fill(ts_code, side, requested_quantity, 0, None, 0.0, "REJECTED", "locked_limit")
        cap = int(math.floor((adv_volume or 0) * self.max_adv_participation / 100) * 100)
        quantity = min(requested_quantity, cap) if cap > 0 else 0
        if quantity == 0:
            return Fill(ts_code, side, requested_quantity, 0, None, 0.0, "REJECTED", "adv_participation_limit")
        position = self.positions.setdefault(ts_code, Position())
        if side == "SELL":
            quantity = min(quantity, position.settled)
            if quantity == 0:
                return Fill(ts_code, side, requested_quantity, 0, None, 0.0, "REJECTED", "t_plus_one_unsettled")
        price = self.costs.fill_price(open_price, side)
        fees = self.costs.fees(ts_code, side, quantity, price)
        if side == "BUY":
            affordable = int(math.floor((self.cash - self.costs.minimum_commission) / price / 100) * 100)
            quantity = min(quantity, max(0, affordable))
            fees = self.costs.fees(ts_code, side, quantity, price) if quantity else 0.0
            if quantity == 0:
                return Fill(ts_code, side, requested_quantity, 0, None, 0.0, "REJECTED", "insufficient_cash")
            self.cash -= quantity * price + fees
            position.unsettled_lots.append((trade_date, quantity))
        else:
            self.cash += quantity * price - fees
            position.settled -= quantity
        status = "FILLED" if quantity == requested_quantity else "PARTIALLY_FILLED"
        return Fill(ts_code, side, requested_quantity, quantity, price, fees, status)

    def market_value(self, closes: dict[str, float]) -> float:
        return sum(self.position_quantity(code) * closes.get(code, 0.0) for code in self.positions)

    def apply_cash_dividend(self, ts_code: str, cash_per_share: float) -> float:
        """Credit a declared per-share cash dividend to all shares held on the record date."""
        if cash_per_share < 0:
            raise ValueError("cash_per_share cannot be negative")
        amount = self.position_quantity(ts_code) * cash_per_share
        self.cash += amount
        return amount
