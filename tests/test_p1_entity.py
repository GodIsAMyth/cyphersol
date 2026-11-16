"""P1 regression tests for entity resolution (NLP-06/07/08/09/11/12/13 and NLP-05 blocking)."""

from __future__ import annotations

import dataclasses
import os
import random
import sys
import time
import unittest
from decimal import Decimal
from typing import List, Optional

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.models import BankAccountStatement, NormalizedTransaction, TxDirection  # noqa: E402
from cyphersol.nlp import entity_resolver as er  # noqa: E402
from cyphersol.nlp.entity_resolver import (  # noqa: E402
    build_alias_map,
    compute_name_similarity,
    extract_pans,
    indian_phonetic_key,
    merge_entities,
    normalize_entity_name,
    preview_clusters,
    reject_merge,
    resolve_case_entities,
    split_entity,
    undo_decision,
)

CASE = "CASE_P1_ENTITY"


def _stmt(holder: str, acc: str) -> BankAccountStatement:
    return BankAccountStatement(
        statement_id=f"ST_{acc}", case_id=CASE, source_filename=f"{holder}.csv", sha256_hash=acc,
        bank_code="HDFC", bank_name="HDFC", account_number=acc, account_holder_name=holder,
        period_start="2026-01-01", period_end="2026-03-31", opening_balance=Decimal("0"),
        closing_balance=Decimal("0"), total_transactions=0,
    )


_N = [0]


def _tx(stmt: BankAccountStatement, cp: Optional[str], ident: Optional[str] = None, narr: str = "",
        amt: int = 1000) -> NormalizedTransaction:
    _N[0] += 1
    return NormalizedTransaction(
        tx_id=f"T{_N[0]}", statement_id=stmt.statement_id, case_id=CASE, row_index=_N[0], txn_date="2026-01-02",
        raw_narration=narr or f"NEFT {cp}", payment_channel="NEFT", direction=TxDirection.DEBIT,
        debit=Decimal(amt), credit=Decimal("0"), running_balance=Decimal("0"), category_id=1,
        category_name="Transfer", categorization_source="RULE", confidence_score=0.9,
        extracted_counterparty=cp, counterparty_vpa_or_acc=ident,
    )


def _ent_of(ents, name):
    name = name.upper()
    for e in ents:
        if name in e.aliases:
            return e
    raise AssertionError(f"{name} not found")


class TestNormalisation(unittest.TestCase):
    """NLP-06 / NLP-07."""

    def test_ms_and_messrs_are_stripped(self) -> None:
        for raw in ("M/S POOJAN VIG", "M/S. POOJAN VIG", "M / S POOJAN VIG", "MESSRS POOJAN VIG", "Messrs. Poojan Vig"):
            self.assertEqual(normalize_entity_name(raw), "POOJAN VIG", raw)

    def test_banking_stopwords_stripped(self) -> None:
        self.assertEqual(normalize_entity_name("TO TRF M/S SHARMA STEELS"), "SHARMA STEELS")
        self.assertEqual(normalize_entity_name("BY NEFT RAHUL MEHTA"), "RAHUL MEHTA")
        self.assertEqual(normalize_entity_name("RAHUL MEHTA 501002938471"), "RAHUL MEHTA")

    def test_vpa_handle_becomes_name(self) -> None:
        self.assertEqual(normalize_entity_name("POOJANVIG@OKHDFC"), "POOJANVIG")

    def test_srs_example_aliases_merge(self) -> None:
        names = ["M/S POOJAN VIG", "POOJAN VIG", "Pujan Vij", "POOJANVIG@OKHDFC"]
        for a in names:
            for b in names:
                self.assertGreaterEqual(compute_name_similarity(a, b), 0.85, (a, b))

    def test_only_trailing_legal_suffix_stripped(self) -> None:
        self.assertEqual(normalize_entity_name("ABC TRADERS PVT LTD"), "ABC TRADERS")
        self.assertEqual(normalize_entity_name("ABC (P) LTD"), "ABC")
        self.assertEqual(normalize_entity_name("XYZ INDIA PRIVATE LIMITED"), "XYZ")
        # Interior "COMPANY" / business words are part of the name
        self.assertEqual(normalize_entity_name("EAST INDIA COMPANY STORES"), "EAST INDIA COMPANY STORES")
        self.assertEqual(normalize_entity_name("LTD"), "LTD")
        self.assertEqual(normalize_entity_name("SURESH P"), "SURESH P")
        self.assertEqual(normalize_entity_name("RAMESH KUMAR HUF"), "RAMESH KUMAR HUF")

    def test_business_type_mismatch_does_not_merge(self) -> None:
        self.assertLess(compute_name_similarity("ABC TRADERS", "ABC ENTERPRISES"), 0.85)
        self.assertLess(compute_name_similarity("SHARMA AGENCIES", "SHARMA SERVICES"), 0.85)
        self.assertGreaterEqual(compute_name_similarity("ABC TRADERS PVT LTD", "ABC TRADERS LIMITED"), 0.99)
        self.assertGreaterEqual(compute_name_similarity("ABC TRADERS", "ABC TRADING CO"), 0.85)


