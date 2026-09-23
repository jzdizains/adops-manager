/* v155.10 — "New form": the accounts come from the same pop-up as everywhere else (search, BC, status,
   multi-select). Renders the real lead_forms.html (jinja2) and drives it in Chromium: submitting with no
   account opens the picker, an account that already has the typed name is locked, the pick is posted. */
"use strict";
const path = require("path"), fs = require("fs"), os = require("os"), { execSync, execFileSync } = require("child_process");
const fails = [];
function check(n, c, x) { console.log((c ? "PASS " : "FAIL ") + n + (!c && x ? "  [" + x + "]" : "")); if (!c) fails.push(n); }
let pw = null;
for (const w of [null, (() => { try { return execSync("npm root -g", { stdio: ["ignore", "pipe", "ignore"] }).toString().trim(); } catch (e) { return ""; } })()]) {
  try { pw = w ? require(path.join(w, "playwright")) : require("playwright"); break; } catch (e) {}
}
if (!pw) { console.log("SKIP-SUITE: Playwright isn't installed here"); process.exit(1); }
const ROOT = path.resolve(__dirname, ".."), S = path.join(ROOT, "app/static/");
const out = path.join(os.tmpdir(), "lf_targets_" + process.pid + ".html");
const py = `
import json, types, sys
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader, ChainableUndefined
from markupsafe import Markup
base='<!doctype html><html><head><meta charset="utf-8"><link rel="stylesheet" href="/static/style.css"><script src="/static/ui.js?v=1"></script><script src="/static/account-picker-modal.js"></script></head><body>{% block content %}{% endblock %}{% block scripts %}{% endblock %}</body></html>'
env=Environment(loader=ChoiceLoader([DictLoader({'base.html':base}),FileSystemLoader(sys.argv[1])]),undefined=ChainableUndefined,autoescape=True)
env.filters.update(local=lambda *a,**k:'',ago=lambda *a,**k:'',money=str,strip_key=str,js=lambda v: Markup(json.dumps(v)))
F=lambda fid,n,o: types.SimpleNamespace(form_id=fid,name=n,owner_advertiser_id=o,status='PUBLISHED')
A=lambda i,n: types.SimpleNamespace(advertiser_id=i,advertiser_name=n,owner_bc_id='bc1')
acc=[A('1','A1 Prime Fashion LLR'),A('2','blue bat_260706030014'),A('3','blue bat_260706030017')]
h=env.get_template('lead_forms.html').render(forms=[F('77','Untitled form','9')],groups=[],names={},accounts=acc,copies={},bcs=[],missing={},acct_bc={},n_accounts=3,web_ready=True,ok='',err='',form_templates=[],ftpl_cov={},form_names={},have_status={'Games 18+':{'3':'PUBLISHED'}},acct_names=[[a.advertiser_id,a.advertiser_name] for a in acc],request=None)
open(sys.argv[2],'w').write(h)`;
execFileSync("python3", ["-c", py, path.join(ROOT, "app/templates"), out]);
(async () => {
  const b = await pw.chromium.launch(); const p = await b.newPage({ viewport: { width: 1300, height: 900 } });
  const errs = [], posts = []; p.on("pageerror", (e) => errs.push(String(e)));
  await p.route("**/*", (r) => {
    const u = r.request().url();
    if (u.includes("/static/")) { const f = S + u.split("/static/")[1].split("?")[0]; return r.fulfill({ body: fs.readFileSync(f), contentType: f.endsWith(".css") ? "text/css" : "application/javascript" }); }
    if (u.endsWith("/accounts/picker.json")) return r.fulfill({ contentType: "application/json", body: JSON.stringify({ accounts: [
      { id: "1", name: "A1 Prime Fashion LLR", state: "fresh", bc: "bc1", bc_name: "Blue Bat" }, { id: "2", name: "blue bat_260706030014", state: "fresh", bc: "bc1", bc_name: "Blue Bat" },
      { id: "3", name: "blue bat_260706030017", state: "used", bc: "bc1", bc_name: "Blue Bat" }], bcs: [{ id: "bc1", name: "Blue Bat", n: 3 }] }) });
    if (r.request().method() === "POST") { posts.push(decodeURIComponent(r.request().postData() || "")); return r.fulfill({ contentType: "text/html", body: "ok" }); }
    return r.fulfill({ contentType: "text/html; charset=utf-8", body: fs.readFileSync(out, "utf8") });
  });
  await p.goto("http://localhost/lead-forms");
  check("the dropdown is gone; the field opens the account pop-up", !(await p.$('select[name="target_advertiser_id"]')) && !!(await p.$("#lfTargetsBtn")));
  await p.click("#lfNewBtn"); await p.waitForSelector("#lfTargetsBtn");
  await p.fill('#lfBuildForm [name="name"]', "Games 18+");
  await p.click("#lfBuildGo"); await p.waitForSelector(".apk-row");
  check("submitting with no account opens the picker (nothing is posted)", posts.length === 0);
  const rows = await p.$$eval(".apk-row", (rs) => rs.map((r) => [r.querySelector(".apk-nm").textContent, !!r.querySelector(".apk-has"), r.querySelector(".apk-cb").disabled]));
  check("an account that already has the typed name is marked and locked", JSON.stringify(rows.find((r) => r[0].endsWith("030017"))) === '["blue bat_260706030017",true,true]', JSON.stringify(rows));
  await p.click(".apk-group .apk-gall"); await p.click(".apk-go"); await p.waitForTimeout(150);
  check("the field shows the pick by name", (await p.textContent("#lfTargetsBtn")) === "2 accounts" && (await p.textContent("#lfTargetsSum")).includes("A1 Prime Fashion LLR, blue bat_260706030014"));
  await p.click("#lfBuildGo"); await p.waitForTimeout(300);
  check("the picked accounts are posted as target_ids", posts.length === 1 && posts[0].includes("target_ids=1,2"), posts.join(" | "));
  check("no page errors", errs.length === 0, errs.join(" | "));
  await b.close(); try { fs.unlinkSync(out); } catch (e) {}
  console.log("---"); console.log(fails.length ? fails.length + " failed" : "all passed"); process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.log("FAIL crashed: " + e); process.exit(1); });
