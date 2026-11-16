"""Native PDF parser (Poppler bbox XML preferred, linear pure-Python content-stream interpreter as fallback),
per-page scanned-PDF OCR bridge, Draw Tool slicer, and PDF fixture generator."""

from __future__ import annotations

import base64
import binascii
import io
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from bisect import bisect_left
from dataclasses import dataclass, field
from functools import lru_cache
import datetime as _dt
import hashlib
import math
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from cyphersol.ingestion.ocr_engine import (
    InflateBudget,
    MAX_STREAM_INFLATE_BYTES,
    _find_binary,
    _inflate_bounded,
    _is_pdf_encrypted,
    _poppler_bin,
    decode_predictor,
    ocr_pdf_bytes,
    private_tempdir,
    render_pdf_page_png,
    write_private_file,
)

MAX_PAGE_CONTENT_BYTES = 16 << 20   # decoded content-stream bytes interpreted per page (incl. Form XObjects)
MAX_FORM_DEPTH = 4                  # nested Form XObject recursion limit
MAX_PAGES = 5000
POPPLER_TIMEOUT_S = 120
TJ_SPACE_EM = 0.15                  # TJ displacement (in em) rendered as a word space
TJ_BREAK_EM = 1.0                   # TJ displacement (in em) that starts a new positioned token
MIN_PAGE_TEXT_CHARS = 24            # below this a page with a full-page scan image is re-OCR'd
MAX_DOC_CONTENT_BYTES = 64 << 20    # content-stream bytes interpreted per document, shared by all pages and Do calls
PDF_TEXT_DEADLINE_S = 150.0         # wall clock for Poppler + the pure-Python pass together, per document
MAX_OCR_PAGES = 250                 # textless pages OCR'd per document
OCR_DOC_DEADLINE_S = 420.0          # wall clock for rendering + OCR of all pages of one document
MAX_LEX_DEPTH = 64                  # nesting of [ ] / << >> in a content stream
MAX_LEX_GROUP_ITEMS = 65536         # operands (across nested arrays) collected for one operator
MAX_CID_WIDTH_ENTRIES = 65536       # CIDs expanded from one /W array in total


class _WorkBudget:
    """Document-wide budget for interpreted content bytes and wall-clock time (PDF-01/PDF-07)."""

    def __init__(self, max_bytes: int = MAX_DOC_CONTENT_BYTES, deadline: Optional[float] = None) -> None:
        self.bytes_left = max_bytes
        self.deadline = deadline
        self.exhausted = False
        self.timed_out = False

    def expired(self) -> bool:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            self.timed_out = True
        return self.timed_out

    def take(self, n: int) -> int:
        """Charge up to ``n`` bytes; returns how many may be interpreted."""
        if self.expired():
            return 0
        allowed = min(n, self.bytes_left)
        self.bytes_left -= allowed
        if allowed < n:
            self.exhausted = True
        return allowed


@dataclass
class PdfTextToken:
    page: int
    x: float
    y: float
    text: str
    x_end: float = 0.0


@dataclass
class PdfExtractionResult:
    pages_text: List[str]
    page_sizes: List[Tuple[float, float]]
    tokens: List[PdfTextToken]
    rows: List[List[str]]
    is_encrypted: bool = False
    needs_ocr: bool = False
    ocr_engine: Optional[str] = None
    # Pages (1-based) whose tokens came from OCR rather than the native text layer.
    ocr_pages: List[int] = field(default_factory=list)
    # None, or one of "password_required", "wrong_password", "decryption_unavailable", "corrupt".
    error: Optional[str] = None
    # Native text layer source: "poppler", "pure_python", "pdfplumber", or None when everything was OCR'd.
    text_source: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    # OCR confidence gate (PDF-11): {page: 0..1} for OCR'd pages, pages kept with low confidence, and pages
    # whose OCR text was discarded {page: reason}.
    ocr_confidence_by_page: Dict[int, float] = field(default_factory=dict)
    low_confidence_pages: List[int] = field(default_factory=list)
    ocr_rejected_pages: Dict[int, str] = field(default_factory=dict)
    # Which password formula opened an encrypted file ("supplied", "dob_ddmmyyyy", ...); never the password.
    password_label: Optional[str] = None
    # True when an encrypted file was decrypted in memory (pikepdf/qpdf), so no password reached a subprocess argv.
    decrypted_in_process: bool = False


# ---------------------------------------------------------------------------
# PDF literal / hex strings and ToUnicode CMaps
# ---------------------------------------------------------------------------

_LITERAL_ESCAPES = {
    ord("n"): 10, ord("r"): 13, ord("t"): 9, ord("b"): 8, ord("f"): 12,
    ord("("): 40, ord(")"): 41, ord("\\"): 92,
}
_NON_HEX_RE = re.compile(r"[^0-9A-Fa-f]")
_NON_HEX_BYTES_RE = re.compile(rb"[^0-9A-Fa-f]")
_BFCHAR_BLOCK_RE = re.compile(r"beginbfchar(.*?)endbfchar", re.DOTALL)
_BFRANGE_BLOCK_RE = re.compile(r"beginbfrange(.*?)endbfrange", re.DOTALL)
_BFCHAR_PAIR_RE = re.compile(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>")
_BFRANGE_ITEM_RE = re.compile(r"<([0-9A-Fa-f]+)>\s*<([0-9A-Fa-f]+)>\s*(?:<([0-9A-Fa-f]+)>|\[([^\]]+)\])", re.DOTALL)
_HEX_ITEM_RE = re.compile(r"<([0-9A-Fa-f]+)>")


def _unescape_pdf_literal(raw: bytes) -> bytes:
    """Resolve backslash escapes of a PDF literal string body (without the outer parentheses)."""
    if b"\\" not in raw:
        return raw
    out = bytearray()
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == 92 and i + 1 < n:
            nxt = raw[i + 1]
            mapped = _LITERAL_ESCAPES.get(nxt)
            if mapped is not None:
                out.append(mapped)
                i += 2
                continue
            if nxt in (10, 13):  # line continuation
                i += 2
                if nxt == 13 and i < n and raw[i] == 10:
                    i += 1
                continue
            if 48 <= nxt <= 55:  # octal \ddd
                j = i + 1
                val = 0
                while j < min(n, i + 4) and 48 <= raw[j] <= 55:
                    val = val * 8 + (raw[j] - 48)
                    j += 1
                out.append(val & 0xFF)
                i = j
                continue
            i += 1  # unknown escape: drop the backslash
            continue
        out.append(ch)
        i += 1
    return bytes(out)


def _decode_pdf_literal(raw: bytes, cmap: Optional[Dict[int, str]] = None) -> str:
    return _FontInfo.from_cmap(cmap).decode(_unescape_pdf_literal(raw))


def _decode_pdf_hex(hex_str: str, cmap: Optional[Dict[int, str]] = None) -> str:
    clean = _NON_HEX_RE.sub("", hex_str)
    if not clean:
        return ""
    if len(clean) % 2 == 1:
        clean += "0"
    raw = bytes.fromhex(clean)
    if cmap:
        return _FontInfo.from_cmap(cmap).decode(raw, hex_string=True)
    return _decode_unmapped(raw, hex_string=True)


def _decode_unmapped(raw: bytes, hex_string: bool = False) -> str:
    if hex_string:
        if raw.startswith(b"\xfe\xff"):
            return raw[2:].decode("utf-16-be", errors="replace")
        if len(raw) >= 2 and len(raw) % 2 == 0 and sum(raw[0::2]) == 0:
            return "".join(chr(b) for b in raw[1::2])
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="replace")


def _decode_cmap_hex_to_unicode(hex_dst: str) -> str:
    raw = bytes.fromhex(_NON_HEX_RE.sub("", hex_dst))
    if not raw:
        return ""
    if len(raw) % 2 == 0:
        try:
            return raw.decode("utf-16-be")
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1", errors="replace")


def _parse_tounicode_cmap(cmap_bytes: bytes) -> Dict[int, str]:
    """Parse PDF /ToUnicode CMap beginbfchar and beginbfrange mappings."""
    txt = cmap_bytes.decode("latin-1", errors="replace")
    mapping: Dict[int, str] = {}
    for block in _BFCHAR_BLOCK_RE.finditer(txt):
        for pair in _BFCHAR_PAIR_RE.finditer(block.group(1)):
            mapping[int(pair.group(1), 16)] = _decode_cmap_hex_to_unicode(pair.group(2))
    for block in _BFRANGE_BLOCK_RE.finditer(txt):
        for rng in _BFRANGE_ITEM_RE.finditer(block.group(1)):
            start_code = int(rng.group(1), 16)
            end_code = int(rng.group(2), 16)
            if rng.group(3):
                base_val = int(rng.group(3), 16)
                for offset, code in enumerate(range(start_code, min(end_code + 1, start_code + 512))):
                    try:
                        mapping[code] = chr(base_val + offset)
                    except ValueError:
                        pass
            elif rng.group(4):
                for offset, hex_u in enumerate(_HEX_ITEM_RE.findall(rng.group(4))):
                    if start_code + offset <= end_code:
                        mapping[start_code + offset] = _decode_cmap_hex_to_unicode(hex_u)
    return mapping


@dataclass
class _FontInfo:
    """What the text interpreter needs from a font: code->Unicode, code->advance width (1/1000 em)."""

    cmap: Optional[Dict[int, str]] = None
    widths: Dict[int, float] = field(default_factory=dict)
    default_width: float = 520.0
    two_byte: bool = False

    @classmethod
    def from_cmap(cls, cmap: Optional[Dict[int, str]]) -> "_FontInfo":
        return cls(cmap=cmap or None, two_byte=bool(cmap) and max(cmap.keys(), default=0) > 255)

    def codes(self, raw: bytes) -> List[int]:
        if self.two_byte and len(raw) >= 2:
            return [(raw[k] << 8) | raw[k + 1] for k in range(0, len(raw) - 1, 2)]
        return list(raw)

    def decode(self, raw: bytes, hex_string: bool = False) -> str:
        cmap = self.cmap
        if not cmap:
            if self.two_byte and len(raw) % 2 == 0:
                return "".join(chr(c) for c in self.codes(raw) if 32 <= c < 0xD800)
            return _decode_unmapped(raw, hex_string=hex_string)
        use_two = self.two_byte or (
            hex_string and len(raw) % 2 == 0 and any(raw[k] == 0 for k in range(0, len(raw), 2))
        )
        if use_two and len(raw) % 2 == 0:
            chars = []
            for k in range(0, len(raw), 2):
                cid = (raw[k] << 8) | raw[k + 1]
                if cid in cmap:
                    chars.append(cmap[cid])
                elif 32 <= cid <= 126:
                    chars.append(chr(cid))
            if chars or self.two_byte:
                return "".join(chars)
        if hex_string:
            return "".join(cmap.get(b, chr(b) if 32 <= b <= 126 else "") for b in raw)
        return "".join(cmap.get(b, chr(b)) for b in raw)

    def advance(self, codes: Sequence[int]) -> Tuple[float, int]:
        """Return (sum of glyph widths in 1/1000 em, number of single-byte spaces)."""
        w = self.widths
        dw = self.default_width
        total = 0.0
        spaces = 0
        for c in codes:
            total += w.get(c, dw)
            if c == 32 and not self.two_byte:
                spaces += 1
        return total, spaces


# ---------------------------------------------------------------------------
# Linear PDF object indexer, bounded stream decoder, dictionary helpers
# ---------------------------------------------------------------------------

_OBJ_HDR_RE = re.compile(rb"(?<![0-9])(\d{1,10})\s+(\d{1,5})\s+obj\b")
_ENDOBJ_RE = re.compile(rb"endobj")
_STREAM_KW_RE = re.compile(rb"(?<![A-Za-z])stream(?:\r\n|\r|\n)")
_ENDSTREAM_RE = re.compile(rb"endstream")
_REF_RE = re.compile(rb"(\d+)\s+(\d+)\s+R\b")
_REF_AT_RE = re.compile(rb"(\d+)\s+(\d+)\s+R\b")
_INT_AT_RE = re.compile(rb"[+-]?\d+")
_SCALAR_AT_RE = re.compile(rb"/?[^\s/<>\[\]()%{}]+")
_NUMBER_RE = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)")
_NAME_ITEM_RE = re.compile(rb"/([^\s/<>\[\]()%{}]+)")
_DICT_DELIM_RE = re.compile(rb"<<|>>|\(|\)|\\.")
_BRACKET_DELIM_RE = re.compile(rb"\[|\]|\(|\)|\\.")
_ROOT_REF_RE = re.compile(rb"/Root\s+(\d+)\s+\d+\s+R")
_TYPE_PAGE_RE = re.compile(rb"/Type\s*/Page\b(?!\s*s\b)")
_TYPE_PAGES_RE = re.compile(rb"/Type\s*/Pages\b")
_IMAGE_MARKER_RE = re.compile(rb"/Subtype\s*/Image\b|/DCTDecode|/JBIG2Decode|/CCITTFaxDecode|/JPXDecode")
_SUBTYPE_IMAGE_RE = re.compile(rb"/Subtype\s*/Image\b")
_SUBTYPE_FORM_RE = re.compile(rb"/Subtype\s*/Form\b")
_W_ITEM_RE = re.compile(rb"\[|\]|[+-]?(?:\d+\.?\d*|\.\d+)")
_NAMED_REF_RE = re.compile(rb"/([^\s/<>\[\]()%{}]+)\s+(\d+)\s+\d+\s+R")
_TYPE0_RE = re.compile(rb"/Subtype\s*/Type0\b")
_OBJSTM_TYPE_RE = re.compile(rb"/Type\s*/ObjStm\b")
_XREF_TYPE_RE = re.compile(rb"/Type\s*/XRef\b")
_WHITESPACE_BYTES_RE = re.compile(rb"\s+")
_NAME_ESCAPE_RE = re.compile(r"#([0-9A-Fa-f]{2})")
_WIDTH_RE = re.compile(rb"/Width\s+(\d+)")
_HEIGHT_RE = re.compile(rb"/Height\s+(\d+)")


@lru_cache(maxsize=64)
def _key_re(key: bytes) -> "re.Pattern[bytes]":
    return re.compile(rb"/" + re.escape(key) + rb"(?![^\s/<>\[\]()%{}])\s*")


def _balanced_end(body: bytes, start: int, delim_re: "re.Pattern[bytes]", open_tok: bytes, close_tok: bytes) -> int:
    """Index just past the delimiter that closes the `open_tok` at `start` (string/escape aware, linear)."""
    depth = 0
    in_str = 0
    for m in delim_re.finditer(body, start):
        tok = m.group()
        if tok[:1] == b"\\":
            continue
        if tok == b"(":
            in_str += 1
        elif tok == b")":
            in_str = max(0, in_str - 1)
        elif in_str:
            continue
        elif tok == open_tok:
            depth += 1
        elif tok == close_tok:
            depth -= 1
            if depth == 0:
                return m.end()
    return len(body)


def _dict_value(body: bytes, key: bytes) -> Optional[bytes]:
    """Raw value of `/key` in a PDF dictionary body: nested dict, array, indirect ref, name or number."""
    m = _key_re(key).search(body)
    if not m:
        return None
    i = m.end()
    if body.startswith(b"<<", i):
        return body[i:_balanced_end(body, i, _DICT_DELIM_RE, b"<<", b">>")]
    if body.startswith(b"[", i):
        return body[i:_balanced_end(body, i, _BRACKET_DELIM_RE, b"[", b"]")]
    ref = _REF_AT_RE.match(body, i)
    if ref:
        return ref.group(0)
    sc = _SCALAR_AT_RE.match(body, i)
    return sc.group(0) if sc else None


def _parse_pdf_objects(pdf_bytes: bytes) -> Dict[int, bytes]:
    """
    Index all indirect `N G obj ... endobj` definitions in linear(ithmic) time.
    Stream bodies are skipped wholesale, so `obj`/`endobj` bytes inside stream data cannot split objects,
    and a missing `endobj` ends the object at the next header instead of swallowing the file.
    Later definitions (incremental updates) override earlier ones.
    """
    headers = list(_OBJ_HDR_RE.finditer(pdf_bytes))
    endobjs = [m.start() for m in _ENDOBJ_RE.finditer(pdf_bytes)]
    streams = [m.start() for m in _STREAM_KW_RE.finditer(pdf_bytes)]
    endstreams = [m.start() for m in _ENDSTREAM_RE.finditer(pdf_bytes)]
    n = len(pdf_bytes)
    objs: Dict[int, bytes] = {}
    pos = 0
    for h_idx, m in enumerate(headers):
        if m.start() < pos:
            continue
        start = m.end()
        nxt_hdr = headers[h_idx + 1].start() if h_idx + 1 < len(headers) else n
        k = bisect_left(endobjs, start)
        end = endobjs[k] if k < len(endobjs) else n
        si = bisect_left(streams, start)
        if si < len(streams) and streams[si] < min(end, nxt_hdr):
            ei = bisect_left(endstreams, streams[si])
            if ei < len(endstreams):
                k2 = bisect_left(endobjs, endstreams[ei])
                end = endobjs[k2] if k2 < len(endobjs) else n
                body_end = end
            else:
                body_end = end = n
        else:
            body_end = min(end, nxt_hdr)
            if nxt_hdr < end:
                end = nxt_hdr
        objs[int(m.group(1))] = pdf_bytes[start:body_end]
        pos = end
    return objs


def _filters_of(dict_b: bytes) -> List[bytes]:
    val = _dict_value(dict_b, b"Filter")
    if not val:
        return []
    return _NAME_ITEM_RE.findall(val)


def _decode_parms_of(dict_b: bytes, n_filters: int, objs: Optional[Dict[int, bytes]] = None) -> List[bytes]:
    """The /DecodeParms dict for each filter in the chain (b"" where none applies)."""
    val = _dict_value(dict_b, b"DecodeParms") or _dict_value(dict_b, b"DP")
    out = [b""] * max(1, n_filters)
    if not val:
        return out
    val = val.strip()
    ref = _REF_AT_RE.fullmatch(val)
    if ref and objs is not None:
        val = objs.get(int(ref.group(1)), b"").strip()
    if val.startswith(b"<<"):
        out[0] = val
    elif val.startswith(b"["):
        items: List[bytes] = []
        i = 1
        while i < len(val) and len(items) < len(out):
            if val.startswith(b"<<", i):
                end = _balanced_end(val, i, _DICT_DELIM_RE, b"<<", b">>")
                items.append(val[i:end])
                i = end
            elif val.startswith(b"null", i):
                items.append(b"")
                i += 4
            else:
                rm = _REF_AT_RE.match(val, i)
                if rm:
                    items.append(objs.get(int(rm.group(1)), b"") if objs is not None else b"")
                    i = rm.end()
                else:
                    i += 1
        for k, item in enumerate(items):
            out[k] = item
    return out


def _apply_decode_parms(data: bytes, parms: bytes) -> Optional[bytes]:
    def _int(key: bytes, default: int) -> int:
        v = _dict_value(parms, key)
        return int(v) if v and _INT_AT_RE.fullmatch(v) else default

    predictor = _int(b"Predictor", 1)
    if predictor <= 1:
        return data
    return decode_predictor(data, predictor, colors=_int(b"Colors", 1),
                            bits_per_component=_int(b"BitsPerComponent", 8), columns=_int(b"Columns", 1))


