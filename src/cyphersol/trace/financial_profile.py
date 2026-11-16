"""CypherTrace Individual Financial Profile compiler per entity/subject."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from cyphersol.models import (
    BankAccountStatement,
    MuleRiskAssessment,
    NormalizedTransaction,
    ResolvedEntity,
)
from cyphersol.nlp.entity_resolver import normalize_entity_name
from cyphersol.taxonomy import CATEGORY_BY_ID

_FIXED_ABB_DAYS = (5, 10, 15, 20, 25)


def _to_date(value: Optional[str]) -> Optional[dt.date]:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _account_eod_series(
    stmts: List[BankAccountStatement], txns: List[NormalizedTransaction]
) -> Dict[dt.date, float]:
    """
    End-of-day balance for one account over its statements' coverage, carrying the last balance forward on
    days without activity. Each statement's opening balance seeds its first day. Uses the reported running
    balance when the account has one, otherwise reconstructs it from opening balance plus net flow.
    """
    ordered_stmts = sorted(stmts, key=lambda s: (s.period_start or "", s.period_end or ""))
    stmt_rank = {s.statement_id: i for i, s in enumerate(ordered_stmts)}
    ordered = sorted(txns, key=lambda t: (t.txn_date, stmt_rank.get(t.statement_id, 0), t.row_index))

    starts = [d for d in (_to_date(s.period_start) for s in ordered_stmts) if d]
    ends = [d for d in (_to_date(s.period_end) for s in ordered_stmts) if d]
    tx_dates = [d for d in (_to_date(t.txn_date) for t in ordered) if d]
    start = min(starts + tx_dates[:1]) if (starts or tx_dates) else None
    end = max(ends + tx_dates[-1:]) if (ends or tx_dates) else None
    if start is None or end is None or end < start:
        return {}

    openings: Dict[dt.date, float] = {}
    for s in ordered_stmts:
        d = _to_date(s.period_start)
        if d and d not in openings:
            openings[d] = float(s.opening_balance)

    has_running = any(float(t.running_balance) != 0.0 for t in ordered)
    eod: Dict[dt.date, float] = {}
    if has_running:
        for t in ordered:
            d = _to_date(t.txn_date)
            if d:
                eod[d] = float(t.running_balance)
    else:
        bal = float(ordered_stmts[0].opening_balance) if ordered_stmts else 0.0
        for t in ordered:
            d = _to_date(t.txn_date)
            bal += float(t.credit) - float(t.debit)
            if d:
                eod[d] = bal

    current = float(ordered_stmts[0].opening_balance) if ordered_stmts else 0.0
    series: Dict[dt.date, float] = {}
    day = start
    one = dt.timedelta(days=1)
    while day <= end:
        if day in openings and day not in eod:
            current = openings[day]
        if day in eod:
            current = eod[day]
        series[day] = current
        day += one
    return series


def _abb_metrics(
    entity_stmts: List[BankAccountStatement], txns_by_stmt: Dict[str, List[NormalizedTransaction]]
) -> Dict[str, Any]:
    """Per-account EOD series summed per day into a consolidated series (TG-03 / EDGE-21)."""
    by_account: Dict[str, List[BankAccountStatement]] = defaultdict(list)
    for s in entity_stmts:
        by_account[s.account_number].append(s)

    consolidated: Dict[dt.date, float] = defaultdict(float)
    per_account: List[Dict[str, Any]] = []
    for acc, stmts in sorted(by_account.items()):
        acc_txns = [t for s in stmts for t in txns_by_stmt.get(s.statement_id, [])]
        series = _account_eod_series(stmts, acc_txns)
        for d, bal in series.items():
            consolidated[d] += bal
        per_account.append({
            "account_number": acc,
            "bank_name": stmts[0].bank_name,
            "period_start": min(s.period_start for s in stmts),
            "period_end": max(s.period_end for s in stmts),
            "days": len(series),
            "daily_abb": round(sum(series.values()) / len(series), 2) if series else None,
        })

    if not consolidated:
        return {"daily_abb": None, "fixed_date_abb": None, "monthly_abb": [], "abb_by_account": per_account}

    monthly: Dict[str, List[float]] = defaultdict(list)
    fixed: List[float] = []
    for d in sorted(consolidated):
        bal = consolidated[d]
        monthly[d.strftime("%Y-%m")].append(bal)
        if d.day in _FIXED_ABB_DAYS or (d + dt.timedelta(days=1)).day == 1:
            fixed.append(bal)
    return {
        "daily_abb": round(sum(consolidated.values()) / len(consolidated), 2),
        "fixed_date_abb": round(sum(fixed) / len(fixed), 2) if fixed else None,
        "monthly_abb": [
            {"month": m, "abb": round(sum(v) / len(v), 2), "days": len(v)} for m, v in sorted(monthly.items())
        ],
        "abb_by_account": per_account,
    }


def _opening_closing(entity_stmts: List[BankAccountStatement]) -> Tuple[Optional[float], Optional[float]]:
    """Earliest opening and latest closing balance per account, summed across accounts (TG-02)."""
    if not entity_stmts:
        return None, None
    by_account: Dict[str, List[BankAccountStatement]] = defaultdict(list)
    for s in entity_stmts:
        by_account[s.account_number].append(s)
    opening = closing = 0.0
    for stmts in by_account.values():
        first = min(stmts, key=lambda s: (s.period_start or "", s.period_end or ""))
        last = max(stmts, key=lambda s: (s.period_end or "", s.period_start or ""))
        opening += float(first.opening_balance)
        closing += float(last.closing_balance)
    return opening, closing


def _entity_statements(
    ent: ResolvedEntity, statements: List[BankAccountStatement], entities: List[ResolvedEntity]
) -> List[BankAccountStatement]:
    """
    Account-first mapping (TG-15): a statement belongs to the entity when its account is linked to the entity.
    A holder-name match is used only for statements whose account is not linked to any other entity.
    """
    linked = {a for a in ent.linked_account_numbers if a}
    others: Dict[str, str] = {}
    for e in entities:
        if e.entity_id != ent.entity_id:
            for a in e.linked_account_numbers:
                if a:
                    others.setdefault(a, e.entity_id)
    alias_norms = {normalize_entity_name(a) for a in ent.aliases} | {normalize_entity_name(ent.canonical_name)}
    alias_norms.discard("")
    out = []
    for s in statements:
        if s.account_number and s.account_number in linked:
            out.append(s)
        elif s.account_number in others and s.account_number not in linked:
            continue
        elif normalize_entity_name(s.account_holder_name) in alias_norms:
            out.append(s)
    return out


def _statement_owners(statements: List[BankAccountStatement], entities: List[ResolvedEntity]) -> Dict[str, str]:
    """statement_id -> party key of the statement's owning entity (account first, then holder name)."""
    by_acct: Dict[str, str] = {}
    by_name: Dict[str, str] = {}
    for e in entities:
        for a in e.linked_account_numbers:
            if a:
                by_acct.setdefault(a, e.entity_id)
        if e.has_statement:
            for n in [e.canonical_name, *e.aliases]:
                nn = normalize_entity_name(n)
                if nn:
                    by_name.setdefault(nn, e.entity_id)
    out: Dict[str, str] = {}
    for s in statements:
        eid = by_acct.get(s.account_number) or by_name.get(normalize_entity_name(s.account_holder_name))
        if eid:
            out[s.statement_id] = f"E:{eid}"
    return out


