/* asset-builds.js — the Instant Page / Instant Form build queue (v150), shared by the
   Instant Pages and Lead Forms screens.
   AB.pickNeeding({kind, templateId, name}) — accounts grouped by Business Center, each marked
     "has it"; "Select all needing" picks only the gaps; queues the builds.
   AB.mount(el, kind) — the live queue: one line per batch (template), rows with the real step
     and how long it has been on it ("stuck?" after 90 s), retry / cancel, TikTok's preview.
   Depends on ui.js. */
(function () {
  "use strict";
  var esc = UI.esc;
  var AB = window.AB = {};
  var mounts = [];

  AB.pickNeeding = function (o) {
    return UI.get("/builds/targets.json?kind=" + o.kind + "&template_id=" + encodeURIComponent(o.templateId)).then(function (d) {
      if (!d.ok) { UI.confirm({ title: "Can't build", text: d.error || "", ok: "OK" }); return null; }
      var what = o.kind === "page" ? "page" : "form";
      var sel = {};
      var body = UI.el('<div class="abp">' +
        '<div class="abp-top"><input type="search" class="abp-q" placeholder="Search accounts or Business Centers…">' +
        '<button type="button" class="btn sm abp-need">Select all needing a ' + what + ' (' + d.needing + ')</button>' +
        '<button type="button" class="btn sm ghost abp-clear">Clear</button><span class="muted abp-n" style="margin-left:auto;font-size:12px;"></span></div>' +
        '<div class="abp-list"></div>' +
        '<div class="hint">An account counts as having it only when it holds a ' + what + ' named “' + esc(d.name) + '”. Accounts already queued are skipped.</div></div>');
      var foot = UI.el('<div style="display:flex;gap:8px;width:100%;justify-content:flex-end;"><button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary abp-go" disabled>Build</button></div>');
      var list = body.querySelector(".abp-list"), q = "";
      function render() {
        var html = "";
        d.groups.forEach(function (g) {
          var rows = g.accounts.filter(function (a) { return !q || (a.name + " " + a.id + " " + g.bc_name).toLowerCase().indexOf(q) >= 0; });
          if (!rows.length) return;
          var need = rows.filter(function (a) { return !a.has && !a.busy; });
          html += '<div class="abp-bc"><b>' + esc(g.bc_name) + '</b> <span class="muted">' + need.length + ' need' + (need.length === 1 ? 's' : '') + ' it</span>' +
            (need.length ? '<button type="button" class="btn sm ghost abp-bcall" data-bc="' + esc(g.bc_id) + '">Select all (' + need.length + ')</button>' : '') + '</div>';
          rows.forEach(function (a) {
            var dis = a.has || a.busy;
            html += '<label class="abp-row' + (dis ? " dis" : "") + '"><input type="checkbox" value="' + esc(a.id) + '"' + (sel[a.id] ? " checked" : "") + (dis ? " disabled" : "") + '>' +
              '<span class="abp-name">' + esc(a.name) + '</span><span class="mono muted abp-id">' + esc(a.id) + '</span>' +
              (a.has ? '<span class="pill ok">has ' + what + '</span>' : (a.busy ? '<span class="pill warn">queued</span>' : '')) + '</label>';
          });
        });
        list.innerHTML = html || '<div class="empty" style="padding:14px;">No accounts match.</div>';
        count();
      }
      function count() { var n = Object.keys(sel).filter(function (k) { return sel[k]; }).length; body.querySelector(".abp-n").textContent = n + " selected"; var go = foot.querySelector(".abp-go"); go.disabled = !n; go.textContent = "Build on " + n + " account" + (n === 1 ? "" : "s"); }
      function shown(g, a) { return !q || (a.name + " " + a.id + " " + g.bc_name).toLowerCase().indexOf(q) >= 0; }
      function pick(pred) { d.groups.forEach(function (g) { g.accounts.forEach(function (a) { if (!a.has && !a.busy && shown(g, a) && pred(g, a)) sel[a.id] = true; }); }); render(); }   // only what the search shows
      body.querySelector(".abp-q").addEventListener("input", function () { q = this.value.trim().toLowerCase(); render(); });
      body.querySelector(".abp-need").addEventListener("click", function () { pick(function () { return true; }); });
      body.querySelector(".abp-clear").addEventListener("click", function () { sel = {}; render(); });
      list.addEventListener("click", function (e) { var b = e.target.closest(".abp-bcall"); if (b) pick(function (g) { return g.bc_id === b.dataset.bc; }); });
      list.addEventListener("change", function (e) { var c = e.target; if (c.type === "checkbox") { sel[c.value] = c.checked; count(); } });
      var m = UI.modal({ title: "Build “" + d.name + "” on…", body: body, footer: foot, wide: true });
      render();
      return new Promise(function (resolve) {
        foot.querySelector(".abp-go").addEventListener("click", function () {
          var ids = Object.keys(sel).filter(function (k) { return sel[k]; }); var b = this; b.disabled = true;
          UI.post("/builds/queue", { kind: o.kind, template_id: o.templateId, advertiser_ids: ids.join(",") }).then(function (r) {
            b.disabled = false;
            if (!r.ok) { UI.confirm({ title: "Couldn't queue it", text: r.error || "", ok: "OK" }); return; }
            m.close(); AB.refresh(); if (window.adopsToast) adopsToast("ok", r.message); resolve(r);
          });
        });
      });
    });
  };

  function fmt(s) { return s < 60 ? s + "s" : Math.floor(s / 60) + "m " + (s % 60) + "s"; }
  var PILL = { pending: "dim", running: "warn", success: "ok", failed: "err", cancelled: "dim" };
  var LABEL = { pending: "waiting", running: "building", success: "built", failed: "failed", cancelled: "cancelled" };

  function draw(M, d) {
    var open = M.open;
    if (!d.batches.length) { M.el.innerHTML = '<div class="muted" style="font-size:12.5px;padding:6px 0;">No builds yet — use “Build on…” on a template.</div>'; return; }
    M.el.innerHTML = d.batches.slice(0, 12).map(function (b) {
      var c = b.counts, isOpen = open[b.batch] !== undefined ? open[b.batch] : b.active;
      var head = '<div class="abq-h" data-batch="' + esc(b.batch) + '"><span class="abq-tw">' + (isOpen ? "▾" : "▸") + '</span><b>' + esc(b.name) + '</b> <span class="muted">' + esc(b.created) + '</span>' +
        '<span class="abq-c">' + (c.running ? '<span class="pill warn">' + c.running + ' building</span>' : '') + (c.pending ? '<span class="pill dim">' + c.pending + ' waiting</span>' : '') +
        (c.success ? '<span class="pill ok">' + c.success + ' built</span>' : '') + (c.failed ? '<span class="pill err">' + c.failed + ' failed</span>' : '') +
        (c.failed ? '<button type="button" class="btn sm ghost abq-rf" data-batch="' + esc(b.batch) + '">Retry failed</button>' : '') + '</span></div>' +
        (b.last_error && !isOpen ? '<div class="abq-last">Last failure: ' + esc(b.last_error.slice(0, 180)) + '</div>' : '');
      if (!isOpen) return '<div class="abq-b">' + head + '</div>';
      var rows = b.rows.map(function (r) {
        var status = '<span class="pill ' + PILL[r.status] + '">' + LABEL[r.status] + '</span>';
        var detail = r.status === "running" ? '<span class="' + (r.stuck ? "abq-stuck" : "") + '">' + esc(r.step) + ' · ' + fmt(r.secs) + (r.stuck ? ' · stuck?' : '') + '</span>'
          : (r.status === "failed" ? '<span class="abq-err">' + esc(r.error) + '</span>' : (r.status === "pending" ? '<span class="muted">' + esc(r.step) + '</span>' : '<span class="muted">' + esc(r.method) + '</span>'));
        var act = r.status === "success" && r.preview ? '<a class="btn sm ghost" href="' + esc(r.preview) + '" target="_blank" rel="noopener">Preview ↗</a>' : "";
        if (r.status === "failed" || r.status === "cancelled") act += '<button type="button" class="btn sm abq-retry" data-id="' + r.id + '">Retry</button>';
        if (r.status === "pending") act += '<button type="button" class="btn sm ghost abq-cancel" data-id="' + r.id + '">Cancel</button>';
        return '<tr><td>' + esc(r.account) + '<div class="mono muted" style="font-size:10.5px;">' + esc(r.advertiser_id) + '</div></td><td>' + status + '</td><td class="abq-d">' + detail + '</td>' +
          '<td class="mono" style="font-size:11px;">' + esc(r.result_id || "—") + '</td><td class="muted" style="white-space:nowrap;font-size:11px;">' + esc(r.at) + '</td><td class="num" style="white-space:nowrap;">' + act + '</td></tr>';
      }).join("");
      return '<div class="abq-b">' + head + '<div class="table-scroll"><table class="data dense abq-t"><thead><tr><th>Account</th><th>Status</th><th>Step / result</th><th>ID</th><th>When</th><th></th></tr></thead><tbody>' + rows + '</tbody></table></div></div>';
    }).join("");
  }

  function load(M) {
    clearTimeout(M.timer);
    if (document.hidden) { M.wantLoad = true; return Promise.resolve(); }   // resumes on visibilitychange
    return UI.get("/builds.json?kind=" + M.kind).then(function (d) {
      if (!d.ok) throw new Error("bad");
      M.fails = 0; M.active = !!d.active;
      draw(M, d);
      if (d.active) M.timer = setTimeout(function () { load(M); }, 3000);   // live only while something runs
    }).catch(function () {
      // a blip (deploy, network) never ends the live view: back off and try again while it was live
      M.fails = (M.fails || 0) + 1;
      if (M.active !== false) M.timer = setTimeout(function () { load(M); }, Math.min(30000, 3000 * Math.pow(2, M.fails)));
    });
  }
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) mounts.forEach(function (M) { if (M.wantLoad || M.active) { M.wantLoad = false; load(M); } });
  });

  AB.mount = function (el, kind) {
    if (!el) return;
    var M = { el: el, kind: kind, open: {}, timer: null };
    mounts.push(M);
    el.addEventListener("click", function (e) {
      var h = e.target.closest(".abq-h"), rt = e.target.closest(".abq-retry"), cn = e.target.closest(".abq-cancel"), rf = e.target.closest(".abq-rf");
      if (rt) { UI.post("/builds/" + rt.dataset.id + "/retry").then(function () { load(M); }); return; }
      if (cn) { UI.post("/builds/" + cn.dataset.id + "/cancel").then(function () { load(M); }); return; }
      if (rf) { e.stopPropagation(); UI.post("/builds/batch/" + encodeURIComponent(rf.dataset.batch) + "/retry-failed").then(function () { load(M); }); return; }
      if (h) { var cur = h.querySelector(".abq-tw").textContent === "▾"; M.open[h.dataset.batch] = !cur; load(M); }
    });
    load(M);
  };
  AB.refresh = function () { mounts.forEach(load); };
})();