def _parse_xref_streams(objs: Dict[int, bytes], budget: Optional[InflateBudget] = None) -> Dict[int, Tuple[int, int, int]]:
    """
    Decode every /Type /XRef cross-reference stream (PDF 1.5+, usually PNG-predicted) into
    {object number: (type, field2, field3)}; type 1 = (offset, generation), type 2 = (object stream, index).
    Later sections do not override entries already read from a newer (later-defined) stream.
    """
    entries: Dict[int, Tuple[int, int, int]] = {}
    for body in objs.values():
        head = body[:4096]
        if not _XREF_TYPE_RE.search(head):
            continue
        dict_b, data = _extract_stream_from_obj(body, budget=budget, objs=objs)
        if not data:
            continue
        w_v = _dict_value(dict_b, b"W")
        widths = [int(x) for x in _INT_AT_RE.findall(w_v or b"")][:3]
        if len(widths) != 3 or any(x < 0 or x > 8 for x in widths) or sum(widths) == 0:
            continue
        size_v = _dict_value(dict_b, b"Size")
        size = int(size_v) if size_v and size_v.isdigit() else 0
        idx_v = _dict_value(dict_b, b"Index")
        idx = [int(x) for x in _INT_AT_RE.findall(idx_v or b"")] or [0, size]
        rec = sum(widths)
        pos = 0
        for start, count in zip(idx[0::2], idx[1::2]):
            for k in range(max(0, count)):
                if pos + rec > len(data):
                    break
                fields: List[int] = []
                off = pos
                for wd in widths:
                    fields.append(int.from_bytes(data[off : off + wd], "big") if wd else -1)
                    off += wd
                pos += rec
                ftype = fields[0] if widths[0] else 1
                f2 = fields[1] if fields[1] >= 0 else 0
                f3 = fields[2] if fields[2] >= 0 else 0
                entries.setdefault(start + k, (ftype, f2, f3))
    return entries


def _extract_stream_from_obj(
    obj_body: bytes,
    budget: Optional[InflateBudget] = None,
    objs: Optional[Dict[int, bytes]] = None,
    max_out: int = MAX_STREAM_INFLATE_BYTES,
) -> Tuple[bytes, Optional[bytes]]:
    """Split an indirect object into (dict_bytes, decoded_stream_bytes_or_None) with bounded decompression."""
    sm = _STREAM_KW_RE.search(obj_body)
    if not sm:
        return obj_body, None
    dict_b = obj_body[: sm.start()]
    data_start = sm.end()
    data_b: Optional[bytes] = None

    length: Optional[int] = None
    lv = _dict_value(dict_b, b"Length")
    if lv:
        ref = _REF_AT_RE.match(lv)
        if ref and objs is not None:
            im = _INT_AT_RE.search(objs.get(int(ref.group(1)), b""))
            length = int(im.group()) if im else None
        elif not ref and _INT_AT_RE.fullmatch(lv):
            length = int(lv)
    if length is not None and 0 <= length and data_start + length <= len(obj_body):
        tail = obj_body[data_start + length : data_start + length + 16].lstrip(b"\r\n \t")
        if tail.startswith(b"endstream") or not tail:
            data_b = obj_body[data_start : data_start + length]
    if data_b is None:
        es = obj_body.rfind(b"endstream")
        data_b = obj_body[data_start : es if es >= data_start else len(obj_body)]
        if data_b.endswith(b"\r\n"):
            data_b = data_b[:-2]
        elif data_b.endswith((b"\n", b"\r")):
            data_b = data_b[:-1]

    filters = _filters_of(dict_b)
    parms = _decode_parms_of(dict_b, len(filters), objs)
    for f_idx, flt in enumerate(filters):
        if flt in (b"FlateDecode", b"Fl"):
            data_b = _inflate_bounded(data_b, max_out=max_out, budget=budget)
            if data_b is not None and parms[f_idx]:
                data_b = _apply_decode_parms(data_b, parms[f_idx])
        elif flt in (b"ASCII85Decode", b"A85"):
            try:
                body = _WHITESPACE_BYTES_RE.sub(b"", data_b)
                if body.startswith(b"<~"):
                    body = body[2:]
                data_b = base64.a85decode(body.split(b"~>")[0])
            except (ValueError, binascii.Error):
                data_b = None
        elif flt in (b"ASCIIHexDecode", b"AHx"):
            clean = _NON_HEX_BYTES_RE.sub(b"", data_b.split(b">")[0])
            data_b = bytes.fromhex((clean + b"0").decode() if len(clean) % 2 else clean.decode())
        else:
            return dict_b, None  # image codecs and other filters are not text content
        if data_b is None:
            return dict_b, None
    return dict_b, data_b


def _expand_object_streams(objs: Dict[int, bytes], budget: InflateBudget) -> None:
    """Add objects stored inside /Type /ObjStm compressed object streams (direct definitions win)."""
    for body in list(objs.values()):
        if b"/ObjStm" not in body[:4096]:
            continue
        dict_b, data = _extract_stream_from_obj(body, budget=budget, objs=objs)
        if not data or not _OBJSTM_TYPE_RE.search(dict_b):
            continue
        n_v, first_v = _dict_value(dict_b, b"N"), _dict_value(dict_b, b"First")
        if not (n_v and first_v and n_v.isdigit() and first_v.isdigit()):
            continue
        count, first = int(n_v), int(first_v)
        nums = [int(x) for x in _INT_AT_RE.findall(data[:first])][: 2 * count]
        pairs = list(zip(nums[0::2], nums[1::2]))
        for idx, (onum, off) in enumerate(pairs):
            start = first + off
            end = first + pairs[idx + 1][1] if idx + 1 < len(pairs) else len(data)
            if 0 <= start <= end <= len(data):
                objs.setdefault(onum, data[start:end])


# ---------------------------------------------------------------------------
# Linear content-stream lexer and text-positioning interpreter
# ---------------------------------------------------------------------------

_WS_BYTES = frozenset(b" \t\r\n\x0c\x00")
_REGULAR_RUN_RE = re.compile(rb"[^ \t\r\n\x0c\x00()<>\[\]{}/%]+")
_STRING_SPECIAL_RE = re.compile(rb"[()\\]")
_INLINE_IMAGE_END_RE = re.compile(rb"\sEI(?=[\s]|$)")
_EOL_RE = re.compile(rb"[\r\n]")
_MAX_OPERANDS = 8192

