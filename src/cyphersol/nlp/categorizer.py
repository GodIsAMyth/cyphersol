"""Hybrid 52-category classifier: learned rules, deterministic rules and curated KB, gated Naive Bayes, Suspense.

Order of evaluation for one transaction (first decisive tier wins):

1. Learned analyst rules on exact identifiers (VPA / full account number). The most specific rule wins and
   its custom Tally ledger is applied.
2. Channel rules (ATM / AEPS / cash deposit / cash withdrawal) and self-transfer evidence (same account, or
   the holder's own name allowing token order and initials; a mere subset of the name is only a suggestion).
3. Curated knowledge base, matched on token boundaries per direction. Confidence reflects match quality.
4. P2P gate: UPI / IMPS / NEFT / RTGS / intra-bank transfer / cheque rows to or from an individual with no
   merchant or KB evidence go to Suspense (category 52) with a confidence below the threshold and top-3
   suggestions; short KB tokens inside such a person's name are demoted.
5. Multinomial Naive Bayes over non-name, non-VPA tokens only, restricted to direction-compatible
   categories, tempered and shrunk by the amount of evidence. It may assign only above the threshold and
   with at least two distinct tokens of evidence for the predicted category. Tabular features (SRS FR-CAT-02:
   direction, log1p(amount), round amount, payment channel, day of month, recurring mandate; NLP-15) tilt
   the text score but never count as evidence on their own. The model is trained on the curated KB plus
   curated tabular priors, and can be refitted on analyst-labelled rows (``fit_labelled_examples``).
6. Learned counterparty-name rules (whole-word match), only for rows that would otherwise be Suspense and
   never for rows the analyst excluded when the rule was learned.
7. Otherwise Suspense.

Results are memoised by (normalised narration, direction, channel, counterparty, identifier, case, holder).
"""

from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Callable, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple, Union

from cyphersol.models import NormalizedTransaction
from cyphersol.nlp.narration_parser import SRS_CHANNEL_EXTENSIONS, SRS_PAYMENT_CHANNELS, internal_payment_channel, srs_payment_channel
from cyphersol.taxonomy import (
    CATEGORY_BY_ID,
    CONTRA_CATEGORY_IDS,
    KB_EVENT_PRIORITY,
    KNOWLEDGE_BASE_PATTERNS,
    SUSPENSE_CATEGORY_ID,
    SUSPENSE_LEDGER_NAME,
    TABULAR_FEATURE_PRIORS,
    tally_voucher_for,
)

# ---------------------------------------------------------------------------------------------------------
# Tokenisation and name heuristics
# ---------------------------------------------------------------------------------------------------------

_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_LONG_DIGITS_RE = re.compile(r"\d{6,}")
_IFSC_RE = re.compile(r"\b[a-z]{4}0[a-z0-9]{6}\b")
_HAS_DIGIT_RE = re.compile(r"\d")

P2P_RAILS: FrozenSet[str] = frozenset({"UPI", "IMPS", "NEFT", "RTGS"})
# Channels on which a person-like counterparty is gated to Suspense and short KB tokens inside the name are
# demoted (NLP-02): intra-bank transfers (INB/TPT, MB/TPT, FT-, TO TRANSFER-) and cheques move money between
# individuals as often as the interbank rails do.
P2P_GATE_CHANNELS: FrozenSet[str] = P2P_RAILS | frozenset({"TRANSFER", "INTERNAL_TRANSFER", "CHEQUE"})
_SELF_TERMS: FrozenSet[str] = frozenset({"self", "own"})
# TRANSFER is also detect_channel's fallback for rows with no rail marker at all. Only rows that carry an
# explicit intra-bank transfer marker are gated as transfers; the others are treated like OTHER (gated only
# when the counterparty looks like a person), so the ML tier is not dead for every unmarked row.
_TRANSFER_MARKER_RE = re.compile(
    r"(?<![a-z0-9])(?:inb|ift|tpt|trf|trfr|tfr|transfer|ft|mb|ib|bil|inf|self|own|fund|funds|sweep|"
    r"by|to|from|a/c|ac|acct)(?![a-z0-9])"
)
_PERSONAL_VPA_RE = re.compile(r"^([a-z]+)[._-]?\d*$")

# Tokens that describe rails, slots or banks rather than the purpose of a payment.
_STOP_TOKENS: FrozenSet[str] = frozenset(
    {
        "upi", "upiar", "upiab", "neft", "rtgs", "imps", "nach", "ach", "ecs", "mmt", "dr", "cr", "p2p",
        "p2a", "p2m", "inb", "ib", "mb", "mob", "bil", "inf", "ift", "tpt", "ft", "trf", "trfr", "transfer",
        "to", "from", "by", "for", "of", "the", "and", "in", "on", "at", "ref", "no", "utr", "rrn", "txn",
        "payment", "payments", "paid", "pay", "bank", "ltd", "pvt", "limited", "private", "india", "ac",
        "acct", "account", "na", "ok", "via", "sent", "received", "money", "debit", "credit",
        # Common bank / IFSC / PSP codes and names occupying structural slots
        "sbin", "sbi", "hdfc", "icic", "icici", "utib", "axis", "kkbk", "kotak", "punb", "pnb", "barb",
        "bob", "cnrb", "canara", "ubin", "union", "idib", "bkid", "cbin", "ioba", "mahb", "psib", "ucba",
        "fdrl", "federal", "idfb", "idfc", "indb", "indusind", "yesb", "yes", "ratn", "rbl", "karb", "kvbl",
        "tmbl", "ciub", "sibl", "dcbl", "airp", "pytm", "fino", "jiop", "aubl", "ybl", "ibl", "axl", "apl",
        "okaxis", "okhdfcbank", "okicici", "oksbi", "paytm", "ptyes", "ptaxis", "pthdfc", "ptsbi",
        "hdfcbank", "axisbank", "ipos",
        # Channel words (the channel is already a separate signal)
        "pos", "ecom", "vps", "ips", "chq", "cheque", "clg", "cts", "micr", "nfs",
    }
)

