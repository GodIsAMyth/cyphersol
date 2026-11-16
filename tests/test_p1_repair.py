"""Repair-agent regression tests: the open findings of the P1 review (layering blow-up, cheque party cleaning,
truncated business forms, account type / stated period, draw field-map validation, entity undo dependencies,
reviewed-pair filtering, merged canonical name, ref-column UTR, loop default, Tally map reset, HTTP numeric
hardening, pikepdf argv fallback, OCR provenance, draw-tool password hints) plus static UI checks."""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import sys
import tempfile
import threading
import time
import unittest
from decimal import Decimal
from http.server import ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from unittest import mock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.edge.itr_compliance import generate_itr_compliance_report  # noqa: E402
from cyphersol.ingestion import pdf_parser  # noqa: E402
from cyphersol.ingestion.anomaly_gate import missing_statement_metadata  # noqa: E402
from cyphersol.ingestion.parser import detect_bank_and_metadata, parse_statement_bytes  # noqa: E402
from cyphersol.models import (  # noqa: E402
    BankAccountStatement, ConflictError, NormalizedTransaction, TxDirection,
)
from cyphersol.nlp.entity_resolver import compute_name_similarity, resolve_case_entities  # noqa: E402
from cyphersol.nlp.narration_parser import parse_narration, utr_from_ref_column  # noqa: E402
from cyphersol.server import create_http_handler  # noqa: E402
from cyphersol.service import CypherApplicationService, validate_draw_column_names  # noqa: E402
from cyphersol.trace import loop_detector  # noqa: E402
from cyphersol.trace.layering import build_layering_tree, expand_layering_node  # noqa: E402

SCRATCH = "/private/tmp/claude-501/cyphersol-p1"
UI_DIR = os.path.join(SRC_DIR, "cyphersol", "ui")
TRACE_CASE = "CASE_TRACE_EOW_041"
ENV_KEYS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CYPHERSOL_ALLOW_CLOUD_LLM",
            "CYPHERSOL_EXECUTION_MODE", "CYPHERSOL_TALLY_HOSTS", "CYPHERSOL_LLM_PROVIDER")


def _read(rel: str) -> str:
    with open(os.path.join(UI_DIR, rel), encoding="utf-8") as fh:
        return fh.read()


# ------------------------------------------------------------------------------------------------- layering
def _dense_graph(n: int, days: int):
    names = ["QWERTY", "ZXCVBN", "POIUYT", "LKJHGF", "MNBVCX", "ASDFGH", "YTREWQ", "HGFDSA", "BVCXZM", "UIOPLK",
             "JKLMNO", "RSTUVW"][:n]
    stmts, txs, k = [], [], 0
    for i, nm in enumerate(names):
        stmts.append(BankAccountStatement(
            statement_id=f"s{i}", case_id="c", source_filename="f", sha256_hash=str(i), bank_code="X",
            bank_name="X", account_number=f"1000000{i:04d}", account_holder_name=nm, period_start="2025-01-01",
            period_end="2025-12-31", opening_balance=Decimal(0), closing_balance=Decimal(0), total_transactions=0))
    for d in range(days):
        date = f"2025-01-{d + 1:02d}"
        for i, a in enumerate(names):
            for j, b in enumerate(names):
                if i == j:
                    continue
                for sid, direction, party, acc in ((f"s{i}", TxDirection.DEBIT, b, j), (f"s{j}", TxDirection.CREDIT, a, i)):
                    k += 1
                    amt = Decimal("1000")
                    txs.append(NormalizedTransaction(
                        tx_id=f"t{k}", statement_id=sid, case_id="c", row_index=k, txn_date=date,
                        raw_narration=f"NEFT {party}", payment_channel="NEFT", direction=direction,
                        debit=amt if direction == TxDirection.DEBIT else Decimal(0),
                        credit=amt if direction == TxDirection.CREDIT else Decimal(0), running_balance=Decimal(0),
                        category_id=1, category_name="x", categorization_source="r", confidence_score=1.0,
                        extracted_counterparty=party, counterparty_vpa_or_acc=f"1000000{acc:04d}"))
    ents, _ = resolve_case_entities("c", stmts, txs)
    src = next(e for e in ents if e.canonical_name == names[0])
    return stmts, txs, ents, src


