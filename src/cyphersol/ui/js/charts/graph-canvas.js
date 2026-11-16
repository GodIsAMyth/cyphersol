/* Canvas (2D) force-directed network renderer + SVG time brush. Hand-written, no libraries, CSP-safe.
 *
 *   const g = createGraphCanvas(frameEl, {
 *     ariaLabel: "Fund-flow network",
 *     nodeStyle: (data) => ({ fill: "--node-hub", hollow: false, shape: "circle" | "diamond" }),  // token names
 *     edgeEmphasis: (edgeData) => bool,          // drawn in --edge-strong (e.g. conduit edges)
 *     edgeLabel: (edgeData) => "₹1.2 L",         // amount label text
 *     describeNode: (data) => "screen-reader sentence",
 *     onHover: (hit | null, { clientX, clientY }) => {},   // hit = { kind: "node" | "edge", data }
 *     onSelect: (data | null, { source }) => {}, onActivate: (data) => {}, onEdgeClick: (data) => {},
 *   });
 *   g.setData(nodes [{id, label, weight, x?, y?, data}], edges [{id, source, target, amount, data}]);
 *   g.setSelected(id, { center }); g.focusNode(id); g.fit(); g.zoomBy(1.25); g.reheat(); g.setPaused(bool);
 *   g.toBlob() -> Promise<Blob>; g.destroy();
 *
 * Layout: Barnes–Hut many-body repulsion + link springs + weak gravity + grid collision, integrated
 * incrementally per animation frame within a time budget and stopped once alpha settles. With
 * prefers-reduced-motion the layout is computed off-screen in chunks and drawn once (no animation).
 * Colours are CSS tokens resolved with cssVar() and re-read on theme change. Edge width ∝ √amount; mutual
 * (A→B and B→A) edges are drawn as opposite curves. System nodes are diamonds, details-only nodes are rings.
 * Keyboard: arrows move focus spatially between nodes, Enter selects, Shift+Enter / O activates, Esc clears,
 * + / − zoom, 0 fits, Home jumps to the most important node. Focus changes are announced in a live region.
 */
import { cssVar, html, render, uid } from "../core/dom.js";
import { onThemeChange } from "../core/theme.js";

const reduceMQ = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
export const prefersReducedMotion = () => Boolean(reduceMQ && reduceMQ.matches);

const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function tokenCache() {
  const m = new Map();
  return {
    get(name) {
      if (!name) return "transparent";
      if (!name.startsWith("--")) return name;
      if (!m.has(name)) m.set(name, cssVar(name) || "gray");
      return m.get(name);
    },
    clear: () => m.clear(),
  };
}

/* ---------------------------------------------------------------- Barnes–Hut quadtree (charge as mass) */
function qNode(x0, y0, s) {
  return { x0, y0, s, body: null, kids: null, m: 0, cx: 0, cy: 0 };
}
function qChild(q, n, d) {
  const h = q.s / 2;
  const i = (n.x >= q.x0 + h ? 1 : 0) + (n.y >= q.y0 + h ? 2 : 0);
  if (!q.kids[i]) q.kids[i] = qNode(q.x0 + (i & 1 ? h : 0), q.y0 + (i & 2 ? h : 0), h);
  qInsert(q.kids[i], n, d + 1);
}
function qInsert(q, n, d) {
  if (q.kids) return qChild(q, n, d);
  if (!q.body) {
    q.body = [n];
    return undefined;
  }
  const b = q.body[0];
  if (d > 30 || (Math.abs(b.x - n.x) < 1e-6 && Math.abs(b.y - n.y) < 1e-6)) {
    q.body.push(n);
    return undefined;
  }
  const old = q.body;
  q.body = null;
  q.kids = [null, null, null, null];
  for (const o of old) qChild(q, o, d);
  return qChild(q, n, d);
}
function qMass(q) {
  let m = 0;
  let cx = 0;
  let cy = 0;
  let w = 0;
  if (q.body) {
    for (const n of q.body) {
      m += n.charge;
      cx += n.x;
      cy += n.y;
      w += 1;
    }
  } else {
    for (const k of q.kids) {
      if (!k) continue;
      qMass(k);
      const kw = Math.abs(k.m) || 1;
      m += k.m;
      cx += k.cx * kw;
      cy += k.cy * kw;
      w += kw;
    }
  }
  q.m = m;
  q.cx = w ? cx / w : 0;
  q.cy = w ? cy / w : 0;
}
function qForce(q, n, alpha, theta2) {
  if (!q || !q.m) return;
  const dx = q.cx - n.x;
  const dy = q.cy - n.y;
  let l = dx * dx + dy * dy;
  if (q.kids && (q.s * q.s) / theta2 < l) {
    if (l < 1) l = 1;
    const f = (q.m * alpha) / l;
    n.vx += dx * f;
    n.vy += dy * f;
    return;
  }
  if (q.kids) {
    for (const k of q.kids) qForce(k, n, alpha, theta2);
    return;
  }
  for (const o of q.body) {
    if (o === n) continue;
    let ex = o.x - n.x;
    let ey = o.y - n.y;
    let el = ex * ex + ey * ey;
    if (el === 0) {
      ex = (Math.random() - 0.5) * 1e-3;
      ey = (Math.random() - 0.5) * 1e-3;
      el = ex * ex + ey * ey;
    }
    if (el < 1) el = 1;
    const f = (o.charge * alpha) / el;
    n.vx += ex * f;
    n.vy += ey * f;
  }
}

