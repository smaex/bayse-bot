"""
Shadow Tracker: Pre-Order / Mid-Market Strategy Empirical Evaluator
===================================================================
Zero-capital shadow monitor for the experimental two-sided maker idea.

Monitors every 5min and 15min candle open for BTC, ETH, and SOL on Bayse.
Simulates two-sided passive limit bids at mid-market (e.g. 0.475 on YES, 0.475 on NO).
Measures:
  1. Both displayed prices touched (<= 45s and <= 90s) and modeled gross pair spread.
  2. One-sided price-touch adverse-selection proxy.
  3. Gross modeled PnL before book position, queue, fees, and fill uncertainty.

A price touch is not proof that a passive order would have filled.

Outputs empirical proof to data/shadow_midmarket.jsonl and Telegram /shadow.
"""

import os
import json
import time
import logging
from typing import Dict, Optional

log = logging.getLogger("shadow_tracker")

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
LOG_FILE = os.path.join(DATA_DIR, "shadow_midmarket.jsonl")

# Target hypothetical bid prices around mid (0.475 + 0.475 = 0.950 -> +5.26% locked spread)
TARGET_BID = 0.475
EVAL_WINDOW_FAST_SEC = 45.0
EVAL_WINDOW_MAX_SEC  = 90.0

class ShadowCandle:
    def __init__(self, market_id: str, asset: str, timeframe: str, threshold: float, open_secs: float):
        self.market_id = market_id
        self.asset = asset
        self.timeframe = timeframe
        self.threshold = threshold
        self.open_secs = open_secs
        self.t0 = time.time()
        
        self.bid_yes = TARGET_BID
        self.bid_no  = TARGET_BID
        
        self.yes_filled_at: Optional[float] = None
        self.no_filled_at:  Optional[float] = None
        
        self.price_yes_45s: Optional[float] = None
        self.price_no_45s:  Optional[float] = None
        
        self.status = "TRACKING"  # TRACKING, COMPLETED
        self.result = "PENDING"   # BOTH_TOUCHED, ONE_LEG proxy, or NEITHER
        self.simulated_pnl = 0.0

    def update_prices(self, yes_p: float, no_p: float):
        elapsed = time.time() - self.t0
        
        # Record displayed YES price touching the hypothetical bid.
        if self.yes_filled_at is None and yes_p <= self.bid_yes:
            self.yes_filled_at = round(elapsed, 1)
            
        # Record displayed NO price touching the hypothetical bid.
        if self.no_filled_at is None and no_p <= self.bid_no:
            self.no_filled_at = round(elapsed, 1)

        if elapsed >= EVAL_WINDOW_FAST_SEC and self.price_yes_45s is None:
            self.price_yes_45s = yes_p
            self.price_no_45s = no_p

    def finalize(self, final_yes_p: float, final_no_p: float):
        self.status = "COMPLETED"
        elapsed = time.time() - self.t0
        
        yes_45 = self.yes_filled_at is not None and self.yes_filled_at <= EVAL_WINDOW_FAST_SEC
        no_45  = self.no_filled_at is not None and self.no_filled_at <= EVAL_WINDOW_FAST_SEC
        
        yes_90 = self.yes_filled_at is not None and self.yes_filled_at <= EVAL_WINDOW_MAX_SEC
        no_90  = self.no_filled_at is not None and self.no_filled_at <= EVAL_WINDOW_MAX_SEC

        # Sizing simulation: ₦100 per leg
        size_per_leg = 100.0

        if yes_45 and no_45:
            self.result = "BOTH_TOUCHED_45S"
            # Equal ₦ stakes at equal prices acquire equal share quantities.
            # ₦100 per leg at 0.475 costs ₦200 and pays ₦210.53 as a complete
            # set, for ₦10.53 gross—not the previous, incorrect ₦5.26.
            self.simulated_pnl = round(
                size_per_leg * (1.0 / self.bid_yes - 2.0), 2
            )
        elif yes_90 and no_90:
            self.result = "BOTH_TOUCHED_90S"
            self.simulated_pnl = round(
                size_per_leg * (1.0 / self.bid_yes - 2.0), 2
            )
        elif yes_90 and not no_90:
            self.result = "ONE_LEG_YES_ADVERSE"
            # Leg 1 filled, Leg 2 failed. If dumped at 45s/90s:
            exit_price = self.price_yes_45s or final_yes_p
            loss = (exit_price - self.bid_yes) * (size_per_leg / self.bid_yes)
            self.simulated_pnl = round(loss, 2)
        elif no_90 and not yes_90:
            self.result = "ONE_LEG_NO_ADVERSE"
            exit_price = self.price_no_45s or final_no_p
            loss = (exit_price - self.bid_no) * (size_per_leg / self.bid_no)
            self.simulated_pnl = round(loss, 2)
        else:
            self.result = "NEITHER_FILLED"
            self.simulated_pnl = 0.0

    def to_dict(self) -> dict:
        return {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(self.t0)),
            "market_id": self.market_id,
            "asset": self.asset,
            "timeframe": self.timeframe,
            "threshold": self.threshold,
            "result": self.result,
            "simulated_pnl_ngn": self.simulated_pnl,
            "yes_filled_at_sec": self.yes_filled_at,
            "no_filled_at_sec": self.no_filled_at,
            "price_yes_45s": self.price_yes_45s,
            "price_no_45s": self.price_no_45s,
        }