class TestPhonetics(unittest.TestCase):
    """NLP-08."""

    def test_initial_j_g_not_merged(self) -> None:
        self.assertNotEqual(indian_phonetic_key("JAYA"), indian_phonetic_key("GAYA"))
        self.assertLess(compute_name_similarity("JAYA SHAH", "GAYA SHAH"), 0.85)

    def test_indic_variants(self) -> None:
        pairs = [
            ("ZAHEER KHAN", "JAHEER KHAN"), ("LAXMI DEVI", "LAKSHMI DEVI"), ("VIKAS RAO", "WIKAS RAO"),
            ("MOHAMMED ASIF", "MOHD ASIF"), ("SHREE RAM", "SRI RAM"), ("PUJAN VIJ", "POOJAN VIG"),
            ("KRISHNA BHATT", "KRISHNA BATT"), ("RAJESH KR SINGH", "RAJESH KUMAR SINGH"),
        ]
        for a, b in pairs:
            self.assertGreaterEqual(compute_name_similarity(a, b), 0.85, (a, b))

    def test_distinct_names_stay_apart(self) -> None:
        for a, b in [("RAMESH KUMAR", "SURESH KUMAR"), ("AMIT SHAH", "AMITA SHAH TRADERS"), ("RAJ MEHTA", "RAJA MEHTA SERVICES")]:
            self.assertLess(compute_name_similarity(a, b), 0.85, (a, b))


class TestIdentifierLinking(unittest.TestCase):
    """NLP-09."""

    def test_full_account_links_counterparty_to_holder(self) -> None:
        a = _stmt("POOJAN VIG", "501002938471")
        b = _stmt("DEEP PATANGE", "000401992831")
        txns = [_tx(b, "PV ENTERPRISE", "501002938471"), _tx(b, "XYZ SUPPLIERS", "XXXXXX8471")]
        ents, _ = resolve_case_entities(CASE, [a, b], txns)
        self.assertIs(_ent_of(ents, "PV ENTERPRISE"), _ent_of(ents, "POOJAN VIG"))
        # Masked accounts never link
        self.assertIsNot(_ent_of(ents, "XYZ SUPPLIERS"), _ent_of(ents, "POOJAN VIG"))

    def test_shared_counterparty_account_links_aliases(self) -> None:
        a = _stmt("DEEP PATANGE", "000401992831")
        txns = [_tx(a, "KOLAR TEX", "1234567890123"), _tx(a, "SRI VENKATESHWARA FABRICS", "1234567890123")]
        ents, _ = resolve_case_entities(CASE, [a], txns)
        self.assertIs(_ent_of(ents, "KOLAR TEX"), _ent_of(ents, "SRI VENKATESHWARA FABRICS"))

    def test_pan_links_aliases(self) -> None:
        self.assertEqual(extract_pans("GST PMT PAN ABCPV1234K REF 99"), ["ABCPV1234K"])
        self.assertEqual(extract_pans("NO PAN HERE ABCDE12345"), [])
        a = _stmt("DEEP PATANGE", "000401992831")
        txns = [_tx(a, "ANIL TRADING", narr="NEFT ANIL TRADING PAN AAACA1234B"),
                _tx(a, "BHARAT IMPEX", narr="RTGS BHARAT IMPEX PAN AAACA1234B")]
        diag: dict = {}
        ents, _ = resolve_case_entities(CASE, [a], txns, diagnostics=diag)
        self.assertIs(_ent_of(ents, "ANIL TRADING"), _ent_of(ents, "BHARAT IMPEX"))
        self.assertTrue(any(ev["reason"] == "PAN" for ev in diag["evidence"]))

    def test_pan_seen_with_many_names_is_ambiguous(self) -> None:
        a = _stmt("DEEP PATANGE", "000401992831")
        txns = [_tx(a, n, narr=f"TDS {n} PAN AAAPD1234B") for n in ("ALPHA LTD", "BETA CORP", "GAMMA INFRA")]
        diag: dict = {}
        ents, _ = resolve_case_entities(CASE, [a], txns, diagnostics=diag)
        self.assertEqual(len({_ent_of(ents, n).entity_id for n in ("ALPHA LTD", "BETA CORP", "GAMMA INFRA")}), 3)
        self.assertTrue(any(c["type"] == "PAN_AMBIGUOUS" for c in diag["conflicts"]))


