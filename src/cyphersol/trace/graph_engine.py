"""CypherTrace directed temporal multigraph builder and centrality analytics."""

from __future__ import annotations

import datetime as dt
import math
from bisect import bisect_right
import random
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from cyphersol.models import (
    BankAccountStatement,
    NormalizedTransaction,
    ResolvedEntity,
    TxDirection,
)
from cyphersol.nlp.entity_resolver import is_system_entity, normalize_entity_name
from cyphersol.taxonomy import CATEGORY_BY_ID

# Mirror-leg matching (TG-11 / TF-09): a credit on B's statement from A is the same transfer as a debit on
# A's statement to B when the UTR matches (within UTR_MATCH_DAYS) or, failing that, the amount matches
# within MIRROR_TOLERANCE_DAYS (NEFT/RTGS batches routinely post T+1/T+2). Each debit absorbs one credit.
MIRROR_TOLERANCE_DAYS = 2
UTR_MATCH_DAYS = 7

# Exit classification (TG-10) by channel / category, never by substring of the counterparty name.
EXIT_CHANNELS = frozenset({"ATM", "AEPS", "CASH_WDL"})
EXIT_CATEGORY_IDS = frozenset(cid for cid, spec in CATEGORY_BY_ID.items() if spec.is_cash_crypto_leakage)

# Scalability limits (TG-04)
BETWEENNESS_SAMPLE_K = 256
BETWEENNESS_EXACT_MAX_NODES = 2000
BETWEENNESS_EXACT_MAX_WORK = 6_000_000  # n * (n + m) for exact stdlib Brandes
DEFAULT_MAX_DISPLAY_NODES = 1500
CONDUIT_BETWEENNESS_PERCENTILE = 0.75
_SAMPLE_SEED = 0x5EED

_SYSTEM_SOURCE_PREFIX = "\x00src:"


@dataclass
class DirectedTransferEvent:
    event_id: str
    tx_id: str
    src_entity_id: str
    src_name: str
    dst_entity_id: str
    dst_name: str
    amount: float
    txn_date: str
    txn_timestamp: str
    channel: str
    utr_number: Optional[str]
    narration: str
    category_id: int
    src_is_system: bool = False
    dst_is_system: bool = False


def _parse_dt(ts: Optional[str], d: str) -> dt.datetime:
    if ts:
        try:
            return dt.datetime.fromisoformat(ts.replace("Z", ""))
        except ValueError:
            pass
    try:
        return dt.datetime.fromisoformat(f"{d}T12:00:00")
    except ValueError:
        return dt.datetime(2026, 1, 1, 12, 0, 0)


def event_datetime(ev: DirectedTransferEvent) -> dt.datetime:
    """Parsed timestamp of a transfer event (falls back to the value date at noon)."""
    return _parse_dt(ev.txn_timestamp, ev.txn_date)


def _date_ordinal(d: str) -> Optional[int]:
    try:
        return dt.date.fromisoformat(d[:10]).toordinal()
    except (TypeError, ValueError):
        return None


def _norm_utr(utr: Optional[str]) -> str:
    return "".join(ch for ch in (utr or "").upper() if ch.isalnum())


