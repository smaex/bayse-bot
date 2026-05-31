import asyncio
import logging
import re
import time
from typing import Optional
import database
import feeds
import scanner
import telegram_bot
import strategy
import comparative_analysis
from config import ARB_MAX_SIZE_NGN, CURRENCY, MIN_PAYOUT_RATIO

log = logging.getLogger("executor")

# These will be initialized by bot.py
active_markets = []
_tg_app = None
_FX_ASSETS = ["EURUSD", "GBPUSD", "EURGBP", "XAUUSD"]

# ── Engine inference cache ────────────────────────────────────────────────────
# Stores the detected engine type per market_id so we only probe the order
# book ONCE per market, not on every trade attempt.
_market_engine_cache: dict[str, str] = {}  # market_id → "AMM" | "CLOB"

# ── Per-market minimum order size cache ───────────────────────────────────────
# When a market rejects an order with "Minimum buy amount is NGN X", we cache
# that X here. Future orders on the same market are pre-flight checked so we
# never hit the API unnecessarily. ₦100 trades still work on all other markets.
_market_min_cache: dict[str, float] = {}  # market_id → min NGN required


async def _infer_engine(client, market: dict, timeout_ms: int = 300) -> str:
    """
    When market.get('engine') is absent, probe the order book to determine
    if this is a CLOB or AMM market.

    - Returns "CLOB" if the order book has any bids or asks.
    - Returns "AMM"  on timeout (>300ms), error, or empty book.
    - Results are cached so each market is only probed once.
    """
    market_id = market.get("market_id", "")

    # Return cached result immediately if we've seen this market before
    if market_id in _market_engine_cache:
        return _market_engine_cache[market_id]

    inferred = "AMM"  # safe default
    try:
        ob = await asyncio.wait_for(
            client.get_orderbook(market["event_id"], market_id),
            timeout=timeout_ms / 1000.0  # convert ms → seconds
        )
        # Bayse may return bids/asks at top level or nested under yes/no
        bids = ob.get("bids") or ob.get("yes", {}).get("bids") or []
        asks = ob.get("asks") or ob.get("yes", {}).get("asks") or []
        if bids or asks:
            inferred = "CLOB"
            log.info(
                f"Engine inferred as CLOB for {market_id} "
                f"({len(bids)} bids / {len(asks)} asks in book)"
            )
        else:
            log.debug(f"Engine inferred as AMM for {market_id} (empty order book)")

    except asyncio.TimeoutError:
        log.debug(
            f"Engine inference timed out ({timeout_ms}ms) for {market_id} "
            "— defaulting to AMM"
        )
    except Exception as exc:
        log.debug(f"Engine inference failed for {market_id}: {exc} — defaulting to AMM")

    # Cache so we never probe this market again
    _market_engine_cache[market_id] = inferred
    return inferred

def init_executor(markets, tg_app):
    global active_markets, _tg_app
    active_markets = markets
    _tg_app = tg_app

