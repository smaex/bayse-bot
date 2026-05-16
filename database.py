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
import queue
import threading
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
_db_queue: queue.Queue = queue.Queue()

def _db_worker():
    while True:
        try:
            # Jitter to prevent connection storms between ghost instances
            time.sleep(random.uniform(2, 8))
            batch = []
            batch.append(_db_queue.get())
            try:
                while len(batch) < 100:
                    batch.append(_db_queue.get_nowait())
            except queue.Empty:
                pass
            
            if not batch:
                continue
                
            try:
                with _cx() as conn:
                    with conn.cursor() as cur:
                        for query, params in batch:
                            cur.execute(query, params)
            except Exception as e:
                log.error(f"DB worker batch execute error: {e}")
                
            for _ in batch:
                _db_queue.task_done()
                
        except Exception as e:
            log.error(f"DB worker loop error: {e}")


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
    Connection Manager — checks out from pool, validates, yields, returns.
    Retry is minimal (3 attempts / 0.3s) to avoid blocking the thread pool.
    """
    import time
    
    if _pool is None:
        _init_pool()
    
    conn = None
    max_retries = 3
    retry_delay = 0.1
    
    for attempt in range(max_retries):
        try:
            conn = _pool.getconn()
            break
        except psycopg2.pool.PoolError:
            if attempt == max_retries - 1:
                log.error("Database connection pool exhausted (3 retries).")
                raise
            time.sleep(retry_delay)
            
    try:
        # Validate connection — CockroachDB free tier drops idle conns aggressively
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            log.warning("Detected dead DB connection. Reconnecting...")
            _pool.putconn(conn, close=True)
            conn = _pool.getconn()
            
        yield conn
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
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

def _enqueue(query: str, params: tuple = ()):
    """Push a write query to the background worker queue."""
    _db_queue.put((query, params))


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
    threading.Thread(target=_db_worker, daemon=True).start()
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
                CREATE TABLE IF NOT EXISTS bot_lock (
                    lock_id    TEXT PRIMARY KEY,
                    process_id INTEGER NOT NULL,
                    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Initialize the single lock row if not exists
            cur.execute("INSERT INTO bot_lock (lock_id, process_id) VALUES ('MASTER', 0) ON CONFLICT DO NOTHING")

def acquire_singleton_lock() -> bool:
    """
    World-Class Ghost Shield:
    Attempts to claim the 'MASTER' lock in the database.
    If another process (PID) has updated the lock in the last 60 seconds, we fail.
    """
    import os
    pid = os.getpid()
    try:
        with _cx() as conn:
            with conn.cursor() as cur:
                # Atomically update the lock if it's stale (>60s) or belongs to us
                cur.execute("""
                    UPDATE bot_lock 
                    SET process_id = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE lock_id = 'MASTER' 
                      AND (updated_at < CURRENT_TIMESTAMP - INTERVAL '60 seconds' OR process_id = %s)
                    RETURNING process_id
                """, (pid, pid))
                row = cur.fetchone()
                if row and row[0] == pid:
                    return True
    except Exception as e:
        log.error(f"Error acquiring singleton lock: {e}")
    return False

def heartbeat_singleton_lock():
    """Updates the lock timestamp to keep it alive."""
    import os
    pid = os.getpid()
    _enqueue("UPDATE bot_lock SET updated_at = CURRENT_TIMESTAMP WHERE lock_id = 'MASTER' AND process_id = %s", (pid,))

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
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS market_price_at_entry REAL")
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS poly_price_at_entry REAL")
            cur.execute("ALTER TABLE trades ADD COLUMN IF NOT EXISTS slippage_ngn REAL")
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
    _enqueue(
        "UPDATE users SET settings=%s WHERE chat_id=%s",
        (json.dumps(settings), chat_id),
    )

def deactivate(chat_id: str):
    _enqueue("UPDATE users SET is_active=0 WHERE chat_id=%s", (chat_id,))

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
    _enqueue("""
        INSERT INTO trades
          (trade_id, chat_id, strategy, asset, timeframe, outcome, outcome_id,
           market_id, event_id, order_id, entry_price, amount_ngn, certainty,
           secs_to_close, spot_vs_threshold_pct,
           momentum_at_entry, regime_at_entry, edge_at_entry, realized_vol_at_entry,
           market_price_at_entry, poly_price_at_entry, slippage_ngn,
           created_at)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
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
        kw.get("market_price_at_entry"), kw.get("poly_price_at_entry"), kw.get("slippage_ngn"),
        now,
    ))
    return trade_id

def get_avg_slippage(asset: str, strategy: str, limit: int = 5) -> float:
    """Returns the average slippage percentage for the last N trades."""
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
            
            total_slip_pct = 0.0
            for slip_ngn, amount in rows:
                if amount > 0:
                    total_slip_pct += (slip_ngn / amount)
            return total_slip_pct / len(rows)

