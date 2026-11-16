"""CypherTrace Progressive Layering Detection & Interactive Drill-Down Tree (with Move to Misc)."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from cyphersol.models import (
    BankAccountStatement,
    LayeringNode,
    NormalizedTransaction,
    ResolvedEntity,
    TxDirection,
)
from cyphersol.nlp.entity_resolver import is_system_entity
from cyphersol.trace.graph_engine import (
    DirectedTransferEvent,
    event_datetime,
    extract_directed_transfer_events,
    resolve_statement_owners,
)

LAYERING_MODES = ("fifo", "all")
DEFAULT_MAX_TREE_NODES = 5000
_EPS = 0.005  # half a paisa
_EPOCH = dt.datetime(1970, 1, 1)


@dataclass
class AttributedLayeringNode(LayeringNode):
    """
    LayeringNode that keeps the traced (attributed) value separate from the full debit value of the
    underlying transactions. `amount` equals `attributed_amount` so diagrams never show more than was traced.
    """

    attributed_amount: float = 0.0
    debit_amount: float = 0.0
    is_system: bool = False
    path: List[str] = field(default_factory=list)
    path_key: str = ""
    expandable: bool = False
    has_more_children: bool = False
    misc_members: List[Dict[str, Any]] = field(default_factory=list)


def _secs(ev: DirectedTransferEvent) -> float:
    return (event_datetime(ev) - _EPOCH).total_seconds()


DEFAULT_TRACE_DEPTH = 8
MAX_TRACE_DEPTH = 32
# mode='all' follows every simple path, which grows exponentially with depth on dense graphs. Every recorded
# emit and opened window counts against this work budget; once it is spent the pass stops and reports truncated.
ALL_MODE_WORK_BUDGET = 200_000
PATH_SEPARATOR = ">"


def misc_path_key(path: Sequence[str]) -> str:
    """Canonical string for a tree path (root entity id first), as used in `misc_paths` and node `path_key`."""
    return PATH_SEPARATOR.join(path)


def _norm_misc_paths(misc_paths: Optional[Iterable[Any]]) -> Set[Tuple[str, ...]]:
    out: Set[Tuple[str, ...]] = set()
    for p in misc_paths or ():
        if isinstance(p, str):
            parts = tuple(x for x in p.split(PATH_SEPARATOR) if x)
        else:
            parts = tuple(str(x) for x in p if x)
        if len(parts) >= 2:
            out.add(parts)
    return out


def _tx_secs(tx: NormalizedTransaction) -> float:
    ts = tx.txn_timestamp or ""
    try:
        d = dt.datetime.fromisoformat(ts.replace("Z", "")) if ts else None
    except ValueError:
        d = None
    if d is None:
        d = dt.datetime.fromisoformat(f"{tx.txn_date[:10]}T12:00:00")
    if d.tzinfo is not None:
        d = d.replace(tzinfo=None)
    return (d - _EPOCH).total_seconds()


def _resolve_root_credit(
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    root_tx_id: str,
) -> Dict[str, Any]:
    """Locate a credit transaction and the statement-holding entity that received it (TF-05)."""
    tx = next((t for t in transactions if t.tx_id == root_tx_id), None)
    if tx is None:
        raise ValueError("root_tx_id not found in this case")
    if tx.direction != TxDirection.CREDIT or float(tx.credit) <= 0:
        raise ValueError("root_tx_id must be a credit transaction")
    owner = resolve_statement_owners(statements, entities).get(tx.statement_id)
    if owner is None:
        raise ValueError("the statement holding root_tx_id is not linked to an entity")
    return {
        "tx_id": tx.tx_id,
        "entity_id": owner.entity_id,
        "amount": round(float(tx.credit), 2),
        "date": tx.txn_date,
        "timestamp": tx.txn_timestamp,
        "utr": tx.utr_number,
        "narration": tx.raw_narration,
        "counterparty_entity_id": tx.resolved_entity_id,
        "t": _tx_secs(tx),
    }


class _TraceState:
    """Outcome of one chronological attribution pass, restricted to the tree paths a caller asked to keep."""

    def __init__(self) -> None:
        self.node_attr: Dict[Tuple[str, ...], float] = defaultdict(float)
        self.node_txs: Dict[Tuple[str, ...], Dict[str, float]] = defaultdict(dict)
        self.misc_attr: Dict[Tuple[str, ...], float] = defaultdict(float)
        self.misc_txs: Dict[Tuple[str, ...], Dict[str, float]] = defaultdict(dict)
        self.misc_members: Dict[Tuple[str, ...], Dict[str, List[float]]] = defaultdict(dict)
        self.extra_children: Dict[Tuple[str, ...], Set[str]] = defaultdict(set)
        self.truncated = False
        self.work = 0
        self.budget_exhausted = False


def _trace(
    events: List[DirectedTransferEvent],
    entity_by_id: Dict[str, ResolvedEntity],
    system_ids: Set[str],
    root_id: str,
    root_credit: Optional[Dict[str, Any]],
    window_s: float,
    mode: str,
    trace_depth: int,
    misc_entity_ids: Set[str],
    misc_path_set: Set[Tuple[str, ...]],
    want: Callable[[Tuple[str, ...]], int],
    max_nodes: int,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    keep_window: Optional[Callable[[Tuple[str, ...]], bool]] = None,
    work_budget: int = ALL_MODE_WORK_BUDGET,
) -> _TraceState:
    """
    Single chronological pass attributing every outflow (TF-01/02/04). `want(child_path)` returns 1 to record a
    tree node, 2 to only count it as an unexpanded child of its parent, 0 to skip recording; attribution always
    runs to `trace_depth` so recorded amounts do not depend on which paths are displayed (TF-03).
    mode='all': paths are independent, so a window is only opened for a path `keep_window` accepts (default:
    one `want` records), and total work is capped at `work_budget` (sets truncated).
    """
    if keep_window is None:
        def keep_window(child: Tuple[str, ...]) -> bool:
            return want(child) == 1
    st = _TraceState()
    root_path: Tuple[str, ...] = (root_id,)
    lots_by_entity: Dict[str, List[List[Any]]] = defaultdict(list)
    lot_head: Dict[str, int] = defaultdict(int)
    windows_by_entity: Dict[str, Dict[Tuple[str, ...], float]] = defaultdict(dict)

    def expandable(entity_id: str, depth: int) -> bool:
        ent = entity_by_id.get(entity_id)
        return bool(ent and ent.has_statement and entity_id not in system_ids and depth < trace_depth)

    def open_window(entity_id: str, path: Tuple[str, ...], value: float, t: float) -> None:
        if mode == "all":
            windows = windows_by_entity[entity_id]
            close = t + window_s
            if windows.get(path, -1.0) < close:
                windows[path] = close
        else:
            lots_by_entity[entity_id].append([t, t + window_s, value, path])

    def emit(path: Tuple[str, ...], ev: DirectedTransferEvent, value: float, t: float) -> None:
        if mode == "all":
            if st.budget_exhausted:
                return
            st.work += 1
            if st.work > work_budget:
                st.budget_exhausted = True
                st.truncated = True
                return
        dst = ev.dst_entity_id
        if dst in path or dst not in entity_by_id:
            return  # funds returned to an ancestor are consumed but not re-expanded (a tree has no cycles)
        if len(path) > trace_depth:
            return
        child = path + (dst,)
        if dst in misc_entity_ids or child in misc_path_set:
            w_misc = want(path + ("\x00misc",))
            if w_misc == 1:
                st.misc_attr[path] += value
                st.misc_txs[path][ev.tx_id] = ev.amount
                mem = st.misc_members[path].setdefault(dst, [0.0, 0.0])
                mem[0] += value
                mem[1] += ev.amount
            elif w_misc == 2:
                st.extra_children[path].add("MISC")
            return
        w = want(child)
        if w == 1:
            if child not in st.node_attr and len(st.node_attr) >= max_nodes:
                st.truncated = True
            else:
                st.node_attr[child] += value
                st.node_txs[child][ev.tx_id] = ev.amount
        elif w == 2:
            st.extra_children[path].add(dst)
        if expandable(dst, len(path)) and (mode != "all" or keep_window(child)):
            open_window(dst, child, value, t)

    def open_lots(entity_id: str, t: float) -> Iterator[List[Any]]:
        """fifo: open, unexhausted lots oldest first (lots are appended in t_in order with one window length,
        so expiry is monotone and the exhausted/expired prefix never needs revisiting)."""
        lots = lots_by_entity.get(entity_id)
        if not lots:
            return
        head = lot_head[entity_id]
        while head < len(lots) and (lots[head][1] < t or lots[head][2] <= _EPS):
            head += 1
        lot_head[entity_id] = head
        for k in range(head, len(lots)):
            lot = lots[k]
            if lot[1] < t:
                continue
            if lot[0] > t:
                break
            yield lot

    def open_paths(entity_id: str, t: float) -> List[Tuple[str, ...]]:
        windows = windows_by_entity.get(entity_id)
        if not windows:
            return []
        for p in [p for p, close in windows.items() if close < t]:
            del windows[p]
        return list(windows)

    root_pending = root_credit is not None
    times = [_secs(ev) for ev in events]
    i = 0
    n_ev = len(events)
    while i < n_ev:
        j = i
        while j < n_ev and times[j] == times[i]:
            j += 1
        t = times[i]
        if root_pending and root_credit["t"] <= t:
            open_window(root_id, root_path, root_credit["amount"], root_credit["t"])
            root_pending = False
        pending: List[List[Any]] = []
        group_dsts = {events[k].dst_entity_id for k in range(i, j)}
        for k in range(i, j):
            ev = events[k]
            if ev.src_entity_id == root_id and root_credit is None:
                if ev.dst_entity_id != root_id and (not date_from or ev.txn_date >= date_from) and (
                    not date_to or ev.txn_date <= date_to
                ):
                    emit(root_path, ev, ev.amount, t)
            elif ev.src_entity_id in lots_by_entity or ev.src_entity_id in windows_by_entity or ev.src_entity_id in group_dsts:
                pending.append([ev, ev.amount, set()])
        progress = True
        while pending and progress:
            progress = False
            for item in pending:
                ev, need, seen_paths = item
                if mode == "all":
                    if st.budget_exhausted:
                        continue
                    for path in open_paths(ev.src_entity_id, t):
                        if path not in seen_paths:
                            seen_paths.add(path)
                            emit(path, ev, ev.amount, t)
                            progress = True
                    continue
                for lot in open_lots(ev.src_entity_id, t):
                    if need <= _EPS:
                        break
                    if lot[2] <= _EPS:
                        continue
                    take = need if need < lot[2] else lot[2]
                    lot[2] -= take
                    need -= take
                    emit(lot[3], ev, round(take, 2), t)
                    progress = True
                item[1] = need
            pending = [it for it in pending if mode == "all" or it[1] > _EPS]
        i = j
    return st


def _prepare(
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    credit_source_entity_id: Optional[str],
    window_days: Any,
    mode: Optional[str],
    fifo_cap: bool,
    root_tx_id: Optional[str],
) -> Tuple[str, Optional[Dict[str, Any]], Optional[str], List[DirectedTransferEvent], Dict[str, ResolvedEntity], Set[str]]:
    resolved_mode = (mode or ("fifo" if fifo_cap else "all")).strip().lower()
    if resolved_mode not in LAYERING_MODES:
        raise ValueError(f"mode must be one of {LAYERING_MODES}")
    if window_days is None or window_days < 0:
        raise ValueError("window_days must be non-negative")
    root_credit = None
    root_id = credit_source_entity_id
    if root_tx_id:
        root_credit = _resolve_root_credit(statements, transactions, entities, root_tx_id)
        if credit_source_entity_id and credit_source_entity_id != root_credit["entity_id"]:
            raise ValueError("root_tx_id was not received by credit_source_entity_id")
        root_id = root_credit["entity_id"]
    events, entity_by_id = extract_directed_transfer_events(statements, transactions, entities)
    system_ids = {e.entity_id for e in entities if is_system_entity(e)}
    return resolved_mode, root_credit, root_id, events, entity_by_id, system_ids


def _resolve_trace_depth(mode: str, display_depth: int, trace_depth: Optional[int]) -> int:
    if mode == "all":
        # 'all' paths are independent of each other: trace only one level below the display (to count children)
        return min(MAX_TRACE_DEPTH, display_depth + 1)
    td = DEFAULT_TRACE_DEPTH if trace_depth is None else int(trace_depth)
    return max(1, min(MAX_TRACE_DEPTH, max(td, display_depth)))


def build_layering_tree(
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    credit_source_entity_id: Optional[str],
    window_days: int = 14,
    max_depth: int = 4,
    fifo_cap: bool = True,
    misc_entity_ids: Optional[Set[str]] = None,
    mode: Optional[str] = None,
    max_nodes: int = DEFAULT_MAX_TREE_NODES,
    root_tx_id: Optional[str] = None,
    misc_paths: Optional[Iterable[Any]] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    trace_depth: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Build the progressive multi-hop Layering Detection tree matching CypherTrace specs:
      - Blue Root: Credit Source ('Total Amount', 'Click to view details')
      - Green Node: Has Statement ('✓ Has Statement', 'Amount Spent', 'Click to drill deeper', 'Move to Misc')
      - Grey Node: Details Only ('Details Only', 'Amount Spent', 'Move to Misc')
      - Misc Node: Aggregated children marked via 'Move to Misc'

    Every traced inflow into a node is a credit lot open over [t_in, t_in + window_days] (TF-02).
    mode='fifo' (default): the node's outflows are matched chronologically against its open lots, oldest
      first, attributing min(outflow remaining, lot remaining); the sum attributed to a node's children never
      exceeds what was attributed to the node (TF-01). Attribution runs to `trace_depth` (default
      max(max_depth, 8)) so the amounts are identical when a node is later drilled into (TF-03).
    mode='all': every outflow inside any open lot window is shown at its full debit value (not capped) (TF-04).
    `root_tx_id` roots the tree at one specific credit: the receiving account becomes the root and only its
      outflows inside that credit's window are traced, FIFO-capped at the credit amount (TF-05). Otherwise
      the root's outflows (optionally limited to date_from..date_to) are all traced.
    `misc_entity_ids` folds an entity into Misc everywhere; `misc_paths` (node paths, root first, as lists or
      "id>id>id" strings) fold only that branch occurrence into its parent's Misc node (TF-06). Misc nodes list
      their members with names and amounts (TF-07).
    Nodes carry `path`, `path_key`, `expandable` and `has_more_children`; nodes at max_depth with
    has_more_children=True can be expanded lazily with expand_layering_node(). System parties (cash, charges,
    UNRESOLVED_*) appear as sinks but are never expanded (NLP-10).
    """
    resolved_mode, root_credit, root_id, events, entity_by_id, system_ids = _prepare(
        statements, transactions, entities, credit_source_entity_id, window_days, mode, fifo_cap, root_tx_id
    )
    misc_entity_ids = set(misc_entity_ids or ())
    misc_path_set = _norm_misc_paths(misc_paths)
    source_ent = entity_by_id.get(root_id or "")
    if not source_ent:
        return {
            "root": None,
            "legend_counts": {"credit_source": 0, "has_statement": 0, "details_only": 0, "total_entities": 0},
            "mode": resolved_mode,
        }
    max_depth = int(max_depth)
    if max_depth < 1:
        raise ValueError("max_depth must be >= 1")
    t_depth = _resolve_trace_depth(resolved_mode, max_depth, trace_depth)
    count_more = t_depth > max_depth

    def want(child: Tuple[str, ...]) -> int:
        n = len(child)
        if n <= max_depth + 1:
            return 1
        if n == max_depth + 2 and count_more:
            return 2
        return 0

    st = _trace(
        events, entity_by_id, system_ids, root_id, root_credit, float(window_days) * 86400.0, resolved_mode,
        t_depth, misc_entity_ids, misc_path_set, want, max_nodes, date_from, date_to,
    )

    if root_credit is not None:
        root_amount = root_credit["amount"]
        root_tx_ids = [root_credit["tx_id"]]
    else:
        root_out = [
            ev for ev in events
            if ev.src_entity_id == root_id and ev.dst_entity_id != root_id
            and (not date_from or ev.txn_date >= date_from) and (not date_to or ev.txn_date <= date_to)
        ]
        root_amount = round(sum(e.amount for e in root_out), 2)
        root_tx_ids = [e.tx_id for e in root_out]

    legend_entities: Dict[str, str] = {root_id: "CREDIT_SOURCE"}
    seq = [0]
    builder = _NodeBuilder(st, entity_by_id, system_ids, misc_entity_ids, seq)

    def build(path: Tuple[str, ...]) -> List[LayeringNode]:
        kids = builder.children(path, t_depth)
        for kid in kids:
            if not kid.is_misc:
                legend_entities.setdefault(kid.entity_id, kid.role)
                kid.children = build(tuple(kid.path))
        return kids

    root_path = (root_id,)
    root_node = AttributedLayeringNode(
        node_id=f"L0_{root_id[:8]}",
        entity_id=root_id,
        entity_name=source_ent.canonical_name,
        role="CREDIT_SOURCE",
        amount=root_amount,
        depth=0,
        has_statement=source_ent.has_statement,
        tx_ids=root_tx_ids,
        children=build(root_path),
        attributed_amount=root_amount,
        debit_amount=root_amount,
        is_system=root_id in system_ids,
        path=[root_id],
        path_key=misc_path_key(root_path),
        expandable=True,
    )
    root_node.has_more_children = bool(root_node.children)

    has_stmt_count = sum(1 for v in legend_entities.values() if v == "HAS_STATEMENT")
    details_count = sum(1 for v in legend_entities.values() if v == "DETAILS_ONLY")
    root_credit_out = None
    if root_credit is not None:
        root_credit_out = {k: v for k, v in root_credit.items() if k != "t"}

    return {
        "root": root_node.to_dict(),
        "legend_counts": {
            "credit_source": 1,
            "has_statement": has_stmt_count,
            "details_only": details_count,
            "total_entities": len(legend_entities),
        },
        "window_days": window_days,
        "max_depth": max_depth,
        "trace_depth": t_depth,
        "mode": resolved_mode,
        "node_count": seq[0] + 1,
        "truncated": st.truncated,
        "work_budget_exhausted": st.budget_exhausted,
        "misc_entity_ids": sorted(misc_entity_ids),
        "misc_paths": sorted(misc_path_key(p) for p in misc_path_set),
        "root_tx_id": root_tx_id or None,
        "root_credit": root_credit_out,
        "date_from": date_from if root_credit is None else None,
        "date_to": date_to if root_credit is None else None,
    }


