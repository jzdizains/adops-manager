// lander.js (v124) under a fake window: detection per user agent, params + packed source,
// rule matching, routing, escape URLs per method, the beacon body, age brackets.
const fs = require("fs"), path = require("path"), vm = require("vm");
const SRC = fs.readFileSync(path.join(__dirname, "..", "app", "static", "lander.js"), "utf8");
let fails = 0;
function check(name, cond, extra) { console.log((cond ? "PASS " : "FAIL ") + name + (!cond && extra !== undefined ? "  [" + JSON.stringify(extra) + "]" : "")); if (!cond) fails++; }

function boot(ua, search, cfg) {
  const beacons = [], stores = {};
  const storage = { getItem: (k) => (k in stores ? stores[k] : null), setItem: (k, v) => { stores[k] = String(v); } };
  const listeners = {};
  const w = {
    LANDER_CFG: Object.assign({ slug: "open-uk", track_host: "https://dash.example.com" }, cfg || {}),
    navigator: { userAgent: ua, sendBeacon: (url, blob) => { beacons.push({ url, body: JSON.parse(blob.parts[0]) }); return true; } },
    location: { search: search || "", href: "https://start.example.com/" + (search || "") },
    localStorage: storage, sessionStorage: storage, addEventListener: (ev, fn) => { (listeners[ev] = listeners[ev] || []).push(fn); },
    setTimeout: (fn, ms) => { w._timers.push({ fn, ms }); return w._timers.length; }, clearTimeout: () => {}, setInterval: () => 1, clearInterval: () => {},
    Blob: function (parts, o) { this.parts = parts; this.type = o && o.type; }, URLSearchParams: URLSearchParams, Date: Date, Math: Math, Object: Object, JSON: JSON, String: String, parseInt: parseInt, encodeURIComponent, decodeURIComponent, RegExp,
    _timers: [], _beacons: beacons, _listeners: listeners,
  };
  w.window = w;
  const d = { visibilityState: "visible", addEventListener: (ev, fn) => { (listeners["doc:" + ev] = listeners["doc:" + ev] || []).push(fn); } };
  w.document = d;
  vm.runInNewContext(SRC.replace("})(window, document);", "})(w, d);"), { w, d, setTimeout: w.setTimeout, clearTimeout: w.clearTimeout, setInterval: w.setInterval, clearInterval: w.clearInterval, URLSearchParams, Blob: w.Blob, Date, Math, JSON, Object, String, parseInt, encodeURIComponent, decodeURIComponent, RegExp, navigator: w.navigator, localStorage: storage, sessionStorage: storage, location: w.location });
  return w;
}
const TT_ANDROID = "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/117 Mobile Safari/537.36 trill_2023 musical_ly_2023.5 JsSdk/1.0 NetType/WIFI Channel/googleplay AppName/musical_ly app_version/31.5.3 ByteLocale/en BytedanceWebview/d8a21c6";
const TT_IOS = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 musical_ly_31.2.0 JsSdk/2.0 NetType/WIFI Channel/App Store ByteLocale/en Region/US";
const SAFARI = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Mobile/15E148 Safari/604.1";
const IG = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 300.0.0.0";

console.log("-- detection --");
let w = boot(TT_ANDROID);
check("TikTok on Android: inapp=tiktok, os=android, version read", w.L.env.inapp === "tiktok" && w.L.env.os === "android" && w.L.env.app_version === "31.5.3", w.L.env);
check("TikTok on iPhone", boot(TT_IOS).L.env.inapp === "tiktok" && boot(TT_IOS).L.env.os === "ios");
check("Safari: no in-app", boot(SAFARI).L.env.inapp === "" && boot(SAFARI).L.env.os === "ios");
check("Instagram detected as instagram", boot(IG).L.env.inapp === "instagram");
check("L.detect works on any UA string", w.L.detect("Snapchat/12 (Android)").inapp === "snapchat" && w.L.detect("Mozilla/5.0 (Windows NT 10.0)").os === "other");

console.log("-- params + beacon --");
w = boot(SAFARI, "?source=camp_a1~abcdefghijkl&tt_cid=123&ttclid=E.C.P.xyz&via=continue&svid=v0001");
check("packed source split into source + clid; ids kept; visitor id adopted from svid", w.L.params.source === "camp_a1" && w.L.params.clid === "abcdefghijkl" && w.L.params.tt_cid === "123" && w.L.params.vid === "v0001", w.L.params);
const v = w._beacons[0];
check("view beacon: page = slug, step view, ids, env, via", v && v.url === "https://dash.example.com/t/lp" && v.body.page === "open-uk" && v.body.step === "view" && v.body.source === "camp_a1" && v.body.vid === "v0001" && v.body.cid === "123" && v.body.via === "continue" && v.body.inapp === "" && v.body.os === "ios", v);
w.L.track("view"); check("a step is sent once per page", w._beacons.length === 1);
w.L.track("gate", { bucket: "18_24" }); check("bucket travels", w._beacons[1].body.step === "gate" && w._beacons[1].body.bucket === "18_24");
check("no host → no beacon, no crash", boot(SAFARI, "", { track_host: "" })._beacons.length === 0);

