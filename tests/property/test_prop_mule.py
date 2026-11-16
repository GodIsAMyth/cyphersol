"""TEST-01 / TEST-02: mule score range, tier consistency and contribution arithmetic on random accounts."""

from __future__ import annotations

import random
import unittest

from ._harness import CaseBuilder, money, random_property

from cyphersol.models import MuleRiskTier  # noqa: E402
from cyphersol.trace.mule_scorer import PARAMETER_WEIGHTS, evaluate_all_mule_accounts, score_account_mule_risk  # noqa: E402

CHANNELS = ["UPI", "NEFT", "RTGS", "IMPS", "ATM", "POS", "AEPS", "CASH_WDL", "CHEQUE", "OTHER"]
NARR = ["UPI/{cp}", "ATM WDL", "BINANCE DEPOSIT", "PAYTM WALLET LOAD", "SWIGGY ORDER", "NEFT/{cp}", "CASH DEP"]


def expected_tier(score: float) -> MuleRiskTier:
    return MuleRiskTier.CRITICAL if score >= 80 else MuleRiskTier.SUSPICIOUS if score >= 50 else MuleRiskTier.LOW_RISK


class MuleProperties(unittest.TestCase):
    @random_property(60)
    def test_score_range_tier_and_contributions(self, rng: random.Random) -> None:
        b = CaseBuilder()
        st = b.statement("SUBJECT")
        cps = [f"CP{i}" for i in range(rng.randint(1, 30))]
        bal = 0
        for i in range(rng.randint(0, 150)):
            cr = rng.random() < 0.5
            amt = money(rng, 1, rng.choice([5_000, 60_000, 1_200_000]))
            cp = rng.choice(cps)
            t = b.tx("SUBJECT", "CR" if cr else "DR", cp, amt, rng.randint(0, 180), rng.randint(0, 23),
                     channel=rng.choice(CHANNELS), narration=rng.choice(NARR).format(cp=cp))
            bal += float(amt) if cr else -float(amt)
            if rng.random() < 0.7:
                from decimal import Decimal
                t.running_balance = Decimal(str(round(max(bal, 0.0), 2)))
        a = score_account_mule_risk(st, b.transactions)
        self.assertTrue(0.0 <= a.composite_score <= 100.0, a.composite_score)
        self.assertEqual(a.risk_tier, expected_tier(a.composite_score))
        self.assertEqual(len(a.explainability), len(PARAMETER_WEIGHTS))
        for v in a.explainability:
            self.assertTrue(0.0 <= v["score"] <= 1.0, v)
            self.assertAlmostEqual(v["contribution"], 100.0 * v["weight"] * v["score"], delta=0.006)
        self.assertAlmostEqual(sum(v["contribution"] for v in a.explainability), a.composite_score, delta=0.06)
        # scoring is a pure function of the rows: order of input does not matter
        shuffled = list(b.transactions)
        rng.shuffle(shuffled)
        self.assertEqual(score_account_mule_risk(st, shuffled).composite_score, a.composite_score)
        # the batch path agrees with the single-account path
        batch = evaluate_all_mule_accounts(b.statements, b.transactions, b.ents())
        self.assertEqual(len(batch), 1)
        self.assertEqual(batch[0].composite_score, a.composite_score)


if __name__ == "__main__":
    unittest.main()
