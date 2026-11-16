"""P2 performance work (NFR-PERF-01): behaviour-preserving optimisations and the new API surface.

Covers REVIEW_2026-11 P2 items 26-30 and 32: incremental duplicate index, worker-side narration enrichment,
lazy ML tier, incremental analysis snapshot, shared transfer events, node-capped graph with expand-on-demand,
loop hub exclusion and report memo, slim overview, background ingest jobs with staged per-file uploads, and the
analysis warm-up. Timing targets are measured by scripts/bench_p2.py, not asserted here.
"""

from __future__ import annotations

import base64
import copy
import http.client
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
import zlib
from http.server import ThreadingHTTPServer
from unittest import mock

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)
SCRIPTS_DIR = os.path.join(ROOT_DIR, "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from cyphersol import service as service_mod  # noqa: E402
from cyphersol.ingestion import parser as parser_mod  # noqa: E402
from cyphersol.ingestion.anomaly_gate import CaseDuplicateIndex, run_anomaly_gate  # noqa: E402
from cyphersol.ingestion.parser import bulk_parse_statements, parse_statement_bytes  # noqa: E402
from cyphersol.jobs import JobManager, UploadStore  # noqa: E402
from cyphersol.nlp.categorizer import HybridTransactionCategorizer  # noqa: E402
from cyphersol.nlp.narration_parser import enrich_transaction_narration  # noqa: E402
from cyphersol.server import create_http_handler  # noqa: E402
from cyphersol.service import CypherApplicationService  # noqa: E402
from cyphersol.trace import graph_engine  # noqa: E402
from cyphersol.trace.loop_detector import detect_circular_loops  # noqa: E402

from bench_p2 import build_case  # noqa: E402

UI_DIR = os.path.join(SRC_DIR, "cyphersol", "ui")
ENV_KEYS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CYPHERSOL_ALLOW_CLOUD_LLM",
            "CYPHERSOL_EXECUTION_MODE", "CYPHERSOL_LLM_PROVIDER")


def _clean_env():
    return {k: v for k, v in os.environ.items() if k not in ENV_KEYS}


class _CaseBase(unittest.TestCase):
    """One synthetic 12-statement case (seeded), ingested once per class."""

    FILES = 12
    ROWS = 160

    @classmethod
    def setUpClass(cls) -> None:
        cls.env_patch = mock.patch.dict(os.environ, _clean_env(), clear=True)
        cls.env_patch.start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.svc = CypherApplicationService(
            os.path.join(cls.tmp.name, "v.db"), os.path.join(cls.tmp.name, "s"), seed_demo=False
        )
        cls.cid = cls.svc.create_case("P2", "cypher_trace", "Bench")["case_id"]
        cls.files = build_case(cls.FILES, cls.ROWS, seed=11)
        cls.result = cls.svc.ingest_statements(cls.cid, cls.files, execution_mode="heuristics")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.env_patch.stop()
        cls.tmp.cleanup()


