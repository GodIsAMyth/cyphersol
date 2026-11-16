/* Settings → "Tally ledger map" card (EDGE: per-case ledger / group mapping used by the Tally XML and XLSX
 * exports). GET /api/cases/{id}/edge/tally-map loads the case's map (defaults overlaid), POST saves the
 * whole map ({ledger_map}) or restores the defaults ({reset: true}); the server validates every group and
 * lists all problems in one 400. The map is per case, so the card has its own case picker (defaults to the
 * current case). Nothing here is kept in browser storage. */
import { api, caseUrl, isAbort } from "../core/api.js";
import { delegate, html, render, uid } from "../core/dom.js";
import { plural } from "../core/format.js";
import { icon } from "../components/icons.js";
import { confirmDialog } from "../components/modal.js";
import { spinner } from "../components/states.js";
import { toast, withBusy } from "../components/toast.js";

/** The 28 groups every Tally company starts with (mirror of edge/tally_exporter.PREDEFINED_TALLY_GROUPS). */
export const TALLY_GROUPS = [
  "Branch / Divisions", "Capital Account", "Current Assets", "Current Liabilities", "Direct Expenses", "Direct Incomes",
  "Fixed Assets", "Indirect Expenses", "Indirect Incomes", "Investments", "Loans (Liability)", "Misc. Expenses (ASSET)",
  "Purchase Accounts", "Sales Accounts", "Suspense A/c", "Bank Accounts", "Bank OD A/c", "Cash-in-Hand", "Deposits (Asset)",
  "Duties & Taxes", "Loans & Advances (Asset)", "Provisions", "Reserves & Surplus", "Secured Loans", "Stock-in-Hand",
  "Sundry Creditors", "Sundry Debtors", "Unsecured Loans",
];

const idList = (s) =>
  String(s || "")
    .split(/[\s,;]+/)
    .filter(Boolean)
    .map(Number);
const normCp = (s) => String(s || "").trim().replace(/\s+/g, " ").toUpperCase();

/**
 * Mount the card body into `host`. ctx: the settings view ctx (store, signal, isCurrent, onCleanup).
 */
