/* Pure (DOM-free) transaction helpers shared by the transactions, suspense, anomalies, mules and profile views.
 * Kept free of DOM access so the P1 tests can run it under node (tests/test_p1_ui_data.py).
 *
 *   SRS FR-CAT-01 payment channels (NLP-24): srsChannel(code), channelOf(tx), channelLabel(code),
 *     channelFilterOptions(meta) — the server sends payment_channel_srs on every transaction and the SRS lists
 *     in /api/meta (srs_payment_channels, srs_channel_extensions); srsChannel mirrors
 *     nlp/narration_parser.srs_payment_channel for payloads that only carry the internal code (profile channel_mix).
 *   Data-quality flags: FLAG_HELP, FLAG_ORDER, flagInfo(flag) — ANOMALY_FLAGS plus flags added in P1 that the
 *     shared constants do not list yet (OUTSIDE_STATEMENT_PERIOD).
 *   URL filters: parseIds, filtersFromParams (accepts both `ids` and the API's own `tx_ids`), idChunks.
 */
import { ANOMALY_FLAGS } from "../core/constants.js";

// ------------------------------------------------------------------------------------------- payment channels
/** SRS FR-CAT-01 enum, in SRS order (fallback when /api/meta is older). */
export const SRS_CHANNELS = ["UPI", "NEFT", "RTGS", "IMPS", "NACH", "ATM", "AEPS", "POS", "CHEQUE", "INTERNAL_TRANSFER", "BANK_CHARGE", "INTEREST", "CASH_CDM"];
/** Rails the engine distinguishes that the SRS has no member for (documented extensions). */
export const SRS_EXTENSIONS = ["CASH_WDL", "FOREX", "OTHER"];

/** Internal channel code -> SRS name (identity for everything not listed). Mirrors narration_parser. */
const INTERNAL_TO_SRS = {
  TRANSFER: "INTERNAL_TRANSFER",
  CHARGES: "BANK_CHARGE",
  ACH: "NACH",
  ECS: "NACH",
  ECOM: "POS",
  CLG: "CHEQUE",
  CASH_DEPOSIT: "CASH_CDM",
};

const LABELS = {
  UPI: "UPI",
  NEFT: "NEFT",
  RTGS: "RTGS",
  IMPS: "IMPS",
  NACH: "NACH / ECS",
  ATM: "ATM",
  AEPS: "AEPS",
  POS: "POS / card",
  CHEQUE: "Cheque",
  INTERNAL_TRANSFER: "Internal transfer",
  BANK_CHARGE: "Bank charge",
  INTEREST: "Interest",
  CASH_CDM: "Cash deposit (CDM)",
  CASH_WDL: "Cash withdrawal",
  FOREX: "Forex",
  OTHER: "Other",
};

/** Map an internal code (or SRS name, or alias) to the SRS enum. None/empty -> "OTHER". Idempotent. */
export function srsChannel(code) {
  const ch = String(code ?? "").trim().toUpperCase();
  if (!ch) return "OTHER";
  return INTERNAL_TO_SRS[ch] || ch;
}

/** SRS channel of a transaction row (server value first). */
export const channelOf = (t) => (t && t.payment_channel_srs) || srsChannel(t?.payment_channel);

/** Human label for an SRS channel code ("INTERNAL_TRANSFER" -> "Internal transfer"). */
export function channelLabel(code) {
  const c = srsChannel(code);
  return LABELS[c] || c.replace(/_/g, " ").toLowerCase().replace(/^\w/, (x) => x.toUpperCase());
}

/** True when the SRS name differs from the code the bank statement used (shown as a hint). */
export const channelRenamed = (t) => Boolean(t?.payment_channel) && channelOf(t) !== String(t.payment_channel).toUpperCase();

/** Grouped select options for the channel filter: SRS enum first, then the documented extensions. */
export function channelFilterOptions(meta) {
  const srs = Array.isArray(meta?.srs_payment_channels) && meta.srs_payment_channels.length ? meta.srs_payment_channels : SRS_CHANNELS;
  const ext = Array.isArray(meta?.srs_channel_extensions) ? meta.srs_channel_extensions : SRS_EXTENSIONS;
  const opt = (c) => ({ value: c, label: c === channelLabel(c) ? c : `${channelLabel(c)} (${c})` });
  return [
    { group: "SRS FR-CAT-01 channels", options: srs.map(opt) },
    ...(ext.length ? [{ group: "Extensions", options: ext.filter((c) => !srs.includes(c)).map(opt) }] : []),
  ];
}

// ------------------------------------------------------------------------------------------- anomaly flags
/** Flags added in P1 that core/constants.js ANOMALY_FLAGS may not list yet. */
const EXTRA_FLAGS = {
  OUTSIDE_STATEMENT_PERIOD: { label: "Outside statement period", tone: "warn", actions: [] },
};