# ---------------------------------------------------------------------------------------------------------------
# Item 26 / 27: ingest hot paths
# ---------------------------------------------------------------------------------------------------------------
class IngestHotPathTests(unittest.TestCase):
    def test_bench_generator_is_deterministic(self) -> None:
        a = build_case(3, 50, seed=5)
        b = build_case(3, 50, seed=5)
        self.assertEqual([(f, d) for f, d, _ in a], [(f, d) for f, d, _ in b])
        self.assertEqual(len(a), 3)

    def test_case_duplicate_index_matches_full_rescan(self) -> None:
        files = build_case(3, 80, seed=3)
        parsed = [parse_statement_bytes(fn, raw, "C1") for fn, raw, _ in files]
        # Same rows re-exported as a second statement of account 0 -> duplicates across statements.
        dup_stmt, dup_txns, _ = parse_statement_bytes("again_" + files[0][0], files[0][1], "C1")
        parsed.append((dup_stmt, dup_txns, {}))

        def run(use_index: bool):
            batch = copy.deepcopy(parsed)
            seen, index, flags = [], CaseDuplicateIndex(), []
            for stmt, txns, _m in batch:
                if use_index:
                    _s, out, summary = run_anomaly_gate(stmt, txns, existing_index=index)
                    index.extend(out)
                else:
                    _s, out, summary = run_anomaly_gate(stmt, txns, existing_case_txns=list(seen))
                seen.extend(out)
                flags.append((summary["duplicate_suspect"], [[f.value for f in t.anomaly_flags] for t in out]))
            return flags

        legacy, indexed = run(False), run(True)
        self.assertEqual(legacy, indexed)
        self.assertGreater(indexed[-1][0], 0, "the re-exported statement must be flagged as duplicates")

    def test_existing_index_rejects_own_rows(self) -> None:
        stmt, txns, _ = parse_statement_bytes(*build_case(1, 20, seed=1)[0][:2], "C1")
        with self.assertRaises(ValueError):
            run_anomaly_gate(stmt, txns, existing_index=CaseDuplicateIndex(txns))

    def test_worker_enrichment_equals_service_enrichment_and_reports_progress(self) -> None:
        files = build_case(3, 60, seed=9)
        progress = []
        res = bulk_parse_statements(files, "C1", return_status=True, enrich_narrations=True, use_processes=False,
                                    on_progress=lambda d, t: progress.append((d, t)))
        self.assertEqual(progress[-1], (3, 3))
        self.assertEqual([d for d, _t in progress], [1, 2, 3])
        for r, (fn, raw, _p) in zip(res, files):
            self.assertTrue(r.meta.get("narration_enriched"))
            _s, plain, _m = parse_statement_bytes(fn, raw, "C1")
            for a, b in zip(r.transactions, plain):
                enrich_transaction_narration(b)
                self.assertEqual(
                    (a.payment_channel, a.utr_number, a.extracted_counterparty, a.counterparty_vpa_or_acc,
                     a.counterparty_ifsc),
                    (b.payment_channel, b.utr_number, b.extracted_counterparty, b.counterparty_vpa_or_acc,
                     b.counterparty_ifsc),
                )

    def test_lazy_ml_tier_matches_eager_evaluation(self) -> None:
        files = build_case(4, 150, seed=21)
        txns = []
        for fn, raw, _p in files:
            txns.extend(enrich_transaction_narration(t) for t in parse_statement_bytes(fn, raw, "C1")[1])
        lazy = HybridTransactionCategorizer()
        eager = HybridTransactionCategorizer()
        real = eager._classify_uncached

        def eager_classify(tx):
            # Force the ML ranking on every row (the pre-P2 behaviour), then classify normally.
            eager._nb.rank(__import__("cyphersol.nlp.categorizer", fromlist=["x"]).ml_feature_tokens(tx),
                           tx.direction.value)
            return real(tx)

        eager._classify_uncached = eager_classify  # type: ignore[assignment]
        a = lazy.classify_batch(copy.deepcopy(txns))
        b = eager.classify_batch(copy.deepcopy(txns))
        self.assertEqual(
            [(t.category_id, t.confidence_score, t.categorization_source, t.top3_suggestions) for t in a],
            [(t.category_id, t.confidence_score, t.categorization_source, t.top3_suggestions) for t in b],
        )

    def test_ingest_reports_stage_timings(self) -> None:
        with mock.patch.dict(os.environ, _clean_env(), clear=True), tempfile.TemporaryDirectory() as d:
            svc = CypherApplicationService(os.path.join(d, "v.db"), os.path.join(d, "s"), seed_demo=False)
            cid = svc.create_case("T", "cypher_edge", "x")["case_id"]
            seen = []
            res = svc.ingest_statements(cid, build_case(2, 40, seed=2), execution_mode="heuristics",
                                        progress=lambda ph, dn, tt: seen.append(ph))
            for k in ("parse", "validate", "categorise", "entities", "commit"):
                self.assertIn(k, res["timings"])
            self.assertEqual(seen[0], "parse")
            self.assertEqual(seen[-1], "commit")


