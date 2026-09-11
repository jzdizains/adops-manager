/* Pass-through + click tracking — put this on the PRELANDER and the LANDER (same script).
 *
 * The ad lands on your prelander with ?source=<campaign name>&tt_cid=…&tt_aid=…&tt_ad=…
 * (TikTok appends its own ttclid). This script:
 *   1. registers the click with the dashboard (POST /t/click) and gets a short click id —
 *      the same thing RedTrack / ClickFlare do with their "direct tracking" script
 *   2. remembers everything in sessionStorage (survives a quiz step that drops the query)
 *   3. rewrites every outbound link, form and late-injected button so the click into
 *      Glitchy's offer link carries  source=<campaign name>~<click id>
 *      → Glitchy echoes {source} back in its postback → the dashboard resolves the click id
 *        to the TikTok click (ttclid, ip, user agent, ad ids) for the Events API + P&L.
 *   4. exposes window.passSource(url) for JS redirects: location.href = passSource(url)
 *
 * Only a 12-character id has to survive the network round trip. If the dashboard can't be
 * reached, the script falls back to packing the raw ttclid behind the source (older behaviour).
 *
 * TRACK_HOST is filled in by Settings → Tracking (or set window.ADOPS_TRACK before this script).
 * Install: <script src="pass-source.js"></script> just before </body> (or inline it).
 */
