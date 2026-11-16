"""Comprehensive test suite for CypherEdge & CypherTrace requirements."""

from __future__ import annotations

from decimal import Decimal
import base64
import io
import json
import os
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.edge.itr_compliance import generate_itr_compliance_report
from cyphersol.edge.loan_eligibility import compute_loan_eligibility_report
from cyphersol.edge.tally_exporter import generate_tally_xml
from cyphersol.ingestion.anomaly_gate import run_anomaly_gate
from cyphersol.ingestion.ocr_engine import generate_scanned_pdf_statement
from cyphersol.ingestion.parser import (
    _reconcile_ocr_balances_and_amounts,
    bulk_parse_statements,
    detect_bank_and_metadata,
    parse_indian_date,
    parse_statement_bytes,
)
from cyphersol.ingestion.pdf_parser import PdfTextToken, _deskew_page_tokens, extract_pdf_data, generate_pdf_statement
from cyphersol.ingestion.xlsx_io import read_xlsx_bytes, write_xlsx_bytes
from cyphersol.llm_agents import LLMConfig, LLMProviderClient, StageAgentOrchestrator
from cyphersol.models import AnomalyFlag, DrawTemplate, MuleRiskTier, NormalizedTransaction, TxDirection
from cyphersol.nlp.categorizer import HybridTransactionCategorizer, apply_suspense_resolution
from cyphersol.nlp.entity_resolver import compute_name_similarity
from cyphersol.nlp.narration_parser import enrich_transaction_narration, parse_narration
from cyphersol.server import create_http_handler
from cyphersol.service import CypherApplicationService
from cyphersol.taxonomy import CATEGORIES_52, INDIAN_BANKS, KNOWLEDGE_BASE_POINT_COUNT


