/* CypherSOL SPA entry: app shell (sidebar, top bar, export menu, theme, ⌘K palette, cloud-AI banner),
 * hash router and global commands. Views live in js/views/<name>.js and follow the contract in router.js. */
import { API_TOKEN, caseApi } from "./core/api.js";
import { registerCommands } from "./core/commands.js";
import { CASE_MODES, EXPORTS, STORAGE_KEYS, caseModeLabel, execModeLabel, lsGet, lsSet } from "./core/constants.js";
import { announce, delegate, html, render, trapFocus } from "./core/dom.js";
import { NAV_BADGES, VIEWS, moreViews, primaryViews, viewTitle } from "./core/nav.js";
import { createRouter, href, loadStyles } from "./core/router.js";
import { TOPICS, store } from "./core/store.js";
import { cycleTheme, getThemePref, onThemeChange, setThemePref } from "./core/theme.js";
import { icon } from "./components/icons.js";
import { createMenu } from "./components/menu.js";
import { initPalette, openPalette } from "./components/palette.js";
import { errorState, viewSkeleton } from "./components/states.js";
import { toast, withBusy } from "./components/toast.js";

const $ = (id) => document.getElementById(id);
const appEl = $("app");
const main = $("main");

let router = null;
let extraCrumbs = [];

// =====================================================================================================
// Cloud-AI banner (LLM-18): persistent whenever /api/health reports cloud_egress_possible.
// =====================================================================================================
function updateCloudBanner() {
  const { health, llmConfig } = store.get();
  const banner = $("cloud-ai-banner");
  if (!banner) return;
  const cfg = llmConfig || {};
  const egress = health ? Boolean(health.cloud_egress_possible) : Boolean(cfg.cloud_egress_possible);
  banner.hidden = !egress;
  if (egress) {
    const provider = (health && health.llm_provider) || cfg.provider || "an external provider";
    const mode = (health && health.effective_execution_mode) || cfg.effective_execution_mode || cfg.execution_mode || "";
    $("cloud-ai-banner-detail").textContent =
      `an API key for ${provider} is active (default mode: ${execModeLabel(mode)}). ` +
      "Statement content may be sent to this external AI provider when an AI mode is used.";
  }
}

// =====================================================================================================
// Sidebar
// =====================================================================================================
function renderCaseSwitcher() {
  const { case: c, caseId, caseStatus } = store.get();
  const slot = $("case-switcher-slot");
  const pending = caseStatus === "error" ? "Case unavailable" : "Loading…";
  // The button element is created once and patched in place, so keyboard focus (e.g. returned to it by the
  // case menu) survives store updates.
  let btn = $("btn-case-switcher");
  if (!btn) {
    render(slot, html`<button type="button" class="case-switcher" id="btn-case-switcher"></button>`);
    btn = $("btn-case-switcher");
    createMenu(btn, {
      label: "Switch case",
      align: "start",
      className: "menu--cases",
      items: () => {
        const { cases, caseId: cur } = store.get();
        return [
          { group: "Cases" },
          ...cases.map((x) => ({
            id: x.case_id,
            label: x.title,
            description: `${caseModeLabel(x.mode)}${x.agency_or_firm_name ? ` · ${x.agency_or_firm_name}` : ""}`,
            icon: x.mode === "cypher_edge" ? "receipt" : "network",
            checked: x.case_id === cur,
            run: () => switchCase(x.case_id),
          })),
          { separator: true },
          { id: "new", label: "New case…", icon: "plus", run: openNewCaseDialog },
          { id: "all", label: "All cases", icon: "folder", href: "#/cases" },
        ];
      },
    });
  }
  btn.title = c ? c.title : "Select a case";
  render(
    btn,
    html`${icon(c && c.mode === "cypher_edge" ? "receipt" : "network")}
      <span class="case-switcher__text">
        <span class="case-switcher__mode">${c ? caseModeLabel(c.mode) : caseId ? pending : "No case selected"}</span>
        <span class="case-switcher__title">${c ? c.title : "Choose a case"}</span>
      </span>
      ${icon("chevrons-up-down", { size: "sm", className: "icon-chevron" })}`
  );
}

