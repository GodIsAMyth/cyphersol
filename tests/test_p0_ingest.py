"""P0 ingestion regression tests (ING-01..09, ING-11..14, ING-16, ING-17, PDF-15)."""

from __future__ import annotations

import glob
import os
import sys
import unittest
from decimal import Decimal
from unittest import mock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.ingestion.anomaly_gate import run_anomaly_gate  # noqa: E402
from cyphersol.ingestion.parser import (  # noqa: E402
    BulkIngestError,
    FileParseResult,
    bulk_parse_statements,
    detect_bank_and_metadata,
    infer_date_order,
    parse_indian_amount,
    parse_indian_date,
    parse_statement_bytes,
)
from cyphersol.ingestion.pdf_parser import PdfExtractionResult  # noqa: E402
from cyphersol.ingestion.xlsx_io import read_xlsx_bytes, write_xlsx_bytes  # noqa: E402
from cyphersol.models import AnomalyFlag, TxDirection  # noqa: E402

SAMPLES_DIR = os.path.join(ROOT_DIR, "sample_statements")


def _csv(*lines: str) -> bytes:
    return "\n".join(lines).encode("utf-8")


def _parse_and_gate(filename: str, data: bytes):
    stmt, txns, meta = parse_statement_bytes(filename, data, "CASE_P0")
    stmt, txns, counts = run_anomaly_gate(stmt, txns)
    return stmt, txns, meta, counts


class TestIng01UndatedRowsNeverDropped(unittest.TestCase):
    def test_invalid_date_row_with_amount_is_emitted_and_flagged(self) -> None:
        data = _csv(
            "Account Number,112233445566",
            "Txn Date,Narration,Debit,Credit,Balance",
            "01/04/2026,OPENING CREDIT,,10000.00,10000.00",
            "31/02/2026,TRANSFER TO VENDOR,500.00,,9500.00",
            "15/04/2026,ATM WDL,1000.00,,8500.00",
        )
        stmt, txns, meta, counts = _parse_and_gate("ing01.csv", data)
        self.assertEqual(len(txns), 3)
        self.assertEqual(sum(t.debit for t in txns), Decimal("1500.00"))
        bad = [t for t in txns if AnomalyFlag.DATE_UNPARSEABLE in t.anomaly_flags]
        self.assertEqual(len(bad), 1)
        self.assertEqual(bad[0].debit, Decimal("500.00"))
        self.assertIn("TRANSFER TO VENDOR", bad[0].raw_narration)
        self.assertTrue(bad[0].txn_date)  # placeholder date from neighbour, never empty
        self.assertTrue(any("31/02/2026" in w for w in meta["warnings"]))
        self.assertEqual(counts["balance_mismatch"], 0)

    def test_blank_date_row_with_amount_is_not_merged_into_previous(self) -> None:
        data = _csv(
            "Txn Date,Narration,Debit,Credit,Balance",
            "01/04/2026,SALARY,,5000.00,5000.00",
            ",UPI/DR/ZOMATO,250.00,,4750.00",
            ",WRAPPED NARRATION ONLY,,,",
            "16/04/2026,ATM,750.00,,4000.00",
        )
        _stmt, txns, meta, counts = _parse_and_gate("ing01b.csv", data)
        self.assertEqual(len(txns), 3)
        self.assertEqual(txns[1].debit, Decimal("250.00"))
        self.assertIn(AnomalyFlag.DATE_UNPARSEABLE, txns[1].anomaly_flags)
        # Pure-narration continuation still merges into the preceding row
        self.assertIn("WRAPPED NARRATION ONLY", txns[1].raw_narration)
        self.assertNotIn("ZOMATO", txns[0].raw_narration)
        self.assertEqual(counts["balance_mismatch"], 0)
        self.assertTrue(meta["warnings"])

    def test_total_rows_are_not_transactions(self) -> None:
        data = _csv(
            "Txn Date,Narration,Debit,Credit,Balance",
            "01/04/2026,SALARY,,5000.00,5000.00",
            "16/04/2026,ATM,750.00,,4250.00",
            ",TOTAL,750.00,5000.00,",
        )
        _stmt, txns, _meta, _counts = _parse_and_gate("ing01c.csv", data)
        self.assertEqual(len(txns), 2)
        self.assertNotIn("TOTAL", txns[-1].raw_narration)


