"""CypherEdge Loan Eligibility, Average Bank Balance (ABB), EMI Load, Bounce & FOIR Engine."""

from __future__ import annotations

import calendar
import math
import datetime as dt
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from cyphersol.models import BankAccountStatement, NormalizedTransaction, TxDirection

FIXED_ABB_DAYS = (5, 10, 15, 20, 25)  # plus the last day of each month

# --- Tokenisation ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[A-Z0-9]+")
_MANDATE_TOKENS = {"NACH", "ECS", "ACH", "SI", "MANDATE", "EMANDATE", "ENACH", "STANDING", "AUTODEBIT"}
_MANDATE_PHRASES = ("AUTO DEBIT", "STANDING INSTRUCTION", "E-MANDATE", "E MANDATE")
_INSTRUMENT_TOKENS = {"CHQ", "CHEQUE", "CHK", "CLG", "CLEARING", "NACH", "ECS", "ACH", "ENACH", "SI", "MANDATE"}
_RETURN_TOKENS = {"RETURN", "RETURNED", "RTN", "RET", "RETD", "BOUNCE", "BOUNCED", "DISHONOUR", "DISHONOURED",
                  "DISHONOR", "DISHONORED", "UNPAID", "REJECT", "REJECTED"}
_CHARGE_TOKENS = {"CHG", "CHGS", "CHRG", "CHRGS", "CHARGE", "CHARGES", "FEE", "FEES", "PENALTY", "PENAL", "GST"}
_INWARD_TOKENS = {"IW", "INWARD"}
_OUTWARD_TOKENS = {"OW", "OUTWARD"}
_BOUNCE_EXCLUDE_PHRASES = ("INCOME TAX RETURN", "TAX RETURN", "GST RETURN", "ITR REFUND", "REFUND", "CASHBACK",
                           "REVERSAL OF CHARGE")
_REVERSAL_TOKENS = {"REV", "REVERSAL", "REVERSED", "REVERSE", "REFUND", "REFUNDED", "WAIVER", "WAIVED"}
_TAX_ON_CHARGE_TOKENS = {"GST", "CGST", "SGST", "IGST", "TAX", "ST"}
_LENDER_STOPWORDS = _MANDATE_TOKENS | {"DR", "CR", "DEBIT", "CREDIT", "TO", "BY", "FROM", "FOR", "THE", "OF",
                                       "PAYMENT", "PMT", "TXN", "REF", "NO", "ID", "UMRN", "INST", "INSTALMENT",
                                       "INSTALLMENT", "COLLECTION", "AUTO", "DEBITED", "PRESENTATION", "MS", "M"}
# Investments / insurance / taxes run on mandates too, but are not loan obligations.
_NON_EMI_CATEGORIES = {31, 32, 33, 35, 36, 37, 38, 39, 40, 26, 27, 28, 29}
_NON_EMI_TOKENS = {"SIP", "MUTUAL", "MF", "INSURANCE", "PREMIUM", "LIC", "NPS", "PPF", "RD", "ELSS"}
_EMI_CATEGORIES = {23, 24, 25}
# A single loan debit with these words is a part-payment / foreclosure, not the first instalment of an EMI.
_ONE_OFF_REPAYMENT_TOKENS = {"PART", "PREPAY", "PREPAYMENT", "FORECLOSURE", "FORECLOSE", "CLOSURE",
                             "PRECLOSURE", "LUMPSUM", "SETTLEMENT", "OTS"}

EMI_AMOUNT_BAND = 0.05      # +/- 5 %
EMI_DAY_TOLERANCE = 5       # debit day within +/- 5 days of the usual day
EMI_MIN_CONSECUTIVE = 2
BOUNCE_PAIR_WINDOW_DAYS = 7

# Banking-surrogate income proxy for self-employed borrowers (EDGE-26): exposed as parameters.
DEFAULT_BUSINESS_INCOME_FACTOR = 0.20   # share of average monthly net business credits treated as income
DEFAULT_ABB_INCOME_FACTOR = 0.50        # share of fixed-date ABB treated as monthly income

# Overdraft / penal events (EDGE-25, FR-EDGE-04 s.3)
_MIN_BAL_RE = re.compile(r"\b(MIN(IMUM)?\s*BAL(ANCE)?|NON[\s-]*MAINT\w*|AMB|MAB|AQB|AVG\s*BAL(ANCE)?)\b")
_TOD_RE = re.compile(r"\b(TOD|TEMP(ORARY)?\s*O/?D|OVERDRAWN|OVERDRAWING|EXCESS\s*(DRAWING|DRAWN|INT|INTEREST)|"
                     r"OVER\s*LIMIT|OVERLIMIT)\b")
_PENAL_RE = re.compile(r"\b(PENAL|PENALTY|LATE\s*PAYMENT|LPP|OVERDUE\s*INT\w*)\b")
_OD_ACCOUNT_TOKENS = frozenset({"od", "cc", "overdraft", "occ"})


