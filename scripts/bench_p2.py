#!/usr/bin/env python3
"""P2 performance benchmark (SRS NFR-PERF-01).

Generates a reproducible synthetic case (default 100 statements x 1,000 rows = 100k transactions), ingests it
through ``CypherApplicationService`` and times the interactive analytics endpoints cold and warm, plus the
exports. Results are printed as a table and optionally written as JSON.

    python scripts/bench_p2.py                       # 100 x 1000
    python scripts/bench_p2.py --files 20 --rows 500 # quick run
    python scripts/bench_p2.py --json out.json --profile-ingest

Targets: ingest < 60 s; warm graph / layering / profile / loops < 500 ms; loops and exports always finish.
The generator is seeded, so the same arguments always produce byte-identical statement files.
"""

from __future__ import annotations

import argparse
import cProfile
import csv
import datetime as dt
import io
import json
import os
import pstats
import random
import shutil
import sys
import tempfile
import time
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

FIRST = ["AARAV", "BHAVNA", "CHIRAG", "DIVYA", "EKANSH", "FARHAN", "GAURI", "HARSH", "ISHITA", "JATIN",
         "KAVYA", "LALIT", "MEERA", "NIKHIL", "OMKAR", "PALLAVI", "RUCHI", "SAMEER", "TANVI", "VARUN"]
LAST = ["AGARWAL", "BANERJEE", "CHAUHAN", "DESHMUKH", "ELANGO", "FADNAVIS", "GHOSH", "HEGDE", "IYER", "JOSHI",
        "KULKARNI", "MENON", "NAIR", "PATIL", "REDDY", "SHETTY", "THAKUR", "VERMA", "YADAV", "ZAVERI"]
BANKS = [("SBI", "SBIN"), ("HDFC", "HDFC"), ("ICICI", "ICIC"), ("AXIS", "UTIB"), ("KOTAK", "KKBK"),
         ("PNB", "PUNB"), ("YES", "YESB")]
MERCHANTS = ["SWIGGY", "ZOMATO", "AMAZON PAY", "FLIPKART", "BIGBASKET", "UBER INDIA", "OLA CABS", "IRCTC",
             "BESCOM ELECTRICITY", "AIRTEL POSTPAID", "JIO RECHARGE", "HP PETROL PUMP", "APOLLO PHARMACY",
             "DMART", "MAKEMYTRIP", "NETFLIX", "LIC PREMIUM", "ZERODHA BROKING", "GROWW MF SIP", "TATA POWER"]
VPA_SUFFIX = ["okhdfcbank", "oksbi", "okicici", "ybl", "paytm", "okaxis"]


