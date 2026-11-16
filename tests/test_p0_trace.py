"""P0 regression tests for CypherTrace correctness (TF-01/02/04/09/10/12, TG-01/02/03/04/08/09/10/11, NLP-10, BE-09)."""

from __future__ import annotations

import datetime as dt
import os
import random
import sys
import time
import unittest
from decimal import Decimal
from typing import Dict, List, Optional, Tuple

ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC_DIR = os.path.join(ROOT_DIR, "src")
if SRC_DIR not in sys.path:
    sys.path.insert(0, SRC_DIR)

from cyphersol.models import (  # noqa: E402
    BankAccountStatement,
    NormalizedTransaction,
    ResolvedEntity,
    TxDirection,
)
from cyphersol.nlp.entity_resolver import is_system_entity, resolve_case_entities  # noqa: E402
from cyphersol.trace import graph_engine  # noqa: E402
from cyphersol.trace.bidirectional import analyze_bidirectional_flow, list_all_bidirectional_pairs  # noqa: E402
from cyphersol.trace.financial_profile import build_individual_financial_profile  # noqa: E402
from cyphersol.trace.graph_engine import build_network_graph, extract_directed_transfer_events  # noqa: E402
from cyphersol.trace.layering import build_layering_tree  # noqa: E402
from cyphersol.trace.loop_detector import detect_circular_loops  # noqa: E402

CASE = "CASE_P0_TRACE"
BASE_DATE = dt.date(2026, 1, 1)


class Fixture:
    """Tiny builder for statements, transactions and resolved entities."""

    def __init__(self) -> None:
        self.statements: List[BankAccountStatement] = []
        self.transactions: List[NormalizedTransaction] = []
        self.entities: Dict[str, ResolvedEntity] = {}
        self._stmt_by_name: Dict[str, BankAccountStatement] = {}
        self._row = 0
        self._tx = 0

    def entity(self, name: str, has_statement: bool = False, account: Optional[str] = None) -> ResolvedEntity:
        if name not in self.entities:
            self.entities[name] = ResolvedEntity(
                entity_id=f"E_{name.replace(' ', '_')}",
                case_id=CASE,
                canonical_name=name,
                aliases=[name],
                linked_account_numbers=[account] if account else [],
                has_statement=has_statement,
            )
        return self.entities[name]

    def statement(
        self,
        name: str,
        account: Optional[str] = None,
        opening: float = 0.0,
        closing: float = 0.0,
        period: Tuple[str, str] = ("2026-01-01", "2026-03-31"),
        sid: Optional[str] = None,
    ) -> BankAccountStatement:
        account = account or f"ACC{len(self.statements) + 1:06d}"
        ent = self.entity(name, True)
        ent.has_statement = True
        if account not in ent.linked_account_numbers:
            ent.linked_account_numbers.append(account)
        st = BankAccountStatement(
            statement_id=sid or f"ST{len(self.statements) + 1}",
            case_id=CASE,
            source_filename="x.csv",
            sha256_hash=f"h{len(self.statements)}",
            bank_code="HDFC",
            bank_name="HDFC Bank",
            account_number=account,
            account_holder_name=name,
            period_start=period[0],
            period_end=period[1],
            opening_balance=Decimal(str(opening)),
            closing_balance=Decimal(str(closing)),
            total_transactions=0,
        )
        self.statements.append(st)
        self._stmt_by_name.setdefault(name, st)
        return st

    def tx(
        self,
        holder: str,
        direction: str,
        cp: Optional[str],
        amount: float,
        day: int,
        hour: int = 12,
        channel: str = "UPI",
        utr: Optional[str] = None,
        balance: float = 0.0,
        stmt: Optional[BankAccountStatement] = None,
        category_id: int = 52,
        category_name: str = "Suspense / Uncategorized",
    ) -> NormalizedTransaction:
        st = stmt or self._stmt_by_name[holder]
        d = BASE_DATE + dt.timedelta(days=day)
        self._row += 1
        self._tx += 1
        is_cr = direction == "CR"
        t = NormalizedTransaction(
            tx_id=f"T{self._tx:06d}",
            statement_id=st.statement_id,
            case_id=CASE,
            row_index=self._row,
            txn_date=d.isoformat(),
            raw_narration=f"{channel}/{cp or ''}",
            payment_channel=channel,
            direction=TxDirection.CREDIT if is_cr else TxDirection.DEBIT,
            debit=Decimal("0") if is_cr else Decimal(str(amount)),
            credit=Decimal(str(amount)) if is_cr else Decimal("0"),
            running_balance=Decimal(str(balance)),
            category_id=category_id,
            category_name=category_name,
            categorization_source="RULE",
            confidence_score=1.0,
            txn_timestamp=f"{d.isoformat()}T{hour:02d}:00:00",
            utr_number=utr,
            extracted_counterparty=cp,
            resolved_entity_id=self.entity(cp).entity_id if cp else None,
            source_account_number=st.account_number,
            source_holder_name=holder,
        )
        self.transactions.append(t)
        return t

    def transfer(self, src: str, dst: str, amount: float, day: int, hour: int = 12, **kw) -> NormalizedTransaction:
        """A debit on src's statement (dst need not have a statement)."""
        return self.tx(src, "DR", dst, amount, day, hour, **kw)

    def ents(self) -> List[ResolvedEntity]:
        return list(self.entities.values())

    def eid(self, name: str) -> str:
        return self.entities[name].entity_id


