# "5 orders placed, 0 fills, 19 hours dark": MAKER quotes that could not fill

Date: 2026-09-25
Scope: MAKER quoting and execution, the trading-stall report and `/why`, and Telegram trade notifications.
Constraint honoured throughout: no risk gate was loosened and no ceiling was raised. The MAKER 0.58 ceiling is unchanged; it is now explicit (`MAKER_MAX_BID`) and enforced against the live book. The only config value changed is the SNIPE max price, which the operator chose to restore from 0.70 to 0.65 (see below).

## What the operator saw

After `/resume`, Telegram showed a sequence of stall reports with codes `NO_EDGE`, then `NO_CONFIRMED_FILL`, then `COOLDOWN_BLOCKED`, all at about 1,124–1,155 minutes since the last confirmed fill. Interleaved with those were four placement messages, each ₦100 and each at **exactly 0.580**:

```
📊 [MAKER] BTC 15min ⬆️ YES ₦100 @ 0.580 (Cert: 100%)
📊 [MAKER] SOL 15min ⬇️ NO  ₦100 @ 0.580 (Cert: …)   ×2
📊 [MAKER] BTC 15min ⬇️ NO  ₦100 @ 0.580 (Cert: …)
```

The report counted "5 orders placed | 0 confirmed fills | 5 still resting unfilled". Its top "gates" were `scope:blockedbypolicy`, `MAKER:enginenotclob` and `SNIPE:assetnotinallowedscope`, with the underscores eaten by Telegram.

## Root cause: the MAKER bid is 0.58 whatever the book looks like

`strategies/maker.py` builds its quote as

```
competitive_bid = max(market_bid + 0.01, fv − 0.05, 0.52)
our_bid         = min(fv − 0.025, competitive_bid)   → clamped to [0.50, 0.58]
```

A side is only chosen when `fv ≥ 0.62`, so `fv − 0.025 ≥ 0.595` and `fv − 0.05 ≥ 0.57`. After the clamp the bid is **0.571–0.580, and exactly 0.580 whenever fv ≥ 0.63**. Running the real strategy over a grid of market prices shows it:

| spot vs strike | side mid (`yes_price`) | MAKER bid | mid − bid |
|---|---|---|---|
| +0.5% | 0.55 | 0.580 | −0.030 (bid above mid: crosses the ask) |
| +0.5% | 0.60 | 0.580 | +0.020 |
| +0.5% | 0.65 | 0.580 | +0.070 |
| +0.8% | 0.70 | 0.580 | +0.120 |
| +0.8% | 0.75 | 0.580 | +0.170 |

`market_bid` is not a bid. It is Bayse's `outcome1Price`/`outcome2Price`, which the API documents as the "current probability price" (a mid). Bayse's `postOnly` flag rejects an order "instead of crossing the spread". A post-only bid of 0.58 therefore ends up in one of two dead zones:

* **Chosen side trades at about 0.62 or higher (the usual case when fv ≥ 0.62).** The bid sits 4–17 ticks under the best bid. A seller hits every bid above it first, so it fills only if the whole bid stack down to 0.58 is swept. That is an adverse-selection fill if it ever happens. Otherwise the 60 s `MAKER_ORDER_TIMEOUT` cancels it, and a minute later the next signal places the same quote again.
* **Chosen side trades below 0.58.** The bid is at or through the ask, and the exchange rejects the post-only order.

Only a narrow band around 0.58–0.60 gives a competitive quote. MAKER's own docstring says it places orders "slightly better than the current best bid" and lists `client.get_orderbook` as verified. The code never called it; the taker path already reads the book for exactly this reason ("midpoint bidding … causes 100% zero-fill").

Timeline (a correlation, not proof): `dd1e830` restored the 0.58-capped quoting block on 2026-09-24 at 16:39 UTC. The last confirmed fill was about 18.7 h before the first report, which is shortly after that.

### Fix: price the post-only bid against the live book (`executor._maker_quote_against_book`)

Before sending a MAKER order, the executor now reads the chosen outcome's book and:

* **fails closed** without a fresh book (`maker_book_unavailable` / `maker_book_stale`), the same rule as every other entry;
* **steps inside the ask** when the intended bid would cross (`best_ask − 0.01`). This is a *lower* price and the top of the book. If no passive price ≥ 0.50 exists, it skips with `maker_would_cross_book`;
* **skips a buried quote** when the bid would sit more than `MAKER_MAX_TICKS_BEHIND_BEST_BID` (1) tick under the best bid. The skip is recorded as `maker_quote_behind_book`, with the best bid, best ask and tick distance in the detail;
* **never pays more than the strategy's bid.** A property test over 2,000 random books pins this.

A skip stamps the per-strategy market cooldown, so the book is read at most once per market per 60 s rather than on every 5 s signal. The 0.58 ceiling is now `config.MAKER_MAX_BID`, env-overridable within 0.50–0.75, with a startup error outside that band. The default is unchanged.

**What to expect after deploy.** When the chosen side trades above about 0.59, MAKER will *not* place an order. Instead of a `📊 … @ 0.580` message followed by an unfilled notice, `/why` reports `MAKER_QUOTE_UNCOMPETITIVE`, for example:

> exec:maker_quote_behind_book ×37 — latest: max bid 0.580 is 8 tick(s) under the best bid 0.660 / best ask 0.690 …

That is the ceiling doing its job. Whether MAKER *should* trade those markets is a risk/reward question, and it needs fill evidence, not this fix. For a fee-free maker fill at price `p`, a win pays `(1 − p)/p` and break-even accuracy is `p`:

| MAKER_MAX_BID | win pays | break-even win rate |
|---|---|---|
| 0.58 (default) | +72% | 58% |
| 0.65 | +54% | 65% |
| 0.70 | +43% | 70% |

MAKER only quotes when its model says fv ≥ 0.62, and it bids at most fv − 0.025. Raising the ceiling therefore spends most of the model's claimed edge on price, which is safe only if that fv is calibrated.

## Reporting defects in the same paste (all fixed)

| What the report said | What was true | Fix |
|---|---|---|
| "Trading stall — 1124 min **without an order**" | The clock measures time since the last *confirmed fill*; 5 orders had been placed | The header now reads "min without a confirmed fill" (the watchdog start-up log line too) |
| "5 still resting unfilled" | A lifetime count of passive placements that never decrements; the quotes had expired | The live count comes from the risk book: "N orders placed (P as passive quotes) … R resting now" |
| Verdict flipped `NO_CONFIRMED_FILL` → `COOLDOWN_BLOCKED` | One MAKER re-quote hit the 60 s cooldown. `COOLDOWN_BLOCKED` was checked first, on a lifetime counter, so it masked "zero fills" permanently (reproduced: still `COOLDOWN_BLOCKED` 6 h later) | `NO_CONFIRMED_FILL` now outranks the cooldown. Cooldown and exposure-cap verdicts need a hit within `RECENT_WINDOW_SEC` (15 min). The cooldown is never listed as an execution cause, because every placement and skip stamps it |
| Top gates: `scope:blocked_by_policy` ×469, `MAKER:engine_not_clob` ×306, `SNIPE:asset_not_in_allowed_scope` ×306 | These are configuration exclusions recorded on every pass, not candidate gates. The identical 306/306 counts most likely come from one AMM market outside SNIPE's asset scope (e.g. an XAUUSD series in the saved assets) being evaluated each pass; this is inferred, not verified against live data | Listed on an "⚙️ Excluded by configuration" line with their detail. They remain the fallback when nothing else was recorded. `scope:no_enabled_strategies` stays a gate because it stops everything |
| `blockedbypolicy`, `MAKERORDERTIMEOUT`, `cloblimitresting` | Legacy Markdown ate the underscores as italics; an odd count makes Telegram reject the whole alert, which was only logged | `stall.format_report(markdown=True)` escapes free text. `send_message` and `/why` retry as plain text on a parse error |
| "N evaluated per cycle" | Last pass only; feed-triggered passes cover one asset | Relabelled "evaluated in the last pass" |
| `📊 [MAKER] … (Cert: 100%)` one-liners | `notify_trade` escaped `\_` inside an `_italic_` entity (legacy Markdown forbids escaping inside entities), so every MAKER message failed to parse and fell back. 100% came from the 0.95 MAKER cap × the 1.2 TREND multiplier, clamped to 1.0 | The reason is rendered in a code span, and the message says "resting post-only bid — not filled yet". Post-multiplier certainty is capped at 0.99; every sizing and admission threshold is lower, so no decision changes. `notify_order_rejected` had the same italic bug and is fixed too |