class TestIng02DateOrder(unittest.TestCase):
    def test_parse_indian_date_order_kwarg(self) -> None:
        self.assertEqual(parse_indian_date("04/01/2026"), "2026-01-04")
        self.assertEqual(parse_indian_date("04/01/2026", order="MDY"), "2026-04-01")
        self.assertEqual(parse_indian_date("12/25/2025"), "2025-12-25")
        self.assertEqual(parse_indian_date("01-31-2026"), "2026-01-31")
        # Digital sources never get OCR digit repairs
        self.assertIsNone(parse_indian_date("12/25/2025", order="DMY", ocr=False))
        self.assertIsNone(parse_indian_date("31/02/2026", ocr=False))
        # Legacy OCR year repair remains available for OCR sources
        self.assertEqual(parse_indian_date("10.05.75"), "2025-05-10")
        self.assertEqual(parse_indian_date("2026-04-05 10:00:00"), "2026-04-05")
        self.assertEqual(parse_indian_date("05-Apr-2026"), "2026-04-05")

    def test_infer_order_field_gt_12_and_monotonicity(self) -> None:
        self.assertEqual(infer_date_order(["12/25/2025", "12/26/2025"])[:2], ("MDY", True))
        self.assertEqual(infer_date_order(["25/12/2025", "26/12/2025"])[:2], ("DMY", True))
        self.assertEqual(infer_date_order(["03/01/2026", "05/01/2026", "02/02/2026"])[:2], ("DMY", True))
        self.assertEqual(infer_date_order(["01/03/2026", "01/05/2026", "02/02/2026"])[:2], ("MDY", True))
        order, decided, _basis = infer_date_order(["05/04/2026"])
        self.assertEqual(order, "DMY")
        self.assertFalse(decided)

    def test_us_dated_csv_is_parsed_month_first(self) -> None:
        data = _csv(
            "Date,Description,Withdrawal,Deposit,Balance",
            "12/25/2025,GIFT SHOP,100.00,,900.00",
            "12/26/2025,REFUND,,50.00,950.00",
            "01/02/2026,GROCERY,200.00,,750.00",
        )
        _stmt, txns, meta, counts = _parse_and_gate("us.csv", data)
        self.assertEqual([t.txn_date for t in txns], ["2025-12-25", "2025-12-26", "2026-01-02"])
        self.assertEqual(meta["date_order"]["order"], "MDY")
        self.assertEqual(counts["balance_mismatch"], 0)

    def test_undecidable_order_is_flagged(self) -> None:
        data = _csv(
            "Txn Date,Narration,Debit,Credit,Balance",
            "05/04/2026,SALARY,,5000.00,5000.00",
        )
        _stmt, txns, _meta, _counts = _parse_and_gate("amb.csv", data)
        self.assertIn(AnomalyFlag.DATE_ORDER_AMBIGUOUS, txns[0].anomaly_flags)


class TestIng03FixedWidthTxt(unittest.TestCase):
    def test_fixed_width_amounts_with_indian_commas(self) -> None:
        lines = [
            "Account No: 445566778899",
            "Date        Narration                      Debit          Credit          Balance",
            "01/04/2026  NEFT CR ACME TEXTILES                       1,23,456.00     1,23,456.00",
            "02/04/2026  RTGS DR VENDOR PAYMENT       23,456.00                      1,00,000.00",
            "03/04/2026  UPI DR SHOP                     500.50                         99,499.50",
        ]
        _stmt, txns, _meta, counts = _parse_and_gate("fixed.txt", "\n".join(lines).encode("utf-8"))
        self.assertEqual(len(txns), 3)
        self.assertEqual(txns[0].credit, Decimal("123456.00"))
        self.assertEqual(txns[1].debit, Decimal("23456.00"))
        self.assertEqual(txns[2].running_balance, Decimal("99499.50"))
        self.assertEqual(counts["balance_mismatch"], 0)

    def test_pipe_txt_with_commas_in_amounts(self) -> None:
        data = _csv(
            "Date|Narration|Debit|Credit|Balance",
            "01/04/2026|SALARY, APRIL||1,50,000.00|1,50,000.00",
            "02/04/2026|RENT||| ",
            "02/04/2026|RENT PAID|25,000.00||1,25,000.00",
        )
        _stmt, txns, _meta, counts = _parse_and_gate("pipe.txt", data)
        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[0].credit, Decimal("150000.00"))
        self.assertEqual(counts["balance_mismatch"], 0)

    def test_unquoted_indian_amounts_in_csv_are_rejoined(self) -> None:
        data = _csv(
            "Date,Narration,Debit,Credit,Balance",
            "01/04/2026,SALARY,,1,50,000.00,1,50,000.00",
            "02/04/2026,RENT,25,000.00,,1,25,000.00",
        )
        _stmt, txns, _meta, counts = _parse_and_gate("unquoted.csv", data)
        self.assertEqual(txns[0].credit, Decimal("150000.00"))
        self.assertEqual(txns[1].debit, Decimal("25000.00"))
        self.assertEqual(txns[1].running_balance, Decimal("125000.00"))
        self.assertEqual(counts["balance_mismatch"], 0)


