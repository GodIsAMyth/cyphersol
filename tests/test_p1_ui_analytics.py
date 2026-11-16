"""P1 UI wiring tests (ui-analytics group): the analytics views consume the P1 server payloads instead of
re-deriving them client-side.

* Static checks over the owned view modules (network, bidirectional, layering, loops, tally, itr, loan and the
  chart helpers): each one must read the P1 fields (TG-07/12, TG-05/06/18, TF-03/05/06, TF-13/14, EDGE-10/11/28,
  EDGE-15..20, EDGE-21/22/25/26) and must no longer carry the client-side work the server now does (Tally XML
  parsing, hard-coded ITR thresholds, client-only IFSC filtering, amount-blind round-trip counting).
* Contract checks against an in-process server with the seeded demo cases: every field the views read exists
  with the expected type, so a backend rename breaks this test rather than the UI silently.
* `node --check` over every owned module when Node is installed (CSP rules are covered by test_p3_ui_server).
"""

from __future__ import annotations

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
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.server import create_http_handler  # noqa: E402
from cyphersol.service import CypherApplicationService  # noqa: E402

UI_DIR = os.path.join(SRC_DIR, "cyphersol", "ui")
VIEWS = os.path.join(UI_DIR, "js", "views")
CHARTS = os.path.join(UI_DIR, "js", "charts")
SCRATCH_DIR = "/private/tmp/claude-501/cyphersol-p1"
ENV_KEYS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CYPHERSOL_ALLOW_CLOUD_LLM",
            "CYPHERSOL_EXECUTION_MODE", "CYPHERSOL_TALLY_HOSTS", "CYPHERSOL_LLM_PROVIDER")
EDGE_CASE = "CASE_EDGE_BALAJI_FY25"
TRACE_CASE = "CASE_TRACE_EOW_041"

OWNED = [os.path.join(VIEWS, f"{n}.js") for n in ("network", "bidirectional", "layering", "loops", "tally", "itr", "loan")]
OWNED += [os.path.join(CHARTS, n) for n in ("graph-canvas.js", "loop-diagram.js", "tree.js")]
OWNED += [os.path.join(VIEWS, "tally-map.js")]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _code(path):
    """Source without // line comments and /* */ blocks, so wiring checks cannot be satisfied by a comment."""
    src = _read(path)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


