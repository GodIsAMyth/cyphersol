"""Pure-Python OpenXML (.xlsx) reader and styled multi-sheet writer (zero external deps).

Also reads legacy binary .xls (optional `xlrd`, else the stdlib BIFF8 reader in xls_biff) and HTML-table
"Excel" exports (stdlib).
"""

from __future__ import annotations

import datetime as dt
import io
import math
import re
import zipfile
from decimal import Decimal
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Sequence, Tuple
import xml.etree.ElementTree as ET

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"


def _col_letter_to_idx(ref: str) -> int:
    letters = "".join(ch for ch in ref if ch.isalpha()).upper()
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - ord("A") + 1)
    return max(0, idx - 1)


def _idx_to_col_letter(idx: int) -> str:
    res = []
    n = idx + 1
    while n > 0:
        n, rem = divmod(n - 1, 26)
        res.append(chr(65 + rem))
    return "".join(reversed(res))


_EXCEL_MAX_SERIAL = 2958465  # 9999-12-31


def _excel_serial_to_iso(serial: float, date1904: bool = False) -> str:
    base = dt.datetime(1904, 1, 1) if date1904 else dt.datetime(1899, 12, 30)
    return (base + dt.timedelta(days=serial)).date().isoformat()


def _normalize_cell(value: Any) -> Any:
    """openpyxl/xlrd values -> plain cells: datetimes become ISO dates (time kept only when present)."""
    if isinstance(value, dt.datetime):
        if value.time() == dt.time(0, 0):
            return value.date().isoformat()
        return value.isoformat(sep=" ")
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def read_xlsx_bytes(data: bytes) -> Dict[str, List[List[Any]]]:
    """Read all sheets from an .xlsx archive into {sheet_name: [[cell, ...], ...]}."""
    try:
        import openpyxl  # type: ignore

        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        out: Dict[str, List[List[Any]]] = {}
        for name in wb.sheetnames:
            ws = wb[name]
            rows: List[List[Any]] = []
            for row in ws.iter_rows(values_only=True):
                rows.append([_normalize_cell(v) if v is not None else "" for v in row])
            out[name] = rows
        return out
    except ImportError:
        pass

    sheets_out: Dict[str, List[List[Any]]] = {}
    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        names = set(zf.namelist())
        shared_strings: List[str] = []
        if "xl/sharedStrings.xml" in names:
            ss_root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in ss_root.findall(f"{{{_NS_MAIN}}}si"):
                texts = [t.text or "" for t in si.iter(f"{{{_NS_MAIN}}}t")]
                shared_strings.append("".join(texts))

        # Detect date-formatted style indices from styles.xml
        date_style_ids = set()
        if "xl/styles.xml" in names:
            st_root = ET.fromstring(zf.read("xl/styles.xml"))
            custom_date_numfmts = set()
            numfmts = st_root.find(f"{{{_NS_MAIN}}}numFmts")
            if numfmts is not None:
                for nf in numfmts.findall(f"{{{_NS_MAIN}}}numFmt"):
                    fmt_code = (nf.attrib.get("formatCode") or "").lower()
                    if any(tok in fmt_code for tok in ("yy", "dd", "mm")) and "h" not in fmt_code.replace("hh", ""):
                        try:
                            custom_date_numfmts.add(int(nf.attrib.get("numFmtId", "-1")))
                        except ValueError:
                            pass
            builtin_date_ids = {14, 15, 16, 17, 18, 19, 20, 21, 22} | custom_date_numfmts
            cell_xfs = st_root.find(f"{{{_NS_MAIN}}}cellXfs")
            if cell_xfs is not None:
                for idx, xf in enumerate(cell_xfs.findall(f"{{{_NS_MAIN}}}xf")):
                    try:
                        nf_id = int(xf.attrib.get("numFmtId", "0"))
                    except ValueError:
                        nf_id = 0
                    if nf_id in builtin_date_ids:
                        date_style_ids.add(idx)

        # Map workbook sheet order and targets
        sheet_entries: List[Tuple[str, str]] = []
        date1904 = False
        if "xl/workbook.xml" in names:
            wb_pr = ET.fromstring(zf.read("xl/workbook.xml")).find(f"{{{_NS_MAIN}}}workbookPr")
            date1904 = wb_pr is not None and wb_pr.attrib.get("date1904", "0").lower() in ("1", "true")
        if "xl/workbook.xml" in names and "xl/_rels/workbook.xml.rels" in names:
            wb_root = ET.fromstring(zf.read("xl/workbook.xml"))
            rels_root = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
            rid_to_target: Dict[str, str] = {}
            for rel in rels_root.findall(f"{{{_NS_PKG_REL}}}Relationship"):
                rid = rel.attrib.get("Id", "")
                target = rel.attrib.get("Target", "")
                target = target.lstrip("/")
                if not target.startswith("xl/"):
                    target = f"xl/{target}"
                rid_to_target[rid] = target
            for sh in wb_root.findall(f".//{{{_NS_MAIN}}}sheet"):
                sname = sh.attrib.get("name", "Sheet1")
                rid = sh.attrib.get(f"{{{_NS_REL}}}id", "")
                target = rid_to_target.get(rid, "")
                if target in names:
                    sheet_entries.append((sname, target))

        if not sheet_entries:
            for nm in sorted(names):
                if re.match(r"^xl/worksheets/sheet\d+\.xml$", nm):
                    sheet_entries.append((nm.rsplit("/", 1)[-1].replace(".xml", ""), nm))

        for sname, spath in sheet_entries:
            ws_root = ET.fromstring(zf.read(spath))
            parsed_rows: List[List[Any]] = []
            for row_el in ws_root.findall(f".//{{{_NS_MAIN}}}row"):
                row_map: Dict[int, Any] = {}
                max_col = -1
                for c_el in row_el.findall(f"{{{_NS_MAIN}}}c"):
                    ref = c_el.attrib.get("r", "")
                    col_idx = _col_letter_to_idx(ref) if ref else (max_col + 1)
                    max_col = max(max_col, col_idx)
                    c_type = c_el.attrib.get("t", "")
                    style_idx = int(c_el.attrib.get("s", "0") or 0)
                    v_el = c_el.find(f"{{{_NS_MAIN}}}v")
                    is_el = c_el.find(f"{{{_NS_MAIN}}}is")
                    val: Any = None
                    if c_type == "s" and v_el is not None and v_el.text is not None:
                        s_i = int(v_el.text)
                        val = shared_strings[s_i] if 0 <= s_i < len(shared_strings) else ""
                    elif c_type == "inlineStr" and is_el is not None:
                        val = "".join(t.text or "" for t in is_el.iter(f"{{{_NS_MAIN}}}t"))
                    elif c_type == "str" and v_el is not None:
                        val = v_el.text or ""
                    elif c_type == "b" and v_el is not None:
                        val = v_el.text == "1"
                    elif v_el is not None and v_el.text is not None:
                        raw = v_el.text.strip()
                        try:
                            num = float(raw)
                            if style_idx in date_style_ids and 0 < num <= _EXCEL_MAX_SERIAL:
                                val = _excel_serial_to_iso(num, date1904)
                            elif num.is_integer():
                                val = int(num)
                            else:
                                val = num
                        except ValueError:
                            val = raw
                    row_map[col_idx] = val
                if max_col >= 0:
                    parsed_rows.append([row_map.get(i, "") for i in range(max_col + 1)])
            sheets_out[sname] = parsed_rows
    return sheets_out



