"""P1 tests for the NLP group: roadmap item 23 (NLP-03/14/15/17/18/19/20/22/23/24/25) and the labelled
macro-F1 benchmark (NFR-REL-01, NLP-26)."""

from __future__ import annotations

import os
import sys
import unittest
from collections import Counter
from decimal import Decimal
from typing import List, Tuple

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.models import NormalizedTransaction, TxDirection  # noqa: E402
from cyphersol.nlp import categorizer as cat_mod  # noqa: E402
from cyphersol.nlp.categorizer import (  # noqa: E402
    HybridTransactionCategorizer,
    detect_recurring_mandates,
    tabular_feature_tokens,
    tabular_features,
)
from cyphersol.nlp.narration_parser import (  # noqa: E402
    SRS_CHANNEL_EXTENSIONS,
    SRS_PAYMENT_CHANNELS,
    detect_channel,
    enrich_transaction_narration,
    extract_cheque_number,
    extract_counterparty_account,
    internal_payment_channel,
    is_known_psp_handle,
    parse_narration,
    srs_payment_channel,
)
from cyphersol.taxonomy import (  # noqa: E402
    CATEGORIES_52,
    CATEGORY_BY_ID,
    KNOWLEDGE_BASE_PATTERNS,
    KNOWLEDGE_BASE_POINT_COUNT,
    _BASE_CATEGORY_PATTERNS,
    category_allows_direction,
    curated_knowledge_base_count,
    knowledge_base_stats,
)

FIXTURES = os.path.join(ROOT_DIR, "tests", "fixtures", "nlp")
DEV_CORPUS = os.path.join(FIXTURES, "labelled_narrations.tsv")
HOLDOUT_CORPUS = os.path.join(FIXTURES, "labelled_narrations_holdout.tsv")

# Floors sit a few points under the measured values so that real regressions fail; the measured numbers are
# printed by the benchmark test. SRS NFR-REL-01 target is 0.92 macro-F1.
DEV_MACRO_F1_FLOOR = 0.95
HOLDOUT_MACRO_F1_FLOOR = 0.93
SRS_MACRO_F1_TARGET = 0.92


def _tx(
    narration: str,
    direction: str = "DR",
    amount: str = "100",
    date: str = "2025-04-01",
    tx_id: str = "T1",
    case_id: str = "C1",
    enrich: bool = True,
) -> NormalizedTransaction:
    amt = Decimal(amount)
    tx = NormalizedTransaction(
        tx_id=tx_id,
        statement_id="S1",
        case_id=case_id,
        row_index=0,
        txn_date=date,
        raw_narration=narration,
        payment_channel="OTHER",
        direction=TxDirection(direction),
        debit=amt if direction == "DR" else Decimal("0"),
        credit=amt if direction == "CR" else Decimal("0"),
        running_balance=Decimal("0"),
        category_id=52,
        category_name="",
        categorization_source="",
        confidence_score=0.0,
    )
    return enrich_transaction_narration(tx) if enrich else tx


def load_corpus(path: str) -> List[Tuple[int, str, Decimal, str, str]]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip() or line.startswith("#"):
                continue
            cid, d, amount, date, narration = line.rstrip("\n").split("\t")
            rows.append((int(cid), d, Decimal(amount), date, narration))
    return rows


def corpus_transactions(rows, case_id: str = "BENCH") -> List[NormalizedTransaction]:
    return [
        _tx(n, d, str(a), dt, tx_id=f"{case_id}-{i}", case_id=case_id) for i, (_c, d, a, dt, n) in enumerate(rows)
    ]


def macro_f1(gold: List[int], pred: List[int]) -> Tuple[float, float, dict]:
    tp, fp, fn = Counter(), Counter(), Counter()
    for g, p in zip(gold, pred):
        if g == p:
            tp[g] += 1
        else:
            fp[p] += 1
            fn[g] += 1
    per = {}
    for label in sorted(set(gold)):
        prec = tp[label] / (tp[label] + fp[label]) if tp[label] + fp[label] else 0.0
        rec = tp[label] / (tp[label] + fn[label]) if tp[label] + fn[label] else 0.0
        per[label] = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    acc = sum(tp.values()) / len(gold) if gold else 0.0
    return sum(per.values()) / len(per), acc, per


