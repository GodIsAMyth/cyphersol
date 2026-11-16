"""P0 regression tests for the NLP group (NLP-01/02/03/04/14/17/19, EDGE-03/04/06, BE-06)."""

from __future__ import annotations

import os
import random
import sys
import time
import unittest
from decimal import Decimal

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.models import NormalizedTransaction, TxDirection, VoucherType
from cyphersol.nlp import categorizer as cat_mod
from cyphersol.nlp.categorizer import (
    HybridTransactionCategorizer,
    apply_suspense_resolution,
    derive_rule_key,
    preview_suspense_propagation,
)
from cyphersol.nlp.narration_parser import enrich_transaction_narration, parse_narration
from cyphersol.taxonomy import (
    CATEGORIES_52,
    CATEGORY_BY_ID,
    KNOWLEDGE_BASE_PATTERNS,
    KNOWLEDGE_BASE_POINT_COUNT,
    SUSPENSE_LEDGER_NAME,
    TALLY_PREDEFINED_GROUPS,
    _BASE_CATEGORY_PATTERNS,
    curated_knowledge_base_count,
    tally_voucher_for,
)

TAU = 0.65
SUSPENSE = "SUSPENSE"


def _tx(
    narration: str,
    direction: str,
    tx_id: str = "T1",
    case_id: str = "C1",
    holder: str | None = None,
    enrich: bool = True,
) -> NormalizedTransaction:
    tx = NormalizedTransaction(
        tx_id=tx_id,
        statement_id="S1",
        case_id=case_id,
        row_index=0,
        txn_date="2025-04-01",
        raw_narration=narration,
        payment_channel="OTHER",
        direction=TxDirection(direction),
        debit=Decimal("100") if direction == "DR" else Decimal("0"),
        credit=Decimal("100") if direction == "CR" else Decimal("0"),
        running_balance=Decimal("0"),
        category_id=52,
        category_name="",
        categorization_source="",
        confidence_score=0.0,
        source_holder_name=holder,
    )
    return enrich_transaction_narration(tx) if enrich else tx