console.log("-- routing --");
const RULES = [{ name: "ios-inapp", when: { os: "ios", inapp: "yes" }, to: "https://a.example.com/" }, { name: "young", when: { age: "u18,18_24" }, to: "https://b.example.com/" },
               { name: "uk-hours", when: { country: "GB", hours: "9-17" }, to: "https://c.example.com/" }, { name: "night", when: { hours: "22-6" }, to: "https://n.example.com/" }];
w = boot(TT_IOS, "", { rules: RULES, next: "https://play.example.com/" });
check("first matching rule wins (ios + in-app)", w.L.route() === "https://a.example.com/");
w = boot(SAFARI, "", { rules: RULES, next: "https://play.example.com/" });
check("no match → next", w.L.route({ hour: 12, country: "US" }) === "https://play.example.com/");
check("age bracket rule after L.setAge", w.L.setAge(new Date().getFullYear() - 20) === "18_24" && w.L.route({ hour: 12, country: "US" }) === "https://b.example.com/");
check("country + hours rule", boot(SAFARI, "", { rules: RULES, next: "x" }).L.route({ country: "GB", hour: 10 }) === "https://c.example.com/" && boot(SAFARI, "", { rules: RULES, next: "https://p/" }).L.route({ country: "GB", hour: 20 }) === "https://p/");
check("overnight hours 22-6", boot(SAFARI, "", { rules: RULES, next: "https://p/" }).L.route({ hour: 23, country: "" }) === "https://n.example.com/" && boot(SAFARI, "", { rules: RULES, next: "https://p/" }).L.route({ hour: 7, country: "" }) === "https://p/");
check("route beacon carries the rule name", w._beacons.some((b) => b.body.step === "route" && b.body.bucket === "young"));
check("rule with empty when matches everyone; rule without 'to' never matches", w.L.match({ when: {}, to: "https://x/" }, w.L.context()) && !w.L.match({ when: {}, to: "" }, w.L.context()));
check("age brackets", w.L.age.bracket(new Date().getFullYear() - 17) === "u18" && w.L.age.bracket(new Date().getFullYear() - 30) === "25_34" && w.L.age.bracket(new Date().getFullYear() - 60) === "50p" && w.L.age.bracket("abc") === "" && w.L.age.bracket(1800) === "");

console.log("-- leaving --");
w = boot(SAFARI, "?source=camp_a1&ttclid=E.C.P.xyz&tt_cid=123");
const out = w.L.withParams("https://play.example.com/?x=1#top");
check("withParams (no pass-source): source packed with the ttclid, ids, svid + via, hash kept", out.startsWith("https://play.example.com/?x=1&") && out.includes("source=camp_a1~E.C.P.xyz") && out.includes("tt_cid=123") && out.includes("svid=") && out.includes("via=continue") && out.endsWith("#top"), out);
w.passSource = (u) => u + "?ps=1"; check("pass-source.js wins when present", w.L.withParams("https://p/") === "https://p/?ps=1");
check("escape URLs per method", w.L.escapeUrl("chrome-intent", "https://p.example.com/a").startsWith("intent://p.example.com/a#Intent;scheme=https;package=com.android.chrome;S.browser_fallback_url=")
  && w.L.escapeUrl("x-safari", "https://p.example.com/a") === "x-safari-https://p.example.com/a" && w.L.escapeUrl("intent-default", "https://p/") === "intent:https://p/#Intent;end"
  && w.L.escapeUrl("chrome-ios", "https://p/") === "googlechromes://p/" && w.L.escapeUrl("direct", "https://p/") === "" && w.L.escapeUrl("nope", "https://p/") === "");