## Latent position-safety bugs found on the way (fixed)

1. **A MAKER quote could erase a filled SNIPE position.** `risk.already_in` allows a MAKER quote and a SNIPE position on the same market. The MAKER path then called `risk.add_position(sig.market_id, …)`, and positions are keyed by market id, so the filled SNIPE entry was overwritten. It dropped out of exit management and would be deleted when the quote expired. MAKER now uses the same collision-free `market:outcome:order` key as the taker fill path.
2. **Opposite sides of one market were allowed.** The comment in `already_in` says MAKER and SNIPE may share a market "only if they are on the SAME outcome side", but the side was never checked. On opposite sides of one binary exactly one leg can pay out, so the two strategies would be betting against each other, and the pair loses outright whenever the two entry prices sum to more than 1.00. The side is now checked, and an unknown side is treated as a conflict. `already_in` also now sees compound-keyed entries. The dual-leg MIDMARKET_MAKER `market_YES`/`market_NO` keys keep their existing behaviour.

## SNIPE max price restored to 0.65 (operator decision)

* `tests/test_snipe_hardening.py::test_snipe_scope_defaults_match_production_evidence` asserts `SNIPE_MAX_MARKET_PRICE == 0.65`, a value guarded since 2026-09-10. It came from the production audit: entries ≥ 0.80 were the −₦314 bucket, and at 0.85 a 93%+ win rate is needed just to break even after fees.
* PR #14 raised the config to 0.70 ("to capture liquid high-EV entries") without new out-of-sample evidence, and left this test failing on `main`.
* The operator reports that SNIPE entered no trades at the 0.70 cap, so there is no evidence for it. On 2026-09-25 the operator chose to restore 0.65 and keep the test as it is. The config is back at 0.65, the audit rationale is restored next to it, and the suite is green again.
* PR #14 also lowered `SNIPE_MIN_ENTRY_PRICE` (0.40 → 0.35) and `SNIPE_MIN_RAW_MODEL_EDGE` (0.06 → 0.035). Those are not changed here.

## Deliberately not changed

* The 0.58 MAKER ceiling, the `fv ≥ 0.62` / 2-cent edge entry rule, and every other SNIPE gate.

## Verification

* `tests/test_maker_book_quoting.py` (23 tests) covers:
  * book pricing: buried, crossing, no passive price, competitive, empty and malformed books, plus the 2,000-book never-pay-more property;
  * executor integration through `_execute_logic`: no buried quote is placed or notified, crossing quotes are re-priced end to end (order, DB row, notification), and the executor fails closed on a missing, timed-out or stale book;
  * the SNIPE-overwrite regression and the same-market side conflicts;
  * `MAKER_MAX_BID` wiring.
* `tests/test_stall_report_accuracy.py` (19 tests) covers:
  * a replay of the pasted counters: the verdict is `NO_CONFIRMED_FILL` with "0 resting on the book now", and structural codes are kept out of the gates;
  * a legacy-Markdown checker implementing Telegram's rules, which confirms the old notification is invalid and the new report, watchdog alert, trade and rejection notices are valid;
  * the plain-text retry, the `MAKER_QUOTE_UNCOMPETITIVE` verdict, the recency window, and the certainty cap.
* Mutation check: against the pre-fix code, 14 of the 23 book-quoting tests and 16 of the 19 report tests fail. The rest are "unchanged behaviour" controls.
* Full suite: `182 passed`. Before the SNIPE restore it was `181 passed, 1 failed`; the failure was the SNIPE config test above, which fails identically on `main`.