class TestHolderGuard(unittest.TestCase):
    """NLP-13."""

    def test_two_statement_holders_never_fuzzy_merge(self) -> None:
        a = _stmt("RAMESH KUMAR", "440011998877")
        b = _stmt("RAMESH KUMAAR", "440011998800")
        ents, _ = resolve_case_entities(CASE, [a, b], [_tx(a, "RAMESH KUMAR"), _tx(b, "RAMESH KUMAAR")], 0.5)
        self.assertIsNot(_ent_of(ents, "RAMESH KUMAR"), _ent_of(ents, "RAMESH KUMAAR"))

    def test_counterparty_bridge_cannot_glue_holders(self) -> None:
        a = _stmt("ALPHA SINGH", "111122223333")
        b = _stmt("BRAVO SINGH", "444455556666")
        c = _stmt("CHARLIE", "777788889999")
        # One counterparty name claims both holders' full account numbers
        txns = [_tx(c, "SINGH", "111122223333"), _tx(c, "SINGH", "444455556666")]
        diag: dict = {}
        ents, _ = resolve_case_entities(CASE, [a, b, c], txns, diagnostics=diag)
        self.assertIsNot(_ent_of(ents, "ALPHA SINGH"), _ent_of(ents, "BRAVO SINGH"))
        self.assertTrue(any(x["type"] == "HOLDER_GUARD" for x in diag["conflicts"]))

    def test_same_account_two_statements_merge(self) -> None:
        a = _stmt("POOJAN VIG", "501002938471")
        b = _stmt("MR. POOJAN VIG", "501002938471")
        ents, _ = resolve_case_entities(CASE, [a, b], [])
        self.assertIs(_ent_of(ents, "POOJAN VIG"), _ent_of(ents, "MR. POOJAN VIG"))

    def test_manual_merge_can_join_holders(self) -> None:
        a = _stmt("ALPHA", "111122223333")
        b = _stmt("BRAVO", "444455556666")
        ents, _ = resolve_case_entities(CASE, [a, b], [], manual_merges={"BRAVO": "ALPHA"})
        self.assertIs(_ent_of(ents, "ALPHA"), _ent_of(ents, "BRAVO"))


class TestStableIds(unittest.TestCase):
    """NLP-12."""

    def _data(self):
        a = _stmt("DEEP PATANGE", "000401992831")
        names = ["POOJAN VIG", "PUJAN VIJ", "POOJAN VIGG", "KOLAR TEXTILES", "IMRAN SABLE"]
        return [a], [_tx(a, n) for n in names]

    def test_ids_deterministic_and_order_independent(self) -> None:
        st, tx = self._data()
        e1, _ = resolve_case_entities(CASE, st, tx)
        e2, _ = resolve_case_entities(CASE, st, list(reversed(tx)))
        self.assertEqual({e.entity_id: sorted(e.aliases) for e in e1}, {e.entity_id: sorted(e.aliases) for e in e2})

    def test_merge_keeps_one_constituent_id_and_split_restores(self) -> None:
        st, tx = self._data()
        strict, _ = resolve_case_entities(CASE, st, tx, similarity_threshold=1.0)
        strict_ids = {e.entity_id for e in strict}
        loose, _ = resolve_case_entities(CASE, st, tx, similarity_threshold=0.85, previous_entities=strict)
        merged = _ent_of(loose, "POOJAN VIG")
        self.assertGreater(len(merged.aliases), 1)
        self.assertIn(merged.entity_id, strict_ids)
        back, _ = resolve_case_entities(CASE, st, tx, similarity_threshold=1.0, previous_entities=loose)
        self.assertEqual({e.entity_id for e in back}, strict_ids)

    def test_previous_ids_preserved_via_alias_map(self) -> None:
        st, tx = self._data()
        ents, _ = resolve_case_entities(CASE, st, tx)
        legacy = [dataclasses.replace(e, entity_id="legacy-" + e.canonical_name) for e in ents]
        again, _ = resolve_case_entities(CASE, st, tx, previous_entities=legacy)
        self.assertEqual({e.entity_id for e in again}, {e.entity_id for e in legacy})
        amap = build_alias_map(legacy)
        self.assertEqual(amap["IMRAN SABLE"], "legacy-IMRAN SABLE")


