"""CypherTrace Temporal Loop Detection (Circular Transaction Analysis: A -> B -> C -> A)."""

from __future__ import annotations

import datetime as dt
import time
import uuid
from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple

from cyphersol.models import (
    BankAccountStatement,
    CircularLoop,
    NormalizedTransaction,
    ResolvedEntity,
)
from cyphersol.trace.graph_engine import DirectedTransferEvent, event_datetime, extract_directed_transfer_events

MIN_CYCLE_LENGTH = 2
MAX_CYCLE_LENGTH = 8
DEFAULT_CYCLE_LENGTH = 8  # FR-TRACE-04: cycles of 2 to 8 hops by default (BE-09 time budget bounds the cost)
DEFAULT_TIME_BUDGET_S = 5.0
DEFAULT_MAX_CYCLES = 1000
DEFAULT_MAX_INSTANCES_PER_LOOP = 50
_BUDGET_CHECK_EVERY = 2048
_EPOCH = dt.datetime(1970, 1, 1)


class LoopDetectionResult(list):
    """
    List of CircularLoop (backward compatible with the previous return type) carrying run metadata:
    `truncated` is True when the wall-clock budget or the max_cycles cap stopped the search early.
    """

    def __init__(self, loops: List[CircularLoop] = (), **meta: Any) -> None:
        super().__init__(loops)
        self.truncated: bool = bool(meta.get("truncated", False))
        self.truncation_reason: Optional[str] = meta.get("truncation_reason")
        self.elapsed_s: float = float(meta.get("elapsed_s", 0.0))
        self.max_cycle_length: int = int(meta.get("max_cycle_length", DEFAULT_CYCLE_LENGTH))
        self.candidate_events: int = int(meta.get("candidate_events", 0))

    def meta(self) -> Dict[str, Any]:
        return {
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
            "elapsed_s": round(self.elapsed_s, 3),
            "max_cycle_length": self.max_cycle_length,
            "candidate_events": self.candidate_events,
            "loop_count": len(self),
        }


@dataclass
class DetailedCircularLoop(CircularLoop):
    """
    CircularLoop with per-intermediary leakage (TF-13) and every temporal instance of the same cycle (TF-14).
    The inherited fields describe the representative (earliest) instance. Each hop additionally carries
    from/to entity ids, hop_index, hours_since_previous_hop, and for every hop after the first the leakage
    retained by the intermediary that received the previous hop (previous amount - this amount).
    """

    total_leakage: float = 0.0
    leakage_pct: float = 0.0
    intermediary_leakage: List[Dict[str, Any]] = field(default_factory=list)
    instance_count: int = 1
    disjoint_instance_count: int = 1
    instances_truncated: bool = False
    instances: List[Dict[str, Any]] = field(default_factory=list)
    total_instance_volume: float = 0.0


