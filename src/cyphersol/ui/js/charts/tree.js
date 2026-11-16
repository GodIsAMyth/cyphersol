/* Tidy tree chart (Reingold–Tilford style, contour-based) laid out left → right and drawn as hand-written SVG with
 * rounded elbow connectors. Domain-agnostic: the caller supplies the node card markup.
 *
 *   const chart = createTreeChart(frameEl, {
 *     ariaLabel: "Layering tree",
 *     nodeW: 232, nodeH: 74, levelGap: 64, siblingGap: 14,
 *     keyOf: (node, parentKey) => string,          // stable key (survives refetches)
 *     childrenOf: (node) => node.children,
 *     renderNode: (node, info) => SafeHTML,        // inner SVG of the node <g>, origin = card top-left
 *     nodeLabel: (node, info) => string,           // accessible name of the treeitem
 *     edgeWidth: (node) => number, edgeClass: (node) => string,
 *     initialCollapsed: (node, depth) => boolean,  // applied once per key the chart has not seen before
 *     onSelect: (node, key, info) => void,
 *     canLoad: (node) => boolean,                  // lazy drill-down: node has unloaded children on the server
 *     onLoad: async (node, key, info) => boolean,  // fetch + attach node.children; chart redraws when it settles
 *   });
 *   chart.setRoot(root); chart.select(key); chart.expandAll(); chart.collapseToDepth(2); chart.zoom(1.2); chart.fit();
 *   chart.exportSvg({ title }) -> string ; chart.destroy()
 *
 * Accessibility: the <svg> is role="tree"; each node is a role="treeitem" with aria-level/-setsize/-posinset/
 * -expanded/-selected and a roving tabindex. Keys: ↑/↓ previous/next visible node, → expand / first child,
 * ← collapse / parent, Home/End, Enter/Space select, +/−/0 zoom. A lazy node (canLoad) shows a "+ more" toggle; → or
 * a click on it calls onLoad and the node is marked aria-busy until the promise settles. Pan by dragging the background; Ctrl/⌘+wheel zooms.
 * Also exports inlineSvgForExport() / downloadText() used by the loop diagram. */
import { html } from "../core/dom.js";
import { downloadText } from "../core/files.js";

const SVG_NS = "http://www.w3.org/2000/svg";

/* ------------------------------------------------------------------------------------------------ layout */

/**
 * Contour-based tidy layout. Breadth axis = y (cards stacked vertically), depth axis = x.
 * Returns { items: [{ node, key, parentKey, depth, x, y, kids: [item], index }], byKey: Map, width, height }.
 */
