"""P1 roadmap item 17: anomaly gate completeness (ING-10/11/15/16/17/18/23)."""

from __future__ import annotations

import copy
import os
import sys
import unittest
from decimal import Decimal

ROOT_DIR = "/Users/manu/Library/CloudStorage/OneDrive-Personal/AML"
SRC_DIR = os.path.join(ROOT_DIR, "src")
TESTS_DIR = os.path.join(ROOT_DIR, "tests")
for _p in (SRC_DIR, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from cyphersol.ingestion import anomaly_gate  # noqa: E402
from cyphersol.ingestion.anomaly_gate import (  # noqa: E402
    duplicate_signature,
    find_duplicate_groups,
    mark_bulk_duplicates_ignored,
    normalize_narration_for_dedup,
    run_anomaly_gate,
)
from cyphersol.ingestion.parser import (  # noqa: E402
    detect_bank_and_metadata,
    parse_standardized_filename,
    parse_statement_bytes,
)
from cyphersol.models import AnomalyFlag  # noqa: E402

from test_p0_edge import mk_stmt, mk_tx  # noqa: E402

SAMPLES = os.path.join(ROOT_DIR, "sample_statements")


def _gate(stmt, txns, existing=None):
    return run_anomaly_gate(stmt, txns, existing_case_txns=existing)


def _rows():
    """Three reconciling rows on account 123456789012 (opening 1000)."""
    return [
        mk_tx(0, "2025-04-01", "UPI/CR/111/ALPHA", cr=500, bal=1500),
        mk_tx(1, "2025-04-02", "NEFT/DR/BETA TRADERS", dr=200, bal=1300),
        mk_tx(2, "2025-04-03", "ATM WDL", dr=300, bal=1000),
    ]


class DuplicateKeyTests(unittest.TestCase):
    """ING-15: SRS key (date, amount, direction, normalised narration, balance) + reference."""

    def test_narration_normalisation_ignores_case_space_and_punctuation(self):
        self.assertEqual(
            normalize_narration_for_dedup("  NEFT/DR -  Beta   Traders. "),
            normalize_narration_for_dedup("neft dr beta traders"),
        )

    def test_same_amount_and_balance_but_different_narration_is_not_duplicate(self):
        st = mk_stmt(opening="1000", closing="1300")
        txns = [
            mk_tx(0, "2025-04-01", "UPI/CR/ALPHA", cr=500, bal=1500),
            mk_tx(1, "2025-04-01", "UPI/DR/GAMMA", dr=200, bal=1300),
        ]
        # A second statement of another file has the same date/amount/balance but a different narration
        other = [mk_tx(0, "2025-04-01", "CASH DEPOSIT BRANCH", cr=500, bal=1500, sid="ST2")]
        for t in other:
            t.source_account_number = "123456789012"
        _, out, summ = _gate(st, txns, existing=other)
        self.assertEqual(summ["duplicate_suspect"], 0)
        self.assertNotIn(AnomalyFlag.DUPLICATE_SUSPECT, out[0].anomaly_flags)

    def test_overlapping_statement_with_reformatted_narration_is_duplicate(self):
        q1 = _rows()
        for t in q1:
            t.source_account_number = "123456789012"
        h1 = copy.deepcopy(_rows())
        h1[1].raw_narration = "neft / dr / beta  traders"
        for i, t in enumerate(h1):
            t.tx_id = f"H1_{i}"
            t.statement_id = "ST2"
        st = mk_stmt(sid="ST2", opening="1000", closing="1000")
        _, out, summ = _gate(st, h1, existing=q1)
        self.assertEqual(summ["duplicate_suspect"], 3)
        self.assertTrue(all(AnomalyFlag.DUPLICATE_SUSPECT in t.anomaly_flags for t in out))

    def test_direction_is_part_of_the_key(self):
        a = mk_tx(0, "2025-04-01", "TRF SELF", cr=100, bal=0)
        b = mk_tx(1, "2025-04-01", "TRF SELF", dr=100, bal=0)
        self.assertNotEqual(duplicate_signature(a, "X")[:-1], duplicate_signature(b, "X")[:-1])

    def test_distinct_references_are_not_duplicates_but_missing_ref_matches(self):
        st = mk_stmt(opening="0", closing="0")
        txns = [
            mk_tx(0, "2025-04-01", "UPI PAYMENT", dr=100, utr="111111111111"),
            mk_tx(1, "2025-04-01", "UPI PAYMENT", dr=100, utr="222222222222"),
            mk_tx(2, "2025-04-01", "UPI PAYMENT", dr=100),
        ]
        _, out, summ = _gate(st, txns)
        self.assertNotIn(AnomalyFlag.DUPLICATE_SUSPECT, out[1].anomaly_flags)
        self.assertIn(AnomalyFlag.DUPLICATE_SUSPECT, out[2].anomaly_flags)
        self.assertEqual(summ["duplicate_suspect"], 1)

    def test_bulk_batch_cross_file_duplicates_and_one_click_ignore(self):
        a = _rows()
        b = copy.deepcopy(_rows())
        for i, t in enumerate(b):
            t.tx_id = f"B_{i}"
            t.statement_id = "ST2"
        for t in a + b:
            t.source_account_number = "123456789012"
        groups = find_duplicate_groups(a + b)
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[0]["keep_tx_id"], a[0].tx_id)
        self.assertEqual(groups[0]["duplicate_tx_ids"], [b[0].tx_id])
        changed = mark_bulk_duplicates_ignored(a + b)
        self.assertEqual(sorted(changed), sorted(t.tx_id for t in b))
        self.assertTrue(all(t.is_duplicate_ignored for t in b))
        self.assertFalse(any(t.is_duplicate_ignored for t in a))
        # Idempotent
        self.assertEqual(mark_bulk_duplicates_ignored(a + b), [])

    def test_different_accounts_never_duplicate(self):
        a = _rows()
        b = copy.deepcopy(_rows())
        for t in a:
            t.source_account_number = "111"
        for i, t in enumerate(b):
            t.source_account_number = "222"
            t.tx_id = f"B_{i}"
        self.assertEqual(find_duplicate_groups(a + b), [])

    def test_legacy_exact_duplicate_row_still_flagged(self):
        csv_bad = (
            "Txn Date,Narration,Debit,Credit,Balance\n"
            "2026-04-01,OPENING DEPOSIT,0,10000,10000\n"
            "2026-04-02,TAMPERED ROW,2000,0,9500\n"
            "2026-04-02,TAMPERED ROW,2000,0,9500\n"
        ).encode()
        stmt, txns, _ = parse_statement_bytes("CASE_BAD_ICICI_00112233.csv", csv_bad, "C")
        _, _, summ = run_anomaly_gate(stmt, txns)
        self.assertEqual(summ["duplicate_suspect"], 1)


