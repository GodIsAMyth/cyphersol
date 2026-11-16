/* Theme preference: "light" | "dark" | "system". Stamped as <html data-theme> (absent = follow OS). */
import { STORAGE_KEYS, lsGet, lsSet } from "./constants.js";

const listeners = new Set();
const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;

export function getThemePref() {
  const v = lsGet(STORAGE_KEYS.theme, "system");
  return v === "light" || v === "dark" ? v : "system";
}

/** The theme actually rendered right now: "light" | "dark". */
export function effectiveTheme() {
  const attr = document.documentElement.getAttribute("data-theme");
  if (attr === "light" || attr === "dark") return attr;
  return mq && mq.matches ? "dark" : "light";
}

export function setThemePref(pref) {
  const p = pref === "light" || pref === "dark" ? pref : "system";
  if (p === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", p);
  lsSet(STORAGE_KEYS.theme, p === "system" ? null : p);
  emit();
  return p;
}

/** Cycle light -> dark -> system. Returns the new preference. */
export function cycleTheme() {
  const order = ["light", "dark", "system"];
  return setThemePref(order[(order.indexOf(getThemePref()) + 1) % order.length]);
}

/** Subscribe to effective-theme changes (toggle or OS switch). For canvas renderers that cache colours. */
export function onThemeChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function emit() {
  const t = effectiveTheme();
  listeners.forEach((fn) => {
    try {
      fn(t);
    } catch (err) {
      console.error(err);
    }
  });
}

if (mq) {
  const handler = () => {
    if (getThemePref() === "system") emit();
  };
  if (mq.addEventListener) mq.addEventListener("change", handler);
  else if (mq.addListener) mq.addListener(handler);
}