Matrix = Tuple[float, float, float, float, float, float]
_IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _mat_mul(m1: Matrix, m2: Matrix) -> Matrix:
    """m1 x m2 in PDF row-vector convention (apply m1 first, then m2)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return (
        a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
        c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
        e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2,
    )


def _apply(m: Matrix, x: float, y: float) -> Tuple[float, float]:
    return x * m[0] + y * m[2] + m[4], x * m[1] + y * m[3] + m[5]


def _lex_content(data: bytes) -> Iterator[Tuple[str, List[Any]]]:
    """
    Tokenize a content stream into (operator, operands) in a single forward pass.
    Literal strings honour nesting and backslash escapes, hex strings, arrays, dictionaries, comments and
    inline-image data are skipped structurally, so keywords such as `ET` inside strings never split blocks.
    Strings are yielded as ("lit", unescaped_bytes) / ("hex", bytes); names as ("name", str).
    """
    operands: List[Any] = []
    stack: List[List[Any]] = []
    overflow = 0  # open [ / << beyond MAX_LEX_DEPTH: tracked only as a count, never allocated
    group_items = 0  # operands collected for the current operator across all nested lists
    n = len(data)
    i = 0

    def push(val: Any) -> None:
        nonlocal group_items
        if overflow or group_items >= MAX_LEX_GROUP_ITEMS:
            return
        if stack:
            if len(stack[-1]) < _MAX_OPERANDS:
                stack[-1].append(val)
                group_items += 1
        elif len(operands) < _MAX_OPERANDS:
            operands.append(val)
            group_items += 1

    def open_group() -> None:
        nonlocal overflow
        if overflow or len(stack) >= MAX_LEX_DEPTH or group_items >= MAX_LEX_GROUP_ITEMS:
            overflow += 1
        else:
            stack.append([])

    def close_group(is_dict: bool) -> None:
        nonlocal overflow
        if overflow:
            overflow -= 1
        elif stack:
            arr = stack.pop()
            push(None if is_dict else arr)

    while i < n:
        c = data[i]
        if c in _WS_BYTES:
            i += 1
        elif c == 0x25:  # % comment
            m = _EOL_RE.search(data, i)
            i = m.end() if m else n
        elif c == 0x28:  # ( literal string )
            depth = 1
            j = i + 1
            while depth:
                m = _STRING_SPECIAL_RE.search(data, j)
                if not m:
                    j = n
                    break
                ch = data[m.start()]
                if ch == 0x5C:
                    j = m.start() + 2
                    continue
                depth += 1 if ch == 0x28 else -1
                j = m.end()
            body = data[i + 1 : (j - 1) if depth == 0 else n]
            push(("lit", _unescape_pdf_literal(body)))
            i = j
        elif c == 0x3C:  # < hex > or <<
            if data.startswith(b"<<", i):
                open_group()
                i += 2
            else:
                j = data.find(b">", i + 1)
                j = n if j < 0 else j
                clean = _NON_HEX_BYTES_RE.sub(b"", data[i + 1 : j])
                if len(clean) % 2:
                    clean += b"0"
                push(("hex", bytes.fromhex(clean.decode("ascii"))))
                i = j + 1
        elif c == 0x3E:  # >>
            if data.startswith(b">>", i):
                close_group(True)
                i += 2
            else:
                i += 1
        elif c == 0x5B:  # [
            open_group()
            i += 1
        elif c == 0x5D:  # ]
            close_group(False)
            i += 1
        elif c in (0x7B, 0x7D, 0x29):  # { } stray )
            i += 1
        elif c == 0x2F:  # /Name
            m = _REGULAR_RUN_RE.match(data, i + 1)
            name = m.group().decode("latin-1") if m else ""
            if "#" in name:
                name = _NAME_ESCAPE_RE.sub(lambda mm: chr(int(mm.group(1), 16)), name)
            push(("name", name))
            i = m.end() if m else i + 1
        else:
            m = _REGULAR_RUN_RE.match(data, i)
            if not m:
                i += 1
                continue
            tok = m.group()
            i = m.end()
            if _NUMBER_RE.fullmatch(tok):
                try:
                    push(float(tok))
                except ValueError:
                    push(0.0)
            elif tok in (b"true", b"false", b"null"):
                push(tok == b"true")
            else:
                op = tok.decode("latin-1")
                if stack or overflow:  # operator inside an unterminated array/dict: discard the broken group
                    stack.clear()
                    overflow = 0
                group_items = 0
                if op == "ID":  # inline image data: skip to the EI keyword (reported as an "ID" operator)
                    em = _INLINE_IMAGE_END_RE.search(data, i)
                    i = em.end() if em else n
                    operands = []
                    yield "ID", []
                    continue
                yield op, operands
                operands = []


def _num(v: Any, default: float = 0.0) -> float:
    return float(v) if isinstance(v, float) else default


@dataclass
class _Resources:
    fonts: Dict[str, _FontInfo] = field(default_factory=dict)
    forms: Dict[str, int] = field(default_factory=dict)


@dataclass
class _RawTok:
    x0: float
    y: float
    x1: float
    text: str
    size: float  # rendered font size (device units) for gap heuristics


class _TextInterpreter:
    """Tracks CTM (q/Q/cm), text/line matrices (BT/Tm/Td/TD/T*), Tf/Tc/Tw/Tz/TL/Ts and glyph advances."""

    def __init__(
        self,
        page_num: int,
        max_bytes: int,
        form_loader: Optional[Callable[[int], Optional[Tuple[bytes, Matrix, _Resources]]]] = None,
        work: Optional[_WorkBudget] = None,
    ) -> None:
        self.page_num = page_num
        self.budget_bytes = max_bytes
        self.form_loader = form_loader
        self.work = work
        self.out: List[_RawTok] = []
        self.truncated = False
        self.saw_inline_image = False

    def run(self, data: bytes, resources: _Resources, ctm: Matrix = _IDENTITY, depth: int = 0) -> None:
        if self.budget_bytes <= 0:
            self.truncated = True
            return
        if len(data) > self.budget_bytes:
            data = data[: self.budget_bytes]
            self.truncated = True
        if self.work is not None:
            # Charged on every run, so a cached Form XObject drawn on many pages is paid for each time
            allowed = self.work.take(len(data))
            if allowed < len(data):
                data = data[:allowed]
                self.truncated = True
            if not data:
                return
        self.budget_bytes -= len(data)

        gs_stack: List[Tuple[Matrix, Tuple[Any, ...]]] = []
        # text state: font, size, Tc, Tw, Th, TL, Ts
        font: _FontInfo = _FontInfo()
        fs = 0.0
        tc = tw = ts = 0.0
        th = 1.0
        tl = 0.0
        tm: Matrix = _IDENTITY
        tlm: Matrix = _IDENTITY
        fonts = resources.fonts
        work = self.work
        n_ops = 0

        for op, ops in _lex_content(data):
            n_ops += 1
            if work is not None and not n_ops & 0x3FFF and work.expired():
                self.truncated = True
                return
            if op == "Tj" or op == "'" or op == '"':
                if op == "'":
                    tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -tl), tlm)
                    tm = tlm
                elif op == '"':
                    if len(ops) >= 3:
                        tw, tc = _num(ops[0]), _num(ops[1])
                    tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -tl), tlm)
                    tm = tlm
                s = ops[-1] if ops else None
                if isinstance(s, tuple) and s[0] in ("lit", "hex"):
                    tm = self._show([s], font, fs, tc, tw, th, ts, tm, ctm)
            elif op == "TJ":
                arr = ops[-1] if ops and isinstance(ops[-1], list) else []
                tm = self._show(arr, font, fs, tc, tw, th, ts, tm, ctm)
            elif op == "Td" or op == "TD":
                if len(ops) >= 2:
                    tx, ty = _num(ops[-2]), _num(ops[-1])
                    if op == "TD":
                        tl = -ty
                    tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, tx, ty), tlm)
                    tm = tlm
            elif op == "Tm":
                if len(ops) >= 6 and all(isinstance(v, float) for v in ops[-6:]):
                    tlm = tm = tuple(ops[-6:])  # type: ignore[assignment]
            elif op == "T*":
                tlm = _mat_mul((1.0, 0.0, 0.0, 1.0, 0.0, -tl), tlm)
                tm = tlm
            elif op == "BT":
                tm = tlm = _IDENTITY
            elif op == "Tf":
                if len(ops) >= 2 and isinstance(ops[-2], tuple) and ops[-2][0] == "name":
                    font = fonts.get(ops[-2][1]) or _FontInfo()
                    fs = _num(ops[-1])
            elif op == "Tc":
                tc = _num(ops[-1]) if ops else tc
            elif op == "Tw":
                tw = _num(ops[-1]) if ops else tw
            elif op == "Tz":
                th = (_num(ops[-1], 100.0) / 100.0) if ops else th
            elif op == "TL":
                tl = _num(ops[-1]) if ops else tl
            elif op == "Ts":
                ts = _num(ops[-1]) if ops else ts
            elif op == "cm":
                if len(ops) >= 6 and all(isinstance(v, float) for v in ops[-6:]):
                    ctm = _mat_mul(tuple(ops[-6:]), ctm)  # type: ignore[arg-type]
            elif op == "q":
                if len(gs_stack) < 256:
                    gs_stack.append((ctm, (font, fs, tc, tw, th, tl, ts)))
            elif op == "Q":
                if gs_stack:
                    ctm, (font, fs, tc, tw, th, tl, ts) = gs_stack.pop()
            elif op == "ID":
                self.saw_inline_image = True
            elif op == "Do":
                if (self.form_loader and depth < MAX_FORM_DEPTH and ops and isinstance(ops[-1], tuple)
                        and ops[-1][0] == "name"):
                    oid = resources.forms.get(ops[-1][1])
                    form = self.form_loader(oid) if oid is not None else None
                    if form is not None:
                        f_data, f_matrix, f_res = form
                        self.run(f_data, f_res, _mat_mul(f_matrix, ctm), depth + 1)

    def _show(
        self,
        items: Sequence[Any],
        font: _FontInfo,
        fs: float,
        tc: float,
        tw: float,
        th: float,
        ts: float,
        tm: Matrix,
        ctm: Matrix,
    ) -> Matrix:
        """Show strings (and TJ displacements); emit one token per contiguous run, return the advanced Tm."""
        parts: List[str] = []
        run_start: Optional[float] = None  # text-space x where the current run starts
        tx = 0.0  # text-space advance from `tm` origin

        def flush(end_tx: float) -> None:
            nonlocal parts, run_start
            text = "".join(parts).strip()
            if text and run_start is not None:
                trm = _mat_mul(tm, ctm)
                x0, y0 = _apply(trm, run_start, ts)
                x1, _ = _apply(trm, end_tx, ts)
                size = abs(fs) * (abs(trm[3]) or abs(trm[0]) or 1.0)
                self.out.append(_RawTok(min(x0, x1), y0, max(x0, x1), text, size))
            parts = []
            run_start = None

        for item in items:
            if isinstance(item, float):
                em = -item / 1000.0
                if abs(em) >= TJ_BREAK_EM:
                    flush(tx)
                elif em >= TJ_SPACE_EM and parts and not parts[-1].endswith(" "):
                    parts.append(" ")
                tx += em * fs * th
            elif isinstance(item, tuple) and item[0] in ("lit", "hex"):
                raw = item[1]
                codes = font.codes(raw)
                text = font.decode(raw, hex_string=item[0] == "hex")
                if "  " in text and len(text) == len(codes):
                    # PDF-28: report writers often draw a whole table line as one space-padded string. A run of
                    # 2+ spaces is a column gap: split there, advancing by the glyph widths of the spaces.
                    k, n = 0, len(codes)
                    while k < n:
                        ch = text[k]
                        if ch == " " and k + 1 < n and text[k + 1] == " ":
                            flush(tx)
                            while k < n and text[k] == " ":
                                gw, sp = font.advance([codes[k]])
                                tx += ((gw / 1000.0) * fs + tc + tw * sp) * th
                                k += 1
                            continue
                        if run_start is None:
                            run_start = tx
                        parts.append(ch)
                        gw, sp = font.advance([codes[k]])
                        tx += ((gw / 1000.0) * fs + tc + tw * sp) * th
                        k += 1
                    continue
                if run_start is None:
                    run_start = tx
                parts.append(text)
                glyph_w, spaces = font.advance(codes)
                tx += ((glyph_w / 1000.0) * fs + tc * len(codes) + tw * spaces) * th
        flush(tx)
        return _mat_mul((1.0, 0.0, 0.0, 1.0, tx, 0.0), tm)


def _merge_fragments(raw: List[_RawTok], page_num: int) -> List[PdfTextToken]:
    """Join glyph/word fragments on one baseline (gap within ~0.5 em) into cell tokens; drop fake-bold overprints."""
    ordered = sorted(raw, key=lambda t: (-round(t.y, 1), t.x0))
    merged: List[_RawTok] = []
    for t in ordered:
        if merged:
            prev = merged[-1]
            size = max(prev.size, t.size, 1.0)
            if abs(prev.y - t.y) <= 0.15 * size:
                gap = t.x0 - prev.x1
                if prev.text == t.text and abs(t.x0 - prev.x0) <= 0.2 * size:
                    continue  # duplicate overprint
                if -0.1 * size <= gap <= 0.08 * size:
                    merged[-1] = _RawTok(prev.x0, prev.y, max(prev.x1, t.x1), prev.text + t.text, size)
                    continue
                if 0.08 * size < gap <= 0.5 * size:
                    merged[-1] = _RawTok(prev.x0, prev.y, max(prev.x1, t.x1), prev.text + " " + t.text, size)
                    continue
        merged.append(t)
    return [PdfTextToken(page_num, round(t.x0, 2), round(t.y, 2), t.text, round(t.x1, 2)) for t in merged]


def _extract_content_tokens(
    stream_bytes: bytes,
    page_num: int,
    font_cmaps: Optional[Dict[str, Dict[int, str]]] = None,
    *,
    fonts: Optional[Dict[str, _FontInfo]] = None,
    max_bytes: int = MAX_PAGE_CONTENT_BYTES,
) -> List[PdfTextToken]:
    """Interpret one content stream into positioned tokens (one token per show run, no fragment merging)."""
    font_map: Dict[str, _FontInfo] = {k: _FontInfo.from_cmap(v) for k, v in (font_cmaps or {}).items()}
    font_map.update(fonts or {})
    interp = _TextInterpreter(page_num, max_bytes)
    interp.run(stream_bytes, _Resources(fonts=font_map))
    return [PdfTextToken(page_num, round(t.x0, 2), round(t.y, 2), t.text, round(t.x1, 2)) for t in interp.out]


# ---------------------------------------------------------------------------
# Pure-Python document walker (page tree, inherited resources, fonts, Form XObjects)
# ---------------------------------------------------------------------------

@dataclass
class _PageInfo:
    oid: int
    body: bytes
    resources: bytes
    size: Tuple[float, float]


@dataclass
class _PyExtraction:
    tokens_by_page: List[List[PdfTextToken]]
    page_sizes: List[Tuple[float, float]]
    page_has_scan: List[bool]
    page_tree_ok: bool
    object_count: int
    warnings: List[str] = field(default_factory=list)
    # Per page: an image XObject in its resources (nested Forms included) or an inline image in its content.
    page_has_image: List[bool] = field(default_factory=list)


class _PdfDocument:
    def __init__(self, pdf_bytes: bytes, budget: InflateBudget) -> None:
        self.data = pdf_bytes
        self.budget = budget
        self.objs = _parse_pdf_objects(pdf_bytes)
        _expand_object_streams(self.objs, budget)
        # Objects the cross-reference streams place in object streams that could not be recovered (PDF-06)
        self.missing_compressed: List[int] = []
        if b"/XRef" in pdf_bytes:
            xref = _parse_xref_streams(self.objs, budget)
            self.missing_compressed = sorted(o for o, e in xref.items() if e[0] == 2 and o not in self.objs)
        self._fonts: Dict[int, _FontInfo] = {}
        self._forms: Dict[int, Optional[Tuple[bytes, Matrix, _Resources]]] = {}
        self._res_cache: Dict[bytes, _Resources] = {}

    def resolve(self, val: Optional[bytes]) -> bytes:
        seen = 0
        while val is not None and seen < 8:
            ref = _REF_AT_RE.fullmatch(val.strip())
            if not ref:
                return val
            val = self.objs.get(int(ref.group(1)))
            seen += 1
        return val or b""

    def stream(self, oid: int, max_out: int = MAX_STREAM_INFLATE_BYTES) -> Optional[bytes]:
        body = self.objs.get(oid)
        if not body:
            return None
        return _extract_stream_from_obj(body, budget=self.budget, objs=self.objs, max_out=max_out)[1]

    # -- page tree -------------------------------------------------------
    def pages(self) -> List[_PageInfo]:
        root_m = None
        for root_m in _ROOT_REF_RE.finditer(self.data):
            pass
        out: List[_PageInfo] = []
        if root_m is not None:
            catalog = self.objs.get(int(root_m.group(1)), b"")
            pages_ref = _dict_value(catalog, b"Pages")
            ref = _REF_AT_RE.match(pages_ref or b"")
            if ref:
                self._walk(int(ref.group(1)), b"", None, out, set(), 0)
        return out

    def _walk(self, oid: int, inh_res: bytes, inh_box: Optional[Tuple[float, float]],
              out: List[_PageInfo], seen: set, depth: int) -> None:
        if oid in seen or depth > 64 or len(out) >= MAX_PAGES:
            return
        seen.add(oid)
        body = self.objs.get(oid)
        if body is None:
            return
        res_v = _dict_value(body, b"Resources")
        res = self.resolve(res_v) if res_v else inh_res
        box = self._mediabox(body) or inh_box
        kids_v = _dict_value(body, b"Kids")
        if kids_v is not None and (_TYPE_PAGES_RE.search(body) or not _TYPE_PAGE_RE.search(body)):
            for km in _REF_RE.finditer(self.resolve(kids_v)):
                self._walk(int(km.group(1)), res, box, out, seen, depth + 1)
        elif _TYPE_PAGE_RE.search(body) or _dict_value(body, b"Contents") is not None:
            out.append(_PageInfo(oid, body, res, box or (595.0, 842.0)))

    def _mediabox(self, body: bytes) -> Optional[Tuple[float, float]]:
        val = self.resolve(_dict_value(body, b"MediaBox")) if _dict_value(body, b"MediaBox") else None
        if not val:
            return None
        nums = [float(x) for x in _NUMBER_RE.findall(val)[:4]]
        if len(nums) < 4:
            return None
        w, h = abs(nums[2] - nums[0]), abs(nums[3] - nums[1])
        return (w, h) if w > 10 and h > 10 else None

    def page_contents(self, page: _PageInfo) -> bytes:
        val = _dict_value(page.body, b"Contents")
        if not val:
            return b""
        ids: List[int] = []
        ref = _REF_AT_RE.fullmatch(val.strip())
        if ref:
            target = self.objs.get(int(ref.group(1)), b"")
            if target.lstrip().startswith(b"["):  # indirect array of streams
                ids = [int(r.group(1)) for r in _REF_RE.finditer(target.split(b"stream")[0])]
            else:
                ids = [int(ref.group(1))]
        else:
            ids = [int(r.group(1)) for r in _REF_RE.finditer(val)]
        chunks: List[bytes] = []
        remaining = MAX_PAGE_CONTENT_BYTES + 1
        for cid in ids:
            data = self.stream(cid, max_out=min(MAX_STREAM_INFLATE_BYTES, remaining))
            if data:
                chunks.append(data)
                remaining -= len(data)
                if remaining <= 0:
                    break
        return b"\n".join(chunks)

    # -- resources -------------------------------------------------------
    def resources(self, res_body: bytes) -> _Resources:
        cached = self._res_cache.get(res_body)
        if cached is not None:
            return cached
        res = _Resources()
        font_dict = self.resolve(_dict_value(res_body, b"Font")) if res_body else b""
        for m in _NAMED_REF_RE.finditer(font_dict):
            res.fonts[m.group(1).decode("latin-1")] = self.font(int(m.group(2)))
        xo_dict = self.resolve(_dict_value(res_body, b"XObject")) if res_body else b""
        for m in _NAMED_REF_RE.finditer(xo_dict):
            res.forms[m.group(1).decode("latin-1")] = int(m.group(2))
        self._res_cache[res_body] = res
        return res

    def page_has_scan_image(self, res_body: bytes) -> bool:
        xo_dict = self.resolve(_dict_value(res_body, b"XObject")) if res_body else b""
        for m in _REF_RE.finditer(xo_dict):
            body = self.objs.get(int(m.group(1)), b"")
            head = body[:2048]
            if not _SUBTYPE_IMAGE_RE.search(head):
                continue
            w = _dict_value(head, b"Width")
            h = _dict_value(head, b"Height")
            if w and h and w.isdigit() and h.isdigit():
                wi, hi = int(w), int(h)
                if max(wi, hi) >= 800 and min(wi, hi) >= 400:
                    return True
        return False

    def page_has_image(self, res_body: bytes, depth: int = 0, seen: Optional[set] = None) -> bool:
        """Any image XObject in these resources, including inside nested Form XObjects (PDF-25)."""
        if not res_body or depth > MAX_FORM_DEPTH:
            return False
        seen = set() if seen is None else seen
        xo_dict = self.resolve(_dict_value(res_body, b"XObject"))
        for m in _REF_RE.finditer(xo_dict):
            oid = int(m.group(1))
            if oid in seen:
                continue
            seen.add(oid)
            head = self.objs.get(oid, b"")[:4096]
            if _SUBTYPE_IMAGE_RE.search(head):
                return True
            if _SUBTYPE_FORM_RE.search(head):
                res_v = _dict_value(head, b"Resources")
                if res_v and self.page_has_image(self.resolve(res_v), depth + 1, seen):
                    return True
        return False

    def font(self, oid: int) -> _FontInfo:
        cached = self._fonts.get(oid)
        if cached is not None:
            return cached
        body = self.objs.get(oid, b"")
        info = _FontInfo()
        tu = _dict_value(body, b"ToUnicode")
        tu_ref = _REF_AT_RE.match(tu or b"")
        if tu_ref:
            cmap_stream = self.stream(int(tu_ref.group(1)))
            if cmap_stream:
                info.cmap = _parse_tounicode_cmap(cmap_stream) or None
        if _TYPE0_RE.search(body):
            info.two_byte = True
            info.default_width = 1000.0
            desc = self.resolve(_dict_value(body, b"DescendantFonts"))
            dref = _REF_RE.search(desc)
            dbody = self.objs.get(int(dref.group(1)), b"") if dref else desc
            dw = _dict_value(dbody, b"DW")
            if dw and _NUMBER_RE.fullmatch(dw):
                info.default_width = float(dw)
            w_arr = self.resolve(_dict_value(dbody, b"W")) if _dict_value(dbody, b"W") else b""
            info.widths = _parse_cid_widths(w_arr)
        else:
            if info.cmap and max(info.cmap.keys(), default=0) > 255:
                info.two_byte = True
            fc = _dict_value(body, b"FirstChar")
            wv = _dict_value(body, b"Widths")
            if fc and wv and _INT_AT_RE.fullmatch(fc):
                arr = self.resolve(wv)
                vals = [float(x) for x in _NUMBER_RE.findall(arr)]
                first = int(fc)
                info.widths = {first + k: v for k, v in enumerate(vals)}
                nz = [v for v in vals if v > 0]
                if nz:
                    info.default_width = sum(nz) / len(nz)
            elif b"Courier" in body:
                info.default_width = 600.0
        self._fonts[oid] = info
        return info

    def form(self, oid: int) -> Optional[Tuple[bytes, Matrix, _Resources]]:
        if oid in self._forms:
            return self._forms[oid]
        result: Optional[Tuple[bytes, Matrix, _Resources]] = None
        body = self.objs.get(oid, b"")
        if body and _SUBTYPE_FORM_RE.search(body[:4096]):
            dict_b, data = _extract_stream_from_obj(body, budget=self.budget, objs=self.objs,
                                                    max_out=MAX_PAGE_CONTENT_BYTES)
            if data:
                mv = _dict_value(dict_b, b"Matrix")
                nums = [float(x) for x in _NUMBER_RE.findall(mv or b"")]
                matrix: Matrix = tuple(nums[:6]) if len(nums) >= 6 else _IDENTITY  # type: ignore[assignment]
                res_v = _dict_value(dict_b, b"Resources")
                res = self.resources(self.resolve(res_v)) if res_v else _Resources()
                result = (data, matrix, res)
        self._forms[oid] = result
        return result


def _parse_cid_widths(w_arr: bytes) -> Dict[int, float]:
    """Parse a CIDFont /W array: `c [w1 w2 ...]` and `c_first c_last w` forms."""
    widths: Dict[int, float] = {}
    pending: List[float] = []
    in_list: Optional[List[float]] = None
    left = MAX_CID_WIDTH_ENTRIES  # total CIDs expanded across the whole array, not per range
    for m in _W_ITEM_RE.finditer(w_arr[1:-1] if w_arr.startswith(b"[") else w_arr):
        if left <= 0:
            break
        tok = m.group()
        if tok == b"[":
            in_list = []
        elif tok == b"]":
            if in_list is not None and pending:
                start = int(pending[-1])
                for k, v in enumerate(in_list[:left]):
                    widths[start + k] = v
                left -= min(len(in_list), left)
            pending, in_list = [], None
        elif in_list is not None:
            if len(in_list) < left:
                in_list.append(float(tok))
        else:
            pending.append(float(tok))
            if len(pending) == 3:
                c0, c1, wv = int(pending[0]), int(pending[1]), pending[2]
                stop = min(c1, c0 + left - 1)
                for c in range(c0, stop + 1):
                    widths[c] = wv
                left -= max(0, stop - c0 + 1)
                pending = []
    return widths


_INLINE_IMAGE_RE = re.compile(rb"(?:^|[\s\]>)])BI\s[\s\S]{0,4096}?\sID\s")


def _extract_pure_python(
    pdf_bytes: bytes,
    budget: Optional[InflateBudget] = None,
    with_text: bool = True,
    work: Optional[_WorkBudget] = None,
    inline_image_pages: Optional[Sequence[int]] = None,
) -> _PyExtraction:
    """
    Decode the page tree, fonts and content streams in pure Python into per-page positioned tokens.
    `with_text=False` only maps pages, sizes and scan images (dictionaries of an encrypted PDF are
    plaintext; its content streams are not); the contents of `inline_image_pages` (1-based) are then still
    scanned for inline images. `work` bounds interpreted bytes and wall clock for the whole document.
    """
    budget = budget or InflateBudget()
    work = work or _WorkBudget()
    inline_check = set(inline_image_pages or ())
    doc = _PdfDocument(pdf_bytes, budget)
    warnings: List[str] = []
    pages = doc.pages()
    page_tree_ok = bool(pages)
    if not pages:  # broken page tree: fall back to /Type /Page objects in object order
        pages = [
            _PageInfo(oid, body, doc.resolve(_dict_value(body, b"Resources")) if _dict_value(body, b"Resources") else b"",
                      doc._mediabox(body) or (595.0, 842.0))
            for oid, body in sorted(doc.objs.items())
            if _TYPE_PAGE_RE.search(body)
        ][:MAX_PAGES]

    tokens_by_page: List[List[PdfTextToken]] = []
    sizes: List[Tuple[float, float]] = []
    has_scan: List[bool] = []
    has_image: List[bool] = []
    for p_idx, page in enumerate(pages, start=1):
        interp = _TextInterpreter(p_idx, MAX_PAGE_CONTENT_BYTES, form_loader=doc.form, work=work)
        page_image = doc.page_has_image(page.resources)
        if with_text and not (work.exhausted or work.timed_out):
            content = doc.page_contents(page)
            if content:
                interp.run(content, doc.resources(page.resources))
            page_image = page_image or interp.saw_inline_image
        elif p_idx in inline_check and not page_image and not work.expired():
            content = doc.page_contents(page)
            page_image = bool(content) and bool(_INLINE_IMAGE_RE.search(content))
        if interp.truncated and not (work.exhausted or work.timed_out):
            warnings.append(f"Page {p_idx}: content stream exceeds {MAX_PAGE_CONTENT_BYTES >> 20} MB; text truncated.")
        tokens_by_page.append(_merge_fragments(interp.out, p_idx))
        sizes.append(page.size)
        has_scan.append(doc.page_has_scan_image(page.resources))
        has_image.append(page_image)
    if with_text and work.exhausted:
        warnings.append(
            f"PDF content interpretation stopped after {MAX_DOC_CONTENT_BYTES >> 20} MB for the document; "
            "text of later pages is missing."
        )
    if with_text and work.timed_out:
        warnings.append("PDF text extraction time limit reached; text of later pages is missing.")

    if not pages and with_text:  # no page objects at all: interpret every text-bearing stream in object order
        for oid in sorted(doc.objs):
            body = doc.objs[oid]
            if _SUBTYPE_IMAGE_RE.search(body[:2048]) or b"/ToUnicode" in body[:2048] or b"stream" not in body:
                continue
            if work.exhausted or work.timed_out:
                break
            data = doc.stream(oid)
            if data and b"BT" in data:
                p_idx = len(tokens_by_page) + 1
                interp = _TextInterpreter(p_idx, MAX_PAGE_CONTENT_BYTES, work=work)
                interp.run(data, _Resources())
                toks = _merge_fragments(interp.out, p_idx)
                if toks:
                    tokens_by_page.append(toks)
                    sizes.append((595.0, 842.0))
                    has_scan.append(False)
                    has_image.append(interp.saw_inline_image)
    if budget.exhausted:
        warnings.append("PDF decompression limit reached; some streams were not decoded.")
    if doc.missing_compressed and with_text:
        warnings.append(
            f"{len(doc.missing_compressed)} compressed object(s) listed in the cross-reference stream could not "
            "be decoded; some text may be missing."
        )
    return _PyExtraction(tokens_by_page, sizes, has_scan, page_tree_ok, len(doc.objs), warnings, has_image)


def _extract_tokens_pure_python(pdf_bytes: bytes) -> Tuple[List[PdfTextToken], List[str], List[Tuple[float, float]]]:
    """Backward-compatible wrapper: (tokens, pages_text, page_sizes)."""
    py = _extract_pure_python(pdf_bytes)
    tokens = [t for page in py.tokens_by_page for t in page]
    pages_text = [" ".join(t.text for t in page) for page in py.tokens_by_page]
    return tokens, pages_text, (py.page_sizes or [(595.0, 842.0)])


# ---------------------------------------------------------------------------
# Poppler `pdftotext -bbox-layout`
# ---------------------------------------------------------------------------

_XMLNS_RE = re.compile(r'\sxmlns="[^"]+"')
_POPPLER_CORRUPT_HINTS = ("couldn't read xref", "couldn't find trailer", "may not be a pdf", "pdf file is damaged")


@dataclass
class _PopplerResult:
    status: str  # "ok" | "missing" | "password" | "corrupt" | "error" | "timeout"
    tokens: List[PdfTextToken] = field(default_factory=list)
    pages_text: List[str] = field(default_factory=list)
    page_sizes: List[Tuple[float, float]] = field(default_factory=list)


def _run_pdftotext(
    pdf_bytes: bytes, password: Optional[str] = None, timeout: float = POPPLER_TIMEOUT_S
) -> _PopplerResult:
    """
    Run `pdftotext -bbox-layout` with the PDF on stdin (no plaintext temp copy) and parse word boxes.
    Words on a line are grouped into cell tokens when the gap is under ~one word height (max 9 pt);
    overlapping runs (e.g. a long date drawn over the narration column) are kept apart.
    """
    bin_path = _poppler_bin("pdftotext")
    if not bin_path:
        return _PopplerResult("missing")
    cmd = [bin_path, "-bbox-layout", "-enc", "UTF-8"]
    if password:
        cmd.extend(["-upw", password])  # Poppler has no non-argv password channel
    cmd.extend(["-", "-"])
    try:
        proc = subprocess.run(cmd, input=pdf_bytes, capture_output=True, timeout=max(1.0, timeout))
    except subprocess.TimeoutExpired:
        return _PopplerResult("timeout")
    except OSError:
        return _PopplerResult("missing")
    stderr = proc.stderr.decode("utf-8", errors="replace").lower()
    if proc.returncode != 0:
        if "password" in stderr:
            return _PopplerResult("password")
        if any(h in stderr for h in _POPPLER_CORRUPT_HINTS):
            return _PopplerResult("corrupt")
        return _PopplerResult("error")

    xml_text = _XMLNS_RE.sub("", proc.stdout.decode("utf-8", errors="replace"))
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return _PopplerResult("error")

    res = _PopplerResult("ok")
    for p_idx, page_el in enumerate(root.iter("page"), start=1):
        pw = float(page_el.attrib.get("width", 595.0))
        ph = float(page_el.attrib.get("height", 842.0))
        res.page_sizes.append((pw, ph))
        page_toks: List[PdfTextToken] = []
        for line_el in page_el.iter("line"):
            words = []
            for w_el in line_el.findall("./word"):
                txt = (w_el.text or "").strip()
                if txt:
                    words.append((float(w_el.attrib["xMin"]), float(w_el.attrib["xMax"]),
                                  float(w_el.attrib["yMin"]), float(w_el.attrib["yMax"]), txt))
            if not words:
                continue
            cx0, cx1, cy0, cy1, first = words[0]
            parts = [first]
            for wx0, wx1, wy0, wy1, txt in words[1:]:
                height = max(cy1 - cy0, wy1 - wy0, 1.0)
                gap = wx0 - cx1
                same_row = abs((wy0 + wy1) / 2.0 - (cy0 + cy1) / 2.0) <= 4.0
                if same_row and -0.5 <= gap <= min(9.0, max(3.0, height)):
                    parts.append(txt)
                    cx1 = max(cx1, wx1)
                    continue
                page_toks.append(PdfTextToken(p_idx, round(cx0, 2), round(ph - (cy0 + cy1) / 2.0, 2),
                                              " ".join(parts), round(cx1, 2)))
                cx0, cx1, cy0, cy1, parts = wx0, wx1, wy0, wy1, [txt]
            page_toks.append(PdfTextToken(p_idx, round(cx0, 2), round(ph - (cy0 + cy1) / 2.0, 2),
                                          " ".join(parts), round(cx1, 2)))
        res.tokens.extend(page_toks)
        res.pages_text.append(" ".join(t.text for t in page_toks))
    return res


def _extract_tokens_pdftotext(
    pdf_bytes: bytes, password: Optional[str] = None
) -> Tuple[List[PdfTextToken], List[str], List[Tuple[float, float]]]:
    """Backward-compatible wrapper around `_run_pdftotext`: (tokens, pages_text, page_sizes)."""
    res = _run_pdftotext(pdf_bytes, password=password)
    return res.tokens, res.pages_text, res.page_sizes


# ---------------------------------------------------------------------------
# Tolerant transaction-date predicate (PDF-03)
# ---------------------------------------------------------------------------

_MON = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
        r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
_TIME_SUFFIX = r"(?:(?:[\sT]+|,\s*)\d{1,2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:\s*[AaPp]\.?[Mm]\.?)?)?"
_DATE_CORE = (
    r"(?:(?P<d1>\d{1,2})[./-](?P<m1>\d{1,2})[./-](?P<y1>\d{4}|\d{2})"
    r"|(?P<y2>\d{4})[./-](?P<m2>\d{1,2})[./-](?P<d2>\d{1,2})"
    r"|(?P<d3>\d{1,2})(?:[\s./-]+|(?=[A-Za-z]))" + _MON + r"\.?(?:[\s./,-]+|(?<=[A-Za-z.])(?=\d))(?P<y3>\d{4}|\d{2})"
    r"|" + _MON + r"\.?\s+(?P<d4>\d{1,2}),?\s+(?P<y4>\d{4}|\d{2}))"
)
_DATE_TOKEN_RE = re.compile(r"^" + _DATE_CORE + _TIME_SUFFIX + r"$", re.IGNORECASE)
_DATE_PREFIX_RE = re.compile(r"^" + _DATE_CORE + _TIME_SUFFIX + r"(?=\s|$)", re.IGNORECASE)
_DATE_ANYWHERE_RE = re.compile(r"(?<![\w/.-])" + _DATE_CORE + r"(?![\w/-])", re.IGNORECASE)


def _date_match_valid(m: "re.Match[str]") -> bool:
    g = m.groupdict()
    if g.get("d1") is not None:
        a, b = int(g["d1"]), int(g["m1"])
        return (1 <= a <= 31 and 1 <= b <= 12) or (1 <= a <= 12 and 1 <= b <= 31)
    if g.get("y2") is not None:
        return 1 <= int(g["m2"]) <= 12 and 1 <= int(g["d2"]) <= 31 and 1900 <= int(g["y2"]) <= 2199
    day = g.get("d3") if g.get("d3") is not None else g.get("d4")
    return day is not None and 1 <= int(day) <= 31


def is_date_token(text: str, allow_suffix: bool = False) -> bool:
    """
    True when `text` is a statement transaction date: DD/MM/YYYY (./- and 2-digit years), YYYY-MM-DD,
    DD Mon YYYY / DD-Mon-YY / Mon DD, YYYY, each optionally followed by a time (`10:22[:11] [AM]`, `T10:22:11`).
    With `allow_suffix`, trailing text after whitespace is accepted (OCR cells that glue date + narration).
    """
    s = text.strip()
    if not s or len(s) > (200 if allow_suffix else 40) or not s[0].isalnum():
        return False
    m = (_DATE_PREFIX_RE if allow_suffix else _DATE_TOKEN_RE).match(s)
    return bool(m) and _date_match_valid(m)  # type: ignore[arg-type]


def _date_spans(line: Sequence[PdfTextToken], max_x: Optional[float] = None) -> List[Tuple[float, float]]:
    """(x0, x1) of each date in a line, joining up to three adjacent tokens (`01` `Apr` `2025`)."""
    toks = sorted((t for t in line if max_x is None or t.x < max_x), key=lambda t: t.x)
    spans: List[Tuple[float, float]] = []
    i = 0
    while i < len(toks):
        found = 0
        for k in (1, 2, 3):
            if i + k > len(toks):
                break
            group = toks[i : i + k]
            if k > 1:
                prev, nxt = group[-2], group[-1]
                prev_end = prev.x_end if prev.x_end > prev.x else prev.x + len(prev.text) * 4.4
                if nxt.x - prev_end > 8.0 or abs(nxt.y - prev.y) > 4.0:
                    break
            if is_date_token(" ".join(t.text.strip() for t in group)):
                found = k
        if found:
            group = toks[i : i + found]
            last = group[-1]
            spans.append((group[0].x, last.x_end if last.x_end > last.x else last.x + len(last.text) * 4.4))
            i += found
        else:
            i += 1
    return spans


def _line_has_date(line: Sequence[PdfTextToken], max_x: Optional[float] = None) -> bool:
    return bool(_date_spans(line, max_x))


# ---------------------------------------------------------------------------
# Stacked Header Detection & Gutter-Aware Tabular Column Slicer
# ---------------------------------------------------------------------------

_HEADER_KEYWORDS = {
    "date", "txn", "tran", "narration", "particulars", "partlculars", "particuiars",
    "description", "remarks", "chq", "cheque", "ref", "debit", "debits",
    "withdrawal", "withdrawals", "withdrawl", "credit", "credits", "crodit",
    "deposit", "deposits", "balance", "balanoe", "amount", "dr", "cr", "value", "post",
}

_FOOTER_SUMMARY_HEADER_WORDS = {"ope", "clo", "count", "cumulative", "uncl", "clr", "mod"}

_RIGHT_ALIGNED_NUMERIC_RE = re.compile(r"^[\(\-\s₹]*\d[\d,]*(?:\.\d{1,2})?(?:\s*(?:CR|DR))?[\)\s]*$", re.IGNORECASE)
_DATE_CELL_RE = re.compile(r"^\d{2}[./-]\d{2}[./-]\d{2,4}$")
_WORDS_RE = re.compile(r"[a-z]+")
_HEADER_META_RE = re.compile(
    r"\b(?:account\s+open|statement\s+(?:from|period)|drawing\s+power|ifsc|micr|cif\s+no|customer\s+id|nominee)\b",
    re.IGNORECASE,
)
_ALPHA_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_OCR_ROW_DATE_RE = re.compile(
    r"^[FPT_*•\-~|7]?(?:\d{2}[./:-][\d:]{2}[./:-]\d{2,4}|\d{2}[-/.][A-Za-z]{3}[-/.]\d{2,4})(?:\s|$)"
)
_AMOUNT_2DP_RE = re.compile(r"\d+\.\d{2}")
_TFR_PREFIX_RE = re.compile(r"^(?:DEP|WDL|TRANSFER|DEBIT|CREDIT)\b", re.IGNORECASE)
_DESKEW_BAL_RE = re.compile(r"^\d[\d,]*\.\d{2}(?:[A-Za-z0-9]{1,3})?$")
_DEBIT_WORDS = frozenset({"debit", "debits", "withdrawal", "withdrawals", "withdrawl", "dr"})
_CREDIT_WORDS = frozenset({"credit", "credits", "crodit", "deposit", "deposits", "cr"})
_BALANCE_WORDS = frozenset({"balance", "balanoe"})
_MONEY_CELL_RE = re.compile(r"^(?:Cr|Dr)?[\d,]+\.\d{2}(?:\s*(?:CR|DR))?$", re.IGNORECASE)
_SKIP_SUMMARY_RE = re.compile(
    r"\b(?:BROUGHT\s+FORWARD|OPENING\s+BAL|CLOSING\s+BAL|CUMULATIVE|OPE\s+BAL|CLO\s+BAL|UNCL\s+BAL|MOD\s+BAL|PAGE\s+NO|STATEMENT\s+OF\s+ACCOUNT)\b",
    re.IGNORECASE,
)


def _merge_header_candidate_tokens(tok_list: List[PdfTextToken], bucket_tol: float = 18.0) -> List[Tuple[float, float, str]]:
    """Bucket X-overlapping tokens across 1 or 2 stacked header lines into `(x0, x1, text)` columns."""
    role_groups = (
        {"post"},
        {"value"},
        {"date", "dt"},
        {"narration", "particulars", "partlculars", "particuiars", "description", "remarks"},
        {"chq", "cheque", "ref", "reference"},
        {"debit", "debits", "withdrawal", "withdrawals", "withdrawl", "dr"},
        {"credit", "credits", "crodit", "deposit", "deposits", "cr"},
        {"balance", "balanoe"},
    )

    def _word_roles(txt: str) -> set[int]:
        ws = set(_WORDS_RE.findall(txt.lower()))
        return {idx for idx, grp in enumerate(role_groups) if ws & grp}

    ordered = sorted(tok_list, key=lambda t: (t.x, -t.y))
    buckets: List[List[PdfTextToken]] = []
    for tok in ordered:
        tok_roles = _word_roles(tok.text)
        tok_x1 = tok.x_end or (tok.x + len(tok.text) * 4.5)
        placed = False
        for b in buckets:
            b_x0 = min(t.x for t in b)
            b_x1 = max(t.x_end or (t.x + len(t.text) * 4.5) for t in b)
            b_roles: set[int] = set()
            for bt in b:
                b_roles |= _word_roles(bt.text)

            # Prevent merging distinct header column roles (e.g. Post Date + Value Date, Debit + Credit)
            if tok_roles and b_roles:
                allow_date_combo = (
                    (tok_roles == {2} and b_roles <= {0, 1} and 2 not in b_roles)
                    or (b_roles == {2} and tok_roles <= {0, 1} and 2 not in tok_roles)
                )
                if not allow_date_combo and (tok_roles & b_roles) != tok_roles and (tok_roles & b_roles) == set():
                    continue
                if tok_roles == b_roles and tok_roles == {2}:
                    continue

            same_y_toks = [t for t in b if abs(tok.y - t.y) <= 3.0]
            if same_y_toks:
                same_y_x1 = max(t.x_end or (t.x + len(t.text) * 4.5) for t in same_y_toks)
                if tok.x - same_y_x1 <= 5.5 and tok.x >= min(t.x for t in same_y_toks) - 2.0:
                    b.append(tok)
                    placed = True
                    break
                continue

            # Different vertical header row: require genuine horizontal X overlap (no cross-column horizontal reach)
            x_overlap = min(tok_x1, b_x1) - max(tok.x, b_x0)
            if x_overlap >= -1.0:
                b.append(tok)
                placed = True
                break
        if not placed:
            buckets.append([tok])

    cols: List[Tuple[float, float, str]] = []
    for b in buckets:
        # Sort top-to-bottom then left-to-right
        b_sorted = sorted(b, key=lambda t: (-round(t.y / 3.0), t.x))
        x0 = min(t.x for t in b_sorted)
        x1 = max((t.x_end or (t.x + len(t.text) * 4.5)) for t in b_sorted)
        txt = " ".join(t.text for t in b_sorted).strip()
        cols.append((x0, x1, txt))
    cols.sort(key=lambda c: c[0])
    return cols


def _score_header_columns(cols: Sequence[Tuple[float, float, str]]) -> Tuple[bool, int]:
    matched_cols = 0
    has_date = False
    has_amt_or_bal = False
    all_words: set[str] = set()
    for _, _, txt in cols:
        words = set(_WORDS_RE.findall(txt.lower()))
        all_words |= words
        if words & _HEADER_KEYWORDS:
            matched_cols += 1
        if "date" in words or "dt" in words:
            has_date = True
        if words & {
            "debit", "debits", "withdrawal", "withdrawals", "withdrawl",
            "credit", "credits", "crodit", "deposit", "deposits",
            "balance", "balanoe", "amount", "dr", "cr",
        }:
            has_amt_or_bal = True
    # Reject bottom summary footers like `Ope Bal | Dr count | Cr count | Debits | Credits | Clo Bal`
    has_narration_col = bool(all_words & {"particulars", "partlculars", "particuiars", "narration", "description", "remarks"})
    if (all_words & _FOOTER_SUMMARY_HEADER_WORDS) and not has_narration_col:
        return False, 0
    return (matched_cols >= 3 and has_date and has_amt_or_bal and len(cols) >= 4), matched_cols


def _compute_gutter_splits(
    cols: Sequence[Tuple[float, float, str]],
    body_lines: Sequence[List[PdfTextToken]],
    page_w: float,
) -> List[float]:
    """
    Place split boundaries inside genuine vertical whitespace gutters across table data rows,
    safely separating right-aligned numeric Debit/Credit/Balance columns without clipping.
    """
    n_cols = len(cols)
    w_bins = max(600, int(page_w) + 20)
    occupancy = [0] * w_bins

    for line in body_lines:
        if len(line) < 2:
            continue
        for tok in line:
            tx0 = max(0, int(tok.x))
            tx1 = min(w_bins - 1, int((tok.x_end or (tok.x + len(tok.text) * 4.3))))
            # Ignore huge full-width disclaimer/footer bars spanning >65% of page width
            if tx1 - tx0 > page_w * 0.65:
                continue
            for bx in range(tx0, tx1 + 1):
                occupancy[bx] += 1

    splits: List[float] = []
    for i in range(n_cols - 1):
        c0_x0, c0_x1, c0_txt = cols[i]
        c1_x0, c1_x1, c1_txt = cols[i + 1]
        c0_mid = (c0_x0 + c0_x1) / 2.0
        c1_mid = (c1_x0 + c1_x1) / 2.0

        # Search window centered between column headers; constrain between adjacent headers when non-overlapping
        if c0_x1 < c1_x0:
            left_bound = max(int(c0_x1 - 2), (int(splits[-1]) + 6) if splits else 0)
            right_bound = int(c1_x0 + 2)
        else:
            left_bound = max(int(c0_x0 + 8), int(c0_mid - 6))
            if splits:
                left_bound = max(left_bound, int(splits[-1]) + 8)
            right_bound = min(int(c1_x1), int(c1_mid + 6))

        best_gutter_mid: Optional[float] = None
        if right_bound - left_bound >= 6:
            # Find longest zero-occupancy run in [left_bound .. right_bound]
            best_len = 0
            cur_start: Optional[int] = None
            for bx in range(left_bound, right_bound + 1):
                if occupancy[bx] == 0:
                    if cur_start is None:
                        cur_start = bx
                else:
                    if cur_start is not None:
                        run_len = bx - cur_start
                        if run_len > best_len:
                            best_len = run_len
                            best_gutter_mid = (cur_start + bx - 1) / 2.0
                        cur_start = None
            if cur_start is not None:
                run_len = (right_bound + 1) - cur_start
                if run_len > best_len:
                    best_len = run_len
                    best_gutter_mid = (cur_start + right_bound) / 2.0

            if best_len < 3:
                best_gutter_mid = None

        if best_gutter_mid is not None:
            split_x = best_gutter_mid
        else:
            # Right-edge-to-left-edge fallback prevents small right-aligned numbers under a header from spilling right
            if c0_x1 < c1_x0:
                split_x = (c0_x1 + c1_x0) / 2.0
            else:
                split_x = (c0_mid + c1_mid) / 2.0

        if c0_x1 < c1_x0:
            split_x = max(c0_x1 - 1.5, min(c1_x0 + 1.5, split_x))

        if splits and split_x <= splits[-1] + 5.0:
            split_x = splits[-1] + 10.0
        splits.append(round(split_x, 2))
    return splits


def _has_body_transactions_below(body_lines: Sequence[List[PdfTextToken]]) -> bool:
    return any(_line_has_date(line) for line in body_lines)


def _is_strong_header(cols: Sequence[Tuple[float, float, str]]) -> bool:
    """A header that names Date, Balance and either Debit+Credit or Amount columns is trusted on its own."""
    words: set[str] = set()
    for _, _, txt in cols:
        words |= set(_WORDS_RE.findall(txt.lower()))
    has_date = "date" in words or "dt" in words
    has_dr_cr = bool(words & _DEBIT_WORDS) and bool(words & _CREDIT_WORDS)
    return has_date and bool(words & _BALANCE_WORDS) and (has_dr_cr or "amount" in words)


def _infer_table_header_splits(
    lines: List[List[PdfTextToken]],
    page_w: float = 595.0,
) -> Tuple[Optional[float], Optional[List[float]], int]:
    """
    Detect 1-line or 2-line stacked table headers and return (header_bottom_y, splits, header_line_span_count).
    """
    # Header candidates whose body showed no recognisable date rows: (strong, score, result)
    fallbacks: List[Tuple[bool, int, Tuple[float, List[float], int]]] = []
    for idx, line in enumerate(lines):
        line_txt_joined = " ".join(t.text for t in line)
        line_has_meta_or_date = bool(_HEADER_META_RE.search(line_txt_joined)) or any(
            m and _date_match_valid(m) for m in _DATE_ANYWHERE_RE.finditer(line_txt_joined)
        )
        if line_has_meta_or_date:
            continue

        cols_1 = _merge_header_candidate_tokens(line)
        ok_1, score_1 = _score_header_columns(cols_1)

        # Check if line + next line forms a richer 2-line stacked header (e.g., Withdrawal / Amt (Dr.))
        cols_2: Optional[List[Tuple[float, float, str]]] = None
        ok_2 = False
        score_2 = 0
        if idx + 1 < len(lines):
            nxt = lines[idx + 1]
            y_gap = line[0].y - nxt[0].y
            nxt_txt_joined = " ".join(t.text for t in nxt)
            # Only merge 2nd header line if vertically close and doesn't already look like a data row starting with a date
            looks_like_date = is_date_token(nxt[0].text, allow_suffix=True) or _line_has_date(nxt[:3])
            if 0 < y_gap <= 15.5 and not looks_like_date and len(nxt_txt_joined) < 180:
                cols_2 = _merge_header_candidate_tokens(line + nxt)
                ok_2, score_2 = _score_header_columns(cols_2)

        if ok_2 and (not ok_1 or score_2 > score_1 or len(cols_2 or []) > len(cols_1)):
            assert cols_2 is not None
            header_bottom_y = lines[idx + 1][0].y
            body = lines[idx + 2 :]
            res = (header_bottom_y, _compute_gutter_splits(cols_2, body, page_w), 2)
            if _has_body_transactions_below(body):
                return res
            fallbacks.append((_is_strong_header(cols_2), score_2, res))
            continue

        if ok_1:
            header_bottom_y = line[0].y
            body = lines[idx + 1 :]
            res = (header_bottom_y, _compute_gutter_splits(cols_1, body, page_w), 1)
            if _has_body_transactions_below(body):
                return res
            fallbacks.append((_is_strong_header(cols_1), score_1, res))

    # Keep a strong header's column splits even when no body row was recognised as dated (unusual date
    # formats must not discard a perfectly good header); weak headers only on tiny snippets.
    strong = [f for f in fallbacks if f[0]]
    if strong:
        return max(strong, key=lambda f: f[1])[2]
    if fallbacks and len(lines) <= 4:
        return fallbacks[0][2]
    return None, None, 0


def _infer_headerless_column_splits(
    lines: Sequence[List[PdfTextToken]],
    page_w: float = 595.0,
) -> Tuple[Optional[float], Optional[List[float]]]:
    """
    Geometrically infer `[Date(s) | Particulars | Debit | Credit | Balance]` column splits
    on headerless continuation pages or cropped passbook snippets.
    Returns `(table_top_y, splits)`.
    """
    date_intervals: List[Tuple[float, float]] = []
    num_intervals: List[Tuple[float, float]] = []
    non_num_words_x: List[Tuple[float, float]] = []
    first_date_y: Optional[float] = None

    for line in lines:
        line_str = " ".join(t.text for t in sorted(line, key=lambda t: t.x))
        is_skip = bool(_SKIP_SUMMARY_RE.search(line_str))
        row_date_spans = _date_spans(line, max_x=page_w * 0.42)
        if row_date_spans and first_date_y is None and not is_skip:
            first_date_y = max(t.y for t in line)
        if is_skip:
            continue
        date_intervals.extend(row_date_spans)
        for tok in line:
            txt = tok.text.strip()
            tx0 = tok.x
            tx1 = tok.x_end if tok.x_end > tok.x + 1.0 else (tok.x + max(4.0, len(txt) * 4.3))
            if any(d0 - 0.5 <= tx0 and tx1 <= d1 + 0.5 for d0, d1 in row_date_spans):
                continue
            if tx0 > page_w * 0.40 and _MONEY_CELL_RE.match(txt):
                num_intervals.append((tx0, tx1))
            elif _ALPHA_WORD_RE.search(txt) and txt.upper() not in {"CR", "DR"}:
                non_num_words_x.append((tx0, tx1))

    if len(num_intervals) < 2:
        return None, None

    def _cluster_intervals(items: List[Tuple[float, float]], pad: float = 2.0) -> List[List[float]]:
        # [x0, x1, count]
        clusters: List[List[float]] = []
        for x0, x1 in sorted(items, key=lambda p: (p[1], p[0])):
            placed = False
            for c in clusters:
                if max(c[0], x0) <= min(c[1], x1) + pad:
                    c[0] = min(c[0], x0)
                    c[1] = max(c[1], x1)
                    c[2] += 1
                    placed = True
                    break
            if not placed:
                clusters.append([x0, x1, 1.0])
        clusters.sort(key=lambda c: c[0])
        return clusters

    d_clusters = [c for c in _cluster_intervals(date_intervals, pad=3.0) if c[2] >= 2 or len(date_intervals) <= 3]
    if not d_clusters and date_intervals:
        d_clusters = _cluster_intervals(date_intervals, pad=3.0)[:1]

    raw_n_clusters = _cluster_intervals(num_intervals, pad=2.0)
    # Exclude numeric clusters inside narration region that heavily collide with regular alpha text
    n_clusters: List[List[float]] = []
    for c in raw_n_clusters:
        if c[0] < page_w * 0.45:
            text_hits = sum(1 for wx0, wx1 in non_num_words_x if wx0 < c[1] - 2.0 and wx1 > c[0] + 2.0)
            if text_hits >= 2:
                continue
        n_clusters.append(c)

    if len(n_clusters) < 2 or not d_clusters:
        return None, None

    splits: List[float] = []
    # Split after each date cluster (up to 2 dates: Tran Date, optional Value Date)
    for k, dc in enumerate(d_clusters[:2]):
        if k + 1 < len(d_clusters[:2]):
            nx = d_clusters[k + 1][0]
        else:
            # Find minimum left edge of non-date text tokens cleanly right of date cluster
            right_texts = [wx0 for wx0, _ in non_num_words_x if wx0 >= dc[1] - 2.0]
            nx = min(right_texts) if right_texts else (dc[1] + 6.0)
        sx = (dc[1] + max(dc[1] + 2.0, nx)) / 2.0
        if sx < n_clusters[0][0] - 8.0:
            splits.append(round(sx, 2))

    # Split between Particulars and first numeric column
    first_num_x0 = n_clusters[0][0]
    part_right_words = [wx1 for wx0, wx1 in non_num_words_x if wx1 <= first_num_x0 + 2.0 and wx0 >= (splits[-1] if splits else 0.0)]
    if part_right_words:
        pre_num_split = max(min(first_num_x0 - 2.0, (max(part_right_words) + first_num_x0) / 2.0), first_num_x0 - 14.0)
    else:
        pre_num_split = first_num_x0 - 4.0
    if not splits or pre_num_split > splits[-1] + 8.0:
        splits.append(round(pre_num_split, 2))

    # Split between numeric columns (Debit | Credit | Balance)
    for j in range(len(n_clusters) - 1):
        mid_x = (n_clusters[j][1] + n_clusters[j + 1][0]) / 2.0
        if mid_x > splits[-1] + 4.0:
            splits.append(round(mid_x, 2))

    table_top_y = (first_date_y + 20.0) if first_date_y is not None else None
    return table_top_y, (splits if len(splits) >= 2 else None)


def _deskew_from_row_anchors(p_tokens: List[PdfTextToken], page_w: float) -> List[PdfTextToken]:
    """
    Estimate and remove non-uniform camera tilt/keystone (`s(y)`) on headerless photographed
    tables by matching left-margin date tokens with right-margin balance tokens across rows.
    """
    date_re = _DATE_CELL_RE
    bal_re = _DESKEW_BAL_RE

    raw_left_dates = [
        t for t in p_tokens
        if t.x < page_w * 0.18 and date_re.match(t.text.strip())
    ]
    if len(raw_left_dates) < 5:
        return p_tokens

    xs_left = sorted(t.x for t in raw_left_dates)
    med_x0 = xs_left[len(xs_left) // 2]
    left_dates = sorted(
        (t for t in raw_left_dates if abs(t.x - med_x0) <= 18.0),
        key=lambda t: t.y,
        reverse=True,
    )
    # Deduplicate same-line date tokens within 3pt
    date_rows: List[PdfTextToken] = []
    for t in left_dates:
        if not date_rows or abs(t.y - date_rows[-1].y) > 3.0:
            date_rows.append(t)
        elif t.x < date_rows[-1].x:
            date_rows[-1] = t
    if len(date_rows) < 5:
        return p_tokens

    min_date_y = date_rows[-1].y
    max_date_y = date_rows[0].y
    raw_right_bals = sorted(
        (
            t for t in p_tokens
            if t.x > page_w * 0.76
            and bal_re.match(t.text.strip())
            and min_date_y - 12.0 <= t.y <= max_date_y + 28.0
        ),
        key=lambda t: t.y,
        reverse=True,
    )
    if len(raw_right_bals) < 5:
        return p_tokens

    # Iterative mutual nearest matching: pass 1 uses |dy| <= 12pt on well-spaced dates, then extrapolates linear slope
    isolated_pairs: List[Tuple[float, float, float]] = []  # (y_date, x_date, y_bal, s) -> store (y_date, dy, x_span)
    for idx, dt_tok in enumerate(date_rows):
        prev_gap = (date_rows[idx - 1].y - dt_tok.y) if idx > 0 else 999.0
        next_gap = (dt_tok.y - date_rows[idx + 1].y) if idx + 1 < len(date_rows) else 999.0
        if min(prev_gap, next_gap) < 14.0:
            continue
        cands = [bt for bt in raw_right_bals if abs(bt.y - dt_tok.y) <= 12.0]
        if not cands:
            continue
        best_bt = min(cands, key=lambda bt: abs(bt.y - dt_tok.y))
        if abs(best_bt.x - dt_tok.x) <= page_w * 0.45:
            continue
        isolated_pairs.append((dt_tok.y, best_bt.y - dt_tok.y, best_bt.x - dt_tok.x))

    if len(isolated_pairs) < 5:
        return p_tokens

    max_abs_dy = max(abs(dy) for _, dy, _ in isolated_pairs)
    # Require noticeable vertical skew (>= 4.5pt) so flat aligned tables/passbooks are untouched
    if max_abs_dy < 4.5:
        # Check if top/bottom edge skew extends further via linear fit check
        ys_t = [p[0] for p in isolated_pairs]
        dys_t = [p[1] for p in isolated_pairs]
        if max(abs(d) for d in dys_t) < 4.5:
            return p_tokens

    # Fit robust linear dy(y) = a * y + b on isolated pairs and verify tight collinearity before expanding anchors
    ny = float(len(isolated_pairs))
    my_i = sum(p[0] for p in isolated_pairs) / ny
    mdy_i = sum(p[1] for p in isolated_pairs) / ny
    var_y = sum((p[0] - my_i) ** 2 for p in isolated_pairs)
    if var_y <= 10.0:
        return p_tokens
    lin_a = sum((p[0] - my_i) * (p[1] - mdy_i) for p in isolated_pairs) / var_y
    lin_b = mdy_i - lin_a * my_i
    fit_res = sorted(abs(p[1] - (lin_a * p[0] + lin_b)) for p in isolated_pairs)
    if fit_res[len(fit_res) // 2] > 3.5:
        return p_tokens

    # Expand matches sequentially outward (bottom->up and top->down) using local slope so curved edge tilt stays accurate
    matched_idx_by_bal: Dict[int, Tuple[float, float]] = {}  # date_idx -> (y_bal, slope_s)
    used_bals: set[int] = set()
    iso_by_y: List[Tuple[int, PdfTextToken, float]] = []
    for idx, dt_tok in enumerate(date_rows):
        prev_gap = (date_rows[idx - 1].y - dt_tok.y) if idx > 0 else 999.0
        next_gap = (dt_tok.y - date_rows[idx + 1].y) if idx + 1 < len(date_rows) else 999.0
        if min(prev_gap, next_gap) < 12.0:
            continue
        pred_y_bal = dt_tok.y + (lin_a * dt_tok.y + lin_b)
        cands = [
            (bi, bt)
            for bi, bt in enumerate(raw_right_bals)
            if bi not in used_bals
            and abs(bt.y - pred_y_bal) <= 7.0
            and bt.x - dt_tok.x > page_w * 0.45
        ]
        if cands:
            bi_best, best_bt = min(cands, key=lambda pair: abs(pair[1].y - pred_y_bal))
            used_bals.add(bi_best)
            slope_s = (best_bt.y - dt_tok.y) / (best_bt.x - dt_tok.x)
            matched_idx_by_bal[idx] = (best_bt.y, slope_s)
            iso_by_y.append((idx, best_bt, best_bt.y - dt_tok.y))

    # Second pass for extreme top/bottom isolated dates with smooth camera curl (> 7pt above linear fit)
    if len(iso_by_y) >= 3:
        iso_by_y.sort(key=lambda item: item[1].y)  # ascending y (bottom to top)
        for idx, dt_tok in enumerate(date_rows):
            if idx in matched_idx_by_bal:
                continue
            prev_gap = (date_rows[idx - 1].y - dt_tok.y) if idx > 0 else 999.0
            next_gap = (dt_tok.y - date_rows[idx + 1].y) if idx + 1 < len(date_rows) else 999.0
            if min(prev_gap, next_gap) < 12.0:
                continue
            # Local extrapolation from top 2 or bottom 2 verified anchors
            ref_pair = iso_by_y[-2:] if dt_tok.y > iso_by_y[-1][1].y else iso_by_y[:2]
            if len(ref_pair) == 2 and abs(ref_pair[1][1].y - ref_pair[0][1].y) > 5.0:
                loc_a = (ref_pair[1][2] - ref_pair[0][2]) / (ref_pair[1][1].y - ref_pair[0][1].y)
                loc_dy = ref_pair[1][2] + loc_a * (dt_tok.y - ref_pair[1][1].y)
            else:
                loc_dy = lin_a * dt_tok.y + lin_b
            pred_y_bal = dt_tok.y + loc_dy
            cands = [
                (bi, bt)
                for bi, bt in enumerate(raw_right_bals)
                if bi not in used_bals
                and abs(bt.y - pred_y_bal) <= 7.0
                and bt.x - dt_tok.x > page_w * 0.45
            ]
            if cands:
                bi_best, best_bt = min(cands, key=lambda pair: abs(pair[1].y - pred_y_bal))
                used_bals.add(bi_best)
                matched_idx_by_bal[idx] = (best_bt.y, (best_bt.y - dt_tok.y) / (best_bt.x - dt_tok.x))
                iso_by_y.append((idx, best_bt, best_bt.y - dt_tok.y))
                iso_by_y.sort(key=lambda item: item[1].y)

    anchors: List[Tuple[float, float]] = list(matched_idx_by_bal.values())

    if len(anchors) < 5:
        return p_tokens

    anchors.sort(key=lambda a: a[0])
    if max(abs(a[1]) for a in anchors) * (page_w * 0.75) < 4.5:
        return p_tokens

    def _interp_slope(y_val: float) -> float:
        if y_val <= anchors[0][0]:
            return anchors[0][1]
        if y_val >= anchors[-1][0]:
            return anchors[-1][1]
        for j in range(1, len(anchors)):
            y0, s0 = anchors[j - 1]
            y1, s1 = anchors[j]
            if y_val <= y1:
                if y1 <= y0:
                    return s1
                frac = (y_val - y0) / (y1 - y0)
                return s0 + frac * (s1 - s0)
        return anchors[-1][1]

    y_low = anchors[0][0] - 8.0
    y_high = anchors[-1][0] + 12.0
    return [
        PdfTextToken(
            page=t.page,
            x=t.x,
            y=(
                t.y - _interp_slope(t.y) * max(0.0, t.x - med_x0)
                if (t.x > med_x0 + 25.0 and y_low <= t.y <= y_high)
                else t.y
            ),
            text=t.text,
            x_end=t.x_end,
        )
        for t in p_tokens
    ]


def _deskew_page_tokens(p_tokens: List[PdfTextToken], page_w: float) -> List[PdfTextToken]:
    """Estimate and remove linear camera tilt (`|slope| <= 0.04`) from page header anchor lines or row anchors."""
    if len(p_tokens) < 6 or page_w <= 10.0:
        return p_tokens

    # Find horizontal header candidate band containing >= 3 distinct header keywords spanning >= 40% page width
    kw_tokens = [
        t for t in p_tokens
        if set(_WORDS_RE.findall(t.text.lower())) & _HEADER_KEYWORDS
    ]
    best_band: List[PdfTextToken] = []
    if len(kw_tokens) >= 3:
        for anchor in kw_tokens:
            band = [t for t in kw_tokens if abs(t.y - anchor.y) <= 13.0]
            if len(band) >= 4:
                band_words: set[str] = set()
                for bt in band:
                    band_words |= set(_WORDS_RE.findall(bt.text.lower()))
                if not (("date" in band_words or "dt" in band_words) and (band_words & {"balance", "balanoe"})):
                    continue
                span = max(t.x for t in band) - min(t.x for t in band)
                if span >= page_w * 0.40 and len(band) > len(best_band):
                    best_band = band

    if len(best_band) >= 4:
        n = float(len(best_band))
        mx = sum(t.x for t in best_band) / n
        my = sum(t.y for t in best_band) / n
        var_x = sum((t.x - mx) ** 2 for t in best_band)
        if var_x > 1.0:
            slope = sum((t.x - mx) * (t.y - my) for t in best_band) / var_x
            if 0.0025 <= abs(slope) <= 0.045:
                residuals = sorted(abs((t.y - my) - slope * (t.x - mx)) for t in best_band)
                if residuals[len(residuals) // 2] <= 3.0:
                    cx = page_w * 0.5
                    return [
                        PdfTextToken(
                            page=t.page,
                            x=t.x,
                            y=t.y - slope * (t.x - cx),
                            text=t.text,
                            x_end=t.x_end,
                        )
                        for t in p_tokens
                    ]

    return _deskew_from_row_anchors(p_tokens, page_w)


def _associate_orphan_amount_lines(
    lines: List[List[PdfTextToken]],
    active_splits: Optional[List[float]],
    page_w: float,
) -> List[List[PdfTextToken]]:
    """
    Merge half-line offset prefix/amount rows (`DEP TFR` / `WDL TFR` + Debit/Credit/Balance above Date,
    or camera-tilted right-hand numbers without dates) with their adjacent transaction Date row.
    """
    if len(lines) < 2:
        return lines

    bal_x_thresh = (active_splits[-1] - 4.0) if active_splits else (page_w * 0.70)

    def _flags(line: Sequence[PdfTextToken]) -> Tuple[bool, bool, bool, bool, float, bool]:
        txt = " ".join(t.text for t in sorted(line, key=lambda t: t.x))
        is_skip = bool(_SKIP_SUMMARY_RE.search(txt))
        has_date = any(
            _OCR_ROW_DATE_RE.match(t.text.strip()) or is_date_token(t.text, allow_suffix=True)
            for t in line
            if t.x < page_w * 0.42
        ) or _line_has_date(line, max_x=page_w * 0.42)
        has_money = any(_AMOUNT_2DP_RE.search(t.text) and t.x >= page_w * 0.35 for t in line)
        has_bal = any(_AMOUNT_2DP_RE.search(t.text) and t.x >= bal_x_thresh for t in line)
        avg_y = sum(t.y for t in line) / max(len(line), 1)
        is_tfr_prefix = bool(_TFR_PREFIX_RE.match(txt.strip()))
        return is_skip, has_date, has_money, has_bal, avg_y, is_tfr_prefix

    consumed = [False] * len(lines)
    merged_into: Dict[int, List[PdfTextToken]] = {}

    meta = [_flags(ln) for ln in lines]
    for i, ln in enumerate(lines):
        is_skip, has_date, has_money, has_bal, avg_y, is_tfr_prefix = meta[i]
        if is_skip or has_date or (not has_money and not is_tfr_prefix):
            continue

        nxt_ok = False
        nxt_dy = 999.0
        if i + 1 < len(lines):
            n_skip, n_date, _, n_bal, n_y, _ = meta[i + 1]
            nxt_dy = abs(avg_y - n_y)
            if not n_skip and n_date and not (has_bal and n_bal) and nxt_dy <= 18.0:
                nxt_ok = True

        prev_ok = False
        prev_dy = 999.0
        if i - 1 >= 0 and not consumed[i - 1]:
            p_skip, p_date, _, p_bal, p_y, _ = meta[i - 1]
            prev_dy = abs(p_y - avg_y)
            if not p_skip and p_date and not (has_bal and p_bal) and prev_dy <= 14.0:
                prev_ok = True

        target_idx: Optional[int] = None
        if nxt_ok and prev_ok:
            target_idx = (i + 1) if (is_tfr_prefix or nxt_dy <= prev_dy) else (i - 1)
        elif nxt_ok:
            target_idx = i + 1
        elif prev_ok:
            target_idx = i - 1

        if target_idx is not None:
            consumed[i] = True
            merged_into.setdefault(target_idx, []).extend(ln)

    out: List[List[PdfTextToken]] = []
    for i, ln in enumerate(lines):
        if consumed[i]:
            continue
        extra = merged_into.get(i)
        if extra:
            out.append(ln + extra)
        else:
            out.append(ln)
    return out


def _assign_line_to_columns(
    line: List[PdfTextToken],
    splits: List[float],
    use_left_edge_only: bool = False,
    numeric_anchor: str = "mid",
) -> List[str]:
    """
    Map tokens into fixed-width columns using x split boundaries, preserving top-to-bottom sub-line order.
    Text tokens are placed by their left edge. Numeric tokens with a known right edge are placed by their
    midpoint (`numeric_anchor="mid"`, inferred splits) or by their right edge (`"right"`, user-drawn dividers:
    a large right-aligned amount that starts left of its divider still lands in its own column, PDF-23).
    `use_left_edge_only=True` places every token by its left edge.
    """
    num_cols = len(splits) + 1
    col_toks: List[List[PdfTextToken]] = [[] for _ in range(num_cols)]
    for tok in sorted(line, key=lambda t: (-t.y, t.x)):
        anchor_x = tok.x
        if not use_left_edge_only and tok.x_end > tok.x and _RIGHT_ALIGNED_NUMERIC_RE.match(tok.text.strip()):
            if numeric_anchor == "right":
                anchor_x = tok.x_end - 0.01
            elif numeric_anchor == "mid":
                # Midpoint keeps small right-aligned amounts inside their column
                anchor_x = (tok.x + tok.x_end) / 2.0
        c_idx = 0
        while c_idx < len(splits) and anchor_x >= splits[c_idx]:
            c_idx += 1
        col_toks[c_idx].append(tok)

    cols: List[str] = []
    for c_list in col_toks:
        if not c_list:
            cols.append("")
            continue
        # Cluster into sub-lines top-to-bottom (`dy > 4.0`) so merged prefix lines (`DEP TFR`) precede main line cleanly
        sublines: List[List[PdfTextToken]] = []
        for t in sorted(c_list, key=lambda t: (-t.y, t.x)):
            if not sublines or abs(sublines[-1][0].y - t.y) > 4.0:
                sublines.append([t])
            else:
                sublines[-1].append(t)
        ordered_texts: List[str] = []
        for sl in sublines:
            ordered_texts.extend(t.text for t in sorted(sl, key=lambda t: t.x))
        cols.append(" ".join(ordered_texts).strip())
    return cols


def _group_tokens_into_rows(
    tokens: List[PdfTextToken],
    y_tolerance: float = 3.5,
    column_splits: Optional[List[float]] = None,
    bbox: Optional[List[float]] = None,
    page_sizes: Optional[List[Tuple[float, float]]] = None,
    page_y_tolerance: Optional[Dict[int, float]] = None,
) -> List[List[str]]:
    """
    Group positioned PDF/OCR tokens into tabular rows and columns, including stacked headers.
    `page_y_tolerance` overrides `y_tolerance` per page (OCR'd pages in a mixed document use a looser one).
    """
    if not tokens:
        return []

    filtered = tokens
    if bbox and len(bbox) == 4 and page_sizes:
        is_norm = max(bbox) <= 1.05
        kept: List[PdfTextToken] = []
        for tok in tokens:
            pw, ph = page_sizes[min(tok.page - 1, len(page_sizes) - 1)]
            if is_norm:
                x0, y0, x1, y1 = bbox[0] * pw, (1.0 - bbox[3]) * ph, bbox[2] * pw, (1.0 - bbox[1]) * ph
            else:
                x0, y0, x1, y1 = bbox
            if x0 <= tok.x <= x1 and y0 <= tok.y <= y1:
                kept.append(tok)
        filtered = kept

    by_page: Dict[int, List[PdfTextToken]] = {}
    for t in filtered:
        by_page.setdefault(t.page, []).append(t)

    all_rows: List[List[str]] = []
    inferred_splits: Optional[List[float]] = None
    base_y_tolerance = y_tolerance
    for p_num in sorted(by_page.keys()):
        y_tolerance = (page_y_tolerance or {}).get(p_num, base_y_tolerance)
        pw = page_sizes[min(p_num - 1, len(page_sizes) - 1)][0] if page_sizes else 595.0
        p_tokens = sorted(_deskew_page_tokens(by_page[p_num], pw), key=lambda t: (-t.y, t.x))
        lines: List[List[PdfTextToken]] = []
        for tok in p_tokens:
            if not lines or abs(lines[-1][0].y - tok.y) > y_tolerance:
                lines.append([tok])
            else:
                lines[-1].append(tok)

        if column_splits:
            splits = [s * pw if max(column_splits) <= 1.05 else s for s in sorted(column_splits)]
            for line in _associate_orphan_amount_lines(lines, splits, pw):
                row = _assign_line_to_columns(line, splits, numeric_anchor="right")
                if any(row):
                    all_rows.append(row)
        else:
            header_y, page_splits, span_count = _infer_table_header_splits(lines, page_w=pw)
            table_top_y: Optional[float] = header_y
            if page_splits is None and inferred_splits is None:
                hl_top_y, page_splits = _infer_headerless_column_splits(lines, page_w=pw)
                if page_splits is not None:
                    table_top_y = hl_top_y
            if page_splits is not None:
                inferred_splits = page_splits
            active_splits = page_splits or inferred_splits

            if header_y is not None and active_splits is not None:
                # Find the exact header bottom line index
                hdr_end_idx = 0
                for idx, ln in enumerate(lines):
                    if abs(ln[0].y - header_y) <= 0.1:
                        hdr_end_idx = idx
                        break
                hdr_start_idx = max(0, hdr_end_idx - (span_count - 1))
                for line in lines[:hdr_start_idx]:
                    row = [tok.text for tok in sorted(line, key=lambda t: t.x)]
                    if any(row):
                        all_rows.append(row)
                hdr_toks: List[PdfTextToken] = []
                for hln in lines[hdr_start_idx : hdr_end_idx + 1]:
                    hdr_toks.extend(hln)
                hdr_row = _assign_line_to_columns(hdr_toks, active_splits)
                if any(hdr_row):
                    all_rows.append(hdr_row)
                body_lines = _associate_orphan_amount_lines(lines[hdr_end_idx + 1 :], active_splits, pw)
                for line in body_lines:
                    row = _assign_line_to_columns(line, active_splits)
                    if any(row):
                        all_rows.append(row)
            else:
                meta_lines: List[List[PdfTextToken]] = []
                body_lines_raw: List[List[PdfTextToken]] = []
                for line in lines:
                    if active_splits is not None and (table_top_y is None or line[0].y <= table_top_y + y_tolerance):
                        body_lines_raw.append(line)
                    else:
                        meta_lines.append(line)
                for line in meta_lines:
                    row = [tok.text for tok in sorted(line, key=lambda t: t.x)]
                    if any(row):
                        all_rows.append(row)
                body_lines = _associate_orphan_amount_lines(body_lines_raw, active_splits, pw)
                for line in body_lines:
                    if active_splits is not None:
                        row = _assign_line_to_columns(line, active_splits)
                    else:
                        row = [tok.text for tok in sorted(line, key=lambda t: t.x)]
                    if any(row):
                        all_rows.append(row)
    return all_rows


# ---------------------------------------------------------------------------
# Main Public PDF Extraction API
# ---------------------------------------------------------------------------

_OCR_NOISE_RE = re.compile(r"[A-Za-z0-9][~•][A-Za-z0-9~]|[~•][A-Za-z]|[?\\^][A-Za-z]{2,}|[A-Za-z]+\\[A-Za-z]+|[A-Za-z]+•")
_OCR_DIGIT_LETTER_AMT_RE = re.compile(
    r"(?<!Rs)(?<!INR)(?<!USD)\b(?:\d+[A-DF-Za-df-z]+\d*|[BDOoIlS]\d{2,}|\d+[lIOoB])\.\d{2}(?:Cr|Dr|CR|DR|C|D)?\b"
)
_AMOUNT_TOKEN_RE = re.compile(r"^(?:Cr|Dr)?[\d,]+\.\d{2}(?:\s*(?:CR|DR))?$", re.IGNORECASE)


def _has_full_page_scan_image(pdf_bytes: bytes) -> bool:
    """Document-level fallback when the page tree cannot be walked: any image XObject of scan size."""
    for m in _SUBTYPE_IMAGE_RE.finditer(pdf_bytes):
        window = pdf_bytes[max(0, m.start() - 400) : m.end() + 400]
        wm = _WIDTH_RE.search(window)
        hm = _HEIGHT_RE.search(window)
        if wm and hm:
            w, h = int(wm.group(1)), int(hm.group(1))
            if max(w, h) >= 800 and min(w, h) >= 400:
                return True
    return False


def _is_corrupted_ocr_text(pages_text: Sequence[str], tokens: Sequence[PdfTextToken]) -> bool:
    """
    Heuristic for a garbage OCR text layer embedded by a scanner (tildes/bullets glued to letters, letters
    inside amounts). `NEFT:HDFC` / `Account Name:John` style colons are normal narration text and do not count.
    """
    joined = "\n".join(pages_text) if pages_text else " ".join(t.text for t in tokens)
    if not joined.strip():
        return False
    tilde_noise = len(_OCR_NOISE_RE.findall(joined))
    digit_letter_amt = len(_OCR_DIGIT_LETTER_AMT_RE.findall(joined))
    return (tilde_noise >= 2) or (digit_letter_amt >= 1 and tilde_noise >= 1) or (digit_letter_amt >= 2)


def _count_dates_and_amounts(tokens: Sequence[PdfTextToken]) -> Tuple[int, int]:
    d_cnt = 0
    a_cnt = 0
    for t in tokens:
        s = t.text.strip()
        if is_date_token(s, allow_suffix=True):
            d_cnt += 1
        if _AMOUNT_TOKEN_RE.match(s):
            a_cnt += 1
    return d_cnt, a_cnt


def _page_needs_ocr(
    toks: Sequence[PdfTextToken],
    has_scan_image: bool,
    doc_has_images: bool,
    doc_has_text: bool,
    page_has_image: Optional[bool] = None,
) -> bool:
    """
    OCR a page only when it lacks a usable text layer:
      * no text at all, and the page itself draws an image (or nothing in the document has text, e.g. outlined
        glyphs); `doc_has_images` stands in for `page_has_image` only when the page could not be inspected; or
      * it carries a full-page scan image and its text layer is tiny, garbled, or has no date/amount density.
    Pages with real text and no full-page scan image are never re-OCR'd (logos do not count), and a blank
    page of a native statement is not OCR'd because another page carries a logo (PDF-25).
    """
    chars = sum(len(t.text.strip()) for t in toks)
    if chars == 0:
        has_image = doc_has_images if page_has_image is None else page_has_image
        return has_image or not doc_has_text
    if not has_scan_image:
        return False
    if chars < MIN_PAGE_TEXT_CHARS:
        return True
    if _is_corrupted_ocr_text([" ".join(t.text for t in toks)], toks):
        return True
    d_cnt, a_cnt = _count_dates_and_amounts(toks)
    return d_cnt < 2 or a_cnt < 2


def _decrypt_in_process(pdf_bytes: bytes, password: Optional[str]) -> Tuple[Optional[bytes], str]:
    """
    Decrypt with pikepdf (in memory) or qpdf (password via a 0600 file, output on stdout).
    Returns (decrypted_bytes_or_None, status) with status in
    {"ok", "wrong_password", "password_required", "corrupt", "unavailable"}.
    """
    try:
        import pikepdf  # type: ignore
    except ImportError:
        pikepdf = None  # type: ignore[assignment]
    if pikepdf is not None:
        try:
            buf = io.BytesIO()
            with pikepdf.open(io.BytesIO(pdf_bytes), password=password or "") as pdf:
                pdf.save(buf)
            return buf.getvalue(), "ok"
        except pikepdf.PasswordError:
            return None, ("wrong_password" if password else "password_required")
        except Exception:  # noqa: BLE001 - pikepdf.PdfError and friends
            # pikepdf (qpdf) is installed but cannot open the file: do NOT fall back to Poppler with the password
            # on its command line (PDF-14). An unencrypted damaged file is still left to Poppler.
            return None, ("corrupt" if password else "unavailable")

    qpdf = _find_binary("qpdf", ("/opt/homebrew/bin/qpdf", "/usr/local/bin/qpdf", "/usr/bin/qpdf"))
    if not qpdf:
        return None, "unavailable"
    with private_tempdir() as td:
        in_p = write_private_file(td, "in.pdf", pdf_bytes)
        pw_p = write_private_file(td, "pw", (password or "").encode("utf-8"))
        try:
            proc = subprocess.run(
                [qpdf, f"--password-file={pw_p}", "--decrypt", in_p, "-"],
                capture_output=True, timeout=POPPLER_TIMEOUT_S,
            )
        except (OSError, subprocess.SubprocessError):
            return None, "unavailable"
    if proc.returncode in (0, 3) and proc.stdout.startswith(b"%PDF"):
        return proc.stdout, "ok"
    err = proc.stderr.decode("utf-8", errors="replace").lower()
    if "password" in err:
        return None, ("wrong_password" if password else "password_required")
    return None, "unavailable"


# ---------------------------------------------------------------------------
# Draw Tool template validation (PDF-22)
# ---------------------------------------------------------------------------

MAX_DRAW_SPLITS = 32
MAX_DRAW_COORD = 14400.0  # PDF user-space limit (200 in at 72 pt/in)


class DrawTemplateError(ValueError):
    """A Draw Tool template (column splits / bbox) is malformed; callers should answer HTTP 400."""


def _finite_number(v: Any, what: str) -> float:
    if isinstance(v, bool):
        raise DrawTemplateError(f"{what} must be a number, not a boolean.")
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise DrawTemplateError(f"{what} must be a number (got {v!r}).") from None
    if not math.isfinite(f):
        raise DrawTemplateError(f"{what} must be a finite number (got {v!r}).")
    return f


def validate_draw_template(
    column_splits: Optional[Sequence[Any]] = None,
    bbox: Optional[Sequence[Any]] = None,
) -> Tuple[Optional[List[float]], Optional[List[float]]]:
    """
    Validate and normalise a Draw Tool template. Returns (sorted splits or None, bbox or None).

    Splits: 1..32 finite, distinct numbers, all normalised (0..1, i.e. at most 1.05) or all in PDF points
    (0..14400). Bbox: [x0, y0, x1, y1], finite, x0 < x1 and y0 < y1, normalised (0..1, y measured from the top,
    as the Draw Tool draws it) or in points. Raises DrawTemplateError (a ValueError) with a readable message.
    """
    splits_out: Optional[List[float]] = None
    if column_splits is not None:
        if isinstance(column_splits, (str, bytes)) or not isinstance(column_splits, Sequence):
            raise DrawTemplateError("column_splits must be a list of numbers.")
        if len(column_splits) > MAX_DRAW_SPLITS:
            raise DrawTemplateError(f"At most {MAX_DRAW_SPLITS} column dividers are supported.")
        vals = sorted(_finite_number(v, f"column_splits[{i}]") for i, v in enumerate(column_splits))
        if vals:
            if vals[0] < 0 or vals[-1] > MAX_DRAW_COORD:
                raise DrawTemplateError("Column dividers must lie on the page (0 .. page width).")
            if any(b - a <= 1e-6 for a, b in zip(vals, vals[1:])):
                raise DrawTemplateError("Column dividers must be distinct.")
            if vals[-1] > 1.05 and 0.0 < vals[0] <= 1.05:
                raise DrawTemplateError("Column dividers mix normalised (0..1) and point coordinates.")
            splits_out = vals
    bbox_out: Optional[List[float]] = None
    if bbox is not None:
        if isinstance(bbox, (str, bytes)) or not isinstance(bbox, Sequence) or len(bbox) != 4:
            raise DrawTemplateError("bbox must be [x0, y0, x1, y1].")
        b = [_finite_number(v, f"bbox[{i}]") for i, v in enumerate(bbox)]
        if min(b) < 0 or max(b) > MAX_DRAW_COORD:
            raise DrawTemplateError("bbox must lie on the page.")
        if not (b[0] < b[2] and b[1] < b[3]):
            raise DrawTemplateError("bbox must satisfy x0 < x1 and y0 < y1.")
        bbox_out = b
    return splits_out, bbox_out


# ---------------------------------------------------------------------------
# Password candidates (PDF-13) and stdlib Standard Security Handler checks (PDF-12/PDF-14)
# ---------------------------------------------------------------------------

MAX_PASSWORD_CANDIDATES = 64
_PW_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")
_SALUTATION_RE = re.compile(r"^(?:MR|MRS|MS|MISS|M/S|MESSRS|DR|SHRI|SMT|KUM|SRI|LATE)\.?\s+", re.IGNORECASE)
_MON_ABBR = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
# Formula order preferred per bank (labels as produced below); other formulas follow in the generic order.
_BANK_PASSWORD_PREFERENCE: Dict[str, Tuple[str, ...]] = {
    "SBI": ("mobile_last5+dob_ddmmyy", "dob_ddmmyy", "dob_ddmmyyyy"),
    "HDFC": ("customer_id", "name4_upper+dob_ddmm", "name4_lower+dob_ddmm"),
    "ICICI": ("name4_lower+dob_ddmm", "name4_upper+dob_ddmm", "account_number"),
    "AXIS": ("name4_upper+dob_ddmm", "name4_lower+dob_ddmm", "customer_id"),
    "KOTAK": ("customer_id", "name4_lower+dob_ddmm", "dob_ddmmyyyy"),
    "YES": ("customer_id", "name4_lower+dob_ddmm"),
    "INDUS": ("name4_lower+dob_ddmm", "customer_id", "dob_ddmmyyyy"),
    "IDFC": ("name4_lower+dob_ddmm", "dob_ddmmyyyy"),
    "PNB": ("dob_ddmmyyyy", "account_last4+dob_ddmm", "name4_upper+dob_ddmm"),
    "BOB": ("name4_upper+dob_ddmm", "dob_ddmmyyyy"),
    "UNION": ("dob_ddmmyyyy", "name4_upper+dob_ddmm"),
    "CANARA": ("dob_ddmmyyyy", "name4_upper+dob_ddmm"),
}


def _meta_get(meta: Mapping[str, Any], *keys: str) -> Any:
    lowered = {str(k).strip().lower(): v for k, v in meta.items()}
    for k in keys:
        v = lowered.get(k)
        if v not in (None, ""):
            return v
    return None


def _parse_dob(value: Any) -> Optional[_dt.date]:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if value is None:
        return None
    txt = str(value).strip()
    if re.fullmatch(r"\d{8}", txt):  # DDMMYYYY or YYYYMMDD
        for fmt in ("%d%m%Y", "%Y%m%d"):
            try:
                d = _dt.datetime.strptime(txt, fmt).date()
                if 1900 <= d.year <= 2100:
                    return d
            except ValueError:
                pass
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d", "%d %b %Y", "%d-%b-%Y",
                "%d %B %Y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return _dt.datetime.strptime(txt, fmt).date()
        except ValueError:
            continue
    return None


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value)) if value is not None else ""


def generate_password_candidates(meta: Mapping[str, Any]) -> List[Tuple[str, str]]:
    """
    Pure function: candidate passwords for an encrypted bank statement from known holder details.

    `meta` keys (case-insensitive; all optional): "passwords" (list of explicitly supplied passwords, tried
    first), "name" / "holder_name" / "account_holder", "dob" / "date_of_birth" (date, datetime or text such as
    1985-03-07, 07/03/1985, 07031985, 07 Mar 1985), "pan", "account_number" / "account_no", "customer_id" /
    "crn" / "cif", "mobile" / "phone", "bank" (SBI, HDFC, ICICI, ...; moves that bank's usual formulas first).

    Returns [(label, password), ...]: unique passwords, deterministic order, at most MAX_PASSWORD_CANDIDATES.
    The label names the formula ("dob_ddmmyyyy", "name4_lower+dob_ddmm", "supplied_1", ...) so callers can
    report which formula worked without echoing the password.
    """
    meta = meta or {}
    out: List[Tuple[str, str]] = []
    seen: set = set()

    def add(label: str, pw: Optional[str]) -> None:
        if pw and pw not in seen and len(out) < MAX_PASSWORD_CANDIDATES:
            seen.add(pw)
            out.append((label, pw))

    supplied = _meta_get(meta, "passwords", "password")
    if isinstance(supplied, str):
        supplied = [supplied]
    for i, pw in enumerate(supplied or [], start=1):
        if isinstance(pw, str):
            add(f"supplied_{i}", pw)

    name_raw = str(_meta_get(meta, "name", "holder_name", "account_holder", "customer_name") or "").strip()
    name_raw = _SALUTATION_RE.sub("", name_raw)
    first = re.sub(r"[^A-Za-z]", "", name_raw.split()[0]) if name_raw.split() else ""
    letters = re.sub(r"[^A-Za-z]", "", name_raw)
    name4 = (first if len(first) >= 4 else letters)[:4]
    dob = _parse_dob(_meta_get(meta, "dob", "date_of_birth", "birth_date"))
    pan = re.sub(r"[^A-Za-z0-9]", "", str(_meta_get(meta, "pan", "pan_number") or "")).upper()
    acct = _digits(_meta_get(meta, "account_number", "account_no", "account"))
    cust = re.sub(r"\s", "", str(_meta_get(meta, "customer_id", "crn", "cif", "customer_number") or ""))
    mobile = _digits(_meta_get(meta, "mobile", "phone", "mobile_number"))[-10:]

    forms: Dict[str, str] = {}
    if dob:
        dd, mm, yyyy = f"{dob.day:02d}", f"{dob.month:02d}", f"{dob.year:04d}"
        mon = _MON_ABBR[dob.month - 1]
        forms.update({
            "dob_ddmmyyyy": dd + mm + yyyy, "dob_ddmmyy": dd + mm + yyyy[2:], "dob_yyyymmdd": yyyy + mm + dd,
            "dob_ddmm": dd + mm, "dob_ddmonyyyy_upper": dd + mon + yyyy, "dob_ddmonyyyy_lower": dd + mon.lower() + yyyy,
        })
    if len(pan) == 10:
        forms.update({"pan_upper": pan, "pan_lower": pan.lower()})
        if dob:
            forms.update({"pan_upper+dob_ddmmyyyy": pan + forms["dob_ddmmyyyy"],
                          "pan_lower+dob_ddmmyyyy": pan.lower() + forms["dob_ddmmyyyy"]})
    if len(name4) >= 3:
        variants = {"lower": name4.lower(), "upper": name4.upper(), "title": name4.capitalize()}
        for vk, nv in variants.items():
            if dob:
                forms[f"name4_{vk}+dob_ddmm"] = nv + forms["dob_ddmm"]
                forms[f"name4_{vk}+dob_ddmmyy"] = nv + forms["dob_ddmmyy"]
                forms[f"name4_{vk}+dob_yyyy"] = nv + f"{dob.year:04d}"
            if len(acct) >= 4:
                forms[f"name4_{vk}+account_last4"] = nv + acct[-4:]
            if len(mobile) >= 4:
                forms[f"name4_{vk}+mobile_last4"] = nv + mobile[-4:]
    if len(mobile) >= 5:
        forms["mobile"] = mobile
        if dob:
            forms["mobile_last5+dob_ddmmyy"] = mobile[-5:] + forms["dob_ddmmyy"]
            forms["mobile_last4+dob_ddmm"] = mobile[-4:] + forms["dob_ddmm"]
    if cust:
        forms["customer_id"] = cust
        if dob:
            forms["customer_id+dob_ddmm"] = cust + forms["dob_ddmm"]
    if len(acct) >= 4:
        forms["account_number"] = acct
        forms["account_last4"] = acct[-4:]
        if len(acct) >= 6:
            forms["account_last6"] = acct[-6:]
        if dob:
            forms["account_last4+dob_ddmm"] = acct[-4:] + forms["dob_ddmm"]
            forms["dob_ddmmyyyy+account_last4"] = forms["dob_ddmmyyyy"] + acct[-4:]

    generic = (
        "dob_ddmmyyyy", "dob_ddmmyy", "name4_lower+dob_ddmm", "name4_upper+dob_ddmm", "customer_id",
        "pan_upper", "pan_lower", "mobile_last5+dob_ddmmyy", "account_number", "dob_yyyymmdd", "dob_ddmm",
        "name4_title+dob_ddmm", "name4_lower+dob_ddmmyy", "name4_upper+dob_ddmmyy", "pan_upper+dob_ddmmyyyy",
        "pan_lower+dob_ddmmyyyy", "dob_ddmonyyyy_upper", "dob_ddmonyyyy_lower", "name4_lower+dob_yyyy",
        "name4_upper+dob_yyyy", "name4_lower+account_last4", "name4_upper+account_last4",
        "name4_lower+mobile_last4", "name4_upper+mobile_last4", "mobile_last4+dob_ddmm", "mobile",
        "customer_id+dob_ddmm", "account_last4", "account_last6", "account_last4+dob_ddmm",
        "dob_ddmmyyyy+account_last4", "name4_title+dob_ddmmyy", "name4_title+dob_yyyy",
        "name4_title+account_last4", "name4_title+mobile_last4",
    )
    bank = re.sub(r"[^A-Z]", "", str(_meta_get(meta, "bank", "bank_name") or "").upper())
    preferred: Tuple[str, ...] = ()
    for key, pref in _BANK_PASSWORD_PREFERENCE.items():
        if bank.startswith(key) or (bank and key in bank):
            preferred = pref
            break
    for label in preferred + generic:
        if label in forms:
            add(label, forms[label])
    for label in sorted(forms):
        add(label, forms[label])
    return out


def _rc4(key: bytes, data: bytes) -> bytes:
    sbox = list(range(256))
    j = 0
    klen = len(key)
    for i in range(256):
        j = (j + sbox[i] + key[i % klen]) & 0xFF
        sbox[i], sbox[j] = sbox[j], sbox[i]
    out = bytearray(len(data))
    i = j = 0
    for k, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + sbox[i]) & 0xFF
        sbox[i], sbox[j] = sbox[j], sbox[i]
        out[k] = byte ^ sbox[(sbox[i] + sbox[j]) & 0xFF]
    return bytes(out)


_ENCRYPT_REF_AT_RE = re.compile(rb"/Encrypt\s*(?:(\d+)\s+\d+\s+R|(<<))")
_ID_FIRST_RE = re.compile(rb"/ID\s*\[\s*(<[0-9A-Fa-f\s]*>|\((?:\\.|[^\\)])*\))")


def _pdf_string_bytes(val: Optional[bytes]) -> Optional[bytes]:
    if not val:
        return None
    v = val.strip()
    if v.startswith(b"<") and not v.startswith(b"<<"):
        hx = _NON_HEX_BYTES_RE.sub(b"", v[1:].split(b">")[0])
        return bytes.fromhex((hx + b"0").decode() if len(hx) % 2 else hx.decode())
    if v.startswith(b"("):
        return _unescape_pdf_literal(v[1:-1] if v.endswith(b")") else v[1:])
    return None


def _dict_string(body: bytes, key: bytes) -> Optional[bytes]:
    """Decoded value of a string-valued `/key` (hex `<..>` or literal `(..)` with nesting and escapes)."""
    m = re.search(rb"/" + re.escape(key) + rb"(?![A-Za-z0-9#_.-])\s*", body)
    if not m:
        return None
    i = m.end()
    if body.startswith(b"<", i) and not body.startswith(b"<<", i):
        end = body.find(b">", i)
        return _pdf_string_bytes(body[i : end + 1]) if end > 0 else None
    if body.startswith(b"(", i):
        depth = 0
        j = i
        while j < len(body):
            c = body[j]
            if c == 0x5C:  # backslash
                j += 2
                continue
            if c == 0x28:
                depth += 1
            elif c == 0x29:
                depth -= 1
                if depth == 0:
                    return _unescape_pdf_literal(body[i + 1 : j])
            j += 1
    return None


def _encrypt_params(pdf_bytes: bytes) -> Optional[Dict[str, Any]]:
    m = None
    for m in _ENCRYPT_REF_AT_RE.finditer(pdf_bytes):
        pass
    if m is None:
        return None
    if m.group(1):
        enc = _parse_pdf_objects(pdf_bytes).get(int(m.group(1)), b"")
    else:
        enc = pdf_bytes[m.start(2):_balanced_end(pdf_bytes, m.start(2), _DICT_DELIM_RE, b"<<", b">>")]
    if b"/Standard" not in enc:
        return None
    cf = _dict_value(enc, b"CF")
    if cf and cf.startswith(b"<<"):
        enc = enc.replace(cf, b"<< >>", 1)  # top-level keys only (/CF /StdCF has its own /Length in bytes)

    def _int(key: bytes, default: int) -> int:
        v = _dict_value(enc, key)
        return int(v) if v and _INT_AT_RE.fullmatch(v) else default
    o_val = _dict_string(enc, b"O")
    u_val = _dict_string(enc, b"U")
    idm = None
    for idm in _ID_FIRST_RE.finditer(pdf_bytes):
        pass
    file_id = _pdf_string_bytes(idm.group(1)) if idm else b""
    if o_val is None or u_val is None:
        return None
    em = _dict_value(enc, b"EncryptMetadata")
    v_num = _int(b"V", 0)
    return {
        "V": v_num, "R": _int(b"R", 2), "P": _int(b"P", 0), "Length": 128 if v_num >= 4 else _int(b"Length", 40),
        "O": o_val, "U": u_val, "ID": file_id or b"", "EncryptMetadata": not (em and em.strip() == b"false"),
    }


def _pw_bytes_legacy(password: str) -> bytes:
    try:
        raw = password.encode("latin-1")
    except UnicodeEncodeError:
        raw = password.encode("utf-8")
    return (raw + _PW_PAD)[:32]


def _legacy_user_key(pw32: bytes, prm: Dict[str, Any]) -> bytes:
    r = prm["R"]
    n = 5 if r == 2 else max(5, min(16, prm["Length"] // 8))
    h = hashlib.md5(pw32 + prm["O"][:32] + (prm["P"] & 0xFFFFFFFF).to_bytes(4, "little") + prm["ID"])
    if r >= 4 and not prm["EncryptMetadata"]:
        h.update(b"\xff\xff\xff\xff")
    key = h.digest()
    if r >= 3:
        for _ in range(50):
            key = hashlib.md5(key[:n]).digest()
    return key[:n]


def _legacy_user_ok(pw32: bytes, prm: Dict[str, Any]) -> bool:
    key = _legacy_user_key(pw32, prm)
    if prm["R"] == 2:
        return _rc4(key, _PW_PAD) == prm["U"][:32]
    x = _rc4(key, hashlib.md5(_PW_PAD + prm["ID"]).digest())
    for i in range(1, 20):
        x = _rc4(bytes(b ^ i for b in key), x)
    return x[:16] == prm["U"][:16]


def check_pdf_password(pdf_bytes: bytes, password: str) -> Optional[bool]:
    """
    Check a user or owner password against a Standard Security Handler in pure stdlib (RC4/MD5 for R2-R4,
    SHA-256 for R5), so many candidates can be tried in memory without spawning processes.
    Returns True / False, or None when the file is not encrypted with a handler this can check (e.g. R6).
    """
    prm = _encrypt_params(pdf_bytes)
    if prm is None:
        return None
    r = prm["R"]
    if r in (2, 3, 4):
        pw32 = _pw_bytes_legacy(password)
        if _legacy_user_ok(pw32, prm):
            return True
        # Owner password: recover the user password from /O (Algorithm 7) and test it.
        key = hashlib.md5(pw32).digest()
        n = 5 if r == 2 else max(5, min(16, prm["Length"] // 8))
        if r >= 3:
            for _ in range(50):
                key = hashlib.md5(key).digest()
        key = key[:n]
        if r == 2:
            user32 = _rc4(key, prm["O"][:32])
        else:
            user32 = prm["O"][:32]
            for i in range(19, -1, -1):
                user32 = _rc4(bytes(b ^ i for b in key), user32)
        return _legacy_user_ok(user32, prm)
    if r == 5:
        pw = password.encode("utf-8")[:127]
        u, o = prm["U"], prm["O"]
        if len(u) >= 48 and hashlib.sha256(pw + u[32:40]).digest() == u[:32]:
            return True
        if len(o) >= 48 and len(u) >= 48 and hashlib.sha256(pw + o[32:40] + u[:48]).digest() == o[:32]:
            return True
        return False
    return None


def _pikepdf_module() -> Any:
    try:
        import pikepdf  # type: ignore
    except ImportError:
        return None
    return pikepdf


def resolve_pdf_password(
    pdf_bytes: bytes,
    candidates: Sequence[Union[str, Tuple[str, str]]],
) -> Dict[str, Any]:
    """
    Try candidate passwords in memory (pikepdf when installed, else the stdlib handler check, else qpdf with a
    0600 password file). The empty user password is always tried first (owner-password-only files).

    Returns {"status": "ok" | "no_match" | "not_encrypted" | "unavailable", "label": formula label or None,
    "password": the matching password (keep it in memory only; never log or persist it), "tried": int}.
    """
    if not _is_pdf_encrypted(pdf_bytes):
        return {"status": "not_encrypted", "label": None, "password": None, "tried": 0}
    norm: List[Tuple[str, str]] = [("empty_user_password", "")]
    seen = {""}
    for i, c in enumerate(candidates or (), start=1):
        label, pw = (c if isinstance(c, tuple) else (f"candidate_{i}", c))
        if isinstance(pw, str) and pw not in seen:
            seen.add(pw)
            norm.append((str(label), pw))
    pikepdf = _pikepdf_module()
    tried = 0
    unknown = 0
    for label, pw in norm[: MAX_PASSWORD_CANDIDATES + 1]:
        tried += 1
        if pikepdf is not None:
            try:
                with pikepdf.open(io.BytesIO(pdf_bytes), password=pw):
                    pass
                return {"status": "ok", "label": label, "password": pw, "tried": tried}
            except pikepdf.PasswordError:
                continue
            except Exception:
                pikepdf = None  # not a pikepdf-readable file: fall back below
        ok = check_pdf_password(pdf_bytes, pw)
        if ok is None:
            _dec, status = _decrypt_in_process(pdf_bytes, pw or None)
            if status == "ok":
                return {"status": "ok", "label": label, "password": pw, "tried": tried}
            if status in ("wrong_password", "password_required"):
                continue
            unknown += 1
            continue
        if ok:
            return {"status": "ok", "label": label, "password": pw, "tried": tried}
    status = "unavailable" if unknown == tried else "no_match"
    return {"status": status, "label": None, "password": None, "tried": tried}


# ---------------------------------------------------------------------------
# Page raster for the Draw Tool (PDF-21)
# ---------------------------------------------------------------------------

DRAW_RENDER_DPI = 110
DRAW_MIN_DPI = 36
DRAW_MAX_DPI = 300


class PageRenderError(Exception):
    """
    A page could not be rendered. `code` is one of "renderer_unavailable" (no Poppler/Ghostscript),
    "password_required", "wrong_password", "page_out_of_range", "corrupt", "render_failed".
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def render_page_png(
    pdf_bytes: bytes,
    page: int = 1,
    dpi: int = DRAW_RENDER_DPI,
    password: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Rasterise one page (1-based) as a grayscale PNG for the Draw Tool overlay.

    Encrypted files are decrypted in memory first (pikepdf/qpdf), so the password reaches no subprocess argv;
    only when neither is installed is it passed to the renderer. Rendering uses Poppler `pdftoppm` (PDF on
    stdin), else Ghostscript. `dpi` is clamped to 36..300 (and lowered for very large pages).

    Returns {"png": bytes, "content_type": "image/png", "page": int, "page_count": int, "dpi": int,
    "width_px": int, "height_px": int, "page_width_pt": float, "page_height_pt": float}; the overlay maps a
    pixel (px, py) to PDF points as (px * page_width_pt / width_px, page_height_pt - py * page_height_pt /
    height_px), the same space as `PdfExtractionResult.tokens` and Draw Tool splits/bbox in points.
    Raises PageRenderError.
    """
    try:
        page = int(page)
        dpi = int(dpi)
    except (TypeError, ValueError):
        raise PageRenderError("page_out_of_range", "Page and dpi must be integers.") from None
    if page < 1:
        raise PageRenderError("page_out_of_range", "Pages are numbered from 1.")
    dpi = max(DRAW_MIN_DPI, min(DRAW_MAX_DPI, dpi))
    if not pdf_bytes.lstrip()[:5].startswith(b"%PDF"):
        raise PageRenderError("corrupt", "The file is not a PDF.")
    render_pw: Optional[str] = None
    if _is_pdf_encrypted(pdf_bytes):
        dec, status = _decrypt_in_process(pdf_bytes, password)
        if status == "wrong_password" and password:
            dec_e, status_e = _decrypt_in_process(pdf_bytes, None)
            if status_e == "ok":
                dec, status = dec_e, status_e
        if status == "ok" and dec is not None:
            pdf_bytes = dec
        elif status in ("wrong_password", "password_required"):
            raise PageRenderError(status, "Incorrect PDF password." if status == "wrong_password"
                                  else "This PDF is password-protected; supply its password.")
        elif status == "corrupt":
            raise PageRenderError("corrupt", "The encrypted PDF could not be decrypted (damaged or unsupported "
                                             "encryption).")
        else:
            render_pw = password
    if not (_poppler_bin("pdftoppm") or _find_binary("gs", ("/opt/homebrew/bin/gs", "/usr/local/bin/gs", "/usr/bin/gs"))):
        raise PageRenderError(
            "renderer_unavailable",
            "Page rendering needs Poppler (pdftoppm) or Ghostscript on the server; install poppler-utils.",
        )
    sizes: List[Tuple[float, float]] = []
    if render_pw is None:
        py = _extract_pure_python(pdf_bytes, with_text=False)
        if py.page_tree_ok:
            sizes = list(py.page_sizes)
    if sizes:
        page_count = len(sizes)
    else:
        from cyphersol.ingestion.ocr_engine import pdf_page_count
        page_count = pdf_page_count(pdf_bytes, password=render_pw)
    if page_count and page > page_count:
        raise PageRenderError("page_out_of_range", f"Page {page} does not exist (the PDF has {page_count}).")
    size: Optional[Tuple[float, float]] = sizes[page - 1] if sizes else None
    png = render_pdf_page_png(pdf_bytes, page, dpi=dpi, password=render_pw, page_size=size)
    if png is None:
        if _is_pdf_encrypted(pdf_bytes):
            raise PageRenderError("wrong_password" if password else "password_required",
                                  "The page could not be decrypted for rendering.")
        raise PageRenderError("render_failed", f"Page {page} could not be rendered.")
    w = int.from_bytes(png[16:20], "big")
    h = int.from_bytes(png[20:24], "big")
    if size is None:
        size = (w * 72.0 / dpi, h * 72.0 / dpi)
    return {
        "png": png, "content_type": "image/png", "page": page, "page_count": page_count or page,
        "dpi": int(round(w * 72.0 / size[0])) if size[0] else dpi, "width_px": w, "height_px": h,
        "page_width_pt": float(size[0]), "page_height_pt": float(size[1]),
    }


def extract_pdf_data(
    pdf_bytes: bytes,
    password: Optional[str] = None,
    column_splits: Optional[List[float]] = None,
    bbox: Optional[List[float]] = None,
    *,
    password_candidates: Optional[Sequence[Union[str, Tuple[str, str]]]] = None,
) -> PdfExtractionResult:
    """
    Extract text, positioned tokens, and table rows from native, scanned, and mixed PDFs.

      0. A Draw Tool template (`column_splits`, `bbox`) is validated first; a malformed one returns
         error="invalid_template" with the reason in `warnings` (see `validate_draw_template`).
      1. Encrypted PDFs are decrypted in memory (pikepdf, else qpdf); otherwise the password is forwarded to
         Poppler and the OCR renderer (with a warning). `password_candidates` ([(label, password)] from
         `generate_password_candidates`, or plain strings) are tried in memory after `password`; the formula
         that worked is reported in `password_label`. Missing/wrong passwords and corrupt files are reported
         in `error`.
      2. The native text layer comes from Poppler `pdftotext -bbox-layout` whenever it is installed, with the
         linear pure-Python content-stream interpreter as fallback.
      3. Pages without a usable text layer (see `_page_needs_ocr`) are OCR'd one by one and merged back in
         page order; `ocr_pages` lists them, with the confidence gate reported in `ocr_confidence_by_page`,
         `low_confidence_pages` and `ocr_rejected_pages`.
    """
    warnings: List[str] = []
    if column_splits is not None or bbox is not None:
        try:
            column_splits, bbox = validate_draw_template(column_splits, bbox)
        except DrawTemplateError as exc:
            return PdfExtractionResult([], [], [], [], error="invalid_template", warnings=[str(exc)])
    text_deadline = time.monotonic() + PDF_TEXT_DEADLINE_S
    encrypted = _is_pdf_encrypted(pdf_bytes)
    password_label: Optional[str] = "supplied" if (encrypted and password) else None
    decrypted_in_process = False
    if encrypted and password_candidates:
        cands: List[Union[str, Tuple[str, str]]] = [("supplied", password)] if password else []
        cands.extend(password_candidates)
        found = resolve_pdf_password(pdf_bytes, cands)
        if found["status"] == "ok":
            password, password_label = found["password"] or None, found["label"]
        elif found["status"] == "no_match":
            return PdfExtractionResult([], [], [], [], is_encrypted=True,
                                       error="wrong_password",
                                       warnings=[f"None of the {found['tried']} candidate password(s) opened the file."])
    if encrypted:
        dec, status = _decrypt_in_process(pdf_bytes, password)
        if status == "wrong_password" and password:
            # Owner-password-only PDFs (empty user password) open without one: a case-level password sent
            # with every upload must not make them fail.
            dec_e, status_e = _decrypt_in_process(pdf_bytes, None)
            if status_e == "ok" and dec_e is not None:
                dec, status = dec_e, status_e
                password_label = "empty_user_password"
        if status == "ok" and dec is not None:
            pdf_bytes, encrypted, decrypted_in_process = dec, False, True
        elif status in ("wrong_password", "password_required"):
            return PdfExtractionResult([], [], [], [], is_encrypted=True, error=status)
        elif status == "corrupt":
            return PdfExtractionResult([], [], [], [], is_encrypted=True, error="corrupt", warnings=[
                "The encrypted PDF could not be decrypted in-process (damaged or unsupported encryption); the "
                "password was not passed to any external program."
            ])
    render_password = password if encrypted else None
    if encrypted and render_password:
        warnings.append(
            "No in-process PDF decryption library (pikepdf/qpdf) is installed: the password was passed to the "
            "Poppler renderer on its command line."
        )

    pop = _run_pdftotext(pdf_bytes, password=render_password,
                         timeout=min(POPPLER_TIMEOUT_S, text_deadline - time.monotonic()))
    if encrypted and pop.status == "password" and render_password:
        pop_e = _run_pdftotext(pdf_bytes, password=None,
                               timeout=min(POPPLER_TIMEOUT_S, text_deadline - time.monotonic()))
        if pop_e.status != "password":
            pop, render_password = pop_e, None
            password_label = "empty_user_password"
    if pop.status == "timeout":
        warnings.append("Poppler text extraction exceeded its time limit.")
    if encrypted:
        if pop.status == "password":
            return PdfExtractionResult([], [], [], [], is_encrypted=True,
                                       error="wrong_password" if password else "password_required")
        if pop.status == "missing":
            return PdfExtractionResult([], [], [], [], is_encrypted=True,
                                       error="decryption_unavailable" if password else "password_required")
        if pop.status == "corrupt":
            return PdfExtractionResult([], [], [], [], error="corrupt")

    # Pure-Python pass: page tree, scan-image map, and fallback text (an encrypted file's streams are
    # ciphertext, so only its structure is read).
    pop_has_text = pop.status == "ok" and any(t.text.strip() for t in pop.tokens)
    textless_pop_pages: List[int] = []
    if pop_has_text:
        with_tokens = {t.page for t in pop.tokens if t.text.strip()}
        textless_pop_pages = [p for p in range(1, len(pop.page_sizes) + 1) if p not in with_tokens]
    work = _WorkBudget(deadline=text_deadline)
    py = _extract_pure_python(pdf_bytes, with_text=not encrypted and not pop_has_text, work=work,
                              inline_image_pages=textless_pop_pages if not encrypted else None)
    warnings.extend(py.warnings)

    py_has_text = any(t.text.strip() for page in py.tokens_by_page for t in page)
    if pop_has_text:
        text_source: Optional[str] = "poppler"
        native_pages: List[List[PdfTextToken]] = [[] for _ in pop.page_sizes]
        for t in pop.tokens:
            native_pages[t.page - 1].append(t)
    elif py_has_text:
        text_source = "pure_python"
        native_pages = [list(p) for p in py.tokens_by_page]
    else:
        text_source = None
        native_pages = []

    n_pages = max(len(pop.page_sizes) if pop.status == "ok" else 0, len(py.page_sizes), len(native_pages))
    if n_pages == 0:
        if pop.status in ("corrupt", "error") or py.object_count == 0:
            return PdfExtractionResult([], [], [], [], error="corrupt", warnings=warnings)
        n_pages = 1
    while len(native_pages) < n_pages:
        native_pages.append([])
    page_sizes: List[Tuple[float, float]] = list(pop.page_sizes if pop.status == "ok" and pop.page_sizes else py.page_sizes)
    if not page_sizes:
        page_sizes = [(595.0, 842.0)]
    while len(page_sizes) < n_pages:
        page_sizes.append(page_sizes[-1])

    # Per-page OCR routing
    doc_has_images = bool(_IMAGE_MARKER_RE.search(pdf_bytes))
    doc_has_text = any(t.text.strip() for page in native_pages for t in page)
    page_image: List[Optional[bool]] = [None] * n_pages
    if py.page_tree_ok and len(py.page_has_scan) == n_pages:
        page_scan = list(py.page_has_scan)
        if len(py.page_has_image) == n_pages and not encrypted:
            page_image = list(py.page_has_image)
    else:
        page_scan = [_has_full_page_scan_image(pdf_bytes)] * n_pages
    ocr_candidates = [
        p for p in range(1, n_pages + 1)
        if _page_needs_ocr(native_pages[p - 1], page_scan[p - 1], doc_has_images, doc_has_text, page_image[p - 1])
    ]
    if len(ocr_candidates) > MAX_OCR_PAGES:
        warnings.append(
            f"{len(ocr_candidates)} page(s) need OCR; only the first {MAX_OCR_PAGES} were processed."
        )
        ocr_candidates = ocr_candidates[:MAX_OCR_PAGES]

    # Optional pdfplumber fallback for documents with no text found by either extractor
    if not doc_has_text and not column_splits and not bbox and not encrypted:
        plumber = _pdfplumber_rows(pdf_bytes, password)
        if plumber is not None:
            plumber.warnings = warnings
            return plumber

    ocr_tokens_by_page: Dict[int, List[PdfTextToken]] = {}
    ocr_text_by_page: Dict[int, str] = {}
    ocr_engine: Optional[str] = None
    ocr_conf: Dict[int, float] = {}
    low_conf_pages: List[int] = []
    rejected_pages: Dict[int, str] = {}
    if ocr_candidates:
        ocr_res = ocr_pdf_bytes(pdf_bytes, page_sizes=page_sizes, pages=ocr_candidates, password=render_password,
                                deadline=time.monotonic() + OCR_DOC_DEADLINE_S)
        ocr_engine = ocr_res.get("engine") or "none"
        for t in ocr_res.get("tokens", []):
            ocr_tokens_by_page.setdefault(t.page, []).append(
                PdfTextToken(page=t.page, x=t.x, y=t.y, text=t.text, x_end=t.x_end)
            )
        by_page = ocr_res.get("pages_text_by_page") or {}
        for p, toks in ocr_tokens_by_page.items():
            ocr_text_by_page[p] = by_page.get(p) or " ".join(t.text for t in toks)
        ocr_conf = {int(k): float(v) for k, v in (ocr_res.get("confidence_by_page") or {}).items()}
        low_conf_pages = sorted(int(p) for p in (ocr_res.get("low_confidence_pages") or []))
        rejected_pages = {int(k): str(v) for k, v in (ocr_res.get("rejected_pages") or {}).items()}
        if ocr_engine == "unavailable":
            warnings.append(
                "No OCR engine is available for the scanned page(s) " + ", ".join(str(p) for p in ocr_candidates)
                + ": install Tesseract (the built-in glyph matcher only reads CypherSOL test fixtures)."
            )
        if rejected_pages:
            warnings.append(
                "OCR text was discarded as unreliable for page(s) "
                + ", ".join(f"{p} ({r})" for p, r in sorted(rejected_pages.items())) + "."
            )
        if low_conf_pages:
            warnings.append(
                "Low OCR confidence on page(s) " + ", ".join(str(p) for p in low_conf_pages)
                + "; verify those rows against the source document."
            )
        skipped = list(ocr_res.get("skipped_pages") or [])
        if skipped:
            warnings.append(
                "OCR time limit reached; page(s) " + ", ".join(str(p) for p in skipped) + " were not OCR'd."
            )
        failed = [p for p in ocr_candidates
                  if p not in ocr_tokens_by_page and p not in skipped and p not in rejected_pages]
        if failed and ocr_engine != "unavailable":
            warnings.append(
                "OCR produced no text for page(s) " + ", ".join(str(p) for p in failed)
                + (f" (engine: {ocr_engine})." if ocr_engine else ".")
            )

    tokens: List[PdfTextToken] = []
    pages_text: List[str] = []
    y_tol_by_page: Dict[int, float] = {}
    for p in range(1, n_pages + 1):
        if p in ocr_tokens_by_page:
            page_toks = ocr_tokens_by_page[p]
            pages_text.append(ocr_text_by_page[p])
            y_tol_by_page[p] = 4.5
        else:
            page_toks = native_pages[p - 1]
            pages_text.append(" ".join(t.text for t in page_toks))
        tokens.extend(page_toks)

    ocr_pages = sorted(ocr_tokens_by_page)
    if ocr_pages and len(ocr_pages) == n_pages:
        text_source = None
    rows = (
        _group_tokens_into_rows(
            tokens,
            y_tolerance=3.5,
            column_splits=column_splits,
            bbox=bbox,
            page_sizes=page_sizes,
            page_y_tolerance=y_tol_by_page,
        )
        if tokens
        else []
    )
    return PdfExtractionResult(
        pages_text=pages_text,
        page_sizes=page_sizes,
        tokens=tokens,
        rows=rows,
        # Only pages whose text actually came from OCR make the statement OCR-derived (PDF-25)
        needs_ocr=bool(ocr_pages),
        ocr_engine=ocr_engine,
        ocr_pages=ocr_pages,
        text_source=text_source,
        warnings=warnings,
        ocr_confidence_by_page=ocr_conf,
        low_confidence_pages=low_conf_pages,
        ocr_rejected_pages=rejected_pages,
        password_label=password_label,
        decrypted_in_process=decrypted_in_process,
    )


def _pdfplumber_rows(pdf_bytes: bytes, password: Optional[str]) -> Optional[PdfExtractionResult]:
    try:
        import pdfplumber  # type: ignore
    except ImportError:
        return None
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes), password=password) as pdf:
            pl_pages_text: List[str] = []
            pl_page_sizes: List[Tuple[float, float]] = []
            pl_rows: List[List[str]] = []
            for p in pdf.pages:
                pl_pages_text.append(p.extract_text() or "")
                pl_page_sizes.append((float(p.width), float(p.height)))
                for table in p.extract_tables() or []:
                    for r in table:
                        cleaned = [(c or "").replace("\n", " ").strip() for c in r]
                        if any(cleaned):
                            pl_rows.append(cleaned)
    except Exception:
        return None
    if not pl_rows:
        return None
    return PdfExtractionResult(pl_pages_text, pl_page_sizes, [], pl_rows, text_source="pdfplumber")


# ---------------------------------------------------------------------------
# Pure-Python Standards-Compliant Native PDF Fixture Generator
# ---------------------------------------------------------------------------

def _pdf_escape(text: str) -> str:
    clean = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    clean = clean.replace("₹", "Rs. ")
    return clean.encode("latin-1", errors="replace").decode("latin-1")


def generate_pdf_statement(
    title_lines: Sequence[str],
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    col_x_positions: Optional[Sequence[float]] = None,
) -> bytes:
    """
    Generate a clean, standards-compliant multi-page PDF 1.4 bank statement using pure Python.
    Uses standard Helvetica Type1 fonts readable by all PDF viewers and `extract_pdf_data()`.
    """
    page_w, page_h = 595.0, 842.0
    if col_x_positions is None:
        col_x_positions = [32.0, 96.0, 305.0, 385.0, 445.0, 515.0]

    pages_ops: List[str] = []
    rows_per_page = 38
    chunks = [rows[i : i + rows_per_page] for i in range(0, max(1, len(rows)), rows_per_page)]

    for page_num, chunk in enumerate(chunks, start=1):
        ops: List[str] = []
        y = 805.0
        ops.append("BT")
        ops.append("/F2 12 Tf")
        for idx, line in enumerate(title_lines):
            font = "/F2 12 Tf" if idx == 0 else "/F1 9 Tf"
            ops.append(font)
            ops.append(f"1 0 0 1 32 {y:.1f} Tm ({_pdf_escape(line)}) Tj")
            y -= 15.0 if idx == 0 else 13.0
        ops.append("ET")

        y -= 8.0
        ops.append("0.06 0.30 0.51 rg")
        ops.append(f"30 {y - 4:.1f} 535 18 re f")
        ops.append("1 1 1 rg")
        ops.append("BT /F2 8.5 Tf")
        for c_i, col_name in enumerate(columns):
            cx = col_x_positions[min(c_i, len(col_x_positions) - 1)]
            ops.append(f"1 0 0 1 {cx:.1f} {y + 1:.1f} Tm ({_pdf_escape(str(col_name))}) Tj")
        ops.append("ET")
        ops.append("0 0 0 rg")
        y -= 18.0

        for r_idx, row in enumerate(chunk):
            if r_idx % 2 == 1:
                ops.append("0.96 0.97 0.99 rg")
                ops.append(f"30 {y - 4:.1f} 535 15 re f")
                ops.append("0 0 0 rg")
            ops.append("BT /F1 8 Tf")
            for c_i, val in enumerate(row):
                cx = col_x_positions[min(c_i, len(col_x_positions) - 1)]
                txt = str(val if val is not None else "")
                if c_i == 1 and len(txt) > 48:
                    txt = txt[:48]
                ops.append(f"1 0 0 1 {cx:.1f} {y:.1f} Tm ({_pdf_escape(txt)}) Tj")
            ops.append("ET")
            y -= 16.0

        ops.append("BT /F1 8 Tf")
        ops.append(f"1 0 0 1 32 24 Tm (CypherSOL Standardized Statement Fixture - Page {page_num} of {len(chunks)}) Tj")
        ops.append("ET")
        pages_ops.append("\n".join(ops))

    objects: List[bytes] = []
    num_pages = len(pages_ops)
    page_obj_ids = [5 + i * 2 for i in range(num_pages)]
    kids_str = " ".join(f"{pid} 0 R" for pid in page_obj_ids)

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [ {kids_str} ] /Count {num_pages} >>".encode("ascii"))
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

    for i, stream_str in enumerate(pages_ops):
        content_id = 6 + i * 2
        stream_bytes = stream_str.encode("latin-1", errors="replace")
        page_dict = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w:.0f} {page_h:.0f}] "
            f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        content_obj = (
            f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii")
            + stream_bytes
            + b"\nendstream"
        )
        objects.append(page_dict)
        objects.append(content_obj)

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: List[int] = [0]
    for idx, obj_body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{idx} 0 obj\n".encode("ascii"))
        out.extend(obj_body)
        out.extend(b"\nendobj\n")

    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_pos}\n%%EOF\n"
        ).encode("ascii")
    )
    return bytes(out)