class ZeroDualAndStatementFlagTests(unittest.TestCase):
    """ING-11 / ING-17 regression guards."""

    def test_zero_and_dual_amount_rows_flagged(self):
        st = mk_stmt(opening="1000", closing="1000")
        txns = [
            mk_tx(0, "2025-04-01", "CHARGES REVERSED", bal=1000),
            mk_tx(1, "2025-04-02", "CONTRA", dr=100, cr=100, bal=1000),
        ]
        _, out, summ = _gate(st, txns)
        self.assertEqual(summ["zero_or_dual_amount"], 2)
        for t in out:
            self.assertIn(AnomalyFlag.ZERO_OR_DUAL_AMOUNT, t.anomaly_flags)

    def test_valid_rows_exist_and_metadata_gaps_are_statement_level(self):
        st = mk_stmt(opening="1000", closing="1000")
        st.ifsc_code = None
        _, out, summ = _gate(st, _rows())
        self.assertEqual(summ["valid"], 3)
        self.assertIn("ifsc_code", summ["missing_metadata"])
        self.assertIn(AnomalyFlag.MISSING_METADATA.value, summ["statement_flags"])
        self.assertTrue(all(t.anomaly_flags == [AnomalyFlag.VALID] for t in out))


class OpeningClosingTests(unittest.TestCase):
    """ING-16: row 0 vs the stated opening, last row vs the stated closing, and stated-period coverage."""

    def test_row0_checked_against_statement_opening(self):
        st = mk_stmt(opening="900", closing="1000")
        _, out, summ = _gate(st, _rows())
        self.assertEqual(summ["opening_balance_mismatch"], 1)
        self.assertIn(AnomalyFlag.BALANCE_MISMATCH, out[0].anomaly_flags)

    def test_last_row_checked_against_statement_closing(self):
        st = mk_stmt(opening="1000", closing="1234")
        _, out, summ = _gate(st, _rows())
        self.assertEqual(summ["closing_balance_mismatch"], 1)
        self.assertIn(AnomalyFlag.BALANCE_MISMATCH, out[-1].anomaly_flags)

    def test_csv_stated_opening_balance_mismatch_detected(self):
        data = (
            "Account Name: TEST HOLDER\n"
            "Opening Balance: 5000.00\n"
            "Date,Narration,Debit,Credit,Balance\n"
            "01/04/2026,UPI/CR/ALPHA,0,500,1500\n"
            "02/04/2026,ATM WDL,500,0,1000\n"
        ).encode()
        stmt, txns, meta = parse_statement_bytes("C1_TEST-HOLDER_SBI_123456789012.csv", data, "C")
        _, out, summ = run_anomaly_gate(stmt, txns)
        self.assertEqual(summ["opening_balance_mismatch"], 1)
        self.assertIn(AnomalyFlag.BALANCE_MISMATCH, out[0].anomaly_flags)

    def test_rows_outside_stated_period_flagged_and_sticky(self):
        data = (
            "Account Name: TEST HOLDER\n"
            "Statement Period: 01/04/2026 to 30/04/2026\n"
            "Date,Narration,Debit,Credit,Balance\n"
            "05/04/2026,UPI/CR/ALPHA,0,500,1500\n"
            "03/05/2026,ATM WDL,500,0,1000\n"
        ).encode()
        stmt, txns, meta = parse_statement_bytes("C1_TEST-HOLDER_SBI_123456789012.csv", data, "C")
        self.assertIn(AnomalyFlag.OUTSIDE_STATEMENT_PERIOD, txns[1].anomaly_flags)
        self.assertNotIn(AnomalyFlag.OUTSIDE_STATEMENT_PERIOD, txns[0].anomaly_flags)
        self.assertEqual(meta["period_coverage"]["rows_outside"], 1)
        self.assertTrue(any("outside the stated statement period" in w for w in meta["warnings"]))
        for _ in range(2):
            stmt, txns, summ = run_anomaly_gate(stmt, txns)
        self.assertIn(AnomalyFlag.OUTSIDE_STATEMENT_PERIOD, txns[1].anomaly_flags)
        self.assertEqual(summ["outside_statement_period"], 1)

    def test_rows_inside_stated_period_not_flagged(self):
        data = (
            "Statement Period: 01/04/2026 to 30/04/2026\n"
            "Date,Narration,Debit,Credit,Balance\n"
            "05/04/2026,UPI/CR/ALPHA,0,500,1500\n"
            "30/04/2026,ATM WDL,500,0,1000\n"
        ).encode()
        _, txns, meta = parse_statement_bytes("C1_TEST-HOLDER_SBI_123456789012.csv", data, "C")
        self.assertFalse(any(AnomalyFlag.OUTSIDE_STATEMENT_PERIOD in t.anomaly_flags for t in txns))
        self.assertEqual(meta["period_coverage"]["rows_outside"], 0)