# Words that mark a counterparty as an organisation rather than an individual.
_BUSINESS_TOKENS: FrozenSet[str] = frozenset(
    {
        "pvt", "ltd", "llp", "inc", "corp", "corporation", "limited", "private", "co", "company", "traders",
        "trading", "enterprise", "enterprises", "solutions", "industries", "industry", "trust", "foundation",
        "hospital", "hospitals", "clinic", "store", "stores", "mart", "shop", "shoppe", "foods", "food",
        "services", "service", "technologies", "technology", "tech", "india", "bank", "finance", "finserv",
        "financial", "capital", "insurance", "medical", "medicals", "medicos", "pharma", "pharmacy",
        "motors", "automobiles", "agency", "agencies", "centre", "center", "hotel", "hotels", "restaurant",
        "cafe", "payments", "retail", "online", "international", "global", "group", "associates",
        "consultancy", "consultants", "exports", "imports", "impex", "broking", "securities", "logistics",
        "travels", "tours", "electronics", "electricals", "hardware", "jewellers", "sweets", "bakery",
        "dairy", "petroleum", "fuels", "filling", "station", "school", "college", "university", "academy",
        "institute", "society", "association", "builders", "developers", "constructions", "infra",
        "distributors", "wholesale", "suppliers", "supplier", "mills", "textiles", "chemicals", "steels",
        "polymers", "labs", "laboratories", "studio", "studios", "media", "ventures", "holdings", "fund",
        "funds", "mf", "amc", "cabs", "telecom", "broadband", "digital", "systems", "software", "infotech",
        "merchant", "seller", "settlement", "pay", "wallet", "recharge", "bills", "bill", "govt", "government", "municipal", "corporation", "nigam",
        "board", "authority", "department", "office", "treasury", "council", "panchayat",
    }
)

_SALUTATIONS: FrozenSet[str] = frozenset({"mr", "mrs", "ms", "miss", "smt", "shri", "sri", "dr", "late", "kum"})

# Purpose / document words that never occur in an Indian personal name: a counterparty slot containing one
# ("REDBUS TICKET", "AIRTEL PREPAID", "WAGES JUNE FACTORY WORKERS") is a description, not an individual.
_PURPOSE_TOKENS: FrozenSet[str] = frozenset(
    {
        "ticket", "tickets", "booking", "order", "orders", "prepaid", "postpaid", "recharge", "renewal",
        "membership", "subscription", "premium", "bill", "bills", "invoice", "inv", "receipt", "challan",
        "repair", "repairs", "servicing", "maintenance", "charges", "charge", "chg", "fee", "fees", "fine",
        "rent", "rental", "lease", "emi", "loan", "salary", "wages", "stipend", "advance", "refund", "deposit",
        "payment", "purchase", "sale", "sales", "listing", "notebooks", "stationery", "workers", "employees",
        "staff", "batch", "bulk", "upload", "interest", "dividend", "commission", "brokerage", "incentive",
        "tax", "gst", "tds", "duty", "license", "licence", "installment", "instalment", "contribution",
        "remittance", "transfer", "settlement", "payout", "redemption", "maturity", "closure", "policy",
        "insurance", "tuition", "school", "admission", "donation", "online", "monthly", "annual", "quarterly",
        "ptax",
    }
)


def _tokens(text: Optional[str]) -> List[str]:
    if not text:
        return []
    return [t for t in _SPLIT_RE.split(text.lower()) if t]


def _name_tokens(name: Optional[str]) -> List[str]:
    """Alphabetic name tokens with salutations and a leading 'M/S' removed."""
    toks = _tokens(name)
    if len(toks) >= 2 and toks[0] == "m" and toks[1] == "s":
        toks = toks[2:]
    return [t for t in toks if t not in _SALUTATIONS]


def _same_person_name(a: Sequence[str], b: Sequence[str]) -> bool:
    """Same name after normalisation: same tokens in any order, where a single-letter initial may stand for
    a full token. At least two full tokens must match exactly (a bare initials match is not enough)."""
    if len(a) != len(b) or len(a) < 2:
        return False
    rest = list(b)
    exact = 0
    for t in sorted(a, key=len, reverse=True):
        if t in rest:
            rest.remove(t)
            exact += len(t) > 1
            continue
        hit = next((r for r in rest if (len(t) == 1 and r.startswith(t)) or (len(r) == 1 and t.startswith(r))), None)
        if hit is None:
            return False
        rest.remove(hit)
    return exact >= 2


def _is_business_name(tokens: Sequence[str]) -> bool:
    return any(t in _BUSINESS_TOKENS for t in tokens)


def _looks_like_person(name: Optional[str]) -> bool:
    """Heuristic: 1-4 alphabetic tokens, no organisation words, no digits, and at most one token that is
    category vocabulary (DHAN in "DHAN SINGH" is fine; "INTEREST ON FD CREDITED" is not a person)."""
    raw = _tokens(name)
    if not raw or (len(raw) >= 2 and raw[0] == "m" and raw[1] == "s"):
        return False
    toks = [t for t in raw if t not in _SALUTATIONS]
    if not toks or len(toks) > 4:
        return False
    if any(not t.isalpha() for t in toks):
        return False
    if _is_business_name(toks) or any(t in _PURPOSE_TOKENS for t in toks):
        return False
    return sum(1 for t in toks if t in _KB_VOCAB) <= 1


def normalize_party(name: Optional[str]) -> str:
    """Normalised counterparty used for learned-rule keys: lower-case alnum tokens joined by single spaces."""
    return " ".join(_name_tokens(name))


# ---------------------------------------------------------------------------------------------------------
# Learned rules (typed keys)
# ---------------------------------------------------------------------------------------------------------

MIN_PARTY_RULE_LEN = 4
_RULE_KINDS = ("vpa", "acc", "party")
_KIND_RANK = {"vpa": 2, "acc": 2, "party": 1}


@dataclass(frozen=True)
class LearnedRule:
    kind: str  # "vpa" | "acc" | "party"
    value: str
    direction: str  # "CR" | "DR" | "BOTH"
    category_id: int
    custom_ledger: Optional[str] = None
    case_id: Optional[str] = None

    @property
    def key(self) -> str:
        return f"{self.kind}:{self.value}"

    def specificity(self) -> Tuple[int, int, int, int]:
        return (
            _KIND_RANK[self.kind],
            1 if self.case_id else 0,
            1 if self.direction != "BOTH" else 0,
            len(self.value),
        )


def _account_digits(value: Optional[str]) -> Optional[str]:
    """Full (unmasked) account number digits, or None for masked / short identifiers."""
    if not value:
        return None
    compact = re.sub(r"[\s-]", "", value)
    if compact.isdigit() and 9 <= len(compact) <= 18:
        return compact
    return None


def parse_rule_key(pattern: str) -> Optional[Tuple[str, str]]:
    """Parse a stored rule pattern into (kind, value). Legacy untyped patterns are coerced; unusable -> None.

    Legacy patterns never fall back to raw-narration substring matching: an "@" makes them a VPA key, a
    9-18 digit string an account key, anything else a word-bounded counterparty key of >= 4 characters.
    """
    p = (pattern or "").strip()
    if not p:
        return None
    kind, sep, rest = p.partition(":")
    if sep and kind.lower() in _RULE_KINDS:
        kind = kind.lower()
        rest = rest.strip()
        if kind == "vpa":
            return ("vpa", rest.lower()) if "@" in rest else None
        if kind == "acc":
            digits = _account_digits(rest)
            return ("acc", digits) if digits else None
        norm = normalize_party(rest)
        return ("party", norm) if len(norm) >= MIN_PARTY_RULE_LEN else None
    if "@" in p and " " not in p:
        return ("vpa", p.lower())
    digits = _account_digits(p)
    if digits:
        return ("acc", digits)
    norm = normalize_party(p)
    return ("party", norm) if len(norm) >= MIN_PARTY_RULE_LEN else None


