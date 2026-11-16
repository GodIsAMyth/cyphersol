"""Pre-built sample statements (.pdf, .xlsx, .csv) and seeded CypherTrace / CypherEdge demo cases."""

from __future__ import annotations

import csv
import io
import os
import uuid
from typing import List, Tuple

from cyphersol.ingestion.anomaly_gate import run_anomaly_gate
from cyphersol.ingestion.parser import parse_statement_bytes
from cyphersol.ingestion.pdf_parser import generate_pdf_statement
from cyphersol.ingestion.xlsx_io import write_xlsx_bytes
from cyphersol.models import InvestigationCase, WorkspaceMode
from cyphersol.nlp.categorizer import HybridTransactionCategorizer
from cyphersol.nlp.entity_resolver import resolve_case_entities
from cyphersol.nlp.narration_parser import enrich_transaction_narration
from cyphersol.storage import CypherVault


def _build_csv_bytes(headers: List[str], rows: List[List[str]]) -> bytes:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(headers)
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def generate_sample_statement_files(sample_dir: str) -> List[Tuple[str, bytes, str]]:
    """
    Generate realistic multi-format Indian bank statements (.pdf, .xlsx, .csv) on disk
    covering both CypherTrace forensic patterns and CypherEdge accounting/loan workflows.
    Returns list of (filename, bytes, case_group: 'trace'|'edge').
    """
    os.makedirs(sample_dir, exist_ok=True)
    generated: List[Tuple[str, bytes, str]] = []

    # ------------------------------------------------------------------
    # CYBER / FORENSIC CASE STATEMENTS (CypherTrace)
    # Includes:
    # 1) Brochure Layering Screenshot scenario:
    #    AYAZ QURESHI -> POOJAN VIG (300) & DEEP PATANGE (200)
    #    POOJAN VIG -> MANISH PATIL (200) & IMRAN SABLE (100)
    #    DEEP PATANGE -> SAMIDA DEVKAR (200)
    # 2) Circular Loop: HAWALA TRADERS -> APEX SHELL PVT LTD -> ZENITH CONSULTANTS -> HAWALA TRADERS
    # 3) Critical Money Mule: RAMESH MULE A/C (rapid pass-through, fan-in/fan-out, AEPS/crypto leakage, zero bal oscillation)
    # ------------------------------------------------------------------

    # Statement 1 (PDF): POOJAN VIG (HDFC)
    poojan_rows = [
        ["2026-04-01 09:10:00", "UPI/CR/40911001/AYAZ QURESHI/ayaz@okhdfcbank", "UTR40911001", "", "300.00", "1300.00"],
        ["2026-04-01 10:15:00", "UPI/DR/40911002/MANISH PATIL/manish@okicici", "UTR40911002", "200.00", "", "1100.00"],
        ["2026-04-01 11:30:00", "UPI/DR/40911003/IMRAN SABLE/imran@ybl", "UTR40911003", "100.00", "", "1000.00"],
        ["2026-04-16 14:00:00", "NEFT-UTIB0001122-4093001-APEX SHELL PVT LTD", "UTR4093001", "", "250000.00", "251000.00"],
        ["2026-04-17 09:30:00", "RTGS/UTR4094002/APEX SHELL PVT LTD", "UTR4094002", "180000.00", "", "71000.00"],
        ["2026-04-18 18:15:00", "UPI/DR/4095003/ZOMATO FOODS/zomato@okaxis", "UTR4095003", "450.00", "", "70550.00"],
        ["2026-04-19 11:00:00", "UPI/DR/4096004/RELIANCE FRESH GROCERY", "UTR4096004", "1850.00", "", "68700.00"],
        ["2026-04-20 15:20:00", "NEFT-HDFC0000123-4097005-SHREE BALAJI ENT", "UTR4097005", "15000.00", "", "53700.00"],
    ]
    poojan_pdf = generate_pdf_statement(
        [
            "HDFC BANK LIMITED - ACCOUNT STATEMENT",
            "Account Holder: POOJAN VIG | Account No: 501002938471 | IFSC: HDFC0000240",
            "Period: 01-04-2026 to 30-04-2026 | Savings Account",
        ],
        ["Txn Date", "Narration", "Chq/Ref No", "Debit", "Credit", "Balance"],
        poojan_rows,
    )
    generated.append(("CASE041_POOJAN-VIG_HDFC_501002938471.pdf", poojan_pdf, "trace"))

    # Statement 2 (CSV): DEEP PATANGE (ICICI) - also tests alias 'Pujan Vij'
    deep_csv = _build_csv_bytes(
        ["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"],
        [
            ["2026-04-01 09:20:00", "UPI/CR/40912001/AYAZ QURESHI/ayaz@okhdfcbank", "UTR40912001", "0", "200", "1200"],
            ["2026-04-01 12:00:00", "UPI/DR/40912002/SAMIDA DEVKAR/samida@oksbi", "UTR40912002", "200", "0", "1000"],
            ["2026-04-17 10:00:00", "IMPS/40922003/Pujan Vij/puja@okhdfc", "UTR40922003", "0", "25000", "26000"],
            ["2026-04-18 16:00:00", "UPI/DR/40952004/INDIAN OIL PETROL PUMP", "UTR40952004", "1200", "0", "24800"],
        ],
    )
    generated.append(("CASE041_DEEP-PATANGE_ICICI_000401992831.csv", deep_csv, "trace"))

    # Statements 3, 4, 5 (CSV): Leaf Layering Nodes with statements (MANISH PATIL, IMRAN SABLE, SAMIDA DEVKAR)
    manish_csv = _build_csv_bytes(
        ["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"],
        [
            ["2026-04-01 10:15:00", "UPI/CR/40911002/POOJAN VIG/poojan@okhdfc", "UTR40911002", "0", "200", "2200"],
            ["2026-04-02 11:00:00", "UPI/DR/40923001/BHARAT TRADERS/bharat@ybl", "UTR40923001", "180", "0", "2020"],
        ],
    )
    generated.append(("CASE041_MANISH-PATIL_SBI_30981234001.csv", manish_csv, "trace"))

    imran_csv = _build_csv_bytes(
        ["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"],
        [
            ["2026-04-01 11:30:00", "UPI/CR/40911003/M/S POOJAN VIG/poojan@okhdfc", "UTR40911003", "0", "100", "900"],
            ["2026-04-02 15:00:00", "ATM WDL/NFS/MUMBAI", "ATM0012", "100", "0", "800"],
        ],
    )
    generated.append(("CASE041_IMRAN-SABLE_AXIS_918010044221.csv", imran_csv, "trace"))

    samida_csv = _build_csv_bytes(
        ["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"],
        [
            ["2026-04-01 12:00:00", "UPI/CR/40912002/DEEP PATANGE/deep@okicici", "UTR40912002", "0", "200", "1700"],
            ["2026-04-02 16:40:00", "UPI/DR/40925001/BIGBASKET GROCERY", "UTR40925001", "150", "0", "1550"],
        ],
    )
    generated.append(("CASE041_SAMIDA-DEVKAR_KOTAK_7711882201.csv", samida_csv, "trace"))

    # Statements 6, 7, 8: 3-Hop Circular Loop (HAWALA TRADERS -> APEX SHELL -> ZENITH CONSULTANTS -> HAWALA TRADERS)
    # Plus AYAZ QURESHI's direct credit source transfers (Total ₹500 -> ₹300 POOJAN VIG & ₹200 DEEP PATANGE)
    ayaz_csv = _build_csv_bytes(
        ["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"],
        [
            ["2026-04-01 08:30:00", "NEFT-SBIN0000300-AY001-CAPITAL INFUSION", "AY001", "0", "5000", "10000"],
            ["2026-04-01 09:10:00", "UPI/DR/40911001/POOJAN VIG/poojan@okhdfc", "UTR40911001", "300", "0", "9700"],
            ["2026-04-01 09:20:00", "UPI/DR/40912001/DEEP PATANGE/deep@okicici", "UTR40912001", "200", "0", "9500"],
        ],
    )
    generated.append(("CASE041_AYAZ-QURESHI_SBI_30112233445.csv", ayaz_csv, "trace"))

    hawala_xlsx = write_xlsx_bytes([
        ("Statement", [
            ["Account Holder: HAWALA TRADERS", "Account No: 990011223344", "Bank: Axis Bank", "IFSC: UTIB0000444"],
            ["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"],
            ["2026-04-02 10:00:00", "RTGS/AY002/SUDHAKAR SHELL CREDITS", "AY002", 0, 450000, 455000],
            ["2026-04-02 11:00:00", "RTGS/LOOP01/APEX SHELL PVT LTD", "LOOP01", 440000, 0, 15000],
            ["2026-04-03 18:00:00", "RTGS/LOOP03/ZENITH CONSULTANTS", "LOOP03", 0, 425000, 440000],
            ["2026-04-04 09:00:00", "ATM WDL/CASH OUT", "ATM99", 40000, 0, 400000],
        ])
    ])
    generated.append(("CASE041_HAWALA-TRADERS_AXIS_990011223344.xlsx", hawala_xlsx, "trace"))

    apex_csv = _build_csv_bytes(
        ["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"],
        [
            ["2026-04-02 11:00:00", "RTGS/LOOP01/HAWALA TRADERS", "LOOP01", "0", "440000", "442000"],
            ["2026-04-02 15:30:00", "RTGS/LOOP02/ZENITH CONSULTANTS", "LOOP02", "432000", "0", "10000"],
            ["2026-04-16 14:00:00", "NEFT-HDFC0000240-4093001-POOJAN VIG", "UTR4093001", "250000", "0", "250000"],
            ["2026-04-17 09:30:00", "RTGS/UTR4094002/POOJAN VIG", "UTR4094002", "0", "180000", "430000"],
        ],
    )
    generated.append(("CASE041_APEX-SHELL-PVT-LTD_INDUS_201009988776.csv", apex_csv, "trace"))

    zenith_csv = _build_csv_bytes(
        ["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"],
        [
            ["2026-04-02 15:30:00", "RTGS/LOOP02/APEX SHELL PVT LTD", "LOOP02", "0", "432000", "435000"],
            ["2026-04-03 18:00:00", "RTGS/LOOP03/HAWALA TRADERS", "LOOP03", "425000", "0", "10000"],
        ],
    )
    generated.append(("CASE041_ZENITH-CONSULTANTS_YES_001980077665.csv", zenith_csv, "trace"))

    # Statement 9: Critical Mule Account (RAMESH MULE A/C - scores > 80 Critical)
    # Dormancy gap (Feb -> Apr), immediate pass-through (< 45 mins), zero lifestyle, 8-victim fan-in,
    # structuring near 49,500, zero-balance oscillations, round numbers, AEPS/Crypto leakage.
    mule_rows = [
        ["2026-02-15 10:00:00", "INT.PD/SB INTEREST", "INT01", "0", "25", "525"],
        ["2026-04-08 10:00:00", "UPI/CR/M001/VICTIM ONE/v1@oksbi", "M001", "0", "49000", "49525"],
        ["2026-04-08 10:05:00", "UPI/CR/M002/VICTIM TWO/v2@ybl", "M002", "0", "49500", "99025"],
        ["2026-04-08 10:10:00", "UPI/CR/M003/VICTIM THREE/v3@okicici", "M003", "0", "48500", "147525"],
        ["2026-04-08 10:15:00", "UPI/CR/M004/VICTIM FOUR/v4@paytm", "M004", "0", "49000", "196525"],
        ["2026-04-08 10:20:00", "UPI/CR/M005/VICTIM FIVE/v5@okaxis", "M005", "0", "49000", "245525"],
        ["2026-04-08 10:25:00", "UPI/CR/M006/VICTIM SIX/v6@okhdfc", "M006", "0", "49500", "295025"],
        ["2026-04-08 10:30:00", "UPI/CR/M007/VICTIM SEVEN/v7@ybl", "M007", "0", "48500", "343525"],
        ["2026-04-08 10:35:00", "UPI/CR/M008/VICTIM EIGHT/v8@oksbi", "M008", "0", "49000", "392525"],
        ["2026-04-08 10:45:00", "UPI/DR/M009/COINDCX CRYPTO EXCHANGE", "M009", "300000", "0", "92525"],
        ["2026-04-08 10:50:00", "AEPS CASH WDL/M010", "M010", "92200", "0", "325"],
        ["2026-04-09 11:00:00", "UPI/CR/M011/VICTIM NINE/v9@okaxis", "M011", "0", "195000", "195325"],
        ["2026-04-09 11:10:00", "UPI/CR/M012/VICTIM TEN/v10@ybl", "M012", "0", "49000", "244325"],
        ["2026-04-09 11:20:00", "AEPS CASH WDL/M013", "M013", "49000", "0", "195325"],
        ["2026-04-09 11:30:00", "IMPS/M014/HAWALA TRADERS", "M014", "195000", "0", "325"],
        ["2026-04-10 09:00:00", "NEFT/M015/VICTIM ELEVEN", "M015", "0", "100000", "100325"],
        ["2026-04-10 09:15:00", "IMPS/M016/HAWALA TRADERS", "M016", "100000", "0", "325"],
    ]
    mule_csv = _build_csv_bytes(
        ["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"],
        mule_rows,
    )
    generated.append(("CASE041_RAMESH-KUMAR-MULE_PNB_440011998877.csv", mule_csv, "trace"))

    # ------------------------------------------------------------------
    # ACCOUNTING, TAX & LOAN ELIGIBILITY CLIENT STATEMENT (CypherEdge)
    # SHREE BALAJI ENTERPRISES (Native PDF + XLSX showcase)
    # ------------------------------------------------------------------
    edge_rows = [
        ["2025-04-02", "RAZORPAY/SETTLEMENT/INV-401", "RZP401", "", "320000.00", "445000.00"],
        ["2025-04-05", "NACH/DR/HDFC HOME LOAN EMI", "NACH01", "42000.00", "", "403000.00"],
        ["2025-04-08", "NEFT-MAHB0000101-OFFICE RENT LANDLORD", "RENT04", "45000.00", "", "358000.00"],
        ["2025-04-12", "UPI/DR/MSEDCL ELECTRICITY BILL", "ELEC04", "6800.00", "", "351200.00"],
        ["2025-04-15", "IMPS/DR/M/S SHARMA LEGAL ADVOCATES", "LEG04", "35000.00", "", "316200.00"],
        ["2025-04-20", "UPI/DR/AWS CLOUD SUBSCRIPTION", "AWS04", "12400.00", "", "303800.00"],
        ["2025-04-25", "NEFT/CR/BHARAT TRADERS INVOICE", "INV402", "", "210000.00", "513800.00"],
        ["2025-04-28", "TRF/998812/S K VERMA", "UNK01", "18500.00", "", "495300.00"],
        ["2025-05-02", "CASH DEPOSIT/BRANCH CDM/MUMBAI", "CDM01", "", "250000.00", "745300.00"],
        ["2025-05-05", "NACH/DR/HDFC HOME LOAN EMI", "NACH02", "42000.00", "", "703300.00"],
        ["2025-05-06", "NACH RETURN CHG/INSUFFICIENT BAL", "RTN01", "590.00", "", "702710.00"],
        ["2025-05-10", "NEFT/DR/GSTN PMT-06 CHALLAN", "GST05", "64000.00", "", "638710.00"],
        ["2025-05-14", "ACH/DR/CAMS MUTUAL FUND SIP", "SIP05", "25000.00", "", "613710.00"],
        ["2025-05-18", "UPI/DR/LIC OF INDIA PREMIUM", "LIC05", "48000.00", "", "565710.00"],
        ["2025-05-22", "RAZORPAY/SETTLEMENT/INV-510", "RZP510", "", "385000.00", "950710.00"],
        ["2025-05-26", "ATM WDL/SELF CASH/ANDHERI", "ATM05", "15000.00", "", "935710.00"],
        ["2025-05-28", "NEFT/DR/BLUEDART COURIER LOGISTICS", "LOG05", "19200.00", "", "916510.00"],
        ["2025-05-30", "TRF/771122/M R PATEL", "UNK02", "22000.00", "", "894510.00"],
        ["2025-06-05", "NACH/DR/HDFC HOME LOAN EMI", "NACH03", "42000.00", "", "852510.00"],
        ["2025-06-09", "NEFT-MAHB0000101-OFFICE RENT LANDLORD", "RENT06", "210000.00", "", "642510.00"],
        ["2025-06-15", "OLTAS ADVANCE TAX CHALLAN 280", "TAX06", "75000.00", "", "567510.00"],
        ["2025-06-22", "RAZORPAY/SETTLEMENT/INV-612", "RZP612", "", "410000.00", "977510.00"],
        ["2025-06-28", "INT.PD/SB INTEREST QUARTERLY", "INT06", "", "12450.00", "989960.00"],
        ["2025-06-30", "SELF TRANSFER TO KOTAK OWN A/C", "TRF06", "100000.00", "", "889960.00"],
    ]
    edge_pdf = generate_pdf_statement(
        [
            "STATE BANK OF INDIA - CURRENT ACCOUNT STATEMENT",
            "Account Holder: SHREE BALAJI ENTERPRISES | Account No: 382910445566 | IFSC: SBIN0000300",
            "Period: 01-04-2025 to 30-06-2025 | Current Account",
        ],
        ["Txn Date", "Narration", "Ref No", "Debit", "Credit", "Balance"],
        edge_rows,
    )
    generated.append(("CA2025_SHREE-BALAJI-ENTERPRISES_SBI_382910445566.pdf", edge_pdf, "edge"))

    # Persist sample files on disk for easy manual upload testing
    for fname, raw_bytes, _ in generated:
        with open(os.path.join(sample_dir, fname), "wb") as f:
            f.write(raw_bytes)

    return generated


