"""Production-grade Indian banking narration parser for UPI, NEFT, RTGS, IMPS, NACH, AEPS, ATM, POS, and Cheque rails."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from cyphersol.models import NormalizedTransaction


@dataclass
class ParsedNarration:
    """Structured fields deterministically extracted from a raw Indian bank narration string.

    ``payment_channel`` keeps the internal channel code that the rest of the engine keys on (TRANSFER,
    CHARGES, CASH_WDL, FOREX, ...). ``srs_channel`` is the same rail in the SRS FR-CAT-01 enum (NLP-24), see
    :func:`srs_payment_channel`.

    ``counterparty_vpa_or_acc`` is the counterparty identifier: VPA, else the counterparty account number
    (full digits when the narration carries them, else the masked form). The holder's own card number on
    POS / ATM rows is never a counterparty identifier; it is reported in ``card_number_masked`` (NLP-18).
    ``cheque_number`` is the 6-digit instrument number of cheque rows (NLP-20); for backward compatibility
    it is still used as the reference (``utr_number``) of a cheque row that has no UTR.
    """

    payment_channel: str
    utr_number: Optional[str]
    counterparty_name: Optional[str]
    counterparty_vpa_or_acc: Optional[str]
    counterparty_ifsc: Optional[str]
    remarks: Optional[str] = None
    matched_grammar: Optional[str] = None
    srs_channel: Optional[str] = None
    counterparty_account: Optional[str] = None
    card_number_masked: Optional[str] = None
    cheque_number: Optional[str] = None


# ---------------------------------------------------------------------------------------------------------
# SRS FR-CAT-01 payment-channel enum (NLP-24)
# ---------------------------------------------------------------------------------------------------------

#: The payment channels named by SRS FR-CAT-01, in SRS order.
SRS_PAYMENT_CHANNELS: Tuple[str, ...] = (
    "UPI", "NEFT", "RTGS", "IMPS", "NACH", "ATM", "AEPS", "POS", "CHEQUE",
    "INTERNAL_TRANSFER", "BANK_CHARGE", "INTEREST", "CASH_CDM",
)
#: Rails the engine distinguishes that the SRS enum has no member for (kept as documented extensions).
SRS_CHANNEL_EXTENSIONS: Tuple[str, ...] = ("CASH_WDL", "FOREX", "OTHER")

# Internal channel code -> SRS enum member. Identity for everything not listed.
_INTERNAL_TO_SRS_CHANNEL: Dict[str, str] = {
    "TRANSFER": "INTERNAL_TRANSFER",
    "CHARGES": "BANK_CHARGE",
    "ACH": "NACH",
    "ECS": "NACH",
    "ECOM": "POS",
    "CLG": "CHEQUE",
    "CASH_DEPOSIT": "CASH_CDM",
    "": "OTHER",
}
# SRS enum member -> internal channel code (inverse of the renames above).
_SRS_TO_INTERNAL_CHANNEL: Dict[str, str] = {"INTERNAL_TRANSFER": "TRANSFER", "BANK_CHARGE": "CHARGES"}


def srs_payment_channel(channel: Optional[str]) -> str:
    """Map an internal channel code (or an SRS name, or an alias such as ACH/ECS/ECOM/CLG) to the SRS
    FR-CAT-01 enum. Unknown codes and the documented extensions (CASH_WDL, FOREX) pass through upper-cased;
    None/empty -> "OTHER". Idempotent."""
    ch = (channel or "").strip().upper()
    return _INTERNAL_TO_SRS_CHANNEL.get(ch, ch or "OTHER")


def internal_payment_channel(channel: Optional[str]) -> str:
    """Inverse of :func:`srs_payment_channel` for the renamed members (INTERNAL_TRANSFER -> TRANSFER,
    BANK_CHARGE -> CHARGES); everything else passes through upper-cased. Idempotent."""
    ch = (channel or "").strip().upper()
    return _SRS_TO_INTERNAL_CHANNEL.get(ch, ch or "OTHER")


_IFSC_RE = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")

# VPAs carry @psp handles and exclude standard web/email TLDs unless known UPI PSP handles
_KNOWN_UPI_PSP_HANDLES = {
    "okaxis", "okhdfcbank", "oksbi", "okicici", "okbizaxis",
    "ybl", "ibl", "axl", "paytm", "ptyes", "ptaxis", "pthdfc", "ptsbi",
    "apl", "yapl", "rapl", "upi", "icici", "hdfcbank", "sbi", "kotak",
    "axisbank", "fbl", "federal", "idfcbank", "indus", "barodampay",
    "cnrb", "pnb", "unionbank", "uboi", "mahb", "boi", "cbin", "iob",
    "sib", "kvb", "tmb", "cub", "rbl", "yesbank", "yesbankltd", "aubank",
    "equitas", "jio", "airtel", "payzapp", "slice", "jupiteraxis", "fi",
    "ikwik", "pingpay", "freecharge", "amazonpay", "waicici", "wahdfcbank",
    "waaxis", "wasbi", "naviaxis", "groww", "zerodha", "cred", "fbpe",
    "superyes", "kmbl", "kbl", "bandhan",
}
_VPA_RE = re.compile(r"(?<![A-Za-z0-9._%+])([A-Za-z0-9._+]{2,}@[A-Za-z0-9]{2,20})(?![A-Za-z0-9.])")
_VPA_HYPHENATED_RE = re.compile(
    r"(?:^|[\s/|*])((?:gpay|phonepe|paytm)-[A-Za-z0-9._+]{2,}@[A-Za-z0-9]{2,20})(?=$|[\s/|*])",
    flags=re.IGNORECASE,
)

# Match RBI SFMS NEFT/RTGS UTRs, tagged UTR/RRN refs, and 12-digit IMPS/UPI RRNs
_UTR_ALPHA_LONG_RE = re.compile(r"(?<![A-Z0-9])([A-Z]{4}[A-Z0-9]{2}\d{10,18}|[A-Z]{4}0?\d{10,18})(?![A-Z0-9])")
_UTR_SFMS_SEQ_RE = re.compile(r"(?<![A-Z0-9])([NR]\d{14,22})(?![A-Z0-9])")
_RRN_12_RE = re.compile(r"(?<![A-Z0-9])(\d{12})(?![A-Z0-9])")
# 16-digit numeric UTRs (some banks' NEFT/RTGS/IMPS references); only trusted on transfer rails because
# card numbers on POS/ATM rows have the same shape.
_UTR_NUM_16_RE = re.compile(r"(?<![A-Z0-9])(\d{16})(?![A-Z0-9])")
_UTR_16_RAILS = frozenset({"NEFT", "RTGS", "IMPS", "UPI"})
_TAGGED_REF_RE = re.compile(
    r"(?<![A-Z0-9])(?:UTR|RRN|REF\s*NO|TXN\s*REF|TRN\s*REF)(?:\s*NO)?\.?[\s:/#-]*([A-Z0-9]{10,22})(?![A-Z0-9])"
)
# Tag words and reference fragments that leak into party slots ("UTR N123... FROM RAHUL")
_PARTY_REF_FRAGMENT_RE = re.compile(
    r"(?<![A-Z0-9])(?:UTR|RRN|REF|TXN\s*REF|TRN\s*REF)(?:\s*NO)?\.?[\s:#-]*(?=[A-Z0-9]*\d)[A-Z0-9]+(?![A-Z0-9])"
)
_MS_SALUTATION_RE = re.compile(r"(?<![A-Z0-9])M\s?/\s?S\b\.?\s*(?=[A-Z])")
_PARTY_LONG_NUMBER_RE = re.compile(r"(?<![A-Z0-9])\d{6,}(?![A-Z0-9])")
_PARTY_LEAD_TAG_RE = re.compile(
    r"^(?:(?:(?:PAYMENT|PMT|PAID|RECEIVED|RECD|SENT|TRANSFER(?:RED)?|TFR|FUNDS?\s+TRANSFER)\s+(?:FROM|FRM|TO|BY)"
    r"|(?:UPI|NEFT|RTGS|IMPS|NACH|ACH|ECS)(?:\s+(?:CR|DR|IN|OUT|P2A|P2P))?"
    r"|FROM|FRM|FR|TO|BY|FOR|UTR|RRN|REF|NO|TRF|TRFR|TXN|A C|A/C|AC|ACCT|ACCOUNT|BENEFICIARY|BENE|TP"
    r"|CLG|CHQ|CHEQUE|DEP|DEPOSIT|MICR|CTS|POS)\s+)+"
)
_PARTY_TRAIL_TAG_RE = re.compile(
    r"(?:\s+(?:FROM|FRM|TO|BY|FOR|UTR|RRN|REF|NO|TRF|TRFR|TXN|A C|A/C|AC|ACCT|ACCOUNT"
    r"|CLG|CHQ|DEP|INVOICE|INV|BILL|PMT|PAYMENT))+$"
)
# Cheque/clearing residue that names the drawee bank or clearing city, not the party (NLP-17)
_CLEARING_CITY_WORDS = frozenset({
    "MUMBAI", "BOMBAY", "DELHI", "NEW", "CHENNAI", "MADRAS", "KOLKATA", "CALCUTTA", "BANGALORE", "BENGALURU",
    "HYDERABAD", "SECUNDERABAD", "PUNE", "AHMEDABAD", "SURAT", "JAIPUR", "LUCKNOW", "KANPUR", "NAGPUR", "INDORE",
    "BHOPAL", "PATNA", "THANE", "NAVI", "NOIDA", "GURGAON", "GURUGRAM", "CHANDIGARH", "KOCHI", "COCHIN",
    "COIMBATORE", "VADODARA", "BARODA", "LUDHIANA", "NASHIK", "RAJKOT", "VISAKHAPATNAM", "MYSORE", "MYSURU",
    "GOA", "PANAJI", "BHUBANESWAR", "GUWAHATI", "RANCHI", "RAIPUR", "DEHRADUN", "AMRITSAR", "VARANASI",
    "AGRA", "MADURAI", "TRICHY", "MANGALORE", "MANGALURU", "HUBLI", "BELGAUM", "BELAGAVI", "KOLHAPUR",
    "AURANGABAD", "JODHPUR", "UDAIPUR", "KOTA", "ALLAHABAD", "PRAYAGRAJ", "MEERUT", "GHAZIABAD", "FARIDABAD",
})
_CLEARING_NOISE_WORDS = frozenset({"BANK", "LTD", "LIMITED", "BR", "BRANCH", "MAIN", "CITY", "HO", "RO", "CLG", "CTS",
                                   "CHQ", "DEP", "DEPOSIT", "INWARD", "OUTWARD", "RETURN", "ZONE", "GRID"})
# A/C fragments with their number ("A/C 30112233445", "A/C NO: XX1234") are identifiers, not party text
_PARTY_ACC_FRAGMENT_RE = re.compile(
    r"(?<![A-Z0-9])(?:A/C|A/C\.|AC|ACCT|ACCOUNT)(?:\s*NO)?\.?[\s:#-]*[X*\d]{3,18}(?![A-Z0-9])"
)
# Clearing location "<CITY> <6-digit PIN>" on cheque rows (MUMBAI 400002): neither party nor cheque number
_CITY_PIN_RE = re.compile(r"(?<![A-Z0-9])[A-Z]{3,}\s+[1-8]\d{5}(?![A-Z0-9])")

# Cheque numbers (NLP-20): a 6-digit instrument number (leading zeros allowed) that is either tagged
# (CHQ / CHEQUE / INST / CLG / CTS / MICR, optionally followed by NO / DEP / PAID ...), or forms a whole
# delimited slot of its own on a cheque row. A bare 6-digit number elsewhere (PIN code, branch code, date,
# MICR digits) is never a cheque number.
_CHQ_TAGGED_RE = re.compile(
    r"(?<![A-Z0-9])(?:CHQ|CHEQUE|CHEQ|INST(?:RUMENT)?|CLG|CTS|MICR)"
    r"(?:[\s.:#-]*(?:NO|NUM|NUMBER|DEP|DEPOSIT|PAID|ISSUED|RET|RETURN|RTN|CLG|CTS|INWARD|OUTWARD|IW|OW))*"
    r"[\s.:/#-]*0{0,4}(\d{6})(?![\d])"
)
_CHQ_SLOT_RE = re.compile(r"^0{0,4}(\d{6})$")

# Counterparty account numbers (NLP-18)
_ACC_TAGGED_RE = re.compile(
    r"(?<![A-Z0-9])(?:BENEF(?:ICIARY)?\s*A/?C|A/C|AC\s*NO|ACCT|ACCOUNT)(?:\s*NO)?\.?[\s:#-]*"
    r"([X*]{0,14}\d[X*\d]{2,17})(?![A-Z0-9])"
)
_ACC_TRF_PHRASE_RE = re.compile(
    r"(?<![A-Z0-9])(?:TRF|TRFR|TFR|TRANSFER(?:RED)?)\s+(?:FR|FROM|TO)\s*[:#-]?\s*(\d{9,18})(?![A-Z0-9])"
)
_ACC_MASKED_UNTAGGED_RE = re.compile(r"(?<![A-Z0-9])([X*]{2,}\d{3,8})(?![A-Z0-9])")
_CARD_MASKED_RE = re.compile(r"(?<![A-Z0-9])(\d{4,6}[X*]{4,8}\d{4})(?![A-Z0-9])")
_FULL_ACC_SLOT_RE = re.compile(r"^\d{9,18}$")
_ACC_SLOT_CHANNELS = frozenset({"IMPS", "NEFT", "RTGS", "TRANSFER"})
# Legacy combined pattern kept for callers that only need "some masked/tagged account-like token"
_ACC_MASKED_RE = re.compile(r"(?:A/C[\s:#-]*([X*\d]{6,18})|\b([X*]{2,}\d{3,8})\b|\b(\d{4,6}[X*]{4,}\d{4})\b)")

# Major RBI Scheduled Bank codes / abbreviations appearing in UPI & IMPS bank slots
_KNOWN_BANK_CODES = {
    "SBIN", "HDFC", "ICIC", "UTIB", "KKBK", "PUNB", "BARB", "CNRB", "UBIN",
    "IDIB", "BKID", "CBIN", "IOBA", "MAHB", "PSIB", "UCBA", "FDRL", "IDFB",
    "INDB", "YESB", "RATN", "KARB", "KVBL", "TMBL", "CIUB", "SIBL", "DCBL",
    "CSBK", "JAKA", "DLXB", "AIRP", "PYTM", "FINO", "JIOP", "AUBL", "ESFB",
    "USFB", "JSFB", "SURY", "NESF", "UJVN", "NSPB", "HSBC", "CITI", "SCBL",
    "DEUT", "DBSS", "STCB", "BARC", "BOFA", "JPMC", "BNPA", "ANDB", "CORP",
    "ORBC", "SYNB", "VIJB", "DENA", "ALLA", "UTBI", "IBKL",
    "SBI", "HDF", "ICI", "AXI", "AXIS", "KOT", "KOTAK", "PNB", "BOB", "CNB",
    "CAN", "UBI", "IDBI", "BOI", "CBI", "IOB", "UCO", "FED", "IND", "YES",
    "RBL", "CUB", "KVB", "TMB", "SIB", "DCB", "AIR", "PYT", "JIO", "AUB",
    "FIN", "PAYTM", "ICICI", "HDFCBANK", "AXISBANK",
}

_KNOWN_BANK_CODES.update({"IPOS", "BKID", "SBIN", "UBIN", "PKGB", "HDFC", "ICIC", "UTIB", "KKBK", "PUNB", "BARB", "CNRB", "IDFB", "YESB", "FDRL"})
_KNOWN_UPI_PSP_HANDLES.update(
    {"ax", "axl", "okaxis", "okhdfcbank", "okicici", "oksbi", "ybl", "ibl", "apl", "paytm", "ptaxis", "ptyes", "pthdfc", "ptsbi", "pingpay", "jio", "ippb"}
)

_BANK_SLOT_NAMES = {
    "STATE BANK OF INDIA", "HDFC BANK", "HDFC BANK LTD", "ICICI BANK", "ICICI BANK LTD",
    "AXIS BANK", "AXIS BANK LTD", "KOTAK MAHINDRA BANK", "KOTAK BANK", "YES BANK",
    "PUNJAB NATIONAL BANK", "BANK OF BARODA", "CANARA BANK", "UNION BANK OF INDIA",
    "INDUSIND BANK", "IDFC FIRST BANK", "IDFC BANK", "FEDERAL BANK", "INDIAN BANK",
    "BANK OF INDIA", "CENTRAL BANK OF INDIA", "IDBI BANK", "RBL BANK", "BANDHAN BANK",
    "PAYTM PAYMENTS BANK", "AIRTEL PAYMENTS BANK", "AU SMALL FINANCE BANK",
    "KARNATAKA BANK", "SOUTH INDIAN BANK", "KARUR VYSYA BANK", "CITY UNION BANK",
}

# Generic transaction descriptions/remarks that frequently occupy UPI/IMPS slots when no party name is sent
_GENERIC_REMARK_PHRASES = {
    "PAYMENT", "PAYMENTS", "PAID", "PAY", "TRANSFER", "FUNDS TRANSFER", "FUND TRANSFER",
    "PAYMENT FROM PHONE", "SENT FROM PAYTM", "SENT USING PAYTM", "PAID VIA CRED",
    "UPI", "UPI PAYMENT", "UPI TXN", "UPI TRANSFER", "UPI MANDATE", "UPILITE",
    "NA", "N A", "NONE", "NIL", "NULL", "NOT APPLICABLE", "OTHERS", "OTHER",
    "BILL PAYMENT", "BILLPAY", "RECHARGE", "ORDER", "PURCHASE", "SHOPPING",
    "COLLECT", "COLLECT REQUEST", "INTENT", "QR PAY", "SCAN AND PAY", "QR",
    "SEND MONEY", "RECEIVE MONEY", "REQUEST", "MERCHANT", "P2M", "P2P", "P2A",
    "TEST", "TXN", "TRANSACTION", "RENT", "INVOICE", "EXPENSE", "GROCERIES",
    "DINNER", "LUNCH", "COFFEE", "FOOD", "CAB", "TRIP", "TRAVEL", "FUEL",
    "SETTLEMENT", "REFUND", "REVERSAL", "REV", "AUTO", "SELF", "OWN ACCOUNT",
    "NO REMARKS", "REMARK", "REMARKS", "VERIFIED", "OK", "DONE", "MB", "IB", "INB",
    "CR", "DR", "IN", "OUT", "BY TRANSFER", "TO TRANSFER", "BULK POSTING",
}

_LEADING_WRAPPER_RE = re.compile(
    r"^(?:(?:DEP|WDL)\s+(?:TFR|TRANSFER)|(?:DEBIT|CREDIT)\s+(?=(?:UPI|NEFT|RTGS|IMPS|ACH|NACH|INB|BY|TO|TRANSFER|CHQ|CLG|ATW|ATM))|"
    r"(?:BY|TO)\s+(?:TRANSFER|TRF|CLG|CLEARING)|TRFR\s+(?:FROM|TO)|FUNDS\s+TRANSFER|"
    r"IB\s+BILLPAY\s+(?:DR|CR)|BULK\s+POSTING|(?:MOB|MB|IB|INB)(?![A-Z]))\s*[-:/]*\s*",
    flags=re.IGNORECASE,
)


def _strip_leading_wrappers(narr_upper: str) -> str:
    cur = narr_upper.strip()
    for _ in range(3):
        nxt = _LEADING_WRAPPER_RE.sub("", cur).strip()
        if nxt == cur:
            break
        cur = nxt
    return cur


def _preprocess_ocr_narration(raw_narr: str) -> str:
    """Normalize OCR rail typos, strip trailing SBI journal stamps, and stitch mid-word/mid-slash wraps."""
    s = re.sub(r"\s+", " ", (raw_narr or "").strip())
    if not s:
        return ""
    # Normalize common OCR payment-rail misreads and strip leading account-number colons
    s = re.sub(r"^\d{10,18}\s*:\s*", "", s)
    s = re.sub(r"(?:^|[:\s])(?:[:!13I|]+\s*nt|[3I]nt)\.?\s*P[da]\b", " Int.Pd", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\b(\d{10})[38](ax|ybl|ibl|okaxis|okicici|oksbi|okhdfcbank|paytm)\b", r"\1@\2", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:UPV|UPl)\s*/", "UPI/", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:VPIAB|CPIAB|UPIA3|UPIA8)\s*/", "UPIAB/", s, flags=re.IGNORECASE)
    s = re.sub(r"\b(?:VPIAR|CPIAR)\s*/", "UPIAR/", s, flags=re.IGNORECASE)
    s = re.sub(r"\bNFFTRB\b", "NEFT RBI", s, flags=re.IGNORECASE)
    s = re.sub(r"\bNFFT\b", "NEFT", s, flags=re.IGNORECASE)
    s = re.sub(r"\bAEPS\s+TRE\b", "AEPS TRF", s, flags=re.IGNORECASE)
    s = re.sub(r"\bACH\s*(DR|CR)\b", r"ACH \1", s, flags=re.IGNORECASE)
    s = re.sub(r"\bINT\.?\s*PA\b", "Int.Pd", s, flags=re.IGNORECASE)
    s = re.sub(r"\bGRUHA\s*LAKSHM[1I]\s*SCH\b", "GRUHA LAKSHMI SCHEME", s, flags=re.IGNORECASE)
    s = re.sub(r"\bGRUHALAK\s*SHMI\b", "GRUHA LAKSHMI SCHEME", s, flags=re.IGNORECASE)
    s = re.sub(r"\bSOKASPARSH\b", "GOKASPARSH", s, flags=re.IGNORECASE)
    s = re.sub(r"\bRY\s+TRANSFOR\b", "BY TRANSFER", s, flags=re.IGNORECASE)
    # Strip trailing SBI journal/branch stamp (`<journal_digits> AT <branch_code> <branch_name>`)
    s = re.sub(r"(?:\s+\d{6,16})+\s+AT\s+\d{4,6}\b.*$", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"\s+AT\s+\d{5,6}(?:\s+[A-Z]{2,}.*)?$", "", s, flags=re.IGNORECASE).strip()
    # Stitch broken 4-letter bank codes inside slash-delimited rails (`PUNEET/B KID/` -> `PUNEET/BKID/`)
    if "/" in s:
        s = re.sub(
            r"(?<=/)\s*([A-Za-z]{1,3})\s+([A-Za-z]{1,3})(?=\s*/|$)",
            lambda m: (m.group(1) + m.group(2)).upper()
            if (m.group(1) + m.group(2)).upper() in _KNOWN_BANK_CODES
            else m.group(0),
            s,
        )
        # Stitch single-letter line wrap right before `/BANK` (`RAJES H/IPOS` -> `RAJESH/IPOS`)
        s = re.sub(
            r"\b([A-Za-z]{5,})\s+([A-Za-z])(?=/([A-Za-z0-9*]{3,}))",
            lambda m: (m.group(1) + m.group(2))
            if (m.group(3).upper() in _KNOWN_BANK_CODES or m.group(3).startswith("*"))
            else m.group(0),
            s,
        )
        # Remove accidental space directly before/after slash inside UPI/IMPS/NEFT tokens
        if re.search(r"\b(?:UPI|IMPS|NEFT|RTGS|AEPS)\S*/", s, re.IGNORECASE):
            s = re.sub(r"(?<=[A-Za-z0-9*])\s*/\s*(?=[A-Za-z0-9*])", "/", s)
    return s


_UPI_CONTEXT_RE = re.compile(r"(?<![A-Z0-9])(?:UPI(?:AR|AB|P2P|P2M|LITE|MANDATE)?|BHIM|VPA)(?![A-Z0-9])", re.IGNORECASE)
# Handle shapes used by bank / PSP handles not (yet) on the whitelist: ok<bank>, pt<bank>, wa<bank>, <x>bank
_PSP_HANDLE_SHAPE_RE = re.compile(r"^(?:ok[a-z]{2,}|pt[a-z]{2,}|wa[a-z]{2,}|[a-z]{2,}bank(?:ltd)?|[a-z]{2,}pay)$")


def is_known_psp_handle(handle: Optional[str]) -> bool:
    """True when ``handle`` (the part after '@', any case) is a known UPI PSP handle or has a PSP shape."""
    h = (handle or "").strip().lower()
    return bool(h) and (h in _KNOWN_UPI_PSP_HANDLES or bool(_PSP_HANDLE_SHAPE_RE.match(h)))


def _extract_vpa(narr: str, upi_context: Optional[bool] = None) -> Optional[str]:
    """First UPI VPA in the narration (lower-cased), or None.

    The PSP whitelist decides (NLP-23): a handle on the whitelist (or with a PSP shape such as ok<bank>) is
    always a VPA; any other ``x@y`` token is a VPA only in a UPI context (the narration names the UPI/BHIM
    rail, or the caller already knows the row is UPI). E-mail addresses never match (the handle cannot be
    followed by a dot).
    """
    if upi_context is None:
        upi_context = bool(_UPI_CONTEXT_RE.search(narr or ""))
    for pattern in (_VPA_HYPHENATED_RE, _VPA_RE):
        for m in pattern.finditer(narr):
            cand = m.group(1)
            handle = cand.split("@", 1)[1]
            if is_known_psp_handle(handle) or upi_context:
                return cand.lower()
    return None


def _extract_utr(core_upper: str, ifsc: Optional[str], channel: Optional[str] = None) -> Optional[str]:
    for tagged in _TAGGED_REF_RE.finditer(core_upper):
        val = tagged.group(1)
        if val != ifsc and any(c.isdigit() for c in val):
            return val

    for m in _UTR_ALPHA_LONG_RE.finditer(core_upper):
        val = m.group(1)
        if val != ifsc and len(val) >= 14:
            return val

    m_sfms = _UTR_SFMS_SEQ_RE.search(core_upper)
    if m_sfms:
        return m_sfms.group(1)

    m_rrn = _RRN_12_RE.search(core_upper)
    if m_rrn:
        return m_rrn.group(1)

    if channel in _UTR_16_RAILS:
        m_16 = _UTR_NUM_16_RE.search(core_upper)
        if m_16:
            return m_16.group(1)

    m_bil = re.match(r"^(?:BIL|INF|INB|TPT|TRF|TRFR|IFT)/(?:INFT|ONL|BPAY|IFT|TRFR|TXN)?/?(\d{6,14})(?:/|$)", core_upper)
    if m_bil:
        return m_bil.group(1)

    m_tagged = re.search(r"(?:^|[/*\-\s])(UTR\d{5,14}|[A-Z]{2,5}\d{6,14})(?=$|[/*\-\s])", core_upper)
    if m_tagged:
        val = m_tagged.group(1)
        if val != ifsc and not _IFSC_RE.fullmatch(val):
            return val
    return None


def _extract_masked_account(core_upper: str) -> Optional[str]:
    m = _ACC_MASKED_RE.search(core_upper)
    if not m:
        return None
    for grp in m.groups():
        if grp:
            return grp
    return None


def _extract_card(core_upper: str) -> Optional[str]:
    """Masked card number (432198XXXXXX1234) on card rails; it identifies the holder's card, not a party."""
    m = _CARD_MASKED_RE.search(core_upper)
    return m.group(1) if m else None