def extract_directed_transfer_events(
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    min_amount: float = 0.0,
    channel_filter: Optional[str] = None,
    keyword: Optional[str] = None,
    ifsc_filter: Optional[str] = None,
    max_amount: Optional[float] = None,
) -> Tuple[List[DirectedTransferEvent], Dict[str, ResolvedEntity]]:
    """
    Convert statement transactions into unified directed edges (src -> dst), deduplicating mirrored
    inter-statement transfers where both A's debit and B's credit exist. Mirror legs are paired by UTR first,
    then by exact amount within +/-MIRROR_TOLERANCE_DAYS, consuming each debit at most once (multiset match).
    Events touching system parties (cash, ATM, charges, UNRESOLVED_*) carry src/dst_is_system flags.
    Filters (applied per transaction, before mirror matching): date range on txn_date, min/max amount, channel,
    narration/counterparty keyword, and `ifsc_filter` (TG-12): comma-separated IFSC codes or prefixes (e.g.
    "HDFC" or "HDFC0001234,SBIN"), matched case-insensitively against the counterparty IFSC or the IFSC of the
    statement the row came from.
    """
    entity_by_id: Dict[str, ResolvedEntity] = {e.entity_id: e for e in entities}
    system_ids: Set[str] = {e.entity_id for e in entities if is_system_entity(e)}
    stmt_to_entity = resolve_statement_owners(statements, entities)
    ifsc_prefixes = parse_ifsc_filter(ifsc_filter)
    stmt_ifsc: Dict[str, str] = {st.statement_id: (st.ifsc_code or "").strip().upper() for st in statements}
    max_amt = float(max_amount) if max_amount not in (None, "") and float(max_amount) > 0 else None

    events: List[DirectedTransferEvent] = []
    # Emitted debit legs available to absorb a mirrored credit: key (src, dst, paise) -> [ordinal, utr, used]
    debit_legs: Dict[Tuple[str, str, int], List[List[Any]]] = defaultdict(list)
    debit_legs_by_utr: Dict[Tuple[str, str, int, str], List[List[Any]]] = defaultdict(list)
    kw_low = keyword.strip().lower() if keyword else None
    ch_up = channel_filter.strip().upper() if channel_filter else None

    # Debits first so outgoing statements anchor mirrored credits
    ordered = sorted(
        (t for t in transactions if not t.is_duplicate_ignored),
        key=lambda t: (0 if t.direction == TxDirection.DEBIT else 1, t.txn_date, t.row_index),
    )

    for tx in ordered:
        if date_from and tx.txn_date < date_from:
            continue
        if date_to and tx.txn_date > date_to:
            continue
        amt = float(tx.credit if tx.direction == TxDirection.CREDIT else tx.debit)
        if amt <= 0 or amt < min_amount:
            continue
        if ch_up and ch_up != "ALL" and tx.payment_channel.upper() != ch_up:
            continue
        if max_amt is not None and amt > max_amt:
            continue
        if kw_low and kw_low not in tx.raw_narration.lower() and kw_low not in (tx.extracted_counterparty or "").lower():
            continue
        if ifsc_prefixes and not _ifsc_matches(
            ifsc_prefixes, (tx.counterparty_ifsc or "").strip().upper(), stmt_ifsc.get(tx.statement_id, "")
        ):
            continue

        stmt_ent = stmt_to_entity.get(tx.statement_id)
        cp_ent = entity_by_id.get(tx.resolved_entity_id or "")
        if not stmt_ent or not cp_ent:
            continue
        if stmt_ent.entity_id == cp_ent.entity_id and tx.category_id != 49:
            continue

        paise = int(round(amt * 100))
        ordinal = _date_ordinal(tx.txn_date)
        utr = _norm_utr(tx.utr_number)
        if tx.direction == TxDirection.DEBIT:
            src_ent, dst_ent = stmt_ent, cp_ent
            leg = [ordinal, utr, False]
            key = (src_ent.entity_id, dst_ent.entity_id, paise)
            debit_legs[key].append(leg)
            if utr:
                debit_legs_by_utr[key + (utr,)].append(leg)
        else:
            src_ent, dst_ent = cp_ent, stmt_ent
            if src_ent.has_statement and _consume_mirror_debit(
                debit_legs, debit_legs_by_utr, (src_ent.entity_id, dst_ent.entity_id, paise), ordinal, utr
            ):
                continue

        events.append(
            DirectedTransferEvent(
                event_id=tx.tx_id,
                tx_id=tx.tx_id,
                src_entity_id=src_ent.entity_id,
                src_name=src_ent.canonical_name,
                dst_entity_id=dst_ent.entity_id,
                dst_name=dst_ent.canonical_name,
                amount=round(amt, 2),
                txn_date=tx.txn_date,
                txn_timestamp=tx.txn_timestamp or f"{tx.txn_date}T12:00:00",
                channel=tx.payment_channel,
                utr_number=tx.utr_number,
                narration=tx.raw_narration,
                category_id=tx.category_id,
                src_is_system=src_ent.entity_id in system_ids,
                dst_is_system=dst_ent.entity_id in system_ids,
            )
        )

    events.sort(key=lambda e: (_parse_dt(e.txn_timestamp, e.txn_date), e.event_id))
    return events, entity_by_id


def resolve_statement_owners(
    statements: List[BankAccountStatement], entities: List[ResolvedEntity]
) -> Dict[str, ResolvedEntity]:
    """statement_id -> the statement-holding entity that owns it (by linked account number, then holder name)."""
    holder_norm_to_entity: Dict[str, ResolvedEntity] = {}
    acc_to_entity: Dict[str, ResolvedEntity] = {}
    for e in entities:
        if e.has_statement:
            for al in e.aliases:
                holder_norm_to_entity[normalize_entity_name(al)] = e
            holder_norm_to_entity[normalize_entity_name(e.canonical_name)] = e
            for acc in e.linked_account_numbers:
                acc_to_entity[acc] = e
    stmt_to_entity: Dict[str, ResolvedEntity] = {}
    for st in statements:
        ent = acc_to_entity.get(st.account_number) or holder_norm_to_entity.get(
            normalize_entity_name(st.account_holder_name)
        )
        if ent:
            stmt_to_entity[st.statement_id] = ent
    return stmt_to_entity


def parse_ifsc_filter(ifsc_filter: Optional[str]) -> Tuple[str, ...]:
    """Normalise an IFSC filter ("hdfc, SBIN0001234") into upper-case alphanumeric prefixes; empty -> ()."""
    if not ifsc_filter:
        return ()
    out: List[str] = []
    for part in str(ifsc_filter).replace(";", ",").split(","):
        code = "".join(ch for ch in part.upper() if ch.isalnum())
        if code and code != "ALL" and code not in out:
            out.append(code)
    return tuple(out)


def _ifsc_matches(prefixes: Tuple[str, ...], *codes: str) -> bool:
    return any(code and code.startswith(p) for code in codes for p in prefixes)