# Global in-memory state
_active_candles: Dict[str, ShadowCandle] = {}
_history_buffer: list = []

def on_market_scan(markets: list):
    """Detects brand new candles opening and initiates shadow tracking."""
    now = time.time()
    for m in markets:
        asset = m.get("asset", "")
        if asset not in ("BTC", "ETH", "SOL"):
            continue
        tf = m.get("timeframe", "")
        if tf not in ("5min", "15min"):
            continue
        secs = m.get("secs_to_close", 0)
        
        # Candle Open Window:
        # 15min candle: open window is secs >= 840 (first 60s)
        # 5min candle: open window is secs >= 270 (first 30s)
        is_new_candle = (tf == "15min" and secs >= 840) or (tf == "5min" and secs >= 270)
        mid = m.get("market_id", "")
        
        if is_new_candle and mid not in _active_candles:
            candle = ShadowCandle(
                market_id=mid,
                asset=asset,
                timeframe=tf,
                threshold=float(m.get("threshold") or 0.0),
                open_secs=secs,
            )
            _active_candles[mid] = candle
            log.info(f"🕶️ SHADOW TRACKER | New Candle detected {asset} {tf} | Placed hypothetical bids @ {TARGET_BID}")

    # Check for expired tracking windows
    to_remove = []
    for mid, candle in _active_candles.items():
        if (now - candle.t0) > EVAL_WINDOW_MAX_SEC:
            # Find current prices from markets
            curr = next((x for x in markets if x.get("market_id") == mid), None)
            final_yes = float(curr.get("yes_price") or 0.5) if curr else 0.5
            final_no  = float(curr.get("no_price") or 0.5) if curr else 0.5
            
            candle.finalize(final_yes, final_no)
            _save_record(candle.to_dict())
            _history_buffer.append(candle.to_dict())
            to_remove.append(mid)
            log.info(f"🕶️ SHADOW RESULT | {candle.asset} {candle.timeframe} -> {candle.result} | Sim PnL: ₦{candle.simulated_pnl:+.2f}")

    for mid in to_remove:
        _active_candles.pop(mid, None)


def on_price_update(market_id: str, prices: dict):
    """Updates active shadow tracking candles on live tick."""
    candle = _active_candles.get(market_id)
    if not candle:
        return
    yes_p = prices.get("yes")
    no_p  = prices.get("no")
    if yes_p is not None and no_p is not None:
        candle.update_prices(float(yes_p), float(no_p))


def _save_record(record: dict):
    """Appends completed shadow evaluation record to JSONL file."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as e:
        log.error(f"Failed to persist shadow record: {e}")


def get_summary_report() -> str:
    """Computes and formats a clean statistical summary of all shadow tracking data."""
    records = []
    if os.path.exists(LOG_FILE):
        try:
            with open(LOG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        records.append(json.loads(line))
        except Exception:
            records = _history_buffer
    else:
        records = _history_buffer

    if not records:
        return (
            "🕶️ *Shadow Tracker Report: Pre-Order / Mid-Market*\n\n"
            "Status: Active and monitoring candle opens.\n"
            "Data: 0 candle cycles recorded yet.\n"
            "_Data populates automatically on every 5m and 15m candle open._"
        )

    total = len(records)
    both_45 = sum(
        1 for r in records
        if r.get("result") in {"BOTH_TOUCHED_45S", "BOTH_FILLED_45S"}
    )
    both_90 = sum(
        1 for r in records
        if r.get("result") in {"BOTH_TOUCHED_90S", "BOTH_FILLED_90S"}
    )
    one_yes = sum(1 for r in records if r.get("result") == "ONE_LEG_YES_ADVERSE")
    one_no  = sum(1 for r in records if r.get("result") == "ONE_LEG_NO_ADVERSE")
    neither = sum(1 for r in records if r.get("result") == "NEITHER_FILLED")
    
    total_both = both_45 + both_90
    total_one_leg = one_yes + one_no
    total_sim_pnl = sum(r.get("simulated_pnl_ngn", 0.0) for r in records)

    both_pct = (total_both / total) * 100.0 if total > 0 else 0.0
    adverse_pct = (total_one_leg / total) * 100.0 if total > 0 else 0.0

    verdict = (
        "🟡 PRICE-TOUCH CANDIDATE — order-book fill validation still required"
        if both_pct >= 65.0 and total_sim_pnl > 0
        else "⚠️ HIGH ADVERSE-SELECTION RISK"
    )

    lines = [
        "🕶️ *Shadow Tracker: Pre-Order / Mid-Market*",
        f"Sample: *{total} candles* monitored (BTC, ETH, SOL)",
        "",
        f"✅ *Both Prices Touched (<=45s)*: {both_45} ({both_45/total:.1%})",
        f"✅ *Both Prices Touched (45-90s)*: {both_90} ({both_90/total:.1%})",
        f"⚠️ *One-Legged Trapped (Adverse)*: {total_one_leg} ({adverse_pct:.1%})",
        f"⚪ *Neither Leg Filled*: {neither} ({neither/total:.1%})",
        "",
        f"💰 *Simulated Net PnL*: *₦{total_sim_pnl:+,.2f}* (on ₦100 simulated legs)",
        f"📊 *Dual-Fill Success Rate*: *{both_pct:.1f}%*",
        "",
        f"*Verdict*: {verdict}",
        "_(Zero capital used. Displayed-price touches are not confirmed fills; "
        "never promote from this report alone.)_"
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(get_summary_report())
