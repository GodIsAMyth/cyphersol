/* Individual financial profile (#/case/{id}/profile?entity=…) — FR-TRACE-06, report TG-16 / §5.3 item 8.
 *
 *   - entity picker (statement holders first, then details-only counterparties), ?entity= in the URL
 *   - identity card: status, linked accounts / VPAs, aliases, mule tier; quick links into the other analytics
 *   - KPI tiles (credits, debits, net, opening/closing, ABB, accounts, mule score)
 *   - monthly inflow/outflow columns with net line (SVG) + data table alternative
 *   - category view (by category or by taxonomy group, bars + table with share of gross, linked to the
 *     entity's transactions in that category) and channel view keyed by the SRS FR-CAT-01 channel names
 *     (in / out split, share, linked to the entity's transactions on that rail)
 *   - top senders / beneficiaries (bar lists, linked to profiles)
 *   - mule contribution waterfall, ABB by account, source statements (full SHA-256), sample transactions
 *   - Export menu: server reports GET export/profile?entity_id=&format=pdf|xlsx|html (formats from
 *     /api/meta profile_export_formats, saved through downloadBlob with the token header) plus a client CSV
 */
import { entityStatus, tierClass } from "../core/constants.js";
import { announce, delegate, html, render } from "../core/dom.js";
import { DASH, date, inr, inrCompact, int, month, num, plural } from "../core/format.js";
import { isAbort, linkAbort } from "../core/api.js";
import { TOPICS } from "../core/store.js";
import { attachChartTooltip, barList, contributionWaterfall, monthlyFlowChart, monthlyFlowTable, mountResponsive } from "../charts/bars.js";
import { createDataGrid } from "../components/datagrid.js";
import { icon } from "../components/icons.js";
import { kpiGrid } from "../components/kpi.js";
import { emptyState, errorState, pageHead, skeleton } from "../components/states.js";
import { createMenu } from "../components/menu.js";
import { toast } from "../components/toast.js";
import { csvCell, downloadText } from "../core/files.js";
import { categoryRows, channelRows, groupRows, profileExportName, profileFormats } from "./profile-export.js";
import { channelLabel, channelOf } from "./transactions-meta.js";

export const styles = ["views/risk.css"];

const CAT_LIMIT = 8;

const sortByName = (a, b) => String(a.canonical_name).localeCompare(String(b.canonical_name));