def _consume_mirror_debit(
    debit_legs: Dict[Tuple[str, str, int], List[List[Any]]],
    debit_legs_by_utr: Dict[Tuple[str, str, int, str], List[List[Any]]],
    key: Tuple[str, str, int],
    ordinal: Optional[int],
    utr: str,
) -> bool:
    """Mark and return True if an unused debit leg mirrors this credit (UTR first, then amount +/- days)."""
    if utr:
        for leg in debit_legs_by_utr.get(key + (utr,), ()):
            if leg[2]:
                continue
            if ordinal is None or leg[0] is None or abs(leg[0] - ordinal) <= UTR_MATCH_DAYS:
                leg[2] = True
                return True
    best: Optional[List[Any]] = None
    best_gap = MIRROR_TOLERANCE_DAYS + 1
    for leg in debit_legs.get(key, ()):
        if leg[2] or (utr and leg[1] and leg[1] != utr):
            continue
        gap = 0 if (ordinal is None or leg[0] is None) else abs(leg[0] - ordinal)
        if gap < best_gap:
            best, best_gap = leg, gap
            if gap == 0:
                break
    if best is not None:
        best[2] = True
        return True
    return False


# --------------------------------------------------------------------------------------------------------
# Centrality
# --------------------------------------------------------------------------------------------------------


def _index_graph(
    nodes: Sequence[str], adj: Dict[str, Dict[str, float]]
) -> Tuple[Dict[str, int], List[List[int]], List[List[float]]]:
    idx = {u: i for i, u in enumerate(nodes)}
    succ: List[List[int]] = [[] for _ in nodes]
    wts: List[List[float]] = [[] for _ in nodes]
    for u, targets in adj.items():
        iu = idx.get(u)
        if iu is None:
            continue
        for v, w in targets.items():
            iv = idx.get(v)
            if iv is None or iv == iu:
                continue
            succ[iu].append(iv)
            wts[iu].append(float(w))
    return idx, succ, wts


def _compute_pagerank(
    nodes: List[str],
    adj: Dict[str, Dict[str, float]],
    d: float = 0.85,
    iters: int = 100,
    tol: float = 1e-10,
) -> Dict[str, float]:
    """Weighted PageRank by power iteration with an L1 convergence check (full precision, TG-08/TG-24)."""
    n = len(nodes)
    if n == 0:
        return {}
    _, succ, wts = _index_graph(nodes, adj)
    norm: List[List[float]] = []
    dangling: List[int] = []
    for i in range(n):
        tot = sum(wts[i])
        if tot > 0:
            norm.append([w / tot for w in wts[i]])
        else:
            norm.append([])
            dangling.append(i)
    pr = [1.0 / n] * n
    base = (1.0 - d) / n
    for _ in range(iters):
        dang = d * sum(pr[i] for i in dangling) / n
        new = [base + dang] * n
        for i in range(n):
            s_i = succ[i]
            if s_i:
                pi = d * pr[i]
                for j, w in zip(s_i, norm[i]):
                    new[j] += pi * w
        err = sum(abs(a - b) for a, b in zip(new, pr))
        pr = new
        if err < n * tol:
            break
    return {u: pr[i] for i, u in enumerate(nodes)}


def _brandes_indexed(n: int, succ: List[List[int]], sources: Sequence[int]) -> List[float]:
    """Brandes' unweighted shortest-path dependency accumulation from the given sources."""
    cb = [0.0] * n
    for s in sources:
        sigma = [0] * n
        dist = [-1] * n
        sigma[s] = 1
        dist[s] = 0
        order = [s]
        pred: Dict[int, List[int]] = {}
        head = 0
        while head < len(order):
            v = order[head]
            head += 1
            dv = dist[v] + 1
            sv = sigma[v]
            for w in succ[v]:
                dw = dist[w]
                if dw < 0:
                    dist[w] = dv
                    sigma[w] = sv
                    pred[w] = [v]
                    order.append(w)
                elif dw == dv:
                    sigma[w] += sv
                    pred[w].append(v)
        delta = [0.0] * n
        for w in reversed(order):
            if w == s:
                continue
            coeff = (1.0 + delta[w]) / sigma[w]
            for v in pred[w]:
                delta[v] += sigma[v] * coeff
            cb[w] += delta[w]
    return cb


def _compute_betweenness(
    nodes: List[str],
    adj: Dict[str, Dict[str, float]],
    sample_k: Optional[int] = None,
    seed: int = _SAMPLE_SEED,
) -> Dict[str, float]:
    """
    Normalised directed betweenness (full precision). Uses networkx when importable; otherwise stdlib Brandes.
    Exact for small graphs; for large graphs (n > BETWEENNESS_EXACT_MAX_NODES or excessive work) sources are
    sampled (k = min(256, n), deterministic seed) and the estimate is rescaled by n / k.
    """
    return _betweenness_with_meta(nodes, adj, sample_k, seed)[0]


