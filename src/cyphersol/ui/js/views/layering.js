/* Layering tree (#/case/{id}/layering?source=&root_tx=&mode=&window_days=&max_depth=&trace_depth=&date_from=&date_to=&tab=)
 *
 * P1 (TF-03/05/06): the first fetch builds max_depth hops; any node with has_more_children and no children is
 * loaded on demand from GET trace/layering/expand?path=<path_key> (tree "+ more" toggle, → key, or the inspector's
 * "Load next hop"). root_tx roots the tree at one credit received by the source (picked from that source's credits).
 * Misc is per branch: POST trace/layering/misc {path, source_entity_id, root_tx_id} parks one path for this source
 * (source_key), {entity_id} still parks an entity case-wide; Misc nodes list misc_members with their scope.
 *
 * Multi-hop fund trail from a credit source (TF-01..TF-08). Tidy tree with elbow connectors (charts/tree.js),
 * node cards with attributed vs debit value, role styling (source / has statement / details only / misc / system),
 * collapse/expand, FIFO vs All-outflows toggle, holding window + depth controls, source picker, Move to Misc /
 * Restore (POST trace/layering/misc), truncation banner, SVG + CSV export, inspector panel and an outline table
 * as the text alternative. */
import { LAYER_ROLES } from "../core/constants.js";
import { announce, delegate, html, render } from "../core/dom.js";
import { date, inr, inrCompact, int, pct, plural, truncate } from "../core/format.js";
import { isAbort, linkAbort } from "../core/api.js";
import { MUTATION_TOPICS, TOPICS } from "../core/store.js";
import { createDataGrid } from "../components/datagrid.js";
import { createMenu } from "../components/menu.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { emptyState, errorState, pageHead, skeleton } from "../components/states.js";
import { createTreeChart } from "../charts/tree.js";
import { csvCell, downloadText } from "../core/files.js";

export const styles = ["views/flow.css"];

const DEFAULTS = { mode: "fifo", window_days: 14, max_depth: 4, trace_depth: 8 };
const MAX_REEXPAND = 40; // lazily loaded branches re-fetched after a reload
const CREDIT_LIMIT = 300;
const NODE_W = 236;
const NODE_H = 78;
const MAX_INITIAL_VISIBLE = 150;
const MODES = [
  { value: "fifo", label: "FIFO attributed", hint: "Each outflow is matched oldest-lot-first; children never exceed the parent." },
  { value: "all", label: "All outflows", hint: "Every outflow inside an open window at full debit value (may exceed the parent)." },
];

const isoOk = (v) => (/^\d{4}-\d{2}-\d{2}$/.test(String(v || "")) ? String(v) : "");
const clampInt = (v, lo, hi, dflt) => {
  const n = Number.parseInt(v, 10);
  return Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : dflt;
};

function readParams(p) {
  return {
    source: p.source || "",
    root_tx: p.root_tx || "",
    mode: p.mode === "all" ? "all" : "fifo",
    window_days: clampInt(p.window_days, 1, 366, DEFAULTS.window_days),
    max_depth: clampInt(p.max_depth, 1, 8, DEFAULTS.max_depth),
    trace_depth: clampInt(p.trace_depth, 1, 32, DEFAULTS.trace_depth),
    date_from: isoOk(p.date_from),
    date_to: isoOk(p.date_to),
    tab: p.tab === "table" ? "table" : "tree",
  };
}

const roleCls = (n) => (n.is_system ? "system" : LAYER_ROLES[n.role]?.cls || "details");
const roleLabel = (n) => (n.is_system ? "System / cash" : LAYER_ROLES[n.role]?.label || "Details only");
const ratio = (a, b) => (Number(b) > 0 ? Number(a) / Number(b) : null);
/** Tree key = the server's path_key ("id>id>…", Misc nodes end in ">MISC"), so keys survive refetches. */
const keyOf = (n, pk) => n.path_key || (pk ? `${pk}>${n.role === "MISC" ? "MISC" : n.entity_id}` : String(n.entity_id));
/** A node the server can extend: it reports more children but sent none (lazy drill-down). */
const isLazy = (n) => Boolean(n && n.role !== "MISC" && n.has_more_children && !(n.children || []).length);

