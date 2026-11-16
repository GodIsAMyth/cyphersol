/* Command registry behind the ⌘K palette.
 *
 * Command shape: {
 *   id: "transactions.export-view",     // unique; re-registering an id replaces it
 *   title: "Export current view",       // shown in the palette
 *   group: "Transactions",              // section header (default "Actions")
 *   icon: "download",                   // icons.js name (optional)
 *   keywords: ["csv", "filter"],        // extra search terms (optional)
 *   hint: "⇧E",                         // right-aligned hint text (optional)
 *   when: () => boolean,                // hide when false (optional)
 *   run: () => void | Promise<void>,    // invoked after the palette closes
 * }
 * Views register through ctx.registerCommands([...]) so their commands disappear on unmount.
 */

const registry = new Map(); // id -> { cmd, owner }
const listeners = new Set();

export function registerCommands(cmds, owner = "global") {
  const ids = [];
  for (const cmd of cmds) {
    if (!cmd || !cmd.id || typeof cmd.run !== "function") continue;
    registry.set(cmd.id, { cmd: { group: "Actions", keywords: [], ...cmd }, owner });
    ids.push(cmd.id);
  }
  notify();
  return () => {
    let changed = false;
    for (const id of ids) {
      const entry = registry.get(id);
      if (entry && entry.owner === owner) {
        registry.delete(id);
        changed = true;
      }
    }
    if (changed) notify();
  };
}

export function getCommands() {
  const out = [];
  for (const { cmd } of registry.values()) {
    try {
      if (cmd.when && !cmd.when()) continue;
    } catch (_e) {
      continue;
    }
    out.push(cmd);
  }
  return out;
}

export function onCommandsChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function notify() {
  listeners.forEach((fn) => fn());
}

/** Rank commands for a query: simple subsequence fuzzy match over title + group + keywords. */
export function searchCommands(query, cmds = getCommands()) {
  const q = String(query || "").trim().toLowerCase();
  if (!q) return cmds;
  const scored = [];
  for (const c of cmds) {
    const hay = `${c.title} ${c.group} ${(c.keywords || []).join(" ")}`.toLowerCase();
    const s = score(q, hay, c.title.toLowerCase());
    if (s > 0) scored.push([s, c]);
  }
  scored.sort((a, b) => b[0] - a[0]);
  return scored.map((x) => x[1]);
}

function score(q, hay, title) {
  if (title.startsWith(q)) return 100;
  if (title.includes(q)) return 80;
  if (hay.includes(q)) return 60;
  const words = q.split(/\s+/).filter(Boolean);
  if (words.length > 1 && words.every((w) => hay.includes(w))) return 50;
  let i = 0;
  for (const ch of hay) if (ch === q[i]) i += 1;
  return i === q.length ? 20 : 0;
}