class TestIng04ColumnRealignment(unittest.TestCase):
    def test_bundled_hdfc_pdf_reconciles(self) -> None:
        path = os.path.join(SAMPLES_DIR, "CASE041_POOJAN-VIG_HDFC_501002938471.pdf")
        with open(path, "rb") as fh:
            data = fh.read()
        stmt, txns, _meta, counts = _parse_and_gate(os.path.basename(path), data)
        self.assertEqual(len(txns), 8)
        self.assertEqual(counts["balance_mismatch"], 0)
        self.assertEqual(stmt.opening_balance, Decimal("1000.00"))
        self.assertEqual(stmt.closing_balance, Decimal("53700.00"))
        for t in txns:
            self.assertFalse(t.debit > 0 and t.credit > 0)
        self.assertEqual(txns[3].credit, Decimal("250000.00"))
        self.assertEqual(txns[4].debit, Decimal("180000.00"))

    def test_collapsed_pdf_rows_are_realigned_and_direction_inferred(self) -> None:
        # Rows as a text extractor emits them when blank Debit/Credit cells are dropped (bundled HDFC fixture)
        rows = [
            ["HDFC BANK LIMITED - ACCOUNT STATEMENT"],
            ["Account Holder: POOJAN VIG | Account No: 501002938471 | IFSC: HDFC0000240"],
            ["Txn Date", "Narration", "Chq/Ref No", "Debit", "Credit", "Balance"],
            ["2026-04-01 09:10:00", "UPI/CR/40911001/AYAZ QURESHI", "UTR40911001", "300.00", "1300.00"],
            ["2026-04-01 10:15:00", "UPI/DR/40911002/MANISH PATIL", "UTR40911002", "200.00", "1100.00"],
            ["2026-04-01 11:30:00", "UPI/DR/40911003/IMRAN SABLE", "UTR40911003", "100.00", "1000.00"],
            ["2026-04-16 14:00:00", "NEFT-UTIB0001122-4093001-APEX SHELL", "UTR4093001", "250000.00", "251000.00"],
            ["2026-04-17 09:30:00", "RTGS/UTR4094002/APEX SHELL PVT LTD", "UTR4094002", "180000.00", "71000.00"],
        ]
        fake = PdfExtractionResult(pages_text=[""], page_sizes=[(595.0, 842.0)], tokens=[], rows=rows)
        with mock.patch("cyphersol.ingestion.parser.extract_pdf_data", return_value=fake):
            stmt, txns, _meta, counts = _parse_and_gate("CASE_X_HDFC_501002938471.pdf", b"%PDF-fake")
        self.assertEqual(counts["balance_mismatch"], 0)
        self.assertEqual(stmt.opening_balance, Decimal("1000.00"))
        self.assertEqual(
            [t.direction for t in txns],
            [TxDirection.CREDIT, TxDirection.DEBIT, TxDirection.DEBIT, TxDirection.CREDIT, TxDirection.DEBIT],
        )
        self.assertEqual(txns[3].credit, Decimal("250000.00"))
        self.assertEqual(txns[4].debit, Decimal("180000.00"))
        for t in txns:
            self.assertIn(AnomalyFlag.DIRECTION_INFERRED, t.anomaly_flags)
            self.assertFalse(t.debit > 0 and t.credit > 0)

    def test_dual_amount_row_warns_and_flags(self) -> None:
        data = _csv(
            "Txn Date,Narration,Debit,Credit,Balance",
            "01/04/2026,SALARY,,5000.00,5000.00",
            "16/04/2026,ODD ROW,100.00,100.00,5000.00",
        )
        _stmt, txns, meta, _counts = _parse_and_gate("dual.csv", data)
        self.assertIn(AnomalyFlag.ZERO_OR_DUAL_AMOUNT, txns[1].anomaly_flags)
        self.assertTrue(any("both" in w.lower() for w in meta["warnings"]))


