"""P0 integration regressions: service/storage wiring, atomic ingest, custody and audit, caching (group: integrate).

Covers REVIEW_2026-11 P0 items 2 (service remainder), 10, 11, 13 and the BE-07/BE-08 quick wins.
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import sqlite3
import stat
import sys
import tempfile
import threading
import unittest
from decimal import Decimal
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol import service as service_mod  # noqa: E402
from cyphersol import storage as storage_mod  # noqa: E402
from cyphersol.models import ConflictError, InvestigationCase, WorkspaceMode  # noqa: E402
from cyphersol.server import create_http_handler  # noqa: E402
from cyphersol.service import CypherApplicationService  # noqa: E402
from cyphersol.storage import CypherVault  # noqa: E402

NAMES = ["RAHUL VERMA", "SUNITA RAO", "AMIT KHANNA", "PRIYA NAIR", "VIKRAM SETH", "NEHA JOSHI", "ARJUN DAS"]


def make_csv(n: int, seed: int = 0, start_balance: float = 100000.0) -> bytes:
    """A small, internally consistent bank CSV; ``seed`` makes the bytes (and SHA-256) unique."""
    lines = ["Date,Narration,Debit,Credit,Balance"]
    bal = start_balance
    for i in range(n):
        day = 1 + (i % 27)
        month = 4 + (i // 27) % 9
        name = NAMES[(i + seed) % len(NAMES)]
        if i % 3 == 0:
            amt = 1000 + 7 * i + seed
            bal += amt
            lines.append(f"{day:02d}/{month:02d}/2025,UPI/CR/{400000000000 + i * 13 + seed}/{name}/x{i}@okaxis,,{amt:.2f},{bal:.2f}")
        else:
            amt = 300 + 3 * i + seed
            bal -= amt
            lines.append(f"{day:02d}/{month:02d}/2025,UPI/DR/{500000000000 + i * 17 + seed}/{name}/y{i}@ybl,{amt:.2f},,{bal:.2f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


BROKEN_CSV = "\n".join([
    "Date,Narration,Debit,Credit,Balance",
    "01/04/2026,OPENING DEPOSIT,,10000.00,10000.00",
    "02/04/2026,UNKNOWN_OPAQUE_WIRE_XYZ,,1500.00,8500.00",
    "03/04/2026,NEFT TO SUPPLIER ABC,500.00,,8000.00",
]).encode("utf-8")


class _Transport:
    """OpenAI-shaped mock transport; replies by agent, records calls."""

    def __init__(self, stage1_rows=None, repair=True):
        self.calls = []
        self.stage1_rows = stage1_rows
        self.repair = repair

    def __call__(self, url, headers, body, timeout):
        self.calls.append({"url": url, "body": body})
        sys_txt = (body.get("messages") or [{}])[0].get("content", "")
        if "connectivity probe" in sys_txt:
            payload = {"status": "ok"}
        elif "Agent 1" in sys_txt:
            payload = {"bank_code": "HDFC", "bank_name": "HDFC Bank", "account_number": "501001",
                       "account_holder_name": "TEST HOLDER", "rows": self.stage1_rows or []}
        elif "Agent 2" in sys_txt:
            payload = {"repairs": [{"row_index": 1, "debit": 1500.0, "credit": 0.0, "running_balance": 8500.0}]} if self.repair else {}
        else:
            payload = {}
        return {"choices": [{"message": {"content": json.dumps(payload)}, "finish_reason": "stop"}]}


def _clean_env():
    env = dict(os.environ)
    for k in ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CYPHERSOL_ALLOW_CLOUD_LLM",
              "CYPHERSOL_EXECUTION_MODE", "CYPHERSOL_LLM_PROVIDER", "CYPHERSOL_DB_KEY", "GOOGLE_API_KEY"):
        env.pop(k, None)
    return env


class _Base(unittest.TestCase):
    def setUp(self):
        self._env = mock.patch.dict(os.environ, _clean_env(), clear=True)
        self._env.start()
        self.tmp = tempfile.TemporaryDirectory(dir="/private/tmp/claude-501/cyphersol-p0")
        self.db = os.path.join(self.tmp.name, "vault.db")
        self.transport = _Transport()
        self.svc = CypherApplicationService(self.db, self.tmp.name, seed_demo=False, llm_transport_fn=self.transport)
        self.cid = self.svc.create_case("Integrate", "cypher_edge", "Test")["case_id"]

    def tearDown(self):
        self._env.stop()
        self.tmp.cleanup()

    def actions(self, case_id=None):
        return [r["action"] for r in self.svc.vault.get_audit_logs(case_id or self.cid, limit=1000)]


os.makedirs("/private/tmp/claude-501/cyphersol-p0", exist_ok=True)


# ---------------------------------------------------------------------------------------------------------
# P0 item 2 remainder: execution_mode pass-through
# ---------------------------------------------------------------------------------------------------------
class ExecutionModeTests(_Base):
    def _with_key(self, mode):
        self.svc.update_llm_config({"provider": "openai", "api_key": "sk-test-0001", "execution_mode": mode})

    def test_run_agents_none_uses_saved_heuristics_mode(self):
        self.svc.ingest_statements(self.cid, [("a.csv", make_csv(6), None)])
        self._with_key("heuristics")
        before = len(self.transport.calls)
        res = self.svc.run_case_llm_agents(self.cid, execution_mode=None)
        self.assertEqual(res["agent_telemetry"]["execution_mode"], "heuristics")
        self.assertEqual(res["agent_telemetry"]["stages_invoked"], [])
        self.assertEqual(len(self.transport.calls), before, "no cloud call when the saved mode is heuristics")

    def test_run_agents_none_uses_saved_both_mode(self):
        self.svc.ingest_statements(self.cid, [("broken.csv", BROKEN_CSV, None)])
        self._with_key("both")
        res = self.svc.run_case_llm_agents(self.cid, execution_mode=None, stages=[2])
        self.assertEqual(res["agent_telemetry"]["execution_mode"], "both")


# ---------------------------------------------------------------------------------------------------------
# P0 item 10: bulk resilience, atomicity, duplicates, UPSERT, locking, settings as arguments
# ---------------------------------------------------------------------------------------------------------
class IngestResilienceTests(_Base):
    def test_one_bad_file_does_not_abort_batch_and_is_persisted(self):
        res = self.svc.ingest_statements(
            self.cid, [("good.csv", make_csv(9), None), ("broken.pdf", b"%PDF-1.4 garbage \x00\x01", None)]
        )
        statuses = {r["filename"]: r["status"] for r in res["ingested"]}
        self.assertEqual(statuses["good.csv"], "ok")
        self.assertEqual(statuses["broken.pdf"], "failed")
        self.assertEqual(res["files_ok"], 1)
        self.assertEqual(res["files_failed"], 1)
        self.assertEqual(len(self.svc.vault.get_statements(self.cid)), 1)
        persisted = {r["filename"]: r for r in self.svc.vault.get_ingest_results(self.cid)}
        self.assertEqual(persisted["broken.pdf"]["status"], "failed")
        self.assertTrue(persisted["broken.pdf"]["error"])
        self.assertEqual(persisted["good.csv"]["status"], "ok")
        self.assertEqual(persisted["good.csv"]["transactions"], 9)
        ingest_log = next(r for r in self.svc.vault.get_audit_logs(self.cid) if r["action"] == "INGEST_STATEMENTS")
        self.assertEqual(ingest_log["details"]["failed"][0]["filename"], "broken.pdf")

    def test_duplicate_sha_raises_conflict_and_writes_nothing(self):
        data = make_csv(5, seed=3)
        self.svc.ingest_statements(self.cid, [("first.csv", data, None)])
        n_tx = len(self.svc.vault.get_transactions(self.cid))
        with self.assertRaises(ConflictError):
            self.svc.ingest_statements(self.cid, [("renamed.csv", data, None), ("other.csv", make_csv(4, seed=8), None)])
        self.assertEqual(len(self.svc.vault.get_statements(self.cid)), 1)
        self.assertEqual(len(self.svc.vault.get_transactions(self.cid)), n_tx)
        # Same file twice in one upload is also a conflict
        with self.assertRaises(ConflictError):
            self.svc.ingest_statements(self.cid, [("x.csv", make_csv(3, seed=11), None), ("y.csv", make_csv(3, seed=11), None)])
        # Another case may hold the same file
        other = self.svc.create_case("Other", "cypher_edge")["case_id"]
        self.assertEqual(self.svc.ingest_statements(other, [("first.csv", data, None)])["files_ok"], 1)

    def test_duplicate_skip_policy_reports_and_continues(self):
        data = make_csv(5, seed=4)
        self.svc.ingest_statements(self.cid, [("first.csv", data, None)])
        res = self.svc.ingest_statements(
            self.cid, [("again.csv", data, None), ("new.csv", make_csv(4, seed=9), None)], on_duplicate="skip"
        )
        statuses = {r["filename"]: r["status"] for r in res["ingested"]}
        self.assertEqual(statuses, {"again.csv": "duplicate", "new.csv": "ok"})
        self.assertEqual(len(self.svc.vault.get_statements(self.cid)), 2)

    def test_storage_rejects_duplicate_inside_transaction(self):
        data = make_csv(4, seed=5)
        self.svc.ingest_statements(self.cid, [("a.csv", data, None)])
        # Bypass the service pre-check (simulates a concurrent upload that raced past it)
        with mock.patch.object(CypherVault, "find_statements_by_sha", return_value={}):
            with self.assertRaises(ConflictError):
                self.svc.ingest_statements(self.cid, [("b.csv", data, None)])
        self.assertEqual(len(self.svc.vault.get_statements(self.cid)), 1)

    def test_empty_earlier_upload_is_superseded_on_retry(self):
        blank = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
        r1 = self.svc.ingest_statements(self.cid, [("scan.png", blank, None)])
        self.assertEqual(r1["files_ok"], 1)
        old_id = self.svc.vault.get_statements(self.cid)[0].statement_id
        r2 = self.svc.ingest_statements(self.cid, [("scan.png", blank, None)])
        self.assertEqual(r2["files_ok"], 1)
        stmts = self.svc.vault.get_statements(self.cid)
        self.assertEqual(len(stmts), 1)
        self.assertNotEqual(stmts[0].statement_id, old_id)
        prov = self.svc.vault.get_statement_provenance(self.cid)
        self.assertEqual(len(prov), 2, "custody rows of both attempts remain")
        self.assertEqual(prov[1]["details"]["superseded_statement_id"], old_id)

    def test_ingest_is_atomic_when_persistence_fails(self):
        with mock.patch.object(CypherVault, "_replace_entities_conn", side_effect=RuntimeError("disk full")):
            with self.assertRaises(RuntimeError):
                self.svc.ingest_statements(self.cid, [("a.csv", make_csv(6, seed=1), None), ("b.csv", make_csv(6, seed=2), None)])
        self.assertEqual(self.svc.vault.get_statements(self.cid), [])
        self.assertEqual(self.svc.vault.get_transactions(self.cid), [])
        self.assertEqual(self.svc.vault.get_statement_provenance(self.cid), [])
        results = self.svc.vault.get_ingest_results(self.cid)
        self.assertEqual({r["filename"] for r in results}, {"a.csv", "b.csv"})
        self.assertTrue(all(r["status"] == "failed" for r in results))
        self.assertIn("INGEST_BATCH_REJECTED", self.actions())
        self.assertNotIn("INGEST_STATEMENTS", self.actions())
        # A later retry works (nothing half-written blocks it)
        self.assertEqual(self.svc.ingest_statements(self.cid, [("a.csv", make_csv(6, seed=1), None)])["files_ok"], 1)

    def test_save_case_upsert_never_cascades(self):
        self.svc.ingest_statements(self.cid, [("a.csv", make_csv(7), None)])
        case = self.svc.vault.get_case(self.cid)
        case.title = "Renamed"
        self.svc.vault.save_case(case)
        self.assertEqual(self.svc.vault.get_case(self.cid).title, "Renamed")
        self.assertEqual(len(self.svc.vault.get_statements(self.cid)), 1)
        self.assertEqual(len(self.svc.vault.get_transactions(self.cid)), 7)
        self.assertTrue(self.svc.vault.get_entities(self.cid))

    def test_statement_upsert_keeps_identity_immutable(self):
        self.svc.ingest_statements(self.cid, [("a.csv", make_csv(4), None)])
        st = self.svc.vault.get_statements(self.cid)[0]
        original_sha = st.sha256_hash
        st.balance_mismatch_count = 7
        st.sha256_hash = "f" * 64  # ignored by the UPSERT (identity columns excluded)
        self.svc.vault.save_statement_and_transactions(st, [])
        again = self.svc.vault.get_statements(self.cid)[0]
        self.assertEqual(again.balance_mismatch_count, 7)
        self.assertEqual(again.sha256_hash, original_sha)
        with sqlite3.connect(self.db) as conn, self.assertRaises(sqlite3.DatabaseError):
            conn.execute("UPDATE statements SET sha256_hash = 'x' WHERE statement_id = ?", (st.statement_id,))

    def test_concurrent_resolutions_do_not_lose_updates(self):
        self.svc.ingest_statements(self.cid, [("p2p.csv", make_csv(21), None)])
        suspense = [t for t in self.svc.vault.get_transactions(self.cid) if t.is_suspense and t.direction.value == "DR"]
        self.assertGreaterEqual(len(suspense), 6)
        targets = suspense[:6]
        errors = []

        def worker(tx_id):
            try:
                self.svc.resolve_suspense(self.cid, tx_id, 44, auto_refresh_similar=False)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(t.tx_id,)) for t in targets]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        by_id = {t.tx_id: t for t in self.svc.vault.get_transactions(self.cid)}
        for t in targets:
            self.assertEqual(by_id[t.tx_id].category_id, 44, t.tx_id)
            self.assertEqual(by_id[t.tx_id].categorization_source, "USER_RULE")

    def test_case_threshold_passed_as_argument_not_mutating_shared_categorizer(self):
        base_tau = self.svc.categorizer.confidence_threshold
        self.svc.vault.update_case_thresholds(self.cid, confidence_threshold=0.97)
        self.svc.ingest_statements(self.cid, [("a.csv", make_csv(6), None)])
        self.assertEqual(self.svc.categorizer.confidence_threshold, base_tau)
        self.assertTrue(all(t.confidence_score < 0.97 or not t.is_suspense for t in self.svc.vault.get_transactions(self.cid)))

    def test_learned_rule_is_case_scoped_and_propagation_previewed(self):
        self.svc.ingest_statements(self.cid, [("p2p.csv", make_csv(21), None)])
        susp = [t for t in self.svc.vault.get_transactions(self.cid) if t.is_suspense]
        anchor = susp[0]
        preview = self.svc.preview_resolution(self.cid, anchor.tx_id)
        self.assertIn("rule_key", preview)
        res = self.svc.resolve_suspense(self.cid, anchor.tx_id, 44, confirmed_tx_ids=preview["tx_ids"][:1])
        self.assertLessEqual(res["updated_count"], 2)
        rules = self.svc.vault.get_user_rules()
        self.assertTrue(rules)
        self.assertTrue(all(len(r) == 5 for r in rules))
        self.assertEqual({r[4] for r in rules}, {self.cid})
        other = self.svc.create_case("Other", "cypher_edge")["case_id"]
        self.assertTrue(all(r[4] is None for r in self.svc.vault.get_user_rules(other)))


# ---------------------------------------------------------------------------------------------------------
# P0 item 11: provenance, audit hash chain, missing audit events, in-place migration
# ---------------------------------------------------------------------------------------------------------
LEGACY_DDL = """
CREATE TABLE cases (case_id TEXT PRIMARY KEY, title TEXT NOT NULL, mode TEXT NOT NULL, agency_or_firm_name TEXT,
  created_at TEXT NOT NULL, similarity_threshold REAL NOT NULL DEFAULT 0.85, confidence_threshold REAL NOT NULL DEFAULT 0.65);
