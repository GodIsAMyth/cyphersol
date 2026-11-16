"""CypherEdge Tally Prime / ERP 9 XML builder, ledger mapper, and localhost:9000 Upload Utility."""

from __future__ import annotations

import datetime as dt
import hashlib
import http.client
import re
import urllib.error
import urllib.request
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from xml.sax.saxutils import escape, quoteattr

from cyphersol.models import (
    BankAccountStatement,
    NormalizedTransaction,
    TxDirection,
    VoucherType,
)
from cyphersol.taxonomy import CATEGORY_BY_ID

# ---------------------------------------------------------------------------
# Tally master data
# ---------------------------------------------------------------------------

# The 28 groups every Tally Prime / ERP 9 company is created with (15 primary + 13 sub-groups).
# A LEDGER whose PARENT is not in this set (or emitted as a GROUP master in the same file) fails import.
PREDEFINED_TALLY_GROUPS = frozenset({
    # Primary groups
    "Branch / Divisions", "Capital Account", "Current Assets", "Current Liabilities",
    "Direct Expenses", "Direct Incomes", "Fixed Assets", "Indirect Expenses", "Indirect Incomes",
    "Investments", "Loans (Liability)", "Misc. Expenses (ASSET)", "Purchase Accounts",
    "Sales Accounts", "Suspense A/c",
    # Sub-groups
    "Bank Accounts", "Bank OD A/c", "Cash-in-Hand", "Deposits (Asset)", "Duties & Taxes",
    "Loans & Advances (Asset)", "Provisions", "Reserves & Surplus", "Secured Loans",
    "Stock-in-Hand", "Sundry Creditors", "Sundry Debtors", "Unsecured Loans",
})
_PREDEFINED_BY_FOLD = {g.casefold(): g for g in PREDEFINED_TALLY_GROUPS}

# Ledgers that exist in every new Tally company and must not be re-created.
_PREDEFINED_LEDGERS = frozenset({"Cash", "Profit & Loss A/c"})

CASH_LEDGER = "Cash"
# Must differ from the predefined *group* "Suspense A/c": Tally names share one namespace (EDGE-04).
SUSPENSE_LEDGER_NAME = "Unreconciled Bank Entries"
UNMATCHED_TRANSFER_LEDGER = "Own-Account Transfers (Unmatched)"

CONTRA_CATEGORIES = (49, 50, 51)
CASH_CATEGORIES = (50, 51)
_MAX_NAME_LEN = 64
_MIRROR_WINDOW_DAYS = 2
# Legs that name each other's account number are the same transfer even when settlement took longer (a
# Friday NEFT credited on Tuesday), so they pair within a wider window (EDGE-05).
_MIRROR_REF_WINDOW_DAYS = 7

# XML 1.0 forbids C0 controls other than TAB/LF/CR, lone surrogates and U+FFFE/U+FFFF.
_XML_ILLEGAL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_WS_RE = re.compile(r"\s+")


def sanitize_xml_text(value: Any) -> str:
    """Return `value` as text with every XML-1.0-illegal character removed."""
    return _XML_ILLEGAL_RE.sub("", "" if value is None else str(value))


def _txt(value: Any) -> str:
    """Escaped element text content."""
    return escape(sanitize_xml_text(value))


def _attr(value: Any) -> str:
    """Quoted, fully escaped attribute value (including the surrounding quotes)."""
    return quoteattr(sanitize_xml_text(value))


def _clean_name(value: Any) -> str:
    name = _WS_RE.sub(" ", sanitize_xml_text(value)).strip()
    return name[:_MAX_NAME_LEN].strip()


def _ledger_name(value: Any) -> str:
    """Clean a ledger name; names longer than Tally's limit keep a short content-hash suffix so two
    different long names that share a 64-character prefix never merge into one ledger (EDGE-29)."""
    name = _WS_RE.sub(" ", sanitize_xml_text(value)).strip()
    if len(name) <= _MAX_NAME_LEN:
        return name
    suffix = hashlib.sha1(name.casefold().encode("utf-8")).hexdigest()[:6]
    return f"{name[: _MAX_NAME_LEN - 8].rstrip()} ~{suffix}"


# ---------------------------------------------------------------------------
# Group / ledger resolution
# ---------------------------------------------------------------------------

_GROUP_KEYWORD_PARENTS: Tuple[Tuple[Tuple[str, ...], str], ...] = (
    (("DRAWING", "PERSONAL", "CAPITAL", "PARTNER", "PROPRIETOR"), "Capital Account"),
    (("SUSPENSE", "UNRECONCILED", "UNCATEGOR"), "Suspense A/c"),
    (("TAX", "DUTY", "DUTIES", "GST", "TDS"), "Duties & Taxes"),
    (("DEPOSIT",), "Deposits (Asset)"),
    (("INVESTMENT",), "Investments"),
    (("SECURED LOAN",), "Secured Loans"),
    (("LOAN", "BORROWING"), "Loans (Liability)"),
    (("PURCHASE",), "Purchase Accounts"),
    (("SALES",), "Sales Accounts"),
    (("CREDITOR", "SUPPLIER", "VENDOR"), "Sundry Creditors"),
    (("DEBTOR", "CUSTOMER"), "Sundry Debtors"),
    (("INCOME", "RECEIPT", "REVENUE"), "Indirect Incomes"),
    (("EXPENSE", "CHARGE", "FEE", "COST"), "Indirect Expenses"),
)


def resolve_parent_group(
    group_name: Optional[str],
    typical_direction: str = "BOTH",
    custom_groups: Optional[Dict[str, str]] = None,
) -> Tuple[str, Optional[str]]:
    """
    Validate a ledger parent against Tally's predefined groups.

    Returns (group_name, None) when it is predefined (spelling normalised), or
    (custom_group_name, predefined_parent) when a GROUP master must be emitted so
    the import never references a missing group. `custom_groups` (name -> predefined parent,
    from a TallyLedgerMap) is consulted before the keyword heuristics.
    """
    name = _clean_name(group_name or "")
    if not name:
        return "Suspense A/c", None
    predefined = _PREDEFINED_BY_FOLD.get(name.casefold())
    if predefined:
        return predefined, None
    for cname, cparent in (custom_groups or {}).items():
        if cname.casefold() == name.casefold():
            return cname, _PREDEFINED_BY_FOLD.get(cparent.casefold(), cparent)
    upper = name.upper()
    for keywords, parent in _GROUP_KEYWORD_PARENTS:
        if any(k in upper for k in keywords):
            return name, parent
    direction = (typical_direction or "BOTH").upper()
    if direction == "DR":
        return name, "Indirect Expenses"
    if direction == "CR":
        return name, "Indirect Incomes"
    return name, "Suspense A/c"


