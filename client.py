import hashlib
import hmac
import base64
import time
import json
import asyncio
import logging
import aiohttp
from typing import Optional
from config import BASE_URL, WRITE_RATE_LIMIT, READ_RATE_LIMIT, CURRENCY

log = logging.getLogger(__name__)


class RateLimiter:
    def __init__(self, rate: int):
        self._rate  = rate
        self._tokens = rate
        self._last  = time.monotonic()
        self._lock  = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._rate, self._tokens + (now - self._last) * self._rate)
            self._last   = now
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
        self._read_rl  = RateLimiter(READ_RATE_LIMIT)

    async def _get_session(self) -> aiohttp.ClientSession:
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
        payload   = f"{timestamp}.{method}.{path}.{body_hash}"
        sig = hmac.new(self.secret_key.encode(), payload.encode(), hashlib.sha256).digest()
        return base64.b64encode(sig).decode()

    def _auth_headers(self, method: str, path: str, body_str: Optional[str] = None) -> dict:
        ts = int(time.time())
        return {
            "X-Public-Key": self.public_key,
            "X-Timestamp":  str(ts),
            "X-Signature":  self._sign(ts, method, path, body_str),
            "Content-Type": "application/json",
        }

    def _read_headers(self) -> dict:
        return {"X-Public-Key": self.public_key}

    async def _get(self, path: str, params: dict = None, auth: str = "read") -> dict:
        await self._read_rl.acquire()
        session = await self._get_session()
        headers = self._read_headers() if auth == "read" else {}
        for attempt in range(3):
            async with session.get(f"{BASE_URL}{path}", params=params, headers=headers) as r:
                if r.status == 429:
                    data = await r.json()
                    await asyncio.sleep(data.get("retryAfter", 2 ** attempt))
                    continue
                r.raise_for_status()
                return await r.json()
        raise RuntimeError(f"GET {path} failed after retries")

    async def _post(self, path: str, body: dict) -> dict:
        await self._write_rl.acquire()
        session  = await self._get_session()
        body_str = json.dumps(body, separators=(",", ":"))
        headers  = self._auth_headers("POST", path, body_str)
        for attempt in range(3):
            async with session.post(f"{BASE_URL}{path}", data=body_str, headers=headers) as r:
                if r.status == 429:
                    data = await r.json()
                    await asyncio.sleep(data.get("retryAfter", 2 ** attempt))
                    continue
                if r.status >= 400:
                    try:
                        err = await r.json()
                        msg = err.get("message") or err.get("error")
                        if msg:
                            raise ValueError(msg)
                    except ValueError:
                        raise
                    except Exception:
                        text = await r.text()
                        log.error(f"API {r.status} on {path}: {text}")
                r.raise_for_status()
                return await r.json()
        raise RuntimeError(f"POST {path} failed after retries")

    async def _delete(self, path: str) -> dict:
        await self._write_rl.acquire()
        session = await self._get_session()
        headers = self._auth_headers("DELETE", path)
        for attempt in range(3):
            async with session.delete(f"{BASE_URL}{path}", headers=headers) as r:
                if r.status == 429:
                    data = await r.json()
                    await asyncio.sleep(data.get("retryAfter", 2 ** attempt))
                    continue
                r.raise_for_status()
                return await r.json()
        raise RuntimeError(f"DELETE {path} failed after retries")

    # ── Market data ───────────────────────────────────────────────────────────

    async def list_events(self, page: int = 1, limit: int = 50) -> dict:
        return await self._get("/v1/pm/events", params={"page": page, "limit": limit}, auth="public")

    async def get_event(self, event_id: str, currency: str = CURRENCY) -> dict:
        """Always request in NGN so prices come back in the right currency."""
        return await self._get(
            f"/v1/pm/events/{event_id}",
            params={"currency": currency},
            auth="read",
        )

    async def get_series_events(self, series_slug: str) -> list:
        data = await self._get(f"/v1/pm/events/series/{series_slug}/lean-events", auth="public")
        return data if isinstance(data, list) else data.get("events", [])

    async def get_orderbook(self, outcome_id: str, depth: int = 5, currency: str = CURRENCY) -> dict:
        try:
            res = await self._get(
                "/v1/pm/books",
                params={"outcomeId[]": outcome_id, "depth": depth, "currency": currency},
                auth="public",
            )
            if isinstance(res, list) and len(res) > 0:
                return res[0]
            return res if isinstance(res, dict) else {}
        except Exception:
            return {}

    # ── Orders ────────────────────────────────────────────────────────────────

    async def get_quote(self, event_id: str, market_id: str, outcome_id: str,
                        side: str, amount: float, currency: str = CURRENCY) -> dict:
        return await self._post(
            f"/v1/pm/events/{event_id}/markets/{market_id}/quote",
            {"outcomeId": outcome_id, "side": side, "amount": amount, "currency": currency},
        )

    async def place_order(self, event_id: str, market_id: str, outcome_id: str,
                          side: str, amount: float, order_type: str = "MARKET",
                          price: float = None, currency: str = CURRENCY,
                          max_slippage: float = 0.05,
                          time_in_force: str = "FAK") -> dict:
        body: dict = {
            "outcomeId": outcome_id,
            "side":      side,
            "amount":    amount,
            "currency":  currency,
            "type":      order_type,
        }
        if order_type == "LIMIT" and price is not None:
            body["price"]       = round(price, 3)
            body["timeInForce"] = time_in_force
        else:
            body["maxSlippage"] = max_slippage
            body["timeInForce"] = time_in_force
        return await self._post(
            f"/v1/pm/events/{event_id}/markets/{market_id}/orders", body
        )

    async def cancel_order(self, order_id: str) -> dict:
        return await self._delete(f"/v1/pm/orders/{order_id}")

    async def list_orders(self, page: int = 1, limit: int = 50) -> dict:
        return await self._get("/v1/pm/orders", params={"page": page, "limit": limit})

    async def get_order(self, order_id: str) -> dict:
        return await self._get(f"/v1/pm/orders/{order_id}")

    # ── Portfolio / wallet ────────────────────────────────────────────────────

    async def get_wallet(self) -> dict:
        return await self._get("/v1/wallet/assets")

    async def get_balance_ngn(self) -> float:
        wallet = await self.get_wallet()
        assets = wallet if isinstance(wallet, list) else wallet.get("assets", [])
        for asset in assets:
            currency = (asset.get("currency") or asset.get("symbol") or "").upper()
            if currency == "NGN":
                # CRITICAL: only trust availableBalance/available — these are
                # the SAME concept (free, uncommitted cash), just possibly
                # different field names across API versions. balance/total
                # likely include funds locked in open positions, which is a
                # DIFFERENT quantity entirely. Mixing them caused repeated
                # false "deposit"/"withdrawal" detection: whenever
                # availableBalance was transiently absent (e.g. right after
                # placing an order, or simply because all cash was deployed
                # in open positions — this account regularly has 2-3 open
                # SNIPE positions), the old code fell through to
                # balance/total and returned a larger number that included
                # locked funds, registering as a fake deposit. The next
                # correct read then looked like a withdrawal, and the false
                # "deposit" had already inflated risk.peak_balance, causing
                # a false drawdown-stop on the very next real reading.
                for field in ("availableBalance", "available"):
                    v = asset.get(field)
                    if v is not None:
                        return float(v)
                # Field genuinely absent (not just zero) — only NOW fall
                # back to a different field, and log it clearly so this
                # is visible rather than silently trusting a possibly
                # wrong number.
                for field in ("balance", "total"):
                    v = asset.get(field)
                    if v is not None:
                        log.warning(
                            f"get_balance_ngn: availableBalance/available "
                            f"missing from API response, falling back to "
                            f"'{field}'={v} — this may include locked funds"
                        )
                        return float(v)
                return 0.0
        return 0.0

    async def get_pnl(self) -> dict:
        return await self._get("/v1/pm/pnl")

    async def get_portfolio(self) -> dict:
        return await self._get("/v1/pm/portfolio")

    # ── Share operations ──────────────────────────────────────────────────────

    async def burn_shares(self, market_id: str, quantity: float, currency: str = CURRENCY) -> dict:
        return await self._post(
            f"/v1/pm/markets/{market_id}/burn",
            {"quantity": quantity, "currency": currency},
        )

    async def mint_shares(self, market_id: str, quantity: float, currency: str = CURRENCY) -> dict:
        return await self._post(
            f"/v1/pm/markets/{market_id}/mint",
            {"quantity": quantity, "currency": currency},
        )

    # ── Helper ────────────────────────────────────────────────────────────────

    @staticmethod
    def parse_filled_shares(order: dict) -> float:
        """
        Extract filled quantity from an order response.
        AMM orders use 'quantity'; CLOB uses 'filledSize'/'sharesMatched'.
        Check AMM field first.
        """
        for field in ("quantity", "filledSize", "shares", "sharesFilled",
                      "sharesMatched", "amountMatched", "filledQuantity"):
            v = order.get(field)
            if v is not None:
                try:
                    val = float(v)
                    if val > 0:
                        return val
                except (TypeError, ValueError):
                    pass
        return 0.0
