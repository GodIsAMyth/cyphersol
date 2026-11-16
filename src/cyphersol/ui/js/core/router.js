/* Hash router with lazy view modules.
 *
 * URLs:  #/cases                      -> views/cases.js
 *        #/settings                   -> views/settings.js
 *        #/case/{caseId}              -> views/overview.js
 *        #/case/{caseId}/{view}?k=v   -> views/{view}.js with ctx.params = { k: "v" }
 *
 * View module contract (views/<name>.js):
 *   export async function mount(el, ctx) { ...; return cleanup }   // cleanup: fn | { unmount?, update? } | void
 *   export const styles = ["views/<name>.css"];                     // optional, relative to ui/styles/
 * `update(params)` (optional) is called instead of a remount when only the query string changed via
 * back/forward or a link; without it the view is remounted. ctx.setParams() never triggers either.
 */
import { caseApi, isAbort } from "./api.js";
import { registerCommands } from "./commands.js";
import { closeDrawer } from "../components/drawer.js";
import { isView, VIEWS, viewTitle } from "./nav.js";
import { store } from "./store.js";

const enc = encodeURIComponent;

/** Parse a location hash into { name, caseId, params, hash }. name is null for "#/" / unknown. */
export function parseHash(hash = window.location.hash) {
  const h = String(hash || "").replace(/^#/, "");
  const [pathPart, query = ""] = h.split("?");
  const segs = pathPart.split("/").filter(Boolean).map((s) => {
    try {
      return decodeURIComponent(s);
    } catch (_e) {
      return s;
    }
  });
  const params = Object.fromEntries(new URLSearchParams(query));
  let name = null;
  let caseId = null;
  if (segs[0] === "case" && segs[1]) {
    caseId = segs[1];
    name = segs[2] || "overview";
  } else if (segs[0] && VIEWS[segs[0]]?.scope === "global") {
    name = segs[0];
  }
  if (name && !isView(name)) name = "notfound";
  if (name && VIEWS[name]?.scope === "case" && !caseId) name = "notfound";
  return { name, caseId, params, hash: `#${h}` };
}

/** Build a hash. href("transactions", { caseId, params: { q: "x" } }) -> "#/case/ID/transactions?q=x". */
export function href(name, { caseId = null, params = null } = {}) {
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params || {})) {
    if (v === null || v === undefined || v === "") continue;
    sp.set(k, String(v));
  }
  const q = sp.toString() ? `?${sp.toString()}` : "";
  if (VIEWS[name]?.scope === "global") return `#/${name}${q}`;
  const cid = caseId || store.get().caseId;
  if (!cid) return "#/cases";
  return name === "overview" && !q ? `#/case/${enc(cid)}` : `#/case/${enc(cid)}/${enc(name)}${q}`;
}

/**
 * createRouter({ outlet, renderLoading(el, route), renderError(el, err, route, retry), onRoute(route),
 *                onMounted(route, el, { first }), extendCtx(ctx) }) -> router
 * onMounted runs after a view finished mounting (announce the route, move focus to its heading).
 * router: { start(), navigate(name, params?, opts?), setParams(patch, opts?), current(), reload(), href }
 */