def ensure_demo_cases_seeded(vault: CypherVault, sample_dir: str) -> None:
    """Seed the two pre-loaded demo workspaces if the database has no cases yet."""
    if vault.list_cases():
        return

    samples = generate_sample_statement_files(sample_dir)
    categorizer = HybridTransactionCategorizer()

    trace_case = InvestigationCase(
        case_id="CASE_TRACE_EOW_041",
        title="EOW Cyber Fraud & Layering Case #041 (Demo)",
        mode=WorkspaceMode.CYPHER_TRACE,
        agency_or_firm_name="Economic Offences Wing (EOW) - Special Investigation Unit",
        created_at="2026-04-15T10:00:00Z",
        similarity_threshold=0.85,
        confidence_threshold=0.65,
    )
    edge_case = InvestigationCase(
        case_id="CASE_EDGE_BALAJI_FY25",
        title="Shree Balaji Enterprises — FY 2025-26 Audit & Loan Appraisal (Demo)",
        mode=WorkspaceMode.CYPHER_EDGE,
        agency_or_firm_name="Mehta & Associates, Chartered Accountants",
        created_at="2026-04-15T10:05:00Z",
        similarity_threshold=0.85,
        confidence_threshold=0.65,
    )
    vault.save_case(trace_case)
    vault.save_case(edge_case)

    for case_obj, group_tag in ((trace_case, "trace"), (edge_case, "edge")):
        case_txns = []
        case_stmts = []
        for fname, raw_bytes, tag in samples:
            if tag != group_tag:
                continue
            stmt, txns, _ = parse_statement_bytes(fname, raw_bytes, case_obj.case_id)
            stmt, txns, _ = run_anomaly_gate(stmt, txns, existing_case_txns=case_txns)
            txns = [enrich_transaction_narration(t) for t in txns]
            txns = categorizer.classify_batch(txns)
            case_stmts.append(stmt)
            case_txns.extend(txns)

        entities, case_txns = resolve_case_entities(
            case_obj.case_id,
            case_stmts,
            case_txns,
            similarity_threshold=case_obj.similarity_threshold,
        )
        for st in case_stmts:
            st_txns = [t for t in case_txns if t.statement_id == st.statement_id]
            vault.save_statement_and_transactions(st, st_txns)
        vault.replace_entities(case_obj.case_id, entities)
        vault.log_audit(case_obj.case_id, "DEMO_SEED", {"statements": len(case_stmts), "transactions": len(case_txns)})
