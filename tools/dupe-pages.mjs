// dupe-pages.mjs — copy ONE finished Instant Page onto a list of ad accounts, published,
// optionally re-pointing its button at another offer URL. Standalone (Node 18+, no
// dependencies); the same flow runs inside the dashboard (Instant Pages › Clone to…),
// which also copies a missing page automatically at launch.
//
//   node tools/dupe-pages.mjs <source_page_id> "<page name>" accounts.txt [new_url] [button text]
//
// cookie.txt  = the full `cookie:` request header of a logged-in ads.tiktok.com session
//               (DevTools › Network › any ads.tiktok.com request › Request Headers).
//               Treat it like a password: never commit it, never share it.
// accounts.txt = one target advertiser id per line.
// done.csv     = advertiser_id,page_id for every success; accounts in it are skipped on a
//               re-run so a second run never creates duplicates.
//
// Flow (recorded from a real "duplicate page" in the Ads Manager editor):
//   1. POST /v1/page_info/{source}/ {account_id: TARGET}  → page_info {data, template_id, thumbnail…}
//   2. POST /v1/create/ {…, duplicate_id: source, account_id: TARGET} → page_id
//      (duplicate_id copies server-side and ignores edits to `data` — a link change needs 3)
//   3. only with a new URL: poll page_info on the new page (record not found right after
//      create), POST /v1/update/ with every link.url re-pointed, read back, refuse if the
//      old URL is still there
//   4. POST /v1/publish/{new}/ {account_id: TARGET} — an unpublished page can't run in ads
// account_id is ALWAYS the target. code 200000, "log into", or a non-JSON answer = dead
// cookies → the run stops. Test on ONE account first and open the copy in Ads Manager.
import { readFileSync, appendFileSync, existsSync } from "node:fs";

const [, , SOURCE, NAME, ACCOUNTS, NEW_URL, NEW_TEXT] = process.argv;
if (!SOURCE || !NAME || !ACCOUNTS) throw new Error('usage: node tools/dupe-pages.mjs <source_page_id> "<page name>" <accounts.txt> [new_url] [button text]');
if (NEW_URL && !/^https?:\/\//i.test(NEW_URL)) throw new Error("new_url must start with http:// or https://");
const COOKIE = readFileSync("cookie.txt", "utf8").trim().replace(/^cookie:\s*/i, "");
const CSRF = COOKIE.match(/(?:^|;\s*)csrftoken=([^;]+)/)?.[1] ?? "";
if (!CSRF) throw new Error("cookie.txt has no csrftoken — copy the WHOLE cookie header");
const done = new Set(existsSync("done.csv") ? readFileSync("done.csv", "utf8").split(/\r?\n/).map((l) => l.split(",")[0]).filter(Boolean) : []);
const targets = readFileSync(ACCOUNTS, "utf8").split(/\s+/).filter((t) => t && !done.has(t));
const BASE = "https://ads.tiktok.com/instant_page/api";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function post(target, path, body) {
  const res = await fetch(BASE + path, {
    method: "POST",
    redirect: "manual",
    headers: {
      cookie: COOKIE,
      "x-csrftoken": CSRF,
      "content-type": "application/json",
      accept: "application/json, text/plain, */*",
      referer: `https://ads.tiktok.com/i18n/material/instantPage?aadvid=${target}`,
      "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    },
    body: JSON.stringify(body),
  });
  const text = await res.text();
  try { return JSON.parse(text); } catch { return { code: -1, msg: `non-JSON answer (HTTP ${res.status}) - cookies probably expired` }; }
}

function rewriteButtons(data, url, text) {
  const doc = JSON.parse(data);
  let touched = 0;
  for (const comp of Object.values(doc.data ?? {})) {
    if (comp && typeof comp === "object" && typeof comp.link?.url === "string") {
      comp.link.url = url;
      if (text) comp.content = { ...(comp.content ?? {}), text };
      touched++;
    }
  }
  return touched ? JSON.stringify(doc) : null;
}

function thumbUri(page) {
  const uri = String(page.thumbnail_uri ?? "");
  if (uri && !/^https?:/i.test(uri)) return uri;
  return String(page.thumbnail ?? uri).match(/\/(tos-[^/]+\/[0-9a-f]+)/)?.[1] ?? "";
}

async function copyTo(target) {
  const info = await post(target, `/v1/page_info/${SOURCE}/`, { account_id: target });
  if (info.code !== 0) throw new Error(`read source: ${info.code} ${info.msg}`);
  const page = info.data?.page_info ?? {};
  if (!page.data) throw new Error("source page definition is empty");
  const thumb = thumbUri(page);
  const rewritten = NEW_URL ? rewriteButtons(page.data, NEW_URL, NEW_TEXT) : null;
  if (NEW_URL && !rewritten) throw new Error("no button with a link found in the page");

  const created = await post(target, "/v1/create/", {
    business_type: page.business_type ?? 6, data: page.data, duplicate_id: SOURCE,
    template_id: page.template_id, title: NAME, thumbnail_uri: thumb, account_id: target,
  });
  if (created.code !== 0) throw new Error(`create: ${created.code} ${created.msg}`);
  const pageId = created.data?.page_id;
  if (!pageId) throw new Error("create returned no page_id");

  if (rewritten) {
    for (let i = 0; i < 20; i++) {
      const seen = await post(target, `/v1/page_info/${pageId}/`, { account_id: target });
      if (seen.code === 0 && seen.data?.page_info?.data) break;
      await sleep(1000);
    }
    const updated = await post(target, "/v1/update/", { data: rewritten, page_id: pageId, title: NAME, template_id: page.template_id, thumbnail_uri: thumb, account_id: target });
    if (updated.code !== 0) throw new Error(`created ${pageId} but update failed: ${updated.code} ${updated.msg}`);
    const back = await post(target, `/v1/page_info/${pageId}/`, { account_id: target });
    const blob = String(back.data?.page_info?.publish_data ?? "") + String(back.data?.page_info?.data ?? "");
    if (!blob.includes(NEW_URL)) throw new Error(`created ${pageId} but it read back WITHOUT the new link - not publishing it`);
  }

  const published = await post(target, `/v1/publish/${pageId}/`, { account_id: target });
  if (published.code !== 0) throw new Error(`created ${pageId} but publish failed: ${published.code} ${published.msg}`);
  return pageId;
}

if (!targets.length) console.log("nothing to do - every account in accounts.txt is already in done.csv");
for (const target of targets) {
  try {
    const id = await copyTo(target);
    console.log(`OK   ${target} -> page ${id}`);
    appendFileSync("done.csv", `${target},${id}\n`);
  } catch (e) {
    console.log(`FAIL ${target}: ${e.message}`);
    if (/200000|log into|non-JSON/i.test(e.message)) { console.log("Cookies are dead - refresh cookie.txt and re-run."); break; }
  }
  await sleep(1500);
}