class CypherSolTestSuite(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test_vault.db")
        self.sample_dir = os.path.join(self.tmp.name, "samples")
        self.service = CypherApplicationService(self.db_path, self.sample_dir, seed_demo=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_01_taxonomy_banks_and_knowledge_base_scale(self) -> None:
        self.assertEqual(len(CATEGORIES_52), 52)
        self.assertGreaterEqual(len(INDIAN_BANKS), 105)
        # Honest curated count (NLP-03): no generated padding.
        from cyphersol.taxonomy import curated_knowledge_base_count
        self.assertEqual(KNOWLEDGE_BASE_POINT_COUNT, curated_knowledge_base_count())
        self.assertGreaterEqual(KNOWLEDGE_BASE_POINT_COUNT, 700)

    def test_02_multi_format_pdf_xlsx_csv_txt_and_draw_tool(self) -> None:
        # 1) PDF generation + extraction + Draw Tool column slicer
        rows = [
            ["01/04/2026", "UPI/CR/111222333444/RAZORPAY/rzp@icici", "UTR111", "", "50000.00", "150000.00"],
            ["02/04/2026", "NEFT-HDFC0000123-ZOMATO FOODS", "UTR112", "1250.50", "", "148749.50"],
        ]
        pdf_bytes = generate_pdf_statement(
            ["HDFC BANK STATEMENT", "Account Holder: TEST USER | Account No: 501009988776 | IFSC: HDFC0000123"],
            ["Txn Date", "Narration", "Chq/Ref No", "Debit", "Credit", "Balance"],
            rows,
        )
        extracted = extract_pdf_data(pdf_bytes)
        self.assertGreaterEqual(len(extracted.rows), 2)

        # Draw Tool split override
        tpl = DrawTemplate(
            template_id="TPL_TEST",
            bank_code="HDFC",
            template_name="HDFC Draw",
            table_bbox=[0.0, 0.0, 1.0, 1.0],
            column_splits=[0.15, 0.50, 0.64, 0.74, 0.85],
            column_names=["date", "narration", "ref", "debit", "credit", "balance"],
        )
        stmt, txns, _ = parse_statement_bytes("CASE1_TEST-USER_HDFC_501009988776.pdf", pdf_bytes, "C1", draw_template=tpl)
        self.assertEqual(stmt.bank_code, "HDFC")
        self.assertEqual(len(txns), 2)
        self.assertEqual(float(txns[0].credit), 50000.00)
        self.assertEqual(float(txns[1].debit), 1250.50)

        # 2) XLSX round-trip
        xlsx_bytes = write_xlsx_bytes([
            ("Statement", [
                ["Date", "Particulars", "Debit", "Credit", "Balance"],
                ["2026-04-01", "SALARY CREDIT TCS", 0, 120000, 150000],
                ["2026-04-02", "ATM WDL/SELF", 5000, 0, 145000],
            ])
        ])
        sheets = read_xlsx_bytes(xlsx_bytes)
        self.assertIn("Statement", sheets)
        stmt_x, txns_x, _ = parse_statement_bytes("CA_TEST_SBI_1122334455.xlsx", xlsx_bytes, "C1")
        self.assertEqual(len(txns_x), 2)
        self.assertEqual(float(txns_x[0].credit), 120000.0)

        # 3) Pipe-delimited TXT with descending order + wrapped narration + Indian Cr/Dr
        txt_bytes = (
            "Date|Narration|Amount|Type|Balance\n"
            "03-04-2026|NEFT/DR/VENDOR X|10,000.00|DR|1,40,000.00 Cr\n"
            "02-04-2026|UPI/CR/CLIENT Y|50,000.00|CR|1,50,000.00 Cr\n"
        ).encode("utf-8")
        stmt_t, txns_t, _ = parse_statement_bytes("CASE_DEMO_AXIS_99887766.txt", txt_bytes, "C1")
        stmt_t, txns_t, counts = run_anomaly_gate(stmt_t, txns_t)
        self.assertEqual(txns_t[0].txn_date, "2026-04-02")
        self.assertEqual(counts["balance_mismatch"], 0)

    def test_03_anomaly_gate_detects_balance_mismatch_and_duplicates(self) -> None:
        csv_bad = (
            "Txn Date,Narration,Debit,Credit,Balance\n"
            "2026-04-01,OPENING DEPOSIT,0,10000,10000\n"
            "2026-04-02,TAMPERED ROW,2000,0,9500\n"
            "2026-04-02,TAMPERED ROW,2000,0,9500\n"
        ).encode("utf-8")
        stmt, txns, _ = parse_statement_bytes("CASE_BAD_ICICI_00112233.csv", csv_bad, "C_BAD")
        stmt, txns, counts = run_anomaly_gate(stmt, txns)
        self.assertGreaterEqual(counts["balance_mismatch"], 1)
        self.assertEqual(counts["duplicate_suspect"], 1)

    def test_04_narration_categorizer_suspense_and_autolearn(self) -> None:
        parsed = parse_narration("UPI/CR/409110012345/AYAZ QURESHI/ayaz@okhdfcbank/HDFC0000240")
        self.assertEqual(parsed.payment_channel, "UPI")
        self.assertEqual(parsed.counterparty_vpa_or_acc, "ayaz@okhdfcbank")
        self.assertEqual(parsed.counterparty_ifsc, "HDFC0000240")
        self.assertEqual(parsed.utr_number, "409110012345")

        # Verify Demo CypherEdge case has Suspense entries and resolves + auto-learns
        edge_over = self.service.get_case_overview("CASE_EDGE_BALAJI_FY25", tx_limit=500)
        self.assertGreater(edge_over["summary"]["suspense_count"], 0)
        suspense_tx = next(t for t in edge_over["transactions"] if t["is_suspense"])
        res = self.service.resolve_suspense(
            "CASE_EDGE_BALAJI_FY25",
            tx_id=suspense_tx["tx_id"],
            category_id=18,  # Legal, Audit & Professional Consultancy Paid
            custom_ledger_name="S K Verma Consultancy",
            auto_refresh_similar=True,
        )
        self.assertGreaterEqual(res["updated_count"], 1)

    def test_05_entity_resolution_and_similarity_threshold(self) -> None:
        sim = compute_name_similarity("POOJAN VIG", "Pujan Vij")
        self.assertGreaterEqual(sim, 0.85)
        # Re-cluster Trace demo case at 0.85 vs 0.99
        res_merged = self.service.recluster_entities("CASE_TRACE_EOW_041", similarity_threshold=0.85)
        poojan_ent = next(e for e in res_merged["entities"] if "POOJAN VIG" in e["canonical_name"])
        self.assertTrue(any("PUJAN VIJ" in a for a in poojan_ent["aliases"]))

    def test_06_cypheredge_tally_itr_and_loan_eligibility(self) -> None:
        overview = self.service.get_case_overview("CASE_EDGE_BALAJI_FY25")
        self.assertEqual(len(overview["statements"]), 1)
        self.assertEqual(overview["statements"][0]["balance_mismatch_count"], 0)

        tally = self.service.get_tally_preview("CASE_EDGE_BALAJI_FY25")
        root = ET.fromstring(tally["xml"])
        self.assertEqual(root.tag, "ENVELOPE")
        self.assertGreater(tally["stats"]["vouchers_exported"], 15)

        itr = self.service.get_itr_report("CASE_EDGE_BALAJI_FY25")
        self.assertGreater(itr["reconciliation"]["gross_credits"], 1_000_000)
        sections = {f["section"] for f in itr["statutory_red_flags"]}
        self.assertIn("Section 269ST", sections)

        loan = self.service.get_loan_report("CASE_EDGE_BALAJI_FY25", foir_pct=60.0)
        self.assertGreater(loan["daily_abb"], 100_000)
        self.assertGreater(loan["fixed_date_abb"], 100_000)
        self.assertEqual(loan["inward_bounce_count"], 1)
        self.assertGreater(loan["max_eligible_loan"], 0)

    def test_07_cyphertrace_layering_loops_bidirectional_and_mule_scoring(self) -> None:
        # 1) Layering Tree matching Brochure Screenshot (AYAZ QURESHI ₹500 -> POOJAN VIG ₹300 & DEEP PATANGE ₹200)
        layering = self.service.get_layering("CASE_TRACE_EOW_041")
        root = layering["root"]
        self.assertIsNotNone(root)
        self.assertEqual(root["role"], "CREDIT_SOURCE")
        self.assertIn("AYAZ QURESHI", root["entity_name"])
        self.assertEqual(root["amount"], 500.0)
        child_map = {c["entity_name"]: c for c in root["children"]}
        self.assertIn("POOJAN VIG", child_map)
        self.assertIn("DEEP PATANGE", child_map)
        self.assertEqual(child_map["POOJAN VIG"]["amount"], 300.0)
        self.assertEqual(child_map["DEEP PATANGE"]["amount"], 200.0)
        self.assertTrue(child_map["POOJAN VIG"]["has_statement"])

        # Test Move to Misc & Restore
        poojan_id = child_map["POOJAN VIG"]["entity_id"]
        self.service.toggle_layering_misc("CASE_TRACE_EOW_041", poojan_id, move_to_misc=True)
        layering_misc = self.service.get_layering("CASE_TRACE_EOW_041", source_entity_id=root["entity_id"])
        roles = [c["role"] for c in layering_misc["root"]["children"]]
        self.assertIn("MISC", roles)
        self.service.toggle_layering_misc("CASE_TRACE_EOW_041", poojan_id, move_to_misc=False)

        # 2) Circular Loop Detection (HAWALA -> APEX -> ZENITH -> HAWALA)
        loops = self.service.get_loops("CASE_TRACE_EOW_041", max_days=30, min_retention=0.5)
        self.assertGreaterEqual(len(loops), 1)
        top_loop = loops[0]
        self.assertEqual(top_loop["hop_count"], 3)
        self.assertEqual(top_loop["entities"][0], top_loop["entities"][-1])

        # 3) Bidirectional Flow (POOJAN VIG <-> APEX SHELL PVT LTD)
        bi = self.service.get_bidirectional("CASE_TRACE_EOW_041")
        self.assertGreaterEqual(len(bi["pairs"]), 1)
        d = bi["selected_detail"]
        self.assertAlmostEqual(d["net_flow"], d["total_a_to_b"] - d["total_b_to_a"], places=2)
        rev = self.service.get_bidirectional("CASE_TRACE_EOW_041", d["entity_b_id"], d["entity_a_id"])["selected_detail"]
        self.assertAlmostEqual(d["net_flow"], -rev["net_flow"], places=2)

        # 4) 10-Parameter Mule Detection (RAMESH MULE must score Critical >= 80)
        mules = self.service.get_mule_assessments("CASE_TRACE_EOW_041")
        mule_by_name = {m["account_holder_name"]: m for m in mules}
        ramesh = next(v for k, v in mule_by_name.items() if "RAMESH" in k.upper())
        self.assertGreaterEqual(ramesh["composite_score"], 80.0)
        self.assertEqual(ramesh["risk_tier"], MuleRiskTier.CRITICAL.value)

        # Low-risk control check on normal account (SAMIDA DEVKAR / MANISH PATIL)
        samida = next(v for k, v in mule_by_name.items() if "SAMIDA" in k.upper())
        self.assertLess(samida["composite_score"], 50.0)

        # 5) Force-Directed Network Graph Roles & Coordinates
        graph = self.service.get_network_graph("CASE_TRACE_EOW_041")
        self.assertGreaterEqual(len(graph["nodes"]), 25)
        valid_roles = {"PRIMARY_HUB", "BRIDGE_CONDUIT", "EXIT_ENDPOINT", "PERIPHERAL_LEAF"}
        for n in graph["nodes"]:
            self.assertIn(n["node_role"], valid_roles)
            self.assertIsInstance(n["x"], float)
            self.assertIsInstance(n["y"], float)
            self.assertTrue(0.0 <= n["x"] <= 920.0)
            self.assertTrue(0.0 <= n["y"] <= 520.0)
        conduits = {n["label"] for n in graph["nodes"] if n["node_role"] == "BRIDGE_CONDUIT"}
        self.assertGreaterEqual(len(conduits), 3)
        self.assertIn("APEX SHELL PVT LTD", conduits)
        self.assertIn("HAWALA TRADERS", conduits)

    def test_08_bulk_100_statements_performance_and_exports(self) -> None:
        # Verify bulk ingestion of 100 statements completes rapidly (< 15 seconds)
        sample_csv = (
            "Txn Date,Narration,Ref No,Debit,Credit,Balance\n"
            "2026-04-01,UPI/CR/40001/CLIENT A,R1,0,25000,25000\n"
            "2026-04-02,UPI/DR/40002/ZOMATO,R2,500,0,24500\n"
        ).encode("utf-8")
        files = [(f"CASE_BULK_ENTITY-{i}_HDFC_501000{i:04d}.csv", sample_csv, None) for i in range(100)]
        t0 = time.perf_counter()
        results = bulk_parse_statements(files, "CASE_BULK")
        elapsed = time.perf_counter() - t0
        self.assertEqual(len(results), 100)
        self.assertLess(elapsed, 15.0)

        # Verify XLSX, HTML Dossier & PDF Dossier exports
        xlsx_out = self.service.export_xlsx("CASE_TRACE_EOW_041")
        parsed_wb = read_xlsx_bytes(xlsx_out)
        self.assertIn("Executive Summary", parsed_wb)
        self.assertIn("Mule Risk Scorecard", parsed_wb)

        html_doc = self.service.export_dossier_html("CASE_TRACE_EOW_041")
        self.assertIn("Section 63 BSA", html_doc)

        pdf_doc = self.service.export_dossier_pdf("CASE_TRACE_EOW_041")
        self.assertTrue(pdf_doc.startswith(b"%PDF-1.4"))

    def test_09_http_api_end_to_end(self) -> None:
        class _MockSocket:
            def __init__(self, raw_request: bytes) -> None:
                self._rbuf = io.BytesIO(raw_request)
                self.wbuf = io.BytesIO()

            def makefile(self, mode: str, *args, **kwargs):
                if "r" in mode:
                    return self._rbuf
                return self.wbuf

            def sendall(self, data: bytes) -> None:
                self.wbuf.write(data)

        def invoke(handler_cls, method: str, path: str, body: dict | None = None) -> tuple[int, bytes]:
            payload = json.dumps(body).encode("utf-8") if body is not None else b""
            headers = [
                f"{method} {path} HTTP/1.1",
                "Host: localhost",
                "Connection: close",
                f"X-CypherSOL-Token: {handler_cls.api_token}",
            ]
            if body is not None:
                headers.append("Content-Type: application/json")
                headers.append(f"Content-Length: {len(payload)}")
            raw_req = ("\r\n".join(headers) + "\r\n\r\n").encode("utf-8") + payload
            sock = _MockSocket(raw_req)
            handler_cls(sock, ("127.0.0.1", 54321), None)
            raw_resp = sock.wbuf.getvalue()
            head, resp_body = raw_resp.split(b"\r\n\r\n", 1)
            status_line = head.split(b"\r\n", 1)[0].decode("latin-1")
            status_code = int(status_line.split()[1])
            return status_code, resp_body

        ui_dir = os.path.join(ROOT_DIR, "src", "cyphersol", "ui")
        handler_cls = create_http_handler(self.service, ui_dir)

        code, body = invoke(handler_cls, "GET", "/api/health")
        self.assertEqual(code, 200)
        health = json.loads(body.decode("utf-8"))
        self.assertEqual(health["status"], "ok")
        self.assertEqual(health["engine"], "CypherSOL Offline Core")
        self.assertEqual(health["version"], "1.0.0")
        self.assertGreaterEqual(health["banks_supported"], 105)

        code, body = invoke(handler_cls, "GET", "/api/cases/CASE_EDGE_BALAJI_FY25/edge/loan?foir=invalid")
        self.assertEqual(code, 400)
        err_resp = json.loads(body.decode("utf-8"))
        self.assertIn("error", err_resp)

        code, body = invoke(handler_cls, "GET", "/")
        self.assertEqual(code, 200)
        html_home = body.decode("utf-8")
        self.assertIn("CypherSOL", html_home)

        code, body = invoke(handler_cls, "GET", "/api/cases/CASE_TRACE_EOW_041/trace/mules")
        self.assertEqual(code, 200)
        mules_resp = json.loads(body.decode("utf-8"))
        self.assertGreaterEqual(len(mules_resp["assessments"]), 5)

        code, body = invoke(
            handler_cls,
            "POST",
            "/api/cases/CASE_TRACE_EOW_041/entities/recluster",
            {"similarity_threshold": 0.85},
        )
        self.assertEqual(code, 200)
        recluster_resp = json.loads(body.decode("utf-8"))
        self.assertGreaterEqual(len(recluster_resp["entities"]), 5)

    def test_10_indian_bank_narration_grammars_comprehensive(self) -> None:
        cases = [
            (
                "TO TRANSFER-NEFT*SBIN0001234*APEX SHELL PVT LTD*SBIN261051234567",
                "NEFT",
                "APEX SHELL PVT LTD",
                "SBIN0001234",
                "SBIN261051234567",
            ),
            (
                "BY TRANSFER-UPI/CR/412345678901/ANITA SHARMA/HDFC/anita@okhdfcbank/UPI",
                "UPI",
                "ANITA SHARMA",
                None,
                "412345678901",
            ),
            (
                "UPI-ZOMATO LIMITED-zomato@hdfcbank-HDFC0000240-412345678902-Dinner",
                "UPI",
                "ZOMATO LIMITED",
                "HDFC0000240",
                "412345678902",
            ),
            (
                "UPI-TATA-AIG-GENERAL-INS-tataaig@icici-ICIC0000001-412345678903-Policy",
                "UPI",
                "TATA AIG GENERAL INS",
                "ICIC0000001",
                "412345678903",
            ),
            (
                "NEFT CR-HDFC0000240-SUNRISE LOGISTICS LLP-N123261234567890",
                "NEFT",
                "SUNRISE LOGISTICS LLP",
                "HDFC0000240",
                "N123261234567890",
            ),
            (
                "ACH D- BAJAJ FINANCE LTD- 9988776655",
                "NACH",
                "BAJAJ FINANCE LTD",
                None,
                None,
            ),
            (
                "MMT/IMPS/412345678904/Rent Apr/VIKRAM PATEL/UTIB0000100",
                "IMPS",
                "VIKRAM PATEL",
                "UTIB0000100",
                "412345678904",
            ),
            (
                "VPS/RELIANCE FRESH/412345678905/MUMBAI",
                "POS",
                "RELIANCE FRESH",
                None,
                "412345678905",
            ),
            (
                "BIL/INFT/8877665544/TATA POWER COMPANY LTD/MUMBAI",
                "TRANSFER",
                "TATA POWER COMPANY LTD",
                None,
                "8877665544",
            ),
            (
                "UPI/412345678906/PAYMENT/rahul.sharma99@okicici/Axis Bank",
                "UPI",
                "RAHUL SHARMA",
                None,
                "412345678906",
            ),
            (
                "IMPS/P2A/412345678907/KIRAN ENTERPRISES/HDFC/987654",
                "IMPS",
                "KIRAN ENTERPRISES",
                None,
                "412345678907",
            ),
            (
                "AEPS/CW/412345678908/FINO AGENT RAMESH",
                "AEPS",
                "FINO AGENT RAMESH",
                None,
                "412345678908",
            ),
            (
                "BY CLG-MICR 112233-SHARMA ENTERPRISES",
                "CHEQUE",
                "SHARMA ENTERPRISES",
                None,
                None,
            ),
        ]
        for raw, exp_ch, exp_party, exp_ifsc, exp_utr in cases:
            with self.subTest(narration=raw):
                parsed = parse_narration(raw)
                self.assertEqual(parsed.payment_channel, exp_ch)
                if exp_party:
                    self.assertIsNotNone(parsed.counterparty_name)
                    # Hyphenated TATA-AIG vs space normalization both acceptable if words match
                    self.assertEqual(
                        parsed.counterparty_name.replace("-", " "),
                        exp_party,
                    )
                if exp_ifsc:
                    self.assertEqual(parsed.counterparty_ifsc, exp_ifsc)
                if exp_utr:
                    self.assertEqual(parsed.utr_number, exp_utr)

        # False-positive resistance: business names containing rail substrings
        fp_upi = parse_narration("NEFT/HDFC0001234/JUPITER HOSPITALITY PVT LTD/HDFCN261051234567")
        self.assertEqual(fp_upi.payment_channel, "NEFT")
        self.assertEqual(fp_upi.counterparty_name, "JUPITER HOSPITALITY PVT LTD")

        fp_atm = parse_narration("RTGS/SBIN0001234/MAHATMA TRUST/SBINR520261234567")
        self.assertEqual(fp_atm.payment_channel, "RTGS")
        self.assertEqual(fp_atm.counterparty_name, "MAHATMA TRUST")

    def test_11_scanned_pdf_and_image_ocr_extraction(self) -> None:
        title_lines = [
            "STATE BANK OF INDIA",
            "Account No: 300011223344",
            "IFSC: SBIN0001234",
            "Opening Balance: 100000.00",
        ]
        columns = ["Txn Date", "Narration", "Debit", "Credit", "Balance"]
        rows = [
            ["05/04/2026", "UPI-ZOMATO-zomato@hdfc-412345678901", "1250.50", "", "98749.50"],
            ["06/04/2026", "NEFT CR-HDFC0000240-APEX TRADERS-N123261234567890", "", "25000.00", "123749.50"],
            ["07/04/2026", "TO TRANSFER-NEFT*SBIN0001234*KIRAN LOGISTICS", "15000.00", "", "108749.50"],
            ["08/04/2026", "IMPS/P2A/412345678909/R K CONSULTANTS", "", "12000.00", "120749.50"],
        ]
        scanned_pdf = generate_scanned_pdf_statement(title_lines, columns, rows)
        self.assertNotIn(b"BT", scanned_pdf)
        self.assertIn(b"/Image", scanned_pdf)

        pdf_res = extract_pdf_data(scanned_pdf)
        self.assertTrue(pdf_res.needs_ocr)
        self.assertNotEqual(pdf_res.ocr_engine, "none")
        self.assertGreaterEqual(len(pdf_res.rows), 4)

        stmt, txns, meta = parse_statement_bytes("scanned_sbi_statement.pdf", scanned_pdf, "CASE_OCR_TEST")
        self.assertTrue(stmt.is_ocr_derived)
        self.assertIn("date", meta["col_map"])
        self.assertEqual(len(txns), 4)
        self.assertEqual(txns[0].payment_channel, "UPI")
        self.assertEqual(txns[0].debit, Decimal("1250.50"))
        self.assertEqual(txns[1].payment_channel, "NEFT")
        self.assertEqual(txns[1].credit, Decimal("25000.00"))
        self.assertEqual(txns[2].debit, Decimal("15000.00"))
        self.assertEqual(txns[3].payment_channel, "IMPS")
        self.assertEqual(txns[3].running_balance, Decimal("120749.50"))

        stmt_checked, checked_txns, counts = run_anomaly_gate(stmt, txns)
        self.assertEqual(counts["balance_mismatch"], 0)
        self.assertEqual(stmt_checked.balance_mismatch_count, 0)
        # ING-17: LOW_OCR_CONFIDENCE is a statement-level flag; clean OCR rows stay VALID (or OCR_AUTOCORRECTED).
        self.assertIn("LOW_OCR_CONFIDENCE", counts["statement_flags"])
        for txn in checked_txns:
            self.assertTrue(
                AnomalyFlag.VALID in txn.anomaly_flags or AnomalyFlag.OCR_AUTOCORRECTED in txn.anomaly_flags
            )

    def test_12_single_amount_column_and_same_day_reverse_chronological(self) -> None:
        # (a) Single unsigned Amount column inferred cleanly from running balance delta
        csv_unsigned = "\n".join(
            [
                "Bank Statement - Single Amount Column",
                "Account Number,998877665544",
                "IFSC,HDFC0000240",
                "Opening Balance,100000.00",
                "Txn Date,Narration,Amount,Balance",
                "05/04/2026,NEFT CR-HDFC0000240-DELTA CORP-N123260000000001,25000.00,125000.00",
                "06/04/2026,UPI-METRO RAIL-metro@axis-412345678910,12500.00,112500.00",
                "07/04/2026,IMPS/P2A/412345678911/POWER GRID,4000.00,108500.00",
                "08/04/2026,RTGS-HDFCR52026040800000001-BETA EXPORTS,40000.00,148500.00",
            ]
        ).encode("utf-8")

        stmt_a, txns_a, _ = parse_statement_bytes("single_amount.csv", csv_unsigned, "CASE_SINGLE_AMT")
        stmt_a, txns_a, counts_a = run_anomaly_gate(stmt_a, txns_a)
        self.assertEqual(counts_a["balance_mismatch"], 0)
        self.assertEqual([t.direction for t in txns_a], [TxDirection.CREDIT, TxDirection.DEBIT, TxDirection.DEBIT, TxDirection.CREDIT])
        self.assertEqual(txns_a[0].credit, Decimal("25000.00"))
        self.assertEqual(txns_a[1].debit, Decimal("12500.00"))
        self.assertEqual(txns_a[3].running_balance, Decimal("148500.00"))

        # (b) Same-day reverse-chronological statement restored to forward chronological order
        csv_same_day_rev = "\n".join(
            [
                "Account Number,445566778899",
                "Opening Balance,50000.00",
                "Txn Date,Narration,Debit,Credit,Balance",
                "05/04/2026,UPI-FINAL SHOP-shop@okaxis-412345678914,2000.00,,68000.00",
                "05/04/2026,NEFT CR-SBIN0001234-CLIENT A-N123260000000002,,30000.00,70000.00",
                "05/04/2026,UPI-COFFEE DAY-ccd@okicici-412345678913,10000.00,,40000.00",
            ]
        ).encode("utf-8")

        stmt_b, txns_b, _ = parse_statement_bytes("same_day_rev.csv", csv_same_day_rev, "CASE_SAME_DAY_REV")
        stmt_b, txns_b, counts_b = run_anomaly_gate(stmt_b, txns_b)
        self.assertEqual(counts_b["balance_mismatch"], 0)
        self.assertEqual(
            [t.running_balance for t in txns_b],
            [Decimal("40000.00"), Decimal("70000.00"), Decimal("68000.00")],
        )

        # (c) Multi-line wrapped narration with "PAGE INDUSTRIES LTD" preserved alongside "Page 1 of 2" footer
        csv_wrapped = "\n".join(
            [
                "Account Number,112233445566",
                "Opening Balance,100000.00",
                "Txn Date,Narration,Debit,Credit,Balance",
                "09/04/2026,NEFT/HDFC0000240/PAGE INDUSTRIES LTD,15000.00,,85000.00",
                ",GARMENT VENDOR SETTLEMENT,,,",
                ",Page 1 of 2,,,",
                "10/04/2026,NEFT CR-ICIC0000001-ACME TEXTILES,,20000.00,105000.00",
            ]
        ).encode("utf-8")

        stmt_c, txns_c, _ = parse_statement_bytes("wrapped_page_ind.csv", csv_wrapped, "CASE_WRAP_PAGE")
        stmt_c, txns_c, counts_c = run_anomaly_gate(stmt_c, txns_c)
        txns_c = [enrich_transaction_narration(t) for t in txns_c]
        self.assertEqual(counts_c["balance_mismatch"], 0)
        self.assertEqual(len(txns_c), 2)
        self.assertIsNotNone(txns_c[0].extracted_counterparty)
        self.assertIn("PAGE INDUSTRIES LTD", txns_c[0].extracted_counterparty)
        self.assertIn("GARMENT VENDOR SETTLEMENT", txns_c[0].raw_narration)
        self.assertNotIn("Page 1 of 2", txns_c[0].raw_narration)

    def test_13_ocr_reconciliation_keystone_and_metadata_hardening(self) -> None:
        # 1) 2-digit OCR year fix ('.75' -> '2025')
        self.assertEqual(parse_indian_date("10.05.75"), "2025-05-10")

        # Helper to construct synthetic NormalizedTransaction rows
        def _mk_tx(
            idx: int,
            txn_date: str,
            raw: str,
            debit: str,
            credit: str,
            bal: str,
        ) -> NormalizedTransaction:
            d = Decimal(debit)
            c = Decimal(credit)
            return NormalizedTransaction(
                tx_id=f"SYN_{idx}",
                statement_id="ST_SYN",
                case_id="CASE_SYN_13",
                row_index=idx,
                txn_date=txn_date,
                raw_narration=raw,
                payment_channel="TRANSFER",
                direction=TxDirection.CREDIT if c > d else TxDirection.DEBIT,
                debit=d,
                credit=c,
                running_balance=Decimal(bal),
                category_id=1,
                category_name="Suspense",
                categorization_source="rule",
                confidence_score=0.5,
            )

        # 2a) Headerless 5-row OCR reconciliation: preserve tx[0].running_balance while repairing tx[1].debit & later dropped digits
        passbook_rows = [
            _mk_tx(0, "2025-05-05", "NEFT CR-TREASURY OFFICE", "0.00", "72.00", "4150.50"),
            _mk_tx(1, "2025-05-09", "PMJJBY RENEWAL", "736.00", "0.00", "3714.50"),
            _mk_tx(2, "2025-05-10", "PMSBY RENEWAL", "20.00", "0.00", "3694.50"),
            _mk_tx(3, "2025-06-06", "APY CONTRIBUTION", "22.00", "0.00", "597.50"),
            _mk_tx(4, "2025-06-11", "PFMS DBT CREDIT", "0.00", "1900.00", "497.50"),
        ]
        reconciled_pb = _reconcile_ocr_balances_and_amounts(
            passbook_rows,
            header_opening_bal=None,
            footer_closing_bal=Decimal("5497.50"),
        )
        self.assertEqual([t.running_balance for t in reconciled_pb], [
            Decimal("4150.50"),
            Decimal("3714.50"),
            Decimal("3694.50"),
            Decimal("3597.50"),
            Decimal("5497.50"),
        ])
        self.assertEqual(reconciled_pb[1].debit, Decimal("436.00"))
        self.assertEqual(reconciled_pb[3].debit, Decimal("97.00"))

        # 2b) Candidate C + strict tol=0.005 paise exactness (.02 drift not skipped)
        paise_rows = [
            _mk_tx(0, "2025-03-31", "UPI CR", "0.00", "21700.00", "23583.75"),
            _mk_tx(1, "2025-04-04", "Int.Pd", "0.00", "16.00", "23599.73"),
            _mk_tx(2, "2025-04-04", "CHEQUE FINANCE", "23720.00", "0.00", "2999.73"),
            _mk_tx(3, "2025-05-02", "UPI CR", "0.00", "8000.00", "10899.15"),
            _mk_tx(4, "2025-05-02", "UPI CR", "0.00", "15000.00", "23899.00"),
        ]
        reconciled_paise = _reconcile_ocr_balances_and_amounts(
            paise_rows,
            header_opening_bal=Decimal("1883.75"),
            footer_closing_bal=Decimal("25899.75"),
        )
        self.assertEqual(reconciled_paise[1].running_balance, Decimal("23599.75"))
        self.assertEqual(reconciled_paise[2].debit, Decimal("20700.00"))
        self.assertEqual(reconciled_paise[2].running_balance, Decimal("2899.75"))
        self.assertEqual(reconciled_paise[3].running_balance, Decimal("10899.75"))
        self.assertEqual(reconciled_paise[4].running_balance, Decimal("25899.75"))

        # 3) Continuation-page bank isolation, nominee exclusion, and same-line account extraction
        prof_hdr, meta_hdr = detect_bank_and_metadata(
            "\n".join([
                "STATE BANK OF INDIA",
                "Nominee Name: KAVITA RAO",
                "Account Name: ARJUN MEHTA AND PRIYA MEHTA",
                "Account Number of Branch",
                "Account Number: 99887766554",
                "IFSC: SBIN0009999",
                "Date | Particulars | Debit | Credit | Balance",
                "03-11-2024 | DEBIT ACHDr NACH00001 IDFC FIRST BANK | 2588.00 | | 4302.12",
            ]),
            "synthetic_page1.pdf",
        )
        self.assertEqual(prof_hdr.bank_code, "SBI")
        self.assertEqual(meta_hdr["account_holder_name"], "ARJUN MEHTA AND PRIYA MEHTA")
        self.assertEqual(meta_hdr["account_number"], "99887766554")

        prof_cont, meta_cont = detect_bank_and_metadata(
            "\n".join([
                "Date | Details | Ref No | Debit | Credit | Balance",
                "02-11-2024 | DEP TFR UPI/CR/111122223333/CANARA BANK | | 2000.00 | 6290.12",
                "03-11-2024 | DEBIT ACHDr NACH00001 IDFC FIRST BANK | 2588.00 | | 4302.12",
            ]),
            "synthetic_page2.pdf",
        )
        self.assertNotIn(prof_cont.bank_code, {"IDFC", "CANARA"})
        self.assertEqual(meta_cont["account_number"], "UNKNOWN_AC")

        # 4) Row-anchor keystone deskew on headerless tilted synthetic tokens
        tilted_tokens: list[PdfTextToken] = []
        base_ys = [120.0, 160.0, 200.0, 240.0, 280.0, 320.0, 360.0]
        for r_idx, y0 in enumerate(base_ys):
            # Non-uniform upward curl toward right margin (-5.0 to -14.0 pts)
            right_shift = -(5.0 + 1.5 * r_idx)
            tilted_tokens.append(PdfTextToken(page=0, x=45.0, y=y0, text=f"0{r_idx + 1}/05/2025", x_end=95.0))
            tilted_tokens.append(PdfTextToken(page=0, x=220.0, y=y0 + right_shift * 0.4, text="UPI TRANSFER", x_end=310.0))
            tilted_tokens.append(PdfTextToken(page=0, x=520.0, y=y0 + right_shift, text=f"{1000 + r_idx * 100}.00Cr", x_end=585.0))
        deskewed = _deskew_page_tokens(tilted_tokens, page_w=612.0)
        for r_idx in range(len(base_ys)):
            date_tok = deskewed[r_idx * 3]
            bal_tok = deskewed[r_idx * 3 + 2]
            self.assertLessEqual(abs(date_tok.y - bal_tok.y), 3.0)

        # 5) OCR narration normalization assertions
        n_int = parse_narration("99887766554:3nt.Pd:01-01-2025 to 31-03-2025")
        self.assertEqual(n_int.payment_channel, "INTEREST")
        self.assertEqual(n_int.counterparty_name, "BANK INTEREST")

        n_neft = parse_narration("NEFT RBI1262527991464 ERBISOGORJER, TREASURY OFFICE")
        self.assertEqual(n_neft.payment_channel, "NEFT")
        self.assertEqual(n_neft.utr_number, "RBI1262527991464")
        self.assertEqual(n_neft.counterparty_name, "TREASURY OFFICE")

        n_chq = parse_narration("HDB FINANCIAL SERVIC L IU KANPUR, SERVICE BRANCH Collecting Bank")
        self.assertEqual(n_chq.payment_channel, "CHEQUE")
        self.assertEqual(n_chq.counterparty_name, "HDB FINANCIAL SERVICES")

    def test_14_llm_backup_agents_and_runtime_modes(self) -> None:
        # 1) Multi-provider request formatting (Gemini, Claude, OpenAI) including Vision images
        tiny_png_b64 = base64.b64encode(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR").decode("ascii")
        recorded_calls: list[dict] = []

        def _mock_provider_transport(url: str, headers: dict, body: dict, timeout: int) -> dict:
            recorded_calls.append({"url": url, "headers": headers, "body": body, "timeout": timeout})
            sys_txt = (
                body.get("system")
                or (body.get("system_instruction", {}).get("parts") or [{}])[0].get("text")
                or (body.get("messages") or [{}])[0].get("content", "")
            )
            if "connectivity probe" in sys_txt:
                payload = {"status": "ok", "service": "cyphersol"}
            elif "Agent 1" in sys_txt:
                payload = {
                    "bank_code": "HDFC",
                    "bank_name": "HDFC Bank",
                    "account_number": "501009988776",
                    "ifsc_code": "HDFC0000240",
                    "account_holder_name": "RAHUL KUMAR SHARMA",
                    "account_type": "Savings",
                    "rows": [
                        {
                            "date": "2026-04-01",
                            "narration": "MYSTERY_VOUCHER_8821",
                            "debit": 0.0,
                            "credit": 50000.0,
                            "balance": 150000.0,
                        },
                        {
                            "date": "2026-04-02",
                            "narration": "PAID TO RAHUL K SHARMA REF 9912",
                            "debit": 10000.0,
                            "credit": 0.0,
                            "balance": 140000.0,
                        },
                    ],
                }
            elif "Agent 2" in sys_txt:
                payload = {
                    "metadata": {
                        "bank_code": "ICICI",
                        "bank_name": "ICICI Bank",
                        "account_number": "000901234567",
                        "ifsc_code": "ICIC0000009",
                        "account_holder_name": "MEERA TRADERS",
                    },
                    "repairs": [
                        {
                            "row_index": 1,
                            "debit": 1500.00,
                            "credit": 0.0,
                            "running_balance": 8500.00,
                        }
                    ],
                }
            elif "Agent 3" in sys_txt:
                user_msg = (
                    body.get("contents", [{}])[0].get("parts", [{}])[0].get("text")
                    or (body.get("messages") or [{}])[-1].get("content", "{}")
                )
                if isinstance(user_msg, list):
                    user_msg = user_msg[0].get("text", "{}")
                tx_items = json.loads(user_msg).get("transactions", [])
                payload = {
                    "results": [
                        {
                            "tx_id": item["tx_id"],
                            "payment_channel": "NEFT" if "MYSTERY" in item["narration"] else "IMPS",
                            "utr_number": "HDFCR5202604010001",
                            "counterparty_ifsc": "HDFC0000240",
                            "counterparty_vpa_or_acc": "arjun@okbiz" if "MYSTERY" in item["narration"] else "rks@okbiz",
                            "extracted_counterparty": "ARJUN V MEHTA" if "MYSTERY" in item["narration"] else "R K SHARMA",
                        }
                        for item in tx_items
                    ]
                }
            elif "Agent 4A" in sys_txt:
                user_msg = (
                    body.get("contents", [{}])[0].get("parts", [{}])[0].get("text")
                    or (body.get("messages") or [{}])[-1].get("content", "{}")
                )
                if isinstance(user_msg, list):
                    user_msg = user_msg[0].get("text", "{}")
                tx_items = json.loads(user_msg).get("transactions", [])
                payload = {
                    "classifications": [
                        {
                            "tx_id": item["tx_id"],
                            "category_id": 2 if item["direction"] == "CR" else 11,
                            "confidence": 0.93,
                            "tally_ledger_name": "ARJUN V MEHTA" if item["direction"] == "CR" else "RAHUL KUMAR SHARMA",
                        }
                        for item in tx_items
                    ]
                }
            elif "Agent 4B" in sys_txt:
                payload = {
                    "merges": [
                        {"alias": "R K SHARMA", "canonical": "RAHUL KUMAR SHARMA"}
                    ]
                }
            else:
                payload = {"ok": True}

            txt = json.dumps(payload)
            if "generativelanguage.googleapis.com" in url:
                return {"candidates": [{"content": {"parts": [{"text": txt}]}}]}
            if "api.anthropic.com" in url:
                return {"content": [{"type": "text", "text": txt}]}
            return {"choices": [{"message": {"content": txt}}]}

        for prov, key_val, expect_url, expect_hdr in [
            ("gemini", "AIzaTestGeminiKey99", "generativelanguage.googleapis.com", None),
            ("claude", "sk-ant-TestClaudeKey99", "api.anthropic.com/v1/messages", "x-api-key"),
            ("openai", "sk-TestOpenAiKey99", "api.openai.com/v1/chat/completions", "Authorization"),
        ]:
            cfg = LLMConfig(provider=prov, api_key=key_val)
            client = LLMProviderClient(cfg, transport_fn=_mock_provider_transport)
            url, hdrs, body, _ = client.build_request(
                "System", "Extract table", images_b64=[("image/png", tiny_png_b64)], use_vision_model=True
            )
            self.assertIn(expect_url, url)
            if expect_hdr:
                self.assertIn(expect_hdr, hdrs)
            if prov == "gemini":
                self.assertIn("inline_data", json.dumps(body))
            elif prov == "claude":
                self.assertIn('"type": "image"', json.dumps(body))
            else:
                self.assertIn("image_url", json.dumps(body))
            probe = client.test_connection()
            self.assertTrue(probe["ok"])

        # 2) CypherApplicationService with mock transport: API key masking & persistence
        srv_llm = CypherApplicationService(
            os.path.join(self.tmp.name, "llm_vault.db"),
            self.sample_dir,
            seed_demo=True,
            llm_transport_fn=_mock_provider_transport,
        )
        updated_cfg = srv_llm.update_llm_config({
            "execution_mode": "both",
            "provider": "claude",
            "model": "claude-sonnet-4-20250514",
            "vision_model": "claude-sonnet-4-20250514",
            "api_key": "sk-ant-secret-key-9876",
        })
        self.assertEqual(updated_cfg["provider"], "claude")
        self.assertTrue(updated_cfg["has_api_key"])
        self.assertNotIn("sk-ant-secret-key-9876", json.dumps(updated_cfg))
        self.assertTrue(updated_cfg["masked_api_key"].endswith("9876"))

        # Verify persistence across service reload
        srv_reloaded = CypherApplicationService(
            os.path.join(self.tmp.name, "llm_vault.db"),
            self.sample_dir,
            seed_demo=False,
            llm_transport_fn=_mock_provider_transport,
        )
        self.assertEqual(srv_reloaded.llm_config.provider, "claude")
        self.assertEqual(srv_reloaded.llm_config.active_api_key(), "sk-ant-secret-key-9876")

        # 3) Test runtime mode switching ('heuristics' vs 'both' vs 'llm')
        case = srv_llm.create_case("Hybrid Agent Case", "cypher_edge", "Audit Co.")
        cid = case["case_id"]

        # Unreadable/blank PNG triggers Stage 1 Vision backup in 'both' mode, but 0 LLM calls in 'heuristics' mode
        blank_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        calls_before_heur = len(recorded_calls)
        res_heur = srv_llm.ingest_statements(
            cid, [("scan_heur.png", blank_png, None)], execution_mode="heuristics"
        )
        self.assertEqual(len(recorded_calls), calls_before_heur)
        self.assertEqual(res_heur["agent_telemetry"]["execution_mode"], "heuristics")
        self.assertEqual(res_heur["agent_telemetry"]["stages_invoked"], [])

        # Stage 2 repair trigger CSV (missing holder + broken running balance on row 1)
        broken_csv = "\n".join([
            "Date,Narration,Debit,Credit,Balance",
            "01/04/2026,OPENING DEPOSIT,,10000.00,10000.00",
            "02/04/2026,UNKNOWN_OPAQUE_WIRE_XYZ,,1500.00,8500.00",
        ]).encode("utf-8")

        res_both = srv_llm.ingest_statements(
            cid,
            [("scan_vision.png", blank_png, None), ("broken_reconcile.csv", broken_csv, None)],
            execution_mode="both",
        )
        tel_both = res_both["agent_telemetry"]
        self.assertEqual(tel_both["execution_mode"], "both")
        self.assertIn(1, tel_both["stages_invoked"])
        self.assertIn(2, tel_both["stages_invoked"])
        self.assertIn(3, tel_both["stages_invoked"])
        self.assertIn(4, tel_both["stages_invoked"])

        ov = srv_llm.get_case_overview(cid)
        healed_stmt = next(s for s in ov["statements"] if s["source_filename"] == "broken_reconcile.csv")
        self.assertEqual(healed_stmt["balance_mismatch_count"], 0)
        self.assertEqual(healed_stmt["account_holder_name"], "MEERA TRADERS")
        vision_stmt = next(s for s in ov["statements"] if s["source_filename"] == "scan_vision.png")
        self.assertEqual(vision_stmt["total_transactions"], 2)
        self.assertEqual(vision_stmt["account_holder_name"], "RAHUL KUMAR SHARMA")

        # Test 'llm' primary execution mode & run_case_llm_agents
        case_llm = srv_llm.create_case("Pure LLM Agents Case", "cypher_edge", "Audit Co.")
        res_llm = srv_llm.ingest_statements(
            case_llm["case_id"],
            [("vision_primary.png", blank_png, None)],
            execution_mode="llm",
        )
        self.assertEqual(res_llm["agent_telemetry"]["stages_invoked"], [1, 2, 3, 4])

        run_existing = srv_llm.run_case_llm_agents(case_llm["case_id"], execution_mode="llm", stages=[2, 3, 4])
        self.assertEqual(run_existing["agent_telemetry"]["execution_mode"], "llm")
        self.assertEqual(run_existing["agent_telemetry"]["stages_invoked"], [2, 3, 4])

        # 4) Verify HTTP routes + UI contemporary artifacts presence
        import http.client
        from http.server import ThreadingHTTPServer
        import threading

        ui_dir = os.path.join(SRC_DIR, "cyphersol", "ui")
        handler_cls = create_http_handler(srv_llm, ui_dir)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            tok = {"X-CypherSOL-Token": handler_cls.api_token}
            conn.request("GET", "/api/llm/config", headers=tok)
            r_cfg = conn.getresponse()
            self.assertEqual(r_cfg.status, 200)
            cfg_json = json.loads(r_cfg.read().decode("utf-8"))
            self.assertIn("provider_catalog", cfg_json)
            self.assertIn("agent_definitions", cfg_json)
            self.assertNotIn("sk-ant-secret-key-9876", json.dumps(cfg_json))

            conn.request("POST", "/api/llm/test", body=json.dumps({"provider": "gemini", "api_key": "AIzaMock"}), headers={"Content-Type": "application/json", **tok})
            r_test = conn.getresponse()
            self.assertEqual(r_test.status, 200)
            self.assertTrue(json.loads(r_test.read().decode("utf-8"))["ok"])

            conn.request("GET", "/")
            r_html = conn.getresponse()
            self.assertEqual(r_html.status, 200)
            html_body = r_html.read().decode("utf-8")
            # P3: the runtime-mode / AI-settings UI moved out of static markup into ES modules (Settings view,
            # shared EXEC_MODES constant); the shell only loads the module entry.
            self.assertIn('<script type="module" src="js/app.js"></script>', html_body)
            conn.request("GET", "/js/core/constants.js")
            r_js = conn.getresponse()
            self.assertEqual(r_js.status, 200)
            self.assertTrue(r_js.getheader("Content-Type", "").startswith("text/javascript"))
            js_body = r_js.read().decode("utf-8")
            for mode in ('value: "heuristics"', 'value: "both"', 'value: "llm"'):
                self.assertIn(mode, js_body)
            conn.close()
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