def read_xls_bytes(data: bytes) -> Dict[str, List[List[Any]]]:
    """Read a legacy binary .xls (BIFF) workbook: optional `xlrd` when installed, else the stdlib BIFF8 reader."""
    try:
        import xlrd  # type: ignore
    except ImportError:
        from .xls_biff import read_xls_biff

        try:
            return read_xls_biff(data)
        except ValueError as exc:
            raise ValueError(f"Could not read .xls workbook ({exc}); save it as .xlsx and retry.") from exc
    try:
        book = xlrd.open_workbook(file_contents=data)
    except Exception as exc:  # noqa: BLE001 - xlrd raises many private error types
        raise ValueError(f"Could not read .xls workbook ({exc}); save it as .xlsx and retry.") from exc
    out: Dict[str, List[List[Any]]] = {}
    for sheet in book.sheets():
        rows: List[List[Any]] = []
        for r in range(sheet.nrows):
            row: List[Any] = []
            for c in range(sheet.ncols):
                cell = sheet.cell(r, c)
                if cell.ctype == xlrd.XL_CELL_DATE:
                    try:
                        row.append(_normalize_cell(xlrd.xldate.xldate_as_datetime(cell.value, book.datemode)))
                    except Exception:  # noqa: BLE001 - out-of-range serials stay numeric
                        row.append(cell.value)
                elif cell.ctype in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
                    row.append("")
                elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                    row.append(bool(cell.value))
                elif cell.ctype == xlrd.XL_CELL_ERROR:
                    row.append("")
                else:
                    row.append(_normalize_cell(cell.value))
            rows.append(row)
        out[sheet.name] = rows
    return out