def extract_counterparty_account(
    core_upper: str, channel: Optional[str] = None, utr: Optional[str] = None
) -> Optional[str]:
    """Counterparty account number in a narration (NLP-18): full digits where the narration carries them,
    else the masked form. Grammar, in order:

    1. tagged: ``A/C``, ``A/C NO``, ``AC NO``, ``ACCT``, ``ACCOUNT``, ``BENEFICIARY A/C`` + number;
    2. transfer phrase: ``TRF FROM|TO <9-18 digits>``;
    3. on IMPS / NEFT / RTGS / intra-bank transfer rows: a whole delimited slot of 9-18 digits that is not
       the row's UTR/RRN (``IMPS/P2A/<rrn>/<name>/<account>/<ifsc>``);
    4. an untagged masked account (``XX1234``, ``XXXXXXXX7788``).

    Masked card numbers (``4321XXXXXXXX1234``) are never returned. Returns None when nothing matches.
    """
    up = (core_upper or "").upper()
    ch = (channel or "").upper()
    for m in _ACC_TAGGED_RE.finditer(up):
        val = m.group(1)
        if val != utr and not _CARD_MASKED_RE.fullmatch(val):
            return val
    m = _ACC_TRF_PHRASE_RE.search(up)
    if m and m.group(1) != utr:
        return m.group(1)
    if ch in _ACC_SLOT_CHANNELS or ch == "INTERNAL_TRANSFER":
        for slot in re.split(r"[/|*]+|(?<=\d)-|-(?=\d)|\s+", up):
            slot = slot.strip()
            if not slot or slot == utr or not _FULL_ACC_SLOT_RE.match(slot):
                continue
            # 12-digit numbers on IMPS are RRNs; a second 12-digit slot is still an account
            if utr is None and ch == "IMPS" and len(slot) == 12:
                continue
            return slot
    m = _ACC_MASKED_UNTAGGED_RE.search(up)
    if m:
        return m.group(1)
    return None