def _betweenness_with_meta(
    nodes: List[str],
    adj: Dict[str, Dict[str, float]],
    sample_k: Optional[int] = None,
    seed: int = _SAMPLE_SEED,
) -> Tuple[Dict[str, float], Dict[str, Any]]:
    n = len(nodes)
    meta: Dict[str, Any] = {"backend": "stdlib", "sampled": False, "samples": n}
    if n <= 2:
        return {v: 0.0 for v in nodes}, meta
    m = sum(len(t) for t in adj.values())
    if sample_k is None and (n > BETWEENNESS_EXACT_MAX_NODES or n * (n + m) > BETWEENNESS_EXACT_MAX_WORK):
        sample_k = min(BETWEENNESS_SAMPLE_K, n)
    if sample_k is not None and sample_k >= n:
        sample_k = None

    try:
        import networkx as nx  # type: ignore

        g = nx.DiGraph()
        g.add_nodes_from(nodes)
        g.add_edges_from((u, v) for u, t in adj.items() for v in t if u != v)
        vals = nx.betweenness_centrality(g, k=sample_k, normalized=True, seed=seed if sample_k else None)
        meta.update(backend="networkx", sampled=sample_k is not None, samples=sample_k or n)
        return {v: float(vals.get(v, 0.0)) for v in nodes}, meta
    except ImportError:
        pass

    _, succ, _ = _index_graph(nodes, adj)
    if sample_k is not None:
        sources = sorted(random.Random(seed).sample(range(n), sample_k))
        meta.update(sampled=True, samples=sample_k)
        scale_sample = n / float(sample_k)
    else:
        sources = range(n)
        scale_sample = 1.0
    cb = _brandes_indexed(n, succ, sources)
    scale = scale_sample / ((n - 1) * (n - 2))
    return {u: cb[i] * scale for i, u in enumerate(nodes)}, meta


def _split_system_nodes(
    node_list: List[str], adj: Dict[str, Dict[str, float]], system_ids: Set[str]
) -> Tuple[List[str], Dict[str, Dict[str, float]]]:
    """
    Split each system node into a sink half (keeps its id, incoming edges only) and a source half
    (outgoing edges only) so shortest paths can start or end at cash/charges/UNRESOLVED nodes but never pass
    through them (NLP-10).
    """
    if not system_ids:
        return node_list, adj
    out_adj: Dict[str, Dict[str, float]] = {}
    extra: List[str] = []
    for u, targets in adj.items():
        key = _SYSTEM_SOURCE_PREFIX + u if u in system_ids else u
        if key != u:
            extra.append(key)
        out_adj[key] = dict(targets)
    return node_list + extra, out_adj


def _nearest_rank_percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return math.inf
    vals = sorted(values)
    rank = max(1, math.ceil(q * len(vals)))
    return vals[rank - 1]


# --------------------------------------------------------------------------------------------------------
# Graph payload
# --------------------------------------------------------------------------------------------------------