def evaluate(path: str, categorizer: HybridTransactionCategorizer = None):
    rows = load_corpus(path)
    txs = corpus_transactions(rows)
    (categorizer or HybridTransactionCategorizer()).classify_batch(txs)
    return rows, txs, macro_f1([r[0] for r in rows], [t.category_id for t in txs])


# ---------------------------------------------------------------------------------------------------------
# NLP-17: party-name cleaning
# ---------------------------------------------------------------------------------------------------------


class TestNlp17PartyCleaning(unittest.TestCase):
    CASES = [
        ("UPI/DR/UTR 412345678901/FROM ACCT RAHUL/rahul@ybl", "RAHUL"),
        ("NEFT UTR NO HDFCN24015123456 FROM SURESH", "SURESH"),
        ("IMPS 1234567890123456 PAYMENT FROM KIRAN", "KIRAN"),
        ("BY TRANSFER-INB/IFT/123456789/AYAZ QURESHI A/C 30112233445", "AYAZ QURESHI"),
        ("TRF FROM 30112233445 MANISH PATIL", "MANISH PATIL"),
        ("CHQ PAID-MICR CTS-MUMBAI 400002-RAJESH", "RAJESH"),
        ("BY CLG PUNE 411001 ANIL", "ANIL"),
        ("NEFT/HDFC0001234/RAHUL SHARMA/HDFCN24015123456", "RAHUL SHARMA"),
        ("ACH D- TP ACH HDFCBANK-1234567890", "HDFCBANK"),
    ]

    def test_tag_words_and_identifiers_never_in_party(self) -> None:
        for narr, expected in self.CASES:
            with self.subTest(narr=narr):
                self.assertEqual(parse_narration(narr).counterparty_name, expected)

    def test_party_has_no_tag_words(self) -> None:
        bad = {"UTR", "RRN", "FROM", "ACCT", "A/C", "PAYMENT FROM"}
        for narr, _ in self.CASES:
            party = parse_narration(narr).counterparty_name or ""
            self.assertFalse(any(party.startswith(b + " ") or party == b for b in bad), (narr, party))


# ---------------------------------------------------------------------------------------------------------
# NLP-18: counterparty account numbers; card number is not a counterparty
# ---------------------------------------------------------------------------------------------------------


class TestNlp18Accounts(unittest.TestCase):
    def test_full_account_numbers_extracted(self) -> None:
        cases = [
            ("IMPS/P2A/412345678901/RAMESH KUMAR/50100293847112/HDFC0000240", "50100293847112"),
            ("NEFT-UTIBN24015123456-KAVITA RAO-UTIB0000123-918010044221", "918010044221"),
            ("TRF FROM 30112233445 MANISH PATIL", "30112233445"),
            ("BY TRANSFER-INB/IFT/123456789/AYAZ QURESHI A/C 30112233445", "30112233445"),
            ("NEFT TO BENEFICIARY A/C NO 001234567890 SHARMA", "001234567890"),
        ]
        for narr, acc in cases:
            with self.subTest(narr=narr):
                p = parse_narration(narr)
                self.assertEqual(p.counterparty_account, acc)
                self.assertEqual(p.counterparty_vpa_or_acc, acc)

    def test_masked_accounts_still_extracted(self) -> None:
        p = parse_narration("IMPS/P2A/412345678901/SITA DEVI/XXXXXXXX7788/SBIN0001111")
        self.assertEqual(p.counterparty_vpa_or_acc, "XXXXXXXX7788")
        self.assertEqual(parse_narration("IMPS-412345678901-RAJ TRADERS-HDFC-XX1234-NA").counterparty_vpa_or_acc, "XX1234")

    def test_utr_or_rrn_never_taken_as_account(self) -> None:
        p = parse_narration("IMPS/P2A/412345678901/RAMESH KUMAR/HDFC0000240")
        self.assertEqual(p.utr_number, "412345678901")
        self.assertIsNone(p.counterparty_account)
        p = parse_narration("NEFT 1234567890123456 KIRAN PATEL")
        self.assertIsNone(p.counterparty_account)

    def test_card_number_is_not_counterparty(self) -> None:
        for narr in ("POS 432198XXXXXX1234 BIG BAZAAR MUMBAI", "POS 432198XXXXXX1234 AWS SERVICES"):
            with self.subTest(narr=narr):
                p = parse_narration(narr)
                self.assertEqual(p.card_number_masked, "432198XXXXXX1234")
                self.assertIsNone(p.counterparty_vpa_or_acc)
                tx = _tx(narr)
                self.assertIsNone(tx.counterparty_vpa_or_acc)

    def test_function_contract(self) -> None:
        self.assertEqual(extract_counterparty_account("A/C NO: 12345678901", "TRANSFER"), "12345678901")
        self.assertIsNone(extract_counterparty_account("4321XXXXXXXX1234", "POS"))
        self.assertIsNone(extract_counterparty_account("", None))


