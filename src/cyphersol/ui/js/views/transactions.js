/* Transactions (#/case/{id}/transactions): every row of the case in a server-paginated, virtualised grid.
 *
 * - GET cases/{id}/transactions with offset/limit/sort/order and the filters q, category_id, channel, direction,
 *   statement_id, entity (-> entity_id), date_from/date_to, amount_min/amount_max, suspense, flagged.
 *   The grid shows "X–Y of Z" (fixes the silent 300-row cap, report §3.9 / UI-S-02).
 * - Filter + sort state is mirrored to the URL (ctx.setParams) and restored by update(params) on back/forward.
 * - Row click / Enter opens the evidence drawer: raw narration, parsed fields, top-3 suggestions, data-quality
 *   flags and statement provenance (SHA-256 custody hash, parser version, ingest time).
 * - "Export view" writes the currently filtered + sorted rows to CSV (client-side Blob, formula-injection safe).
 *
 * This module also exports the shared transaction renderers used by the suspense and anomalies views
 * (openTxDrawer, flagChips, statusCell, amountCell, accountCell, categoryOptions, loadAllTransactions, FLAG_HELP),
 * so the three views stay visually and behaviourally consistent. */
import {
  ALL_FLAG_KEYS, FLAG_HELP as FLAG_HELP_P1, channelFilterOptions, channelLabel, channelOf, channelRenamed,
  filtersFromParams, flagInfo, parseIds, toServer, FILTER_KEYS,
} from "./transactions-meta.js";
import { delegate, html, render, setVars } from "../core/dom.js";
import { DASH, catNo, date, dateTime, inr, int, pct, plural } from "../core/format.js";
import { TOPICS } from "../core/store.js";
import { createDataGrid } from "../components/datagrid.js";
import { openDrawer } from "../components/drawer.js";
import { icon } from "../components/icons.js";
import { pageHead, spinner } from "../components/states.js";
import { toast, withBusy } from "../components/toast.js";
import { downloadCsv } from "../core/files.js";

export const styles = ["views/transactions.css"];

// ------------------------------------------------------------------------------------------------ shared helpers
/** Plain-language meaning of each data-quality flag (drawer + anomalies view); defined in transactions-meta.js. */
export const FLAG_HELP = FLAG_HELP_P1;

const SOURCE_LABEL = {
  USER_RULE: "Analyst rule",
  SUSPENSE: "Suspense",
  SUSPENSE_P2P: "Suspense (P2P)",
  KEYWORD: "Keyword",
  KEYWORD_RULE: "Keyword rule",
  LLM: "LLM agent",
  ML: "ML model",
};
export const sourceLabel = (s) => SOURCE_LABEL[s] || String(s || DASH).replace(/_/g, " ").toLowerCase().replace(/^\w/, (c) => c.toUpperCase());

const realFlags = (t) => (t?.anomaly_flags || []).filter((f) => f && f !== "VALID");
export const isFlagged = (t) => realFlags(t).length > 0;

/** Status for the row stripe: flagged (red), ignored duplicate (grey) or valid (green). */
export function rowStatus(t) {
  if (t?.is_duplicate_ignored) return { cls: "ignored", label: "Ignored duplicate", icon: "minus" };
  if (isFlagged(t)) return { cls: "flag", label: `Flagged: ${realFlags(t).map((f) => flagInfo(f).label).join(", ")}`, icon: "alert" };
  return { cls: "ok", label: "Valid — passed balance, date and duplicate checks", icon: "check" };
}

/** Row class for createDataGrid's rowClassName: stripes flagged rows and mutes ignored duplicates. */
export const txRowClass = (t) => {
  const c = rowStatus(t).cls;
  return c === "ok" ? "" : `v-transactions-row--${c}`;
};

export const statusCell = (t) => {
  const s = rowStatus(t);
  return html`<span class="v-transactions-st v-transactions-st--${s.cls}" title="${s.label}">${icon(s.icon, { size: "sm", label: s.label })}</span>`;
};

/** Anomaly flag chips (VALID hidden). max limits the visible chips; the rest collapse into "+n". */
export function flagChips(t, { max = 2, empty = "" } = {}) {
  const flags = realFlags(t);
  if (!flags.length) return empty;
  const shown = flags.slice(0, max);
  const rest = flags.length - shown.length;
  return html`<span class="v-transactions-flags">${shown.map(
    (f) => html`<span class="badge badge--${flagInfo(f).tone}" title="${FLAG_HELP[f] || f}">${flagInfo(f).label}</span>`
  )}${rest > 0 ? html`<span class="badge badge--outline" title="${flags.slice(max).map((f) => flagInfo(f).label).join(", ")}">+${rest}</span>` : ""}</span>`;
}

/** Signed amount with Dr/Cr colour and sign (colour is never the only cue). */
export const amountCell = (t) =>
  t.credit > 0
    ? html`<span class="is-credit">+${inr(t.credit)}</span>`
    : html`<span class="is-debit">${inr(-(t.debit || 0))}</span>`;

export const accountCell = (t) =>
  html`<span class="truncate">${t.source_holder_name || DASH}</span><span class="dg__sub mono">${t.source_account_number || ""}</span>`;