class TestIng05MultiSheet(unittest.TestCase):
    def test_all_transaction_sheets_parsed(self) -> None:
        wb = write_xlsx_bytes(
            [
                ("Notes", [["This workbook was exported by the bank"], ["No transactions here"]]),
                ("April", [
                    ["Date", "Particulars", "Debit", "Credit", "Balance"],
                    ["01/04/2026", "SALARY", "", 50000, 50000],
                    ["15/04/2026", "RENT", 20000, "", 30000],
                ]),
                ("May", [
                    ["Date", "Particulars", "Debit", "Credit", "Balance"],
                    ["01/05/2026", "SALARY", "", 50000, 80000],
                    ["20/05/2026", "EMI", 10000, "", 70000],
                ]),
            ]
        )
        _stmt, txns, meta, counts = _parse_and_gate("CASE_X_SBI_1234567890.xlsx", wb)
        self.assertEqual(len(txns), 4)
        self.assertEqual(counts["balance_mismatch"], 0)
        sheets = {s["name"]: s for s in meta["sheets"]}
        self.assertEqual(sheets["April"]["rows"], 2)
        self.assertEqual(sheets["May"]["rows"], 2)
        self.assertEqual(set(meta["source_sheet_by_tx"].values()), {"April", "May"})
        self.assertTrue(any("Notes" in w for w in meta["warnings"]))


class TestIng06XlsAndEncodings(unittest.TestCase):
    def test_binary_xls_without_xlrd_gives_clear_error(self) -> None:
        try:
            import xlrd  # type: ignore  # noqa: F401
        except ImportError:
            fake_xls = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512
            with self.assertRaises(ValueError) as ctx:
                parse_statement_bytes("old.xls", fake_xls, "C")
            self.assertIn(".xlsx", str(ctx.exception))
            self.assertIn("save", str(ctx.exception).lower())
        else:  # pragma: no cover - environment dependent
            self.skipTest("xlrd installed")

    def test_html_disguised_xls_is_parsed(self) -> None:
        html = (
            "<html><body><table>"
            "<tr><td>Date</td><td>Narration</td><td>Debit</td><td>Credit</td><td>Balance</td></tr>"
            "<tr><td>01/04/2026</td><td>SALARY</td><td></td><td>1,000.00</td><td>1,000.00</td></tr>"
            "<tr><td>02/04/2026</td><td>ATM &amp; CASH</td><td>200.00</td><td></td><td>800.00</td></tr>"
            "</table></body></html>"
        ).encode("utf-8")
        _stmt, txns, _meta, counts = _parse_and_gate("export.xls", html)
        self.assertEqual(len(txns), 2)
        self.assertEqual(txns[1].raw_narration, "ATM & CASH")
        self.assertEqual(counts["balance_mismatch"], 0)

    def test_utf16_bom_csv(self) -> None:
        text = "Txn Date,Narration,Debit,Credit,Balance\r\n01/04/2026,SALARY,,1000.00,1000.00\r\n02/04/2026,ATM,200.00,,800.00\r\n"
        _stmt, txns, _meta, _counts = _parse_and_gate("u16.csv", text.encode("utf-16"))
        self.assertEqual(len(txns), 2)

    def test_cp1252_before_latin1(self) -> None:
        text = "Txn Date,Narration,Debit,Credit,Balance\n01/04/2026,CAF\xc9 – RENT,100.00,,900.00\n"
        _stmt, txns, _meta, _counts = _parse_and_gate("cp.csv", text.encode("cp1252"))
        self.assertIn("–", txns[0].raw_narration)


