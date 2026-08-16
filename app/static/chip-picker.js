/* chip-picker.js — vanilla multi-select rendered as clickable chips.
   Usage: <div data-chip-picker data-name="age_groups"
               data-options='[["AGE_18_24","18–24"],["AGE_25_34","25–34"]]'
               data-selected='["AGE_18_24"]'></div>
   Selected values are kept in hidden inputs (one per value) so plain HTML
   form posts work. No framework. */
(function () {
  "use strict";

  function build(el) {
    var name = el.dataset.name || "chips";
    var options, selected;
    try { options = JSON.parse(el.dataset.options || "[]"); } catch (e) { options = []; }
    try { selected = new Set(JSON.parse(el.dataset.selected || "[]")); } catch (e) { selected = new Set(); }

    var wrap = document.createElement("div");
    wrap.className = "cp-wrap";

    function syncInputs() {
      wrap.querySelectorAll("input[type=hidden]").forEach(function (i) { i.remove(); });
      selected.forEach(function (v) {
        var input = document.createElement("input");
        input.type = "hidden"; input.name = name; input.value = v;
        wrap.appendChild(input);
      });
    }

    options.forEach(function (opt) {
      var value = opt[0], label = opt[1] || opt[0];
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "cp-chip" + (selected.has(value) ? " active" : "");
      chip.textContent = label;
      chip.addEventListener("click", function () {
        if (selected.has(value)) { selected.delete(value); chip.classList.remove("active"); }
        else { selected.add(value); chip.classList.add("active"); }
        syncInputs();
      });
      wrap.appendChild(chip);
    });

    syncInputs();
    el.appendChild(wrap);
  }

  var css = document.createElement("style");
  css.textContent =
    ".cp-wrap{display:flex;gap:6px;flex-wrap:wrap}" +
    ".cp-chip{font:inherit;font-size:12px;font-weight:600;padding:5px 12px;border-radius:999px;" +
    "border:1px solid var(--border-strong,#d1d5db);background:var(--panel,#fff);" +
    "color:var(--text-soft,#4b5563);cursor:pointer}" +
    ".cp-chip:hover{background:var(--panel-hover,#f3f4f6)}" +
    ".cp-chip.active{background:var(--accent,#7c3aed);border-color:var(--accent,#7c3aed);color:#fff}";
  document.head.appendChild(css);

  document.addEventListener("DOMContentLoaded", function () {
    document.querySelectorAll("[data-chip-picker]").forEach(build);
  });
})();