_CORP_SUFFIXES = {"PVT", "LTD", "LLP", "INC", "CORP", "LIMITED", "PRIVATE", "TRADERS", "ENTERPRISES", "SOLUTIONS", "INDUSTRIES", "TRUST", "FOUNDATION", "HOSPITAL", "CLINIC"}


def _looks_like_remark(text: str) -> bool:
    upper = text.upper().strip()
    if upper in _GENERIC_REMARK_PHRASES:
        return True
    words = upper.split()
    if not words:
        return True
    if any(w in _CORP_SUFFIXES for w in words):
        return False
    first_word = words[0]
    if len(words) <= 3 and first_word in {"RENT", "SALARY", "EMI", "LOAN", "FEE", "FEES", "BILL", "INV", "INVOICE", "ADV", "ADVANCE", "ORDER", "REF", "MONTHLY", "REPAYMENT", "MAINTENANCE", "TOWARDS", "PART"}:
        return True
    return False


def _clean_party_text(candidate: str) -> Optional[str]:
    """Normalize a candidate counterparty slot while preserving full business names (e.g. PAGE INDUSTRIES)."""
    txt = re.sub(r"[^A-Za-z0-9 .&'/-]", " ", candidate or "").strip()
    txt = txt.replace("/", " ").strip()
    txt = re.sub(r"\s+", " ", txt)
    if txt:
        up = txt.upper()
        stripped = _PARTY_REF_FRAGMENT_RE.sub(" ", up)
        stripped = _PARTY_LONG_NUMBER_RE.sub(" ", stripped)
        stripped = re.sub(r"\s+", " ", stripped).strip(" .-")
        stripped = _PARTY_TRAIL_TAG_RE.sub("", _PARTY_LEAD_TAG_RE.sub("", stripped)).strip(" .-")
        if stripped != up:
            txt = stripped
    if not txt or len(txt) < 2:
        return None

    upper = txt.upper().strip(" .-")
    if not upper or len(upper) < 2:
        return None
    if re.fullmatch(r"[\dX*\s.\-]+", upper):
        return None
    if upper in _KNOWN_BANK_CODES or upper in _BANK_SLOT_NAMES:
        return None
    if _looks_like_remark(upper):
        return None
    if _IFSC_RE.fullmatch(upper):
        return None
    if _UTR_ALPHA_LONG_RE.fullmatch(upper) and len(upper) >= 14:
        return None
    if _UTR_SFMS_SEQ_RE.fullmatch(upper):
        return None
    if re.fullmatch(r"[A-Z]{1,6}\d{2,}[A-Z0-9]*", upper):
        return None
    if re.fullmatch(
        r"(?:DR|CR|IN|OUT|P2P|P2M|P2A|UPI|NEFT|RTGS|IMPS|ACH|NACH|ECS|POS|ATM|CLG|CHQ|CW|BE|MS|CD|TRF|TRFR|TRANSFER|MB|IB|TXN|BIL|INF|INB|IFT|TPT|FT)",
        upper,
    ):
        return None
    if not any(ch.isalpha() for ch in upper):
        return None
    return upper


