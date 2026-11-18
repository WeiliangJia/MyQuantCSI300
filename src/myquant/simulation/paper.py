"""Daily account journal and explicit execution costs for MQ300 research replay."""

from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_HALF_UP
import json
import math
import sqlite3

from myquant.simulation.broker import CostModel, Fill, PaperBroker, Position


def fen(amount):
    return float(Decimal(str(amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


FEE_FIELDS = ("broker_commission", "exchange_fee", "regulatory_fee", "transfer_fee", "stamp_duty")
COST_FIELDS = (*FEE_FIELDS, "half_spread", "impact", "fixed_cost", "dividend_tax")


@dataclass(frozen=True)
class ResearchCosts2024(CostModel):
    """CNY rates for the 2024 replay and an explicitly assumed broker tariff."""

    half_spread_bps: float = 2.0
    impact_bps: float = 3.0
    exchange_bps: float = 0.341
    regulatory_bps: float = 0.2
    transfer_bps: float = 0.1

    def fill_price(self, open_price, side):
        direction = 1 if side == "BUY" else -1
        return fen(open_price * (1 + direction * (self.half_spread_bps + self.impact_bps) / 10_000))

    def fee_detail(self, side, quantity, price):
        value = quantity * price
        commission = fen(max(value * self.commission_bps / 10_000, self.minimum_commission))
        exchange = fen(value * self.exchange_bps / 10_000)
        regulatory = fen(value * self.regulatory_bps / 10_000)
        return {
            "broker_commission": fen(commission - exchange - regulatory),
            "exchange_fee": exchange, "regulatory_fee": regulatory,
            "transfer_fee": fen(value * self.transfer_bps / 10_000),
            "stamp_duty": fen(value * self.sell_stamp_duty_bps / 10_000) if side == "SELL" else 0.0,
        }

    def fees(self, ts_code, side, quantity, price):
        return fen(sum(self.fee_detail(side, quantity, price).values()))

    def order_record(self, fill, open_price, forced_exit):
        costs = dict.fromkeys((*FEE_FIELDS, "half_spread", "impact"), 0.0)
        notional = 0.0
        if fill.filled_quantity:
            costs.update(self.fee_detail(fill.side, fill.filled_quantity, fill.price))
            notional = fen(fill.filled_quantity * fill.price)
            direction = 1 if fill.side == "BUY" else -1
            implementation_cost = fen(direction * (notional - fill.filled_quantity * open_price))
            model_bps = self.half_spread_bps + self.impact_bps
            if model_bps:
                costs["half_spread"] = fen(implementation_cost * self.half_spread_bps / model_bps)
            costs["impact"] = fen(implementation_cost - costs["half_spread"])
        return {**asdict(fill), **costs, "reference_open": open_price, "notional": notional,
                "forced_exit": forced_exit}


def restore_broker(cash, positions, costs):
    broker = PaperBroker(cash=cash, costs=costs, exchange_lots=True)
    broker.positions = {
        row["ts_code"]: Position(row["settled"], row["unsettled_lots"]) for row in positions
    }
    return broker


def book_session(day, quotes, member_codes, target, actions, previous, costs, initial_cash,
                 fixed_cost, dividend_tax_rate):
    prior = previous["account"] if previous else {"cash": initial_cash, "market_value": 0.0, "nav_cny": initial_cash}
    broker = restore_broker(prior["cash"], previous["positions"] if previous else [], costs)
    broker.settle(day)
    journals, orders = [], []
    receivables = list(previous["receivables"]) if previous else []
    for action in actions:
        code = action["ts_code"]
        shares = broker.position_quantity(code)
        if not shares:
            continue
        cash = fen(shares * action["cash"])
        added = math.floor(shares * action["shares"] + 1e-9)
        if cash:
            receivable = {"ts_code": code, "pay_date": action["pay_date"], "amount": cash,
                          "payment_evidence": action["payment_evidence"],
                          "payment_source_url": action.get("payment_source_url", "")}
            receivables.append(receivable)
            journals.append({**receivable, "event": "dividend_receivable", "cash_delta": 0.0, "shares": shares})
        if added:
            broker.positions[code].settled += added
            journals.append({"ts_code": code, "event": "stock_distribution", "shares": added, "cash_delta": 0.0})
    tax = 0.0
    paid = 0.0
    unpaid = []
    for receivable in receivables:
        if receivable["pay_date"] > day:
            unpaid.append(receivable)
            continue
        deduction = fen(receivable["amount"] * dividend_tax_rate)
        paid += receivable["amount"]
        tax += deduction
        broker.cash += receivable["amount"] - deduction
        journals.append({**receivable, "event": "dividend_paid", "dividend_tax": deduction,
                         "cash_delta": fen(receivable["amount"] - deduction)})
    broker.cash -= fixed_cost
    if broker.cash < -0.005:
        raise ValueError(f"{day}: available cash cannot cover fixed costs")
    holdings = {code for code, position in broker.positions.items() if position.quantity}
    forced = holdings - member_codes
    for code in holdings:
        if code not in quotes.index or not math.isfinite(quotes.at[code, "close"]) or quotes.at[code, "close"] <= 0:
            raise ValueError(f"{day}: missing closing valuation for {code}")
    if target is not None or forced:
        open_equity = broker.cash + sum(item["amount"] for item in unpaid)
        for code in holdings:
            opening = quotes.at[code, "open"]
            if not math.isfinite(opening) or opening <= 0:
                raise ValueError(f"{day}: missing opening valuation for {code}")
            open_equity += opening * broker.position_quantity(code)
        proposed = []
        for code in sorted((holdings | set(target)) if target is not None else forced):
            current = broker.position_quantity(code)
            weight = 0.0 if code in forced else target.get(code, 0.0)
            if weight > 0 and code not in member_codes:
                continue
            if code not in quotes.index:
                raise ValueError(f"{day}: missing market evidence for target {code}")
            row = quotes.loc[code]
            opening = float(row.open)
            if not math.isfinite(opening) or opening <= 0:
                raise ValueError(f"{day}: missing reference opening price for {code}")
            desired = int(open_equity * weight / opening)
            difference = desired - current
            side = "BUY" if difference > 0 else "SELL"
            quantity = abs(difference) if desired == 0 else broker.lot_quantity(code, abs(difference))
            if quantity:
                proposed.append((side, code, quantity, row))
        for side, code, quantity, row in sorted(proposed, key=lambda order: order[0] == "BUY"):
            opening = float(row.open)
            if side == "BUY" and not row.suspended and row.isST != 0:
                fill = Fill(code, side, quantity, 0, None, 0, "REJECTED", "st_or_missing_status")
            else:
                band = None
                locked = False
                if not row.suspended:
                    if not (math.isfinite(row.down_limit) and math.isfinite(row.up_limit)):
                        raise ValueError(f"{day}: missing price limits for {code}")
                    band = (float(row.down_limit), float(row.up_limit))
                    locked = opening >= band[1] if side == "BUY" else opening <= band[0]
                fill = broker.submit(day, code, side, quantity, opening, row.adv_volume,
                                     bool(row.suspended), locked, price_band=band)
            orders.append(costs.order_record(fill, opening, code in forced))
    positions = []
    for code, position in sorted(broker.positions.items()):
        if position.quantity:
            close = float(quotes.at[code, "close"])
            if not math.isfinite(close) or close <= 0:
                raise ValueError(f"{day}: invalid closing valuation for {code}")
            positions.append({"ts_code": code, "quantity": position.quantity, "close": close,
                              "market_value": fen(position.quantity * close), **asdict(position)})

    costs_today = {name: fen(sum(order.get(name, 0) for order in orders)) for name in COST_FIELDS}
    costs_today.update(fixed_cost=fixed_cost, dividend_tax=fen(tax))
    buys = fen(sum(order["notional"] for order in orders if order["side"] == "BUY"))
    sells = fen(sum(order["notional"] for order in orders if order["side"] == "SELL"))
    fees = fen(sum(order["fees"] for order in orders))
    cash_error = fen(broker.cash - (prior["cash"] + sells - buys - fees + paid - tax - fixed_cost))
    market_value = fen(sum(row["market_value"] for row in positions))
    receivable_value = fen(sum(item["amount"] for item in unpaid))
    total_cost = fen(sum(costs_today.values()))
    if cash_error or fen(sum(costs_today[field] for field in FEE_FIELDS) - fees):
        raise ValueError(f"{day}: account cash or fee reconciliation failed")
    nav = fen(broker.cash + market_value + receivable_value)
    if nav <= 0 or fen(broker.cash) < 0:
        raise ValueError(f"{day}: invalid account cash or NAV")
    return {
        "trade_date": day,
        "account": {"cash": fen(broker.cash), "market_value": market_value,
                    "receivable": receivable_value, "nav_cny": nav, "previous_nav_cny": prior["nav_cny"],
                    "cash_error": cash_error, "daily_pnl_cny": fen(nav - prior["nav_cny"]),
                    "buy_notional": buys, "sell_notional": sells,
                    "dividend_received": fen(paid), "fees": fees, "total_cost": total_cost, **costs_today},
        "positions": positions, "orders": orders, "actions": journals, "receivables": unpaid,
    }


def save_sessions(database, fingerprint, days, step):
    """Commit each session once, keeping orders and the resume state in the same transaction."""
    with sqlite3.connect(database, isolation_level=None, timeout=30) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS run (id INTEGER PRIMARY KEY CHECK(id=1), fingerprint TEXT)")
        connection.execute("CREATE TABLE IF NOT EXISTS session (trade_date TEXT PRIMARY KEY, payload TEXT)")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute("INSERT OR IGNORE INTO run VALUES (1, ?)", (fingerprint,))
            if connection.execute("SELECT fingerprint FROM run WHERE id=1").fetchone()[0] != fingerprint:
                raise ValueError("Inputs or settings changed; choose a separate replay output directory")
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

        for day in days:
            connection.execute("BEGIN IMMEDIATE")
            try:
                if not connection.execute("SELECT 1 FROM session WHERE trade_date=?", (day,)).fetchone():
                    row = connection.execute("SELECT payload FROM session ORDER BY trade_date DESC LIMIT 1").fetchone()
                    previous = json.loads(row[0]) if row else None
                    if previous and previous["trade_date"] >= day:
                        raise ValueError("Cannot backdate a committed account")
                    record = step(day, previous)
                    connection.execute("INSERT INTO session VALUES (?, ?)", (day, json.dumps(record, allow_nan=False)))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        return [json.loads(row[0]) for row in connection.execute("SELECT payload FROM session ORDER BY trade_date")]
