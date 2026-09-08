/* campaigns.js — the Campaigns console: filters, column chooser, group rows,
   inline on/off with undo, selection + bulk actions, row drawer (metrics,
   hourly chart, budget/bid, note, timeline), keyboard shortcuts, in-place
   refresh. Depends on ui.js. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var state = {}; try { state = JSON.parse($("#pageState").textContent || "{}"); } catch (e) {}
  var esc = UI.esc, money = UI.money;

  // ---- filters: chip dropdowns + segmented state/group -----------------------
  var form = $("#filterForm");
  function submit() { form.submit(); }
  $$("[data-fsel]").forEach(function (fs) {
    var btn = fs.querySelector("button"), input = fs.querySelector("input[type=hidden]");
    btn.addEventListener("click", function (e) { e.stopPropagation(); var open = fs.classList.contains("open"); $$("[data-fsel].open").forEach(function (o) { o.classList.remove("open"); }); if (!open) fs.classList.add("open"); });
    fs.querySelectorAll(".fmenu a").forEach(function (a) {
      a.addEventListener("click", function (e) {
        e.preventDefault(); var val = a.getAttribute("data-val"); input.value = val; fs.querySelector(".v").textContent = a.textContent.trim(); fs.classList.remove("open");
        if (input.name === "range" && val === "custom") { $("#customDates").style.display = "inline-flex"; return; }
        if (input.name === "range") $("#customDates").style.display = "none";
        submit();
      });
    });
  });
  document.addEventListener("click", function () { $$("[data-fsel].open").forEach(function (o) { o.classList.remove("open"); }); });
  $$("#stateSeg button").forEach(function (b) { b.addEventListener("click", function () { form.querySelector("[name=state]").value = b.dataset.state; submit(); }); });
  $$("#groupSeg button").forEach(function (b) { b.addEventListener("click", function () { form.querySelector("[name=group]").value = b.dataset.group; submit(); }); });
  form.addEventListener("keydown", function (e) { if (e.key === "Enter" && e.target.name === "q") { e.preventDefault(); submit(); } });

  // ---- columns: remembered per browser --------------------------------------
  var COLS = [["spend", "Spend"], ["revenue", "Revenue"], ["profit", "Profit"], ["roas", "ROAS"], ["epc", "EPC"], ["cpa", "CPA"], ["conv", "Conv"], ["impr", "Impressions + pace"], ["clicks", "Clicks"],
              ["ctr", "CTR"], ["cpc", "CPC"], ["cpm", "CPM"], ["cvr", "CVR (postback)"], ["pbclicks", "PB clicks"], ["source", "Source"], ["budget", "Budget"], ["trend", "Last 12h"]];
  var DEFAULT_HIDDEN = { ctr: 1, cpc: 1, cpm: 1, cvr: 1, pbclicks: 1, source: 1 };
  function colsSaved() { try { var v = JSON.parse(localStorage.getItem("adops-camp-cols2") || "null"); if (v && typeof v === "object") return v; } catch (e) {} return Object.assign({}, DEFAULT_HIDDEN); }
  var hidden = colsSaved();
  function applyCols() {
    COLS.forEach(function (c) { $$(".c-" + c[0]).forEach(function (el) { el.classList.toggle("col-hide", !!hidden[c[0]]); }); });
  }
  applyCols();
  $("#colsBtn").addEventListener("click", function () {
    var html = '<div style="font-weight:700;margin-bottom:8px;">Columns</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 14px;">' +
      COLS.map(function (c) { return '<label style="display:flex;gap:8px;align-items:center;font-size:12.5px;cursor:pointer;"><input type="checkbox" data-col="' + c[0] + '"' + (hidden[c[0]] ? "" : " checked") + ' style="accent-color:var(--accent);">' + esc(c[1]) + "</label>"; }).join("") +
      '</div><div style="margin-top:10px;display:flex;gap:6px;"><button type="button" class="btn sm" data-preset="money">Money only</button><button type="button" class="btn sm" data-preset="all">Everything</button><button type="button" class="btn sm" data-preset="default">Default</button></div>';
    var p = UI.popover($("#colsBtn"), html, { alignRight: true });
    p.addEventListener("change", function (e) { if (e.target.dataset.col) { hidden[e.target.dataset.col] = !e.target.checked; save(); } });
    p.addEventListener("click", function (e) {
      var b = e.target.closest("[data-preset]"); if (!b) return;
      if (b.dataset.preset === "money") { hidden = {}; COLS.forEach(function (c) { if (["impr", "clicks", "ctr", "cpc", "cpm", "cvr", "pbclicks", "source", "trend"].indexOf(c[0]) >= 0) hidden[c[0]] = 1; }); }
      else if (b.dataset.preset === "all") hidden = {};
      else hidden = Object.assign({}, DEFAULT_HIDDEN);
      save(); p.close(); $("#colsBtn").click();
    });
    function save() { try { localStorage.setItem("adops-camp-cols2", JSON.stringify(hidden)); } catch (e) {} applyCols(); }
  });

  // ---- group rows: expand / collapse (remembered while on the page) ---------
  var openGroups = {};
  function applyGroups() {
    $$(".grp-row").forEach(function (g) {
      var on = !!openGroups[g.dataset.gkey];
      g.classList.toggle("open", on);
      $$('.grp-child[data-grp="' + CSS.escape(g.dataset.gkey) + '"]').forEach(function (r) { r.hidden = !on; });
    });
  }
  document.addEventListener("click", function (e) {
    var g = e.target.closest && e.target.closest(".grp-row");
    if (!g || e.target.closest("input, .previewBtn, a")) return;
    openGroups[g.dataset.gkey] = !openGroups[g.dataset.gkey]; applyGroups();
  });

  // ---- selection + bulk bar ----------------------------------------------------
  function selectedRows() { return $$(".camp-row").filter(function (r) { return r.querySelector(".rowchk").checked; }); }
  function syncBar() {
    var n = selectedRows().length, bar = $("#selBar");
    bar.hidden = n === 0; $("#selCount").textContent = n + " selected";
    $$(".camp-row").forEach(function (r) { r.classList.toggle("rowsel", r.querySelector(".rowchk").checked); });
  }
  document.addEventListener("change", function (e) {
    if (e.target.classList.contains("rowchk")) syncBar();
    if (e.target.classList.contains("grpchk")) { var g = e.target.closest(".grp-row"); $$('.grp-child[data-grp="' + CSS.escape(g.dataset.gkey) + '"] .rowchk').forEach(function (c) { c.checked = e.target.checked; }); syncBar(); }
    if (e.target.id === "selAll") { $$(".camp-row .rowchk, .grpchk").forEach(function (c) { c.checked = e.target.checked; }); syncBar(); }
  });

  // ---- pause / resume with undo --------------------------------------------------
  function setStatus(row, op) {
    var tg = row.querySelector("[data-toggle]"); tg.classList.add("busy");
    return UI.post("/campaigns/" + row.dataset.adv + "/" + row.dataset.cid + "/status", { operation_status: op, next: "/status" })
      .then(function (d) {
        tg.classList.remove("busy");
        if (!d.ok) { window.adopsToast && adopsToast("err", d.error || "TikTok refused"); return false; }
        row.dataset.status = op; tg.classList.toggle("on", op === "ENABLE"); tg.title = (op === "ENABLE" ? "Pause" : "Resume") + " on TikTok";
        return true;
      });
  }
  function toggleRows(rows, op, why) {
    if (!rows.length) return;
    var label = (op === "DISABLE" ? "Paused " : "Resumed ") + (rows.length === 1 ? rows[0].dataset.name : rows.length + " campaigns");
    var prev = rows.map(function (r) { return r.dataset.status; });
    Promise.all(rows.map(function (r) { return setStatus(r, op); })).then(function (oks) {
      var done = rows.filter(function (r, i) { return oks[i]; });
      if (!done.length) return;
      UI.undo(label + (done.length < rows.length ? " (" + (rows.length - done.length) + " failed)" : ""), function () {
        return Promise.all(done.map(function (r, i) { return setStatus(r, prev[rows.indexOf(r)]); })).then(function () { adopsToast && adopsToast("ok", "Undone"); });
      });
    });
  }
  document.addEventListener("click", function (e) {
    var tg = e.target.closest && e.target.closest("[data-toggle]");
    if (!tg) return;
    e.preventDefault(); e.stopPropagation();
    var row = tg.closest(".camp-row"), op = row.dataset.status === "ENABLE" ? "DISABLE" : "ENABLE";
    toggleRows([row], op);
  });

  // ---- budget / bid popovers (single row or bulk) ---------------------------------
  function budgetPop(anchor, rows) {
    var cur = rows.length === 1 && rows[0].dataset.budget ? parseFloat(rows[0].dataset.budget) : null;
    var html = '<div style="font-weight:700;">Daily budget · ' + (rows.length === 1 ? esc(rows[0].dataset.name.slice(0, 36)) : rows.length + " campaigns") + '</div>' +
      '<div class="muted" style="font-size:12px;margin:3px 0 8px;">Sets every ad-group budget' + (cur ? " · now $" + cur.toFixed(2) : "") + '</div>' +
      '<div style="display:flex;gap:4px;margin-bottom:8px;" class="bq">' + ["-20", "-10", "+10", "+20", "+50"].map(function (p) { return '<button type="button" class="btn sm" data-pct="' + p + '"' + (cur ? "" : " disabled") + '>' + p + '%</button>'; }).join("") + '</div>' +
      '<div style="display:flex;gap:6px;align-items:center;"><span class="mono muted">$</span><input type="text" inputmode="decimal" style="flex:1;" value="' + (cur ? cur.toFixed(2) : "") + '" placeholder="25.00"><button type="button" class="btn sm primary">Apply' + (rows.length > 1 ? " to " + rows.length : "") + '</button></div><div class="hint" style="margin-top:6px;font-size:11px;">Queued to TikTok; you get a notification per campaign.</div>';
    var p = UI.popover(anchor, html, { alignRight: true }), inp = p.querySelector("input");
    p.querySelector(".bq").addEventListener("click", function (e) { var b = e.target.closest("[data-pct]"); if (!b || !cur) return; inp.value = (Math.round(cur * (1 + parseInt(b.dataset.pct, 10) / 100) * 100) / 100).toFixed(2); });
    p.querySelector(".primary").addEventListener("click", function () {
      var v = parseFloat(String(inp.value).replace(",", ".")); if (!(v > 0)) { inp.focus(); return; }
      p.close();
      Promise.all(rows.map(function (r) { return UI.post("/campaigns/" + r.dataset.adv + "/" + r.dataset.cid + "/edit", { adgroup_budget_all: v.toFixed(2) }); }))
        .then(function (ds) { var ok = ds.filter(function (d) { return d.ok; }).length; adopsToast && adopsToast(ok ? "ok" : "err", "Budget $" + v.toFixed(2) + " queued for " + ok + " of " + rows.length); });
    });
    inp.addEventListener("keydown", function (e) { if (e.key === "Enter") p.querySelector(".primary").click(); });
  }
  function bidPop(anchor, rows) {
    var html = '<div style="font-weight:700;">Cost cap · ' + (rows.length === 1 ? esc(rows[0].dataset.name.slice(0, 36)) : rows.length + " campaigns") + '</div><div class="muted bcur" style="font-size:12px;margin:3px 0 8px;">' + (rows.length === 1 ? "Reading ad groups…" : "One cap for every ad group of each campaign") + '</div>' +
      '<div style="display:flex;gap:4px;margin-bottom:8px;" class="bq"></div>' +
      '<div style="display:flex;gap:6px;align-items:center;"><span class="mono muted">$</span><input type="text" inputmode="decimal" style="flex:1;" placeholder="9.50"><button type="button" class="btn sm primary">Apply' + (rows.length > 1 ? " to " + rows.length : "") + '</button></div><div class="hint bh" style="margin-top:6px;font-size:11px;">BID_TYPE_CUSTOM on every ad group. TikTok applies it within minutes — watch the pace chips.</div>';
    var p = UI.popover(anchor, html, { alignRight: true }), inp = p.querySelector("input"), cap = null;
    if (rows.length === 1) {
      UI.get("/campaigns/" + rows[0].dataset.adv + "/" + rows[0].dataset.cid + "/bids").then(function (d) {
        var c = p.querySelector(".bcur");
        if (d.error) { c.innerHTML = '<span style="color:var(--warn);">' + esc(d.error) + "</span>"; return; }
        if (!d.n) { c.textContent = "No ad groups."; return; }
        if (d.cap) { cap = d.cap; c.textContent = money(d.cap) + " cap on " + d.n + " ad group" + (d.n > 1 ? "s" : ""); }
        else if (d.caps && d.caps.length) { cap = d.caps[d.caps.length - 1]; c.textContent = "mixed: " + d.caps.map(function (x) { return money(x); }).join(", "); }
        else c.textContent = (d.no_bid ? "no cap (lowest cost)" : "no cost cap set") + " · " + d.n + " ad groups";
        if (cap) { inp.value = cap.toFixed(2); p.querySelector(".bq").innerHTML = ["-10", "-5", "+5", "+10", "+20"].map(function (x) { return '<button type="button" class="btn sm" data-pct="' + x + '">' + x + '%</button>'; }).join(""); }
        if (d.smart_plus) p.querySelector(".bh").textContent = "Smart+ campaign: TikTok may refuse ad-group edits — the answer arrives as a notification.";
      });
    }
    p.querySelector(".bq").addEventListener("click", function (e) { var b = e.target.closest("[data-pct]"); if (!b || !cap) return; inp.value = (Math.round(cap * (1 + parseInt(b.dataset.pct, 10) / 100) * 100) / 100).toFixed(2); });
    p.querySelector(".primary").addEventListener("click", function () {
      var v = parseFloat(String(inp.value).replace(",", ".")); if (!(v > 0)) { inp.focus(); return; }
      p.close();
      Promise.all(rows.map(function (r) { return UI.post("/campaigns/" + r.dataset.adv + "/" + r.dataset.cid + "/bids", { cap: v.toFixed(2) }); }))
        .then(function (ds) { var ok = ds.filter(function (d) { return d.queued || d.ok; }).length; adopsToast && adopsToast(ok ? "ok" : "err", "Cost cap $" + v.toFixed(2) + " queued for " + ok + " of " + rows.length); });
    });
    inp.addEventListener("keydown", function (e) { if (e.key === "Enter") p.querySelector(".primary").click(); });
  }
  $("#selBar").addEventListener("click", function (e) {
    var b = e.target.closest("[data-bulk]"); if (!b) return;
    var rows = selectedRows(), k = b.dataset.bulk;
    if (k === "clear") { $$(".rowchk, .grpchk, #selAll").forEach(function (c) { c.checked = false; }); syncBar(); return; }
    if (!rows.length) return;
    if (k === "pause" || k === "resume") {
      var op = k === "pause" ? "DISABLE" : "ENABLE", todo = rows.filter(function (r) { return r.dataset.status !== op; });
      if (!todo.length) { adopsToast && adopsToast("ok", "Already " + (k === "pause" ? "paused" : "running")); return; }
      UI.confirm({ title: (k === "pause" ? "Pause " : "Resume ") + todo.length + " campaign" + (todo.length > 1 ? "s" : "") + "?", text: todo.slice(0, 6).map(function (r) { return r.dataset.name; }).join(", ") + (todo.length > 6 ? " +" + (todo.length - 6) + " more" : "") + ". You can undo for 10 s after.", ok: (k === "pause" ? "Pause " : "Resume ") + todo.length })
        .then(function (yes) { if (yes) toggleRows(todo, op); });
    } else if (k === "budget") budgetPop(b, rows);
    else if (k === "bid") bidPop(b, rows);
  });

  // ---- row menu (···) --------------------------------------------------------------
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".rowmenu"); if (!b) return;
    e.preventDefault(); e.stopPropagation();
    var row = b.closest(".camp-row");
    var html = '<div style="display:flex;flex-direction:column;gap:2px;min-width:200px;">' +
      '<button type="button" class="btn sm ghost" data-act="open" style="justify-content:flex-start;">Open details</button>' +
      '<button type="button" class="btn sm ghost" data-act="budget" style="justify-content:flex-start;">Set budget…</button>' +
      '<button type="button" class="btn sm ghost" data-act="bid" style="justify-content:flex-start;">Set cost cap…</button>' +
      '<button type="button" class="btn sm ghost" data-act="toggle" style="justify-content:flex-start;">' + (row.dataset.status === "ENABLE" ? "Pause" : "Resume") + '</button>' +
      '<a class="btn sm ghost" href="https://ads.tiktok.com/i18n/dashboard?aadvid=' + esc(row.dataset.adv) + '" target="_blank" rel="noopener" style="justify-content:flex-start;">Open in Ads Manager ↗</a>' +
      '<button type="button" class="btn sm ghost" data-act="rename" style="justify-content:flex-start;">Rename…</button></div>';
    var p = UI.popover(b, html, { alignRight: true });
    p.addEventListener("click", function (ev) {
      var a = ev.target.closest("[data-act]"); if (!a) return; p.close();
      if (a.dataset.act === "open") openDrawer(row);
      else if (a.dataset.act === "rename") renamePop(b, row);
      else if (a.dataset.act === "budget") budgetPop(b, [row]);
      else if (a.dataset.act === "bid") bidPop(b, [row]);
      else if (a.dataset.act === "toggle") toggleRows([row], row.dataset.status === "ENABLE" ? "DISABLE" : "ENABLE");
    });
  });

  // ---- drawer --------------------------------------------------------------------------
  var drawer = null;
  function renamePop(anchor, row) {
    var p = UI.popover(anchor, '<div style="font-weight:700;margin-bottom:6px;">Rename campaign</div><div style="display:flex;gap:6px;"><input type="text" value="' + esc(row.dataset.name) + '" style="flex:1;min-width:260px;"><button type="button" class="btn sm primary">Save</button></div><div class="muted" style="font-size:11px;margin-top:4px;">Renames it on TikTok in the background. In campaign-name source mode the ?source= keeps the OLD name — postbacks still match.</div>', { alignRight: true });
    function go() {
      var v = p.querySelector("input").value.trim(); if (!v || v === row.dataset.name) { p.close(); return; }
      UI.post("/campaigns/" + row.dataset.adv + "/" + row.dataset.cid + "/edit", { campaign_name: v }).then(function (r) {
        p.close(); adopsToast && adopsToast(r.ok ? "ok" : "err", r.ok ? "Rename queued: " + v : (r.error || "failed"));
        if (r.ok) { row.dataset.name = v; var n = row.querySelector(".cname a"); if (n) n.textContent = v; if (drawer) drawer.setTitle(v, null); }
      });
    }
    p.querySelector(".primary").addEventListener("click", go);
    p.querySelector("input").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); go(); } });
  }
  function openDrawer(row) {
    if (drawer) drawer.close();
    var adv = row.dataset.adv, cid = row.dataset.cid;
    drawer = UI.drawer({ title: row.dataset.name, sub: "Loading…", body: '<div class="muted">Loading…</div>', onClose: function () { drawer = null; } });
    UI.get("/campaigns/" + adv + "/" + cid + "/detail").then(function (d) {
      if (!drawer) return;
      if (d.error) { drawer.body.innerHTML = '<div class="flash err">' + esc(d.error) + "</div>"; return; }
      var c = d.campaign, m = d.metrics, y = d.yesterday;
      var st = c.blocked ? '<span class="pill err">blocked · ' + esc(c.blocked) + "</span>" : (c.status === "ENABLE" ? '<span class="pill ok">live</span>' : '<span class="pill warn">paused</span>');
      drawer.setTitle(c.name, esc(c.account) + (c.bc ? " · " + esc(c.bc) : "") + " · " + st + (c.launched_ago ? " · launched " + esc(c.launched_ago) : "") + (c.smart_plus ? ' · <span class="pill dim">S+</span>' : ""));
      function kpi(l, v, cls) { return '<div class="kpi"><div class="lab">' + l + '</div><div class="val ' + (cls || "") + '">' + v + "</div></div>"; }
      var has = !!c.source;
      var profitTxt = has ? ((m.profit > 0 ? "+" : "") + money(m.profit, Math.abs(m.profit) >= 100 ? 0 : 2)) : "—";
      var yTxt = has && (y.spend || y.revenue) ? ("yesterday " + (y.profit >= 0 ? "+" : "") + money(y.profit, 0) + " · " + y.roas.toFixed(2) + "×") : "";
      var html =
        '<div class="kpis kpis-4" style="margin:0;gap:8px;">' + kpi("Profit", profitTxt, has ? (m.profit >= 0 ? "good" : "bad") : "") + kpi("ROAS", has && m.spend ? m.roas.toFixed(2) + "×" : "—") + kpi("Spend", money(m.spend)) + kpi("EPC", has && m.clicks ? money(m.epc) : "—") + "</div>" +
        (yTxt ? '<div class="muted" style="font-size:11.5px;margin-top:-6px;">' + yTxt + "</div>" : "") +
        '<div><div class="drawer-sec">Today by hour · yesterday dashed</div><div id="dwChart"></div><div style="display:flex;justify-content:space-between;font-size:10.5px;color:var(--faint);"><span>0h</span><span>6h</span><span>12h</span><span>18h</span><span>23h</span></div>' +
        '<div class="seg" style="margin-top:6px;"><button type="button" class="on" data-series="spend">Spend</button><button type="button" data-series="revenue">Revenue</button><button type="button" data-series="conversions">Conv</button><button type="button" data-series="clicks">Clicks</button></div></div>' +
        '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px 10px;font-size:12px;">' + [["Revenue", has ? money(m.revenue) : "—"], ["Conversions", m.conversions], ["CPA", m.conversions ? money(m.cpa) : "—"], ["Impressions", m.impressions.toLocaleString()], ["Clicks", m.clicks.toLocaleString()], ["CTR", m.ctr.toFixed(2) + "%"], ["CPC", m.clicks ? money(m.cpc) : "—"], ["CPM", m.impressions ? money(m.cpm) : "—"], ["PB clicks", has ? Math.round(m.pb_clicks) : "—"]].map(function (x) { return '<div><div class="muted" style="font-size:10.5px;">' + x[0] + '</div><div style="font-weight:600;">' + x[1] + "</div></div>"; }).join("") + "</div>" +
        (m.shared_n > 1 ? '<div class="muted" style="font-size:11px;">Source <span class="mono">' + esc(c.source) + "</span> is shared by " + m.shared_n + " campaigns — revenue split by spend share (" + Math.round(m.share * 100) + "%).</div>" : "") +
        (c.budget_mode && c.budget_mode !== "BUDGET_MODE_INFINITE" && c.budget ? '<div><div class="drawer-sec">Campaign budget (CBO · ' + (c.budget_mode === "BUDGET_MODE_TOTAL" ? "total" : "daily") + ')</div><div style="display:flex;gap:6px;"><input type="text" inputmode="decimal" id="dwCbo" value="' + c.budget.toFixed(2) + '"><button type="button" class="btn sm" id="dwCboSave">Save</button></div></div>' : "") +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;"><div><div class="drawer-sec">Ad group budgets (each)</div><div style="display:flex;gap:6px;"><input type="text" inputmode="decimal" id="dwBudget" value="' + (c.budget ? c.budget.toFixed(2) : "") + '" placeholder="25.00"><button type="button" class="btn sm" id="dwBudgetSave">Save</button></div></div>' +
        '<div><div class="drawer-sec">Cost cap</div><div style="display:flex;gap:6px;"><input type="text" inputmode="decimal" id="dwCap" placeholder="9.50"><button type="button" class="btn sm" id="dwCapSave">Save</button></div><div class="muted" id="dwCapCur" style="font-size:10.5px;margin-top:3px;">reading…</div></div></div>' +
        (d.creative ? '<div style="display:flex;gap:10px;align-items:center;"><span class="thumb previewBtn" style="width:34px;height:46px;border-radius:6px;background:var(--accent-soft);display:grid;place-items:center;color:var(--accent-ink);cursor:pointer;flex:none;" data-src="' + esc(d.creative.file) + '" data-name="' + esc(d.creative.name) + '">▶</span><div style="min-width:0;font-size:12px;"><b>' + esc(d.creative.name) + '</b><div class="muted" style="font-size:11px;">library ' + esc(d.creative.kind) + ' · <a href="/creatives?view=performance">performance</a></div></div></div>' : (d.spark ? '<div style="font-size:12px;"><b>✦ ' + esc(d.spark) + '</b> <span class="muted">spark code</span></div>' : "")) +
        '<div><div class="drawer-sec">Note</div><textarea id="dwNote" rows="2" style="min-height:52px;font-family:var(--font-sans);font-size:12.5px;" placeholder="Why you scaled it, what to watch…">' + esc(d.note) + '</textarea><div class="muted" id="dwNoteSt" style="font-size:10.5px;margin-top:2px;">saves on its own</div></div>' +
        '<div><div class="drawer-sec">Timeline</div>' + (d.timeline.length ? d.timeline.map(function (t) { return '<div style="display:flex;gap:8px;font-size:12px;padding:3px 0;border-bottom:1px solid var(--border-soft);"><span class="muted" style="flex:none;width:64px;">' + esc(t.ago) + '</span><span style="min-width:0;"><b>' + esc(t.action) + "</b> " + esc(t.detail) + (t.who ? ' <span class="muted">· ' + esc(t.who) + "</span>" : "") + "</span></div>"; }).join("") : '<div class="muted">Nothing yet.</div>') + "</div>" +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;"><button type="button" class="btn sm" id="dwToggle">' + (c.status === "ENABLE" ? "Pause" : "Resume") + '</button><a class="btn sm" href="' + esc(c.ads_manager_url) + '" target="_blank" rel="noopener">Ads Manager ↗</a><a class="btn sm" href="/status?account=' + esc(adv) + '">This account</a><button type="button" class="btn sm ghost" id="dwRename">Rename…</button></div>';
      drawer.body.innerHTML = html;
      // chart
      var H = d.hourly, cur = "spend";
      function draw() { UI.hourChart($("#dwChart"), H.today[cur], H.yesterday[cur], { hourNow: H.hour_now }); }
      draw();
      $$("[data-series]", drawer.body).forEach(function (b) { b.addEventListener("click", function () { $$("[data-series]", drawer.body).forEach(function (x) { x.classList.remove("on"); }); b.classList.add("on"); cur = b.dataset.series; draw(); }); });
      // budget / cap
      $("#dwBudgetSave").addEventListener("click", function () {
        var v = parseFloat(String($("#dwBudget").value).replace(",", ".")); if (!(v > 0)) return;
        UI.post("/campaigns/" + adv + "/" + cid + "/edit", { adgroup_budget_all: v.toFixed(2) }).then(function (r) { adopsToast && adopsToast(r.ok ? "ok" : "err", r.ok ? "Budget $" + v.toFixed(2) + " queued" : (r.error || "failed")); });
      });
      UI.get("/campaigns/" + adv + "/" + cid + "/bids").then(function (b) {
        var el = $("#dwCapCur"); if (!el) return;
        if (b.error) { el.textContent = b.error; return; }
        if (b.cap) { el.textContent = "now " + money(b.cap) + " on " + b.n + " ad group" + (b.n > 1 ? "s" : ""); $("#dwCap").value = b.cap.toFixed(2); }
        else if (b.caps && b.caps.length) { el.textContent = "mixed: " + b.caps.map(function (x) { return money(x); }).join(", "); }
        else el.textContent = b.n ? (b.no_bid ? "no cap (lowest cost)" : "no cap set") : "no ad groups";
      });
      $("#dwCapSave").addEventListener("click", function () {
        var v = parseFloat(String($("#dwCap").value).replace(",", ".")); if (!(v > 0)) return;
        UI.post("/campaigns/" + adv + "/" + cid + "/bids", { cap: v.toFixed(2) }).then(function (r) { adopsToast && adopsToast(r.queued ? "ok" : "err", r.queued ? "Cost cap $" + v.toFixed(2) + " queued" : (r.error || "failed")); });
      });
      // note autosave
      var nt = $("#dwNote"), timer = null, last = d.note;
      nt.addEventListener("input", function () { clearTimeout(timer); $("#dwNoteSt").textContent = "…"; timer = setTimeout(function () {
        if (nt.value === last) { $("#dwNoteSt").textContent = "saved"; return; }
        UI.post("/notes/campaign/" + cid, { text: nt.value }).then(function (r) { last = nt.value; $("#dwNoteSt").textContent = r.ok ? "saved" : "couldn't save"; var badge = row.querySelector(".cname"); if (badge && r.ok) { var old = badge.querySelector("[title]"); if (nt.value.trim() && !old) badge.insertAdjacentHTML("beforeend", ' <span title="' + esc(nt.value.slice(0, 200)) + '" style="font-size:11px;">📝</span>'); else if (!nt.value.trim() && old) old.remove(); } });
      }, 700); });
      $("#dwToggle").addEventListener("click", function () { var op = row.dataset.status === "ENABLE" ? "DISABLE" : "ENABLE"; toggleRows([row], op); this.textContent = op === "ENABLE" ? "Pause" : "Resume"; });
      var cboBtn = $("#dwCboSave"); if (cboBtn) cboBtn.addEventListener("click", function () {
        var v = parseFloat(String($("#dwCbo").value).replace(",", ".")); if (!(v > 0)) return;
        UI.post("/campaigns/" + adv + "/" + cid + "/edit", { campaign_budget: v.toFixed(2) }).then(function (r) { adopsToast && adopsToast(r.ok ? "ok" : "err", r.ok ? "Campaign budget $" + v.toFixed(2) + " queued" : (r.error || "failed")); });
      });
      $("#dwRename").addEventListener("click", function () { renamePop(this, row); });
    });
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest(".open-drawer");
    if (a) { e.preventDefault(); openDrawer(a.closest(".camp-row")); return; }
    var row = e.target.closest && e.target.closest(".camp-row");
    if (row && !e.target.closest("input, button, a, .previewBtn, .tog")) { openDrawer(row); }
  });

  // ---- creative preview modal ------------------------------------------------------------
  var modal = $("#vidModal"), player = $("#vidPlayer");
  function closeModal() { modal.style.display = "none"; player.pause(); player.removeAttribute("src"); player.load(); }
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".previewBtn"); if (!b) return;
    e.stopPropagation(); e.preventDefault(); $("#vidTitle").textContent = b.dataset.name || "Preview"; player.src = b.dataset.src; modal.style.display = "flex"; player.play().catch(function () {});
  });
  $("#vidClose").addEventListener("click", closeModal);
  modal.addEventListener("click", function (e) { if (e.target === modal) closeModal(); });

  // ---- keyboard: J/K move · Enter open · P pause · B budget · X select · ? help ---------
  var focus = null;
  function visibleRows() { return $$(".camp-row").filter(function (r) { return !r.hidden; }); }
  function setFocus(r) { $$(".rowfocus").forEach(function (x) { x.classList.remove("rowfocus"); }); focus = r; if (r) { r.classList.add("rowfocus"); r.scrollIntoView({ block: "nearest" }); } }
  document.addEventListener("keydown", function (e) {
    var tag = (e.target.tagName || "").toUpperCase();
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.metaKey || e.ctrlKey || e.altKey) return;
    if (document.querySelector(".layer-bg")) return;
    var rows = visibleRows(); if (!rows.length && e.key !== "?") return;
    var i = rows.indexOf(focus);
    if (e.key === "j" || e.key === "ArrowDown") { e.preventDefault(); setFocus(rows[Math.min(i + 1, rows.length - 1)]); }
    else if (e.key === "k" || e.key === "ArrowUp") { e.preventDefault(); setFocus(rows[Math.max(i - 1, 0)]); }
    else if (e.key === "Enter" && focus) { e.preventDefault(); openDrawer(focus); }
    else if (e.key === "p" && focus) { e.preventDefault(); toggleRows([focus], focus.dataset.status === "ENABLE" ? "DISABLE" : "ENABLE"); }
    else if (e.key === "b" && focus) { e.preventDefault(); budgetPop(focus.querySelector(".c-budget"), [focus]); }
    else if (e.key === "x" && focus) { e.preventDefault(); var c = focus.querySelector(".rowchk"); c.checked = !c.checked; syncBar(); }
    else if (e.key === "?") { e.preventDefault(); UI.kbdHelp([["J / K", "Move down / up"], ["Enter", "Open the campaign drawer"], ["P", "Pause or resume (with Undo)"], ["B", "Set the daily budget"], ["X", "Select / unselect the row"], ["Esc", "Close drawer, pop-up or menu"], ["⌘K", "Search or jump anywhere"], ["⌘\\", "Collapse the sidebar"]]); }
  });

  // ---- KPI sparklines ------------------------------------------------------------------------
  var spark = {}; try { spark = JSON.parse($("#sparkData").textContent || "{}"); } catch (e) {}
  function drawSparks() {
    var cs = getComputedStyle(document.documentElement), accent = cs.getPropertyValue("--accent").trim(), good = cs.getPropertyValue("--ok").trim();
    $$("[data-spark]").forEach(function (el) {
      var data = spark[el.getAttribute("data-spark")]; if (!data || data.length < 3) { el.style.display = "none"; return; }
      var w = 120, h = 22, mx = Math.max.apply(null, data), mn = Math.min.apply(null, data), rng = (mx - mn) || 1, n = data.length, color = el.getAttribute("data-spark") === "revenue" ? good : accent;
      var pts = data.map(function (v, i) { return ((i / (n - 1)) * w).toFixed(1) + "," + (h - 2 - ((v - mn) / rng) * (h - 4)).toFixed(1); });
      el.innerHTML = '<svg viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none"><path d="M0,' + h + ' L' + pts.join(" L") + ' L' + w + ',' + h + ' Z" fill="' + color + '" opacity="0.12"/><path d="M' + pts.join(" L") + '" fill="none" stroke="' + color + '" stroke-width="1.5" vector-effect="non-scaling-stroke"/></svg>';
    });
  }
  drawSparks();

  // ---- in-place refresh (keeps filters, columns, open groups, selection) -----------------------
  var refreshBtn = $("#refreshBtn"), syncBtn = $("#syncBtn"), syncForm = $("#syncForm"), busy = false, syncWaiting = false;
  function setBusy(on, which) { busy = on; [refreshBtn, syncBtn].forEach(function (b) { if (b) b.disabled = on; }); if (refreshBtn) refreshBtn.textContent = on && which === "refresh" ? "↻ Refreshing…" : "↻ Refresh"; if (syncBtn) syncBtn.textContent = on && which === "sync" ? "⟳ Syncing…" : "⟳ Sync"; }
  function swapFrom(doc) {
    var sel = {}; selectedRows().forEach(function (r) { sel[r.dataset.cid] = 1; });
    var fcid = focus && focus.dataset.cid;
    ["pendingFlash", "campKpis", "campCard"].forEach(function (id) { var cur = document.getElementById(id), nxt = doc.getElementById(id); if (cur && nxt) cur.replaceWith(nxt); });
    var sa = doc.getElementById("syncedAgo"), sc = $("#syncedAgo"); if (sa && sc) sc.textContent = sa.textContent;
    var sd = doc.getElementById("sparkData"); if (sd) { try { spark = JSON.parse(sd.textContent || "{}"); } catch (e) {} }
    drawSparks(); applyCols(); applyGroups();
    $$(".camp-row").forEach(function (r) { if (sel[r.dataset.cid]) r.querySelector(".rowchk").checked = true; });
    syncBar();
    if (fcid) setFocus($$(".camp-row").filter(function (r) { return r.dataset.cid === fcid; })[0] || null);
  }
  function refreshInPlace(which) {
    if (busy) return Promise.resolve();
    if (which === "sync") {
      setBusy(true, "sync"); syncWaiting = true;
      return UI.post("/status/sync", {}).then(function (d) { if (!d.queued) { syncWaiting = false; setBusy(false); } }).catch(function () { syncWaiting = false; setBusy(false); });
    }
    setBusy(true, "refresh");
    return fetch(location.pathname + location.search, { credentials: "same-origin", headers: { "X-Requested-With": "fetch" } })
      .then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.text(); })
      .then(function (html) { swapFrom(new DOMParser().parseFromString(html, "text/html")); })
      .catch(function () { location.reload(); })
      .then(function () { setBusy(false); });
  }
  document.addEventListener("adops:job", function (e) {
    var j = e.detail || {};
    if (j.kind === "status_sync" && syncWaiting) { syncWaiting = false; setBusy(false); refreshInPlace("refresh"); return; }
    if (j.kind === "bid" || j.kind === "campaign_edit" || j.kind === "launch") refreshInPlace("refresh");
  });
  refreshBtn.addEventListener("click", function () { refreshInPlace("refresh"); });
  syncForm.addEventListener("submit", function (e) { e.preventDefault(); refreshInPlace("sync"); });
  if (state.range === "today") {
    setInterval(function () {
      var el = document.activeElement, typing = el && (el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA");
      if (document.hidden || typing || String(window.getSelection && window.getSelection()) || document.querySelector("[data-fsel].open, .layer-bg, .pop") || modal.style.display === "flex") return;
      refreshInPlace("refresh");
    }, 60000);
  }
})();