export function tidyLayout(root, { keyOf, childrenOf, nodeW, nodeH, levelGap, siblingGap, pad = 16, isCollapsed = () => false, canLoad = () => false }) {
  const items = [];
  const byKey = new Map();

  function build(node, depth, parent) {
    const key = keyOf(node, parent ? parent.key : null);
    const item = { node, key, parentKey: parent ? parent.key : null, parent, depth, x: 0, y: 0, dy: 0, off: 0, kids: [], all: 0 };
    byKey.set(key, item);
    const ch = childrenOf(node) || [];
    item.all = ch.length;
    item.lazy = ch.length === 0 && Boolean(canLoad(node));
    item.collapsed = ch.length > 0 && isCollapsed(key, node, depth);
    if (!item.collapsed) item.kids = ch.map((c) => build(c, depth + 1, item));
    return item;
  }

  // returns contour: array per relative level of [top, bottom] relative to this item's top (=0)
  function lay(item) {
    if (!item.kids.length) return [[0, nodeH]];
    let acc = null;
    for (const c of item.kids) {
      const cc = lay(c);
      if (!acc) {
        c.off = 0;
        acc = cc.map(([a, b]) => [a, b]);
        continue;
      }
      let off = -Infinity;
      const shared = Math.min(acc.length, cc.length);
      for (let l = 0; l < shared; l += 1) off = Math.max(off, acc[l][1] + siblingGap - cc[l][0]);
      c.off = off;
      for (let l = 0; l < cc.length; l += 1) {
        if (l < acc.length) acc[l] = [Math.min(acc[l][0], cc[l][0] + off), Math.max(acc[l][1], cc[l][1] + off)];
        else acc.push([cc[l][0] + off, cc[l][1] + off]);
      }
    }
    const first = item.kids[0].off;
    const last = item.kids[item.kids.length - 1].off;
    const mid = (first + last) / 2;
    for (const c of item.kids) c.dy = c.off - mid;
    return [[0, nodeH], ...acc.map(([a, b]) => [a - mid, b - mid])];
  }

  const rootItem = build(root, 0, null);
  lay(rootItem);

  let minY = Infinity;
  let maxY = -Infinity;
  let maxDepth = 0;
  (function place(item, y) {
    item.y = y;
    item.x = item.depth * (nodeW + levelGap);
    item.index = items.length;
    items.push(item);
    minY = Math.min(minY, y);
    maxY = Math.max(maxY, y + nodeH);
    maxDepth = Math.max(maxDepth, item.depth);
    for (const c of item.kids) place(c, y + c.dy);
  })(rootItem, 0);

  const shift = pad - minY;
  for (const it of items) {
    it.y += shift;
    it.x += pad;
  }
  return {
    items,
    byKey,
    root: rootItem,
    width: Math.ceil(maxDepth * (nodeW + levelGap) + nodeW + pad * 2 + 24),
    height: Math.ceil(maxY - minY + pad * 2),
  };
}

/** Rounded elbow connector from a parent card's right edge to a child card's left edge. */
export function elbowPath(x0, y0, x1, y1, radius = 7) {
  const mx = x0 + (x1 - x0) / 2;
  const dy = y1 - y0;
  if (Math.abs(dy) < 0.5) return `M${x0} ${y0}H${x1}`;
  const s = Math.sign(dy);
  const r = Math.min(radius, Math.abs(dy) / 2, (x1 - x0) / 2);
  return `M${x0} ${y0}H${mx - r}Q${mx} ${y0} ${mx} ${y0 + s * r}V${y1 - s * r}Q${mx} ${y1} ${mx + r} ${y1}H${x1}`;
}

/* ------------------------------------------------------------------------------------------------ export */

const EXPORT_PROPS = [
  "fill", "fill-opacity", "stroke", "stroke-width", "stroke-dasharray", "stroke-linecap", "stroke-linejoin",
  "stroke-opacity", "opacity", "font-family", "font-size", "font-weight", "letter-spacing", "text-anchor",
  "dominant-baseline", "paint-order",
];

/**
 * Serialise an on-page SVG into a standalone file: resolves every CSS class/token to concrete presentation
 * attributes (so the file renders identically outside the app, in the current theme), strips interactive
 * attributes and adds a background + <title>.
 */
