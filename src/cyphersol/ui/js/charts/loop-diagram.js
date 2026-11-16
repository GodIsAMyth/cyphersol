/* Circular loop diagram (A → B → C → A) as hand-written SVG.
 *
 *   const dia = renderLoopDiagram(containerEl, loop, {
 *     statusOf: (entityId) => "statement" | "details" | "system",   // node fill (.fill-<cls>)
 *     onEntity: (entityId, name) => …,                             // node click / Enter / Space
 *     compact: containerEl.clientWidth < 520,
 *     caption: "Instance 2 of 5",                                 // optional sub-line in the ring centre
 *   });
 *   dia.svg ; dia.exportSvg(title) ; dia.destroy()
 *
 * Entities sit clockwise on a ring in hop order, starting at 12 o'clock. Each hop is a ring arc with an arrowhead;
 * its stroke width is proportional to the hop amount relative to the loop's largest hop, so value decay (retention)
 * reads at a glance. Arc labels (amount, date/time, leakage vs the previous hop and the UTR when known — P1, TF-13)
 * sit inside the ring with a surface-coloured plate; hop numbers
 * sit on the arc. Nodes are keyboard-focusable links (role="link"). The svg carries an aria-label summary; the view
 * renders the hop table as the full text alternative. Colours come only from token classes (theme-safe). */
import { delegate, html, uid } from "../core/dom.js";
import { clockTime, date, hours, inr, inrCompact, pct, truncate } from "../core/format.js";
import { inlineSvgForExport } from "./tree.js";

const TAU = Math.PI * 2;
const f1 = (v) => Number(v).toFixed(1);

function initials(name) {
  const parts = String(name || "?").replace(/[^A-Za-z0-9 ]+/g, " ").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) return "?";
  return (parts[0][0] + (parts[1] ? parts[1][0] : parts[0][1] || "")).toUpperCase();
}

const timeOf = (hop) => clockTime(hop.timestamp);

function shortDate(d) {
  const s = date(d);
  return /^\d{2} \w{3} \d{4}$/.test(s) ? s.slice(0, 6) : s;
}

/** Plain-text summary used for aria-label and the export <title>. */
export function loopSummary(loop) {
  const hops = loop.hops || [];
  const chain = hops
    .map((h, i) => `${i === 0 ? h.from : ""}${i === 0 ? " → " : ""}${h.to} (${inr(h.amount)}${h.date ? `, ${date(h.date)}` : ""}${h.utr ? `, UTR ${h.utr}` : ""}${Number(h.leakage) > 0 ? `, leakage ${inr(h.leakage)}` : ""})`)
    .join(" → ");
  const leak = Number(loop.total_leakage) > 0 ? ` Total leakage ${inr(loop.total_leakage)}.` : "";
  return `${loop.hop_count}-hop circular loop, ${pct(loop.retention_ratio)} of value retained over ${hours(loop.duration_hours)}: ${chain}.${leak}`;
}

const shortUtr = (u) => (String(u).length > 12 ? `…${String(u).slice(-10)}` : String(u));
const leakText = (h) => (Number(h.leakage) > 0 ? `−${inrCompact(h.leakage)}${h.leakage_pct != null ? ` (${Number(h.leakage_pct).toFixed(1)}%)` : ""}` : "");

