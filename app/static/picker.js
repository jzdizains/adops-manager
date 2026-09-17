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
  /* UI.pickProfileVideos({bcs:[{id,name,accounts}], selected:[{item_id,...}]}) → Promise<[{item_id, identity_id, identity_type, bc_id, handle, text, cover, type, auth_code, url}] | null>
     Every post of every profile the viewer's Business Centers share, loaded BC by BC and shown as it arrives;
     filter by profile (not BC), multi-select, preview plays the post. Data: /super-launcher/profile-videos.json?bc_id= */
  UI.pickProfileVideos = function (o) {
    o = o || {};
    return new Promise(function (resolve) {
      var bcs = o.bcs || [], q = "", profiles = [], sel = {}, order = [], cur = null, done = false, loading = 0, pf = "", errors = [];
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
      function shownProfiles() { return profiles.filter(function (p) { return !pf || key(p) === pf; }); }
      function shown(p) { var qq = q.toLowerCase(); return p.videos.filter(function (v) { return !qq || (v.text || "").toLowerCase().indexOf(qq) >= 0 || (p.name || "").toLowerCase().indexOf(qq) >= 0; }); }
      function mark() {
        var n = order.filter(function (id) { return sel[id]; }).length;
        foot.querySelector(".pk-sel").textContent = n + " selected";
        foot.querySelector(".pk-use").textContent = "Use " + n + " video" + (n === 1 ? "" : "s");
        foot.querySelector(".pk-use").disabled = n === 0;
      }
      function fillProfiles() {
        var keep = pfSel.value;
        pfSel.innerHTML = '<option value="">All profiles · ' + profiles.reduce(function (a, p) { return a + p.videos.length; }, 0) + ' posts</option>' +
          profiles.slice().sort(function (a, b) { return a.name.localeCompare(b.name); }).map(function (p) { return '<option value="' + esc(key(p)) + '">@' + esc(p.name) + ' · ' + p.videos.length + (p.bc_name ? ' · ' + esc(p.bc_name) : '') + '</option>'; }).join("");
        pfSel.value = keep; if (pfSel.value !== keep) { pf = ""; }
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
          html += '<div class="pv-head">' + (p.avatar ? '<img src="' + esc(p.avatar) + '" alt="">' : '<span class="pv-ph">@</span>') + '<b>@' + esc(p.name) + '</b><span class="muted">' + p.videos.length + ' post' + (p.videos.length === 1 ? '' : 's') + (p.bc_name ? ' · ' + esc(p.bc_name) : '') + (picked ? ' · ' + picked + ' picked' : '') + (p.error ? ' · <span style="color:var(--err);">' + esc(p.error) + '</span>' : '') + '</span>' +
            (vs.length ? '<button type="button" class="btn sm pv-all" data-pid="' + esc(key(p)) + '">' + (vs.every(function (v) { return sel[v.item_id]; }) ? "None" : "Select all") + '</button>' : '') + '</div>';
          html += vs.map(function (v) { return tile(p, v); }).join("");
          if (!vs.length) html += '<div class="muted pv-empty" style="padding:4px 6px 10px;font-size:12px;">No ad-usable posts on this profile.</div>';
        });
        if (loading) html += '<div class="muted pv-empty" style="padding:10px 6px;font-size:12px;">Reading ' + loading + ' more Business Center' + (loading === 1 ? "" : "s") + '…</div>';
        if (errors.length) html += '<div class="muted pv-empty" style="padding:4px 6px;font-size:12px;color:var(--err);">' + errors.map(esc).join("<br>") + '</div>';
        grid.innerHTML = (list.length || loading) ? html : '<div class="empty pv-empty">No profiles shared on your Business Centers' + (q ? " match “" + esc(q) + "”" : "") + '.</div>';
        body.querySelector(".pk-count").textContent = profiles.length ? (profiles.length + " profile" + (profiles.length === 1 ? "" : "s") + " · " + ((q || pf) ? vis + " of " : "") + profiles.reduce(function (a, p) { return a + p.videos.length; }, 0) + " post" + (total === 1 ? "" : "s")) : "";
        mark();
      }
      function load(refresh) {
        profiles = []; errors = []; loading = bcs.length; fillProfiles(); render();
        if (!bcs.length) { grid.innerHTML = '<div class="empty pv-empty">No Business Center with a connected account in this view.</div>'; return; }
        // one Business Center at a time (a few TikTok calls each), shown as each arrives
        (function next(i) {
          if (i >= bcs.length) { loading = 0; fillProfiles(); render(); return; }
          var b = bcs[i];
          UI.get("/super-launcher/profile-videos.json?bc_id=" + encodeURIComponent(b.id) + (refresh ? "&refresh=1" : "")).then(function (d) {
            if (d && d.ok === false) errors.push(b.name + ": " + (d.error || "TikTok didn't answer"));
            ((d && d.profiles) || []).forEach(function (p) { p.bc_name = b.name; profiles.push(p); });
            loading = bcs.length - i - 1; fillProfiles(); render(); next(i + 1);
          }).catch(function () { errors.push(b.name + ": couldn't reach the dashboard"); loading = bcs.length - i - 1; render(); next(i + 1); });
        })(0);
      }
      function find(id) { var hit = null; profiles.forEach(function (p) { p.videos.forEach(function (v) { if (v.item_id == id) hit = { p: p, v: v }; }); }); return hit; }
      function entry(p, v) { return { item_id: v.item_id, identity_id: p.identity_id, identity_type: p.identity_type, bc_id: p.bc_id, handle: p.name, text: v.text, cover: v.cover, type: v.type, auth_code: v.auth_code, url: v.url }; }
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
        var a = e.target.closest(".pv-all");
        if (a) { var p = profiles.filter(function (x) { return key(x) === a.dataset.pid; })[0]; if (!p) return; var vs = shown(p), every = vs.every(function (v) { return sel[v.item_id]; }); vs.forEach(function (v) { if (every ? sel[v.item_id] : !sel[v.item_id]) { if (sel[v.item_id]) { delete sel[v.item_id]; order = order.filter(function (x) { return x !== v.item_id; }); } else { sel[v.item_id] = entry(p, v); order.push(v.item_id); } } }); render(); return; }
        var t = e.target.closest(".pk-tile"); if (!t) return;
        var hit = find(t.dataset.id); if (!hit) return;
        if (e.target.closest(".pk-thumb")) toggle(hit.p, hit.v);
        preview(hit.p, hit.v);
      });
      grid.addEventListener("mouseover", function (e) { var t = e.target.closest(".pk-tile"); if (!t) return; var hit = find(t.dataset.id); if (hit && (!cur || cur.item_id !== hit.v.item_id) && !(side.querySelector("video") && !side.querySelector("video").paused)) preview(hit.p, hit.v); });
      pfSel.addEventListener("change", function () { pf = pfSel.value; render(); });
      body.querySelector(".pv-refresh").addEventListener("click", function () { if (!loading) load(true); });
      var qt = null; body.querySelector(".pk-q").addEventListener("input", function (e) { clearTimeout(qt); q = e.target.value; qt = setTimeout(render, 150); });
      foot.querySelector(".pk-use").addEventListener("click", function () {
        var chosen = order.filter(function (id) { return sel[id]; }).map(function (id) { return sel[id]; });
        done = true; m.close(); resolve(chosen);
      });
      mark(); load(false);
    });
  };
})();
