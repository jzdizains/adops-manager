// Lander funnel beacon: runs the real "funnel beacon" <script> from /start and /play
// against a DOM stub and checks what would reach POST /t/lp.
const fs = require("fs"), path = require("path"), vm = require("vm");
const LANDER = process.env.LANDER_DIR || path.join(__dirname, "..", "..", "lander");
if (!fs.existsSync(path.join(LANDER, "play", "index.html"))) {
  console.log("SKIP lander files not found at " + LANDER + " (set LANDER_DIR to the unzipped tikmobileplay folder)");
  process.exit(0);
}
let fails = 0;
function check(name, cond, extra) { console.log((cond ? "PASS " : "FAIL ") + name + (cond || !extra ? "" : "  [" + extra + "]")); if (!cond) fails++; }

function beaconBlock(html) {
  const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
  const b = blocks.find(s => s.includes("funnel beacon (dashboard)"));
  if (!b) throw new Error("no beacon block");
  return b;
}

function run(page, opts) {
  const html = fs.readFileSync(path.join(LANDER, page, "index.html"), "utf8");
  check(page + ": beacon block is the LAST script before </body>", /funnel beacon \(dashboard\)[\s\S]*<\/script>\s*<\/body>/.test(html) && html.split("funnel beacon (dashboard)").length === 2);
  const src = beaconBlock(html);
  const sent = [], timers = [], listeners = {}, winListeners = {};
  const target = { closest: sel => (opts.clickMatches && opts.clickMatches === sel) ? { tag: "a" } : null };
  const document = {
    readyState: opts.readyState || "complete", cookie: opts.cookie || "",
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
  };
  const window = {
    document, location: { search: opts.search || "" },
    localStorage: { getItem: k => (opts.ls || {})[k] || null },
    navigator: { sendBeacon: (url, blob) => { sent.push({ url, blob }); return true; } },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); },
    URLSearchParams, Blob: class { constructor(parts, o) { this.text = parts.join(""); this.type = o && o.type; } },
    JSON,
  };
  window.window = window;
  const ctx = vm.createContext(Object.assign(Object.create(null), window));
  vm.runInContext(src, ctx);
  return { sent, timers, listeners, winListeners, target };
}
const parse = s => JSON.parse(s.blob.text);

for (const [page, sel, step] of [["play", "a.cta", "cta"], ["start", "#ctaBtn", "continue"]]) {
  // normal visit: complete page, source + ttclid on the URL, visitor id in localStorage
  let r = run(page, { search: "?ttclid=E_C_P_abc&source=Camp_120000_a1111&cpid=x&cid=777", ls: { tmp_vid: "vid-1" }, clickMatches: sel });
  r.timers.forEach(t => t.fn());                       // fire the 0 ms and 3 s timers
  check(page + ": exactly one view beacon", r.sent.filter(s => parse(s).step === "view").length === 1, r.sent.length);
  let v = parse(r.sent[0]);
  check(page + ": view carries page/source/vid/ttclid/cid", v.page === page && v.source === "Camp_120000_a1111" && v.vid === "vid-1" && v.ttclid === 1 && v.cid === "777", JSON.stringify(v));
  check(page + ": posts to the dashboard beacon endpoint", r.sent[0].url === "https://adops-manager.onrender.com/t/lp");
  check(page + ": body is text/plain (no CORS preflight)", r.sent[0].blob.type === "text/plain");
  // the click
  (r.listeners.click || []).forEach(fn => fn({ target: r.target }));
  check(page + ": clicking the CTA sends '" + step + "'", r.sent.some(s => parse(s).step === step));
  // a click somewhere else sends nothing
  const before = r.sent.length;
  (r.listeners.click || []).forEach(fn => fn({ target: { closest: () => null } }));
  check(page + ": a click elsewhere sends nothing", r.sent.length === before);
  // click listener is in capture phase (runs even if the CTA handler navigates)
  check(page + ": click listener registered", (r.listeners.click || []).length === 1);

  // page still loading: view waits for load, the 3 s fallback still counts it once
  r = run(page, { readyState: "loading", search: "", cookie: "tmp_vid=vid-2" });
  check(page + ": nothing sent before load", r.sent.length === 0);
  const fb = r.timers.find(t => t.ms === 3000); fb.fn();
  (r.winListeners.load || []).forEach(fn => fn()); r.timers.filter(t => t.ms === 0).forEach(t => t.fn());
  check(page + ": view sent once even when load and the fallback both fire", r.sent.filter(s => parse(s).step === "view").length === 1);
  v = parse(r.sent[0]);
  check(page + ": visitor id falls back to the cookie; no ttclid → 0; no source → ''", v.vid === "vid-2" && v.ttclid === 0 && v.source === "" && v.cid === "", JSON.stringify(v));
}
console.log(fails ? `\n${fails} FAILED` : "\nALL PASS");
process.exit(fails ? 1 : 0);
