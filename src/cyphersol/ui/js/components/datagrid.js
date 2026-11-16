/* Virtualised data grid with server pagination, sorting, toolbar filters and keyboard row navigation
 * (report UI-S-02 / item 37). Works in two modes:
 *   - server mode: pass fetchPage({ offset, limit, sort, order, filters, signal }) -> { rows, total }
 *   - client mode: pass rows: [...] (optionally clientFilter(row, filters)); sorting happens in memory.
 *
 * const grid = createDataGrid(el, {
 *   ariaLabel: "Transactions",
 *   rowKey: "tx_id",
 *   columns: [
 *     { key: "txn_date", label: "Date", width: "110px", sortable: true, format: (v) => date(v) },
 *     { key: "raw_narration", label: "Narration", width: "minmax(260px, 3fr)", sortable: true,
 *       render: (r) => html`${r.raw_narration}<span class="dg__sub">${r.extracted_counterparty || "—"}</span>` },
 *     { key: "debit", label: "Debit", width: "130px", align: "right", sortable: true, defaultOrder: "desc",
 *       format: (v) => (v > 0 ? inr(v) : "—"), className: "is-debit" },
 *   ],
 *   filters: [
 *     { key: "q", type: "search", label: "Search", placeholder: "Narration, party, UTR…" },
 *     { key: "category_id", type: "select", label: "Category", options: [{ value: "", label: "All categories" }, …] },
 *     { type: "date-range", label: "Date", keys: ["date_from", "date_to"] },
 *     { type: "number-range", label: "Amount", keys: ["amount_min", "amount_max"] },
 *     { key: "suspense", type: "toggle", label: "Suspense only" },
 *   ],
 *   initialFilters: { q: ctx.params.q }, initialSort: { key: "txn_date", order: "asc" },
 *   fetchPage: ({ offset, limit, sort, order, filters, signal }) =>
 *     ctx.api.get("transactions", { offset, limit, sort, order, ...filters }, { signal })
 *       .then((r) => ({ rows: r.transactions, total: r.total })),
 *   onRowClick: (row, { index }) => openDetail(row),
 *   onStateChange: ({ sort, order, filters }) => ctx.setParams({ ...filters, sort, order }),
 *   emptyState: { icon: "list", title: "No transactions", message: "Upload statements to begin." },
 *   countNoun: ["transaction", "transactions"], height: "min(68vh, 640px)", rowHeight: 40, pageSize: 200,
 *   toolbarExtra: html`<button class="btn btn--sm" data-action="export-view">Export view</button>`,
 * });
 * grid.reload(); grid.refresh(); grid.setFilters({ q: "x" }); grid.setSort("debit", "desc");
 * grid.updateRow(txId, { category_id: 7 }); grid.getRow(index); grid.getState(); grid.setRows(rows); grid.destroy();
 * Row buttons: give them data-action and read the row via grid.rowFromEvent(e) (rows carry data-index).
 * Optional: rowClassName(row) -> "class names" for each body row; onRowToggle(row, {index}) makes Space toggle
 * (e.g. a row checkbox) instead of opening the row; select filter options may be grouped:
 * options: [{ value: "", label: "All" }, { group: "Income", options: [{ value, label }, …] }].
 * grid.getVisibleRows() (client mode: filtered + sorted rows; server mode: loaded rows in order),
 * grid.getActiveIndex().
 */
import { debounce, html, render, setVars, uid } from "../core/dom.js";
import { isAbort } from "../core/api.js";
import { int } from "../core/format.js";
import { icon } from "./icons.js";
import { emptyState, errorState } from "./states.js";

const HEAD_H = 34;
const OVERSCAN = 8;
/* Browsers cap element height (~33.5M px in Chrome, ~17.9M in Firefox). Above this the canvas is capped and
 * scroll positions are mapped proportionally onto row offsets, so every row stays reachable. */
const MAX_CANVAS_PX = 15_000_000;