# ---------------------------------------------------------------------------------------------------------------
# Item 28: analysis cache, slim overview
# ---------------------------------------------------------------------------------------------------------------
class AnalysisCacheTests(_CaseBase):
    def _fresh_order(self):
        return [t.tx_id for t in self.svc.vault.get_transactions(self.cid)]

    def test_incremental_snapshot_equals_full_reload(self) -> None:
        snap = self.svc._snapshot(self.cid)
        self.assertEqual([t.tx_id for t in snap.transactions], self._fresh_order())
        target = next(t for t in snap.transactions if t.is_suspense)
        calls = []
        real = self.svc.vault.get_transactions_with_order_keys

        def spy(case_id, changed_since=None):
            calls.append(changed_since)
            return real(case_id, changed_since=changed_since)

        with mock.patch.object(self.svc.vault, "get_transactions_with_order_keys", spy):
            self.svc.toggle_duplicate_ignore(self.cid, target.tx_id)
            snap2 = self.svc._snapshot(self.cid)
        self.assertEqual(calls, [snap.revision], "only rows changed since the cached revision are read")
        self.assertGreater(snap2.revision, snap.revision)
        self.assertEqual([t.tx_id for t in snap2.transactions], self._fresh_order())
        changed = next(t for t in snap2.transactions if t.tx_id == target.tx_id)
        self.assertNotEqual(changed.is_duplicate_ignored, target.is_duplicate_ignored)
        fresh = {t.tx_id: t.to_dict() for t in self.svc.vault.get_transactions(self.cid)}
        self.assertEqual({t.tx_id: t.to_dict() for t in snap2.transactions}, fresh)
        self.svc.toggle_duplicate_ignore(self.cid, target.tx_id)  # restore for the other tests

    def test_transfer_events_extracted_once_per_snapshot(self) -> None:
        self.svc._analysis_cache.clear()
        real = graph_engine._extract_directed_transfer_events
        n = {"calls": 0}

        def spy(*a, **k):
            n["calls"] += 1
            return real(*a, **k)

        with mock.patch.object(graph_engine, "_extract_directed_transfer_events", spy):
            self.svc.get_network_graph(self.cid)
            self.svc.get_loops_report(self.cid)
            self.svc.get_bidirectional(self.cid)
            self.svc.get_layering(self.cid)
        self.assertEqual(n["calls"], 1)

    def test_overview_is_aggregates_only(self) -> None:
        ov = self.svc.get_case_overview(self.cid)
        for key in ("transactions", "suspense_transactions", "anomaly_transactions"):
            self.assertNotIn(key, ov)
        self.assertEqual(ov["summary"]["transactions_count"], self.result["total_case_transactions"])
        legacy = self.svc.get_case_overview(self.cid, tx_limit=5)
        self.assertEqual(len(legacy["transactions"]), 5)

    def test_warm_up_populates_cache(self) -> None:
        self.svc._analysis_cache.clear()
        out = self.svc.warm_case_analysis(self.cid)
        self.assertFalse(out["superseded"])
        self.assertIn("graph", out["timings"])
        with mock.patch.object(service_mod, "build_network_graph", side_effect=AssertionError("recomputed")), \
                mock.patch.object(service_mod, "detect_circular_loops", side_effect=AssertionError("recomputed")):
            self.svc.get_network_graph(self.cid, keyword="", channel="")
            self.svc.get_loops_report(self.cid, max_days=30, min_retention=0.5, max_cycle_length=8,
                                      max_instances_per_loop=50)
            self.svc.export_dossier_html(self.cid)  # exports reuse the cached loops

    def test_warm_up_disabled_by_default(self) -> None:
        self.assertIsNone(self.svc.schedule_warmup(self.cid))


