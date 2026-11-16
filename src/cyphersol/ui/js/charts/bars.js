/* Hand-written bar charts for the risk views (mules, profile, entities). No libraries, CSP-safe:
 *   - every chart returns SafeHTML built with html`` (all labels escaped);
 *   - colours come from token classes (.fill-credit, .fill-1, .fill-critical …) or var(--…) — never hex;
 *   - geometry uses SVG presentation attributes (x/y/width/height), never style="…".
 *
 * Exports
 *   contributionWaterfall(assessment, { params, compact })      mule score waterfall (w·S·100 steps → composite)
 *   barList(items, { segments, format, max, label, empty })     horizontal (optionally stacked) bar list
 *   monthlyFlowChart(rows, { width, height, label })            grouped inflow/outflow columns + net line (SVG)
 *   monthlyFlowTable(rows)                                      text alternative for monthlyFlowChart
 *   mountResponsive(el, draw, { onDraw })                       re-render draw(width) when el resizes -> off()
 *   attachChartTooltip(root, selector)                          tooltip for [data-tip] marks (hover + focus) -> off()
 *
 * Row-based charts (waterfall, bar list) keep their text in HTML so it stays legible at 375 px; only the bar
 * track is an SVG stretched with preserveAspectRatio="none" (non-scaling strokes for marker lines).
 * Classes use the shared "v-risk-" prefix and are styled in styles/views/risk.css.
 */
import { MULE_PARAMS, tierClass, tierFromScore } from "../core/constants.js";
import { html, render } from "../core/dom.js";
import { DASH, inr, inrCompact, month, num } from "../core/format.js";

const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
const n = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);
const r2 = (v) => Math.round(v * 100) / 100;

/* ------------------------------------------------------------------------------------------------ waterfall */

/**
 * Contribution waterfall for one mule assessment. Each of the 10 parameters is a step that starts where the
 * previous one ended (cumulative), drawn over a faint "headroom" bar (its maximum, weight × 100). The last row
 * is the composite score with the 50 / 80 tier thresholds. Steps sum to the composite (w·S·100).
 * Returns SafeHTML; rows are real text, so screen readers read "label, contribution of max".
 */
