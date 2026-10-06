"""Completing a MAKER pair, and why the taker kept getting suppressed.

Three production complaints live in this file:

1. **Both legs of a MAKER quote almost never filled together, and the system
   cancelled the trade.** Two defects did that, and they compounded.

   * The surviving leg was treated as an ordinary standing quote: it was
     withdrawn on ``MAKER_ORDER_TIMEOUT`` (60s), on a 0.10% oracle move, and by
     ``CANCEL_RESTING`` late in the candle -- even though the sibling leg had
     already filled and this order was the one that would lock the set.
   * Once the quote was gone, the market was blocked *forever*: the executor's
     ``maker_quote_already_resting`` guard and ``risk.already_in`` both counted
     the already-*filled* leg as "a quote already resting", so no new MAKER
     quote could ever be sent to finish the set. The inventory skew meant to
     push the next quote at the opposite side was unreachable -- and was also
     erased every time a quote was withdrawn, because it lived inside the quote
     record.

   The result was the account bleed in the report: a filled leg left naked, no
   completing order, and an exit policy that eventually stopped it out.

2. **TAKER had not fired.** A *resting* (unfilled) maker leg on one side of a
   market blocked a taker entry on the other side of that same market, and a
   duplicate MAKER signal -- one that could only be skipped by the executor --
   was allowed to out-rank a real taker signal in the collision resolver. A
   quote is an order, not a position: it cannot be on the other side of
   anything, so it must not suppress an entry.

3. **No notification when a trade was being executed.** Only fills were
   announced. The executor now announces the attempt before the order is sent.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

import bot
import config
import executor
import strategies
from risk import RiskManager
from strategies.base import QuoteLeg, TradeSignal
from strategies.maker import MakerStrategy


CHAT = "chat-pair-completion"
MARKET_ID = "market-pair"

# One spot/threshold pair with a wide, stable fair-value split:
# P(YES) ~= 0.417, P(NO) ~= 0.583 at 300s to close.
SPOT = 99_900.0
THRESHOLD = 100_000.0


def _book(bid: float, ask: float, quantity: float = 5_000.0) -> dict:
    return {
        "timestamp": time.time(),
        "bids": [{"price": bid, "quantity": quantity}],
        "asks": [{"price": ask, "quantity": quantity}],
    }


def _market(**overrides) -> dict:
    market = {
        "market_id": MARKET_ID,
        "event_id": "event-pair",
        "asset": "BTC",
        "timeframe": "15min",
        "threshold": THRESHOLD,
        "secs_to_close": 300,
        "yes_id": "yes",
        "no_id": "no",
        "yes_price": 0.42,
        "no_price": 0.58,
        "engine": "CLOB",
        "fee_rate": 0.02,
        "minimum_order_amount": 100.0,
        "closing_date": "2026-10-06T12:15:00Z",
        "status": "open",
    }
    market.update(overrides)
    return market


def _position(key: str, **overrides) -> dict:
    pos = {
        "market_id": MARKET_ID,
        "event_id": "event-pair",
        "outcome_id": "no",
        "order_id": key,
        "outcome": "NO",
        "entry_price": 0.50,
        "amount_ngn": 500.0,
        "filled_quantity": 0.0,
        "confirmed_filled": False,
        "strategy": "MAKER",
        "asset": "BTC",
        "timeframe": "15min",
        "threshold": THRESHOLD,
        "placed_at": time.time() - 600,
    }
    pos.update(overrides)
    return pos


def _fresh_oracle(monkeypatch):
    monkeypatch.setattr(
        bot.feeds_direct, "get_direct_price", lambda _asset: (SPOT, time.time())
    )
    monkeypatch.setattr(bot.feeds, "spot", {"BTC": SPOT})
    monkeypatch.setattr(bot.feeds, "spot_updated_at", {"BTC": time.time()})


@pytest.fixture(autouse=True)
def _clean_maker_state():
    from strategies.maker import maker_strategy

    maker_strategy.open_quotes.clear()
    # getattr: these tests are run against the pre-fix code as well, to prove
    # they are regressions rather than restatements of the new behaviour.
    inventory = getattr(maker_strategy, "inventory", {})
    inventory.clear()
    yield
    maker_strategy.open_quotes.clear()
    inventory.clear()


class _RestingClient:
    """A fake exchange where placed orders rest until they are cancelled."""

    def __init__(self, *, fills: dict[str, float] | None = None, books=None):
        self.cancelled: list[str] = []
        self.cancelled_ids: set[str] = set()
        self.fills = fills or {}
        self.placed: list[dict] = []
        self.books = books or {}

    async def get_orderbook(self, outcome_id, depth=5):
        return self.books.get(outcome_id, {})

    async def get_order(self, order_id):
        if order_id in self.cancelled_ids:
            return {"status": "cancelled", "filledSize": 0}
        return {
            "status": "filled" if self.fills.get(order_id) else "open",
            "filledSize": self.fills.get(order_id, 0.0),
            "quantity": self.fills.get(order_id, 0.0),
        }

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)
        self.cancelled_ids.add(order_id)
        return {"status": "cancelled"}

    @staticmethod
    def parse_filled_shares(order):
        return float((order or {}).get("filledSize")
                     or (order or {}).get("quantity") or 0.0)


def _maker_env(monkeypatch, *, market=None):
    market = market or _market()
    monkeypatch.setattr(bot, "active_markets", [market])
    monkeypatch.setattr(executor, "active_markets", [market])
    monkeypatch.setattr(bot, "_tg_app", None)
    monkeypatch.setattr(executor, "_tg_app", None)
    monkeypatch.setattr(executor, "_trade_cooldown", {})
    monkeypatch.setattr(executor.database, "get_alpha_trend", lambda *_a: 1.0)
    monkeypatch.setattr(executor.database, "record_trade", lambda **kw: "trade-1")
    monkeypatch.setattr(executor.database, "update_trade_fill", lambda *a, **k: None)
    monkeypatch.setattr(executor.database, "resolve_trade", lambda *a, **k: None)
    monkeypatch.setattr(bot.database, "resolve_trade", lambda *a, **k: None)
    monkeypatch.setattr(bot.database, "update_trade_fill", lambda *a, **k: None)
    return RiskManager()


# ── 1. A half-filled pair keeps its completing leg resting ───────────────────

def test_a_completion_leg_survives_the_standing_quote_timeout(monkeypatch):
    """The oracle moved and the quote is old, but the sibling has filled.

    Both triggers would withdraw an ordinary quote -- and did, which is how a
    pair that was one fill from locking became a naked directional position.
    """
    _fresh_oracle(monkeypatch)
    risk = _maker_env(monkeypatch)
    risk.add_position(
        "market-pair:YES:filled", _position(
            "market-pair:YES:filled", outcome="YES", entry_price=0.36,
            filled_quantity=1_000.0, confirmed_filled=True, outcome_id="yes",
        ),
    )
    risk.add_position(
        "market-pair:NO:resting", _position(
            "market-pair:NO:resting", outcome="NO", entry_price=0.44,
        ),
    )
    client = _RestingClient()

    asyncio.run(bot._manage_unfilled_maker_orders(CHAT, client, risk, {}))

    assert client.cancelled == [], (
        "a fill here completes a locked set; timing it out is what broke the pair"
    )
    assert "market-pair:NO:resting" in risk.open_positions


def test_a_buried_completion_bid_is_requoted_instead_of_left_resting(monkeypatch):
    """Worth owning is not the same as able to fill.

    A bid six ticks under the book will sit until the candle ends while the
    filled sibling stays naked. Withdraw it so the next pass re-quotes the
    completing side at the skewed price, which is where the maker's willingness
    to pay up for the missing side actually lives.
    """
    _fresh_oracle(monkeypatch)
    risk = _maker_env(monkeypatch)
    risk.add_position("market-pair:YES:filled", _position(
        "market-pair:YES:filled", outcome="YES", entry_price=0.36,
        filled_quantity=1_000.0, confirmed_filled=True, outcome_id="yes",
    ))
    risk.add_position("market-pair:NO:resting", _position(
        "market-pair:NO:resting", outcome="NO", entry_price=0.44,
    ))
    client = _RestingClient(books={"no": _book(0.50, 0.58)})

    asyncio.run(bot._manage_unfilled_maker_orders(CHAT, client, risk, {}))

    assert client.cancelled, "a completion bid the book has left behind cannot fill"
    assert "market-pair:NO:resting" not in risk.open_positions


def test_a_competitive_completion_bid_keeps_resting(monkeypatch):
    """The requote must not become churn: one tick under the book is fine."""
    _fresh_oracle(monkeypatch)
    risk = _maker_env(monkeypatch)
    risk.add_position("market-pair:YES:filled", _position(
        "market-pair:YES:filled", outcome="YES", entry_price=0.36,
        filled_quantity=1_000.0, confirmed_filled=True, outcome_id="yes",
    ))
    risk.add_position("market-pair:NO:resting", _position(
        "market-pair:NO:resting", outcome="NO", entry_price=0.44,
    ))
    client = _RestingClient(books={"no": _book(0.45, 0.58)})

    asyncio.run(bot._manage_unfilled_maker_orders(CHAT, client, risk, {}))

    assert client.cancelled == []
    assert "market-pair:NO:resting" in risk.open_positions


def test_a_completion_leg_is_withdrawn_when_the_pair_stops_locking(monkeypatch):
    """If the sibling filled high enough that bid + fill >= 1, there is no set."""
    _fresh_oracle(monkeypatch)
    risk = _maker_env(monkeypatch)
    # 0.44 + 0.60 = 1.04: completing this pair would buy a locked loss.
    risk.add_position(
        "market-pair:YES:filled", _position(
            "market-pair:YES:filled", outcome="YES", entry_price=0.60,
            filled_quantity=1_000.0, confirmed_filled=True, outcome_id="yes",
        ),
    )
    risk.add_position(
        "market-pair:NO:resting", _position(
            "market-pair:NO:resting", outcome="NO", entry_price=0.44,
        ),
    )
    client = _RestingClient()

    asyncio.run(bot._manage_unfilled_maker_orders(CHAT, client, risk, {}))

    assert client.cancelled, "a pair that no longer locks must not keep bidding"
    assert "market-pair:NO:resting" not in risk.open_positions


def test_a_completion_leg_is_withdrawn_when_the_bid_is_no_longer_worth_it(monkeypatch):
    """A stale bid above the fresh fair value is not a completion, it is a loss."""
    _fresh_oracle(monkeypatch)
    risk = _maker_env(monkeypatch)
    risk.add_position(
        "market-pair:YES:filled", _position(
            "market-pair:YES:filled", outcome="YES", entry_price=0.24,
            filled_quantity=1_000.0, confirmed_filled=True, outcome_id="yes",
        ),
    )
    # Fair value of NO is ~0.583 and the required edge there is ~0.026, so a
    # 0.72 bid to complete the set is above what the side is worth.
    risk.add_position(
        "market-pair:NO:resting", _position(
            "market-pair:NO:resting", outcome="NO", entry_price=0.72,
        ),
    )
    client = _RestingClient()

    asyncio.run(bot._manage_unfilled_maker_orders(CHAT, client, risk, {}))

    assert client.cancelled, "the value test must still apply to a completion leg"
    assert "market-pair:NO:resting" not in risk.open_positions


def test_the_completion_leg_verdict_reads_the_pair_and_the_model(monkeypatch):
    _fresh_oracle(monkeypatch)
    monkeypatch.setattr(bot, "active_markets", [_market()])
    pos = _position("no", outcome="NO", entry_price=0.44)
    sibling = _position("yes", outcome="YES", entry_price=0.36,
                        confirmed_filled=True, filled_quantity=1_000.0)

    keep, detail = bot._completion_leg_verdict(pos, sibling, _market())
    assert keep, detail
    assert "still +" in detail

    # Sibling filled one tick too high: 0.44 + 0.56 = 1.00 costs the whole edge.
    keep, detail = bot._completion_leg_verdict(
        pos, dict(sibling, entry_price=0.56), _market()
    )
    assert not keep and "no longer locks" in detail


def test_the_completion_verdict_uses_the_same_skew_that_priced_the_quote(monkeypatch):
    """A completing leg is allowed a thinner edge than a fresh one.

    Re-judging it without the skew would cancel exactly the order the skew
    exists to keep -- so the verdict has to read the same rule the quote was
    priced by.
    """
    _fresh_oracle(monkeypatch)
    monkeypatch.setattr(bot, "active_markets", [_market()])
    # 0.570 is a ~+0.013 edge under a 0.583 fair value: below the ~0.028 the
    # normal requirement asks for, but above the floor once a two-tick
    # completion skew is applied.
    pos = _position("no", outcome="NO", entry_price=0.570)

    big_sibling = _position("yes", outcome="YES", entry_price=0.36,
                            confirmed_filled=True, filled_quantity=1_000.0)
    keep, detail = bot._completion_leg_verdict(pos, big_sibling, _market())
    assert keep, detail

    # With almost nothing held there is no completion skew to spend.
    tiny_sibling = dict(big_sibling, filled_quantity=1.0)
    keep, detail = bot._completion_leg_verdict(pos, tiny_sibling, _market())
    assert not keep and "edge=" in detail


# ── 2. A half-filled pair is never blocked from being completed ──────────────

class _MakerClient(_RestingClient):
    """A CLOB whose book prices the pair, and whose orders rest unfilled."""

    def __init__(self, books=None, **kwargs):
        super().__init__(**kwargs)
        self.books = books or {"yes": _book(0.34, 0.42), "no": _book(0.50, 0.58)}

    async def get_orderbooks(self, outcome_ids, depth=5):
        return {oid: self.books.get(oid, {}) for oid in outcome_ids}

    async def get_orderbook(self, outcome_id, depth=5):
        return self.books.get(outcome_id, {})

    async def place_order(self, **kwargs):
        self.placed.append(kwargs)
        return {"order": {"id": f"order-{len(self.placed)}", "status": "open",
                          "quantity": 0.0, "filledSize": 0.0}}


def _completion_signal(**overrides) -> TradeSignal:
    base = dict(
        strategy="MAKER", event_id="event-pair", market_id=MARKET_ID,
        asset="BTC", timeframe="15min", outcome="BOTH", outcome_id="yes",
        certainty=0.6, mode_floor=0.0, win_prob=0.58, market_price=0.51,
        size_pct=0.02, reason="completion quote",
        legs=[QuoteLeg("YES", "yes", 0.35, 0.02, 0.417),
              QuoteLeg("NO", "no", 0.51, 0.02, 0.583)],
    )
    base.update(overrides)
    return TradeSignal(**base)


def _run_logic(chat, sig, client, risk):
    asyncio.run(executor._execute_logic(
        chat, sig, client, risk,
        {"mode": "balanced", "risk_pct": 2.0, "mintrade": 100.0,
         "maxtrade": 5_000.0, "maxexposure": 20.0},
        20_000.0, 20_000.0,
    ))


def test_a_maker_pair_may_be_requoted_around_a_filled_leg(monkeypatch):
    """The executor must send the quote that completes the set."""
    risk = _maker_env(monkeypatch)
    risk.add_position("market-pair:NO:filled", _position(
        "market-pair:NO:filled", outcome="NO", filled_quantity=1_000.0,
        confirmed_filled=True,
    ))
    client = _MakerClient()

    _run_logic(CHAT, _completion_signal(), client, risk)

    assert [call["outcome_id"] for call in client.placed] == ["yes", "no"], (
        "a filled leg is a position, not a resting quote: it cannot block the "
        "quote that completes the set, and the quote must contain the side "
        "that is still open"
    )


def test_the_live_path_can_send_the_completing_quote(monkeypatch):
    """End to end through `execute_trade`, `already_in` included.

    `_execute_logic` alone is not the live path: `execute_trade` runs the risk
    book's `already_in` first, and that gate used to refuse *any* second MAKER
    entry on a market where a maker leg was tracked -- filled or not. So the
    quote that completes a half-filled pair was refused before the executor's
    own completion check ever ran.
    """
    monkeypatch.setattr(executor.config, "LIVE_TRADING", True)
    risk = _maker_env(monkeypatch)
    risk.add_position("market-pair:YES:filled", _position(
        "market-pair:YES:filled", outcome="YES", outcome_id="yes",
        filled_quantity=1_000.0, confirmed_filled=True,
    ))
    client = _MakerClient()

    asyncio.run(executor.execute_trade(
        CHAT, _completion_signal(), client, risk,
        {"mode": "balanced", "risk_pct": 2.0, "mintrade": 100.0,
         "maxtrade": 5_000.0, "maxexposure": 20.0},
        20_000.0, 20_000.0,
    ))

    assert [call["outcome_id"] for call in client.placed] == ["yes", "no"], (
        "already_in refused the only quote that could complete the set"
    )


def test_a_second_maker_market_on_the_same_asset_is_still_blocked(monkeypatch):
    """The completion exemption is scoped to the market being completed."""
    monkeypatch.setattr(executor.config, "LIVE_TRADING", True)
    risk = _maker_env(monkeypatch)
    risk.add_position("other-btc-market:YES:filled", _position(
        "other-btc-market:YES:filled", market_id="other-btc-market",
        outcome="YES", outcome_id="yes",
        filled_quantity=1_000.0, confirmed_filled=True,
    ))

    assert risk.already_in(
        MARKET_ID, asset="BTC", strategy="MAKER", outcome="BOTH"
    ) is True


def test_a_new_quote_will_not_simply_re_bid_a_side_we_already_hold(monkeypatch):
    """The completion allowance is not a licence to stack the same side."""
    risk = _maker_env(monkeypatch)
    risk.add_position("market-pair:YES:filled", _position(
        "market-pair:YES:filled", outcome="YES", outcome_id="yes",
        filled_quantity=1_000.0, confirmed_filled=True,
    ))
    client = _MakerClient()
    sig = _completion_signal(
        outcome="YES", outcome_id="yes",
        legs=[QuoteLeg("YES", "yes", 0.35, 0.02, 0.417)],
    )

    _run_logic(CHAT, sig, client, risk)

    assert client.placed == [], (
        "a new quote that only re-bids the side we already hold is adding "
        "directional risk, not completing a set"
    )


def test_a_resting_quote_does_not_block_a_taker_on_the_other_side():
    """A quote is an order, not a position. Only a fill can oppose an entry."""
    risk = RiskManager()
    risk.add_position("market-pair:YES:resting", _position(
        "market-pair:YES:resting", outcome="YES", outcome_id="yes",
        confirmed_filled=False,
    ))
    risk.add_position("market-pair:NO:resting", _position(
        "market-pair:NO:resting", outcome="NO", confirmed_filled=False,
    ))

    assert risk.already_in(
        MARKET_ID, asset="BTC", strategy="TAKER", outcome="NO"
    ) is False, (
        "a two-sided maker quote always has a leg on the 'other' side; while "
        "that suppressed the taker, it could not fire on the markets MAKER "
        "was quoting"
    )
    # And once one of those legs fills, the opposite-side rule is real again:
    # a filled YES position cannot be hedged by a taker buying NO, because
    # exactly one of the two pays out. The same side is still fine.
    risk.open_positions["market-pair:YES:resting"]["confirmed_filled"] = True
    risk.open_positions["market-pair:YES:resting"]["filled_quantity"] = 10.0
    assert risk.already_in(
        MARKET_ID, asset="BTC", strategy="TAKER", outcome="NO"
    ) is True
    assert risk.already_in(
        MARKET_ID, asset="BTC", strategy="TAKER", outcome="YES"
    ) is False


# ── 3. Inventory survives the quote it came from ─────────────────────────────

def test_inventory_skew_survives_the_quote_being_withdrawn():
    """Cancelling a quote must not delete the memory of its filled sibling."""
    maker = MakerStrategy()
    maker.record_fill("market-pair", "YES", 40.0)
    maker.drop("market-pair")

    assert maker.inventory_skew("market-pair") == pytest.approx(40.0), (
        "the next quote has to know which side to complete"
    )

    # And the live loop's risk book overrides it, so the skew is correct after
    # a restart even with an empty fill ledger.
    fresh = MakerStrategy()
    positions = {
        "m:YES:o1": {"market_id": "m", "strategy": "MAKER", "outcome": "YES",
                     "filled_quantity": 30.0, "confirmed_filled": True},
        "m:NO:o2": {"market_id": "m", "strategy": "MAKER", "outcome": "NO",
                    "filled_quantity": 12.0, "confirmed_filled": True},
        "m:NO:resting": {"market_id": "m", "strategy": "MAKER", "outcome": "NO",
                         "filled_quantity": 0.0, "confirmed_filled": False},
    }
    assert fresh.inventory_skew("m", positions) == pytest.approx(18.0)


# ── 4. The late-candle cancel leaves a completion leg alone ──────────────────

def test_exit_policy_does_not_cancel_a_completion_leg():
    common = dict(
        outcome="NO", w_est=0.583, bid=0.44, peak_price=0.44, entry_price=0.44,
        secs=100.0, fee_rate=0.02, is_maker_pos=True, confirmed_filled=False,
    )
    ordinary = bot._exit_decision(**common)
    assert ordinary is not None and ordinary["reason"] == "CANCEL_RESTING"

    completion = bot._exit_decision(**common, completion_leg=True)
    assert completion is None, (
        "cancelling the completing leg leaves the filled sibling unhedged"
    )


def test_the_exit_loop_will_not_cancel_a_completing_order(monkeypatch):
    _fresh_oracle(monkeypatch)
    market = _market(secs_to_close=100)
    risk = _maker_env(monkeypatch, market=market)
    risk.add_position("market-pair:YES:filled", _position(
        "market-pair:YES:filled", outcome="YES", outcome_id="yes",
        entry_price=0.36, filled_quantity=1_000.0, confirmed_filled=True,
        awaiting_settlement=True,
    ))
    risk.add_position("market-pair:NO:resting", _position(
        "market-pair:NO:resting", outcome="NO", outcome_id="no", entry_price=0.44,
    ))
    client = _RestingClient()

    async def _books(outcome_ids, depth=5):
        return {}

    async def _no_position(outcome_id):
        return None

    client.get_orderbooks = _books
    client.get_position = _no_position

    asyncio.run(bot._evaluate_and_exit_positions(CHAT, client, risk, {}))

    assert client.cancelled == [], (
        "the last three minutes are exactly when a completing fill is best: "
        "it locks the set whatever the market settles at"
    )


# ── 5. An execution is announced when it is sent, not only when it fills ─────

class _FakeBot:
    def __init__(self):
        self.messages: list[str] = []

    async def send_message(self, chat_id, text, **kwargs):
        self.messages.append(text)


class _TakerClient(_MakerClient):
    """Accepts one taker order and reports the fill the test asks for."""

    def __init__(self, *, fills=None, books=None):
        super().__init__(books)
        self.fills = fills

    async def place_order(self, **kwargs):
        self.placed.append(kwargs)
        index = len(self.placed) - 1
        shares = float(self.fills[index]) if self.fills is not None else 10.0
        ask = kwargs.get("price") or 0.55
        if shares <= 0:
            return {"order": {"id": f"order-{len(self.placed)}",
                              "status": "cancelled", "quantity": 0.0}}
        return {"order": {"id": f"order-{len(self.placed)}", "status": "filled",
                          "quantity": shares, "avgFillPrice": ask,
                          "totalCost": kwargs["amount"]}}


def _taker_env(monkeypatch):
    market = _market()
    app = SimpleNamespace(bot=_FakeBot())
    monkeypatch.setattr(executor, "active_markets", [market])
    monkeypatch.setattr(executor, "_trade_cooldown", {})
    monkeypatch.setattr(executor, "_tg_app", app)
    monkeypatch.setattr(executor.database, "get_alpha_trend", lambda *_a: 1.0)
    monkeypatch.setattr(executor.database, "record_trade", lambda **kw: "trade-1")
    monkeypatch.setattr(executor.database, "resolve_trade", lambda *a, **k: None)
    return app


def _taker_signal() -> TradeSignal:
    return TradeSignal(
        strategy="TAKER", event_id="event-pair", market_id=MARKET_ID,
        asset="BTC", timeframe="15min", outcome="YES", outcome_id="yes",
        certainty=0.56, mode_floor=0.0, win_prob=0.75, market_price=0.54,
        size_pct=0.02, reason="TAKER directional test",
        min_net_ev=config.TAKER_MIN_NET_EV_DEFAULT,
    )


def test_a_taker_entry_announces_execution_before_the_fill(monkeypatch):
    app = _taker_env(monkeypatch)
    client = _TakerClient(fills=[10.0], books={"yes": _book(0.53, 0.55)})

    _run_logic(CHAT, _taker_signal(), client, RiskManager())

    assert client.placed, "the taker order was never sent"
    assert "Order being executed" in app.bot.messages[0], (
        "the operator must be able to see an order go out, not only the ones "
        "that end in a fill"
    )


def test_a_zero_fill_still_announced_the_execution_attempt(monkeypatch):
    app = _taker_env(monkeypatch)
    client = _TakerClient(fills=[0.0], books={"yes": _book(0.53, 0.55)})

    _run_logic(CHAT, _taker_signal(), client, RiskManager())

    assert client.placed
    assert "Order being executed" in app.bot.messages[0]
    assert any("Unfilled" in m or "UNFILLED" in m for m in app.bot.messages)


# ── 6. A duplicate MAKER signal must not delete a real taker entry ───────────

def _canned_signal(sig):
    async def _evaluate(market, learned, state, spot_price=None, books=None):
        return sig
    return _evaluate


def test_a_duplicate_maker_quote_does_not_outrank_a_taker(monkeypatch):
    """A MAKER pair that cannot be sent must not delete the taker that can be.

    ``_resolve_collision`` prefers a locked MAKER pair over a TAKER by design.
    A *duplicate* pair -- one on a market where a quote is already resting --
    can never become an order, so letting it into that comparison silently
    removes real taker entries from the account.
    """
    maker_sig = TradeSignal(
        strategy="MAKER", event_id="event-pair", market_id=MARKET_ID,
        asset="BTC", timeframe="15min", outcome="BOTH", outcome_id="yes",
        certainty=0.6, mode_floor=0.0, win_prob=0.58, market_price=0.51,
        size_pct=0.02, reason="duplicate pair",
        legs=[QuoteLeg("YES", "yes", 0.35, 0.02, 0.417),
              QuoteLeg("NO", "no", 0.51, 0.02, 0.583)],
    )
    taker_sig = _taker_signal()
    monkeypatch.setattr(strategies._strategies["MAKER"], "evaluate",
                        _canned_signal(maker_sig))
    monkeypatch.setattr(strategies._strategies["TAKER"], "evaluate",
                        _canned_signal(taker_sig))

    learned = {
        "strategies": ["TAKER", "MAKER"],
        "open_positions": {
            "market-pair:NO:resting": {
                "market_id": MARKET_ID, "strategy": "MAKER", "outcome": "NO",
                "confirmed_filled": False, "filled_quantity": 0.0,
            },
        },
    }
    signals = asyncio.run(strategies.evaluate_all(
        _market(), learned, None,
    ))
    assert [s.strategy for s in signals] == ["TAKER"], (
        "the duplicate maker quote was allowed to suppress the taker"
    )


def test_a_locked_maker_pair_still_beats_a_taker_when_it_can_be_sent(monkeypatch):
    """The preference itself is deliberate; only the unexecutable duplicate goes."""
    maker_sig = TradeSignal(
        strategy="MAKER", event_id="event-pair", market_id=MARKET_ID,
        asset="BTC", timeframe="15min", outcome="BOTH", outcome_id="yes",
        certainty=0.6, mode_floor=0.0, win_prob=0.58, market_price=0.51,
        size_pct=0.02, reason="fresh pair",
        legs=[QuoteLeg("YES", "yes", 0.35, 0.02, 0.417),
              QuoteLeg("NO", "no", 0.51, 0.02, 0.583)],
    )
    monkeypatch.setattr(strategies._strategies["MAKER"], "evaluate",
                        _canned_signal(maker_sig))
    monkeypatch.setattr(strategies._strategies["TAKER"], "evaluate",
                        _canned_signal(_taker_signal()))

    signals = asyncio.run(strategies.evaluate_all(
        _market(), {"strategies": ["TAKER", "MAKER"], "open_positions": {}}, None,
    ))
    assert [s.strategy for s in signals] == ["MAKER"]
