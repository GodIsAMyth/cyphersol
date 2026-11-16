/* Suspense workbench (#/case/{id}/suspense?q=<counterparty>).
 *
 * - Loads every pending suspense row (GET transactions?suspense=true&include_ignored=false, paged by 2000) and
 *   groups them by counterparty (extracted party, else VPA/account) with row counts and Cr/Dr totals.
 * - Left: counterparty groups (sortable, searchable). Right: the rows of the selected group with top-3 AI
 *   suggestion chips, multi-select and a bulk bar (52-category picker from /api/meta + custom Tally ledger).
 * - Single-row resolution always previews propagation first (GET transactions/{tx}/resolve-preview): when the
 *   learned rule would touch other suspense rows, a confirm dialog lists them with checkboxes and only the
 *   confirmed ids are sent (confirmed_tx_ids); unticked rows are excluded from the rule by the server.
 * - Bulk assignment resolves the selection anchor by anchor with confirmed_tx_ids limited to the selection, so
 *   nothing outside the selection changes.
 * - After every POST the resolved rows are removed in place (no reload) and ctx.mutated() refreshes badges. */
import { isAbort } from "../core/api.js";
import { lsGet, lsSet } from "../core/constants.js";
import { delegate, html, render } from "../core/dom.js";
import { DASH, catNo, date, inr, inrCompact, int, pct, plural } from "../core/format.js";
import { MUTATION_TOPICS, TOPICS } from "../core/store.js";
import { createDataGrid } from "../components/datagrid.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { openModal } from "../components/modal.js";
import { emptyState, errorState, pageHead, skeleton } from "../components/states.js";
import { toast, withBusy } from "../components/toast.js";
import { amountCell, categoryOptions, loadAllTransactions, openTxDrawer, txRowClass } from "./transactions.js";
import { channelLabel, channelOf } from "./transactions-meta.js";

export const styles = ["views/transactions.css"];

const PREF_PROPAGATE = "cyphersol.suspense.propagate";
const PREF_SCOPE = "cyphersol.suspense.scope";
const ALL = "__all__";
const enc = encodeURIComponent;

function groupKeyOf(t) {
  const p = String(t.extracted_counterparty || "").trim();
  if (p) return { key: `P:${p.toUpperCase()}`, label: p };
  const v = String(t.counterparty_vpa_or_acc || "").trim();
  if (v) return { key: `V:${v.toLowerCase()}`, label: v };
  return { key: "U:", label: "Unidentified counterparty" };
}

function buildGroups(rows) {
  const map = new Map();
  for (const t of rows) {
    const { key, label } = groupKeyOf(t);
    let g = map.get(key);
    if (!g) {
      g = { key, label, count: 0, cr: 0, dr: 0, ids: [], vpas: new Set(), top: new Map() };
      map.set(key, g);
    }
    g.count += 1;
    g.cr += Number(t.credit) || 0;
    g.dr += Number(t.debit) || 0;
    g.ids.push(t.tx_id);
    if (t.counterparty_vpa_or_acc) g.vpas.add(String(t.counterparty_vpa_or_acc));
    const s = (t.top3_suggestions || [])[0];
    if (s) {
      const cur = g.top.get(s.category_id) || { category_id: s.category_id, category_name: s.category_name, n: 0, conf: 0 };
      cur.n += 1;
      cur.conf += Number(s.confidence) || 0;
      g.top.set(s.category_id, cur);
    }
  }
  return Array.from(map.values()).map((g) => ({ ...g, gross: g.cr + g.dr }));
}

const rowMatches = (t, f) => {
  if (f.direction && t.direction !== f.direction) return false;
  const q = String(f.q || "").trim().toLowerCase();
  if (!q) return true;
  return [t.raw_narration, t.extracted_counterparty, t.counterparty_vpa_or_acc, t.utr_number, t.source_holder_name, t.cheque_or_ref_no]
    .some((v) => v && String(v).toLowerCase().includes(q));
};