export const narrationCell = (t) =>
  html`<span class="v-transactions-narr" title="${t.raw_narration}">${t.raw_narration}</span><span class="dg__sub">${t.extracted_counterparty || "Party not extracted"}${t.utr_number ? html` · UTR <span class="mono">${t.utr_number}</span>` : t.cheque_or_ref_no ? html` · Ref <span class="mono">${t.cheque_or_ref_no}</span>` : ""}</span>`;


/** SRS FR-CAT-01 channel badge; the bank's own code is kept in the tooltip when it differs (NLP-24). */
export const channelCell = (t) => {
  const code = channelOf(t);
  const tip = channelRenamed(t) ? `${channelLabel(code)} (SRS ${code}; statement code ${t.payment_channel})` : `${channelLabel(code)} (SRS ${code})`;
  return html`<span class="badge badge--info v-transactions-ch" title="${tip}">${channelLabel(code)}</span>`;
};

/** <option>/<optgroup> list for the 52-category taxonomy (grouped as in /api/meta). */
export function categoryOptions(categories, { excludeSuspense = true, selected = null, placeholder = "Choose a category…" } = {}) {
  const groups = new Map();
  for (const c of categories || []) {
    if (excludeSuspense && Number(c.category_id) === 52) continue;
    const g = c.group || "Other";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(c);
  }
  return html`${placeholder ? html`<option value="">${placeholder}</option>` : ""}${Array.from(groups, ([g, cats]) =>
    html`<optgroup label="${g}">${cats.map(
      (c) => html`<option value="${c.category_id}" ${Number(selected) === Number(c.category_id) ? html`selected` : ""}>${catNo(c.category_id)} — ${c.name}</option>`
    )}</optgroup>`
  )}`;
}

/** The 52 categories as grouped datagrid select-filter options (suspense included, so it can be filtered on). */
export function categoryFilterGroups(categories) {
  const groups = new Map();
  for (const c of categories || []) {
    const g = c.group || "Other";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push({ value: String(c.category_id), label: `${catNo(c.category_id)} — ${c.name}` });
  }
  return Array.from(groups, ([group, options]) => ({ group, options }));
}

/**
 * Page through GET transactions until every matching row is loaded (or `cap` rows).
 * Returns { rows, total, truncated }. onProgress(loaded, total) after every page.
 */
export async function loadAllTransactions(ctx, params, { signal = ctx.signal, cap = 20000, pageSize = 2000, onProgress = null } = {}) {
  const rows = [];
  let total = 0;
  let offset = 0;
  for (;;) {
    const res = await ctx.api.get("transactions", { ...params, offset, limit: pageSize }, { signal });
    const batch = res.transactions || [];
    total = Number(res.total) || 0;
    rows.push(...batch);
    offset += batch.length;
    onProgress?.(rows.length, total);
    if (!batch.length || offset >= total || rows.length >= cap) break;
  }
  return { rows, total, truncated: rows.length < total };
}

// provenance (statement custody records) is case-wide; cache per case + revision
let provCache = { key: "", promise: null };
function loadProvenance(ctx) {
  const rev = ctx.store.get().caseData?.revision ?? "";
  const key = `${ctx.caseId}|${rev}`;
  if (provCache.key !== key || !provCache.promise) {
    // not bound to the view signal: the drawer may outlive a navigation and the result is shared
    provCache = { key, promise: ctx.api.get("provenance", {}, { signal: undefined }).catch((err) => { provCache = { key: "", promise: null }; throw err; }) };
  }
  return provCache.promise;
}

/**
 * Evidence drawer for one transaction.
 * opts.actions: [{ id, label, variant, icon, run(tx, drawer) }] rendered in the footer (e.g. ignore duplicate).
 * opts.onAssign(categoryId, tx, drawer): when given and the row is in suspense, suggestions become buttons.
 * Returns the drawer handle plus update(tx) to repaint after a mutation.
 */
