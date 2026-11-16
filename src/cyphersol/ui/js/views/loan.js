/* CypherEdge · Loan (#/case/{id}/loan?foir=55&rate=11.5&tenure=60&income_factor=&abb_factor=&income_override=&account=<account_key>)
 *
 * P1 (EDGE-21/22/25/26): ABB is period-bounded (each account only over its own statement period; consolidated
 * days sum the accounts covering that day) — consolidated_coverage.segments / full / partial / uncovered days and
 * per-month accounts_covered show the coverage per day; per-account coverage_gaps and overdrawn days; penal events
 * (min-balance, TOD / overdrawing, penal / late-payment charges) with penal_event_breakdown; cash_withdrawal_pct;
 * and the income basis (income_basis) driven by income_factor / abb_factor / income_override (blank = server default).
 *
 * Loan eligibility appraisal (GET edge/loan?foir&rate&tenure): credit grade, max eligible EMI / loan,
 * FOIR utilisation, consolidated and per-account ABB (daily + fixed-date 5/10/15/20/25), detected EMI
 * obligations (recurring NACH/ECS/SI per lender), cheque / mandate bounces, and a hand-written SVG
 * monthly balance chart (min–max band + daily ABB + fixed-date ABB) with hover/keyboard tooltip and a
 * table alternative (the monthly breakdown table below it).
 */
import { isAbort } from "../core/api.js";
import { announce, delegate, html, render } from "../core/dom.js";
import { date, DASH, inr, inrCompact, int, month, num, pct, plural } from "../core/format.js";
import { TOPICS } from "../core/store.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { emptyState, errorState, pageHead, skeleton } from "../components/states.js";

export const styles = ["views/edge.css"];

const DEFAULTS = { foir: "55", rate: "11.5", tenure: "60" };
const LIMITS = {
  foir: { min: 1, max: 100, step: "0.5", label: "FOIR cap", unit: "%" },
  rate: { min: 0, max: 40, step: "0.05", label: "Interest rate", unit: "% p.a." },
  tenure: { min: 1, max: 480, step: "1", label: "Tenure", unit: "months" },
  income_factor: { min: 0, max: 1, step: "0.01", label: "Business income factor", unit: "× net business inflow", optional: true },
  abb_factor: { min: 0, max: 1, step: "0.01", label: "ABB income factor", unit: "× fixed-date ABB", optional: true },
  income_override: { min: 0, max: 1e11, step: "1", label: "Monthly income override", unit: "₹", optional: true },
};
/* blank optional inputs = the server's defaults (shown as placeholders once income_basis arrives) */
const OPTIONAL_DEFAULTS = { income_factor: "", abb_factor: "", income_override: "" };
const PENAL_LABEL = { MIN_BALANCE: "Minimum-balance charge", TOD: "Temporary overdraft / overdrawing", OVERDRAWN: "Overdrawn", PENAL: "Penal / late-payment charge" };
const penalLabel = (t) => PENAL_LABEL[String(t || "").toUpperCase()] || String(t || "Penal event").replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());

function gradeTone(g) {
  const s = String(g || "").toUpperCase();
  if (!s || s.includes("INSUFFICIENT")) return "";
  if (/\(A\+?\)|PRIME|EXCELLENT/.test(s)) return "success";
  if (/\(B\)|STANDARD/.test(s)) return "accent";
  if (/\(C\)|HIGH RISK|SUB/.test(s)) return "danger";
  return "warn";
}