def derive_rule_key(tx: NormalizedTransaction) -> Optional[str]:
    """Most specific typed key for learning from a row: exact VPA, full account, else normalised party."""
    ident = (tx.counterparty_vpa_or_acc or "").strip()
    if "@" in ident:
        return f"vpa:{ident.lower()}"
    digits = _account_digits(ident)
    if digits:
        return f"acc:{digits}"
    norm = normalize_party(tx.extracted_counterparty)
    if len(norm) >= MIN_PARTY_RULE_LEN:
        return f"party:{norm}"
    return None


def _party_ngrams(tokens: Sequence[str], max_n: int) -> Iterable[str]:
    n_tok = len(tokens)
    for i in range(n_tok):
        for n in range(1, min(max_n, n_tok - i) + 1):
            yield " ".join(tokens[i : i + n])


# ---------------------------------------------------------------------------------------------------------
# Knowledge base matcher (token boundary, per direction)
# ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _KbEntry:
    category_id: int
    pattern: str
    n_tokens: int
    char_len: int


def _directions_for(cat_id: int) -> Tuple[str, ...]:
    d = CATEGORY_BY_ID[cat_id].typical_direction
    return ("CR", "DR") if d == "BOTH" else (d,)


class _KbMatcher:
    """Token n-gram lookup of the curated KB, one index per direction.

    Every pattern must match whole tokens. A single-token pattern of >= 5 characters may also match as the
    prefix of a longer token (e.g. SWIGGYINSTAMART) at reduced confidence; shorter patterns never do.
    """

    PREFIX_MIN = 5

    def __init__(self, kb: Dict[int, List[str]]) -> None:
        self.index: Dict[str, Dict[str, List[_KbEntry]]] = {"CR": defaultdict(list), "DR": defaultdict(list)}
        self.max_n = 1
        for cat_id, patterns in kb.items():
            if cat_id == SUSPENSE_CATEGORY_ID:
                continue
            for pat in patterns:
                toks = _tokens(pat)
                if not toks:
                    continue
                norm = " ".join(toks)
                entry = _KbEntry(cat_id, norm, len(toks), len("".join(toks)))
                self.max_n = max(self.max_n, len(toks))
                for d in _directions_for(cat_id):
                    self.index[d][norm].append(entry)
        self.index = {d: dict(v) for d, v in self.index.items()}
        self.first_tokens = {d: {k.split(" ", 1)[0] for k in v} for d, v in self.index.items()}

    @staticmethod
    def _quality(entry: _KbEntry, prefix: bool) -> float:
        bonus = KB_EVENT_PRIORITY.get(entry.category_id, 0.0)
        if prefix:
            return (0.78 if entry.char_len >= 8 else 0.72) + bonus
        if entry.n_tokens >= 2 or entry.char_len >= 8:
            return 0.95 + bonus
        if entry.char_len >= 5:
            return 0.90 + bonus
        return 0.82 + bonus

    def hits(
        self,
        tokens: Sequence[str],
        direction: str,
        person_tokens: Optional[Set[str]],
    ) -> List[Tuple[float, int, int, int]]:
        """Return [(confidence, n_tokens, char_len, category_id)] for every match, best first.

        `person_tokens` is the token set of a counterparty that looks like an individual (only on P2P rails).
        A single-token hit lying inside such a multi-token name (e.g. "DHAN" in DHAN SINGH) is demoted
        below the threshold because the token is far more likely part of the name than a merchant.
        """
        index = self.index[direction]
        firsts = self.first_tokens[direction]
        out: List[Tuple[float, int, int, int]] = []
        n_tok = len(tokens)

        def demote(tok: str, e: _KbEntry) -> bool:
            return bool(person_tokens) and tok in person_tokens and e.char_len < 8

        for i, tok in enumerate(tokens):
            if tok in firsts:
                for n in range(min(self.max_n, n_tok - i), 0, -1):
                    key = tok if n == 1 else " ".join(tokens[i : i + n])
                    entries = index.get(key)
                    if entries:
                        for e in entries:
                            conf = self._quality(e, prefix=False)
                            if e.n_tokens == 1 and demote(tok, e):
                                conf = 0.5
                            out.append((conf, e.n_tokens, e.char_len, e.category_id))
            if len(tok) > self.PREFIX_MIN and not tok.isdigit():
                for cut in range(len(tok) - 1, self.PREFIX_MIN - 1, -1):
                    entries = index.get(tok[:cut])
                    if entries:
                        for e in entries:
                            if e.n_tokens != 1:
                                continue
                            conf = self._quality(e, prefix=True)
                            if demote(tok, e):
                                conf = 0.5
                            out.append((conf, e.n_tokens, e.char_len, e.category_id))
                        break
        out.sort(reverse=True)
        return out


# ---------------------------------------------------------------------------------------------------------
# Naive Bayes (tempered, evidence-shrunk)
# ---------------------------------------------------------------------------------------------------------


def _ml_tokens_from_text(text: str) -> List[str]:
    return [t for t in _tokens(text) if len(t) >= 2 and t not in _STOP_TOKENS and not _HAS_DIGIT_RE.search(t)]


# Category vocabulary (the ML feature space of the curated KB); used by the person-name heuristic.
_KB_VOCAB: FrozenSet[str] = frozenset(
    t
    for cid, pats in KNOWLEDGE_BASE_PATTERNS.items()
    if cid != SUSPENSE_CATEGORY_ID
    for p in pats
    for t in _ml_tokens_from_text(p)
)


def ml_feature_tokens(tx: NormalizedTransaction) -> List[str]:
    """ML features: narration tokens minus the VPA / identifier, IFSC, long numbers, rail and bank words,
    and the counterparty name when it looks like an individual (names carry no category signal)."""
    text = (tx.raw_narration or "").lower()
    ident = (tx.counterparty_vpa_or_acc or "").lower()
    if ident:
        text = text.replace(ident, " ")
    text = re.sub(r"\S+@\S+", " ", text)
    text = _IFSC_RE.sub(" ", text)
    excluded: Set[str] = set(_tokens(ident))
    cp = tx.extracted_counterparty
    if cp and _looks_like_person(cp):
        excluded.update(_tokens(cp))
    elif cp:
        text = f"{text} {cp.lower()}"
    return list(dict.fromkeys(t for t in _ml_tokens_from_text(text) if t not in excluded))


