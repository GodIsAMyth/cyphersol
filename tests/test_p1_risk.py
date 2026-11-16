"""P1 risk group: mule scoring per SRS (TF-15..23), profiles and exports (TG-02/14..17, BE-18/19/20, EDGE-13/19)."""

from __future__ import annotations

import json
import math
import os
import re
import sys
import unittest
from decimal import Decimal
from typing import List, Optional

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.ingestion.xlsx_io import read_xlsx_bytes  # noqa: E402
from cyphersol.models import (  # noqa: E402
    BankAccountStatement,
    InvestigationCase,
    MuleRiskTier,
    NormalizedTransaction,
    ResolvedEntity,
    TxDirection,
    WorkspaceMode,
)
from cyphersol import reporting  # noqa: E402
from cyphersol.trace.financial_profile import build_individual_financial_profile  # noqa: E402
from cyphersol.trace.mule_scorer import (  # noqa: E402
    PARAMETER_WEIGHTS,
    evaluate_all_mule_accounts,
    score_account_mule_risk,
)

CASE = "CASE_P1_RISK"


def stmt(sid="S1", account="111122223333", holder="SUBJECT ONE", start="2026-01-01", end="2026-03-31",
         opening=0.0, closing=0.0, bank_code="HDFC", sha="a" * 64) -> BankAccountStatement:
    return BankAccountStatement(
        statement_id=sid, case_id=CASE, source_filename=f"{sid}.csv", sha256_hash=sha, bank_code=bank_code,
        bank_name="HDFC Bank", account_number=account, account_holder_name=holder, period_start=start,
        period_end=end, opening_balance=Decimal(str(opening)), closing_balance=Decimal(str(closing)),
        total_transactions=0,
    )


class TxB:
    def __init__(self) -> None:
        self.n = 0

    def __call__(self, date, cr=0.0, dr=0.0, bal=0.0, sid="S1", cp=None, eid=None, channel="UPI", ts=None,
                 cat=(52, "Uncategorised"), narr=None, vpa=None) -> NormalizedTransaction:
        self.n += 1
        direction = TxDirection.CREDIT if cr > 0 else TxDirection.DEBIT
        return NormalizedTransaction(
            tx_id=f"T{self.n:04d}", statement_id=sid, case_id=CASE, row_index=self.n, txn_date=date,
            raw_narration=narr or f"{channel}/{cp or 'X'}/{self.n}", payment_channel=channel, direction=direction,
            debit=Decimal(str(dr)), credit=Decimal(str(cr)), running_balance=Decimal(str(bal)),
            category_id=cat[0], category_name=cat[1], categorization_source="RULE", confidence_score=0.9,
            txn_timestamp=ts, extracted_counterparty=cp, resolved_entity_id=eid, counterparty_vpa_or_acc=vpa,
        )


def vec(a):
    return {v["key"]: v for v in a.explainability}


