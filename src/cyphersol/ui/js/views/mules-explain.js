/* Pure (DOM-free) explainability model for the mule view (FR-TRACE-05, report TF-15).
 *
 * The server's assessment carries `explainability[]`: one entry per parameter with
 *   {key, rank, label, weight, score (S, 0–1), contribution (w·S·100), raw_metric, raw{…}, formula,
 *    evidence_tx_ids[], evidence_count}.
 * explainRows() normalises that into the 10 SRS parameters in weight order (MULE_PARAMS), falling back to
 * `parameters` / `weighted_contributions` for older payloads, and describes each raw metric in plain words.
 * Tested under node by tests/test_p1_ui_data.py.
 */
import { MULE_PARAMS } from "../core/constants.js";
import { hours, inrCompact, num, plural } from "../core/format.js";
import { idChunks } from "./transactions-meta.js";

const fin = (v) => v != null && v !== "" && Number.isFinite(Number(v));
const N = (v) => (fin(v) ? Number(v) : 0);
const pctOf = (v, d = 1) => `${num(N(v) * 100, d)}%`;

/** One-line description of the raw metric behind S for each parameter. Never throws on missing keys. */
export function describeRaw(key, raw, rawMetric) {
  const r = raw && typeof raw === "object" ? raw : {};
  switch (key) {
    case "flow_through_ratio":
      return `${inrCompact(r.total_credits)} in, ${inrCompact(r.total_debits)} out · ratio ${num(r.flow_through_ratio ?? rawMetric, 3)}${r.turnover_gate_passed === false ? ` · below the ${inrCompact(r.turnover_gate)} turnover gate` : ""}`;
    case "velocity_hold_time":
      return fin(r.median_hold_hours ?? rawMetric)
        ? `Median hold ${hours(r.median_hold_hours ?? rawMetric)}${fin(r.fifo_matches) ? ` · ${plural(N(r.fifo_matches), "FIFO match", "FIFO matches")}` : ""}${fin(r.matches_within_24h) ? `, ${num(r.matches_within_24h, 0)} within 24 h` : ""}`
        : "No credit could be matched to a later debit";
    case "lifestyle_spending_absence":
      return `${plural(N(r.lifestyle_txn_count), "lifestyle txn")} over ${num(r.active_months, 1)} active months (${num(r.lifestyle_txns_per_month, 1)}/month vs ${num(r.expected_per_month ?? 6, 0)} expected)`;
    case "counterparty_concentration":
      return `HHI ${num(r.hhi ?? rawMetric, 3)} across ${plural(N(r.counterparties), "counterparty", "counterparties")}${fin(r.top_counterparty_share) ? ` · top share ${pctOf(r.top_counterparty_share)}` : ""}${N(r.unidentified_txns) ? ` · ${plural(N(r.unidentified_txns), "unidentified txn")}` : ""}`;
    case "fan_in_fan_out_pattern":
      return `Peak ${num(r.window_hours ?? 24, 0)} h window: ${plural(N(r.peak_window_unique_senders), "sender")} → ${plural(N(r.peak_window_unique_receivers), "receiver")}${r.pattern ? ` (${String(r.pattern).toLowerCase().replace(/_/g, " ")})` : ""}`;
    case "cash_crypto_wallet_leakage":
      return `${inrCompact(r.leakage_debits)} of ${inrCompact(r.total_debits)} debits to cash / crypto / wallets (${num(r.leakage_pct ?? rawMetric, 1)}%)`;
    case "structuring_smurfing":
      return `${plural(N(r.near_threshold_txns ?? rawMetric), "near-threshold txn")} of ${num(r.total_txns, 0)}${N(r.split_structuring_groups) ? ` · ${plural(N(r.split_structuring_groups), "split-structuring group")}` : ""}`;
    case "zero_balance_oscillation":
      return r.balance_available === false
        ? "No running balance in the statement: not assessed"
        : `${num(r.drained_cycles ?? rawMetric, 0)} of ${plural(N(r.inflow_cycles), "large inflow")} drained to ≤ ${inrCompact(r.zero_floor)}`;
    case "burst_dormancy_pattern":
      return `Peak 30-day turnover ${inrCompact(r.peak_30d_turnover ?? rawMetric)}${fin(r.max_dormancy_gap_days) ? ` after a ${num(r.max_dormancy_gap_days, 0)}-day gap` : ""}${fin(r.median_prior_monthly_turnover) ? ` (prior median ${inrCompact(r.median_prior_monthly_turnover)}/month)` : ""}`;
    case "round_number_anomalies":
      return `${num(r.round_5000_multiples ?? rawMetric, 0)} of ${plural(N(r.electronic_txns), "electronic txn")} are multiples of ₹5,000`;
    default: {
      const pairs = Object.entries(r).filter(([, v]) => v == null || typeof v !== "object").slice(0, 4);
      if (pairs.length) return pairs.map(([k, v]) => `${k.replace(/_/g, " ")} ${typeof v === "number" ? num(v, 2) : v}`).join(" · ");
      return fin(rawMetric) ? num(rawMetric, 3) : "";
    }
  }
}

