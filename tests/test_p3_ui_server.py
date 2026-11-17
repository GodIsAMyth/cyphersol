"""P3 UI/server tests: static MIME types, CSP and security headers, transaction sorting, connection close on
rejected requests, Transfer-Encoding rejection, and static checks over the ES-module UI bundle."""

from __future__ import annotations

import http.client
import json
import os
import re
import socket
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

from cyphersol.server import CONTENT_SECURITY_POLICY, create_http_handler  # noqa: E402
from cyphersol.service import CypherApplicationService  # noqa: E402

UI_DIR = os.path.join(SRC_DIR, "cyphersol", "ui")
SCRATCH_DIR = os.path.join(tempfile.gettempdir(), "claude-501", "cyphersol-p3")
ENV_KEYS = ("GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CYPHERSOL_ALLOW_CLOUD_LLM",
            "CYPHERSOL_EXECUTION_MODE", "CYPHERSOL_TALLY_HOSTS", "CYPHERSOL_LLM_PROVIDER")
EDGE_CASE = "CASE_EDGE_BALAJI_FY25"
TRACE_CASE = "CASE_TRACE_EOW_041"


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _ui_files(ext):
    out = []
    for dirpath, _dirs, files in os.walk(UI_DIR):
        for name in files:
            if name.endswith(ext):
                out.append(os.path.join(dirpath, name))
    return sorted(out)


