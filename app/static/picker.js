/* picker.js — the creative picker pop-up shared by Launch, Presets and Creatives.
   UI.pickCreatives({kind, multi, selected, title}) → Promise<[{id,name,...}] | null>
   Depends on ui.js. Data: /creatives/pick.json */
(function () {
  "use strict";
  var esc = UI.esc, money = UI.money;
  function roas(r) { if (!r) return '<span class="roas-pill n">—</span>'; return '<span class="roas-pill ' + (r >= 1.3 ? "g" : (r >= 0.9 ? "w" : "r")) + '">' + r.toFixed(2) + "×</span>"; }
  UI.pickCreatives = function (o) {
    o = o || {};
    return new Promise(function (resolve) {
      var kind = o.kind || "video", state = o.state || "fresh", q = "", items = [], sel = {}, order = [], cur = null, done = false;
      (o.selected || []).forEach(function (id) { sel[id] = true; order.push(id); });
      var body = UI.el('<div class="pk"><div class="pk-top"><span class="seg pk-kind">' +
        '<button type="button" data-kind="video">Videos</button><button type="button" data-kind="carousel">Carousels</button><button type="button" data-kind="image">Images</button></span>' +
        '<input type="text" class="pk-q" placeholder="Search name, source, note…" style="width:220px;">' +
        '<span class="seg pk-state"><button type="button" data-state="fresh">Fresh <span class="n"></span></button><button type="button" data-state="used">Used <span class="n"></span></button><button type="button" data-state="all">All</button></span>' +
        '<span class="muted pk-count" style="margin-left:auto;font-size:12px;"></span></div>' +
        '<div class="pk-body"><div class="pk-grid"></div><div class="pk-side"><div class="muted" style="padding:20px 0;text-align:center;">Hover or click a tile to preview</div></div></div></div>');
      var foot = UI.el('<div style="display:flex;align-items:center;gap:8px;width:100%;"><b class="pk-sel">0 selected</b><span class="muted pk-hint" style="font-size:12px;"></span><button type="button" class="btn" data-close style="margin-left:auto;">Cancel</button><button type="button" class="btn primary pk-use">Use</button></div>');
      var m = UI.modal({ title: o.title || (o.multi ? "Pick creatives" : "Pick a creative"), body: body, footer: foot, wide: true, onClose: function () { if (!done) { done = true; resolve(null); } } });
      m.el.classList.add("pk-modal");
      var grid = body.querySelector(".pk-grid"), side = body.querySelector(".pk-side");
      if (!o.kinds) o.kinds = ["video", "carousel", "image"];
      body.querySelectorAll(".pk-kind button").forEach(function (b) { if (o.kinds.indexOf(b.dataset.kind) < 0) b.remove(); });
      function mark() {
        body.querySelectorAll(".pk-kind button").forEach(function (b) { b.classList.toggle("on", b.dataset.kind === kind); });
        body.querySelectorAll(".pk-state button").forEach(function (b) { b.classList.toggle("on", b.dataset.state === state); });
        var n = Object.keys(sel).filter(function (k) { return sel[k]; }).length;
        foot.querySelector(".pk-sel").textContent = n + " selected";
        var noun = o.noun || "creative";
        foot.querySelector(".pk-use").textContent = o.multi ? ("Use " + n + " " + noun + (n === 1 ? "" : "s")) : ("Use this " + noun);
        foot.querySelector(".pk-use").disabled = n === 0;
        foot.querySelector(".pk-hint").textContent = o.hint || "";
      }
      function tile(it) {
        var on = !!sel[it.id];
        return '<div class="pk-tile' + (on ? " on" : "") + '" data-id="' + it.id + '" title="' + esc(it.name) + '">' +
          '<div class="pk-thumb"><img loading="lazy" src="' + esc(it.poster) + '" alt=""><span class="pill ' + (it.state === "fresh" ? "ok" : "mute") + ' pk-state-pill">' + it.state + '</span>' +
          (it.slides ? '<span class="pk-dur">' + it.slides + ' slides</span>' : "") + '<span class="pk-chk">' + (on ? "✓" : "") + '</span></div>' +
          '<div class="pk-meta"><div class="pk-name">' + esc(it.name) + '</div><div class="muted" style="font-size:10.5px;">' + esc(it.uploaded_ago) + (it.variants > 1 ? " · " + it.variants + " variants" : "") + (it.note ? " · 📝" : "") + '</div></div></div>';
      }
      function render() {
        grid.dataset.kind = kind;          // images get the Images-shelf tile shape (4/5), video/carousel stay 9/13
        grid.innerHTML = items.length ? items.map(tile).join("") : '<div class="empty" style="grid-column:1/-1;">Nothing here' + (q ? " for “" + esc(q) + "”" : "") + ".</div>";
        mark();
      }
      function load() {
        grid.dataset.kind = kind;
        grid.innerHTML = '<div class="muted" style="grid-column:1/-1;padding:20px;">Loading…</div>';
        UI.get("/creatives/pick.json?kind=" + kind + "&state=" + state + "&q=" + encodeURIComponent(q)).then(function (d) {
          items = d.items || [];
          body.querySelector('.pk-state [data-state="fresh"] .n').textContent = d.counts ? d.counts.fresh : "";
          body.querySelector('.pk-state [data-state="used"] .n').textContent = d.counts ? d.counts.used : "";
          body.querySelector(".pk-count").textContent = items.length + " shown";
          render();
        });
      }
      function preview(it) {
        cur = it;
        var media = it.kind === "video" && it.file ? '<video src="' + esc(it.file) + '" controls playsinline preload="metadata" poster="' + esc(it.poster) + '"></video>' : (it.kind === "carousel" ? "" : '<img src="' + esc(it.poster) + '" alt="">');
        side.innerHTML = '<div class="pk-phone">' + media + '</div><div class="pk-info"><b>' + esc(it.name) + '</b>' +
          '<div class="muted" style="font-size:11.5px;">' + esc(it.kind) + (it.slides ? " · " + it.slides + " slides" : "") + (it.music ? " · ♫ " + esc(it.music) : "") + " · " + it.size_mb + " MB</div>" +
          '<div style="margin-top:8px;display:flex;gap:6px;align-items:center;"><span class="pill ' + (it.state === "fresh" ? "ok" : "mute") + '">' + it.state + '</span><span class="muted" style="font-size:11.5px;">uploaded ' + esc(it.uploaded_ago) + '</span></div>' +
          (it.spend || it.revenue ? '<div style="margin-top:8px;font-size:12.5px;">All time: <span class="profit-cell ' + (it.profit >= 0 ? "pos" : "neg") + '">' + (it.profit > 0 ? "+" : "") + money(it.profit, 0) + "</span> · " + roas(it.roas) + ' <span class="muted">' + money(it.spend, 0) + " spend</span></div>" : '<div class="muted" style="margin-top:8px;font-size:12px;">Never launched.</div>') +
          (it.used_in ? '<div class="muted" style="font-size:11.5px;margin-top:4px;">last in ' + esc(it.used_in) + " · " + esc(it.used_account) + "</div>" : "") +
          (it.note ? '<div class="muted" style="font-size:12px;margin-top:8px;border-left:2px solid var(--border-strong);padding-left:8px;">' + esc(it.note) + "</div>" : "") +
          '<div style="margin-top:10px;"><button type="button" class="btn sm ' + (sel[it.id] ? "" : "primary") + ' pk-toggle">' + (sel[it.id] ? "Remove" : (o.multi ? "Add" : "Select")) + "</button></div></div>";
        if (it.kind === "carousel") UI.slides(side.querySelector(".pk-phone"), it.slide_ids || [], { poster: it.poster });
        side.querySelector(".pk-toggle").addEventListener("click", function () { toggle(it.id); preview(it); });
      }
      function toggle(id) {
        if (!o.multi) { Object.keys(sel).forEach(function (k) { sel[k] = false; }); order = []; }
        sel[id] = !sel[id];
        if (sel[id]) order.push(id); else order = order.filter(function (x) { return x !== id; });
        var t = grid.querySelector('.pk-tile[data-id="' + id + '"]'); if (t) { t.classList.toggle("on", !!sel[id]); t.querySelector(".pk-chk").textContent = sel[id] ? "✓" : ""; }
        if (!o.multi) grid.querySelectorAll(".pk-tile.on").forEach(function (x) { if (x.dataset.id != id) { x.classList.remove("on"); x.querySelector(".pk-chk").textContent = ""; } });
        mark();
      }
      grid.addEventListener("click", function (e) {
        var t = e.target.closest(".pk-tile"); if (!t) return;
        var it = items.filter(function (x) { return x.id == t.dataset.id; })[0]; if (!it) return;
        if (e.target.closest(".pk-thumb")) { toggle(it.id); }
        preview(it);
      });
      grid.addEventListener("mouseover", function (e) { var t = e.target.closest(".pk-tile"); if (!t) return; var it = items.filter(function (x) { return x.id == t.dataset.id; })[0]; if (it && (!cur || cur.id !== it.id) && !(side.querySelector("video") && !side.querySelector("video").paused)) preview(it); });
      body.querySelector(".pk-kind").addEventListener("click", function (e) { var b = e.target.closest("[data-kind]"); if (!b) return; kind = b.dataset.kind; if (!o.multi) { sel = {}; order = []; } load(); });
      body.querySelector(".pk-state").addEventListener("click", function (e) { var b = e.target.closest("[data-state]"); if (!b) return; state = b.dataset.state; load(); });
      var qt = null; body.querySelector(".pk-q").addEventListener("input", function (e) { clearTimeout(qt); q = e.target.value; qt = setTimeout(load, 250); });
      foot.querySelector(".pk-use").addEventListener("click", function () {
        var chosen = order.filter(function (id) { return sel[id]; }).map(function (id) { return items.filter(function (x) { return x.id == id; })[0] || { id: id, name: "#" + id, kind: kind }; });
        done = true; m.close(); resolve(chosen);
      });
      mark(); load();
    });
  };

  /* UI.pickSpark({selected, title}) → Promise<{id,name,creator,type,state,thumb,post_url}|null>
     One creator post (spark code) from the Sparks list — search, fresh/used, post link. Data: /spark-codes/pick.json */
  UI.pickSpark = function (o) {
    o = o || {};
    if (o.multi) return pickSparks(o);
    return new Promise(function (resolve) {
      var state = o.state || "fresh", q = "", items = [], sel = o.selected || null, done = false;
      var body = UI.el('<div class="pk"><div class="pk-top">' +
        '<input type="text" class="pk-q" placeholder="Search name, creator, code, source…" style="width:260px;">' +
        '<span class="seg pk-state"><button type="button" data-state="fresh">Fresh <span class="n"></span></button><button type="button" data-state="used">Used <span class="n"></span></button><button type="button" data-state="all">All</button></span>' +
        '<a href="/spark-codes" class="btn sm" style="margin-left:auto;">＋ Add codes</a></div>' +
        '<div class="sp-list"></div></div>');
      var foot = UI.el('<div style="display:flex;align-items:center;gap:8px;width:100%;"><span class="muted pk-hint" style="font-size:12px;">The creator\'s post runs as the ad under their identity; the preset\'s text, CTA and destination still apply.</span><button type="button" class="btn" data-close style="margin-left:auto;">Cancel</button><button type="button" class="btn primary pk-use" disabled>Use this spark code</button></div>');
      var m = UI.modal({ title: o.title || "Pick a spark code", body: body, footer: foot, wide: true, onClose: function () { if (!done) { done = true; resolve(null); } } });
      m.el.classList.add("pk-modal");
      var list = body.querySelector(".sp-list");
      function mark() {
        body.querySelectorAll(".pk-state button").forEach(function (b) { b.classList.toggle("on", b.dataset.state === state); });
        list.querySelectorAll(".sp-row").forEach(function (r) { r.classList.toggle("on", !!sel && r.dataset.id == sel); });
        foot.querySelector(".pk-use").disabled = !sel;
      }
      function row(it) {
        return '<div class="sp-row" data-id="' + it.id + '">' +
          (it.thumb ? '<img src="' + esc(it.thumb) + '" alt="" loading="lazy">' : '<span class="sp-ph">✦</span>') +
          '<div class="sp-main"><b>' + esc(it.name) + '</b>' + (it.creator ? ' <span class="muted">· @' + esc(it.creator.replace(/^@/, "")) + '</span>' : "") +
          '<div class="muted" style="font-size:11px;">' + esc(it.type) + (it.source ? " · source " + esc(it.source) : "") + (it.uses ? " · used " + it.uses + "×" + (it.last_used ? " · last " + esc(it.last_used) : "") : (it.added ? " · added " + esc(it.added) : "")) + '</div></div>' +
          '<span class="pill ' + (it.state === "fresh" ? "ok" : "mute") + '">' + esc(it.state) + '</span>' +
          (it.post_url ? '<a href="' + esc(it.post_url) + '" target="_blank" rel="noopener" class="muted" style="font-size:11px;" title="Open the post">post ↗</a>' : "") +
          '<span class="sp-chk">' + (sel == it.id ? "✓" : "") + '</span></div>';
      }
      function load() {
        list.innerHTML = '<div class="muted" style="padding:20px;">Loading…</div>';
        UI.get("/spark-codes/pick.json?state=" + state + "&q=" + encodeURIComponent(q)).then(function (d) {
          items = d.items || [];
          body.querySelector('.pk-state [data-state="fresh"] .n').textContent = d.counts ? d.counts.fresh : "";
          body.querySelector('.pk-state [data-state="used"] .n').textContent = d.counts ? d.counts.used : "";
          list.innerHTML = items.length ? items.map(row).join("") : '<div class="empty">No spark codes' + (q ? " for “" + esc(q) + "”" : (state === "fresh" ? " left unused — try Used or All, or add codes" : "")) + ".</div>";
          mark();
        });
      }
      list.addEventListener("click", function (e) {
        if (e.target.closest("a")) return;
        var r = e.target.closest(".sp-row"); if (!r) return;
        sel = r.dataset.id; mark(); list.querySelectorAll(".sp-chk").forEach(function (c) { c.textContent = c.closest(".sp-row").dataset.id == sel ? "✓" : ""; });
      });
      list.addEventListener("dblclick", function (e) { var r = e.target.closest(".sp-row"); if (r) { sel = r.dataset.id; foot.querySelector(".pk-use").click(); } });
      body.querySelector(".pk-state").addEventListener("click", function (e) { var b = e.target.closest("[data-state]"); if (!b) return; state = b.dataset.state; load(); });
      var qt = null; body.querySelector(".pk-q").addEventListener("input", function (e) { clearTimeout(qt); q = e.target.value; qt = setTimeout(load, 250); });
      foot.querySelector(".pk-use").addEventListener("click", function () {
        var it = items.filter(function (x) { return x.id == sel; })[0]; if (!it) return;
        done = true; m.close(); resolve(it);
      });
      setTimeout(function () { try { body.querySelector(".pk-q").focus(); } catch (e) {} }, 30);
      mark(); load();
    });
  };
  /* UI.pickSpark({multi:true, selected:[ids]}) → Promise<[{id,name,creator,type,state,thumb,post_url}] | null>
     Several creator posts at once, for the launcher's board; same list, ticks instead of a radio. */
  function pickSparks(o) {
    return new Promise(function (resolve) {
      var state = o.state || "fresh", q = "", items = [], sel = {}, order = [], done = false;
      (o.selected || []).forEach(function (id) { sel[id] = true; order.push(String(id)); });
      var body = UI.el('<div class="pk"><div class="pk-top">' +
        '<input type="text" class="pk-q" placeholder="Search name, creator, code, source…" style="width:260px;">' +
        '<span class="seg pk-state"><button type="button" data-state="fresh">Fresh <span class="n"></span></button><button type="button" data-state="used">Used <span class="n"></span></button><button type="button" data-state="all">All</button></span>' +
        '<button type="button" class="btn sm sp-all" style="margin-left:auto;">Select all shown</button></div>' +
        '<div class="sp-list"></div></div>');
      var foot = UI.el('<div class="pv-foot"><b class="pk-sel">0 selected</b><span class="muted pk-hint">Each post runs under its creator\'s identity; the preset\'s text, CTA and destination still apply.</span><span class="pv-foot-btns"><button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary pk-use" disabled>Use</button></span></div>');
      var m = UI.modal({ title: o.title || "Pick spark codes", body: body, footer: foot, wide: true, onClose: function () { if (!done) { done = true; resolve(null); } } });
      m.el.classList.add("pk-modal");
      var list = body.querySelector(".sp-list");
      function n() { return order.filter(function (id) { return sel[id]; }).length; }
      function mark() {
        body.querySelectorAll(".pk-state button").forEach(function (b) { b.classList.toggle("on", b.dataset.state === state); });
        list.querySelectorAll(".sp-row").forEach(function (r) { r.classList.toggle("on", !!sel[r.dataset.id]); r.querySelector(".sp-chk").textContent = sel[r.dataset.id] ? "✓" : ""; });
        var k = n(); foot.querySelector(".pk-sel").textContent = k + " selected";
        foot.querySelector(".pk-use").textContent = "Use " + k + " spark code" + (k === 1 ? "" : "s"); foot.querySelector(".pk-use").disabled = !k;
        body.querySelector(".sp-all").textContent = items.length && items.every(function (it) { return sel[it.id]; }) ? "None shown" : "Select all shown";
      }
      function row(it) {
        return '<div class="sp-row" data-id="' + it.id + '">' +
          (it.thumb ? '<img src="' + esc(it.thumb) + '" alt="" loading="lazy">' : '<span class="sp-ph">✦</span>') +
          '<div class="sp-main"><b>' + esc(it.name) + '</b>' + (it.creator ? ' <span class="muted">· @' + esc(it.creator.replace(/^@/, "")) + '</span>' : "") +
          '<div class="muted" style="font-size:11px;">' + esc(it.type) + (it.source ? " · source " + esc(it.source) : "") + (it.uses ? " · used " + it.uses + "×" + (it.last_used ? " · last " + esc(it.last_used) : "") : (it.added ? " · added " + esc(it.added) : "")) + '</div></div>' +
          '<span class="pill ' + (it.state === "fresh" ? "ok" : "mute") + '">' + esc(it.state) + '</span>' +
          (it.post_url ? '<a href="' + esc(it.post_url) + '" target="_blank" rel="noopener" class="muted" style="font-size:11px;" title="Open the post">post ↗</a>' : "") +
          '<span class="sp-chk"></span></div>';
      }
      function load() {
        list.innerHTML = '<div class="muted" style="padding:20px;">Loading…</div>';
        UI.get("/spark-codes/pick.json?state=" + state + "&q=" + encodeURIComponent(q)).then(function (d) {
          items = d.items || [];
          body.querySelector('.pk-state [data-state="fresh"] .n').textContent = d.counts ? d.counts.fresh : "";
          body.querySelector('.pk-state [data-state="used"] .n').textContent = d.counts ? d.counts.used : "";
          list.innerHTML = items.length ? items.map(row).join("") : '<div class="empty">No spark codes' + (q ? " for “" + esc(q) + "”" : (state === "fresh" ? " left unused — try Used or All, or paste codes" : "")) + ".</div>";
          items.forEach(function (it) { if (sel[it.id]) sel[String(it.id)] = it; });
          mark();
        });
      }
      function toggle(id, on) { id = String(id); var it = items.filter(function (x) { return String(x.id) === id; })[0]; if (on === undefined) on = !sel[id]; if (on) { sel[id] = it || sel[id] || true; if (order.indexOf(id) < 0) order.push(id); } else { delete sel[id]; order = order.filter(function (x) { return x !== id; }); } }
      list.addEventListener("click", function (e) {
        if (e.target.closest("a")) return;
        var r = e.target.closest(".sp-row"); if (!r) return;
        toggle(r.dataset.id); mark();
      });
      body.querySelector(".sp-all").addEventListener("click", function () { var all = items.length && items.every(function (it) { return sel[it.id]; }); items.forEach(function (it) { toggle(it.id, !all); }); mark(); });
      body.querySelector(".pk-state").addEventListener("click", function (e) { var b = e.target.closest("[data-state]"); if (!b) return; state = b.dataset.state; load(); });
      var qt = null; body.querySelector(".pk-q").addEventListener("input", function (e) { clearTimeout(qt); q = e.target.value; qt = setTimeout(load, 250); });
      foot.querySelector(".pk-use").addEventListener("click", function () {
        // picks made on an earlier page/filter and not loaded again keep only their id; the caller already knows those
        var chosen = order.filter(function (id) { return sel[id]; }).map(function (id) { return typeof sel[id] === "object" ? sel[id] : { id: id }; });
        done = true; m.close(); resolve(chosen);
      });
      setTimeout(function () { try { body.querySelector(".pk-q").focus(); } catch (e) {} }, 30);
      mark(); load();
    });
  }
  /* UI.pickProfileVideos({bcs:[{id,name,accounts}], selected:[{item_id,...}]}) → Promise<[{item_id, identity_id, identity_type, bc_id, handle, text, cover, type, auth_code, url}] | null>
     Every post of every profile the viewer's Business Centers share, loaded BC by BC and shown as it arrives;
     filter by profile (not BC), multi-select, preview plays the post. Data: /super-launcher/profile-videos.json?bc_id= */
  // Profile-video posts cached per Business Center for the page session, so the picker
  // opens instantly on what's already loaded. UI.warmProfileVideos() fills it in the
  // background (up to 5 BCs at once) on page load, so there's nothing to wait for.
  var PV_STORE = {};                 // bc_id -> { profiles:[…], error:"" }
  var PV_CONCURRENCY = 5;
  function pvFetchBc(b, refresh) {
    return UI.get("/super-launcher/profile-videos.json?bc_id=" + encodeURIComponent(b.id) + (refresh ? "&refresh=1" : ""))
      .then(function (d) {
        var e = { profiles: [], error: "" };
        if (d && d.ok === false) e.error = (d.error || "TikTok didn't answer");
        ((d && d.profiles) || []).forEach(function (p) { p.bc_name = b.name; e.profiles.push(p); });
        PV_STORE[b.id] = e; return e;
      })
      .catch(function () { PV_STORE[b.id] = { profiles: [], error: "couldn't reach the dashboard" }; return PV_STORE[b.id]; });
  }
  function pvPump(bcs, refresh, onEach, onDone) {
    var i = 0, active = 0;
    function fin() { active--; step(); }
    function step() {
      while (active < PV_CONCURRENCY && i < bcs.length) {
        var b = bcs[i++]; active++;
        (function (b) {
          if (!refresh && PV_STORE[b.id]) { Promise.resolve().then(function () { if (onEach) onEach(b, PV_STORE[b.id]); fin(); }); return; }
          pvFetchBc(b, refresh).then(function (e) { if (onEach) onEach(b, e); }).then(fin);
        })(b);
      }
      if (i >= bcs.length && active === 0 && onDone) onDone();
    }
    if (bcs.length) step(); else if (onDone) onDone();
  }
  UI.warmProfileVideos = function (bcs, refresh) { if (bcs && bcs.length) pvPump(bcs, !!refresh, null, null); };

  UI.pickProfileVideos = function (o) {
    o = o || {};
    return new Promise(function (resolve) {
      var bcs = o.bcs || [], q = "", profiles = [], sel = {}, order = [], cur = null, done = false, loading = 0, pf = "", errors = [];
      var favs = {}; (o.favorites || []).forEach(function (id) { favs[String(id)] = true; });
      var userPickedFilter = false;                 // once the operator picks a filter, stop auto-defaulting
      function isFav(p) { return !!favs[String(p.identity_id)]; }
      (o.selected || []).forEach(function (v) { sel[v.item_id] = v; order.push(v.item_id); });
      var body = UI.el('<div class="pk"><div class="pk-top">' +
        '<select class="pv-profile" style="max-width:280px;"><option value="">All profiles</option></select>' +
        '<input type="text" class="pk-q" placeholder="Search caption or profile…" style="width:220px;">' +
        '<button type="button" class="btn sm pv-refresh" title="Re-read every profile from TikTok (the list is kept for 10 minutes; covers and previews expire after about an hour)">↻ Refresh</button>' +
        '<span class="muted pk-count" style="margin-left:auto;font-size:12px;"></span></div>' +
        '<div class="pk-body"><div class="pk-grid" data-kind="video"></div><div class="pk-side"><div class="muted" style="padding:20px 0;text-align:center;">Hover or click a post to preview</div></div></div></div>');
      var foot = UI.el('<div class="pv-foot"><b class="pk-sel">0 selected</b><span class="muted pk-hint">Each post runs under its profile\'s identity — no spark code needed.</span><span class="pv-foot-btns"><button type="button" class="btn" data-close>Cancel</button><button type="button" class="btn primary pk-use">Use</button></span></div>');
      var m = UI.modal({ title: o.title || "Pick profile videos", body: body, footer: foot, wide: true, onClose: function () { if (!done) { done = true; resolve(null); } } });
      m.el.classList.add("pk-modal");
      var grid = body.querySelector(".pk-grid"), side = body.querySelector(".pk-side"), pfSel = body.querySelector(".pv-profile");
      function key(p) { return p.bc_id + ":" + p.identity_id; }
      function shownProfiles() { return profiles.filter(function (p) { return pf === "__fav__" ? isFav(p) : (!pf || key(p) === pf); }); }
      function shown(p) { var qq = q.toLowerCase(); return p.videos.filter(function (v) { return !qq || (v.text || "").toLowerCase().indexOf(qq) >= 0 || (p.name || "").toLowerCase().indexOf(qq) >= 0; }); }
      function mark() {
        var n = order.filter(function (id) { return sel[id]; }).length;
        foot.querySelector(".pk-sel").textContent = n + " selected";
        foot.querySelector(".pk-use").textContent = "Use " + n + " video" + (n === 1 ? "" : "s");
        foot.querySelector(".pk-use").disabled = n === 0;
      }
      function fillProfiles() {
        var total = profiles.reduce(function (a, p) { return a + p.videos.length; }, 0);
        var favProfs = profiles.filter(isFav), favPosts = favProfs.reduce(function (a, p) { return a + p.videos.length; }, 0);
        if (!userPickedFilter) pf = favProfs.length ? "__fav__" : "";     // default to favorites once they've loaded
        pfSel.innerHTML =
          (favProfs.length ? '<option value="__fav__">★ Favorites · ' + favProfs.length + ' profile' + (favProfs.length === 1 ? '' : 's') + ' · ' + favPosts + ' posts</option>' : '') +
          '<option value="">All profiles · ' + total + ' posts</option>' +
          profiles.slice().sort(function (a, b) { return a.name.localeCompare(b.name); }).map(function (p) { return '<option value="' + esc(key(p)) + '">' + (isFav(p) ? '★ ' : '') + '@' + esc(p.name) + ' · ' + p.videos.length + (p.bc_name ? ' · ' + esc(p.bc_name) : '') + '</option>'; }).join("");
        if (pf === "__fav__" && !favProfs.length) pf = "";
        pfSel.value = pf;
        if (pfSel.value !== pf) pf = pfSel.value || "";                    // selected profile scrolled out of the list
      }
      function tile(p, v) {
        var on = !!sel[v.item_id];
        return '<div class="pk-tile' + (on ? " on" : "") + '" data-id="' + esc(v.item_id) + '" data-pid="' + esc(key(p)) + '" title="' + esc(v.text || v.item_id) + '">' +
          '<div class="pk-thumb">' + (v.cover ? '<img loading="lazy" src="' + esc(v.cover) + '" alt="">' : '<span class="sp-ph" style="position:absolute;inset:0;display:flex;align-items:center;justify-content:center;">✦</span>') +
          '<span class="pv-type">' + esc(v.type === "carousel" ? (v.slides ? v.slides + " photos" : "photos") : (v.duration ? v.duration + "s" : "video")) + '</span><span class="pk-chk">' + (on ? "✓" : "") + '</span></div>' +
          '<div class="pk-meta"><div class="pk-name">' + esc(v.text || ("post " + v.item_id.slice(-6))) + '</div><div class="muted" style="font-size:10.5px;">@' + esc(p.name) + (v.created ? " · " + esc(v.created.slice(0, 10)) : "") + '</div></div></div>';
      }
      function render() {
        var html = "", total = 0, vis = 0, list = shownProfiles();
        list.forEach(function (p) {
          var vs = shown(p); total += p.videos.length; vis += vs.length;
          if (!vs.length && q) return;
          var picked = p.videos.filter(function (v) { return sel[v.item_id]; }).length;
          html += '<div class="pv-head">' +
            '<button type="button" class="pv-star' + (isFav(p) ? ' on' : '') + '" data-idn="' + esc(p.identity_id) + '" title="' + (isFav(p) ? 'Remove from favorites' : 'Favorite this profile') + '">' + (isFav(p) ? '★' : '☆') + '</button>' +
            (p.avatar ? '<img src="' + esc(p.avatar) + '" alt="">' : '<span class="pv-ph">@</span>') + '<b>@' + esc(p.name) + '</b><span class="muted">' + p.videos.length + ' post' + (p.videos.length === 1 ? '' : 's') + (p.bc_name ? ' · ' + esc(p.bc_name) : '') + (picked ? ' · ' + picked + ' picked' : '') + (p.error ? ' · <span style="color:var(--err);">' + esc(p.error) + '</span>' : '') + '</span>' +
            (vs.length ? '<button type="button" class="btn sm pv-all" data-pid="' + esc(key(p)) + '">' + (vs.every(function (v) { return sel[v.item_id]; }) ? "None" : "Select all") + '</button>' : '') + '</div>';
          html += vs.map(function (v) { return tile(p, v); }).join("");
          if (!vs.length) html += '<div class="muted pv-empty" style="padding:4px 6px 10px;font-size:12px;">No ad-usable posts on this profile.</div>';
        });
        if (loading) html += '<div class="muted pv-empty" style="padding:10px 6px;font-size:12px;">Reading ' + loading + ' more Business Center' + (loading === 1 ? "" : "s") + '…</div>';
        if (errors.length) html += '<div class="muted pv-empty" style="padding:4px 6px;font-size:12px;color:var(--err);">' + errors.map(esc).join("<br>") + '</div>';
        grid.innerHTML = (list.length || loading) ? html
          : (pf === "__fav__" ? '<div class="empty pv-empty">No favorite profiles yet — choose “All profiles” above and tap ☆ on the ones you use, so this window opens on just those next time.</div>'
             : '<div class="empty pv-empty">No profiles shared on your Business Centers' + (q ? " match “" + esc(q) + "”" : "") + '.</div>');
        body.querySelector(".pk-count").textContent = profiles.length ? (profiles.length + " profile" + (profiles.length === 1 ? "" : "s") + " · " + ((q || pf) ? vis + " of " : "") + profiles.reduce(function (a, p) { return a + p.videos.length; }, 0) + " post" + (total === 1 ? "" : "s")) : "";
        mark();
      }
      function load(refresh) {
        errors = []; profiles = [];
        // instant: show whatever was already warmed in the background on page load
        if (!refresh) bcs.forEach(function (b) { var e = PV_STORE[b.id]; if (e) { e.profiles.forEach(function (p) { profiles.push(p); }); if (e.error) errors.push(b.name + ": " + e.error); } });
        var pending = bcs.filter(function (b) { return refresh || !PV_STORE[b.id]; });
        loading = pending.length; fillProfiles(); render();
        if (!bcs.length) { grid.innerHTML = '<div class="empty pv-empty">No Business Center with a connected account in this view.</div>'; return; }
        if (!pending.length) { loading = 0; render(); return; }
        // fetch only the Business Centers not already cached (5 at a time), streaming each in
        pvPump(pending, refresh, function (b, e) {
          e.profiles.forEach(function (p) { profiles.push(p); });
          if (e.error) errors.push(b.name + ": " + e.error);
          loading = Math.max(loading - 1, 0); fillProfiles(); render();
        }, function () { loading = 0; fillProfiles(); render(); });
      }
      function find(id) { var hit = null; profiles.forEach(function (p) { p.videos.forEach(function (v) { if (v.item_id == id) hit = { p: p, v: v }; }); }); return hit; }
      function entry(p, v) { return { item_id: v.item_id, identity_id: p.identity_id, identity_type: p.identity_type, bc_id: p.bc_id, handle: p.name, text: v.text, cover: v.cover, preview: v.preview || "", type: v.type, auth_code: v.auth_code, url: v.url, duration: v.duration || 0, slides: v.slides || 0 }; }
      function preview(p, v) {
        cur = v;
        var media = v.preview ? '<video src="' + esc(v.preview) + '" controls playsinline preload="metadata"' + (v.cover ? ' poster="' + esc(v.cover) + '"' : '') + '></video>' : (v.cover ? '<img src="' + esc(v.cover) + '" alt="">' : '<div class="muted" style="padding:40px 10px;text-align:center;font-size:12px;">No preview from TikTok for this post — open it ↗</div>');
        side.innerHTML = '<div class="pk-phone">' + media + '</div><div class="pk-info"><b>@' + esc(p.name) + '</b>' +
          '<div style="font-size:12.5px;margin-top:4px;">' + esc(v.text || "(no caption)") + '</div>' +
          '<div class="muted" style="font-size:11.5px;margin-top:6px;">' + esc(v.type === "carousel" ? (v.slides ? v.slides + " photos" : "photo post") : "video") + (v.created ? " · " + esc(v.created) : "") + (v.duration ? " · " + v.duration + "s" : "") + (v.url ? ' · <a href="' + esc(v.url) + '" target="_blank" rel="noopener">open post ↗</a>' : "") + '</div>' +
          '<div style="margin-top:10px;"><button type="button" class="btn sm ' + (sel[v.item_id] ? "" : "primary") + ' pk-toggle">' + (sel[v.item_id] ? "Remove" : "Add") + "</button></div></div>";
        side.querySelector(".pk-toggle").addEventListener("click", function () { toggle(p, v); preview(p, v); });
      }
      function toggle(p, v) {
        if (sel[v.item_id]) { delete sel[v.item_id]; order = order.filter(function (x) { return x !== v.item_id; }); }
        else { sel[v.item_id] = entry(p, v); order.push(v.item_id); }
        render();
      }
      grid.addEventListener("click", function (e) {
        var st = e.target.closest(".pv-star");
        if (st) {
          var idn = String(st.dataset.idn), on = !favs[idn];
          if (on) favs[idn] = true; else delete favs[idn];
          if (o.onToggleFav) { try { o.onToggleFav(idn, on); } catch (_e) {} }
          fillProfiles(); render(); return;
        }
        var a = e.target.closest(".pv-all");
        if (a) { var p = profiles.filter(function (x) { return key(x) === a.dataset.pid; })[0]; if (!p) return; var vs = shown(p), every = vs.every(function (v) { return sel[v.item_id]; }); vs.forEach(function (v) { if (every ? sel[v.item_id] : !sel[v.item_id]) { if (sel[v.item_id]) { delete sel[v.item_id]; order = order.filter(function (x) { return x !== v.item_id; }); } else { sel[v.item_id] = entry(p, v); order.push(v.item_id); } } }); render(); return; }
        var t = e.target.closest(".pk-tile"); if (!t) return;
        var hit = find(t.dataset.id); if (!hit) return;
        if (e.target.closest(".pk-thumb")) toggle(hit.p, hit.v);
        preview(hit.p, hit.v);
      });
      grid.addEventListener("mouseover", function (e) { var t = e.target.closest(".pk-tile"); if (!t) return; var hit = find(t.dataset.id); if (hit && (!cur || cur.item_id !== hit.v.item_id) && !(side.querySelector("video") && !side.querySelector("video").paused)) preview(hit.p, hit.v); });
      pfSel.addEventListener("change", function () { userPickedFilter = true; pf = pfSel.value; render(); });
      body.querySelector(".pv-refresh").addEventListener("click", function () { if (!loading) load(true); });
      var qt = null; body.querySelector(".pk-q").addEventListener("input", function (e) { clearTimeout(qt); q = e.target.value; qt = setTimeout(render, 150); });
      foot.querySelector(".pk-use").addEventListener("click", function () {
        var chosen = order.filter(function (id) { return sel[id]; }).map(function (id) { return sel[id]; });
        done = true; m.close(); resolve(chosen);
      });
      mark(); load(false);
    });
  };
  /* UI.uploadCreatives({title}) → Promise<[{id,name,kind,poster,file}] | null>
     Drop or choose MP4/MOV/WEBM (and PNG/JPG/WEBP) — they land in the library as fresh,
     with an upload bar; the caller (the launcher's board) selects what landed.
     Data: POST /creatives/upload (fetch → JSON). */
  UI.uploadCreatives = function (o) {
    o = o || {};
    return new Promise(function (resolve) {
      var done = false, files = [], busy = false;
      var body = UI.el('<div class="up"><div class="up-drop" tabindex="0"><b>Drop videos here</b><div class="muted" style="font-size:12px;margin-top:4px;">or click to choose · MP4, MOV, WEBM (images too) · up to 500 MB each</div><input type="file" multiple accept="video/mp4,video/quicktime,video/webm,image/png,image/jpeg,image/webp" hidden></div>' +
        '<div class="up-list"></div>' +
        '<label class="field" style="margin:10px 0 0;"><span class="field-label">Source prefix <span class="muted">(optional)</span></span><input type="text" name="source_prefix" placeholder="e.g. sept_hoodie — each file gets prefix_id as its source" style="width:100%;"></label>' +
        '<div class="up-bar" hidden><div class="up-fill"></div></div><div class="muted up-msg" style="font-size:12px;margin-top:6px;"></div></div>');
      var foot = UI.el('<div style="display:flex;align-items:center;gap:8px;width:100%;"><span class="muted up-hint" style="font-size:12px;">They land in the library as fresh and are added to this launch.</span><button type="button" class="btn" data-close style="margin-left:auto;">Cancel</button><button type="button" class="btn primary up-go" disabled>Upload</button></div>');
      var m = UI.modal({ title: o.title || "Upload creatives", body: body, footer: foot, onClose: function () { if (!done) { done = true; resolve(null); } } });
      var drop = body.querySelector(".up-drop"), input = body.querySelector("input[type=file]"), list = body.querySelector(".up-list"), go = foot.querySelector(".up-go"), msg = body.querySelector(".up-msg");
      function mb(n) { return (n / 1e6).toFixed(n > 1e7 ? 0 : 1) + " MB"; }
      function render() {
        list.innerHTML = files.map(function (f, i) { return '<div class="up-row"><span class="up-name">' + esc(f.name) + '</span><span class="muted">' + mb(f.size) + '</span><button type="button" class="linkish" data-i="' + i + '" title="Remove">✕</button></div>'; }).join("");
        go.disabled = !files.length || busy; go.textContent = files.length ? "Upload " + files.length + " file" + (files.length === 1 ? "" : "s") : "Upload";
      }
      function add(fl) { Array.prototype.forEach.call(fl || [], function (f) { if (!files.some(function (x) { return x.name === f.name && x.size === f.size; })) files.push(f); }); render(); }
      drop.addEventListener("click", function () { input.click(); });
      drop.addEventListener("keydown", function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); input.click(); } });
      input.addEventListener("change", function () { add(input.files); input.value = ""; });
      ["dragenter", "dragover"].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.add("over"); }); });
      ["dragleave", "drop"].forEach(function (ev) { drop.addEventListener(ev, function (e) { e.preventDefault(); drop.classList.remove("over"); }); });
      drop.addEventListener("drop", function (e) { add(e.dataTransfer && e.dataTransfer.files); });
      list.addEventListener("click", function (e) { var b = e.target.closest("[data-i]"); if (b && !busy) { files.splice(parseInt(b.dataset.i, 10), 1); render(); } });
      go.addEventListener("click", function () {
        if (!files.length || busy) return;
        busy = true; render(); msg.textContent = "Uploading…";
        var bar = body.querySelector(".up-bar"), fill = body.querySelector(".up-fill"); bar.hidden = false; fill.style.width = "0%";
        var fd = new FormData(); files.forEach(function (f) { fd.append("files", f, f.name); });
        var pre = body.querySelector('[name="source_prefix"]').value.trim(); if (pre) fd.append("source_prefix", pre);
        var xhr = new XMLHttpRequest();
        xhr.open("POST", "/creatives/upload"); xhr.setRequestHeader("X-Requested-With", "fetch"); xhr.setRequestHeader("Accept", "application/json");
        xhr.upload.onprogress = function (e) { if (e.lengthComputable) { var pc = Math.round(e.loaded / e.total * 100); fill.style.width = pc + "%"; msg.textContent = pc < 100 ? "Uploading… " + pc + "%" : "Saving…"; } };
        xhr.onerror = function () { busy = false; render(); msg.textContent = "Upload failed — check the connection and try again."; };
        xhr.onload = function () {
          var d = {}; try { d = JSON.parse(xhr.responseText || "{}"); } catch (e) {}
          if (xhr.status !== 200 || !d.ok) { busy = false; render(); msg.textContent = d.message || d.error || (d.skipped && d.skipped.join(" · ")) || ("Upload failed (" + xhr.status + ")"); return; }
          if (d.skipped && d.skipped.length) { if (window.adopsToast) adopsToast("err", d.skipped.join(" · ")); }
          done = true; m.close(); resolve(d.items || []);
        };
        xhr.send(fd);
      });
      render();
    });
  };

  /* UI.pasteSparks({title}) → Promise<[{id,name,creator,type,state,thumb,post_url}] | null>
     Paste spark codes (one per line, or  name | code | video/carousel | post URL | source);
     they are saved to Spark codes and handed back picker-shaped (codes already saved come
     back too, so pasting a known code still selects it). Data: POST /spark-codes/bulk (fetch → JSON). */
  UI.pasteSparks = function (o) {
    o = o || {};
    return new Promise(function (resolve) {
      var done = false, busy = false;
      var body = UI.el('<div><textarea class="ps-text" rows="7" style="width:100%;font-family:var(--font-mono);font-size:12px;" placeholder="#CT7QabcDEF…=\nname | #code | carousel | https://www.tiktok.com/@creator/photo/… | source"></textarea>' +
        '<div class="form-row" style="margin-top:8px;"><label class="field" style="margin:0;"><span class="field-label">Type (when a line doesn\'t say)</span><select name="media_type"><option value="VIDEO">video</option><option value="CAROUSEL">carousel / photos</option></select></label>' +
        '<label class="field" style="margin:0;"><span class="field-label">Creator <span class="muted">(optional)</span></span><input type="text" name="group_name" placeholder="@handle"></label>' +
        '<label class="field" style="margin:0;"><span class="field-label">Source <span class="muted">(optional)</span></span><input type="text" name="source" placeholder="P&amp;L join key"></label></div>' +
        '<div class="muted ps-msg" style="font-size:12px;margin-top:6px;"></div></div>');
      var foot = UI.el('<div style="display:flex;align-items:center;gap:8px;width:100%;"><span class="muted" style="font-size:12px;">Saved to Spark codes and added to this launch.</span><button type="button" class="btn" data-close style="margin-left:auto;">Cancel</button><button type="button" class="btn primary ps-go">Add codes</button></div>');
      var m = UI.modal({ title: o.title || "Paste spark codes", body: body, footer: foot, onClose: function () { if (!done) { done = true; resolve(null); } } });
      var ta = body.querySelector(".ps-text"), go = foot.querySelector(".ps-go"), msg = body.querySelector(".ps-msg");
      go.addEventListener("click", function () {
        var text = ta.value.trim(); if (!text || busy) { if (!text) msg.textContent = "Paste at least one code."; return; }
        busy = true; go.disabled = true; msg.textContent = "Saving…";
        UI.post("/spark-codes/bulk", { lines: text, media_type: body.querySelector('[name="media_type"]').value, group_name: body.querySelector('[name="group_name"]').value.trim(), source: body.querySelector('[name="source"]').value.trim() }).then(function (d) {
          busy = false; go.disabled = false;
          if (!d.ok) { msg.textContent = d.error || d.message || "Nothing was added."; return; }
          if (d.bad && d.bad.length && window.adopsToast) adopsToast("err", d.bad.length + " line(s) had no code");
          done = true; m.close(); resolve(d.items || []);
        }).catch(function () { busy = false; go.disabled = false; msg.textContent = "Couldn't reach the dashboard."; });
      });
      setTimeout(function () { try { ta.focus(); } catch (e) {} }, 30);
    });
  };

  /* UI.previewCreative({name, sub, cover, video, slides, url}) — a phone-shaped preview pop-up
     for one board tile: plays a library video / profile post, shows a carousel's slides, or the cover. */
  UI.previewCreative = function (it) {
    it = it || {};
    var media = it.video ? '<video src="' + esc(it.video) + '" controls autoplay playsinline preload="metadata"' + (it.cover ? ' poster="' + esc(it.cover) + '"' : "") + '></video>'
      : (it.cover ? '<img src="' + esc(it.cover) + '" alt="">' : '<div class="muted" style="padding:40px 10px;text-align:center;font-size:12px;">No preview for this one' + (it.url ? " — open it ↗" : "") + '</div>');
    var body = UI.el('<div class="pk-prev"><div class="pk-phone">' + media + '</div><div class="pk-info"><b>' + esc(it.name || "") + '</b>' +
      (it.sub ? '<div class="muted" style="font-size:11.5px;margin-top:4px;">' + esc(it.sub) + '</div>' : "") +
      (it.text ? '<div style="font-size:12.5px;margin-top:6px;">' + esc(it.text) + '</div>' : "") +
      (it.url ? '<div style="margin-top:8px;"><a href="' + esc(it.url) + '" target="_blank" rel="noopener">open ↗</a></div>' : "") + '</div></div>');
    var m = UI.modal({ title: it.title || "Preview", body: body });
    if (it.slides && it.slides.length && UI.slides) UI.slides(body.querySelector(".pk-phone"), it.slides, { poster: it.cover });
    return m;
  };
})();
