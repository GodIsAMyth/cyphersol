"""P1 backend wiring tests: persistence (entity decisions, decision history, per-branch Misc, case settings,
statement PAN, card / cheque columns), schema migration of pre-P1 vaults, SQL-side transaction filtering, and the
new or changed service methods and HTTP endpoints (cluster review, draw-tool raster, password hints, bulk dedup,
Tally ledger map, profile / Tally / ITR exports, layering drill-down, bidirectional and loop parameters)."""

from __future__ import annotations

import base64
import http.client
import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from typing import Any, Dict, Optional, Tuple
from unittest import mock

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (SRC_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cyphersol.ingestion import pdf_parser  # noqa: E402
from cyphersol.models import NormalizedTransaction  # noqa: E402
from cyphersol.server import CONTENT_SECURITY_POLICY, create_http_handler  # noqa: E402
from cyphersol.service import CypherApplicationService, tx_to_json  # noqa: E402
from cyphersol.storage import CypherVault  # noqa: E402

SCRATCH = os.path.join(tempfile.gettempdir(), "claude-501", "cyphersol-p1")
UI_DIR = os.path.join(SRC_DIR, "cyphersol", "ui")
SAMPLES = os.path.join(ROOT_DIR, "sample_statements")
OLD_DB = os.path.join(SCRATCH, "old_pre_p1.db")
EDGE_CASE = "CASE_EDGE_BALAJI_FY25"
TRACE_CASE = "CASE_TRACE_EOW_041"
ENV_KEYS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CYPHERSOL_ALLOW_CLOUD_LLM",
            "CYPHERSOL_EXECUTION_MODE", "CYPHERSOL_TALLY_HOSTS", "CYPHERSOL_LLM_PROVIDER")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _clean_env() -> Dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in ENV_KEYS}


def _csv(rows, holder="MEERA TRADERS", acct="123456789012") -> bytes:
    lines = [f"Account Holder: {holder}", f"Account Number: {acct}", "Date,Narration,Debit,Credit,Balance"]
    lines += rows
    return "\n".join(lines).encode("utf-8")


