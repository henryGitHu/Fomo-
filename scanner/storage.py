"""SQLite storage: tokens, snapshots, mentions, alerts, outcomes.

The mentions / alerts / outcomes tables are created now so the database
layout is stable; they are filled in by later phases.
"""
from __future__ import annotations

import sqlite3
import time
from dataclasses import asdict, fields
from pathlib import Path

from .models import Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS tokens (
    chain              TEXT NOT NULL,
    token_address      TEXT NOT NULL,
    symbol             TEXT,
    name               TEXT,
    first_seen         REAL NOT NULL,
    last_seen_in_feed  REAL NOT NULL,
    last_feeds         TEXT,
    PRIMARY KEY (chain, token_address)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    chain             TEXT NOT NULL,
    token_address     TEXT NOT NULL,
    symbol            TEXT,
    name              TEXT,
    pair_address      TEXT,
    dex               TEXT,
    url               TEXT,
    source            TEXT,
    price_usd         REAL,
    price_change_m5   REAL,
    price_change_h1   REAL,
    price_change_h6   REAL,
    volume_m5         REAL,
    volume_h1         REAL,
    volume_h6         REAL,
    volume_h24        REAL,
    buys_m5           INTEGER,
    sells_m5          INTEGER,
    buys_h1           INTEGER,
    sells_h1          INTEGER,
    liquidity_usd     REAL,
    fdv_usd           REAL,
    market_cap_usd    REAL,
    pair_created_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_snapshots_token_ts ON snapshots (chain, token_address, ts);
CREATE INDEX IF NOT EXISTS idx_snapshots_ts ON snapshots (ts);

CREATE TABLE IF NOT EXISTS safety_checks (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    chain          TEXT NOT NULL,
    token_address  TEXT NOT NULL,
    status         TEXT NOT NULL,      -- PASS / FAIL / UNVERIFIED
    details        TEXT                -- JSON: individual checks + sources
);
CREATE INDEX IF NOT EXISTS idx_safety_token_ts ON safety_checks (chain, token_address, ts);

CREATE TABLE IF NOT EXISTS mentions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL NOT NULL,
    source         TEXT NOT NULL,      -- reddit / telegram / ...
    channel        TEXT,               -- subreddit or channel name
    author         TEXT,
    author_age_days REAL,
    chain          TEXT,
    token_address  TEXT,
    ticker         TEXT,
    match_type     TEXT,               -- address / ticker
    text_hash      TEXT,
    url            TEXT
);
CREATE INDEX IF NOT EXISTS idx_mentions_token_ts ON mentions (chain, token_address, ts);
CREATE INDEX IF NOT EXISTS idx_mentions_ticker_ts ON mentions (ticker, ts);

CREATE TABLE IF NOT EXISTS alerts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               REAL NOT NULL,
    chain            TEXT NOT NULL,
    token_address    TEXT NOT NULL,
    symbol           TEXT,
    score            REAL,
    score_breakdown  TEXT,             -- JSON
    signals          TEXT,             -- JSON list of signals that fired
    safety_status    TEXT,             -- PASS / UNVERIFIED
    safety_details   TEXT,             -- JSON
    price_usd        REAL,
    take_profit_usd  REAL,
    stop_loss_usd    REAL,
    position_usd     REAL,
    est_cost_pct     REAL,
    message          TEXT,
    delivered        INTEGER DEFAULT 0,
    pair_address     TEXT,
    liquidity_usd    REAL,
    risk             TEXT,
    dry_run          INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_alerts_token_ts ON alerts (chain, token_address, ts);

