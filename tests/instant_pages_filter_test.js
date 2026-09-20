/* Instant Pages page logic (v132): the pure filter/coverage/drawer helpers in the SHIPPED
   app/static/instant-pages.js, run without a DOM. Proves search, favourite/tag/BC/status/
   coverage filters, the coverage math that follows the BC filter, the clone choices, and
   the accounts drawer HTML (grouped by BC, haves before misses, clone only where missing). */
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");

const fails = [];
function check(name, cond, extra) {
  console.log((cond ? "PASS " : "FAIL ") + name + (!cond && extra ? "  [" + extra + "]" : ""));
  if (!cond) fails.push(name);
}

// load the shipped file with no document → only the pure IP.* is defined
const ctx = { globalThis: {} }; ctx.globalThis = ctx; vm.createContext(ctx);
vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "app", "static", "instant-pages.js"), "utf8"), ctx);
const IP = ctx.IP;

const data = {
  total: 4,
  accounts: [
    { id: "a1", name: "Acct One", bc: "bc1" },
    { id: "a2", name: "Acct Two", bc: "bc1" },
    { id: "a3", name: "Acct Three", bc: "bc2" },
    { id: "a4", name: "Loose", bc: "" },
  ],
  bcs: [
    { bc_id: "bc1", name: "Blue Bat", n: 2 },
    { bc_id: "bc2", name: "BC 60", n: 1 },
    { bc_id: "", name: "No Business Center", n: 1 },
  ],
  groups: [
    { name: "Benjamin", count: 3, published: 2, drafts: 1, favorite: false, tags: [{ id: 8, name: "cash", color: "green" }],
      source: { page_id: "p1", adv: "a1" }, bcs: ["bc1", "bc2"], missing_by_bc: { bc1: 0, bc2: 0, "": 1 },
      copies: [{ adv: "a1", page_id: "p1", status: "PUBLISHED", preview: "https://x/1", bc: "bc1" },
               { adv: "a2", page_id: "p2", status: "PUBLISHED", preview: "", bc: "bc1" },
               { adv: "a3", page_id: "p3", status: "EDITED", preview: "", bc: "bc2" }] },
    { name: "Alpha", count: 1, published: 1, drafts: 0, favorite: true, tags: [],
      source: { page_id: "p5", adv: "a4" }, bcs: [""], missing_by_bc: { bc1: 2, bc2: 1, "": 0 },
      copies: [{ adv: "a4", page_id: "p5", status: "PUBLISHED", preview: "", bc: "" }] },
  ],
};
const idx = IP.index(data);
const G = {}; data.groups.forEach((g) => (G[g.name] = g));

console.log("-- search + filters --");
check("index builds a search string over name, account names, page ids and BC names",
  G.Benjamin._search.includes("benjamin") && G.Benjamin._search.includes("acct three") && G.Benjamin._search.includes("p2") && G.Benjamin._search.includes("blue bat"));
check("search matches account name", IP.matches(G.Benjamin, { q: "acct three" }, 4) && !IP.matches(G.Alpha, { q: "acct three" }, 4));
check("favourites only", IP.matches(G.Alpha, { fav: true }, 4) && !IP.matches(G.Benjamin, { fav: true }, 4));
check("tag chip (AND across chips)", IP.matches(G.Benjamin, { tags: ["8"] }, 4) && !IP.matches(G.Alpha, { tags: ["8"] }, 4) && !IP.matches(G.Benjamin, { tags: ["8", "7"] }, 4));
check("status: has drafts / published", IP.matches(G.Benjamin, { status: "draft" }, 4) && !IP.matches(G.Alpha, { status: "draft" }, 4));
check("coverage overall: full vs missing-somewhere", IP.matches(G.Benjamin, { cov: "partial" }, 4) && !IP.matches(G.Benjamin, { cov: "full" }, 4) && IP.matches(G.Alpha, { cov: "partial" }, 4));

console.log("-- BC filter + coverage math --");
check("BC filter keeps a page present in that BC", IP.matches(G.Benjamin, { bc: "bc1" }, 4) && !IP.matches(G.Alpha, { bc: "bc1" }, 4));
check("BC + 'missing here' surfaces a page absent from that BC (Alpha missing in bc1)", IP.matches(G.Alpha, { bc: "bc1", cov: "partial" }, 4));
check("coverage overall = total − count", IP.coverage(G.Benjamin, 4, "").missing === 1 && IP.coverage(G.Alpha, 4, "").missing === 3);
check("coverage within a BC uses missing_by_bc", IP.coverage(G.Benjamin, 4, "bc1").missing === 0 && IP.coverage(G.Alpha, 4, "bc1").missing === 2);

console.log("-- clone choices --");
const ch = IP.cloneChoices(G.Benjamin, data);
check("accounts that already hold the page are flagged has=true", ch.accounts.find((a) => a.id === "a1").has === true && ch.accounts.find((a) => a.id === "a4").has === false);
check("BC choices carry how many still lack it", ch.bcs.find((b) => b.bc_id === "bc1").missing === 0 && ch.bcs.length === 2);

console.log("-- accounts drawer --");
const html = IP.drawerHtml(G.Benjamin, data, idx);
check("grouped by BC with a name + a coverage pill", html.includes("Blue Bat") && html.includes("BC 60") && html.includes("2 of 2"));
check("a held account shows its page id, status and links; Ads Manager deep link present", html.includes("p2") && html.includes("Published") && html.includes("aadvid=a3"));
check("a missing account offers 'Clone here' (source exists)", html.includes('class="btn sm ipd-clone" data-to="a4"'));
const noSrc = IP.drawerHtml({ name: "Zeta", count: 1, copies: [{ adv: "a1", page_id: "p9", status: "EDITED", preview: "", bc: "bc1" }], source: null, missing_by_bc: {}, bcs: ["bc1"] }, data, idx);
check("no published source → no 'Clone here', says so instead", !noSrc.includes("ipd-clone") && noSrc.includes("no published copy to clone"));
check("drawer escapes account names (no raw injection)", !IP.drawerHtml({ name: "x", count: 0, copies: [], source: null, missing_by_bc: {}, bcs: [] },
  { accounts: [{ id: "a1", name: "<script>bad</script>", bc: "" }], bcs: [{ bc_id: "", name: "No Business Center", n: 1 }] }, IP.index({ accounts: [], bcs: [], groups: [] })).includes("<script>bad"));

console.log();
console.log(fails.length ? fails.length + " FAILED: " + fails.join(", ") : "ALL PASS");
process.exit(fails.length ? 1 : 0);