class _NodeBuilder:
    def __init__(
        self,
        st: _TraceState,
        entity_by_id: Dict[str, ResolvedEntity],
        system_ids: Set[str],
        misc_entity_ids: Set[str],
        seq: List[int],
    ) -> None:
        self.st = st
        self.entity_by_id = entity_by_id
        self.system_ids = system_ids
        self.misc_entity_ids = misc_entity_ids
        self.seq = seq
        self.children_of: Dict[Tuple[str, ...], List[Tuple[str, ...]]] = defaultdict(list)
        for path in st.node_attr:
            self.children_of[path[:-1]].append(path)

    def children(self, path: Tuple[str, ...], t_depth: int) -> List["AttributedLayeringNode"]:
        """One level of children (their own `children` left empty) below `path`."""
        st = self.st
        depth = len(path)
        kids: List[AttributedLayeringNode] = []
        for child in sorted(self.children_of.get(path, ()), key=lambda p: (-st.node_attr[p], p[-1])):
            dst_id = child[-1]
            ent = self.entity_by_id[dst_id]
            role = "HAS_STATEMENT" if ent.has_statement else "DETAILS_ONLY"
            self.seq[0] += 1
            attributed = round(st.node_attr[child], 2)
            can_expand = bool(ent.has_statement and dst_id not in self.system_ids and depth < t_depth)
            more = bool(self.children_of.get(child)) or bool(st.extra_children.get(child)) or st.misc_attr.get(child, 0.0) > _EPS
            kids.append(
                AttributedLayeringNode(
                    node_id=f"L{depth}_{dst_id[:8]}_{self.seq[0]}",
                    entity_id=dst_id,
                    entity_name=ent.canonical_name,
                    role=role,
                    amount=attributed,
                    depth=depth,
                    has_statement=ent.has_statement,
                    tx_ids=list(st.node_txs[child]),
                    children=[],
                    attributed_amount=attributed,
                    debit_amount=round(sum(st.node_txs[child].values()), 2),
                    is_system=dst_id in self.system_ids,
                    path=list(child),
                    path_key=misc_path_key(child),
                    expandable=can_expand,
                    has_more_children=can_expand and more,
                )
            )
        if st.misc_attr.get(path, 0.0) > _EPS:
            parent_id = path[-1]
            self.seq[0] += 1
            m_attr = round(st.misc_attr[path], 2)
            members = []
            for mid, (attr, debit) in sorted(st.misc_members[path].items(), key=lambda kv: (-kv[1][0], kv[0])):
                ment = self.entity_by_id.get(mid)
                members.append({
                    "entity_id": mid,
                    "entity_name": ment.canonical_name if ment else mid,
                    "has_statement": bool(ment and ment.has_statement),
                    "attributed_amount": round(attr, 2),
                    "debit_amount": round(debit, 2),
                    "path": list(path) + [mid],
                    "path_key": misc_path_key(path + (mid,)),
                    "scope": "entity" if mid in self.misc_entity_ids else "path",
                })
            kids.append(
                AttributedLayeringNode(
                    node_id=f"L{depth}_MISC_{parent_id[:6]}_{self.seq[0]}",
                    entity_id=f"MISC_{parent_id}",
                    entity_name=f"Miscellaneous ({len(members)})",
                    role="MISC",
                    amount=m_attr,
                    depth=depth,
                    has_statement=False,
                    is_misc=True,
                    misc_count=len(members),
                    tx_ids=list(st.misc_txs[path]),
                    children=[],
                    attributed_amount=m_attr,
                    debit_amount=round(sum(st.misc_txs[path].values()), 2),
                    path=list(path) + ["MISC"],
                    path_key=misc_path_key(path + ("MISC",)),
                    misc_members=members,
                )
            )
        return kids


