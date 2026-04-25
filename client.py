import hashlib
import hmac
import base64
import time
import json
import asyncio
import aiohttp
from typing import Optional
from config import BASE_URL, WRITE_RATE_LIMIT, READ_RATE_LIMIT


class RateLimiter:
    def __init__(self, rate: int):
        self._rate = rate
        self._tokens = rate
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._rate, self._tokens + elapsed * self._rate)
            self._last = now
            if self._tokens < 1:
                await asyncio.sleep((1 - self._tokens) / self._rate)
                self._tokens = 0
            else:
                self._tokens -= 1


class BayseClient:
    def __init__(self, public_key: str, secret_key: str):
        self.public_key = public_key
        self.secret_key = secret_key
        self._session: Optional[aiohttp.ClientSession] = None
        self._write_rl = RateLimiter(WRITE_RATE_LIMIT)
        self._read_rl = RateLimiter(READ_RATE_LIMIT)

    async def _session_get(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Content-Type": "application/json"}
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    def _sign(self, timestamp: int, method: str, path: str, body_str: Optional[str]) -> str:
        body_hash = hashlib.sha256((body_str or "").encode()).hexdigest()
        payload = f"{timestamp}.{method}.{path}.{body_hash}"
        sig = hmac.new(self.secret_key.encode(), payload.encode(), hashlib.sha256).digest()
        return base64.b64encode(sig).decode()

    def _write_headers(self, method: str, path: str, body_str: Optional[str]) -> dict:
        ts = int(time.time())
        return {
            "X-Public-Key": self.public_key,
            "X-Timestamp": str(ts),
            "X-Signature": self._sign(ts, method, path, body_str),
            "Content-Type": "application/json",
        }

    def _read_headers(self) -> dict:
        return {"X-Public-Key": self.public_key}

    async def _get(self, path: str, params: dict = None, auth: str = "read") -> dict:
        await self._read_rl.acquire()
        session = await self._session_get()
        headers = self._read_headers() if auth == "read" else {}
        for attempt in range(3):
            async with session.get(f"{BASE_URL}{path}", params=params, headers=headers) as r:
                if r.status == 429:
                    data = await r.json()
                    wait = data.get("retryAfter", 2 ** attempt)
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return await r.json()
        raise RuntimeError(f"GET {path} failed after retries")

    async def _post(self, path: str, body: dict) -> dict:
        await self._write_rl.acquire()
        session = await self._session_get()
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._write_headers("POST", path, body_str)
        for attempt in range(3):
            async with session.post(f"{BASE_URL}{path}", data=body_str, headers=headers) as r:
                if r.status == 429:
                    data = await r.json()
                    wait = data.get("retryAfter", 2 ** attempt)
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return await r.json()
        raise RuntimeError(f"POST {path} failed after retries")

    async def _delete(self, path: str) -> dict:
        await self._write_rl.acquire()
        session = await self._session_get()
        headers = self._write_headers("DELETE", path, None)
        for attempt in range(3):
            async with session.delete(f"{BASE_URL}{path}", headers=headers) as r:
                if r.status == 429:
                    data = await r.json()
                    wait = data.get("retryAfter", 2 ** attempt)
                    await asyncio.sleep(wait)
                    continue
                r.raise_for_status()
                return await r.json()
        raise RuntimeError(f"DELETE {path} failed after retries")

    # ── Market Data (public) ─────────────────────────────────────────────────

    async def list_events(self, page: int = 1, limit: int = 50) -> dict:
        return await self._get("/v1/pm/events", params={"page": page, "limit": limit}, auth="public")

    async def get_event(self, event_id: str) -> dict:
        return await self._get(f"/v1/pm/events/{event_id}", auth="public")

    async def get_series_events(self, series_slug: str) -> list:
        data = await self._get(f"/v1/pm/events/series/{series_slug}/lean-events", auth="public")
        return data if isinstance(data, list) else data.get("events", [])

    async def get_quote(self, event_id: str, market_id: str, outcome_id: str,
                        side: str, amount: float, currency: str = "NGN") -> dict:
        return await self._post(
            f"/v1/pm/events/{event_id}/markets/{market_id}/quote",
            {"outcomeId": outcome_id, "side": side, "amount": amount, "currency": currency},
        )

    async def get_trades(self, market_id: str = None, limit: int = 50) -> dict:
        params = {"limit": limit}
        if market_id:
            params["marketId"] = market_id
        return await self._get("/v1/pm/trades", params=params, auth="public")

    async def get_price_history(self, event_id: str, resolution: str = "1h") -> list:
        data = await self._get(f"/v1/pm/events/{event_id}/price-history",
                               params={"resolution": resolution}, auth="public")
        return data if isinstance(data, list) else data.get("history", [])

    # ── Orders (write) ───────────────────────────────────────────────────────

    async def place_order(self, event_id: str, market_id: str, outcome_id: str,
                          side: str, amount: float, order_type: str = "MARKET",
                          price: float = None, currency: str = "NGN",
                          max_slippage: float = 0.05) -> dict:
        body = {
            "outcomeId": outcome_id,
            "side": side,
            "amount": amount,
            "currency": currency,
            "type": order_type,
        }
        if order_type == "LIMIT" and price is not None:
            body["price"] = price
            body["timeInForce"] = "GTC"
        else:
            body["maxSlippage"] = max_slippage
            body["timeInForce"] = "FAK"
        return await self._post(
            f"/v1/pm/events/{event_id}/markets/{market_id}/orders", body
        )

    async def cancel_order(self, order_id: str) -> dict:
        return await self._delete(f"/v1/pm/orders/{order_id}")

    async def list_orders(self, page: int = 1, limit: int = 50) -> dict:
        return await self._get("/v1/pm/orders", params={"page": page, "limit": limit})

    # ── Portfolio ─────────────────────────────────────────────────────────────

    async def get_portfolio(self) -> dict:
        return await self._get("/v1/pm/portfolio")

    async def get_pnl(self) -> dict:
        return await self._get("/v1/pm/pnl")

    # ── Wallet ────────────────────────────────────────────────────────────────

    async def get_wallet(self) -> dict:
        return await self._get("/v1/wallet/assets")

    async def get_balance_ngn(self) -> float:
        wallet = await self.get_wallet()
        for asset in wallet.get("assets", []):
            if asset.get("currency") == "NGN":
                return float(asset.get("available", 0))
        return 0.0

    async def get_pnl_summary(self) -> dict:
        """Returns realized PnL and summary stats."""
        return await self._get("/v1/pm/pnl")

    # ── Share Operations ──────────────────────────────────────────────────────

    async def burn_shares(self, market_id: str, quantity: float, currency: str = "NGN") -> dict:
        """Redeem YES+NO share pairs back to wallet — used for arb exit."""
        return await self._post(f"/v1/pm/markets/{market_id}/burn",
                                {"quantity": quantity, "currency": currency})

    async def mint_shares(self, market_id: str, quantity: float, currency: str = "NGN") -> dict:
        """Create YES+NO share pairs from wallet balance — used for arb entry."""
        return await self._post(f"/v1/pm/markets/{market_id}/mint",
                                {"quantity": quantity, "currency": currency})
