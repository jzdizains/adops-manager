/* AdOps pass-through — put this on the PRELANDER and the LANDER (same script).
 *
 * The ad sends the visitor to your prelander with ?source=<campaign name>
 * (plus ttclid etc.). This script carries those parameters through every hop
 * until they reach Glitchy's offer link, so Glitchy's {source} macro is filled:
 *
 *   ad → prelander?source=X → lander?source=X → glitchy-offer?source=X
 *
 * How it works
 *   1. reads the query string of the current page
 *   2. remembers it in sessionStorage (survives internal navigation / a second
 *      page in the same tab, e.g. a quiz step that drops the query string)
 *   3. rewrites every outbound link, form and late-injected button to carry it
 *      (source / ttclid from the ad always win over a hard-coded value in the link)
 *   4. exposes window.passSource(url) for JS redirects: location.href = passSource(url)
 *
 * Install: <script src="pass-source.js"></script> just before </body> (or inline it).
 * Only same-tab, first-party — nothing is sent anywhere except in the links you already have.
 */
(function () {
  var KEY = "adops_pass", ALWAYS_WIN = { source: 1, ttclid: 1 };

  function parse(qs) {
    var out = {};
    if (!qs) return out;
    qs.replace(/^\?/, "").split("&").forEach(function (kv) {
      if (!kv) return;
      var i = kv.indexOf("="), k = decodeURIComponent(i < 0 ? kv : kv.slice(0, i)).trim();
      var v = i < 0 ? "" : decodeURIComponent(kv.slice(i + 1).replace(/\+/g, " "));
      if (k && v !== "") out[k] = v;
    });
    return out;
  }
  function load() { try { return JSON.parse(sessionStorage.getItem(KEY) || "{}"); } catch (e) { return {}; } }
  function save(p) { try { sessionStorage.setItem(KEY, JSON.stringify(p)); } catch (e) {} }

  // current URL beats what was remembered; remembered fills gaps
  var params = load(), fresh = parse(location.search);
  for (var k in fresh) params[k] = fresh[k];
  save(params);
  if (!Object.keys(params).length) return;

  function withParams(url) {
    if (!url || /^(#|javascript:|mailto:|tel:)/i.test(url)) return url;
    var a = document.createElement("a"); a.href = url;
    var existing = parse(a.search), q = [];
    for (var k in params) {
      if (existing[k] === undefined || ALWAYS_WIN[k]) existing[k] = params[k];
    }
    for (var k2 in existing) q.push(encodeURIComponent(k2) + "=" + encodeURIComponent(existing[k2]));
    a.search = q.length ? "?" + q.join("&") : "";
    return a.href;
  }
  window.passSource = withParams;

  function isOutbound(a) {
    // rewrite links that leave this page's origin, plus anything marked data-pass
    return a.hasAttribute("data-pass") || (a.host && a.host !== location.host);
  }
  function fixLinks(root) {
    (root.querySelectorAll ? root.querySelectorAll("a[href]") : []).forEach(function (a) {
      if (a.dataset.passDone) return;
      if (isOutbound(a)) { a.href = withParams(a.getAttribute("href")); a.dataset.passDone = "1"; }
    });
    (root.querySelectorAll ? root.querySelectorAll("form") : []).forEach(function (f) {
      if (f.dataset.passDone) return;
      f.dataset.passDone = "1";
      var method = (f.getAttribute("method") || "get").toLowerCase();
      if (method === "get") { f.action = withParams(f.action || location.href); return; }
      for (var k in params) {
        if (f.querySelector('[name="' + k + '"]')) continue;
        var i = document.createElement("input"); i.type = "hidden"; i.name = k; i.value = params[k]; f.appendChild(i);
      }
    });
  }
  function run() { fixLinks(document); }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run); else run();
  // buttons/links injected later by the page's own scripts
  if (window.MutationObserver) {
    new MutationObserver(function (muts) {
      muts.forEach(function (m) { m.addedNodes.forEach(function (n) { if (n.nodeType === 1) fixLinks(n.parentNode || n); }); });
    }).observe(document.documentElement, { childList: true, subtree: true });
  }
  // links that get their href set at click time (common with button libraries)
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("a[href]");
    if (a && isOutbound(a) && !a.dataset.passDone) a.href = withParams(a.getAttribute("href"));
  }, true);
})();