def _walk(node: dict):
    yield node
    for c in node.get("children", []):
        yield from _walk(c)


def _find(node: dict, name: str) -> Optional[dict]:
    return next((n for n in _walk(node) if n["entity_name"] == name), None)


class TestLayeringFifo(unittest.TestCase):
    def _chain(self) -> Fixture:
        f = Fixture()
        f.statement("ROOT SRC")
        f.statement("MULE B")
        f.transfer("ROOT SRC", "MULE B", 10_000, day=1)
        f.transfer("MULE B", "PARTY C", 6_000, day=2)
        f.transfer("MULE B", "PARTY D", 6_000, day=3)
        f.transfer("MULE B", "PARTY E", 6_000, day=4)
        return f

    def test_tf01_fifo_never_exceeds_source_credit(self) -> None:
        f = self._chain()
        tree = build_layering_tree(f.statements, f.transactions, f.ents(), f.eid("ROOT SRC"), window_days=14)
        b = _find(tree["root"], "MULE B")
        self.assertEqual(b["attributed_amount"], 10_000)
        kids = {c["entity_name"]: c for c in b["children"]}
        self.assertEqual(set(kids), {"PARTY C", "PARTY D"})
        self.assertEqual(kids["PARTY C"]["attributed_amount"], 6_000)
        self.assertEqual(kids["PARTY D"]["attributed_amount"], 4_000)
        self.assertEqual(kids["PARTY D"]["debit_amount"], 6_000)
        self.assertEqual(kids["PARTY D"]["amount"], 4_000)
        self.assertLessEqual(sum(c["attributed_amount"] for c in b["children"]), b["attributed_amount"] + 0.005)
        self.assertEqual(tree["mode"], "fifo")

    def test_tf02_window_per_credit_lot(self) -> None:
        f = Fixture()
        f.statement("ROOT SRC")
        f.statement("MULE B")
        f.transfer("ROOT SRC", "MULE B", 1_000, day=1)
        f.transfer("ROOT SRC", "MULE B", 10_000, day=20)
        f.transfer("MULE B", "PARTY C", 10_000, day=22)
        tree = build_layering_tree(f.statements, f.transactions, f.ents(), f.eid("ROOT SRC"), window_days=14)
        b = _find(tree["root"], "MULE B")
        c = _find(b, "PARTY C")
        self.assertIsNotNone(c, "outflow inside the second lot's window must be traced")
        # Lot 1 (day 1) is expired on day 22, so only lot 2 funds the outflow.
        self.assertEqual(c["attributed_amount"], 10_000)

    def test_tf02_outflow_before_lot_is_not_attributed(self) -> None:
        f = Fixture()
        f.statement("ROOT SRC")
        f.statement("MULE B")
        f.transfer("MULE B", "PARTY EARLY", 5_000, day=1)
        f.transfer("ROOT SRC", "MULE B", 5_000, day=3)
        tree = build_layering_tree(f.statements, f.transactions, f.ents(), f.eid("ROOT SRC"))
        self.assertIsNone(_find(tree["root"], "PARTY EARLY"))

    def test_tf01_no_double_attribution_across_branches(self) -> None:
        f = Fixture()
        for nm in ("ROOT SRC", "MULE B", "MULE C", "MULE X"):
            f.statement(nm)
        f.transfer("ROOT SRC", "MULE B", 10_000, day=1)
        f.transfer("ROOT SRC", "MULE C", 10_000, day=1, hour=13)
        f.transfer("MULE B", "MULE X", 10_000, day=2)
        f.transfer("MULE C", "MULE X", 10_000, day=2, hour=13)
        f.transfer("MULE X", "PARTY Y", 10_000, day=3)  # a single 10k debit
        tree = build_layering_tree(f.statements, f.transactions, f.ents(), f.eid("ROOT SRC"))
        ys = [n for n in _walk(tree["root"]) if n["entity_name"] == "PARTY Y"]
        self.assertAlmostEqual(sum(n["attributed_amount"] for n in ys), 10_000, places=2)
        # FIFO: the older lot (via MULE B) funds the debit
        self.assertEqual(len(ys), 1)

    def test_same_timestamp_chain_is_traced(self) -> None:
        f = Fixture()
        f.statement("ROOT SRC")
        f.statement("MULE B")
        # Date-only statements: both legs carry the same timestamp; outflow row listed before the inflow
        f.transfer("MULE B", "PARTY C", 4_000, day=5)
        f.transfer("ROOT SRC", "MULE B", 4_000, day=5)
        tree = build_layering_tree(f.statements, f.transactions, f.ents(), f.eid("ROOT SRC"))
        c = _find(tree["root"], "PARTY C")
        self.assertIsNotNone(c)
        self.assertEqual(c["attributed_amount"], 4_000)

    def test_tf04_all_outflows_mode(self) -> None:
        f = self._chain()
        tree = build_layering_tree(f.statements, f.transactions, f.ents(), f.eid("ROOT SRC"), mode="all")
        b = _find(tree["root"], "MULE B")
        self.assertEqual({c["entity_name"] for c in b["children"]}, {"PARTY C", "PARTY D", "PARTY E"})
        self.assertEqual(tree["mode"], "all")
        legacy = build_layering_tree(f.statements, f.transactions, f.ents(), f.eid("ROOT SRC"), fifo_cap=False)
        self.assertEqual(legacy["mode"], "all")
        with self.assertRaises(ValueError):
            build_layering_tree(f.statements, f.transactions, f.ents(), f.eid("ROOT SRC"), mode="bogus")

    def test_property_fifo_invariant_and_netflow_antisymmetry(self) -> None:
        for seed in range(25):
            rng = random.Random(seed)
            f = Fixture()
            holders = [f"HOLDER {i}" for i in range(6)]
            outsiders = [f"OUTSIDER {i}" for i in range(4)]
            for h in holders:
                f.statement(h)
            for _ in range(rng.randint(20, 70)):
                src = rng.choice(holders)
                dst = rng.choice(holders + outsiders)
                if dst == src:
                    continue
                amt = float(rng.choice([500, 1_000, 2_500, 7_000, 12_345.67, 50_000]))
                day = rng.randint(0, 45)
                hour = rng.randint(0, 23)
                f.transfer(src, dst, amt, day, hour)
                if dst in holders and rng.random() < 0.6:
                    # mirrored credit leg, sometimes T+1
                    f.tx(dst, "CR", src, amt, day + rng.choice([0, 0, 1, 2]), hour)
            ents = f.ents()
            root = f.eid(rng.choice(holders))
            tree = build_layering_tree(
                f.statements, f.transactions, ents, root, window_days=rng.choice([3, 7, 14]), max_depth=5
            )
            r = tree["root"]
            self.assertAlmostEqual(sum(c["attributed_amount"] for c in r["children"]), r["attributed_amount"], places=2)
            for node in _walk(r):
                kids = node.get("children", [])
                self.assertLessEqual(
                    sum(c["attributed_amount"] for c in kids), node["attributed_amount"] + 0.01, f"seed={seed}"
                )
                if node["depth"] > 0:
                    self.assertLessEqual(node["attributed_amount"], node["debit_amount"] + 0.01)
                    self.assertGreater(node["attributed_amount"], 0)
            # NetFlow antisymmetry for every pair
            for a in holders[:3]:
                for b in holders[3:]:
                    ab = analyze_bidirectional_flow(f.statements, f.transactions, ents, f.eid(a), f.eid(b))
                    ba = analyze_bidirectional_flow(f.statements, f.transactions, ents, f.eid(b), f.eid(a))
                    self.assertAlmostEqual(ab.net_flow, -ba.net_flow, places=2)
                    self.assertAlmostEqual(ab.net_flow, ab.total_a_to_b - ab.total_b_to_a, places=2)


