"""CypherTrace Bidirectional Flow Analyzer (pairwise A <-> B flow & turnaround analysis)."""

from __future__ import annotations

import datetime as dt
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from cyphersol.models import (
    BankAccountStatement,
    BidirectionalFlowResult,
    NormalizedTransaction,
    ResolvedEntity,
)
from cyphersol.trace.graph_engine import DirectedTransferEvent, extract_directed_transfer_events


DEFAULT_RAPID_WINDOW_HOURS = 48.0
DEFAULT_ROUND_TRIP_WINDOW_HOURS = 30 * 24.0
DEFAULT_AMOUNT_TOLERANCE = 0.10


@dataclass
class BidirectionalFlowAnalysis(BidirectionalFlowResult):
    """
    BidirectionalFlowResult plus the FR-TRACE-02 detail (TG-05/06/18). The inherited turnaround fields keep
    their direction-flip meaning; `round_trips` are amount-matched A->B->A (anchor "A") and B->A->B (anchor "B")
    pairs, and `rapid_round_trip_count` counts anchor-A round trips completed within `rapid_window_hours`.
    """

    max_turnaround_hours: Optional[float] = None
    direction_reversal_count: int = 0
    rapid_reversal_count: int = 0
    rapid_round_trip_count_b: int = 0
    round_trips: List[Dict[str, Any]] = field(default_factory=list)
    round_trip_stats: Dict[str, Any] = field(default_factory=dict)
    ledger_a_to_b: List[Dict[str, Any]] = field(default_factory=list)
    ledger_b_to_a: List[Dict[str, Any]] = field(default_factory=list)
    cumulative_series: List[Dict[str, Any]] = field(default_factory=list)
    daily_series: List[Dict[str, Any]] = field(default_factory=list)
    parameters: Dict[str, Any] = field(default_factory=dict)


def _parse_dt(ts: str, d: str) -> dt.datetime:
    try:
        return dt.datetime.fromisoformat(ts.replace("Z", ""))
    except ValueError:
        return dt.datetime.fromisoformat(f"{d}T12:00:00")


def _ev_dt(ev: DirectedTransferEvent) -> dt.datetime:
    return _parse_dt(ev.txn_timestamp, ev.txn_date)


def _stats(values: List[float]) -> Dict[str, Optional[float]]:
    if not values:
        return {"count": 0, "min_hours": None, "median_hours": None, "max_hours": None}
    return {
        "count": len(values),
        "min_hours": round(min(values), 2),
        "median_hours": round(statistics.median(values), 2),
        "max_hours": round(max(values), 2),
    }


def match_round_trips(
    pair_events: List[DirectedTransferEvent],
    anchor_src: str,
    window_hours: float,
    amount_tolerance: float,
) -> List[Dict[str, Any]]:
    """
    FIFO one-to-one matching of each outflow from `anchor_src` with the earliest later return from the other
    party whose amount is within `amount_tolerance` (relative to the outflow) and arrives within
    `window_hours`. Each event is used at most once. Events must be chronological.
    """
    open_out: List[DirectedTransferEvent] = []
    trips: List[Dict[str, Any]] = []
    for ev in pair_events:
        t = _ev_dt(ev)
        if ev.src_entity_id == anchor_src:
            open_out.append(ev)
            continue
        # drop expired outflows, then match the oldest amount-compatible one
        open_out = [o for o in open_out if (t - _ev_dt(o)).total_seconds() / 3600.0 <= window_hours]
        for k, o in enumerate(open_out):
            if abs(ev.amount - o.amount) <= amount_tolerance * o.amount + 0.005:
                hrs = (t - _ev_dt(o)).total_seconds() / 3600.0
                if hrs < 0:
                    continue
                trips.append({
                    "anchor_entity_id": anchor_src,
                    "out_tx_id": o.tx_id,
                    "back_tx_id": ev.tx_id,
                    "out_amount": o.amount,
                    "back_amount": ev.amount,
                    "amount_diff": round(ev.amount - o.amount, 2),
                    "out_date": o.txn_date,
                    "back_date": ev.txn_date,
                    "out_timestamp": o.txn_timestamp,
                    "back_timestamp": ev.txn_timestamp,
                    "out_utr": o.utr_number,
                    "back_utr": ev.utr_number,
                    "turnaround_hours": round(hrs, 2),
                })
                del open_out[k]
                break
    return trips