(function () {
  var KEY = "adops_pass", ALWAYS_WIN = { source: 1, ttclid: 1, clid: 1 }, SEP = "~";
  // extra names the offer link should carry the same value under (a network that reads sub1/s1
  // instead of source). Filled in by Settings → Tracking, or window.ADOPS_EXTRA before this script.
  var EXTRA = (window.ADOPS_EXTRA || "__ADOPS_EXTRA_PARAMS__");
  EXTRA = /__ADOPS/.test(EXTRA) ? [] : String(EXTRA).split(",").map(function (x) { return x.trim(); }).filter(Boolean);
  EXTRA.forEach(function (n) { ALWAYS_WIN[n] = 1; });
  var TRACK_HOST = window.ADOPS_TRACK || "__ADOPS_TRACK_HOST__";
  if (/__ADOPS/.test(TRACK_HOST)) { try { var cs = document.currentScript; TRACK_HOST = cs && /^https?:/.test(cs.src) ? cs.src.replace(/\/static\/.*$/, "") : ""; } catch (e) { TRACK_HOST = ""; } }

  // source=X~T  →  source=X plus the packed part: our click id (clid) or, from an older hop, a ttclid.
  // Safe to run on every hop, so a page that already received a packed source is fine.
  function unpack(p) {
    var s = p.source, i = s ? s.indexOf(SEP) : -1;
    if (i < 0) return p;
    var t = s.slice(i + 1);
    p.source = s.slice(0, i);
    if (t) { if (/^[a-z0-9]{12}$/.test(t)) { if (!p.clid) p.clid = t; } else if (!p.ttclid) p.ttclid = t; }
    if (!p.source) delete p.source;
    return p;
  }
  // what goes into outbound links: the source with the click id packed in (ttclid until we have one)
  function outgoing(p) {
    var o = {};
    for (var k in p) if (k !== "tt_cid" && k !== "tt_aid" && k !== "tt_ad") o[k] = p[k];
    if (o.source && (o.clid || o.ttclid)) o.source = o.source + SEP + (o.clid || o.ttclid);
    if (o.source) EXTRA.forEach(function (n) { o[n] = o.source; });
    return o;
  }

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
  var params = unpack(load()), fresh = unpack(parse(location.search));
  // a NEW ad click (fresh source, no click id) must not inherit an old click / ttclid
  if (fresh.source && !fresh.ttclid && !fresh.clid) { delete params.ttclid; delete params.clid; }
  if (fresh.ttclid && fresh.ttclid !== params.ttclid && !fresh.clid) delete params.clid;
  for (var k in fresh) params[k] = fresh[k];
  save(params);
  if (!Object.keys(params).length) return;
  var out = outgoing(params);
  // Pages often want the values themselves — the TikTok pixel's identify() needs ttclid, for
  // instance. Expose them (and keep them fresh once the click id arrives) rather than making
  // every page re-parse the URL: window.adopsParams.ttclid / .source / .clid
  function expose() { try { window.adopsParams = params; } catch (e) {} }
  expose();

  // register the click once per ad click (a new ttclid / source) → short click id
  function register() {
    if (params.clid || !TRACK_HOST || !(params.source || params.ttclid) || !window.fetch) return;
    var body = JSON.stringify({ source: params.source || "", ttclid: params.ttclid || "", tt_cid: params.tt_cid || "", tt_aid: params.tt_aid || "", tt_ad: params.tt_ad || "", url: location.href.slice(0, 900), ref: document.referrer.slice(0, 400) });
    fetch(TRACK_HOST + "/t/click", { method: "POST", mode: "cors", credentials: "omit", keepalive: true, headers: { "Content-Type": "text/plain" }, body: body })
      .then(function (r) { return r.json(); })
      .then(function (d) { if (d && d.click_id) { params.clid = d.click_id; save(params); out = outgoing(params); expose(); refresh(); try { document.dispatchEvent(new CustomEvent("adops:click", { detail: params })); } catch (e) {} } })
      .catch(function () {});   // unreachable → links keep the raw ttclid packed (still attributable)
  }

  function withParams(url) {
    if (!url || /^(#|javascript:|mailto:|tel:)/i.test(url)) return url;
    var a = document.createElement("a"); a.href = url;
    var existing = parse(a.search), q = [];
    for (var k in out) {
      if (existing[k] === undefined || ALWAYS_WIN[k]) existing[k] = out[k];
    }
    for (var k2 in existing) q.push(encodeURIComponent(k2) + "=" + encodeURIComponent(existing[k2]));
    a.search = q.length ? "?" + q.join("&") : "";
    return a.href;
  }
  window.passSource = withParams;

  // JS-driven navigation: page builders (Lovable, Webflow, Framer…) wire buttons
  // to window.open(url) with a hard-coded URL — no <a> for us to rewrite, so the
  // source would be lost right at the offer. Wrap it so the URL gets the params.
  var _open = window.open;
  if (typeof _open === "function") {
    window.open = function (url, target, features) {
      try { if (url) url = withParams(String(url)); } catch (e) {}
      return _open.call(window, url, target, features);
    };
  }

  function isOutbound(a) {
    // EVERY http(s) link gets the parameters — not just the ones leaving this host. A prelander
    // very often exits through a link on its OWN domain (/go, /out, a page-builder route) that
    // redirects to the offer server-side; those used to lose the source completely. Opt a link
    // out with data-pass="off".
    if (a.getAttribute("data-pass") === "off") return false;
    if (a.hasAttribute("data-pass")) return true;
    var href = a.getAttribute("href") || "";
    if (/^(#|javascript:|mailto:|tel:|sms:)/i.test(href)) return false;
    return a.protocol === "http:" || a.protocol === "https:";
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
      for (var k in out) {
        if (f.querySelector('[name="' + k + '"]')) continue;
        var i = document.createElement("input"); i.type = "hidden"; i.name = k; i.value = out[k]; i.dataset.passAdded = "1"; f.appendChild(i);
      }
    });
  }
  function refresh() {
    (document.querySelectorAll ? document.querySelectorAll("a[data-pass-done], form[data-pass-done]") : []).forEach(function (el) { el.removeAttribute("data-pass-done"); el.querySelectorAll && el.querySelectorAll("input[data-pass-added]").forEach(function (i) { i.remove(); }); });
    fixLinks(document);
  }
  function run() { fixLinks(document); register(); }
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
