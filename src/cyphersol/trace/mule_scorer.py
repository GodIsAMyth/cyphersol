"""CypherTrace 10-Parameter Behavioral Mule Account Detection & Risk Scoring Engine (SRS FR-TRACE-05).

Every account is scored once across all of its uploaded statements (TF-23). Each score carries a full
explainability vector: for every parameter the raw metric(s), the normalised score S_k, the weight w_k,
the weighted contribution 100 * w_k * S_k, and the tx_ids of the supporting evidence (TF-15).
"""

from __future__ import annotations

import datetime as dt
import math
import re
import statistics
from collections import Counter, defaultdict, deque
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cyphersol.models import (
    BankAccountStatement,
    MuleParameterBreakdown,
    MuleRiskAssessment,
    MuleRiskTier,
    NormalizedTransaction,
    ResolvedEntity,
    TxDirection,
)
from cyphersol.nlp.entity_resolver import normalize_entity_name
from cyphersol.taxonomy import CATEGORY_BY_ID

PARAMETER_WEIGHTS: Dict[str, float] = {
    "flow_through_ratio": 0.25,
    "velocity_hold_time": 0.20,
    "lifestyle_spending_absence": 0.15,
    "counterparty_concentration": 0.10,
    "fan_in_fan_out_pattern": 0.10,
    "cash_crypto_wallet_leakage": 0.05,
    "structuring_smurfing": 0.05,
    "zero_balance_oscillation": 0.05,
    "burst_dormancy_pattern": 0.03,
    "round_number_anomalies": 0.02,
}

PARAMETER_LABELS: Dict[str, str] = {
    "flow_through_ratio": "Flow-Through Ratio",
    "velocity_hold_time": "Velocity of Funds (Hold Time)",
    "lifestyle_spending_absence": "Lifestyle Spending Absence",
    "counterparty_concentration": "Counterparty Concentration (HHI)",
    "fan_in_fan_out_pattern": "Fan-In / Fan-Out Topology",
    "cash_crypto_wallet_leakage": "Cash / Crypto / Wallet Leakage",
    "structuring_smurfing": "Structuring / Smurfing",
    "zero_balance_oscillation": "Zero Balance Oscillation",
    "burst_dormancy_pattern": "Burst / Dormancy Pattern",
    "round_number_anomalies": "Round Number Anomalies",
}

PARAMETER_FORMULAS: Dict[str, str] = {
    "flow_through_ratio": "S1 = 1 - |sum CR - sum DR| / max(sum CR, sum DR); 0 when max turnover < min turnover gate",
    "velocity_hold_time": "S2 = exp(-median_hold_hours / 12), FIFO amount-weighted median hold",
    "lifestyle_spending_absence": "S3 = 1 - min(1, lifestyle_txns_per_active_month / 6)",
    "counterparty_concentration": "S4 = sum p_i^2 over counterparty volume shares",
    "fan_in_fan_out_pattern": "S5 = max(min(1,din/10)*(1-min(0.8,dout/(din+1))), min(1,dout/10)*(1-min(0.8,din/(dout+1)))) over 24h windows",
    "cash_crypto_wallet_leakage": "S6 = min(1, cash/AEPS/crypto/wallet debits / total debits)",
    "structuring_smurfing": "S7 = min(1, 3 * near_threshold_txns / total_txns)",
    "zero_balance_oscillation": "S8 = drained large-inflow cycles / large-inflow cycles (floor = max(500, 1% peak credit))",
    "burst_dormancy_pattern": "S9 = 1 - exp(-peak_30d_turnover / (max(median_prior_monthly_turnover, 10000) * 5)) when a >= 30-day dormancy precedes the peak",
    "round_number_anomalies": "S10 = electronic (UPI/NEFT/RTGS/IMPS) txns with amount mod 5000 == 0 / electronic txns",
}

