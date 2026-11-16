/* Dropdown menu (ARIA menu button pattern). Used by the Export menu, the case switcher and view overflow menus.
 *
 * const m = createMenu(buttonEl, {
 *   label: "Export",
 *   align: "end" | "start",
 *   items: () => [                           // array or function (re-evaluated on every open)
 *     { group: "Download" },                 // group label
 *     { id: "xlsx", label: "Excel workbook", description: "…", icon: "download", hint: "⌘E",
 *       disabled: false, checked: undefined, run: () => … },
 *     { separator: true },
 *     { id: "web", label: "Open docs", href: "#/settings" },
 *   ],
 * });
 * m.open(); m.close(); m.destroy();
 * Keyboard: Enter/Space/↓ open + focus first item, ↑/↓/Home/End move, Esc closes and refocuses the button,
 * typing a letter jumps to the next item starting with it.
 */
import { html, render } from "../core/dom.js";
import { icon } from "./icons.js";
import { toast } from "./toast.js";

let openMenu = null;

export function createMenu(button, { items, align = "end", label = "", className = "", onOpen = null } = {}) {
  const menuId = `menu-${Math.random().toString(36).slice(2, 8)}`;
  button.setAttribute("aria-haspopup", "menu");
  button.setAttribute("aria-expanded", "false");
  button.setAttribute("aria-controls", menuId);
  let el = null;
  let currentItems = [];

  function getItems() {
    return typeof items === "function" ? items() || [] : items || [];
  }

  function position() {
    const r = button.getBoundingClientRect();
    const mw = el.offsetWidth;
    const mh = el.offsetHeight;
    let left = align === "end" ? r.right - mw : r.left;
    left = Math.max(8, Math.min(left, window.innerWidth - mw - 8));
    let top = r.bottom + 4;
    if (top + mh > window.innerHeight - 8 && r.top - mh - 4 > 8) top = r.top - mh - 4;
    el.style.left = `${Math.round(left)}px`;
    el.style.top = `${Math.round(top)}px`;
  }

  function itemEls() {
    return el ? Array.from(el.querySelectorAll('[role^="menuitem"]:not([aria-disabled="true"])')) : [];
  }

  function open({ focus = "first" } = {}) {
    if (openMenu && openMenu !== api) openMenu.close({ restore: false });
    if (el) return;
    currentItems = getItems();
    el = document.createElement("div");
    el.className = `menu ${className}`.trim();
    el.id = menuId;
    el.setAttribute("role", "menu");
    if (label) el.setAttribute("aria-label", label);
    render(
      el,
      html`${currentItems.map((it, i) => {
        if (it.separator) return html`<div class="menu__sep" role="separator"></div>`;
        if (it.group) return html`<div class="menu__group-label caps" role="presentation">${it.group}</div>`;
        const role = it.checked === undefined ? "menuitem" : "menuitemradio";
        const inner = html`${it.icon ? icon(it.icon) : ""}
          <span class="menu__text"><span class="truncate">${it.label}</span>${it.description ? html`<span class="menu__desc">${it.description}</span>` : ""}</span>
          ${it.hint ? html`<span class="menu__hint">${it.hint}</span>` : ""}
          ${it.checked ? icon("check", { size: "sm" }) : ""}`;
        return it.href
          ? html`<a class="menu__item" role="${role}" tabindex="-1" href="${it.href}" data-index="${i}" ${it.checked !== undefined ? html`aria-checked="${it.checked ? "true" : "false"}"` : ""}>${inner}</a>`
          : html`<button type="button" class="menu__item" role="${role}" tabindex="-1" data-index="${i}"
              ${it.disabled ? html`aria-disabled="true"` : ""} ${it.checked !== undefined ? html`aria-checked="${it.checked ? "true" : "false"}"` : ""}>${inner}</button>`;
      })}`
    );
    document.body.appendChild(el);
    position();
    button.setAttribute("aria-expanded", "true");
    openMenu = api;
    el.addEventListener("click", onClick);
    el.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", onOutside, true);
    window.addEventListener("resize", onWin);
    window.addEventListener("scroll", onWin, true);
    const list = itemEls();
    const target = focus === "last" ? list[list.length - 1] : list.find((x) => x.getAttribute("aria-checked") === "true") || list[0];
    target?.focus();
    onOpen?.(el);
  }

  function close({ restore = true } = {}) {
    if (!el) return;
    el.removeEventListener("click", onClick);
    el.removeEventListener("keydown", onKey);
    document.removeEventListener("pointerdown", onOutside, true);
    window.removeEventListener("resize", onWin);
    window.removeEventListener("scroll", onWin, true);
    el.remove();
    el = null;
    button.setAttribute("aria-expanded", "false");
    if (openMenu === api) openMenu = null;
    if (restore) button.focus();
  }

  function onWin(e) {
    if (e.type === "scroll" && el && el.contains(e.target)) return;
    close({ restore: false });
  }
  function onOutside(e) {
    if (el && !el.contains(e.target) && !button.contains(e.target)) close({ restore: false });
  }
  async function onClick(e) {
    const itemEl = e.target.closest("[data-index]");
    if (!itemEl || itemEl.getAttribute("aria-disabled") === "true") return;
    const it = currentItems[Number(itemEl.dataset.index)];
    if (it.href) {
      close({ restore: false });
      return; // let the link navigate
    }
    e.preventDefault();
    close();
    try {
      await it.run?.();
    } catch (err) {
      toast.error(err);
    }
  }
  function onKey(e) {
    const list = itemEls();
    const i = list.indexOf(document.activeElement);
    if (e.key === "ArrowDown") {
      e.preventDefault();
      list[(i + 1) % list.length]?.focus();
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      list[(i - 1 + list.length) % list.length]?.focus();
    } else if (e.key === "Home") {
      e.preventDefault();
      list[0]?.focus();
    } else if (e.key === "End") {
      e.preventDefault();
      list[list.length - 1]?.focus();
    } else if (e.key === "Escape") {
      e.preventDefault();
      close();
    } else if (e.key === "Tab") {
      close({ restore: false });
    } else if (e.key === "Enter" || e.key === " ") {
      if (document.activeElement && document.activeElement.tagName === "BUTTON") {
        e.preventDefault();
        document.activeElement.click();
      }
    } else if (e.key.length === 1 && /\S/.test(e.key)) {
      const ch = e.key.toLowerCase();
      const order = [...list.slice(i + 1), ...list.slice(0, i + 1)];
      order.find((x) => x.textContent.trim().toLowerCase().startsWith(ch))?.focus();
    }
  }

  const onButtonClick = () => (el ? close() : open());
  const onButtonKey = (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      open({ focus: e.key === "ArrowUp" ? "last" : "first" });
    }
  };
  button.addEventListener("click", onButtonClick);
  button.addEventListener("keydown", onButtonKey);

  const api = {
    open,
    close,
    get isOpen() {
      return Boolean(el);
    },
    destroy() {
      close({ restore: false });
      button.removeEventListener("click", onButtonClick);
      button.removeEventListener("keydown", onButtonKey);
    },
  };
  return api;
}
