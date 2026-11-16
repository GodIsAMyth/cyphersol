"""Excel Workbook, Courtroom Forensic Dossier, and Section 63 BSA Evidence Certificate exporters."""

from __future__ import annotations

import datetime as dt
import html
import math
import re
import textwrap
import unicodedata
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence, Tuple

from cyphersol.edge.itr_compliance import generate_itr_compliance_report
from cyphersol.edge.loan_eligibility import compute_loan_eligibility_report
from cyphersol.ingestion.xlsx_io import write_xlsx_bytes
from cyphersol.models import (
    AnomalyFlag,
    BankAccountStatement,
    CircularLoop,
    InvestigationCase,
    MuleRiskAssessment,
    MuleRiskTier,
    NormalizedTransaction,
    ResolvedEntity,
    WorkspaceMode,
)
from cyphersol.trace.bidirectional import list_all_bidirectional_pairs
from cyphersol.trace.graph_engine import build_network_graph
from cyphersol.trace.loop_detector import detect_circular_loops


def _infer_llm_assisted(
    transactions: Sequence[NormalizedTransaction], llm_assisted: Optional[bool]
) -> Optional[bool]:
    """Explicit caller value wins; otherwise any LLM-sourced row proves AI use; else unknown (None)."""
    if llm_assisted is not None:
        return bool(llm_assisted)
    for tx in transactions:
        if str(tx.categorization_source or "").upper().startswith("LLM") or AnomalyFlag.LLM_REPAIRED in (
            tx.anomaly_flags or []
        ):
            return True
    return None


