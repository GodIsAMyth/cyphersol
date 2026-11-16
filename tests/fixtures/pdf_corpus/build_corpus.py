"""Rebuild the third-party-producer PDF regression corpus (PDF-28).

The PDFs in this folder are NOT written by CypherSOL's own generator: they are rendered by the macOS print
pipeline (`cupsfilter`, Producer "Quartz PDFContext": subset TrueType font, MacRoman encoding, Flate streams,
one Tj per text line with space padding). Each PDF's expectations live in manifest.json.

Run on macOS:  python3 tests/fixtures/pdf_corpus/build_corpus.py
"""

from __future__ import annotations

import json
import os
import random
import subprocess
import tempfile
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))


def _inr(v: Decimal) -> str:
    s = f"{v:.2f}"
    whole, frac = s.split(".")
    neg = whole.startswith("-")
    whole = whole.lstrip("-")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + "." + frac


def hdfc_two_line() -> tuple:
    rows = [
        ("2026-04-01 09:10:00", "UPI/CR/40911001/AYAZ QURESHI/ayaz@okhdfcbank", "UTR40911001", "", "300.00", "1300.00"),
        ("2026-04-01 10:15:00", "UPI/DR/40911002/MANISH PATIL/manish@okicici", "UTR40911002", "200.00", "", "1100.00"),
        ("2026-04-01 11:30:00", "UPI/DR/40911003/IMRAN SABLE/imran@ybl", "UTR40911003", "100.00", "", "1000.00"),
        ("2026-04-16 14:00:00", "NEFT-UTIB0001122-4093001-APEX SHELL PVT LTD", "UTR4093001", "", "250000.00", "251000.00"),
        ("2026-04-17 09:30:00", "RTGS/UTR4094002/APEX SHELL PVT LTD", "UTR4094002", "180000.00", "", "71000.00"),
        ("2026-04-18 18:15:00", "UPI/DR/4095003/ZOMATO FOODS/zomato@okaxis", "UTR4095003", "450.00", "", "70550.00"),
        ("2026-04-19 11:00:00", "UPI/DR/4096004/RELIANCE FRESH GROCERY", "UTR4096004", "1850.00", "", "68700.00"),
        ("2026-04-20 15:20:00", "NEFT-HDFC0000123-4097005-SHREE BALAJI ENT", "UTR4097005", "15000.00", "", "53700.00"),
    ]
    lines = [
        "HDFC BANK LIMITED - ACCOUNT STATEMENT",
        "Account Holder: POOJAN VIG | Account No: 501002938471 | IFSC: HDFC0000240",
        "Period: 01-04-2026 to 30-04-2026 | Savings Account",
        "",
        f"{'Txn Date':<16}{'Narration':<47}{'Chq/Ref No':<14}{'Debit':<12}{'Credit':<12}Balance",
        "",
    ]
    for d, n, r, dr, cr, bal in rows:
        lines.append(d)
        lines.append(f"{'':<16}{n:<47}{r:<14}{dr:<12}{cr:<12}{bal}")
        lines.append("")
    exp = {"rows": len(rows), "opening_balance": "1000.00", "closing_balance": "53700.00", "bank_code": "HDFC",
           "account_number": "501002938471", "total_debit": "197600.00", "total_credit": "250300.00"}
    return "PDF28_POOJAN-VIG_HDFC_501002938471.pdf", lines, exp


def sbi_multipage(n_rows: int = 140, seed: int = 28) -> tuple:
    rng = random.Random(seed)
    parties = ["KAVYA TRADERS", "RAVI KUMAR", "SHREE GANESH STEEL", "ANITA DESAI", "MAHA ELECTRIC", "OM LOGISTICS"]
    bal = Decimal("50000.00")
    opening = bal
    lines = [
        "STATE BANK OF INDIA",
        "Account Name : NIKHIL JOSHI                 Account Number : 38291045771",
        "IFSC : SBIN0001234       Statement From 01/04/2026 to 30/06/2026",
        "",
        f"{'Txn Date':<12}{'Description':<42}{'Ref No':<14}{'Debit':>14}{'Credit':>14}{'Balance':>15}",
    ]
    tot_dr = tot_cr = Decimal("0")
    day = 0
    for i in range(n_rows):
        day = min(day + rng.choice([0, 0, 1, 1, 2]), 89)
        y, m, d = 2026, 4 + day // 30, day % 30 + 1
        amt = Decimal(rng.randint(100, 90000)) + Decimal(rng.randint(0, 99)) / 100
        party = rng.choice(parties)
        rail = rng.choice(["NEFT", "IMPS", "UPI"])
        ref = f"{rail[:1]}{rng.randint(10**9, 10**10 - 1)}"
        if rng.random() < 0.5 or bal < amt:
            bal += amt
            tot_cr += amt
            dr, cr = "", _inr(amt)
            narr = f"{rail}/CR/{party}"
        else:
            bal -= amt
            tot_dr += amt
            dr, cr = _inr(amt), ""
            narr = f"{rail}/DR/{party}"
        lines.append(f"{d:02d}/{m:02d}/{y:<6}{narr:<42}{ref:<14}{dr:>14}{cr:>14}{_inr(bal):>15}")
    exp = {"rows": n_rows, "opening_balance": f"{opening:.2f}", "closing_balance": f"{bal:.2f}", "bank_code": "SBI",
           "account_number": "38291045771", "total_debit": f"{tot_dr:.2f}", "total_credit": f"{tot_cr:.2f}",
           "min_pages": 2}
    return "PDF28_NIKHIL-JOSHI_SBI_38291045771.pdf", lines, exp


def main() -> None:
    manifest = {}
    for builder in (hdfc_two_line, sbi_multipage):
        name, lines, exp = builder()
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "statement.txt")
            with open(src, "w", encoding="ascii") as fh:
                fh.write("\n".join(lines) + "\n")
            pdf = subprocess.run(
                ["cupsfilter", "-m", "application/pdf", "-o", "cpi=16", "-o", "lpi=8",
                 "-o", "page-left=24", "-o", "page-right=24", src],
                check=True, capture_output=True,
            ).stdout
        with open(os.path.join(HERE, name), "wb") as fh:
            fh.write(pdf)
        exp["producer"] = "Quartz PDFContext (cupsfilter)"
        manifest[name] = exp
    with open(os.path.join(HERE, "manifest.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
        fh.write("\n")


if __name__ == "__main__":
    main()
