import asyncio

from client import BayseClient


class CapturingClient(BayseClient):
    def __init__(self):
        super().__init__("public", "secret")
        self.writes = []
        self.public_posts = []

    async def _post(self, path, body, extra_headers=None):
        self.writes.append((path, body, extra_headers))
        return {"ok": True}

    async def _public_post(self, path, body):
        self.public_posts.append((path, body))
        return {"completeFill": True}


def test_market_order_matches_documented_amm_shape():
    client = CapturingClient()
    asyncio.run(client.place_order(
        "event", "market", "yes", "BUY", 100,
        order_type="MARKET", max_slippage=0.01,
        currency="NGN", time_in_force="FAK",
    ))

    path, body, _ = client.writes[0]
    assert path == "/v1/pm/events/event/markets/market/orders"
    assert body == {
        "outcomeId": "yes",
        "side": "BUY",
        "amount": 100,
        "currency": "NGN",
        "type": "MARKET",
        "maxSlippage": 0.01,
        "timeInForce": "FAK",
    }


def test_post_only_limit_order_matches_documented_clob_shape():
    client = CapturingClient()
    asyncio.run(client.place_order(
        "event", "market", "yes", "BUY", 100,
        order_type="LIMIT", price=0.5714, currency="NGN",
        time_in_force="GTC", post_only=True, stp_mode="CANCEL_OLDEST",
    ))

    _, body, _ = client.writes[0]
    assert body["price"] == 0.571
    assert body["timeInForce"] == "GTC"
    assert body["postOnly"] is True
    assert body["stpMode"] == "CANCEL_OLDEST"
    assert "maxSlippage" not in body


def test_sell_amount_remains_desired_wallet_proceeds():
    client = CapturingClient()
    asyncio.run(client.place_order(
        "event", "market", "yes", "SELL", 497.5,
        order_type="MARKET", currency="NGN",
    ))

    assert client.writes[0][1]["amount"] == 497.5
    assert client.writes[0][1]["side"] == "SELL"


def test_quote_uses_public_non_mutating_endpoint():
    client = CapturingClient()
    asyncio.run(client.get_quote(
        "event", "market", "yes", "BUY", 100, "NGN"
    ))

    assert client.writes == []
    assert client.public_posts == [(
        "/v1/pm/events/event/markets/market/quote",
        {
            "outcomeId": "yes", "side": "BUY",
            "amount": 100, "currency": "NGN",
        },
    )]


def test_mint_and_burn_request_quantity_is_wallet_amount():
    client = CapturingClient()
    asyncio.run(client.mint_shares("market", 100, "NGN"))
    asyncio.run(client.burn_shares("market", 100, "NGN"))

    assert client.writes[0][0] == "/v1/pm/markets/market/mint"
    assert client.writes[0][1] == {"quantity": 100, "currency": "NGN"}
    assert client.writes[1][0] == "/v1/pm/markets/market/burn"
    assert client.writes[1][1] == {"quantity": 100, "currency": "NGN"}


def test_batch_orders_use_idempotency_but_are_not_assumed_atomic():
    client = CapturingClient()
    orders = [{"outcomeId": "yes", "side": "BUY", "amount": 100}]
    asyncio.run(client.place_batch_orders(orders, "cycle-123"))

    path, body, headers = client.writes[0]
    assert path == "/v1/pm/orders/batch"
    assert body == {"orders": orders}
    assert headers == {"Idempotency-Key": "cycle-123"}