# ---------------------------------------------------------------------------------------------------------
# Tabular features (SRS FR-CAT-02 feature pipeline; NLP-15)
# ---------------------------------------------------------------------------------------------------------

_MANDATE_RE = re.compile(
    r"(?<![a-z0-9])(?:nach|ach|ecs|umrn|mandate|e-?mandate|autopay|auto\s*debit|standing\s+instruction|si\s+dr|"
    r"si-|upi\s*mandate|upimandate|recurring)(?![a-z0-9])"
)
TABULAR_CHANNELS: Tuple[str, ...] = SRS_PAYMENT_CHANNELS + SRS_CHANNEL_EXTENSIONS


def _tx_amount(tx: NormalizedTransaction) -> Decimal:
    for v in (tx.debit, tx.credit):
        try:
            d = Decimal(v or 0)
        except (InvalidOperation, TypeError, ValueError):
            continue
        if d > 0:
            return d
    return Decimal(0)


def _day_of_month(tx: NormalizedTransaction) -> int:
    m = re.match(r"^\d{4}-\d{2}-(\d{2})", tx.txn_date or "")
    return int(m.group(1)) if m else 0


def _is_round_amount(amount: Decimal) -> bool:
    return amount >= 100 and amount == amount.to_integral_value() and int(amount) % 100 == 0


def _mandate_keyword(tx: NormalizedTransaction) -> bool:
    return internal_payment_channel(tx.payment_channel) == "NACH" or bool(_MANDATE_RE.search((tx.raw_narration or "").lower()))


def _recurrence_key(tx: NormalizedTransaction) -> Optional[Tuple[str, str, str, str]]:
    ident = (tx.counterparty_vpa_or_acc or "").strip().lower() or normalize_party(tx.extracted_counterparty)
    amount = _tx_amount(tx)
    if not ident or amount <= 0:
        return None
    return (tx.case_id or "", tx.direction.value, ident, str(amount.quantize(Decimal("1"))))


def detect_recurring_mandates(transactions: Sequence[NormalizedTransaction], min_months: int = 3) -> Set[str]:
    """tx_ids of rows that recur: same case, direction, counterparty identifier (VPA / account / normalised
    party) and whole-rupee amount in at least ``min_months`` distinct calendar months."""
    months: Dict[tuple, Set[str]] = defaultdict(set)
    members: Dict[tuple, List[str]] = defaultdict(list)
    for t in transactions:
        key = _recurrence_key(t)
        if key is None or not (t.txn_date or "")[:7]:
            continue
        months[key].add(t.txn_date[:7])
        members[key].append(t.tx_id)
    return {tid for k, ids in members.items() if len(months[k]) >= min_months for tid in ids}


def tabular_features(tx: NormalizedTransaction, recurring: bool = False) -> Dict[str, float]:
    """SRS FR-CAT-02 tabular features for one row, as a flat dict of floats:

    ``direction_cr`` (1.0 credit / 0.0 debit), ``log1p_amount``, ``is_round_number`` (whole hundreds,
    >= 100), ``day_of_month`` (1-31, 0 when unknown), ``is_recurring_mandate`` (NACH/ACH/ECS/mandate wording,
    or ``recurring`` from :func:`detect_recurring_mandates`), and one ``channel_<SRS name>`` one-hot per
    member of the SRS channel enum plus extensions (CASH_WDL, FOREX, OTHER).
    """
    amount = _tx_amount(tx)
    feats: Dict[str, float] = {
        "direction_cr": 1.0 if tx.direction.value == "CR" else 0.0,
        "log1p_amount": round(math.log1p(float(amount)), 6),
        "is_round_number": 1.0 if _is_round_amount(amount) else 0.0,
        "day_of_month": float(_day_of_month(tx)),
        "is_recurring_mandate": 1.0 if (recurring or _mandate_keyword(tx)) else 0.0,
    }
    srs = srs_payment_channel(tx.payment_channel)
    if srs not in TABULAR_CHANNELS:
        srs = "OTHER"
    for ch in TABULAR_CHANNELS:
        feats[f"channel_{ch}"] = 1.0 if ch == srs else 0.0
    return feats


def tabular_feature_tokens(tx: NormalizedTransaction, recurring: bool = False) -> List[str]:
    """Discretised tabular features used by the Naive Bayes tier (prefixed ``__`` so they never collide
    with narration tokens): ``__ch_<srs>``, ``__amt_b<decade>``, ``__amt_round``, ``__dom_early|mid|late``,
    ``__mandate``. Direction is not a token because the model is already restricted per direction."""
    f = tabular_features(tx, recurring)
    srs = next((ch for ch in TABULAR_CHANNELS if f[f"channel_{ch}"]), "OTHER")
    toks = [f"__ch_{srs.lower()}"]
    amount = _tx_amount(tx)
    if amount > 0:
        toks.append(f"__amt_b{min(7, max(0, int(math.log10(float(amount)))))}")
    if f["is_round_number"]:
        toks.append("__amt_round")
    dom = int(f["day_of_month"])
    if dom:
        toks.append("__dom_early" if dom <= 7 else ("__dom_late" if dom >= 25 else "__dom_mid"))
    if f["is_recurring_mandate"]:
        toks.append("__mandate")
    return toks