def _name_key(name: Optional[str]) -> Optional[str]:
    norm = normalize_entity_name(name or "")
    return f"N:{norm}" if norm else None


def _party_key(tx: NormalizedTransaction, entity_by_id: Dict[str, ResolvedEntity]) -> str:
    """Counterparty identity (TG-14): resolved entity, else normalised name/VPA; never the subject itself."""
    if tx.resolved_entity_id and tx.resolved_entity_id in entity_by_id:
        return f"E:{tx.resolved_entity_id}"
    return (
        _name_key(tx.extracted_counterparty)
        or (f"V:{tx.counterparty_vpa_or_acc.strip().lower()}" if tx.counterparty_vpa_or_acc else None)
        or "UNIDENTIFIED"
    )


def _party_label(
    key: str, tx: NormalizedTransaction, entity_by_id: Dict[str, ResolvedEntity], derived: bool
) -> str:
    if key.startswith("E:"):
        ent = entity_by_id.get(key[2:])
        if ent:
            return ent.canonical_name
    if key == "UNIDENTIFIED":
        return "Unidentified counterparty"
    if derived:
        return tx.source_holder_name or key[2:]
    if key.startswith("V:"):
        return tx.counterparty_vpa_or_acc or key[2:]
    return tx.extracted_counterparty or key[2:]


