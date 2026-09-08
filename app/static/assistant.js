/* assistant.js — chat page: renders the assistant's blocks (text with light markdown incl. tables,
   tool activity lines, approval cards), sends turns, applies approved actions through the same
   JSON endpoints the Campaigns console uses (status / edit / bids). */
(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); }, $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var esc = UI.esc, money = UI.money;
  var log = $("#asLog"), chatId = log.dataset.chat, form = $("#asForm"), ta = $("#asText"), sendBtn = $("#asSend");

  // ---- light markdown: paragraphs, **bold**, `code`, bullets, headings, pipe tables --------------
  function inline(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`([^`]+)`/g, '<span class="mono">$1</span>'); }
  function md(text) {
    var lines = String(text).replace(/\r/g, "").split("\n"), out = [], i = 0;
    while (i < lines.length) {
      var l = lines[i];
      if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        var head = l.trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); }), rows = []; i += 2;
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) { rows.push(lines[i].trim().replace(/^\||\|$/g, "").split("|").map(function (c) { return c.trim(); })); i++; }
        var num = function (c) { return /^[-+−]?[$€£]?[\d,.]+(%|×|x)?$/.test(c.replace(/\s/g, "")); };
        var t = '<div class="as-table"><table class="data dense"><thead><tr>' + head.map(function (h, j) { return "<th" + (rows.length && rows.every(function (r) { return !r[j] || num(r[j]); }) ? ' class="num"' : "") + ">" + inline(h) + "</th>"; }).join("") + "</tr></thead><tbody>" +
          rows.map(function (r) { return "<tr>" + r.map(function (c, j) { var n = num(c); var cls = n ? ' class="num"' : ""; var v = inline(c); if (n && /^[+−-]/.test(c) && /\$/.test(c)) v = '<span class="profit-cell ' + (c.charAt(0) === "+" ? "pos" : "neg") + '">' + v + "</span>"; return "<td" + cls + ">" + v + "</td>"; }).join("") + "</tr>"; }).join("") + "</tbody></table>" +
          '<div class="as-table-f"><button type="button" class="btn sm ghost as-csv">⇓ CSV</button><button type="button" class="btn sm ghost as-copy">Copy</button></div></div>';
        out.push(t); continue;
      }
      if (/^\s*([-*•]|\d+\.)\s+/.test(l)) {
        var items = [];
        while (i < lines.length && /^\s*([-*•]|\d+\.)\s+/.test(lines[i])) { items.push("<li>" + inline(lines[i].replace(/^\s*([-*•]|\d+\.)\s+/, "")) + "</li>"); i++; }
        out.push("<ul>" + items.join("") + "</ul>"); continue;
      }
      if (/^#{1,3}\s+/.test(l)) { out.push('<div class="as-h">' + inline(l.replace(/^#{1,3}\s+/, "")) + "</div>"); i++; continue; }
      if (!l.trim()) { i++; continue; }
      var para = [l]; i++;
      while (i < lines.length && lines[i].trim() && !/^\s*([-*•]|\d+\.)\s+/.test(lines[i]) && !/^\s*\|/.test(lines[i]) && !/^#{1,3}\s+/.test(lines[i])) { para.push(lines[i]); i++; }
      out.push("<p>" + inline(para.join(" ")) + "</p>");
    }
    return out.join("");
  }

  var TOOL_LABEL = { overview: "Read totals", pnl: "Read P&L", campaigns: "Read campaigns", accounts: "Read accounts", creatives: "Read creatives", inbox: "Read inbox", hourly: "Read hourly delivery", propose_actions: "Proposed changes" };
  function argsText(a) { return Object.keys(a || {}).filter(function (k) { return k !== "actions"; }).map(function (k) { var v = a[k]; return k + "=" + (typeof v === "object" ? JSON.stringify(v) : v); }).join(" · "); }

  function actionCard(b) {
    var rows = b.actions.map(function (a, i) {
      var what = a.type === "pause" ? "Pause" : a.type === "resume" ? "Resume" : a.type === "budget" ? "Budget " + money(a.value, 2) + " / ad group" : "Cost cap " + money(a.value, 2);
      return '<label class="as-act"><input type="checkbox" checked data-i="' + i + '"><span style="flex:1;min-width:0;"><b>' + esc(a.name) + '</b><span class="muted" style="display:block;font-size:11px;">' + esc(a.account) + " · " + money(a.spend, 0) + " spent today" + (a.status === "ENABLE" ? " · live" : " · paused") + (a.why ? " · " + esc(a.why) : "") + '</span></span><span class="pill ' + (a.type === "pause" ? "warn" : "ok") + '">' + what + "</span></label>";
    }).join("");
    return '<div class="as-card as-approve" data-actions=\'' + esc(JSON.stringify(b.actions)) + '\'><div class="as-card-h">🛡 Needs your OK — ' + esc(b.title) + (b.stale ? ' <span class="muted" style="font-weight:500;font-size:11px;">(earlier in this chat)</span>' : "") + "</div>" + rows +
      (b.rejected && b.rejected.length ? '<div class="muted" style="font-size:11px;margin-top:4px;">Skipped: ' + b.rejected.map(function (r) { return esc(r.campaign_id) + " (" + esc(r.error) + ")"; }).join(", ") + "</div>" : "") +
      '<div style="display:flex;gap:8px;margin-top:10px;"><button type="button" class="btn sm primary as-apply">Apply selected</button><button type="button" class="btn sm as-skip">Skip</button><span class="muted as-res" style="font-size:11.5px;align-self:center;"></span></div></div>';
  }

  function block(b) {
    if (b.type === "me") return '<div class="as-me">' + esc(b.text) + "</div>";
    if (b.type === "text") return '<div class="as-ai"><span class="as-av">✦</span><div class="as-bubble">' + md(b.text) + "</div></div>";
    if (b.type === "tool") return '<div class="as-tool">' + esc(TOOL_LABEL[b.name] || b.name) + (argsText(b.args) ? ' <span class="muted">' + esc(argsText(b.args)) + "</span>" : "") + "</div>";
    if (b.type === "actions") return '<div class="as-ai"><span class="as-av">✦</span><div style="flex:1;min-width:0;">' + actionCard(b) + "</div></div>";
    if (b.type === "error") return '<div class="as-ai"><span class="as-av">✦</span><div class="as-bubble" style="color:var(--err);">' + esc(b.text) + "</div></div>";
    return "";
  }
  function append(html) { if (!html) return; var e = $(".as-empty", log); if (e) e.remove(); log.insertAdjacentHTML("beforeend", html); log.scrollTop = log.scrollHeight; }
  function renderAll(blocks) { append(blocks.map(block).join("")); }
  try { renderAll(JSON.parse($("#asBlocks").textContent || "[]")); } catch (e) {}

  // ---- send ------------------------------------------------------------------------------------
  var busy = false;
  function send(text) {
    text = (text || ta.value).trim(); if (!text || busy || !chatId) return;
    busy = true; sendBtn.disabled = true; ta.value = ""; ta.style.height = "";
    append(block({ type: "me", text: text }) + '<div class="as-tool as-thinking" id="asThinking">Thinking…</div>');
    UI.post("/assistant/" + chatId + "/send", { text: text }).then(function (d) {
      var th = $("#asThinking"); if (th) th.remove();
      if (!d.ok) { append(block({ type: "error", text: d.error || "Something went wrong." })); }
      else { renderAll(d.blocks || []); if (d.title) { var t = $('.as-chat[data-id="' + chatId + '"] .as-chat-t'); if (t) t.textContent = d.title; } }
    }).catch(function () { var th = $("#asThinking"); if (th) th.remove(); append(block({ type: "error", text: "Network error — try again." })); })
      .then(function () { busy = false; sendBtn.disabled = false; ta.focus(); });
  }
  form.addEventListener("submit", function (e) { e.preventDefault(); send(); });
  ta.addEventListener("keydown", function (e) { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } });
  ta.addEventListener("input", function () { ta.style.height = "auto"; ta.style.height = Math.min(ta.scrollHeight, 160) + "px"; });
  document.addEventListener("click", function (e) {
    var s = e.target.closest("[data-sugg]"); if (s) { if (!chatId) { document.querySelector('form[action="/assistant/new"]').submit(); return; } send(s.textContent); }
  });

  // ---- approval cards → the console's endpoints ------------------------------------------------
  document.addEventListener("click", function (e) {
    var card = e.target.closest(".as-approve"); if (!card) return;
    if (e.target.closest(".as-skip")) { card.querySelectorAll("input, button").forEach(function (x) { x.disabled = true; }); $(".as-res", card).textContent = "skipped"; return; }
    var ap = e.target.closest(".as-apply"); if (!ap) return;
    var acts = JSON.parse(card.dataset.actions), picked = $$("input[type=checkbox]:checked", card).map(function (c) { return acts[+c.dataset.i]; });
    if (!picked.length) return;
    UI.confirm({ title: "Apply " + picked.length + " change" + (picked.length > 1 ? "s" : "") + " on TikTok?", text: picked.map(function (a) { return (a.type === "pause" ? "Pause " : a.type === "resume" ? "Resume " : a.type === "budget" ? "Budget " + money(a.value, 2) + " → " : "Cost cap " + money(a.value, 2) + " → ") + a.name; }).join("\n"), ok: "Apply" }).then(function (y) {
      if (!y) return;
      ap.disabled = true; $(".as-res", card).textContent = "applying…";
      Promise.all(picked.map(function (a) {
        var base = "/campaigns/" + a.advertiser_id + "/" + a.campaign_id;
        if (a.type === "pause" || a.type === "resume") return UI.post(base + "/status", { operation_status: a.type === "pause" ? "DISABLE" : "ENABLE" }).then(function (d) { return !!d.ok; });
        if (a.type === "budget") return UI.post(base + "/edit", { adgroup_budget_all: a.value.toFixed(2) }).then(function (d) { return !!d.ok; });
        return UI.post(base + "/bids", { cap: a.value.toFixed(2) }).then(function (d) { return !!(d.queued || d.ok); });
      })).then(function (rs) {
        var ok = rs.filter(Boolean).length;
        $(".as-res", card).textContent = ok + " of " + picked.length + " applied" + (ok < picked.length ? " — see Inbox" : "");
        card.querySelectorAll("input").forEach(function (x) { x.disabled = true; });
        adopsToast && adopsToast(ok ? "ok" : "err", ok + " of " + picked.length + " change" + (picked.length > 1 ? "s" : "") + " applied");
      });
    });
  });

  // ---- table helpers + chat delete -----------------------------------------------------------
  document.addEventListener("click", function (e) {
    var t = e.target.closest(".as-table"); if (!t) return;
    var rows = $$("tr", t).map(function (r) { return $$("th, td", r).map(function (c) { return c.textContent.trim(); }); });
    if (e.target.closest(".as-csv")) { var csv = rows.map(function (r) { return r.map(function (c) { return '"' + c.replace(/"/g, '""') + '"'; }).join(","); }).join("\n"); var a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = "assistant-table.csv"; document.body.appendChild(a); a.click(); setTimeout(function () { URL.revokeObjectURL(a.href); a.remove(); }, 500); }
    if (e.target.closest(".as-copy") && navigator.clipboard) { navigator.clipboard.writeText(rows.map(function (r) { return r.join("\t"); }).join("\n")); adopsToast && adopsToast("ok", "Copied"); }
  });
  document.addEventListener("click", function (e) {
    var d = e.target.closest(".as-del"); if (!d) return; e.preventDefault(); e.stopPropagation();
    var a = d.closest(".as-chat");
    UI.confirm({ title: "Delete this chat?", ok: "Delete", danger: true }).then(function (y) { if (y) UI.post("/assistant/" + a.dataset.id + "/delete").then(function () { location.href = "/assistant"; }); });
  });
})();
