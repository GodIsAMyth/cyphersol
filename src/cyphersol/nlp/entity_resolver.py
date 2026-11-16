"""Entity Resolution & 'Merge Similar' fuzzy/phonetic clustering engine."""

from __future__ import annotations

import re
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from difflib import SequenceMatcher
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

from cyphersol.models import BankAccountStatement, NormalizedTransaction, ResolvedEntity

try:  # optional accelerators; the stdlib path below is always available
    from rapidfuzz.fuzz import ratio as _rf_ratio  # type: ignore
except Exception:  # noqa: BLE001
    _rf_ratio = None
try:
    from jellyfish import jaro_winkler_similarity as _jf_jaro_winkler  # type: ignore
except Exception:  # noqa: BLE001
    _jf_jaro_winkler = None

_SALUTATIONS = frozenset({
    "MR", "MRS", "MS", "MISS", "SHRI", "SRI", "SHREE", "SMT", "SHRIMATI", "KUM", "KUMARI", "DR", "PROF",
    "MESSRS", "MESSERS", "MESRS", "MSRS",
})


UNRESOLVED_PREFIX = "UNRESOLVED_"

# Generic parties emitted by the narration parser (or typed by banks) that denote the banking system
# itself rather than a natural/legal person. They may terminate a money trail (sink/source) but must never
# act as an intermediary in graph, loop or layering traversal (NLP-10). Stored in normalised form.
_SYSTEM_PARTY_NAMES = frozenset({
    "CASH", "CASH WITHDRAWAL", "CASH WITHDRAWAL ATM", "ATM", "ATM WITHDRAWAL", "ATM CASH", "ATM CASH WITHDRAWAL",
    "CASH DEPOSIT", "CASH DEPOSIT CDM", "CDM CASH DEPOSIT", "BY CASH", "TO CASH", "SELF CASH",
    "AEPS CASH TRANSACTION", "AEPS CASH WITHDRAWAL",
    "BANK INTEREST", "INTEREST", "INTEREST CREDIT", "INTEREST PAID", "INT PD", "SB INTEREST",
    "BANK CHARGES", "CHARGES", "SERVICE CHARGES", "SMS CHARGES", "ATM CHARGES", "MIN BAL CHARGES",
    "GST", "GST ON CHARGES", "TDS", "TDS ON INTEREST",
    "AUTO SWEEP FD LINK", "AUTO SWEEP", "SWEEP", "RETURN REVERSAL", "RETURN", "REVERSAL",
})

# Names made only of these generic banking words are system parties too, whatever their order or the
# rail prefix the parser kept ("IMPS RETURN", "RTN CHGS", "FD CLOSURE PROCEEDS", "INTEREST ON FD").
_SYSTEM_CORE_TOKENS = frozenset({
    "CASH", "ATM", "CDM", "AEPS", "INTEREST", "INT", "CHARGES", "CHGS", "CHG", "GST", "TDS", "SWEEP",
    "AUTOSWEEP", "FD", "RD", "RETURN", "RTN", "REVERSAL", "REV", "REVERSED", "BOUNCE", "PENALTY", "FEE", "FEES",
})
_SYSTEM_FILLER_TOKENS = frozenset({
    "AUTO", "MOD", "LINK", "LINKED", "TRF", "TRANSFER", "TO", "FROM", "BY", "ON", "OF", "FOR", "THE", "AND",
    "BANK", "SB", "CA", "CC", "OD", "PD", "PAID", "CREDIT", "DEBIT", "CR", "DR", "CLOSURE", "PROCEEDS",
    "MATURITY", "PREMATURE", "WITHDRAWAL", "DEPOSIT", "SERVICE", "SMS", "MIN", "BAL", "NEFT", "RTGS", "IMPS",
    "UPI", "NACH", "ECS", "ACH", "CHQ", "CHEQUE", "INWARD", "OUTWARD", "TXN", "AMT", "AMOUNT", "SELF",
})


def is_system_party_name(name: str) -> bool:
    """True for UNRESOLVED_* buckets and generic banking-system parties (cash, ATM, charges, interest)."""
    if not name:
        return False
    up = name.strip().upper()
    if up.startswith(UNRESOLVED_PREFIX):
        return True
    tokens = re.sub(r"[^A-Z0-9]", " ", up).split()
    if " ".join(tokens) in _SYSTEM_PARTY_NAMES:
        return True
    words = [t for t in tokens if not t.isdigit()]
    return bool(words) and any(t in _SYSTEM_CORE_TOKENS for t in words) and all(
        t in _SYSTEM_CORE_TOKENS or t in _SYSTEM_FILLER_TOKENS for t in words
    )


def is_system_entity(entity: Union["ResolvedEntity", str, Any]) -> bool:
    """
    Whether a resolved entity (or raw party name) is a system/generic party. Uses the in-memory `is_system`
    flag set by resolve_case_entities when present, and otherwise derives it from the canonical name and
    aliases so that entities re-loaded from storage are classified identically.
    """
    if entity is None:
        return False
    if isinstance(entity, str):
        return is_system_party_name(entity)
    if getattr(entity, "is_system", False):
        return True
    if getattr(entity, "has_statement", False):
        return False
    if is_system_party_name(getattr(entity, "canonical_name", "") or ""):
        return True
    aliases = getattr(entity, "aliases", None) or []
    return bool(aliases) and all(is_system_party_name(a) for a in aliases)