export function contributionWaterfall(a, { params = MULE_PARAMS, compact = false } = {}) {
  const contrib = a?.weighted_contributions || {};
  const scores = a?.parameters || {};
  const total = n(a?.composite_score);
  const tier = a?.risk_tier || tierFromScore(total);
  let cum = 0;
  const rows = params.map((p) => {
    const max = p.weight * 100;
    const c = contrib[p.key] != null ? n(contrib[p.key]) : n(scores[p.key]) * max;
    const start = cum;
    cum += c;
    const s = scores[p.key];
    const tip = `${p.label}: ${num(c, 2)} of ${num(max, 0)} pts (S = ${s == null ? DASH : num(s, 2)}, weight ${num(max, 0)}%)`;
    return { p, c, max, start, s, tip };
  });
  const tierCls = tierClass(tier);
  return html`<div class="v-risk-wf ${compact ? "v-risk-wf--compact" : ""}" role="list" aria-label="Score contributions: ${rows.length} parameters summing to ${num(total, 1)}">
    <div class="v-risk-wf__head" aria-hidden="true">
      <span>Parameter</span><span class="v-risk-wf__scale"><span>0</span><span>50</span><span>80</span><span>100</span></span><span class="num">Pts</span>
    </div>
    ${rows.map(
      (r) => html`<div class="v-risk-wf__row" role="listitem">
        <span class="v-risk-wf__label truncate" title="${r.tip}">${r.p.label}<span class="sr-only">: ${num(r.c, 2)} of ${num(r.max, 0)} points</span></span>
        <svg class="v-risk-wf__track" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true" focusable="false"><title>${r.tip}</title>
          <rect class="v-risk-wf__room" x="${r2(r.start)}" y="1" width="${r2(Math.max(0, r.max))}" height="8" rx="0.6"></rect>
          ${r.c > 0 ? html`<rect class="v-risk-wf__step" x="${r2(r.start)}" y="1" width="${r2(Math.max(0.35, r.c))}" height="8" rx="0.6"></rect>` : ""}
          <line class="v-risk-wf__guide" x1="${r2(r.start + r.c)}" x2="${r2(r.start + r.c)}" y1="0" y2="10" vector-effect="non-scaling-stroke"></line>
        </svg>
        <span class="num tabular v-risk-wf__val ${r.c > 0 ? "" : "muted"}" aria-hidden="true">${r.c > 0 ? `+${num(r.c, 1)}` : num(0, 1)}</span>
      </div>`
    )}
    <div class="v-risk-wf__row v-risk-wf__row--total" role="listitem">
      <span class="v-risk-wf__label strong">Composite score<span class="sr-only">: ${num(total, 1)}, ${tier}</span></span>
      <svg class="v-risk-wf__track" viewBox="0 0 100 10" preserveAspectRatio="none" aria-hidden="true" focusable="false">
        <rect class="v-risk-wf__room" x="0" y="1" width="100" height="8" rx="0.6"></rect>
        <rect class="fill-${tierCls}" x="0" y="1" width="${r2(clamp(total, 0, 100))}" height="8" rx="0.6"></rect>
        <line class="v-risk-wf__thr" x1="50" x2="50" y1="0" y2="10" vector-effect="non-scaling-stroke"></line>
        <line class="v-risk-wf__thr" x1="80" x2="80" y1="0" y2="10" vector-effect="non-scaling-stroke"></line>
      </svg>
      <span class="num tabular strong v-risk-wf__val" aria-hidden="true">${num(total, 1)}</span>
    </div>
  </div>`;
}

/* ------------------------------------------------------------------------------------------------ bar list */

/**
 * Horizontal bar list. items: [{ label, sub?, href?, values: { key: number }, title? }]
 * segments: [{ key, label, cls }] stacked in order (cls = token suffix: "credit", "debit", "1".."8").
 * format(value) formats the right-hand total (default inrCompact). max defaults to the largest row total.
 */
export function barList(items, { segments = [{ key: "value", label: "Amount", cls: "1" }], format = inrCompact, exact = inr, max = null, label = "Bar chart", empty = "No data" } = {}) {
  const list = (items || []).filter(Boolean);
  if (!list.length) return html`<p class="muted small">${empty}</p>`;
  const totals = list.map((it) => segments.reduce((s, sg) => s + Math.max(0, n(it.values?.[sg.key])), 0));
  const top = max || Math.max(1, ...totals);
  return html`<ol class="v-risk-bars" aria-label="${label}">
    ${list.map((it, i) => {
      let x = 0;
      const parts = segments
        .map((sg) => ({ sg, v: Math.max(0, n(it.values?.[sg.key])) }))
        .filter((p) => p.v > 0);
      const detail = segments.length > 1
        ? segments.map((sg) => `${sg.label} ${exact(n(it.values?.[sg.key]))}`).join(" · ")
        : exact(totals[i]);
      return html`<li class="v-risk-bars__row" title="${it.title || `${it.label}: ${detail}`}">
        <span class="v-risk-bars__label">
          ${it.href ? html`<a class="truncate" href="${it.href}">${it.label}</a>` : html`<span class="truncate">${it.label}</span>`}
          ${it.sub ? html`<span class="v-risk-bars__sub truncate">${it.sub}</span>` : ""}
        </span>
        <span class="num tabular v-risk-bars__val">${format(totals[i])}<span class="sr-only"> (${detail})</span></span>
        <svg class="v-risk-bars__track" viewBox="0 0 100 6" preserveAspectRatio="none" aria-hidden="true" focusable="false">
          <rect class="v-risk-bars__bg" x="0" y="0" width="100" height="6" rx="1"></rect>
          ${parts.map((p) => {
            const w = (p.v / top) * 100;
            const out = html`<rect class="fill-${p.sg.cls}" x="${r2(x)}" y="0" width="${r2(Math.max(0.4, w))}" height="6"></rect>`;
            x += w;
            return out;
          })}
        </svg>
      </li>`;
    })}
  </ol>`;
}