async def execute_trade(chat_id, sig, client, risk, settings, equity, free_cash):
    """decide and execute a single-sided trade"""
    
    # ── Systemic Halt Check ──
    if strategy.global_state.systemic_halt_until > time.time():
        log.warning(f"[{chat_id}] SYSTEMIC HALT active — skipping {sig.strategy}")
        return

    # ── Already in Position Check ──
    if risk.already_in(sig.market_id):
        log.debug(f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — Already in position or pending.")
        return

    risk.lock_market(sig.market_id)
    try:
        await _execute_trade_logic(chat_id, sig, client, risk, settings, equity, free_cash)
    finally:
        risk.unlock_market(sig.market_id)

async def _execute_trade_logic(chat_id, sig, client, risk, settings, equity, free_cash):
    mode     = settings.get("mode", "balanced")
    min_t    = settings.get("mintrade", 100)
    max_t    = settings.get("maxtrade", 5_000)   # BUG-FIX: was 500,000 (way too high)
    max_exp  = settings.get("maxexposure", 20.0) / 100.0
    learned  = settings.get("learned", {})
    mult     = learned.get("size_multipliers", {}).get(sig.strategy, 1.0)
    # User's configured risk % per trade (capped at 5% for safety)
    user_risk_pct = min(settings.get("risk_pct", 2.0), 5.0) / 100.0

    # ── Trailing Profit Shield (Strict Mode) ──
    if risk.is_in_strict_mode():
        # Once 80% of daily target is reached, only take 'God Tier' signals (>= 0.70)
        if sig.certainty < 0.70:
            log.info(f"[{chat_id}] SKIPPED — Strict Mode active (Gain Shield), certainty {sig.certainty:.2f} < 0.70")
            return

    # ── Conviction Sizing (Tiered Risk) ──────────────────────────────────────
    # Scales risk based on certainty tiers, anchored to user's risk_pct setting.
    # e.g. if risk_pct=2%: Low=1%, Mid=2%, High=3%, Top=4%
    if sig.certainty >= 0.90:
        tier_mult = 2.0   # 4% at 2% risk_pct
    elif sig.certainty >= 0.70:
        tier_mult = 1.5   # 3% at 2% risk_pct
    elif sig.certainty >= 0.55:
        tier_mult = 1.0   # 2% at 2% risk_pct
    else:
        tier_mult = 0.5   # 1% at 2% risk_pct

    base_pct = user_risk_pct * tier_mult

    # Apply external multipliers (FX factor, ML learned multipliers)
    fx_factor = 0.5 if sig.asset in _FX_ASSETS else 1.0
    raw_pct = base_pct * mult * fx_factor

    # ── Conviction Booster (Extreme Certainty) ──
    # BUG-FIX: Reduced from 2.0x to 1.5x — prevents doubling into hard cap
    if sig.certainty >= 0.95:
        conviction_mult = 1.5
        raw_pct *= conviction_mult
        log.info(f"[{chat_id}] 🔥 CONVICTION BOOSTER: Boosting size by {conviction_mult}x (Certainty {sig.certainty:.0%})")

    # ── Alpha Decay Shield (Anti-Bleed) ──
    # If the edge magnitude is shrinking over the last 10 trades, reduce size.
    decay_mult = await asyncio.to_thread(database.get_alpha_trend, chat_id, sig.strategy, sig.asset)
    if decay_mult < 0.85:
        raw_pct *= 0.5
        log.info(f"[{chat_id}] 🛡️ ALPHA DECAY SHIELD: Slashing size by 50% (Decay Factor: {decay_mult:.2f})")

    # ── Probationary Sizing ──
    if risk.is_on_probation():
        probation_mult = 0.25
        raw_pct *= probation_mult
        log.info(f"[{chat_id}] PROBATION ACTIVE: Slashing size by {1-probation_mult:.0%}")

    # ── Micro-Account Handling (For ₦2,000 starts) ─────────────────────────
    if equity < 3000:
        # On tiny accounts, we force a ₦100 minimum viable size.
        amount = 100.0
        log.info(f"[{chat_id}] Micro-Account Mode: forcing ₦100 minimum viable size")
    else:
        # BUG-FIX: Cap raw_pct at user_risk_pct * 3 (max 3x tier ceiling), not 10%
        capped_pct = min(raw_pct, user_risk_pct * 3.0)
        amount     = equity * capped_pct
        # Respect user's mintrade/maxtrade as authoritative boundaries first.
        amount     = max(min_t, min(max_t, amount))

    # ── Hard Safety Cap (5% equity) ──
    # The real ceiling is the LOWER of (5% equity) and the user's maxtrade.
    # - If user set maxtrade=₦2,000 and 5% equity=₦10,000 → ceiling is ₦2,000.
    # - If user set maxtrade=₦50,000 and 5% equity=₦5,000 → ceiling is ₦5,000.
    # This way the user's maxtrade is always respected and is never bypassed.
    system_cap = max(100.0, equity * 0.05)
    hard_cap   = min(system_cap, max_t)

    if amount > hard_cap:
        cap_reason = "user maxtrade cap" if hard_cap < system_cap else "5% equity cap"
        log.info(
            f"[{chat_id}] ⚖️ HARD CAP CLAMP: Scaling ₦{amount:,.0f} → ₦{hard_cap:,.0f} ({cap_reason})."
        )
        amount = hard_cap

    log.info(
        f"[{chat_id}] 📐 SIZING | equity=₦{equity:,.0f} risk_pct={user_risk_pct:.1%} "
        f"tier_mult={tier_mult:.1f}x base={base_pct:.2%} raw={raw_pct:.2%} "
        f"final=₦{amount:,.0f} hard_cap=₦{hard_cap:,.0f} "
        f"mintrade=₦{min_t:,.0f} maxtrade=₦{max_t:,.0f} mult={mult:.2f} fx={fx_factor:.1f}"
    )

    # Skip if below the effective minimum — the higher of ₦100 or user's mintrade.
    # This catches cases where the hard cap brought the amount below the user's floor.
    effective_minimum = max(100.0, min_t)
    if amount < effective_minimum:
        log.info(
            f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — "
            f"Final ₦{amount:,.0f} is below effective minimum ₦{effective_minimum:,.0f} "
            f"(mintrade=₦{min_t:,.0f})."
        )
        return

    if amount > free_cash:
        log.info(f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — Insufficient free cash (Need ₦{amount:,.0f}, Have ₦{free_cash:,.0f})")
        return


    # ── Safety Guardrails (Alpha Resurrection) ──
    # 1. Global Price Cap: Never buy above 0.80 (Downside is 4x upside)
    MAX_ENTRY_PRICE = 0.80
    if sig.market_price > MAX_ENTRY_PRICE:
        log.info(f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — Price {sig.market_price:.2f} above safety cap {MAX_ENTRY_PRICE}")
        return

    # Fetch market to get actual fee rate
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)

    # 2. Mathematical EV Filter
    # EV = win_p * (1.0 - effective_fee) / market_price - 1.0
    # We demand at least 5% EV margin for non-probe trades.
    fee_rate = market.get("fee_rate", settings.get("fee_rate", 0.04)) if market else settings.get("fee_rate", 0.04)
    win_p = sig.win_prob
    effective_fee = fee_rate * max(1.0 - sig.market_price, 0.5)
    ev = win_p * (1.0 - effective_fee) / sig.market_price - 1.0
    
    # ── Discovery Probe ──
    is_probe = False
    # Probes: Small trades to collect data even if certainty is modest (0.35+)
    # This keeps the bot 'in the game' without risking the bankroll.
    # BUG-FIX: Respect user's mintrade — if mintrade > ₦100, skip probe rather than
    # violate the user's minimum bet size configuration.
    if sig.certainty >= 0.35 and sig.certainty < sig.mode_floor:
        if min_t <= 100.0:
            is_probe = True
            amount = 100.0
            log.info(f"[{chat_id}] DISCOVERY PROBE: Sizing down to ₦100 for Bayesian data collection.")
        else:
            log.info(f"[{chat_id}] DISCOVERY PROBE skipped — mintrade=₦{min_t:,.0f} > ₦100 probe size.")

    if not is_probe and ev < 0.05:
        log.info(f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — Insufficient EV ({ev:+.1%})")
        return

    if not risk.can_trade(equity, amount, max_exp):
        log.info(f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — Risk limit or max exposure reached")
        return

    max_allowed_price = (1.0 - effective_fee) / (1.0 + 0.01) # Baseline 1% buffer
    is_fx = sig.asset in _FX_ASSETS
    
    # ── Adaptive Slippage ──
    slip_map = {"safe": 0.002, "balanced": 0.005, "aggressive": 0.01, "full_send": 0.025}
    base_slip = slip_map.get(mode, 0.005)
    
    # Volatility multiplier
    from strategies.utils import realized_vol_hourly
    from strategies.base import global_state
    import config
    current_vol = realized_vol_hourly(sig.asset, global_state)
    base_vol = config.ASSET_HOURLY_VOL.get(sig.asset, 0.022)
    vol_mult = current_vol / base_vol
    adaptive_slip = base_slip * vol_mult
    
    # ── Synthetic Quote Latency Shield ──
    import feeds_direct
    bias = feeds_direct.get_latency_bias(sig.asset, feeds.spot.get(sig.asset, 0.0))
    # If Oracle indicates price is moving against us, and Bayse hasn't updated yet.
    # bias > 0: Oracle > Bayse (Upward pressure)
    # bias < 0: Oracle < Bayse (Downward pressure)
    #
    # BN-5 FIX: Raised from 0.08% to 0.15% for crypto. At 0.08%, normal feed
    # scheduling differences between Bayse relay and Binance oracle caused constant
    # false blocks. FX assets are entirely exempt — they move too slowly for latency
    # arbitrage to matter. Also only block when the oracle data itself is fresh (<5s).
    _oracle_data = feeds_direct.direct_spot.get(sig.asset, {})
    _oracle_age  = time.time() - _oracle_data.get("time", 0)
    _oracle_fresh = _oracle_age < 5.0  # only trust oracle signal if data is <5s old
    
    _is_fx = sig.asset in _FX_ASSETS
    latency_threshold = 0.0 if _is_fx else 0.0015  # 0.15% for crypto, skip for FX
    
    if not _is_fx and _oracle_fresh and latency_threshold > 0:
        if sig.outcome == "YES" and bias < -latency_threshold:
            log.warning(f"[{chat_id}] 🛡️ LATENCY SHIELD: Blocked {sig.asset} YES. Oracle {bias:+.2%} below Bayse (oracle age {_oracle_age:.1f}s).")
            return
        if sig.outcome == "NO" and bias > latency_threshold:
            log.warning(f"[{chat_id}] 🛡️ LATENCY SHIELD: Blocked {sig.asset} NO. Oracle {bias:+.2%} above Bayse (oracle age {_oracle_age:.1f}s).")
            return

    # ── Engine Detection ──
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    declared_engine = market.get("engine") if market else None

    if declared_engine:
        # Bayse explicitly told us the engine — trust it, no extra call needed
        engine = declared_engine
    elif market:
        # Engine field missing — probe order book with 300ms timeout.
        # Result is cached so subsequent trades on this market are instant.
        engine = await _infer_engine(client, market, timeout_ms=300)
    else:
        engine = "AMM"  # no market object at all — safe fallback

    # ── Hybrid Maker/Taker Routing ──
    # CLOB markets: we can post a LIMIT order (maker) instead of immediately
    # taking liquidity. Makers pay no fee and get better fill prices.
    #
    # Use MAKER when:
    #   1. Market is CLOB (not AMM)
    #   2. Volatility is not extreme (<2.5x baseline)
    #   3. Either: high conviction signal (≥0.75) OR near-zero momentum
    #
    # Use TAKER otherwise (AMM always, or CLOB in fast-moving markets).
    is_maker = False
    time_in_force = "FAK"

    if engine == "CLOB" and vol_mult <= 2.5:
        normalized_threshold = 0.005 * vol_mult
        low_momentum  = abs(sig.momentum_at_entry) < normalized_threshold
        high_conviction = sig.certainty >= 0.75  # High-conviction → post limit, save fee

        if low_momentum or high_conviction:
            is_maker = True
            # Post 1% below current price — patient fill, no fee paid
            limit_price = min(sig.market_price * 0.99, max_allowed_price)
            time_in_force = "GTC"
            log.info(
                f"[{chat_id}] CLOB MAKER: Posting limit at {limit_price:.3f} "
                f"({'high conviction' if high_conviction else 'low momentum'}) — fee saved"
            )

    if not is_maker:
        limit_price = min(sig.market_price * (1.0 + adaptive_slip), max_allowed_price)

    execution_style = "MAKER" if is_maker else "TAKER"
    log.info(
        f"[{chat_id}] PLACING {sig.strategy} | {sig.asset} ({engine} {execution_style}) "
        f"price={sig.market_price:.3f} limit={limit_price:.3f} slip={adaptive_slip:.2%} "
        f"certainty={sig.certainty:.0%} ₦{amount:,.0f}"
    )

    try:
        # ── Slippage Shield (Alpha Capture Phase) ──
        # Check recent performance. If slippage > 1.5%, reduce size by 50%
        avg_slip = await asyncio.to_thread(database.get_avg_slippage, sig.asset, sig.strategy)
        if avg_slip > 0.015:
            log.warning(f"[{chat_id}] SLIPPAGE SHIELD ACTIVE: Reducing size for {sig.asset} ({avg_slip:.1%} avg slip)")
            amount = amount * 0.5
            # BUG-FIX: Re-check mintrade after slippage shield halves amount.
            # Without this, a trade can proceed below the user's minimum bet size.
            if amount < effective_minimum:
                log.info(
                    f"[{chat_id}] SKIPPED after slippage shield — "
                    f"halved amount ₦{amount:,.0f} < mintrade ₦{min_t:,.0f}."
                )
                return

        # Place the order
        # AMM usually requires MARKET + maxSlippage. CLOB can take LIMIT.
        final_order_type = "LIMIT" if engine == "CLOB" else "MARKET"
        
        multiplier = 100.0 if CURRENCY == "NGN" else 1.0
        order_amount = amount
        if final_order_type == "LIMIT":
            min_shares = 100.0
            order_amount = amount / (limit_price * multiplier)
            if order_amount < min_shares:
                needed_ngn = min_shares * limit_price * multiplier
                # To protect capital, only scale up if it doesn't exceed 25% of equity (and respects hard cap)
                max_allowed_for_scaling = min(hard_cap, equity * 0.25)
                if needed_ngn <= max_allowed_for_scaling and needed_ngn <= free_cash:
                    log.info(f"[{chat_id}] CLOB SIZING: Scaling up LIMIT order from {order_amount:.2f} to {min_shares:.2f} shares (₦{amount:,.0f} → ₦{needed_ngn:,.0f}) to meet exchange minimum.")
                    amount = needed_ngn
                    order_amount = min_shares
                else:
                    log.info(f"[{chat_id}] SKIPPED — CLOB LIMIT order requires {min_shares:.2f} shares (₦{needed_ngn:,.0f}), which exceeds safety limit (₦{max_allowed_for_scaling:,.0f}) or free cash.")
                    return
            
        # ── Pre-flight: check cached market minimum before hitting the API ──
        cached_min = _market_min_cache.get(sig.market_id, 0.0)
        if cached_min > 0 and amount < cached_min:
            log.info(
                f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — "
                f"Market requires ₦{cached_min:,.0f} minimum (cached). Our ₦{amount:,.0f} is too small. "
                f"Will pick this market again when capital grows."
            )
            return

        resp = await client.place_order(
            event_id=sig.event_id, market_id=sig.market_id,
            outcome_id=sig.outcome_id, side="BUY", amount=order_amount,
            order_type=final_order_type, price=limit_price if final_order_type == "LIMIT" else None,
            max_slippage=adaptive_slip,
            currency=CURRENCY,
            time_in_force=time_in_force
        )
        order = resp.get("order", resp)
        # Robust share/quantity detection across different API versions
        shares_filled = float(
            order.get("filledSize") or
            order.get("shares") or 
            order.get("quantity") or 
            order.get("sharesFilled") or 
            order.get("sharesMatched") or 
            order.get("amountMatched") or 
            order.get("filledQuantity") or 
            0
        )
        
        # ── Order Chaser (CLOB only) ──
        # If CLOB and zero fill, try one more time at a slightly more aggressive price (if still profitable)
        if engine == "CLOB" and not is_maker and shares_filled <= 0:
            chase_price = min(limit_price * 1.002, max_allowed_price)
            if chase_price > limit_price:
                log.info(f"[{chat_id}] CLOB CHASE: First order missed. Retrying at {chase_price:.3f}")
                chase_shares = amount / (chase_price * multiplier)
                if chase_shares < 100.0:
                    chase_shares = 100.0
                    log.info(f"[{chat_id}] CLOB CHASE SIZING: Scaling chase shares to 100.0 to meet exchange minimum.")
                try:
                    resp = await client.place_order(
                        event_id=sig.event_id, market_id=sig.market_id,
                        outcome_id=sig.outcome_id, side="BUY", amount=chase_shares,
                        order_type="LIMIT", price=chase_price, currency=CURRENCY,
                    )
                except Exception as e:
                    log.debug(f"[{chat_id}] CLOB CHASE order failed at {chase_price:.3f}: {e}")
                    # Fall through — shares_filled stays 0, zero-fill path handles it below
                else:
                    order = resp.get("order", resp)
                shares_filled = float(
                    order.get("filledSize") or
                    order.get("shares") or 
                    order.get("quantity") or 
                    order.get("sharesFilled") or 
                    order.get("sharesMatched") or 
                    order.get("amountMatched") or 
                    order.get("filledQuantity") or 
                    0
                )

        filled_price = float(order.get("avgFillPrice") or order.get("price", limit_price) or limit_price)
        
        # Fallback: if we have an ID and it's successful, but no shares_filled was returned, estimate it.
        order_id = order.get("id") or order.get("orderId") or order.get("order_id")
        if shares_filled <= 0 and order_id:
            log.info(f"[{chat_id}] Order {order_id} has no explicit fill qty in response, assuming success.")
            shares_filled = amount / (filled_price * multiplier)

        if shares_filled <= 0:
            log.info(f"[{chat_id}] {sig.asset} order not filled (price moved away). Response: {resp}")
            return

        bayse_order_id = order_id
        actual_ngn = shares_filled * filled_price * multiplier

        spot_vs_thresh = 0.0
        if market and market.get("threshold") and feeds.spot.get(sig.asset):
            spot_vs_thresh = (feeds.spot[sig.asset] - market["threshold"]) / market["threshold"]

        trade_id = await asyncio.to_thread(
            database.record_trade,
            chat_id=chat_id, strategy=sig.strategy, asset=sig.asset,
            timeframe=sig.timeframe, outcome=sig.outcome, outcome_id=sig.outcome_id,
            market_id=sig.market_id, event_id=sig.event_id, order_id=bayse_order_id,
            entry_price=filled_price, amount_ngn=actual_ngn, certainty=sig.certainty,
            secs_to_close=market["secs_to_close"] if market else 0,
            spot_vs_threshold_pct=spot_vs_thresh,
            momentum_at_entry=sig.momentum_at_entry,
            regime_at_entry=sig.regime_at_entry,
            edge_at_entry=sig.edge_at_entry,
            realized_vol_at_entry=sig.realized_vol_at_entry,
            market_price_at_entry=sig.market_price,
            slippage_ngn=((filled_price / sig.market_price) - 1.0) * actual_ngn if sig.market_price > 0 else 0,
            poly_price_at_entry=await comparative_analysis.get_comparative_price(sig.asset, market.get("threshold", 0)) if market else None,
            engine=engine,
            execution_style=execution_style
        )


        risk.add_position(sig.market_id, {
            "trade_id": trade_id, "event_id": sig.event_id,
            "outcome": sig.outcome, "outcome_id": sig.outcome_id,
            "entry_price": filled_price, "amount_ngn": actual_ngn,
            "strategy": sig.strategy, "asset": sig.asset, "timeframe": sig.timeframe,
        })

    except Exception as e:
        err_str = str(e)
        # ── Graceful Skip: Exchange minimum buy amount enforcement ──
        # Parse the NGN minimum from the error message and cache it for this
        # market so future attempts skip pre-flight without hitting the API.
        # Example message: "Minimum buy amount is NGN 500.00."
        min_match = re.search(r'Minimum buy amount is [A-Z]+ ([\d,.]+)', err_str)
        if min_match:
            market_min = float(min_match.group(1).replace(',', ''))
            _market_min_cache[sig.market_id] = market_min
            log.info(
                f"[{chat_id}] SKIPPED {sig.strategy} | {sig.asset} — "
                f"Market {sig.market_id} enforces ₦{market_min:,.0f} minimum (now cached). "
                f"Our ₦{amount:,.0f} is too small — will try other markets."
            )
        else:
            log.error(f"[{chat_id}] order failed {sig.market_id}: {e}", exc_info=True)
        return

    # ── BUG-FIX: Notification is OUTSIDE the order try/except ──
    # Previously, if notify_trade() threw (e.g. Markdown parse error from special
    # chars in sig.reason), the exception was silently swallowed as "order failed".
    # Now it has its own isolated try/except with a clear error log.
    if _tg_app:
        try:
            # notify_trade() handles Markdown sanitization and plain-text fallback internally.
            await telegram_bot.notify_trade(_tg_app, chat_id, sig, actual_ngn)
            log.info(f"[{chat_id}] ✅ Notification sent for {sig.strategy} {sig.asset} ₦{actual_ngn:,.0f}")
        except Exception as notify_err:
            log.error(f"[{chat_id}] ❌ NOTIFICATION FAILED for {sig.strategy} {sig.asset}: {notify_err}", exc_info=True)
    else:
        log.warning(f"[{chat_id}] ⚠️ _tg_app is None — notification skipped. Bot may not have initialized Telegram correctly.")

async def execute_arb(chat_id, sig, client, equity, free_cash, settings):
    """Executes a market-neutral arbitrage"""
    market = next((m for m in active_markets if m["market_id"] == sig.market_id), None)
    if not market: return

    budget = min(ARB_MAX_SIZE_NGN, free_cash * 0.10)
    if budget < 200: return

    try:
        yes_p = market["yes_price"]; no_p = market["no_price"]
        amount_yes = round(budget * (yes_p / (yes_p + no_p)), 2)
        amount_no = round(budget * (no_p / (yes_p + no_p)), 2)
        
        quote = await client.get_quote(sig.event_id, sig.market_id, market["yes_id"], "BUY", amount_yes, CURRENCY)
        if float(quote.get("sharesMatched", 0)) <= 0: return
    except Exception as e:
        log.debug(f"[{chat_id}] ARB pre-flight quote failed for {sig.market_id}: {e}")
        return

    yes_ok = False; yes_shares = 0
    try:
        batch_orders = [
            {
                "outcomeId": market["yes_id"],
                "side": "BUY",
                "type": "MARKET",
                "amount": amount_yes,
                "currency": CURRENCY,
                "timeInForce": "FAK"
            },
            {
                "outcomeId": market["no_id"],
                "side": "BUY",
                "type": "MARKET",
                "amount": amount_no,
                "currency": CURRENCY,
                "timeInForce": "FAK"
            }
        ]
        
        batch_resp = await client.batch_place_orders(batch_orders)
        results = batch_resp.get("results", [])
        if len(results) < 2:
            raise RuntimeError("Invalid batch response")
            
        yes_res = results[0]
        no_res = results[1]
        
        yes_ok = yes_res.get("success", False)
        if yes_ok:
            y_ord = yes_res.get("order", {})
            yes_shares = float(y_ord.get("shares", y_ord.get("quantity", 0)) or 0)
            
        no_ok = no_res.get("success", False)
        no_shares = 0
        if no_ok:
            n_ord = no_res.get("order", {})
            no_shares = float(n_ord.get("shares", n_ord.get("quantity", 0)) or 0)
            
        if not yes_ok or not no_ok:
            raise RuntimeError(f"Batch incomplete. YES ok: {yes_ok}, NO ok: {no_ok}")

        burn_pairs = min(yes_shares, no_shares)
        if burn_pairs > 0:
            await client.burn_shares(sig.market_id, burn_pairs, CURRENCY)
            profit = burn_pairs - (amount_yes + amount_no)
            log.info(f"[{chat_id}] ARB SUCCESS | {sig.asset} | {burn_pairs:.2f} pairs | Profit: ₦{profit:,.2f}")
            
            trade_id = await asyncio.to_thread(
                database.record_trade,
                chat_id=chat_id, strategy="ARB", asset=sig.asset, timeframe=sig.timeframe,
                outcome="ARB", outcome_id="burn", market_id=sig.market_id, event_id=sig.event_id,
                order_id=str(burn_pairs), entry_price=yes_p + no_p, amount_ngn=budget,
                certainty=1.0, secs_to_close=0, spot_vs_threshold_pct=0.0
            )
            await asyncio.to_thread(database.resolve_trade, trade_id, won=True, pnl_ngn=profit)
            if _tg_app: await telegram_bot.notify_arb(_tg_app, chat_id, sig, burn_pairs, profit)

    except Exception as e:
        log.error(f"[{chat_id}] ARB error: {e}")
        if yes_ok and yes_shares > 0:
            try:
                await client.place_order(sig.event_id, sig.market_id, market["yes_id"], "SELL", amount_yes, "MARKET", currency=CURRENCY)
                log.info(f"[{chat_id}] ARB ROLLBACK SUCCESS")
            except Exception as re:
                log.critical(f"[{chat_id}] ARB ROLLBACK FAILED: {re}")
