"""SQLite connection management and schema for the pump reliability app."""
from __future__ import annotations

import os
import sqlite3

DEFAULT_DB_PATH = os.environ.get(
    "PUMP_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "instance", "pumps.db"),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    pump_id     TEXT PRIMARY KEY,
    entry_hours REAL NOT NULL CHECK (entry_hours >= 0),
    exit_hours  REAL NOT NULL CHECK (exit_hours >= entry_hours),
    event_type  TEXT NOT NULL CHECK (event_type IN ('failure', 'preventive', 'censored')),
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS life_table (
    t_hours          REAL PRIMARY KEY,
    entered          INTEGER NOT NULL,
    at_risk          INTEGER NOT NULL,
    failures         INTEGER NOT NULL,
    preventive       INTEGER NOT NULL,
    censored         INTEGER NOT NULL,
    hazard_failure   REAL NOT NULL,
    hazard_preventive REAL NOT NULL,
    survival         REAL NOT NULL,
    cum_failure      REAL NOT NULL,
    cum_preventive   REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def get_db_path() -> str:
    return DEFAULT_DB_PATH


def connect(db_path: str | None = None) -> sqlite3.Connection:
    """Open a connection with WAL and foreign keys enabled."""
    path = db_path or DEFAULT_DB_PATH
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()