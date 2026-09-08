/* accounts.js — Ad accounts page: filter/search, BC chips + collapse, row selection with a bulk bar
   (launch / switch on-off), row menu, account drawer (facts, campaigns, transfer from BC wallet,
   note autosave), Connect pop-up. Server-rendered table; everything here is progressive. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); }, $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var esc = UI.esc, money = UI.money;

  // ---- Connect pop-up ------------------------------------------------------------------------
  var box = $("#acConnectBox");
  function openConnect() {
    var node = box.firstElementChild; box.hidden = false;
    UI.modal({ title: "Connect a Business Center", body: node, onClose: function () { box.appendChild(node); box.hidden = true; } });
  }
  ["#acConnectBtn", "#acConnectBtn2", "#acConnectBtn3"].forEach(function (id) { var b = $(id); if (b) b.addEventListener("click", openConnect); });
  if (/[?&]connect=1/.test(location.search)) openConnect();

  var rows = $$(".ac-row");
  if (!rows.length) return;

  // ---- filter / search / BC chips ------------------------------------------------------------
  var f = "", bcs = {}, search = $("#acSearch"), shown = $("#acShown");
  try { bcs = JSON.parse(localStorage.getItem("adops-acc-bcs") || "{}"); } catch (e) { bcs = {}; }
  var wantBc = new URLSearchParams(location.search).get("bc");   // arrived from Home → only that Business Center
  if (wantBc != null && $('#acBcChips .chip[data-bc="' + wantBc + '"]')) { bcs = {}; bcs[wantBc] = true; }
  function anyBc() { return Object.keys(bcs).some(function (k) { return bcs[k]; }); }
  function apply() {
    var q = search.value.trim().toLowerCase(), n = 0;
    rows.forEach(function (r) {
      var st = r.dataset.state, on = r.dataset.enabled === "1";
      var okF = !f || (f === "off" ? !on : (f === "blocked" ? (st === "blocked" || st === "cooldown") : st === f));
      var okQ = !q || r.dataset.search.indexOf(q) >= 0;
      r.hidden = !(okF && okQ); if (!r.hidden) n++;
    });
    $$(".ac-bc").forEach(function (g) {
      var vis = $$(".ac-row", g).filter(function (r) { return !r.hidden; }).length;
      var bcOk = !anyBc() || !!bcs[g.dataset.bc];
      g.hidden = !bcOk || (vis === 0 && (f || q));
      $(".ac-bc-n", g).textContent = vis;
    });
    $$("#acBcChips .chip").forEach(function (c) { c.classList.toggle("on", !!bcs[c.dataset.bc]); });
    var total = rows.length;
    shown.textContent = (n === total ? total + " accounts" : n + " of " + total + " accounts") + " · grouped by Business Center · profit first";
    var visibleGroups = $$(".ac-bc").filter(function (g) { return !g.hidden; }).length;
    $("#acEmpty").hidden = visibleGroups > 0;
  }
  search.addEventListener("input", apply);
  $$("#acFilter button").forEach(function (b) { b.addEventListener("click", function () { $$("#acFilter button").forEach(function (o) { o.classList.remove("on"); }); b.classList.add("on"); f = b.dataset.f; apply(); }); });
  $$("#acBcChips .chip").forEach(function (c) { c.addEventListener("click", function () { bcs[c.dataset.bc] = !bcs[c.dataset.bc]; try { localStorage.setItem("adops-acc-bcs", JSON.stringify(bcs)); } catch (e) {} apply(); }); });
  $$(".ac-bc-h").forEach(function (h) {
    h.addEventListener("click", function (e) {
      if (e.target.closest("a, button, input")) return;
      var card = h.closest(".ac-bc"), body = $(".ac-body", card), open = body.hidden;
      body.hidden = !open; $(".ac-chev", h).style.transform = open ? "" : "rotate(-90deg)";
    });
  });
  var pre = new URLSearchParams(location.search).get("state");
  if (pre) { var pb = $('#acFilter [data-f="' + pre + '"]'); if (pb) pb.click(); }
  apply();

  // ---- selection + bulk bar ------------------------------------------------------------------
  function sel() { return rows.filter(function (r) { return $(".acchk", r).checked; }); }
  function sync() {
    var s = sel(); $("#acSel").hidden = !s.length; $("#acSelCount").textContent = s.length + " selected";
    rows.forEach(function (r) { r.classList.toggle("rowsel", $(".acchk", r).checked); });
  }
  document.addEventListener("change", function (e) {
    if (e.target.classList.contains("acchk")) sync();
    if (e.target.classList.contains("ac-all")) { $$(".ac-row", e.target.closest(".ac-bc")).forEach(function (r) { if (!r.hidden) $(".acchk", r).checked = e.target.checked; }); sync(); }
  });
  function setEnabled(list, on) {
    var todo = list.filter(function (r) { return (r.dataset.enabled === "1") !== on; });
    if (!todo.length) return Promise.resolve();
    return Promise.all(todo.map(function (r) { return UI.post("/accounts/" + r.dataset.id + "/toggle").then(function (d) { if (d.ok) { r.dataset.enabled = d.enabled ? "1" : "0"; r.style.opacity = d.enabled ? "" : ".55"; } }); }))
      .then(function () { adopsToast && adopsToast("ok", (on ? "Switched on " : "Switched off ") + todo.length + " account" + (todo.length > 1 ? "s" : "")); apply(); });
  }
  $("#acSel").addEventListener("click", function (e) {
    var b = e.target.closest("[data-ac]"); if (!b) return; var s = sel(), k = b.dataset.ac;
    if (k === "clear") { $$(".acchk, .ac-all").forEach(function (c) { c.checked = false; }); sync(); return; }
    if (!s.length) return;
    if (k === "launch") { location.href = "/super-launcher?accounts=" + s.map(function (r) { return r.dataset.id; }).join(","); return; }
    if (k === "on" || k === "off") {
      var on = k === "on", before = s.map(function (r) { return r.dataset.enabled; });
      setEnabled(s, on).then(function () { UI.undo((on ? "Switched on " : "Switched off ") + s.length + " account" + (s.length > 1 ? "s" : ""), function () { return Promise.all(s.map(function (r, i) { return (r.dataset.enabled !== before[i]) ? setEnabled([r], before[i] === "1") : null; })); }); });
    }
  });

  // ---- row menu -------------------------------------------------------------------------------
  document.addEventListener("click", function (e) {
    var b = e.target.closest(".ac-menu"); if (!b) return; e.stopPropagation();
    var r = b.closest(".ac-row"), d = r.dataset, on = d.enabled === "1";
    var html = '<div style="display:flex;flex-direction:column;gap:2px;min-width:230px;">' +
      '<button type="button" class="btn sm ghost" data-act="open" style="justify-content:flex-start;">Details</button>' +
      '<a class="btn sm ghost" href="/super-launcher?accounts=' + esc(d.id) + '" style="justify-content:flex-start;">🚀 Launch here</a>' +
      '<a class="btn sm ghost" href="/status?account=' + esc(d.id) + '&origin=all" style="justify-content:flex-start;">Campaigns</a>' +
      '<a class="btn sm ghost" href="https://ads.tiktok.com/i18n/dashboard?aadvid=' + esc(d.id) + '" target="_blank" rel="noopener" style="justify-content:flex-start;">Ads Manager ↗</a>' +
      '<button type="button" class="btn sm ghost" data-act="transfer" style="justify-content:flex-start;">Transfer from BC wallet…</button>' +
      '<button type="button" class="btn sm ghost' + (on ? " danger" : "") + '" data-act="toggle" style="justify-content:flex-start;">' + (on ? "Switch off (skip in launchers)" : "Switch on") + "</button></div>";
    var p = UI.popover(b, html, { alignRight: true });
    p.addEventListener("click", function (ev) {
      var a = ev.target.closest("[data-act]"); if (!a) return; p.close();
      if (a.dataset.act === "open") openDrawer(r);
      else if (a.dataset.act === "toggle") setEnabled([r], !on).then(function () { UI.undo((on ? "Switched off " : "Switched on ") + d.name, function () { return setEnabled([r], on); }); });
      else if (a.dataset.act === "transfer") transferPop(b, d.id, d.name);
    });
  });

  // ---- transfer from BC wallet (same /bc/transfer/ call the auto top-up uses) -----------------
  function transferPop(anchor, id, name, onDone) {
    var p = UI.popover(anchor, '<div style="font-weight:700;margin-bottom:4px;">Transfer to ' + esc(name) + '</div><div class="muted" style="font-size:11px;margin-bottom:6px;">From its Business Center wallet into the ad account.</div><div style="display:flex;gap:6px;"><input type="text" inputmode="decimal" placeholder="50" style="flex:1;"><button type="button" class="btn sm primary">Transfer</button></div><div class="muted tf-err" style="font-size:11px;margin-top:4px;color:var(--err);"></div>', { alignRight: true });
    function go() {
      var v = parseFloat(p.querySelector("input").value); if (!(v > 0)) return;
      UI.confirm({ title: "Transfer " + money(v) + " to " + name + "?", text: "Moves money from the Business Center wallet into this ad account on TikTok. This cannot be undone here.", ok: "Transfer" }).then(function (y) {
        if (!y) return;
        UI.post("/accounts/" + id + "/transfer", { amount: v }).then(function (d) {
          if (!d.ok) { $(".tf-err", p).textContent = d.error || "Transfer failed."; return; }
          p.close(); adopsToast && adopsToast("ok", "Transferred " + money(v) + " to " + name);
          if (onDone) onDone(d); else location.reload();
        });
      });
    }
    p.querySelector(".primary").addEventListener("click", go);
    p.querySelector("input").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); go(); } });
  }

  // ---- drawer --------------------------------------------------------------------------------
  var drawer = null;
  function openDrawer(row) {
    if (drawer) drawer.close();
    var id = row.dataset.id;
    drawer = UI.drawer({ title: row.dataset.name, sub: "Loading…", body: '<div class="muted">Loading…</div>', onClose: function () { drawer = null; } });
    UI.get("/accounts/" + id + "/detail").then(function (d) {
      if (!drawer) return;
      if (d.error) { drawer.body.innerHTML = '<div class="flash err">' + esc(d.error) + "</div>"; return; }
      var f = d.facts, has = !!(f.spend || f.revenue);
      var stMap = { fresh: "ok", used: "dim", active: "warn", cooldown: "err", blocked: "err" }, stLbl = { fresh: "fresh", used: "used", active: "live", cooldown: "cooling", blocked: "blocked" };
      drawer.setTitle(d.name, '<span class="mono">' + esc(d.id) + "</span> · " + (d.bc ? esc(d.bc.name) + " · " : "") + '<span class="pill ' + stMap[f.state] + '">' + stLbl[f.state] + "</span>" + (f.reason ? " " + esc(f.reason) : "") + (d.enabled ? "" : ' · <span class="pill dim">off</span>'));
      function kpi(l, v, cls) { return '<div class="kpi"><div class="lab">' + l + '</div><div class="val ' + (cls || "") + '">' + v + "</div></div>"; }
      var profitTxt = has ? ((f.profit > 0 ? "+" : "") + money(f.profit, Math.abs(f.profit) >= 100 ? 0 : 2)) : "—";
      var html =
        '<div class="kpis kpis-4" style="margin:0;gap:8px;">' + kpi("Profit today", profitTxt, has ? (f.profit >= 0 ? "good" : "bad") : "") + kpi("ROAS", has && f.spend ? f.roas.toFixed(2) + "×" : "—") + kpi("Spend", money(f.spend)) + kpi("Balance", d.balance == null ? "—" : money(d.balance, 0), d.balance != null && d.balance < 20 ? "bad" : "") + "</div>" +
        '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px 10px;font-size:12px;">' + [["Campaigns", f.active + " live / " + f.total], ["Last launch", f.last || "never"], ["TikTok status", (d.status || "—").replace("STATUS_", "").replace(/_/g, " ").toLowerCase()], ["Currency", d.currency], ["Timezone", d.timezone || "—"], ["Token expires", d.token_expires || "—"]].map(function (x) { return '<div><div class="muted" style="font-size:10.5px;">' + x[0] + '</div><div style="font-weight:600;">' + esc(x[1]) + "</div></div>"; }).join("") + "</div>" +
        (d.bc ? '<div style="font-size:12px;display:flex;align-items:center;gap:8px;"><span class="muted">BC wallet</span><b style="' + ((d.bc.balance || 0) < 100 ? "color:var(--err);" : "") + '">' + esc(d.bc.name) + " · " + money(d.bc.balance || 0, 0) + '</b><button type="button" class="btn sm" id="dwTransfer" style="margin-left:auto;">Transfer…</button></div>' : "") +
        (d.cooldown_until ? '<div class="flash warn" style="font-size:12px;">Cooling down until ' + esc(d.cooldown_until.replace("T", " ").slice(0, 16)) + " after " + d.error_count + " failed launches.</div>" : "") +
        '<div><div class="drawer-sec">Campaigns today</div>' + (d.campaigns.length ? '<table class="data dense" style="font-size:12px;"><tbody>' + d.campaigns.slice(0, 12).map(function (c) { return '<tr><td style="max-width:170px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"><a href="/status?q=' + encodeURIComponent(c.name) + '" style="color:inherit;">' + esc(c.name) + "</a></td><td>" + (c.status === "ENABLE" ? '<span class="pill ok">live</span>' : '<span class="pill dim">paused</span>') + '</td><td class="num">' + money(c.spend, 0) + '</td><td class="num">' + (c.spend || c.revenue ? '<span class="profit-cell ' + (c.profit >= 0 ? "pos" : "neg") + '">' + (c.profit > 0 ? "+" : "") + money(c.profit, 0) + "</span>" : "—") + "</td></tr>"; }).join("") + "</tbody></table>" + (d.campaigns.length > 12 ? '<div class="muted" style="font-size:11px;margin-top:4px;">+ ' + (d.campaigns.length - 12) + ' more · <a href="/status?account=' + esc(d.id) + '&origin=all">all campaigns</a></div>' : "") : '<div class="muted">Nothing launched here yet.</div>') + "</div>" +
        '<div><div class="drawer-sec">Note</div><textarea id="dwNote" rows="2" style="min-height:52px;font-family:var(--font-sans);font-size:12.5px;" placeholder="Card on file, who owns it, what it is for…">' + esc(d.note) + '</textarea><div class="muted" id="dwNoteSt" style="font-size:10.5px;margin-top:2px;">saves on its own</div></div>' +
        '<div><div class="drawer-sec">Recent launches</div>' + (d.launches.length ? d.launches.map(function (l) { return '<div style="display:flex;gap:8px;font-size:12px;padding:3px 0;border-bottom:1px solid var(--border-soft);"><span class="muted" style="flex:none;width:64px;">' + esc(l.at) + "</span><span>" + (l.ok ? '<span class="pill ok">ok</span> ' + esc(l.campaign_id) : '<span class="pill err">failed</span> ' + esc(l.error)) + "</span></div>"; }).join("") : '<div class="muted">No launches yet.</div>') + "</div>" +
        '<div style="display:flex;gap:8px;flex-wrap:wrap;"><a class="btn sm primary" href="/super-launcher?accounts=' + esc(d.id) + '">🚀 Launch here</a><a class="btn sm" href="' + esc(d.ads_manager) + '" target="_blank" rel="noopener">Ads Manager ↗</a><button type="button" class="btn sm" id="dwToggle">' + (d.enabled ? "Switch off" : "Switch on") + "</button></div>";
      drawer.body.innerHTML = html;
      var tr = $("#dwTransfer", drawer.el); if (tr) tr.addEventListener("click", function () { transferPop(tr, d.id, d.name, function (res) { drawer.body.querySelector(".kpi:nth-child(4) .val").textContent = money(res.balance, 0); }); });
      $("#dwToggle", drawer.el).addEventListener("click", function () { setEnabled([row], !(row.dataset.enabled === "1")).then(function () { openDrawer(row); }); });
      var note = $("#dwNote", drawer.el), st = $("#dwNoteSt", drawer.el), t;
      note.addEventListener("input", function () { st.textContent = "typing…"; clearTimeout(t); t = setTimeout(function () { UI.post("/notes/account/" + id, { text: note.value }).then(function (res) { st.textContent = res.ok ? "saved" : "could not save"; }); }, 700); });
    });
  }
  document.addEventListener("click", function (e) {
    var r = e.target.closest(".ac-row"); if (!r || e.target.closest("a, button, input, label")) return;
    openDrawer(r);
  });
  var openId = new URLSearchParams(location.search).get("open");
  if (openId) { var orow = rows.filter(function (r) { return r.dataset.id === openId; })[0]; if (orow) openDrawer(orow); }
})();