/* ---------------------------------------------------------------------------------------- monthly flow chart */

function niceStep(range, ticks = 4) {
  const raw = range / Math.max(1, ticks);
  const mag = 10 ** Math.floor(Math.log10(raw || 1));
  const f = raw / mag;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 2.5 ? 2.5 : f <= 5 ? 5 : 10) * mag;
}

const shortMonth = (m) => {
  const s = month(m); // "Apr 2026"
  const parts = String(s).split(" ");
  return parts.length === 2 ? `${parts[0]} ’${parts[1].slice(2)}` : s;
};

/**
 * Grouped inflow (credit) / outflow (debit) columns per month with a net-flow line.
 * rows: [{ month: "2026-04", credit, debit, net, tx_count }]. width is the rendered pixel width (use
 * mountResponsive so text stays at its real size). Each month has a hover hit area with data-tip; pair with
 * monthlyFlowTable for the accessible numbers.
 */
export function monthlyFlowChart(rows, { width = 640, height = 260, label = "Monthly inflow and outflow" } = {}) {
  const data = fillMonths((rows || []).map((r) => ({ ...r, credit: n(r.credit), debit: n(r.debit), net: r.net != null ? n(r.net) : n(r.credit) - n(r.debit) })));
  if (!data.length) return html`<p class="muted small">No monthly activity.</p>`;
  const W = Math.max(280, Math.round(width));
  const narrow = W < 480;
  const m = { top: 12, right: 8, bottom: 26, left: narrow ? 50 : 62 };
  const iw = W - m.left - m.right;
  const ih = height - m.top - m.bottom;
  const hi = Math.max(1, ...data.map((d) => Math.max(d.credit, d.debit, d.net)));
  const rawLo = Math.min(0, ...data.map((d) => d.net));
  const lo = rawLo < -hi * 0.02 ? rawLo : 0; // ignore a negligible negative net so the axis is not padded
  const step = niceStep(hi - lo, narrow ? 3 : 4);
  const yMax = Math.ceil(hi / step) * step;
  const yMin = Math.floor(lo / step) * step;
  const y = (v) => m.top + ih - ((v - yMin) / (yMax - yMin || 1)) * ih;
  const ticks = [];
  for (let t = yMin; t <= yMax + step / 2; t += step) ticks.push(t);
  const band = iw / data.length;
  const gap = Math.min(12, band * 0.22);
  const bw = Math.max(2, Math.min(28, (band - gap) / 2));
  const labelEvery = Math.max(1, Math.ceil(data.length / Math.max(1, Math.floor(iw / (narrow ? 44 : 56)))));
  const cx = (i) => m.left + band * i + band / 2;
  const netPts = data.map((d, i) => `${r2(cx(i))},${r2(y(d.net))}`).join(" ");
  const summary = `${label}: ${data.length} months, ${month(data[0].month)} to ${month(data[data.length - 1].month)}. ` +
    `Peak inflow ${inrCompact(Math.max(...data.map((d) => d.credit)))}, peak outflow ${inrCompact(Math.max(...data.map((d) => d.debit)))}.`;
  return html`<svg class="v-risk-chart" width="${W}" height="${height}" viewBox="0 0 ${W} ${height}" role="img" aria-label="${summary}">
    ${ticks.map((t) => html`<line class="${t === 0 ? "svg-axis" : "svg-grid"}" x1="${m.left}" x2="${W - m.right}" y1="${r2(y(t))}" y2="${r2(y(t))}"></line>
      <text class="svg-text-3 v-risk-chart__tick" x="${m.left - 6}" y="${r2(y(t))}" dy="0.32em" text-anchor="end">${inrCompact(t)}</text>`)}
    ${data.map((d, i) => {
      const x0 = cx(i) - bw - 1;
      const tip = `${month(d.month)}|Inflow ${inr(d.credit)}|Outflow ${inr(d.debit)}|Net ${inr(d.net)}${d.tx_count != null ? `|${d.tx_count} txns` : ""}`;
      return html`<g class="v-risk-chart__col" data-tip="${tip}">
        <rect class="v-risk-chart__hit" x="${r2(m.left + band * i)}" y="${m.top}" width="${r2(band)}" height="${ih}"></rect>
        <rect class="fill-credit" x="${r2(x0)}" y="${r2(y(d.credit))}" width="${r2(bw)}" height="${r2(Math.max(0, y(0) - y(d.credit)))}" rx="2"></rect>
        <rect class="fill-debit" x="${r2(x0 + bw + 2)}" y="${r2(y(d.debit))}" width="${r2(bw)}" height="${r2(Math.max(0, y(0) - y(d.debit)))}" rx="2"></rect>
        ${i % labelEvery === 0 ? html`<text class="svg-text-3 v-risk-chart__tick" x="${r2(cx(i))}" y="${height - 8}" text-anchor="middle">${shortMonth(d.month)}</text>` : ""}
      </g>`;
    })}
    ${data.length > 1 ? html`<polyline class="v-risk-chart__net" points="${netPts}" fill="none"></polyline>` : ""}
    ${data.map((d, i) => html`<circle class="v-risk-chart__netpt" cx="${r2(cx(i))}" cy="${r2(y(d.net))}" r="3"></circle>`)}
  </svg>`;
}