class TestClusterReview(unittest.TestCase):
    """NLP-11."""

    def setUp(self) -> None:
        a = _stmt("DEEP PATANGE", "000401992831")
        self.st = [a]
        self.tx = [_tx(a, n) for n in ("POOJAN VIG", "PUJAN VIJ", "POOJAN VIGG", "KOLAR TEXTILES", "KOLAR TEXTILE MILLS")]

    def test_preview_is_pure_and_reports_evidence_and_candidates(self) -> None:
        before = [t.resolved_entity_id for t in self.tx]
        pv = preview_clusters(CASE, self.st, self.tx, 0.85)
        self.assertEqual(before, [t.resolved_entity_id for t in self.tx])
        self.assertEqual(pv["threshold"], 0.85)
        big = next(c for c in pv["clusters"] if "PUJAN VIJ" in c["aliases"])
        self.assertTrue(big["evidence"])
        self.assertIn("reason", big["evidence"][0])
        for key in ("candidates", "conflicts", "stats", "changes", "entity_count"):
            self.assertIn(key, pv)
        ents, _ = resolve_case_entities(CASE, self.st, self.tx, 0.85)
        self.assertEqual(sorted(e.entity_id for e in ents), sorted(c["entity_id"] for c in pv["clusters"]))

    def test_preview_changes_against_previous(self) -> None:
        strict, _ = resolve_case_entities(CASE, self.st, self.tx, 1.0)
        pv = preview_clusters(CASE, self.st, self.tx, 0.85, previous_entities=strict)
        self.assertTrue(pv["changes"]["merged"])

    def test_split_then_undo(self) -> None:
        ents, _ = resolve_case_entities(CASE, self.st, self.tx, 0.85)
        ent = _ent_of(ents, "PUJAN VIJ")
        self.assertGreaterEqual(len(ent.aliases), 2)
        decisions, d = split_entity([], ents, ent.entity_id, ["PUJAN VIJ"], actor="analyst")
        self.assertEqual(d["op"], "split")
        after, _ = resolve_case_entities(CASE, self.st, self.tx, 0.85, decisions=decisions)
        self.assertIsNot(_ent_of(after, "PUJAN VIJ"), _ent_of(after, "POOJAN VIG"))
        # The un-split remainder keeps the original id
        self.assertEqual(_ent_of(after, "POOJAN VIG").entity_id, ent.entity_id)
        decisions2, removed = undo_decision(decisions)
        self.assertEqual(removed["decision_id"], d["decision_id"])
        self.assertEqual(decisions2, [])
        again, _ = resolve_case_entities(CASE, self.st, self.tx, 0.85, decisions=decisions2)
        self.assertIs(_ent_of(again, "PUJAN VIJ"), _ent_of(again, "POOJAN VIG"))

    def test_split_validation(self) -> None:
        ents, _ = resolve_case_entities(CASE, self.st, self.tx, 0.85)
        ent = _ent_of(ents, "PUJAN VIJ")
        with self.assertRaises(ValueError):
            split_entity([], ents, ent.entity_id, list(ent.aliases))
        with self.assertRaises(ValueError):
            split_entity([], ents, ent.entity_id, ["NOT AN ALIAS"])
        with self.assertRaises(ValueError):
            split_entity([], ents, "missing-id", ["PUJAN VIJ"])

    def test_merge_by_entity_id_then_later_split_wins_and_merge_after_reject(self) -> None:
        ents, _ = resolve_case_entities(CASE, self.st, self.tx, 0.99)
        a, b = _ent_of(ents, "KOLAR TEXTILES"), _ent_of(ents, "POOJAN VIG")
        decs, m = merge_entities([], ents, a.entity_id, b.entity_id)
        merged, _ = resolve_case_entities(CASE, self.st, self.tx, 0.99, decisions=decs)
        self.assertIs(_ent_of(merged, "KOLAR TEXTILES"), _ent_of(merged, "POOJAN VIG"))
        self.assertEqual(_ent_of(merged, "KOLAR TEXTILES").entity_id, b.entity_id)
        # A later split of the manually merged entity wins over the earlier merge
        decs2, _ = split_entity(decs, merged, b.entity_id, ["KOLAR TEXTILES"])
        unmerged, _ = resolve_case_entities(CASE, self.st, self.tx, 0.99, decisions=decs2)
        self.assertIsNot(_ent_of(unmerged, "KOLAR TEXTILES"), _ent_of(unmerged, "POOJAN VIG"))
        # A reject between two separate entities blocks the fuzzy merge
        loose, _ = resolve_case_entities(CASE, self.st, self.tx, 0.85)
        self.assertIs(_ent_of(loose, "POOJAN VIG"), _ent_of(loose, "PUJAN VIJ"))
        strict, _ = resolve_case_entities(CASE, self.st, self.tx, 1.0)
        decs3, _ = reject_merge([], strict, _ent_of(strict, "POOJAN VIG").entity_id, _ent_of(strict, "PUJAN VIJ").entity_id)
        rej, _ = resolve_case_entities(CASE, self.st, self.tx, 0.85, decisions=decs3)
        self.assertIsNot(_ent_of(rej, "POOJAN VIG"), _ent_of(rej, "PUJAN VIJ"))
        # A later merge overrides the earlier reject
        decs4, _ = merge_entities(decs3, rej, _ent_of(rej, "PUJAN VIJ").entity_id, _ent_of(rej, "POOJAN VIG").entity_id)
        fin, _ = resolve_case_entities(CASE, self.st, self.tx, 0.85, decisions=decs4)
        self.assertIs(_ent_of(fin, "POOJAN VIG"), _ent_of(fin, "PUJAN VIJ"))

    def test_undo_specific_and_unknown(self) -> None:
        ents, _ = resolve_case_entities(CASE, self.st, self.tx, 1.0)
        d1s, d1 = reject_merge([], ents, _ent_of(ents, "POOJAN VIG").entity_id, _ent_of(ents, "PUJAN VIJ").entity_id)
        d2s, d2 = merge_entities(d1s, ents, _ent_of(ents, "KOLAR TEXTILES").entity_id,
                                 _ent_of(ents, "KOLAR TEXTILE MILLS").entity_id)
        out, removed = undo_decision(d2s, d1["decision_id"])
        self.assertEqual([x["decision_id"] for x in out], [d2["decision_id"]])
        self.assertEqual(removed, d1)
        self.assertEqual(len(d2s), 2, "inputs are not mutated")
        with self.assertRaises(ValueError):
            undo_decision(d2s, "nope")
        self.assertEqual(undo_decision([]), ([], None))