export async function mount(el, ctx) {
  await ctx.store.ensureCaseData();
  if (!ctx.isCurrent()) return undefined;

  render(
    el,
    html`<div class="stack-lg">
      ${pageHead({
        title: "Financial profile",
        sub: "Individual cash-flow profile of a resolved entity across every statement in the case.",
        actions: html`<div class="v-profile-picker">
          <label class="sr-only" for="pf-entity">Entity</label>
          <select id="pf-entity" class="select" data-slot="picker"></select>
          <button type="button" class="btn btn--icon" data-action="prev" aria-label="Previous entity" title="Previous entity">${icon("chevron-left", { size: "sm" })}</button>
          <button type="button" class="btn btn--icon" data-action="next" aria-label="Next entity" title="Next entity">${icon("chevron-right", { size: "sm" })}</button>
          <button type="button" class="btn" data-action="export" disabled>${icon("download", { size: "sm" })}Export${icon("chevron-down", { size: "sm" })}</button>
        </div>`,
      })}
      <div data-slot="body"></div>
    </div>`
  );
  const picker = el.querySelector('[data-slot="picker"]');
  const body = el.querySelector('[data-slot="body"]');
  const exportBtn = el.querySelector('[data-action="export"]');

  let entities = [];
  let currentId = ctx.params.entity || null;
  let profile = null;
  let loadCtl = null;
  let showAllCats = false;
  let catMode = ctx.params.catview === "group" ? "group" : "category";
  let exportCtl = null;
  const teardown = []; // per-render disposers (charts, grids, tooltips)
  const disposeRender = () => teardown.splice(0).forEach((fn) => { try { fn(); } catch (e) { console.error(e); } });
  ctx.onCleanup(() => {
    loadCtl?.abort();
    exportCtl?.abort();
    disposeRender();
  });

  // ------------------------------------------------------------------ picker
  const drawPicker = () => {
    entities = ctx.store.entities().slice().sort(sortByName);
    const withStmt = entities.filter((e) => e.has_statement);
    const details = entities.filter((e) => !e.has_statement);
    const opt = (e) => html`<option value="${e.entity_id}" ${e.entity_id === currentId ? html`selected` : ""}>${e.canonical_name}${e.linked_account_numbers?.length ? ` · ${e.linked_account_numbers[0]}` : ""}</option>`;
    const known = entities.some((e) => e.entity_id === currentId);
    render(
      picker,
      html`${!currentId || !known ? html`<option value="" ${currentId ? "" : html`selected`}>${currentId ? "Entity not in current list" : "Choose an entity…"}</option>` : ""}
        ${withStmt.length ? html`<optgroup label="Has statement (${withStmt.length})">${withStmt.map(opt)}</optgroup>` : ""}
        ${details.length ? html`<optgroup label="Details only (${details.length})">${details.map(opt)}</optgroup>` : ""}`
    );
    picker.disabled = !entities.length;
    el.querySelectorAll('[data-action="prev"], [data-action="next"]').forEach((b) => (b.disabled = entities.length < 2));
  };
  const ordered = () => [...entities.filter((e) => e.has_statement), ...entities.filter((e) => !e.has_statement)];
  const go = (id, { push = true } = {}) => {
    if (!id || id === currentId) return;
    currentId = id;
    ctx.setParams({ entity: id }, { push });
    load();
  };
  picker.addEventListener("change", () => go(picker.value));
  delegate(el, "click", '[data-action="prev"], [data-action="next"]', (e, b) => {
    const list = ordered();
    if (!list.length) return;
    const i = list.findIndex((x) => x.entity_id === currentId);
    const step = b.dataset.action === "next" ? 1 : -1;
    go(list[(i + step + list.length) % list.length].entity_id);
  });

  // ------------------------------------------------------------------ lookups for linking parties to profiles
  const nameIndex = () => {
    const m = new Map();
    for (const e of ctx.store.entities()) {
      m.set(String(e.canonical_name).toUpperCase(), e.entity_id);
      for (const a of e.aliases || []) if (!m.has(String(a).toUpperCase())) m.set(String(a).toUpperCase(), e.entity_id);
    }
    return m;
  };

  // ------------------------------------------------------------------ rendering
  function identityCard(p) {
    const e = p.entity || {};
    const st = entityStatus(e);
    const mule = (p.mule_assessments || []).slice().sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0))[0];
    const aliases = (e.aliases || []).filter((a) => String(a).toUpperCase() !== String(e.canonical_name).toUpperCase());
    return html`<section class="card v-profile-id" aria-labelledby="pf-name">
      <div class="card__body stack-sm">
        <div class="row-between v-profile-id__top">
          <div class="min-w-0">
            <h2 class="v-profile-name" id="pf-name">${e.canonical_name || "Unknown entity"}</h2>
            <div class="row-wrap">
              <span class="badge badge--${st.cls}">${st.label}</span>
              ${e.is_moved_to_misc ? html`<span class="badge badge--misc">Moved to Misc</span>` : ""}
              ${mule ? html`<a class="badge badge--${tierClass(mule.risk_tier)}" href="${ctx.href("mules", { entity: e.entity_id })}" title="Open mule score breakdown">Mule ${num(mule.composite_score, 1)} · ${mule.risk_tier}</a>` : ""}
              <span class="xsmall muted mono">${e.entity_id}</span>
            </div>
          </div>
          <div class="row-wrap v-profile-links">
            <a class="btn btn--sm" href="${ctx.href("network", { entity: e.entity_id })}">${icon("network", { size: "sm" })}Network</a>
            <a class="btn btn--sm" href="${ctx.href("layering", { source: e.entity_id })}">${icon("layers", { size: "sm" })}Layering</a>
            <a class="btn btn--sm" href="${ctx.href("bidirectional", { a: e.entity_id })}">${icon("swap", { size: "sm" })}Bidirectional</a>
            <a class="btn btn--sm" href="${ctx.href("transactions", { entity: e.entity_id })}">${icon("list", { size: "sm" })}Transactions</a>
          </div>
        </div>
        <dl class="v-profile-meta">
          <div><dt>Accounts</dt><dd>${e.linked_account_numbers?.length ? e.linked_account_numbers.map((a) => html`<span class="mono">${a}</span> `) : html`<span class="muted">None linked</span>`}</dd></div>
          <div><dt>VPAs</dt><dd>${e.linked_vpas?.length ? e.linked_vpas.map((v) => html`<span class="mono">${v}</span> `) : html`<span class="muted">None</span>`}</dd></div>
          <div><dt>Aliases</dt><dd>${aliases.length
            ? html`${aliases.slice(0, 8).map((a) => html`<span class="badge badge--neutral">${a}</span> `)}${aliases.length > 8 ? html`<span class="muted small">+${aliases.length - 8} more</span>` : ""}`
            : html`<span class="muted">No other spellings</span>`}</dd></div>
          <div><dt>Statements</dt><dd>${int((p.statements || []).length)}</dd></div>
        </dl>
      </div>
    </section>`;
  }

  function kpis(p) {
    const k = p.kpis || {};
    const mule = (p.mule_assessments || []).slice().sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0))[0];
    const derived = Boolean(p.derived_from_counterparty_statements);
    const tone = mule ? (tierClass(mule.risk_tier) === "critical" ? "critical" : tierClass(mule.risk_tier) === "suspicious" ? "warn" : "success") : "";
    return kpiGrid(
      [
        { label: "Total credits", value: inrCompact(k.total_credits), title: inr(k.total_credits), icon: "arrow-down-left", sub: "inflow" },
        { label: "Total debits", value: inrCompact(k.total_debits), title: inr(k.total_debits), icon: "arrow-up-right", sub: "outflow" },
        { label: "Net flow", value: inrCompact(k.net_flow, { sign: true }), title: inr(k.net_flow, { sign: true }), tone: (k.net_flow || 0) < 0 ? "danger" : "" },
        { label: "Transactions", value: int(k.total_transactions), href: ctx.href("transactions", { entity: p.entity?.entity_id }) },
        !derived && { label: "Opening balance", value: inrCompact(k.opening_balance), title: inr(k.opening_balance) },
        !derived && { label: "Closing balance", value: inrCompact(k.closing_balance), title: inr(k.closing_balance) },
        !derived && { label: "Daily ABB", value: inrCompact(k.daily_abb), title: inr(k.daily_abb), sub: "average bank balance" },
        !derived && { label: "Fixed-date ABB", value: inrCompact(k.fixed_date_abb), title: inr(k.fixed_date_abb) },
        { label: "Active accounts", value: int(k.active_accounts) },
        { label: "Mule score", value: mule ? num(mule.composite_score, 1) : DASH, sub: mule ? mule.risk_tier : "not scored", tone, href: mule ? ctx.href("mules", { entity: p.entity?.entity_id }) : "" },
      ],
      { label: "Profile key figures" }
    );
  }

  const legend = html`<div class="legend" aria-hidden="true">
    <span class="legend__item"><span class="legend__swatch sw-credit"></span>Inflow (Cr)</span>
    <span class="legend__item"><span class="legend__swatch sw-debit"></span>Outflow (Dr)</span>
    <span class="legend__item"><span class="v-risk-legend-line"></span>Net</span></div>`;

  function partyItems(list, idx) {
    return (list || []).slice(0, 15).map((x) => {
      const id = idx.get(String(x.party || "").toUpperCase());
      return { label: x.party || "Unknown", href: id && id !== currentId ? ctx.href("profile", { entity: id }) : "", values: { value: x.amount } };
    });
  }

  const pctText = (v) => `${num((Number(v) || 0) * 100, 1)}%`;
  const txHref = (extra) => ctx.href("transactions", { entity: currentId, ...extra });

  function drawCategories(p) {
    const box = body.querySelector('[data-slot="cats"]');
    if (!box) return;
    const byGroup = catMode === "group";
    const cats = byGroup ? groupRows(p.category_breakdown) : categoryRows(p.category_breakdown);
    const shown = showAllCats ? cats : cats.slice(0, CAT_LIMIT);
    const more = Math.max(0, cats.length - CAT_LIMIT);
    const items = shown.map((c) => byGroup
      ? { label: c.group, sub: `${plural(c.categories, "category", "categories")} · ${plural(c.tx_count, "txn")} · ${pctText(c.share)}`, values: { credit: c.credit, debit: c.debit } }
      : { label: c.name, sub: `${plural(c.tx_count, "txn")} · ${pctText(c.share)}`, href: c.id != null ? txHref({ category_id: c.id }) : "", values: { credit: c.credit, debit: c.debit } });
    render(
      box,
      html`<div class="row-wrap v-profile-seg" role="group" aria-label="Group categories by">
          <button type="button" class="chip" data-action="cat-mode" data-mode="category" aria-pressed="${byGroup ? "false" : "true"}">By category</button>
          <button type="button" class="chip" data-action="cat-mode" data-mode="group" aria-pressed="${byGroup ? "true" : "false"}">By group</button>
        </div>
        ${barList(items, {
          segments: [{ key: "credit", label: "Inflow", cls: "credit" }, { key: "debit", label: "Outflow", cls: "debit" }],
          label: byGroup ? "Taxonomy group breakdown, inflow and outflow" : "Category breakdown, inflow and outflow",
          empty: "No categorised transactions.",
        })}
        ${more ? html`<button type="button" class="btn btn--ghost btn--sm" data-action="toggle-cats" aria-expanded="${showAllCats ? "true" : "false"}">
            ${showAllCats ? `Show top ${byGroup ? "groups" : "categories"}` : `Show all (${more} more)`}</button>` : ""}
        ${cats.length ? html`<details class="v-profile-details"><summary class="small">Show data table</summary>
          <div class="table-wrap"><table class="table table--compact">
            <caption class="sr-only">${byGroup ? "Taxonomy groups" : "Categories"}: inflow, outflow, transactions and share of gross</caption>
            <thead><tr><th scope="col">${byGroup ? "Group" : "Category"}</th><th scope="col" class="num">Inflow</th><th scope="col" class="num">Outflow</th><th scope="col" class="num">Txns</th><th scope="col" class="num">Share</th></tr></thead>
            <tbody>${cats.map((c) => html`<tr>
              <th scope="row">${byGroup ? c.group : c.id != null ? html`<a href="${txHref({ category_id: c.id })}">${c.name}</a>` : c.name}${!byGroup && c.suspense ? html` <span class="badge badge--suspicious">Suspense</span>` : ""}${!byGroup ? html`<div class="xsmall muted">${c.group}</div>` : ""}</th>
              <td class="num tabular is-credit">${inr(c.credit)}</td><td class="num tabular is-debit">${inr(c.debit)}</td>
              <td class="num tabular">${int(c.tx_count)}</td><td class="num tabular">${pctText(c.share)}</td></tr>`)}</tbody>
          </table></div></details>` : ""}`
    );
  }

  function channelSection(p) {
    const rows = channelRows(p.channel_mix);
    if (!rows.length) return html`<p class="muted small">No channel data.</p>`;
    return html`${barList(
        rows.map((c) => ({
          label: c.label,
          sub: `${plural(c.tx_count, "txn")} · ${pctText(c.share)}`,
          href: txHref({ channel: c.code }),
          title: `${c.label} (SRS ${c.code}${c.sources.length && (c.sources.length > 1 || c.sources[0] !== c.code) ? `; statement codes ${c.sources.join(", ")}` : ""}): in ${inr(c.credit)} · out ${inr(c.debit)}`,
          values: { credit: c.credit, debit: c.debit },
        })),
        { segments: [{ key: "credit", label: "Inflow", cls: "credit" }, { key: "debit", label: "Outflow", cls: "debit" }], label: "Payment channel mix by SRS channel, inflow and outflow", empty: "No channel data." }
      )}
      <details class="v-profile-details"><summary class="small">Show data table</summary>
        <div class="table-wrap"><table class="table table--compact">
          <caption class="sr-only">Payment channels (SRS FR-CAT-01 names): inflow, outflow, transactions and share of gross</caption>
          <thead><tr><th scope="col">Channel</th><th scope="col" class="num">Inflow</th><th scope="col" class="num">Outflow</th><th scope="col" class="num">Txns</th><th scope="col" class="num">Share</th></tr></thead>
          <tbody>${rows.map((c) => html`<tr><th scope="row"><a href="${txHref({ channel: c.code })}">${c.label}</a><div class="xsmall muted mono">${c.code}${c.sources.some((x) => x !== c.code) ? ` · from ${c.sources.join(", ")}` : ""}</div></th>
            <td class="num tabular is-credit">${inr(c.credit)}</td><td class="num tabular is-debit">${inr(c.debit)}</td>
            <td class="num tabular">${int(c.tx_count)}</td><td class="num tabular">${pctText(c.share)}</td></tr>`)}</tbody>
        </table></div></details>`;
  }

  function drawProfile(p) {
    disposeRender();
    const e = p.entity || {};
    const idx = nameIndex();
    const derived = Boolean(p.derived_from_counterparty_statements);
    const muleList = (p.mule_assessments || []).slice().sort((a, b) => (b.composite_score || 0) - (a.composite_score || 0));
    const months = p.monthly_breakdown || [];
    render(
      body,
      html`<div class="stack-lg">
        ${identityCard(p)}
        ${derived
          ? html`<div class="callout callout--info">${icon("info")}<div><strong>Derived from counterparty statements.</strong>
              No statement for this entity is loaded, so its flows are reconstructed from the other side of the case's statements
              (money it sent appears as outflow). Balances and ABB are not available.</div></div>`
          : ""}
        ${kpis(p)}
        <section class="card" aria-labelledby="pf-monthly">
          <div class="card__head"><div><h2 class="card__title" id="pf-monthly">Monthly cash flow</h2>
            <p class="card__sub">${months.length ? `${month(months[0].month)} – ${month(months[months.length - 1].month)} · ${plural(months.length, "active month")}` : "No activity"}</p></div>${months.length ? legend : ""}</div>
          <div class="card__body stack-sm">
            <div class="v-profile-chart" data-slot="monthly"></div>
            ${months.length ? html`<details class="v-profile-details"><summary class="small">Show data table</summary>${monthlyFlowTable(months)}</details>` : ""}
          </div>
        </section>
        <div class="grid-2">
          <section class="card" aria-labelledby="pf-cats">
            <div class="card__head"><div><h2 class="card__title" id="pf-cats">Category breakdown</h2>
              <p class="card__sub">Inflow and outflow by category (52-category taxonomy) or rolled up to taxonomy groups; share is of gross flow.</p></div>
              <div class="legend" aria-hidden="true"><span class="legend__item"><span class="legend__swatch sw-credit"></span>In</span><span class="legend__item"><span class="legend__swatch sw-debit"></span>Out</span></div></div>
            <div class="card__body" data-slot="cats"></div>
          </section>
          <section class="card" aria-labelledby="pf-ch">
            <div class="card__head"><div><h2 class="card__title" id="pf-ch">Payment channels</h2>
              <p class="card__sub">Inflow and outflow per rail, using the SRS channel names; share is of gross flow.</p></div>
              <div class="legend" aria-hidden="true"><span class="legend__item"><span class="legend__swatch sw-credit"></span>In</span><span class="legend__item"><span class="legend__swatch sw-debit"></span>Out</span></div></div>
            <div class="card__body stack-sm">${channelSection(p)}</div>
          </section>
          <section class="card" aria-labelledby="pf-snd">
            <div class="card__head"><div><h2 class="card__title" id="pf-snd">Top senders</h2>
              <p class="card__sub">Who paid ${e.canonical_name || "this entity"} (inflow).</p></div></div>
            <div class="card__body">${barList(partyItems(p.top_senders, idx), { segments: [{ key: "value", label: "Received", cls: "credit" }], label: "Top senders", empty: "No inbound counterparties." })}</div>
          </section>
          <section class="card" aria-labelledby="pf-ben">
            <div class="card__head"><div><h2 class="card__title" id="pf-ben">Top beneficiaries</h2>
              <p class="card__sub">Where the money went (outflow).</p></div></div>
            <div class="card__body">${barList(partyItems(p.top_beneficiaries, idx), { segments: [{ key: "value", label: "Paid", cls: "debit" }], label: "Top beneficiaries", empty: "No outbound counterparties." })}</div>
          </section>
        </div>
        ${muleList.length
          ? html`<section class="card" aria-labelledby="pf-mule">
              <div class="card__head"><div><h2 class="card__title" id="pf-mule">Mule assessment</h2>
                <p class="card__sub">10-parameter behavioural score per account.</p></div>
                <a class="btn btn--sm" href="${ctx.href("mules", { entity: e.entity_id })}">Open in Mule scoring${icon("arrow-right", { size: "sm" })}</a></div>
              <div class="card__body grid-2">${muleList.map(
                (m) => html`<div class="stack-sm min-w-0">
                  <div class="row-wrap"><span class="badge badge--${tierClass(m.risk_tier)}">${m.risk_tier}</span>
                    <span class="strong tabular">${num(m.composite_score, 1)} / 100</span>
                    <span class="muted small">${m.bank_name || ""} · <span class="mono">${m.account_number || DASH}</span></span></div>
                  ${contributionWaterfall(m, { compact: true })}
                </div>`
              )}</div>
            </section>`
          : ""}
        ${(p.abb_by_account || []).length || (p.statements || []).length
          ? html`<div class="grid-2">
              ${(p.abb_by_account || []).length
                ? html`<section class="card" aria-labelledby="pf-abb">
                    <div class="card__head"><div><h2 class="card__title" id="pf-abb">Average bank balance by account</h2></div></div>
                    <div class="card__body card__body--flush"><div class="table-wrap"><table class="table table--compact">
                      <thead><tr><th scope="col">Account</th><th scope="col">Period</th><th scope="col" class="num">Days</th><th scope="col" class="num">Daily ABB</th></tr></thead>
                      <tbody>${p.abb_by_account.map((a) => html`<tr><td>${a.bank_name || ""}<div class="mono xsmall muted">${a.account_number}</div></td>
                        <td class="nowrap small">${date(a.period_start)} → ${date(a.period_end)}</td><td class="num tabular">${int(a.days)}</td>
                        <td class="num tabular">${inr(a.daily_abb)}</td></tr>`)}</tbody></table></div></div>
                  </section>`
                : ""}
              ${(p.statements || []).length
                ? html`<section class="card" aria-labelledby="pf-stm">
                    <div class="card__head"><div><h2 class="card__title" id="pf-stm">Source statements</h2><p class="card__sub">Chain of custody (SHA-256).</p></div></div>
                    <div class="card__body stack-sm">${p.statements.map((s) => html`<div class="v-profile-stmt">
                      <div class="row-between"><span class="strong truncate">${s.source_filename}</span><span class="small tabular nowrap">${int(s.total_transactions)} txns</span></div>
                      <div class="small text-2">${s.bank_name} · <span class="mono">${s.account_number}</span> · ${date(s.period_start)} → ${date(s.period_end)}</div>
                      <div class="hash xsmall" title="SHA-256">${s.sha256_hash}</div></div>`)}</div>
                  </section>`
                : ""}
            </div>`
          : ""}
        <section class="card" aria-labelledby="pf-tx">
          <div class="card__head"><div><h2 class="card__title" id="pf-tx">Sample transactions</h2>
            <p class="card__sub">${int((p.recent_transactions || []).length)} rows returned with the profile. Open Transactions for the complete, filterable list.</p></div>
            <a class="btn btn--sm" href="${ctx.href("transactions", { entity: e.entity_id })}">All transactions${icon("arrow-right", { size: "sm" })}</a></div>
          <div class="card__body card__body--flush" data-slot="tx"></div>
        </section>
      </div>`
    );
    drawCategories(p);

    const chartBox = body.querySelector('[data-slot="monthly"]');
    if (months.length) {
      teardown.push(mountResponsive(chartBox, (w) => monthlyFlowChart(months, { width: w, height: w < 480 ? 220 : 260, label: `Monthly cash flow of ${e.canonical_name}` })));
      teardown.push(attachChartTooltip(chartBox, "[data-tip]"));
    } else {
      render(chartBox, emptyState({ icon: "chart", title: "No monthly activity", compact: true }));
    }

    const txGrid = createDataGrid(body.querySelector('[data-slot="tx"]'), {
      ariaLabel: `Sample transactions of ${e.canonical_name}`,
      rowKey: "tx_id",
      rows: p.recent_transactions || [],
      height: "min(50vh, 420px)",
      rowHeight: 46,
      showToolbar: false,
      countNoun: ["transaction", "transactions"],
      initialSort: { key: "txn_date", order: "desc" },
      emptyState: { icon: "list", title: "No transactions" },
      onRowClick: (t) => ctx.navigate("transactions", t.tx_id ? { ids: t.tx_id } : { q: t.utr_number || t.extracted_counterparty || (t.raw_narration || "").slice(0, 40), date_from: t.txn_date, date_to: t.txn_date }),
      columns: [
        { key: "txn_date", label: "Date", width: "108px", sortable: true, format: (v) => date(v) },
        { key: "raw_narration", label: "Narration & counterparty", width: "minmax(240px, 3fr)", sortable: true,
          render: (t) => html`<span class="truncate">${t.raw_narration}</span><span class="dg__sub">${t.extracted_counterparty || DASH} · ${t.source_holder_name || ""}</span>` },
        { key: "payment_channel", label: "Channel", width: "120px", sortable: true, sortValue: (t) => channelOf(t),
          render: (t) => html`<span class="badge badge--neutral" title="SRS ${channelOf(t)}${t.payment_channel && t.payment_channel !== channelOf(t) ? `; statement code ${t.payment_channel}` : ""}">${channelLabel(channelOf(t))}</span>` },
        { key: "amount", label: "Amount", width: "140px", align: "right", sortable: true, sortValue: (t) => (t.credit || 0) - (t.debit || 0),
          className: (t) => ((t.credit || 0) > 0 ? "is-credit tabular" : "is-debit tabular"),
          render: (t) => ((t.credit || 0) > 0 ? html`+${inr(t.credit)}` : html`${inr(-(t.debit || 0))}`) },
        { key: "category_name", label: "Category", width: "minmax(150px, 1.2fr)", sortable: true,
          render: (t) => html`<span class="badge ${t.is_suspense ? "badge--suspicious" : "badge--neutral"}">${t.category_name || "Uncategorised"}</span>` },
      ],
    });
    teardown.push(() => txGrid.destroy());
    ctx.setCrumbs?.([{ label: e.canonical_name || "Entity" }]);
  }

  delegate(body, "click", '[data-action="toggle-cats"]', () => {
    showAllCats = !showAllCats;
    if (profile) drawCategories(profile);
  });
  delegate(body, "click", '[data-action="cat-mode"]', (e, b) => {
    const mode = b.dataset.mode === "group" ? "group" : "category";
    if (mode === catMode) return;
    catMode = mode;
    showAllCats = false;
    ctx.setParams({ catview: mode === "group" ? "group" : null });
    if (profile) drawCategories(profile);
    body.querySelector(`[data-action="cat-mode"][data-mode="${mode}"]`)?.focus();
  });

  // ------------------------------------------------------------------ data
  async function load() {
    loadCtl?.abort();
    const ctl = new AbortController();
    loadCtl = ctl;
    exportBtn.disabled = true;
    disposeRender();
    render(body, html`<div class="stack-lg" aria-busy="true">${skeleton({ kind: "block" })}${skeleton({ kind: "kpis", rows: 6 })}${skeleton({ kind: "table", rows: 6 })}</div>`);
    if (!entities.length) {
      render(body, emptyState({ icon: "users", title: "No entities resolved yet", message: "Upload statements so counterparties can be resolved into entities.", action: { label: "Upload statements", href: ctx.href("ingest") } }));
      return;
    }
    try {
      const p = await ctx.api.get("trace/profile", currentId ? { entity_id: currentId } : {}, { signal: ctl.signal });
      if (ctl.signal.aborted || !ctx.isCurrent()) return;
      if (p && p.error) {
        profile = null;
        render(body, emptyState({ icon: "user", title: "Profile unavailable", message: p.error, tone: "warn" }));
        return;
      }
      profile = p;
      const id = p.entity?.entity_id;
      if (id && id !== currentId) {
        currentId = id;
        ctx.setParams({ entity: id });
      }
      drawPicker();
      drawProfile(p);
      announce(`Profile loaded for ${p.entity?.canonical_name || p.entity?.entity_name || "the selected entity"}.`);
      exportBtn.disabled = false;
    } catch (err) {
      if (isAbort(err) || ctl.signal.aborted || !ctx.isCurrent()) return;
      profile = null;
      render(body, errorState(err, { retryId: "pf-retry", title: err?.isNotFound ? "Entity not found" : "Could not load the profile" }));
    }
  }
  delegate(body, "click", '[data-action="pf-retry"]', () => load());

  // ------------------------------------------------------------------ export menu (server PDF / XLSX / HTML + client CSV)
  function exportCsv() {
    if (!profile) return;
    try {
      const name = profile.entity?.canonical_name || "entity";
      const f = downloadText(
        `profile_${String(name).replace(/[^A-Za-z0-9]+/g, "_").replace(/^_|_$/g, "")}_${new Date().toISOString().slice(0, 10)}.csv`,
        "\ufeff" + profileCsv(profile, ctx.caseId),
        "text/csv;charset=utf-8"
      );
      toast.success(`Saved ${f.filename}`);
    } catch (err) {
      toast.error(err, { title: "Export failed" });
    }
  }

  async function exportServer(fmt) {
    const ent = profile?.entity;
    if (!ent?.entity_id) return;
    if (exportBtn.dataset.busy === "1") return;
    exportCtl?.abort();
    const ctl = new AbortController();
    exportCtl = ctl;
    const unlink = linkAbort(ctx.signal, ctl);
    exportBtn.dataset.busy = "1";
    exportBtn.setAttribute("aria-busy", "true");
    const info = profileFormats(ctx.store.get().meta).find((f) => f.fmt === fmt);
    try {
      const f = await ctx.api.download("export/profile", profileExportName(ent, fmt), { params: { entity_id: ent.entity_id, format: fmt }, signal: ctl.signal });
      toast.success(`Saved ${f.filename}`, { title: `${info?.label || fmt.toUpperCase()} exported` });
    } catch (err) {
      if (!isAbort(err) && ctx.isCurrent()) toast.error(err, { title: "Export failed" });
    } finally {
      unlink();
      if (exportCtl === ctl) exportCtl = null;
      delete exportBtn.dataset.busy;
      exportBtn.removeAttribute("aria-busy");
    }
  }

  const exportMenu = createMenu(exportBtn, {
    label: "Export profile",
    items: () => [
      { group: "Report (generated on the server)" },
      ...profileFormats(ctx.store.get().meta).map((f) => ({
        id: `pf-${f.fmt}`, label: f.label, description: f.desc, icon: f.icon, disabled: !profile, run: () => exportServer(f.fmt),
      })),
      { separator: true },
      { id: "pf-csv", label: "CSV (this page)", description: "Key figures and breakdowns as shown here", icon: "table", disabled: !profile, run: exportCsv },
    ],
  });
  ctx.onCleanup(() => exportMenu.destroy());

  drawPicker();
  await load();
  if (!ctx.isCurrent()) return undefined;

  ctx.onData([TOPICS.analytics, TOPICS.entities], () => {
    drawPicker();
    load();
  });

  ctx.registerCommands([
    { id: "profile.next", title: "Profile: next entity", group: "Financial profile", icon: "chevron-right", when: () => entities.length > 1, run: () => el.querySelector('[data-action="next"]').click() },
    { id: "profile.prev", title: "Profile: previous entity", group: "Financial profile", icon: "chevron-left", when: () => entities.length > 1, run: () => el.querySelector('[data-action="prev"]').click() },
    { id: "profile.export", title: "Profile: export as CSV", group: "Financial profile", icon: "download", when: () => Boolean(profile), run: exportCsv },
    ...profileFormats(ctx.store.get().meta).map((f) => ({ id: `profile.export.${f.fmt}`, title: `Profile: export ${f.label}`, group: "Financial profile", icon: "download", when: () => Boolean(profile), run: () => exportServer(f.fmt) })),
    { id: "profile.pick", title: "Profile: choose entity…", group: "Financial profile", icon: "user", run: () => picker.focus() },
    { id: "profile.network", title: "Profile: show entity in network graph", group: "Financial profile", icon: "network", when: () => Boolean(currentId), run: () => ctx.navigate("network", { entity: currentId }) },
  ]);

  return {
    update(params) {
      const id = params.entity || null;
      if (id && id !== currentId) {
        currentId = id;
        drawPicker();
        load();
      }
    },
  };
}