/* ---------------------------------------------------------------- graph canvas */
export function createGraphCanvas(container, opts = {}) {
  const {
    ariaLabel = "Network graph",
    nodeStyle = () => ({ fill: "--node-leaf" }),
    edgeEmphasis = () => false,
    edgeLabel = null,
    describeNode = (d) => String(d?.label || ""),
    onHover = null,
    onSelect = null,
    onActivate = null,
    onEdgeClick = null,
    onSettle = null,
    onStatus = null,
  } = opts;

  const liveId = uid("graph-live");
  render(
    container,
    html`<canvas class="v-network-canvas" tabindex="0" role="application" aria-roledescription="network graph"
        aria-label="${ariaLabel}" aria-describedby="${liveId}"></canvas>
      <div class="sr-only" id="${liveId}" aria-live="polite" aria-atomic="true"></div>`
  );
  const canvas = container.querySelector("canvas");
  const live = container.querySelector(`#${CSS.escape(liveId)}`);
  const ctx2d = canvas.getContext("2d");
  const T = tokenCache();
  let font = cssVar("--font-sans") || "system-ui, sans-serif";

  let W = 0;
  let H = 0;
  let dpr = 1;
  let k = 1;
  let tx = 0;
  let ty = 0;
  let nodes = [];
  let edges = [];
  let byId = new Map();
  let adj = new Map(); // id -> Set(neighbour ids)
  let order = []; // draw order (least important first)
  let alpha = 0;
  let alphaTarget = 0;
  const alphaMin = 0.002;
  const alphaDecay = 1 - Math.pow(alphaMin, 1 / 280);
  let running = false;
  let paused = false;
  let raf = 0;
  let chunkTimer = 0;
  let autoFit = true;
  let selected = null;
  let hover = null;
  let hoverEdge = null;
  let focused = null;
  let drawQueued = 0;
  let destroyed = false;

  const setStatus = (s) => onStatus?.(s);

  /* ----- geometry helpers */
  const rScale = () => clamp(Math.sqrt(k), 0.6, 1.7);
  const toWorld = (px, py) => ({ x: (px - tx) / k, y: (py - ty) / k });
  const localPoint = (e) => {
    const r = canvas.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  };

  function resize() {
    const r = container.getBoundingClientRect();
    const w = Math.max(0, Math.floor(r.width));
    const h = Math.max(0, Math.floor(r.height));
    if (!w || !h) return;
    dpr = window.devicePixelRatio || 1;
    if (w === W && h === H && canvas.width === Math.round(w * dpr)) return;
    const cx = W ? (W / 2 - tx) / k : 0;
    const cy = H ? (H / 2 - ty) / k : 0;
    W = w;
    H = h;
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(h * dpr);
    canvas.style.width = `${w}px`;
    canvas.style.height = `${h}px`;
    if (autoFit) fitNow();
    else {
      tx = W / 2 - cx * k;
      ty = H / 2 - cy * k;
    }
    draw();
  }
  const ro = new ResizeObserver(() => resize());
  ro.observe(container);

  function bounds(list = nodes) {
    let x0 = Infinity;
    let y0 = Infinity;
    let x1 = -Infinity;
    let y1 = -Infinity;
    for (const n of list) {
      x0 = Math.min(x0, n.x - n.r);
      y0 = Math.min(y0, n.y - n.r);
      x1 = Math.max(x1, n.x + n.r);
      y1 = Math.max(y1, n.y + n.r);
    }
    return { x0, y0, x1, y1 };
  }

  function fitNow() {
    if (!nodes.length || !W || !H) {
      k = 1;
      tx = W / 2;
      ty = H / 2;
      return;
    }
    const b = bounds();
    const pad = Math.min(56, Math.max(20, Math.min(W, H) * 0.08));
    const bw = Math.max(40, b.x1 - b.x0);
    const bh = Math.max(40, b.y1 - b.y0);
    k = clamp(Math.min((W - 2 * pad) / bw, (H - 2 * pad - 10) / bh), 0.08, 2.4);
    tx = W / 2 - ((b.x0 + b.x1) / 2) * k;
    ty = H / 2 - ((b.y0 + b.y1) / 2) * k;
  }

  /* ----- simulation */
  function tick() {
    alpha += (alphaTarget - alpha) * alphaDecay;
    // links
    for (const e of edges) {
      const s = e.s;
      const t = e.t;
      if (s === t) continue;
      let dx = t.x + t.vx - s.x - s.vx || 1e-3;
      let dy = t.y + t.vy - s.y - s.vy || 1e-3;
      const l0 = Math.sqrt(dx * dx + dy * dy);
      const l = ((l0 - e.dist) / l0) * alpha * e.strength;
      dx *= l;
      dy *= l;
      t.vx -= dx * e.bias;
      t.vy -= dy * e.bias;
      s.vx += dx * (1 - e.bias);
      s.vy += dy * (1 - e.bias);
    }
    // many-body
    if (nodes.length > 1) {
      const b = bounds();
      const size = Math.max(b.x1 - b.x0, b.y1 - b.y0, 1) + 2;
      const root = qNode(b.x0 - 1, b.y0 - 1, size);
      for (const n of nodes) qInsert(root, n, 0);
      qMass(root);
      for (const n of nodes) qForce(root, n, alpha, 0.81);
    }
    // gravity
    const g = 0.035 * alpha;
    for (const n of nodes) {
      n.vx -= n.x * g;
      n.vy -= n.y * g;
    }
    // collision on a uniform grid
    let maxR = 0;
    for (const n of nodes) maxR = Math.max(maxR, n.r);
    const cell = 2 * maxR + 10;
    const grid = new Map();
    for (const n of nodes) {
      const key = Math.floor(n.x / cell) * 100003 + Math.floor(n.y / cell);
      let arr = grid.get(key);
      if (!arr) grid.set(key, (arr = []));
      arr.push(n);
    }
    for (const n of nodes) {
      const cx = Math.floor(n.x / cell);
      const cy = Math.floor(n.y / cell);
      for (let i = -1; i <= 1; i += 1) {
        for (let j = -1; j <= 1; j += 1) {
          const arr = grid.get((cx + i) * 100003 + (cy + j));
          if (!arr) continue;
          for (const o of arr) {
            if (o.index <= n.index) continue;
            const dx = o.x - n.x || 1e-3;
            const dy = o.y - n.y || 1e-3;
            const d = Math.sqrt(dx * dx + dy * dy);
            const min = n.r + o.r + 5;
            if (d < min) {
              const push = ((min - d) / d) * 0.35;
              n.vx -= dx * push;
              n.vy -= dy * push;
              o.vx += dx * push;
              o.vy += dy * push;
            }
          }
        }
      }
    }
    // integrate
    for (const n of nodes) {
      if (n.fx != null) {
        n.x = n.fx;
        n.y = n.fy;
        n.vx = 0;
        n.vy = 0;
      } else {
        n.vx *= 0.6;
        n.vy *= 0.6;
        n.x += n.vx;
        n.y += n.vy;
      }
    }
  }

  function stopLoop() {
    if (raf) cancelAnimationFrame(raf);
    if (chunkTimer) clearTimeout(chunkTimer);
    raf = 0;
    chunkTimer = 0;
  }

  function settled() {
    running = false;
    setStatus("settled");
    if (autoFit) fitNow();
    draw();
    onSettle?.();
  }

  function frame() {
    raf = 0;
    if (destroyed || !running || paused) return;
    const t0 = performance.now();
    const budget = nodes.length > 600 ? 12 : 8;
    do tick();
    while (alpha >= alphaMin && performance.now() - t0 < budget);
    if (autoFit) fitNow();
    draw();
    if (alpha < alphaMin && alphaTarget === 0) settled();
    else raf = requestAnimationFrame(frame);
  }

  function chunk() {
    chunkTimer = 0;
    if (destroyed || !running || paused) return;
    const t0 = performance.now();
    while (alpha >= alphaMin && performance.now() - t0 < 14) tick();
    if (alpha < alphaMin) settled();
    else chunkTimer = setTimeout(chunk, 0);
  }

  function start(a) {
    alpha = Math.max(alpha, a);
    running = true;
    stopLoop();
    if (paused || !nodes.length) {
      draw();
      return;
    }
    setStatus("running");
    if (prefersReducedMotion()) chunkTimer = setTimeout(chunk, 0);
    else raf = requestAnimationFrame(frame);
  }

  /* ----- drawing */
  function queueDraw() {
    if (drawQueued || destroyed) return;
    drawQueued = requestAnimationFrame(() => {
      drawQueued = 0;
      draw();
    });
  }

  function highlightSet() {
    const c = hover || selected;
    if (!c) return null;
    const s = new Set(adj.get(c.id) || []);
    s.add(c.id);
    if (selected && hover && hover !== selected) {
      s.add(selected.id);
      for (const id of adj.get(selected.id) || []) s.add(id);
    }
    return s;
  }

  function shapePath(n, r) {
    ctx2d.beginPath();
    if (n.shape === "diamond") {
      const d = r * 1.25;
      ctx2d.moveTo(n.sx, n.sy - d);
      ctx2d.lineTo(n.sx + d, n.sy);
      ctx2d.lineTo(n.sx, n.sy + d);
      ctx2d.lineTo(n.sx - d, n.sy);
      ctx2d.closePath();
    } else ctx2d.arc(n.sx, n.sy, r, 0, Math.PI * 2);
  }

  function edgeGeometry(e) {
    const s = e.s;
    const t = e.t;
    const dx = t.sx - s.sx;
    const dy = t.sy - s.sy;
    const len = Math.sqrt(dx * dx + dy * dy);
    if (len < s.sr + t.sr + 4) return null;
    const ux = dx / len;
    const uy = dy / len;
    const mx = (s.sx + t.sx) / 2;
    const my = (s.sy + t.sy) / 2;
    const cx = mx - uy * len * e.curve;
    const cy = my + ux * len * e.curve;
    let ax = cx - s.sx;
    let ay = cy - s.sy;
    let al = Math.sqrt(ax * ax + ay * ay) || 1;
    const x0 = s.sx + (ax / al) * (s.sr * (s.shape === "diamond" ? 1.2 : 1) + 1);
    const y0 = s.sy + (ay / al) * (s.sr * (s.shape === "diamond" ? 1.2 : 1) + 1);
    ax = t.sx - cx;
    ay = t.sy - cy;
    al = Math.sqrt(ax * ax + ay * ay) || 1;
    const ex = ax / al;
    const ey = ay / al;
    const tipX = t.sx - ex * (t.sr * (t.shape === "diamond" ? 1.2 : 1) + 1.5);
    const tipY = t.sy - ey * (t.sr * (t.shape === "diamond" ? 1.2 : 1) + 1.5);
    const head = 4 + e.w * 1.3;
    return { x0, y0, cx, cy, tipX, tipY, ex, ey, head, x1: tipX - ex * head * 0.7, y1: tipY - ey * head * 0.7, len };
  }

  function placeLabel(placed, x, y, w, h) {
    for (const p of placed) if (x < p.x + p.w && x + w > p.x && y < p.y + p.h && y + h > p.y) return false;
    placed.push({ x, y, w, h });
    return true;
  }

  function pill(placed, text, x, y, { bold = false, color = "--text-1", force = false, size = 11 } = {}) {
    ctx2d.font = `${bold ? 600 : 500} ${size}px ${font}`;
    const w = ctx2d.measureText(text).width + 8;
    const h = size + 6;
    const lx = x - w / 2;
    const ly = y - h / 2;
    if (lx > W || ly > H || lx + w < 0 || ly + h < 0) return;
    if (!placeLabel(placed, lx, ly, w, h) && !force) return;
    ctx2d.globalAlpha = 0.9;
    ctx2d.fillStyle = T.get("--surface-1");
    ctx2d.beginPath();
    if (ctx2d.roundRect) ctx2d.roundRect(lx, ly, w, h, 4);
    else ctx2d.rect(lx, ly, w, h);
    ctx2d.fill();
    ctx2d.globalAlpha = 1;
    ctx2d.fillStyle = T.get(color);
    ctx2d.fillText(text, x, y + 0.5);
  }

  function draw() {
    if (destroyed || !W || !H) return;
    ctx2d.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx2d.globalAlpha = 1;
    ctx2d.fillStyle = T.get("--chart-surface");
    ctx2d.fillRect(0, 0, W, H);
    if (!nodes.length) return;
    const rs = rScale();
    for (const n of nodes) {
      n.sx = n.x * k + tx;
      n.sy = n.y * k + ty;
      n.sr = n.r * rs;
    }
    const hl = highlightSet();
    const strong = T.get("--edge-strong");
    const muted = T.get("--edge-muted");
    const focusC = hover || selected;

    // edges
    ctx2d.lineCap = "round";
    for (const e of edges) {
      e.g = edgeGeometry(e);
      if (!e.g) continue;
      const g = e.g;
      const incident = focusC && (e.s === focusC || e.t === focusC);
      const emph = incident || e === hoverEdge || (!hl && e.emph);
      ctx2d.globalAlpha = hl ? (incident || e === hoverEdge ? 0.95 : 0.1) : e.emph ? 0.85 : 0.55;
      const col = emph ? strong : muted;
      ctx2d.strokeStyle = col;
      ctx2d.fillStyle = col;
      ctx2d.lineWidth = e === hoverEdge ? e.w + 1.5 : e.w;
      ctx2d.beginPath();
      ctx2d.moveTo(g.x0, g.y0);
      if (e.curve) ctx2d.quadraticCurveTo(g.cx, g.cy, g.x1, g.y1);
      else ctx2d.lineTo(g.x1, g.y1);
      ctx2d.stroke();
      // arrow head
      const hw = g.head * 0.55;
      ctx2d.beginPath();
      ctx2d.moveTo(g.tipX, g.tipY);
      ctx2d.lineTo(g.tipX - g.ex * g.head - g.ey * hw, g.tipY - g.ey * g.head + g.ex * hw);
      ctx2d.lineTo(g.tipX - g.ex * g.head + g.ey * hw, g.tipY - g.ey * g.head - g.ex * hw);
      ctx2d.closePath();
      ctx2d.fill();
    }

    // nodes
    const stroke = T.get("--node-stroke");
    const surface = T.get("--surface-1");
    for (const n of order) {
      const dim = hl && !hl.has(n.id);
      ctx2d.globalAlpha = dim ? 0.22 : 1;
      const st = n.style;
      const fill = T.get(st.fill);
      shapePath(n, n.sr);
      if (st.hollow) {
        ctx2d.fillStyle = surface;
        ctx2d.fill();
        ctx2d.lineWidth = clamp(n.sr * 0.42, 2, 5);
        ctx2d.strokeStyle = fill;
        ctx2d.stroke();
      } else {
        ctx2d.fillStyle = fill;
        ctx2d.fill();
        ctx2d.lineWidth = 1.5;
        ctx2d.strokeStyle = stroke;
        ctx2d.stroke();
      }
      ctx2d.globalAlpha = 1;
      if (n === selected) {
        shapePath(n, n.sr + 4);
        ctx2d.lineWidth = 2.5;
        ctx2d.strokeStyle = T.get("--accent");
        ctx2d.stroke();
      } else if (n === hover) {
        shapePath(n, n.sr + 3);
        ctx2d.lineWidth = 1.5;
        ctx2d.strokeStyle = T.get("--text-2");
        ctx2d.stroke();
      }
      if (n === focused && document.activeElement === canvas) {
        ctx2d.setLineDash([3, 3]);
        shapePath(n, n.sr + 7);
        ctx2d.lineWidth = 2;
        ctx2d.strokeStyle = T.get("--focus-ring");
        ctx2d.stroke();
        ctx2d.setLineDash([]);
      }
    }

    // labels: forced (selection context) first, then by importance within a zoom-dependent budget
    ctx2d.textAlign = "center";
    ctx2d.textBaseline = "middle";
    const placed = [];
    const forced = [];
    if (selected) forced.push(selected);
    if (hover && hover !== selected) forced.push(hover);
    if (focused && !forced.includes(focused)) forced.push(focused);
    for (const n of forced) pill(placed, n.short, n.sx, n.sy + n.sr + 10, { bold: true, force: true });
    // neighbours of the selection / hover get labels before edge amounts compete for space
    if (hl && hl.size <= 60) {
      for (let i = order.length - 1; i >= 0; i -= 1) {
        const n = order[i];
        if (hl.has(n.id) && !forced.includes(n)) pill(placed, n.short, n.sx, n.sy + n.sr + 10, { color: n.style.hollow ? "--text-2" : "--text-1" });
      }
    }

    // edge amount labels (selection context + emphasised edges when zoomed in)
    if (edgeLabel) {
      for (const e of edges) {
        if (!e.g) continue;
        const ctxEdge = focusC && (e.s === focusC || e.t === focusC);
        if (!(ctxEdge || e === hoverEdge || (!hl && e.emph && k >= 0.9 && e.g.len > 110))) continue;
        const g = e.g;
        const mx = 0.25 * g.x0 + 0.5 * (e.curve ? g.cx : (g.x0 + g.x1) / 2) + 0.25 * g.x1;
        const my = 0.25 * g.y0 + 0.5 * (e.curve ? g.cy : (g.y0 + g.y1) / 2) + 0.25 * g.y1;
        pill(placed, edgeLabel(e.data), mx, my, { size: 10, color: "--text-2", bold: true });
      }
    }
    const budget = Math.round(clamp((W * H) / 16000, 6, 90) * clamp(k, 0.5, 3));
    let shown = 0;
    for (let i = order.length - 1; i >= 0 && shown < budget; i -= 1) {
      const n = order[i];
      if (forced.includes(n)) continue;
      if (hl) continue;
      if (n.sr < 5 && k < 1.2) continue;
      const before = placed.length;
      pill(placed, n.short, n.sx, n.sy + n.sr + 10, { color: n.style.hollow ? "--text-2" : "--text-1" });
      if (placed.length > before) shown += 1;
    }
    ctx2d.globalAlpha = 1;
  }

  /* ----- data */
  function setData(nodeList, edgeList, { keepView = false } = {}) {
    const prev = byId;
    byId = new Map();
    let maxW = 0;
    for (const n of nodeList) maxW = Math.max(maxW, n.weight || 0);
    nodes = nodeList.map((d, index) => {
      const old = prev.get(d.id);
      const r = 5 + 13 * Math.sqrt((d.weight || 0) / (maxW || 1));
      const n = {
        id: d.id,
        index,
        data: d.data,
        label: d.label || d.id,
        short: d.label && d.label.length > 24 ? `${d.label.slice(0, 23)}…` : d.label || d.id,
        weight: d.weight || 0,
        r,
        charge: -110 - 14 * r,
        x: old ? old.x : d.x ?? (Math.random() - 0.5) * 200,
        y: old ? old.y : d.y ?? (Math.random() - 0.5) * 200,
        vx: 0,
        vy: 0,
        fx: null,
        fy: null,
        deg: 0,
        sx: 0,
        sy: 0,
        sr: r,
        style: {},
        shape: "circle",
      };
      byId.set(n.id, n);
      return n;
    });
    adj = new Map(nodes.map((n) => [n.id, new Set()]));
    const pairs = new Set(edgeList.map((e) => `${e.source}\u0000${e.target}`));
    let maxA = 0;
    for (const e of edgeList) maxA = Math.max(maxA, e.amount || 0);
    edges = [];
    for (const e of edgeList) {
      const s = byId.get(e.source);
      const t = byId.get(e.target);
      if (!s || !t || s === t) continue;
      s.deg += 1;
      t.deg += 1;
      adj.get(s.id).add(t.id);
      adj.get(t.id).add(s.id);
      edges.push({ id: e.id, s, t, data: e.data, amount: e.amount || 0, curve: pairs.has(`${e.target}\u0000${e.source}`) ? 0.2 : 0, w: 0.9 + 5.1 * Math.sqrt((e.amount || 0) / (maxA || 1)) });
    }
    for (const e of edges) {
      e.bias = e.s.deg / (e.s.deg + e.t.deg);
      e.strength = 1 / Math.min(e.s.deg, e.t.deg);
      e.dist = 46 + 7 * Math.sqrt(Math.min(e.s.deg, e.t.deg)) + e.s.r + e.t.r;
      e.emph = Boolean(edgeEmphasis(e.data));
    }
    // place new nodes without coordinates next to an already-placed neighbour
    for (const n of nodes) {
      if (prev.has(n.id) || nodeList[n.index].x != null) continue;
      const nb = [...adj.get(n.id)].map((id) => byId.get(id)).find((o) => prev.has(o.id));
      if (nb) {
        n.x = nb.x + (Math.random() - 0.5) * 40;
        n.y = nb.y + (Math.random() - 0.5) * 40;
      }
    }
    restyle(false);
    selected = selected ? byId.get(selected.id) || null : null;
    focused = focused ? byId.get(focused.id) || null : null;
    hover = null;
    hoverEdge = null;
    if (!keepView) autoFit = true;
    alpha = 0;
    if (!nodes.length) {
      stopLoop();
      running = false;
      draw();
      setStatus("empty");
      return;
    }
    start(prev.size ? 0.45 : 0.9);
  }

  function restyle(redraw = true) {
    for (const n of nodes) {
      n.style = nodeStyle(n.data) || {};
      n.shape = n.style.shape === "diamond" ? "diamond" : "circle";
    }
    for (const e of edges) e.emph = Boolean(edgeEmphasis(e.data));
    order = [...nodes].sort((a, b) => (a.style.z || 0) - (b.style.z || 0) || a.weight - b.weight);
    if (redraw) draw();
  }

  /* ----- hit testing */
  function nodeAt(px, py) {
    for (let i = order.length - 1; i >= 0; i -= 1) {
      const n = order[i];
      const r = n.sr * (n.shape === "diamond" ? 1.25 : 1) + 3;
      const dx = px - n.sx;
      const dy = py - n.sy;
      if (dx * dx + dy * dy <= r * r) return n;
    }
    return null;
  }
  function segDist2(px, py, ax, ay, bx, by) {
    const vx = bx - ax;
    const vy = by - ay;
    const l = vx * vx + vy * vy || 1;
    const t = clamp(((px - ax) * vx + (py - ay) * vy) / l, 0, 1);
    const x = ax + t * vx - px;
    const y = ay + t * vy - py;
    return x * x + y * y;
  }
  function edgeAt(px, py) {
    let best = null;
    let bestD = Infinity;
    for (const e of edges) {
      const g = e.g;
      if (!g) continue;
      const thr = Math.max(4, e.w / 2 + 3);
      const minX = Math.min(g.x0, g.tipX, g.cx) - thr;
      const maxX = Math.max(g.x0, g.tipX, g.cx) + thr;
      const minY = Math.min(g.y0, g.tipY, g.cy) - thr;
      const maxY = Math.max(g.y0, g.tipY, g.cy) + thr;
      if (px < minX || px > maxX || py < minY || py > maxY) continue;
      let lx = g.x0;
      let ly = g.y0;
      const steps = e.curve ? 10 : 1;
      for (let i = 1; i <= steps; i += 1) {
        const t = i / steps;
        const u = 1 - t;
        const x = e.curve ? u * u * g.x0 + 2 * u * t * g.cx + t * t * g.tipX : g.tipX;
        const y = e.curve ? u * u * g.y0 + 2 * u * t * g.cy + t * t * g.tipY : g.tipY;
        const d = segDist2(px, py, lx, ly, x, y);
        if (d < thr * thr && d < bestD) {
          bestD = d;
          best = e;
        }
        lx = x;
        ly = y;
      }
    }
    return best;
  }

  /* ----- selection / focus */
  function announce(n) {
    live.textContent = n ? describeNode(n.data) : "";
  }
  function ensureVisible(n) {
    const sx = n.x * k + tx;
    const sy = n.y * k + ty;
    const m = 40;
    if (sx < m || sx > W - m || sy < m || sy > H - m) {
      autoFit = false;
      tx = W / 2 - n.x * k;
      ty = H / 2 - n.y * k;
    }
  }
  function select(n, source) {
    selected = n;
    if (n) focused = n;
    draw();
    onSelect?.(n ? n.data : null, { source });
  }
  function focusOn(n) {
    focused = n;
    if (n) ensureVisible(n);
    announce(n);
    draw();
  }
  function mostImportant() {
    return order[order.length - 1] || null;
  }
  function moveFocus(dx, dy) {
    const f = focused || selected;
    if (!f) return focusOn(mostImportant());
    let best = null;
    let bestScore = Infinity;
    for (const n of nodes) {
      if (n === f) continue;
      const vx = n.x - f.x;
      const vy = n.y - f.y;
      const along = vx * dx + vy * dy;
      if (along <= 0) continue;
      const perp = Math.abs(vx * dy - vy * dx);
      if (perp > along * 2.2) continue;
      const score = along + perp * 2.5;
      if (score < bestScore) {
        bestScore = score;
        best = n;
      }
    }
    if (best) focusOn(best);
    return undefined;
  }

  /* ----- zoom */
  function zoomBy(f, px = W / 2, py = H / 2) {
    const nk = clamp(k * f, 0.08, 8);
    tx = px - (px - tx) * (nk / k);
    ty = py - (py - ty) * (nk / k);
    k = nk;
    autoFit = false;
    queueDraw();
  }

  /* ----- pointer interaction */
  const pointers = new Map();
  let drag = null;
  let pan = null;
  let pinch = null;
  let hoverQueued = 0;
  let lastMove = null;

  function setCursor(c) {
    if (canvas.style.cursor !== c) canvas.style.cursor = c;
  }

  function doHover() {
    hoverQueued = 0;
    if (!lastMove || drag || pan || pinch) return;
    const p = localPoint(lastMove);
    const n = nodeAt(p.x, p.y);
    const e = n ? null : edgeAt(p.x, p.y);
    const changed = n !== hover || e !== hoverEdge;
    hover = n;
    hoverEdge = e;
    setCursor(n ? "pointer" : e ? "help" : "grab");
    if (changed) draw();
    onHover?.(n ? { kind: "node", data: n.data } : e ? { kind: "edge", data: e.data } : null, { clientX: lastMove.clientX, clientY: lastMove.clientY });
  }

  const onPointerDown = (e) => {
    if (e.button !== undefined && e.button > 0) return;
    canvas.setPointerCapture?.(e.pointerId);
    const p = localPoint(e);
    pointers.set(e.pointerId, p);
    onHover?.(null, {});
    if (pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      pinch = { d: Math.hypot(a.x - b.x, a.y - b.y) || 1, k };
      drag = null;
      pan = null;
      return;
    }
    const n = nodeAt(p.x, p.y);
    if (n) drag = { node: n, x: p.x, y: p.y, moved: false };
    else pan = { x: p.x, y: p.y, tx, ty, moved: false };
    setCursor(n ? "grabbing" : "grabbing");
  };

  const onPointerMove = (e) => {
    const p = localPoint(e);
    if (pointers.has(e.pointerId)) pointers.set(e.pointerId, p);
    if (pinch && pointers.size === 2) {
      const [a, b] = [...pointers.values()];
      const d = Math.hypot(a.x - b.x, a.y - b.y) || 1;
      zoomBy((pinch.k * (d / pinch.d)) / k, (a.x + b.x) / 2, (a.y + b.y) / 2);
      return;
    }
    if (drag) {
      if (!drag.moved && Math.hypot(p.x - drag.x, p.y - drag.y) > 3) drag.moved = true;
      if (drag.moved) {
        const w = toWorld(p.x, p.y);
        const n = drag.node;
        n.fx = w.x;
        n.fy = w.y;
        autoFit = false;
        if (prefersReducedMotion() || paused) {
          n.x = w.x;
          n.y = w.y;
          queueDraw();
        } else {
          alphaTarget = 0.25;
          if (!raf) start(0.25);
        }
      }
      return;
    }
    if (pan) {
      if (!pan.moved && Math.hypot(p.x - pan.x, p.y - pan.y) > 3) pan.moved = true;
      if (pan.moved) {
        tx = pan.tx + (p.x - pan.x);
        ty = pan.ty + (p.y - pan.y);
        autoFit = false;
        queueDraw();
      }
      return;
    }
    lastMove = e;
    if (!hoverQueued) hoverQueued = requestAnimationFrame(doHover);
  };

  const onPointerUp = (e) => {
    pointers.delete(e.pointerId);
    if (pinch) {
      if (pointers.size < 2) pinch = null;
      return;
    }
    if (drag) {
      const n = drag.node;
      if (!drag.moved) select(n === selected ? null : n, "pointer");
      else {
        n.fx = null;
        n.fy = null;
        alphaTarget = 0;
      }
      drag = null;
    } else if (pan) {
      if (!pan.moved) {
        const p = localPoint(e);
        const edge = edgeAt(p.x, p.y);
        if (edge && onEdgeClick) onEdgeClick(edge.data);
        else if (selected) select(null, "pointer");
      }
      pan = null;
    }
    setCursor("grab");
  };

  const onLeave = () => {
    lastMove = null;
    if (hover || hoverEdge) {
      hover = null;
      hoverEdge = null;
      draw();
    }
    onHover?.(null, {});
  };

  // Plain wheel scrolls the page unless the graph has focus (clicked / tabbed into); Ctrl/⌘ + wheel and
  // trackpad pinch (reported as ctrlKey) always zoom. Avoids hijacking page scroll on a long view.
  const onWheel = (e) => {
    if (!(e.ctrlKey || e.metaKey || document.activeElement === canvas)) return;
    e.preventDefault();
    const p = localPoint(e);
    const f = Math.exp(-e.deltaY * (e.deltaMode === 1 ? 0.05 : 0.0018));
    zoomBy(f, p.x, p.y);
  };

  const onDbl = (e) => {
    const p = localPoint(e);
    const n = nodeAt(p.x, p.y);
    if (n) onActivate?.(n.data);
    else zoomBy(1.6, p.x, p.y);
  };

  const onKey = (e) => {
    const dirs = { ArrowLeft: [-1, 0], ArrowRight: [1, 0], ArrowUp: [0, -1], ArrowDown: [0, 1] };
    if (dirs[e.key]) {
      e.preventDefault();
      moveFocus(...dirs[e.key]);
      return;
    }
    switch (e.key) {
      case "Enter":
      case " ":
        if (!focused) return;
        e.preventDefault();
        if (e.shiftKey && e.key === "Enter") onActivate?.(focused.data);
        else select(focused, "keyboard");
        break;
      case "o":
      case "O":
        if (focused) onActivate?.(focused.data);
        break;
      case "Escape":
        if (selected) {
          e.preventDefault();
          select(null, "keyboard");
        }
        break;
      case "+":
      case "=":
        e.preventDefault();
        zoomBy(1.25);
        break;
      case "-":
      case "_":
        e.preventDefault();
        zoomBy(0.8);
        break;
      case "0":
        e.preventDefault();
        autoFit = true;
        fitNow();
        draw();
        break;
      case "Home":
        e.preventDefault();
        focusOn(mostImportant());
        break;
      default:
    }
  };

  const onFocus = () => {
    if (!focused) focused = selected || mostImportant();
    if (focused) announce(focused);
    draw();
  };
  const onBlur = () => draw();

  canvas.addEventListener("pointerdown", onPointerDown);
  canvas.addEventListener("pointermove", onPointerMove);
  canvas.addEventListener("pointerup", onPointerUp);
  canvas.addEventListener("pointercancel", onPointerUp);
  canvas.addEventListener("pointerleave", onLeave);
  canvas.addEventListener("wheel", onWheel, { passive: false });
  canvas.addEventListener("dblclick", onDbl);
  canvas.addEventListener("keydown", onKey);
  canvas.addEventListener("focus", onFocus);
  canvas.addEventListener("blur", onBlur);
  setCursor("grab");

  const offTheme = onThemeChange(() => {
    T.clear();
    font = cssVar("--font-sans") || font;
    draw();
  });

  resize();

  return {
    canvas,
    setData,
    restyle: () => restyle(true),
    redraw: () => draw(),
    setSelected(id, { center = false } = {}) {
      const n = id ? byId.get(id) || null : null;
      selected = n;
      if (n) {
        focused = n;
        if (center) ensureVisible(n);
      }
      draw();
      return Boolean(n);
    },
    focusNode(id) {
      const n = byId.get(id);
      if (!n) return;
      canvas.focus({ preventScroll: true });
      focusOn(n);
    },
    centerOn(id) {
      const n = byId.get(id);
      if (!n) return;
      autoFit = false;
      tx = W / 2 - n.x * k;
      ty = H / 2 - n.y * k;
      draw();
    },
    fit() {
      autoFit = true;
      fitNow();
      draw();
    },
    zoomBy: (f) => zoomBy(f),
    reheat() {
      for (const n of nodes) {
        n.x += (Math.random() - 0.5) * 30;
        n.y += (Math.random() - 0.5) * 30;
      }
      autoFit = true;
      start(0.9);
    },
    setPaused(p) {
      paused = Boolean(p);
      if (paused) {
        stopLoop();
        setStatus("paused");
      } else if (running) start(alpha);
      else setStatus("settled");
    },
    isRunning: () => running && !paused,
    hasNode: (id) => byId.has(id),
    neighbours: (id) => [...(adj.get(id) || [])],
    toBlob: () => new Promise((resolve) => canvas.toBlob((b) => resolve(b), "image/png")),
    destroy() {
      destroyed = true;
      stopLoop();
      if (drawQueued) cancelAnimationFrame(drawQueued);
      if (hoverQueued) cancelAnimationFrame(hoverQueued);
      ro.disconnect();
      offTheme();
      canvas.removeEventListener("pointerdown", onPointerDown);
      canvas.removeEventListener("pointermove", onPointerMove);
      canvas.removeEventListener("pointerup", onPointerUp);
      canvas.removeEventListener("pointercancel", onPointerUp);
      canvas.removeEventListener("pointerleave", onLeave);
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("dblclick", onDbl);
      canvas.removeEventListener("keydown", onKey);
      canvas.removeEventListener("focus", onFocus);
      canvas.removeEventListener("blur", onBlur);
    },
  };
}

