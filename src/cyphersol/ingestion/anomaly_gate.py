"""Step 02: Automatic Anomaly Detection & Data Quality Gate."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cyphersol.models import AnomalyFlag, BankAccountStatement, NormalizedTransaction

# Row flags owned by upstream stages (parser, OCR self-healing, LLM repair). The gate recomputes its own checks
# on every run but must never erase these, otherwise re-running the gate would hide provenance.
PRESERVED_ROW_FLAGS = frozenset(
    {
        AnomalyFlag.DATE_UNPARSEABLE,
        AnomalyFlag.DATE_ORDER_AMBIGUOUS,
        AnomalyFlag.DIRECTION_INFERRED,
        AnomalyFlag.OCR_AUTOCORRECTED,
        AnomalyFlag.LLM_REPAIRED,
        AnomalyFlag.LOW_OCR_CONFIDENCE,
        AnomalyFlag.OUTSIDE_STATEMENT_PERIOD,
    }
)

_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
_NON_ALNUM_RE = re.compile(r"[^A-Z0-9]+")

# (account, date, amount, direction, normalised narration, balance, reference)
DuplicateSignature = Tuple[str, str, str, str, str, str, str]


def normalize_narration_for_dedup(narration: Optional[str]) -> str:
    """Narration key for duplicate detection: upper-case alphanumerics only, so re-exports that differ only in
    case, spacing, wrapping or punctuation (PDF vs CSV of the same account) still match (FR-ING-06.2)."""
    return _NON_ALNUM_RE.sub("", (narration or "").upper())


def _normalize_ref(raw: Optional[str]) -> str:
    ref = _NON_ALNUM_RE.sub("", (raw or "").upper()).lstrip("0")
    return ref


def duplicate_signature(tx: NormalizedTransaction, account_fallback: str = "") -> DuplicateSignature:
    """
    SRS FR-ING-06.2 duplicate key, scoped to one account: (account, date, amount, direction, normalised narration,
    running balance) plus the bank reference (UTR, else cheque/ref no.) as the last element. Two rows match when the
    first six elements are equal and their references are equal or at least one of them is blank.
    """
    amount = tx.credit if tx.credit > 0 else tx.debit
    direction = tx.direction.value if hasattr(tx.direction, "value") else str(tx.direction or "")
    return (
        tx.source_account_number or account_fallback or "",
        tx.txn_date or "",
        f"{amount:.2f}",
        direction,
        normalize_narration_for_dedup(tx.raw_narration),
        f"{tx.running_balance:.2f}",
        _normalize_ref(tx.utr_number or tx.cheque_or_ref_no),
    )


class _DuplicateIndex:
    """Signatures seen so far; base key -> list of (reference, tx_id)."""

    def __init__(self) -> None:
        self._seen: Dict[Tuple[str, ...], List[Tuple[str, str]]] = {}

    def match(self, sig: DuplicateSignature) -> Optional[str]:
        """tx_id of an earlier row this signature duplicates, else None."""
        for ref, tx_id in self._seen.get(sig[:-1], ()):
            if not ref or not sig[-1] or ref == sig[-1]:
                return tx_id
        return None

    def add(self, sig: DuplicateSignature, tx_id: str) -> None:
        self._seen.setdefault(sig[:-1], []).append((sig[-1], tx_id))


def _account_for(tx: NormalizedTransaction, account_by_statement: Optional[Dict[str, str]]) -> str:
    if tx.source_account_number:
        return tx.source_account_number
    if account_by_statement and tx.statement_id in account_by_statement:
        return account_by_statement[tx.statement_id]
    # Unknown account: only rows of the same statement can be compared with each other
    return f"statement:{tx.statement_id}"


def find_duplicate_groups(
    transactions: Iterable[NormalizedTransaction],
    account_by_statement: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Group duplicate rows across any number of statements (bulk batch or whole case) by the SRS duplicate key.
    The kept row of each group is its first row that is not already ignored (else its first row), in input order.
    Returns [{"keep_tx_id", "duplicate_tx_ids": [...], "statement_ids": [...], "account_number", "txn_date",
    "amount", "direction"}] in order of first appearance.
    """
    index = _DuplicateIndex()
    groups: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    members: Dict[str, List[NormalizedTransaction]] = {}
    for tx in transactions:
        sig = duplicate_signature(tx, _account_for(tx, account_by_statement))
        first = index.match(sig)
        if first is None:
            index.add(sig, tx.tx_id)
            members[tx.tx_id] = [tx]
            groups[tx.tx_id] = {"account_number": sig[0], "txn_date": sig[1], "amount": sig[2], "direction": sig[3]}
            continue
        if len(members[first]) == 1:
            order.append(first)
        members[first].append(tx)
    out: List[Dict[str, Any]] = []
    for first in order:
        rows = members[first]
        keep = next((t for t in rows if not t.is_duplicate_ignored), rows[0])
        out.append(
            {
                "keep_tx_id": keep.tx_id,
                "duplicate_tx_ids": [t.tx_id for t in rows if t is not keep],
                "statement_ids": list(dict.fromkeys(t.statement_id for t in rows)),
                **groups[first],
            }
        )
    return out