MIN_TURNOVER_GATE = 50_000.0          # S1 minimum turnover (SRS default)
HOLD_HALF_LIFE_HOURS = 12.0           # S2 H0
LIFESTYLE_EXPECTED_PER_MONTH = 6.0    # S3 L_expected
FAN_DEGREE_SATURATION = 10.0          # S5 divisor
LARGE_INFLOW = 10_000.0               # S8 inflow threshold
S8_CYCLE_MAX_DAYS = 30                # a drain must follow its inflow within this horizon
DORMANCY_DAYS = 30                    # S9 dormancy gap
BURST_WINDOW_DAYS = 30                # S9 peak turnover window
BASE_TURNOVER_FLOOR = 10_000.0        # S9 floor on mu_base
ELECTRONIC_CHANNELS = ("UPI", "NEFT", "RTGS", "IMPS")
# Reporting thresholds; a transaction is "near" when it is within the band immediately below one (paise kept).
STRUCTURING_BANDS: Tuple[Tuple[float, float], ...] = (
    (45_000.0, 50_000.0),
    (180_000.0, 200_000.0),
    (900_000.0, 1_000_000.0),
)
EVIDENCE_CAP = 50

_WALLET_CRYPTO_TOKENS = ("PAYTM WALLET", "MOBIKWIK", "AMAZON PAY", "PHONEPE WALLET", "COINDCX", "WAZIRX", "BINANCE")
_LEAK_CHANNELS = ("ATM", "AEPS", "CASH_WDL")
_DIGITS_RE = re.compile(r"\D+")


def _parse_dt(ts: Optional[str], d: str) -> dt.datetime:
    if ts:
        try:
            return dt.datetime.fromisoformat(ts.replace("Z", "")).replace(tzinfo=None)
        except ValueError:
            pass
    return dt.datetime.fromisoformat(f"{d}T12:00:00")


def _amt(tx: NormalizedTransaction) -> float:
    return float(tx.credit if tx.direction == TxDirection.CREDIT else tx.debit)


def _is_credit(tx: NormalizedTransaction) -> bool:
    return tx.direction == TxDirection.CREDIT and float(tx.credit) > 0


def _is_debit(tx: NormalizedTransaction) -> bool:
    return tx.direction == TxDirection.DEBIT and float(tx.debit) > 0


def counterparty_key(tx: NormalizedTransaction) -> Optional[str]:
    """Identity of the other party: resolved entity, else normalised name, else VPA/account; None if unknown."""
    if tx.resolved_entity_id:
        return f"E:{tx.resolved_entity_id}"
    if tx.extracted_counterparty:
        norm = normalize_entity_name(tx.extracted_counterparty)
        if norm:
            return f"N:{norm}"
    if tx.counterparty_vpa_or_acc:
        return f"V:{tx.counterparty_vpa_or_acc.strip().lower()}"
    return None


def _cap(ids: Iterable[str]) -> List[str]:
    return list(dict.fromkeys(i for i in ids if i))[:EVIDENCE_CAP]


def _weighted_median_hold(holds: List[Tuple[float, float, str, str]]) -> Optional[float]:
    if not holds:
        return None
    ordered = sorted(holds, key=lambda h: h[0])
    total = sum(h[1] for h in ordered)
    cum = 0.0
    for hrs, matched, _, _ in ordered:
        cum += matched
        if cum >= total * 0.5:
            return hrs
    return ordered[-1][0]


def _account_key(st: BankAccountStatement) -> Tuple[str, str]:
    digits = _DIGITS_RE.sub("", st.account_number or "")
    if not digits:
        return ("STMT", st.statement_id)
    return ((st.bank_code or st.bank_name or "").upper(), digits)


def _tier(score: float) -> MuleRiskTier:
    if score >= 80.0:
        return MuleRiskTier.CRITICAL
    if score >= 50.0:
        return MuleRiskTier.SUSPICIOUS
    return MuleRiskTier.LOW_RISK