def _derive_name_from_vpa(vpa: Optional[str]) -> Optional[str]:
    """Derive a clean, collision-safe party identifier from a UPI VPA handle."""
    if not vpa or "@" not in vpa:
        return None
    local = vpa.split("@", 1)[0].strip().lower()
    if not local:
        return None

    # Keep numeric mobile VPAs or opaque QR VPAs unique rather than collapsing them
    if local.isdigit() or re.fullmatch(r"(?:q|paytmqr|bharatpe|gpay)[._-]*\d+[a-z0-9]*", local):
        return vpa.upper()
    if local.startswith(("paytmqr", "gpay-", "phonepe")):
        tokens = [t for t in re.split(r"[._-]+", local) if t]
        alpha_tokens = [t for t in tokens[1:] if re.search(r"[a-z]{3,}", t) and not t.isdigit()]
        if alpha_tokens:
            return " ".join(alpha_tokens).upper()
        return local.upper()

    if local.startswith("bharatpe."):
        tail = local[len("bharatpe.") :]
        if tail.isdigit():
            return f"BHARATPE {tail}"
        local = tail

    raw_parts = [p for p in re.split(r"[._-]+", local) if p]
    cleaned_parts: List[str] = []
    for p in raw_parts:
        if p.isdigit() and cleaned_parts:
            continue
        # Strip trailing numeric differentiators from personal VPAs like rahul.sharma99 -> rahul sharma
        p_alpha = re.sub(r"(?<=[a-z]{3})\d{1,4}$", "", p)
        if p_alpha and any(c.isalpha() for c in p_alpha):
            cleaned_parts.append(p_alpha.upper())
    if cleaned_parts:
        candidate = " ".join(cleaned_parts)
        if candidate not in _KNOWN_BANK_CODES and candidate not in _GENERIC_REMARK_PHRASES:
            return candidate
    return vpa.upper()


def _is_structural_token(seg_upper: str, ifsc: Optional[str], utr: Optional[str], vpa: Optional[str]) -> bool:
    if not seg_upper:
        return True
    if ifsc and seg_upper == ifsc:
        return True
    if utr and seg_upper == utr:
        return True
    if vpa and seg_upper == vpa.upper():
        return True
    if "@" in seg_upper:
        return True
    if seg_upper in _KNOWN_BANK_CODES or seg_upper in _BANK_SLOT_NAMES:
        return True
    if _IFSC_RE.fullmatch(seg_upper):
        return True
    if _UTR_ALPHA_LONG_RE.fullmatch(seg_upper) and len(seg_upper) >= 14:
        return True
    if _UTR_SFMS_SEQ_RE.fullmatch(seg_upper):
        return True
    if re.fullmatch(r"[\dX*\s.\-]+", seg_upper):
        return True
    if re.fullmatch(r"[A-Z]{1,6}\d{2,}[A-Z0-9]*", seg_upper):
        return True
    if seg_upper in {
        "CR", "DR", "IN", "OUT", "P2P", "P2M", "P2A", "MOB", "MB",
        "INB", "IB", "REV", "AD", "TRF", "TRFR", "TRANSFER", "BIL",
        "INF", "IFT", "TPT", "FT", "TXN",
    }:
        return True
    return False