# ---------------------------------------------------------------------------------------------------------
# NLP-19: UTR grammars
# ---------------------------------------------------------------------------------------------------------


class TestNlp19Utr(unittest.TestCase):
    CASES = [
        ("NEFT/HDFC0001234/RAHUL SHARMA/HDFCN24015123456", "HDFCN24015123456"),  # 16-char NEFT
        ("RTGS/SBIN0001234/ACME STEELS PVT LTD/SBINR52024011512345678", "SBINR52024011512345678"),  # 22-char RTGS
        ("RTGS UTR CNRBR12024011500012345 KISHORE", "CNRBR12024011500012345"),
        ("NEFT 1234567890123456 KIRAN PATEL", "1234567890123456"),  # 16-digit numeric
        ("IMPS 1234567890123456 PAYMENT FROM KIRAN", "1234567890123456"),
        ("UPI/CR/409110012345/AYAZ QURESHI/ayaz@okhdfcbank/HDFC0000240", "409110012345"),  # 12-digit RRN
        ("NEFT-XYZN24015123456-AKASH", "XYZN24015123456"),  # no prefix truncation
        ("NEFT CR-HDFC0000240-PRIYA SHARMA-NETBANK-HDFCH00412345678", "HDFCH00412345678"),
        ("BY TRANSFER-INB/IFT/123456789/AYAZ QURESHI A/C 30112233445", "123456789"),  # IFT reference
    ]

    def test_utrs(self) -> None:
        for narr, utr in self.CASES:
            with self.subTest(narr=narr):
                self.assertEqual(parse_narration(narr).utr_number, utr)

    def test_16_digit_card_number_is_not_utr(self) -> None:
        self.assertIsNone(parse_narration("POS 4321987654321234 BIG BAZAAR").utr_number)


# ---------------------------------------------------------------------------------------------------------
# NLP-20: cheque numbers
# ---------------------------------------------------------------------------------------------------------


class TestNlp20Cheque(unittest.TestCase):
    def test_cheque_numbers(self) -> None:
        cases = [
            ("CLG/123456/HDFC BANK/RAHUL", "123456"),
            ("CTS CLG CHQ NO 000789 PAID TO SUNIL TRADERS", "000789"),
            ("TO CLG 000457 KAVERI ELECTRICALS", "000457"),
            ("CHQ DEP 0000123456 RAHUL", "123456"),
            ("CASH WDL SELF CHQ 000123", "000123"),
        ]
        for narr, chq in cases:
            with self.subTest(narr=narr):
                self.assertEqual(parse_narration(narr).cheque_number, chq)

    def test_pin_codes_and_other_6_digit_numbers_are_not_cheques(self) -> None:
        for narr in (
            "CHQ PAID-MICR CTS-MUMBAI 400002-RAJESH",
            "BY CLG PUNE 411001 ANIL",
            "NEFT/SBIN0001234/ORDER 123456 RAHUL/SBINN24015123456",
            "MICR 400002003 CLEARING",
            "UPI/DR/409110012345/SWIGGY/swiggy@icici/ICIC0000123",
        ):
            with self.subTest(narr=narr):
                p = parse_narration(narr)
                self.assertIsNone(p.cheque_number)
                if p.payment_channel == "CHEQUE":
                    self.assertNotIn(p.utr_number or "", ("400002", "411001"))

    def test_untagged_slot_only_on_cheque_rows(self) -> None:
        self.assertIsNone(extract_cheque_number("NEFT/123456/RAHUL", "NEFT"))
        self.assertEqual(extract_cheque_number("CLG/123456/RAHUL", "CHEQUE"), "123456")


