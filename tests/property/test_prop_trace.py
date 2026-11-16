"""TEST-01 / TEST-03 / TG-26: FIFO attribution, NetFlow antisymmetry and planted-loop recall properties."""

from __future__ import annotations

import random
import unittest
from collections import defaultdict
from decimal import Decimal

from ._harness import CaseBuilder, money, random_case, random_property

from cyphersol.nlp.entity_resolver import is_system_entity  # noqa: E402
from cyphersol.trace.bidirectional import analyze_bidirectional_flow, list_all_bidirectional_pairs  # noqa: E402
from cyphersol.trace.graph_engine import build_network_graph  # noqa: E402
from cyphersol.trace.layering import build_layering_tree  # noqa: E402
from cyphersol.trace.loop_detector import detect_circular_loops  # noqa: E402

TOL = 0.011


def _walk(node):
    yield node
    for c in node.get("children", []):
        yield from _walk(c)


class FifoProperties(unittest.TestCase):
    @random_property(30)
    def test_fifo_attributed_never_exceeds_credit_at_any_node(self, rng: random.Random) -> None:
        b, holders = random_case(rng)
        ents = b.ents()
        credits_in = defaultdict(float)  # every credit any entity could have received, either leg
        for t in b.transactions:
            if t.credit > 0:
                credits_in[b.entities[t.source_holder_name].entity_id] += float(t.credit)
            elif t.resolved_entity_id:
                credits_in[t.resolved_entity_id] += float(t.debit)
        root = b.eid(rng.choice(list(b.entities)))
        tree = build_layering_tree(
            b.statements, b.transactions, ents, root, window_days=rng.choice([1, 3, 7, 14, 30]),
            max_depth=rng.randint(2, 6), mode=rng.choice(["fifo", "all"]) if rng.random() < 0.2 else None,
        )
        fifo = tree.get("mode", "fifo") != "all"
        r = tree["root"]
        for node in _walk(r):
            kids = node.get("children", [])
            if fifo:
                self.assertLessEqual(sum(c["attributed_amount"] for c in kids), node["attributed_amount"] + TOL)
            if node["depth"] > 0:
                self.assertGreater(node["attributed_amount"], 0)
                self.assertLessEqual(node["attributed_amount"], node["debit_amount"] + TOL)
                if fifo and node.get("role") != "MISC":
                    # a node can never be attributed more than it was ever credited in the whole case
                    self.assertLessEqual(node["attributed_amount"], credits_in[node["entity_id"]] + TOL, node["entity_name"])


class NetFlowProperties(unittest.TestCase):
    @random_property(25)
    def test_netflow_antisymmetric_and_consistent(self, rng: random.Random) -> None:
        b, holders = random_case(rng)
        ents = b.ents()
        names = list(b.entities)
        for _ in range(4):
            a, c = rng.sample(names, 2)
            ab = analyze_bidirectional_flow(b.statements, b.transactions, ents, b.eid(a), b.eid(c))
            ba = analyze_bidirectional_flow(b.statements, b.transactions, ents, b.eid(c), b.eid(a))
            self.assertAlmostEqual(ab.net_flow, -ba.net_flow, places=2)
            self.assertAlmostEqual(ab.total_a_to_b, ba.total_b_to_a, places=2)
            self.assertAlmostEqual(ab.net_flow, ab.total_a_to_b - ab.total_b_to_a, places=2)
            self.assertEqual(ab.tx_count_a_to_b, ba.tx_count_b_to_a)
            self.assertGreaterEqual(min(ab.total_a_to_b, ab.total_b_to_a), 0.0)
        for p in list_all_bidirectional_pairs(b.statements, b.transactions, ents):
            pd = p if isinstance(p, dict) else p.to_dict()
            if "net_flow" in pd and "total_a_to_b" in pd:
                self.assertAlmostEqual(pd["net_flow"], pd["total_a_to_b"] - pd["total_b_to_a"], places=2)

    @random_property(15)
    def test_graph_node_flows_balance(self, rng: random.Random) -> None:
        b, _holders = random_case(rng)
        g = build_network_graph(b.statements, b.transactions, b.ents())
        edge_total = sum(float(e.get("total_amount", e.get("weight", 0)) or 0) for e in g["edges"])
        out_total = sum(float(n.get("total_outflow", 0) or 0) for n in g["nodes"])
        in_total = sum(float(n.get("total_inflow", 0) or 0) for n in g["nodes"])
        if not g["summary"].get("truncated") and out_total and in_total:
            # every edge is one sender's outflow and one receiver's inflow: the graph conserves value
            self.assertAlmostEqual(out_total, in_total, delta=0.05 * max(1.0, out_total) / 100)
            self.assertAlmostEqual(edge_total, out_total, delta=0.05 * max(1.0, out_total) / 100)


class PlantedLoopRecall(unittest.TestCase):
    MAX_DAYS = 30
    MIN_RET = 0.8

    def _noise(self, rng: random.Random) -> CaseBuilder:
        b = CaseBuilder()
        names = [f"N{i:03d}" for i in range(rng.randint(15, 40))]
        for nm in names:
            b.statement(nm)
        for _ in range(rng.randint(80, 300)):
            a, c = rng.sample(names, 2)
            b.tx(a, "DR", c, Decimal(rng.randint(1, 100) * 1000), rng.randint(0, 120), rng.randint(0, 23))
        return b

    @random_property(15)
    def test_planted_loops_are_always_found(self, rng: random.Random) -> None:
        b = self._noise(rng)
        noisy = [e for e in b.entities]
        planted = []
        for k in range(rng.randint(1, 3)):
            hops = rng.randint(2, 6)
            # half the loops reuse busy noise accounts as intermediaries
            if rng.random() < 0.5:
                path = rng.sample(noisy, hops)
            else:
                path = [f"LOOP{k}_{i}" for i in range(hops)]
                for nm in path:
                    b.statement(nm)
            amount = Decimal(rng.randint(200, 2000) * 1000)
            leak = rng.uniform(0.0, (1 - self.MIN_RET) / hops)
            start = rng.randint(0, 80)
            span = rng.randint(0, self.MAX_DAYS - 1)
            days = sorted(start + rng.randint(0, span) for _ in range(hops))
            for i, nm in enumerate(path):
                amt = (amount * Decimal(str(1 - leak * i))).quantize(Decimal("0.01"))
                b.tx(nm, "DR", path[(i + 1) % hops], amt, days[i], 8 + i, channel="NEFT")
            planted.append(frozenset(path))
        loops = detect_circular_loops(b.statements, b.transactions, b.ents(), max_cycle_days=self.MAX_DAYS,
                                      min_retention_ratio=self.MIN_RET, max_cycle_length=6, time_budget_s=60)
        self.assertFalse(loops.truncated, loops.truncation_reason)
        found = {frozenset(lp.entities) for lp in loops}
        for sig in planted:
            self.assertIn(sig, found)
        for lp in loops:
            ts = [h["timestamp"] for h in lp.hops]
            self.assertEqual(ts, sorted(ts))
            self.assertGreaterEqual(lp.retention_ratio + 1e-9, self.MIN_RET)
            self.assertLessEqual(lp.duration_hours, self.MAX_DAYS * 24 + 24)
            self.assertTrue(2 <= lp.hop_count <= 6)
            self.assertEqual(len(set(lp.entity_ids)), lp.hop_count)
            self.assertFalse(any(is_system_entity(n) for n in lp.entities))


if __name__ == "__main__":
    unittest.main()