export function createRouter({ outlet, renderLoading, renderError, onRoute, onMounted, extendCtx }) {
  let gen = 0;
  let mounts = 0;
  let current = null; // { route, unmount, update, controller, cleanups, el }
  let lastHash = null;

  function teardown() {
    if (!current) return;
    const c = current;
    current = null;
    closeDrawer(); // inspector/evidence drawers belong to the view (and its case); never let one outlive it
    c.controller.abort();
    for (const fn of c.cleanups.splice(0).reverse()) {
      try {
        fn();
      } catch (err) {
        console.error(err);
      }
    }
    try {
      c.unmount?.();
    } catch (err) {
      console.error(err);
    }
  }

  function sameView(a, b) {
    return a && b && a.name === b.name && a.caseId === b.caseId;
  }

  async function handle({ force = false } = {}) {
    const hash = window.location.hash;
    if (!force && hash === lastHash) return;
    lastHash = hash;
    let route = parseHash(hash);

    if (!route.name) {
      const s = store.get();
      const target = s.caseId || (s.cases[0] && s.cases[0].case_id);
      replaceHash(target ? href("overview", { caseId: target }) : "#/cases");
      return handle({ force: true });
    }

    // query-only change on the same view: let the view update in place when it can
    if (!force && current && sameView(current.route, route)) {
      current.route = route;
      current.ctx.params = { ...route.params };
      if (typeof current.update === "function") {
        try {
          current.update({ ...route.params });
        } catch (err) {
          console.error(err);
        }
        onRoute?.(route);
        return;
      }
    }

    teardown();
    gen += 1;
    const myGen = gen;
    const controller = new AbortController();
    const el = document.createElement("div");
    el.className = `view-root v-${route.name}`;
    el.dataset.view = route.name;
    outlet.replaceChildren(el);
    const cleanups = [];
    const entry = { route, controller, cleanups, el, unmount: null, update: null, ctx: null, owner: `view:${route.name}:${myGen}` };
    current = entry;
    onRoute?.(route);
    renderLoading?.(el, route);

    const isCurrent = () => myGen === gen && !controller.signal.aborted;

    try {
      if (route.caseId) {
        try {
          await store.setCase(route.caseId);
        } catch (err) {
          // a newer refresh of the same case may have aborted this load: wait for that one instead
          if (!isAbort(err) || !isCurrent() || store.get().caseId !== route.caseId) throw err;
          await store.ensureCaseData();
        }
        if (!isCurrent()) return;
      }
      const mod = await import(`../views/${route.name}.js`);
      if (!isCurrent()) return;
      if (Array.isArray(mod.styles)) mod.styles.forEach(loadStyles);
      el.replaceChildren();
      const ctx = makeCtx(route, entry, isCurrent);
      entry.ctx = ctx;
      const result = await mod.mount(el, ctx);
      if (!isCurrent()) {
        // navigated away while mounting: run the cleanup we just received
        const fn = typeof result === "function" ? result : result?.unmount;
        try {
          fn?.();
        } catch (err) {
          console.error(err);
        }
        return;
      }
      if (typeof result === "function") entry.unmount = result;
      else if (result && typeof result === "object") {
        entry.unmount = typeof result.unmount === "function" ? result.unmount : null;
        entry.update = typeof result.update === "function" ? result.update : null;
      }
      mounts += 1;
      try {
        onMounted?.(route, el, { first: mounts === 1 });
      } catch (err) {
        console.error(err);
      }
    } catch (err) {
      if (isAbort(err) || !isCurrent()) return;
      console.error(err);
      renderError?.(el, err, route, () => handle({ force: true }));
    }
  }

  function makeCtx(route, entry, isCurrent) {
    const s = store.get();
    const ctx = {
      view: route.name,
      title: viewTitle(route.name),
      caseId: route.caseId,
      case: route.caseId ? s.case : null,
      mode: route.caseId ? s.mode : null,
      params: { ...route.params },
      signal: entry.controller.signal,
      isCurrent,
      store,
      api: route.caseId ? caseApi(route.caseId, entry.controller.signal) : null,
      href: (name, params = null, opts = {}) => href(name, { caseId: opts.caseId || route.caseId, params }),
      navigate: (name, params = null, opts = {}) => router.navigate(name, params, { caseId: route.caseId, ...opts }),
      setParams: (patch, opts) => router.setParams(patch, opts),
      registerCommands: (cmds) => {
        const off = registerCommands(cmds, entry.owner);
        entry.cleanups.push(off);
        return off;
      },
      onCleanup: (fn) => entry.cleanups.push(fn),
      onData: (topics, fn) => {
        const off = store.onTopics(topics, (t) => {
          if (isCurrent()) fn(t);
        });
        entry.cleanups.push(off);
        return off;
      },
      mutated: (topics, opts) => store.mutated(topics, { caseId: route.caseId, ...opts }),
    };
    return extendCtx ? extendCtx(ctx, entry) || ctx : ctx;
  }

  function replaceHash(h) {
    history.replaceState(history.state, "", h);
  }

  const router = {
    href,
    current: () => (current ? { ...current.route, params: { ...current.route.params } } : null),
    start() {
      window.addEventListener("hashchange", () => handle());
      window.addEventListener("popstate", () => handle());
      return handle({ force: true });
    },
    /** Navigate to a view (pushes history). opts: { caseId, replace } */
    navigate(name, params = null, { caseId = null, replace = false } = {}) {
      const h = href(name, { caseId, params });
      if (replace) replaceHash(h);
      else if (window.location.hash !== h) history.pushState(null, "", h);
      return handle({ force: true });
    },
    /**
     * Merge params into the current URL WITHOUT remounting (filters, selected tab, selected entity…).
     * null/"" removes a key. opts: { push = false } — push adds a history entry (Back restores it).
     */
    setParams(patch, { push = false } = {}) {
      if (!current) return;
      const params = { ...current.route.params };
      for (const [k, v] of Object.entries(patch || {})) {
        if (v === null || v === undefined || v === "") delete params[k];
        else params[k] = String(v);
      }
      const h = href(current.route.name, { caseId: current.route.caseId, params });
      if (h === window.location.hash) return;
      if (push) history.pushState(null, "", h);
      else replaceHash(h);
      lastHash = window.location.hash;
      current.route = { ...current.route, params, hash: lastHash };
      if (current.ctx) current.ctx.params = { ...params };
      onRoute?.(current.route);
    },
    /** Remount the current view (e.g. after a case switch from the sidebar). */
    reload: () => handle({ force: true }),
  };
  return router;
}

const loadedStyles = new Set();
/** Inject a view stylesheet once (path relative to ui/styles/, e.g. "views/network.css"). */
export function loadStyles(path) {
  if (!path || loadedStyles.has(path) || /^[a-z]+:|^\/\/|\.\./i.test(path)) return;
  loadedStyles.add(path);
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = `styles/${path}`;
  document.head.appendChild(link);
}