# ------------------------------------------------------------------------------------------- mule scorer
class MuleScorerSrsTests(unittest.TestCase):
    def test_tf17_s1_hard_turnover_gate(self):
        t = TxB()
        small = [t("2026-01-05", cr=20000, cp="A"), t("2026-01-06", dr=20000, cp="B")]
        self.assertEqual(score_account_mule_risk(stmt(), small).parameters.flow_through_ratio, 0.0)
        big = [t("2026-01-05", cr=60000, cp="A"), t("2026-01-06", dr=60000, cp="B")]
        a = score_account_mule_risk(stmt(), big)
        self.assertEqual(a.parameters.flow_through_ratio, 1.0)
        self.assertTrue(vec(a)["flow_through_ratio"]["raw"]["turnover_gate_passed"])

    def test_tf16_s5_uses_divisor_10_without_degree_gate(self):
        t = TxB()
        rows = [t("2026-01-05", cr=1000, cp=f"SENDER {i}", ts=f"2026-01-05T10:0{i}:00") for i in range(5)]
        a = score_account_mule_risk(stmt(), rows)
        self.assertAlmostEqual(a.parameters.fan_in_fan_out_pattern, 0.5, places=4)  # 5/10 * (1 - 0)
        one = score_account_mule_risk(stmt(), [TxB()("2026-01-05", cr=1000, cp="SOLO")])
        self.assertAlmostEqual(one.parameters.fan_in_fan_out_pattern, 0.1, places=4)
        self.assertEqual(vec(a)["fan_in_fan_out_pattern"]["raw"]["peak_window_unique_senders"], 5)

    def test_tf18_s4_unresolved_rows_not_pooled_by_category(self):
        t = TxB()
        rows = [t(f"2026-01-0{i + 1}", cr=1000) for i in range(4)]  # no counterparty at all, same category
        a = score_account_mule_risk(stmt(), rows)
        self.assertAlmostEqual(a.parameters.counterparty_concentration, 0.25, places=4)
        resolved = [t("2026-01-01", cr=1000, cp="Alpha Traders"), t("2026-01-02", cr=1000, cp="ALPHA TRADERS PVT LTD")]
        self.assertAlmostEqual(score_account_mule_risk(stmt(), resolved).parameters.counterparty_concentration, 1.0, places=4)

    def test_tf21_s7_paise_and_same_day_split(self):
        t = TxB()
        rows = [t("2026-01-02", cr=49999.50, cp="P1")] + [t("2026-01-10", cr=500, cp=f"Z{i}") for i in range(9)]
        a = score_account_mule_risk(stmt(), rows)
        self.assertAlmostEqual(a.parameters.structuring_smurfing, 0.3, places=4)  # 3 * 1/10
        t2 = TxB()
        split = [t2("2026-01-03", dr=20000, cp="MULE X") for _ in range(3)] + [t2("2026-01-09", cr=100, cp=f"Q{i}") for i in range(3)]
        b = score_account_mule_risk(stmt(), split)
        raw = vec(b)["structuring_smurfing"]["raw"]
        self.assertEqual(raw["split_structuring_groups"], 1)
        self.assertAlmostEqual(b.parameters.structuring_smurfing, 1.0, places=4)  # min(1, 3*3/6)

    def test_tf20_s8_zero_without_balance_column(self):
        t = TxB()
        rows = [t("2026-01-02", cr=50000, cp="A"), t("2026-01-02", dr=50000, cp="B")]  # running balance all 0
        a = score_account_mule_risk(stmt(), rows)
        self.assertEqual(a.parameters.zero_balance_oscillation, 0.0)
        self.assertFalse(vec(a)["zero_balance_oscillation"]["raw"]["balance_available"])

    def test_s8_counts_drained_inflow_cycles(self):
        t = TxB()
        rows = [
            t("2026-01-02", cr=50000, bal=50100, cp="A"), t("2026-01-02", dr=50000, bal=100, cp="B"),
            t("2026-01-10", cr=40000, bal=40100, cp="A"), t("2026-01-11", dr=10000, bal=30100, cp="B"),
        ]
        a = score_account_mule_risk(stmt(), rows)
        self.assertAlmostEqual(a.parameters.zero_balance_oscillation, 0.5, places=4)
        self.assertIn(rows[1].tx_id, vec(a)["zero_balance_oscillation"]["evidence_tx_ids"])

    def test_tf19_s9_dormancy_must_precede_peak_and_base_is_prior_median(self):
        t = TxB()
        # Steady activity, then a burst; no dormancy -> 0.
        steady = [t(f"2026-0{m}-1{d}", cr=5000, cp="S") for m in (1, 2, 3) for d in (0, 5)]
        steady += [t("2026-04-02", cr=500000, cp="BIG")]
        self.assertEqual(score_account_mule_risk(stmt(end="2026-04-30"), steady).parameters.burst_dormancy_pattern, 0.0)
        # Dormant 60 days after an early credit, then a burst: base = median of prior months.
        t2 = TxB()
        dormant = [t2("2026-01-02", cr=1000, cp="S"), t2("2026-03-05", cr=200000, cp="B1"), t2("2026-03-06", dr=200000, cp="B2")]
        a = score_account_mule_risk(stmt(end="2026-03-31"), dormant)
        raw = vec(a)["burst_dormancy_pattern"]["raw"]
        self.assertGreaterEqual(raw["max_dormancy_gap_days"], 30)
        self.assertEqual(raw["peak_30d_turnover"], 400000.0)
        expected = 1 - math.exp(-400000 / (10000 * 5))
        self.assertAlmostEqual(a.parameters.burst_dormancy_pattern, round(expected, 4), places=4)
        # A dormancy gap that only occurs after the peak does not count.
        t3 = TxB()
        after = [t3("2026-01-02", cr=300000, cp="B"), t3("2026-01-03", dr=300000, cp="C"), t3("2026-03-20", cr=100, cp="D")]
        self.assertEqual(score_account_mule_risk(stmt(), after).parameters.burst_dormancy_pattern, 0.0)

    def test_tf22_s10_only_neft_rtgs_imps_upi(self):
        t = TxB()
        rows = [t("2026-01-02", cr=10000, channel="TRANSFER", cp="A"), t("2026-01-03", cr=10000, channel="UPI", cp="B"),
                t("2026-01-04", cr=12345, channel="NEFT", cp="C"), t("2026-01-05", dr=5000, channel="ATM")]
        a = score_account_mule_risk(stmt(), rows)
        self.assertAlmostEqual(a.parameters.round_number_anomalies, 0.5, places=4)
        self.assertEqual(vec(a)["round_number_anomalies"]["raw"]["electronic_txns"], 2)

    def test_tf23_scored_per_account_across_statements(self):
        t = TxB()
        s1 = stmt("S1", start="2026-01-01", end="2026-01-31")
        s2 = stmt("S2", start="2026-02-01", end="2026-02-28")
        other = stmt("S3", account="999988887777", holder="OTHER")
        rows = [t("2026-01-05", cr=60000, sid="S1", cp="A"), t("2026-02-05", dr=60000, sid="S2", cp="B"),
                t("2026-02-06", cr=10, sid="S3", cp="C")]
        ents = [ResolvedEntity("E1", CASE, "SUBJECT ONE", ["SUBJECT ONE"], ["111122223333"], has_statement=True)]
        res = evaluate_all_mule_accounts([s1, s2, other], rows, ents)
        self.assertEqual(len(res), 2)
        subj = next(a for a in res if a.account_number == "111122223333")
        self.assertEqual(sorted(subj.statement_ids), ["S1", "S2"])
        self.assertEqual(subj.tx_count, 2)
        self.assertEqual(subj.entity_id, "E1")
        self.assertEqual(subj.parameters.flow_through_ratio, 1.0)  # only visible when both statements combine

    def test_tf15_explainability_vector_complete(self):
        t = TxB()
        rows = [t("2026-01-02", cr=49000, bal=49000, cp=f"V{i}", ts=f"2026-01-02T10:0{i}:00") for i in range(5)]
        rows += [t("2026-01-02", dr=245000 - 100, bal=100, cp="COINDCX", channel="UPI", ts="2026-01-02T11:00:00",
                   narr="UPI/COINDCX CRYPTO")]
        a = score_account_mule_risk(stmt(), rows)
        self.assertEqual([v["key"] for v in a.explainability], list(PARAMETER_WEIGHTS))
        ids = {r.tx_id for r in rows}
        for v in a.explainability:
            for field in ("label", "weight", "score", "contribution", "raw", "raw_metric", "formula", "evidence_tx_ids"):
                self.assertIn(field, v)
            self.assertTrue(v["raw"], v["key"])
            self.assertAlmostEqual(v["contribution"], round(100 * v["weight"] * v["score"], 2), places=2)
            self.assertTrue(set(v["evidence_tx_ids"]) <= ids)
        self.assertAlmostEqual(sum(v["contribution"] for v in a.explainability), a.composite_score, delta=0.06)
        self.assertTrue(vec(a)["structuring_smurfing"]["evidence_tx_ids"])
        self.assertTrue(vec(a)["cash_crypto_wallet_leakage"]["evidence_tx_ids"])
        d = a.to_dict()
        json.dumps(d)
        self.assertEqual(len(d["explainability"]), 10)

    def test_empty_account_has_zero_vector(self):
        a = score_account_mule_risk(stmt(), [])
        self.assertEqual(a.composite_score, 0.0)
        self.assertEqual(len(a.explainability), 10)
        self.assertEqual(a.risk_tier, MuleRiskTier.LOW_RISK)