# --------------------------------------------------------------------------------------------- parameters
def _s1_flow_through(txns: Sequence[NormalizedTransaction]) -> Tuple[float, Dict[str, Any], List[str]]:
    total_cr = sum(float(t.credit) for t in txns)
    total_dr = sum(float(t.debit) for t in txns)
    max_flow = max(total_cr, total_dr)
    ratio = (1.0 - abs(total_cr - total_dr) / max_flow) if max_flow > 0 else 0.0
    gated = max_flow >= MIN_TURNOVER_GATE
    score = max(0.0, ratio) if gated else 0.0
    raw = {
        "total_credits": round(total_cr, 2),
        "total_debits": round(total_dr, 2),
        "flow_through_ratio": round(ratio, 4),
        "turnover_gate": MIN_TURNOVER_GATE,
        "turnover_gate_passed": gated,
    }
    # Evidence: the largest credits and debits that make up the in/out balance.
    credits = sorted((t for t in txns if _is_credit(t)), key=lambda t: float(t.credit), reverse=True)[:10]
    debits = sorted((t for t in txns if _is_debit(t)), key=lambda t: float(t.debit), reverse=True)[:10]
    return score, raw, [t.tx_id for t in credits + debits] if score > 0 else []


def _s2_velocity(txns: Sequence[NormalizedTransaction], times: Sequence[dt.datetime]) -> Tuple[float, Dict[str, Any], List[str], Optional[float]]:
    lots: deque = deque()  # [time, remaining, tx_id]
    holds: List[Tuple[float, float, str, str]] = []
    for tx, t_dt in zip(txns, times):
        if _is_credit(tx):
            lots.append([t_dt, float(tx.credit), tx.tx_id])
        elif _is_debit(tx):
            rem = float(tx.debit)
            while rem > 0.01 and lots:
                lot = lots[0]
                matched = min(rem, lot[1])
                hrs = max(0.05, (t_dt - lot[0]).total_seconds() / 3600.0)
                holds.append((hrs, matched, lot[2], tx.tx_id))
                lot[1] -= matched
                if lot[1] <= 0.01:
                    lots.popleft()
                rem -= matched
    med = _weighted_median_hold(holds)
    score = math.exp(-med / HOLD_HALF_LIFE_HOURS) if med is not None else 0.0
    evidence: List[str] = []
    for hrs, _, cr_id, dr_id in holds:
        if hrs <= 24.0:
            evidence.extend((cr_id, dr_id))
    raw = {
        "median_hold_hours": round(med, 2) if med is not None else None,
        "fifo_matched_amount": round(sum(h[1] for h in holds), 2),
        "fifo_matches": len(holds),
        "matches_within_24h": sum(1 for h in holds if h[0] <= 24.0),
        "half_life_hours": HOLD_HALF_LIFE_HOURS,
    }
    return score, raw, evidence, med


def _s3_lifestyle(txns: Sequence[NormalizedTransaction]) -> Tuple[float, Dict[str, Any], List[str], int]:
    life = [
        t for t in txns
        if CATEGORY_BY_ID.get(t.category_id) and CATEGORY_BY_ID[t.category_id].is_lifestyle
    ]
    start_d = dt.date.fromisoformat(txns[0].txn_date)
    end_d = dt.date.fromisoformat(txns[-1].txn_date)
    active_months = max(1.0, ((end_d - start_d).days + 1) / 30.0)
    rate = len(life) / active_months
    score = max(0.0, 1.0 - min(1.0, rate / LIFESTYLE_EXPECTED_PER_MONTH))
    raw = {
        "lifestyle_txn_count": len(life),
        "active_months": round(active_months, 2),
        "lifestyle_txns_per_month": round(rate, 3),
        "expected_per_month": LIFESTYLE_EXPECTED_PER_MONTH,
    }
    # Evidence for an absence parameter: the few lifestyle rows that do exist.
    return score, raw, [t.tx_id for t in life], len(life)