class LayeringAllModeBudgetTests(unittest.TestCase):
    """High: mode='all' followed every simple path (157 s / 4 GB at depth 8 on 12 dense holders)."""

    def test_dense_all_mode_is_bounded_and_truncated(self) -> None:
        stmts, txs, ents, src = _dense_graph(12, 2)
        t0 = time.perf_counter()
        tree = build_layering_tree(stmts, txs, ents, src.entity_id, window_days=14, max_depth=8, mode="all")
        self.assertLess(time.perf_counter() - t0, 20.0)
        self.assertTrue(tree["truncated"])

    def test_deep_expand_in_all_mode_is_fast(self) -> None:
        stmts, txs, ents, src = _dense_graph(10, 2)
        by_name = {e.canonical_name: e.entity_id for e in ents}
        path = [src.entity_id] + [by_name[n] for n in ("ZXCVBN", "POIUYT", "LKJHGF", "MNBVCX", "ASDFGH", "YTREWQ")]
        t0 = time.perf_counter()
        res = expand_layering_node(stmts, txs, ents, src.entity_id, path, window_days=14, mode="all")
        self.assertLess(time.perf_counter() - t0, 20.0)
        self.assertTrue(res["children"])

    def test_small_tree_unchanged(self) -> None:
        stmts, txs, ents, src = _dense_graph(4, 1)
        tree = build_layering_tree(stmts, txs, ents, src.entity_id, window_days=14, max_depth=3, mode="all")
        self.assertFalse(tree["truncated"])
        self.assertFalse(tree["work_budget_exhausted"])


# ---------------------------------------------------------------------------------------------- narration
class ChequePartyCleaningTests(unittest.TestCase):
    def test_cheques_without_payee_stay_unresolved(self) -> None:
        for narr in ("CHQ NO 123456 CLG ICICI", "CHQ NO 654321 CLG ICICI", "CLG CHQ 445566",
                     "BY CLG 778899 SBI MUMBAI", "CHQ DEP 000123 HDFC"):
            r = parse_narration(narr)
            self.assertIsNone(r.counterparty_name, narr)
            self.assertIsNone(r.utr_number, f"{narr}: a cheque number is not a UTR")
            self.assertTrue(r.cheque_number, narr)

    def test_cheque_payee_and_tag_residue(self) -> None:
        self.assertEqual(parse_narration("CHQ 123456 PAID TO RAMESH KUMAR").counterparty_name, "RAMESH KUMAR")
        self.assertEqual(parse_narration("CHQ PAID-MICR CTS-MUMBAI-ACME LTD").counterparty_name, "ACME LTD")
        self.assertEqual(parse_narration("CHQ DEP 000123 HDFC BANK SHARMA STEELS").counterparty_name, "SHARMA STEELS")
        self.assertEqual(parse_narration("NEFT/CR/BHARAT TRADERS INVOICE").counterparty_name, "BHARAT TRADERS")
        self.assertNotEqual(
            parse_narration("UPI/DR/123456789012/POS 123456 MERCHANT/YESB/shopx@ybl/pay").counterparty_name,
            "POS MERCHANT")

    def test_utr_from_ref_column(self) -> None:
        self.assertEqual(utr_from_ref_column("UTR4093001", "NEFT"), "UTR4093001")
        self.assertEqual(utr_from_ref_column("409311223344", "IMPS"), "409311223344")
        self.assertEqual(utr_from_ref_column("HDFCN52024041512345678", "NEFT"), "HDFCN52024041512345678")
        self.assertIsNone(utr_from_ref_column("123456", "NEFT"))          # cheque-number shape
        self.assertIsNone(utr_from_ref_column("UTR4093001", "CHEQUE"))
        self.assertIsNone(utr_from_ref_column("LOOP01", "NEFT"))
        self.assertIsNone(utr_from_ref_column("HDFC0000123", "NEFT"))     # IFSC


class TruncatedBusinessFormTests(unittest.TestCase):
    def test_ent_matches_enterprises(self) -> None:
        self.assertGreaterEqual(compute_name_similarity("SHREE BALAJI ENT", "SHREE BALAJI ENTERPRISES"), 0.85)
        self.assertGreaterEqual(compute_name_similarity("BHARAT TRDRS", "BHARAT TRADERS"), 0.85)
        self.assertLess(compute_name_similarity("BHARAT TRADERS", "BHARAT ENT"), 0.85)  # type clash still penalised


# ---------------------------------------------------------------------------------------------- ingestion
_CSV_HEAD = ("Account Holder: MEERA TRADERS\nAccount Number: 123456789012\n"
             "Statement Period: 01/04/2024 to 30/04/2024\nDate,Narration,Debit,Credit,Balance\n")
