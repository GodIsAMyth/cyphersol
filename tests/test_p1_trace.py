"""P1 requirement-gap tests for CypherTrace (roadmap 18 and 20).

TF-03 lazy drill-down, TF-05 root at a specific credit, TF-06/07 per-branch Misc with named members,
TF-13 per-intermediary leakage / date / UTR on loop hops, TF-14 multiple loop instances,
TG-05/06 amount-matched round trips with min/median/max turnaround, TG-18 dual ledger + cumulative series,
TF-09/TG-11 mirror dedup by UTR in the bidirectional view, TG-07 colour-by attributes, TG-12 IFSC/amount filters.
"""

from __future__ import annotations

import os
import random
import sys
import unittest
from typing import List

TESTS_DIR = os.path.abspath(os.path.dirname(__file__))
ROOT_DIR = os.path.abspath(os.path.join(TESTS_DIR, ".."))
for _p in (os.path.join(ROOT_DIR, "src"), TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from test_p0_trace import Fixture, _find, _walk  # noqa: E402

from cyphersol.trace.bidirectional import analyze_bidirectional_flow  # noqa: E402
from cyphersol.trace.graph_engine import COLOUR_BY_LEGEND, build_network_graph, extract_directed_transfer_events  # noqa: E402
from cyphersol.trace.layering import build_layering_tree, expand_layering_node, misc_path_key  # noqa: E402
from cyphersol.trace.loop_detector import detect_circular_loops  # noqa: E402


def _kids_by_name(node: dict) -> dict:
    return {c["entity_name"]: c for c in node["children"]}


def _random_case(seed: int) -> Fixture:
    rng = random.Random(seed)
    f = Fixture()
    holders = [f"HOLDER {i}" for i in range(7)]
    outsiders = [f"OUTSIDER {i}" for i in range(4)]
    for h in holders:
        f.statement(h)
    for _ in range(rng.randint(30, 90)):
        src = rng.choice(holders)
        dst = rng.choice(holders + outsiders)
        if dst == src:
            continue
        amt = float(rng.choice([500, 1_000, 2_500, 7_000, 12_345.67, 50_000]))
        day = rng.randint(0, 40)
        f.transfer(src, dst, amt, day, rng.randint(0, 23))
        if dst in holders and rng.random() < 0.5:
            f.tx(dst, "CR", src, amt, day + rng.choice([0, 1]), rng.randint(0, 23))
    return f


def _assert_fifo(tc: unittest.TestCase, root: dict, msg: str = "") -> None:
    for node in _walk(root):
        kids = node.get("children", [])
        tc.assertLessEqual(sum(c["attributed_amount"] for c in kids), node["attributed_amount"] + 0.01, msg)


class TestLayeringDrillDown(unittest.TestCase):
    def _deep_chain(self) -> Fixture:
        f = Fixture()
        names = [f"HOP {i}" for i in range(9)]
        for nm in names[:-1]:
            f.statement(nm)
        for i in range(len(names) - 1):
            f.transfer(names[i], names[i + 1], 10_000 - 100 * i, day=1 + i)
        return f

    def test_tf03_expand_returns_one_level_beyond_max_depth(self) -> None:
        f = self._deep_chain()
        ents = f.ents()
        root = f.eid("HOP 0")
        tree = build_layering_tree(f.statements, f.transactions, ents, root, max_depth=2)
        deepest = [n for n in _walk(tree["root"]) if n["depth"] == 2]
        self.assertEqual(len(deepest), 1)
        node = deepest[0]
        self.assertEqual(node["children"], [])
        self.assertTrue(node["has_more_children"], "node at max_depth must advertise lazy expansion")
        self.assertEqual(node["path"], [root, f.eid("HOP 1"), f.eid("HOP 2")])
        self.assertEqual(node["path_key"], misc_path_key(node["path"]))
        # drill all the way down one level at a time
        path = node["path"]
        for i in range(3, 9):
            res = expand_layering_node(f.statements, f.transactions, ents, root, path, trace_depth=tree["trace_depth"])
            self.assertEqual(len(res["children"]), 1, i)
            child = res["children"][0]
            self.assertEqual(child["entity_name"], f"HOP {i}")
            self.assertEqual(child["children"], [])
            self.assertEqual(child["depth"], i)
            path = child["path"]
        self.assertFalse(child["has_more_children"])  # HOP 8 is details-only
        # path_key string form is accepted too
        res2 = expand_layering_node(f.statements, f.transactions, ents, root, node["path_key"])
        self.assertEqual(res2["children"][0]["entity_name"], "HOP 3")

    def test_tf03_expand_matches_full_tree_amounts(self) -> None:
        for seed in range(12):
            f = _random_case(seed)
            ents = f.ents()
            root = f.eid("HOLDER 0")
            for mode in ("fifo", "all"):
                tree = build_layering_tree(f.statements, f.transactions, ents, root, max_depth=3, mode=mode)
                for node in _walk(tree["root"]):
                    if node["is_misc"] or node["depth"] >= 3:
                        continue
                    res = expand_layering_node(f.statements, f.transactions, ents, root, node["path"], mode=mode)
                    got = {(c["entity_id"], c["attributed_amount"], c["has_more_children"]) for c in res["children"]}
                    want = {(c["entity_id"], c["attributed_amount"], c["has_more_children"]) for c in node["children"]}
                    self.assertEqual(got, want, f"seed={seed} mode={mode} path={node['path_key']}")
                    if mode == "fifo":
                        self.assertLessEqual(res["children_total"], node["attributed_amount"] + 0.01)

    def test_tf03_expand_validates_path(self) -> None:
        f = self._deep_chain()
        ents = f.ents()
        root = f.eid("HOP 0")
        with self.assertRaises(ValueError):
            expand_layering_node(f.statements, f.transactions, ents, root, [f.eid("HOP 1")])
        with self.assertRaises(ValueError):
            expand_layering_node(f.statements, f.transactions, ents, root, [])
        with self.assertRaises(ValueError):
            expand_layering_node(f.statements, f.transactions, ents, root, [root, "E_NOPE"])

    def test_tf05_root_at_specific_credit(self) -> None:
        f = Fixture()
        for nm in ("SOURCE A", "SOURCE Z", "MULE B"):
            f.statement(nm)
        f.transfer("SOURCE Z", "MULE B", 50_000, day=1)
        f.transfer("MULE B", "PARTY EARLY", 40_000, day=2)  # before the traced credit: excluded
        f.transfer("SOURCE A", "MULE B", 10_000, day=10, utr="UTR10")
        credit = f.tx("MULE B", "CR", "SOURCE A", 10_000, day=10, utr="UTR10")  # mirror leg (absorbed)
        f.transfer("MULE B", "PARTY C", 6_000, day=11)
        f.transfer("MULE B", "PARTY D", 6_000, day=12)
        f.transfer("MULE B", "PARTY LATE", 6_000, day=40)  # outside the 14-day window
        tree = build_layering_tree(f.statements, f.transactions, f.ents(), None, root_tx_id=credit.tx_id)
        r = tree["root"]
        self.assertEqual(r["entity_name"], "MULE B")
        self.assertEqual(r["attributed_amount"], 10_000)
        self.assertEqual(r["tx_ids"], [credit.tx_id])
        kids = _kids_by_name(r)
        self.assertEqual(set(kids), {"PARTY C", "PARTY D"})
        self.assertEqual(kids["PARTY C"]["attributed_amount"], 6_000)
        self.assertEqual(kids["PARTY D"]["attributed_amount"], 4_000)
        self.assertEqual(tree["root_credit"]["tx_id"], credit.tx_id)
        self.assertEqual(tree["root_credit"]["amount"], 10_000)
        # entity mismatch and non-credit are rejected
        with self.assertRaises(ValueError):
            build_layering_tree(f.statements, f.transactions, f.ents(), f.eid("SOURCE A"), root_tx_id=credit.tx_id)
        debit = f.transactions[1]
        with self.assertRaises(ValueError):
            build_layering_tree(f.statements, f.transactions, f.ents(), None, root_tx_id=debit.tx_id)
        with self.assertRaises(ValueError):
            build_layering_tree(f.statements, f.transactions, f.ents(), None, root_tx_id="NOPE")
        # expand works from a credit root as well
        res = expand_layering_node(f.statements, f.transactions, f.ents(), None, [f.eid("MULE B")], root_tx_id=credit.tx_id)
        self.assertEqual({c["entity_name"] for c in res["children"]}, {"PARTY C", "PARTY D"})

    def test_tf05_entity_root_date_range(self) -> None:
        f = Fixture()
        f.statement("ROOT SRC")
        f.transfer("ROOT SRC", "PARTY X", 1_000, day=1)
        f.transfer("ROOT SRC", "PARTY Y", 2_000, day=20)
        tree = build_layering_tree(
            f.statements, f.transactions, f.ents(), f.eid("ROOT SRC"), date_from="2026-01-10", date_to="2026-01-31"
        )
        self.assertEqual(set(_kids_by_name(tree["root"])), {"PARTY Y"})
        self.assertEqual(tree["root"]["attributed_amount"], 2_000)

    def test_tf06_misc_is_per_branch_and_tf07_members_named(self) -> None:
        f = Fixture()
        for nm in ("ROOT SRC", "MULE B", "MULE C"):
            f.statement(nm)
        f.transfer("ROOT SRC", "MULE B", 10_000, day=1)
        f.transfer("ROOT SRC", "MULE C", 10_000, day=1, hour=13)
        f.transfer("MULE B", "SHOP S", 3_000, day=2)
        f.transfer("MULE B", "PARTY P", 2_000, day=2, hour=14)
        f.transfer("MULE C", "SHOP S", 4_000, day=2)
        ents = f.ents()
        root = f.eid("ROOT SRC")
        b_path = [root, f.eid("MULE B"), f.eid("SHOP S")]
        tree = build_layering_tree(f.statements, f.transactions, ents, root, misc_paths=[b_path])
        b = _find(tree["root"], "MULE B")
        c = _find(tree["root"], "MULE C")
        b_kids = _kids_by_name(b)
        self.assertNotIn("SHOP S", b_kids)
        misc = next(k for k in b["children"] if k["is_misc"])
        self.assertEqual(misc["attributed_amount"], 3_000)
        self.assertEqual(misc["misc_count"], 1)
        self.assertEqual(misc["misc_members"][0]["entity_name"], "SHOP S")
        self.assertEqual(misc["misc_members"][0]["attributed_amount"], 3_000)
        self.assertEqual(misc["misc_members"][0]["scope"], "path")
        self.assertEqual(misc["misc_members"][0]["path_key"], misc_path_key(b_path))
        # the other branch still shows SHOP S as a normal node
        self.assertIn("SHOP S", _kids_by_name(c))
        self.assertFalse(any(k["is_misc"] for k in c["children"]))
        self.assertEqual(tree["misc_paths"], [misc_path_key(b_path)])
        # string path form and global entity misc still work
        tree2 = build_layering_tree(f.statements, f.transactions, ents, root, misc_paths=[misc_path_key(b_path)])
        self.assertNotIn("SHOP S", _kids_by_name(_find(tree2["root"], "MULE B")))
        tree3 = build_layering_tree(f.statements, f.transactions, ents, root, misc_entity_ids={f.eid("SHOP S")})
        for nm in ("MULE B", "MULE C"):
            node = _find(tree3["root"], nm)
            misc = next(k for k in node["children"] if k["is_misc"])
            self.assertEqual(misc["misc_members"][0]["scope"], "entity")

    def test_property_fifo_with_credit_root_and_misc_paths(self) -> None:
        for seed in range(20):
            rng = random.Random(1000 + seed)
            f = _random_case(seed)
            ents = f.ents()
            credits = [t for t in f.transactions if t.direction.value == "CR"] if f.transactions else []
            if credits:
                cr = rng.choice(credits)
                tree = build_layering_tree(f.statements, f.transactions, ents, None, root_tx_id=cr.tx_id, max_depth=5)
                self.assertLessEqual(
                    sum(c["attributed_amount"] for c in tree["root"]["children"]), float(cr.credit) + 0.01, f"seed={seed}"
                )
                _assert_fifo(self, tree["root"], f"seed={seed}")
            root = f.eid("HOLDER 0")
            base = build_layering_tree(f.statements, f.transactions, ents, root, max_depth=4)
            nodes = [n for n in _walk(base["root"]) if n["depth"] > 0 and not n["is_misc"]]
            paths = [n["path"] for n in rng.sample(nodes, min(3, len(nodes)))]
            tree = build_layering_tree(f.statements, f.transactions, ents, root, max_depth=4, misc_paths=paths)
            _assert_fifo(self, tree["root"], f"seed={seed}")
            self.assertAlmostEqual(
                sum(c["attributed_amount"] for c in tree["root"]["children"]),
                sum(c["attributed_amount"] for c in base["root"]["children"]),
                places=2,
            )


class TestLoopDetail(unittest.TestCase):
    def test_tf13_leakage_date_utr_per_hop(self) -> None:
        f = Fixture()
        for nm in ("A", "B", "C"):
            f.statement(nm)
        f.transfer("A", "B", 100_000, day=1, utr="U1")
        f.transfer("B", "C", 95_000, day=2, utr="U2")
        f.transfer("C", "A", 90_000, day=3, utr="U3")
        loops = detect_circular_loops(f.statements, f.transactions, f.ents(), min_retention_ratio=0.5)
        self.assertEqual(len(loops), 1)
        lp = loops[0].to_dict()
        self.assertEqual([h["utr"] for h in lp["hops"]], ["U1", "U2", "U3"])
        self.assertEqual([h["date"] for h in lp["hops"]], ["2026-01-02", "2026-01-03", "2026-01-04"])
        self.assertEqual([h["leakage"] for h in lp["hops"]], [None, 5_000, 5_000])
        self.assertEqual(lp["hops"][1]["hours_since_previous_hop"], 24.0)
        self.assertEqual([x["entity_name"] for x in lp["intermediary_leakage"]], ["B", "C"])
        self.assertEqual(lp["intermediary_leakage"][0]["leakage_pct"], 5.0)
        self.assertEqual(lp["total_leakage"], 10_000)
        self.assertEqual(lp["leakage_pct"], 10.0)
        self.assertEqual(lp["hops"][0]["from_entity_id"], f.eid("A"))

    def test_tf14_multiple_instances(self) -> None:
        f = Fixture()
        for nm in ("A", "B", "C"):
            f.statement(nm)
        for week in range(3):
            d = 1 + 7 * week
            f.transfer("A", "B", 100_000, day=d)
            f.transfer("B", "C", 99_000, day=d + 1)
            f.transfer("C", "A", 98_000, day=d + 2)
        loops = detect_circular_loops(f.statements, f.transactions, f.ents(), max_cycle_days=3)
        self.assertEqual(len(loops), 1)
        lp = loops[0]
        self.assertEqual(lp.instance_count, 3)
        self.assertEqual(lp.disjoint_instance_count, 3)
        self.assertEqual(len(lp.instances), 3)
        self.assertEqual(lp.start_date, "2026-01-02")  # representative = earliest
        self.assertEqual([i["start_date"] for i in lp.instances], ["2026-01-02", "2026-01-09", "2026-01-16"])
        self.assertEqual(lp.total_instance_volume, 300_000)
        capped = detect_circular_loops(f.statements, f.transactions, f.ents(), max_cycle_days=3, max_instances_per_loop=2)
        self.assertEqual(capped[0].instance_count, 3)
        self.assertEqual(len(capped[0].instances), 2)
        self.assertTrue(capped[0].instances_truncated)
        self.assertEqual(capped[0].instances[0]["start_date"], "2026-01-02")
        with self.assertRaises(ValueError):
            detect_circular_loops(f.statements, f.transactions, f.ents(), max_instances_per_loop=0)


class TestBidirectionalDetail(unittest.TestCase):
    def _pair(self) -> Fixture:
        f = Fixture()
        f.statement("ALPHA")
        f.statement("BETA")
        f.transfer("ALPHA", "BETA", 100_000, day=1, hour=10)
        f.transfer("BETA", "ALPHA", 5_000, day=1, hour=12)  # reversal but not amount-matched
        f.transfer("BETA", "ALPHA", 98_000, day=2, hour=10)  # amount-matched return after 24 h
        f.transfer("BETA", "ALPHA", 50_000, day=10, hour=10)
        f.transfer("ALPHA", "BETA", 49_000, day=14, hour=10)  # B-anchored round trip, 96 h
        return f

    def test_tg05_06_amount_matched_round_trips(self) -> None:
        f = self._pair()
        res = analyze_bidirectional_flow(f.statements, f.transactions, f.ents(), f.eid("ALPHA"), f.eid("BETA"))
        a_trips = [t for t in res.round_trips if t["anchor"] == "A"]
        b_trips = [t for t in res.round_trips if t["anchor"] == "B"]
        self.assertEqual(len(a_trips), 1)
        self.assertEqual(a_trips[0]["back_amount"], 98_000)
        self.assertEqual(a_trips[0]["turnaround_hours"], 24.0)
        self.assertEqual(res.rapid_round_trip_count, 1)
        self.assertEqual(len(b_trips), 1)
        self.assertEqual(b_trips[0]["turnaround_hours"], 96.0)
        self.assertEqual(res.rapid_round_trip_count_b, 0)
        st = res.round_trip_stats["anchor_a"]
        self.assertEqual((st["min_hours"], st["median_hours"], st["max_hours"]), (24.0, 24.0, 24.0))
        # direction-flip turnaround keeps min/median and now has max
        self.assertEqual(res.min_turnaround_hours, 2.0)
        self.assertEqual(res.max_turnaround_hours, 96.0)
        self.assertEqual(res.direction_reversal_count, 2)
        # configurable windows and tolerance
        tight = analyze_bidirectional_flow(
            f.statements, f.transactions, f.ents(), f.eid("ALPHA"), f.eid("BETA"), rapid_window_hours=12
        )
        self.assertEqual(tight.rapid_round_trip_count, 0)
        strict = analyze_bidirectional_flow(
            f.statements, f.transactions, f.ents(), f.eid("ALPHA"), f.eid("BETA"), amount_tolerance=0.01
        )
        self.assertEqual(len(strict.round_trips), 0)
        short = analyze_bidirectional_flow(
            f.statements, f.transactions, f.ents(), f.eid("ALPHA"), f.eid("BETA"), round_trip_window_hours=12
        )
        self.assertEqual(len(short.round_trips), 0)
        for bad in ({"amount_tolerance": 2}, {"rapid_window_hours": -1}, {"round_trip_window_hours": 0}):
            with self.assertRaises(ValueError):
                analyze_bidirectional_flow(f.statements, f.transactions, f.ents(), f.eid("ALPHA"), f.eid("BETA"), **bad)

    def test_tg18_dual_ledger_and_cumulative_series(self) -> None:
        f = self._pair()
        res = analyze_bidirectional_flow(f.statements, f.transactions, f.ents(), f.eid("ALPHA"), f.eid("BETA")).to_dict()
        self.assertEqual(len(res["ledger_a_to_b"]), 2)
        self.assertEqual(len(res["ledger_b_to_a"]), 3)
        self.assertEqual(res["ledger_a_to_b"][-1]["cumulative"], res["total_a_to_b"])
        self.assertEqual(res["ledger_b_to_a"][-1]["cumulative"], res["total_b_to_a"])
        series = res["cumulative_series"]
        self.assertEqual(len(series), 5)
        for prev, cur in zip(series, series[1:]):
            self.assertGreaterEqual(cur["cumulative_a_to_b"], prev["cumulative_a_to_b"])
            self.assertGreaterEqual(cur["cumulative_b_to_a"], prev["cumulative_b_to_a"])
        self.assertEqual(series[-1]["net_a_to_b"], res["net_flow"])
        daily = res["daily_series"]
        self.assertEqual([d["date"] for d in daily], sorted(d["date"] for d in daily))
        self.assertEqual(daily[-1]["cumulative_a_to_b"], res["total_a_to_b"])
        self.assertEqual(res["transactions"][-1]["cumulative_b_to_a"], res["total_b_to_a"])
        # date filter
        part = analyze_bidirectional_flow(
            f.statements, f.transactions, f.ents(), f.eid("ALPHA"), f.eid("BETA"), date_from="2026-01-05"
        )
        self.assertEqual(part.tx_count_a_to_b + part.tx_count_b_to_a, 2)

    def test_tf09_mirror_dedup_by_utr_in_bidirectional(self) -> None:
        f = Fixture()
        f.statement("ALPHA")
        f.statement("BETA")
        f.transfer("ALPHA", "BETA", 25_000, day=1, utr="NEFT0001")
        f.tx("BETA", "CR", "ALPHA", 25_000, day=2, utr="NEFT0001")  # T+1
        f.transfer("ALPHA", "BETA", 30_000, day=5, utr="RTGS0002")
        f.tx("BETA", "CR", "ALPHA", 30_000, day=10, utr="RTGS0002")  # T+5, matched only by UTR
        f.transfer("ALPHA", "BETA", 7_000, day=20)
        f.tx("BETA", "CR", "ALPHA", 7_000, day=20)
        f.tx("BETA", "CR", "ALPHA", 7_000, day=21)  # a genuine second transfer (no debit leg to absorb it)
        res = analyze_bidirectional_flow(f.statements, f.transactions, f.ents(), f.eid("ALPHA"), f.eid("BETA"))
        self.assertEqual(res.tx_count_a_to_b, 4)
        self.assertEqual(res.total_a_to_b, 25_000 + 30_000 + 7_000 + 7_000)
        self.assertEqual(res.tx_count_b_to_a, 0)


class TestGraphFiltersAndColour(unittest.TestCase):
    def _case(self) -> Fixture:
        f = Fixture()
        st_h = f.statement("HOLDER H")
        st_h.ifsc_code = "HDFC0000123"
        st_k = f.statement("HOLDER K")
        st_k.ifsc_code = "ICIC0000456"
        t1 = f.transfer("HOLDER H", "PARTY SBI", 10_000, day=1)
        t1.counterparty_ifsc = "SBIN0001234"
        t2 = f.transfer("HOLDER H", "PARTY AXIS", 20_000, day=2)
        t2.counterparty_ifsc = "UTIB0000999"
        f.transfer("HOLDER K", "PARTY PLAIN", 500_000, day=3)
        f.transfer("HOLDER H", "HOLDER K", 1_000, day=4)
        return f

    def test_tg12_ifsc_and_amount_filters(self) -> None:
        f = self._case()
        g = build_network_graph(f.statements, f.transactions, f.ents(), ifsc_filter="sbin")
        self.assertEqual({(e["source_name"], e["target_name"]) for e in g["edges"]}, {("HOLDER H", "PARTY SBI")})
        self.assertEqual(g["summary"]["filters"]["ifsc"], ["SBIN"])
        g2 = build_network_graph(f.statements, f.transactions, f.ents(), ifsc_filter="ICIC, SBIN0001234")
        self.assertEqual(
            {e["target_name"] for e in g2["edges"]}, {"PARTY SBI", "PARTY PLAIN"}
        )  # statement IFSC of HOLDER K matches ICIC
        g3 = build_network_graph(f.statements, f.transactions, f.ents(), min_amount=5_000, max_amount=100_000)
        self.assertEqual({e["target_name"] for e in g3["edges"]}, {"PARTY SBI", "PARTY AXIS"})
        g4 = build_network_graph(f.statements, f.transactions, f.ents(), date_from="2026-01-04", date_to="2026-01-05")
        self.assertEqual({e["target_name"] for e in g4["edges"]}, {"PARTY PLAIN", "HOLDER K"})
        evs, _ = extract_directed_transfer_events(f.statements, f.transactions, f.ents(), ifsc_filter="ALL")
        self.assertEqual(len(evs), 4)

    def test_tg07_colour_by_attributes(self) -> None:
        f = self._case()
        mule = {f.eid("HOLDER H"): {"risk_tier": "Critical", "composite_score": 91}}
        g = build_network_graph(f.statements, f.transactions, f.ents(), mule_scores=mule)
        self.assertEqual(set(g["summary"]["colour_by"]), {"role", "statement", "risk_tier", "betweenness"})
        by_name = {n["label"]: n for n in g["nodes"]}
        for n in g["nodes"]:
            keys = n["colour_keys"]
            for scheme, value in keys.items():
                self.assertIn(value, COLOUR_BY_LEGEND[scheme], (n["label"], scheme))
            self.assertGreaterEqual(n["betweenness_percentile"], 0.0)
            self.assertLessEqual(n["betweenness_percentile"], 1.0)
        self.assertEqual(by_name["HOLDER H"]["colour_keys"]["risk_tier"], "CRITICAL")
        self.assertEqual(by_name["HOLDER K"]["colour_keys"]["risk_tier"], "LOW_RISK")
        self.assertEqual(by_name["PARTY SBI"]["colour_keys"]["statement"], "DETAILS_ONLY")
        self.assertEqual(by_name["HOLDER H"]["colour_keys"]["statement"], "HAS_STATEMENT")
        self.assertEqual(by_name["HOLDER K"]["colour_keys"]["betweenness"], "top")  # H -> K -> PLAIN
        self.assertEqual(by_name["PARTY SBI"]["colour_keys"]["betweenness"], "none")


if __name__ == "__main__":
    unittest.main()