def _s4_concentration(txns: Sequence[NormalizedTransaction]) -> Tuple[float, Dict[str, Any], List[str]]:
    vols: Dict[str, float] = defaultdict(float)
    ids: Dict[str, List[str]] = defaultdict(list)
    unidentified = 0
    for tx in txns:
        key = counterparty_key(tx)
        if key is None:
            # Unidentified rows are never pooled together (TF-18): each is its own singleton bucket.
            unidentified += 1
            key = f"U:{tx.tx_id}"
        vols[key] += _amt(tx)
        ids[key].append(tx.tx_id)
    total = sum(vols.values())
    score = sum((v / total) ** 2 for v in vols.values()) if total > 0 else 0.0
    top = sorted(vols.items(), key=lambda kv: kv[1], reverse=True)[:3]
    raw = {
        "hhi": round(score, 4),
        "counterparties": len(vols) - unidentified,
        "unidentified_txns": unidentified,
        "top_counterparty_share": round(top[0][1] / total, 4) if top and total > 0 else 0.0,
    }
    evidence = [i for k, _ in top if not k.startswith("U:") for i in ids[k]]
    return score, raw, evidence


def _s5_fan(txns: Sequence[NormalizedTransaction], times: Sequence[dt.datetime]) -> Tuple[float, Dict[str, Any], List[str]]:
    n = len(txns)
    keys = [counterparty_key(t) or f"U:{(t.raw_narration or '')[:20].upper()}" for t in txns]
    senders: Counter = Counter()
    receivers: Counter = Counter()
    best = (0.0, 0, 0, 0, 0)  # score, din, dout, left, right
    right = 0
    for left in range(n):
        win_end = times[left] + dt.timedelta(hours=24)
        while right < n and times[right] <= win_end:
            if _is_credit(txns[right]):
                senders[keys[right]] += 1
            elif _is_debit(txns[right]):
                receivers[keys[right]] += 1
            right += 1
        d_in, d_out = len(senders), len(receivers)
        fan_in = min(1.0, d_in / FAN_DEGREE_SATURATION) * (1.0 - min(0.8, d_out / (d_in + 1.0)))
        fan_out = min(1.0, d_out / FAN_DEGREE_SATURATION) * (1.0 - min(0.8, d_in / (d_out + 1.0)))
        s = max(fan_in, fan_out)
        if s > best[0]:
            best = (s, d_in, d_out, left, right)
        # slide: drop txns[left] from the window
        if _is_credit(txns[left]):
            senders[keys[left]] -= 1
            if senders[keys[left]] <= 0:
                del senders[keys[left]]
        elif _is_debit(txns[left]):
            receivers[keys[left]] -= 1
            if receivers[keys[left]] <= 0:
                del receivers[keys[left]]
    score, d_in, d_out, lo, hi = best
    raw = {
        "peak_window_unique_senders": d_in,
        "peak_window_unique_receivers": d_out,
        "peak_window_start": times[lo].isoformat() if score > 0 else None,
        "pattern": ("COLLECTOR" if d_in >= d_out else "DISTRIBUTOR") if score > 0 else None,
        "window_hours": 24,
    }
    evidence = [t.tx_id for t in txns[lo:hi]] if score > 0 else []
    return min(1.0, score), raw, evidence


def _is_leakage(tx: NormalizedTransaction) -> bool:
    spec = CATEGORY_BY_ID.get(tx.category_id)
    narr_u = (tx.raw_narration or "").upper()
    return bool(
        (spec and spec.is_cash_crypto_leakage)
        or tx.category_id == 51
        or tx.payment_channel in _LEAK_CHANNELS
        or any(w in narr_u for w in _WALLET_CRYPTO_TOKENS)
    )