# ---------------------------------------------------------------------------
# Configurable ledger / group map (EDGE-10)
# ---------------------------------------------------------------------------

_STRUCTURAL_CATEGORIES = (49, 50, 51)   # contra legs: their counterpart is Cash / a bank ledger, never mapped
_VOUCHER_PREFIX_RE = re.compile(r"^[A-Za-z0-9/_-]{1,12}$")
_CP_KEY_RE = re.compile(r"[^A-Z0-9&]+")
_MAP_KEYS = frozenset({
    "version", "category_groups", "category_ledgers", "counterparty_ledgers", "custom_groups",
    "company_name", "use_trade_vouchers", "voucher_prefix", "sales_categories", "purchase_categories",
})
LEDGER_MAP_VERSION = 1
_MAX_COMPANY_LEN = 120
_MAX_MAP_ENTRIES = 5000


def counterparty_key(name: Optional[str]) -> str:
    """Normalised lookup key for counterparty ledger overrides ("M/s. Airtel " -> "M S AIRTEL")."""
    return " ".join(_CP_KEY_RE.sub(" ", (name or "").upper()).split())


def _default_category_groups() -> Dict[int, str]:
    return {cid: spec.tally_parent_group for cid, spec in sorted(CATEGORY_BY_ID.items())}


@dataclass
class TallyLedgerMap:
    """
    CA-configurable 52-category <-> Tally group map plus ledger overrides (FR-EDGE-01, EDGE-10).

    JSON shape (`to_dict` / `from_dict`; a partial dict is overlaid on the defaults):
      {
        "version": 1,
        "category_groups": {"16": "Indirect Expenses", ...},       # all 52 categories
        "category_ledgers": {"16": "Telephone & Internet Expenses"},  # optional fixed ledger per category
        "counterparty_ledgers": {"AIRTEL": {"ledger": "...", "group": "Indirect Expenses" | null}},
        "custom_groups": {"Software Expenses": "Indirect Expenses"},  # name -> predefined parent
        "company_name": "Acme Traders" | null,
        "use_trade_vouchers": false,
        "voucher_prefix": "CS",
        "sales_categories": [2],
        "purchase_categories": [11, 12]
      }
    """

    category_groups: Dict[int, str] = field(default_factory=_default_category_groups)
    category_ledgers: Dict[int, str] = field(default_factory=dict)
    counterparty_ledgers: Dict[str, Dict[str, Optional[str]]] = field(default_factory=dict)
    custom_groups: Dict[str, str] = field(default_factory=dict)
    company_name: Optional[str] = None
    use_trade_vouchers: bool = False
    voucher_prefix: str = "CS"
    sales_categories: Tuple[int, ...] = (2,)
    purchase_categories: Tuple[int, ...] = (11, 12)

    # -- construction --------------------------------------------------------------------------
    @classmethod
    def defaults(cls) -> "TallyLedgerMap":
        return cls()

    @classmethod
    def validate_dict(cls, data: Any) -> List[str]:
        """Every problem with `data` as a list of messages (empty when valid)."""
        try:
            cls._parse(data)
        except _MapErrors as exc:
            return list(exc.errors)
        return []

    @classmethod
    def from_dict(cls, data: Any) -> "TallyLedgerMap":
        """Overlay `data` on the defaults; raises ValueError listing every invalid entry."""
        try:
            return cls._parse(data)
        except _MapErrors as exc:
            raise ValueError("Invalid Tally ledger map: " + "; ".join(exc.errors)) from None

    @classmethod
    def coerce(cls, value: "TallyLedgerMap | Dict[str, Any] | None") -> "TallyLedgerMap":
        if value is None:
            return cls.defaults()
        if isinstance(value, TallyLedgerMap):
            return cls.from_dict(value.to_dict())
        return cls.from_dict(value)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": LEDGER_MAP_VERSION,
            "category_groups": {str(k): v for k, v in sorted(self.category_groups.items())},
            "category_ledgers": {str(k): v for k, v in sorted(self.category_ledgers.items())},
            "counterparty_ledgers": {
                k: {"ledger": v.get("ledger"), "group": v.get("group")}
                for k, v in sorted(self.counterparty_ledgers.items())
            },
            "custom_groups": dict(sorted(self.custom_groups.items())),
            "company_name": self.company_name,
            "use_trade_vouchers": bool(self.use_trade_vouchers),
            "voucher_prefix": self.voucher_prefix,
            "sales_categories": list(self.sales_categories),
            "purchase_categories": list(self.purchase_categories),
        }

    # -- lookups -------------------------------------------------------------------------------
    def group_for(self, category_id: int) -> str:
        return self.category_groups.get(category_id) or CATEGORY_BY_ID.get(category_id, CATEGORY_BY_ID[52]).tally_parent_group

    def counterparty_override(self, *names: Optional[str]) -> Optional[Dict[str, Optional[str]]]:
        for name in names:
            key = counterparty_key(name)
            if key and key in self.counterparty_ledgers:
                return self.counterparty_ledgers[key]
        return None

    # -- parsing -------------------------------------------------------------------------------
    @classmethod
    def _parse(cls, data: Any) -> "TallyLedgerMap":
        errors: List[str] = []
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise _MapErrors(["ledger map must be a JSON object"])
        for key in data:
            if key not in _MAP_KEYS:
                errors.append(f"unknown key {key!r}")
        version = data.get("version", LEDGER_MAP_VERSION)
        if version != LEDGER_MAP_VERSION:
            errors.append(f"unsupported version {version!r} (expected {LEDGER_MAP_VERSION})")
        out = cls.defaults()

        def _cat_id(raw: Any, where: str) -> Optional[int]:
            try:
                cid = int(str(raw).strip())
            except (TypeError, ValueError):
                errors.append(f"{where}: {raw!r} is not a category id")
                return None
            if cid not in CATEGORY_BY_ID:
                errors.append(f"{where}: unknown category {cid}")
                return None
            return cid

        def _mapping(key: str) -> Dict[Any, Any]:
            val = data.get(key, {})
            if val is None:
                return {}
            if not isinstance(val, dict):
                errors.append(f"{key} must be an object")
                return {}
            if len(val) > _MAX_MAP_ENTRIES:
                errors.append(f"{key} has more than {_MAX_MAP_ENTRIES} entries")
                return {}
            return val

        # custom groups first: other entries may reference them
        for raw_name, raw_parent in _mapping("custom_groups").items():
            name = _clean_name(raw_name) if isinstance(raw_name, str) else ""
            if not name or len(_WS_RE.sub(" ", str(raw_name)).strip()) > _MAX_NAME_LEN:
                errors.append(f"custom_groups: invalid group name {raw_name!r} (1-{_MAX_NAME_LEN} characters)")
                continue
            if name.casefold() in _PREDEFINED_BY_FOLD:
                errors.append(f"custom_groups: {name!r} is already a predefined Tally group")
                continue
            parent = _PREDEFINED_BY_FOLD.get(str(raw_parent or "").strip().casefold())
            if parent is None:
                errors.append(f"custom_groups: parent {raw_parent!r} of {name!r} is not a predefined Tally group")
                continue
            out.custom_groups[name] = parent
        group_names = {g.casefold(): g for g in PREDEFINED_TALLY_GROUPS}
        group_names.update({g.casefold(): g for g in out.custom_groups})

        def _group(raw: Any, where: str) -> Optional[str]:
            g = group_names.get(str(raw or "").strip().casefold())
            if g is None:
                errors.append(f"{where}: group {raw!r} is neither predefined nor declared in custom_groups")
            return g

        def _ledger(raw: Any, where: str) -> Optional[str]:
            if not isinstance(raw, str) or not _clean_name(raw):
                errors.append(f"{where}: ledger name must be a non-empty string")
                return None
            name = _WS_RE.sub(" ", sanitize_xml_text(raw)).strip()
            if len(name) > _MAX_NAME_LEN:
                errors.append(f"{where}: ledger name longer than {_MAX_NAME_LEN} characters")
                return None
            if name.casefold() in group_names:
                errors.append(f"{where}: ledger {name!r} clashes with a group of the same name")
                return None
            if name.casefold() in {n.casefold() for n in (*_PREDEFINED_LEDGERS, SUSPENSE_LEDGER_NAME,
                                                          UNMATCHED_TRANSFER_LEDGER)}:
                errors.append(f"{where}: ledger {name!r} is reserved")
                return None
            return name

        for raw_cid, raw_group in _mapping("category_groups").items():
            cid = _cat_id(raw_cid, "category_groups")
            if cid is None:
                continue
            g = _group(raw_group, f"category_groups[{cid}]")
            if g is None:
                continue
            if cid in _STRUCTURAL_CATEGORIES and g != CATEGORY_BY_ID[cid].tally_parent_group:
                errors.append(f"category_groups[{cid}]: contra category groups are fixed")
                continue
            out.category_groups[cid] = g

        for raw_cid, raw_ledger in _mapping("category_ledgers").items():
            cid = _cat_id(raw_cid, "category_ledgers")
            if cid is None:
                continue
            if cid in _STRUCTURAL_CATEGORIES or cid == 52:
                errors.append(f"category_ledgers[{cid}]: contra / suspense ledgers cannot be overridden")
                continue
            name = _ledger(raw_ledger, f"category_ledgers[{cid}]")
            if name:
                out.category_ledgers[cid] = name

        for raw_cp, raw_val in _mapping("counterparty_ledgers").items():
            key = counterparty_key(raw_cp if isinstance(raw_cp, str) else "")
            where = f"counterparty_ledgers[{raw_cp!r}]"
            if not key:
                errors.append(f"{where}: empty counterparty")
                continue
            if isinstance(raw_val, str):
                raw_val = {"ledger": raw_val}
            if not isinstance(raw_val, dict) or set(raw_val) - {"ledger", "group"}:
                errors.append(f"{where}: must be a ledger name or {{ledger, group}}")
                continue
            name = _ledger(raw_val.get("ledger"), where)
            group = None
            if raw_val.get("group") not in (None, ""):
                group = _group(raw_val.get("group"), where)
                if group is None:
                    continue
            if name:
                out.counterparty_ledgers[key] = {"ledger": name, "group": group}

        if "company_name" in data:
            cn = data["company_name"]
            if cn in (None, ""):
                out.company_name = None
            elif not isinstance(cn, str) or len(cn.strip()) > _MAX_COMPANY_LEN:
                errors.append(f"company_name must be a string of at most {_MAX_COMPANY_LEN} characters")
            else:
                out.company_name = sanitize_xml_text(cn).strip() or None
        if "use_trade_vouchers" in data:
            if not isinstance(data["use_trade_vouchers"], bool):
                errors.append("use_trade_vouchers must be true or false")
            else:
                out.use_trade_vouchers = data["use_trade_vouchers"]
        if "voucher_prefix" in data:
            vp = data["voucher_prefix"]
            if not isinstance(vp, str) or not _VOUCHER_PREFIX_RE.match(vp):
                errors.append("voucher_prefix must be 1-12 letters, digits, '/', '_' or '-'")
            else:
                out.voucher_prefix = vp

        for key, allowed_dirs, label in (("sales_categories", ("CR", "BOTH"), "Sales"),
                                         ("purchase_categories", ("DR", "BOTH"), "Purchase")):
            if key not in data:
                continue
            raw = data[key]
            if not isinstance(raw, (list, tuple)):
                errors.append(f"{key} must be a list of category ids")
                continue
            ids: List[int] = []
            for item in raw:
                cid = _cat_id(item, key)
                if cid is None:
                    continue
                spec = CATEGORY_BY_ID[cid]
                if cid in _STRUCTURAL_CATEGORIES or cid == 52 or spec.typical_direction not in allowed_dirs:
                    errors.append(f"{key}: category {cid} cannot carry {label} vouchers")
                    continue
                if cid not in ids:
                    ids.append(cid)
            setattr(out, key, tuple(ids))

        if errors:
            raise _MapErrors(errors)
        return out


