/* Reusable account-picker POP-UP — the launcher's rich selection (search + Business
   Center chips + Fresh/Used/Live/Blocked tabs + grouped multi-select) as a modal any
   page can open, replacing the old single-account dropdowns.

     UI.pickAccounts({ title, confirmLabel, exclude:[ids], preselect:[ids],
                       has:{id: status}, hasLabel })
       -> Promise<string[] | null>   (advertiser ids, or null if cancelled)

   `has` (v155.2): accounts that ALREADY hold the thing being copied (a form, a page) — each
   gets a "Has it" pill, can't be ticked (the copy would be skipped anyway), sorts last in its
   Business Center, and the "Has it / Missing it" chips filter on it.

   Data comes from /accounts/picker.json (one fetch, cached). This is a SEPARATE
   component from the launcher's own picker, so the launch flow is never touched. */
(function () {
  if (!window.UI) window.UI = {};
  var CACHE = null, STYLED = false;
  var STATE_TABS = [["", "All"], ["fresh", "Fresh"], ["used", "Used"], ["active", "Live"], ["blocked", "Blocked"]];

  function esc(s) { return (window.UI && UI.esc) ? UI.esc(s) : String(s == null ? "" : s).replace(/[&<>"]/g, function (c) { return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]; }); }

  function styleOnce() {
    if (STYLED) return; STYLED = true;
    var css = ""
      + ".apk-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px;}"
      + ".apk-q{flex:1;min-width:200px;}"
      + ".apk-tabs,.apk-bcs{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px;}"
      + ".apk-list{max-height:52vh;overflow:auto;border:1px solid var(--border-soft,#2a2f3a);border-radius:8px;}"
      + ".apk-ghead{display:flex;align-items:center;gap:8px;padding:7px 12px;background:var(--surface-2,#12151c);position:sticky;top:0;z-index:1;border-bottom:1px solid var(--border-soft,#2a2f3a);font-size:12.5px;}"
      + ".apk-ghead label{display:flex;align-items:center;gap:8px;cursor:pointer;}"
      + ".apk-gn{margin-left:auto;font-size:11px;}"
      + ".apk-row{display:flex;align-items:center;gap:9px;padding:6px 12px;font-size:12.5px;cursor:pointer;border-bottom:1px solid var(--border-soft,#20242e);}"
      + ".apk-row:hover{background:var(--surface-2,#12151c);}"
      + ".apk-nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}"
      + ".apk-id{font-size:10px;}"
      + ".apk-st{font-size:10px;padding:1px 7px;border-radius:999px;text-transform:capitalize;}"
      + ".apk-st-fresh{background:rgba(52,199,89,.16);color:#34c759;}.apk-st-used{background:rgba(255,255,255,.08);color:var(--text-dim);}"
      + ".apk-has{font-size:10px;padding:1px 7px;border-radius:999px;background:rgba(52,199,89,.16);color:#34c759;white-space:nowrap;}"
      + ".apk-has.draft{background:rgba(255,255,255,.08);color:var(--text-dim);}"
      + ".apk-row.is-has{cursor:default;}.apk-row.is-has .apk-nm{opacity:.75;}"
      + ".apk-sep{width:1px;align-self:stretch;background:var(--border-soft,#2a2f3a);margin:0 2px;}"
      + ".apk-st-active{background:rgba(255,214,10,.16);color:#ffd60a;}.apk-st-cooldown{background:rgba(255,159,10,.16);color:#ff9f0a;}.apk-st-blocked{background:rgba(255,69,58,.16);color:#ff453a;}";
    var s = document.createElement("style"); s.textContent = css; document.head.appendChild(s);
  }

  function load(force) {
    if (CACHE && !force) return Promise.resolve(CACHE);
    return fetch("/accounts/picker.json", { headers: { "X-Requested-With": "fetch" }, credentials: "same-origin" })
      .then(function (r) { return r.json(); }).then(function (d) { CACHE = d; return d; });
  }

  UI.pickAccounts = function (opts) {
    opts = opts || {};
    styleOnce();
    var exclude = {}; (opts.exclude || []).forEach(function (id) { exclude[String(id)] = true; });
    var pre = {}; (opts.preselect || []).forEach(function (id) { pre[String(id)] = true; });
    var has = {}; Object.keys(opts.has || {}).forEach(function (id) { has[String(id)] = String((opts.has || {})[id] || "yes"); });
    var hasOn = Object.keys(has).length > 0, hasLabel = opts.hasLabel || "Has it";
    function isDraft(st) { st = String(st || "").toUpperCase(); return st && st !== "YES" && st.indexOf("PUBLISH") < 0; }
    return new Promise(function (resolve) {
      var done = false;
      var body = document.createElement("div");
      body.innerHTML = '<div class="apk-bar"><input type="search" class="apk-q" placeholder="Search account, ID or Business Center…" autocomplete="off"></div>'
        + '<div class="apk-extra"></div>'
        + '<div class="apk-tabs"></div><div class="apk-bcs"></div><div class="apk-list"><div class="muted" style="padding:16px;">Loading accounts…</div></div>';
      var extraEls = {};
      if (opts.extra && opts.extra.length) {
        var exWrap = body.querySelector(".apk-extra"); exWrap.style.margin = "0 0 10px";
        opts.extra.forEach(function (fld) {
          var lab = document.createElement("label"); lab.style.cssText = "display:block;margin-bottom:6px;font-size:11.5px;color:var(--text-dim);";
          lab.innerHTML = esc(fld.label || fld.name) + '<input type="' + esc(fld.type || "text") + '" placeholder="' + esc(fld.placeholder || "") + '" style="width:100%;margin-top:3px;">';
          extraEls[fld.name] = lab.querySelector("input"); exWrap.appendChild(lab);
        });
      }
      var footer = document.createElement("div");
      footer.innerHTML = '<span class="apk-count muted" style="margin-right:auto;">0 selected</span>'
        + '<button type="button" class="btn" data-close>Cancel</button>'
        + '<button type="button" class="btn primary apk-go" disabled>' + esc(opts.confirmLabel || "Use selected") + '</button>';
      var modal = UI.modal({ title: opts.title || "Select accounts", body: body, footer: footer, wide: true,
        onClose: function () { if (!done) { done = true; resolve(null); } } });
      if (modal.el) { modal.el.style.width = "min(920px, 94vw)"; modal.el.style.maxWidth = "920px"; }

      var sel = {}, q = "", bcOn = {}, stateF = "", haveF = "";
      var listEl = body.querySelector(".apk-list"), qEl = body.querySelector(".apk-q"),
          tabsEl = body.querySelector(".apk-tabs"), bcsEl = body.querySelector(".apk-bcs"),
          countEl = footer.querySelector(".apk-count"), goEl = footer.querySelector(".apk-go");

      load(opts.refresh).then(render).catch(function () { listEl.innerHTML = '<div class="muted" style="padding:16px;">Couldn\'t load accounts — try again.</div>'; });

      function render(d) {
        var accts = (d.accounts || []).filter(function (a) { return !exclude[String(a.id)]; });
        if (opts.onlyBc) accts = accts.filter(function (a) { return String(a.bc) === String(opts.onlyBc); });
        var cnt = { fresh: 0, used: 0, active: 0, cooldown: 0, blocked: 0 };
        accts.forEach(function (a) { if (cnt[a.state] != null) cnt[a.state]++; });
        tabsEl.innerHTML = STATE_TABS.map(function (t) {
          var n = t[0] ? (cnt[t[0]] || 0) : accts.length;
          return '<button type="button" class="chip apk-tab' + (t[0] === "" ? " on" : "") + '" data-s="' + t[0] + '">' + t[1] + ' <span class="n">' + n + '</span></button>';
        }).join("");
        if (hasOn) {
          var nHas = accts.filter(function (a) { return has[String(a.id)]; }).length;
          tabsEl.innerHTML += '<span class="apk-sep"></span>'
            + '<button type="button" class="chip apk-have" data-h="missing" title="Accounts that don\'t have it yet — the ones a copy can go to">Missing it <span class="n">' + (accts.length - nHas) + '</span></button>'
            + '<button type="button" class="chip apk-have" data-h="has" title="Accounts that already have it (by name)">' + esc(hasLabel) + ' <span class="n">' + nHas + '</span></button>';
        }
        bcsEl.innerHTML = opts.onlyBc ? "" : (d.bcs || []).map(function (b) { return '<button type="button" class="chip apk-bc" data-bc="' + esc(b.id) + '">' + esc(b.name) + ' <span class="n">' + b.n + '</span></button>'; }).join("");
        var groups = {};
        accts.forEach(function (a) { (groups[a.bc_name] = groups[a.bc_name] || []).push(a); });
        var html = "";
        Object.keys(groups).sort().forEach(function (g) {
          html += '<div class="apk-group"><div class="apk-ghead"><label><input type="checkbox" class="apk-gall"> <b>' + esc(g) + '</b></label><span class="apk-gn muted"></span></div>';
          // accounts that already have it go last — the ones still missing it are what you pick from
          groups[g].slice().sort(function (x, y) { return (has[String(x.id)] ? 1 : 0) - (has[String(y.id)] ? 1 : 0); }).forEach(function (a) {
            var h = has[String(a.id)];
            var on = (pre[String(a.id)] && !h) ? " checked" : ""; if (on) sel[String(a.id)] = true;
            html += '<label class="apk-row' + (h ? " is-has" : "") + '" data-bc="' + esc(a.bc) + '" data-state="' + esc(a.state) + '" data-has="' + (h ? "1" : "") + '" data-search="' + esc((a.name + " " + a.id + " " + a.bc_name).toLowerCase()) + '"'
              + (h ? ' title="Already has it — a copy here would be skipped"' : "") + '>'
              + '<input type="checkbox" class="apk-cb" value="' + esc(a.id) + '"' + on + (h ? " disabled" : "") + '>'
              + '<span class="apk-nm">' + esc(a.name) + '</span>'
              + (h ? '<span class="apk-has' + (isDraft(h) ? " draft" : "") + '">✓ ' + esc(hasLabel) + (isDraft(h) ? " · " + esc(h.toLowerCase().replace(/_/g, " ")) : "") + '</span>' : "")
              + '<span class="apk-st apk-st-' + esc(a.state) + '">' + esc(a.state) + '</span>'
              + '<span class="mono muted apk-id">' + esc(a.id) + '</span></label>';
          });
          html += '</div>';
        });
        listEl.innerHTML = html || '<div class="muted" style="padding:16px;">No accounts.</div>';
        wire(); apply();
      }
      function rowsOf() { return Array.prototype.slice.call(listEl.querySelectorAll(".apk-row")); }
      function apply() {
        var bcList = Object.keys(bcOn).filter(function (k) { return bcOn[k]; });
        rowsOf().forEach(function (r) {
          var ok = (!q || r.dataset.search.indexOf(q) >= 0) && (!stateF || r.dataset.state === stateF) && (!bcList.length || bcList.indexOf(r.dataset.bc) >= 0)
            && (!haveF || (haveF === "has") === (r.dataset.has === "1"));
          r.style.display = ok ? "" : "none";
        });
        listEl.querySelectorAll(".apk-group").forEach(function (g) {
          var vis = Array.prototype.slice.call(g.querySelectorAll(".apk-row")).filter(function (r) { return r.style.display !== "none"; });
          g.style.display = vis.length ? "" : "none";
          var pickable = vis.filter(function (r) { return !r.querySelector(".apk-cb").disabled; });
          var selN = pickable.filter(function (r) { return r.querySelector(".apk-cb").checked; }).length;
          var hasN = vis.length - pickable.length;
          g.querySelector(".apk-gn").textContent = selN + " / " + pickable.length + (hasOn ? " · " + hasN + " " + (hasN === 1 ? "has" : "have") + " it" : "");
          g.querySelector(".apk-gall").checked = pickable.length > 0 && selN === pickable.length;
          g.querySelector(".apk-gall").disabled = !pickable.length;
        });
        var n = Object.keys(sel).filter(function (k) { return sel[k]; }).length;
        countEl.textContent = n + " selected"; goEl.disabled = !n;
      }
      function wire() {
        listEl.querySelectorAll(".apk-cb").forEach(function (cb) { cb.addEventListener("change", function () { sel[cb.value] = cb.checked; apply(); }); });
        listEl.querySelectorAll(".apk-gall").forEach(function (all) {
          all.addEventListener("change", function () {
            all.closest(".apk-group").querySelectorAll(".apk-row").forEach(function (r) {
              var cb = r.querySelector(".apk-cb");
              if (r.style.display !== "none" && !cb.disabled) { cb.checked = all.checked; sel[cb.value] = cb.checked; }
            });
            apply();
          });
        });
        tabsEl.querySelectorAll(".apk-tab").forEach(function (b) { b.addEventListener("click", function () { tabsEl.querySelectorAll(".apk-tab").forEach(function (o) { o.classList.remove("on"); }); b.classList.add("on"); stateF = b.dataset.s; apply(); }); });
        tabsEl.querySelectorAll(".apk-have").forEach(function (b) { b.addEventListener("click", function () {
          var on = haveF !== b.dataset.h; haveF = on ? b.dataset.h : "";
          tabsEl.querySelectorAll(".apk-have").forEach(function (o) { o.classList.toggle("on", o.dataset.h === haveF); }); apply(); }); });
        bcsEl.querySelectorAll(".apk-bc").forEach(function (b) { b.addEventListener("click", function () { bcOn[b.dataset.bc] = !bcOn[b.dataset.bc]; b.classList.toggle("on", bcOn[b.dataset.bc]); apply(); }); });
      }
      qEl.addEventListener("input", function () { q = this.value.trim().toLowerCase(); apply(); });
      goEl.addEventListener("click", function () {
        var ids = Object.keys(sel).filter(function (k) { return sel[k]; });
        if (!ids.length) return;
        done = true; if (modal.close) modal.close();
        var out = ids;   // back-compat: plain id list when no extra fields were requested
        if (opts.extra && opts.extra.length) {
          var vals = {}; opts.extra.forEach(function (f) { vals[f.name] = extraEls[f.name] ? extraEls[f.name].value.trim() : ""; });
          out = { ids: ids, values: vals };
        }
        if (opts.onConfirm) opts.onConfirm(out);
        resolve(out);
      });
    });
  };
})();