def _s6_leakage(txns: Sequence[NormalizedTransaction]) -> Tuple[float, Dict[str, Any], List[str], float]:
    total_dr = sum(float(t.debit) for t in txns)
    leak = [t for t in txns if _is_debit(t) and _is_leakage(t)]
    leak_amt = sum(float(t.debit) for t in leak)
    pct = (leak_amt / total_dr) if total_dr > 0 else 0.0
    raw = {"leakage_debits": round(leak_amt, 2), "total_debits": round(total_dr, 2), "leakage_pct": round(pct * 100.0, 2)}
    return min(1.0, pct), raw, [t.tx_id for t in leak], pct


def _near_threshold(amount: float) -> bool:
    return any(lo <= amount < hi for lo, hi in STRUCTURING_BANDS)


def _s7_structuring(txns: Sequence[NormalizedTransaction]) -> Tuple[float, Dict[str, Any], List[str]]:
    near = {t.tx_id for t in txns if _near_threshold(_amt(t))}
    # Same-day split transfers: >= 2 sub-threshold rows of one direction with one counterparty (or one cash
    # channel) on one day whose sum reaches a reporting threshold while each row stays below it.
    groups: Dict[Tuple[str, str, str], List[NormalizedTransaction]] = defaultdict(list)
    for t in txns:
        key = counterparty_key(t) or (f"C:{t.payment_channel}" if t.payment_channel in ("CASH", "ATM", "AEPS", "CASH_WDL", "CASH_DEP") else None)
        if key is None:
            continue
        groups[(t.txn_date, t.direction.value, key)].append(t)
    split_ids: set = set()
    split_groups = 0
    thresholds = [hi for _, hi in STRUCTURING_BANDS]
    for rows in groups.values():
        if len(rows) < 2:
            continue
        total = sum(_amt(r) for r in rows)
        biggest = max(_amt(r) for r in rows)
        if any(biggest < th <= total for th in thresholds):
            split_groups += 1
            split_ids.update(r.tx_id for r in rows)
    flagged = near | split_ids
    score = min(1.0, 3.0 * len(flagged) / len(txns)) if txns else 0.0
    raw = {
        "near_threshold_txns": len(near),
        "split_structuring_groups": split_groups,
        "split_structuring_txns": len(split_ids),
        "flagged_txns": len(flagged),
        "total_txns": len(txns),
    }
    order = [t.tx_id for t in txns if t.tx_id in flagged]
    return score, raw, order


def _has_balance(txns: Sequence[NormalizedTransaction]) -> bool:
    return any(float(t.running_balance) != 0.0 for t in txns)


def _s8_zero_oscillation(txns: Sequence[NormalizedTransaction]) -> Tuple[float, Dict[str, Any], List[str]]:
    peak_cr = max((float(t.credit) for t in txns), default=0.0)
    floor = max(500.0, 0.01 * peak_cr)
    if not _has_balance(txns):
        # TF-20: without a balance column nothing can be said about draining to zero.
        return 0.0, {"balance_available": False, "zero_floor": round(floor, 2), "inflow_cycles": 0, "drained_cycles": 0}, []
    # A cycle starts on each day with an inflow > 10,000 and runs until the next such day (max 30 days).
    cycle_days: List[str] = []
    for t in txns:
        if _is_credit(t) and float(t.credit) > LARGE_INFLOW and (not cycle_days or cycle_days[-1] != t.txn_date):
            cycle_days.append(t.txn_date)
    drained = 0
    evidence: List[str] = []
    for i, day in enumerate(cycle_days):
        next_day = cycle_days[i + 1] if i + 1 < len(cycle_days) else None
        limit = (dt.date.fromisoformat(day) + dt.timedelta(days=S8_CYCLE_MAX_DAYS)).isoformat()
        seen_inflow = False
        for t in txns:
            if t.txn_date < day:
                continue
            if next_day is not None and t.txn_date >= next_day:
                break
            if t.txn_date > limit:
                break
            if _is_credit(t) and float(t.credit) > LARGE_INFLOW:
                seen_inflow = True
                continue
            if seen_inflow and _is_debit(t) and float(t.running_balance) <= floor:
                drained += 1
                evidence.append(t.tx_id)
                break
    score = (drained / len(cycle_days)) if cycle_days else 0.0
    raw = {
        "balance_available": True,
        "zero_floor": round(floor, 2),
        "inflow_cycles": len(cycle_days),
        "drained_cycles": drained,
        "large_inflow_threshold": LARGE_INFLOW,
    }
    return min(1.0, score), raw, evidence


