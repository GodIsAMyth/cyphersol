"""Unified multi-format bank statement parser (PDF, XLSX, CSV, TXT) and bulk runner."""

from __future__ import annotations

import codecs
import csv
import datetime as dt
import hashlib
import io
import logging
import multiprocessing
import os
import pickle
import re
import time
import uuid
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from cyphersol.ingestion.ocr_engine import ocr_image_bytes
from cyphersol.ingestion.pdf_parser import PdfTextToken, _group_tokens_into_rows, extract_pdf_data
from cyphersol.ingestion.xlsx_io import read_html_table_rows, read_xls_bytes, read_xlsx_bytes
from cyphersol.models import (
    AnomalyFlag,
    BankAccountStatement,
    DrawTemplate,
    NormalizedTransaction,
    TxDirection,
)
from cyphersol.nlp.narration_parser import enrich_transaction_narration
from cyphersol.taxonomy import BANK_BY_CODE, INDIAN_BANKS, BankProfile

_LOG = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pgm", ".ppm", ".bmp", ".tif", ".tiff"}

# Month-name formats only: purely numeric dates go through `_parse_numeric_date` so the day/month order is explicit
_DATE_FORMATS = (
    "%d-%b-%Y",
    "%d-%b-%y",
    "%d-%B-%Y",
    "%d %b %Y",
    "%d %b %y",
    "%d %B %Y",
    "%d/%b/%Y",
    "%d/%b/%y",
    "%d %b, %Y",
    "%b %d, %Y",
    "%b %d %Y",
    "%d%b%Y",
)

# Day/month order for purely numeric dates
DATE_ORDER_DMY = "DMY"
DATE_ORDER_MDY = "MDY"
DATE_ORDER_AUTO = "auto"

_NUMERIC_DATE_RE = re.compile(r"^(\d{1,4})([./-])(\d{1,2})\2(\d{1,4})$")
_NUMERIC_DATE_PREFIX_RE = re.compile(r"^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})(?!\d)")
_TIME_SUFFIX_RE = re.compile(r"\s+\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:\s*[AP]M)?$", re.IGNORECASE)

# Order checks specific composite fields (`value_date`, `type`) before shorter single-token fields (`date`, `debit`, `credit`)
_HEADER_SYNONYMS = {
    "value_date": ("value date", "val date", "value dt"),
    "date": ("txn date", "tran date", "transaction date", "post date", "date"),
    "narration": ("narration", "particulars", "description", "remarks", "details", "transaction remarks", "tran particulars", "transaction description"),
    "ref": ("chq./ref.no.", "ref./cheque", "chq/ref no", "ref no", "cheque no", "chq", "cheque", "utr", "reference"),
    "type": ("dr/cr", "cr/dr", "drcr", "txn type", "type"),
    "debit": ("withdrawal amt", "debit amt", "withdrawals", "withdrawal", "debit", "dr"),
    "credit": ("deposit amt", "credit amt", "deposits", "deposit", "credit", "cr"),
    "amount": ("txn amount", "transaction amount", "amount"),
    "balance": ("closing balance", "running balance", "available balance", "balance", "bal"),
}

_FOOTER_LINE_RE = re.compile(
    r"^\s*(?:"
    r"page\s+(?:no\.?\s*)?\d+(?:\s*(?:of|/)\s*\d+)?\s*$"
    r"|(?:opening|closing|brought\s+forward|carried\s+forward|available|ledger|total)\s+balance\b"
    r"|(?:statement\s+(?:summary|period|of\s+account)|generated\s+(?:on|by)|this\s+is\s+a\s+computer\s+generated|end\s+of\s+statement|cyphersol\s+standardized\s+statement\s+fixture)\b"
    r")",
    flags=re.IGNORECASE,
)


def compute_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _expand_year(y_str: str) -> Optional[int]:
    if len(y_str) == 4:
        return int(y_str)
    if len(y_str) == 2:
        yy = int(y_str)
        # Same pivot as strptime `%y`: 00-68 -> 20xx, 69-99 -> 19xx
        return 2000 + yy if yy <= 68 else 1900 + yy
    return None


def _safe_date(y: Optional[int], m: int, d: int) -> Optional[str]:
    if y is None:
        return None
    try:
        return dt.date(y, m, d).isoformat()
    except ValueError:
        return None


def _parse_numeric_date(s: str, order: str) -> Tuple[bool, Optional[str]]:
    """Return (is_numeric_pattern, iso) for `d/m/y`, `m/d/y` or `y/m/d` strings under the requested order."""
    m = _NUMERIC_DATE_RE.match(s)
    if not m:
        return False, None
    a, _sep, b, c = m.groups()
    if len(a) == 4:
        return True, _safe_date(int(a), int(b), int(c)) if len(c) <= 2 else None
    if len(a) > 2 or len(c) not in (2, 4):
        return True, None
    year = _expand_year(c)
    first, second = int(a), int(b)
    if order == DATE_ORDER_MDY:
        return True, _safe_date(year, first, second)
    res = _safe_date(year, second, first)
    if res is None and order == DATE_ORDER_AUTO and first <= 12 < second:
        # Unambiguously month-first (`12/25/2025`) when read without statement context
        res = _safe_date(year, first, second)
    return True, res


def _normalize_date_text(raw: Any) -> str:
    if raw is None:
        return ""
    if isinstance(raw, dt.datetime):
        return raw.date().isoformat()
    if isinstance(raw, dt.date):
        return raw.isoformat()
    s = re.sub(r"\s+", " ", str(raw).strip())
    if not s:
        return ""
    # Strip timestamp portion if attached (e.g. '05/04/2026 14:22:10', '05.04.26 09:30 AM')
    s = _TIME_SUFFIX_RE.sub("", s).strip()
    if "T" in s and re.match(r"^\d{4}-\d{2}-\d{2}T", s):
        s = s.split("T")[0]
    return s


def _parse_date_basic(raw: Any, order: str = DATE_ORDER_AUTO) -> Optional[str]:
    """Strict date parse with no OCR repairs: numeric dates honour `order`; month-name formats are order-free."""
    s = _normalize_date_text(raw)
    if not s:
        return None
    is_numeric, res = _parse_numeric_date(s, order)
    if is_numeric:
        return res
    if not re.search(r"[A-Za-z]{3}", s):
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def parse_timestamp(raw: Any, fallback_date: Optional[str]) -> Optional[str]:
    if raw is None:
        return f"{fallback_date}T12:00:00" if fallback_date else None
    s = str(raw).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2})?", s):
        return s.replace(" ", "T")
    m = re.search(r"(\d{2}:\d{2}(?::\d{2})?)", s)
    if m and fallback_date:
        t = m.group(1)
        if len(t) == 5:
            t += ":00"
        return f"{fallback_date}T{t}"
    return f"{fallback_date}T12:00:00" if fallback_date else None


def parse_indian_amount(raw: Any, ocr: bool = True) -> Tuple[Decimal, Optional[str]]:
    """
    Parse numbers like '1,23,456.78', 'Rs. 500.00', '45,000.00 Cr', '(1200.00)', '250.00-' into
    (Decimal, 'CR'|'DR'|None). Negative values ('-500', '(500)', trailing minus on digital sources) yield 'DR'.
    `ocr=True` also repairs OCR glyph confusions (`O`->0, `l`->1, `0r`->Cr); digital sources pass `ocr=False`.
    """
    if raw is None:
        return Decimal("0.00"), None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        val = Decimal(str(raw))
        return abs(val).quantize(Decimal("0.01")), ("DR" if val < 0 else None)

    s = str(raw or "").strip()
    if not s or s in {"-", "--", "NIL", "nil", "NA", "N/A"}:
        return Decimal("0.00"), None

    multi_amts = re.findall(r"\(?\-?(?:Rs\.?|INR|₹)?\s*[\d,]+\.\d{2}(?:\s*(?:CR|DR))?\)?", s, flags=re.IGNORECASE)
    if len(multi_amts) >= 2:
        return parse_indian_amount(multi_amts[-1], ocr=ocr)

    s = re.sub(r"(\d)\.\s+(\d{1,2})\b", r"\1.\2", s)
    s = re.sub(r"(\.\d{2})\.+$", r"\1", s)
    s = re.sub(r"[*#;]+$", "", s).strip()
    s = re.sub(r"^(?:Rs\.?|INR|₹)\s*", "", s, flags=re.IGNORECASE).strip()

    hint: Optional[str] = None
    trailing_minus = False
    if not ocr and re.search(r"\d\s*-$", s):
        trailing_minus = True
        s = s[:-1].rstrip()
    m_crdr = re.search(r"[\s(]*(C\s*R|D\s*R|C\s*A|D\s*A)\.?\s*\)?$", s, flags=re.IGNORECASE)
    if m_crdr:
        tag = re.sub(r"\s+", "", m_crdr.group(1)).upper()
        hint = "CR" if tag.startswith("C") else "DR"
        s = s[: m_crdr.start()].strip()
    elif ocr:
        m_crdr_ocr = re.search(
            r"(\.\d{1,2})\s*(?:0\s*[rRгtT:\-=]|(C|D)[A-Za-z0-9г:=]*|1[tTrR]|[:=\-])\s*$",
            s,
            flags=re.IGNORECASE,
        )
        if m_crdr_ocr:
            c_ch = (m_crdr_ocr.group(2) or "").upper()
            if c_ch == "D":
                hint = "DR"
            elif c_ch == "C":
                hint = "CR"
            s = s[: m_crdr_ocr.start()] + m_crdr_ocr.group(1)
        elif re.search(r"\.\d{1,2}\s*[CD]$", s, flags=re.IGNORECASE):
            m_tail = re.search(r"([CD])$", s, flags=re.IGNORECASE)
            if m_tail:
                hint = "CR" if m_tail.group(1).upper() == "C" else "DR"
                s = s[: m_tail.start()].strip()

    if re.match(r"^[\d,]+\.$", s):
        s = s + "00"
    if ocr and re.match(r"^:\d\.\d{2}$", s):
        s = "1" + s[1:]

    is_negative = trailing_minus or s.startswith("-") or (s.startswith("(") and s.endswith(")"))

    # Repair OCR digit/letter confusions only when all non-punctuation chars are digits or OCR digit lookalikes
    digit_count = sum(1 for ch in s if ch.isdigit())
    if ocr and digit_count >= 1 and re.match(r"^[\-\(\s]*[0-9.,OoDQIl|BSZi]+[\)\s]*$", s):
        ocr_map = str.maketrans({"O": "0", "o": "0", "D": "0", "Q": "0", "I": "1", "l": "1", "i": "1", "|": "1", "B": "8", "S": "5", "Z": "2"})
        s = s.translate(ocr_map)

    # Fix comma-as-decimal (`5,497,50` -> `5497.50`) and thousands-dot (`3.714.50` -> `3714.50`)
    if re.match(r"^[\-\(\s]*\d{1,3}(?:\.\d{3})+,\d{2}[\)\s]*$", s):
        s = s.replace(".", "").replace(",", ".")  # European `1.234,50`
    elif "." not in s and re.search(r",\d{2}$", s):
        s = re.sub(r",(\d{2})$", r".\1", s)
    if re.match(r"^[\-\(\s]*\d{1,3}(?:\.\d{3})+\.\d{2}[\)\s]*$", s):
        parts = s.rsplit(".", 1)
        s = parts[0].replace(".", "") + "." + parts[1]

    cleaned = re.sub(r"[^0-9.]", "", s)
    if cleaned.count(".") > 1 and re.search(r"\.\d{2}$", cleaned):
        head, tail = cleaned.rsplit(".", 1)
        cleaned = head.replace(".", "") + "." + tail
    if not cleaned or cleaned == ".":
        return Decimal("0.00"), hint
    try:
        dec = Decimal(cleaned).quantize(Decimal("0.01"))
    except InvalidOperation:
        return Decimal("0.00"), hint
    if is_negative and hint is None:
        hint = "DR"
    return dec, hint


_TWO_DATES_RE = re.compile(
    r"^(\d{1,2}[./-](?:\d{1,2}|[A-Za-z]{3})[./-]\d{2,4})\s+\d{1,2}[./-](?:\d{1,2}|[A-Za-z]{3})[./-]\d{2,4}$"
)


def _light_clean_date_text(raw: Any) -> str:
    """Source-agnostic tidy-up (edge punctuation, spaces around separators, Post+Value date in one cell)."""
    s = _normalize_date_text(raw)
    s = re.sub(r"^[^0-9A-Za-z]+|[^0-9A-Za-z]+$", "", s)
    s = re.sub(r"\s*([./-])\s*", r"\1", s)
    m_two = _TWO_DATES_RE.match(s)
    return m_two.group(1) if m_two else s


def _normalize_order(order: Optional[str]) -> str:
    up = str(order or "").strip().upper()
    if up in (DATE_ORDER_DMY, DATE_ORDER_MDY):
        return up
    return DATE_ORDER_AUTO


def _clean_ocr_date_text(raw: Any) -> str:
    s = str(raw or "").strip()
    if not s:
        return ""
    s = re.sub(r"^[^0-9A-Za-z]+|[^0-9A-Za-z]+$", "", s)
    s = re.sub(r"^[FPTfpt](?=[0-3]\d[./:-][0-9:]{2}[./:-][0-9:]{2,4}(?:\D|$))", "", s)
    s = re.sub(r"^7(?=[0-3]\d[./-]\d{2}[./-]\d{2,4}(?:\D|$))", "", s)
    s = re.sub(r"\s*([./-])\s*", r"\1", s)
    s = re.sub(r"^(\d{2}),(\d{2})([./]\d{2,4})$", r"\1.\2\3", s)
    # If cell contains two adjacent dates (`Post Date` + `Value Date`), extract first
    m_two = re.match(
        r"^([0-9OoQqIl|SBZ]{2}[./-](?:[A-Za-z]{3}|[0-9OoQqIl|SBZ]{2})[./-][0-9OoQqIl|SBZ]{2,4})\s+[0-9OoQqIl|SBZ]{2}[./-](?:[A-Za-z]{3}|[0-9OoQqIl|SBZ]{2})[./-][0-9OoQqIl|SBZ]{2,4}$",
        s,
    )
    if m_two:
        s = m_two.group(1)
    # Repair colon/digit confusions in numeric dot-matrix dates (`3:-33-2025`, `04-34-2025`, `24-34-2:25`, `02-:0-2025`)
    m_num = re.match(r"^([0-9OoQqIl|SBZ:]{1,2})([./-])([0-9OoQqIl|SBZ:]{1,2})\2([0-9OoQqIl|SBZ:]{2,4})$", s)
    if m_num:
        ocr_tr = str.maketrans({"O": "0", "o": "0", "Q": "0", "I": "1", "l": "1", "|": "1", "S": "5", "B": "8", "Z": "2"})
        d_p, sep, m_p, y_p = (
            m_num.group(1).translate(ocr_tr),
            m_num.group(2),
            m_num.group(3).translate(ocr_tr),
            m_num.group(4).translate(ocr_tr),
        )
        d_p = d_p.replace(":", "1")
        if len(d_p) == 2 and d_p.isdigit() and int(d_p) > 31 and d_p[0] in ("3", "4", "5", "6", "7", "8", "9"):
            d_p = "0" + d_p[1]
        if ":" in m_p:
            m_p = "10" if m_p == ":0" else m_p.replace(":", "0")
        if len(m_p) == 2 and m_p.isdigit() and int(m_p) > 12:
            if m_p[0] in ("2", "3", "6", "8"):
                m_p = "0" + m_p[1]
        y_p = y_p.replace(":", "0")
        if len(y_p) == 4 and y_p.isdigit() and y_p[0] == "2" and y_p[1] in ("3", "6", "8", "9") and y_p[2] in ("0", "1", "2", "3"):
            y_p = "20" + y_p[2:]
        if len(y_p) == 2 and y_p.isdigit() and 70 <= int(y_p) <= 79:
            y_p = "2" + y_p[1]
        s = f"{d_p}{sep}{m_p}{sep}{y_p}"
    return s


def parse_indian_date(raw: Any, order: Optional[str] = DATE_ORDER_AUTO, ocr: bool = True) -> Optional[str]:
    """
    Parse common Indian bank statement dates into ISO YYYY-MM-DD.

    `order` fixes the day/month order of purely numeric dates: "DMY", "MDY" or "auto" (day-first unless only a
    month-first reading is valid, e.g. `12/25/2025`). Use `infer_date_order` to decide it once per statement.
    `ocr=True` additionally applies OCR glyph/digit repairs (`3:-33-2025`, `10.05.75`); digital sources should
    pass `ocr=False` so impossible dates stay unparsed instead of being "repaired" into a different day.
    """
    order = _normalize_order(order)
    res = _parse_date_basic(raw, order)
    if not ocr:
        if res is None:
            light = _light_clean_date_text(raw)
            if light and light != _normalize_date_text(raw):
                res = _parse_date_basic(light, order)
        return res
    cleaned = _clean_ocr_date_text(raw)
    if res is not None:
        # Handle 2-digit OCR year misreads like "10.05.75" that would otherwise parse as 1975-05-10
        if int(res[:4]) < 2000 and cleaned and cleaned != str(raw or "").strip():
            res_clean = _parse_date_basic(cleaned, order)
            if res_clean is not None and int(res_clean[:4]) >= 2000:
                return res_clean
        return res
    if not cleaned:
        return None
    return _parse_date_basic(cleaned, order)