export function createDataGrid(container, opts) {
  const {
    columns,
    fetchPage = null,
    clientFilter = null,
    filters: filterDefs = [],
    initialFilters = {},
    initialSort = null,
    pageSize = 200,
    rowHeight = 36,
    height = "min(68vh, 640px)",
    rowKey = "id",
    onRowClick = null,
    onRowToggle = null,
    rowClassName = null,
    onStateChange = null,
    emptyState: emptyOpts = {},
    ariaLabel = "Data table",
    countNoun = ["row", "rows"],
    toolbarExtra = null,
    showToolbar = true,
  } = opts;
  let clientRows = Array.isArray(opts.rows) ? opts.rows : null;
  const gridId = uid("dg");
  const keyOf = typeof rowKey === "function" ? rowKey : (r) => r?.[rowKey];

  const state = {
    sort: initialSort?.key || null,
    order: initialSort?.order || "asc",
    filters: { ...cleanFilters(initialFilters) },
    total: null,
    activeIndex: -1,
    error: null,
  };
  let gen = 0;
  let controller = new AbortController();
  const pages = new Map();
  const pending = new Map();
  const failed = new Map(); // page -> error; never refetched automatically (no retry storm), only via Retry
  let view = []; // client mode: filtered + sorted rows
  let rafId = 0;
  let lastRenderKey = "";
  let dataVersion = 0;

  // ------------------------------------------------------------------ skeleton
  render(
    container,
    html`<div class="dg ${onRowClick ? "dg--clickable" : ""}">
      ${showToolbar
        ? html`<div class="dg__toolbar">
            <div class="dg__filters">${filterDefs.map(renderFilter)}</div>
            <div class="dg__extra">${toolbarExtra || ""}</div>
            <div class="dg__count" aria-hidden="true"></div>
          </div>`
        : ""}
      <span class="sr-only" role="status" aria-live="polite" data-dg-status></span>
      <div class="dg__frame">
        <div class="dg__loadbar" hidden></div>
        <div class="dg__scroll" role="grid" tabindex="0" aria-label="${ariaLabel}" aria-colcount="${columns.length}" aria-rowcount="-1" id="${gridId}">
          <div class="dg__table">
            <div class="dg__head" role="rowgroup">
              <div class="dg__row" role="row" aria-rowindex="1">
                ${columns.map(
                  (c, ci) => html`<div class="dg__th" role="columnheader" aria-colindex="${ci + 1}" data-align="${c.align || "left"}"
                      data-col="${c.key}" ${c.sortable ? html`aria-sort="none"` : ""}>
                      ${c.sortable
                        ? html`<button type="button" class="dg__sort" data-sort="${c.sortKey || c.key}">${c.label}${icon("sort", { size: "sm" })}</button>`
                        : html`<span class="truncate">${c.label}</span>`}
                    </div>`
                )}
              </div>
            </div>
            <div class="dg__body" role="rowgroup"><div class="dg__canvas"><div class="dg__window"></div></div></div>
          </div>
        </div>
        <div class="dg__overlay" hidden></div>
      </div>
    </div>`
  );
  const root = container.querySelector(".dg");
  const scroller = root.querySelector(".dg__scroll");
  const canvas = root.querySelector(".dg__canvas");
  const win = root.querySelector(".dg__window");
  const overlay = root.querySelector(".dg__overlay");
  const loadbar = root.querySelector(".dg__loadbar");
  const countEl = root.querySelector(".dg__count");
  const statusEl = root.querySelector("[data-dg-status]");

  const minWidth = columns.reduce((sum, c) => sum + minPx(c.width), 0);
  setVars(root, {
    "--dg-cols": columns.map((c) => c.width || "minmax(110px, 1fr)").join(" "),
    "--dg-row-h": `${rowHeight}px`,
    "--dg-height": height,
    "--dg-min-width": `${Math.max(minWidth, 0)}px`,
  });
  syncSortHeaders();
  syncFilterControls();

  // ------------------------------------------------------------------ filters
  function renderFilter(f) {
    const id = uid("dgf");
    if (f.type === "search") {
      return html`<label class="input-group" for="${id}"><span class="sr-only">${f.label || "Search"}</span>${icon("search", { size: "sm" })}
        <input id="${id}" class="input" type="search" data-filter="${f.key}" placeholder="${f.placeholder || "Search…"}" autocomplete="off" /></label>`;
    }
    if (f.type === "select") {
      return html`<label class="sr-only" for="${id}">${f.label}</label>
        <select id="${id}" class="select ${f.wide ? "" : "input--w-md"}" data-filter="${f.key}">
          ${(f.options || []).map((o) =>
            Array.isArray(o.options)
              ? html`<optgroup label="${o.group || ""}">${o.options.map((x) => html`<option value="${x.value}">${x.label}</option>`)}</optgroup>`
              : html`<option value="${o.value}">${o.label}</option>`
          )}
        </select>`;
    }
    if (f.type === "date-range" || f.type === "number-range") {
      const t = f.type === "date-range" ? "date" : "number";
      const [a, b] = f.keys || [];
      return html`<span class="dg__range" role="group" aria-label="${f.label}">
        <span class="xsmall muted">${f.label}</span>
        <input class="input input--sm ${t === "number" ? "input--num" : ""}" type="${t}" data-filter="${a}" aria-label="${f.label} from"
          placeholder="${t === "number" ? "Min" : ""}" ${t === "number" ? html`inputmode="decimal" min="0" step="any"` : ""} />
        <span class="muted" aria-hidden="true">–</span>
        <input class="input input--sm ${t === "number" ? "input--num" : ""}" type="${t}" data-filter="${b}" aria-label="${f.label} to"
          placeholder="${t === "number" ? "Max" : ""}" ${t === "number" ? html`inputmode="decimal" min="0" step="any"` : ""} />
      </span>`;
    }
    if (f.type === "toggle") {
      return html`<button type="button" class="chip" data-filter-toggle="${f.key}" aria-pressed="false">${f.label}</button>`;
    }
    return "";
  }

  function syncFilterControls() {
    root.querySelectorAll("[data-filter]").forEach((input) => {
      const v = state.filters[input.dataset.filter];
      input.value = v == null ? "" : String(v);
    });
    root.querySelectorAll("[data-filter-toggle]").forEach((b) => {
      b.setAttribute("aria-pressed", state.filters[b.dataset.filterToggle] ? "true" : "false");
    });
  }

  const applyFilterDebounced = debounce(() => resetData(), 300);
  root.addEventListener("input", (e) => {
    const input = e.target.closest("[data-filter]");
    if (!input) return;
    setFilterValue(input.dataset.filter, input.value);
    if (input.tagName === "SELECT" || input.type === "date") {
      applyFilterDebounced.cancel();
      resetData();
    } else applyFilterDebounced();
  });
  root.addEventListener("click", (e) => {
    const t = e.target.closest("[data-filter-toggle]");
    if (t) {
      const k = t.dataset.filterToggle;
      setFilterValue(k, state.filters[k] ? "" : "true");
      t.setAttribute("aria-pressed", state.filters[k] ? "true" : "false");
      resetData();
      return;
    }
    const s = e.target.closest("[data-sort]");
    if (s) {
      const key = s.dataset.sort;
      const col = columns.find((c) => (c.sortKey || c.key) === key);
      if (state.sort === key) state.order = state.order === "asc" ? "desc" : "asc";
      else {
        state.sort = key;
        state.order = col?.defaultOrder || "asc";
      }
      syncSortHeaders();
      resetData();
      return;
    }
    if (e.target.closest('[data-action="dg-clear-filters"]')) {
      state.filters = {};
      syncFilterControls();
      resetData();
    } else if (e.target.closest('[data-action="dg-retry"]')) {
      if (pages.size && !clientRows) {
        // retry only the pages that failed; loaded pages stay as they are
        failed.clear();
        state.error = null;
        statusEl.textContent = "";
        dataVersion += 1;
        schedule();
      } else resetData({ keepScroll: true });
    }
  });

  function setFilterValue(key, value) {
    if (value === "" || value == null) delete state.filters[key];
    else state.filters[key] = value;
  }

  function syncSortHeaders() {
    root.querySelectorAll(".dg__th[aria-sort]").forEach((th) => {
      const btn = th.querySelector("[data-sort]");
      const active = btn && btn.dataset.sort === state.sort;
      th.setAttribute("aria-sort", active ? (state.order === "desc" ? "descending" : "ascending") : "none");
      const ic = th.querySelector(".icon");
      if (ic) ic.outerHTML = String(icon(active ? (state.order === "desc" ? "sort-desc" : "sort-asc") : "sort", { size: "sm" }));
    });
  }

  // ------------------------------------------------------------------ data
  function emitState() {
    onStateChange?.({ sort: state.sort, order: state.order, filters: { ...state.filters } });
  }

  function resetData({ keepScroll = false, silent = false } = {}) {
    gen += 1;
    controller.abort();
    controller = new AbortController();
    pages.clear();
    pending.clear();
    failed.clear();
    state.error = null;
    dataVersion += 1;
    if (!keepScroll) {
      scroller.scrollTop = 0;
      state.activeIndex = -1;
      state.total = null;
    }
    if (!silent) emitState();
    if (clientRows) {
      computeClientView();
      schedule();
    } else {
      ensurePage(0);
      schedule();
    }
  }

  function computeClientView() {
    let rows = clientRows.slice();
    if (clientFilter) rows = rows.filter((r) => clientFilter(r, state.filters));
    if (state.sort) {
      const col = columns.find((c) => (c.sortKey || c.key) === state.sort);
      const get = col?.sortValue || ((r) => r[state.sort]);
      const dir = state.order === "desc" ? -1 : 1;
      rows.sort((a, b) => cmp(get(a), get(b)) * dir);
    }
    view = rows;
    state.total = rows.length;
    updateCanvas();
  }

  function ensurePage(p) {
    if (clientRows || pages.has(p) || pending.has(p) || failed.has(p)) return;
    if (state.total != null && p * pageSize >= state.total && p !== 0) return;
    const myGen = gen;
    const { signal } = controller;
    const promise = Promise.resolve()
      .then(() =>
        fetchPage({ offset: p * pageSize, limit: pageSize, sort: state.sort, order: state.order, filters: { ...state.filters }, signal })
      )
      .then((res) => {
        if (myGen !== gen) return;
        failed.delete(p);
        pages.set(p, (res && res.rows) || []);
        const total = Number(res && res.total);
        state.total = Number.isFinite(total) ? total : pages.get(p).length;
        dataVersion += 1;
        updateCanvas();
      })
      .catch((err) => {
        if (isAbort(err) || myGen !== gen) return;
        console.error(err);
        state.error = err;
        failed.set(p, err); // shown as an error strip with Retry; not re-requested on the next draw
        dataVersion += 1;
        if (pages.size) statusEl.textContent = "Some rows could not be loaded. Use Retry to try again.";
      })
      .finally(() => {
        if (myGen !== gen) return;
        pending.delete(p);
        schedule();
      });
    pending.set(p, promise);
  }

  function rowAt(i) {
    if (clientRows) return view[i];
    const page = pages.get(Math.floor(i / pageSize));
    return page ? page[i % pageSize] : undefined;
  }

  function updateCanvas() {
    const total = state.total || 0;
    canvas.style.height = `${Math.min(total * rowHeight, MAX_CANVAS_PX)}px`;
    scroller.setAttribute("aria-rowcount", String(total + 1));
    statusEl.textContent = state.total == null ? "" : `${int(total)} ${total === 1 ? countNoun[0] : countNoun[1]}`;
  }

  // ------------------------------------------------------------------ rendering
  // rAF for smooth scrolling, with a timer fallback so hidden/background documents still render.
  let timerId = 0;
  function schedule() {
    if (rafId || timerId) return;
    const run = () => {
      cancelAnimationFrame(rafId);
      clearTimeout(timerId);
      rafId = 0;
      timerId = 0;
      draw();
    };
    rafId = requestAnimationFrame(run);
    timerId = setTimeout(run, 50);
  }

  /** Virtual (row-space) offset for the current scrollTop, and the inverse; identity below MAX_CANVAS_PX. */
  function scale(vh) {
    const real = (state.total || 0) * rowHeight;
    const capped = Math.min(real, MAX_CANVAS_PX);
    return capped >= real || capped <= vh ? 1 : (real - vh) / (capped - vh);
  }
  function virtualTop(vh = Math.max(0, scroller.clientHeight - HEAD_H)) {
    return scroller.scrollTop * scale(vh);
  }
  function scrollToVirtual(top, vh = Math.max(0, scroller.clientHeight - HEAD_H)) {
    scroller.scrollTop = top / scale(vh);
  }

  function visibleRange() {
    const vh = Math.max(0, scroller.clientHeight - HEAD_H);
    const top = virtualTop(vh);
    const total = state.total || 0;
    const first = Math.max(0, Math.floor(top / rowHeight));
    const last = Math.min(total - 1, Math.ceil((top + vh) / rowHeight) - 1);
    return { first, last, vh };
  }

  function draw() {
    drawInner();
    // An auto-height grid with no rows is only a header tall: give the loading/error/empty overlay room.
    root.classList.toggle("dg--overlay", !overlay.hidden);
  }

  function drawInner() {
    const total = state.total;
    loadbar.hidden = pending.size === 0 || total == null;

    // overlays: initial loading / error / empty
    if (state.error && !pages.size && !clientRows) {
      overlay.hidden = false;
      render(overlay, errorState(state.error, { retryId: "dg-retry", title: "Could not load rows", compact: true }));
      win.replaceChildren();
      countEl && (countEl.textContent = "");
      return;
    }
    if (total == null) {
      overlay.hidden = false;
      render(overlay, html`<div class="skeleton-stack" aria-hidden="true">${Array.from({ length: 8 }, () => html`<div class="skeleton skeleton--line"></div>`)}</div>`);
      return;
    }
    if (total === 0) {
      overlay.hidden = false;
      const filtered = Object.keys(state.filters).length > 0;
      render(
        overlay,
        filtered
          ? emptyState({ icon: "filter", title: "No matching rows", message: "Nothing matches the current filters.", action: { label: "Clear filters", id: "dg-clear-filters" }, compact: true })
          : emptyState({ compact: true, ...emptyOpts })
      );
      win.replaceChildren();
      if (countEl) countEl.textContent = `0 ${countNoun[1]}`;
      return;
    }
    overlay.hidden = true;

    const { first, last } = visibleRange();
    const start = Math.max(0, first - OVERSCAN);
    const end = Math.min(total - 1, last + OVERSCAN);
    if (!clientRows) {
      for (let p = Math.floor(start / pageSize); p <= Math.floor(end / pageSize); p += 1) ensurePage(p);
    }
    if (countEl) countEl.textContent = `${int(first + 1)}–${int(Math.max(first + 1, last + 1))} of ${int(total)}`;

    // in capped (scaled) mode the window is placed relative to the viewport, so update it on every draw
    win.style.transform = `translateY(${scroller.scrollTop + start * rowHeight - virtualTop()}px)`;
    const key = `${start}:${end}:${dataVersion}:${state.activeIndex}`;
    if (key === lastRenderKey) return;
    lastRenderKey = key;
    retryShown = false;
    const rows = [];
    for (let i = start; i <= end; i += 1) rows.push(renderRow(i, rowAt(i)));
    render(win, html`${rows}`);
    if (state.activeIndex >= start && state.activeIndex <= end) scroller.setAttribute("aria-activedescendant", `${gridId}-r${state.activeIndex}`);
    else scroller.removeAttribute("aria-activedescendant");
  }

  let retryShown = false;
  function renderRow(i, row) {
    const active = i === state.activeIndex;
    if (!row && !clientRows && failed.has(Math.floor(i / pageSize))) {
      const withButton = !retryShown;
      retryShown = true;
      return html`<div class="dg__row dg__row--failed" role="row" aria-rowindex="${i + 2}" id="${gridId}-r${i}" data-index="${i}">
        <div class="dg__td dg__td--failed" role="gridcell" aria-colindex="1">${icon("alert", { size: "sm" })}<span class="truncate">Could not load rows</span>
          ${withButton ? html`<button type="button" class="btn btn--sm" data-action="dg-retry">Retry</button>` : ""}</div>
        ${columns.slice(1).map((_, ci) => html`<div class="dg__td" role="gridcell" aria-colindex="${ci + 2}"></div>`)}</div>`;
    }
    if (!row) {
      return html`<div class="dg__row" role="row" aria-rowindex="${i + 2}" id="${gridId}-r${i}" data-index="${i}" aria-busy="true">
        ${columns.map(() => html`<div class="dg__td" role="gridcell"><div class="dg__placeholder"></div></div>`)}</div>`;
    }
    let extraCls = "";
    if (rowClassName) {
      try {
        extraCls = String(rowClassName(row, i) || "");
      } catch (err) {
        console.error(err);
      }
    }
    return html`<div class="dg__row ${active ? "is-active" : ""} ${extraCls}" role="row" aria-rowindex="${i + 2}" id="${gridId}-r${i}" data-index="${i}"
        ${active ? html`aria-selected="true"` : ""}>
      ${columns.map((c, ci) => {
        let content;
        try {
          content = c.render ? c.render(row, i) : c.format ? c.format(row[c.key], row) : row[c.key] ?? "—";
        } catch (err) {
          console.error(err);
          content = "—";
        }
        const cls = typeof c.className === "function" ? c.className(row) : c.className || "";
        const title = c.title ? c.title(row) : "";
        return html`<div class="dg__td ${c.wrap ? "dg__td--wrap" : ""} ${cls}" role="gridcell" aria-colindex="${ci + 1}"
            data-align="${c.align || "left"}" ${title ? html`title="${title}"` : ""}>${content}</div>`;
      })}
    </div>`;
  }

  // ------------------------------------------------------------------ interaction
  scroller.addEventListener("scroll", schedule, { passive: true });
  const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(schedule) : null;
  ro?.observe(scroller);

  scroller.addEventListener("click", (e) => {
    const rowEl = e.target.closest(".dg__body .dg__row[data-index]");
    if (!rowEl) return;
    const index = Number(rowEl.dataset.index);
    setActive(index, { scroll: false });
    if (e.target.closest("button, a, input, select, textarea, label, [data-action]")) return;
    const row = rowAt(index);
    if (row && onRowClick) onRowClick(row, { index, event: e });
  });

  scroller.addEventListener("focus", () => {
    if (state.activeIndex < 0 && state.total) setActive(visibleRange().first, { scroll: false });
  });

  scroller.addEventListener("keydown", (e) => {
    if (e.target !== scroller || !state.total) return;
    const { first, last } = visibleRange();
    const pageRows = Math.max(1, last - first);
    let next = null;
    if (e.key === "ArrowDown") next = state.activeIndex + 1;
    else if (e.key === "ArrowUp") next = state.activeIndex - 1;
    else if (e.key === "PageDown") next = state.activeIndex + pageRows;
    else if (e.key === "PageUp") next = state.activeIndex - pageRows;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = state.total - 1;
    else if (e.key === " " && onRowToggle) {
      const row = rowAt(state.activeIndex);
      if (row) {
        e.preventDefault();
        onRowToggle(row, { index: state.activeIndex, event: e });
      }
      return;
    } else if (e.key === "Enter" || e.key === " ") {
      const row = rowAt(state.activeIndex);
      if (row && onRowClick) {
        e.preventDefault();
        onRowClick(row, { index: state.activeIndex, event: e });
      }
      return;
    }
    if (next === null) return;
    e.preventDefault();
    setActive(Math.max(0, Math.min(state.total - 1, next)));
  });

  function setActive(index, { scroll = true } = {}) {
    state.activeIndex = index;
    if (scroll) {
      const top = index * rowHeight;
      const vh = Math.max(0, scroller.clientHeight - HEAD_H);
      const cur = virtualTop(vh);
      if (top < cur) scrollToVirtual(top, vh);
      else if (top + rowHeight > cur + vh) scrollToVirtual(top + rowHeight - vh, vh);
    }
    schedule();
  }

  // ------------------------------------------------------------------ public api
  const api = {
    el: root,
    scroller,
    toolbar: root.querySelector(".dg__extra"),
    /** Refetch from scratch (filters/sort unchanged). keepScroll keeps position and active row. */
    reload: ({ keepScroll = false } = {}) => resetData({ keepScroll, silent: true }),
    /** Refetch keeping scroll position — use after a mutation. */
    refresh: () => resetData({ keepScroll: true, silent: true }),
    setFilters(patch, { replace = false, silent = false } = {}) {
      state.filters = cleanFilters(replace ? { ...patch } : { ...state.filters, ...patch });
      syncFilterControls();
      resetData({ silent });
    },
    getFilters: () => ({ ...state.filters }),
    setSort(key, order = "asc", { silent = false } = {}) {
      state.sort = key;
      state.order = order === "desc" ? "desc" : "asc";
      syncSortHeaders();
      resetData({ silent });
    },
    getState: () => ({ sort: state.sort, order: state.order, filters: { ...state.filters }, total: state.total }),
    /** Client mode: replace all rows. */
    setRows(rows) {
      clientRows = Array.isArray(rows) ? rows : [];
      resetData({ keepScroll: true, silent: true });
    },
    getRow: (index) => rowAt(index),
    getActiveIndex: () => state.activeIndex,
    /** Client mode: the filtered + sorted rows; server mode: the loaded rows in display order. */
    getVisibleRows() {
      if (clientRows) return view.slice();
      return [...pages.keys()].sort((a, b) => a - b).flatMap((p) => pages.get(p));
    },
    /** The row object for an event inside a grid row (for delegated row buttons). */
    rowFromEvent(e) {
      const rowEl = e.target.closest?.(".dg__row[data-index]");
      return rowEl ? rowAt(Number(rowEl.dataset.index)) : undefined;
    },
    /** Patch a loaded row in place by key (object merge, or fn(row) -> newRow). Returns true if found. */
    updateRow(key, patch) {
      const apply = (arr) => {
        const i = arr.findIndex((r) => keyOf(r) === key);
        if (i < 0) return false;
        arr[i] = typeof patch === "function" ? patch(arr[i]) : { ...arr[i], ...patch };
        return true;
      };
      let found = false;
      if (clientRows) found = apply(clientRows) && (computeClientView(), true);
      else for (const arr of pages.values()) found = apply(arr) || found;
      if (found) {
        dataVersion += 1;
        schedule();
      }
      return found;
    },
    focus: () => scroller.focus(),
    destroy() {
      gen += 1;
      controller.abort();
      ro?.disconnect();
      cancelAnimationFrame(rafId);
      clearTimeout(timerId);
      applyFilterDebounced.cancel();
      container.replaceChildren();
    },
  };

  resetData({ silent: true });
  return api;
}

function cleanFilters(f) {
  const out = {};
  for (const [k, v] of Object.entries(f || {})) if (v !== "" && v != null) out[k] = v;
  return out;
}

function minPx(width) {
  const m = /(\d+(?:\.\d+)?)px/.exec(String(width || "110px"));
  return m ? Number(m[1]) : 110;
}

function cmp(a, b) {
  if (a == null && b == null) return 0;
  if (a == null) return -1;
  if (b == null) return 1;
  if (typeof a === "number" && typeof b === "number") return a - b;
  const na = Number(a);
  const nb = Number(b);
  if (a !== "" && b !== "" && Number.isFinite(na) && Number.isFinite(nb)) return na - nb;
  return String(a).localeCompare(String(b), "en-IN", { numeric: true, sensitivity: "base" });
}