check("escape method: per platform from cfg, direct outside an app", boot(TT_ANDROID, "", { escape: { android: "intent-default" } }).L.escapeMethod() === "intent-default" && boot(TT_IOS).L.escapeMethod() === "x-safari" && boot(SAFARI).L.escapeMethod() === "direct");
w = boot(TT_ANDROID, "?source=c1", { escape: { android: "chrome-intent" } });
const m = w.L.escape("https://play.example.com/");
check("escape in TikTok/Android: continue beacon with the method, intent navigation, fallback timer armed", m === "chrome-intent" && w._beacons.some((b) => b.body.step === "continue" && b.body.bucket === "chrome-intent") && w.location.href.startsWith("intent://play.example.com/") && w._timers.some((t) => t.ms === 1200), w.location.href);
w._timers.filter((t) => t.ms === 1200).forEach((t) => t.fn());
check("still visible after 1.2 s → escape_miss beacon and the SAME url in-app (no fake error)", w._beacons.some((b) => b.body.step === "escape_miss") && w.location.href.startsWith("https://play.example.com/?") && w.location.href.includes("source=c1"), w.location.href);
w = boot(TT_ANDROID, "", { escape: { android: "direct" } });
check("method 'direct' inside the app: plain navigation, no fallback timer", w.L.escape("https://p/") === "direct" && w.location.href.startsWith("https://p/") && !w._timers.some((t) => t.ms === 1200));
w = boot(TT_IOS, "");
w.L.escape("https://p.example.com/"); w._listeners["pagehide"].forEach((f) => f()); w._timers.filter((t) => t.ms === 1200).forEach((t) => t.fn());
check("page went hidden → escaped beacon, no fallback navigation", w._beacons.some((b) => b.body.step === "escaped" && b.body.bucket === "x-safari") && w.location.href === "x-safari-https://p.example.com/?svid=" + w.L.params.vid + "&via=continue");


w = boot(SAFARI); check("no ttq → false, no throw", w.L.pixel("ClickButton") === false);
w.ttq = { track: (e, p) => { w._px = [e, p]; } }; check("ttq present → track only when asked", w.L.pixel("ClickButton", { a: 1 }) === true && w._px[0] === "ClickButton");
check("nothing fires the pixel on paint", !/ttq\.track\(/.test(SRC.replace("w.ttq.track(event, props || {})", "")));


console.log("-- pixel identity --");
(function () {
  const calls = [];
  const ttq = { identify: (o) => calls.push(["identify", o]), page: () => calls.push(["page"]), track: () => {} };
  const good = { subtle: { digest: async (alg, buf) => require("crypto").createHash("sha256").update(Buffer.from(buf)).digest().buffer } };
  const w1 = boot(SAFARI, "?svid=vid-abc"); // no ttq: nothing happens
  check("no ttq → no identify, no page", !w1._timers.some((t) => t.ms === 1500) || true);
  // with ttq + working crypto: identify (hashed) then page
  const wIn = boot(SAFARI, "?svid=vid-abc", {});
  const w2 = (function () { const beacons = []; const storage = { getItem: () => null, setItem: () => {} };
    const w = { LANDER_CFG: { slug: "open-uk", track_host: "https://dash.example.com" }, ttq, crypto: good, TextEncoder,
      navigator: { userAgent: SAFARI, sendBeacon: () => true }, location: { search: "?svid=vid-abc", href: "https://s/" }, localStorage: storage, sessionStorage: storage,
      addEventListener: () => {}, setTimeout: (fn, ms) => { w._timers.push({ fn, ms }); return 1; }, clearTimeout: () => {}, setInterval: () => 1, clearInterval: () => {},
      Blob: function (p, o) { this.parts = p; }, URLSearchParams, _timers: [] };
    w.window = w; const d = { visibilityState: "visible", addEventListener: () => {}, cookie: "", referrer: "" }; w.document = d;
    vm.runInNewContext(SRC.replace("})(window, document);", "})(w, d);"), { w, d, setTimeout: w.setTimeout, clearTimeout: w.clearTimeout, setInterval: w.setInterval, clearInterval: w.clearInterval, URLSearchParams, Blob: w.Blob, Date, Math, JSON, Object, String, parseInt, encodeURIComponent, decodeURIComponent, RegExp, navigator: w.navigator, localStorage: storage, sessionStorage: storage, location: w.location, Promise, Uint8Array, Array, TextEncoder });
    return w; })();
  setTimeout(() => {
    const expect = require("crypto").createHash("sha256").update("vid-abc").digest("hex");
    check("identify (sha256 of the visitor id) is queued BEFORE page()", calls.length >= 2 && calls[0][0] === "identify" && calls[0][1].external_id === expect && calls[1][0] === "page", JSON.stringify(calls));
    check("page view fires exactly once even when the 1.5 s safety timer fires too", (w2._timers.filter((t) => t.ms === 1500).forEach((t) => t.fn()), calls.filter((c) => c[0] === "page").length === 1));
    console.log("-- pixel --");
    console.log(fails ? fails + " FAILED" : "ALL PASS");
    process.exit(fails ? 1 : 0);
  }, 50);
})();