def _numeric_date_parts(raw: Any) -> Optional[Tuple[int, int, str]]:
    m = _NUMERIC_DATE_PREFIX_RE.match(_normalize_date_text(raw))
    return (int(m.group(1)), int(m.group(2)), m.group(3)) if m else None


def infer_date_order(raw_dates: Iterable[Any], period: Sequence[Any] = ()) -> Tuple[str, bool, str]:
    """
    Decide the day/month order of numeric dates once per statement (FR-ING-06.3).

    Evidence, strongest first: any field > 12 (in rows or the stated period), then fit inside the stated period,
    then chronological monotonicity (ascending or descending), then a much shorter overall span. Without any
    discriminating evidence the Indian default (DMY) is returned with `decided=False`.
    Returns (order, decided, basis).
    """
    triples = [t for t in (_numeric_date_parts(r) for r in raw_dates) if t is not None]
    period_triples = [t for t in (_numeric_date_parts(p) for p in period) if t is not None]
    evidence = triples + period_triples
    dmy_hits = sum(1 for a, b, _ in evidence if a > 12 >= b)
    mdy_hits = sum(1 for a, b, _ in evidence if b > 12 >= a)
    if dmy_hits or mdy_hits:
        if dmy_hits >= mdy_hits:
            return DATE_ORDER_DMY, mdy_hits == 0, "day field > 12" if mdy_hits == 0 else "mixed day/month evidence"
        return DATE_ORDER_MDY, dmy_hits == 0, "day field > 12" if dmy_hits == 0 else "mixed day/month evidence"
    if not any(a != b for a, b, _ in triples):
        return DATE_ORDER_DMY, True, "no ambiguous dates"

    def _dates(order: str, src: Sequence[Tuple[int, int, str]]) -> List[dt.date]:
        out: List[dt.date] = []
        for a, b, y in src:
            iso = _safe_date(_expand_year(y), a, b) if order == DATE_ORDER_MDY else _safe_date(_expand_year(y), b, a)
            if iso is not None:
                out.append(dt.date.fromisoformat(iso))
        return out

    def _score(order: str) -> Tuple[int, int, int]:
        ds = _dates(order, triples)
        invalid = len(triples) - len(ds)
        asc_inv = sum(1 for i in range(1, len(ds)) if ds[i] < ds[i - 1])
        desc_inv = sum(1 for i in range(1, len(ds)) if ds[i] > ds[i - 1])
        outside = 0
        pd = _dates(order, period_triples)
        if len(pd) == 2:
            lo, hi = min(pd), max(pd)
            outside = sum(1 for d in ds if not lo <= d <= hi)
        return outside, invalid, min(asc_inv, desc_inv)

    s_dmy, s_mdy = _score(DATE_ORDER_DMY), _score(DATE_ORDER_MDY)
    if s_dmy != s_mdy:
        basis = "statement period" if s_dmy[0] != s_mdy[0] else "chronological monotonicity"
        return (DATE_ORDER_DMY if s_dmy < s_mdy else DATE_ORDER_MDY), True, basis

    def _span(order: str) -> int:
        ds = _dates(order, triples)
        return (max(ds) - min(ds)).days if ds else 0

    span_dmy, span_mdy = _span(DATE_ORDER_DMY), _span(DATE_ORDER_MDY)
    if span_dmy < span_mdy:
        return DATE_ORDER_DMY, True, "shorter statement span"
    return DATE_ORDER_DMY, False, "undecidable; Indian day-first default"


def parse_standardized_filename(filename: str) -> Dict[str, Optional[str]]:
    """
    Parse forensic standardized filename:
    <CaseID>_<EntityName>_<BankCode>_<AccountNo>[_<Period>].ext
    """
    stem = os.path.splitext(os.path.basename(filename))[0]
    parts = stem.split("_")
    meta: Dict[str, Optional[str]] = {
        "case_hint": None,
        "holder_hint": None,
        "bank_code_hint": None,
        "account_hint": None,
        "period_hint_raw": None,
        "period_start_hint": None,
        "period_end_hint": None,
    }
    if len(parts) >= 4:
        meta["case_hint"] = parts[0]
        # Find bank code token among parts
        for idx, tok in enumerate(parts):
            if tok.upper() in BANK_BY_CODE:
                meta["bank_code_hint"] = tok.upper()
                if idx > 1:
                    meta["holder_hint"] = " ".join(parts[1:idx]).replace("-", " ").title()
                if idx + 1 < len(parts):
                    acct = re.sub(r"[^A-Za-z0-9]", "", parts[idx + 1])
                    if len(acct) >= 4:
                        meta["account_hint"] = acct
                if idx + 2 < len(parts):
                    raw_period = "_".join(parts[idx + 2 :])
                    period = _parse_filename_period(raw_period)
                    if period:
                        meta["period_hint_raw"] = raw_period
                        meta["period_start_hint"], meta["period_end_hint"] = period
                break
    return meta


_MONTH_ABBR = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), 1)}


def _compact_date(tok: str, end: bool) -> Optional[str]:
    """8-digit YYYYMMDD / DDMMYYYY, or MonYY / MonYYYY (first or last day of that month)."""
    if re.fullmatch(r"\d{8}", tok):
        y, m, d = int(tok[:4]), int(tok[4:6]), int(tok[6:])
        if 1900 <= y <= 2100:
            iso = _safe_date(y, m, d)
            if iso:
                return iso
        return _safe_date(int(tok[4:]), int(tok[2:4]), int(tok[:2]))
    m_mon = re.fullmatch(r"([A-Z]{3})[A-Z]*[-.]?(\d{2}|\d{4})", tok.upper())
    if m_mon and m_mon.group(1) in _MONTH_ABBR:
        y = _expand_year(m_mon.group(2))
        if y is None:
            return None
        mon = _MONTH_ABBR[m_mon.group(1)]
        if not end:
            return _safe_date(y, mon, 1)
        nxt = dt.date(y + (mon == 12), mon % 12 + 1, 1)
        return (nxt - dt.timedelta(days=1)).isoformat()
    return None


def _parse_filename_period(raw: str) -> Optional[Tuple[str, str]]:
    """
    Period token of a standardized filename (ING-18): `01042025-31032026`, `20250401to20260331`,
    `2025-04-01_2026-03-31`, `APR25-MAR26`, `FY2024-25` / `FY24-25`. Returns (start_iso, end_iso) or None.
    """
    tok = raw.strip().upper()
    m_fy = re.fullmatch(r"FY[-_]?(\d{2}|\d{4})(?:[-_]?(\d{2}|\d{4}))?", tok)
    if m_fy:
        y = _expand_year(m_fy.group(1))
        if y is None:
            return None
        return f"{y:04d}-04-01", f"{y + 1:04d}-03-31"
    m_iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:_|-|TO)(\d{4})-(\d{2})-(\d{2})", tok)
    if m_iso:
        a = _safe_date(int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3)))
        b = _safe_date(int(m_iso.group(4)), int(m_iso.group(5)), int(m_iso.group(6)))
        return (a, b) if a and b and a <= b else None
    m_pair = re.fullmatch(r"([A-Z0-9.]+?)(?:_|-|TO)([A-Z0-9.]+)", tok)
    if m_pair:
        a = _compact_date(m_pair.group(1), end=False)
        b = _compact_date(m_pair.group(2), end=True)
        if a and b and a <= b:
            return a, b
    return None


_HOLDER_STOPWORDS = frozenset(
    {"MR", "MRS", "MS", "MISS", "SHRI", "SMT", "KUMARI", "DR", "M", "S", "MS", "THE", "AND", "SO", "DO", "WO"}
)


def _holder_tokens(name: Optional[str]) -> set:
    return {t for t in re.split(r"[^A-Z0-9]+", (name or "").upper()) if len(t) > 1 and t not in _HOLDER_STOPWORDS}


def _account_digits(acc: str) -> str:
    return re.sub(r"\D", "", acc or "")


def _crosscheck_account(header_acc: Optional[str], file_acc: Optional[str]) -> Tuple[Optional[str], str, Optional[str]]:
    """
    Reconcile the header account number with the filename's (ING-18). Returns (account, status, warning).
    A masked header (`XXXXXX4001`) is replaced by a filename number with the same visible trailing digits; two
    full numbers that disagree keep the header (the evidence) and warn.
    """
    if not header_acc:
        return (file_acc, "filename_only", None) if file_acc else (None, "missing", None)
    if not file_acc:
        return header_acc, "header_only", None
    masked = bool(re.search(r"[X*]", header_acc, re.IGNORECASE))
    file_digits = _account_digits(file_acc)
    if masked:
        visible = re.search(r"(\d+)$", header_acc)
        tail = visible.group(1) if visible else ""
        lead = re.match(r"^(\d+)", header_acc)
        head = lead.group(1) if lead else ""
        same_len = len(header_acc) == len(file_digits)
        if len(tail) >= 3 and file_digits.endswith(tail) and (not head or file_digits.startswith(head)):
            note = None if same_len else " (lengths differ)"
            return (
                file_acc,
                "masked_header_resolved",
                f"Header account number {header_acc} is masked; using the full number {file_acc} from the filename, "
                f"whose last digits match{note or ''}.",
            )
        return (
            header_acc,
            "conflict",
            f"Account number conflict: masked header account {header_acc} does not match filename account "
            f"{file_acc}; kept the header value.",
        )
    header_digits = _account_digits(header_acc)
    if header_digits.lstrip("0") == file_digits.lstrip("0"):
        return header_acc, "match", None
    return (
        header_acc,
        "conflict",
        f"Account number conflict: header says {header_acc} but filename says {file_acc}; kept the header value.",
    )


def _crosscheck_holder(header_holder: Optional[str], file_holder: Optional[str]) -> Tuple[str, Optional[str]]:
    if not header_holder:
        return ("filename_only" if file_holder else "missing"), None
    if not file_holder:
        return "header_only", None
    h, f = _holder_tokens(header_holder), _holder_tokens(file_holder)
    if not h or not f:
        return "unknown", None
    common = h & f
    if len(common) >= min(2, len(f), len(h)):
        return "match", None
    if common:
        return "partial", None
    return "conflict", (
        f"Account holder conflict: header says '{header_holder}' but filename says '{file_holder}'; kept the header value."
    )


_ACCT_TYPE_LABEL_RE = re.compile(
    r"\b(?:(?:account|a/c|acct)\s*type|type\s+of\s+(?:account|a/c)|product(?:\s*(?:name|type|code))?|"
    r"scheme(?:\s*(?:name|type|code))?|account\s+category)\s*[:\-]\s*([^\n|,;]{2,40})",
    re.IGNORECASE,
)
_CURRENT_VALUE_RE = re.compile(r"\b(?:current|ca|od|overdraft|cash\s*credit|cc|o/d)\b", re.IGNORECASE)
_SAVINGS_VALUE_RE = re.compile(r"\b(?:savings?|sb|sba|salary|regular\s+sav)\b", re.IGNORECASE)
_CURRENT_PHRASE_RE = re.compile(
    r"\b(?:current\s+(?:account|a/c|acct)|ca\s+a/c|overdraft(?:\s+(?:account|a/c|limit))?|cash\s+credit|"
    r"cc\s+a/c|od\s+(?:a/c|account|acct|limit))\b",
    re.IGNORECASE,
)
_SAVINGS_PHRASE_RE = re.compile(r"\b(?:savings?\s+(?:bank\s+)?(?:account|a/c|acct)|sb\s+a/c)\b", re.IGNORECASE)


def _detect_account_type(preamble_lines: Sequence[str], non_txn_lines: Sequence[str]) -> Optional[str]:
    """
    Account type from a labelled header field first ('Account Type: Current'), then from explicit account-type
    phrases in non-transaction lines. Stray words ('Current Balance', an 'OD' branch code, body narrations) never
    decide it (ING-23).
    """
    for lines in (preamble_lines, non_txn_lines):
        for ln in lines:
            for m in _ACCT_TYPE_LABEL_RE.finditer(ln):
                val = m.group(1)
                if _CURRENT_VALUE_RE.search(val):
                    return "Current/OD"
                if _SAVINGS_VALUE_RE.search(val):
                    return "Savings"
    for lines in (preamble_lines, non_txn_lines):
        text = "\n".join(lines)
        cur, sav = _CURRENT_PHRASE_RE.search(text), _SAVINGS_PHRASE_RE.search(text)
        if cur and (not sav or cur.start() < sav.start()):
            return "Current/OD"
        if sav:
            return "Savings"
    # Not stated: never guess (EDGE-17). SFT/loan logic report the assumption they make for unknown types.
    return None


# Placeholder profile when no evidence identifies the bank: never guess a bank or fabricate an IFSC (ING-07)
UNKNOWN_BANK = BankProfile("UNKNOWN", "Unknown Bank", "", ())

_IFSC_RE = re.compile(r"\b([A-Z]{4}[0O][A-Z0-9]{6})\b")
_IFSC_LABELLED_RE = re.compile(r"\bIFS(?:C|\s*CODE)\b(?:\s*CODE)?\s*(?:NO\.?)?\s*[:\-]?\s*([A-Z]{4}[0O][A-Z0-9]{6})\b")
_TXN_LINE_RE = re.compile(
    r"^\s*(?:\d{1,4}[./-]\d{1,2}[./-]\d{1,4}|\d{1,2}[ -][A-Za-z]{3}[ -]\d{2,4})"
    r"|\b(?:UPI|NEFT|RTGS|IMPS|NACH|ACH|TRANSFER|TRF|TFR|BY\s+CLG|TO\s+CLG|CHQ|CHEQUE)\b",
    re.IGNORECASE,
)


def _normalize_ifsc(raw: str) -> str:
    raw = raw.upper()
    return raw[:4] + "0" + raw[5:].replace("O", "0")


def _extract_statement_ifsc(preamble_lines: Sequence[str], non_txn_lines: Sequence[str]) -> Optional[str]:
    """Statement IFSC from the header only (ING-09): a labelled IFSC anywhere outside the body, else an
    unlabelled code in a non-transaction preamble line. Counterparty IFSCs in narrations are never used."""
    for ln in list(preamble_lines) + [x for x in non_txn_lines if x not in preamble_lines]:
        m = _IFSC_LABELLED_RE.search(ln.upper())
        if m:
            return _normalize_ifsc(m.group(1))
    for ln in preamble_lines:
        if _TXN_LINE_RE.search(ln):
            continue
        for m in _IFSC_RE.finditer(ln.upper()):
            if not m.group(1).startswith("RBIS"):
                return _normalize_ifsc(m.group(1))
    return None


_BANKS_BY_IFSC_PREFIX: List[BankProfile] = sorted(
    (b for b in INDIAN_BANKS if len(b.ifsc_prefix) >= 4), key=lambda b: -len(b.ifsc_prefix)
)


def _bank_from_ifsc(ifsc: str) -> Optional[BankProfile]:
    up = ifsc.upper()
    for bank in _BANKS_BY_IFSC_PREFIX:  # longest registered prefix first (`BARB0BUPGBX` before `BARB`)
        if up.startswith(bank.ifsc_prefix.upper()):
            return bank
    return None


def _keyword_pattern(kw: str) -> "re.Pattern[str]":
    # IFSC-prefix keywords like `sbin0` are followed by branch digits, so only bound them on the left
    if re.fullmatch(r"[a-z]{4}0", kw):
        return re.compile(rf"(?<![a-z0-9]){re.escape(kw)}")
    return re.compile(rf"(?<![a-z0-9]){re.escape(kw)}(?![a-z0-9])")


_BANK_KEYWORD_PATTERNS: List[Tuple[int, int, "re.Pattern[str]", BankProfile]] = sorted(
    (
        (len(kw), order, _keyword_pattern(kw.lower()), bank)
        for order, bank in enumerate(INDIAN_BANKS)
        for kw in bank.keywords
    ),
    key=lambda item: (-item[0], item[1]),
)


def _bank_from_keywords(search_space: str) -> Optional[BankProfile]:
    """Longest word-boundary keyword wins (`central bank of india` beats `bank of india`; `kasbi` is not `sbi`)."""
    if re.search(r"\bunion\s+bank(?:\s+of\s+india)?\b", search_space) and "city union" not in search_space:
        return BANK_BY_CODE.get("UNION")
    for _len, _order, pat, bank in _BANK_KEYWORD_PATTERNS:
        if pat.search(search_space):
            return bank
    if (
        "pkgb" in search_space
        or "karnataka gramin" in search_space
        or (
            "new acct type" in search_space
            and "1011" in search_space
            and any(loc in search_space for loc in ("kagwad", "belagavi", "cr rt chg"))
        )
    ):
        return BANK_BY_CODE.get("KRGB")
    return None