CREATE TABLE statements (statement_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, source_filename TEXT NOT NULL,
  sha256_hash TEXT NOT NULL, bank_code TEXT NOT NULL, bank_name TEXT NOT NULL, account_number TEXT NOT NULL, ifsc_code TEXT,
  account_holder_name TEXT NOT NULL, account_type TEXT, period_start TEXT NOT NULL, period_end TEXT NOT NULL,
  opening_balance REAL NOT NULL, closing_balance REAL NOT NULL, total_transactions INTEGER NOT NULL,
  balance_mismatch_count INTEGER NOT NULL DEFAULT 0, duplicate_suspect_count INTEGER NOT NULL DEFAULT 0,
  is_ocr_derived INTEGER NOT NULL DEFAULT 0, ingested_at TEXT NOT NULL,
  FOREIGN KEY (case_id) REFERENCES cases(case_id) ON DELETE CASCADE);
CREATE TABLE entities (entity_id TEXT PRIMARY KEY, case_id TEXT NOT NULL, canonical_name TEXT NOT NULL,
  aliases_json TEXT NOT NULL, linked_accounts_json TEXT NOT NULL, linked_vpas_json TEXT NOT NULL,
  has_statement INTEGER NOT NULL DEFAULT 0, is_moved_to_misc INTEGER NOT NULL DEFAULT 0, total_credited REAL NOT NULL DEFAULT 0,
  total_debited REAL NOT NULL DEFAULT 0, tx_count INTEGER NOT NULL DEFAULT 0);