class StaticWiringTests(unittest.TestCase):
    def assertIn(self, member, container, msg=None):  # noqa: N802 - short failure text for large sources
        if isinstance(container, str):
            self.assertTrue(member in container, f"missing {member!r}" + (f" ({msg})" if msg else ""))
        else:
            super().assertIn(member, container, msg)

    def assertNotIn(self, member, container, msg=None):  # noqa: N802
        if isinstance(container, str):
            self.assertFalse(member in container, f"unexpected {member!r}" + (f" ({msg})" if msg else ""))
        else:
            super().assertNotIn(member, container, msg)

    def assertRegex(self, text, regex, msg=None):  # noqa: N802
        self.assertTrue(re.search(regex, text), f"pattern {regex!r} not found" + (f" ({msg})" if msg else ""))

    def test_network_colours_and_legend_come_from_payload(self):
        src = _code(os.path.join(VIEWS, "network.js"))
        self.assertIn("colour_keys", src)          # node fill keyed by server colour_keys (TG-07)
        self.assertIn("colour_by", src)            # legend from summary.colour_by
        self.assertIn("summary.filters", src.replace("?.", "."))  # applied filters echoed from the server (TG-12)
        # ifsc goes to the engine as a comma list; no client-side bank filtering of nodes
        self.assertRegex(src, r"ifsc[^\n]*split\(")
        self.assertNotIn("bankOf(n)).some", src)

    def test_bidirectional_uses_round_trips_and_server_ledgers(self):
        src = _code(os.path.join(VIEWS, "bidirectional.js"))
        for field in ("round_trips", "round_trip_stats", "ledger_a_to_b", "ledger_b_to_a", "cumulative_series",
                      "max_turnaround_hours", "rapid_reversal_count", "amount_tolerance", "round_trip_window_hours",
                      "rapid_window_hours", "date_from"):
            self.assertIn(field, src, field)
        self.assertRegex(src, r"(?i)mirror")      # mirror-dedup note
        self.assertNotIn("const RAPID_HOURS = 48", src)  # window is a parameter now

    def test_layering_lazy_expand_root_credit_and_branch_misc(self):
        src = _code(os.path.join(VIEWS, "layering.js"))
        self.assertIn("trace/layering/expand", src)   # TF-03 lazy drill-down
        self.assertIn("has_more_children", src)
        self.assertIn("root_tx_id", src)              # TF-05 root at one credit
        self.assertIn("path_key", src)                # TF-06 per-branch Misc
        self.assertRegex(src, r"trace/layering/misc[^\n]*\n?[^\n]*path")
        self.assertIn("misc_members", src)
        self.assertIn("misc_paths", src)
        tree = _code(os.path.join(CHARTS, "tree.js"))
        self.assertIn("canLoad", tree)
        self.assertIn("onLoad", tree)

    def test_loops_show_leakage_dates_utr_and_instances(self):
        src = _code(os.path.join(VIEWS, "loops.js"))
        for field in ("intermediary_leakage", "total_leakage", "instances", "instance_count", "max_instances_per_loop"):
            self.assertIn(field, src, field)
        dia = _code(os.path.join(CHARTS, "loop-diagram.js"))
        self.assertIn("utr", dia)                     # UTR on arcs (TF-13)
        self.assertIn("leakage", dia)                 # leakage on arcs
        self.assertRegex(dia, r"uid\(|markerId|instanceId")  # unique ids per diagram

    def test_tally_uses_structured_payload_and_ledger_map(self):
        src = _code(os.path.join(VIEWS, "tally.js")) + _code(os.path.join(VIEWS, "tally-map.js"))
        self.assertNotIn("DOMParser", src)            # EDGE-28: no client-side XML parsing
        self.assertNotIn("parseTallyXml", src)
        for field in ("vouchers", "ledgers", "xml_preview", "xml_sha256", "xml_bytes", "edge/tally-map",
                      "export/tally-xlsx", "company_name", "use_trade_vouchers", "sales_categories",
                      "purchase_categories", "category_groups", "counterparty_ledgers"):
            self.assertIn(field, src, field)
        self.assertIn("describeTallyPushResult", src)  # P3 contract

    def test_itr_has_no_hard_coded_thresholds(self):
        src = _code(os.path.join(VIEWS, "itr.js"))
        self.assertIn("checks_run", src)
        self.assertIn("check_id", src)
        self.assertIn("deductions", src)
        self.assertIn("reconciliation_waterfall", src)
        self.assertIn("thresholds", src)
        self.assertIn("export/itr-xlsx", src)
        self.assertNotIn("checksFor", src)
        for literal in ("1_000_000", "5_000_000", "30,000", "1,00,000", "2,40,000", "10,000", "2,00,000"):
            self.assertNotIn(literal, src, f"hard-coded threshold {literal}")

    def test_loan_coverage_penal_and_income_inputs(self):
        src = _code(os.path.join(VIEWS, "loan.js"))
        for field in ("consolidated_coverage", "full_coverage_days", "partial_coverage_days", "uncovered_days",
                      "accounts_covered", "coverage_gaps", "penal_events", "penal_event_breakdown", "overdrawn_days",
                      "cash_withdrawal_pct", "income_basis", "income_factor", "abb_factor", "income_override"):
            self.assertIn(field, src, field)

    @unittest.skipUnless(shutil.which("node") or os.path.exists("/usr/local/bin/node"), "node not installed")
    def test_owned_modules_parse_as_es_modules(self):
        node = shutil.which("node") or "/usr/local/bin/node"
        for path in OWNED:
            with open(path, "rb") as f:
                res = subprocess.run([node, "--input-type=module", "--check"], stdin=f, capture_output=True, timeout=30)
            self.assertEqual(res.returncode, 0, f"{path}: {res.stderr.decode()[:400]}")


