"""P1 tests for the PDF/OCR domain: PDF-06/10/11/12/13/14/17/19/21/22/23."""

from __future__ import annotations

import datetime as dt
import math
import os
import subprocess
import sys
import unittest
import zlib
from typing import Dict, List, Optional, Sequence
from unittest import mock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (SRC_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cyphersol.ingestion import ocr_engine, pdf_parser  # noqa: E402
from cyphersol.ingestion.ocr_engine import (  # noqa: E402
    FIXTURE_PRODUCER_MARKER,
    GLYPH_MIN_CONFIDENCE,
    OcrWordToken,
    _find_binary,
    decode_predictor,
    generate_scanned_pdf_statement,
    glyph_ocr_allowed,
    page_confidence,
)
from cyphersol.ingestion.pdf_parser import (  # noqa: E402
    DrawTemplateError,
    PageRenderError,
    PdfTextToken,
    _assign_line_to_columns,
    check_pdf_password,
    extract_pdf_data,
    generate_password_candidates,
    render_page_png,
    resolve_pdf_password,
    validate_draw_template,
)
from test_p0_pdf import _serialize, _statement_page, build_pdf, encrypt_pdf_rc4  # noqa: E402

try:
    import pikepdf  # type: ignore
except ImportError:  # pragma: no cover - optional dependency
    pikepdf = None

HAS_PDFTOPPM = bool(_find_binary("pdftoppm", ("/opt/homebrew/bin/pdftoppm", "/usr/local/bin/pdftoppm")))
HAS_POPPLER = bool(_find_binary("pdftotext", ("/opt/homebrew/bin/pdftotext", "/usr/local/bin/pdftotext")))
SAMPLES = os.path.join(ROOT_DIR, "sample_statements")

HEADER = ["Date", "Narration", "Debit", "Credit", "Balance"]
XS = [40.0, 110.0, 330.0, 410.0, 490.0]
ROWS = [
    ["01/04/2026", "NEFT CR ACME", "", "1000.00", "11000.00"],
    ["02/04/2026", "UPI ZOMATO", "250.00", "", "10750.00"],
    ["03/04/2026", "IMPS RAVI", "750.00", "", "10000.00"],
]


def _no_poppler():
    return mock.patch.object(pdf_parser, "_run_pdftotext", return_value=pdf_parser._PopplerResult("missing"))


# ---------------------------------------------------------------------------
# PNG predictor encoder used to build fixtures
# ---------------------------------------------------------------------------

def _png_predict(data: bytes, columns: int, colors: int = 1, bpc: int = 8, ftypes: Sequence[int] = (2,)) -> bytes:
    row_len = (colors * bpc * columns + 7) // 8
    bpp = max(1, colors * bpc // 8)
    out = bytearray()
    prev = bytearray(row_len)
    for r_i, r0 in enumerate(range(0, len(data), row_len)):
        row = bytearray(data[r0:r0 + row_len]).ljust(row_len, b"\x00")
        ft = ftypes[r_i % len(ftypes)]
        enc = bytearray(row_len)
        for i in range(row_len):
            a = row[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if ft == 0:
                p = 0
            elif ft == 1:
                p = a
            elif ft == 2:
                p = b
            elif ft == 3:
                p = (a + b) >> 1
            else:
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                p = a if pa <= pb and pa <= pc else (b if pb <= pc else c)
            enc[i] = (row[i] - p) & 0xFF
        out.append(ft)
        out += enc
        prev = row
    return bytes(out)


def _objstm_pdf(content: bytes) -> bytes:
    """PDF 1.5 with catalog/pages/page/font inside an object stream and a PNG-predicted XRef stream."""
    page = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 3 0 R >> >> /Contents 5 0 R >>"
    inner = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        4: page,
    }
    header_parts, body = [], bytearray()
    for onum, ob in inner.items():
        header_parts.append(f"{onum} {len(body)}")
        body += ob + b"\n"
    header = (" ".join(header_parts) + "\n").encode()
    objstm_data = header + bytes(body)
    cols = 16
    padded = objstm_data + b" " * ((-len(objstm_data)) % cols)
    objstm_z = zlib.compress(_png_predict(padded, cols, ftypes=(0, 1, 2, 3, 4)))
    content_z = zlib.compress(content)
    out = bytearray(b"%PDF-1.5\n%\xe2\xe3\xcf\xd3\n")
    off5 = len(out)
    out += f"5 0 obj\n<< /Length {len(content_z)} /Filter /FlateDecode >>\nstream\n".encode() + content_z + b"\nendstream\nendobj\n"
    off6 = len(out)
    out += (f"6 0 obj\n<< /Type /ObjStm /N {len(inner)} /First {len(header)} /Length {len(objstm_z)} /Filter /FlateDecode "
            f"/DecodeParms << /Predictor 12 /Columns {cols} >> >>\nstream\n").encode() + objstm_z + b"\nendstream\nendobj\n"
    off7 = len(out)
    rows = [bytes([0, 0, 0, 0xFF])]  # obj 0 free
    for idx in range(4):  # objs 1..4 in objstm 6
        rows.append(bytes([2, 0, 6, idx]))
    rows.append(bytes([1]) + off5.to_bytes(2, "big") + b"\x00")
    rows.append(bytes([1]) + off6.to_bytes(2, "big") + b"\x00")
    rows.append(bytes([1]) + off7.to_bytes(2, "big") + b"\x00")
    xref_raw = b"".join(rows)
    xref_z = zlib.compress(_png_predict(xref_raw, 4, ftypes=(2,)))
    out += (f"7 0 obj\n<< /Type /XRef /Size 8 /W [1 2 1] /Root 1 0 R /Length {len(xref_z)} /Filter /FlateDecode "
            f"/DecodeParms << /Predictor 12 /Columns 4 >> >>\nstream\n").encode() + xref_z + b"\nendstream\nendobj\n"
    out += f"startxref\n{off7}\n%%EOF\n".encode()
    return bytes(out)


class PredictorAndObjectStreamTests(unittest.TestCase):
    """PDF-06: object/xref streams and PNG predictors."""

    def test_png_predictor_roundtrip_all_filter_types(self):
        raw = bytes((i * 37 + (i // 7) * 11) & 0xFF for i in range(9 * 12))
        for colors in (1, 3):
            cols = 12 // colors
            enc = _png_predict(raw, cols, colors=colors, ftypes=(0, 1, 2, 3, 4))
            self.assertEqual(decode_predictor(enc, 12, colors=colors, columns=cols), raw)
        self.assertEqual(decode_predictor(b"abc", 1), b"abc")
        self.assertIsNone(decode_predictor(b"\x09abc", 12, columns=3))  # bad filter type

    def test_tiff_predictor(self):
        raw = bytes([10, 20, 30, 40, 5, 5, 5, 5])
        enc = bytearray(raw)
        for r0 in (0, 4):
            for i in range(r0 + 3, r0, -1):
                enc[i] = (raw[i] - raw[i - 1]) & 0xFF
        self.assertEqual(decode_predictor(bytes(enc), 2, columns=4), raw)

    def test_flate_content_stream_with_predictor_is_decoded(self):
        content = _statement_page(ROWS, HEADER, XS)
        z = zlib.compress(_png_predict(content + b" " * ((-len(content)) % 20), 20, ftypes=(1, 2, 4)))
        obj = f"<< /Length {len(z)} /Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns 20 >> >>\nstream\n".encode() + z + b"\nendstream"
        _d, data = pdf_parser._extract_stream_from_obj(obj)
        self.assertTrue(data.startswith(content))
        arr = (f"<< /Length {len(z)} /Filter [/FlateDecode] /DecodeParms [<< /Predictor 12 /Columns 20 >>] >>\nstream\n").encode() + z + b"\nendstream"
        self.assertTrue(pdf_parser._extract_stream_from_obj(arr)[1].startswith(content))

    def test_objstm_and_xref_stream_pdf_parses_without_poppler(self):
        pdf = _objstm_pdf(_statement_page(ROWS, HEADER, XS))
        objs = pdf_parser._parse_pdf_objects(pdf)
        xref = pdf_parser._parse_xref_streams(objs)
        self.assertEqual(xref[4], (2, 6, 3))
        self.assertEqual(xref[5][0], 1)
        with _no_poppler():
            res = extract_pdf_data(pdf)
        self.assertEqual(res.text_source, "pure_python")
        self.assertFalse(res.needs_ocr)
        flat = [" | ".join(r) for r in res.rows]
        self.assertTrue(any("UPI ZOMATO" in r and "250.00" in r for r in flat), flat)
        self.assertFalse(any("compressed object" in w for w in res.warnings))

    def test_missing_compressed_objects_are_reported(self):
        pdf = _objstm_pdf(_statement_page(ROWS, HEADER, XS))
        broken = pdf.replace(b"/Type /ObjStm", b"/Type /ObjXXX")
        with _no_poppler():
            res = extract_pdf_data(broken)
        self.assertTrue(any("compressed object" in w for w in res.warnings), res.warnings)


class EmbeddedImageTests(unittest.TestCase):
    """PDF-19: predictors, CMYK/Indexed images, logos skipped, ObjStm-aware page mapping."""

    def _pdf_with_image(self, img_dict: str, data: bytes) -> bytes:
        img = f"<< /Type /XObject /Subtype /Image {img_dict} /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"
        return build_pdf([{"content": b"q 595 0 0 842 0 0 cm /Im1 Do Q", "resources": "/XObject << /Im1 20 0 R >>"}],
                         extra_objects={20: img})

    def test_predicted_cmyk_indexed_and_small_images(self):
        w, h = 320, 310
        gray = bytes((x * 255 // w) for _y in range(h) for x in range(w))
        z = zlib.compress(_png_predict(gray, w, ftypes=(1, 2, 4)))
        pdf = self._pdf_with_image(f"/Width {w} /Height {h} /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode "
                                   f"/DecodeParms << /Predictor 15 /Columns {w} >>", z)
        imgs = ocr_engine._extract_embedded_pdf_images(pdf)
        self.assertEqual(len(imgs), 1)
        self.assertEqual(bytes(imgs[0][3]), gray)

        cmyk = bytes(v for _ in range(w * h) for v in (0, 0, 0, 255))  # pure black ink
        pdf = self._pdf_with_image(f"/Width {w} /Height {h} /ColorSpace /DeviceCMYK /BitsPerComponent 8 /Filter /FlateDecode",
                                   zlib.compress(cmyk))
        imgs = ocr_engine._extract_embedded_pdf_images(pdf)
        self.assertEqual(set(imgs[0][3]), {0})

        idx = bytes((i % 2) for i in range(w * h))
        pdf = self._pdf_with_image(f"/Width {w} /Height {h} /ColorSpace [/Indexed /DeviceRGB 1 <FFFFFF000000>] "
                                   "/BitsPerComponent 8 /Filter /FlateDecode", zlib.compress(idx))
        imgs = ocr_engine._extract_embedded_pdf_images(pdf)
        self.assertEqual(list(imgs[0][3][:4]), [255, 0, 255, 0])

        logo = self._pdf_with_image("/Width 120 /Height 40 /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /FlateDecode",
                                    zlib.compress(bytes(120 * 40)))
        self.assertEqual(ocr_engine._extract_embedded_pdf_images(logo), [])

    def test_dct_images_are_left_to_the_renderer(self):
        pdf = self._pdf_with_image("/Width 400 /Height 400 /ColorSpace /DeviceGray /BitsPerComponent 8 /Filter /DCTDecode",
                                   b"\xff\xd8\xff\xe0garbage")
        self.assertEqual(ocr_engine._extract_embedded_pdf_images(pdf), [])


class GlyphMatcherRestrictionTests(unittest.TestCase):
    """PDF-11: 5x7 glyph matcher only for fixtures, with a confidence gate."""

    @classmethod
    def setUpClass(cls):
        cls.scan = generate_scanned_pdf_statement(
            ["STATE BANK OF INDIA", "Account No: 300011223344"], ["Txn Date", "Narration", "Debit", "Credit", "Balance"],
            [["05/04/2026", "UPI-ZOMATO-412345678901", "1250.50", "", "98749.50"]], dpi=150)

    def _no_external(self):
        return mock.patch.multiple(ocr_engine, _tesseract_bin=mock.Mock(return_value=None),
                                   _vision_available=mock.Mock(return_value=False))

    def test_fixture_carries_marker_and_is_read_with_high_confidence(self):
        self.assertIn(FIXTURE_PRODUCER_MARKER, self.scan)
        self.assertNotIn(b"BT", self.scan)
        with self._no_external(), mock.patch.dict(os.environ, {ocr_engine.GLYPH_OCR_ENV: ""}):
            res = ocr_engine.ocr_pdf_bytes(self.scan, pages=[1])
        self.assertEqual(res["engine"], "pure_python_glyph_ocr")
        self.assertGreaterEqual(res["confidence_by_page"][1], GLYPH_MIN_CONFIDENCE)
        self.assertIn("1250.50", res["pages_text_by_page"][1])

    def test_real_scan_without_engine_is_reported_unavailable_not_garbage(self):
        real = self.scan.replace(FIXTURE_PRODUCER_MARKER, b"Acme Scanner 9000 v1.2xx")
        self.assertFalse(glyph_ocr_allowed(real))
        with self._no_external(), mock.patch.dict(os.environ, {ocr_engine.GLYPH_OCR_ENV: ""}):
            res = ocr_engine.ocr_pdf_bytes(real, pages=[1])
            self.assertEqual(res["engine"], "unavailable")
            self.assertEqual(res["tokens"], [])
            full = extract_pdf_data(real)
        self.assertEqual(full.rows, [])
        self.assertTrue(any("No OCR engine" in w for w in full.warnings), full.warnings)

    def test_env_override_allows_glyph_matcher(self):
        real = self.scan.replace(FIXTURE_PRODUCER_MARKER, b"Acme Scanner 9000 v1.2xx")
        with mock.patch.dict(os.environ, {ocr_engine.GLYPH_OCR_ENV: "1"}):
            self.assertTrue(glyph_ocr_allowed(real))

    @unittest.skipUnless(HAS_PDFTOPPM, "pdftoppm not installed")
    def test_glyph_matcher_rejects_non_fixture_fonts(self):
        native = build_pdf([{"content": _statement_page(ROWS, HEADER, XS)}])
        rendered = ocr_engine.render_pdf_page_gray(native, 1, dpi=200)
        assert rendered is not None
        toks = ocr_engine._ocr_gray_image_pure_python(*rendered, 1, 595.0, 842.0)
        conf = page_confidence(toks)
        self.assertIsNotNone(conf)
        self.assertLess(conf, GLYPH_MIN_CONFIDENCE)
        verdict, _ = ocr_engine._gate_page(toks, "pure_python_glyph_ocr")
        self.assertEqual(verdict, "reject")

    def test_image_ocr_without_marker_does_not_use_glyph_matcher(self):
        gray = bytearray([252] * (400 * 60))
        ocr_engine._draw_text_to_gray_buffer(gray, 400, 60, 10, 10, "01/04/2026 1250.50", scale=2)
        png = ocr_engine._gray_bytes_to_png(bytes(gray), 400, 60)
        with self._no_external(), mock.patch.dict(os.environ, {ocr_engine.GLYPH_OCR_ENV: ""}):
            res = ocr_engine.ocr_image_bytes(png)
            self.assertEqual(res["engine"], "unavailable")
            res2 = ocr_engine.ocr_image_bytes(png, allow_glyph_ocr=True)
        self.assertEqual(res2["engine"], "pure_python_glyph_ocr")
        self.assertIn("1250.50", res2["pages_text"][0])


class TesseractConfidenceGateTests(unittest.TestCase):
    """PDF-11: Tesseract path with per-word confidences and a page gate."""

    TSV_HEAD = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"

    def _tsv(self, words: Sequence[tuple]) -> str:
        lines = [self.TSV_HEAD]
        for left, top, text, conf in words:
            lines.append(f"5\t1\t1\t1\t1\t1\t{left}\t{top}\t{len(text) * 20}\t30\t{conf}\t{text}\n")
        lines.append("4\t1\t1\t1\t1\t0\t0\t0\t10\t10\t-1\t\n")
        return "".join(lines)

    def test_tsv_parsing_keeps_word_confidence(self):
        tsv = self._tsv([(100, 100, "01/04/2026", 96), (600, 100, "NEFT", 90), (2000, 100, "1250.50", 88)])
        toks = ocr_engine._parse_tesseract_tsv(tsv, 1, 595.0, 842.0, 2480, 3508)
        self.assertEqual([t.text for t in toks], ["01/04/2026", "NEFT", "1250.50"])
        self.assertAlmostEqual(toks[0].conf, 0.96)
        self.assertAlmostEqual(page_confidence(toks), (0.96 * 10 + 0.90 * 4 + 0.88 * 7) / 21, places=3)

    def _run(self, tsv: str):
        fake_proc = subprocess.CompletedProcess(args=[], returncode=0, stdout=tsv, stderr="")
        with mock.patch.object(ocr_engine, "_tesseract_bin", return_value="/usr/bin/tesseract"), \
                mock.patch.object(ocr_engine, "_vision_available", return_value=False), \
                mock.patch.object(ocr_engine, "render_pdf_page_gray", return_value=(100, 140, bytearray(14000))), \
                mock.patch.object(ocr_engine.subprocess, "run", return_value=fake_proc), \
                mock.patch.dict(os.environ, {ocr_engine.GLYPH_OCR_ENV: ""}):
            return ocr_engine.ocr_pdf_bytes(b"%PDF-1.4 scan", page_sizes=[(595.0, 842.0)], pages=[1])

    def test_low_and_rejected_pages(self):
        good = self._run(self._tsv([(10, 10, "01/04/2026", 95), (60, 10, "1250.50", 93)]))
        self.assertEqual(good["ocr_pages"], [1])
        self.assertEqual(good["low_confidence_pages"], [])
        low = self._run(self._tsv([(10, 10, "01/04/2026", 60), (60, 10, "1250.50", 55)]))
        self.assertEqual(low["ocr_pages"], [1])
        self.assertEqual(low["low_confidence_pages"], [1])
        bad = self._run(self._tsv([(10, 10, "0l/O4/2O26", 20), (60, 10, "l25O.5O", 15)]))
        self.assertEqual(bad["ocr_pages"], [])
        self.assertIn(1, bad["rejected_pages"])
        self.assertEqual(bad["tokens"], [])

    def test_extract_pdf_data_surfaces_confidence(self):
        img = zlib.compress(bytes([250]) * (900 * 1200))
        img_obj = (f"<< /Type /XObject /Subtype /Image /Width 900 /Height 1200 /ColorSpace /DeviceGray "
                   f"/BitsPerComponent 8 /Filter /FlateDecode /Length {len(img)} >>\nstream\n").encode() + img + b"\nendstream"
        pdf = build_pdf([{"content": _statement_page(ROWS, HEADER, XS)},
                         {"content": b"q 595 0 0 842 0 0 cm /Im1 Do Q", "resources": "/XObject << /Im1 30 0 R >>"}],
                        extra_objects={30: img_obj})
        ocr_ret = {"tokens": [OcrWordToken(2, 40.0, 700.0, "04/04/2026", 80.0, 0.6),
                              OcrWordToken(2, 490.0, 700.0, "9000.00", 530.0, 0.6)],
                   "pages_text_by_page": {2: "04/04/2026 9000.00"}, "ocr_pages": [2], "engine": "tesseract",
                   "confidence_by_page": {2: 0.6}, "low_confidence_pages": [2], "rejected_pages": {}}
        with mock.patch.object(pdf_parser, "ocr_pdf_bytes", return_value=ocr_ret) as ocr_mock:
            res = extract_pdf_data(pdf)
        self.assertEqual(ocr_mock.call_args.kwargs["pages"], [2])  # PDF-10: only the textless page
        self.assertEqual(res.ocr_pages, [2])
        self.assertEqual(res.ocr_confidence_by_page, {2: 0.6})
        self.assertEqual(res.low_confidence_pages, [2])
        self.assertTrue(any("Low OCR confidence" in w for w in res.warnings))


class PasswordCandidateTests(unittest.TestCase):
    """PDF-13: pure password-candidate generator and in-memory resolution."""

    META = {"name": "Mr. Poojan Vig", "dob": "1985-03-07", "pan": "abcde1234f", "account_number": "5010-0293-8471",
            "customer_id": "77665544", "mobile": "+91 98765 43210"}

    def test_generator_is_pure_deterministic_and_unique(self):
        a = generate_password_candidates(dict(self.META))
        b = generate_password_candidates(dict(self.META))
        self.assertEqual(a, b)
        pws = [pw for _, pw in a]
        self.assertEqual(len(pws), len(set(pws)))
        self.assertLessEqual(len(a), pdf_parser.MAX_PASSWORD_CANDIDATES)
        d = dict(a)
        self.assertEqual(d["dob_ddmmyyyy"], "07031985")
        self.assertEqual(d["dob_ddmmyy"], "070385")
        self.assertEqual(d["dob_yyyymmdd"], "19850307")
        self.assertEqual(d["pan_upper"], "ABCDE1234F")
        self.assertEqual(d["name4_lower+dob_ddmm"], "pooj0703")
        self.assertEqual(d["name4_upper+dob_ddmm"], "POOJ0703")
        self.assertEqual(d["mobile_last5+dob_ddmmyy"], "43210070385")
        self.assertEqual(d["customer_id"], "77665544")
        self.assertEqual(d["account_number"], "501002938471")
        self.assertEqual(d["dob_ddmonyyyy_upper"], "07MAR1985")

    def test_bank_preference_and_supplied_first(self):
        meta = dict(self.META, bank="SBI", passwords=["explicit-1"])
        out = generate_password_candidates(meta)
        self.assertEqual(out[0], ("supplied_1", "explicit-1"))
        self.assertEqual(out[1][0], "mobile_last5+dob_ddmmyy")
        icici = generate_password_candidates(dict(self.META, bank="ICICI Bank"))
        self.assertEqual(icici[0][0], "name4_lower+dob_ddmm")

    def test_date_inputs_and_sparse_meta(self):
        for dob in (dt.date(1985, 3, 7), dt.datetime(1985, 3, 7, 10, 0), "07/03/1985", "07031985", "7 Mar 1985"):
            self.assertIn(("dob_ddmmyyyy", "07031985"), generate_password_candidates({"DOB": dob}), dob)
        self.assertEqual(generate_password_candidates({}), [])
        self.assertEqual(generate_password_candidates({"dob": "not a date", "pan": "SHORT"}), [])

    def test_stdlib_password_check_r2(self):
        plain = build_pdf([{"content": _statement_page(ROWS, HEADER, XS)}])
        enc = encrypt_pdf_rc4(plain, "07031985", owner_pw="own3r")
        self.assertTrue(check_pdf_password(enc, "07031985"))
        self.assertTrue(check_pdf_password(enc, "own3r"))
        self.assertFalse(check_pdf_password(enc, "wrong"))
        self.assertIsNone(check_pdf_password(plain, "x"))

    @unittest.skipUnless(pikepdf is not None, "pikepdf not installed")
    def test_stdlib_password_check_r3_r4_r6(self):
        import io
        plain = build_pdf([{"content": _statement_page(ROWS, HEADER, XS)}])
        for r, aes, expect_none in ((3, False, False), (4, False, False), (4, True, False), (6, True, True)):
            buf = io.BytesIO()
            with pikepdf.open(io.BytesIO(plain)) as pdf:
                pdf.save(buf, encryption=pikepdf.Encryption(user="pooj0703", owner="o-pw", R=r, aes=aes,
                                                                metadata=aes))
            enc = buf.getvalue()
            if expect_none:
                self.assertIsNone(check_pdf_password(enc, "pooj0703"))
                continue
            self.assertTrue(check_pdf_password(enc, "pooj0703"), (r, aes))
            self.assertTrue(check_pdf_password(enc, "o-pw"), (r, aes))
            self.assertFalse(check_pdf_password(enc, "pooj0704"), (r, aes))

    def test_resolve_reports_label_and_extract_uses_candidates(self):
        plain = build_pdf([{"content": _statement_page(ROWS, HEADER, XS)}])
        enc = encrypt_pdf_rc4(plain, "pooj0703")
        cands = generate_password_candidates(self.META)
        found = resolve_pdf_password(enc, cands)
        self.assertEqual(found["status"], "ok")
        self.assertEqual(found["label"], "name4_lower+dob_ddmm")
        with mock.patch.object(pdf_parser, "_pikepdf_module", return_value=None):
            found2 = resolve_pdf_password(enc, cands)
        self.assertEqual(found2["label"], "name4_lower+dob_ddmm")  # stdlib check path
        self.assertEqual(resolve_pdf_password(enc, [("x", "nope")])["status"], "no_match")
        self.assertEqual(resolve_pdf_password(plain, cands)["status"], "not_encrypted")

        res = extract_pdf_data(enc, password_candidates=cands)
        self.assertIsNone(res.error)
        self.assertEqual(res.password_label, "name4_lower+dob_ddmm")
        self.assertNotIn("pooj0703", repr(res))
        self.assertGreater(len(res.rows), 0)
        miss = extract_pdf_data(enc, password_candidates=[("dob_ddmmyyyy", "01011990")])
        self.assertEqual(miss.error, "wrong_password")
        self.assertTrue(miss.is_encrypted)
        # Per-file password: the explicitly supplied one wins and is labelled as such
        self.assertEqual(extract_pdf_data(enc, password="pooj0703").password_label, "supplied")


class PasswordProcessHygieneTests(unittest.TestCase):
    """PDF-12/PDF-14: decrypt once in-process; no password in any subprocess argv."""

    def _recording_run(self, seen: List[List[str]]):
        real_run = subprocess.run

        def run(cmd, *a, **kw):
            seen.append([str(c) for c in cmd])
            return real_run(cmd, *a, **kw)
        return run

    @unittest.skipUnless(pikepdf is not None and HAS_POPPLER, "pikepdf/poppler not installed")
    def test_password_never_reaches_argv_when_pikepdf_available(self):
        plain = build_pdf([{"content": _statement_page(ROWS, HEADER, XS)}, {"content": b""}])
        enc = encrypt_pdf_rc4(plain, "S3cretPw!")
        seen: List[List[str]] = []
        with mock.patch.object(subprocess, "run", side_effect=self._recording_run(seen)):
            res = extract_pdf_data(enc, password="S3cretPw!")
            render_page_png(enc, 1, dpi=40, password="S3cretPw!")
        self.assertTrue(res.decrypted_in_process)
        self.assertIsNone(res.error)
        self.assertTrue(seen)
        self.assertFalse(any("S3cretPw!" in arg for cmd in seen for arg in cmd), seen)
        self.assertFalse(any("-upw" in cmd for cmd in seen))

    @unittest.skipUnless(HAS_POPPLER, "poppler not installed")
    def test_argv_fallback_is_flagged(self):
        plain = build_pdf([{"content": _statement_page(ROWS, HEADER, XS)}])
        enc = encrypt_pdf_rc4(plain, "pw-x")
        with mock.patch.object(pdf_parser, "_decrypt_in_process", return_value=(None, "unavailable")):
            res = extract_pdf_data(enc, password="pw-x")
        self.assertFalse(res.decrypted_in_process)
        self.assertTrue(any("command line" in w for w in res.warnings), res.warnings)

    def test_corrupt_is_not_wrong_password(self):
        res = extract_pdf_data(b"%PDF-1.4\n/Encrypt garbage", password="x")
        self.assertEqual(res.error, "corrupt")


class RenderPagePngTests(unittest.TestCase):
    """PDF-21: page raster for the Draw Tool."""

    def setUp(self):
        self.plain = build_pdf([{"content": _statement_page(ROWS, HEADER, XS)}, {"content": b""}])

    @unittest.skipUnless(HAS_PDFTOPPM, "pdftoppm not installed")
    def test_render_returns_png_and_geometry(self):
        out = render_page_png(self.plain, 2, dpi=72)
        self.assertTrue(out["png"].startswith(b"\x89PNG"))
        self.assertEqual(out["page"], 2)
        self.assertEqual(out["page_count"], 2)
        self.assertEqual((out["page_width_pt"], out["page_height_pt"]), (595.0, 842.0))
        self.assertAlmostEqual(out["width_px"], 595, delta=2)
        self.assertEqual(out["content_type"], "image/png")

    @unittest.skipUnless(HAS_PDFTOPPM, "pdftoppm not installed")
    def test_errors(self):
        with self.assertRaises(PageRenderError) as cm:
            render_page_png(self.plain, 3)
        self.assertEqual(cm.exception.code, "page_out_of_range")
        with self.assertRaises(PageRenderError) as cm:
            render_page_png(b"not a pdf", 1)
        self.assertEqual(cm.exception.code, "corrupt")
        enc = encrypt_pdf_rc4(self.plain, "pw9")
        with self.assertRaises(PageRenderError) as cm:
            render_page_png(enc, 1, password="bad")
        self.assertEqual(cm.exception.code, "wrong_password")
        with self.assertRaises(PageRenderError) as cm:
            render_page_png(enc, 1)
        self.assertEqual(cm.exception.code, "password_required")
        self.assertTrue(render_page_png(enc, 1, dpi=40, password="pw9")["png"].startswith(b"\x89PNG"))

    def test_renderer_unavailable(self):
        with mock.patch.object(pdf_parser, "_poppler_bin", return_value=None), \
                mock.patch.object(pdf_parser, "_find_binary", return_value=None):
            with self.assertRaises(PageRenderError) as cm:
                render_page_png(self.plain, 1)
        self.assertEqual(cm.exception.code, "renderer_unavailable")
        self.assertIn("pdftoppm", cm.exception.message)


class DrawTemplateTests(unittest.TestCase):
    """PDF-22/PDF-23: template validation and right-edge placement of amounts."""

    def test_validation(self):
        self.assertEqual(validate_draw_template([300, 100.5, 200]), ([100.5, 200.0, 300.0], None))
        self.assertEqual(validate_draw_template([0.5, 0.2], [0.1, 0.1, 0.9, 0.9]), ([0.2, 0.5], [0.1, 0.1, 0.9, 0.9]))
        self.assertEqual(validate_draw_template(None, None), (None, None))
        for bad in ([math.nan], [float("inf")], ["abc"], [True], [100, 100], [-5], [0.5, 300], list(range(1, 40)), "1,2"):
            with self.assertRaises(DrawTemplateError, msg=repr(bad)):
                validate_draw_template(bad)
        for bad in ([0, 0, 1], [0.9, 0.1, 0.2, 0.5], [math.nan, 0, 1, 1], [0, 0, 1, 1, 1]):
            with self.assertRaises(DrawTemplateError, msg=repr(bad)):
                validate_draw_template([100.0], bad)
        self.assertTrue(issubclass(DrawTemplateError, ValueError))

    def test_extract_reports_invalid_template_instead_of_crashing(self):
        pdf = build_pdf([{"content": _statement_page(ROWS, HEADER, XS)}])
        res = extract_pdf_data(pdf, column_splits=[math.nan, 100.0])
        self.assertEqual(res.error, "invalid_template")
        self.assertTrue(res.warnings)
        res = extract_pdf_data(pdf, column_splits=[100.0], bbox=[1, 1, 0, 0])
        self.assertEqual(res.error, "invalid_template")

    def test_large_right_aligned_amount_stays_in_its_column(self):
        splits = [100.0, 300.0, 400.0, 480.0]
        line = [
            PdfTextToken(1, 40.0, 700.0, "01/04/2026", 85.0),
            PdfTextToken(1, 110.0, 700.0, "RTGS BIG PAYMENT", 200.0),
            # 12,34,56,789.00 right-aligned at 470 starts left of the Debit/Credit divider (400)
            PdfTextToken(1, 395.0, 700.0, "12,34,56,789.00", 470.0),
            PdfTextToken(1, 500.0, 700.0, "99,99,99,999.00", 560.0),
        ]
        row = _assign_line_to_columns(line, splits, numeric_anchor="right")
        self.assertEqual(row, ["01/04/2026", "RTGS BIG PAYMENT", "", "12,34,56,789.00", "99,99,99,999.00"])
        legacy = _assign_line_to_columns(line, splits, use_left_edge_only=True)
        self.assertEqual(legacy[2], "12,34,56,789.00")  # the left-edge bug this fixes

    def test_draw_template_path_uses_right_edge(self):
        toks = [PdfTextToken(1, 40.0, 700.0, "01/04/2026", 85.0), PdfTextToken(1, 110.0, 700.0, "RTGS", 140.0),
                PdfTextToken(1, 395.0, 700.0, "12,34,56,789.00", 470.0), PdfTextToken(1, 500.0, 700.0, "1.00", 560.0)]
        rows = pdf_parser._group_tokens_into_rows(toks, column_splits=[100.0, 300.0, 400.0, 480.0],
                                                  page_sizes=[(595.0, 842.0)])
        self.assertEqual(rows[-1][3], "12,34,56,789.00")


class SampleStatementRegressionTests(unittest.TestCase):
    """Every bundled PDF still parses after the ingestion changes."""

    def test_sample_pdfs_parse(self):
        for name in sorted(os.listdir(SAMPLES)):
            if not name.lower().endswith(".pdf"):
                continue
            with open(os.path.join(SAMPLES, name), "rb") as fh:
                res = extract_pdf_data(fh.read())
            self.assertIsNone(res.error, name)
            self.assertGreater(len(res.rows), 5, name)


if __name__ == "__main__":
    unittest.main()
