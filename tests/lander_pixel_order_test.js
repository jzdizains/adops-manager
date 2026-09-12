// Lander pixel: identify() is queued BEFORE ttq.page() / ViewContent, and the page view
// still goes out when hashing is unavailable or slow. Runs the real <script> block that
// defines window.ttEvents from both pages (play + start) against a tiny DOM stub.
const fs = require("fs"), path = require("path"), vm = require("vm");
const LANDER = process.env.LANDER_DIR || path.join(__dirname, "..", "..", "lander");
if (!fs.existsSync(path.join(LANDER, "play", "index.html"))) {
  console.log("SKIP lander files not found at " + LANDER + " (set LANDER_DIR to the unzipped tikmobileplay folder)");
  process.exit(0);
}
let fails = 0;
function check(name, cond, extra) { console.log((cond ? "PASS " : "FAIL ") + name + (cond || !extra ? "" : "  [" + extra + "]")); if (!cond) fails++; }

function eventsBlock(html) {
  // the IIFE that starts at the "TikTok events" banner and ends at its own })();
  const start = html.indexOf("/* ---- TikTok events ---- */");
  if (start < 0) throw new Error("no TikTok events block");
  const end = html.indexOf("\n})();", start);
  return html.slice(start, end + "\n})();".length);
}

async function run(page, opts) {
  const html = fs.readFileSync(path.join(LANDER, page, "index.html"), "utf8");
  check(page + ": loader no longer calls ttq.page() itself", !/ttq\.load\('DAFJCDJC77UC8FLJR9QG'\);\s*ttq\.page\(\);/.test(html));
  const src = eventsBlock(html);
  const queue = [];
  const ttq = {};
  ["page", "track", "identify"].forEach(m => ttq[m] = (...a) => queue.push([m, ...a]));
  const listeners = {};
  const document = {
    cookie: "",
    addEventListener: (t, fn) => { (listeners[t] = listeners[t] || []).push(fn); },
    getElementById: () => ({ textContent: "", setAttribute() {}, addEventListener() {} }), querySelectorAll: () => [], querySelector: () => null,
  };
  const timers = [];
  const window = {
    ttq, document, localStorage: { _m: {}, getItem(k) { return this._m[k] || null; }, setItem(k, v) { this._m[k] = String(v); } },
    location: { search: "", protocol: "https:", href: "https://tikmobileplay.com/" + page + "/" },
    navigator: { userAgent: "Mozilla/5.0 (Linux; Android 13) Mobile" },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimeout: () => {},
    crypto: opts.crypto,
    TextEncoder: class { encode(s) { return Buffer.from(String(s)); } },
    URLSearchParams,
  };
  window.window = window;
  const ctx = vm.createContext(Object.assign(Object.create(null), window));
  vm.runInContext(src, ctx);
  await new Promise(r => setImmediate(r));
  await new Promise(r => setImmediate(r));
  return { queue, timers };
}

(async () => {
  const goodCrypto = { subtle: { digest: async (alg, buf) => require("crypto").createHash("sha256").update(Buffer.from(buf)).digest().buffer }, randomUUID: () => "11111111-2222-3333-4444-555555555555" };
  for (const page of ["play", "start"]) {
    // hashing works: identify → page → ViewContent, in that order
    let r = await run(page, { crypto: goodCrypto });
    const names = r.queue.map(q => q[0]);
    check(page + ": order is identify, page, track", names.slice(0, 3).join(",") === "identify,page,track", names.join(","));
    check(page + ": first track is ViewContent", r.queue[2] && r.queue[2][1] === "ViewContent");
    const ident = r.queue[0][1] || {};
    check(page + ": identify carries a hashed external_id (64 hex)", /^[0-9a-f]{64}$/.test(ident.external_id || ""), JSON.stringify(ident));
    check(page + ": identify sends nothing we don't have (no email/phone)", !("email" in ident) && !("phone_number" in ident));
    check(page + ": page view fired exactly once", names.filter(n => n === "page").length === 1);
    const fallback = r.timers.find(t => t.ms === 1500);
    check(page + ": a 1.5 s safety timer exists", !!fallback);
    if (fallback) { fallback.fn(); check(page + ": safety timer does not double-fire", r.queue.map(q => q[0]).filter(n => n === "page").length === 1); }

    // no crypto.subtle (old webview): no identify, but page + ViewContent still go out
    r = await run(page, { crypto: undefined });
    const n2 = r.queue.map(q => q[0]);
    check(page + ": without hashing, no identify is queued", !n2.includes("identify"), n2.join(","));
    check(page + ": …but page and ViewContent still fire", n2[0] === "page" && n2[1] === "track" && r.queue[1][1] === "ViewContent", n2.join(","));

    // hashing hangs forever: the timer rescues the page view
    const hang = { subtle: { digest: () => new Promise(() => {}) }, randomUUID: goodCrypto.randomUUID };
    r = await run(page, { crypto: hang });
    check(page + ": hashing hangs → nothing sent yet", r.queue.length === 0, r.queue.map(q => q[0]).join(","));
    r.timers.find(t => t.ms === 1500).fn();
    check(page + ": hashing hangs → timer sends page + ViewContent", r.queue.map(q => q[0]).join(",") === "page,track");
  }
  console.log(fails ? `\n${fails} FAILED` : "\nALL PASS");
  process.exit(fails ? 1 : 0);
})().catch(e => { console.error("ERROR", e); process.exit(1); });
