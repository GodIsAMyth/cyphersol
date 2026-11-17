"""Tests for persistent input documents, export artifacts, log storage, and cloud reverse-proxy compatibility."""

from __future__ import annotations

import base64
import http.client
import json
import logging
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.models import InvestigationCase, WorkspaceMode
from cyphersol.server import create_http_handler
from cyphersol.service import CypherApplicationService
from cyphersol.storage import CypherVault

SAMPLE_DIR = os.path.join(ROOT_DIR, "sample_statements")
UI_DIR = os.path.join(SRC_DIR, "cyphersol", "ui")


class StorageAndDeploymentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test_vault.db")
        self.vault = CypherVault(self.db_path)
        self.case_id = "CASE_TEST_STORAGE_001"
        self.vault.save_case(
            InvestigationCase(
                case_id=self.case_id,
                title="Storage & Deployment Test Case",
                agency_or_firm_name="Test Agency",
                mode=WorkspaceMode.CYPHER_TRACE,
                created_at="2026-01-01T00:00:00Z",
            )
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_vault_inputs_storage_and_retrieval(self) -> None:
        content = b"PDF dummy statement content with headers and transactions"
        record = self.vault.save_case_input(
            case_id=self.case_id,
            filename="statement_sample.pdf",
            content=content,
            metadata={"source": "test"},
        )
        self.assertEqual(record["filename"], "statement_sample.pdf")
        self.assertEqual(record["file_size_bytes"], len(content))
        self.assertEqual(record["content_type"], "application/pdf")
        self.assertTrue(os.path.exists(os.path.join(self.vault.cases_dir, self.case_id, "inputs")))

        # List inputs
        inputs = self.vault.get_case_inputs(self.case_id)
        self.assertEqual(len(inputs), 1)
        self.assertEqual(inputs[0]["input_id"], record["input_id"])
        self.assertEqual(inputs[0]["sha256"], record["sha256"])

        # Fetch file bytes by input_id and by sha256
        res_by_id = self.vault.get_case_input_file(self.case_id, record["input_id"])
        self.assertIsNotNone(res_by_id)
        fname, ctype, data = res_by_id
        self.assertEqual(fname, "statement_sample.pdf")
        self.assertEqual(ctype, "application/pdf")
        self.assertEqual(data, content)

        res_by_sha = self.vault.get_case_input_file(self.case_id, record["sha256"])
        self.assertIsNotNone(res_by_sha)
        self.assertEqual(res_by_sha[2], content)

    def test_vault_outputs_storage_and_retrieval(self) -> None:
        content = b"Fake XLSX workbook content binary data"
        out_record = self.vault.save_case_output(
            case_id=self.case_id,
            output_type="audit_workbook",
            filename=f"{self.case_id}_audit.xlsx",
            content=content,
            metadata={"sheets": 5},
        )
        self.assertEqual(out_record["filename"], f"{self.case_id}_audit.xlsx")
        self.assertEqual(out_record["output_type"], "audit_workbook")
        self.assertEqual(out_record["file_size_bytes"], len(content))

        # List outputs
        outputs = self.vault.get_case_outputs(self.case_id)
        self.assertEqual(len(outputs), 1)
        self.assertEqual(outputs[0]["output_id"], out_record["output_id"])

        # Fetch file
        res = self.vault.get_case_output_file(self.case_id, out_record["output_id"])
        self.assertIsNotNone(res)
        fname, ctype, data = res
        self.assertEqual(fname, f"{self.case_id}_audit.xlsx")
        self.assertEqual(data, content)

    def test_service_logging_to_rotating_file(self) -> None:
        service = CypherApplicationService(self.db_path, SAMPLE_DIR, seed_demo=False)
        log_path = os.path.join(service.vault.data_dir, "logs", "cyphersol.log")
        self.assertTrue(os.path.exists(log_path))
        logger = logging.getLogger("cyphersol")
        logger.info("Test message written to log file")
        for h in logger.handlers:
            h.flush()
        with open(log_path, "r", encoding="utf-8") as f:
            log_text = f.read()
        self.assertIn("Test message written to log file", log_text)

    def test_service_ingest_and_exports_persist_artifacts(self) -> None:
        service = CypherApplicationService(self.db_path, SAMPLE_DIR, seed_demo=False)
        demo_case = self.case_id

        # Ingest a CSV statement
        csv_content = (
            b"Txn Date,Description,Ref No,Debit,Credit,Balance\n"
            b"01/01/2024,OPENING BALANCE,,,100000.00,100000.00\n"
            b"02/01/2024,TRANSFER TO VENDOR,REF001,25000.00,,75000.00\n"
        )
        res = service.ingest_statements(demo_case, [("test_statement.csv", csv_content, None)])
        self.assertEqual(res["files_ok"], 1)

        # Verify input was saved to case inputs
        inputs = service.list_case_inputs(demo_case)
        self.assertTrue(any(i["filename"] == "test_statement.csv" for i in inputs))
        test_in = next(i for i in inputs if i["filename"] == "test_statement.csv")
        file_res = service.get_case_input_file(demo_case, test_in["input_id"])
        self.assertIsNotNone(file_res)
        self.assertEqual(file_res[2], csv_content)

        # Generate exports and check they are recorded in outputs
        xlsx_bytes = service.export_xlsx(demo_case)
        self.assertGreater(len(xlsx_bytes), 0)
        html_doc = service.export_dossier_html(demo_case)
        self.assertGreater(len(html_doc), 0)
        tally_xml = service.export_tally_xml(demo_case)
        self.assertIn("xml", tally_xml)

        outputs = service.list_case_outputs(demo_case)
        types = {o["output_type"] for o in outputs}
        self.assertIn("audit_workbook", types)
        self.assertIn("forensic_dossier_html", types)
        self.assertIn("tally_xml", types)


class ServerCloudAndStorageHttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmp.name, "http_vault.db")
        cls.service = CypherApplicationService(db_path, SAMPLE_DIR, seed_demo=True)
        cls.token = "cloud-test-token-" + "z" * 24
        handler = create_http_handler(
            cls.service,
            UI_DIR,
            api_token=cls.token,
            allowed_hosts=["cyphersol.onrender.com", "*.run.app"],
        )
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.tmp.cleanup()

    def _req(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict | None = None,
        host: str | None = None,
    ) -> tuple[int, dict, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"X-CypherSOL-Token": self.token, "Host": host or f"127.0.0.1:{self.port}"}
        if headers:
            h.update(headers)
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}
        conn.close()
        return resp.status, resp_headers, data

    def test_reverse_proxy_cloud_origin_and_host(self) -> None:
        # Request with Host: cyphersol.onrender.com and X-Forwarded-Proto: https
        status, _, data = self._req(
            "GET",
            "/api/cases",
            host="cyphersol.onrender.com",
            headers={
                "X-Forwarded-Proto": "https",
                "Origin": "https://cyphersol.onrender.com",
            },
        )
        self.assertEqual(status, 200)
        payload = json.loads(data.decode("utf-8"))
        self.assertIn("cases", payload)

    def test_http_inputs_and_outputs_api_flow(self) -> None:
        case_id = "CASE_TRACE_EOW_041"

        # 1. Upload extra document via POST /api/cases/{cid}/inputs
        raw_doc = b"JSON document evidence: {'key': 'value'}"
        b64 = base64.b64encode(raw_doc).decode("ascii")
        post_body = json.dumps({"filename": "evidence.json", "content_b64": b64}).encode("utf-8")
        status, _, data = self._req(
            "POST",
            f"/api/cases/{case_id}/inputs",
            body=post_body,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 201)
        in_record = json.loads(data.decode("utf-8"))
        self.assertEqual(in_record["filename"], "evidence.json")

        # 2. List inputs via GET /api/cases/{cid}/inputs
        status, _, data = self._req("GET", f"/api/cases/{case_id}/inputs")
        self.assertEqual(status, 200)
        inputs_list = json.loads(data.decode("utf-8"))["inputs"]
        self.assertTrue(any(i["filename"] == "evidence.json" for i in inputs_list))

        # 3. Download input via GET /api/cases/{cid}/inputs/{id}/download
        status, headers, dl_data = self._req("GET", f"/api/cases/{case_id}/inputs/{in_record['input_id']}/download")
        self.assertEqual(status, 200)
        self.assertEqual(dl_data, raw_doc)
        self.assertIn("attachment", headers.get("content-disposition", "").lower())

        # 4. Trigger an export (e.g. XLSX)
        status, _, _ = self._req("GET", f"/api/cases/{case_id}/export/xlsx")
        self.assertEqual(status, 200)

        # 5. List outputs via GET /api/cases/{cid}/outputs
        status, _, data = self._req("GET", f"/api/cases/{case_id}/outputs")
        self.assertEqual(status, 200)
        outputs_list = json.loads(data.decode("utf-8"))["outputs"]
        self.assertGreater(len(outputs_list), 0)

        # 6. Download output via GET /api/cases/{cid}/outputs/{id}/download
        out_id = outputs_list[0]["output_id"]
        status, _, dl_out_data = self._req("GET", f"/api/cases/{case_id}/outputs/{out_id}/download")
        self.assertEqual(status, 200)
        self.assertGreater(len(dl_out_data), 0)


if __name__ == "__main__":
    from cyphersol.models import InvestigationCase
    unittest.main()