# ---------------------------------------------------------------------------------------------------------
# NLP-23: PSP whitelist
# ---------------------------------------------------------------------------------------------------------


class TestNlp23PspWhitelist(unittest.TestCase):
    def test_known_handle_is_vpa_without_upi_keyword(self) -> None:
        p = parse_narration("PAID TO rahul.sharma@okaxis FOR DINNER")
        self.assertEqual(p.counterparty_vpa_or_acc, "rahul.sharma@okaxis")
        self.assertEqual(p.payment_channel, "UPI")

    def test_unknown_handle_needs_upi_context(self) -> None:
        p = parse_narration("CONTACT support@acme REF 1234")
        self.assertNotEqual(p.payment_channel, "UPI")
        self.assertIsNone(p.counterparty_vpa_or_acc)
        p = parse_narration("UPI/DR/409110012345/ACME/acme@newpsp/ICIC0000123")
        self.assertEqual(p.counterparty_vpa_or_acc, "acme@newpsp")

    def test_email_is_never_vpa(self) -> None:
        p = parse_narration("UPI/DR/409110012345/RAHUL/rahul@gmail.com/ICIC0000123")
        self.assertNotEqual(p.counterparty_vpa_or_acc, "rahul@gmail")

    def test_handle_shapes(self) -> None:
        for h in ("ybl", "okaxis", "OKICICI", "paytm", "okfoobank", "newbank"):
            self.assertTrue(is_known_psp_handle(h), h)
        for h in ("acme", "", None, "example"):
            self.assertFalse(is_known_psp_handle(h), h)


# ---------------------------------------------------------------------------------------------------------
# NLP-24: SRS payment-channel enum
# ---------------------------------------------------------------------------------------------------------


class TestNlp24SrsChannels(unittest.TestCase):
    def test_enum_matches_srs(self) -> None:
        self.assertEqual(
            SRS_PAYMENT_CHANNELS,
            ("UPI", "NEFT", "RTGS", "IMPS", "NACH", "ATM", "AEPS", "POS", "CHEQUE",
             "INTERNAL_TRANSFER", "BANK_CHARGE", "INTEREST", "CASH_CDM"),
        )

    def test_mapping(self) -> None:
        self.assertEqual(srs_payment_channel("TRANSFER"), "INTERNAL_TRANSFER")
        self.assertEqual(srs_payment_channel("CHARGES"), "BANK_CHARGE")
        self.assertEqual(srs_payment_channel("ecs"), "NACH")
        self.assertEqual(srs_payment_channel(None), "OTHER")
        for ch in SRS_PAYMENT_CHANNELS:
            self.assertEqual(srs_payment_channel(ch), ch)  # idempotent
        self.assertEqual(internal_payment_channel("INTERNAL_TRANSFER"), "TRANSFER")
        self.assertEqual(internal_payment_channel("BANK_CHARGE"), "CHARGES")

    def test_every_parsed_row_has_srs_channel(self) -> None:
        allowed = set(SRS_PAYMENT_CHANNELS) | set(SRS_CHANNEL_EXTENSIONS)
        for _c, _d, _a, _dt, narr in load_corpus(DEV_CORPUS):
            p = parse_narration(narr)
            self.assertIn(p.srs_channel, allowed, narr)
            self.assertEqual(p.srs_channel, srs_payment_channel(p.payment_channel))

    def test_categorizer_accepts_srs_channel_names(self) -> None:
        cat = HybridTransactionCategorizer()
        tx = _tx("MB/TPT/SANJAY VERMA/12345678901")
        tx.payment_channel = "INTERNAL_TRANSFER"
        cat.classify_transaction(tx)
        self.assertEqual(tx.category_id, 52)
        tx = _tx("ATW/516012345678/HDFC ATM ANDHERI")
        self.assertEqual(cat.classify_transaction(tx).category_id, 51)

    def test_generic_charges_word_is_not_bank_charge_channel(self) -> None:
        self.assertNotEqual(detect_channel("BLUE DART EXPRESS COURIER CHARGES"), "CHARGES")
        self.assertNotEqual(detect_channel("MCA21 ROC FILING FEE"), "CHARGES")
        self.assertEqual(detect_channel("SMS CHG FOR QTR"), "CHARGES")
        self.assertEqual(detect_channel("ATM TXN CHARGES OTHER BANK"), "CHARGES")
        self.assertEqual(detect_channel("DEBIT CARD ANNUAL FEE"), "CHARGES")