# ---------------------------------------------------------------------------------------------- profiles
class ProfileTests(unittest.TestCase):
    def setUp(self):
        t = TxB()
        self.s1 = stmt("S1", start="2026-01-01", end="2026-01-31", opening=1000, closing=1500)
        self.s2 = stmt("S2", start="2026-02-01", end="2026-02-28", opening=1500, closing=900)
        # A statement of another person's account that shares the subject's holder name.
        self.s3 = stmt("S3", account="555566667777", holder="SUBJECT ONE", start="2026-01-01", end="2026-01-31")
        self.ents = [
            ResolvedEntity("E1", CASE, "SUBJECT ONE", ["SUBJECT ONE"], ["111122223333"], has_statement=True),
            ResolvedEntity("E9", CASE, "SUBJECT ONE NAMESAKE", ["SUBJECT ONE NAMESAKE"], ["555566667777"], has_statement=True),
            ResolvedEntity("E2", CASE, "ALPHA TRADERS", ["ALPHA TRADERS", "ALFA TRDRS"], []),
        ]
        self.txns = [
            t("2026-01-05", cr=700, bal=1700, sid="S1", cp="ALPHA TRADERS", eid="E2", cat=(2, "Sales"), channel="NEFT"),
            t("2026-01-09", cr=300, bal=2000, sid="S1", cp="ALFA TRDRS", eid="E2", cat=(2, "Sales"), channel="UPI"),
            t("2026-01-20", dr=500, bal=1500, sid="S1", cat=(43, "Food"), channel="UPI"),
            t("2026-02-03", dr=600, bal=900, sid="S2", cp="LANDLORD", cat=(17, "Rent"), channel="IMPS"),
            t("2026-01-10", cr=99999, bal=99999, sid="S3", cp="Z"),
        ]
        self.case = InvestigationCase(CASE, "Risk & Profile ₹ Case – test", WorkspaceMode.CYPHER_TRACE, "2026-01-01")
        self.mules = evaluate_all_mule_accounts([self.s1, self.s2, self.s3], self.txns, self.ents)
        self.profile = build_individual_financial_profile("E1", [self.s1, self.s2, self.s3], self.txns, self.ents, self.mules)

    def test_tg02_opening_closing_per_account(self):
        k = self.profile["kpis"]
        self.assertEqual(k["opening_balance"], 1000.0)
        self.assertEqual(k["closing_balance"], 900.0)

    def test_tg15_account_first_statement_mapping(self):
        self.assertEqual(sorted(s["statement_id"] for s in self.profile["statements"]), ["S1", "S2"])
        self.assertEqual(self.profile["kpis"]["total_credits"], 1000.0)

    def test_tg14_top_parties_by_resolved_entity_never_subject(self):
        senders = self.profile["top_senders"]
        self.assertEqual(len(senders), 1)
        self.assertEqual(senders[0]["party"], "ALPHA TRADERS")
        self.assertEqual(senders[0]["entity_id"], "E2")
        self.assertEqual(senders[0]["amount"], 1000.0)
        parties = [p["party"] for p in self.profile["top_beneficiaries"]]
        self.assertNotIn("SUBJECT ONE", parties)
        self.assertIn("Unidentified counterparty", parties)

    def test_tg16_category_and_channel_breakdowns(self):
        cats = {c["category"]: c for c in self.profile["category_breakdown"]}
        self.assertEqual(cats["Sales"]["category_id"], 2)
        self.assertAlmostEqual(sum(c["share_pct"] for c in cats.values()), 100.0, delta=0.05)
        ch = {c["channel"]: c for c in self.profile["channel_mix"]}
        self.assertEqual(ch["UPI"]["credit"], 300.0)
        self.assertEqual(ch["UPI"]["debit"], 500.0)
        self.assertIn("share_pct", ch["NEFT"])

    # ---------------------------------------------------------------------------------- TG-17 exports
    def test_tg17_profile_html_is_self_contained_and_csp_clean(self):
        doc = reporting.export_profile_html(self.profile, self.case, llm_assisted=False)
        self.assertIn("Individual Financial Profile - SUBJECT ONE", doc)
        for section in ("Monthly Credit", "Category-wise", "Payment Channel Mix", "Top inflow senders", "Mule Risk Explainability"):
            self.assertIn(section, doc)
        self.assertNotIn("<script", doc.lower())
        self.assertIsNone(re.search(r"\sstyle\s*=", doc))
        self.assertIsNone(re.search(r"\son[a-z]+\s*=", doc))
        self.assertIn("deterministic heuristics only", doc)
        self.assertIn("a" * 64, doc)

    def test_tg17_profile_xlsx_sheets(self):
        wb = read_xlsx_bytes(reporting.export_profile_xlsx(self.profile))
        for sheet in ("Profile Summary", "Monthly", "Categories", "Channels", "Top Senders", "Top Beneficiaries",
                      "Mule Explainability", "Statements", "Recent Transactions"):
            self.assertIn(sheet, wb)
        self.assertEqual(len(wb["Mule Explainability"]), 1 + 10)
        self.assertEqual(wb["Top Senders"][1][0], "ALPHA TRADERS")

    def test_tg17_profile_pdf_native(self):
        pdf = reporting.export_profile_pdf(self.profile, self.case, llm_assisted=True, llm_provider="anthropic")
        self.assertTrue(pdf.startswith(b"%PDF-1.4"))
        self.assertTrue(pdf.rstrip().endswith(b"%%EOF"))
        text = pdf.decode("cp1252", errors="replace")
        self.assertIn("Individual Financial Profile - SUBJECT ONE", text)
        self.assertIn(r"Cloud AI assistance \(anthropic\)", text)
        self.assertIn("SHA-256: " + "a" * 64, text)
        self.assertNotIn("Fixture", text)
        self._check_xref(pdf)

    def test_tg17_dispatch(self):
        for fmt, ctype in (("html", "text/html"), ("xlsx", "spreadsheetml"), ("pdf", "application/pdf")):
            body, content_type, name = reporting.export_entity_profile(self.profile, fmt, self.case)
            self.assertIn(ctype, content_type)
            self.assertTrue(name.endswith("." + fmt))
            self.assertTrue(body)
        with self.assertRaises(ValueError):
            reporting.export_entity_profile(self.profile, "docx")
        with self.assertRaises(ValueError):
            reporting.export_entity_profile({"error": "Entity 'X' not found"}, "pdf")

    def _check_xref(self, pdf: bytes) -> None:
        start = int(pdf.rsplit(b"startxref", 1)[1].split()[0])
        self.assertTrue(pdf[start:].startswith(b"xref"))
        offsets = [int(l.split()[0]) for l in pdf[start:].split(b"\n")[3:] if l.endswith(b" n ")]
        for i, off in enumerate(offsets, start=1):
            self.assertTrue(pdf[off:].startswith(f"{i} 0 obj".encode()), i)

    # ------------------------------------------------------------------- BE-20 / EDGE-19 / EDGE-13 / BE-18
    def test_be20_case_workbook_has_entity_itr_loan_bidirectional_sheets(self):
        wb = read_xlsx_bytes(reporting.export_audit_workbook_xlsx(
            self.case, [self.s1, self.s2], self.txns[:4], self.ents, self.mules, loops=[]))
        for sheet in ("Executive Summary", "Mule Risk Scorecard", "Mule Explainability", "Entities", "Bidirectional Flows",
                      "ITR Summary", "ITR Red Flags", "Loan Eligibility", "Loan Monthly", "Chain of Custody"):
            self.assertIn(sheet, wb)
        self.assertEqual(len(wb["Entities"]), 1 + len(self.ents))

    def test_edge19_itr_workbook(self):
        wb = read_xlsx_bytes(reporting.export_itr_workbook_xlsx(self.txns[:4], [self.s1, self.s2]))
        self.assertIn("ITR Summary", wb)
        self.assertIn("ITR Category Schedule", wb)
        self.assertEqual(wb["ITR Summary"][1][1], "FY 2025-26")
        with self.assertRaises(ValueError):
            reporting.export_itr_workbook_xlsx(self.txns[:4], [self.s1], fy="FY 2019-20")

    def test_edge13_tally_workbook_matches_xml(self):
        from cyphersol.edge.tally_exporter import generate_tally_xml

        _xml, stats = generate_tally_xml("t", [self.s1, self.s2], self.txns[:4], include_suspense=True)
        wb = read_xlsx_bytes(reporting.export_tally_workbook_xlsx("t", [self.s1, self.s2], self.txns[:4], include_suspense=True))
        for sheet in ("Vouchers", "Ledger Entries", "Ledger Masters", "Groups", "Summary"):
            self.assertIn(sheet, wb)
        vouchers = wb["Vouchers"][1:]
        self.assertEqual(len(vouchers), stats["vouchers_exported"])
        self.assertTrue(all(v[3] and v[4] and float(v[5]) > 0 for v in vouchers))
        entries = wb["Ledger Entries"][1:]
        self.assertAlmostEqual(sum(float(e[6]) for e in entries), 0.0, places=2)

    def test_be18_dossier_pdf_is_laid_out_and_unicode_safe(self):
        pdf = reporting.export_forensic_dossier_pdf(self.case, [self.s1, self.s2], self.txns[:4], self.ents, self.mules,
                                                    llm_assisted=False, loops=[])
        text = pdf.decode("cp1252", errors="replace")
        self.assertIn("Rs. ", text)            # rupee transliterated
        self.assertIn("–", text)           # en dash kept via WinAnsi, not '?'
        self.assertIn("S10", text)              # all 10 parameters tabulated
        self.assertIn(" re f", text)            # drawn table/KPI boxes
        self.assertIn("deterministic heuristics only", text)
        self.assertNotIn("air-gapped", text.lower())
        self._check_xref(pdf)

    def test_be19_certificate_wording_follows_recorded_llm_use(self):
        yes = reporting.export_forensic_dossier_html(self.case, [self.s1], self.txns[:3], self.ents, self.mules,
                                                     llm_assisted=True, llm_provider="gemini", loops=[], graph={"nodes": [], "edges": []})
        self.assertIn("Cloud AI assistance (gemini)", yes)
        unknown = reporting.custody_processing_statement(self.txns[:3])
        self.assertIn("was not recorded", unknown)

    def test_dossier_html_lists_all_ten_parameters(self):
        doc = reporting.export_forensic_dossier_html(self.case, [self.s1], self.txns[:3], self.ents, self.mules,
                                                     loops=[], graph={"nodes": [], "edges": []})
        self.assertIn("S10 Round", doc)
        self.assertIn("Top drivers", doc)