class ProvenanceStickinessTests(unittest.TestCase):
    """ING-10 / LLM-07: OCR_AUTOCORRECTED and LLM_REPAIRED survive any number of gate re-runs."""

    def test_llm_repaired_sticky_and_counted(self):
        st = mk_stmt(opening="1000", closing="1000")
        txns = _rows()
        txns[1].anomaly_flags = [AnomalyFlag.LLM_REPAIRED, AnomalyFlag.VALID]
        txns[2].anomaly_flags = [AnomalyFlag.OCR_AUTOCORRECTED, AnomalyFlag.LOW_OCR_CONFIDENCE]
        for _ in range(3):
            st, txns, summ = _gate(st, txns)
        self.assertIn(AnomalyFlag.LLM_REPAIRED, txns[1].anomaly_flags)
        self.assertNotIn(AnomalyFlag.VALID, txns[1].anomaly_flags)
        self.assertIn(AnomalyFlag.OCR_AUTOCORRECTED, txns[2].anomaly_flags)
        self.assertEqual(summ["llm_repaired"], 1)
        self.assertEqual(summ["ocr_autocorrected"], 1)
        self.assertEqual(summ["valid"], 1)

    def test_preserved_flags_cover_provenance(self):
        for f in (AnomalyFlag.OCR_AUTOCORRECTED, AnomalyFlag.LLM_REPAIRED, AnomalyFlag.OUTSIDE_STATEMENT_PERIOD):
            self.assertIn(f, anomaly_gate.PRESERVED_ROW_FLAGS)


