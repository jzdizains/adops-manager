/* creatives.js — results board + library grid: side drawer (results by account, note,
   labels, favourite, archive, rename), selection + bulk actions, upload pop-up, filters. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var esc = UI.esc, money = UI.money;
  function roas(r) { if (!r) return '<span class="roas-pill n">—</span>'; return '<span class="roas-pill ' + (r >= 1.3 ? "g" : (r >= 0.9 ? "w" : "r")) + '">' + r.toFixed(2) + "×</span>"; }
  function profit(v) { return '<span class="profit-cell ' + (v >= 0 ? "pos" : "neg") + '">' + (v > 0 ? "+" : "") + money(v, Math.abs(v) >= 100 ? 0 : 2) + "</span>"; }

  // ---- upload pop-up (the existing uploader form, moved into a modal) ----------
  var ub = $("#uploadBtn"), box = $("#uploadBox");
  if (ub) ub.addEventListener("click", function () {
    if (!box) { location.href = "/creatives?view=library&upload=1"; return; }
    var node = box.firstElementChild; box.hidden = false;
    var m = UI.modal({ title: "Upload videos & images", body: node, wide: true, onClose: function () { box.appendChild(node); box.hidden = true; } });
    m.el.classList.add("up-modal");
  });
  if (box && /[?&]upload=1/.test(location.search)) ub.click();

  // ---- chip dropdowns ---------------------------------------------------------------
  $$("[data-fsel]").forEach(function (fs) {
    fs.querySelector("button").addEventListener("click", function (e) { e.stopPropagation(); var open = fs.classList.contains("open"); $$("[data-fsel].open").forEach(function (o) { o.classList.remove("open"); }); if (!open) fs.classList.add("open"); });
  });
  document.addEventListener("click", function () { $$("[data-fsel].open").forEach(function (o) { o.classList.remove("open"); }); });
  var lf = $("#libFilter"); if (lf) lf.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); lf.submit(); } });

  // ---- grid size (remembered) ---------------------------------------------------------
  var grid = $("#libGrid"), sizeSeg = $("#libSize");
  if (grid && sizeSeg) {
    var sz = "m"; try { sz = localStorage.getItem("adops-cr-size") || "m"; } catch (e) {}
    grid.dataset.size = sz; $$("button", sizeSeg).forEach(function (b) { b.classList.toggle("on", b.dataset.size === sz); });
    sizeSeg.addEventListener("click", function (e) { var b = e.target.closest("[data-size]"); if (!b) return; grid.dataset.size = b.dataset.size; $$("button", sizeSeg).forEach(function (x) { x.classList.toggle("on", x === b); }); try { localStorage.setItem("adops-cr-size", b.dataset.size); } catch (e2) {} });
  }

  // ---- results filter (winners / learning / losing) -----------------------------------
  var rf = $("#rsFilter");
  if (rf) rf.addEventListener("click", function (e) { var b = e.target.closest("[data-cls]"); if (!b) return; $$("button", rf).forEach(function (x) { x.classList.toggle("on", x === b); }); $$("#rsGrid .cr-tile").forEach(function (t) { t.hidden = !!b.dataset.cls && t.dataset.cls !== b.dataset.cls; }); });

  // ---- selection + bulk (both grids) -------------------------------------------------------
  function bulkSetup(gridSel, chk, barSel, countSel, attr) {
    var g = $(gridSel), bar = $(barSel); if (!g || !bar) return;
    function selected() { return $$(".cr-tile", g).filter(function (t) { return t.querySelector(chk).checked; }); }
    function sync() { var s = selected(); bar.hidden = !s.length; $(countSel).textContent = s.length + " selected"; $$(".cr-tile", g).forEach(function (t) { t.classList.toggle("sel", t.querySelector(chk).checked); }); }
    var last = null;
    g.addEventListener("change", function (e) { if (e.target.matches(chk)) { last = e.target.closest(".cr-tile"); sync(); } });
    // shift-click on a checkbox selects the range from the last one; in Select mode a click anywhere on the tile toggles it
    g.addEventListener("click", function (e) {
      var t = e.target.closest(".cr-tile"); if (!t || t.hidden) return;
      var onBox = !!e.target.closest(chk);
      if (!onBox && !g.classList.contains("selmode")) return;
      if (!onBox) { if (e.target.closest("a, button, .cr-play")) return; e.preventDefault(); var c = t.querySelector(chk); c.checked = !c.checked; }
      if (e.shiftKey && last && last !== t) {
        var tiles = $$(".cr-tile", g).filter(function (x) { return !x.hidden; }), a = tiles.indexOf(last), b = tiles.indexOf(t), on = t.querySelector(chk).checked;
        if (a >= 0 && b >= 0) tiles.slice(Math.min(a, b), Math.max(a, b) + 1).forEach(function (x) { x.querySelector(chk).checked = on; });
      }
      last = t; sync();
    });
    document.addEventListener("keydown", function (e) { if (e.key === "Escape" && selected().length && !document.querySelector(".layer-bg")) { $$(chk, g).forEach(function (c) { c.checked = false; }); sync(); } });
    function ids(s) { return s.map(function (t) { return t.dataset.cids || t.dataset.cid; }).join(","); }
    bar.addEventListener("click", function (e) {
      var b = e.target.closest("[" + attr + "]"); if (!b) return;
      var s = selected(), k = b.getAttribute(attr);
      if (k === "clear") { $$(chk, g).forEach(function (c) { c.checked = false; }); sync(); return; }
      if (k === "all") { $$(".cr-tile", g).forEach(function (t) { if (!t.hidden) t.querySelector(chk).checked = true; }); sync(); return; }
      if (!s.length) return;
      if (k === "launch") { location.href = "/super-launcher?creatives=" + ids(s); return; }
      if (k === "pause") {
        var camps = [], advs = [];
        s.forEach(function (t) { (t.dataset.camps || "").split(",").forEach(function (c, i) { if (c) { camps.push(c); advs.push((t.dataset.advs || "").split(",")[i]); } }); });
        if (!camps.length) return;
        UI.confirm({ title: "Pause " + camps.length + " campaign" + (camps.length > 1 ? "s" : "") + "?", text: "Every campaign running the selected creatives is paused on TikTok. Undo for 10 s after.", ok: "Pause " + camps.length }).then(function (yes) {
          if (!yes) return;
          Promise.all(camps.map(function (c, i) { return UI.post("/campaigns/" + advs[i] + "/" + c + "/status", { operation_status: "DISABLE" }); })).then(function (ds) {
            var ok = ds.filter(function (d) { return d.ok; }).length;
            UI.undo("Paused " + ok + " campaign" + (ok > 1 ? "s" : ""), function () { return Promise.all(camps.map(function (c, i) { return UI.post("/campaigns/" + advs[i] + "/" + c + "/status", { operation_status: "ENABLE" }); })); });
          });
        });
        return;
      }
      if (k === "label") { labelPop(b, ids(s), function () { location.reload(); }); return; }
      var action = { fav: "favorite", archive: "archive", restore: "restore", delete: "delete" }[k];
      var go = function () { UI.post("/creatives/bulk", { action: action, ids: ids(s) }).then(function (d) { if (d.ok) { if (action === "archive") UI.undo("Archived " + d.n + " creative" + (d.n > 1 ? "s" : ""), function () { return UI.post("/creatives/bulk", { action: "restore", ids: ids(s) }).then(function () { location.reload(); }); }); setTimeout(function () { location.reload(); }, action === "archive" ? 900 : 0); } }); };
      if (action === "delete") UI.confirm({ title: "Delete " + s.length + " creative" + (s.length > 1 ? "s" : "") + " for good?", text: "Files are removed from disk; campaigns already launched with them keep running on TikTok. Images used by a carousel stay. Prefer Archive if you might want them back.", ok: "Delete " + s.length, danger: true }).then(function (y) { if (y) go(); });
      else go();
    });
  }
  bulkSetup("#rsGrid", ".rschk", "#rsSel", "#rsSelCount", "data-rs");
  bulkSetup("#libGrid", ".libchk", "#libSel", "#libSelCount", "data-lib");

  function labelPop(anchor, ids, after) {
    UI.get("/creatives/labels.json").then(function (d) {
      var html = '<div style="font-weight:700;margin-bottom:6px;">Add a label</div><div style="display:flex;gap:6px;"><input type="text" placeholder="hook A, UK, lucy…" maxlength="40" style="flex:1;"><button type="button" class="btn sm primary">Add</button></div>' +
        (d.labels && d.labels.length ? '<div style="display:flex;gap:4px;flex-wrap:wrap;margin-top:8px;">' + d.labels.slice(0, 12).map(function (l) { return '<span class="chip" data-l="' + esc(l[0]) + '" style="cursor:pointer;">' + esc(l[0]) + ' <span class="muted">' + l[1] + "</span></span>"; }).join("") + "</div>" : "");
      var p = UI.popover(anchor, html), inp = p.querySelector("input");
      function add(l) { l = (l || "").trim().toLowerCase(); if (!l) return; p.close(); UI.post("/creatives/bulk", { action: "label", ids: ids, label: l }).then(function () { if (after) after(l); }); }
      p.querySelector(".primary").addEventListener("click", function () { add(inp.value); });
      inp.addEventListener("keydown", function (e) { if (e.key === "Enter") add(inp.value); });
      p.addEventListener("click", function (e) { var c = e.target.closest("[data-l]"); if (c) add(c.dataset.l); });
    });
  }

  // ---- drawer: one creative --------------------------------------------------------------
  var drawer = null;
  function openDrawer(cid) {
    if (drawer) drawer.close();
    drawer = UI.drawer({ title: "…", sub: "", body: '<div class="muted">Loading…</div>', onClose: function () { drawer = null; } });
    UI.get("/creatives/" + cid + "/detail").then(function (d) {
      if (!drawer) return;
      if (d.error) { drawer.body.innerHTML = '<div class="flash err">' + esc(d.error) + "</div>"; return; }
      drawer.setTitle(d.name, esc(d.kind) + (d.slides ? " · " + d.slides + " slides" : "") + " · " + d.size_mb + " MB · <span class='pill " + (d.status === "available" ? "ok" : "mute") + "'>" + (d.status === "available" ? "fresh" : d.status) + "</span>" + (d.archived ? " · <span class='pill err'>archived</span>" : ""));
      var t = d.today, a = d.alltime;
      var media = d.kind === "video" && d.file ? '<video src="' + esc(d.file) + '" controls playsinline preload="metadata" poster="' + esc(d.poster) + '"></video>' : (d.kind === "carousel" ? "" : '<img src="' + esc(d.poster) + '" alt="">');
      var html = '<div class="dw-phone">' + media + "</div>" +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">' +
        '<div class="kpi"><div class="lab">Today</div><div class="val">' + (t.tests ? profit(t.profit) : '<span class="muted">—</span>') + '</div><div class="sub">' + (t.tests ? roas(t.roas) + " · " + t.tests + " test" + (t.tests > 1 ? "s" : "") + (t.epc ? " · EPC " + money(t.epc) : "") : "no spend today") + "</div></div>" +
        '<div class="kpi"><div class="lab">All time</div><div class="val">' + (a.tests ? profit(a.profit) : '<span class="muted">—</span>') + '</div><div class="sub">' + (a.tests ? roas(a.roas) + " · " + a.tests + " test" + (a.tests > 1 ? "s" : "") + " · " + money(a.spend, 0) + " spend" : "never launched") + "</div></div></div>" +
        '<div><div class="drawer-sec">Details</div><div style="display:grid;grid-template-columns:1fr 1fr;gap:4px 10px;font-size:12px;">' +
        '<div><span class="muted">Uploaded</span><br>' + esc(d.uploaded_str || "—") + ' <span class="muted">(' + esc(d.uploaded_ago) + ')</span></div><div><span class="muted">Source</span><br><span class="mono">' + esc(d.source || "—") + '</span></div>' +
        (d.music ? '<div><span class="muted">Music</span><br>♫ ' + esc(d.music) + "</div>" : "") + (d.variants.length ? '<div><span class="muted">Variants</span><br>' + d.variants.length + " other cop" + (d.variants.length > 1 ? "ies" : "y") + "</div>" : "") + "</div></div>" +
        '<div><div class="drawer-sec">Labels</div><div class="cr-labels" id="dwLabels">' + d.labels.map(function (l) { return '<span data-l="' + esc(l) + '" title="Remove">' + esc(l) + " ✕</span>"; }).join("") + '<button type="button" class="btn sm" id="dwLabelAdd">+ label</button></div></div>' +
        '<div><div class="drawer-sec">Note</div><textarea id="dwNote" rows="2" style="min-height:52px;font-family:var(--font-sans);font-size:12.5px;" placeholder="What this creative is, what to try next…">' + esc(d.note) + '</textarea><div class="muted" id="dwNoteSt" style="font-size:10.5px;margin-top:2px;">saves on its own</div></div>' +
        (d.by_account.length ? '<div><div class="drawer-sec">By account · best → worst</div>' + d.by_account.slice(0, 12).map(function (x) { return '<div style="display:flex;justify-content:space-between;gap:8px;font-size:12px;padding:4px 0;border-bottom:1px solid var(--border-soft);"><span style="min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;"><a href="/status?q=' + encodeURIComponent(x.campaign) + '" style="color:inherit;">' + esc(x.account) + "</a>" + (x.active ? ' <span class="dot on" style="margin-left:4px;"></span>' : "") + "</span><span style='flex:none;'>" + profit(x.profit) + " " + roas(x.roas) + "</span></div>"; }).join("") + "</div>" : "") +
        '<div style="display:flex;gap:6px;flex-wrap:wrap;"><a class="btn sm primary" href="/super-launcher?creatives=' + d.id + '">🚀 Launch with preset…</a><button type="button" class="btn sm" id="dwFav">' + (d.favorite ? "★ Favourited" : "☆ Favourite") + '</button><button type="button" class="btn sm" id="dwRename">Rename</button><button type="button" class="btn sm ghost" id="dwArchive">' + (d.archived ? "Restore" : "Archive") + '</button><button type="button" class="btn sm ghost danger" id="dwDelete">Delete</button></div>';
      drawer.body.innerHTML = html;
      if (d.kind === "carousel") UI.slides(drawer.body.querySelector(".dw-phone"), d.slide_ids || [], { poster: d.poster });
      var nt = $("#dwNote"), timer = null, last = d.note;
      nt.addEventListener("input", function () { clearTimeout(timer); $("#dwNoteSt").textContent = "…"; timer = setTimeout(function () { if (nt.value === last) { $("#dwNoteSt").textContent = "saved"; return; } UI.post("/notes/creative/" + d.id, { text: nt.value }).then(function (r) { last = nt.value; $("#dwNoteSt").textContent = r.ok ? "saved" : "couldn't save"; }); }, 700); });
      $("#dwFav").addEventListener("click", function () { var on = this.textContent.indexOf("★") === 0; UI.post("/creatives/bulk", { action: on ? "unfavorite" : "favorite", ids: String(d.id) }).then(function () { location.reload(); }); });
      $("#dwDelete").addEventListener("click", function () {
        UI.confirm({ title: "Delete “" + d.name + "” for good?", text: (d.status === "used" ? "It has been launched — the campaign on TikTok keeps its own copy, but its results disappear from Results here. Archive keeps the history instead. " : "") + "The file is removed from disk." + (d.kind === "carousel" ? " Its slide images stay." : ""), ok: "Delete", danger: true })
          .then(function (y) { if (!y) return; UI.post("/creatives/" + d.id + "/delete", {}).then(function (r) { if (!r.ok) { adopsToast && adopsToast("err", r.error || "Could not delete."); return; } drawer.close(); location.reload(); }); });
      });
      $("#dwArchive").addEventListener("click", function () { UI.post("/creatives/bulk", { action: d.archived ? "restore" : "archive", ids: String(d.id) }).then(function () { location.reload(); }); });
      $("#dwRename").addEventListener("click", function () {
        var p = UI.popover(this, '<div style="font-weight:700;margin-bottom:6px;">Rename</div><div style="display:flex;gap:6px;"><input type="text" value="' + esc(d.name) + '" style="flex:1;"><button type="button" class="btn sm primary">Save</button></div>');
        p.querySelector(".primary").addEventListener("click", function () { var v = p.querySelector("input").value.trim(); if (!v) return; UI.post("/creatives/" + d.id + "/rename", { name: v }).then(function () { location.reload(); }); });
      });
      $("#dwLabelAdd").addEventListener("click", function () { labelPop(this, String(d.id), function () { openDrawer(d.id); }); });
      $("#dwLabels").addEventListener("click", function (e) { var l = e.target.closest("[data-l]"); if (!l) return; UI.post("/creatives/bulk", { action: "unlabel", ids: String(d.id), label: l.dataset.l }).then(function () { openDrawer(d.id); }); });
    });
  }
  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest(".cr-tile");
    if (!t || e.target.closest("input, label, a, button, .cr-play")) return;
    if (t.closest(".selmode")) return;                       // Select mode: the tile click toggled the box instead
    openDrawer(t.dataset.cid);
  });
  $$(".cr-selmode").forEach(function (b) {
    b.addEventListener("click", function () {
      var g = $(b.dataset.grid); if (!g) return;
      var on = !g.classList.contains("selmode"); g.classList.toggle("selmode", on); b.classList.toggle("primary", on); b.textContent = on ? "☑ Selecting" : "☐ Select";
      if (!on) { $$("input[type=checkbox]", g).forEach(function (c) { c.checked = false; }); g.dispatchEvent(new Event("change", { bubbles: true })); var bar = $(g.id === "rsGrid" ? "#rsSel" : "#libSel"); if (bar) bar.hidden = true; $$(".cr-tile.sel", g).forEach(function (t) { t.classList.remove("sel"); }); }
    });
  });
})();
