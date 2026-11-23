"""Transaction-bound services: ledger writes, summary and roster rebuilds.

Every write path updates the ledger, the life-table summary and the
case-control roster in a single SQLite transaction. If any step fails the
transaction is rolled back and the previous ledger, summary and roster
stay intact. Query helpers only touch the summary/roster tables.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .lifetable import PumpRecord, build_life_table, incidence_at
from .matching import (MAX_CONTROL_PICKS, MAX_FAILURE_PICKS,
                       STATE_LABELS, build_matching)
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


# ---------------------------------------------------------------- roster --
def _load_roster_spec(conn: sqlite3.Connection) -> dict | None:
    """Pure SELECT helper; must not commit (it runs inside write txns)."""
    spec = conn.execute(
        "SELECT max_diff_hours, updated_at FROM roster_spec WHERE id = 1"
    ).fetchone()
    if spec is None:
        return None
    failures = [r["pump_id"] for r in conn.execute(
        "SELECT pump_id FROM roster_failures WHERE roster_id = 1 ORDER BY pick_order"
    )]
    candidates = [r["pump_id"] for r in conn.execute(
        "SELECT pump_id FROM roster_candidates WHERE roster_id = 1 ORDER BY pick_order"
    )]
    previous_pairs = {
        r["failure_id"]: r["control_id"]
        for r in conn.execute(
            "SELECT failure_id, control_id FROM roster_pairs WHERE roster_id = 1"
        )
    }
    return {
        "max_diff_hours": spec["max_diff_hours"],
        "updated_at": spec["updated_at"],
        "failure_ids": failures,
        "candidate_ids": candidates,
        "previous_pairs": previous_pairs,
    }


def _persist_roster_pairs(conn: sqlite3.Connection, result: dict) -> None:
    conn.execute("DELETE FROM roster_pairs WHERE roster_id = 1")
    conn.executemany(
        "INSERT INTO roster_pairs (roster_id, failure_id, control_id,"
        " diff_hours, retained, pick_order) VALUES (1, ?, ?, ?, ?, ?)",
        [(p.failure_id, p.control_id, p.diff_hours, 1 if p.retained else 0, order)
         for order, p in enumerate(result["pairs"])],
    )


def _rebuild_roster(conn: sqlite3.Connection,
                    records: list[PumpRecord]) -> dict | None:
    """Re-run the matching inside the current transaction.

    Called after every ledger write. The saved selection (failure pumps,
    candidate controls, hour-difference limit) is kept; only the resulting
    pairs change. Returns the matching result, or ``None`` when no roster
    has ever been saved.
    """
    spec = _load_roster_spec(conn)
    if spec is None:
        return None
    result = build_matching(
        records,
        spec["failure_ids"],
        spec["candidate_ids"],
        spec["max_diff_hours"],
        previous_pairs=spec["previous_pairs"],
    )
    _persist_roster_pairs(conn, result)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('roster_updated_at', ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_now(),),
    )
    return result


def save_roster(conn: sqlite3.Connection, failure_ids: list[str],
                candidate_ids: list[str], max_diff: float) -> dict:
    """Validate and persist a roster specification, then build the pairs.

    The ledger, life-table summary and roster are refreshed in one
    transaction; any failure rolls everything back and the previous
    roster stays in force.
    """
    init_db(conn)
    failures_dedup = list(dict.fromkeys(failure_ids))
    candidates_dedup = list(dict.fromkeys(candidate_ids))
    errors: list[str] = []
    if not failures_dedup:
        errors.append("至少选择 1 台故障泵")
    if len(failures_dedup) > MAX_FAILURE_PICKS:
        errors.append(f"故障泵最多选择 {MAX_FAILURE_PICKS} 台"
                      f"（当前 {len(failures_dedup)} 台）")
    if not candidates_dedup:
        errors.append("至少选择 1 台候选对照泵")
    if len(candidates_dedup) > MAX_CONTROL_PICKS:
        errors.append(f"候选对照泵最多选择 {MAX_CONTROL_PICKS} 台"
                      f"（当前 {len(candidates_dedup)} 台）")
    if not isinstance(max_diff, (int, float)) or max_diff < 0:
        errors.append("入册工时差上限必须是非负数字")
    overlap = set(failures_dedup) & set(candidates_dedup)
    if overlap:
        errors.append("已选故障泵不能同时作为候选对照: "
                      + "、".join(sorted(overlap)))
    if errors:
        raise ValueError("名单参数有误：\n" + "\n".join(errors))

    try:
        conn.execute("BEGIN IMMEDIATE")
        records = _load_all_records(conn)
        by_id = {r.pump_id: r for r in records}
        unknown = [pid for pid in failures_dedup + candidates_dedup
                   if pid not in by_id]
        if unknown:
            raise ValueError("以下泵号在台账中不存在: " + "、".join(unknown))
        not_failures = [pid for pid in failures_dedup
                        if by_id[pid].event_type != "failure"]
        if not_failures:
            raise ValueError("以下所选泵当前不是故障泵（结束方式非故障）: "
                             + "、".join(not_failures))

        previous = _load_roster_spec(conn)
        previous_pairs = previous["previous_pairs"] if previous else {}

        conn.execute(
            "INSERT INTO roster_spec (id, max_diff_hours, updated_at)"
            " VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET"
            " max_diff_hours=excluded.max_diff_hours,"
            " updated_at=excluded.updated_at",
            (float(max_diff), _now()),
        )
        conn.execute("DELETE FROM roster_failures WHERE roster_id = 1")
        conn.executemany(
            "INSERT INTO roster_failures (roster_id, pump_id, pick_order)"
            " VALUES (1, ?, ?)",
            [(pid, i) for i, pid in enumerate(failures_dedup)],
        )
        conn.execute("DELETE FROM roster_candidates WHERE roster_id = 1")
        conn.executemany(
            "INSERT INTO roster_candidates (roster_id, pump_id, pick_order)"
            " VALUES (1, ?, ?)",
            [(pid, i) for i, pid in enumerate(candidates_dedup)],
        )
        result = build_matching(records, failures_dedup, candidates_dedup,
                                float(max_diff), previous_pairs)
        _persist_roster_pairs(conn, result)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('roster_updated_at', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_now(),),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {
        "failure_count": len(failures_dedup),
        "candidate_count": len(candidates_dedup),
        "max_diff_hours": float(max_diff),
        "pair_count": result["pair_count"],
        "retained_count": result["retained_count"],
    }


def get_roster(conn: sqlite3.Connection) -> dict | None:
    """Read-only view of the current roster with live status annotations.

    The matching is recomputed without a retention preference (the stored
    pairs are the *current* roster, not a previous one to retain); the
    displayed retained flags come straight from the persisted rows so they
    reflect what the last rebuild actually kept.
    """
    init_db(conn)
    spec = _load_roster_spec(conn)
    if spec is None:
        return None
    records = _load_all_records(conn)
    result = build_matching(
        records,
        spec["failure_ids"],
        spec["candidate_ids"],
        spec["max_diff_hours"],
    )
    stored_retained = {
        r["failure_id"]: bool(r["retained"])
        for r in conn.execute(
            "SELECT failure_id, retained FROM roster_pairs WHERE roster_id = 1"
        )
    }
    by_id = {r.pump_id: r for r in records}
    status_by_id = {s.pump_id: s for s in result["statuses"]}
    failures_view = []
    for fid in spec["failure_ids"]:
        st = status_by_id.get(fid)
        rec = by_id.get(fid)
        failures_view.append({
            "pump_id": fid,
            "state": st.state if st else "matched",
            "state_label": st.state_label if st else STATE_LABELS["matched"],
            "reason": st.reason if st else "",
            "failure_hours": st.failure_hours if st else rec.exit_hours,
            "entry_hours": rec.entry_hours if rec else None,
            "exists": rec is not None,
            "event_type": rec.event_type if rec else None,
        })
    candidates_view = []
    used = set(result["used_controls"])
    failure_set = set(spec["failure_ids"])
    for cid in spec["candidate_ids"]:
        rec = by_id.get(cid)
        pair = next((p for p in result["pairs"] if p.control_id == cid), None)
        candidates_view.append({
            "pump_id": cid,
            "exists": rec is not None,
            "entry_hours": rec.entry_hours if rec else None,
            "exit_hours": rec.exit_hours if rec else None,
            "event_type": rec.event_type if rec else None,
            "is_selected_failure": cid in failure_set,
            "used": cid in used,
            "paired_with": pair.failure_id if pair else None,
        })
    return {
        "max_diff_hours": spec["max_diff_hours"],
        "updated_at": spec["updated_at"],
        "failures": failures_view,
        "candidates": candidates_view,
        "pairs": [
            {
                "failure_id": p.failure_id,
                "control_id": p.control_id,
                "failure_hours": p.failure_hours,
                "failure_entry": p.failure_entry,
                "control_entry": p.control_entry,
                "diff_hours": p.diff_hours,
                "retained": stored_retained.get(p.failure_id, False),
                "control_event": p.control_event,
                "control_exit_hours": p.control_exit_hours,
            }
            for p in result["pairs"]
        ],
        "stats": {
            "failure_total": result["failure_total"],
            "eligible_failure_total": result["eligible_failure_total"],
            "pair_count": result["pair_count"],
            "retained_count": sum(
                1 for p in result["pairs"]
                if stored_retained.get(p.failure_id, False)),
            "unmatched_count": sum(
                1 for s in result["statuses"] if s.state == "unmatched"),
            "missing_count": sum(
                1 for s in result["statuses"] if s.state == "missing"),
            "not_failure_count": sum(
                1 for s in result["statuses"] if s.state == "not_failure"),
            "candidate_total": result["candidate_total"],
            "control_total": result["control_total"],
            "used_control_count": len(result["used_controls"]),
        },
    }


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
        roster_result = _rebuild_roster(conn, records)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_source', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (source,),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"pump_count": len(records),
            "pair_count": roster_result["pair_count"] if roster_result else None}


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
        full_records = _load_all_records(conn)
        _rebuild_summary(conn, full_records)
        roster_result = _rebuild_roster(conn, full_records)
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('last_source', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"single-pump-{action}",),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return {"pump_id": record.pump_id, "action": action,
            "pair_count": roster_result["pair_count"] if roster_result else None}


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