class TestIng07to09BankDetection(unittest.TestCase):
    def test_unknown_bank_never_fabricates_ifsc(self) -> None:
        bank, meta = detect_bank_and_metadata("Sahyadri Credit Society\nAccount No: 1234567890", "statement.csv")
        self.assertEqual(bank.bank_code, "UNKNOWN")
        self.assertIsNone(meta["ifsc_code"])

    def test_longest_word_boundary_keyword(self) -> None:
        self.assertEqual(detect_bank_and_metadata("Central Bank of India\nAccount No: 1234567890", "a.csv")[0].bank_code, "CBI")
        self.assertEqual(detect_bank_and_metadata("South Indian Bank Ltd\nAccount No: 1234567890", "a.csv")[0].bank_code, "SOUTHIND")
        self.assertNotEqual(detect_bank_and_metadata("Kasbi Traders\nAccount No: 1234567890", "a.csv")[0].bank_code, "SBI")

    def test_ifsc_prefix_wins(self) -> None:
        bank, meta = detect_bank_and_metadata("Bank of India branch statement\nIFSC: CBIN0281234\nAccount No: 1234567890", "a.csv")
        self.assertEqual(bank.bank_code, "CBI")
        self.assertEqual(meta["ifsc_code"], "CBIN0281234")

    def test_statement_ifsc_not_taken_from_narration(self) -> None:
        header = "\n".join(
            [
                "Txn Date Narration Ref No Debit Credit Balance",
                "2026-04-02 NEFT-HDFC0000240-4093001-POOJAN VIG UTR1 0 440000 442000",
            ]
        )
        bank, meta = detect_bank_and_metadata(header, "CASE041_APEX-SHELL-PVT-LTD_INDUS_201009988776.csv")
        self.assertEqual(bank.bank_code, "INDUS")
        self.assertIsNone(meta["ifsc_code"])

    def test_statement_level_missing_metadata(self) -> None:
        data = _csv(
            "Txn Date,Narration,Debit,Credit,Balance",
            "01/04/2026,SALARY,,5000.00,5000.00",
            "16/04/2026,ATM,750.00,,4250.00",
        )
        stmt, txns, _meta, counts = _parse_and_gate("noinfo.csv", data)
        self.assertEqual(stmt.bank_code, "UNKNOWN")
        self.assertIsNone(stmt.ifsc_code)
        self.assertIn("ifsc_code", counts["missing_metadata"])
        self.assertIn("account_number", counts["missing_metadata"])
        for t in txns:
            self.assertNotIn(AnomalyFlag.MISSING_METADATA, t.anomaly_flags)
            self.assertIn(AnomalyFlag.VALID, t.anomaly_flags)


class TestIng11And17RowFlags(unittest.TestCase):
    def test_zero_amount_row_flagged(self) -> None:
        data = _csv(
            "Account Number,998877665544",
            "Txn Date,Narration,Debit,Credit,Balance",
            "18/04/2026,SALARY,,5000.00,5000.00",
            "19/04/2026,SMS ALERT CHARGES REVERSED,0.00,0.00,5000.00",
        )
        _stmt, txns, _meta, counts = _parse_and_gate("zero.csv", data)
        self.assertEqual(len(txns), 2)
        self.assertIn(AnomalyFlag.VALID, txns[0].anomaly_flags)
        self.assertIn(AnomalyFlag.ZERO_OR_DUAL_AMOUNT, txns[1].anomaly_flags)
        self.assertEqual(counts["zero_or_dual_amount"], 1)

    def test_gate_preserves_parser_flags(self) -> None:
        data = _csv(
            "Txn Date,Narration,Debit,Credit,Balance",
            "18/04/2026,SALARY,,5000.00,5000.00",
            "31/04/2026,BAD DATE,100.00,,4900.00",
        )
        stmt, txns, _meta = parse_statement_bytes("keep.csv", data, "C")
        stmt, txns, _ = run_anomaly_gate(stmt, txns)
        stmt, txns, _ = run_anomaly_gate(stmt, txns)  # idempotent re-run keeps parser flags
        self.assertIn(AnomalyFlag.DATE_UNPARSEABLE, txns[1].anomaly_flags)
        self.assertNotIn(AnomalyFlag.VALID, txns[1].anomaly_flags)


