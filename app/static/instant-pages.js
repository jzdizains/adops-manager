/* Instant Pages (v132): one row per page NAME, with search, filters, favourites, tags and an
   accounts drawer — all running on one compact JSON blob (#ipData) instead of per-account rows.

   Pure part (IP.index / IP.matches / IP.coverage / IP.drawerHtml / IP.cloneChoices): no DOM,
   tested in node. DOM part wires the toolbar, star, tag pop-up, drawer and clone popover. */
(function (w) {
  "use strict";
  var IP = w.IP = w.IP || {};
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]; }); }
  IP.esc = esc;

  // ---- pure --------------------------------------------------------------------------
  IP.index = function (data) {
    // account id → account; bc id → name; per group: search text + tag id list
    var byId = {}, bcName = {};
    (data.accounts || []).forEach(function (a) { byId[a.id] = a; });
    (data.bcs || []).forEach(function (b) { bcName[b.bc_id] = b.name; });
    (data.groups || []).forEach(function (g) {
      var parts = [g.name];
      (g.copies || []).forEach(function (c) { var a = byId[c.adv]; parts.push(a ? a.name : c.adv, c.page_id, bcName[c.bc] || ""); });
      g._search = parts.join(" ").toLowerCase();
      g._tagIds = (g.tags || []).map(function (t) { return String(t.id); });
    });
    return { byId: byId, bcName: bcName, total: data.total || 0 };
  };

  IP.coverage = function (g, total, bc) {
    // how many accounts (in one BC, or overall) still lack this page
    if (bc) { var m = (g.missing_by_bc || {})[bc]; return { missing: m == null ? 0 : m }; }
    return { missing: Math.max(0, (total || 0) - (g.count || 0)) };
  };

  IP.matches = function (g, st, total) {
    // st: {q, fav, tags:[ids], bc, status:""|"published"|"draft", cov:""|"full"|"partial"}
    st = st || {};
    if (st.q && (g._search || "").indexOf(st.q) === -1) return false;
    if (st.fav && !g.favorite) return false;
    if (st.tags && st.tags.length) {
      for (var i = 0; i < st.tags.length; i++) if ((g._tagIds || []).indexOf(String(st.tags[i])) === -1) return false;
    }
    if (st.bc && (g.bcs || []).indexOf(st.bc) === -1 && !((g.missing_by_bc || {})[st.bc] > 0 && st.cov === "partial")) return false;
    if (st.status === "published" && !g.published) return false;
    if (st.status === "draft" && !g.drafts) return false;
    if (st.cov) {
      var missing = IP.coverage(g, total, st.bc).missing;
      if (st.cov === "full" && missing > 0) return false;
      if (st.cov === "partial" && missing === 0) return false;
    }
    return true;
  };

  IP.cloneChoices = function (g, data) {
    // the accounts a copy can go to (the source's own account and accounts that already
    // have the page are shown but not offered), and the BCs with how many still lack it
    var have = {}; (g.copies || []).forEach(function (c) { have[c.adv] = 1; });
    var accounts = (data.accounts || []).map(function (a) { return { id: a.id, name: a.name, has: !!have[a.id] }; });
    var bcs = (data.bcs || []).filter(function (b) { return b.bc_id; }).map(function (b) {
      return { bc_id: b.bc_id, name: b.name, n: b.n, missing: (g.missing_by_bc || {})[b.bc_id] || 0 };
    });
    return { accounts: accounts, bcs: bcs };
  };

  IP.drawerHtml = function (g, data, idx) {
    // accounts grouped by Business Center: the ones that have the page, then the ones missing it
    var have = {}; (g.copies || []).forEach(function (c) { have[c.adv] = c; });
    var byBc = {};
    (data.accounts || []).forEach(function (a) { (byBc[a.bc || ""] = byBc[a.bc || ""] || []).push(a); });
    var order = (data.bcs || []).map(function (b) { return b.bc_id; });
    Object.keys(byBc).forEach(function (k) { if (order.indexOf(k) === -1) order.push(k); });
    var src = g.source, html = '<div class="ipd-tools"><input type="search" class="ipd-q" placeholder="Filter accounts…" autocomplete="off"><span class="muted ipd-n"></span></div>';
    order.forEach(function (bc) {
      var list = byBc[bc]; if (!list || !list.length) return;
      var n = list.filter(function (a) { return have[a.id]; }).length;
      var name = bc ? (idx.bcName[bc] || bc) : "No Business Center";
      html += '<div class="ipd-bc" data-bc="' + esc(bc) + '"><div class="ipd-bch"><b>' + esc(name) + '</b><span class="pill ' + (n === list.length ? "ok" : (n ? "warn" : "dim")) + '">' + n + " of " + list.length + "</span></div>";
      list.slice().sort(function (a, b) { var ha = !!have[a.id], hb = !!have[b.id]; if (ha !== hb) return ha ? -1 : 1; return a.name.toLowerCase() < b.name.toLowerCase() ? -1 : 1; })
        .forEach(function (a) {
          var c = have[a.id];
          html += '<div class="ipd-row ' + (c ? "have" : "miss") + '" data-s="' + esc((a.name + " " + a.id + " " + (c ? c.page_id : "")).toLowerCase()) + '">' +
            '<span class="ipd-dot">' + (c ? "✓" : "○") + '</span><span class="ipd-name">' + esc(a.name) + "</span>" +
            (c ? '<span class="mono ipd-id">' + esc(c.page_id) + '</span><span class="pill ' + (c.status === "PUBLISHED" ? "ok" : "dim") + '">' + (c.status === "PUBLISHED" ? "Published" : "Draft") + "</span>" +
                 (c.preview ? '<a href="' + esc(c.preview) + '" target="_blank" rel="noopener">Preview</a>' : "") +
                 '<a href="https://ads.tiktok.com/i18n/material/instantPage?aadvid=' + esc(a.id) + '" target="_blank" rel="noopener">Ads Manager</a>'
               : '<span class="muted">not on this account</span>' +
                 (src ? '<button type="button" class="btn sm ipd-clone" data-to="' + esc(a.id) + '">Clone here</button>' : '<span class="muted" title="Only a published copy can be cloned">no published copy to clone</span>')) +
            "</div>";
        });
      html += "</div>";
    });
    return html;
  };

  // ---- DOM ---------------------------------------------------------------------------
  if (typeof document === "undefined" || !w.document) return;
  var dataEl = document.getElementById("ipData");
  if (!dataEl) return;
  var DATA = JSON.parse(dataEl.textContent || "{}"), IDX = IP.index(DATA);
  var byName = {}; (DATA.groups || []).forEach(function (g) { byName[g.name] = g; });
  var $ = function (s, r) { return (r || document).querySelector(s); }, $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var TAG_COLORS = ["green", "red", "amber", "blue", "purple", "pink", "teal", "grey"];
  var WEB_READY = dataEl.dataset.web === "1";

  function state() {
    var q = ($("#ipSearch") || {}).value || "";
    return {
      q: q.trim().toLowerCase(), fav: $("#ipFav") && $("#ipFav").classList.contains("on"),
      tags: $$("#ipTags .chip.on").map(function (c) { return c.dataset.tag; }),
      bc: ($("#ipBc") || {}).value || "", status: ($("#ipStatus") || {}).value || "", cov: ($("#ipCov") || {}).value || "",
    };
  }
  function apply() {
    var st = state(), shown = 0, rows = $$("#ipTable tbody tr.ip-row");
    rows.forEach(function (r) { var g = byName[r.dataset.name]; var on = g ? IP.matches(g, st, IDX.total) : true; r.hidden = !on; if (on) shown++; });
    var empty = $("#ipEmpty"); if (empty) empty.hidden = shown > 0 || !rows.length;
    var c = $("#ipCount"); if (c) c.textContent = shown === rows.length ? rows.length + " page" + (rows.length === 1 ? "" : "s") + " · " + IDX.total + " accounts" : shown + " of " + rows.length + " pages";
    // coverage cells follow the BC filter: "12 of 14 in this BC" instead of "62 of 282"
    rows.forEach(function (r) { var g = byName[r.dataset.name]; if (!g) return; var cell = r.querySelector(".cov"); if (!cell) return;
      var total = st.bc ? ((DATA.bcs || []).filter(function (b) { return b.bc_id === st.bc; })[0] || { n: 0 }).n : IDX.total;
      var haveN = st.bc ? total - IP.coverage(g, IDX.total, st.bc).missing : g.count;
      var pct = total ? Math.round(100 * haveN / total) : 0;
      cell.querySelector("i").style.width = pct + "%"; cell.querySelector("i").className = haveN >= total && total ? "full" : "";
      cell.querySelector("span").textContent = haveN + " of " + total + (st.bc ? " here" : "");
    });
    refreshChipCounts();
  }
  function refreshChipCounts() {
    var fav = $("#ipFav"); if (fav) fav.querySelector(".n").textContent = (DATA.groups || []).filter(function (g) { return g.favorite; }).length;
    $$("#ipTags .chip").forEach(function (c) { c.querySelector(".n").textContent = (DATA.groups || []).filter(function (g) { return (g._tagIds || []).indexOf(c.dataset.tag) !== -1; }).length; });
  }
  var t; function debounced() { clearTimeout(t); t = setTimeout(apply, 120); }
  var s = $("#ipSearch"); if (s) { s.addEventListener("input", debounced); s.addEventListener("keydown", function (e) { if (e.key === "Escape") { s.value = ""; apply(); } }); }
  ["#ipBc", "#ipStatus", "#ipCov"].forEach(function (id) { var e = $(id); if (e) e.addEventListener("change", apply); });
  var favBtn = $("#ipFav"); if (favBtn) favBtn.addEventListener("click", function () { favBtn.classList.toggle("on"); apply(); });
  document.addEventListener("click", function (e) {
    var chip = e.target.closest && e.target.closest("#ipTags .chip"); if (chip) { chip.classList.toggle("on"); apply(); }
  });
  document.addEventListener("keydown", function (e) { if (e.key === "/" && !e.target.closest("input, textarea, select") && s) { e.preventDefault(); s.focus(); } });

  // ---- favourite star: updates in place, no reload ----
  document.addEventListener("click", function (e) {
    var star = e.target.closest && e.target.closest(".ip-star"); if (!star) return;
    var row = star.closest("tr"), g = byName[row.dataset.name]; if (!g) return;
    var want = !g.favorite; star.disabled = true;
    UI.post("/instant-pages/mark", { name: g.name, favorite: want ? 1 : 0 }).then(function (r) {
      star.disabled = false;
      if (!r || !r.ok) { w.adopsToast && adopsToast("err", (r && r.error) || "Couldn't save the favourite."); return; }
      g.favorite = !!r.favorite; row.dataset.fav = g.favorite ? "1" : "0"; star.classList.toggle("on", g.favorite); star.title = g.favorite ? "Favourite — click to remove" : "Add to favourites";
      apply();
    });
  });

  // ---- tags: the same pop-up campaigns use, saving on the page name ----
  function tagPill(tg) { return '<span class="ctag ctag-' + esc(tg.color) + '" data-tag="' + tg.id + '">' + esc(tg.name) + '<span class="ctag-x" title="Remove tag">×</span></span>'; }
  function paintTags(row, g) { var h = row.querySelector(".ctags"); if (h) h.innerHTML = (g.tags || []).map(tagPill).join(""); g._tagIds = (g.tags || []).map(function (x) { return String(x.id); }); }
  function setTag(row, g, tagId, on) {
    return UI.post("/instant-pages/mark", { name: g.name, tag_id: tagId, on: on ? 1 : 0 }).then(function (r) {
      if (!r || !r.ok) { w.adopsToast && adopsToast("err", (r && r.error) || "Couldn't change the tag."); return false; }
      g.tags = r.tags || []; paintTags(row, g); ensureChip(r.tags || []); apply(); return true;
    });
  }
  function ensureChip(tags) {
    var bar = $("#ipTags"); if (!bar) return;
    tags.forEach(function (tg) {
      if ($('#ipTags .chip[data-tag="' + tg.id + '"]')) return;
      var c = document.createElement("button"); c.type = "button"; c.className = "chip"; c.dataset.tag = String(tg.id);
      c.innerHTML = '<span class="ctag ctag-' + esc(tg.color) + '" style="margin-left:0;">' + esc(tg.name) + '</span> <span class="n">0</span>'; bar.appendChild(c); bar.hidden = false;
    });
  }
  function tagPopup(anchor, row, g) {
    var color = "green";
    UI.get("/tags.json").then(function (d) {
      var tags = (d && d.tags) || [], have = {};
      (g._tagIds || []).forEach(function (id) { have[id] = 1; });
      var list = tags.length ? tags.map(function (tg) {
        return '<label class="tp-row"><input type="checkbox" data-tag="' + tg.id + '"' + (have[String(tg.id)] ? " checked" : "") + '>' +
          '<span class="ctag ctag-' + esc(tg.color) + '">' + esc(tg.name) + '</span><span class="tp-del" data-del="' + tg.id + '" title="Delete this tag everywhere">delete</span></label>';
      }).join("") : '<div class="muted" style="padding:2px 4px 6px;">No tags yet — make one below.</div>';
      var html = '<div class="tagpop"><div class="muted" style="font-size:11px;margin-bottom:4px;">Tags for “' + esc(g.name) + '”</div>' + list +
        '<div class="tp-sep"></div><div class="muted" style="font-size:11px;">New tag</div>' +
        '<input type="text" class="tp-name" maxlength="32" placeholder="e.g. UK, cash offer, needs update" style="width:100%;box-sizing:border-box;margin-top:4px;">' +
        '<div class="tp-sw">' + TAG_COLORS.map(function (c) { return '<span data-c="' + c + '" class="' + (c === color ? "on" : "") + '" title="' + c + '"></span>'; }).join("") + "</div>" +
        '<div style="display:flex;justify-content:flex-end;"><button type="button" class="btn sm primary tp-add">Add</button></div></div>';
      var p = UI.popover(anchor, html, {});
      $$(".tp-sw span", p).forEach(function (sw) { var probe = document.createElement("span"); probe.className = "ctag ctag-" + sw.dataset.c; probe.style.position = "absolute"; probe.style.visibility = "hidden"; document.body.appendChild(probe); sw.style.background = getComputedStyle(probe).color; probe.remove(); });
      p.addEventListener("change", function (e) {
        var cb = e.target.closest && e.target.closest("input[type=checkbox][data-tag]");
        if (cb) { cb.disabled = true; setTag(row, g, cb.dataset.tag, cb.checked).then(function (ok) { cb.disabled = false; if (!ok) cb.checked = !cb.checked; }); }
      });
      p.addEventListener("click", function (e) {
        var sw = e.target.closest && e.target.closest(".tp-sw span");
        if (sw) { color = sw.dataset.c; $$(".tp-sw span", p).forEach(function (o) { o.classList.toggle("on", o === sw); }); return; }
        var del = e.target.closest && e.target.closest(".tp-del");
        if (del) {
          e.preventDefault();
          UI.confirm({ title: "Delete this tag everywhere?", text: "It comes off every page and campaign that carries it. This can't be undone.", ok: "Delete", danger: true }).then(function (yes) {
            if (!yes) return;
            UI.post("/tags/" + del.dataset.del + "/delete", {}).then(function (r) {
              if (!r || !r.ok) { w.adopsToast && adopsToast("err", "Couldn't delete the tag."); return; }
              (DATA.groups || []).forEach(function (gg) { gg.tags = (gg.tags || []).filter(function (x) { return String(x.id) !== del.dataset.del; }); var rr = $('#ipTable tr.ip-row[data-name="' + CSS.escape(gg.name) + '"]'); if (rr) paintTags(rr, gg); });
              var chip = $('#ipTags .chip[data-tag="' + del.dataset.del + '"]'); if (chip) chip.remove();
              del.closest(".tp-row").remove(); apply();
            });
          });
          return;
        }
        if (e.target.closest && e.target.closest(".tp-add")) add();
      });
      function add() {
        var inp = p.querySelector(".tp-name"), name = inp.value.trim();
        if (!name) { inp.focus(); return; }
        UI.post("/tags", { name: name, color: color }).then(function (r) {
          if (!r || !r.ok) { w.adopsToast && adopsToast("err", (r && r.error) || "Couldn't create the tag."); return; }
          setTag(row, g, r.tag.id, true).then(function () { p.close(); tagPopup(anchor, row, g); });
        });
      }
      p.querySelector(".tp-name").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); add(); } });
    });
  }
  document.addEventListener("click", function (e) {
    var add = e.target.closest && e.target.closest(".ip-row .ctag-add");
    if (add) { e.preventDefault(); e.stopImmediatePropagation(); var row = add.closest("tr"); tagPopup(add, row, byName[row.dataset.name]); return; }
    var x = e.target.closest && e.target.closest(".ip-row .ctag-x");
    if (x) { e.preventDefault(); e.stopImmediatePropagation(); var row2 = x.closest("tr"), pill = x.closest(".ctag"); setTag(row2, byName[row2.dataset.name], pill.dataset.tag, false); }
  });

  // ---- posting a clone: the routes answer with a redirect + message, so a real form POST ----
  function postForm(action, fields) {
    var f = document.createElement("form"); f.method = "post"; f.action = action; f.hidden = true;
    Object.keys(fields).forEach(function (k) { var i = document.createElement("input"); i.type = "hidden"; i.name = k; i.value = fields[k] == null ? "" : fields[k]; f.appendChild(i); });
    document.body.appendChild(f); f.submit();
  }

  // ---- accounts drawer ----
  function openDrawer(g) {
    var d = UI.drawer({ title: g.name, sub: g.count + " of " + IDX.total + " accounts · " + (g.bcs || []).filter(Boolean).length + " Business Center" + ((g.bcs || []).filter(Boolean).length === 1 ? "" : "s"), body: IP.drawerHtml(g, DATA, IDX) });
    var q = d.body.querySelector(".ipd-q"), n = d.body.querySelector(".ipd-n");
    function filt() {
      var v = (q.value || "").trim().toLowerCase(), shown = 0;
      $$(".ipd-row", d.body).forEach(function (r) { var on = !v || r.dataset.s.indexOf(v) !== -1; r.hidden = !on; if (on) shown++; });
      $$(".ipd-bc", d.body).forEach(function (b) { b.hidden = !$$(".ipd-row", b).some(function (r) { return !r.hidden; }); });
      n.textContent = v ? shown + " match" + (shown === 1 ? "" : "es") : "";
    }
    q.addEventListener("input", filt); setTimeout(function () { q.focus(); }, 30);
    d.body.addEventListener("click", function (e) {
      var b = e.target.closest && e.target.closest(".ipd-clone"); if (!b || !g.source) return;
      if (!WEB_READY) { w.adopsToast && adopsToast("err", "Cloning needs the ads.tiktok.com cookies from the TikTok Cookies page."); return; }
      b.disabled = true; b.textContent = "Cloning…";
      postForm("/instant-pages/clone", { page_id: g.source.page_id, from_advertiser_id: g.source.adv, to_advertiser_id: b.dataset.to, new_url: "", new_text: "" });
    });
  }
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest(".ip-open, .ip-accounts"); if (!a) return;
    e.preventDefault(); var row = a.closest("tr"), g = byName[row.dataset.name]; if (g) openDrawer(g);
  });

  // ---- Clone to… : opens the reusable account pop-up (search + BC + status + multi-select) ----
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest(".ip-clone"); if (!b || b.disabled || !UI.pickAccounts) return;
    var row = b.closest("tr"), g = byName[row.dataset.name]; if (!g || !g.source) return;
    UI.pickAccounts({
      title: "Clone “" + g.name + "” to…", confirmLabel: "Clone to selected", exclude: [g.source.adv],
      has: (g.copies || []).reduce(function (m, c) { m[c.adv] = c.status || "PUBLISHED"; return m; }, {}), hasLabel: "Has this page",
      extra: [{ name: "new_url", label: "Button link (optional) — leave empty to keep the page’s own", type: "url", placeholder: "https://…" },
              { name: "new_text", label: "New button text (optional)", type: "text", placeholder: "" }]
    }).then(function (r) {
      if (!r || !r.ids || !r.ids.length) return;
      postForm("/instant-pages/clone-multi", { page_id: g.source.page_id, from_advertiser_id: g.source.adv,
        target_ids: r.ids.join(","), new_url: (r.values && r.values.new_url) || "", new_text: (r.values && r.values.new_text) || "" });
    });
  });

  apply();
})(typeof window !== "undefined" ? window : globalThis);