CREATE TABLE user_rules (rule_id TEXT PRIMARY KEY, pattern TEXT NOT NULL, direction TEXT NOT NULL, category_id INTEGER NOT NULL,
  custom_ledger_name TEXT, created_at TEXT NOT NULL, UNIQUE(pattern, direction));
CREATE TABLE audit_logs (log_id TEXT PRIMARY KEY, case_id TEXT, actor TEXT NOT NULL, action TEXT NOT NULL,
  details_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE llm_settings (setting_key TEXT PRIMARY KEY, setting_value_json TEXT NOT NULL, updated_at TEXT NOT NULL);
"""


class CustodyAndAuditTests(_Base):
    def test_provenance_row_is_complete_and_append_only(self):
        data = make_csv(5)
        self.svc.ingest_statements(self.cid, [("a.csv", data, None)])
        prov = self.svc.vault.get_statement_provenance(self.cid)
        self.assertEqual(len(prov), 1)
        p = prov[0]
        import hashlib
        self.assertEqual(p["sha256"], hashlib.sha256(data).hexdigest())
        self.assertEqual(len(p["sha256"]), 64)
        self.assertEqual(p["file_size_bytes"], len(data))
        self.assertTrue(p["parser_version"].startswith("cyphersol-"))
        self.assertTrue(p["analyst"])
        self.assertTrue(p["ingested_at"])
        with sqlite3.connect(self.db) as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE statement_provenance SET sha256 = 'x'")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM statement_provenance")

    def test_audit_log_hash_chain_and_abort_triggers(self):
        self.svc.ingest_statements(self.cid, [("a.csv", make_csv(4), None)])
        chain = self.svc.vault.verify_audit_chain()
        self.assertTrue(chain["ok"], chain)
        self.assertGreaterEqual(chain["count"], 2)
        with sqlite3.connect(self.db) as conn:
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("UPDATE audit_logs SET action = 'X'")
            with self.assertRaises(sqlite3.DatabaseError):
                conn.execute("DELETE FROM audit_logs")
            with self.assertRaises(sqlite3.DatabaseError):  # breaks the chain
                conn.execute(
                    "INSERT INTO audit_logs (log_id, case_id, actor, action, details_json, created_at, prev_hash, row_hash) "
                    "VALUES ('forged', NULL, 'x', 'FORGED', '{}', 't', ?, ?)", ("0" * 64, "a" * 64)
                )
        # Tampering behind the triggers' back is detected by verification
        with sqlite3.connect(self.db) as conn:
            conn.execute("DROP TRIGGER trg_audit_no_update")
            conn.execute("UPDATE audit_logs SET details_json = '{}' WHERE action = 'INGEST_STATEMENTS'")
        bad = self.svc.vault.verify_audit_chain()
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["reason"], "row_hash")

    def test_missing_audit_events_are_logged_without_secrets(self):
        self.svc.save_draw_template("HDFC", "HDFC layout", [0, 0, 1, 1], [0.2, 0.5], ["date", "narration", "debit"], case_id=self.cid)
        self.assertIn("DRAW_TEMPLATE_SAVED", self.actions())
        self.svc.update_llm_config({"provider": "openai", "api_key": "sk-supersecret-1234", "execution_mode": "both"})
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute("SELECT details_json FROM audit_logs WHERE action = 'LLM_CONFIG_UPDATED'").fetchall()
        self.assertEqual(len(rows), 1)
        self.assertNotIn("supersecret", rows[0][0])
        self.assertEqual(json.loads(rows[0][0])["key_source"], "vault")

        self.svc.ingest_statements(self.cid, [("a.csv", make_csv(8), None)], execution_mode="heuristics")
        self.svc.export_tally_xml(self.cid)
        tally_log = next(r for r in self.svc.vault.get_audit_logs(self.cid) if r["action"] == "TALLY_XML_EXPORT")
        self.assertEqual(len(tally_log["details"]["xml_sha256"]), 64)

        self.svc.recluster_entities(self.cid, manual_merge=("Sunita R", "SUNITA RAO"))
        merge_log = next(r for r in self.svc.vault.get_audit_logs(self.cid) if r["action"] == "MANUAL_MERGE")
        self.assertEqual(merge_log["details"], {"alias": "SUNITA R", "canonical": "SUNITA RAO"})

    def test_llm_repairs_logged_with_before_and_after(self):
        self.svc.update_llm_config({"provider": "openai", "api_key": "sk-test-0002", "execution_mode": "both"})
        self.svc.ingest_statements(self.cid, [("broken.csv", BROKEN_CSV, None)])
        rep = [r for r in self.svc.vault.get_audit_logs(self.cid) if r["action"] == "LLM_STAGE2_REPAIR"]
        self.assertEqual(len(rep), 1)
        repair = rep[0]["details"]["repairs"][0]
        self.assertEqual(repair["original"]["credit"], "1500.00")
        self.assertEqual(repair["repaired"]["debit"], "1500.00")
        self.assertNotIn("sk-test-0002", json.dumps(rep[0]))
        # The dossier then says cloud AI was used
        llm_used, provider = self.svc._llm_provenance(self.cid)
        self.assertTrue(llm_used)
        self.assertEqual(provider, "openai")
        self.assertIn("Cloud AI assistance (openai)", self.svc.export_dossier_html(self.cid))

    def test_dossier_heuristic_case_is_deterministic(self):
        self.svc.ingest_statements(self.cid, [("a.csv", make_csv(5), None)])
        self.assertEqual(self.svc._llm_provenance(self.cid), (False, None))
        html_doc = self.svc.export_dossier_html(self.cid)
        self.assertNotIn("air-gapped", html_doc.lower())
        self.assertNotIn("Cloud AI assistance", html_doc)

    def test_legacy_vault_migrates_in_place_without_data_loss(self):
        path = os.path.join(self.tmp.name, "legacy.db")
        with sqlite3.connect(path) as conn:
            conn.executescript(LEGACY_DDL)
            conn.execute("INSERT INTO cases VALUES ('C1','Old','cypher_edge',NULL,'2025-01-01',0.85,0.65)")
            conn.execute(
                "INSERT INTO statements VALUES ('S1','C1','old.csv',?, 'HDFC','HDFC Bank','123','HDFC0000001','X',"
                "'Savings','2025-04-01','2025-04-30',0,0,3,0,0,0,'2025-05-01')", ("ab" * 32,)
            )
            conn.execute("INSERT INTO entities VALUES ('E1','C1','X','[]','[]','[]',1,0,10,5,3)")
            conn.execute("INSERT INTO user_rules VALUES ('r1','zomato','DR',9,NULL,'2025-01-01')")
            for i in range(3):
                conn.execute("INSERT INTO audit_logs VALUES (?, 'C1', 'local_operator', 'OLD', '{}', ?)", (f"L{i}", f"2025-01-0{i + 1}"))
        v = CypherVault(path)
        self.assertEqual(v.get_case("C1").title, "Old")
        self.assertEqual(len(v.get_statements("C1")), 1)
        self.assertEqual(v.get_entities("C1")[0].canonical_name, "X")
        self.assertEqual(v.get_user_rules(), [("zomato", "DR", 9, None, None)])
        self.assertEqual(len(v.get_audit_logs("C1")), 3)
        self.assertTrue(v.verify_audit_chain()["ok"])
        prov = v.get_statement_provenance("C1")
        self.assertEqual(len(prov), 1)
        self.assertTrue(prov[0]["details"]["backfilled"])
        v.log_audit("C1", "NEW", {"a": 1})
        self.assertTrue(v.verify_audit_chain()["ok"])
        v.save_user_rule("party:sunita rao", "DR", 44, case_id="C1")
        v.save_user_rule("party:sunita rao", "DR", 45, case_id=None)
        self.assertEqual(len(v.get_user_rules()), 3)
        self.assertEqual(v.get_case_revision("C1"), 0)
        # Re-opening is idempotent
        v2 = CypherVault(path)
        self.assertEqual(len(v2.get_statement_provenance("C1")), 1)
        self.assertTrue(v2.verify_audit_chain()["ok"])


# ---------------------------------------------------------------------------------------------------------
# P0 item 13: file modes, optional SQLCipher, environment keys never persisted
# ---------------------------------------------------------------------------------------------------------
class VaultSecurityTests(_Base):
    def test_new_vault_file_and_dir_are_owner_only(self):
        new_dir = os.path.join(self.tmp.name, "fresh", "nested")
        path = os.path.join(new_dir, "v.db")
        CypherVault(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(new_dir).st_mode), 0o700)

    def test_existing_dir_mode_untouched(self):
        mode_before = stat.S_IMODE(os.stat(self.tmp.name).st_mode)
        CypherVault(os.path.join(self.tmp.name, "other.db"))
        self.assertEqual(stat.S_IMODE(os.stat(self.tmp.name).st_mode), mode_before)

    def test_db_key_without_sqlcipher_warns_and_reports_unencrypted(self):
        with mock.patch.object(storage_mod, "_sqlcipher", None), mock.patch.dict(os.environ, {"CYPHERSOL_DB_KEY": "k"}):
            with self.assertLogs("cyphersol.storage", level=logging.WARNING):
                v = CypherVault(os.path.join(self.tmp.name, "k.db"))
        self.assertFalse(v.encrypted)

    def test_db_key_uses_sqlcipher_pragma_key_when_available(self):
        executed = []
        real_connect = sqlite3.connect

        class _Conn:
            def __init__(self, inner):
                self._inner = inner
                self.row_factory = None

            def execute(self, sql, *a):
                executed.append(sql)
                if sql.startswith("PRAGMA key"):
                    return None
                return self._inner.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._inner, name)

            def __setattr__(self, name, value):
                if name in ("_inner",):
                    object.__setattr__(self, name, value)
                elif name == "row_factory":
                    object.__setattr__(self, name, value)
                    if hasattr(self, "_inner"):
                        self._inner.row_factory = value
                else:
                    setattr(self._inner, name, value)

            def __enter__(self):
                return self._inner.__enter__()

            def __exit__(self, *a):
                return self._inner.__exit__(*a)

        fake = mock.MagicMock()
        fake.connect = lambda p, check_same_thread=False: _Conn(real_connect(p, check_same_thread=check_same_thread))
        fake.Row = sqlite3.Row
        with mock.patch.object(storage_mod, "_sqlcipher", fake), mock.patch.dict(os.environ, {"CYPHERSOL_DB_KEY": "o'k"}):
            v = CypherVault(os.path.join(self.tmp.name, "enc.db"))
            v.log_audit(None, "X", {})
        self.assertTrue(v.encrypted)
        self.assertIn("PRAGMA key = 'o''k'", executed)

    def test_environment_keys_never_persisted(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-from-env-777", "CYPHERSOL_ALLOW_CLOUD_LLM": "1"}):
            svc = CypherApplicationService(os.path.join(self.tmp.name, "env.db"), self.tmp.name, seed_demo=False)
            self.assertEqual(svc.llm_config.key_source("openai"), "env")
            svc.update_llm_config({"provider": "openai", "execution_mode": "both"})
            self.assertEqual(svc.llm_config.key_source("openai"), "env")
            raw = json.dumps(svc.vault.get_llm_settings())
            self.assertNotIn("sk-from-env-777", raw)
            svc.update_llm_config({"provider": "openai", "clear_api_key": True})
            self.assertFalse(svc.llm_config.has_valid_key("openai"))
            # 'clear key' survives a restart even with the env opt-in
            again = CypherApplicationService(os.path.join(self.tmp.name, "env.db"), self.tmp.name, seed_demo=False)
            self.assertFalse(again.llm_config.has_valid_key("openai"))
        with sqlite3.connect(os.path.join(self.tmp.name, "env.db")) as conn:
            dump = "\n".join(conn.iterdump())
        self.assertNotIn("sk-from-env-777", dump)

    def test_docstring_no_longer_claims_encryption(self):
        self.assertNotIn("encrypted-vault", storage_mod.__doc__)


# ---------------------------------------------------------------------------------------------------------
# LLM handoffs: Stage 1 adoption guard, Stage 4 threshold
# ---------------------------------------------------------------------------------------------------------
class LlmWiringTests(_Base):
    def test_llm_mode_keeps_better_deterministic_parse(self):
        self.transport.stage1_rows = [{"date": "2025-04-01", "narration": "ONE ROW", "debit": 0, "credit": 5, "balance": 5}]
        self.transport.repair = False
        self.svc.update_llm_config({"provider": "openai", "api_key": "sk-test-0003", "execution_mode": "llm"})
        res = self.svc.ingest_statements(self.cid, [("a.csv", make_csv(6), None)])
        self.assertEqual(res["ingested"][0]["transactions"], 6)
        self.assertNotIn(1, res["agent_telemetry"]["stages_invoked"])
        self.assertTrue(any("deterministic" in w for w in res["ingested"][0]["warnings"]))

    def test_stage4_receives_case_confidence_threshold(self):
        self.svc.update_llm_config({"provider": "openai", "api_key": "sk-test-0004", "execution_mode": "both"})
        self.svc.vault.update_case_thresholds(self.cid, confidence_threshold=0.91)
        seen = {}
        from cyphersol.llm_agents import Stage4CategorizationEntityAgent
        real = Stage4CategorizationEntityAgent.classify_and_resolve

        def spy(agent, *a, **kw):
            seen["tau"] = kw.get("confidence_threshold")
            return real(agent, *a, **kw)

        with mock.patch.object(Stage4CategorizationEntityAgent, "classify_and_resolve", spy):
            self.svc.ingest_statements(self.cid, [("a.csv", make_csv(6), None)])
        self.assertEqual(seen.get("tau"), 0.91)


# ---------------------------------------------------------------------------------------------------------
# BE-07 / BE-08: analysis cache keyed by revision, slim overview, paginated transactions
# ---------------------------------------------------------------------------------------------------------
class CacheAndPaginationTests(_Base):
    def test_analysis_cache_reused_until_mutation(self):
        self.svc.ingest_statements(self.cid, [("a.csv", make_csv(12), None)])
        calls = {"graph": 0, "mules": 0}
        real_graph, real_mules = service_mod.build_network_graph, service_mod.evaluate_all_mule_accounts

        def g(*a, **k):
            calls["graph"] += 1
            return real_graph(*a, **k)

        def m(*a, **k):
            calls["mules"] += 1
            return real_mules(*a, **k)

        with mock.patch.object(service_mod, "build_network_graph", g), mock.patch.object(service_mod, "evaluate_all_mule_accounts", m):
            self.svc.get_network_graph(self.cid)
            self.svc.get_network_graph(self.cid)
            self.svc.get_mule_assessments(self.cid)
            self.svc.get_financial_profile(self.cid)
            self.assertEqual(calls, {"graph": 1, "mules": 1})
            rev = self.svc.vault.get_case_revision(self.cid)
            tx = self.svc.vault.get_transactions(self.cid)[0]
            self.svc.toggle_duplicate_ignore(self.cid, tx.tx_id)
            self.assertGreater(self.svc.vault.get_case_revision(self.cid), rev)
            self.svc.get_network_graph(self.cid)
            self.assertEqual(calls["graph"], 2)
            self.svc.toggle_layering_misc(self.cid, self.svc.vault.get_entities(self.cid)[0].entity_id, True)
            self.svc.get_network_graph(self.cid)
            self.assertEqual(calls["graph"], 3)

    def test_overview_is_slim_and_transactions_paginate(self):
        self.svc.ingest_statements(self.cid, [("a.csv", make_csv(40), None)])
        ov = self.svc.get_case_overview(self.cid, tx_limit=10)
        self.assertEqual(len(ov["transactions"]), 10)
        self.assertEqual(ov["transactions_page"]["total"], 40)
        self.assertTrue(ov["transactions_page"]["has_more"])
        self.assertEqual(ov["summary"]["transactions_count"], 40)
        self.assertLessEqual(len(ov["suspense_transactions"]), 10)
        all_ids = []
        for off in range(0, 40, 15):
            page = self.svc.list_transactions(self.cid, offset=off, limit=15)
            all_ids += [t["tx_id"] for t in page["transactions"]]
        self.assertEqual(len(all_ids), 40)
        self.assertEqual(len(set(all_ids)), 40)
        q = self.svc.list_transactions(self.cid, q="SUNITA")
        self.assertTrue(q["transactions"])
        self.assertTrue(all("SUNITA" in t["raw_narration"] for t in q["transactions"]))
        susp = self.svc.list_transactions(self.cid, is_suspense=True)
        self.assertEqual(susp["total"], ov["summary"]["suspense_count"])
        cr = self.svc.list_transactions(self.cid, direction="CR")
        self.assertTrue(all(t["direction"] == "CR" for t in cr["transactions"]))

    def test_http_transactions_route_and_layering_mode_and_loops_meta(self):
        self.svc.ingest_statements(self.cid, [("a.csv", make_csv(20), None)])
        handler = create_http_handler(self.svc, os.path.join(SRC_DIR, "cyphersol", "ui"))
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            hdr = {"X-CypherSOL-Token": handler.api_token, "Host": f"127.0.0.1:{port}"}

            def get(path):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request("GET", path, headers=hdr)
                r = conn.getresponse()
                body = r.read()
                conn.close()
                return r.status, json.loads(body or b"{}")

            st, body = get(f"/api/cases/{self.cid}/transactions?limit=5&offset=5")
            self.assertEqual(st, 200)
            self.assertEqual(len(body["transactions"]), 5)
            self.assertEqual(body["total"], 20)
            st, body = get(f"/api/cases/{self.cid}/transactions?suspense=maybe")
            self.assertEqual(st, 400)
            st, body = get(f"/api/cases/{self.cid}/trace/loops?max_cycle_length=4")
            self.assertEqual(st, 200)
            self.assertIn("truncated", body)
            self.assertEqual(body["max_cycle_length"], 4)
            st, body = get(f"/api/cases/{self.cid}/trace/loops?max_cycle_length=12")
            self.assertEqual(st, 400)
            st, body = get(f"/api/cases/{self.cid}/trace/layering?mode=all")
            self.assertEqual(st, 200)
            st, body = get(f"/api/cases/{self.cid}/trace/layering?mode=bogus")
            self.assertEqual(st, 400)
            st, body = get(f"/api/cases/{self.cid}/edge/itr?fy=garbage")
            self.assertEqual(st, 400)
            st, body = get(f"/api/cases/{self.cid}/provenance")
            self.assertEqual(st, 200)
            self.assertEqual(len(body["provenance"]), 1)
            st, body = get("/api/cases/NOPE/transactions")
            self.assertEqual(st, 404)
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
