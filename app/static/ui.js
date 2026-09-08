/* ui.js — shared layers for every page: modal, drawer, popover, confirm,
   undo toast, keyboard help, fetch helpers. No framework, no build step. */
(function () {
  "use strict";
  var UI = window.UI = {};

  function el(html) { var t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  UI.el = el; UI.esc = esc;
  UI.money = function (v, d) { v = Number(v) || 0; var s = "$" + Math.abs(v).toLocaleString(undefined, { minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d }); return v < 0 ? "−" + s : s; };

  // ---- fetch (same-origin, marks itself so routes answer JSON) ---------------
  UI.post = function (url, data) {
    var body = data instanceof FormData ? data : new URLSearchParams(data || {});
    return fetch(url, { method: "POST", credentials: "same-origin", body: body, headers: { "X-Requested-With": "fetch", "Accept": "application/json" } })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { d._status = r.status; if (!r.ok && d.ok == null) d.ok = false; return d; }); });
  };
  UI.get = function (url) {
    return fetch(url, { credentials: "same-origin", headers: { "X-Requested-With": "fetch", "Accept": "application/json" } })
      .then(function (r) { return r.json().catch(function () { return {}; }).then(function (d) { d._status = r.status; return d; }); });
  };

  // ---- layers: one stack, Escape closes the top one --------------------------
  var stack = [];
  function open(node, opts) {
    opts = opts || {};
    var bg = el('<div class="layer-bg' + (opts.right ? " right" : "") + '"></div>');
    bg.appendChild(node);
    document.body.appendChild(bg);
    var rec = { bg: bg, node: node, onClose: opts.onClose };
    stack.push(rec);
    bg.addEventListener("mousedown", function (e) { if (e.target === bg && !opts.sticky) close(rec); });
    node.querySelectorAll("[data-close]").forEach(function (b) { b.addEventListener("click", function () { close(rec); }); });
    var f = node.querySelector("[autofocus], input:not([type=hidden]), textarea, button.primary");
    if (f) setTimeout(function () { try { f.focus(); } catch (e) {} }, 30);
    return rec;
  }
  function close(rec) {
    var i = stack.indexOf(rec); if (i < 0) return;
    stack.splice(i, 1); rec.bg.remove();
    if (rec.onClose) rec.onClose();
  }
  UI.closeTop = function () { if (stack.length) close(stack[stack.length - 1]); };
  UI.closeAll = function () { while (stack.length) close(stack[stack.length - 1]); };
  document.addEventListener("keydown", function (e) { if (e.key === "Escape" && stack.length) { e.stopPropagation(); UI.closeTop(); } }, true);

  UI.modal = function (o) {
    // o: {title, body (html|node), footer (html|node), wide, onClose}
    var m = el('<div class="modal' + (o.wide ? " wide" : "") + '" role="dialog"><div class="modal-h"><span></span><button type="button" class="x" data-close title="Close">✕</button></div><div class="modal-b"></div><div class="modal-f"></div></div>');
    m.querySelector(".modal-h span").textContent = o.title || "";
    var b = m.querySelector(".modal-b"), f = m.querySelector(".modal-f");
    if (o.body) { if (typeof o.body === "string") b.innerHTML = o.body; else b.appendChild(o.body); }
    if (o.footer) { if (typeof o.footer === "string") f.innerHTML = o.footer; else f.appendChild(o.footer); } else f.remove();
    var rec = open(m, { onClose: o.onClose, sticky: o.sticky });
    rec.el = m; rec.close = function () { close(rec); };
    return rec;
  };
  UI.drawer = function (o) {
    var d = el('<div class="drawer" role="dialog"><div class="drawer-h"><div style="min-width:0;flex:1;"><b></b><div class="sub"></div></div><button type="button" class="x" data-close title="Close (Esc)">✕</button></div><div class="drawer-b"></div></div>');
    d.querySelector("b").textContent = o.title || "";
    d.querySelector(".sub").innerHTML = o.sub || "";
    var b = d.querySelector(".drawer-b");
    if (o.body) { if (typeof o.body === "string") b.innerHTML = o.body; else b.appendChild(o.body); }
    var rec = open(d, { right: true, onClose: o.onClose });
    rec.el = d; rec.body = b; rec.close = function () { close(rec); };
    rec.setTitle = function (t, s) { d.querySelector("b").textContent = t; if (s != null) d.querySelector(".sub").innerHTML = s; };
    return rec;
  };
  UI.confirm = function (o) {
    // resolves true/false. o: {title, text, ok, danger}
    return new Promise(function (resolve) {
      var done = false;
      var f = el('<div><button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary' + (o.danger ? " danger" : "") + '"></button></div>');
      f.querySelector(".primary").textContent = o.ok || "Confirm";
      var rec = UI.modal({ title: o.title, body: '<div class="muted" style="font-size:13px;line-height:1.5;">' + (o.html ? o.text : esc(o.text || "")) + "</div>", footer: f, onClose: function () { if (!done) { done = true; resolve(false); } } });
      f.querySelector(".primary").addEventListener("click", function () { done = true; rec.close(); resolve(true); });
      f.querySelector(".primary").focus();
    });
  };
  UI.popover = function (anchor, html, o) {
    // small floating panel below/above an element; closes on outside click
    o = o || {};
    var p = el('<div class="pop"></div>'); p.innerHTML = html; document.body.appendChild(p);
    var r = anchor.getBoundingClientRect(), w = p.offsetWidth || 260, h = p.offsetHeight || 120;
    var left = Math.min(Math.max(8, (o.alignRight ? r.right - w : r.left)), window.innerWidth - w - 8);
    var top = r.bottom + 6 + window.scrollY;
    if (r.bottom + h + 12 > window.innerHeight) top = r.top - h - 6 + window.scrollY;
    p.style.left = left + "px"; p.style.top = top + "px";
    function away(e) { if (!p.contains(e.target) && e.target !== anchor && !anchor.contains(e.target)) kill(); }
    function key(e) { if (e.key === "Escape") { kill(); e.stopPropagation(); } }
    function kill() { p.remove(); document.removeEventListener("mousedown", away); document.removeEventListener("keydown", key, true); if (o.onClose) o.onClose(); }
    setTimeout(function () { document.addEventListener("mousedown", away); document.addEventListener("keydown", key, true); }, 0);
    p.close = kill;
    var f = p.querySelector("input, textarea"); if (f) setTimeout(function () { f.focus(); f.select && f.select(); }, 20);
    return p;
  };

  // ---- undo toast: "Paused 3 campaigns  [Undo · 8s]" -------------------------
  var undoCur = null;
  UI.undo = function (text, onUndo, secs) {
    secs = secs || 10;
    if (undoCur) undoCur.kill(true);
    var t = el('<div class="undo-toast"><span class="msg"></span><button type="button" class="btn sm">Undo · ' + secs + 's</button><span class="bar"></span></div>');
    t.querySelector(".msg").textContent = text;
    document.body.appendChild(t);
    var left = secs, tick, bar = t.querySelector(".bar");
    function kill(noCb) { clearInterval(tick); t.remove(); if (undoCur === me) undoCur = null; }
    var me = { kill: kill };
    undoCur = me;
    bar.style.transition = "transform " + secs + "s linear"; requestAnimationFrame(function () { bar.style.transform = "scaleX(0)"; });
    tick = setInterval(function () { left -= 1; t.querySelector("button").textContent = "Undo · " + left + "s"; if (left <= 0) kill(); }, 1000);
    t.querySelector("button").addEventListener("click", function () { kill(); Promise.resolve(onUndo()).catch(function () {}); });
    return me;
  };

  // ---- keyboard help (?) ------------------------------------------------------
  UI.kbdHelp = function (rows) {
    var html = '<div class="kbd-help">' + rows.map(function (r) { return "<span><kbd>" + esc(r[0]) + "</kbd></span><span>" + esc(r[1]) + "</span>"; }).join("") + "</div>";
    UI.modal({ title: "Keyboard shortcuts", body: html });
  };

  // ---- tiny SVG hourly chart (today solid, yesterday ghosted) -----------------
  UI.hourChart = function (host, today, yday, opts) {
    opts = opts || {};
    var w = 400, h = 76, n = 24, pad = 4;
    var all = today.concat(yday), mx = Math.max.apply(null, all.concat([0.0001]));
    function pts(arr) { return arr.map(function (v, i) { return [(i / (n - 1)) * w, h - pad - (v / mx) * (h - pad * 2)]; }); }
    function line(arr) { return "M" + pts(arr).map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" L"); }
    var cut = Math.max(0, Math.min(23, opts.hourNow == null ? 23 : opts.hourNow));
    var tArr = today.slice(0, cut + 1);
    var tp = pts(tArr), area = "M" + tp.map(function (p) { return p[0].toFixed(1) + "," + p[1].toFixed(1); }).join(" L") + " L" + tp[tp.length - 1][0].toFixed(1) + "," + h + " L0," + h + " Z";
    var cs = getComputedStyle(document.documentElement), accent = cs.getPropertyValue("--accent").trim() || "#8b5cf6", dim = cs.getPropertyValue("--faint").trim() || "#666";
    var gid = "hg" + Math.random().toString(36).slice(2, 7);
    host.innerHTML = '<svg class="hchart" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none">' +
      '<defs><linearGradient id="' + gid + '" x1="0" y1="0" x2="0" y2="1"><stop offset="0" stop-color="' + accent + '" stop-opacity=".35"/><stop offset="1" stop-color="' + accent + '" stop-opacity="0"/></linearGradient></defs>' +
      '<path d="' + line(yday) + '" fill="none" stroke="' + dim + '" stroke-width="1.5" stroke-dasharray="4 4" opacity=".8" vector-effect="non-scaling-stroke"/>' +
      '<path d="' + area + '" fill="url(#' + gid + ')"/>' +
      '<path d="' + line(tArr) + '" fill="none" stroke="' + accent + '" stroke-width="2.2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>' +
      '<circle cx="' + tp[tp.length - 1][0].toFixed(1) + '" cy="' + tp[tp.length - 1][1].toFixed(1) + '" r="3" fill="' + accent + '"/></svg>';
  };
})();
