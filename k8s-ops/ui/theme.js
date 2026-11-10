/* K8S-MCP theme control — Auto / Light / Dark (fleet pattern, wave
   theme-auto-20261105; see .scratch/theme-auto-20261105/THEME-SPEC.md).
   Loaded synchronously in <head>, so the saved choice is applied before
   first paint. It is an external file rather than an inline script because
   the console shell is served with CSP script-src 'self', which blocks
   inline scripts (see src/k8s_mcp/server.py _UI_SECURITY_HEADERS). */
var THEME_KEY = "k8s-mcp-theme";
function applyTheme(t) {
  if (t !== "light" && t !== "dark") t = "auto";
  document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem(THEME_KEY, t); } catch (e) {}
  var btns = document.querySelectorAll("[data-theme-btn]");
  for (var i = 0; i < btns.length; i++) {
    var on = btns[i].getAttribute("data-theme-btn") === t;
    btns[i].classList.toggle("on", on);
    btns[i].setAttribute("aria-pressed", on ? "true" : "false");
  }
}
(function () {
  var t = "auto";
  try { t = localStorage.getItem(THEME_KEY) || "auto"; } catch (e) {}
  if (t !== "light" && t !== "dark") t = "auto";
  document.documentElement.setAttribute("data-theme", t);
  function boot() {
    applyTheme(t);
    var btns = document.querySelectorAll("[data-theme-btn]");
    for (var i = 0; i < btns.length; i++) {
      btns[i].onclick = function () { applyTheme(this.getAttribute("data-theme-btn")); };
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