# ---------------------------------------------------------------------------------------------------------
# NLP-25: lifestyle flags; NLP-03: honest KB
# ---------------------------------------------------------------------------------------------------------


class TestNlp25Lifestyle(unittest.TestCase):
    def test_utilities_and_telecom_are_not_lifestyle(self) -> None:
        self.assertFalse(CATEGORY_BY_ID[15].is_lifestyle)
        self.assertFalse(CATEGORY_BY_ID[16].is_lifestyle)
        self.assertEqual({c.category_id for c in CATEGORIES_52 if c.is_lifestyle}, set(range(43, 49)))


class TestNlp03HonestKb(unittest.TestCase):
    def test_count_is_curated_terms_only(self) -> None:
        self.assertEqual(KNOWLEDGE_BASE_POINT_COUNT, curated_knowledge_base_count())
        literal = {" ".join(t.strip().lower().split()) for terms in _BASE_CATEGORY_PATTERNS.values() for t in terms}
        for cid, pats in KNOWLEDGE_BASE_PATTERNS.items():
            for p in pats:
                self.assertIn(p, literal, (cid, p))  # nothing generated
        stats = knowledge_base_stats()
        self.assertEqual(stats["curated_terms"], KNOWLEDGE_BASE_POINT_COUNT)
        self.assertLess(KNOWLEDGE_BASE_POINT_COUNT, 7500)  # the honest number, not the marketing one
        self.assertEqual(stats["categories_covered"], 51)


# ---------------------------------------------------------------------------------------------------------
# NLP-14: learned rule ledger and specificity; NLP-22: top-3
# ---------------------------------------------------------------------------------------------------------


class TestNlp14RuleLedger(unittest.TestCase):
    def test_custom_ledger_reapplied_and_most_specific_wins(self) -> None:
        cat = HybridTransactionCategorizer()
        cat.add_user_rule("party:sanjay verma", "BOTH", 11, "Sanjay Verma (Vendor)")
        cat.add_user_rule("vpa:sanjay.v@okaxis", "DR", 14, "Payroll - Sanjay")
        tx = cat.classify_transaction(_tx("UPI/DR/409110012345/SANJAY VERMA/sanjay.v@okaxis/UTIB0000123"))
        self.assertEqual((tx.category_id, tx.tally_ledger_name), (14, "Payroll - Sanjay"))
        tx = cat.classify_transaction(_tx("IMPS/P2A/409110012346/SANJAY VERMA/SBIN0001234", tx_id="T2"))
        self.assertEqual((tx.category_id, tx.tally_ledger_name), (11, "Sanjay Verma (Vendor)"))


class TestNlp22Top3(unittest.TestCase):
    def test_top3_direction_compatible_monotone_and_bounded(self) -> None:
        cat = HybridTransactionCategorizer()
        rows = load_corpus(DEV_CORPUS)
        for tx in cat.classify_batch(corpus_transactions(rows)):
            confs = [s["confidence"] for s in tx.top3_suggestions]
            with self.subTest(narr=tx.raw_narration):
                self.assertEqual(len(tx.top3_suggestions), 3)
                self.assertEqual(confs, sorted(confs, reverse=True))
                self.assertTrue(all(0.0 <= c <= 1.0 for c in confs))
                for s in tx.top3_suggestions:
                    self.assertTrue(category_allows_direction(s["category_id"], tx.direction.value))
                    self.assertNotEqual(s["category_id"], 52)