def _month_iter(start: dt.date, end: dt.date) -> List[str]:
    out: List[str] = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


def _s9_burst(
    txns: Sequence[NormalizedTransaction], period_start: Optional[dt.date]
) -> Tuple[float, Dict[str, Any], List[str]]:
    dates = [dt.date.fromisoformat(t.txn_date) for t in txns]
    vols = [float(t.credit) + float(t.debit) for t in txns]
    # Peak rolling 30-day turnover.
    best_vol, best_lo, best_hi = 0.0, 0, 0
    right, acc = 0, 0.0
    for left in range(len(txns)):
        end = dates[left] + dt.timedelta(days=BURST_WINDOW_DAYS - 1)
        while right < len(txns) and dates[right] <= end:
            acc += vols[right]
            right += 1
        if acc > best_vol:
            best_vol, best_lo, best_hi = acc, left, right
        acc -= vols[left]
    peak_start = dates[best_lo]
    # Dormancy gap >= 30 days that ends at or before the peak window starts (dormant, then burst).
    anchors = ([period_start] if period_start and period_start < dates[0] else []) + dates[: best_lo + 1]
    max_gap = 0
    for a, b in zip(anchors, anchors[1:]):
        max_gap = max(max_gap, (b - a).days)
    # Median monthly turnover over the calendar months before the peak window (zero months included).
    first_day = min(anchors) if anchors else dates[0]
    monthly: Dict[str, float] = defaultdict(float)
    for d, v in zip(dates, vols):
        monthly[d.strftime("%Y-%m")] += v
    prior_months = _month_iter(first_day, peak_start)[:-1]
    prior_vals = [monthly.get(m, 0.0) for m in prior_months]
    mu_base = statistics.median(prior_vals) if prior_vals else 0.0
    dormant = max_gap >= DORMANCY_DAYS
    score = (1.0 - math.exp(-best_vol / (max(mu_base, BASE_TURNOVER_FLOOR) * 5.0))) if dormant else 0.0
    raw = {
        "max_dormancy_gap_days": max_gap,
        "dormancy_threshold_days": DORMANCY_DAYS,
        "peak_30d_turnover": round(best_vol, 2),
        "peak_window_start": peak_start.isoformat(),
        "median_prior_monthly_turnover": round(mu_base, 2),
        "prior_months": len(prior_vals),
    }
    evidence = [t.tx_id for t in txns[best_lo:best_hi]] if dormant else []
    return score, raw, evidence


def _s10_round(txns: Sequence[NormalizedTransaction]) -> Tuple[float, Dict[str, Any], List[str]]:
    electronic = [t for t in txns if (t.payment_channel or "").upper() in ELECTRONIC_CHANNELS and _amt(t) > 0]
    hits = [t for t in electronic if abs(math.fmod(_amt(t), 5000.0)) < 0.005]
    score = (len(hits) / len(electronic)) if electronic else 0.0
    raw = {
        "electronic_txns": len(electronic),
        "round_5000_multiples": len(hits),
        "round_10000_multiples": sum(1 for t in hits if abs(math.fmod(_amt(t), 10000.0)) < 0.005),
    }
    return score, raw, [t.tx_id for t in hits]


