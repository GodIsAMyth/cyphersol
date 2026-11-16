"""P1 requirement-gap tests for CypherEdge (roadmap item 22).

EDGE-10 configurable ledger/group map, EDGE-28/29 structured export and safe ledger names,
EDGE-18/19/20 ITR deductions, investment/property SFT and the reconciliation waterfall plus the
per-FY checks_run catalogue, EDGE-22/25/26/27 loan ABB coverage, penal events, income basis and
input validation.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal
from typing import Optional

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.edge.itr_compliance import (  # noqa: E402
    generate_itr_compliance_report,
    itr_check_catalogue,
)
from cyphersol.edge.loan_eligibility import compute_loan_eligibility_report  # noqa: E402
from cyphersol.edge.tally_exporter import (  # noqa: E402
    PREDEFINED_TALLY_GROUPS,
    TallyLedgerMap,
    build_tally_export,
    generate_tally_xml,
)
from cyphersol.models import BankAccountStatement, NormalizedTransaction, TxDirection  # noqa: E402
from cyphersol.taxonomy import CATEGORY_BY_ID  # noqa: E402


def mk_stmt(sid="ST1", acct="123456789012", bank="SBI", start="2025-04-01", end="2025-06-30",
            opening="0", closing="0", account_type="Savings") -> BankAccountStatement:
    return BankAccountStatement(
        statement_id=sid, case_id="CASE_T", source_filename=f"{sid}.pdf", sha256_hash="0" * 64,
        bank_code=bank, bank_name=bank, account_number=acct, account_holder_name="TEST HOLDER",
        period_start=start, period_end=end, opening_balance=Decimal(opening),
        closing_balance=Decimal(closing), total_transactions=0, account_type=account_type,
    )


def mk_tx(idx, date, narr, dr=0.0, cr=0.0, bal=0.0, cat=52, sid="ST1", cp: Optional[str] = None,
          channel="TRANSFER") -> NormalizedTransaction:
    spec = CATEGORY_BY_ID[cat]
    return NormalizedTransaction(
        tx_id=f"{sid}_{idx}", statement_id=sid, case_id="CASE_T", row_index=idx, txn_date=date,
        raw_narration=narr, payment_channel=channel,
        direction=TxDirection.CREDIT if cr > 0 else TxDirection.DEBIT,
        debit=Decimal(str(dr)), credit=Decimal(str(cr)), running_balance=Decimal(str(bal)),
        category_id=cat, category_name=spec.name, categorization_source="rule", confidence_score=0.9,
        extracted_counterparty=cp, tally_voucher_type=spec.tally_voucher, tally_ledger_name=None,
    )


def _ledgers(root):
    return {l.get("NAME"): (l.findtext("PARENT") or "") for l in root.findall(".//LEDGER")}


def _groups(root):
    return {g.get("NAME"): (g.findtext("PARENT") or "") for g in root.findall(".//GROUP")}


# ---------------------------------------------------------------------------------------------- Tally

class TallyLedgerMapTests(unittest.TestCase):
    def test_edge10_defaults_round_trip_json(self) -> None:
        m = TallyLedgerMap.defaults()
        d = m.to_dict()
        json.dumps(d)  # JSON-serialisable
        self.assertEqual(len(d["category_groups"]), 52)
        self.assertEqual(d["category_groups"]["16"], "Indirect Expenses")
        again = TallyLedgerMap.from_dict(json.loads(json.dumps(d)))
        self.assertEqual(again.to_dict(), d)
        self.assertEqual(TallyLedgerMap.from_dict({}).to_dict(), d)  # partial dict = defaults

    def test_edge10_validation_errors(self) -> None:
        bad = [
            {"category_groups": {"99": "Indirect Expenses"}},             # unknown category
            {"category_groups": {"16": "Telephone Group"}},                # non-predefined, not declared
            {"custom_groups": {"Telephone Group": "No Such Parent"}},      # custom parent invalid
            {"category_groups": {"50": "Indirect Expenses"}},              # contra structure is fixed
            {"counterparty_ledgers": {"AIRTEL": {"ledger": ""}}},          # empty ledger
            {"counterparty_ledgers": {"AIRTEL": {"ledger": "Indirect Expenses"}}},  # clashes with group
            {"voucher_prefix": "bad prefix!"},
            {"sales_categories": [16]},                                    # DR category as Sales
            {"unknown_key": 1},
        ]
        for payload in bad:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    TallyLedgerMap.from_dict(payload)
        errs = TallyLedgerMap.validate_dict({"category_groups": {"99": "X"}, "voucher_prefix": "a b"})
        self.assertGreaterEqual(len(errs), 2)

    def test_edge10_map_applies_groups_overrides_and_custom_group(self) -> None:
        st = mk_stmt()
        txns = [
            mk_tx(0, "2025-04-02", "UPI AIRTEL PREPAID", dr=499, cat=16, cp="AIRTEL"),
            mk_tx(1, "2025-04-03", "NEFT VENDOR X", dr=50000, cat=11, cp="Vendor X"),
            mk_tx(2, "2025-04-04", "SAAS", dr=900, cat=17, cp="ACME CLOUD"),
            mk_tx(3, "2025-04-05", "NEFT CUSTOMER", cr=100000, cat=2, cp="Buyer Co"),
        ]
        m = TallyLedgerMap.from_dict({
            "company_name": "Acme Traders (2025-26)",
            "use_trade_vouchers": True,
            "voucher_prefix": "AC",
            "custom_groups": {"Software Expenses": "Indirect Expenses"},
            "category_groups": {"17": "Software Expenses"},
            "counterparty_ledgers": {
                "airtel": {"ledger": "Telephone & Internet Expenses", "group": "Indirect Expenses"},
                "VENDOR X": {"ledger": "X Enterprises Pvt Ltd", "group": "Sundry Creditors"},
            },
        })
        xml, stats = generate_tally_xml("Case", [st], txns, ledger_map=m)
        root = ET.fromstring(xml)
        led = _ledgers(root)
        self.assertEqual(led["Telephone & Internet Expenses"], "Indirect Expenses")
        self.assertEqual(led["X Enterprises Pvt Ltd"], "Sundry Creditors")
        self.assertEqual(led["ACME CLOUD"], "Software Expenses")
        self.assertEqual(_groups(root)["Software Expenses"], "Indirect Expenses")
        self.assertEqual(root.findtext(".//SVCURRENTCOMPANY"), "Acme Traders (2025-26)")
        types = {v.findtext("VOUCHERTYPENAME") for v in root.findall(".//VOUCHER")}
        self.assertIn("Sales", types)
        self.assertIn("Purchase", types)
        self.assertTrue(all(v.findtext("VOUCHERNUMBER").startswith("AC-") for v in root.findall(".//VOUCHER")))
        # Explicit kwargs still win over the map
        xml2, _ = generate_tally_xml("Case", [st], txns, ledger_map=m, use_trade_vouchers=False)
        types2 = {v.findtext("VOUCHERTYPENAME") for v in ET.fromstring(xml2).findall(".//VOUCHER")}
        self.assertNotIn("Sales", types2)
        # Every parent is predefined or an emitted group
        groups = _groups(root)
        for name, parent in led.items():
            self.assertTrue(parent in PREDEFINED_TALLY_GROUPS or parent in groups, (name, parent))
        # A plain dict is accepted too
        xml3, _ = generate_tally_xml("Case", [st], txns, ledger_map=m.to_dict())
        self.assertEqual(xml3, xml)


class TallyStructuredExportTests(unittest.TestCase):
    def test_structured_vouchers_ledgers_and_preview(self) -> None:
        st = mk_stmt()
        txns = [mk_tx(i, f"2025-04-{i + 1:02d}", f"NEFT PARTY {i} " + "X" * 40, dr=1000 + i, cat=11, cp=f"Party {i}")
                for i in range(30)]
        exp = build_tally_export("Case", [st], txns, preview_chars=2000)
        self.assertEqual(set(exp) >= {"xml", "xml_preview", "xml_truncated", "xml_sha256", "xml_bytes",
                                      "stats", "vouchers", "ledgers", "groups"}, True)
        self.assertTrue(exp["xml_truncated"])
        self.assertLessEqual(len(exp["xml_preview"]), 2000)
        self.assertEqual(len(exp["vouchers"]), exp["stats"]["vouchers_exported"])
        v = exp["vouchers"][0]
        for key in ("voucher_number", "voucher_type", "date", "guid", "amount", "party_ledger", "bank_ledger",
                    "narration", "tx_id", "category_id", "entries"):
            self.assertIn(key, v)
        self.assertEqual(sum(e["amount"] for e in v["entries"]), 0)  # balanced
        led_names = {l["name"] for l in exp["ledgers"]}
        self.assertEqual(led_names, set(_ledgers(ET.fromstring(exp["xml"]))))
        party = next(l for l in exp["ledgers"] if l["name"] == "Party 0")
        self.assertEqual(party["voucher_count"], 1)
        self.assertAlmostEqual(party["amount"], 1000.0)
        json.dumps({k: v for k, v in exp.items() if k != "xml"})

    def test_edge29_truncated_ledger_names_do_not_merge(self) -> None:
        st = mk_stmt()
        base = "M/S SHREE GANESH ENTERPRISES AND GENERAL TRADING COMPANY PRIVATE LIMITED"
        txns = [
            mk_tx(0, "2025-04-02", "NEFT", dr=1000, cat=11, cp=base + " MUMBAI BRANCH"),
            mk_tx(1, "2025-04-03", "NEFT", dr=2000, cat=11, cp=base + " PUNE BRANCH"),
        ]
        root = ET.fromstring(generate_tally_xml("Case", [st], txns)[0])
        names = [n for n in _ledgers(root) if n.startswith("M/S")]
        self.assertEqual(len(names), 2)
        self.assertTrue(all(len(n) <= 64 for n in names))
        parties = {e.findtext("LEDGERNAME") for e in root.findall(".//ALLLEDGERENTRIES.LIST")}
        self.assertTrue(set(names) <= parties)

    def test_edge29_no_orphan_groups(self) -> None:
        st = mk_stmt()
        m = TallyLedgerMap.from_dict({
            "custom_groups": {"Software Expenses": "Indirect Expenses", "Unused Group": "Indirect Expenses"},
            "category_groups": {"17": "Software Expenses"},
        })
        txns = [mk_tx(0, "2025-04-02", "UPI", dr=500, cat=16, cp="AIRTEL")]
        root = ET.fromstring(generate_tally_xml("Case", [st], txns, ledger_map=m)[0])
        self.assertEqual(_groups(root), {})


# ---------------------------------------------------------------------------------------------- ITR

class ItrP1Tests(unittest.TestCase):
    def test_checks_run_catalogue_per_fy(self) -> None:
        old = {c["id"]: c for c in itr_check_catalogue("FY 2024-25")}
        new = {c["id"]: c for c in itr_check_catalogue("FY 2025-26")}
        for cid in ("194C", "194H", "194I", "194J", "40A3", "269ST", "SFT003", "SFT004", "SFT005", "SFT007",
                    "SFT009", "SFT012", "VDA"):
            self.assertIn(cid, new)
            self.assertIn("rule", new[cid])
            self.assertIn("thresholds", new[cid])
        self.assertEqual(old["194J"]["thresholds"]["aggregate_limit"], 30000)
        self.assertEqual(new["194J"]["thresholds"]["aggregate_limit"], 50000)
        self.assertEqual(new["194I"]["thresholds"]["period"], "month")

        txns = [mk_tx(0, "2025-05-01", "NEFT LEGAL FEES", dr=60000, cat=18, cp="ABC LLP")]
        rep = generate_itr_compliance_report(txns)
        runs = {c["id"]: c for c in rep["checks_run"]}
        self.assertEqual(runs["194J"]["status"], "flag")
        self.assertEqual(runs["194J"]["flag_count"], 1)
        self.assertEqual(runs["40A3"]["status"], "pass")
        self.assertTrue(all(f.get("check_id") for f in rep["statutory_red_flags"]))

    def test_edge18_deduction_bucketing(self) -> None:
        txns = [
            mk_tx(0, "2025-05-01", "LIC PREMIUM POLICY 123", dr=20000, cat=35),
            mk_tx(1, "2025-05-02", "STAR HEALTH INSURANCE PREMIUM", dr=15000, cat=35),
            mk_tx(2, "2025-05-03", "ICICI LOMBARD MOTOR INSURANCE", dr=8000, cat=35),
            mk_tx(3, "2025-05-04", "SIP AXIS ELSS TAX SAVER", dr=5000, cat=31),
            mk_tx(4, "2025-05-05", "SIP HDFC FLEXI CAP", dr=7000, cat=31),
            mk_tx(5, "2025-05-06", "NPS TIER 1 CONTRIBUTION", dr=50000, cat=36),
            mk_tx(6, "2025-05-07", "PPF DEPOSIT", dr=10000, cat=36),
            mk_tx(7, "2025-05-08", "HDFC HOME LOAN EMI", dr=40000, cat=23),
        ]
        rep = generate_itr_compliance_report(txns)
        dc = rep["deduction_candidates"]
        self.assertEqual(dc["sec_80c_elss_lic_pf"], 35000.0)       # LIC + ELSS + PPF
        self.assertEqual(dc["sec_80d_health_insurance"], 15000.0)
        self.assertEqual(dc["sec_80ccd_nps"], 50000.0)
        self.assertEqual(dc["home_loan_emi_outflow"], 40000.0)
        self.assertEqual(dc["not_deductible_reviewed"], 15000.0)    # motor + non-ELSS SIP
        buckets = {b["section"]: b for b in rep["deductions"]}
        self.assertEqual(buckets["Sec 80C"]["tx_count"], 3)
        self.assertEqual(buckets["Sec 80C"]["statutory_cap"], 150000)
        self.assertIn("Sec 24(b) / 80C", buckets)

    def test_edge19_investment_and_property_sft(self) -> None:
        txns = [
            mk_tx(0, "2025-05-01", "FD BOOKING", dr=1200000, cat=33),
            mk_tx(1, "2025-05-02", "MF PURCHASE HDFC MF", dr=1100000, cat=31),
            mk_tx(2, "2025-05-03", "ZERODHA BROKING", dr=1050000, cat=32),
            mk_tx(3, "2025-05-04", "RTGS XYZ DEVELOPERS FLAT BOOKING SALE DEED", dr=3500000, cat=52),
        ]
        rep = generate_itr_compliance_report(txns)
        secs = {f["check_id"] for f in rep["statutory_red_flags"]}
        self.assertTrue({"SFT004", "SFT009", "SFT007", "SFT012"} <= secs, secs)
        below = generate_itr_compliance_report([mk_tx(0, "2025-05-01", "FD", dr=999999, cat=33)])
        self.assertNotIn("SFT004", {f["check_id"] for f in below["statutory_red_flags"]})

    def test_edge20_reconciliation_waterfall_balances(self) -> None:
        txns = [
            mk_tx(0, "2025-05-01", "SALES", cr=500000, cat=2),
            mk_tx(1, "2025-05-02", "SELF TRF", cr=100000, cat=49),
            mk_tx(2, "2025-05-03", "CASH DEP", cr=50000, cat=50),
            mk_tx(3, "2025-05-04", "LOAN DISB", cr=300000, cat=8),
            mk_tx(4, "2025-05-05", "FD MATURITY", cr=200000, cat=34),
            mk_tx(5, "2025-05-06", "SALARY", cr=80000, cat=1),
            mk_tx(6, "2025-05-07", "IT REFUND", cr=12000, cat=7),
            mk_tx(7, "2025-05-08", "UNKNOWN", cr=7000, cat=52),
        ]
        rep = generate_itr_compliance_report(txns)
        wf = rep["reconciliation_waterfall"]
        self.assertEqual(wf[0]["step"], "gross_credits")
        self.assertEqual(wf[0]["amount"], 1249000.0)
        self.assertEqual(wf[-1]["step"], "unexplained_credits")
        total = sum(s["amount"] for s in wf if s["kind"] == "less")
        heads = sum(s["amount"] for s in wf if s["kind"] == "head")
        self.assertAlmostEqual(wf[0]["amount"] - total, heads + wf[-1]["amount"], places=2)
        self.assertAlmostEqual(wf[-1]["amount"], 7000.0)
        self.assertTrue(rep["reconciliation"]["reconciles"])
        self.assertEqual(rep["reconciliation"]["excluded_contra_and_cash_deposits"], 150000.0)


# ---------------------------------------------------------------------------------------------- Loan

class LoanP1Tests(unittest.TestCase):
    def test_edge22_gap_between_statements_not_counted(self) -> None:
        s1 = mk_stmt("A1", start="2025-04-01", end="2025-04-30", opening="1000")
        s2 = mk_stmt("A2", start="2025-06-01", end="2025-06-30", opening="9000")
        txns = [mk_tx(0, "2025-04-10", "C", cr=1000, bal=2000, cat=2, sid="A1"),
                mk_tx(0, "2025-06-10", "C", cr=1000, bal=10000, cat=2, sid="A2")]
        rep = compute_loan_eligibility_report(txns, statements=[s1, s2])
        may = next(m for m in rep["monthly_breakdown"] if m["month"] == "2025-05")
        self.assertEqual(may["days_covered"], 0)
        self.assertEqual(may["accounts_covered"], [])
        acct = rep["per_account_abb"][0]
        self.assertEqual(acct["gap_days"], 31)
        self.assertEqual(acct["coverage_gaps"], [{"from": "2025-05-01", "to": "2025-05-31", "days": 31}])
        self.assertEqual(rep["months_with_balance_data"], 2)
        # ABB is the mean of covered months only
        self.assertEqual(rep["months_analyzed"], 3)

    def test_edge22_abb_bounded_by_statement_period(self) -> None:
        st = mk_stmt(start="2025-04-15", end="2025-05-14", opening="1000")
        txns = [mk_tx(0, "2025-04-20", "C", cr=1000, bal=2000, cat=2)]
        rep = compute_loan_eligibility_report(txns, statements=[st])
        apr = rep["monthly_breakdown"][0]
        self.assertEqual(apr["days_covered"], 16)
        self.assertEqual(rep["period_start"], "2025-04-15")

    def test_consolidated_coverage_reported(self) -> None:
        s1 = mk_stmt("A", acct="111", start="2025-04-01", end="2025-04-30", opening="1000")
        s2 = mk_stmt("B", acct="222", start="2025-04-16", end="2025-04-30", opening="5000")
        txns = [mk_tx(0, "2025-04-02", "X", cr=1, bal=1001, cat=4, sid="A"),
                mk_tx(0, "2025-04-20", "X", cr=1, bal=5001, cat=4, sid="B")]
        rep = compute_loan_eligibility_report(txns, statements=[s1, s2])
        apr = rep["monthly_breakdown"][0]
        self.assertEqual(sorted(apr["accounts_covered"]), ["SBI - 111", "SBI - 222"])
        self.assertEqual(apr["full_coverage_days"], 15)
        segs = rep["consolidated_coverage"]["segments"]
        self.assertEqual(segs[0], {"from": "2025-04-01", "to": "2025-04-15", "days": 15, "accounts": ["SBI - 111"]})
        self.assertEqual(segs[1]["accounts"], ["SBI - 111", "SBI - 222"])
        self.assertIn("basis", rep["consolidated_coverage"])
        self.assertEqual(rep["consolidated_coverage"]["partial_coverage_days"], 15)

    def test_edge25_penal_events_and_cash_pct(self) -> None:
        st = mk_stmt(start="2025-04-01", end="2025-04-30", opening="10000")
        txns = [
            mk_tx(0, "2025-04-02", "CASH DEP", cr=20000, bal=30000, cat=50),
            mk_tx(1, "2025-04-03", "NEFT SALES", cr=80000, bal=110000, cat=2),
            mk_tx(2, "2025-04-04", "ATM WDL", dr=25000, bal=85000, cat=51),
            mk_tx(3, "2025-04-05", "MIN BAL CHARGES NON MAINT", dr=590, bal=84410, cat=27),
            mk_tx(4, "2025-04-06", "TOD INTEREST CHARGED", dr=120, bal=84290, cat=29),
            mk_tx(5, "2025-04-07", "PENAL INTEREST", dr=50, bal=84240, cat=29),
            mk_tx(6, "2025-04-08", "SMS CHARGES", dr=15, bal=84225, cat=27),
            mk_tx(7, "2025-04-09", "NEFT VENDOR", dr=90000, bal=-5775, cat=11),
            mk_tx(8, "2025-04-12", "NEFT SALES", cr=10000, bal=4225, cat=2),
        ]
        rep = compute_loan_eligibility_report(txns, statements=[st])
        self.assertEqual(rep["penal_event_count"], 3)
        kinds = {e["type"] for e in rep["penal_events"]}
        self.assertEqual(kinds, {"MIN_BALANCE_PENALTY", "TOD_OVERDRAWN", "PENAL_INTEREST"})
        self.assertEqual(rep["overdrawn_days"], 3)  # 9, 10, 11 April
        self.assertAlmostEqual(rep["cash_deposit_pct"], 20000 / 110000 * 100, places=1)
        self.assertAlmostEqual(rep["cash_withdrawal_pct"], 25000 / (25000 + 590 + 120 + 50 + 15 + 90000) * 100,
                               places=1)
        apr = rep["monthly_breakdown"][0]
        for key in ("cash_deposits", "cash_withdrawals", "cash_deposit_pct", "cash_withdrawal_pct",
                    "net_surplus", "penal_events"):
            self.assertIn(key, apr)

    def test_edge26_income_basis_exposed_and_configurable(self) -> None:
        st = mk_stmt(start="2025-04-01", end="2025-04-30", opening="100000")
        txns = [mk_tx(0, "2025-04-03", "NEFT SALES", cr=100000, bal=200000, cat=2)]
        rep = compute_loan_eligibility_report(txns, statements=[st])
        self.assertEqual(rep["income_basis"]["method"], "banking_surrogate")
        self.assertEqual(rep["income_basis"]["business_income_factor"], 0.20)
        rep2 = compute_loan_eligibility_report(txns, statements=[st], business_income_factor=0.5,
                                               abb_income_factor=0.0)
        self.assertAlmostEqual(rep2["assessable_monthly_income"], 50000, places=0)
        rep3 = compute_loan_eligibility_report(txns, statements=[st], monthly_income_override=75000)
        self.assertEqual(rep3["assessable_monthly_income"], 75000)
        self.assertEqual(rep3["income_basis"]["method"], "override")

    def test_edge27_input_validation(self) -> None:
        txns = [mk_tx(0, "2025-04-03", "X", cr=1, bal=1, cat=2)]
        for kwargs in ({"foir_pct": 0}, {"foir_pct": 150}, {"foir_pct": float("nan")},
                       {"annual_interest_rate": -1}, {"annual_interest_rate": 80}, {"tenure_months": 0},
                       {"tenure_months": 1000}, {"business_income_factor": -0.1},
                       {"monthly_income_override": -5}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(ValueError):
                    compute_loan_eligibility_report(txns, **kwargs)


if __name__ == "__main__":
    unittest.main()