def mark_bulk_duplicates_ignored(
    transactions: List[NormalizedTransaction],
    account_by_statement: Optional[Dict[str, str]] = None,
) -> List[str]:
    """
    One-click deduplication (FR-ING-06.2): sets `is_duplicate_ignored` on every non-kept row of each duplicate
    group and makes sure it carries DUPLICATE_SUSPECT. Idempotent. Returns the tx_ids whose state changed, so the
    caller can persist exactly those rows and audit the action.
    """
    by_id = {t.tx_id: t for t in transactions}
    changed: List[str] = []
    for grp in find_duplicate_groups(transactions, account_by_statement):
        for tx_id in grp["duplicate_tx_ids"]:
            tx = by_id[tx_id]
            dirty = False
            if not tx.is_duplicate_ignored:
                tx.is_duplicate_ignored = True
                dirty = True
            if AnomalyFlag.DUPLICATE_SUSPECT not in tx.anomaly_flags:
                tx.anomaly_flags = [f for f in tx.anomaly_flags if f != AnomalyFlag.VALID] + [AnomalyFlag.DUPLICATE_SUSPECT]
                dirty = True
            if dirty:
                changed.append(tx_id)
    return changed


def _count_balance_matches(seq: List[NormalizedTransaction], tol: Decimal) -> int:
    """Count consecutive transactions satisfying running_balance[t] == balance[t-1] + credit[t] - debit[t]."""
    hits = 0
    for i in range(1, len(seq)):
        expected = (seq[i - 1].running_balance + seq[i].credit - seq[i].debit).quantize(Decimal("0.01"))
        if abs(seq[i].running_balance - expected) <= tol:
            hits += 1
    return hits


def _derived_opening(tx: NormalizedTransaction) -> Decimal:
    return (tx.running_balance - tx.credit + tx.debit).quantize(Decimal("0.01"))


def missing_statement_metadata(statement: BankAccountStatement) -> List[str]:
    """Statement-level metadata gaps (FR-ING-06.4): reported once per statement, never stamped on every row."""
    missing: List[str] = []
    if not statement.bank_code or statement.bank_code.upper() == "UNKNOWN":
        missing.append("bank_code")
    if not statement.ifsc_code or not _IFSC_RE.match(statement.ifsc_code.upper()):
        missing.append("ifsc_code")
    if not statement.account_number or statement.account_number == "UNKNOWN_AC":
        missing.append("account_number")
    holder = (statement.account_holder_name or "").strip()
    if not holder or holder == "Unknown Holder" or holder.startswith("Holder "):
        missing.append("account_holder_name")
    if not statement.period_start or not statement.period_end:
        missing.append("period")
    if not (statement.account_type or "").strip() or (statement.account_type or "").strip().lower() == "unknown":
        missing.append("account_type")
    return missing


