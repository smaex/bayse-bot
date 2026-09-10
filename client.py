import hashlib
import hmac
import base64
import time
import json
import asyncio
import logging
import aiohttp
from typing import Optional
from config import (
    API_CONNECT_TIMEOUT_SEC,
    API_READ_RETRIES,
    API_REQUEST_TIMEOUT_SEC,
    BASE_URL,
    CURRENCY,
    READ_RATE_LIMIT,
    WRITE_RATE_LIMIT,
)

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
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                # One newly generated token is consumed by this request.
                self._last = time.monotonic()
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
            timeout = aiohttp.ClientTimeout(
                total=API_REQUEST_TIMEOUT_SEC,
                connect=API_CONNECT_TIMEOUT_SEC,
                sock_connect=API_CONNECT_TIMEOUT_SEC,
            )
            connector = aiohttp.TCPConnector(
                limit=50,
                ttl_dns_cache=300,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                headers={
                    "Content-Type": "application/json",
                    "User-Agent": "bayse-bot/production",
                },
                timeout=timeout,
                connector=connector,
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

    @staticmethod
    async def _retry_delay(response: aiohttp.ClientResponse, attempt: int) -> float:
        try:
            data = await response.json()
        except (aiohttp.ContentTypeError, json.JSONDecodeError):
            data = {}
        raw = data.get("retryAfter") or response.headers.get("Retry-After")
        try:
            return max(0.05, min(float(raw), 30.0))
        except (TypeError, ValueError):
            return min(2 ** attempt, 8.0)

    async def _get(self, path: str, params: dict = None, auth: str = "read") -> dict:
        session = await self._get_session()
        headers = self._read_headers() if auth == "read" else {}
        last_error: Exception | None = None
        for attempt in range(API_READ_RETRIES):
            await self._read_rl.acquire()
            try:
                async with session.get(f"{BASE_URL}{path}", params=params, headers=headers) as r:
                    if r.status == 429:
                        await asyncio.sleep(await self._retry_delay(r, attempt))
                        continue
                    if 500 <= r.status < 600 and attempt + 1 < API_READ_RETRIES:
                        await r.read()
                        await asyncio.sleep(min(0.25 * (2 ** attempt), 2.0))
                        continue
                    r.raise_for_status()
                    return await r.json()
            except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt + 1 >= API_READ_RETRIES:
                    break
                await asyncio.sleep(min(0.25 * (2 ** attempt), 2.0))
        raise RuntimeError(f"GET {path} failed after {API_READ_RETRIES} attempts: {last_error}")

    async def _post(self, path: str, body: dict, extra_headers: dict | None = None) -> dict:
        """Send a signed write.

        Network/5xx failures are deliberately *not* retried here because a
        single-order write has no documented idempotency key. Retrying an
        ambiguous order response can double the position. A 429 is safe to
        retry because Bayse rejects it before execution.
        """
        session  = await self._get_session()
        body_str = json.dumps(body, separators=(",", ":"))
        for attempt in range(API_READ_RETRIES):
            await self._write_rl.acquire()
            # Regenerate timestamp/signature after every rate-limit wait.
            headers = self._auth_headers("POST", path, body_str)
            if extra_headers:
                headers.update(extra_headers)
            async with session.post(f"{BASE_URL}{path}", data=body_str, headers=headers) as r:
                if r.status == 429:
                    await asyncio.sleep(await self._retry_delay(r, attempt))
                    continue
                if r.status >= 400:
                    try:
                        err = await r.json()
                    except (aiohttp.ContentTypeError, json.JSONDecodeError):
                        err = {"message": await r.text()}
                    msg = err.get("message") or err.get("error") or str(err)
                    raise ValueError(f"API {r.status} {path}: {msg}")
                return await r.json()
        raise RuntimeError(f"POST {path} remained rate-limited after retries")

    async def _public_post(self, path: str, body: dict) -> dict:
        """Retry-safe public POST used for quote calculation (no mutation)."""
        session = await self._get_session()
        last_error: Exception | None = None
        for attempt in range(API_READ_RETRIES):
            await self._read_rl.acquire()
            try:
                async with session.post(
                    f"{BASE_URL}{path}", json=body, headers=self._read_headers()
                ) as r:
                    if r.status == 429:
                        await asyncio.sleep(await self._retry_delay(r, attempt))
                        continue
                    if 500 <= r.status < 600 and attempt + 1 < API_READ_RETRIES:
                        await r.read()
                        await asyncio.sleep(min(0.25 * (2 ** attempt), 2.0))
                        continue
                    r.raise_for_status()
                    return await r.json()
            except (aiohttp.ClientConnectionError, aiohttp.ServerTimeoutError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt + 1 >= API_READ_RETRIES:
                    break
                await asyncio.sleep(min(0.25 * (2 ** attempt), 2.0))
        raise RuntimeError(f"QUOTE {path} failed after retries: {last_error}")

    async def _delete(self, path: str) -> dict:
        session = await self._get_session()
        for attempt in range(API_READ_RETRIES):
            await self._write_rl.acquire()
            headers = self._auth_headers("DELETE", path)
            async with session.delete(f"{BASE_URL}{path}", headers=headers) as r:
                if r.status == 429:
                    await asyncio.sleep(await self._retry_delay(r, attempt))
                    continue
                r.raise_for_status()
                if r.status == 204:
                    return {}
                return await r.json()
        raise RuntimeError(f"DELETE {path} remained rate-limited after retries")

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
        return await self._public_post(
            f"/v1/pm/events/{event_id}/markets/{market_id}/quote",
            {"outcomeId": outcome_id, "side": side, "amount": amount, "currency": currency},
        )

    async def place_order(self, event_id: str, market_id: str, outcome_id: str,
                          side: str, amount: float, order_type: str = "MARKET",
                          price: float = None, currency: str = CURRENCY,
                          max_slippage: float = 0.05,
                          time_in_force: str = "FAK",
                          post_only: bool = False,
                          stp_mode: str = "SKIP") -> dict:
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
            body["postOnly"]    = bool(post_only)
            body["stpMode"]     = stp_mode
        else:
            body["maxSlippage"] = max_slippage
            body["timeInForce"] = time_in_force
        return await self._post(
            f"/v1/pm/events/{event_id}/markets/{market_id}/orders", body
        )

    async def place_batch_orders(self, orders: list[dict], idempotency_key: str) -> dict:
        """Place a CLOB batch with Bayse's documented 24-hour idempotency."""
        if not orders or len(orders) > 20:
            raise ValueError("orders must contain between 1 and 20 items")
        return await self._post(
            "/v1/pm/orders/batch",
            {"orders": orders},
            extra_headers={"Idempotency-Key": idempotency_key},
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

    async def get_position(self, outcome_id: str) -> dict | None:
        portfolio = await self.get_portfolio()
        rows = portfolio.get("outcomeBalances", []) if isinstance(portfolio, dict) else []
        return next((row for row in rows if row.get("outcomeId") == outcome_id), None)

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
        # CLOB's `size`/`amount` are requested quantities, not fills. Prefer
        # explicit fill fields and never infer a fill for an open/cancelled GTC
        # order. AMM confirmations use `quantity` and status=filled.
        for field in ("filledSize", "sharesFilled", "sharesMatched",
                      "amountMatched", "filledQuantity"):
            v = order.get(field)
            if v is not None:
                try:
                    return max(0.0, float(v))
                except (TypeError, ValueError):
                    continue

        status = str(order.get("status") or "").strip().lower()
        if status in {
            "pending", "open", "new", "cancelled", "canceled", "killed",
            "rejected", "expired",
        }:
            return 0.0

        for field in ("quantity", "shares"):
            v = order.get(field)
            if v is not None:
                try:
                    return max(0.0, float(v))
                except (TypeError, ValueError):
                    continue
        return 0.0