def _hop_rows(chosen_ev: List[DirectedTransferEvent], times: List[float]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for k, ev in enumerate(chosen_ev):
        row: Dict[str, Any] = {
            "hop_index": k + 1,
            "tx_id": ev.tx_id,
            "from": ev.src_name,
            "to": ev.dst_name,
            "from_entity_id": ev.src_entity_id,
            "to_entity_id": ev.dst_entity_id,
            "amount": ev.amount,
            "date": ev.txn_date,
            "timestamp": ev.txn_timestamp,
            "channel": ev.channel,
            "utr": ev.utr_number,
            "hours_since_previous_hop": None if k == 0 else round((times[k] - times[k - 1]) / 3600.0, 2),
            "leakage": None,
            "leakage_pct": None,
        }
        if k > 0:
            prev_amt = chosen_ev[k - 1].amount
            leak = round(prev_amt - ev.amount, 2)
            row["leakage"] = leak
            row["leakage_pct"] = round(100.0 * leak / prev_amt, 2) if prev_amt > 0 else None
        rows.append(row)
    return rows


def _instance_summary(chosen_ev: List[DirectedTransferEvent], times: List[float]) -> Dict[str, Any]:
    amounts = [ev.amount for ev in chosen_ev]
    lo, hi = min(amounts), max(amounts)
    return {
        "tx_ids": [ev.tx_id for ev in chosen_ev],
        "amounts": amounts,
        "utrs": [ev.utr_number for ev in chosen_ev],
        "start_date": chosen_ev[0].txn_date,
        "end_date": chosen_ev[-1].txn_date,
        "start_timestamp": chosen_ev[0].txn_timestamp,
        "end_timestamp": chosen_ev[-1].txn_timestamp,
        "min_amount": round(lo, 2),
        "max_amount": round(hi, 2),
        "retention_ratio": round(lo / max(1e-9, hi), 4),
        "duration_hours": round((times[-1] - times[0]) / 3600.0, 2),
        "leakage": round(amounts[0] - amounts[-1], 2),
    }


class _Stop(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _find_simple_cycles(adj: Dict[str, Set[str]], max_len: int = 6) -> List[List[str]]:
    """Bounded simple (structural) cycle enumeration up to max_len hops. Retained for callers/tests."""
    nodes = sorted(adj.keys())
    cycles: List[List[str]] = []

    for start in nodes:
        path = [start]
        visited = {start}

        def dfs(curr: str) -> None:
            for nxt in sorted(adj.get(curr, ())):
                if nxt == start and len(path) >= 2:
                    cycles.append(list(path))
                elif nxt > start and nxt not in visited and len(path) < max_len:
                    visited.add(nxt)
                    path.append(nxt)
                    dfs(nxt)
                    path.pop()
                    visited.remove(nxt)

        dfs(start)
    return cycles


def _strongly_connected(nodes: List[str], adj: Dict[str, Set[str]]) -> Dict[str, int]:
    """Iterative Tarjan SCC; returns node -> component index."""
    index: Dict[str, int] = {}
    low: Dict[str, int] = {}
    on_stack: Set[str] = set()
    stack: List[str] = []
    comp: Dict[str, int] = {}
    counter = 0
    n_comp = 0
    for root in nodes:
        if root in index:
            continue
        work: List[Tuple[str, Any]] = [(root, iter(adj.get(root, ())))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(adj.get(w, ()))))
                    advanced = True
                    break
                if w in on_stack and index[w] < low[v]:
                    low[v] = index[w]
            if advanced:
                continue
            work.pop()
            if work:
                parent = work[-1][0]
                if low[v] < low[parent]:
                    low[parent] = low[v]
            if low[v] == index[v]:
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp[w] = n_comp
                    if w == v:
                        break
                n_comp += 1
    return comp


def detect_circular_loops(
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    max_cycle_days: int = 30,
    min_retention_ratio: float = 0.50,
    max_hops: int = DEFAULT_CYCLE_LENGTH,
    max_cycle_length: Optional[int] = None,
    time_budget_s: Optional[float] = DEFAULT_TIME_BUDGET_S,
    max_cycles: int = DEFAULT_MAX_CYCLES,
    max_instances_per_loop: int = DEFAULT_MAX_INSTANCES_PER_LOOP,
) -> LoopDetectionResult:
    """
    Detect temporally valid circular transaction loops (A -> B -> C -> A):
      - Monotonic non-decreasing timestamps along the cycle
      - Total loop duration <= max_cycle_days
      - Value retention ratio (min_amt / max_amt) >= min_retention_ratio
      - 2 <= hops <= max_cycle_length (default 6, validated against 2..8); `max_hops` is the legacy alias
    Search is a backtracking DFS over concrete transfer events (TF-10), restricted to each start event's time
    window via bisect on per-source time-sorted events, to strongly connected components, and to nodes that can
    still reach the start within the remaining hops (BE-09/TF-12). System parties (cash, ATM, charges,
    UNRESOLVED_*) are never part of a loop (NLP-10). One DetailedCircularLoop is reported per cycle signature:
    its main fields describe the earliest instance, `instances` lists up to `max_instances_per_loop` temporal
    instances (chronological), `instance_count` counts all found and `disjoint_instance_count` the greedy
    maximum of stored instances sharing no transaction (TF-14). Hops carry date, UTR and per-intermediary
    leakage (TF-13). The search stops at `time_budget_s` seconds or `max_cycles` signatures and sets
    `result.truncated`.
    """
    started = time.monotonic()
    cycle_len = max_hops if max_cycle_length is None else max_cycle_length
    if not isinstance(cycle_len, int) or isinstance(cycle_len, bool) or not (
        MIN_CYCLE_LENGTH <= cycle_len <= MAX_CYCLE_LENGTH
    ):
        raise ValueError(f"max_cycle_length must be an integer between {MIN_CYCLE_LENGTH} and {MAX_CYCLE_LENGTH}")
    if not (0.0 <= float(min_retention_ratio) <= 1.0):
        raise ValueError("min_retention_ratio must be between 0 and 1")
    if max_cycle_days is None or float(max_cycle_days) < 0:
        raise ValueError("max_cycle_days must be non-negative")
    if max_cycles is not None and int(max_cycles) < 1:
        raise ValueError("max_cycles must be >= 1")
    if max_instances_per_loop is None or int(max_instances_per_loop) < 1:
        raise ValueError("max_instances_per_loop must be >= 1")
    inst_cap = int(max_instances_per_loop)

    events, entity_by_id = extract_directed_transfer_events(statements, transactions, entities)
    events = [
        ev for ev in events
        if ev.src_entity_id != ev.dst_entity_id and not ev.src_is_system and not ev.dst_is_system and ev.amount > 0
    ]

    # Structural pruning: only events inside a non-trivial strongly connected component can lie on a cycle
    struct: Dict[str, Set[str]] = defaultdict(set)
    for ev in events:
        struct[ev.src_entity_id].add(ev.dst_entity_id)
    nodes = sorted(set(struct) | {v for t in struct.values() for v in t})
    comp = _strongly_connected(nodes, struct)
    comp_size: Dict[int, int] = defaultdict(int)
    for c in comp.values():
        comp_size[c] += 1
    events = [
        ev for ev in events
        if comp[ev.src_entity_id] == comp[ev.dst_entity_id] and comp_size[comp[ev.src_entity_id]] >= 2
    ]

    times = [(event_datetime(ev) - _EPOCH).total_seconds() for ev in events]
    by_src_t: Dict[str, List[float]] = defaultdict(list)
    by_src_i: Dict[str, List[int]] = defaultdict(list)
    rev: Dict[str, Set[str]] = defaultdict(set)
    for i, ev in enumerate(events):  # events are already chronological
        by_src_t[ev.src_entity_id].append(times[i])
        by_src_i[ev.src_entity_id].append(i)
        rev[ev.dst_entity_id].add(ev.src_entity_id)

    window_s = float(max_cycle_days) * 86400.0
    rho = float(min_retention_ratio)
    deadline = None if not time_budget_s or time_budget_s <= 0 else started + float(time_budget_s)
    cap = int(max_cycles) if max_cycles else None

    found: Dict[Tuple[str, ...], List[List[int]]] = {}
    found_count: Dict[Tuple[str, ...], int] = defaultdict(int)
    ops = [0]
    truncated_reason: Optional[str] = None

    def tick() -> None:
        ops[0] += 1
        if deadline is not None and ops[0] % _BUDGET_CHECK_EVERY == 0 and time.monotonic() > deadline:
            raise _Stop("time_budget")

    def dist_to(target: str) -> Dict[str, int]:
        """Hop distance from each node to `target` (reverse BFS, bounded by cycle length - 1)."""
        dist = {target: 0}
        q = deque([target])
        while q:
            v = q.popleft()
            dv = dist[v]
            if dv >= cycle_len - 1:
                continue
            for u in rev.get(v, ()):
                tick()
                if u not in dist:
                    dist[u] = dv + 1
                    q.append(u)
        return dist

    def signature(path_nodes: List[str]) -> Tuple[str, ...]:
        k = path_nodes.index(min(path_nodes))
        return tuple(path_nodes[k:] + path_nodes[:k])

    # Start nodes in descending outflow order, so a truncated run reports the highest-value loops first.
    # Every temporal instance is found exactly once: from its earliest event, at its own start node.
    start_nodes = sorted(by_src_i, key=lambda u: (-sum(events[i].amount for i in by_src_i[u]), u))
    try:
        for start in start_nodes:
            if deadline is not None and time.monotonic() > deadline:
                raise _Stop("time_budget")
            d_start = dist_to(start)
            for i0 in by_src_i[start]:
                e0 = events[i0]
                first = e0.dst_entity_id
                if first not in d_start:
                    continue
                t0 = times[i0]
                t_end = t0 + window_s
                path_nodes = [start, first]
                chosen = [i0]
                visited = {start, first}

                def dfs(node: str, t_cur: float, cur_min: float, cur_max: float) -> None:
                    hops = len(chosen)
                    lst_t = by_src_t.get(node)
                    if not lst_t:
                        return
                    lst_i = by_src_i[node]
                    lo = bisect_left(lst_t, t_cur)
                    hi = bisect_right(lst_t, t_end)
                    for k in range(lo, hi):
                        tick()
                        j = lst_i[k]
                        ev = events[j]
                        nmin = cur_min if cur_min <= ev.amount else ev.amount
                        nmax = cur_max if cur_max >= ev.amount else ev.amount
                        if nmin < rho * nmax:
                            continue
                        nxt = ev.dst_entity_id
                        if nxt == start:
                            sig = signature(path_nodes)
                            inst = found.get(sig)
                            if inst is None:
                                if cap is not None and len(found) >= cap:
                                    raise _Stop("max_cycles")
                                inst = found[sig] = []
                            found_count[sig] += 1
                            if len(inst) < inst_cap:
                                inst.append(chosen + [j])
                            else:
                                # keep the earliest instances: replace the latest stored one if this is earlier
                                k_last = max(range(len(inst)), key=lambda q: (times[inst[q][0]], times[inst[q][-1]]))
                                last = inst[k_last]
                                if (t0, times[j]) < (times[last[0]], times[last[-1]]):
                                    inst[k_last] = chosen + [j]
                            continue
                        if nxt in visited or hops + 1 >= cycle_len:
                            continue
                        dn = d_start.get(nxt)
                        if dn is None or hops + 1 + dn > cycle_len:
                            continue
                        visited.add(nxt)
                        path_nodes.append(nxt)
                        chosen.append(j)
                        dfs(nxt, times[j], nmin, nmax)
                        chosen.pop()
                        path_nodes.pop()
                        visited.discard(nxt)

                dfs(first, t0, e0.amount, e0.amount)
    except _Stop as stop:
        truncated_reason = stop.reason

    verified: List[CircularLoop] = []
    for sig, inst_list in found.items():
        inst_list.sort(key=lambda idxs: (times[idxs[0]], times[idxs[-1]], [events[i].tx_id for i in idxs]))
        idxs = inst_list[0]
        chosen_ev: List[DirectedTransferEvent] = [events[i] for i in idxs]
        hop_times = [times[i] for i in idxs]
        cyc = [ev.src_entity_id for ev in chosen_ev]
        amounts = [ev.amount for ev in chosen_ev]
        lo_amt, hi_amt = min(amounts), max(amounts)
        duration_hrs = round((times[idxs[-1]] - times[idxs[0]]) / 3600.0, 2)
        ent_names = [entity_by_id[eid].canonical_name if eid in entity_by_id else eid for eid in cyc]
        hops = _hop_rows(chosen_ev, hop_times)
        intermediary = [
            {
                "entity_id": h["from_entity_id"],
                "entity_name": h["from"],
                "received": chosen_ev[h["hop_index"] - 2].amount,
                "forwarded": h["amount"],
                "leakage": h["leakage"],
                "leakage_pct": h["leakage_pct"],
            }
            for h in hops[1:]
        ]
        total_leak = round(amounts[0] - amounts[-1], 2)
        instances = [_instance_summary([events[i] for i in ix], [times[i] for i in ix]) for ix in inst_list]
        # greedy maximum set of instances sharing no transaction (earliest finish first)
        used: Set[int] = set()
        disjoint = 0
        for ix in sorted(inst_list, key=lambda ix: (times[ix[-1]], times[ix[0]])):
            if used.isdisjoint(ix):
                used.update(ix)
                disjoint += 1
        verified.append(
            DetailedCircularLoop(
                loop_id=str(uuid.uuid5(uuid.NAMESPACE_OID, ",".join(ev.tx_id for ev in chosen_ev))),
                hop_count=len(chosen_ev),
                entities=ent_names + [ent_names[0]],
                entity_ids=cyc + [cyc[0]],
                min_amount=round(lo_amt, 2),
                max_amount=round(hi_amt, 2),
                retention_ratio=round(lo_amt / max(1e-9, hi_amt), 4),
                duration_hours=duration_hrs,
                start_date=chosen_ev[0].txn_date,
                end_date=chosen_ev[-1].txn_date,
                hops=hops,
                total_leakage=total_leak,
                leakage_pct=round(100.0 * total_leak / amounts[0], 2) if amounts[0] > 0 else 0.0,
                intermediary_leakage=intermediary,
                instance_count=found_count[sig],
                disjoint_instance_count=disjoint,
                instances_truncated=found_count[sig] > len(inst_list),
                instances=instances,
                total_instance_volume=round(sum(it["amounts"][0] for it in instances), 2),
            )
        )

    verified.sort(key=lambda l: (l.max_amount, l.hop_count), reverse=True)
    return LoopDetectionResult(
        verified,
        truncated=truncated_reason is not None,
        truncation_reason=truncated_reason,
        elapsed_s=time.monotonic() - started,
        max_cycle_length=cycle_len,
        candidate_events=len(events),
    )
