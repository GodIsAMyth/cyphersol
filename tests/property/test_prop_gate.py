"""TEST-01 / ING-25: balance conservation through parse -> anomaly gate on generated statements."""

from __future__ import annotations

import csv
import datetime as dt
import io
import random
import unittest
from decimal import Decimal
from typing import List, Tuple

from ._harness import money, random_property  # noqa: F401  (sys.path set up by the harness)

from cyphersol.ingestion.anomaly_gate import run_anomaly_gate  # noqa: E402
from cyphersol.ingestion.parser import parse_statement_bytes  # noqa: E402
from cyphersol.models import AnomalyFlag  # noqa: E402

NARRATIONS = [
    "UPI/DR/{ref}/RAVI KUMAR/ravi@okaxis", "NEFT-HDFC0000123-{ref}-SHREE GANESH STEEL, PUNE",
    'IMPS/P2A/{ref}/ANITA "ANU" DESAI', "RTGS/UTR{ref}/OM LOGISTICS PVT LTD", "ATM WDL {ref} MG ROAD",
    "POS {ref} RELIANCE FRESH", "NACH/DR/{ref}/HDFC HOME LOAN EMI", "CHQ DEP {ref} CLG",
]


def _inr(v: Decimal) -> str:
    whole, frac = f"{v:.2f}".split(".")
    if len(whole) > 3:
        head, tail, groups = whole[:-3], whole[-3:], []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        whole = ",".join(([head] if head else []) + groups + [tail])
    return f"{whole}.{frac}"


def build_csv(rng: random.Random, n: int, descending: bool = False) -> Tuple[bytes, List[Tuple[str, Decimal, Decimal, Decimal]], Decimal]:
    opening = money(rng, 0, 100_000)
    bal = opening
    day = 0
    rows = []
    fmt_amt = rng.choice([lambda v: f"{v:.2f}", _inr])
    blank = rng.choice(["", "0", "0.00"])
    for i in range(n):
        day += rng.choice([0, 0, 1, 2, 3])
        d = dt.date(2026, 4, 1) + dt.timedelta(days=day)
        amt = money(rng, 1, 250_000)
        if rng.random() < 0.5 or bal < amt:
            dr, cr = Decimal("0"), amt
        else:
            dr, cr = amt, Decimal("0")
        bal = bal + cr - dr
        rows.append((d.isoformat(), dr, cr, bal, rng.choice(NARRATIONS).format(ref=rng.randint(10**6, 10**9))))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"])
    body = list(reversed(rows)) if descending else rows
    for i, (d, dr, cr, b, narr) in enumerate(body):
        w.writerow([d, narr, f"R{i:05d}", fmt_amt(dr) if dr else blank, fmt_amt(cr) if cr else blank, fmt_amt(b)])
    return out.getvalue().encode("utf-8"), [(r[0], r[1], r[2], r[3]) for r in rows], opening


def ingest(data: bytes):
    stmt, txns, _meta = parse_statement_bytes("CASEP_PROP-HOLDER_HDFC_501000001111.csv", data, "CASEP")
    return run_anomaly_gate(stmt, txns)


def mismatched(txns) -> List[int]:
    return [i for i, t in enumerate(txns) if AnomalyFlag.BALANCE_MISMATCH in t.anomaly_flags]


class BalanceConservationProperties(unittest.TestCase):
    @random_property(40)
    def test_parse_gate_conserves_balances_and_rows(self, rng: random.Random) -> None:
        n = rng.randint(1, 60)
        data, expected, opening = build_csv(rng, n, descending=False)
        stmt, txns, summary = ingest(data)
        self.assertEqual(len(txns), n, "silent row drop or invention")
        self.assertEqual(summary["balance_mismatch"], 0)
        self.assertEqual(mismatched(txns), [])
        self.assertEqual(stmt.opening_balance, opening)
        self.assertEqual(stmt.closing_balance, expected[-1][3])
        self.assertEqual(sum(t.credit for t in txns) - sum(t.debit for t in txns), stmt.closing_balance - stmt.opening_balance)
        for t, (d, dr, cr, bal) in zip(txns, expected):
            self.assertEqual((t.txn_date, t.debit, t.credit, t.running_balance), (d, dr, cr, bal))
        self.assertEqual([t.row_index for t in txns], list(range(n)))

    @random_property(20)
    def test_descending_statement_is_normalised(self, rng: random.Random) -> None:
        n = rng.randint(3, 40)
        data, expected, opening = build_csv(rng, n, descending=True)
        if expected[0][0] == expected[-1][0]:
            return  # single-day descending files are only detectable by balance hits; covered elsewhere
        stmt, txns, summary = ingest(data)
        self.assertEqual(len(txns), n)
        self.assertEqual(summary["balance_mismatch"], 0)
        self.assertEqual(summary["date_out_of_order"], 0)
        self.assertEqual(stmt.opening_balance, opening)
        self.assertEqual(stmt.closing_balance, expected[-1][3])

    @random_property(30)
    def test_single_tampered_balance_is_localised(self, rng: random.Random) -> None:
        n = rng.randint(3, 40)
        data, _expected, _opening = build_csv(rng, n)
        k = rng.randint(1, n - 1)
        rows = list(csv.reader(io.StringIO(data.decode("utf-8"))))
        bal = Decimal(rows[k + 1][5].replace(",", ""))
        rows[k + 1][5] = f"{bal + Decimal(rng.choice(['0.10', '1.00', '100.00', '-7.77'])):.2f}"
        out = io.StringIO()
        csv.writer(out).writerows(rows)
        _stmt, txns, summary = ingest(out.getvalue().encode("utf-8"))
        self.assertEqual(len(txns), n)
        flagged = mismatched(txns)
        self.assertIn(k, flagged)
        self.assertTrue(set(flagged) <= {k, k + 1}, flagged)
        self.assertEqual(summary["balance_mismatch"], len(flagged))


if __name__ == "__main__":
    unittest.main()