export function mountLedgerMap(host, ctx) {
  const { store } = ctx;
  const p = uid("st-lm");
  const st = { caseId: null, map: null, isDefault: true, loading: false, error: "", custom: [], cps: [] };
  let ctl = null;

  const cases = () => store.get().cases || [];
  const categories = () => ((store.get().meta && store.get().meta.categories) || []).slice().sort((a, b) => a.category_id - b.category_id);
  const pickDefault = () => {
    const cur = store.get().caseId;
    if (cur && cases().some((c) => c.case_id === cur)) return cur;
    return (cases().find((c) => c.mode === "cypher_edge") || cases()[0] || {}).case_id || null;
  };
  const groupOptions = (selected) => {
    const custom = st.custom.map((r) => r.name.trim()).filter(Boolean);
    const all = [...TALLY_GROUPS, ...custom];
    const extra = selected && !all.includes(selected) ? [selected] : [];
    return html`<optgroup label="Tally predefined">${TALLY_GROUPS.map((g) => html`<option value="${g}" ${g === selected ? html`selected` : ""}>${g}</option>`)}</optgroup>
      ${custom.length ? html`<optgroup label="Custom groups">${custom.map((g) => html`<option value="${g}" ${g === selected ? html`selected` : ""}>${g}</option>`)}</optgroup>` : ""}
      ${extra.map((g) => html`<option value="${g}" selected>${g} (unknown)</option>`)}`;
  };

  function draw() {
    const list = cases();
    if (!list.length) {
      render(host, html`<p class="small muted">Create a case first: the ledger map is saved per case.</p>`);
      return;
    }
    if (!st.caseId || !list.some((c) => c.case_id === st.caseId)) st.caseId = pickDefault();
    const m = st.map;
    render(
      host,
      html`<div class="stack">
        <div class="row-wrap v-settings-lm__bar">
          <div class="field"><label class="field__label" for="${p}-case">Case</label>
            <select class="select input--w-md" id="${p}-case" data-lm="case">
              ${list.map((c) => html`<option value="${c.case_id}" ${c.case_id === st.caseId ? html`selected` : ""}>${c.title || c.case_id}${c.mode === "cypher_edge" ? "" : " (CypherTrace)"}</option>`)}
            </select></div>
          <span data-lm="state">${st.loading ? spinner("Loading ledger map") : m ? html`<span class="badge badge--${st.isDefault ? "neutral" : "info"}">${st.isDefault ? "Defaults" : "Customised for this case"}</span>` : ""}</span>
        </div>
        ${st.error ? html`<div class="callout callout--danger small" role="alert">${icon("alert", { size: "sm" })}<div>${st.error}</div></div>` : ""}
        ${m ? formBody(m) : st.loading ? "" : html`<p class="small muted">Choose a case to load its map.</p>`}
      </div>`
    );
  }

  function formBody(m) {
    const cats = categories();
    const cg = m.category_groups || {};
    const cl = m.category_ledgers || {};
    return html`<form class="stack" data-lm="form" novalidate autocomplete="off">
      <div class="form-grid">
        <div class="field"><label class="field__label" for="${p}-co">Company name <span class="muted">(optional)</span></label>
          <input class="input" id="${p}-co" name="company_name" maxlength="120" value="${m.company_name || ""}" placeholder="As it appears in Tally" /></div>
        <div class="field"><label class="field__label" for="${p}-vp">Voucher number prefix</label>
          <input class="input mono" id="${p}-vp" name="voucher_prefix" maxlength="12" value="${m.voucher_prefix || ""}" /></div>
        <div class="field"><label class="field__label" for="${p}-sc">Sales categories</label>
          <input class="input mono" id="${p}-sc" name="sales_categories" value="${(m.sales_categories || []).join(", ")}" aria-describedby="${p}-cat-hint" /></div>
        <div class="field"><label class="field__label" for="${p}-pc">Purchase categories</label>
          <input class="input mono" id="${p}-pc" name="purchase_categories" value="${(m.purchase_categories || []).join(", ")}" aria-describedby="${p}-cat-hint" /></div>
      </div>
      <span class="field__hint" id="${p}-cat-hint">Category numbers (1–52) separated by commas. With trade vouchers on, these become Sales / Purchase vouchers instead of Receipt / Payment.</span>
      <label class="row small"><input type="checkbox" name="use_trade_vouchers" ${m.use_trade_vouchers ? html`checked` : ""} /> Use Sales / Purchase vouchers for the categories above</label>

      <details class="v-settings-lm__sec" open>
        <summary class="strong">Custom groups <span class="muted small">(${plural(st.custom.length, "group")})</span></summary>
        <p class="xsmall muted">A group that is not predefined in Tally must be declared here with its predefined parent, so the import creates it before any ledger uses it.</p>
        <div class="stack-sm" data-lm="custom">${st.custom.map((r, i) => html`<div class="row-wrap v-settings-lm__row">
            <input class="input" aria-label="Custom group ${i + 1} name" data-custom-name="${i}" value="${r.name}" placeholder="Group name" maxlength="80" />
            <select class="select" aria-label="Parent of custom group ${i + 1}" data-custom-parent="${i}">${TALLY_GROUPS.map((g) => html`<option value="${g}" ${g === r.parent ? html`selected` : ""}>${g}</option>`)}</select>
            <button type="button" class="btn btn--ghost btn--sm" data-lm-del-custom="${i}" aria-label="Remove custom group ${r.name || i + 1}">${icon("x", { size: "sm" })}</button>
          </div>`)}</div>
        <button type="button" class="btn btn--ghost btn--sm" data-lm="add-custom">${icon("plus", { size: "sm" })}Add custom group</button>
      </details>

      <details class="v-settings-lm__sec">
        <summary class="strong">Category → ledger and group <span class="muted small">(${plural(cats.length, "category", "categories")})</span></summary>
        <div class="table-wrap table-wrap--max"><table class="table table--compact v-settings-lm__table">
          <caption class="sr-only">Tally group and ledger name for each category</caption>
          <thead><tr><th scope="col" class="num">#</th><th scope="col">Category</th><th scope="col">Tally group</th><th scope="col">Ledger name</th></tr></thead>
          <tbody>${cats.map((c) => {
            const id = String(c.category_id);
            return html`<tr><td class="num">${id}</td><td class="small">${c.name}</td>
              <td><select class="select select--sm" aria-label="Tally group for ${c.name}" data-cat-group="${id}">${groupOptions(cg[id] || c.tally_parent_group || "Suspense A/c")}</select></td>
              <td><input class="input input--sm" aria-label="Ledger name for ${c.name}" data-cat-ledger="${id}" value="${cl[id] || ""}" placeholder="${c.name}" maxlength="120" /></td></tr>`;
          })}</tbody></table></div>
      </details>

      <details class="v-settings-lm__sec" ${st.cps.length ? html`open` : ""}>
        <summary class="strong">Counterparty ledgers <span class="muted small">(${plural(st.cps.length, "rule")})</span></summary>
        <p class="xsmall muted">Post every transaction with a counterparty to a named ledger (for example a party's Sundry Debtors account). Names are matched case-insensitively.</p>
        <div class="stack-sm">${st.cps.map((r, i) => html`<div class="row-wrap v-settings-lm__row">
            <input class="input" aria-label="Counterparty ${i + 1}" data-cp-name="${i}" value="${r.cp}" placeholder="Counterparty as normalised" maxlength="120" />
            <input class="input" aria-label="Ledger for counterparty ${i + 1}" data-cp-ledger="${i}" value="${r.ledger}" placeholder="Ledger name" maxlength="120" />
            <select class="select" aria-label="Group for counterparty ${i + 1}" data-cp-group="${i}">
              <option value="" ${r.group ? "" : html`selected`}>Category's group</option>${groupOptions(r.group || "")}</select>
            <button type="button" class="btn btn--ghost btn--sm" data-lm-del-cp="${i}" aria-label="Remove counterparty rule ${r.cp || i + 1}">${icon("x", { size: "sm" })}</button>
          </div>`)}</div>
        <button type="button" class="btn btn--ghost btn--sm" data-lm="add-cp">${icon("plus", { size: "sm" })}Add counterparty rule</button>
      </details>

      <div class="row-wrap">
        <button type="submit" class="btn btn--primary">${icon("check", { size: "sm" })}Save ledger map</button>
        <button type="button" class="btn btn--ghost" data-lm="reset" ${st.isDefault ? html`disabled` : ""}>Restore defaults</button>
      </div>
    </form>`;
  }

  // the dynamic rows keep their typed values when rows are added/removed (they are read back before redraw)
  function readRows() {
    const f = host.querySelector('[data-lm="form"]');
    if (!f) return;
    st.custom = st.custom.map((r, i) => ({
      name: f.querySelector(`[data-custom-name="${i}"]`)?.value ?? r.name,
      parent: f.querySelector(`[data-custom-parent="${i}"]`)?.value ?? r.parent,
    }));
    st.cps = st.cps.map((r, i) => ({
      cp: f.querySelector(`[data-cp-name="${i}"]`)?.value ?? r.cp,
      ledger: f.querySelector(`[data-cp-ledger="${i}"]`)?.value ?? r.ledger,
      group: f.querySelector(`[data-cp-group="${i}"]`)?.value ?? r.group,
    }));
    st.map = collect(f);
  }

  function collect(f) {
    const m = { ...(st.map || {}) };
    const val = (n) => f.elements[n]?.value ?? "";
    m.company_name = val("company_name").trim() || null;
    m.voucher_prefix = val("voucher_prefix").trim();
    m.use_trade_vouchers = Boolean(f.elements.use_trade_vouchers?.checked);
    m.sales_categories = idList(val("sales_categories"));
    m.purchase_categories = idList(val("purchase_categories"));
    m.category_groups = { ...(m.category_groups || {}) };
    m.category_ledgers = {};
    f.querySelectorAll("[data-cat-group]").forEach((s) => (m.category_groups[s.dataset.catGroup] = s.value));
    f.querySelectorAll("[data-cat-ledger]").forEach((i) => {
      if (i.value.trim()) m.category_ledgers[i.dataset.catLedger] = i.value.trim();
    });
    m.custom_groups = {};
    st.custom.forEach((r) => {
      if (r.name.trim()) m.custom_groups[r.name.trim()] = r.parent;
    });
    m.counterparty_ledgers = {};
    st.cps.forEach((r) => {
      if (normCp(r.cp) && r.ledger.trim()) m.counterparty_ledgers[normCp(r.cp)] = { ledger: r.ledger.trim(), group: r.group || null };
    });
    return m;
  }

  function adopt(res) {
    st.map = res.ledger_map || {};
    st.isDefault = Boolean(res.is_default);
    st.custom = Object.entries(st.map.custom_groups || {}).map(([name, parent]) => ({ name, parent }));
    st.cps = Object.entries(st.map.counterparty_ledgers || {}).map(([cp, v]) => ({ cp, ledger: (v && v.ledger) || "", group: (v && v.group) || "" }));
  }

  async function load() {
    if (!st.caseId) return draw();
    ctl?.abort();
    ctl = new AbortController();
    const signal = ctl.signal;
    ctx.signal?.addEventListener("abort", () => ctl?.abort(), { once: true });
    st.loading = true;
    st.error = "";
    st.map = null;
    draw();
    try {
      const res = await api.get(caseUrl(st.caseId, "edge/tally-map"), { signal });
      if (!ctx.isCurrent() || signal.aborted) return;
      adopt(res);
    } catch (err) {
      if (isAbort(err) || !ctx.isCurrent()) return;
      st.error = err?.message || "Could not load the ledger map.";
    }
    st.loading = false;
    draw();
  }

  async function save(btn) {
    readRows();
    const ledger_map = st.map;
    await withBusy(btn, async () => {
      try {
        const res = await api.post(caseUrl(st.caseId, "edge/tally-map"), { ledger_map }, { signal: ctx.signal });
        if (!ctx.isCurrent()) return;
        adopt(res);
        st.error = "";
        draw();
        toast.success("Tally exports for this case will use the saved map.", { title: "Ledger map saved" });
      } catch (err) {
        if (isAbort(err) || !ctx.isCurrent()) return;
        st.error = err?.message || "The ledger map was not saved.";
        draw();
        host.querySelector('[role="alert"]')?.scrollIntoView({ block: "nearest" });
      }
    }, { silent: true });
  }

  async function reset(btn) {
    const ok = await confirmDialog({
      title: "Restore the default ledger map?",
      message: "Custom groups, ledger names and counterparty rules for this case are removed. The change is audited.",
      confirmLabel: "Restore defaults",
    });
    if (!ok || !ctx.isCurrent()) return;
    await withBusy(btn, async () => {
      const res = await api.post(caseUrl(st.caseId, "edge/tally-map"), { reset: true }, { signal: ctx.signal });
      if (!ctx.isCurrent()) return;
      adopt(res);
      st.error = "";
      draw();
    }, { success: "Default ledger map restored." });
  }

  const offs = [
    delegate(host, "change", '[data-lm="case"]', (e, s) => {
      st.caseId = s.value;
      load();
    }),
    delegate(host, "submit", '[data-lm="form"]', (e) => {
      e.preventDefault();
      save(e.target.querySelector('button[type="submit"]'));
    }),
    delegate(host, "click", '[data-lm="reset"]', (e, b) => reset(b)),
    delegate(host, "click", '[data-lm="add-custom"]', () => {
      readRows();
      st.custom.push({ name: "", parent: "Indirect Expenses" });
      draw();
      host.querySelector(`[data-custom-name="${st.custom.length - 1}"]`)?.focus();
    }),
    delegate(host, "click", '[data-lm="add-cp"]', () => {
      readRows();
      st.cps.push({ cp: "", ledger: "", group: "" });
      draw();
      host.querySelector(`[data-cp-name="${st.cps.length - 1}"]`)?.focus();
    }),
    delegate(host, "click", "[data-lm-del-custom]", (e, b) => {
      readRows();
      st.custom.splice(Number(b.dataset.lmDelCustom), 1);
      draw();
    }),
    delegate(host, "click", "[data-lm-del-cp]", (e, b) => {
      readRows();
      st.cps.splice(Number(b.dataset.lmDelCp), 1);
      draw();
    }),
    // a renamed custom group must appear in the group pickers: redraw on blur, keeping the typed values
    delegate(host, "change", "[data-custom-name]", () => {
      readRows();
      draw();
    }),
  ];
  ctx.onCleanup(() => {
    offs.forEach((f) => f());
    ctl?.abort();
  });
  // the case list or the categories can arrive after mount
  const late = () => {
    if (!ctx.isCurrent()) return;
    if (!st.caseId && cases().length) {
      st.caseId = pickDefault();
      load();
    } else {
      readRows(); // keeps unsaved edits; the category table needs meta.categories
      draw();
    }
  };
  ctx.onCleanup(store.select((s) => s.cases, late));
  ctx.onCleanup(store.select((s) => s.meta, late));

  st.caseId = pickDefault();
  if (st.caseId) load();
  else draw();
}