class _HtmlTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: List[List[str]] = []
        self._row: Optional[List[str]] = None
        self._cell: Optional[List[str]] = None
        self._colspan = 1
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth += 1
        elif tag == "tr":
            self._end_row()
            self._row = []
        elif tag in ("td", "th"):
            self._end_cell()
            if self._row is None:
                self._row = []
            self._cell = []
            try:
                self._colspan = max(1, min(50, int(dict(attrs).get("colspan") or 1)))
            except ValueError:
                self._colspan = 1
        elif tag == "br" and self._cell is not None:
            self._cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in ("script", "style"):
            self._skip_depth = max(0, self._skip_depth - 1)
        elif tag in ("td", "th"):
            self._end_cell()
        elif tag in ("tr", "table"):
            self._end_row()

    def handle_data(self, data: str) -> None:
        if self._cell is not None and not self._skip_depth:
            self._cell.append(data)

    def _end_cell(self) -> None:
        if self._cell is not None and self._row is not None:
            self._row.append(re.sub(r"\s+", " ", "".join(self._cell)).strip())
            self._row.extend([""] * (self._colspan - 1))
        self._cell = None
        self._colspan = 1

    def _end_row(self) -> None:
        self._end_cell()
        if self._row is not None and any(c for c in self._row):
            self.rows.append(self._row)
        self._row = None

    def close(self) -> None:
        super().close()
        self._end_row()


def read_html_table_rows(text: str) -> List[List[str]]:
    """Rows of every <table> in an HTML document (bank "Excel" exports are often HTML saved as .xls)."""
    parser = _HtmlTableParser()
    parser.feed(text)
    parser.close()
    return parser.rows

def _xml_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def _strip_invalid_xml_chars(text: str) -> str:
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", " ", text)