export function inlineSvgForExport(svg, { title = "", width, height, background } = {}) {
  const clone = svg.cloneNode(true);
  const src = [svg, ...svg.querySelectorAll("*")];
  const dst = [clone, ...clone.querySelectorAll("*")];
  src.forEach((s, i) => {
    const d = dst[i];
    if (!d) return;
    const cs = getComputedStyle(s);
    if (s !== svg && (cs.display === "none" || cs.visibility === "hidden")) {
      d.setAttribute("display", "none");
    } else {
      for (const p of EXPORT_PROPS) {
        const v = cs.getPropertyValue(p);
        if (v && v !== "normal" && v !== "auto") d.setAttribute(p, v.trim());
      }
    }
    for (const a of ["class", "tabindex", "role", "aria-level", "aria-setsize", "aria-posinset", "aria-expanded",
      "aria-selected", "aria-label", "aria-hidden", "focusable", "style"]) d.removeAttribute(a);
    for (const a of Array.from(d.attributes)) if (a.name.startsWith("data-")) d.removeAttribute(a.name);
  });
  const vb = svg.viewBox && svg.viewBox.baseVal;
  const w = width || (vb && vb.width) || svg.clientWidth;
  const h = height || (vb && vb.height) || svg.clientHeight;
  clone.setAttribute("xmlns", SVG_NS);
  clone.setAttribute("width", String(w));
  clone.setAttribute("height", String(h));
  clone.removeAttribute("fill");
  clone.removeAttribute("stroke");
  const bg = document.createElementNS(SVG_NS, "rect");
  bg.setAttribute("x", vb ? String(vb.x) : "0");
  bg.setAttribute("y", vb ? String(vb.y) : "0");
  bg.setAttribute("width", String(w));
  bg.setAttribute("height", String(h));
  bg.setAttribute("fill", background || getComputedStyle(document.documentElement).getPropertyValue("--surface-1").trim() || "white");
  clone.insertBefore(bg, clone.firstChild);
  if (title) {
    const t = document.createElementNS(SVG_NS, "title");
    t.textContent = title;
    clone.insertBefore(t, clone.firstChild);
  }
  return `<?xml version="1.0" encoding="UTF-8"?>\n${new XMLSerializer().serializeToString(clone)}`;
}

/** Kept for existing imports; the implementation lives in core/files.js. */
export { downloadText };

/* ------------------------------------------------------------------------------------------------ chart */

const ZOOM_MIN = 0.3;
const ZOOM_MAX = 2;

