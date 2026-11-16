"""Regression tests for the P0 repair round: findings a reviewer reproduced after the parallel P0 fixes."""

from __future__ import annotations

import os
import resource
import sqlite3
import struct
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import zlib
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
TESTS_DIR = os.path.join(ROOT_DIR, "tests")
for _p in (SRC_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cyphersol.edge import tally_exporter  # noqa: E402
from cyphersol.edge.itr_compliance import generate_itr_compliance_report  # noqa: E402
from cyphersol.edge.loan_eligibility import compute_loan_eligibility_report  # noqa: E402
from cyphersol.edge.tally_exporter import generate_tally_xml, parse_tally_response, push_to_tally_http  # noqa: E402
from cyphersol.ingestion import pdf_parser  # noqa: E402
from cyphersol.ingestion.anomaly_gate import run_anomaly_gate  # noqa: E402
from cyphersol.ingestion.ocr_engine import ocr_pdf_bytes  # noqa: E402
from cyphersol.ingestion.parser import decode_statement_text, parse_statement_bytes  # noqa: E402
from cyphersol.ingestion.xls_biff import read_xls_biff  # noqa: E402
from cyphersol.llm_agents import Stage2ReconciliationAgent  # noqa: E402
from cyphersol.models import AnomalyFlag, NormalizedTransaction, TxDirection  # noqa: E402
from cyphersol.nlp.categorizer import HybridTransactionCategorizer, apply_suspense_resolution  # noqa: E402
from cyphersol.nlp.entity_resolver import is_system_party_name  # noqa: E402
from cyphersol.nlp.narration_parser import enrich_transaction_narration, parse_narration  # noqa: E402
from cyphersol.storage import CypherVault  # noqa: E402
from cyphersol.trace.graph_engine import build_network_graph  # noqa: E402
from cyphersol.trace.layering import build_layering_tree  # noqa: E402
from cyphersol.trace.loop_detector import detect_circular_loops  # noqa: E402

# Helpers only (never TestCase classes, so discovery does not run their tests twice)
from test_p0_edge import mk_stmt, mk_tx  # noqa: E402
from test_p0_integrate import LEGACY_DDL, _clean_env, _Transport  # noqa: E402
from test_p0_pdf import HAS_POPPLER, _image_object, _statement_page, build_pdf, encrypt_pdf_rc4  # noqa: E402
from test_p0_security import _FakeClient, _stmt, _tx  # noqa: E402
from test_p0_trace import Fixture, _find  # noqa: E402

SCRATCH = "/private/tmp/claude-501/cyphersol-p0"
os.makedirs(SCRATCH, exist_ok=True)


def _parse(name: str, data: bytes):
    stmt, txns, meta = parse_statement_bytes(name, data, "C1")
    stmt, txns, anom = run_anomaly_gate(stmt, txns)
    return stmt, txns, meta, anom


def _native_rows() -> bytes:
    xs = [32.0, 96.0, 305.0, 385.0, 445.0, 515.0]
    rows = [["01/04/2026", "NEFT:HDFC Account Name:John Doe", "UTR1", "", "500.00", "1500.00"],
            ["02/04/2026", "IMPS:SBIN Ref:ABC TO:XYZ", "UTR2", "200.00", "", "1300.00"]]
    return _statement_page(rows, ["Txn Date", "Narration", "Chq/Ref No", "Debit", "Credit", "Balance"], xs)


def _flate_stream(data: bytes, extra: bytes = b"") -> bytes:
    comp = zlib.compress(data, 9)
    return b"<< " + extra + b" /Filter /FlateDecode /Length " + str(len(comp)).encode() + b" >>\nstream\n" + comp + b"\nendstream"


# ---------------------------------------------------------------------------------------------------------
# Minimal BIFF8 (.xls) writer for the stdlib reader test
# ---------------------------------------------------------------------------------------------------------

def _rec(rid: int, body: bytes) -> bytes:
    return struct.pack("<HH", rid, len(body)) + body


def build_xls(sheet_name, rows, date_cells=frozenset(), split_sst=False) -> bytes:
    """BIFF8 workbook in an OLE2 container. Strings go to the SST (optionally split across a CONTINUE
    record in UTF-16), ints to RK records, floats to NUMBER records; ``date_cells`` use an XF with format 14."""
    strings, idx = [], {}
    for row in rows:
        for v in row:
            if isinstance(v, str) and v and v not in idx:
                idx[v] = len(strings)
                strings.append(v)
    parts, cont = [struct.pack("<II", len(strings), len(strings))], None
    for k, s in enumerate(strings):
        if split_sst and k == 0 and len(s) >= 4:
            h = len(s) // 2
            parts.append(struct.pack("<HB", len(s), 0) + s[:h].encode("latin-1"))
            cont = bytes([1]) + s[h:].encode("utf-16-le")
            cont += b"".join(struct.pack("<HB", len(x), 0) + x.encode("latin-1") for x in strings[1:])
            break
        parts.append(struct.pack("<HB", len(s), 0) + s.encode("latin-1"))
    sst = _rec(0x00FC, b"".join(parts)) + (_rec(0x003C, cont) if cont is not None else b"")

    def bof(kind):
        return _rec(0x0809, struct.pack("<HHHHII", 0x0600, kind, 0, 0, 0, 0))

    xfs = _rec(0x00E0, struct.pack("<HH", 0, 0) + b"\x00" * 16) + _rec(0x00E0, struct.pack("<HH", 0, 14) + b"\x00" * 16)
    sheet = bof(0x10)
    for r, row in enumerate(rows):
        for c, v in enumerate(row):
            if v is None or v == "":
                continue
            if isinstance(v, str):
                sheet += _rec(0x00FD, struct.pack("<HHHI", r, c, 0, idx[v]))
            elif isinstance(v, int) and (r, c) not in date_cells:
                sheet += _rec(0x027E, struct.pack("<HHHI", r, c, 0, ((v << 2) | 2) & 0xFFFFFFFF))
            else:
                sheet += _rec(0x0203, struct.pack("<HHHd", r, c, 1 if (r, c) in date_cells else 0, float(v)))
    sheet += _rec(0x000A, b"")

    def globals_(pos):
        name = bytes([len(sheet_name), 0]) + sheet_name.encode("latin-1")
        return bof(0x05) + xfs + _rec(0x0085, struct.pack("<IBB", pos, 0, 0) + name) + sst + _rec(0x000A, b"")

    g = globals_(len(globals_(0)))
    stream = g + sheet
    stream += b"\x00" * max(0, 4096 - len(stream))
    ssz = 512
    n = (len(stream) + ssz - 1) // ssz
    fat = [0xFFFFFFFD, 0xFFFFFFFE] + [i + 1 for i in range(2, 2 + n - 1)] + [0xFFFFFFFE]
    fat += [0xFFFFFFFF] * (128 - len(fat))

    def dirent(name, etype, child, start, size):
        nm = name.encode("utf-16-le") + b"\x00\x00"
        e = nm.ljust(64, b"\x00") + struct.pack("<HBB", len(nm), etype, 1)
        e += struct.pack("<III", 0xFFFFFFFF, 0xFFFFFFFF, child) + b"\x00" * 36
        return e + struct.pack("<II", start, size) + b"\x00" * 4

    dirs = dirent("Root Entry", 5, 1, 0xFFFFFFFE, 0) + dirent("Workbook", 2, 0xFFFFFFFF, 2, len(stream))
    dirs += b"\x00" * (ssz - len(dirs))
    hdr = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 16 + struct.pack("<HHHHH", 0x3E, 3, 0xFFFE, 9, 6)
    hdr += b"\x00" * 6 + struct.pack("<IIIIIIIII", 0, 1, 1, 0, 4096, 0xFFFFFFFE, 0, 0xFFFFFFFE, 0)
    hdr += struct.pack("<109I", 0, *([0xFFFFFFFF] * 108))
    return hdr + struct.pack("<128I", *fat) + dirs + stream + b"\x00" * (n * ssz - len(stream))


# ---------------------------------------------------------------------------------------------------------
# security: LLM-07 boundary rows
# ---------------------------------------------------------------------------------------------------------


class Llm07BoundaryRepairTests(unittest.TestCase):
    def _rows(self):
        return [_tx(i, "0", "100", str(100 * (i + 1))) for i in range(5)]

    def test_last_row_fabricated_amount_rejected(self):
        txns = self._rows()
        txns[4].running_balance += 7
        txns[4].anomaly_flags = [AnomalyFlag.BALANCE_MISMATCH]
        fake = _FakeClient([{"repairs": [{"row_index": 4, "debit": 0, "credit": 250000, "running_balance": 250400}]}])
        st, out, info = Stage2ReconciliationAgent(fake).reconcile(_stmt(), txns)
        self.assertEqual(info["rows_repaired"], 0)
        self.assertEqual(info["repairs_rejected"], 1)
        self.assertEqual(out[4].credit, Decimal("100"))
        self.assertNotEqual(st.closing_balance, Decimal("250400"))

    def test_first_row_fabricated_amount_rejected(self):
        txns = self._rows()
        txns[0].running_balance += 7
        txns[1].anomaly_flags = [AnomalyFlag.BALANCE_MISMATCH]
        fake = _FakeClient([{"repairs": [{"row_index": 0, "debit": 90000, "credit": 0, "running_balance": 100}]}])
        st, out, info = Stage2ReconciliationAgent(fake).reconcile(_stmt(), txns)
        self.assertEqual(info["rows_repaired"], 0)
        self.assertEqual(out[0].debit, Decimal("0"))
        self.assertNotEqual(st.opening_balance, Decimal("90100"))

    def test_last_row_balance_only_fix_is_accepted(self):
        txns = self._rows()
        txns[4].running_balance += 7
        txns[4].anomaly_flags = [AnomalyFlag.BALANCE_MISMATCH]
        fake = _FakeClient([{"repairs": [{"row_index": 4, "running_balance": 500}]}])
        st, out, info = Stage2ReconciliationAgent(fake).reconcile(_stmt(closing_balance=Decimal("507")), txns)
        self.assertEqual(info["rows_repaired"], 1)
        self.assertEqual(out[4].running_balance, Decimal("500"))
        self.assertIn(AnomalyFlag.LLM_REPAIRED, out[4].anomaly_flags)

    def test_row0_amount_change_needs_stated_opening(self):
        # Row 0 credit OCR slip 1000 -> 100 with balance 100 matches a header opening of 0 only after the fix
        txns = self._rows()
        txns[0].credit = Decimal("1000")
        txns[1].anomaly_flags = [AnomalyFlag.BALANCE_MISMATCH]
        rep = {"repairs": [{"row_index": 0, "credit": 100}]}
        # Opening derived from row 0 (no header value): an amount change at row 0 is unconstrained -> rejected
        derived = _stmt(opening_balance=Decimal("-900"), closing_balance=Decimal("500"))
        _st, out, info = Stage2ReconciliationAgent(_FakeClient([rep])).reconcile(derived, [_tx(i, "0", "100", str(100 * (i + 1))) if i else txns[0] for i in range(5)])
        self.assertEqual(info["rows_repaired"], 0)
        # Stated header opening 0 validates it -> accepted, and the stated opening is kept
        txns2 = self._rows()
        txns2[0].credit = Decimal("1000")
        txns2[1].anomaly_flags = [AnomalyFlag.BALANCE_MISMATCH]
        stated = _stmt(opening_balance=Decimal("0"), closing_balance=Decimal("500"))
        st2, out2, info2 = Stage2ReconciliationAgent(_FakeClient([rep])).reconcile(stated, txns2)
        self.assertEqual(info2["rows_repaired"], 1)
        self.assertEqual(out2[0].credit, Decimal("100"))
        self.assertEqual(st2.opening_balance, Decimal("0"))


# ---------------------------------------------------------------------------------------------------------
# ingest: ING-16, ING-01, ING-12/13, NUL padding, ING-06
# ---------------------------------------------------------------------------------------------------------


class IngestRepairTests(unittest.TestCase):
    HEAD = b"Date,Narration,Debit,Credit,Balance\n01/04/2025,UPI A,100.00,,900.00\n02/04/2025,UPI B,,500.00,1400.00\n"
    TAIL = b"03/04/2025,UPI C,200.00,,1200.00\n"

    def test_ing16_closing_balance_column_header_is_not_a_stated_value(self):
        data = (b"Date,Narration,Debit,Credit,Closing Balance\n01/04/2025,UPI A,100.00,,900.00\n"
                b"02/04/2025,UPI B,,500.00,1400.00\n03/04/2025,UPI C,200.00,,1200.00")
        st, txns, meta, anom = _parse("a.csv", data)
        self.assertEqual(st.closing_balance, Decimal("1200.00"))
        self.assertIsNone(meta.get("stated_closing_balance"))
        self.assertEqual(anom["balance_mismatch"], 0)
        self.assertEqual(anom["closing_balance_mismatch"], 0)
        self.assertFalse(any("stated closing" in w for w in meta["warnings"]))

    def test_ing16_footer_closing_balance_still_read(self):
        data = self.HEAD + self.TAIL + b",Closing Balance,,,1200.00\n"
        st, _txns, meta, _anom = _parse("f.csv", data)
        self.assertEqual(meta.get("stated_closing_balance"), "1200.00")
        self.assertEqual(st.closing_balance, Decimal("1200.00"))

    def test_ing01_undated_summary_lines_never_become_transactions(self):
        for mid in (b",Page Total,100.00,500.00,\n", b",Transaction Total,100.00,500.00,\n",
                    b"PAGE TOTAL,,100.00,500.00,\n", b",Balance B/F,,,1400.00\n", b",Balance C/F,,,1400.00\n"):
            with self.subTest(line=mid):
                st, txns, meta, anom = _parse("b.csv", self.HEAD + mid + self.TAIL)
                self.assertEqual([t.raw_narration for t in txns], ["UPI A", "UPI B", "UPI C"])
                self.assertEqual(sum(t.debit for t in txns), Decimal("300.00"))
                self.assertEqual(anom["balance_mismatch"], 0)
                self.assertEqual(st.closing_balance, Decimal("1200.00"))
                self.assertTrue(any("total/summary" in w for w in meta["warnings"]))

    def test_ing01_undated_balance_only_line_is_not_a_zero_amount_row(self):
        _st, txns, _meta, _anom = _parse("c.csv", self.HEAD + b",carry note,,,1400.00\n" + self.TAIL)
        self.assertEqual(len(txns), 3)
        self.assertFalse(any(t.debit == 0 and t.credit == 0 for t in txns))

    def test_ing12_signed_debit_credit_column(self):
        data = (b"Date,Particulars,Debit/Credit,Balance\n01/04/2025,UPI A,-100.00,900.00\n"
                b"02/04/2025,SALARY,500.00,1400.00\n03/04/2025,ATM,-200.00,1200.00")
        _st, txns, _meta, anom = _parse("d.csv", data)
        self.assertEqual([t.direction for t in txns], [TxDirection.DEBIT, TxDirection.CREDIT, TxDirection.DEBIT])
        self.assertEqual([t.debit + t.credit for t in txns], [Decimal("100.00"), Decimal("500.00"), Decimal("200.00")])
        self.assertEqual(anom["balance_mismatch"], 0)

    def test_ing13_amount_dr_cr_column(self):
        data = (b"Date,Particulars,Amount (Dr/Cr),Balance\n01/04/2025,UPI A,100.00 Dr,900.00\n"
                b"02/04/2025,SALARY,500.00 Cr,1400.00\n03/04/2025,ATM,200.00 Dr,1200.00")
        _st, txns, _meta, _anom = _parse("e.csv", data)
        self.assertEqual([(t.debit, t.credit) for t in txns],
                         [(Decimal("100.00"), Decimal("0.00")), (Decimal("0.00"), Decimal("500.00")),
                          (Decimal("200.00"), Decimal("0.00"))])

    def test_ing13_hint_in_debit_or_credit_column_is_honoured(self):
        data = (b"Date,Narration,Debit,Credit,Balance\n01/04/2025,UPI A,,100.00 Dr,900.00\n"
                b"02/04/2025,UPI B,500.00 Cr,,1400.00\n")
        _st, txns, _meta, anom = _parse("h.csv", data)
        self.assertEqual([t.direction for t in txns], [TxDirection.DEBIT, TxDirection.CREDIT])
        self.assertEqual(anom["balance_mismatch"], 0)

    def test_trailing_nul_padding_is_tolerated(self):
        data = (b"Date,Narration,Debit,Credit,Balance\r\n01/04/2025,UPI A,100.00,,900.00\r\n"
                b"02/04/2025,UPI B,,500.00,1400.00\r\n" + b"\x00" * 16)
        _st, txns, _meta, _anom = _parse("x.csv", data)
        self.assertEqual(len(txns), 2)
        warnings = []
        text, _enc = decode_statement_text(b"Date,Narr\x00ation\n" + b"a" * 400, warnings=warnings)
        self.assertNotIn("\x00", text)
        self.assertTrue(warnings)
        with self.assertRaises(ValueError):
            decode_statement_text(b"abcdefgh\x00" * 50)

    def test_ing06_biff8_xls_without_xlrd(self):
        rows = [["Statement of Account XYZ HOLDER"], ["Date", "Narration", "Debit", "Credit", "Balance"],
                [45748, "UPI/DR/123/ALPHA", 100.5, None, 900], [45749, "NEFT CR SALARY", None, 500, 1400.0]]
        data = build_xls("Sheet1", rows, date_cells={(2, 0), (3, 0)}, split_sst=True)
        sheets = read_xls_biff(data)
        self.assertEqual(sheets["Sheet1"][0][0], "Statement of Account XYZ HOLDER")
        self.assertEqual(sheets["Sheet1"][2][:3], ["2025-04-01", "UPI/DR/123/ALPHA", 100.5])
        with mock.patch.dict(sys.modules, {"xlrd": None}):
            _st, txns, _meta, anom = _parse("stmt.xls", data)
        self.assertEqual([(t.txn_date, t.debit, t.credit) for t in txns],
                         [("2025-04-01", Decimal("100.50"), Decimal("0.00")), ("2025-04-02", Decimal("0.00"), Decimal("500.00"))])
        self.assertEqual(anom["balance_mismatch"], 0)

    def test_ing06_ui_upload_accepts_xls(self):
        # P3: the accepted upload types live in one shared constant used by the ingest view.
        with open(os.path.join(SRC_DIR, "cyphersol", "ui", "js", "core", "constants.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertRegex(js, r'UPLOAD_ACCEPT = "[^"]*\.xls[,"]')


# ---------------------------------------------------------------------------------------------------------
# pdf: PDF-01/07 budgets, lexer depth, /W expansion, PDF-25 blank pages, owner-only password
# ---------------------------------------------------------------------------------------------------------


class PdfRepairTests(unittest.TestCase):
    def _shared_form_pdf(self, n_pages: int, form_bytes: int) -> bytes:
        form = _flate_stream(b"1 " * (form_bytes // 2), b"/Type /XObject /Subtype /Form /BBox [0 0 10 10]")
        return build_pdf([{"content": b"/F1 Do", "resources": "/XObject << /F1 30 0 R >>"} for _ in range(n_pages)],
                         extra_objects={30: form})

    def test_pdf01_document_budget_charges_shared_form_on_every_page(self):
        pdf = self._shared_form_pdf(10, 1 << 20)
        seen = []
        orig_take = pdf_parser._WorkBudget.take

        def take(self, n):
            got = orig_take(self, n)
            seen.append(got)
            return got

        with mock.patch.object(pdf_parser, "MAX_DOC_CONTENT_BYTES", 3 << 20), \
                mock.patch.object(pdf_parser._WorkBudget, "take", take):
            py = pdf_parser._extract_pure_python(pdf, work=pdf_parser._WorkBudget(max_bytes=3 << 20))
        self.assertLessEqual(sum(seen), 3 << 20)
        self.assertTrue(any("for the document" in w for w in py.warnings))
        self.assertEqual(len(py.page_sizes), 10)

    def test_pdf01_deadline_stops_interpretation(self):
        pdf = self._shared_form_pdf(4, 1 << 20)
        work = pdf_parser._WorkBudget(deadline=time.monotonic() - 1)
        t0 = time.monotonic()
        py = pdf_parser._extract_pure_python(pdf, work=work)
        self.assertLess(time.monotonic() - t0, 2.0)
        self.assertTrue(work.timed_out)
        self.assertTrue(any("time limit" in w for w in py.warnings))

    def test_pdf01_lexer_nesting_is_bounded(self):
        tracemalloc.start()
        try:
            ops = list(pdf_parser._lex_content(b"[" * (1 << 20) + b" Tj"))
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLess(peak, 32 << 20)
        self.assertEqual([op for op, _ in ops], ["Tj"])
        # Normal nesting still works, and brackets beyond the cap are balanced correctly
        ops = list(pdf_parser._lex_content(b"[(a) [(b)] -100 (c)] TJ"))
        self.assertEqual(ops[0][0], "TJ")
        self.assertEqual(len(ops[0][1][0]), 4)
        deep = b"[" * 100 + b"]" * 100 + b" [(x)] TJ"
        op, operands = list(pdf_parser._lex_content(deep))[-1]
        self.assertEqual((op, operands[-1]), ("TJ", [("lit", b"x")]))
        depth, node = 0, operands[0]
        while isinstance(node, list) and node:
            depth, node = depth + 1, node[0]
        self.assertLessEqual(depth + 1, pdf_parser.MAX_LEX_DEPTH)

    def test_pdf01_cid_widths_total_expansion_is_bounded(self):
        w = b"[" + b"0 65535 500 " * 2000 + b"]"
        t0 = time.monotonic()
        widths = pdf_parser._parse_cid_widths(w)
        self.assertLess(time.monotonic() - t0, 1.0)
        self.assertLessEqual(len(widths), pdf_parser.MAX_CID_WIDTH_ENTRIES)
        self.assertEqual(pdf_parser._parse_cid_widths(b"[1 [10 20] 5 7 300]"), {1: 10.0, 2: 20.0, 5: 300.0, 6: 300.0, 7: 300.0})

    def test_pdf25_blank_page_of_native_statement_with_logo_is_not_ocrd(self):
        logo = _image_object(60, 30, bytes([0]) * 1800)
        c1 = b"q 60 0 0 30 500 800 cm /Logo Do Q\n" + _native_rows()
        pdf = build_pdf([{"content": c1, "resources": "/Font << /F1 3 0 R /F2 4 0 R >> /XObject << /Logo 31 0 R >>"},
                         {"content": b""}], extra_objects={31: logo})
        with mock.patch.object(pdf_parser, "ocr_pdf_bytes") as ocr_mock:
            res = pdf_parser.extract_pdf_data(pdf)
        ocr_mock.assert_not_called()
        self.assertFalse(res.needs_ocr)
        self.assertEqual(res.ocr_pages, [])
        stmt, _txns, _meta = parse_statement_bytes("stmt.pdf", pdf, "C1")
        self.assertFalse(stmt.is_ocr_derived)

    def test_pdf25_textless_page_with_its_own_image_is_ocrd(self):
        scan = _image_object(60, 30, bytes([0]) * 1800)
        pdf = build_pdf([{"content": _native_rows()},
                         {"content": b"q 595 0 0 842 0 0 cm /Scan Do Q", "resources": "/XObject << /Scan 31 0 R >>"}],
                        extra_objects={31: scan})
        with mock.patch.object(pdf_parser, "ocr_pdf_bytes",
                               return_value={"tokens": [], "pages_text_by_page": {}, "engine": "none"}) as ocr_mock:
            res = pdf_parser.extract_pdf_data(pdf)
        self.assertEqual(ocr_mock.call_args.kwargs["pages"], [2])
        self.assertIsNotNone(ocr_mock.call_args.kwargs.get("deadline"))
        self.assertFalse(res.needs_ocr)  # OCR produced nothing for it

    def test_pdf01_ocr_page_cap(self):
        pdf = build_pdf([{"content": b""} for _ in range(5)])  # no text anywhere -> every page is a candidate
        with mock.patch.object(pdf_parser, "MAX_OCR_PAGES", 2), mock.patch.object(
                pdf_parser, "_pdfplumber_rows", return_value=None), mock.patch.object(
                pdf_parser, "ocr_pdf_bytes", return_value={"tokens": []}) as ocr_mock:
            res = pdf_parser.extract_pdf_data(pdf)
        self.assertEqual(ocr_mock.call_args.kwargs["pages"], [1, 2])
        self.assertTrue(any("only the first 2" in w for w in res.warnings))

    def test_pdf01_ocr_deadline_skips_remaining_pages(self):
        with mock.patch("cyphersol.ingestion.ocr_engine.render_pdf_page_gray") as render:
            res = ocr_pdf_bytes(b"%PDF-1.4", page_sizes=[(595.0, 842.0)] * 3, pages=[1, 2, 3],
                                deadline=time.monotonic() - 1)
        render.assert_not_called()
        self.assertEqual(res["skipped_pages"], [1, 2, 3])
        self.assertEqual(res["ocr_pages"], [])

    @unittest.skipUnless(HAS_POPPLER, "pdftotext not installed")
    def test_owner_password_only_pdf_opens_with_any_supplied_password(self):
        plain = build_pdf([{"content": _native_rows()}])
        own = encrypt_pdf_rc4(plain, "")
        base = pdf_parser.extract_pdf_data(own)
        res = pdf_parser.extract_pdf_data(own, password="case-default-password")
        self.assertIsNone(res.error)
        self.assertFalse(res.is_encrypted)
        self.assertEqual(len(res.rows), len(base.rows))
        self.assertGreater(len(res.rows), 0)
        # A genuinely user-password-protected file still reports the wrong password
        locked = encrypt_pdf_rc4(plain, "s3cret")
        self.assertEqual(pdf_parser.extract_pdf_data(locked, password="nope").error, "wrong_password")

    def test_owner_password_retry_in_decrypt_path(self):
        calls = []

        def fake_decrypt(data, password):
            calls.append(password)
            return (b"%PDF-plain", "ok") if not password else (None, "wrong_password")

        with mock.patch.object(pdf_parser, "_is_pdf_encrypted", return_value=True), \
                mock.patch.object(pdf_parser, "_decrypt_in_process", side_effect=fake_decrypt), \
                mock.patch.object(pdf_parser, "_run_pdftotext",
                                  return_value=pdf_parser._PopplerResult("corrupt")) as pop:
            res = pdf_parser.extract_pdf_data(b"%PDF-enc", password="guess")
        self.assertEqual(calls, ["guess", None])
        self.assertNotEqual(res.error, "wrong_password")
        self.assertIsNone(pop.call_args.kwargs.get("password"))


# ---------------------------------------------------------------------------------------------------------
# nlp: NLP-01 self-transfer, NLP-02 channels, NLP-04 learned rules
# ---------------------------------------------------------------------------------------------------------


def _ntx(narr: str, d: str = "DR", holder: str = "AMIT KUMAR SINGH", tx_id: str = "T0", case_id: str = "C1"):
    t = NormalizedTransaction(
        tx_id=tx_id, statement_id="S", case_id=case_id, row_index=0, txn_date="2025-04-01", raw_narration=narr,
        payment_channel="OTHER", direction=TxDirection.DEBIT if d == "DR" else TxDirection.CREDIT,
        debit=Decimal(100) if d == "DR" else Decimal(0), credit=Decimal(0) if d == "DR" else Decimal(100),
        running_balance=Decimal(1000), category_id=52, category_name="Suspense / Uncategorized",
        categorization_source="PENDING", confidence_score=0.0, source_account_number="111122223333",
        source_holder_name=holder, is_suspense=True,
    )
    return enrich_transaction_narration(t)


class NlpRepairTests(unittest.TestCase):
    def setUp(self):
        self.cat = HybridTransactionCategorizer()

    def test_nlp01_shared_surname_tokens_are_not_self_transfer(self):
        cases = [("IMPS/P2A/412345678901/KUMAR SINGH/SBIN", "DR", "AMIT KUMAR SINGH"),
                 ("UPI/DR/412345678901/AMIT KUMAR/SBIN/ak@oksbi/UPI", "DR", "AMIT KUMAR SINGH"),
                 ("UPI/CR/412345678901/SHARMA RAJESH KUMAR/SBIN/rk@ybl/UPI", "CR", "RAJESH SHARMA")]
        for narr, d, holder in cases:
            with self.subTest(narr=narr):
                tx = self.cat.classify_transaction(_ntx(narr, d, holder))
                self.assertEqual(tx.category_id, 52)
                self.assertLess(tx.confidence_score, self.cat.confidence_threshold)
                self.assertEqual(tx.top3_suggestions[0]["category_id"], 49)

    def test_nlp01_same_name_in_any_order_or_with_initials_is_self_transfer(self):
        for narr, holder in (("UPI/CR/412345678901/SHARMA RAJESH/SBIN/rk@ybl/UPI", "RAJESH SHARMA"),
                             ("IMPS/P2A/412345678901/AMIT K SINGH/SBIN", "AMIT KUMAR SINGH")):
            with self.subTest(narr=narr):
                tx = self.cat.classify_transaction(_ntx(narr, "CR" if "/CR/" in narr else "DR", holder))
                self.assertEqual((tx.category_id, tx.categorization_source), (49, "RULE_SELF_TRANSFER"))
        tx = self.cat.classify_transaction(_ntx("IMPS/P2A/412345678901/KUMAR SINGH SELF/SBIN", "DR"))
        self.assertEqual(tx.category_id, 49)

    def test_nlp02_person_names_on_transfer_and_cheque_channels(self):
        for narr in ("INB/TPT/DHAN SINGH", "MB/TPT/ZARA KHAN/123", "FT-ZARA KHAN-123456789012",
                     "TO TRANSFER-ZARA KHAN", "CHQ PAID 123456 DHAN SINGH", "CLG/123456/TITAN SHARMA",
                     "UPI/DR/412345678901/ZARA/SBIN/zara12@ybl/UPI"):
            with self.subTest(narr=narr):
                tx = self.cat.classify_transaction(_ntx(narr, holder="X Y"))
                self.assertEqual(tx.category_id, 52, tx.categorization_source)
                self.assertLess(tx.confidence_score, self.cat.confidence_threshold)
        # single-word merchants keep their KB hit
        tx = self.cat.classify_transaction(_ntx("UPI/DR/412345678901/SWIGGY/AXIS/swiggy@axisbank/UPI", holder="X Y"))
        self.assertEqual(tx.categorization_source, "RULE_KB")

    def test_nlp04_learned_rule_respects_exclusions_and_kb_rows(self):
        txns = [_ntx("UPI/DR/412345678901/RAHUL/SBIN/UPI", tx_id="A", holder="X Y"),
                _ntx("UPI/DR/412345678902/RAHUL VERMA/SBIN/UPI", tx_id="B", holder="X Y"),
                _ntx("UPI/DR/412345678903/RAHUL SHARMA/SBIN/UPI", tx_id="C", holder="X Y"),
                _ntx("NEFT DR SWIGGY RAHUL N123456789012", tx_id="D", holder="X Y")]
        self.cat.classify_batch(txns)
        kb_before = txns[3].category_id
        cnt, learned = apply_suspense_resolution(txns, "A", 14, confirmed_tx_ids=[], categorizer=self.cat, case_id="C1")
        self.assertEqual(cnt, 1)
        self.assertIsNone(learned)  # single-token party key needs explicit confirmation
        self.cat.classify_batch([t for t in txns if t.categorization_source != "USER_RULE"])
        by_id = {t.tx_id: t for t in txns}
        self.assertEqual(by_id["B"].category_id, 52)
        self.assertEqual(by_id["C"].category_id, 52)
        self.assertEqual(by_id["D"].category_id, kb_before)

    def test_nlp04_multi_token_rule_never_hits_excluded_or_kb_rows(self):
        mk = lambda i, n: _ntx(f"IMPS/P2A/{i}/RAMESH KUMAR/SBIN0001234", tx_id=n, holder="X Y")  # noqa: E731
        txns = [mk(1, "A"), mk(2, "B"), mk(3, "C")]
        self.cat.classify_batch(txns)
        _cnt, learned = apply_suspense_resolution(txns, "A", 18, confirmed_tx_ids=["C"], categorizer=self.cat)
        self.assertEqual(learned, "party:ramesh kumar")
        self.assertEqual(self.cat.rule_exclusions_for(learned, "DR"), ["B"])
        fresh = [mk(2, "B"), mk(9, "Z")]
        self.cat.classify_batch(fresh)
        self.assertEqual(fresh[0].category_id, 52)  # excluded by the analyst
        self.assertEqual((fresh[1].category_id, fresh[1].categorization_source), (18, "USER_RULE"))
        # A KB-decided row is never overwritten by a learned party rule
        self.cat.add_user_rule("party:swiggy", "DR", 14)
        kb = self.cat.classify_transaction(_ntx("POS 432198XXXXXX1234 SWIGGY BLR", holder="X Y", tx_id="K"))
        self.assertEqual(kb.categorization_source, "RULE_KB")

    def test_nlp04_exclusions_persist_through_the_service(self):
        from cyphersol.service import CypherApplicationService

        with mock.patch.dict(os.environ, _clean_env(), clear=True), tempfile.TemporaryDirectory(dir=SCRATCH) as td:
            svc = CypherApplicationService(os.path.join(td, "v.db"), td, seed_demo=False)
            cid = svc.create_case("x", "cypher_edge", "t")["case_id"]
            csv = ("Date,Narration,Debit,Credit,Balance\n"
                   "01/04/2025,IMPS/P2A/1/RAMESH KUMAR/SBIN0001234,100.00,,900.00\n"
                   "02/04/2025,IMPS/P2A/2/RAMESH KUMAR/SBIN0001234,100.00,,800.00\n"
                   "03/04/2025,IMPS/P2A/3/RAMESH KUMAR/SBIN0001234,100.00,,700.00\n").encode()
            svc.ingest_statements(cid, [("s.csv", csv, None)])
            txns = sorted(svc.vault.get_transactions(cid), key=lambda t: t.row_index)
            res = svc.resolve_suspense(cid, txns[0].tx_id, 18, confirmed_tx_ids=[txns[2].tx_id])
            self.assertEqual(res["learned_pattern"], "party:ramesh kumar")
            excl = svc.vault.get_user_rule_exclusions()
            self.assertEqual(excl, [("party:ramesh kumar", "DR", cid, [txns[1].tx_id])])
            svc2 = CypherApplicationService(os.path.join(td, "v.db"), td, seed_demo=False)
            svc2.run_case_llm_agents(cid, execution_mode="offline", stages=[4])
            after = {t.tx_id: t for t in svc2.vault.get_transactions(cid)}
            self.assertEqual(after[txns[1].tx_id].category_id, 52)
            self.assertEqual(after[txns[2].tx_id].category_id, 18)


# ---------------------------------------------------------------------------------------------------------
# trace: NLP-10 generic system parties, layering complexity
# ---------------------------------------------------------------------------------------------------------


class TraceRepairTests(unittest.TestCase):
    def test_nlp10_parser_generic_parties_are_system_parties(self):
        for narr in ("AUTOSWEEP TRF TO FD 1234567890", "NEFT RETURN REVERSAL", "IMPS RETURN"):
            with self.subTest(narr=narr):
                self.assertTrue(is_system_party_name(parse_narration(narr).counterparty_name or ""))
        self.assertFalse(is_system_party_name("RAHUL SHARMA"))
        self.assertFalse(is_system_party_name("REV KUMAR"))

    def test_nlp10_sweep_party_is_not_a_hub(self):
        f = Fixture()
        f.statement("A")
        f.statement("B")
        f.transfer("A", "AUTO SWEEP / FD LINK", 50000, day=1)
        f.tx("A", "CR", "AUTO SWEEP / FD LINK", 50000, day=5)
        f.transfer("B", "AUTO SWEEP / FD LINK", 40000, day=2)
        f.tx("B", "CR", "AUTO SWEEP / FD LINK", 40000, day=6)
        self.assertEqual(list(detect_circular_loops(f.statements, f.transactions, f.ents())), [])
        g = build_network_graph(f.statements, f.transactions, f.ents())
        comp = {n["label"]: n["component_id"] for n in g["nodes"]}
        self.assertNotEqual(comp["A"], comp["B"])
        sweep = next(n for n in g["nodes"] if n["label"] == "AUTO SWEEP / FD LINK")
        self.assertNotEqual(sweep["node_role"], "BRIDGE_CONDUIT")

    def _fan(self, k: int) -> Fixture:
        f = Fixture()
        f.statement("ROOT")
        f.statement("B")
        for i in range(k):
            f.tx("ROOT", "DR", "B", 100, day=0, hour=i % 23)
        for i in range(k):
            f.tx("B", "DR", f"C{i % 50}", 1, day=1 + (i % 10), hour=i % 23)
        return f

    def test_tf08_layering_is_not_quadratic_in_lots(self):
        f = self._fan(8000)
        for mode in ("fifo", "all"):
            t0 = time.monotonic()
            r = build_layering_tree(f.statements, f.transactions, f.ents(), f.eid("ROOT"), mode=mode)
            self.assertLess(time.monotonic() - t0, 3.0, mode)
            b = _find(r["root"], "B")
            self.assertEqual(sum(c["amount"] for c in b["children"]), 8000.0)

    def test_all_mode_windows_still_expire_per_path(self):
        f = Fixture()
        f.statement("ROOT")
        f.statement("B")
        f.transfer("ROOT", "B", 100, day=1)
        f.transfer("ROOT", "B", 100, day=20)
        f.transfer("B", "C", 150, day=21)
        f.transfer("B", "D", 70, day=40)  # day 20 window (14 d) closed by day 40
        r = build_layering_tree(f.statements, f.transactions, f.ents(), f.eid("ROOT"), mode="all", window_days=14)
        self.assertEqual(_find(r["root"], "C")["amount"], 150.0)
        self.assertIsNone(_find(r["root"], "D"))


# ---------------------------------------------------------------------------------------------------------
# edge: EDGE-05, EDGE-24, Tally response, UI, CC/OD, 269ST self, FOIR provisional EMI
# ---------------------------------------------------------------------------------------------------------


class _Stub(BaseHTTPRequestHandler):
    status = 200
    body = b""

    def do_POST(self):  # noqa: N802
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):
        pass


class EdgeRepairTests(unittest.TestCase):
    def _serve(self, status: int, body: bytes) -> int:
        srv = HTTPServer(("127.0.0.1", 0), type("H", (_Stub,), {"status": status, "body": body}))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_edge05_own_transfer_legs_four_days_apart_post_once(self):
        sa = mk_stmt("SA", acct="111122223333", bank="SBI")
        sb = mk_stmt("SB", acct="444455556666", bank="HDFC")
        txns = [mk_tx(0, "2025-05-02", "NEFT TO OWN HDFC", dr=1000, cat=49, sid="SA", cp_acc="444455556666"),
                mk_tx(0, "2025-05-06", "NEFT FROM OWN SBI", cr=1000, cat=49, sid="SB", cp_acc="111122223333")]
        xml, stats = generate_tally_xml("c", [sa, sb], txns)
        self.assertEqual(xml.count('VCHTYPE="Contra"'), 1)
        self.assertEqual(stats.get("mirror_legs_deduped"), 1)

    def test_edge24_charge_and_gst_rows_are_one_bounce(self):
        st = mk_stmt(start="2025-04-01", end="2025-06-30", opening="100000")
        txns = [mk_tx(0, "2025-04-05", "NACH/DR/HDFC LOAN EMI", dr=10000, cat=24),
                mk_tx(1, "2025-05-05", "NACH/DR/HDFC LOAN EMI", dr=10000, cat=24),
                mk_tx(2, "2025-06-05", "NACH/DR/HDFC LOAN EMI", dr=10000, cat=24),
                mk_tx(3, "2025-06-06", "NACH RTN HDFC LOAN INSUFF FUNDS", cr=10000, cat=52),
                mk_tx(4, "2025-06-06", "NACH RTN CHGS", dr=500, cat=28),
                mk_tx(5, "2025-06-06", "GST ON NACH RTN CHGS", dr=90, cat=28),
                mk_tx(6, "2025-06-20", "REV NACH RTN CHGS", cr=500, cat=52)]
        rep = compute_loan_eligibility_report(txns, statements=[st])
        self.assertEqual(rep["inward_bounce_count"], 1, rep["bounce_events"])
        ev = rep["bounce_events"][0]
        self.assertFalse(ev["derived_from_charge_only"])
        self.assertEqual(sorted(ev["charge_narrations"]), ["GST ON NACH RTN CHGS", "NACH RTN CHGS"])

    def test_edge24_two_separate_charge_only_bounces_still_count_twice(self):
        st = mk_stmt(start="2025-05-01", end="2025-05-31", opening="1000")
        txns = [mk_tx(0, "2025-05-05", "NACH RETURN CHG/INSUFFICIENT BAL", dr=59, cat=28),
                mk_tx(1, "2025-05-09", "NACH RETURN CHG/INSUFFICIENT BAL", dr=59, cat=28)]
        rep = compute_loan_eligibility_report(txns, statements=[st])
        self.assertEqual(rep["inward_bounce_count"], 2)

    def test_tally_unknown_request_is_not_success(self):
        parsed = parse_tally_response("<RESPONSE>Unknown Request, cannot be processed</RESPONSE>")
        self.assertFalse(parsed["recognised"])
        port = self._serve(200, b"<RESPONSE>Unknown Request, cannot be processed</RESPONSE>")
        res = push_to_tally_http('<ENVELOPE><VOUCHER REMOTEID="x"></VOUCHER></ENVELOPE>', port=port)
        self.assertTrue(res["connected"])
        self.assertFalse(res["success"])
        self.assertIn("Unknown Request", res["message"])
        ok = parse_tally_response("<RESPONSE><CREATED>2</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS></RESPONSE>")
        self.assertTrue(ok["recognised"])
        self.assertEqual(ok["created"], 2)

    def test_tally_zero_created_for_sent_vouchers_is_not_success(self):
        port = self._serve(200, b"<RESPONSE><CREATED>0</CREATED><ALTERED>0</ALTERED><ERRORS>0</ERRORS></RESPONSE>")
        res = push_to_tally_http('<ENVELOPE><VOUCHER REMOTEID="x"></VOUCHER></ENVELOPE>', port=port)
        self.assertFalse(res["success"])
        self.assertIn("created no vouchers", res["message"])

    def test_ui_tally_push_branches_on_success(self):
        # P3: every view reports Tally pushes through the shared describeTallyPushResult() helper.
        with open(os.path.join(SRC_DIR, "cyphersol", "ui", "js", "core", "results.js"), encoding="utf-8") as fh:
            js = fh.read()
        block = js[js.index("export function describeTallyPushResult"):]
        block = block[: block.index("\n}\n")]
        self.assertIn("res.success", block)
        self.assertIn("res.message", block)
        self.assertIn("line_errors", block)
        self.assertNotIn("res.connected\n      ? `Tally Sync Complete", block)

    def test_itr_cc_od_account_uses_current_limit(self):
        st = mk_stmt(account_type="CC/OD")
        txns = [mk_tx(0, "2025-05-01", "CASH DEP", cr=900000, cat=50, cp="TEST HOLDER"),
                mk_tx(1, "2025-05-02", "CASH DEP", cr=900000, cat=50, cp="TEST HOLDER")]
        rep = generate_itr_compliance_report(txns, statements=[st])
        self.assertEqual(rep["cash_summary"]["cash_deposits_non_current"], 0)
        self.assertEqual(rep["cash_summary"]["cash_deposits_current"], 1800000)
        self.assertFalse(any(f["section"].startswith("SFT-003") for f in rep["statutory_red_flags"]))

    def test_itr_269st_ignores_holders_own_cash(self):
        st = mk_stmt()
        txns = [mk_tx(0, "2025-05-01", "CASH DEP SELF", cr=250000, cat=50, cp="SELF"),
                mk_tx(1, "2025-05-02", "CASH DEP", cr=250000, cat=50, cp="TEST HOLDER"),
                mk_tx(2, "2025-05-03", "CASH DEP", cr=250000, cat=50),
                mk_tx(3, "2025-05-04", "CASH DEP BY RAVI", cr=250000, cat=50, cp="RAVI KUMAR")]
        rep = generate_itr_compliance_report(txns, statements=[st])
        flags = [f for f in rep["statutory_red_flags"] if f["section"] == "Section 269ST"]
        critical = [f for f in flags if f["severity"] == "CRITICAL"]
        self.assertEqual(len(critical), 1)
        self.assertEqual([o["person"] for o in critical[0]["occurrences"]], ["RAVI KUMAR"])
        verify = [f for f in flags if f.get("verify_source")]
        self.assertEqual(len(verify), 1)
        self.assertEqual([o["date"] for o in verify[0]["occurrences"]], ["2025-05-03"])

    def test_foir_counts_emi_first_seen_in_last_month(self):
        st = mk_stmt(start="2025-04-01", end="2025-06-30", opening="100000")
        txns = [mk_tx(i, f"2025-0{4 + i}-01", "SALARY CREDIT", cr=100000, cat=1) for i in range(3)]
        txns.append(mk_tx(3, "2025-06-05", "NACH/DR/BAJAJ FINANCE LOAN", dr=30000, cat=24))
        rep = compute_loan_eligibility_report(txns, foir_pct=55.0, statements=[st])
        self.assertEqual(rep["monthly_existing_emi"], 30000)
        self.assertEqual(rep["max_eligible_emi"], 25000)
        self.assertFalse(rep["emi_obligations"][0]["confirmed"])


# ---------------------------------------------------------------------------------------------------------
# integrate: atomic user_rules migration, Stage 1 provenance
# ---------------------------------------------------------------------------------------------------------


class IntegrateRepairTests(unittest.TestCase):
    def _legacy(self, td: str) -> str:
        path = os.path.join(td, "legacy.db")
        with sqlite3.connect(path) as c:
            c.executescript(LEGACY_DDL)
            c.execute("INSERT INTO user_rules VALUES ('r1','zomato','DR',9,NULL,'2025-01-01')")
            c.execute("INSERT INTO user_rules VALUES ('r2','swiggy','DR','nine',NULL,'2025-01-02')")
        c.close()
        return path

    def test_malformed_legacy_rule_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as td:
            v = CypherVault(self._legacy(td))
            self.assertEqual(v.get_user_rules(), [("zomato", "DR", 9, None, None)])

    def test_failed_migration_rolls_back_the_rebuild(self):
        with tempfile.TemporaryDirectory(dir=SCRATCH) as td:
            path = self._legacy(td)
            with mock.patch("cyphersol.storage._rule_id", side_effect=RuntimeError("disk error")):
                with self.assertRaises(RuntimeError):
                    CypherVault(path)
            with sqlite3.connect(path) as c:
                rows = c.execute("SELECT pattern FROM user_rules ORDER BY pattern").fetchall()
                ddl = c.execute("SELECT sql FROM sqlite_master WHERE name='user_rules'").fetchone()[0]
            c.close()
            self.assertEqual([r[0] for r in rows], ["swiggy", "zomato"])
            self.assertIn("UNIQUE", ddl.upper())  # untouched: the migration re-runs on the next open
            v = CypherVault(path)
            self.assertEqual(v.get_user_rules(), [("zomato", "DR", 9, None, None)])

    def test_stage1_adoption_is_recorded_in_provenance_and_report(self):
        from cyphersol.service import CypherApplicationService

        rows = [{"date": "2025-04-01", "narration": "UPI/CR/123/RAHUL VERMA", "debit": 0, "credit": 500, "balance": 500},
                {"date": "2025-04-02", "narration": "UPI/DR/124/SUNITA RAO", "debit": 100, "credit": 0, "balance": 400}]
        with mock.patch.dict(os.environ, _clean_env(), clear=True), tempfile.TemporaryDirectory(dir=SCRATCH) as td:
            svc = CypherApplicationService(os.path.join(td, "v.db"), td, seed_demo=False,
                                           llm_transport_fn=_Transport(stage1_rows=rows, repair=False))
            cid = svc.create_case("x", "cypher_edge", "t")["case_id"]
            svc.update_llm_config({"provider": "openai", "api_key": "sk-test-1", "execution_mode": "both"})
            res = svc.ingest_statements(cid, [("scan.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 100, None)])
            rep = res["ingested"][0]
            self.assertEqual((rep["status"], rep["transactions"]), ("ok", 2))
            self.assertFalse(any("No transaction rows extracted" in w for w in rep["warnings"]))
            details = svc.vault.get_statement_provenance(cid)[0]["details"]
            self.assertTrue(details["stage1_llm_adopted"])
            self.assertEqual(details["stage1_provider"], "openai")
            self.assertTrue(details["stage1_model"])
            self.assertEqual(details["text_source"], "llm_stage1")


if __name__ == "__main__":
    unittest.main()
