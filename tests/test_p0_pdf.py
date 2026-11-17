"""P0 regression tests for the native PDF parser and the OCR bridge (review findings PDF-01..PDF-26)."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import struct
import sys
import time
import unittest
import zlib
from decimal import Decimal
from typing import Dict, List, Optional, Sequence, Tuple
from unittest import mock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.ingestion import ocr_engine, pdf_parser  # noqa: E402
from cyphersol.ingestion.ocr_engine import (  # noqa: E402
    InflateBudget,
    _find_binary,
    _inflate_bounded,
    _png_dimensions,
    generate_scanned_pdf_statement,
    private_tempdir,
    render_pdf_page_png,
)
from cyphersol.ingestion.parser import parse_statement_bytes  # noqa: E402
from cyphersol.ingestion.pdf_parser import (  # noqa: E402
    PdfTextToken,
    _extract_content_tokens,
    _group_tokens_into_rows,
    _is_corrupted_ocr_text,
    _line_has_date,
    extract_pdf_data,
    generate_pdf_statement,
    is_date_token,
)
from cyphersol.models import AnomalyFlag  # noqa: E402

SAMPLES = os.path.join(ROOT_DIR, "sample_statements")
HDFC_SAMPLE = os.path.join(SAMPLES, "CASE041_POOJAN-VIG_HDFC_501002938471.pdf")
SBI_SAMPLE = os.path.join(SAMPLES, "CA2025_SHREE-BALAJI-ENTERPRISES_SBI_382910445566.pdf")
HAS_POPPLER = bool(_find_binary("pdftotext", ("/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext")))
HAS_PDFTOPPM = bool(_find_binary("pdftoppm", ("/opt/homebrew/bin/pdftoppm", "/usr/local/bin/pdftoppm")))


# ---------------------------------------------------------------------------
# Minimal PDF writer and RC4 (Standard Security Handler R2) encryptor for fixtures
# ---------------------------------------------------------------------------

def build_pdf(pages: Sequence[Dict[str, object]], extra_objects: Optional[Dict[int, bytes]] = None) -> bytes:
    """pages: [{"content": bytes, "resources": str}] -> PDF bytes. Objects 1/2 are Catalog/Pages, 3/4 fonts."""
    objects: Dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
    }
    next_id = 10
    kids: List[int] = []
    for page in pages:
        pid, cid = next_id, next_id + 1
        next_id += 2
        res = str(page.get("resources") or "/Font << /F1 3 0 R /F2 4 0 R >>")
        content = page["content"]
        assert isinstance(content, bytes)
        objects[pid] = (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << {res} >> "
                        f"/Contents {cid} 0 R >>").encode("ascii")
        objects[cid] = f"<< /Length {len(content)} >>\nstream\n".encode("ascii") + content + b"\nendstream"
        kids.append(pid)
    objects[2] = f"<< /Type /Pages /Kids [{' '.join(f'{k} 0 R' for k in kids)}] /Count {len(kids)} >>".encode("ascii")
    objects.update(extra_objects or {})
    return _serialize(objects, b"/Root 1 0 R")


def _serialize(objects: Dict[int, bytes], trailer_extra: bytes) -> bytes:
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    max_id = max(objects)
    offsets: Dict[int, int] = {}
    for oid in sorted(objects):
        offsets[oid] = len(out)
        out += f"{oid} 0 obj\n".encode("ascii") + objects[oid] + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {max_id + 1}\n".encode("ascii") + b"0000000000 65535 f \n"
    for oid in range(1, max_id + 1):
        out += (f"{offsets[oid]:010d} 00000 n \n" if oid in offsets else "0000000000 65535 f \n").encode("ascii")
    out += b"trailer\n<< /Size " + str(max_id + 1).encode() + b" " + trailer_extra + b" >>\nstartxref\n"
    out += str(xref).encode() + b"\n%%EOF\n"
    return bytes(out)


_PAD = bytes.fromhex("28BF4E5E4E758A4164004E56FFFA01082E2E00B6D0683E802F0CA9FE6453697A")


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for k, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[k] = byte ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def encrypt_pdf_rc4(pdf_bytes: bytes, user_pw: str, owner_pw: str = "owner-secret") -> bytes:
    """Re-serialize a simple fixture PDF with 40-bit RC4 encryption (PDF Standard Security Handler, R2)."""
    pad = lambda pw: (pw.encode("latin-1") + _PAD)[:32]  # noqa: E731
    o_val = _rc4(hashlib.md5(pad(owner_pw)).digest()[:5], pad(user_pw))
    file_id = hashlib.md5(b"cyphersol-fixture").digest()
    perms = -44
    key = hashlib.md5(pad(user_pw) + o_val + struct.pack("<i", perms) + file_id).digest()[:5]
    u_val = _rc4(key, _PAD)

    objects: Dict[int, bytes] = {}
    root_ref = re.search(rb"/Root\s+(\d+\s+\d+\s+R)", pdf_bytes).group(1)  # type: ignore[union-attr]
    for m in re.finditer(rb"(\d+) 0 obj\n(.*?)\nendobj\n", pdf_bytes, flags=re.DOTALL):
        oid, body = int(m.group(1)), m.group(2)
        sm = re.match(rb"(.*?stream\n)(.*)(\nendstream)$", body, flags=re.DOTALL)
        if sm:
            obj_key = hashlib.md5(key + oid.to_bytes(3, "little") + b"\x00\x00").digest()[:10]
            body = sm.group(1) + _rc4(obj_key, sm.group(2)) + sm.group(3)
        objects[oid] = body
    enc_id = max(objects) + 1
    objects[enc_id] = (b"<< /Filter /Standard /V 1 /R 2 /O <" + o_val.hex().encode() + b"> /U <"
                       + u_val.hex().encode() + b"> /P " + str(perms).encode() + b" >>")
    trailer = (b"/Root " + root_ref + b" /Encrypt " + str(enc_id).encode() + b" 0 R /ID [<"
               + file_id.hex().encode() + b"><" + file_id.hex().encode() + b">]")
    return _serialize(objects, trailer)


def _tj(x: float, y: float, text: str, font: str = "/F1 8 Tf") -> str:
    return f"BT {font} 1 0 0 1 {x:.1f} {y:.1f} Tm ({text}) Tj ET"


def _statement_page(rows: Sequence[Sequence[str]], header: Sequence[str], xs: Sequence[float], top: float = 760.0) -> bytes:
    ops = [_tj(32, 805, "HDFC BANK LIMITED - ACCOUNT STATEMENT", "/F2 12 Tf"),
           _tj(32, 790, "Account Holder: TEST USER | Account No: 501009988776 | IFSC: HDFC0000123", "/F1 9 Tf")]
    ops.extend(_tj(x, top, h, "/F2 8.5 Tf") for x, h in zip(xs, header))
    y = top - 18
    for row in rows:
        ops.extend(_tj(x, y, cell) for x, cell in zip(xs, row) if cell)
        y -= 16
    return "\n".join(ops).encode("latin-1")


def _balance_mismatches(txns) -> int:
    bad = 0
    for prev, cur in zip(txns, txns[1:]):
        if prev.running_balance - cur.debit + cur.credit != cur.running_balance:
            bad += 1
    return bad


# ---------------------------------------------------------------------------
# PDF-01 / PDF-02 / PDF-08 / PDF-26: content-stream tokenizer and text positioning
# ---------------------------------------------------------------------------

class ContentStreamTokenizerTests(unittest.TestCase):
    def test_pdf01_redos_unterminated_tj_array_is_linear(self) -> None:
        # Old regex: `\[(?:[^\[\]]|\(...\)|<...>)*\]` backtracks exponentially when `]` is missing.
        evil = b"BT /F1 9 Tf 1 0 0 1 10 700 Tm [" + b"(ET)" * 40 + b" ET\n" + b"BT [" + b"(BT) " * 30 + b"(x)" * 30
        t0 = time.perf_counter()
        _extract_content_tokens(evil, 1)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_pdf01_redos_full_document_under_one_second(self) -> None:
        evil = b"BT /F1 9 Tf 1 0 0 1 10 700 Tm [" + b"(a)(ET)" * 60 + b"\n(" + b"\\(" * 2000 + b"\nET"
        pdf = build_pdf([{"content": evil}])
        t0 = time.perf_counter()
        extract_pdf_data(pdf)
        self.assertLess(time.perf_counter() - t0, 1.0)

    def test_pdf01_large_page_stream_is_capped(self) -> None:
        big = b"BT /F1 8 Tf 1 0 0 1 10 700 Tm " + b"(xxxxxxxx) Tj " * 200_000 + b"ET"
        t0 = time.perf_counter()
        toks = _extract_content_tokens(big, 1, max_bytes=64 * 1024)
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertLess(len(toks), 10_000)

    def test_pdf02_et_inside_string_does_not_split_block(self) -> None:
        stream = (b"BT /F1 9 Tf 1 0 0 1 50 700 Tm (NET ET MARKET) Tj 0 -20 Td (ET) Tj 0 -20 Td (BT\\) ET \\() Tj ET\n"
                  b"BT /F1 9 Tf 1 0 0 1 50 600 Tm <414243> Tj ET")
        texts = [t.text for t in _extract_content_tokens(stream, 1)]
        self.assertEqual(texts, ["NET ET MARKET", "ET", "BT) ET (", "ABC"])

    def test_pdf08_tm_scale_and_td(self) -> None:
        toks = _extract_content_tokens(b"BT /F1 1 Tf 9 0 0 9 10 700 Tm (A) Tj 30 0 Td (B) Tj ET", 1)
        self.assertEqual([t.text for t in toks], ["A", "B"])
        self.assertAlmostEqual(toks[1].x, 280.0, places=3)
        self.assertAlmostEqual(toks[1].y, 700.0, places=3)

    def test_pdf08_cm_and_q_stack(self) -> None:
        toks = _extract_content_tokens(
            b"q 2 0 0 2 0 0 cm BT /F1 8 Tf 1 0 0 1 10 10 Tm (X) Tj ET Q BT /F1 8 Tf 1 0 0 1 10 10 Tm (Y) Tj ET", 1
        )
        self.assertAlmostEqual(toks[0].x, 20.0, places=3)
        self.assertAlmostEqual(toks[1].x, 10.0, places=3)

    def test_pdf08_consecutive_tj_advance(self) -> None:
        fonts = {"F1": pdf_parser._FontInfo(widths={ord(c): 500.0 for c in "ABCD"}, default_width=500.0)}
        toks = _extract_content_tokens(b"BT /F1 10 Tf 1 0 0 1 100 500 Tm (AB) Tj (CD) Tj ET", 1, fonts=fonts)
        self.assertEqual(len(toks), 2)
        self.assertAlmostEqual(toks[0].x_end, 110.0, places=3)
        self.assertAlmostEqual(toks[1].x, 110.0, places=3)
        self.assertAlmostEqual(toks[1].x_end, 120.0, places=3)

    def test_pdf04_tj_displacement_breaks_tokens(self) -> None:
        toks = _extract_content_tokens(
            b"BT /F1 9 Tf 1 0 0 1 40 700 Tm [(D)-20(ate)-4000(Amo)10(unt)] TJ [(A)-300(B)] TJ ET", 1
        )
        texts = [t.text for t in toks]
        self.assertEqual(texts[:2], ["Date", "Amount"])
        self.assertIn("A B", texts)
        self.assertGreater(toks[1].x, toks[0].x_end + 30.0)


# ---------------------------------------------------------------------------
# PDF-07: bounded decompression
# ---------------------------------------------------------------------------

def _flate_bomb(n_bytes: int) -> bytes:
    co = zlib.compressobj(9)
    chunk = b"\0" * (1 << 20)
    parts = [co.compress(chunk) for _ in range(n_bytes >> 20)]
    parts.append(co.flush())
    return b"".join(parts)


class BoundedInflateTests(unittest.TestCase):
    def test_pdf07_per_stream_cap(self) -> None:
        bomb = _flate_bomb(64 << 20)
        t0 = time.perf_counter()
        self.assertIsNone(_inflate_bounded(bomb, max_out=1 << 20))
        self.assertLess(time.perf_counter() - t0, 1.0)
        self.assertEqual(len(_inflate_bounded(zlib.compress(b"abc" * 10), max_out=1 << 20) or b""), 30)

    def test_pdf07_per_document_budget(self) -> None:
        budget = InflateBudget(3 << 20)
        payload = zlib.compress(b"\x01" * (2 << 20))
        self.assertIsNotNone(_inflate_bounded(payload, max_out=8 << 20, budget=budget))
        self.assertIsNone(_inflate_bounded(payload, max_out=8 << 20, budget=budget))
        self.assertTrue(budget.exhausted)

    def test_pdf07_bomb_content_stream_in_document(self) -> None:
        bomb = _flate_bomb(400 << 20)
        page_ok = _tj(40, 700, "Opening line")
        objects = {
            20: b"<< /Length " + str(len(bomb)).encode() + b" /Filter /FlateDecode >>\nstream\n" + bomb + b"\nendstream",
        }
        pdf = build_pdf([{"content": page_ok.encode()}], extra_objects=objects)
        with mock.patch.object(pdf_parser, "_run_pdftotext", return_value=pdf_parser._PopplerResult("missing")):
            t0 = time.perf_counter()
            res = extract_pdf_data(pdf)
            self.assertLess(time.perf_counter() - t0, 5.0)
        self.assertIn("Opening line", " ".join(res.pages_text))


# ---------------------------------------------------------------------------
# PDF-03: tolerant date predicate and strong header splits
# ---------------------------------------------------------------------------

class DatePredicateAndHeaderTests(unittest.TestCase):
    def test_pdf03_date_token_predicate(self) -> None:
        for good in ("01/04/2025", "01-04-25", "01.04.2025", "01 Apr 2025", "01-Apr-25", "01-APR-2025",
                     "2025-04-01", "2025-04-01 10:22:11", "2025-04-01T10:22:11", "01/04/2025 10:22",
                     "1 Apr 2025", "01 Apr 25 10:22:11 AM", "Apr 01, 2025"):
            self.assertTrue(is_date_token(good), good)
        for bad in ("2025", "UTR40911001", "300.00", "1,250.50", "Apr", "01", "Balance", "40911001", "12/34"):
            self.assertFalse(is_date_token(bad), bad)

    def test_pdf03_split_date_tokens_on_a_line(self) -> None:
        line = [PdfTextToken(1, 30, 700, "01", 38), PdfTextToken(1, 40, 700, "Apr", 52),
                PdfTextToken(1, 54, 700, "2025", 70), PdfTextToken(1, 100, 700, "UPI/DR/1", 140)]
        self.assertTrue(_line_has_date(line))
        self.assertFalse(_line_has_date([PdfTextToken(1, 30, 700, "Apr", 40), PdfTextToken(1, 300, 700, "2025", 320)]))

    def _assert_columns(self, rows: List[List[str]], dates: Sequence[str]) -> None:
        hdr_idx = next(i for i, r in enumerate(rows) if any("Narration" in c for c in r))
        hdr = rows[hdr_idx]
        self.assertEqual(len(hdr), 6, hdr)
        body = [r for r in rows[hdr_idx + 1:] if r and any(d in r[0] for d in dates)]
        self.assertEqual(len(body), 2, rows)
        self.assertEqual(body[0][3:], ["", "50000.00", "150000.00"])
        self.assertEqual(body[1][3:], ["1250.50", "", "148749.50"])

    def _run_layout(self, d1: str, d2: str, split_tokens: bool) -> List[List[str]]:
        xs = [32.0, 112.0, 305.0, 385.0, 445.0, 515.0]
        header = ["Txn Date", "Narration", "Chq/Ref No", "Debit", "Credit", "Balance"]
        rows = [[d1, "UPI/CR/111/RAZORPAY", "UTR111", "", "50000.00", "150000.00"],
                [d2, "NEFT-HDFC0000123-ZOMATO", "UTR112", "1250.50", "", "148749.50"]]
        if not split_tokens:
            return extract_pdf_data(build_pdf([{"content": _statement_page(rows, header, xs)}])).rows
        # Emit each date part as its own positioned show (report-engine style)
        ops = [_tj(x, 760, h, "/F2 8.5 Tf") for x, h in zip(xs, header)]
        y = 742.0
        for row in rows:
            dx = 32.0
            for part in row[0].split(" "):
                ops.append(_tj(dx, y, part))
                dx += len(part) * 4.6 + 2.2
            ops.extend(_tj(x, y, c) for x, c in zip(xs[1:], row[1:]) if c)
            y -= 16
        content = "\n".join([_tj(32, 805, "STATEMENT OF ACCOUNT", "/F2 12 Tf")] + ops).encode()
        return extract_pdf_data(build_pdf([{"content": content}])).rows

    def test_pdf03_dd_mon_yyyy_rows_keep_header_columns(self) -> None:
        self._assert_columns(self._run_layout("01 Apr 2025", "02 Apr 2025", False), ["01 Apr 2025", "02 Apr 2025"])

    def test_pdf03_split_dd_mon_yyyy_tokens(self) -> None:
        with mock.patch.object(pdf_parser, "_run_pdftotext", return_value=pdf_parser._PopplerResult("missing")):
            rows = self._run_layout("01 Apr 2025", "02 Apr 2025", True)
        self._assert_columns(rows, ["01 Apr 2025", "02 Apr 2025"])

    def test_pdf03_iso_datetime_rows(self) -> None:
        self._assert_columns(self._run_layout("2025-04-01 10:22:11", "2025-04-02 09:00:00", False),
                             ["2025-04-01", "2025-04-02"])

    def test_pdf03_dd_mon_yy_rows(self) -> None:
        self._assert_columns(self._run_layout("01-Apr-25", "02-Apr-25", False), ["01-Apr-25", "02-Apr-25"])

    def test_pdf03_hdfc_sample_regression(self) -> None:
        with open(HDFC_SAMPLE, "rb") as f:
            data = f.read()
        stmt, txns, meta = parse_statement_bytes(os.path.basename(HDFC_SAMPLE), data, "CASE_P0_PDF")
        self.assertEqual(len(txns), 8)
        expected = [("0", "300.00", "1300.00"), ("200.00", "0", "1100.00"), ("100.00", "0", "1000.00"),
                    ("0", "250000.00", "251000.00"), ("180000.00", "0", "71000.00"), ("450.00", "0", "70550.00"),
                    ("1850.00", "0", "68700.00"), ("15000.00", "0", "53700.00")]
        for txn, (dr, cr, bal) in zip(txns, expected):
            self.assertEqual((txn.debit, txn.credit, txn.running_balance), (Decimal(dr), Decimal(cr), Decimal(bal)))
            self.assertNotIn(AnomalyFlag.BALANCE_MISMATCH, txn.anomaly_flags)
        self.assertEqual(_balance_mismatches(txns), 0)
        self.assertEqual(stmt.opening_balance, Decimal("1000.00"))
        self.assertEqual(stmt.closing_balance, Decimal("53700.00"))

    def test_pdf03_sbi_sample_still_parses(self) -> None:
        with open(SBI_SAMPLE, "rb") as f:
            data = f.read()
        _, txns, _ = parse_statement_bytes(os.path.basename(SBI_SAMPLE), data, "CASE_P0_PDF")
        self.assertEqual(len(txns), 24)
        self.assertEqual(_balance_mismatches(txns), 0)
        for t in txns:
            self.assertTrue((t.debit == 0) != (t.credit == 0), t)


# ---------------------------------------------------------------------------
# PDF-04 / PDF-05 / PDF-10 / PDF-25: Poppler preference, scan detection and per-page OCR
# ---------------------------------------------------------------------------

def _image_object(w: int, h: int, gray: bytes) -> bytes:
    z = zlib.compress(gray, 6)
    return (f"<< /Type /XObject /Subtype /Image /Width {w} /Height {h} /ColorSpace /DeviceGray "
            f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(z)} >>\nstream\n").encode() + z + b"\nendstream"


class ScanDetectionAndOcrRoutingTests(unittest.TestCase):
    def _native_rows(self) -> bytes:
        xs = [32.0, 96.0, 305.0, 385.0, 445.0, 515.0]
        rows = [["01/04/2026", "NEFT:HDFC Account Name:John Doe", "UTR1", "", "500.00", "1500.00"],
                ["02/04/2026", "IMPS:SBIN Ref:ABC TO:XYZ", "UTR2", "200.00", "", "1300.00"]]
        return _statement_page(rows, ["Txn Date", "Narration", "Chq/Ref No", "Debit", "Credit", "Balance"], xs)

    def test_pdf05_colon_tokens_with_logo_and_background_scan_keep_native_text(self) -> None:
        bg = _image_object(1200, 1700, bytes([250]) * (1200 * 1700))
        logo = _image_object(60, 30, bytes([0]) * 1800)
        content = b"q 595 0 0 842 0 0 cm /Bg Do Q q 60 0 0 30 500 800 cm /Logo Do Q\n" + self._native_rows()
        pdf = build_pdf([{"content": content,
                          "resources": "/Font << /F1 3 0 R /F2 4 0 R >> /XObject << /Bg 30 0 R /Logo 31 0 R >>"}],
                        extra_objects={30: bg, 31: logo})
        self.assertFalse(_is_corrupted_ocr_text(["NEFT:HDFC Account Name:John Doe IMPS:SBIN Ref:ABC"], []))
        with mock.patch.object(pdf_parser, "ocr_pdf_bytes") as ocr_mock:
            res = extract_pdf_data(pdf)
        ocr_mock.assert_not_called()
        self.assertFalse(res.needs_ocr)
        self.assertEqual(res.ocr_pages, [])
        self.assertTrue(any("NEFT:HDFC" in c for r in res.rows for c in r))

    def test_pdf25_blank_page_without_images_is_not_ocrd(self) -> None:
        pdf = build_pdf([{"content": self._native_rows()}, {"content": b""}])
        with mock.patch.object(pdf_parser, "ocr_pdf_bytes") as ocr_mock:
            res = extract_pdf_data(pdf)
        ocr_mock.assert_not_called()
        self.assertFalse(res.needs_ocr)
        self.assertEqual(len(res.page_sizes), 2)

    def test_pdf10_mixed_document_ocrs_only_textless_pages_in_order(self) -> None:
        img = _image_object(100, 100, bytes([255]) * 10000)
        pdf = build_pdf(
            [{"content": self._native_rows()},
             {"content": b"q 595 0 0 842 0 0 cm /Im1 Do Q", "resources": "/XObject << /Im1 30 0 R >>"},
             {"content": _tj(40, 700, "Page three native closing summary text line").encode()}],
            extra_objects={30: img},
        )
        fake = {
            "tokens": [ocr_engine.OcrWordToken(page=2, x=32.0, y=742.0, text="03/04/2026", x_end=70.0),
                       ocr_engine.OcrWordToken(page=2, x=96.0, y=742.0, text="SCANNED ROW", x_end=150.0),
                       ocr_engine.OcrWordToken(page=2, x=515.0, y=742.0, text="1100.00", x_end=545.0)],
            "pages_text": ["03/04/2026 SCANNED ROW 1100.00"],
            "pages_text_by_page": {2: "03/04/2026 SCANNED ROW 1100.00"},
            "ocr_pages": [2],
            "engine": "tesseract",
        }
        with mock.patch.object(pdf_parser, "ocr_pdf_bytes", return_value=fake) as ocr_mock:
            res = extract_pdf_data(pdf, password=None)
        self.assertEqual(ocr_mock.call_args.kwargs.get("pages"), [2])
        self.assertTrue(res.needs_ocr)
        self.assertEqual(res.ocr_pages, [2])
        self.assertEqual(res.ocr_engine, "tesseract")
        self.assertEqual(len(res.pages_text), 3)
        self.assertIn("SCANNED ROW", res.pages_text[1])
        pages_seq = [t.page for t in res.tokens]
        self.assertEqual(pages_seq, sorted(pages_seq))
        flat = [" ".join(r) for r in res.rows]
        i_native = next(i for i, r in enumerate(flat) if "IMPS:SBIN" in r)
        i_scan = next(i for i, r in enumerate(flat) if "SCANNED ROW" in r)
        i_last = next(i for i, r in enumerate(flat) if "Page three" in r)
        self.assertLess(i_native, i_scan)
        self.assertLess(i_scan, i_last)

    @unittest.skipUnless(HAS_POPPLER, "Poppler not installed")
    def test_pdf04_poppler_preferred_even_when_pure_python_has_tokens(self) -> None:
        pdf = build_pdf([{"content": self._native_rows()}])
        with mock.patch.object(pdf_parser, "_run_pdftotext", wraps=pdf_parser._run_pdftotext) as pop:
            res = extract_pdf_data(pdf)
        pop.assert_called()
        self.assertEqual(res.text_source, "poppler")

    def test_lexer_splits_unseparated_numbers(self) -> None:
        ops = list(pdf_parser._lex_content(b"BT [(D)-12-4000(a)] TJ ET"))
        self.assertEqual(ops[1], ("TJ", [[("lit", b"D"), -12.0, -4000.0, ("lit", b"a")]]))

    def test_pdf04_kerned_tj_document_parses_without_poppler(self) -> None:
        def kern(word: str) -> str:
            return "".join(f"({c})-12" for c in word)

        ops = ["BT /F1 8 Tf 1 0 0 1 32 760 Tm [" + kern("Date") + "-4000" + kern("Narration") + "-9000" + kern("Debit")
               + "-3000" + kern("Credit") + "-3000" + kern("Balance") + "] TJ ET"]
        ops.append("BT /F1 8 Tf 1 0 0 1 32 742 Tm [" + kern("01/04/2026") + "-1500" + kern("SALARY") + "-16000"
                   + "-3600" + kern("9000.00") + "-3000" + kern("19000.00") + "] TJ ET")
        ops.append("BT /F1 8 Tf 1 0 0 1 32 726 Tm [" + kern("02/04/2026") + "-1500" + kern("RENT") + "-16900"
                   + kern("5000.00") + "-7400" + kern("14000.00") + "] TJ ET")
        pdf = build_pdf([{"content": "\n".join(ops).encode()}])
        with mock.patch.object(pdf_parser, "_run_pdftotext", return_value=pdf_parser._PopplerResult("missing")):
            res = extract_pdf_data(pdf)
        joined = [" | ".join(r) for r in res.rows]
        self.assertTrue(any("01/04/2026" in r and "SALARY" in r and "9000.00" in r for r in joined), joined)
        self.assertGreaterEqual(len(res.rows), 3)


# ---------------------------------------------------------------------------
# PDF-12 / PDF-14 / PDF-16: passwords, private temp files, rendering
# ---------------------------------------------------------------------------

class PasswordAndRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        rows = [["01/04/2026", "UPI/CR/1/RAZORPAY", "UTR1", "", "500.00", "1500.00"],
                ["02/04/2026", "NEFT-ZOMATO", "UTR2", "200.00", "", "1300.00"]]
        cls.plain = generate_pdf_statement(["HDFC BANK", "Account No: 501009988776"],
                                           ["Txn Date", "Narration", "Chq/Ref No", "Debit", "Credit", "Balance"], rows)
        cls.encrypted = encrypt_pdf_rc4(cls.plain, "s3cret")

    @unittest.skipUnless(HAS_POPPLER, "Poppler not installed")
    def test_pdf12_encrypted_without_password(self) -> None:
        res = extract_pdf_data(self.encrypted)
        self.assertTrue(res.is_encrypted)
        self.assertEqual(res.error, "password_required")
        self.assertFalse(res.needs_ocr)

    @unittest.skipUnless(HAS_POPPLER, "Poppler not installed")
    def test_pdf12_wrong_password_is_not_reported_as_scan(self) -> None:
        with mock.patch.object(pdf_parser, "ocr_pdf_bytes") as ocr_mock:
            res = extract_pdf_data(self.encrypted, password="nope")
        ocr_mock.assert_not_called()
        self.assertTrue(res.is_encrypted)
        self.assertEqual(res.error, "wrong_password")
        self.assertFalse(res.needs_ocr)

    @unittest.skipUnless(HAS_POPPLER, "Poppler not installed")
    def test_pdf12_correct_password_parses(self) -> None:
        res = extract_pdf_data(self.encrypted, password="s3cret")
        self.assertFalse(res.is_encrypted)
        self.assertIsNone(res.error)
        flat = " ".join(" ".join(r) for r in res.rows)
        self.assertIn("RAZORPAY", flat)
        self.assertIn("1300.00", flat)

    def test_pdf12_corrupt_file_is_not_reported_as_encrypted(self) -> None:
        for junk in (b"not a pdf at all", b"%PDF-1.4\n/Encrypt garbage \x00\x01\x02 trailer"):
            res = extract_pdf_data(junk)
            self.assertFalse(res.is_encrypted, junk)
            self.assertEqual(res.error, "corrupt", junk)
            self.assertEqual(res.rows, [])

    @unittest.skipUnless(HAS_PDFTOPPM, "pdftoppm not installed")
    def test_pdf12_password_reaches_ocr_renderer(self) -> None:
        scan = generate_scanned_pdf_statement(["SCAN"], ["Date", "Narration", "Debit", "Credit", "Balance"],
                                              [["01/04/2026", "X", "", "1.00", "2.00"]], dpi=72)
        enc = encrypt_pdf_rc4(scan, "pw1")
        self.assertIsNone(render_pdf_page_png(enc, 1, dpi=36, password="bad"))
        png = render_pdf_page_png(enc, 1, dpi=36, password="pw1")
        self.assertIsNotNone(png)
        with mock.patch.object(pdf_parser, "ocr_pdf_bytes",
                               return_value={"tokens": [], "pages_text": [], "ocr_pages": [], "engine": "none"}) as ocr_mock:
            res = extract_pdf_data(enc, password="pw1")
        if res.decrypted_in_process:
            self.assertIsNone(ocr_mock.call_args.kwargs.get("password"))  # decrypted in memory (PDF-14)
        else:
            self.assertEqual(ocr_mock.call_args.kwargs.get("password"), "pw1")
        self.assertEqual(ocr_mock.call_args.kwargs.get("pages"), [1])
        # needs_ocr reports pages whose text came from OCR (PDF-25); the mocked engine produced none
        self.assertFalse(res.needs_ocr)
        self.assertFalse(res.is_encrypted)

    def test_pdf14_private_tempdir_is_0700_and_removed(self) -> None:
        with private_tempdir() as td:
            mode = stat.S_IMODE(os.stat(td).st_mode)
            self.assertEqual(mode, 0o700)
            path = ocr_engine.write_private_file(td, "x.pdf", b"secret plaintext")
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertFalse(os.path.exists(td))

    def test_pdf16_png_dimensions_from_ihdr(self) -> None:
        png = ocr_engine._gray_bytes_to_png(bytes(12 * 7), 12, 7)
        self.assertEqual(_png_dimensions(png), (12, 7))
        self.assertIsNone(_png_dimensions(b"nope"))

    @unittest.skipUnless(HAS_PDFTOPPM, "pdftoppm not installed")
    def test_pdf16_render_is_300dpi_grayscale(self) -> None:
        self.assertEqual(ocr_engine.OCR_RENDER_DPI, 300)
        png = render_pdf_page_png(self.plain, 1)
        assert png is not None
        w, h = _png_dimensions(png)  # type: ignore[misc]
        self.assertAlmostEqual(w, 595 * 300 / 72, delta=3)
        self.assertAlmostEqual(h, 842 * 300 / 72, delta=3)
        self.assertEqual(png[25], 0)  # IHDR colour type 0 = grayscale


class GroupRowsCompatTests(unittest.TestCase):
    def test_group_rows_accepts_per_page_tolerance(self) -> None:
        toks = [PdfTextToken(1, 30, 700, "a", 40), PdfTextToken(1, 60, 696, "b", 70),
                PdfTextToken(2, 30, 700, "c", 40), PdfTextToken(2, 60, 696, "d", 70)]
        rows = _group_tokens_into_rows(toks, y_tolerance=3.5, page_sizes=[(595, 842)] * 2,
                                       page_y_tolerance={2: 4.5})
        self.assertEqual(rows, [["a"], ["b"], ["c", "d"]])


if __name__ == "__main__":
    unittest.main()