# ------------------------------------------------------------------------------------------------ normalisation
# NLP-06: "M/S" (Messrs) must be removed *before* punctuation is stripped, otherwise it becomes "M S".
_MS_RE = re.compile(r"(?<![A-Z0-9])M\s*/\s*S(?![A-Z0-9])\.?")
_VPA_RE = re.compile(r"([A-Z0-9][A-Z0-9._\-]*)@[A-Z0-9][A-Z0-9.\-]*")
# Leading banking/rail filler words that parsers leave in front of (or after) a party name.
_BANK_STOPWORDS = frozenset({
    "TO", "BY", "FROM", "TRF", "TFR", "TRANSFER", "TRANSFERRED", "NEFT", "RTGS", "IMPS", "UPI", "INB", "IB",
    "MOB", "MB", "NETBANKING", "CR", "CREDIT", "DEBIT", "PAYMENT", "PAY", "PAID", "PMT", "FOR", "AC", "ACC",
    "ACCOUNT", "CHQ", "CHEQUE", "CLG", "ACH", "NACH", "ECS", "REF", "INW", "INWARD", "OUTWARD", "FT",
    "VIA", "THRU", "P2A", "P2P", "P2M",
})
# Legal-form suffixes: removed only from the END of a name (NLP-07).
_LEGAL_FORMS = frozenset({
    "PVT", "PRIVATE", "PRIV", "PVTLTD", "LTD", "LIMITED", "LLP", "INC", "INCORPORATED", "CORP", "CORPORATION",
    "CO", "COMPANY", "P", "PLC", "OPC",
})
# Business-type descriptors are *kept* in the name; a type mismatch is penalised in the similarity score.
_BUSINESS_TYPES: Dict[str, str] = {
    "TRADERS": "TRADE", "TRADER": "TRADE", "TRADING": "TRADE", "TRADE": "TRADE",
    "ENTERPRISE": "ENT", "ENTERPRISES": "ENT", "AGENCY": "AGENCY", "AGENCIES": "AGENCY",
    "ASSOCIATES": "ASSOC", "ASSOCIATE": "ASSOC", "SOLUTIONS": "SOLN", "SOLUTION": "SOLN",
    "SERVICES": "SERV", "SERVICE": "SERV", "VENTURES": "VENT", "VENTURE": "VENT",
    "INDUSTRIES": "INDUS", "INDUSTRY": "INDUS", "EXPORTS": "EXIM", "EXPORT": "EXIM", "IMPORTS": "EXIM",
    "IMPEX": "EXIM", "CONSTRUCTION": "BUILD", "CONSTRUCTIONS": "BUILD", "BUILDERS": "BUILD", "DEVELOPERS": "BUILD",
    "TEXTILES": "TEX", "TEXTILE": "TEX", "FABRICS": "TEX", "STORES": "STORE", "STORE": "STORE", "MART": "STORE",
    "MOTORS": "MOTOR", "AUTOMOBILES": "MOTOR", "JEWELLERS": "JEWEL", "JEWELLERY": "JEWEL", "FOODS": "FOOD",
    "PHARMA": "PHARMA", "PHARMACEUTICALS": "PHARMA", "MEDICALS": "PHARMA", "TRAVELS": "TRAVEL", "TOURS": "TRAVEL",
    "LOGISTICS": "LOGI", "TRANSPORT": "LOGI", "CARRIERS": "LOGI", "INFRA": "INFRA", "INFRASTRUCTURE": "INFRA",
    "FINANCE": "FIN", "FINANCIAL": "FIN", "FINSERV": "FIN", "CAPITAL": "FIN", "INVESTMENTS": "FIN",
    "HOSPITAL": "HOSP", "CLINIC": "HOSP", "HOTEL": "HOTEL", "RESTAURANT": "HOTEL", "STEELS": "STEEL",
    "STEEL": "STEEL", "MILLS": "MILL", "TECHNOLOGIES": "TECH", "TECH": "TECH", "SYSTEMS": "TECH",
    "SOFTWARE": "TECH", "HUF": "HUF", "TRUST": "TRUST", "FOUNDATION": "TRUST", "SOCIETY": "TRUST",
    # Truncations that bank narrations produce (FR-ENT-01: SHREE BALAJI ENT == SHREE BALAJI ENTERPRISES)
    "ENT": "ENT", "ENTS": "ENT", "ENTP": "ENT", "ENTPR": "ENT", "ENTPRS": "ENT", "ENTPRISES": "ENT",
    "ENTERPRIS": "ENT", "TRDRS": "TRADE", "TRDR": "TRADE", "TRDG": "TRADE", "TRDS": "TRADE",
    "TRADRS": "TRADE", "SERV": "SERV", "SERVS": "SERV", "SVCS": "SERV", "SVC": "SERV", "SRVCS": "SERV",
    "INDS": "INDUS", "INDUS": "INDUS", "INDSTRS": "INDUS", "ASSOC": "ASSOC", "ASSO": "ASSOC", "ASSCTS": "ASSOC",
    "AGNCY": "AGENCY", "AGNCS": "AGENCY", "SOLN": "SOLN", "SOLNS": "SOLN", "CONST": "BUILD", "CONSTN": "BUILD",
    "CONSTRN": "BUILD", "TECHNO": "TECH", "TECHS": "TECH", "PHARM": "PHARMA", "TEXS": "TEX",
    "JEWLRS": "JEWEL", "JWLRS": "JEWEL", "TRVLS": "TRAVEL", "LOGIS": "LOGI", "EXPTS": "EXIM", "FINSRV": "FIN",
}
_TOKEN_CANON: Dict[str, str] = {
    "MOHAMMAD": "MOHD", "MOHAMMED": "MOHD", "MUHAMMAD": "MOHD", "MOHAMED": "MOHD", "MOHAMAD": "MOHD",
    "MOHD": "MOHD", "MD": "MOHD", "SHREE": "SRI", "SHRI": "SRI", "SREE": "SRI", "SRI": "SRI", "KR": "KUMAR",
    "KMR": "KUMAR", "BROS": "BROTHERS", "BRO": "BROTHERS",
}


def _strip_tokens(tokens: List[str]) -> List[str]:
    tokens = list(tokens)
    while len(tokens) > 1 and (tokens[0] in _SALUTATIONS or tokens[0] in _BANK_STOPWORDS):
        tokens.pop(0)
    while len(tokens) > 1 and tokens[-1] in _BANK_STOPWORDS:
        tokens.pop()
    stripped_legal = False
    # A bare trailing "P" is usually an initial (SURESH P); it is a legal form only in "(P) LTD".
    while len(tokens) > 1 and tokens[-1] in _LEGAL_FORMS and (tokens[-1] != "P" or stripped_legal):
        tokens.pop()
        stripped_legal = True
    if stripped_legal:
        while len(tokens) > 1 and tokens[-1] in ("INDIA", "AND"):
            tokens.pop()
    while len(tokens) > 1 and tokens[-1] in _BANK_STOPWORDS:
        tokens.pop()
    return tokens


def normalize_entity_name(raw_name: str) -> str:
    """
    Normalise a party name: upper-case, "M/S"/Messrs and other salutations and leading banking filler words
    removed, a VPA reduced to its handle, long reference/account numbers dropped, and *trailing* legal-form
    suffixes (PVT LTD, LLP, & CO ...) removed. Interior words and business-type descriptors are kept.
    """
    if not raw_name:
        return ""
    up = str(raw_name).upper()
    up = _MS_RE.sub(" MESSRS ", up)
    up = _VPA_RE.sub(lambda m: " " + m.group(1) + " ", up)
    up = re.sub(r"[^A-Z0-9\s]", " ", up)
    raw_tokens = [t for t in up.split() if t]
    tokens = [t for t in raw_tokens if not (t.isdigit() and len(t) >= 6)] or raw_tokens
    return " ".join(_strip_tokens(tokens)).strip()