class P3ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.makedirs(SCRATCH_DIR, exist_ok=True)
        cls.tmp = tempfile.TemporaryDirectory(dir=SCRATCH_DIR)
        cls.env_patch = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k not in ENV_KEYS}, clear=True)
        cls.env_patch.start()
        cls.service = CypherApplicationService(
            os.path.join(cls.tmp.name, "p3_vault.db"), os.path.join(cls.tmp.name, "samples"), seed_demo=True
        )
        cls.token = "p3-test-token-" + "y" * 24
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

    def request(self, method, path, body=None, token=True, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = dict(headers or {})
        if token:
            h["X-CypherSOL-Token"] = self.token
        if body is not None and not isinstance(body, (bytes, str)):
            body = json.dumps(body)
            h.setdefault("Content-Type", "application/json")
        conn.request(method, path, body=body, headers=h)
        res = conn.getresponse()
        data = res.read()
        headers_out = {k.lower(): v for k, v in res.getheaders()}
        conn.close()
        return res.status, headers_out, data

    def raw(self, payload: bytes) -> bytes:
        """Send raw bytes and read until the server closes (or 3 s), returning everything received."""
        s = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        try:
            s.sendall(payload)
            chunks = []
            while True:
                try:
                    c = s.recv(65536)
                except socket.timeout:
                    chunks.append(b"<<TIMEOUT>>")
                    break
                if not c:
                    break
                chunks.append(c)
            return b"".join(chunks)
        finally:
            s.close()

    # ------------------------------------------------------------------ static assets
    def test_index_has_csp_and_security_headers(self):
        status, h, body = self.request("GET", "/", token=False)
        self.assertEqual(status, 200)
        self.assertTrue(h["content-type"].startswith("text/html"))
        csp = h.get("content-security-policy", "")
        self.assertEqual(csp, CONTENT_SECURITY_POLICY)
        for directive in ("default-src 'self'", "script-src 'self'", "connect-src 'self'", "frame-ancestors 'none'",
                          "base-uri 'none'", "form-action 'none'", "img-src 'self' data: blob:"):
            self.assertIn(directive, csp)
        self.assertNotIn("unsafe-inline", csp)
        self.assertNotIn("unsafe-eval", csp)
        self.assertEqual(h.get("referrer-policy"), "no-referrer")
        self.assertEqual(h.get("x-content-type-options"), "nosniff")
        self.assertIn(self.token, body.decode("utf-8"))

    def test_every_js_and_css_asset_served_with_correct_mime(self):
        js = _ui_files(".js")
        css = _ui_files(".css")
        self.assertGreaterEqual(len(js), 20)
        self.assertGreaterEqual(len(css), 3)
        for path in js + css:
            rel = "/" + os.path.relpath(path, UI_DIR).replace(os.sep, "/")
            status, h, body = self.request("GET", rel, token=False)
            self.assertEqual(status, 200, rel)
            expected = "text/javascript" if rel.endswith(".js") else "text/css"
            self.assertTrue(h["content-type"].startswith(expected), (rel, h["content-type"]))
            self.assertEqual(h.get("x-content-type-options"), "nosniff")
            self.assertNotIn("content-security-policy", h, "CSP is only sent on HTML documents")
            with open(path, "rb") as f:
                self.assertEqual(body, f.read())

    def test_static_traversal_and_missing(self):
        for p in ("/../server.py", "/js/../../server.py", "/%2e%2e/server.py", "/js/views/nope.js"):
            status, _h, _b = self.request("GET", p, token=False)
            self.assertEqual(status, 404, p)

    # ------------------------------------------------------------------ transactions sort
    def _tx(self, case, **params):
        q = "&".join(f"{k}={v}" for k, v in params.items())
        status, _h, body = self.request("GET", f"/api/cases/{case}/transactions?{q}")
        return status, json.loads(body)

    def test_transactions_sort_amount_desc_and_asc(self):
        st, desc = self._tx(TRACE_CASE, sort="amount", order="desc", limit=500)
        self.assertEqual(st, 200)
        self.assertEqual((desc["sort"], desc["order"]), ("amount", "desc"))
        amounts = [max(t["debit"], t["credit"]) for t in desc["transactions"]]
        self.assertGreater(len(amounts), 5)
        self.assertEqual(amounts, sorted(amounts, reverse=True))
        st, asc = self._tx(TRACE_CASE, sort="amount", order="asc", limit=500)
        self.assertEqual([max(t["debit"], t["credit"]) for t in asc["transactions"]], sorted(amounts))
        self.assertEqual(asc["total"], desc["total"])

    def test_transactions_sort_paginates_consistently(self):
        _st, full = self._tx(EDGE_CASE, sort="debit", order="desc", limit=500)
        ids = [t["tx_id"] for t in full["transactions"]]
        _st, p1 = self._tx(EDGE_CASE, sort="debit", order="desc", limit=5, offset=0)
        _st, p2 = self._tx(EDGE_CASE, sort="debit", order="desc", limit=5, offset=5)
        self.assertEqual([t["tx_id"] for t in p1["transactions"] + p2["transactions"]], ids[:10])
        self.assertTrue(p1["has_more"])

    def test_transactions_sort_with_filters_and_dates(self):
        st, res = self._tx(TRACE_CASE, sort="txn_date", order="desc", direction="CR", limit=500)
        self.assertEqual(st, 200)
        dates = [t["txn_date"] for t in res["transactions"]]
        self.assertEqual(dates, sorted(dates, reverse=True))
        self.assertTrue(all(t["credit"] > 0 for t in res["transactions"]))

    def test_transactions_amount_range(self):
        st, res = self._tx(TRACE_CASE, amount_min=100000, amount_max=400000, limit=500)
        self.assertEqual(st, 200)
        self.assertGreater(res["total"], 0)
        for t in res["transactions"]:
            self.assertTrue(100000 <= max(t["debit"], t["credit"]) <= 400000)
        st, none = self._tx(TRACE_CASE, amount_min=10**12)
        self.assertEqual((st, none["total"]), (200, 0))

    def test_transactions_default_order_unchanged(self):
        _st, res = self._tx(EDGE_CASE, limit=500)
        self.assertEqual((res["sort"], res["order"]), ("txn_date", "asc"))
        dates = [t["txn_date"] for t in res["transactions"]]
        self.assertEqual(dates, sorted(dates))

    def test_transactions_tx_ids_filter(self):
        _st, base = self._tx(TRACE_CASE, limit=10)
        want = [t["tx_id"] for t in base["transactions"][2:6]]
        st, res = self._tx(TRACE_CASE, tx_ids=",".join(want + [want[0], "does-not-exist"]), limit=500)
        self.assertEqual(st, 200)
        self.assertEqual(res["total"], len(want))
        self.assertEqual(sorted(t["tx_id"] for t in res["transactions"]), sorted(want))
        too_many = ",".join(f"id{i}" for i in range(501))
        status, _h, _b = self.request("GET", f"/api/cases/{TRACE_CASE}/transactions?tx_ids={too_many}")
        self.assertEqual(status, 400)

    def test_transactions_sort_row_index_within_statement(self):
        _st, ov = self._tx(EDGE_CASE, limit=1)
        sid = ov["transactions"][0]["statement_id"]
        st, res = self._tx(EDGE_CASE, statement_id=sid, sort="row_index", order="asc", limit=500)
        self.assertEqual(st, 200)
        idx = [t["row_index"] for t in res["transactions"]]
        self.assertEqual(idx, sorted(idx))

    def test_loan_params_validated(self):
        for q in ("foir=200", "foir=0", "rate=-1", "rate=51", "tenure=0", "tenure=481", "foir=nan"):
            status, _h, body = self.request("GET", f"/api/cases/{EDGE_CASE}/edge/loan?{q}")
            self.assertEqual(status, 400, q)
            self.assertIn("error", json.loads(body))
        status, _h, _b = self.request("GET", f"/api/cases/{EDGE_CASE}/edge/loan?foir=100&rate=0&tenure=480")
        self.assertEqual(status, 200)

    def test_layering_misc_rejects_unknown_entity(self):
        status, _h, body = self.request(
            "POST", f"/api/cases/{TRACE_CASE}/trace/layering/misc", {"entity_id": "not-an-entity", "move_to_misc": True}
        )
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))
        status, _h, _b = self.request("POST", f"/api/cases/{TRACE_CASE}/trace/layering/misc", {"move_to_misc": True})
        self.assertEqual(status, 400)

    def test_graph_max_amount_and_ifsc(self):
        status, _h, body = self.request("GET", f"/api/cases/{TRACE_CASE}/trace/graph")
        self.assertEqual(status, 200)
        full = json.loads(body)
        status, _h, body = self.request("GET", f"/api/cases/{TRACE_CASE}/trace/graph?max_amount=50000")
        self.assertEqual(status, 200)
        capped = json.loads(body)
        self.assertLessEqual(len(capped["edges"]), len(full["edges"]))
        self.assertLess(len(capped["edges"]), len(full["edges"]))
        ids = sorted({i for e in capped["edges"] for i in e.get("tx_ids") or []})[:500]
        self.assertTrue(ids)
        _st, rows = self._tx(TRACE_CASE, tx_ids=",".join(ids), limit=500)
        self.assertTrue(rows["transactions"])
        for t in rows["transactions"]:
            self.assertLessEqual(max(t["debit"], t["credit"]), 50000)
        status, _h, body = self.request("GET", f"/api/cases/{TRACE_CASE}/trace/graph?ifsc=ZZZZ0000000")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["edges"], [])
        for q in ("ifsc=bad%20ifsc!", "min_amount=100&max_amount=10"):
            status, _h, _b = self.request("GET", f"/api/cases/{TRACE_CASE}/trace/graph?{q}")
            self.assertEqual(status, 400, q)

    def test_transactions_sort_rejects_unknown_field_and_order(self):
        for params in ({"sort": "raw_narration;DROP TABLE transactions"}, {"sort": "tx_id"}, {"sort": "amount", "order": "sideways"},
                       {"amount_min": "abc"}):
            q = "&".join(f"{k}={v}" for k, v in params.items()).replace(" ", "%20")
            status, h, body = self.request("GET", f"/api/cases/{EDGE_CASE}/transactions?{q}")
            self.assertEqual(status, 400, params)
            self.assertIn("error", json.loads(body))
        # the table is still there
        st, res = self._tx(EDGE_CASE, limit=1)
        self.assertEqual(st, 200)
        self.assertGreater(res["total"], 0)

    # ------------------------------------------------------------------ connection handling
    def test_rejected_post_closes_connection(self):
        body = b'{"title": "x"}'
        smuggled = b"GET /api/cases HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n" % self.port
        # 403: no token; body never read -> response must carry Connection: close and the server must not
        # interpret the body (or anything after it) as a second request.
        req = (b"POST /api/cases HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\n"
               b"Content-Length: %d\r\n\r\n" % (self.port, len(body))) + body + smuggled
        out = self.raw(req)
        self.assertIn(b"HTTP/1.1 403", out)
        self.assertIn(b"Connection: close", out)
        self.assertEqual(out.count(b"HTTP/1.1 "), 1, out[:400])
        self.assertNotIn(b"<<TIMEOUT>>", out)

    def test_415_closes_connection(self):
        body = b"title=x"
        req = (b"POST /api/cases HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-CypherSOL-Token: %s\r\n"
               b"Content-Type: application/x-www-form-urlencoded\r\nContent-Length: %d\r\n\r\n"
               % (self.port, self.token.encode(), len(body))) + body
        out = self.raw(req)
        self.assertIn(b"HTTP/1.1 415", out)
        self.assertIn(b"Connection: close", out)
        self.assertNotIn(b"<<TIMEOUT>>", out)

    def test_413_closes_connection(self):
        req = (b"POST /api/cases HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-CypherSOL-Token: %s\r\n"
               b"Content-Type: application/json\r\nContent-Length: 999999999999\r\n\r\n{}"
               % (self.port, self.token.encode()))
        out = self.raw(req)
        self.assertIn(b"HTTP/1.1 413", out)
        self.assertIn(b"Connection: close", out)

    def test_transfer_encoding_rejected_with_411(self):
        req = (b"POST /api/cases HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-CypherSOL-Token: %s\r\n"
               b"Content-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
               b"4\r\n{}  \r\n0\r\n\r\n" % (self.port, self.token.encode()))
        out = self.raw(req)
        self.assertIn(b"HTTP/1.1 411", out)
        self.assertIn(b"Connection: close", out)
        self.assertEqual(out.count(b"HTTP/1.1 "), 1)
        out = self.raw(b"GET / HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n" % self.port)
        self.assertIn(b"HTTP/1.1 411", out)

    def test_successful_requests_keep_alive(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            for _ in range(3):
                conn.request("GET", "/api/cases", headers={"X-CypherSOL-Token": self.token})
                res = conn.getresponse()
                res.read()
                self.assertEqual(res.status, 200)
                self.assertNotEqual((res.getheader("Connection") or "").lower(), "close")
            conn.request("POST", "/api/llm/config", body=json.dumps({}), headers={
                "X-CypherSOL-Token": self.token, "Content-Type": "application/json"})
            res = conn.getresponse()
            res.read()
            self.assertEqual(res.status, 200)
            conn.request("GET", "/api/health")
            res = conn.getresponse()
            res.read()
            self.assertEqual(res.status, 200)
        finally:
            conn.close()

    def test_get_404_api_keeps_working(self):
        status, _h, body = self.request("GET", "/api/cases/NOPE_CASE/overview")
        self.assertEqual(status, 404)
        self.assertIn("error", json.loads(body))


class P3UiBundleStaticTests(unittest.TestCase):
    """Static guarantees the strict CSP depends on, over every UI file."""

    @classmethod
    def setUpClass(cls):
        cls.js = {p: _read(p) for p in _ui_files(".js")}
        with open(os.path.join(UI_DIR, "index.html"), encoding="utf-8") as f:
            cls.html = f.read()

    def test_index_shell_contract(self):
        h = self.html
        self.assertIn('<meta name="cyphersol-token" content="__CYPHERSOL_TOKEN__"', h)
        self.assertIn('<script type="module" src="js/app.js"></script>', h)
        self.assertIn('id="cloud-ai-banner"', h)
        self.assertIn('class="skip-link"', h)
        self.assertRegex(h, r"<main[^>]*id=\"main\"")
        self.assertRegex(h, r"<header[^>]*class=\"topbar\"")
        self.assertRegex(h, r"<nav[^>]*id=\"sidebar-nav\"")
        self.assertIn('aria-live="polite"', h)
        # no inline script bodies, handlers or style attributes (CSP: script-src/style-src 'self')
        for m in re.finditer(r"<script\b([^>]*)>(.*?)</script>", h, re.S):
            self.assertIn("src=", m.group(1))
            self.assertEqual(m.group(2).strip(), "")
        self.assertNotRegex(h, r"\son[a-z]+\s*=")
        self.assertNotRegex(h, r"\sstyle\s*=")
        self.assertNotIn("<style", h)
        self.assertNotRegex(h, r"https?://")

    def test_js_has_no_inline_handlers_or_style_attributes(self):
        for path, src in self.js.items():
            self.assertNotRegex(src, r"\son(?:click|change|input|submit|load|error|mouse\w+|key\w+)\s*=\s*[\"'`]", path)
            self.assertNotRegex(src, r"<[a-zA-Z][^<>]*\sstyle\s*=\s*[\"']", f"{path}: style attributes are blocked by CSP; use CSSOM")
            self.assertNotRegex(src, r"\beval\(|new Function\(", path)
            # html`` escapes plain-string interpolations, so `${x ? 'attr="v"' : ""}` would render &quot;
            # — attribute fragments must be html`attr="v"` or raw().
            self.assertNotRegex(src, r"\?\s*'[^'\n]*=\"[^'\n]*'\s*:", f"{path}: quoted attribute fragment in a plain string")

    def test_no_external_network_or_cdn(self):
        for path, src in self.js.items():
            for url in re.findall(r"https?://[^\s\"'`)]+", src):
                self.assertRegex(url, r"^http://www\.w3\.org/", f"{path} references external URL {url}")
        for path in _ui_files(".css"):
            with open(path, encoding="utf-8") as f:
                self.assertNotRegex(f.read(), r"@import\s+url\(\s*['\"]?https?:|url\(\s*['\"]?https?:", path)

    def test_relative_imports_resolve(self):
        imp = re.compile(r"""(?:^|\n)\s*(?:import|export)\s[^;]*?from\s+["'](\.{1,2}/[^"']+)["']""")
        for path, src in self.js.items():
            for spec in imp.findall(src):
                target = os.path.normpath(os.path.join(os.path.dirname(path), spec))
                self.assertTrue(os.path.isfile(target), f"{path}: import {spec} does not resolve")

    def test_every_registered_view_has_a_module(self):
        nav = _read(os.path.join(UI_DIR, "js", "core", "nav.js"))
        block = nav[nav.index("export const VIEWS = {"): nav.index("};", nav.index("export const VIEWS = {"))]
        names = re.findall(r"^\s{2}(\w+):\s*\{", block, re.M)
        self.assertGreaterEqual(len(names), 18)
        for name in names:
            path = os.path.join(UI_DIR, "js", "views", f"{name}.js")
            self.assertTrue(os.path.isfile(path), f"missing view module {name}")
            self.assertRegex(_read(path), r"export (async )?function mount\(", name)

    def test_single_fetch_and_token_header(self):
        all_js = "\n".join(self.js.values())
        self.assertEqual(len(re.findall(r"\bfetch\(", all_js)), 1)
        self.assertIn('headers.set(TOKEN_HEADER, API_TOKEN)', all_js)
        self.assertNotRegex(all_js, r"[?&]token=")


if __name__ == "__main__":
    unittest.main()
