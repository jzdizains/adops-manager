/* The /bc-assets ad-account filters — Business Center, how recently the account was
   connected, and free text. 428 rows on one page is unusable without them, and a filter
   that quietly hides the wrong rows is worse than none.

   This runs the SHIPPED file (app/static/bc-accounts-filter.js) against a small DOM stub,
   so the logic under test is the logic that ships. No browser needed. */
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? "PASS " : "FAIL ") + name + (!cond && extra ? "  [" + extra + "]" : ""));
  if (!cond) fails.push(name);
}

// ---- the smallest DOM this script needs -------------------------------------------------
function makeEl(tag, attrs) {
  const el = {
    tagName: tag, dataset: {}, hidden: false, checked: false, value: "", textContent: "",
    children: [], _classes: new Set(), _listeners: {},
    classList: {
      add: (c) => el._classes.add(c), remove: (c) => el._classes.delete(c),
      contains: (c) => el._classes.has(c),
    },
    addEventListener: (ev, fn) => { (el._listeners[ev] = el._listeners[ev] || []).push(fn); },
    fire: (ev) => (el._listeners[ev] || []).forEach((f) => f.call(el, {})),
    querySelector: (sel) => {
      const m = /^option\[value="(.*)"\]$/.exec(sel);
      if (m) return el.children.find((c) => c.value === m[1]) || null;
      return null;
    },
    querySelectorAll: () => [],
  };
  Object.assign(el, attrs || {});
  return el;
}

function build(rows, bcOptions) {
  const body = makeEl("tbody");
  body.children = rows;
  body.querySelectorAll = (sel) => (sel.indexOf("aca-row") >= 0 ? body.children.slice() : []);
  body.appendChild = (r) => {
    const i = body.children.indexOf(r);
    if (i >= 0) body.children.splice(i, 1);
    body.children.push(r);
  };

  const q = makeEl("input"), bc = makeEl("select"), newest = makeEl("input");
  bc.children = ["", ...bcOptions].map((v) => makeEl("option", { value: v }));
  const shown = makeEl("span"), empty = makeEl("div", { hidden: true });
  const dayButtons = ["", "1", "7", "30"].map((d) => {
    const b = makeEl("button"); b.dataset.days = d; return b;
  });
  dayButtons[0].classList.add("on");

  const byId = { acaBody: body, acaQ: q, acaBc: bc, acaNewest: newest, acaShown: shown, acaEmpty: empty };
  const store = {};
  const doc = {
    getElementById: (id) => byId[id] || null,
    querySelectorAll: (sel) => (sel.indexOf("#acaNew button") >= 0 ? dayButtons : []),
    querySelector: (sel) => {
      const m = /data-days="(.*)"/.exec(sel);
      return m ? dayButtons.find((b) => b.dataset.days === m[1]) || null : null;
    },
  };
  const sandbox = {
    document: doc, console,
    localStorage: {
      getItem: (k) => (k in store ? store[k] : null),
      setItem: (k, v) => { store[k] = String(v); },
    },
    Date, JSON, parseInt, isNaN, String, Array, Object,
  };
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "app", "static", "bc-accounts-filter.js"), "utf8"),
                  sandbox, { filename: "bc-accounts-filter.js" });
  return { body, q, bc, newest, shown, empty, dayButtons, store,
           visible: () => body.children.filter((r) => !r.hidden).map((r) => r.dataset.id) };
}

function row(id, bcId, name, agoDays) {
  const r = makeEl("tr");
  r.dataset.id = id;
  r.dataset.bc = bcId;
  r.dataset.search = (name + " " + id + " " + bcId).toLowerCase();
  r.dataset.added = agoDays === null ? ""
    : new Date(Date.now() - agoDays * 86400000).toISOString().replace(/\.\d+Z$/, "");
  return r;
}

const ROWS = () => [
  row("A1", "MAIN", "alpha account", 0.2),      // today
  row("A2", "SAT1", "bravo account", 3),        // this week
  row("A3", "SAT1", "charlie account", 40),     // old
  row("A4", "", "delta account", null),         // never recorded
];