def expand_layering_node(
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    credit_source_entity_id: Optional[str],
    node_path: Any,
    window_days: int = 14,
    mode: Optional[str] = None,
    misc_entity_ids: Optional[Set[str]] = None,
    misc_paths: Optional[Iterable[Any]] = None,
    root_tx_id: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    trace_depth: Optional[int] = None,
    max_nodes: int = DEFAULT_MAX_TREE_NODES,
) -> Dict[str, Any]:
    """
    Lazy drill-down (TF-03): return exactly one level of children below the tree node at `node_path` (a list of
    entity ids starting with the root, or an "id>id>id" path_key). Amounts equal those build_layering_tree
    reports for the same parameters (same trace_depth). Each child has children=[] and has_more_children telling
    the UI whether it can be expanded again. Raises ValueError for a malformed path or a path whose first id is
    not the root.
    """
    if isinstance(node_path, str):
        path = tuple(x for x in node_path.split(PATH_SEPARATOR) if x)
    else:
        path = tuple(str(x) for x in (node_path or ()) if x)
    if not path:
        raise ValueError("node_path must contain at least the root entity id")
    if len(set(path)) != len(path):
        raise ValueError("node_path must not repeat an entity")
    resolved_mode, root_credit, root_id, events, entity_by_id, system_ids = _prepare(
        statements, transactions, entities, credit_source_entity_id or path[0], window_days, mode, True, root_tx_id
    )
    if path[0] != root_id:
        raise ValueError("node_path must start at the root entity")
    if root_id not in entity_by_id:
        raise ValueError("unknown root entity")
    for eid in path:
        if eid not in entity_by_id:
            raise ValueError("node_path contains an unknown entity")
    misc_entity_ids = set(misc_entity_ids or ())
    misc_path_set = _norm_misc_paths(misc_paths)
    depth = len(path) - 1
    t_depth = _resolve_trace_depth(resolved_mode, depth + 1, trace_depth)
    if resolved_mode == "fifo":
        t_depth = max(t_depth, depth + 2)
    else:
        t_depth = depth + 2  # one more level so has_more_children is known
    t_depth = min(t_depth, MAX_TRACE_DEPTH)
    n = len(path)

    def want(child: Tuple[str, ...]) -> int:
        if len(child) == n + 1 and child[:n] == path:
            return 1
        if len(child) == n + 2 and child[:n] == path:
            return 2
        return 0

    def keep_window(child: Tuple[str, ...]) -> bool:
        k = len(child)
        return (k <= n and child == path[:k]) or want(child) == 1

    st = _trace(
        events, entity_by_id, system_ids, root_id, root_credit, float(window_days) * 86400.0, resolved_mode,
        t_depth, misc_entity_ids, misc_path_set, want, max_nodes, date_from, date_to, keep_window=keep_window,
    )
    # extra_children of a child are recorded under the child path; NodeBuilder reads them for has_more_children
    builder = _NodeBuilder(st, entity_by_id, system_ids, misc_entity_ids, [0])
    kids = builder.children(path, t_depth)
    ent = entity_by_id[path[-1]]
    total = round(sum(k.attributed_amount for k in kids), 2)
    return {
        "path": list(path),
        "path_key": misc_path_key(path),
        "entity_id": path[-1],
        "entity_name": ent.canonical_name,
        "depth": depth,
        "children": [k.to_dict() for k in kids],
        "children_total": total,
        "mode": resolved_mode,
        "window_days": window_days,
        "trace_depth": t_depth,
        "truncated": st.truncated,
        "work_budget_exhausted": st.budget_exhausted,
        "root_tx_id": root_tx_id or None,
    }