# ---------------------------------------------------------------------------------------------------------------
# Items 29 / 30: loops and graph
# ---------------------------------------------------------------------------------------------------------------
class GraphAndLoopTests(_CaseBase):
    def test_graph_node_cap_layout_modes_and_tx_id_cap(self) -> None:
        full = self.svc.get_network_graph(self.cid, max_nodes=5000)
        total = full["summary"]["total_nodes"]
        self.assertGreater(total, 20)
        capped = self.svc.get_network_graph(self.cid, max_nodes=10)
        self.assertEqual(len(capped["nodes"]), 10)
        self.assertTrue(capped["summary"]["truncated"])
        self.assertEqual(capped["summary"]["total_nodes"], total)
        kept = {n["entity_id"] for n in capped["nodes"]}
        self.assertTrue(all(e["source"] in kept and e["target"] in kept for e in capped["edges"]))
        self.assertEqual(capped["summary"]["layout"], "server")
        for n in capped["nodes"]:
            self.assertTrue(0.0 <= n["x"] <= 920.0 and 0.0 <= n["y"] <= 520.0)
        seed = self.svc.get_network_graph(self.cid, max_nodes=10, layout="seed")
        self.assertEqual(seed["summary"]["layout"], "seed")
        self.assertTrue(all("x" in n for n in seed["nodes"]))
        none = self.svc.get_network_graph(self.cid, max_nodes=10, layout="none")
        self.assertTrue(all("x" not in n for n in none["nodes"]))
        with self.assertRaises(ValueError):
            self.svc.get_network_graph(self.cid, layout="radial")
        with self.assertRaises(ValueError):
            self.svc.get_network_graph(self.cid, max_nodes=1)
        for e in full["edges"]:
            self.assertLessEqual(len(e["tx_ids"]), service_mod.GRAPH_EDGE_TX_IDS)
            if e.get("tx_ids_truncated"):
                self.assertGreater(e["tx_count"], len(e["tx_ids"]))

    def test_graph_expand_on_demand(self) -> None:
        capped = self.svc.get_network_graph(self.cid, max_nodes=10)
        hub = capped["nodes"][0]["entity_id"]
        out = self.svc.expand_network_node(self.cid, hub, limit=5)
        self.assertEqual(out["nodes"][0]["entity_id"], hub)
        self.assertLessEqual(len(out["edges"]), 5)
        for e in out["edges"]:
            self.assertIn(hub, (e["source"], e["target"]))
        ids = {n["entity_id"] for n in out["nodes"]}
        self.assertTrue(all(e["source"] in ids and e["target"] in ids for e in out["edges"]))
        self.assertGreaterEqual(out["total_edges"], len(out["edges"]))
        with self.assertRaises(KeyError):
            self.svc.expand_network_node(self.cid, "nope")
        with self.assertRaises(ValueError):
            self.svc.expand_network_node(self.cid, hub, limit=0)

    def test_loop_hub_exclusion_and_memo(self) -> None:
        snap = self.svc._snapshot(self.cid)
        base = detect_circular_loops(snap.statements, list(snap.transactions), snap.entities, hub_degree_cap=None)
        self.assertEqual(base.meta()["excluded_hubs"], [])
        hubbed = detect_circular_loops(snap.statements, list(snap.transactions), snap.entities, hub_degree_cap=2)
        meta = hubbed.meta()
        self.assertEqual(meta["hub_degree_cap"], 2)
        self.assertTrue(meta["excluded_hubs"])
        excluded = {h["entity_id"] for h in meta["excluded_hubs"]}
        for lp in hubbed:
            self.assertFalse(excluded & set(lp.entity_ids))
        with self.assertRaises(ValueError):
            detect_circular_loops(snap.statements, list(snap.transactions), snap.entities, hub_degree_cap=1)
        r1 = self.svc.get_loops_report(self.cid)
        r2 = self.svc.get_loops_report(self.cid, max_cycle_length=8, max_instances_per_loop=50)
        self.assertIs(r1, r2, "default parameters share one memo entry")
        off = self.svc.get_loops_report(self.cid, hub_degree_cap=0)
        self.assertIsNone(off["hub_degree_cap"])
        with self.assertRaises(ValueError):
            self.svc.get_loops_report(self.cid, hub_degree_cap=1)

    def test_loop_budget_marks_truncation(self) -> None:
        snap = self.svc._snapshot(self.cid)
        res = detect_circular_loops(snap.statements, list(snap.transactions), snap.entities, max_cycles=1)
        if len(res):
            self.assertTrue(res.truncated)
            self.assertEqual(res.truncation_reason, "max_cycles")


