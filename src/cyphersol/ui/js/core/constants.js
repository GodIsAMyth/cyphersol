/* Shared domain constants and label/class mappings. Keep UI vocabulary in one place so every view agrees. */

/** File types accepted by the statement upload (ING-06: legacy .xls included). */
export const UPLOAD_ACCEPT = ".pdf,.xlsx,.xlsm,.xls,.csv,.tsv,.txt,.png,.jpg,.jpeg,.bmp,.pgm";

export const CASE_MODES = {
  cypher_edge: { label: "CypherEdge", short: "Edge", desc: "CA / ITR / Tally & loan appraisal" },
  cypher_trace: { label: "CypherTrace", short: "Trace", desc: "Forensic fund trail & AML investigation" },
};
export const caseModeLabel = (mode) => CASE_MODES[mode]?.label || "Case";

/** Runtime execution modes (single source of truth lives in /api/llm/config; see Settings view). */
export const EXEC_MODES = [
  { value: "heuristics", label: "Heuristics only", desc: "Local processing; no external API calls" },
  { value: "both", label: "Hybrid", desc: "Heuristics first, LLM agents as backup" },
  { value: "llm", label: "LLM primary", desc: "Stages 1–4 routed through the configured provider" },
];
export const execModeLabel = (m) => EXEC_MODES.find((x) => x.value === m)?.label || m || "Heuristics only";

export const AGENT_STAGES = [
  { stage: 1, key: "enable_stage1_vision", short: "Vision OCR", label: "Stage 1 — Vision & OCR table extractor" },
  { stage: 2, key: "enable_stage2_reconciliation", short: "Reconciler", label: "Stage 2 — Metadata & balance healer" },
  { stage: 3, key: "enable_stage3_narration", short: "Rail & party", label: "Stage 3 — Payment rail & counterparty parser" },
  { stage: 4, key: "enable_stage4_categorization", short: "Category & entity", label: "Stage 4 — 52-category classifier & entity resolver" },
];

export const PAYMENT_CHANNELS = ["UPI", "NEFT", "RTGS", "IMPS", "ATM", "AEPS", "POS", "NACH", "CHEQUE", "OTHER"];

/** Mule risk tiers (80/50 thresholds). tierClass("Critical") -> "critical" (badge--critical, --risk-critical). */
export const RISK_TIERS = {
  Critical: { cls: "critical", label: "Critical", range: "80–100" },
  Suspicious: { cls: "suspicious", label: "Suspicious", range: "50–79" },
  "Low Risk": { cls: "low", label: "Low risk", range: "0–49" },
};
export const tierClass = (tier) => RISK_TIERS[tier]?.cls || "low";
export const tierFromScore = (score) => (score >= 80 ? "Critical" : score >= 50 ? "Suspicious" : "Low Risk");

/** 10-parameter mule model: key, label, weight (fraction). Order = weight descending. */
export const MULE_PARAMS = [
  { key: "flow_through_ratio", label: "Flow-through ratio", weight: 0.25 },
  { key: "velocity_hold_time", label: "Velocity / hold time", weight: 0.2 },
  { key: "lifestyle_spending_absence", label: "Lifestyle absence", weight: 0.15 },
  { key: "counterparty_concentration", label: "Counterparty concentration (HHI)", weight: 0.1 },
  { key: "fan_in_fan_out_pattern", label: "Fan-in / fan-out", weight: 0.1 },
  { key: "cash_crypto_wallet_leakage", label: "Cash / crypto leakage", weight: 0.05 },
  { key: "structuring_smurfing", label: "Structuring / smurfing", weight: 0.05 },
  { key: "zero_balance_oscillation", label: "Zero-balance oscillation", weight: 0.05 },
  { key: "burst_dormancy_pattern", label: "Burst / dormancy", weight: 0.03 },
  { key: "round_number_anomalies", label: "Round numbers", weight: 0.02 },
];

/** Graph topology roles (never coloured like risk tiers). cls feeds .fill-<cls>, .sw-<cls>. */
export const GRAPH_ROLES = {
  PRIMARY_HUB: { label: "Primary hub", cls: "hub" },
  BRIDGE_CONDUIT: { label: "Bridge / conduit", cls: "conduit" },
  EXIT_ENDPOINT: { label: "Cash / VDA exit", cls: "exit" },
  PERIPHERAL_LEAF: { label: "Peripheral", cls: "leaf" },
};