# ---------------------------------------------------------------------------------------------------------
# NLP-15: tabular features for the ML tier
# ---------------------------------------------------------------------------------------------------------


class TestNlp15TabularFeatures(unittest.TestCase):
    def test_feature_vector(self) -> None:
        tx = _tx("NACH DR BAJAJ FINANCE LTD UMRN BAJF0000123456", "DR", "5000", "2025-06-05")
        f = tabular_features(tx)
        self.assertEqual(f["direction_cr"], 0.0)
        self.assertAlmostEqual(f["log1p_amount"], 8.517393, places=5)
        self.assertEqual(f["is_round_number"], 1.0)
        self.assertEqual(f["day_of_month"], 5.0)
        self.assertEqual(f["is_recurring_mandate"], 1.0)
        self.assertEqual(f["channel_NACH"], 1.0)
        self.assertEqual(sum(v for k, v in f.items() if k.startswith("channel_")), 1.0)
        self.assertEqual(
            tabular_feature_tokens(tx), ["__ch_nach", "__amt_b3", "__amt_round", "__dom_early", "__mandate"]
        )
        f2 = tabular_features(_tx("UPI/CR/409110012345/RAHUL/rahul@ybl", "CR", "123.45", "2025-06-28"))
        self.assertEqual((f2["direction_cr"], f2["is_round_number"], f2["is_recurring_mandate"]), (1.0, 0.0, 0.0))
        self.assertEqual(f2["channel_UPI"], 1.0)

    def test_recurring_detection(self) -> None:
        txs = [
            _tx("UPI/DR/40911001234%d/GYM FIT/gymfit@ybl/YESB0000001" % i, "DR", "1500", f"2025-0{m}-03", tx_id=f"R{m}")
            for i, m in enumerate((4, 5, 6))
        ]
        txs.append(_tx("UPI/DR/409110012399/GYM FIT/gymfit@ybl/YESB0000001", "DR", "999", "2025-07-03", tx_id="R7"))
        self.assertEqual(detect_recurring_mandates(txs), {"R4", "R5", "R6"})
        self.assertIn("__mandate", tabular_feature_tokens(txs[0], recurring=True))

    def test_tabular_tokens_alone_never_decide(self) -> None:
        cat = HybridTransactionCategorizer()
        for narr in ("NACH DR PRIYA SHARMA", "UPI/DR/409110012345/PRIYA SHARMA/priya99@ybl/HDFC0000240"):
            tx = cat.classify_transaction(_tx(narr, "DR", "5000", "2025-06-05"))
            self.assertEqual(tx.category_id, 52, narr)
        nb = cat._nb
        self.assertEqual(nb.rank([], "DR", ["__ch_nach", "__mandate"]), [])

    def test_tabular_tokens_tilt_the_ml_tier(self) -> None:
        nb = cat_mod._NaiveBayes(KNOWLEDGE_BASE_PATTERNS, cat_mod.TABULAR_FEATURE_PRIORS)
        toks = ["finance"]
        plain = dict((c, p) for c, p, _ in nb.rank(toks, "DR"))
        tilted = dict((c, p) for c, p, _ in nb.rank(toks, "DR", ["__ch_nach", "__mandate"]))
        self.assertGreater(tilted[25], plain[25])

    def test_cache_respects_tabular_tokens(self) -> None:
        cat = HybridTransactionCategorizer()
        a = cat.classify_transaction(_tx("ACME FINANCE", "DR", "5000", "2025-06-05", tx_id="A"))
        b = cat.classify_transaction(_tx("ACME FINANCE", "DR", "5000", "2025-06-05", tx_id="B"))
        self.assertEqual((a.category_id, a.confidence_score), (b.category_id, b.confidence_score))
        fresh = HybridTransactionCategorizer()
        c = fresh.classify_transaction(_tx("ACME FINANCE", "DR", "37.10", "2025-06-15", tx_id="C"))
        d = cat.classify_transaction(_tx("ACME FINANCE", "DR", "37.10", "2025-06-15", tx_id="D"))
        self.assertEqual((c.category_id, c.confidence_score), (d.category_id, d.confidence_score))

    def test_fit_labelled_examples_learns_and_keeps_gates(self) -> None:
        cat = HybridTransactionCategorizer()
        novel = "QWIKFIT MONTHLY PLAN"
        before = cat.classify_transaction(_tx(novel, "DR", "1500", tx_id="N0"))
        self.assertEqual(before.category_id, 52)
        examples = [(_tx(f"{novel} {m}", "DR", "1500", tx_id=f"E{m}"), 17) for m in ("APR", "MAY", "JUN", "JUL")]
        examples.append((_tx("UPI/DR/409110012345/PRIYA SHARMA/priya99@ybl/HDFC0000240"), 52))  # skipped
        self.assertEqual(cat.fit_labelled_examples(examples), 4)
        after = cat.classify_transaction(_tx(novel, "DR", "1500", tx_id="N1"))
        self.assertEqual((after.category_id, after.categorization_source), (17, "ML_MODEL"))
        p2p = cat.classify_transaction(_tx("UPI/DR/409110012345/SHIPRA AWASTHI/shipra.awasthi@okaxis/UTIB0000123"))
        self.assertEqual(p2p.category_id, 52)

    def test_copy_has_own_memo(self) -> None:
        import copy

        cat = HybridTransactionCategorizer()
        clone = copy.copy(cat)
        self.assertIsNot(clone._cache, cat._cache)
        self.assertIsNot(clone._tab_sensitive, cat._tab_sensitive)
        self.assertIs(clone._kb, cat._kb)


