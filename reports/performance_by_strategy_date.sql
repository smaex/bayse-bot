-- Read-only daily performance report by strategy, asset, and timeframe.
-- Compatible with the current pre-migration Supabase/PostgreSQL schema.
--
-- 1. Replace REPLACE_WITH_YOUR_TELEGRAM_CHAT_ID once.
-- 2. Adjust start_date/end_date if desired. Both dates are inclusive.
-- 3. Run in the Supabase SQL editor and export/copy the result grid.
--
-- No API credentials or encrypted keys are read or returned.

WITH params AS (
    SELECT
        'REPLACE_WITH_YOUR_TELEGRAM_CHAT_ID'::text AS chat_id,
        DATE '2000-01-01' AS start_date,
        CURRENT_DATE AS end_date,
        'Africa/Lagos'::text AS trading_timezone
), resolved AS (
    SELECT
        (t.created_at AT TIME ZONE p.trading_timezone)::date AS trade_date,
        (t.resolved_at AT TIME ZONE p.trading_timezone)::date AS resolved_date,
        t.strategy,
        t.asset,
        t.timeframe,
        t.outcome,
        t.won,
        t.amount_ngn::numeric AS amount_ngn,
        t.entry_price::numeric AS entry_price,
        t.pnl_ngn::numeric AS stored_pnl_ngn,
        t.certainty::numeric AS certainty,
        t.slippage_ngn::numeric AS slippage_ngn,
        CASE
            -- ARB and early exits are not simple settle-at-0/1 trades, so the
            -- reconstructed value is a diagnostic rather than ground truth.
            WHEN t.strategy = 'ARB' THEN NULL
            WHEN t.won = 1 AND t.entry_price > 0
                THEN t.amount_ngn::numeric
                     * (1.0 / t.entry_price::numeric - 1.0)
            WHEN t.won = 0 THEN -t.amount_ngn::numeric
            ELSE NULL
        END AS binary_reconstructed_pnl_ngn
    FROM trades t
    CROSS JOIN params p
    WHERE t.chat_id = p.chat_id
      AND t.won IS NOT NULL
      AND t.amount_ngn > 0
      AND (t.created_at AT TIME ZONE p.trading_timezone)::date
          BETWEEN p.start_date AND p.end_date
), daily AS (
    SELECT
        trade_date,
        strategy,
        asset,
        timeframe,
        COUNT(*) AS resolved_n,
        COUNT(*) FILTER (WHERE won = 1) AS wins,
        COUNT(*) FILTER (WHERE won = 0) AS losses,
        SUM(amount_ngn) AS deployed_ngn,
        SUM(stored_pnl_ngn) AS stored_pnl_ngn,
        SUM(binary_reconstructed_pnl_ngn) AS reconstructed_pnl_ngn,
        AVG(entry_price) AS avg_entry_price,
        AVG(certainty) AS avg_certainty,
        AVG(slippage_ngn) AS avg_slippage_ngn,
        AVG(CASE WHEN stored_pnl_ngn > 0 THEN stored_pnl_ngn END)
            AS avg_stored_win_ngn,
        AVG(CASE WHEN stored_pnl_ngn < 0 THEN ABS(stored_pnl_ngn) END)
            AS avg_stored_loss_ngn,
        MIN(resolved_date) AS first_resolution_date,
        MAX(resolved_date) AS last_resolution_date
    FROM resolved
    GROUP BY trade_date, strategy, asset, timeframe
), formatted AS (
    SELECT
        trade_date,
        strategy,
        asset,
        timeframe,
        resolved_n,
        wins,
        losses,
        ROUND((100.0 * wins / NULLIF(resolved_n, 0))::numeric, 2)
            AS win_rate_pct,
        ROUND(deployed_ngn, 2) AS deployed_ngn,
        ROUND(stored_pnl_ngn, 2) AS stored_pnl_ngn,
        ROUND((100.0 * stored_pnl_ngn / NULLIF(deployed_ngn, 0))::numeric, 2)
            AS stored_roi_pct,
        ROUND(reconstructed_pnl_ngn, 2) AS binary_reconstructed_pnl_ngn,
        ROUND(avg_entry_price, 4) AS avg_entry_price,
        ROUND(avg_certainty, 4) AS avg_certainty,
        ROUND(avg_slippage_ngn, 2) AS avg_slippage_ngn,
        ROUND(avg_stored_win_ngn, 2) AS avg_stored_win_ngn,
        ROUND(avg_stored_loss_ngn, 2) AS avg_stored_loss_ngn,
        first_resolution_date,
        last_resolution_date
    FROM daily
)
SELECT
    *,
    ROUND(
        SUM(stored_pnl_ngn) OVER (
            PARTITION BY strategy, asset, timeframe
            ORDER BY trade_date
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ),
        2
    ) AS cumulative_stored_pnl_ngn
FROM formatted
ORDER BY trade_date DESC, stored_pnl_ngn DESC, strategy, asset, timeframe;
