/* Circular loops (#/case/{id}/loops?max_days=&min_retention=&max_cycle_length=&max_inst=&loop=&inst=&q=&sort=)
 *
 * P1 (TF-13/14): hops carry date, UTR, hours since the previous hop and leakage (shown on the arcs and in the hop
 * table); each loop lists intermediary_leakage (what each intermediary kept back) and its instances[] (every
 * repetition of the same entity cycle, capped by max_instances_per_loop). Picking an instance redraws the diagram
 * with that instance's amounts and UTRs.
 *
 * Temporal round-trip detection A → B → … → A (TF-10/TF-12): scan parameters, truncation banner with the scan
 * metadata, summary KPIs, a keyboard-navigable loop list (hop count, retention, duration, value) and the selected
 * loop as a circular diagram (charts/loop-diagram.js) with a hop table as its text alternative. Entity nodes and
 * names open the entity profile. */
import { delegate, html, render, debounce } from "../core/dom.js";
import { date, dateTime, hours, inr, inrCompact, int, pct, plural, truncate } from "../core/format.js";
import { isAbort } from "../core/api.js";
import { TOPICS } from "../core/store.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { emptyState, errorState, pageHead, skeleton } from "../components/states.js";
import { loopSummary, renderLoopDiagram } from "../charts/loop-diagram.js";
import { downloadText } from "../charts/tree.js";

export const styles = ["views/flow.css"];

const DEFAULTS = { max_days: 30, min_retention: 50, max_cycle_length: 8, max_inst: 50 };
const SORTS = [
  { value: "value", label: "Largest value", fn: (a, b) => b.max_amount - a.max_amount || b.hop_count - a.hop_count },
  { value: "retention", label: "Highest retention", fn: (a, b) => b.retention_ratio - a.retention_ratio || b.max_amount - a.max_amount },
  { value: "fastest", label: "Fastest", fn: (a, b) => a.duration_hours - b.duration_hours || b.max_amount - a.max_amount },
  { value: "hops", label: "Most hops", fn: (a, b) => b.hop_count - a.hop_count || b.max_amount - a.max_amount },
  { value: "instances", label: "Most repeated", fn: (a, b) => (b.instance_count || 1) - (a.instance_count || 1) || b.max_amount - a.max_amount },
  { value: "leakage", label: "Most leakage", fn: (a, b) => (Number(b.total_leakage) || 0) - (Number(a.total_leakage) || 0) },
  { value: "recent", label: "Most recent", fn: (a, b) => String(b.start_date).localeCompare(String(a.start_date)) },
];
const REASONS = {
  time_budget: "the detector's time budget ran out",
  max_cycles: "the maximum number of distinct loops was reached",
};

const clampNum = (v, lo, hi, dflt) => {
  const n = Number(v);
  return v !== undefined && v !== null && v !== "" && Number.isFinite(n) ? Math.min(hi, Math.max(lo, n)) : dflt;
};

function readParams(p) {
  // min_retention lives in the URL as the API ratio (0.5); the form edits it as a percentage.
  const ret = p.min_retention != null && p.min_retention !== "" ? clampNum(p.min_retention, 0, 1, 0.5) * 100 : DEFAULTS.min_retention;
  return {
    max_days: Math.round(clampNum(p.max_days, 0, 3650, DEFAULTS.max_days)),
    min_retention: Math.round(ret),
    max_cycle_length: Math.round(clampNum(p.max_cycle_length, 2, 8, DEFAULTS.max_cycle_length)),
    max_inst: Math.round(clampNum(p.max_inst, 1, 500, DEFAULTS.max_inst)),
    loop: p.loop || "",
    inst: Math.max(0, Math.round(clampNum(p.inst, 0, 499, 0))),
    q: p.q || "",
    sort: SORTS.some((s) => s.value === p.sort) ? p.sort : "value",
  };
}

/** The loop as drawn for one instance: the representative hops with that instance's amounts, UTRs and leakage.
 * Only the first and last timestamps of an instance are known, so the middle hops show no date. */