def detect_bank_and_metadata(
    header_text: str, filename: str
) -> Tuple[BankProfile, Dict[str, Optional[str]]]:
    """Identify the Indian bank and extract account holder, account no, and IFSC from header/filename."""
    file_meta = parse_standardized_filename(filename)
    all_lines = header_text.splitlines()
    table_hdr_re = re.compile(
        r"\b(?:DATE|Txn\s+Date|Post\s+Date)\b.*\b(?:PARTICULARS|Description|Narration|Balance)\b|\bBROUGHT\s+FORWARD\b",
        re.IGNORECASE,
    )
    preamble_lines: List[str] = []
    saw_table_header = False
    for ln in all_lines[:24]:
        if re.search(r"\b(?:collecting\s+b(?:ank|ranch)|beneficiary|remitter)\b", ln, re.IGNORECASE):
            continue
        if table_hdr_re.search(ln):
            saw_table_header = True
            break
        preamble_lines.append(ln)
    if not preamble_lines and not saw_table_header:
        preamble_lines = all_lines[:14]

    # Exclude transaction body rows/continuations (counterparty banks like "IDFC FIRST BAN", "HDFC")
    non_txn_lines = list(preamble_lines)
    in_body = saw_table_header
    for ln in all_lines[len(preamble_lines) :]:
        if table_hdr_re.search(ln) or re.match(
            r"^\s*(?:\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b|\b(?:DEP\s+TFR|WDL\s+TFR|DEBIT\s+ACH|CREDIT\s+ACH|UPI/|VPI|NACH)\b)",
            ln,
            re.IGNORECASE,
        ):
            in_body = True
            continue
        if not in_body or "new acct type" in ln.lower() or _FOOTER_LINE_RE.match(ln) or _OCR_EXTRA_FOOTER_RE.search(ln):
            if _FOOTER_LINE_RE.match(ln) or _OCR_EXTRA_FOOTER_RE.search(ln):
                in_body = False
            non_txn_lines.append(ln)

    top_combined = ("\n".join(preamble_lines) + f"\n{filename}").lower()
    non_txn_combined = ("\n".join(non_txn_lines) + f"\n{filename}").lower()

    detection_warnings: List[str] = []
    preamble_text = "\n".join(preamble_lines)
    ifsc = _extract_statement_ifsc(preamble_lines, non_txn_lines)

    ifsc_bank = _bank_from_ifsc(ifsc) if ifsc else None
    file_bank = BANK_BY_CODE.get(file_meta["bank_code_hint"] or "")
    keyword_bank: Optional[BankProfile] = None
    for search_space in (top_combined, non_txn_combined):
        keyword_bank = _bank_from_keywords(search_space)
        if keyword_bank is not None:
            break

    # Precedence: statement IFSC prefix > standardized filename bank code > longest word-boundary keyword
    matched_bank: BankProfile = ifsc_bank or file_bank or keyword_bank or UNKNOWN_BANK
    if ifsc_bank and file_bank and ifsc_bank.bank_code != file_bank.bank_code:
        detection_warnings.append(
            f"Bank conflict: statement IFSC {ifsc} belongs to {ifsc_bank.bank_code} but filename says "
            f"{file_bank.bank_code}; using the IFSC."
        )
    elif keyword_bank and ifsc_bank and keyword_bank.bank_code != ifsc_bank.bank_code:
        detection_warnings.append(
            f"Bank conflict: header text suggests {keyword_bank.bank_code} but IFSC {ifsc} belongs to "
            f"{ifsc_bank.bank_code}; using the IFSC."
        )
    if matched_bank is UNKNOWN_BANK:
        detection_warnings.append("Bank could not be identified from IFSC, filename or header; marked UNKNOWN.")
    if ifsc is None:
        detection_warnings.append("Statement IFSC not found in the statement header; left empty.")

    # Extract Account Number (same-line first, then tilted two-line `:12345678901 \n Account No` or same-line A/c fallback)
    acc_m = re.search(
        r"(?<!of[ \t])(?:account|a/c)[ \t]*(?:no\.?|number|#)[ \t]*[:\-]?[ \t]*([X*\dOo]{6,18})\b",
        header_text,
        flags=re.IGNORECASE,
    )
    if not acc_m:
        acc_m = re.search(
            r":\s*([X*\dOo]{8,18})\b[^\n]*\n[^\n]{0,40}\b(?:account|a/c)\s*no\b",
            header_text,
            flags=re.IGNORECASE,
        )
    if not acc_m:
        acc_m = re.search(
            r"(?<!of[ \t])(?:account|a/c)[ \t]*[:\-][ \t]*([X*\dOo]{6,18})\b",
            header_text,
            flags=re.IGNORECASE,
        )
    header_acc: Optional[str] = None
    if acc_m:
        raw_acc = acc_m.group(1).strip()
        if sum(c.isdigit() for c in raw_acc) >= 6:
            raw_acc = raw_acc.replace("O", "0").replace("o", "0")
        header_acc = raw_acc.upper() if re.search(r"[x*]", raw_acc) else raw_acc
    resolved_acc, acc_status, acc_warning = _crosscheck_account(header_acc, file_meta["account_hint"])
    if acc_warning:
        detection_warnings.append(acc_warning)
    acc_no = resolved_acc or "UNKNOWN_AC"

    # Extract Account Holder Name
    _holder_stop = re.compile(
        r"(?:\s{2,}|\n|\b(?:branch|account|a/c|ifsc|date|address|page|type|product|nominee|micr|cif|sbsrp|statement|from|to|period|currency|ckyc)\b)",
        re.IGNORECASE,
    )
    holder_m = re.search(
        r"(?:account[ \t]*(?:holder(?:[ \t]*name)?|name)|customer[ \t]*name|(?<!nominee[ \t])(?<!father[ \t])(?<!branch[ \t])\bname)[ \t]*[:\-][ \t]*([A-Za-z][A-Za-z0-9 .,&/'-]{2,65})",
        header_text,
        flags=re.IGNORECASE,
    )
    if not holder_m:
        holder_m = re.search(
            r"\b(?:Mrs?\.?|Ms\.?|Miss|Shri|Smt\.?|M/s)\s+([A-Z][A-Za-z .,&/'-]{3,55})",
            header_text,
        )
        holder_raw = holder_m.group(0) if holder_m else ""
    else:
        holder_raw = holder_m.group(1)
    if not holder_raw:
        m_rel = re.search(
            r"\b([A-Z]{2,}(?:\s+[A-Z]{2,}){1,3}\s+(?:S/O|D/O|W/O|SO|DO|WO|AND)\s+[A-Z]{2,}(?:\s+[A-Z]{2,}){1,3})\b",
            preamble_text,
        )
        if m_rel:
            holder_raw = re.sub(r"^(?:MP|UP|RJ|PB|HR|KA|MH|DL|GJ)\s+", "", m_rel.group(1).strip())
    header_holder: Optional[str] = None
    if holder_raw:
        holder_clean = _holder_stop.split(holder_raw)[0].strip(" :-\t.,")
        if len(holder_clean) >= 3:
            header_holder = holder_clean
    holder_name = header_holder or file_meta["holder_hint"] or f"Holder {acc_no}"
    holder_status, holder_warning = _crosscheck_holder(header_holder, file_meta["holder_hint"])
    if holder_warning:
        detection_warnings.append(holder_warning)

    # Account type from labelled header fields / explicit phrases only (ING-23)
    acc_type = _detect_account_type(preamble_lines, non_txn_lines)

    bank_status = "unknown"
    if file_bank is None:
        bank_status = "header_only" if matched_bank is not UNKNOWN_BANK else "missing"
    elif ifsc_bank is not None:
        bank_status = "match" if ifsc_bank.bank_code == file_bank.bank_code else "conflict"
    elif keyword_bank is not None:
        bank_status = "match" if keyword_bank.bank_code == file_bank.bank_code else "conflict"
    else:
        bank_status = "filename_only"

    return matched_bank, {
        "ifsc_code": ifsc,
        "account_number": acc_no,
        "account_holder_name": holder_name,
        "account_type": acc_type,
        "detection_warnings": detection_warnings,
        "filename_crosscheck": {
            "bank": bank_status,
            "account": acc_status,
            "holder": holder_status,
            "header_account_number": header_acc,
            "filename_account_number": file_meta["account_hint"],
            "header_holder_name": header_holder,
            "filename_holder_name": file_meta["holder_hint"],
            "filename_period": (
                {"start": file_meta["period_start_hint"], "end": file_meta["period_end_hint"]}
                if file_meta["period_start_hint"]
                else None
            ),
        },
    }


_BOM_ENCODINGS: Tuple[Tuple[bytes, str], ...] = (
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
)


_NUL_MAX_SHARE = 0.01


def _is_multibyte_encoding(name: str) -> bool:
    n = codecs.lookup(name).name if name else ""
    return n.startswith(("utf", "gb", "big5", "shift_jis", "euc", "iso2022", "cp932", "cp949", "cp950", "hz",
                         "johab"))


def decode_statement_text(raw_bytes: bytes, warnings: Optional[List[str]] = None) -> Tuple[str, str]:
    """
    Decode a text statement: BOM sniff -> BOM-less UTF-16 sniff -> strict utf-8 -> charset_normalizer (if
    installed; multi-byte encodings only) -> cp1252 -> latin-1. Returns (text, encoding).

    Leading/trailing NUL padding (written by some core-banking exporters) is stripped; a few stray NULs are
    removed with a warning appended to ``warnings``. Raises ValueError when NULs make up more than 1% of the
    text, i.e. the content is binary rather than a text statement.
    """
    text: Optional[str] = None
    encoding = ""
    for bom, enc in _BOM_ENCODINGS:
        if raw_bytes.startswith(bom):
            try:
                text, encoding = raw_bytes.decode(enc), enc
            except UnicodeDecodeError:
                text = None
            break
    if text is None:
        head = raw_bytes[:4096]
        if len(head) >= 4 and head.count(b"\x00") >= len(head) // 3:
            enc = "utf-16-le" if head[1::2].count(0) > head[0::2].count(0) else "utf-16-be"
            try:
                text, encoding = raw_bytes.decode(enc), enc
            except UnicodeDecodeError:
                text = None
    if text is None:
        try:
            text, encoding = raw_bytes.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text = None
    if text is None:
        # The detector only decides between multi-byte encodings (GB18030, Shift-JIS, EUC-*, Big5...). On short
        # single-byte statements every Latin codepage ties, so its guess there is arbitrary (a cp1252 export came
        # back as mac_latin2 and lost its en dash); single-byte text uses the Windows default, cp1252.
        try:
            from charset_normalizer import from_bytes  # type: ignore

            best = from_bytes(raw_bytes).best()
            if best is not None and best.encoding and _is_multibyte_encoding(best.encoding):
                text, encoding = str(best), best.encoding
        except ImportError:
            pass
        except Exception:  # noqa: BLE001 - detector failures fall through to the deterministic chain
            text = None
    if text is None:
        for enc in ("cp1252", "latin-1"):
            try:
                text, encoding = raw_bytes.decode(enc), enc
                break
            except UnicodeDecodeError:
                continue
    assert text is not None  # latin-1 decodes every byte sequence
    if "\x00" in text:
        text = text.strip("\x00")
        stray = text.count("\x00")
        if stray > len(text) * _NUL_MAX_SHARE:
            raise ValueError("File contains NUL bytes after decoding; it looks binary, not a text statement.")
        if stray:
            text = text.replace("\x00", "")
            msg = f"Removed {stray} stray NUL character(s) from the text statement."
            _LOG.warning(msg)
            if warnings is not None:
                warnings.append(msg)
    return text, encoding


_QUOTED_FIELD_RE = re.compile(r'"(?:[^"]|"")*"')
_DIGIT_COMMA_RE = re.compile(r"(?<=\d),(?=\d)")


def _choose_delimiter(lines: Sequence[str], candidates: Sequence[str], default: Optional[str]) -> Optional[str]:
    """Pick the delimiter whose (non-zero) per-line count is most consistent, ignoring commas inside numbers."""
    masked = [_DIGIT_COMMA_RE.sub("", _QUOTED_FIELD_RE.sub('""', ln)) for ln in lines]
    best: Optional[str] = None
    best_score = 0
    for cand in candidates:
        counts = [ln.count(cand) for ln in masked if ln.count(cand) > 0]
        if not counts:
            continue
        modal = max(set(counts), key=lambda c: (counts.count(c), c))
        support = counts.count(modal)
        if support >= 2 and support >= 0.5 * len(counts) and support > best_score:
            best, best_score = cand, support
    return best or default


_AMOUNT_HEAD_RE = re.compile(r"^-?\d{1,3}$")
_AMOUNT_GROUP_RE = re.compile(r"^\d{2,3}$")
_AMOUNT_TAIL_RE = re.compile(r"^\d{3}(?:\.\d{1,2})?$")


def _rejoin_split_amounts(row: List[str], target_width: int) -> List[str]:
    """Re-join unquoted Indian/Western amounts that a comma delimiter split (`1,50,000.00` -> 3 cells)."""
    excess = len(row) - target_width
    if excess <= 0:
        return row
    out: List[str] = []
    i = 0
    while i < len(row):
        if excess > 0 and _AMOUNT_HEAD_RE.match(row[i]):
            j = i
            while (
                j + 1 < len(row)
                and (j - i) < excess
                and "." not in row[j]
                and (_AMOUNT_GROUP_RE.match(row[j + 1]) or _AMOUNT_TAIL_RE.match(row[j + 1]))
            ):
                j += 1
            if j > i and _AMOUNT_TAIL_RE.match(row[j]):
                out.append(",".join(row[i : j + 1]))
                excess -= j - i
                i = j + 1
                continue
        out.append(row[i])
        i += 1
    return out


def _fixed_width_rows(lines: Sequence[str]) -> List[List[str]]:
    """Split a fixed-width text statement by the header's column offsets (blank cells keep their column)."""
    token_re = re.compile(r"\S+(?: \S+)*")
    header_i = -1
    for i, ln in enumerate(lines[:60]):
        low = ln.lower()
        if re.search(r"\bdate\b", low) and re.search(r"\b(?:balance|narration|particulars|description|debit|credit|withdrawal|deposit)", low):
            if len(token_re.findall(ln)) >= 3:
                header_i = i
                break
    if header_i < 0:
        return [re.split(r"\s{2,}", ln.strip()) for ln in lines if ln.strip()]

    spans = [(m.start(), m.end()) for m in token_re.finditer(lines[header_i])]
    rows: List[List[str]] = [re.split(r"\s{2,}", ln.strip()) for ln in lines[:header_i] if ln.strip()]
    rows.append([lines[header_i][a:b] for a, b in spans])
    for ln in lines[header_i + 1 :]:
        if not ln.strip():
            continue
        cells = [""] * len(spans)
        for m in token_re.finditer(ln):
            ts, te = m.start(), m.end()
            best_k, best_key = 0, None
            for k, (hs, he) in enumerate(spans):
                overlap = min(te, he) - max(ts, hs)
                gap = max(0, hs - te, ts - he)
                key = (overlap if overlap > 0 else 0, -gap)
                if best_key is None or key > best_key:
                    best_k, best_key = k, key
            cells[best_k] = f"{cells[best_k]} {m.group(0)}".strip()
        rows.append(cells)
    return rows


def _decode_delimited_rows(raw_bytes: bytes, ext: str, warnings: Optional[List[str]] = None) -> List[List[str]]:
    text, _encoding = decode_statement_text(raw_bytes, warnings=warnings)
    lines = text.splitlines()
    sample = [ln for ln in lines[:60] if ln.strip()]
    if ext == ".tsv":
        delimiter: Optional[str] = _choose_delimiter(sample, ("\t",), "\t")
    elif ext == ".txt":
        delimiter = _choose_delimiter(sample, ("|", "\t", ";", ","), None)
        if delimiter is None:
            return _fixed_width_rows(lines)
    else:
        delimiter = _choose_delimiter(sample, (",", ";", "\t", "|"), ",")

    reader = csv.reader(io.StringIO(text), delimiter=delimiter or ",")
    rows = [[cell.strip() for cell in row] for row in reader if any(c.strip() for c in row)]
    if delimiter == "," and rows:
        hdr_idx, _hdr_map = _scan_header_row(rows)
        if hdr_idx >= 0:
            hdr = rows[hdr_idx]
            target = len(hdr)
            while target > 0 and not str(hdr[target - 1]).strip():
                target -= 1
            rows = rows[: hdr_idx + 1] + [
                _rejoin_split_amounts(r, target) if len(r) > target else r for r in rows[hdr_idx + 1 :]
            ]
    return rows


_EXTRA_HEADER_SYNONYMS: Dict[str, List[str]] = {
    "narration": ["partlculars", "particuiars", "descr1ption"],
    "debit": ["withdrawl", "withdrawls", "debits", "wr:tdrawals", "w:thdrawals"],
    "credit": ["crodit", "credits", "depos:ts", "depos:7s", "depos7s"],
    "balance": ["balanoe"],
}
for _hf_k, _hf_vals in _EXTRA_HEADER_SYNONYMS.items():
    _existing = tuple(_HEADER_SYNONYMS.get(_hf_k, ()))
    _HEADER_SYNONYMS[_hf_k] = tuple(dict.fromkeys(_existing + tuple(_hf_vals)))


def _header_matches(cell_low: str, synonym: str) -> bool:
    if cell_low == synonym:
        return True
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(synonym)}(?![a-z0-9])", cell_low))


_FIELD_ORDER = {name: i for i, name in enumerate(_HEADER_SYNONYMS)}