class _Base(unittest.TestCase):
    """Fresh seeded service per test class, plus an HTTP server on a random loopback port."""

    @classmethod
    def setUpClass(cls) -> None:
        os.makedirs(SCRATCH, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.env_patch = mock.patch.dict(os.environ, _clean_env(), clear=True)
        cls.env_patch.start()
        cls.db = os.path.join(cls.tmp.name, "v.db")
        cls.svc = CypherApplicationService(cls.db, os.path.join(cls.tmp.name, "samples"), seed_demo=True)
        cls.token = "p1-backend-token-" + "z" * 24
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_http_handler(cls.svc, UI_DIR, api_token=cls.token))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.env_patch.stop()
        cls.tmp.cleanup()

    def req(self, method: str, path: str, body: Any = None, token: bool = True,
            headers: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = dict(headers or {})
        if token:
            h["X-CypherSOL-Token"] = self.token
        if body is not None and not isinstance(body, (bytes, str)):
            body = json.dumps(body)
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        data = res.read()
        out = (res.status, {k.lower(): v for k, v in res.getheaders()}, data)
        conn.close()
        return out

    def jget(self, path: str) -> Tuple[int, Any]:
        st, _h, data = self.req("GET", path)
        return st, json.loads(data.decode("utf-8")) if data else None

    def jpost(self, path: str, body: Any, method: str = "POST") -> Tuple[int, Any]:
        st, _h, data = self.req(method, path, body)
        return st, json.loads(data.decode("utf-8")) if data else None


# ================================================================================================ storage
class StorageP1Tests(unittest.TestCase):
    def setUp(self) -> None:
        os.makedirs(SCRATCH, exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.vault = CypherVault(os.path.join(self.tmp.name, "s.db"))
        from cyphersol.models import InvestigationCase, WorkspaceMode
        self.vault.save_case(InvestigationCase("C1", "t", WorkspaceMode.CYPHER_TRACE, "2026-01-01T00:00:00+00:00"))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_entity_decisions_roundtrip_and_history_is_append_only(self) -> None:
        rev0 = self.vault.get_case_revision("C1")
        ds = [{"decision_id": "d1", "op": "merge", "groups": [["A"], ["B"]], "created_at": "x"},
              {"decision_id": "d2", "op": "reject", "groups": [["C"], ["D"]], "created_at": "y"}]
        self.vault.replace_entity_decisions("C1", ds)
        self.assertEqual([d["decision_id"] for d in self.vault.get_entity_decisions("C1")], ["d1", "d2"])
        self.assertGreater(self.vault.get_case_revision("C1"), rev0)
        with self.assertRaises(ValueError):
            self.vault.replace_entity_decisions("C1", [{"decision_id": "d1"}, {"decision_id": "d1"}])
        self.assertEqual(len(self.vault.get_entity_decisions("C1")), 2, "failed replace left the list intact")
        self.vault.commit_case_update("C1", entity_decisions=ds[:1], decision_events=[("ENTITY_DECISION_UNDO", ds[1])])
        hist = self.vault.get_entity_decision_history("C1")
        self.assertEqual((hist[0]["action"], hist[0]["decision_id"]), ("ENTITY_DECISION_UNDO", "d2"))
        with closing(sqlite3.connect(self.vault.db_path)) as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM entity_decision_history")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE entity_decision_history SET action = 'X'")

    def test_misc_paths_and_case_settings(self) -> None:
        self.assertEqual(self.vault.toggle_misc_path("C1", "E1", "E1>E2"), ["E1>E2"])
        self.assertEqual(self.vault.toggle_misc_path("C1", "E1", "E1>E3"), ["E1>E2", "E1>E3"])
        self.assertEqual(self.vault.toggle_misc_path("C1", "tx:T9", "E1>E2"), ["E1>E2"])
        self.assertEqual(self.vault.toggle_misc_path("C1", "E1", "E1>E2", move_to_misc=False), ["E1>E3"])
        self.assertEqual(self.vault.get_all_misc_paths("C1"), {"E1": ["E1>E3"], "tx:T9": ["E1>E2"]})
        self.assertIsNone(self.vault.get_case_setting("C1", "k"))
        self.vault.save_case_setting("C1", "k", {"a": 1})
        self.assertEqual(self.vault.get_case_setting("C1", "k"), {"a": 1})

    def test_indexes_for_transaction_queries(self) -> None:
        with closing(sqlite3.connect(self.vault.db_path)) as conn:
            idx = {r[0]: r[1] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index'")}
        self.assertIn("idx_tx_case_date", idx)
        self.assertIn("idx_tx_category", idx)
        self.assertIn("idx_tx_case_stmt", idx)
        self.assertIn("case_id, statement_id", idx["idx_tx_case_stmt"])


class MigrationTests(unittest.TestCase):
    def _assert_p1_schema(self, path: str) -> None:
        with closing(sqlite3.connect(path)) as conn:
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            stmt_cols = {r[1] for r in conn.execute("PRAGMA table_info(statements)")}
            tx_cols = {r[1] for r in conn.execute("PRAGMA table_info(transactions)")}
        self.assertTrue({"entity_decisions", "entity_decision_history", "layering_misc_paths", "case_settings"} <= tables)
        self.assertIn("pan", stmt_cols)
        self.assertTrue({"card_number_masked", "cheque_number"} <= tx_cols)

    @unittest.skipUnless(os.path.exists(OLD_DB), "pre-P1 vault fixture not created (backend_scripts/make_old_db.py)")
    def test_pre_p1_vault_migrates_idempotently_and_keeps_data(self) -> None:
        with tempfile.TemporaryDirectory(dir=SCRATCH) as td, mock.patch.dict(os.environ, _clean_env(), clear=True):
            path = os.path.join(td, "old.db")
            shutil.copy(OLD_DB, path)
            with closing(sqlite3.connect(path)) as conn:
                n_tx = conn.execute("SELECT COUNT(*) FROM transactions WHERE case_id = ?", (TRACE_CASE,)).fetchone()[0]
                n_audit = conn.execute("SELECT COUNT(*) FROM audit_logs").fetchone()[0]
                old_ids = sorted(r[0] for r in conn.execute("SELECT entity_id FROM entities WHERE case_id = ?", (TRACE_CASE,)))
                merges = conn.execute("SELECT COUNT(*) FROM manual_merges").fetchone()[0]
            for _ in range(2):  # idempotent
                svc = CypherApplicationService(path, os.path.join(td, "samples"), seed_demo=True)
                self._assert_p1_schema(path)
                self.assertEqual(len(svc.vault.get_transactions(TRACE_CASE)), n_tx)
                self.assertTrue(svc.vault.verify_audit_chain()["ok"])
            self.assertGreaterEqual(svc.vault.verify_audit_chain()["count"], n_audit)
            self.assertEqual(len(svc.vault.get_manual_merges(TRACE_CASE)), merges, "legacy merges still honoured")
            # Re-clustering keeps the existing IDs (NLP-12): previous entities are passed to the resolver.
            res = svc.recluster_entities(TRACE_CASE)
            new_ids = sorted(e["entity_id"] for e in res["entities"])
            self.assertEqual(new_ids, old_ids)
            ov = svc.get_case_overview(TRACE_CASE, tx_limit=50)
            self.assertIn("payment_channel_srs", ov["transactions"][0])

    def test_fresh_vault_reopen_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory(dir=SCRATCH) as td:
            path = os.path.join(td, "fresh.db")
            CypherVault(path)
            CypherVault(path)
            self._assert_p1_schema(path)


# ================================================================================================ service
class TransactionQueryTests(_Base):
    def test_sql_sort_amount_and_tx_ids(self) -> None:
        all_rows = self.svc.list_transactions(TRACE_CASE, limit=2000)["transactions"]
        ids = [r["tx_id"] for r in all_rows[:3]]
        res = self.svc.list_transactions(TRACE_CASE, tx_ids=",".join(ids))
        self.assertEqual(sorted(r["tx_id"] for r in res["transactions"]), sorted(ids))
        res = self.svc.list_transactions(TRACE_CASE, sort="amount", order="desc", limit=5)
        amts = [max(r["debit"], r["credit"]) for r in res["transactions"]]
        self.assertEqual(amts, sorted(amts, reverse=True))
        res = self.svc.list_transactions(TRACE_CASE, amount_min=1000, amount_max=50000, limit=2000)
        self.assertTrue(all(1000 <= max(r["debit"], r["credit"]) <= 50000 for r in res["transactions"]))
        with self.assertRaises(ValueError):
            self.svc.list_transactions(TRACE_CASE, sort="DROP TABLE")
        with self.assertRaises(ValueError):
            self.svc.list_transactions(TRACE_CASE, amount_min=10, amount_max=5)
        # query_transactions does it all in SQL now
        rows, total = self.svc.vault.query_transactions(TRACE_CASE, filters={"tx_ids": ids[:2]}, sort="amount", order="desc")
        self.assertEqual(total, 2)

    def test_srs_channel_in_json_filter_and_meta(self) -> None:
        rows = self.svc.list_transactions(EDGE_CASE, limit=2000)["transactions"]
        self.assertTrue(all("payment_channel_srs" in r for r in rows))
        internal = [r for r in rows if r["payment_channel"] == "TRANSFER"]
        if internal:
            self.assertEqual(internal[0]["payment_channel_srs"], "INTERNAL_TRANSFER")
            via_srs = self.svc.list_transactions(EDGE_CASE, channel="INTERNAL_TRANSFER", limit=2000)["total"]
            self.assertEqual(via_srs, len(internal))
        meta = self.svc.get_metadata()
        self.assertIn("INTERNAL_TRANSFER", meta["srs_payment_channels"])
        self.assertIn("OTHER", meta["srs_channel_extensions"])

    def test_http_tx_ids_filter(self) -> None:
        st, body = self.jget(f"/api/cases/{TRACE_CASE}/transactions?limit=3")
        ids = [r["tx_id"] for r in body["transactions"]]
        st, body = self.jget(f"/api/cases/{TRACE_CASE}/transactions?tx_ids={','.join(ids[:2])}")
        self.assertEqual(st, 200)
        self.assertEqual(body["total"], 2)
        st, body = self.jget(f"/api/cases/{TRACE_CASE}/transactions?tx_ids=" + ",".join(f"x{i}" for i in range(501)))
        self.assertEqual(st, 400)


class ClusterReviewTests(_Base):
    def _two_people(self) -> Tuple[str, str]:
        ents = [e for e in self.svc.vault.get_entities(TRACE_CASE) if not getattr(e, "is_system", False)]
        return ents[0].entity_id, ents[1].entity_id

    def test_overview_candidates_have_ids_and_preview_is_read_only(self) -> None:
        ov = self.svc.get_case_overview(TRACE_CASE)
        for c in ov["near_merge_candidates"]:
            self.assertTrue({"entity_a", "entity_b", "similarity", "entity_a_id", "entity_b_id"} <= set(c))
        rev = self.svc.vault.get_case_revision(TRACE_CASE)
        prev = self.svc.preview_entity_clusters(TRACE_CASE, 0.70)
        self.assertEqual(prev["threshold"], 0.7)
        self.assertIn("changes", prev)
        self.assertEqual(self.svc.vault.get_case_revision(TRACE_CASE), rev)
        for bad in (0.2, 1.5, "x", float("nan")):
            with self.assertRaises(ValueError):
                self.svc.preview_entity_clusters(TRACE_CASE, bad)

    def test_merge_reject_split_undo_roundtrip(self) -> None:
        # Merge a statement holder INTO a counterparty: the hardest case for keeping IDs stable on undo.
        ents = [e for e in self.svc.vault.get_entities(TRACE_CASE) if not getattr(e, "is_system", False)]
        a = next(e.entity_id for e in ents if e.has_statement)
        b = next(e.entity_id for e in ents if not e.has_statement)
        ids0 = {e.entity_id for e in self.svc.vault.get_entities(TRACE_CASE)}
        n0 = len(ids0)
        res = self.svc.merge_entities(TRACE_CASE, a, b)
        self.assertEqual(res["decision"]["op"], "merge")
        ids = {e["entity_id"] for e in res["entities"]}
        self.assertIn(b, ids, "merged cluster keeps the target id")
        self.assertNotIn(a, ids)
        self.assertEqual(len(res["entities"]), n0 - 1)
        merged = next(e for e in res["entities"] if e["entity_id"] == b)
        aliases = merged["aliases"]
        # split part of it back out
        part = [x for x in aliases if x != merged["canonical_name"]][:1] or aliases[:1]
        res2 = self.svc.split_entity(TRACE_CASE, b, part)
        self.assertEqual(res2["decision"]["op"], "split")
        self.assertEqual(len(self.svc.vault.get_entity_decisions(TRACE_CASE)), 2)
        # undo the split, then the merge
        res3 = self.svc.undo_entity_decision(TRACE_CASE)
        self.assertEqual(res3["decision"]["op"], "split")
        res4 = self.svc.undo_entity_decision(TRACE_CASE, res["decision"]["decision_id"])
        self.assertEqual(res4["decision"]["op"], "merge")
        self.assertEqual(len(res4["entities"]), n0)
        self.assertEqual({e["entity_id"] for e in res4["entities"]}, ids0, "original IDs come back after undo")
        none = self.svc.undo_entity_decision(TRACE_CASE)
        self.assertFalse(none["changed"])
        # reject is persisted and audited
        self.svc.reject_entity_merge(TRACE_CASE, a, b)
        actions = [h["action"] for h in self.svc.vault.get_entity_decision_history(TRACE_CASE)]
        self.assertEqual(actions[:5], ["ENTITY_REJECT", "ENTITY_DECISION_UNDO", "ENTITY_DECISION_UNDO", "ENTITY_SPLIT",
                                       "ENTITY_MERGE"])
        audit = [r["action"] for r in self.svc.vault.get_audit_logs(TRACE_CASE, limit=10)]
        self.assertIn("ENTITY_REJECT", audit)
        self.svc.undo_entity_decision(TRACE_CASE)
        with self.assertRaises(ValueError):
            self.svc.merge_entities(TRACE_CASE, a, a)
        with self.assertRaises(ValueError):
            self.svc.undo_entity_decision(TRACE_CASE, "nope")

    def test_undo_restores_ids_after_holder_merged_into_counterparty(self) -> None:
        ents = {e.canonical_name: e.entity_id for e in self.svc.vault.get_entities(TRACE_CASE)}
        if "POOJAN VIG" not in ents or "VICTIM ELEVEN" not in ents:
            self.skipTest("demo names changed")
        ids0 = set(ents.values())
        a, b = ents["POOJAN VIG"], ents["VICTIM ELEVEN"]
        merged = next(e for e in self.svc.merge_entities(TRACE_CASE, a, b)["entities"] if e["entity_id"] == b)
        part = [x for x in merged["aliases"] if x != merged["canonical_name"]][:1]
        self.svc.split_entity(TRACE_CASE, b, part)
        self.svc.reject_entity_merge(TRACE_CASE, a, b)
        for _ in range(3):
            last = self.svc.undo_entity_decision(TRACE_CASE)
        self.assertEqual(last["decision"]["op"], "merge")
        self.assertEqual({e["entity_id"] for e in last["entities"]}, ids0)
        after = {e.canonical_name: e.entity_id for e in self.svc.vault.get_entities(TRACE_CASE)}
        self.assertEqual((after["POOJAN VIG"], after["VICTIM ELEVEN"]), (a, b))

    def test_http_cluster_endpoints(self) -> None:
        a, b = self._two_people()
        st, body = self.jget(f"/api/cases/{TRACE_CASE}/entities/preview?threshold=0.8")
        self.assertEqual(st, 200)
        self.assertIn("clusters", body)
        self.assertEqual(self.jget(f"/api/cases/{TRACE_CASE}/entities/preview?threshold=3")[0], 400)
        st, body = self.jpost(f"/api/cases/{TRACE_CASE}/entities/reject", {"entity_a_id": a, "entity_b_id": b})
        self.assertEqual(st, 200)
        did = body["decision"]["decision_id"]
        st, body = self.jget(f"/api/cases/{TRACE_CASE}/entities/decisions")
        self.assertEqual([d["decision_id"] for d in body["decisions"]], [did])
        self.assertEqual(self.jpost(f"/api/cases/{TRACE_CASE}/entities/undo", {"decision_id": did})[0], 200)
        self.assertEqual(self.jpost(f"/api/cases/{TRACE_CASE}/entities/merge", {"source_entity_id": a})[0], 400)
        self.assertEqual(self.jpost(f"/api/cases/{TRACE_CASE}/entities/split", {"entity_id": a, "aliases": "x"})[0], 400)
        self.assertEqual(self.jpost(f"/api/cases/{TRACE_CASE}/entities/merge",
                                    {"source_entity_id": "nope", "target_entity_id": b})[0], 400)
        self.assertEqual(self.jpost("/api/cases/NOPE/entities/undo", {})[0], 404)
        self.assertEqual(self.req("POST", f"/api/cases/{TRACE_CASE}/entities/undo", {}, token=False)[0], 403)


class DrawToolAndPasswordTests(_Base):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        with open(os.path.join(SAMPLES, "CASE041_POOJAN-VIG_HDFC_501002938471.pdf"), "rb") as f:
            cls.pdf = f.read()

    def test_save_template_validates_geometry(self) -> None:
        with self.assertRaises(ValueError):
            self.svc.save_draw_template("X", "bad", [0, 0, 1, 1], [0.5, float("nan")], ["date", "narration", "amount"])
        tpl = self.svc.save_draw_template("X", "ok", [0, 0, 1, 1], [0.7, 0.2], ["date", "narration", "amount"])
        self.assertEqual(tpl["column_splits"], sorted(tpl["column_splits"]))
        st, body = self.jpost("/api/draw-templates", {"column_splits": [2, -1], "table_bbox": [0, 0, 1, 1]})
        self.assertEqual(st, 400)

    def test_page_image_endpoint(self) -> None:
        b64 = base64.b64encode(self.pdf).decode()
        try:
            pdf_parser.render_page_png(self.pdf, 1, 36)
        except pdf_parser.PageRenderError as exc:
            self.skipTest(f"no renderer: {exc.code}")
        st, body = self.jpost("/api/draw/page-image", {"content_b64": b64, "page": 1, "dpi": 72})
        self.assertEqual(st, 200)
        self.assertTrue(body["data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(base64.b64decode(body["png_b64"])[:8], b"\x89PNG\r\n\x1a\n")
        for k in ("page_count", "width_px", "height_px", "page_width_pt", "page_height_pt"):
            self.assertIn(k, body)
        self.assertEqual(self.jpost("/api/draw/page-image", {"content_b64": b64, "page": 99})[0], 400)
        self.assertEqual(self.jpost("/api/draw/page-image", {"content_b64": b64, "dpi": 5000})[0], 400)
        self.assertEqual(self.jpost("/api/draw/page-image", {})[0], 400)

    def test_page_render_error_mapping(self) -> None:
        for code, status in (("renderer_unavailable", 503), ("wrong_password", 401), ("corrupt", 400)):
            err = pdf_parser.PageRenderError(code, "boom") if _render_error_takes_code() else _mk_err(code)
            with mock.patch.object(self.svc, "render_draw_page", side_effect=err):
                st, body = self.jpost("/api/draw/page-image", {"content_b64": "AAAA"})
            self.assertEqual(st, status, code)
            self.assertEqual(body["code"], code)

    def test_password_hints_open_encrypted_pdf_and_are_never_stored(self) -> None:
        from test_p0_pdf import encrypt_pdf_rc4
        secret = "17051990"
        enc = encrypt_pdf_rc4(self.pdf, secret)
        cid = self.svc.create_case("pw", "cypher_trace")["case_id"]
        st, body = self.jpost(f"/api/cases/{cid}/upload", {
            "files": [{"filename": "enc.pdf", "content_b64": base64.b64encode(enc).decode()}],
            "password_hints": {"dob": "1990-05-17", "name": "POOJAN VIG"},
        })
        self.assertEqual(st, 200, body)
        rep = body["ingested"][0]
        self.assertEqual(rep["status"], "ok", rep)
        self.assertEqual(rep["password_label"], "dob_ddmmyyyy")
        self.assertGreater(rep["transactions"], 0)
        prov = self.svc.vault.get_statement_provenance(cid)[0]["details"]
        self.assertEqual(prov["password_label"], "dob_ddmmyyyy")
        self.svc.vault.get_case_revision(cid)  # flush
        for suffix in ("", "-wal"):
            p = self.db + suffix
            if os.path.exists(p):
                with open(p, "rb") as f:
                    self.assertNotIn(secret.encode(), f.read(), f"password leaked into {p}")
        # password-check reports the label only
        st, chk = self.jpost("/api/pdf/password-check", {"content_b64": base64.b64encode(enc).decode(),
                                                          "password_hints": {"dob": "17/05/1990"}})
        self.assertEqual(st, 200)
        self.assertEqual((chk["status"], chk["label"]), ("ok", "dob_ddmmyyyy"))
        self.assertNotIn(secret, json.dumps(chk))
        st, _ = self.jpost("/api/pdf/password-check", {"content_b64": "QQ==", "password_hints": {"shoe": "x"}})
        self.assertEqual(st, 400)


def _render_error_takes_code() -> bool:
    try:
        pdf_parser.PageRenderError("corrupt", "x")
        return True
    except TypeError:
        return False


def _mk_err(code: str) -> Exception:
    e = pdf_parser.PageRenderError("x")
    e.code, e.message = code, "boom"
    return e


class DedupAndIngestReportTests(_Base):
    def test_bulk_dedup_preview_and_apply(self) -> None:
        cid = self.svc.create_case("dedup", "cypher_trace")["case_id"]
        rows = ["01/04/2026,UPI/111/RAVI KUMAR,,500.00,1500.00", "02/04/2026,NEFT/222/ACME LTD,200.00,,1300.00"]
        a = _csv(["31/03/2026,OPENING,,1000.00,1000.00"] + rows)
        b = _csv(rows + ["03/04/2026,ATM WDL,100.00,,1200.00"])
        res = self.svc.ingest_statements(cid, [("a.csv", a, None), ("b.csv", b, None)])
        self.assertEqual(res["files_ok"], 2)
        st, prev = self.jpost(f"/api/cases/{cid}/dedup", {"apply": False})
        self.assertEqual(st, 200)
        self.assertFalse(prev["applied"])
        self.assertGreaterEqual(prev["group_count"], 1)
        rev = self.svc.vault.get_case_revision(cid)
        st, done = self.jpost(f"/api/cases/{cid}/dedup", {"apply": True})
        self.assertEqual(st, 200)
        self.assertEqual(len(done["changed_tx_ids"]), done["duplicate_count"])
        self.assertGreater(self.svc.vault.get_case_revision(cid), rev)
        ignored = self.svc.vault.get_transactions_by_ids(cid, done["changed_tx_ids"])
        self.assertTrue(all(t.is_duplicate_ignored for t in ignored))
        self.assertIn("BULK_DEDUP", [r["action"] for r in self.svc.vault.get_audit_logs(cid, limit=5)])
        again = self.svc.dedup_transactions(cid, apply=True)
        self.assertEqual(again["changed_tx_ids"], [], "idempotent")
        self.assertEqual(self.jpost(f"/api/cases/{cid}/dedup", {"apply": "yes"})[0], 400)

    def test_ingest_report_carries_crosscheck_and_coverage(self) -> None:
        cid = self.svc.create_case("rep", "cypher_trace")["case_id"]
        data = _csv(["01/04/2026,OPENING,,1000.00,1000.00", "02/04/2026,UPI/1/X,100.00,,900.00"])
        rep = self.svc.ingest_statements(cid, [("CASE1_MEERA-TRADERS_SBI_123456789012.csv", data, None)])["ingested"][0]
        for key in ("filename_crosscheck", "period_coverage", "password_label", "low_ocr_confidence"):
            self.assertIn(key, rep)
        self.assertIsInstance(rep["filename_crosscheck"], dict)
        prov = self.svc.vault.get_statement_provenance(cid)[0]["details"]
        self.assertIn("filename_crosscheck", prov)

    def test_statement_pan_is_parsed_and_persisted(self) -> None:
        cid = self.svc.create_case("pan", "cypher_trace")["case_id"]
        data = ("Account Holder: MEERA TRADERS\nAccount Number: 123456789012\nPAN: ABCPM1234K\n"
                "Date,Narration,Debit,Credit,Balance\n01/04/2026,OPENING,,1000.00,1000.00\n").encode()
        rep = self.svc.ingest_statements(cid, [("p.csv", data, None)])["ingested"][0]
        self.assertEqual(rep["pan"], "ABCPM1234K")
        self.assertEqual(self.svc.vault.get_statements(cid)[0].pan, "ABCPM1234K")
        self.assertEqual(self.svc.get_case_overview(cid)["statements"][0]["pan"], "ABCPM1234K")

    def test_ocr_autocorrect_is_audited(self) -> None:
        cid = self.svc.create_case("ocr", "cypher_trace")["case_id"]
        fake = {"warnings": [], "ocr_corrections": [{"tx_id": "t", "row_index": 0, "before": {}, "after": {}}]}
        orig = __import__("cyphersol.ingestion.parser", fromlist=["x"]).bulk_parse_statements

        def wrapped(*a, **k):
            out = orig(*a, **k)
            for r in out:
                if r.ok:
                    r.meta.update(fake)
            return out

        with mock.patch("cyphersol.service.bulk_parse_statements", side_effect=wrapped):
            self.svc.ingest_statements(cid, [("x.csv", _csv(["01/04/2026,UPI/1/X,,100.00,100.00"]), None)])
        rows = [r for r in self.svc.vault.get_audit_logs(cid, limit=10) if r["action"] == "OCR_AUTOCORRECT"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["details"]["corrections"][0]["tx_id"], "t")


class LabelledExamplesTests(_Base):
    def test_resolution_refits_categoriser(self) -> None:
        sus = self.svc.list_transactions(EDGE_CASE, is_suspense=True, limit=1)["transactions"]
        if not sus:
            self.skipTest("no suspense rows in demo case")
        with mock.patch.object(self.svc.categorizer, "fit_labelled_examples", return_value=1) as fit:
            self.svc.resolve_suspense(EDGE_CASE, sus[0]["tx_id"], 13, auto_refresh_similar=False)
        self.assertTrue(fit.called)
        examples = list(fit.call_args.args[0])
        self.assertTrue(all(isinstance(t, NormalizedTransaction) and isinstance(c, int) for t, c in examples))
        self.assertIn(sus[0]["tx_id"], {t.tx_id for t, _ in examples})


class EdgeWiringTests(_Base):
    def test_tally_map_get_set_and_preview_shape(self) -> None:
        st, body = self.jget(f"/api/cases/{EDGE_CASE}/edge/tally-map")
        self.assertEqual(st, 200)
        self.assertTrue(body["is_default"])
        lm = body["ledger_map"]
        lm["company_name"] = "Balaji Books"
        lm["use_trade_vouchers"] = True
        st, saved = self.jpost(f"/api/cases/{EDGE_CASE}/edge/tally-map", {"ledger_map": lm}, method="PUT")
        self.assertEqual(st, 200, saved)
        self.assertFalse(saved["is_default"])
        self.assertEqual(self.jget(f"/api/cases/{EDGE_CASE}/edge/tally-map")[1]["ledger_map"]["company_name"], "Balaji Books")
        st, err = self.jpost(f"/api/cases/{EDGE_CASE}/edge/tally-map", {"ledger_map": {"category_groups": {"1": 5}}})
        self.assertEqual(st, 400)
        self.assertIn("error", err)
        st, prev = self.jget(f"/api/cases/{EDGE_CASE}/edge/tally")
        self.assertEqual(st, 200)
        self.assertNotIn("xml", prev)
        for k in ("stats", "vouchers", "ledgers", "groups", "xml_preview", "xml_truncated", "xml_sha256", "xml_bytes", "ledger_map"):
            self.assertIn(k, prev)
        self.assertTrue(prev["stats"]["use_trade_vouchers"], "map setting applies when the query omits it")
        st, prev2 = self.jget(f"/api/cases/{EDGE_CASE}/edge/tally?use_trade_vouchers=false&include_xml=true")
        self.assertFalse(prev2["stats"]["use_trade_vouchers"])
        self.assertIn("<ENVELOPE>", prev2["xml"])
        self.assertIn("TALLY_MAP_UPDATE", [r["action"] for r in self.svc.vault.get_audit_logs(EDGE_CASE, limit=20)])
        # PUT is only for the ledger map
        self.assertEqual(self.req("PUT", f"/api/cases/{EDGE_CASE}/dedup", {"apply": False})[0], 405)
        st, reset = self.jpost(f"/api/cases/{EDGE_CASE}/edge/tally-map", {"reset": True})
        self.assertEqual(reset["ledger_map"]["company_name"], None)

    def test_loan_income_parameters(self) -> None:
        st, body = self.jget(f"/api/cases/{EDGE_CASE}/edge/loan?income_factor=0.3&abb_factor=0.4")
        self.assertEqual(st, 200)
        self.assertEqual(body["income_basis"]["business_income_factor"], 0.3)
        self.assertEqual(body["income_basis"]["abb_income_factor"], 0.4)
        st, body = self.jget(f"/api/cases/{EDGE_CASE}/edge/loan?income_override=50000")
        self.assertEqual(body["income_basis"]["monthly_income_override"], 50000.0)
        for q in ("income_factor=2", "abb_factor=-1", "income_override=-5", "income_factor=nan"):
            self.assertEqual(self.jget(f"/api/cases/{EDGE_CASE}/edge/loan?{q}")[0], 400, q)

    def test_itr_checks_and_exports(self) -> None:
        st, itr = self.jget(f"/api/cases/{EDGE_CASE}/edge/itr")
        self.assertEqual(st, 200)
        self.assertTrue(itr["checks_run"])
        self.assertTrue(all("thresholds" in c and "status" in c for c in itr["checks_run"]))
        st, h, data = self.req("GET", f"/api/cases/{EDGE_CASE}/export/itr-xlsx")
        self.assertEqual((st, h["content-type"]), (200, XLSX))
        self.assertIn("attachment", h["content-disposition"])
        from cyphersol.ingestion.xlsx_io import read_xlsx_bytes
        names = [n for n, _rows in read_xlsx_bytes(data)] if isinstance(read_xlsx_bytes(data), list) else list(read_xlsx_bytes(data))
        self.assertIn("ITR Checks Run", names)
        self.assertEqual(self.req("GET", f"/api/cases/{EDGE_CASE}/export/itr-xlsx?fy=FY1999-00")[0], 400)
        st, h, data = self.req("GET", f"/api/cases/{EDGE_CASE}/export/tally-xlsx?trade_vouchers=true")
        self.assertEqual((st, h["content-type"]), (200, XLSX))
        self.assertEqual(data[:2], b"PK")


class TraceWiringTests(_Base):
    def _source(self) -> str:
        return self.svc.get_layering(TRACE_CASE)["selected_source_id"]

    def test_layering_params_expand_and_branch_misc(self) -> None:
        tree = self.svc.get_layering(TRACE_CASE, max_depth=2, trace_depth=3)
        self.assertEqual(tree["trace_depth"], 3)  # attribution depth, never below the display depth
        root = tree["root"]
        self.assertIn("path_key", root)
        src = tree["selected_source_id"]
        exp = self.svc.expand_layering(TRACE_CASE, src, root["path_key"], trace_depth=3)
        self.assertEqual(exp["path_key"], root["path_key"])
        self.assertIsInstance(exp["children"], list)
        st, body = self.jget(f"/api/cases/{TRACE_CASE}/trace/layering/expand?source={src}&path={root['path_key']}")
        self.assertEqual(st, 200)
        self.assertEqual(self.jget(f"/api/cases/{TRACE_CASE}/trace/layering?trace_depth=99")[0], 400)
        self.assertEqual(self.jget(f"/api/cases/{TRACE_CASE}/trace/layering/expand?source={src}")[0], 400)
        kids = [c for c in root.get("children") or [] if c.get("entity_id") and not c.get("is_misc")]
        if kids:
            child = kids[0]
            st, res = self.jpost(f"/api/cases/{TRACE_CASE}/trace/layering/misc", {"path": child["path"], "move_to_misc": True})
            self.assertEqual(st, 200, res)
            self.assertEqual(res["scope"], "path")
            self.assertIn(child["path_key"], res["misc_paths"])
            again = self.svc.get_layering(TRACE_CASE, source_entity_id=src)
            self.assertIn(child["path_key"], again["misc_paths"])
            st, res = self.jpost(f"/api/cases/{TRACE_CASE}/trace/layering/misc",
                                 {"path": child["path_key"], "move_to_misc": False})
            self.assertEqual(res["misc_paths"], [])
        self.assertEqual(self.jpost(f"/api/cases/{TRACE_CASE}/trace/layering/misc", {"path": ["nope", "x"]})[0], 404)
        self.assertEqual(self.jpost(f"/api/cases/{TRACE_CASE}/trace/layering/misc", {})[0], 400)

    def test_root_at_credit(self) -> None:
        credit = next(t for t in self.svc.vault.get_transactions(TRACE_CASE)
                      if t.direction.value == "CR" and t.resolved_entity_id)
        st, tree = self.jget(f"/api/cases/{TRACE_CASE}/trace/layering?root_tx_id={credit.tx_id}")
        self.assertEqual(st, 200, tree)
        self.assertEqual(tree["root_tx_id"], credit.tx_id)
        self.assertEqual(tree["root_credit"]["tx_id"], credit.tx_id)
        self.assertEqual(tree["source_key"], f"tx:{credit.tx_id}")
        self.assertEqual(self.jget(f"/api/cases/{TRACE_CASE}/trace/layering?root_tx_id=nope")[0], 400)
        debit = next(t for t in self.svc.vault.get_transactions(TRACE_CASE) if t.direction.value == "DR")
        self.assertEqual(self.jget(f"/api/cases/{TRACE_CASE}/trace/layering?root_tx_id={debit.tx_id}")[0], 400)

    def test_bidirectional_and_loops_params(self) -> None:
        st, body = self.jget(f"/api/cases/{TRACE_CASE}/trace/bidirectional?rapid_window_hours=24&amount_tolerance=0.05")
        self.assertEqual(st, 200)
        if body["selected_detail"]:
            self.assertEqual(body["selected_detail"]["parameters"]["rapid_window_hours"], 24.0)
            self.assertIn("round_trips", body["selected_detail"])
        self.assertEqual(self.jget(f"/api/cases/{TRACE_CASE}/trace/bidirectional?amount_tolerance=-1")[0], 400)
        self.assertEqual(self.jget(f"/api/cases/{TRACE_CASE}/trace/bidirectional?a=nope&b=nada")[0], 404)
        st, body = self.jget(f"/api/cases/{TRACE_CASE}/trace/loops?max_instances_per_loop=2")
        self.assertEqual(st, 200)
        for lp in body["loops"]:
            self.assertLessEqual(len(lp.get("instances") or []), 2)
        self.assertEqual(self.jget(f"/api/cases/{TRACE_CASE}/trace/loops?max_instances_per_loop=0")[0], 400)

    def test_graph_ifsc_list_and_amount(self) -> None:
        st, g = self.jget(f"/api/cases/{TRACE_CASE}/trace/graph?ifsc=SBIN,HDFC&max_amount=100000&min_amount=10")
        self.assertEqual(st, 200)
        self.assertEqual(sorted(g["summary"]["filters"]["ifsc"]), ["HDFC", "SBIN"])
        self.assertIn("colour_by", g["summary"])
        self.assertEqual(self.jget(f"/api/cases/{TRACE_CASE}/trace/graph?ifsc=bad!")[0], 400)
        self.assertEqual(self.jget(f"/api/cases/{TRACE_CASE}/trace/graph?min_amount=10&max_amount=5")[0], 400)


class ProfileExportTests(_Base):
    def test_profile_exports(self) -> None:
        ent = next(e for e in self.svc.vault.get_entities(TRACE_CASE) if e.has_statement)
        for fmt, ctype, magic in (("html", "text/html", b"<"), ("xlsx", XLSX, b"PK"), ("pdf", "application/pdf", b"%PDF")):
            st, h, data = self.req("GET", f"/api/cases/{TRACE_CASE}/export/profile?entity_id={ent.entity_id}&format={fmt}")
            self.assertEqual(st, 200, fmt)
            self.assertTrue(h["content-type"].startswith(ctype), h["content-type"])
            self.assertIn("attachment", h["content-disposition"])
            self.assertTrue(data.lstrip()[:len(magic)] == magic, fmt)
            if fmt == "html":
                self.assertIn("sandbox", h.get("content-security-policy", ""))
        self.assertEqual(self.req("GET", f"/api/cases/{TRACE_CASE}/export/profile?format=doc")[0], 400)
        self.assertEqual(self.req("GET", f"/api/cases/{TRACE_CASE}/export/profile?entity_id=nope&format=pdf")[0], 404)
        actions = [r["action"] for r in self.svc.vault.get_audit_logs(TRACE_CASE, limit=10)]
        self.assertIn("EXPORT_PROFILE_PDF", actions)


class HttpHardeningTests(_Base):
    def test_new_endpoints_require_token_and_json(self) -> None:
        for method, path in (("GET", f"/api/cases/{TRACE_CASE}/entities/preview"),
                             ("GET", f"/api/cases/{EDGE_CASE}/edge/tally-map"),
                             ("GET", f"/api/cases/{TRACE_CASE}/export/profile?format=html"),
                             ("POST", "/api/draw/page-image"), ("POST", f"/api/cases/{TRACE_CASE}/dedup"),
                             ("PUT", f"/api/cases/{EDGE_CASE}/edge/tally-map")):
            st, _h, _d = self.req(method, path, {} if method != "GET" else None, token=False)
            self.assertEqual(st, 403, (method, path))
        st, _h, _d = self.req("POST", f"/api/cases/{TRACE_CASE}/dedup", "apply=1",
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(st, 415)
        st, h, _d = self.req("GET", "/", token=False)
        self.assertEqual(h.get("content-security-policy"), CONTENT_SECURITY_POLICY)

    def test_tx_to_json_helper(self) -> None:
        t = self.svc.vault.get_transactions(EDGE_CASE)[0]
        d = tx_to_json(t)
        self.assertIn("payment_channel_srs", d)
        self.assertIn("card_number_masked", d)
        self.assertIn("cheque_number", d)
        self.assertFalse(math.isnan(float(d["debit"])))


if __name__ == "__main__":
    unittest.main()
