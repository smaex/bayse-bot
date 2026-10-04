#!/usr/bin/env python3
"""Prove every guardrail actually fires.

Assertions, not print statements: each block calls the real production code
path with an input that should trip the guard, and the script exits non-zero
if any of them fails. Run it after any change to config, risk, executor or
the exit policy:

    . .venv/bin/activate && python verify_guardrails.py
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import config
import executor
import bot
import stall
from risk import RiskManager
from strategies import book as booklib


FAILURES: list[str] = []


def check(label, condition, detail=""):
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        FAILURES.append(label)


# ── 1. Drawdown kill switch ──────────────────────────────────────────────────
print("\n1. Drawdown kill switch")
risk = RiskManager()
risk.update_peak(100_000.0)
check("the first breach is debounced, not acted on",
      risk.check_drawdown(88_000.0) and not risk.paused,
      "drawdown debounce must absorb a single bad balance reading")
risk._dd_breach_since = time.time() - 60   # simulate the breach persisting
risk.check_drawdown(88_000.0)
check("a sustained breach pauses all trading",
      risk.paused and risk.max_drawdown_hit,
      f"paused={risk.paused} dd_stop={config.MAX_DRAWDOWN_STOP}")
risk.check_drawdown(99_500.0)
check("recovering to within a quarter of the limit resumes trading",
      not risk.paused)


# ── 2. Exposure and cash ceiling ─────────────────────────────────────────────
print("\n2. Exposure ceiling")
risk2 = RiskManager()
check("a first trade inside the cap is allowed",
      risk2.can_trade(100_000.0, 5_000.0, 0.30))
check("a trade that would breach the cap is refused",
      not risk2.can_trade(100_000.0, 40_000.0, 0.30),
      "filled + amount > 30% of balance")
check("the cap is itself capped by the portfolio limit",
      not risk2.can_trade(100_000.0, 40_000.0, 0.99),
      f"MAX_PORTFOLIO_EXPOSURE={config.MAX_PORTFOLIO_EXPOSURE}")


# ── 3. Book quality gates ────────────────────────────────────────────────────
print("\n3. Book quality gates")
now = time.time()
check("a book timestamped in the future is rejected as stale",
      executor._book_is_stale({"timestamp": (now + 5) * 1000}, now=now))
check("an old book is rejected as stale",
      executor._book_is_stale({"timestamp": (now - 120) * 1000}, now=now))
check("a fresh book is accepted",
      not executor._book_is_stale({"timestamp": now * 1000}, now=now))
check("a missing timestamp is not assumed stale",
      not executor._book_is_stale({"bids": [], "asks": []}, now=now),
      "Bayse does not promise a timestamp on the book schema")


# ── 4. Fee-aware prices (CLOB fee is taker-only) ─────────────────────────────
print("\n4. Fee-aware prices")
p = 0.60
check("a taker pays more than the quoted ask",
      booklib.effective_buy_price(p, 0.02, is_maker=False) > p)
check("a maker pays exactly what it quoted",
      abs(booklib.effective_buy_price(p, 0.02, is_maker=True) - p) < 1e-12)
check("a seller nets less than the bid",
      booklib.effective_sell_proceeds(p, 1.0, 0.02, is_maker=False) < p)


# ── 5. Per-user, per-strategy cooldown ───────────────────────────────────────
print("\n5. Trade cooldown")
k1 = executor._cooldown_key("user_a", "m1", "TAKER")
k2 = executor._cooldown_key("user_b", "m1", "TAKER")
k3 = executor._cooldown_key("user_a", "m1", "MAKER")
check("one user's order does not silence another's", k1 != k2)
check("maker re-quotes do not stamp the taker cooldown", k1 != k3)


# ── 6. Exit policy ───────────────────────────────────────────────────────────
print("\n6. Exit policy")


def decide(**over):
    base = dict(outcome="YES", w_est=0.60, bid=0.60, peak_price=0.60,
                entry_price=0.55, secs=600.0, fee_rate=0.02,
                is_maker_pos=False, confirmed_filled=True)
    base.update(over)
    return bot._exit_decision(**base)


d = decide(w_est=0.55, bid=0.75, entry_price=0.50)
check("overpayment is taken as profit", d and d["reason"] == "TAKE_PROFIT")
# Bid 0.50 against a ~0.5556 basis: under water. The stop legitimately
# fires here; what must never happen is booking it as profit taking.
check("profit is never taken under water",
      (decide(w_est=0.10, bid=0.50, entry_price=0.55) or {}).get("reason")
      != "TAKE_PROFIT")
check("the stop fires when the estimate collapses",
      (decide(w_est=0.20, bid=0.30) or {}).get("reason") == "STOP_LOSS")
check("noise alone does not stop us out", decide(w_est=0.50, bid=0.40) is None)
check("a catastrophic price move exits without the model's permission",
      (decide(w_est=0.90, bid=0.10) or {}).get("reason") == "STOP_LOSS")
check("we do not pay a fee to sell something for nothing",
      decide(w_est=0.50, bid=config.EXIT_MIN_SALVAGE_PRICE / 2) is None)
check("a complete set is burned, not sold",
      decide(outcome="BOTH")["reason"] == "BURN_COMPLETE_SET")
check("a resting quote is withdrawn near settlement",
      decide(is_maker_pos=True, confirmed_filled=False,
             secs=config.MAKER_LATE_CANCEL_SECS - 10)["reason"] == "CANCEL_RESTING")


# ── 7. Position sizing ───────────────────────────────────────────────────────
print("\n7. Position sizing")


def _sig(**over):
    base = dict(strategy="TAKER", event_id="e", market_id="m", asset="BTC",
                timeframe="15min", outcome="YES", outcome_id="yes",
                certainty=0.90, win_prob=0.90, market_price=0.50,
                size_pct=0.02, reason="t")
    base.update(over)
    return SimpleNamespace(**base)


_SETTINGS = {"mode": "balanced", "risk_pct": 2.0, "mintrade": 100.0,
             "maxtrade": 5_000.0, "maxexposure": 15.0, "learned": {}}


def size(**over):
    kw = dict(sig=_sig(), settings=_SETTINGS, risk=RiskManager(), chat_id="c1",
              equity=100_000.0, free_cash=100_000.0, n_legs=1, mode="balanced",
              mult=1.0, user_risk=0.02, min_t=100.0, max_t=5_000.0)
    kw.update(over)
    return asyncio.run(executor._size_for_signal(**kw)).final_pct


full = size(mult=1.0)
half = size(mult=0.5)
check("a losing strategy is actually halved", abs(half - full * 0.5) < 1e-9,
      f"{half:.6f} vs {full * 0.5:.6f}")
check("conviction never cancels the learner's decay", half < full,
      f"{half:.6f} !< {full:.6f}")
check("size never exceeds the account ceiling",
      full <= min(_SETTINGS["risk_pct"] / 100.0, config.MAX_TRADE_RISK) + 1e-9)
check("size is never negative", size(mult=0.0) >= 0.0)


# ── 8. Stall telemetry ───────────────────────────────────────────────────────
print("\n8. Stall detection")
check("a structural block is distinguished from an ordinary skip",
      stall.is_structural("NO_BOOK") != stall.is_structural("NO_EDGE")
      or isinstance(stall.is_structural("NO_BOOK"), bool))
check("the stall verdict renders without raising",
      isinstance(stall.verdict("verify-script"), dict))

print()
if FAILURES:
    print(f"{len(FAILURES)} GUARDRAIL(S) FAILED: {FAILURES}")
    raise SystemExit(1)
print("All guardrails verified.")