# A generic CHARGES / FEE / AMC word is a bank charge only with bank-charge context; "COURIER CHARGES" or
# "ROC FILING FEE" are payments to a third party for a purpose.
_BANK_CHARGE_CONTEXT_RE = re.compile(
    r"\b(?:SMS|MIN\s+BAL|MAB|AMB|QAB|NON[\s-]*MAINT\w*|A/?C\s+MAINT\w*|ACCOUNT\s+MAINT\w*|ACCOUNT\s+KEEPING|"
    r"DEBIT\s+CARD|ATM|CARD\s+(?:ANNUAL|ISSUANCE|REPLACEMENT|FEE)|ANNUAL\s+FEE|CHE?Q(?:UE)?\s+BOOK|CHQ\s*BK|FOLIO|LEDGER|"
    r"DD\s+(?:ISSUE|CHG|CHARGES?)|NEFT|RTGS|IMPS|UPI|BOUNCE|RETURN|RTN|RET|INSUFF\w*|DISHONOU?R\w*|PENAL|"
    r"PROCESSING\s+FEE|SERVICE\s+(?:CHARGES?|CHG|FEE)|STOP\s+PAY\w*|STATEMENT|DUPLICATE|CASH\s+HANDLING|"
    r"GST\s+ON|SI\s+FAIL\w*|ECS|NACH|ACH|MANDATE|BANK|BRANCH|LOCKER|INCIDENTAL|CHG|CHGS|CHRG|CHRGS|CHGE)\b"
)


def detect_channel(raw_narr_or_upper: str) -> str:
    """
    Boundary-aware Indian payment rail detector.
    Matches leading rail grammar first so company names (e.g. MAHATMA, JUPITER, DEPOSITS)
    never hijack the underlying transfer rail.
    """
    upper = _preprocess_ocr_narration(raw_narr_or_upper).upper()
    core = _strip_leading_wrappers(upper)

    # Priority bank charges/fees check before AEPS/UPI keywords (e.g. AEPS_AUTH_CHARGES, MMSD-CHARGES, SMS CHARGES, CR RT CHG)
    if re.search(
        r"(?:^|[\s_:/\-])(?:AEPS[_\s]+AUTH[_\s]+CHARGES?|DEBI[T1][_\s]+AUTH[_\s]+CHARGES?|MMSD[\s\-]+CHARGES?|SMS\s+(?:CHG|CHARGES?)|CR\s+RT\s+CHG|MIN\s+BAL\s+CHG|RETURN\s+CHG|FOLIO\s+CHG|SERVICE\s+CHG|ATM\s+(?:TXN\s+|TRANSACTION\s+|DECLINE\s+)?(?:CHG|CHGS|CHARGES?|FEE))(?:$|[\s_:/\-])",
        upper,
    ):
        return "CHARGES"

    # 1. Explicit leading rail headers (highest precedence)
    if re.match(r"^(?:AEPS\b|AADHAAR\s*PAY\b)", core):
        return "AEPS"
    if re.match(r"^(?:MMT/IMPS\b|IMPS(?:\b|[/\-*]))", core):
        return "IMPS"
    if re.match(r"^(?:RTGS(?:\b|[/\-*]))", core):
        return "RTGS"
    if re.match(r"^(?:NEFT(?:\b|[/\-*]))", core):
        return "NEFT"
    if re.match(r"^(?:UPI(?:AR|AB|P2P|P2M|LITE|MANDATE)?(?:\b|[/\-*])|BHIM\b)", core):
        return "UPI"
    if re.match(r"^(?:NACH(?:\b|[/\-*])|ACH(?:\s+[CD]|\s*[/\-*]|\s+DEBIT|\s+CREDIT)|ECS(?:\b|[/\-*]))", core):
        return "NACH"
    if re.match(r"^(?:ATW|CWL|EAW|NFS|ATM)(?:\b|[/\-])", core):
        return "ATM"
    if re.match(r"^(?:CDM|BNA|CASH\s+DEP|CASH\s+CR)\b", core) or re.match(r"^BY\s+CASH\b", upper):
        return "CASH_CDM"
    if re.match(r"^(?:CASH\s+WDL|SELF\s+CHQ)\b", core) or re.match(r"^TO\s+CASH\b", upper):
        return "CASH_WDL"
    if re.match(r"^(?:VPS|IPS|POS\s+PUR|ECOM\s+PUR|INTL\s+POS|SI\s+ECOM|POS)(?:\b|[/\-])", core):
        return "POS"
    if re.match(r"^(?:CLG|CTS|CHQ|CHEQUE|MICR)(?:\b|[/\-])", core):
        return "CHEQUE"
    if re.match(r"^(?:BIL|INF|IFT|TPT|FT)(?:\b|[/\-])", core):
        return "TRANSFER"

    # 2. Boundary-aware internal/anywhere rail keywords
    if re.search(r"\b(?:AEPS|AADHAAR\s*PAY)\b", upper):
        return "AEPS"
    if re.search(r"\bRTGS\b", upper):
        return "RTGS"
    if re.search(r"\bNEFT\b", upper):
        return "NEFT"
    if re.search(r"\bIMPS\b", upper):
        return "IMPS"
    if re.search(r"\b(?:NACH|ECS)\b|\bACH\s*[-/ ]", upper):
        return "NACH"
    if re.search(r"\bUPI(?:AR|AB|P2P|P2M|LITE|MANDATE)?\b", upper) or _extract_vpa(raw_narr_or_upper) is not None:
        return "UPI"
    if re.search(r"\b(?:CASH\s+DEP|CDM|BNA|CASH\s+CR|BY\s+CASH)\b", upper):
        return "CASH_CDM"
    if re.search(r"\b(?:ATW|CWL|EAW|ATM)\b|\bNFS/", upper):
        return "ATM"
    if re.search(r"\b(?:CASH\s+WDL|TO\s+CASH|SELF\s+CHQ)\b|\bCHQ\b.*\bSELF\b", upper):
        return "CASH_WDL"
    if re.search(r"\b(?:POS|VPS|IPS|ECOM|VISA|MASTERCARD|RUPAY)\b", upper):
        return "POS"
    if re.search(r"\b(?:CHQ|CHEQUE|CLG|CTS|MICR|COLLECTING\s+(?:BANK|[B38]RANCH)|SERVICE\s+BRANC[HA])\b", upper):
        return "CHEQUE"
    if re.search(r"\b(?:INT\.?\s*P[DA]|INTPD|CR\.?\s*INT|INTEREST\s+(?:PAID|CREDIT|CR)|CREDIT\s+INTEREST|SB\s*INT(?:EREST)?)\b", upper):
        return "INTEREST"
    if re.search(r"\b(?:SMS\s+CHG|MIN\s+BAL|CHARGES?|RETURN\s+CHG|BOUNCE|AMC|PENALTY|FEE)\b", upper) and _BANK_CHARGE_CONTEXT_RE.search(upper):
        return "CHARGES"
    if re.search(r"\b(?:FOREX|SWIFT|LRS)\b|\bTT\b", upper):
        return "FOREX"
    return "TRANSFER"


