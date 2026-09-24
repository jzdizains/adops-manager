/* v155.14 — Launch result: the "Lead Generation Terms" failure's button accepts them for THAT account
   (after a confirm), then points at Retry failed. Renders the real launch_result.html (jinja2 +
   the real error_messages.fix_for) and drives it in Chromium; the accept call is stubbed. */
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
const out = path.join(os.tmpdir(), "lr_terms_" + process.pid + ".html");
const py = `
import sys, types, json
sys.path.insert(0, sys.argv[1])
from jinja2 import Environment, FileSystemLoader, ChoiceLoader, DictLoader, ChainableUndefined
from app import error_messages as em
base='<!doctype html><html><head><meta charset="utf-8"><script src="/static/ui.js?v=1"></script></head><body>{% block content %}{% endblock %}</body></html>'
env=Environment(loader=ChoiceLoader([DictLoader({'base.html':base}),FileSystemLoader(sys.argv[1]+'/app/templates')]),undefined=ChainableUndefined,autoescape=True)
env.filters.update(local=lambda *a,**k:'',ago=lambda *a,**k:'',money=str,strip_key=str,js=json.dumps)
env.globals.update(fix_for=em.fix_for, acct_link=lambda a,b: b)
msg="TikTok's Lead Generation Terms aren't accepted on this ad account, so TikTok would refuse the Instant Form ad - nothing was created."
L=types.SimpleNamespace(id=5,batch_ref='892537',ok=False,advertiser_id='7659134748954083346',advertiser_name='blue bat_260706030014',campaign_id='',
  error_code='CONFIG',error_message=msg,error_technical=msg,template_id=1,spark_code_id=None,template_name='Lead preset',optimization_event='',source='',landing_url='')
h=env.get_template('launch_result.html').render(logs=[L],batch_ref='892537',ok_count=0,fail_count=1,retry_n=1,can_retry=True,retry={},traces={},job=None,request=types.SimpleNamespace(query_params={}))
open(sys.argv[2],'w').write(h)`;
execFileSync("python3", ["-c", py, ROOT, out]);
(async () => {
  const b = await pw.chromium.launch(); const p = await b.newPage(); const errs = [], posts = []; p.on("pageerror", (e) => errs.push(String(e)));
  await p.route("**/*", (r) => {
    const u = r.request().url();
    if (u.includes("/static/")) return r.fulfill({ contentType: "application/javascript", body: fs.readFileSync(S + u.split("/static/")[1].split("?")[0]) });
    if (u.includes("/campaigns/lead-terms/text.json")) return r.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true, text: "TIKTOK LEAD GEN TERMS TEXT" }) });
    if (u.includes("/campaigns/lead-terms/accept")) { posts.push(r.request().postData()); return r.fulfill({ contentType: "application/json", body: JSON.stringify({ ok: true, msg: "Accepted on 1 of 1 account(s)." }) }); }
    return r.fulfill({ contentType: "text/html; charset=utf-8", body: fs.readFileSync(out, "utf8") });
  });
  await p.goto("http://localhost/campaigns/result/892537");
  await p.evaluate(() => { window.adopsToast = () => {}; });
  const btn = await p.$(".lt-accept");
  check("the failure shows a Confirm button (not just a link to read)", !!btn && (await btn.textContent()).includes("Confirm Lead Generation Terms"));
  check("…for that account, with the terms still one click away", (await btn.getAttribute("data-adv")) === "7659134748954083346" && !!(await p.$('.lr-fix a[href*="lead-gen-terms"]')));
  await btn.click(); await p.waitForSelector(".modal");
  await p.waitForFunction(() => /TIKTOK LEAD GEN TERMS TEXT/.test(document.querySelector(".modal").textContent), null, { timeout: 3000 }).catch(() => {});
  const dlg = await p.textContent(".modal");
  check("it asks first, naming the account and showing TikTok's own Terms text", dlg.includes("blue bat_260706030014") && dlg.includes("TIKTOK LEAD GEN TERMS TEXT") && !!(await p.$(".modal a[href*=lead-gen-terms]")), dlg.slice(0, 200));
  check("nothing sent before the answer", posts.length === 0);
  await p.evaluate(() => { const bs = [...document.querySelectorAll(".modal button")]; (bs.find((x) => /^Confirm on/.test(x.textContent.trim())) || bs[bs.length - 1]).click(); });
  await p.waitForFunction(() => /Confirmed/.test(document.querySelector(".lt-accept").textContent), null, { timeout: 5000 }).catch(() => {});
  check("confirms that one account, then says Retry failed is next", posts.length === 1 && decodeURIComponent(posts[0]).includes("advertiser_ids=7659134748954083346")
        && (await p.textContent(".lt-accept")).includes("now Retry failed"));
  check("…and highlights the Retry failed button", await p.evaluate(() => document.querySelector('form[action$="/retry"] button').classList.contains("primary")));
  check("no page errors", errs.length === 0, errs.join(" | "));
  await b.close(); try { fs.unlinkSync(out); } catch (e) {}
  console.log("---"); console.log(fails.length ? fails.length + " failed" : "all passed"); process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.log("FAIL crashed: " + e); process.exit(1); });
