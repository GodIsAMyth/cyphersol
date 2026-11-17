"""Shared harness for property tests.

`random_property(n)` runs a test method `fn(self, rng)` over `n` generated `random.Random` instances:
under hypothesis (when installed) via `st.randoms(use_true_random=False)` with derandomised, database-free
settings so CI runs are reproducible; otherwise over seeds 0..n-1 (each failure reports its seed).
"""

from __future__ import annotations

import datetime as dt
import functools
import os
import random
import sys
from decimal import Decimal
from typing import Callable, Dict, List, Optional, Tuple

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.models import BankAccountStatement, NormalizedTransaction, ResolvedEntity, TxDirection  # noqa: E402

import tempfile

_P1_SCRATCH = os.path.join(tempfile.gettempdir(), "claude-501", "cyphersol-p1")
SCRATCH_DIR = os.path.join(_P1_SCRATCH if os.path.isdir(_P1_SCRATCH) else tempfile.gettempdir(), "property")

try:  # optional dependency (requirements.txt "# optional")
    from hypothesis import HealthCheck, given, settings, strategies as st
    from hypothesis.configuration import set_hypothesis_home_dir

    # keep hypothesis' caches out of the project tree
    set_hypothesis_home_dir(os.environ.get("CYPHERSOL_HYPOTHESIS_HOME") or os.path.join(SCRATCH_DIR, ".hypothesis"))
    HAVE_HYPOTHESIS = True
except ImportError:  # pragma: no cover - exercised only without hypothesis
    HAVE_HYPOTHESIS = False

FORCE_FALLBACK = os.environ.get("CYPHERSOL_PROPERTY_FALLBACK") == "1"


def random_property(max_examples: int = 25) -> Callable:
    def deco(fn: Callable) -> Callable:
        if HAVE_HYPOTHESIS and not FORCE_FALLBACK:
            return settings(
                max_examples=max_examples,
                deadline=None,
                derandomize=True,
                database=None,
                suppress_health_check=list(HealthCheck),
            )(given(rng=st.randoms(use_true_random=False))(fn))

        @functools.wraps(fn)
        def wrapper(self) -> None:
            for seed in range(max_examples):
                with self.subTest(seed=seed):
                    fn(self, random.Random(seed))

        return wrapper

    return deco


# --------------------------------------------------------------------------------------------- builders
CASE = "CASE_PROP"
BASE_DATE = dt.date(2026, 1, 1)


class CaseBuilder:
    """Statements, transactions and resolved entities for trace / mule / Tally properties."""

    def __init__(self) -> None:
        self.statements: List[BankAccountStatement] = []
        self.transactions: List[NormalizedTransaction] = []
        self.entities: Dict[str, ResolvedEntity] = {}
        self._stmt_by_name: Dict[str, BankAccountStatement] = {}
        self._n = 0

    def entity(self, name: str, has_statement: bool = False, account: Optional[str] = None) -> ResolvedEntity:
        if name not in self.entities:
            self.entities[name] = ResolvedEntity(
                entity_id=f"E_{name.replace(' ', '_')}", case_id=CASE, canonical_name=name, aliases=[name],
                linked_account_numbers=[account] if account else [], has_statement=has_statement,
            )
        return self.entities[name]

    def statement(self, name: str, bank: str = "HDFC") -> BankAccountStatement:
        account = f"{50100000 + len(self.statements) + 1:012d}"
        ent = self.entity(name, True)
        ent.has_statement = True
        ent.linked_account_numbers.append(account)
        st = BankAccountStatement(
            statement_id=f"ST{len(self.statements) + 1}", case_id=CASE, source_filename="x.csv",
            sha256_hash=f"{len(self.statements):064d}", bank_code=bank, bank_name=bank, account_number=account,
            account_holder_name=name, period_start="2026-01-01", period_end="2026-06-30",
            opening_balance=Decimal("0"), closing_balance=Decimal("0"), total_transactions=0,
        )
        self.statements.append(st)
        self._stmt_by_name[name] = st
        return st

    def tx(self, holder: str, direction: str, cp: Optional[str], amount: Decimal, day: int, hour: int = 12,
           channel: str = "UPI", narration: Optional[str] = None, category: Tuple[int, str] = (52, "Suspense / Uncategorized"),
           utr: Optional[str] = None) -> NormalizedTransaction:
        st = self._stmt_by_name[holder]
        d = BASE_DATE + dt.timedelta(days=day)
        self._n += 1
        is_cr = direction == "CR"
        amount = Decimal(amount).quantize(Decimal("0.01"))
        t = NormalizedTransaction(
            tx_id=f"T{self._n:06d}", statement_id=st.statement_id, case_id=CASE, row_index=self._n,
            txn_date=d.isoformat(), raw_narration=narration or f"{channel}/{cp or ''}", payment_channel=channel,
            direction=TxDirection.CREDIT if is_cr else TxDirection.DEBIT,
            debit=Decimal("0") if is_cr else amount, credit=amount if is_cr else Decimal("0"),
            running_balance=Decimal("0"), category_id=category[0], category_name=category[1],
            categorization_source="RULE", confidence_score=1.0, txn_timestamp=f"{d.isoformat()}T{hour:02d}:00:00",
            utr_number=utr, extracted_counterparty=cp, resolved_entity_id=self.entity(cp).entity_id if cp else None,
            source_account_number=st.account_number, source_holder_name=holder,
        )
        self.transactions.append(t)
        return t

    def ents(self) -> List[ResolvedEntity]:
        return list(self.entities.values())

    def eid(self, name: str) -> str:
        return self.entities[name].entity_id


def money(rng: random.Random, lo: int = 1, hi: int = 500_000) -> Decimal:
    """Random rupee amount with paise; round figures and threshold-adjacent values are over-represented."""
    pick = rng.random()
    if pick < 0.2:
        return Decimal(rng.randint(lo, max(lo, hi // 5000)) * 5000 if hi >= 5000 else rng.randint(lo, hi))
    if pick < 0.3:
        return Decimal(rng.choice(["49999.99", "49000", "199999", "999999.99", "0.01", "1.00"]))
    return (Decimal(rng.randint(lo * 100, hi * 100)) / 100).quantize(Decimal("0.01"))


def random_case(rng: random.Random, n_holders: int = 6, n_outsiders: int = 4, mirror_p: float = 0.6) -> Tuple[CaseBuilder, List[str]]:
    b = CaseBuilder()
    holders = [f"HOLDER {i}" for i in range(rng.randint(3, n_holders))]
    outsiders = [f"OUTSIDER {i}" for i in range(rng.randint(1, n_outsiders))]
    for h in holders:
        b.statement(h)
    for _ in range(rng.randint(15, 80)):
        src = rng.choice(holders)
        dst = rng.choice(holders + outsiders)
        if dst == src:
            continue
        amt = money(rng, 1, 200_000)
        day, hour = rng.randint(0, 45), rng.randint(0, 23)
        b.tx(src, "DR", dst, amt, day, hour, channel=rng.choice(["UPI", "NEFT", "IMPS", "RTGS"]))
        if dst in holders and rng.random() < mirror_p:
            b.tx(dst, "CR", src, amt, day + rng.choice([0, 0, 1, 2]), hour)
        if rng.random() < 0.3:
            b.tx(src, "CR", rng.choice(outsiders), money(rng, 1, 300_000), rng.randint(0, 45), rng.randint(0, 23))
    return b, holders
