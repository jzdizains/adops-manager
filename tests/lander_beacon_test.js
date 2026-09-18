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
  const intervals = [];
  const document = {
    readyState: opts.readyState || "complete", cookie: opts.cookie || "", visibilityState: opts.visibility || "visible",
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
  };
  const window = {
    document, location: { search: opts.search || "" },
    localStorage: { getItem: k => (opts.ls || {})[k] || null },
    navigator: { sendBeacon: (url, blob) => { sent.push({ url, blob }); return true; } },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    setInterval: (fn, ms) => { intervals.push({ fn, ms, on: true }); return intervals.length; },
    clearInterval: id => { if (intervals[id - 1]) intervals[id - 1].on = false; },
    addEventListener: (t, fn) => { (winListeners[t] = winListeners[t] || []).push(fn); },
    URLSearchParams, Blob: class { constructor(parts, o) { this.text = parts.join(""); this.type = o && o.type; } },
    JSON,
  };
  window.window = window;
  const ctx = vm.createContext(Object.assign(Object.create(null), window));
  vm.runInContext(src, ctx);
  const tickAll = n => { for (let i = 0; i < n; i++) intervals.filter(x => x.on).forEach(x => x.fn()); };
  return { sent, timers, listeners, winListeners, target, intervals, tickAll, document };
}
const parse = s => JSON.parse(s.blob.text);