class _NaiveBayes:
    ALPHA = 0.5
    TEMPERATURE = 1.0
    SHRINK_K = 1.0
    MIN_CLASS_EVIDENCE = 2
    TAB_WEIGHT = 0.5  # log-ratio weight of a tabular token; a tilt, never decisive on its own
    TAB_BETA = 1.0

    def __init__(
        self,
        kb: Dict[int, List[str]],
        tabular_priors: Optional[Dict[str, Dict[int, int]]] = None,
        examples: Iterable[Tuple[Sequence[str], Sequence[str], int]] = (),
    ) -> None:
        counts: Dict[int, Counter] = defaultdict(Counter)
        for cat_id, patterns in kb.items():
            if cat_id == SUSPENSE_CATEGORY_ID:
                continue
            for pat in patterns:
                counts[cat_id].update(set(_ml_tokens_from_text(pat)))
        tab_counts: Dict[str, Counter] = defaultdict(Counter)
        for feat, per_cat in (tabular_priors or {}).items():
            for cid, cnt in per_cat.items():
                if cid in CATEGORY_BY_ID and cid != SUSPENSE_CATEGORY_ID:
                    tab_counts[feat][cid] += int(cnt)
        self.n_examples = 0
        for text_toks, tab_toks, cid in examples:
            if cid not in CATEGORY_BY_ID or cid == SUSPENSE_CATEGORY_ID:
                continue
            counts[cid].update(set(text_toks))
            for f in set(tab_toks):
                tab_counts[f][cid] += 1
            self.n_examples += 1
        self.tab_index: Dict[str, Dict[int, float]] = {
            f: {cid: self.TAB_WEIGHT * math.log((n + self.TAB_BETA) / self.TAB_BETA) for cid, n in c.items() if n > 0}
            for f, c in tab_counts.items()
        }
        vocab = {t for c in counts.values() for t in c}
        v_size = max(1, len(vocab))
        self.classes: Tuple[int, ...] = tuple(sorted(counts))
        self.default_logp: Dict[int, float] = {}
        # token -> [(cat_id, logp(token|cat) - default_logp(cat))]
        self.token_index: Dict[str, List[Tuple[int, float]]] = defaultdict(list)
        for cat_id in self.classes:
            c = counts[cat_id]
            denom = sum(c.values()) + self.ALPHA * v_size
            default = math.log(self.ALPHA / denom)
            self.default_logp[cat_id] = default
            for tok, cnt in c.items():
                self.token_index[tok].append((cat_id, math.log((cnt + self.ALPHA) / denom) - default))
        self.token_index = dict(self.token_index)
        self.allowed: Dict[str, Tuple[int, ...]] = {
            d: tuple(cid for cid in self.classes if d in _directions_for(cid)) for d in ("CR", "DR")
        }

    def has_text_evidence(self, tokens: Sequence[str]) -> bool:
        return any(t in self.token_index for t in tokens)

    def rank(
        self, tokens: Sequence[str], direction: str, tab_tokens: Sequence[str] = ()
    ) -> List[Tuple[int, float, int]]:
        """[(category_id, calibrated_confidence, distinct_evidence_tokens_for_that_class)] best first.

        Empty when no text feature token is in the vocabulary (tabular tokens alone are no evidence).
        Evidence counts text tokens only.
        """
        known = [t for t in tokens if t in self.token_index]
        if not known:
            return []
        allowed = self.allowed[direction]
        n = len(known)
        scores = {cid: n * self.default_logp[cid] for cid in allowed}
        evidence: Dict[int, Set[str]] = defaultdict(set)
        for tok in known:
            for cid, delta in self.token_index[tok]:
                if cid in scores:
                    scores[cid] += delta
                    evidence[cid].add(tok)
        for f in tab_tokens:
            for cid, delta in self.tab_index.get(f, {}).items():
                if cid in scores:
                    scores[cid] += delta
        max_s = max(scores.values())
        exp = {cid: math.exp((s - max_s) / self.TEMPERATURE) for cid, s in scores.items()}
        total = sum(exp.values())
        uniform = 1.0 / len(allowed)
        ranked = []
        for cid, e in exp.items():
            p = e / total
            conf = (n * p + self.SHRINK_K * uniform) / (n + self.SHRINK_K)
            ranked.append((cid, conf, len(evidence.get(cid, ()))))
        ranked.sort(key=lambda r: (r[1], r[2]), reverse=True)
        return ranked


# Neutral suggestions for evidence-free transfer rows (analyst picks; never auto-assigned).
_FALLBACK_SUGGESTIONS: Dict[str, Tuple[int, ...]] = {"DR": (11, 49, 14), "CR": (2, 49, 9)}

# (category_id, confidence, source, custom_ledger, top3 ((cid, conf), ...))
_Result = Tuple[int, float, str, Optional[str], Tuple[Tuple[int, float], ...]]