/** label / tone / actions for a flag (ANOMALY_FLAGS first, then the P1 extras, then a neutral fallback). */
export function flagInfo(f) {
  return ANOMALY_FLAGS[f] || EXTRA_FLAGS[f] || { label: String(f || "").replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase()), tone: "neutral", actions: [] };
}

/** Plain-language meaning of each data-quality flag (drawer + anomalies view). */
export const FLAG_HELP = {
  BALANCE_MISMATCH: "Previous balance + credit − debit does not equal the running balance printed on this row. Rows may be missing, mis-read or the statement may have been altered.",
  DUPLICATE_SUSPECT: "Same account, date, amount, direction and narration as another row — usually overlapping statement periods. Ignore it to exclude it from totals and analytics.",
  DATE_OUT_OF_ORDER: "The date is earlier than the row before it in the statement.",
  DATE_UNPARSEABLE: "The date could not be parsed; the row may be placed in the wrong period.",
  DATE_ORDER_AMBIGUOUS: "Day and month could be swapped (e.g. 03/04); the parser chose one order for the whole statement.",
  LOW_OCR_CONFIDENCE: "Extracted from a scanned page whose OCR confidence is below the gate — verify against the source document.",
  MISSING_METADATA: "Statement metadata (account number, IFSC, holder or period) could not be extracted.",
  ZERO_OR_DUAL_AMOUNT: "The row has a zero amount, or both a debit and a credit.",
  DIRECTION_INFERRED: "Debit/credit direction was inferred from the balance movement, not read from a column.",
  OCR_AUTOCORRECTED: "OCR digits were auto-corrected to restore the running-balance chain; the before and after values are shown below and kept in the audit log.",
  LLM_REPAIRED: "A Stage 2 agent repaired this row; the change is logged in the audit trail.",
  OUTSIDE_STATEMENT_PERIOD: "The transaction date falls outside the period printed in the statement header.",
  VALID: "Passed running-balance, date and duplicate checks.",
};

/** Display order of the flag cards (anything else follows). */
export const FLAG_ORDER = [
  "BALANCE_MISMATCH", "DUPLICATE_SUSPECT", "DATE_OUT_OF_ORDER", "DATE_UNPARSEABLE", "DATE_ORDER_AMBIGUOUS", "OUTSIDE_STATEMENT_PERIOD",
  "ZERO_OR_DUAL_AMOUNT", "LOW_OCR_CONFIDENCE", "MISSING_METADATA", "DIRECTION_INFERRED", "OCR_AUTOCORRECTED", "LLM_REPAIRED",
];

/** Every known non-VALID flag key. */
export const ALL_FLAG_KEYS = Array.from(new Set([...FLAG_ORDER, ...Object.keys(ANOMALY_FLAGS)])).filter((f) => f !== "VALID");

// ------------------------------------------------------------------------------------------- URL filters
export const MAX_IDS = 500; // server cap for tx_ids
export const FILTER_KEYS = ["q", "category_id", "channel", "direction", "statement_id", "entity", "ids", "date_from", "date_to", "amount_min", "amount_max", "suspense", "flagged"];

/** Evidence-row ids from a comma list (deduplicated, blanks dropped, capped at the server limit). */
export function parseIds(v, cap = MAX_IDS) {
  return [...new Set(String(v || "").split(",").map((x) => x.trim()).filter(Boolean))].slice(0, cap);
}

/** Split any number of ids into comma strings the server accepts (≤ MAX_IDS each). */
export function idChunks(ids, size = MAX_IDS) {
  const list = [...new Set((ids || []).filter(Boolean))];
  const out = [];
  for (let i = 0; i < list.length; i += size) out.push(list.slice(i, i + size).join(","));
  return out;
}

/** URL params -> grid filters. `tx_ids` (the API name) is accepted as an alias of `ids`; channel is mapped to its SRS name. */
export function filtersFromParams(p) {
  const f = {};
  for (const k of FILTER_KEYS) {
    let v = p?.[k];
    if (k === "ids" && (v == null || v === "")) v = p?.tx_ids;
    if (v == null || v === "") continue;
    if ((k === "suspense" || k === "flagged") && v !== "true") continue;
    if (k === "direction" && !["CR", "DR"].includes(String(v).toUpperCase())) continue;
    if (k === "ids") {
      const ids = parseIds(v);
      if (ids.length) f.ids = ids.join(",");
      continue;
    }
    f[k] = k === "direction" ? String(v).toUpperCase() : k === "channel" ? srsChannel(v) : String(v);
  }
  return f;
}

/** Grid filters -> API params (entity -> entity_id, ids -> tx_ids + include_ignored). */
export function toServer(filters) {
  const out = { ...filters };
  if (out.entity) {
    out.entity_id = out.entity;
    delete out.entity;
  }
  if (out.ids) {
    out.tx_ids = out.ids;
    delete out.ids;
    out.include_ignored = "true"; // an exact evidence set shows every listed row, ignored duplicates included
  }
  return out;
}
