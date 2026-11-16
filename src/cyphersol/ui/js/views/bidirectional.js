/* CypherTrace bidirectional flow (#/case/{id}/bidirectional?a=&b=&rapid_h=&rt_days=&tol=&date_from=&date_to=)
 *
 * GET trace/bidirectional?a&b&rapid_window_hours&round_trip_window_hours&amount_tolerance&date_from&date_to ->
 * { pairs: [ranked mutual pairs], selected_detail }. The detail (P1, TG-05/06/18) carries:
 *   - round_trips[]: amount-matched A→B→A (anchor "A") and B→A→B (anchor "B") pairs within the round-trip
 *     window and tolerance, with turnaround; round_trip_stats{anchor_a, anchor_b, direction_flips} min/median/max;
 *   - min/median/max_turnaround_hours, direction_reversal_count and rapid_reversal_count (direction flips);
 *   - ledger_a_to_b / ledger_b_to_a with each side's running cumulative, cumulative_series (gross per side + net)
 *     and the chronological transactions with direction_code.
 * Mirrored legs (A's debit + B's credit of one transfer) are de-duplicated by the server (UTR, then amount ±2 d).
 * Without a and b the server analyses the top-ranked pair; the view then writes a/b into the URL.
 * Layout: pair picker + analysis parameters, ranked pair list, KPIs, a timeline (A→B bars up, B→A bars down,
 * arcs joining each amount-matched round trip) over the server's cumulative curves, the round-trip table, the
 * dual ledger (one table per direction) and the chronological ledger (the chart's text alternative).
 */
import { isAbort, linkAbort } from "../core/api.js";
import { delegate, html, render, uid } from "../core/dom.js";
import { clockTime, date, hours, inr, inrCompact, int, num, pct, plural, signClass, truncate } from "../core/format.js";
import { TOPICS } from "../core/store.js";
import { createDataGrid } from "../components/datagrid.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { emptyState, errorState, pageHead, skeleton } from "../components/states.js";

export const styles = ["views/bidirectional.css"];

/** Defaults mirror trace/bidirectional.py (DEFAULT_RAPID_WINDOW_HOURS, 30-day round-trip window, 10 %). */
const PARAM_DEFAULTS = { rapid_h: 48, rt_days: 30, tol: 10 };
const PARAM_LIMITS = { rapid_h: [1, 8760], rt_days: [1, 366], tol: [0, 100] };
const H = 352;

const tsOf = (t) => {
  const v = Date.parse(t.timestamp ? String(t.timestamp).replace("Z", "") : `${t.date}T12:00:00`);
  return Number.isFinite(v) ? v : Date.parse(`${t.date}T12:00:00`);
};
const timeOf = (t) => clockTime(t.timestamp);
const isoOk = (v) => (/^\d{4}-\d{2}-\d{2}$/.test(String(v || "")) ? String(v) : "");
const numParam = (v, key) => {
  const n = Number(v);
  const [lo, hi] = PARAM_LIMITS[key];
  return v != null && v !== "" && Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : PARAM_DEFAULTS[key];
};
function readParams(p) {
  return {
    rapid_h: numParam(p.rapid_h, "rapid_h"),
    rt_days: numParam(p.rt_days, "rt_days"),
    tol: numParam(p.tol, "tol"),
    date_from: isoOk(p.date_from),
    date_to: isoOk(p.date_to),
  };
}

/** Chronological rows with the server's direction, the turnaround since the previous direction flip and the
 * amount-matched round trip (if any) each transfer belongs to. */
function enrich(d, rapidH) {
  const trips = new Map();
  (d.round_trips || []).forEach((tr, k) => {
    trips.set(tr.out_tx_id, { tr, k, leg: "out" });
    trips.set(tr.back_tx_id, { tr, k, leg: "back" });
  });
  let prev = null;
  return (d.transactions || []).map((t, i) => {
    const ab = t.direction_code ? t.direction_code === "A_TO_B" : t.from_entity === d.entity_a_name;
    const row = { ...t, i, ab, ts: tsOf(t), turnaround: null, trip: trips.get(t.tx_id) || null };
    if (prev && prev.ab !== ab) {
      const h = (row.ts - prev.ts) / 36e5;
      if (h >= 0) row.turnaround = h;
    }
    row.rapidFlip = row.turnaround != null && row.turnaround <= rapidH;
    prev = row;
    return row;
  });
}