class _MapErrors(Exception):
    def __init__(self, errors: List[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _voucher_for(
    tx: NormalizedTransaction,
    use_trade_vouchers: bool,
    sales_categories: Sequence[int] = (2,),
    purchase_categories: Sequence[int] = (11, 12),
) -> VoucherType:
    """Voucher type by direction (EDGE-09): Contra for 49/50/51, Payment for DR, Receipt for CR;
    Sales / Purchase for the configured trade categories when trade vouchers are enabled."""
    if tx.category_id in CONTRA_CATEGORIES:
        return VoucherType.CONTRA
    is_credit = tx.direction == TxDirection.CREDIT
    if use_trade_vouchers:
        if is_credit and tx.category_id in sales_categories:
            return VoucherType.SALES
        if not is_credit and tx.category_id in purchase_categories:
            return VoucherType.PURCHASE
    return VoucherType.RECEIPT if is_credit else VoucherType.PAYMENT


def resolve_tally_mapping(
    tx: NormalizedTransaction,
    use_trade_vouchers: bool = False,
    ledger_map: Optional[TallyLedgerMap] = None,
) -> Tuple[VoucherType, str, str]:
    """
    Return (voucher_type, ledger_name, parent_group) for a NormalizedTransaction.

    `parent_group` is the configured Tally group as-is; callers must pass it through
    `resolve_parent_group` before emitting masters. Category 49 transfers are resolved against
    uploaded statements in `generate_tally_xml`; here they map to the unmatched own-account
    clearing ledger. Ledger precedence: counterparty override > category ledger > the row's
    `tally_ledger_name` > extracted counterparty > category name.
    """
    lm = ledger_map or _DEFAULT_MAP
    spec = CATEGORY_BY_ID.get(tx.category_id, CATEGORY_BY_ID[52])
    voucher = _voucher_for(tx, use_trade_vouchers, lm.sales_categories, lm.purchase_categories)

    if tx.category_id in CASH_CATEGORIES:
        return voucher, CASH_LEDGER, "Cash-in-Hand"
    if tx.category_id == 49:
        return VoucherType.PAYMENT if tx.direction == TxDirection.DEBIT else VoucherType.RECEIPT, \
            UNMATCHED_TRANSFER_LEDGER, "Suspense A/c"
    if tx.category_id == 52:
        return voucher, SUSPENSE_LEDGER_NAME, "Suspense A/c"

    group = lm.group_for(spec.category_id)
    override = lm.counterparty_override(tx.extracted_counterparty, tx.tally_ledger_name)
    if override:
        return voucher, override["ledger"] or "", override.get("group") or group
    if spec.category_id in lm.category_ledgers:
        return voucher, lm.category_ledgers[spec.category_id], group
    ledger_name = _ledger_name(tx.tally_ledger_name or tx.extracted_counterparty or spec.name) or _clean_name(spec.name)
    return voucher, ledger_name, group


_DEFAULT_MAP = TallyLedgerMap()


# ---------------------------------------------------------------------------
# GUIDs, accounts, mirror legs
# ---------------------------------------------------------------------------

_GUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:cyphersol:tally:voucher")
_NARR_NORM_RE = re.compile(r"[^A-Z0-9]+")


def _amount(tx: NormalizedTransaction) -> Decimal:
    return Decimal(tx.credit if tx.direction == TxDirection.CREDIT else tx.debit)


def _account_key(tx: NormalizedTransaction, stmt: Optional[BankAccountStatement]) -> str:
    if stmt is not None:
        return f"{stmt.bank_code}:{stmt.account_number}"
    return tx.source_account_number or tx.statement_id


def voucher_guid(tx: NormalizedTransaction, account_key: str) -> str:
    """
    Deterministic GUID from transaction content (EDGE-07): stable across re-ingestion of the
    same statement, unlike the per-ingest uuid4 `tx_id`.
    """
    narration = _NARR_NORM_RE.sub(" ", (tx.raw_narration or "").upper()).strip()
    payload = "|".join([
        account_key,
        tx.txn_date,
        f"{_amount(tx).quantize(Decimal('0.01'))}",
        tx.direction.value,
        narration,
        (tx.utr_number or "").strip().upper(),
        str(tx.row_index),
    ])
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return str(uuid.uuid5(_GUID_NAMESPACE, digest))


def _bank_ledger_label(st: BankAccountStatement) -> str:
    acct = st.account_number or ""
    return _clean_name(f"{st.bank_code} - {acct[-4:] if len(acct) >= 4 else acct}")


def _digits(value: Optional[str]) -> str:
    return re.sub(r"\D", "", value or "")


def _date(iso: str) -> Optional[dt.date]:
    try:
        return dt.date.fromisoformat(iso[:10])
    except ValueError:
        return None


@dataclass
class _Leg:
    tx: NormalizedTransaction
    account: str
    amount: Decimal
    date: Optional[dt.date]


def _references_account(tx: NormalizedTransaction, account_number: str) -> bool:
    ref = _digits(tx.counterparty_vpa_or_acc)
    acct = _digits(account_number)
    if not ref or not acct:
        return False
    if ref == acct:
        return True
    # Masked references such as XXXXXX1234 still identify the account when >= 4 digits match the tail.
    return len(ref) >= 4 and acct.endswith(ref)


def _find_mirror_pairs(
    legs: List[_Leg], account_number_by_key: Dict[str, str]
) -> Dict[str, NormalizedTransaction]:
    """
    Pair category-49 legs with the opposite leg in another uploaded account (EDGE-05).
    Match by UTR first, then by amount and date within +/- 2 days, or +/- 7 days when either leg names the
    other leg's account number. Returns tx_id -> mirror tx.
    """
    by_amount: Dict[Tuple[Decimal, str], List[_Leg]] = {}
    for leg in legs:
        by_amount.setdefault((leg.amount, leg.tx.direction.value), []).append(leg)

    pairs: Dict[str, NormalizedTransaction] = {}
    for leg in sorted((l for l in legs if l.tx.category_id == 49), key=lambda l: (l.tx.txn_date, l.tx.row_index)):
        if leg.tx.tx_id in pairs:
            continue
        opposite = TxDirection.CREDIT.value if leg.tx.direction == TxDirection.DEBIT else TxDirection.DEBIT.value
        utr = (leg.tx.utr_number or "").strip().upper()
        best: Optional[_Leg] = None
        best_score: Tuple[int, int] = (99, 99)
        for cand in by_amount.get((leg.amount, opposite), []):
            if cand.account == leg.account or cand.tx.tx_id in pairs:
                continue
            c_utr = (cand.tx.utr_number or "").strip().upper()
            if utr and c_utr and utr != c_utr:
                continue
            if leg.date is None or cand.date is None:
                continue
            gap = abs((cand.date - leg.date).days)
            if gap > _MIRROR_REF_WINDOW_DAYS:
                continue
            utr_hit = bool(utr and c_utr)
            references = _references_account(leg.tx, account_number_by_key.get(cand.account, "")) or _references_account(
                cand.tx, account_number_by_key.get(leg.account, "")
            )
            if gap > _MIRROR_WINDOW_DAYS and not references:
                continue
            linked = cand.tx.category_id == 49 or references
            if not (utr_hit or linked):
                continue
            score = (0 if utr_hit else 1, gap)
            if score < best_score:
                best, best_score = cand, score
        if best is not None:
            pairs[leg.tx.tx_id] = best.tx
            pairs[best.tx.tx_id] = leg.tx
    return pairs


# ---------------------------------------------------------------------------
# XML generation
# ---------------------------------------------------------------------------

def _entry_pairs(amt: Decimal, bank_receives: bool) -> Tuple[Tuple[str, str], Tuple[str, str]]:
    """((party_deemed_positive, party_amount), (bank_deemed_positive, bank_amount)).

    Tally sign convention: debit entries are negative with ISDEEMEDPOSITIVE=Yes, credits positive with No."""
    amt_s = f"{amt:.2f}"
    if bank_receives:
        return ("No", amt_s), ("Yes", f"-{amt_s}")
    return ("Yes", f"-{amt_s}"), ("No", amt_s)


def _ledger_entries(party_ledger: str, bank_ledger: str, amt: Decimal, bank_receives: bool) -> str:
    party, bank = _entry_pairs(amt, bank_receives)
    return (
        f"""        <ALLLEDGERENTRIES.LIST>
          <LEDGERNAME>{_txt(party_ledger)}</LEDGERNAME>
          <ISDEEMEDPOSITIVE>{party[0]}</ISDEEMEDPOSITIVE>
          <AMOUNT>{party[1]}</AMOUNT>
        </ALLLEDGERENTRIES.LIST>
        <ALLLEDGERENTRIES.LIST>
          <LEDGERNAME>{_txt(bank_ledger)}</LEDGERNAME>
          <ISDEEMEDPOSITIVE>{bank[0]}</ISDEEMEDPOSITIVE>
          <AMOUNT>{bank[1]}</AMOUNT>
        </ALLLEDGERENTRIES.LIST>"""
    )


def _unique_ledger_name(name: str, reserved: Set[str]) -> str:
    """Ledger names may not collide (case-insensitively) with any group name."""
    folded = {r.casefold() for r in reserved}
    if name.casefold() not in folded:
        return name
    base = name[: _MAX_NAME_LEN - 9].strip()
    candidate = f"{base} (Ledger)"
    n = 2
    while candidate.casefold() in folded:
        candidate = f"{base} (Ledger {n})"
        n += 1
    return candidate


def _ledger_kind(name: str, parent: str, bank_names: Set[str]) -> str:
    if name in bank_names:
        return "bank"
    if name == CASH_LEDGER:
        return "cash"
    if name == SUSPENSE_LEDGER_NAME:
        return "suspense"
    if name == UNMATCHED_TRANSFER_LEDGER:
        return "transfer_clearing"
    return "party"


def _build_tally(
    case_title: Optional[str],
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    include_suspense: bool,
    company_name: Optional[str],
    use_trade_vouchers: Optional[bool],
    voucher_prefix: Optional[str],
    ledger_map: "TallyLedgerMap | Dict[str, Any] | None",
) -> Dict[str, Any]:
    lm = TallyLedgerMap.coerce(ledger_map)
    trade = lm.use_trade_vouchers if use_trade_vouchers is None else bool(use_trade_vouchers)
    prefix = voucher_prefix if voucher_prefix else lm.voucher_prefix
    company_src = company_name if company_name else lm.company_name
    stmt_by_id = {s.statement_id: s for s in statements}

    # One bank ledger per real account (several statements of one account share it).
    bank_ledger_by_key: Dict[str, str] = {}
    account_number_by_key: Dict[str, str] = {}
    used_labels: Dict[str, str] = {}
    for st in statements:
        key = f"{st.bank_code}:{st.account_number}"
        if key in bank_ledger_by_key:
            continue
        label = _bank_ledger_label(st) or f"Bank {len(bank_ledger_by_key) + 1}"
        if label in used_labels and used_labels[label] != key:
            label = _ledger_name(f"{st.bank_code} - {st.account_number}")
        used_labels[label] = key
        bank_ledger_by_key[key] = label
        account_number_by_key[key] = st.account_number

    ledgers: Dict[str, str] = {name: "Bank Accounts" for name in bank_ledger_by_key.values()}
    custom_groups: Dict[str, str] = {}

    # Select exportable transactions.
    selected: List[_Leg] = []
    skipped_suspense = 0
    skipped_zero = 0
    for tx in transactions:
        if tx.is_duplicate_ignored:
            continue
        if tx.category_id == 52 and not include_suspense:
            skipped_suspense += 1
            continue
        amt = _amount(tx)
        if amt <= 0:
            skipped_zero += 1
            continue
        st = stmt_by_id.get(tx.statement_id)
        selected.append(_Leg(tx, _account_key(tx, st), amt, _date(tx.txn_date)))

    mirrors = _find_mirror_pairs(selected, account_number_by_key)

    def _parent_of(raw_parent: str, spec_direction: str) -> str:
        parent, custom_parent = resolve_parent_group(raw_parent, spec_direction, lm.custom_groups)
        if custom_parent is not None:
            custom_groups.setdefault(parent, custom_parent)
        return parent

    # Pre-pass: every group name a ledger could be placed under, so ledger names never clash with one.
    reserved_names = set(PREDEFINED_TALLY_GROUPS) | set(lm.custom_groups)
    for leg in selected:
        if leg.tx.category_id in CONTRA_CATEGORIES or leg.tx.category_id == 52:
            continue
        spec = CATEGORY_BY_ID.get(leg.tx.category_id, CATEGORY_BY_ID[52])
        _, _, raw_parent = resolve_tally_mapping(leg.tx, trade, lm)
        reserved_names.add(resolve_parent_group(raw_parent, spec.typical_direction, lm.custom_groups)[0])
    for key, bank_name in list(bank_ledger_by_key.items()):
        safe = _unique_ledger_name(bank_name, reserved_names)
        if safe != bank_name:
            del ledgers[bank_name]
            ledgers[safe] = "Bank Accounts"
            bank_ledger_by_key[key] = safe
    fallback_bank = "Bank Account"
    selected.sort(key=lambda l: (l.tx.txn_date, l.account, l.tx.row_index))

    voucher_blocks: List[str] = []
    vouchers: List[Dict[str, Any]] = []
    voucher_counts: Dict[str, int] = {}
    ledger_usage: Dict[str, Dict[str, Any]] = {}
    emitted_mirror_ids: Set[str] = set()
    mirror_deduped = 0
    unmatched_transfers = 0
    overrides_applied = 0
    seq = 0

    def _use(name: str, amount: Decimal) -> None:
        u = ledger_usage.setdefault(name, {"voucher_count": 0, "amount": Decimal("0")})
        u["voucher_count"] += 1
        u["amount"] += amount

    for leg in selected:
        tx = leg.tx
        if tx.tx_id in emitted_mirror_ids:
            mirror_deduped += 1
            continue
        bank_ledger = bank_ledger_by_key.get(leg.account)
        if bank_ledger is None:
            bank_ledger = fallback_bank
            ledgers.setdefault(fallback_bank, "Bank Accounts")

        mirror = mirrors.get(tx.tx_id)
        mirror_tx_id: Optional[str] = None
        if mirror is not None:
            other_key = _account_key(mirror, stmt_by_id.get(mirror.statement_id))
            voucher_type = VoucherType.CONTRA
            party_ledger = bank_ledger_by_key.get(other_key, fallback_bank)
            emitted_mirror_ids.add(mirror.tx_id)
            mirror_tx_id = mirror.tx_id
        elif tx.category_id == 49:
            target = next(
                (k for k, num in account_number_by_key.items() if k != leg.account and _references_account(tx, num)),
                None,
            )
            if target is not None:
                voucher_type, party_ledger = VoucherType.CONTRA, bank_ledger_by_key[target]
            else:
                voucher_type, party_ledger, _ = resolve_tally_mapping(tx, trade, lm)
                ledgers.setdefault(party_ledger, "Suspense A/c")
                unmatched_transfers += 1
        else:
            voucher_type, party_ledger, raw_parent = resolve_tally_mapping(tx, trade, lm)
            if tx.category_id not in CONTRA_CATEGORIES and tx.category_id != 52 and \
                    lm.counterparty_override(tx.extracted_counterparty, tx.tally_ledger_name):
                overrides_applied += 1
            spec = CATEGORY_BY_ID.get(tx.category_id, CATEGORY_BY_ID[52])
            parent = _parent_of(raw_parent, spec.typical_direction)
            reserved_names.add(parent)
            party_ledger = _unique_ledger_name(party_ledger, reserved_names)
            ledgers.setdefault(party_ledger, parent)

        seq += 1
        guid = voucher_guid(tx, leg.account)
        vch_num = f"{prefix}-{seq:06d}"
        narration = f"{tx.raw_narration} | Cat: {tx.category_name} | UTR: {tx.utr_number or 'NA'}"
        vtype = voucher_type.value
        bank_receives = tx.direction == TxDirection.CREDIT
        voucher_blocks.append(
            f"""    <TALLYMESSAGE xmlns:UDF="TallyUDF">
      <VOUCHER REMOTEID={_attr(guid)} VCHTYPE={_attr(vtype)} ACTION="Create">
        <DATE>{_txt(tx.txn_date.replace("-", ""))}</DATE>
        <GUID>{_txt(guid)}</GUID>
        <NARRATION>{_txt(narration)}</NARRATION>
        <VOUCHERTYPENAME>{_txt(vtype)}</VOUCHERTYPENAME>
        <VOUCHERNUMBER>{_txt(vch_num)}</VOUCHERNUMBER>
        <PERSISTEDVIEW>Accounting Voucher View</PERSISTEDVIEW>
{_ledger_entries(party_ledger, bank_ledger, leg.amount, bank_receives)}
      </VOUCHER>
    </TALLYMESSAGE>"""
        )
        party_e, bank_e = _entry_pairs(leg.amount, bank_receives)
        vouchers.append({
            "voucher_number": vch_num,
            "voucher_type": vtype,
            "date": tx.txn_date,
            "guid": guid,
            "amount": float(leg.amount),
            "party_ledger": party_ledger,
            "bank_ledger": bank_ledger,
            "narration": sanitize_xml_text(narration),
            "tx_id": tx.tx_id,
            "mirror_tx_id": mirror_tx_id,
            "statement_id": tx.statement_id,
            "category_id": tx.category_id,
            "direction": tx.direction.value,
            "utr": tx.utr_number,
            "entries": [
                {"ledger": party_ledger, "is_deemed_positive": party_e[0] == "Yes", "amount": float(party_e[1])},
                {"ledger": bank_ledger, "is_deemed_positive": bank_e[0] == "Yes", "amount": float(bank_e[1])},
            ],
        })
        _use(party_ledger, leg.amount)
        _use(bank_ledger, leg.amount)
        voucher_counts[vtype] = voucher_counts.get(vtype, 0) + 1

    # Only groups that some emitted ledger (or emitted group) sits under are created (EDGE-29).
    emitted_ledgers = {n: p for n, p in ledgers.items() if n not in _PREDEFINED_LEDGERS}
    needed_groups: Dict[str, str] = {}
    frontier = [p for p in emitted_ledgers.values() if p in custom_groups]
    while frontier:
        g = frontier.pop()
        if g in needed_groups:
            continue
        needed_groups[g] = custom_groups[g]
        if custom_groups[g] in custom_groups:
            frontier.append(custom_groups[g])

    group_blocks = [
        f"""    <TALLYMESSAGE xmlns:UDF="TallyUDF">
      <GROUP NAME={_attr(gname)} ACTION="Create">
        <NAME.LIST><NAME>{_txt(gname)}</NAME></NAME.LIST>
        <PARENT>{_txt(gparent)}</PARENT>
      </GROUP>
    </TALLYMESSAGE>"""
        for gname, gparent in sorted(needed_groups.items())
    ]
    ledger_blocks = [
        f"""    <TALLYMESSAGE xmlns:UDF="TallyUDF">
      <LEDGER NAME={_attr(lname)} ACTION="Create">
        <NAME.LIST><NAME>{_txt(lname)}</NAME></NAME.LIST>
        <PARENT>{_txt(pgroup)}</PARENT>
      </LEDGER>
    </TALLYMESSAGE>"""
        for lname, pgroup in sorted(emitted_ledgers.items())
    ]

    company = sanitize_xml_text(company_src).strip() if company_src else ""
    static_vars = (
        f"""
        <STATICVARIABLES>
          <SVCURRENTCOMPANY>{escape(company)}</SVCURRENTCOMPANY>
        </STATICVARIABLES>"""
        if company
        else ""
    )
    body = "\n".join(b for b in (*group_blocks, *ledger_blocks, *voucher_blocks))
    envelope = f"""<?xml version="1.0" encoding="UTF-8"?>
<ENVELOPE>
  <HEADER>
    <TALLYREQUEST>Import Data</TALLYREQUEST>
  </HEADER>
  <BODY>
    <IMPORTDATA>
      <REQUESTDESC>
        <REPORTNAME>All Masters</REPORTNAME>{static_vars}
      </REQUESTDESC>
      <REQUESTDATA>
{body}
      </REQUESTDATA>
    </IMPORTDATA>
  </BODY>
</ENVELOPE>"""

    bank_names = set(bank_ledger_by_key.values()) | {fallback_bank}
    ledger_rows = [
        {
            "name": lname,
            "parent": pgroup,
            "kind": _ledger_kind(lname, pgroup, bank_names),
            "voucher_count": ledger_usage.get(lname, {}).get("voucher_count", 0),
            "amount": float(ledger_usage.get(lname, {}).get("amount", Decimal("0"))),
        }
        for lname, pgroup in sorted(emitted_ledgers.items())
    ]
    stats = {
        "company_name": company or None,
        "case_title": case_title,
        "groups_created": len(group_blocks),
        "ledgers_created": len(ledger_blocks),
        "vouchers_exported": len(voucher_blocks),
        "skipped_suspense": skipped_suspense,
        "skipped_zero_amount": skipped_zero,
        "mirror_legs_deduped": mirror_deduped,
        "unmatched_own_transfers": unmatched_transfers,
        "counterparty_overrides_applied": overrides_applied,
        "use_trade_vouchers": trade,
        "voucher_prefix": prefix,
        "voucher_breakdown": voucher_counts,
    }
    return {
        "xml": envelope,
        "stats": stats,
        "vouchers": vouchers,
        "ledgers": ledger_rows,
        "groups": [{"name": g, "parent": p} for g, p in sorted(needed_groups.items())],
    }


def generate_tally_xml(
    case_title: Optional[str],
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    include_suspense: bool = False,
    *,
    company_name: Optional[str] = None,
    use_trade_vouchers: Optional[bool] = None,
    voucher_prefix: Optional[str] = None,
    ledger_map: "TallyLedgerMap | Dict[str, Any] | None" = None,
) -> Tuple[str, Dict[str, Any]]:
    """
    Build a Tally Prime / ERP 9 XML ENVELOPE with GROUP and LEDGER masters plus VOUCHER entries.

    `case_title` is informational only. SVCURRENTCOMPANY is written only when a company name
    (the exact Tally company name) is supplied, by argument or by the ledger map; otherwise
    Tally imports into the loaded company. `ledger_map` (TallyLedgerMap or its dict form) sets
    the category groups, ledger overrides, trade vouchers and voucher prefix; explicit keyword
    arguments that are not None win over the map. Returns (xml_string, summary_stats).
    """
    res = _build_tally(case_title, statements, transactions, include_suspense, company_name,
                       use_trade_vouchers, voucher_prefix, ledger_map)
    return res["xml"], res["stats"]


DEFAULT_PREVIEW_CHARS = 20_000


def build_tally_export(
    case_title: Optional[str],
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    include_suspense: bool = False,
    *,
    company_name: Optional[str] = None,
    use_trade_vouchers: Optional[bool] = None,
    voucher_prefix: Optional[str] = None,
    ledger_map: "TallyLedgerMap | Dict[str, Any] | None" = None,
    preview_chars: int = DEFAULT_PREVIEW_CHARS,
) -> Dict[str, Any]:
    """
    Tally export with structured data alongside the XML, so a UI never parses XML (EDGE-28).

    Returns {"xml", "xml_preview" (first `preview_chars` characters), "xml_truncated",
    "xml_sha256", "xml_bytes", "stats", "vouchers": [...], "ledgers": [...], "groups": [...],
    "ledger_map": <effective map dict>}.
    """
    res = _build_tally(case_title, statements, transactions, include_suspense, company_name,
                       use_trade_vouchers, voucher_prefix, ledger_map)
    xml = res["xml"]
    limit = max(0, int(preview_chars))
    raw = xml.encode("utf-8")
    return {
        **res,
        "xml_preview": xml[:limit],
        "xml_truncated": len(xml) > limit,
        "xml_sha256": hashlib.sha256(raw).hexdigest(),
        "xml_bytes": len(raw),
        "ledger_map": TallyLedgerMap.coerce(ledger_map).to_dict(),
    }



# ---------------------------------------------------------------------------
# Push to Tally's HTTP XML server
# ---------------------------------------------------------------------------

_COUNT_TAGS = ("CREATED", "ALTERED", "DELETED", "COMBINED", "IGNORED", "ERRORS", "CANCELLED", "EXCEPTIONS")
_TAG_RE_CACHE = {t: re.compile(rf"<{t}>\s*(-?\d+)\s*</{t}>", re.I) for t in _COUNT_TAGS}
_LINEERROR_RE = re.compile(r"<LINEERROR>(.*?)</LINEERROR>", re.I | re.S)


def parse_tally_response(body: str) -> Dict[str, Any]:
    """Parse a Tally import response (RESPONSE or ENVELOPE/IMPORTRESULT forms) into counts and line errors.

    ``recognised`` is True only when the body carries import counters (CREATED/ALTERED/ERRORS/...), an
    IMPORTRESULT element or LINEERROR entries. A free-form ``<RESPONSE>Unknown Request, cannot be
    processed</RESPONSE>`` is Tally rejecting the request, not an import result.
    """
    counts: Dict[str, int] = {t.lower(): 0 for t in _COUNT_TAGS}
    line_errors: List[str] = []
    last_vch_id: Optional[str] = None
    has_counters = False
    clean = sanitize_xml_text(body)
    try:
        root = ET.fromstring(clean)
        has_counters = root.tag.upper() == "IMPORTRESULT" or root.find(".//IMPORTRESULT") is not None
        for tag in _COUNT_TAGS:
            total = 0
            for el in root.iter(tag):
                has_counters = True
                try:
                    total += int((el.text or "0").strip() or 0)
                except ValueError:
                    continue
            counts[tag.lower()] = total
        line_errors = [(el.text or "").strip() for el in root.iter("LINEERROR") if (el.text or "").strip()]
        lv = root.find(".//LASTVCHID")
        last_vch_id = (lv.text or "").strip() if lv is not None else None
    except ET.ParseError:
        # Tally sometimes returns fragments; fall back to tolerant regex extraction.
        for tag, rx in _TAG_RE_CACHE.items():
            found = rx.findall(clean)
            counts[tag.lower()] = sum(int(m) for m in found)
            has_counters = has_counters or bool(found)
        has_counters = has_counters or "<IMPORTRESULT" in clean.upper()
        line_errors = [m.strip() for m in _LINEERROR_RE.findall(clean) if m.strip()]
    recognised = has_counters or bool(line_errors)
    return {
        **counts,
        "line_errors": line_errors,
        "last_voucher_id": last_vch_id,
        "recognised": recognised,
    }


def push_to_tally_http(
    xml_payload: str, host: str = "127.0.0.1", port: int = 9000, timeout: float = 5.0
) -> Dict[str, Any]:
    """
    One-Click Tally Upload Utility: POST XML payload to local Tally XML server (default port 9000).
    Never raises on network failures; returns a structured result instead.
    """
    url = f"http://{host}:{port}"
    req = urllib.request.Request(
        url,
        data=xml_payload.encode("utf-8"),
        headers={"Content-Type": "application/xml"},
        method="POST",
    )
    fallback_hint = "Download the XML file and import via Tally -> Gateway -> Import -> Vouchers."
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return {
            "connected": True,
            "success": False,
            "url": url,
            "http_status": exc.code,
            "message": f"Tally listener at {url} returned HTTP {exc.code}. {fallback_hint}",
        }
    except urllib.error.URLError as exc:
        return {
            "connected": False,
            "success": False,
            "url": url,
            "message": f"Tally local listener not reachable at {url} ({exc.reason}). {fallback_hint}",
        }
    except (TimeoutError, OSError, http.client.HTTPException, ValueError) as exc:
        return {
            "connected": False,
            "success": False,
            "url": url,
            "message": f"Tally local listener at {url} failed ({type(exc).__name__}: {exc}). {fallback_hint}",
        }

    parsed = parse_tally_response(body)
    sent_vouchers = xml_payload.count("<VOUCHER ")
    imported = parsed["created"] + parsed["altered"] + parsed["combined"]
    nothing_imported = sent_vouchers > 0 and imported == 0
    success = (
        parsed["recognised"]
        and parsed["errors"] == 0
        and parsed["exceptions"] == 0
        and not parsed["line_errors"]
        and not nothing_imported
    )
    result: Dict[str, Any] = {
        "connected": True,
        "success": success,
        "url": url,
        **{k: v for k, v in parsed.items() if k != "recognised"},
        "raw_response": sanitize_xml_text(body)[:1000],
    }
    if not parsed["recognised"]:
        detail = re.sub(r"<[^>]+>", " ", sanitize_xml_text(body))
        detail = " ".join(detail.split())[:200]
        result["message"] = "Tally did not import the data" + (f": {detail}" if detail else ".") + f" {fallback_hint}"
    elif nothing_imported and not (parsed["errors"] or parsed["exceptions"] or parsed["line_errors"]):
        result["message"] = f"Tally accepted the request but created no vouchers ({sent_vouchers} sent)."
    elif not success:
        result["message"] = (
            f"Tally reported {parsed['errors']} error(s)"
            + (f": {parsed['line_errors'][0]}" if parsed["line_errors"] else ".")
        )
    return result


__all__ = [
    "DEFAULT_PREVIEW_CHARS",
    "LEDGER_MAP_VERSION",
    "PREDEFINED_TALLY_GROUPS",
    "TallyLedgerMap",
    "build_tally_export",
    "counterparty_key",
    "SUSPENSE_LEDGER_NAME",
    "generate_tally_xml",
    "parse_tally_response",
    "push_to_tally_http",
    "resolve_parent_group",
    "resolve_tally_mapping",
    "sanitize_xml_text",
    "voucher_guid",
]
