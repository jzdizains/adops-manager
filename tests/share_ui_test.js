/* v155.9 — Share buttons in a real browser: data-share copies (clipboard), data-share-url fetches the
   report then copies, and when the clipboard is refused the text opens selected in a pop-up. */
"use strict";
const path = require("path"), fs = require("fs"), { execSync } = require("child_process");
const fails = [];
function check(n, c, x) { console.log((c ? "PASS " : "FAIL ") + n + (!c && x ? "  [" + x + "]" : "")); if (!c) fails.push(n); }
let pw = null;
for (const w of [null, (() => { try { return execSync("npm root -g", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim(); } catch (e) { return ""; } })()]) {
  try { pw = w ? require(path.join(w, "playwright")) : require("playwright"); break; } catch (e) {}
}
if (!pw) { console.log("SKIP-SUITE: Playwright isn't installed here"); process.exit(1); }
const ROOT = path.resolve(__dirname, "..");
const ui = fs.readFileSync(path.join(ROOT, "app/static/ui.js"), "utf8");
(async () => {
  const b = await pw.chromium.launch();
  const ctx = await b.newContext({ permissions: ["clipboard-read", "clipboard-write"] });
  const p = await ctx.newPage(); const errs = []; p.on("pageerror", (e) => errs.push(String(e)));
  await p.route("**/*", (r) => {
    const u = r.request().url();
    if (u.includes("/share/")) return r.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true, text: "FAILED LAUNCH — preset X\nTikTok's answer: Invalid budget type." }) });
    return r.fulfill({ contentType: "text/html; charset=utf-8", body: '<!doctype html><html><head><script src="/static/ui.js?v=160"></script></head><body>' +
      '<button id="a" data-share="Job: clone form\nResult: 1 failed" data-share-title="FAILED JOB #7">⧉ Share</button>' +
      '<button id="b" data-share-url="/campaigns/result/892537/share/5">⧉ Share</button></body></html>' });
  });
  await p.route("**/static/ui.js*", (r) => r.fulfill({ contentType: "application/javascript", body: ui }));
  await p.goto("http://localhost/campaigns/result/892537");   // localhost counts as secure: the clipboard API is there
  await p.evaluate(() => { window.__t = []; window.adopsToast = (k, m) => window.__t.push(k + ":" + m); });
  await p.click("#a"); await p.waitForTimeout(200);
  let clip = await p.evaluate(() => navigator.clipboard.readText().catch(() => "(no clipboard)"));
  const copiedA = clip.startsWith("FAILED JOB #7\nhttp://localhost/campaigns/result/892537") || (await p.evaluate(() => !!document.querySelector(".modal textarea")));
  check("data-share: the header (title, page, time, version) and the text are copied", copiedA, clip.slice(0, 120));
  const modalA = await p.evaluate(() => { const t = document.querySelector(".modal textarea"); return t ? t.value : ""; });
  const textA = clip.includes("Result: 1 failed") ? clip : modalA;
  check("…the text itself is intact", textA.includes("Job: clone form\nResult: 1 failed") && textA.includes("v160"), textA.slice(0, 200));
  // v155.20: the pop-up ALWAYS opens (a click on Share visibly does something), text already copied, Copy button confirms
  check("the report opens in a pop-up every time, already copied, with a Copy button saying so",
        modalA.includes("Job: clone form") && (await p.$eval(".modal .share-copy", (x) => x.textContent)) === "✓ Copied"
        && (await p.$eval(".modal .modal-h span", (x) => x.textContent)).startsWith("Share: FAILED JOB #7"));
  await p.evaluate(() => navigator.clipboard.writeText("x"));
  await p.click(".modal .share-copy"); await p.waitForTimeout(150);
  check("…Copy copies it again", (await p.evaluate(() => navigator.clipboard.readText())).includes("Job: clone form"));
  await p.evaluate(() => document.querySelectorAll(".modal [data-close]").forEach((x) => x.click()));
  const labelB = await p.$eval("#b", (x) => x.textContent);
  await p.click("#b"); await p.waitForTimeout(400);
  clip = await p.evaluate(() => navigator.clipboard.readText().catch(() => ""));
  const modalB = await p.evaluate(() => { const t = document.querySelector(".modal textarea"); return t ? t.value : ""; });
  check("data-share-url: the report is fetched, then copied", (clip + modalB).includes("TikTok's answer: Invalid budget type."), (clip + modalB).slice(0, 160));
  check("the button comes back after collecting", await p.$eval("#b", (x, l) => !x.disabled && x.textContent === l, labelB));
  // clipboard refused → the pop-up with the text, selected
  await p.evaluate(() => { document.querySelectorAll(".modal [data-close]").forEach((x) => x.click());
    Object.defineProperty(navigator, "clipboard", { value: { writeText: () => Promise.reject(new Error("denied")) }, configurable: true });
    document.execCommand = () => false; });
  await p.click("#a"); await p.waitForTimeout(300);
  const sel = await p.evaluate(() => { const t = document.querySelector(".modal textarea"); return t ? [t.value.includes("Result: 1 failed"), t.selectionEnd - t.selectionStart === t.value.length] : [false, false]; });
  check("clipboard refused → the text opens in a pop-up, fully selected", sel[0] && sel[1], JSON.stringify(sel));
  check("no page errors", errs.length === 0, errs.join(" | "));
  await b.close();
  console.log("---"); console.log(fails.length ? fails.length + " failed" : "all passed"); process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.log("FAIL crashed: " + e); process.exit(1); });
