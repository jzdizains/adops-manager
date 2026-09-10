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
})();