CREATE TABLE IF NOT EXISTS alert_prices (
    alert_id  INTEGER NOT NULL REFERENCES alerts(id),
    ts        REAL NOT NULL,
    price_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_prices ON alert_prices (alert_id, ts);

CREATE TABLE IF NOT EXISTS outcomes (
    alert_id         INTEGER PRIMARY KEY REFERENCES alerts(id),
    price_5m         REAL,
    price_15m        REAL,
    price_60m        REAL,
    price_4h         REAL,
    hit              TEXT,             -- TP / SL / OPEN / EXPIRED
    hit_ts           REAL,
    gross_return_pct REAL,
    net_return_pct   REAL,
    updated_ts       REAL
);
"""

_SNAPSHOT_COLS = [f.name for f in fields(Snapshot)]

# Columns added after a table was first released: (table, column, type).
# Older databases get them added automatically on startup.
_MIGRATIONS = [
    ("alerts", "pair_address", "TEXT"),
    ("alerts", "liquidity_usd", "REAL"),
    ("alerts", "risk", "TEXT"),
    ("alerts", "dry_run", "INTEGER DEFAULT 0"),
]


class Storage:
    def __init__(self, path: Path | str):
        path = Path(path)
        if str(path) != ":memory:":
            path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        for table, column, ctype in _MIGRATIONS:
            existing = {r[1] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ tokens
    def upsert_token(self, chain: str, address: str, symbol: str, name: str,
                     feeds: set[str], now: float) -> None:
        self.conn.execute(
            """INSERT INTO tokens (chain, token_address, symbol, name, first_seen,
                                   last_seen_in_feed, last_feeds)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(chain, token_address) DO UPDATE SET
                   symbol = COALESCE(NULLIF(excluded.symbol, ''), tokens.symbol),
                   name = COALESCE(NULLIF(excluded.name, ''), tokens.name),
                   last_seen_in_feed = excluded.last_seen_in_feed,
                   last_feeds = excluded.last_feeds""",
            (chain, address, symbol, name, now, now, ",".join(sorted(feeds))),
        )

    def recent_tokens(self, chain: str, since: float) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM tokens WHERE chain = ? AND last_seen_in_feed >= ?
               ORDER BY last_seen_in_feed DESC""",
            (chain, since),
        ).fetchall()

    # ------------------------------------------------------------------ snapshots
    def add_snapshots(self, snaps: list[Snapshot]) -> None:
        if not snaps:
            return
        placeholders = ",".join("?" for _ in _SNAPSHOT_COLS)
        self.conn.executemany(
            f"INSERT INTO snapshots ({','.join(_SNAPSHOT_COLS)}) VALUES ({placeholders})",
            [tuple(asdict(s)[c] for c in _SNAPSHOT_COLS) for s in snaps],
        )

    def snapshot_history(self, chain: str, address: str, since: float,
                         before: float | None = None) -> list[Snapshot]:
        before = before if before is not None else time.time() + 1
        rows = self.conn.execute(
            f"""SELECT {','.join(_SNAPSHOT_COLS)} FROM snapshots
                WHERE chain = ? AND token_address = ? AND ts >= ? AND ts < ?
                ORDER BY ts""",
            (chain, address, since, before),
        ).fetchall()
        return [Snapshot(**dict(r)) for r in rows]

    def prune_snapshots(self, older_than: float) -> int:
        cur = self.conn.execute("DELETE FROM snapshots WHERE ts < ?", (older_than,))
        self.conn.execute("DELETE FROM safety_checks WHERE ts < ?", (older_than,))
        return cur.rowcount

    # ------------------------------------------------------------------ safety
    def save_safety(self, chain: str, address: str, result) -> None:
        self.conn.execute(
            "INSERT INTO safety_checks (ts, chain, token_address, status, details) VALUES (?, ?, ?, ?, ?)",
            (result.checked_at, chain, address, result.status, result.to_json()),
        )
        self.conn.commit()

    def latest_safety(self, chain: str, address: str):
        from .safety import SafetyResult
        row = self.conn.execute(
            """SELECT ts, status, details FROM safety_checks
               WHERE chain = ? AND token_address = ? ORDER BY ts DESC LIMIT 1""",
            (chain, address),
        ).fetchone()
        if row is None:
            return None
        return SafetyResult.from_json(row["status"], row["details"], row["ts"])

    def commit(self) -> None:
        self.conn.commit()

    # ------------------------------------------------------------------ alerts
    def add_alert(self, **values) -> int:
        cols = list(values)
        cur = self.conn.execute(
            f"INSERT INTO alerts ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
            [values[c] for c in cols],
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def mark_delivered(self, alert_id: int) -> None:
        self.conn.execute("UPDATE alerts SET delivered = 1 WHERE id = ?", (alert_id,))
        self.conn.commit()

    def last_alert(self, chain: str, address: str) -> sqlite3.Row | None:
        return self.conn.execute(
            """SELECT * FROM alerts WHERE chain = ? AND token_address = ?
               ORDER BY ts DESC LIMIT 1""",
            (chain, address),
        ).fetchone()

    # ------------------------------------------------------------------ paper trades
    def alerts_to_track(self, since: float) -> list[sqlite3.Row]:
        """Alerts still inside their tracking window, plus any never evaluated."""
        return self.conn.execute(
            """SELECT a.* FROM alerts a LEFT JOIN outcomes o ON o.alert_id = a.id
               WHERE a.ts >= ? OR o.alert_id IS NULL ORDER BY a.ts""",
            (since,),
        ).fetchall()

    def add_alert_price(self, alert_id: int, ts: float, price: float) -> None:
        self.conn.execute("INSERT INTO alert_prices (alert_id, ts, price_usd) VALUES (?, ?, ?)",
                          (alert_id, ts, price))

    def price_observations(self, alert_id: int, chain: str, address: str,
                           start: float, end: float) -> list[tuple[float, float]]:
        """(ts, price) after the alert, from the tracker and from regular scans."""
        rows = self.conn.execute(
            """SELECT ts, price_usd FROM alert_prices WHERE alert_id = ? AND ts > ? AND ts <= ?
               UNION
               SELECT ts, price_usd FROM snapshots
               WHERE chain = ? AND token_address = ? AND ts > ? AND ts <= ? AND price_usd IS NOT NULL
               ORDER BY ts""",
            (alert_id, start, end, chain, address, start, end),
        ).fetchall()
        return [(r[0], r[1]) for r in rows if r[1] and r[1] > 0]

    def recently_snapshotted(self, chain: str, address: str, since: float) -> bool:
        return self.conn.execute(
            "SELECT 1 FROM snapshots WHERE chain = ? AND token_address = ? AND ts >= ? LIMIT 1",
            (chain, address, since),
        ).fetchone() is not None

    def save_outcome(self, alert_id: int, **values) -> None:
        cols = ["alert_id"] + list(values)
        updates = ", ".join(f"{c} = excluded.{c}" for c in values)
        self.conn.execute(
            f"""INSERT INTO outcomes ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})
                ON CONFLICT(alert_id) DO UPDATE SET {updates}""",
            [alert_id] + [values[c] for c in values],
        )

    def alerts_with_outcomes(self, since: float = 0) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT a.*, o.price_5m, o.price_15m, o.price_60m, o.price_4h, o.hit, o.hit_ts,
                      o.gross_return_pct, o.net_return_pct
               FROM alerts a LEFT JOIN outcomes o ON o.alert_id = a.id
               WHERE a.ts >= ? ORDER BY a.ts""",
            (since,),
        ).fetchall()