def _top_parties(vols: Dict[str, float], counts: Dict[str, int], labels: Dict[str, str], n: int = 15) -> List[Dict[str, Any]]:
    out = []
    for k, v in sorted(vols.items(), key=lambda kv: kv[1], reverse=True)[:n]:
        out.append({
            "party": labels.get(k, k),
            "entity_id": k[2:] if k.startswith("E:") else None,
            "amount": round(v, 2),
            "tx_count": int(counts.get(k, 0)),
        })
    return out


def _pct(part: float, whole: float) -> float:
    return round(100.0 * part / whole, 2) if whole > 0 else 0.0


def _category_group(cid: Optional[int]) -> str:
    spec = CATEGORY_BY_ID.get(cid) if cid is not None else None
    return spec.group if spec else ""


def build_individual_financial_profile(
    entity_id: str,
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    mule_assessments: List[MuleRiskAssessment],
) -> Dict[str, Any]:
    """
    Build comprehensive monthly, category, channel, counterparty, and risk profile for one subject.
    For a Details-Only entity (no uploaded statement) the rows come from its counterparties' statements and
    are read from the entity's side: a statement debit to it is its credit and vice versa (TG-01); the
    profile is labelled derived_from_counterparty_statements=True and carries no balances.
    """
    ent = next((e for e in entities if e.entity_id == entity_id), None)
    if ent is None:
        return {"error": f"Entity '{entity_id}' not found"}

    linked_accounts = set(ent.linked_account_numbers)
    entity_stmts = _entity_statements(ent, statements, entities)
    entity_by_id = {e.entity_id: e for e in entities}
    owner_by_stmt = _statement_owners(statements, entities)
    stmt_ids = {s.statement_id for s in entity_stmts}
    derived = not stmt_ids

    if stmt_ids:
        relevant_txns = [t for t in transactions if t.statement_id in stmt_ids and not t.is_duplicate_ignored]
    else:
        relevant_txns = [t for t in transactions if t.resolved_entity_id == entity_id and not t.is_duplicate_ignored]

    relevant_txns.sort(key=lambda t: (t.txn_date, t.row_index))

    def flows(tx: NormalizedTransaction) -> Tuple[float, float]:
        """(credit, debit) from the profiled entity's own perspective."""
        if derived:
            return float(tx.debit), float(tx.credit)
        return float(tx.credit), float(tx.debit)

    monthly: Dict[str, Dict[str, float]] = defaultdict(lambda: {"credit": 0.0, "debit": 0.0, "count": 0})
    cat_dist: Dict[str, Dict[str, float]] = defaultdict(lambda: {"credit": 0.0, "debit": 0.0, "count": 0})
    channel_mix: Dict[str, Dict[str, float]] = defaultdict(lambda: {"amount": 0.0, "credit": 0.0, "debit": 0.0, "count": 0})
    senders: Dict[str, float] = defaultdict(float)
    receivers: Dict[str, float] = defaultdict(float)
    sender_counts: Dict[str, int] = defaultdict(int)
    receiver_counts: Dict[str, int] = defaultdict(int)
    party_label: Dict[str, str] = {}
    cat_ids: Dict[str, int] = {}
    total_cr = total_dr = 0.0

    for tx in relevant_txns:
        cr, dr = flows(tx)
        total_cr += cr
        total_dr += dr
        ym = tx.txn_date[:7]
        monthly[ym]["credit"] += cr
        monthly[ym]["debit"] += dr
        monthly[ym]["count"] += 1

        cat_dist[tx.category_name]["credit"] += cr
        cat_dist[tx.category_name]["debit"] += dr
        cat_dist[tx.category_name]["count"] += 1
        cat_ids.setdefault(tx.category_name, tx.category_id)

        channel = tx.payment_channel or "OTHER"
        channel_mix[channel]["amount"] += cr + dr
        channel_mix[channel]["credit"] += cr
        channel_mix[channel]["debit"] += dr
        channel_mix[channel]["count"] += 1

        if derived:
            party_key = owner_by_stmt.get(tx.statement_id) or _name_key(tx.source_holder_name) or "UNIDENTIFIED"
        else:
            party_key = _party_key(tx, entity_by_id)
        if party_key not in party_label:
            party_label[party_key] = _party_label(party_key, tx, entity_by_id, derived)
        if cr > 0:
            senders[party_key] += cr
            sender_counts[party_key] += 1
        if dr > 0:
            receivers[party_key] += dr
            receiver_counts[party_key] += 1

    if derived:
        opening_bal = closing_bal = None
        abb: Dict[str, Any] = {"daily_abb": None, "fixed_date_abb": None, "monthly_abb": [], "abb_by_account": []}
    else:
        opening_bal, closing_bal = _opening_closing(entity_stmts)
        txns_by_stmt: Dict[str, List[NormalizedTransaction]] = defaultdict(list)
        for t in relevant_txns:
            txns_by_stmt[t.statement_id].append(t)
        abb = _abb_metrics(entity_stmts, txns_by_stmt)
    monthly_abb_map = {r["month"]: r["abb"] for r in abb["monthly_abb"]}

    entity_mules = [
        m.to_dict() for m in mule_assessments
        if m.entity_id == entity_id or m.account_number in linked_accounts
        or (not derived and m.account_number in {s.account_number for s in entity_stmts})
    ]

    return {
        "entity": ent.to_dict(),
        "derived_from_counterparty_statements": derived,
        "statements": [s.to_dict() for s in entity_stmts],
        "kpis": {
            "total_transactions": len(relevant_txns),
            "opening_balance": round(opening_bal, 2) if opening_bal is not None else None,
            "closing_balance": round(closing_bal, 2) if closing_bal is not None else None,
            "active_accounts": len({s.account_number for s in entity_stmts}),
            "total_credits": round(total_cr, 2),
            "total_debits": round(total_dr, 2),
            "net_flow": round(total_cr - total_dr, 2),
            "daily_abb": abb["daily_abb"],
            "fixed_date_abb": abb["fixed_date_abb"],
        },
        "monthly_abb": abb["monthly_abb"],
        "abb_by_account": abb["abb_by_account"],
        "mule_assessments": entity_mules,
        "monthly_breakdown": [
            {
                "month": m,
                "credit": round(v["credit"], 2),
                "debit": round(v["debit"], 2),
                "net": round(v["credit"] - v["debit"], 2),
                "tx_count": int(v["count"]),
                "abb": monthly_abb_map.get(m),
            }
            for m, v in sorted(monthly.items())
        ],
        "category_breakdown": [
            {
                "category": c,
                "category_id": cat_ids.get(c),
                "group": _category_group(cat_ids.get(c)),
                "credit": round(v["credit"], 2),
                "debit": round(v["debit"], 2),
                "tx_count": int(v["count"]),
                "share_pct": _pct(v["credit"] + v["debit"], total_cr + total_dr),
            }
            for c, v in sorted(cat_dist.items(), key=lambda kv: kv[1]["credit"] + kv[1]["debit"], reverse=True)
        ],
        "channel_mix": [
            {
                "channel": ch,
                "amount": round(v["amount"], 2),
                "credit": round(v["credit"], 2),
                "debit": round(v["debit"], 2),
                "tx_count": int(v["count"]),
                "share_pct": _pct(v["amount"], total_cr + total_dr),
            }
            for ch, v in sorted(channel_mix.items(), key=lambda kv: kv[1]["amount"], reverse=True)
        ],
        "top_senders": _top_parties(senders, sender_counts, party_label),
        "top_beneficiaries": _top_parties(receivers, receiver_counts, party_label),
        "recent_transactions": [t.to_dict() for t in reversed(relevant_txns[-100:])],
    }