function renderNav() {
  const { caseId, mode, summary } = store.get();
  const route = router ? router.current() : null;
  const active = route ? route.name : null;
  const link = (name) => {
    const v = VIEWS[name];
    const badge = NAV_BADGES[name] ? NAV_BADGES[name](summary) : null;
    const h = v.scope === "global" ? href(name) : href(name, { caseId });
    return html`<li><a class="nav-link" href="${h}" ${active === name ? html`aria-current="page"` : ""} title="${v.title}" data-nav="${name}">
      ${icon(v.icon)}<span class="nav-link__label">${v.title}</span>
      ${badge ? html`<span class="nav-link__badge" data-tone="${badge.tone}" aria-hidden="true">${badge.count > 999 ? "999+" : badge.count}</span><span class="sr-only">, ${badge.count} ${badge.label}</span>` : ""}
    </a></li>`;
  };
  // re-rendering replaces the links: remember which nav control had focus and restore it afterwards
  const ae = document.activeElement;
  const inNav = ae && ($("sidebar-nav").contains(ae) || $("sidebar-foot").contains(ae));
  const focusKey = inNav ? (ae.dataset.nav ? `[data-nav="${ae.dataset.nav}"]` : ae.id ? `#${ae.id}` : null) : null;
  const moreOpen = lsGet(STORAGE_KEYS.navMore, "0") === "1" || (active && moreViews(mode).includes(active));
  const other = mode === "cypher_edge" ? CASE_MODES.cypher_trace.label : CASE_MODES.cypher_edge.label;
  render(
    $("sidebar-nav"),
    caseId
      ? html`<div class="nav-section">
            <div class="nav-section__label">${mode ? caseModeLabel(mode) : "Workspace"}</div>
            <ul class="nav-list">${primaryViews(mode).map(link)}</ul>
          </div>
          <div class="nav-section">
            <button type="button" class="nav-section__label" id="btn-nav-more" aria-expanded="${moreOpen ? "true" : "false"}" aria-controls="nav-more-list">
              ${icon(moreOpen ? "chevron-down" : "chevron-right", { size: "sm" })}More · ${other}
            </button>
            <ul class="nav-list" id="nav-more-list" ${moreOpen ? "" : "hidden"}>${moreViews(mode).map(link)}</ul>
          </div>`
      : html`<div class="nav-section"><ul class="nav-list">${link("cases")}</ul></div>`
  );
  render($("sidebar-foot"), html`<ul class="nav-list">${caseId ? link("cases") : ""}${link("settings")}</ul>`);
  if (focusKey) ($("sidebar-nav").querySelector(focusKey) || $("sidebar-foot").querySelector(focusKey))?.focus({ preventScroll: true });
}

function syncSidebarState() {
  const collapsed = lsGet(STORAGE_KEYS.sidebar) === "collapsed";
  appEl.dataset.sidebar = collapsed ? "collapsed" : "expanded";
  const b = $("btn-collapse");
  b.setAttribute("aria-pressed", collapsed ? "true" : "false");
  b.setAttribute("aria-label", collapsed ? "Expand sidebar" : "Collapse sidebar");
  render(b, icon("sidebar"));
}

function toggleSidebar() {
  lsSet(STORAGE_KEYS.sidebar, lsGet(STORAGE_KEYS.sidebar) === "collapsed" ? null : "collapsed");
  syncSidebarState();
}

const mobileMq = window.matchMedia("(max-width: 767.98px)");
let releaseDrawerTrap = null;

/** Off-canvas sidebar (<768px): inert while closed, focus-trapped (main column inert) while open. */
function syncDrawerInert() {
  const open = appEl.dataset.drawer === "open";
  const mobile = mobileMq.matches;
  $("sidebar").inert = mobile && !open;
  appEl.querySelector(".app__main-col").inert = mobile && open;
}