/** Insert zero rows for months with no activity so the time axis is continuous ("2026-02", "2026-04" -> + "2026-03"). */
export function fillMonths(rows) {
  const keyed = rows.filter((r) => /^\d{4}-\d{2}/.test(String(r.month || ""))).sort((a, b) => String(a.month).localeCompare(String(b.month)));
  if (keyed.length < 2 || keyed.length !== rows.length) return rows;
  const by = new Map(keyed.map((r) => [String(r.month).slice(0, 7), r]));
  let [y, m] = String(keyed[0].month).slice(0, 7).split("-").map(Number);
  const last = String(keyed[keyed.length - 1].month).slice(0, 7);
  const out = [];
  for (let guard = 0; guard < 240; guard += 1) {
    const key = `${y}-${String(m).padStart(2, "0")}`;
    out.push(by.get(key) || { month: key, credit: 0, debit: 0, net: 0, tx_count: 0, abb: null, _filled: true });
    if (key === last) return out;
    m += 1;
    if (m > 12) { m = 1; y += 1; }
  }
  return rows;
}

/** Accessible table alternative for monthlyFlowChart (also the precise numbers). */
export function monthlyFlowTable(rows, { abb = true } = {}) {
  const data = rows || [];
  const showAbb = abb && data.some((r) => r.abb != null);
  return html`<div class="table-wrap"><table class="table table--compact">
    <caption class="sr-only">Monthly inflow, outflow and net flow</caption>
    <thead><tr><th scope="col">Month</th><th scope="col" class="num">Inflow (Cr)</th><th scope="col" class="num">Outflow (Dr)</th>
      <th scope="col" class="num">Net</th><th scope="col" class="num">Txns</th>${showAbb ? html`<th scope="col" class="num">Daily ABB</th>` : ""}</tr></thead>
    <tbody>${data.map((r) => {
      const net = r.net != null ? n(r.net) : n(r.credit) - n(r.debit);
      return html`<tr><th scope="row" class="nowrap">${month(r.month)}</th>
        <td class="num tabular is-credit">${inr(r.credit)}</td><td class="num tabular is-debit">${inr(r.debit)}</td>
        <td class="num tabular ${net < 0 ? "is-neg" : ""}">${inr(net, { sign: true })}</td>
        <td class="num tabular">${r.tx_count == null ? DASH : num(r.tx_count)}</td>
        ${showAbb ? html`<td class="num tabular">${inr(r.abb)}</td>` : ""}</tr>`;
    })}</tbody>
  </table></div>`;
}

