from types import SimpleNamespace

import pytest

import config
import maker_shadow


def _signal(*, market="m", outcome="YES", fv=0.789, cap=0.58):
    return SimpleNamespace(
        strategy="MAKER",
        market_id=market,
        asset="SOL",
        timeframe="15min",
        outcome=outcome,
        outcome_id=f"{market}-{outcome.lower()}",
        win_prob=fv,
        market_price=cap,
    )


def _book(bids, asks):
    return {
        "bids": [{"price": price, "quantity": 500} for price in bids],
        "asks": [{"price": price, "quantity": 500} for price in asks],
    }


def _level(snapshot, cap):
    return next(row for row in snapshot["levels"] if row["cap"] == pytest.approx(cap))


def test_shadow_ladder_shows_why_raising_cap_would_not_reach_this_book():
    snapshot = maker_shadow.build_snapshot(
        _signal(fv=0.789), _book([0.92], [0.94]), now=1_800_000_000
    )

    assert snapshot["best_bid"] == pytest.approx(0.92)
    assert snapshot["best_ask"] == pytest.approx(0.94)
    current = _level(snapshot, config.MAKER_MAX_BID)
    highest = _level(snapshot, 0.74)
    assert current["edge_ok"] is True
    assert current["book_competitive"] is False
    assert current["both_ok"] is False
    assert highest["edge_ok"] is True
    assert highest["book_competitive"] is False
    assert highest["both_ok"] is False
    assert snapshot["interpretation"] == "snapshot_only_no_fill_or_settlement_observed"


def test_shadow_ladder_finds_prices_that_are_model_and_book_eligible():
    snapshot = maker_shadow.build_snapshot(
        _signal(fv=0.789), _book([0.66], [0.69]), now=1_800_000_000
    )

    at_58 = _level(snapshot, 0.58)
    at_66 = _level(snapshot, 0.66)
    assert at_58["edge_ok"] is True
    assert at_58["book_competitive"] is False
    assert at_66["edge_ok"] is True
    assert at_66["book_competitive"] is True
    assert at_66["both_ok"] is True
    assert at_66["quote_price"] == pytest.approx(0.66)


def test_shadow_does_not_call_a_competitive_but_negative_edge_quote_viable():
    snapshot = maker_shadow.build_snapshot(
        _signal(fv=0.633), _book([0.66], [0.75]), now=1_800_000_000
    )
    at_66 = _level(snapshot, 0.66)

    assert at_66["book_competitive"] is True
    assert at_66["edge_ok"] is False
    assert at_66["both_ok"] is False


def test_shadow_prices_inside_the_ask_and_never_cross():
    snapshot = maker_shadow.build_snapshot(
        _signal(fv=0.80), _book([0.66], [0.69]), now=1_800_000_000
    )
    at_70 = _level(snapshot, 0.70)

    assert at_70["quote_price"] == pytest.approx(0.68)
    assert at_70["quote_price"] < 0.69
    assert at_70["edge_ok"] is True
    assert at_70["book_competitive"] is True


def test_shadow_recorder_deduplicates_and_report_disclaims_fills(tmp_path, monkeypatch):
    log_file = tmp_path / "maker_shadow.jsonl"
    monkeypatch.setattr(maker_shadow, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(maker_shadow, "_LOG_FILE", str(log_file))
    monkeypatch.setattr(maker_shadow, "_last_recorded", {})
    sig = _signal()
    book = _book([0.92], [0.94])

    assert maker_shadow.record_candidate(sig, book, now=1_800_000_000) is True
    assert maker_shadow.record_candidate(sig, book, now=1_800_000_030) is False
    assert maker_shadow.record_candidate(
        _signal(outcome="NO"), book, now=1_800_000_030
    ) is True
    assert len(log_file.read_text().splitlines()) == 2

    report = maker_shadow.get_summary_report()
    assert "Snapshots: *2* distinct skipped signal/book observations" in report
    assert "does not infer queue position, fills, settlement, or realized profit" in report
    assert "0.58" in report and "configured" in report


def test_shadow_ignores_invalid_or_non_maker_signals():
    assert maker_shadow.build_snapshot(
        _signal(fv=float("nan")), _book([0.6], [0.7])
    ) is None
    assert maker_shadow.build_snapshot(
        SimpleNamespace(strategy="SNIPE"), _book([0.6], [0.7])
    ) is None