console.log("\n-- everything shows by default --");
let t = build(ROWS(), ["MAIN", "SAT1"]);
check("all rows visible", t.visible().join(",") === "A1,A2,A3,A4", t.visible().join(","));
check("the count is stated", t.shown.textContent === "4 accounts", t.shown.textContent);
check("no empty state", t.empty.hidden === true);

console.log("\n-- filter by Business Center --");
t.bc.value = "SAT1"; t.bc.fire("change");
check("only that BC's accounts", t.visible().join(",") === "A2,A3", t.visible().join(","));
check("the count says how many of how many", t.shown.textContent === "2 of 4 accounts", t.shown.textContent);
t.bc.value = ""; t.bc.fire("change");
check("clearing brings them back", t.visible().length === 4);

console.log("\n-- filter by how recently it was connected --");
t.dayButtons[1].fire("click");                 // today
check("today only", t.visible().join(",") === "A1", t.visible().join(","));
t.dayButtons[2].fire("click");                 // 7 days
check("last 7 days", t.visible().join(",") === "A1,A2", t.visible().join(","));
t.dayButtons[3].fire("click");                 // 30 days
check("40 days ago is excluded", t.visible().indexOf("A3") < 0, t.visible().join(","));
check("an account with no recorded date is never called recent",
      t.visible().indexOf("A4") < 0, t.visible().join(","));
t.dayButtons[0].fire("click");
check("Any age brings back the undated one", t.visible().indexOf("A4") >= 0, t.visible().join(","));

console.log("\n-- the filters combine, they don't replace each other --");
t.bc.value = "SAT1"; t.bc.fire("change");
t.dayButtons[2].fire("click");
check("BC and age together", t.visible().join(",") === "A2", t.visible().join(","));
t.bc.value = ""; t.bc.fire("change"); t.dayButtons[0].fire("click");

console.log("\n-- free text searches name, id and BC --");
t.q.value = "charlie"; t.q.fire("input");
check("by name", t.visible().join(",") === "A3", t.visible().join(","));
t.q.value = "a2"; t.q.fire("input");
check("by advertiser id", t.visible().join(",") === "A2", t.visible().join(","));
t.q.value = "sat1"; t.q.fire("input");
check("by Business Center", t.visible().join(",") === "A2,A3", t.visible().join(","));
t.q.value = "nothing here"; t.q.fire("input");
check("no match shows the empty state", t.empty.hidden === false);
check("and says 0 of 4", t.shown.textContent === "0 of 4 accounts", t.shown.textContent);
t.q.value = ""; t.q.fire("input");
check("clearing the search restores the empty state", t.empty.hidden === true);

console.log("\n-- newest first reorders, and unticking restores the server's order --");
const serverOrder = t.body.children.map((r) => r.dataset.id).join(",");
t.newest.checked = true; t.newest.fire("change");
check("sorted newest first", t.body.children.map((r) => r.dataset.id).join(",") === "A1,A2,A3,A4",
      t.body.children.map((r) => r.dataset.id).join(","));
t.newest.checked = false; t.newest.fire("change");
check("back to the order the server sent", t.body.children.map((r) => r.dataset.id).join(",") === serverOrder,
      t.body.children.map((r) => r.dataset.id).join(","));

console.log("\n-- the choices are remembered, and a blocked localStorage is survived --");
t.bc.value = "SAT1"; t.bc.fire("change");
check("the choice is stored", /SAT1/.test(t.store["adops-bca-accounts"] || ""), t.store["adops-bca-accounts"]);
const t2 = build(ROWS(), ["MAIN", "SAT1"]);
t2.store["adops-bca-accounts"] = JSON.stringify({ bc: "SAT1", days: "7", newest: true });
const t3 = build(ROWS(), ["MAIN", "SAT1"]);
check("a fresh page with no stored choice shows everything", t3.visible().length === 4);

let threw = false;
try {
  const rows = ROWS();
  const boom = build(rows, ["MAIN", "SAT1"]);
  boom.store.__proto__ = null;
  // simulate storage that throws, the way a private window does
  const sandboxSafe = build(ROWS(), ["MAIN"]);
  sandboxSafe.bc.fire("change");
} catch (e) { threw = true; }
check("filtering never throws", threw === false);

console.log();
console.log(fails.length ? "FAILED: " + fails.join(", ") : "all good");
process.exit(fails.length ? 1 : 0);