def _classify_header_cell(low: str, taken: Dict[str, int]) -> Optional[str]:
    """
    Most specific synonym wins (ING-12): the longest matching synonym decides, and the generic `amount` field
    only applies when nothing more specific matches ("Balance Amount" -> balance, "Debit Amount" -> debit).
    """
    def hits(field_name: str) -> bool:
        return any(_header_matches(low, syn) for syn in _HEADER_SYNONYMS[field_name])

    # A combined amount column ("Debit/Credit", "Amount (Dr/Cr)") carries signed or Dr/Cr-tagged values and
    # must go through the signed/type/balance-delta resolution of the generic `amount` field (ING-12/13).
    is_type_marker = hits("type")
    combined = (hits("debit") and hits("credit") and not is_type_marker) or (hits("amount") and is_type_marker)
    if combined:
        if "amount" not in taken:
            return "amount"
        if "type" not in taken:
            return "type"
    best: Optional[Tuple[int, int, int]] = None
    best_field: Optional[str] = None
    for field_name, syns in _HEADER_SYNONYMS.items():
        if field_name in taken:
            continue
        hit = max((len(syn) for syn in syns if _header_matches(low, syn)), default=0)
        if not hit:
            continue
        key = (0 if field_name == "amount" else 1, hit, -_FIELD_ORDER[field_name])
        if best is None or key > best:
            best, best_field = key, field_name
    return best_field


def _scan_header_row(rows: Sequence[Sequence[Any]]) -> Tuple[int, Dict[str, int]]:
    """Find the transaction table header among the first 40 rows; (-1, {}) when none is present."""
    best_idx = -1
    best_score = 0
    best_map: Dict[str, int] = {}

    for idx, row in enumerate(rows[:40]):
        row_low = " ".join(str(c or "").strip().lower() for c in row)
        # Reject bottom summary footers like `Ope Bal | Dr count | Cr count | Debits | Credits | Clo Bal`
        if any(w in row_low for w in ("ope bal", "clo bal", "dr count", "cr count", "uncl bal", "clr bal")) and "particulars" not in row_low and "narration" not in row_low:
            continue
        col_map: Dict[str, int] = {}
        for c_i, cell in enumerate(row):
            low = re.sub(r"\s+", " ", str(cell or "").strip().lower())
            if not low or len(low) > 60:
                continue
            field_name = _classify_header_cell(low, col_map)
            if field_name is not None:
                col_map[field_name] = c_i
        if "date" not in col_map and "value_date" in col_map:
            col_map["date"] = col_map.pop("value_date")
        if "debit" in col_map and "balance" in col_map and "credit" not in col_map and col_map["balance"] - col_map["debit"] == 2:
            col_map["credit"] = col_map["debit"] + 1
        elif "credit" in col_map and "balance" in col_map and "debit" not in col_map and col_map["credit"] - col_map.get("ref", col_map.get("narration", 1)) == 2:
            col_map["debit"] = col_map["credit"] - 1
        score = len(col_map)
        if "date" in col_map and ("narration" in col_map or "balance" in col_map) and score > best_score:
            best_score = score
            best_idx = idx
            best_map = col_map
    return best_idx, best_map


