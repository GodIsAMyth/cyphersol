"""P0 regression tests for CypherEdge: Tally export, ITR compliance and loan eligibility.

Each test reproduces a finding from docs/REVIEW_2026-11.md (EDGE-02 ... EDGE-24).
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import List, Optional

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.edge import tally_exporter as tx_mod  # noqa: E402
from cyphersol.edge.itr_compliance import generate_itr_compliance_report  # noqa: E402
from cyphersol.edge.loan_eligibility import compute_loan_eligibility_report  # noqa: E402
from cyphersol.edge.tally_exporter import (  # noqa: E402
    PREDEFINED_TALLY_GROUPS,
    generate_tally_xml,
    push_to_tally_http,
)
from cyphersol.models import BankAccountStatement, NormalizedTransaction, TxDirection  # noqa: E402
from cyphersol.taxonomy import CATEGORY_BY_ID  # noqa: E402


def mk_stmt(
    sid: str = "ST1",
    acct: str = "123456789012",
    bank: str = "SBI",
    start: str = "2025-04-01",
    end: str = "2025-06-30",
    opening: str = "0",
    closing: str = "0",
    account_type: str = "Savings",
) -> BankAccountStatement:
    return BankAccountStatement(
        statement_id=sid,
        case_id="CASE_T",
        source_filename=f"{sid}.pdf",
        sha256_hash="0" * 64,
        bank_code=bank,
        bank_name=bank,
        account_number=acct,
        account_holder_name="TEST HOLDER",
        period_start=start,
        period_end=end,
        opening_balance=Decimal(opening),
        closing_balance=Decimal(closing),
        total_transactions=0,
        account_type=account_type,
    )


def mk_tx(
    idx: int,
    date: str,
    narr: str,
    dr: float = 0.0,
    cr: float = 0.0,
    bal: float = 0.0,
    cat: int = 52,
    sid: str = "ST1",
    cp: Optional[str] = None,
    utr: Optional[str] = None,
    cp_acc: Optional[str] = None,
    tx_id: Optional[str] = None,
    channel: str = "TRANSFER",
) -> NormalizedTransaction:
    spec = CATEGORY_BY_ID[cat]
    return NormalizedTransaction(
        tx_id=tx_id or f"{sid}_{idx}",
        statement_id=sid,
        case_id="CASE_T",
        row_index=idx,
        txn_date=date,
        raw_narration=narr,
        payment_channel=channel,
        direction=TxDirection.CREDIT if cr > 0 else TxDirection.DEBIT,
        debit=Decimal(str(dr)),
        credit=Decimal(str(cr)),
        running_balance=Decimal(str(bal)),
        category_id=cat,
        category_name=spec.name,
        categorization_source="rule",
        confidence_score=0.9,
        utr_number=utr,
        extracted_counterparty=cp,
        counterparty_vpa_or_acc=cp_acc,
        tally_voucher_type=spec.tally_voucher,
        tally_ledger_name=cp or spec.name,
    )


def _vouchers(root: ET.Element) -> List[ET.Element]:
    return root.findall(".//VOUCHER")


def _ledgers(root: ET.Element) -> dict:
    return {l.get("NAME"): (l.findtext("PARENT") or "") for l in root.findall(".//LEDGER")}


def _groups(root: ET.Element) -> dict:
    return {g.get("NAME"): (g.findtext("PARENT") or "") for g in root.findall(".//GROUP")}


class TallyExporterTests(unittest.TestCase):
    def test_edge02_quotes_and_control_chars_produce_valid_xml(self) -> None:
        st = mk_stmt()
        txns = [
            mk_tx(0, "2025-04-02", 'NEFT/DR/"ACME" & <SONS>\x01\x0b', dr=1000, bal=9000, cat=11, cp='ACME "QUOTED" & SONS\x02'),
            mk_tx(1, "2025-04-03", "UPI/CR/O'BRIEN\x1f", cr=500, bal=9500, cat=2, cp="O'BRIEN"),
        ]
        xml, _ = generate_tally_xml("Case", [st], txns, company_name="My \"Co\" <Ltd>\x07")
        root = ET.fromstring(xml)  # must not raise
        names = set(_ledgers(root))
        self.assertIn('ACME "QUOTED" & SONS', names)
        self.assertIn("O'BRIEN", names)
        for ch in ("\x01", "\x02", "\x07", "\x0b", "\x1f"):
            self.assertNotIn(ch, xml)
        self.assertEqual(root.findtext(".//SVCURRENTCOMPANY"), 'My "Co" <Ltd>')

    def test_edge03_every_ledger_parent_is_predefined_or_emitted_group(self) -> None:
        st = mk_stmt()
        txns = [
            mk_tx(0, "2025-04-02", "SMS CHARGES", dr=20, bal=980, cat=27),
            mk_tx(1, "2025-04-03", "SWIGGY", dr=300, bal=680, cat=43, cp="SWIGGY"),
            mk_tx(2, "2025-04-04", "NACH RTN CHG", dr=500, bal=180, cat=28),
        ]
        xml, stats = generate_tally_xml("Co", [st], txns)
        root = ET.fromstring(xml)
        groups = _groups(root)
        for gname, gparent in groups.items():
            self.assertIn(gparent, PREDEFINED_TALLY_GROUPS, gname)
            self.assertNotIn(gname, PREDEFINED_TALLY_GROUPS)
        for lname, parent in _ledgers(root).items():
            self.assertTrue(parent in PREDEFINED_TALLY_GROUPS or parent in groups, (lname, parent))
        # Non-predefined parents (from the taxonomy, before or after any remap) must
        # never reach a LEDGER without a GROUP master.
        msgs = list(root.iter("TALLYMESSAGE"))
        first_ledger = next(i for i, m in enumerate(msgs) if m.find("LEDGER") is not None)
        group_positions = [i for i, m in enumerate(msgs) if m.find("GROUP") is not None]
        self.assertTrue(all(p < first_ledger for p in group_positions))
        self.assertEqual(stats["groups_created"], len(groups))

    def test_edge03_robust_to_unknown_custom_parent(self) -> None:
        # Simulate a taxonomy where the category points to an arbitrary custom group.
        st = mk_stmt()
        t = mk_tx(0, "2025-04-02", "MISC", dr=100, bal=900, cat=43, cp="SHOP")
        orig = tx_mod.CATEGORY_BY_ID
        try:
            from dataclasses import replace

            patched = dict(orig)
            patched[43] = replace(orig[43], tally_parent_group="Lifestyle Spends")
            tx_mod.CATEGORY_BY_ID = patched
            xml, _ = generate_tally_xml("Co", [st], [t])
        finally:
            tx_mod.CATEGORY_BY_ID = orig
        root = ET.fromstring(xml)
        groups = _groups(root)
        self.assertIn("Lifestyle Spends", groups)
        self.assertIn(groups["Lifestyle Spends"], PREDEFINED_TALLY_GROUPS)
        self.assertEqual(_ledgers(root)["SHOP"], "Lifestyle Spends")

    def test_edge04_suspense_ledger_name_differs_from_group(self) -> None:
        st = mk_stmt()
        t = mk_tx(0, "2025-04-02", "TRF/998812/UNKNOWN", dr=100, bal=900, cat=52)
        t.tally_ledger_name = "Suspense A/c"  # what the categorizer sets
        xml, _ = generate_tally_xml("Co", [st], [t], include_suspense=True)
        root = ET.fromstring(xml)
        ledgers = _ledgers(root)
        self.assertNotIn("Suspense A/c", ledgers)
        sus = [n for n, p in ledgers.items() if p == "Suspense A/c"]
        self.assertEqual(len(sus), 1)
        v = _vouchers(root)[0]
        self.assertEqual(v.get("VCHTYPE"), "Payment")  # not Journal against a bank ledger
        self.assertFalse(set(ledgers) & (set(_groups(root)) | PREDEFINED_TALLY_GROUPS))

    def test_edge05_cat49_no_fake_bank_ledgers_and_mirror_dedup(self) -> None:
        s1 = mk_stmt("ST1", acct="111122223333", bank="SBI")
        s2 = mk_stmt("ST2", acct="444455556666", bank="HDFC")
        out_leg = mk_tx(0, "2025-04-10", "NEFT/DR/SELF/HDFC", dr=50000, bal=10000, cat=49, sid="ST1",
                        utr="UTR123", cp="TEST HOLDER")
        in_leg = mk_tx(0, "2025-04-11", "NEFT/CR/SELF/SBI", cr=50000, bal=60000, cat=49, sid="ST2",
                       utr="UTR123", cp="TEST HOLDER")
        # Transfer to an own account that was NOT uploaded: no Bank Accounts ledger may be invented.
        orphan = mk_tx(1, "2025-04-12", "SELF TRANSFER TO KOTAK OWN A/C", dr=1000, bal=9000, cat=49, sid="ST1",
                       cp="KOTAK OWN")
        xml, stats = generate_tally_xml("Co", [s1, s2], [out_leg, in_leg, orphan])
        root = ET.fromstring(xml)
        ledgers = _ledgers(root)
        bank_ledgers = {n for n, p in ledgers.items() if p == "Bank Accounts"}
        self.assertEqual(bank_ledgers, {"SBI - 3333", "HDFC - 6666"})
        vouchers = _vouchers(root)
        self.assertEqual(len(vouchers), 2)  # mirror leg deduped
        self.assertEqual(stats["mirror_legs_deduped"], 1)
        contra = [v for v in vouchers if v.get("VCHTYPE") == "Contra"]
        self.assertEqual(len(contra), 1)
        names = {e.findtext("LEDGERNAME") for e in contra[0].findall("ALLLEDGERENTRIES.LIST")}
        self.assertEqual(names, {"SBI - 3333", "HDFC - 6666"})
        other = [v for v in vouchers if v.get("VCHTYPE") != "Contra"][0]
        self.assertEqual(other.get("VCHTYPE"), "Payment")

    def test_edge05_mirror_dedup_without_utr_by_amount_and_date(self) -> None:
        s1 = mk_stmt("ST1", acct="111122223333", bank="SBI")
        s2 = mk_stmt("ST2", acct="444455556666", bank="HDFC")
        a = mk_tx(0, "2025-04-10", "IMPS TO OWN", dr=7000, bal=1, cat=49, sid="ST1", cp_acc="444455556666")
        b = mk_tx(0, "2025-04-10", "IMPS FROM OWN", cr=7000, bal=1, cat=49, sid="ST2")
        _, stats = generate_tally_xml("Co", [s1, s2], [a, b])
        self.assertEqual(stats["vouchers_exported"], 1)

    def test_edge06_fd_is_payment_receipt_not_contra(self) -> None:
        st = mk_stmt()
        txns = [
            mk_tx(0, "2025-04-02", "FD CREATION", dr=100000, bal=1, cat=33),
            mk_tx(1, "2025-05-02", "FD MATURITY", cr=101000, bal=1, cat=34),
        ]
        root = ET.fromstring(generate_tally_xml("Co", [st], txns)[0])
        types = [v.get("VCHTYPE") for v in _vouchers(root)]
        self.assertEqual(types, ["Payment", "Receipt"])
        self.assertEqual(_ledgers(root)[CATEGORY_BY_ID[33].name], "Deposits (Asset)")

    def test_edge07_guid_is_content_hash_and_stable_across_reingest(self) -> None:
        st = mk_stmt()
        a = mk_tx(0, "2025-04-02", "UPI/DR/VENDOR", dr=100, bal=900, cat=11, cp="VENDOR", utr="U1", tx_id="uuid-a")
        b = mk_tx(0, "2025-04-02", "UPI/DR/VENDOR", dr=100, bal=900, cat=11, cp="VENDOR", utr="U1", tx_id="uuid-b")
        g1 = ET.fromstring(generate_tally_xml("Co", [st], [a])[0]).find(".//VOUCHER/GUID").text
        g2 = ET.fromstring(generate_tally_xml("Co", [st], [b])[0]).find(".//VOUCHER/GUID").text
        self.assertEqual(g1, g2)
        c = mk_tx(0, "2025-04-02", "UPI/DR/VENDOR", dr=101, bal=900, cat=11, cp="VENDOR", utr="U1")
        g3 = ET.fromstring(generate_tally_xml("Co", [st], [c])[0]).find(".//VOUCHER/GUID").text
        self.assertNotEqual(g1, g3)

    def test_edge08_voucher_numbers_unique_across_statements(self) -> None:
        s1 = mk_stmt("ST1", acct="111122223333")
        s2 = mk_stmt("ST2", acct="444455556666", bank="HDFC")
        txns = [mk_tx(i, "2025-04-0%d" % (i + 1), "V", dr=10, bal=1, cat=11, sid=sid, cp="V")
                for sid in ("ST1", "ST2") for i in range(3)]
        root = ET.fromstring(generate_tally_xml("Co", [s1, s2], txns)[0])
        nums = [v.findtext("VOUCHERNUMBER") for v in _vouchers(root)]
        guids = [v.findtext("GUID") for v in _vouchers(root)]
        self.assertEqual(len(nums), 6)
        self.assertEqual(len(set(nums)), 6)
        self.assertEqual(len(set(guids)), 6)

    def test_edge09_voucher_type_by_direction_and_trade_option(self) -> None:
        st = mk_stmt()
        txns = [
            mk_tx(0, "2025-04-02", "REFUND FROM VENDOR", cr=500, bal=1, cat=11, cp="V1"),  # CR in a DR category
            mk_tx(1, "2025-04-03", "SALES", cr=1000, bal=1, cat=2, cp="C1"),
            mk_tx(2, "2025-04-04", "PURCHASE", dr=700, bal=1, cat=12, cp="S1"),
            mk_tx(3, "2025-04-05", "CASH DEP", cr=900, bal=1, cat=50),
        ]
        root = ET.fromstring(generate_tally_xml("Co", [st], txns)[0])
        self.assertEqual([v.get("VCHTYPE") for v in _vouchers(root)], ["Receipt", "Receipt", "Payment", "Contra"])
        root2 = ET.fromstring(generate_tally_xml("Co", [st], txns, use_trade_vouchers=True)[0])
        self.assertEqual([v.get("VCHTYPE") for v in _vouchers(root2)], ["Receipt", "Sales", "Purchase", "Contra"])
        for v in _vouchers(root2):
            amts = [Decimal(e.findtext("AMOUNT")) for e in v.findall("ALLLEDGERENTRIES.LIST")]
            self.assertEqual(sum(amts), Decimal("0"))

    def test_edge11_company_name_optional(self) -> None:
        st = mk_stmt()
        t = mk_tx(0, "2025-04-02", "X", dr=1, bal=1, cat=11, cp="X")
        root = ET.fromstring(generate_tally_xml(None, [st], [t])[0])
        self.assertIsNone(root.find(".//SVCURRENTCOMPANY"))
        root = ET.fromstring(generate_tally_xml("Case Title", [st], [t], company_name="Balaji Enterprises")[0])
        self.assertEqual(root.findtext(".//SVCURRENTCOMPANY"), "Balaji Enterprises")


class _TallyStub(BaseHTTPRequestHandler):
    body = b""

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args) -> None:  # silence
        pass


class TallyPushTests(unittest.TestCase):
    def _serve(self, body: bytes):
        handler = type("H", (_TallyStub,), {"body": body})
        srv = HTTPServer(("127.0.0.1", 0), handler)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def test_edge12_parses_created_altered_errors_lineerror(self) -> None:
        body = (b"<RESPONSE><CREATED>3</CREATED><ALTERED>2</ALTERED><DELETED>0</DELETED>"
                b"<LASTVCHID>9</LASTVCHID><IGNORED>1</IGNORED><ERRORS>1</ERRORS><CANCELLED>0</CANCELLED>"
                b"<EXCEPTIONS>0</EXCEPTIONS><LINEERROR>Ledger 'X' does not exist!</LINEERROR></RESPONSE>")
        port = self._serve(body)
        res = push_to_tally_http("<ENVELOPE/>", port=port, timeout=3)
        self.assertTrue(res["connected"])
        self.assertEqual((res["created"], res["altered"], res["errors"], res["ignored"]), (3, 2, 1, 1))
        self.assertEqual(res["line_errors"], ["Ledger 'X' does not exist!"])
        self.assertFalse(res["success"])

    def test_edge12_import_result_envelope_and_non_xml(self) -> None:
        port = self._serve(b"<ENVELOPE><BODY><DATA><IMPORTRESULT><CREATED>4</CREATED><ERRORS>0</ERRORS>"
                           b"</IMPORTRESULT></DATA></BODY></ENVELOPE>")
        res = push_to_tally_http("<ENVELOPE/>", port=port, timeout=3)
        self.assertEqual(res["created"], 4)
        self.assertTrue(res["success"])
        port2 = self._serve(b"Unknown Request, cannot be processed\x00")
        res2 = push_to_tally_http("<ENVELOPE/>", port=port2, timeout=3)
        self.assertTrue(res2["connected"])
        self.assertFalse(res2["success"])

    def test_edge12_network_errors_do_not_escape(self) -> None:
        # Closed port -> connection refused
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        res = push_to_tally_http("<ENVELOPE/>", port=port, timeout=1)
        self.assertFalse(res["connected"])
        # Listener that accepts but never answers -> timeout (socket.timeout / TimeoutError)
        lst = socket.socket()
        lst.bind(("127.0.0.1", 0))
        lst.listen(1)
        self.addCleanup(lst.close)
        res = push_to_tally_http("<ENVELOPE/>", port=lst.getsockname()[1], timeout=0.5)
        self.assertFalse(res["connected"])
        self.assertIn("message", res)


class ItrComplianceTests(unittest.TestCase):
    def test_edge14_fy_filter_and_available_fys(self) -> None:
        txns = [
            mk_tx(0, "2024-05-01", "SALARY", cr=100000, cat=1),
            mk_tx(1, "2025-05-01", "SALARY", cr=200000, cat=1),
            mk_tx(2, "2025-06-01", "SALARY", cr=200000, cat=1),
        ]
        rep = generate_itr_compliance_report(txns)
        self.assertEqual(rep["financial_year"], "FY 2025-26")
        self.assertEqual(rep["available_fys"], ["FY 2024-25", "FY 2025-26"])
        self.assertEqual(rep["income_heads"]["salary_sec_15"], 400000)
        self.assertEqual(rep["transaction_count"], 2)
        rep = generate_itr_compliance_report(txns, fy="2024-25")
        self.assertEqual(rep["financial_year"], "FY 2024-25")
        self.assertEqual(rep["income_heads"]["salary_sec_15"], 100000)
        rep = generate_itr_compliance_report(txns, target_fy="FY 2024-25")
        self.assertEqual(rep["transaction_count"], 1)
        with self.assertRaises(ValueError):
            generate_itr_compliance_report(txns, fy="FY 2019-20")

    def _tds(self, rep, section):
        return [f for f in rep["statutory_red_flags"] if f["section"].startswith(section)]

    def test_edge15_tds_per_payee_and_fy_dated_thresholds(self) -> None:
        # FY 2025-26: 194J threshold is 50,000 per payee; two payees of 40k each -> no flag.
        txns = [
            mk_tx(0, "2025-05-01", "IMPS/LEGAL A", dr=40000, cat=18, cp="ADV A"),
            mk_tx(1, "2025-06-01", "IMPS/LEGAL B", dr=40000, cat=18, cp="ADV B"),
        ]
        self.assertEqual(self._tds(generate_itr_compliance_report(txns), "Section 194J"), [])
        # Same payments in FY 2024-25 -> 30,000 threshold, both payees flagged.
        old = [
            mk_tx(0, "2024-05-01", "IMPS/LEGAL A", dr=40000, cat=18, cp="ADV A"),
            mk_tx(1, "2024-06-01", "IMPS/LEGAL B", dr=40000, cat=18, cp="ADV B"),
        ]
        f = self._tds(generate_itr_compliance_report(old), "Section 194J")
        self.assertEqual(len(f), 1)
        self.assertEqual({p["payee"] for p in f[0]["payees"]}, {"ADV A", "ADV B"})
        # 194J aggregate per payee crosses 50k in FY25-26
        agg = [mk_tx(i, f"2025-0{5 + i}-01", "LEGAL", dr=20000, cat=18, cp="ADV A") for i in range(3)]
        f = self._tds(generate_itr_compliance_report(agg), "Section 194J")
        self.assertEqual(f[0]["payees"][0]["amount"], 60000)

    def test_edge15_194c_single_and_aggregate(self) -> None:
        single = [mk_tx(0, "2025-05-01", "VENDOR", dr=35000, cat=11, cp="V1")]
        f = self._tds(generate_itr_compliance_report(single), "Section 194C")
        self.assertEqual(len(f), 1)
        small = [mk_tx(i, "2025-05-%02d" % (i + 1), "VENDOR", dr=25000, cat=11, cp="V1") for i in range(4)]
        self.assertEqual(self._tds(generate_itr_compliance_report(small), "Section 194C"), [])  # 1,00,000 not > 1L
        small.append(mk_tx(9, "2025-06-01", "VENDOR", dr=1000, cat=11, cp="V1"))
        self.assertEqual(len(self._tds(generate_itr_compliance_report(small), "Section 194C")), 1)
        # Same total spread across distinct payees -> no flag
        spread = [mk_tx(i, "2025-05-01", "VENDOR", dr=25000, cat=11, cp=f"V{i}") for i in range(5)]
        self.assertEqual(self._tds(generate_itr_compliance_report(spread), "Section 194C"), [])

    def test_edge15_194i_monthly_from_fy2526_and_194h(self) -> None:
        # FY 2025-26: 45k/month rent -> below 50k/month -> no flag (old 2.4L annual rule would fire)
        rent = [mk_tx(i, f"2025-{m:02d}-05", "RENT", dr=45000, cat=13, cp="LANDLORD") for i, m in enumerate(range(4, 10))]
        self.assertEqual(self._tds(generate_itr_compliance_report(rent), "Section 194I"), [])
        rent.append(mk_tx(20, "2025-10-05", "RENT", dr=60000, cat=13, cp="LANDLORD"))
        self.assertEqual(len(self._tds(generate_itr_compliance_report(rent), "Section 194I")), 1)
        # FY 2024-25: annual > 2.4L per payee
        old = [mk_tx(i, f"2024-{m:02d}-05", "RENT", dr=45000, cat=13, cp="LANDLORD") for i, m in enumerate(range(4, 10))]
        self.assertEqual(len(self._tds(generate_itr_compliance_report(old), "Section 194I")), 1)
        comm = [mk_tx(0, "2025-05-01", "NEFT/DR/COMMISSION PAID AGENT", dr=18000, cat=11, cp="AGENT")]
        self.assertEqual(self._tds(generate_itr_compliance_report(comm), "Section 194H"), [])  # 20k from FY25-26
        comm_old = [mk_tx(0, "2024-05-01", "NEFT/DR/COMMISSION PAID AGENT", dr=18000, cat=11, cp="AGENT")]
        self.assertEqual(len(self._tds(generate_itr_compliance_report(comm_old), "Section 194H")), 1)

    def _sec(self, rep, section):
        return [f for f in rep["statutory_red_flags"] if f["section"] == section]

    def test_edge16_40a3_uses_cash_payments_to_a_party(self) -> None:
        # ATM withdrawals are not payments to a party
        atm = [mk_tx(0, "2025-05-01", "ATM WDL", dr=20000, cat=51, channel="ATM")]
        self.assertEqual(self._sec(generate_itr_compliance_report(atm), "Section 40A(3)"), [])
        paid = [
            mk_tx(0, "2025-05-01", "CHQ PAID CASH TO RAMESH", dr=6000, cat=51, cp="RAMESH", channel="CASH"),
            mk_tx(1, "2025-05-01", "CHQ PAID CASH TO RAMESH", dr=6000, cat=51, cp="RAMESH", channel="CASH"),
            mk_tx(2, "2025-05-01", "CHQ PAID CASH TO SURESH", dr=9000, cat=51, cp="SURESH", channel="CASH"),
        ]
        f = self._sec(generate_itr_compliance_report(paid), "Section 40A(3)")
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["amount"], 12000)

    def test_edge16_269st_per_person_per_day(self) -> None:
        txns = [
            mk_tx(0, "2025-05-01", "CASH DEP BY A", cr=150000, cat=50, cp="A"),
            mk_tx(1, "2025-05-01", "CASH DEP BY B", cr=150000, cat=50, cp="B"),
        ]
        self.assertEqual(self._sec(generate_itr_compliance_report(txns), "Section 269ST"), [])
        txns.append(mk_tx(2, "2025-05-01", "CASH DEP BY A", cr=50000, cat=50, cp="A"))
        f = self._sec(generate_itr_compliance_report(txns), "Section 269ST")
        self.assertEqual(f[0]["amount"], 200000)

    def test_edge17_sft_account_type_and_inclusive_threshold(self) -> None:
        sav = mk_stmt("SAV", account_type="Savings")
        cur = mk_stmt("CUR", acct="999988887777", account_type="Current")
        dep = [mk_tx(i, "2025-05-%02d" % (i + 1), "CASH DEP", cr=100000, cat=50, sid="SAV") for i in range(10)]
        rep = generate_itr_compliance_report(dep, statements=[sav, cur])
        self.assertEqual(len(self._sec(rep, "SFT-003 / Rule 114E")), 1)  # exactly 10L -> reportable (>=)
        cur_dep = [mk_tx(i, "2025-05-%02d" % (i + 1), "CASH DEP", cr=100000, cat=50, sid="CUR") for i in range(10)]
        rep = generate_itr_compliance_report(cur_dep, statements=[sav, cur])
        self.assertEqual(self._sec(rep, "SFT-003 / Rule 114E"), [])  # current a/c threshold is 50L
        big = [mk_tx(i, "2025-05-%02d" % (i % 28 + 1), "CASH DEP", cr=500000, cat=50, sid="CUR") for i in range(10)]
        rep = generate_itr_compliance_report(big, statements=[sav, cur])
        self.assertEqual(len(self._sec(rep, "SFT-003 / Rule 114E")), 1)


class LoanEligibilityTests(unittest.TestCase):
    def test_edge21_abb_sums_accounts(self) -> None:
        s1 = mk_stmt("A", acct="111", start="2025-04-01", end="2025-04-30", opening="10000", closing="10000")
        s2 = mk_stmt("B", acct="222", start="2025-04-01", end="2025-04-30", opening="500000", closing="500000")
        txns = [
            mk_tx(0, "2025-04-10", "INT", cr=0.01, bal=10000.01, cat=4, sid="A"),
            mk_tx(0, "2025-04-12", "INT", cr=0.01, bal=500000.01, cat=4, sid="B"),
        ]
        rep = compute_loan_eligibility_report(txns, statements=[s1, s2])
        self.assertAlmostEqual(rep["daily_abb"], 510000, delta=1)
        self.assertEqual(len(rep["per_account_abb"]), 2)
        per = {a["account"]: a["daily_abb"] for a in rep["per_account_abb"]}
        self.assertAlmostEqual(per["SBI - 111"], 10000, delta=1)
        # Without statements, accounts are still separated by statement_id
        rep2 = compute_loan_eligibility_report(txns)
        self.assertGreater(rep2["daily_abb"], 500000)

    def test_edge21_fixed_date_abb(self) -> None:
        st = mk_stmt(start="2025-04-01", end="2025-04-30", opening="0")
        txns = [
            mk_tx(0, "2025-04-05", "C", cr=1000, bal=1000, cat=2),
            mk_tx(1, "2025-04-12", "C", cr=1000, bal=2000, cat=2),
            mk_tx(2, "2025-04-26", "C", cr=1000, bal=3000, cat=2),
        ]
        rep = compute_loan_eligibility_report(txns, statements=[st])
        # 5:1000 10:1000 15:2000 20:2000 25:2000 30:3000 -> 11000/6
        self.assertAlmostEqual(rep["fixed_date_abb"], 11000 / 6, places=1)

    def test_edge22_empty_months_included(self) -> None:
        st = mk_stmt(start="2025-04-01", end="2025-06-30", opening="1000")
        txns = [
            mk_tx(0, "2025-04-10", "C", cr=1000, bal=2000, cat=2),
            mk_tx(1, "2025-06-10", "C", cr=1000, bal=3000, cat=2),
        ]
        rep = compute_loan_eligibility_report(txns, statements=[st])
        months = [m["month"] for m in rep["monthly_breakdown"]]
        self.assertEqual(months, ["2025-04", "2025-05", "2025-06"])
        may = rep["monthly_breakdown"][1]
        self.assertEqual(may["tx_count"], 0)
        self.assertEqual(may["daily_abb"], 2000)
        self.assertEqual(rep["months_analyzed"], 3)
        self.assertAlmostEqual(rep["avg_monthly_inflow"], 2000 / 3, places=1)

    def test_edge23_emi_recurrence_detection(self) -> None:
        st = mk_stmt(start="2025-04-01", end="2025-06-30", opening="1000000")
        txns = [
            mk_tx(0, "2025-04-05", "NACH/DR/HDFC HOME LOAN EMI", dr=42000, bal=1, cat=23),
            mk_tx(1, "2025-05-05", "NACH/DR/HDFC HOME LOAN EMI", dr=42000, bal=1, cat=23),
            mk_tx(2, "2025-06-06", "NACH/DR/HDFC HOME LOAN EMI", dr=42500, bal=1, cat=23),
            # one-off loan-category debit (foreclosure / part-payment) must not count as EMI
            mk_tx(3, "2025-05-20", "NEFT/DR/HDFC LOAN PART PAYMENT", dr=300000, bal=1, cat=23),
            # ECS EMI to Bajaj (not categorised as loan) recurring
            mk_tx(4, "2025-04-03", "ECS/BAJAJ FINANCE LTD/12345", dr=5000, bal=1, cat=52),
            mk_tx(5, "2025-05-03", "ECS/BAJAJ FINANCE LTD/12399", dr=5000, bal=1, cat=52),
            mk_tx(6, "2025-06-03", "ECS/BAJAJ FINANCE LTD/12401", dr=5000, bal=1, cat=52),
            # SIP via ACH is an investment, not an EMI
            mk_tx(7, "2025-04-14", "ACH/DR/CAMS MUTUAL FUND SIP", dr=25000, bal=1, cat=31),
            mk_tx(8, "2025-05-14", "ACH/DR/CAMS MUTUAL FUND SIP", dr=25000, bal=1, cat=31),
        ]
        rep = compute_loan_eligibility_report(txns, statements=[st])
        lenders = {o["lender"]: o for o in rep["emi_obligations"]}
        self.assertEqual(len(lenders), 2, lenders.keys())
        self.assertAlmostEqual(rep["monthly_existing_emi"], 42000 + 5000, delta=600)
        self.assertTrue(all(o["active"] for o in rep["emi_obligations"]))

    def test_edge24_bounce_logic(self) -> None:
        st = mk_stmt(start="2025-04-01", end="2025-05-31", opening="100000")
        txns = [
            mk_tx(0, "2025-04-05", "NACH/DR/HDFC EMI", dr=42000, bal=1, cat=23),
            mk_tx(1, "2025-04-05", "NACH RTN/HDFC EMI/INSUFFICIENT FUNDS", cr=42000, bal=1, cat=52),
            mk_tx(2, "2025-04-06", "NACH RETURN CHARGES", dr=590, bal=1, cat=28),  # paired -> not a 2nd bounce
            mk_tx(3, "2025-04-20", "INCOME TAX RETURN REFUND", cr=12000, bal=1, cat=7),  # not a bounce
            mk_tx(4, "2025-04-22", "IMPS RETURN REFUND FLIPKART", cr=999, bal=1, cat=48),  # no instrument
            mk_tx(5, "2025-05-05", "NACH/DR/HDFC EMI", dr=42000, bal=1, cat=23),
            mk_tx(6, "2025-05-07", "CHQ 000123 PAID", dr=10000, bal=1, cat=11),
            mk_tx(7, "2025-05-10", "O/W CHQ RET 556677 FUNDS INSUFF", dr=25000, bal=1, cat=52),
            mk_tx(8, "2025-05-11", "O/W CHQ RETURN CHGS", dr=150, bal=1, cat=28),
        ]
        rep = compute_loan_eligibility_report(txns, statements=[st])
        self.assertEqual(rep["inward_bounce_count"], 1)
        self.assertEqual(rep["outward_bounce_count"], 1)
        # attempted debit instruments: NACH x2 + CHQ x1 = 3
        self.assertEqual(rep["attempted_debit_instruments"], 3)
        self.assertAlmostEqual(rep["bounce_rate_pct"], 100 / 3, places=1)

    def test_edge24_unpaired_charge_counts_once(self) -> None:
        st = mk_stmt(start="2025-05-01", end="2025-05-31", opening="1000")
        txns = [
            mk_tx(0, "2025-05-05", "NACH/DR/HDFC HOME LOAN EMI", dr=500, bal=500, cat=23),
            mk_tx(1, "2025-05-06", "NACH RETURN CHG/INSUFFICIENT BAL", dr=59, bal=441, cat=28),
        ]
        rep = compute_loan_eligibility_report(txns, statements=[st])
        self.assertEqual(rep["inward_bounce_count"], 1)
        self.assertEqual(rep["outward_bounce_count"], 0)


if __name__ == "__main__":
    unittest.main()
