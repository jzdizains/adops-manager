/* Campaign drawer: what each ad group shows for a TikTok rejection and when the
   one-click Appeal button appears. Runs the real appealPill/appealBlock from
   app/static/campaigns.js (sliced out of the module) with an esc() stub. */
"use strict";
const fs = require("fs"), path = require("path"), vm = require("vm");
const fails = [];
function check(name, cond, extra) { console.log((cond ? "PASS " : "FAIL ") + name + (!cond && extra ? "  [" + extra + "]" : "")); if (!cond) fails.push(name); }

const src = fs.readFileSync(path.join(__dirname, "..", "app", "static", "campaigns.js"), "utf8");
const a = src.indexOf("  function appealPill("), b = src.indexOf("  function pollAppeal(");
if (a < 0 || b < 0) { console.log("FAIL appealPill/appealBlock not found"); process.exit(1); }
const ctx = { esc: s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;") };
vm.createContext(ctx);
vm.runInContext(src.slice(a, b) + "\nthis.appealPill = appealPill; this.appealBlock = appealBlock;", ctx);
const { appealBlock } = ctx;

check("healthy ad group → nothing rendered", appealBlock({ appeal: null, rejected_live: false }) === "");
let h = appealBlock({ appeal: { id: 1, status: "pending", label: "Not appealed yet", reasons: "Prohibited content", suggestion: "Edit the ad", can_appeal: true } });
check("pending: red pill + reasons + Appeal button", /pill err/.test(h) && h.includes("rejected by TikTok") && h.includes("Prohibited content — Edit the ad") && /dw-ag-appeal-btn[^>]*>Appeal<\/button>/.test(h), h);
h = appealBlock({ appeal: { id: 1, status: "appealing", filed_ago: "3 h ago", can_appeal: false } });
check("appealing: amber pill, no button", /pill warn/.test(h) && h.includes("appeal filed 3 h ago · waiting for TikTok") && !h.includes("dw-ag-appeal-btn"), h);
h = appealBlock({ appeal: { id: 1, status: "failed", label: "Appeal rejected", can_appeal: false } });
check("failed: red pill 'appeal rejected by TikTok', no button (one appeal per rejection)", h.includes("appeal rejected by TikTok") && !h.includes("dw-ag-appeal-btn"), h);
h = appealBlock({ appeal: { id: 1, status: "successful", label: "Appeal accepted", can_appeal: false } });
check("successful: green pill with the label", /pill ok/.test(h) && h.includes("Appeal accepted"), h);
h = appealBlock({ appeal: { id: 1, status: "error", error: "code 40001: no permission", can_appeal: true } });
check("error: shows the last attempt and offers 'Appeal again'", h.includes("last attempt: code 40001") && h.includes(">Appeal again</button>"), h);
h = appealBlock({ appeal: null, rejected_live: true });
check("rejected on TikTok but not scanned yet: red pill + Appeal button (endpoint tracks it on demand)", h.includes("rejected by TikTok") && h.includes(">Appeal</button>"), h);
h = appealBlock({ appeal: { id: 1, status: "pending", reasons: '<img src=x onerror="1">', can_appeal: true } });
check("reasons are escaped", !h.includes("<img") && h.includes("&lt;img"), h);

console.log(fails.length ? `\n${fails.length} FAILED` : "\nALL PASS");
process.exit(fails.length ? 1 : 0);
