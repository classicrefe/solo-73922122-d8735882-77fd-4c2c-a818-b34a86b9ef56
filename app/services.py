"""Transaction-bound services: ledger writes and summary rebuilds.

Every write path updates the ledger and the life-table summary in a single
SQLite transaction. If any step fails the transaction is rolled back and the
previous ledger and summary stay intact. Query helpers only touch the
summary tables.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .lifetable import PumpRecord, build_life_table, incidence_at
from .db import init_db

LIFE_TABLE_COLUMNS = (
    "t_hours", "entered", "at_risk", "failures", "preventive", "censored",
    "hazard_failure", "hazard_preventive", "survival",
    "cum_failure", "cum_preventive",
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _rebuild_summary(conn: sqlite3.Connection, records: list[PumpRecord]) -> None:
    rows = build_life_table(records)
    conn.execute("DELETE FROM life_table")
    placeholders = ",".join("?" for _ in LIFE_TABLE_COLUMNS)
    conn.executemany(
        f"INSERT INTO life_table ({','.join(LIFE_TABLE_COLUMNS)}) "
        f"VALUES ({placeholders})",
        [tuple(row[col] for col in LIFE_TABLE_COLUMNS) for row in rows],
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('pump_count', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (str(len(records)),),
    )
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('summary_updated_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_now(),),
    )


def _load_all_records(conn: sqlite3.Connection) -> list[PumpRecord]:
    return [
        PumpRecord(r["pump_id"], r["entry_hours"], r["exit_hours"], r["event_type"])
        for r in conn.execute(
            "SELECT pump_id, entry_hours, exit_hours, event_type FROM ledger"
        )
    ]


def replace_ledger(conn: sqlite3.Connection, records: list[PumpRecord],
                   source: str = "csv-upload") -> dict:
    """Replace the whole ledger and rebuild the summary atomically."""
    init_db(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM ledger")
        conn.executemany(
            "INSERT INTO ledger (pump_id, entry_hours, exit_hours, event_type, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            [(r.pump_id, r.entry_hours, r.exit_hours, r.event_type, _now())
             for r in records],
        )
        _rebuild_summary(conn, records)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_source', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (source,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"pump_count": len(records)}


def upsert_pump(conn: sqlite3.Connection, record: PumpRecord) -> dict:
    """Correct a single pump without the caller supplying other pumps.

    The summary is rebuilt inside the same transaction from the full ledger;
    callers never need to read or resubmit the other pumps' details.
    """
    init_db(conn)
    try:
        conn.execute("BEGIN IMMEDIATE")
        existing = conn.execute(
            "SELECT 1 FROM ledger WHERE pump_id = ?", (record.pump_id,)
        ).fetchone()
        action = "updated" if existing else "inserted"
        conn.execute(
            "INSERT INTO ledger (pump_id, entry_hours, exit_hours, event_type, updated_at)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(pump_id) DO UPDATE SET"
            " entry_hours=excluded.entry_hours,"
            " exit_hours=excluded.exit_hours,"
            " event_type=excluded.event_type,"
            " updated_at=excluded.updated_at",
            (record.pump_id, record.entry_hours, record.exit_hours,
             record.event_type, _now()),
        )
        _rebuild_summary(conn, _load_all_records(conn))
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_source', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"single-pump-{action}",),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"pump_id": record.pump_id, "action": action}


def get_pump(conn: sqlite3.Connection, pump_id: str) -> sqlite3.Row | None:
    init_db(conn)
    return conn.execute(
        "SELECT pump_id, entry_hours, exit_hours, event_type, updated_at"
        " FROM ledger WHERE pump_id = ?",
        (pump_id,),
    ).fetchone()


def list_pumps(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    init_db(conn)
    return list(conn.execute(
        "SELECT pump_id, entry_hours, exit_hours, event_type, updated_at"
        " FROM ledger ORDER BY pump_id"
    ))


# ---------------------------------------------------------------- queries --
# Everything below is read-only and only touches the summary tables.

def get_life_table(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    init_db(conn)
    return list(conn.execute(
        "SELECT * FROM life_table ORDER BY t_hours"
    ))


def get_meta(conn: sqlite3.Connection) -> dict[str, str]:
    init_db(conn)
    return {r["key"]: r["value"]
            for r in conn.execute("SELECT key, value FROM meta")}


def query_incidence(conn: sqlite3.Connection, hours: float) -> dict:
    """Read-only cumulative incidence query backed by the summary table."""
    if hours < 0:
        raise ValueError("查询小时数不能为负")
    init_db(conn)
    rows = [dict(r) for r in get_life_table(conn)]
    result = incidence_at(rows, hours)
    result["meta"] = get_meta(conn)
    return result