_CSV_ROWS = ("02/04/2024,NEFT/CR/RAMESH,0,1000,11000\n03/04/2024,UPI/DR/123456789012/SHOP/x@ybl,500,0,10500\n"
             "05/04/2024,NEFT/CR/SURESH,0,500,11000\n")


class AccountTypeAndPeriodTests(unittest.TestCase):
    def test_unstated_type_is_unknown_and_reported(self) -> None:
        stmt, _txns, _meta = parse_statement_bytes("C1_MEERA-TRADERS_SBI_123456789012.csv",
                                                   (_CSV_HEAD + _CSV_ROWS).encode(), "C1")
        self.assertIsNone(stmt.account_type)
        self.assertIn("account_type", missing_statement_metadata(stmt))
        _b, meta = detect_bank_and_metadata("Account Type: Current Account\n", "x.csv")
        self.assertEqual(meta["account_type"], "Current/OD")

    def test_stated_period_bounds_the_statement(self) -> None:
        stmt, _txns, meta = parse_statement_bytes("C1_MEERA-TRADERS_SBI_123456789012.csv",
                                                  (_CSV_HEAD + _CSV_ROWS).encode(), "C1")
        self.assertEqual((stmt.period_start, stmt.period_end), ("2024-04-01", "2024-04-30"))
        self.assertEqual(meta["transaction_period"], {"start": "2024-04-02", "end": "2024-04-05"})

    def test_itr_notes_unknown_type_accounts(self) -> None:
        rows = "".join(f"{d:02d}/04/2024,CASH DEPOSIT BY SELF,0,450000,{450000 * (d - 1)}\n" for d in range(2, 6))
        stmt, txns, _meta = parse_statement_bytes("C1_MEERA-TRADERS_SBI_123456789012.csv",
                                                  (_CSV_HEAD + rows).encode(), "C1")
        for t in txns:
            t.category_id = 50  # cash deposit
        rep = generate_itr_compliance_report(txns, statements=[stmt])
        self.assertFalse(rep["cash_summary"]["account_types_known"])
        self.assertEqual(len(rep["cash_summary"]["unknown_type_accounts"]), 1)
        sft = next((f for f in rep["statutory_red_flags"] if f["section"].startswith("SFT-003")), None)
        self.assertIsNotNone(sft)
        self.assertIn("Account type not stated", sft["detail"])
        check = next(c for c in rep["checks_run"] if c["id"] == "SFT003")
        self.assertTrue(check["caveats"])


class DrawFieldMapTests(unittest.TestCase):
    def test_rejects_bad_field_maps(self) -> None:
        for names, splits in (
            (["Txn Date", "Narration", "Ref", "Debit", "Credit", "Balance"], 5),
            (["narration", "ref", "debit", "credit", "balance", "x"], 5),
            (["date", "date", "balance"], 2),
            (["date", "narration", "foo"], 2),
            (["date", "narration", "balance"], 2),
            (["date", "narration", "debit"], 3),
        ):
            with self.assertRaises(ValueError, msg=names):
                validate_draw_column_names(names, splits)
        self.assertEqual(validate_draw_column_names(["Date", "narration", "ignore", "ignore", "amount"], 4),
                         ["date", "narration", "ignore", "ignore", "amount"])


# ------------------------------------------------------------------------------------------- service + HTTP
class _ServiceBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.makedirs(SCRATCH, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.env_patch = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in ENV_KEYS},
                                        clear=True)
        cls.env_patch.start()
        cls.svc = CypherApplicationService(os.path.join(cls.tmp.name, "v.db"), os.path.join(cls.tmp.name, "s"),
                                           seed_demo=True)
        cls.token = "repair-token-" + "r" * 28
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_http_handler(cls.svc, UI_DIR, api_token=cls.token))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env_patch.stop()
        cls.tmp.cleanup()

    def req(self, method: str, path: str, body: Any = None) -> Tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"X-CypherSOL-Token": self.token}
        if body is not None:
            if not isinstance(body, (bytes, str)):
                body = json.dumps(body)
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, data

    def ents(self):
        return {e.canonical_name: e for e in self.svc.vault.get_entities(TRACE_CASE)}


