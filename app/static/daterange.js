/* daterange.js — tiny vanilla date-range picker.
   Renders preset chips (Today / Yesterday / 7d / 30d / MTD / Custom) into any
   element with [data-daterange]; keeps state in the query params
   ?range=&start=&end= and reloads on change. No framework. */
(function () {
  "use strict";

  var PRESETS = [
    ["today", "Today"], ["yesterday", "Yesterday"], ["7d", "7 days"],
    ["30d", "30 days"], ["mtd", "MTD"], ["custom", "Custom"]
  ];

  function qp() { return new URLSearchParams(window.location.search); }

  function go(params) {
    window.location.search = params.toString();
  }

  function build(el) {
    var params = qp();
    var current = params.get("range") || "today";
    var wrap = document.createElement("div");
    wrap.className = "dr-wrap";

    PRESETS.forEach(function (p) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "dr-chip" + (current === p[0] ? " active" : "");
      b.textContent = p[1];
      b.addEventListener("click", function () {
        if (p[0] === "custom") {
          custom.style.display = custom.style.display === "flex" ? "none" : "flex";
          return;
        }
        var np = qp();
        np.set("range", p[0]); np.delete("start"); np.delete("end");
        go(np);
      });
      wrap.appendChild(b);
    });

    var custom = document.createElement("div");
    custom.className = "dr-custom";
    custom.style.display = current === "custom" ? "flex" : "none";
    var s = document.createElement("input"); s.type = "date"; s.value = params.get("start") || "";
    var e = document.createElement("input"); e.type = "date"; e.value = params.get("end") || "";
    var apply = document.createElement("button");
    apply.type = "button"; apply.className = "dr-apply"; apply.textContent = "Apply";
    apply.addEventListener("click", function () {
      if (!s.value || !e.value) return;
      var np = qp();
      np.set("range", "custom"); np.set("start", s.value); np.set("end", e.value);
      go(np);
    });
    custom.appendChild(s); custom.appendChild(e); custom.appendChild(apply);
    wrap.appendChild(custom);
    el.appendChild(wrap);
  }

  var css = document.createElement("style");
  css.textContent =
    ".dr-wrap{display:flex;align-items:center;gap:6px;flex-wrap:wrap}" +
    ".dr-chip{font:inherit;font-size:12px;font-weight:700;padding:5px 11px;border-radius:999px;" +
    "border:1px solid var(--border,#e5e7eb);background:var(--panel,#fff);color:var(--text-soft,#4b5563);cursor:pointer}" +
    ".dr-chip:hover{background:var(--panel-hover,#f3f4f6)}" +
    ".dr-chip.active{background:var(--accent-soft,rgba(124,58,237,.1));color:var(--accent,#7c3aed);border-color:transparent}" +
    ".dr-custom{display:flex;gap:6px;align-items:center}" +
    ".dr-custom input{font:inherit;font-size:12px;padding:4px 8px;border:1px solid var(--border-strong,#d1d5db);" +
    "border-radius:8px;width:auto}" +
    ".dr-apply{font:inherit;font-size:12px;font-weight:700;padding:5px 11px;border-radius:8px;border:none;" +
    "background:var(--accent,#7c3aed);color:#fff;cursor:pointer}";
  document.head.appendChild(css);

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-daterange]").forEach(build);
  });
})();
