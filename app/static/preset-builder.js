/* preset-builder.js — the stepped preset builder: choice cards bound to hidden selects,
   objective → valid destinations, cost-cap ladder chips, live phone preview + per-launch
   summary, creative/spark pickers, display-card gallery + upload, step memory + validation. */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); }, $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var esc = UI.esc;
  var RULES = JSON.parse($("#pbRules").textContent), DEST_LABELS = JSON.parse($("#pbDest").textContent), META = JSON.parse($("#pbMeta").textContent);
  var form = $("#presetForm");

  // ---- choice cards ↔ hidden <select> ----------------------------------------------------------
  function bindCards(gridId, selId, onChange) {
    var grid = $("#" + gridId), sel = $("#" + selId);
    function paint() { $$(".pb-card", grid).forEach(function (c) { c.classList.toggle("on", c.dataset.val === sel.value); }); }
    grid.addEventListener("click", function (e) { var c = e.target.closest(".pb-card"); if (!c || c.hidden) return; sel.value = c.dataset.val; paint(); if (onChange) onChange(); refresh(); });
    paint();
    return { paint: paint, sel: sel, grid: grid };
  }
  // ---- objective → destinations ----------------------------------------------------------------
  var objSel = $("#objType"), destSel = $("#destType");
  function syncObjective() {
    var rule = RULES[objSel.value] || { destinations: ["website"], event: "none" };
    $$("#destCards .pb-card").forEach(function (c) { c.hidden = rule.destinations.indexOf(c.dataset.val) < 0; });
    if (rule.destinations.indexOf(destSel.value) < 0) destSel.value = rule.destinations[0];
    // goals that optimise for a conversion have no "website without pixel" option — say why, offer the way out
    var note = $("#destNote"); if (note) note.hidden = rule.event === "none" || rule.destinations.indexOf("website") >= 0;
    dest.paint(); syncDest();
  }
  var toClick = $("#destToClick"); if (toClick) toClick.addEventListener("click", function () { objSel.value = "TRAFFIC"; obj.paint(); syncObjective(); destSel.value = "website"; dest.paint(); syncDest(); refresh(); });
  function syncDest() {
    var objRule = RULES[objSel.value] || { event: "none" }, d = destSel.value;
    $$("[data-dest]").forEach(function (el) {
      var ok = el.dataset.dest.split(" ").indexOf(d) >= 0;
      if (el.dataset.needsEvent && objRule.event === "none") ok = false;
      el.hidden = !ok;
    });
  }
  var obj = bindCards("objCards", "objType", syncObjective);
  var dest = bindCards("destCards", "destType", syncDest);
  syncObjective();

  // ---- budget mode + ladder --------------------------------------------------------------------
  var budget = bindCards("budgetCards", "budgetMode", syncBudget);
  var STRATEGY_LABEL = { ABO: "Ad group budget (ABO)", BUDGET_MODE_DAY: "Campaign budget · daily (CBO)",
                         BUDGET_MODE_TOTAL: "Campaign budget · lifetime (CBO)" };
  function syncBudget() {
    // the choice lives in step 1, where TikTok Ads Manager puts it; step 4 holds the
    // amounts and echoes the choice so the two are never read apart
    var mode = $("#budgetMode").value, abo = mode === "ABO";
    $("#cboBudgetField").hidden = abo;
    $("#aboBudgetField").hidden = !abo;
    var echo = $("#pbStrategyEcho");
    if (echo) echo.textContent = STRATEGY_LABEL[mode] || mode;
  }
  syncBudget();
  var ladderInput = $("#ladderInput"), ladder = $("#ladder");
  function ladderVals() { return ladderInput.value.replace(/,/g, " ").split(/\s+/).filter(Boolean).map(Number).filter(function (v) { return v > 0; }); }
  function paintLadder() {
    var vals = ladderVals();
    ladder.innerHTML = vals.map(function (v, i) { return '<span class="pk-chip">$' + v.toFixed(2) + '<button type="button" data-rm="' + i + '" title="Remove">✕</button></span>'; }).join("");
    $("#ladderHint").textContent = vals.length ? vals.length + " cap" + (vals.length > 1 ? "s" : "") + " → " + vals.length + " ad group" + (vals.length > 1 ? "s" : "") + " per copy" : "Empty = lowest cost (no cap).";
  }
  ladder.addEventListener("click", function (e) { var b = e.target.closest("[data-rm]"); if (!b) return; var vals = ladderVals(); vals.splice(+b.dataset.rm, 1); ladderInput.value = vals.join(" "); paintLadder(); refresh(); });
  function addCap() { var v = parseFloat(String($("#ladderNew").value).replace(",", ".")); if (!(v > 0)) return; ladderInput.value = ladderVals().concat([v]).join(" "); $("#ladderNew").value = ""; paintLadder(); refresh(); }
  $("#ladderAdd").addEventListener("click", addCap);
  $("#ladderNew").addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); addCap(); } });
  paintLadder();
  var sched = $("#schedType");
  function syncSched() { $$("[data-sched]").forEach(function (el) { el.hidden = el.dataset.sched !== sched.value; }); }
  sched.addEventListener("change", syncSched); syncSched();

  // ---- gender segment, placements toggle -------------------------------------------------------
  $$("[data-seg-for]").forEach(function (seg) {
    var sel = $("#" + seg.dataset.segFor);
    function paint() { $$("button", seg).forEach(function (b) { b.classList.toggle("on", b.dataset.val === sel.value); }); }
    seg.addEventListener("click", function (e) { var b = e.target.closest("button[data-val]"); if (!b) return; sel.value = b.dataset.val; paint(); refresh(); });
    paint();
  });
  $("#placementAuto").addEventListener("change", function () { $("#placementMode").value = this.checked ? "auto" : "tiktok"; });

  // ---- creative source, pickers, spark list ----------------------------------------------------
  var picked = { video: META.video, carousel: META.carousel, spark: META.spark };
  var creative = bindCards("creativeCards", "creativeSource", syncCreative);
  var smart = $("#smartCreative");
  function syncCreative() {
    var v = $("#creativeSource").value, lib = v === "library", car = v === "carousel";
    $("#sparkField").hidden = v !== "spark"; $("#videoPick").hidden = !lib || smart.checked; $("#carouselPick").hidden = !car;
    $("#poolModes").hidden = !(lib || car);
    $("#smartCreativeToggle").hidden = !lib; if (car || v === "spark") smart.checked = false;
    $("#smartCreativeOpts").hidden = !(lib && smart.checked);
  }
  smart.addEventListener("change", function () { syncCreative(); refresh(); });
  syncCreative();
  function setPick(kind, it) {
    picked[kind] = it; $("#" + kind + "Id").value = it ? it.id : "";
    var chip = $("#" + kind + "Chip"); chip.hidden = !it;
    if (it) { var img = chip.querySelector("img"); if (img) img.src = it.poster || "/creatives/" + it.id + "/poster"; $("#" + kind + "ChipName").textContent = it.name; }
    $("#" + kind + "Auto").hidden = !!it;
    var b = $('[data-pick="' + kind + '"]'); if (b) b.textContent = it ? "Change…" : "Pin a specific " + kind + "…";
    refresh();
  }
  $$("[data-pick]").forEach(function (b) {
    b.addEventListener("click", function () {
      var kind = b.dataset.pick, cur = picked[kind];
      UI.pickCreatives({ kind: kind, kinds: [kind], multi: false, state: "all", selected: cur ? [Number(cur.id)] : [], title: "Pin a " + kind, hint: "Every account this preset launches to gets this one." })
        .then(function (items) { if (items && items.length) setPick(kind, items[0]); });
    });
  });
  $$("[data-unpin]").forEach(function (x) { x.addEventListener("click", function () {
    var k = x.dataset.unpin;
    if (k === "spark") { picked.spark = null; $("#sparkId").value = ""; $("#sparkChip").hidden = true; $("#sparkAuto").hidden = false; $("#sparkChoose").textContent = "Choose a spark…"; refresh(); }
    else setPick(k, null);
  }); });
  $("#sparkChoose").addEventListener("click", function () { var l = $("#sparkList"); l.hidden = !l.hidden; if (!l.hidden) $("#sparkQ").focus(); });
  $("#sparkQ").addEventListener("input", function () { var q = this.value.trim().toLowerCase(); $$("#sparkList .cl-spark").forEach(function (r) { r.hidden = !!q && r.dataset.search.indexOf(q) < 0; }); });
  $("#sparkList").addEventListener("click", function (e) {
    var r = e.target.closest(".cl-spark"); if (!r) return;
    picked.spark = { id: r.dataset.id, name: r.dataset.name, thumb: r.dataset.thumb }; $("#sparkId").value = r.dataset.id;
    var chip = $("#sparkChip"); chip.hidden = false; $("#sparkChipName").textContent = r.dataset.name;
    var img = chip.querySelector("img"), ph = chip.querySelector(".pb-pick-ph");
    if (r.dataset.thumb) { if (!img) { img = document.createElement("img"); chip.insertBefore(img, chip.firstChild); } img.src = r.dataset.thumb; if (ph) ph.remove(); }
    $("#sparkAuto").hidden = true; $("#sparkChoose").textContent = "Change…"; $("#sparkList").hidden = true; refresh();
  });

  // ---- display cards ---------------------------------------------------------------------------
  var dcSel = $("#displayCardSel"), dcGrid = $("#dcGrid");
  function paintCards() { $$(".pb-dc[data-val]", dcGrid).forEach(function (c) { c.classList.toggle("on", c.dataset.val === dcSel.value); }); $("#displayCardDel").hidden = !dcSel.value; if (dcSel.value) $("#displayCardDelForm").action = "/display-cards/" + dcSel.value + "/delete?next=" + encodeURIComponent(location.pathname); }
  dcGrid.addEventListener("click", function (e) { var c = e.target.closest(".pb-dc[data-val]"); if (!c) return; dcSel.value = c.dataset.val; paintCards(); refresh(); });
  paintCards();
  var dcFile = $("#displayCardFile"), dcUp = $("#displayCardUp"), dcMsg = $("#displayCardMsg");
  dcFile.addEventListener("change", function () { dcUp.hidden = !dcFile.files.length; if (dcFile.files.length) dcMsg.textContent = dcFile.files[0].name + " ready — click Upload & select."; });
  dcUp.addEventListener("click", function () {
    var f = dcFile.files[0]; if (!f) { dcMsg.textContent = "Pick an image file first."; return; }
    var fd = new FormData(); fd.append("file", f); fd.append("name", $("#displayCardName").value);
    dcMsg.textContent = "Uploading…"; dcUp.disabled = true;
    fetch("/display-cards/upload", { method: "POST", body: fd, headers: { "Accept": "application/json" }, credentials: "same-origin" })
      .then(function (r) { return r.text().then(function (t) { var j; try { j = JSON.parse(t); } catch (e) { j = { error: "Unexpected reply from the server (HTTP " + r.status + (r.redirected ? ", redirected — are you still logged in?" : "") + ")." }; } return { ok: r.ok && !r.redirected, j: j }; }); })
      .then(function (res) {
        dcUp.disabled = false;
        if (!res.ok) { dcMsg.textContent = res.j.error || "Upload failed."; return; }
        var o = document.createElement("option"); o.value = res.j.id; o.textContent = res.j.name; o.selected = true; dcSel.appendChild(o);
        var tile = UI.el('<button type="button" class="pb-dc" data-val="' + res.j.id + '" data-name="' + esc(res.j.name) + '"><img src="/display-cards/' + res.j.id + '/image" alt=""><span>' + esc(res.j.name) + "</span></button>");
        dcGrid.insertBefore(tile, dcGrid.querySelector(".pb-dc-up")); paintCards(); refresh();
        dcMsg.textContent = res.j.message + " Save the preset to keep it selected."; dcFile.value = ""; $("#displayCardName").value = ""; dcUp.hidden = true;
      })
      .catch(function () { dcUp.disabled = false; dcMsg.textContent = "Upload failed — check the connection and try again."; });
  });

  // ---- live preview + per-launch summary + rail subtitles -------------------------------------
  var CTA_LABEL = function (v) { return v === "AUTO" ? "Learn more" : v.replace(/_/g, " ").toLowerCase().replace(/^\w/, function (c) { return c.toUpperCase(); }); };
  function optText(sel) { var s = $(sel); return s && s.options[s.selectedIndex] ? s.options[s.selectedIndex].text : ""; }
  function refresh() {
    var src = $("#creativeSource").value, pin = src === "library" ? picked.video : (src === "carousel" ? picked.carousel : picked.spark);
    var media = $("#phMedia");
    if (src === "spark") media.innerHTML = pin && pin.thumb ? '<img src="' + esc(pin.thumb) + '" alt="">' : '<div class="pb-phone-ph">✦<br><span>' + (pin ? esc(pin.name) : "spark picked at launch") + "</span></div>";
    else if (pin) {
      var ids = [];
      if (src === "carousel" && pin.slides) { try { ids = JSON.parse(pin.slides); } catch (e) { ids = []; } }
      if (src === "carousel" && pin.slide_ids) ids = pin.slide_ids;
      if (ids.length > 1) UI.slides(media, ids, { poster: pin.poster }); else { media.classList.remove("slides"); media.innerHTML = '<img src="' + esc(pin.poster || "/creatives/" + pin.id + "/poster") + '" alt="">'; }
    } else media.innerHTML = '<div class="pb-phone-ph">' + (src === "carousel" ? "🖼" : "🎬") + '<br><span>next unused ' + (src === "carousel" ? "carousel" : "video") + " per launch</span></div>";
    var txt = $("#pbAdText").value.trim(); $("#phText").textContent = txt || ($("#adTextMode").value === "pool" && !$("#poolModes").hidden ? "next line from Ad texts" : "Ad text…");
    $("#adTextCount").textContent = txt.length + " / 100";
    $("#phCta").textContent = CTA_LABEL($("#ctaSel").value) + ($("#ctaSel").value === "AUTO" ? " ·" : "");
    var card = $("#phCard"), dcOpt = dcSel.options[dcSel.selectedIndex];
    card.hidden = !dcSel.value || src === "carousel";
    if (!card.hidden) { card.querySelector("img").src = "/display-cards/" + dcSel.value + "/image"; card.querySelector("span").textContent = dcOpt ? dcOpt.text : ""; }
    $("#phSub").textContent = (src === "spark" ? "spark post" : (src === "carousel" ? "carousel" : "video")) + (pin ? " · " + pin.name : "") + ($("#smartCreative").checked && src === "library" ? " · Smart Creative" : "") + (dcSel.value && src !== "carousel" ? " · display card" : "");
    // per-launch numbers
    var caps = ladderVals(), dup = Math.max(1, parseInt($("#pbDup").value, 10) || 1), n = Math.max(1, caps.length) * dup, abo = $("#budgetMode").value === "ABO";
    var agb = parseFloat($("#pbAgb").value) || 0, cbo = parseFloat($("#pbCbo").value) || 0, smartPlus = $('[name="smart_plus"]').checked;
    if (smartPlus) n = 1;
    var daily = abo ? agb * n : cbo;
    $("#pbCalc").innerHTML = '<b>' + n + "</b> ad group" + (n > 1 ? "s" : "") + " per campaign" + (caps.length ? " (" + caps.length + " cap" + (caps.length > 1 ? "s" : "") + " × " + dup + ")" : " (no cap × " + dup + ")") + (abo ? " · <b>" + UI.money(daily, 0) + "</b>/day if every ad group spends" : " · <b>" + UI.money(cbo, 0) + "</b> " + ($("#budgetMode").value === "BUDGET_MODE_TOTAL" ? "total" : "/day") + " (CBO)") + (smartPlus ? " · Smart+ → one smart ad group" : "");
    $("#pbBudgetMeta").textContent = n + " ad group" + (n > 1 ? "s" : "") + " · " + UI.money(daily, 0) + (abo || $("#budgetMode").value === "BUDGET_MODE_DAY" ? "/day" : " total");
    var rule = RULES[objSel.value] || {}, destLab = DEST_LABELS[destSel.value] || destSel.value;
    var rows = [["Goal", optText("#objType")], ["Destination", destLab + (destSel.value === "pixel" && optText('[name="optimization_event"]') && $('[name="optimization_event"]').value ? " · " + $('[name="optimization_event"]').value : "")],
      ["Lands on", destSel.value === "instant_page" ? ($('[name="instant_page_name"]').value || "—") : destSel.value === "lead_form" ? ($('[name="lead_form_name"]').value || "—") : ($("#pbLanding").value || "—")],
      ["Ad groups", n + (caps.length ? " · caps $" + caps.join(" / $") : " · lowest cost")], ["Budget", abo ? UI.money(agb, 2) + " per ad group" : UI.money(cbo, 2) + " campaign"],
      ["Targeting", [$("#pbLoc").value ? $("#pbLoc").value.split(/\s+/).filter(Boolean).length + " location" + ($("#pbLoc").value.split(/\s+/).filter(Boolean).length > 1 ? "s" : "") : "all locations", { GENDER_UNLIMITED: "all genders", GENDER_MALE: "men", GENDER_FEMALE: "women" }[$("#genderSel").value], $$('input[name="age_groups"]').length ? $$('input[name="age_groups"]').length + " age groups" : "all ages"].join(" · ")],
      ["Creative", $("#phSub").textContent], ["CTA", CTA_LABEL($("#ctaSel").value) + ($("#ctaSel").value === "AUTO" ? " (auto)" : "")]];
    $("#pbSumm").innerHTML = rows.map(function (r) { return '<div><span class="muted">' + r[0] + "</span><b>" + esc(r[1]) + "</b></div>"; }).join("");
    var sums = { campaign: optText("#objType") + (smartPlus ? " · Smart+" : ""), location: destLab, targeting: rows[5][1], budget: n + " ad groups · " + UI.money(daily, 0) + (abo ? "/day" : ""), ad: $("#phSub").textContent, addons: dcSel.value ? (dcOpt ? dcOpt.text : "display card") : "no display card" };
    Object.keys(sums).forEach(function (k) { var el = $('[data-sum="' + k + '"]'); if (el) el.textContent = sums[k]; });
    var nm = $("#pbName").value.trim(); if (nm) $("#pbTitle").textContent = nm;

  }
  form.addEventListener("input", refresh); form.addEventListener("change", refresh);
  form.addEventListener("click", function (e) { if (e.target.closest(".cp-wrap")) setTimeout(refresh, 0); });
  document.addEventListener("DOMContentLoaded", refresh); setTimeout(refresh, 0);   // chip pickers build their hidden inputs on DOMContentLoaded
  refresh();

  // ---- steps: one at a time, remembered per preset; invalid fields pull you to their step -----
  var tabs = $$("#pfNav a[data-tab]"), key = "adops-preset-step-" + META.preset_id;
  function show(k) {
    if (!$('.stab[data-tab="' + k + '"]')) k = "campaign";
    $$(".stab").forEach(function (s) { s.hidden = s.dataset.tab !== k; });
    tabs.forEach(function (a) { a.classList.toggle("on", a.dataset.tab === k); });
    try { sessionStorage.setItem(key, k); } catch (e) {}
    if (location.hash !== "#" + k) history.replaceState(null, "", "#" + k);
    window.scrollTo(0, 0);
  }
  tabs.forEach(function (a) { a.addEventListener("click", function (e) { e.preventDefault(); show(a.dataset.tab); }); });
  document.addEventListener("click", function (e) { var b = e.target.closest("[data-next]"); if (b) show(b.dataset.next); });
  var start = (location.hash || "").slice(1); if (!start) { try { start = sessionStorage.getItem(key) || ""; } catch (e) {} }
  show(start || "campaign");
  form.addEventListener("submit", function (e) {
    if (form.checkValidity()) { if ($("#creativeSource").value === "library" && $("#adTextMode").value === "fixed" && !$("#pbAdText").value.trim() && !$("#smartCreative").checked) { e.preventDefault(); show("ad"); adopsToast && adopsToast("err", "TikTok needs ad text — type one or switch to Unique per launch."); $("#pbAdText").focus(); } return; }
    e.preventDefault();
    var bad = form.querySelector(":invalid"), sct = bad && bad.closest(".stab");
    if (sct) show(sct.dataset.tab);
    if (bad) { bad.reportValidity(); bad.focus(); }
  });
})();
