/* CypherTrace network graph (#/case/{id}/network): directed fund-flow graph between resolved entities.
 *
 * Data: GET trace/graph?date_from&date_to&min_amount&max_amount&channel&keyword&ifsc&max_nodes (server filters, all
 * per transaction; ifsc is a comma list of up to 20 full IFSCs or bank prefixes, matched by the engine against
 * the statement account's IFSC or the counterparty IFSC in the narration; centrality is computed server-side on
 * the filtered graph; summary.filters echoes what the engine applied). The server sends the top max_nodes
 * entities by volume (P2: default 400, summary.truncated); "Load more from server" doubles max_nodes and
 * "Expand neighbours" in the inspector merges GET trace/graph/expand?entity_id&limit (the node's heaviest
 * edges and their counterparties) into the loaded graph. Large views arrive with seed positions only
 * (summary.layout "seed"); the client force layout places them.
 * Colours: each node's colour_keys{role, statement, risk_tier, betweenness} (P1, TG-07) pick the fill, and the
 * legend lists summary.colour_by[mode] — the server owns the bins, so the UI never re-derives a tier. Client-side refinements: conduit focus (legacy "conduits only"), neighbourhood isolation and a node cap with
 * "show more". The date window comes from a time brush over the case's statement activity
 * (GET transactions, paged, txn_date + amount only used).
 *
 * Layout: canvas renderer in charts/graph-canvas.js (client-side force layout seeded with the server
 * coordinates). Colour-by: role / risk tier / statement status / betweenness. System parties are diamonds,
 * details-only counterparties are rings. Click a node -> entity inspector (side panel; drawer on phones);
 * double-click / Shift+Enter -> profile. Accessible alternatives: the Entities (centrality) and Flows tables,
 * keyboard navigation on the canvas with a live region, and an aria summary of the graph.
 * URL params: entity, date_from, date_to, amount_min, amount_max, channel, q, ifsc, color, conduits, ego, cap, tab.
 */
import { isAbort, linkAbort } from "../core/api.js";
import { saveBlob } from "../core/files.js";
import { GRAPH_ROLES, PAYMENT_CHANNELS, entityStatus, tierClass } from "../core/constants.js";
import { delegate, html, render } from "../core/dom.js";
import { date, inr, inrCompact, int, num, plural, signClass } from "../core/format.js";
import { TOPICS } from "../core/store.js";
import { createDataGrid } from "../components/datagrid.js";
import { openDrawer } from "../components/drawer.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { createTabs } from "../components/tabs.js";
import { emptyState, errorState, pageHead, skeleton, spinner } from "../components/states.js";
import { createGraphCanvas, createTimeBrush } from "../charts/graph-canvas.js";

export const styles = ["views/network.css"];

const CAP_STEP = 150;
const SERVER_NODES = 400; // trace/graph max_nodes default (server: GRAPH_DEFAULT_MAX_NODES)
const SERVER_NODES_MAX = 5000;
const EXPAND_LIMIT = 50;
const ROLE_ORDER = { PERIPHERAL_LEAF: 0, EXIT_ENDPOINT: 1, PRIMARY_HUB: 2, BRIDGE_CONDUIT: 3 };
const COLOR_MODES = [
  { id: "role", label: "Role" },
  { id: "risk", label: "Risk tier" },
  { id: "statement", label: "Statement" },
  { id: "betweenness", label: "Betweenness" },
];
const SERVER_KEYS = ["date_from", "date_to", "amount_min", "amount_max", "channel", "q", "ifsc"];
const CLIENT_KEYS = [];
const ACTIVITY_PAGES = 5;
const ACTIVITY_PAGE = 2000;

const roleMeta = (role) => GRAPH_ROLES[role] || GRAPH_ROLES.PERIPHERAL_LEAF;
const edgeKey = (e) => `${e.source}>${e.target}`;
const posNum = (v) => {
  const n = Number(v);
  return Number.isFinite(n) && n > 0 ? n : null;
};
/** Fill token + legend swatch class + label for each server colour key, per colour-by mode. */
const COLOUR_KEYS = {
  role: Object.fromEntries(Object.entries(GRAPH_ROLES).map(([k, r]) => [k, { fill: `--node-${r.cls}`, sw: `sw-${r.cls}`, label: r.label }])),
  statement: {
    HAS_STATEMENT: { fill: "--node-statement", sw: "sw-statement", label: "Statement held" },
    DETAILS_ONLY: { fill: "--node-details", sw: "sw-details", label: "Details only" },
    SYSTEM: { fill: "--node-system", sw: "sw-system", label: "System / generic party" },
  },
  risk_tier: {
    CRITICAL: { fill: "--risk-critical", sw: "sw-critical", label: "Critical (80–100)" },
    SUSPICIOUS: { fill: "--risk-suspicious", sw: "sw-suspicious", label: "Suspicious (50–79)" },
    LOW_RISK: { fill: "--risk-low", sw: "sw-low", label: "Low risk (scored)" },
    DETAILS_ONLY: { fill: "--node-details", sw: "sw-details", label: "No statement (not scored)" },
    UNSCORED: { fill: "--node-system", sw: "sw-system", label: "Not scored" },
  },
  betweenness: {
    none: { fill: "--chart-seq-1", sw: "sw-seq-1", label: "None" },
    low: { fill: "--chart-seq-3", sw: "sw-seq-3", label: "Low" },
    medium: { fill: "--chart-seq-4", sw: "sw-seq-4", label: "Medium" },
    high: { fill: "--chart-seq-6", sw: "sw-seq-6", label: "High" },
    top: { fill: "--chart-seq-7", sw: "sw-seq-7", label: "Top (highest percentile)" },
  },
};
const MODE_KEY = { role: "role", risk: "risk_tier", statement: "statement", betweenness: "betweenness" };
/** Normalise the IFSC filter to the engine's comma list (upper case, de-duplicated, max 20). */
const ifscList = (v) => [...new Set(String(v || "").toUpperCase().split(/[\s,;]+/).map((x) => x.trim()).filter(Boolean))].slice(0, 20);
const isoOk = (v) => (/^\d{4}-\d{2}-\d{2}$/.test(String(v || "")) ? String(v) : "");

const roleBadge = (n) => {
  const r = roleMeta(n.node_role);
  return html`<span class="badge badge--outline v-network-role"><span class="legend__swatch legend__swatch--round sw-${r.cls}"></span>${n.is_system ? "System party" : r.label}</span>`;
};
const statusBadge = (n) => {
  const s = entityStatus({ ...n, canonical_name: n.label });
  return html`<span class="badge badge--${s.cls}">${s.label}</span>`;
};
const riskBadge = (n) =>
  n.mule_score != null
    ? html`<span class="badge badge--${tierClass(n.risk_tier)}" title="Composite mule score ${num(n.mule_score, 1)} / 100">${n.risk_tier} · ${num(n.mule_score, 1)}</span>`
    : html`<span class="badge badge--neutral">${n.risk_tier || "—"}</span>`;

