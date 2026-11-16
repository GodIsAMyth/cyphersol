/* Pure (DOM-free) helpers for the financial profile (FR-TRACE-06, report TG-16 / TG-17).
 *
 *   profileFormats(meta)            server export formats (GET export/profile?entity_id=&format=) for the menu
 *   profileExportName(entity, fmt)  fallback filename when Content-Disposition is missing
 *   channelRows(channel_mix)        channel mix keyed by the SRS FR-CAT-01 name (aliases folded together)
 *   categoryRows(breakdown)         categories sorted by gross amount, with share of gross
 *   groupRows(breakdown)            the same rolled up to taxonomy groups
 * Tested under node by tests/test_p1_ui_data.py.
 */
import { channelLabel, srsChannel } from "./transactions-meta.js";

const N = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);

const FORMAT_INFO = {
  pdf: { label: "PDF report", desc: "Printable profile with charts and source statements", icon: "file" },
  xlsx: { label: "Excel workbook", desc: "Profile sheets: KPIs, monthly, categories, channels, parties", icon: "table" },
  html: { label: "HTML report", desc: "Self-contained page (opens offline in any browser)", icon: "download" },
};
const DEFAULT_FORMATS = ["pdf", "xlsx", "html"];

/** [{ fmt, label, desc, icon, ext }] in a stable order (PDF, Excel, HTML), limited to what the server offers. */
export function profileFormats(meta) {
  const offered = Array.isArray(meta?.profile_export_formats) && meta.profile_export_formats.length
    ? meta.profile_export_formats.map((f) => String(f).toLowerCase())
    : DEFAULT_FORMATS;
  const order = [...DEFAULT_FORMATS, ...offered.filter((f) => !DEFAULT_FORMATS.includes(f))];
  return order
    .filter((f) => offered.includes(f))
    .map((fmt) => ({ fmt, ext: fmt, ...(FORMAT_INFO[fmt] || { label: fmt.toUpperCase(), desc: "", icon: "download" }) }));
}

export function profileExportName(entity, fmt) {
  const base = String(entity?.canonical_name || "entity").replace(/[^A-Za-z0-9]+/g, "_").replace(/^_|_$/g, "") || "entity";
  return `profile_${base}.${fmt}`;
}

/**
 * channel_mix rows [{channel, amount, credit, debit, tx_count, share_pct}] -> SRS-keyed rows
 * [{ code, label, credit, debit, amount, tx_count, share }] (share of the gross total, 0–1), largest first.
 */
export function channelRows(mix) {
  const by = new Map();
  for (const c of mix || []) {
    const code = c.channel_srs || srsChannel(c.channel);
    const r = by.get(code) || { code, label: channelLabel(code), credit: 0, debit: 0, amount: 0, tx_count: 0, sources: [] };
    const credit = N(c.credit);
    const debit = N(c.debit);
    r.credit += credit;
    r.debit += debit;
    r.amount += c.amount != null ? N(c.amount) : credit + debit;
    r.tx_count += N(c.tx_count);
    if (c.channel && !r.sources.includes(String(c.channel).toUpperCase())) r.sources.push(String(c.channel).toUpperCase());
    by.set(code, r);
  }
  const rows = Array.from(by.values());
  const total = rows.reduce((s, r) => s + r.amount, 0) || 1;
  rows.forEach((r) => (r.share = r.amount / total));
  return rows.sort((a, b) => b.amount - a.amount || a.code.localeCompare(b.code));
}

/** Category rows sorted by gross (credit + debit), with share of gross 0–1. */
export function categoryRows(breakdown) {
  const rows = (breakdown || []).map((c) => ({
    id: c.category_id,
    name: c.category || c.category_name || "Uncategorised",
    group: c.group || "Other",
    credit: N(c.credit),
    debit: N(c.debit),
    gross: N(c.credit) + N(c.debit),
    tx_count: N(c.tx_count),
    suspense: Number(c.category_id) === 52,
  }));
  const total = rows.reduce((s, r) => s + r.gross, 0) || 1;
  rows.forEach((r) => (r.share = r.gross / total));
  return rows.sort((a, b) => b.gross - a.gross || String(a.name).localeCompare(String(b.name)));
}

/** Categories rolled up to their taxonomy group, sorted by gross. */
export function groupRows(breakdown) {
  const by = new Map();
  for (const c of categoryRows(breakdown)) {
    const g = by.get(c.group) || { group: c.group, credit: 0, debit: 0, gross: 0, tx_count: 0, categories: 0, share: 0 };
    g.credit += c.credit;
    g.debit += c.debit;
    g.gross += c.gross;
    g.tx_count += c.tx_count;
    g.share += c.share;
    g.categories += 1;
    by.set(c.group, g);
  }
  return Array.from(by.values()).sort((a, b) => b.gross - a.gross || a.group.localeCompare(b.group));
}