function instanceView(l, k) {
  const inst = (l.instances || [])[k];
  if (!inst || k === 0) return l;
  const hops = (l.hops || []).map((h, i, all) => {
    const amount = Number(inst.amounts?.[i] ?? h.amount);
    const prev = i ? Number(inst.amounts?.[i - 1] ?? all[i - 1].amount) : null;
    const leak = prev == null ? null : Math.round((prev - amount) * 100) / 100;
    const last = i === all.length - 1;
    return {
      ...h,
      tx_id: inst.tx_ids?.[i] || h.tx_id,
      amount,
      utr: inst.utrs?.[i] ?? null,
      date: i === 0 ? inst.start_date : last ? inst.end_date : null,
      timestamp: i === 0 ? inst.start_timestamp : last ? inst.end_timestamp : null,
      hours_since_previous_hop: null,
      leakage: leak,
      leakage_pct: leak != null && prev ? (leak / prev) * 100 : null,
    };
  });
  return {
    ...l, hops, min_amount: inst.min_amount, max_amount: inst.max_amount, retention_ratio: inst.retention_ratio,
    duration_hours: inst.duration_hours, start_date: inst.start_date, end_date: inst.end_date, total_leakage: inst.leakage,
    leakage_pct: inst.amounts?.[0] ? (Number(inst.leakage) / Number(inst.amounts[0])) * 100 : null, intermediary_leakage: null,
  };
}

const retentionTone = (r) => (r >= 0.9 ? "critical" : r >= 0.7 ? "suspicious" : "neutral");
const median = (xs) => {
  if (!xs.length) return null;
  const s = xs.slice().sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
};