/* ======================================================================================================
 * Time brush: SVG activity histogram with a draggable / keyboard-adjustable date window.
 *
 *   const b = createTimeBrush(el, { ariaLabel, onChange: ([from, to] | null, { final }) => {}, formatDate });
 *   b.setData({ from: "2026-04-01", to: "2026-04-30", daily: Map<"YYYY-MM-DD", value> });
 *   b.setValue(["2026-04-03", "2026-04-20"] | null);  b.destroy();
 * Dragging the background draws a new window, dragging the window moves it, the two handles are
 * role="slider" (←/→ one day, Shift+←/→ seven days, Home/End). A window equal to the full domain = null.
 * ==================================================================================================== */
const DAY = 864e5;
export const dayOf = (iso) => {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(iso || ""));
  return m ? Math.floor(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])) / DAY) : null;
};
export const isoOf = (day) => new Date(day * DAY).toISOString().slice(0, 10);

export function createTimeBrush(el, { ariaLabel = "Date range", onChange = null, formatDate = (s) => s } = {}) {
  const H = 64;
  const BAR_H = 42;
  let W = 0;
  let d0 = null;
  let d1 = null;
  let daily = new Map();
  let value = null; // [day, day] or null
  let bins = [];
  let gesture = null;
  let destroyed = false;

  render(el, html`<svg class="v-network-brush__svg" role="group" aria-label="${ariaLabel}" height="${H}"></svg>`);
  const svg = el.querySelector("svg");

  const span = () => Math.max(1, d1 - d0 + 1);
  const xOf = (day) => ((day - d0) / span()) * W;
  const dayAt = (x) => clamp(Math.floor((x / Math.max(1, W)) * span()) + d0, d0, d1);
  const cur = () => value || [d0, d1];

  function rebin() {
    bins = [];
    if (d0 == null || !W) return;
    const per = Math.max(1, Math.ceil(span() / Math.max(1, W / 5)));
    for (let s = d0; s <= d1; s += per) {
      let v = 0;
      for (let d = s; d < s + per && d <= d1; d += 1) v += daily.get(d) || 0;
      bins.push({ s, e: Math.min(d1, s + per - 1), v });
    }
  }

  function draw() {
    if (destroyed) return;
    if (d0 == null || !W) {
      render(svg, "");
      return;
    }
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    svg.setAttribute("width", String(W));
    const max = Math.max(1, ...bins.map((b) => b.v));
    const [a, b] = cur();
    const bw = W / Math.max(1, bins.length);
    const ticks = [];
    const nt = Math.max(2, Math.min(6, Math.floor(W / 120)));
    for (let i = 0; i <= nt; i += 1) ticks.push(Math.round(d0 + ((d1 - d0) * i) / nt));
    const xa = xOf(a);
    const xb = xOf(b + 1);
    render(
      svg,
      html`<rect class="v-network-brush__bg" x="0" y="0" width="${W}" height="${BAR_H}" rx="4"></rect>
        ${bins.map((bn, i) => {
          const h = bn.v ? Math.max(2, (Math.sqrt(bn.v) / Math.sqrt(max)) * (BAR_H - 6)) : 0;
          const inside = bn.e >= a && bn.s <= b;
          return h
            ? html`<rect class="v-network-brush__bar ${inside ? "is-in" : ""}" x="${(i * bw + 0.5).toFixed(1)}" y="${(BAR_H - h).toFixed(1)}"
                width="${Math.max(1, bw - 1).toFixed(1)}" height="${h.toFixed(1)}"></rect>`
            : "";
        })}
        ${value ? html`<rect class="v-network-brush__shade" x="0" y="0" width="${xa.toFixed(1)}" height="${BAR_H}"></rect>
          <rect class="v-network-brush__shade" x="${xb.toFixed(1)}" y="0" width="${Math.max(0, W - xb).toFixed(1)}" height="${BAR_H}"></rect>` : ""}
        <rect class="v-network-brush__sel ${value ? "is-active" : ""}" data-part="sel" x="${xa.toFixed(1)}" y="0.5" width="${Math.max(2, xb - xa).toFixed(1)}" height="${BAR_H - 1}" rx="3"></rect>
        ${[0, 1].map((h) => {
          const x = h ? xb : xa;
          const day = h ? b : a;
          return html`<g class="v-network-brush__handle" data-part="h${h}" tabindex="0" role="slider"
              aria-label="${h ? "End date" : "Start date"}" aria-valuemin="${d0}" aria-valuemax="${d1}" aria-valuenow="${day}"
              aria-valuetext="${formatDate(isoOf(day))}">
              <rect class="v-network-brush__grip" x="${(x - 5).toFixed(1)}" y="6" width="10" height="${BAR_H - 12}" rx="3"></rect>
              <line class="v-network-brush__grip-line" x1="${x.toFixed(1)}" x2="${x.toFixed(1)}" y1="12" y2="${BAR_H - 12}"></line>
            </g>`;
        })}
        ${ticks.map((d, i) => html`<text class="svg-text-3 v-network-brush__tick" x="${clamp(xOf(d) + (i === ticks.length - 1 ? bw : 0), 0, W).toFixed(1)}" y="${H - 4}"
            text-anchor="${i === 0 ? "start" : i === ticks.length - 1 ? "end" : "middle"}">${formatDate(isoOf(d))}</text>`)}`
    );
  }

  function emit(final) {
    let v = value;
    if (v && v[0] <= d0 && v[1] >= d1) v = null;
    value = v;
    onChange?.(v ? [isoOf(v[0]), isoOf(v[1])] : null, { final });
  }

  const local = (e) => {
    const r = svg.getBoundingClientRect();
    return clamp(e.clientX - r.left, 0, W);
  };

  const onDown = (e) => {
    if (d0 == null || (e.button !== undefined && e.button > 0)) return;
    const part = e.target.closest("[data-part]")?.dataset.part || "bg";
    const x = local(e);
    const day = dayAt(x);
    const [a, b] = cur();
    svg.setPointerCapture?.(e.pointerId);
    if (part === "h0" || part === "h1") gesture = { kind: part, fixed: part === "h0" ? b : a };
    else if (part === "sel" && value) gesture = { kind: "move", off: day - a, len: b - a };
    else gesture = { kind: "new", anchor: day, moved: false };
    e.preventDefault();
  };
  const onMove = (e) => {
    if (!gesture) return;
    const day = dayAt(local(e));
    if (gesture.kind === "h0") value = [Math.min(day, gesture.fixed), Math.max(day, gesture.fixed)];
    else if (gesture.kind === "h1") value = [Math.min(day, gesture.fixed), Math.max(day, gesture.fixed)];
    else if (gesture.kind === "move") {
      const s = clamp(day - gesture.off, d0, d1 - gesture.len);
      value = [s, s + gesture.len];
    } else {
      if (day !== gesture.anchor) gesture.moved = true;
      value = [Math.min(day, gesture.anchor), Math.max(day, gesture.anchor)];
    }
    draw();
    emit(false);
  };
  const onUp = () => {
    if (!gesture) return;
    const g = gesture;
    gesture = null;
    if (g.kind === "new" && !g.moved) {
      // single click on the background clears the window
      value = null;
      draw();
    }
    emit(true);
  };
  const onKey = (e) => {
    const part = e.target.closest?.("[data-part]")?.dataset.part;
    if (part !== "h0" && part !== "h1") return;
    let [a, b] = cur();
    const step = e.shiftKey ? 7 : 1;
    let handled = true;
    const move = (d) => {
      if (part === "h0") a = clamp(a + d, d0, b);
      else b = clamp(b + d, a, d1);
    };
    if (e.key === "ArrowLeft" || e.key === "ArrowDown") move(-step);
    else if (e.key === "ArrowRight" || e.key === "ArrowUp") move(step);
    else if (e.key === "Home") part === "h0" ? (a = d0) : (b = a);
    else if (e.key === "End") part === "h0" ? (a = b) : (b = d1);
    else handled = false;
    if (!handled) return;
    e.preventDefault();
    value = [a, b];
    draw();
    svg.querySelector(`[data-part="${part}"]`)?.focus();
    emit(true);
  };

  svg.addEventListener("pointerdown", onDown);
  svg.addEventListener("pointermove", onMove);
  svg.addEventListener("pointerup", onUp);
  svg.addEventListener("pointercancel", onUp);
  svg.addEventListener("keydown", onKey);
  const ro = new ResizeObserver(() => {
    const w = Math.floor(el.getBoundingClientRect().width);
    if (w && w !== W) {
      W = w;
      rebin();
      draw();
    }
  });
  ro.observe(el);

  return {
    setData({ from, to, daily: map }) {
      d0 = dayOf(from);
      d1 = dayOf(to);
      if (d0 != null && d1 != null && d1 < d0) [d0, d1] = [d1, d0];
      daily = new Map();
      for (const [iso, v] of map || []) {
        const d = dayOf(iso);
        if (d != null) daily.set(d, (daily.get(d) || 0) + v);
      }
      if (value && d0 != null) value = [clamp(value[0], d0, d1), clamp(value[1], d0, d1)];
      if (!W) W = Math.floor(el.getBoundingClientRect().width);
      rebin();
      draw();
    },
    setValue(v) {
      const a = v && v[0] ? dayOf(v[0]) : null;
      const b = v && v[1] ? dayOf(v[1]) : null;
      if (a == null && b == null) value = null;
      else if (d0 != null) value = [clamp(a ?? d0, d0, d1), clamp(b ?? d1, d0, d1)];
      else value = [a ?? b, b ?? a];
      if (value && value[0] > value[1]) value = [value[1], value[0]];
      draw();
    },
    hasDomain: () => d0 != null,
    destroy() {
      destroyed = true;
      ro.disconnect();
      svg.removeEventListener("pointerdown", onDown);
      svg.removeEventListener("pointermove", onMove);
      svg.removeEventListener("pointerup", onUp);
      svg.removeEventListener("pointercancel", onUp);
      svg.removeEventListener("keydown", onKey);
    },
  };
}