def resolve_trade(trade_id: str, won: bool, pnl_ngn: float):
    now = datetime.now(timezone.utc).isoformat()
    _enqueue(
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

def get_combo_stats(chat_id: str, days: int = 14) -> list[dict]:
    """
    Returns granular win rates for each (strategy, asset, timeframe) combo.
    Used by the Self-Correction Engine to identify and deactivate
    specific losing patterns (e.g. 'SNIPE on SOL 5min' losing 80% of the time).
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = _fetch_all("""
        SELECT strategy, asset, timeframe,
               COUNT(*)       AS total,
               SUM(won)       AS wins,
               SUM(pnl_ngn)   AS total_pnl
        FROM trades
        WHERE chat_id=%s AND won IS NOT NULL AND created_at::TIMESTAMPTZ > %s::TIMESTAMPTZ
        GROUP BY strategy, asset, timeframe
        HAVING COUNT(*) >= 3
        ORDER BY SUM(pnl_ngn) ASC
    """, (chat_id, cutoff))
    for r in rows:
        r["win_rate"] = (r["wins"] or 0) / r["total"] if r["total"] else 0.0
    return rows

def get_alpha_trend(chat_id: str, strategy: str, asset: str, limit: int = 10) -> float:
    """
    Calculates Alpha Decay by comparing the edge of recent trades to older ones.
    Returns a 'Decay Factor' (1.0 = stable, < 1.0 = decaying, > 1.0 = expanding).
    """
    rows = _fetch_all("""
        SELECT edge_at_entry FROM trades
        WHERE chat_id=%s AND strategy=%s AND asset=%s AND won IS NOT NULL
        ORDER BY created_at DESC LIMIT %s
    """, (chat_id, strategy, asset, limit))
    
    edges = [float(r["edge_at_entry"] or 0) for r in rows]
    if len(edges) < 6: return 1.0 # Not enough data to judge trend
    
    # Compare recent 3 to older 3
    recent_avg = sum(edges[:3]) / 3
    older_avg = sum(edges[3:6]) / 3
    
    if older_avg <= 0: return 1.0
    return recent_avg / older_avg

# ── Quant State Persistence ──────────────────────────────────────────────────

def save_quant_state(asset: str, state: dict):
    now = datetime.now(timezone.utc).isoformat()
    _enqueue(
        "INSERT INTO quant_state (asset, state_json, updated_at) VALUES (%s,%s,%s) "
        "ON CONFLICT (asset) DO UPDATE SET state_json=EXCLUDED.state_json, updated_at=EXCLUDED.updated_at",
        (asset, json.dumps(state), now)
    )

def load_quant_states() -> dict[str, dict]:
    rows = _fetch_all("SELECT asset, state_json FROM quant_state")
    return {r["asset"]: json.loads(r["state_json"]) for r in rows}

def get_hourly_stats(chat_id: str, days: int = 30) -> list[dict]:
    """Returns win rates indexed by UTC hour for the last X days."""
    query = """
        SELECT 
            EXTRACT(HOUR FROM created_at AT TIME ZONE 'UTC')::INTEGER as hour,
            COUNT(*) as total,
            SUM(CASE WHEN won = True THEN 1 ELSE 0 END)::FLOAT / COUNT(*) as win_rate
        FROM trades
        WHERE chat_id = %s 
          AND resolved = True 
          AND created_at > NOW() - INTERVAL '30 days'
        GROUP BY hour
        ORDER BY hour ASC
    """
    with _cx() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(query, (chat_id,))
            return cur.fetchall()


# ── Persistent Recordings ────────────────────────────────────────────────────

def save_recording(type: str, data: dict, asset: str = None):
    """Save a market snapshot or spot tick for future backtesting."""
    _enqueue(
        "INSERT INTO recordings (type, asset, data_json) VALUES (%s, %s, %s)",
        (type, asset, json.dumps(data))
    )


def save_recording_nonblocking(type: str, data, asset: str = None):
    """
    Non-blocking save — implicitly non-blocking since we enqueue it.
    """
    _enqueue(
        "INSERT INTO recordings (type, asset, data_json) VALUES (%s, %s, %s)",
        (type, asset, json.dumps(data))
    )


def save_recordings_batch(ticks: list[dict]):
    """
    Batch-insert spot ticks in a SINGLE connection checkout.
    Receives a list of {"asset": ..., "price": ..., "time": ...} dicts.
    This replaces the old per-tick INSERT that was exhausting the pool.
    """
    if not ticks:
        return
    try:
        with _cx() as conn:
            with conn.cursor() as cur:
                psycopg2.extras.execute_values(
                    cur,
                    "INSERT INTO recordings (type, asset, data_json) VALUES %s",
                    [
                        ("spot_tick", t["asset"], json.dumps({"price": t["price"], "time": t["time"]}))
                        for t in ticks
                    ],
                    page_size=100,
                )
        log.debug(f"Batch-inserted {len(ticks)} spot ticks")
    except psycopg2.pool.PoolError:
        log.debug("Tick batch skipped — pool busy (non-critical)")
    except Exception as e:
        log.warning(f"Tick batch failed: {e}")


def get_recordings(type: str = None, limit: int = 1000) -> list[dict]:
    query = "SELECT * FROM recordings"
    params = ()
    if type:
        query += " WHERE type=%s"
        params = (type,)
    query += " ORDER BY created_at ASC LIMIT %s"
    params += (limit,)
    return _fetch_all(query, params)
def get_recent_trades(days: int = 7) -> list:
    """Fetch all trades from the last X days for optimization."""
    try:
        with _cx() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
                cutoff = datetime.now(timezone.utc) - timedelta(days=days)
                cur.execute(
                    "SELECT * FROM trades WHERE created_at::TIMESTAMPTZ > %s::TIMESTAMPTZ",
                    (cutoff,)
                )
                return [dict(r) for r in cur.fetchall()]
    except Exception as e:
        log.error(f"Error fetching recent trades: {e}")
        return []

def save_optimized_params(params: dict):
    """Save winning parameters from nightly optimization."""
    # We'll store this in a 'global_config' table or as a special user record
    # For now, we'll queue it to the batch worker for a generic key-value store
    query = "UPSERT INTO config (key, value) VALUES (%s, %s)"
    _db_queue.put((query, ("optimized_params", json.dumps(params))))