# (narration, direction, expected category id or SUSPENSE)
CORPUS = [
    # --- P2P transfers to/from individuals: must never be confidently categorised ---
    ("UPI/DR/409110012345/SHIPRA AWASTHI/shipra.awasthi@okaxis/UTIB0000123", "DR", SUSPENSE),
    ("UPI/CR/409110012346/PRIYA SHARMA/priya99@ybl/HDFC0000240", "CR", SUSPENSE),
    ("UPI/DR/409110012347/BHOLA/bhola123@paytm/SBIN0001234", "DR", SUSPENSE),
    ("UPI/DR/409110012348/SIPRA/sipra@oksbi/SBIN0001234", "DR", SUSPENSE),
    ("UPI/DR/409110012349/LAWS/laws@ybl/ICIC0000123", "DR", SUSPENSE),
    ("UPI/CR/409110012350/KUBER/kuber@ibl/PUNB0001234", "CR", SUSPENSE),
    ("UPI/DR/409110012351/KUBER/kuber@ibl/PUNB0001234", "DR", SUSPENSE),
    ("UPI/CR/409110012352/SHIPRA AWASTHI/shipra.awasthi@okaxis/UTIB0000123", "CR", SUSPENSE),
    ("UPI/DR/409110012353/PRIYA SHARMA/priya99@ybl/HDFC0000240", "DR", SUSPENSE),
    ("IMPS/P2A/409110012354/RAMESH KUMAR/SBIN0001234", "DR", SUSPENSE),
    ("NEFT/HDFC0001234/ANITA DEVI/HDFCN52025012345678", "CR", SUSPENSE),
    ("UPI-SUNITA YADAV-9876543210@ybl-YESB0YBLUPI-409110012355-PAYMENT", "DR", SUSPENSE),
    ("UPI/CR/409110012356/DHAN SINGH/dhan.singh@okicici/ICIC0000123", "CR", SUSPENSE),
    ("UPI/DR/409110012357/DHAN SINGH/dhan.singh@okicici/ICIC0000123", "DR", SUSPENSE),
    ("UPI/DR/409110012358/MOHD ASLAM/9123456780@paytm/PYTM0123456", "DR", SUSPENSE),
    ("IMPS/P2A/409110012359/VIKAS GUPTA/HDFC0000001", "CR", SUSPENSE),
    ("UPI/DR/409110012360/PRIYA SHARMA/priya99@ybl/HDFC0000240/rent june", "DR", SUSPENSE),
    ("NEFT/SBIN0004321/KAVITA RANI/SBINN52025013345678", "DR", SUSPENSE),
    # --- Real merchants / institutions ---
    ("UPI/DR/409110012361/SWIGGY/swiggy@icici/ICIC0000123", "DR", 43),
    ("UPI/DR/409110012362/ZOMATO LTD/zomato@hdfcbank/HDFC0000123", "DR", 43),
    ("UPI/DR/409110012363/AIRTEL/airtelpayments@ybl/AIRP0000001", "DR", 16),
    ("POS 432198XXXXXX1234 AIRTEL PREPAID RECHARGE", "DR", 16),
    ("NACH/DR/LIC OF INDIA/POLICY 123456789", "DR", 35),
    ("ACH DR LIC PREMIUM 987654321", "DR", 35),
    ("UPI/DR/409110012364/ZERODHA BROKING/zerodha@hdfcbank/HDFC0000123", "DR", 32),
    ("NEFT/HDFC0000555/ZERODHA BROKING LTD/HDFCN52025017777777", "CR", 32),
    ("UPI/CR/409110012365/WAZIRX/wazirx@ybl/YESB0000001", "CR", 37),
    ("UPI/DR/409110012366/WAZIRX/wazirx@ybl/YESB0000001", "DR", 37),
    ("NACH DR BAJAJ FINANCE LTD UMRN BAJF0000123456", "DR", 25),
    ("ACH/DR/BAJAJ FINANCE NACH/EMI 0425", "DR", 25),
    ("ATM WDL 409110012367 SBI ATM MUMBAI", "DR", 51),
    ("ATW/409110012368/HDFC ATM ANDHERI", "DR", 51),
    ("CASH DEP BRANCH MUMBAI", "CR", 50),
    ("BY CASH DEPOSIT CDM 0042", "CR", 50),
    ("NEFT/ICIC0000001/INFOSYS LTD SALARY JAN/ICICN52025011234567", "CR", 1),
    ("SALARY CREDIT FOR MAR 2025 TCS LTD", "CR", 1),
    ("UPI/DR/409110012369/UBER INDIA/uber@axisbank/UTIB0000001", "DR", 45),
    ("UPI/DR/409110012370/OLA/olacabs@ybl/YESB0000001", "DR", 45),
    ("ACH/DR/SIP HDFC MF/123456", "DR", 31),
    ("POS 432198XXXXXX1234 AWS SERVICES", "DR", 17),
    ("SMS CHG FOR QTR", "DR", 27),
    ("UPI/DR/409110012371/AMAZON PAY/amazon@apl/UTIB0000001", "DR", 48),
]


