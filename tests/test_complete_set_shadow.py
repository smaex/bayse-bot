import asyncio

import pytest

from complete_set_shadow import (
    clob_buy_cost_per_net_share,
    clob_sell_proceeds_per_share,
    evaluate_books,
    scan_markets,
)


def _book(*, bids, asks):
    return {
        "timestamp": "2026-09-10T12:00:00Z",
        "bids": [
            {"price": price, "quantity": quantity, "total": price * quantity}
            for price, quantity in bids
        ],
        "asks": [
            {"price": price, "quantity": quantity, "total": price * quantity}
            for price, quantity in asks
        ],
    }


def test_complete_set_buy_burn_detects_only_fee_adjusted_edge():
    snapshot = evaluate_books(
        market_id="m", asset="BTC", timeframe="15min",
        yes_book=_book(bids=[(0.45, 10)], asks=[(0.47, 8)]),
        no_book=_book(bids=[(0.45, 10)], asks=[(0.47, 5)]),
        fee_rate=0.0,
        min_edge=0.02,
    )

    assert snapshot is not None
    assert snapshot.buy_burn_edge == pytest.approx(0.06)
    assert snapshot.buy_burn_pair_quantity == 5
    assert snapshot.buy_burn_opportunity is True
    assert snapshot.mint_sell_opportunity is False


def test_complete_set_mint_sell_detects_net_bid_surplus():
    snapshot = evaluate_books(
        market_id="m", asset="SOL", timeframe="15min",
        yes_book=_book(bids=[(0.52, 7)], asks=[(0.55, 8)]),
        no_book=_book(bids=[(0.51, 4)], asks=[(0.54, 5)]),
        fee_rate=0.0,
        min_edge=0.02,
    )

    assert snapshot is not None
    assert snapshot.mint_sell_edge == pytest.approx(0.03)
    assert snapshot.mint_sell_pair_quantity == 4
    assert snapshot.mint_sell_opportunity is True


def test_clob_fee_semantics_match_bayse_share_and_proceeds_rules():
    # feeRate=5%, p>0.5 -> fee fraction=2.5% of notional.
    assert clob_buy_cost_per_net_share(0.65, 0.05) == pytest.approx(
        0.65 / 0.975
    )
    assert clob_sell_proceeds_per_share(0.65, 0.05) == pytest.approx(
        0.65 * 0.975
    )


def test_apparent_sub_one_pair_is_rejected_after_taker_fees():
    snapshot = evaluate_books(
        market_id="m", asset="BTC", timeframe="15min",
        yes_book=_book(bids=[(0.48, 10)], asks=[(0.49, 10)]),
        no_book=_book(bids=[(0.48, 10)], asks=[(0.49, 10)]),
        fee_rate=0.10,
        min_edge=0.0,
    )

    assert snapshot is not None
    assert snapshot.buy_burn_edge < 0
    assert snapshot.buy_burn_opportunity is False


class FakeBookClient:
    def __init__(self):
        self.calls = []

    async def get_orderbook(self, outcome_id, depth=5):
        self.calls.append((outcome_id, depth))
        return _book(bids=[(0.45, 10)], asks=[(0.47, 10)])

    async def place_order(self, **_kwargs):
        raise AssertionError("shadow scanner must never place orders")


def test_shadow_scanner_is_read_only(monkeypatch):
    import complete_set_shadow

    monkeypatch.setattr(complete_set_shadow, "_append", lambda _snapshot: None)
    monkeypatch.setattr(complete_set_shadow, "_last_scan_by_market", {})
    client = FakeBookClient()
    markets = [{
        "market_id": "m",
        "asset": "BTC",
        "timeframe": "15min",
        "engine": "CLOB",
        "status": "open",
        "yes_id": "yes",
        "no_id": "no",
        "fee_rate": 0.0,
    }]

    snapshots = asyncio.run(scan_markets(client, markets))

    assert len(snapshots) == 1
    assert client.calls == [("yes", 5), ("no", 5)]
