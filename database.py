"""
PostgreSQL store (Supabase).
Per-user: API keys (AES-encrypted), settings, trade history.
Uses a standard ThreadedConnectionPool — Supabase supports many more
connections than CockroachDB free tier so no 5-connection gymnastics needed.
"""

import json
import logging
import os
import uuid
import threading
import time
import copy
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import psycopg2
import psycopg2.extras
import psycopg2.pool
from cryptography.fernet import Fernet

log = logging.getLogger(__name__)

DATABASE_URL = os.environ.get("DATABASE_URL", "")

DEFAULT_SETTINGS: dict = {
    "assets":           ["BTC", "ETH", "SOL"],
    "timeframes":       ["5min", "15min", "1h"],
    "strategies":       ["SNIPE", "ARB", "FRONTRUN", "CORRELATE"],
    "risk_pct":         2.0,
    "mintrade":         100,
    "maxtrade":         5_000,
    "maxexposure":      20.0,
    "daily_multiplier": 10,
    "daily_target_ngn": 0,
    "paused":           False,
    "learned":          {},
    "mode":             "balanced",
}

# ── Encryption ────────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    key = os.environ.get("ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY not set. Generate: "
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode())

def _enc(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()

def _dec(text: str) -> str:
    return _fernet().decrypt(text.encode()).decode()


# ── Connection pool ───────────────────────────────────────────────────────────
# Supabase is standard PostgreSQL — no connection-count anxiety.

_pool: psycopg2.pool.ThreadedConnectionPool | None = None

# Simple in-memory caches
_USER_CACHE: dict[str, dict] = {}
_ACTIVE_USERS_CACHE: list[dict] | None = None


def _init_pool():
    global _pool
    if _pool is not None:
        return
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set.")
    _pool = psycopg2.pool.ThreadedConnectionPool(minconn=2, maxconn=15, dsn=DATABASE_URL)
    log.info("Supabase connection pool ready (min=2, max=15)")


@contextmanager
def _cx():
    if _pool is None:
        _init_pool()
    conn = None
    try:
        conn = _pool.getconn()
        # Validate — Supabase drops idle connections
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            log.warning("Dead DB connection detected — reconnecting")
            _pool.putconn(conn, close=True)
            conn = _pool.getconn()
        yield conn
        conn.commit()
    except Exception:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            _pool.putconn(conn)


def check_connection() -> bool:
    try:
        with _cx():
            return True
    except Exception:
        return False


def _execute(query: str, params: tuple = ()):
    with _cx() as conn:
        with conn.cursor() as cur:
            cur.execute(query, params)


def _fetch_one(query: str, params: tuple = ()) -> dict | None:
    with _cx() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            row = cur.fetchone()
        return dict(row) if row else None


def _fetch_all(query: str, params: tuple = ()) -> list[dict]:
    with _cx() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
        return [dict(r) for r in rows]


# ── Schema setup ──────────────────────────────────────────────────────────────

def init_db():
    _init_pool()
    with _cx() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    chat_id    TEXT PRIMARY KEY,
                    pub_enc    TEXT NOT NULL,
                    sec_enc    TEXT NOT NULL,
                    settings   TEXT DEFAULT '{}',
                    is_active  INTEGER DEFAULT 1,
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS bot_lock (
                    lock_id    TEXT PRIMARY KEY,
                    process_id INTEGER NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            cur.execute(
                "INSERT INTO bot_lock (lock_id, process_id) VALUES ('MASTER', 0) ON CONFLICT DO NOTHING"
            )
            cur.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    trade_id              TEXT PRIMARY KEY,
                    chat_id               TEXT NOT NULL,
                    strategy              TEXT,
                    asset                 TEXT,
                    timeframe             TEXT,
                    outcome               TEXT,
                    outcome_id            TEXT,
                    market_id             TEXT,
                    event_id              TEXT,
                    order_id              TEXT,
                    entry_price           REAL,
                    amount_ngn            REAL,
                    certainty             REAL,
                    secs_to_close         REAL,
                    spot_vs_threshold_pct REAL,
                    momentum_at_entry     REAL,
                    regime_at_entry       REAL,
                    edge_at_entry         REAL,
                    realized_vol_at_entry REAL,
                    market_price_at_entry REAL,
                    slippage_ngn          REAL,
                    engine                TEXT,
                    won                   INTEGER,
                    pnl_ngn               REAL,
                    created_at            TIMESTAMPTZ DEFAULT NOW(),
                    resolved_at           TIMESTAMPTZ
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_user
                ON trades(chat_id, created_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS quant_state (
                    asset      TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS config (
                    key   TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
            """)
        conn.commit()
    log.info("Database ready (Supabase / PostgreSQL)")


# ── Singleton lock ────────────────────────────────────────────────────────────

def force_acquire_singleton_lock() -> bool:
    pid = os.getpid()
    try:
        with _cx() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO bot_lock (lock_id, process_id, updated_at)
                    VALUES ('MASTER', %s, NOW())
                    ON CONFLICT (lock_id) DO UPDATE
                        SET process_id = %s, updated_at = NOW()
                """, (pid, pid))
        return True
    except Exception as e:
        log.error(f"Error acquiring lock: {e}")
        return False


def heartbeat_singleton_lock() -> bool:
    pid = os.getpid()
    try:
        with _cx() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    UPDATE bot_lock SET updated_at = NOW()
                    WHERE lock_id = 'MASTER' AND process_id = %s
                    RETURNING process_id
                """, (pid,))
                row = cur.fetchone()
                return bool(row)
    except Exception as e:
        log.error(f"Heartbeat error: {e}")
        return True  # don't self-terminate on transient DB error


def release_singleton_lock() -> bool:
    try:
        with _cx() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM bot_lock WHERE lock_id = 'MASTER'")
            conn.commit()
            return True
    except Exception:
        return False


# ── Users ─────────────────────────────────────────────────────────────────────

def add_user(chat_id: str, public_key: str, secret_key: str) -> dict:
    global _ACTIVE_USERS_CACHE
    _execute(
        """
        INSERT INTO users (chat_id, pub_enc, sec_enc, settings)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (chat_id) DO UPDATE
            SET pub_enc = EXCLUDED.pub_enc,
                sec_enc = EXCLUDED.sec_enc,
                is_active = 1
        """,
        (chat_id, _enc(public_key), _enc(secret_key), json.dumps(DEFAULT_SETTINGS)),
    )
    _USER_CACHE.pop(chat_id, None)
    _ACTIVE_USERS_CACHE = None
    return get_user(chat_id)


def get_user(chat_id: str) -> dict | None:
    if chat_id in _USER_CACHE:
        return copy.deepcopy(_USER_CACHE[chat_id])
    row = _fetch_one("SELECT * FROM users WHERE chat_id = %s", (chat_id,))
    if row:
        h = _hydrate(row)
        _USER_CACHE[chat_id] = copy.deepcopy(h)
        return h
    return None


def get_all_active() -> list[dict]:
    global _ACTIVE_USERS_CACHE
    if _ACTIVE_USERS_CACHE is not None:
        return copy.deepcopy(_ACTIVE_USERS_CACHE)
    rows = _fetch_all("SELECT * FROM users WHERE is_active = 1")
    result = [_hydrate(r) for r in rows if str(r.get("chat_id", "")) != "0"]
    for u in result:
        _USER_CACHE[u["chat_id"]] = copy.deepcopy(u)
    _ACTIVE_USERS_CACHE = copy.deepcopy(result)
    return result


def update_settings(chat_id: str, settings: dict):
    global _ACTIVE_USERS_CACHE
    _execute(
        "UPDATE users SET settings = %s WHERE chat_id = %s",
        (json.dumps(settings), chat_id),
    )
    if chat_id in _USER_CACHE:
        _USER_CACHE[chat_id]["settings"] = copy.deepcopy(settings)
    if _ACTIVE_USERS_CACHE:
        for u in _ACTIVE_USERS_CACHE:
            if u["chat_id"] == chat_id:
                u["settings"] = copy.deepcopy(settings)


def invalidate_user_cache(chat_id: str = None):
    global _ACTIVE_USERS_CACHE
    _ACTIVE_USERS_CACHE = None
    if chat_id:
        _USER_CACHE.pop(chat_id, None)
    else:
        _USER_CACHE.clear()


def deactivate(chat_id: str):
    global _ACTIVE_USERS_CACHE
    _execute("UPDATE users SET is_active = 0 WHERE chat_id = %s", (chat_id,))
    _USER_CACHE.pop(chat_id, None)
    _ACTIVE_USERS_CACHE = None


def _hydrate(row: dict) -> dict:
    pub = row.pop("pub_enc", None)
    sec = row.pop("sec_enc", None)
    row["chat_id"] = str(row.get("chat_id", ""))
    try:
        row["public_key"] = _dec(pub) if pub else ""
    except Exception:
        row["public_key"] = pub or ""
    try:
        row["secret_key"] = _dec(sec) if sec else ""
    except Exception:
        row["secret_key"] = sec or ""
    saved = json.loads(row.get("settings") or "{}")
    row["settings"] = {**DEFAULT_SETTINGS, **saved}
    return row


# ── Trades ────────────────────────────────────────────────────────────────────

def record_trade(chat_id: str, **kw) -> str:
    trade_id = str(uuid.uuid4())
    _execute("""
        INSERT INTO trades (
            trade_id, chat_id, strategy, asset, timeframe, outcome, outcome_id,
            market_id, event_id, order_id, entry_price, amount_ngn, certainty,
            secs_to_close, spot_vs_threshold_pct, momentum_at_entry,
            regime_at_entry, edge_at_entry, realized_vol_at_entry,
            market_price_at_entry, slippage_ngn, engine
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
        )
    """, (
        trade_id, chat_id,
        kw.get("strategy"), kw.get("asset"), kw.get("timeframe"),
        kw.get("outcome"), kw.get("outcome_id"),
        kw.get("market_id"), kw.get("event_id"), kw.get("order_id"),
        kw.get("entry_price"), kw.get("amount_ngn"), kw.get("certainty"),
        kw.get("secs_to_close", 0), kw.get("spot_vs_threshold_pct", 0.0),
        kw.get("momentum_at_entry", 0.0), kw.get("regime_at_entry", 0.0),
        kw.get("edge_at_entry", 0.0), kw.get("realized_vol_at_entry", 0.0),
        kw.get("market_price_at_entry"), kw.get("slippage_ngn"),
        kw.get("engine"),
    ))
    return trade_id


def resolve_trade(trade_id: str, won: bool, pnl_ngn: float):
    _execute(
        "UPDATE trades SET won = %s, pnl_ngn = %s, resolved_at = NOW() WHERE trade_id = %s",
        (1 if won else 0, pnl_ngn, trade_id),
    )


def get_unresolved(chat_id: str, older_than_minutes: int = 6) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
    return _fetch_all("""
        SELECT * FROM trades
        WHERE chat_id = %s AND won IS NULL
          AND created_at < %s::TIMESTAMPTZ
    """, (chat_id, cutoff))


def get_all_unresolved(chat_id: str) -> list[dict]:
    return _fetch_all("""
        SELECT * FROM trades WHERE chat_id = %s AND won IS NULL
        ORDER BY created_at DESC
    """, (chat_id,))


def recent_trades(chat_id: str, limit: int = 10) -> list[dict]:
    return _fetch_all("""
        SELECT * FROM trades WHERE chat_id = %s
        ORDER BY created_at DESC LIMIT %s
    """, (chat_id, limit))


def recent_stats(chat_id: str, days: int = 30) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = _fetch_all("""
        SELECT strategy, asset, timeframe,
               COUNT(*)       AS total,
               SUM(won)       AS wins,
               SUM(pnl_ngn)   AS total_pnl,
               AVG(certainty) AS avg_certainty
        FROM trades
        WHERE chat_id = %s AND won IS NOT NULL
          AND created_at > %s::TIMESTAMPTZ
        GROUP BY strategy, asset, timeframe
        ORDER BY strategy, asset, timeframe
    """, (chat_id, cutoff))
    for r in rows:
        r["win_rate"] = (r["wins"] or 0) / r["total"] if r["total"] else 0.0
    return rows


def all_time_stats(chat_id: str) -> dict:
    row = _fetch_one("""
        SELECT COUNT(*) AS total, SUM(won) AS wins, SUM(pnl_ngn) AS pnl
        FROM trades WHERE chat_id = %s AND won IS NOT NULL
    """, (chat_id,))
    total = int(row["total"] or 0)
    wins  = int(row["wins"]  or 0)
    pnl   = float(row["pnl"] or 0.0)
    return {
        "total": total, "wins": wins, "losses": total - wins,
        "win_rate": wins / total if total else 0.0,
        "total_pnl": pnl,
    }


def get_combo_stats(chat_id: str, days: int = 14) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = _fetch_all("""
        SELECT strategy, asset, timeframe,
               COUNT(*) AS total, SUM(won) AS wins, SUM(pnl_ngn) AS total_pnl
        FROM trades
        WHERE chat_id = %s AND won IS NOT NULL
          AND created_at > %s::TIMESTAMPTZ
        GROUP BY strategy, asset, timeframe
        HAVING COUNT(*) >= 3
        ORDER BY SUM(pnl_ngn) ASC
    """, (chat_id, cutoff))
    for r in rows:
        r["win_rate"] = (r["wins"] or 0) / r["total"] if r["total"] else 0.0
    return rows


def get_recent_streak(chat_id: str, strategy: str, asset: str, timeframe: str, limit: int = 5) -> list[int]:
    with _cx() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT won FROM trades
                WHERE chat_id = %s AND strategy = %s AND asset = %s
                  AND timeframe = %s AND won IS NOT NULL
                ORDER BY created_at DESC LIMIT %s
            """, (chat_id, strategy, asset, timeframe, limit))
            return [row[0] for row in cur.fetchall()]


def get_hourly_stats(chat_id: str, days: int = 30) -> list[dict]:
    with _cx() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC')::INTEGER AS hour,
                    COUNT(*) AS total,
                    SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END)::FLOAT / COUNT(*) AS win_rate
                FROM trades
                WHERE chat_id = %s AND won IS NOT NULL
                  AND created_at > NOW() - INTERVAL '30 days'
                GROUP BY hour ORDER BY hour
            """, (chat_id,))
            return [dict(r) for r in cur.fetchall()]


def get_avg_slippage(asset: str, strategy: str, limit: int = 5) -> float:
    with _cx() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT slippage_ngn, amount_ngn FROM trades
                WHERE asset = %s AND strategy = %s AND slippage_ngn IS NOT NULL
                ORDER BY created_at DESC LIMIT %s
            """, (asset, strategy, limit))
            rows = cur.fetchall()
    if not rows:
        return 0.0
    total = sum((s / a) for s, a in rows if a and a > 0)
    return total / len(rows)


# ── Quant state ───────────────────────────────────────────────────────────────

def save_quant_state(asset: str, state: dict):
    _execute("""
        INSERT INTO quant_state (asset, state_json, updated_at)
        VALUES (%s, %s, NOW())
        ON CONFLICT (asset) DO UPDATE
            SET state_json = EXCLUDED.state_json, updated_at = NOW()
    """, (asset, json.dumps(state)))


def load_quant_states() -> dict[str, dict]:
    rows = _fetch_all("SELECT asset, state_json FROM quant_state")
    return {r["asset"]: json.loads(r["state_json"]) for r in rows}


def get_alpha_trend(chat_id: str, strategy: str, asset: str, days: int = 7) -> float:
    """
    Returns a 0.0–1.0 multiplier reflecting recent strategy/asset performance.
    1.0 = performing normally, <0.85 = underperforming, triggers size reduction.
    Based on win rate over last `days` days vs expected baseline.
    Returns 1.0 (no penalty) if fewer than 5 resolved trades exist.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    try:
        row = _fetch_one("""
            SELECT
                COUNT(*)                                        AS total,
                SUM(CASE WHEN won = 1 THEN 1 ELSE 0 END)       AS wins
            FROM trades
            WHERE chat_id = %s
              AND strategy = %s
              AND asset    = %s
              AND won IS NOT NULL
              AND created_at > %s::TIMESTAMPTZ
        """, (chat_id, strategy, asset, cutoff))

        if not row or int(row.get("total") or 0) < 5:
            return 1.0   # not enough data — no penalty

        total    = int(row["total"])
        wins     = int(row.get("wins") or 0)
        win_rate = wins / total

        expected = 0.65 if strategy == "SNIPE" else 0.55

        if win_rate >= expected:
            return 1.0
        # Linear decay: at 0% win rate → 0.5 multiplier, at expected → 1.0
        return max(0.5, win_rate / expected)

    except Exception as e:
        log.debug(f"get_alpha_trend error: {e}")
        return 1.0