/** Raw metric value as a short figure for the table's "Raw" column. */
export function rawFigure(key, raw, rawMetric) {
  const r = raw && typeof raw === "object" ? raw : {};
  switch (key) {
    case "flow_through_ratio": return num(r.flow_through_ratio ?? rawMetric, 3);
    case "velocity_hold_time": return fin(r.median_hold_hours ?? rawMetric) ? hours(r.median_hold_hours ?? rawMetric) : "—";
    case "lifestyle_spending_absence": return `${num(r.lifestyle_txns_per_month ?? rawMetric, 1)}/mo`;
    case "counterparty_concentration": return `HHI ${num(r.hhi ?? rawMetric, 3)}`;
    case "fan_in_fan_out_pattern": return `${num(r.peak_window_unique_senders ?? rawMetric, 0)}→${num(r.peak_window_unique_receivers, 0)}`;
    case "cash_crypto_wallet_leakage": return `${num(r.leakage_pct ?? rawMetric, 1)}%`;
    case "structuring_smurfing": return `${num(r.flagged_txns ?? r.near_threshold_txns ?? rawMetric, 0)} txns`;
    case "zero_balance_oscillation": return r.balance_available === false ? "n/a" : `${num(r.drained_cycles ?? rawMetric, 0)}/${num(r.inflow_cycles, 0)}`;
    case "burst_dormancy_pattern": return inrCompact(r.peak_30d_turnover ?? rawMetric);
    case "round_number_anomalies": return `${num(r.round_5000_multiples ?? rawMetric, 0)}/${num(r.electronic_txns, 0)}`;
    default: return fin(rawMetric) ? num(rawMetric, 2) : "—";
  }
}

/**
 * The 10 parameters of one assessment, in MULE_PARAMS order:
 * [{ key, label, weight, max, score, contribution, rank, rawMetric, raw, rawText, rawFig, formula, evidenceIds, evidenceCount }]
 */
export function explainRows(a) {
  const byKey = new Map((a?.explainability || []).map((e) => [e.key, e]));
  const known = new Set(MULE_PARAMS.map((p) => p.key));
  const params = [...MULE_PARAMS, ...Array.from(byKey.keys()).filter((k) => !known.has(k)).map((k) => ({ key: k, label: byKey.get(k).label || k, weight: N(byKey.get(k).weight) }))];
  return params.map((p) => {
    const e = byKey.get(p.key) || {};
    const weight = fin(e.weight) ? Number(e.weight) : p.weight;
    const max = weight * 100;
    const score = fin(e.score) ? Number(e.score) : fin(a?.parameters?.[p.key]) ? Number(a.parameters[p.key]) : null;
    const contribution = fin(e.contribution) ? Number(e.contribution)
      : fin(a?.weighted_contributions?.[p.key]) ? Number(a.weighted_contributions[p.key])
      : score == null ? null : score * max;
    const evidenceIds = Array.from(new Set(e.evidence_tx_ids || []));
    return {
      key: p.key,
      label: p.label,
      serverLabel: e.label || p.label,
      weight,
      max,
      score,
      contribution,
      rank: fin(e.rank) ? Number(e.rank) : null,
      rawMetric: e.raw_metric ?? null,
      raw: e.raw || null,
      rawText: e.raw || e.raw_metric != null ? describeRaw(p.key, e.raw, e.raw_metric) : "",
      rawFig: e.raw || e.raw_metric != null ? rawFigure(p.key, e.raw, e.raw_metric) : "—",
      formula: e.formula || "",
      evidenceIds,
      evidenceCount: fin(e.evidence_count) ? Number(e.evidence_count) : evidenceIds.length,
    };
  });
}

/** Sum of contributions (should equal the composite within server rounding). */
export const contributionSum = (rows) => rows.reduce((s, r) => s + N(r.contribution), 0);

/** Transactions-view params for a set of evidence ids: [{ ids }] chunks of ≤ 500 (the server's tx_ids cap). */
export function evidenceLinks(ids) {
  return idChunks(ids).map((chunk) => ({ ids: chunk }));
}

/** Short tooltip for a score cell. */
export const scoreTitle = (r) => `${r.serverLabel}: S = ${r.score == null ? "—" : num(r.score, 3)} × weight ${num(r.max, 0)} = ${r.contribution == null ? "—" : num(r.contribution, 2)} points`;

