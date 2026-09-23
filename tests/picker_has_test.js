/* v155.2 — the account pop-up shows which accounts ALREADY have the form / page being copied.

   Runs the shipped ui.js + account-picker-modal.js in real Chromium (Playwright) with a stubbed
   /accounts/picker.json: "Has it" pills, those rows can't be ticked (the copy would be skipped),
   they sort last, "select all" in a Business Center skips them, the Has it / Missing it chips
   filter, and the counts say how many have it. */
"use strict";
const path = require("path"), fs = require("fs"), { execSync } = require("child_process");
const fails = [];
function check(name, cond, extra) {
  console.log((cond ? "PASS " : "FAIL ") + name + (!cond && extra ? "  [" + extra + "]" : ""));
  if (!cond) fails.push(name);
}
let pw = null;
for (const where of [null, (() => { try { return execSync("npm root -g", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim(); } catch (e) { return ""; } })()]) {
  try { pw = where ? require(path.join(where, "playwright")) : require("playwright"); break; } catch (e) { /* next */ }
}
if (!pw) { console.log("SKIP-SUITE: Playwright isn't installed here"); process.exit(1); }

const ROOT = path.resolve(__dirname, "..");
const js = (f) => fs.readFileSync(path.join(ROOT, "app/static", f), "utf8");
const ACCTS = {
  accounts: [
    { id: "1", name: "Blue Bat A", state: "fresh", bc: "bc1", bc_name: "Blue Bat 3/9" },
    { id: "2", name: "Blue Bat B", state: "blocked", bc: "bc1", bc_name: "Blue Bat 3/9" },
    { id: "3", name: "Blue Bat C", state: "active", bc: "bc1", bc_name: "Blue Bat 3/9" },
    { id: "4", name: "Other D", state: "fresh", bc: "bc2", bc_name: "Chenchij" },
    { id: "9", name: "Source", state: "used", bc: "bc2", bc_name: "Chenchij" },
  ],
  bcs: [{ id: "bc1", name: "Blue Bat 3/9", n: 3 }, { id: "bc2", name: "Chenchij", n: 2 }],
};

(async () => {
  const browser = await pw.chromium.launch();
  const page = await browser.newPage();
  const errs = [];
  page.on("pageerror", (e) => errs.push(String(e)));
  await page.route("**/*", (r) => {
    const u = r.request().url();
    if (u.endsWith("/accounts/picker.json")) return r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(ACCTS) });
    return r.fulfill({ status: 200, contentType: "text/html", body: "<!doctype html><html><head></head><body></body></html>" });
  });
  await page.goto("http://picker.test/");
  await page.addScriptTag({ content: js("ui.js") });
  await page.addScriptTag({ content: js("account-picker-modal.js") });
  await page.evaluate(() => {
    window.__res = "pending";
    UI.pickAccounts({ title: "Clone", confirmLabel: "Clone to selected", exclude: ["9"],
                      has: { "1": "PUBLISHED", "3": "DRAFT" }, hasLabel: "Has this form" })
      .then((r) => { window.__res = r; });
  });
  await page.waitForSelector(".apk-row");
  const rows = await page.$$eval(".apk-row", (rs) => rs.map((r) => ({
    name: r.querySelector(".apk-nm").textContent, has: (r.querySelector(".apk-has") || {}).textContent || "",
    disabled: r.querySelector(".apk-cb").disabled })));
  const byName = Object.fromEntries(rows.map((r) => [r.name, r]));
  check("the source account isn't offered", !byName["Source"]);
  check("accounts with it wear a pill (a draft says so)", byName["Blue Bat A"].has === "✓ Has this form" && byName["Blue Bat C"].has === "✓ Has this form · draft" && byName["Blue Bat B"].has === "", JSON.stringify(rows));
  check("…and can't be ticked", byName["Blue Bat A"].disabled && byName["Blue Bat C"].disabled && !byName["Blue Bat B"].disabled && !byName["Other D"].disabled);
  check("they sort last in their Business Center", rows.filter((r) => r.name.startsWith("Blue")).map((r) => r.name).join(",") === "Blue Bat B,Blue Bat A,Blue Bat C", rows.map((r) => r.name).join(","));
  const head = await page.$eval(".apk-group .apk-gn", (e) => e.textContent);
  check("group header: selectable count and how many have it", head === "0 / 1 · 2 have it", head);
  const chips = await page.$$eval(".apk-have", (bs) => bs.map((b) => b.textContent.replace(/\s+/g, " ").trim()));
  check("Missing it / Has it chips with counts", chips.join("|") === "Missing it 2|Has this form 2", chips.join("|"));

  await page.click(".apk-group .apk-gall");
  const ticked = await page.$$eval(".apk-cb:checked", (cs) => cs.map((c) => c.value));
  check("'select all' in a BC skips the ones that have it", ticked.join(",") === "2", ticked.join(","));

  await page.click('.apk-have[data-h="has"]');
  const vis1 = await page.$$eval(".apk-row", (rs) => rs.filter((r) => r.style.display !== "none").map((r) => r.querySelector(".apk-nm").textContent));
  check("'Has it' shows only those", vis1.sort().join(",") === "Blue Bat A,Blue Bat C", vis1.join(","));
  await page.click('.apk-have[data-h="missing"]');
  const vis2 = await page.$$eval(".apk-row", (rs) => rs.filter((r) => r.style.display !== "none").map((r) => r.querySelector(".apk-nm").textContent));
  check("'Missing it' shows the rest", vis2.sort().join(",") === "Blue Bat B,Other D", vis2.join(","));
  await page.click('.apk-have[data-h="missing"]');
  const vis3 = await page.$$eval(".apk-row", (rs) => rs.filter((r) => r.style.display !== "none").length);
  check("clicking it again clears the filter", vis3 === 4, String(vis3));

  await page.click(".apk-go");
  await page.waitForFunction(() => window.__res !== "pending");
  check("confirm returns only the ticked accounts", JSON.stringify(await page.evaluate(() => window.__res)) === '["2"]');

  // without `has`, the picker is exactly as before (no chips, nothing disabled)
  await page.evaluate(() => { window.__res = "pending"; UI.pickAccounts({ title: "Plain", exclude: ["9"] }).then((r) => { window.__res = r; }); });
  await page.waitForFunction(() => document.querySelectorAll(".apk-row").length === 4 && !document.querySelector(".apk-have"));
  check("other callers are unchanged", await page.evaluate(() => !document.querySelector(".apk-has") && !document.querySelector(".apk-cb:disabled")));
  check("no page errors", errs.length === 0, errs.join(" | "));
  await browser.close();
  console.log("---"); console.log(fails.length ? fails.length + " failed" : "all passed");
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.log("FAIL crashed: " + e); process.exit(1); });