export function renderLoopDiagram(container, loop, { statusOf = () => "details", onEntity = null, compact = false, caption = "" } = {}) {
  const titleId = uid("loop-dia");
  const hops = loop.hops || [];
  const ids = (loop.entity_ids || []).slice(0, loop.hop_count);
  const names = (loop.entities || []).slice(0, loop.hop_count);
  const n = Math.max(ids.length, hops.length, 1);

  const W = compact ? 360 : 640;
  const H = compact ? 380 : 470;
  const cx = W / 2;
  const cy = H / 2 + (compact ? 4 : 2);
  const R = compact ? 118 : 158;
  const nodeR = compact ? 17 : 21;
  const labelR = compact ? R * 0.55 : R - 50;
  const nameMax = compact ? 16 : 26;
  const maxAmt = Math.max(...hops.map((h) => Number(h.amount) || 0), 1);

  const angle = (i) => -Math.PI / 2 + (TAU * i) / n;
  const pt = (a, r = R) => ({ x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) });
  const gap = (nodeR + 7) / R;

  const arcs = hops.map((h, i) => {
    const a0 = angle(i) + gap;
    const a1 = angle(i + 1) - gap;
    const p0 = pt(a0);
    const p1 = pt(a1);
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const ratio = (Number(h.amount) || 0) / maxAmt;
    const sw = 1.75 + 4.25 * ratio;
    // arrowhead at p1 along the clockwise tangent
    const tx = -Math.sin(a1);
    const ty = Math.cos(a1);
    const nx = Math.cos(a1);
    const ny = Math.sin(a1);
    const len = 8 + sw;
    const half = 4 + sw * 0.6;
    const bx = p1.x - tx * len;
    const by = p1.y - ty * len;
    const arrow = `M${f1(p1.x + tx * 2)} ${f1(p1.y + ty * 2)}L${f1(bx + nx * half)} ${f1(by + ny * half)}L${f1(bx - nx * half)} ${f1(by - ny * half)}Z`;
    const pe = pt(a1 - (len - 2) / R);
    const am = (a0 + a1) / 2;
    const badge = pt(am);
    const lab = pt(am, labelR);
    const amt = inrCompact(h.amount);
    const when = h.date ? `${shortDate(h.date)}${timeOf(h) ? ` ${timeOf(h)}` : ""}` : "";
    const leak = leakText(h);
    const utr = h.utr && !compact ? `UTR ${shortUtr(h.utr)}` : "";
    const lines = [when, leak, utr].filter(Boolean);
    const plateW = Math.max(amt.length * 7.4, ...lines.map((t) => t.length * 6)) + 14;
    const plateH = 20 + 12 * lines.length;
    return html`<g class="v-loops-hop" data-hop="${i}">
      <title>Hop ${i + 1}: ${h.from} → ${h.to}, ${inr(h.amount)}${h.date ? ` on ${date(h.date)}` : ""}${timeOf(h) ? ` ${timeOf(h)}` : ""}${h.channel ? ` via ${h.channel}` : ""}${h.utr ? `, UTR ${h.utr}` : ""}${Number(h.leakage) > 0 ? `, ${inr(h.leakage)} kept back by ${h.from}` : ""}${h.hours_since_previous_hop != null ? `, ${hours(h.hours_since_previous_hop)} after the previous hop` : ""}</title>
      <path class="v-loops-arc" d="M${f1(p0.x)} ${f1(p0.y)}A${R} ${R} 0 ${large} 1 ${f1(pe.x)} ${f1(pe.y)}" stroke-width="${f1(sw)}"></path>
      <path class="v-loops-arrow" d="${arrow}"></path>
      <g transform="translate(${f1(badge.x)} ${f1(badge.y)})"><circle class="v-loops-badge" r="${compact ? 8 : 9}"></circle>
        <text class="v-loops-badge-text" y="3.5" text-anchor="middle">${i + 1}</text></g>
      <g transform="translate(${f1(lab.x)} ${f1(lab.y)})">
        <rect class="v-loops-plate" x="${f1(-plateW / 2)}" y="${f1(-plateH / 2)}" width="${f1(plateW)}" height="${f1(plateH)}" rx="6"></rect>
        <text class="v-loops-amt" y="${f1(-plateH / 2 + 14)}" text-anchor="middle">${amt}</text>
        ${lines.map((t, k) => html`<text class="${t === leak ? "v-loops-leak" : t === utr ? "v-loops-utr" : "v-loops-when"}" y="${f1(-plateH / 2 + 27 + 12 * k)}" text-anchor="middle">${t}</text>`)}
      </g>
    </g>`;
  });

  const nodes = ids.map((id, i) => {
    const a = angle(i);
    const p = pt(a);
    const name = names[i] || id;
    const above = Math.sin(a) < -0.35;
    const ly = above ? -(nodeR + 9) : nodeR + 16;
    const cls = statusOf(id) || "details";
    return html`<g class="v-loops-node" data-entity="${id}" data-name="${name}" transform="translate(${f1(p.x)} ${f1(p.y)})"
        ${onEntity ? html`role="link" tabindex="0"` : ""} aria-label="${name}${onEntity ? " — open profile" : ""}">
      <title>${name}</title>
      <circle class="v-loops-node-focus" r="${nodeR + 5}"></circle>
      ${i === 0 ? html`<circle class="v-loops-node-start" r="${nodeR + 3.5}"></circle>` : ""}
      <circle class="v-loops-node-dot fill-${cls}" r="${nodeR}"></circle>
      <text class="v-loops-node-initials" y="4" text-anchor="middle">${initials(name)}</text>
      <text class="v-loops-node-name" y="${ly}" text-anchor="middle">${truncate(name, nameMax)}</text>
      ${i === 0 ? html`<text class="v-loops-node-tag" y="${above ? ly - 13 : ly + 13}" text-anchor="middle">START / END</text>` : ""}
    </g>`;
  });

  const center = compact ? "" : html`<g class="v-loops-center" transform="translate(${cx} ${cy})" aria-hidden="true">
    <text class="v-loops-center-big" y="-6" text-anchor="middle">${pct(loop.retention_ratio)}</text>
    <text class="v-loops-center-sub" y="12" text-anchor="middle">retained · ${hours(loop.duration_hours)}</text>
    ${Number(loop.total_leakage) > 0 ? html`<text class="v-loops-center-sub" y="27" text-anchor="middle">leakage ${inrCompact(loop.total_leakage)}</text>` : ""}
    ${caption ? html`<text class="v-loops-center-sub" y="${Number(loop.total_leakage) > 0 ? 42 : 27}" text-anchor="middle">${caption}</text>` : ""}
  </g>`;

  container.replaceChildren();
  const holder = document.createElement("div");
  holder.className = "v-loops-dia";
  holder.innerHTML = String(html`<svg class="v-loops-dia-svg" viewBox="0 0 ${W} ${H}" role="group" aria-labelledby="${titleId}">
    <title id="${titleId}">${caption ? `${caption}. ` : ""}${loopSummary(loop)}</title>
    <circle class="v-loops-ring" cx="${cx}" cy="${cy}" r="${R}"></circle>
    ${center}<g class="v-loops-hops">${arcs}</g><g class="v-loops-nodes">${nodes}</g></svg>`);
  container.appendChild(holder);
  const svg = holder.firstElementChild;

  const offs = [];
  if (onEntity) {
    const activate = (g) => onEntity(g.dataset.entity, g.dataset.name);
    offs.push(delegate(svg, "click", ".v-loops-node", (e, g) => activate(g)));
    offs.push(delegate(svg, "keydown", ".v-loops-node", (e, g) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        activate(g);
      }
    }));
  }
  // hover/focus a hop label highlights its arc (and vice versa via CSS :hover on the group)
  offs.push(delegate(svg, "pointerover", ".v-loops-hop", (e, g) => g.classList.add("is-hot")));
  offs.push(delegate(svg, "pointerout", ".v-loops-hop", (e, g) => g.classList.remove("is-hot")));

  return {
    svg,
    highlightHop(i) {
      svg.querySelectorAll(".v-loops-hop").forEach((g) => g.classList.toggle("is-hot", Number(g.dataset.hop) === i));
    },
    exportSvg: (title) => inlineSvgForExport(svg, { title: title || loopSummary(loop), width: W, height: H }),
    destroy() {
      offs.splice(0).forEach((off) => off());
      container.replaceChildren();
    },
  };
}