class EntityDecisionRepairTests(_ServiceBase):
    def test_undo_refused_while_later_merge_depends_on_it(self) -> None:
        e = self.ents()
        z, s, c = (e["ZENITH CONSULTANTS"].entity_id, e["SUDHAKAR SHELL CREDITS"].entity_id,
                   e["COINDCX CRYPTO EXCHANGE"].entity_id)
        d1 = self.svc.merge_entities(TRACE_CASE, z, s)["decision"]["decision_id"]
        d2 = self.svc.merge_entities(TRACE_CASE, s, c)["decision"]["decision_id"]
        with self.assertRaises(ConflictError):
            self.svc.undo_entity_decision(TRACE_CASE, d1)
        st, _ = self.req("POST", f"/api/cases/{TRACE_CASE}/entities/undo", {"decision_id": d1})
        self.assertIn(st, (404, 409))
        self.svc.undo_entity_decision(TRACE_CASE, d2)
        res = self.svc.undo_entity_decision(TRACE_CASE, d1)
        self.assertTrue(res["changed"])
        names = {x["canonical_name"] for x in res["entities"]}
        self.assertTrue({"ZENITH CONSULTANTS", "SUDHAKAR SHELL CREDITS", "COINDCX CRYPTO EXCHANGE"} <= names)

    def test_merged_entity_keeps_target_name(self) -> None:
        e = self.ents()
        src = next(x for x in e.values() if not x.has_statement and not getattr(x, "is_system", False)
                   and not x.canonical_name.startswith("UNRESOLVED"))
        tgt = next(x for x in e.values() if not x.has_statement and x.entity_id != src.entity_id
                   and len(x.canonical_name) < len(src.canonical_name) and not x.canonical_name.startswith("UNRESOLVED")
                   and not getattr(x, "is_system", False))
        res = self.svc.merge_entities(TRACE_CASE, src.entity_id, tgt.entity_id)
        merged = next(x for x in res["entities"] if x["entity_id"] == tgt.entity_id)
        self.assertEqual(merged["canonical_name"], tgt.canonical_name)
        self.svc.undo_entity_decision(TRACE_CASE)

    def test_rejected_pair_leaves_review_candidates(self) -> None:
        prev = self.svc.preview_entity_clusters(TRACE_CASE)
        if not prev["candidates"]:
            self.skipTest("no near-merge candidates in the demo case")
        c = prev["candidates"][0]
        self.svc.reject_entity_merge(TRACE_CASE, c["entity_a_id"], c["entity_b_id"])
        after = self.svc.preview_entity_clusters(TRACE_CASE)
        pairs = {frozenset((x["entity_a_id"], x["entity_b_id"])) for x in after["candidates"]}
        self.assertNotIn(frozenset((c["entity_a_id"], c["entity_b_id"])), pairs)
        self.svc.undo_entity_decision(TRACE_CASE)


