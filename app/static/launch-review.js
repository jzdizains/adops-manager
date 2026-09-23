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
  var COLS = [["page", "Page"], ["form", "Form"], ["card", "Card"], ["identity", "Identity"], ["geo", "Geo"]];

  window.LR = {
    mount: function (el, o) {
      if (!el) return null;
      var seq = 0, last = "", data = null, timer = null, leaveOut = true;
      var hidden = o.form.querySelector('input[name="exclude_ids"]');
      if (!hidden) { hidden = document.createElement("input"); hidden.type = "hidden"; hidden.name = "exclude_ids"; o.form.appendChild(hidden); }

      function verdict(r) {
        var order = ["account", "page", "form", "card", "identity", "geo"], why = "", warns = [];
        order.forEach(function (k) { var c = r.cells[k]; if (!c) return; if (c.state === "bad" && !why) why = c.hint || c.text; else if (c.state === "warn") warns.push(c.hint || c.text); });
        r.blocked = why; r.warnings = warns;
      }
      function state() {
        var rows = data ? data.rows : [], blocked = rows.filter(function (r) { return r.blocked; });
        return { rows: rows.length, blocked: blocked.length, ready: rows.length - blocked.length, leaveOut: leaveOut, excluded: leaveOut ? blocked.map(function (r) { return r.id; }) : [] };
      }
      function sync() { var st = state(); hidden.value = st.excluded.join(","); if (o.onChange) o.onChange(st); }

      function draw() {
        if (!data) return;
        var rows = data.rows;
        if (!rows.length) { el.innerHTML = '<div class="muted lr-empty">' + (o.emptyText || "No accounts to check yet.") + "</div>"; sync(); return; }
        var cols = COLS.filter(function (c) { return rows.some(function (r) { return r.cells[c[0]] && r.cells[c[0]].state !== "na"; }) || (c[0] === "identity" && rows.some(function (r) { return r.identity_pending; })); });
        var st = state(), warned = rows.filter(function (r) { return !r.blocked && r.warnings.length; }).length;
        var missing = rows.some(function (r) { return ["page", "form"].some(function (k) { return r.cells[k] && r.cells[k].text === "missing"; }); });
        var head = '<div class="lr-top"><b>' + st.ready + " ready</b>" + (st.blocked ? ' · <span class="lr-bad">' + st.blocked + " blocked</span>" : "") + (warned ? ' · <span class="muted">' + warned + " with notes</span>" : "") +
          (missing ? '<button type="button" class="btn sm ghost lr-live" title="Ask TikTok again for the pages / forms the last sync didn\'t see">Re-check on TikTok</button>' : "") +
          (st.blocked ? '<label class="lr-leave"><input type="checkbox" class="lr-leave-cb"' + (leaveOut ? " checked" : "") + "> Leave out the " + st.blocked + " blocked account" + (st.blocked === 1 ? "" : "s") + "</label>" : "") + "</div>";
        var th = "<tr><th>Account</th>" + cols.map(function (c) { return "<th>" + c[1] + "</th>"; }).join("") + '<th title="&quot;Start now&quot; in the ad account\'s own timezone — TikTok reads the start time there">Starts</th></tr>';
        var tb = rows.map(function (r) {
          var tds = cols.map(function (c) {
            var x = r.cells[c[0]];
            if (!x) return '<td class="muted">—</td>';
            return '<td><span class="pill ' + PILL[x.state] + '" title="' + esc(x.hint || "") + '">' + esc(x.text) + "</span></td>";
          }).join("");
          var sub = r.blocked ? '<div class="lr-why">⛔ ' + esc(r.blocked) + "</div>" : (r.warnings.length ? '<div class="lr-note">' + esc(r.warnings.join(" · ")) + "</div>" : "");
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
          data = d; draw(); identities(my);
        }).catch(function () { if (my === seq) { el.classList.remove("lr-loading"); el.innerHTML = '<div class="muted lr-empty">Couldn\'t check the accounts — the launch still checks each one.</div>'; } });
      }

      el.addEventListener("click", function (e) {
        if (e.target.closest(".lr-live")) { e.target.closest(".lr-live").disabled = true; load(true); }
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
