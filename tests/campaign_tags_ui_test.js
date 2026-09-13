/* Campaign tags in the table: pills render escaped, group rows show the UNION of
   their campaigns' tags, and a change repaints every holder that shows an affected
   campaign — nothing else. Runs the real helpers sliced from app/static/campaigns.js. */
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");
const fails = [];
function check(name, cond, extra) { console.log((cond ? "PASS " : "FAIL ") + name + (!cond && extra ? "  [" + extra + "]" : "")); if (!cond) fails.push(name); }

const src = fs.readFileSync(path.join(__dirname, "..", "app", "static", "campaigns.js"), "utf8");
const a = src.indexOf("  var TAG_COLORS = "), b = src.indexOf("  function tagPopup(");
if (a < 0 || b < 0) { console.log("FAIL tag helpers not found"); process.exit(1); }

// DOM stub: holders are objects with dataset + innerHTML
function holder(o) { return { dataset: o, innerHTML: "", _pills: o.pills || [] }; }
const holders = [
  holder({ cid: "c1" }), holder({ cid: "c2" }), holder({ cid: "c3" }),
  holder({ cids: "c1,c2" }),            // group row containing c1 + c2
  holder({ cids: "c3" }),               // group row containing c3 only
];
const posted = [];
const ctx = {
  esc: s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;"),
  $$: (sel, root) => sel === ".ctags" ? holders : (sel === ".ctag" ? (root._pills || []) : []),
  UI: { post: (url, data) => { posted.push({ url, data }); return Promise.resolve(ctx._reply); } },
  adopsToast: () => {}, Object, Promise,
};
vm.createContext(ctx);
vm.runInContext(src.slice(a, b) + "\nthis.tagPill = tagPill; this.cidsOf = cidsOf; this.holdersTouching = holdersTouching; this.repaintTags = repaintTags; this.setTag = setTag;", ctx);

// pills
let h = ctx.tagPill({ id: 5, name: '<b>x</b> & "y"', color: "green" }, false);
check("pill escapes the name and carries the tag id", h.includes("&lt;b&gt;x&lt;/b&gt; &amp; &quot;y&quot;") && h.includes('data-tag="5"') && h.includes("ctag-green"), h);
check("group pill explains the × removes from the whole group", ctx.tagPill({ id: 1, name: "a", color: "red" }, true).includes("every campaign in this group"));
check("cidsOf: single and group holders", JSON.stringify(ctx.cidsOf(holders[0])) === '["c1"]' && JSON.stringify(ctx.cidsOf(holders[3])) === '["c1","c2"]');

// which holders repaint
let t = ctx.holdersTouching(["c1"]);
check("changing c1 touches its row and its group, not c2/c3 rows nor the other group", t.length === 2 && t.includes(holders[0]) && t.includes(holders[3]), t.length);

// union rendering
ctx.repaintTags([holders[3], holders[0], holders[2]], { c1: [{ id: 1, name: "winner", color: "green" }], c2: [{ id: 1, name: "winner", color: "green" }, { id: 2, name: "Watch", color: "amber" }], c3: [] });
check("group row shows the union once, sorted by name", (holders[3].innerHTML.match(/class="ctag /g) || []).length === 2 && holders[3].innerHTML.indexOf("Watch") < holders[3].innerHTML.indexOf("winner"), holders[3].innerHTML);
check("campaign row shows only its own", (holders[0].innerHTML.match(/class="ctag /g) || []).length === 1 && holders[0].innerHTML.includes("winner"));
check("a campaign with no tags renders empty", holders[2].innerHTML === "");

// a click on the + or a pill must never open the campaign drawer
check("row click-to-open ignores the tag controls", /openDrawer\(row\); \}/.test(src) && /closest\("input, button, a, \.previewBtn, \.tog, \.ctag, \.ctag-add, \.pop"\)/.test(src));
check("tag handlers stop the other document listeners", (src.match(/stopImmediatePropagation\(\)/g) || []).length >= 2);

// setTag posts the targets AND every campaign whose holder must repaint
(async () => {
  ctx._reply = { ok: true, tags: { c1: [{ id: 1, name: "winner", color: "green" }] } };
  const ok = await ctx.setTag(holders[0], 1, true);
  const p = posted[0];
  check("setTag posts campaign_ids + also + tag_id + on", ok === true && p.url === "/campaigns/tags" && p.data.campaign_ids === "c1" && p.data.tag_id === 1 && p.data.on === 1, JSON.stringify(p));
  check("'also' covers the group's other member so its union stays right", p.data.also.split(",").sort().join(",") === "c1,c2", p.data.also);
  check("after the reply the group row is repainted from the map (c2 has none now)", holders[3].innerHTML.includes("winner") && !holders[3].innerHTML.includes("Watch"), holders[3].innerHTML);
  ctx._reply = { ok: false, error: "nope" };
  const bad = await ctx.setTag(holders[1], 1, true);
  check("a refused change returns false (the checkbox is put back)", bad === false);
  console.log(fails.length ? `\n${fails.length} FAILED` : "\nALL PASS");
  process.exit(fails.length ? 1 : 0);
})();