class HybridTransactionCategorizer:
    """Offline categoriser; see the module docstring for the tier order and gating rules."""

    CACHE_LIMIT = 250_000

    def __init__(self, confidence_threshold: float = 0.65) -> None:
        self.confidence_threshold = confidence_threshold
        self.user_rules: List[LearnedRule] = []
        self._rules_exact: Dict[Tuple[str, str], List[LearnedRule]] = {}
        self._rules_party: Dict[str, List[LearnedRule]] = {}
        self._party_max_n = 1
        # Rows the analyst left out when a rule was learned: (kind, value, direction, case_id) -> tx_ids
        self._rule_exclusions: Dict[Tuple[str, str, str, Optional[str]], FrozenSet[str]] = {}
        self._excluded_tx_ids: FrozenSet[str] = frozenset()
        self._kb = _KbMatcher(KNOWLEDGE_BASE_PATTERNS)
        self._nb = _NaiveBayes(KNOWLEDGE_BASE_PATTERNS, TABULAR_FEATURE_PRIORS)
        self._cache: Dict[tuple, _Result] = {}
        self._tab_sensitive: Set[tuple] = set()
        self._last_tab_sensitive = False
        self._recurring_ids: FrozenSet[str] = frozenset()

    def __copy__(self) -> "HybridTransactionCategorizer":
        """Shallow copy sharing the read-only KB / ML indexes and rules, with its own memo state."""
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone._cache = {}
        clone._tab_sensitive = set()
        return clone

    # ----------------------------------------------------------------------------------- ML training
    def fit_labelled_examples(self, examples: Iterable[Tuple[NormalizedTransaction, int]]) -> int:
        """Refit the Naive Bayes tier on the curated KB + tabular priors + analyst-labelled rows.

        ``examples`` are (enriched transaction, category_id) pairs, e.g. rows an analyst resolved. Suspense
        and unknown categories are skipped; person names and VPAs are never features. Replaces any earlier
        fit (it is not cumulative). Returns the number of examples used. The KB, rules and gates are unchanged.
        """
        prepared: List[Tuple[List[str], List[str], int]] = []
        for tx, cid in examples:
            cid = int(cid)
            if cid not in CATEGORY_BY_ID or cid == SUSPENSE_CATEGORY_ID:
                continue
            prepared.append((ml_feature_tokens(tx), tabular_feature_tokens(tx), cid))
        self._nb = _NaiveBayes(KNOWLEDGE_BASE_PATTERNS, TABULAR_FEATURE_PRIORS, prepared)
        self._cache = {}
        return self._nb.n_examples

    # ----------------------------------------------------------------------------------- learned rules
    def load_user_rules(self, rules: Iterable[Sequence]) -> None:
        """Replace rules from persisted rows: (pattern, direction, category_id, custom_ledger[, case_id])."""
        self.user_rules = []
        for row in rules:
            pat, direction, cat_id, ledger = row[0], row[1], row[2], row[3]
            case_id = row[4] if len(row) > 4 else None
            self._add_rule(pat, direction, cat_id, ledger, case_id)
        self._reindex_rules()

    def add_user_rule(
        self,
        pattern: str,
        direction: str,
        category_id: int,
        custom_ledger: Optional[str] = None,
        case_id: Optional[str] = None,
        excluded_tx_ids: Optional[Iterable[str]] = None,
    ) -> Optional[LearnedRule]:
        """Add or replace one rule. Returns the stored rule, or None if the key is unusable (too short).

        ``excluded_tx_ids`` are rows the analyst left out when the rule was learned; the rule never applies to
        them on later classification (NLP-04)."""
        rule = self._add_rule(pattern, direction, category_id, custom_ledger, case_id)
        if rule is not None and excluded_tx_ids:
            self._merge_exclusions({self._rule_ident(rule): frozenset(excluded_tx_ids)})
        self._reindex_rules()
        return rule

    @staticmethod
    def _rule_ident(rule: LearnedRule) -> Tuple[str, str, str, Optional[str]]:
        return (rule.kind, rule.value, rule.direction, rule.case_id)

    def _merge_exclusions(self, extra: Dict[Tuple[str, str, str, Optional[str]], FrozenSet[str]]) -> None:
        # Rebind (never mutate): request-local copies share these containers with the service instance
        merged = dict(self._rule_exclusions)
        for ident, ids in extra.items():
            merged[ident] = merged.get(ident, frozenset()) | ids
        self._rule_exclusions = merged
        self._excluded_tx_ids = frozenset().union(*merged.values()) if merged else frozenset()
        self._cache = {}

    def set_rule_exclusions(self, rows: Iterable[Sequence]) -> None:
        """Replace persisted exclusions from rows (pattern, direction, case_id, tx_ids)."""
        mapping: Dict[Tuple[str, str, str, Optional[str]], FrozenSet[str]] = {}
        for pattern, direction, case_id, tx_ids in rows:
            parsed = parse_rule_key(pattern)
            if parsed is None:
                continue
            ident = (parsed[0], parsed[1], (direction or "BOTH").upper(), case_id or None)
            mapping[ident] = mapping.get(ident, frozenset()) | frozenset(tx_ids)
        self._rule_exclusions = {}
        self._merge_exclusions(mapping)

    def rule_exclusions_for(self, pattern: str, direction: str, case_id: Optional[str] = None) -> List[str]:
        """Rows excluded from the rule stored under this key (sorted), for persistence."""
        parsed = parse_rule_key(pattern)
        if parsed is None:
            return []
        ident = (parsed[0], parsed[1], (direction or "BOTH").upper(), case_id or None)
        return sorted(self._rule_exclusions.get(ident, ()))

    def _add_rule(
        self, pattern: str, direction: str, category_id: int, ledger: Optional[str], case_id: Optional[str]
    ) -> Optional[LearnedRule]:
        parsed = parse_rule_key(pattern)
        if parsed is None or int(category_id) not in CATEGORY_BY_ID:
            return None
        d = (direction or "BOTH").upper()
        rule = LearnedRule(parsed[0], parsed[1], d, int(category_id), ledger or None, case_id or None)
        self.user_rules = [
            r for r in self.user_rules if (r.kind, r.value, r.direction, r.case_id) != (rule.kind, rule.value, d, rule.case_id)
        ]
        self.user_rules.append(rule)
        return rule

    def _reindex_rules(self) -> None:
        exact: Dict[Tuple[str, str], List[LearnedRule]] = defaultdict(list)
        party: Dict[str, List[LearnedRule]] = defaultdict(list)
        for r in self.user_rules:
            if r.kind == "party":
                party[r.value].append(r)
            else:
                exact[(r.kind, r.value)].append(r)
        self._rules_exact = dict(exact)
        self._rules_party = dict(party)
        self._party_max_n = max((v.count(" ") + 1 for v in party), default=1)
        self._cache.clear()

    def _rule_candidates(self, tx: NormalizedTransaction) -> Iterable[LearnedRule]:
        ident = (tx.counterparty_vpa_or_acc or "").strip()
        if ident and self._rules_exact:
            if "@" in ident:
                yield from self._rules_exact.get(("vpa", ident.lower()), ())
            digits = _account_digits(ident)
            if digits:
                yield from self._rules_exact.get(("acc", digits), ())
        if self._rules_party and tx.extracted_counterparty:
            toks = _name_tokens(tx.extracted_counterparty)
            for gram in _party_ngrams(toks, self._party_max_n):
                yield from self._rules_party.get(gram, ())

    def match_user_rule(self, tx: NormalizedTransaction) -> Optional[LearnedRule]:
        """Most specific applicable rule (typed key, direction, and case scope) or None."""
        d = tx.direction.value
        best: Optional[LearnedRule] = None
        excluded = tx.tx_id in self._excluded_tx_ids
        for r in self._rule_candidates(tx):
            if r.direction not in ("BOTH", d):
                continue
            if r.case_id is not None and r.case_id != tx.case_id:
                continue
            if excluded and tx.tx_id in self._rule_exclusions.get(self._rule_ident(r), ()):
                continue
            if best is None or r.specificity() > best.specificity():
                best = r
        return best

    # ----------------------------------------------------------------------------------- classification
    def _cache_key(self, tx: NormalizedTransaction) -> tuple:
        return (
            _LONG_DIGITS_RE.sub("#", (tx.raw_narration or "").lower()),
            tx.direction.value,
            tx.payment_channel,
            tx.extracted_counterparty,
            tx.counterparty_vpa_or_acc,
            tx.case_id,
            tx.source_holder_name,
            tx.source_account_number,
            self.confidence_threshold,
        )

    def _tab_tokens(self, tx: NormalizedTransaction) -> List[str]:
        return tabular_feature_tokens(tx, recurring=tx.tx_id in self._recurring_ids)

    def _self_transfer(self, tx: NormalizedTransaction, cp_tokens: List[str]) -> Optional[bool]:
        """True: own-account evidence (same account number, or the same name after normalisation, allowing
        token order and initials). None: a weaker hint (one name is a subset of the other) that only becomes
        evidence with an explicit SELF/OWN term; it is surfaced as a Suspense suggestion. False: no evidence.

        Two shared common name tokens alone (KUMAR SINGH vs AMIT KUMAR SINGH) are never enough (NLP-01).
        """
        own_acc = _account_digits(tx.source_account_number)
        if own_acc and _account_digits(tx.counterparty_vpa_or_acc) == own_acc:
            return True
        holder = _name_tokens(tx.source_holder_name)
        if len(holder) < 2 or len(cp_tokens) < 2:
            return False
        if _same_person_name(holder, cp_tokens):
            return True
        h, c = set(holder), set(cp_tokens) - _SELF_TERMS
        if len({t for t in h & c if len(t) > 1}) >= 2 and (c <= h or h <= c):
            if _SELF_TERMS & set(_tokens(tx.raw_narration)):
                return True
            return None
        return False

    @staticmethod
    def _gate_person_tokens(
        cp_tokens: List[str], person: bool, on_rail: bool, ident: str
    ) -> Optional[Set[str]]:
        """Counterparty tokens whose short (< 8 character) KB hits are demoted, for person names on P2P
        channels (NLP-02).

        Multi-token names always qualify. A single-token name qualifies only when its
        identifier is a personal VPA built from that name plus digits (zara12@ybl), so single-word merchants
        (uber@axisbank, olacabs@ybl) keep their KB hit.
        """
        if not (person and on_rail and cp_tokens):
            return None
        if len(cp_tokens) >= 2:
            return set(cp_tokens)
        local = ident.split("@", 1)[0].lower() if "@" in ident else ""
        m = _PERSONAL_VPA_RE.match(local) if local else None
        if m and m.group(1) == cp_tokens[0] and local != cp_tokens[0]:
            return set(cp_tokens)
        return None

    def _suggestions(
        self,
        direction: str,
        chosen: Optional[Tuple[int, float]],
        kb_hits: Sequence[Tuple[float, int, int, int]],
        ml_ranked: Union[Sequence[Tuple[int, float, int]], Callable[[], Sequence[Tuple[int, float, int]]]],
    ) -> Tuple[Tuple[int, float], ...]:
        out: List[Tuple[int, float]] = []
        seen: Set[int] = set()

        def push(cid: int, conf: float) -> None:
            if cid in seen or cid == SUSPENSE_CATEGORY_ID or len(out) >= 3:
                return
            ceiling = out[-1][1] if out else 1.0
            seen.add(cid)
            out.append((cid, round(min(conf, ceiling), 4)))

        if chosen:
            push(*chosen)
        for conf, _n, _l, cid in kb_hits:
            push(cid, conf)
        if len(out) >= 3:
            return tuple(out)  # the ML ranking cannot change a full top-3, so it is never computed (BE-06)
        for cid, conf, _ev in (ml_ranked() if callable(ml_ranked) else ml_ranked):
            push(cid, conf)
            if len(out) >= 3:
                return tuple(out)
        floor = round(1.0 / max(1, len(self._nb.allowed[direction])), 4)
        for cid in _FALLBACK_SUGGESTIONS[direction]:
            push(cid, floor)
        if len(out) < 3:
            for cid in self._nb.allowed[direction]:
                push(cid, floor)
                if len(out) >= 3:
                    break
        return tuple(out)

    def _classify_uncached(self, tx: NormalizedTransaction) -> _Result:
        tau = self.confidence_threshold
        d = tx.direction.value
        channel = internal_payment_channel(tx.payment_channel) if tx.payment_channel else ""
        if channel == "TRANSFER" and not _TRANSFER_MARKER_RE.search((tx.raw_narration or "").lower()):
            channel = "OTHER"
        cp = tx.extracted_counterparty or ""
        cp_tokens = _name_tokens(cp)
        person = _looks_like_person(cp)
        ident = (tx.counterparty_vpa_or_acc or "").strip()
        on_rail = channel in P2P_GATE_CHANNELS or (channel in ("OTHER", "") and person and bool(cp_tokens))
        p2p = on_rail and not _is_business_name(cp_tokens)

        hay = _tokens(f"{tx.raw_narration or ''} {cp} {ident}")
        kb_hits = self._kb.hits(hay, d, self._gate_person_tokens(cp_tokens, person, on_rail, ident))
        # The ML tier is evaluated lazily: only on a Tier 1 miss, or when the top-3 suggestions need it (BE-06).
        # A row whose ML ranking was never computed cannot depend on the tabular tokens.
        self._last_tab_sensitive = False
        ml_memo: List[Sequence[Tuple[int, float, int]]] = []

        def ml_ranked() -> Sequence[Tuple[int, float, int]]:
            if not ml_memo:
                ml_tokens = ml_feature_tokens(tx)
                # Tabular tokens only matter when the text gives the ML tier something to rank
                self._last_tab_sensitive = self._nb.has_text_evidence(ml_tokens)
                ml_memo.append(self._nb.rank(ml_tokens, d, self._tab_tokens(tx) if self._last_tab_sensitive else ()))
            return ml_memo[0]

        def decided(cid: int, conf: float, source: str, ledger: Optional[str] = None) -> _Result:
            conf = round(conf, 3)
            return cid, conf, source, ledger, self._suggestions(d, (cid, conf), kb_hits, ml_ranked)

        # 1. Learned analyst rules on exact identifiers (VPA / account). Learned counterparty-name rules only
        #    fill rows that would otherwise stay in Suspense (NLP-04), see tier 6.
        rule = self.match_user_rule(tx)
        if rule is not None and rule.kind != "party":
            return decided(rule.category_id, 0.99, "USER_RULE", rule.custom_ledger)

        # 2. Channel rules and self-transfer evidence
        if d == "DR" and channel in ("ATM", "AEPS", "CASH_WDL"):
            return decided(51, 0.97, "RULE_CHANNEL")
        if d == "CR" and channel == "CASH_CDM":
            return decided(50, 0.97, "RULE_CHANNEL")
        self_hint = False
        if cp_tokens:
            st = self._self_transfer(tx, cp_tokens)
            if st:
                return decided(49, 0.9, "RULE_SELF_TRANSFER")
            self_hint = st is None

        # 3. Curated KB on token boundaries
        if kb_hits and kb_hits[0][0] >= tau:
            return decided(kb_hits[0][3], kb_hits[0][0], "RULE_KB")

        # 4/5. ML only where it is allowed to decide: not a bare P2P transfer, enough class evidence,
        #      never a contra category (those need deterministic evidence)
        if not p2p and ml_ranked():
            cid, conf, ev = ml_ranked()[0]
            if conf >= tau and ev >= _NaiveBayes.MIN_CLASS_EVIDENCE and cid not in CONTRA_CATEGORY_IDS:
                return decided(cid, conf, "ML_MODEL")

        # 6. A learned counterparty-name rule decides only what would otherwise be Suspense
        if rule is not None:
            return decided(rule.category_id, 0.97, "USER_RULE", rule.custom_ledger)

        # 7. Suspense, confidence strictly below the threshold
        top3 = self._suggestions(d, (49, tau - 0.05) if self_hint else None, kb_hits, ml_ranked)
        best = top3[0][1] if top3 else 0.0
        conf = round(max(0.0, min(best, tau - 0.05)), 3)
        source = "SUSPENSE_P2P" if p2p and (person or not cp) else "SUSPENSE"
        return SUSPENSE_CATEGORY_ID, conf, source, None, top3

    def _apply(self, tx: NormalizedTransaction, res: _Result) -> None:
        cid, conf, source, ledger, top3 = res
        spec = CATEGORY_BY_ID[cid]
        tx.category_id = cid
        tx.category_name = spec.name
        tx.categorization_source = source
        tx.confidence_score = conf
        tx.is_suspense = cid == SUSPENSE_CATEGORY_ID
        tx.tally_voucher_type = tally_voucher_for(cid, tx.direction.value)
        if tx.is_suspense:
            tx.tally_ledger_name = SUSPENSE_LEDGER_NAME
        else:
            tx.tally_ledger_name = ledger or tx.extracted_counterparty or spec.name
        tx.top3_suggestions = [
            {"category_id": c, "category_name": CATEGORY_BY_ID[c].name, "confidence": p} for c, p in top3
        ]

    def classify_transaction(self, tx: NormalizedTransaction) -> NormalizedTransaction:
        """Classify one transaction in place (category, confidence, source, Tally mapping, top-3)."""
        if tx.tx_id in self._excluded_tx_ids:  # rule exclusions are per row, so the memo cannot be shared
            self._apply(tx, self._classify_uncached(tx))
            return tx
        base = self._cache_key(tx)
        # Rows whose ML tier saw text evidence also depend on the tabular tokens (amount, date, mandate)
        key = base + (tuple(self._tab_tokens(tx)),) if base in self._tab_sensitive else base
        res = self._cache.get(key)
        if res is None:
            res = self._classify_uncached(tx)
            if len(self._cache) >= self.CACHE_LIMIT:
                self._cache.clear()
                self._tab_sensitive = set()
            if self._last_tab_sensitive and key is base:
                self._tab_sensitive.add(base)
                key = base + (tuple(self._tab_tokens(tx)),)
            self._cache[key] = res
        self._apply(tx, res)
        return tx

    def classify_batch(self, transactions: List[NormalizedTransaction]) -> List[NormalizedTransaction]:
        """Classify rows in place; recurring-mandate detection (a tabular feature) runs over the batch."""
        prev = self._recurring_ids
        self._recurring_ids = frozenset(detect_recurring_mandates(transactions))
        try:
            for tx in transactions:
                self.classify_transaction(tx)
        finally:
            self._recurring_ids = prev
        return transactions


