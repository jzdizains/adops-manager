/* The chip picker's "All" option.

   Connection type had no "All": to lift the restriction you had to untick every chip and
   end up with a picker where nothing was lit, which looks unset rather than deliberate.
   "All" means NO RESTRICTION — the field is left out of the payload entirely, which is
   already how the launcher treats an empty selection. So the chip must post nothing, and
   must light up exactly when nothing else is selected.

   Runs the shipped app/static/chip-picker.js against a DOM stub. */
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? "PASS " : "FAIL ") + name + (!cond && extra ? "  [" + extra + "]" : ""));
  if (!cond) fails.push(name);
}

function el(tag) {
  const e = {
    tagName: tag, dataset: {}, children: [], _cls: new Set(), _on: {},
    type: "", name: "", value: "", textContent: "", title: "", className: "",
    classList: {
      add: (c) => e._cls.add(c), remove: (c) => e._cls.delete(c),
      contains: (c) => e._cls.has(c),
      toggle: (c, on) => (on ? e._cls.add(c) : e._cls.delete(c)),
    },
    addEventListener: (ev, fn) => { (e._on[ev] = e._on[ev] || []).push(fn); },
    click: () => (e._on.click || []).forEach((f) => f.call(e, {})),
    appendChild: (c) => { e.children.push(c); return c; },
    remove() { const p = e._parent; if (p) p.children.splice(p.children.indexOf(e), 1); },
    querySelectorAll: (sel) => {
      const out = [];
      (function walk(n) {
        n.children.forEach((c) => {
          if (sel === "input[type=hidden]" && c.tagName === "input" && c.type === "hidden") out.push(c);
          walk(c);
        });
      })(e);
      out.forEach((o) => { o._parent = e; });
      return out;
    },
  };
  Object.defineProperty(e, "className", {
    get: () => Array.from(e._cls).join(" "),
    set: (v) => { e._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
  });
  return e;
}

function mount(attrs) {
  const host = el("div");
  Object.assign(host.dataset, attrs);
  let ready = null;
  const doc = {
    createElement: el,
    head: el("head"),
    addEventListener: (ev, fn) => { if (ev === "DOMContentLoaded") ready = fn; },
    querySelectorAll: () => [host],
  };
  const sandbox = { document: doc, JSON, Set, Array, Object, String, console };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "app", "static", "chip-picker.js"), "utf8"),
                  sandbox, { filename: "chip-picker.js" });
  ready();
  const wrap = host.children[0];
  return {
    wrap,
    chips: wrap.children.filter((c) => c.tagName === "button"),
    posted: () => wrap.children.filter((c) => c.tagName === "input" && c.type === "hidden")
      .map((i) => i.name + "=" + i.value),
  };
}

const OPTS = JSON.stringify([["WIFI", "WIFI"], ["2G", "2G"], ["3G", "3G"], ["4G", "4G"], ["5G", "5G"]]);

console.log("\n-- with nothing selected, All is the one lit --");
let p = mount({ all: "All", name: "network_types", options: OPTS, selected: "[]" });
check("All is first", p.chips[0].textContent === "All", p.chips[0].textContent);
check("All is active", p.chips[0].classList.contains("active"));
check("no specific chip is active", p.chips.slice(1).every((c) => !c.classList.contains("active")));
check("and nothing is posted — no restriction", p.posted().length === 0, JSON.stringify(p.posted()));

console.log("\n-- picking one turns All off --");
p.chips[1].click();                                   // WIFI
check("WIFI is active", p.chips[1].classList.contains("active"));
check("All went out", !p.chips[0].classList.contains("active"));
check("WIFI is posted", p.posted().join(",") === "network_types=WIFI", JSON.stringify(p.posted()));

console.log("\n-- unticking the last one brings All back by itself --");
p.chips[1].click();
check("All is lit again", p.chips[0].classList.contains("active"));
check("nothing posted", p.posted().length === 0, JSON.stringify(p.posted()));

console.log("\n-- All clears whatever was picked --");
p.chips[1].click(); p.chips[3].click();               // WIFI + 4G
check("two posted", p.posted().length === 2, JSON.stringify(p.posted()));
p.chips[0].click();                                   // All
check("selection cleared", p.posted().length === 0, JSON.stringify(p.posted()));
check("every specific chip went out", p.chips.slice(1).every((c) => !c.classList.contains("active")));
check("All is lit", p.chips[0].classList.contains("active"));

console.log("\n-- a saved preset with chips ticked opens with All off --");
p = mount({ all: "All", name: "network_types", options: OPTS, selected: '["WIFI","5G"]' });
check("All is not lit", !p.chips[0].classList.contains("active"));
check("the saved chips are lit",
      p.chips[1].classList.contains("active") && p.chips[5].classList.contains("active"));
check("and they post", p.posted().sort().join(",") === "network_types=5G,network_types=WIFI",
      JSON.stringify(p.posted()));

console.log("\n-- pickers without data-all are unchanged --");
p = mount({ name: "age_groups", options: JSON.stringify([["A", "18-24"], ["B", "25-34"]]), selected: "[]" });
check("no All chip is added", p.chips.length === 2, String(p.chips.length));
check("nothing selected posts nothing", p.posted().length === 0);
p.chips[0].click();
check("it still works", p.posted().join(",") === "age_groups=A", JSON.stringify(p.posted()));

console.log();
console.log(fails.length ? "FAILED: " + fails.join(", ") : "all good");
process.exit(fails.length ? 1 : 0);