export function openTxDrawer(tx, ctx, { actions = [], onAssign = null } = {}) {
  let t = tx;
  const d = openDrawer({ title: "Transaction", width: 600, className: "v-transactions-drawer", body: html`<div class="v-transactions-dw">${spinner()}</div>` });

  const stmt = () => ctx.store.statements().find((s) => s.statement_id === t.statement_id) || null;
  const entity = () => (t.resolved_entity_id ? ctx.store.entity(t.resolved_entity_id) : null);

  const footer = () => {
    const links = [];
    const party = t.extracted_counterparty || t.counterparty_vpa_or_acc;
    if (t.is_suspense && !onAssign) links.push(html`<a class="btn btn--sm" href="${ctx.href("suspense", { q: party || "" })}">${icon("inbox", { size: "sm" })}Resolve in Suspense</a>`);
    if (isFlagged(t) && ctx.view !== "anomalies") links.push(html`<a class="btn btn--sm" href="${ctx.href("anomalies", { tab: realFlags(t)[0] })}">${icon("alert", { size: "sm" })}Open in Anomalies</a>`);
    if (t.resolved_entity_id && entity() && !entity().is_system) {
      links.push(html`<a class="btn btn--sm" href="${ctx.href("transactions", { entity: t.resolved_entity_id })}">${icon("list", { size: "sm" })}All with this party</a>`);
      links.push(html`<a class="btn btn--sm" href="${ctx.href("profile", { entity: t.resolved_entity_id })}">${icon("user", { size: "sm" })}Entity profile</a>`);
    }
    const acts = actions.filter((a) => !a.when || a.when(t));
    return html`<div class="row-wrap v-transactions-dw-foot">${links}<span class="spacer"></span>${acts.map(
      (a) => html`<button type="button" class="btn btn--sm ${a.variant ? `btn--${a.variant}` : ""}" data-txd-action="${a.id}">${a.icon ? icon(a.icon, { size: "sm" }) : ""}${typeof a.label === "function" ? a.label(t) : a.label}</button>`
    )}</div>`;
  };

  /** ING-10: the OCR self-healing change on this row (before → after), from the OCR_AUTOCORRECT audit entry. */
  const ocrFix = (prov) => {
    if (!(t.anomaly_flags || []).includes("OCR_AUTOCORRECTED")) return "";
    if (!prov) return html`<p class="xsmall muted">Loading the OCR correction…</p>`;
    const c = (prov.ocr_corrections || {})[t.tx_id];
    if (!c) return html`<p class="xsmall muted">The OCR correction for this row is not in the audit log.</p>`;
    const fields = [["debit", "Debit"], ["credit", "Credit"], ["running_balance", "Running balance"]];
    const b = c.before || {};
    const a = c.after || {};
    return html`<div class="stack-sm">
      <div class="small text-2">OCR auto-correction (verify against the source page before relying on it)</div>
      <div class="table-wrap"><table class="table table--compact"><caption class="sr-only">OCR values before and after self-healing</caption>
        <thead><tr><th scope="col">Field</th><th scope="col" class="num">As read by OCR</th><th scope="col" class="num">Corrected to</th></tr></thead>
        <tbody>${fields.map(([k, label]) => html`<tr class="${String(b[k]) !== String(a[k]) ? "is-changed" : ""}"><th scope="row">${label}</th>
          <td class="num tabular">${b[k] == null ? DASH : inr(Number(b[k]))}</td>
          <td class="num tabular ${String(b[k]) !== String(a[k]) ? "strong" : ""}">${a[k] == null ? DASH : inr(Number(a[k]))}</td></tr>`)}</tbody></table></div>
    </div>`;
  };

  const paint = (prov) => {
    const s = stmt();
    const ent = entity();
    const p = prov && s ? (prov.provenance || []).find((x) => x.statement_id === s.statement_id) : null;
    const flags = realFlags(t);
    const st = rowStatus(t);
    const sugg = (t.top3_suggestions || []).slice(0, 3);
    d.setTitle(html`${t.credit > 0 ? "Credit" : "Debit"} · ${date(t.txn_date)}`);
    d.setBody(html`<div class="v-transactions-dw stack">
      <div class="v-transactions-dw-hero">
        <div>
          <div class="v-transactions-dw-amt ${t.credit > 0 ? "is-credit" : "is-debit"}">${t.credit > 0 ? html`+${inr(t.credit)}` : inr(-(t.debit || 0))}</div>
          <div class="small muted">${t.credit > 0 ? "Cr" : "Dr"} · ${channelLabel(channelOf(t))} · balance after ${inr(t.running_balance)}</div>
        </div>
        <div class="v-transactions-dw-badges">
          <span class="badge badge--dot ${st.cls === "flag" ? "badge--danger" : st.cls === "ignored" ? "badge--neutral" : "badge--success"}">${st.cls === "flag" ? "Flagged" : st.cls === "ignored" ? "Ignored duplicate" : "Valid"}</span>
          ${t.is_suspense ? html`<span class="badge badge--suspicious">Suspense</span>` : ""}
        </div>
      </div>

      <section class="stack-sm" aria-labelledby="txd-narr">
        <h3 class="caps" id="txd-narr">Raw narration</h3>
        <pre class="code v-transactions-dw-narr">${t.raw_narration}</pre>
      </section>

      <section class="stack-sm" aria-labelledby="txd-parsed">
        <h3 class="caps" id="txd-parsed">Parsed fields</h3>
        <dl class="kv">
          <dt>Counterparty</dt><dd>${t.extracted_counterparty || DASH}${ent && !ent.is_system && String(ent.canonical_name || "").toUpperCase() !== String(t.extracted_counterparty || "").toUpperCase() ? html` <span class="muted small">→ ${ent.canonical_name}</span>` : ""}</dd>
          <dt>VPA / account</dt><dd class="mono">${t.counterparty_vpa_or_acc || DASH}</dd>
          <dt>IFSC</dt><dd class="mono">${t.counterparty_ifsc || DASH}</dd>
          <dt>UTR</dt><dd class="mono">${t.utr_number || DASH}</dd>
          <dt>Channel (SRS)</dt><dd><span class="mono">${channelOf(t)}</span>${channelRenamed(t) ? html` <span class="muted small">statement code ${t.payment_channel}</span>` : ""}</dd>
          <dt>Cheque number</dt><dd class="mono">${t.cheque_number || DASH}</dd>
          <dt>Card</dt><dd class="mono">${t.card_number_masked || DASH}</dd>
          <dt>Ref no.</dt><dd class="mono">${t.cheque_or_ref_no || DASH}</dd>
          <dt>Transaction date</dt><dd>${date(t.txn_date)}${t.value_date && t.value_date !== t.txn_date ? html` <span class="muted small">(value ${date(t.value_date)})</span>` : ""}</dd>
          <dt>Timestamp</dt><dd>${t.txn_timestamp ? dateTime(t.txn_timestamp) : DASH}</dd>
          <dt>Debit / credit</dt><dd class="tabular">${t.debit > 0 ? html`<span class="is-debit">${inr(-t.debit)}</span>` : DASH} / ${t.credit > 0 ? html`<span class="is-credit">+${inr(t.credit)}</span>` : DASH}</dd>
          <dt>Running balance</dt><dd class="tabular">${inr(t.running_balance)}</dd>
        </dl>
      </section>

      <section class="stack-sm" aria-labelledby="txd-cat">
        <h3 class="caps" id="txd-cat">Classification</h3>
        <dl class="kv">
          <dt>Category</dt><dd><span class="badge ${t.is_suspense ? "badge--suspicious" : "badge--neutral"}">${catNo(t.category_id)} — ${t.category_name}</span></dd>
          <dt>Confidence</dt><dd class="tabular">${pct(t.confidence_score, { decimals: 0 })}</dd>
          <dt>Source</dt><dd>${sourceLabel(t.categorization_source)}</dd>
          <dt>Tally</dt><dd>${t.tally_voucher_type || DASH} → ${t.tally_ledger_name || DASH}</dd>
        </dl>
        ${sugg.length
          ? html`<div class="stack-sm">
              <div class="small text-2">Top suggestions${onAssign && t.is_suspense ? " — click to assign" : ""}</div>
              <ul class="v-transactions-sugg" role="list">${sugg.map(
                (sg, i) => html`<li class="v-transactions-sugg__item">
                  ${onAssign && t.is_suspense
                    ? html`<button type="button" class="btn btn--sm" data-txd-assign="${sg.category_id}">${icon("check", { size: "sm" })}${catNo(sg.category_id)} — ${sg.category_name}</button>`
                    : html`<span class="small">${catNo(sg.category_id)} — ${sg.category_name}</span>`}
                  <span class="meter v-transactions-sugg__meter" data-sugg-meter="${i}" role="img" aria-label="Confidence ${pct(sg.confidence, { decimals: 0 })}"><span class="meter__fill"></span></span>
                  <span class="num small">${pct(sg.confidence, { decimals: 0 })}</span>
                </li>`
              )}</ul>
            </div>`
          : ""}
      </section>

      <section class="stack-sm" aria-labelledby="txd-flags">
        <h3 class="caps" id="txd-flags">Data-quality checks</h3>
        ${flags.length
          ? html`<ul class="v-transactions-flaglist" role="list">${flags.map(
              (f) => html`<li><span class="badge badge--${flagInfo(f).tone}">${flagInfo(f).label}</span><span class="small text-2">${FLAG_HELP[f] || ""}</span></li>`
            )}</ul>`
          : html`<p class="row small text-2">${icon("check-circle", { size: "sm" })}<span>${FLAG_HELP.VALID}</span></p>`}
        ${t.is_duplicate_ignored ? html`<div class="callout callout--info small">${icon("info", { size: "sm" })}<div>Ignored as a duplicate: excluded from totals, Tally and analytics.</div></div>` : ""}
        ${ocrFix(prov)}
      </section>

      <section class="stack-sm" aria-labelledby="txd-prov">
        <h3 class="caps" id="txd-prov">Provenance</h3>
        <dl class="kv">
          <dt>Source file</dt><dd>${s ? s.source_filename : DASH}</dd>
          <dt>Bank / account</dt><dd>${s ? html`${s.bank_name || DASH} · <span class="mono">${s.account_number || t.source_account_number || DASH}</span>${s.ifsc_code ? html` · <span class="mono">${s.ifsc_code}</span>` : ""}` : html`<span class="mono">${t.source_account_number || DASH}</span>`}</dd>
          <dt>Holder</dt><dd>${t.source_holder_name || DASH}</dd>
          ${s ? html`<dt>Statement period</dt><dd>${date(s.period_start)} → ${date(s.period_end)}${s.is_ocr_derived ? html` <span class="badge badge--info">OCR</span>` : ""}</dd>` : ""}
          <dt>Row in statement</dt><dd class="tabular">${Number.isFinite(t.row_index) ? int(t.row_index + 1) : DASH}</dd>
          <dt>File SHA-256</dt><dd>${s && s.sha256_hash ? html`<span class="hash">${s.sha256_hash}</span>` : DASH}</dd>
          <dt>Parser</dt><dd>${p ? p.parser_version || DASH : prov ? DASH : html`<span class="muted small">loading…</span>`}</dd>
          <dt>Ingested</dt><dd>${p ? html`${dateTime(p.ingested_at)}${p.analyst ? html` <span class="muted small">by ${p.analyst}</span>` : ""}` : prov ? DASH : ""}</dd>
          <dt>Transaction ID</dt><dd class="mono xsmall">${t.tx_id}</dd>
        </dl>
      </section>
    </div>`);
    d.body.querySelectorAll("[data-sugg-meter]").forEach((m) => {
      const sg = sugg[Number(m.dataset.suggMeter)];
      setVars(m, { "--value": `${Math.max(2, Math.min(100, (Number(sg?.confidence) || 0) * 100)).toFixed(1)}%` });
    });
    d.setFooter(footer());
  };

  let prov = null;
  paint(null);
  loadProvenance(ctx)
    .then((p) => {
      prov = p;
      if (d.el.isConnected) paint(prov);
    })
    .catch(() => {
      prov = { provenance: [] };
      if (d.el.isConnected) paint(prov);
    });

  d.el.addEventListener("click", async (e) => {
    const a = e.target.closest("[data-txd-action]");
    if (a) {
      const def = actions.find((x) => x.id === a.dataset.txdAction);
      if (def) await withBusy(a, () => def.run(t, api));
      return;
    }
    const s = e.target.closest("[data-txd-assign]");
    if (s && onAssign) await onAssign(Number(s.dataset.txdAssign), t, api);
  });

  const api = {
    ...d,
    tx: () => t,
    update(next) {
      t = { ...t, ...next };
      if (d.el.isConnected) paint(prov);
    },
  };
  return api;
}