def _parse_upi(core: str, ifsc: Optional[str], utr: Optional[str], vpa: Optional[str]) -> Tuple[Optional[str], Optional[str], str]:
    """Parse SBI/HDFC/ICICI/Axis/Kotak UPI narration formats."""
    # Case 1: Slash-delimited UPI (SBI, ICICI, Axis, PNB, BOB, Canara, Union, IDFC)
    if "/" in core and (core.count("/") >= 2):
        parts = [p.strip() for p in core.split("/")]
        # Strip leading UPI subtype token
        slots = parts[1:] if re.match(r"^UPI", parts[0], re.IGNORECASE) else parts
        text_slots: List[str] = []
        remark_slots: List[str] = []
        for s in slots:
            s_up = s.upper().strip()
            if _is_structural_token(s_up, ifsc, utr, vpa):
                continue
            cleaned = _clean_party_text(s)
            if cleaned:
                text_slots.append(cleaned)
            else:
                norm_rem = re.sub(r"\s+", " ", re.sub(r"[^A-Za-z0-9 ._-]", " ", s)).strip()
                if norm_rem and norm_rem.upper() not in _KNOWN_BANK_CODES and not _is_structural_token(norm_rem.upper(), ifsc, utr, vpa):
                    remark_slots.append(norm_rem.upper())

        if text_slots:
            party = text_slots[0]
            remarks = "; ".join(text_slots[1:] + remark_slots) or None
            return party, remarks, "UPI_SLASH"
        # All textual slots were remarks/bank/vpa -> derive from VPA
        return _derive_name_from_vpa(vpa), ("; ".join(remark_slots) or None), "UPI_SLASH_VPA"

    # Case 2: Hyphen-delimited UPI (HDFC, Kotak) — e.g. UPI-<Name (may contain '-')>-<VPA>-<IFSC>-<RRN>-<Remark>
    if "-" in core:
        # Try anchor-preserving HDFC extraction first when VPA or IFSC/RRN is present
        m_hdfc_vpa = None
        if vpa:
            m_hdfc_vpa = re.search(
                rf"^UPI(?:[A-Z0-9]*)-(?:(\d{{12}})-)?(.+?)-{re.escape(vpa.upper())}(?:-(.*))?$",
                core,
            )
        if m_hdfc_vpa:
            raw_party_block = m_hdfc_vpa.group(2)
            tail = m_hdfc_vpa.group(3) or ""
            # Replace internal hyphens inside business names like TATA-AIG-INS cleanly
            party_clean = _clean_party_text(raw_party_block.replace("-", " "))
            tail_parts = [p.strip() for p in tail.split("-") if p.strip()]
            rem_parts = [
                p for p in tail_parts
                if not _is_structural_token(p.upper(), ifsc, utr, vpa)
            ]
            remarks = " ".join(rem_parts).strip() or None
            if party_clean:
                return party_clean, remarks, "UPI_HYPHEN_VPA_ANCHORED"
            return _derive_name_from_vpa(vpa), (remarks or raw_party_block), "UPI_HYPHEN_VPA"

        # Generic hyphen split when no VPA anchor matched
        parts = [p.strip() for p in core.split("-")]
        slots = parts[1:] if re.match(r"^UPI", parts[0], re.IGNORECASE) else parts
        text_slots = []
        remark_slots = []
        for s in slots:
            s_up = s.upper().strip()
            if _is_structural_token(s_up, ifsc, utr, vpa):
                continue
            cleaned = _clean_party_text(s)
            if cleaned:
                text_slots.append(cleaned)
            elif s_up and not _is_structural_token(s_up, ifsc, utr, vpa):
                remark_slots.append(s_up)
        if text_slots:
            return text_slots[0], ("; ".join(text_slots[1:] + remark_slots) or None), "UPI_HYPHEN"
        return _derive_name_from_vpa(vpa), ("; ".join(remark_slots) or None), "UPI_HYPHEN_VPA"

    return _derive_name_from_vpa(vpa), None, "UPI_GENERIC"


def _parse_neft_rtgs(rail: str, core: str, ifsc: Optional[str], utr: Optional[str]) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
    """Parse SBI (*), HDFC (-), ICICI/Axis/Kotak/PNB/BOB (/) NEFT & RTGS templates."""
    # 1. SBI star-delimited: NEFT*IFSC*UTR*PartyName[*Remarks]
    if "*" in core:
        parts = [p.strip() for p in core.split("*") if p.strip()]
        slots = [p for p in parts if not re.match(rf"^{rail}$", p, re.IGNORECASE)]
        detected_ifsc = ifsc
        detected_utr = utr
        non_struct: List[str] = []
        for s in slots:
            if not detected_ifsc and _IFSC_RE.fullmatch(s):
                detected_ifsc = s
                continue
            if _is_structural_token(s, detected_ifsc, detected_utr, None):
                if not detected_utr and len(s) >= 12 and any(c.isdigit() for c in s):
                    detected_utr = s
                continue
            cleaned = _clean_party_text(s)
            if cleaned:
                non_struct.append(cleaned)
        if non_struct:
            return non_struct[0], (" ".join(non_struct[1:]) or None), detected_utr, f"{rail}_STAR"

    # 2. HDFC NEFT/RTGS CR/DR hyphen template:
    #    NEFT CR-<IFSC>-<Counterparty Name>[-<Self/Ref>]-<UTR>
    if "-" in core and re.match(rf"^{rail}(?:\s+(?:CR|DR))?-", core):
        parts = [p.strip() for p in core.split("-") if p.strip()]
        slots = parts[1:]
        detected_utr = utr
        non_struct = []
        for s in slots:
            if _is_structural_token(s, ifsc, detected_utr, None):
                continue
            cleaned = _clean_party_text(s)
            if cleaned:
                non_struct.append(cleaned)
        if non_struct:
            # First valid party name after IFSC is always the external counterparty in HDFC NEFT/RTGS
            return non_struct[0], (" ".join(non_struct[1:]) or None), detected_utr, f"{rail}_HDFC_HYPHEN"

    # 3. ICICI / Axis / Kotak / PNB / BOB slash/colon/space-delimited templates
    parts = [p.strip() for p in re.split(r"[/|:*]+|(?<!\S)-(?!\S)|-", core) if p.strip()]
    slots = [p for p in parts if not re.match(rf"^{rail}(?:\s+(?:CR|DR|IN|OUT))?$", p, re.IGNORECASE)]
    non_struct = []
    remarks: List[str] = []
    for s in slots:
        s_clean = re.sub(rf"^(?:{rail}\s+)?(?:(?:CR|DR|IN|OUT)\b)?\s*", "", s, flags=re.IGNORECASE).strip()
        s_clean = re.sub(r"\b(?:TRF|TRFR)\s+(?:FR|FROM|TO)\s+\d{6,18}\b", "", s_clean, flags=re.IGNORECASE).strip()
        if len(slots) == 1:
            if utr:
                s_clean = s_clean.replace(utr, " ")
            if ifsc:
                s_clean = s_clean.replace(ifsc, " ")
            # Strip OCR-garbled RBI NEFT routing IFSCs like `ERBISOGORJER,` / `RBIS0GORJEP,`
            s_clean = re.sub(r"\b[A-Z]?RBIS[0O][A-Z0-9]{4,7}[,:\s]*", " ", s_clean, flags=re.IGNORECASE).strip()
        if _is_structural_token(s_clean, ifsc, utr, None):
            continue
        cleaned = _clean_party_text(s_clean)
        if cleaned:
            non_struct.append(cleaned)
        elif s_clean:
            remarks.append(s_clean)

    # Space-separated NEFT fallback (e.g. "NEFT N095260123456 INFOSYS LIMITED")
    if not non_struct:
        stripped = re.sub(rf"^{rail}(?:\s+(?:CR|DR|IN|OUT))?\s+", "", core).strip()
        stripped = re.sub(r"\b(?:TRF|TRFR)\s+(?:FR|FROM|TO)\s+\d{6,18}\b", "", stripped, flags=re.IGNORECASE).strip()
        for rem_tok in [x for x in (ifsc, utr) if x]:
            stripped = stripped.replace(rem_tok, " ")
        stripped = re.sub(r"\b[A-Z]?RBIS[0O][A-Z0-9]{4,7}[,:\s]*", " ", stripped, flags=re.IGNORECASE).strip()
        cleaned = _clean_party_text(stripped)
        if cleaned:
            non_struct.append(cleaned)

    if non_struct:
        return non_struct[0], (" ".join(non_struct[1:] + remarks) or None), utr, f"{rail}_STRUCTURED"
    return None, None, utr, f"{rail}_UNRESOLVED"


def _parse_imps(core: str, ifsc: Optional[str], utr: Optional[str]) -> Tuple[Optional[str], Optional[str], str]:
    """Parse ICICI MMT/IMPS, Axis IMPS/P2A, SBI, HDFC, Kotak IMPS templates."""
    if core.startswith("MMT/IMPS") and "/" in core:
        # MMT/IMPS/<RRN>/<Slot3 (remark or party)>/<Slot4 (party or ref)>/<Bank?>
        parts = [p.strip() for p in core.split("/") if p.strip()][2:]
        candidates: List[str] = []
        remarks: List[str] = []
        for s in parts:
            if _is_structural_token(s, ifsc, utr, None):
                continue
            cleaned = _clean_party_text(s)
            if cleaned:
                candidates.append(cleaned)
            else:
                remarks.append(s)
        if len(candidates) >= 2:
            # In ICICI MMT/IMPS, if two text slots exist, check if slot 0 looks like short remark while slot 1 is formal name
            if len(candidates[0].split()) == 1 and len(candidates[1].split()) >= 2:
                return candidates[1], candidates[0], "IMPS_MMT"
            return candidates[-1] if any(w in candidates[-1] for w in ("PVT", "LTD", "INC", "CORP")) else candidates[0], candidates[-1], "IMPS_MMT"
        if candidates:
            return candidates[0], ("; ".join(remarks) or None), "IMPS_MMT"
        return None, ("; ".join(remarks) or None), "IMPS_MMT"

    parts = [p.strip() for p in re.split(r"[/|*-]+", core) if p.strip()]
    slots = [p for p in parts if not re.match(r"^IMPS$", p, re.IGNORECASE)]
    candidates = []
    remarks = []
    for s in slots:
        if _is_structural_token(s, ifsc, utr, None):
            continue
        cleaned = _clean_party_text(s)
        if cleaned:
            candidates.append(cleaned)
        else:
            remarks.append(s)
    if candidates:
        return candidates[0], ("; ".join(candidates[1:] + remarks) or None), "IMPS_STRUCTURED"
    return None, ("; ".join(remarks) or None), "IMPS_UNRESOLVED"


