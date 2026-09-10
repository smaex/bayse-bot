-- Read-only Bayse bot performance audit.
-- Run this in the Supabase/PostgreSQL SQL editor after replacing the value
-- below with your own Telegram chat ID. The result grids do not output it.
-- This query never reads or returns API credentials.

-- 1) Data coverage: confirms what assets/strategies actually traded.
WITH params AS (
    SELECT 'REPLACE_WITH_YOUR_TELEGRAM_CHAT_ID'::text AS chat_id
), scoped AS (
    SELECT t.*
    FROM trades t, params p
    WHERE t.chat_id = p.chat_id
)
SELECT
    MIN(created_at) AS first_trade_at,
    MAX(created_at) AS last_trade_at,
    COUNT(*) AS recorded_orders,
    COUNT(*) FILTER (WHERE won IS NOT NULL) AS resolved_trades,
    COUNT(*) FILTER (WHERE resolved_at IS NULL) AS unresolved_trades,
    COUNT(*) FILTER (WHERE won IS NULL AND resolved_at IS NOT NULL) AS void_or_unfilled,
    COUNT(*) FILTER (WHERE filled_quantity > 0) AS rows_with_confirmed_quantity,
    STRING_AGG(DISTINCT asset, ', ' ORDER BY asset) AS assets_seen,
    STRING_AGG(DISTINCT strategy, ', ' ORDER BY strategy) AS strategies_seen,
    STRING_AGG(DISTINCT timeframe, ', ' ORDER BY timeframe) AS timeframes_seen
FROM scoped;

-- 2) Strategy x asset x timeframe economics with 95% Wilson win-rate bounds.
-- "stored_pnl" is the bot's recorded result. Older settlement records may
-- contain the pre-fix NGN multiplier error.
-- "binary_reconstructed_pnl" independently reconstructs normal binary
-- settlement from amount and entry price. It is diagnostic only for ARB or
-- early-exit records, whose economics are not a simple settle-at-0/1 payoff.
WITH params AS (
    SELECT 'REPLACE_WITH_YOUR_TELEGRAM_CHAT_ID'::text AS chat_id
), base AS (
    SELECT
        strategy,
        asset,
        timeframe,
        won,
        amount_ngn::numeric AS amount_ngn,
        entry_price::numeric AS entry_price,
        pnl_ngn::numeric AS stored_pnl,
        CASE
            WHEN strategy = 'ARB' THEN NULL
            WHEN won = 1 AND entry_price > 0
                THEN amount_ngn::numeric * (1.0 / entry_price::numeric - 1.0)
            WHEN won = 0 THEN -amount_ngn::numeric
            ELSE NULL
        END AS binary_reconstructed_pnl
    FROM trades t, params p
    WHERE t.chat_id = p.chat_id
      AND won IS NOT NULL
      AND amount_ngn > 0
), grouped AS (
    SELECT
        strategy,
        asset,
        timeframe,
        COUNT(*)::numeric AS n,
        SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END)::numeric AS wins,
        SUM(amount_ngn) AS deployed_ngn,
        SUM(stored_pnl) AS stored_pnl_ngn,
        SUM(binary_reconstructed_pnl) AS reconstructed_pnl_ngn,
        AVG(entry_price) AS avg_entry_price,
        -- Common-probability break-even rate, weighted by each trade's payout.
        SUM(amount_ngn) / NULLIF(SUM(amount_ngn / NULLIF(entry_price, 0)), 0)
            AS capital_weighted_break_even_wr,
        AVG(CASE WHEN stored_pnl > 0 THEN stored_pnl END) AS avg_stored_win,
        AVG(CASE WHEN stored_pnl < 0 THEN ABS(stored_pnl) END) AS avg_stored_loss
    FROM base
    GROUP BY strategy, asset, timeframe
), stats AS (
    SELECT *, wins / NULLIF(n, 0) AS observed_wr
    FROM grouped
)
SELECT
    strategy,
    asset,
    timeframe,
    n::integer AS resolved_n,
    wins::integer AS wins,
    ROUND((observed_wr * 100)::numeric, 2) AS win_rate_pct,
    ROUND((
        (observed_wr + 1.9208 / n)
        / (1 + 3.8416 / n)
        - 1.96 * SQRT(
            observed_wr * (1 - observed_wr) / n + 3.8416 / (4 * n * n)
          ) / (1 + 3.8416 / n)
    ) * 100, 2) AS win_rate_95_low_pct,
    ROUND((
        (observed_wr + 1.9208 / n)
        / (1 + 3.8416 / n)
        + 1.96 * SQRT(
            observed_wr * (1 - observed_wr) / n + 3.8416 / (4 * n * n)
          ) / (1 + 3.8416 / n)
    ) * 100, 2) AS win_rate_95_high_pct,
    ROUND((capital_weighted_break_even_wr * 100)::numeric, 2)
        AS break_even_wr_pct,
    ROUND(((observed_wr - capital_weighted_break_even_wr) * 100)::numeric, 2)
        AS observed_edge_percentage_points,
    ROUND(deployed_ngn, 2) AS deployed_ngn,
    ROUND(stored_pnl_ngn, 2) AS stored_pnl_ngn,
    ROUND((100 * stored_pnl_ngn / NULLIF(deployed_ngn, 0))::numeric, 2)
        AS stored_roi_pct,
    ROUND(reconstructed_pnl_ngn, 2) AS binary_reconstructed_pnl_ngn,
    ROUND(avg_entry_price, 4) AS avg_entry_price,
    ROUND(avg_stored_win, 2) AS avg_stored_win_ngn,
    ROUND(avg_stored_loss, 2) AS avg_stored_loss_ngn
FROM stats
ORDER BY stored_pnl_ngn DESC NULLS LAST, resolved_n DESC;

-- 3) Asset-level totals. Use this to decide what to retain, reduce, or suspend;
-- do not rank an asset with a tiny sample as if it were proven.
WITH params AS (
    SELECT 'REPLACE_WITH_YOUR_TELEGRAM_CHAT_ID'::text AS chat_id
)
SELECT
    asset,
    COUNT(*) FILTER (WHERE won IS NOT NULL) AS resolved_n,
    COUNT(*) FILTER (WHERE won = 1) AS wins,
    ROUND((100.0 * COUNT(*) FILTER (WHERE won = 1)
        / NULLIF(COUNT(*) FILTER (WHERE won IS NOT NULL), 0))::numeric, 2)
        AS win_rate_pct,
    ROUND((SUM(amount_ngn) FILTER (WHERE won IS NOT NULL))::numeric, 2)
        AS deployed_ngn,
    ROUND((SUM(pnl_ngn) FILTER (WHERE won IS NOT NULL))::numeric, 2)
        AS stored_pnl_ngn,
    ROUND((100.0 * (SUM(pnl_ngn) FILTER (WHERE won IS NOT NULL))
        / NULLIF((SUM(amount_ngn) FILTER (WHERE won IS NOT NULL)), 0))::numeric, 2)
        AS stored_roi_pct
FROM trades t, params p
WHERE t.chat_id = p.chat_id
GROUP BY asset
ORDER BY stored_pnl_ngn DESC NULLS LAST;