class MiscServiceRepairTests(_ServiceBase):
    def test_tally_map_reset_reports_default(self) -> None:
        cid = self.svc.list_cases()[0]["case_id"] if hasattr(self.svc, "list_cases") else TRACE_CASE
        cur = self.svc.get_tally_ledger_map(cid)["ledger_map"]
        self.svc.set_tally_ledger_map(cid, {**cur, "voucher_prefix": "ZZ"})
        self.assertFalse(self.svc.get_tally_ledger_map(cid)["is_default"])
        out = self.svc.set_tally_ledger_map(cid, None)
        self.assertTrue(out["is_default"])
        self.assertTrue(self.svc.get_tally_ledger_map(cid)["is_default"])

    def test_loop_default_is_eight(self) -> None:
        self.assertEqual(loop_detector.DEFAULT_CYCLE_LENGTH, 8)
        self.assertEqual(self.svc.get_loops_report(TRACE_CASE)["max_cycle_length"], 8)

    def test_draw_template_field_map_is_validated(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.save_draw_template("X", "bad", [0, 0, 1, 1], [0.2, 0.5],
                                        ["Txn Date", "Narration", "Balance"])
        st, _ = self.req("POST", "/api/draw-templates", {
            "bank_code": "X", "template_name": "t", "table_bbox": [0, 0, 1, 1], "column_splits": [0.3, 0.6],
            "column_names": ["date", "date", "balance"]})
        self.assertEqual(st, 400)

    def test_ocr_corrections_in_provenance(self) -> None:
        self.svc._audit(TRACE_CASE, "OCR_AUTOCORRECT", {
            "statement_id": "S", "filename": "scan.pdf",
            "corrections": [{"tx_id": "TXOCR1", "row_index": 3, "before": {"debit": "10.00"},
                             "after": {"debit": "100.00"}}]})
        prov = self.svc.get_statement_provenance(TRACE_CASE)
        self.assertEqual(prov["ocr_corrections"]["TXOCR1"]["after"], {"debit": "100.00"})

    def test_draw_password_resolved_from_hints(self) -> None:
        with mock.patch("cyphersol.service.resolve_pdf_password",
                        return_value={"status": "ok", "password": "POOJ1503", "label": "x", "tried": 3}) as rp:
            pw = self.svc._draw_password(b"%PDF-1.4", None, {"name": "POOJAN VIG", "dob": "15/03/1990"})
        self.assertEqual(pw, "POOJ1503")
        self.assertTrue(rp.called)
        self.assertEqual(self.svc._draw_password(b"%PDF", "abc", None), "abc")


class HttpNumericHardeningTests(_ServiceBase):
    def test_non_finite_and_huge_numbers_are_400(self) -> None:
        e = list(self.ents().values())
        a, b = e[0].entity_id, e[1].entity_id
        st, data = self.req("GET", f"/api/cases/{TRACE_CASE}/trace/bidirectional?a={a}&b={b}&rapid_window_hours=nan")
        self.assertEqual(st, 400, data)
        st, _ = self.req("GET", f"/api/cases/{TRACE_CASE}/trace/bidirectional?a={a}&b={b}&round_trip_window_hours=inf")
        self.assertEqual(st, 400)
        st, _ = self.req("GET", f"/api/cases/{TRACE_CASE}/transactions?offset=99999999999999999999")
        self.assertEqual(st, 400)
        st, _ = self.req("POST", "/api/draw/page-image", '{"content_b64": "JVBERg==", "dpi": Infinity}')
        self.assertEqual(st, 400)

    def test_responses_are_strict_json(self) -> None:
        st, data = self.req("GET", f"/api/cases/{TRACE_CASE}/trace/loops")
        self.assertEqual(st, 200)
        json.loads(data.decode("utf-8"), parse_constant=lambda c: self.fail(f"bare {c} in JSON"))


class PikepdfArgvFallbackTests(unittest.TestCase):
    def test_pikepdf_non_password_error_never_falls_back_to_argv(self) -> None:
        try:
            import pikepdf  # noqa: F401
        except ImportError:
            self.skipTest("pikepdf not installed")

        class Boom(Exception):
            pass

        with mock.patch("pikepdf.open", side_effect=Boom("bad xref")):
            dec, status = pdf_parser._decrypt_in_process(b"%PDF-1.7 encrypted", "secret")
        self.assertIsNone(dec)
        self.assertEqual(status, "corrupt")


# ---------------------------------------------------------------------------------------------- UI (static)
class UiRepairStaticTests(unittest.TestCase):
    def test_mule_formula_wraps(self) -> None:
        css = _read("styles/views/risk.css")
        self.assertRegex(css, r'v-mules-params th\[scope="row"\],\s*\n?\s*\.v-mules \.v-mules-params \.v-mules-formula \{ white-space: normal')

    def test_datagrid_overlay_has_room(self) -> None:
        self.assertIn('classList.toggle("dg--overlay"', _read("js/components/datagrid.js"))
        self.assertIn(".dg--overlay .dg__scroll", _read("styles/components.css"))

    def test_draw_tool_hints_and_locked_reset(self) -> None:
        js = _read("js/views/ingest.js")
        self.assertIn("password_hints: draw.hints", js)
        self.assertRegex(js, r"if \(res\.is_encrypted\) \{[\s\S]{0,400}drawWorkEmpty\(\)")
        self.assertIn("Map one column to Txn date.", js)

    def test_layering_root_credit_labels(self) -> None:
        js = _read("js/views/layering.js")
        self.assertIn('label: "Root credit"', js)
        self.assertIn("shareBase", js)

    def test_loops_default_and_tally_prefix(self) -> None:
        self.assertIn("max_cycle_length: 8", _read("js/views/loops.js"))
        tm = _read("js/views/tally-map.js")
        self.assertIn("PREFIX_RE", tm)
        self.assertIn('setAttribute("aria-invalid", "true")', tm)

    def test_bidirectional_styles_moved(self) -> None:
        self.assertIn('export const styles = ["views/bidirectional.css"];', _read("js/views/bidirectional.js"))
        self.assertNotIn(".v-bidirectional", _read("styles/views/network.css"))
        self.assertIn("Net sent A → B", _read("js/views/bidirectional.js"))

    def test_no_inline_handlers_or_styles_added(self) -> None:
        for rel in ("js/views/transactions.js", "js/views/tally-map.js", "js/views/ingest.js", "js/views/loan.js",
                    "js/views/layering.js", "js/views/bidirectional.js"):
            src = _read(rel)
            self.assertIsNone(re.search(r"\son(click|input|change|load)=", src), rel)
            self.assertNotIn(' style="', src, rel)


if __name__ == "__main__":
    unittest.main()