# ---------------------------------------------------------------------------------------------------------------
# Item 27: jobs, staged uploads (unit)
# ---------------------------------------------------------------------------------------------------------------
class JobUnitTests(unittest.TestCase):
    def test_job_lifecycle_progress_and_errors(self) -> None:
        jm = JobManager(workers=1)

        def work(progress):
            progress("parse", 1, 2)
            progress("parse", 2, 2)
            return {"ok": True}

        job = jm.wait(jm.submit("t", "C", work, weights=(("parse", 0.5), ("commit", 0.5))).job_id, timeout=5)
        d = job.to_dict()
        self.assertEqual(d["status"], "succeeded")
        self.assertEqual(d["progress"], 1.0)
        self.assertEqual(d["result"], {"ok": True})
        bad = jm.wait(jm.submit("t", "C", lambda p: (_ for _ in ()).throw(ValueError("nope"))).job_id, timeout=5)
        self.assertEqual((bad.status, bad.http_status, bad.error), ("failed", 400, "nope"))
        with self.assertLogs("cyphersol.jobs", "ERROR"):
            boom = jm.wait(jm.submit("t", "C", lambda p: 1 / 0).job_id, timeout=5)
        self.assertEqual((boom.status, boom.http_status), ("failed", 500))
        self.assertNotIn("division", boom.error)
        self.assertTrue(boom.to_dict()["error_id"])
        with self.assertRaises(KeyError):
            jm.get("0" * 32)

    def test_upload_store_limits_and_case_scope(self) -> None:
        st = UploadStore(max_total_bytes=10, max_files=2, ttl_s=60)
        a = st.put("C1", "a.csv", b"12345")
        with self.assertRaises(ValueError):
            st.put("C1", "b.csv", b"123456")  # over the byte budget
        with self.assertRaises(ValueError):
            st.put("C1", "", b"1")
        with self.assertRaises(ValueError):
            st.put("C1", "e.csv", b"")
        with self.assertRaises(KeyError):
            st.take("C2", [a.upload_id])  # another case cannot claim it
        self.assertEqual(st.take("C1", [a.upload_id])[0].data, b"12345")
        with self.assertRaises(KeyError):
            st.take("C1", [a.upload_id])  # consumed
        expired = UploadStore(ttl_s=0.0)
        e = expired.put("C1", "x.csv", b"1")
        with self.assertRaises(KeyError):
            expired.take("C1", [e.upload_id])