def write_xlsx_bytes(sheets: List[Tuple[str, List[Sequence[Any]]]]) -> bytes:
    """
    Create a valid multi-sheet OpenXML (.xlsx) workbook in memory.
    `sheets` is a list of `(sheet_name, rows)` where `rows[0]` is styled as header.
    """
    shared_strings: List[str] = []
    ss_index: Dict[str, int] = {}

    def get_ss_idx(s: str) -> int:
        clean = _strip_invalid_xml_chars(s)
        if clean not in ss_index:
            ss_index[clean] = len(shared_strings)
            shared_strings.append(clean)
        return ss_index[clean]

    safe_sheets: List[Tuple[str, str]] = []
    used_names = set()
    for idx, (raw_name, rows) in enumerate(sheets, start=1):
        name = re.sub(r"[\\/*?:\[\]]", "_", raw_name).strip()[:31] or f"Sheet{idx}"
        base = name
        suffix = 1
        while name in used_names:
            suffix_str = f"_{suffix}"
            name = f"{base[:31 - len(suffix_str)]}{suffix_str}"
            suffix += 1
        used_names.add(name)

        # Compute reasonable column widths
        col_widths: Dict[int, int] = {}
        for r in rows[:250]:
            for c_i, val in enumerate(r):
                s_len = len(str(val if val is not None else ""))
                col_widths[c_i] = min(48, max(col_widths.get(c_i, 10), s_len + 3))

        cols_xml = ""
        if col_widths:
            col_tags = [
                f'<col min="{c_i + 1}" max="{c_i + 1}" width="{w}" customWidth="1"/>'
                for c_i, w in sorted(col_widths.items())
            ]
            cols_xml = f"<cols>{''.join(col_tags)}</cols>"

        row_xml_parts: List[str] = []
        for r_i, row in enumerate(rows, start=1):
            style_id = "1" if r_i == 1 else "0"
            cell_parts: List[str] = []
            for c_i, val in enumerate(row):
                ref = f"{_idx_to_col_letter(c_i)}{r_i}"
                if val is None or (isinstance(val, str) and val == ""):
                    continue
                if isinstance(val, (dt.datetime, dt.date)):
                    val = _normalize_cell(val)
                if isinstance(val, bool):
                    cell_parts.append(f'<c r="{ref}" t="b" s="{style_id}"><v>{1 if val else 0}</v></c>')
                elif isinstance(val, (int, float, Decimal)):
                    if isinstance(val, Decimal):
                        if not val.is_finite():
                            continue
                        num_text = format(val, "f")
                        is_fraction = val != val.to_integral_value() or val.as_tuple().exponent < 0
                    else:
                        if isinstance(val, float) and not math.isfinite(val):
                            continue
                        num_text = repr(val) if isinstance(val, float) else str(val)
                        is_fraction = isinstance(val, float)
                    num_style = "2" if (r_i > 1 and is_fraction) else style_id
                    cell_parts.append(f'<c r="{ref}" s="{num_style}"><v>{num_text}</v></c>')
                else:
                    s_idx = get_ss_idx(str(val))
                    cell_parts.append(f'<c r="{ref}" t="s" s="{style_id}"><v>{s_idx}</v></c>')
            row_xml_parts.append(f'<row r="{r_i}">{"".join(cell_parts)}</row>')

        ws_xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            f'<worksheet xmlns="{_NS_MAIN}">'
            f"{cols_xml}"
            f'<sheetData>{"".join(row_xml_parts)}</sheetData>'
            "</worksheet>"
        )
        safe_sheets.append((name, ws_xml))

    # Build sharedStrings.xml
    ss_items = "".join(
        f'<si><t xml:space="preserve">{_xml_escape(s)}</t></si>' if s != s.strip() else f"<si><t>{_xml_escape(s)}</t></si>"
        for s in shared_strings
    )
    ss_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<sst xmlns="{_NS_MAIN}" count="{len(shared_strings)}" uniqueCount="{len(shared_strings)}">'
        f"{ss_items}</sst>"
    )

    # Minimal clean styles: 0=default, 1=bold header with light blue fill, 2=2-decimal float
    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<styleSheet xmlns="{_NS_MAIN}">'
        '<numFmts count="1"><numFmt numFmtId="164" formatCode="#,##0.00"/></numFmts>'
        '<fonts count="2">'
        '<font><sz val="10"/><name val="Calibri"/></font>'
        '<font><b/><sz val="10"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font>'
        "</fonts>"
        '<fills count="3">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF0F4C81"/></patternFill></fill>'
        "</fills>"
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/>'
        '<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        "</cellXfs>"
        "</styleSheet>"
    )

    sheet_tags = "".join(
        f'<sheet name="{_xml_escape(name)}" sheetId="{idx}" r:id="rId{idx}"/>'
        for idx, (name, _) in enumerate(safe_sheets, start=1)
    )
    wb_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<workbook xmlns="{_NS_MAIN}" xmlns:r="{_NS_REL}">'
        f"<sheets>{sheet_tags}</sheets></workbook>"
    )

    rel_tags = "".join(
        f'<Relationship Id="rId{idx}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{idx}.xml"/>'
        for idx in range(1, len(safe_sheets) + 1)
    )
    next_id = len(safe_sheets) + 1
    rel_tags += (
        f'<Relationship Id="rId{next_id}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        f'<Relationship Id="rId{next_id + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
    )
    wb_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_NS_PKG_REL}">{rel_tags}</Relationships>'
    )

    pkg_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<Relationships xmlns="{_NS_PKG_REL}">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )

    overrides = "".join(
        f'<Override PartName="/xl/worksheets/sheet{idx}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for idx in range(1, len(safe_sheets) + 1)
    )
    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        f"{overrides}</Types>"
    )
    # Fix workbook content type override explicitly
    content_types_xml = content_types_xml.replace(
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
    )

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", pkg_rels_xml)
        zf.writestr("xl/workbook.xml", wb_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels_xml)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/sharedStrings.xml", ss_xml)
        for idx, (_, ws_xml) in enumerate(safe_sheets, start=1):
            zf.writestr(f"xl/worksheets/sheet{idx}.xml", ws_xml)
    return buf.getvalue()