def custody_processing_statement(
    transactions: Sequence[NormalizedTransaction],
    llm_assisted: Optional[bool] = None,
    llm_provider: Optional[str] = None,
    loops: Optional[List[CircularLoop]] = None,
    graph: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Certificate wording that reflects how the case was actually processed (BE-19). Never claims an air gap:
    the application cannot attest to the host's network isolation.
    """
    used = _infer_llm_assisted(transactions, llm_assisted)
    digest_note = (
        "The SHA-256 digests below were computed locally over the original uploaded files at ingestion."
    )
    if used is True:
        prov = f" ({llm_provider})" if llm_provider else ""
        return (
            f"Cloud AI assistance{prov} was used while processing this case: portions of statement content "
            "(narrations, amounts and, for scanned documents, page images) were transmitted to an external AI "
            "provider's API, and AI-derived values are marked in the transaction data. " + digest_note
        )
    if used is False:
        return (
            "Statements in this case were processed locally by CypherSOL using deterministic heuristics only; "
            "no AI-assisted processing was recorded for this case. " + digest_note
        )
    return (
        "The processing mode (deterministic heuristics or AI-assisted) for this case was not recorded in this "
        "export. " + digest_note
    )


def export_audit_workbook_xlsx(
    case: InvestigationCase,
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    mule_assessments: List[MuleRiskAssessment],
    loops: Optional[List[CircularLoop]] = None,
    bidirectional_pairs: Optional[List[Dict[str, Any]]] = None,
) -> bytes:
    """Generate a comprehensive multi-sheet .xlsx workbook covering Edge + Trace outputs.

    ``loops`` / ``bidirectional_pairs`` let the caller pass already-computed (cached) results instead of
    re-running detection. Sheets: Executive Summary, Transactions, Category Schedule, Mule Risk Scorecard,
    Mule Explainability, Circular Loops, Entities, Bidirectional Flows, ITR Summary / Red Flags / Flag
    Details / Category Schedule, Loan Eligibility / Monthly / EMI Obligations / Bounces, Chain of Custody.
    """
    itr = generate_itr_compliance_report(transactions, statements=statements)
    loan = compute_loan_eligibility_report(transactions, statements=statements)
    if loops is None:
        loops = detect_circular_loops(statements, transactions, entities)

    # Sheet 1: Executive Overview
    s1_rows: List[Sequence[Any]] = [
        ["CypherSOL Audit & Forensic Summary", "Value"],
        ["Case / Workspace Title", case.title],
        ["Mode", case.mode.value],
        ["Agency / CA Firm", case.agency_or_firm_name or "Local Workspace"],
        ["Generated At (UTC)", dt.datetime.now(dt.timezone.utc).isoformat()],
        ["Total Uploaded Statements", len(statements)],
        ["Total Extracted Transactions", len(transactions)],
        ["Total Resolved Entities", len(entities)],
        ["Suspense Pending Count", itr["suspense_pending_count"]],
        ["Gross Credits (INR)", itr["reconciliation"]["gross_credits"]],
        ["Gross Debits (INR)", itr["reconciliation"]["gross_debits"]],
        ["Daily Average Bank Balance (INR)", loan["daily_abb"]],
        ["Fixed-Date ABB (5/10/15/20/25/EOM)", loan["fixed_date_abb"]],
        ["Detected Monthly EMI Load (INR)", loan["monthly_existing_emi"]],
        ["Inward / Outward Bounces", f"{loan['inward_bounce_count']} / {loan['outward_bounce_count']}"],
        ["Max Eligible Loan Estimate (INR)", loan["max_eligible_loan"]],
        ["Circular Loops Detected", len(loops)],
        ["Critical Mule Accounts (80-100)", sum(1 for m in mule_assessments if m.risk_tier.value == "Critical")],
    ]

    # Sheet 2: All Normalized Transactions
    s2_rows: List[Sequence[Any]] = [
        [
            "Date", "Account No", "Holder Name", "Channel", "Narration", "UTR / Ref",
            "Counterparty", "Debit (DR)", "Credit (CR)", "Balance", "Category ID",
            "Category Name", "Confidence", "Source", "Tally Voucher", "Tally Ledger", "Anomaly Flags",
        ]
    ]
    for t in transactions:
        s2_rows.append([
            t.txn_date,
            t.source_account_number or "",
            t.source_holder_name or "",
            t.payment_channel,
            t.raw_narration,
            t.utr_number or t.cheque_or_ref_no or "",
            t.extracted_counterparty or "",
            float(t.debit),
            float(t.credit),
            float(t.running_balance),
            t.category_id,
            t.category_name,
            round(t.confidence_score, 3),
            t.categorization_source,
            t.tally_voucher_type.value if t.tally_voucher_type else "",
            t.tally_ledger_name or "",
            ", ".join(f.value for f in t.anomaly_flags),
        ])

    # Sheet 3: 52-Category Summary
    s3_rows: List[Sequence[Any]] = [["Category ID", "Category Group", "Category Name", "ITR Section", "Total Credit", "Total Debit", "Txn Count"]]
    for item in itr["category_schedule"]:
        s3_rows.append([
            item["category_id"],
            item["group"],
            item["category_name"],
            item["itr_section"],
            item["credit"],
            item["debit"],
            item["tx_count"],
        ])

    # Sheet 4: Mule Risk Scorecard
    s4_rows: List[Sequence[Any]] = [
        [
            "Account No", "Holder", "Bank", "Composite Score", "Risk Tier",
            "Flow-Through (25%)", "Velocity (20%)", "Lifestyle Absence (15%)",
            "Concentration (10%)", "Fan-In/Out (10%)", "Cash/Crypto Leak (5%)",
            "Structuring (5%)", "Zero Bal Osc (5%)", "Burst/Dormancy (3%)", "Round No (2%)",
            "Median Hold (hrs)",
        ]
    ]
    for m in mule_assessments:
        p = m.parameters
        s4_rows.append([
            m.account_number,
            m.account_holder_name,
            m.bank_name,
            m.composite_score,
            m.risk_tier.value,
            p.flow_through_ratio,
            p.velocity_hold_time,
            p.lifestyle_spending_absence,
            p.counterparty_concentration,
            p.fan_in_fan_out_pattern,
            p.cash_crypto_wallet_leakage,
            p.structuring_smurfing,
            p.zero_balance_oscillation,
            p.burst_dormancy_pattern,
            p.round_number_anomalies,
            m.median_hold_hours if m.median_hold_hours is not None else "",
        ])

    # Sheet 5: Circular Loops & Anomalies
    s5_rows: List[Sequence[Any]] = [["Loop ID", "Hops", "Path", "Min Amount", "Max Amount", "Retention %", "Duration (hrs)", "Start Date", "End Date"]]
    for lp in loops:
        s5_rows.append([
            lp.loop_id,
            lp.hop_count,
            " -> ".join(lp.entities),
            lp.min_amount,
            lp.max_amount,
            round(lp.retention_ratio * 100.0, 1),
            lp.duration_hours,
            lp.start_date,
            lp.end_date,
        ])

    # Sheet 6: Resolved Entities & SHA-256 Chain of Custody
    s6_rows: List[Sequence[Any]] = [["Statement File", "Bank", "Account No", "Holder", "Period", "SHA-256 Digest", "Mismatches", "Duplicates"]]
    for st in statements:
        s6_rows.append([
            st.source_filename,
            st.bank_name,
            st.account_number,
            st.account_holder_name,
            f"{st.period_start} to {st.period_end}",
            st.sha256_hash,
            st.balance_mismatch_count,
            st.duplicate_suspect_count,
        ])

    # Mule explainability: one row per account x parameter (TF-15)
    s4b_rows: List[Sequence[Any]] = [[
        "Account No", "Holder", "Composite", "Tier", "#", "Parameter", "Weight", "Raw Metric", "Raw Details",
        "Score S_k", "Contribution (w*S*100)", "Evidence Count", "Evidence tx_ids",
    ]]
    for m in mule_assessments:
        for item in getattr(m, "explainability", None) or []:
            s4b_rows.append([
                m.account_number, m.account_holder_name, m.composite_score, m.risk_tier.value, item.get("rank"),
                item.get("label"), item.get("weight"),
                item.get("raw_metric") if item.get("raw_metric") is not None else "",
                _raw_metric_text(item), item.get("score"), item.get("contribution"), item.get("evidence_count"),
                ", ".join(item.get("evidence_tx_ids") or []),
            ])

    # Entities
    ent_rows: List[Sequence[Any]] = [[
        "Entity ID", "Canonical Name", "Aliases", "Linked Accounts", "Linked VPAs", "Has Statement",
        "Moved to Misc", "Total Credited", "Total Debited", "Txns",
    ]]
    for e in entities:
        ent_rows.append([
            e.entity_id, e.canonical_name, "; ".join(e.aliases), "; ".join(e.linked_account_numbers),
            "; ".join(e.linked_vpas), bool(e.has_statement), bool(e.is_moved_to_misc),
            float(e.total_credited), float(e.total_debited), e.tx_count,
        ])

    # Bidirectional flows
    if bidirectional_pairs is None:
        bidirectional_pairs = list_all_bidirectional_pairs(statements, transactions, entities)
    bi_rows: List[Sequence[Any]] = [[
        "Entity A", "Entity B", "A -> B", "B -> A", "Net (A - B)", "Txns A -> B", "Txns B -> A", "Mutual Volume",
        "Entity A ID", "Entity B ID",
    ]]
    for p in bidirectional_pairs:
        bi_rows.append([
            p.get("entity_a_name"), p.get("entity_b_name"), p.get("total_a_to_b"), p.get("total_b_to_a"),
            p.get("net_flow"), p.get("tx_count_a_to_b"), p.get("tx_count_b_to_a"), p.get("total_mutual_volume", ""),
            p.get("entity_a_id"), p.get("entity_b_id"),
        ])

    return write_xlsx_bytes([
        ("Executive Summary", s1_rows),
        ("Transactions", s2_rows),
        ("Category Schedule", s3_rows),
        ("Mule Risk Scorecard", s4_rows),
        ("Mule Explainability", s4b_rows),
        ("Circular Loops", s5_rows),
        ("Entities", ent_rows),
        ("Bidirectional Flows", bi_rows),
        *_itr_sheets(itr),
        *_loan_sheets(loan),
        ("Chain of Custody", s6_rows),
    ])


def _top_drivers(m: MuleRiskAssessment, n: int = 3) -> str:
    vec = getattr(m, "explainability", None) or []
    top = sorted(vec, key=lambda v: float(v.get("contribution", 0)), reverse=True)[:n]
    return ", ".join(f"{v.get('label')} {float(v.get('contribution', 0)):.1f}" for v in top if float(v.get("contribution", 0)) > 0)


def _render_dossier_network_svg(graph: Dict[str, Any]) -> str:
    """Render an inline force-directed network diagram SVG for printable dossiers."""
    nodes = graph.get("nodes", [])
    edges = graph.get("edges", [])
    if not nodes:
        return "<p>No network transfer edges available.</p>"

    node_map = {n["entity_id"]: n for n in nodes}
    role_style = {
        "PRIMARY_HUB": (13.0, "#dc2626", "#ffffff", 2.0, True),
        "BRIDGE_CONDUIT": (12.5, "#eab308", "#b45309", 2.5, True),
        "EXIT_ENDPOINT": (10.5, "#2563eb", "#ffffff", 1.8, True),
        "PERIPHERAL_LEAF": (7.0, "#94a3b8", "#ffffff", 1.2, False),
    }

    parts = [
        '<svg viewBox="0 0 920 520" style="width:100%;max-width:920px;border:1px solid #cbd5e1;border-radius:8px;background:#fcfdff">',
        '<defs>'
        '<marker id="d-arr-m" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="5" markerHeight="5" orient="auto-start-reverse">'
        '<path d="M 0 1 L 10 5 L 0 9 z" fill="#94a3b8"/></marker>'
        '<marker id="d-arr-b" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
        '<path d="M 0 1 L 10 5 L 0 9 z" fill="#d97706"/></marker>'
        '</defs>',
    ]
    for e in edges:
        src = node_map.get(e["source"])
        dst = node_map.get(e["target"])
        if not src or not dst:
            continue
        is_bridge = (
            src.get("node_role") == "BRIDGE_CONDUIT"
            or dst.get("node_role") == "BRIDGE_CONDUIT"
            or (src.get("node_role") == "PRIMARY_HUB" and dst.get("node_role") == "EXIT_ENDPOINT")
        )
        dx = dst["x"] - src["x"]
        dy = dst["y"] - src["y"]
        dist = math.hypot(dx, dy) or 1.0
        r1 = role_style.get(src.get("node_role", "PERIPHERAL_LEAF"), role_style["PERIPHERAL_LEAF"])[0] + 1.0
        r2 = role_style.get(dst.get("node_role", "PERIPHERAL_LEAF"), role_style["PERIPHERAL_LEAF"])[0] + 4.0
        if dist <= r1 + r2 + 4.0:
            continue
        x1 = src["x"] + (dx / dist) * r1
        y1 = src["y"] + (dy / dist) * r1
        x2 = dst["x"] - (dx / dist) * r2
        y2 = dst["y"] - (dy / dist) * r2
        stroke = "#f59e0b" if is_bridge else "#cbd5e1"
        marker = "d-arr-b" if is_bridge else "d-arr-m"
        parts.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{stroke}" stroke-width="{"2.2" if is_bridge else "1.1"}" marker-end="url(#{marker})"/>'
        )

    for n in nodes:
        r, fill, stroke, sw, bold = role_style.get(n.get("node_role", "PERIPHERAL_LEAF"), role_style["PERIPHERAL_LEAF"])
        lbl = html.escape(n["label"][:20])
        parts.append(
            f'<circle cx="{n["x"]:.1f}" cy="{n["y"]:.1f}" r="{r:.1f}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"/>'
            f'<text x="{n["x"]:.1f}" y="{n["y"] + r + 10:.1f}" font-size="{"10" if bold else "8"}" '
            f'font-weight="{"700" if bold else "500"}" fill="#0f172a" text-anchor="middle">{lbl}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def export_forensic_dossier_html(
    case: InvestigationCase,
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    mule_assessments: List[MuleRiskAssessment],
    llm_assisted: Optional[bool] = None,
    llm_provider: Optional[str] = None,
    loops: Optional[List[CircularLoop]] = None,
    graph: Optional[Dict[str, Any]] = None,
) -> str:
    """
    Generate a self-contained courtroom/audit printable HTML dossier + Sec 63 BSA hash certificate.

    ``llm_assisted``/``llm_provider`` describe how the case was processed (from the audit log / telemetry);
    when omitted, AI use is inferred from LLM-sourced rows and otherwise reported as not recorded.
    """
    custody_text = custody_processing_statement(transactions, llm_assisted, llm_provider)
    itr = generate_itr_compliance_report(transactions, statements=statements)
    loan = compute_loan_eligibility_report(transactions, statements=statements)
    if loops is None:
        loops = detect_circular_loops(statements, transactions, entities)
    if graph is None:
        mule_map = {m.entity_id: m.to_dict() for m in mule_assessments if m.entity_id}
        graph = build_network_graph(statements, transactions, entities, mule_scores=mule_map)
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    stmt_rows_html = "".join(
        f"<tr><td>{html.escape(s.source_filename)}</td><td>{html.escape(s.bank_name)}</td>"
        f"<td>{html.escape(s.account_number)}</td><td>{html.escape(s.account_holder_name)}</td>"
        f"<td style='font-family:monospace;font-size:11px'>{html.escape(s.sha256_hash)}</td></tr>"
        for s in statements
    )
    mule_rows_html = "".join(
        f"<tr><td>{html.escape(m.account_holder_name)} ({html.escape(m.account_number)})</td>"
        f"<td><b>{m.composite_score:.1f}</b></td><td>{html.escape(m.risk_tier.value)}</td>"
        + "".join(f"<td>{float(v):.2f}</td>" for v in m.parameters.to_dict().values())
        + f"<td>{html.escape(_top_drivers(m))}</td></tr>"
        for m in mule_assessments
    )
    loop_rows_html = "".join(
        f"<tr><td>{lp.hop_count}</td><td>{html.escape(' → '.join(lp.entities))}</td>"
        f"<td>₹{lp.min_amount:,.2f} – ₹{lp.max_amount:,.2f}</td><td>{lp.retention_ratio * 100:.1f}%</td>"
        f"<td>{lp.duration_hours:.1f} hrs</td></tr>"
        for lp in loops
    ) or "<tr><td colspan='5'>No temporal circular loops detected within threshold.</td></tr>"

    flag_rows_html = "".join(
        f"<tr><td>{html.escape(f['section'])}</td><td>{html.escape(f['severity'])}</td>"
        f"<td>{html.escape(f['title'])}</td><td>₹{f['amount']:,.2f}</td></tr>"
        for f in itr["statutory_red_flags"]
    ) or "<tr><td colspan='4'>No statutory ITR compliance red flags detected.</td></tr>"

    network_section_html = ""
    if case.mode == WorkspaceMode.CYPHER_TRACE or len(statements) > 1:
        network_svg = _render_dossier_network_svg(graph)
        network_section_html = f"""
  <h2>3. Multi-Account Fund Trail Network Topology</h2>
  <p style="font-size:12px;color:#475569">Red: Primary Account Hub | Gold: Bridge / Conduit Entity | Blue: Cash / VDA Exit Endpoint | Grey: Peripheral Counterparty</p>
  {network_svg}
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>CypherSOL Dossier - {html.escape(case.title)}</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #0f172a; margin: 32px; line-height: 1.5; }}
  h1, h2 {{ color: #0f4c81; border-bottom: 2px solid #e2e8f0; padding-bottom: 6px; }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin: 16px 0; }}
  .kpi {{ border: 1px solid #cbd5e1; border-radius: 8px; padding: 12px; background: #f8fafc; }}
  .kpi label {{ font-size: 11px; text-transform: uppercase; color: #64748b; display: block; }}
  .kpi strong {{ font-size: 18px; color: #0f172a; }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0 24px; font-size: 13px; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 8px; text-align: left; }}
  th {{ background: #0f4c81; color: #fff; }}
  .cert {{ background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px; padding: 14px; margin-top: 20px; }}
  @media print {{ body {{ margin: 12px; }} }}
</style>
</head>
<body>
  <h1>CypherSOL Forensic &amp; Financial Intelligence Dossier</h1>
  <p><b>Case / Client:</b> {html.escape(case.title)} &nbsp;|&nbsp; <b>Mode:</b> {html.escape(case.mode.value)} &nbsp;|&nbsp; <b>Agency / Firm:</b> {html.escape(case.agency_or_firm_name or 'Local Offline Workspace')} &nbsp;|&nbsp; <b>Generated:</b> {now_iso}</p>

  <div class="kpi-grid">
    <div class="kpi"><label>Statements Processed</label><strong>{len(statements)}</strong></div>
    <div class="kpi"><label>Transactions Analyzed</label><strong>{len(transactions)}</strong></div>
    <div class="kpi"><label>Gross Credits / Debits</label><strong>₹{itr['reconciliation']['gross_credits']:,.0f} / ₹{itr['reconciliation']['gross_debits']:,.0f}</strong></div>
    <div class="kpi"><label>Daily ABB / Max Loan Est.</label><strong>₹{loan['daily_abb']:,.0f} / ₹{loan['max_eligible_loan']:,.0f}</strong></div>
  </div>

  <h2>1. Mule Account Risk Assessment (10-Parameter Model)</h2>
  <table>
    <thead><tr><th>Account</th><th>Score (0-100)</th><th>Tier</th><th>S1 Flow-Through</th><th>S2 Velocity</th><th>S3 Lifestyle Absence</th><th>S4 Concentration</th><th>S5 Fan-In/Out</th><th>S6 Leakage</th><th>S7 Structuring</th><th>S8 Zero-Bal</th><th>S9 Burst</th><th>S10 Round</th><th>Top drivers (w&#183;S&#183;100)</th></tr></thead>
    <tbody>{mule_rows_html}</tbody>
  </table>

  <h2>2. Circular Money Laundering Loops (A → B → C → A)</h2>
  <table>
    <thead><tr><th>Hops</th><th>Circular Trail</th><th>Amount Band</th><th>Retention</th><th>Duration</th></tr></thead>
    <tbody>{loop_rows_html}</tbody>
  </table>
{network_section_html}
  <h2>{"4" if network_section_html else "3"}. Statutory &amp; ITR Compliance Red Flags</h2>
  <table>
    <thead><tr><th>Section</th><th>Severity</th><th>Observation</th><th>Amount</th></tr></thead>
    <tbody>{flag_rows_html}</tbody>
  </table>

  <h2>{"5" if network_section_html else "4"}. Section 63 BSA / Section 65B Digital Evidence Chain-of-Custody Certificate</h2>
  <div class="cert">
    <p>{html.escape(custody_text)}</p>
    <table>
      <thead><tr><th>Filename</th><th>Bank</th><th>Account No</th><th>Holder</th><th>SHA-256 Hash</th></tr></thead>
      <tbody>{stmt_rows_html}</tbody>
    </table>
  </div>
</body>
</html>"""


_PDF_PAGE_W, _PDF_PAGE_H = 595.0, 842.0
_PDF_MARGIN = 40.0
_PDF_BRAND = (0.059, 0.298, 0.506)  # #0f4c81
_PDF_FONTS = {"regular": "/F1", "bold": "/F2", "mono": "/F3"}
_PDF_CHAR_W = {"regular": 0.56, "bold": 0.61, "mono": 0.60}  # average glyph advance / font size
_PDF_TRANSLIT = {
    "₹": "Rs.", "→": "->", "←": "<-", "↔": "<->", "≥": ">=", "≤": "<=", "≈": "~", "×": "x", "✓": "Y",
    " ": " ", " ": " ", " ": " ",
}


def _pdf_encode(text: Any) -> bytes:
    """Text -> WinAnsi (cp1252) bytes for the standard Type1 fonts; unmappable glyphs are transliterated."""
    s = str(text if text is not None else "")
    for k, v in _PDF_TRANSLIT.items():
        if k in s:
            s = s.replace(k, v)
    out = bytearray()
    for ch in s:
        try:
            out.extend(ch.encode("cp1252"))
        except UnicodeEncodeError:
            base = unicodedata.normalize("NFKD", ch).encode("ascii", "ignore")
            out.extend(base or b"?")
    return bytes(out)


def _pdf_str(text: Any) -> bytes:
    raw = _pdf_encode(text)
    raw = raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")
    raw = re.sub(rb"[\x00-\x1f]", b" ", raw)
    return b"(" + raw + b")"


def _pdf_text(text: Any) -> str:
    """Backward-compatible helper: escaped, WinAnsi-safe text (without the surrounding parentheses)."""
    return _pdf_str(text)[1:-1].decode("cp1252")


def _wrap_to_width(text: Any, width_pt: float, size: float, font: str = "regular") -> List[str]:
    chars = max(4, int(width_pt / (size * _PDF_CHAR_W.get(font, 0.55))))
    out: List[str] = []
    for para in str(text if text is not None else "").split("\n"):
        out.extend(textwrap.wrap(para, width=chars, break_long_words=True, break_on_hyphens=False) or [""])
    return out


class _PdfDocument:
    """
    Small stdlib PDF 1.4 layout engine for dossiers and profiles (BE-18): headings, wrapped paragraphs,
    ruled tables with repeating shaded headers, KPI grids and horizontal bar rows, with page footers.
    Text stays uncompressed and extractable (each line is one Tj with WinAnsi encoding).
    """

    def __init__(self, title: str, footer: str) -> None:
        self.title = title
        self.footer = footer
        self.pages: List[List[bytes]] = []
        self.y = 0.0
        self.width = _PDF_PAGE_W - 2 * _PDF_MARGIN
        self._new_page()

    # ----------------------------------------------------------------------------- primitives
    def _new_page(self) -> None:
        self.pages.append([])
        self.y = _PDF_PAGE_H - _PDF_MARGIN

    def _ensure(self, height: float) -> bool:
        if self.y - height < _PDF_MARGIN + 18:
            self._new_page()
            return True
        return False

    def _op(self, op: str | bytes) -> None:
        self.pages[-1].append(op.encode("ascii") if isinstance(op, str) else op)

    def _text(self, x: float, y: float, text: Any, font: str = "regular", size: float = 9.0,
              color: Tuple[float, float, float] = (0.06, 0.09, 0.16)) -> None:
        if text in (None, ""):
            return
        self._op(
            f"BT {color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg {_PDF_FONTS.get(font, '/F1')} {size:.1f} Tf "
            f"1 0 0 1 {x:.2f} {y:.2f} Tm ".encode("ascii") + _pdf_str(text) + b" Tj ET"
        )

    def _rect(self, x: float, y: float, w: float, h: float, fill: Tuple[float, float, float]) -> None:
        self._op(f"{fill[0]:.3f} {fill[1]:.3f} {fill[2]:.3f} rg {x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def _line(self, x1: float, y1: float, x2: float, y2: float, gray: float = 0.8, width: float = 0.5) -> None:
        self._op(f"{gray:.2f} G {width:.2f} w {x1:.2f} {y1:.2f} m {x2:.2f} {y2:.2f} l S")

    # ----------------------------------------------------------------------------- blocks
    def title_block(self, title: str, subtitle_lines: Sequence[str] = ()) -> None:
        lines = _wrap_to_width(title, self.width - 20, 13.0, "bold")[:3]
        h = 14 + 16 * len(lines)
        self._rect(_PDF_MARGIN, self.y - h, self.width, h, _PDF_BRAND)
        for i, ln in enumerate(lines):
            self._text(_PDF_MARGIN + 10, self.y - 20 - 16 * i, ln, "bold", 13.0, (1, 1, 1))
        self.y -= h + 12
        for line in subtitle_lines:
            self.paragraph(line, size=8.5, color=(0.28, 0.33, 0.41))
        self.spacer(4)

    def heading(self, text: str) -> None:
        self._ensure(40)
        self.y -= 10
        self._text(_PDF_MARGIN, self.y - 11, text, "bold", 11.5, _PDF_BRAND)
        self.y -= 15
        self._line(_PDF_MARGIN, self.y, _PDF_MARGIN + self.width, self.y, gray=0.75, width=0.8)
        self.y -= 6

    def subheading(self, text: str) -> None:
        self._ensure(26)
        self.y -= 4
        self._text(_PDF_MARGIN, self.y - 9, text, "bold", 9.5)
        self.y -= 14

    def paragraph(self, text: Any, size: float = 9.0, font: str = "regular",
                  color: Tuple[float, float, float] = (0.06, 0.09, 0.16)) -> None:
        for line in _wrap_to_width(text, self.width, size, font):
            self._ensure(size + 4)
            self.y -= size + 3.5
            self._text(_PDF_MARGIN, self.y, line, font, size, color)

    def mono(self, text: Any, size: float = 7.5) -> None:
        self.paragraph(text, size=size, font="mono")

    def spacer(self, h: float = 6.0) -> None:
        self.y -= h

    def kpis(self, items: Sequence[Tuple[str, Any]], per_row: int = 4) -> None:
        box_w = (self.width - (per_row - 1) * 6) / per_row
        box_h = 34.0
        for i in range(0, len(items), per_row):
            self._ensure(box_h + 6)
            row = items[i:i + per_row]
            for j, (label, value) in enumerate(row):
                x = _PDF_MARGIN + j * (box_w + 6)
                self._rect(x, self.y - box_h, box_w, box_h, (0.97, 0.98, 0.99))
                self._line(x, self.y - box_h, x + box_w, self.y - box_h, gray=0.8)
                self._text(x + 6, self.y - 11, str(label).upper()[:34], "regular", 6.5, (0.39, 0.45, 0.55))
                val = str(value if value is not None else "")
                size = 10.0
                while size > 6.5 and len(val) * size * _PDF_CHAR_W["bold"] > box_w - 10:
                    size -= 0.5
                val = _wrap_to_width(val, box_w - 10, size, "bold")[0]
                self._text(x + 6, self.y - 26, val, "bold", size)
            self.y -= box_h + 6

    def table(self, headers: Sequence[str], rows: Sequence[Sequence[Any]], widths: Optional[Sequence[float]] = None,
              size: float = 7.5, align_right: Sequence[int] = ()) -> None:
        n = len(headers)
        if widths is None:
            widths = [1.0] * n
        total = float(sum(widths)) or 1.0
        col_w = [self.width * w / total for w in widths]
        pad = 3.0
        line_h = size + 2.5

        def draw_header() -> None:
            wrapped = [_wrap_to_width(h, col_w[i] - 2 * pad, size, "bold") for i, h in enumerate(headers)]
            h = max(len(w) for w in wrapped) * line_h + 4
            self._ensure(h + line_h + 4)
            self._rect(_PDF_MARGIN, self.y - h, self.width, h, _PDF_BRAND)
            x = _PDF_MARGIN
            for i, lines in enumerate(wrapped):
                for k, ln in enumerate(lines):
                    self._text(x + pad, self.y - 2 - (k + 1) * line_h + 2.5, ln, "bold", size, (1, 1, 1))
                x += col_w[i]
            self.y -= h

        draw_header()
        if not rows:
            self._ensure(line_h + 4)
            self._text(_PDF_MARGIN + pad, self.y - line_h, "No records.", "regular", size, (0.39, 0.45, 0.55))
            self.y -= line_h + 4
        for r_i, row in enumerate(rows):
            wrapped = [_wrap_to_width(_fmt_cell(row[i] if i < len(row) else ""), col_w[i] - 2 * pad, size)
                       for i in range(n)]
            h = max(len(w) for w in wrapped) * line_h + 3
            if self._ensure(h):
                draw_header()
            if r_i % 2 == 1:
                self._rect(_PDF_MARGIN, self.y - h, self.width, h, (0.96, 0.97, 0.98))
            x = _PDF_MARGIN
            for i, lines in enumerate(wrapped):
                for k, ln in enumerate(lines):
                    tx = x + pad
                    if i in align_right:
                        tx = x + col_w[i] - pad - len(ln) * size * _PDF_CHAR_W["regular"]
                    self._text(tx, self.y - (k + 1) * line_h + 1, ln, "regular", size)
                x += col_w[i]
            self.y -= h
            self._line(_PDF_MARGIN, self.y, _PDF_MARGIN + self.width, self.y, gray=0.85, width=0.3)
        self.y -= 6

    def bars(self, items: Sequence[Tuple[str, float, str]], max_value: Optional[float] = None,
             color: Tuple[float, float, float] = _PDF_BRAND) -> None:
        """Horizontal bar rows: (label, value, value_text)."""
        if not items:
            return
        top = max_value if max_value else max((v for _, v, _ in items), default=0.0)
        label_w, value_w = self.width * 0.34, self.width * 0.18
        bar_w = self.width - label_w - value_w - 8
        for label, value, text in items:
            self._ensure(13)
            self.y -= 12
            self._text(_PDF_MARGIN, self.y + 2, _wrap_to_width(label, label_w - 4, 7.5)[0], "regular", 7.5)
            self._rect(_PDF_MARGIN + label_w, self.y + 1, bar_w, 7, (0.93, 0.94, 0.96))
            frac = max(0.0, min(1.0, (value / top) if top else 0.0))
            if frac > 0:
                self._rect(_PDF_MARGIN + label_w, self.y + 1, max(0.8, bar_w * frac), 7, color)
            self._text(_PDF_MARGIN + label_w + bar_w + 6, self.y + 2, text, "regular", 7.5)
        self.y -= 6

    # ----------------------------------------------------------------------------- output
    def render(self) -> bytes:
        total = len(self.pages)
        streams: List[bytes] = []
        for p_idx, ops in enumerate(self.pages, start=1):
            foot = [
                f"0.80 G 0.50 w {_PDF_MARGIN:.2f} 32 m {_PDF_PAGE_W - _PDF_MARGIN:.2f} 32 l S".encode("ascii"),
                b"BT 0.390 0.450 0.550 rg /F1 7.5 Tf 1 0 0 1 40.00 22.00 Tm "
                + _pdf_str(f"{self.footer} - Page {p_idx} of {total}") + b" Tj ET",
            ]
            streams.append(b"\n".join(ops + foot))

        info = b"<< /Title " + _pdf_str(self.title) + b" /Producer (CypherSOL) >>"
        objects: List[bytes] = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"",  # Pages placeholder
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier /Encoding /WinAnsiEncoding >>",
            info,
        ]
        page_ids: List[int] = []
        for stream in streams:
            page_id = len(objects) + 1
            page_ids.append(page_id)
            objects.append(
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_PDF_PAGE_W:.0f} {_PDF_PAGE_H:.0f}] "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R /F3 5 0 R >> >> /Contents {page_id + 1} 0 R >>"
                ).encode("ascii")
            )
            objects.append(f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream")
        objects[1] = (
            f"<< /Type /Pages /Kids [ {' '.join(f'{pid} 0 R' for pid in page_ids)} ] /Count {len(page_ids)} >>"
        ).encode("ascii")

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets: List[int] = []
        for idx, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out.extend(f"{idx} 0 obj\n".encode("ascii") + body + b"\nendobj\n")
        xref_pos = len(out)
        out.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("ascii"))
        for off in offsets:
            out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
        out.extend(
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R /Info 6 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
        )
        return bytes(out)


def _fmt_cell(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, float):
        return f"{v:,.2f}"
    if isinstance(v, int):
        return f"{v:,}"
    return str(v)


def _inr(v: Any) -> str:
    try:
        return f"Rs. {float(v):,.2f}"
    except (TypeError, ValueError):
        return "-"


def _build_text_pdf(lines: Sequence[Tuple[str, str, float]], footer: str) -> bytes:
    """Backward-compatible plain line writer (pre-wrapped (text, font, size) lines) on the new engine."""
    doc = _PdfDocument(footer, footer)
    for text, font, size in lines:
        if text:
            doc.paragraph(text, size=size, font=font)
        else:
            doc.spacer(size)
    return doc.render()


def _mule_param_rows(m: MuleRiskAssessment) -> List[List[Any]]:
    vec = getattr(m, "explainability", None) or []
    rows: List[List[Any]] = []
    for item in vec:
        rows.append([
            item.get("label", item.get("key")),
            f"{float(item.get('weight', 0)) * 100:.0f}%",
            _raw_metric_text(item),
            f"{float(item.get('score', 0)):.2f}",
            f"{float(item.get('contribution', 0)):.2f}",
            int(item.get("evidence_count", len(item.get("evidence_tx_ids", []) or []))),
        ])
    return rows


def _raw_metric_text(item: Dict[str, Any]) -> str:
    raw = item.get("raw") or {}
    parts = []
    for k, v in raw.items():
        if isinstance(v, float):
            v = f"{v:,.2f}"
        parts.append(f"{k.replace('_', ' ')}: {v}")
    return "; ".join(parts[:4])


def export_forensic_dossier_pdf(
    case: InvestigationCase,
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    entities: List[ResolvedEntity],
    mule_assessments: List[MuleRiskAssessment],
    llm_assisted: Optional[bool] = None,
    llm_provider: Optional[str] = None,
    loops: Optional[List[CircularLoop]] = None,
) -> bytes:
    """Dedicated courtroom dossier PDF (BE-18): laid-out KPI grid, tables, explainability and certificate."""
    itr = generate_itr_compliance_report(transactions, statements=statements)
    loan = compute_loan_eligibility_report(transactions, statements=statements)
    if loops is None:
        loops = detect_circular_loops(statements, transactions, entities)
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    doc = _PdfDocument(f"CypherSOL Dossier - {case.title}", f"CypherSOL Forensic Dossier - {case.case_id}")
    doc.title_block(
        f"CypherSOL Forensic & Financial Dossier - {case.title}",
        [
            f"Case ID: {case.case_id} | Agency/Firm: {case.agency_or_firm_name or 'Local Workspace'} | "
            f"Mode: {case.mode.value} | Generated: {now_iso}",
        ],
    )
    doc.kpis([
        ("Statements", len(statements)),
        ("Transactions", len(transactions)),
        ("Resolved entities", len(entities)),
        ("Circular loops", len(loops)),
        ("Gross credits", _inr(itr["reconciliation"]["gross_credits"])),
        ("Gross debits", _inr(itr["reconciliation"]["gross_debits"])),
        ("Daily ABB", _inr(loan["daily_abb"])),
        ("Critical mule accounts", sum(1 for m in mule_assessments if m.risk_tier == MuleRiskTier.CRITICAL)),
    ])

    doc.heading("1. Mule Account Risk Assessment (10-Parameter Model)")
    doc.table(
        ["Account", "Bank", "Score", "Tier", "S1", "S2", "S3", "S4", "S5", "S6", "S7", "S8", "S9", "S10"],
        [
            [
                f"{m.account_holder_name} ({m.account_number})", m.bank_name, f"{m.composite_score:.1f}", m.risk_tier.value,
                *[f"{v:.2f}" for v in (
                    m.parameters.flow_through_ratio, m.parameters.velocity_hold_time,
                    m.parameters.lifestyle_spending_absence, m.parameters.counterparty_concentration,
                    m.parameters.fan_in_fan_out_pattern, m.parameters.cash_crypto_wallet_leakage,
                    m.parameters.structuring_smurfing, m.parameters.zero_balance_oscillation,
                    m.parameters.burst_dormancy_pattern, m.parameters.round_number_anomalies,
                )],
            ]
            for m in mule_assessments
        ],
        widths=[3.2, 1.6, 0.9, 1.3] + [0.62] * 10,
    )
    for m in mule_assessments:
        if m.risk_tier == MuleRiskTier.LOW_RISK or not getattr(m, "explainability", None):
            continue
        doc.subheading(f"Explainability - {m.account_holder_name} ({m.account_number}): {m.composite_score:.1f} / 100, {m.risk_tier.value}")
        doc.table(["Parameter", "Weight", "Raw metrics", "S_k", "Contribution", "Evidence"],
                  _mule_param_rows(m), widths=[2.2, 0.8, 4.3, 0.6, 1.4, 0.9], size=7.0)

    doc.heading("2. Circular Money Laundering Loops")
    doc.table(
        ["Hops", "Circular trail", "Amount band", "Retention", "Duration", "Start"],
        [
            [lp.hop_count, " -> ".join(lp.entities), f"{_inr(lp.min_amount)} - {_inr(lp.max_amount)}",
             f"{lp.retention_ratio * 100:.1f}%", f"{lp.duration_hours:.1f} h", lp.start_date]
            for lp in loops
        ],
        widths=[0.6, 4.5, 2.6, 0.9, 0.9, 1.2],
    )

    doc.heading(f"3. Statutory & ITR Compliance Red Flags ({itr['financial_year']})")
    doc.table(
        ["Section", "Severity", "Observation", "Amount"],
        [[f["section"], f["severity"], f["title"], _inr(f["amount"])] for f in itr["statutory_red_flags"]],
        widths=[1.8, 0.9, 5.0, 1.6],
    )

    doc.heading("4. Section 63 BSA / Section 65B Chain-of-Custody Certificate")
    doc.paragraph(custody_processing_statement(transactions, llm_assisted, llm_provider))
    doc.spacer(4)
    for st in statements:
        doc.paragraph(
            f"{st.source_filename} | {st.bank_name} | A/c {st.account_number} | {st.account_holder_name} | "
            f"{st.period_start} to {st.period_end}", size=8.0, font="bold",
        )
        doc.mono(f"SHA-256: {st.sha256_hash}")
        doc.spacer(3)
    return doc.render()


# ============================================================================ per-entity profile exports
PROFILE_EXPORT_FORMATS = ("html", "xlsx", "pdf")


def _profile_name(profile: Dict[str, Any]) -> str:
    ent = profile.get("entity") or {}
    return str(ent.get("canonical_name") or ent.get("entity_id") or "Entity")


def _safe_filename(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_")[:60] or "profile"


def _profile_kpi_items(profile: Dict[str, Any]) -> List[Tuple[str, str]]:
    k = profile.get("kpis") or {}
    mules = profile.get("mule_assessments") or []
    top = max(mules, key=lambda m: m.get("composite_score", 0.0)) if mules else None
    derived = bool(profile.get("derived_from_counterparty_statements"))
    return [
        ("Opening balance", "n/a" if derived or k.get("opening_balance") is None else _inr(k.get("opening_balance"))),
        ("Closing balance", "n/a" if derived or k.get("closing_balance") is None else _inr(k.get("closing_balance"))),
        ("Total credits", _inr(k.get("total_credits", 0.0))),
        ("Total debits", _inr(k.get("total_debits", 0.0))),
        ("Net flow", _inr(k.get("net_flow", 0.0))),
        ("Mule score / tier", f"{top['composite_score']:.1f} / {top['risk_tier']}" if top else "Not scored"),
        ("Active accounts", str(k.get("active_accounts", 0))),
        ("Transactions", str(k.get("total_transactions", 0))),
        ("Daily ABB", "n/a" if k.get("daily_abb") is None else _inr(k.get("daily_abb"))),
        ("Fixed-date ABB", "n/a" if k.get("fixed_date_abb") is None else _inr(k.get("fixed_date_abb"))),
    ]


def _svg_bar_chart(items: Sequence[Tuple[str, float]], width: int = 640, color: str = "#0f4c81") -> str:
    """Horizontal bar chart as inline SVG (presentation attributes only, no CSS/script)."""
    if not items:
        return "<p class='muted'>No data.</p>"
    row_h, label_w, value_w = 20, 210, 120
    bar_w = width - label_w - value_w
    top = max((v for _, v in items), default=0.0) or 1.0
    h = row_h * len(items) + 6
    parts = [f'<svg class="chart" viewBox="0 0 {width} {h}" role="img" xmlns="http://www.w3.org/2000/svg">']
    for i, (label, v) in enumerate(items):
        y = i * row_h + 4
        w = max(1.0, bar_w * max(0.0, v) / top) if v > 0 else 0.0
        parts.append(
            f'<text x="{label_w - 6}" y="{y + 12}" font-size="11" text-anchor="end" fill="#334155">{html.escape(str(label)[:32])}</text>'
            f'<rect x="{label_w}" y="{y + 2}" width="{bar_w}" height="12" fill="#eef2f7"/>'
            f'<rect x="{label_w}" y="{y + 2}" width="{w:.1f}" height="12" fill="{color}"/>'
            f'<text x="{label_w + bar_w + 6}" y="{y + 12}" font-size="11" fill="#0f172a">{html.escape(_fmt_cell(round(v, 2)))}</text>'
        )
    parts.append("</svg>")
    return "".join(parts)


def _svg_monthly_chart(monthly: Sequence[Dict[str, Any]], width: int = 640, height: int = 220) -> str:
    """Grouped credit/debit columns per month with an ABB polyline."""
    if not monthly:
        return "<p class='muted'>No monthly activity.</p>"
    pad_l, pad_b, pad_t = 60, 34, 10
    plot_w, plot_h = width - pad_l - 10, height - pad_b - pad_t
    vals = [float(m.get("credit") or 0) for m in monthly] + [float(m.get("debit") or 0) for m in monthly]
    vals += [float(m.get("abb") or 0) for m in monthly if m.get("abb") is not None]
    top = max(vals + [1.0])
    slot = plot_w / len(monthly)
    bw = max(2.0, min(18.0, slot / 2 - 3))
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" xmlns="http://www.w3.org/2000/svg">',
             f'<line x1="{pad_l}" y1="{pad_t + plot_h}" x2="{width - 10}" y2="{pad_t + plot_h}" stroke="#94a3b8"/>',
             f'<text x="4" y="{pad_t + 10}" font-size="10" fill="#64748b">{html.escape(_fmt_cell(round(top)))}</text>']
    pts = []
    for i, m in enumerate(monthly):
        x0 = pad_l + i * slot + slot / 2
        for j, (key, color) in enumerate((("credit", "#16a34a"), ("debit", "#dc2626"))):
            v = float(m.get(key) or 0)
            h = plot_h * v / top
            parts.append(f'<rect x="{x0 - bw - 1 + j * (bw + 2):.1f}" y="{pad_t + plot_h - h:.1f}" width="{bw:.1f}" height="{h:.1f}" fill="{color}"/>')
        if m.get("abb") is not None:
            pts.append(f"{x0:.1f},{pad_t + plot_h - plot_h * float(m['abb']) / top:.1f}")
        parts.append(f'<text x="{x0:.1f}" y="{height - 18}" font-size="9" text-anchor="middle" fill="#475569">{html.escape(str(m.get("month", "")))}</text>')
    if len(pts) > 1:
        parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="#0f4c81" stroke-width="2"/>')
    parts.append(f'<text x="{pad_l}" y="{height - 4}" font-size="10" fill="#16a34a">Credits</text>'
                 f'<text x="{pad_l + 60}" y="{height - 4}" font-size="10" fill="#dc2626">Debits</text>'
                 f'<text x="{pad_l + 110}" y="{height - 4}" font-size="10" fill="#0f4c81">ABB</text></svg>')
    return "".join(parts)


def _svg_ego_network(profile: Dict[str, Any], width: int = 640, height: int = 360) -> str:
    """Ego network: subject in the centre, top senders on the left, top beneficiaries on the right."""
    senders = (profile.get("top_senders") or [])[:8]
    receivers = (profile.get("top_beneficiaries") or [])[:8]
    if not senders and not receivers:
        return "<p class='muted'>No counterparties.</p>"
    cx, cy = width / 2, height / 2
    top = max([p["amount"] for p in senders + receivers] + [1.0])
    parts = [f'<svg class="chart" viewBox="0 0 {width} {height}" role="img" xmlns="http://www.w3.org/2000/svg">']
    for side, items, color in ((-1, senders, "#16a34a"), (1, receivers, "#dc2626")):
        n = len(items)
        for i, p in enumerate(items):
            y = (height - 40) * (i + 0.5) / max(1, n) + 20
            x = cx + side * (width / 2 - 120)
            sw = 0.8 + 4.0 * float(p["amount"]) / top
            x1, x2 = (x, cx - 30) if side < 0 else (cx + 30, x)
            parts.append(f'<line x1="{x1:.1f}" y1="{y if side < 0 else cy:.1f}" x2="{x2:.1f}" y2="{cy if side < 0 else y:.1f}" stroke="{color}" stroke-width="{sw:.1f}" stroke-opacity="0.7"/>')
            parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="{color}"/>')
            anchor = "end" if side < 0 else "start"
            tx = x - 10 if side < 0 else x + 10
            parts.append(f'<text x="{tx:.1f}" y="{y + 4:.1f}" font-size="10" text-anchor="{anchor}" fill="#0f172a">{html.escape(str(p["party"])[:22])} ({html.escape(_fmt_cell(p["amount"]))})</text>')
    parts.append(f'<circle cx="{cx}" cy="{cy}" r="28" fill="#0f4c81"/>'
                 f'<text x="{cx}" y="{cy + 4}" font-size="10" font-weight="700" text-anchor="middle" fill="#ffffff">{html.escape(_profile_name(profile)[:14])}</text>'
                 f'<text x="10" y="14" font-size="10" fill="#16a34a">Inflow senders</text>'
                 f'<text x="{width - 10}" y="14" font-size="10" text-anchor="end" fill="#dc2626">Outflow beneficiaries</text></svg>')
    return "".join(parts)


def _html_table(headers: Sequence[str], rows: Sequence[Sequence[Any]], num_cols: Sequence[int] = ()) -> str:
    head = "".join(f"<th>{html.escape(str(h))}</th>" for h in headers)
    if not rows:
        return f"<table><thead><tr>{head}</tr></thead><tbody><tr><td colspan='{len(headers)}' class='muted'>No records.</td></tr></tbody></table>"
    body = []
    for r in rows:
        cells = "".join(
            f"<td class='num'>{html.escape(_fmt_cell(v))}</td>" if i in num_cols else f"<td>{html.escape(_fmt_cell(v))}</td>"
            for i, v in enumerate(r)
        )
        body.append(f"<tr>{cells}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


_EXPORT_CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: #0f172a; margin: 32px; line-height: 1.45; }
  h1, h2 { color: #0f4c81; border-bottom: 2px solid #e2e8f0; padding-bottom: 6px; }
  h3 { margin-bottom: 4px; }
  .muted { color: #64748b; font-size: 12px; }
  .kpi-grid { display: grid; grid-template-columns: repeat(5, 1fr); gap: 10px; margin: 16px 0; }
  .kpi { border: 1px solid #cbd5e1; border-radius: 8px; padding: 10px; background: #f8fafc; }
  .kpi span { font-size: 11px; text-transform: uppercase; color: #64748b; display: block; }
  .kpi strong { font-size: 16px; }
  table { width: 100%; border-collapse: collapse; margin: 10px 0 22px; font-size: 12.5px; }
  th, td { border: 1px solid #cbd5e1; padding: 6px 8px; text-align: left; vertical-align: top; }
  th { background: #0f4c81; color: #fff; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .chart { width: 100%; max-width: 760px; display: block; margin: 8px 0 18px; }
  .note { background: #fff7ed; border: 1px solid #fdba74; border-radius: 8px; padding: 10px; }
  .cert { background: #f0fdf4; border: 1px solid #86efac; border-radius: 8px; padding: 12px; margin-top: 18px; }
  @media print { body { margin: 12px; } }
"""


def export_profile_html(
    profile: Dict[str, Any],
    case: Optional[InvestigationCase] = None,
    llm_assisted: Optional[bool] = None,
    llm_provider: Optional[str] = None,
) -> str:
    """Self-contained printable HTML for one Individual Financial Profile (TG-17 / FR-TRACE-06).

    Pure renderer over the dict from ``build_individual_financial_profile``. No scripts, no inline style
    attributes (one <style> block), SVG charts with presentation attributes only.
    """
    if profile.get("error"):
        raise ValueError(str(profile["error"]))
    name = _profile_name(profile)
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    kpis = "".join(
        f"<div class='kpi'><span>{html.escape(lbl)}</span><strong>{html.escape(val)}</strong></div>"
        for lbl, val in _profile_kpi_items(profile)
    )
    derived_note = (
        "<p class='note'>No statement of this entity was uploaded: figures are derived from counterparty "
        "statements (read from the entity's side) and carry no balances.</p>"
        if profile.get("derived_from_counterparty_statements") else ""
    )
    mule_html = []
    for m in profile.get("mule_assessments") or []:
        vec = m.get("explainability") or []
        mule_html.append(
            f"<h3>{html.escape(str(m.get('account_holder_name', '')))} ({html.escape(str(m.get('account_number', '')))}) - "
            f"{float(m.get('composite_score', 0)):.1f} / 100, {html.escape(str(m.get('risk_tier', '')))}</h3>"
        )
        mule_html.append(_svg_bar_chart([(v.get("label", v.get("key")), float(v.get("contribution", 0))) for v in vec], color="#b45309"))
        mule_html.append(_html_table(
            ["Parameter", "Weight", "Raw metrics", "S_k", "Contribution", "Evidence tx"],
            [[v.get("label"), f"{float(v.get('weight', 0)) * 100:.0f}%", _raw_metric_text(v), f"{float(v.get('score', 0)):.2f}",
              f"{float(v.get('contribution', 0)):.2f}", int(v.get("evidence_count", 0))] for v in vec],
            num_cols=(3, 4, 5),
        ))
    monthly = profile.get("monthly_breakdown") or []
    cats = profile.get("category_breakdown") or []
    chans = profile.get("channel_mix") or []
    certificate = ""
    if case is not None or llm_assisted is not None:
        certificate = (
            f"<div class='cert'><b>Processing statement.</b> {html.escape(custody_processing_statement([], llm_assisted, llm_provider))}"
            + _html_table(["File", "Bank", "Account", "SHA-256"], [
                [s.get("source_filename"), s.get("bank_name"), s.get("account_number"), s.get("sha256_hash")]
                for s in profile.get("statements") or []
            ]) + "</div>"
        )
    case_line = (
        f"<b>Case:</b> {html.escape(case.title)} ({html.escape(case.case_id)}) &nbsp;|&nbsp; " if case is not None else ""
    )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>Financial Profile - {html.escape(name)}</title>
<style>{_EXPORT_CSS}</style>
</head>
<body>
  <h1>Individual Financial Profile - {html.escape(name)}</h1>
  <p class="muted">{case_line}<b>Entity ID:</b> {html.escape(str((profile.get('entity') or {}).get('entity_id', '')))} &nbsp;|&nbsp; <b>Generated:</b> {now_iso}</p>
  {derived_note}
  <div class="kpi-grid">{kpis}</div>

  <h2>1. Monthly Credit / Debit / ABB</h2>
  {_svg_monthly_chart(monthly)}
  {_html_table(["Month", "Credits", "Debits", "Net", "Txns", "ABB"], [[m.get("month"), m.get("credit"), m.get("debit"), m.get("net"), m.get("tx_count"), m.get("abb")] for m in monthly], num_cols=(1, 2, 3, 4, 5))}

  <h2>2. Category-wise Distribution</h2>
  {_svg_bar_chart([(c.get("category"), float(c.get("credit", 0)) + float(c.get("debit", 0))) for c in cats[:15]])}
  {_html_table(["ID", "Category", "Group", "Credits", "Debits", "Txns", "Share %"], [[c.get("category_id"), c.get("category"), c.get("group"), c.get("credit"), c.get("debit"), c.get("tx_count"), c.get("share_pct")] for c in cats], num_cols=(3, 4, 5, 6))}

  <h2>3. Payment Channel Mix</h2>
  {_svg_bar_chart([(c.get("channel"), float(c.get("amount", 0))) for c in chans], color="#0e7490")}
  {_html_table(["Channel", "Credits", "Debits", "Total", "Txns", "Share %"], [[c.get("channel"), c.get("credit"), c.get("debit"), c.get("amount"), c.get("tx_count"), c.get("share_pct")] for c in chans], num_cols=(1, 2, 3, 4, 5))}

  <h2>4. Counterparty Network (Top 15 Senders and Beneficiaries)</h2>
  {_svg_ego_network(profile)}
  <h3>Top inflow senders</h3>
  {_html_table(["Party", "Amount", "Txns"], [[p.get("party"), p.get("amount"), p.get("tx_count")] for p in profile.get("top_senders") or []], num_cols=(1, 2))}
  <h3>Top outflow beneficiaries</h3>
  {_html_table(["Party", "Amount", "Txns"], [[p.get("party"), p.get("amount"), p.get("tx_count")] for p in profile.get("top_beneficiaries") or []], num_cols=(1, 2))}

  <h2>5. Mule Risk Explainability</h2>
  {"".join(mule_html) or "<p class='muted'>No mule assessment for this entity.</p>"}

  <h2>6. Average Bank Balance by Account</h2>
  {_html_table(["Account", "Bank", "From", "To", "Days", "Daily ABB"], [[a.get("account_number"), a.get("bank_name"), a.get("period_start"), a.get("period_end"), a.get("days"), a.get("daily_abb")] for a in profile.get("abb_by_account") or []], num_cols=(4, 5))}
  {certificate}
</body>
</html>"""


def export_profile_xlsx(profile: Dict[str, Any]) -> bytes:
    """Multi-sheet Excel workbook for one Individual Financial Profile (TG-17)."""
    if profile.get("error"):
        raise ValueError(str(profile["error"]))
    ent = profile.get("entity") or {}
    summary: List[Sequence[Any]] = [["Field", "Value"], ["Entity", _profile_name(profile)], ["Entity ID", ent.get("entity_id", "")],
                                    ["Derived from counterparty statements", bool(profile.get("derived_from_counterparty_statements"))],
                                    ["Generated At (UTC)", dt.datetime.now(dt.timezone.utc).isoformat()]]
    for k, v in (profile.get("kpis") or {}).items():
        summary.append([k.replace("_", " ").title(), v if v is not None else ""])
    mule_rows: List[Sequence[Any]] = [["Account", "Holder", "Composite", "Tier", "Parameter", "Weight", "Raw Metric", "Raw Details",
                                       "Score S_k", "Contribution", "Evidence Count", "Evidence tx_ids"]]
    for m in profile.get("mule_assessments") or []:
        for v in m.get("explainability") or []:
            mule_rows.append([m.get("account_number"), m.get("account_holder_name"), m.get("composite_score"), m.get("risk_tier"),
                              v.get("label"), v.get("weight"), v.get("raw_metric") if v.get("raw_metric") is not None else "",
                              _raw_metric_text(v), v.get("score"), v.get("contribution"), v.get("evidence_count"),
                              ", ".join(v.get("evidence_tx_ids") or [])])
    sheets: List[Tuple[str, List[Sequence[Any]]]] = [
        ("Profile Summary", summary),
        ("Monthly", [["Month", "Credits", "Debits", "Net", "Txns", "ABB"]] + [
            [m.get("month"), m.get("credit"), m.get("debit"), m.get("net"), m.get("tx_count"), m.get("abb") if m.get("abb") is not None else ""]
            for m in profile.get("monthly_breakdown") or []]),
        ("Categories", [["Category ID", "Category", "Group", "Credits", "Debits", "Txns", "Share %"]] + [
            [c.get("category_id") if c.get("category_id") is not None else "", c.get("category"), c.get("group", ""), c.get("credit"),
             c.get("debit"), c.get("tx_count"), c.get("share_pct", "")] for c in profile.get("category_breakdown") or []]),
        ("Channels", [["Channel", "Credits", "Debits", "Total", "Txns", "Share %"]] + [
            [c.get("channel"), c.get("credit", ""), c.get("debit", ""), c.get("amount"), c.get("tx_count"), c.get("share_pct", "")]
            for c in profile.get("channel_mix") or []]),
        ("Top Senders", [["Party", "Entity ID", "Amount", "Txns"]] + [
            [p.get("party"), p.get("entity_id") or "", p.get("amount"), p.get("tx_count", "")] for p in profile.get("top_senders") or []]),
        ("Top Beneficiaries", [["Party", "Entity ID", "Amount", "Txns"]] + [
            [p.get("party"), p.get("entity_id") or "", p.get("amount"), p.get("tx_count", "")] for p in profile.get("top_beneficiaries") or []]),
        ("Mule Explainability", mule_rows),
        ("ABB by Account", [["Account", "Bank", "From", "To", "Days", "Daily ABB"]] + [
            [a.get("account_number"), a.get("bank_name"), a.get("period_start"), a.get("period_end"), a.get("days"),
             a.get("daily_abb") if a.get("daily_abb") is not None else ""] for a in profile.get("abb_by_account") or []]),
        ("Statements", [["File", "Bank", "Account", "Holder", "From", "To", "SHA-256"]] + [
            [s.get("source_filename"), s.get("bank_name"), s.get("account_number"), s.get("account_holder_name"),
             s.get("period_start"), s.get("period_end"), s.get("sha256_hash")] for s in profile.get("statements") or []]),
        ("Recent Transactions", [["Date", "Account", "Channel", "Narration", "Counterparty", "Debit", "Credit", "Balance", "Category", "tx_id"]] + [
            [t.get("txn_date"), t.get("source_account_number") or "", t.get("payment_channel"), t.get("raw_narration"),
             t.get("extracted_counterparty") or "", _num(t.get("debit")), _num(t.get("credit")), _num(t.get("running_balance")),
             t.get("category_name"), t.get("tx_id")] for t in profile.get("recent_transactions") or []]),
    ]
    return write_xlsx_bytes(sheets)


def _num(v: Any) -> Any:
    try:
        return float(v)
    except (TypeError, ValueError):
        return ""


def export_profile_pdf(
    profile: Dict[str, Any],
    case: Optional[InvestigationCase] = None,
    llm_assisted: Optional[bool] = None,
    llm_provider: Optional[str] = None,
) -> bytes:
    """Native PDF for one Individual Financial Profile (TG-17), built on the dossier PDF engine."""
    if profile.get("error"):
        raise ValueError(str(profile["error"]))
    name = _profile_name(profile)
    ent = profile.get("entity") or {}
    now_iso = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    doc = _PdfDocument(f"Financial Profile - {name}", f"CypherSOL Financial Profile - {ent.get('entity_id', '')}")
    sub = [f"Entity ID: {ent.get('entity_id', '')} | Generated: {now_iso}"]
    if case is not None:
        sub.insert(0, f"Case: {case.title} ({case.case_id})")
    if profile.get("derived_from_counterparty_statements"):
        sub.append("No statement of this entity was uploaded: figures are derived from counterparty statements and carry no balances.")
    doc.title_block(f"Individual Financial Profile - {name}", sub)
    doc.kpis(_profile_kpi_items(profile), per_row=5)

    monthly = profile.get("monthly_breakdown") or []
    doc.heading("1. Monthly Credit / Debit / ABB")
    doc.table(["Month", "Credits", "Debits", "Net", "Txns", "ABB"],
              [[m.get("month"), _inr(m.get("credit")), _inr(m.get("debit")), _inr(m.get("net")), m.get("tx_count"),
                _inr(m.get("abb")) if m.get("abb") is not None else "-"] for m in monthly],
              widths=[1, 1.6, 1.6, 1.6, 0.7, 1.6], align_right=(1, 2, 3, 4, 5))

    cats = profile.get("category_breakdown") or []
    doc.heading("2. Category-wise Distribution")
    doc.bars([(c.get("category"), float(c.get("credit", 0)) + float(c.get("debit", 0)), f"{c.get('share_pct', 0)}%") for c in cats[:12]])
    doc.table(["ID", "Category", "Credits", "Debits", "Txns", "Share %"],
              [[c.get("category_id"), c.get("category"), _inr(c.get("credit")), _inr(c.get("debit")), c.get("tx_count"), c.get("share_pct")] for c in cats],
              widths=[0.5, 3.2, 1.5, 1.5, 0.6, 0.8], align_right=(2, 3, 4, 5))

    chans = profile.get("channel_mix") or []
    doc.heading("3. Payment Channel Mix")
    doc.bars([(c.get("channel"), float(c.get("amount", 0)), f"{c.get('share_pct', 0)}%") for c in chans], color=(0.055, 0.455, 0.565))
    doc.table(["Channel", "Credits", "Debits", "Total", "Txns", "Share %"],
              [[c.get("channel"), _inr(c.get("credit")), _inr(c.get("debit")), _inr(c.get("amount")), c.get("tx_count"), c.get("share_pct")] for c in chans],
              widths=[1.2, 1.5, 1.5, 1.5, 0.6, 0.8], align_right=(1, 2, 3, 4, 5))

    doc.heading("4. Top 15 Inflow Senders and Outflow Beneficiaries")
    doc.subheading("Inflow senders")
    doc.table(["Party", "Amount", "Txns"], [[p.get("party"), _inr(p.get("amount")), p.get("tx_count", "")] for p in profile.get("top_senders") or []],
              widths=[4, 1.6, 0.6], align_right=(1, 2))
    doc.subheading("Outflow beneficiaries")
    doc.table(["Party", "Amount", "Txns"], [[p.get("party"), _inr(p.get("amount")), p.get("tx_count", "")] for p in profile.get("top_beneficiaries") or []],
              widths=[4, 1.6, 0.6], align_right=(1, 2))

    doc.heading("5. Mule Risk Explainability")
    mules = profile.get("mule_assessments") or []
    if not mules:
        doc.paragraph("No mule assessment for this entity.")
    for m in mules:
        doc.subheading(f"{m.get('account_holder_name')} ({m.get('account_number')}): {float(m.get('composite_score', 0)):.1f} / 100, {m.get('risk_tier')}")
        vec = m.get("explainability") or []
        doc.bars([(v.get("label"), float(v.get("contribution", 0)), f"{float(v.get('contribution', 0)):.2f}") for v in vec],
                 max_value=max([float(v.get("weight", 0)) * 100 for v in vec] + [1.0]), color=(0.706, 0.325, 0.035))
        doc.table(["Parameter", "Weight", "Raw metrics", "S_k", "Contribution", "Evidence"],
                  [[v.get("label"), f"{float(v.get('weight', 0)) * 100:.0f}%", _raw_metric_text(v), f"{float(v.get('score', 0)):.2f}",
                    f"{float(v.get('contribution', 0)):.2f}", v.get("evidence_count", 0)] for v in vec],
                  widths=[2.2, 0.8, 4.3, 0.6, 1.4, 0.9], size=7.0)

    if profile.get("abb_by_account"):
        doc.heading("6. Average Bank Balance by Account")
        doc.table(["Account", "Bank", "From", "To", "Days", "Daily ABB"],
                  [[a.get("account_number"), a.get("bank_name"), a.get("period_start"), a.get("period_end"), a.get("days"),
                    _inr(a.get("daily_abb")) if a.get("daily_abb") is not None else "-"] for a in profile["abb_by_account"]],
                  widths=[1.6, 1.6, 1.1, 1.1, 0.6, 1.4], align_right=(4, 5))

    if case is not None or llm_assisted is not None:
        doc.heading("Processing Statement and Source Digests")
        doc.paragraph(custody_processing_statement([], llm_assisted, llm_provider))
        for s in profile.get("statements") or []:
            doc.paragraph(f"{s.get('source_filename')} | {s.get('bank_name')} | A/c {s.get('account_number')}", size=8.0, font="bold")
            doc.mono(f"SHA-256: {s.get('sha256_hash')}")
    return doc.render()


def export_entity_profile(
    profile: Dict[str, Any],
    fmt: str,
    case: Optional[InvestigationCase] = None,
    llm_assisted: Optional[bool] = None,
    llm_provider: Optional[str] = None,
) -> Tuple[bytes, str, str]:
    """Dispatch a profile export. Returns (body, content_type, filename). Raises ValueError on bad fmt/profile."""
    fmt = (fmt or "").lower().strip()
    stem = "profile_" + _safe_filename(_profile_name(profile) if not profile.get("error") else "entity")
    if fmt == "html":
        return (export_profile_html(profile, case, llm_assisted, llm_provider).encode("utf-8"),
                "text/html; charset=utf-8", f"{stem}.html")
    if fmt == "xlsx":
        return (export_profile_xlsx(profile),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", f"{stem}.xlsx")
    if fmt == "pdf":
        return export_profile_pdf(profile, case, llm_assisted, llm_provider), "application/pdf", f"{stem}.pdf"
    raise ValueError(f"Unsupported profile export format '{fmt}'; expected one of {', '.join(PROFILE_EXPORT_FORMATS)}")


# ============================================================================ ITR workbook (EDGE-19)
def _itr_sheets(itr: Dict[str, Any]) -> List[Tuple[str, List[Sequence[Any]]]]:
    rec = itr.get("reconciliation") or {}
    summary: List[Sequence[Any]] = [["Item", "Value (INR)"], ["Financial Year", itr.get("financial_year", "")],
                                    ["Available FYs", ", ".join(itr.get("available_fys") or [])],
                                    ["Transactions in FY", itr.get("transaction_count", 0)],
                                    ["Excluded (other FY)", itr.get("excluded_other_fy_count", 0)],
                                    ["Suspense pending", itr.get("suspense_pending_count", 0)]]
    for section, title in (("reconciliation", "Reconciliation"), ("income_heads", "Income"),
                           ("deduction_candidates", "Deduction"), ("cash_summary", "Cash")):
        for k, v in (itr.get(section) or {}).items():
            summary.append([f"{title}: {k.replace('_', ' ')}", v])
    flags: List[Sequence[Any]] = [["Check ID", "Section", "Severity", "Title", "Amount", "Detail"]]
    detail_rows: List[Sequence[Any]] = [["Section", "Date / Payee", "Party", "Amount", "Notes"]]
    for f in itr.get("statutory_red_flags") or []:
        flags.append([f.get("check_id", ""), f.get("section"), f.get("severity"), f.get("title"), f.get("amount"),
                      f.get("detail", "")])
        for o in f.get("occurrences") or []:
            detail_rows.append([f.get("section"), o.get("date", ""), o.get("party", ""), o.get("amount", ""), ""])
        for p in f.get("payees") or []:
            detail_rows.append([f.get("section"), p.get("payee", ""), p.get("payee_key", ""), p.get("amount", ""),
                                "; ".join(p.get("reasons") or [])])
    cats: List[Sequence[Any]] = [["Category ID", "Group", "Category", "ITR Section", "Credit", "Debit", "Txns"]]
    for c in itr.get("category_schedule") or []:
        cats.append([c["category_id"], c["group"], c["category_name"], c["itr_section"], c["credit"], c["debit"], c["tx_count"]])
    checks: List[Sequence[Any]] = [["Check ID", "Group", "Section", "Title", "Rule", "Status", "Flags", "Severity",
                                    "Amount", "Caveats"]]
    for c in itr.get("checks_run") or []:
        checks.append([c.get("id"), c.get("group"), c.get("section"), c.get("title"), c.get("rule"), c.get("status"),
                       c.get("flag_count"), c.get("severity") or "", c.get("amount"), "; ".join(c.get("caveats") or [])])
    deds: List[Sequence[Any]] = [["Section", "Title", "Amount", "Txns", "Statutory Cap", "Eligible up to Cap"]]
    for d in itr.get("deductions") or []:
        deds.append([d.get("section"), d.get("title"), d.get("amount"), d.get("tx_count"),
                     d.get("statutory_cap") if d.get("statutory_cap") is not None else "",
                     d.get("eligible_upto_cap") if d.get("eligible_upto_cap") is not None else ""])
    if itr.get("deduction_note"):
        deds.append(["Note", itr.get("deduction_note"), "", "", "", ""])
    waterfall: List[Sequence[Any]] = [["Step", "Label", "Kind", "Amount", "Running Balance"]]
    for w in itr.get("reconciliation_waterfall") or []:
        waterfall.append([w.get("step"), w.get("label"), w.get("kind"), w.get("amount"), w.get("running_balance")])
    return [("ITR Summary", summary), ("ITR Checks Run", checks), ("ITR Red Flags", flags),
            ("ITR Flag Details", detail_rows), ("ITR Deductions", deds), ("ITR Reconciliation", waterfall),
            ("ITR Category Schedule", cats)]


def export_itr_workbook_xlsx(
    transactions: List[NormalizedTransaction],
    statements: Optional[List[BankAccountStatement]] = None,
    fy: Optional[str] = None,
) -> bytes:
    """FR-EDGE-03 FY compliance workbook. Raises ValueError for an FY with no transactions."""
    itr = generate_itr_compliance_report(transactions, fy=fy, statements=statements)
    return write_xlsx_bytes(_itr_sheets(itr))


def _dict_rows(items: Sequence[Any]) -> List[Sequence[Any]]:
    """List of flat dicts -> header + rows (union of keys in first-seen order; lists joined)."""
    dicts = [d for d in items if isinstance(d, dict)]
    keys: List[str] = []
    for d in dicts:
        for k in d:
            if k not in keys:
                keys.append(k)
    rows: List[Sequence[Any]] = [[k.replace("_", " ").title() for k in keys] or ["(none)"]]
    for d in dicts:
        rows.append([
            "; ".join(str(x) for x in d.get(k)) if isinstance(d.get(k), (list, tuple))
            else ("" if d.get(k) is None or isinstance(d.get(k), dict) else d.get(k))
            for k in keys
        ])
    return rows


def _loan_sheets(loan: Dict[str, Any]) -> List[Tuple[str, List[Sequence[Any]]]]:
    scalar = [["Metric", "Value"]] + [
        [k.replace("_", " "), v] for k, v in loan.items() if not isinstance(v, (list, dict))
    ]
    monthly = [["Month", "Total Credits", "Net Business Credits", "Total Debits", "Net Cash Flow", "EMI Outflow",
                "Daily ABB", "Fixed-Date ABB", "Min Balance", "Max Balance", "Days", "Txns"]]
    for m in loan.get("monthly_breakdown") or []:
        monthly.append([m.get("month"), m.get("total_credits"), m.get("net_business_credits"), m.get("total_debits"),
                        m.get("net_cash_flow"), m.get("emi_outflow"), m.get("daily_abb"), m.get("fixed_date_abb"),
                        m.get("min_balance"), m.get("max_balance"), m.get("days_covered"), m.get("tx_count")])
    emis = _dict_rows(loan.get("emi_obligations") or [])
    bounces = _dict_rows(loan.get("bounce_events") or [])
    return [("Loan Eligibility", scalar), ("Loan Monthly", monthly), ("Loan EMI Obligations", emis), ("Loan Bounces", bounces)]


# ============================================================================ Excel-to-Tally (EDGE-13)
def export_tally_workbook_xlsx(
    case_title: Optional[str],
    statements: List[BankAccountStatement],
    transactions: List[NormalizedTransaction],
    include_suspense: bool = False,
    *,
    company_name: Optional[str] = None,
    use_trade_vouchers: Optional[bool] = False,
    voucher_prefix: Optional[str] = "CS",
    ledger_map: Any = None,
) -> bytes:
    """
    Excel-to-Tally workbook (FR-EDGE-02): the same vouchers and masters as the Tally XML export (it is
    derived from ``generate_tally_xml`` so both stay identical), laid out for Excel import utilities:
    Vouchers (one row per voucher with Dr/Cr ledgers), Ledger Entries, Ledger Masters, Groups, Summary.
    """
    from cyphersol.edge.tally_exporter import generate_tally_xml

    xml_text, stats = generate_tally_xml(
        case_title, statements, transactions, include_suspense,
        company_name=company_name, use_trade_vouchers=use_trade_vouchers, voucher_prefix=voucher_prefix,
        ledger_map=ledger_map,
    )
    root = ET.fromstring(xml_text.encode("utf-8"))
    vouchers: List[Sequence[Any]] = [["Date", "Voucher Type", "Voucher No", "Debit Ledger", "Credit Ledger", "Amount", "Narration", "GUID"]]
    entries: List[Sequence[Any]] = [["Voucher No", "Date", "Voucher Type", "Ledger", "Dr/Cr", "Amount", "Tally Signed Amount"]]
    ledgers: List[Sequence[Any]] = [["Ledger Name", "Under (Parent Group)"]]
    groups: List[Sequence[Any]] = [["Group Name", "Under (Parent Group)"]]
    for g in root.iter("GROUP"):
        groups.append([g.get("NAME", ""), (g.findtext("PARENT") or "").strip()])
    for led in root.iter("LEDGER"):
        ledgers.append([led.get("NAME", ""), (led.findtext("PARENT") or "").strip()])
    for v in root.iter("VOUCHER"):
        raw_date = (v.findtext("DATE") or "").strip()
        date_iso = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}" if len(raw_date) == 8 else raw_date
        vtype = (v.findtext("VOUCHERTYPENAME") or "").strip()
        vno = (v.findtext("VOUCHERNUMBER") or "").strip()
        dr_ledger = cr_ledger = ""
        amount = 0.0
        for e in v.iter("ALLLEDGERENTRIES.LIST"):
            name = (e.findtext("LEDGERNAME") or "").strip()
            try:
                signed = float((e.findtext("AMOUNT") or "0").strip())
            except ValueError:
                signed = 0.0
            is_dr = (e.findtext("ISDEEMEDPOSITIVE") or "").strip().lower() == "yes"
            if is_dr:
                dr_ledger = name
            else:
                cr_ledger = name
            amount = max(amount, abs(signed))
            entries.append([vno, date_iso, vtype, name, "Dr" if is_dr else "Cr", abs(signed), signed])
        vouchers.append([date_iso, vtype, vno, dr_ledger, cr_ledger, round(amount, 2),
                         (v.findtext("NARRATION") or "").strip(), (v.findtext("GUID") or "").strip()])
    summary: List[Sequence[Any]] = [["Item", "Value"], ["Case", case_title or ""], ["Tally company", company_name or "(loaded company)"]]
    for k, val in stats.items():
        if isinstance(val, dict):
            for kk, vv in val.items():
                summary.append([f"{k.replace('_', ' ')}: {kk}", vv])
        elif k not in ("company_name", "case_title"):
            summary.append([k.replace("_", " "), val if val is not None else ""])
    return write_xlsx_bytes([
        ("Vouchers", vouchers),
        ("Ledger Entries", entries),
        ("Ledger Masters", ledgers),
        ("Groups", groups),
        ("Summary", summary),
    ])
