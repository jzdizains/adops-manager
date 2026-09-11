/* Ad accounts: filter by Business Center, by how recently the account was connected, and
   by name/id. All in the browser — the rows are already on the page, so there is no round
   trip and no job. The choices are remembered per browser so the page opens where it was
   left; they are conveniences only, and the table renders correctly without them. */
(function () {
  "use strict";
  var body = document.getElementById("acaBody");
  if (!body) return;
  var rows = Array.prototype.slice.call(body.querySelectorAll("tr.aca-row"));
  var q = document.getElementById("acaQ"), bc = document.getElementById("acaBc");
  var newest = document.getElementById("acaNewest"), shown = document.getElementById("acaShown");
  var empty = document.getElementById("acaEmpty"), days = "";
  var order = rows.slice();                       // the server's order, to go back to

  var KEY = "adops-bca-accounts";
  function save() {
    try {
      localStorage.setItem(KEY, JSON.stringify({ bc: bc.value, days: days, newest: newest.checked }));
    } catch (e) {}                                 // private window / blocked storage: just don't remember
  }
  function restore() {
    var v = {};
    try { v = JSON.parse(localStorage.getItem(KEY) || "{}") || {}; } catch (e) { v = {}; }
    if (v.bc && bc.querySelector('option[value="' + String(v.bc).replace(/"/g, "") + '"]')) bc.value = v.bc;
    if (v.days) {
      var b = document.querySelector('#acaNew button[data-days="' + String(v.days).replace(/"/g, "") + '"]');
      if (b) { setDays(b); }
    }
    newest.checked = !!v.newest;
  }
  function setDays(btn) {
    Array.prototype.forEach.call(document.querySelectorAll("#acaNew button"), function (o) { o.classList.remove("on"); });
    btn.classList.add("on");
    days = btn.dataset.days || "";
  }

  function apply() {
    var text = (q.value || "").trim().toLowerCase(), want = bc.value, n = 0;
    var cutoff = days ? Date.now() - parseInt(days, 10) * 86400000 : 0;
    rows.forEach(function (r) {
      var okQ = !text || (r.dataset.search || "").indexOf(text) >= 0;
      var okBc = !want || (r.dataset.bc || "") === want;
      var okNew = true;
      if (cutoff) {
        var at = Date.parse((r.dataset.added || "") + "Z");   // stored UTC, no zone on it
        okNew = !isNaN(at) && at >= cutoff;                   // unknown age never counts as recent
      }
      r.hidden = !(okQ && okBc && okNew);
      if (!r.hidden) n++;
    });
    if (newest.checked) {
      var sorted = rows.slice().sort(function (a, b2) {
        return (Date.parse((b2.dataset.added || "") + "Z") || 0) - (Date.parse((a.dataset.added || "") + "Z") || 0);
      });
      sorted.forEach(function (r) { body.appendChild(r); });
    } else {
      order.forEach(function (r) { body.appendChild(r); });
    }
    empty.hidden = n > 0;
    shown.textContent = (n === rows.length ? rows.length + " accounts" : n + " of " + rows.length + " accounts");
    save();
  }

  q.addEventListener("input", apply);
  bc.addEventListener("change", apply);
  newest.addEventListener("change", apply);
  Array.prototype.forEach.call(document.querySelectorAll("#acaNew button"), function (b) {
    b.addEventListener("click", function () { setDays(b); apply(); });
  });
  restore();
  apply();
})();