/** Duplicate-ignore toggle shared by the transactions drawer and the anomalies view. Returns the server result. */
export async function toggleDuplicate(ctx, tx) {
  const res = await ctx.api.post(`transactions/${encodeURIComponent(tx.tx_id)}/toggle-duplicate`, {}, { signal: undefined });
  toast.success(res.is_duplicate_ignored ? "Row ignored as a duplicate — excluded from totals and analytics." : "Row restored — included in totals and analytics again.", { title: "Duplicate check" });
  return res;
}

// --------------------------------------------------------------------------------------------- CSV export
const CSV_COLS = [
  ["txn_date", "Date"], ["value_date", "Value date"], ["source_holder_name", "Account holder"], ["source_account_number", "Account number"],
  ["payment_channel_srs", "Channel (SRS)"], ["payment_channel", "Channel (statement code)"], ["direction", "Dr/Cr"], ["raw_narration", "Narration"], ["extracted_counterparty", "Counterparty"],
  ["counterparty_vpa_or_acc", "Counterparty VPA/account"], ["counterparty_ifsc", "Counterparty IFSC"], ["utr_number", "UTR"],
  ["cheque_or_ref_no", "Cheque/ref no."], ["cheque_number", "Cheque number"], ["card_number_masked", "Card (masked)"], ["debit", "Debit"], ["credit", "Credit"], ["running_balance", "Balance"],
  ["category_id", "Category ID"], ["category_name", "Category"], ["confidence_score", "Confidence"], ["categorization_source", "Categorisation source"],
  ["tally_voucher_type", "Tally voucher"], ["tally_ledger_name", "Tally ledger"], ["anomaly_flags", "Flags"], ["is_suspense", "Suspense"],
  ["is_duplicate_ignored", "Duplicate ignored"], ["statement_id", "Statement ID"], ["tx_id", "Transaction ID"],
];