def analyze_bidirectional_flow(
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    entity_a_id: str,
    entity_b_id: str,
    rapid_window_hours: float = DEFAULT_RAPID_WINDOW_HOURS,
    round_trip_window_hours: float = DEFAULT_ROUND_TRIP_WINDOW_HOURS,
    amount_tolerance: float = DEFAULT_AMOUNT_TOLERANCE,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> BidirectionalFlowAnalysis:
    """
    Compute mutual fund exchanges, net flow, turnaround timing, amount-matched round trips and the dual ledger
    between A and B. Mirrored legs (A's debit + B's credit) are counted once: the event extractor pairs them by
    UTR first, then by amount within +/-2 days (TF-09/TG-11).
    """
    if rapid_window_hours is None or not math.isfinite(float(rapid_window_hours)) or float(rapid_window_hours) < 0:
        raise ValueError("rapid_window_hours must be a finite, non-negative number")
    if (round_trip_window_hours is None or not math.isfinite(float(round_trip_window_hours))
            or float(round_trip_window_hours) <= 0):
        raise ValueError("round_trip_window_hours must be a finite, positive number")
    if amount_tolerance is None or not (0.0 <= float(amount_tolerance) <= 1.0):
        raise ValueError("amount_tolerance must be between 0 and 1")
    rapid_window_hours = float(rapid_window_hours)
    events, entity_by_id = extract_directed_transfer_events(
        statements, transactions, entities, date_from=date_from, date_to=date_to
    )
    ent_a = entity_by_id.get(entity_a_id)
    ent_b = entity_by_id.get(entity_b_id)
    name_a = ent_a.canonical_name if ent_a else entity_a_id
    name_b = ent_b.canonical_name if ent_b else entity_b_id

    ab_events: List[DirectedTransferEvent] = []
    ba_events: List[DirectedTransferEvent] = []
    pair_events: List[DirectedTransferEvent] = []

    for ev in events:
        if ev.src_entity_id == entity_a_id and ev.dst_entity_id == entity_b_id:
            ab_events.append(ev)
            pair_events.append(ev)
        elif ev.src_entity_id == entity_b_id and ev.dst_entity_id == entity_a_id:
            ba_events.append(ev)
            pair_events.append(ev)

    total_ab = round(sum(e.amount for e in ab_events), 2)
    total_ba = round(sum(e.amount for e in ba_events), 2)
    net_flow = round(total_ab - total_ba, 2)

    # Turnaround times between direction flips (any amount)
    turnarounds: List[float] = []
    rapid_reversals = 0
    for i in range(1, len(pair_events)):
        prev_ev = pair_events[i - 1]
        cur_ev = pair_events[i]
        if prev_ev.src_entity_id != cur_ev.src_entity_id:
            delta_hrs = (_ev_dt(cur_ev) - _ev_dt(prev_ev)).total_seconds() / 3600.0
            if delta_hrs >= 0:
                turnarounds.append(round(delta_hrs, 2))
                if delta_hrs <= rapid_window_hours:
                    rapid_reversals += 1
    flip_stats = _stats(turnarounds)

    # Amount-matched round trips anchored on each side (TG-05/06)
    trips_a = match_round_trips(pair_events, entity_a_id, float(round_trip_window_hours), float(amount_tolerance))
    trips_b = match_round_trips(pair_events, entity_b_id, float(round_trip_window_hours), float(amount_tolerance))
    for tr in trips_a:
        tr["anchor"] = "A"
        tr["rapid"] = tr["turnaround_hours"] <= rapid_window_hours
    for tr in trips_b:
        tr["anchor"] = "B"
        tr["rapid"] = tr["turnaround_hours"] <= rapid_window_hours
    rapid_a = sum(1 for tr in trips_a if tr["rapid"])
    rapid_b = sum(1 for tr in trips_b if tr["rapid"])
    round_trips = sorted(trips_a + trips_b, key=lambda tr: (tr["out_timestamp"], tr["anchor"]))
    round_trip_stats = {
        "anchor_a": {**_stats([tr["turnaround_hours"] for tr in trips_a]), "rapid_count": rapid_a,
                     "matched_volume": round(sum(tr["out_amount"] for tr in trips_a), 2)},
        "anchor_b": {**_stats([tr["turnaround_hours"] for tr in trips_b]), "rapid_count": rapid_b,
                     "matched_volume": round(sum(tr["out_amount"] for tr in trips_b), 2)},
        "direction_flips": flip_stats,
    }

    running_net = 0.0
    cum_ab = 0.0
    cum_ba = 0.0
    ledger_rows: List[Dict[str, Any]] = []
    ledger_ab: List[Dict[str, Any]] = []
    ledger_ba: List[Dict[str, Any]] = []
    series: List[Dict[str, Any]] = []
    daily: Dict[str, Dict[str, float]] = {}
    for ev in pair_events:
        is_ab = ev.src_entity_id == entity_a_id
        if is_ab:
            running_net += ev.amount
            cum_ab += ev.amount
            flow_dir = f"{name_a} → {name_b}"
        else:
            running_net -= ev.amount
            cum_ba += ev.amount
            flow_dir = f"{name_b} → {name_a}"
        row = {
            "tx_id": ev.tx_id,
            "date": ev.txn_date,
            "timestamp": ev.txn_timestamp,
            "direction": flow_dir,
            "direction_code": "A_TO_B" if is_ab else "B_TO_A",
            "from_entity": ev.src_name,
            "to_entity": ev.dst_name,
            "amount": ev.amount,
            "channel": ev.channel,
            "utr": ev.utr_number,
            "narration": ev.narration,
            "cumulative_net_a_to_b": round(running_net, 2),
            "cumulative_a_to_b": round(cum_ab, 2),
            "cumulative_b_to_a": round(cum_ba, 2),
        }
        ledger_rows.append(row)
        side = {k: row[k] for k in ("tx_id", "date", "timestamp", "amount", "channel", "utr", "narration")}
        if is_ab:
            side["cumulative"] = round(cum_ab, 2)
            ledger_ab.append(side)
        else:
            side["cumulative"] = round(cum_ba, 2)
            ledger_ba.append(side)
        series.append({
            "timestamp": ev.txn_timestamp,
            "date": ev.txn_date,
            "cumulative_a_to_b": round(cum_ab, 2),
            "cumulative_b_to_a": round(cum_ba, 2),
            "net_a_to_b": round(running_net, 2),
        })
        day = daily.setdefault(ev.txn_date, {"a_to_b": 0.0, "b_to_a": 0.0})
        day["a_to_b" if is_ab else "b_to_a"] += ev.amount

    daily_rows: List[Dict[str, Any]] = []
    d_ab = d_ba = 0.0
    for d in sorted(daily):
        d_ab += daily[d]["a_to_b"]
        d_ba += daily[d]["b_to_a"]
        daily_rows.append({
            "date": d,
            "a_to_b": round(daily[d]["a_to_b"], 2),
            "b_to_a": round(daily[d]["b_to_a"], 2),
            "cumulative_a_to_b": round(d_ab, 2),
            "cumulative_b_to_a": round(d_ba, 2),
            "net_a_to_b": round(d_ab - d_ba, 2),
        })

    return BidirectionalFlowAnalysis(
        entity_a_id=entity_a_id,
        entity_a_name=name_a,
        entity_b_id=entity_b_id,
        entity_b_name=name_b,
        total_a_to_b=total_ab,
        total_b_to_a=total_ba,
        net_flow=net_flow,
        tx_count_a_to_b=len(ab_events),
        tx_count_b_to_a=len(ba_events),
        min_turnaround_hours=flip_stats["min_hours"],
        median_turnaround_hours=flip_stats["median_hours"],
        rapid_round_trip_count=rapid_a,
        transactions=ledger_rows,
        max_turnaround_hours=flip_stats["max_hours"],
        direction_reversal_count=len(turnarounds),
        rapid_reversal_count=rapid_reversals,
        rapid_round_trip_count_b=rapid_b,
        round_trips=round_trips,
        round_trip_stats=round_trip_stats,
        ledger_a_to_b=ledger_ab,
        ledger_b_to_a=ledger_ba,
        cumulative_series=series,
        daily_series=daily_rows,
        parameters={
            "rapid_window_hours": rapid_window_hours,
            "round_trip_window_hours": float(round_trip_window_hours),
            "amount_tolerance": float(amount_tolerance),
            "date_from": date_from,
            "date_to": date_to,
        },
    )


def list_all_bidirectional_pairs(
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    include_system: bool = False,
) -> List[Dict[str, Any]]:
    """
    Discover all pairs with mutual two-way fund exchanges, sorted by total bidirectional volume.
    Pairs involving system parties (cash, ATM, charges, UNRESOLVED_*) are excluded unless include_system=True:
    a cash deposit and a cash withdrawal are not a mutual exchange with a counterparty.
    """
    events, entity_by_id = extract_directed_transfer_events(statements, transactions, entities)
    pair_dirs: Dict[tuple[str, str], Dict[str, float]] = {}
    pair_counts: Dict[tuple[str, str], Dict[str, int]] = {}

    for ev in events:
        if ev.src_entity_id == ev.dst_entity_id:
            continue
        if not include_system and (ev.src_is_system or ev.dst_is_system):
            continue
        a, b = sorted([ev.src_entity_id, ev.dst_entity_id])
        key = (a, b)
        if key not in pair_dirs:
            pair_dirs[key] = {"ab": 0.0, "ba": 0.0}
            pair_counts[key] = {"ab": 0, "ba": 0}
        if ev.src_entity_id == a:
            pair_dirs[key]["ab"] += ev.amount
            pair_counts[key]["ab"] += 1
        else:
            pair_dirs[key]["ba"] += ev.amount
            pair_counts[key]["ba"] += 1

    results: List[Dict[str, Any]] = []
    for (a, b), vols in pair_dirs.items():
        if vols["ab"] > 0 and vols["ba"] > 0:
            ent_a = entity_by_id.get(a)
            ent_b = entity_by_id.get(b)
            results.append({
                "entity_a_id": a,
                "entity_a_name": ent_a.canonical_name if ent_a else a,
                "entity_b_id": b,
                "entity_b_name": ent_b.canonical_name if ent_b else b,
                "total_a_to_b": round(vols["ab"], 2),
                "total_b_to_a": round(vols["ba"], 2),
                "net_flow": round(vols["ab"] - vols["ba"], 2),
                "tx_count_a_to_b": pair_counts[(a, b)]["ab"],
                "tx_count_b_to_a": pair_counts[(a, b)]["ba"],
                "total_mutual_volume": round(vols["ab"] + vols["ba"], 2),
            })

    results.sort(key=lambda r: r["total_mutual_volume"], reverse=True)
    return results
