/* Account picker (markup: templates/_account_picker.html) — search, state filter,
   Business Center tags (remembered per browser), grouped checkboxes.
   window.AccountPicker.init({ onChange }) → { boxes, rows, selected(), selectRows(pred), applyFilter() } */
(function () {
  function init(opts) {
    opts = opts || {};
    var boxes = Array.prototype.slice.call(document.querySelectorAll(".sl-acct"));
    var rows = Array.prototype.slice.call(document.querySelectorAll(".sl-row"));
    var count = document.getElementById("slCount"), shown = document.getElementById("slShown");
    var search = document.getElementById("slSearch"), filterBtns = Array.prototype.slice.call(document.querySelectorAll("#slFilter button"));
    var filterState = "";
    var bcTags = Array.prototype.slice.call(document.querySelectorAll("#slBcTags .bc-tag[data-bc]"));
    var bcClear = document.getElementById("slBcClear");
    var activeBcs = {};
    var storeKey = opts.storeKey || "adops-sl-bc";
    try { (JSON.parse(localStorage.getItem(storeKey) || "[]") || []).forEach(function (b) { activeBcs[b] = true; }); } catch (e) {}
    function bcActiveList() { return Object.keys(activeBcs).filter(function (k) { return activeBcs[k]; }); }
    function syncBcTags() {
      var known = {}; bcTags.forEach(function (t) { known[t.getAttribute("data-bc")] = true; });
      Object.keys(activeBcs).forEach(function (k) { if (!known[k]) delete activeBcs[k]; });
      bcTags.forEach(function (t) { t.classList.toggle("on", !!activeBcs[t.getAttribute("data-bc")]); });
      if (bcClear) bcClear.hidden = bcActiveList().length === 0;
      try { localStorage.setItem(storeKey, JSON.stringify(bcActiveList())); } catch (e) {}
    }
    bcTags.forEach(function (t) {
      t.addEventListener("click", function () {
        var k = t.getAttribute("data-bc"); activeBcs[k] = !activeBcs[k];
        syncBcTags(); applyFilter();
      });
    });
    if (bcClear) bcClear.addEventListener("click", function () { activeBcs = {}; syncBcTags(); applyFilter(); });
    syncBcTags();

    function visible(r) { return !r.hidden; }
    function applyFilter() {
      var q = (search ? search.value : "").trim().toLowerCase(), n = 0, anyBc = bcActiveList().length > 0;
      rows.forEach(function (r) {
        var st = r.getAttribute("data-state");
        var okState = !filterState || st === filterState;
        var okText = !q || r.getAttribute("data-search").indexOf(q) >= 0;
        var okBc = !anyBc || !!activeBcs[r.getAttribute("data-bc")];
        r.hidden = !(okState && okText && okBc);
        if (!r.hidden) n++;
      });
      document.querySelectorAll("[data-group]").forEach(function (g) {
        var vis = Array.prototype.filter.call(g.querySelectorAll(".sl-row"), visible).length;
        g.hidden = vis === 0;
        g.querySelector(".sl-group-n").textContent = vis;
      });
      var empty = document.getElementById("slEmpty"); if (empty) empty.hidden = n > 0;
      if (shown) shown.textContent = n === rows.length ? "" : "· " + n + " of " + rows.length + " shown";
      syncCount();
    }
    function selected() { return boxes.filter(function (b) { return b.checked; }); }
    function syncCount() {
      var n = selected().length;
      if (count) count.textContent = n + " selected";
      document.querySelectorAll("[data-group]").forEach(function (g) {
        var vis = Array.prototype.filter.call(g.querySelectorAll(".sl-row"), visible);
        var on = vis.filter(function (r) { return r.querySelector("input").checked; }).length;
        var all = g.querySelector(".sl-group-all");
        if (all) { all.checked = vis.length > 0 && on === vis.length; all.indeterminate = on > 0 && on < vis.length; }
      });
      if (opts.onChange) opts.onChange(n);
    }
    function selectRows(pred) {
      rows.forEach(function (r) { var b = r.querySelector("input"); if (!b.disabled) b.checked = pred(r); });
      syncCount();
    }
    boxes.forEach(function (b) { b.addEventListener("change", syncCount); });
    document.querySelectorAll(".sl-group-all").forEach(function (all) {
      all.addEventListener("change", function () {
        var g = all.closest("[data-group]");
        g.querySelectorAll(".sl-row").forEach(function (r) { var b = r.querySelector("input"); if (!r.hidden && !b.disabled) b.checked = all.checked; });
        syncCount();
      });
    });
    var elAll = document.getElementById("slAll"), elNone = document.getElementById("slNone"), elFresh = document.getElementById("slFresh");
    if (elAll) elAll.addEventListener("click", function () { selectRows(function (r) { return !r.hidden || r.querySelector("input").checked; }); });
    if (elNone) elNone.addEventListener("click", function () { selectRows(function () { return false; }); });
    if (elFresh) elFresh.addEventListener("click", function () { selectRows(function (r) { return r.getAttribute("data-state") === "fresh"; }); });
    if (search) search.addEventListener("input", applyFilter);
    filterBtns.forEach(function (btn) {
      btn.addEventListener("click", function () {
        filterBtns.forEach(function (o) { o.classList.remove("on"); }); btn.classList.add("on");
        filterState = btn.getAttribute("data-f"); applyFilter();
      });
    });
    // ↻ Refresh: re-read every account's TikTok status in place (no page reload — the
    // picks, the preset and the creative choice stay as they are)
    var PILL = { fresh: "ok", used: "dim", active: "warn", cooldown: "err", blocked: "err" };
    var LABEL = { fresh: "fresh", used: "used", active: "live", cooldown: "cooling", blocked: "blocked" };
    var TITLE = { fresh: "Never launched — best for a first campaign", used: "Has campaigns, none active", active: "Has an active campaign right now", cooldown: "Cooling down after launch failures" };
    function applyStates(info, counts) {
      rows.forEach(function (r) {
        var b = r.querySelector("input"), d = info[b.value]; if (!d) return;
        var st = d.state, pill = r.querySelector(".pill"), reason = r.querySelector(".sl-reason");
        r.setAttribute("data-state", st);
        b.disabled = st === "blocked"; if (b.disabled) b.checked = false;
        if (pill) { pill.className = "pill " + (PILL[st] || "dim"); pill.textContent = LABEL[st] || st; pill.title = st === "blocked" ? "Cannot launch: " + (d.reason || "") : (TITLE[st] || ""); }
        if (!reason && st === "blocked") { reason = document.createElement("span"); reason.className = "muted sl-reason"; reason.style.fontSize = "10.5px"; pill.insertAdjacentElement("afterend", reason); }
        if (reason) { reason.textContent = st === "blocked" ? (d.reason || "") : ""; reason.hidden = st !== "blocked"; }
      });
      if (counts) filterBtns.forEach(function (btn) { var f = btn.getAttribute("data-f"), n = btn.querySelector(".n"); if (f && n && counts[f] !== undefined) n.textContent = counts[f]; });
      applyFilter();
    }
    var refreshBtn = document.getElementById("slRefresh");
    if (refreshBtn) refreshBtn.addEventListener("click", function () {
      refreshBtn.disabled = true; var was = refreshBtn.textContent; refreshBtn.textContent = "Refreshing…";
      fetch("/super-launcher/refresh-accounts", { method: "POST", headers: { "X-Requested-With": "fetch", "Accept": "application/json" }, credentials: "same-origin" })
        .then(function (r) { return r.json(); })
        .then(function (d) {
          if (!d || !d.ok) throw new Error((d && d.error) || "refresh failed");
          applyStates(d.info || {}, d.counts);
          if (window.adopsToast) adopsToast("ok", d.changed ? d.changed + " account(s) changed state" + (d.synced ? " · " + d.synced + " statuses re-read from TikTok" : "") : "Statuses re-read from TikTok — nothing changed" + (d.queued ? " · campaign sync running in the background" : ""));
        })
        .catch(function (e) { if (window.adopsToast) adopsToast("err", "Couldn't refresh: " + e.message); })
        .then(function () { refreshBtn.disabled = false; refreshBtn.textContent = was; });
    });
    applyFilter();
    return { boxes: boxes, rows: rows, selected: selected, selectRows: selectRows, applyFilter: applyFilter, syncCount: syncCount, applyStates: applyStates };
  }
  window.AccountPicker = { init: init };
})();