def build_case(n_files: int, rows_per_file: int, seed: int = 2026) -> List[Tuple[str, bytes, None]]:
    """Seeded synthetic statements: P2P UPI/NEFT/IMPS transfers between holders (so the graph, loops and
    layering have real structure), merchant spends, salary, ATM, charges, EMI and a few planted loops."""
    rng = random.Random(seed)
    holders = [f"{f} {l}" for l in LAST for f in FIRST][:n_files]
    externals = [f"{f} {l}" for f in reversed(FIRST) for l in reversed(LAST)][: max(50, n_files * 3)]
    ledgers: Dict[str, List[list]] = {h: [] for h in holders}
    base = dt.datetime(2025, 4, 1, 9)
    days = 365
    ref = [0]

    def nref() -> str:
        ref[0] += 1
        return f"{ref[0]:012d}"

    def when() -> dt.datetime:
        return base + dt.timedelta(days=rng.randint(0, days - 1), minutes=rng.randint(0, 600))

    def transfer(src: str, dst: str, amount: Decimal, t: dt.datetime) -> None:
        r = nref()
        rail = rng.choice(("UPI", "NEFT", "IMPS"))
        if rail == "UPI":
            vs = dst.split()[0].lower() + "@" + rng.choice(VPA_SUFFIX)
            vd = src.split()[0].lower() + "@" + rng.choice(VPA_SUFFIX)
            ledgers[src].append([t, f"UPI/DR/{r}/{dst}/{vs}/Payment", r, amount, Decimal(0)])
            ledgers[dst].append([t + dt.timedelta(minutes=1), f"UPI/CR/{r}/{src}/{vd}/Payment", r, Decimal(0), amount])
        else:
            ledgers[src].append([t, f"{rail}/DR/{r}/{dst}", r, amount, Decimal(0)])
            ledgers[dst].append([t + dt.timedelta(minutes=5), f"{rail}/CR/{r}/{src}", r, Decimal(0), amount])

    def money(lo: int, hi: int) -> Decimal:
        return Decimal(rng.randint(lo, hi)) + Decimal(rng.randint(0, 99)) / 100

    # Inter-holder transfers fill ~30% of rows (each transfer writes 2 rows).
    n_internal = int(n_files * rows_per_file * 0.15) if n_files >= 2 else 0
    for _ in range(n_internal):
        a, b = rng.sample(holders, 2)
        transfer(a, b, money(500, 40000), when())
    # Planted loops (3 and 4 hops).
    for k in range(min(5, max(1, n_files // 20))):
        size = 3 + (k % 2)
        if size > n_files:
            continue
        ring = rng.sample(holders, size)
        start = base + dt.timedelta(days=30 + 50 * k)
        amt = Decimal(500000 + 10000 * k)
        for i, nm in enumerate(ring):
            transfer(nm, ring[(i + 1) % size], amt - Decimal(3000 * i), start + dt.timedelta(days=i))

    files = []
    for i, h in enumerate(holders):
        bank, ifsc_prefix = BANKS[i % len(BANKS)]
        rows = ledgers[h]
        # Salary monthly, then fill the remainder with external / merchant / cash rows.
        for m in range(12):
            r = nref()
            rows.append([base + dt.timedelta(days=30 * m), f"NEFT/CR/{r}/ACME SOFTWARE PVT LTD SALARY", r,
                         Decimal(0), Decimal(85000)])
        while len(rows) < rows_per_file:
            r = nref()
            t = when()
            kind = rng.random()
            if kind < 0.40:
                m = rng.choice(MERCHANTS)
                rows.append([t, f"UPI/DR/{r}/{m}/{m.split()[0].lower()}@ybl/Pay", r, money(50, 4000), Decimal(0)])
            elif kind < 0.62:
                ext = rng.choice(externals)
                vpa = ext.split()[0].lower() + "@" + rng.choice(VPA_SUFFIX)
                rows.append([t, f"UPI/DR/{r}/{ext}/{vpa}/Sent", r, money(100, 9000), Decimal(0)])
            elif kind < 0.80:
                ext = rng.choice(externals)
                vpa = ext.split()[0].lower() + "@" + rng.choice(VPA_SUFFIX)
                rows.append([t, f"UPI/CR/{r}/{ext}/{vpa}/Recd", r, Decimal(0), money(100, 9000)])
            elif kind < 0.86:
                rows.append([t, f"ATM WDL/{r}/MUMBAI ANDHERI", r, Decimal(rng.choice((500, 1000, 2000, 5000))), Decimal(0)])
            elif kind < 0.90:
                rows.append([t, f"NACH/DR/{r}/HDFC HOME LOAN EMI", r, Decimal(24500), Decimal(0)])
            elif kind < 0.93:
                rows.append([t, f"SMS ALERT CHARGES QTR {r}", r, Decimal("17.70"), Decimal(0)])
            elif kind < 0.96:
                rows.append([t, f"CASH DEPOSIT BY SELF {r}", r, Decimal(0), money(1000, 20000)])
            else:
                ext = rng.choice(externals)
                rows.append([t, f"IMPS/P2A/{r}/{ext}/XXXX{rng.randint(1000, 9999)}", r, money(1000, 30000), Decimal(0)])
        rows = sorted(rows, key=lambda x: (x[0], x[2]))
        bal = Decimal("5000000.00")
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"])
        for t, narr, rr, dr, cr in rows:
            bal += cr - dr
            w.writerow([t.strftime("%d/%m/%Y %H:%M:%S"), narr, rr, f"{dr:.2f}", f"{cr:.2f}", f"{bal:.2f}"])
        acct = f"{5500000000 + i * 7919:012d}"
        name = f"BENCH_{h.replace(' ', '-')}_{bank}_{acct}.csv"
        files.append((name, out.getvalue().encode("utf-8"), None))
    return files


def timed(fn: Callable[[], Any]) -> Tuple[float, Any]:
    t0 = time.perf_counter()
    out = fn()
    return time.perf_counter() - t0, out


def json_size(obj: Any) -> int:
    return len(json.dumps(obj, default=str))


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--files", type=int, default=100)
    ap.add_argument("--rows", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=2026)
    ap.add_argument("--json", help="write results as JSON to this path")
    ap.add_argument("--profile-ingest", action="store_true", help="print the top cProfile entries for ingest")
    ap.add_argument("--skip-exports", action="store_true")
    ap.add_argument("--ingest-only", action="store_true")
    ap.add_argument("--reuse-db", help="vault path: ingest into it if missing, else skip ingest and reuse it")
    ap.add_argument("--db", choices=("file", "memory"), default="file")
    args = ap.parse_args(argv)

    os.environ.pop("GEMINI_API_KEY", None)
    os.environ.pop("ANTHROPIC_API_KEY", None)
    os.environ.pop("OPENAI_API_KEY", None)
    from cyphersol.service import CypherApplicationService

    t_gen, files = timed(lambda: build_case(args.files, args.rows, args.seed))
    total_rows = sum(f[1].count(b"\n") - 1 for f in files)
    tmp = tempfile.mkdtemp(prefix="cyphersol-bench-")
    results: Dict[str, Any] = {
        "files": args.files, "rows_per_file": args.rows, "total_rows": total_rows, "seed": args.seed,
        "cpu_count": os.cpu_count(), "python": sys.version.split()[0], "generate_s": round(t_gen, 3),
        "timings": {},
    }
    timings = results["timings"]
    try:
        db = ":memory:" if args.db == "memory" else os.path.join(tmp, "bench.db")
        reuse = bool(args.reuse_db and os.path.exists(args.reuse_db))
        if args.reuse_db:
            db = args.reuse_db
        svc = CypherApplicationService(db, os.path.join(tmp, "samples"), seed_demo=False)
        prof = cProfile.Profile() if args.profile_ingest else None
        if reuse:
            cid = next(c["case_id"] for c in svc.list_cases() if c.get("case_name", c.get("title")) == "Bench"
                       or "Bench" in str(c))
        else:
            cid = svc.create_case("Bench", "cypher_trace", "Bench")["case_id"]
            if prof:
                prof.enable()
            t, res = timed(lambda: svc.ingest_statements(cid, files, execution_mode="heuristics"))
            if prof:
                prof.disable()
            timings["ingest"] = {"s": t, "files_ok": res.get("files_ok"),
                                 "rows": res.get("total_case_transactions"), "stage_timings": res.get("timings")}

        def run(name: str, fn: Callable[[], Any], warm: bool = True) -> None:
            t1, out = timed(fn)
            entry: Dict[str, Any] = {"cold_s": t1, "bytes": json_size(out) if not isinstance(out, (bytes, str)) else len(out)}
            if warm:
                t2, out = timed(fn)
                entry["warm_s"] = t2
                if not isinstance(out, (bytes, str)):
                    # what the HTTP server adds on top of the service call (json.dumps + utf-8 encode)
                    entry["warm_http_s"] = t2 + timed(
                        lambda: json.dumps(out, ensure_ascii=False, allow_nan=False, default=str).encode("utf-8")
                    )[0]
            if isinstance(out, dict) and "truncated" in out:
                entry["truncated"] = out.get("truncated")
            timings[name] = entry

        if args.ingest_only:
            return _report(args, results, timings, prof)
        run("overview", lambda: svc.get_case_overview(cid))
        run("transactions_page", lambda: svc.list_transactions(cid, offset=5000, limit=200))
        run("mules", lambda: svc.get_mule_assessments(cid))
        run("graph", lambda: svc.get_network_graph(cid))
        run("layering", lambda: svc.get_layering(cid))
        run("profile", lambda: svc.get_financial_profile(cid))
        run("loops", lambda: svc.get_loops_report(cid))
        run("bidirectional", lambda: svc.get_bidirectional(cid))
        if not args.skip_exports:
            run("export_xlsx", lambda: svc.export_xlsx(cid), warm=False)
            run("export_dossier_html", lambda: svc.export_dossier_html(cid), warm=False)
        if hasattr(svc, "warm_case_analysis"):
            # After a one-row mutation: background re-warm (incremental snapshot + default analytics), then the
            # first graph request is a cache hit.
            tx_id = svc.list_transactions(cid, limit=1)["transactions"][0]["tx_id"]
            svc.toggle_duplicate_ignore(cid, tx_id)
            t, out = timed(lambda: svc.warm_case_analysis(cid))
            timings["rewarm_after_mutation"] = {"cold_s": t, "steps": out.get("timings")}
            run("graph_after_rewarm", lambda: svc.get_network_graph(cid))
            svc.toggle_duplicate_ignore(cid, tx_id)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _report(args, results, timings, prof)


def _report(args: argparse.Namespace, results: Dict[str, Any], timings: Dict[str, Any], prof: Any) -> int:
    total_rows = results["total_rows"]
    print(f"Synthetic case: {args.files} statements, {total_rows} rows, cpu={os.cpu_count()}, py={results['python']}")
    print(f"{'step':<22}{'cold s':>10}{'warm s':>10}{'+json s':>10}{'KB':>10}  note")
    for name, e in timings.items():
        cold = e.get("cold_s", e.get("s"))
        warm = e.get("warm_s")
        wj = e.get("warm_http_s")
        note = ""
        if "truncated" in e:
            note = f"truncated={e['truncated']}"
        if name == "ingest":
            note = f"files_ok={e['files_ok']} rows={e['rows']}"
        kb = f"{e['bytes'] / 1024:.0f}" if "bytes" in e else ""
        print(f"{name:<22}{cold:>10.3f}{(f'{warm:.3f}' if warm is not None else ''):>10}"
              f"{(f'{wj:.3f}' if wj is not None else ''):>10}{kb:>10}  {note}")
    stages = timings.get("ingest", {}).get("stage_timings") or {}
    if stages:
        print("ingest stages: " + ", ".join(f"{k}={v:.2f}s" for k, v in stages.items()))
    if prof:
        s = io.StringIO()
        pstats.Stats(prof, stream=s).sort_stats("cumulative").print_stats(35)
        print(s.getvalue())
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2, default=str)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
