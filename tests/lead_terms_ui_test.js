/* v155.13 — Review step: the Lead terms column fills in lazily, an account without TikTok's Lead
   Generation Terms is blocked with a Share-able reason, and "Accept Lead Generation Terms" asks first,
   then signs and re-checks in place (no reload). Real ui.js + launch-review.js in Chromium; the
   dashboard's JSON answers are stubbed. */
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
  await p.addScriptTag({ content: fs.readFileSync(S + "ui.js", "utf8") }); await p.addScriptTag({ content: fs.readFileSync(S + "launch-review.js", "utf8") });
  await p.evaluate(() => {
    window.__posts = []; window.adopsToast = () => {}; window.__signed = { "7659134559183142930": true };
    UI.post = (url, data) => {
      window.__posts.push(url + " " + JSON.stringify(data));
      if (url === "/campaigns/review.json") return Promise.resolve({ ok: true, rows: [
        { id: "7659134748954083346", name: "blue bat_260706030014", blocked: "", warnings: [], tz: "UTC−5", starts: "16:57", terms_pending: true, cells: { form: { state: "ok", text: "Untitled form" }, terms: { state: "na", text: "checking…" } } },
        { id: "7659134559183142930", name: "blue bat_260706030017", blocked: "", warnings: [], tz: "UTC−5", starts: "16:57", terms_pending: true, cells: { form: { state: "ok", text: "Untitled form" }, terms: { state: "na", text: "checking…" } } }] });
      if (url === "/campaigns/review/lead-terms.json") {
        const cells = {}; data.advertiser_ids.split(",").forEach((id) => { cells[id] = window.__signed[id] ? { state: "ok", text: "accepted" } : { state: "bad", text: "not accepted", hint: "TikTok's Lead Generation Terms aren't accepted on this ad account" }; });
        return Promise.resolve({ ok: true, cells });
      }
      if (url === "/campaigns/lead-terms/accept") { data.advertiser_ids.split(",").forEach((id) => { window.__signed[id] = true; }); return Promise.resolve({ ok: true, msg: "Accepted on 1 of 1 account(s)." }); }
      return Promise.resolve({ ok: true });
    };
    LR.mount(document.getElementById("lr"), { form: document.getElementById("f"), params: () => ({ a: 1 }) }).refresh(true);
  });
  await p.waitForSelector(".lr-terms");
  const col = await p.$$eval(".lr-t thead th", (t) => t.map((x) => x.textContent));
  check("a Lead terms column", col.includes("Lead terms"), col.join(","));
  check("the account without the terms is blocked, the other isn't", await p.evaluate(() => [...document.querySelectorAll(".lr-t tbody tr")].map((r) => r.classList.contains("lr-blocked")).join(",")) === "true,false");
  check("one button for the accounts missing it, with the terms linked", (await p.textContent(".lr-terms")).includes("1 account") && !!(await p.$('a[href*="lead-gen-terms"]')));
  await p.click(".lr-terms"); await p.waitForSelector(".modal");
  const dialog = await p.textContent(".modal");
  check("asks first, naming what gets signed", dialog.includes("Lead Generation Terms") && dialog.includes("lead-gen-terms"), dialog.slice(0, 160));
  check("nothing was signed before the answer", !(await p.evaluate(() => window.__posts.some((x) => x.startsWith("/campaigns/lead-terms/accept")))));
  await p.evaluate(() => { const bs = [...document.querySelectorAll(".modal button")]; (bs.find((x) => /Accept on/.test(x.textContent)) || bs[bs.length - 1]).click(); });
  await p.waitForFunction(() => !document.querySelector(".lr-terms") && ![...document.querySelectorAll(".lr-t tbody tr")].some((r) => r.classList.contains("lr-blocked")), null, { timeout: 5000 }).catch(() => {});
  check("after accepting: only the missing account was sent, re-checked in place, nobody blocked, button gone",
        (await p.evaluate(() => window.__posts.filter((x) => x.startsWith("/campaigns/lead-terms/accept")).join("|"))).includes('"advertiser_ids":"7659134748954083346"')
        && !(await p.$(".lr-terms")) && (await p.evaluate(() => ![...document.querySelectorAll(".lr-t tbody tr")].some((r) => r.classList.contains("lr-blocked")))));
  check("no page errors", errs.length === 0, errs.join(" | "));
  await b.close(); console.log("---"); console.log(fails.length ? fails.length + " failed" : "all passed"); process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.log("FAIL crashed: " + e); process.exit(1); });
