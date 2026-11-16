/* CypherEdge · Tally (#/case/{id}/tally?suspense=&trade=&company=&tab=)
 *
 * Structured preview of the Tally Prime / ERP 9 import (GET edge/tally, EDGE-28): the server returns
 * { stats, vouchers[], ledgers[], groups[], xml_preview, xml_truncated, xml_sha256, xml_bytes, ledger_map } —
 * the UI no longer parses XML. Options: include suspense, Sales / Purchase vouchers (trade=1|0; absent = the stored
 * ledger map's use_trade_vouchers) and the company name (absent = the map's company_name). The ledger map
 * (category → group / ledger, counterparty ledgers, custom groups, Sales / Purchase categories, defaults) is edited
 * in views/tally-map.js and saved per case (edge/tally-map); it applies to the preview, both exports and the push.
 * Downloads: export/tally-xml and export/tally-xlsx (Blob, audit-logged). Push: POST edge/tally-push, reported
 * through describeTallyPushResult().
 */
import { isAbort } from "../core/api.js";
import { getTallyConnection, setTallyConnection } from "../core/constants.js";
import { announce, delegate, html, render } from "../core/dom.js";
import { bytes, catNo, date, DASH, inr, inrCompact, int, plural } from "../core/format.js";
import { describeTallyPushResult } from "../core/results.js";
import { TOPICS } from "../core/store.js";
import { createDataGrid } from "../components/datagrid.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { openModal } from "../components/modal.js";
import { emptyState, errorState, pageHead, skeleton } from "../components/states.js";
import { createTabs } from "../components/tabs.js";
import { describeMapSummary, openTallyMapEditor } from "./tally-map.js";

export const styles = ["views/edge.css"];

const TYPE_TONE = { Receipt: "info", Sales: "info", Payment: "accent", Purchase: "accent", Contra: "neutral", Journal: "neutral" };
const KIND_LABEL = { bank: "Bank", cash: "Cash", suspense: "Suspense", transfer_clearing: "Transfer clearing", party: "Party" };
const KIND_TONE = { bank: "info", cash: "neutral", suspense: "suspicious", transfer_clearing: "outline", party: "neutral" };
const TABS = ["vouchers", "ledgers", "groups", "xml"];

const isOn = (v) => v === "1" || v === "true";
/** trade URL param: "1" / "0" force the voucher style; absent = the stored map decides (null). */
const triState = (v) => (v === "1" || v === "true" ? true : v === "0" || v === "false" ? false : null);

/** Server voucher -> grid row (Dr / Cr ledgers from the signed entries; balanced when the legs net to zero). */
function toRow(v, i) {
  const entries = v.entries || [];
  const dr = entries.filter((e) => e.is_deemed_positive);
  const cr = entries.filter((e) => !e.is_deemed_positive);
  const net = entries.reduce((s, e) => s + (Number(e.amount) || 0), 0);
  const isBank = (name) => name && name === v.bank_ledger;
  const bankDr = dr.some((e) => isBank(e.ledger));
  const bankCr = cr.some((e) => isBank(e.ledger));
  return {
    ...v,
    key: v.guid || v.voucher_number || `v${i}`,
    seq: i,
    drLedger: dr.map((e) => e.ledger).join(", "),
    crLedger: cr.map((e) => e.ledger).join(", "),
    flow: v.voucher_type === "Contra" ? "contra" : bankDr || v.direction === "CR" ? "in" : bankCr || v.direction === "DR" ? "out" : "none",
    balanced: Math.abs(net) < 0.005,
  };
}