for (const [page, sel, step] of [["play", "a.cta", "cta"], ["start", "#ctaBtn", "continue"]]) {
  // normal visit: complete page, source + ttclid on the URL, visitor id in localStorage
  let r = run(page, { search: "?ttclid=E_C_P_abc&source=Camp_120000_a1111&cpid=x&cid=777", ls: { tmp_vid: "vid-1" }, clickMatches: sel });
  r.timers.forEach(t => t.fn());                       // fire the 0 ms and 3 s timers
  check(page + ": exactly one view beacon", r.sent.filter(s => parse(s).step === "view").length === 1, r.sent.length);
  let v = parse(r.sent[0]);
  check(page + ": view carries page/source/vid/ttclid (the value, v7)/cid", v.page === page && v.source === "Camp_120000_a1111" && v.vid === "vid-1" && v.ttclid === "E_C_P_abc" && v.cid === "777", JSON.stringify(v));
  check(page + ": via is sent (empty when not a hand-off)", "via" in v && v.via === "", JSON.stringify(v));
  check(page + ": posts to the dashboard beacon endpoint", r.sent[0].url === "https://adops-manager.onrender.com/t/lp");
  check(page + ": body is text/plain (no CORS preflight)", r.sent[0].blob.type === "text/plain");
  // engaged: visible for 4 s (8 ticks of 500 ms) → one 'engaged'; more ticks never repeat it
  check(page + ": no 'engaged' before 4 s", !r.sent.some(s => parse(s).step === "engaged"));
  r.tickAll(7); check(page + ": still none at 3.5 s", !r.sent.some(s => parse(s).step === "engaged"));
  r.tickAll(1); check(page + ": 'engaged' at 4 s of visibility", r.sent.filter(s => parse(s).step === "engaged").length === 1);
  r.tickAll(20); check(page + ": 'engaged' sent once", r.sent.filter(s => parse(s).step === "engaged").length === 1);
  check(page + ": engaged carries the same source/vid", (() => { const e = parse(r.sent.find(s => parse(s).step === "engaged")); return e.source === "Camp_120000_a1111" && e.vid === "vid-1"; })());
  // the click
  (r.listeners.click || []).forEach(fn => fn({ target: r.target }));
  check(page + ": clicking the CTA sends '" + step + "'", r.sent.some(s => parse(s).step === step));
  check(page + ": order of steps is view, engaged, " + step, r.sent.map(s => parse(s).step).join(",") === "view,engaged," + step, r.sent.map(s => parse(s).step).join(","));

  // a touch counts as engaged immediately
  let t = run(page, { search: "?source=Camp_120000_a1111", ls: { tmp_vid: "vid-t" } });
  t.timers.forEach(x => x.fn());
  (t.winListeners.touchstart || []).forEach(fn => fn({}));
  check(page + ": a touch sends 'engaged' at once", t.sent.map(s => parse(s).step).join(",") === "view,engaged", t.sent.map(s => parse(s).step).join(","));
  t.tickAll(10); check(page + ": …and the timer does not send it again", t.sent.filter(s => parse(s).step === "engaged").length === 1);
  check(page + ": touch/scroll/pointer/key listeners are passive one-shots", ["touchstart", "scroll", "pointerdown", "keydown"].every(k => (t.winListeners[k] || []).length === 1));

  // a hidden (prerendered) page: view fires, engaged never does while hidden
  let h = run(page, { search: "?source=Camp_120000_a1111", ls: { tmp_vid: "vid-h" }, visibility: "hidden" });
  h.timers.forEach(x => x.fn()); h.tickAll(20);
  check(page + ": hidden page → view only, no 'engaged'", h.sent.map(s => parse(s).step).join(",") === "view", h.sent.map(s => parse(s).step).join(","));
  h.document.visibilityState = "visible"; (h.listeners.visibilitychange || []).forEach(fn => fn()); h.tickAll(8);
  check(page + ": once shown, 4 visible seconds → 'engaged'", h.sent.map(s => parse(s).step).join(",") === "view,engaged", h.sent.map(s => parse(s).step).join(","));

  // a click on the CTA without any prior engaged proves a person too — engaged goes first
  let c = run(page, { search: "", ls: { tmp_vid: "vid-c" }, clickMatches: sel });
  c.timers.forEach(x => x.fn());
  (c.listeners.click || []).forEach(fn => fn({ target: c.target }));
  check(page + ": CTA click → 'engaged' then '" + step + "'", c.sent.map(s => parse(s).step).join(",") === "view,engaged," + step, c.sent.map(s => parse(s).step).join(","));
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
// ---- the hand-off ---------------------------------------------------------------------
{
  const play = fs.readFileSync(path.join(LANDER, "play", "index.html"), "utf8");
  const start = fs.readFileSync(path.join(LANDER, "start", "index.html"), "utf8");
  // /start: destination() carries via=continue and the visitor id
  const d0 = start.indexOf("  function myVid()"), d1 = start.indexOf("  btn.setAttribute('href', destination());");
  const dctx = { window: { localStorage: { getItem: k => k === "tmp_vid" ? "3f2c9a1e-7b4d-4c1e-9a8f-1d2e3f4a5b6c" : null }, location: { search: "?ttclid=E_C_P_x&source=Camp&cpid=1" } }, document: { cookie: "" }, URLSearchParams };
  dctx.window.window = dctx.window;
  vm.createContext(dctx); vm.runInContext(start.slice(d0, d1) + "\nthis.destination = destination;", dctx);
  const dest = new URL(dctx.destination());
  check("start: /play URL keeps the original params", dest.pathname === "/play/" && dest.searchParams.get("ttclid") === "E_C_P_x" && dest.searchParams.get("source") === "Camp" && dest.searchParams.get("cpid") === "1");
  check("start: …and adds via=continue + svid", dest.searchParams.get("via") === "continue" && dest.searchParams.get("svid") === "3f2c9a1e-7b4d-4c1e-9a8f-1d2e3f4a5b6c", dest.search);
  check("start: href is refreshed on the click (id minted after load)", /btn\.setAttribute\('href', dest\);/.test(start));
  // /play: the adopt script runs BEFORE the pixel loader and stores svid
  const ai = play.indexOf("Hand-off from /start"), pi = play.indexOf("TikTok Pixel Code Start");
  check("play: adopt script sits before the pixel code", ai > 0 && ai < pi);
  const ablock = [...play.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]).find(s => s.includes("Hand-off from /start"));
  function adopt(search) {
    const st = { ls: {}, cookie: "" };
    const c = { location: { search, protocol: "https:" }, window: { localStorage: { setItem: (k, v) => { st.ls[k] = v; } } }, document: { get cookie() { return st.cookie; }, set cookie(v) { st.cookie = v; } }, URLSearchParams };
    vm.createContext(c); vm.runInContext(ablock, c); return st;
  }
  let st = adopt("?via=continue&svid=3f2c9a1e-7b4d-4c1e-9a8f-1d2e3f4a5b6c&source=Camp");
  check("play: adopts the visitor id into localStorage + cookie", st.ls.tmp_vid === "3f2c9a1e-7b4d-4c1e-9a8f-1d2e3f4a5b6c" && /tmp_vid=3f2c9a1e-7b4d-4c1e-9a8f-1d2e3f4a5b6c/.test(st.cookie) && /Secure/.test(st.cookie), JSON.stringify(st));
  st = adopt("?svid=%3Cscript%3E");
  check("play: an odd svid is ignored", !st.ls.tmp_vid && st.cookie === "");
  st = adopt("?svid=short");
  check("play: a too-short svid is ignored", !st.ls.tmp_vid && st.cookie === "");
  st = adopt("?source=Camp");
  check("play: no svid → nothing written", !st.ls.tmp_vid && st.cookie === "");
  // /play beacon reports via=continue on a hand-off visit
  let r = run("play", { search: "?via=continue&svid=3f2c9a1e-7b4d-4c1e-9a8f-1d2e3f4a5b6c&source=Camp_120000_a1111", ls: { tmp_vid: "3f2c9a1e-7b4d-4c1e-9a8f-1d2e3f4a5b6c" } });
  r.timers.forEach(t => t.fn());
  check("play: view beacon says via=continue with the shared visitor id", parse(r.sent[0]).via === "continue" && parse(r.sent[0]).vid === "3f2c9a1e-7b4d-4c1e-9a8f-1d2e3f4a5b6c", r.sent[0].blob.text);
}
console.log(fails ? `\n${fails} FAILED` : "\nALL PASS");
process.exit(fails ? 1 : 0);