def _tokens(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").upper().replace("I/W", "IW").replace("O/W", "OW"))


def _d(iso: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat((iso or "")[:10])
    except ValueError:
        return None


def _month_index(d: dt.date) -> int:
    return d.year * 12 + d.month - 1


def _iter_months(start: dt.date, end: dt.date) -> List[str]:
    out: List[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


# --- Accounts and balances ------------------------------------------------------------------

@dataclass
class _Account:
    key: str
    label: str
    statement_ids: List[str]
    start: dt.date
    end: dt.date
    opening: float
    txns: List[NormalizedTransaction] = field(default_factory=list)
    eod: Dict[dt.date, float] = field(default_factory=dict)
    # Covered statement periods: (start, end, opening balance or None to carry forward) (EDGE-22).
    segments: List[Tuple[dt.date, dt.date, Optional[float]]] = field(default_factory=list)
    account_type: str = ""

    def coverage_gaps(self) -> List[Dict[str, Any]]:
        gaps: List[Dict[str, Any]] = []
        for (_, prev_end, _), (nxt_start, _, _) in zip(self.segments, self.segments[1:]):
            first, last = prev_end + dt.timedelta(days=1), nxt_start - dt.timedelta(days=1)
            if first <= last:
                gaps.append({"from": first.isoformat(), "to": last.isoformat(), "days": (last - first).days + 1})
        return gaps


def _build_accounts(
    active: Sequence[NormalizedTransaction], statements: Sequence[BankAccountStatement]
) -> List[_Account]:
    stmt_by_id = {s.statement_id: s for s in statements}
    groups: Dict[str, List[NormalizedTransaction]] = defaultdict(list)
    stmts_by_key: Dict[str, List[BankAccountStatement]] = defaultdict(list)
    for s in statements:
        stmts_by_key[f"{s.bank_code}:{s.account_number}"].append(s)
    for tx in active:
        st = stmt_by_id.get(tx.statement_id)
        key = f"{st.bank_code}:{st.account_number}" if st else f"TX:{tx.source_account_number or tx.statement_id}"
        groups[key].append(tx)

    # Accounts without a statement record share the case's overall transaction span.
    orphan_dates = [d for k, txs in groups.items() if k.startswith("TX:") for d in (_d(t.txn_date) for t in txs) if d]
    orphan_span = (min(orphan_dates), max(orphan_dates)) if orphan_dates else None

    accounts: List[_Account] = []
    for key in sorted(set(groups) | set(stmts_by_key)):
        sts = sorted(stmts_by_key.get(key, []), key=lambda s: (s.period_start or "", s.statement_id))
        order = {s.statement_id: i for i, s in enumerate(sts)}
        direction = _row_direction(groups.get(key, []))
        txs = sorted(
            groups.get(key, []),
            key=lambda t: (t.txn_date, order.get(t.statement_id, 0), direction.get(t.statement_id, 1) * t.row_index),
        )
        if sts:
            starts = [d for d in (_d(s.period_start) for s in sts) if d]
            ends = [d for d in (_d(s.period_end) for s in sts) if d]
            tx_dates = [d for d in (_d(t.txn_date) for t in txs) if d]
            start = min(starts + tx_dates) if starts or tx_dates else None
            end = max(ends + tx_dates) if ends or tx_dates else None
            if start is None or end is None:
                continue
            first = sts[0]
            if txs and txs[0].statement_id == first.statement_id:
                # The first row's implied opening is authoritative when it lies in the first statement.
                opening = float(txs[0].running_balance - txs[0].credit + txs[0].debit)
            else:
                opening = float(first.opening_balance or 0)
            acct = first.account_number or ""
            label = f"{first.bank_code} - {acct[-4:] if len(acct) >= 4 else acct}"
            sids = [s.statement_id for s in sts]
            segments = _statement_segments(sts, txs, opening)
            acct_type = first.account_type or ""
        else:
            if not txs or orphan_span is None:
                continue
            start, end = orphan_span
            opening = float(txs[0].running_balance - txs[0].credit + txs[0].debit)
            label = txs[0].source_account_number or txs[0].statement_id
            sids = sorted({t.statement_id for t in txs})
            segments = [(start, end, opening)]
            acct_type = ""
        account = _Account(key, label, sids, start, end, opening, txs, segments=segments, account_type=acct_type)
        _fill_eod(account)
        accounts.append(account)
    return accounts


def _statement_segments(
    sts: Sequence[BankAccountStatement], txs: Sequence[NormalizedTransaction], first_opening: float
) -> List[Tuple[dt.date, dt.date, Optional[float]]]:
    """Merge each statement's period (widened by its own transaction dates) into covered segments.

    Overlapping or adjacent statements form one segment whose balance carries forward; a later segment
    after a gap restarts from its own opening (implied by its first transaction, else the statement's
    opening balance), and the gap days are not covered (EDGE-22)."""
    tx_by_stmt: Dict[str, List[NormalizedTransaction]] = defaultdict(list)
    for t in txs:
        tx_by_stmt[t.statement_id].append(t)
    raw: List[Tuple[dt.date, dt.date, BankAccountStatement]] = []
    for s in sts:
        dates = [d for d in (_d(t.txn_date) for t in tx_by_stmt.get(s.statement_id, [])) if d]
        start = _d(s.period_start) or (min(dates) if dates else None)
        end = _d(s.period_end) or (max(dates) if dates else None)
        if dates:
            start = min([start, *dates]) if start else min(dates)
            end = max([end, *dates]) if end else max(dates)
        if start is None or end is None or end < start:
            continue
        raw.append((start, end, s))
    raw.sort(key=lambda r: (r[0], r[1]))
    segments: List[Tuple[dt.date, dt.date, Optional[float]]] = []
    for start, end, s in raw:
        if segments and start <= segments[-1][1] + dt.timedelta(days=1):
            ps, pe, po = segments[-1]
            segments[-1] = (ps, max(pe, end), po)
            continue
        if not segments:
            opening: Optional[float] = first_opening
        else:
            first_tx = next((t for t in txs if (_d(t.txn_date) or start) >= start), None)
            if first_tx is not None and (_d(first_tx.txn_date) or end) <= end:
                opening = float(first_tx.running_balance - first_tx.credit + first_tx.debit)
            else:
                opening = float(s.opening_balance or 0)
        segments.append((start, end, opening))
    return segments


def _row_direction(txs: Sequence[NormalizedTransaction]) -> Dict[str, int]:
    """+1 when a statement lists rows oldest-first, -1 when newest-first (so same-day EOD picks the last posting)."""
    by_stmt: Dict[str, List[NormalizedTransaction]] = defaultdict(list)
    for t in txs:
        by_stmt[t.statement_id].append(t)
    out: Dict[str, int] = {}
    for sid, rows in by_stmt.items():
        rows = sorted(rows, key=lambda t: t.row_index)
        out[sid] = -1 if len(rows) > 1 and rows[0].txn_date > rows[-1].txn_date else 1
    return out


def _date_range(start: dt.date, end: dt.date):
    cur = start
    one = dt.timedelta(days=1)
    while cur <= end:
        yield cur
        cur += one


def _fill_eod(acct: _Account) -> None:
    last_bal_by_day: Dict[dt.date, float] = {}
    for tx in acct.txns:
        d = _d(tx.txn_date)
        if d is not None:
            last_bal_by_day[d] = float(tx.running_balance)
    # Carry the balance forward over days without transactions inside each covered segment; days in a
    # gap between statements have no balance and are excluded from ABB (EDGE-22).
    segments = acct.segments or [(acct.start, acct.end, acct.opening)]
    bal = acct.opening
    for seg_start, seg_end, seg_opening in segments:
        if seg_opening is not None:
            bal = seg_opening
        for day in _date_range(seg_start, seg_end):
            if day in last_bal_by_day:
                bal = last_bal_by_day[day]
            acct.eod[day] = bal


def _abb_stats(series: Dict[dt.date, float], months: List[str]) -> Dict[str, Dict[str, Any]]:
    by_month: Dict[str, List[Tuple[dt.date, float]]] = defaultdict(list)
    for day, bal in series.items():
        by_month[f"{day.year:04d}-{day.month:02d}"].append((day, bal))
    out: Dict[str, Dict[str, Any]] = {}
    for ym in months:
        pts = sorted(by_month.get(ym, []))
        if not pts:
            continue
        vals = [b for _, b in pts]
        last_dom = calendar.monthrange(pts[0][0].year, pts[0][0].month)[1]
        fixed_days = set(FIXED_ABB_DAYS) | {last_dom}
        fixed = [b for d, b in pts if d.day in fixed_days]
        out[ym] = {
            "daily_abb": sum(vals) / len(vals),
            "fixed_date_abb": (sum(fixed) / len(fixed)) if fixed else sum(vals) / len(vals),
            "min_balance": min(vals),
            "max_balance": max(vals),
            "days_covered": len(vals),
        }
    return out


# --- EMI detection --------------------------------------------------------------------------

def _is_mandate(tokens: List[str], narr_u: str) -> bool:
    return bool(_MANDATE_TOKENS.intersection(tokens)) or any(p in narr_u for p in _MANDATE_PHRASES)


def _lender_key(tx: NormalizedTransaction, tokens: List[str]) -> str:
    cp = (tx.extracted_counterparty or "").strip().upper()
    if cp:
        words = [w for w in _TOKEN_RE.findall(cp) if not any(c.isdigit() for c in w)]
        if words:
            return " ".join(words)
    words = [w for w in tokens if w not in _LENDER_STOPWORDS and not any(c.isdigit() for c in w) and len(w) > 1]
    return " ".join(words[:5]) or "UNIDENTIFIED LENDER"


def _cluster_amounts(items: List[Tuple[float, NormalizedTransaction, dt.date]]) -> List[List[Tuple[float, NormalizedTransaction, dt.date]]]:
    clusters: List[List[Tuple[float, NormalizedTransaction, dt.date]]] = []
    for item in sorted(items, key=lambda x: x[0]):
        if clusters:
            ref = statistics.median(a for a, _, _ in clusters[-1])
            if abs(item[0] - ref) <= EMI_AMOUNT_BAND * ref:
                clusters[-1].append(item)
                continue
        clusters.append([item])
    return clusters


def _day_distance(a: int, b: int) -> int:
    diff = abs(a - b)
    return min(diff, 31 - diff)  # month-end wrap (e.g. 30th vs 1st)


def _detect_emis(
    active: Sequence[NormalizedTransaction], account_key_by_stmt: Dict[str, str], period_end: dt.date
) -> Tuple[List[Dict[str, Any]], Set[str]]:
    groups: Dict[Tuple[str, str], List[Tuple[float, NormalizedTransaction, dt.date]]] = defaultdict(list)
    for tx in active:
        if tx.direction != TxDirection.DEBIT or float(tx.debit) <= 0 or tx.category_id in _NON_EMI_CATEGORIES:
            continue
        narr_u = (tx.raw_narration or "").upper()
        tokens = _tokens(narr_u)
        if _NON_EMI_TOKENS.intersection(tokens) or _is_bounce_candidate(tokens, narr_u):
            continue
        if not (_is_mandate(tokens, narr_u) or tx.category_id in _EMI_CATEGORIES):
            continue
        d = _d(tx.txn_date)
        if d is None:
            continue
        acct = account_key_by_stmt.get(tx.statement_id, tx.statement_id)
        groups[(acct, _lender_key(tx, tokens))].append((float(tx.debit), tx, d))

    obligations: List[Dict[str, Any]] = []
    emi_tx_ids: Set[str] = set()
    for (acct, lender), items in groups.items():
        for cluster in _cluster_amounts(items):
            usual_day = int(statistics.median(d.day for _, _, d in cluster))
            on_day = [c for c in cluster if _day_distance(c[2].day, usual_day) <= EMI_DAY_TOLERANCE]
            by_month: Dict[int, Tuple[float, NormalizedTransaction, dt.date]] = {}
            for c in sorted(on_day, key=lambda c: c[2]):
                by_month.setdefault(_month_index(c[2]), c)  # first on-day debit per month
            months = sorted(by_month)
            best_run: List[int] = []
            run: List[int] = []
            for m in months:
                run = run + [m] if run and m == run[-1] + 1 else [m]
                if len(run) >= len(best_run):
                    best_run = run
            if len(best_run) < EMI_MIN_CONSECUTIVE:
                # A loan that started in the last statement month (or any EMI in a short statement) has one
                # occurrence: count it as a provisional, unconfirmed obligation instead of dropping it.
                latest_amt, latest_tx, latest_d = by_month[months[-1]] if months else max(cluster, key=lambda c: c[2])
                one_off = bool(_ONE_OFF_REPAYMENT_TOKENS & set(_tokens(latest_tx.raw_narration or "")))
                if not one_off and _month_index(period_end) - _month_index(latest_d) <= 1:
                    emi_tx_ids.add(latest_tx.tx_id)
                    obligations.append({
                        "lender": lender,
                        "account": acct,
                        "monthly_emi": round(latest_amt, 2),
                        "latest_amount": round(latest_amt, 2),
                        "usual_debit_day": latest_d.day,
                        "months_observed": len(months) or 1,
                        "consecutive_months": len(best_run) or 1,
                        "first_debit": latest_d.isoformat(),
                        "last_debit": latest_d.isoformat(),
                        "active": True,
                        "confirmed": False,
                    })
                continue
            members = [by_month[m] for m in months if best_run[0] <= m]
            amounts = [a for a, _, _ in members]
            last = members[-1][2]
            active_flag = _month_index(period_end) - _month_index(last) <= 1
            emi_tx_ids.update(t.tx_id for _, t, _ in members)
            obligations.append({
                "lender": lender,
                "account": acct,
                "monthly_emi": round(statistics.median(amounts), 2),
                "latest_amount": round(amounts[-1], 2),
                "usual_debit_day": usual_day,
                "months_observed": len(members),
                "consecutive_months": len(best_run),
                "first_debit": members[0][2].isoformat(),
                "last_debit": last.isoformat(),
                "active": active_flag,
                "confirmed": True,
            })
    obligations.sort(key=lambda o: (-o["monthly_emi"], o["lender"]))
    return obligations, emi_tx_ids


# --- Bounce detection -----------------------------------------------------------------------

def _is_excluded(narr_u: str) -> bool:
    return any(p in narr_u for p in _BOUNCE_EXCLUDE_PHRASES)


def _is_charge_reversal(tx: NormalizedTransaction, tset: Set[str]) -> bool:
    """A credit that reverses / refunds a bounce charge ("REV NACH RTN CHGS") is not a new bounce (EDGE-24)."""
    return tx.direction == TxDirection.CREDIT and bool(_REVERSAL_TOKENS & tset) and bool(
        _CHARGE_TOKENS & tset or tx.category_id == 28
    )


def _is_bounce_candidate(tokens: List[str], narr_u: str) -> bool:
    if _is_excluded(narr_u):
        return False
    tset = set(tokens)
    has_return = bool(_RETURN_TOKENS & tset) or "INSUFF" in narr_u or "FUNDS INSUFFICIENT" in narr_u
    return has_return and bool(_INSTRUMENT_TOKENS & tset)


def _instrument_family(tokens: Set[str]) -> str:
    if tokens & {"CHQ", "CHEQUE", "CHK", "CLG", "CLEARING"}:
        return "CHEQUE"
    if tokens & {"NACH", "ENACH", "ACH", "ECS", "MANDATE"}:
        return "MANDATE"
    if "SI" in tokens:
        return "SI"
    return "OTHER"


def _detect_bounces(
    active: Sequence[NormalizedTransaction], account_key_by_stmt: Dict[str, str]
) -> Tuple[List[Dict[str, Any]], int, int]:
    """Return (bounce_events, attempted_debit_instruments, attempted_credit_instruments)."""
    returns: List[Dict[str, Any]] = []
    charges: List[Dict[str, Any]] = []
    instr_debits: List[Tuple[str, float, dt.date]] = []
    instr_credits = 0
    for tx in active:
        narr_u = (tx.raw_narration or "").upper()
        tokens = _tokens(narr_u)
        tset = set(tokens)
        d = _d(tx.txn_date)
        if d is None:
            continue
        acct = account_key_by_stmt.get(tx.statement_id, tx.statement_id)
        if _is_charge_reversal(tx, tset):
            continue
        is_candidate = _is_bounce_candidate(tokens, narr_u)
        is_charge_row = tx.category_id == 28 or (is_candidate and bool(_CHARGE_TOKENS & tset)
                                                 and tx.direction == TxDirection.DEBIT)
        if is_charge_row and not _is_excluded(narr_u):
            charges.append({"tx": tx, "date": d, "account": acct, "tokens": tset})
            continue
        if is_candidate:
            returns.append({"tx": tx, "date": d, "account": acct, "tokens": tset})
            continue
        if _INSTRUMENT_TOKENS & tset:
            if tx.direction == TxDirection.DEBIT and float(tx.debit) > 0:
                instr_debits.append((acct, float(tx.debit), d))
            elif tx.direction == TxDirection.CREDIT and float(tx.credit) > 0:
                instr_credits += 1

    def _side(entry: Dict[str, Any], is_charge: bool) -> str:
        toks = entry["tokens"]
        if toks & _OUTWARD_TOKENS:
            return "OUTWARD"
        if toks & _INWARD_TOKENS:
            return "INWARD"
        if is_charge:
            return "INWARD"
        # A credit reverses a debit we issued (I/W return); a debit reverses a credit we deposited (O/W).
        return "INWARD" if entry["tx"].direction == TxDirection.CREDIT else "OUTWARD"

    events: List[Dict[str, Any]] = []
    for r in returns:
        r["side"] = _side(r, False)
        r["family"] = _instrument_family(r["tokens"])
        events.append(r)

    # Several charge rows can belong to one return: the charge and the GST on it are usually separate rows
    # (EDGE-24). Base charges are attached first, at most one per return (a second base charge is a separate,
    # charge-only bounce); tax-on-charge rows then attach to any return, preferring one that already has its
    # base charge. A tax row that pairs with nothing is absorbed by any bounce event of the account nearby.
    for c in charges:
        c["is_tax"] = bool(_TAX_ON_CHARGE_TOKENS & c["tokens"])
    for c in sorted(charges, key=lambda c: (c["is_tax"], c["date"], c["tx"].row_index)):
        c_side = _side(c, True) if (c["tokens"] & (_OUTWARD_TOKENS | _INWARD_TOKENS)) else None
        c_family = _instrument_family(c["tokens"])
        best_i, best_key = None, None
        for i, ev in enumerate(events):
            if ev.get("is_charge") or ev["account"] != c["account"]:
                continue
            gap = (c["date"] - ev["date"]).days
            if gap < -1 or gap > BOUNCE_PAIR_WINDOW_DAYS:
                continue
            if c_side and ev["side"] != c_side:
                continue
            if c_family != "OTHER" and ev["family"] != "OTHER" and c_family != ev["family"]:
                continue
            has_base = bool(ev.get("has_base_charge"))
            if not c["is_tax"] and has_base:
                continue
            key = (0 if (has_base or not c["is_tax"]) else 1, abs(gap))
            if best_key is None or key < best_key:
                best_i, best_key = i, key
        if best_i is not None:
            ev = events[best_i]
            ev.setdefault("charge_txs", []).append(c["tx"])
            ev.setdefault("charge_tx", c["tx"])
            if not c["is_tax"]:
                ev["has_base_charge"] = True
            continue
        if c["is_tax"] and any(
            ev["account"] == c["account"] and abs((c["date"] - ev["date"]).days) <= BOUNCE_PAIR_WINDOW_DAYS
            for ev in events
        ):
            continue
        c["side"] = c_side or "INWARD"
        c["family"] = c_family
        c["is_charge"] = True
        events.append(c)

    # Attempted debit instruments: posted instrument debits plus inward returns whose original
    # debit is not on the statement (bank only posted the return/charge).
    unmatched_inward = 0
    used: Set[int] = set()
    for ev in events:
        if ev["side"] != "INWARD":
            continue
        amt = float(ev["tx"].credit) if not ev.get("is_charge") else 0.0
        hit = None
        if amt > 0:
            for i, (acct, a, d) in enumerate(instr_debits):
                if i in used or acct != ev["account"] or abs(a - amt) > 0.005:
                    continue
                if 0 <= (ev["date"] - d).days <= BOUNCE_PAIR_WINDOW_DAYS:
                    hit = i
                    break
        if hit is None:
            unmatched_inward += 1
        else:
            used.add(hit)
    attempted_debits = len(instr_debits) + unmatched_inward

    out = [
        {
            "date": ev["tx"].txn_date,
            "narration": ev["tx"].raw_narration,
            "direction": ev["side"],
            "instrument": ev["family"],
            "amount": float(ev["tx"].credit or ev["tx"].debit),
            "charge_narration": ev["charge_tx"].raw_narration if ev.get("charge_tx") is not None else None,
            "charge_narrations": [t.raw_narration for t in ev.get("charge_txs") or []],
            "derived_from_charge_only": bool(ev.get("is_charge")),
        }
        for ev in sorted(events, key=lambda e: (e["date"], e["tx"].row_index))
    ]
    outward_count = sum(1 for e in out if e["direction"] == "OUTWARD")
    return out, attempted_debits, instr_credits + outward_count


# --- Penal / overdraft events ---------------------------------------------------------------

def _is_od_account(account_type: str) -> bool:
    kind = " ".join(re.sub(r"[^a-z]+", " ", (account_type or "").lower()).split())
    return "cash credit" in kind or any(tok in _OD_ACCOUNT_TOKENS for tok in kind.split())


def _penal_type(tx: NormalizedTransaction) -> Optional[str]:
    """MIN_BALANCE_PENALTY / TOD_OVERDRAWN / PENAL_INTEREST for a debit row, else None (EDGE-25).
    Bounce returns and their charges are counted under bounces, not here."""
    if tx.direction != TxDirection.DEBIT or float(tx.debit) <= 0 or tx.category_id == 28:
        return None
    narr_u = (tx.raw_narration or "").upper()
    tokens = _tokens(narr_u)
    if _is_bounce_candidate(tokens, narr_u):
        return None
    norm = " ".join(tokens)
    if _MIN_BAL_RE.search(norm):
        return "MIN_BALANCE_PENALTY"
    if _TOD_RE.search(narr_u) or _TOD_RE.search(norm):
        return "TOD_OVERDRAWN"
    if _PENAL_RE.search(norm):
        return "PENAL_INTEREST"
    return None


# --- Parameters -----------------------------------------------------------------------------

def _validate_params(
    foir_pct: float, annual_interest_rate: float, tenure_months: int, business_income_factor: float,
    abb_income_factor: float, monthly_income_override: Optional[float],
) -> None:
    """Raise ValueError for out-of-range inputs (EDGE-27); ranges match the HTTP API's checks."""
    def _num(name: str, v: Any) -> float:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number") from None
        if not math.isfinite(f):
            raise ValueError(f"{name} must be a finite number")
        return f

    if not 0 < _num("foir_pct", foir_pct) <= 100:
        raise ValueError("foir_pct must be a percentage greater than 0 and at most 100")
    if not 0 <= _num("annual_interest_rate", annual_interest_rate) <= 50:
        raise ValueError("annual_interest_rate must be an annual percentage between 0 and 50")
    t = _num("tenure_months", tenure_months)
    if t != int(t) or not 1 <= t <= 480:
        raise ValueError("tenure_months must be a whole number of months between 1 and 480")
    for name, v in (("business_income_factor", business_income_factor), ("abb_income_factor", abb_income_factor)):
        if not 0 <= _num(name, v) <= 1:
            raise ValueError(f"{name} must be between 0 and 1")
    if monthly_income_override is not None and _num("monthly_income_override", monthly_income_override) < 0:
        raise ValueError("monthly_income_override must be zero or positive")


# --- Report ---------------------------------------------------------------------------------

def _empty_report(foir_pct: float, annual_interest_rate: float, tenure_months: int) -> Dict[str, Any]:
    return {
        "months_analyzed": 0,
        "months_with_balance_data": 0,
        "monthly_breakdown": [],
        "per_account_abb": [],
        "consolidated_coverage": {"basis": _CONSOLIDATION_BASIS, "accounts": [], "segments": [],
                                  "full_coverage_days": 0, "partial_coverage_days": 0, "uncovered_days": 0},
        "daily_abb": 0.0,
        "fixed_date_abb": 0.0,
        "avg_monthly_inflow": 0.0,
        "avg_monthly_net_business_inflow": 0.0,
        "assessable_monthly_income": 0.0,
        "income_basis": {"method": "none"},
        "monthly_existing_emi": 0.0,
        "emi_obligations": [],
        "detected_emi_transactions": [],
        "inward_bounce_count": 0,
        "outward_bounce_count": 0,
        "bounce_events": [],
        "attempted_debit_instruments": 0,
        "bounce_rate_pct": 0.0,
        "outward_bounce_rate_pct": 0.0,
        "cash_deposit_pct": 0.0,
        "cash_withdrawal_pct": 0.0,
        "penal_events": [],
        "penal_event_count": 0,
        "penal_event_breakdown": {},
        "overdrawn_days": 0,
        "overdrawn_days_by_account": {},
        "foir_pct": foir_pct,
        "annual_interest_rate": annual_interest_rate,
        "tenure_months": tenure_months,
        "max_eligible_emi": 0.0,
        "max_eligible_loan": 0.0,
        "credit_grade": "INSUFFICIENT_DATA",
    }


_CONSOLIDATION_BASIS = (
    "Consolidated EOD balance for a day is the sum of the accounts whose statement period covers that day; "
    "days in no statement period are excluded. Monthly ABB averages covered days only; see Accounts covered and "
    "Full-coverage days in the monthly table, and the coverage segments table for which accounts each date range "
    "includes."
)


def _coverage_segments(accounts: Sequence[_Account]) -> List[Dict[str, Any]]:
    by_day: Dict[dt.date, Tuple[str, ...]] = {}
    for a in accounts:
        for day in a.eod:
            by_day[day] = by_day.get(day, ()) + (a.label,)
    segs: List[Dict[str, Any]] = []
    for day in sorted(by_day):
        accts = sorted(set(by_day[day]))
        if segs and segs[-1]["accounts"] == accts and dt.date.fromisoformat(segs[-1]["to"]) + dt.timedelta(days=1) == day:
            segs[-1]["to"] = day.isoformat()
            segs[-1]["days"] += 1
        else:
            segs.append({"from": day.isoformat(), "to": day.isoformat(), "days": 1, "accounts": accts})
    return segs


def compute_loan_eligibility_report(
    transactions: List[NormalizedTransaction],
    foir_pct: float = 55.0,
    annual_interest_rate: float = 11.5,
    tenure_months: int = 60,
    *,
    statements: Optional[List[BankAccountStatement]] = None,
    business_income_factor: float = DEFAULT_BUSINESS_INCOME_FACTOR,
    abb_income_factor: float = DEFAULT_ABB_INCOME_FACTOR,
    monthly_income_override: Optional[float] = None,
) -> Dict[str, Any]:
    """
    Evaluate borrower banking strength: per-account and consolidated Daily & Fixed-Date ABB
    (5/10/15/20/25/EOM) over each statement's period, recurring EMI obligations, inward/outward
    cheque/NACH bounces, overdraft / penal events, cash ratios and maximum loan eligibility.

    Pass `statements` so ABB covers each statement's full period and opening balance; without it
    each account spans the case's transaction date range with the opening balance implied by
    its first transaction. Days between two statements of an account are not covered.

    Income: a salary average when salary credits exist; otherwise the banking surrogate
    max(avg net business credits x `business_income_factor`, fixed-date ABB x `abb_income_factor`);
    `monthly_income_override` replaces both. Raises ValueError for out-of-range parameters.
    """
    _validate_params(foir_pct, annual_interest_rate, tenure_months, business_income_factor, abb_income_factor,
                     monthly_income_override)
    foir_pct, annual_interest_rate, tenure_months = float(foir_pct), float(annual_interest_rate), int(tenure_months)
    active = [t for t in transactions if not t.is_duplicate_ignored and _d(t.txn_date) is not None]
    if not active:
        return _empty_report(foir_pct, annual_interest_rate, tenure_months)

    accounts = _build_accounts(active, statements or [])
    account_key_by_stmt = {sid: a.key for a in accounts for sid in a.statement_ids}
    period_start = min(a.start for a in accounts)
    period_end = max(a.end for a in accounts)
    months = _iter_months(period_start, period_end)
    all_labels = sorted({a.label for a in accounts})

    # Consolidated EOD = sum of every account covering that day.
    consolidated: Dict[dt.date, float] = defaultdict(float)
    covering: Dict[dt.date, Set[str]] = defaultdict(set)
    for a in accounts:
        for day, bal in a.eod.items():
            consolidated[day] += bal
            covering[day].add(a.label)
    cons_stats = _abb_stats(consolidated, months)
    month_accounts: Dict[str, Set[str]] = defaultdict(set)
    month_full_days: Dict[str, int] = defaultdict(int)
    for day, labels in covering.items():
        ym = f"{day.year:04d}-{day.month:02d}"
        month_accounts[ym] |= labels
        if len(labels) == len(all_labels):
            month_full_days[ym] += 1

    overdrawn_by_account: Dict[str, int] = {}
    per_account: List[Dict[str, Any]] = []
    for a in accounts:
        a_stats = _abb_stats(a.eod, months)
        n = max(1, len(a_stats))
        gaps = a.coverage_gaps()
        is_od = _is_od_account(a.account_type)
        overdrawn = 0 if is_od else sum(1 for b in a.eod.values() if b < 0)
        if overdrawn:
            overdrawn_by_account[a.label] = overdrawn
        per_account.append({
            "account": a.label,
            "account_key": a.key,
            "account_type": a.account_type or None,
            "statement_ids": a.statement_ids,
            "period_start": a.start.isoformat(),
            "period_end": a.end.isoformat(),
            "opening_balance": round(a.opening, 2),
            "days_covered": len(a.eod),
            "gap_days": sum(g["days"] for g in gaps),
            "coverage_gaps": gaps,
            "overdrawn_days": overdrawn,
            "daily_abb": round(sum(m["daily_abb"] for m in a_stats.values()) / n, 2),
            "fixed_date_abb": round(sum(m["fixed_date_abb"] for m in a_stats.values()) / n, 2),
            "monthly": [
                {"month": ym, "daily_abb": round(v["daily_abb"], 2), "fixed_date_abb": round(v["fixed_date_abb"], 2),
                 "min_balance": round(v["min_balance"], 2), "max_balance": round(v["max_balance"], 2),
                 "days_covered": v["days_covered"]}
                for ym, v in sorted(a_stats.items())
            ],
        })

    emi_obligations, emi_tx_ids = _detect_emis(active, account_key_by_stmt, period_end)
    bounce_events, attempted_debits, attempted_credits = _detect_bounces(active, account_key_by_stmt)
    inward_bounces = sum(1 for e in bounce_events if e["direction"] == "INWARD")
    outward_bounces = sum(1 for e in bounce_events if e["direction"] == "OUTWARD")

    label_by_key = {a.key: a.label for a in accounts}
    penal_events: List[Dict[str, Any]] = []
    for tx in sorted(active, key=lambda t: (t.txn_date, t.row_index)):
        kind = _penal_type(tx)
        if kind:
            penal_events.append({
                "date": tx.txn_date,
                "type": kind,
                "amount": round(float(tx.debit), 2),
                "narration": tx.raw_narration,
                "account": label_by_key.get(account_key_by_stmt.get(tx.statement_id, ""), tx.statement_id),
            })
    penal_breakdown: Dict[str, int] = defaultdict(int)
    penal_by_month: Dict[str, int] = defaultdict(int)
    for e in penal_events:
        penal_breakdown[e["type"]] += 1
        penal_by_month[e["date"][:7]] += 1

    by_month: Dict[str, List[NormalizedTransaction]] = defaultdict(list)
    for tx in active:
        by_month[tx.txn_date[:7]].append(tx)

    monthly_breakdown: List[Dict[str, Any]] = []
    for ym in months:
        mtx = by_month.get(ym, [])
        m_cr = sum(float(t.credit) for t in mtx)
        m_dr = sum(float(t.debit) for t in mtx)
        m_net_biz_cr = sum(float(t.credit) for t in mtx if t.category_id not in (8, 9, 49, 50, 34))
        m_emi = sum(float(t.debit) for t in mtx if t.tx_id in emi_tx_ids)
        m_cash_dep = sum(float(t.credit) for t in mtx if t.category_id == 50)
        m_cash_wdl = sum(float(t.debit) for t in mtx if t.category_id == 51)
        st = cons_stats.get(ym)
        monthly_breakdown.append({
            "month": ym,
            "total_credits": round(m_cr, 2),
            "net_business_credits": round(m_net_biz_cr, 2),
            "total_debits": round(m_dr, 2),
            "net_cash_flow": round(m_cr - m_dr, 2),
            "net_surplus": round(m_cr - m_dr, 2),
            "cash_deposits": round(m_cash_dep, 2),
            "cash_withdrawals": round(m_cash_wdl, 2),
            "cash_deposit_pct": round(m_cash_dep / m_cr * 100.0, 2) if m_cr else 0.0,
            "cash_withdrawal_pct": round(m_cash_wdl / m_dr * 100.0, 2) if m_dr else 0.0,
            "emi_outflow": round(m_emi, 2),
            "penal_events": penal_by_month.get(ym, 0),
            "daily_abb": round(st["daily_abb"], 2) if st else 0.0,
            "fixed_date_abb": round(st["fixed_date_abb"], 2) if st else 0.0,
            "min_balance": round(st["min_balance"], 2) if st else 0.0,
            "max_balance": round(st["max_balance"], 2) if st else 0.0,
            "days_covered": st["days_covered"] if st else 0,
            "accounts_covered": sorted(month_accounts.get(ym, set())),
            "full_coverage_days": month_full_days.get(ym, 0),
            "tx_count": len(mtx),
        })

    n_months = max(1, len(monthly_breakdown))
    abb_months = [m for m in monthly_breakdown if m["days_covered"]]
    n_abb = max(1, len(abb_months))
    overall_daily_abb = sum(m["daily_abb"] for m in abb_months) / n_abb
    overall_fixed_abb = sum(m["fixed_date_abb"] for m in abb_months) / n_abb
    avg_inflow = sum(m["total_credits"] for m in monthly_breakdown) / n_months
    avg_biz_inflow = sum(m["net_business_credits"] for m in monthly_breakdown) / n_months
    monthly_existing_emi = sum(o["monthly_emi"] for o in emi_obligations if o["active"])

    salary_months = sum(float(t.credit) for t in active if t.category_id == 1) / n_months
    business_proxy = avg_biz_inflow * float(business_income_factor)
    abb_proxy = overall_fixed_abb * float(abb_income_factor)
    if monthly_income_override is not None:
        method = "override"
        assessable_monthly_income = float(monthly_income_override)
    elif salary_months > 0:
        method = "salary"
        assessable_monthly_income = salary_months
    else:
        method = "banking_surrogate"
        assessable_monthly_income = max(business_proxy, abb_proxy)
    income_basis = {
        "method": method,
        "salary_monthly_avg": round(salary_months, 2),
        "avg_monthly_net_business_inflow": round(avg_biz_inflow, 2),
        "business_income_factor": float(business_income_factor),
        "business_income_proxy": round(business_proxy, 2),
        "fixed_date_abb": round(overall_fixed_abb, 2),
        "abb_income_factor": float(abb_income_factor),
        "abb_income_proxy": round(abb_proxy, 2),
        "monthly_income_override": None if monthly_income_override is None else float(monthly_income_override),
        "note": "Indicative banking surrogate, not verified income; lenders apply their own programme norms.",
    }

    max_eligible_emi = max(0.0, (assessable_monthly_income * (foir_pct / 100.0)) - monthly_existing_emi)
    monthly_rate = (annual_interest_rate / 12.0) / 100.0
    if monthly_rate > 0 and tenure_months > 0:
        factor = ((1 + monthly_rate) ** tenure_months - 1) / (monthly_rate * ((1 + monthly_rate) ** tenure_months))
        max_eligible_loan = max_eligible_emi * factor
    else:
        max_eligible_loan = max_eligible_emi * tenure_months

    total_credits = sum(float(t.credit) for t in active)
    total_debits = sum(float(t.debit) for t in active)
    cash_dep_total = sum(float(t.credit) for t in active if t.category_id == 50)
    cash_wdl_total = sum(float(t.debit) for t in active if t.category_id == 51)
    bounce_rate = (inward_bounces / attempted_debits * 100.0) if attempted_debits else 0.0
    outward_rate = (outward_bounces / attempted_credits * 100.0) if attempted_credits else 0.0
    cash_dep_pct = (cash_dep_total / max(1.0, total_credits)) * 100.0
    cash_wdl_pct = (cash_wdl_total / max(1.0, total_debits)) * 100.0

    if inward_bounces == 0 and overall_fixed_abb >= 25_000 and max_eligible_loan > 0:
        grade = "PRIME (A)"
    elif inward_bounces <= 2 and overall_fixed_abb >= 10_000:
        grade = "STANDARD (B)"
    else:
        grade = "HIGH RISK / REFER (C)"

    segments = _coverage_segments(accounts)
    full_days = sum(s["days"] for s in segments if len(s["accounts"]) == len(all_labels))
    covered_days = sum(s["days"] for s in segments)
    span_days = (period_end - period_start).days + 1

    return {
        "months_analyzed": n_months,
        "months_with_balance_data": len(abb_months),
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "monthly_breakdown": monthly_breakdown,
        "per_account_abb": per_account,
        "consolidated_coverage": {
            "basis": _CONSOLIDATION_BASIS,
            "accounts": all_labels,
            "segments": segments,
            "full_coverage_days": full_days,
            "partial_coverage_days": covered_days - full_days,
            "uncovered_days": span_days - covered_days,
        },
        "daily_abb": round(overall_daily_abb, 2),
        "fixed_date_abb": round(overall_fixed_abb, 2),
        "avg_monthly_inflow": round(avg_inflow, 2),
        "avg_monthly_net_business_inflow": round(avg_biz_inflow, 2),
        "assessable_monthly_income": round(assessable_monthly_income, 2),
        "income_basis": income_basis,
        "monthly_existing_emi": round(monthly_existing_emi, 2),
        "emi_obligations": emi_obligations,
        "detected_emi_transactions": [
            {
                "date": t.txn_date,
                "narration": t.raw_narration,
                "amount": float(t.debit),
                "category": t.category_name,
            }
            for t in active if t.tx_id in emi_tx_ids
        ][:25],
        "inward_bounce_count": inward_bounces,
        "outward_bounce_count": outward_bounces,
        "bounce_events": bounce_events,
        "attempted_debit_instruments": attempted_debits,
        "bounce_rate_pct": round(bounce_rate, 2),
        "outward_bounce_rate_pct": round(outward_rate, 2),
        "cash_deposit_pct": round(cash_dep_pct, 2),
        "cash_withdrawal_pct": round(cash_wdl_pct, 2),
        "penal_events": penal_events,
        "penal_event_count": len(penal_events),
        "penal_event_breakdown": dict(penal_breakdown),
        "overdrawn_days": sum(overdrawn_by_account.values()),
        "overdrawn_days_by_account": overdrawn_by_account,
        "foir_pct": foir_pct,
        "annual_interest_rate": annual_interest_rate,
        "tenure_months": tenure_months,
        "max_eligible_emi": round(max_eligible_emi, 2),
        "max_eligible_loan": round(max_eligible_loan, 2),
        "credit_grade": grade,
    }
