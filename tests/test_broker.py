from myquant.simulation.broker import PaperBroker


def test_t_plus_one_blocks_same_day_sale_then_settles_next_day() -> None:
    broker = PaperBroker(cash=1_000_000, max_adv_participation=1.0)
    bought = broker.submit("2024-01-02", "000001.SZ", "BUY", 1000, 10.0, 1_000_000)
    assert bought.filled_quantity == 1000
    rejected = broker.submit("2024-01-02", "000001.SZ", "SELL", 1000, 10.0, 1_000_000)
    assert rejected.status == "REJECTED"
    assert rejected.reason == "t_plus_one_unsettled"
    broker.settle("2024-01-03")
    sold = broker.submit("2024-01-03", "000001.SZ", "SELL", 1000, 10.0, 1_000_000)
    assert sold.filled_quantity == 1000


def test_locked_limit_rejects_order() -> None:
    broker = PaperBroker(cash=1_000_000, max_adv_participation=1.0)
    fill = broker.submit("2024-01-02", "000001.SZ", "BUY", 100, 10.0, 100_000, locked_limit=True)
    assert fill.status == "REJECTED"
    assert fill.reason == "locked_limit"


def test_cash_dividend_is_credited_to_held_shares() -> None:
    broker = PaperBroker(cash=1_000_000, max_adv_participation=1.0)
    broker.submit("2024-01-02", "000001.SZ", "BUY", 1000, 10.0, 1_000_000)
    credited = broker.apply_cash_dividend("000001.SZ", 0.2)
    assert credited == 200.0
