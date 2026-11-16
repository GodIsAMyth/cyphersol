"""TEST-04 / EDGE-30: Tally XML structure properties on adversarial generated ledgers."""

from __future__ import annotations

import random
import re
import unittest
import xml.etree.ElementTree as ET
from decimal import Decimal

from ._harness import CaseBuilder, money, random_property

from cyphersol.edge import tally_exporter  # noqa: E402
from cyphersol.edge.tally_exporter import PREDEFINED_TALLY_GROUPS, generate_tally_xml  # noqa: E402

# Ledgers every Tally company already has (Cash under Cash-in-Hand, Profit & Loss A/c)
PREDEFINED_LEDGERS = frozenset(getattr(tally_exporter, "_PREDEFINED_LEDGERS", {"Cash", "Profit & Loss A/c"}))
from cyphersol.taxonomy import CATEGORY_BY_ID  # noqa: E402

# Characters XML 1.0 forbids (C0 controls except TAB/LF/CR, U+FFFE/U+FFFF, lone surrogates)
XML10_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿\ud800-\udfff]")
NASTY = [
    "A&B TRADERS", "<script>alert(1)</script>", 'O"BRIEN & SONS', "D'SOUZA", "]]> CDATA END", "TAB\tNAME",
    "CTRL\x01\x02\x1fNAME", "BOM﻿NAME", "￾BAD", "हिंदी ट्रेडर्स", "ZERO​WIDTH", "X" * 300, "  ", "",
    "SBI - 0001", "Suspense A/c", "Bank Accounts", "Sundry Debtors", "café ñandú", "EMOJI \U0001F4B8",
]


def _ledger_masters(root):
    return {l.get("NAME"): (l.findtext("PARENT") or "") for l in root.iter("LEDGER")}


class TallyStructureProperties(unittest.TestCase):
    @random_property(40)
    def test_xml_is_wellformed_balanced_and_closed(self, rng: random.Random) -> None:
        b = CaseBuilder()
        holders = [rng.choice(NASTY[:12]) + f" {i}" for i in range(rng.randint(1, 3))]
        for h in holders:
            b.statement(h, bank=rng.choice(["SBI", "HDFC", "ICICI & CO", "AXIS<B>"]))
        cats = sorted(CATEGORY_BY_ID)
        for i in range(rng.randint(1, 50)):
            cid = rng.choice(cats)
            cp = rng.choice(NASTY + holders + [None])
            b.tx(rng.choice(holders), rng.choice(["CR", "DR"]), cp, money(rng, 1, 2_000_000), rng.randint(0, 200),
                 narration=rng.choice(NASTY) + f" {i}", category=(cid, CATEGORY_BY_ID[cid].name),
                 utr=rng.choice([None, f"UTR{i:08d}", "<&>"]))
        for t in b.transactions:
            t.tally_voucher_type = CATEGORY_BY_ID[t.category_id].tally_voucher
            t.tally_ledger_name = t.extracted_counterparty or t.category_name
            t.txn_date = f"2025-{4 + (int(t.txn_date[8:10]) % 9):02d}-{t.txn_date[8:10]}"
        xml, stats = generate_tally_xml(
            rng.choice(NASTY), b.statements, b.transactions, include_suspense=rng.random() < 0.5,
            company_name=rng.choice([None, "ACME & Co <Pvt>", "\x01Co"]), use_trade_vouchers=rng.choice([None, True, False]),
            voucher_prefix=rng.choice([None, "CS", "X&Y"]),
        )
        self.assertIsNone(XML10_ILLEGAL.search(xml), "XML 1.0 illegal character in output")
        root = ET.fromstring(xml.encode("utf-8"))  # re-parse
        self.assertEqual(root.tag, "ENVELOPE")
        groups = {g.get("NAME"): (g.findtext("PARENT") or "") for g in root.iter("GROUP")}
        for gname, gparent in groups.items():
            self.assertTrue(gparent in PREDEFINED_TALLY_GROUPS or gparent in groups, (gname, gparent))
            self.assertNotIn(gname, PREDEFINED_TALLY_GROUPS)
        ledgers = _ledger_masters(root)
        for lname, parent in ledgers.items():
            self.assertTrue(lname and lname.strip(), "empty ledger name")
            self.assertTrue(parent in PREDEFINED_TALLY_GROUPS or parent in groups, (lname, parent))
            self.assertNotIn(lname.casefold(), {p.casefold() for p in PREDEFINED_LEDGERS}, "redeclared predefined ledger")
        known = set(ledgers) | PREDEFINED_LEDGERS
        vouchers = list(root.iter("VOUCHER"))
        self.assertEqual(len(vouchers), stats["vouchers_exported"])
        numbers, guids = set(), set()
        for v in vouchers:
            entries = v.findall("ALLLEDGERENTRIES.LIST")
            self.assertGreaterEqual(len(entries), 2)
            total = Decimal("0")
            for e in entries:
                amt = Decimal(e.findtext("AMOUNT"))
                total += amt
                self.assertNotEqual(amt, 0)
                self.assertEqual(e.findtext("ISDEEMEDPOSITIVE"), "Yes" if amt < 0 else "No")
                self.assertIn(e.findtext("LEDGERNAME"), known, "voucher references an undeclared ledger")
            self.assertEqual(total, 0, "unbalanced voucher")
            self.assertRegex(v.findtext("DATE") or "", r"^\d{8}$")
            numbers.add(v.findtext("VOUCHERNUMBER"))
            guids.add(v.findtext("GUID"))
            self.assertEqual(v.get("REMOTEID"), v.findtext("GUID"))
        self.assertEqual(len(numbers), len(vouchers), "duplicate voucher numbers")
        self.assertEqual(len(guids), len(vouchers), "duplicate GUIDs")
        # deterministic: same input -> byte-identical XML
        xml2, _ = generate_tally_xml(None, b.statements, b.transactions)
        xml3, _ = generate_tally_xml(None, b.statements, b.transactions)
        self.assertEqual(xml2, xml3)


if __name__ == "__main__":
    unittest.main()
