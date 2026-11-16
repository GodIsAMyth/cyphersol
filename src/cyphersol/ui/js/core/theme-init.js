/* Classic (non-module) script loaded synchronously in <head> so the saved theme applies before first paint.
 * Must stay tiny and dependency-free. "system" (or nothing saved) leaves data-theme unset so
 * prefers-color-scheme decides. */
(function () {
  try {
    var t = window.localStorage.getItem("cyphersol.theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
    var s = window.localStorage.getItem("cyphersol.sidebar");
    if (s === "collapsed") document.documentElement.setAttribute("data-sidebar-pref", "collapsed");
  } catch (e) {
    /* storage blocked: default theme */
  }
})();