export function saveCsv(rows, filename) {
  return downloadCsv(filename, [CSV_COLS.map((c) => c[1]), ...rows.map((r) => CSV_COLS.map(([k]) => (k === "payment_channel_srs" ? channelOf(r) : r[k])))]);
}

// ------------------------------------------------------------------------------------------------ the view
const SORT_KEYS = ["txn_date", "debit", "credit", "amount", "running_balance", "payment_channel", "category_id", "category_name", "confidence_score", "extracted_counterparty", "source_holder_name", "raw_narration", "categorization_source", "row_index"];
const EXPORT_CAP = 100000;

function sortFromParams(p) {
  const key = p?.sort === "date" ? "txn_date" : p?.sort === "balance" ? "running_balance" : p?.sort;
  return SORT_KEYS.includes(key) ? { key, order: p.order === "desc" ? "desc" : "asc" } : { key: "txn_date", order: p?.order === "desc" ? "desc" : "asc" };
}
const sameObj = (a, b) => {
  const ka = Object.keys(a).sort();
  const kb = Object.keys(b).sort();
  return ka.length === kb.length && ka.every((k, i) => k === kb[i] && String(a[k]) === String(b[k]));
};

export async function mount(el, ctx) {
  const data = await ctx.store.ensureCaseData();
  if (!ctx.isCurrent()) return;
  const statements = data.statements || [];
  const categories = ctx.store.categories();

  render(
    el,
    html`<div class="stack-lg">
      <div data-slot="head"></div>
      <section class="card v-transactions-card" aria-label="Transactions grid">
        <div class="card__body" data-slot="grid"></div>
      </section>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);

  const drawHead = () => {
    const s = ctx.store.get().summary || {};
    render(
      slot("head"),
      pageHead({
        title: "Transactions",
        sub: html`${plural(s.transactions_count || 0, "row")} across ${plural(s.statements_count || 0, "statement")}
          · <span class="v-transactions-legend"><span class="v-transactions-st v-transactions-st--ok">${icon("check", { size: "sm" })}</span>valid
          <span class="v-transactions-st v-transactions-st--flag">${icon("alert", { size: "sm" })}</span>flagged
          <span class="v-transactions-st v-transactions-st--ignored">${icon("minus", { size: "sm" })}</span>ignored duplicate</span>`,
        actions: html`<button type="button" class="btn btn--sm" data-action="export-view">${icon("download", { size: "sm" })}Export view (CSV)</button>`,
      })
    );
  };
  drawHead();

  const initialSort = sortFromParams(ctx.params);
  const initialFilters = filtersFromParams(ctx.params);

  const columns = [
    { key: "status", label: "QC", width: "44px", align: "center", render: statusCell },
    { key: "txn_date", label: "Date", width: "108px", sortable: true, format: (v) => date(v), title: (t) => (t.value_date && t.value_date !== t.txn_date ? `Value date ${date(t.value_date)}` : "") },
    { key: "source_holder_name", label: "Account", width: "minmax(150px, 1fr)", sortable: true, render: accountCell },
    { key: "payment_channel", label: "Channel", width: "92px", sortable: true, render: channelCell },
    { key: "raw_narration", label: "Narration · party · UTR", width: "minmax(260px, 3fr)", sortable: true, render: narrationCell },
    { key: "debit", label: "Debit", width: "128px", align: "right", sortable: true, defaultOrder: "desc", className: "is-debit", format: (v) => (v > 0 ? inr(v) : DASH) },
    { key: "credit", label: "Credit", width: "128px", align: "right", sortable: true, defaultOrder: "desc", className: "is-credit", format: (v) => (v > 0 ? inr(v) : DASH) },
    { key: "running_balance", label: "Balance", width: "136px", align: "right", sortable: true, defaultOrder: "desc", format: (v) => inr(v) },
    {
      key: "category_name", label: "Category · confidence", width: "minmax(200px, 1.4fr)", sortable: true,
      title: (t) => `${catNo(t.category_id)} — ${t.category_name} (${sourceLabel(t.categorization_source)})`,
      render: (t) => html`<span class="badge ${t.is_suspense ? "badge--suspicious" : "badge--neutral"} v-transactions-cat">${catNo(t.category_id)} · ${t.category_name}</span><span class="dg__sub">${pct(t.confidence_score, { decimals: 0 })} · ${sourceLabel(t.categorization_source)}</span>`,
    },
    { key: "anomaly_flags", label: "Flags", width: "minmax(150px, 1fr)", render: (t) => flagChips(t, { max: 2, empty: html`<span class="muted xsmall">—</span>` }) },
    {
      key: "tally_ledger_name", label: "Tally voucher → ledger", width: "minmax(170px, 1fr)",
      render: (t) => html`<span class="truncate">${t.tally_ledger_name || DASH}</span><span class="dg__sub">${t.tally_voucher_type || DASH}</span>`,
    },
  ];

  const filters = [
    { key: "q", type: "search", label: "Search transactions", placeholder: "Narration, party, UTR, holder…" },
    { key: "category_id", type: "select", label: "Category", options: [{ value: "", label: "All categories" }, ...categoryFilterGroups(categories)] },
    { key: "channel", type: "select", label: "Channel (SRS)", options: [{ value: "", label: "All channels" }, ...channelFilterOptions(ctx.store.get().meta)] },
    { key: "direction", type: "select", label: "Direction", options: [{ value: "", label: "Dr & Cr" }, { value: "CR", label: "Credits only" }, { value: "DR", label: "Debits only" }] },
    ...(statements.length > 1
      ? [{ key: "statement_id", type: "select", label: "Account / statement", options: [{ value: "", label: "All accounts" }, ...statements.map((s) => ({ value: s.statement_id, label: `${s.account_holder_name || s.bank_name || "Account"} · ${s.bank_name || ""} ${s.account_number || ""}`.trim() }))] }]
      : []),
    { type: "date-range", label: "Date", keys: ["date_from", "date_to"] },
    { type: "number-range", label: "Amount ₹", keys: ["amount_min", "amount_max"] },
    { key: "suspense", type: "toggle", label: "Suspense only" },
    { key: "flagged", type: "toggle", label: "Flagged only" },
  ];

  let lastPage = { total: null };
  const grid = createDataGrid(slot("grid"), {
    ariaLabel: "Transactions",
    rowKey: "tx_id",
    rowClassName: txRowClass,
    columns,
    filters,
    initialFilters,
    initialSort,
    pageSize: 200,
    rowHeight: 48,
    height: "max(360px, calc(100dvh - 300px))",
    countNoun: ["transaction", "transactions"],
    fetchPage: ({ offset, limit, sort, order, filters: f, signal }) =>
      ctx.api.get("transactions", { offset, limit, sort, order, ...toServer(f) }, { signal }).then((r) => {
        lastPage = { total: r.total };
        return { rows: r.transactions || [], total: r.total };
      }),
    onRowClick: (t) => openDetail(t),
    onStateChange: ({ sort, order, filters: f }) => {
      const patch = Object.fromEntries(FILTER_KEYS.map((k) => [k, null]));
      patch.tx_ids = null; // the API-style alias is folded into `ids`
      Object.assign(patch, f, { sort: sort && sort !== "txn_date" ? sort : null, order: sort && (sort !== "txn_date" || order === "desc") ? order : null });
      ctx.setParams(patch);
      syncExtra();
    },
    emptyState: { icon: "upload", title: "No transactions yet", message: "Upload bank statements to populate this case.", action: { label: "Upload statements", href: ctx.href("ingest") } },
    toolbarExtra: html`<button type="button" class="btn btn--sm btn--ghost v-transactions-morebtn" data-action="toggle-filters" aria-expanded="false">${icon("filter", { size: "sm" })}Filters<span class="v-transactions-fcount" data-fcount></span></button>
      <span class="v-transactions-entity" data-entity-chip hidden></span>
      <span class="v-transactions-entity" data-ids-chip hidden></span>
      <span class="v-transactions-entity" data-stmt-chip hidden></span>
      <span class="v-transactions-entity" data-sort-chip hidden></span>
      <button type="button" class="btn btn--sm btn--ghost" data-action="clear-filters" hidden>${icon("x", { size: "sm" })}Clear filters</button>`,
  });
  ctx.onCleanup(() => grid.destroy());
  grid.el.classList.add("v-transactions-grid");

  const SORT_LABEL = { amount: "amount (larger of Dr/Cr)", confidence_score: "confidence", categorization_source: "categorisation source" };
  function syncExtra() {
    const f = grid.getFilters();
    const st = grid.getState();
    const sortChip = grid.toolbar.querySelector("[data-sort-chip]");
    if (st.sort && !columns.some((c) => c.sortable && (c.sortKey || c.key) === st.sort)) {
      render(sortChip, html`<span class="chip" aria-pressed="true">${icon(st.order === "desc" ? "sort-desc" : "sort-asc", { size: "sm" })}Sorted by ${SORT_LABEL[st.sort] || st.sort.replace(/_/g, " ")}
        <button type="button" class="btn btn--ghost btn--icon btn--sm" data-action="clear-sort" aria-label="Reset sort to date">${icon("x", { size: "sm" })}</button></span>`);
      sortChip.hidden = false;
    } else {
      sortChip.hidden = true;
      sortChip.replaceChildren();
    }
    const n = Object.keys(f).filter((k) => k !== "q").length;
    const clear = grid.toolbar.querySelector('[data-action="clear-filters"]');
    clear.hidden = Object.keys(f).length === 0;
    const fc = grid.toolbar.querySelector("[data-fcount]");
    fc.textContent = n ? ` (${n})` : "";
    const idsChip = grid.toolbar.querySelector("[data-ids-chip]");
    if (f.ids) {
      const n = parseIds(f.ids).length;
      render(idsChip, html`<span class="chip" aria-pressed="true" title="Showing only the transactions linked from the evidence you opened">${icon("filter", { size: "sm" })}Filtered to ${plural(n, "evidence row")}
        <button type="button" class="btn btn--ghost btn--icon btn--sm" data-action="clear-ids" aria-label="Show all transactions">${icon("x", { size: "sm" })}</button></span>`);
      idsChip.hidden = false;
    } else {
      idsChip.hidden = true;
      idsChip.replaceChildren();
    }
    // A statement filter arriving by link when the account select is not shown (single-statement case)
    const stmtChip = grid.toolbar.querySelector("[data-stmt-chip]");
    if (f.statement_id && statements.length <= 1) {
      const st = statements.find((x) => x.statement_id === f.statement_id);
      render(stmtChip, html`<span class="chip" aria-pressed="true">${icon("file", { size: "sm" })}${st ? `${st.account_holder_name || st.bank_name || "Statement"} · ${st.account_number || ""}` : "One statement"}
        <button type="button" class="btn btn--ghost btn--icon btn--sm" data-action="clear-stmt" aria-label="Remove statement filter">${icon("x", { size: "sm" })}</button></span>`);
      stmtChip.hidden = false;
    } else {
      stmtChip.hidden = true;
      stmtChip.replaceChildren();
    }
    const chip = grid.toolbar.querySelector("[data-entity-chip]");
    if (f.entity) {
      const ent = ctx.store.entity(f.entity);
      render(chip, html`<span class="chip" aria-pressed="true">${icon("user", { size: "sm" })}${ent ? ent.canonical_name : "Selected entity"}
        <button type="button" class="btn btn--ghost btn--icon btn--sm" data-action="clear-entity" aria-label="Remove entity filter">${icon("x", { size: "sm" })}</button></span>`);
      chip.hidden = false;
      ctx.setCrumbs([{ label: ent ? ent.canonical_name : "Entity" }]);
    } else {
      chip.hidden = true;
      chip.replaceChildren();
      ctx.setCrumbs([]);
    }
  }
  syncExtra();

  // ---------------------------------------------------------------- detail drawer
  let drawer = null;
  function openDetail(t) {
    drawer = openTxDrawer(t, ctx, {
      actions: [
        {
          id: "toggle-dup",
          when: (x) => x.is_duplicate_ignored || (x.anomaly_flags || []).includes("DUPLICATE_SUSPECT"),
          label: (x) => (x.is_duplicate_ignored ? "Restore row" : "Ignore duplicate"),
          icon: "eye",
          run: async (x, dw) => {
            const res = await toggleDuplicate(ctx, x);
            grid.updateRow(x.tx_id, { is_duplicate_ignored: res.is_duplicate_ignored });
            dw.update({ is_duplicate_ignored: res.is_duplicate_ignored });
            await selfMutated(["transactions", "anomalies", "suspense", "analytics"]);
          },
        },
      ],
    });
  }
  ctx.onCleanup(() => drawer && drawer.el.isConnected && drawer.close());

  // our own mutations already patched the rows; skip the topic-driven refetch they trigger
  let selfDepth = 0;
  async function selfMutated(topics) {
    selfDepth += 1;
    try {
      await ctx.mutated(topics);
    } finally {
      selfDepth -= 1;
    }
  }
  ctx.onData([TOPICS.transactions, TOPICS.suspense, TOPICS.anomalies, TOPICS.entities], () => {
    if (selfDepth > 0 || !ctx.isCurrent()) return;
    grid.refresh();
  });
  ctx.onCleanup(ctx.store.select((s) => s.summary, () => ctx.isCurrent() && drawHead()));

  // ---------------------------------------------------------------- toolbar / page actions
  async function exportView(btn) {
    const st = grid.getState();
    const total = st.total ?? lastPage.total ?? 0;
    if (!total) {
      toast.info("Nothing to export with the current filters.");
      return;
    }
    await withBusy(btn, async () => {
      const label = btn.lastChild;
      const orig = label.textContent;
      try {
        const { rows, truncated } = await loadAllTransactions(ctx, { sort: st.sort || "txn_date", order: st.order || "asc", ...toServer(st.filters) }, {
          cap: EXPORT_CAP,
          onProgress: (n, tot) => {
            label.textContent = `Exporting ${int(n)} / ${int(Math.min(tot, EXPORT_CAP))}…`;
          },
        });
        if (!ctx.isCurrent()) return;
        const c = ctx.store.get().case || ctx.case || {};
        const stamp = new Date().toISOString().slice(0, 10);
        saveCsv(rows, `${String(c.case_id || ctx.caseId).replace(/[^\w.-]+/g, "_")}_transactions_${stamp}.csv`);
        if (truncated) toast.warn(`Exported the first ${int(rows.length)} of ${int(total)} rows. Narrow the filters to export the rest.`, { title: "Export capped" });
        else toast.success(`Exported ${plural(rows.length, "row")} matching the current view.`, { title: "CSV ready" });
      } finally {
        label.textContent = orig;
      }
    }, { error: "Export failed" });
  }

  ctx.onCleanup(
    delegate(el, "click", "[data-action]", (e, b) => {
      const a = b.dataset.action;
      if (a === "export-view") exportView(b);
      else if (a === "clear-filters") grid.setFilters({}, { replace: true });
      else if (a === "clear-sort") grid.setSort("txn_date", "asc");
      else if (a === "clear-entity" || a === "clear-ids" || a === "clear-stmt") {
        const f = grid.getFilters();
        delete f[a === "clear-entity" ? "entity" : a === "clear-ids" ? "ids" : "statement_id"];
        grid.setFilters(f, { replace: true });
      } else if (a === "toggle-filters") {
        const open = grid.el.classList.toggle("v-transactions-filters-open");
        b.setAttribute("aria-expanded", open ? "true" : "false");
      }
    })
  );

  const quick = (patch) => grid.setFilters(patch);
  ctx.registerCommands([
    { id: "transactions.search", title: "Search transactions", group: "Transactions", icon: "search", hint: "/", run: () => grid.el.querySelector('input[type="search"]')?.focus() },
    { id: "transactions.suspense", title: "Show suspense rows only", group: "Transactions", icon: "inbox", run: () => quick({ suspense: "true" }) },
    { id: "transactions.flagged", title: "Show flagged rows only", group: "Transactions", icon: "alert", run: () => quick({ flagged: "true" }) },
    { id: "transactions.credits", title: "Show credits only", group: "Transactions", icon: "arrow-down-left", run: () => quick({ direction: "CR" }) },
    { id: "transactions.debits", title: "Show debits only", group: "Transactions", icon: "arrow-up-right", run: () => quick({ direction: "DR" }) },
    { id: "transactions.largest", title: "Sort by largest amount", group: "Transactions", icon: "sort-desc", run: () => grid.setSort("amount", "desc") },
    { id: "transactions.clear", title: "Clear transaction filters", group: "Transactions", icon: "x", run: () => grid.setFilters({}, { replace: true }) },
    { id: "transactions.export", title: "Export current view (CSV)", group: "Transactions", icon: "download", keywords: ["csv", "download"], run: () => exportView(el.querySelector('[data-action="export-view"]')) },
  ]);

  // "/" focuses the search box (ignored while typing elsewhere)
  const onKey = (e) => {
    if (e.key !== "/" || e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = (e.target && e.target.tagName) || "";
    if (/INPUT|TEXTAREA|SELECT/.test(tag) || e.target?.isContentEditable || document.querySelector("dialog[open]")) return;
    const s = grid.el.querySelector('input[type="search"]');
    if (s) {
      e.preventDefault();
      s.focus();
    }
  };
  document.addEventListener("keydown", onKey);
  ctx.onCleanup(() => document.removeEventListener("keydown", onKey));

  return {
    update(params) {
      const f = filtersFromParams(params);
      const s = sortFromParams(params);
      const st = grid.getState();
      if (s.key !== st.sort || s.order !== st.order) grid.setSort(s.key, s.order, { silent: true });
      if (!sameObj(f, st.filters)) grid.setFilters(f, { replace: true, silent: true });
      syncExtra();
    },
  };
}

/** Exposed for the anomalies view's flag legend. */
export const FLAG_KEYS = ALL_FLAG_KEYS;
