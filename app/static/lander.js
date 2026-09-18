/* lander.js — the shared runtime every lander built by the dashboard carries (inlined at build).
 *
 * One small object, window.L:
 *   L.env      { os: ios|android|other, inapp: tiktok|instagram|facebook|snapchat|"", ua }
 *   L.cfg      the lander's config: baked into the page at build, then refreshed from the
 *              dashboard (/t/l/<slug>.json, 1.5 s budget) so offers/rules change without re-uploading
 *   L.params   what the ad sent: source, ttclid, tt_cid/tt_aid/tt_ad, clid — plus vid (stable visitor id)
 *   L.ready(fn) runs fn once the live config answered (or the budget ran out)
 *   L.track(step, extra)   one sendBeacon to /t/lp — the lander funnel (view / engaged / continue / …)
 *   L.pixel(event, props)  ttq.track only for REAL actions (never on paint, never on any tap)
 *   L.route()  the URL the rules pick for THIS visitor (os / in-app / age / country / hour), else cfg.next
 *   L.go(url)  leave, carrying the source + click id (pass-source.js when present)
 *   L.escape(url, method) try to open url in the phone's real browser (the escape test's methods);
 *              if the page is still visible after 1.2 s, continue in-app to the same url — never a fake error
 *   L.age(...)  helpers for an age step: L.age.bracket(year) → "u18" | "18_24" | "25_34" | "35_49" | "50p"
 *
 * Honest by design: detection adapts the page, it never cloaks; every visitor gets the same destination
 * rules; nothing is stored beyond the visitor id and the ids the ad already sent.
 */