class PayloadContractTests(unittest.TestCase):
    """The fields the views read, on the seeded demo cases."""

    @classmethod
    def setUpClass(cls) -> None:
        os.makedirs(SCRATCH_DIR, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=SCRATCH_DIR)
        cls.env_patch = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in ENV_KEYS}, clear=True)
        cls.env_patch.start()
        cls.service = CypherApplicationService(
            os.path.join(cls.tmp.name, "ua_vault.db"), os.path.join(cls.tmp.name, "samples"), seed_demo=True
        )
        cls.token = "p1-ua-test-token-" + "z" * 24
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_http_handler(cls.service, UI_DIR, api_token=cls.token))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env_patch.stop()
        cls.tmp.cleanup()

    def call(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"X-CypherSOL-Token": self.token}
        if body is not None:
            body = json.dumps(body)
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, (json.loads(data) if data[:1] in (b"{", b"[") else data)

    def get(self, path):
        status, data = self.call("GET", path)
        self.assertEqual(status, 200, f"{path}: {data}")
        return data

    def test_graph_colour_keys_and_filters(self):
        d = self.get(f"/api/cases/{TRACE_CASE}/trace/graph?ifsc=SBIN,HDFC")
        self.assertIn("filters", d["summary"])
        self.assertEqual(d["summary"]["filters"]["ifsc"], ["SBIN", "HDFC"])
        self.assertTrue({"role", "statement", "risk_tier", "betweenness"} <= set(d["summary"]["colour_by"]))
        for n in d["nodes"]:
            self.assertTrue({"role", "statement", "risk_tier", "betweenness"} <= set(n["colour_keys"]))

    def test_bidirectional_detail_fields(self):
        d = self.get(f"/api/cases/{TRACE_CASE}/trace/bidirectional?rapid_window_hours=24&amount_tolerance=0.2")
        s = d["selected_detail"]
        for k in ("round_trips", "round_trip_stats", "ledger_a_to_b", "ledger_b_to_a", "cumulative_series",
                  "min_turnaround_hours", "median_turnaround_hours", "max_turnaround_hours", "rapid_reversal_count",
                  "rapid_round_trip_count", "parameters", "transactions"):
            self.assertIn(k, s)
        self.assertEqual(s["parameters"]["rapid_window_hours"], 24.0)
        for row in s["ledger_a_to_b"] + s["ledger_b_to_a"]:
            self.assertIn("cumulative", row)
        self.assertTrue(all("direction_code" in t for t in s["transactions"]))

    def test_layering_expand_and_root_credit(self):
        d = self.get(f"/api/cases/{TRACE_CASE}/trace/layering?max_depth=1")
        root = d["root"]
        self.assertIn("source_key", d)
        child = next(c for c in root["children"] if c["has_more_children"])
        self.assertEqual(child["children"], [])
        e = self.get(f"/api/cases/{TRACE_CASE}/trace/layering/expand?path={child['path_key']}&max_depth=1")
        self.assertEqual(e["path_key"], child["path_key"])
        self.assertTrue(all(c["path_key"].startswith(child["path_key"] + ">") for c in e["children"]))
        # credits received by the source can root the tree
        stmts = self.get(f"/api/cases/{TRACE_CASE}/overview?tx_limit=0")
        ent = next(x for x in stmts["entities"] if x["entity_id"] == root["entity_id"])
        st = next((s for s in stmts["statements"] if s["account_number"] in ent["linked_account_numbers"]), None)
        if st is not None:
            tx = self.get(f"/api/cases/{TRACE_CASE}/transactions?statement_id={st['statement_id']}&direction=CR&limit=5")
            if tx["transactions"]:
                t = tx["transactions"][0]["tx_id"]
                r = self.get(f"/api/cases/{TRACE_CASE}/trace/layering?root_tx_id={t}")
                self.assertEqual(r["source_key"], f"tx:{t}")
                self.assertEqual(r["root_credit"]["tx_id"], t)

    def test_layering_branch_misc_round_trip(self):
        d = self.get(f"/api/cases/{TRACE_CASE}/trace/layering?max_depth=2")
        child = d["root"]["children"][0]
        status, res = self.call("POST", f"/api/cases/{TRACE_CASE}/trace/layering/misc",
                                {"path": child["path_key"], "move_to_misc": True, "source_entity_id": d["source_key"]})
        self.assertEqual(status, 200, res)
        self.assertIn(child["path_key"], res["misc_paths"])
        status, res = self.call("POST", f"/api/cases/{TRACE_CASE}/trace/layering/misc",
                                {"path": child["path_key"], "move_to_misc": False, "source_entity_id": d["source_key"]})
        self.assertEqual(status, 200, res)
        self.assertNotIn(child["path_key"], res["misc_paths"])

    def test_loops_fields(self):
        d = self.get(f"/api/cases/{TRACE_CASE}/trace/loops?max_instances_per_loop=5")
        self.assertTrue(d["loops"])
        for lp in d["loops"]:
            for k in ("total_leakage", "intermediary_leakage", "instances", "instance_count", "disjoint_instance_count"):
                self.assertIn(k, lp)
            for h in lp["hops"]:
                for k in ("date", "utr", "leakage", "leakage_pct", "hours_since_previous_hop"):
                    self.assertIn(k, h)

    def test_tally_structured_preview_and_map(self):
        d = self.get(f"/api/cases/{EDGE_CASE}/edge/tally?company_name=Demo%20Co")
        self.assertNotIn("xml", d)
        for k in ("vouchers", "ledgers", "groups", "xml_preview", "xml_sha256", "xml_bytes", "ledger_map"):
            self.assertIn(k, d)
        self.assertEqual(d["stats"]["company_name"], "Demo Co")
        v = d["vouchers"][0]
        for k in ("voucher_number", "voucher_type", "date", "amount", "party_ledger", "bank_ledger", "entries", "direction"):
            self.assertIn(k, v)
        m = self.get(f"/api/cases/{EDGE_CASE}/edge/tally-map")
        for k in ("category_groups", "category_ledgers", "counterparty_ledgers", "custom_groups",
                  "sales_categories", "purchase_categories", "use_trade_vouchers", "company_name"):
            self.assertIn(k, m["ledger_map"])
        status, res = self.call("PUT", f"/api/cases/{EDGE_CASE}/edge/tally-map", {"ledger_map": {"company_name": "Balaji UI"}})
        self.assertEqual(status, 200, res)
        self.assertEqual(self.get(f"/api/cases/{EDGE_CASE}/edge/tally")["stats"]["company_name"], "Balaji UI")
        status, res = self.call("PUT", f"/api/cases/{EDGE_CASE}/edge/tally-map", {"reset": True})
        self.assertEqual(status, 200, res)

    def test_itr_checks_run_and_waterfall(self):
        d = self.get(f"/api/cases/{EDGE_CASE}/edge/itr")
        ids = {c["id"] for c in d["checks_run"]}
        self.assertTrue({"194C", "269ST", "SFT003"} <= ids)
        for c in d["checks_run"]:
            for k in ("status", "rule", "thresholds", "flag_count", "caveats", "section", "title", "group"):
                self.assertIn(k, c)
        self.assertTrue(all(f.get("check_id") in ids for f in d["statutory_red_flags"]))
        self.assertTrue(d["reconciliation_waterfall"])
        self.assertIn("reconciles", d["reconciliation"])
        for x in d["deductions"]:
            for k in ("section", "title", "amount", "statutory_cap", "eligible_upto_cap", "transactions"):
                self.assertIn(k, x)

    def test_loan_coverage_and_penal(self):
        d = self.get(f"/api/cases/{EDGE_CASE}/edge/loan?income_factor=0.3&abb_factor=0.4")
        cov = d["consolidated_coverage"]
        for k in ("segments", "full_coverage_days", "partial_coverage_days", "uncovered_days"):
            self.assertIn(k, cov)
        for k in ("penal_events", "penal_event_count", "penal_event_breakdown", "overdrawn_days", "cash_withdrawal_pct", "income_basis"):
            self.assertIn(k, d)
        self.assertEqual(d["income_basis"]["business_income_factor"], 0.3)
        self.assertIn("accounts_covered", d["monthly_breakdown"][0])
        self.assertIn("coverage_gaps", d["per_account_abb"][0])


if __name__ == "__main__":
    unittest.main()