def _map_header_columns(rows: List[List[Any]]) -> Tuple[int, Dict[str, int]]:
    """Locate header row index and map canonical fields to column indices with word-boundary & headerless fallback."""
    best_idx, best_map = _scan_header_row(rows)
    if best_idx != -1:
        return best_idx, best_map

    # Data-driven headerless fallback: inspect up to first 20 dated rows across columns
    dated_indices = [
        idx for idx, row in enumerate(rows)
        if row and (parse_indian_date(row[0]) or (len(row) > 1 and parse_indian_date(row[1])))
    ]
    if dated_indices:
        sample = [rows[idx] for idx in dated_indices[:20]]
        max_c = max(len(r) for r in sample)
        if max_c >= 4:
            date_cols: List[int] = []
            money_hits = [0] * max_c
            crdr_hits = [0] * max_c
            alpha_lens = [0] * max_c
            int_only_hits = [0] * max_c
            n_rows = len(sample)

            for c in range(max_c):
                d_hits = 0
                for r in sample:
                    val = str(r[c] if c < len(r) else "").strip()
                    if not val:
                        continue
                    if parse_indian_date(val):
                        d_hits += 1
                    if re.search(r"\d[\d,]*\.\d{2}", val):
                        money_hits[c] += 1
                    elif val.isdigit() and len(val) >= 4:
                        int_only_hits[c] += 1
                    up = val.upper()
                    if up in {"DR", "CR", "DEBIT", "CREDIT", "D", "C"}:
                        crdr_hits[c] += 1
                    elif up not in {"NIL", "NA", "-"}:
                        alpha_lens[c] += sum(1 for ch in val if ch.isalpha())
                if c <= 2 and d_hits >= max(1, n_rows // 3):
                    date_cols.append(c)

            inferred: Dict[str, int] = {}
            inferred["date"] = date_cols[0] if date_cols else 0
            if len(date_cols) >= 2:
                inferred["value_date"] = date_cols[1]
            last_d = date_cols[-1] if date_cols else 0

            # Rightmost money column is Running Balance
            bal_candidates = [c for c in range(max_c - 1, last_d, -1) if money_hits[c] > 0]
            bal_col = bal_candidates[0] if bal_candidates else (max_c - 1)
            inferred["balance"] = bal_col

            # Longest alphabetic column between date and balance is Narration
            mid_cols = list(range(last_d + 1, bal_col))
            if mid_cols:
                narr_col = max(mid_cols, key=lambda c: (alpha_lens[c], -c))
                inferred["narration"] = narr_col
                right_mid = [c for c in range(narr_col + 1, bal_col)]
                if len(right_mid) >= 3:
                    inferred["ref"] = right_mid[0]
                    inferred["debit"] = right_mid[-2]
                    inferred["credit"] = right_mid[-1]
                elif len(right_mid) == 2:
                    if crdr_hits[right_mid[1]] >= max(1, n_rows // 2):
                        inferred["amount"] = right_mid[0]
                        inferred["type"] = right_mid[1]
                    elif int_only_hits[right_mid[0]] > money_hits[right_mid[0]] and money_hits[right_mid[1]] > 0:
                        inferred["ref"] = right_mid[0]
                        inferred["amount"] = right_mid[1]
                    else:
                        inferred["debit"] = right_mid[0]
                        inferred["credit"] = right_mid[1]
                elif len(right_mid) == 1:
                    inferred["amount"] = right_mid[0]
                return dated_indices[0] - 1, inferred

    return 0, {"date": 0, "narration": 1, "debit": 2, "credit": 3, "balance": 4}


def _extract_header_opening_balance(
    header_text: str, raw_rows: List[List[Any]], header_idx: int, exclude_lines: Optional[Sequence[str]] = None
) -> Optional[Decimal]:
    """Check statement preamble, header_idx+1 opening-balance row, or BROUGHT FORWARD row for a starting balance.

    ``exclude_lines`` are transaction-table body lines: a mid-table "Balance B/F" page carry-forward is not the
    statement's opening balance.
    """
    excluded = {_norm_line(x) for x in (exclude_lines or ()) if x and x.strip()}
    if header_idx >= 0:
        for r in raw_rows[header_idx + 1 : min(len(raw_rows), header_idx + 3)]:
            joined_r = " ".join(str(c or "") for c in r if str(c or "").strip())
            if not joined_r:
                continue
            has_alpha_narr = bool(re.search(r"[A-Za-z]{4,}", re.sub(r"\b(?:BROUGHT\s+FORWARD|CR|DR|INR|RS)\b", "", joined_r, flags=re.IGNORECASE)))
            has_date = any(parse_indian_date(c) for c in r[:2] if c)
            if not has_date and (not has_alpha_narr or re.search(r"brought\s+forward|b/f|opening", joined_r, re.IGNORECASE)):
                m_amts = re.findall(r"[\d,]+\.\d{2}(?:\s*(?:C[rR:]|D[rR:]))?", joined_r)
                if m_amts:
                    val, hint = parse_indian_amount(m_amts[-1])
                    if val > 0:
                        return -val if hint == "DR" else val

    lines_to_check: List[str] = list(header_text.splitlines())
    for r in raw_rows[: max(12, header_idx + 4)]:
        lines_to_check.append(" ".join(str(c or "") for c in r if str(c or "").strip()))

    op_re = re.compile(r"(?:opening\s+balance|brought\s+forward|b/f\s+balance|balance\s+b/f)", re.IGNORECASE)
    amt_pat = re.compile(r"(?:Rs\.?\s*|INR\s*|₹\s*)?[\d,]+\.\d{2}(?:\s*(?:Cr|Dr))?", re.IGNORECASE)
    for ln in lines_to_check:
        if op_re.search(ln) and _norm_line(ln) not in excluded:
            matches = amt_pat.findall(ln)
            if matches:
                preferred = [m for m in matches if re.search(r"(?:Cr|Dr)\s*$", m, re.IGNORECASE)]
                chosen = preferred[-1] if preferred else matches[-1]
                val, hint = parse_indian_amount(chosen)
                if val > 0:
                    return -val if hint == "DR" else val
    return None


def _extract_footer_closing_balance(raw_rows: Sequence[Sequence[Any]]) -> Optional[Decimal]:
    """Extract footer summary closing balance (`Clr Bal`, `Cir Bal`, `Clo Bal`, `Cumulative Totals`) when printed."""
    tail_rows = raw_rows[max(0, len(raw_rows) - 18) :]
    amt_pat = re.compile(r"(?:Rs\.?\s*|INR\s*|₹\s*)?[\d,]+\.\d{0,2}(?:\s*(?:Cr|Dr))?", re.IGNORECASE)
    for idx, r in enumerate(tail_rows):
        ln = " ".join(str(c or "") for c in r if str(c or "").strip())
        if re.search(r"(?:c[li1]r\s*bal|closing\s+bal|cumulative\s+totals?|cr\.?\s*ia_ve\s+so:?als?)", ln, re.IGNORECASE):
            m = re.search(r"(?:c[li1]r\s*bal|closing\s+balance)\s*[:\-]?\s*((?:Rs\.?\s*|INR\s*|₹\s*)?[\d,]+\.\d{2}(?:\s*(?:Cr|Dr))?)", ln, re.IGNORECASE)
            if m:
                val, hint = parse_indian_amount(m.group(1))
                if val > 0:
                    return -val if hint == "DR" else val
            matches = [x for x in amt_pat.findall(ln) if re.search(r"\d", x)]
            if matches:
                val, hint = parse_indian_amount(matches[-1])
                if val > 0:
                    return -val if hint == "DR" else val
        if re.search(r"\bclo(?:sing)?\s*bal\b", ln, re.IGNORECASE) and idx + 1 < len(tail_rows):
            nxt_ln = " ".join(str(c or "") for c in tail_rows[idx + 1] if str(c or "").strip())
            matches = amt_pat.findall(nxt_ln)
            if matches:
                val, hint = parse_indian_amount(matches[-1])
                if val > 0:
                    return -val if hint == "DR" else val
    return None


def _count_unsigned_continuity(txns: List[NormalizedTransaction], tol: Decimal = Decimal("1.00")) -> int:
    """Count consecutive rows whose balance delta magnitude matches transaction amount."""
    hits = 0
    for i in range(1, len(txns)):
        amt = max(txns[i].debit, txns[i].credit)
        if amt <= 0:
            continue
        delta = abs(txns[i].running_balance - txns[i - 1].running_balance)
        if abs(delta - amt) <= tol:
            hits += 1
    return hits


def _resolve_ambiguous_directions(
    txns: List[NormalizedTransaction],
    ambiguous_tx_ids: set[str],
    header_opening_bal: Optional[Decimal],
    unresolved: Optional[set[str]] = None,
) -> List[NormalizedTransaction]:
    """
    Deduce DEBIT vs CREDIT from running-balance deltas when a statement uses a single
    unsigned Amount column without explicit Dr/Cr indicators.
    Handles both chronological and reverse-chronological (including single-day) row orders.
    """
    if not txns or not ambiguous_tx_ids:
        return txns

    tol = Decimal("1.00")
    if len(txns) >= 2:
        fwd_hits = _count_unsigned_continuity(txns, tol)
        rev_seq = list(reversed(txns))
        rev_hits = _count_unsigned_continuity(rev_seq, tol)
        if txns[0].txn_date > txns[-1].txn_date or (rev_hits > fwd_hits):
            txns = rev_seq
            for idx, tx in enumerate(txns):
                tx.row_index = idx

    # Infer row 0 direction first if ambiguous
    first = txns[0]
    if first.tx_id in ambiguous_tx_ids:
        amt = max(first.debit, first.credit)
        inferred_dir: Optional[TxDirection] = None
        if header_opening_bal is not None and amt > 0:
            delta0 = first.running_balance - header_opening_bal
            if abs(delta0 - amt) <= tol:
                inferred_dir = TxDirection.CREDIT
            elif abs(delta0 + amt) <= tol:
                inferred_dir = TxDirection.DEBIT
        if inferred_dir is None:
            up = first.raw_narration.upper()
            if any(k in up for k in ("/CR/", "NEFT CR", "RTGS CR", "IMPS CR", "BY TRANSFER", "BY CLG", "CASH DEP", "BY CASH", "SALARY", "INTEREST CR", "REFUND")):
                inferred_dir = TxDirection.CREDIT
            elif any(k in up for k in ("/DR/", "NEFT DR", "RTGS DR", "IMPS DR", "TO TRANSFER", "ATM", "CASH WDL", "WITHDRAWAL", "CHARGES")):
                inferred_dir = TxDirection.DEBIT
            else:
                inferred_dir = TxDirection.DEBIT
                if unresolved is not None:
                    unresolved.add(first.tx_id)
        if inferred_dir == TxDirection.CREDIT:
            first.credit = amt
            first.debit = Decimal("0.00")
        else:
            first.debit = amt
            first.credit = Decimal("0.00")
        first.direction = inferred_dir

    for i in range(1, len(txns)):
        tx = txns[i]
        if tx.tx_id not in ambiguous_tx_ids:
            continue
        amt = max(tx.debit, tx.credit)
        if amt <= 0:
            continue
        delta = tx.running_balance - txns[i - 1].running_balance
        if abs(delta - amt) <= tol:
            tx.credit = amt
            tx.debit = Decimal("0.00")
            tx.direction = TxDirection.CREDIT
        elif abs(delta + amt) <= tol:
            tx.debit = amt
            tx.credit = Decimal("0.00")
            tx.direction = TxDirection.DEBIT
        elif unresolved is not None:
            unresolved.add(tx.tx_id)

    return txns


_NON_MONETARY_MEMO_RE = re.compile(
    r"\b(?:CR\s+RT\s+CHG|NEW\s+ACCT?\s*TYPE|RATE\s+CHANGE|INT(?:EREST)?\s+RATE\s+CHG|KYC\s+UPD(?:ATED)?|NOMINEE\s+REG(?:ISTERED)?|BROUGHT\s+FORWARD|CARRIED\s+FORWARD|OPENING\s+BAL(?:ANCE)?|CLOSING\s+BAL(?:ANCE)?)\b",
    re.IGNORECASE,
)

_OCR_EXTRA_FOOTER_RE = re.compile(
    r"(?:^\s*(?:uncl(?:eared)?\s*bal|clr\s*bal|\+?\s*mod\s*bal|cumulative\s*totals?|ope(?:ning)?\s*bal|clo(?:sing)?\s*bal|dr\s*count|cr\s*count|statement\s*summary|please\s*examine|this\s+is\s+(?:a\s+)?(?:system|computer)\s+generated|does\s+not\s+require|end\s+of\s+statement|grand\s+total)\b|^\s*page\s*(?:no\.?|#|:)?\s*\d+(?:\s*(?:of|/)\s*\d+)?\s*$)",
    re.IGNORECASE,
)


def _is_footer_or_summary_line(joined: str) -> bool:
    if _FOOTER_LINE_RE.match(joined) or _OCR_EXTRA_FOOTER_RE.search(joined):
        return True
    stripped_tags = re.sub(r"(?:(?<=\d)|(?<=[\s.]))(?:CR|DR|INR|RS|NIL|NA)\b|\b(?:CR|DR|INR|RS|NIL|NA)\b", "", joined, flags=re.IGNORECASE)
    if not re.search(r"[A-Za-z]{2,}", stripped_tags) and re.search(r"\d+\.\d{2}", joined):
        # Pure numeric summary total line with zero descriptive narration text
        return True
    return False


def _ocr_digit_distance(expected: Decimal, observed: Decimal, allow_truncated_decimal: bool = False) -> Optional[int]:
    """Return character edit/Hamming distance if `observed` is an OCR-plausible variant of `expected`, else None."""
    if observed == Decimal("0.00"):
        return 0
    if abs(expected - observed) <= Decimal("0.01"):
        return 0
    s1 = f"{expected:.2f}"
    s2 = f"{observed:.2f}"
    if abs(observed / Decimal("100") - expected) <= Decimal("0.01") or abs(expected / Decimal("100") - observed) <= Decimal("0.01"):
        return 1
    if len(s1) == len(s2):
        diffs = sum(1 for a, b in zip(s1, s2) if a != b)
        max_diffs = 1 if len(s1) <= 6 else 2
        if diffs <= max_diffs:
            return diffs
    if allow_truncated_decimal and observed % 1 == 0 and expected > 0 and observed > 0:
        si1 = str(int(expected))
        si2 = str(int(observed))
        if len(si1) == len(si2) and sum(1 for a, b in zip(si1, si2) if a != b) <= 1:
            return 1
    if abs(len(s1) - len(s2)) == 1:
        min_len = min(len(s1), len(s2))
        if min_len >= 4:
            tail_diffs = sum(1 for a, b in zip(s1[-min_len:], s2[-min_len:]) if a != b)
            allowed = 0 if min_len == 4 else 1
            if tail_diffs <= allowed:
                return 1 + tail_diffs
    return None


def _reconcile_ocr_balances_and_amounts(
    txns: List[NormalizedTransaction],
    header_opening_bal: Optional[Decimal] = None,
    footer_closing_bal: Optional[Decimal] = None,
) -> List[NormalizedTransaction]:
    """
    Reconcile OCR digit substitutions (`736->436`, `70->20`, `20729->20700`, `10899.15->10899.75`,
    `23899.15->25899.75`, `12032.62->12031.67`) and column/direction drifts across running balances.
    """
    if not txns:
        return txns

    # Ensure chronological order if OCR rows arrived strictly newest-first
    if txns[0].txn_date > txns[-1].txn_date:
        txns.reverse()

    tol = Decimal("0.005")

    def _matches(prev_b: Decimal, tx: NormalizedTransaction) -> bool:
        return abs((prev_b + tx.credit - tx.debit) - tx.running_balance) <= tol

    # Precompute forward-verified prefix indices so backward anchor propagation never overwrites verified balances
    # Precompute forward-verified balances (both from opening balance and mutually exact consecutive links)
    locked = [False] * len(txns)
    if header_opening_bal is not None and _matches(header_opening_bal, txns[0]):
        locked[0] = True
    elif header_opening_bal is not None and txns[0].running_balance > 0:
        d0_amt = _ocr_digit_distance(abs(txns[0].running_balance - header_opening_bal), max(txns[0].debit, txns[0].credit))
        if d0_amt is not None and d0_amt <= 1 and len(txns) >= 2 and _matches(txns[0].running_balance, txns[1]):
            locked[0] = True
    for k in range(1, len(txns)):
        if txns[k - 1].running_balance > 0 and txns[k].running_balance > 0 and max(txns[k].debit, txns[k].credit) > 0 and _matches(txns[k - 1].running_balance, txns[k]):
            locked[k - 1] = True
            locked[k] = True
        elif locked[k - 1] and txns[k].running_balance > 0 and max(txns[k].debit, txns[k].credit) > 0:
            imp_k = txns[k].running_balance - txns[k - 1].running_balance
            d_a = _ocr_digit_distance(abs(imp_k), max(txns[k].debit, txns[k].credit))
            same_s = (txns[k].debit > 0 and imp_k < 0) or (txns[k].credit > 0 and imp_k > 0)
            if d_a is not None and d_a <= 1 and same_s and (k + 1 >= len(txns) or _matches(txns[k].running_balance, txns[k + 1])):
                locked[k] = True

    # 0. Backward anchor pass from exact footer closing balance (e.g. Clr Bal: 5497.50 Cr on passbook)
    if footer_closing_bal is not None and footer_closing_bal > 0 and footer_closing_bal % 1 != 0:
        cur_target = footer_closing_bal
        for k in range(len(txns) - 1, 0, -1):
            tk = txns[k]
            amt_k = max(tk.debit, tk.credit)
            if abs(cur_target - tk.running_balance) <= tol:
                if amt_k > 0:
                    cur_target = (cur_target - tk.credit + tk.debit).quantize(Decimal("0.01"))
                    continue
                break
            d_k = _ocr_digit_distance(cur_target, tk.running_balance)
            if d_k is not None and amt_k > 0:
                tk.running_balance = cur_target
                # Never propagate backwards over an already forward-verified balance at k-1; solve k's amount instead
                if locked[k - 1]:
                    implied_k = cur_target - txns[k - 1].running_balance
                    if abs(implied_k) > 0 and ((tk.debit > 0 and implied_k < 0) or (tk.credit > 0 and implied_k > 0)):
                        if implied_k < 0:
                            tk.debit, tk.credit, tk.direction = abs(implied_k), Decimal("0.00"), TxDirection.DEBIT
                        else:
                            tk.credit, tk.debit, tk.direction = abs(implied_k), Decimal("0.00"), TxDirection.CREDIT
                    break
                cur_target = (cur_target - tk.credit + tk.debit).quantize(Decimal("0.01"))
                continue
            # Also repair amount at k if previous running_balance is already verified against cur_target
            prev_obs = txns[k - 1].running_balance
            implied_k = cur_target - prev_obs
            if (
                prev_obs > 0
                and abs(implied_k) > 0
                and ((tk.debit > 0 and implied_k < 0) or (tk.credit > 0 and implied_k > 0))
                and (locked[k - 1] or _ocr_digit_distance(abs(implied_k), amt_k) is not None)
            ):
                if implied_k < 0:
                    tk.debit, tk.credit, tk.direction = abs(implied_k), Decimal("0.00"), TxDirection.DEBIT
                else:
                    tk.credit, tk.debit, tk.direction = abs(implied_k), Decimal("0.00"), TxDirection.CREDIT
                tk.running_balance = cur_target
            break

    # 1. Reconcile row 0 against explicit opening balance (BROUGHT FORWARD / Opening Balance) if present
    if header_opening_bal is not None:
        tx0 = txns[0]
        if not _matches(header_opening_bal, tx0):
            amt0 = max(tx0.debit, tx0.credit)
            if amt0 == 0 and tx0.running_balance != 0 and (len(txns) == 1 or _matches(tx0.running_balance, txns[1])):
                imp0 = tx0.running_balance - header_opening_bal
                if abs(imp0) > 0:
                    if imp0 >= 0:
                        tx0.credit, tx0.debit, tx0.direction = abs(imp0), Decimal("0.00"), TxDirection.CREDIT
                    else:
                        tx0.debit, tx0.credit, tx0.direction = abs(imp0), Decimal("0.00"), TxDirection.DEBIT
            elif tx0.debit > 0 and tx0.credit == 0 and abs((header_opening_bal + amt0) - tx0.running_balance) <= tol:
                tx0.credit, tx0.debit, tx0.direction = amt0, Decimal("0.00"), TxDirection.CREDIT
            elif tx0.credit > 0 and tx0.debit == 0 and abs((header_opening_bal - amt0) - tx0.running_balance) <= tol:
                tx0.debit, tx0.credit, tx0.direction = amt0, Decimal("0.00"), TxDirection.DEBIT
            elif tx0.running_balance != 0 and (len(txns) == 1 or _matches(tx0.running_balance, txns[1])):
                imp0 = tx0.running_balance - header_opening_bal
                if _ocr_digit_distance(abs(imp0), amt0) is not None:
                    if imp0 >= 0:
                        tx0.credit, tx0.debit, tx0.direction = abs(imp0), Decimal("0.00"), TxDirection.CREDIT
                    else:
                        tx0.debit, tx0.credit, tx0.direction = abs(imp0), Decimal("0.00"), TxDirection.DEBIT
            elif amt0 > 0:
                cand0 = header_opening_bal + tx0.credit - tx0.debit
                if cand0 > 0 and (
                    (_ocr_digit_distance(cand0, tx0.running_balance) is not None and (len(txns) == 1 or _matches(cand0, txns[1])))
                    or (len(txns) >= 2 and _matches(cand0, txns[1]))
                ):
                    tx0.running_balance = cand0
    elif len(txns) >= 2 and txns[0].running_balance != 0 and not _matches(txns[0].running_balance, txns[1]):
        amt1 = max(txns[1].debit, txns[1].credit)
        imp1 = txns[1].running_balance - txns[0].running_balance
        same_s1 = (txns[1].debit > 0 and imp1 < 0) or (txns[1].credit > 0 and imp1 > 0)
        d_amt1 = _ocr_digit_distance(abs(imp1), amt1) if (amt1 > 0 and same_s1) else None
        if d_amt1 is None or d_amt1 > 1:
            back_bal0 = txns[1].running_balance + txns[1].debit - txns[1].credit
            if back_bal0 > 0 and (len(txns) == 2 or _matches(txns[1].running_balance, txns[2])) and _ocr_digit_distance(back_bal0, txns[0].running_balance) is not None:
                txns[0].running_balance = back_bal0

    # 2. Repair single-row Debit/Credit column swaps where magnitude matches exact balance delta
    for i in range(1, len(txns)):
        prev_b = txns[i - 1].running_balance
        tx = txns[i]
        if prev_b != 0 and not _matches(prev_b, tx):
            amt = max(tx.debit, tx.credit)
            if amt > 0:
                if tx.debit > 0 and tx.credit == 0 and abs((prev_b + amt) - tx.running_balance) <= tol:
                    tx.credit, tx.debit, tx.direction = amt, Decimal("0.00"), TxDirection.CREDIT
                elif tx.credit > 0 and tx.debit == 0 and abs((prev_b - amt) - tx.running_balance) <= tol:
                    tx.debit, tx.credit, tx.direction = amt, Decimal("0.00"), TxDirection.DEBIT

    # 3. Lookahead-scored OCR amount vs running balance continuity repair
    def _chain_score(start_bal: Decimal, start_idx: int) -> float:
        score = 0.0
        cur_b = start_bal
        for k in range(start_idx, len(txns)):
            tk = txns[k]
            amt_k = max(tk.debit, tk.credit)
            exp_b = cur_b + tk.credit - tk.debit
            if abs(exp_b - tk.running_balance) <= tol:
                score += 3.0
                cur_b = tk.running_balance
                continue
            d_bal = _ocr_digit_distance(exp_b, tk.running_balance, allow_truncated_decimal=(k == len(txns) - 1)) if amt_k > 0 else None
            imp_k = tk.running_balance - cur_b
            d_amt = _ocr_digit_distance(abs(imp_k), amt_k) if tk.running_balance != 0 else None
            # Allow bridged 2-digit fix on amount (e.g. `20729 -> 20700`) or balance (`23899.15 -> 25899.75`)
            if d_bal is not None and d_amt is None:
                score += max(0.5, 2.0 - d_bal * 0.5)
                cur_b = exp_b
            elif d_amt is not None and d_bal is None:
                score += max(0.5, 2.0 - d_amt * 0.5)
                cur_b = tk.running_balance
            elif d_amt is not None and d_bal is not None:
                # Prefer keeping observed balance if it reaches footer closing balance or exact next step
                score += 1.5
                cur_b = tk.running_balance
            else:
                return score - 4.0
        if footer_closing_bal is not None and (
            abs(cur_b - footer_closing_bal) <= tol
            or (footer_closing_bal % 1 == 0 and int(cur_b) == int(footer_closing_bal))
        ):
            score += 10.0
        return score

    for i in range(1, len(txns)):
        prev_b = txns[i - 1].running_balance
        tx = txns[i]
        if prev_b == 0 and tx.running_balance == 0:
            continue
        if _matches(prev_b, tx):
            continue

        obs_amt = max(tx.debit, tx.credit)
        implied_amt = tx.running_balance - prev_b
        expected_bal = prev_b + tx.credit - tx.debit

        # Candidate A: fix amount at row i so `prev_b + new_amt == tx.running_balance`
        d_amt = _ocr_digit_distance(abs(implied_amt), obs_amt) if tx.running_balance != 0 else None
        # Also allow 2-digit amount repair on 5-6 char numbers ONLY when next balance or footer proves tx.running_balance
        if d_amt is None and tx.running_balance != 0 and obs_amt > 0:
            s_exp, s_obs = f"{abs(implied_amt):.2f}", f"{obs_amt:.2f}"
            if (len(s_exp) == len(s_obs) and sum(a != b for a, b in zip(s_exp, s_obs)) <= 2) or (
                abs(tx.running_balance - expected_bal) > tol
                and len(s_exp) == len(s_obs)
                and s_exp.endswith(".00")
                and s_obs.endswith(".00")
                and len(s_exp) <= 5
            ):
                if (i == len(txns) - 1 and footer_closing_bal is not None and abs(tx.running_balance - footer_closing_bal) <= tol) or (
                    i + 1 < len(txns) and (_matches(tx.running_balance, txns[i + 1]) or _ocr_digit_distance(tx.running_balance + txns[i + 1].credit - txns[i + 1].debit, txns[i + 1].running_balance) is not None)
                ):
                    d_amt = 2

        # Candidate B: fix running_balance at row i so `expected_bal` becomes `tx.running_balance`
        d_bal = _ocr_digit_distance(expected_bal, tx.running_balance, allow_truncated_decimal=(i == len(txns) - 1)) if obs_amt > 0 else None
        two_neighbor_bridge = False
        if d_bal is None and obs_amt > 0 and expected_bal > 0 and i + 1 < len(txns):
            nxt_amt = max(txns[i + 1].debit, txns[i + 1].credit)
            if nxt_amt > 0 and _matches(expected_bal, txns[i + 1]):
                d_bal = 2
                two_neighbor_bridge = True

        score_a = (_chain_score(tx.running_balance, i + 1) + 2.0 - 0.3 * d_amt) if d_amt is not None else -999.0
        score_b = (_chain_score(expected_bal, i + 1) + 2.0 - 0.3 * d_bal + (6.0 if two_neighbor_bridge else 0.0)) if d_bal is not None else -999.0

        # Last-row tie-breaker against explicit footer closing balance
        if i == len(txns) - 1 and footer_closing_bal is not None:
            if d_amt is not None and abs(tx.running_balance - footer_closing_bal) <= tol:
                score_a += 10.0
            if d_bal is not None and (
                abs(expected_bal - footer_closing_bal) <= tol
                or (footer_closing_bal % 1 == 0 and int(expected_bal) == int(footer_closing_bal))
            ):
                score_b += 10.0

        # Candidate C: simultaneous (amount, balance) OCR repair when i+1 anchors the true balance
        best_c_pair: Optional[Tuple[Decimal, Decimal, float]] = None
        if max(score_a, score_b) <= 0 and obs_amt > 0 and i + 1 < len(txns):
            nxt = txns[i + 1]
            nxt_amt = max(nxt.debit, nxt.credit)
            if nxt.running_balance > 0 and nxt_amt > 0:
                nxt_bal_cands = [nxt.running_balance]
                if nxt_amt % 1 == 0 and prev_b % 1 != 0:
                    paise = prev_b - int(prev_b)
                    nxt_bal_cands.append((Decimal(int(nxt.running_balance)) + paise).quantize(Decimal("0.01")))
                for nb in nxt_bal_cands:
                    cand_bal = (nb - nxt.credit + nxt.debit).quantize(Decimal("0.01"))
                    if cand_bal <= 0:
                        continue
                    dc_bal = _ocr_digit_distance(cand_bal, tx.running_balance)
                    if dc_bal is None:
                        continue
                    cand_imp = (cand_bal - prev_b).quantize(Decimal("0.01"))
                    same_sign = (tx.debit > 0 and cand_imp < 0) or (tx.credit > 0 and cand_imp > 0)
                    if not same_sign:
                        continue
                    dc_amt = _ocr_digit_distance(abs(cand_imp), obs_amt)
                    if dc_amt is None:
                        continue
                    sc_c = _chain_score(cand_bal, i + 1) + 2.0 - 0.3 * (dc_bal + dc_amt)
                    if sc_c > 0 and (best_c_pair is None or sc_c > best_c_pair[2]):
                        best_c_pair = (cand_imp, cand_bal, sc_c)

        if best_c_pair is not None and best_c_pair[2] > max(score_a, score_b):
            c_imp, c_bal, _ = best_c_pair
            if c_imp >= 0:
                tx.credit, tx.debit, tx.direction = abs(c_imp), Decimal("0.00"), TxDirection.CREDIT
            else:
                tx.debit, tx.credit, tx.direction = abs(c_imp), Decimal("0.00"), TxDirection.DEBIT
            tx.running_balance = c_bal
        elif score_a > score_b and d_amt is not None and (score_a > 0 or i == len(txns) - 1):
            if implied_amt >= 0:
                tx.credit = abs(implied_amt)
                tx.debit = Decimal("0.00")
                tx.direction = TxDirection.CREDIT
            else:
                tx.debit = abs(implied_amt)
                tx.credit = Decimal("0.00")
                tx.direction = TxDirection.DEBIT
        elif score_b >= score_a and d_bal is not None and (
            score_b > 0
            or i == len(txns) - 1
            or (d_amt is None and d_bal <= 1 and abs(expected_bal - tx.running_balance) <= Decimal("0.10"))
        ):
            tx.running_balance = expected_bal

    for idx, tx in enumerate(txns):
        tx.row_index = idx
    return txns


def _money_str(v: Decimal) -> str:
    return f"{v:.2f}"


_HEADER_PAN_RE = re.compile(r"\bPAN\b(?:\s*(?:NO|NUMBER)\.?)?\s*[:\-]?\s*([A-Z]{3}[ABCFGHJLPT][A-Z][0-9]{4}[A-Z])\b")


def _header_pan(header_text: Optional[str]) -> Optional[str]:
    """Account holder PAN when the statement header carries an explicitly labelled one (NLP-09)."""
    m = _HEADER_PAN_RE.search((header_text or "").upper())
    return m.group(1) if m else None


def parse_statement_bytes(
    filename: str,
    file_bytes: bytes,
    case_id: str,
    password: Optional[str] = None,
    draw_template: Optional[DrawTemplate] = None,
) -> Tuple[BankAccountStatement, List[NormalizedTransaction], Dict[str, Any]]:
    """
    Ingest a single PDF, Image, XLSX/XLS, CSV, or TXT bank statement and normalize all transaction rows.
    Applies OCR running-balance and amount continuity self-healing when the source is OCR-derived; every
    self-healed row is flagged OCR_AUTOCORRECTED + LOW_OCR_CONFIDENCE and listed in meta["ocr_corrections"].
    """
    anchors: Dict[str, Optional[Decimal]] = {}
    stmt, txns, meta = _parse_statement_bytes_core(
        filename=filename,
        file_bytes=file_bytes,
        case_id=case_id,
        password=password,
        draw_template=draw_template,
        _out_anchors=anchors,
    )
    if stmt.is_ocr_derived and txns:
        before = {t.tx_id: (t.debit, t.credit, t.running_balance, t.direction) for t in txns}
        txns = _reconcile_ocr_balances_and_amounts(
            txns,
            header_opening_bal=anchors.get("opening"),
            footer_closing_bal=anchors.get("closing"),
        )
        corrections: List[Dict[str, Any]] = []
        for t in txns:
            prev = before.get(t.tx_id)
            if prev is None or prev == (t.debit, t.credit, t.running_balance, t.direction):
                continue
            for flag in (AnomalyFlag.OCR_AUTOCORRECTED, AnomalyFlag.LOW_OCR_CONFIDENCE):
                if flag not in t.anomaly_flags:
                    t.anomaly_flags.append(flag)
            corrections.append(
                {
                    "tx_id": t.tx_id,
                    "row_index": t.row_index,
                    "before": {"debit": _money_str(prev[0]), "credit": _money_str(prev[1]), "running_balance": _money_str(prev[2])},
                    "after": {"debit": _money_str(t.debit), "credit": _money_str(t.credit), "running_balance": _money_str(t.running_balance)},
                }
            )
        meta["ocr_corrections"] = corrections
        if corrections:
            meta["warnings"].append(
                f"OCR self-healing changed {len(corrections)} row(s); originals are in ocr_corrections and the rows are flagged OCR_AUTOCORRECTED."
            )
        stmt.closing_balance = txns[-1].running_balance
        stmt.opening_balance = (
            anchors["opening"]
            if anchors.get("opening") is not None
            else (txns[0].running_balance + txns[0].debit - txns[0].credit)
        )
        stmt.period_start = min(t.txn_date for t in txns)
        stmt.period_end = max(t.txn_date for t in txns)

        from cyphersol.ingestion.anomaly_gate import run_anomaly_gate

        stmt, txns, gate_summary = run_anomaly_gate(stmt, txns)
        meta["gate_summary"] = gate_summary
    return stmt, txns, meta


_TOTAL_ROW_RE = re.compile(r"^\s*(?:grand\s+|sub[\s-]*)?totals?\b", re.IGNORECASE)
# Summary/carry-forward tokens anywhere in an undated line (ING-01): "Page Total", "Transaction Total", "Balance B/F"
_CARRY_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:b\s*/\s*f|c\s*/\s*f|b/fwd|c/fwd|brought\s+(?:forward|fwd|fw)|carried\s+(?:forward|fwd|fw)|"
    r"balance\s+forward|opening\s+balance|closing\s+balance)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_TOTAL_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])(?:grand\s+|sub[\s-]*|page\s+)?totals?(?![A-Za-z0-9])", re.IGNORECASE)
_RAIL_TOKEN_RE = re.compile(r"\b(?:UPI|NEFT|RTGS|IMPS|POS|ATM|ACH|NACH|ECS|CHQ|CLG|TRF|INB|MB)\b", re.IGNORECASE)


def _is_undated_summary_line(joined: str) -> bool:
    """Undated total / page-total / carry-forward line that must never become a transaction (ING-01)."""
    if _CARRY_TOKEN_RE.search(joined):
        return True
    return bool(_TOTAL_TOKEN_RE.search(joined)) and not _RAIL_TOKEN_RE.search(joined)
_STRICT_MONEY_RE = re.compile(
    r"^\(?-?\s*(?:Rs\.?|INR|₹)?\s*\d[\d,]*\.\d{2}\)?-?(?:\s*(?:Cr|Dr)\.?)?$", re.IGNORECASE
)
_SIGNED_AMOUNT_RE = re.compile(r"^\s*(?:-\s*(?:Rs\.?|INR|₹)?\s*\d|\(\s*(?:Rs\.?|INR|₹)?\s*\d)|\d\s*-\s*$", re.IGNORECASE)
_CREDIT_TYPE_TOKENS = frozenset({"CR", "C", "CREDIT", "CREDITED", "DEPOSIT", "DEPOSITS", "DEP", "RECEIPT", "RECEIVED"})
_DEBIT_TYPE_TOKENS = frozenset({"DR", "D", "DEBIT", "DEBITED", "WITHDRAWAL", "WITHDRAWALS", "WITHDRAW", "WDL", "PAYMENT", "PAID"})
_MONEY_FIELDS = ("debit", "credit", "amount", "balance")
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_SPREADSHEET_EXTENSIONS = {".xlsx", ".xlsm", ".xls"}


def _cell_text(row: Sequence[Any], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row) or row[idx] is None:
        return ""
    return str(row[idx]).strip()


def _direction_from_type(raw_type: str) -> Optional[str]:
    """Map a Dr/Cr or transaction-type cell (DEBIT/WITHDRAWAL/DEPOSIT/CREDIT/DR/CR...) to 'DR'/'CR'."""
    up = raw_type.strip().upper()
    if not up:
        return None
    toks = set(re.findall(r"[A-Z]+", up))
    is_cr = bool(toks & _CREDIT_TYPE_TOKENS)
    is_dr = bool(toks & _DEBIT_TYPE_TOKENS)
    if is_cr != is_dr:
        return "CR" if is_cr else "DR"
    if not is_cr and not is_dr:
        if "CR" in up and "DR" not in up:
            return "CR"
        if "DR" in up and "CR" not in up:
            return "DR"
    return None


def _row_has_money(row: Sequence[Any], col_map: Dict[str, int], ocr: bool) -> bool:
    for field_name in _MONEY_FIELDS:
        txt = _cell_text(row, col_map.get(field_name))
        if txt and parse_indian_amount(txt, ocr=ocr)[0] > 0:
            return True
    return False


def _realign_pdf_row(row: Sequence[Any], col_map: Dict[str, int]) -> Tuple[List[Any], bool]:
    """
    PDF text extraction drops blank cells, shifting amounts left (ING-04). When the balance column is empty but
    the row ends in a run of 2-3 money cells, right-align them: last = balance; with 2 cells the remaining one is
    a single amount whose direction must be inferred from the balance delta (returned flag True).
    """
    if not all(f in col_map for f in ("debit", "credit", "balance")):
        return list(row), False
    cells = [str(c).strip() if c is not None else "" for c in row]
    bal_i = col_map["balance"]
    if bal_i < len(cells) and _STRICT_MONEY_RE.match(cells[bal_i]):
        return list(row), False
    end = len(cells)
    while end > 0 and not cells[end - 1]:
        end -= 1
    start = end
    while start > 0 and _STRICT_MONEY_RE.match(cells[start - 1]):
        start -= 1
    money = cells[start:end]
    if len(money) not in (2, 3):
        return list(row), False
    width = max(col_map.values()) + 1
    first_money_col = min(col_map["debit"], col_map["credit"], bal_i)
    narr_i = col_map.get("narration")
    new: List[Any] = [""] * width
    for i, val in enumerate(cells[:start]):
        if i < first_money_col:
            new[i] = val
        elif narr_i is not None and val:
            new[narr_i] = f"{new[narr_i]} {val}".strip()
    new[bal_i] = money[-1]
    if len(money) == 3:
        new[col_map["debit"]], new[col_map["credit"]] = money[0], money[1]
        return new, False
    new[col_map["debit"]] = money[0]
    return new, True


def _is_order_ambiguous(raw: Any) -> bool:
    parts = _numeric_date_parts(raw)
    return bool(parts and parts[0] != parts[1] and parts[0] <= 12 and parts[1] <= 12)


def _read_spreadsheet(file_bytes: bytes, ext: str, filename: str) -> Dict[str, List[List[Any]]]:
    """Read .xlsx/.xlsm/.xls by content: OOXML zip, legacy BIFF (xlrd), HTML tables or text saved as .xls."""
    if file_bytes[:4] == b"PK\x03\x04":
        return read_xlsx_bytes(file_bytes)
    if file_bytes[:8] == _OLE2_MAGIC:
        return read_xls_bytes(file_bytes)
    if ext in {".xlsx", ".xlsm"}:
        raise ValueError(f"'{filename}' is not a valid Excel workbook (no OOXML zip container).")
    text, _encoding = decode_statement_text(file_bytes)
    if re.search(r"<\s*table\b", text[:1_000_000], re.IGNORECASE):
        return {"Sheet1": read_html_table_rows(text)}
    return {"Sheet1": _decode_delimited_rows(file_bytes, ".txt")}


def _row_line(r: Sequence[Any]) -> str:
    return " ".join(str(c if c is not None else "") for c in r)


def _select_sheet_blocks(
    sheets: Dict[str, List[List[Any]]], filename: str
) -> Tuple[List[Tuple[Optional[str], List[List[Any]]]], str, List[str]]:
    """Every worksheet with a transaction header is parsed (ING-05); other non-empty sheets are reported."""
    blocks: List[Tuple[Optional[str], List[List[Any]]]] = []
    skipped: List[Tuple[str, List[List[Any]]]] = []
    for name, rows in sheets.items():
        if not any(str(c).strip() for r in rows for c in r if c is not None):
            continue
        if _scan_header_row(rows)[0] >= 0:
            blocks.append((name, rows))
        else:
            skipped.append((name, rows))
    if not blocks:
        if not skipped:
            raise ValueError(f"Excel workbook '{filename}' has no readable sheets.")
        name, rows = skipped.pop(0)
        blocks.append((name, rows))
    warnings = [f"Worksheet '{name}' skipped: no transaction table header found." for name, _ in skipped]
    header_lines = [_row_line(r) for _name, rows in skipped[:3] for r in rows[:12]]
    header_lines += [_row_line(r) for r in blocks[0][1][:12]]
    return blocks, "\n".join(header_lines), warnings


_STATED_CLOSING_RE = re.compile(
    r"(?:closing\s+bal(?:ance)?|clo(?:sing)?\.?\s*bal\.?|clr\s*bal|balance\s+c/f|c/f\s+balance|carried\s+forward)\b",
    re.IGNORECASE,
)
_AMOUNT_AFTER_LABEL_RE = re.compile(r"(?<![\d/.-])-?(?:Rs\.?\s*|INR\s*|₹\s*)?\d[\d,]*(?:\.\d{1,2})?(?:\s*(?:Cr|Dr)\b)?", re.IGNORECASE)


def _table_body_lines(rows: Sequence[Sequence[Any]], header_idx: int, date_col: int) -> List[str]:
    """Row lines from the first to the last dated row after the table header (empty when nothing is dated)."""
    start = header_idx + 1 if header_idx >= 0 else 0

    def dated(r: Sequence[Any]) -> bool:
        return parse_indian_date(_cell_text(r, date_col)) is not None

    first = next((i for i in range(start, len(rows)) if dated(rows[i])), None)
    if first is None:
        return []
    last = next(i for i in range(len(rows) - 1, first - 1, -1) if dated(rows[i]))
    return [_row_line(r) for r in rows[first + 1 : last]]


def _norm_line(ln: str) -> str:
    return re.sub(r"\s+", " ", ln).strip().lower()


def _starts_with_date(ln: str) -> bool:
    tok = ln.strip().split(" ", 1)[0] if ln.strip() else ""
    return bool(tok) and parse_indian_date(tok) is not None


def _looks_like_table_header(ln: str) -> bool:
    """A column-header line: no digits and at least a date column plus one other transaction-table column."""
    if re.search(r"\d", ln):
        return False
    low = _norm_line(ln)
    if not re.search(r"(?<![a-z])(?:date|dt)(?![a-z])", low):
        return False
    others = ("narration", "particulars", "description", "remarks", "details", "debit", "credit", "withdrawal",
              "deposit", "amount", "chq", "cheque", "ref")
    return any(re.search(rf"(?<![a-z]){o}", low) for o in others)


def _extract_stated_closing_balance(
    header_text: str,
    raw_rows: Sequence[Sequence[Any]],
    exclude_lines: Optional[Sequence[str]] = None,
) -> Optional[Decimal]:
    """Closing balance printed in the header preamble or the statement footer (ING-16).

    Transaction-table header rows (a "Closing Balance" / "Clo Bal" column title) are never a stated value, and
    the next-line fallback never reads a dated transaction row or another header row.
    """
    lines = header_text.splitlines() + [_row_line(r) for r in raw_rows[max(0, len(raw_rows) - 18) :]]
    excluded = {_norm_line(x) for x in (exclude_lines or ()) if x and x.strip()}

    def is_header(ln: str) -> bool:
        return _norm_line(ln) in excluded or _looks_like_table_header(ln)

    for i, ln in enumerate(lines):
        m = _STATED_CLOSING_RE.search(ln)
        if not m or is_header(ln):
            continue
        rest = ln[m.end() :]
        rest = re.sub(r"^\s*(?:as\s+on\s+)?\d{1,2}[./-]\w{2,3}[./-]\d{2,4}", "", rest, flags=re.IGNORECASE)
        cands = [c for c in _AMOUNT_AFTER_LABEL_RE.findall(rest) if re.search(r"\d", c)]
        if (
            not cands
            and i + 1 < len(lines)
            and not _STATED_CLOSING_RE.search(lines[i + 1])
            and not _starts_with_date(lines[i + 1])
            and not is_header(lines[i + 1])
        ):
            nxt = [c for c in _AMOUNT_AFTER_LABEL_RE.findall(lines[i + 1]) if "." in c]
            cands = nxt[-1:]
        if cands:
            val, hint = parse_indian_amount(cands[0], ocr=False)
            if val > 0 or cands[0].strip("₹RsINR. ") in {"0", "0.00"}:
                return -val if hint == "DR" else val
    return None


def _parse_statement_bytes_core(
    filename: str,
    file_bytes: bytes,
    case_id: str,
    password: Optional[str] = None,
    draw_template: Optional[DrawTemplate] = None,
    _out_anchors: Optional[Dict[str, Optional[Decimal]]] = None,
) -> Tuple[BankAccountStatement, List[NormalizedTransaction], Dict[str, Any]]:
    """
    Core statement ingestion routine returning (BankAccountStatement, List[NormalizedTransaction], ingestion_metadata).
    """
    sha256_hash = compute_sha256(file_bytes)
    ext = os.path.splitext(filename)[1].lower()
    header_text = ""
    is_ocr = False
    warnings: List[str] = []
    blocks: List[Tuple[Optional[str], List[List[Any]]]] = []
    pdf_meta: Dict[str, Any] = {}

    if ext == ".pdf":
        # ``password`` may be a plain string or a list of candidates ([(label, password)] from
        # pdf_parser.generate_password_candidates, optionally led by ("supplied", pw)); memory only (PDF-13).
        pdf_password: Optional[str] = None
        pdf_candidates: Optional[List[Any]] = None
        if isinstance(password, (list, tuple)):
            pdf_candidates = list(password)
        else:
            pdf_password = password
        pdf_res = extract_pdf_data(
            file_bytes,
            password=pdf_password,
            column_splits=draw_template.column_splits if draw_template else None,
            bbox=draw_template.table_bbox if draw_template else None,
            password_candidates=pdf_candidates,
        )
        pdf_error = getattr(pdf_res, "error", None)
        if pdf_error == "invalid_template":
            raise ValueError(f"Invalid Draw Tool template: {'; '.join(getattr(pdf_res, 'warnings', None) or [])}")
        if pdf_error == "wrong_password":
            raise ValueError(f"Incorrect password for PDF '{filename}'.")
        if pdf_error == "decryption_unavailable":
            raise ValueError("Encrypted PDF could not be decrypted: install pikepdf or Poppler/qpdf.")
        if pdf_error == "corrupt":
            raise ValueError(f"PDF '{filename}' is damaged or not a PDF.")
        if pdf_res.is_encrypted:
            raise ValueError(f"PDF '{filename}' is password-protected; provide decryption password.")
        warnings.extend(getattr(pdf_res, "warnings", None) or [])
        pdf_meta["pdf_text_source"] = getattr(pdf_res, "text_source", None)
        # PDF-11 confidence gate / PDF-13 password formula label (never the password itself).
        conf_by_page = dict(getattr(pdf_res, "ocr_confidence_by_page", None) or {})
        if conf_by_page:
            pdf_meta["ocr_confidence_by_page"] = {str(k): v for k, v in conf_by_page.items()}
        low_pages = list(getattr(pdf_res, "low_confidence_pages", None) or [])
        if low_pages:
            pdf_meta["low_confidence_pages"] = low_pages
            pdf_meta["low_ocr_confidence"] = True
        rejected_pages = dict(getattr(pdf_res, "ocr_rejected_pages", None) or {})
        if rejected_pages:
            pdf_meta["ocr_rejected_pages"] = {str(k): v for k, v in rejected_pages.items()}
        if getattr(pdf_res, "password_label", None):
            pdf_meta["password_label"] = pdf_res.password_label
        if pdf_res.needs_ocr:
            is_ocr = True
            eng_label = pdf_res.ocr_engine or "offline_ocr"
            ocr_pages = list(getattr(pdf_res, "ocr_pages", None) or [])
            pdf_meta["ocr_pages"] = ocr_pages
            where = f"Scanned page(s) {ocr_pages}" if ocr_pages else "Scanned PDF"
            warnings.append(f"{where} processed via offline OCR engine ({eng_label}).")
        raw_rows = pdf_res.rows
        row_lines = [
            " ".join(str(c or "").strip() for c in r if str(c or "").strip())
            for r in raw_rows[:35]
        ]
        header_text = "\n".join([ln for ln in row_lines if ln] + list(pdf_res.pages_text[:1]))
        blocks = [(None, raw_rows)]
    elif ext in _IMAGE_EXTENSIONS:
        ocr_res = ocr_image_bytes(file_bytes)
        is_ocr = True
        if ocr_res.get("engine") == "unavailable":
            # Reported, not raised: an empty statement keeps the retry/supersede semantics of BE-12.
            warnings.append("No OCR engine available; install Tesseract to read image statements.")
            pdf_meta["ocr_unavailable"] = True
        else:
            warnings.append(f"Image statement processed via offline OCR engine ({ocr_res['engine']}).")
        if ocr_res.get("confidence") is not None:
            pdf_meta["ocr_confidence_by_page"] = {"1": ocr_res.get("confidence")}
        if ocr_res.get("low_confidence"):
            pdf_meta["low_confidence_pages"] = [1]
            pdf_meta["low_ocr_confidence"] = True
        if ocr_res.get("rejected"):
            pdf_meta["ocr_rejected_pages"] = {"1": ocr_res.get("rejected")}
        ocr_tokens = [
            PdfTextToken(page=t.page, x=t.x, y=t.y, text=t.text, x_end=t.x_end)
            for t in ocr_res["tokens"]
        ]
        raw_rows = _group_tokens_into_rows(
            ocr_tokens,
            y_tolerance=4.5,
            column_splits=draw_template.column_splits if draw_template else None,
            bbox=draw_template.table_bbox if draw_template else None,
            page_sizes=[(595.0, 842.0)],
        )
        header_text = "\n".join(ocr_res["pages_text"])
        blocks = [(None, raw_rows)]
    elif ext in _SPREADSHEET_EXTENSIONS:
        sheets = _read_spreadsheet(file_bytes, ext, filename)
        blocks, header_text, sheet_warnings = _select_sheet_blocks(sheets, filename)
        warnings.extend(sheet_warnings)
    elif ext in {".csv", ".tsv", ".txt"}:
        raw_rows = _decode_delimited_rows(file_bytes, ext, warnings=warnings)
        header_text = "\n".join(_row_line(r) for r in raw_rows[:12])
        blocks = [(None, raw_rows)]
    else:
        raise ValueError(
            f"Unsupported statement format '{ext}'. Supported: .pdf, image scans (.png/.jpg/.bmp/.pgm), "
            ".xlsx, .xls, .csv, .tsv, .txt"
        )

    bank_profile, meta = detect_bank_and_metadata(header_text, filename)
    warnings.extend(meta.pop("detection_warnings", None) or [])  # type: ignore[arg-type]
    filename_crosscheck: Dict[str, Any] = dict(meta.pop("filename_crosscheck", None) or {})  # type: ignore[arg-type]
    is_pdf_like = ext == ".pdf" or ext in _IMAGE_EXTENSIONS
    use_template_cols = bool(draw_template and draw_template.column_names)

    mapped: List[Tuple[Optional[str], List[List[Any]], int, Dict[str, int]]] = []
    for sheet_name, rows in blocks:
        if use_template_cols:
            assert draw_template is not None
            mapped.append((sheet_name, rows, -1, {name: idx for idx, name in enumerate(draw_template.column_names) if name}))
        else:
            h_idx, c_map = _map_header_columns(rows)
            mapped.append((sheet_name, rows, h_idx, c_map))
    _first_name, first_rows, first_header_idx, first_col_map = mapped[0]

    # Lines inside the transaction table (header row and the body between the first and last dated rows) are
    # never statement-level stated balances; only the preamble and the footer are (ING-16).
    table_lines: List[str] = []
    for _sn, rows, h, c_map in mapped:
        if 0 <= h < len(rows):
            table_lines.append(_row_line(rows[h]))
        table_lines.extend(_table_body_lines(rows, h, c_map.get("date", 0)))
    header_opening_bal = _extract_header_opening_balance(header_text, first_rows, first_header_idx, exclude_lines=table_lines)
    stated_closing_bal = _extract_stated_closing_balance(header_text, mapped[-1][1], exclude_lines=table_lines)

    m_period = re.search(
        r"(?:from|period)\s*[:\-]?\s*([0-9./:-]{8,10})\s+to\s*[:\-]?\s*([0-9./:-]{8,10})",
        header_text,
        flags=re.IGNORECASE,
    )
    period_raw: Tuple[str, ...] = (m_period.group(1), m_period.group(2)) if m_period else ()

    # Statement-level DD/MM vs MM/DD decision (ING-02), made once from every date cell plus the stated period
    raw_date_cells: List[Any] = []
    for _sn, rows, h_idx, c_map in mapped:
        d_col = c_map.get("date", 0)
        for r in rows[h_idx + 1 :] if h_idx >= 0 else rows:
            if d_col < len(r) and r[d_col] not in (None, ""):
                raw_date_cells.append(r[d_col])
    date_order, order_decided, order_basis = infer_date_order(raw_date_cells, period=period_raw)
    if date_order == DATE_ORDER_MDY:
        warnings.append(f"Numeric dates read as MM/DD (month first); basis: {order_basis}.")

    stmt_period_start: Optional[str] = None
    stmt_period_end: Optional[str] = None
    if period_raw:
        stmt_period_start = parse_indian_date(period_raw[0], order=date_order, ocr=is_ocr)
        stmt_period_end = parse_indian_date(period_raw[1], order=date_order, ocr=is_ocr)

    statement_id = str(uuid.uuid4())
    parsed_txns: List[NormalizedTransaction] = []
    ambiguous_tx_ids: set[str] = set()
    row_flags: Dict[str, List[AnomalyFlag]] = {}
    undated_ids: List[str] = []
    undated_notes: List[str] = []
    source_sheet_by_tx: Dict[str, str] = {}
    sheet_summaries: List[Dict[str, Any]] = []
    dual_rows = 0
    skipped_summary_money_rows = 0
    ocr_dropped_money_rows = 0
    order_ambiguous_rows = 0
    if _out_anchors is not None:
        _out_anchors["opening"] = header_opening_bal
        _out_anchors["closing"] = _extract_footer_closing_balance(mapped[-1][1])

    def _flag(tx_id: str, flag: AnomalyFlag) -> None:
        lst = row_flags.setdefault(tx_id, [])
        if flag not in lst:
            lst.append(flag)

    for sheet_name, rows, header_idx, col_map in mapped:
        data_rows = rows[header_idx + 1 :] if header_idx >= 0 else rows
        row_offset = header_idx + 2 if header_idx >= 0 else 1  # 1-based source row number of data_rows[0]
        date_col = col_map.get("date", 0)
        narr_col = col_map.get("narration", 1)
        amt_col = col_map.get("amount")
        signed_amounts = amt_col is not None and any(_SIGNED_AMOUNT_RE.search(_cell_text(r, amt_col)) for r in data_rows)
        block_start = len(parsed_txns)
        last_txn: Optional[NormalizedTransaction] = None  # target for wrapped-narration merges
        last_dated: Optional[NormalizedTransaction] = None  # chronological context for OCR date repair
        pending_prefix_parts: List[str] = []
        pause_continuation = False

        for r_i, row in enumerate(data_rows):
            if not row or all(str(c if c is not None else "").strip() == "" for c in row):
                continue
            realigned_ambiguous = False
            if is_pdf_like and not use_template_cols:
                row, realigned_ambiguous = _realign_pdf_row(row, col_map)
            raw_date = _cell_text(row, date_col)
            extra_date_prefix = ""
            embedded_val_date: Optional[str] = None
            m_lead_date = re.match(
                r"^\s*([0-9FPTfpt7:OoQqIl|SBZ]{1,2}[./:-][0-9:OoQqIl|SBZ]{1,2}[./:-][0-9:OoQqIl|SBZ]{2,4}|[0-9]{2}[-/.][A-Za-z]{3}[-/.][0-9]{2,4}|(?:TT|II)[./-]\d{2}[./-]-?\d{2,4})\s+(.+)$",
                raw_date,
            )
            if m_lead_date and not re.match(r"^\d{1,2}:\d{2}", m_lead_date.group(2)):
                raw_date_tok, extra_date_prefix = m_lead_date.group(1), m_lead_date.group(2).strip()
                m_second_date = re.match(
                    r"^([0-9FPTfpt7:OoQqIl|SBZ]{1,2}[./:-][0-9:OoQqIl|SBZ]{1,2}[./:-][0-9:OoQqIl|SBZ]{2,4}|[0-9]{2}[-/.][A-Za-z]{3}[-/.][0-9]{2,4})(?:\s+(.*))?$",
                    extra_date_prefix,
                )
                if m_second_date:
                    second_iso = parse_indian_date(m_second_date.group(1), order=date_order, ocr=is_ocr)
                    if second_iso:
                        embedded_val_date = second_iso
                        extra_date_prefix = (m_second_date.group(2) or "").strip()
            else:
                raw_date_tok = raw_date
            iso_date = parse_indian_date(raw_date_tok, order=date_order, ocr=is_ocr)
            ocr_low_conf = False

            # If invalid OCR date (e.g. `34-01-2025`, `52-10-2025`) cleaned to out-of-order or post-period month, repair month
            if (
                iso_date is not None
                and is_ocr
                and last_dated is not None
                and _parse_date_basic(raw_date_tok, date_order) is None
                and (
                    iso_date < last_dated.txn_date
                    or (stmt_period_end is not None and iso_date > stmt_period_end)
                )
            ):
                cleaned_day = int(iso_date[8:10])
                cleaned_yr = int(iso_date[:4])
                prev_dt = dt.date.fromisoformat(last_dated.txn_date)
                for cand_m in (prev_dt.month, min(12, prev_dt.month + 1)):
                    try:
                        cand_iso = dt.date(cleaned_yr, cand_m, cleaned_day).isoformat()
                    except ValueError:
                        continue
                    if cand_iso >= last_dated.txn_date and (stmt_period_end is None or cand_iso <= stmt_period_end):
                        iso_date = cand_iso
                        ocr_low_conf = True
                        break

            # Recover dateless OCR transaction rows when debit/credit + balance/rail clearly indicate a transaction
            if iso_date is None and is_ocr and last_dated is not None and not pause_continuation:
                joined_chk = re.sub(r"\s+", " ", " ".join(str(c or "").strip() for c in row)).strip()
                narr_cell_chk = _cell_text(row, narr_col)
                if joined_chk and narr_cell_chk and not _is_footer_or_summary_line(joined_chk) and not _NON_MONETARY_MEMO_RE.search(joined_chk):
                    dr_chk = parse_indian_amount(_cell_text(row, col_map.get("debit")))[0]
                    cr_chk = parse_indian_amount(_cell_text(row, col_map.get("credit")))[0]
                    bal_chk = parse_indian_amount(_cell_text(row, col_map.get("balance")))[0]
                    if (dr_chk > 0 or cr_chk > 0) and not (dr_chk > 0 and cr_chk > 0) and (
                        bal_chk > 0
                        or re.search(r"\b(?:UPI|VPI|NEFT|RTGS|IMPS|PFMS|Credit\s+Through|PMJJBY|PMSBY|APY|Int\.?\s*Pd)\b", joined_chk, re.IGNORECASE)
                    ):
                        iso_date = last_dated.txn_date
                        ocr_low_conf = True

            date_unparseable = False
            if iso_date is None:
                joined = re.sub(r"\s+", " ", " ".join(str(c if c is not None else "").strip() for c in row)).strip()
                if not joined:
                    continue
                has_money = _row_has_money(row, col_map, is_ocr)
                if (
                    _is_footer_or_summary_line(joined)
                    or _NON_MONETARY_MEMO_RE.search(joined)
                    or _TOTAL_ROW_RE.match(joined)
                    or _is_undated_summary_line(joined)
                ):
                    if _OCR_EXTRA_FOOTER_RE.search(joined):
                        pause_continuation = True
                    if has_money and not _NON_MONETARY_MEMO_RE.search(joined) and not _FOOTER_LINE_RE.match(joined):
                        skipped_summary_money_rows += 1
                    continue
                if has_money and not _row_has_money(row, {k: v for k, v in col_map.items() if k != "balance"}, is_ocr):
                    # Only the balance cell carries money: never a zero-amount transaction (ING-01)
                    has_money = False
                if not has_money:
                    # Wrapped multi-line narration: a continuation only when every amount/balance cell is empty (ING-01)
                    if pause_continuation or len(joined) >= 180:
                        continue
                    nxt_is_empty_narr_anchor = False
                    for nxt_r in data_rows[r_i + 1 : r_i + 3]:
                        if nxt_r and any(str(c if c is not None else "").strip() for c in nxt_r):
                            nxt_date = parse_indian_date(_cell_text(nxt_r, date_col), order=date_order, ocr=is_ocr)
                            if nxt_date is not None and not _cell_text(nxt_r, narr_col):
                                nxt_is_empty_narr_anchor = True
                            break
                    if nxt_is_empty_narr_anchor:
                        pending_prefix_parts.append(joined)
                    elif last_txn is not None:
                        last_txn.raw_narration = f"{last_txn.raw_narration} {joined}".strip()
                    continue
                if is_ocr and (pause_continuation or last_dated is None):
                    ocr_dropped_money_rows += 1
                    continue
                date_unparseable = True
                iso_date = ""

            pause_continuation = False
            narration = _cell_text(row, narr_col)
            if extra_date_prefix:
                narration = f"{extra_date_prefix} {narration}".strip()
            if pending_prefix_parts:
                narration = " ".join(pending_prefix_parts + ([narration] if narration else [])).strip()
                pending_prefix_parts.clear()
            if not narration and len(row) > 1:
                narration = " ".join(str(c or "") for c in row[1:2]).strip()
            if date_unparseable and not narration:
                money_idx = {col_map[f] for f in _MONEY_FIELDS if f in col_map}
                narration = " ".join(
                    str(c).strip() for i, c in enumerate(row) if i not in money_idx and i != date_col and c is not None and str(c).strip()
                )

            ref_col = col_map.get("ref")
            ref_no = _cell_text(row, ref_col) or None
            if ref_no and (
                "@" in ref_no
                or re.search(r"\d{6,}3(?:ax|yb|ib)\b", ref_no, re.IGNORECASE)
                or ref_no.lower().startswith(("to ", "so ", "by "))
                or (" " in ref_no and not re.match(r"^[A-Z0-9/_-]+$", ref_no, re.IGNORECASE))
            ):
                narration = f"{narration} {ref_no}".strip()
                ref_no = None

            val_col = col_map.get("value_date")
            val_date = (
                parse_indian_date(_cell_text(row, val_col), order=date_order, ocr=is_ocr) if val_col is not None else None
            ) or embedded_val_date or iso_date

            bal_val, bal_hint = parse_indian_amount(_cell_text(row, col_map.get("balance")) or "0", ocr=is_ocr)
            if bal_hint == "DR" and bal_val > 0:
                bal_val = -bal_val

            debit = Decimal("0.00")
            credit = Decimal("0.00")
            is_ambiguous_dir = realigned_ambiguous

            if "debit" in col_map or "credit" in col_map:
                debit, d_hint = parse_indian_amount(_cell_text(row, col_map.get("debit")), ocr=is_ocr)
                credit, c_hint = parse_indian_amount(_cell_text(row, col_map.get("credit")), ocr=is_ocr)
                # An explicit Cr tag in the debit column, or a Dr tag / negative sign in the credit column, wins
                if d_hint == "CR" and debit > 0 and credit == 0:
                    debit, credit = Decimal("0.00"), debit
                elif c_hint == "DR" and credit > 0 and debit == 0:
                    debit, credit = credit, Decimal("0.00")
            if debit == 0 and credit == 0 and amt_col is not None and amt_col < len(row):
                amt_val, amt_hint = parse_indian_amount(_cell_text(row, amt_col), ocr=is_ocr)
                type_dir = _direction_from_type(_cell_text(row, col_map.get("type")))
                if type_dir == "CR" or (type_dir is None and amt_hint == "CR"):
                    credit = amt_val
                elif type_dir == "DR" or amt_hint == "DR":
                    debit = amt_val
                elif amt_val > 0 and signed_amounts:
                    # Signed single-amount column: negatives are debits, so unsigned values are credits (ING-13)
                    credit = amt_val
                elif amt_val > 0:
                    # Single unsigned amount without explicit Dr/Cr indicator -> solve via running balance delta
                    debit = amt_val
                    is_ambiguous_dir = True

            # Skip non-monetary rate/acct-type/opening/closing memo rows so they never pollute transaction ledger or continuity
            if (debit == 0 and credit == 0) and (_NON_MONETARY_MEMO_RE.search(narration) or bal_val == 0):
                continue
            if re.search(r"\b(?:BROUGHT\s+FORWARD|CARRIED\s+FORWARD)\b", narration, re.IGNORECASE):
                continue
            if debit > 0 and credit > 0:
                dual_rows += 1

            direction = TxDirection.CREDIT if credit > 0 and debit == 0 else TxDirection.DEBIT
            tx_id = str(uuid.uuid4())
            tx = NormalizedTransaction(
                tx_id=tx_id,
                statement_id=statement_id,
                case_id=case_id,
                row_index=len(parsed_txns),
                txn_date=iso_date,
                txn_timestamp=parse_timestamp(raw_date, iso_date) if iso_date else None,
                value_date=(val_date or iso_date) or None,
                raw_narration=narration,
                cheque_or_ref_no=ref_no,
                payment_channel="OTHER",
                direction=direction,
                debit=debit,
                credit=credit,
                running_balance=bal_val,
                category_id=52,
                category_name="Suspense / Uncategorized",
                categorization_source="PENDING",
                confidence_score=0.0,
                source_account_number=meta["account_number"],
                source_holder_name=meta["account_holder_name"],
            )
            parsed_txns.append(tx)
            if sheet_name is not None:
                source_sheet_by_tx[tx_id] = sheet_name
            if is_ambiguous_dir:
                ambiguous_tx_ids.add(tx_id)
            if ocr_low_conf:
                _flag(tx_id, AnomalyFlag.LOW_OCR_CONFIDENCE)
            if date_unparseable:
                undated_ids.append(tx_id)
                _flag(tx_id, AnomalyFlag.DATE_UNPARSEABLE)
                where = f"sheet '{sheet_name}' " if sheet_name else ""
                undated_notes.append(
                    f"{where}row {row_offset + r_i}: date '{raw_date or '(blank)'}' unparseable "
                    f"(debit {_money_str(debit)}, credit {_money_str(credit)})"
                )
            elif not order_decided and _is_order_ambiguous(raw_date_tok):
                _flag(tx_id, AnomalyFlag.DATE_ORDER_AMBIGUOUS)
                order_ambiguous_rows += 1
            last_txn = tx
            if not date_unparseable:
                last_dated = tx

        if sheet_name is not None:
            sheet_summaries.append(
                {"name": sheet_name, "rows": len(parsed_txns) - block_start, "header_row": header_idx + 1 if header_idx >= 0 else None}
            )

    if undated_ids:
        _fill_placeholder_dates(parsed_txns, set(undated_ids), stmt_period_start)
        shown = "; ".join(undated_notes[:10]) + (f"; ... {len(undated_notes) - 10} more" if len(undated_notes) > 10 else "")
        warnings.append(
            f"{len(undated_ids)} row(s) with amounts had unparseable dates and were kept with flag DATE_UNPARSEABLE "
            f"(date borrowed from the neighbouring row): {shown}."
        )
    if order_ambiguous_rows:
        warnings.append(
            f"Day/month order could not be determined ({order_basis}); {order_ambiguous_rows} row(s) read as DD/MM "
            "and flagged DATE_ORDER_AMBIGUOUS."
        )
    if skipped_summary_money_rows:
        warnings.append(f"{skipped_summary_money_rows} undated total/summary line(s) with amounts were not treated as transactions.")
    if ocr_dropped_money_rows:
        warnings.append(
            f"{ocr_dropped_money_rows} undated OCR line(s) with amounts outside the transaction table were ignored; review the scan."
        )
    if dual_rows:
        warnings.append(
            f"{dual_rows} row(s) have both debit and credit amounts; review them (flagged ZERO_OR_DUAL_AMOUNT)."
        )

    if ambiguous_tx_ids and parsed_txns:
        unresolved: set[str] = set()
        parsed_txns = _resolve_ambiguous_directions(parsed_txns, ambiguous_tx_ids, header_opening_bal, unresolved)
        for tx_id in ambiguous_tx_ids:
            _flag(tx_id, AnomalyFlag.DIRECTION_INFERRED)
        if unresolved:
            warnings.append(
                f"Direction of {len(unresolved)} row(s) could not be confirmed from running balances or Dr/Cr markers; "
                "recorded as DEBIT and flagged DIRECTION_INFERRED."
            )

    # Stated-period coverage (ING-16) and filename-period cross-check (ING-18)
    undated_set = set(undated_ids)
    period_coverage: Optional[Dict[str, Any]] = None
    if stmt_period_start and stmt_period_end and parsed_txns:
        outside = [
            t for t in parsed_txns
            if t.tx_id not in undated_set and t.txn_date and not (stmt_period_start <= t.txn_date <= stmt_period_end)
        ]
        for t in outside:
            _flag(t.tx_id, AnomalyFlag.OUTSIDE_STATEMENT_PERIOD)
        dated = [t.txn_date for t in parsed_txns if t.txn_date]
        first_d, last_d = min(dated), max(dated)
        period_coverage = {
            "stated_start": stmt_period_start,
            "stated_end": stmt_period_end,
            "first_txn_date": first_d,
            "last_txn_date": last_d,
            "rows_outside": len(outside),
            "days_without_rows_at_start": max(0, (dt.date.fromisoformat(first_d) - dt.date.fromisoformat(stmt_period_start)).days),
            "days_without_rows_at_end": max(0, (dt.date.fromisoformat(stmt_period_end) - dt.date.fromisoformat(last_d)).days),
        }
        if outside:
            shown = ", ".join(f"row {t.row_index + 1} ({t.txn_date})" for t in outside[:5])
            more = f" and {len(outside) - 5} more" if len(outside) > 5 else ""
            warnings.append(
                f"{len(outside)} row(s) fall outside the stated statement period {stmt_period_start} to "
                f"{stmt_period_end}: {shown}{more}; flagged OUTSIDE_STATEMENT_PERIOD."
            )
    file_period = filename_crosscheck.get("filename_period")
    if file_period:
        if stmt_period_start and stmt_period_end:
            same = (stmt_period_start, stmt_period_end) == (file_period["start"], file_period["end"])
            filename_crosscheck["period"] = "match" if same else "conflict"
            if not same:
                warnings.append(
                    f"Statement period conflict: header says {stmt_period_start} to {stmt_period_end} but the filename "
                    f"says {file_period['start']} to {file_period['end']}; kept the header period."
                )
        else:
            stray = [t for t in parsed_txns if t.txn_date and not (file_period["start"] <= t.txn_date <= file_period["end"])]
            filename_crosscheck["period"] = "conflict" if stray else "filename_only"
            if stray:
                warnings.append(
                    f"{len(stray)} row(s) fall outside the filename period {file_period['start']} to "
                    f"{file_period['end']}; check the file naming."
                )
    else:
        filename_crosscheck["period"] = "header_only" if (stmt_period_start and stmt_period_end) else "missing"

    for tx in parsed_txns:
        extra = row_flags.get(tx.tx_id)
        if extra:
            tx.anomaly_flags = list(dict.fromkeys(list(tx.anomaly_flags) + extra))

    has_balances = any(t.running_balance != 0 for t in parsed_txns)
    if parsed_txns:
        parsed_txns = [enrich_transaction_narration(t) for t in parsed_txns]
        txn_period_start = min(t.txn_date for t in parsed_txns)
        txn_period_end = max(t.txn_date for t in parsed_txns)
        period_start, period_end = txn_period_start, txn_period_end
        # EDGE-22 / ING-16: the stated statement period bounds the account (ABB, month iteration, dormancy),
        # so leading/trailing days without rows count. Rows outside it (flagged above) still widen it.
        if stmt_period_start and stmt_period_end and stmt_period_start <= stmt_period_end:
            period_start = min(stmt_period_start, txn_period_start)
            period_end = max(stmt_period_end, txn_period_end)
        first = parsed_txns[0]
        derived_opening = (first.running_balance - first.credit + first.debit).quantize(Decimal("0.01"))
        opening_bal = header_opening_bal if (header_opening_bal is not None and has_balances) else derived_opening
        closing_bal = stated_closing_bal if (stated_closing_bal is not None and has_balances) else parsed_txns[-1].running_balance
        if has_balances:
            descending = parsed_txns[0].txn_date > parsed_txns[-1].txn_date
            chron_first = parsed_txns[-1] if descending else parsed_txns[0]
            chron_last = parsed_txns[0] if descending else parsed_txns[-1]
            tol = Decimal("0.05")
            if header_opening_bal is not None:
                implied = (chron_first.running_balance - chron_first.credit + chron_first.debit).quantize(Decimal("0.01"))
                if abs(implied - header_opening_bal) > tol:
                    warnings.append(
                        f"First row does not reconcile with the stated opening balance {_money_str(header_opening_bal)} "
                        f"(row implies {_money_str(implied)})."
                    )
            if stated_closing_bal is not None and abs(chron_last.running_balance - stated_closing_bal) > tol:
                warnings.append(
                    f"Last row balance {_money_str(chron_last.running_balance)} does not match the stated closing balance "
                    f"{_money_str(stated_closing_bal)}."
                )
    else:
        today = dt.date.today().isoformat()
        period_start = today
        period_end = today
        opening_bal = Decimal("0.00")
        closing_bal = Decimal("0.00")
        warnings.append("No transaction rows extracted; consider using the PDF Draw Tool.")

    stmt = BankAccountStatement(
        statement_id=statement_id,
        case_id=case_id,
        source_filename=os.path.basename(filename),
        sha256_hash=sha256_hash,
        bank_code=bank_profile.bank_code,
        bank_name=bank_profile.bank_name,
        account_number=meta["account_number"] or "UNKNOWN_AC",
        ifsc_code=meta["ifsc_code"],
        account_holder_name=meta["account_holder_name"] or "Unknown Holder",
        account_type=meta["account_type"],
        period_start=period_start,
        period_end=period_end,
        opening_balance=opening_bal,
        closing_balance=closing_bal,
        total_transactions=len(parsed_txns),
        is_ocr_derived=is_ocr,
        ingested_at=dt.datetime.now(dt.timezone.utc).isoformat(),
        pan=_header_pan(header_text),
    )
    return stmt, parsed_txns, {
        "warnings": warnings,
        "col_map": first_col_map,
        "date_order": {"order": date_order, "decided": order_decided, "basis": order_basis},
        "sheets": sheet_summaries,
        "source_sheet_by_tx": source_sheet_by_tx,
        "stated_opening_balance": _money_str(header_opening_bal) if header_opening_bal is not None else None,
        "stated_closing_balance": _money_str(stated_closing_bal) if stated_closing_bal is not None else None,
        "stated_period": {"start": stmt_period_start, "end": stmt_period_end} if period_raw else None,
        "period_coverage": period_coverage,
        "transaction_period": (
            {"start": min(t.txn_date for t in parsed_txns), "end": max(t.txn_date for t in parsed_txns)}
            if parsed_txns else None
        ),
        "filename_crosscheck": filename_crosscheck,
        **pdf_meta,
    }


def _fill_placeholder_dates(txns: List[NormalizedTransaction], undated: set[str], fallback: Optional[str]) -> None:
    """Give DATE_UNPARSEABLE rows the nearest preceding (else following) parsed date so they stay in sequence."""
    prev: Optional[str] = None
    for tx in txns:
        if tx.txn_date:
            prev = tx.txn_date
        elif prev:
            tx.txn_date = prev
    nxt: Optional[str] = None
    for tx in reversed(txns):
        if tx.txn_date:
            nxt = tx.txn_date
        elif nxt:
            tx.txn_date = nxt
    default = fallback or dt.date.today().isoformat()
    for tx in txns:
        if tx.tx_id in undated:
            tx.txn_date = tx.txn_date or default
            tx.value_date = tx.value_date or tx.txn_date
            tx.txn_timestamp = tx.txn_timestamp or f"{tx.txn_date}T12:00:00"


# ---------------------------------------------------------------------------
# Bulk ingestion
# ---------------------------------------------------------------------------

PROCESS_POOL_MIN_FILES = 8


@dataclass
class FileParseResult:
    """Per-file outcome of a bulk ingest: a failure never aborts the other files (ING-14 / PDF-15)."""

    filename: str
    ok: bool
    statement: Optional[BankAccountStatement] = None
    transactions: List[NormalizedTransaction] = field(default_factory=list)
    meta: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    error_type: Optional[str] = None
    elapsed_s: float = 0.0

    def as_tuple(self) -> Tuple[BankAccountStatement, List[NormalizedTransaction], Dict[str, Any]]:
        if not self.ok or self.statement is None:
            raise ValueError(f"'{self.filename}' failed to parse: {self.error}")
        return self.statement, self.transactions, self.meta

    def to_status(self) -> Dict[str, Any]:
        return {
            "filename": self.filename,
            "ok": self.ok,
            "error": self.error,
            "error_type": self.error_type,
            "statement_id": self.statement.statement_id if self.statement else None,
            "transactions": len(self.transactions),
            "warnings": list(self.meta.get("warnings", [])) if self.meta else [],
            "elapsed_s": round(self.elapsed_s, 3),
        }


class BulkIngestError(ValueError):
    """Raised by legacy (tuple-returning) bulk ingest when some files fail; `.results` keeps every outcome."""

    def __init__(self, results: List[FileParseResult]):
        self.results = results
        failed = [r for r in results if not r.ok]
        detail = "; ".join(f"{r.filename}: {r.error}" for r in failed[:5])
        more = f"; ... {len(failed) - 5} more" if len(failed) > 5 else ""
        super().__init__(f"{len(failed)} of {len(results)} statement file(s) failed to parse: {detail}{more}")


_BulkJob = Tuple[str, bytes, Optional[str], str, Optional[DrawTemplate], bool]


def _parse_file_job(job: _BulkJob) -> FileParseResult:
    filename, raw, password, case_id, draw_template, enrich = job
    t0 = time.perf_counter()
    try:
        stmt, txns, meta = parse_statement_bytes(filename, raw, case_id, password, draw_template)
        if enrich:
            # Narration parsing is pure per-row regex work: doing it here spreads it over the pool (BE-06).
            for t in txns:
                enrich_transaction_narration(t)
            meta["narration_enriched"] = True
        return FileParseResult(filename, True, stmt, txns, meta, elapsed_s=time.perf_counter() - t0)
    except Exception as exc:  # noqa: BLE001 - isolating one bad file from the batch is the purpose
        return FileParseResult(
            filename,
            False,
            error=str(exc) or exc.__class__.__name__,
            error_type=exc.__class__.__name__,
            elapsed_s=time.perf_counter() - t0,
        )


def _notify(on_progress: Optional[Any], done: int, total: int) -> None:
    if on_progress is not None:
        try:
            on_progress(done, total)
        except Exception:  # noqa: BLE001 - a progress callback must never break parsing
            _LOG.debug("bulk ingest progress callback failed", exc_info=True)


def _ordered_map(pool: Any, jobs: List[_BulkJob], on_progress: Optional[Any]) -> List[FileParseResult]:
    """Submit every job, report progress as each finishes, return results in input order."""
    futures = [pool.submit(_parse_file_job, j) for j in jobs]
    index = {f: i for i, f in enumerate(futures)}
    out: List[Optional[FileParseResult]] = [None] * len(jobs)
    done = 0
    for fut in as_completed(futures):
        out[index[fut]] = fut.result()
        done += 1
        _notify(on_progress, done, len(jobs))
    return out  # type: ignore[return-value]


def _run_bulk_jobs(
    jobs: List[_BulkJob], max_workers: Optional[int], use_processes: Optional[bool], on_progress: Optional[Any] = None
) -> List[FileParseResult]:
    if len(jobs) <= 1:
        out = [_parse_file_job(j) for j in jobs]
        _notify(on_progress, len(out), len(jobs))
        return out
    cpu = os.cpu_count() or 2
    want_processes = len(jobs) >= PROCESS_POOL_MIN_FILES if use_processes is None else use_processes
    if want_processes and cpu > 1:
        workers = max(1, min(max_workers or cpu, len(jobs)))
        try:
            ctx = multiprocessing.get_context("spawn")  # fork is unsafe in threaded servers and with macOS frameworks
            with ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
                return _ordered_map(pool, jobs, on_progress)
        except (pickle.PicklingError, BrokenProcessPool, OSError, NotImplementedError, AttributeError, TypeError, EOFError, ImportError) as exc:
            _LOG.warning("Process pool unavailable for bulk ingest (%s: %s); falling back to threads.", exc.__class__.__name__, exc)
    workers = max(1, min(max_workers or 4, len(jobs)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return _ordered_map(pool, jobs, on_progress)


def bulk_parse_statements(
    files: Sequence[Tuple[str, bytes, Optional[str]]],
    case_id: str,
    draw_template: Optional[DrawTemplate] = None,
    max_workers: Optional[int] = None,
    return_status: bool = False,
    use_processes: Optional[bool] = None,
    enrich_narrations: bool = False,
    on_progress: Optional[Any] = None,
) -> List[Any]:
    """
    Bulk ingestion for 100+ statements. Each file is parsed in isolation; a process pool is used for
    `PROCESS_POOL_MIN_FILES`+ files (falling back to threads when processes or pickling are unavailable).

    `return_status=True` returns one `FileParseResult` per input file, in order, including failures.
    `enrich_narrations=True` also runs the narration parser on every row inside the worker and sets
    ``meta['narration_enriched']``. `on_progress(done, total)` is called as files finish (in the caller's thread).
    The legacy default returns `(statement, transactions, meta)` tuples and, if any file failed, raises
    `BulkIngestError` *after* all files were attempted; its `.results` holds every per-file outcome.
    """
    jobs: List[_BulkJob] = [(fn, raw, pwd, case_id, draw_template, bool(enrich_narrations)) for fn, raw, pwd in files]
    results = _run_bulk_jobs(jobs, max_workers, use_processes, on_progress)
    if return_status:
        return results
    if any(not r.ok for r in results):
        raise BulkIngestError(results)
    return [r.as_tuple() for r in results]