def build_network_graph(
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    mule_scores: Optional[Dict[str, Dict[str, Any]]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    min_amount: float = 0.0,
    channel_filter: Optional[str] = None,
    keyword: Optional[str] = None,
    max_display_nodes: int = DEFAULT_MAX_DISPLAY_NODES,
    conduit_percentile: float = CONDUIT_BETWEENNESS_PERCENTILE,
    ifsc_filter: Optional[str] = None,
    max_amount: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Construct the forensic Network Graph JSON payload with nodes, weighted edges, and centrality.
    Centrality is computed on the full graph; when it has more than `max_display_nodes` nodes only the
    top-N nodes by volume (and the edges among them) are returned and laid out, with summary.truncated=True.
    """
    events, entity_by_id = extract_directed_transfer_events(
        statements, transactions, entities, date_from, date_to, min_amount, channel_filter, keyword,
        ifsc_filter=ifsc_filter, max_amount=max_amount,
    )

    adj: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    edge_agg: Dict[Tuple[str, str], Dict[str, Any]] = {}
    node_inflow: Dict[str, float] = defaultdict(float)
    node_outflow: Dict[str, float] = defaultdict(float)
    exit_inflow: Dict[str, float] = defaultdict(float)
    active_node_ids: Set[str] = set()
    system_ids: Set[str] = set()

    for ev in events:
        active_node_ids.add(ev.src_entity_id)
        active_node_ids.add(ev.dst_entity_id)
        if ev.src_is_system:
            system_ids.add(ev.src_entity_id)
        if ev.dst_is_system:
            system_ids.add(ev.dst_entity_id)
        adj[ev.src_entity_id][ev.dst_entity_id] += ev.amount
        node_outflow[ev.src_entity_id] += ev.amount
        node_inflow[ev.dst_entity_id] += ev.amount
        if (ev.channel or "").upper() in EXIT_CHANNELS or ev.category_id in EXIT_CATEGORY_IDS:
            exit_inflow[ev.dst_entity_id] += ev.amount

        key = (ev.src_entity_id, ev.dst_entity_id)
        rec = edge_agg.get(key)
        if rec is None:
            rec = edge_agg[key] = {
                "source": ev.src_entity_id,
                "source_name": ev.src_name,
                "target": ev.dst_entity_id,
                "target_name": ev.dst_name,
                "total_amount": 0.0,
                "tx_count": 0,
                "first_date": ev.txn_date,
                "last_date": ev.txn_date,
                "channels": set(),
                "tx_ids": [],
            }
        rec["total_amount"] += ev.amount
        rec["tx_count"] += 1
        if ev.txn_date < rec["first_date"]:
            rec["first_date"] = ev.txn_date
        if ev.txn_date > rec["last_date"]:
            rec["last_date"] = ev.txn_date
        rec["channels"].add(ev.channel)
        rec["tx_ids"].append(ev.tx_id)

    node_list = sorted(active_node_ids)
    cent_nodes, cent_adj = _split_system_nodes(node_list, adj, system_ids)
    pr_raw = _compute_pagerank(cent_nodes, cent_adj)
    pr = {nid: pr_raw.get(nid, 0.0) + pr_raw.get(_SYSTEM_SOURCE_PREFIX + nid, 0.0) for nid in node_list}
    btw_raw, btw_meta = _betweenness_with_meta(cent_nodes, cent_adj)
    btw = {nid: btw_raw.get(nid, 0.0) for nid in node_list}

    in_deg = {nid: 0 for nid in node_list}
    out_deg = {nid: len(adj.get(nid, {})) for nid in node_list}
    neighbors: Dict[str, Set[str]] = defaultdict(set)
    for u, targets in adj.items():
        for v in targets:
            in_deg[v] = in_deg.get(v, 0) + 1
            neighbors[u].add(v)
            neighbors[v].add(u)

    comp_id, n_components = _connected_components(node_list, neighbors, system_ids, node_inflow, node_outflow, adj)

    # Relative conduit threshold (TG-09): a statement-holding account is a conduit candidate when its
    # betweenness is in the top quartile (by default) of all statement-holding accounts in this graph, or within
    # half of the most central account (so a small group of near-equal brokers is treated alike).
    stmt_btw = [
        btw[n] for n in node_list
        if n not in system_ids and entity_by_id.get(n) and entity_by_id[n].has_statement
    ]
    btw_threshold = min(
        _nearest_rank_percentile(stmt_btw, conduit_percentile),
        0.5 * max(stmt_btw) if stmt_btw else math.inf,
    )
    stmt_in_degrees = sorted(in_deg[n] for n in node_list if entity_by_id.get(n) and entity_by_id[n].has_statement)
    hub_in_degree_cap = max(4, stmt_in_degrees[len(stmt_in_degrees) // 2]) if stmt_in_degrees else 4

    btw_sorted = sorted(btw[n] for n in node_list if n not in system_ids)

    nodes_out: List[Dict[str, Any]] = []
    mule_scores = mule_scores or {}
    for nid in node_list:
        ent = entity_by_id.get(nid)
        if not ent:
            continue
        is_sys = nid in system_ids
        m_info = mule_scores.get(nid)
        risk_tier = m_info["risk_tier"] if m_info else ("Details Only" if not ent.has_statement else "Low Risk")
        risk_score = m_info["composite_score"] if m_info else None

        in_d = in_deg.get(nid, 0)
        out_d = out_deg.get(nid, 0)
        btw_val = btw.get(nid, 0.0)
        connected_hubs = sum(
            1 for nb in neighbors.get(nid, ()) if entity_by_id.get(nb) and entity_by_id[nb].has_statement
        )
        inflow = node_inflow[nid]

        if not ent.has_statement and inflow > 0 and exit_inflow[nid] >= 0.5 * inflow:
            node_role, role_label = "EXIT_ENDPOINT", "Cash / VDA Exit"
        elif is_sys:
            node_role, role_label = "PERIPHERAL_LEAF", "System / Generic Party"
        elif (not ent.has_statement and (connected_hubs >= 2 or (in_d >= 1 and out_d >= 1))) or (
            ent.has_statement and btw_val > 0 and btw_val >= btw_threshold and in_d <= hub_in_degree_cap
        ):
            node_role, role_label = "BRIDGE_CONDUIT", "Bridge / Conduit"
        elif ent.has_statement:
            node_role, role_label = "PRIMARY_HUB", "Primary Account Hub"
        else:
            node_role, role_label = "PERIPHERAL_LEAF", "Peripheral Counterparty"

        nodes_out.append({
            "entity_id": nid,
            "label": ent.canonical_name,
            "has_statement": ent.has_statement,
            "is_system": is_sys,
            "aliases": ent.aliases,
            "accounts": ent.linked_account_numbers,
            "total_inflow": round(inflow, 2),
            "total_outflow": round(node_outflow[nid], 2),
            "in_degree": in_d,
            "out_degree": out_d,
            "pagerank": pr.get(nid, 0.0),
            "betweenness": btw_val,
            "component_id": comp_id.get(nid, 1),
            "risk_tier": risk_tier,
            "mule_score": risk_score,
            "node_role": node_role,
            "role_label": role_label,
            "connected_hubs": connected_hubs,
            **_colour_attributes(ent.has_statement, is_sys, risk_tier, btw_val, btw_sorted, node_role),
        })

    nodes_out.sort(key=lambda n: (n["total_inflow"] + n["total_outflow"]), reverse=True)
    total_nodes = len(nodes_out)
    total_edges = len(edge_agg)
    total_volume = round(sum(rec["total_amount"] for rec in edge_agg.values()), 2)
    conduit_total = sum(1 for n in nodes_out if n["node_role"] == "BRIDGE_CONDUIT")
    stmt_total = sum(1 for n in nodes_out if n["has_statement"])
    intermediaries = [
        n for n in sorted(nodes_out, key=lambda x: x["betweenness"], reverse=True) if n["betweenness"] > 0
    ][:10]

    truncated = max_display_nodes > 0 and total_nodes > max_display_nodes
    if truncated:
        nodes_out = nodes_out[:max_display_nodes]
    kept = {n["entity_id"] for n in nodes_out}

    edges_out: List[Dict[str, Any]] = []
    for rec in edge_agg.values():
        if truncated and (rec["source"] not in kept or rec["target"] not in kept):
            continue
        edges_out.append({
            "source": rec["source"],
            "source_name": rec["source_name"],
            "target": rec["target"],
            "target_name": rec["target_name"],
            "total_amount": round(rec["total_amount"], 2),
            "tx_count": rec["tx_count"],
            "first_date": rec["first_date"],
            "last_date": rec["last_date"],
            "channels": sorted(rec["channels"]),
            "tx_ids": rec["tx_ids"],
        })
    edges_out.sort(key=lambda e: e["total_amount"], reverse=True)

    _compute_force_layout(nodes_out, edges_out)

    return {
        "nodes": nodes_out,
        "edges": edges_out,
        "summary": {
            "total_nodes": total_nodes,
            "total_edges": total_edges,
            "displayed_nodes": len(nodes_out),
            "displayed_edges": len(edges_out),
            "truncated": truncated,
            "max_display_nodes": max_display_nodes,
            "total_events": len(events),
            "total_volume": total_volume,
            "connected_components": n_components,
            "statement_nodes": stmt_total,
            "details_only_nodes": total_nodes - stmt_total,
            "system_nodes": len(system_ids),
            "conduit_nodes": conduit_total,
            "conduit_betweenness_threshold": None if math.isinf(btw_threshold) else btw_threshold,
            "centrality_backend": btw_meta["backend"],
            "betweenness_sampled": btw_meta["sampled"],
            "betweenness_samples": btw_meta["samples"],
            "filters": {
                "date_from": date_from,
                "date_to": date_to,
                "min_amount": min_amount,
                "max_amount": max_amount,
                "channel": channel_filter,
                "keyword": keyword,
                "ifsc": list(parse_ifsc_filter(ifsc_filter)),
            },
            "colour_by": COLOUR_BY_LEGEND,
        },
        "key_intermediaries": intermediaries,
    }


# Colour-by attributes (TG-07): every node carries a categorical key per colour scheme so the UI can switch
# between role / statement availability / risk tier / betweenness without conflating role with risk.
BETWEENNESS_BUCKETS = ("none", "low", "medium", "high", "top")
COLOUR_BY_LEGEND: Dict[str, List[str]] = {
    "role": ["PRIMARY_HUB", "BRIDGE_CONDUIT", "PERIPHERAL_LEAF", "EXIT_ENDPOINT"],
    "statement": ["HAS_STATEMENT", "DETAILS_ONLY", "SYSTEM"],
    "risk_tier": ["CRITICAL", "SUSPICIOUS", "LOW_RISK", "DETAILS_ONLY", "UNSCORED"],
    "betweenness": list(BETWEENNESS_BUCKETS),
}


def _risk_tier_key(risk_tier: Optional[str]) -> str:
    t = str(getattr(risk_tier, "value", risk_tier) or "").strip().lower()
    if t.startswith("details"):
        return "DETAILS_ONLY"
    if t.startswith("critical"):
        return "CRITICAL"
    if t.startswith("suspicious") or t.startswith("high") or t.startswith("medium"):
        return "SUSPICIOUS"
    if t.startswith("low"):
        return "LOW_RISK"
    return "UNSCORED"


def _colour_attributes(
    has_statement: bool,
    is_system: bool,
    risk_tier: Optional[str],
    betweenness: float,
    btw_sorted: Sequence[float],
    node_role: str,
) -> Dict[str, Any]:
    """Per-node colour keys (TG-07). betweenness_percentile is the share of non-system nodes with a lower
    or equal betweenness (0..1, the most central node is 1.0); the bucket is "none" for zero betweenness, else
    percentile-based low (<0.5) / medium (<0.75) / high (<0.9) / top."""
    if btw_sorted:
        pct = bisect_right(btw_sorted, betweenness) / float(len(btw_sorted))
    else:
        pct = 0.0
    if betweenness <= 0:
        bucket = "none"
    elif pct >= 0.9:
        bucket = "top"
    elif pct >= 0.75:
        bucket = "high"
    elif pct >= 0.5:
        bucket = "medium"
    else:
        bucket = "low"
    statement_key = "SYSTEM" if is_system else ("HAS_STATEMENT" if has_statement else "DETAILS_ONLY")
    return {
        "betweenness_percentile": round(pct, 4),
        "colour_keys": {
            "role": node_role,
            "statement": statement_key,
            "risk_tier": _risk_tier_key(risk_tier),
            "betweenness": bucket,
        },
    }


def _connected_components(
    node_list: List[str],
    neighbors: Dict[str, Set[str]],
    system_ids: Set[str],
    node_inflow: Dict[str, float],
    node_outflow: Dict[str, float],
    adj: Dict[str, Dict[str, float]],
) -> Tuple[Dict[str, int], int]:
    """
    Undirected components that never connect *through* a system node. Each system node joins the component
    of the neighbour it exchanges the most volume with (or forms its own if it only touches system nodes).
    """
    comp_id: Dict[str, int] = {}
    cid = 0
    for nid in node_list:
        if nid in comp_id or nid in system_ids:
            continue
        cid += 1
        comp_id[nid] = cid
        stack = [nid]
        while stack:
            cur = stack.pop()
            for nb in neighbors.get(cur, ()):
                if nb not in comp_id and nb not in system_ids:
                    comp_id[nb] = cid
                    stack.append(nb)
    for nid in node_list:
        if nid not in system_ids:
            continue
        best, best_vol = None, -1.0
        for nb in neighbors.get(nid, ()):
            if nb in system_ids:
                continue
            vol = adj.get(nid, {}).get(nb, 0.0) + adj.get(nb, {}).get(nid, 0.0)
            if vol > best_vol:
                best, best_vol = nb, vol
        if best is not None:
            comp_id[nid] = comp_id[best]
        else:
            cid += 1
            comp_id[nid] = cid
    return comp_id, cid


# --------------------------------------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------------------------------------

_EXACT_REPULSION_MAX_NODES = 300
_GOLDEN_ANGLE = math.pi * (3.0 - math.sqrt(5.0))


def _compute_force_layout(
    nodes_out: List[Dict[str, Any]],
    edges_out: List[Dict[str, Any]],
    width: float = 920.0,
    height: float = 520.0,
) -> None:
    """
    Compute deterministic hub-and-spoke Fruchterman-Reingold coordinates on nodes_out.
    Exact all-pairs repulsion for small graphs; for larger graphs a uniform-grid neighbourhood approximation
    (repulsion only within 2k) keeps each iteration near-linear.
    """
    n = len(nodes_out)
    if n == 0:
        return
    cx, cy = width / 2.0, height / 2.0
    if n == 1:
        nodes_out[0]["x"] = round(cx, 1)
        nodes_out[0]["y"] = round(cy, 1)
        return

    node_by_id = {nd["entity_id"]: nd for nd in nodes_out}
    conduits = [nd for nd in nodes_out if nd["node_role"] == "BRIDGE_CONDUIT"]
    hubs = [nd for nd in nodes_out if nd["node_role"] == "PRIMARY_HUB"]
    leaves = [nd for nd in nodes_out if nd["node_role"] in ("PERIPHERAL_LEAF", "EXIT_ENDPOINT")]

    pos: Dict[str, List[float]] = {}
    for idx, nd in enumerate(conduits):
        ang = (2.0 * math.pi * idx) / max(1, len(conduits))
        r = 85.0 if len(conduits) > 1 else 0.0
        pos[nd["entity_id"]] = [cx + r * math.cos(ang), cy + r * math.sin(ang)]

    for idx, nd in enumerate(hubs):
        ang = (2.0 * math.pi * idx) / max(1, len(hubs)) + 0.35
        r = 195.0 if (conduits or len(hubs) > 1) else 0.0
        pos[nd["entity_id"]] = [cx + r * math.cos(ang), cy + r * math.sin(ang)]

    # Build neighbor volume map so peripheral leaves seed radially around their parent hub
    best_parent: Dict[str, Tuple[float, str]] = {}
    for e in edges_out:
        s_id, t_id, amt = e["source"], e["target"], float(e["total_amount"])
        if s_id not in node_by_id or t_id not in node_by_id:
            continue
        for leaf_id, other_id in ((s_id, t_id), (t_id, s_id)):
            if node_by_id[leaf_id]["node_role"] in ("PERIPHERAL_LEAF", "EXIT_ENDPOINT"):
                score = amt + (1e9 if node_by_id[other_id]["node_role"] in ("PRIMARY_HUB", "BRIDGE_CONDUIT") else 0.0)
                if leaf_id not in best_parent or score > best_parent[leaf_id][0]:
                    best_parent[leaf_id] = (score, other_id)

    siblings_by_parent: Dict[str, List[str]] = defaultdict(list)
    for nd in leaves:
        nid = nd["entity_id"]
        pid = best_parent[nid][1] if nid in best_parent else ""
        siblings_by_parent[pid].append(nid)

    for pid, sibs in siblings_by_parent.items():
        if pid in pos:
            px, py = pos[pid]
            base_ang = math.atan2(py - cy, px - cx)
            if math.hypot(px - cx, py - cy) < 5.0:
                base_ang = 0.0
            if len(sibs) > 16:
                # Phyllotaxis spiral so large sibling groups do not start stacked on one arc
                for i, nid in enumerate(sibs):
                    r = 30.0 + 9.0 * math.sqrt(i + 1)
                    ang = base_ang + i * _GOLDEN_ANGLE
                    pos[nid] = [px + r * math.cos(ang), py + r * math.sin(ang)]
                continue
            span = min(math.pi * 1.4, max(0.7, len(sibs) * 0.42))
            for i, nid in enumerate(sibs):
                offset = 0.0 if len(sibs) == 1 else ((i / (len(sibs) - 1)) - 0.5) * span
                ang = base_ang + offset
                pos[nid] = [px + 78.0 * math.cos(ang), py + 78.0 * math.sin(ang)]
        else:
            for i, nid in enumerate(sibs):
                ang = (2.0 * math.pi * i) / max(1, len(sibs))
                rad = 1.0 if len(sibs) <= 16 else (0.35 + 0.65 * math.sqrt((i + 1) / len(sibs)))
                pos[nid] = [cx + 210.0 * rad * math.cos(ang), cy + 165.0 * rad * math.sin(ang)]

    area = width * height
    k = math.sqrt(area / max(4, n)) * 0.48
    node_ids = [nd["entity_id"] for nd in nodes_out]
    roles = [node_by_id[nid]["node_role"] for nid in node_ids]
    is_core = [r in ("PRIMARY_HUB", "BRIDGE_CONDUIT") for r in roles]
    is_leaf = [r in ("PERIPHERAL_LEAF", "EXIT_ENDPOINT") for r in roles]
    index = {nid: i for i, nid in enumerate(node_ids)}
    xs = [pos[nid][0] for nid in node_ids]
    ys = [pos[nid][1] for nid in node_ids]
    valid_edges = [
        (index[e["source"]], index[e["target"]])
        for e in edges_out
        if e["source"] in index and e["target"] in index and e["source"] != e["target"]
    ]
    exact = n <= _EXACT_REPULSION_MAX_NODES
    iterations = 70 if exact else 40
    kk = k * k

    for it in range(iterations):
        temp = 18.0 * (1.0 - (it / (iterations + 5.0)))
        dxs = [0.0] * n
        dys = [0.0] * n

        if exact:
            pairs = ((i, j) for i in range(n) for j in range(i + 1, n))
        else:
            pairs = _grid_pairs(xs, ys, 2.0 * k)
        for i, j in pairs:
            dx = xs[i] - xs[j]
            dy = ys[i] - ys[j]
            dist = math.hypot(dx, dy)
            if dist < 1.0:
                dx = 1.0 + ((i + j) % 3) * 0.1
                dy = 1.0 - ((i * 2 + j) % 3) * 0.1
                dist = math.hypot(dx, dy)
            rep_mult = 1.25 if (is_core[i] and is_core[j]) else 0.72
            f_rep = (kk / dist) * rep_mult / dist
            fx = dx * f_rep
            fy = dy * f_rep
            dxs[i] += fx
            dys[i] += fy
            dxs[j] -= fx
            dys[j] -= fy

        # Spring attraction along directed edges
        for u, v in valid_edges:
            dx = xs[u] - xs[v]
            dy = ys[u] - ys[v]
            dist = max(1.0, math.hypot(dx, dy))
            edge_weight = 1.55 if (is_leaf[u] or is_leaf[v]) else 1.05
            f_att = (dist / k) * edge_weight
            fx = dx * f_att
            fy = dy * f_att
            dxs[u] -= fx
            dys[u] -= fy
            dxs[v] += fx
            dys[v] += fy

        # Role-weighted central & radial gravity (conduits stay central, leaves fan outward)
        for i in range(n):
            role = roles[i]
            dx = xs[i] - cx
            dy = ys[i] - cy
            r_cur = max(1.0, math.hypot(dx, dy))
            if role == "BRIDGE_CONDUIT":
                target_r, strength = 65.0, 0.35
            elif role == "PRIMARY_HUB":
                target_r, strength = 165.0, 0.22
            else:
                target_r = 220.0 if r_cur < 120.0 else r_cur
                strength = 0.12
            r_delta = (r_cur - target_r) * strength
            dxs[i] -= (dx / r_cur) * r_delta + 0.04 * dx
            dys[i] -= (dy / r_cur) * r_delta + 0.04 * dy

        for i in range(n):
            dx, dy = dxs[i], dys[i]
            d_len = math.hypot(dx, dy)
            if d_len > 0:
                step = min(d_len, temp) / d_len
                xs[i] += dx * step
                ys[i] += dy * step

    # Proportionally center and fit the relaxed graph cleanly inside the viewport margin
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    span_x = max(1.0, max_x - min_x)
    span_y = max(1.0, max_y - min_y)
    pad_x, pad_y_top, pad_y_bot = 76.0, 52.0, 58.0
    usable_w = width - 2.0 * pad_x
    usable_h = height - pad_y_top - pad_y_bot
    scale = min(1.6, usable_w / span_x, usable_h / span_y)
    mid_x = (min_x + max_x) / 2.0
    mid_y = (min_y + max_y) / 2.0
    target_cy = pad_y_top + usable_h / 2.0

    for i, nd in enumerate(nodes_out):
        px = cx + (xs[i] - mid_x) * scale
        py = target_cy + (ys[i] - mid_y) * scale
        nd["x"] = round(max(55.0, min(width - 55.0, px)), 1)
        nd["y"] = round(max(45.0, min(height - 45.0, py)), 1)


def _grid_pairs(xs: List[float], ys: List[float], cell: float, max_per_cell: int = 48):
    """Yield index pairs within neighbouring grid cells (each unordered pair once). Over-full cells are
    subsampled deterministically so a dense seed cluster cannot make an iteration quadratic."""
    cell = max(cell, 1e-6)
    grid: Dict[Tuple[int, int], List[int]] = defaultdict(list)
    for i in range(len(xs)):
        grid[(int(math.floor(xs[i] / cell)), int(math.floor(ys[i] / cell)))].append(i)
    for (gx, gy), members in grid.items():
        own = members if len(members) <= max_per_cell else members[:: max(1, len(members) // max_per_cell)]
        for a_pos, i in enumerate(own):
            for j in own[a_pos + 1:]:
                yield i, j
        for ox, oy in ((1, -1), (1, 0), (1, 1), (0, 1)):
            other = grid.get((gx + ox, gy + oy))
            if not other:
                continue
            if len(other) > max_per_cell:
                other = other[:: max(1, len(other) // max_per_cell)]
            for i in own:
                for j in other:
                    yield i, j