class TestFinancialProfile(unittest.TestCase):
    def test_tg01_details_only_profile_from_counterparty_side(self) -> None:
        f = Fixture()
        f.statement("HOLDER H")
        f.transfer("HOLDER H", "DETAILS X", 5_000, day=3)  # X receives 5k
        f.tx("HOLDER H", "CR", "DETAILS X", 3_000, day=10)  # X pays 3k
        prof = build_individual_financial_profile(f.eid("DETAILS X"), f.statements, f.transactions, f.ents(), [])
        self.assertTrue(prof["derived_from_counterparty_statements"])
        self.assertEqual(prof["kpis"]["total_credits"], 5_000)
        self.assertEqual(prof["kpis"]["total_debits"], 3_000)
        self.assertEqual(prof["kpis"]["net_flow"], 2_000)
        self.assertEqual(prof["monthly_breakdown"][0]["credit"], 5_000)
        self.assertEqual(prof["monthly_breakdown"][0]["debit"], 3_000)
        self.assertEqual(prof["top_senders"][0]["party"], "HOLDER H")
        self.assertEqual(prof["top_senders"][0]["amount"], 5_000)
        self.assertEqual(prof["top_beneficiaries"][0]["amount"], 3_000)
        self.assertEqual(prof["category_breakdown"][0]["credit"], 5_000)

        holder = build_individual_financial_profile(f.eid("HOLDER H"), f.statements, f.transactions, f.ents(), [])
        self.assertFalse(holder["derived_from_counterparty_statements"])

    def test_tg02_opening_closing_per_account(self) -> None:
        f = Fixture()
        f.statement("SUBJECT S", account="A1", opening=1_000, closing=2_000, period=("2026-01-01", "2026-01-31"), sid="S_JAN")
        f.statement("SUBJECT S", account="A1", opening=2_000, closing=500, period=("2026-02-01", "2026-02-28"), sid="S_FEB")
        f.statement("SUBJECT S", account="B1", opening=100, closing=100, period=("2026-01-01", "2026-02-28"), sid="S_B")
        prof = build_individual_financial_profile(f.eid("SUBJECT S"), f.statements, f.transactions, f.ents(), [])
        self.assertEqual(prof["kpis"]["opening_balance"], 1_100)
        self.assertEqual(prof["kpis"]["closing_balance"], 600)

    def test_tg03_abb_sums_per_account_eod_series(self) -> None:
        f = Fixture()
        sa = f.statement("SUBJECT S", account="A1", opening=10_000, closing=10_000, period=("2026-01-01", "2026-01-31"))
        sb = f.statement("SUBJECT S", account="B1", opening=500_000, closing=500_000, period=("2026-01-01", "2026-01-31"))
        # Interleaved activity that leaves each account's balance unchanged by EOD
        f.tx("SUBJECT S", "CR", "PAYER P", 1_000, day=4, hour=10, balance=11_000, stmt=sa)
        f.tx("SUBJECT S", "DR", "PAYEE Q", 1_000, day=4, hour=11, balance=10_000, stmt=sa)
        f.tx("SUBJECT S", "CR", "PAYER P", 2_000, day=9, hour=10, balance=502_000, stmt=sb)
        f.tx("SUBJECT S", "DR", "PAYEE Q", 2_000, day=9, hour=11, balance=500_000, stmt=sb)
        prof = build_individual_financial_profile(f.eid("SUBJECT S"), f.statements, f.transactions, f.ents(), [])
        self.assertAlmostEqual(prof["kpis"]["daily_abb"], 510_000, places=2)
        self.assertEqual(prof["monthly_abb"][0]["month"], "2026-01")
        self.assertAlmostEqual(prof["monthly_abb"][0]["abb"], 510_000, places=2)
        by_acc = {r["account_number"]: r["daily_abb"] for r in prof["abb_by_account"]}
        self.assertAlmostEqual(by_acc["A1"], 10_000, places=2)
        self.assertAlmostEqual(by_acc["B1"], 500_000, places=2)
        self.assertEqual(prof["monthly_breakdown"][0]["abb"], prof["monthly_abb"][0]["abb"])