function niceMax(v) {
  if (!(v > 0)) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

export async function mount(el, ctx) {
  const inputs = {
    foir: ctx.params.foir || DEFAULTS.foir,
    rate: ctx.params.rate || DEFAULTS.rate,
    tenure: ctx.params.tenure || DEFAULTS.tenure,
    income_factor: ctx.params.income_factor || "",
    abb_factor: ctx.params.abb_factor || "",
    income_override: ctx.params.income_override || "",
  };
  let account = ctx.params.account || "";
  let data = null;
  let ctl = null;
  let gen = 0;
  let ro = null;
  let tip = null;
  ctx.onCleanup(() => {
    ctl?.abort();
    ro?.disconnect();
    tip?.remove();
  });

  render(
    el,
    html`<div class="stack-lg">
      ${pageHead({
        title: "Loan eligibility",
        sub: "Banking-surrogate appraisal: average bank balance, FOIR on detected obligations, bounces and maximum eligible loan.",
      })}
      <form class="card v-loan-form" novalidate aria-label="Appraisal parameters">
        <div class="card__body v-loan-form__body">
          ${Object.entries(LIMITS).map(
            ([k, l]) => html`<div class="field">
              <label class="field__label" for="loan-${k}">${l.label} <span class="muted">(${l.unit})</span></label>
              <input class="input input--num" id="loan-${k}" name="${k}" type="number" inputmode="decimal" ${l.optional ? "" : html`required`}
                min="${l.min}" max="${l.max}" step="${l.step}" value="${inputs[k]}" aria-describedby="loan-${k}-err" ${l.optional ? html`placeholder="Default"` : ""} />
              <span class="field__error" id="loan-${k}-err" hidden></span>
            </div>`
          )}
          <div class="v-loan-form__actions">
            <button type="submit" class="btn btn--primary">${icon("refresh", { size: "sm" })}Recalculate</button>
            <button type="button" class="btn btn--ghost" data-action="loan-reset">Reset</button>
          </div>
        </div>
      </form>
      <div data-slot="body">${skeleton({ kind: "kpis", rows: 8 })}${skeleton({ kind: "block" })}</div>
    </div>`
  );
  const form = el.querySelector("form");
  const body = el.querySelector('[data-slot="body"]');

  /* ---------------------------------------------------------------- inputs */
  function validate() {
    let ok = true;
    for (const [k, l] of Object.entries(LIMITS)) {
      const input = form.elements[k];
      const err = form.querySelector(`#loan-${k}-err`);
      const v = Number(input.value);
      if (l.optional && input.value === "") {
        input.setAttribute("aria-invalid", "false");
        err.hidden = true;
        continue;
      }
      const bad = input.value === "" || !Number.isFinite(v) || v < l.min || v > l.max || (k === "tenure" && !Number.isInteger(v));
      input.setAttribute("aria-invalid", bad ? "true" : "false");
      err.hidden = !bad;
      err.textContent = bad ? `Enter ${k === "tenure" ? "a whole number " : ""}between ${l.min} and ${l.max}` : "";
      if (bad && ok) input.focus();
      ok = ok && !bad;
    }
    return ok;
  }

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    if (!validate()) return;
    for (const k of Object.keys(LIMITS)) inputs[k] = form.elements[k].value === "" && LIMITS[k].optional ? "" : String(Number(form.elements[k].value));
    ctx.setParams({
      foir: inputs.foir === DEFAULTS.foir ? null : inputs.foir,
      rate: inputs.rate === DEFAULTS.rate ? null : inputs.rate,
      tenure: inputs.tenure === DEFAULTS.tenure ? null : inputs.tenure,
      income_factor: inputs.income_factor || null,
      abb_factor: inputs.abb_factor || null,
      income_override: inputs.income_override || null,
    });
    load();
  });

  /* ---------------------------------------------------------------- loading */
  async function load() {
    ctl?.abort();
    ctl = new AbortController();
    const myGen = ++gen;
    body.setAttribute("aria-busy", "true");
    const btn = form.querySelector('button[type="submit"]');
    btn.setAttribute("aria-busy", "true");
    if (!data) render(body, html`${skeleton({ kind: "kpis", rows: 8 })}${skeleton({ kind: "block" })}`);
    try {
      const res = await ctx.api.get(
        "edge/loan",
        { foir: inputs.foir, rate: inputs.rate, tenure: inputs.tenure, income_factor: inputs.income_factor, abb_factor: inputs.abb_factor, income_override: inputs.income_override },
        { signal: ctl.signal }
      );
      if (!ctx.isCurrent() || myGen !== gen) return;
      data = res;
      draw();
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent() || myGen !== gen) return;
      data = null;
      ro?.disconnect();
      render(body, html`<div class="card"><div class="card__body">${errorState(err, { retryId: "loan-retry", title: "Could not compute loan eligibility" })}</div></div>`);
    } finally {
      if (myGen === gen) {
        body.removeAttribute("aria-busy");
        btn.removeAttribute("aria-busy");
      }
    }
  }

  /* ---------------------------------------------------------------- drawing */
  function draw() {
    const d = data;
    if (!d.months_analyzed && !(d.monthly_breakdown || []).length) {
      ro?.disconnect();
      render(body, html`<div class="card"><div class="card__body">${emptyState({
        icon: "bank", title: "No banking data to appraise",
        message: "Upload at least 3–6 months of bank statements to compute ABB, obligations and eligibility.",
        action: { label: "Upload statements", href: ctx.href("ingest") },
      })}</div></div>`);
      return;
    }
    const accounts = d.per_account_abb || [];
    if (account && !accounts.some((a) => a.account_key === account)) account = "";
    const income = Number(d.assessable_monthly_income) || 0;
    const emi = Number(d.monthly_existing_emi) || 0;
    const foirUsed = income > 0 ? emi / income : 0;
    const foirCap = (Number(d.foir_pct) || 0) / 100;
    const bounces = (d.inward_bounce_count || 0) + (d.outward_bounce_count || 0);
    const shortHistory = (d.months_analyzed || 0) < 6;
    const ib = d.income_basis || {};
    const cov = d.consolidated_coverage || {};
    const penal = d.penal_events || [];
    // blank optional inputs show the factors the server actually applied
    form.elements.income_factor.placeholder = ib.business_income_factor != null ? `${ib.business_income_factor} (default)` : "Default";
    form.elements.abb_factor.placeholder = ib.abb_income_factor != null ? `${ib.abb_income_factor} (default)` : "Default";
    form.elements.income_override.placeholder = "None";
    announce(`Eligibility calculated: max eligible loan ${inr(d.max_eligible_loan)}, max EMI ${inr(d.max_eligible_emi)}.`);

    render(
      body,
      html`<div class="stack-lg">
        ${kpiGrid(
          [
            { label: "Credit grade", value: String(d.credit_grade || DASH).replace(/_/g, " "), tone: gradeTone(d.credit_grade), icon: "shield",
              sub: `${plural(d.months_analyzed || 0, "month")} · ${date(d.period_start)} – ${date(d.period_end)}` },
            { label: "Max eligible loan", value: inrCompact(d.max_eligible_loan), title: inr(d.max_eligible_loan), tone: "accent", icon: "bank",
              sub: `${num(d.annual_interest_rate, 2)}% · ${int(d.tenure_months)} months` },
            { label: "Max eligible EMI", value: inrCompact(d.max_eligible_emi), title: inr(d.max_eligible_emi), sub: `at ${num(d.foir_pct, 1)}% FOIR` },
            { label: "Daily ABB (consolidated)", value: inrCompact(d.daily_abb), title: inr(d.daily_abb), sub: plural(accounts.length, "account") },
            { label: "Fixed-date ABB", value: inrCompact(d.fixed_date_abb), title: inr(d.fixed_date_abb), sub: "5th/10th/15th/20th/25th" },
            { label: "Existing EMI / month", value: inrCompact(emi), title: inr(emi), sub: plural((d.emi_obligations || []).filter((o) => o.active).length, "active obligation") },
            { label: "Assessable income / month", value: inrCompact(income), title: inr(income), sub: `avg inflow ${inrCompact(d.avg_monthly_inflow)}` },
            { label: "Bounces (I/W · O/W)", value: `${int(d.inward_bounce_count)} · ${int(d.outward_bounce_count)}`, tone: bounces ? "danger" : "success",
              sub: `${pct(d.bounce_rate_pct, { ratio: false })} of ${plural(d.attempted_debit_instruments || 0, "debit instrument")}` },
            { label: "Cash deposit share", value: pct(d.cash_deposit_pct, { ratio: false }), tone: d.cash_deposit_pct >= 30 ? "warn" : "", sub: "of total credits" },
            { label: "Cash withdrawal share", value: pct(d.cash_withdrawal_pct, { ratio: false }), sub: "of total debits" },
            { label: "Penal events", value: int(d.penal_event_count ?? penal.length), tone: (d.penal_event_count ?? penal.length) ? "danger" : "success",
              sub: Object.entries(d.penal_event_breakdown || {}).map(([k, n]) => `${penalLabel(k)} ${int(n)}`).join(" · ") || "no penal / overdraft charges" },
            { label: "Overdrawn days", value: int(d.overdrawn_days), tone: d.overdrawn_days ? "danger" : "success", sub: "end-of-day balance below zero" },
            { label: "Coverage", value: `${int(cov.full_coverage_days)} d`, tone: cov.partial_coverage_days || cov.uncovered_days ? "warn" : "",
              sub: `${int(cov.partial_coverage_days)} partial · ${int(cov.uncovered_days)} uncovered`, title: "Days on which every account's statement period covers the day" },
          ],
          { label: "Loan appraisal summary" }
        )}
        ${shortHistory
          ? html`<div class="callout callout--warn">${icon("alert")}<div><strong>Only ${plural(d.months_analyzed || 0, "month")} of history.</strong> Lenders typically appraise 6–12 months of statements; treat the grade and eligibility as indicative.</div></div>`
          : ""}

        ${incomeBlock(d, ib)}

        <section class="card" aria-labelledby="loan-foir-h">
          <div class="card__head"><div><h2 class="card__title" id="loan-foir-h">FOIR utilisation</h2>
            <p class="card__sub">Existing obligations as a share of assessable monthly income, against the ${num(d.foir_pct, 1)}% cap.</p></div></div>
          <div class="card__body stack-sm">
            <div class="v-loan-foir" role="img" aria-label="Existing EMI ${inr(emi)} is ${pct(foirUsed)} of assessable income ${inr(income)}; FOIR cap ${pct(foirCap)} leaves ${inr(d.max_eligible_emi)} per month for a new EMI.">
              <div class="v-loan-foir__track">
                <div class="v-loan-foir__used" data-w="${Math.min(100, foirUsed * 100).toFixed(2)}"></div>
                <div class="v-loan-foir__room" data-w="${Math.max(0, Math.min(100, (foirCap - foirUsed) * 100)).toFixed(2)}"></div>
                <div class="v-loan-foir__cap" data-x="${Math.min(100, foirCap * 100).toFixed(2)}"></div>
              </div>
            </div>
            <div class="legend small">
              <span class="legend__item"><span class="legend__swatch v-loan-sw-used"></span>Existing EMI ${inr(emi)} (${pct(foirUsed)})</span>
              <span class="legend__item"><span class="legend__swatch v-loan-sw-room"></span>Headroom for new EMI ${inr(d.max_eligible_emi)}</span>
              <span class="legend__item"><span class="v-loan-sw-cap" aria-hidden="true"></span>FOIR cap ${pct(foirCap)} of ${inr(income)}</span>
            </div>
            <p class="xsmall muted">Max EMI = assessable income × FOIR − existing EMI; max loan = present value of that EMI at ${num(d.annual_interest_rate, 2)}% p.a. over ${int(d.tenure_months)} months.</p>
          </div>
        </section>

        <section class="card" aria-labelledby="loan-chart-h">
          <div class="card__head"><div><h2 class="card__title" id="loan-chart-h">Monthly balances</h2>
            <p class="card__sub">Daily ABB and fixed-date ABB per month, with the month's minimum–maximum balance range.</p></div>
            ${accounts.length > 1
              ? html`<div class="field field--inline v-loan-series"><label class="field__label" for="loan-series">Series</label>
                  <select class="select select--sm" id="loan-series">
                    <option value="" ${account ? "" : html`selected`}>Consolidated (${plural(accounts.length, "account")})</option>
                    ${accounts.map((a) => html`<option value="${a.account_key}" ${a.account_key === account ? html`selected` : ""}>${a.account}</option>`)}
                  </select></div>`
              : ""}</div>
          <div class="card__body stack-sm">
            <div class="legend small" aria-hidden="true">
              <span class="legend__item"><span class="legend__swatch v-loan-sw-abb"></span>Daily ABB</span>
              <span class="legend__item"><span class="legend__swatch v-loan-sw-fixed"></span>Fixed-date ABB</span>
              <span class="legend__item"><span class="legend__swatch v-loan-sw-band"></span>Min–max balance</span>
            </div>
            <div class="v-loan-chart" data-slot="chart"></div>
            <p class="xsmall muted">Use ←/→ on the focused chart to step through months; the monthly cash-flow table below lists every value.</p>
          </div>
        </section>

        <section class="card" aria-labelledby="loan-monthly-h">
          <div class="card__head"><h2 class="card__title" id="loan-monthly-h">Monthly cash flow (consolidated)</h2></div>
          <div class="card__body card__body--flush"><div class="table-wrap">${monthlyTable(d)}</div></div>
        </section>

        <section class="card" aria-labelledby="loan-cov-h">
          <div class="card__head"><div><h2 class="card__title" id="loan-cov-h">Statement coverage</h2>
            <p class="card__sub">Which accounts' statement periods cover each day. ABB is period-bounded: a day only counts for the accounts whose statement covers it.</p></div></div>
          <div class="card__body stack-sm">${coverageBlock(d, cov)}</div>
        </section>

        <section class="card" aria-labelledby="loan-acc-h">
          <div class="card__head"><div><h2 class="card__title" id="loan-acc-h">ABB by account</h2>
            <p class="card__sub">Each account's end-of-day series over its own statement period only (gaps inside it are listed); the consolidated figure sums the covering accounts per day.</p></div></div>
          <div class="card__body card__body--flush"><div class="table-wrap">${accountTable(d)}</div></div>
        </section>

        <section class="card" aria-labelledby="loan-penal-h">
          <div class="card__head"><div><h2 class="card__title" id="loan-penal-h">Penal &amp; overdraft events</h2>
            <p class="card__sub">Minimum-balance charges, temporary overdraft / overdrawing and penal or late-payment charges found in the narrations.</p></div></div>
          <div class="card__body card__body--flush">${penalBlock(d, penal)}</div>
        </section>

        <div class="grid-2 v-loan-cols">
          <section class="card" aria-labelledby="loan-emi-h">
            <div class="card__head"><div><h2 class="card__title" id="loan-emi-h">EMI obligations detected</h2>
              <p class="card__sub">Recurring NACH / ECS / SI debits grouped by lender and amount band.</p></div></div>
            <div class="card__body card__body--flush">${emiBlock(d)}</div>
          </section>
          <section class="card" aria-labelledby="loan-bounce-h">
            <div class="card__head"><div><h2 class="card__title" id="loan-bounce-h">Bounces</h2>
              <p class="card__sub">Returned cheques / mandates. I/W = our debit returned; O/W = deposited instrument returned.</p></div></div>
            <div class="card__body card__body--flush">${bounceBlock(d)}</div>
          </section>
        </div>
      </div>`
    );
    body.querySelectorAll("[data-w]").forEach((n) => n.style.setProperty("--w", `${n.dataset.w}%`));
    body.querySelectorAll("[data-x]").forEach((n) => n.style.setProperty("--x", `${n.dataset.x}%`));
    body.querySelectorAll("[data-seg-w]").forEach((n) => n.style.setProperty("--seg-w", `${n.dataset.segW}%`));

    const chartEl = body.querySelector('[data-slot="chart"]');
    ro?.disconnect();
    let lastW = 0;
    ro = new ResizeObserver(() => {
      const w = Math.round(chartEl.clientWidth);
      if (w && w !== lastW) {
        lastW = w;
        drawChart(chartEl);
      }
    });
    ro.observe(chartEl);
    drawChart(chartEl);
  }

  function series() {
    const d = data;
    if (account) {
      const a = (d.per_account_abb || []).find((x) => x.account_key === account);
      if (a) return { label: a.account, rows: a.monthly || [] };
    }
    return { label: "Consolidated", rows: d.monthly_breakdown || [] };
  }

  function drawChart(host) {
    const { label, rows } = series();
    const W = Math.max(280, host.clientWidth || 640);
    const H = W < 480 ? 220 : 280;
    const m = { top: 16, right: W < 480 ? 12 : 92, bottom: 30, left: 64 };
    const iw = W - m.left - m.right;
    const ih = H - m.top - m.bottom;
    if (!rows.length) {
      render(host, emptyState({ icon: "chart", title: "No monthly balances", compact: true }));
      return;
    }
    const vmax = niceMax(Math.max(...rows.map((r) => Math.max(r.max_balance || 0, r.daily_abb || 0, r.fixed_date_abb || 0))));
    const vmin = Math.min(0, ...rows.map((r) => Math.min(r.min_balance ?? 0, r.daily_abb ?? 0)));
    const lo = vmin < 0 ? -niceMax(-vmin) : 0;
    const y = (v) => m.top + ih - ((v - lo) / (vmax - lo)) * ih;
    const step = rows.length > 1 ? iw / (rows.length - 1) : 0;
    const x = (i) => m.left + (rows.length > 1 ? i * step : iw / 2);
    const ticks = Array.from({ length: 5 }, (_, i) => lo + ((vmax - lo) * i) / 4);
    const line = (key) => rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r[key] || 0).toFixed(1)}`).join("");
    const band =
      rows.map((r, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(r.max_balance ?? r.daily_abb ?? 0).toFixed(1)}`).join("") +
      rows.slice().reverse().map((r, j) => `L${x(rows.length - 1 - j).toFixed(1)},${y(r.min_balance ?? r.daily_abb ?? 0).toFixed(1)}`).join("") + "Z";
    const labelEvery = Math.max(1, Math.ceil(rows.length / Math.max(1, Math.floor(iw / 70))));
    const lastI = rows.length - 1;
    const hitW = rows.length > 1 ? step : iw;
    const latest = rows[lastI];
    const summary = `${label} monthly balances, ${month(rows[0].month)} to ${month(latest.month)}. Daily ABB ranges ${inr(Math.min(...rows.map((r) => r.daily_abb || 0)))} to ${inr(Math.max(...rows.map((r) => r.daily_abb || 0)))}; latest month ${month(latest.month)} daily ABB ${inr(latest.daily_abb)}, fixed-date ABB ${inr(latest.fixed_date_abb)}.`;

    render(
      host,
      html`<svg class="v-loan-svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" role="img" tabindex="0" aria-label="${summary}">
        <g aria-hidden="true">
          ${ticks.map((t) => html`<line class="svg-grid" x1="${m.left}" x2="${m.left + iw}" y1="${y(t)}" y2="${y(t)}"></line>
            <text class="svg-text-3 v-loan-tick" x="${m.left - 8}" y="${y(t) + 4}" text-anchor="end">${inrCompact(t)}</text>`)}
          <line class="svg-axis" x1="${m.left}" x2="${m.left + iw}" y1="${y(lo)}" y2="${y(lo)}"></line>
          ${rows.map((r, i) => (i % labelEvery === 0 || i === lastI) && !(i !== lastI && lastI - i < labelEvery)
            ? html`<text class="svg-text-3 v-loan-tick" x="${x(i)}" y="${H - 8}" text-anchor="${rows.length === 1 ? "middle" : i === 0 ? "start" : i === lastI ? "end" : "middle"}">${month(r.month)}</text>`
            : "")}
          <path class="v-loan-band" d="${band}"></path>
          <path class="v-loan-line v-loan-line--fixed" d="${line("fixed_date_abb")}"></path>
          <path class="v-loan-line v-loan-line--abb" d="${line("daily_abb")}"></path>
          ${rows.map((r, i) => html`<circle class="v-loan-dot v-loan-dot--abb" cx="${x(i)}" cy="${y(r.daily_abb || 0)}" r="4"></circle>`)}
          ${m.right > 40
            ? html`<text class="svg-text-2 v-loan-direct" x="${x(lastI) + 8}" y="${y(latest.daily_abb || 0) + 4}">Daily ${inrCompact(latest.daily_abb)}</text>
              <text class="svg-text-2 v-loan-direct" x="${x(lastI) + 8}" y="${y(latest.fixed_date_abb || 0) + (Math.abs(y(latest.fixed_date_abb || 0) - y(latest.daily_abb || 0)) < 14 ? 18 : 4)}">Fixed ${inrCompact(latest.fixed_date_abb)}</text>`
            : ""}
          <line class="v-loan-cross" data-cross x1="0" x2="0" y1="${m.top}" y2="${m.top + ih}" visibility="hidden"></line>
          ${rows.map((r, i) => {
            // Hit areas are clamped to the plot area so hovering the card padding never opens a month tooltip
            const hx0 = Math.max(m.left, x(i) - hitW / 2);
            const hx1 = Math.min(m.left + iw, x(i) + hitW / 2);
            return html`<rect class="v-loan-hit" data-i="${i}" x="${hx0}" y="${m.top}" width="${Math.max(1, hx1 - hx0)}" height="${ih}"></rect>`;
          })}
        </g>
      </svg>`
    );
    const svg = host.querySelector("svg");
    const cross = svg.querySelector("[data-cross]");
    let active = -1;
    const show = (i, anchor) => {
      if (i < 0 || i >= rows.length) return;
      active = i;
      const r = rows[i];
      cross.setAttribute("x1", x(i));
      cross.setAttribute("x2", x(i));
      cross.setAttribute("visibility", "visible");
      const full = !account ? (data.monthly_breakdown || []).find((b) => b.month === r.month) : null;
      if (!tip) {
        tip = document.createElement("div");
        tip.className = "tooltip v-loan-tip";
        tip.setAttribute("role", "status");
        document.body.appendChild(tip);
      }
      render(
        tip,
        html`<div class="strong">${month(r.month)} · ${label}</div>
          <dl class="v-loan-tip__kv">
            <dt><span class="v-loan-key v-loan-sw-abb"></span>Daily ABB</dt><dd class="tabular">${inr(r.daily_abb)}</dd>
            <dt><span class="v-loan-key v-loan-sw-fixed"></span>Fixed-date ABB</dt><dd class="tabular">${inr(r.fixed_date_abb)}</dd>
            <dt>Min / max</dt><dd class="tabular">${inrCompact(r.min_balance)} / ${inrCompact(r.max_balance)}</dd>
            ${full ? html`<dt>Credits / debits</dt><dd class="tabular">${inrCompact(full.total_credits)} / ${inrCompact(full.total_debits)}</dd>
              <dt>EMI outflow</dt><dd class="tabular">${inrCompact(full.emi_outflow)}</dd>` : ""}
          </dl>`
      );
      const rect = svg.getBoundingClientRect();
      const px = anchor?.clientX ?? rect.left + x(i);
      const py = anchor?.clientY ?? rect.top + y(r.daily_abb || 0);
      const tw = tip.offsetWidth || 220;
      const left = px + 14 + tw > window.innerWidth - 8 ? px - tw - 14 : px + 14;
      tip.style.left = `${Math.max(8, left)}px`;
      tip.style.top = `${Math.max(8, Math.min(window.innerHeight - (tip.offsetHeight || 120) - 8, py - 20))}px`;
      tip.hidden = false;
    };
    const hide = () => {
      active = -1;
      cross.setAttribute("visibility", "hidden");
      if (tip) tip.hidden = true;
    };
    svg.addEventListener("pointermove", (e) => {
      const hit = e.target.closest?.("[data-i]");
      if (hit) show(Number(hit.dataset.i), e);
    });
    svg.addEventListener("pointerleave", hide);
    svg.addEventListener("blur", hide);
    svg.addEventListener("focus", () => show(active >= 0 ? active : lastI));
    svg.addEventListener("keydown", (e) => {
      const k = e.key;
      if (k === "ArrowRight" || k === "ArrowLeft" || k === "Home" || k === "End") {
        e.preventDefault();
        const cur = active >= 0 ? active : lastI;
        const next = k === "Home" ? 0 : k === "End" ? lastI : Math.max(0, Math.min(lastI, cur + (k === "ArrowRight" ? 1 : -1)));
        show(next);
      } else if (k === "Escape") hide();
    });
  }

  function monthlyTable(d) {
    const rows = d.monthly_breakdown || [];
    return html`<table class="table table--compact v-loan-table">
      <caption class="sr-only">Monthly cash flow and balances (consolidated); data table for the monthly balances chart</caption>
      <thead><tr>
        <th scope="col">Month</th><th scope="col" class="num">Credits</th><th scope="col" class="num">Net business credits</th>
        <th scope="col" class="num">Debits</th><th scope="col" class="num">Net flow</th><th scope="col" class="num">EMI outflow</th>
        <th scope="col" class="num">Daily ABB</th><th scope="col" class="num">Fixed-date ABB</th><th scope="col" class="num">Min balance</th>
        <th scope="col" class="num">Max balance</th><th scope="col" class="num">Days</th><th scope="col" class="num">Full-coverage days</th>
        <th scope="col">Accounts covered</th><th scope="col" class="num">Penal</th><th scope="col" class="num">Txns</th>
      </tr></thead>
      <tbody>${rows.map((r) => html`<tr>
        <th scope="row" class="nowrap">${month(r.month)}</th>
        <td class="num is-credit">${inr(r.total_credits)}</td><td class="num">${inr(r.net_business_credits)}</td>
        <td class="num is-debit">${inr(r.total_debits)}</td>
        <td class="num ${r.net_cash_flow < 0 ? "is-neg" : ""}">${inr(r.net_cash_flow, { sign: true })}</td>
        <td class="num">${inr(r.emi_outflow, { zero: DASH })}</td>
        <td class="num strong">${inr(r.daily_abb)}</td><td class="num">${inr(r.fixed_date_abb)}</td>
        <td class="num ${r.min_balance < 0 ? "is-neg" : ""}">${inr(r.min_balance)}</td><td class="num">${inr(r.max_balance)}</td>
        <td class="num">${int(r.days_covered)}</td>
        <td class="num ${r.full_coverage_days != null && r.full_coverage_days < r.days_covered ? "is-neg" : ""}">${r.full_coverage_days == null ? DASH : int(r.full_coverage_days)}</td>
        <td class="small">${(r.accounts_covered || []).join(", ") || DASH}</td>
        <td class="num ${r.penal_events ? "is-neg strong" : ""}">${int(r.penal_events || 0)}</td>
        <td class="num">${int(r.tx_count)}</td>
      </tr>`)}</tbody>
    </table>`;
  }

  function accountTable(d) {
    const accs = d.per_account_abb || [];
    if (!accs.length) return emptyState({ icon: "bank", title: "No per-account series", compact: true });
    return html`<table class="table table--compact">
      <caption class="sr-only">Average bank balance per account and consolidated</caption>
      <thead><tr><th scope="col">Account</th><th scope="col">Period</th><th scope="col" class="num">Days covered</th><th scope="col">Gaps</th>
        <th scope="col" class="num">Opening balance</th><th scope="col" class="num">Daily ABB</th><th scope="col" class="num">Fixed-date ABB</th>
        <th scope="col" class="num">Overdrawn days</th><th scope="col" class="num">Statements</th></tr></thead>
      <tbody>
        ${accs.map((a) => html`<tr>
          <th scope="row"><span class="strong">${a.account}</span>${a.account_type ? html` <span class="badge badge--neutral">${a.account_type}</span>` : ""}<div class="xsmall muted mono">${a.account_key}</div></th>
          <td class="nowrap small">${date(a.period_start)} – ${date(a.period_end)}</td>
          <td class="num">${int(a.days_covered)}</td>
          <td class="small">${(a.coverage_gaps || []).length
            ? html`<span class="badge badge--warn">${plural(a.gap_days || 0, "day")}</span><div class="xsmall muted">${a.coverage_gaps.map((g) => `${date(g.from)} – ${date(g.to)}`).join("; ")}</div>`
            : html`<span class="muted">None</span>`}</td>
          <td class="num">${inr(a.opening_balance)}</td><td class="num">${inr(a.daily_abb)}</td><td class="num">${inr(a.fixed_date_abb)}</td>
          <td class="num ${a.overdrawn_days ? "is-neg strong" : ""}">${int(a.overdrawn_days || 0)}</td>
          <td class="num">${int((a.statement_ids || []).length)}</td></tr>`)}
        <tr class="v-loan-total"><th scope="row">Consolidated</th><td class="nowrap small">${date(d.period_start)} – ${date(d.period_end)}</td>
          <td class="num">${int((d.consolidated_coverage || {}).full_coverage_days)}</td><td class="num">${DASH}</td><td class="num">${DASH}</td>
          <td class="num strong">${inr(d.daily_abb)}</td><td class="num strong">${inr(d.fixed_date_abb)}</td><td class="num">${int(d.overdrawn_days || 0)}</td><td class="num">${DASH}</td></tr>
      </tbody>
    </table>`;
  }

  function incomeBlock(d, ib) {
    if (!ib || !ib.method) return "";
    const override = ib.monthly_income_override;
    const rows = [
      ["Salary credits (monthly average)", ib.salary_monthly_avg, ""],
      ["Net business inflow (monthly average)", ib.avg_monthly_net_business_inflow, ""],
      [`× business income factor ${num(ib.business_income_factor, 2)}`, ib.business_income_proxy, "Business income proxy"],
      ["Fixed-date ABB", ib.fixed_date_abb, ""],
      [`× ABB income factor ${num(ib.abb_income_factor, 2)}`, ib.abb_income_proxy, "ABB income proxy"],
    ];
    return html`<section class="card" aria-labelledby="loan-inc-h">
      <div class="card__head"><div><h2 class="card__title" id="loan-inc-h">Income basis</h2>
        <p class="card__sub">How the assessable monthly income of ${inr(d.assessable_monthly_income)} was derived (${String(ib.method).replace(/_/g, " ")}).</p></div>
        ${override != null ? html`<span class="badge badge--warn">Override in use</span>` : ""}</div>
      <div class="card__body stack-sm">
        <div class="table-wrap"><table class="table table--compact v-loan-income">
          <caption class="sr-only">Income basis components</caption>
          <tbody>${rows.map(([label, v, note]) => html`<tr><th scope="row" class="small">${label}${note ? html`<div class="xsmall muted">${note}</div>` : ""}</th><td class="num tabular">${inr(v || 0)}</td></tr>`)}
            ${override != null ? html`<tr><th scope="row" class="small">Monthly income override</th><td class="num tabular strong">${inr(override)}</td></tr>` : ""}
            <tr class="v-loan-total"><th scope="row">Assessable monthly income</th><td class="num tabular strong">${inr(d.assessable_monthly_income)}</td></tr></tbody></table></div>
        ${ib.note ? html`<p class="xsmall muted">${ib.note}</p>` : ""}
      </div></section>`;
  }

  function coverageBlock(d, cov) {
    const segs = cov.segments || [];
    if (!segs.length) return emptyState({ icon: "calendar", title: "No coverage information", compact: true });
    const total = segs.reduce((s, x) => s + (Number(x.days) || 0), 0) || 1;
    const nAcc = (cov.accounts || []).length;
    const tone = (x) => ((x.accounts || []).length === 0 ? "none" : (x.accounts || []).length >= nAcc ? "full" : "partial");
    return html`<div class="v-loan-cov" role="img" aria-label="Coverage: ${int(cov.full_coverage_days)} days covered by all ${plural(nAcc, "account")}, ${int(cov.partial_coverage_days)} partially covered, ${int(cov.uncovered_days)} uncovered. The table below lists each date range.">
        ${segs.map((x) => html`<span class="v-loan-cov__seg v-loan-cov__seg--${tone(x)}" data-seg-w="${((Number(x.days) || 0) / total * 100).toFixed(2)}"
          title="${date(x.from)} – ${date(x.to)} · ${(x.accounts || []).join(", ") || "no account"}"></span>`)}
      </div>
      <div class="legend small" aria-hidden="true">
        <span class="legend__item"><span class="legend__swatch v-loan-sw-full"></span>All accounts (${int(cov.full_coverage_days)} d)</span>
        <span class="legend__item"><span class="legend__swatch v-loan-sw-partial"></span>Some accounts (${int(cov.partial_coverage_days)} d)</span>
        <span class="legend__item"><span class="legend__swatch v-loan-sw-none"></span>Uncovered (${int(cov.uncovered_days)} d)</span>
      </div>
      <div class="table-wrap"><table class="table table--compact">
        <caption class="sr-only">Coverage segments: which accounts cover each date range</caption>
        <thead><tr><th scope="col">From</th><th scope="col">To</th><th scope="col" class="num">Days</th><th scope="col">Accounts covering</th></tr></thead>
        <tbody>${segs.map((x) => html`<tr><td class="nowrap">${date(x.from)}</td><td class="nowrap">${date(x.to)}</td><td class="num">${int(x.days)}</td>
          <td class="small">${(x.accounts || []).length ? x.accounts.join(", ") : html`<span class="badge badge--warn">None</span>`}
            ${tone(x) === "partial" ? html` <span class="badge badge--outline">partial</span>` : ""}</td></tr>`)}</tbody></table></div>
      ${cov.basis ? html`<p class="xsmall muted">${cov.basis}</p>` : ""}`;
  }

  function penalBlock(d, penal) {
    const byAcc = Object.entries(d.overdrawn_days_by_account || {}).filter(([, n]) => n);
    if (!penal.length) {
      return html`<div class="card__body">${emptyState({ icon: "check", title: "No penal or overdraft events", compact: true, tone: "success",
        message: byAcc.length ? `Overdrawn days: ${byAcc.map(([a, n]) => `${a} ${n}`).join(", ")}.` : "No minimum-balance, overdrawing or penal charges in the narrations." })}</div>`;
    }
    const bd = Object.entries(d.penal_event_breakdown || {});
    return html`${bd.length ? html`<div class="card__body row-wrap">${bd.map(([k, n]) => html`<span class="badge badge--danger">${penalLabel(k)} · ${int(n)}</span>`)}
        ${byAcc.map(([a, n]) => html`<span class="badge badge--warn">${a}: ${plural(n, "overdrawn day")}</span>`)}</div>` : ""}
      <div class="table-wrap"><table class="table table--compact">
        <caption class="sr-only">Penal and overdraft events</caption>
        <thead><tr><th scope="col">Date</th><th scope="col">Type</th><th scope="col">Account</th><th scope="col">Narration</th><th scope="col" class="num">Amount</th></tr></thead>
        <tbody>${penal.map((p) => html`<tr><td class="nowrap"><a href="${ctx.href("transactions", { date_from: p.date, date_to: p.date, direction: "DR" })}">${date(p.date)}</a></td>
          <td class="nowrap"><span class="badge badge--danger">${penalLabel(p.type)}</span></td>
          <td class="small mono">${p.account || DASH}</td><td class="small">${p.narration}</td><td class="num is-debit">${inr(-(p.amount || 0))}</td></tr>`)}</tbody></table></div>`;
  }

  function emiBlock(d) {
    const obs = d.emi_obligations || [];
    const txs = d.detected_emi_transactions || [];
    if (!obs.length) {
      return html`<div class="card__body">${emptyState({ icon: "check", title: "No recurring EMI detected", compact: true, tone: "success",
        message: "No NACH / ECS / SI debit recurred for the same lender across consecutive months." })}</div>`;
    }
    return html`<div class="table-wrap"><table class="table table--compact">
        <caption class="sr-only">Detected EMI obligations</caption>
        <thead><tr><th scope="col">Lender</th><th scope="col" class="num">Monthly EMI</th><th scope="col" class="num">Debit day</th>
          <th scope="col" class="num">Months</th><th scope="col">Status</th></tr></thead>
        <tbody>${obs.map((o) => html`<tr>
          <th scope="row"><span class="strong">${o.lender}</span><div class="xsmall muted">${date(o.first_debit)} – ${date(o.last_debit)} · <span class="mono">${o.account}</span></div></th>
          <td class="num">${inr(o.monthly_emi)}${o.latest_amount && o.latest_amount !== o.monthly_emi ? html`<div class="xsmall muted">latest ${inr(o.latest_amount)}</div>` : ""}</td>
          <td class="num">${o.usual_debit_day ? int(o.usual_debit_day) : DASH}</td>
          <td class="num" title="${int(o.consecutive_months)} consecutive of ${int(o.months_observed)} observed">${int(o.consecutive_months)}/${int(o.months_observed)}</td>
          <td class="nowrap">${o.active ? html`<span class="badge badge--info">Active</span>` : html`<span class="badge badge--neutral">Closed</span>`}
            ${o.confirmed ? html` <span class="badge badge--success">Confirmed</span>` : html` <span class="badge badge--warn">Probable</span>`}</td>
        </tr>`)}</tbody>
      </table></div>
      ${txs.length
        ? html`<details class="v-loan-details"><summary class="small">${plural(txs.length, "EMI debit")} matched</summary>
            <div class="table-wrap"><table class="table table--compact">
              <thead><tr><th scope="col">Date</th><th scope="col">Narration</th><th scope="col" class="num">Amount</th></tr></thead>
              <tbody>${txs.map((t) => html`<tr><td class="nowrap">${date(t.date)}</td><td class="small">${t.narration}<div class="xsmall muted">${t.category || ""}</div></td>
                <td class="num is-debit">${inr(-(t.amount || 0))}</td></tr>`)}</tbody></table></div></details>`
        : ""}`;
  }

  function bounceBlock(d) {
    const ev = d.bounce_events || [];
    if (!ev.length) {
      return html`<div class="card__body">${emptyState({ icon: "check", title: "No bounces", compact: true, tone: "success",
        message: `None of ${plural(d.attempted_debit_instruments || 0, "debit instrument")} was returned.` })}</div>`;
    }
    return html`<div class="table-wrap"><table class="table table--compact">
      <caption class="sr-only">Bounce events</caption>
      <thead><tr><th scope="col">Date</th><th scope="col">Type</th><th scope="col">Narration</th><th scope="col" class="num">Amount</th></tr></thead>
      <tbody>${ev.map((b) => html`<tr>
        <td class="nowrap">${date(b.date)}</td>
        <td class="nowrap"><span class="badge ${b.direction === "INWARD" ? "badge--danger" : "badge--warn"}">${b.direction === "INWARD" ? "I/W" : b.direction === "OUTWARD" ? "O/W" : b.direction}</span>
          <div class="xsmall muted">${b.instrument || ""}</div></td>
        <td class="small">${b.narration}
          ${b.derived_from_charge_only ? html`<div class="xsmall"><span class="badge badge--outline">From return charge only</span></div>` : ""}
          ${(b.charge_narrations || []).length ? html`<div class="xsmall muted">Charge: ${b.charge_narrations.join("; ")}</div>` : ""}</td>
        <td class="num">${inr(b.amount)}</td></tr>`)}</tbody>
    </table></div>
    <p class="xsmall muted v-loan-note">Rate = bounces ÷ attempted debit instruments (${int(d.attempted_debit_instruments)}). Outward bounce rate ${pct(d.outward_bounce_rate_pct, { ratio: false })}.</p>`;
  }

  /* ---------------------------------------------------------------- events */
  delegate(el, "click", "[data-action]", (e, b) => {
    if (b.dataset.action === "loan-retry") load();
    else if (b.dataset.action === "loan-reset") {
      Object.assign(inputs, DEFAULTS, OPTIONAL_DEFAULTS);
      for (const k of Object.keys(LIMITS)) form.elements[k].value = inputs[k];
      validate();
      ctx.setParams({ foir: null, rate: null, tenure: null, income_factor: null, abb_factor: null, income_override: null });
      load();
    }
  });
  delegate(el, "change", "#loan-series", (e, sel) => {
    account = sel.value;
    ctx.setParams({ account: account || null });
    if (tip) tip.hidden = true;
    const c = body.querySelector('[data-slot="chart"]');
    if (c && data) drawChart(c);
  });

  ctx.onData([TOPICS.transactions, TOPICS.suspense, TOPICS.statements, TOPICS.anomalies], () => load());
  ctx.registerCommands([
    { id: "loan.recalc", title: "Loan: recalculate eligibility", group: "Loan", icon: "refresh", run: () => form.requestSubmit() },
    { id: "loan.reset", title: "Loan: reset FOIR / rate / tenure / income factors", group: "Loan", icon: "minus", run: () => el.querySelector('[data-action="loan-reset"]')?.click() },
  ]);

  await load();

  return {
    update(params) {
      const next = {
        foir: params.foir || DEFAULTS.foir, rate: params.rate || DEFAULTS.rate, tenure: params.tenure || DEFAULTS.tenure,
        income_factor: params.income_factor || "", abb_factor: params.abb_factor || "", income_override: params.income_override || "",
      };
      const changed = Object.keys(next).some((k) => next[k] !== inputs[k]);
      Object.assign(inputs, next);
      for (const k of Object.keys(LIMITS)) form.elements[k].value = inputs[k];
      const acc = params.account || "";
      if (changed) {
        account = acc;
        load();
      } else if (acc !== account) {
        account = acc;
        if (data) draw();
      }
    },
  };
}