(function (w, d) {
  "use strict";
  var L = w.L = w.L || {};
  var ua = (w.navigator && navigator.userAgent) || "";
  var cfg = w.LANDER_CFG || {};
  var HOST = cfg.track_host || "";

  // ---- environment ----------------------------------------------------------------
  function env(u) {
    u = u == null ? ua : u;
    var os = /iPhone|iPad|iPod/i.test(u) ? "ios" : (/Android/i.test(u) ? "android" : "other");
    var inapp = /TikTok|musical_ly|Bytedance|BytedanceWebview|ByteLocale|trill/i.test(u) ? "tiktok"
      : (/Instagram/i.test(u) ? "instagram" : (/FBAN|FBAV|FB_IAB/i.test(u) ? "facebook" : (/Snapchat/i.test(u) ? "snapchat" : "")));
    var m = /app_version\/(\d+(?:\.\d+)+)/i.exec(u) || /(?:musical_ly|TikTok|trill)[_/ ]?v?(\d+(?:\.\d+)+)/i.exec(u);
    return { os: os, inapp: inapp, app_version: m ? m[1] : "", ua: u };
  }
  L.detect = env;
  L.env = env();

  // ---- params + visitor id ----------------------------------------------------------
  function qs() {
    var out = {};
    try { new URLSearchParams(location.search).forEach(function (v, k) { out[k] = v; }); } catch (e) {}
    // source=X~T (a packed click id from /t/c or an earlier hop)
    if (out.source && out.source.indexOf("~") >= 0) { var i = out.source.indexOf("~"), t = out.source.slice(i + 1); out.source = out.source.slice(0, i); if (/^[a-z0-9]{12}$/.test(t) && !out.clid) out.clid = t; else if (t && !out.ttclid) out.ttclid = t; }
    return out;
  }
  function store(k, v) { try { if (v == null) return localStorage.getItem(k) || sessionStorage.getItem(k) || ""; localStorage.setItem(k, v); sessionStorage.setItem(k, v); } catch (e) {} return v; }
  function vid() {
    var p = qs(), v = p.svid || store("tmp_vid");
    if (!v) { v = "v" + Math.random().toString(36).slice(2, 12) + Date.now().toString(36); }
    return store("tmp_vid", v);
  }
  L.params = qs();
  L.params.vid = vid();
  ["source", "ttclid", "tt_cid", "tt_aid", "tt_ad", "clid"].forEach(function (k) { if (L.params[k]) store("lp_" + k, L.params[k]); else { var s = store("lp_" + k); if (s) L.params[k] = s; } });

  // ---- config: baked, then live ---------------------------------------------------------
  var readyFns = [], isReady = false, liveDone = false;
  function fire() { if (isReady) return; isReady = true; readyFns.forEach(function (f) { try { f(L.cfg); } catch (e) {} }); readyFns = []; }
  L.cfg = cfg;
  L.ready = function (fn) { if (isReady) { try { fn(L.cfg); } catch (e) {} } else readyFns.push(fn); };
  (function live() {
    if (!HOST || !cfg.slug || !w.fetch) { fire(); return; }
    var t = setTimeout(function () { if (!liveDone) { liveDone = true; L.cfg.live = false; fire(); } }, cfg.live_budget_ms || 1500);
    fetch(HOST + "/t/l/" + encodeURIComponent(cfg.slug) + ".json", { mode: "cors", credentials: "omit", cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (liveDone) return; liveDone = true; clearTimeout(t);
        if (j && j.ok && j.cfg) { var keep = L.cfg; L.cfg = j.cfg; L.cfg.track_host = keep.track_host || j.cfg.track_host; L.cfg.slug = keep.slug; L.cfg.live = true; L.cfg.country = j.country || ""; }
        else L.cfg.live = false;
        fire();
      }).catch(function () { if (!liveDone) { liveDone = true; clearTimeout(t); L.cfg.live = false; fire(); } });
  })();

  // ---- funnel beacon ------------------------------------------------------------------
  var sent = {};
  L.track = function (step, extra) {
    if (!HOST || !cfg.slug || sent[step]) return false;
    sent[step] = true;
    var body = { page: cfg.slug, step: step, source: L.params.source || "", vid: L.params.vid, ttclid: L.params.ttclid || L.params.clid || "", cid: L.params.tt_cid || "", inapp: L.env.inapp || "", os: L.env.os, url: String(location.href || "").slice(0, 900), ref: (d.referrer || "").slice(0, 400) };
    if (extra && typeof extra === "object") { if (extra.via) body.via = String(extra.via); if (extra.bucket) body.bucket = String(extra.bucket).slice(0, 40); }
    try {
      var s = JSON.stringify(body);
      if (navigator.sendBeacon) return navigator.sendBeacon(HOST + "/t/lp", new Blob([s], { type: "text/plain" }));
      fetch(HOST + "/t/lp", { method: "POST", body: s, keepalive: true, headers: { "Content-Type": "text/plain" } }).catch(function () {});
    } catch (e) {}
    return true;
  };
  // "engaged" = a person: visible for a few seconds, or touched / scrolled
  (function engaged() {
    var done = false, mark = function () { if (done) return; done = true; L.track("engaged"); };
    ["touchstart", "scroll", "keydown", "pointerdown"].forEach(function (ev) { w.addEventListener(ev, mark, { passive: true, once: true }); });
    var t0 = Date.now(), timer = setInterval(function () { if (d.visibilityState !== "hidden" && Date.now() - t0 >= 3000) { clearInterval(timer); mark(); } }, 500);
  })();
  L.track("view", { via: L.params.via || "" });

  // ---- pixel: real actions only ----------------------------------------------------------
  L.pixel = function (event, props) {
    try { if (w.ttq && typeof w.ttq.track === "function" && event) { w.ttq.track(event, props || {}); return true; } } catch (e) {}
    return false;
  };

  // ---- age helpers --------------------------------------------------------------------------
  L.age = {
    years: function (year) { var y = parseInt(year, 10); if (!y) return 0; var now = new Date().getFullYear(); return y > 1900 && y <= now ? now - y : 0; },
    bracket: function (year) { var a = L.age.years(year); if (!a) return ""; return a < 18 ? "u18" : (a < 25 ? "18_24" : (a < 35 ? "25_34" : (a < 50 ? "35_49" : "50p"))); }
  };

  // ---- routing: rules → url -------------------------------------------------------------------
  // rule = { when: { os: "ios"|"android"|"other"|"", inapp: "yes"|"no"|"", age: "u18,18_24,…"|"", country: "US,GB"|"", hours: "9-17"|"" }, to: url }
  function inList(list, v) { list = String(list || "").split(",").map(function (x) { return x.trim().toLowerCase(); }).filter(Boolean); return !list.length || list.indexOf(String(v || "").toLowerCase()) >= 0; }
  function hourOk(spec, h) { spec = String(spec || "").trim(); if (!spec) return true; var m = /^(\d{1,2})\s*-\s*(\d{1,2})$/.exec(spec); if (!m) return true; var a = +m[1], b = +m[2]; return a <= b ? (h >= a && h < b) : (h >= a || h < b); }
  L.match = function (rule, ctx) {
    var wn = (rule && rule.when) || {};
    if (wn.os && wn.os !== "any" && !inList(wn.os, ctx.os)) return false;
    if (wn.inapp === "yes" && !ctx.inapp) return false;
    if (wn.inapp === "no" && ctx.inapp) return false;
    if (wn.age && !inList(wn.age, ctx.age)) return false;
    if (wn.country && !inList(wn.country, ctx.country)) return false;
    if (wn.hours && !hourOk(wn.hours, ctx.hour)) return false;
    return !!rule.to;
  };
  L.context = function (extra) {
    var c = { os: L.env.os, inapp: L.env.inapp, country: (L.cfg && L.cfg.country) || "", hour: new Date().getHours(), age: store("lp_age") || "" };
    if (extra) for (var k in extra) if (Object.prototype.hasOwnProperty.call(extra, k)) c[k] = extra[k];
    return c;
  };
  L.route = function (extra) {
    var rules = (L.cfg && L.cfg.rules) || [], ctx = L.context(extra);
    for (var i = 0; i < rules.length; i++) if (L.match(rules[i], ctx)) { L.track("route", { bucket: rules[i].name || ("rule" + (i + 1)) }); return rules[i].to; }
    return (L.cfg && L.cfg.next) || "";
  };
  L.setAge = function (year) { var b = L.age.bracket(year); if (b) { store("lp_age", b); L.track("gate", { bucket: b }); } return b; };

  // ---- leaving: carry the ids ------------------------------------------------------------
  L.withParams = function (url) {
    if (!url) return url;
    if (typeof w.passSource === "function") { try { return w.passSource(url); } catch (e) {} }
    var p = L.params, add = {};
    if (p.source) add.source = p.source + (p.clid ? "~" + p.clid : (p.ttclid ? "~" + p.ttclid : ""));
    ["ttclid", "tt_cid", "tt_aid", "tt_ad", "clid"].forEach(function (k) { if (p[k]) add[k] = p[k]; });
    add.svid = p.vid; add.via = "continue";
    var q = Object.keys(add).map(function (k) { return encodeURIComponent(k) + "=" + encodeURIComponent(add[k]); }).join("&");
    if (!q) return url;
    var h = url.indexOf("#"), hash = h >= 0 ? url.slice(h) : "", base = h >= 0 ? url.slice(0, h) : url;
    return base + (base.indexOf("?") >= 0 ? "&" : "?") + q + hash;
  };
  L.go = function (url) { url = L.withParams(url); if (url) location.href = url; return url; };

  // ---- escape the in-app browser (the escape test's methods) ----------------------------------------
  L.escapeUrl = function (method, target) {
    var bare = target.replace(/^https?:\/\//, "");
    switch (method) {
      case "chrome-intent": case "intent-open": return "intent://" + bare + "#Intent;scheme=https;package=com.android.chrome;S.browser_fallback_url=" + encodeURIComponent(target) + ";end";
      case "intent-default": return "intent:" + target + "#Intent;end";
      case "intent-browsable": return "intent://" + bare + "#Intent;scheme=https;action=android.intent.action.VIEW;category=android.intent.category.BROWSABLE;S.browser_fallback_url=" + encodeURIComponent(target) + ";end";
      case "chrome-scheme": return "googlechrome://" + bare;
      case "x-safari": case "x-safari-open": return "x-safari-" + target;
      case "chrome-ios": return "googlechromes://" + bare;
      case "firefox-ios": return "firefox://open-url?url=" + encodeURIComponent(target);
      case "shortcuts": return "shortcuts://x-callback-url/run-shortcut?name=" + encodeURIComponent("open") + "&x-error=" + encodeURIComponent(target);
      default: return "";
    }
  };
  L.escapeMethod = function () {
    var m = (L.cfg && L.cfg.escape) || {};
    if (!L.env.inapp) return "direct";
    if (L.env.os === "android") return m.android || "chrome-intent";
    if (L.env.os === "ios") return m.ios || "x-safari";
    return "direct";
  };
  L.escape = function (target, method) {
    target = L.withParams(target);
    method = method || L.escapeMethod();
    var esc = method === "direct" ? "" : L.escapeUrl(method, target);
    L.track("continue", { bucket: method });
    if (!esc) { location.href = target; return "direct"; }
    var left = false, gone = function () { left = true; };
    d.addEventListener("visibilitychange", function () { if (d.visibilityState === "hidden") gone(); });
    w.addEventListener("pagehide", gone); w.addEventListener("blur", gone);
    if (method === "intent-open" || method === "x-safari-open") { try { w.open(esc, "_blank"); } catch (e) { location.href = esc; } }
    else location.href = esc;
    setTimeout(function () {
      if (!left && d.visibilityState !== "hidden") { L.track("escape_miss", { bucket: method }); location.href = target; }   // still here: carry on in-app, same page
      else L.track("escaped", { bucket: method });
    }, cfg.escape_wait_ms || 1200);
    return method;
  };
})(window, document);
