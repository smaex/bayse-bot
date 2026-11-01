"""Stop-loss and take-profit gaps (audit of 2026-11-01).

Written before the fixes. Each test names the gap it guards.
"""

import inspect

import pytest

import bot
import config
import database


# Gap 1: the trailing peak must survive a restart --------------------------

def test_restored_peak_comes_from_the_trade_row():
    assert bot._restored_peak({"peak_price": 0.72, "entry_price": 0.50}) == 0.72


def test_restored_peak_falls_back_to_entry_for_legacy_rows():
    # Rows written before the column existed have no peak; entry is the old behaviour.
    assert bot._restored_peak({"peak_price": None, "entry_price": 0.50}) == 0.50
    assert bot._restored_peak({"entry_price": 0.50}) == 0.50


def test_restored_peak_never_sits_below_entry():
    assert bot._restored_peak({"peak_price": 0.40, "entry_price": 0.50}) == 0.50


def test_peak_is_persisted_only_when_it_moves_materially():
    # Egress: one write per cycle for every open position would be a pile-up.
    assert bot._peak_persist_due(saved=None, peak=0.50) is True
    assert bot._peak_persist_due(saved=0.50, peak=0.505) is False
    assert bot._peak_persist_due(saved=0.50, peak=0.51) is True


def test_update_trade_peak_only_ever_raises_the_stored_peak(monkeypatch):
    captured = {}
    monkeypatch.setattr(database, "_execute", lambda sql, params: captured.update(sql=sql, params=params))
    database.update_trade_peak("t-1", 0.66)
    assert "GREATEST" in captured["sql"]
    assert "resolved_at IS NULL" in captured["sql"]
    assert captured["params"] == (0.66, "t-1")


def test_trades_table_gets_a_peak_column_by_migration():
    src = inspect.getsource(database)
    assert "ALTER TABLE trades ADD COLUMN IF NOT EXISTS peak_price REAL" in src


# Gap 2: no exit decision on a substituted price ---------------------------

def test_a_missing_book_falls_back_to_the_quoted_price_not_entry():
    # A book-fetch timeout must not switch the stops off. The quoted side
    # price keeps them alive; the sell quote still checks the real proceeds.
    market = {"engine": "CLOB", "yes_price": 0.80, "no_price": 0.20}
    assert bot._exit_bid(None, market, "YES") == pytest.approx(0.80)
    assert bot._exit_bid({"bids": [], "asks": []}, market, "YES") == pytest.approx(0.80)


def test_clob_with_a_usable_book_uses_the_best_bid():
    book = {"bids": [{"price": 0.61, "size": 10}], "asks": [{"price": 0.63, "size": 10}]}
    assert bot._exit_bid(book, {"yes_price": 0.80}, "YES") == pytest.approx(0.61)


def test_amm_without_a_book_uses_the_quoted_side_price():
    # AMM markets have no book; the quoted price is the only price there is.
    market = {"engine": "AMM", "yes_price": 0.42, "no_price": 0.58}
    assert bot._exit_bid(None, market, "NO") == pytest.approx(0.58)


def test_no_market_and_no_book_means_no_bid_not_entry_price():
    assert bot._exit_bid(None, None, "YES") is None


# Gap 3: a stop the exchange cannot take is surfaced once, not silently ---

def test_sub_minimum_notice_fires_once_per_position():
    pos = {}
    assert bot._sub_minimum_notice_due(pos) is True
    assert bot._sub_minimum_notice_due(pos) is False


# Gap 4: dead config --------------------------------------------------------

def test_unused_fixed_take_profit_target_is_removed():
    assert not hasattr(config, "TAKE_PROFIT_PRICE_TARGET")


# Gap 5: a stop that cannot fill backs off instead of hammering the book ---

def test_failed_stop_retries_back_off_and_are_capped():
    assert bot._exit_retry_delay(0) == 0.0
    first = bot._exit_retry_delay(1)
    second = bot._exit_retry_delay(2)
    assert first > 0
    assert second > first
    assert bot._exit_retry_delay(50) <= 300.0


def test_stop_retry_gate_blocks_until_the_backoff_passes():
    pos = {"exit_retry_at": 1_000.0}
    assert bot._stop_retry_blocked(pos, now=999.0) is True
    assert bot._stop_retry_blocked(pos, now=1_000.0) is False
    assert bot._stop_retry_blocked({}, now=0.0) is False


# Wiring: the exit loop persists the peak it has seen -------------------------

def test_exit_loop_persists_the_peak_for_a_tracked_trade(monkeypatch):
    import asyncio
    import time

    from risk import RiskManager
    from test_strategy_execution_and_exits import MockClient

    risk = RiskManager()
    risk.add_position("market-1", {
        "market_id": "market-1", "event_id": "event-1", "trade_id": "trade-42",
        "outcome_id": "yes-1", "outcome": "YES", "entry_price": 0.55,
        "amount_ngn": 500, "filled_quantity": 9.09, "confirmed_filled": True,
        "strategy": "TAKER", "asset": "SOL", "timeframe": "15min",
    })
    monkeypatch.setattr(bot, "active_markets", [{
        "market_id": "market-1", "threshold": 100.0, "secs_to_close": 300,
        "yes_price": 0.85, "no_price": 0.15, "minimum_order_amount": 100,
        "fee_rate": 0.02,
    }])
    monkeypatch.setattr(bot.feeds_direct, "get_direct_price", lambda _a: (100.02, time.time()))
    monkeypatch.setattr(bot, "_tg_app", None)

    persisted = []
    monkeypatch.setattr(bot.database, "update_trade_peak",
                        lambda trade_id, peak: persisted.append((trade_id, peak)))

    client = MockClient(order_response={"status": "open", "filledSize": 10})
    asyncio.run(bot._evaluate_and_exit_positions("chat-1", client, risk, {}))

    assert persisted, "the peak seen this cycle must reach the ledger"
    assert persisted[0][0] == "trade-42"
    assert persisted[0][1] == pytest.approx(0.85)