/* -------------------------------------------------------------------------------------------- view */
export async function mount(el, ctx) {
  const opts = {
    includeSuspense: isOn(ctx.params.suspense),
    trade: triState(ctx.params.trade),
    company: ctx.params.company || "",
  };
  let data = null; // { stats, vouchers, ledgers, groups, xml_*, ledger_map }
  let ctl = null;
  let gen = 0;
  const grids = {};
  ctx.onCleanup(() => {
    ctl?.abort();
    Object.values(grids).forEach((g) => g.destroy());
  });

  const query = () => ({
    include_suspense: opts.includeSuspense ? "true" : "false",
    use_trade_vouchers: opts.trade == null ? "" : opts.trade ? "true" : "false",
    company_name: opts.company.trim(),
  });
  const effectiveTrade = () => (opts.trade == null ? Boolean(data?.stats?.use_trade_vouchers ?? data?.ledger_map?.use_trade_vouchers) : opts.trade);

  render(
    el,
    html`<div class="stack-lg">
      ${pageHead({
        title: "Tally export",
        sub: "Vouchers and ledger masters for Tally Prime / ERP 9 import, built from categorised bank transactions and this case's ledger map.",
        actions: html`<button type="button" class="btn" data-action="tally-map">${icon("settings", { size: "sm" })}Ledger map…</button>
          <button type="button" class="btn" data-action="tally-push">${icon("upload", { size: "sm" })}Push to Tally…</button>
          <button type="button" class="btn" data-action="tally-xlsx">${icon("table", { size: "sm" })}Excel</button>
          <button type="button" class="btn btn--primary" data-action="tally-download">${icon("download", { size: "sm" })}Download XML</button>`,
      })}
      <section class="card" aria-label="Export options">
        <div class="card__body v-tally-options">
          <div class="v-tally-opt">
            <button type="button" class="switch" role="switch" id="tally-opt-suspense" aria-checked="${opts.includeSuspense ? "true" : "false"}" data-opt="includeSuspense"></button>
            <label for="tally-opt-suspense" class="v-tally-opt__text"><span class="strong small">Include suspense</span>
              <span class="xsmall muted">Post uncategorised rows to Suspense A/c instead of skipping them</span></label>
          </div>
          <div class="v-tally-opt">
            <button type="button" class="switch" role="switch" id="tally-opt-trade" aria-checked="${opts.trade ? "true" : "false"}" data-opt="trade" aria-describedby="tally-opt-trade-src"></button>
            <label for="tally-opt-trade" class="v-tally-opt__text"><span class="strong small">Sales / Purchase vouchers</span>
              <span class="xsmall muted">Trade receipts and payments as Sales / Purchase instead of Receipt / Payment</span>
              <span class="xsmall muted" id="tally-opt-trade-src" data-slot="trade-src"></span></label>
          </div>
          <div class="field v-tally-company">
            <label class="field__label" for="tally-opt-company">Tally company</label>
            <input class="input input--sm" id="tally-opt-company" name="company" autocomplete="off" maxlength="120"
              placeholder="Company open in Tally" value="${opts.company}" aria-describedby="tally-opt-company-hint" />
            <span class="field__hint" id="tally-opt-company-hint" data-slot="company-hint">Blank uses the ledger map's company</span>
          </div>
        </div>
      </section>
      <div data-slot="body">${skeleton({ kind: "kpis", rows: 6 })}${skeleton({ kind: "table", rows: 8 })}</div>
    </div>`
  );
  const body = el.querySelector('[data-slot="body"]');
  const companyInput = el.querySelector("#tally-opt-company");

  /* ---------------------------------------------------------------- loading */
  async function load({ quiet = false } = {}) {
    ctl?.abort();
    ctl = new AbortController();
    const myGen = ++gen;
    if (data && quiet) body.setAttribute("aria-busy", "true");
    else if (!data) render(body, html`${skeleton({ kind: "kpis", rows: 6 })}${skeleton({ kind: "table", rows: 8 })}`);
    try {
      const res = await ctx.api.get("edge/tally", query(), { signal: ctl.signal });
      if (!ctx.isCurrent() || myGen !== gen) return;
      data = {
        ...res,
        stats: res.stats || {},
        rows: (res.vouchers || []).map(toRow),
        ledgers: res.ledgers || [],
        groups: res.groups || [],
      };
      drawOptions();
      draw();
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent() || myGen !== gen) return;
      data = null;
      destroyGrids();
      tabs = null;
      render(body, errorState(err, { retryId: "tally-retry", title: "Could not build the Tally preview" }));
    } finally {
      if (myGen === gen) body.removeAttribute("aria-busy");
    }
  }

  function destroyGrids() {
    for (const k of Object.keys(grids)) {
      grids[k].destroy();
      delete grids[k];
    }
  }

  /** Reflect the effective (server-applied) trade setting and company in the option controls. */
  function drawOptions() {
    const lm = data?.ledger_map || {};
    el.querySelector('[data-opt="trade"]').setAttribute("aria-checked", effectiveTrade() ? "true" : "false");
    el.querySelector('[data-slot="trade-src"]').textContent = opts.trade == null
      ? `Ledger map default (${lm.use_trade_vouchers ? "on" : "off"}) · Sales: ${(lm.sales_categories || []).map(catNo).join(", ") || "none"} · Purchase: ${(lm.purchase_categories || []).map(catNo).join(", ") || "none"}`
      : `Overridden for this page · map default is ${lm.use_trade_vouchers ? "on" : "off"}`;
    companyInput.placeholder = lm.company_name ? `Map default: ${lm.company_name}` : "Company open in Tally";
    el.querySelector('[data-slot="company-hint"]').textContent = opts.company.trim()
      ? "Overrides the ledger map's company for this export"
      : lm.company_name ? `Blank uses “${lm.company_name}” from the ledger map` : "Blank imports into the company open in Tally";
  }

  /* ---------------------------------------------------------------- drawing */
  let tabs = null;
  function draw() {
    const { stats, rows, ledgers, groups } = data;
    const empty = !rows.length;
    announce(empty ? "Tally preview: no vouchers to export." : `Tally preview ready: ${int(rows.length)} vouchers, ${int(ledgers.length)} ledgers.`);
    if (empty) {
      destroyGrids();
      tabs = null;
      render(
        body,
        html`<div class="stack">${drawKpis()}${drawCallouts()}
          <div class="card"><div class="card__body">${emptyState({
            icon: "file",
            title: stats.skipped_suspense ? "Every transaction is still in suspense" : "No vouchers to export",
            message: stats.skipped_suspense
              ? "Categorise suspense items or switch on “Include suspense” to post them to Suspense A/c."
              : "Upload bank statements for this case to generate Tally vouchers.",
            action: stats.skipped_suspense ? { label: "Open Suspense", href: ctx.href("suspense") } : { label: "Upload statements", href: ctx.href("ingest") },
          })}</div></div></div>`
      );
      return;
    }
    if (!tabs) {
      render(body, html`<div class="stack"><div data-slot="kpis"></div><div data-slot="callouts"></div><section class="card" aria-label="Tally import preview"><div class="card__body" data-slot="tabs"></div></section></div>`);
      tabs = createTabs(body.querySelector('[data-slot="tabs"]'), {
        label: "Tally preview sections",
        tabs: [
          { id: "vouchers", label: "Vouchers", icon: "list", badge: int(rows.length) },
          { id: "ledgers", label: "Ledgers", icon: "table", badge: int(ledgers.length) },
          { id: "groups", label: "Groups", icon: "layers", badge: int(groups.length) },
          { id: "xml", label: "XML", icon: "file" },
        ],
        selected: TABS.includes(ctx.params.tab) ? ctx.params.tab : "vouchers",
        onChange: (id, panel) => {
          if (ctx.params.tab !== id) ctx.setParams({ tab: id === "vouchers" ? null : id });
          drawPanel(id, panel);
        },
      });
    } else {
      tabs.setBadge("vouchers", int(rows.length));
      tabs.setBadge("ledgers", int(ledgers.length));
      tabs.setBadge("groups", int(groups.length));
      TABS.forEach((id) => {
        if (id === tabs.selected || grids[id] || id === "xml" || id === "groups") drawPanel(id, tabs.panel(id), { refresh: true });
      });
    }
    render(body.querySelector('[data-slot="kpis"]'), drawKpis());
    render(body.querySelector('[data-slot="callouts"]'), drawCallouts());
  }

  function drawKpis() {
    const s = data.stats;
    const total = data.rows.reduce((a, v) => a + (Number(v.amount) || 0), 0);
    const breakdown = Object.entries(s.voucher_breakdown || {});
    return kpiGrid(
      [
        { label: "Vouchers", value: int(s.vouchers_exported ?? data.rows.length), icon: "list",
          sub: breakdown.length ? breakdown.map(([k, n]) => `${k} ${int(n)}`).join(" · ") : "none" },
        { label: "Voucher value", value: inrCompact(total), title: inr(total), icon: "swap", sub: `numbered ${s.voucher_prefix || "CS"}-000001…` },
        { label: "Ledgers created", value: int(s.ledgers_created), icon: "table", sub: s.counterparty_overrides_applied ? `${plural(s.counterparty_overrides_applied, "counterparty override")} applied` : "from categories and parties" },
        { label: "Groups created", value: int(s.groups_created), sub: "custom groups from the ledger map" },
        { label: "Skipped suspense", value: int(s.skipped_suspense), tone: s.skipped_suspense ? "warn" : "success",
          sub: opts.includeSuspense ? "included as Suspense A/c" : "not exported", href: s.skipped_suspense ? ctx.href("suspense") : "" },
        { label: "Skipped zero-amount", value: int(s.skipped_zero_amount), sub: "rows with no debit/credit" },
        { label: "Mirror legs de-duplicated", value: int(s.mirror_legs_deduped), sub: "own-account transfers posted once" },
        { label: "Unmatched own transfers", value: int(s.unmatched_own_transfers), tone: s.unmatched_own_transfers ? "warn" : "success",
          sub: s.unmatched_own_transfers ? "other leg not in case" : "all transfers paired" },
      ],
      { label: "Tally export statistics", compact: true }
    );
  }

  function drawCallouts() {
    const s = data.stats;
    const lm = data.ledger_map || {};
    const unbalanced = data.rows.filter((v) => !v.balanced).length;
    const parts = [];
    if (s.skipped_suspense && !opts.includeSuspense) {
      parts.push(html`<div class="callout callout--warn">${icon("alert")}<div><strong>${plural(s.skipped_suspense, "suspense transaction")} not exported.</strong>
        Resolve them in <a href="${ctx.href("suspense")}">Suspense</a> or switch on “Include suspense” to post them to Suspense A/c.</div></div>`);
    }
    if (s.unmatched_own_transfers) {
      parts.push(html`<div class="callout callout--info">${icon("info")}<div><strong>${plural(s.unmatched_own_transfers, "own-account transfer")} without a matching leg.</strong>
        The counter-account statement is not in this case, so the amount is posted to “Own-Account Transfers (Unmatched)” under Suspense A/c. Upload the other statement to pair it as a Contra.</div></div>`);
    }
    if (unbalanced) {
      parts.push(html`<div class="callout callout--danger">${icon("alert")}<div><strong>${plural(unbalanced, "voucher")} do not balance.</strong> Debit and credit legs differ; Tally will reject them on import.</div></div>`);
    }
    const summary = describeMapSummary(lm);
    parts.push(html`<div class="callout">${icon("info")}<div class="row-wrap v-tally-mapnote">
      <span>${s.company_name ? html`Vouchers target the Tally company <strong>${s.company_name}</strong>.` : "Vouchers import into the company open in Tally."}
        ${summary ? html`Ledger map: ${summary}.` : "Default ledger map."}</span>
      <button type="button" class="btn btn--sm btn--ghost" data-action="tally-map">Edit ledger map</button></div></div>`);
    return html`<div class="stack-sm">${parts}</div>`;
  }

  function drawPanel(id, panel, { refresh = false } = {}) {
    if (!data) return;
    if (id === "vouchers") {
      const types = Array.from(new Set(data.rows.map((v) => v.voucher_type))).sort();
      if (grids.vouchers && grids.vouchers.typesKey !== types.join("|")) {
        // the voucher-type filter options changed (e.g. Sales / Purchase switched on): rebuild the grid
        grids.vouchers.destroy();
        delete grids.vouchers;
      }
      if (grids.vouchers) {
        if (refresh) grids.vouchers.setRows(data.rows);
        return;
      }
      grids.vouchers = createDataGrid(panel, {
        ariaLabel: "Tally vouchers",
        rowKey: "key",
        rows: data.rows,
        height: "min(62vh, 560px)",
        rowHeight: 48,
        countNoun: ["voucher", "vouchers"],
        initialSort: { key: "date", order: "asc" },
        filters: [
          { key: "q", type: "search", label: "Search vouchers", placeholder: "Ledger, narration, voucher no, UTR…" },
          { key: "type", type: "select", label: "Voucher type", options: [{ value: "", label: "All voucher types" }, ...types.map((t) => ({ value: t, label: t }))] },
        ],
        clientFilter: (v, f) => {
          if (f.type && v.voucher_type !== f.type) return false;
          if (f.q) {
            const q = String(f.q).toLowerCase();
            return `${v.voucher_number} ${v.drLedger} ${v.crLedger} ${v.narration} ${v.utr || ""}`.toLowerCase().includes(q);
          }
          return true;
        },
        onRowClick: (v) => (v.tx_id ? ctx.navigate("transactions", { ids: [v.tx_id, v.mirror_tx_id].filter(Boolean).join(",") }) : null),
        emptyState: { icon: "list", title: "No vouchers" },
        columns: [
          { key: "date", label: "Date", width: "112px", sortable: true, format: (v) => date(v) },
          { key: "voucher_number", label: "Voucher no.", width: "120px", sortable: true, sortValue: (v) => v.seq,
            render: (v) => html`<span class="mono small">${v.voucher_number || DASH}</span>`, title: (v) => (v.guid ? `GUID ${v.guid}` : "") },
          { key: "voucher_type", label: "Type", width: "112px", sortable: true,
            render: (v) => html`<span class="badge badge--${TYPE_TONE[v.voucher_type] || "outline"}">${v.voucher_type}</span>${v.balanced ? "" : html` <span class="badge badge--danger" title="Debit and credit legs differ">Unbalanced</span>`}` },
          { key: "amount", label: "Amount", width: "150px", align: "right", sortable: true, defaultOrder: "desc",
            className: (v) => (v.flow === "in" ? "is-credit" : v.flow === "out" ? "is-debit" : ""),
            title: (v) => `${v.flow === "in" ? "Into bank" : v.flow === "out" ? "Out of bank" : v.flow === "contra" ? "Bank to bank" : "Non-bank"} · ${inr(v.amount)}`,
            render: (v) => html`${v.flow === "in" ? "+" : v.flow === "out" ? "−" : ""}${inr(v.amount)}` },
          { key: "drLedger", label: "Debit ledger (Dr)", width: "minmax(170px, 1.3fr)", sortable: true, title: (v) => v.drLedger,
            render: (v) => html`<span class="truncate">${v.drLedger || DASH}</span>` },
          { key: "crLedger", label: "Credit ledger (Cr)", width: "minmax(170px, 1.3fr)", sortable: true, title: (v) => v.crLedger,
            render: (v) => html`<span class="truncate">${v.crLedger || DASH}</span>` },
          { key: "category_id", label: "Cat.", width: "64px", align: "right", sortable: true, title: (v) => ctx.store.categoryLabel(v.category_id),
            render: (v) => html`<span class="tabular">${v.category_id != null ? catNo(v.category_id) : DASH}</span>` },
          { key: "narration", label: "Narration", width: "minmax(240px, 2fr)", sortable: true, title: (v) => v.narration,
            render: (v) => html`<span class="v-tally-narr">${v.narration || DASH}</span>` },
        ],
      });
      grids.vouchers.typesKey = types.join("|");
    } else if (id === "ledgers") {
      if (grids.ledgers) {
        if (refresh) grids.ledgers.setRows(data.ledgers);
        return;
      }
      grids.ledgers = createDataGrid(panel, {
        ariaLabel: "Tally ledger masters",
        rowKey: "name",
        rows: data.ledgers,
        height: "min(62vh, 560px)",
        countNoun: ["ledger", "ledgers"],
        initialSort: { key: "voucher_count", order: "desc" },
        filters: [
          { key: "q", type: "search", label: "Search ledgers", placeholder: "Ledger or group…" },
          { key: "kind", type: "select", label: "Kind", options: [{ value: "", label: "All kinds" }, ...Object.entries(KIND_LABEL).map(([value, label]) => ({ value, label }))] },
        ],
        clientFilter: (l, f) => (!f.kind || l.kind === f.kind) && (!f.q || `${l.name} ${l.parent}`.toLowerCase().includes(String(f.q).toLowerCase())),
        onRowClick: (l) => {
          tabs?.select("vouchers");
          grids.vouchers?.setFilters({ q: l.name, type: "" });
        },
        emptyState: { icon: "table", title: "No ledger masters" },
        columns: [
          { key: "name", label: "Ledger", width: "minmax(220px, 2fr)", sortable: true, render: (l) => html`<span class="strong">${l.name}</span>` },
          { key: "parent", label: "Parent group", width: "minmax(170px, 1.2fr)", sortable: true,
            render: (l) => html`<span class="badge ${/suspense/i.test(l.parent) ? "badge--suspicious" : /bank accounts/i.test(l.parent) ? "badge--info" : "badge--neutral"}">${l.parent || DASH}</span>` },
          { key: "kind", label: "Kind", width: "130px", sortable: true, render: (l) => html`<span class="badge badge--${KIND_TONE[l.kind] || "outline"}">${KIND_LABEL[l.kind] || l.kind || DASH}</span>` },
          { key: "voucher_count", label: "Vouchers", width: "100px", align: "right", sortable: true, defaultOrder: "desc", format: (v) => int(v) },
          { key: "amount", label: "Amount", width: "150px", align: "right", sortable: true, defaultOrder: "desc", format: (v) => (v ? inr(v) : DASH) },
        ],
      });
    } else if (id === "groups") {
      const gs = data.groups;
      render(
        panel,
        gs.length
          ? html`<div class="table-wrap"><table class="table table--compact"><caption class="sr-only">Custom Tally groups created by this export</caption>
              <thead><tr><th scope="col">Group</th><th scope="col">Under</th></tr></thead>
              <tbody>${gs.map((g) => html`<tr><td class="strong">${g.name ?? g}</td><td>${g.parent || DASH}</td></tr>`)}</tbody></table></div>`
          : emptyState({ icon: "layers", title: "No custom groups", message: "Every ledger sits under one of Tally's predefined groups. Add custom groups in the ledger map.",
              action: { label: "Edit ledger map", id: "tally-map" }, compact: true })
      );
    } else if (id === "xml") {
      render(
        panel,
        html`<div class="stack-sm">
          <div class="row-between row-wrap">
            <dl class="kv v-tally-xmlmeta">
              <dt>Size</dt><dd class="tabular">${bytes(data.xml_bytes)}</dd>
              <dt>SHA-256</dt><dd>${data.xml_sha256 ? html`<span class="hash">${data.xml_sha256}</span>` : html`<span class="muted">${DASH}</span>`}</dd>
            </dl>
            <div class="row">
              <button type="button" class="btn btn--sm" data-action="tally-download">${icon("download", { size: "sm" })}Download XML</button>
            </div>
          </div>
          <p class="xsmall muted">Computed by the server over the exact file; the same hash is recorded in the audit trail when it is downloaded or pushed.</p>
          ${data.xml_truncated ? html`<div class="callout callout--info">${icon("info")}<div>Showing the start of the file only. Download it for the complete XML (${bytes(data.xml_bytes)}).</div></div>` : ""}
          <pre class="code v-tally-xml" tabindex="0" aria-label="Tally XML preview"></pre>
        </div>`
      );
      panel.querySelector("pre").textContent = data.xml_preview || "";
    }
  }

  /* ---------------------------------------------------------------- actions */
  async function download(btn) {
    await ctx.withBusy(
      btn,
      async () => {
        const fallback = `${ctx.caseId}_tally_import.xml`;
        const r = await ctx.api.download("export/tally-xml", fallback, { params: query() });
        ctx.mutated([]).catch(() => null); // export is audit-logged; refresh the overview's audit trail
        return r;
      },
      { success: (r) => `Saved ${r.filename} (${bytes(r.size)})`, error: "Download failed" }
    );
  }

  async function downloadXlsx(btn) {
    await ctx.withBusy(
      btn,
      async () => {
        const q = query();
        const r = await ctx.api.download("export/tally-xlsx", `${ctx.caseId}_tally_import.xlsx`, {
          params: { include_suspense: q.include_suspense, company_name: q.company_name, trade_vouchers: q.use_trade_vouchers },
        });
        ctx.mutated([]).catch(() => null);
        return r;
      },
      { success: (r) => `Saved ${r.filename} (${bytes(r.size)})`, error: "Excel export failed" }
    );
  }

  let mapOpen = false;
  async function editMap() {
    if (mapOpen) return; // the editor fetches the map first; ignore repeat clicks meanwhile
    mapOpen = true;
    try {
      const saved = await openTallyMapEditor({ api: ctx.api, categories: ctx.store.categories(), toast: ctx.toast });
      if (!saved || !ctx.isCurrent()) return;
      ctx.mutated([]).catch(() => null); // audit-logged (TALLY_MAP_UPDATE)
      load({ quiet: true });
    } catch (err) {
      if (!isAbort(err)) ctx.toast.error(err, { title: "Could not open the ledger map" });
    } finally {
      mapOpen = false;
    }
  }

  function openPush() {
    const s = data?.stats || {};
    const { host, port } = getTallyConnection(); // defaults from Settings → Tally connection
    const m = openModal({
      title: "Push to Tally",
      description: "Sends the vouchers and ledger masters to Tally's XML listener (Gateway of Tally → F1 Help → Settings → Connectivity → port).",
      size: "md",
      body: html`<div class="stack">
        <div class="form-grid v-tally-push-grid">
          <div class="field">
            <label class="field__label" for="tally-push-host">Host</label>
            <input class="input" id="tally-push-host" name="host" required maxlength="253" autocomplete="off" spellcheck="false"
              value="${host}" />
            <span class="field__hint">Loopback, or a host listed in CYPHERSOL_TALLY_HOSTS</span>
          </div>
          <div class="field">
            <label class="field__label" for="tally-push-port">Port</label>
            <input class="input input--num" id="tally-push-port" name="port" type="number" required min="1" max="65535" step="1" value="${port}" />
          </div>
        </div>
        <div class="callout">${icon("info")}<div>
          ${data ? html`Will send <strong>${plural(data.rows.length, "voucher")}</strong> and <strong>${plural(data.ledgers.length, "ledger master")}</strong>` : "Will send the current export"}
          ${opts.includeSuspense ? " including suspense rows" : s.skipped_suspense ? html` (${plural(s.skipped_suspense, "suspense row")} excluded)` : ""}${effectiveTrade() ? ", as Sales / Purchase vouchers" : ""}${s.company_name ? html` into company <strong>${s.company_name}</strong>` : " into the company open in Tally"}.
          Re-pushing creates duplicates unless Tally matches the voucher GUIDs.</div></div>
        <div data-slot="push-result" aria-live="polite"></div>
      </div>`,
      actions: [
        { label: "Close", value: "close" },
        { label: "Push to Tally", variant: "primary", type: "submit" },
      ],
      initialFocus: "#tally-push-host",
      onSubmit: async (values, { el: dlg }) => {
        const out = dlg.querySelector('[data-slot="push-result"]');
        const payload = {
          host: String(values.host || "").trim(),
          port: Number(values.port),
          include_suspense: opts.includeSuspense,
          use_trade_vouchers: opts.trade == null ? undefined : opts.trade,
          company_name: opts.company.trim() || undefined,
        };
        setTallyConnection({ host: payload.host, port: payload.port });
        render(out, html`<div class="callout"><span class="spinner" aria-hidden="true"></span><div>Contacting Tally at <span class="mono">${payload.host}:${payload.port}</span>…</div></div>`);
        try {
          const res = await ctx.api.post("edge/tally-push", payload);
          if (!dlg.isConnected) return false;
          const d = describeTallyPushResult(res);
          render(out, pushResult(res, d));
          if (d.ok) ctx.toast.success(d.text, { title: d.title });
          else ctx.toast(d.text, { type: d.tone === "error" ? "error" : "warn", title: d.title });
          ctx.mutated([]).catch(() => null); // push is audit-logged
        } catch (err) {
          if (isAbort(err)) return false;
          // a 403 here is the server's Tally host allow-list, not a lost session: show its own text
          const msg = err?.status === 403 && /tally/i.test(err.serverMessage || "") ? err.serverMessage : err?.message || String(err);
          if (dlg.isConnected) {
            render(out, html`<div class="callout callout--danger" role="alert">${icon("alert")}<div><strong>Push rejected</strong><div>${msg}${err?.errorId ? ` (ref ${err.errorId})` : ""}</div></div></div>`);
          }
        }
        return false; // keep the dialog open to show the result
      },
    });
    return m;
  }


  function pushResult(res, d) {
    const tone = d.tone === "error" ? "danger" : d.tone;
    const counts = ["created", "altered", "combined", "deleted", "ignored", "errors", "cancelled", "exceptions"].filter((k) => typeof res[k] === "number" && (["created", "altered", "errors", "exceptions"].includes(k) || res[k] > 0));
    const lines = Array.isArray(res.line_errors) ? res.line_errors : [];
    return html`<div class="stack-sm">
      <div class="callout callout--${tone}" role="${d.ok ? "status" : "alert"}">${icon(d.ok ? "check-circle" : "alert")}<div><strong>${d.title}</strong><div>${d.text}</div></div></div>
      ${counts.length
        ? html`<div class="v-tally-counts" role="list" aria-label="Tally import counters">${counts.map(
            (k) => html`<div class="v-tally-count ${k === "errors" || k === "exceptions" ? (res[k] ? "is-bad" : "") : ""}" role="listitem">
              <span class="xsmall muted caps">${k}</span><span class="strong tabular">${int(res[k])}</span></div>`
          )}</div>`
        : ""}
      <dl class="kv small">
        <dt>Listener</dt><dd class="mono">${res.url || DASH}${res.http_status ? ` · HTTP ${res.http_status}` : ""}</dd>
        <dt>Connected</dt><dd>${res.connected ? "Yes" : "No"}</dd>
        ${res.last_voucher_id ? html`<dt>Last voucher id</dt><dd class="mono">${res.last_voucher_id}</dd>` : ""}
      </dl>
      ${lines.length
        ? html`<details class="v-tally-lineerrors" open><summary class="small strong">${plural(lines.length, "line error")} reported by Tally</summary>
            <ol class="v-tally-lineerrors__list mono xsmall">${lines.slice(0, 200).map((l) => html`<li>${l}</li>`)}</ol></details>`
        : ""}
      ${!res.connected && !/download/i.test(res.message || "") ? html`<p class="xsmall muted">Tip: download the XML and import it in Tally via Gateway → Import → Vouchers.</p>` : ""}
    </div>`;
  }

  function setOption(key, value, { reload = true } = {}) {
    if (opts[key] === value) return;
    opts[key] = value;
    const sw = el.querySelector(`[data-opt="${key}"]`);
    if (sw) sw.setAttribute("aria-checked", value ? "true" : "false");
    if (key === "company" && companyInput.value !== value) companyInput.value = value;
    ctx.setParams({
      suspense: opts.includeSuspense ? "1" : null,
      trade: opts.trade == null ? null : opts.trade ? "1" : "0",
      company: opts.company.trim() || null,
    });
    if (reload) load({ quiet: true });
  }

  /* ---------------------------------------------------------------- events */
  ctx.onCleanup(delegate(el, "click", "[data-opt]", (e, sw) => {
    const key = sw.dataset.opt;
    const next = sw.getAttribute("aria-checked") !== "true";
    // the trade switch overrides the map default; switching back to the map's value clears the override
    if (key === "trade") setOption("trade", next === Boolean(data?.ledger_map?.use_trade_vouchers) ? null : next);
    else setOption(key, next);
  }));
  companyInput.addEventListener("change", () => setOption("company", companyInput.value.trim()));
  companyInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      setOption("company", companyInput.value.trim());
    }
  });
  ctx.onCleanup(delegate(el, "click", "[data-action]", (e, b) => {
    const a = b.dataset.action;
    if (a === "tally-download") download(b);
    else if (a === "tally-xlsx") downloadXlsx(b);
    else if (a === "tally-push") openPush();
    else if (a === "tally-map") editMap();
    else if (a === "tally-retry") load();
  }));

  ctx.onData([TOPICS.transactions, TOPICS.suspense, TOPICS.statements, TOPICS.entities, TOPICS.anomalies], () => load({ quiet: true }));
  ctx.registerCommands([
    { id: "tally.download", title: "Download Tally XML", group: "Tally", icon: "download", keywords: ["export", "xml"], run: () => download(null) },
    { id: "tally.xlsx", title: "Download Tally vouchers as Excel", group: "Tally", icon: "table", keywords: ["export", "xlsx"], run: () => downloadXlsx(null) },
    { id: "tally.push", title: "Push vouchers to Tally…", group: "Tally", icon: "upload", keywords: ["sync", "tally prime"], run: () => openPush() },
    { id: "tally.map", title: "Edit Tally ledger map…", group: "Tally", icon: "settings", keywords: ["ledger", "group", "mapping"], run: () => editMap() },
    { id: "tally.suspense", title: "Tally: toggle “Include suspense”", group: "Tally", icon: "filter", run: () => setOption("includeSuspense", !opts.includeSuspense) },
    { id: "tally.trade", title: "Tally: toggle Sales / Purchase vouchers", group: "Tally", icon: "filter", run: () => setOption("trade", !effectiveTrade()) },
  ]);

  await load();

  return {
    update(params) {
      const next = { includeSuspense: isOn(params.suspense), trade: triState(params.trade), company: params.company || "" };
      const changed = next.includeSuspense !== opts.includeSuspense || next.trade !== opts.trade || next.company !== opts.company;
      Object.assign(opts, next);
      el.querySelector('[data-opt="includeSuspense"]')?.setAttribute("aria-checked", opts.includeSuspense ? "true" : "false");
      companyInput.value = opts.company;
      if (data) drawOptions();
      if (tabs && params.tab !== undefined && TABS.includes(params.tab) && tabs.selected !== params.tab) tabs.select(params.tab);
      else if (tabs && !params.tab && tabs.selected !== "vouchers") tabs.select("vouchers");
      if (changed) load({ quiet: true });
    },
  };
}
