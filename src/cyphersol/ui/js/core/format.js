/* Number, money and date formatting (en-IN). Negative numbers use a true minus sign (−₹1,200.00, UI-S-25).
 * All formatters accept numbers or numeric strings and return "—" for null/undefined/NaN. */

export const DASH = "—";
const MINUS = "−";

const toNum = (v) => {
  if (v == null || v === "") return null;
  const n = typeof v === "number" ? v : Number(v);
  return Number.isFinite(n) ? n : null;
};

const nfCache = new Map();
function nf(min, max) {
  const key = `${min}|${max}`;
  if (!nfCache.has(key)) {
    nfCache.set(key, new Intl.NumberFormat("en-IN", { minimumFractionDigits: min, maximumFractionDigits: max }));
  }
  return nfCache.get(key);
}

/**
 * Full INR: inr(1234567.5) -> "₹12,34,567.50"; inr(-50) -> "−₹50.00".
 * opts: { decimals = 2, sign = false (prefix "+" for positives), zero = undefined (string to show for 0) }
 */
export function inr(v, { decimals = 2, sign = false, zero } = {}) {
  const n = toNum(v);
  if (n == null) return DASH;
  if (n === 0 && zero !== undefined) return zero;
  const body = "₹" + nf(decimals, decimals).format(Math.abs(n));
  if (n < 0) return MINUS + body;
  return sign && n > 0 ? "+" + body : body;
}

/**
 * Compact INR with Indian units: ₹950, ₹45,200, ₹1.2 L, ₹3.4 Cr, ₹1,250 Cr. Use for tiles/labels and put the
 * exact inr() value in a title/tooltip.
 */
export function inrCompact(v, { sign = false } = {}) {
  const n = toNum(v);
  if (n == null) return DASH;
  const a = Math.abs(n);
  let body;
  if (a >= 1e7) body = `₹${nf(0, a >= 1e9 ? 0 : 2).format(a / 1e7)} Cr`;
  else if (a >= 1e5) body = `₹${nf(0, 2).format(a / 1e5)} L`;
  else body = `₹${nf(0, 0).format(Math.round(a))}`;
  if (n < 0) return MINUS + body;
  return sign && n > 0 ? "+" + body : body;
}

/** Plain number with Indian grouping: num(1234567.891, 2) -> "12,34,567.89". */
export function num(v, decimals = 0) {
  const n = toNum(v);
  if (n == null) return DASH;
  const s = nf(decimals, decimals).format(Math.abs(n));
  return n < 0 ? MINUS + s : s;
}

/** Integer count: int(12345) -> "12,345". */
export const int = (v) => num(v, 0);

/**
 * Percent. By default the input is a ratio (0.531 -> "53.1%"); pass { ratio: false } when it is already a
 * percentage (53.1 -> "53.1%").
 */
export function pct(v, { decimals = 1, ratio = true } = {}) {
  const n = toNum(v);
  if (n == null) return DASH;
  return `${num(ratio ? n * 100 : n, decimals)}%`;
}

/** Signed CSS class for a number: "is-neg" | "is-pos" | "". */
export const signClass = (v) => {
  const n = toNum(v);
  return n == null || n === 0 ? "" : n < 0 ? "is-neg" : "is-pos";
};

/** Debit/credit helpers for transaction rows ({debit, credit}). */
export const txAmount = (t) => (toNum(t?.credit) > 0 ? toNum(t.credit) : -(toNum(t?.debit) || 0));
export const drCr = (t) => (toNum(t?.credit) > 0 ? "Cr" : "Dr");

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function parseDate(v) {
  if (v == null || v === "") return null;
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : v;
  const s = String(v);
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s);
  if (m) return new Date(Number(m[1]), Number(m[2]) - 1, Number(m[3]));
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "2026-04-05" -> "05 Apr 2026". Unparseable input is returned unchanged (escaped by the caller). */
export function date(v) {
  const d = parseDate(v);
  if (!d) return v ? String(v) : DASH;
  return `${String(d.getDate()).padStart(2, "0")} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

/** ISO timestamp -> "05 Apr 2026, 14:32". */
export function dateTime(v) {
  const d = parseDate(v);
  if (!d) return v ? String(v) : DASH;
  return `${date(d)}, ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

/** "2026-04" or "2026-04-01" -> "Apr 2026". */
export function month(v) {
  const m = /^(\d{4})-(\d{2})/.exec(String(v ?? ""));
  if (!m) return v ? String(v) : DASH;
  return `${MONTHS[Number(m[2]) - 1]} ${m[1]}`;
}

/** Relative time: "just now", "5 min ago", "3 h ago", "2 d ago", else date(). */
export function relTime(v, now = Date.now()) {
  const d = parseDate(v);
  if (!d) return DASH;
  const s = Math.round((now - d.getTime()) / 1000);
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 86400 * 14) return `${Math.round(s / 86400)} d ago`;
  return date(d);
}

/** Hours -> "36 h" / "2.5 d" (null -> "—"). */
export function hours(v) {
  const n = toNum(v);
  if (n == null) return DASH;
  if (n >= 72) return `${num(n / 24, 1)} d`;
  // one decimal below a day (19.5 h), dropping a trailing ".0" (20 h); whole hours from 24 h to 72 h
  const r = Math.round(n * 10) / 10;
  return `${num(r, n < 24 && !Number.isInteger(r) ? 1 : 0)} h`;
}

/** Pluralise: plural(3, "file") -> "3 files". */
export const plural = (n, word, many = `${word}s`) => `${int(n)} ${Number(n) === 1 ? word : many}`;

/** Truncate long text with an ellipsis (for SVG labels; use CSS .truncate for HTML). */
export const truncate = (s, max = 24) => {
  const str = String(s ?? "");
  return str.length > max ? `${str.slice(0, max - 1)}…` : str;
};

/** Bytes -> "1.2 MB". */
export function bytes(v) {
  const n = toNum(v);
  if (n == null) return DASH;
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let x = n;
  while (x >= 1024 && i < units.length - 1) {
    x /= 1024;
    i += 1;
  }
  return `${num(x, i === 0 ? 0 : 1)} ${units[i]}`;
}

/** Two-digit category number: catNo(7) -> "07". */
export const catNo = (id) => String(id ?? "").padStart(2, "0");

/** "HH:MM" from an ISO timestamp, or "" when there is no time (or it is exactly midnight, i.e. date-only). */
export function clockTime(ts) {
  const m = /T(\d{2}:\d{2})/.exec(String(ts || ""));
  return m && m[1] !== "00:00" ? m[1] : "";
}