def _phonetic_token(tok: str) -> str:
    if not tok or tok.isdigit():
        return tok
    t = _TOKEN_CANON.get(tok, tok)
    t = t.replace("X", "KS").replace("Q", "K").replace("CK", "K")
    t = t.replace("CHH", "CH").replace("CH", "\x01").replace("C", "K").replace("\x01", "C")
    for old, new in (("PH", "F"), ("SH", "S"), ("BH", "B"), ("DH", "D"), ("GH", "G"), ("KH", "K"), ("TH", "T"),
                     ("JH", "J"), ("W", "V"), ("Z", "J"), ("EE", "I"), ("OO", "U"), ("AA", "A"), ("Y", "I")):
        t = t.replace(old, new)
    # NLP-08: J/G are only neutralised word-finally after I (VIJ/VIG); an initial J/G is distinct (JAYA vs GAYA).
    if len(t) >= 3 and t.endswith("IJ"):
        t = t[:-1] + "G"
    return re.sub(r"(.)\1+", r"\1", t)


def indian_phonetic_key(name: str) -> str:
    """
    Per-token phonetic key tailored to Indian transliteration variants (Poojan Vig <-> Pujan Vij,
    Mohd <-> Mohammed, Vikas <-> Wikas, Shree <-> Sri, Laxmi <-> Lakshmi, Zaheer <-> Jaheer).
    """
    s = normalize_entity_name(name)
    return " ".join(_phonetic_token(t) for t in s.split()) if s else ""


def _jaro_winkler(s1: str, s2: str) -> float:
    if _jf_jaro_winkler is not None:
        return float(_jf_jaro_winkler(s1, s2))
    if s1 == s2:
        return 1.0
    if not s1 or not s2:
        return 0.0
    l1, l2 = len(s1), len(s2)
    match_dist = max(0, max(l1, l2) // 2 - 1)
    s1_matches = [False] * l1
    s2_matches = [False] * l2
    matches = 0
    transpositions = 0

    for i in range(l1):
        start = max(0, i - match_dist)
        end = min(i + match_dist + 1, l2)
        for j in range(start, end):
            if s2_matches[j] or s1[i] != s2[j]:
                continue
            s1_matches[i] = True
            s2_matches[j] = True
            matches += 1
            break

    if matches == 0:
        return 0.0

    k = 0
    for i in range(l1):
        if not s1_matches[i]:
            continue
        while not s2_matches[k]:
            k += 1
        if s1[i] != s2[k]:
            transpositions += 1
        k += 1

    jaro = ((matches / l1) + (matches / l2) + ((matches - transpositions / 2.0) / matches)) / 3.0
    prefix = 0
    for i in range(min(4, l1, l2)):
        if s1[i] == s2[i]:
            prefix += 1
        else:
            break
    return jaro + prefix * 0.1 * (1.0 - jaro)


class _Feat:
    __slots__ = ("norm", "compact", "tokens", "token_set", "sorted_str", "ptoks", "pkey", "pcompact", "types")

    def __init__(self, norm: str) -> None:
        self.norm = norm
        self.tokens = norm.split()
        self.token_set = frozenset(self.tokens)
        self.compact = "".join(self.tokens)
        self.sorted_str = " ".join(sorted(self.tokens))
        # Business descriptors compare by class (TRADERS == TRADING), names by Indic phonetic key.
        ptoks = ["#" + _BUSINESS_TYPES[t] if t in _BUSINESS_TYPES else _phonetic_token(t) for t in self.tokens]
        self.ptoks = tuple(sorted(ptoks))
        self.pkey = " ".join(self.ptoks)
        self.pcompact = "".join(ptoks)
        self.types = frozenset(_BUSINESS_TYPES[t] for t in self.tokens if t in _BUSINESS_TYPES)


@lru_cache(maxsize=131072)
def _features(norm: str) -> _Feat:
    return _Feat(norm)


def _ratio(a: str, b: str) -> float:
    if _rf_ratio is not None:
        return _rf_ratio(a, b) / 100.0
    return SequenceMatcher(None, a, b).ratio()


def _len_bound(a: str, b: str) -> float:
    la, lb = len(a), len(b)
    return 2.0 * min(la, lb) / (la + lb) if la + lb else 0.0


def _score_features(fa: _Feat, fb: _Feat, floor: float = 0.0) -> float:
    """Hybrid similarity in [0,1]. Returns -1.0 early when the score provably cannot reach ``floor``."""
    if not fa.norm or not fb.norm:
        return 0.0
    if fa.norm == fb.norm:
        return 1.0
    if fa.compact == fb.compact:
        return 0.97
    inter = len(fa.token_set & fb.token_set)
    tset = 2.0 * inter / max(1, len(fa.token_set) + len(fb.token_set))
    type_clash = bool(fa.types and fb.types and fa.types.isdisjoint(fb.types))
    pk_equal = fa.pkey == fb.pkey and len(fa.pcompact) >= 4
    pc_equal = fa.pcompact == fb.pcompact and len(fa.pcompact) >= 6
    if floor > 0.0 and not (pk_equal or pc_equal):
        ub = 0.40 * _len_bound(fa.sorted_str, fb.sorted_str) + 0.25 + 0.20 * _len_bound(fa.pkey, fb.pkey) + 0.15 * tset
        if type_clash:
            ub = min(ub * 0.85, 0.80)
        if ub < floor:
            return -1.0
    token_sort = _ratio(fa.sorted_str, fb.sorted_str)
    jw = _jaro_winkler(fa.norm, fb.norm)
    phon = _ratio(fa.pkey, fb.pkey)
    score = 0.40 * token_sort + 0.25 * jw + 0.20 * phon + 0.15 * tset
    if pk_equal:
        score = max(score, 0.92)
    elif pc_equal:
        score = max(score, 0.90)
    if type_clash:
        score = min(score * 0.85, 0.80)
    return round(min(1.0, score), 4)


def compute_name_similarity(name_a: str, name_b: str) -> float:
    """
    Hybrid lexical + token-sort + per-token Indic phonetic similarity in [0.0, 1.0] on normalised names.
    Space-insensitive (POOJANVIG == POOJAN VIG); a business-type mismatch (TRADERS vs ENTERPRISES) is penalised.
    """
    s = _score_features(_features(normalize_entity_name(name_a)), _features(normalize_entity_name(name_b)))
    return max(0.0, s)


# ------------------------------------------------------------------------------------------------ identifiers
_PAN_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{3}[ABCFGHJLPT][A-Z][0-9]{4}[A-Z])(?![A-Z0-9])")


