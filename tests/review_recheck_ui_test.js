/* v155.19 — Review step: a page / form still "missing" is re-checked on TikTok by itself — on a
   timer while the review is on screen, and at once when a share / copy / build job finishes — so
   the row turns green without the operator pressing anything. Real ui.js + launch-review.js in
   Chromium; the dashboard's JSON answers are stubbed. */
"use strict";
const path = require("path"), fs = require("fs"), { execSync } = require("child_process");
const fails = [];
function check(n, c, x) { console.log((c ? "PASS " : "FAIL ") + n + (!c && x ? "  [" + x + "]" : "")); if (!c) fails.push(n); }
let pw = null;
for (const w of [null, (() => { try { return execSync("npm root -g", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim(); } catch (e) { return ""; } })()]) {
  try { pw = w ? require(path.join(w, "playwright")) : require("playwright"); break; } catch (e) {}
}
if (!pw) { console.log("SKIP-SUITE: Playwright isn't installed here"); process.exit(1); }
const S = path.resolve(__dirname, "..", "app/static") + "/";
(async () => {
  const b = await pw.chromium.launch(); const p = await b.newPage(); const errs = []; p.on("pageerror", (e) => errs.push(String(e)));
  await p.route("**/*", (r) => r.fulfill({ contentType: "text/html; charset=utf-8", body: '<!doctype html><html><body><form id="f"><select name="template_id"><option selected>Lead preset</option></select></form><div id="lr"></div></body></html>' }));
  await p.goto("http://localhost/super-launcher");
  await p.addScriptTag({ content: fs.readFileSync(S + "ui.js", "utf8") });
  await p.evaluate(() => { window.LR_RECHECK_MS = 400; });
  await p.addScriptTag({ content: fs.readFileSync(S + "launch-review.js", "utf8") });
  await p.evaluate(() => {
    window.__posts = []; window.__has = false; window.adopsToast = () => {};
    UI.post = (url, data) => {
      window.__posts.push(url + " " + JSON.stringify(data));
      if (url === "/campaigns/review.json") {
        const form = window.__has ? { state: "ok", text: "tes" } : { state: "bad", text: "missing", hint: "no lead form “tes” on this account" };
        return Promise.resolve({ ok: true, rows: [{ id: "7659133246093148168", name: "blue bat_260706030009", blocked: window.__has ? "" : "no lead form “tes” on this account", warnings: [], tz: "UTC−5", starts: "13:28",
          cells: { form, terms: { state: "ok", text: "accepted" }, geo: { state: "ok", text: "Any geo" } } }] });
      }
      return Promise.resolve({ ok: true });
    };
    UI.get = () => Promise.resolve({ ok: true });
    LR.mount(document.getElementById("lr"), { form: document.getElementById("f"), params: () => ({ a: 1 }) }).refresh(true);
  });
  await p.waitForSelector(".lr-live");
  const reviews = () => p.evaluate(() => window.__posts.filter((x) => x.startsWith("/campaigns/review.json")));
  check("a missing form: blocked, with the Re-check button and a note that it re-checks itself",
        (await p.$$eval(".lr-blocked", (r) => r.length)) === 1 && (await p.textContent(".lr-auto")).includes("re-checks itself"));
  check("the first read isn't a live one", (await reviews()).length === 1 && !(await reviews())[0].includes('"live"'));
  await p.waitForFunction(() => window.__posts.filter((x) => x.startsWith("/campaigns/review.json")).length >= 3, null, { timeout: 4000 }).catch(() => {});
  const r3 = await reviews();
  check("while it stays missing, TikTok is asked again on the timer (live reads)", r3.length >= 3 && r3.slice(1).every((x) => x.includes('"live":"1"')), r3.join(" | "));
  // the form appears (shared / copied) and a job says so → checked at once, row turns green
  await p.evaluate(() => { window.__has = true; document.dispatchEvent(new CustomEvent("adops:job", { detail: { kind: "lead_form_clone_all", status: "ok" } })); });
  await p.waitForFunction(() => !document.querySelector(".lr-blocked"), null, { timeout: 3000 }).catch(() => {});
  check("a finished form-copy job re-checks at once and the row goes green — nothing to press", !(await p.$(".lr-blocked")) && !(await p.$(".lr-live")));
  const n = (await reviews()).length;
  await p.waitForTimeout(1200);
  check("once nothing is missing the timer stops", (await reviews()).length === n);
  // an unrelated job doesn't cause a re-read
  await p.evaluate(() => { document.dispatchEvent(new CustomEvent("adops:job", { detail: { kind: "status_sync", status: "ok" } })); });
  await p.waitForTimeout(300);
  check("an unrelated job doesn't", (await reviews()).length === n);
  check("no page errors", errs.length === 0, errs.join(" | "));
  await b.close(); console.log("---"); console.log(fails.length ? fails.length + " failed" : "all passed"); process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.log("FAIL crashed: " + e); process.exit(1); });
