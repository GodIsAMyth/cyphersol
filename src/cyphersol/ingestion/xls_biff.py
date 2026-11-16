"""Pure-stdlib reader for legacy binary Excel (.xls, BIFF8 inside an OLE2 compound file).

Used when the optional `xlrd` package is not installed (ING-06). Reads cell values only: shared strings,
inline labels, numbers, RK/MULRK numbers, booleans and cached formula results. Date-formatted numbers become
ISO dates. Encrypted (FILEPASS) workbooks and pre-BIFF8 (Excel 95 and older) files are rejected clearly.
"""

from __future__ import annotations

import datetime as dt
import math
import re
import struct
from typing import Any, Dict, List, Optional, Tuple

_CFB_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_ENDOFCHAIN = 0xFFFFFFFE
_FREESECT = 0xFFFFFFFF
_MAX_CELLS = 5_000_000
_MAX_ROWS = 1_048_576
_MAX_COLS = 16_384

# BIFF8 record ids
_BOF, _EOF, _BOUNDSHEET, _SST, _CONTINUE = 0x0809, 0x000A, 0x0085, 0x00FC, 0x003C
_FORMAT, _XF, _DATEMODE, _FILEPASS = 0x041E, 0x00E0, 0x0022, 0x002F
_LABELSST, _LABEL, _NUMBER, _RK, _MULRK = 0x00FD, 0x0204, 0x0203, 0x027E, 0x00BD
_FORMULA, _STRING, _BOOLERR = 0x0006, 0x0207, 0x0205

_BUILTIN_DATE_FORMATS = frozenset(range(14, 23)) | {45, 46, 47}


class XlsFormatError(ValueError):
    """The bytes are not a readable BIFF8 workbook."""


# ---------------------------------------------------------------------------
# OLE2 / Compound File Binary container
# ---------------------------------------------------------------------------