def extract_pans(text: Optional[str]) -> List[str]:
    """PAN numbers (AAAAA9999A with a valid holder-type 4th letter) found in free text, de-duplicated, in order."""
    if not text:
        return []
    out: List[str] = []
    for m in _PAN_RE.finditer(str(text).upper()):
        if m.group(1) not in out:
            out.append(m.group(1))
    return out


def full_account_number(raw: Optional[str]) -> Optional[str]:
    """An unmasked 9-18 digit account number, or None for masked (XXXX1234), short or non-numeric values."""
    if not raw:
        return None
    s = re.sub(r"[\s\-/.]", "", str(raw).upper())
    if not s.isdigit() or not 9 <= len(s) <= 18 or len(set(s)) == 1:
        return None
    return s


# ------------------------------------------------------------------------------------------------ decisions
_DECISION_OPS = ("merge", "split", "reject")


def _ent_get(ent: Any, key: str, default: Any = None) -> Any:
    if isinstance(ent, dict):
        return ent.get(key, default)
    return getattr(ent, key, default)


def _find_entity(entities: Iterable[Any], entity_id: str) -> Any:
    for e in entities:
        if _ent_get(e, "entity_id") == entity_id:
            return e
    raise ValueError(f"Unknown entity_id: {entity_id}")


def _alias_set(ent: Any) -> List[str]:
    names = {str(a).strip().upper() for a in (_ent_get(ent, "aliases") or []) if str(a).strip()}
    canon = str(_ent_get(ent, "canonical_name") or "").strip().upper()
    if canon:
        names.add(canon)
    return sorted(names)