class FilenameCrossCheckTests(unittest.TestCase):
    """ING-18: filename vs header cross-verification, unmasked account preferred, Period parsed."""

    def test_filename_period_parsed(self):
        m = parse_standardized_filename("C1_RAM-LAL_SBI_123456789012_01042025-31032026.pdf")
        self.assertEqual(m["account_hint"], "123456789012")
        self.assertEqual(m["period_start_hint"], "2025-04-01")
        self.assertEqual(m["period_end_hint"], "2026-03-31")
        m2 = parse_standardized_filename("C1_RAM-LAL_SBI_123456789012_FY2024-25.pdf")
        self.assertEqual((m2["period_start_hint"], m2["period_end_hint"]), ("2024-04-01", "2025-03-31"))
        m3 = parse_standardized_filename("C1_RAM-LAL_SBI_123456789012.pdf")
        self.assertIsNone(m3["period_start_hint"])

    def test_masked_header_account_replaced_by_matching_filename_account(self):
        _, meta = detect_bank_and_metadata(
            "Account Name: MANISH PATIL\nAccount No: XXXXXXX4001\n", "C1_MANISH-PATIL_SBI_30981234001.csv"
        )
        self.assertEqual(meta["account_number"], "30981234001")
        self.assertTrue(any("masked" in w.lower() for w in meta["detection_warnings"]))

    def test_conflicting_full_accounts_keep_header_and_warn(self):
        _, meta = detect_bank_and_metadata(
            "Account Name: MANISH PATIL\nAccount No: 30981234999\n", "C1_MANISH-PATIL_SBI_30981234001.csv"
        )
        self.assertEqual(meta["account_number"], "30981234999")
        self.assertTrue(any("account" in w.lower() and "filename" in w.lower() for w in meta["detection_warnings"]))
        self.assertEqual(meta["filename_crosscheck"]["account"], "conflict")

    def test_holder_mismatch_warns(self):
        _, meta = detect_bank_and_metadata(
            "Account Name: SURESH KUMAR\nAccount No: 30981234001\n", "C1_MANISH-PATIL_SBI_30981234001.csv"
        )
        self.assertEqual(meta["filename_crosscheck"]["holder"], "conflict")
        self.assertEqual(meta["filename_crosscheck"]["account"], "match")
        self.assertTrue(any("holder" in w.lower() for w in meta["detection_warnings"]))

    def test_holder_match_ignores_honorific_and_order(self):
        _, meta = detect_bank_and_metadata(
            "Account Name: Mr. PATIL MANISH R\nAccount No: 30981234001\n", "C1_MANISH-PATIL_SBI_30981234001.csv"
        )
        self.assertEqual(meta["filename_crosscheck"]["holder"], "match")

    def test_period_conflict_between_filename_and_header_warns(self):
        data = (
            "Statement Period: 01/04/2026 to 30/04/2026\n"
            "Date,Narration,Debit,Credit,Balance\n"
            "05/04/2026,UPI/CR/ALPHA,0,500,1500\n"
        ).encode()
        _, _, meta = parse_statement_bytes("C1_TEST-HOLDER_SBI_123456789012_01052026-31052026.csv", data, "C")
        self.assertEqual(meta["filename_crosscheck"]["period"], "conflict")
        self.assertTrue(any("period" in w.lower() and "filename" in w.lower() for w in meta["warnings"]))


class AccountTypeTests(unittest.TestCase):
    """ING-23: account type comes from a labelled header field, not stray words such as 'Current Balance'."""

    def test_current_balance_label_is_not_current_account(self):
        _, meta = detect_bank_and_metadata(
            "Account Name: RAM LAL\nAccount No: 123456789012\nCurrent Balance: 5,000.00\nBranch Code: OD12\n",
            "C1_RAM-LAL_SBI_123456789012.csv",
        )
        # EDGE-17 (repair): a statement that does not state its type is recorded as unknown, not "Savings"
        self.assertIsNone(meta["account_type"])

    def test_labelled_current_account(self):
        _, meta = detect_bank_and_metadata(
            "Account Name: RAM LAL\nAccount Type: Current Account\nAccount No: 123456789012\n",
            "C1_RAM-LAL_SBI_123456789012.csv",
        )
        self.assertEqual(meta["account_type"], "Current/OD")

    def test_overdraft_and_cash_credit(self):
        for hdr in ("Product: Cash Credit A/c\n", "Scheme : OD ACCOUNT - MSME\n", "CC A/C No: 123456789012\n"):
            _, meta = detect_bank_and_metadata("Account Name: RAM LAL\n" + hdr, "C1_RAM-LAL_SBI_123456789012.csv")
            self.assertEqual(meta["account_type"], "Current/OD", hdr)

    def test_labelled_savings_wins_over_body_words(self):
        _, meta = detect_bank_and_metadata(
            "Account Type: SAVINGS BANK\nAccount No: 123456789012\nDate,Narration,Debit,Credit,Balance\n"
            "01/04/2026,OD INTEREST CURRENT A/C TRF,10,0,100\n",
            "C1_RAM-LAL_SBI_123456789012.csv",
        )
        self.assertEqual(meta["account_type"], "Savings")


class SampleRegressionTests(unittest.TestCase):
    def test_samples_reconcile_and_have_valid_rows(self):
        for name in sorted(os.listdir(SAMPLES)):
            with open(os.path.join(SAMPLES, name), "rb") as fh:
                stmt, txns, _ = parse_statement_bytes(name, fh.read(), "C")
            _, txns, summ = run_anomaly_gate(stmt, txns)
            expected_bm = 1 if name.startswith("CASE041_APEX-SHELL") else 0  # APEX row 3 is a source defect
            self.assertEqual(summ["balance_mismatch"], expected_bm, name)
            self.assertEqual(summ["duplicate_suspect"], 0, name)
            self.assertGreater(summ["valid"], 0, name)
            self.assertEqual(summ["outside_statement_period"], 0, name)


if __name__ == "__main__":
    unittest.main()
