"""Case-control matching for failed pumps (1:1, minimum-cost maximum flow).

Failure analysts pair each failed pump with one control pump whose
inspection archive is comparable. A control ``c`` is eligible for a failed
pump ``f`` when all of the following hold:

1. the control is already on the register at the failure hour
   (``entry_hours(c) <= exit_hours(f)``);
2. the control leaves the register after that hour, or observation ends
   exactly at that hour
   (``exit_hours(c) > exit_hours(f)`` or
   (``exit_hours(c) == exit_hours(f)`` and event == 'censored'));
3. the absolute difference of the two pumps' entry hours does not exceed
   the analyst-supplied limit (``|entry_f - entry_c| <= max_diff``);
4. the control is not itself one of the selected failed pumps.

Each control may be used at most once. The optimisation first maximises
the number of matched pairs; among all maximum-cardinality matchings it
keeps as many pairs from the previous roster as possible. Remaining ties
are broken deterministically (smaller entry-hour difference, then pump
ids) so reruns produce identical output.

The optimiser is a minimum-cost maximum-flow network:

    source -> failed-pump nodes (cap 1) -> control nodes (cap 1) -> sink

Edge cost components are encoded in a single scalar with bases large
enough to keep the lexicographic ordering:

    cost = RETAIN_BASE * retained_penalty
         + DIFF_BASE   * diff_rank
         + edge_index

A retained previous pair has retained_penalty 0, every other pair has 1,
so the flow first maximises pair count (every unit of flow costs far less
than the retain penalty) and then maximises retained pairs.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from .lifetable import PumpRecord

HOURS_EPS = 1e-9
MAX_FAILURE_PICKS = 20
MAX_CONTROL_PICKS = 60

# Cost hierarchy (all encoded in one integer; Python handles big ints):
#   1. flow cardinality is structural -- the network always pushes the
#      maximum possible amount of flow regardless of edge cost;
#   2. retention (retained pair costs 0, every other pair costs
#      RETAIN_BASE), so among maximum matchings retention is maximised;
#   3. sum of per-failure entry-diff ranks;
#   4. a canonical, input-order-independent tie-break: failures in pump-id
#      order each take the smallest globally-ranked control id. Position
#      weights K**(n-1-i) make this a strict lexicographic ordering.
K = MAX_CONTROL_PICKS + 1


@dataclass(frozen=True)
class Pair:
    failure_id: str
    control_id: str
    failure_hours: float
    failure_entry: float
    control_entry: float
    diff_hours: float
    retained: bool
    control_event: str
    control_exit_hours: float


@dataclass(frozen=True)
class FailureStatus:
    pump_id: str
    state: str          # 'matched' | 'unmatched' | 'not_failure' | 'missing'
    state_label: str
    failure_hours: float | None
    reason: str


STATE_LABELS = {
    "matched": "已配对",
    "unmatched": "尚未配到对照",
    "not_failure": "已改为非故障",
    "missing": "缺记录",
}


def is_eligible(failure: PumpRecord, control: PumpRecord,
                max_diff: float) -> bool:
    """Pure eligibility check for one (failure, control) candidate pair."""
    t = failure.exit_hours
    if abs(control.entry_hours - failure.entry_hours) > max_diff + HOURS_EPS:
        return False
    if control.entry_hours > t + HOURS_EPS:
        return False
    if control.exit_hours > t + HOURS_EPS:
        return True
    if abs(control.exit_hours - t) <= HOURS_EPS and control.event_type == "censored":
        return True
    return False


class _Edge:
    __slots__ = ("to", "rev", "cap", "cost")

    def __init__(self, to: int, rev: int, cap: int, cost: int):
        self.to = to
        self.rev = rev
        self.cap = cap
        self.cost = cost


class _MinCostFlow:
    """Successive shortest augmenting path with Bellman-Ford (no neg edges)."""

    def __init__(self, n: int):
        self.n = n
        self.g: list[list[_Edge]] = [[] for _ in range(n)]

    def add_edge(self, fr: int, to: int, cap: int, cost: int) -> None:
        self.g[fr].append(_Edge(to, len(self.g[to]), cap, cost))
        self.g[to].append(_Edge(fr, len(self.g[fr]) - 1, 0, -cost))

    def flow(self, s: int, t: int, maxf: int) -> tuple[int, int]:
        total_flow = 0
        total_cost = 0
        n = self.n
        while total_flow < maxf:
            dist = [None] * n
            dist[s] = 0
            prev_v = [-1] * n
            prev_e = [-1] * n
            in_queue = [False] * n
            queue = deque([s])
            in_queue[s] = True
            while queue:
                v = queue.popleft()
                in_queue[v] = False
                dv = dist[v]
                for ei, e in enumerate(self.g[v]):
                    if e.cap <= 0:
                        continue
                    nd = dv + e.cost
                    if dist[e.to] is None or nd < dist[e.to]:
                        dist[e.to] = nd
                        prev_v[e.to] = v
                        prev_e[e.to] = ei
                        if not in_queue[e.to]:
                            queue.append(e.to)
                            in_queue[e.to] = True
            if dist[t] is None:
                break
            v = t
            while v != s:
                e = self.g[prev_v[v]][prev_e[v]]
                e.cap -= 1
                self.g[v][e.rev].cap += 1
                v = prev_v[v]
            total_flow += 1
            total_cost += dist[t]
        return total_flow, total_cost

    def edge_used(self, fr: int, edge_index: int) -> bool:
        # forward edges store residual capacity; original cap was 1, so
        # cap == 0 means the edge carries one unit of flow.
        return self.g[fr][edge_index].cap == 0


def _edge_cost(retained: bool, diff_rank: int, control_rank: int,
               failure_position: int, failure_count: int) -> int:
    """Lexicographic cost: retention > diff rank > canonical control id.

    Position weights make the canonical tie-break strictly ordered by
    failure position: the earliest failure in pump-id order gets the
    smallest control rank, then the next, and so on.
    """
    retain_penalty = 0 if retained else 1
    retain_base = 10_000 * (K ** failure_count)
    diff_base = K ** failure_count
    position_weight = K ** (failure_count - 1 - failure_position)
    return (retain_penalty * retain_base
            + diff_rank * diff_base
            + control_rank * position_weight)


def build_matching(
    records: list[PumpRecord],
    failure_ids: list[str],
    candidate_ids: list[str],
    max_diff: float,
    previous_pairs: dict[str, str] | None = None,
) -> dict:
    """Run the full matching and return pairs plus per-failure status.

    ``previous_pairs`` maps failure pump id -> control pump id from the
    last saved roster and drives the retention tie-breaker.
    """
    previous_pairs = previous_pairs or {}
    by_id = {r.pump_id: r for r in records}
    failure_set = set(failure_ids)
    failure_order = {pid: i for i, pid in enumerate(failure_ids)}

    failures: list[PumpRecord] = []
    statuses: list[FailureStatus] = []
    for fid in failure_ids:
        rec = by_id.get(fid)
        if rec is None:
            statuses.append(FailureStatus(
                fid, "missing", STATE_LABELS["missing"], None,
                "台账中已无该泵记录"))
            continue
        if rec.event_type != "failure":
            statuses.append(FailureStatus(
                fid, "not_failure", STATE_LABELS["not_failure"],
                rec.exit_hours, f"当前结束方式为 {rec.event_type}，不再是故障泵"))
            continue
        failures.append(rec)

    # A selected failed pump can never serve as a control.
    controls: list[PumpRecord] = [
        by_id[cid] for cid in candidate_ids
        if cid in by_id and cid not in failure_set
    ]
    # Canonical order makes the optimum independent of the order in which
    # ids were submitted; missing / non-failure statuses keep the original
    # selection order separately below.
    failures.sort(key=lambda r: r.pump_id)
    controls.sort(key=lambda r: r.pump_id)
    control_index = {c.pump_id: j for j, c in enumerate(controls)}

    source = 0
    f_base = 1
    c_base = f_base + len(failures)
    sink = c_base + len(controls)
    mcf = _MinCostFlow(sink + 1)
    for i in range(len(failures)):
        mcf.add_edge(source, f_base + i, 1, 0)
    for j in range(len(controls)):
        mcf.add_edge(c_base + j, sink, 1, 0)

    # edges[i] records the forward-edge index and metadata for failure i.
    edges: list[list[dict]] = [[] for _ in failures]
    n_failures = len(failures)
    for i, f in enumerate(failures):
        raw: list[tuple[PumpRecord, float]] = []
        for c in controls:
            if is_eligible(f, c, max_diff):
                raw.append((c, abs(f.entry_hours - c.entry_hours)))
        raw.sort(key=lambda item: (item[1], item[0].pump_id))
        for rank, (c, diff) in enumerate(raw):
            retained = previous_pairs.get(f.pump_id) == c.pump_id
            edge_index = len(mcf.g[f_base + i])
            mcf.add_edge(
                f_base + i, c_base + control_index[c.pump_id], 1,
                _edge_cost(retained, rank, control_index[c.pump_id],
                           i, n_failures),
            )
            edges[i].append({
                "edge_index": edge_index,
                "control": c,
                "diff": diff,
                "retained": retained,
            })

    pair_count, total_cost = mcf.flow(source, sink, len(failures))

    pairs: list[Pair] = []
    matched_failures: set[str] = set()
    used_controls: set[str] = set()
    for i, f in enumerate(failures):
        for info in edges[i]:
            if mcf.edge_used(f_base + i, info["edge_index"]):
                c = info["control"]
                pairs.append(Pair(
                    failure_id=f.pump_id,
                    control_id=c.pump_id,
                    failure_hours=f.exit_hours,
                    failure_entry=f.entry_hours,
                    control_entry=c.entry_hours,
                    diff_hours=info["diff"],
                    retained=info["retained"],
                    control_event=c.event_type,
                    control_exit_hours=c.exit_hours,
                ))
                matched_failures.add(f.pump_id)
                used_controls.add(c.pump_id)
                break
    pairs.sort(key=lambda p: p.failure_id)

    for f in failures:
        if f.pump_id not in matched_failures:
            statuses.append(FailureStatus(
                f.pump_id, "unmatched", STATE_LABELS["unmatched"],
                f.exit_hours,
                "没有满足在册/出册/工时差条件且尚未占用的对照泵"))
    statuses.sort(key=lambda s: failure_order[s.pump_id])

    retained_count = sum(1 for p in pairs if p.retained)
    return {
        "pairs": pairs,
        "statuses": statuses,
        "pair_count": pair_count,
        "retained_count": retained_count,
        "failure_total": len(failure_ids),
        "eligible_failure_total": len(failures),
        "candidate_total": len(candidate_ids),
        "control_total": len(controls),
        "used_controls": sorted(used_controls),
        "total_cost": total_cost,
    }