class TestIng12And13Amounts(unittest.TestCase):
    def test_balance_amount_header_maps_to_balance(self) -> None:
        data = _csv(
            "Date,Description,Debit Amount,Credit Amount,Balance Amount",
            "18/04/2026,SALARY,,5000.00,5000.00",
            "19/04/2026,ATM,1000.00,,4000.00",
        )
        _stmt, txns, meta, counts = _parse_and_gate("hdr.csv", data)
        self.assertEqual(meta["col_map"]["balance"], 4)
        self.assertEqual(meta["col_map"]["debit"], 2)
        self.assertEqual(meta["col_map"]["credit"], 3)
        self.assertEqual(counts["balance_mismatch"], 0)

    def test_signed_amount_column(self) -> None:
        data = _csv(
            "Date,Description,Amount",
            "18/04/2026,SALARY,5000.00",
            "19/04/2026,ATM,-1000.00",
            "20/04/2026,SHOP,250.00-",
        )
        _stmt, txns, _meta, _counts = _parse_and_gate("signed.csv", data)
        self.assertEqual(txns[0].credit, Decimal("5000.00"))
        self.assertEqual(txns[0].direction, TxDirection.CREDIT)
        self.assertEqual(txns[1].debit, Decimal("1000.00"))
        self.assertEqual(txns[2].debit, Decimal("250.00"))

    def test_trailing_minus_digital(self) -> None:
        self.assertEqual(parse_indian_amount("250.00-", ocr=False), (Decimal("250.00"), "DR"))
        self.assertEqual(parse_indian_amount("1,250.00 Cr", ocr=False), (Decimal("1250.00"), "CR"))
        self.assertEqual(parse_indian_amount("1.234,50", ocr=False), (Decimal("1234.50"), None))
        self.assertEqual(parse_indian_amount("(1,200.00)", ocr=False), (Decimal("1200.00"), "DR"))

    def test_type_dictionary(self) -> None:
        data = _csv(
            "Date,Description,Amount,Type,Balance",
            "18/04/2026,SALARY,5000.00,DEPOSIT,5000.00",
            "19/04/2026,ATM,1000.00,WITHDRAWAL,4000.00",
            "20/04/2026,REFUND,100.00,Credit,4100.00",
            "21/04/2026,FEE,10.00,Debit,4090.00",
        )
        _stmt, txns, _meta, counts = _parse_and_gate("types.csv", data)
        self.assertEqual(
            [t.direction for t in txns],
            [TxDirection.CREDIT, TxDirection.DEBIT, TxDirection.CREDIT, TxDirection.DEBIT],
        )
        self.assertEqual(counts["balance_mismatch"], 0)
        for t in txns:
            self.assertNotIn(AnomalyFlag.DIRECTION_INFERRED, t.anomaly_flags)

    def test_unresolvable_direction_flagged(self) -> None:
        data = _csv(
            "Date,Description,Amount",
            "18/04/2026,SOMETHING,5000.00",
        )
        _stmt, txns, meta, _counts = _parse_and_gate("nodir.csv", data)
        self.assertIn(AnomalyFlag.DIRECTION_INFERRED, txns[0].anomaly_flags)
        self.assertTrue(any("direction" in w.lower() for w in meta["warnings"]))


class TestIng16StatedBalances(unittest.TestCase):
    def test_opening_balance_mismatch_flags_row0(self) -> None:
        data = _csv(
            "Account Number,998877665544",
            "Opening Balance,100000.00",
            "Txn Date,Narration,Debit,Credit,Balance",
            "18/04/2026,SALARY,,5000.00,95000.00",
            "19/04/2026,ATM,1000.00,,94000.00",
        )
        stmt, txns, meta, counts = _parse_and_gate("open.csv", data)
        self.assertEqual(stmt.opening_balance, Decimal("100000.00"))
        self.assertIn(AnomalyFlag.BALANCE_MISMATCH, txns[0].anomaly_flags)
        self.assertNotIn(AnomalyFlag.BALANCE_MISMATCH, txns[1].anomaly_flags)
        self.assertTrue(any("opening" in w.lower() for w in meta["warnings"]))

    def test_closing_balance_mismatch_flags_last_row(self) -> None:
        data = _csv(
            "Account Number,998877665544",
            "Closing Balance,90000.00",
            "Txn Date,Narration,Debit,Credit,Balance",
            "18/04/2026,SALARY,,5000.00,95000.00",
            "19/04/2026,ATM,1000.00,,94000.00",
        )
        stmt, txns, meta, counts = _parse_and_gate("close.csv", data)
        self.assertEqual(stmt.closing_balance, Decimal("90000.00"))
        self.assertIn(AnomalyFlag.BALANCE_MISMATCH, txns[-1].anomaly_flags)
        self.assertTrue(any("closing" in w.lower() for w in meta["warnings"]))


