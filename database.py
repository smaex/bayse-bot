"""
Multi-user PostgreSQL store (CockroachDB free tier).
Per-user: API keys (AES-encrypted), settings, trade history.
Survives every Replit/Render redeploy — no persistent disk needed.

v2: Connection pooling (ThreadedConnectionPool) — avoids per-call connection
churn that was burning through CockroachDB's 5-connection free-tier limit.
"""

import json
import logging
import os
import uuid
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
    "strategies":       ["SNIPE", "CORRELATE", "ARB", "NEWS"],
    "risk_pct":         3.0,
    "mintrade":         100,
    "maxtrade":         500_000,
    "maxexposure":      30.0,
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
            "ENCRYPTION_KEY not set. Generate one with:\n"
            'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
        )
    return Fernet(key.encode())

def _enc(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()

def _dec(text: str) -> str:
    return _fernet().decrypt(text.encode()).decode()


# ── Connection Pool ───────────────────────────────────────────────────────────
# CockroachDB free tier: 5 max connections.  Pool avoids per-call connection
# churn (old: open → query → close on every DB call = connection storm).

_pool: psycopg2.pool.ThreadedConnectionPool | None = None


def _init_pool():
    """Create the connection pool. Called once from init_db()."""
    global _pool
    if _pool is not None:
        return
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL not set. Add your CockroachDB connection string.")
    url = DATABASE_URL.replace("sslmode=verify-full", "sslmode=require")
    _pool = psycopg2.pool.ThreadedConnectionPool(
        minconn=2,
        maxconn=5,   # CockroachDB free tier limit
        dsn=url,
    )
    log.info("Connection pool created (min=2, max=5)")


@contextmanager
def _cx():
    """
    Trench-Hardened Connection Manager:
    1. Checks out a connection from the pool.
    2. Validates it with a 'SELECT 1' to ensure it hasn't been dropped by CockroachDB.
    3. If dropped, it closes the dead handle and creates a fresh one.
    """
    if _pool is None:
        _init_pool()
    
    conn = None
    try:
        conn = _pool.getconn()
        # Validate connection — CockroachDB free tier drops idle conns aggressively
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            log.warning("Detected dead DB connection. Reconnecting...")
            _pool.putconn(conn, close=True) # Ensure dead conn is fully removed
            conn = _pool.getconn()
            
        yield conn
        conn.commit() # Auto-commit successful transactions
    except Exception as e:
        if conn:
            conn.rollback() # Rollback on error
        log.error(f"Database transaction error: {e}")
        raise
    finally:
        if conn:
            _pool.putconn(conn)


def check_connection() -> bool:
    """Used for health checks to ensure DB is reachable."""
    try:
        with _cx() as conn:
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


# ── DB setup ──────────────────────────────────────────────────────────────────

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
                    created_at TEXT
                )
            """)
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
                    won                   INTEGER,
                    pnl_ngn               REAL,
                    created_at            TEXT,
                    resolved_at           TEXT
                )
            """)
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS order_id TEXT")
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS momentum_at_entry REAL")
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS regime_at_entry REAL")
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS edge_at_entry REAL")
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS realized_vol_at_entry REAL")
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_trades_user
                ON trades(chat_id, created_at DESC)
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS quant_state (
                    asset      TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS recordings (
                    id         SERIAL PRIMARY KEY,
                    type       TEXT NOT NULL,
                    asset      TEXT,
                    data_json  TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
        conn.commit()
    log.info("Database ready (PostgreSQL, pooled connections)")


# ── Users ─────────────────────────────────────────────────────────────────────

def add_user(chat_id: str, public_key: str, secret_key: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        "INSERT INTO users (chat_id, pub_enc, sec_enc, settings, created_at) VALUES (%s,%s,%s,%s,%s) "
        "ON CONFLICT (chat_id) DO UPDATE SET pub_enc=EXCLUDED.pub_enc, sec_enc=EXCLUDED.sec_enc, is_active=1",
        (chat_id, _enc(public_key), _enc(secret_key), json.dumps(DEFAULT_SETTINGS), now),
    )
    return get_user(chat_id)

def get_user(chat_id: str) -> dict | None:
    row = _fetch_one("SELECT * FROM users WHERE chat_id=%s", (chat_id,))
    return _hydrate(row) if row else None

def get_all_active() -> list[dict]:
    rows = _fetch_all("SELECT * FROM users WHERE is_active=1")
    return [_hydrate(r) for r in rows]

def update_settings(chat_id: str, settings: dict):
    _execute(
        "UPDATE users SET settings=%s WHERE chat_id=%s",
        (json.dumps(settings), chat_id),
    )

def deactivate(chat_id: str):
    _execute("UPDATE users SET is_active=0 WHERE chat_id=%s", (chat_id,))

def _hydrate(row: dict) -> dict:
    row["public_key"] = _dec(row.pop("pub_enc"))
    row["secret_key"] = _dec(row.pop("sec_enc"))
    saved = json.loads(row.get("settings") or "{}")
    row["settings"] = {**DEFAULT_SETTINGS, **saved}
    return row


# ── Trade records ─────────────────────────────────────────────────────────────

def record_trade(chat_id: str, **kw) -> str:
    trade_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    _execute("""
        INSERT INTO trades
          (trade_id, chat_id, strategy, asset, timeframe, outcome, outcome_id,
           market_id, event_id, order_id, entry_price, amount_ngn, certainty,
           secs_to_close, spot_vs_threshold_pct,
           momentum_at_entry, regime_at_entry, edge_at_entry, realized_vol_at_entry,
           created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
    """, (
        trade_id, chat_id,
        kw.get("strategy"), kw.get("asset"), kw.get("timeframe"),
        kw.get("outcome"), kw.get("outcome_id"),
        kw.get("market_id"), kw.get("event_id"), kw.get("order_id"),
        kw.get("entry_price"), kw.get("amount_ngn"),
        kw.get("certainty"), kw.get("secs_to_close"),
        kw.get("spot_vs_threshold_pct", 0.0),
        kw.get("momentum_at_entry", 0.0), kw.get("regime_at_entry", 0.0),
        kw.get("edge_at_entry", 0.0), kw.get("realized_vol_at_entry", 0.0),
        now,
    ))
    return trade_id

def resolve_trade(trade_id: str, won: bool, pnl_ngn: float):
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        "UPDATE trades SET won=%s, pnl_ngn=%s, resolved_at=%s WHERE trade_id=%s",
        (1 if won else 0, pnl_ngn, now, trade_id),
    )

def get_unresolved(chat_id: str, older_than_minutes: int = 6) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
    return _fetch_all("""
        SELECT * FROM trades
        WHERE chat_id=%s AND won IS NULL AND created_at < %s
    """, (chat_id, cutoff))

def get_all_unresolved(chat_id: str) -> list[dict]:
    """Return ALL unresolved trades for a user — used to reconstruct positions on restart."""
    return _fetch_all("""
        SELECT * FROM trades
        WHERE chat_id=%s AND won IS NULL
        ORDER BY created_at DESC
    """, (chat_id,))

def recent_stats(chat_id: str, days: int = 30) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    rows = _fetch_all("""
        SELECT strategy, asset, timeframe,
               COUNT(*)       AS total,
               SUM(won)       AS wins,
               SUM(pnl_ngn)   AS total_pnl,
               AVG(certainty) AS avg_certainty
        FROM trades
        WHERE chat_id=%s AND won IS NOT NULL AND created_at > %s
        GROUP BY strategy, asset, timeframe
        ORDER BY strategy, asset, timeframe
    """, (chat_id, cutoff))
    result = []
    for r in rows:
        r["win_rate"] = (r["wins"] or 0) / r["total"] if r["total"] else 0.0
        result.append(r)
    return result

def all_time_stats(chat_id: str) -> dict:
    row = _fetch_one("""
        SELECT COUNT(*) AS total, SUM(won) AS wins, SUM(pnl_ngn) AS pnl
        FROM trades WHERE chat_id=%s AND won IS NOT NULL
    """, (chat_id,))
    total = int(row["total"] or 0)
    wins  = int(row["wins"]  or 0)
    pnl   = float(row["pnl"] or 0.0)
    return {
        "total": total, "wins": wins, "losses": total - wins,
        "win_rate": wins / total if total else 0.0,
        "total_pnl": pnl,
    }

def recent_trades(chat_id: str, limit: int = 10) -> list[dict]:
    return _fetch_all("""
        SELECT * FROM trades WHERE chat_id=%s
        ORDER BY created_at DESC LIMIT %s
    """, (chat_id, limit))

# ── Quant State Persistence ──────────────────────────────────────────────────

def save_quant_state(asset: str, state: dict):
    now = datetime.now(timezone.utc).isoformat()
    _execute(
        "INSERT INTO quant_state (asset, state_json, updated_at) VALUES (%s,%s,%s) "
        "ON CONFLICT (asset) DO UPDATE SET state_json=EXCLUDED.state_json, updated_at=EXCLUDED.updated_at",
        (asset, json.dumps(state), now)
    )

def load_quant_states() -> dict[str, dict]:
    rows = _fetch_all("SELECT asset, state_json FROM quant_state")
    return {r["asset"]: json.loads(r["state_json"]) for r in rows}


# ── Persistent Recordings ────────────────────────────────────────────────────

def save_recording(type: str, data: dict, asset: str = None):
    """Save a market snapshot or spot tick for future backtesting."""
    _execute(
        "INSERT INTO recordings (type, asset, data_json) VALUES (%s, %s, %s)",
        (type, asset, json.dumps(data))
    )

def get_recordings(type: str = None, limit: int = 1000) -> list[dict]:
    query = "SELECT * FROM recordings"
    params = ()
    if type:
        query += " WHERE type=%s"
        params = (type,)
    query += " ORDER BY created_at ASC LIMIT %s"
    params += (limit,)
    return _fetch_all(query, params)
