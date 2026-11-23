"""Life-table computation for pump cumulative failure incidence.

Each pump contributes one record:
    entry_hours  -- cumulative operating hours when the pump joined the register
    exit_hours   -- cumulative operating hours when the pump left the register
    event_type   -- 'failure' | 'preventive' | 'censored'

Time axis is operating hours (not calendar time). The time-ordered convention
at every distinct event/exit hour t is:

  1. count pumps entering the risk set at t (delayed entry);
  2. compute failure and preventive-replacement incidence rates against the
     number at risk at t;
  3. remove every pump leaving at t (failures, preventive replacements and
     censored/observation-ended pumps all leave the risk set).

Cumulative failure incidence is the Aalen-Johansen estimator with preventive
replacement treated as a competing event:

    S(t)      = product over u <= t of (1 - d_f(u)/n(u) - d_p(u)/n(u))
    F_fail(t) = sum over u <= t of S(u-) * d_f(u) / n(u)
    F_prev(t) = sum over u <= t of S(u-) * d_p(u) / n(u)

Censoring only shrinks the risk set; it carries no event probability.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

EVENT_TYPES = ("failure", "preventive", "censored")
EVENT_LABELS = {
    "failure": "故障",
    "preventive": "预防更换",
    "censored": "观察结束",
}


@dataclass(frozen=True)
class PumpRecord:
    pump_id: str
    entry_hours: float
    exit_hours: float
    event_type: str


def _validate(records: list[PumpRecord]) -> None:
    seen: set[str] = set()
    for r in records:
        if not r.pump_id or not str(r.pump_id).strip():
            raise ValueError("存在泵号为空的记录")
        if r.pump_id in seen:
            raise ValueError(f"泵号重复: {r.pump_id}")
        seen.add(r.pump_id)
        if r.entry_hours < 0:
            raise ValueError(f"泵 {r.pump_id}: 入册小时数不能为负")
        if r.exit_hours < r.entry_hours:
            raise ValueError(
                f"泵 {r.pump_id}: 出册小时数 ({r.exit_hours}) 不能早于入册 ({r.entry_hours})"
            )
        if r.event_type not in EVENT_TYPES:
            raise ValueError(f"泵 {r.pump_id}: 未知事件类型 {r.event_type!r}")


def build_life_table(records: list[PumpRecord]) -> list[dict]:
    """Return one life-table row per distinct event/exit hour.

    Rows are ordered by hour. Each row carries the point-in-time counts used
    in the calculation (entered, at_risk, failures, preventive, censored),
    the two cause-specific hazards, the survival probability and the two
    cumulative incidence functions.
    """
    _validate(records)

    entries_at: dict[float, int] = defaultdict(int)
    exits: dict[float, dict[str, int]] = defaultdict(
        lambda: {"failure": 0, "preventive": 0, "censored": 0}
    )
    time_points: set[float] = set()

    for r in records:
        entries_at[r.entry_hours] += 1
        time_points.add(r.entry_hours)
        exits[r.exit_hours][r.event_type] += 1
        time_points.add(r.exit_hours)

    at_risk = 0
    survival = 1.0          # probability of no event of either type so far
    cum_failure = 0.0
    cum_preventive = 0.0
    rows: list[dict] = []

    for t in sorted(time_points):
        entered = entries_at.get(t, 0)
        at_risk += entered  # step 1: delayed entries count first

        d_f = exits[t]["failure"]
        d_p = exits[t]["preventive"]
        d_c = exits[t]["censored"]
        n = at_risk

        hazard_f = d_f / n if n > 0 else 0.0
        hazard_p = d_p / n if n > 0 else 0.0

        # step 2: weight the hazards by the probability of still being
        # event-free just before t, then accumulate.
        survival_before = survival
        cum_failure += survival_before * hazard_f
        cum_preventive += survival_before * hazard_p
        survival *= (1.0 - hazard_f - hazard_p)
        if survival < 0.0:
            survival = 0.0  # guard against rounding noise

        rows.append({
            "t_hours": float(t),
            "entered": entered,
            "at_risk": n,
            "failures": d_f,
            "preventive": d_p,
            "censored": d_c,
            "hazard_failure": hazard_f,
            "hazard_preventive": hazard_p,
            "survival": survival,
            "cum_failure": cum_failure,
            "cum_preventive": cum_preventive,
        })

        # step 3: all exits (events and censoring) leave the risk set
        at_risk -= d_f + d_p + d_c

    return rows


def incidence_at(rows: list[dict], hours: float) -> dict:
    """Cumulative failure incidence at an arbitrary hour value.

    Uses the last life-table row whose time point is <= ``hours`` (step
    function). Before the first time point the incidence is zero. Also
    returns the row actually used so the UI can show the point-in-time
    counts behind the answer.
    """
    chosen: dict | None = None
    for row in rows:
        if row["t_hours"] <= hours + 1e-9:
            chosen = row
        else:
            break
    if chosen is None:
        return {
            "hours": float(hours),
            "cum_failure": 0.0,
            "cum_preventive": 0.0,
            "survival": 1.0,
            "basis_row": None,
        }
    return {
        "hours": float(hours),
        "cum_failure": chosen["cum_failure"],
        "cum_preventive": chosen["cum_preventive"],
        "survival": chosen["survival"],
        "basis_row": chosen,
    }