class TestNlp01NoConfidentMiscategorisation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cat = HybridTransactionCategorizer(confidence_threshold=TAU)

    def test_corpus_expected_outcomes(self) -> None:
        self.assertGreaterEqual(len(CORPUS), 40)
        for narr, d, expected in CORPUS:
            with self.subTest(narration=narr, direction=d):
                tx = self.cat.classify_transaction(_tx(narr, d))
                if expected == SUSPENSE:
                    self.assertEqual(tx.category_id, 52, f"{narr} -> {tx.category_id} ({tx.categorization_source})")
                    self.assertTrue(tx.is_suspense)
                    self.assertLess(tx.confidence_score, TAU)
                    self.assertEqual(tx.tally_ledger_name, SUSPENSE_LEDGER_NAME)
                    self.assertEqual(len(tx.top3_suggestions), 3)
                else:
                    self.assertEqual(tx.category_id, expected, f"{narr} -> {tx.category_id} ({tx.categorization_source})")
                    self.assertGreaterEqual(tx.confidence_score, TAU)
                    self.assertFalse(tx.is_suspense)

    def test_direction_hard_filter_on_assignment_and_suggestions(self) -> None:
        for narr, d, _ in CORPUS:
            tx = self.cat.classify_transaction(_tx(narr, d))
            with self.subTest(narration=narr, direction=d):
                for cid in [tx.category_id] + [s["category_id"] for s in tx.top3_suggestions]:
                    spec_dir = CATEGORY_BY_ID[cid].typical_direction
                    self.assertIn(spec_dir, ("BOTH", d), f"category {cid} ({spec_dir}) offered for {d}")
                # Suggestions share one probability scale: monotone non-increasing, in [0,1]
                confs = [s["confidence"] for s in tx.top3_suggestions]
                self.assertTrue(all(0.0 <= c <= 1.0 for c in confs))
                self.assertEqual(len({s["category_id"] for s in tx.top3_suggestions}), len(confs))

    def test_ml_ignores_person_name_and_vpa_tokens(self) -> None:
        toks = cat_mod.ml_feature_tokens(_tx("UPI/DR/409110012345/SHIPRA AWASTHI/shipra.awasthi@okaxis/UTIB0000123", "DR"))
        joined = " ".join(toks)
        self.assertNotIn("shipra", joined)
        self.assertNotIn("awasthi", joined)
        self.assertNotIn("okaxis", joined)

    def test_ml_evidence_floor_blocks_single_weak_token(self) -> None:
        # Remark-only evidence ("rent") on a P2P row must not be auto-assigned
        tx = self.cat.classify_transaction(
            _tx("UPI/DR/409110012360/PRIYA SHARMA/priya99@ybl/HDFC0000240/rent june", "DR")
        )
        self.assertEqual(tx.category_id, 52)
        self.assertIn(13, [s["category_id"] for s in tx.top3_suggestions])

    def test_self_transfer_requires_evidence(self) -> None:
        tx = self.cat.classify_transaction(
            _tx("IMPS/P2A/409110099999/RAVI KUMAR VERMA/HDFC0000001", "DR", holder="Ravi Kumar Verma")
        )
        self.assertEqual(tx.category_id, 49)
        tx2 = self.cat.classify_transaction(
            _tx("IMPS/P2A/409110099999/RAVI KUMAR VERMA/HDFC0000001", "DR", holder="SUNIL MEHTA")
        )
        self.assertEqual(tx2.category_id, 52)
        tx3 = self.cat.classify_transaction(_tx("SELF TRANSFER TO OWN A/C 1234", "DR"))
        self.assertEqual(tx3.category_id, 49)


