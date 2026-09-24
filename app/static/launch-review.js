/* launch-review.js — the Review step's per-account check (v151), shared by Create Campaign and
   the Super Launcher. LR.mount(el, {params(), form, onChange(state)}) → {refresh(force), state()}.
   One row per account: page / form / display card / identity / geo / start time in the account's
   own clock, and why it is blocked. "Leave out blocked accounts" writes their ids into the form's
   exclude_ids so the launch skips them. Identities of library ads are fetched afterwards, five
   accounts at a time. Depends on ui.js. */
(function () {
  "use strict";
  var esc = UI.esc;
  var PILL = { ok: "ok", warn: "warn", bad: "err", na: "dim" };
  var COLS = [["page", "Page"], ["form", "Form"], ["terms", "Lead terms"], ["card", "Card"], ["identity", "Identity"], ["geo", "Geo"]];
  var TERMS_URL = "https://ads.tiktok.com/i18n/official/policy/lead-gen-terms";

  window.LR = {
    mount: function (el, o) {
      if (!el) return null;
      var seq = 0, last = "", data = null, timer = null, leaveOut = true;
      var hidden = o.form.querySelector('input[name="exclude_ids"]');
      if (!hidden) { hidden = document.createElement("input"); hidden.type = "hidden"; hidden.name = "exclude_ids"; o.form.appendChild(hidden); }

      function verdict(r) {
        var order = ["account", "page", "form", "terms", "card", "identity", "geo"], why = "", warns = [];
        order.forEach(function (k) { var c = r.cells[k]; if (!c) return; if (c.state === "bad" && !why) why = c.hint || c.text; else if (c.state === "warn") warns.push(c.hint || c.text); });
        r.blocked = why; r.warnings = warns;
      }
      function state() {
        var rows = data ? data.rows : [], blocked = rows.filter(function (r) { return r.blocked; });
        return { rows: rows.length, blocked: blocked.length, ready: rows.length - blocked.length, leaveOut: leaveOut, excluded: leaveOut ? blocked.map(function (r) { return r.id; }) : [] };
      }
      // Share (v155.9): the blocked accounts as paste-ready text — preset, account, every check
      function presetName() {
        var el2 = o.form.querySelector('select[name="template_id"]');
        if (el2 && el2.selectedIndex >= 0 && el2.options[el2.selectedIndex]) return el2.options[el2.selectedIndex].text.trim();
        var h = o.form.querySelector('[name="template_name"]'); return h ? h.value : "";
      }
      function rowText(r) {
        var cells = Object.keys(r.cells || {}).map(function (k) { var c = r.cells[k]; return "  " + k + ": " + c.state + " — " + c.text + (c.hint ? " (" + c.hint + ")" : ""); });
        return r.name + " (" + r.id + ")" + (r.blocked ? "\n  BLOCKED: " + r.blocked : "") + (r.warnings && r.warnings.length ? "\n  notes: " + r.warnings.join(" · ") : "") +
          "\n" + cells.join("\n") + "\n  starts: " + (r.starts || "") + " " + (r.tz || "");
      }
      function shareRows(list) {
        var p = presetName();
        UI.share((p ? "Preset: " + p + "\n" : "") + list.map(rowText).join("\n\n"),
                 "LAUNCH CHECK — " + list.length + " blocked account" + (list.length === 1 ? "" : "s"));
      }
      function sync() { var st = state(); hidden.value = st.excluded.join(","); if (o.onChange) o.onChange(st); }

      function draw() {
        if (!data) return;
        var rows = data.rows;
        if (!rows.length) { el.innerHTML = '<div class="muted lr-empty">' + (o.emptyText || "No accounts to check yet.") + "</div>"; sync(); return; }
        var cols = COLS.filter(function (c) { return rows.some(function (r) { return r.cells[c[0]] && r.cells[c[0]].state !== "na"; }) || (c[0] === "identity" && rows.some(function (r) { return r.identity_pending; })) || (c[0] === "terms" && rows.some(function (r) { return r.terms_pending; })); });
        var noTerms = rows.filter(function (r) { return r.cells.terms && r.cells.terms.state === "bad"; });
        var st = state(), warned = rows.filter(function (r) { return !r.blocked && r.warnings.length; }).length;
        var missing = rows.some(function (r) { return ["page", "form"].some(function (k) { return r.cells[k] && r.cells[k].text === "missing"; }); });
        var head = '<div class="lr-top"><b>' + st.ready + " ready</b>" + (st.blocked ? ' · <span class="lr-bad">' + st.blocked + " blocked</span>" : "") + (warned ? ' · <span class="muted">' + warned + " with notes</span>" : "") +
          (missing ? '<button type="button" class="btn sm ghost lr-live" title="Ask TikTok again for the pages / forms the last sync didn\'t see">Re-check on TikTok</button>' : "") +
          (noTerms.length ? '<button type="button" class="btn sm primary lr-terms" title="Signs TikTok\'s Lead Generation Terms on these ad accounts — what Ads Manager does silently the first time a lead ad is built there">Accept Lead Generation Terms · ' + noTerms.length + " account" + (noTerms.length === 1 ? "" : "s") + '</button> <a class="muted" style="font-size:11.5px;" href="' + TERMS_URL + '" target="_blank" rel="noopener">read the terms ↗</a>' : "") +
          (st.blocked > 1 ? '<button type="button" class="btn sm ghost lr-share-all" title="Copy every blocked account and why, ready to paste">⧉ Share all ' + st.blocked + "</button>" : "") +
          (st.blocked ? '<label class="lr-leave"><input type="checkbox" class="lr-leave-cb"' + (leaveOut ? " checked" : "") + "> Leave out the " + st.blocked + " blocked account" + (st.blocked === 1 ? "" : "s") + "</label>" : "") + "</div>";
        var th = "<tr><th>Account</th>" + cols.map(function (c) { return "<th>" + c[1] + "</th>"; }).join("") + '<th title="&quot;Start now&quot; in the ad account\'s own timezone — TikTok reads the start time there">Starts</th></tr>';
        var tb = rows.map(function (r) {
          var tds = cols.map(function (c) {
            var x = r.cells[c[0]];
            if (!x) return '<td class="muted">—</td>';
            return '<td><span class="pill ' + PILL[x.state] + '" title="' + esc(x.hint || "") + '">' + esc(x.text) + "</span></td>";
          }).join("");
          var sub = r.blocked ? '<div class="lr-why">⛔ ' + esc(r.blocked) + ' <button type="button" class="btn sm ghost lr-share" data-id="' + esc(r.id) + '" title="Copy this account\'s check, ready to paste">⧉ Share</button></div>' : (r.warnings.length ? '<div class="lr-note">' + esc(r.warnings.join(" · ")) + "</div>" : "");
          return '<tr class="' + (r.blocked ? "lr-blocked" + (leaveOut ? " lr-out" : "") : "") + '"><td><b>' + esc(r.name) + '</b> <span class="mono muted lr-id">' + esc(r.id) + "</span>" + sub + "</td>" + tds +
            '<td class="muted lr-tz">' + (r.starts ? esc(r.starts) + " " : "") + esc(r.tz) + "</td></tr>";
        }).join("");
        el.innerHTML = head + '<div class="table-scroll lr-scroll"><table class="data dense lr-t"><thead>' + th + "</thead><tbody>" + tb + "</tbody></table></div>";
        sync();
      }

      function identities(my) {
        var todo = data.rows.filter(function (r) { return r.identity_pending; }).map(function (r) { return r.id; });
        function next() {
          if (my !== seq || !todo.length) return;
          var chunk = todo.splice(0, 5);
          function give(cells) {
            data.rows.forEach(function (r) {
              if (chunk.indexOf(r.id) < 0) return;
              r.cells.identity = (cells && cells[r.id]) || { state: "warn", text: "not checked", hint: "couldn't read its identities — checked at launch" };
              r.identity_pending = false; verdict(r);
            });
            draw(); next();          // one failed chunk never leaves the rest on "checking…"
          }
          UI.post("/campaigns/review/identities.json", { advertiser_ids: chunk.join(",") }).then(function (d) {
            if (my !== seq) return;
            give(d.ok ? d.cells : null);
          }).catch(function () { if (my === seq) give(null); });
        }
        next();
      }

      // TikTok's Lead Generation Terms (v155.13): read lazily, five accounts at a time
      function terms(my, ids) {
        var todo = ids || data.rows.filter(function (r) { return r.terms_pending; }).map(function (r) { return r.id; });
        function next() {
          if (my !== seq || !todo.length) return;
          var chunk = todo.splice(0, 5);
          function give(cells) {
            data.rows.forEach(function (r) {
              if (chunk.indexOf(r.id) < 0) return;
              r.cells.terms = (cells && cells[r.id]) || { state: "warn", text: "not checked", hint: "couldn't read it — the launch will try" };
              r.terms_pending = false; verdict(r);
            });
            draw(); next();
          }
          UI.post("/campaigns/review/lead-terms.json", { advertiser_ids: chunk.join(",") }).then(function (d) { if (my === seq) give(d.ok ? d.cells : null); })
            .catch(function () { if (my === seq) give(null); });
        }
        next();
      }
      function acceptTerms(btn) {
        var ids = data.rows.filter(function (r) { return r.cells.terms && r.cells.terms.state === "bad"; }).map(function (r) { return r.id; });
        if (!ids.length) return;
        UI.confirm({ title: "Accept TikTok's Lead Generation Terms on " + ids.length + " ad account" + (ids.length === 1 ? "" : "s") + "?",
                     text: "This signs TikTok's Lead Generation Terms (" + TERMS_URL + ") for these ad accounts, as your TikTok login — the same thing Ads Manager does silently the first time a lead ad is built there. Instant Form ads can run on them afterwards.",
                     ok: "Accept on " + ids.length })
          .then(function (yes) {
            if (!yes) return;
            btn.disabled = true; btn.textContent = "Accepting…";
            UI.post("/campaigns/lead-terms/accept", { advertiser_ids: ids.join(",") }).then(function (d) {
              if (window.adopsToast) adopsToast(d.ok || d.queued ? "ok" : "err", d.msg || d.error || "Done.");
              if (d.queued) { btn.textContent = "Accepting in the background…"; return; }
              data.rows.forEach(function (r) { if (ids.indexOf(r.id) >= 0) { r.cells.terms = { state: "na", text: "checking…" }; r.terms_pending = true; } });
              draw(); terms(seq, ids.slice());
            }, function () { btn.disabled = false; if (window.adopsToast) adopsToast("err", "Couldn't reach the dashboard."); });
          });
      }
      document.addEventListener("adops:job", function (e) {
        var j = e.detail || {};
        if (j.kind === "lead_terms_accept" && data) {
          data.rows.forEach(function (r) { if (r.cells.terms && r.cells.terms.state === "bad") { r.cells.terms = { state: "na", text: "checking…" }; r.terms_pending = true; } });
          draw(); terms(seq);
        }
      });

      function load(live) {
        var p = o.params();
        if (!p) { data = { rows: [] }; draw(); return; }
        var key = JSON.stringify(p);
        if (!live && key === last && data) return;
        last = key; var my = ++seq;
        if (live) p.live = "1";
        el.classList.add("lr-loading");
        if (!data || !data.rows.length) el.innerHTML = '<div class="muted lr-empty">Checking each account…</div>';
        UI.post("/campaigns/review.json", p).then(function (d) {
          if (my !== seq) return;
          el.classList.remove("lr-loading");
          if (!d.ok) { data = null; el.innerHTML = '<div class="muted lr-empty">' + esc(d.error || "Couldn't check the accounts.") + "</div>"; hidden.value = ""; if (o.onChange) o.onChange(state()); return; }
          data = d; draw(); identities(my); terms(my);
        }).catch(function () { if (my === seq) { el.classList.remove("lr-loading"); el.innerHTML = '<div class="muted lr-empty">Couldn\'t check the accounts — the launch still checks each one.</div>'; } });
      }

      el.addEventListener("click", function (e) {
        if (e.target.closest(".lr-live")) { e.target.closest(".lr-live").disabled = true; load(true); }
        var sb = e.target.closest(".lr-share");
        if (sb && data) { e.preventDefault(); shareRows(data.rows.filter(function (r) { return String(r.id) === sb.dataset.id; })); }
        if (e.target.closest(".lr-terms") && data) { e.preventDefault(); acceptTerms(e.target.closest(".lr-terms")); }
        if (e.target.closest(".lr-share-all") && data) { e.preventDefault(); shareRows(data.rows.filter(function (r) { return r.blocked; })); }
      });
      el.addEventListener("change", function (e) {
        if (e.target.classList.contains("lr-leave-cb")) { leaveOut = e.target.checked; draw(); }
      });
      return {
        refresh: function (force) { if (force) last = ""; clearTimeout(timer); timer = setTimeout(function () { load(false); }, force ? 0 : 250); },
        state: state
      };
    }
  };
})();
