"""Executor audit (taker and maker), written before the fixes."""

import asyncio

import executor
from client import BayseClient
from risk import RiskManager
from test_executor_fail_closed import FakeClient, _install, _signal


class _FilledClient(FakeClient):
    parse_filled_shares = staticmethod(BayseClient.parse_filled_shares)

    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        return {"order": {
            "id": "buy-1", "status": "filled", "quantity": 2.0,
            "avgFillPrice": 0.60, "totalCost": kwargs["amount"],
        }}


def _quote():
    return {"price": 0.60, "quantity": 2, "amount": 100,
            "currencyBaseMultiplier": 100, "completeFill": True}


SETTINGS = {"mode": "balanced", "risk_pct": 2, "mintrade": 100, "maxtrade": 5_000}


# Taker B1: a confirmed fill must never be reported as a rejection ---------

def test_confirmed_fill_survives_a_post_fill_bookkeeping_error(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(executor.database, "record_trade", lambda **_kwargs: "trade-1")
    monkeypatch.setattr(executor, "_tg_app", None)
    monkeypatch.setattr(executor.telegram_bot, "_bot_app", None)

    def boom(*_args, **_kwargs):
        raise RuntimeError("stall bookkeeping broke after the fill")

    monkeypatch.setattr(executor.stall, "note_order", boom)

    client = _FilledClient(_quote())
    risk = RiskManager()
    asyncio.run(executor._execute_logic("chat", _signal(), client, risk, dict(SETTINGS), 20_000, 20_000))

    # The shares are on the exchange. The risk book must hold them, or exits
    # never see the position.
    assert len(risk.open_positions) == 1
    position = next(iter(risk.open_positions.values()))
    assert position["filled_quantity"] == 2.0
    assert position["confirmed_filled"] is True


# Maker M1: a definite rejection must not freeze the account ---------------

class _Leg:
    def __init__(self, outcome, outcome_id, price):
        self.outcome = outcome
        self.outcome_id = outcome_id
        self.price = price
        self.fair_value = 0.5


class _RejectingMakerClient(FakeClient):
    async def place_order(self, **kwargs):
        self.place_calls.append(kwargs)
        raise RuntimeError("API 422 post-only order would cross the spread")

    async def cancel_order(self, _order_id):
        return {"status": "cancelled"}


def test_definite_maker_rejection_does_not_pause_the_account(monkeypatch):
    _install(monkeypatch)
    monkeypatch.setattr(executor, "_tg_app", None)
    monkeypatch.setattr(executor.telegram_bot, "_bot_app", None)
    monkeypatch.setattr(executor.database, "record_trade", lambda **_kwargs: "trade-1")

    client = _RejectingMakerClient({})
    risk = RiskManager()
    settings = dict(SETTINGS)
    sig = _signal()
    sig.strategy = "MAKER"
    legs = [_Leg("YES", "yes", 0.45), _Leg("NO", "no", 0.45)]

    asyncio.run(executor._place_maker_quote(
        "chat", sig, client, risk, settings, legs, [100.0, 100.0],
        market=None, equity=20_000, max_exp=20_000,
    ))

    assert len(client.place_calls) == 1
    assert not settings.get("paused"), "a 422 is a definite rejection, not an unknown outcome"
    assert risk.paused is False