class TestNlp02TokenBoundaryKB(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.cat = HybridTransactionCategorizer(confidence_threshold=TAU)

    def test_short_patterns_do_not_hijack_names(self) -> None:
        bad = {"BHOLA": 45, "SIPRA": 31, "LAWS": 17, "KUBER": 45, "DHANRAJ": 32, "PROLAN": 45}
        for name, wrong in bad.items():
            with self.subTest(name=name):
                tx = self.cat.classify_transaction(
                    _tx(f"UPI/DR/409110012345/{name}/{name.lower()}@ybl/YESB0000001", "DR")
                )
                self.assertNotEqual(tx.category_id, wrong)

    def test_match_quality_drives_confidence(self) -> None:
        exact_long = self.cat.classify_transaction(_tx("NACH/DR/LIC OF INDIA/POLICY 123456789", "DR"))
        exact_short = self.cat.classify_transaction(_tx("UPI/DR/409110012369/UBER/uber@axisbank/UTIB0000001", "DR"))
        prefix = self.cat.classify_transaction(_tx("POS 432198XXXXXX1234 SWIGGYINSTAMART BLR", "DR"))
        self.assertEqual(prefix.category_id, 43)
        self.assertGreater(exact_long.confidence_score, exact_short.confidence_score)
        self.assertGreater(exact_short.confidence_score, prefix.confidence_score)
        # Infix of a longer word is not a match at all
        infix = self.cat.classify_transaction(_tx("POS 432198XXXXXX1234 MYSWIGGYSTORE", "DR"))
        self.assertNotEqual(infix.categorization_source, "RULE_KB")

    def test_direction_aligned_kb(self) -> None:
        cr = self.cat.classify_transaction(_tx("NEFT/HDFC0000001/AMAZON SELLER SERVICES/HDFCN52025011111111", "CR"))
        self.assertEqual(cr.category_id, 2)
        dr = self.cat.classify_transaction(_tx("POS 432198XXXXXX1234 AMAZON WEB SERVICES", "DR"))
        self.assertEqual(dr.category_id, 17)


class TestNlp03HonestKnowledgeBase(unittest.TestCase):
    def test_no_generated_padding(self) -> None:
        all_pats = {p for pats in KNOWLEDGE_BASE_PATTERNS.values() for p in pats}
        self.assertNotIn("upi/zomato", all_pats)
        self.assertNotIn("zomato@okicici", all_pats)
        self.assertNotIn("sbi sms chg", all_pats)
        self.assertFalse(any("@" in p for p in all_pats))

    def test_honest_count_and_full_curated_coverage(self) -> None:
        n = curated_knowledge_base_count()
        self.assertEqual(n, KNOWLEDGE_BASE_POINT_COUNT)
        self.assertEqual(n, sum(len(v) for v in KNOWLEDGE_BASE_PATTERNS.values()))
        self.assertLess(n, 2000)
        for cid, terms in _BASE_CATEGORY_PATTERNS.items():
            for t in terms:
                self.assertIn(t.strip().lower(), KNOWLEDGE_BASE_PATTERNS[cid])
        # Terms formerly reachable only through generated bank templates are curated now
        self.assertIn("nach rtn", KNOWLEDGE_BASE_PATTERNS[28])
        self.assertIn("atm fee", KNOWLEDGE_BASE_PATTERNS[27])
        self.assertIn("self trf", KNOWLEDGE_BASE_PATTERNS[49])

    def test_generated_variants_still_matched_by_boundaries(self) -> None:
        cat = HybridTransactionCategorizer()
        for narr, d, exp in [
            ("SBI SMS CHG QTR", "DR", 27),
            ("PNB NACH RTN CHG", "DR", 28),
            ("UPI/DR/1234567890/ZOMATO/zomato@okicici/ICIC0000001", "DR", 43),
            ("HDFC SELF TRF 123", "DR", 49),
        ]:
            with self.subTest(narr=narr):
                self.assertEqual(cat.classify_transaction(_tx(narr, d)).category_id, exp)


class TestEdgeTallyGroups(unittest.TestCase):
    def test_parent_groups_are_tally_predefined(self) -> None:
        self.assertEqual(len(CATEGORIES_52), 52)
        for spec in CATEGORIES_52:
            with self.subTest(category=spec.category_id):
                self.assertIn(spec.tally_parent_group, TALLY_PREDEFINED_GROUPS)

    def test_suspense_ledger_does_not_clash_with_group(self) -> None:
        self.assertNotIn(SUSPENSE_LEDGER_NAME, TALLY_PREDEFINED_GROUPS)
        self.assertNotEqual(SUSPENSE_LEDGER_NAME.lower(), "suspense a/c")

    def test_fd_creation_and_maturity_not_contra(self) -> None:
        self.assertNotEqual(CATEGORY_BY_ID[33].tally_voucher, VoucherType.CONTRA)
        self.assertNotEqual(CATEGORY_BY_ID[34].tally_voucher, VoucherType.CONTRA)
        self.assertEqual(tally_voucher_for(33, "DR"), VoucherType.PAYMENT)
        self.assertEqual(tally_voucher_for(34, "CR"), VoucherType.RECEIPT)
        self.assertEqual(tally_voucher_for(52, "CR"), VoucherType.RECEIPT)
        self.assertEqual(tally_voucher_for(52, "DR"), VoucherType.PAYMENT)
        self.assertEqual(tally_voucher_for(51, "DR"), VoucherType.CONTRA)


class TestNlp04LearnedRules(unittest.TestCase):
    def _suspense(self, cat: HybridTransactionCategorizer, narr: str, d: str, tx_id: str, case_id: str = "C1"):
        return cat.classify_transaction(_tx(narr, d, tx_id=tx_id, case_id=case_id))

    def test_typed_keys(self) -> None:
        self.assertEqual(
            derive_rule_key(_tx("UPI/DR/409110012345/SHIPRA AWASTHI/shipra.awasthi@okaxis/UTIB0000123", "DR")),
            "vpa:shipra.awasthi@okaxis",
        )
        self.assertEqual(derive_rule_key(_tx("IMPS/P2A/409110012354/RAMESH KUMAR/SBIN0001234", "DR")), "party:ramesh kumar")
        short = _tx("IMPS/P2A/409110012354/RAM/SBIN0001234", "DR")
        self.assertIsNone(derive_rule_key(short))

    def test_party_rule_uses_word_boundaries_no_narration_fallback(self) -> None:
        cat = HybridTransactionCategorizer()
        cat.add_user_rule("party:ramesh", "DR", 18, "Ramesh Legal")
        hit = cat.classify_transaction(_tx("IMPS/P2A/409110012354/RAMESH KUMAR/SBIN0001234", "DR"))
        self.assertEqual(hit.category_id, 18)
        self.assertEqual(hit.categorization_source, "USER_RULE")
        self.assertEqual(hit.tally_ledger_name, "Ramesh Legal")
        for other in ("RAMESHWAR SINGH", "PROGRAM FEES", "TELEGRAM PREMIUM"):
            with self.subTest(other=other):
                miss = cat.classify_transaction(_tx(f"IMPS/P2A/409110012354/{other}/SBIN0001234", "DR"))
                self.assertNotEqual(miss.categorization_source, "USER_RULE")
        # Raw-narration fallback is gone: the term only in remarks does not trigger
        miss2 = cat.classify_transaction(_tx("UPI/DR/409110012360/SUNIL/sunil@ybl/HDFC0000240/paid ramesh", "DR"))
        self.assertNotEqual(miss2.categorization_source, "USER_RULE")
        # Legacy short rule ("ram") is ignored entirely
        cat.load_user_rules([("ram", "DR", 18, None)])
        miss3 = cat.classify_transaction(_tx("IMPS/P2A/409110012354/RAM PRASAD/SBIN0001234", "DR"))
        self.assertNotEqual(miss3.categorization_source, "USER_RULE")

    def test_most_specific_rule_wins_and_returns_ledger(self) -> None:
        cat = HybridTransactionCategorizer()
        cat.load_user_rules(
            [
                ("party:ramesh", "DR", 11, "Generic Vendor"),
                ("party:ramesh kumar", "DR", 18, "RK Legal"),
                ("vpa:rk@ybl", "DR", 13, "RK Rent"),
            ]
        )
        party = cat.classify_transaction(_tx("IMPS/P2A/409110012354/RAMESH KUMAR/SBIN0001234", "DR"))
        self.assertEqual((party.category_id, party.tally_ledger_name), (18, "RK Legal"))
        vpa = cat.classify_transaction(_tx("UPI/DR/409110012345/RAMESH KUMAR/rk@ybl/YESB0000001", "DR"))
        self.assertEqual((vpa.category_id, vpa.tally_ledger_name), (13, "RK Rent"))

    def test_case_scoped_rules(self) -> None:
        cat = HybridTransactionCategorizer()
        cat.load_user_rules([("party:ramesh kumar", "DR", 18, None, "CASE_A")])
        a = cat.classify_transaction(_tx("IMPS/P2A/1/RAMESH KUMAR/SBIN0001234", "DR", case_id="CASE_A"))
        b = cat.classify_transaction(_tx("IMPS/P2A/1/RAMESH KUMAR/SBIN0001234", "DR", case_id="CASE_B"))
        self.assertEqual(a.category_id, 18)
        self.assertNotEqual(b.categorization_source, "USER_RULE")
        # Case rule beats a global rule of equal specificity
        cat.add_user_rule("party:ramesh kumar", "DR", 11, None)
        a2 = cat.classify_transaction(_tx("IMPS/P2A/1/RAMESH KUMAR/SBIN0001234", "DR", case_id="CASE_A"))
        b2 = cat.classify_transaction(_tx("IMPS/P2A/1/RAMESH KUMAR/SBIN0001234", "DR", case_id="CASE_B"))
        self.assertEqual((a2.category_id, b2.category_id), (18, 11))

    def test_propagation_only_suspense_rows_with_preview(self) -> None:
        cat = HybridTransactionCategorizer()
        txns = [
            self._suspense(cat, "IMPS/P2A/1/RAMESH KUMAR/SBIN0001234", "DR", "A"),
            self._suspense(cat, "IMPS/P2A/2/RAMESH KUMAR/SBIN0001234", "DR", "B"),
            self._suspense(cat, "IMPS/P2A/3/RAMESH KUMAR/SBIN0001234", "DR", "C"),
            self._suspense(cat, "IMPS/P2A/4/RAMESHWAR/SBIN0001234", "DR", "D"),
            self._suspense(cat, "IMPS/P2A/5/RAMESH KUMAR/SBIN0001234", "CR", "E"),
            self._suspense(cat, "IMPS/P2A/6/RAMESH KUMAR/SBIN0001234", "DR", "F"),
        ]
        self.assertTrue(all(t.is_suspense for t in txns))
        # Rows C and F were categorised earlier (manually / by KB): never overwritten by propagation
        txns[2].category_id, txns[2].is_suspense, txns[2].categorization_source = 14, False, "USER_RULE"
        txns[5].category_id, txns[5].is_suspense, txns[5].categorization_source = 43, False, "RULE_KB"

        key, preview = preview_suspense_propagation(txns, "A")
        self.assertEqual(key, "party:ramesh kumar")
        self.assertEqual(preview, ["B"])

        before = {t.tx_id: t.category_id for t in txns}
        cnt, learned = apply_suspense_resolution(
            txns, "A", 18, custom_ledger_name="RK Legal", categorizer=cat, confirmed_tx_ids=[]
        )
        self.assertEqual(cnt, 1)  # only the anchor
        self.assertEqual(learned, "party:ramesh kumar")
        self.assertEqual(next(t for t in txns if t.tx_id == "B").category_id, before["B"])

        cnt2, _ = apply_suspense_resolution(txns, "B", 18, categorizer=cat)
        self.assertEqual(cnt2, 1)
        by_id = {t.tx_id: t for t in txns}
        self.assertEqual(by_id["C"].category_id, 14)
        self.assertEqual(by_id["D"].category_id, 52)
        self.assertEqual(by_id["E"].category_id, 52)
        self.assertEqual(by_id["F"].category_id, 43)
        self.assertEqual(by_id["A"].tally_ledger_name, "RK Legal")


class TestNarrationParserExtraction(unittest.TestCase):
    def test_no_prefix_truncation_of_party(self) -> None:
        p = parse_narration("NEFT/ICIC0000001/INFOSYS LTD/ICICN52025011234567")
        self.assertEqual(p.counterparty_name, "INFOSYS LTD")
        p2 = parse_narration("NEFT/ICIC0000001/OUTLOOK TRADERS/ICICN52025011234567")
        self.assertEqual(p2.counterparty_name, "OUTLOOK TRADERS")

    def test_tag_words_stripped_from_party(self) -> None:
        p = parse_narration("NEFT/HDFC0000001/UTR N123456789012345 FROM RAHUL VERMA")
        self.assertEqual(p.counterparty_name, "RAHUL VERMA")
        self.assertEqual(p.utr_number, "N123456789012345")
        p2 = parse_narration("IMPS/P2A/409110012354/TO RAMESH KUMAR/SBIN0001234")
        self.assertEqual(p2.counterparty_name, "RAMESH KUMAR")
        p3 = parse_narration("NEFT/SBIN0001234/BY SHARMA ENTERPRISES/SBINN52025013345678")
        self.assertEqual(p3.counterparty_name, "SHARMA ENTERPRISES")

    def test_sixteen_digit_utr(self) -> None:
        p = parse_narration("NEFT/1234567890123456/RAHUL VERMA")
        self.assertEqual(p.utr_number, "1234567890123456")
        p2 = parse_narration("RTGS UTR NO 9876543210987654 SHARMA ENTERPRISES")
        self.assertEqual(p2.utr_number, "9876543210987654")
        self.assertEqual(p2.counterparty_name, "SHARMA ENTERPRISES")

    def test_tagged_utr_not_truncated_or_taken_from_words(self) -> None:
        # A 24-char token is not a UTR; it must not be silently cut to 22
        p = parse_narration("NEFT UTR ABCD123456789012345678901 RAHUL")
        self.assertNotEqual(p.utr_number, "ABCD123456789012345678")
        # 'RRN' inside a word is not a tag
        p2 = parse_narration("NEFT/HDFC0000001/CORRNET SYSTEMS 1234567890/HDFCN52025011111111")
        self.assertEqual(p2.utr_number, "HDFCN52025011111111")


class TestPerformance(unittest.TestCase):
    def test_100k_categorisation_benchmark(self) -> None:
        rng = random.Random(7)
        merchants = [
            ("SWIGGY", "swiggy@icici", "DR"), ("ZOMATO LTD", "zomato@hdfcbank", "DR"),
            ("AIRTEL", "airtel@ybl", "DR"), ("ZERODHA BROKING", "zerodha@hdfcbank", "DR"),
            ("WAZIRX", "wazirx@ybl", "CR"), ("BIGBASKET", "bigbasket@icici", "DR"),
            ("UBER INDIA", "uber@axisbank", "DR"), ("IRCTC", "irctc@sbi", "DR"),
        ]
        first = ["PRIYA", "SHIPRA", "RAHUL", "AMIT", "SUNITA", "MOHD", "KAVITA", "VIKAS", "ANITA", "RAVI"]
        last = ["SHARMA", "AWASTHI", "KUMAR", "VERMA", "YADAV", "GUPTA", "SINGH", "DEVI", "MEHTA", "KHAN"]
        txns = []
        for i in range(100_000):
            r = rng.random()
            rrn = f"{409000000000 + i}"
            if r < 0.45:
                name = f"{rng.choice(first)} {rng.choice(last)}"
                vpa = f"{name.split()[0].lower()}{rng.randint(1, 999)}@ybl"
                d = rng.choice(["DR", "CR"])
                narr = f"UPI/{d}/{rrn}/{name}/{vpa}/YESB0000001"
                ch = "UPI"
            elif r < 0.85:
                name, vpa, d = rng.choice(merchants)
                narr = f"UPI/{d}/{rrn}/{name}/{vpa}/ICIC0000123"
                ch = "UPI"
            elif r < 0.95:
                name, vpa, d = "BAJAJ FINANCE LTD", None, "DR"
                narr = f"NACH DR BAJAJ FINANCE LTD UMRN BAJF{i:010d}"
                ch = "NACH"
            else:
                name, vpa, d = "CASH WITHDRAWAL (ATM)", None, "DR"
                narr = f"ATM WDL {rrn} SBI ATM MUMBAI"
                ch = "ATM"
            tx = _tx(narr, d, tx_id=f"T{i}", enrich=False)
            tx.payment_channel, tx.extracted_counterparty, tx.counterparty_vpa_or_acc = ch, name, vpa
            txns.append(tx)

        cat = HybridTransactionCategorizer()
        t0 = time.perf_counter()
        cat.classify_batch(txns)
        elapsed = time.perf_counter() - t0
        print(f"\n[BENCH] categorised 100,000 synthetic narrations in {elapsed:.2f}s")
        self.assertLess(elapsed, 15.0)
        misfiled = [t for t in txns if t.extracted_counterparty and t.payment_channel == "UPI"
                    and t.counterparty_vpa_or_acc and t.counterparty_vpa_or_acc.endswith("@ybl")
                    and t.extracted_counterparty.split()[0] in first and not t.is_suspense]
        self.assertEqual(misfiled, [])


if __name__ == "__main__":
    unittest.main()
