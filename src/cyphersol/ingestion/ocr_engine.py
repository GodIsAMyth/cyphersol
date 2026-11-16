"""100% Offline Multi-Backend Scanned PDF & Image Statement OCR Engine.

Backends tried in priority order (all zero-API, 100% local):
  1. Tesseract CLI (`tesseract <img_path> stdout --psm 6 tsv`) when installed.
  2. macOS Apple Vision (`VNRecognizeTextRequest` via ctypes) on Darwin hosts when GPU/IOSurface is permitted.
  3. Pure-Python Embedded Raster Extractor & Multi-Scale Glyph Template OCR (works everywhere with zero dependencies,
     decoding PDF /FlateDecode bitmaps directly or via Poppler `pdftoppm`/Ghostscript PGM renders, plus PNG/PGM/BMP files).
"""

from __future__ import annotations

import contextlib
import ctypes
from ctypes import byref, c_bool, c_char_p, c_double, c_float, c_long, c_ubyte, c_uint32, c_ulong, c_void_p, Structure
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple


@dataclass
class OcrWordToken:
    page: int
    x: float
    y: float
    text: str
    x_end: float = 0.0
    # Recognition confidence in [0, 1]; -1.0 when the engine reports none.
    conf: float = -1.0


_LUT_GAMMA_DARK = bytes(min(255, int(round(((i / 255.0) ** 2.5) * 255.0))) for i in range(256))
_LUT_WATERMARK_WHITE = bytes(
    0 if i <= 70 else (255 if i >= 175 else int(round((i - 70) * 255.0 / 105.0)))
    for i in range(256)
)
_DATE_TOKEN_PREFIX_RE = re.compile(
    r"^[:;|,.\[\]()_*•\-~]*(?:[FPT7](?=[0-3]\d[./-]\d{2}[./-]\d{2,4}(?:\D|$)))?(\d{2}[./-]\d{2}[./-]\d{2,4})(.*)$"
)
_GLUED_AMOUNTS_RE = re.compile(
    r"^(\d[\d,]*\.\d{2}(?:CR|DR)?)(\d[\d,]*\.\d{2}(?:CR|DR)?)$",
    re.IGNORECASE,
)
_STRICT_DATE_TOK_RE = re.compile(r"^\d{2}[./-]\d{2}[./-]\d{2,4}$")
_STRICT_AMT_TOK_RE = re.compile(r"^(?:(?:\d{1,2},)*\d{1,3},\d{3}|\d+)\.\d{2}(?:CR|DR)?$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Resource limits, bounded inflate, private temp files and page rendering
# ---------------------------------------------------------------------------

OCR_RENDER_DPI = 300
OCR_PAGE_TIMEOUT_S = 60
MAX_STREAM_INFLATE_BYTES = 64 << 20      # per decoded PDF stream / PNG IDAT
MAX_DOCUMENT_INFLATE_BYTES = 256 << 20   # per PDF document, across all streams
MAX_IMAGE_PIXELS = 40_000_000            # ~ A4 at 600 DPI; larger rasters are downsampled or skipped

# OCR confidence gate (PDF-11). Confidences are normalised to [0, 1].
OCR_LOW_CONFIDENCE = 0.70                # pages below this are kept but listed in "low_confidence_pages"
OCR_REJECT_CONFIDENCE = 0.40             # pages below this are discarded (reported in "rejected_pages")
GLYPH_MIN_CONFIDENCE = 0.86              # the 5x7 glyph matcher must clear this to be trusted at all
GLYPH_MIN_IMAGE_SIDE = 300               # embedded rasters smaller than this (logos, stamps) are not OCR'd
# The pure-Python 5x7 glyph matcher only reads CypherSOL's own fixture font. It is used only for documents
# carrying this producer marker (written by `generate_scanned_pdf_statement`) or when the operator sets
# CYPHERSOL_ALLOW_GLYPH_OCR=1; anything else needs Tesseract or Apple Vision.
FIXTURE_PRODUCER_MARKER = b"CypherSOL-5x7-Fixture"
GLYPH_OCR_ENV = "CYPHERSOL_ALLOW_GLYPH_OCR"

_ENCRYPT_REF_RE = re.compile(rb"/Encrypt\s*(?:\d+\s+\d+\s+R|<<)")
_PDFINFO_PAGES_RE = re.compile(r"^Pages:\s+(\d+)", re.MULTILINE)
_PAGE_TYPE_RE = re.compile(rb"/Type\s*/Page\b(?!\s*s\b)")


class InflateBudget:
    """Per-document decompression allowance shared by every stream decoded from one PDF."""

    __slots__ = ("remaining", "exhausted")

    def __init__(self, limit: int = MAX_DOCUMENT_INFLATE_BYTES) -> None:
        self.remaining = int(limit)
        self.exhausted = False

    def charge(self, n: int) -> None:
        self.remaining -= n
        if self.remaining <= 0:
            self.remaining = 0
            self.exhausted = True


def _inflate_bounded(
    data: bytes,
    max_out: int = MAX_STREAM_INFLATE_BYTES,
    budget: Optional[InflateBudget] = None,
) -> Optional[bytes]:
    """
    Inflate a zlib/Flate stream without ever producing more than `max_out` bytes (or the document budget).
    Returns None when the stream is corrupt or would exceed the limit (decompression-bomb guard).
    Truncated streams (missing end marker) return what could be decoded, as PDF readers do.
    """
    limit = max_out if budget is None else min(max_out, budget.remaining)
    if limit <= 0:
        if budget is not None:
            budget.exhausted = True
        return None
    dobj = zlib.decompressobj()
    try:
        out = dobj.decompress(data, limit + 1)
    except zlib.error:
        return None
    if len(out) > limit:
        if budget is not None:
            budget.charge(limit)  # the work was done; repeated bombs drain the document allowance
        return None
    if budget is not None:
        budget.charge(len(out))
    return out


@contextlib.contextmanager
def private_tempdir(prefix: str = "cyphersol-") -> Iterator[str]:
    """Owner-only (0700) scratch directory; files are overwritten with zeros and removed on exit."""
    td = tempfile.mkdtemp(prefix=prefix)
    os.chmod(td, 0o700)
    try:
        yield td
    finally:
        for root, _dirs, files in os.walk(td):
            for name in files:
                path = os.path.join(root, name)
                try:
                    size = os.path.getsize(path)
                    with open(path, "r+b", buffering=0) as fh:
                        zero = bytes(min(size, 1 << 20))
                        left = size
                        while left > 0:
                            left -= fh.write(zero[: min(left, len(zero))])
                except OSError:
                    pass
        shutil.rmtree(td, ignore_errors=True)


def write_private_file(directory: str, name: str, data: bytes) -> str:
    """Create `directory/name` exclusively with mode 0600 and write `data`; returns the path."""
    path = os.path.join(directory, name)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    return path


def _png_dimensions(png: bytes) -> Optional[Tuple[int, int]]:
    """Read (width, height) from a PNG IHDR header without decoding pixel data."""
    if len(png) < 24 or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
        return None
    w, h = struct.unpack(">II", png[16:24])
    return (w, h) if w > 0 and h > 0 else None


def _is_pdf_encrypted(pdf_bytes: bytes) -> bool:
    return bool(_ENCRYPT_REF_RE.search(pdf_bytes))


def _poppler_bin(name: str) -> Optional[str]:
    return _find_binary(name, (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}"))


def _effective_dpi(dpi: int, page_size: Optional[Tuple[float, float]]) -> int:
    if not page_size:
        return dpi
    pw, ph = page_size
    px = (pw * dpi / 72.0) * (ph * dpi / 72.0)
    if px <= MAX_IMAGE_PIXELS or px <= 0:
        return dpi
    return max(72, int(dpi * math.sqrt(MAX_IMAGE_PIXELS / px)))


def pdf_page_count(pdf_bytes: bytes, password: Optional[str] = None) -> int:
    """Page count via Poppler `pdfinfo` (stdin, no temp file); falls back to counting /Type /Page objects."""
    pdfinfo = _poppler_bin("pdfinfo")
    if pdfinfo:
        cmd = [pdfinfo]
        if password:
            cmd.extend(["-upw", password])
        cmd.append("-")
        try:
            proc = subprocess.run(cmd, input=pdf_bytes, capture_output=True, timeout=30)
            m = _PDFINFO_PAGES_RE.search(proc.stdout.decode("latin-1", errors="replace"))
            if proc.returncode == 0 and m:
                return int(m.group(1))
        except (OSError, subprocess.SubprocessError):
            pass
    return len(_PAGE_TYPE_RE.findall(pdf_bytes))


def render_pdf_page_gray(
    pdf_bytes: bytes,
    page: int,
    dpi: int = OCR_RENDER_DPI,
    password: Optional[str] = None,
    timeout: float = OCR_PAGE_TIMEOUT_S,
    page_size: Optional[Tuple[float, float]] = None,
) -> Optional[Tuple[int, int, bytearray]]:
    """
    Render one PDF page (1-based) to an 8-bit grayscale raster with Poppler (`pdftoppm -gray`, PDF on stdin and
    PGM on stdout, so no plaintext copy of a decrypted statement touches disk) or Ghostscript as fallback.
    The password is forwarded to the renderer. Returns None when the page cannot be rendered.
    """
    dpi = _effective_dpi(dpi, page_size)
    pdftoppm = _poppler_bin("pdftoppm")
    if pdftoppm:
        cmd = [pdftoppm, "-gray", "-r", str(dpi), "-f", str(page), "-l", str(page)]
        if password:
            cmd.extend(["-upw", password])
        cmd.append("-")
        try:
            proc = subprocess.run(cmd, input=pdf_bytes, capture_output=True, timeout=timeout)
            if proc.returncode == 0 and proc.stdout:
                dec = _decode_pnm(proc.stdout)
                if dec is not None:
                    return dec
        except (OSError, subprocess.SubprocessError):
            pass
    gs_bin = _find_binary("gs", ("/opt/homebrew/bin/gs", "/usr/local/bin/gs", "/usr/bin/gs"))
    if gs_bin:
        with private_tempdir() as td:
            pdf_p = write_private_file(td, "in.pdf", pdf_bytes)
            cmd = [gs_bin, "-q", "-dSAFER", "-dNOPAUSE", "-dBATCH", "-sDEVICE=pgmraw", f"-r{dpi}",
                   f"-dFirstPage={page}", f"-dLastPage={page}", "-sOutputFile=-"]
            if password:
                cmd.append(f"-sPDFPassword={password}")
            cmd.append(pdf_p)
            try:
                proc = subprocess.run(cmd, capture_output=True, timeout=timeout)
                if proc.returncode == 0 and proc.stdout:
                    return _decode_pnm(proc.stdout)
            except (OSError, subprocess.SubprocessError):
                pass
    return None


def render_pdf_page_png(
    pdf_bytes: bytes,
    page: int,
    dpi: int = OCR_RENDER_DPI,
    password: Optional[str] = None,
    timeout: float = OCR_PAGE_TIMEOUT_S,
    page_size: Optional[Tuple[float, float]] = None,
) -> Optional[bytes]:
    """Render one page as a grayscale (colour type 0) PNG; see `render_pdf_page_gray`."""
    dec = render_pdf_page_gray(pdf_bytes, page, dpi=dpi, password=password, timeout=timeout, page_size=page_size)
    if dec is None:
        return None
    w, h, gray = dec
    return _gray_bytes_to_png(bytes(gray), w, h)


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _gray_bytes_to_png(gray: bytes, width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + gray[r * width : (r + 1) * width] for r in range(height))
    idat = zlib.compress(raw, level=1)
    return b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", ihdr) + _png_chunk(b"IDAT", idat) + _png_chunk(b"IEND", b"")


def _extract_gray_from_image_bytes(img_bytes: bytes) -> Optional[Tuple[int, int, bytes]]:
    """Decode PNG/JPEG/TIFF image bytes into an 8-bit grayscale buffer via macOS CoreGraphics."""
    if sys.platform != "darwin" or not img_bytes:
        return None
    try:
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
        imageio = ctypes.CDLL("/System/Library/Frameworks/ImageIO.framework/ImageIO")

        class _CGPoint(Structure):
            _fields_ = [("x", c_double), ("y", c_double)]

        class _CGSize(Structure):
            _fields_ = [("width", c_double), ("height", c_double)]

        class _CGRect(Structure):
            _fields_ = [("origin", _CGPoint), ("size", _CGSize)]

        cf.CFDataCreate.argtypes = [c_void_p, c_char_p, c_long]
        cf.CFDataCreate.restype = c_void_p
        cf.CFRelease.argtypes = [c_void_p]
        cf.CFRelease.restype = None

        imageio.CGImageSourceCreateWithData.argtypes = [c_void_p, c_void_p]
        imageio.CGImageSourceCreateWithData.restype = c_void_p
        imageio.CGImageSourceCreateImageAtIndex.argtypes = [c_void_p, c_ulong, c_void_p]
        imageio.CGImageSourceCreateImageAtIndex.restype = c_void_p

        cg.CGImageGetWidth.argtypes = [c_void_p]
        cg.CGImageGetWidth.restype = c_ulong
        cg.CGImageGetHeight.argtypes = [c_void_p]
        cg.CGImageGetHeight.restype = c_ulong
        cg.CGColorSpaceCreateDeviceGray.argtypes = []
        cg.CGColorSpaceCreateDeviceGray.restype = c_void_p
        cg.CGBitmapContextCreate.argtypes = [c_void_p, c_ulong, c_ulong, c_ulong, c_ulong, c_void_p, c_uint32]
        cg.CGBitmapContextCreate.restype = c_void_p
        cg.CGContextSetGrayFillColor.argtypes = [c_void_p, c_double, c_double]
        cg.CGContextSetGrayFillColor.restype = None
        cg.CGContextFillRect.argtypes = [c_void_p, _CGRect]
        cg.CGContextFillRect.restype = None
        cg.CGContextDrawImage.argtypes = [c_void_p, _CGRect, c_void_p]
        cg.CGContextDrawImage.restype = None
        cg.CGContextRelease.argtypes = [c_void_p]
        cg.CGContextRelease.restype = None
        cg.CGColorSpaceRelease.argtypes = [c_void_p]
        cg.CGColorSpaceRelease.restype = None
        cg.CGImageRelease.argtypes = [c_void_p]
        cg.CGImageRelease.restype = None

        cf_data = src = cg_img = cs = ctx = None
        try:
            cf_data = cf.CFDataCreate(None, img_bytes, len(img_bytes))
            if not cf_data:
                return None
            src = imageio.CGImageSourceCreateWithData(cf_data, None)
            if not src:
                return None
            cg_img = imageio.CGImageSourceCreateImageAtIndex(src, 0, None)
            if not cg_img:
                return None
            w = int(cg.CGImageGetWidth(cg_img))
            h = int(cg.CGImageGetHeight(cg_img))
            if w <= 0 or h <= 0 or w * h > 80_000_000:
                return None
            cs = cg.CGColorSpaceCreateDeviceGray()
            if not cs:
                return None
            buf = (c_ubyte * (w * h))()
            ctx = cg.CGBitmapContextCreate(buf, w, h, 8, w, cs, 0)
            if not ctx:
                return None
            rect = _CGRect(_CGPoint(0.0, 0.0), _CGSize(float(w), float(h)))
            cg.CGContextSetGrayFillColor(ctx, 1.0, 1.0)
            cg.CGContextFillRect(ctx, rect)
            cg.CGContextDrawImage(ctx, rect, cg_img)
            return w, h, bytes(buf)
        finally:
            if ctx:
                cg.CGContextRelease(ctx)
            if cs:
                cg.CGColorSpaceRelease(cs)
            if cg_img:
                cg.CGImageRelease(cg_img)
            if src:
                cf.CFRelease(src)
            if cf_data:
                cf.CFRelease(cf_data)
    except Exception:
        return None


def _split_glued_ocr_token(
    y_pt: float, x0: float, x1: float, tok: str, depth: int = 0
) -> List[Tuple[float, float, float, str]]:
    """Split table-glued OCR tokens (e.g. `:05.05.25`, `10.05.25PMSBY`, `01-11-202401-11-2024`) proportionally."""
    tok = tok.strip()
    if not tok or depth > 4:
        return [(y_pt, x0, x1, tok)] if tok else []

    width = max(0.0, x1 - x0)
    total_len = max(len(tok), 1)

    if "|" in tok:
        trimmed = tok.strip("|")
        if not trimmed:
            return []
        if "|" in trimmed and re.search(r"(\d[.,\d]*\|)|(\|\d)|(\|[A-Za-z]{2,})", trimmed):
            out: List[Tuple[float, float, float, str]] = []
            offset = 0
            for part in trimmed.split("|"):
                if part:
                    sx0 = x0 + (offset / total_len) * width
                    sx1 = x0 + ((offset + len(part)) / total_len) * width
                    out.extend(_split_glued_ocr_token(y_pt, sx0, sx1, part, depth + 1))
                offset += len(part) + 1
            return out
        tok = trimmed

    m_date = _DATE_TOKEN_PREFIX_RE.match(tok)
    if m_date:
        date_str, rest = m_date.group(1), m_date.group(2)
        d_start = tok.find(date_str)
        d_end = d_start + len(date_str)
        dx0 = x0 + (d_start / total_len) * width
        dx1 = x0 + (d_end / total_len) * width
        if not rest:
            return [(y_pt, dx0, dx1, date_str)]
        if not re.match(r"^:\d{2}", rest) and (
            re.match(r"^[A-Za-z]", rest) or re.match(r"^\d{2}[./-]\d{2}[./-]\d{2,4}", rest) or rest.startswith(":")
        ):
            clean_rest = rest.lstrip(":;|,")
            if clean_rest:
                rx0 = x0 + ((total_len - len(clean_rest)) / total_len) * width
                return [(y_pt, dx0, dx1, date_str)] + _split_glued_ocr_token(y_pt, rx0, x1, clean_rest, depth + 1)
            return [(y_pt, dx0, dx1, date_str)]

    m_amts = _GLUED_AMOUNTS_RE.match(tok)
    if m_amts:
        a1, a2 = m_amts.group(1), m_amts.group(2)
        mid_x = x0 + (len(a1) / total_len) * width
        return [(y_pt, x0, mid_x, a1), (y_pt, mid_x, x1, a2)]

    return [(y_pt, x0, x1, tok)]


def _vision_token_quality(text: str, pass_idx: int) -> float:
    base = 0.35 if pass_idx == 0 else (0.20 if pass_idx == 1 else 0.15)
    t = text.strip()
    if not t:
        return -10.0
    score = base
    if _STRICT_DATE_TOK_RE.match(t):
        score += 5.0
    elif _STRICT_AMT_TOK_RE.match(t):
        score += 5.0
    elif re.match(r"^\d[\d,]*\.\d{1,3}$", t):
        score += 1.2
    elif re.match(r"^[A-Za-z0-9/.,:@()_&-]+$", t):
        score += 1.0
    noise = len(re.findall(r"[~?^*`|]", t))
    score -= noise * 2.0
    return score


def _merge_multipass_vision_tokens(pass_lists: List[List[OcrWordToken]]) -> List[OcrWordToken]:
    """Deduplicate and merge tokens recovered across raw, gamma-darkened, and watermark-whitened passes."""
    if not pass_lists:
        return []
    if len(pass_lists) == 1:
        return pass_lists[0]

    kept: List[Tuple[int, OcrWordToken]] = [(0, t) for t in pass_lists[0]]

    def _span(t: OcrWordToken) -> Tuple[float, float]:
        x1 = t.x_end if t.x_end > t.x + 1.0 else (t.x + max(6.0, len(t.text) * 4.5))
        return t.x, x1

    for pass_idx in range(1, len(pass_lists)):
        for cand in pass_lists[pass_idx]:
            cx0, cx1 = _span(cand)
            cw = max(1.0, cx1 - cx0)
            matched_indices: List[int] = []
            for idx, (_, kt) in enumerate(kept):
                if abs(kt.y - cand.y) > 4.5:
                    continue
                kx0, kx1 = _span(kt)
                kw = max(1.0, kx1 - kx0)
                inter = min(cx1, kx1) - max(cx0, kx0)
                if inter > 0 and (inter / min(cw, kw) >= 0.35 or inter >= 4.0):
                    matched_indices.append(idx)

            if not matched_indices:
                kept.append((pass_idx, cand))
            elif len(matched_indices) == 1:
                m_idx = matched_indices[0]
                k_pass, k_tok = kept[m_idx]
                if _vision_token_quality(cand.text, pass_idx) > _vision_token_quality(k_tok.text, k_pass) + 0.05:
                    kept[m_idx] = (pass_idx, cand)
            else:
                # Multiple existing tokens occupy this region: replace only if cand has higher total quality (rare)
                pass

    out = [t for _, t in kept]
    out.sort(key=lambda t: (-round(t.y, 1), t.x))
    return out


# Clean, unmistakable 5x7 monochrome bitmap font (ASCII 32..126).
# Used both by `generate_scanned_pdf_statement()` and the normalized glyph template recognizer.
_FONT_5X7: Dict[str, Tuple[str, ...]] = {
    " ": (".....", ".....", ".....", ".....", ".....", ".....", "....."),
    "!": ("..#..", "..#..", "..#..", "..#..", "..#..", ".....", "..#.."),
    '"': (".#.#.", ".#.#.", ".....", ".....", ".....", ".....", "....."),
    "#": (".#.#.", "#####", ".#.#.", ".#.#.", "#####", ".#.#.", "....."),
    "$": ("..#..", ".####", "#.#..", ".###.", "..#.#", "####.", "..#.."),
    "%": ("##...", "##..#", "...#.", "..#..", ".#...", "#..##", "...##"),
    "&": (".##..", "#..#.", "#.#..", ".#...", "#.#.#", "#..#.", ".##.#"),
    "'": ("..#..", "..#..", ".#...", ".....", ".....", ".....", "....."),
    "(": ("...#.", "..#..", ".#...", ".#...", ".#...", "..#..", "...#."),
    ")": (".#...", "..#..", "...#.", "...#.", "...#.", "..#..", ".#..."),
    "*": (".....", "#...#", ".#.#.", "#####", ".#.#.", "#...#", "....."),
    "+": (".....", "..#..", "..#..", "#####", "..#..", "..#..", "....."),
    ",": (".....", ".....", ".....", ".....", "..##.", "..#..", ".#..."),
    "-": (".....", ".....", ".....", "#####", ".....", ".....", "....."),
    ".": (".....", ".....", ".....", ".....", ".....", "..##.", "..##."),
    "/": ("....#", "...#.", "...#.", "..#..", ".#...", ".#...", "#...."),
    "0": (".###.", "#...#", "#..##", "#.#.#", "##..#", "#...#", ".###."),
    "1": ("..#..", ".##..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "2": (".###.", "#...#", "....#", "..##.", ".#...", "#....", "#####"),
    "3": ("####.", "....#", "....#", ".###.", "....#", "....#", "####."),
    "4": ("...#.", "..##.", ".#.#.", "#..#.", "#####", "...#.", "...#."),
    "5": ("#####", "#....", "####.", "....#", "....#", "#...#", ".###."),
    "6": (".###.", "#....", "#....", "####.", "#...#", "#...#", ".###."),
    "7": ("#####", "....#", "...#.", "..#..", ".#...", ".#...", ".#..."),
    "8": (".###.", "#...#", "#...#", ".###.", "#...#", "#...#", ".###."),
    "9": (".###.", "#...#", "#...#", ".####", "....#", "....#", ".###."),
    ":": (".....", "..##.", "..##.", ".....", "..##.", "..##.", "....."),
    ";": (".....", "..##.", "..##.", ".....", "..##.", "..#..", ".#..."),
    "<": ("...#.", "..#..", ".#...", "#....", ".#...", "..#..", "...#."),
    "=": (".....", ".....", "#####", ".....", "#####", ".....", "....."),
    ">": (".#...", "..#..", "...#.", "....#", "...#.", "..#..", ".#..."),
    "?": (".###.", "#...#", "....#", "..##.", "..#..", ".....", "..#.."),
    "@": (".###.", "#...#", "#.###", "#.#.#", "#.###", "#....", ".###."),
    "A": ("..#..", ".#.#.", "#...#", "#...#", "#####", "#...#", "#...#"),
    "B": ("####.", "#...#", "#...#", "####.", "#...#", "#...#", "####."),
    "C": (".###.", "#...#", "#....", "#....", "#....", "#...#", ".###."),
    "D": ("####.", "#...#", "#...#", "#...#", "#...#", "#...#", "####."),
    "E": ("#####", "#....", "#....", "####.", "#....", "#....", "#####"),
    "F": ("#####", "#....", "#....", "####.", "#....", "#....", "#...."),
    "G": (".###.", "#...#", "#....", "#..##", "#...#", "#...#", ".###."),
    "H": ("#...#", "#...#", "#...#", "#####", "#...#", "#...#", "#...#"),
    "I": (".###.", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "J": ("..###", "...#.", "...#.", "...#.", "#..#.", "#..#.", ".##.."),
    "K": ("#...#", "#..#.", "#.#..", "##...", "#.#..", "#..#.", "#...#"),
    "L": ("#....", "#....", "#....", "#....", "#....", "#....", "#####"),
    "M": ("#...#", "##.##", "#.#.#", "#...#", "#...#", "#...#", "#...#"),
    "N": ("#...#", "##..#", "#.#.#", "#..##", "#...#", "#...#", "#...#"),
    "O": (".###.", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "P": ("####.", "#...#", "#...#", "####.", "#....", "#....", "#...."),
    "Q": (".###.", "#...#", "#...#", "#...#", "#.#.#", "#..#.", ".##.#"),
    "R": ("####.", "#...#", "#...#", "####.", "#.#..", "#..#.", "#...#"),
    "S": (".####", "#....", "#....", ".###.", "....#", "....#", "####."),
    "T": ("#####", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "U": ("#...#", "#...#", "#...#", "#...#", "#...#", "#...#", ".###."),
    "V": ("#...#", "#...#", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "W": ("#...#", "#...#", "#...#", "#...#", "#.#.#", "##.##", "#...#"),
    "X": ("#...#", "#...#", ".#.#.", "..#..", ".#.#.", "#...#", "#...#"),
    "Y": ("#...#", "#...#", ".#.#.", "..#..", "..#..", "..#..", "..#.."),
    "Z": ("#####", "....#", "...#.", "..#..", ".#...", "#....", "#####"),
    "[": (".###.", ".#...", ".#...", ".#...", ".#...", ".#...", ".###."),
    "\\": ("#....", ".#...", ".#...", "..#..", "...#.", "...#.", "....#"),
    "]": (".###.", "...#.", "...#.", "...#.", "...#.", "...#.", ".###."),
    "^": ("..#..", ".#.#.", "#...#", ".....", ".....", ".....", "....."),
    "_": (".....", ".....", ".....", ".....", ".....", ".....", "#####"),
    "`": (".#...", "..#..", ".....", ".....", ".....", ".....", "....."),
    "a": (".....", ".....", ".###.", "....#", ".####", "#...#", ".####"),
    "b": ("#....", "#....", "####.", "#...#", "#...#", "#...#", "####."),
    "c": (".....", ".....", ".###.", "#...#", "#....", "#...#", ".###."),
    "d": ("....#", "....#", ".####", "#...#", "#...#", "#...#", ".####"),
    "e": (".....", ".....", ".###.", "#...#", "#####", "#....", ".###."),
    "f": ("..##.", ".#..#", ".#...", "###..", ".#...", ".#...", ".#..."),
    "g": (".....", ".####", "#...#", "#...#", ".####", "....#", ".###."),
    "h": ("#....", "#....", "#.##.", "##..#", "#...#", "#...#", "#...#"),
    "i": ("..#..", ".....", ".##..", "..#..", "..#..", "..#..", ".###."),
    "j": ("...#.", ".....", "..##.", "...#.", "...#.", "#..#.", ".##.."),
    "k": ("#....", "#....", "#..#.", "#.#..", "##...", "#.#..", "#..#."),
    "l": (".##..", "..#..", "..#..", "..#..", "..#..", "..#..", ".###."),
    "m": (".....", ".....", "##.#.", "#.#.#", "#.#.#", "#...#", "#...#"),
    "n": (".....", ".....", "####.", "#...#", "#...#", "#...#", "#...#"),
    "o": (".....", ".....", ".###.", "#...#", "#...#", "#...#", ".###."),
    "p": (".....", "####.", "#...#", "#...#", "####.", "#....", "#...."),
    "q": (".....", ".####", "#...#", "#...#", ".####", "....#", "....#"),
    "r": (".....", ".....", "#.##.", "##..#", "#....", "#....", "#...."),
    "s": (".....", ".....", ".####", "#....", ".###.", "....#", "####."),
    "t": (".#...", ".#...", "###..", ".#...", ".#...", ".#..#", "..##."),
    "u": (".....", ".....", "#...#", "#...#", "#...#", "#..##", ".##.#"),
    "v": (".....", ".....", "#...#", "#...#", "#...#", ".#.#.", "..#.."),
    "w": (".....", ".....", "#...#", "#...#", "#.#.#", "#.#.#", ".#.#."),
    "x": (".....", ".....", "#...#", ".#.#.", "..#..", ".#.#.", "#...#"),
    "y": (".....", "#...#", "#...#", ".####", "....#", "#...#", ".###."),
    "z": (".....", ".....", "#####", "...#.", "..#..", ".#...", "#####"),
    "{": ("..##.", ".#...", ".#...", "#....", ".#...", ".#...", "..##."),
    "|": ("..#..", "..#..", "..#..", "..#..", "..#..", "..#..", "..#.."),
    "}": (".##..", "...#.", "...#.", "....#", "...#.", "...#.", ".##.."),
    "~": (".....", ".....", ".#..#", "#.##.", ".....", ".....", "....."),
}


def _find_binary(name: str, extra_paths: Sequence[str] = ()) -> Optional[str]:
    hit = shutil.which(name)
    if hit:
        return hit
    for p in extra_paths:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def _draw_text_to_gray_buffer(
    buf: bytearray,
    width: int,
    height: int,
    x_px: int,
    y_top_px: int,
    text: str,
    scale: int = 2,
    ink_val: int = 12,
) -> None:
    """Render ASCII text into an 8-bit grayscale image buffer using `_FONT_5X7`."""
    cx = x_px
    step_x = 6 * scale
    for ch in text:
        glyph = _FONT_5X7.get(ch) or _FONT_5X7.get(ch.upper()) or _FONT_5X7[" "]
        for r_idx, row_str in enumerate(glyph):
            for c_idx, px_ch in enumerate(row_str):
                if px_ch != "#":
                    continue
                for dy in range(scale):
                    yy = y_top_px + r_idx * scale + dy
                    if not (0 <= yy < height):
                        continue
                    row_off = yy * width
                    for dx in range(scale):
                        xx = cx + c_idx * scale + dx
                        if 0 <= xx < width:
                            buf[row_off + xx] = ink_val
        cx += step_x


def generate_scanned_pdf_statement(
    title_lines: Sequence[str],
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    col_x_positions: Optional[Sequence[float]] = None,
    dpi: int = 200,
) -> bytes:
    """
    Generate an authentic multi-page image-only (scanned) PDF 1.4 bank statement with zero text operators.
    All text is rasterized into /Subtype /Image /FlateDecode XObjects so native text extraction fails
    and only OCR can recover the statement rows.
    """
    page_w_pt, page_h_pt = 595.0, 842.0
    scale_pt_to_px = dpi / 72.0
    img_w = int(round(page_w_pt * scale_pt_to_px))
    img_h = int(round(page_h_pt * scale_pt_to_px))
    # Keep glyph size proportional to ~7.5pt (at 200 DPI, font_scale=2 yields ~4.3pt char advance)
    font_scale = max(2, int(round(dpi / 96.0)))

    if col_x_positions is None:
        col_x_positions = [28.0, 92.0, 316.0, 396.0, 462.0, 528.0]

    rows_per_page = 34
    chunks = [rows[i : i + rows_per_page] for i in range(0, max(1, len(rows)), rows_per_page)]

    page_images: List[bytes] = []
    for page_num, chunk in enumerate(chunks, start=1):
        buf = bytearray([252] * (img_w * img_h))
        y_pt = 36.0
        for idx, line in enumerate(title_lines):
            _draw_text_to_gray_buffer(
                buf,
                img_w,
                img_h,
                int(28.0 * scale_pt_to_px),
                int(y_pt * scale_pt_to_px),
                str(line).replace("₹", "Rs. "),
                scale=font_scale,
                ink_val=10,
            )
            y_pt += 16.0 if idx == 0 else 13.0

        y_pt += 8.0
        # Light header background bar (>220 gray so binarization ignores background)
        bar_y0 = max(0, int((y_pt - 3.0) * scale_pt_to_px))
        bar_y1 = min(img_h, int((y_pt + 12.0) * scale_pt_to_px))
        x0_bar = int(24.0 * scale_pt_to_px)
        x1_bar = min(img_w, int(575.0 * scale_pt_to_px))
        for yy in range(bar_y0, bar_y1):
            row_off = yy * img_w
            for xx in range(x0_bar, x1_bar):
                buf[row_off + xx] = 228

        for c_i, col_name in enumerate(columns):
            cx_pt = col_x_positions[min(c_i, len(col_x_positions) - 1)]
            _draw_text_to_gray_buffer(
                buf,
                img_w,
                img_h,
                int(cx_pt * scale_pt_to_px),
                int(y_pt * scale_pt_to_px),
                str(col_name),
                scale=font_scale,
                ink_val=8,
            )

        y_pt += 18.0
        for r_idx, row in enumerate(chunk):
            if r_idx % 2 == 1:
                ry0 = max(0, int((y_pt - 2.0) * scale_pt_to_px))
                ry1 = min(img_h, int((y_pt + 11.0) * scale_pt_to_px))
                for yy in range(ry0, ry1):
                    row_off = yy * img_w
                    for xx in range(x0_bar, x1_bar):
                        buf[row_off + xx] = 242
            for c_i, val in enumerate(row):
                cx_pt = col_x_positions[min(c_i, len(col_x_positions) - 1)]
                txt = str(val if val is not None else "").replace("₹", "Rs. ")
                _draw_text_to_gray_buffer(
                    buf,
                    img_w,
                    img_h,
                    int(cx_pt * scale_pt_to_px),
                    int(y_pt * scale_pt_to_px),
                    txt,
                    scale=font_scale,
                    ink_val=12,
                )
            y_pt += 18.0

        _draw_text_to_gray_buffer(
            buf,
            img_w,
            img_h,
            int(28.0 * scale_pt_to_px),
            int((page_h_pt - 30.0) * scale_pt_to_px),
            f"Page {page_num} of {len(chunks)}",
            scale=font_scale,
            ink_val=40,
        )
        page_images.append(zlib.compress(bytes(buf), level=6))

    # Build PDF objects (1: Catalog, 2: Pages, then 3 objects per page: Page, Image, Content)
    objects: List[bytes] = []
    num_pages = len(page_images)
    page_ids = [3 + i * 3 for i in range(num_pages)]
    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(f"<< /Type /Pages /Kids [ {kids} ] /Count {num_pages} >>".encode("ascii"))

    for i, img_z in enumerate(page_images):
        img_id = 4 + i * 3
        content_id = 5 + i * 3
        page_dict = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {page_w_pt:.0f} {page_h_pt:.0f}] "
            f"/Resources << /XObject << /Im1 {img_id} 0 R >> >> /Contents {content_id} 0 R >>"
        ).encode("ascii")
        img_obj = (
            f"<< /Type /XObject /Subtype /Image /Width {img_w} /Height {img_h} "
            f"/ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode /Length {len(img_z)} >>\nstream\n"
        ).encode("ascii") + img_z + b"\nendstream"
        content_stream = f"q {page_w_pt:.0f} 0 0 {page_h_pt:.0f} 0 0 cm /Im1 Do Q".encode("ascii")
        content_obj = (
            f"<< /Length {len(content_stream)} >>\nstream\n".encode("ascii")
            + content_stream
            + b"\nendstream"
        )
        objects.extend([page_dict, img_obj, content_obj])

    # Producer marker: lets the 5x7 glyph matcher read these fixtures (and nothing else; PDF-11)
    objects.append(b"<< /Producer (" + FIXTURE_PRODUCER_MARKER + b") >>")
    info_id = len(objects)
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: List[int] = [0]
    for idx, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{idx} 0 obj\n".encode("ascii"))
        out.extend(body)
        out.extend(b"\nendobj\n")

    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.extend(b"0000000000 65535 f \n")
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info {info_id} 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


# ---------------------------------------------------------------------------
# Pure-Python Raster Image Loaders (PGM/PPM, PNG, BMP) & PDF Stream Decoders
# ---------------------------------------------------------------------------

def _decode_pnm(raw: bytes) -> Optional[Tuple[int, int, bytearray]]:
    """Decode binary PGM (P5) or PPM (P6) into (width, height, gray_bytes)."""
    idx = 0
    n = len(raw)

    def next_tok() -> Optional[bytes]:
        nonlocal idx
        while idx < n:
            if raw[idx] in b" \t\r\n":
                idx += 1
            elif raw[idx : idx + 1] == b"#":
                while idx < n and raw[idx] != 10:
                    idx += 1
            else:
                break
        if idx >= n:
            return None
        start = idx
        while idx < n and raw[idx] not in b" \t\r\n#":
            idx += 1
        return raw[start:idx]

    magic = next_tok()
    if magic not in (b"P5", b"P6"):
        return None
    w_b, h_b, max_b = next_tok(), next_tok(), next_tok()
    if not (w_b and h_b and max_b):
        return None
    w, h = int(w_b), int(h_b)
    # Exactly one whitespace byte separates header from binary raster
    if idx < n and raw[idx] in b" \t\r\n":
        idx += 1
    pixels = raw[idx:]
    if magic == b"P5":
        if len(pixels) < w * h:
            return None
        return w, h, bytearray(pixels[: w * h])
    # P6 RGB -> luminosity gray
    if len(pixels) < w * h * 3:
        return None
    gray = bytearray(w * h)
    for i in range(w * h):
        r, g, b = pixels[3 * i], pixels[3 * i + 1], pixels[3 * i + 2]
        gray[i] = (77 * r + 150 * g + 29 * b) >> 8
    return w, h, gray


def _decode_bmp(raw: bytes) -> Optional[Tuple[int, int, bytearray]]:
    """Decode uncompressed 8-bit, 24-bit, or 32-bit BMP into (width, height, top_down_gray)."""
    if len(raw) < 54 or raw[:2] != b"BM":
        return None
    data_offset = struct.unpack_from("<I", raw, 10)[0]
    w, h_signed = struct.unpack_from("<ii", raw, 18)
    bpp = struct.unpack_from("<H", raw, 28)[0]
    comp = struct.unpack_from("<I", raw, 30)[0]
    if comp != 0 or w <= 0 or h_signed == 0:
        return None
    top_down = h_signed < 0
    h = abs(h_signed)
    if bpp not in (8, 24, 32):
        return None
    stride = ((w * bpp + 31) // 32) * 4
    gray = bytearray(w * h)
    for r in range(h):
        src_r = r if top_down else (h - 1 - r)
        row_start = data_offset + src_r * stride
        dst_start = r * w
        for x in range(w):
            if bpp == 8:
                gray[dst_start + x] = raw[row_start + x]
            else:
                step = bpp // 8
                b, g, r_c = raw[row_start + x * step], raw[row_start + x * step + 1], raw[row_start + x * step + 2]
                gray[dst_start + x] = (77 * r_c + 150 * g + 29 * b) >> 8
    return w, h, gray


def _decode_png(raw: bytes) -> Optional[Tuple[int, int, bytearray]]:
    """Decode standard 8-bit PNG (Grayscale, RGB, Gray+Alpha, RGBA) in pure Python."""
    if len(raw) < 33 or raw[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    pos = 8
    w = h = bit_depth = color_type = 0
    idat = bytearray()
    while pos + 8 <= len(raw):
        length, ctype = struct.unpack_from(">I4s", raw, pos)
        data_start = pos + 8
        data_end = data_start + length
        if data_end + 4 > len(raw):
            break
        chunk = raw[data_start:data_end]
        pos = data_end + 4
        if ctype == b"IHDR":
            w, h, bit_depth, color_type, comp, flt, interlace = struct.unpack(">IIBBBBB", chunk[:13])
            if bit_depth != 8 or comp != 0 or flt != 0 or interlace != 0:
                return None
        elif ctype == b"IDAT":
            idat.extend(chunk)
        elif ctype == b"IEND":
            break

    bpp_map = {0: 1, 2: 3, 4: 2, 6: 4}
    if color_type not in bpp_map or w <= 0 or h <= 0:
        return None
    bpp = bpp_map[color_type]
    if w * h > MAX_IMAGE_PIXELS:
        return None
    decompressed = _inflate_bounded(bytes(idat), max_out=h * (w * bpp + 1))
    if decompressed is None:
        return None

    stride = w * bpp
    if len(decompressed) < h * (stride + 1):
        return None

    def paeth(a: int, b: int, c: int) -> int:
        p = a + b - c
        pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
        return a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)

    recon = bytearray(h * stride)
    for r in range(h):
        in_off = r * (stride + 1)
        ftype = decompressed[in_off]
        scan = decompressed[in_off + 1 : in_off + 1 + stride]
        out_off = r * stride
        prev_off = (r - 1) * stride
        for i in range(stride):
            x = scan[i]
            a = recon[out_off + i - bpp] if i >= bpp else 0
            b = recon[prev_off + i] if r > 0 else 0
            c = recon[prev_off + i - bpp] if (r > 0 and i >= bpp) else 0
            if ftype == 0:
                val = x
            elif ftype == 1:
                val = (x + a) & 0xFF
            elif ftype == 2:
                val = (x + b) & 0xFF
            elif ftype == 3:
                val = (x + ((a + b) >> 1)) & 0xFF
            elif ftype == 4:
                val = (x + paeth(a, b, c)) & 0xFF
            else:
                return None
            recon[out_off + i] = val

    gray = bytearray(w * h)
    if color_type == 0:
        gray[:] = recon
    elif color_type == 4:
        for i in range(w * h):
            gray[i] = recon[2 * i]
    else:
        step = bpp
        for i in range(w * h):
            r_c, g_c, b_c = recon[i * step], recon[i * step + 1], recon[i * step + 2]
            gray[i] = (77 * r_c + 150 * g_c + 29 * b_c) >> 8
    return w, h, gray


def _load_image_as_gray(img_bytes: bytes) -> Optional[Tuple[int, int, bytearray]]:
    for decoder in (_decode_pnm, _decode_png, _decode_bmp):
        res = decoder(img_bytes)
        if res is not None:
            return res
    # Fallback on macOS using built-in `sips` for JPEG/TIFF -> BMP
    sips = _find_binary("sips", ("/usr/bin/sips",))
    if sips:
        try:
            with private_tempdir() as td:
                in_p = write_private_file(td, "in.img", img_bytes)
                out_p = os.path.join(td, "out.bmp")
                subprocess.run([sips, "-s", "format", "bmp", in_p, "--out", out_p], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=OCR_PAGE_TIMEOUT_S)
                if os.path.exists(out_p):
                    with open(out_p, "rb") as f:
                        return _decode_bmp(f.read())
        except Exception:
            pass
    return None


def decode_predictor(
    data: bytes,
    predictor: int,
    colors: int = 1,
    bits_per_component: int = 8,
    columns: int = 1,
) -> Optional[bytes]:
    """
    Undo a /DecodeParms /Predictor applied after Flate/LZW (PDF 32000-1 7.4.4.4): 1 = none, 2 = TIFF
    (8-bit components only), 10-15 = PNG (per-row filter byte: None/Sub/Up/Average/Paeth).
    Returns None for unsupported parameters or malformed data (PDF-06/PDF-19).
    """
    if predictor in (0, 1):
        return data
    colors = max(1, int(colors))
    bpc = int(bits_per_component)
    columns = max(1, int(columns))
    if bpc not in (1, 2, 4, 8, 16) or colors > 32 or columns > 1 << 20:
        return None
    row_len = (colors * bpc * columns + 7) // 8
    bpp = max(1, (colors * bpc) // 8)
    if predictor == 2:
        if bpc != 8:
            return None
        out = bytearray(data)
        for r0 in range(0, len(out), row_len):
            for i in range(r0 + colors, min(r0 + row_len, len(out))):
                out[i] = (out[i] + out[i - colors]) & 0xFF
        return bytes(out)
    if not 10 <= predictor <= 15:
        return None
    stride = row_len + 1
    out = bytearray()
    prev = bytearray(row_len)
    for r0 in range(0, len(data), stride):
        ftype = data[r0]
        row = bytearray(data[r0 + 1 : r0 + stride])
        n = len(row)
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(bpp, n):
                row[i] = (row[i] + row[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(n):
                row[i] = (row[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(n):
                left = row[i - bpp] if i >= bpp else 0
                row[i] = (row[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(n):
                a = row[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pr = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
                row[i] = (row[i] + pr) & 0xFF
        else:
            return None
        out += row
        prev = row if n == row_len else row + prev[n:]
    return bytes(out)


def _image_components(doc: Any, dict_b: bytes) -> Tuple[int, Optional[bytes]]:
    """(components per sample, 256-entry gray palette for /Indexed or None) of an image's /ColorSpace."""
    from cyphersol.ingestion.pdf_parser import _dict_value, _NAME_ITEM_RE  # local import (cycle)

    cs = doc.resolve(_dict_value(dict_b, b"ColorSpace") or b"").strip()
    if not cs:
        return 1, None
    names = _NAME_ITEM_RE.findall(cs[:64])
    head = names[0] if names else b""
    if head in (b"DeviceGray", b"CalGray", b"G"):
        return 1, None
    if head in (b"DeviceRGB", b"CalRGB", b"RGB", b"Lab"):
        return 3, None
    if head in (b"DeviceCMYK", b"CMYK"):
        return 4, None
    if head == b"ICCBased":
        ref = re.search(rb"(\d+)\s+\d+\s+R", cs)
        n_v = _dict_value(doc.objs.get(int(ref.group(1)), b"")[:2048], b"N") if ref else None
        return (int(n_v) if n_v and n_v.isdigit() and int(n_v) in (1, 3, 4) else 3), None
    if head in (b"Indexed", b"I"):
        inner = cs[cs.find(b"Indexed") + 7 if b"Indexed" in cs else 2 :].strip()
        base_n = 3
        bnames = _NAME_ITEM_RE.findall(inner[:48])
        if bnames and bnames[0] in (b"DeviceGray", b"G", b"CalGray"):
            base_n = 1
        elif bnames and bnames[0] in (b"DeviceCMYK", b"CMYK"):
            base_n = 4
        lookup: Optional[bytes] = None
        hm = re.search(rb"<([0-9A-Fa-f\s]*)>", inner)
        lm = re.search(rb"\((.*)\)", inner, re.DOTALL)
        rm = list(re.finditer(rb"(\d+)\s+\d+\s+R", inner))
        if hm:
            hx = re.sub(rb"\s+", b"", hm.group(1))
            lookup = bytes.fromhex((hx + b"0").decode() if len(hx) % 2 else hx.decode())
        elif lm:
            from cyphersol.ingestion.pdf_parser import _unescape_pdf_literal
            lookup = _unescape_pdf_literal(lm.group(1))
        elif rm:
            lookup = doc.stream(int(rm[-1].group(1)), max_out=256 * 4 + 16)
        if not lookup:
            return 1, None
        pal = bytearray(256)
        for i in range(256):
            px = lookup[i * base_n : (i + 1) * base_n]
            if len(px) < base_n:
                pal[i] = pal[i - 1] if i else 0
            elif base_n == 1:
                pal[i] = px[0]
            elif base_n == 3:
                pal[i] = (77 * px[0] + 150 * px[1] + 29 * px[2]) >> 8
            else:
                pal[i] = max(0, 255 - min(255, (77 * px[0] + 150 * px[1] + 29 * px[2]) // 256 + px[3]))
        return 1, bytes(pal)
    return 1, None


def _samples_to_gray(raw: bytes, w: int, h: int, bpc: int, ncomp: int,
                     palette: Optional[bytes], invert: bool) -> Optional[bytearray]:
    """Unpack decoded image samples (any bpc 1/2/4/8/16; Gray/RGB/CMYK/Indexed) to 8-bit gray."""
    row_bytes = (w * ncomp * bpc + 7) // 8
    if len(raw) < h * row_bytes:
        return None
    gray = bytearray(w * h)
    maxv = (1 << bpc) - 1 if bpc < 16 else 65535
    for y in range(h):
        row = raw[y * row_bytes : (y + 1) * row_bytes]
        if bpc == 8:
            samples: Sequence[int] = row
        elif bpc == 16:
            samples = [row[i] for i in range(0, len(row), 2)]  # high byte
        else:
            per = 8 // bpc
            mask = (1 << bpc) - 1
            samples = [(byte >> (8 - bpc * (k + 1))) & mask for byte in row for k in range(per)]
        dst = y * w
        for x in range(w):
            base = x * ncomp
            if palette is not None:
                v = palette[samples[base] & 0xFF]
            elif ncomp == 1:
                sv = samples[base]
                v = sv if bpc in (8, 16) else (sv * 255) // maxv
            elif ncomp == 3:
                r, g, b = samples[base], samples[base + 1], samples[base + 2]
                if bpc not in (8, 16):
                    r, g, b = r * 255 // maxv, g * 255 // maxv, b * 255 // maxv
                v = (77 * r + 150 * g + 29 * b) >> 8
            else:
                c, m, yy, k = samples[base], samples[base + 1], samples[base + 2], samples[base + 3]
                if bpc not in (8, 16):
                    c, m, yy, k = (c * 255 // maxv, m * 255 // maxv, yy * 255 // maxv, k * 255 // maxv)
                v = max(0, 255 - min(255, (77 * c + 150 * m + 29 * yy) // 256 + k))
            gray[dst + x] = 255 - v if invert else v
    return gray


def _extract_embedded_pdf_images(
    pdf_bytes: bytes,
    pages: Optional[Sequence[int]] = None,
    budget: Optional[InflateBudget] = None,
    min_side: int = GLYPH_MIN_IMAGE_SIDE,
) -> List[Tuple[int, int, int, bytearray]]:
    """
    Decode the page-sized image XObjects of each page to 8-bit gray for the fixture glyph matcher.
    Returns [(page_idx, width, height, gray_bytes), ...], optionally restricted to `pages`.

    Pages come from the /Pages tree (object streams expanded, Form XObjects followed). Flate/ASCII filters
    with PNG/TIFF predictors, 1/2/4/8/16-bit Gray/RGB/CMYK/ICCBased/Indexed samples and /Decode [1 0] are
    handled; DCT/JPX/JBIG2/CCITT images are left to the page renderer. Images smaller than `min_side` on
    either axis (logos, signatures, stamps) are skipped (PDF-19). Decompression is bounded per image
    (expected raster size) and per document (`budget`).
    """
    from cyphersol.ingestion.pdf_parser import (  # local import: pdf_parser imports this module
        MAX_FORM_DEPTH, _PdfDocument, _REF_RE, _SUBTYPE_FORM_RE, _SUBTYPE_IMAGE_RE, _dict_value,
        _extract_stream_from_obj,
    )

    if _is_pdf_encrypted(pdf_bytes):
        return []  # stream data is ciphertext; the renderer path handles encrypted scans
    budget = budget or InflateBudget()
    doc = _PdfDocument(pdf_bytes, budget)
    wanted = set(pages) if pages else None

    def _images_of(res_body: bytes, depth: int, seen: set) -> List[int]:
        found: List[int] = []
        if not res_body or depth > MAX_FORM_DEPTH:
            return found
        for m in _REF_RE.finditer(doc.resolve(_dict_value(res_body, b"XObject"))):
            oid = int(m.group(1))
            if oid in seen:
                continue
            seen.add(oid)
            head = doc.objs.get(oid, b"")[:4096]
            if _SUBTYPE_IMAGE_RE.search(head):
                found.append(oid)
            elif _SUBTYPE_FORM_RE.search(head):
                res_v = _dict_value(head, b"Resources")
                if res_v:
                    found.extend(_images_of(doc.resolve(res_v), depth + 1, seen))
        return found

    results: List[Tuple[int, int, int, bytearray]] = []
    for p_idx, page in enumerate(doc.pages(), start=1):
        if wanted is not None and p_idx not in wanted:
            continue
        for oid in _images_of(page.resources, 0, set()):
            body = doc.objs.get(oid, b"")
            sm = re.search(rb"\bstream(?:\r\n|\r|\n)", body)
            if not sm:
                continue
            dict_b = body[: sm.start()]
            w_v, h_v = _dict_value(dict_b, b"Width"), _dict_value(dict_b, b"Height")
            if not (w_v and h_v and w_v.isdigit() and h_v.isdigit()):
                continue
            w, h = int(w_v), int(h_v)
            if min(w, h) < min_side or w * h > MAX_IMAGE_PIXELS:
                continue
            is_mask = bool(re.search(rb"/ImageMask\s+true", dict_b))
            bpc_v = _dict_value(dict_b, b"BitsPerComponent")
            bpc = 1 if is_mask else (int(bpc_v) if bpc_v and bpc_v.isdigit() else 8)
            if bpc not in (1, 2, 4, 8, 16):
                continue
            ncomp, palette = (1, None) if is_mask else _image_components(doc, dict_b)
            expected = h * ((w * ncomp * bpc + 7) // 8)
            _d, raw_px = _extract_stream_from_obj(body, budget=budget, objs=doc.objs, max_out=expected + h + 64)
            if raw_px is None:
                continue  # DCT/JPX/JBIG2/CCITT or corrupt: the renderer path reads those
            dec_v = _dict_value(dict_b, b"Decode") or b""
            invert = bool(re.match(rb"\[\s*1(?:\.0*)?\s+0(?:\.0*)?\s*\]", dec_v.strip()))
            gray = _samples_to_gray(raw_px, w, h, bpc, ncomp, palette, invert)
            if gray is not None:
                results.append((p_idx, w, h, gray))
    return results


# ---------------------------------------------------------------------------
# Normalized Glyph Template Recognizer (Pure Python Backend C)
# ---------------------------------------------------------------------------

def _resample_glyph(ink_grid: List[List[int]], gx0: int, gy0: int, gx1: int, gy1: int, tw: int = 5, th: int = 7) -> List[float]:
    gw = max(1, gx1 - gx0 + 1)
    gh = max(1, gy1 - gy0 + 1)
    out = [0.0] * (tw * th)
    for ty in range(th):
        sy0 = gy0 + (ty * gh) // th
        sy1 = gy0 + max(ty * gh // th + 1, ((ty + 1) * gh) // th)
        for tx in range(tw):
            sx0 = gx0 + (tx * gw) // tw
            sx1 = gx0 + max(tx * gw // tw + 1, ((tx + 1) * gw) // tw)
            hits = 0
            area = 0
            for sy in range(sy0, min(gy1 + 1, sy1)):
                row = ink_grid[sy]
                for sx in range(sx0, min(gx1 + 1, sx1)):
                    hits += row[sx]
                    area += 1
            out[ty * tw + tx] = hits / max(1, area)
    return out


def _build_glyph_templates() -> List[Tuple[str, List[float], float, str]]:
    """
    Build cropped normalized 5x7 vectors + natural aspect ratio + vertical-position class for ASCII chars.
    Vertical-position classes: 'top', 'mid', 'low', 'full', 'any'
    """
    templates: List[Tuple[str, List[float], float, str]] = []
    for ch, rows in _FONT_5X7.items():
        if ch == " ":
            continue
        ys = [r for r in range(7) if "#" in rows[r]]
        xs = [c for c in range(5) if any(rows[r][c] == "#" for r in range(7))]
        if not ys or not xs:
            continue
        gy0, gy1 = min(ys), max(ys)
        gx0, gx1 = min(xs), max(xs)
        grid = [[1 if rows[r][c] == "#" else 0 for c in range(5)] for r in range(7)]
        vec = _resample_glyph(grid, gx0, gy0, gx1, gy1, 5, 7)
        aspect = (gx1 - gx0 + 1) / max(1, gy1 - gy0 + 1)
        rel_y = ((gy0 + gy1) / 2.0) / 6.0
        if ch in {"'", '"', "`", "^"}:
            vpos = "top"
        elif ch in {".", ","}:
            vpos = "low_dot"
        elif ch in {"_"}:
            vpos = "bottom_bar"
        elif ch in {"-", "~"}:
            vpos = "mid_bar"
        elif rel_y > 0.72:
            vpos = "low"
        else:
            vpos = "any"
        templates.append((ch, vec, aspect, vpos))
    return templates


_GLYPH_TEMPLATES = _build_glyph_templates()


def _match_glyph(
    ink_grid: List[List[int]],
    gx0: int,
    gy0: int,
    gx1: int,
    gy1: int,
    band_y0: int,
    band_y1: int,
    char_unit_h: int,
) -> str:
    return _match_glyph_scored(ink_grid, gx0, gy0, gx1, gy1, band_y0, band_y1, char_unit_h)[0]


def _match_glyph_scored(
    ink_grid: List[List[int]],
    gx0: int,
    gy0: int,
    gx1: int,
    gy1: int,
    band_y0: int,
    band_y1: int,
    char_unit_h: int,
) -> Tuple[str, float]:
    """Best 5x7 template and its confidence in [0, 1] (1 = exact template match)."""
    gw = gx1 - gx0 + 1
    gh = gy1 - gy0 + 1
    aspect = gw / max(1, gh)
    band_h = max(1, char_unit_h)
    rel_y = (((gy0 + gy1) / 2.0) - band_y0) / band_h

    # Fast geometric disambiguation for punctuation & small marks
    if gh <= max(2, int(band_h * 0.36)) and gw <= max(3, int(band_h * 0.45)):
        return ("," if gh > gw and rel_y > 0.60 else "."), _GLYPH_SHAPE_ONLY_CONF
    if gw > gh * 1.45 and gh <= max(3, int(band_h * 0.35)):
        return ("_" if rel_y > 0.75 else "-"), _GLYPH_SHAPE_ONLY_CONF

    vec = _resample_glyph(ink_grid, gx0, gy0, gx1, gy1, 5, 7)
    best_ch = "?"
    best_score = -1e9
    for ch, tvec, taspect, vpos in _GLYPH_TEMPLATES:
        if vpos in {"top", "low_dot", "bottom_bar", "mid_bar"} and gh < band_h * 0.45:
            # Allow punctuation templates only for compact glyphs
            pass
        elif vpos == "top" and rel_y > 0.45:
            continue
        elif vpos == "mid_bar" and (rel_y < 0.25 or rel_y > 0.78):
            continue
        elif vpos == "bottom_bar" and rel_y < 0.65:
            continue

        # Normalized negative L1 distance minus slight aspect & height penalty
        dist = sum(abs(a - b) for a, b in zip(vec, tvec)) / 35.0
        asp_pen = abs(math.log((aspect + 0.05) / (taspect + 0.05))) * 0.12
        # Penalize matching lowercase full-height glyphs like l/i against punctuation
        size_pen = 0.0
        if ch in {".", ",", "'", '"', "-", "_"} and gh >= band_h * 0.52:
            size_pen += 0.35
        # Disambiguate uppercase vs lowercase height when clearly shorter x-height
        if ch in "acemnorsuvwxz" and gh > band_h * 0.88:
            size_pen += 0.06
        elif ch in "ACEMNORSUVWXZ" and gh < band_h * 0.76:
            size_pen += 0.06

        score = -(dist + asp_pen + size_pen)
        if score > best_score:
            best_score = score
            best_ch = ch
    return best_ch, max(0.0, min(1.0, 1.0 + best_score))


_GLYPH_SHAPE_ONLY_CONF = 0.8  # punctuation decided by box shape alone


def _ocr_gray_image_pure_python(
    width: int,
    height: int,
    gray: bytearray,
    page_idx: int,
    page_w_pt: float,
    page_h_pt: float,
) -> List[OcrWordToken]:
    """Segment horizontal text bands and column cells from a grayscale bitmap and decode glyphs."""
    scale_x = page_w_pt / max(1, width)
    scale_y = page_h_pt / max(1, height)

    # Binarize text pixels (threshold 175 cleanly discards paper & light stripe shading >= 220)
    ink_grid: List[List[int]] = []
    row_sums: List[int] = []
    for y in range(height):
        off = y * width
        row = [1 if gray[off + x] < 175 else 0 for x in range(width)]
        ink_grid.append(row)
        row_sums.append(sum(row))

    # Extract horizontal line bands with 1-px vertical tolerance (handles accents/dots over i/j)
    raw_bands: List[Tuple[int, int]] = []
    in_band = False
    by0 = 0
    for y, s in enumerate(row_sums):
        if s >= 2 and not in_band:
            in_band = True
            by0 = y
        elif s < 2 and in_band:
            in_band = False
            raw_bands.append((by0, y - 1))
    if in_band:
        raw_bands.append((by0, height - 1))

    # Merge bands separated by 1px gap (e.g. dot above lowercase 'i'/'j') if resulting height <= 28px
    bands: List[Tuple[int, int]] = []
    for y0, y1 in raw_bands:
        if bands and (y0 - bands[-1][1]) <= 3 and (y1 - bands[-1][0]) <= max(20, int(width * 0.025)):
            bands[-1] = (bands[-1][0], y1)
        elif y1 - y0 >= 3:
            bands.append((y0, y1))

    tokens: List[OcrWordToken] = []
    for y0, y1 in bands:
        band_h = y1 - y0 + 1
        col_sums = [sum(ink_grid[y][x] for y in range(y0, y1 + 1)) for x in range(width)]

        # Segment individual non-empty ink columns (glyph slices)
        slices: List[Tuple[int, int]] = []
        in_slice = False
        sx0 = 0
        for x, cs in enumerate(col_sums):
            if cs > 0 and not in_slice:
                in_slice = True
                sx0 = x
            elif cs == 0 and in_slice:
                in_slice = False
                slices.append((sx0, x - 1))
        if in_slice:
            slices.append((sx0, width - 1))
        if not slices:
            continue

        # Inter-character gap in 5x7@2x is ~2-6px; space gap is ~14-18px; separate column cells >= ~20px
        word_gap_px = max(5, int(round(band_h * 0.60)))
        cell_gap_px = max(15, int(round(band_h * 1.45)))

        cells: List[List[Tuple[int, int, str]]] = []
        current_cell: List[Tuple[int, int, str]] = []
        for idx, (gx0, gx1) in enumerate(slices):
            # Tighten vertical crop for this specific glyph
            gy0 = next((y for y in range(y0, y1 + 1) if any(ink_grid[y][x] for x in range(gx0, gx1 + 1))), y0)
            gy1 = next((y for y in range(y1, y0 - 1, -1) if any(ink_grid[y][x] for x in range(gx0, gx1 + 1))), y1)
            ch, g_conf = _match_glyph_scored(ink_grid, gx0, gy0, gx1, gy1, y0, y1, band_h)
            if idx == 0:
                current_cell = [(gx0, gx1, ch, g_conf)]
                continue
            gap = gx0 - slices[idx - 1][1] - 1
            if gap >= cell_gap_px:
                cells.append(current_cell)
                current_cell = [(gx0, gx1, ch, g_conf)]
            else:
                # Avoid bogus spaces before narrow punctuation like '.', ',', ':', ';' unless clearly wide
                punct_tight = ch in {".", ",", ":", ";"} and gap < int(round(word_gap_px * 1.4))
                if gap >= word_gap_px and not punct_tight:
                    current_cell.append((slices[idx - 1][1] + 1, gx0 - 1, " ", -1.0))
                current_cell.append((gx0, gx1, ch, g_conf))
        if current_cell:
            cells.append(current_cell)

        y_pdf = page_h_pt - ((y0 + y1) / 2.0) * scale_y
        for cell in cells:
            text = "".join(item[2] for item in cell).strip()
            if text:
                x0_px = cell[0][0]
                x1_px = cell[-1][1]
                confs = [item[3] for item in cell if item[3] >= 0.0]
                tokens.append(
                    OcrWordToken(
                        page=page_idx,
                        x=round(x0_px * scale_x, 2),
                        y=round(y_pdf, 2),
                        text=text,
                        x_end=round(x1_px * scale_x, 2),
                        conf=round(sum(confs) / len(confs), 4) if confs else -1.0,
                    )
                )
    return tokens


# ---------------------------------------------------------------------------
# Backend A & B: Tesseract CLI and macOS Apple Vision
# ---------------------------------------------------------------------------

def _tesseract_bin() -> Optional[str]:
    return _find_binary("tesseract", ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract", "/usr/bin/tesseract"))


def _vision_available() -> bool:
    return sys.platform == "darwin" and os.path.exists("/System/Library/Frameworks/Vision.framework")


def _parse_tesseract_tsv(tsv: str, page_idx: int, page_w_pt: float, page_h_pt: float,
                         img_w: int, img_h: int) -> List[OcrWordToken]:
    """Word rows (level 5) of `tesseract ... tsv` output -> cell tokens carrying the word confidence (0..1)."""
    sx = page_w_pt / max(1, img_w)
    sy = page_h_pt / max(1, img_h)
    raw_words: List[Tuple[float, float, float, str, float]] = []
    for line in tsv.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12 or parts[0] != "5":
            continue
        txt = parts[11].strip()
        if not txt:
            continue
        try:
            left, top, w_px, h_px = float(parts[6]), float(parts[7]), float(parts[8]), float(parts[9])
            conf = float(parts[10])
        except ValueError:
            continue
        if conf < 0:  # -1: not a recognised word (layout element)
            continue
        x_pt = left * sx
        x_end_pt = (left + w_px) * sx
        y_pt = page_h_pt - (top + h_px * 0.5) * sy
        raw_words.append((y_pt, x_pt, x_end_pt, txt, min(1.0, conf / 100.0)))
    return _merge_ocr_words_to_cells(raw_words, page_idx)


def _ocr_image_tesseract(image_path: str, page_idx: int, page_w_pt: float, page_h_pt: float, img_w: int, img_h: int) -> List[OcrWordToken]:
    tess = _tesseract_bin()
    if not tess:
        return []
    try:
        proc = subprocess.run(
            [tess, image_path, "stdout", "--psm", "6", "-l", "eng", "tsv"],
            check=True,
            capture_output=True,
            text=True,
            timeout=OCR_PAGE_TIMEOUT_S,
        )
    except Exception:
        return []
    return _parse_tesseract_tsv(proc.stdout, page_idx, page_w_pt, page_h_pt, img_w, img_h)


def _ocr_image_apple_vision_single(img_bytes: bytes, page_idx: int, page_w_pt: float, page_h_pt: float) -> List[OcrWordToken]:
    """Run one pass of macOS Apple Vision VNRecognizeTextRequest locally via ctypes."""
    if sys.platform != "darwin":
        return []
    try:
        objc = ctypes.cdll.LoadLibrary("/usr/lib/libobjc.A.dylib")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Foundation.framework/Foundation")
        ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Vision.framework/Vision")

        class CGPoint(Structure):
            _fields_ = [("x", c_double), ("y", c_double)]

        class CGSize(Structure):
            _fields_ = [("width", c_double), ("height", c_double)]

        class CGRect(Structure):
            _fields_ = [("origin", CGPoint), ("size", CGSize)]

        class NSRange(Structure):
            _fields_ = [("location", c_ulong), ("length", c_ulong)]

        objc.objc_getClass.restype = c_void_p
        objc.objc_getClass.argtypes = [c_char_p]
        objc.sel_registerName.restype = c_void_p
        objc.sel_registerName.argtypes = [c_char_p]

        def cls(name: str) -> c_void_p:
            return objc.objc_getClass(name.encode("utf-8"))

        def sel(name: str) -> c_void_p:
            return objc.sel_registerName(name.encode("utf-8"))

        def call(obj: Any, selector: str, restype: Any = c_void_p, argtypes: Tuple[Any, ...] = (), *args: Any) -> Any:
            fn = ctypes.CFUNCTYPE(restype, c_void_p, c_void_p, *argtypes)(("objc_msgSend", objc))
            return fn(obj, sel(selector), *args)

        ns_data = call(cls("NSData"), "dataWithBytes:length:", c_void_p, (c_char_p, c_ulong), img_bytes, len(img_bytes))
        dict_ns = call(cls("NSDictionary"), "dictionary", c_void_p)
        req = call(call(cls("VNRecognizeTextRequest"), "alloc"), "init")
        call(req, "setRecognitionLevel:", None, (c_long,), 0)
        call(req, "setUsesLanguageCorrection:", None, (c_bool,), False)
        call(req, "setMinimumTextHeight:", None, (c_float,), 0.004)
        arr = call(cls("NSArray"), "arrayWithObject:", c_void_p, (c_void_p,), req)
        handler = call(
            call(cls("VNImageRequestHandler"), "alloc"),
            "initWithData:options:",
            c_void_p,
            (c_void_p, c_void_p),
            ns_data,
            dict_ns,
        )
        err = c_void_p(0)
        ok = call(handler, "performRequests:error:", c_bool, (c_void_p, ctypes.POINTER(c_void_p)), arr, byref(err))
        if not ok:
            return []

        results = call(req, "results", c_void_p)
        count = call(results, "count", c_ulong) if results else 0
        raw_words: List[Tuple[float, float, float, str, float]] = []
        for i in range(count):
            obs = call(results, "objectAtIndex:", c_void_p, (c_ulong,), i)
            cands = call(obs, "topCandidates:", c_void_p, (c_ulong,), 1)
            if call(cands, "count", c_ulong) > 0:
                cand = call(cands, "objectAtIndex:", c_void_p, (c_ulong,), 0)
                txt_b = call(call(cand, "string", c_void_p), "UTF8String", c_char_p)
                raw_txt = txt_b.decode("utf-8", errors="replace") if txt_b else ""
                try:
                    obs_conf = float(call(cand, "confidence", c_float))
                except Exception:
                    obs_conf = -1.0
                if not (0.0 <= obs_conf <= 1.0):
                    obs_conf = -1.0
                if raw_txt.strip():
                    bbox = call(obs, "boundingBox", CGRect)
                    x_pt = bbox.origin.x * page_w_pt
                    x_end_pt = (bbox.origin.x + bbox.size.width) * page_w_pt
                    y_pt = (bbox.origin.y + bbox.size.height * 0.5) * page_h_pt
                    line_len = max(len(raw_txt), 1)
                    for m in re.finditer(r"\S+", raw_txt):
                        tok = m.group(0)
                        s, e = m.start(), m.end()
                        u16_loc = len(raw_txt[:s].encode("utf-16-le")) // 2
                        u16_len = len(tok.encode("utf-16-le")) // 2
                        rect_obs = call(
                            cand,
                            "boundingBoxForRange:error:",
                            c_void_p,
                            (NSRange, c_void_p),
                            NSRange(u16_loc, u16_len),
                            None,
                        )
                        if rect_obs:
                            wbox = call(rect_obs, "boundingBox", CGRect)
                            if wbox.size.width > 0.0:
                                wx0 = wbox.origin.x * page_w_pt
                                wx1 = (wbox.origin.x + wbox.size.width) * page_w_pt
                                wy = (wbox.origin.y + wbox.size.height * 0.5) * page_h_pt
                            else:
                                wx0 = x_pt + (s / line_len) * (x_end_pt - x_pt)
                                wx1 = x_pt + (e / line_len) * (x_end_pt - x_pt)
                                wy = y_pt
                        else:
                            wx0 = x_pt + (s / line_len) * (x_end_pt - x_pt)
                            wx1 = x_pt + (e / line_len) * (x_end_pt - x_pt)
                            wy = y_pt
                        raw_words.extend(
                            (sy, sx0, sx1, stok, obs_conf) for sy, sx0, sx1, stok in _split_glued_ocr_token(wy, wx0, wx1, tok)
                        )
        return [
            OcrWordToken(page=page_idx, x=round(wx0, 2), y=round(wy, 2), text=tok.strip(), x_end=round(wx1, 2),
                         conf=round(conf, 4) if conf >= 0.0 else -1.0)
            for wy, wx0, wx1, tok, conf in sorted(raw_words, key=lambda w: (-w[0], w[1]))
            if tok.strip()
        ]
    except Exception:
        return []


def _ocr_image_apple_vision(img_bytes: bytes, page_idx: int, page_w_pt: float, page_h_pt: float) -> List[OcrWordToken]:
    """Run multi-pass macOS Apple Vision OCR (raw + gamma-darkened + optional watermark-whitened) with box deduplication."""
    if sys.platform != "darwin":
        return []
    pass0 = _ocr_image_apple_vision_single(img_bytes, page_idx, page_w_pt, page_h_pt)
    gray_info = _extract_gray_from_image_bytes(img_bytes)
    if gray_info is None:
        return pass0
    w, h, gray_bytes = gray_info
    sample = gray_bytes[::31]
    shaded_count = sum(1 for px in sample if 90 <= px <= 220)
    shaded_ratio = shaded_count / max(len(sample), 1)
    has_noise = any(re.search(r"[~?|]", t.text) for t in pass0)
    if pass0 and len(pass0) >= 500 and shaded_ratio < 0.008 and not has_noise:
        return pass0

    passes: List[List[OcrWordToken]] = [pass0]
    luts = [_LUT_GAMMA_DARK]
    if len(pass0) < 60:
        luts.append(_LUT_WATERMARK_WHITE)
    for lut in luts:
        png_var = _gray_bytes_to_png(gray_bytes.translate(lut), w, h)
        toks_var = _ocr_image_apple_vision_single(png_var, page_idx, page_w_pt, page_h_pt)
        if toks_var:
            passes.append(toks_var)
    return _merge_multipass_vision_tokens(passes)


def _merge_ocr_words_to_cells(
    raw_words: Sequence[Tuple[Any, ...]],
    page_idx: int,
    y_tol: float = 4.0,
    cell_gap_pt: float = 11.0,
) -> List[OcrWordToken]:
    """
    Group closely spaced OCR words on the same horizontal row into table cell tokens.
    Words are (y, x0, x1, text) or (y, x0, x1, text, conf); a cell's confidence is the character-weighted mean.
    """
    if not raw_words:
        return []
    words = [(w[0], w[1], w[2], w[3], (w[4] if len(w) > 4 else -1.0)) for w in raw_words]
    ordered = sorted(words, key=lambda w: (-w[0], w[1]))
    lines: List[List[Tuple[float, float, float, str, float]]] = []
    for w in ordered:
        if not lines or abs(lines[-1][0][0] - w[0]) > y_tol:
            lines.append([w])
        else:
            lines[-1].append(w)

    def _emit(y: float, x0: float, x1: float, txt: str, confs: List[Tuple[float, int]]) -> OcrWordToken:
        known = [(c, n) for c, n in confs if c >= 0.0]
        tot = sum(n for _, n in known)
        conf = round(sum(c * n for c, n in known) / tot, 4) if tot else -1.0
        return OcrWordToken(page=page_idx, x=round(x0, 2), y=round(y, 2), text=txt.strip(), x_end=round(x1, 2), conf=conf)

    tokens: List[OcrWordToken] = []
    for line in lines:
        line.sort(key=lambda w: w[1])
        cur_y, cur_x0, cur_x1, cur_txt, c0 = line[0]
        cur_confs = [(c0, max(1, len(cur_txt)))]
        for y, x0, x1, txt, conf in line[1:]:
            if x0 - cur_x1 <= cell_gap_pt:
                cur_txt = f"{cur_txt} {txt}"
                cur_x1 = max(cur_x1, x1)
                cur_confs.append((conf, max(1, len(txt))))
            else:
                tokens.append(_emit(cur_y, cur_x0, cur_x1, cur_txt, cur_confs))
                cur_y, cur_x0, cur_x1, cur_txt = y, x0, x1, txt
                cur_confs = [(conf, max(1, len(txt)))]
        tokens.append(_emit(cur_y, cur_x0, cur_x1, cur_txt, cur_confs))
    return tokens


def page_confidence(tokens: Sequence[OcrWordToken]) -> Optional[float]:
    """Character-weighted mean confidence of the tokens that carry one; None when no engine reported any."""
    num = 0.0
    den = 0
    for t in tokens:
        if t.conf >= 0.0:
            n = max(1, len(t.text.strip()))
            num += t.conf * n
            den += n
    return round(num / den, 4) if den else None


# ---------------------------------------------------------------------------
# Public OCR Orchestrators
# ---------------------------------------------------------------------------

def glyph_ocr_allowed(data: Optional[bytes] = None) -> bool:
    """
    The 5x7 glyph matcher is restricted to CypherSOL fixtures (PDF-11): documents carrying
    `FIXTURE_PRODUCER_MARKER`, or any document when CYPHERSOL_ALLOW_GLYPH_OCR=1 is set.
    """
    if os.environ.get(GLYPH_OCR_ENV, "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    return bool(data) and FIXTURE_PRODUCER_MARKER in data  # type: ignore[operator]


def _gate_page(tokens: List[OcrWordToken], engine: str) -> Tuple[str, Optional[float]]:
    """Confidence gate for one OCR'd page: ("ok" | "low" | "reject", page confidence or None)."""
    conf = page_confidence(tokens)
    if engine == "pure_python_glyph_ocr":
        return ("ok" if conf is not None and conf >= GLYPH_MIN_CONFIDENCE else "reject"), conf
    if conf is None:
        return "ok", None
    if conf < OCR_REJECT_CONFIDENCE:
        return "reject", conf
    if conf < OCR_LOW_CONFIDENCE:
        return "low", conf
    return "ok", conf


def ocr_image_bytes(
    image_bytes: bytes,
    page_idx: int = 1,
    page_w_pt: float = 595.0,
    page_h_pt: float = 842.0,
    *,
    allow_glyph_ocr: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Run offline OCR on a standalone scanned image buffer: Tesseract, then Apple Vision, then (fixtures only,
    see `glyph_ocr_allowed`) the 5x7 glyph matcher. Pages below the confidence gate are rejected.

    Returns {"tokens", "pages_text", "engine" ("tesseract" | "apple_vision" | "pure_python_glyph_ocr" |
    "none" | "unavailable"), "confidence" (0..1 or None), "low_confidence" (bool), "rejected" (reason or None)}.
    """
    allow_glyph = glyph_ocr_allowed(image_bytes) if allow_glyph_ocr is None else allow_glyph_ocr
    gray_info = _load_image_as_gray(image_bytes)
    img_w = gray_info[0] if gray_info else int(page_w_pt * 2)
    img_h = gray_info[1] if gray_info else int(page_h_pt * 2)
    rejected: Optional[str] = None
    best_conf: Optional[float] = None

    def _done(toks: List[OcrWordToken], engine: str, conf: Optional[float], low: bool) -> Dict[str, Any]:
        return {"tokens": toks, "pages_text": [" ".join(t.text for t in toks)], "engine": engine,
                "confidence": conf, "low_confidence": low, "rejected": None}

    attempts: List[Tuple[str, Any]] = []
    tess = _tesseract_bin()
    if tess:
        def _tess() -> List[OcrWordToken]:
            with private_tempdir() as td:
                img_p = write_private_file(td, "page.img", image_bytes)
                return _ocr_image_tesseract(img_p, page_idx, page_w_pt, page_h_pt, img_w, img_h)
        attempts.append(("tesseract", _tess))
    if _vision_available():
        attempts.append(("apple_vision", lambda: _ocr_image_apple_vision(image_bytes, page_idx, page_w_pt, page_h_pt)))
    if allow_glyph and gray_info is not None:
        attempts.append(("pure_python_glyph_ocr",
                         lambda: _ocr_gray_image_pure_python(gray_info[0], gray_info[1], gray_info[2],
                                                             page_idx, page_w_pt, page_h_pt)))
    for engine, run in attempts:
        toks = run()
        if not toks:
            continue
        verdict, conf = _gate_page(toks, engine)
        if verdict != "reject":
            return _done(toks, engine, conf, verdict == "low")
        rejected = f"{engine} confidence {conf if conf is not None else 'n/a'} below the gate"
        best_conf = conf
    engine_label = "none" if attempts else "unavailable"
    return {"tokens": [], "pages_text": [], "engine": engine_label, "confidence": best_conf,
            "low_confidence": False, "rejected": rejected}


def ocr_pdf_bytes(
    pdf_bytes: bytes,
    page_sizes: Optional[List[Tuple[float, float]]] = None,
    *,
    pages: Optional[Sequence[int]] = None,
    password: Optional[str] = None,
    dpi: int = OCR_RENDER_DPI,
    page_timeout: float = OCR_PAGE_TIMEOUT_S,
    deadline: Optional[float] = None,
    allow_glyph_ocr: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Offline OCR of scanned PDF pages, one page at a time.

    `deadline` (a `time.monotonic()` value) bounds the whole document: each render gets at most the remaining
    time, and pages not started before it are listed in "skipped_pages".

    `pages` (1-based) restricts OCR to those pages (default: every page). `password` is forwarded to the
    renderer for encrypted scans (callers decrypt in-process first whenever pikepdf/qpdf is available, so it is
    normally None). Each page is rendered at `dpi` (300) in grayscale with its own timeout, then read by
    Tesseract CLI, then macOS Apple Vision. The pure-Python 5x7 glyph matcher (embedded rasters first, then the
    rendered page) runs only for CypherSOL fixtures (`glyph_ocr_allowed`, overridable by `allow_glyph_ocr`).

    Confidence gate (PDF-11): a page whose character-weighted confidence is below OCR_REJECT_CONFIDENCE (or,
    for the glyph matcher, below GLYPH_MIN_CONFIDENCE) is discarded and listed in "rejected_pages"; one below
    OCR_LOW_CONFIDENCE is kept and listed in "low_confidence_pages".

    Returns {"tokens", "pages_text" (texts of OCR'd pages, in page order), "pages_text_by_page",
    "ocr_pages" (pages that produced accepted text), "failed_pages", "skipped_pages", "engine" (engine used for
    most pages, "none", or "unavailable" when no OCR engine exists on this host), "engines_by_page",
    "confidence_by_page" {page: 0..1}, "low_confidence_pages", "rejected_pages" {page: reason},
    "glyph_ocr_allowed"}.
    """
    sizes = list(page_sizes or [(595.0, 842.0)])
    if pages:
        target_pages = sorted({int(p) for p in pages if int(p) >= 1})
    else:
        n_pages = pdf_page_count(pdf_bytes, password=password) or len(sizes)
        target_pages = list(range(1, n_pages + 1))

    tess = _tesseract_bin()
    vision = _vision_available()
    external_ocr = bool(tess) or vision
    allow_glyph = glyph_ocr_allowed(pdf_bytes) if allow_glyph_ocr is None else allow_glyph_ocr
    budget = InflateBudget()
    embedded_by_page: Optional[Dict[int, List[Tuple[int, int, bytearray]]]] = None

    all_tokens: List[OcrWordToken] = []
    text_by_page: Dict[int, str] = {}
    engines_by_page: Dict[int, str] = {}
    conf_by_page: Dict[int, float] = {}
    low_pages: List[int] = []
    rejected: Dict[int, str] = {}
    failed_pages: List[int] = []
    skipped_pages: List[int] = []

    for p_idx in target_pages:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 1.0:
                skipped_pages.append(p_idx)
                continue
            page_timeout = min(page_timeout, remaining)
        pw, ph = sizes[min(p_idx - 1, len(sizes) - 1)]
        rendered: Optional[Tuple[int, int, bytearray]] = None
        rendered_tried = False
        accepted: Optional[Tuple[List[OcrWordToken], str, Optional[float], bool]] = None
        reasons: List[str] = []

        def _consider(toks: List[OcrWordToken], engine: str) -> bool:
            nonlocal accepted
            if not toks:
                return False
            verdict, conf = _gate_page(toks, engine)
            if verdict == "reject":
                reasons.append(f"{engine} confidence {conf:.2f}" if conf is not None else f"{engine} unscored")
                return False
            accepted = (toks, engine, conf, verdict == "low")
            return True

        if external_ocr:
            rendered = render_pdf_page_gray(pdf_bytes, p_idx, dpi=dpi, password=password,
                                            timeout=page_timeout, page_size=(pw, ph))
            rendered_tried = True
            if rendered is not None:
                img_w, img_h, gray = rendered
                png_b = _gray_bytes_to_png(bytes(gray), img_w, img_h)
                done = False
                if tess:
                    with private_tempdir() as td:
                        img_p = write_private_file(td, "page.png", png_b)
                        done = _consider(_ocr_image_tesseract(img_p, p_idx, pw, ph, img_w, img_h), "tesseract")
                if not done and vision:
                    _consider(_ocr_image_apple_vision(png_b, p_idx, pw, ph), "apple_vision")

        if accepted is None and allow_glyph:
            if embedded_by_page is None:
                embedded_by_page = {}
                for e_page, w, h, gray in _extract_embedded_pdf_images(pdf_bytes, pages=target_pages, budget=budget):
                    embedded_by_page.setdefault(e_page, []).append((w, h, gray))
            emb_toks: List[OcrWordToken] = []
            for w, h, gray in embedded_by_page.get(p_idx, []):
                emb_toks.extend(_ocr_gray_image_pure_python(w, h, gray, p_idx, pw, ph))
            if not _consider(emb_toks, "pure_python_glyph_ocr"):
                if not rendered_tried:
                    rendered = render_pdf_page_gray(pdf_bytes, p_idx, dpi=dpi, password=password,
                                                    timeout=page_timeout, page_size=(pw, ph))
                if rendered is not None:
                    _consider(_ocr_gray_image_pure_python(rendered[0], rendered[1], rendered[2], p_idx, pw, ph),
                              "pure_python_glyph_ocr")

        if accepted is not None:
            toks, engine, conf, low = accepted
            all_tokens.extend(toks)
            text_by_page[p_idx] = " ".join(t.text for t in toks)
            engines_by_page[p_idx] = engine
            if conf is not None:
                conf_by_page[p_idx] = conf
            if low:
                low_pages.append(p_idx)
        else:
            failed_pages.append(p_idx)
            if reasons:
                rejected[p_idx] = "; ".join(reasons)

    ocr_pages = sorted(text_by_page)
    engine_label = "none" if (external_ocr or allow_glyph) else "unavailable"
    if engines_by_page:
        counts: Dict[str, int] = {}
        for eng in engines_by_page.values():
            counts[eng] = counts.get(eng, 0) + 1
        engine_label = max(counts.items(), key=lambda kv: kv[1])[0]
    return {
        "tokens": all_tokens,
        "pages_text": [text_by_page[p] for p in ocr_pages],
        "pages_text_by_page": text_by_page,
        "ocr_pages": ocr_pages,
        "failed_pages": failed_pages,
        "skipped_pages": skipped_pages,
        "engine": engine_label,
        "engines_by_page": engines_by_page,
        "confidence_by_page": conf_by_page,
        "low_confidence_pages": low_pages,
        "rejected_pages": rejected,
        "glyph_ocr_allowed": allow_glyph,
    }