export async function mount(el, ctx) {
  await ctx.store.ensureCaseData();
  if (!ctx.isCurrent()) return undefined;

  const p = ctx.params;
  const state = {
    f: {
      date_from: isoOk(p.date_from),
      date_to: isoOk(p.date_to),
      amount_min: posNum(p.amount_min) ? String(posNum(p.amount_min)) : "",
      channel: PAYMENT_CHANNELS.includes(String(p.channel || "").toUpperCase()) ? String(p.channel).toUpperCase() : "",
      q: p.q || "",
      amount_max: posNum(p.amount_max) ? String(posNum(p.amount_max)) : "",
      ifsc: ifscList(p.ifsc).join(","),
    },
    color: COLOR_MODES.some((m) => m.id === p.color) ? p.color : "role",
    conduits: p.conduits === "1",
    ego: p.ego === "1",
    cap: Math.max(CAP_STEP, Number(p.cap) || CAP_STEP),
    serverNodes: SERVER_NODES,
    expanded: new Set(), // entity ids merged by "Expand neighbours"; always shown, beyond the cap
    selected: p.entity || null,
    graph: null,
    view: null,
    error: null,
    loading: false,
    maxBetweenness: 0,
  };
  const narrowMQ = window.matchMedia ? window.matchMedia("(max-width: 767.98px)") : null;

  render(
    el,
    html`<div class="stack-lg">
      ${pageHead({
        title: "Fund-flow network",
        sub: "Directed transfers between resolved entities. Edge width ∝ √ amount; curved pairs are mutual flows.",
        actions: html`<button type="button" class="btn btn--sm" data-action="png">${icon("download", { size: "sm" })}PNG</button>
          <button type="button" class="btn btn--sm" data-action="refresh">${icon("refresh", { size: "sm" })}Refresh</button>`,
      })}
      <div data-slot="kpis">${skeleton({ kind: "kpis", rows: 6 })}</div>
      <div data-slot="notes"></div>
      <section class="card" aria-labelledby="net-filters-title">
        <form class="card__body stack v-network-filters" data-slot="form" novalidate>
          <h2 class="sr-only" id="net-filters-title">Graph filters</h2>
          <div class="v-network-filter-row">
            <label class="field v-network-f-q"><span class="field__label">Keyword</span>
              <span class="input-group">${icon("search", { size: "sm" })}<input class="input" type="search" name="q" placeholder="Narration or party" autocomplete="off"></span></label>
            <label class="field"><span class="field__label">Channel</span>
              <select class="select" name="channel"><option value="">All channels</option>${PAYMENT_CHANNELS.map((c) => html`<option value="${c}">${c}</option>`)}</select></label>
            <label class="field"><span class="field__label">Min ₹ per transaction</span>
              <input class="input input--num" type="number" name="amount_min" min="0" step="any" inputmode="decimal" placeholder="0"></label>
            <label class="field"><span class="field__label">Max ₹ per transaction</span>
              <input class="input input--num" type="number" name="amount_max" min="0" step="any" inputmode="decimal" placeholder="No limit"></label>
            <label class="field"><span class="field__label">Bank / IFSC</span>
              <input class="input" type="text" name="ifsc" data-slot="ifsc" list="net-ifsc-list" autocomplete="off" spellcheck="false"
                placeholder="e.g. SBIN, HDFC0000240" aria-describedby="net-ifsc-hint">
              <datalist id="net-ifsc-list" data-slot="ifsc-list"></datalist>
              <span class="field__hint" id="net-ifsc-hint">Comma list of bank codes or full IFSCs (up to 20)</span></label>
            <div class="v-network-filter-actions">
              <button type="submit" class="btn btn--primary">${icon("filter", { size: "sm" })}Apply</button>
              <button type="button" class="btn btn--ghost" data-action="reset">Reset</button>
            </div>
          </div>
          <div class="v-network-time">
            <div class="row-between row-wrap v-network-time__head">
              <span class="field__label" id="net-brush-label">Date window <span class="muted" data-slot="brush-note"></span></span>
              <div class="row row-wrap v-network-dates">
                <label class="field field--inline"><span class="field__label">From</span><input class="input input--sm" type="date" name="date_from"></label>
                <label class="field field--inline"><span class="field__label">To</span><input class="input input--sm" type="date" name="date_to"></label>
                <button type="button" class="btn btn--sm btn--ghost" data-action="clear-dates">All dates</button>
              </div>
            </div>
            <div class="v-network-brush" data-slot="brush" aria-labelledby="net-brush-label"></div>
            <p class="field__hint">Bars show daily statement volume. Drag across the strip to restrict the graph to a period; drag the window to move it; click the strip to clear.</p>
          </div>
        </form>
      </section>
      <div class="v-network-layout">
        <section class="card v-network-main" aria-label="Network">
          <div class="card__body" data-slot="tabs"></div>
        </section>
        <aside class="card v-network-inspector" aria-label="Entity inspector" data-slot="inspector"></aside>
      </div>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);
  const form = slot("form");

  /* ------------------------------------------------------------------ tooltip (fixed, CSSOM positioned) */
  const tip = document.createElement("div");
  tip.className = "tooltip v-network-tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.appendChild(tip);
  ctx.onCleanup(() => tip.remove());
  function showTip(content, x, y) {
    render(tip, content);
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    const left = Math.min(window.innerWidth - r.width - 8, x + 14);
    const top = y + 16 + r.height > window.innerHeight ? y - r.height - 12 : y + 16;
    tip.style.left = `${Math.max(8, left)}px`;
    tip.style.top = `${Math.max(8, top)}px`;
  }
  const hideTip = () => {
    tip.hidden = true;
  };

  /* ------------------------------------------------------------------ statements lookup (IFSC, banks) */
  let acctToStmt = new Map();
  function indexStatements() {
    acctToStmt = new Map();
    const ifsc = new Map();
    for (const s of ctx.store.statements()) {
      if (s.account_number) acctToStmt.set(String(s.account_number), s);
      if (s.ifsc_code && !ifsc.has(s.ifsc_code)) ifsc.set(s.ifsc_code, s.bank_name || s.bank_code || "");
    }
    // suggestions: every statement IFSC and its 4-letter bank prefix (the engine accepts either)
    const sugg = new Map();
    for (const [code, bank] of ifsc.entries()) {
      sugg.set(code, bank);
      if (code.length >= 4 && !sugg.has(code.slice(0, 4))) sugg.set(code.slice(0, 4), `${bank || "bank"} · all branches`);
    }
    const opts = [...sugg.entries()].sort((a, b) => a[0].localeCompare(b[0]));
    render(slot("ifsc-list"), html`${opts.map(([code, bank]) => html`<option value="${code}">${bank || ""}</option>`)}`);
    slot("ifsc").value = state.f.ifsc;
  }
  const bankOf = (n) => (n.accounts || []).map((a) => acctToStmt.get(String(a))).filter(Boolean);

  function syncForm() {
    for (const k of [...SERVER_KEYS, ...CLIENT_KEYS]) {
      const input = form.elements.namedItem(k);
      if (input) input.value = state.f[k] || "";
    }
  }

  /* ------------------------------------------------------------------ tabs + graph shell */
  let entGrid = null;
  let flowGrid = null;
  const tabs = createTabs(slot("tabs"), {
    label: "Network views",
    tabs: [
      { id: "graph", label: "Graph", icon: "network" },
      { id: "entities", label: "Entities", icon: "table", badge: "" },
      { id: "flows", label: "Flows", icon: "swap", badge: "" },
    ],
    selected: ["graph", "entities", "flows"].includes(p.tab) ? p.tab : "graph",
    fireInitial: false,
    onChange: (id) => {
      ctx.setParams({ tab: id === "graph" ? null : id });
      ensureTable(id);
    },
  });
  const graphPanel = tabs.panel("graph");
  render(
    graphPanel,
    html`<div class="stack-sm">
      <div class="toolbar v-network-toolbar">
        <div class="seg" role="radiogroup" aria-label="Colour nodes by">
          ${COLOR_MODES.map((m) => html`<button type="button" class="seg__btn" role="radio" data-color="${m.id}" aria-checked="${m.id === state.color ? "true" : "false"}">${m.label}</button>`)}
        </div>
        <button type="button" class="chip" data-action="conduits" aria-pressed="${state.conduits ? "true" : "false"}"
          title="Bridge / conduit accounts and the hubs and exits directly connected to them">Conduit focus</button>
        <span class="spacer"></span>
        <span class="xsmall muted v-network-status" data-slot="status" aria-live="polite"></span>
        <div class="btn-group" role="group" aria-label="Graph controls">
          <button type="button" class="btn btn--sm btn--icon" data-action="zoom-out" aria-label="Zoom out" title="Zoom out (−)">${icon("minus", { size: "sm" })}</button>
          <button type="button" class="btn btn--sm btn--icon" data-action="zoom-in" aria-label="Zoom in" title="Zoom in (+)">${icon("plus", { size: "sm" })}</button>
          <button type="button" class="btn btn--sm btn--icon" data-action="fit" aria-label="Fit graph to view" title="Fit (0)">${icon("maximize", { size: "sm" })}</button>
          <button type="button" class="btn btn--sm btn--icon" data-action="pause" aria-label="Pause layout" title="Pause layout" aria-pressed="false">${icon("play", { size: "sm" })}</button>
          <button type="button" class="btn btn--sm btn--icon" data-action="relayout" aria-label="Re-run layout" title="Re-run layout">${icon("refresh", { size: "sm" })}</button>
        </div>
      </div>
      <div data-slot="cap"></div>
      <div class="v-network-frame">
        <div class="v-network-stage" data-slot="stage"></div>
        <div class="v-network-overlay" data-slot="overlay"><div class="v-network-overlay__box">${spinner("Loading network")}<span>Building network…</span></div></div>
      </div>
      <div class="row-between row-wrap v-network-foot">
        <div class="legend" data-slot="legend" aria-label="Legend"></div>
        <p class="xsmall muted v-network-help">Drag to pan · Ctrl/⌘ + scroll or pinch to zoom (plain scroll once the graph has focus) · drag nodes · focus the graph and use arrow keys, Enter to inspect, Shift+Enter for the profile.</p>
      </div>
      <p class="sr-only" data-slot="summary"></p>
    </div>`
  );
  const gslot = (n) => graphPanel.querySelector(`[data-slot="${n}"]`);

  /* ------------------------------------------------------------------ node / edge presentation */
  function nodeStyle(n) {
    const z = n.is_system ? -1 : ROLE_ORDER[n.node_role] || 0;
    if (n.is_system) return { fill: "--node-system", shape: "diamond", z };
    const hollow = !n.has_statement;
    const ck = n.colour_keys?.[MODE_KEY[state.color]];
    const meta = ck != null ? COLOUR_KEYS[MODE_KEY[state.color]]?.[ck] : null;
    if (meta) return { fill: meta.fill, hollow, z };
    let fill;
    if (state.color === "risk") {
      fill = n.risk_tier === "Critical" ? "--risk-critical" : n.risk_tier === "Suspicious" ? "--risk-suspicious" : n.risk_tier === "Low Risk" ? "--risk-low" : "--node-details";
    } else if (state.color === "statement") {
      fill = n.has_statement ? "--node-statement" : "--node-details";
    } else if (state.color === "betweenness") {
      const bin = state.maxBetweenness > 0 ? Math.min(5, Math.floor((n.betweenness / state.maxBetweenness) * 6)) : 0;
      fill = `--chart-seq-${bin + 2}`;
    } else fill = `--node-${roleMeta(n.node_role).cls}`;
    return { fill, hollow, z };
  }

  const describe = (n) =>
    `${n.label}. ${n.is_system ? "System party" : n.role_label}. ${n.has_statement ? "Statement held" : "Details only"}. ` +
    `Risk ${n.risk_tier}${n.mule_score != null ? ` (score ${num(n.mule_score, 1)})` : ""}. Inflow ${inr(n.total_inflow)}, outflow ${inr(n.total_outflow)}. ` +
    `${n.in_degree} incoming and ${n.out_degree} outgoing counterparties. Press Enter to inspect.`;

  const nodeTip = (n) => html`<div class="strong">${n.label}</div>
    <div>${n.is_system ? "System / generic party" : n.role_label} · ${n.has_statement ? "statement held" : "details only"}</div>
    <div>Risk: ${n.risk_tier}${n.mule_score != null ? ` (${num(n.mule_score, 1)})` : ""}</div>
    <div class="tabular">In ${inr(n.total_inflow)} · Out ${inr(n.total_outflow)}</div>
    <div class="tabular">Betweenness ${num(n.betweenness, 4)} · Degree ${n.in_degree}/${n.out_degree}</div>`;

  const mutualSet = () => new Set((state.graph?.edges || []).map(edgeKey));
  const edgeTip = (e) => {
    const mutual = mutualSet().has(`${e.target}>${e.source}`);
    return html`<div class="strong">${e.source_name} → ${e.target_name}</div>
      <div class="tabular">${inr(e.total_amount)} in ${plural(e.tx_count, "transaction")}</div>
      <div>${(e.channels || []).join(" / ") || "—"} · ${date(e.first_date)}${e.last_date !== e.first_date ? html` → ${date(e.last_date)}` : ""}</div>
      ${mutual ? html`<div>Mutual flow — funds also moved back</div>` : ""}`;
  };

  /* ------------------------------------------------------------------ canvas */
  const graph = createGraphCanvas(gslot("stage"), {
    ariaLabel: "Fund-flow network graph. Use arrow keys to move between entities, Enter to inspect.",
    nodeStyle,
    edgeEmphasis: (e) => {
      const nodes = state.view?.byId;
      if (!nodes) return false;
      const s = nodes.get(e.source);
      const t = nodes.get(e.target);
      return (s && s.node_role === "BRIDGE_CONDUIT") || (t && t.node_role === "BRIDGE_CONDUIT") || (s?.node_role === "PRIMARY_HUB" && t?.node_role === "EXIT_ENDPOINT");
    },
    edgeLabel: (e) => inrCompact(e.total_amount),
    describeNode: describe,
    onHover: (hit, pos) => {
      if (!hit) return hideTip();
      return showTip(hit.kind === "node" ? nodeTip(hit.data) : edgeTip(hit.data), pos.clientX, pos.clientY);
    },
    onSelect: (n, { source }) => {
      hideTip();
      setSelected(n ? n.entity_id : null, { fromGraph: true });
      if (n && source === "pointer" && narrowMQ && narrowMQ.matches) openInspectorDrawer();
    },
    onActivate: (n) => ctx.navigate("profile", { entity: n.entity_id }),
    onEdgeClick: (e) => setSelected(e.source, { center: false }),
    onStatus: (s) => {
      const label = { running: "Laying out…", settled: "Layout settled", paused: "Layout paused", empty: "" }[s] ?? "";
      render(gslot("status"), label);
      const btn = graphPanel.querySelector('[data-action="pause"]');
      const isPaused = s === "paused";
      btn.setAttribute("aria-pressed", isPaused ? "true" : "false");
      btn.setAttribute("aria-label", isPaused ? "Resume layout" : "Pause layout");
      btn.title = isPaused ? "Resume layout" : "Pause layout";
    },
  });
  ctx.onCleanup(() => graph.destroy());

  /* ------------------------------------------------------------------ overlay states */
  const overlay = gslot("overlay");
  function setOverlay(content) {
    if (!content) {
      overlay.hidden = true;
      render(overlay, "");
      return;
    }
    overlay.hidden = false;
    render(overlay, html`<div class="v-network-overlay__box">${content}</div>`);
  }

  /* ------------------------------------------------------------------ view computation (client filters + cap) */
  const filtersActive = () => SERVER_KEYS.some((k) => state.f[k]) || CLIENT_KEYS.some((k) => state.f[k]);

  function computeView() {
    const g = state.graph;
    const all = g.nodes || [];
    const rawById = new Map(all.map((n) => [n.entity_id, n]));
    let edges = g.edges || [];
    let clientFiltered = false;
    let matched = null;

    if (state.conduits) {
      const conduitIds = new Set(all.filter((n) => n.node_role === "BRIDGE_CONDUIT").map((n) => n.entity_id));
      const focus = new Set(conduitIds);
      for (const e of edges) {
        if (conduitIds.has(e.source) && rawById.get(e.target)?.node_role !== "PERIPHERAL_LEAF") focus.add(e.target);
        if (conduitIds.has(e.target) && rawById.get(e.source)?.node_role !== "PERIPHERAL_LEAF") focus.add(e.source);
      }
      edges = edges.filter((e) => focus.has(e.source) && focus.has(e.target));
      clientFiltered = true;
    }
    if (state.ego && state.selected && rawById.has(state.selected)) {
      const id = state.selected;
      const keep = new Set([id]);
      for (const e of edges) {
        if (e.source === id) keep.add(e.target);
        if (e.target === id) keep.add(e.source);
      }
      edges = edges.filter((e) => keep.has(e.source) && keep.has(e.target));
      clientFiltered = true;
    }
    let filtered = all;
    if (clientFiltered) {
      const touched = new Set();
      for (const e of edges) {
        touched.add(e.source);
        touched.add(e.target);
      }
      if (matched) for (const id of matched) touched.add(id);
      if (state.selected) touched.add(state.selected);
      filtered = all.filter((n) => touched.has(n.entity_id));
    }
    // node cap: the server sorts nodes by volume; keep the selection and expanded nodes visible beyond the cap
    let shown = filtered.slice(0, state.cap);
    const extra = new Set(state.expanded);
    if (state.selected) extra.add(state.selected);
    if (extra.size) {
      const have = new Set(shown.map((n) => n.entity_id));
      shown = [...shown, ...filtered.filter((n) => extra.has(n.entity_id) && !have.has(n.entity_id))];
    }
    const shownIds = new Set(shown.map((n) => n.entity_id));
    const filteredIds = new Set(filtered.map((n) => n.entity_id));
    return {
      filtered,
      filteredEdges: edges.filter((e) => filteredIds.has(e.source) && filteredIds.has(e.target)),
      nodes: shown,
      edges: edges.filter((e) => shownIds.has(e.source) && shownIds.has(e.target)),
      byId: rawById,
      matched,
    };
  }

  function rebuild({ keepView = false } = {}) {
    if (!state.graph) return;
    const v = computeView();
    state.view = v;
    state.maxBetweenness = Math.max(0, ...v.filtered.map((n) => n.betweenness || 0));
    graph.setData(
      v.nodes.map((n) => ({ id: n.entity_id, label: n.label, weight: (n.total_inflow || 0) + (n.total_outflow || 0), x: n.x != null ? n.x - 460 : null, y: n.y != null ? n.y - 260 : null, data: n })),
      v.edges.map((e) => ({ id: edgeKey(e), source: e.source, target: e.target, amount: e.total_amount, data: e })),
      { keepView }
    );
    if (state.selected) graph.setSelected(state.selected);
    drawKpis();
    drawCap();
    drawLegend();
    drawSummary();
    tabs.setBadge("entities", int(v.filtered.length));
    tabs.setBadge("flows", int(v.filteredEdges.length));
    entGrid?.setRows(v.filtered);
    flowGrid?.setRows(v.filteredEdges);
    drawInspector();
    if (!v.nodes.length) {
      setOverlay(
        filtersActive() || state.conduits || state.ego
          ? emptyState({ icon: "filter", title: "No flows match these filters", message: "Widen the date window, lower the amount thresholds or clear the bank / conduit filters.", action: { label: "Reset filters", id: "reset" }, compact: true })
          : emptyState({ icon: "network", title: "No inter-entity transfers yet", message: "The graph is built from debits and credits between resolved entities. Upload statements to begin.", action: { label: "Upload statements", href: ctx.href("ingest") }, compact: true })
      );
    } else setOverlay(null);
  }

  /* ------------------------------------------------------------------ KPIs, notes, cap, legend, summary */
  function drawKpis() {
    const s = state.graph?.summary || {};
    const v = state.view;
    render(
      slot("kpis"),
      kpiGrid(
        [
          { label: "Entities", value: int(s.total_nodes), sub: v ? `${int(v.nodes.length)} in view` : "", icon: "users" },
          { label: "Directed flows", value: int(s.total_edges), sub: v ? `${int(v.edges.length)} in view` : "", icon: "swap" },
          // engine rule: details-only parties seen on both sides (or linked to 2+ accounts), plus statement
          // accounts with top-quartile betweenness, so the label must not claim "high betweenness" for all
          { label: "Bridge / conduits", value: int(s.conduit_nodes), tone: s.conduit_nodes ? "warn" : "",
            sub: "two-way / multi-account parties", title: "Counterparties that both send and receive (or touch 2+ statement accounts), plus statement accounts with top-quartile betweenness" },
          { label: "Flow volume", value: inrCompact(s.total_volume), title: inr(s.total_volume), sub: plural(s.total_events || 0, "transfer") },
          { label: "Components", value: int(s.connected_components), sub: "connected groups" },
          { label: "Statement accounts", value: int(s.statement_nodes), sub: `${int(s.details_only_nodes)} details-only · ${int(s.system_nodes)} system` },
        ],
        { compact: true, label: "Network summary" }
      )
    );
    const notes = [];
    if (s.truncated) notes.push(html`The server returned the ${int(s.displayed_nodes)} highest-volume of ${int(s.total_nodes)} entities; centrality was computed on the full graph. Use “Expand neighbours” on an entity, or load more from the server.`);
    if (s.betweenness_sampled) notes.push(html`Betweenness is approximated from ${int(s.betweenness_samples)} sampled sources.`);
    const applied = describeApplied(state.graph?.summary?.filters);
    if (applied.length) notes.push(html`Engine filters applied: ${applied.join(" · ")}. Centrality and roles are computed on this filtered graph.`);
    render(slot("notes"), notes.length ? html`<div class="callout callout--info">${icon("info")}<div>${notes.map((n) => html`<div>${n}</div>`)}</div></div>` : "");
  }

  /** Human text for summary.filters (what the engine actually applied, not what the form says). */
  function describeApplied(f) {
    if (!f) return [];
    const out = [];
    if (f.date_from || f.date_to) out.push(`${f.date_from ? date(f.date_from) : "start"} → ${f.date_to ? date(f.date_to) : "end"}`);
    if (Number(f.min_amount) > 0) out.push(`≥ ${inr(f.min_amount)}`);
    if (f.max_amount != null) out.push(`≤ ${inr(f.max_amount)}`);
    if (f.channel) out.push(`channel ${f.channel}`);
    if (f.keyword) out.push(`keyword “${f.keyword}”`);
    if (Array.isArray(f.ifsc) && f.ifsc.length) out.push(`IFSC ${f.ifsc.join(", ")}`);
    return out;
  }

  function drawCap() {
    const v = state.view;
    const hidden = v.filtered.length - v.nodes.length;
    const s = state.graph?.summary || {};
    const serverHidden = s.truncated ? Math.max(0, (s.total_nodes || 0) - (s.displayed_nodes || 0)) : 0;
    render(
      gslot("cap"),
      hidden <= 0 && serverHidden > 0 && state.serverNodes < SERVER_NODES_MAX
        ? html`<div class="v-network-cap" role="status">
            <span>${icon("info", { size: "sm" })} ${int(serverHidden)} lower-volume entities were not loaded.</span>
            <button type="button" class="btn btn--sm btn--subtle" data-action="server-more">Load ${int(Math.min(serverHidden, Math.min(SERVER_NODES_MAX, state.serverNodes * 2) - state.serverNodes))} more from server</button>
          </div>`
        : hidden > 0
        ? html`<div class="v-network-cap" role="status">
            <span>${icon("info", { size: "sm" })} Showing the ${int(v.nodes.length)} highest-volume of ${int(v.filtered.length)} entities.</span>
            <button type="button" class="btn btn--sm btn--subtle" data-action="more">Show ${int(Math.min(CAP_STEP, hidden))} more</button>
            <button type="button" class="btn btn--sm btn--ghost" data-action="all">Show all</button>
          </div>`
        : state.cap > CAP_STEP && v.filtered.length > CAP_STEP
          ? html`<div class="v-network-cap"><span>${icon("info", { size: "sm" })} All ${int(v.filtered.length)} entities shown.</span>
              <button type="button" class="btn btn--sm btn--ghost" data-action="less">Show top ${CAP_STEP}</button></div>`
          : ""
    );
  }

  function drawLegend() {
    let items;
    const modeKey = MODE_KEY[state.color];
    const served = state.graph?.summary?.colour_by?.[modeKey];
    if (Array.isArray(served) && served.length) {
      // the server's legend for this mode, in its order; counts from the nodes in view
      const counts = new Map();
      for (const n of state.view?.nodes || []) {
        const k = n.colour_keys?.[modeKey];
        counts.set(k, (counts.get(k) || 0) + 1);
      }
      render(
        gslot("legend"),
        html`${served.map((k) => {
          const m = COLOUR_KEYS[modeKey]?.[k] || { sw: "sw-system", label: String(k).replace(/_/g, " ").toLowerCase() };
          return html`<span class="legend__item"><span class="legend__swatch legend__swatch--round ${m.sw}"></span>${m.label}<span class="muted tabular"> (${int(counts.get(k) || 0)})</span></span>`;
        })}
          ${modeKey === "betweenness" ? html`<span class="legend__item muted">Bins are betweenness percentiles computed by the engine</span>` : ""}
          <span class="legend__item"><span class="v-network-key v-network-key--ring" aria-hidden="true"></span>Ring = details only</span>
          <span class="legend__item"><span class="v-network-key v-network-key--diamond" aria-hidden="true"></span>System / generic party</span>
          <span class="legend__item"><span class="v-network-key v-network-key--edge" aria-hidden="true"></span>Width ∝ √ amount</span>`
      );
      return;
    }
    if (state.color === "risk") {
      items = [["critical", "Critical (80–100)"], ["suspicious", "Suspicious (50–79)"], ["low", "Low risk"], ["details", "No statement (not scored)"]];
    } else if (state.color === "statement") {
      items = [["statement", "Statement held"], ["details", "Details only"]];
    } else if (state.color === "betweenness") {
      items = null;
    } else items = Object.values(GRAPH_ROLES).map((r) => [r.cls, r.label]);
    render(
      gslot("legend"),
      html`${items
        ? items.map(([cls, label]) => html`<span class="legend__item"><span class="legend__swatch legend__swatch--round sw-${cls}"></span>${label}</span>`)
        : html`<span class="legend__item">Low<span class="v-network-ramp" aria-hidden="true">${[2, 3, 4, 5, 6, 7].map((i) => html`<span class="v-network-ramp__step v-network-seq-${i}"></span>`)}</span>High betweenness</span>`}
        <span class="legend__item"><span class="v-network-key v-network-key--ring" aria-hidden="true"></span>Ring = details only</span>
        <span class="legend__item"><span class="v-network-key v-network-key--diamond" aria-hidden="true"></span>System / generic party</span>
        <span class="legend__item"><span class="v-network-key v-network-key--edge" aria-hidden="true"></span>Width ∝ √ amount</span>`
    );
  }

  function drawSummary() {
    const v = state.view;
    const top = [...v.nodes].sort((a, b) => b.betweenness - a.betweenness).slice(0, 3).filter((n) => n.betweenness > 0);
    const conduits = v.nodes.filter((n) => n.node_role === "BRIDGE_CONDUIT").length;
    render(
      gslot("summary"),
      `Graph of ${v.nodes.length} entities and ${v.edges.length} directed flows (${conduits} bridge or conduit accounts). ` +
        (top.length ? `Most central intermediaries: ${top.map((n) => n.label).join(", ")}. ` : "") +
        "The Entities and Flows tabs list the same data as tables."
    );
  }

  /* ------------------------------------------------------------------ inspector */
  const inspector = slot("inspector");
  let drawer = null;

  function counterparties(id) {
    const edges = state.graph?.edges || [];
    const keys = mutualSet();
    const byId = state.view?.byId || new Map();
    const inn = edges.filter((e) => e.target === id).sort((a, b) => b.total_amount - a.total_amount);
    const out = edges.filter((e) => e.source === id).sort((a, b) => b.total_amount - a.total_amount);
    const row = (e, other, name) => {
      const mutual = keys.has(`${e.target}>${e.source}`);
      const o = byId.get(other);
      return html`<li class="v-network-cp">
        <button type="button" class="v-network-cp__name" data-action="select" data-id="${other}" title="${name}">
          <span class="legend__swatch legend__swatch--round sw-${o ? (o.is_system ? "system" : roleMeta(o.node_role).cls) : "leaf"}"></span><span class="truncate">${name}</span></button>
        <span class="num tabular" title="${inr(e.total_amount)}">${inrCompact(e.total_amount)}</span>
        <span class="xsmall muted num">${int(e.tx_count)}×</span>
        ${mutual ? html`<a class="v-network-cp__pair" href="${ctx.href("bidirectional", { a: id, b: other })}" title="Mutual flow — open pair analysis" aria-label="Pair analysis with ${name}">⇄</a>` : html`<span></span>`}
      </li>`;
    };
    const list = (title, arr, pick) => html`<div class="stack-sm"><div class="caps">${title} <span class="muted">(${int(arr.length)})</span></div>
      ${arr.length ? html`<ul class="v-network-cplist">${arr.slice(0, 6).map((e) => pick(e))}</ul>${arr.length > 6 ? html`<div class="xsmall muted">+${int(arr.length - 6)} more in the Flows tab</div>` : ""}` : html`<div class="xsmall muted">None</div>`}</div>`;
    return html`${list("Received from", inn, (e) => row(e, e.source, e.source_name))}${list("Sent to", out, (e) => row(e, e.target, e.target_name))}`;
  }

  function inspectorBody(n) {
    const net = (n.total_inflow || 0) - (n.total_outflow || 0);
    const banks = bankOf(n);
    const inView = state.view?.nodes.some((x) => x.entity_id === n.entity_id);
    const mutual = (state.graph?.edges || []).find((e) => e.source === n.entity_id && mutualSet().has(`${e.target}>${e.source}`));
    return html`<div class="stack">
      <div class="row row-wrap">${roleBadge(n)}${statusBadge(n)}${riskBadge(n)}</div>
      ${!inView ? html`<div class="callout callout--warn">${icon("filter")}<div>Hidden by the current filters.</div></div>` : ""}
      ${n.mule_score != null
        ? html`<div class="stack-sm"><div class="row-between xsmall"><span class="muted">Mule score</span><span class="num strong">${num(n.mule_score, 1)} / 100</span></div>
            <div class="meter meter--${tierClass(n.risk_tier)}" data-meter="${Math.max(0, Math.min(100, Number(n.mule_score) || 0))}" role="img" aria-label="Mule score ${num(n.mule_score, 1)} of 100"><div class="meter__fill"></div></div></div>`
        : ""}
      <dl class="kv v-network-kv">
        <dt>Inflow</dt><dd class="num is-credit" title="${inr(n.total_inflow)}">${inr(n.total_inflow)}</dd>
        <dt>Outflow</dt><dd class="num is-debit" title="${inr(n.total_outflow)}">${inr(n.total_outflow)}</dd>
        <dt>Net position</dt><dd class="num ${signClass(net)}">${inr(net, { sign: true })}</dd>
        <dt>Counterparties</dt><dd class="num">${int(n.in_degree)} in · ${int(n.out_degree)} out</dd>
        <dt>Betweenness</dt><dd class="num">${num(n.betweenness, 4)}</dd>
        <dt>PageRank</dt><dd class="num">${num(n.pagerank, 4)}</dd>
        <dt>Component</dt><dd class="num">#${n.component_id}</dd>
        ${(n.accounts || []).length ? html`<dt>Accounts</dt><dd class="mono small">${n.accounts.join(", ")}</dd>` : ""}
        ${banks.length ? html`<dt>Bank / IFSC</dt><dd class="small">${banks.map((s) => html`<div>${s.bank_name || s.bank_code} · <span class="mono">${s.ifsc_code || "—"}</span></div>`)}</dd>` : ""}
        ${(n.aliases || []).length > 1 ? html`<dt>Aliases</dt><dd class="small">${n.aliases.join(" · ")}</dd>` : ""}
      </dl>
      <div class="row row-wrap">
        <a class="btn btn--sm btn--primary" href="${ctx.href("profile", { entity: n.entity_id })}">${icon("user", { size: "sm" })}Open profile</a>
        <a class="btn btn--sm" href="${ctx.href("transactions", { q: n.label })}">${icon("list", { size: "sm" })}Transactions</a>
        ${mutual ? html`<a class="btn btn--sm" href="${ctx.href("bidirectional", { a: n.entity_id, b: mutual.target })}">${icon("swap", { size: "sm" })}Pair analysis</a>` : ""}
      </div>
      <div class="row row-wrap">
        <button type="button" class="chip" data-action="ego" aria-pressed="${state.ego ? "true" : "false"}">Isolate neighbourhood</button>
        <button type="button" class="chip" data-action="center">${icon("eye", { size: "sm" })}Centre in graph</button>
        ${canExpand(n) ? html`<button type="button" class="chip" data-action="expand" data-id="${n.entity_id}" title="Load this entity's ${EXPAND_LIMIT} heaviest flows and their counterparties">${icon("plus", { size: "sm" })}Expand neighbours</button>` : ""}
      </div>
      ${counterparties(n.entity_id)}
    </div>`;
  }

  function emptyInspector() {
    const ki = state.graph?.key_intermediaries || [];
    const max = Math.max(0, ...ki.map((n) => n.betweenness || 0));
    return html`<div class="card__head"><div><h2 class="card__title">Key intermediaries</h2>
        <p class="card__sub">Highest betweenness — accounts most often on the path between others.</p></div></div>
      <div class="card__body stack">
        ${ki.length
          ? html`<ol class="v-network-ki">${ki.map((n) => html`<li>
              <button type="button" class="v-network-ki__btn" data-action="select" data-id="${n.entity_id}">
                <span class="row-between"><span class="row min-w-0"><span class="legend__swatch legend__swatch--round sw-${roleMeta(n.node_role).cls}"></span><span class="truncate strong">${n.label}</span></span>
                  <span class="num xsmall">${num(n.betweenness, 3)}</span></span>
                <span class="meter" data-meter="${max ? ((n.betweenness / max) * 100).toFixed(1) : 0}"><span class="meter__fill"></span></span>
              </button></li>`)}</ol>`
          : html`<p class="small muted">${state.graph ? "No intermediaries: no entity sits between two others." : "Loading…"}</p>`}
        <p class="xsmall muted">Select a node in the graph or a row in the tables to inspect an entity.</p>
      </div>`;
  }

  const applyMeters = (root) => root.querySelectorAll("[data-meter]").forEach((m) => m.style.setProperty("--value", `${m.dataset.meter}%`));

  function drawInspector() {
    const n = state.selected ? state.view?.byId.get(state.selected) || state.graph?.nodes.find((x) => x.entity_id === state.selected) : null;
    if (!n) {
      render(inspector, emptyInspector());
    } else {
      render(
        inspector,
        html`<div class="card__head"><div class="min-w-0"><h2 class="card__title v-network-insp-title" title="${n.label}">${n.label}</h2>
            <p class="card__sub">${n.is_system ? "System / generic party" : n.role_label}</p></div>
            <button type="button" class="btn btn--ghost btn--icon btn--sm" data-action="deselect" aria-label="Clear selection">${icon("x", { size: "sm" })}</button></div>
          <div class="card__body">${inspectorBody(n)}</div>`
      );
    }
    applyMeters(inspector);
    if (drawer) {
      if (n) {
        drawer.setTitle(n.label);
        drawer.setBody(inspectorBody(n));
        applyMeters(drawer.body);
      } else drawer.close();
    }
  }

  function openInspectorDrawer() {
    const n = state.view?.byId.get(state.selected);
    if (!n) return;
    drawer = openDrawer({ title: n.label, body: inspectorBody(n), width: 420, onClose: () => { drawer = null; } });
    applyMeters(drawer.body);
    delegate(drawer.body, "click", "[data-action]", (e, b) => {
      if (b.dataset.action === "select" || b.dataset.action === "center") drawer?.close();
      onAction(e, b);
    });
    delegate(drawer.body, "click", "a[href]", () => drawer?.close());
  }
  ctx.onCleanup(() => drawer?.close());

  function setSelected(id, { fromGraph = false, center = true } = {}) {
    const changed = (id || null) !== state.selected;
    state.selected = id || null;
    const rawNode = id ? state.view?.byId.get(id) : null;
    const egoWas = state.ego;
    if (!state.selected) state.ego = false;
    ctx.setParams({ entity: state.selected, ego: state.ego ? "1" : null });
    ctx.setCrumbs(rawNode ? [{ label: rawNode.label }] : []);
    if (egoWas && changed) {
      rebuild({ keepView: !state.ego });
    } else if (!fromGraph) {
      if (id && !graph.hasNode(id) && state.view?.filtered.some((n) => n.entity_id === id)) rebuild({ keepView: true });
      graph.setSelected(id, { center });
      drawInspector();
    } else drawInspector();
  }

  /* ------------------------------------------------------------------ tables (accessible alternative) */
  function ensureTable(id) {
    if (id === "entities" && !entGrid) {
      entGrid = createDataGrid(tabs.panel("entities"), {
        ariaLabel: "Entities and centrality",
        rowKey: "entity_id",
        rows: state.view?.filtered || [],
        height: "min(62vh, 600px)",
        rowHeight: 44,
        countNoun: ["entity", "entities"],
        initialSort: { key: "betweenness", order: "desc" },
        filters: [{ key: "q", type: "search", label: "Search entities", placeholder: "Name, alias or account" }],
        clientFilter: (r, f) => {
          const q = String(f.q || "").trim().toLowerCase();
          return !q || [r.label, ...(r.aliases || []), ...(r.accounts || [])].some((s) => String(s).toLowerCase().includes(q));
        },
        onRowClick: (r) => setSelected(r.entity_id, { center: true }),
        emptyState: { icon: "users", title: "No entities", message: "No entities match the current graph filters." },
        columns: [
          { key: "label", label: "Entity", width: "minmax(200px, 2fr)", sortable: true,
            render: (n) => html`<span class="strong">${n.label}</span><span class="dg__sub mono">${(n.accounts || []).join(", ") || "—"}</span>` },
          { key: "node_role", label: "Role", width: "170px", sortable: true, sortValue: (n) => ROLE_ORDER[n.node_role] ?? 0, render: (n) => roleBadge(n) },
          { key: "has_statement", label: "Statement", width: "130px", sortable: true, sortValue: (n) => (n.is_system ? 0 : n.has_statement ? 2 : 1), render: (n) => statusBadge(n) },
          { key: "risk_tier", label: "Risk", width: "150px", sortable: true, sortValue: (n) => n.mule_score ?? -1, render: (n) => riskBadge(n) },
          { key: "total_inflow", label: "Inflow", width: "130px", align: "right", sortable: true, defaultOrder: "desc", className: "is-credit", title: (n) => inr(n.total_inflow), format: (v) => inr(v, { decimals: 0 }) },
          { key: "total_outflow", label: "Outflow", width: "130px", align: "right", sortable: true, defaultOrder: "desc", className: "is-debit", title: (n) => inr(n.total_outflow), format: (v) => inr(v, { decimals: 0 }) },
          { key: "in_degree", label: "In / out", width: "90px", align: "right", sortable: true, sortValue: (n) => n.in_degree + n.out_degree, render: (n) => html`${int(n.in_degree)} / ${int(n.out_degree)}` },
          { key: "betweenness", label: "Betweenness", width: "110px", align: "right", sortable: true, defaultOrder: "desc", format: (v) => num(v, 4) },
          { key: "pagerank", label: "PageRank", width: "100px", align: "right", sortable: true, defaultOrder: "desc", format: (v) => num(v, 4) },
        ],
      });
      ctx.onCleanup(() => entGrid.destroy());
    }
    if (id === "flows" && !flowGrid) {
      flowGrid = createDataGrid(tabs.panel("flows"), {
        ariaLabel: "Directed flows",
        rowKey: (e) => edgeKey(e),
        rows: state.view?.filteredEdges || [],
        height: "min(62vh, 600px)",
        rowHeight: 44,
        countNoun: ["flow", "flows"],
        initialSort: { key: "total_amount", order: "desc" },
        filters: [{ key: "q", type: "search", label: "Search flows", placeholder: "Sender or beneficiary" }],
        clientFilter: (e, f) => {
          const q = String(f.q || "").trim().toLowerCase();
          return !q || `${e.source_name} ${e.target_name}`.toLowerCase().includes(q);
        },
        onRowClick: (e) => setSelected(e.source, { center: true }),
        emptyState: { icon: "swap", title: "No flows", message: "No flows match the current graph filters." },
        columns: [
          { key: "source_name", label: "From", width: "minmax(160px, 1.5fr)", sortable: true, render: (e) => html`<span class="strong">${e.source_name}</span>` },
          { key: "target_name", label: "To", width: "minmax(160px, 1.5fr)", sortable: true, render: (e) => html`${e.target_name}` },
          { key: "total_amount", label: "Total", width: "140px", align: "right", sortable: true, defaultOrder: "desc", title: (e) => inr(e.total_amount), format: (v) => inr(v) },
          { key: "tx_count", label: "Txns", width: "70px", align: "right", sortable: true, defaultOrder: "desc", format: (v) => int(v) },
          { key: "channels", label: "Channels", width: "130px", sortValue: (e) => (e.channels || []).join(","), sortable: true, render: (e) => html`${(e.channels || []).map((c) => html`<span class="badge badge--neutral">${c}</span> `)}` },
          { key: "first_date", label: "First", width: "110px", sortable: true, format: (v) => date(v) },
          { key: "last_date", label: "Last", width: "110px", sortable: true, format: (v) => date(v) },
          { key: "mutual", label: "Mutual", width: "110px", sortable: true, sortValue: (e) => (mutualSet().has(`${e.target}>${e.source}`) ? 1 : 0),
            render: (e) => (mutualSet().has(`${e.target}>${e.source}`)
              ? html`<a class="btn btn--sm btn--ghost" href="${ctx.href("bidirectional", { a: e.source, b: e.target })}">⇄ Pair</a>`
              : html`<span class="muted">—</span>`) },
        ],
      });
      ctx.onCleanup(() => flowGrid.destroy());
    }
  }

  /* ------------------------------------------------------------------ loading */
  let loadCtl = null;
  ctx.onCleanup(() => loadCtl?.abort());
  function filterParams() {
    const f = state.f;
    return {
      date_from: f.date_from, date_to: f.date_to, min_amount: posNum(f.amount_min) || "", max_amount: posNum(f.amount_max) || "",
      channel: f.channel, keyword: f.q.trim(), ifsc: ifscList(f.ifsc).join(","),
    };
  }
  function serverParams() {
    return { ...filterParams(), max_nodes: state.serverNodes };
  }

  /** True when the loaded graph may be missing some of this node's flows (server node cap). */
  function canExpand(n) {
    if (!state.graph?.summary?.truncated) return false;
    const known = (state.graph.edges || []).filter((e) => e.source === n.entity_id || e.target === n.entity_id).length;
    return known < (n.in_degree || 0) + (n.out_degree || 0);
  }

  /** Merge GET trace/graph/expand (one node's heaviest edges + neighbours) into the loaded graph. */
  async function expandNode(id, btn) {
    if (!id || !state.graph) return;
    if (btn) btn.disabled = true;
    try {
      const res = await ctx.api.get("trace/graph/expand", { ...filterParams(), entity_id: id, limit: EXPAND_LIMIT }, { signal: ctx.signal });
      if (!ctx.isCurrent() || !state.graph) return;
      const nodes = state.graph.nodes || [];
      const have = new Set(nodes.map((n) => n.entity_id));
      const added = (res.nodes || []).filter((n) => !have.has(n.entity_id));
      const edgeSet = new Set((state.graph.edges || []).map(edgeKey));
      const newEdges = (res.edges || []).filter((e) => !edgeSet.has(edgeKey(e)));
      state.graph = { ...state.graph, nodes: [...nodes, ...added], edges: [...(state.graph.edges || []), ...newEdges] };
      (res.nodes || []).forEach((n) => state.expanded.add(n.entity_id));
      rebuild({ keepView: true });
      ctx.toast?.info?.(`Added ${plural(added.length, "entity", "entities")} and ${plural(newEdges.length, "flow")}${res.truncated ? ` (heaviest ${int(res.limit)} of ${int(res.total_edges)})` : ""}.`);
    } catch (err) {
      if (!isAbort(err) && ctx.isCurrent()) ctx.toast?.error?.(err, { title: "Could not expand this entity" });
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  async function load({ keepView = false } = {}) {
    loadCtl?.abort();
    const c = new AbortController();
    loadCtl = c;
    const unlink = linkAbort(ctx.signal, c);
    state.loading = true;
    if (!state.graph) setOverlay(html`${spinner("Loading network")}<span>Building network…</span>`);
    else overlay.classList.add("is-busy");
    el.querySelector(".v-network-frame").setAttribute("aria-busy", "true");
    try {
      const g = await ctx.api.get("trace/graph", serverParams(), { signal: c.signal });
      if (!ctx.isCurrent() || loadCtl !== c) return;
      state.graph = g;
      state.error = null;
      rebuild({ keepView });
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent()) return;
      state.error = err;
      if (!state.graph) {
        render(slot("kpis"), "");
        setOverlay(errorState(err, { retryId: "retry", title: "Could not build the network", compact: true }));
      } else ctx.toast.error(err, { title: "Could not refresh the network" });
    } finally {
      unlink();
      if (loadCtl === c) {
        state.loading = false;
        overlay.classList.remove("is-busy");
        el.querySelector(".v-network-frame")?.removeAttribute("aria-busy");
      }
    }
  }

  /* ------------------------------------------------------------------ time brush (statement activity) */
  const brush = createTimeBrush(slot("brush"), {
    ariaLabel: "Date window over daily statement volume",
    formatDate: (iso) => date(iso),
    onChange: (v, { final }) => {
      form.elements.namedItem("date_from").value = v ? v[0] : "";
      form.elements.namedItem("date_to").value = v ? v[1] : "";
      if (final) applyFilters({ date_from: v ? v[0] : "", date_to: v ? v[1] : "" });
    },
  });
  ctx.onCleanup(() => brush.destroy());
  let actCtl = null;
  ctx.onCleanup(() => actCtl?.abort());
  async function loadActivity() {
    actCtl?.abort();
    const c = new AbortController();
    actCtl = c;
    const unlink = linkAbort(ctx.signal, c);
    const stmts = ctx.store.statements();
    let from = stmts.map((s) => s.period_start).filter(Boolean).sort()[0] || "";
    let to = stmts.map((s) => s.period_end).filter(Boolean).sort().pop() || "";
    const daily = new Map();
    let sampled = false;
    try {
      for (let page = 0; page < ACTIVITY_PAGES; page += 1) {
        const r = await ctx.api.get("transactions", { offset: page * ACTIVITY_PAGE, limit: ACTIVITY_PAGE, sort: "txn_date", order: "asc", include_ignored: "false" }, { signal: c.signal });
        if (!ctx.isCurrent() || actCtl !== c) return;
        for (const t of r.transactions || []) {
          if (!t.txn_date) continue;
          daily.set(t.txn_date, (daily.get(t.txn_date) || 0) + Math.max(Number(t.debit) || 0, Number(t.credit) || 0));
        }
        if (!r.has_more) break;
        if (page === ACTIVITY_PAGES - 1) sampled = true;
      }
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent()) return;
      render(slot("brush-note"), "(activity unavailable)");
    } finally {
      unlink();
    }
    const days = [...daily.keys()].sort();
    if (days.length) {
      if (!from || days[0] < from) from = days[0];
      if (!to || days[days.length - 1] > to) to = days[days.length - 1];
    }
    if (!from || !to) {
      render(slot("brush-note"), "— no dated activity yet");
      return;
    }
    render(slot("brush-note"), html`· ${date(from)} → ${date(to)}${sampled ? html` · first ${int(ACTIVITY_PAGES * ACTIVITY_PAGE)} rows` : ""}`);
    brush.setData({ from, to, daily });
    brush.setValue(state.f.date_from || state.f.date_to ? [state.f.date_from, state.f.date_to] : null);
    for (const k of ["date_from", "date_to"]) {
      const input = form.elements.namedItem(k);
      input.min = from;
      input.max = to;
    }
  }

  /* ------------------------------------------------------------------ filter application */
  function applyFilters(patch) {
    const next = { ...state.f, ...patch };
    if (next.date_from && next.date_to && next.date_from > next.date_to) [next.date_from, next.date_to] = [next.date_to, next.date_from];
    const serverChanged = SERVER_KEYS.some((k) => (next[k] || "") !== (state.f[k] || ""));
    const clientChanged = CLIENT_KEYS.some((k) => (next[k] || "") !== (state.f[k] || ""));
    state.f = next;
    ctx.setParams(Object.fromEntries([...SERVER_KEYS, ...CLIENT_KEYS].map((k) => [k, next[k] || null])));
    if (serverChanged) state.expanded.clear(); // expansions belong to the previous filter set
    if (serverChanged) load();
    else if (clientChanged) rebuild();
  }

  function readForm() {
    const v = {};
    for (const k of [...SERVER_KEYS, ...CLIENT_KEYS]) {
      const input = form.elements.namedItem(k);
      v[k] = input ? String(input.value || "").trim() : "";
    }
    v.amount_min = posNum(v.amount_min) ? String(posNum(v.amount_min)) : "";
    v.amount_max = posNum(v.amount_max) ? String(posNum(v.amount_max)) : "";
    v.date_from = isoOk(v.date_from);
    v.date_to = isoOk(v.date_to);
    v.ifsc = ifscList(v.ifsc).join(",");
    return v;
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const v = readForm();
    brush.setValue(v.date_from || v.date_to ? [v.date_from, v.date_to] : null);
    applyFilters(v);
  });
  form.addEventListener("change", (e) => {
    const name = e.target?.name;
    if (!name) return;
    if (name === "date_from" || name === "date_to") {
      const v = readForm();
      brush.setValue(v.date_from || v.date_to ? [v.date_from, v.date_to] : null);
    }
    applyFilters({ [name]: readForm()[name] });
  });

  function resetFilters() {
    state.f = { date_from: "", date_to: "", amount_min: "", channel: "", q: "", amount_max: "", ifsc: "" };
    state.expanded.clear();
    state.serverNodes = SERVER_NODES;
    state.conduits = false;
    state.ego = false;
    state.cap = CAP_STEP;
    syncForm();
    brush.setValue(null);
    graphPanel.querySelector('[data-action="conduits"]').setAttribute("aria-pressed", "false");
    ctx.setParams(Object.fromEntries([...SERVER_KEYS, ...CLIENT_KEYS, "conduits", "ego", "cap"].map((k) => [k, null])));
    load();
  }

  function setColor(mode) {
    if (!COLOR_MODES.some((m) => m.id === mode)) return;
    state.color = mode;
    graphPanel.querySelectorAll("[data-color]").forEach((b) => b.setAttribute("aria-checked", b.dataset.color === mode ? "true" : "false"));
    ctx.setParams({ color: mode === "role" ? null : mode });
    graph.restyle();
    drawLegend();
  }

  function setConduits(on) {
    state.conduits = on;
    graphPanel.querySelector('[data-action="conduits"]').setAttribute("aria-pressed", on ? "true" : "false");
    ctx.setParams({ conduits: on ? "1" : null });
    rebuild();
  }

  async function downloadPng(btn) {
    await ctx.withBusy(btn, async () => {
      if (!state.view?.nodes.length) throw new Error("Nothing to export — the graph is empty.");
      const blob = await graph.toBlob();
      if (!blob) throw new Error("The browser could not render the image.");
      saveBlob(blob, `${ctx.caseId}-network.png`);
      return true;
    }, { success: "Graph image saved", error: "Could not export the graph" });
  }

  /* ------------------------------------------------------------------ actions (delegated) */
  function onAction(e, b) {
    const a = b.dataset.action;
    switch (a) {
      case "refresh":
        load({ keepView: true });
        loadActivity();
        break;
      case "retry":
        load();
        break;
      case "reset":
        resetFilters();
        break;
      case "clear-dates":
        brush.setValue(null);
        form.elements.namedItem("date_from").value = "";
        form.elements.namedItem("date_to").value = "";
        applyFilters({ date_from: "", date_to: "" });
        break;
      case "png":
        downloadPng(b);
        break;
      case "conduits":
        setConduits(!state.conduits);
        break;
      case "zoom-in":
        graph.zoomBy(1.25);
        break;
      case "zoom-out":
        graph.zoomBy(0.8);
        break;
      case "fit":
        graph.fit();
        break;
      case "pause":
        graph.setPaused(b.getAttribute("aria-pressed") !== "true");
        break;
      case "relayout":
        graph.setPaused(false);
        graph.reheat();
        break;
      case "server-more":
        state.serverNodes = Math.min(SERVER_NODES_MAX, state.serverNodes * 2);
        state.cap = Math.max(state.cap, state.view?.filtered.length || 0);
        load({ keepView: true });
        break;
      case "expand":
        expandNode(b.dataset.id, b);
        break;
      case "more":
        state.cap += CAP_STEP;
        ctx.setParams({ cap: String(state.cap) });
        rebuild({ keepView: true });
        break;
      case "all":
        state.cap = Math.max(state.cap, state.view?.filtered.length || 0);
        ctx.setParams({ cap: String(state.cap) });
        rebuild();
        break;
      case "less":
        state.cap = CAP_STEP;
        ctx.setParams({ cap: null });
        rebuild();
        break;
      case "select":
        setSelected(b.dataset.id, { center: true });
        break;
      case "deselect":
        setSelected(null);
        break;
      case "center":
        if (state.selected) {
          tabs.select("graph");
          graph.centerOn(state.selected);
          graph.focusNode(state.selected);
        }
        break;
      case "ego":
        state.ego = !state.ego;
        ctx.setParams({ ego: state.ego ? "1" : null });
        rebuild();
        break;
      default:
    }
  }
  ctx.onCleanup(delegate(el, "click", "[data-action]", onAction));
  ctx.onCleanup(delegate(el, "click", "[data-color]", (e, b) => setColor(b.dataset.color)));
  ctx.onCleanup(delegate(el, "keydown", '[role="radiogroup"] [data-color]', (e) => {
    if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) return;
    e.preventDefault();
    const i = COLOR_MODES.findIndex((m) => m.id === state.color);
    const d = e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 1;
    const next = COLOR_MODES[(i + d + COLOR_MODES.length) % COLOR_MODES.length].id;
    setColor(next);
    graphPanel.querySelector(`[data-color="${next}"]`)?.focus();
  }));
  // roving tabindex for the colour radiogroup
  const syncRoving = () => graphPanel.querySelectorAll("[data-color]").forEach((b) => { b.tabIndex = b.dataset.color === state.color ? 0 : -1; });
  syncRoving();
  ctx.onCleanup(delegate(el, "click", "[data-color]", syncRoving));
  ctx.onCleanup(delegate(el, "keyup", "[data-color]", syncRoving));

  /* ------------------------------------------------------------------ palette commands */
  ctx.registerCommands([
    { id: "network.fit", title: "Fit network graph to view", group: "Network", icon: "maximize", run: () => { tabs.select("graph"); graph.fit(); } },
    ...COLOR_MODES.map((m) => ({ id: `network.colour.${m.id}`, title: `Colour network by ${m.label.toLowerCase()}`, group: "Network", icon: "network", keywords: ["color", "colour"], run: () => setColor(m.id) })),
    { id: "network.conduits", title: "Toggle conduit focus", group: "Network", icon: "filter", run: () => setConduits(!state.conduits) },
    { id: "network.reset", title: "Reset network filters", group: "Network", icon: "refresh", run: resetFilters },
    { id: "network.entities", title: "Show network entities table", group: "Network", icon: "table", run: () => tabs.select("entities") },
    { id: "network.flows", title: "Show network flows table", group: "Network", icon: "swap", run: () => tabs.select("flows") },
    { id: "network.png", title: "Download network graph as PNG", group: "Network", icon: "download", run: () => downloadPng(null) },
  ]);

  /* ------------------------------------------------------------------ data refresh after mutations */
  ctx.onData([TOPICS.analytics, TOPICS.entities], () => {
    load({ keepView: true });
  });
  ctx.onData([TOPICS.statements], () => {
    indexStatements();
    loadActivity();
  });

  /* ------------------------------------------------------------------ initial */
  indexStatements();
  syncForm();
  ensureTable(tabs.selected);
  drawInspector();
  load();
  loadActivity();

  return {
    update(params) {
      const next = {
        date_from: isoOk(params.date_from),
        date_to: isoOk(params.date_to),
        amount_min: posNum(params.amount_min) ? String(posNum(params.amount_min)) : "",
        channel: String(params.channel || "").toUpperCase(),
        q: params.q || "",
        amount_max: posNum(params.amount_max) ? String(posNum(params.amount_max)) : "",
        ifsc: ifscList(params.ifsc).join(","),
      };
      const serverChanged = SERVER_KEYS.some((k) => next[k] !== state.f[k]);
      let clientChanged = CLIENT_KEYS.some((k) => next[k] !== state.f[k]);
      state.f = next;
      syncForm();
      brush.setValue(next.date_from || next.date_to ? [next.date_from, next.date_to] : null);
      const conduits = params.conduits === "1";
      const ego = params.ego === "1";
      const cap = Math.max(CAP_STEP, Number(params.cap) || CAP_STEP);
      if (conduits !== state.conduits || ego !== state.ego || cap !== state.cap) clientChanged = true;
      state.conduits = conduits;
      state.ego = ego;
      state.cap = cap;
      graphPanel.querySelector('[data-action="conduits"]').setAttribute("aria-pressed", conduits ? "true" : "false");
      if (params.color !== undefined || state.color !== "role") {
        const mode = COLOR_MODES.some((m) => m.id === params.color) ? params.color : "role";
        if (mode !== state.color) {
          state.color = mode;
          graphPanel.querySelectorAll("[data-color]").forEach((b) => b.setAttribute("aria-checked", b.dataset.color === mode ? "true" : "false"));
          syncRoving();
          graph.restyle();
          drawLegend();
        }
      }
      const sel = params.entity || null;
      const selChanged = sel !== state.selected;
      state.selected = sel;
      if (serverChanged) load();
      else if (clientChanged) rebuild();
      else if (selChanged) {
        graph.setSelected(sel, { center: true });
        drawInspector();
      }
      const n = sel ? state.view?.byId.get(sel) : null;
      ctx.setCrumbs(n ? [{ label: n.label }] : []);
      const tab = ["graph", "entities", "flows"].includes(params.tab) ? params.tab : "graph";
      if (tab !== tabs.selected) tabs.select(tab);
    },
  };
}