export function createTreeChart(frame, opts) {
  const o = {
    ariaLabel: "Tree",
    nodeW: 232,
    nodeH: 74,
    levelGap: 64,
    siblingGap: 14,
    pad: 18,
    keyOf: (n, pk) => (pk ? `${pk}>${n.id}` : String(n.id)),
    childrenOf: (n) => n.children || [],
    renderNode: () => "",
    nodeLabel: () => "",
    edgeWidth: () => 1.5,
    edgeClass: () => "",
    nodeClass: () => "",
    initialCollapsed: () => false,
    canLoad: () => false,
    onLoad: null,
    onSelect: null,
    onZoom: null,
    ...opts,
  };

  frame.classList.add("v-layering-tree-frame");
  frame.replaceChildren();
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("class", "v-layering-tree-svg");
  svg.setAttribute("role", "tree");
  svg.setAttribute("aria-label", o.ariaLabel);
  frame.appendChild(svg);

  let root = null;
  let layout = null;
  let selectedKey = null;
  let focusKey = null;
  let scale = 1;
  const collapsed = new Set();
  const known = new Set();
  const loading = new Set();
  const offs = [];

  const info = (it) => ({
    key: it.key,
    depth: it.depth,
    collapsed: it.collapsed,
    childCount: it.all,
    lazy: Boolean(it.lazy),
    loading: loading.has(it.key),
    hiddenCount: it.collapsed ? countDescendants(it.node) : 0,
    selected: it.key === selectedKey,
    onPath: isOnSelectedPath(it.key),
    parentKey: it.parentKey,
  });

  function countDescendants(n) {
    let c = 0;
    const stack = [...(o.childrenOf(n) || [])];
    while (stack.length) {
      const x = stack.pop();
      c += 1;
      stack.push(...(o.childrenOf(x) || []));
    }
    return c;
  }

  function pathKeys(key) {
    const out = new Set();
    let it = layout && layout.byKey.get(key);
    while (it) {
      out.add(it.key);
      it = it.parent;
    }
    return out;
  }
  let pathSet = new Set();
  const isOnSelectedPath = (key) => pathSet.has(key);

  /** Walk the full data tree (not just visible) so initialCollapsed applies to unseen keys. */
  function seedCollapse() {
    (function walk(n, pk, depth) {
      const key = o.keyOf(n, pk);
      const ch = o.childrenOf(n) || [];
      if (!known.has(key)) {
        known.add(key);
        if (ch.length && o.initialCollapsed(n, depth)) collapsed.add(key);
      }
      ch.forEach((c) => walk(c, key, depth + 1));
    })(root, null, 0);
  }

  function draw() {
    if (!root) {
      svg.replaceChildren();
      return;
    }
    const hadFocus = svg.contains(document.activeElement);
    layout = tidyLayout(root, {
      keyOf: o.keyOf,
      childrenOf: o.childrenOf,
      nodeW: o.nodeW,
      nodeH: o.nodeH,
      levelGap: o.levelGap,
      siblingGap: o.siblingGap,
      pad: o.pad,
      isCollapsed: (key) => collapsed.has(key),
      canLoad: (n) => Boolean(o.onLoad) && o.canLoad(n),
    });
    if (!selectedKey || !layout.byKey.has(selectedKey)) selectedKey = layout.root.key;
    if (!focusKey || !layout.byKey.has(focusKey)) focusKey = selectedKey;
    pathSet = pathKeys(selectedKey);

    const { nodeW: W, nodeH: H } = o;
    const edges = [];
    const nodes = [];
    for (const it of layout.items) {
      if (it.parent) {
        const p = it.parent;
        const cls = `v-layering-tree-edge ${o.edgeClass(it.node) || ""} ${pathSet.has(it.key) ? "is-path" : ""}`;
        edges.push(html`<path class="${cls}" data-edge="${it.key}" d="${elbowPath(p.x + W, p.y + H / 2, it.x, it.y + H / 2)}" stroke-width="${Number(o.edgeWidth(it.node) || 1.5).toFixed(2)}"></path>`);
      }
      const inf = info(it);
      const siblings = it.parent ? it.parent.kids : [it];
      const pos = siblings.indexOf(it) + 1;
      const cls = `v-layering-tree-node ${o.nodeClass(it.node, inf) || ""} ${inf.selected ? "is-selected" : ""} ${inf.onPath ? "is-on-path" : ""}`;
      const toggle = it.all
        ? html`<g class="v-layering-tree-toggle" data-tree-toggle="1" transform="translate(${W} ${H / 2})">
            <circle r="9"></circle>
            <path d="${it.collapsed ? "M-4 0H4M0 -4V4" : "M-4 0H4"}"></path>
            ${it.collapsed ? html`<text x="14" y="4" class="v-layering-tree-hidden">+${inf.hiddenCount}</text>` : ""}
          </g>`
        : it.lazy
          ? html`<g class="v-layering-tree-toggle is-lazy ${inf.loading ? "is-loading" : ""}" data-tree-toggle="1" transform="translate(${W} ${H / 2})">
              <title>${inf.loading ? "Loading the next hop…" : "Load the next hop"}</title>
              <circle r="9"></circle>
              ${inf.loading ? html`<circle class="v-layering-tree-spin" r="5"></circle>` : html`<path d="M-4 0H4M0 -4V4"></path>`}
              <text x="14" y="4" class="v-layering-tree-hidden">${inf.loading ? "loading…" : "more"}</text>
            </g>`
          : "";
      nodes.push(html`<g class="${cls}" data-key="${it.key}" transform="translate(${it.x} ${it.y})" role="treeitem"
        aria-level="${it.depth + 1}" aria-setsize="${siblings.length}" aria-posinset="${pos}"
        ${it.all ? html`aria-expanded="${it.collapsed ? "false" : "true"}"` : it.lazy ? html`aria-expanded="false"` : ""}
        ${inf.loading ? html`aria-busy="true"` : ""}
        aria-selected="${inf.selected ? "true" : "false"}" tabindex="${it.key === focusKey ? "0" : "-1"}"
        aria-label="${o.nodeLabel(it.node, inf)}">
        <rect class="v-layering-tree-focus" x="-4" y="-4" width="${W + 8}" height="${H + 8}" rx="11"></rect>
        ${o.renderNode(it.node, inf)}${toggle}</g>`);
    }
    svg.setAttribute("viewBox", `0 0 ${layout.width} ${layout.height}`);
    svg.innerHTML = String(html`<g class="v-layering-tree-edges" aria-hidden="true">${edges}</g><g class="v-layering-tree-nodes">${nodes}</g>`);
    applyScale();
    if (hadFocus) nodeEl(focusKey)?.focus({ preventScroll: true });
  }

  const nodeEl = (key) => (key ? svg.querySelector(`g[data-key="${CSS.escape(key)}"]`) : null);

  function applyScale() {
    if (!layout) return;
    svg.setAttribute("width", String(Math.round(layout.width * scale)));
    svg.setAttribute("height", String(Math.round(layout.height * scale)));
    o.onZoom?.(scale);
  }

  /** Cheap selection update (no relayout): classes, aria-selected and the roving tabindex. */
  function restyle() {
    pathSet = pathKeys(selectedKey);
    for (const g of svg.querySelectorAll("g[data-key]")) {
      const k = g.dataset.key;
      const sel = k === selectedKey;
      g.classList.toggle("is-selected", sel);
      g.classList.toggle("is-on-path", pathSet.has(k));
      g.setAttribute("aria-selected", sel ? "true" : "false");
      g.setAttribute("tabindex", k === focusKey ? "0" : "-1");
    }
    for (const p of svg.querySelectorAll("path[data-edge]")) p.classList.toggle("is-path", pathSet.has(p.dataset.edge));
  }

  function zoomTo(next, anchor = null) {
    const k = Math.min(ZOOM_MAX, Math.max(ZOOM_MIN, next));
    if (!layout || k === scale) return;
    const ax = anchor ? anchor.x : frame.clientWidth / 2;
    const ay = anchor ? anchor.y : frame.clientHeight / 2;
    const cx = (frame.scrollLeft + ax) / scale;
    const cy = (frame.scrollTop + ay) / scale;
    scale = k;
    applyScale();
    frame.scrollLeft = cx * scale - ax;
    frame.scrollTop = cy * scale - ay;
  }

  function scrollIntoViewKey(key) {
    const it = layout && layout.byKey.get(key);
    if (!it) return;
    const x0 = it.x * scale;
    const y0 = it.y * scale;
    const x1 = (it.x + o.nodeW + 40) * scale;
    const y1 = (it.y + o.nodeH) * scale;
    const m = 24;
    if (x0 < frame.scrollLeft + m) frame.scrollLeft = Math.max(0, x0 - m);
    else if (x1 > frame.scrollLeft + frame.clientWidth - m) frame.scrollLeft = x1 - frame.clientWidth + m;
    if (y0 < frame.scrollTop + m) frame.scrollTop = Math.max(0, y0 - m);
    else if (y1 > frame.scrollTop + frame.clientHeight - m) frame.scrollTop = y1 - frame.clientHeight + m;
  }

  function select(key, { focus = false, scroll = true, notify = true } = {}) {
    if (!layout) return;
    if (!layout.byKey.has(key)) {
      // reveal: expand every collapsed ancestor present in the data
      if (!revealKey(key)) return;
    }
    selectedKey = key;
    focusKey = key;
    restyle();
    if (scroll) scrollIntoViewKey(key);
    if (focus) nodeEl(key)?.focus({ preventScroll: true });
    const it = layout.byKey.get(key);
    if (notify && it) o.onSelect?.(it.node, key, info(it));
  }

  /** Expand all ancestors of a key that exists in the data but is hidden. */
  function revealKey(key) {
    let found = null;
    (function walk(n, pk, chain) {
      if (found) return;
      const k = o.keyOf(n, pk);
      if (k === key) {
        found = chain;
        return;
      }
      for (const c of o.childrenOf(n) || []) walk(c, k, [...chain, k]);
    })(root, null, []);
    if (!found) return false;
    found.forEach((k) => collapsed.delete(k));
    draw();
    return layout.byKey.has(key);
  }

  /** Lazy drill-down: ask the caller for the children of a node the server has not sent yet. */
  function loadKey(key) {
    const it = layout && layout.byKey.get(key);
    if (!it || !it.lazy || loading.has(key) || !o.onLoad) return Promise.resolve(false);
    loading.add(key);
    focusKey = key;
    draw();
    return Promise.resolve()
      .then(() => o.onLoad(it.node, key, info(it)))
      .catch(() => false)
      .then((ok) => {
        loading.delete(key);
        if (!root) return false;
        collapsed.delete(key);
        seedCollapse();
        draw();
        return Boolean(ok);
      });
  }

  function toggle(key, force) {
    const it = layout && layout.byKey.get(key);
    if (it && it.lazy && force !== true) {
      loadKey(key);
      return;
    }
    if (!it || !it.all) return;
    const willCollapse = force === undefined ? !collapsed.has(key) : force;
    if (willCollapse) collapsed.add(key);
    else collapsed.delete(key);
    if (willCollapse && selectedKey !== key && pathKeys(selectedKey).has(key)) selectedKey = key;
    focusKey = key;
    draw();
    if (willCollapse && it.key === selectedKey) {
      const n = layout.byKey.get(key);
      if (n) o.onSelect?.(n.node, key, info(n));
    }
  }

  function visibleOrder() {
    return layout ? layout.items : [];
  }

  // ---------------------------------------------------------------- events
  offs.push(listen(svg, "click", (e) => {
    const g = e.target.closest && e.target.closest("g[data-key]");
    if (!g) return;
    const key = g.dataset.key;
    if (e.target.closest("[data-tree-toggle]")) {
      toggle(key);
      nodeEl(key)?.focus({ preventScroll: true });
      return;
    }
    select(key, { focus: true, scroll: false });
  }));

  offs.push(listen(svg, "keydown", (e) => {
    const g = e.target.closest && e.target.closest("g[data-key]");
    if (!g || !layout) return;
    const key = g.dataset.key;
    const it = layout.byKey.get(key);
    const order = visibleOrder();
    const idx = order.indexOf(it);
    let target = null;
    switch (e.key) {
      case "ArrowDown":
        target = order[Math.min(order.length - 1, idx + 1)];
        break;
      case "ArrowUp":
        target = order[Math.max(0, idx - 1)];
        break;
      case "Home":
        target = order[0];
        break;
      case "End":
        target = order[order.length - 1];
        break;
      case "ArrowRight":
        if (it.lazy) {
          loadKey(key);
          e.preventDefault();
          return;
        }
        if (it.all && it.collapsed) {
          toggle(key, false);
          e.preventDefault();
          return;
        }
        target = it.kids[0] || null;
        break;
      case "ArrowLeft":
        if (it.all && !it.collapsed) {
          toggle(key, true);
          e.preventDefault();
          return;
        }
        target = it.parent;
        break;
      case "Enter":
      case " ":
        e.preventDefault();
        select(key, { focus: true });
        return;
      case "+":
      case "=":
        e.preventDefault();
        zoomTo(scale * 1.2);
        return;
      case "-":
        e.preventDefault();
        zoomTo(scale / 1.2);
        return;
      case "0":
        e.preventDefault();
        zoomTo(1);
        return;
      default:
        return;
    }
    e.preventDefault();
    if (target) select(target.key, { focus: true });
  }));

  // drag-to-pan (background or nodes); a drag suppresses the click that follows it
  let drag = null;
  offs.push(listen(frame, "pointerdown", (e) => {
    if (e.button !== 0 || e.pointerType === "touch") return; // touch scrolls natively
    if (e.target.closest && e.target.closest("[data-tree-toggle]")) return;
    drag = { x: e.clientX, y: e.clientY, sl: frame.scrollLeft, st: frame.scrollTop, moved: false, id: e.pointerId };
  }));
  offs.push(listen(frame, "pointermove", (e) => {
    if (!drag || e.pointerId !== drag.id) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 5) return;
    if (!drag.moved) {
      drag.moved = true;
      frame.setPointerCapture?.(e.pointerId);
      frame.classList.add("is-panning");
    }
    frame.scrollLeft = drag.sl - dx;
    frame.scrollTop = drag.st - dy;
  }));
  const endDrag = (e) => {
    if (!drag || (e && e.pointerId !== drag.id)) return;
    const moved = drag.moved;
    drag = null;
    frame.classList.remove("is-panning");
    if (moved) {
      const swallow = (ev) => {
        ev.stopPropagation();
        ev.preventDefault();
      };
      frame.addEventListener("click", swallow, { capture: true, once: true });
      setTimeout(() => frame.removeEventListener("click", swallow, { capture: true }), 0);
    }
  };
  offs.push(listen(frame, "pointerup", endDrag));
  offs.push(listen(frame, "pointercancel", endDrag));
  offs.push(listen(frame, "wheel", (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    const r = frame.getBoundingClientRect();
    zoomTo(scale * (e.deltaY < 0 ? 1.12 : 1 / 1.12), { x: e.clientX - r.left, y: e.clientY - r.top });
  }, { passive: false }));

  // ---------------------------------------------------------------- API
  return {
    el: svg,
    setRoot(next, { resetState = false } = {}) {
      root = next;
      if (resetState) {
        collapsed.clear();
        known.clear();
        loading.clear();
        selectedKey = null;
        focusKey = null;
      }
      if (root) seedCollapse();
      draw();
    },
    select: (key, opt) => select(key, opt),
    selected: () => selectedKey,
    selectedItem: () => (layout && layout.byKey.get(selectedKey)) || null,
    toggle,
    load: (key) => loadKey(key),
    isLoading: (key) => loading.has(key),
    expandAll() {
      collapsed.clear();
      draw();
    },
    collapseAll() {
      if (!root) return;
      (function walk(n, pk) {
        const k = o.keyOf(n, pk);
        if ((o.childrenOf(n) || []).length && pk !== null) collapsed.add(k);
        (o.childrenOf(n) || []).forEach((c) => walk(c, k));
      })(root, null);
      selectedKey = layout ? layout.root.key : null;
      draw();
    },
    collapseToDepth(d) {
      if (!root) return;
      collapsed.clear();
      (function walk(n, pk, depth) {
        const k = o.keyOf(n, pk);
        const ch = o.childrenOf(n) || [];
        if (ch.length && depth >= d) collapsed.add(k);
        ch.forEach((c) => walk(c, k, depth + 1));
      })(root, null, 0);
      draw();
    },
    zoom: (factor) => zoomTo(scale * factor),
    zoomTo,
    scale: () => scale,
    fit({ min = ZOOM_MIN } = {}) {
      if (!layout) return;
      const k = Math.min(1, (frame.clientWidth - 8) / layout.width, (frame.clientHeight - 8) / layout.height);
      zoomTo(Math.max(min, ZOOM_MIN, k));
      frame.scrollLeft = 0;
      frame.scrollTop = 0;
    },
    focus: () => nodeEl(focusKey)?.focus({ preventScroll: true }),
    visibleCount: () => (layout ? layout.items.length : 0),
    exportSvg: ({ title = "" } = {}) => (layout ? inlineSvgForExport(svg, { title, width: layout.width, height: layout.height }) : ""),
    redraw: draw,
    destroy() {
      offs.splice(0).forEach((off) => off());
      frame.replaceChildren();
    },
  };
}

function listen(target, type, fn, opts) {
  target.addEventListener(type, fn, opts);
  return () => target.removeEventListener(type, fn, opts);
}