export async function mount(el, ctx) {
  let p = readParams(ctx.params);
  let data = null;
  let index = new Map(); // key -> { key, node, depth, parentKey, path:[{key,name}] }
  let flat = [];
  let rootAmt = 0;
  // Base for "share of source": the root's total outflow, or (rooted at one credit) the outflow traced from it
  let shareBase = 0;
  const rootedAtCredit = () => Boolean(data?.root_credit || p.root_tx);
  let loadCtl = null;
  let loadSeq = 0;
  let grid = null;
  let chart = null;
  let lastAnnounced = null; // inspector selection last announced to screen readers
  const lazyLoaded = new Set(); // path_keys loaded through trace/layering/expand (re-expanded after a reload)
  let credits = { source: null, rows: [], loading: false, error: null };
  let creditsCtl = null;

  render(
    el,
    html`<div class="stack">
      ${pageHead({
        title: "Layering tree",
        sub: "Multi-hop fund trail from a credit source. Each inflow opens a credit lot for the holding window; onward debits inside it are attributed to the next layer.",
        actions: html`<button type="button" class="btn" data-slot="misc-btn" aria-haspopup="menu">${icon("layers", { size: "sm" })}<span data-slot="misc-label">Misc</span></button>
          <button type="button" class="btn" data-slot="export-btn" aria-haspopup="menu">${icon("download", { size: "sm" })}Export</button>`,
      })}
      <section class="card v-layering-controls" aria-label="Trace parameters">
        <div class="card__body">
          <form class="v-layering-form" data-slot="form" novalidate>
            <div class="field v-layering-field-source">
              <label class="field__label" for="lay-source">Credit source</label>
              <select class="select" id="lay-source" name="source" data-slot="source"><option value="">Loading…</option></select>
            </div>
            <div class="field v-layering-field-mode">
              <span class="field__label" id="lay-mode-label">Attribution</span>
              <div class="seg" role="radiogroup" aria-labelledby="lay-mode-label" data-slot="mode">
                ${MODES.map((m) => html`<button type="button" class="seg__btn" role="radio" data-mode="${m.value}" title="${m.hint}"
                  aria-checked="${p.mode === m.value ? "true" : "false"}" tabindex="${p.mode === m.value ? "0" : "-1"}">${m.label}</button>`)}
              </div>
            </div>
            <div class="field v-layering-field-root">
              <label class="field__label" for="lay-root">Root at</label>
              <select class="select" id="lay-root" name="root_tx" data-slot="root" aria-describedby="lay-root-hint"><option value="">All credits received</option></select>
              <span class="field__hint" id="lay-root-hint" data-slot="root-hint">Or pick one credit to trace only that money</span>
            </div>
            <div class="field">
              <label class="field__label" for="lay-window">Holding window (days)</label>
              <input class="input input--num input--w-sm" id="lay-window" name="window_days" type="number" min="1" max="366" step="1" inputmode="numeric" value="${p.window_days}">
            </div>
            <div class="field">
              <label class="field__label" for="lay-depth">Max depth (hops)</label>
              <select class="select input--w-sm" id="lay-depth" name="max_depth">
                ${[1, 2, 3, 4, 5, 6, 7, 8].map((d) => html`<option value="${d}" ${d === p.max_depth ? html`selected` : ""}>${d}</option>`)}
              </select>
            </div>
            <div class="field">
              <label class="field__label" for="lay-tdepth">Attribution depth</label>
              <input class="input input--num input--w-sm" id="lay-tdepth" name="trace_depth" type="number" min="1" max="32" step="1" inputmode="numeric" value="${p.trace_depth}"
                title="How many hops FIFO attribution follows (at least the max depth). Deeper hops load on demand.">
            </div>
            <div class="field">
              <label class="field__label" for="lay-from">From</label>
              <input class="input" id="lay-from" name="date_from" type="date" value="${p.date_from}">
            </div>
            <div class="field">
              <label class="field__label" for="lay-to">To</label>
              <input class="input" id="lay-to" name="date_to" type="date" value="${p.date_to}">
            </div>
          </form>
        </div>
      </section>
      <div data-slot="notices" class="stack-sm"></div>
      <div data-slot="kpis">${skeleton({ kind: "kpis", rows: 6 })}</div>
      <div class="v-layering-main">
        <section class="card v-layering-treecard" aria-labelledby="lay-tree-title">
          <div class="card__head">
            <div class="min-w-0"><h2 class="card__title" id="lay-tree-title">Fund trail</h2>
              <div class="legend v-layering-legend" data-slot="legend"></div></div>
            <div class="row-wrap">
              <div class="seg" role="tablist" aria-label="Presentation" data-slot="tabs">
                <button type="button" class="seg__btn" role="tab" data-tab="tree" aria-selected="${p.tab === "tree" ? "true" : "false"}" aria-controls="lay-tree-panel">Tree</button>
                <button type="button" class="seg__btn" role="tab" data-tab="table" aria-selected="${p.tab === "table" ? "true" : "false"}" aria-controls="lay-table-panel">Table</button>
              </div>
              <div class="btn-group v-layering-zoom" data-slot="zoom" role="group" aria-label="Zoom">
                <button type="button" class="btn btn--sm btn--icon" data-action="zoom-out" aria-label="Zoom out" title="Zoom out (−)">${icon("minus", { size: "sm" })}</button>
                <button type="button" class="btn btn--sm v-layering-zoom-val" data-action="zoom-reset" title="Reset zoom (0)" aria-label="Reset zoom"><span data-slot="zoom-val">100%</span></button>
                <button type="button" class="btn btn--sm btn--icon" data-action="zoom-in" aria-label="Zoom in" title="Zoom in (+)">${icon("plus", { size: "sm" })}</button>
                <button type="button" class="btn btn--sm" data-action="fit" title="Fit tree to frame">${icon("maximize", { size: "sm" })}Fit</button>
              </div>
              <button type="button" class="btn btn--sm" data-slot="expand-btn" aria-haspopup="menu">${icon("chevrons-up-down", { size: "sm" })}Expand</button>
            </div>
          </div>
          <div class="card__body card__body--flush v-layering-body" data-slot="body">
            <div class="v-layering-busy" data-slot="busy" hidden><span class="spinner" role="status" aria-label="Recomputing tree"></span></div>
            <div id="lay-tree-panel" role="tabpanel" aria-labelledby="lay-tree-title" data-slot="tree-panel">
              <div data-slot="tree-msg">${skeleton({ kind: "block" })}</div>
              <div class="v-layering-tree-host" data-slot="tree"></div>
              <p class="v-layering-hint xsmall muted">Drag to pan · Ctrl/⌘ + wheel to zoom · ↑↓ move · → expand · ← collapse · Enter select</p>
            </div>
            <div id="lay-table-panel" role="tabpanel" aria-label="Fund trail as a table" data-slot="table-panel" hidden></div>
          </div>
        </section>
        <aside class="card v-layering-insp" aria-labelledby="lay-insp-title" data-slot="insp-card">
          <div class="card__head"><h2 class="card__title" id="lay-insp-title">Selected node</h2></div>
          <div class="card__body" data-slot="insp">${skeleton({ kind: "text", rows: 6 })}</div>
        </aside>
      </div>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);
  const form = slot("form");

  // ------------------------------------------------------------------------------------------ helpers
  const nameOf = (id) => {
    const c = (data?.source_candidates || []).find((x) => x.entity_id === id);
    if (c) return c.name;
    const e = ctx.store.entity(id);
    if (e) return e.canonical_name;
    for (const r of flat) if (r.node.entity_id === id) return r.node.entity_name;
    return id;
  };

  function indexTree(root) {
    index = new Map();
    flat = [];
    if (!root) return;
    (function walk(n, pk, depth, path) {
      const key = keyOf(n, pk);
      const myPath = [...path, { key, name: n.entity_name }];
      const row = { key, node: n, depth, parentKey: pk, path: myPath, name: n.entity_name };
      index.set(key, row);
      flat.push(row);
      (n.children || []).forEach((c) => walk(c, key, depth + 1, myPath));
    })(root, null, 0, []);
  }

  /** After a refetch the previously selected node may be gone (e.g. moved to Misc): prefer the parent's Misc
   * node, then the closest surviving ancestor. */
  function nearestKey(key) {
    if (index.has(key)) return key;
    const parts = String(key).split(">");
    while (parts.length > 1) {
      parts.pop();
      const parent = parts.join(">");
      const miscKey = `${parent}>MISC`;
      if (index.has(miscKey)) return miscKey;
      if (index.has(parent)) return parent;
    }
    return null;
  }

  function initialDepthLimit() {
    const perDepth = [];
    for (const r of flat) perDepth[r.depth] = (perDepth[r.depth] || 0) + 1;
    let total = 0;
    for (let d = 0; d < perDepth.length; d += 1) {
      total += perDepth[d] || 0;
      if (total > MAX_INITIAL_VISIBLE) return Math.max(1, d - 1);
    }
    return Infinity;
  }
  let depthLimit = Infinity;

  // ------------------------------------------------------------------------------------------ tree
  const zoomVal = slot("zoom-val");
  chart = createTreeChart(slot("tree"), {
    ariaLabel: "Layering tree",
    nodeW: NODE_W,
    nodeH: NODE_H,
    levelGap: 64,
    siblingGap: 12,
    keyOf,
    childrenOf: (n) => n.children || [],
    initialCollapsed: (n, depth) => depth >= depthLimit,
    canLoad: isLazy,
    onLoad: (n, key) => expandNode(n, key),
    nodeClass: (n) => `v-layering-node--${roleCls(n)}`,
    edgeClass: (n) => (n.role === "MISC" ? "is-misc" : ""),
    edgeWidth: (n) => 1.25 + 5 * Math.sqrt(Math.max(0, Math.min(1, ratio(n.attributed_amount ?? n.amount, shareBase) || 0))),
    nodeLabel: (n, inf) => {
      const attr = n.attributed_amount ?? n.amount;
      return `${n.entity_name}, ${roleLabel(n)}, hop ${inf.depth}, attributed ${inr(attr)} of ${inr(n.debit_amount ?? attr)} debit` +
        (inf.childCount ? `, ${plural(inf.childCount, "onward flow")}` : "") +
        (inf.lazy ? (inf.loading ? ", loading the next hop" : ", more hops available, press right arrow to load") : "");
    },
    renderNode: (n, inf) => {
      const cls = roleCls(n);
      const attr = n.attributed_amount ?? n.amount;
      const debit = n.debit_amount ?? attr;
      const r = Math.max(0, Math.min(1, ratio(attr, debit) ?? 1));
      const mw = NODE_W - 28;
      const sub = inf.depth === 0
        ? rootedAtCredit()
          ? `credit received · ${inrCompact(shareBase)} traced on`
          : `total outflow · ${plural((n.tx_ids || []).length, "txn")}`
        : Math.abs(attr - debit) < 0.01
          ? `of ${inrCompact(debit)} debit (100%)`
          : `of ${inrCompact(debit)} debit (${pct(r, { decimals: 0 })})`;
      return html`<title>${n.entity_name}</title>
        <rect class="v-layering-card v-layering-card--${cls}" width="${NODE_W}" height="${NODE_H}" rx="8"></rect>
        <rect class="v-layering-card-bar fill-${cls}" x="0" y="8" width="3.5" height="${NODE_H - 16}" rx="1.75"></rect>
        <text class="v-layering-card-role" x="14" y="17">${roleLabel(n).toUpperCase()}${inf.depth ? ` · HOP ${inf.depth}` : ""}</text>
        <text class="v-layering-card-name" x="14" y="36">${truncate(n.entity_name, 26)}</text>
        <text class="v-layering-card-amt" x="14" y="56">${inrCompact(attr)}<tspan class="v-layering-card-of" dx="6">${sub}</tspan></text>
        <rect class="v-layering-card-track" x="14" y="64" width="${mw}" height="4" rx="2"></rect>
        <rect class="v-layering-card-fill fill-${cls}" x="14" y="64" width="${(mw * r).toFixed(1)}" height="4" rx="2"></rect>`;
    },
    onSelect: (n, key) => {
      drawInspector(key);
    },
    onZoom: (k) => {
      zoomVal.textContent = `${Math.round(k * 100)}%`;
    },
  });
  ctx.onCleanup(() => chart.destroy());

  // ------------------------------------------------------------------------------------------ table (text alternative)
  function ensureGrid() {
    if (grid) return grid;
    grid = createDataGrid(slot("table-panel"), {
      ariaLabel: "Layering tree as an outline table",
      rowKey: "key",
      rows: flat,
      height: "min(64vh, 620px)",
      rowHeight: 40,
      countNoun: ["node", "nodes"],
      filters: [{ key: "q", type: "search", label: "Find entity", placeholder: "Find entity in tree…" }],
      clientFilter: (r, f) => !f.q || String(r.name || "").toLowerCase().includes(String(f.q).toLowerCase()),
      onRowClick: (r) => {
        chart.select(r.key, { scroll: true });
        drawInspector(r.key);
      },
      emptyState: { icon: "layers", title: "No layering flows" },
      columns: [
        { key: "name", label: "Entity", width: "minmax(240px, 3fr)",
          render: (r) => html`<span class="v-layering-indent v-layering-indent--${Math.min(r.depth, 8)}"><span class="legend__swatch sw-${roleCls(r.node)}" aria-hidden="true"></span><span class="truncate">${r.name}</span>${isLazy(r.node) ? html` <span class="badge badge--outline" title="More hops available: select the node and choose Load next hop">more</span>` : ""}</span>` },
        { key: "role", label: "Role", width: "140px", render: (r) => html`<span class="badge badge--${roleCls(r.node)}">${roleLabel(r.node)}</span>` },
        { key: "depth", label: "Hop", width: "64px", align: "right", format: (v) => int(v) },
        { key: "attr", label: "Attributed", width: "140px", align: "right", title: (r) => inr(r.node.attributed_amount ?? r.node.amount),
          render: (r) => html`${inr(r.node.attributed_amount ?? r.node.amount)}` },
        { key: "debit", label: "Debit value", width: "140px", align: "right", render: (r) => html`${inr(r.node.debit_amount)}` },
        { key: "ratio", label: "Attr./debit", width: "100px", align: "right",
          render: (r) => html`${pct(ratio(r.node.attributed_amount ?? r.node.amount, r.node.debit_amount))}` },
        { key: "share", label: "Of source", width: "100px", align: "right",
          render: (r) => html`${pct(ratio(r.node.attributed_amount ?? r.node.amount, shareBase))}` },
        { key: "txns", label: "Txns", width: "70px", align: "right", render: (r) => html`${int((r.node.tx_ids || []).length)}` },
      ],
    });
    ctx.onCleanup(() => grid.destroy());
    return grid;
  }

  function showTab(tab, { push = false } = {}) {
    p.tab = tab;
    slot("tabs").querySelectorAll("[data-tab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === tab ? "true" : "false"));
    slot("tree-panel").hidden = tab !== "tree";
    slot("table-panel").hidden = tab !== "table";
    slot("zoom").hidden = tab !== "tree";
    slot("expand-btn").hidden = tab !== "tree";
    if (tab === "table") ensureGrid().setRows(flat);
    ctx.setParams({ tab: tab === "tree" ? null : tab }, { push });
  }

  // ------------------------------------------------------------------------------------------ renderers
  function drawSources() {
    const sel = slot("source");
    const cands = data?.source_candidates || [];
    const withStmt = cands.filter((c) => c.has_statement);
    const other = cands.filter((c) => !c.has_statement);
    render(
      sel,
      cands.length
        ? html`${withStmt.length ? html`<optgroup label="Has statement">${withStmt.map((c) => html`<option value="${c.entity_id}">${c.name}</option>`)}</optgroup>` : ""}
            ${other.length ? html`<optgroup label="Details only">${other.map((c) => html`<option value="${c.entity_id}">${c.name}</option>`)}</optgroup>` : ""}`
        : html`<option value="">No entities in this case</option>`
    );
    sel.disabled = !cands.length;
    sel.value = sourceId() || "";
    loadCredits();
  }

  /** The entity the tree is traced from (the root credit's receiver when rooted at one credit). */
  const sourceId = () => data?.selected_source_id || data?.root_credit?.entity_id || data?.root?.entity_id || p.source || "";

  /** Credits received by the source (its own statements), for the "Root at" picker. */
  async function loadCredits() {
    const src = sourceId();
    if (!src || credits.source === src) {
      drawRootPicker();
      return;
    }
    creditsCtl?.abort();
    const c = new AbortController();
    creditsCtl = c;
    const unlink = linkAbort(ctx.signal, c);
    credits = { source: src, rows: [], loading: true, error: null };
    drawRootPicker();
    try {
      await ctx.store.ensureCaseData();
      if (!ctx.isCurrent() || creditsCtl !== c) return;
      const ent = ctx.store.entity(src);
      const accts = new Set((ent?.linked_account_numbers || []).map(String));
      const stmts = ctx.store.statements().filter((st) => accts.has(String(st.account_number)));
      const rows = [];
      for (const st of stmts) {
        const r = await ctx.api.get("transactions", { statement_id: st.statement_id, direction: "CR", sort: "amount", order: "desc", limit: CREDIT_LIMIT }, { signal: c.signal });
        if (!ctx.isCurrent() || creditsCtl !== c) return;
        rows.push(...(r.transactions || []));
      }
      rows.sort((a, b) => (Number(b.credit) || 0) - (Number(a.credit) || 0));
      credits = { source: src, rows: rows.slice(0, CREDIT_LIMIT), loading: false, error: null };
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent()) return;
      credits = { source: src, rows: [], loading: false, error: err };
    } finally {
      unlink();
    }
    drawRootPicker();
  }

  function drawRootPicker() {
    const sel = slot("root");
    const hint = slot("root-hint");
    const rc = data?.root_credit;
    const rows = credits.rows.slice();
    if (rc && !rows.some((t) => t.tx_id === rc.tx_id)) rows.unshift({ tx_id: rc.tx_id, txn_date: rc.date, credit: rc.amount, extracted_counterparty: rc.narration, raw_narration: rc.narration });
    const label = (t) => `${date(t.txn_date)} · ${inrCompact(t.credit)} · ${truncate(t.extracted_counterparty || t.raw_narration || "credit", 28)}`;
    render(
      sel,
      html`<option value="">All credits received</option>${rows.length
        ? html`<optgroup label="${credits.source && credits.rows.length >= CREDIT_LIMIT ? `Largest ${CREDIT_LIMIT} credits` : "One credit"}">${rows.map((t) => html`<option value="${t.tx_id}">${label(t)}</option>`)}</optgroup>`
        : ""}`
    );
    sel.value = p.root_tx || "";
    sel.disabled = credits.loading && !rows.length;
    hint.textContent = credits.loading
      ? "Loading this source's credits…"
      : credits.error
        ? "Could not load the credits for this source"
        : rows.length
          ? `${plural(credits.rows.length, "credit")} on this source's statements — pick one to trace only that money`
          : "This source has no statement credits to root at";
  }

  function drawNotices() {
    const out = [];
    const truncated = Boolean(data?.truncated || data?.root?.truncated);
    if (truncated) {
      out.push(html`<div class="callout callout--warn" role="status">${icon("alert")}<div><strong>Tree truncated.</strong>
        The trace stopped at ${int(data.node_count)} nodes, so deeper or smaller branches are missing. Reduce the max depth or
        holding window, switch to FIFO attribution, or move high-fan-out counterparties to Misc.</div></div>`);
    }
    const rc = data?.root_credit;
    if (rc) {
      out.push(html`<div class="callout callout--info v-layering-rootcredit-callout">${icon("info")}<div class="v-layering-rootcredit">
        <span><strong>Rooted at one credit:</strong> ${inr(rc.amount)} received by ${nameOf(rc.entity_id)} on ${date(rc.date)}${rc.utr ? html` · UTR <span class="mono">${rc.utr}</span>` : ""}.</span>
        <span class="muted small">${truncate(rc.narration || "", 80)}</span>
        <button type="button" class="btn btn--sm btn--ghost" data-action="clear-root">Trace all credits</button></div></div>`);
    }
    if (p.date_from || p.date_to) {
      out.push(html`<div class="callout callout--info">${icon("calendar")}<div>Only transactions ${p.date_from ? `from ${date(p.date_from)} ` : ""}${p.date_to ? `to ${date(p.date_to)}` : ""} are traced.
        <button type="button" class="btn btn--sm btn--ghost" data-action="clear-dates">Clear dates</button></div></div>`);
    }
    if (p.mode === "all") {
      out.push(html`<div class="callout callout--info">${icon("info")}<div><strong>All-outflows mode.</strong>
        Every debit inside an open holding window is shown at its full value, so a branch can exceed its parent and the
        same transaction can appear under more than one path. Use FIFO for attributable (court-ready) amounts.</div></div>`);
    }
    render(slot("notices"), html`${out}`);
  }

  function drawKpis() {
    const root = data?.root;
    if (!root) {
      render(slot("kpis"), "");
      return;
    }
    const lg = data.legend_counts || {};
    let maxDepth = 0;
    let endpoints = 0;
    let system = 0;
    let misc = 0;
    for (const r of flat) {
      const a = Number(r.node.attributed_amount ?? r.node.amount) || 0;
      maxDepth = Math.max(maxDepth, r.depth);
      if (r.depth > 0 && !(r.node.children || []).length) endpoints += a;
      if (r.node.is_system) system += a;
      if (r.node.role === "MISC") misc += a;
    }
    render(
      slot("kpis"),
      kpiGrid(
        [
          rootedAtCredit()
            ? { label: "Root credit", value: inrCompact(rootAmt), title: inr(rootAmt), sub: `${root.entity_name} · ${inrCompact(shareBase)} traced onward`, icon: "arrow-down-left" }
            : { label: "Source outflow", value: inrCompact(rootAmt), title: inr(rootAmt), sub: root.entity_name, icon: "arrow-up-right" },
          { label: "Hops traced", value: int(maxDepth), sub: `${int(data.window_days ?? p.window_days)} d window · ${flat.some((r) => isLazy(r.node)) ? "more on demand" : "fully loaded"}` },
          { label: "Entities", value: int(lg.total_entities), sub: `${int(lg.has_statement)} with statement · ${int(lg.details_only)} details-only` },
          { label: "Value at endpoints", value: inrCompact(endpoints), title: inr(endpoints), sub: p.mode === "all" ? "full debit values (may double count)" : "where traced funds stop" },
          { label: "Cash & system exits", value: inrCompact(system), title: inr(system), tone: system > 0 ? "warn" : "", sub: "ATM, charges, unresolved" },
          { label: "In Misc", value: inrCompact(misc), title: inr(misc),
            sub: `${plural((data.misc_paths || []).length, "branch", "branches")} · ${plural((data.misc_entity_ids || []).length, "entity", "entities")} case-wide` },
        ],
        { compact: true, label: "Layering summary" }
      )
    );
  }

  function drawLegend() {
    const lg = data?.legend_counts || {};
    const count = (pred) => flat.filter(pred).length;
    const items = [
      ["source", "Credit source", lg.credit_source || (data?.root ? 1 : 0)],
      ["statement", "Has statement", lg.has_statement],
      ["details", "Details only", lg.details_only],
      ["system", "System / cash", count((r) => r.node.is_system)],
      ["misc", "Misc (collapsed)", count((r) => r.node.role === "MISC")],
    ];
    render(
      slot("legend"),
      html`${items.map(([cls, label, n]) => html`<span class="legend__item"><span class="legend__swatch sw-${cls}" aria-hidden="true"></span>${label} <span class="tabular muted">(${int(n || 0)})</span></span>`)}`
    );
    const nMisc = (data?.misc_entity_ids || []).length + (data?.misc_paths || []).length;
    slot("misc-label").textContent = nMisc ? `Misc (${nMisc})` : "Misc";
  }

  function drawInspector(key) {
    const row = key && index.get(key);
    const box = slot("insp");
    if (!row) {
      render(box, emptyState({ icon: "layers", title: "Nothing selected", message: "Select a node in the tree or table.", compact: true }));
      return;
    }
    const n = row.node;
    if (key !== lastAnnounced) {
      lastAnnounced = key;
      announce(`Selected ${n.entity_name}.`);
    }
    const cls = roleCls(n);
    const attr = n.attributed_amount ?? n.amount;
    const debit = n.debit_amount ?? attr;
    const r = ratio(attr, debit);
    const isMisc = n.role === "MISC";
    const isRoot = row.depth === 0;
    const kids = (n.children || []).slice().sort((a, b) => (b.attributed_amount ?? b.amount) - (a.attributed_amount ?? a.amount));
    const members = n.misc_members || [];
    const lazy = isLazy(n);
    const loadingNow = lazy && chart.isLoading(key);
    render(
      box,
      html`<div class="stack-sm">
        <div class="row-wrap">
          <span class="badge badge--${cls}">${roleLabel(n)}</span>
          ${!isMisc && !isRoot && n.has_statement ? html`<span class="badge badge--statement">Statement on file</span>` : ""}
          <span class="badge badge--neutral">${isRoot ? "Root" : `Hop ${row.depth}`}</span>
        </div>
        <h3 class="v-layering-insp-name">${n.entity_name}</h3>
        ${row.path.length > 1
          ? html`<nav class="v-layering-path" aria-label="Path from source"><ol>${row.path.map((s, i) => html`<li>${i < row.path.length - 1
              ? html`<button type="button" class="v-layering-path-btn" data-action="select" data-key="${s.key}">${truncate(s.name, 22)}</button>`
              : html`<span aria-current="true" class="strong">${truncate(s.name, 22)}</span>`}</li>`)}</ol></nav>`
          : ""}
        <dl class="kv v-layering-kv">
          <dt>${isRoot ? (rootedAtCredit() ? "Root credit received" : "Total outflow") : "Attributed"}</dt><dd class="tabular strong">${inr(attr)}</dd>
          ${isRoot && rootedAtCredit() ? html`<dt>Traced onward</dt><dd class="tabular">${inr(shareBase)}</dd>` : ""}
          ${isRoot ? "" : html`<dt>Debit value</dt><dd class="tabular">${inr(debit)}</dd>
          <dt>Attributed / debit</dt><dd><span class="tabular">${pct(r)}</span>
            <span class="meter v-layering-meter" data-meter="${r == null ? 0 : Math.min(1, r)}" aria-hidden="true"><span class="meter__fill"></span></span></dd>
          <dt>Share of source</dt><dd class="tabular">${pct(ratio(attr, shareBase))}</dd>`}
          <dt>Transactions</dt><dd class="tabular">${int((n.tx_ids || []).length)}</dd>
          <dt>Onward flows</dt><dd class="tabular">${lazy ? html`<span class="muted">not loaded yet</span>` : int(kids.length)}</dd>
        </dl>
        ${lazy ? html`<button type="button" class="btn btn--sm btn--primary" data-action="load" data-key="${key}" ${loadingNow ? html`disabled aria-busy="true"` : ""}>${icon("plus", { size: "sm" })}${loadingNow ? "Loading next hop…" : "Load next hop"}</button>` : ""}
        <div class="row-wrap v-layering-actions">
          ${isMisc ? "" : html`<a class="btn btn--sm btn--primary" href="${ctx.href("profile", { entity: n.entity_id })}">${icon("user", { size: "sm" })}Open profile</a>
            <a class="btn btn--sm" href="${ctx.href("network", { entity: n.entity_id })}">${icon("network", { size: "sm" })}Network</a>`}
          ${!isRoot && !isMisc && n.has_statement && !n.is_system
            ? html`<button type="button" class="btn btn--sm" data-action="reroot" data-entity="${n.entity_id}">${icon("arrow-right", { size: "sm" })}Trace from here</button>` : ""}
          ${!isRoot && !isMisc
            ? html`<button type="button" class="btn btn--sm btn--ghost" data-action="misc-path" data-path="${n.path_key || key}" data-move="1"
                title="Park only this branch (this path from this source) in its parent's Misc node">${icon("layers", { size: "sm" })}Move branch to Misc</button>
              <button type="button" class="btn btn--sm btn--ghost" data-action="misc" data-entity="${n.entity_id}" data-move="1"
                title="Park this entity in Misc wherever it appears, for every source">Misc everywhere</button>` : ""}
        </div>
        ${isMisc
          ? html`<div class="stack-sm"><h4 class="caps">In this Misc node</h4>
              ${members.length
                ? html`<ul class="v-layering-misc-list">${members.map((m) => html`<li class="row-between">
                    <span class="min-w-0"><span class="truncate strong">${m.entity_name}</span>
                      <span class="xsmall muted tabular"> ${inrCompact(m.attributed_amount)}</span></span>
                    <span class="badge ${m.scope === "path" ? "badge--misc" : "badge--neutral"} v-layering-misc-scope">${m.scope === "path" ? "This branch" : "Case-wide"}</span>
                    ${m.scope === "path"
                      ? html`<button type="button" class="btn btn--sm" data-action="misc-path" data-path="${m.path_key}" data-move="0">Restore</button>`
                      : html`<button type="button" class="btn btn--sm" data-action="misc" data-entity="${m.entity_id}" data-move="0">Restore</button>`}</li>`)}</ul>`
                : html`<p class="muted small">No members listed.</p>`}</div>`
          : ""}
        ${kids.length
          ? html`<div class="stack-sm"><h4 class="caps">Onward flows</h4>
              <div class="table-wrap"><table class="table table--compact"><caption class="sr-only">Onward flows from ${n.entity_name}</caption>
                <thead><tr><th scope="col">Recipient</th><th scope="col" class="num">Attributed</th><th scope="col" class="num">Share</th></tr></thead>
                <tbody>${kids.slice(0, 12).map((c) => html`<tr class="is-clickable" data-action="select" data-key="${keyOf(c, key)}">
                  <td><span class="legend__swatch sw-${roleCls(c)}" aria-hidden="true"></span> ${truncate(c.entity_name, 28)}</td>
                  <td class="num tabular">${inrCompact(c.attributed_amount ?? c.amount)}</td>
                  <td class="num tabular">${pct(ratio(c.attributed_amount ?? c.amount, isRoot ? shareBase : attr), { decimals: 0 })}</td></tr>`)}</tbody></table></div>
              ${kids.length > 12 ? html`<p class="xsmall muted">+${int(kids.length - 12)} more in the tree</p>` : ""}</div>`
          : ""}
      </div>`
    );
    box.querySelectorAll("[data-meter]").forEach((m) => m.style.setProperty("--value", `${(Number(m.dataset.meter) * 100).toFixed(1)}%`));
  }

  function showMessage(content) {
    const msg = slot("tree-msg");
    const has = content != null;
    render(msg, has ? content : "");
    msg.hidden = !has;
    slot("tree").hidden = has;
    el.querySelector(".v-layering-hint").hidden = has;
  }

  function drawEmpty(kind, err) {
    chart.setRoot(null);
    if (kind === "error") {
      showMessage(errorState(err, { retryId: "retry", title: "Could not compute the layering tree" }));
    } else {
      const noCands = !(data?.source_candidates || []).length;
      showMessage(
        noCands
          ? emptyState({ icon: "upload", title: "No entities yet", message: "Upload statements to resolve entities and trace layering.", action: { label: "Upload statements", href: ctx.href("ingest") } })
          : emptyState({ icon: "layers", title: "No onward flows", message: "This source has no traced outflows for the current window. Try a longer holding window, another source, or All-outflows mode." })
      );
    }
  }

  // ------------------------------------------------------------------------------------------ load
  async function load({ reset = false } = {}) {
    loadCtl?.abort();
    const ac = new AbortController();
    loadCtl = ac;
    const onAbort = () => ac.abort();
    ctx.signal.addEventListener("abort", onAbort, { once: true });
    const seq = (loadSeq += 1);
    const first = !data;
    slot("busy").hidden = first;
    slot("body").setAttribute("aria-busy", "true");
    try {
      const res = await ctx.api.get("trace/layering", { ...traceParams(), source_entity_id: p.root_tx ? null : p.source || null, max_depth: p.max_depth }, { signal: ac.signal });
      if (!ctx.isCurrent() || seq !== loadSeq) return;
      const prevSource = data?.source_key;
      if (reset || prevSource !== res.source_key) lazyLoaded.clear();
      else await reexpand(res.root, ac.signal);
      if (!ctx.isCurrent() || seq !== loadSeq) return;
      data = res;
      rootAmt = Number(res.root?.attributed_amount ?? res.root?.amount) || 0;
      shareBase = res.root_credit
        ? (res.root?.children || []).reduce((sum, c) => sum + (Number(c.attributed_amount ?? c.amount) || 0), 0)
        : rootAmt;
      indexTree(res.root);
      drawSources();
      drawNotices();
      drawKpis();
      drawLegend();
      if (grid) grid.setRows(flat);
      if (!res.root || !(res.root.children || []).length) {
        drawEmpty("empty");
        if (res.root) drawInspector(keyOf(res.root, null));
        else drawInspector(null);
        return;
      }
      showMessage(null);
      const sourceChanged = reset || prevSource !== res.source_key;
      if (sourceChanged) depthLimit = initialDepthLimit();
      const keep = chart.selected();
      chart.setRoot(res.root, { resetState: sourceChanged });
      if (first || sourceChanged) chart.fit({ min: 0.7 });
      if (keep && !sourceChanged) {
        const target = nearestKey(keep);
        if (target) chart.select(target, { scroll: false, notify: false });
      }
      drawInspector(chart.selected());
      ctx.setCrumbs([{ label: res.root.entity_name }]);
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent() || seq !== loadSeq) return;
      if (first) {
        render(slot("kpis"), "");
        drawEmpty("error", err);
        render(slot("insp"), emptyState({ icon: "layers", title: "Nothing selected", compact: true }));
      } else {
        ctx.toast.error(err, { title: "Layering not updated" });
      }
    } finally {
      ctx.signal.removeEventListener("abort", onAbort);
      if (seq === loadSeq) {
        slot("busy").hidden = true;
        slot("body").removeAttribute("aria-busy");
      }
    }
  }

  /** Parameters shared by trace/layering and trace/layering/expand. */
  function traceParams() {
    return {
      window_days: p.window_days,
      mode: p.mode,
      trace_depth: Math.max(p.trace_depth, p.max_depth),
      root_tx_id: p.root_tx || null,
      date_from: p.date_from || null,
      date_to: p.date_to || null,
    };
  }

  /** GET trace/layering/expand for one node: children of node.path_key; attaches them in place. */
  async function fetchChildren(n, signal) {
    const res = await ctx.api.get(
      "trace/layering/expand",
      { ...traceParams(), source: data?.selected_source_id || (p.root_tx ? null : p.source) || null, path: n.path_key },
      { signal }
    );
    n.children = res.children || [];
    n.has_more_children = n.children.length > 0;
    if (res.truncated) n.truncated = true;
    return res;
  }

  /** Tree callback for a lazy node ("+ more", →, or Load next hop). */
  async function expandNode(n, key) {
    if (!isLazy(n)) return false;
    drawInspector(chart.selected());
    try {
      const res = await fetchChildren(n, ctx.signal);
      if (!ctx.isCurrent()) return false;
      lazyLoaded.add(n.path_key);
      indexTree(data.root);
      drawKpis();
      drawLegend();
      if (grid) grid.setRows(flat);
      announce(res.children?.length ? `Loaded ${plural(res.children.length, "onward flow")} from ${n.entity_name}.` : `${n.entity_name} has no further onward flows.`);
      if (res.truncated) ctx.toast.warn(`The next hop from ${n.entity_name} was truncated at the node limit.`);
      return true;
    } catch (err) {
      if (!isAbort(err) && ctx.isCurrent()) ctx.toast.error(err, { title: "Could not load the next hop" });
      return false;
    } finally {
      if (ctx.isCurrent()) setTimeout(() => ctx.isCurrent() && drawInspector(chart.selected()), 0);
    }
  }

  /** After a refetch, re-load the branches the analyst drilled into (shallowest first, capped). */
  async function reexpand(root, signal) {
    if (!root || !lazyLoaded.size) return;
    const byKey = new Map();
    (function walk(n) {
      if (n.path_key) byKey.set(n.path_key, n);
      (n.children || []).forEach(walk);
    })(root);
    const keys = [...lazyLoaded].sort((a, b) => a.split(">").length - b.split(">").length).slice(0, MAX_REEXPAND);
    for (const k of keys) {
      const n = byKey.get(k);
      if (!n || !isLazy(n)) {
        if (!n) lazyLoaded.delete(k);
        continue;
      }
      try {
        await fetchChildren(n, signal);
        (n.children || []).forEach(function walk(c) {
          if (c.path_key) byKey.set(c.path_key, c);
          (c.children || []).forEach(walk);
        });
      } catch (err) {
        if (isAbort(err)) throw err;
        lazyLoaded.delete(k);
      }
    }
  }

  // ------------------------------------------------------------------------------------------ actions
  function setParam(patch, { push = false } = {}) {
    Object.assign(p, patch);
    ctx.setParams(
      {
        source: p.source || null,
        root_tx: p.root_tx || null,
        mode: p.mode === DEFAULTS.mode ? null : p.mode,
        window_days: p.window_days === DEFAULTS.window_days ? null : String(p.window_days),
        max_depth: p.max_depth === DEFAULTS.max_depth ? null : String(p.max_depth),
        trace_depth: p.trace_depth === DEFAULTS.trace_depth ? null : String(p.trace_depth),
        date_from: p.date_from || null,
        date_to: p.date_to || null,
      },
      { push }
    );
  }

  function syncControls() {
    slot("mode").querySelectorAll("[data-mode]").forEach((b) => {
      const on = b.dataset.mode === p.mode;
      b.setAttribute("aria-checked", on ? "true" : "false");
      b.tabIndex = on ? 0 : -1;
    });
    form.elements.window_days.value = String(p.window_days);
    form.elements.max_depth.value = String(p.max_depth);
    form.elements.trace_depth.value = String(p.trace_depth);
    form.elements.date_from.value = p.date_from;
    form.elements.date_to.value = p.date_to;
    if (p.source && data) slot("source").value = p.source;
    slot("root").value = p.root_tx || "";
  }

  function setMode(mode) {
    if (mode === p.mode) return;
    setParam({ mode }, { push: true });
    syncControls();
    load();
  }

  /** Case-wide Misc (entity scope): the entity is parked wherever it appears, for every source. */
  async function toggleMisc(entityId, move, btn) {
    const name = nameOf(entityId);
    const res = await ctx.withBusy(btn, () => ctx.api.post("trace/layering/misc", { entity_id: entityId, move_to_misc: move }), {
      error: move ? "Could not move to Misc" : "Could not restore",
    });
    if (!res || !ctx.isCurrent()) return;
    ctx.toast(move ? `${name} moved to Misc everywhere` : `${name} restored to the tree`, {
      type: "success",
      action: { label: "Undo", run: () => toggleMisc(entityId, !move, null) },
    });
    // analytics bump → onData below refetches the tree (collapse + selection preserved)
    await ctx.mutated(MUTATION_TOPICS.layeringMisc);
  }

  /** Per-branch Misc (path scope): only this path from this source (source_key) is parked. */
  async function toggleMiscPath(pathKey, move, btn) {
    const ids = String(pathKey || "").split(">").filter((x) => x && x !== "MISC");
    if (ids.length < 2) return;
    const name = nameOf(ids[ids.length - 1]);
    const src = data?.selected_source_id || ids[0];
    const res = await ctx.withBusy(btn, () => ctx.api.post("trace/layering/misc", { path: ids.join(">"), move_to_misc: move, source_entity_id: src, root_tx_id: p.root_tx || null }), {
      error: move ? "Could not move this branch to Misc" : "Could not restore this branch",
    });
    if (!res || !ctx.isCurrent()) return;
    ctx.toast(move ? `${name} moved to Misc on this branch` : `${name} restored on this branch`, {
      type: "success",
      action: { label: "Undo", run: () => toggleMiscPath(pathKey, !move, null) },
    });
    await ctx.mutated(MUTATION_TOPICS.layeringMisc);
  }

  const pathLabel = (pk) => String(pk).split(">").map((id) => truncate(nameOf(id), 16)).join(" › ");

  function exportSvg() {
    if (!data?.root || !chart.visibleCount()) {
      ctx.toast("Nothing to export yet", { type: "warn" });
      return;
    }
    const src = data.root.entity_name;
    const title = `CypherTrace layering tree — ${src} — ${p.mode.toUpperCase()}, ${p.window_days} d window, depth ${p.max_depth} — case ${ctx.caseId}`;
    const text = chart.exportSvg({ title });
    const f = downloadText(`layering_${src}_${p.mode}_${new Date().toISOString().slice(0, 10)}.svg`, text, "image/svg+xml");
    ctx.toast.success(`Saved ${f.filename}`);
  }

  function exportCsv() {
    if (!flat.length) {
      ctx.toast("Nothing to export yet", { type: "warn" });
      return;
    }
    const head = ["hop", "path", "entity", "entity_id", "role", "is_system", "attributed_amount", "debit_amount", "share_of_source", "tx_count", "tx_ids"];
    const lines = [head.join(",")];
    for (const r of flat) {
      const n = r.node;
      const attr = n.attributed_amount ?? n.amount;
      lines.push([r.depth, r.path.map((s) => s.name).join(" > "), n.entity_name, n.entity_id, n.role, n.is_system ? "yes" : "no",
        Number(attr).toFixed(2), Number(n.debit_amount ?? attr).toFixed(2), (ratio(attr, shareBase) ?? 0).toFixed(4),
        (n.tx_ids || []).length, (n.tx_ids || []).join(" ")].map(csvCell).join(","));
    }
    const f = downloadText(`layering_${data.root.entity_name}_${p.mode}.csv`, `﻿${lines.join("\r\n")}`, "text/csv;charset=utf-8");
    ctx.toast.success(`Saved ${f.filename}`);
  }

  // ------------------------------------------------------------------------------------------ events
  const exportMenu = createMenu(slot("export-btn"), {
    label: "Export layering",
    items: () => [
      { id: "svg", label: "Tree as SVG", description: "Visible nodes, current theme", icon: "download", disabled: !data?.root, run: exportSvg },
      { id: "csv", label: "All nodes as CSV", description: "Path, amounts, transaction ids", icon: "table", disabled: !flat.length, run: exportCsv },
    ],
  });
  ctx.onCleanup(() => exportMenu.destroy());

  const miscMenu = createMenu(slot("misc-btn"), {
    label: "Misc entities",
    items: () => {
      const ids = data?.misc_entity_ids || [];
      const paths = data?.misc_paths || [];
      if (!ids.length && !paths.length) return [{ group: "Misc" }, { id: "none", label: "Nothing parked", description: "Select a node and choose Move branch to Misc", disabled: true }];
      return [
        ...(paths.length ? [{ group: `Branches parked for this source (${paths.length})` },
          ...paths.map((pk) => ({ id: `restore-path-${pk}`, label: `Restore ${nameOf(pk.split(">").pop())}`, description: pathLabel(pk), icon: "refresh", run: () => toggleMiscPath(pk, false, null) }))] : []),
        ...(ids.length ? [{ group: `Parked case-wide (${ids.length})` },
          ...ids.map((id) => ({ id: `restore-${id}`, label: `Restore ${nameOf(id)}`, description: "Every source", icon: "refresh", run: () => toggleMisc(id, false, null) }))] : []),
      ];
    },
  });
  ctx.onCleanup(() => miscMenu.destroy());

  const expandMenu = createMenu(slot("expand-btn"), {
    label: "Expand or collapse",
    items: () => [
      { id: "all", label: "Expand all", description: `${plural(flat.length, "node")}`, run: () => chart.expandAll() },
      { id: "collapse", label: "Collapse to first hop", run: () => chart.collapseAll() },
      { separator: true },
      ...[2, 3, 4].map((d) => ({ id: `d${d}`, label: `Show ${d} hops`, run: () => chart.collapseToDepth(d) })),
    ],
  });
  ctx.onCleanup(() => expandMenu.destroy());

  ctx.onCleanup(delegate(el, "click", "[data-action]", (e, b) => {
    const a = b.dataset.action;
    if (a === "zoom-in") chart.zoom(1.2);
    else if (a === "zoom-out") chart.zoom(1 / 1.2);
    else if (a === "zoom-reset") chart.zoomTo(1);
    else if (a === "fit") chart.fit();
    else if (a === "retry") load();
    else if (a === "select") {
      chart.select(b.dataset.key, { scroll: true });
      drawInspector(b.dataset.key);
    } else if (a === "reroot") {
      setParam({ source: b.dataset.entity }, { push: true });
      load({ reset: true });
    } else if (a === "misc") toggleMisc(b.dataset.entity, b.dataset.move === "1", b);
    else if (a === "misc-path") toggleMiscPath(b.dataset.path, b.dataset.move === "1", b);
    else if (a === "load") {
      chart.select(b.dataset.key, { scroll: true, notify: false });
      chart.load(b.dataset.key);
    } else if (a === "clear-root") {
      setParam({ root_tx: "" }, { push: true });
      syncControls();
      load({ reset: true });
    } else if (a === "clear-dates") {
      setParam({ date_from: "", date_to: "" }, { push: true });
      syncControls();
      load();
    }
  }));
  ctx.onCleanup(delegate(slot("mode"), "click", "[data-mode]", (e, b) => setMode(b.dataset.mode)));
  ctx.onCleanup(delegate(slot("mode"), "keydown", "[data-mode]", (e) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
    e.preventDefault();
    const next = p.mode === "fifo" ? "all" : "fifo";
    setMode(next);
    slot("mode").querySelector(`[data-mode="${next}"]`)?.focus();
  }));
  ctx.onCleanup(delegate(slot("tabs"), "click", "[data-tab]", (e, b) => showTab(b.dataset.tab, { push: false })));
  ctx.onCleanup(delegate(slot("tabs"), "keydown", "[data-tab]", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const next = p.tab === "tree" ? "table" : "tree";
    showTab(next);
    slot("tabs").querySelector(`[data-tab="${next}"]`)?.focus();
  }));
  form.addEventListener("submit", (e) => e.preventDefault());
  form.addEventListener("change", (e) => {
    const t = e.target;
    if (t.name === "source") {
      setParam({ source: t.value, root_tx: "" }, { push: true });
      syncControls();
      load({ reset: true });
    } else if (t.name === "root_tx") {
      setParam({ root_tx: t.value }, { push: true });
      load({ reset: true });
    } else if (t.name === "trace_depth") {
      const v = clampInt(t.value, 1, 32, DEFAULTS.trace_depth);
      t.value = String(v);
      if (v === p.trace_depth) return;
      setParam({ trace_depth: v });
      load();
    } else if (t.name === "date_from" || t.name === "date_to") {
      const from = isoOk(form.elements.date_from.value);
      const to = isoOk(form.elements.date_to.value);
      if (from && to && from > to) {
        ctx.toast.warn("“From” must be on or before “To”.");
        syncControls();
        return;
      }
      setParam({ date_from: from, date_to: to }, { push: true });
      load();
    } else if (t.name === "window_days") {
      const v = clampInt(t.value, 1, 366, DEFAULTS.window_days);
      t.value = String(v);
      if (v === p.window_days) return;
      setParam({ window_days: v });
      load();
    } else if (t.name === "max_depth") {
      setParam({ max_depth: clampInt(t.value, 1, 8, DEFAULTS.max_depth) });
      load();
    }
  });

  ctx.onData([TOPICS.analytics, TOPICS.entities], () => load());

  ctx.registerCommands([
    { id: "layering.export-svg", title: "Export layering tree as SVG", group: "Layering", icon: "download", run: exportSvg },
    { id: "layering.export-csv", title: "Export layering nodes as CSV", group: "Layering", icon: "table", run: exportCsv },
    { id: "layering.expand-all", title: "Expand all layering nodes", group: "Layering", icon: "plus", run: () => chart.expandAll() },
    { id: "layering.collapse-all", title: "Collapse layering tree to first hop", group: "Layering", icon: "minus", run: () => chart.collapseAll() },
    { id: "layering.mode-fifo", title: "Layering: FIFO attribution", group: "Layering", keywords: ["mode"], when: () => p.mode !== "fifo", run: () => setMode("fifo") },
    { id: "layering.mode-all", title: "Layering: all outflows", group: "Layering", keywords: ["mode"], when: () => p.mode !== "all", run: () => setMode("all") },
    { id: "layering.table", title: "Layering: show as table", group: "Layering", when: () => p.tab !== "table", run: () => showTab("table") },
    { id: "layering.tree", title: "Layering: show as tree", group: "Layering", when: () => p.tab !== "tree", run: () => showTab("tree") },
  ]);

  showTab(p.tab);
  await load({ reset: true });

  return {
    update(params) {
      const next = readParams(params);
      const refetch = ["source", "root_tx", "mode", "window_days", "max_depth", "trace_depth", "date_from", "date_to"].some((k) => next[k] !== p[k]);
      const sourceChanged = next.source !== p.source || next.root_tx !== p.root_tx;
      const tabChanged = next.tab !== p.tab;
      p = next;
      syncControls();
      if (tabChanged) showTab(p.tab);
      if (refetch) load({ reset: sourceChanged });
    },
    unmount() {
      loadCtl?.abort();
      creditsCtl?.abort();
    },
  };
}