class TestSystemParties(unittest.TestCase):
    def test_nlp10_predicate(self) -> None:
        for nm in ("UNRESOLVED_SUSPENSE / UNCATEGORIZED", "CASH WITHDRAWAL (ATM)", "BANK CHARGES", "BANK INTEREST", "CASH DEPOSIT"):
            self.assertTrue(is_system_entity(nm), nm)
        for nm in ("PRATMESH TRADERS", "CASHIFY PVT LTD", "POOJAN VIG"):
            self.assertFalse(is_system_entity(nm), nm)

    def _unresolved_bridge(self) -> Fixture:
        f = Fixture()
        f.statement("ALPHA")
        f.statement("BRAVO")
        # Neither narration has a counterparty: both resolve to UNRESOLVED_<category>
        f.tx("ALPHA", "DR", None, 9_000, day=1)
        f.tx("BRAVO", "CR", None, 9_000, day=2)
        f.tx("BRAVO", "DR", None, 8_000, day=3)
        f.tx("ALPHA", "CR", None, 8_000, day=4)
        f.tx("ALPHA", "DR", "CASH WITHDRAWAL (ATM)", 1_000, day=5, channel="ATM", category_id=51)
        f.tx("BRAVO", "CR", "CASH DEPOSIT", 1_000, day=6, channel="CASH_CDM", category_id=50)
        return f

    def test_nlp10_resolver_flags_and_engines_do_not_traverse(self) -> None:
        f = self._unresolved_bridge()
        ents, txns = resolve_case_entities(CASE, f.statements, f.transactions)
        by_name = {e.canonical_name: e for e in ents}
        unresolved = [e for e in ents if e.canonical_name.startswith("UNRESOLVED_")]
        self.assertEqual(len(unresolved), 1)
        self.assertTrue(getattr(unresolved[0], "is_system", False))
        self.assertTrue(is_system_entity(unresolved[0]))
        self.assertTrue(is_system_entity(by_name["CASH WITHDRAWAL (ATM)"]))
        self.assertFalse(is_system_entity(by_name["ALPHA"]))

        loops = detect_circular_loops(f.statements, txns, ents, max_cycle_days=30, min_retention_ratio=0.5)
        self.assertEqual(len(loops), 0, "A -> UNRESOLVED -> A must not be reported as a loop")

        tree = build_layering_tree(f.statements, txns, ents, by_name["ALPHA"].entity_id)
        u = _find(tree["root"], unresolved[0].canonical_name)
        self.assertIsNotNone(u, "system party may appear as a sink")
        self.assertEqual(u["children"], [])
        self.assertIsNone(_find(tree["root"], "BRAVO"))

        graph = build_network_graph(f.statements, txns, ents)
        nodes = {n["label"]: n for n in graph["nodes"]}
        un = nodes[unresolved[0].canonical_name]
        self.assertTrue(un["is_system"])
        self.assertEqual(un["betweenness"], 0.0)
        self.assertNotEqual(un["node_role"], "BRIDGE_CONDUIT")
        # ALPHA and BRAVO are only linked via the system node: separate components
        self.assertNotEqual(nodes["ALPHA"]["component_id"], nodes["BRAVO"]["component_id"])