class SampleCaseRegressionTests(unittest.TestCase):
    def test_sample_case_scores_and_exports(self):
        import shutil
        import tempfile
        from cyphersol.service import CypherApplicationService

        tmp = tempfile.mkdtemp(prefix="p1risk_", dir="/private/tmp/claude-501/cyphersol-p1") if os.path.isdir(
            "/private/tmp/claude-501/cyphersol-p1") else tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        # Seed from a copy: demo seeding regenerates sample files, which must not rewrite the repo's sample_statements.
        samples = os.path.join(tmp, "sample_statements")
        shutil.copytree(os.path.join(ROOT_DIR, "sample_statements"), samples)
        svc = CypherApplicationService(os.path.join(tmp, "v.db"), samples, seed_demo=True)
        mules = svc.get_mule_assessments("CASE_TRACE_EOW_041")
        ramesh = next(m for m in mules if "RAMESH" in m["account_holder_name"].upper())
        self.assertGreaterEqual(ramesh["composite_score"], 80.0)
        self.assertEqual(len(ramesh["explainability"]), 10)
        prof = svc.get_financial_profile("CASE_TRACE_EOW_041", entity_id=ramesh["entity_id"])
        for fmt in reporting.PROFILE_EXPORT_FORMATS:
            body, _, _ = reporting.export_entity_profile(prof, fmt)
            self.assertGreater(len(body), 500)
        wb = read_xlsx_bytes(svc.export_xlsx("CASE_TRACE_EOW_041"))
        self.assertIn("Bidirectional Flows", wb)
        self.assertGreater(len(wb["Bidirectional Flows"]), 1)


if __name__ == "__main__":
    unittest.main()