def _parse_nach(core: str, ifsc: Optional[str], utr: Optional[str]) -> Tuple[Optional[str], Optional[str], str]:
    """Parse NACH / ACH / ECS mandate and institution strings."""
    cleaned_core = re.sub(
        r"^(?:(?:NACH|ACH|ECS)(?:[\s/\-]*(?:DR|CR|DEBIT|CREDIT|TP|AD|D|C)\b)?[\s/\-*]*)+",
        "",
        core,
        flags=re.IGNORECASE,
    ).strip()
    parts = [p.strip() for p in re.split(r"[/|*-]+", cleaned_core) if p.strip()]
    candidates: List[str] = []
    remarks: List[str] = []
    for s in parts:
        # Strip trailing UMRN/mandate codes attached with spaces
        s_trim = re.sub(r"\b(?:UMRN|MANDATE|POLICY|LOAN\s*NO)?\s*[A-Z0-9]*\d{6,}[A-Z0-9]*\b", "", s).strip()
        if _is_structural_token(s_trim, ifsc, utr, None):
            remarks.append(s)
            continue
        c = _clean_party_text(s_trim)
        if c:
            candidates.append(c)
        else:
            remarks.append(s)
    if candidates:
        return candidates[0], ("; ".join(candidates[1:] + remarks) or None), "NACH_STRUCTURED"
    # On a mandate the sponsor bank is the institution being paid (loan EMI to HDFC BANK)
    for s in parts:
        s_up = _PARTY_LEAD_TAG_RE.sub("", re.sub(r"\s+", " ", s.upper()).strip()).strip()
        if len(s_up) >= 3 and (s_up in _BANK_SLOT_NAMES or (s_up in _KNOWN_BANK_CODES and len(s_up) >= 5)):
            return s_up, ("; ".join(remarks) or None), "NACH_BANK"
    return None, None, "NACH_UNRESOLVED"


def _parse_pos(core: str) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
    """Parse POS / VPS / IPS / ECOM card purchase narrations."""
    card_mask = _extract_card(core) or _extract_masked_account(core)
    stripped = re.sub(
        r"^(?:POS\s+ATM\s+PURCH|ECOM\s+PUR|POS\s+PUR|INTL\s+POS|SI\s+ECOM|VPS|IPS|POS|ECOM)[\s/\-]*",
        "",
        core,
        flags=re.IGNORECASE,
    ).strip()
    if card_mask:
        stripped = stripped.replace(card_mask, " ")
    # Remove embedded card numbers (e.g. 432198XXXXXX1234 or 16 digits), dates DDMMYY / YYYYMMDD, ref numbers
    stripped = re.sub(r"\b\d{4,6}[X*]{4,8}\d{4}\b", " ", stripped)
    stripped = re.sub(r"\b\d{6,16}\b", " ", stripped)
    parts = [p.strip() for p in re.split(r"[/|*-]+", stripped) if p.strip()]
    for s in parts:
        if re.fullmatch(r"\d{2,4}[-/]\d{2}(?:[-/]\d{2,4})?", s):
            continue
        cleaned = _clean_party_text(s)
        if cleaned:
            return cleaned, card_mask, None, "POS_MERCHANT"
    return _clean_party_text(stripped), card_mask, None, "POS_GENERIC"


def extract_cheque_number(core_upper: str, channel: Optional[str] = None) -> Optional[str]:
    """6-digit cheque / instrument number (NLP-20), or None. Zero padding beyond six digits is dropped
    (``0000123456`` -> ``123456``); a 6-digit number with leading zeros is kept as written (``000789``).

    Tagged forms (``CHQ NO 000789``, ``CLG/123456``, ``CTS-123456``, ``INST 123456``) are accepted on any
    channel. An untagged 6-digit number counts only on a cheque row and only when it forms a whole
    delimited slot (``.../123456/...``) that is not preceded by a place name (``PUNE 411001`` is a PIN code).
    """
    up = (core_upper or "").upper()
    m = _CHQ_TAGGED_RE.search(up)
    if m:
        return m.group(1)
    if (channel or "").upper() != "CHEQUE":
        return None
    for slot in re.split(r"[/|*,;]+|\s-\s|-", up):
        mm = _CHQ_SLOT_RE.match(slot.strip())
        if mm:
            return mm.group(1)
    return None


def _parse_cheque(core: str) -> Tuple[Optional[str], Optional[str], str]:
    """Parse Cheque / Clearing (CLG/CTS/MICR/CHQ) narrations."""
    core = re.sub(r"[\s/,:-]*\bCOLLECTING\b.*$", "", core, flags=re.IGNORECASE).strip()
    core = re.sub(r"[\s,:;-]*\bSERVICE\s+BRANC[HA]\b.*$", "", core, flags=re.IGNORECASE).strip()
    core = re.sub(r"\bHD[38]\s+FINANCIA[:L]?\s+SERVICES?\b", "HDB FINANCIAL SERVICES", core, flags=re.IGNORECASE)
    core = re.sub(r"\bHDB\s+FINANCIAL\s+SERVIC\b", "HDB FINANCIAL SERVICES", core, flags=re.IGNORECASE)
    core = re.sub(r"\s+(?:L\s*[2I1]?U|-\s*1)\b.*$", "", core, flags=re.IGNORECASE).strip()
    chq_ref = extract_cheque_number(core, "CHEQUE")
    # Clearing location (CITY + PIN) is neither the party nor the instrument number
    core = _CITY_PIN_RE.sub(" ", core.upper())
    stripped = re.sub(
        r"\b(?:BY|TO|CLG|CTS|CHQ|CHEQUE|MICR|INST|INWARD|OUTWARD|PAID|CLEARING|REF|NO|DEP|DEPOSIT|DEPOSITED)\b",
        " ",
        core,
        flags=re.IGNORECASE,
    )
    if chq_ref:
        stripped = re.sub(rf"\b0*{re.escape(chq_ref)}\b", " ", stripped)
    parts = [p.strip() for p in re.split(r"[/|*-]+", stripped) if p.strip()]
    cands = [c for s in parts if (c := _clean_party_text(_strip_clearing_prefix(s))) and not _is_clearing_residue(c)]
    if cands:
        return cands[0], chq_ref, "CHEQUE_STRUCTURED"
    return None, chq_ref, "CHEQUE_NO_PARTY"


def _strip_clearing_prefix(text: str) -> str:
    """Drop a leading drawee-bank / clearing-city prefix ("HDFC BANK SHARMA STEELS" -> "SHARMA STEELS")."""
    words = text.split()
    k = 0
    while k < len(words) - 1 and (
        words[k].upper() in _KNOWN_BANK_CODES or words[k].upper() in _CLEARING_CITY_WORDS
        or words[k].upper() in _CLEARING_NOISE_WORDS
    ):
        k += 1
    return " ".join(words[k:])


def _is_clearing_residue(text: str) -> bool:
    """True when a cheque slot only names the drawee bank and/or clearing city ("SBI MUMBAI", "ICICI")."""
    words = [w for w in re.split(r"[\s.&'/-]+", text.upper()) if w]
    if not words:
        return True
    return all(
        w in _KNOWN_BANK_CODES or w in _CLEARING_CITY_WORDS or w in _CLEARING_NOISE_WORDS or w.isdigit()
        for w in words
    ) or text.upper() in _BANK_SLOT_NAMES