function setDrawer(open) {
  const wasOpen = appEl.dataset.drawer === "open";
  appEl.dataset.drawer = open ? "open" : "closed";
  $("btn-drawer").setAttribute("aria-expanded", open ? "true" : "false");
  syncDrawerInert();
  if (open && !wasOpen && mobileMq.matches) {
    releaseDrawerTrap?.();
    releaseDrawerTrap = trapFocus($("sidebar"), { initial: ".case-switcher", restore: false });
  } else if (!open && wasOpen) {
    releaseDrawerTrap?.();
    releaseDrawerTrap = null;
    // return focus to the hamburger unless focus already moved somewhere visible (e.g. a route change)
    const ae = document.activeElement;
    if (mobileMq.matches && (!ae || ae === document.body || $("sidebar").contains(ae))) $("btn-drawer").focus();
  }
}

// =====================================================================================================
// Top bar: breadcrumbs, runtime status, export menu, theme, palette
// =====================================================================================================
function renderBreadcrumbs() {
  const route = router ? router.current() : null;
  const { case: c, caseId } = store.get();
  const crumbs = [];
  if (route && route.caseId) {
    crumbs.push({ label: c ? c.title : caseId, href: href("overview", { caseId }) });
    if (route.name !== "overview" || extraCrumbs.length) crumbs.push({ label: viewTitle(route.name), href: href(route.name, { caseId }), view: true });
  } else {
    crumbs.push({ label: "CypherSOL", href: "#/cases" });
    if (route && route.name) crumbs.push({ label: viewTitle(route.name), href: href(route.name), view: true });
  }
  crumbs.push(...extraCrumbs);
  // On narrow screens only one crumb fits: CSS shows the view crumb (crumb--view), not a selected entity.
  render(
    $("breadcrumbs"),
    html`<ol>${crumbs.map((cr, i) =>
      i === crumbs.length - 1
        ? html`<li class="${cr.view ? "crumb--view" : ""}" aria-current="page">${cr.label}</li>`
        : html`<li class="${cr.view ? "crumb--view" : ""}"><a href="${cr.href || "#"}">${cr.label}</a></li>`
    )}</ol>`
  );
  const viewLabel = route ? viewTitle(route.name) : "";
  const extraLabel = extraCrumbs.length ? extraCrumbs[extraCrumbs.length - 1].label : "";
  document.title = [viewLabel, extraLabel, c && route?.caseId ? c.title : "", "CypherSOL"]
    .filter(Boolean)
    .join(" · ");
}

function themeIcon() {
  const p = getThemePref();
  return p === "dark" ? "moon" : p === "light" ? "sun" : "monitor";
}

function renderTopbarActions() {
  render(
    $("topbar-actions"),
    html`<a class="runtime-chip" id="runtime-chip" href="#/settings"></a>
      <button type="button" class="btn btn--sm" id="btn-palette" aria-label="Open command palette" aria-keyshortcuts="Control+K Meta+K">
        ${icon("search", { size: "sm" })}<span class="btn__label">Search</span><kbd class="palette-hint">⌘K</kbd>
      </button>
      <button type="button" class="btn btn--sm" id="btn-export" aria-label="Export">
        ${icon("download", { size: "sm" })}<span class="btn__label">Export</span>${icon("chevron-down", { size: "sm" })}
      </button>
      <button type="button" class="btn btn--ghost btn--icon btn--sm" id="btn-theme"></button>`
  );
  render($("btn-drawer"), icon("menu"));
  $("btn-palette").addEventListener("click", () => openPalette());
  $("btn-theme").addEventListener("click", () => {
    const p = cycleTheme();
    toast(`Theme: ${p === "system" ? "match system" : p}`, { timeout: 1600 });
  });
  createMenu($("btn-export"), {
    label: "Export",
    items: () => {
      const { caseId, mode } = store.get();
      if (!caseId) return [{ id: "none", label: "Select a case to export", disabled: true }];
      return [
        { group: `Export · ${caseModeLabel(mode)}` },
        ...EXPORTS.filter((x) => x.modes.includes(mode)).map((x) => ({ id: x.id, label: x.label, description: x.desc, icon: "download", run: () => runExport(x.id) })),
        { separator: true },
        { id: "view_artifacts", label: "View Stored Files & Exports…", description: "Download raw documents or exported reports", icon: "folder", run: () => router.navigate("ingest", null, { caseId, tab: "artifacts" }) },
      ];
    },
  });
  syncThemeButton();
  renderRuntimeChip();
}

function syncThemeButton() {
  const b = $("btn-theme");
  if (!b) return;
  const p = getThemePref();
  const label = `Theme: ${p === "system" ? "match system" : p}. Click to change.`;
  b.setAttribute("aria-label", label);
  b.title = label;
  render(b, icon(themeIcon()));
}

function renderRuntimeChip() {
  const chip = $("runtime-chip");
  if (!chip) return;
  const { llmConfig, health } = store.get();
  const cfg = llmConfig || {};
  const egress = health ? Boolean(health.cloud_egress_possible) : Boolean(cfg.cloud_egress_possible);
  const mode = cfg.execution_mode || "heuristics";
  const tone = egress ? "warn" : "success";
  chip.title = egress
    ? `Runtime: ${execModeLabel(mode)} · cloud AI key active (${cfg.provider || "provider"}). Open settings.`
    : `Runtime: ${execModeLabel(mode)} · offline (no cloud egress). Open settings.`;
  render(chip, html`<span class="status-dot" data-tone="${tone}"></span><span class="runtime-chip__label">${execModeLabel(mode)}</span>${icon(egress ? "cloud" : "cpu", { size: "sm" })}`);
}

// =====================================================================================================
// Actions
// =====================================================================================================
async function switchCase(caseId) {
  if (!caseId) return;
  const route = router.current();
  const view = route && route.caseId && VIEWS[route.name]?.scope === "case" ? route.name : "overview";
  const targetMode = store.get().cases.find((c) => c.case_id === caseId)?.mode;
  // keep the same view when it exists in the target workspace, else land on overview
  const keep = targetMode && (primaryViews(targetMode).includes(view) || ["overview", "ingest", "transactions", "suspense", "anomalies"].includes(view));
  await router.navigate(keep ? view : "overview", null, { caseId });
}

async function runExport(id) {
  const { caseId } = store.get();
  const spec = EXPORTS.find((x) => x.id === id);
  if (!caseId || !spec) return;
  const dismiss = toast(`Preparing ${spec.label}…`, { timeout: 0 });
  try {
    const res = await caseApi(caseId).download(spec.sub, `${caseId}_${spec.id}.${spec.ext}`);
    dismiss();
    toast.success(`${res.filename} saved.`, { title: "Export ready" });
  } catch (err) {
    dismiss();
    toast.error(err, { title: "Export failed" });
  }
}

/** Palette / sidebar "New case…": the same workspace-card dialog as the Cases view (one implementation). */
async function openNewCaseDialog() {
  try {
    const mod = await import("./views/cases.js");
    (mod.styles || []).forEach(loadStyles);
    await mod.openCreateCaseDialog({
      store,
      defaultMode: store.get().mode || "cypher_trace",
      onCreated: (c) => router.navigate("ingest", null, { caseId: c.case_id }),
    });
  } catch (err) {
    toast.error(err, { title: "Could not open the new-case dialog" });
  }
}

function registerGlobalCommands() {
  const navCmds = Object.entries(VIEWS)
    .filter(([, v]) => v.scope !== "hidden")
    .map(([name, v]) => ({
      id: `nav.${name}`,
      title: `Go to ${v.title}`,
      group: "Navigate",
      icon: v.icon,
      keywords: [v.desc || "", name],
      when: () => v.scope === "global" || Boolean(store.get().caseId),
      run: () => router.navigate(name),
    }));
  registerCommands(navCmds, "global");
  registerCommands(
    [
      { id: "case.new", title: "New case…", group: "Cases", icon: "plus", keywords: ["create", "workspace"], run: openNewCaseDialog },
      { id: "case.refresh", title: "Refresh case data", group: "Cases", icon: "refresh", when: () => Boolean(store.get().caseId),
        run: async () => {
          await store.mutated(Object.values(TOPICS));
          toast("Case data refreshed", { timeout: 1500 });
        } },
      ...EXPORTS.map((x) => ({
        id: `export.${x.id}`,
        title: `Export ${x.label}`,
        group: "Export",
        icon: "download",
        keywords: [x.desc, x.ext],
        when: () => Boolean(store.get().caseId) && x.modes.includes(store.get().mode),
        run: () => runExport(x.id),
      })),
      { id: "theme.light", title: "Theme: light", group: "Appearance", icon: "sun", run: () => setThemePref("light") },
      { id: "theme.dark", title: "Theme: dark", group: "Appearance", icon: "moon", run: () => setThemePref("dark") },
      { id: "theme.system", title: "Theme: match system", group: "Appearance", icon: "monitor", run: () => setThemePref("system") },
      { id: "ui.sidebar", title: "Toggle sidebar", group: "Appearance", icon: "sidebar", hint: "Ctrl+\\", run: toggleSidebar },
    ],
    "global"
  );
  // one "Switch to …" command per case, refreshed when the case list changes
  let offCases = () => {};
  const syncCaseCmds = () => {
    offCases();
    offCases = registerCommands(
      store.get().cases.map((c) => ({
        id: `case.switch.${c.case_id}`,
        title: `Switch to ${c.title}`,
        group: "Cases",
        icon: c.mode === "cypher_edge" ? "receipt" : "network",
        keywords: [caseModeLabel(c.mode), c.agency_or_firm_name || "", c.case_id],
        when: () => store.get().caseId !== c.case_id,
        run: () => switchCase(c.case_id),
      })),
      "cases"
    );
  };
  syncCaseCmds();
  store.select((s) => s.cases, syncCaseCmds);
}

// =====================================================================================================
// Router
// =====================================================================================================
/** ctx.toast: like toast(), but a toast with an action (Undo…) is dismissed when its view unmounts, since
 * actionable toasts persist until dismissed and their action belongs to that view. */
function viewToast(ctx) {
  const t = (msg, opts = {}) => {
    const dismiss = toast(msg, opts);
    if (opts && opts.action) ctx.onCleanup(dismiss);
    return dismiss;
  };
  t.info = (msg, opts) => t(msg, { ...opts, type: "info" });
  t.success = (msg, opts) => t(msg, { ...opts, type: "success" });
  t.warn = (msg, opts) => t(msg, { ...opts, type: "warn" });
  t.error = toast.error;
  return t;
}
function createAppRouter() {
  return createRouter({
    outlet: main,
    renderLoading: (el) => render(el, viewSkeleton()),
    renderError: (el, err, route, retry) => {
      const notFound = err && err.status === 404;
      render(
        el,
        errorState(err, {
          title: notFound && route.caseId ? "Case not found" : "Could not open this view",
          retryId: notFound ? "" : "retry",
        })
      );
      if (notFound) {
        const p = document.createElement("p");
        p.className = "small";
        render(p, html`<a href="#/cases">Back to all cases</a>`);
        el.querySelector(".empty")?.appendChild(p);
      }
      el.querySelector('[data-action="retry"]')?.addEventListener("click", retry);
    },
    onMounted: (route, el, { first }) => {
      announce(`${document.title.split(" · ")[0] || viewTitle(route.name)} loaded`);
      if (first) return;
      // move focus to the new view's heading unless the view (or a dialog) already took it
      const ae = document.activeElement;
      const lost = !ae || ae === document.body || !ae.isConnected || $("sidebar").contains(ae) || $("topbar").contains(ae);
      if (!lost || document.querySelector("dialog[open]")) return;
      const target = el.querySelector("h1.page-title") || main;
      if (target !== main && !target.hasAttribute("tabindex")) target.setAttribute("tabindex", "-1");
      target.focus({ preventScroll: true });
    },
    onRoute: () => {
      extraCrumbs = [];
      setDrawer(false);
      renderNav();
      renderBreadcrumbs();
    },
    extendCtx: (ctx) =>
      Object.assign(ctx, {
        toast: viewToast(ctx),
        withBusy,
        /** Extra breadcrumb trail after the view title: [{ label, href? }]. */
        setCrumbs(crumbs) {
          if (!ctx.isCurrent()) return;
          extraCrumbs = (crumbs || []).filter((c) => c && c.label);
          renderBreadcrumbs();
        },
      }),
  });
}

// =====================================================================================================
// Boot
// =====================================================================================================
async function boot() {
  initPalette();
  syncSidebarState();
  renderTopbarActions();
  renderCaseSwitcher();

  $("btn-collapse").addEventListener("click", toggleSidebar);
  $("btn-drawer").addEventListener("click", () => setDrawer(appEl.dataset.drawer !== "open"));
  $("sidebar-scrim").addEventListener("click", () => setDrawer(false));
  mobileMq.addEventListener("change", () => {
    if (!mobileMq.matches && appEl.dataset.drawer === "open") setDrawer(false);
    syncDrawerInert();
  });
  syncDrawerInert();
  delegate($("sidebar-nav"), "click", "#btn-nav-more", (e, btn) => {
    const open = btn.getAttribute("aria-expanded") !== "true";
    lsSet(STORAGE_KEYS.navMore, open ? "1" : "0");
    renderNav();
    $("btn-nav-more")?.focus();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && appEl.dataset.drawer === "open") setDrawer(false);
    // Ctrl/Cmd+\\ toggles the sidebar (a modifier is required: WCAG 2.1.4 character key shortcuts)
    if (e.key === "\\" && (e.metaKey || e.ctrlKey) && !e.altKey && !e.shiftKey) {
      e.preventDefault();
      toggleSidebar();
    }
  });
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") store.loadHealth().catch(() => {});
  });

  store.subscribe((s, changed) => {
    if (changed.some((k) => ["case", "caseId", "cases", "caseStatus"].includes(k))) renderCaseSwitcher();
    if (changed.some((k) => ["caseId", "mode", "summary", "case"].includes(k))) renderNav();
    if (changed.some((k) => ["case", "caseId"].includes(k))) renderBreadcrumbs();
    if (changed.some((k) => ["health", "llmConfig"].includes(k))) {
      updateCloudBanner();
      renderRuntimeChip();
    }
  });
  onThemeChange(syncThemeButton);

  if (!API_TOKEN || API_TOKEN === "__CYPHERSOL_TOKEN__") {
    toast.warn("This page was not served by the CypherSOL server, so API calls will be rejected.", { timeout: 0 });
  }

  const [meta, cases] = await Promise.allSettled([store.loadMeta(), store.loadCases(), store.loadHealth(), store.loadLlmConfig()]);
  if (meta.status === "rejected") {
    render(main, errorState(meta.reason, { title: "CypherSOL could not start", retryId: "reload" }));
    main.querySelector('[data-action="reload"]')?.addEventListener("click", () => window.location.reload());
    return;
  }
  if (cases.status === "rejected") toast.error(cases.reason, { title: "Could not load cases" });
  updateCloudBanner();

  // first visit: restore the last case if no route given
  if (!window.location.hash || window.location.hash === "#" || window.location.hash === "#/") {
    const last = lsGet(STORAGE_KEYS.lastCase);
    const list = store.get().cases;
    const target = list.find((c) => c.case_id === last) || list[0];
    history.replaceState(null, "", target ? href("overview", { caseId: target.case_id }) : "#/cases");
  }

  router = createAppRouter();
  window.addEventListener("cyphersol:new-case", openNewCaseDialog);
  registerGlobalCommands();
  await router.start();
}

boot().catch((err) => {
  console.error(err);
  render(main, errorState(err, { title: "CypherSOL could not start", retryId: "" }));
});