# ---------------------------------------------------------------------------------------------------------
# Labelled benchmark (NFR-REL-01 / NLP-26)
# ---------------------------------------------------------------------------------------------------------


class TestNlpBenchmark(unittest.TestCase):
    def test_corpus_shape(self) -> None:
        dev = load_corpus(DEV_CORPUS)
        self.assertGreaterEqual(len(dev), 300)
        self.assertEqual({r[0] for r in dev}, set(range(1, 53)))
        for cid, d, amount, date, narr in dev + load_corpus(HOLDOUT_CORPUS):
            self.assertTrue(category_allows_direction(cid, d), (cid, d, narr))
            self.assertGreater(amount, 0)
            self.assertRegex(date, r"^\d{4}-\d{2}-\d{2}$")
        dev_narrs = {r[4] for r in dev}
        overlap = [r[4] for r in load_corpus(HOLDOUT_CORPUS) if r[4] in dev_narrs]
        self.assertEqual(overlap, [])

    def test_macro_f1(self) -> None:
        results = {}
        for name, path, floor in (("dev", DEV_CORPUS, DEV_MACRO_F1_FLOOR), ("holdout", HOLDOUT_CORPUS, HOLDOUT_MACRO_F1_FLOOR)):
            rows, txs, (f1, acc, _per) = evaluate(path)
            results[name] = (f1, acc, len(rows))
            self.assertGreaterEqual(f1, floor, name)
        print(
            "\n[NLP BENCH] macro-F1 dev=%.4f (acc %.4f, n=%d) holdout=%.4f (acc %.4f, n=%d) SRS target %.2f"
            % (*results["dev"], *results["holdout"], SRS_MACRO_F1_TARGET)
        )

    def test_no_p2p_row_confidently_categorised(self) -> None:
        for path in (DEV_CORPUS, HOLDOUT_CORPUS):
            rows, txs, _ = evaluate(path)
            for (cid, *_rest), tx in zip(rows, txs):
                if cid == 52:
                    self.assertEqual(tx.category_id, 52, tx.raw_narration)
                    self.assertLess(tx.confidence_score, 0.65)


if __name__ == "__main__":
    unittest.main()