export async function mount(el, ctx) {
  let p = readParams(ctx.params);
  let data = null;
  let loops = [];
  let shown = [];
  let selectedId = p.loop;
  let instIdx = p.inst;
  let dia = null;
  let loadCtl = null;
  let loadSeq = 0;
  let compact = false;

  render(
    el,
    html`<div class="stack">
      ${pageHead({
        title: "Circular loops",
        sub: "Round-trip flows (A → B → … → A) with time-ordered hops inside the window, where the smallest hop keeps at least the minimum share of the largest.",
        actions: html`<button type="button" class="btn" data-action="export-svg">${icon("download", { size: "sm" })}Export SVG</button>`,
      })}
      <section class="card" aria-label="Scan parameters">
        <div class="card__body">
          <form class="v-loops-form" data-slot="form" novalidate>
            <div class="field">
              <label class="field__label" for="loops-days">Max loop duration (days)</label>
              <input class="input input--num input--w-sm" id="loops-days" name="max_days" type="number" min="0" max="3650" step="1" inputmode="numeric" value="${p.max_days}">
            </div>
            <div class="field v-loops-field-ret">
              <label class="field__label" for="loops-ret">Min retention <output class="tabular" data-slot="ret-out" for="loops-ret">${p.min_retention}%</output></label>
              <input id="loops-ret" name="min_retention" type="range" min="0" max="100" step="5" value="${p.min_retention}" aria-describedby="loops-ret-hint">
              <span class="field__hint" id="loops-ret-hint">Smallest hop ÷ largest hop</span>
            </div>
            <div class="field">
              <label class="field__label" for="loops-len">Max hops</label>
              <select class="select input--w-xs" id="loops-len" name="max_cycle_length">
                ${[2, 3, 4, 5, 6, 7, 8].map((d) => html`<option value="${d}" ${d === p.max_cycle_length ? html`selected` : ""}>${d}</option>`)}
              </select>
            </div>
            <div class="field">
              <label class="field__label" for="loops-inst">Instances per loop</label>
              <input class="input input--num input--w-sm" id="loops-inst" name="max_instances_per_loop" type="number" min="1" max="500" step="1" inputmode="numeric" value="${p.max_inst}"
                aria-describedby="loops-inst-hint">
              <span class="field__hint" id="loops-inst-hint">Repetitions listed per cycle</span>
            </div>
            <div class="row v-loops-form-actions">
              <button type="submit" class="btn btn--primary" data-slot="scan">${icon("refresh", { size: "sm" })}Scan</button>
              <button type="button" class="btn btn--ghost" data-action="reset">Reset</button>
            </div>
          </form>
          <p class="xsmall muted v-loops-meta" data-slot="meta" aria-live="polite"></p>
        </div>
      </section>
      <div data-slot="notices"></div>
      <div data-slot="kpis">${skeleton({ kind: "kpis", rows: 5 })}</div>
      <div class="v-loops-main" data-slot="main">
        <section class="card v-loops-listcard" aria-labelledby="loops-list-title">
          <div class="card__head">
            <h2 class="card__title" id="loops-list-title">Detected loops <span class="badge badge--neutral tabular" data-slot="count">0</span></h2>
          </div>
          <div class="v-loops-listtools">
            <div class="input-group">${icon("search", { size: "sm" })}
              <input class="input input--sm" type="search" name="q" data-slot="q" aria-label="Filter loops by entity" placeholder="Filter by entity…" value="${p.q}"></div>
            <label class="sr-only" for="loops-sort">Sort loops</label>
            <select class="select select--sm" id="loops-sort" data-slot="sort">
              ${SORTS.map((s) => html`<option value="${s.value}" ${s.value === p.sort ? html`selected` : ""}>${s.label}</option>`)}
            </select>
          </div>
          <div class="v-loops-list" data-slot="list" role="listbox" aria-labelledby="loops-list-title" tabindex="0">${skeleton({ kind: "text", rows: 5 })}</div>
        </section>
        <section class="card v-loops-detail" aria-labelledby="loops-detail-title">
          <div class="card__head"><div class="min-w-0"><h2 class="card__title" id="loops-detail-title" data-slot="detail-title">Loop</h2>
            <p class="card__sub" data-slot="detail-sub"></p></div>
            <div class="legend"><span class="legend__item"><span class="legend__swatch legend__swatch--round sw-statement" aria-hidden="true"></span>Has statement</span>
              <span class="legend__item"><span class="legend__swatch legend__swatch--round sw-details" aria-hidden="true"></span>Details only</span>
              <span class="legend__item xsmall muted">Arc width ∝ hop amount · red = leakage vs previous hop</span></div>
          </div>
          <div class="card__body stack" data-slot="detail">${skeleton({ kind: "block" })}</div>
        </section>
      </div>
    </div>`
  );
  const slot = (n) => el.querySelector(`[data-slot="${n}"]`);
  const form = slot("form");

  const statusOf = (id) => {
    const e = ctx.store.entity(id);
    if (!e) return "details";
    if (e.is_system || String(e.canonical_name || "").startsWith("UNRESOLVED_")) return "system";
    return e.has_statement ? "statement" : "details";
  };

  // ------------------------------------------------------------------------------------------ render
  function drawNotices() {
    if (!data?.truncated) {
      render(slot("notices"), "");
      return;
    }
    render(
      slot("notices"),
      html`<div class="callout callout--warn" role="status">${icon("alert")}<div><strong>Scan truncated — results are incomplete.</strong>
        The search stopped early because ${REASONS[data.truncation_reason] || data.truncation_reason || "a safety limit was hit"}
        after ${plural(data.loop_count ?? loops.length, "loop")}. The highest-value loops are reported first; narrow the duration,
        raise the minimum retention or lower the max hops to scan exhaustively.</div></div>`
    );
  }

  function drawMeta() {
    if (!data) return;
    slot("meta").textContent = `Scanned ${int(data.candidate_events)} candidate transfers in ${Number(data.elapsed_s || 0).toFixed(2)} s · max ${int(data.max_cycle_length ?? p.max_cycle_length)} hops · system parties (cash, charges, unresolved) excluded.`;
  }

  function drawKpis() {
    if (!loops.length) {
      render(slot("kpis"), "");
      return;
    }
    const ids = new Set();
    loops.forEach((l) => (l.entity_ids || []).forEach((id) => ids.add(id)));
    const cycled = loops.reduce((s, l) => s + (Number(l.min_amount) || 0), 0);
    const leaked = loops.reduce((s, l) => s + (Number(l.total_leakage) || 0), 0);
    const repeats = loops.reduce((s, l) => s + (Number(l.instance_count) || 1), 0);
    const medRet = median(loops.map((l) => Number(l.retention_ratio) || 0));
    const fastest = Math.min(...loops.map((l) => Number(l.duration_hours) || 0));
    const high = loops.filter((l) => l.retention_ratio >= 0.9).length;
    render(
      slot("kpis"),
      kpiGrid(
        [
          { label: "Loops", value: int(loops.length), sub: data?.truncated ? "scan truncated" : `${plural(high, "loop")} ≥ 90% retained`, tone: high ? "critical" : "", icon: "repeat" },
          { label: "Value round-tripped", value: inrCompact(cycled), title: `${inr(cycled)} — sum of the smallest hop of each loop`, sub: "smallest hop per loop" },
          { label: "Median retention", value: pct(medRet), sub: `threshold ${p.min_retention}%` },
          { label: "Fastest loop", value: hours(fastest), sub: "first to last hop" },
          { label: "Leakage", value: inrCompact(leaked), title: `${inr(leaked)} kept back by intermediaries across the representative loops`, sub: "kept by intermediaries" },
          { label: "Instances", value: int(repeats), sub: repeats > loops.length ? `${plural(repeats - loops.length, "repeat")} of the same cycles` : "no repeated cycles" },
          { label: "Entities involved", value: int(ids.size), icon: "users" },
        ],
        { compact: true, label: "Loop summary" }
      )
    );
  }

  function filterSort() {
    const q = p.q.trim().toLowerCase();
    const sorter = (SORTS.find((s) => s.value === p.sort) || SORTS[0]).fn;
    shown = loops.filter((l) => !q || (l.entities || []).some((n) => String(n).toLowerCase().includes(q))).sort(sorter);
  }

  function chainText(l) {
    const names = l.entities || [];
    return names.join(" → ");
  }

  function drawList() {
    filterSort();
    slot("count").textContent = shown.length === loops.length ? int(loops.length) : `${int(shown.length)} / ${int(loops.length)}`;
    const list = slot("list");
    if (!loops.length) {
      render(list, emptyState({
        icon: "repeat",
        title: "No circular loops",
        message: "No round trips matched the duration and retention thresholds. Try a longer duration or a lower minimum retention.",
        compact: true,
      }));
      list.removeAttribute("aria-activedescendant");
      return;
    }
    if (!shown.length) {
      render(list, emptyState({ icon: "search", title: "No loops match the filter", action: { label: "Clear filter", id: "clear-q" }, compact: true }));
      return;
    }
    if (!shown.some((l) => l.loop_id === selectedId)) selectedId = shown[0].loop_id;
    render(
      list,
      html`${shown.map((l, i) => html`<div class="v-loops-item" role="option" id="loop-opt-${l.loop_id}" data-loop="${l.loop_id}"
          aria-selected="${l.loop_id === selectedId ? "true" : "false"}" aria-posinset="${i + 1}" aria-setsize="${shown.length}">
        <div class="row-between">
          <span class="strong">${l.hop_count}-hop loop</span>
          <span class="badge badge--${retentionTone(l.retention_ratio)} tabular" title="Smallest hop ÷ largest hop">${pct(l.retention_ratio)} retained</span>
        </div>
        <div class="v-loops-chain">${chainText(l)}</div>
        <div class="v-loops-item-meta">
          <span class="tabular strong" title="${inr(l.max_amount)}">${inrCompact(l.max_amount)}</span>
          <span>${hours(l.duration_hours)}</span>
          ${Number(l.total_leakage) > 0 ? html`<span class="tabular" title="Leakage ${inr(l.total_leakage)}">−${inrCompact(l.total_leakage)}</span>` : ""}
          ${(l.instance_count || 1) > 1 ? html`<span class="v-loops-inst" title="${plural(l.instance_count, "instance")}, ${int(l.disjoint_instance_count)} without shared transactions">×${int(l.instance_count)}${l.instances_truncated ? "+" : ""}</span>` : ""}
          <span>${date(l.start_date)}${l.end_date && l.end_date !== l.start_date ? html` – ${date(l.end_date)}` : ""}</span>
        </div>
      </div>`)}`
    );
    list.setAttribute("aria-activedescendant", `loop-opt-${selectedId}`);
  }

  function drawDetail() {
    const box = slot("detail");
    dia?.destroy();
    dia = null;
    const base = loops.find((x) => x.loop_id === selectedId);
    if (!base || !shown.length) {
      slot("detail-title").textContent = "Loop";
      slot("detail-sub").textContent = "";
      render(box, emptyState({ icon: "repeat", title: loops.length ? "Select a loop" : "Nothing to show", message: loops.length ? "Pick a loop from the list to see its diagram and hops." : "", compact: true }));
      return;
    }
    const insts = base.instances || [];
    if (instIdx >= insts.length) instIdx = 0;
    const l = instanceView(base, instIdx);
    const pos = shown.indexOf(base) + 1;
    const instNote = insts.length > 1 ? ` · instance ${instIdx + 1} of ${int(insts.length)}${base.instances_truncated ? "+" : ""}` : "";
    slot("detail-title").textContent = `${l.hop_count}-hop loop · ${l.entities?.[0] || ""}`;
    slot("detail-sub").textContent = `Loop ${pos} of ${shown.length} · ${date(l.start_date)}${l.end_date !== l.start_date ? ` – ${date(l.end_date)}` : ""} · ${hours(l.duration_hours)}${instNote}`;
    const ids = l.entity_ids || [];
    const hops = l.hops || [];
    const leaks = base.intermediary_leakage || [];
    const leakTotal = Number(l.total_leakage ?? l.max_amount - l.min_amount) || 0;
    render(
      box,
      html`${kpiGrid(
        [
          { label: "Largest hop", value: inrCompact(l.max_amount), title: inr(l.max_amount) },
          { label: "Smallest hop", value: inrCompact(l.min_amount), title: inr(l.min_amount) },
          { label: "Retention", value: pct(l.retention_ratio), tone: l.retention_ratio >= 0.9 ? "critical" : l.retention_ratio >= 0.7 ? "warn" : "" },
          { label: "Leakage", value: inrCompact(leakTotal), title: inr(leakTotal),
            sub: l.leakage_pct != null ? `${Number(l.leakage_pct).toFixed(1)}% of the first hop` : "kept back along the loop" },
          { label: "Duration", value: hours(l.duration_hours) },
          { label: "Instances", value: int(base.instance_count || insts.length || 1), title: `${int(base.disjoint_instance_count || 0)} without shared transactions`,
            sub: base.total_instance_volume ? `${inrCompact(base.total_instance_volume)} total volume` : "", tone: (base.instance_count || 1) > 1 ? "warn" : "" },
        ],
        { compact: true, label: "Selected loop" }
      )}
      <figure class="v-loops-figure">
        <div data-slot="dia" class="v-loops-dia-host"></div>
        <figcaption class="sr-only">${loopSummary(l)}</figcaption>
      </figure>
      <div class="table-wrap">
        <table class="table table--compact v-loops-hops">
          <caption class="sr-only">Hops of the selected loop${instNote} in time order</caption>
          <thead><tr><th scope="col" class="num">#</th><th scope="col">When</th><th scope="col">From → To</th>
            <th scope="col" class="num">Amount</th><th scope="col" class="num">Leakage</th><th scope="col" class="num">Gap</th><th scope="col">Channel</th><th scope="col">UTR</th></tr></thead>
          <tbody>${hops.map((h, i) => {
            const leak = Number(h.leakage);
            return html`<tr data-hop="${i}">
              <td class="num tabular">${i + 1}</td>
              <td class="nowrap tabular">${h.timestamp && !/T00:00(:00)?$/.test(h.timestamp) ? dateTime(h.timestamp) : h.date ? date(h.date) : html`<span class="muted" title="Only the first and last dates of a repeated instance are known">—</span>`}</td>
              <td class="v-loops-fromto"><a href="${ctx.href("profile", { entity: ids[i] })}">${h.from}</a>
                <span class="muted" aria-hidden="true"> → </span><span class="sr-only"> to </span><a href="${ctx.href("profile", { entity: ids[(i + 1) % Math.max(1, l.hop_count)] })}">${h.to}</a></td>
              <td class="num tabular strong">${inr(h.amount)}</td>
              <td class="num tabular ${leak > 0 ? "is-neg" : leak < 0 ? "is-pos" : ""}" title="${h.leakage == null ? "First hop" : `${h.from} kept back ${inr(h.leakage)}`}">${h.leakage == null ? "—" : html`${inr(-leak, { sign: true })}${h.leakage_pct != null ? html`<span class="dg__sub">${Number(h.leakage_pct).toFixed(1)}%</span>` : ""}`}</td>
              <td class="num tabular">${h.hours_since_previous_hop == null ? "—" : hours(h.hours_since_previous_hop)}</td>
              <td>${h.channel ? html`<span class="badge badge--neutral">${h.channel}</span>` : "—"}</td>
              <td class="mono xsmall">${h.utr || "—"}</td>
            </tr>`;
          })}</tbody>
        </table>
      </div>
      ${leaks.length && instIdx === 0
        ? html`<div class="stack-sm"><h3 class="subhead">Leakage by intermediary</h3>
          <div class="table-wrap"><table class="table table--compact">
            <caption class="sr-only">Amount each intermediary received, forwarded and kept back</caption>
            <thead><tr><th scope="col">Intermediary</th><th scope="col" class="num">Received</th><th scope="col" class="num">Forwarded</th><th scope="col" class="num">Kept back</th><th scope="col" class="num">%</th></tr></thead>
            <tbody>${leaks.map((x) => html`<tr><td><a href="${ctx.href("profile", { entity: x.entity_id })}">${x.entity_name}</a></td>
              <td class="num tabular">${inr(x.received)}</td><td class="num tabular">${inr(x.forwarded)}</td>
              <td class="num tabular ${Number(x.leakage) > 0 ? "is-neg" : ""}">${inr(x.leakage)}</td>
              <td class="num tabular">${x.leakage_pct == null ? "—" : `${Number(x.leakage_pct).toFixed(1)}%`}</td></tr>`)}</tbody></table></div></div>`
        : ""}
      ${insts.length > 1
        ? html`<div class="stack-sm"><h3 class="subhead" id="loops-inst-title">Instances of this cycle</h3>
          <p class="xsmall muted">The same entity cycle repeated ${plural(base.instance_count || insts.length, "time")}${base.instances_truncated ? ` (only the first ${int(insts.length)} are listed — raise “Instances per loop”)` : ""};
            ${int(base.disjoint_instance_count || 0)} share no transaction with another. Select a row to draw it.</p>
          <div class="table-wrap table-wrap--max"><table class="table table--compact v-loops-insts" aria-labelledby="loops-inst-title">
            <thead><tr><th scope="col" class="num">#</th><th scope="col">Start</th><th scope="col">End</th><th scope="col" class="num">Largest</th>
              <th scope="col" class="num">Retention</th><th scope="col" class="num">Leakage</th><th scope="col" class="num">Duration</th><th scope="col">UTRs</th></tr></thead>
            <tbody>${insts.map((it, k) => html`<tr data-inst="${k}" tabindex="0" aria-selected="${k === instIdx ? "true" : "false"}">
              <td class="num tabular">${k + 1}</td>
              <td class="nowrap tabular">${it.start_timestamp ? dateTime(it.start_timestamp) : date(it.start_date)}</td>
              <td class="nowrap tabular">${it.end_timestamp ? dateTime(it.end_timestamp) : date(it.end_date)}</td>
              <td class="num tabular">${inr(it.max_amount)}</td>
              <td class="num tabular">${pct(it.retention_ratio)}</td>
              <td class="num tabular">${inr(it.leakage)}</td>
              <td class="num tabular">${hours(it.duration_hours)}</td>
              <td class="mono xsmall v-loops-utrs">${(it.utrs || []).filter(Boolean).join(", ") || "—"}</td></tr>`)}</tbody></table></div></div>`
        : ""}
      <div class="row-wrap v-loops-actions">
        <a class="btn btn--sm" href="${ctx.href("transactions", { ids: hops.map((h) => h.tx_id).filter(Boolean).join(",") })}">${icon("list", { size: "sm" })}Show ${plural(hops.length, "transaction")}</a>
        <a class="btn btn--sm" href="${ctx.href("bidirectional", { a: ids[0], b: ids[1] })}" title="Bidirectional flow ${l.entities?.[0] || ""} ⇄ ${l.entities?.[1] || ""}">${icon("swap", { size: "sm" })}<span class="truncate">${truncate(l.entities?.[0] || "", 16)} ⇄ ${truncate(l.entities?.[1] || "", 16)}</span></a>
        <a class="btn btn--sm" href="${ctx.href("network", { entity: ids[0] })}">${icon("network", { size: "sm" })}Show in network</a>
      </div>`
    );
    const host = box.querySelector('[data-slot="dia"]');
    compact = host.clientWidth > 0 && host.clientWidth < 520;
    dia = renderLoopDiagram(host, l, {
      compact,
      statusOf,
      caption: insts.length > 1 ? `instance ${instIdx + 1} of ${insts.length}` : "",
      onEntity: (id) => ctx.navigate("profile", { entity: id }),
    });
  }

  function selectInstance(k, { focus = false } = {}) {
    instIdx = k;
    ctx.setParams({ inst: k ? String(k) : null });
    drawDetail();
    if (focus) slot("detail").querySelector(`tr[data-inst="${k}"]`)?.focus({ preventScroll: true });
  }

  function select(id, { focusList = false, push = false } = {}) {
    if (!id) return;
    if (id !== selectedId) instIdx = 0;
    selectedId = id;
    slot("list").querySelectorAll("[data-loop]").forEach((o) => o.setAttribute("aria-selected", o.dataset.loop === id ? "true" : "false"));
    slot("list").setAttribute("aria-activedescendant", `loop-opt-${id}`);
    slot("list").querySelector(`[data-loop="${CSS.escape(id)}"]`)?.scrollIntoView({ block: "nearest" });
    if (focusList) slot("list").focus({ preventScroll: true });
    ctx.setParams({ loop: id, inst: instIdx ? String(instIdx) : null }, { push });
    drawDetail();
  }

  // ------------------------------------------------------------------------------------------ load
  async function load() {
    loadCtl?.abort();
    const ac = new AbortController();
    loadCtl = ac;
    const onAbort = () => ac.abort();
    ctx.signal.addEventListener("abort", onAbort, { once: true });
    const seq = (loadSeq += 1);
    const first = !data;
    const btn = slot("scan");
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    slot("main").setAttribute("aria-busy", "true");
    slot("main").classList.add("is-loading");
    try {
      const res = await ctx.api.get(
        "trace/loops",
        { max_days: p.max_days, min_retention: (p.min_retention / 100).toFixed(2), max_cycle_length: p.max_cycle_length, max_instances_per_loop: p.max_inst },
        { signal: ac.signal }
      );
      if (!ctx.isCurrent() || seq !== loadSeq) return;
      data = res;
      loops = res.loops || [];
      drawNotices();
      drawMeta();
      drawKpis();
      drawList();
      drawDetail();
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent() || seq !== loadSeq) return;
      if (first) {
        render(slot("kpis"), "");
        render(slot("list"), errorState(err, { retryId: "retry", title: "Could not scan for loops", compact: true }));
        render(slot("detail"), "");
      } else {
        ctx.toast.error(err, { title: "Loop scan failed" });
      }
    } finally {
      ctx.signal.removeEventListener("abort", onAbort);
      if (seq === loadSeq) {
        btn.disabled = false;
        btn.removeAttribute("aria-busy");
        slot("main").removeAttribute("aria-busy");
        slot("main").classList.remove("is-loading");
      }
    }
  }

  // ------------------------------------------------------------------------------------------ actions
  function pushScanParams() {
    ctx.setParams({
      max_days: p.max_days === DEFAULTS.max_days ? null : String(p.max_days),
      min_retention: p.min_retention === DEFAULTS.min_retention ? null : (p.min_retention / 100).toFixed(2),
      max_cycle_length: p.max_cycle_length === DEFAULTS.max_cycle_length ? null : String(p.max_cycle_length),
      max_inst: p.max_inst === DEFAULTS.max_inst ? null : String(p.max_inst),
    });
  }

  function readForm() {
    p.max_days = Math.round(clampNum(form.elements.max_days.value, 0, 3650, DEFAULTS.max_days));
    p.min_retention = Math.round(clampNum(form.elements.min_retention.value, 0, 100, DEFAULTS.min_retention));
    p.max_cycle_length = Math.round(clampNum(form.elements.max_cycle_length.value, 2, 8, DEFAULTS.max_cycle_length));
    p.max_inst = Math.round(clampNum(form.elements.max_instances_per_loop.value, 1, 500, DEFAULTS.max_inst));
    syncControls();
  }

  function syncControls() {
    form.elements.max_days.value = String(p.max_days);
    form.elements.min_retention.value = String(p.min_retention);
    form.elements.max_cycle_length.value = String(p.max_cycle_length);
    form.elements.max_instances_per_loop.value = String(p.max_inst);
    slot("ret-out").textContent = `${p.min_retention}%`;
    slot("q").value = p.q;
    slot("sort").value = p.sort;
  }

  function exportSvg() {
    const l = loops.find((x) => x.loop_id === selectedId);
    if (!dia || !l) {
      ctx.toast("Select a loop to export", { type: "warn" });
      return;
    }
    const f = downloadText(`loop_${l.hop_count}hop_${String(l.entities?.[0] || "loop")}_${l.start_date}.svg`, dia.exportSvg(`CypherTrace circular loop — ${loopSummary(l)} — case ${ctx.caseId}`), "image/svg+xml");
    ctx.toast.success(`Saved ${f.filename}`);
  }

  const autoScan = debounce(() => {
    readForm();
    pushScanParams();
    load();
  }, 450);
  ctx.onCleanup(() => autoScan.cancel());

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    autoScan.cancel();
    readForm();
    pushScanParams();
    load();
  });
  form.addEventListener("input", (e) => {
    if (e.target.name === "min_retention") slot("ret-out").textContent = `${e.target.value}%`;
  });
  form.addEventListener("change", () => autoScan());

  const onSearch = debounce(() => {
    p.q = slot("q").value;
    ctx.setParams({ q: p.q || null });
    drawList();
    drawDetail();
  }, 200);
  ctx.onCleanup(() => onSearch.cancel());
  slot("q").addEventListener("input", onSearch);
  slot("sort").addEventListener("change", () => {
    p.sort = slot("sort").value;
    ctx.setParams({ sort: p.sort === "value" ? null : p.sort });
    drawList();
    drawDetail();
  });

  ctx.onCleanup(delegate(slot("list"), "click", "[data-loop]", (e, o) => select(o.dataset.loop, { focusList: true, push: true })));
  slot("list").addEventListener("keydown", (e) => {
    if (!shown.length) return;
    const i = Math.max(0, shown.findIndex((l) => l.loop_id === selectedId));
    let j = null;
    if (e.key === "ArrowDown") j = Math.min(shown.length - 1, i + 1);
    else if (e.key === "ArrowUp") j = Math.max(0, i - 1);
    else if (e.key === "Home") j = 0;
    else if (e.key === "End") j = shown.length - 1;
    else if (e.key === "PageDown") j = Math.min(shown.length - 1, i + 8);
    else if (e.key === "PageUp") j = Math.max(0, i - 8);
    if (j == null) return;
    e.preventDefault();
    if (j !== i) select(shown[j].loop_id);
  });
  ctx.onCleanup(delegate(el, "click", "[data-action]", (e, b) => {
    const a = b.dataset.action;
    if (a === "retry") load();
    else if (a === "export-svg") exportSvg();
    else if (a === "clear-q") {
      p.q = "";
      slot("q").value = "";
      ctx.setParams({ q: null });
      drawList();
      drawDetail();
    } else if (a === "reset") {
      Object.assign(p, { max_days: DEFAULTS.max_days, min_retention: DEFAULTS.min_retention, max_cycle_length: DEFAULTS.max_cycle_length, max_inst: DEFAULTS.max_inst });
      syncControls();
      pushScanParams();
      load();
    }
  }));
  ctx.onCleanup(delegate(slot("detail"), "click", "tr[data-inst]", (e, tr) => selectInstance(Number(tr.dataset.inst), { focus: true })));
  ctx.onCleanup(delegate(slot("detail"), "keydown", "tr[data-inst]", (e, tr) => {
    const k = Number(tr.dataset.inst);
    const n = slot("detail").querySelectorAll("tr[data-inst]").length;
    let j = null;
    if (e.key === "Enter" || e.key === " ") j = k;
    else if (e.key === "ArrowDown") j = Math.min(n - 1, k + 1);
    else if (e.key === "ArrowUp") j = Math.max(0, k - 1);
    if (j == null) return;
    e.preventDefault();
    selectInstance(j, { focus: true });
  }));
  // hovering a hop row highlights its arc
  ctx.onCleanup(delegate(slot("detail"), "pointerover", "tr[data-hop]", (e, tr) => dia?.highlightHop(Number(tr.dataset.hop))));
  ctx.onCleanup(delegate(slot("detail"), "pointerleave", ".v-loops-hops", () => dia?.highlightHop(-1), true));

  // re-render the diagram when crossing the compact breakpoint (no refetch)
  if (typeof ResizeObserver === "function") {
    const ro = new ResizeObserver(() => {
      const host = slot("detail").querySelector('[data-slot="dia"]');
      if (!host || !dia) return;
      const next = host.clientWidth > 0 && host.clientWidth < 520;
      if (next !== compact) drawDetail();
    });
    ro.observe(slot("detail"));
    ctx.onCleanup(() => ro.disconnect());
  }
  ctx.onCleanup(() => dia?.destroy());

  ctx.onData([TOPICS.analytics, TOPICS.entities], () => load());

  ctx.registerCommands([
    { id: "loops.scan", title: "Rescan circular loops", group: "Loops", icon: "refresh", run: () => load() },
    { id: "loops.export-svg", title: "Export selected loop as SVG", group: "Loops", icon: "download", when: () => Boolean(dia), run: exportSvg },
    { id: "loops.retention-90", title: "Loops: only ≥ 90% retention", group: "Loops", keywords: ["retention", "threshold"],
      run: () => { p.min_retention = 90; syncControls(); pushScanParams(); load(); } },
  ]);

  await load();

  return {
    update(params) {
      const next = readParams(params);
      const rescan = next.max_days !== p.max_days || next.min_retention !== p.min_retention || next.max_cycle_length !== p.max_cycle_length || next.max_inst !== p.max_inst;
      const reinst = next.inst !== instIdx;
      const relist = next.q !== p.q || next.sort !== p.sort;
      const reselect = next.loop && next.loop !== selectedId;
      p = next;
      syncControls();
      if (reselect) selectedId = next.loop;
      instIdx = next.inst;
      if (rescan) load();
      else if (relist || reselect || reinst) {
        drawList();
        drawDetail();
      }
    },
    unmount() {
      loadCtl?.abort();
    },
  };
}