class TestGraphEngine(unittest.TestCase):
    def test_tg10_exit_by_channel_not_substring(self) -> None:
        f = Fixture()
        f.statement("HOLDER H")
        f.transfer("HOLDER H", "PRATMESH TRADERS", 5_000, day=1, channel="UPI", category_id=48)
        f.transfer("HOLDER H", "KOTAK ATM 0042", 2_000, day=2, channel="ATM", category_id=51)
        f.transfer("HOLDER H", "ZEBPAY", 3_000, day=3, channel="IMPS", category_id=37)
        graph = build_network_graph(f.statements, f.transactions, f.ents())
        roles = {n["label"]: n["node_role"] for n in graph["nodes"]}
        self.assertNotEqual(roles["PRATMESH TRADERS"], "EXIT_ENDPOINT")
        self.assertEqual(roles["KOTAK ATM 0042"], "EXIT_ENDPOINT")
        self.assertEqual(roles["ZEBPAY"], "EXIT_ENDPOINT")

    def test_tg11_mirror_dedup_utr_and_tolerance_multiset(self) -> None:
        f = Fixture()
        f.statement("ALPHA")
        f.statement("BRAVO")
        # T+1 NEFT mirror: one transfer
        f.transfer("ALPHA", "BRAVO", 5_000, day=1, channel="NEFT")
        f.tx("BRAVO", "CR", "ALPHA", 5_000, day=2, channel="NEFT")
        # UTR mirror outside +-2 days still dedups
        f.transfer("ALPHA", "BRAVO", 7_000, day=10, channel="NEFT", utr="UTR777")
        f.tx("BRAVO", "CR", "ALPHA", 7_000, day=14, channel="NEFT", utr="UTR777")
        # Multiset: one debit, two same-day credits of the same amount -> two transfers
        f.transfer("ALPHA", "BRAVO", 3_000, day=20)
        f.tx("BRAVO", "CR", "ALPHA", 3_000, day=20)
        f.tx("BRAVO", "CR", "ALPHA", 3_000, day=20, hour=15)
        # Conflicting UTRs are never merged
        f.transfer("ALPHA", "BRAVO", 4_000, day=30, utr="UTR1")
        f.tx("BRAVO", "CR", "ALPHA", 4_000, day=30, utr="UTR2")
        events, _ = extract_directed_transfer_events(f.statements, f.transactions, f.ents())
        by_amt: Dict[float, int] = {}
        for e in events:
            by_amt[e.amount] = by_amt.get(e.amount, 0) + 1
        self.assertEqual(by_amt, {5_000: 1, 7_000: 1, 3_000: 2, 4_000: 2})

    def test_tg08_full_precision_and_exact_brandes(self) -> None:
        nodes = ["a", "b", "c"]
        adj = {"a": {"b": 1.0}, "b": {"c": 1.0}}
        btw = graph_engine._compute_betweenness(nodes, adj)
        self.assertAlmostEqual(btw["b"], 0.5)
        self.assertEqual(btw["a"], 0.0)
        pr = graph_engine._compute_pagerank(nodes, adj)
        self.assertAlmostEqual(sum(pr.values()), 1.0, places=6)
        self.assertTrue(any(abs(v - round(v, 5)) > 0 for v in pr.values()), "PageRank must not be rounded")

    def _random_graph_fixture(self, n_holders: int, per_holder: int, n_outsiders: int, seed: int) -> Fixture:
        rng = random.Random(seed)
        f = Fixture()
        holders = [f"H{i:05d}" for i in range(n_holders)]
        outsiders = [f"O{i:05d}" for i in range(n_outsiders)]
        for h in holders:
            f.statement(h)
        pool = holders + outsiders
        for h in holders:
            for _ in range(per_holder):
                dst = rng.choice(pool)
                if dst != h:
                    f.transfer(h, dst, float(rng.randint(1, 500) * 100), rng.randint(0, 80), rng.randint(0, 23))
        return f

    def test_tg09_percentile_conduit_threshold(self) -> None:
        f = self._random_graph_fixture(60, 6, 0, seed=7)
        graph = build_network_graph(f.statements, f.transactions, f.ents())
        conduits = [n for n in graph["nodes"] if n["has_statement"] and n["node_role"] == "BRIDGE_CONDUIT"]
        self.assertGreater(len(conduits), 0, "percentile threshold must fire on a connected statement graph")
        self.assertLess(len(conduits), len(graph["nodes"]) // 2)
        self.assertIn("conduit_betweenness_threshold", graph["summary"])

    def test_tg04_perf_5k_nodes_50k_edges(self) -> None:
        f = self._random_graph_fixture(5_000, 10, 0, seed=11)
        t0 = time.perf_counter()
        graph = build_network_graph(f.statements, f.transactions, f.ents())
        elapsed = time.perf_counter() - t0
        self.assertGreaterEqual(graph["summary"]["total_nodes"], 4_990)
        self.assertGreaterEqual(graph["summary"]["total_edges"], 49_000)
        self.assertTrue(graph["summary"]["truncated"])
        self.assertLessEqual(len(graph["nodes"]), 1_500)
        self.assertTrue(graph["summary"]["betweenness_sampled"])
        kept = {n["entity_id"] for n in graph["nodes"]}
        for e in graph["edges"]:
            self.assertIn(e["source"], kept)
            self.assertIn(e["target"], kept)
        for n in graph["nodes"]:
            self.assertTrue(0.0 <= n["x"] <= 920.0 and 0.0 <= n["y"] <= 520.0)
        self.assertLess(elapsed, 12.0, f"graph analytics took {elapsed:.1f}s")


class TestLoopDetector(unittest.TestCase):
    def _noisy(self, seed: int) -> Fixture:
        rng = random.Random(seed)
        f = Fixture()
        names = [f"N{i:03d}" for i in range(40)]
        for nm in names:
            f.statement(nm)
        for _ in range(400):
            a, b = rng.sample(names, 2)
            f.transfer(a, b, float(rng.randint(1, 100) * 1_000), rng.randint(0, 90), rng.randint(0, 23))
        return f

    def _plant(self, f: Fixture, path: List[str], amount: float, day: int) -> None:
        for nm in path:
            if nm not in f.entities or not f.entities[nm].has_statement:
                f.statement(nm)
        for i, nm in enumerate(path):
            f.transfer(nm, path[(i + 1) % len(path)], amount * (1 - 0.02 * i), day + i, 9)

    def test_planted_3_and_4_hop_recall(self) -> None:
        for seed in (1, 2, 3):
            f = self._noisy(seed)
            self._plant(f, ["P3A", "P3B", "P3C"], 900_000, 10)
            self._plant(f, ["P4A", "P4B", "P4C", "P4D"], 700_000, 20)
            loops = detect_circular_loops(f.statements, f.transactions, f.ents(), max_cycle_days=30, min_retention_ratio=0.8)
            sigs = {frozenset(lp.entities) for lp in loops}
            self.assertIn(frozenset({"P3A", "P3B", "P3C"}), sigs, f"seed={seed}")
            self.assertIn(frozenset({"P4A", "P4B", "P4C", "P4D"}), sigs, f"seed={seed}")
            self.assertFalse(loops.truncated)
            for lp in loops:
                ts = [h["timestamp"] for h in lp.hops]
                self.assertEqual(ts, sorted(ts))
                self.assertGreaterEqual(lp.retention_ratio, 0.8)

    def test_tf10_backtracking_finds_loop_greedy_misses(self) -> None:
        f = Fixture()
        for nm in ("A", "B", "C"):
            f.statement(nm)
        f.transfer("A", "B", 100_000, day=1)
        f.transfer("B", "C", 70_000, day=2)  # greedy picks this; C->A then fails retention 0.6
        f.transfer("B", "C", 100_000, day=3)
        f.transfer("C", "A", 120_000, day=4)
        loops = detect_circular_loops(f.statements, f.transactions, f.ents(), min_retention_ratio=0.6)
        self.assertEqual(len(loops), 1)
        self.assertEqual([h["amount"] for h in loops[0].hops], [100_000, 100_000, 120_000])

    def test_tf11_max_cycle_length_validated_and_respected(self) -> None:
        f = Fixture()
        self._plant(f, ["Q1", "Q2", "Q3", "Q4", "Q5", "Q6", "Q7"], 50_000, 1)
        st, tx, en = f.statements, f.transactions, f.ents()
        # FR-TRACE-04 (repair): the default cap is 8 hops, so a 7-hop loop is found without raising it
        self.assertEqual(len(detect_circular_loops(st, tx, en)), 1)
        self.assertEqual(len(detect_circular_loops(st, tx, en, max_cycle_length=6)), 0)
        self.assertEqual(len(detect_circular_loops(st, tx, en, max_cycle_length=7)), 1)
        for bad in (1, 9):
            with self.assertRaises(ValueError):
                detect_circular_loops(st, tx, en, max_cycle_length=bad)

    def test_be09_budget_and_cap_truncate(self) -> None:
        rng = random.Random(5)
        f = Fixture()
        names = [f"D{i:02d}" for i in range(30)]
        for nm in names:
            f.statement(nm)
        for _ in range(3_000):
            a, b = rng.sample(names, 2)
            f.transfer(a, b, 10_000.0, rng.randint(0, 5), rng.randint(0, 23))
        st, tx, en = f.statements, f.transactions, f.ents()
        t0 = time.perf_counter()
        res = detect_circular_loops(st, tx, en, max_cycle_length=8, time_budget_s=0.3)
        self.assertLess(time.perf_counter() - t0, 3.0)
        self.assertTrue(res.truncated)
        capped = detect_circular_loops(st, tx, en, max_cycles=5, time_budget_s=5.0)
        self.assertLessEqual(len(capped), 5)
        self.assertTrue(capped.truncated)


class TestBidirectional(unittest.TestCase):
    def test_pairs_exclude_system_parties(self) -> None:
        f = Fixture()
        f.statement("HOLDER H")
        f.statement("HOLDER K")
        f.tx("HOLDER H", "DR", "CASH WITHDRAWAL (ATM)", 1_000, day=1, channel="ATM")
        f.tx("HOLDER H", "CR", "CASH WITHDRAWAL (ATM)", 500, day=2, channel="ATM")
        f.transfer("HOLDER H", "HOLDER K", 2_000, day=3)
        f.transfer("HOLDER K", "HOLDER H", 1_000, day=4)
        pairs = list_all_bidirectional_pairs(f.statements, f.transactions, f.ents())
        names = {(p["entity_a_name"], p["entity_b_name"]) for p in pairs}
        self.assertEqual(len(pairs), 1, names)


if __name__ == "__main__":
    unittest.main()