# ---------------------------------------------------------------------------------------------------------------
# HTTP: job polling, per-file uploads, new graph / loop parameters, slim overview
# ---------------------------------------------------------------------------------------------------------------
class P2HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.env_patch = mock.patch.dict(os.environ, _clean_env(), clear=True)
        cls.env_patch.start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.svc = CypherApplicationService(
            os.path.join(cls.tmp.name, "v.db"), os.path.join(cls.tmp.name, "s"), seed_demo=False, warm_analysis=True
        )
        cls.cid = cls.svc.create_case("Jobs", "cypher_trace", "x")["case_id"]
        cls.token = "p2-test-token-" + "z" * 24
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), create_http_handler(cls.svc, UI_DIR, api_token=cls.token))
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.files = build_case(4, 80, seed=31)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for j in cls.svc.jobs.list():
            try:
                cls.svc.jobs.wait(j.job_id, timeout=60)
            except TimeoutError:
                pass
        cls.env_patch.stop()
        cls.tmp.cleanup()

    def request(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"X-CypherSOL-Token": self.token}
        if body is not None:
            body = json.dumps(body)
            h["Content-Type"] = "application/json"
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        data = res.read()
        conn.close()
        return res.status, (json.loads(data) if data else None)

    def poll(self, job_id, timeout=120):
        deadline = time.monotonic() + timeout
        seen = []
        while time.monotonic() < deadline:
            st, job = self.request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(st, 200)
            seen.append(job["progress"])
            if job["status"] in ("succeeded", "failed"):
                self.assertEqual(seen, sorted(seen), "progress never goes backwards")
                return job
            time.sleep(0.05)
        self.fail("job did not finish")

    def stage(self, name, raw):
        st, up = self.request("POST", f"/api/cases/{self.cid}/uploads",
                              {"filename": name, "content_b64": base64.b64encode(raw).decode()})
        self.assertEqual(st, 201, up)
        return up

    def test_1_staged_upload_job_flow(self) -> None:
        ups = [self.stage(fn, raw) for fn, raw, _ in self.files[:3]]
        st, listing = self.request("GET", f"/api/cases/{self.cid}/uploads")
        self.assertEqual(len(listing["uploads"]), 3)
        st, job = self.request("POST", f"/api/cases/{self.cid}/ingest-jobs",
                               {"files": [{"upload_id": u["upload_id"]} for u in ups], "execution_mode": "heuristics"})
        self.assertEqual(st, 202, job)
        self.assertIn(job["status"], ("queued", "running", "succeeded"))
        self.assertEqual(job["file_count"], 3)
        done = self.poll(job["job_id"])
        self.assertEqual(done["status"], "succeeded", done)
        self.assertEqual(done["result"]["files_ok"], 3)
        self.assertEqual(done["progress"], 1.0)
        st, slim = self.request("GET", f"/api/jobs/{job['job_id']}?include_result=false")
        self.assertNotIn("result", slim)
        st, listing = self.request("GET", f"/api/cases/{self.cid}/uploads")
        self.assertEqual(listing["uploads"], [], "staged files are consumed by the job")
        st, jobs = self.request("GET", f"/api/cases/{self.cid}/jobs")
        self.assertIn(job["job_id"], [j["job_id"] for j in jobs["jobs"]])
        # the ingest job queues an analysis warm-up
        for _ in range(600):
            warm = [j for j in self.svc.jobs.list(case_id=self.cid, kind="warmup")]
            if warm and all(j.status == "succeeded" for j in warm):
                break
            time.sleep(0.05)
        self.assertTrue(warm)

    def test_2_job_errors(self) -> None:
        st, err = self.request("POST", f"/api/cases/{self.cid}/ingest-jobs", {"files": [{"upload_id": "f" * 32}]})
        self.assertEqual(st, 404, err)
        st, err = self.request("POST", f"/api/cases/{self.cid}/ingest-jobs", {"files": []})
        self.assertEqual(st, 400)
        up = self.stage("x.csv", self.files[3][1])
        st, err = self.request("POST", f"/api/cases/{self.cid}/ingest-jobs",
                               {"files": [{"upload_id": up["upload_id"]}], "on_duplicate": "maybe"})
        self.assertEqual(st, 400)
        st, _ = self.request("POST", f"/api/cases/{self.cid}/uploads/{up['upload_id']}/discard", {})
        self.assertEqual(st, 200)
        st, _ = self.request("POST", f"/api/cases/{self.cid}/uploads/{up['upload_id']}/discard", {})
        self.assertEqual(st, 404)
        # inline file already in the case -> the job fails with the synchronous endpoint's 409
        fn, raw, _ = self.files[0]
        st, job = self.request("POST", f"/api/cases/{self.cid}/ingest-jobs",
                               {"files": [{"filename": fn, "content_b64": base64.b64encode(raw).decode()}]})
        self.assertEqual(st, 202)
        done = self.poll(job["job_id"])
        self.assertEqual((done["status"], done["http_status"], done["error_type"]), ("failed", 409, "ConflictError"))
        st, _ = self.request("GET", "/api/jobs/" + "0" * 32)
        self.assertEqual(st, 404)
        st, _ = self.request("POST", "/api/cases/NOPE/uploads", {"filename": "a.csv", "content_b64": "QQ=="})
        self.assertEqual(st, 404)

    def test_3_graph_loops_overview_params(self) -> None:
        st, g = self.request("GET", f"/api/cases/{self.cid}/trace/graph?max_nodes=10&layout=seed")
        self.assertEqual(st, 200, g)
        self.assertLessEqual(len(g["nodes"]), 10)
        self.assertEqual(g["summary"]["layout"], "seed")
        hub = g["nodes"][0]["entity_id"]
        st, ex = self.request("GET", f"/api/cases/{self.cid}/trace/graph/expand?entity_id={hub}&limit=3")
        self.assertEqual(st, 200, ex)
        self.assertLessEqual(len(ex["edges"]), 3)
        st, _ = self.request("GET", f"/api/cases/{self.cid}/trace/graph/expand?entity_id=nope")
        self.assertEqual(st, 404)
        st, _ = self.request("GET", f"/api/cases/{self.cid}/trace/graph?layout=bogus")
        self.assertEqual(st, 400)
        st, lp = self.request("GET", f"/api/cases/{self.cid}/trace/loops?hub_degree_cap=0")
        self.assertEqual(st, 200)
        self.assertIsNone(lp["hub_degree_cap"])
        st, ov = self.request("GET", f"/api/cases/{self.cid}/overview")
        self.assertEqual(st, 200)
        self.assertNotIn("transactions", ov)
        self.assertIn("summary", ov)


# ---------------------------------------------------------------------------------------------------------------
# Item 32: OCR size probe
# ---------------------------------------------------------------------------------------------------------------
class OcrProbeTests(unittest.TestCase):
    def test_png_size_from_ihdr(self) -> None:
        from cyphersol.ingestion import ocr_engine

        def png(w, h):
            ihdr = struct.pack(">IIBBBBB", w, h, 8, 0, 0, 0, 0)
            chunk = struct.pack(">I", len(ihdr)) + b"IHDR" + ihdr + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr))
            return b"\x89PNG\r\n\x1a\n" + chunk

        self.assertEqual(ocr_engine.png_dimensions(png(3720, 5262)), (3720, 5262))
        self.assertIsNone(ocr_engine.png_dimensions(b"not a png"))


if __name__ == "__main__":
    unittest.main()