def _new_decision(op: str, groups: List[List[str]], entity_ids: List[str], actor: Optional[str], note: str,
                  decision_id: Optional[str], created_at: Optional[str]) -> Dict[str, Any]:
    return {
        "decision_id": decision_id or uuid.uuid4().hex,
        "op": op,
        "groups": [sorted(set(g)) for g in groups],
        "entity_ids": list(entity_ids),
        "actor": actor,
        "note": note or "",
        "created_at": created_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def merge_entities(
    decisions: Sequence[Dict[str, Any]], entities: Iterable[Any], source_entity_id: str, target_entity_id: str,
    actor: Optional[str] = None, note: str = "", decision_id: Optional[str] = None, created_at: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Pure: append a manual merge of two entities (by ID). Returns (new_decisions, decision). The merged
    entity keeps the target's entity ID when re-resolved with ``previous_entities``."""
    entities = list(entities)
    if source_entity_id == target_entity_id:
        raise ValueError("Cannot merge an entity with itself.")
    src = _find_entity(entities, source_entity_id)
    tgt = _find_entity(entities, target_entity_id)
    d = _new_decision("merge", [_alias_set(src), _alias_set(tgt)], [source_entity_id, target_entity_id],
                      actor, note, decision_id, created_at)
    return list(decisions) + [d], d


def split_entity(
    decisions: Sequence[Dict[str, Any]], entities: Iterable[Any], entity_id: str, aliases: Iterable[str],
    actor: Optional[str] = None, note: str = "", decision_id: Optional[str] = None, created_at: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Pure: append a split of ``aliases`` out of entity ``entity_id``. The split-off aliases stay together and
    are never linked back to the remainder (by fuzzy *or* identifier evidence) until the decision is undone."""
    ent = _find_entity(list(entities), entity_id)
    members = set(_alias_set(ent))
    part = {str(a).strip().upper() for a in aliases if str(a).strip()}
    if not part:
        raise ValueError("Select at least one alias to split off.")
    unknown = sorted(part - members)
    if unknown:
        raise ValueError(f"Not aliases of {entity_id}: {', '.join(unknown)}")
    rest = members - part
    if not rest:
        raise ValueError("A split must leave at least one alias in the original entity.")
    d = _new_decision("split", [sorted(part), sorted(rest)], [entity_id], actor, note, decision_id, created_at)
    return list(decisions) + [d], d


def reject_merge(
    decisions: Sequence[Dict[str, Any]], entities: Iterable[Any], entity_a_id: str, entity_b_id: str,
    actor: Optional[str] = None, note: str = "", decision_id: Optional[str] = None, created_at: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Pure: append a never-link constraint between two entities (e.g. a rejected merge candidate)."""
    entities = list(entities)
    if entity_a_id == entity_b_id:
        raise ValueError("Cannot reject an entity against itself.")
    a = _find_entity(entities, entity_a_id)
    b = _find_entity(entities, entity_b_id)
    d = _new_decision("reject", [_alias_set(a), _alias_set(b)], [entity_a_id, entity_b_id],
                      actor, note, decision_id, created_at)
    return list(decisions) + [d], d


def undo_decision(
    decisions: Sequence[Dict[str, Any]], decision_id: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Pure: remove the given decision (default: the most recent). Returns (new_decisions, removed_or_None)."""
    decisions = list(decisions)
    if not decisions:
        if decision_id:
            raise ValueError(f"Unknown decision_id: {decision_id}")
        return [], None
    if decision_id is None:
        return decisions[:-1], decisions[-1]
    for i, d in enumerate(decisions):
        if d.get("decision_id") == decision_id:
            return decisions[:i] + decisions[i + 1:], d
    raise ValueError(f"Unknown decision_id: {decision_id}")


def manual_merges_to_decisions(manual_merges: Dict[str, str]) -> List[Dict[str, Any]]:
    """Migrate legacy {alias: canonical} free-text merges into merge decisions (deterministic IDs)."""
    out = []
    for alias, canon in sorted((manual_merges or {}).items()):
        a, c = str(alias).strip().upper(), str(canon).strip().upper()
        if a and c and a != c:
            did = uuid.uuid5(uuid.NAMESPACE_URL, f"legacy-merge:{a}:{c}").hex
            out.append(_new_decision("merge", [[a], [c]], [], None, "legacy manual merge", did, "1970-01-01T00:00:00+00:00"))
    return out


def _compile_decisions(decisions: Sequence[Dict[str, Any]]) -> Tuple[List[Set[str]], List[Tuple[Set[str], Set[str]]]]:
    """Replay decisions in order into must-link groups and never-link pairs. A later merge cancels earlier
    never-link constraints it contradicts; a later split/reject wins over earlier merges (enforced at union time)."""
    musts: List[Set[str]] = []
    cannots: List[Tuple[Set[str], Set[str]]] = []
    for d in decisions or []:
        op = str(d.get("op") or "").lower()
        groups = [{str(a).strip().upper() for a in g if str(a).strip()} for g in (d.get("groups") or [])]
        if op not in _DECISION_OPS or len(groups) < 2 or not groups[0] or not groups[1]:
            continue
        a, b = groups[0], groups[1]
        if op == "merge":
            u = a | b
            cannots = [(x, y) for (x, y) in cannots if not ((x & u) and (y & u))]
            musts.append(u)
        else:
            if op == "split" and len(a) > 1:
                musts.append(set(a))
            cannots.append((a, b))
    return musts, cannots


# ------------------------------------------------------------------------------------------------ clustering
class _UF:
    """Union-find with holder-guard and never-link tags carried on each root."""

    def __init__(self, names: Iterable[str]) -> None:
        self.parent: Dict[str, str] = {}
        self.size: Dict[str, int] = {}
        self.holders: Dict[str, Set[str]] = {}
        self.tags: Dict[str, Set[int]] = {}
        for n in names:
            self.parent[n] = n
            self.size[n] = 1

    def find(self, x: str) -> str:
        parent = self.parent
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def blocked(self, ra: str, rb: str, guard_holders: bool) -> Optional[str]:
        ta, tb = self.tags.get(ra), self.tags.get(rb)
        if ta and tb:
            small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
            if any((t ^ 1) in big for t in small):
                return "CANNOT_LINK"
        if guard_holders:
            ha, hb = self.holders.get(ra), self.holders.get(rb)
            if ha and hb and ha.isdisjoint(hb):
                return "HOLDER_GUARD"
        return None

    def union(self, ra: str, rb: str) -> str:
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        for store in (self.holders, self.tags):
            other = store.pop(rb, None)
            if other:
                store.setdefault(ra, set()).update(other)
        return ra


class _Resolution:
    __slots__ = ("clusters", "holder_raw", "statement_holders", "holder_accounts", "name_to_accs", "name_to_vpas",
                 "name_to_pans", "system_names", "evidence", "conflicts", "candidates", "stats", "find")


def _tx_party(tx: NormalizedTransaction) -> str:
    cp = (tx.extracted_counterparty or "").strip().upper()
    return cp or f"{UNRESOLVED_PREFIX}{(tx.category_name or '').upper()}"


_BLOCK_CAP = 60     # blocks larger than this are compared with a sorted-neighbourhood window
_BLOCK_WINDOW = 12


def _cluster(
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    similarity_threshold: float,
    manual_merges: Optional[Dict[str, str]],
    decisions: Optional[Sequence[Dict[str, Any]]],
    candidate_floor: Optional[float] = None,
) -> _Resolution:
    t0 = time.perf_counter()
    res = _Resolution()
    evidence: List[Dict[str, Any]] = []
    conflicts: List[Dict[str, Any]] = []
    candidates: Dict[Tuple[str, str], float] = {}

    statement_holders: Set[str] = set()
    holder_accounts: Dict[str, Set[str]] = {}
    holder_raw: Set[str] = set()
    raw_names: Set[str] = set()
    name_to_vpas: Dict[str, Set[str]] = {}
    name_to_accs: Dict[str, Set[str]] = {}
    name_to_pans: Dict[str, Set[str]] = {}
    stmt_pans: Dict[str, Set[str]] = {}

    for st in statements:
        raw = (st.account_holder_name or "").strip().upper()
        if not raw:
            continue
        raw_names.add(raw)
        holder_raw.add(raw)
        norm = normalize_entity_name(raw)
        if norm:
            statement_holders.add(norm)
            holder_accounts.setdefault(norm, set()).add(st.account_number)
        name_to_accs.setdefault(raw, set()).add(st.account_number)
        for attr in ("pan", "pan_number", "holder_pan"):
            pan = getattr(st, attr, None)
            if pan and _PAN_RE.fullmatch(str(pan).strip().upper()):
                stmt_pans.setdefault(raw, set()).add(str(pan).strip().upper())

    pan_names: Dict[str, Set[str]] = {}
    for tx in transactions:
        cp = _tx_party(tx)
        raw_names.add(cp)
        ident = (tx.counterparty_vpa_or_acc or "").strip()
        if ident:
            if "@" in ident:
                name_to_vpas.setdefault(cp, set()).add(ident)
            else:
                name_to_accs.setdefault(cp, set()).add(ident)
        if "@" in cp and _VPA_RE.fullmatch(cp):
            name_to_vpas.setdefault(cp, set()).add(cp)
        pans = extract_pans(tx.raw_narration)
        if len(pans) == 1 and not cp.startswith(UNRESOLVED_PREFIX):
            name_to_pans.setdefault(cp, set()).add(pans[0])
            pan_names.setdefault(pans[0], set()).add(cp)

    names_list = sorted(raw_names)
    system_names = {n for n in names_list if is_system_party_name(n) and normalize_entity_name(n) not in statement_holders}
    uf = _UF(names_list)
    for raw in holder_raw:
        key = normalize_entity_name(raw) or raw
        uf.holders[raw] = {key}

    musts, cannots = _compile_decisions(decisions or [])
    for k, (sa, sb) in enumerate(cannots):
        for side, group in ((0, sa), (1, sb)):
            for nm in group:
                if nm in uf.parent:
                    uf.tags.setdefault(uf.find(nm), set()).add(2 * k + side)

    def link(a: str, b: str, reason: str, detail: Any, force: bool = False) -> bool:
        ra, rb = uf.find(a), uf.find(b)
        if ra == rb:
            return True
        why = uf.blocked(ra, rb, guard_holders=not force)
        if why:
            conflicts.append({"type": why, "a": a, "b": b, "reason": reason, "detail": detail})
            return False
        uf.union(ra, rb)
        evidence.append({"a": a, "b": b, "reason": reason, "detail": detail})
        return True

    # 1. Analyst decisions and legacy free-text merges (bypass the holder guard, honour never-link constraints)
    for alias_name, target in sorted((manual_merges or {}).items()):
        au, tu = alias_name.strip().upper(), str(target).strip().upper()
        if au in uf.parent and tu in uf.parent:
            link(tu, au, "MANUAL", None, force=True)
    for group in musts:
        present = sorted(n for n in group if n in uf.parent)
        for other in present[1:]:
            link(present[0], other, "DECISION", None, force=True)

    # 2. Statement-level identity: the same full account number (or PAN) on two statements is one holder.
    acc_holder: Dict[str, str] = {}
    pan_holder: Dict[str, str] = {}
    for st in sorted(statements, key=lambda s: ((s.account_holder_name or "").strip().upper(), s.account_number or "")):
        raw = (st.account_holder_name or "").strip().upper()
        if not raw:
            continue
        acc = full_account_number(st.account_number)
        if acc:
            if acc in acc_holder:
                link(acc_holder[acc], raw, "STATEMENT_ACCOUNT", acc, force=True)
            else:
                acc_holder[acc] = raw
        for pan in sorted(stmt_pans.get(raw, ())):
            if pan in pan_holder:
                link(pan_holder[pan], raw, "STATEMENT_PAN", pan, force=True)
            else:
                pan_holder[pan] = raw

    # 3. Identifier linking (system parties never link: a shared ATM/charges reference must not glue people).
    acc_owner: Dict[str, str] = dict(acc_holder)
    for nm in names_list:
        if nm in system_names or nm in holder_raw:
            continue
        for raw_acc in sorted(name_to_accs.get(nm, ())):
            acc = full_account_number(raw_acc)
            if not acc:
                continue
            if acc in acc_owner:
                link(acc_owner[acc], nm, "ACCOUNT", acc)
            else:
                acc_owner[acc] = nm
    vpa_owner: Dict[str, str] = {}
    for nm in names_list:
        if nm in system_names:
            continue
        for v in sorted(name_to_vpas.get(nm, ())):
            key = v.strip().lower()
            if key in vpa_owner:
                link(vpa_owner[key], nm, "VPA", v)
            else:
                vpa_owner[key] = nm
    for pan in sorted(pan_names):
        owners = sorted(n for n in pan_names[pan] if n not in system_names)
        if pan in pan_holder:
            owners = [pan_holder[pan]] + [o for o in owners if o != pan_holder[pan]]
        distinct = {normalize_entity_name(o) for o in owners}
        if len(distinct) > 2 and pan not in pan_holder:
            # A PAN quoted next to many different parties is usually the holder's own (TDS/GST narrations).
            conflicts.append({"type": "PAN_AMBIGUOUS", "a": owners[0], "b": owners[1], "reason": "PAN",
                              "detail": {"pan": pan, "names": owners[:10]}})
            continue
        for o in owners[1:]:
            link(owners[0], o, "PAN", pan)

    # 4. Fuzzy + phonetic clustering with blocking (NLP-05). Identical normalised names link directly.
    by_norm: Dict[str, List[str]] = {}
    for nm in names_list:
        if nm in system_names:
            continue
        norm = normalize_entity_name(nm)
        if norm:
            by_norm.setdefault(norm, []).append(nm)
    for norm, members in by_norm.items():
        for other in members[1:]:
            link(members[0], other, "NAME", 1.0)

    reps = sorted(by_norm)
    feats = [_features(n) for n in reps]
    df: Dict[str, int] = {}
    for f in feats:
        for pt in set(f.ptoks):
            df[pt] = df.get(pt, 0) + 1
    blocks: Dict[str, List[int]] = {}
    for idx, f in enumerate(feats):
        keys: Set[str] = set()
        usable = sorted((pt for pt in set(f.ptoks) if len(pt) >= 2 and not pt.startswith("#")),
                        key=lambda p: (df[p], p))
        # The two rarest phonetic tokens: a true variant pair almost always shares one of them exactly.
        keys.update("T:" + pt for pt in usable[:2])
        if usable and len(usable[0]) >= 4:
            # Typo-tolerant keys on the rarest token: its phonetic head and tail.
            keys.add("P:" + usable[0][:3])
            keys.add("S:" + usable[0][-3:])
        if len(f.pcompact) >= 4:
            keys.add("C:" + f.pcompact[:4])
        if len(f.compact) >= 4:
            keys.add("R:" + f.compact[:4])
        for k in keys:
            blocks.setdefault(k, []).append(idx)

    floor = similarity_threshold if candidate_floor is None else min(similarity_threshold, candidate_floor)
    seen: Set[Tuple[int, int]] = set()
    comparisons = 0
    for key in sorted(blocks):
        members = blocks[key]
        if len(members) < 2:
            continue
        if len(members) <= _BLOCK_CAP:
            pairs: Iterable[Tuple[int, int]] = ((members[i], members[j]) for i in range(len(members))
                                                 for j in range(i + 1, len(members)))
        else:
            ordered = sorted(members, key=lambda i: (feats[i].pcompact, i))
            pairs = ((ordered[i], ordered[j]) for i in range(len(ordered))
                     for j in range(i + 1, min(len(ordered), i + 1 + _BLOCK_WINDOW)))
        for i, j in pairs:
            if i > j:
                i, j = j, i
            if (i, j) in seen:
                continue
            seen.add((i, j))
            a, b = by_norm[reps[i]][0], by_norm[reps[j]][0]
            if uf.find(a) == uf.find(b):
                continue
            comparisons += 1
            sim = _score_features(feats[i], feats[j], floor)
            if sim >= similarity_threshold:
                if not link(a, b, "NAME", sim) and candidate_floor is not None:
                    candidates[(a, b)] = sim
            elif candidate_floor is not None and sim >= candidate_floor:
                candidates[(a, b)] = sim

    clusters: Dict[str, List[str]] = {}
    for nm in names_list:
        clusters.setdefault(uf.find(nm), []).append(nm)

    res.clusters = clusters
    res.holder_raw = holder_raw
    res.statement_holders = statement_holders
    res.holder_accounts = holder_accounts
    res.name_to_accs = name_to_accs
    res.name_to_vpas = name_to_vpas
    res.name_to_pans = name_to_pans
    res.system_names = system_names
    res.evidence = evidence
    res.conflicts = conflicts
    res.candidates = [{"name_a": a, "name_b": b, "similarity": s} for (a, b), s in candidates.items()]
    res.find = uf.find
    res.stats = {
        "names": len(names_list),
        "distinct_normalised": len(reps),
        "blocks": len(blocks),
        "comparisons": comparisons,
        "elapsed_ms": round((time.perf_counter() - t0) * 1000.0, 1),
    }
    return res


def _anchor_id(case_id: str, alias: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{case_id}:{alias}"))


def build_alias_map(entities: Iterable[Any]) -> Dict[str, str]:
    """{ALIAS (upper) -> entity_id} for a previously persisted entity list (ResolvedEntity or dicts)."""
    out: Dict[str, str] = {}
    for e in entities or []:
        eid = _ent_get(e, "entity_id")
        if not eid:
            continue
        for a in _alias_set(e):
            out.setdefault(a, eid)
    return out


def _assign_ids(case_id: str, ordered: List[Tuple[str, List[str], List[str]]],
                previous_entities: Optional[Iterable[Any]],
                decisions: Optional[Sequence[Dict[str, Any]]] = None) -> Dict[str, str]:
    """
    NLP-12: stable entity IDs. A cluster inherits the previous entity ID it overlaps most (alias map), else
    uuid5(case:anchor) where the anchor is its smallest statement-holder alias (or smallest alias). Because the
    anchor of a union is the anchor of one of its parts, a merge keeps one constituent's ID and a split hands
    the ID back to the part that holds the anchor.
    """
    used: Set[str] = set()
    out: Dict[str, str] = {}
    prev_sets: Dict[str, Set[str]] = {}
    for e in previous_entities or []:
        eid = _ent_get(e, "entity_id")
        if eid:
            prev_sets.setdefault(eid, set()).update(_alias_set(e))
    # Merge decisions name their target entity: the merged cluster keeps the target's ID.
    pref: Dict[str, int] = {}
    for d in decisions or []:
        ids_ = d.get("entity_ids") or []
        groups = d.get("groups") or []
        if str(d.get("op")) != "merge" or len(ids_) != 2 or len(groups) != 2:
            continue
        for eid, grp, weight in ((ids_[0], groups[0], -1), (ids_[1], groups[1], 1)):
            if eid not in prev_sets:
                prev_sets[eid] = {str(a).strip().upper() for a in grp}
            pref[eid] = weight
    if prev_sets:
        alias_to_prev: Dict[str, Set[str]] = {}
        for eid, al in prev_sets.items():
            for a in al:
                alias_to_prev.setdefault(a, set()).add(eid)
        scored: List[Tuple[int, int, str, str]] = []
        for root, members, _ in ordered:
            counts: Dict[str, int] = {}
            for m in members:
                for eid in alias_to_prev.get(m, ()):
                    counts[eid] = counts.get(eid, 0) + 1
            for eid, cnt in counts.items():
                anchored = int(any(_anchor_id(case_id, m) == eid for m in members if m in prev_sets[eid]))
                scored.append((-pref.get(eid, 0), -cnt, -anchored, eid, root))
        for _pref, _cnt, _anch, eid, root in sorted(scored):
            if root in out or eid in used:
                continue
            out[root] = eid
            used.add(eid)
    for root, members, anchors in ordered:
        if root in out:
            continue
        eid = None
        for a in anchors + [m for m in members if m not in anchors]:
            cand = _anchor_id(case_id, a)
            if cand not in used:
                eid = cand
                break
        n = 1
        while eid is None or eid in used:
            eid = _anchor_id(case_id, f"{anchors[0] if anchors else members[0]}#{n}")
            n += 1
        out[root] = eid
        used.add(eid)
    return out


def _build_entities(case_id: str, res: _Resolution, previous_entities: Optional[Iterable[Any]],
                    decisions: Optional[Sequence[Dict[str, Any]]] = None) -> Tuple[
        List[ResolvedEntity], Dict[str, str], Dict[str, str]]:
    ordered: List[Tuple[str, List[str], List[str]]] = []
    for root, members in res.clusters.items():
        members = sorted(members)
        holders = [m for m in members if m in res.holder_raw]
        anchors = holders or sorted(m for m in members if "@" not in m and not m.startswith(UNRESOLVED_PREFIX)) or members
        ordered.append((root, members, anchors))
    # Holder clusters first, then larger clusters, then by anchor: deterministic and independent of input order.
    ordered.sort(key=lambda r: (0 if r[2] and r[2][0] in res.holder_raw else 1, -len(r[1]), r[2][0]))
    previous_entities = list(previous_entities) if previous_entities is not None else None
    ids = _assign_ids(case_id, ordered, previous_entities, decisions)
    # A merged entity keeps the target's ID, so it should also keep the target's name ("merged into target").
    prev_canon = {str(_ent_get(e, "entity_id")): str(_ent_get(e, "canonical_name") or "").strip().upper()
                  for e in (previous_entities or [])}
    preferred: Dict[str, str] = {}
    for d in decisions or []:
        eids = d.get("entity_ids") or []
        if str(d.get("op") or "").lower() == "merge" and len(eids) >= 2:
            canon = prev_canon.get(str(eids[1])) or next(
                (str(a).strip().upper() for a in sorted((d.get("groups") or [[], []])[1], key=len, reverse=True)), ""
            )
            if canon:
                preferred[str(eids[1])] = canon

    name_to_entity_id: Dict[str, str] = {}
    entities: List[ResolvedEntity] = []
    for root, members, _anchors in ordered:
        holder_members = [m for m in members if normalize_entity_name(m) in res.statement_holders]
        if holder_members:
            canon = max(holder_members, key=len)
            has_stmt = True
        else:
            pool = [m for m in members if "@" not in m] or members
            canon = max(pool, key=len)
            has_stmt = False
        eid = ids[root]
        want = preferred.get(eid)
        if want and want in members and (not holder_members or want in holder_members):
            canon = want
        is_sys = not has_stmt and all(m in res.system_names for m in members)
        accs: Set[str] = set()
        vpas: Set[str] = set()
        for m in members:
            name_to_entity_id[m] = eid
            accs.update(res.name_to_accs.get(m, set()))
            vpas.update(res.name_to_vpas.get(m, set()))
            accs.update(res.holder_accounts.get(normalize_entity_name(m), set()))
        ent = ResolvedEntity(
            entity_id=eid,
            case_id=case_id,
            canonical_name=canon,
            aliases=sorted(set(members)),
            linked_account_numbers=sorted(accs),
            linked_vpas=sorted(vpas),
            has_statement=has_stmt,
            total_credited=Decimal("0.00"),
            total_debited=Decimal("0.00"),
            tx_count=0,
        )
        # Runtime flag (models.ResolvedEntity has no persisted field yet); is_system_entity() re-derives it
        # from the canonical name after a storage round-trip.
        ent.is_system = is_sys  # type: ignore[attr-defined]
        entities.append(ent)
    root_to_id = {root: ids[root] for root, _m, _a in ordered}
    return entities, name_to_entity_id, root_to_id


def resolve_case_entities(
    case_id: str,
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    similarity_threshold: float = 0.85,
    manual_merges: Optional[Dict[str, str]] = None,
    decisions: Optional[Sequence[Dict[str, Any]]] = None,
    previous_entities: Optional[Iterable[Any]] = None,
    diagnostics: Optional[Dict[str, Any]] = None,
) -> Tuple[List[ResolvedEntity], List[NormalizedTransaction]]:
    """
    Consolidate statement holders and extracted transaction counterparties into unified ResolvedEntity objects.
    Updates `tx.resolved_entity_id` on every transaction.

    Linking order: analyst decisions / legacy manual merges -> same account or PAN across statements ->
    full account number / VPA / PAN shared by counterparties -> blocked fuzzy + Indic-phonetic name matching.
    Two distinct statement holders are never merged except by an identifier on both statements or an analyst
    decision (NLP-13). ``previous_entities`` keeps entity IDs stable across re-clusters (NLP-12). When a dict is
    passed as ``diagnostics`` it is filled with ``evidence``, ``conflicts`` and ``stats``.
    """
    res = _cluster(statements, transactions, similarity_threshold, manual_merges, decisions)
    entities, name_to_entity_id, _ = _build_entities(case_id, res, previous_entities, decisions)
    entity_map = {e.entity_id: e for e in entities}
    for tx in transactions:
        eid = name_to_entity_id.get(_tx_party(tx))
        tx.resolved_entity_id = eid
        if eid and eid in entity_map:
            ent = entity_map[eid]
            ent.total_credited += tx.credit
            ent.total_debited += tx.debit
            ent.tx_count += 1
    if diagnostics is not None:
        diagnostics["evidence"] = res.evidence
        diagnostics["conflicts"] = res.conflicts
        diagnostics["stats"] = res.stats
    return entities, transactions


def preview_clusters(
    case_id: str,
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    similarity_threshold: float = 0.85,
    manual_merges: Optional[Dict[str, str]] = None,
    decisions: Optional[Sequence[Dict[str, Any]]] = None,
    previous_entities: Optional[Iterable[Any]] = None,
    candidate_floor: float = 0.60,
    max_candidates: int = 50,
) -> Dict[str, Any]:
    """
    Pure dry run of entity resolution at ``similarity_threshold`` for the cluster-review UI / slider preview.
    Does not mutate ``transactions``. Returns clusters (with link evidence), near-miss merge candidates across
    clusters, guard/never-link conflicts, and the change set against ``previous_entities``.
    """
    prev_list = list(previous_entities) if previous_entities is not None else None
    res = _cluster(statements, transactions, similarity_threshold, manual_merges, decisions, candidate_floor)
    entities, name_to_entity_id, root_to_id = _build_entities(case_id, res, prev_list, decisions)
    entity_map = {e.entity_id: e for e in entities}
    for tx in transactions:
        eid = name_to_entity_id.get(_tx_party(tx))
        if eid and eid in entity_map:
            ent = entity_map[eid]
            ent.total_credited += tx.credit
            ent.total_debited += tx.debit
            ent.tx_count += 1

    ev_by_eid: Dict[str, List[Dict[str, Any]]] = {}
    for ev in res.evidence:
        ev_by_eid.setdefault(name_to_entity_id[ev["a"]], []).append(ev)

    clusters = []
    for e in sorted(entities, key=lambda x: (-len(x.aliases), x.canonical_name)):
        clusters.append({
            "entity_id": e.entity_id,
            "canonical_name": e.canonical_name,
            "aliases": list(e.aliases),
            "has_statement": e.has_statement,
            "is_system": bool(getattr(e, "is_system", False)),
            "linked_account_numbers": list(e.linked_account_numbers),
            "linked_vpas": list(e.linked_vpas),
            "tx_count": e.tx_count,
            "total_credited": str(e.total_credited),
            "total_debited": str(e.total_debited),
            "evidence": ev_by_eid.get(e.entity_id, []),
        })

    # Pairs the analyst already marked as different parties (reject/split never-link constraints) are handled:
    # they leave the review queue.
    _musts, cannots = _compile_decisions(decisions or [])
    alias_sets = {e.entity_id: {str(a).strip().upper() for a in e.aliases} | {e.canonical_name.strip().upper()}
                  for e in entities}

    def _never_linked(x: str, y: str) -> bool:
        ax, ay = alias_sets.get(x, set()), alias_sets.get(y, set())
        return any((ax & p and ay & q) or (ax & q and ay & p) for p, q in cannots)

    cands: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for c in res.candidates:
        ia, ib = name_to_entity_id[c["name_a"]], name_to_entity_id[c["name_b"]]
        if ia == ib or _never_linked(ia, ib):
            continue
        key = (min(ia, ib), max(ia, ib))
        if key not in cands or cands[key]["similarity"] < c["similarity"]:
            ea, eb = entity_map[key[0]], entity_map[key[1]]
            cands[key] = {"entity_a_id": key[0], "entity_b_id": key[1], "entity_a": ea.canonical_name,
                          "entity_b": eb.canonical_name, "name_a": c["name_a"] if name_to_entity_id[c["name_a"]] == key[0] else c["name_b"],
                          "name_b": c["name_b"] if name_to_entity_id[c["name_b"]] == key[1] else c["name_a"],
                          "similarity": c["similarity"]}
    candidates = sorted(cands.values(), key=lambda x: (-x["similarity"], x["entity_a"], x["entity_b"]))[:max_candidates]

    changes: Dict[str, Any] = {"merged": [], "split": [], "new_entity_ids": [], "retired_entity_ids": []}
    if prev_list is not None:
        prev_alias = build_alias_map(prev_list)
        prev_ids = {_ent_get(e, "entity_id") for e in prev_list}
        new_ids = set(entity_map)
        for e in entities:
            olds = sorted({prev_alias[a] for a in e.aliases if a in prev_alias})
            if len(olds) > 1:
                changes["merged"].append({"entity_id": e.entity_id, "from_entity_ids": olds})
        parts: Dict[str, Set[str]] = {}
        for nm, eid in name_to_entity_id.items():
            if nm in prev_alias:
                parts.setdefault(prev_alias[nm], set()).add(eid)
        for old, news in sorted(parts.items()):
            if len(news) > 1:
                changes["split"].append({"entity_id": old, "into_entity_ids": sorted(news)})
        changes["new_entity_ids"] = sorted(new_ids - prev_ids)
        changes["retired_entity_ids"] = sorted(i for i in prev_ids - new_ids if i)

    return {
        "case_id": case_id,
        "threshold": similarity_threshold,
        "entity_count": len(entities),
        "multi_alias_clusters": sum(1 for c in clusters if len(c["aliases"]) > 1),
        "clusters": clusters,
        "candidates": candidates,
        "conflicts": res.conflicts,
        "changes": changes,
        "stats": res.stats,
    }