class TestBlockingScale(unittest.TestCase):
    """NLP-05: blocking keeps clustering sub-quadratic."""

    def test_10k_parties(self) -> None:
        rng = random.Random(7)
        first = ["RAMESH", "SURESH", "POOJA", "ANIL", "SUNITA", "IMRAN", "AYAZ", "DEEPAK", "MANISH", "KAVITA",
                 "RAHUL", "PRIYA", "VIKAS", "NEHA", "ARJUN", "FATIMA", "GURPREET", "HARISH", "JYOTI", "LAXMI"]
        last = ["KUMAR", "SHARMA", "PATIL", "QURESHI", "VIG", "DESAI", "REDDY", "NAIR", "SINGH", "KHAN",
                "MEHTA", "JOSHI", "GUPTA", "BHATT", "RAO", "IYER", "PATEL", "SHAH", "VERMA", "DAS"]
        a = _stmt("DEEP PATANGE", "000401992831")
        names = set()
        while len(names) < 10_000:
            syl = "".join(rng.choice("BCDFGHJKLMNPRSTV") + rng.choice("AEIOU") for _ in range(rng.randint(1, 3)))
            names.add(f"{rng.choice(first)} {syl} {rng.choice(last)}")
        txns = [_tx(a, n) for n in sorted(names)]
        t0 = time.perf_counter()
        diag: dict = {}
        ents, _ = resolve_case_entities(CASE, [a], txns, 0.85, diagnostics=diag)
        elapsed = time.perf_counter() - t0
        self.assertLess(diag["stats"]["comparisons"], 10_000 * 10_000 // 20)
        self.assertLess(elapsed, 60.0)
        self.assertGreater(len(ents), 100)


if __name__ == "__main__":
    unittest.main()
