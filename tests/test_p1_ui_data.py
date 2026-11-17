"""P1 UI wiring (ui-data group): entity cluster review, mule explainability, profile category/channel views and
server exports, transactions tx_ids / SRS channels / new anomaly flags.

Three layers:
* contract tests against an in-process server (seeded demo cases): the payload fields the views read;
* node-driven tests of the DOM-free helper modules the views use (skipped when node is unavailable);
* static wiring checks over the view modules (endpoints called, CSP rules kept).
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
from urllib.parse import quote

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.models import AnomalyFlag  # noqa: E402
from cyphersol.nlp.narration_parser import SRS_CHANNEL_EXTENSIONS, SRS_PAYMENT_CHANNELS, srs_payment_channel  # noqa: E402
from cyphersol.server import create_http_handler  # noqa: E402
from cyphersol.service import CypherApplicationService  # noqa: E402

UI_DIR = os.path.join(SRC_DIR, "cyphersol", "ui")
JS_DIR = os.path.join(UI_DIR, "js")
VIEWS = os.path.join(JS_DIR, "views")
SCRATCH_DIR = os.path.join(tempfile.gettempdir(), "claude-501", "cyphersol-p1")
ENV_KEYS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CYPHERSOL_ALLOW_CLOUD_LLM",
            "CYPHERSOL_EXECUTION_MODE", "CYPHERSOL_TALLY_HOSTS", "CYPHERSOL_LLM_PROVIDER")
TRACE_CASE = "CASE_TRACE_EOW_041"
NODE = shutil.which("node") or ("/usr/local/bin/node" if os.path.exists("/usr/local/bin/node") else None)
OWNED = ["transactions.js", "suspense.js", "anomalies.js", "entities.js", "mules.js", "profile.js",
         "transactions-meta.js", "mules-explain.js", "entities-review.js", "profile-export.js"]


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _node_supports_modules() -> bool:
    if not NODE:
        return False
    try:
        r = subprocess.run([NODE, "--experimental-default-type=module", "--input-type=module", "-e", "console.log(1)"],
                           capture_output=True, text=True, timeout=20)
        return r.returncode == 0 and r.stdout.strip() == "1"
    except Exception:  # pragma: no cover - environment specific
        return False


NODE_OK = _node_supports_modules()


def run_node(body: str, data=None):
    """Run an ES-module snippet with `mod(name)` (imports js/<name>) and `DATA` (JSON); returns its JSON output."""
    prelude = (
        f"const JS = {json.dumps('file://' + quote(JS_DIR))};\n"
        "const mod = (p) => import(`${JS}/${p}`);\n"
        "const DATA = JSON.parse(process.env.P1_DATA || 'null');\n"
        "const out = (v) => process.stdout.write(JSON.stringify(v));\n"
    )
    env = dict(os.environ, P1_DATA=json.dumps(data))
    r = subprocess.run([NODE, "--experimental-default-type=module", "--no-warnings", "--input-type=module", "-e", prelude + body],
                       capture_output=True, text=True, timeout=60, env=env)
    if r.returncode != 0:
        raise AssertionError(f"node failed:\n{r.stderr[-3000:]}")
    return json.loads(r.stdout)


class _Server:
    @classmethod
    def start(cls):
        os.makedirs(SCRATCH_DIR, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=SCRATCH_DIR, prefix="p1uidata_")
        cls.env_patch = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in ENV_KEYS}, clear=True)
        cls.env_patch.start()
        cls.service = CypherApplicationService(os.path.join(cls.tmp.name, "v.db"), os.path.join(cls.tmp.name, "samples"), seed_demo=True)
        cls.token = "p1-ui-data-token-" + "z" * 24
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_http_handler(cls.service, UI_DIR, api_token=cls.token))
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def stop(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env_patch.stop()
        cls.tmp.cleanup()

    @classmethod
    def req(cls, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=20)
        h = {"X-CypherSOL-Token": cls.token}
        if body is not None:
            body = json.dumps(body)
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        data = res.read()
        headers = {k.lower(): v for k, v in res.getheaders()}
        conn.close()
        return res.status, headers, data

    @classmethod
    def get_json(cls, path):
        st, _h, data = cls.req("GET", path)
        assert st == 200, (path, st, data[:300])
        return json.loads(data)


# =============================================================================================== contracts
class UiDataContractTests(unittest.TestCase):
    """The fields each rewired view reads, from the real server."""

    @classmethod
    def setUpClass(cls):
        _Server.start()
        cls.S = _Server

    @classmethod
    def tearDownClass(cls):
        _Server.stop()

    def base(self, sub=""):
        return f"/api/cases/{TRACE_CASE}/{sub}"

    def test_meta_lists_srs_channels_and_profile_formats(self):
        meta = self.S.get_json("/api/meta")
        self.assertEqual(meta["srs_payment_channels"], list(SRS_PAYMENT_CHANNELS))
        self.assertEqual(meta["srs_channel_extensions"], list(SRS_CHANNEL_EXTENSIONS))
        self.assertEqual(sorted(meta["profile_export_formats"]), ["html", "pdf", "xlsx"])

    def test_transactions_tx_ids_exact_rows_with_srs_channel(self):
        rows = self.S.get_json(self.base("transactions?limit=5"))["transactions"]
        ids = [r["tx_id"] for r in rows[:3]]
        res = self.S.get_json(self.base("transactions?tx_ids=" + ",".join(ids) + "&limit=500&include_ignored=true"))
        self.assertEqual(sorted(t["tx_id"] for t in res["transactions"]), sorted(ids))
        for t in res["transactions"]:
            self.assertEqual(t["payment_channel_srs"], srs_payment_channel(t["payment_channel"]))
            self.assertIn("card_number_masked", t)
            self.assertIn("cheque_number", t)

    def test_transactions_channel_filter_accepts_srs_names(self):
        res = self.S.get_json(self.base("transactions?channel=UPI&limit=2000"))
        self.assertTrue(res["transactions"])
        self.assertTrue(all(t["payment_channel_srs"] == "UPI" for t in res["transactions"]))

    def test_mule_explainability_has_ten_parameters_with_raw_and_evidence(self):
        mules = self.S.get_json(self.base("trace/mules"))["assessments"]
        self.assertTrue(mules)
        a = mules[0]
        ex = a["explainability"]
        self.assertEqual(len(ex), 10)
        for e in ex:
            for k in ("key", "label", "weight", "score", "contribution", "raw_metric", "raw", "formula", "evidence_tx_ids"):
                self.assertIn(k, e)
        ev = next(e for e in ex if e["evidence_tx_ids"])
        ids = ev["evidence_tx_ids"][:500]
        got = self.S.get_json(self.base("transactions?include_ignored=true&limit=500&tx_ids=" + ",".join(ids)))
        self.assertEqual(sorted(t["tx_id"] for t in got["transactions"]), sorted(set(ids)))

    def test_entity_preview_and_decisions_shape(self):
        p = self.S.get_json(self.base("entities/preview?threshold=0.95"))
        for k in ("threshold", "entity_count", "clusters", "candidates", "conflicts", "changes"):
            self.assertIn(k, p)
        for k in ("merged", "split", "new_entity_ids", "retired_entity_ids"):
            self.assertIn(k, p["changes"])
        st, _h, _d = self.S.req("GET", self.base("entities/preview?threshold=0.2"))
        self.assertEqual(st, 400)
        d = self.S.get_json(self.base("entities/decisions"))
        for k in ("decisions", "history", "legacy_manual_merges"):
            self.assertIn(k, d)

    def test_entity_merge_split_reject_undo_roundtrip(self):
        ov = self.S.get_json(self.base("overview?tx_limit=0"))
        ents = {e["canonical_name"]: e for e in ov["entities"]}
        a, b = ents["VICTIM NINE"]["entity_id"], ents["VICTIM ONE"]["entity_id"]
        st, _h, data = self.S.req("POST", self.base("entities/merge"), {"source_entity_id": a, "target_entity_id": b, "note": "ui test"})
        self.assertEqual(st, 200, data)
        res = json.loads(data)
        self.assertEqual(res["decision"]["op"], "merge")
        self.assertIn(a, res["retired_entity_ids"])
        merged = next(e for e in res["entities"] if e["entity_id"] == b)
        self.assertGreaterEqual(len(merged["aliases"]), 2)
        # split one alias back out, then undo twice -> original IDs restored
        st, _h, data = self.S.req("POST", self.base("entities/split"), {"entity_id": b, "aliases": ["VICTIM NINE"]})
        self.assertEqual(st, 200, data)
        self.assertEqual(json.loads(data)["decision"]["op"], "split")
        for _ in range(2):
            st, _h, data = self.S.req("POST", self.base("entities/undo"), {})
            self.assertEqual(st, 200, data)
        ids = {e["entity_id"] for e in self.S.get_json(self.base("overview?tx_limit=0"))["entities"]}
        self.assertIn(a, ids)
        self.assertIn(b, ids)
        # reject, then undo by id
        st, _h, data = self.S.req("POST", self.base("entities/reject"), {"entity_a_id": a, "entity_b_id": b})
        self.assertEqual(st, 200, data)
        did = json.loads(data)["decision"]["decision_id"]
        st, _h, data = self.S.req("POST", self.base("entities/undo"), {"decision_id": did})
        self.assertEqual(st, 200, data)
        self.assertEqual(self.S.get_json(self.base("entities/decisions"))["decisions"], [])
        st, _h, _d = self.S.req("POST", self.base("entities/merge"), {"source_entity_id": a, "target_entity_id": a})
        self.assertEqual(st, 400)

    def test_profile_payload_and_server_exports(self):
        p = self.S.get_json(self.base("trace/profile"))
        eid = p["entity"]["entity_id"]
        self.assertTrue(p["category_breakdown"])
        for k in ("category_id", "group", "credit", "debit", "tx_count"):
            self.assertIn(k, p["category_breakdown"][0])
        for k in ("channel", "amount", "credit", "debit", "tx_count"):
            self.assertIn(k, p["channel_mix"][0])
        expect = {"pdf": "application/pdf", "xlsx": "spreadsheetml", "html": "text/html"}
        for fmt, ctype in expect.items():
            st, h, data = self.S.req("GET", self.base(f"export/profile?entity_id={eid}&format={fmt}"))
            self.assertEqual(st, 200, (fmt, data[:200]))
            self.assertIn(ctype, h["content-type"])
            self.assertIn("attachment", h["content-disposition"])
            self.assertTrue(data)
        st, _h, _d = self.S.req("GET", self.base(f"export/profile?entity_id={eid}&format=docx"))
        self.assertEqual(st, 400)


# =============================================================================================== node helpers
@unittest.skipUnless(NODE_OK, "node with --experimental-default-type=module is required")
class UiDataHelperTests(unittest.TestCase):
    def test_all_owned_modules_parse(self):
        for name in OWNED:
            path = os.path.join(VIEWS, name)
            with open(path, "rb") as fh:
                r = subprocess.run([NODE, "--input-type=module", "--check"], stdin=fh, capture_output=True, timeout=30)
            self.assertEqual(r.returncode, 0, f"{name}: {r.stderr.decode()[-800:]}")
        with open(os.path.join(JS_DIR, "charts", "bars.js"), "rb") as fh:
            r = subprocess.run([NODE, "--input-type=module", "--check"], stdin=fh, capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr.decode()[-800:])

    def test_srs_channel_mapping_matches_python(self):
        codes = ["TRANSFER", "CHARGES", "ACH", "ECS", "ECOM", "CLG", "CASH_DEPOSIT", "", None, "UPI", "neft", "CASH_WDL",
                 "FOREX", "INTERNAL_TRANSFER", "BANK_CHARGE", "INTEREST", "OTHER", "NACH", "CHEQUE", "POS", " imps "]
        got = run_node("""
          const m = await mod('views/transactions-meta.js');
          out({ mapped: DATA.map((c) => m.srsChannel(c)), idem: DATA.map((c) => m.srsChannel(m.srsChannel(c))),
                labels: ['INTERNAL_TRANSFER','BANK_CHARGE','CASH_CDM','UPI'].map(m.channelLabel),
                tx: m.channelOf({ payment_channel: 'TRANSFER' }), txSrv: m.channelOf({ payment_channel: 'X', payment_channel_srs: 'UPI' }),
                opts: m.channelFilterOptions({ srs_payment_channels: ['UPI','INTERNAL_TRANSFER'], srs_channel_extensions: ['FOREX','OTHER'] }),
                optsDefault: m.channelFilterOptions(null) });
        """, codes)
        self.assertEqual(got["mapped"], [srs_payment_channel(c) for c in codes])
        self.assertEqual(got["idem"], got["mapped"])
        self.assertEqual(got["labels"], ["Internal transfer", "Bank charge", "Cash deposit (CDM)", "UPI"])
        self.assertEqual(got["tx"], "INTERNAL_TRANSFER")
        self.assertEqual(got["txSrv"], "UPI")
        values = [o["value"] for g in got["opts"] for o in g["options"]]
        self.assertEqual(values, ["UPI", "INTERNAL_TRANSFER", "FOREX", "OTHER"])
        default_values = [o["value"] for g in got["optsDefault"] for o in g["options"]]
        self.assertEqual(default_values, list(SRS_PAYMENT_CHANNELS) + list(SRS_CHANNEL_EXTENSIONS))

    def test_every_anomaly_flag_has_label_help_and_order(self):
        flags = [f.value for f in AnomalyFlag]
        got = run_node("""
          const m = await mod('views/transactions-meta.js');
          out(DATA.map((f) => ({ f, label: m.flagInfo(f).label, tone: m.flagInfo(f).tone, help: m.FLAG_HELP[f] || '',
                                 ordered: f === 'VALID' || m.FLAG_ORDER.includes(f), inAll: f === 'VALID' || m.ALL_FLAG_KEYS.includes(f) })));
        """, flags)
        for row in got:
            self.assertTrue(row["help"], f"no FLAG_HELP for {row['f']}")
            self.assertTrue(row["ordered"] and row["inAll"], f"{row['f']} missing from FLAG_ORDER")
            self.assertNotEqual(row["label"], row["f"], f"{row['f']} has no human label")
        outside = next(r for r in got if r["f"] == "OUTSIDE_STATEMENT_PERIOD")
        self.assertEqual(outside["label"], "Outside statement period")

    def test_url_filters_accept_tx_ids_alias_and_cap(self):
        many = ",".join(f"id{i}" for i in range(620))
        got = run_node("""
          const m = await mod('views/transactions-meta.js');
          out({ alias: m.filtersFromParams({ tx_ids: 'a, b,a,,c' }), both: m.filtersFromParams({ ids: 'x', tx_ids: 'y' }),
                cap: m.filtersFromParams({ ids: DATA }).ids.split(',').length, server: m.toServer({ ids: 'a,b', entity: 'E1', channel: 'UPI' }),
                chunks: m.idChunks(DATA.split(',')).map((c) => c.split(',').length), channel: m.filtersFromParams({ channel: 'upi' }).channel, chInternal: m.filtersFromParams({ channel: 'transfer' }).channel });
        """, many)
        self.assertEqual(got["alias"], {"ids": "a,b,c"})
        self.assertEqual(got["both"], {"ids": "x"})
        self.assertEqual(got["cap"], 500)
        self.assertEqual(got["server"], {"tx_ids": "a,b", "entity_id": "E1", "channel": "UPI", "include_ignored": "true"})
        self.assertEqual(got["chunks"], [500, 120])
        self.assertEqual(got["channel"], "UPI")
        self.assertEqual(got["chInternal"], "INTERNAL_TRANSFER")

    def test_mule_explain_rows_cover_all_ten_parameters(self):
        _Server.start()
        try:
            mules = _Server.get_json(f"/api/cases/{TRACE_CASE}/trace/mules")["assessments"]
        finally:
            _Server.stop()
        got = run_node("""
          const m = await mod('views/mules-explain.js');
          const legacy = { ...DATA[0] }; delete legacy.explainability;
          out(DATA.map((a) => { const rows = m.explainRows(a); return { keys: rows.map((r) => r.key), raw: rows.map((r) => r.rawText),
                fig: rows.map((r) => r.rawFig), sum: m.contributionSum(rows), composite: a.composite_score,
                ev: rows.map((r) => r.evidenceIds.length), evc: rows.map((r) => r.evidenceCount),
                expectEv: (a.explainability || []).map((e) => new Set(e.evidence_tx_ids).size),
                formulas: rows.map((r) => r.formula) }; })
              .concat([{ legacy: m.explainRows(legacy).map((r) => [r.key, r.score, r.contribution, r.rawText]) },
                       { links: m.evidenceLinks(Array.from({ length: 1001 }, (_, i) => 't' + i)).map((l) => l.ids.split(',').length) },
                       { unknown: m.describeRaw('mystery', { a_b: 2, c: 'x' }, null), none: m.describeRaw('zero_balance_oscillation', { balance_available: false }, 0) }]));
        """, mules)
        keys_expected = ["flow_through_ratio", "velocity_hold_time", "lifestyle_spending_absence", "counterparty_concentration",
                         "fan_in_fan_out_pattern", "cash_crypto_wallet_leakage", "structuring_smurfing", "zero_balance_oscillation",
                         "burst_dormancy_pattern", "round_number_anomalies"]
        for row in got[: len(mules)]:
            self.assertEqual(row["keys"], keys_expected)
            self.assertTrue(all(row["raw"]), row["raw"])
            self.assertTrue(all(f and f != "—" for f in row["fig"]), row["fig"])
            self.assertTrue(all(row["formulas"]))
            self.assertAlmostEqual(row["sum"], row["composite"], delta=0.2)
            self.assertEqual(sorted(row["ev"]), sorted(row["expectEv"]))
        legacy = got[len(mules)]["legacy"]
        self.assertEqual(len(legacy), 10)
        self.assertTrue(all(r[1] is not None and r[2] is not None for r in legacy))
        self.assertEqual(got[len(mules) + 1]["links"], [500, 500, 1])
        self.assertIn("a b 2.00", got[len(mules) + 2]["unknown"])
        self.assertIn("not assessed", got[len(mules) + 2]["none"])

    def test_entity_review_messages(self):
        preview = {
            "threshold": 0.9, "entity_count": 3, "multi_alias_clusters": 1,
            "clusters": [{"entity_id": "E1", "canonical_name": "POOJAN VIG"}, {"entity_id": "E3", "canonical_name": "PUJAN VIJ"}],
            "candidates": [{"entity_a_id": "E1", "entity_b_id": "E2", "similarity": 0.8}, {"entity_a_id": "E2", "entity_b_id": "E1", "similarity": 0.8},
                           {"entity_a_id": "E1", "entity_b_id": "E3", "similarity": 0.9}],
            "conflicts": [{"type": "HOLDER_GUARD", "a": "DEEP PATANGE", "b": "DEEP PATANGI", "reason": "NAME", "detail": 0.93},
                          {"type": "PAN_AMBIGUOUS", "a": "X", "b": "Y", "reason": "PAN", "detail": {"pan": "ABCDE1234F"}}],
            "changes": {"merged": [{"entity_id": "E1", "from_entity_ids": ["E1", "E9"]}], "split": [{"entity_id": "E4", "into_entity_ids": ["E4", "E3"]}],
                        "new_entity_ids": ["E3"], "retired_entity_ids": ["E9"]},
        }
        got = run_node("""
          const m = await mod('views/entities-review.js');
          const names = { E9: 'POOJAN VIG.', E4: 'OLD NAME' };
          const s = m.previewSummary(DATA, (id) => names[id] || '');
          out({ s, empty: m.previewSummary({ changes: {} }).headline, pairs: m.candidatePairs(DATA.candidates).map((c) => c.entity_a_id + c.entity_b_id),
                warn: m.holderGuardWarning({ canonical_name: 'A', has_statement: true }, { canonical_name: 'B', has_statement: true }),
                nowarn: m.holderGuardWarning({ canonical_name: 'A', has_statement: true }, { canonical_name: 'B', has_statement: false }),
                dm: m.decisionText({ op: 'merge', groups: [['PUJAN VIJ'], ['POOJAN VIG']] }), ds: m.decisionText({ op: 'split', groups: [['PUJAN VIJ'], ['POOJAN VIG']] }),
                dr: m.decisionText({ op: 'reject', groups: [['A'], ['B']] }), ev: m.evidenceText({ reason: 'NAME', detail: 0.92 }),
                se1: m.splitError(['A', 'B'], []), se2: m.splitError(['A', 'B'], ['A', 'B']), se3: m.splitError(['A', 'B'], ['B']),
                hl: m.historyLabel('ENTITY_DECISION_UNDO') });
        """, preview)
        s = got["s"]
        self.assertEqual(s["merged"][0]["absorbed"], ["POOJAN VIG."])
        self.assertEqual(s["split"][0]["into"], ["OLD NAME", "PUJAN VIJ"])
        self.assertEqual(len(s["holderGuards"]), 1)
        self.assertIn("DEEP PATANGE", s["holderGuards"][0]["message"])
        self.assertIn("different statements", s["holderGuards"][0]["message"])
        self.assertIn("ABCDE1234F", s["conflicts"][1]["message"])
        self.assertIn("1 merge", s["headline"])
        self.assertEqual(got["empty"], "No entity would change at this threshold.")
        self.assertEqual(got["pairs"], ["E1E3", "E1E2"])
        self.assertIn("holder guard", got["warn"])
        self.assertIsNone(got["nowarn"])
        self.assertEqual(got["dm"], "Merged PUJAN VIJ into POOJAN VIG")
        self.assertEqual(got["ds"], "Split PUJAN VIJ out of POOJAN VIG")
        self.assertIn("different parties", got["dr"])
        self.assertEqual(got["ev"], "name similarity 0.92")
        self.assertTrue(got["se1"] and got["se2"])
        self.assertEqual(got["se3"], "")
        self.assertEqual(got["hl"], "Undone")

    def test_profile_formats_channels_and_categories(self):
        data = {
            "mix": [{"channel": "TRANSFER", "amount": 100, "credit": 60, "debit": 40, "tx_count": 2},
                    {"channel": "INTERNAL_TRANSFER", "amount": 50, "credit": 0, "debit": 50, "tx_count": 1},
                    {"channel": "UPI", "amount": 300, "credit": 100, "debit": 200, "tx_count": 5}],
            "cats": [{"category": "Salary", "category_id": 1, "group": "Income", "credit": 100, "debit": 0, "tx_count": 1},
                     {"category": "Rent", "category_id": 30, "group": "Expense", "credit": 0, "debit": 50, "tx_count": 1},
                     {"category": "Bonus", "category_id": 3, "group": "Income", "credit": 250, "debit": 0, "tx_count": 1}],
        }
        got = run_node("""
          const m = await mod('views/profile-export.js');
          out({ f: m.profileFormats({ profile_export_formats: ['html', 'xlsx', 'pdf'] }).map((x) => x.fmt),
                f2: m.profileFormats({ profile_export_formats: ['html'] }).map((x) => x.fmt), f3: m.profileFormats(null).map((x) => x.label),
                name: m.profileExportName({ canonical_name: 'Poojan Vig / X' }, 'pdf'),
                ch: m.channelRows(DATA.mix), cats: m.categoryRows(DATA.cats).map((c) => c.name), groups: m.groupRows(DATA.cats) });
        """, data)
        self.assertEqual(got["f"], ["pdf", "xlsx", "html"])
        self.assertEqual(got["f2"], ["html"])
        self.assertEqual(len(got["f3"]), 3)
        self.assertEqual(got["name"], "profile_Poojan_Vig_X.pdf")
        self.assertEqual([c["code"] for c in got["ch"]], ["UPI", "INTERNAL_TRANSFER"])
        it = got["ch"][1]
        self.assertEqual((it["amount"], it["credit"], it["debit"], it["tx_count"]), (150, 60, 90, 3))
        self.assertEqual(it["label"], "Internal transfer")
        self.assertAlmostEqual(sum(c["share"] for c in got["ch"]), 1.0)
        self.assertEqual(got["cats"], ["Bonus", "Salary", "Rent"])
        self.assertEqual([g["group"] for g in got["groups"]], ["Income", "Expense"])
        self.assertEqual(got["groups"][0]["categories"], 2)


# =============================================================================================== static wiring
class UiDataWiringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = {n: _read(os.path.join(VIEWS, n)) for n in OWNED}
        cls.bars = _read(os.path.join(JS_DIR, "charts", "bars.js"))

    def test_entities_view_calls_review_endpoints(self):
        s = self.src["entities.js"]
        for ep in ("entities/preview", "entities/decisions", "entities/merge", "entities/split", "entities/reject", "entities/undo", "entities/recluster"):
            self.assertIn(f'"{ep}"', s, ep)
        self.assertIn("holderGuardWarning", s)
        self.assertIn("AbortController", s)

    def test_mules_view_uses_explainability_and_exact_evidence_filter(self):
        s = self.src["mules.js"]
        self.assertIn("explainRows", s)
        self.assertIn("evidenceLinks", s)
        self.assertIn("tx_ids", s)
        self.assertIn('"transactions", { ids', s)

    def test_profile_view_server_export_menu_and_breakdowns(self):
        s = self.src["profile.js"]
        self.assertIn('"export/profile"', s)
        self.assertIn("createMenu", s)
        self.assertIn("profileFormats", s)
        self.assertIn("channelRows", s)
        self.assertIn("groupRows", s)

    def test_transactions_view_srs_channels_and_flags(self):
        s = self.src["transactions.js"]
        self.assertIn("channelFilterOptions", s)
        self.assertIn("channelOf", s)
        self.assertIn("card_number_masked", s)
        self.assertIn("cheque_number", s)
        self.assertIn("filtersFromParams", s)
        self.assertNotIn("PAYMENT_CHANNELS.map", s)
        a = self.src["anomalies.js"]
        self.assertIn("FLAG_ORDER", a)
        self.assertIn('"dedup"', a)

    def test_csp_rules_hold_in_owned_files(self):
        for name, src in list(self.src.items()) + [("bars.js", self.bars)]:
            self.assertNotRegex(src, r"\son(?:click|change|input|submit|load|error|mouse\w+|key\w+)\s*=\s*[\"'`]", name)
            self.assertNotRegex(src, r"<[a-zA-Z][^<>]*\sstyle\s*=\s*[\"']", name)
            self.assertNotRegex(src, r"\beval\(|new Function\(", name)
            self.assertNotRegex(src, r"\?\s*'[^'\n]*=\"[^'\n]*'\s*:", name)
            self.assertNotRegex(src, r"\bfetch\(", name)
            self.assertNotRegex(src, r"https?://(?!www\.w3\.org)", name)


if __name__ == "__main__":
    unittest.main()