def _parse_internal_transfer(core: str, ifsc: Optional[str], utr: Optional[str], vpa: Optional[str]) -> Tuple[Optional[str], Optional[str], str]:
    """Parse BIL/INB/INF/IFT/TPT/TRF/FT/Sweep narrations."""
    stripped = re.sub(
        r"^(?:BIL/(?:INFT|ONL|BPAY)|INF/(?:INFT|TRFR)|INB/(?:IFT|TRFR|TXN)?|(?:TRFR|TRF|IFT|TPT|FT)(?![A-Z0-9]))\s*[/\-:]?\s*",
        "",
        core,
        flags=re.IGNORECASE,
    ).strip()
    if any(k in core for k in ("AUTOSWEEP", "AUTO SWEEP", "MOD SWEEP", "SWEEP TRF", "SWEEP TO", "SWEEP FROM")):
        return "AUTO SWEEP / FD LINK", None, "BANK_SWEEP"

    parts = [p.strip() for p in re.split(r"[/|*-]+", stripped) if p.strip()]
    cands: List[str] = []
    remarks: List[str] = []
    for s in parts:
        s_clean = re.sub(r"\b(?:TRF|TRFR)\s+(?:FR|FROM|TO)\s+\d{6,18}\b", "", s, flags=re.IGNORECASE).strip()
        if _is_structural_token(s_clean, ifsc, utr, vpa):
            continue
        cleaned = _clean_party_text(s_clean)
        if cleaned:
            cands.append(cleaned)
        else:
            remarks.append(s_clean)
    if cands:
        return cands[0], ("; ".join(cands[1:] + remarks) or None), "INTERNAL_STRUCTURED"
    return None, None, "INTERNAL_GENERIC"


def parse_narration(raw_narration: str) -> ParsedNarration:
    """Extract payment channel, UTR/RRN, IFSC, VPA/Account, counterparty name, and remarks from raw narration."""
    narr = _preprocess_ocr_narration(raw_narration)
    upper = narr.upper()
    core = _strip_leading_wrappers(upper)
    channel = detect_channel(narr)
    # "M/S" (Messrs) is a salutation; left in place its slash splits slot grammars ("M" + "S SHARMA ...")
    core = _MS_SALUTATION_RE.sub("", core)

    ifsc_m = _IFSC_RE.search(upper)
    ifsc = ifsc_m.group(1) if ifsc_m else None

    vpa = _extract_vpa(narr, upi_context=True if channel == "UPI" else None)
    # Account numbers named as such (A/C NO 001234567890, TRF FROM 30112233445) are never the UTR/RRN
    utr = _extract_utr(_ACC_TRF_PHRASE_RE.sub(" ", _ACC_TAGGED_RE.sub(" ", core)), ifsc, channel)
    card_mask: Optional[str] = _extract_card(core) if channel in ("POS", "ATM") else None
    account = extract_counterparty_account(core, channel, utr) if channel not in ("POS", "ATM") else None
    vpa_or_acc: Optional[str] = vpa or account
    # Tag words such as "TO CLG" are stripped from `core` as wrappers, so tagged numbers are read from `upper`
    cheque_no = extract_cheque_number(upper, channel) or extract_cheque_number(core, channel)
    # Identifier fragments ("A/C 30112233445") must not leak into party slots (NLP-17)
    core = re.sub(r"\s+", " ", _PARTY_ACC_FRAGMENT_RE.sub(" ", core)).strip()

    party: Optional[str] = None
    remarks: Optional[str] = None
    grammar: str = channel
    chq_ref_from_grammar: Optional[str] = None

    if channel == "UPI":
        party, remarks, grammar = _parse_upi(core, ifsc, utr, vpa)
    elif channel in ("NEFT", "RTGS"):
        party, remarks, utr_override, grammar = _parse_neft_rtgs(channel, core, ifsc, utr)
        utr = utr or utr_override
    elif channel == "IMPS":
        party, remarks, grammar = _parse_imps(core, ifsc, utr)
    elif channel == "NACH":
        party, remarks, grammar = _parse_nach(core, ifsc, utr)
    elif channel == "POS":
        party, pos_card, remarks, grammar = _parse_pos(core)
        card_mask = card_mask or pos_card
    elif channel == "ATM":
        party = "CASH WITHDRAWAL (ATM)"
        grammar = "ATM_CASH"
    elif channel == "CASH_CDM":
        party = "CASH DEPOSIT"
        grammar = "CASH_DEPOSIT"
    elif channel == "CASH_WDL":
        party = "CASH WITHDRAWAL"
        grammar = "CASH_WITHDRAWAL"
    elif channel == "AEPS":
        parts = [p.strip() for p in re.split(r"[/|*-]+", core) if p.strip()]
        cands = [
            c for p in parts[1:]
            if not _is_structural_token(p, ifsc, utr, vpa) and (c := _clean_party_text(p))
        ]
        party = cands[0] if cands else "AEPS CASH TRANSACTION"
        grammar = "AEPS"
    elif channel == "CHEQUE":
        party, chq_ref, grammar = _parse_cheque(core)
        chq_ref_from_grammar = chq_ref
        # A cheque/instrument number is not a UTR (NLP-17): keep it in cheque_number only
        if utr and utr.lstrip("0") in {(x or "").lstrip("0") for x in (cheque_no, chq_ref) if x}:
            utr = None
    elif channel == "INTEREST":
        party = "BANK INTEREST"
        grammar = "BANK_INTEREST"
    elif channel == "CHARGES":
        party = "BANK CHARGES"
        grammar = "BANK_CHARGES"
    else:
        up_clean = core.upper()
        if "PMJJBY" in up_clean:
            party, grammar = "PMJJBY INSURANCE RENEWAL", "GOVT_SCHEME"
        elif "PMSBY" in up_clean:
            party, grammar = "PMSBY INSURANCE RENEWAL", "GOVT_SCHEME"
        elif re.search(r"\bAPY\b", up_clean):
            party, grammar = "APY PENSION CONTRIBUTION", "GOVT_SCHEME"
        elif "PFMS" in up_clean:
            party, grammar = "PFMS DBT CREDIT", "GOVT_SCHEME"
        elif "GRUHA LAKSHMI SCHEME" in up_clean:
            party, grammar = "GRUHA LAKSHMI SCHEME", "GOVT_SCHEME"
        else:
            party, remarks, grammar = _parse_internal_transfer(core, ifsc, utr, vpa)

    # Final safe fallback if structured grammar left party empty
    if not party and vpa:
        party = _derive_name_from_vpa(vpa)
    if not party and channel != "CHEQUE":
        # (cheque rows with no payee stay unresolved: tag-word residue would merge unrelated cheques, NLP-17)
        tokens = [t.strip() for t in re.split(r"[/|*-]+", core) if t.strip()]
        for tok in tokens:
            if not _is_structural_token(tok, ifsc, utr, vpa):
                cleaned = _clean_party_text(tok)
                if cleaned:
                    party = cleaned
                    break

    return ParsedNarration(
        payment_channel=channel,
        utr_number=utr,
        counterparty_name=party,
        counterparty_vpa_or_acc=vpa_or_acc,
        counterparty_ifsc=ifsc,
        remarks=remarks,
        matched_grammar=grammar,
        srs_channel=srs_payment_channel(channel),
        counterparty_account=account,
        card_number_masked=card_mask,
        cheque_number=cheque_no or (chq_ref_from_grammar if channel == "CHEQUE" else None),
    )


def utr_from_ref_column(ref: Optional[str], channel: Optional[str], cheque_number: Optional[str] = None) -> Optional[str]:
    """
    A UTR/RRN taken from the statement's own Ref/UTR column when the narration has none (TG-11/FR-TRACE-04):
    the whole cell must be one UTR-shaped token (UTR-prefixed, a 12-digit RRN, a 16-digit rail number or a
    14-22 character alphanumeric UTR). Cheque rows and short numeric cheque numbers are never promoted.
    """
    if not ref or channel == "CHEQUE":
        return None
    val = re.sub(r"\s+", "", str(ref)).upper().strip(".:-/")
    if not val or (cheque_number and val.lstrip("0") == str(cheque_number).lstrip("0")):
        return None
    if val.isdigit():
        return val if len(val) in (12, 16, 22) else None
    if re.fullmatch(r"UTR[A-Z0-9]{5,22}", val) and any(c.isdigit() for c in val):
        return val
    if re.fullmatch(r"[A-Z0-9]{14,22}", val) and sum(c.isdigit() for c in val) >= 6 and not _IFSC_RE.fullmatch(val):
        return val
    return None


def enrich_transaction_narration(tx: NormalizedTransaction) -> NormalizedTransaction:
    """Populate normalized payment rail, UTR, counterparty, VPA/Account, and IFSC on a transaction."""
    parsed = parse_narration(tx.raw_narration)
    tx.payment_channel = parsed.payment_channel
    tx.utr_number = tx.utr_number or parsed.utr_number or utr_from_ref_column(
        getattr(tx, "cheque_or_ref_no", None), parsed.payment_channel, parsed.cheque_number
    )
    tx.extracted_counterparty = parsed.counterparty_name
    tx.counterparty_vpa_or_acc = parsed.counterparty_vpa_or_acc
    tx.counterparty_ifsc = parsed.counterparty_ifsc
    if hasattr(tx, "card_number_masked"):
        tx.card_number_masked = getattr(parsed, "card_number_masked", None) or tx.card_number_masked
    if hasattr(tx, "cheque_number"):
        tx.cheque_number = getattr(parsed, "cheque_number", None) or tx.cheque_number
    return tx