export async function mount(el, ctx) {
  await ctx.store.ensureCaseData();
  if (!ctx.store.get().meta) await ctx.store.loadMeta();
  if (!ctx.isCurrent()) return;
  const categories = ctx.store.categories();
  const catLabel = (id) => {
    const c = categories.find((x) => Number(x.category_id) === Number(id));
    return c ? `${catNo(c.category_id)} — ${c.name}` : `Category ${id}`;
  };

  const state = {
    rows: [], // pending suspense rows
    byId: new Map(),
    groups: [],
    group: ALL,
    selected: new Set(),
    total: 0,
    truncated: false,
    propagate: lsGet(PREF_PROPAGATE, "true") !== "false",
    scope: lsGet(PREF_SCOPE, "case") === "global" ? "global" : "case",
    busy: false,
  };

  render(
    el,
    html`<div class="stack-lg">
      <div data-slot="head"></div>
      <div data-slot="kpis">${skeleton({ kind: "kpis", rows: 4 })}</div>
      <div data-slot="notice"></div>
      <div class="card"><div class="card__body v-suspense-opts">
        <div class="v-suspense-opt">
          <button type="button" class="switch" role="switch" id="sus-prop" aria-checked="${state.propagate ? "true" : "false"}" data-action="toggle-propagate" aria-describedby="sus-prop-hint"></button>
          <div><label class="strong small" for="sus-prop">Apply to matching rows by default</label>
            <div class="xsmall muted" id="sus-prop-hint">Each assignment learns a counterparty rule. You confirm which matching suspense rows it updates; unticked matches are excluded from the rule.</div></div>
        </div>
        <div class="v-suspense-opt">
          <label class="small strong" for="sus-scope">Rule scope</label>
          <select class="select input--sm input--w-md" id="sus-scope" data-pref="scope">
            <option value="case" ${state.scope === "case" ? html`selected` : ""}>This case only</option>
            <option value="global" ${state.scope === "global" ? html`selected` : ""}>All cases (global rule)</option>
          </select>
        </div>
      </div></div>
      <div data-slot="body">${skeleton({ kind: "table", rows: 8 })}</div>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);

  render(slot("head"), pageHead({
    title: "Suspense workbench",
    sub: "Assign uncategorised rows to the 52-category taxonomy. Rules learned here are applied to future imports.",
    actions: html`<a class="btn btn--sm" href="${ctx.href("transactions", { suspense: "true" })}">${icon("list", { size: "sm" })}Open in Transactions</a>`,
  }));

  // ---------------------------------------------------------------- data
  let loadGen = 0;
  async function load({ keepGroup = true } = {}) {
    const gen = ++loadGen;
    try {
      const res = await loadAllTransactions(ctx, { suspense: "true", include_ignored: "false", sort: "txn_date", order: "asc" });
      if (!ctx.isCurrent() || gen !== loadGen) return;
      state.rows = res.rows;
      state.total = res.total;
      state.truncated = res.truncated;
      state.byId = new Map(res.rows.map((t) => [t.tx_id, t]));
      state.selected.clear();
      regroup();
      if (!keepGroup || (state.group !== ALL && !state.groups.some((g) => g.key === state.group))) state.group = ALL;
      paintAll();
    } catch (err) {
      if (!ctx.isCurrent() || gen !== loadGen) return;
      if (isAbort(err)) return;
      destroyGrids();
      render(slot("kpis"), "");
      render(slot("body"), html`<div class="card"><div class="card__body">${errorState(err, { retryId: "reload", title: "Could not load suspense rows" })}</div></div>`);
    }
  }

  function regroup() {
    state.groups = buildGroups(state.rows);
  }

  const groupRows = () => (state.group === ALL ? state.rows : state.rows.filter((t) => groupKeyOf(t).key === state.group));
  const currentGroup = () => state.groups.find((g) => g.key === state.group) || null;

  // ---------------------------------------------------------------- rendering
  let groupGrid = null;
  let rowGrid = null;

  function paintKpis() {
    const cr = state.rows.reduce((s, t) => s + (Number(t.credit) || 0), 0);
    const dr = state.rows.reduce((s, t) => s + (Number(t.debit) || 0), 0);
    render(slot("kpis"), kpiGrid([
      { label: "Pending rows", value: int(state.total), tone: state.total ? "warn" : "success", icon: "inbox" },
      { label: "Counterparties", value: int(state.groups.length), icon: "users" },
      { label: "Credits in suspense", value: inrCompact(cr), title: inr(cr), icon: "arrow-down-left" },
      { label: "Debits in suspense", value: inrCompact(dr), title: inr(dr), icon: "arrow-up-right" },
    ], { compact: true, label: "Suspense summary" }));
    render(slot("notice"), state.truncated
      ? html`<div class="callout callout--warn">${icon("alert")}<div>Showing the first ${int(state.rows.length)} of ${int(state.total)} suspense rows. Resolve groups to load the rest.</div></div>`
      : "");
  }

  function paintAll() {
    paintKpis();
    if (!state.rows.length) {
      destroyGrids();
      render(slot("body"), html`<div class="card"><div class="card__body">${emptyState({
        icon: "check-circle", tone: "success", title: "Zero suspense entries remaining",
        message: "Every transaction in this case is categorised.",
        action: { label: "Review transactions", href: ctx.href("transactions") },
      })}</div></div>`);
      return;
    }
    if (!groupGrid) buildLayout();
    groupGrid.setRows(state.groups);
    paintGroupHead();
    rowGrid.setRows(groupRows());
    syncSelection();
  }

  function destroyGrids() {
    groupGrid?.destroy();
    rowGrid?.destroy();
    groupGrid = null;
    rowGrid = null;
  }
  ctx.onCleanup(destroyGrids);

  function buildLayout() {
    render(
      slot("body"),
      html`<div class="v-suspense-layout">
        <section class="card v-suspense-groups" aria-labelledby="sus-groups-title">
          <div class="card__head"><div><h2 class="card__title" id="sus-groups-title">Counterparties</h2>
            <p class="card__sub">Grouped by extracted party or VPA / account.</p></div></div>
          <div class="card__body stack-sm">
            <button type="button" class="v-suspense-all" data-action="group-all" aria-pressed="false"></button>
            <div data-slot="groups"></div>
          </div>
        </section>
        <section class="card v-suspense-detail" aria-labelledby="sus-detail-title">
          <div class="card__head v-suspense-dhead" data-slot="ghead"></div>
          <div class="card__body" data-slot="rows"></div>
          <div class="v-suspense-bulk" data-slot="bulk" hidden>
            <div class="v-suspense-bulk__info" aria-live="polite" data-bulk-info></div>
            <label class="sr-only" for="sus-bulk-cat">Category for selected rows</label>
            <select class="select v-suspense-bulk__cat" id="sus-bulk-cat" data-bulk-cat>${categoryOptions(categories)}</select>
            <label class="sr-only" for="sus-bulk-ledger">Custom Tally ledger (optional)</label>
            <input class="input v-suspense-bulk__ledger" id="sus-bulk-ledger" data-bulk-ledger placeholder="Custom Tally ledger (optional)" autocomplete="off" maxlength="120" />
            <button type="button" class="btn btn--primary" data-action="bulk-assign">${icon("check", { size: "sm" })}<span data-bulk-label>Assign</span></button>
            <button type="button" class="btn btn--ghost" data-action="clear-sel">Clear</button>
          </div>
        </section>
      </div>`
    );

    groupGrid = createDataGrid(slot("groups"), {
      ariaLabel: "Suspense counterparties",
      rowKey: "key",
      rows: state.groups,
      height: "var(--v-suspense-gh, min(62vh, 600px))",
      rowHeight: 48,
      countNoun: ["counterparty", "counterparties"],
      initialSort: { key: "gross", order: "desc" },
      filters: [{ key: "q", type: "search", label: "Search counterparties", placeholder: "Find counterparty…" }],
      clientFilter: (g, f) => !f.q || g.label.toLowerCase().includes(String(f.q).toLowerCase()) || Array.from(g.vpas).some((v) => v.toLowerCase().includes(String(f.q).toLowerCase())),
      rowClassName: (g) => (g.key === state.group ? "v-suspense-grow--sel" : ""),
      onRowClick: (g) => selectGroup(g.key, { push: true }),
      emptyState: { icon: "users", title: "No counterparties" },
      columns: [
        {
          key: "label", label: "Counterparty", width: "minmax(120px, 2fr)", sortable: true,
          render: (g) => html`<span class="truncate strong ${g.key === state.group ? "v-suspense-gsel" : ""}" ${g.key === state.group ? html`aria-current="true"` : ""}>${g.label}</span>
            <span class="dg__sub">${g.vpas.size ? Array.from(g.vpas).slice(0, 2).join(", ") : g.key === "U:" ? "No party or VPA extracted" : "Name match"}</span>`,
        },
        { key: "count", label: "Rows", width: "64px", align: "right", sortable: true, defaultOrder: "desc", format: (v) => int(v) },
        {
          key: "gross", label: "Value", width: "108px", align: "right", sortable: true, defaultOrder: "desc",
          title: (g) => `Credits ${inr(g.cr)} · Debits ${inr(g.dr)}`,
          render: (g) => html`${inrCompact(g.gross)}<span class="dg__sub">${g.cr ? html`<span class="is-credit">+${inrCompact(g.cr)}</span>` : ""}${g.cr && g.dr ? " " : ""}${g.dr ? html`<span class="is-debit">−${inrCompact(g.dr)}</span>` : ""}</span>`,
        },
      ],
    });

    rowGrid = createDataGrid(slot("rows"), {
      ariaLabel: "Suspense rows in the selected group",
      rowKey: "tx_id",
      rows: groupRows(),
      height: "min(62vh, 600px)",
      rowHeight: 56,
      countNoun: ["row", "rows"],
      initialSort: { key: "txn_date", order: "asc" },
      filters: [
        { key: "q", type: "search", label: "Search rows", placeholder: "Narration, UTR, holder…" },
        { key: "direction", type: "select", label: "Direction", options: [{ value: "", label: "Dr & Cr" }, { value: "CR", label: "Credits" }, { value: "DR", label: "Debits" }] },
      ],
      clientFilter: rowMatches,
      rowClassName: (t) => [txRowClass(t), state.selected.has(t.tx_id) ? "v-suspense-row--sel" : ""].join(" "),
      onRowClick: (t) => openDetail(t),
      // Space toggles the row's checkbox (keyboard multi-select); Enter opens the row
      onRowToggle: (t) => {
        if (state.selected.has(t.tx_id)) state.selected.delete(t.tx_id);
        else state.selected.add(t.tx_id);
        rowGrid.updateRow(t.tx_id, {});
        syncSelection();
      },
      emptyState: { icon: "check-circle", title: "Group resolved", message: "Pick another counterparty." },
      toolbarExtra: html`<label class="check v-suspense-selall"><input type="checkbox" data-action="sel-all" /> Select all</label>`,
      columns: [
        {
          key: "sel", label: "", width: "40px", align: "center",
          render: (t) => html`<input type="checkbox" class="v-suspense-cb" data-action="sel" ${state.selected.has(t.tx_id) ? html`checked` : ""}
            aria-label="Select ${date(t.txn_date)} ${t.credit > 0 ? `credit ${inr(t.credit)}` : `debit ${inr(t.debit)}`}" />`,
        },
        { key: "txn_date", label: "Date", width: "104px", sortable: true, format: (v) => date(v) },
        {
          key: "raw_narration", label: "Narration · account", width: "minmax(200px, 2fr)", sortable: true,
          render: (t) => html`<span class="v-transactions-narr" title="${t.raw_narration}">${t.raw_narration}</span>
            <span class="dg__sub">${t.source_holder_name || DASH} · ${channelLabel(channelOf(t))}${t.counterparty_vpa_or_acc ? html` · <span class="mono">${t.counterparty_vpa_or_acc}</span>` : ""}</span>`,
        },
        {
          key: "amount", label: "Amount", width: "132px", align: "right", sortable: true, defaultOrder: "desc",
          sortValue: (t) => Math.max(Number(t.credit) || 0, Number(t.debit) || 0), render: amountCell,
        },
        {
          key: "top3_suggestions", label: "AI suggestions — click to assign", width: "minmax(330px, 3fr)",
          render: (t) => {
            const sg = (t.top3_suggestions || []).filter((s) => Number(s.category_id) !== 52).slice(0, 3);
            return sg.length
              ? html`<span class="v-suspense-chips">${sg.map((s) => html`<button type="button" class="chip v-suspense-sugg" data-action="sugg" data-cat="${s.category_id}"
                  title="Assign ${catNo(s.category_id)} — ${s.category_name} (${pct(s.confidence, { decimals: 0 })} confidence)">
                  <span class="truncate">${s.category_name}</span><span class="v-suspense-conf">${pct(s.confidence, { decimals: 0 })}</span></button>`)}</span>`
              : html`<span class="muted xsmall">No suggestion</span>`;
          },
        },
        { key: "act", label: "", width: "92px", align: "right", render: () => html`<button type="button" class="btn btn--sm" data-action="assign">Assign…</button>` },
      ],
    });

    // row-level actions (chips, assign, checkboxes)
    delegate(rowGrid.el, "click", "[data-action]", (e, b) => {
      const a = b.dataset.action;
      if (a === "sugg" || a === "assign") {
        const t = rowGrid.rowFromEvent(e);
        if (!t) return;
        if (a === "sugg") withBusy(b, () => assignFlow(t, { categoryId: Number(b.dataset.cat), quick: true }), { silent: false });
        else withBusy(b, () => assignFlow(t, { categoryId: (t.top3_suggestions || []).find((s) => Number(s.category_id) !== 52)?.category_id ?? null }));
      }
    });
    delegate(rowGrid.el, "change", "input[data-action]", (e, inp) => {
      if (inp.dataset.action === "sel") {
        const t = rowGrid.rowFromEvent(e);
        if (!t) return;
        if (inp.checked) state.selected.add(t.tx_id);
        else state.selected.delete(t.tx_id);
        rowGrid.updateRow(t.tx_id, {});
        syncSelection();
      } else if (inp.dataset.action === "sel-all") {
        const vis = rowGrid.getVisibleRows();
        if (inp.checked) vis.forEach((t) => state.selected.add(t.tx_id));
        else vis.forEach((t) => state.selected.delete(t.tx_id));
        rowGrid.setRows(groupRows());
        syncSelection();
      }
    });
  }

  function paintGroupHead() {
    const all = el.querySelector('[data-action="group-all"]');
    if (all) {
      all.setAttribute("aria-pressed", state.group === ALL ? "true" : "false");
      render(all, html`${icon("inbox", { size: "sm" })}<span class="strong">All pending rows</span><span class="spacer"></span><span class="num">${int(state.rows.length)}</span>`);
    }
    const g = currentGroup();
    const rows = groupRows();
    const cr = rows.reduce((s, t) => s + (Number(t.credit) || 0), 0);
    const dr = rows.reduce((s, t) => s + (Number(t.debit) || 0), 0);
    const tops = g ? Array.from(g.top.values()).filter((x) => Number(x.category_id) !== 52).sort((a, b) => b.n - a.n || b.conf - a.conf).slice(0, 3) : [];
    render(
      slot("ghead"),
      html`<div class="min-w-0 stack-sm">
          <h2 class="card__title truncate" id="sus-detail-title">${g ? g.label : "All pending rows"}</h2>
          <p class="card__sub">${plural(rows.length, "row")} ·
            ${cr ? html`<span class="is-credit" title="${inr(cr)}">+${inrCompact(cr)}</span>` : ""}${cr && dr ? " · " : ""}${dr ? html`<span class="is-debit" title="${inr(dr)}">−${inrCompact(dr)}</span>` : ""}
            ${g && g.vpas.size ? html` · <span class="mono">${Array.from(g.vpas).slice(0, 3).join(", ")}${g.vpas.size > 3 ? "…" : ""}</span>` : ""}</p>
          ${tops.length && rows.length > 1
            ? html`<div class="v-suspense-gsugg" role="group" aria-label="Assign the whole group">
                <span class="xsmall muted">Assign all ${int(rows.length)} to:</span>
                ${tops.map((x) => html`<button type="button" class="chip" data-action="group-sugg" data-cat="${x.category_id}" title="Top suggestion for ${x.n} of ${rows.length} rows">
                  ${catNo(x.category_id)} · ${x.category_name} <span class="muted">(${x.n}/${rows.length})</span></button>`)}
              </div>`
            : ""}
        </div>`
    );
  }

  function syncSelection() {
    const bulk = slot("bulk");
    if (!bulk) return;
    // drop selections that no longer exist
    for (const id of state.selected) if (!state.byId.has(id)) state.selected.delete(id);
    const sel = Array.from(state.selected).map((id) => state.byId.get(id)).filter(Boolean);
    bulk.hidden = sel.length === 0;
    const cr = sel.reduce((s, t) => s + (Number(t.credit) || 0), 0);
    const dr = sel.reduce((s, t) => s + (Number(t.debit) || 0), 0);
    render(bulk.querySelector("[data-bulk-info]"), html`<span class="strong">${plural(sel.length, "row")} selected</span>
      <span class="small">${cr ? html`<span class="is-credit">+${inr(cr)}</span>` : ""}${cr && dr ? " · " : ""}${dr ? html`<span class="is-debit">−${inr(dr)}</span>` : ""}</span>`);
    bulk.querySelector("[data-bulk-label]").textContent = `Assign ${plural(sel.length, "row")}`;
    const all = rowGrid?.el.querySelector('input[data-action="sel-all"]');
    if (all) {
      const vis = rowGrid.getVisibleRows();
      const n = vis.filter((t) => state.selected.has(t.tx_id)).length;
      all.checked = n > 0 && n === vis.length;
      all.indeterminate = n > 0 && n < vis.length;
    }
  }

  function selectGroup(key, { push = false } = {}) {
    if (!state.groups.some((g) => g.key === key)) key = ALL;
    if (key === state.group && rowGrid) return;
    state.group = key;
    state.selected.clear();
    if (!rowGrid) return;
    groupGrid.setRows(state.groups); // repaint selection marker
    rowGrid.setRows(groupRows());
    paintGroupHead();
    syncSelection();
    const g = currentGroup();
    ctx.setParams({ q: g ? g.label : null }, { push });
    ctx.setCrumbs(g ? [{ label: g.label }] : []);
  }

  function selectGroupFromParam(q) {
    if (!q) return selectGroup(ALL);
    const u = String(q).trim().toUpperCase();
    const g = state.groups.find((x) => x.label.toUpperCase() === u || x.key === `P:${u}` || x.key === `V:${String(q).toLowerCase()}`)
      || state.groups.find((x) => x.label.toUpperCase().includes(u));
    if (g) selectGroup(g.key);
    else if (groupGrid) {
      selectGroup(ALL);
      groupGrid.setFilters({ q }, { silent: true });
    }
  }

  // ---------------------------------------------------------------- mutations
  let selfDepth = 0;
  async function selfMutated() {
    selfDepth += 1;
    try {
      await ctx.mutated(MUTATION_TOPICS.resolveSuspense);
    } finally {
      selfDepth -= 1;
    }
  }

  /** Remove resolved rows in place and repaint (no refetch). */
  function applyResolved(ids) {
    const gone = new Set(ids);
    if (!gone.size) return;
    state.rows = state.rows.filter((t) => !gone.has(t.tx_id));
    gone.forEach((id) => {
      state.byId.delete(id);
      state.selected.delete(id);
    });
    state.total = Math.max(0, state.total - gone.size);
    regroup();
    if (state.group !== ALL && !state.groups.some((g) => g.key === state.group)) {
      // group finished: move to the next largest group so the analyst keeps flowing
      const next = state.groups.slice().sort((a, b) => b.gross - a.gross)[0];
      state.group = next ? next.key : ALL;
      const g = currentGroup();
      ctx.setParams({ q: g ? g.label : null });
      ctx.setCrumbs(g ? [{ label: g.label }] : []);
    }
    paintAll();
  }

  const resolveOne = (anchorId, body) =>
    ctx.api.post(`transactions/${enc(anchorId)}/resolve`, body, { signal: undefined });

  function successToast(n, categoryId, learned, scope) {
    toast.success(
      html`${plural(n, "row")} assigned to <strong>${catLabel(categoryId)}</strong>.${learned ? html` Learned ${scope === "global" ? "global" : "case"} rule <span class="mono">${learned}</span>.` : ""}`,
      { title: "Suspense resolved" }
    );
  }

  /**
   * Resolve one anchor row. Always previews propagation; if the learned rule would touch other suspense rows (or
   * needs a single-word-party opt-in) a confirm dialog lists them. quick=true skips the dialog when nothing else
   * would change.
   */
  async function assignFlow(anchor, { categoryId = null, quick = false } = {}) {
    const preview = await ctx.api.get(`transactions/${enc(anchor.tx_id)}/resolve-preview`, {}, { signal: undefined });
    if (!ctx.isCurrent()) return;
    const key = preview.rule_key || "";
    const singleToken = key.startsWith("party:") && !key.slice(6).trim().includes(" ");
    const matches = (preview.transactions || []).filter((t) => t.tx_id !== anchor.tx_id);
    if (quick && categoryId && preview.count === 0 && !singleToken) {
      const res = await resolveOne(anchor.tx_id, { category_id: categoryId, custom_ledger_name: null, auto_refresh_similar: true, confirmed_tx_ids: [], rule_scope: state.scope });
      if (!ctx.isCurrent()) return;
      applyResolved(res.updated_tx_ids && res.updated_tx_ids.length ? res.updated_tx_ids : [anchor.tx_id]);
      successToast(res.updated_count || 1, categoryId, res.learned_pattern, res.rule_scope);
      await selfMutated();
      return;
    }

    const checkedByDefault = state.propagate;
    const m = openModal({
      title: "Assign category",
      description: "Review which suspense rows this decision updates before it is applied.",
      size: "lg",
      className: "v-transactions-modal",
      body: html`<div class="stack">
        <div class="v-suspense-anchor">
          <div class="min-w-0"><div class="strong truncate" title="${anchor.raw_narration}">${anchor.raw_narration}</div>
            <div class="xsmall muted">${date(anchor.txn_date)} · ${anchor.source_holder_name || DASH} · ${channelLabel(channelOf(anchor))}</div></div>
          <div class="num strong">${amountCell(anchor)}</div>
        </div>
        <div class="form-grid">
          <div class="field">
            <label class="field__label" for="sus-m-cat">Category</label>
            <select class="select" id="sus-m-cat" name="category_id" required>${categoryOptions(categories, { selected: categoryId })}</select>
          </div>
          <div class="field">
            <label class="field__label" for="sus-m-ledger">Custom Tally ledger <span class="muted">(optional)</span></label>
            <input class="input" id="sus-m-ledger" name="custom_ledger_name" maxlength="120" autocomplete="off" placeholder="Defaults to the counterparty name" />
          </div>
          <div class="field">
            <label class="field__label" for="sus-m-scope">Rule scope</label>
            <select class="select" id="sus-m-scope" name="rule_scope">
              <option value="case" ${state.scope === "case" ? html`selected` : ""}>This case only</option>
              <option value="global" ${state.scope === "global" ? html`selected` : ""}>All cases (global)</option>
            </select>
          </div>
        </div>
        ${key
          ? html`<div class="callout callout--info">${icon("zap")}<div class="small">Learned rule key <span class="mono strong">${key}</span> (${anchor.direction === "CR" ? "credits" : "debits"}).
              ${matches.length ? html`It also matches <strong>${plural(matches.length, "other suspense row")}</strong>. Untick rows that should stay in suspense — they are excluded from the rule.` : "No other pending rows match it."}</div></div>`
          : html`<div class="callout callout--warn">${icon("info")}<div class="small">No counterparty, VPA or account could be extracted, so no rule is learned. Only this row is updated.</div></div>`}
        ${singleToken
          ? html`<label class="check"><input type="checkbox" name="learn_single_token_party" /> Learn the single-word party rule <span class="mono">${key}</span> (may over-match common names)</label>`
          : ""}
        ${matches.length
          ? html`<div class="stack-sm">
              <div class="row-between"><label class="check"><input type="checkbox" data-m-all ${checkedByDefault ? html`checked` : ""} /> <span class="strong small">Also update matching rows</span></label>
                <span class="xsmall muted" data-m-count></span></div>
              <div class="table-wrap v-suspense-mtable"><table class="table table--compact">
                <caption class="sr-only">Suspense rows matched by the learned rule</caption>
                <thead><tr><th scope="col"><span class="sr-only">Include</span></th><th scope="col">Date</th><th scope="col">Narration</th><th scope="col">Account</th><th scope="col" class="num">Amount</th></tr></thead>
                <tbody>${matches.map((t) => html`<tr>
                  <td><input type="checkbox" data-m-row="${t.tx_id}" ${checkedByDefault ? html`checked` : ""} aria-label="Include ${date(t.txn_date)} ${t.raw_narration}" /></td>
                  <td class="nowrap">${date(t.txn_date)}</td><td class="v-suspense-mnarr">${t.raw_narration}</td>
                  <td class="small">${t.source_holder_name || DASH}</td><td class="num nowrap">${amountCell(t)}</td></tr>`)}</tbody>
              </table></div>
              ${preview.count > matches.length ? html`<p class="xsmall muted">Showing ${int(matches.length)} of ${int(preview.count)} matches.</p>` : ""}
            </div>`
          : ""}
      </div>`,
      actions: [{ label: "Cancel", value: "cancel" }, { spacer: true }, { label: "Assign", variant: "primary", type: "submit", id: "sus-m-submit" }],
      initialFocus: categoryId ? "#sus-m-submit" : "#sus-m-cat",
      onOpen: (dlg) => {
        const sync = () => {
          const boxes = Array.from(dlg.querySelectorAll("[data-m-row]"));
          const n = boxes.filter((b) => b.checked).length;
          const all = dlg.querySelector("[data-m-all]");
          if (all) {
            all.checked = n > 0 && n === boxes.length;
            all.indeterminate = n > 0 && n < boxes.length;
          }
          const c = dlg.querySelector("[data-m-count]");
          if (c) c.textContent = `${int(n)} of ${int(boxes.length)} selected`;
          const sub = dlg.querySelector("#sus-m-submit");
          if (sub) sub.textContent = `Assign ${plural(n + 1, "row")}`;
        };
        dlg.addEventListener("change", (e) => {
          if (e.target.matches("[data-m-all]")) dlg.querySelectorAll("[data-m-row]").forEach((b) => (b.checked = e.target.checked));
          sync();
        });
        sync();
      },
      onSubmit: async (values, { el: dlg }) => {
        const cat = Number(values.category_id);
        if (!cat) return false;
        const confirmed = Array.from(dlg.querySelectorAll("[data-m-row]")).filter((b) => b.checked).map((b) => b.dataset.mRow);
        const res = await resolveOne(anchor.tx_id, {
          category_id: cat,
          custom_ledger_name: String(values.custom_ledger_name || "").trim() || null,
          auto_refresh_similar: true,
          confirmed_tx_ids: confirmed,
          rule_scope: values.rule_scope === "global" ? "global" : "case",
          learn_single_token_party: Boolean(values.learn_single_token_party),
        });
        return { res, cat };
      },
    });
    const out = await m.closed;
    if (!out || !out.res || !ctx.isCurrent()) return;
    applyResolved(out.res.updated_tx_ids && out.res.updated_tx_ids.length ? out.res.updated_tx_ids : [anchor.tx_id]);
    successToast(out.res.updated_count || 1, out.cat, out.res.learned_pattern, out.res.rule_scope);
    await selfMutated();
  }

  /** Assign every selected row (only the selection changes; matching rows outside it are excluded from rules). */
  async function bulkAssign(categoryId, ledger) {
    const order = state.rows.filter((t) => state.selected.has(t.tx_id)).map((t) => t.tx_id);
    if (!order.length) return;
    if (!categoryId) {
      toast.warn("Choose a category for the selected rows first.");
      el.querySelector("[data-bulk-cat]")?.focus();
      return;
    }
    const sel = order.map((id) => state.byId.get(id));
    const cr = sel.reduce((s, t) => s + (Number(t.credit) || 0), 0);
    const dr = sel.reduce((s, t) => s + (Number(t.debit) || 0), 0);
    const done = new Set();
    const learned = new Set();
    const m = openModal({
      title: `Assign ${plural(order.length, "row")}?`,
      size: "md",
      className: "v-transactions-modal",
      body: html`<div class="stack-sm">
        <dl class="kv">
          <dt>Category</dt><dd class="strong">${catLabel(categoryId)}</dd>
          <dt>Tally ledger</dt><dd>${ledger || html`<span class="muted">Counterparty name (default)</span>`}</dd>
          <dt>Rows</dt><dd>${int(order.length)} · ${cr ? html`<span class="is-credit">+${inr(cr)}</span>` : ""}${cr && dr ? " · " : ""}${dr ? html`<span class="is-debit">−${inr(dr)}</span>` : ""}</dd>
          <dt>Rule scope</dt><dd>${state.scope === "global" ? "All cases (global)" : "This case only"}</dd>
        </dl>
        <p class="small text-2">A counterparty rule is learned from each distinct party/VPA in the selection. Only the selected rows change;
          other suspense rows matching those rules stay in suspense and are excluded from the rules.</p>
        <div class="small" data-m-progress aria-live="polite"></div>
      </div>`,
      actions: [{ label: "Cancel", value: "cancel" }, { spacer: true }, { label: `Assign ${plural(order.length, "row")}`, variant: "primary", type: "submit" }],
      onSubmit: async (_v, { el: dlg }) => {
        const prog = dlg.querySelector("[data-m-progress]");
        for (const id of order) {
          if (done.has(id)) continue;
          const res = await resolveOne(id, {
            category_id: categoryId,
            custom_ledger_name: ledger || null,
            auto_refresh_similar: true,
            confirmed_tx_ids: order.filter((x) => x !== id && !done.has(x)),
            rule_scope: state.scope,
          });
          done.add(id);
          (res.updated_tx_ids || []).forEach((x) => done.add(x));
          if (res.learned_pattern) learned.add(res.learned_pattern);
          prog.textContent = `Assigned ${int(Math.min(done.size, order.length))} of ${int(order.length)}…`;
        }
        return "done";
      },
    });
    await m.closed;
    if (!ctx.isCurrent()) return;
    if (done.size) {
      applyResolved(Array.from(done));
      const n = order.filter((id) => done.has(id)).length;
      if (n < order.length) toast.warn(`${plural(n, "row")} of ${int(order.length)} assigned before the dialog was closed.`);
      successToast(n, categoryId, learned.size === 1 ? Array.from(learned)[0] : learned.size > 1 ? `${learned.size} rules` : null, state.scope);
      const ledgerEl = el.querySelector("[data-bulk-ledger]");
      if (ledgerEl) ledgerEl.value = "";
      await selfMutated();
    }
  }

  // ---------------------------------------------------------------- detail drawer
  let drawer = null;
  function openDetail(t) {
    drawer = openTxDrawer(t, ctx, {
      onAssign: async (catId, tx, dw) => {
        dw.close();
        try {
          await assignFlow(tx, { categoryId: catId, quick: true });
        } catch (err) {
          toast.error(err);
        }
      },
      actions: [{ id: "assign", label: "Assign…", variant: "primary", icon: "tag", run: async (tx, dw) => {
        dw.close();
        await assignFlow(tx, { categoryId: (tx.top3_suggestions || []).find((s) => Number(s.category_id) !== 52)?.category_id ?? null });
      } }],
    });
  }
  ctx.onCleanup(() => drawer && drawer.el.isConnected && drawer.close());

  // ---------------------------------------------------------------- page-level events
  ctx.onCleanup(
    delegate(el, "click", "[data-action]", (e, b) => {
      const a = b.dataset.action;
      if (a === "toggle-propagate") {
        state.propagate = b.getAttribute("aria-checked") !== "true";
        b.setAttribute("aria-checked", state.propagate ? "true" : "false");
        lsSet(PREF_PROPAGATE, state.propagate ? "true" : "false");
      } else if (a === "group-all") selectGroup(ALL, { push: true });
      else if (a === "clear-sel") {
        state.selected.clear();
        rowGrid?.setRows(groupRows());
        syncSelection();
      } else if (a === "bulk-assign") {
        const cat = Number(el.querySelector("[data-bulk-cat]")?.value || 0);
        const ledger = String(el.querySelector("[data-bulk-ledger]")?.value || "").trim();
        withBusy(b, () => bulkAssign(cat, ledger));
      } else if (a === "group-sugg") {
        const cat = Number(b.dataset.cat);
        groupRows().forEach((t) => state.selected.add(t.tx_id));
        rowGrid?.setRows(groupRows());
        syncSelection();
        const sel = el.querySelector("[data-bulk-cat]");
        if (sel) sel.value = String(cat);
        withBusy(b, () => bulkAssign(cat, String(el.querySelector("[data-bulk-ledger]")?.value || "").trim()));
      } else if (a === "reload") {
        destroyGrids();
        render(slot("body"), skeleton({ kind: "table", rows: 8 }));
        load();
      }
    })
  );
  ctx.onCleanup(
    delegate(el, "change", "[data-pref]", (_e, s) => {
      if (s.dataset.pref === "scope") {
        state.scope = s.value === "global" ? "global" : "case";
        lsSet(PREF_SCOPE, state.scope);
      }
    })
  );

  ctx.onData([TOPICS.suspense, TOPICS.transactions, TOPICS.entities], () => {
    if (selfDepth > 0 || !ctx.isCurrent()) return;
    load({ keepGroup: true });
  });

  ctx.registerCommands([
    { id: "suspense.all", title: "Show all pending suspense rows", group: "Suspense", icon: "inbox", run: () => selectGroup(ALL, { push: true }) },
    {
      id: "suspense.next", title: "Next counterparty group", group: "Suspense", icon: "arrow-right",
      run: () => {
        const gs = state.groups.slice().sort((a, b) => b.gross - a.gross);
        const i = gs.findIndex((g) => g.key === state.group);
        if (gs.length) selectGroup(gs[(i + 1) % gs.length].key, { push: true });
      },
    },
    {
      id: "suspense.select-all", title: "Select all rows in this group", group: "Suspense", icon: "check",
      run: () => {
        groupRows().forEach((t) => state.selected.add(t.tx_id));
        rowGrid?.setRows(groupRows());
        syncSelection();
      },
    },
    { id: "suspense.refresh", title: "Reload suspense rows", group: "Suspense", icon: "refresh", run: () => load({ keepGroup: true }) },
  ]);

  await load({ keepGroup: false });
  if (!ctx.isCurrent()) return;
  if (ctx.params.q) selectGroupFromParam(ctx.params.q);

  return {
    update(params) {
      const g = currentGroup();
      const want = params.q || "";
      if ((g ? g.label : "") !== want) selectGroupFromParam(want);
    },
  };
}
