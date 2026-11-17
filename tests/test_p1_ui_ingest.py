"""P1 UI wiring for the Ingest view (PDF-13, PDF-21, PDF-23, ING-10/11/16/17 surfacing).

Static checks over src/cyphersol/ui/js/views/ingest.js (the UI has no JS test runner), a node syntax check when
node is installed, and an HTTP round trip of the endpoints the view calls with the exact payload shapes it sends.
"""

from __future__ import annotations

import base64
import http.client
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for p in (SRC_DIR, TESTS_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

from cyphersol.server import create_http_handler  # noqa: E402
from cyphersol.service import CypherApplicationService  # noqa: E402

UI_DIR = os.path.join(SRC_DIR, "cyphersol", "ui")
INGEST_JS = os.path.join(UI_DIR, "js", "views", "ingest.js")
SETTINGS_JS = os.path.join(UI_DIR, "js", "views", "settings.js")
LEDGER_JS = os.path.join(UI_DIR, "js", "views", "settings-ledger.js")
CASES_CSS = os.path.join(UI_DIR, "styles", "views", "cases.css")
SCRATCH_DIR = os.path.join(tempfile.gettempdir(), "claude-501", "cyphersol-p1")
ENV_KEYS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CYPHERSOL_ALLOW_CLOUD_LLM",
            "CYPHERSOL_EXECUTION_MODE", "CYPHERSOL_TALLY_HOSTS", "CYPHERSOL_LLM_PROVIDER")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class IngestViewStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _read(INGEST_JS)

    # ---- PDF-13: per-file passwords, holder-detail hints, candidate check (never stored)
    def test_password_check_endpoint_and_hints_are_wired(self):
        self.assertIn('"/api/pdf/password-check"', self.src)
        self.assertIn("password_hints", self.src)
        # per-file hints go into each files[] entry of the upload payload
        self.assertRegex(self.src, r"payload\.push\(\{[^}]*password_hints")
        for key in ("name", "dob", "pan", "account_number", "customer_id", "mobile", "bank", "passwords"):
            self.assertRegex(self.src, rf'key: "{key}"', key)

    def test_hints_and_passwords_never_persisted(self):
        self.assertNotIn("localStorage", self.src)
        self.assertNotIn("sessionStorage", self.src)
        self.assertNotRegex(self.src, r"lsSet\(")
        # hints and password are dropped from memory once the file is ingested
        self.assertRegex(self.src, r"q\.hints = null")
        self.assertRegex(self.src, r'q\.password = ""')

    def test_password_check_result_is_explained(self):
        for status in ('"ok"', '"no_match"', '"not_encrypted"', '"unavailable"'):
            self.assertIn(status, self.src)
        self.assertIn("password_label", self.src)

    # ---- PDF-21: page raster + drawn frame; PDF-23: right-edge anchoring of numeric tokens in the preview
    def test_draw_tool_uses_page_raster(self):
        self.assertIn('"/api/draw/page-image"', self.src)
        self.assertIn('createElementNS(SVG_NS, "image")', self.src)
        self.assertIn("data:image/png;base64,", self.src)
        self.assertIn("renderer_unavailable", self.src)

    def test_draw_tool_can_draw_a_new_frame(self):
        self.assertIn("newFrame", self.src)

    def test_column_fields_include_value_date_amount_and_drcr(self):
        m = re.search(r"const COLUMN_FIELDS = \[(.*?)\];", self.src, re.S)
        self.assertIsNotNone(m)
        values = re.findall(r'value: "([a-z_]+)"', m.group(1))
        for v in ("date", "value_date", "narration", "ref", "debit", "credit", "amount", "type", "balance"):
            self.assertIn(v, values)

    def test_live_preview_anchors_numeric_tokens_by_right_edge(self):
        self.assertIn("x_end", self.src)
        self.assertIn("RIGHT_NUMERIC", self.src)

    # ---- anomaly gate (ING-10/11/16/17) and cross-checks surfaced per file
    def test_gate_flags_surface_in_per_file_results(self):
        for key in ("opening_balance_mismatch", "closing_balance_mismatch", "balance_check_skipped",
                    "zero_or_dual_amount", "outside_statement_period", "ocr_autocorrected", "llm_repaired",
                    "date_unparseable", "low_confidence_pages", "ocr_rejected_pages", "ocr_confidence_by_page",
                    "filename_crosscheck", "period_coverage", "pan"):
            self.assertIn(key, self.src, key)

    # ---- CSP / escaping rules from P3
    def test_csp_rules(self):
        self.assertNotRegex(self.src, r"\son[a-z]+=")
        self.assertNotRegex(self.src, r'\sstyle="')
        self.assertNotIn("innerHTML =", self.src)
        self.assertNotIn("fetch(", self.src)
        self.assertNotRegex(self.src, r"sha256_hash[^;\n]*\.slice\(")

    def test_settings_points_to_case_ledger_map(self):
        self.assertIn("ledger", _read(SETTINGS_JS).lower())

    # ---- Tally ledger map editor (Settings, per case)
    def test_settings_mounts_ledger_map_editor(self):
        settings = _read(SETTINGS_JS)
        self.assertIn('from "./settings-ledger.js"', settings)
        self.assertIn('mountLedgerMap(slot("ledger"), ctx)', settings)
        src = _read(LEDGER_JS)
        self.assertIn('"edge/tally-map"', src)
        self.assertIn("{ reset: true }", src)
        self.assertIn("{ ledger_map }", src)
        for key in ("category_groups", "category_ledgers", "custom_groups", "counterparty_ledgers",
                    "use_trade_vouchers", "voucher_prefix", "sales_categories", "purchase_categories", "company_name"):
            self.assertIn(key, src, key)
        self.assertNotIn("localStorage", src)
        self.assertNotRegex(src, r"\son[a-z]+=")
        self.assertNotRegex(src, r'\sstyle="')
        self.assertNotIn("innerHTML", src)
        self.assertIn(".v-settings-lm__sec", _read(CASES_CSS))

    def test_ledger_editor_group_list_matches_backend(self):
        from cyphersol.edge.tally_exporter import PREDEFINED_TALLY_GROUPS

        m = re.search(r"export const TALLY_GROUPS = \[(.*?)\];", _read(LEDGER_JS), re.S)
        self.assertIsNotNone(m)
        self.assertEqual(set(re.findall(r'"([^"]+)"', m.group(1))), set(PREDEFINED_TALLY_GROUPS))

    def test_view_styles_exist(self):
        css = _read(CASES_CSS)
        for cls in (".v-ingest-raster", ".v-ingest-newframe", ".v-ingest-gate"):
            self.assertIn(cls, css)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_node_module_syntax(self):
        for path in (INGEST_JS, SETTINGS_JS, LEDGER_JS, os.path.join(UI_DIR, "js", "views", "cases.js")):
            with open(path, "rb") as f:
                res = subprocess.run([shutil.which("node"), "--input-type=module", "--check"], stdin=f,
                                     capture_output=True, timeout=60)
            self.assertEqual(res.returncode, 0, res.stderr.decode()[:500])