_PRIMARY_RAW = {
    "flow_through_ratio": "flow_through_ratio",
    "velocity_hold_time": "median_hold_hours",
    "lifestyle_spending_absence": "lifestyle_txns_per_month",
    "counterparty_concentration": "hhi",
    "fan_in_fan_out_pattern": "peak_window_unique_senders",
    "cash_crypto_wallet_leakage": "leakage_pct",
    "structuring_smurfing": "flagged_txns",
    "zero_balance_oscillation": "drained_cycles",
    "burst_dormancy_pattern": "peak_30d_turnover",
    "round_number_anomalies": "round_5000_multiples",
}


# --------------------------------------------------------------------------------------------- scoring
def score_account_mule_risk(
    statement: BankAccountStatement,
    account_txns: List[NormalizedTransaction],
    entity_id: Optional[str] = None,
    statements: Optional[List[BankAccountStatement]] = None,
) -> MuleRiskAssessment:
    """
    Evaluate one bank account across all 10 weighted behavioural parameters (0-100):
      Critical (80-100), Suspicious (50-79), Low Risk (0-49).

    ``statement`` supplies the account metadata; ``statements`` (optional) lists every statement of the same
    account whose rows are in ``account_txns`` (used for coverage start and statement_ids).
    """
    stmts = list(statements) if statements else [statement]
    stmt_rank = {s.statement_id: i for i, s in enumerate(sorted(stmts, key=lambda s: (s.period_start or "", s.period_end or "")))}
    txns = sorted(
        (t for t in account_txns if not t.is_duplicate_ignored),
        key=lambda t: (_parse_dt(t.txn_timestamp, t.txn_date), stmt_rank.get(t.statement_id, 0), t.row_index),
    )
    base = dict(
        account_number=statement.account_number,
        account_holder_name=statement.account_holder_name,
        bank_name=statement.bank_name,
        entity_id=entity_id,
    )
    if not txns:
        zero = MuleParameterBreakdown(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
        empty_vec = [
            {
                "key": k, "rank": i + 1, "label": PARAMETER_LABELS[k], "weight": w, "score": 0.0, "contribution": 0.0,
                "raw_metric": None, "raw": {}, "formula": PARAMETER_FORMULAS[k], "evidence_tx_ids": [], "evidence_count": 0,
            }
            for i, (k, w) in enumerate(PARAMETER_WEIGHTS.items())
        ]
        res = MuleRiskAssessment(composite_score=0.0, risk_tier=MuleRiskTier.LOW_RISK, parameters=zero, **base)
        res.weighted_contributions = {k: 0.0 for k in PARAMETER_WEIGHTS}
        res.explainability = empty_vec
        res.statement_ids = [s.statement_id for s in stmts]
        return res

    times = [_parse_dt(t.txn_timestamp, t.txn_date) for t in txns]
    starts = [d for d in (s.period_start for s in stmts) if d]
    try:
        period_start = dt.date.fromisoformat(min(starts)[:10]) if starts else None
    except ValueError:
        period_start = None

    s1, r1, e1 = _s1_flow_through(txns)
    s2, r2, e2, med_hold = _s2_velocity(txns, times)
    s3, r3, e3, life_cnt = _s3_lifestyle(txns)
    s4, r4, e4 = _s4_concentration(txns)
    s5, r5, e5 = _s5_fan(txns, times)
    s6, r6, e6, leak_pct = _s6_leakage(txns)
    s7, r7, e7 = _s7_structuring(txns)
    s8, r8, e8 = _s8_zero_oscillation(txns)
    s9, r9, e9 = _s9_burst(txns, period_start)
    s10, r10, e10 = _s10_round(txns)

    results = {
        "flow_through_ratio": (s1, r1, e1),
        "velocity_hold_time": (s2, r2, e2),
        "lifestyle_spending_absence": (s3, r3, e3),
        "counterparty_concentration": (s4, r4, e4),
        "fan_in_fan_out_pattern": (s5, r5, e5),
        "cash_crypto_wallet_leakage": (s6, r6, e6),
        "structuring_smurfing": (s7, r7, e7),
        "zero_balance_oscillation": (s8, r8, e8),
        "burst_dormancy_pattern": (s9, r9, e9),
        "round_number_anomalies": (s10, r10, e10),
    }
    scores = {k: round(min(1.0, max(0.0, v[0])), 4) for k, v in results.items()}
    breakdown = MuleParameterBreakdown(**scores)
    contributions = {k: round(100.0 * PARAMETER_WEIGHTS[k] * scores[k], 2) for k in PARAMETER_WEIGHTS}
    composite = round(min(100.0, max(0.0, sum(100.0 * PARAMETER_WEIGHTS[k] * scores[k] for k in PARAMETER_WEIGHTS))), 2)

    vector: List[Dict[str, Any]] = []
    flagged: List[str] = []
    for i, k in enumerate(PARAMETER_WEIGHTS):
        _, raw, ev = results[k]
        ev_ids = list(dict.fromkeys(ev))
        # Evidence supports a score only when the parameter fired (S3 rows are counter-evidence).
        if scores[k] > 0 and k != "lifestyle_spending_absence":
            flagged.extend(ev_ids)
        vector.append({
            "key": k,
            "rank": i + 1,
            "label": PARAMETER_LABELS[k],
            "weight": PARAMETER_WEIGHTS[k],
            "score": scores[k],
            "contribution": contributions[k],
            "raw_metric": raw.get(_PRIMARY_RAW[k]),
            "raw": raw,
            "formula": PARAMETER_FORMULAS[k],
            "evidence_tx_ids": ev_ids[:EVIDENCE_CAP],
            "evidence_count": len(ev_ids),
        })

    res = MuleRiskAssessment(
        composite_score=composite,
        risk_tier=_tier(composite),
        parameters=breakdown,
        total_credited=r1["total_credits"],
        total_debited=r1["total_debits"],
        tx_count=len(txns),
        median_hold_hours=round(med_hold, 2) if med_hold is not None else None,
        cash_crypto_leakage_pct=round(leak_pct * 100.0, 2),
        lifestyle_txn_count=life_cnt,
        weighted_contributions=contributions,
        flagged_evidence_tx_ids=_cap(flagged),
        **base,
    )
    res.explainability = vector
    res.statement_ids = [s.statement_id for s in stmts]
    return res


def evaluate_all_mule_accounts(
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
) -> List[MuleRiskAssessment]:
    """Score every uploaded bank account once (all its statements together), sorted by composite score."""
    holder_to_eid: Dict[str, str] = {}
    acct_to_eid: Dict[str, str] = {}
    for e in entities:
        if e.has_statement:
            holder_to_eid.setdefault(normalize_entity_name(e.canonical_name), e.entity_id)
            for al in e.aliases:
                holder_to_eid.setdefault(normalize_entity_name(al), e.entity_id)
            for acc in e.linked_account_numbers:
                digits = _DIGITS_RE.sub("", acc or "")
                if digits:
                    acct_to_eid.setdefault(digits, e.entity_id)

    tx_by_stmt: Dict[str, List[NormalizedTransaction]] = defaultdict(list)
    for tx in transactions:
        tx_by_stmt[tx.statement_id].append(tx)

    groups: Dict[Tuple[str, str], List[BankAccountStatement]] = defaultdict(list)
    for st in statements:
        groups[_account_key(st)].append(st)

    assessments: List[MuleRiskAssessment] = []
    for key, stmts in groups.items():
        stmts.sort(key=lambda s: (s.period_start or "", s.period_end or ""))
        latest = stmts[-1]
        eid = acct_to_eid.get(key[1]) if key[0] != "STMT" else None
        eid = eid or holder_to_eid.get(normalize_entity_name(latest.account_holder_name))
        rows = [t for s in stmts for t in tx_by_stmt.get(s.statement_id, [])]
        assessments.append(score_account_mule_risk(latest, rows, entity_id=eid, statements=stmts))

    assessments.sort(key=lambda a: a.composite_score, reverse=True)
    return assessments
