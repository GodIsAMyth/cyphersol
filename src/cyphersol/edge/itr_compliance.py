"""CypherEdge ITR-Ready Tax & Statutory Compliance Report Engine."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from cyphersol.models import BankAccountStatement, NormalizedTransaction, TxDirection
from cyphersol.nlp.entity_resolver import is_system_party_name
from cyphersol.taxonomy import CATEGORY_BY_ID

_FY_RE = re.compile(r"^(?:FY\s*)?(\d{4})(?:\s*[-/]\s*(\d{2}|\d{4}))?$", re.I)


def _indian_fy(iso_date: str) -> str:
    try:
        year = int(iso_date[:4])
        month = int(iso_date[5:7])
        if month >= 4:
            return f"FY {year}-{str(year + 1)[-2:]}"
        return f"FY {year - 1}-{str(year)[-2:]}"
    except (ValueError, IndexError):
        return "FY Unknown"


def _fy_start_year(fy_label: str) -> Optional[int]:
    try:
        return int(fy_label[3:7])
    except (ValueError, IndexError):
        return None


def normalize_fy(value: str) -> str:
    """Accept 'FY 2025-26', '2025-26', '2025-2026' or '2025' (start year) and return 'FY 2025-26'."""
    m = _FY_RE.match((value or "").strip())
    if not m:
        raise ValueError(f"Unrecognised financial year {value!r}; expected e.g. 'FY 2025-26'")
    start = int(m.group(1))
    if m.group(2):
        end = int(m.group(2))
        expected = start + 1 if end >= 1000 else (start + 1) % 100
        if end != expected:
            raise ValueError(f"Financial year {value!r} must span consecutive years")
    return f"FY {start}-{str(start + 1)[-2:]}"


# ---------------------------------------------------------------------------
# TDS thresholds (dated by the FY in which the payment is made)
# ---------------------------------------------------------------------------
# Sources: Income-tax Act 1961 ss. 194C(5), 194H, 194-I, 194J as amended by the Finance Act 2025
# (thresholds revised w.e.f. 1 April 2025, i.e. from FY 2025-26):
#   194C  single contract > 30,000 or aggregate in the FY > 1,00,000 (unchanged)
#   194H  commission/brokerage aggregate in the FY > 15,000; > 20,000 from FY 2025-26
#   194-I rent aggregate in the FY > 2,40,000 up to FY 2024-25; from FY 2025-26 > 50,000
#         for a month or part of a month
#   194J  professional / technical fees aggregate in the FY > 30,000; > 50,000 from FY 2025-26
# These sections bind payers other than individuals/HUFs not subject to tax audit u/s 44AB;
# the report flags candidates for verification, it does not determine liability.


@dataclass(frozen=True)
class TdsRule:
    section: str
    title: str
    from_fy_start: int           # first FY start year the rule applies to
    to_fy_start: Optional[int]   # last FY start year (inclusive), None = open-ended
    period: str                  # "fy" or "month"
    aggregate_limit: float       # aggregate per payee per period must EXCEED this
    single_limit: Optional[float] = None  # any single payment exceeding this also triggers
    rate_note: str = ""


TDS_THRESHOLDS: Tuple[TdsRule, ...] = (
    TdsRule("Section 194C (TDS)", "Contractor / Vendor Payments", 2000, None, "fy", 100_000, 30_000,
            "1% (individual/HUF) / 2% (others)"),
    TdsRule("Section 194H (TDS)", "Commission / Brokerage Paid", 2000, 2024, "fy", 15_000, None, "5% (2% from 1 Oct 2024)"),
    TdsRule("Section 194H (TDS)", "Commission / Brokerage Paid", 2025, None, "fy", 20_000, None, "2%"),
    TdsRule("Section 194I (TDS)", "Rent Paid", 2000, 2024, "fy", 240_000, None, "10% (land/building)"),
    TdsRule("Section 194I (TDS)", "Rent Paid", 2025, None, "month", 50_000, None, "10% (land/building)"),
    TdsRule("Section 194J (TDS)", "Professional / Technical Fees Paid", 2000, 2024, "fy", 30_000, None, "10% / 2%"),
    TdsRule("Section 194J (TDS)", "Professional / Technical Fees Paid", 2025, None, "fy", 50_000, None, "10% / 2%"),
)

_COMMISSION_RE = re.compile(r"\b(COMMISSION|COMMN|BROKERAGE|BROKER\s+FEE)\b")
_SELF_RE = re.compile(r"\b(SELF|OWN\s+A/?C|ATM|CDM|CASH\s+WDL|CASH\s+WITHDRAWAL|AEPS)\b")

# Rule 114E SFT-003 cash-deposit thresholds (aggregate per FY, >= inclusive)
SFT_CASH_SAVINGS_LIMIT = 1_000_000   # one or more accounts other than current account / time deposit
SFT_CASH_CURRENT_LIMIT = 5_000_000   # one or more current accounts
SEC_269ST_LIMIT = 200_000            # receipt from a person in a day / per transaction (>=)
SEC_40A3_LIMIT = 10_000              # cash payment to a person in a day (> ; 35,000 for transporters)

# Rule 114E SFT codes for investments / property / card (reported by banks, registrars, RTAs; the
# report reconciles the bank-statement side against what AIS will show).
SFT_TIME_DEPOSIT_LIMIT = 1_000_000   # SFT-004: time deposits aggregating >= 10 lakh in an FY (excl. renewals)
SFT_CREDIT_CARD_LIMIT = 1_000_000    # SFT-005: credit card bills paid by any mode >= 10 lakh in an FY
SFT_SHARES_LIMIT = 1_000_000         # SFT-007/008: shares / buy-back >= 10 lakh in an FY
SFT_MUTUAL_FUND_LIMIT = 1_000_000    # SFT-009: mutual fund units >= 10 lakh in an FY
SFT_PROPERTY_LIMIT = 3_000_000       # SFT-012: purchase / sale of immovable property >= 30 lakh

_RENEWAL_RE = re.compile(r"\b(RENEW|RENEWAL|RENEWED|AUTO\s*RENEW)\b")
_PROPERTY_RE = re.compile(
    r"\b(PROPERTY|FLAT|PLOT|APARTMENT|VILLA|SALE\s+DEED|REGISTRATION\s+FEE|SUB\s*REGISTRAR|STAMP\s+DUTY|"
    r"BUILDERS?|DEVELOPERS?|REALTY|REALTORS?|REAL\s+ESTATE)\b"
)
_PROPERTY_EXCLUDED_CATEGORIES = frozenset({8, 23, 24, 25, 29, 6, 13, 40, 49, 50, 51})

# Chapter VI-A sub-classification (EDGE-18)
_HEALTH_INS_RE = re.compile(r"\b(HEALTH|MEDICLAIM|MEDICLAIMS|MEDICAL\s+INS|STAR\s+HEALTH|CARE\s+HEALTH|NIVA|BUPA|"
                            r"MANIPAL\s*CIGNA|AROGYA|HOSPITALI[SZ]ATION)\b")
_MOTOR_INS_RE = re.compile(r"\b(MOTOR|VEHICLE|CAR|BIKE|TWO\s*WHEELER|FOUR\s*WHEELER|TRAVEL\s+INS|HOME\s+INS|"
                           r"FIRE|MARINE|SHOP\s*KEEPER)\b")
_LIFE_INS_RE = re.compile(r"\b(LIC|LIFE|TERM|JEEVAN|PLI|POSTAL\s+LIFE|ENDOWMENT|ULIP|PRU|PRUDENTIAL|TATA\s*AIA|"
                          r"MAX\s+LIFE|AVIVA|EXIDE\s+LIFE|AEGON)\b")
_ELSS_RE = re.compile(r"\b(ELSS|TAX\s*SAVER|TAXSAVER|TAX\s+SAVING|80C|LONG\s+TERM\s+EQUITY)\b")
_NPS_RE = re.compile(r"\b(NPS|PRAN|NATIONAL\s+PENSION|ATAL\s+PENSION|APY|PFRDA)\b")
_80C_SCHEME_RE = re.compile(r"\b(PPF|EPF|VPF|PROVIDENT|SUKANYA|SSY|NSC|SCSS)\b")
_EDU_LOAN_RE = re.compile(r"\b(EDUCATION\s+LOAN|EDU\s+LOAN|STUDENT\s+LOAN|EDUCATIONAL\s+LOAN)\b")
_DONATION_RE = re.compile(r"\b(DONATION|DONATIONS|PM\s*CARES|PMNRF|CHARITY|CHARITABLE)\b")

DEDUCTION_CAPS: Dict[str, Optional[float]] = {
    "Sec 80C": 150_000,
    "Sec 80CCD(1B)": 50_000,
    "Sec 80D": 25_000,           # 50,000 where the insured is a senior citizen
    "Sec 24(b) / 80C": 200_000,  # interest u/s 24(b) for a self-occupied property; principal falls in 80C
    "Sec 80E": None,
    "Sec 80G": None,
}


def _tds_rule_for(section_prefix: str, fy_start: Optional[int]) -> Optional[TdsRule]:
    if fy_start is None:
        return None
    for rule in TDS_THRESHOLDS:
        if rule.section.startswith(section_prefix) and rule.from_fy_start <= fy_start and (
            rule.to_fy_start is None or fy_start <= rule.to_fy_start
        ):
            return rule
    return None


def _norm_name(name: str) -> str:
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", (name or "").upper()).split())


def _payee_key(tx: NormalizedTransaction) -> str:
    name = (tx.extracted_counterparty or "").strip().upper()
    if tx.resolved_entity_id:
        return f"ENT:{tx.resolved_entity_id}"
    if name:
        return f"NAME:{re.sub(r'[^A-Z0-9]+', ' ', name).strip()}"
    if tx.counterparty_vpa_or_acc:
        return f"ACC:{tx.counterparty_vpa_or_acc.strip().upper()}"
    return "UNIDENTIFIED"


def _payee_label(tx: NormalizedTransaction) -> str:
    return (tx.extracted_counterparty or tx.counterparty_vpa_or_acc or "Unidentified payee").strip()


def _tds_section(tx: NormalizedTransaction) -> Optional[str]:
    if tx.direction != TxDirection.DEBIT or float(tx.debit) <= 0:
        return None
    if tx.category_id in (27, 28, 29, 50, 51, 49):
        return None
    narr = (tx.raw_narration or "").upper()
    if _COMMISSION_RE.search(narr):
        return "Section 194H"
    if tx.category_id == 13:
        return "Section 194I"
    if tx.category_id == 18:
        return "Section 194J"
    if tx.category_id in (11, 19, 20, 22):
        return "Section 194C"
    return None


def _tds_flags(fy_txns: Iterable[NormalizedTransaction], fy_start: Optional[int]) -> List[Dict[str, Any]]:
    # section -> payee_key -> {"label", "fy_total", "months": {ym: amt}, "max_single", "count"}
    buckets: Dict[str, Dict[str, Dict[str, Any]]] = defaultdict(dict)
    for tx in fy_txns:
        sec = _tds_section(tx)
        if sec is None:
            continue
        key = _payee_key(tx)
        b = buckets[sec].setdefault(key, {"label": _payee_label(tx), "fy_total": 0.0, "months": defaultdict(float),
                                          "max_single": 0.0, "count": 0})
        amt = float(tx.debit)
        b["fy_total"] += amt
        b["months"][tx.txn_date[:7]] += amt
        b["max_single"] = max(b["max_single"], amt)
        b["count"] += 1

    flags: List[Dict[str, Any]] = []
    for sec in ("Section 194C", "Section 194H", "Section 194I", "Section 194J"):
        rule = _tds_rule_for(sec, fy_start)
        if rule is None or sec not in buckets:
            continue
        offenders: List[Dict[str, Any]] = []
        for key, b in buckets[sec].items():
            reasons: List[str] = []
            if rule.period == "month":
                months = sorted(ym for ym, amt in b["months"].items() if amt > rule.aggregate_limit)
                if months:
                    reasons.append(f"rent > ₹{rule.aggregate_limit:,.0f} in month(s) {', '.join(months)}")
            elif b["fy_total"] > rule.aggregate_limit:
                reasons.append(f"FY aggregate ₹{b['fy_total']:,.2f} > ₹{rule.aggregate_limit:,.0f}")
            if rule.single_limit is not None and b["max_single"] > rule.single_limit:
                reasons.append(f"single payment ₹{b['max_single']:,.2f} > ₹{rule.single_limit:,.0f}")
            if reasons:
                offenders.append({
                    "payee": b["label"],
                    "payee_key": key,
                    "amount": round(b["fy_total"], 2),
                    "payment_count": b["count"],
                    "reasons": reasons,
                })
        if not offenders:
            continue
        offenders.sort(key=lambda o: -o["amount"])
        limit_desc = (
            f"₹{rule.aggregate_limit:,.0f} per month" if rule.period == "month"
            else f"₹{rule.aggregate_limit:,.0f} per FY per payee"
        )
        if rule.single_limit is not None:
            limit_desc += f" or ₹{rule.single_limit:,.0f} single payment"
        flags.append({
            "section": rule.section,
            "severity": "MEDIUM",
            "title": f"{rule.title} Above TDS Threshold ({len(offenders)} payee(s))",
            "amount": round(sum(o["amount"] for o in offenders), 2),
            "detail": (f"Threshold {limit_desc} applicable for this FY; TDS rate {rule.rate_note}. "
                       "Applies to payers other than individuals/HUFs below the s.44AB audit limits; "
                       "verify deduction and deposit."),
            "payees": offenders,
        })
    return flags


def _is_cash_payment_to_party(tx: NormalizedTransaction, holder_names: set) -> bool:
    if tx.direction != TxDirection.DEBIT or float(tx.debit) <= 0:
        return False
    channel = (tx.payment_channel or "").upper()
    if tx.category_id != 51 and channel not in ("CASH", "CHEQUE_CASH", "BEARER_CHEQUE"):
        return False
    party = (tx.extracted_counterparty or "").strip().upper()
    if not party or party in holder_names or _SELF_RE.search(party):
        return False
    return True


_CURRENT_ACCOUNT_TOKENS = frozenset({"current", "ca", "cc", "od", "overdraft", "occ"})


def _is_current_account(st: Optional[BankAccountStatement]) -> bool:
    """Current / CC / OD accounts use the SFT-003 current-account limit; anything else is savings-like.

    The type is tokenised, so "CC/OD", "Current A/c" and "Cash Credit" all count as current."""
    if st is None:
        return False
    kind = " ".join(re.sub(r"[^a-z]+", " ", (st.account_type or "").lower()).split())
    if "cash credit" in kind:
        return True
    return any(tok in _CURRENT_ACCOUNT_TOKENS for tok in kind.split())


def _account_type_known(st: Optional[BankAccountStatement]) -> bool:
    """False when the statement did not state its account type (EDGE-17): the SFT limit is then an assumption."""
    if st is None:
        return False
    kind = (st.account_type or "").strip().lower()
    return bool(kind) and kind not in ("unknown", "none", "n/a", "na", "not stated")


def _is_cash_receipt(tx: NormalizedTransaction) -> bool:
    if tx.direction != TxDirection.CREDIT or float(tx.credit) <= 0:
        return False
    return tx.category_id == 50 or (tx.payment_channel or "").upper() == "CASH"


# ---------------------------------------------------------------------------
# Check catalogue (dated by FY) — the UI renders this instead of a hard-coded copy
# ---------------------------------------------------------------------------

def _inr(v: float) -> str:
    """Indian digit grouping: 1000000 -> '10,00,000'."""
    s = f"{int(round(v))}"
    if len(s) <= 3:
        return s
    head, tail = s[:-3], s[-3:]
    parts = []
    while len(head) > 2:
        parts.insert(0, head[-2:])
        head = head[:-2]
    if head:
        parts.insert(0, head)
    return ",".join(parts) + "," + tail


def itr_check_catalogue(fy_label: Optional[str]) -> List[Dict[str, Any]]:
    """
    Every statutory check the ITR engine runs for `fy_label` ('FY 2025-26'), in display order, with
    the thresholds in force for that FY. Each entry: {id, group, section, title, rule, thresholds}.
    Red flags in the report carry the matching `check_id`.
    """
    try:
        fy_start = _fy_start_year(normalize_fy(fy_label)) if fy_label and fy_label != "FY Unknown" else None
    except ValueError:
        fy_start = None
    year = max(2000, fy_start) if fy_start is not None else 9999

    def _tds(prefix: str, cid: str, section: str) -> Dict[str, Any]:
        rule = _tds_rule_for(prefix, year)
        assert rule is not None
        if rule.period == "month":
            text = f"Per payee: > ₹{_inr(rule.aggregate_limit)} for a month or part of a month"
        else:
            text = f"Per payee: FY aggregate > ₹{_inr(rule.aggregate_limit)}"
        if rule.single_limit is not None:
            text = (f"Per payee: single payment > ₹{_inr(rule.single_limit)} or FY aggregate "
                    f"> ₹{_inr(rule.aggregate_limit)}")
        return {
            "id": cid, "group": "TDS", "section": section, "title": rule.title,
            "rule": f"{text} (TDS {rule.rate_note}).",
            "thresholds": {"period": rule.period, "aggregate_limit": rule.aggregate_limit,
                           "single_limit": rule.single_limit, "rate_note": rule.rate_note,
                           "applies_from_fy_start": rule.from_fy_start, "applies_to_fy_start": rule.to_fy_start},
        }

    return [
        _tds("Section 194C", "194C", "Sec 194C"),
        _tds("Section 194H", "194H", "Sec 194H"),
        _tds("Section 194I", "194I", "Sec 194-I"),
        _tds("Section 194J", "194J", "Sec 194J"),
        {"id": "40A3", "group": "Cash", "section": "Sec 40A(3)", "title": "Cash payments to a single party",
         "rule": f"Cash paid to one party in a day > ₹{_inr(SEC_40A3_LIMIT)} (₹35,000 for transporters) is "
                 "disallowed as an expense unless a Rule 6DD exception applies.",
         "thresholds": {"per_party_per_day": SEC_40A3_LIMIT, "transporter_per_day": 35_000, "inclusive": False}},
        {"id": "269ST", "group": "Cash", "section": "Sec 269ST", "title": "Cash receipts from a single person",
         "rule": f"Cash received from one person in a day ≥ ₹{_inr(SEC_269ST_LIMIT)} (penalty u/s 271DA). "
                 "Unnamed depositors are flagged for source verification.",
         "thresholds": {"per_person_per_day": SEC_269ST_LIMIT, "inclusive": True}},
        {"id": "SFT003", "group": "SFT", "section": "SFT-003 · Rule 114E",
         "title": "Cash deposits / withdrawals reportable in AIS",
         "rule": f"FY cash deposits ≥ ₹{_inr(SFT_CASH_SAVINGS_LIMIT)} in non-current accounts, or deposits / "
                 f"withdrawals ≥ ₹{_inr(SFT_CASH_CURRENT_LIMIT)} in current accounts.",
         "thresholds": {"non_current_deposits": SFT_CASH_SAVINGS_LIMIT, "current_deposits": SFT_CASH_CURRENT_LIMIT,
                        "current_withdrawals": SFT_CASH_CURRENT_LIMIT, "inclusive": True}},
        {"id": "SFT004", "group": "SFT", "section": "SFT-004 · Rule 114E", "title": "Time deposits",
         "rule": f"Fixed / time deposits (other than renewals) aggregating ≥ ₹{_inr(SFT_TIME_DEPOSIT_LIMIT)} in the FY.",
         "thresholds": {"fy_aggregate": SFT_TIME_DEPOSIT_LIMIT, "inclusive": True}},
        {"id": "SFT005", "group": "SFT", "section": "SFT-005", "title": "Credit card settlements",
         "rule": f"Credit card bills paid in the FY ≥ ₹{_inr(SFT_CREDIT_CARD_LIMIT)} reconcile against AIS.",
         "thresholds": {"fy_aggregate": SFT_CREDIT_CARD_LIMIT, "inclusive": True}},
        {"id": "SFT007", "group": "SFT", "section": "SFT-007/008 · Rule 114E", "title": "Share purchases",
         "rule": f"Broker / share settlements (debits) aggregating ≥ ₹{_inr(SFT_SHARES_LIMIT)} in the FY.",
         "thresholds": {"fy_aggregate": SFT_SHARES_LIMIT, "inclusive": True}},
        {"id": "SFT009", "group": "SFT", "section": "SFT-009 · Rule 114E", "title": "Mutual fund purchases",
         "rule": f"Mutual fund / SIP purchases aggregating ≥ ₹{_inr(SFT_MUTUAL_FUND_LIMIT)} in the FY.",
         "thresholds": {"fy_aggregate": SFT_MUTUAL_FUND_LIMIT, "inclusive": True}},
        {"id": "SFT012", "group": "SFT", "section": "SFT-012 · Rule 114E", "title": "Immovable property",
         "rule": f"Property purchase / sale payments with one party ≥ ₹{_inr(SFT_PROPERTY_LIMIT)} "
                 "(builder, developer, registrar, stamp duty or sale-deed narrations).",
         "thresholds": {"per_party_fy_aggregate": SFT_PROPERTY_LIMIT, "inclusive": True}},
        {"id": "VDA", "group": "Other", "section": "Sec 115BBH / 194S", "title": "Virtual digital assets (crypto)",
         "rule": "Any VDA trade needs Schedule VDA (30% flat tax) and 1% TDS reconciliation.",
         "thresholds": {"any_amount": 0}},
    ]


_SECTION_TO_CHECK = (
    ("Section 194C", "194C"), ("Section 194H", "194H"), ("Section 194I", "194I"), ("Section 194J", "194J"),
    ("Section 40A(3)", "40A3"), ("Section 269ST", "269ST"), ("SFT-003", "SFT003"), ("SFT-004", "SFT004"),
    ("SFT-005", "SFT005"), ("SFT-007", "SFT007"), ("SFT-009", "SFT009"), ("SFT-012", "SFT012"),
    ("Section 115BBH", "VDA"),
)
_SEV_RANK = {"CRITICAL": 3, "HIGH": 2, "MEDIUM": 1, "LOW": 0}


def _check_id_for(section: str) -> Optional[str]:
    for prefix, cid in _SECTION_TO_CHECK:
        if section.startswith(prefix):
            return cid
    return None


def _unknown_type_note(unknown_type_cash: Dict[str, Dict[str, Any]]) -> str:
    if not unknown_type_cash:
        return ""
    parts = [
        f"{(d.get('account_holder_name') or 'account').strip()} ({d.get('account_number') or d['statement_id']}): "
        f"₹{d['cash_deposits']:,.2f}"
        for d in sorted(unknown_type_cash.values(), key=lambda d: -d["cash_deposits"])
    ]
    return (" Account type not stated on the statement for " + "; ".join(parts)
            + ". The savings limit (₹10,00,000) was applied; if the account is current/CC/OD the limit is "
              "₹50,00,000.")


def _checks_run(fy_label: str, red_flags: List[Dict[str, Any]], has_data: bool,
                provisional: bool, account_types_known: bool) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for check in itr_check_catalogue(fy_label):
        flags = [f for f in red_flags if f.get("check_id") == check["id"]]
        if not has_data:
            status = "no_data"
        elif not flags:
            status = "pass"
        else:
            worst = max(_SEV_RANK.get(str(f.get("severity", "")).upper(), 1) for f in flags)
            status = "verify" if worst < 2 and all(f.get("verify_source") for f in flags) else "flag"
        worst_sev = None
        if flags:
            worst_sev = max((str(f.get("severity", "MEDIUM")).upper() for f in flags),
                            key=lambda s: _SEV_RANK.get(s, 1))
        caveats: List[str] = []
        if status == "pass" and provisional:
            caveats.append("Provisional: uncategorised (suspense) rows are not yet assessed.")
        if check["id"] == "SFT003" and not account_types_known:
            caveats.append("Account type not stated for one or more accounts: those were assessed against the "
                           "savings limit (₹10 lakh); the current-account limit is ₹50 lakh.")
        out.append({
            **check,
            "status": status,
            "flag_count": len(flags),
            "severity": worst_sev,
            "amount": round(sum(float(f.get("amount") or 0) for f in flags), 2),
            "caveats": caveats,
        })
    return out


# ---------------------------------------------------------------------------
# Chapter VI-A deduction bucketing (EDGE-18)
# ---------------------------------------------------------------------------

_DEDUCTION_BUCKETS: Tuple[Tuple[str, str, str], ...] = (
    # key, section, title
    ("sec_80c_elss_lic_pf", "Sec 80C", "ELSS, life insurance, PPF / EPF / SSY / NSC"),
    ("sec_80ccd_nps", "Sec 80CCD(1B)", "NPS / Atal Pension contributions"),
    ("sec_80d_health_insurance", "Sec 80D", "Health insurance premium"),
    ("home_loan_emi_outflow", "Sec 24(b) / 80C", "Home loan EMI (interest u/s 24(b), principal u/s 80C)"),
    ("sec_80e_education_loan", "Sec 80E", "Education loan repayments (interest only is deductible)"),
    ("sec_80g_donations", "Sec 80G", "Donations (eligibility depends on the donee)"),
    ("medical_expenses_verify", "Sec 80D / 80DDB (verify)", "Medical spends: preventive check-up / senior "
                                                            "citizen / specified disease"),
    ("insurance_unclassified", "Review", "Insurance premium of unknown type"),
    ("not_deductible_reviewed", "Not deductible", "Motor / general insurance and non-ELSS mutual funds"),
)


def _deduction_key(tx: NormalizedTransaction) -> Optional[str]:
    if tx.direction != TxDirection.DEBIT or float(tx.debit) <= 0:
        return None
    narr = " ".join(re.sub(r"[^A-Z0-9]+", " ", (tx.raw_narration or "").upper()).split())
    cp = " ".join(re.sub(r"[^A-Z0-9]+", " ", (tx.extracted_counterparty or "").upper()).split())
    text = f"{narr} {cp}"
    cat = tx.category_id
    if _DONATION_RE.search(text):
        return "sec_80g_donations"
    if cat == 35:
        if _HEALTH_INS_RE.search(text):
            return "sec_80d_health_insurance"
        if _MOTOR_INS_RE.search(text):
            return "not_deductible_reviewed"
        if _LIFE_INS_RE.search(text):
            return "sec_80c_elss_lic_pf"
        return "insurance_unclassified"
    if cat == 31:
        return "sec_80c_elss_lic_pf" if _ELSS_RE.search(text) else "not_deductible_reviewed"
    if cat == 36:
        return "sec_80ccd_nps" if _NPS_RE.search(text) else "sec_80c_elss_lic_pf"
    if cat == 23:
        return "home_loan_emi_outflow"
    if cat in (24, 25) and _EDU_LOAN_RE.search(text):
        return "sec_80e_education_loan"
    if cat == 47:
        return "medical_expenses_verify"
    return None


def _deductions(fy_txns: Iterable[NormalizedTransaction]) -> Tuple[Dict[str, float], List[Dict[str, Any]]]:
    sums: Dict[str, float] = defaultdict(float)
    rows: Dict[str, List[NormalizedTransaction]] = defaultdict(list)
    for tx in fy_txns:
        key = _deduction_key(tx)
        if key:
            sums[key] += float(tx.debit)
            rows[key].append(tx)
    buckets: List[Dict[str, Any]] = []
    for key, section, title in _DEDUCTION_BUCKETS:
        if key not in rows:
            continue
        cap = DEDUCTION_CAPS.get(section)
        amt = round(sums[key], 2)
        buckets.append({
            "key": key,
            "section": section,
            "title": title,
            "amount": amt,
            "tx_count": len(rows[key]),
            "statutory_cap": cap,
            "eligible_upto_cap": round(min(amt, cap), 2) if cap is not None else None,
            "transactions": [
                {"date": t.txn_date, "narration": t.raw_narration, "amount": round(float(t.debit), 2),
                 "category_id": t.category_id}
                for t in sorted(rows[key], key=lambda t: (t.txn_date, t.row_index))[:50]
            ],
        })
    return {k: round(v, 2) for k, v in sums.items()}, buckets


# ---------------------------------------------------------------------------
# Investment / property SFT (EDGE-19)
# ---------------------------------------------------------------------------

def _investment_property_flags(fy_txns: List[NormalizedTransaction]) -> List[Dict[str, Any]]:
    flags: List[Dict[str, Any]] = []
    td = [t for t in fy_txns if t.category_id == 33 and t.direction == TxDirection.DEBIT
          and not _RENEWAL_RE.search((t.raw_narration or "").upper())]
    mf = [t for t in fy_txns if t.category_id == 31 and t.direction == TxDirection.DEBIT]
    shares = [t for t in fy_txns if t.category_id == 32 and t.direction == TxDirection.DEBIT]
    for txs, limit, section, title, detail in (
        (td, SFT_TIME_DEPOSIT_LIMIT, "SFT-004 / Rule 114E", "Time Deposits Reach SFT-004 Threshold",
         "Fixed / time deposits (excluding renewals) reportable by the bank in AIS."),
        (shares, SFT_SHARES_LIMIT, "SFT-007/008 / Rule 114E", "High-Value Share Purchases",
         "Share purchases reconcile against AIS (SFT-007/008 and broker reporting)."),
        (mf, SFT_MUTUAL_FUND_LIMIT, "SFT-009 / Rule 114E", "Mutual Fund Purchases Reach SFT-009 Threshold",
         "Mutual fund purchases reportable by the AMC / RTA in AIS."),
    ):
        total = sum(float(t.debit) for t in txs)
        if total >= limit:
            flags.append({
                "section": section, "severity": "MEDIUM", "title": title, "amount": round(total, 2),
                "detail": f"₹{total:,.2f} across {len(txs)} debit(s) (threshold ₹{_inr(limit)}). {detail}",
            })

    prop: Dict[str, Dict[str, Any]] = {}
    for t in fy_txns:
        if t.category_id in _PROPERTY_EXCLUDED_CATEGORIES:
            continue
        text = f"{(t.raw_narration or '').upper()} {(t.extracted_counterparty or '').upper()}"
        if not _PROPERTY_RE.search(text):
            continue
        amt = float(t.debit) if t.direction == TxDirection.DEBIT else float(t.credit)
        side = "purchase" if t.direction == TxDirection.DEBIT else "sale"
        slot = prop.setdefault(f"{side}|{_payee_key(t)}", {"party": _payee_label(t), "side": side, "total": 0.0,
                                                           "dates": []})
        slot["total"] += amt
        slot["dates"].append(t.txn_date)
    hits = [v for v in prop.values() if v["total"] >= SFT_PROPERTY_LIMIT]
    if hits:
        flags.append({
            "section": "SFT-012 / Rule 114E",
            "severity": "MEDIUM",
            "verify_source": True,  # keyword-detected: confirm it is a property transaction
            "title": "Immovable Property Transaction ≥ ₹30,00,000",
            "amount": round(sum(v["total"] for v in hits), 2),
            "detail": f"{len(hits)} property-related counterparty(ies) at or above ₹{_inr(SFT_PROPERTY_LIMIT)}; "
                      "the registrar reports these in AIS. Verify the sale deed and capital-gains / 194-IA TDS.",
            "occurrences": [
                {"party": v["party"], "side": v["side"], "amount": round(v["total"], 2),
                 "first_date": min(v["dates"]), "last_date": max(v["dates"])}
                for v in sorted(hits, key=lambda v: -v["total"])
            ],
        })
    return flags


# ---------------------------------------------------------------------------
# Reconciliation waterfall (EDGE-20)
# ---------------------------------------------------------------------------

_WATERFALL_LESS = (
    ("own_account_transfers", "Less: own-account / contra transfers", (49,)),
    ("cash_deposits", "Less: cash deposits (source shown under cash checks)", (50,)),
    ("loan_disbursals", "Less: loan / credit-line disbursals", (8,)),
    ("capital_infusion", "Less: capital / partner infusion (Sec 68 verification)", (9,)),
    ("deposit_maturities", "Less: FD / investment maturity proceeds", (34,)),
    ("tax_refunds", "Less: tax refunds", (7,)),
)
_WATERFALL_HEADS = (
    ("salary", "Salary (Sec 15-17)", (1,)),
    ("business_professional", "Business / professional receipts (PGBP)", (2, 3, 10)),
    ("house_property", "Rent received (house property)", (6,)),
    ("interest", "Interest income (OS / 80TTA / 80TTB)", (4,)),
    ("dividend_capital_gains", "Dividend / capital-gain payouts", (5,)),
)


def _waterfall(cat_totals: Dict[int, Dict[str, float]], gross: float) -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = [{"step": "gross_credits", "label": "Gross credits in the FY", "kind": "start",
                                    "categories": [], "amount": round(gross, 2), "running_balance": round(gross, 2)}]
    used: Set[int] = set()
    running = gross
    for key, label, cats in _WATERFALL_LESS:
        amt = sum(cat_totals[c]["credit"] for c in cats if c in cat_totals)
        used.update(cats)
        running -= amt
        steps.append({"step": key, "label": label, "kind": "less", "categories": list(cats),
                      "amount": round(amt, 2), "running_balance": round(running, 2)})
    steps.append({"step": "assessable_receipts", "label": "Receipts to explain by income head", "kind": "subtotal",
                  "categories": [], "amount": round(running, 2), "running_balance": round(running, 2)})
    for key, label, cats in _WATERFALL_HEADS:
        amt = sum(cat_totals[c]["credit"] for c in cats if c in cat_totals)
        used.update(cats)
        running -= amt
        steps.append({"step": key, "label": label, "kind": "head", "categories": list(cats),
                      "amount": round(amt, 2), "running_balance": round(running, 2)})
    other_cats = sorted(c for c in cat_totals if c not in used and c != 52 and cat_totals[c]["credit"])
    other = sum(cat_totals[c]["credit"] for c in other_cats)
    running -= other
    steps.append({"step": "other_categorised_credits",
                  "label": "Other categorised credits (refunds, broker / VDA proceeds, reversals)",
                  "kind": "head", "categories": other_cats, "amount": round(other, 2),
                  "running_balance": round(running, 2)})
    unexplained = cat_totals[52]["credit"] if 52 in cat_totals else 0.0
    running -= unexplained
    steps.append({"step": "unexplained_credits", "label": "Unexplained (uncategorised) credits",
                  "kind": "unexplained", "categories": [52], "amount": round(unexplained, 2),
                  "running_balance": round(running, 2)})
    return steps


def generate_itr_compliance_report(
    transactions: List[NormalizedTransaction],
    target_fy: str | None = None,
    *,
    fy: str | None = None,
    statements: Optional[List[BankAccountStatement]] = None,
) -> Dict[str, Any]:
    """
    Compute Financial-Year ITR compliance metrics, Income/Deduction schedules,
    and statutory Indian Income Tax red flags (SFT, Sec 269ST, Sec 40A(3), Sec 115BBH, TDS).

    Only transactions in the selected FY are considered. The FY defaults to the latest FY
    present; an FY that is not present raises ValueError. `statements` supplies account
    types for SFT-003 (savings vs current); without it every account is treated as savings.
    """
    active = [t for t in transactions if not t.is_duplicate_ignored]
    available_fys = sorted({_indian_fy(t.txn_date) for t in active} - {"FY Unknown"})
    requested = fy or target_fy
    if requested:
        selected_fy = normalize_fy(requested)
        if selected_fy not in available_fys:
            raise ValueError(
                f"{selected_fy} has no transactions; available: {', '.join(available_fys) or 'none'}"
            )
    else:
        selected_fy = available_fys[-1] if available_fys else "FY Unknown"
    fy_txns = [t for t in active if _indian_fy(t.txn_date) == selected_fy]
    fy_start = _fy_start_year(selected_fy)

    stmt_by_id = {s.statement_id: s for s in (statements or [])}
    holder_names = {(s.account_holder_name or "").strip().upper() for s in stmt_by_id.values()} - {""}
    holder_names |= {(t.source_holder_name or "").strip().upper() for t in fy_txns if t.source_holder_name}
    holder_keys = {_norm_name(n) for n in holder_names}

    cat_totals: Dict[int, Dict[str, float]] = defaultdict(lambda: {"credit": 0.0, "debit": 0.0, "count": 0})
    cash_dep_savings = 0.0
    cash_dep_current = 0.0
    cash_wdl_current = 0.0
    unknown_type_cash: Dict[str, Dict[str, Any]] = {}
    cash_receipts_by_person_day: Dict[Tuple[str, str], Dict[str, Any]] = {}
    unidentified_cash_by_day: Dict[str, Dict[str, Any]] = {}
    cash_payments_by_party_day: Dict[Tuple[str, str], Dict[str, Any]] = {}
    vda_crypto_total = 0.0
    suspense_count = 0

    for tx in fy_txns:
        cr = float(tx.credit)
        dr = float(tx.debit)
        cat_totals[tx.category_id]["credit"] += cr
        cat_totals[tx.category_id]["debit"] += dr
        cat_totals[tx.category_id]["count"] += 1

        st = stmt_by_id.get(tx.statement_id)
        is_current = _is_current_account(st)
        if _is_cash_receipt(tx):
            if is_current:
                cash_dep_current += cr
            else:
                cash_dep_savings += cr
            if st is not None and not _account_type_known(st):
                slot = unknown_type_cash.setdefault(st.statement_id, {
                    "statement_id": st.statement_id,
                    "account_holder_name": st.account_holder_name,
                    "account_number": st.account_number,
                    "bank_name": st.bank_name,
                    "cash_deposits": 0.0,
                })
                slot["cash_deposits"] += cr
            depositor = (tx.extracted_counterparty or "").strip().upper()
            if depositor and (_norm_name(depositor) in holder_keys or _SELF_RE.search(depositor)):
                pass  # the holder depositing their own cash: Section 269ST does not apply
            elif not depositor or is_system_party_name(depositor):
                slot = unidentified_cash_by_day.setdefault(tx.txn_date, {"total": 0.0, "max_single": 0.0})
                slot["total"] += cr
                slot["max_single"] = max(slot["max_single"], cr)
            else:
                slot = cash_receipts_by_person_day.setdefault(
                    (_payee_key(tx), tx.txn_date),
                    {"label": tx.extracted_counterparty, "total": 0.0, "max_single": 0.0},
                )
                slot["total"] += cr
                slot["max_single"] = max(slot["max_single"], cr)
        if tx.category_id == 51 and is_current:
            cash_wdl_current += dr
        if _is_cash_payment_to_party(tx, holder_names):
            slot = cash_payments_by_party_day.setdefault(
                (_payee_key(tx), tx.txn_date), {"label": tx.extracted_counterparty, "total": 0.0}
            )
            slot["total"] += dr
        if tx.category_id == 37:  # Crypto / VDA
            vda_crypto_total += cr + dr
        if tx.category_id == 52:
            suspense_count += 1

    gross_credits = sum(float(t.credit) for t in fy_txns)
    gross_debits = sum(float(t.debit) for t in fy_txns)
    contra_credits = cat_totals[49]["credit"] + cat_totals[50]["credit"]
    loan_receipts = cat_totals[8]["credit"] + cat_totals[9]["credit"] + cat_totals[34]["credit"]

    taxable_business_receipts = cat_totals[2]["credit"] + cat_totals[3]["credit"] + cat_totals[10]["credit"]
    salary_income = cat_totals[1]["credit"]
    rental_income = cat_totals[6]["credit"]
    interest_income = cat_totals[4]["credit"]
    dividend_income = cat_totals[5]["credit"]

    credit_card_spends = cat_totals[26]["debit"]

    red_flags: List[Dict[str, Any]] = []
    # EDGE-17: an account whose statement does not state its type is assessed against the savings limit, and
    # the assumption is reported per account (it would be the ₹50 lakh current-account limit if it is current).
    for slot in unknown_type_cash.values():
        slot["cash_deposits"] = round(slot["cash_deposits"], 2)
    unknown_type_list = sorted(unknown_type_cash.values(), key=lambda d: -d["cash_deposits"])
    account_types_known = bool(stmt_by_id) and not any(
        not _account_type_known(s) for s in stmt_by_id.values()
    )

    sft_parts: List[str] = []
    sft_amount = 0.0
    if cash_dep_savings >= SFT_CASH_SAVINGS_LIMIT:
        sft_parts.append(f"₹{cash_dep_savings:,.2f} cash deposited in non-current accounts (limit ₹10,00,000)")
        sft_amount += cash_dep_savings
    if cash_dep_current >= SFT_CASH_CURRENT_LIMIT:
        sft_parts.append(f"₹{cash_dep_current:,.2f} cash deposited in current accounts (limit ₹50,00,000)")
        sft_amount += cash_dep_current
    if cash_wdl_current >= SFT_CASH_CURRENT_LIMIT:
        sft_parts.append(f"₹{cash_wdl_current:,.2f} cash withdrawn from current accounts (limit ₹50,00,000)")
        sft_amount += cash_wdl_current
    if sft_parts:
        red_flags.append({
            "section": "SFT-003 / Rule 114E",
            "severity": "HIGH",
            "title": "Cash Transactions Reach SFT-003 Reporting Threshold",
            "amount": round(sft_amount, 2),
            "detail": "; ".join(sft_parts) + ". Reportable by the bank in AIS (Form 61A)."
                      + ("" if stmt_by_id else " Account type unknown: all accounts treated as savings.")
                      + _unknown_type_note(unknown_type_cash),
        })

    st_hits = [
        (d, v) for (_, d), v in cash_receipts_by_person_day.items()
        if v["total"] >= SEC_269ST_LIMIT or v["max_single"] >= SEC_269ST_LIMIT
    ]
    if st_hits:
        red_flags.append({
            "section": "Section 269ST",
            "severity": "CRITICAL",
            "title": "Cash Receipts ≥ ₹2,00,000 From a Single Person in a Day",
            "amount": round(sum(v["total"] for _, v in st_hits), 2),
            "detail": f"{len(st_hits)} person-day(s) with cash receipts ≥ ₹2,00,000 (penalty u/s 271DA equal to "
                      "the amount). The holder's own cash deposits are excluded.",
            "occurrences": [
                {"date": d, "person": v["label"], "amount": round(v["total"], 2)} for d, v in sorted(st_hits)
            ],
        })

    unidentified_hits = [
        (d, v) for d, v in sorted(unidentified_cash_by_day.items())
        if v["total"] >= SEC_269ST_LIMIT or v["max_single"] >= SEC_269ST_LIMIT
    ]
    if unidentified_hits:
        red_flags.append({
            "section": "Section 269ST",
            "severity": "MEDIUM",
            "verify_source": True,
            "title": "Cash Deposits ≥ ₹2,00,000 in a Day With No Identified Depositor",
            "amount": round(sum(v["total"] for _, v in unidentified_hits), 2),
            "detail": f"{len(unidentified_hits)} day(s) with large cash deposits whose depositor is not named. "
                      "Section 269ST applies only if the cash was received from another person; verify the source.",
            "occurrences": [{"date": d, "amount": round(v["total"], 2)} for d, v in unidentified_hits],
        })

    a3_hits = [(d, v) for (_, d), v in cash_payments_by_party_day.items() if v["total"] > SEC_40A3_LIMIT]
    if a3_hits:
        red_flags.append({
            "section": "Section 40A(3)",
            "severity": "MEDIUM",
            "title": "Cash Payments > ₹10,000 to a Single Party in a Day",
            "amount": round(sum(v["total"] for _, v in a3_hits), 2),
            "detail": f"{len(a3_hits)} party-day(s) with cash payments > ₹10,000 (₹35,000 for transporters); "
                      "the expenditure is disallowable unless a Rule 6DD exception applies.",
            "occurrences": [
                {"date": d, "party": v["label"], "amount": round(v["total"], 2)} for d, v in sorted(a3_hits)
            ],
        })

    if vda_crypto_total > 0:
        red_flags.append({
            "section": "Section 115BBH / 194S",
            "severity": "HIGH",
            "title": "Virtual Digital Asset (Crypto) Transactions Detected",
            "amount": round(vda_crypto_total, 2),
            "detail": "Schedule VDA reporting (30% flat tax on gains) and 1% TDS reconciliation required.",
        })

    red_flags.extend(_tds_flags(fy_txns, fy_start))

    if credit_card_spends >= 1_000_000:
        red_flags.append({
            "section": "SFT-005 (Credit Card)",
            "severity": "MEDIUM",
            "title": "Credit Card Settlements ≥ ₹10,00,000",
            "amount": round(credit_card_spends, 2),
            "detail": "High-value credit card payments reconcile against AIS SFT-005.",
        })

    red_flags.extend(_investment_property_flags(fy_txns))
    for flag in red_flags:
        flag.setdefault("check_id", _check_id_for(flag["section"]))

    deduction_sums, deduction_buckets = _deductions(fy_txns)
    waterfall = _waterfall(cat_totals, gross_credits)
    explained = sum(s["amount"] for s in waterfall if s["kind"] in ("less", "head", "unexplained"))

    category_schedule = [
        {
            "category_id": cid,
            "category_name": CATEGORY_BY_ID[cid].name if cid in CATEGORY_BY_ID else f"Category {cid}",
            "group": CATEGORY_BY_ID[cid].group if cid in CATEGORY_BY_ID else "",
            "itr_section": CATEGORY_BY_ID[cid].itr_section if cid in CATEGORY_BY_ID else "",
            "credit": round(vals["credit"], 2),
            "debit": round(vals["debit"], 2),
            "tx_count": int(vals["count"]),
        }
        for cid, vals in sorted(cat_totals.items())
    ]

    return {
        "financial_year": selected_fy,
        "available_fys": available_fys,
        "transaction_count": len(fy_txns),
        "excluded_other_fy_count": len(active) - len(fy_txns),
        "suspense_pending_count": suspense_count,
        "reconciliation": {
            "gross_credits": round(gross_credits, 2),
            "gross_debits": round(gross_debits, 2),
            "excluded_contra_and_cash_deposits": round(contra_credits, 2),
            "non_taxable_loans_and_capital": round(loan_receipts, 2),
            "net_business_and_professional_receipts": round(taxable_business_receipts, 2),
            "unexplained_credits": waterfall[-1]["amount"],
            "reconciles": abs(explained - gross_credits) < 0.01,
        },
        "reconciliation_waterfall": waterfall,
        "income_heads": {
            "salary_sec_15": round(salary_income, 2),
            "business_profession_pgbp": round(taxable_business_receipts, 2),
            "house_property_rent": round(rental_income, 2),
            "interest_income_80tta_80ttb": round(interest_income, 2),
            "dividend_income": round(dividend_income, 2),
        },
        "deduction_candidates": {
            **{key: deduction_sums.get(key, 0.0) for key, _, _ in _DEDUCTION_BUCKETS},
            "advance_tax_and_gst_paid": round(cat_totals[38]["debit"] + cat_totals[39]["debit"], 2),
        },
        "deductions": deduction_buckets,
        "deduction_note": ("Chapter VI-A deductions (other than 80CCD(2)) and the 24(b) self-occupied interest are "
                           "not available under the default new regime (s.115BAC); amounts are bank-side "
                           "candidates only, before statutory caps and proof."),
        "cash_summary": {
            "cash_deposits_non_current": round(cash_dep_savings, 2),
            "cash_deposits_current": round(cash_dep_current, 2),
            "cash_withdrawals_current": round(cash_wdl_current, 2),
            "account_types_known": account_types_known,
            "unknown_type_accounts": unknown_type_list,
            "account_type_note": _unknown_type_note(unknown_type_cash).strip() or None,
        },
        "statutory_red_flags": red_flags,
        "checks_run": _checks_run(selected_fy, red_flags, bool(fy_txns), suspense_count > 0, account_types_known),
        "category_schedule": category_schedule,
    }