/* ---------------------------------------------------------------------------------------------- CSV */
function profileCsv(p, caseId) {
  const e = p.entity || {};
  const k = p.kpis || {};
  const lines = [];
  const row = (...cells) => lines.push(cells.map(csvCell).join(","));
  row("CypherSOL financial profile");
  row("Case", caseId);
  row("Entity", e.canonical_name);
  row("Entity ID", e.entity_id);
  row("Status", entityStatus(e).label);
  row("Derived from counterparty statements", p.derived_from_counterparty_statements ? "yes" : "no");
  row("Linked accounts", (e.linked_account_numbers || []).join(" "));
  row("Linked VPAs", (e.linked_vpas || []).join(" "));
  row("Aliases", (e.aliases || []).join(" | "));
  row("Generated", new Date().toISOString());
  row();
  row("Key figures");
  [["Total credits", k.total_credits], ["Total debits", k.total_debits], ["Net flow", k.net_flow], ["Transactions", k.total_transactions],
    ["Opening balance", k.opening_balance], ["Closing balance", k.closing_balance], ["Daily ABB", k.daily_abb], ["Fixed-date ABB", k.fixed_date_abb],
    ["Active accounts", k.active_accounts]].forEach(([a, b]) => row(a, b));
  (p.mule_assessments || []).forEach((m) => row("Mule score", m.composite_score, m.risk_tier, m.account_number));
  row();
  row("Monthly breakdown");
  row("Month", "Inflow", "Outflow", "Net", "Transactions", "Daily ABB");
  (p.monthly_breakdown || []).forEach((m) => row(m.month, m.credit, m.debit, m.net, m.tx_count, m.abb));
  row();
  row("Category breakdown");
  row("Category", "Group", "Inflow", "Outflow", "Transactions");
  (p.category_breakdown || []).forEach((c) => row(c.category, c.group, c.credit, c.debit, c.tx_count));
  row();
  row("Channel mix");
  row("Channel (SRS)", "Statement codes", "Inflow", "Outflow", "Amount", "Transactions");
  channelRows(p.channel_mix).forEach((c) => row(c.code, c.sources.join(" "), c.credit, c.debit, c.amount, c.tx_count));
  row();
  row("Top senders");
  row("Party", "Amount");
  (p.top_senders || []).forEach((x) => row(x.party, x.amount));
  row();
  row("Top beneficiaries");
  row("Party", "Amount");
  (p.top_beneficiaries || []).forEach((x) => row(x.party, x.amount));
  row();
  row("Source statements");
  row("File", "Bank", "Account", "Period start", "Period end", "SHA-256");
  (p.statements || []).forEach((s) => row(s.source_filename, s.bank_name, s.account_number, s.period_start, s.period_end, s.sha256_hash));
  return lines.join("\r\n");
}