class TestIng14BulkResilience(unittest.TestCase):
    GOOD = _csv(
        "Txn Date,Narration,Ref No,Debit,Credit,Balance",
        "2026-04-01,UPI/CR/40001/CLIENT A,R1,0,25000,25000",
        "2026-04-02,UPI/DR/40002/ZOMATO,R2,500,0,24500",
    )

    def _files(self, n_good: int):
        files = [(f"CASE_B_E-{i}_HDFC_50100{i:04d}.csv", self.GOOD, None) for i in range(n_good)]
        files.insert(1, ("broken.docx", b"not a statement", None))
        files.insert(3, ("bad.xlsx", b"PK\x03\x04garbage", None))
        return files

    def test_status_objects_small_batch(self) -> None:
        results = bulk_parse_statements(self._files(3), "C", return_status=True)
        self.assertEqual(len(results), 5)
        self.assertTrue(all(isinstance(r, FileParseResult) for r in results))
        self.assertEqual([r.ok for r in results], [True, False, True, False, True])
        self.assertIn("Unsupported", results[1].error)
        self.assertEqual(results[0].statement.total_transactions, 2)
        self.assertEqual(results[1].filename, "broken.docx")

    def test_status_objects_process_pool_batch(self) -> None:
        results = bulk_parse_statements(self._files(10), "C", return_status=True)
        self.assertEqual(len(results), 12)
        self.assertEqual(sum(1 for r in results if r.ok), 10)
        self.assertEqual(sum(1 for r in results if not r.ok), 2)
        for r in results:
            if r.ok:
                self.assertEqual(len(r.transactions), 2)

    def test_legacy_mode_raises_with_salvageable_results(self) -> None:
        with self.assertRaises(BulkIngestError) as ctx:
            bulk_parse_statements(self._files(2), "C")
        self.assertEqual(len(ctx.exception.results), 4)
        self.assertEqual(sum(1 for r in ctx.exception.results if r.ok), 2)
        # Legacy tuple shape still returned when everything parses
        ok = bulk_parse_statements([("a.csv", self.GOOD, None), ("b.csv", self.GOOD, None)], "C")
        self.assertEqual(len(ok[0]), 3)


class TestXlsxIo(unittest.TestCase):
    def test_writer_handles_decimal_nan_and_spaces(self) -> None:
        wb = write_xlsx_bytes([("S", [["A", "B", "C"], [Decimal("12.50"), float("nan"), "  padded  "]])])
        rows = read_xlsx_bytes(wb)["S"]
        self.assertEqual(rows[1][0], 12.5)
        self.assertEqual(rows[1][1], "")
        self.assertEqual(rows[1][2], "  padded  ")


class TestSampleStatements(unittest.TestCase):
    # Row 3 of this fixture debits 250000 while the balance rises 10000 -> 250000; the file itself is inconsistent.
    KNOWN_SOURCE_DEFECTS = {"CASE041_APEX-SHELL-PVT-LTD_INDUS_201009988776.csv": 1}

    def test_every_sample_parses_and_tabular_ones_reconcile(self) -> None:
        paths = sorted(glob.glob(os.path.join(SAMPLES_DIR, "*")))
        self.assertGreaterEqual(len(paths), 10)
        for path in paths:
            name = os.path.basename(path)
            with self.subTest(sample=name):
                with open(path, "rb") as fh:
                    stmt, txns, _meta, counts = _parse_and_gate(name, fh.read())
                self.assertGreater(len(txns), 0)
                self.assertNotEqual(stmt.bank_code, "UNKNOWN")
                if name.lower().endswith((".csv", ".xlsx")):
                    self.assertEqual(counts["balance_mismatch"], self.KNOWN_SOURCE_DEFECTS.get(name, 0))
                    self.assertEqual(counts.get("date_unparseable", 0), 0)


if __name__ == "__main__":
    unittest.main()
