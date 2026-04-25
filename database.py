"""
Multi-user SQLite store.
Per-user: API keys (AES-encrypted), settings, trade history.
"""

import json
import logging
import os
import sqlite3
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from cryptography.fernet import Fernet

log = logging.getLogger(__name__)

# On Render: use the mounted persistent disk (/data).
# Locally: fall back to ./data so nothing changes for development.
DATA_DIR = Path(os.environ.get("DATA_DIR", "data"))
DB_PATH  = DATA_DIR / "users.db"

DEFAULT_SETTINGS: dict = {
    "assets":           ["BTC", "ETH", "SOL"],
    "timeframes":       ["5min", "15min", "1h"],
    "strategies":       ["SNIPE", "CORRELATE", "ARB", "NEWS"],
    "risk_pct":         3.0,
    "mintrade":         100,
    "maxtrade":         500_000,
    "maxexposure":      30.0,
    "daily_multiplier": 50,      # stop when profit = N × starting balance
    "daily_target_ngn": 0,       # 0 = use multiplier; positive = fixed ₦ target
    "paused":           False,
    "learned":          {},
}


# ── Encryption ────────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    key = os.environ.get("ENCRYPTION_KEY", "")
    if not key:
        raise RuntimeError(
            "ENCRYPTION_KEY not set.\n"
            "Generate one with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode())


def _enc(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def _dec(text: str) -> str:
    return _fernet().decrypt(text.encode()).decode()


# ── DB setup ──────────────────────────────────────────────────────────────────

def init_db():
    DATA_DIR.mkdir(exist_ok=True)
    with _cx() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                chat_id    TEXT PRIMARY KEY,
                pub_enc    TEXT NOT NULL,
                sec_enc    TEXT NOT NULL,
                settings   TEXT DEFAULT '{}',
                is_active  INTEGER DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

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
                entry_price           REAL,
                amount_ngn            REAL,
                certainty             REAL,
                secs_to_close         REAL,
                spot_vs_threshold_pct REAL,
                won                   INTEGER,
                pnl_ngn               REAL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                resolved_at TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_trades_user
                ON trades(chat_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_trades_open
                ON trades(chat_id, won) WHERE won IS NULL;
        """)
    log.info("Database ready")


def _cx() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, isolation_level=None)


# ── Users ─────────────────────────────────────────────────────────────────────

def add_user(chat_id: str, public_key: str, secret_key: str) -> dict:
    with _cx() as db:
        db.execute(
            "INSERT OR REPLACE INTO users (chat_id, pub_enc, sec_enc, settings) VALUES (?,?,?,?)",
            (chat_id, _enc(public_key), _enc(secret_key), json.dumps(DEFAULT_SETTINGS)),
        )
    return get_user(chat_id)


def get_user(chat_id: str) -> dict | None:
    with _cx() as db:
        db.row_factory = sqlite3.Row
        row = db.execute("SELECT * FROM users WHERE chat_id=?", (chat_id,)).fetchone()
    return _hydrate(dict(row)) if row else None


def get_all_active() -> list[dict]:
    with _cx() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("SELECT * FROM users WHERE is_active=1").fetchall()
    return [_hydrate(dict(r)) for r in rows]


def update_settings(chat_id: str, settings: dict):
    with _cx() as db:
        db.execute(
            "UPDATE users SET settings=? WHERE chat_id=?",
            (json.dumps(settings), chat_id),
        )


def deactivate(chat_id: str):
    with _cx() as db:
        db.execute("UPDATE users SET is_active=0 WHERE chat_id=?", (chat_id,))


def _hydrate(row: dict) -> dict:
    row["public_key"] = _dec(row.pop("pub_enc"))
    row["secret_key"] = _dec(row.pop("sec_enc"))
    saved = json.loads(row.get("settings") or "{}")
    row["settings"] = {**DEFAULT_SETTINGS, **saved}
    return row


# ── Trade records ─────────────────────────────────────────────────────────────

def record_trade(chat_id: str, **kw) -> str:
    trade_id = str(uuid.uuid4())
    with _cx() as db:
        db.execute("""
            INSERT INTO trades
              (trade_id, chat_id, strategy, asset, timeframe, outcome, outcome_id,
               market_id, event_id, entry_price, amount_ngn, certainty,
               secs_to_close, spot_vs_threshold_pct)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            trade_id, chat_id,
            kw.get("strategy"), kw.get("asset"), kw.get("timeframe"),
            kw.get("outcome"), kw.get("outcome_id"),
            kw.get("market_id"), kw.get("event_id"),
            kw.get("entry_price"), kw.get("amount_ngn"),
            kw.get("certainty"), kw.get("secs_to_close"),
            kw.get("spot_vs_threshold_pct", 0.0),
        ))
    return trade_id


def resolve_trade(trade_id: str, won: bool, pnl_ngn: float):
    now = datetime.now(timezone.utc).isoformat()
    with _cx() as db:
        db.execute(
            "UPDATE trades SET won=?, pnl_ngn=?, resolved_at=? WHERE trade_id=?",
            (1 if won else 0, pnl_ngn, now, trade_id),
        )


def get_unresolved(chat_id: str, older_than_minutes: int = 6) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=older_than_minutes)).isoformat()
    with _cx() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT * FROM trades
            WHERE chat_id=? AND won IS NULL AND created_at < ?
        """, (chat_id, cutoff)).fetchall()
    return [dict(r) for r in rows]


def recent_stats(chat_id: str, days: int = 30) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with _cx() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT strategy, asset, timeframe,
                   COUNT(*)       AS total,
                   SUM(won)       AS wins,
                   SUM(pnl_ngn)   AS total_pnl,
                   AVG(certainty) AS avg_certainty
            FROM trades
            WHERE chat_id=? AND won IS NOT NULL AND created_at > ?
            GROUP BY strategy, asset, timeframe
            ORDER BY strategy, asset, timeframe
        """, (chat_id, cutoff)).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["win_rate"] = (d["wins"] or 0) / d["total"] if d["total"] else 0.0
        result.append(d)
    return result


def all_time_stats(chat_id: str) -> dict:
    with _cx() as db:
        row = db.execute("""
            SELECT COUNT(*), SUM(won), SUM(pnl_ngn)
            FROM trades WHERE chat_id=? AND won IS NOT NULL
        """, (chat_id,)).fetchone()
    total, wins, pnl = row
    total, wins, pnl = total or 0, wins or 0, pnl or 0.0
    return {
        "total": total, "wins": wins, "losses": total - wins,
        "win_rate": wins / total if total else 0.0,
        "total_pnl": pnl,
    }


def recent_trades(chat_id: str, limit: int = 10) -> list[dict]:
    with _cx() as db:
        db.row_factory = sqlite3.Row
        rows = db.execute("""
            SELECT * FROM trades WHERE chat_id=?
            ORDER BY created_at DESC LIMIT ?
        """, (chat_id, limit)).fetchall()
    return [dict(r) for r in rows]