export async function mount(el, ctx) {
  await ctx.store.ensureCaseData();
  if (!ctx.isCurrent()) return undefined;

  const state = { a: ctx.params.a || "", b: ctx.params.b || "", p: readParams(ctx.params), data: null, rows: [], pairs: [], loaded: false };
  const pickA = uid("bi-a");
  const pickB = uid("bi-b");
  const fid = (n) => `${pickA}-${n}`;

  render(
    el,
    html`<div class="stack-lg">
      ${pageHead({
        title: "Bidirectional flow",
        sub: "Mutual exchanges, amount-matched round trips, net position and turnaround between two parties — a classic round-tripping and layering signal.",
      })}
      <section class="card" aria-labelledby="bi-pick-title">
        <div class="card__head"><div><h2 class="card__title" id="bi-pick-title">Pair</h2>
          <p class="card__sub">Pick any two entities, or choose a ranked mutual pair. Parameters apply to the round-trip matching.</p></div></div>
        <form class="card__body stack" data-slot="form" novalidate>
          <div class="v-bidirectional-picker">
            <label class="field" for="${pickA}"><span class="field__label">Entity A</span>
              <select class="select" id="${pickA}" name="a"></select></label>
            <button type="button" class="btn btn--icon v-bidirectional-swap" data-action="swap" aria-label="Swap A and B" title="Swap A and B">${icon("swap")}</button>
            <label class="field" for="${pickB}"><span class="field__label">Entity B</span>
              <select class="select" id="${pickB}" name="b"></select></label>
            <button type="submit" class="btn btn--primary">${icon("play", { size: "sm" })}Analyse</button>
          </div>
          <fieldset class="field v-bidirectional-params">
            <legend class="field__label">Analysis parameters</legend>
            <div class="form-grid">
              <label class="field" for="${fid("rapid")}"><span class="field__label">Rapid window (hours)</span>
                <input class="input input--num" type="number" id="${fid("rapid")}" name="rapid_h" min="1" max="8760" step="1" inputmode="numeric" required>
                <span class="field__hint">A round trip or reversal this fast is “rapid”.</span></label>
              <label class="field" for="${fid("rt")}"><span class="field__label">Round-trip window (days)</span>
                <input class="input input--num" type="number" id="${fid("rt")}" name="rt_days" min="1" max="366" step="1" inputmode="numeric" required>
                <span class="field__hint">Longest gap between an outflow and its matching return.</span></label>
              <label class="field" for="${fid("tol")}"><span class="field__label">Amount tolerance (%)</span>
                <input class="input input--num" type="number" id="${fid("tol")}" name="tol" min="0" max="100" step="1" inputmode="decimal" required>
                <span class="field__hint">Return within ± this % of the outflow counts as a match.</span></label>
              <label class="field" for="${fid("from")}"><span class="field__label">From</span>
                <input class="input" type="date" id="${fid("from")}" name="date_from"></label>
              <label class="field" for="${fid("to")}"><span class="field__label">To</span>
                <input class="input" type="date" id="${fid("to")}" name="date_to"></label>
            </div>
            <div class="row row-wrap">
              <button type="button" class="btn btn--sm btn--ghost" data-action="reset-params">Reset parameters</button>
            </div>
          </fieldset>
        </form>
      </section>
      <div class="v-bidirectional-layout">
        <div class="stack-lg min-w-0" data-slot="result">${skeleton({ kind: "kpis", rows: 4 })}${skeleton({ kind: "block" })}</div>
        <section class="card" aria-labelledby="bi-pairs-title">
          <div class="card__head"><div><h2 class="card__title" id="bi-pairs-title">Mutual pairs</h2>
            <p class="card__sub">Ranked by two-way volume. System parties (cash, charges) excluded.</p></div></div>
          <div class="card__body" data-slot="pairs"></div>
        </section>
      </div>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);
  const form = slot("form");
  const selA = form.elements.namedItem("a");
  const selB = form.elements.namedItem("b");
  const pIn = (n) => form.elements.namedItem(n);

  function writeParamInputs() {
    pIn("rapid_h").value = String(state.p.rapid_h);
    pIn("rt_days").value = String(state.p.rt_days);
    pIn("tol").value = String(state.p.tol);
    pIn("date_from").value = state.p.date_from;
    pIn("date_to").value = state.p.date_to;
  }
  function readParamInputs() {
    return readParams({
      rapid_h: pIn("rapid_h").value, rt_days: pIn("rt_days").value, tol: pIn("tol").value,
      date_from: pIn("date_from").value, date_to: pIn("date_to").value,
    });
  }
  /** URL form of the parameters: defaults are omitted so shared links stay short. */
  const urlParams = (p) => ({
    rapid_h: p.rapid_h !== PARAM_DEFAULTS.rapid_h ? String(p.rapid_h) : null,
    rt_days: p.rt_days !== PARAM_DEFAULTS.rt_days ? String(p.rt_days) : null,
    tol: p.tol !== PARAM_DEFAULTS.tol ? String(p.tol) : null,
    date_from: p.date_from || null,
    date_to: p.date_to || null,
  });
  /** Query for trace/bidirectional (server units: hours, hours, ratio). */
  const apiParams = () => ({
    a: state.a && state.b ? state.a : null,
    b: state.a && state.b ? state.b : null,
    rapid_window_hours: state.p.rapid_h,
    round_trip_window_hours: state.p.rt_days * 24,
    amount_tolerance: state.p.tol / 100,
    date_from: state.p.date_from || null,
    date_to: state.p.date_to || null,
  });

  /* ------------------------------------------------------------------ entity options */
  function fillOptions() {
    const ents = [...ctx.store.entities()].sort((x, y) => String(x.canonical_name).localeCompare(String(y.canonical_name)));
    const known = new Set(ents.map((e) => e.entity_id));
    const extra = [];
    const d = state.data;
    for (const [id, name] of [[state.a, d?.entity_a_id === state.a ? d.entity_a_name : null], [state.b, d?.entity_b_id === state.b ? d.entity_b_name : null]]) {
      if (id && !known.has(id)) {
        known.add(id);
        extra.push({ entity_id: id, canonical_name: name || ctx.store.entity(id)?.canonical_name || id, has_statement: false });
      }
    }
    const opts = (cur) => html`<option value="">Select an entity…</option>${[...ents, ...extra].map(
      (e) => html`<option value="${e.entity_id}" ${e.entity_id === cur ? html`selected` : ""}>${e.canonical_name}${e.has_statement ? " · statement" : ""}</option>`
    )}`;
    render(selA, opts(state.a));
    render(selB, opts(state.b));
  }

  /* ------------------------------------------------------------------ pairs list */
  const isSel = (p) => (p.entity_a_id === state.a && p.entity_b_id === state.b) || (p.entity_a_id === state.b && p.entity_b_id === state.a);
  const pairGrid = createDataGrid(slot("pairs"), {
    ariaLabel: "Ranked mutual pairs",
    rowKey: (p) => `${p.entity_a_id}|${p.entity_b_id}`,
    rows: [],
    height: "min(56vh, 520px)",
    rowHeight: 52,
    countNoun: ["pair", "pairs"],
    initialSort: { key: "total_mutual_volume", order: "desc" },
    filters: [{ key: "q", type: "search", label: "Search pairs", placeholder: "Entity name" }],
    clientFilter: (p, f) => {
      const q = String(f.q || "").trim().toLowerCase();
      return !q || `${p.entity_a_name} ${p.entity_b_name}`.toLowerCase().includes(q);
    },
    onRowClick: (p) => choose(p.entity_a_id, p.entity_b_id),
    emptyState: { icon: "swap", title: "No mutual pairs", message: "No two entities have sent funds to each other. You can still compare any two entities above.", compact: true },
    columns: [
      { key: "entity_a_name", label: "Pair", width: "minmax(150px, 1fr)", sortable: true,
        title: (p) => `${p.entity_a_name} ⇄ ${p.entity_b_name}`,
        render: (p) => html`<span class="v-bidirectional-pair-cell"><span class="truncate strong">${p.entity_a_name}${isSel(p) ? html` <span class="badge badge--accent">Selected</span>` : ""}</span>
          <span class="dg__sub">⇄ ${p.entity_b_name} · ${int(p.tx_count_a_to_b + p.tx_count_b_to_a)} txns</span></span>` },
      { key: "total_mutual_volume", label: "Two-way", width: "96px", align: "right", sortable: true, defaultOrder: "desc",
        title: (p) => `${inr(p.total_a_to_b)} → / ${inr(p.total_b_to_a)} ←`, format: (v) => inrCompact(v) },
    ],
  });
  ctx.onCleanup(() => pairGrid.destroy());

  /* ------------------------------------------------------------------ tooltip */
  const tip = document.createElement("div");
  tip.className = "tooltip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.appendChild(tip);
  ctx.onCleanup(() => tip.remove());
  const showTip = (content, x, y) => {
    render(tip, content);
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    tip.style.left = `${Math.max(8, Math.min(window.innerWidth - r.width - 8, x + 14))}px`;
    tip.style.top = `${Math.max(8, y + 16 + r.height > window.innerHeight ? y - r.height - 12 : y + 16)}px`;
  };

  /* ------------------------------------------------------------------ result rendering */
  let grids = [];
  let chartRO = null;
  let chartW = 0;
  const destroyResult = () => {
    for (const g of grids) g.destroy();
    grids = [];
    chartRO?.disconnect();
    chartRO = null;
    chartW = 0;
  };
  ctx.onCleanup(destroyResult);

  const paramText = (d) => {
    const pr = d.parameters || {};
    const rapid = pr.rapid_window_hours ?? state.p.rapid_h;
    const days = pr.round_trip_window_hours != null ? pr.round_trip_window_hours / 24 : state.p.rt_days;
    const tol = pr.amount_tolerance != null ? pr.amount_tolerance * 100 : state.p.tol;
    return { rapid, days, tol };
  };

  function interpretation(d) {
    const A = d.entity_a_name;
    const B = d.entity_b_name;
    const { rapid, days, tol } = paramText(d);
    const st = d.round_trip_stats || {};
    const nA = st.anchor_a?.count || 0;
    const nB = st.anchor_b?.count || 0;
    const parts = [];
    if (d.net_flow > 0) parts.push(html`<strong>${A}</strong> is the net sender: ${inr(d.net_flow)} more went to ${B} than came back.`);
    else if (d.net_flow < 0) parts.push(html`<strong>${B}</strong> is the net sender: ${inr(-d.net_flow)} more went to ${A} than came back.`);
    else parts.push(html`Flows in both directions cancel out exactly — a fully round-tripped position.`);
    if (nA || nB) {
      parts.push(html` ${plural(nA + nB, "amount-matched round trip")} (±${num(tol, 0)} %, within ${plural(days, "day")}):
        ${int(nA)} A → B → A and ${int(nB)} B → A → B, ${int((d.rapid_round_trip_count || 0) + (d.rapid_round_trip_count_b || 0))} of them within ${hours(rapid)}.`);
    } else {
      parts.push(html` No outflow came back within ±${num(tol, 0)} % and ${plural(days, "day")}, so there are no amount-matched round trips.`);
    }
    if (d.direction_reversal_count) parts.push(html` ${plural(d.direction_reversal_count, "direction flip")} (any amount), ${int(d.rapid_reversal_count)} within ${hours(rapid)}.`);
    const big = Math.max(d.total_a_to_b, d.total_b_to_a);
    const ratio = big ? Math.min(d.total_a_to_b, d.total_b_to_a) / big : 0;
    const warn = nA + nB > 0 || ratio >= 0.5;
    return html`<div class="callout ${warn ? "callout--warn" : "callout--info"}">${icon(warn ? "alert" : "info")}<div>${parts}</div></div>`;
  }

  function statsTable(d) {
    const st = d.round_trip_stats || {};
    const rowsT = [
      { label: "Round trips A → B → A", s: st.anchor_a, rapid: st.anchor_a?.rapid_count, vol: st.anchor_a?.matched_volume },
      { label: "Round trips B → A → B", s: st.anchor_b, rapid: st.anchor_b?.rapid_count, vol: st.anchor_b?.matched_volume },
      { label: "Direction flips (any amount)", s: st.direction_flips, rapid: d.rapid_reversal_count, vol: null },
    ];
    return html`<div class="table-wrap"><table class="table table--compact">
      <caption class="sr-only">Turnaround statistics in hours: count, minimum, median, maximum and rapid count per kind</caption>
      <thead><tr><th scope="col">Kind</th><th scope="col" class="num">Count</th><th scope="col" class="num">Min</th><th scope="col" class="num">Median</th>
        <th scope="col" class="num">Max</th><th scope="col" class="num">Rapid</th><th scope="col" class="num">Matched volume</th></tr></thead>
      <tbody>${rowsT.map((r) => html`<tr><th scope="row">${r.label}</th>
        <td class="num tabular">${int(r.s?.count || 0)}</td>
        <td class="num tabular">${r.s?.count ? hours(r.s.min_hours) : "—"}</td>
        <td class="num tabular">${r.s?.count ? hours(r.s.median_hours) : "—"}</td>
        <td class="num tabular">${r.s?.count ? hours(r.s.max_hours) : "—"}</td>
        <td class="num tabular">${r.rapid ? html`<span class="badge badge--suspicious">${int(r.rapid)}</span>` : "0"}</td>
        <td class="num tabular">${r.vol == null ? "—" : inr(r.vol)}</td></tr>`)}</tbody></table></div>`;
  }

  function drawResult() {
    destroyResult();
    const out = slot("result");
    const d = state.data;
    if (!d) {
      render(
        out,
        html`<div class="card"><div class="card__body">${emptyState({
          icon: "swap",
          title: state.pairs.length ? "Choose a pair" : "No mutual exchanges found",
          message: state.pairs.length
            ? "Pick two entities above or select a ranked pair."
            : "No two entities have sent money to each other in this case. Pick any two entities above to inspect one-way flows between them.",
          compact: true,
        })}</div></div>`
      );
      return;
    }
    const rows = state.rows;
    const A = d.entity_a_name;
    const B = d.entity_b_name;
    const { rapid, days, tol } = paramText(d);
    const st = d.round_trip_stats || {};
    const big = Math.max(d.total_a_to_b, d.total_b_to_a);
    const ratio = big ? Math.min(d.total_a_to_b, d.total_b_to_a) / big : null;
    const trips = d.round_trips || [];
    const rapidTrips = (d.rapid_round_trip_count || 0) + (d.rapid_round_trip_count_b || 0);
    render(
      out,
      html`<div class="v-bidirectional-pairhead">
          <a href="${ctx.href("profile", { entity: d.entity_a_id })}" title="Open profile">${A}</a>
          <span class="v-bidirectional-arrow" aria-hidden="true">⇄</span><span class="sr-only"> and </span>
          <a href="${ctx.href("profile", { entity: d.entity_b_id })}" title="Open profile">${B}</a>
          <span class="spacer"></span>
          <a class="btn btn--sm" href="${ctx.href("network", { entity: d.entity_a_id })}">${icon("network", { size: "sm" })}Show in network</a>
        </div>
        ${kpiGrid(
          [
            { label: `${truncate(A, 18)} → ${truncate(B, 18)}`, value: inrCompact(d.total_a_to_b), title: inr(d.total_a_to_b), sub: plural(d.tx_count_a_to_b, "transfer"), icon: "arrow-up-right" },
            { label: `${truncate(B, 18)} → ${truncate(A, 18)}`, value: inrCompact(d.total_b_to_a), title: inr(d.total_b_to_a), sub: plural(d.tx_count_b_to_a, "transfer"), icon: "arrow-down-left" },
            { label: "Net flow (A → B)", value: inrCompact(d.net_flow, { sign: true }), title: inr(d.net_flow, { sign: true }),
              sub: d.net_flow > 0 ? `${truncate(A, 22)} is net sender` : d.net_flow < 0 ? `${truncate(B, 22)} is net sender` : "Balanced" },
            { label: "Return ratio", value: ratio == null ? "—" : pct(ratio, { decimals: 0 }), sub: "smaller leg ÷ larger leg", tone: ratio != null && ratio >= 0.5 ? "warn" : "" },
            { label: "Round trips A → B → A", value: int(st.anchor_a?.count || 0), sub: `median ${st.anchor_a?.count ? hours(st.anchor_a.median_hours) : "—"}`,
              tone: st.anchor_a?.rapid_count ? "critical" : st.anchor_a?.count ? "warn" : "success" },
            { label: "Round trips B → A → B", value: int(st.anchor_b?.count || 0), sub: `median ${st.anchor_b?.count ? hours(st.anchor_b.median_hours) : "—"}`,
              tone: st.anchor_b?.rapid_count ? "critical" : st.anchor_b?.count ? "warn" : "success" },
            { label: "Rapid round trips", value: int(rapidTrips), sub: `amount-matched, ≤ ${hours(rapid)}`, tone: rapidTrips ? "critical" : "success" },
            { label: "Flip turnaround", value: d.direction_reversal_count ? hours(d.median_turnaround_hours) : "—",
              sub: `min ${hours(d.min_turnaround_hours)} · max ${hours(d.max_turnaround_hours)}`,
              title: `${plural(d.direction_reversal_count || 0, "direction flip")}, ${int(d.rapid_reversal_count)} within ${hours(rapid)}` },
          ],
          { compact: true, label: "Pair summary" }
        )}
        ${interpretation(d)}
        <div class="callout callout--info v-bidirectional-dedup">${icon("info")}<div>
          Mirrored legs are counted once: when both statements are loaded, A's debit and B's credit for the same transfer are paired
          (by UTR first, then by amount within ±2 days) so a single transfer never appears twice. Matching uses ±${num(tol, 0)} % within
          ${plural(days, "day")}${d.parameters?.date_from || d.parameters?.date_to ? html`, between ${d.parameters.date_from ? date(d.parameters.date_from) : "the start"} and ${d.parameters.date_to ? date(d.parameters.date_to) : "the end"}` : ""}.
        </div></div>
        <section class="card" aria-labelledby="bi-chart-title">
          <div class="card__head"><div><h2 class="card__title" id="bi-chart-title">Dual ledger timeline</h2>
            <p class="card__sub">Transfers A → B above the line, B → A below; arcs join each amount-matched round trip. The lower band shows each side's cumulative total and A's net position.</p></div>
            <div class="legend">
              <span class="legend__item"><span class="legend__swatch sw-1"></span>${truncate(A, 20)} → ${truncate(B, 20)}</span>
              <span class="legend__item"><span class="legend__swatch sw-2"></span>${truncate(B, 20)} → ${truncate(A, 20)}</span>
              <span class="legend__item"><span class="legend__swatch v-bidirectional-sw-rapid"></span>Rapid round trip (≤ ${hours(rapid)})</span>
              <span class="legend__item"><span class="legend__swatch v-bidirectional-sw-trip"></span>Round trip</span>
              <span class="legend__item"><span class="legend__swatch v-bidirectional-sw-cum"></span>Net (A → B)</span>
            </div></div>
          <div class="card__body">
            ${rows.length ? html`<div class="v-bidirectional-chart" data-slot="chart"></div>` : emptyState({ icon: "chart", title: "No transfers between these two entities", message: "Pick a different pair or widen the dates.", compact: true })}
          </div>
        </section>
        <section class="card" aria-labelledby="bi-stats-title">
          <div class="card__head"><div><h2 class="card__title" id="bi-stats-title">Turnaround</h2>
            <p class="card__sub">Minimum, median and maximum turnaround for amount-matched round trips on each side, and for plain direction flips.</p></div></div>
          <div class="card__body">${statsTable(d)}</div>
        </section>
        <section class="card" aria-labelledby="bi-trips-title">
          <div class="card__head"><div><h2 class="card__title" id="bi-trips-title">Round trips</h2>
            <p class="card__sub">Each outflow paired with the earliest later return within ±${num(tol, 0)} % and ${plural(days, "day")} (each transfer used once).</p></div></div>
          <div class="card__body" data-slot="trips"></div>
        </section>
        <section class="card" aria-labelledby="bi-dual-title">
          <div class="card__head"><div><h2 class="card__title" id="bi-dual-title">Dual ledger</h2>
            <p class="card__sub">Each direction on its own, with that side's running total.</p></div></div>
          <div class="card__body v-bidirectional-dual">
            <div class="stack-sm min-w-0"><h3 class="subhead">${truncate(A, 28)} → ${truncate(B, 28)}</h3><div data-slot="ledger-ab"></div></div>
            <div class="stack-sm min-w-0"><h3 class="subhead">${truncate(B, 28)} → ${truncate(A, 28)}</h3><div data-slot="ledger-ba"></div></div>
          </div>
        </section>
        <section class="card" aria-labelledby="bi-ledger-title">
          <div class="card__head"><div><h2 class="card__title" id="bi-ledger-title">Pair ledger</h2>
            <p class="card__sub">Chronological transfers with the round trip each belongs to, turnaround since the previous direction flip and A's running net position.</p></div></div>
          <div class="card__body" data-slot="ledger"></div>
        </section>`
    );

    if (rows.length) {
      const chartEl = out.querySelector('[data-slot="chart"]');
      const sizeChart = () => {
        const w = Math.floor(chartEl.getBoundingClientRect().width);
        if (w && w !== chartW) {
          chartW = w;
          drawChart(chartEl, d, rows, w);
        }
      };
      chartRO = new ResizeObserver(sizeChart);
      chartRO.observe(chartEl);
      sizeChart();
    }

    const toTx = (id) => ctx.navigate("transactions", { ids: id });
    const utrCell = (u) => (u ? html`<span class="mono xsmall">${u}</span>` : html`<span class="muted">—</span>`);
    const fmtWhen = (dt, ts) => html`${date(dt)}${clockTime(ts) ? html`<span class="dg__sub tabular">${clockTime(ts)}</span>` : ""}`;

    grids.push(createDataGrid(out.querySelector('[data-slot="trips"]'), {
      ariaLabel: `Amount-matched round trips between ${A} and ${B}`,
      rowKey: (r) => `${r.out_tx_id}>${r.back_tx_id}`,
      rows: trips,
      height: trips.length > 8 ? "min(50vh, 420px)" : "auto",
      rowHeight: 48,
      countNoun: ["round trip", "round trips"],
      initialSort: { key: "out_timestamp", order: "asc" },
      showToolbar: false,
      onRowClick: (r) => ctx.navigate("transactions", { ids: `${r.out_tx_id},${r.back_tx_id}` }),
      emptyState: { icon: "repeat", title: "No amount-matched round trips", message: `No outflow came back within ±${num(tol, 0)} % and ${plural(days, "day")}. Widen the tolerance or window above.`, compact: true },
      columns: [
        { key: "anchor", label: "Pattern", width: "110px", sortable: true,
          render: (r) => html`<span class="badge ${r.anchor === "A" ? "badge--info" : "badge--accent"}">${r.anchor === "A" ? "A → B → A" : "B → A → B"}</span>` },
        { key: "out_timestamp", label: "Out", width: "120px", sortable: true, sortValue: (r) => String(r.out_timestamp || r.out_date), render: (r) => fmtWhen(r.out_date, r.out_timestamp) },
        { key: "out_amount", label: "Out amount", width: "130px", align: "right", sortable: true, format: (v) => inr(v) },
        { key: "out_utr", label: "Out UTR", width: "minmax(100px, 1fr)", render: (r) => utrCell(r.out_utr) },
        { key: "back_timestamp", label: "Back", width: "120px", sortable: true, sortValue: (r) => String(r.back_timestamp || r.back_date), render: (r) => fmtWhen(r.back_date, r.back_timestamp) },
        { key: "back_amount", label: "Back amount", width: "130px", align: "right", sortable: true, format: (v) => inr(v) },
        { key: "back_utr", label: "Back UTR", width: "minmax(100px, 1fr)", render: (r) => utrCell(r.back_utr) },
        { key: "amount_diff", label: "Difference", width: "120px", align: "right", sortable: true, className: (r) => signClass(r.amount_diff), format: (v) => inr(v, { sign: true }) },
        { key: "turnaround_hours", label: "Turnaround", width: "110px", align: "right", sortable: true,
          render: (r) => (r.rapid ? html`<span class="badge badge--suspicious" title="Within the rapid window">${hours(r.turnaround_hours)}</span>` : html`${hours(r.turnaround_hours)}`) },
      ],
    }));

    const sideGrid = (slotName, list, label) => createDataGrid(out.querySelector(`[data-slot="${slotName}"]`), {
      ariaLabel: label,
      rowKey: "tx_id",
      rows: list || [],
      height: (list || []).length > 8 ? "min(46vh, 380px)" : "auto",
      rowHeight: 48,
      countNoun: ["transfer", "transfers"],
      initialSort: { key: "ts", order: "asc" },
      showToolbar: false,
      onRowClick: (r) => toTx(r.tx_id),
      emptyState: { icon: "list", title: "No transfers in this direction", compact: true },
      columns: [
        { key: "ts", label: "Date", width: "110px", sortable: true, sortValue: (r) => String(r.timestamp || r.date), render: (r) => fmtWhen(r.date, r.timestamp) },
        { key: "amount", label: "Amount", width: "minmax(110px, 1fr)", align: "right", sortable: true, format: (v) => inr(v) },
        { key: "channel", label: "Channel / UTR", width: "minmax(110px, 1fr)", title: (r) => r.narration,
          render: (r) => html`<span class="badge badge--neutral">${r.channel || "—"}</span>${r.utr ? html`<span class="dg__sub mono">${r.utr}</span>` : ""}` },
        { key: "cumulative", label: "Running total", width: "minmax(120px, 1fr)", align: "right", sortable: true, format: (v) => inr(v) },
      ],
    });
    grids.push(sideGrid("ledger-ab", d.ledger_a_to_b, `${A} to ${B} ledger`));
    grids.push(sideGrid("ledger-ba", d.ledger_b_to_a, `${B} to ${A} ledger`));

    grids.push(createDataGrid(out.querySelector('[data-slot="ledger"]'), {
      ariaLabel: `Ledger between ${A} and ${B}`,
      rowKey: "tx_id",
      rows,
      height: rows.length > 10 ? "min(60vh, 520px)" : "auto",
      rowHeight: 48,
      countNoun: ["transfer", "transfers"],
      initialSort: { key: "ts", order: "asc" },
      showToolbar: rows.length > 10,
      filters: [{ key: "q", type: "search", label: "Search ledger", placeholder: "Narration, UTR, channel" }],
      clientFilter: (r, f) => {
        const q = String(f.q || "").trim().toLowerCase();
        return !q || `${r.narration} ${r.utr || ""} ${r.channel}`.toLowerCase().includes(q);
      },
      onRowClick: (r) => ctx.navigate("transactions", r.tx_id ? { ids: r.tx_id } : { q: r.utr || String(r.narration || "").slice(0, 40) }),
      emptyState: { icon: "list", title: "No transfers", message: "These two entities have not exchanged funds in this period." },
      columns: [
        { key: "ts", label: "Date", width: "120px", sortable: true, title: (r) => r.timestamp || r.date,
          render: (r) => html`${date(r.date)}${timeOf(r) ? html`<span class="dg__sub tabular">${timeOf(r)}</span>` : ""}` },
        { key: "ab", label: "Direction", width: "minmax(170px, 1.4fr)", sortable: true, title: (r) => r.direction,
          render: (r) => html`<span class="v-bidirectional-dir"><span class="legend__swatch ${r.ab ? "sw-1" : "sw-2"}"></span><span class="truncate">${r.ab ? "A → B" : "B → A"}</span></span>
            <span class="dg__sub">${r.from_entity} → ${r.to_entity}</span>` },
        { key: "amount", label: "Amount (A's view)", width: "150px", align: "right", sortable: true, defaultOrder: "desc",
          className: (r) => (r.ab ? "is-debit" : "is-credit"),
          title: (r) => (r.ab ? `A paid B ${inr(r.amount)}` : `B paid A ${inr(r.amount)}`),
          render: (r) => (r.ab ? html`${inr(-r.amount)}` : html`${inr(r.amount, { sign: true })}`) },
        { key: "cumulative_net_a_to_b", label: "Net sent A → B", width: "150px", align: "right", sortable: true,
          className: (r) => signClass(r.cumulative_net_a_to_b), title: (r) => `Cumulative net A → B ${inr(r.cumulative_net_a_to_b, { sign: true })} (positive: A has sent B more than it received)`,
          format: (v) => inr(v, { sign: true }) },
        { key: "turnaround", label: "Flip turnaround", width: "130px", align: "right", sortable: true, sortValue: (r) => (r.turnaround == null ? Infinity : r.turnaround),
          render: (r) => (r.turnaround == null
            ? html`<span class="muted">—</span>`
            : r.rapidFlip
              ? html`<span class="badge badge--suspicious" title="Direction flip within ${hours(rapid)}">${hours(r.turnaround)}</span>`
              : html`${hours(r.turnaround)}`) },
        { key: "channel", label: "Channel", width: "90px", sortable: true, render: (r) => html`<span class="badge badge--neutral">${r.channel || "—"}</span>` },
        { key: "utr", label: "UTR", width: "minmax(110px, 0.8fr)", sortable: true, render: (r) => utrCell(r.utr) },
        { key: "trip", label: "Round trip", width: "130px", sortable: true, sortValue: (r) => (r.trip ? r.trip.k : Infinity),
          render: (r) => (r.trip
            ? html`<span class="badge ${r.trip.tr.rapid ? "badge--suspicious" : "badge--neutral"}" title="${r.trip.tr.anchor === "A" ? "A → B → A" : "B → A → B"}, ${hours(r.trip.tr.turnaround_hours)}">#${r.trip.k + 1} ${r.trip.leg === "out" ? "out" : "back"}</span>`
            : html`<span class="muted">—</span>`) },
        { key: "narration", label: "Narration", width: "minmax(180px, 2fr)", sortable: true, title: (r) => r.narration, render: (r) => html`${r.narration}` },
      ],
    }));
  }

  /* ------------------------------------------------------------------ dual ledger chart (SVG) */
  function drawChart(host, d, rows, W) {
    const { rapid } = paramText(d);
    const m = { l: 64, r: 14, t: 22 };
    const plotW = Math.max(40, W - m.l - m.r);
    const band1 = { top: m.t, h: 150 };
    const mid = band1.top + band1.h / 2;
    const band2 = { top: band1.top + band1.h + 30, h: 110 };
    const axisY = band2.top + band2.h + 8;
    const series = (d.cumulative_series || []).map((s) => ({ ...s, ts: tsOf(s) }));
    const allTs = [...rows.map((r) => r.ts), ...series.map((s) => s.ts)];
    let t0 = Math.min(...allTs);
    let t1 = Math.max(...allTs);
    if (t1 - t0 < 864e5) {
      t0 -= 432e5;
      t1 += 432e5;
    }
    const padT = (t1 - t0) * 0.03;
    t0 -= padT;
    t1 += padT;
    const x = (t) => m.l + ((t - t0) / (t1 - t0)) * plotW;
    const maxAmt = Math.max(1, ...rows.map((r) => r.amount));
    const barH = (a) => Math.max(1.5, (a / maxAmt) * (band1.h / 2 - 6));
    const bw = Math.max(3, Math.min(14, (plotW / rows.length) * 0.6));

    // lower band: server cumulative series (gross per side + net A → B)
    const vals = series.flatMap((s) => [Number(s.cumulative_a_to_b) || 0, Number(s.cumulative_b_to_a) || 0, Number(s.net_a_to_b) || 0]);
    const cMax = Math.max(0, ...vals);
    const cMin = Math.min(0, ...vals);
    const cSpan = cMax - cMin || 1;
    const yc = (v) => band2.top + 4 + ((cMax - v) / cSpan) * (band2.h - 8);
    const zeroY = yc(0);
    const stepPath = (key) => {
      let p = `M ${m.l.toFixed(1)} ${zeroY.toFixed(1)}`;
      let prevY = zeroY;
      for (const s of series) {
        const xi = x(s.ts);
        const yi = yc(Number(s[key]) || 0);
        p += ` L ${xi.toFixed(1)} ${prevY.toFixed(1)} L ${xi.toFixed(1)} ${yi.toFixed(1)}`;
        prevY = yi;
      }
      return `${p} L ${(m.l + plotW).toFixed(1)} ${prevY.toFixed(1)}`;
    };
    const netPath = stepPath("net_a_to_b");
    const area = `${netPath} L ${(m.l + plotW).toFixed(1)} ${zeroY.toFixed(1)} Z`;
    const clipPos = uid("bi-clip-pos");
    const clipNeg = uid("bi-clip-neg");

    // arcs joining each amount-matched round trip (out leg → back leg)
    const byTx = new Map(rows.map((r) => [r.tx_id, r]));
    const arcs = [];
    for (const tr of d.round_trips || []) {
      const p = byTx.get(tr.out_tx_id);
      const r = byTx.get(tr.back_tx_id);
      if (!p || !r) continue;
      const x0 = x(p.ts);
      const x1 = x(r.ts);
      const y0 = p.ab ? mid - barH(p.amount) : mid + barH(p.amount);
      const y1 = r.ab ? mid - barH(r.amount) : mid + barH(r.amount);
      const cy = p.ab ? band1.top - 6 : band1.top + band1.h + 6;
      arcs.push({ d: `M ${x0.toFixed(1)} ${y0.toFixed(1)} Q ${((x0 + x1) / 2).toFixed(1)} ${cy} ${x1.toFixed(1)} ${y1.toFixed(1)}`, tr });
    }

    const nt = Math.max(2, Math.min(6, Math.floor(plotW / 110)));
    const ticks = Array.from({ length: nt + 1 }, (_, i) => t0 + padT + ((t1 - t0 - 2 * padT) * i) / nt);
    const iso = (t) => new Date(t).toISOString().slice(0, 10);
    const A = d.entity_a_name;
    const B = d.entity_b_name;
    const last = rows[rows.length - 1];
    const lastS = series[series.length - 1];
    const summary =
      `Timeline of ${rows.length} transfers between ${A} and ${B} from ${date(rows[0].date)} to ${date(last.date)}. ` +
      `${A} to ${B}: ${inr(d.total_a_to_b)} in ${d.tx_count_a_to_b}; ${B} to ${A}: ${inr(d.total_b_to_a)} in ${d.tx_count_b_to_a}. ` +
      `${(d.round_trips || []).length} amount-matched round trips, ${(d.rapid_round_trip_count || 0) + (d.rapid_round_trip_count_b || 0)} within ${hours(rapid)}. ` +
      `Final net ${inr(lastS ? lastS.net_a_to_b : last.cumulative_net_a_to_b, { sign: true })}. The tables below list every transfer and round trip.`;
    const colW = Math.max(bw + 4, Math.min(40, plotW / rows.length));

    render(
      host,
      html`<svg class="v-bidirectional-svg" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="${summary}">
        <defs>
          <clipPath id="${clipPos}"><rect x="0" y="0" width="${W}" height="${zeroY.toFixed(1)}"></rect></clipPath>
          <clipPath id="${clipNeg}"><rect x="0" y="${zeroY.toFixed(1)}" width="${W}" height="${(H - zeroY).toFixed(1)}"></rect></clipPath>
        </defs>
        <text class="svg-text-2 v-bidirectional-band-label" x="${m.l - 8}" y="${band1.top + 10}" text-anchor="end">A → B</text>
        <text class="svg-text-3 v-bidirectional-tick" x="${m.l - 8}" y="${band1.top + 22}" text-anchor="end">${inrCompact(maxAmt)}</text>
        <text class="svg-text-2 v-bidirectional-band-label" x="${m.l - 8}" y="${band1.top + band1.h - 14}" text-anchor="end">B → A</text>
        <text class="svg-text-3 v-bidirectional-tick" x="${m.l - 8}" y="${band1.top + band1.h - 2}" text-anchor="end">${inrCompact(maxAmt)}</text>
        ${ticks.map((t) => html`<line class="svg-grid" x1="${x(t).toFixed(1)}" x2="${x(t).toFixed(1)}" y1="${band1.top}" y2="${band2.top + band2.h}"></line>`)}
        <line class="v-bidirectional-mid" x1="${m.l}" x2="${m.l + plotW}" y1="${mid}" y2="${mid}"></line>
        ${rows.map((r) => {
          const h = barH(r.amount);
          return html`<rect class="${r.ab ? "v-bidirectional-bar--ab" : "v-bidirectional-bar--ba"}" x="${(x(r.ts) - bw / 2).toFixed(1)}" y="${(r.ab ? mid - h : mid).toFixed(1)}"
            width="${bw.toFixed(1)}" height="${h.toFixed(1)}" rx="1.5"></rect>`;
        })}
        ${arcs.map((a) => html`<path class="${a.tr.rapid ? "v-bidirectional-rapid" : "v-bidirectional-trip"}" d="${a.d}"><title>${a.tr.anchor === "A" ? "A → B → A" : "B → A → B"} round trip: ${inr(a.tr.out_amount)} out, ${inr(a.tr.back_amount)} back after ${hours(a.tr.turnaround_hours)}</title></path>`)}
        <text class="svg-text-2 v-bidirectional-band-label" x="${m.l - 8}" y="${band2.top + 8}" text-anchor="end">Cumulative</text>
        <text class="svg-text-3 v-bidirectional-tick" x="${m.l - 8}" y="${(yc(cMax) + 20).toFixed(1)}" text-anchor="end">${cMax ? inrCompact(cMax) : ""}</text>
        <text class="svg-text-3 v-bidirectional-tick" x="${m.l - 8}" y="${(zeroY + 3).toFixed(1)}" text-anchor="end">0</text>
        ${cMin < 0 ? html`<text class="svg-text-3 v-bidirectional-tick" x="${m.l - 8}" y="${yc(cMin).toFixed(1)}" text-anchor="end">${inrCompact(cMin)}</text>` : ""}
        <path class="v-bidirectional-area--pos" d="${area}" clip-path="url(#${clipPos})"></path>
        <path class="v-bidirectional-area--neg" d="${area}" clip-path="url(#${clipNeg})"></path>
        <line class="v-bidirectional-zero" x1="${m.l}" x2="${m.l + plotW}" y1="${zeroY.toFixed(1)}" y2="${zeroY.toFixed(1)}"></line>
        <path class="v-bidirectional-gross--ab" d="${stepPath("cumulative_a_to_b")}"></path>
        <path class="v-bidirectional-gross--ba" d="${stepPath("cumulative_b_to_a")}"></path>
        <path class="v-bidirectional-cum" d="${netPath}"></path>
        ${series.map((s) => html`<circle class="v-bidirectional-dot" cx="${x(s.ts).toFixed(1)}" cy="${yc(Number(s.net_a_to_b) || 0).toFixed(1)}" r="3"></circle>`)}
        <line class="svg-axis" x1="${m.l}" x2="${m.l + plotW}" y1="${axisY}" y2="${axisY}"></line>
        ${ticks.map((t, i) => html`<text class="svg-text-3 v-bidirectional-tick" x="${x(t).toFixed(1)}" y="${axisY + 14}"
            text-anchor="${i === 0 ? "start" : i === ticks.length - 1 ? "end" : "middle"}">${date(iso(t))}</text>`)}
        <line class="v-bidirectional-cursor" data-slot="cursor" x1="0" x2="0" y1="${band1.top}" y2="${band2.top + band2.h}" visibility="hidden"></line>
        ${rows.map((r) => html`<rect class="v-bidirectional-hit" data-i="${r.i}" x="${(x(r.ts) - colW / 2).toFixed(1)}" y="${band1.top}" width="${colW.toFixed(1)}" height="${band2.top + band2.h - band1.top}"></rect>`)}
      </svg>`
    );
    const svg = host.querySelector("svg");
    const cursor = svg.querySelector('[data-slot="cursor"]');
    const byI = new Map(rows.map((r) => [String(r.i), r]));
    svg.addEventListener("pointermove", (e) => {
      const hit = e.target.closest?.("[data-i]");
      const r = hit ? byI.get(hit.dataset.i) : null;
      if (!r) {
        tip.hidden = true;
        cursor.setAttribute("visibility", "hidden");
        return;
      }
      const cx = x(r.ts).toFixed(1);
      cursor.setAttribute("x1", cx);
      cursor.setAttribute("x2", cx);
      cursor.setAttribute("visibility", "visible");
      showTip(
        html`<div class="strong">${date(r.date)}${timeOf(r) ? ` ${timeOf(r)}` : ""} · ${r.channel || "—"}</div>
          <div>${r.from_entity} → ${r.to_entity}</div>
          <div class="tabular">${inr(r.amount)}${r.utr ? ` · UTR ${r.utr}` : ""}</div>
          ${r.trip ? html`<div>Round trip #${r.trip.k + 1} (${r.trip.leg}) · ${hours(r.trip.tr.turnaround_hours)}${r.trip.tr.rapid ? " · rapid" : ""}</div>` : ""}
          ${r.turnaround != null ? html`<div>Direction flip after ${hours(r.turnaround)}</div>` : ""}
          <div class="tabular">A → B ${inr(r.cumulative_a_to_b)} · B → A ${inr(r.cumulative_b_to_a)}</div>
          <div class="tabular">Net ${inr(r.cumulative_net_a_to_b, { sign: true })}</div>`,
        e.clientX,
        e.clientY
      );
    });
    svg.addEventListener("pointerleave", () => {
      tip.hidden = true;
      cursor.setAttribute("visibility", "hidden");
    });
  }

  /* ------------------------------------------------------------------ loading */
  let loadCtl = null;
  ctx.onCleanup(() => loadCtl?.abort());
  async function load() {
    if (state.a && state.b && state.a === state.b) {
      destroyResult();
      render(slot("result"), html`<div class="callout callout--warn">${icon("alert")}<div>Pick two different entities.</div></div>`);
      return;
    }
    loadCtl?.abort();
    const c = new AbortController();
    loadCtl = c;
    const unlink = linkAbort(ctx.signal, c);
    const out = slot("result");
    out.setAttribute("aria-busy", "true");
    if (!state.loaded) render(out, html`${skeleton({ kind: "kpis", rows: 4 })}${skeleton({ kind: "block" })}`);
    try {
      const res = await ctx.api.get("trace/bidirectional", apiParams(), { signal: c.signal });
      if (!ctx.isCurrent() || loadCtl !== c) return;
      state.loaded = true;
      state.pairs = res.pairs || [];
      state.data = res.selected_detail || null;
      if (state.data) {
        state.a = state.data.entity_a_id;
        state.b = state.data.entity_b_id;
        ctx.setParams({ a: state.a, b: state.b, ...urlParams(state.p) });
        ctx.setCrumbs([{ label: `${state.data.entity_a_name} ⇄ ${state.data.entity_b_name}` }]);
      } else ctx.setCrumbs([]);
      state.rows = state.data ? enrich(state.data, state.p.rapid_h) : [];
      fillOptions();
      pairGrid.setRows(state.pairs);
      drawResult();
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent()) return;
      destroyResult();
      render(out, html`<div class="card"><div class="card__body">${errorState(err, { retryId: "retry", title: "Could not analyse this pair", compact: true })}</div></div>`);
    } finally {
      unlink();
      if (loadCtl === c) out.removeAttribute("aria-busy");
    }
  }

  function choose(a, b) {
    state.a = a || "";
    state.b = b || "";
    selA.value = state.a;
    selB.value = state.b;
    ctx.setParams({ a: state.a || null, b: state.b || null, ...urlParams(state.p) }, { push: true });
    if (state.a && state.b) load();
  }

  function applyParams() {
    const next = readParamInputs();
    if (next.date_from && next.date_to && next.date_from > next.date_to) {
      ctx.toast.warn("“From” must be on or before “To”.");
      return false;
    }
    state.p = next;
    writeParamInputs();
    return true;
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!applyParams()) return;
    if (!selA.value || !selB.value) {
      if (selA.value || selB.value) {
        ctx.toast.warn("Select both entities to analyse the pair.");
        return;
      }
      ctx.setParams(urlParams(state.p), { push: true });
      load();
      return;
    }
    choose(selA.value, selB.value);
  });
  form.addEventListener("change", (e) => {
    if (e.target !== selA && e.target !== selB) return;
    if (selA.value && selB.value && (selA.value !== state.a || selB.value !== state.b)) {
      applyParams();
      choose(selA.value, selB.value);
    }
  });

  ctx.onCleanup(
    delegate(el, "click", "[data-action]", (e, b) => {
      if (b.dataset.action === "swap") choose(selB.value || state.b, selA.value || state.a);
      else if (b.dataset.action === "retry") load();
      else if (b.dataset.action === "reset-params") {
        state.p = readParams({});
        writeParamInputs();
        ctx.setParams(urlParams(state.p));
        load();
      }
    })
  );

  ctx.registerCommands([
    { id: "bidirectional.swap", title: "Swap pair direction (A ⇄ B)", group: "Bidirectional", icon: "swap", when: () => Boolean(state.a && state.b), run: () => choose(state.b, state.a) },
    { id: "bidirectional.top", title: "Analyse top mutual pair", group: "Bidirectional", icon: "zap", when: () => state.pairs.length > 0,
      run: () => choose(state.pairs[0].entity_a_id, state.pairs[0].entity_b_id) },
    { id: "bidirectional.network", title: "Show entity A in the network graph", group: "Bidirectional", icon: "network", when: () => Boolean(state.a),
      run: () => ctx.navigate("network", { entity: state.a }) },
  ]);

  ctx.onData([TOPICS.analytics, TOPICS.entities], () => load());

  writeParamInputs();
  fillOptions();
  load();

  return {
    update(params) {
      const a = params.a || "";
      const b = params.b || "";
      const p = readParams(params);
      const same = a === state.a && b === state.b && JSON.stringify(p) === JSON.stringify(state.p);
      if (same) return;
      state.a = a;
      state.b = b;
      state.p = p;
      selA.value = a;
      selB.value = b;
      writeParamInputs();
      load();
    },
  };
}