# ---------------------------------------------------------------------------------------------------------
# Suspense resolution and propagation
# ---------------------------------------------------------------------------------------------------------


def _rule_applies(rule: LearnedRule, tx: NormalizedTransaction) -> bool:
    if rule.direction not in ("BOTH", tx.direction.value):
        return False
    ident = (tx.counterparty_vpa_or_acc or "").strip()
    if rule.kind == "vpa":
        return ident.lower() == rule.value
    if rule.kind == "acc":
        return _account_digits(ident) == rule.value
    toks = _name_tokens(tx.extracted_counterparty)
    return rule.value in set(_party_ngrams(toks, rule.value.count(" ") + 1))


def preview_suspense_propagation(
    transactions: Sequence[NormalizedTransaction], target_tx_id: str
) -> Tuple[Optional[str], List[str]]:
    """Rows a resolution of `target_tx_id` would propagate to, for analyst confirmation before applying.

    Only Suspense rows in the same direction (not ignored duplicates) whose typed key matches are listed.
    Returns (rule_key, [tx_id, ...]); rule_key is None when the anchor has no usable key.
    """
    anchor = next((t for t in transactions if t.tx_id == target_tx_id), None)
    if anchor is None:
        return None, []
    key = derive_rule_key(anchor)
    parsed = parse_rule_key(key) if key else None
    if parsed is None:
        return None, []
    rule = LearnedRule(parsed[0], parsed[1], anchor.direction.value, SUSPENSE_CATEGORY_ID)
    ids = [
        t.tx_id
        for t in transactions
        if t.tx_id != target_tx_id and t.is_suspense and not t.is_duplicate_ignored and _rule_applies(rule, t)
    ]
    return key, ids


