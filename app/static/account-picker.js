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
    applyFilter();
    return { boxes: boxes, rows: rows, selected: selected, selectRows: selectRows, applyFilter: applyFilter, syncCount: syncCount };
  }
  window.AccountPicker = { init: init };
})();