def _cfb_stream(data: bytes, names: Tuple[str, ...]) -> bytes:
    if len(data) < 512 or data[:8] != _CFB_MAGIC:
        raise XlsFormatError("not an OLE2 compound file")
    sector_shift, mini_shift = struct.unpack_from("<HH", data, 0x1E)
    if not 7 <= sector_shift <= 16 or not 2 <= mini_shift < sector_shift:
        raise XlsFormatError("invalid sector size")
    ssz, mssz = 1 << sector_shift, 1 << mini_shift
    (n_fat, first_dir, _tx, cutoff, first_minifat, n_minifat, first_difat, n_difat) = struct.unpack_from(
        "<IIIIIIII", data, 0x2C
    )
    n_sectors = max(0, (len(data) - ssz) // ssz) if ssz <= len(data) else 0

    def sector(i: int) -> bytes:
        if i >= n_sectors:
            raise XlsFormatError("sector index out of range")
        off = (i + 1) * ssz
        return data[off : off + ssz]

    per = ssz // 4
    difat = [x for x in struct.unpack_from("<109I", data, 0x4C) if x != _FREESECT]
    nxt, seen = first_difat, 0
    while nxt not in (_ENDOFCHAIN, _FREESECT) and seen < max(n_difat, 0) + 1 and seen <= n_sectors:
        vals = struct.unpack(f"<{per}I", sector(nxt))
        difat.extend(x for x in vals[:-1] if x != _FREESECT)
        nxt, seen = vals[-1], seen + 1
    fat: List[int] = []
    for s in difat[: max(n_fat, 0) or len(difat)]:
        fat.extend(struct.unpack(f"<{per}I", sector(s)))

    def chain(start: int, table: List[int], limit: int) -> List[int]:
        out: List[int] = []
        cur = start
        while cur not in (_ENDOFCHAIN, _FREESECT):
            if cur >= len(table) or len(out) > limit:
                raise XlsFormatError("broken sector chain")
            out.append(cur)
            cur = table[cur]
        return out

    def read_chain(start: int, size: Optional[int] = None) -> bytes:
        buf = b"".join(sector(i) for i in chain(start, fat, n_sectors))
        return buf if size is None else buf[:size]

    dir_bytes = read_chain(first_dir)
    entries = []
    for off in range(0, len(dir_bytes) - 127, 128):
        name_len, etype = struct.unpack_from("<HB", dir_bytes, off + 64)
        name = dir_bytes[off : off + max(0, min(name_len, 64) - 2)].decode("utf-16-le", "replace")
        start, size = struct.unpack_from("<II", dir_bytes, off + 116)
        entries.append((name, etype, start, size))
    if not entries or entries[0][1] != 5:
        raise XlsFormatError("missing root directory entry")

    wanted = {n.lower() for n in names}
    for name, etype, start, size in entries:
        if etype != 2 or name.lower() not in wanted:
            continue
        if size > len(data) * 2 + 4096:
            raise XlsFormatError("stream size exceeds file size")
        if size >= cutoff:
            return read_chain(start, size)
        # Small stream: lives in the mini stream (root entry's chain), addressed through the mini FAT
        mini_stream = read_chain(entries[0][2], entries[0][3])
        minifat_raw = read_chain(first_minifat) if n_minifat else b""
        minifat = list(struct.unpack(f"<{len(minifat_raw) // 4}I", minifat_raw[: len(minifat_raw) // 4 * 4]))
        parts = [mini_stream[i * mssz : (i + 1) * mssz] for i in chain(start, minifat, len(minifat))]
        return b"".join(parts)[:size]
    raise XlsFormatError("no Workbook stream (not an Excel file)")


# ---------------------------------------------------------------------------
# BIFF8 records
# ---------------------------------------------------------------------------


def _records(stream: bytes, pos: int = 0):
    n = len(stream)
    while pos + 4 <= n:
        rid, ln = struct.unpack_from("<HH", stream, pos)
        body = stream[pos + 4 : pos + 4 + ln]
        yield pos, rid, body
        pos += 4 + ln


class _SegReader:
    """Reads BIFF8 unicode strings that may be split across CONTINUE records."""

    def __init__(self, segments: List[bytes]) -> None:
        self.segs = segments
        self.i = 0
        self.p = 0

    def _ensure(self) -> None:
        while self.i < len(self.segs) and self.p >= len(self.segs[self.i]):
            self.i += 1
            self.p = 0
        if self.i >= len(self.segs):
            raise XlsFormatError("truncated shared string table")

    def take(self, n: int) -> bytes:
        out = bytearray()
        while n > 0:
            self._ensure()
            seg = self.segs[self.i]
            chunk = seg[self.p : self.p + n]
            out += chunk
            self.p += len(chunk)
            n -= len(chunk)
        return bytes(out)

    def chars(self, cch: int, wide: bool) -> str:
        parts: List[str] = []
        while cch > 0:
            self._ensure()
            seg = self.segs[self.i]
            avail = len(seg) - self.p
            width = 2 if wide else 1
            n = min(cch, avail // width)
            if n == 0:
                raise XlsFormatError("malformed shared string")
            raw = seg[self.p : self.p + n * width]
            parts.append(raw.decode("utf-16-le", "replace") if wide else raw.decode("latin-1"))
            self.p += n * width
            cch -= n
            if cch > 0:
                # String continues in the next CONTINUE record, which restarts with an option-flags byte
                self.i += 1
                self.p = 0
                if self.i >= len(self.segs):
                    raise XlsFormatError("truncated shared string table")
                wide = bool(self.segs[self.i][0] & 0x01)
                self.p = 1
        return "".join(parts)

    def unicode_string(self) -> str:
        cch = struct.unpack("<H", self.take(2))[0]
        flags = self.take(1)[0]
        runs = struct.unpack("<H", self.take(2))[0] if flags & 0x08 else 0
        ext = struct.unpack("<I", self.take(4))[0] if flags & 0x04 else 0
        text = self.chars(cch, bool(flags & 0x01))
        if runs:
            self.take(4 * runs)
        if ext:
            self.take(ext)
        return text


def _xl_unicode(body: bytes, off: int, len_bytes: int) -> str:
    """XLUnicodeString (2-byte length) or ShortXLUnicodeString (1-byte length) inside one record."""
    if len_bytes == 2:
        cch = struct.unpack_from("<H", body, off)[0]
    else:
        cch = body[off]
    flags = body[off + len_bytes]
    p = off + len_bytes + 1
    if flags & 0x01:
        return body[p : p + 2 * cch].decode("utf-16-le", "replace")
    return body[p : p + cch].decode("latin-1")


def _rk_value(rk: int) -> float:
    if rk & 0x02:
        val: float = float(rk >> 2 if rk < 0x80000000 else (rk - (1 << 32)) >> 2)
    else:
        val = struct.unpack("<d", struct.pack("<Q", (rk & 0xFFFFFFFC) << 32))[0]
    return val / 100.0 if rk & 0x01 else val


_FMT_LITERAL_RE = re.compile(r'"[^"]*"|\[[^\]]*\]|\\.|_.|\*.')


def _is_date_format(fmt: str) -> bool:
    s = _FMT_LITERAL_RE.sub("", fmt).lower()
    if not s or s == "general":
        return False
    return bool(re.search(r"[dmyhs]", s.split(";")[0]))


def _serial_to_cell(serial: float, date1904: bool) -> Any:
    if not math.isfinite(serial) or serial < 0 or serial > 2958465:
        return serial
    base = dt.datetime(1904, 1, 1) if date1904 else dt.datetime(1899, 12, 30)
    value = base + dt.timedelta(days=serial)
    if value.time() == dt.time(0, 0):
        return value.date().isoformat()
    return value.replace(microsecond=0).isoformat(sep=" ")


def _num(v: float) -> Any:
    return int(v) if math.isfinite(v) and v.is_integer() else v


def read_xls_biff(data: bytes) -> Dict[str, List[List[Any]]]:
    """Read every worksheet of a BIFF8 .xls workbook into lists of plain cell values."""
    try:
        stream = _cfb_stream(data, ("Workbook", "Book"))
    except struct.error as exc:
        raise XlsFormatError(f"damaged compound file ({exc})") from exc
    if len(stream) < 8 or struct.unpack_from("<H", stream, 0)[0] != _BOF:
        raise XlsFormatError("Workbook stream does not start with a BOF record")
    if struct.unpack_from("<H", stream, 4)[0] != 0x0600:
        raise XlsFormatError("only BIFF8 (Excel 97-2003) workbooks are supported; save it as .xlsx")

    sheets: List[Tuple[str, int]] = []
    sst_segments: List[bytes] = []
    formats: Dict[int, str] = {}
    xf_fmt: List[int] = []
    date1904 = False
    in_sst = False
    sst_unique = 0
    try:
        for _pos, rid, body in _records(stream):
            if rid == _CONTINUE and in_sst:
                sst_segments.append(body)
                continue
            in_sst = False
            if rid == _FILEPASS:
                raise XlsFormatError("workbook is password-protected; save an unprotected copy and retry")
            if rid == _BOUNDSHEET and len(body) >= 8:
                sh_pos, sh_type = struct.unpack_from("<I", body, 0)[0], body[5]
                if sh_type == 0:  # worksheet (not chart / macro sheet)
                    sheets.append((_xl_unicode(body, 6, 1), sh_pos))
            elif rid == _SST and len(body) >= 8:
                sst_unique = struct.unpack_from("<I", body, 4)[0]
                sst_segments = [body[8:]]
                in_sst = True
            elif rid == _FORMAT and len(body) >= 5:
                formats[struct.unpack_from("<H", body, 0)[0]] = _xl_unicode(body, 2, 2)
            elif rid == _XF and len(body) >= 4:
                xf_fmt.append(struct.unpack_from("<H", body, 2)[0])
            elif rid == _DATEMODE and len(body) >= 2:
                date1904 = struct.unpack_from("<H", body, 0)[0] == 1
            elif rid == _EOF:
                break
        sst: List[str] = []
        if sst_segments:
            reader = _SegReader(sst_segments)
            for _ in range(sst_unique):
                try:
                    sst.append(reader.unicode_string())
                except XlsFormatError:
                    break
    except struct.error as exc:
        raise XlsFormatError(f"damaged workbook globals ({exc})") from exc

    date_xf = {
        i for i, f in enumerate(xf_fmt) if f in _BUILTIN_DATE_FORMATS or (f in formats and _is_date_format(formats[f]))
    }

    def number(xf: int, v: float) -> Any:
        return _serial_to_cell(v, date1904) if xf in date_xf else _num(v)

    out: Dict[str, List[List[Any]]] = {}
    total_cells = 0
    for name, sh_pos in sheets:
        cells: Dict[Tuple[int, int], Any] = {}
        pending_formula: Optional[Tuple[int, int]] = None
        if sh_pos >= len(stream):
            out[name] = []
            continue
        try:
            for _p, rid, body in _records(stream, sh_pos):
                if rid == _EOF:
                    break
                if total_cells > _MAX_CELLS:
                    raise XlsFormatError("workbook has too many cells")
                if rid == _LABELSST and len(body) >= 10:
                    r, c, _xf, idx = struct.unpack_from("<HHHI", body, 0)
                    cells[(r, c)] = sst[idx] if idx < len(sst) else ""
                elif rid == _LABEL and len(body) >= 9:
                    r, c = struct.unpack_from("<HH", body, 0)
                    cells[(r, c)] = _xl_unicode(body, 6, 2)
                elif rid == _NUMBER and len(body) >= 14:
                    r, c, xf = struct.unpack_from("<HHH", body, 0)
                    cells[(r, c)] = number(xf, struct.unpack_from("<d", body, 6)[0])
                elif rid == _RK and len(body) >= 10:
                    r, c, xf, rk = struct.unpack_from("<HHHI", body, 0)
                    cells[(r, c)] = number(xf, _rk_value(rk))
                elif rid == _MULRK and len(body) >= 6:
                    r, c0 = struct.unpack_from("<HH", body, 0)
                    n = (len(body) - 6) // 6
                    for k in range(n):
                        xf, rk = struct.unpack_from("<HI", body, 4 + 6 * k)
                        cells[(r, c0 + k)] = number(xf, _rk_value(rk))
                elif rid == _BOOLERR and len(body) >= 8:
                    r, c = struct.unpack_from("<HH", body, 0)
                    cells[(r, c)] = bool(body[6]) if body[7] == 0 else ""
                elif rid == _FORMULA and len(body) >= 14:
                    r, c, xf = struct.unpack_from("<HHH", body, 0)
                    res = body[6:14]
                    if res[6:8] == b"\xff\xff":
                        kind = res[0]
                        if kind == 0:
                            pending_formula = (r, c)  # value follows in a STRING record
                            continue
                        cells[(r, c)] = bool(res[2]) if kind == 1 else ""
                    else:
                        cells[(r, c)] = number(xf, struct.unpack("<d", res)[0])
                elif rid == _STRING and pending_formula is not None and len(body) >= 3:
                    cells[pending_formula] = _xl_unicode(body, 0, 2)
                pending_formula = None if rid != _CONTINUE else pending_formula
        except struct.error as exc:
            raise XlsFormatError(f"damaged worksheet '{name}' ({exc})") from exc
        total_cells += len(cells)
        if not cells:
            out[name] = []
            continue
        n_rows = min(max(r for r, _ in cells) + 1, _MAX_ROWS)
        n_cols = min(max(c for _, c in cells) + 1, _MAX_COLS)
        grid: List[List[Any]] = [[""] * n_cols for _ in range(n_rows)]
        for (r, c), v in cells.items():
            if r < n_rows and c < n_cols:
                grid[r][c] = v
        out[name] = grid
    return out