def run_anomaly_gate(
    statement: BankAccountStatement,
    transactions: List[NormalizedTransaction],
    existing_case_txns: List[NormalizedTransaction] | None = None,
    balance_tolerance: Decimal = Decimal("0.05"),
) -> Tuple[BankAccountStatement, List[NormalizedTransaction], Dict[str, Any]]:
    """
    Validate chronological order, running balance conservation (including row 0 against the statement opening
    balance and the last row against its closing balance), duplicates, zero/dual amounts and metadata.

    Row flags set upstream (see PRESERVED_ROW_FLAGS) are kept; VALID is set only on rows with no flag at all.
    Returns (updated_statement, validated_transactions, summary). Summary keys: total, valid, balance_mismatch,
    duplicate_suspect, date_out_of_order, zero_or_dual_amount, date_unparseable, date_order_ambiguous,
    direction_inferred, ocr_autocorrected, llm_repaired, outside_statement_period, opening_balance_mismatch, closing_balance_mismatch,
    balance_check_skipped, missing_metadata (list of field names) and statement_flags (list of flag values).
    """
    missing_meta = missing_statement_metadata(statement)
    statement_flags: List[str] = []
    if missing_meta:
        statement_flags.append(AnomalyFlag.MISSING_METADATA.value)
    if statement.is_ocr_derived:
        statement_flags.append(AnomalyFlag.LOW_OCR_CONFIDENCE.value)

    summary: Dict[str, Any] = {
        "total": 0,
        "valid": 0,
        "balance_mismatch": 0,
        "duplicate_suspect": 0,
        "date_out_of_order": 0,
        "zero_or_dual_amount": 0,
        "date_unparseable": 0,
        "date_order_ambiguous": 0,
        "direction_inferred": 0,
        "ocr_autocorrected": 0,
        "llm_repaired": 0,
        "outside_statement_period": 0,
        "opening_balance_mismatch": 0,
        "closing_balance_mismatch": 0,
        "balance_check_skipped": False,
        "missing_metadata": missing_meta,
        "statement_flags": statement_flags,
    }
    if not transactions:
        return statement, transactions, summary

    # Auto-detect descending statement order (multi-day or same-day reverse-chronological) and normalize ascending
    if len(transactions) >= 2:
        fwd_hits = _count_balance_matches(transactions, balance_tolerance)
        rev_seq = list(reversed(transactions))
        rev_hits = _count_balance_matches(rev_seq, balance_tolerance)
        is_desc_dates = transactions[0].txn_date > transactions[-1].txn_date
        same_day_desc_bal = transactions[0].txn_date == transactions[-1].txn_date and rev_hits > fwd_hits
        if is_desc_dates or same_day_desc_bal:
            # Opening/closing that were derived from the old end rows are re-derived; stated values are kept
            opening_was_derived = statement.opening_balance == _derived_opening(transactions[0])
            closing_was_derived = statement.closing_balance == transactions[-1].running_balance
            transactions = rev_seq
            for idx, tx in enumerate(transactions):
                tx.row_index = idx
            if opening_was_derived:
                statement.opening_balance = _derived_opening(transactions[0])
            if closing_was_derived:
                statement.closing_balance = transactions[-1].running_balance

    # Duplicate index over the rest of the case (other files of this bulk batch and earlier uploads)
    dup_index = _DuplicateIndex()
    if existing_case_txns:
        own_ids = {t.tx_id for t in transactions}
        for etx in existing_case_txns:
            if etx.tx_id in own_ids:
                continue
            dup_index.add(duplicate_signature(etx, ""), etx.tx_id)

    # A statement without a balance column (every running balance zero) cannot be continuity-checked
    check_balances = any(tx.running_balance != 0 for tx in transactions)
    summary["balance_check_skipped"] = not check_balances

    prev_balance: Decimal | None = statement.opening_balance if check_balances else None
    prev_date: str | None = None
    last_idx = len(transactions) - 1

    for idx, tx in enumerate(transactions):
        flags: List[AnomalyFlag] = [f for f in tx.anomaly_flags if f in PRESERVED_ROW_FLAGS]

        # 1. Running balance math check (row 0 against the statement opening balance)
        if check_balances:
            row_mismatch = False
            if prev_balance is not None:
                expected_bal = (prev_balance + tx.credit - tx.debit).quantize(Decimal("0.01"))
                if abs(tx.running_balance - expected_bal) > balance_tolerance:
                    row_mismatch = True
                    if idx == 0:
                        summary["opening_balance_mismatch"] = 1
            if idx == last_idx and abs(tx.running_balance - statement.closing_balance) > balance_tolerance:
                row_mismatch = True
                summary["closing_balance_mismatch"] = 1
            if row_mismatch:
                flags.append(AnomalyFlag.BALANCE_MISMATCH)
                summary["balance_mismatch"] += 1
            prev_balance = tx.running_balance

        # 2. Chronological date inversion check
        if prev_date is not None and tx.txn_date < prev_date:
            flags.append(AnomalyFlag.DATE_OUT_OF_ORDER)
            summary["date_out_of_order"] += 1
        prev_date = tx.txn_date

        # 3. Duplicate transaction check (SRS key, see duplicate_signature)
        sig = duplicate_signature(tx, statement.account_number)
        if dup_index.match(sig) is not None:
            flags.append(AnomalyFlag.DUPLICATE_SUSPECT)
            summary["duplicate_suspect"] += 1
        else:
            dup_index.add(sig, tx.tx_id)

        # 4. Zero or dual (both debit and credit) amounts
        if (tx.debit == 0 and tx.credit == 0) or (tx.debit > 0 and tx.credit > 0):
            flags.append(AnomalyFlag.ZERO_OR_DUAL_AMOUNT)
            summary["zero_or_dual_amount"] += 1

        # 5. Row-level metadata: empty narration only (statement gaps are reported in `missing_metadata`)
        if not tx.raw_narration.strip():
            flags.append(AnomalyFlag.MISSING_METADATA)

        for flag, key in (
            (AnomalyFlag.DATE_UNPARSEABLE, "date_unparseable"),
            (AnomalyFlag.DATE_ORDER_AMBIGUOUS, "date_order_ambiguous"),
            (AnomalyFlag.DIRECTION_INFERRED, "direction_inferred"),
            (AnomalyFlag.OCR_AUTOCORRECTED, "ocr_autocorrected"),
            (AnomalyFlag.LLM_REPAIRED, "llm_repaired"),
            (AnomalyFlag.OUTSIDE_STATEMENT_PERIOD, "outside_statement_period"),
        ):
            if flag in flags:
                summary[key] += 1

        if not flags:
            flags.append(AnomalyFlag.VALID)
            summary["valid"] += 1

        tx.anomaly_flags = list(dict.fromkeys(flags))
        tx.row_index = idx

    statement.balance_mismatch_count = summary["balance_mismatch"]
    statement.duplicate_suspect_count = summary["duplicate_suspect"]
    statement.total_transactions = len(transactions)
    summary["total"] = len(transactions)
    return statement, transactions, summary