def apply_suspense_resolution(
    transactions: List[NormalizedTransaction],
    target_tx_id: str,
    new_category_id: int,
    custom_ledger_name: Optional[str] = None,
    auto_refresh_similar: bool = True,
    categorizer: Optional[HybridTransactionCategorizer] = None,
    confirmed_tx_ids: Optional[Iterable[str]] = None,
    case_id: Optional[str] = None,
    learn_single_token_party: bool = False,
) -> Tuple[int, Optional[str]]:
    """Reclassify the anchor row and (optionally) propagate to matching Suspense rows only.

    - The learned key is typed (``vpa:``, ``acc:`` or ``party:``); there is no raw-narration fallback.
    - Propagation never touches rows that are not in Suspense. When ``confirmed_tx_ids`` is given (the
      analyst-confirmed subset of :func:`preview_suspense_propagation`), only those rows are updated.
    - The learned rule never applies to matching Suspense rows that were not updated here (left out of
      ``confirmed_tx_ids`` or not propagated): they are stored as rule exclusions (NLP-04). A single-token
      counterparty key (``party:rahul``) is learned only with ``learn_single_token_party=True``, and learned
      party rules only fill rows that would otherwise be Suspense on later classification.
    - ``case_id`` scopes the learned rule to one case; ``None`` keeps it global (legacy behaviour).

    Returns (updated_count, learned_rule_key); the key is None when nothing was learned.
    """
    if new_category_id not in CATEGORY_BY_ID:
        raise ValueError(f"Unknown category_id {new_category_id}")
    spec = CATEGORY_BY_ID[new_category_id]
    anchor = next((t for t in transactions if t.tx_id == target_tx_id), None)
    if anchor is None:
        return 0, None

    learned_key = derive_rule_key(anchor)
    targets: Set[str] = {target_tx_id}
    excluded: List[str] = []
    if learned_key:
        _key, candidates = preview_suspense_propagation(transactions, target_tx_id)
        chosen = candidates
        if confirmed_tx_ids is not None:
            allowed = set(confirmed_tx_ids)
            chosen = [c for c in candidates if c in allowed]
        if auto_refresh_similar:
            targets.update(chosen)
        excluded = [c for c in candidates if c not in targets]
    if learned_key and learned_key.startswith("party:") and " " not in learned_key and not learn_single_token_party:
        learned_key = None

    if categorizer is not None and learned_key:
        categorizer.add_user_rule(
            learned_key, anchor.direction.value, new_category_id, custom_ledger_name, case_id=case_id,
            excluded_tx_ids=excluded,
        )

    updated = 0
    for tx in transactions:
        if tx.tx_id not in targets:
            continue
        tx.category_id = new_category_id
        tx.category_name = spec.name
        tx.categorization_source = "USER_RULE"
        tx.confidence_score = 1.0
        tx.is_suspense = new_category_id == SUSPENSE_CATEGORY_ID
        tx.tally_voucher_type = tally_voucher_for(new_category_id, tx.direction.value)
        if tx.is_suspense:
            tx.tally_ledger_name = SUSPENSE_LEDGER_NAME
        else:
            tx.tally_ledger_name = custom_ledger_name or tx.extracted_counterparty or spec.name
        updated += 1

    return updated, learned_key