/** Layering roles (SRS vocabulary). cls feeds .badge--<cls>, .fill-<cls>, .sw-<cls>. */
export const LAYER_ROLES = {
  CREDIT_SOURCE: { label: "Credit source", cls: "source" },
  HAS_STATEMENT: { label: "Has statement", cls: "statement" },
  DETAILS_ONLY: { label: "Details only", cls: "details" },
  MISC: { label: "Misc (collapsed)", cls: "misc" },
};

/** Entity status helper: has_statement / details-only / system. */
export const entityStatus = (e) =>
  e?.is_system || String(e?.canonical_name || "").startsWith("UNRESOLVED_")
    ? { label: "System", cls: "system" }
    : e?.has_statement
      ? { label: "Has statement", cls: "statement" }
      : { label: "Details only", cls: "details" };

/** Anomaly flags: label, severity tone (badge--<tone>) and the actions that make sense for each (UI-S-15). */
export const ANOMALY_FLAGS = {
  BALANCE_MISMATCH: { label: "Balance mismatch", tone: "danger", actions: [] },
  DUPLICATE_SUSPECT: { label: "Duplicate suspect", tone: "warn", actions: ["toggle-duplicate"] },
  DATE_OUT_OF_ORDER: { label: "Date out of order", tone: "warn", actions: [] },
  DATE_UNPARSEABLE: { label: "Unparseable date", tone: "danger", actions: [] },
  DATE_ORDER_AMBIGUOUS: { label: "Ambiguous date order", tone: "warn", actions: [] },
  LOW_OCR_CONFIDENCE: { label: "Low OCR confidence", tone: "warn", actions: [] },
  MISSING_METADATA: { label: "Missing metadata", tone: "neutral", actions: [] },
  ZERO_OR_DUAL_AMOUNT: { label: "Zero / dual amount", tone: "warn", actions: [] },
  DIRECTION_INFERRED: { label: "Direction inferred", tone: "info", actions: [] },
  OCR_AUTOCORRECTED: { label: "OCR auto-corrected", tone: "info", actions: [] },
  LLM_REPAIRED: { label: "LLM repaired", tone: "info", actions: [] },
  VALID: { label: "Valid", tone: "success", actions: [] },
};
export const flagMeta = (f) => ANOMALY_FLAGS[f] || { label: String(f), tone: "neutral", actions: [] };

/** Export formats for the top-bar Export menu, filtered by case mode (UI-S-27). */
export const EXPORTS = [
  { id: "xlsx", label: "Excel workbook", desc: "Audit workbook (.xlsx)", sub: "export/xlsx", ext: "xlsx", modes: ["cypher_edge", "cypher_trace"] },
  { id: "tally-xml", label: "Tally XML", desc: "Vouchers + ledger masters", sub: "export/tally-xml", ext: "xml", modes: ["cypher_edge"] },
  { id: "dossier-pdf", label: "Forensic dossier (PDF)", desc: "Sec 63 BSA certified report", sub: "export/dossier-pdf", ext: "pdf", modes: ["cypher_edge", "cypher_trace"] },
  { id: "dossier-html", label: "Forensic dossier (HTML)", desc: "Sec 63 report, browser-viewable", sub: "export/dossier-html", ext: "html", modes: ["cypher_edge", "cypher_trace"] },
];

export const STORAGE_KEYS = {
  theme: "cyphersol.theme",
  sidebar: "cyphersol.sidebar",
  lastCase: "cyphersol.lastCase",
  navMore: "cyphersol.navMore",
  tally: "cyphersol.tally",
};

/** localStorage wrappers that never throw (private mode, blocked storage). */
export function lsGet(key, fallback = null) {
  try {
    const v = window.localStorage.getItem(key);
    return v === null ? fallback : v;
  } catch (_e) {
    return fallback;
  }
}
export function lsSet(key, value) {
  try {
    if (value === null || value === undefined) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, String(value));
  } catch (_e) {
    /* storage unavailable: preference simply is not remembered */
  }
}

const TALLY_DEFAULT = { host: "127.0.0.1", port: 9000, company_name: "" };

/** Tally connection saved on this machine in Settings (falls back to 127.0.0.1:9000). */
export function getTallyConnection() {
  try {
    const v = JSON.parse(lsGet(STORAGE_KEYS.tally, "null"));
    if (v && typeof v === "object") return { ...TALLY_DEFAULT, ...v };
  } catch (_e) {
    /* corrupt value: use defaults */
  }
  return { ...TALLY_DEFAULT };
}

/** Merge `patch` into the saved Tally connection; `null` resets it to the defaults. */
export function setTallyConnection(patch) {
  if (patch === null) return lsSet(STORAGE_KEYS.tally, null);
  lsSet(STORAGE_KEYS.tally, JSON.stringify({ ...getTallyConnection(), ...patch }));
}