/* ------------------------------------------------------------------------------------------------ helpers */

/**
 * Render draw(width) into el now and whenever el's width changes (rAF-throttled). Returns off().
 * Use for SVG charts whose text must stay at its real pixel size.
 */
export function mountResponsive(el, draw, { onDraw = null } = {}) {
  let lastW = -1;
  let raf = 0;
  const paint = () => {
    raf = 0;
    const w = Math.floor(el.clientWidth || el.getBoundingClientRect().width || 0);
    if (!w || Math.abs(w - lastW) < 4) return;
    lastW = w;
    render(el, draw(w));
    onDraw?.(el);
  };
  const ro = typeof ResizeObserver === "function" ? new ResizeObserver(() => { if (!raf) raf = requestAnimationFrame(paint); }) : null;
  ro?.observe(el);
  paint();
  if (lastW < 0) raf = requestAnimationFrame(paint);
  return () => {
    ro?.disconnect();
    if (raf) cancelAnimationFrame(raf);
  };
}

/**
 * Lightweight tooltip for chart marks carrying data-tip="Title|line|line". Shows on hover and keyboard
 * focus, hides on leave/blur/Escape. Text is set via textContent (never parsed). Returns off().
 */
export function attachChartTooltip(root, selector = "[data-tip]") {
  const tip = document.createElement("div");
  tip.className = "tooltip v-risk-tip";
  tip.setAttribute("role", "tooltip");
  tip.hidden = true;
  document.body.appendChild(tip);
  let current = null;
  const show = (mark, x, y) => {
    const lines = String(mark.dataset.tip || "").split("|");
    tip.replaceChildren(
      ...lines.map((line, i) => {
        const d = document.createElement("div");
        if (i === 0) d.className = "strong";
        d.textContent = line;
        return d;
      })
    );
    tip.hidden = false;
    const r = tip.getBoundingClientRect();
    const left = clamp(x + 12, 8, window.innerWidth - r.width - 8);
    const top = y - r.height - 12 < 8 ? y + 16 : y - r.height - 12;
    tip.style.left = `${left}px`;
    tip.style.top = `${top}px`;
    if (current && current !== mark) current.classList.remove("is-hot");
    current = mark;
    mark.classList.add("is-hot");
  };
  const hide = () => {
    tip.hidden = true;
    current?.classList.remove("is-hot");
    current = null;
  };
  const onMove = (e) => {
    const mark = e.target.closest?.(selector);
    if (mark && root.contains(mark)) show(mark, e.clientX, e.clientY);
    else if (current) hide();
  };
  const onFocus = (e) => {
    const mark = e.target.closest?.(selector);
    if (!mark || !root.contains(mark)) return;
    const r = mark.getBoundingClientRect();
    show(mark, r.left + r.width / 2, r.top + 8);
  };
  const onKey = (e) => {
    if (e.key === "Escape" && current) hide();
  };
  root.addEventListener("pointermove", onMove);
  root.addEventListener("pointerleave", hide);
  root.addEventListener("focusin", onFocus);
  root.addEventListener("focusout", hide);
  root.addEventListener("keydown", onKey);
  return () => {
    root.removeEventListener("pointermove", onMove);
    root.removeEventListener("pointerleave", hide);
    root.removeEventListener("focusin", onFocus);
    root.removeEventListener("focusout", hide);
    root.removeEventListener("keydown", onKey);
    tip.remove();
  };
}