class IngestViewApiContractTests(unittest.TestCase):
    """The exact request shapes ingest.js sends are accepted by the server."""

    @classmethod
    def setUpClass(cls):
        os.makedirs(SCRATCH_DIR, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=SCRATCH_DIR)
        cls.env_patch = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in ENV_KEYS}, clear=True)
        cls.env_patch.start()
        cls.service = CypherApplicationService(
            os.path.join(cls.tmp.name, "ui_ingest.db"), os.path.join(cls.tmp.name, "samples"), seed_demo=False
        )
        cls.case_id = cls.service.create_case(title="UI ingest", mode="cypher_trace")["case_id"]
        cls.token = "p1-ui-ingest-token-" + "z" * 24
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_http_handler(cls.service, UI_DIR, api_token=cls.token))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env_patch.stop()
        cls.tmp.cleanup()

    def post(self, path, body):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.request("POST", path, body=json.dumps(body),
                     headers={"X-CypherSOL-Token": self.token, "Content-Type": "application/json"})
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, json.loads(data or b"{}")

    def _encrypted_pdf(self):
        from test_p0_pdf import _statement_page, build_pdf, encrypt_pdf_rc4  # noqa: E402
        from test_p1_pdf import HEADER, ROWS, XS  # noqa: E402

        plain = build_pdf([{"content": _statement_page(ROWS, HEADER, XS)}])
        return encrypt_pdf_rc4(plain, "pooj0703")

    def test_password_check_with_ui_hint_shape(self):
        enc = self._encrypted_pdf()
        hints = {"name": "Poojan Vig", "dob": "1990-03-07", "passwords": ["nope-1"]}  # <input type=date> value
        status, res = self.post("/api/pdf/password-check",
                                {"content_b64": base64.b64encode(enc).decode(), "password": None, "password_hints": hints})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["label"].startswith("name4_"))
        self.assertNotIn("pooj0703", json.dumps(res))

    def test_upload_with_per_file_hints_reports_formula(self):
        enc = self._encrypted_pdf()
        body = {
            "files": [{"filename": "locked.pdf", "content_b64": base64.b64encode(enc).decode(), "password": None,
                       "password_hints": {"name": "Poojan Vig", "dob": "1990-03-07"}}],
            "template_id": None, "execution_mode": "heuristics", "on_duplicate": "reject",
        }
        status, res = self.post(f"/api/cases/{self.case_id}/upload", body)
        self.assertEqual(status, 200, res)
        rep = res["ingested"][0]
        self.assertEqual(rep["status"], "ok", rep)
        self.assertTrue(str(rep["password_label"]).startswith("name4_"))
        self.assertNotIn("pooj0703", json.dumps(res))
        for key in ("anomalies", "filename_crosscheck", "period_coverage", "low_ocr_confidence"):
            self.assertIn(key, rep)

    def test_ledger_map_save_and_reset_with_ui_shape(self):
        edge_case = self.service.create_case(title="UI ledger", mode="cypher_edge")["case_id"]
        path = f"/api/cases/{edge_case}/edge/tally-map"
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.request("GET", path, headers={"X-CypherSOL-Token": self.token})
        res = conn.getresponse()
        current = json.loads(res.read())
        conn.close()
        self.assertEqual(res.status, 200)
        ledger_map = dict(current["ledger_map"])
        ledger_map.update({
            "company_name": "Shree Test", "voucher_prefix": "CS", "use_trade_vouchers": True,
            "sales_categories": [2], "purchase_categories": [11, 12],
            "custom_groups": {"Office Rent Group": "Indirect Expenses"},
            "category_groups": {**ledger_map["category_groups"], "13": "Office Rent Group"},
            "category_ledgers": {"13": "Rent Paid"},
            "counterparty_ledgers": {"APEX SHELL PVT LTD": {"ledger": "Apex Shell (Debtor)", "group": "Sundry Debtors"}},
        })
        status, res = self.post(path, {"ledger_map": ledger_map})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["ledger_map"]["category_groups"]["13"], "Office Rent Group")
        self.assertEqual(res["ledger_map"]["counterparty_ledgers"]["APEX SHELL PVT LTD"]["ledger"], "Apex Shell (Debtor)")
        # a group the UI shows as "(unknown)" after its custom group was removed is rejected with a message
        bad = {**ledger_map, "custom_groups": {}}
        status, res = self.post(path, {"ledger_map": bad})
        self.assertEqual(status, 400)
        self.assertIn("Office Rent Group", res["error"])
        status, res = self.post(path, {"reset": True})
        self.assertEqual(status, 200, res)
        self.assertEqual(res["ledger_map"]["custom_groups"], {})

    def test_page_image_errors_are_coded(self):
        enc = self._encrypted_pdf()
        status, res = self.post("/api/draw/page-image", {"content_b64": base64.b64encode(enc).decode(), "page": 1, "dpi": 72})
        self.assertIn(status, (401, 503), res)
        self.assertIn(res.get("code"), ("password_required", "renderer_unavailable"))


if __name__ == "__main__":
    unittest.main()
