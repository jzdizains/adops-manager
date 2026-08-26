/* chart.js — the dashboard line chart, built to the dataviz spec:
   recessive hairline gridlines + clean y-ticks, 2px lines with round caps,
   10%-opacity area wash, >=8px end markers with a 2px surface ring, direct
   end labels, a legend with line keys, and a crosshair + single tooltip that
   reads out EVERY series at the hovered X. Vanilla JS, no deps.

   Usage: AdopsChart(el, { labels: [...], series: [{ name, values, color }] })
   All series must share one unit (one axis — never two scales). */
(function () {
  "use strict";

  function fmtMoney(v) {
    var abs = Math.abs(v);
    var s = abs >= 1000 ? (abs / 1000).toFixed(abs >= 10000 ? 0 : 1) + "K"
                        : abs.toFixed(abs >= 100 ? 0 : 2);
    return (v < 0 ? "-$" : "$") + s;
  }

  function niceTicks(min, max, count) {
    if (min === max) { max = min + 1; }
    var span = max - min;
    var step = Math.pow(10, Math.floor(Math.log10(span / count)));
    var err = span / count / step;
    if (err >= 7.5) step *= 10; else if (err >= 3.5) step *= 5; else if (err >= 1.5) step *= 2;
    var ticks = [];
    for (var v = Math.ceil(min / step) * step; v <= max + 1e-9; v += step) ticks.push(v);
    return ticks;
  }

  window.AdopsChart = function (el, data) {
    el.innerHTML = "";
    el.style.position = "relative";
    var W = 860, H = 240, PAD = { t: 14, r: 74, b: 26, l: 52 };
    var series = data.series.filter(function (s) { return s.values && s.values.length; });
    var labels = data.labels || [];
    var n = labels.length;
    if (!series.length || n < 2) {
      el.innerHTML = '<div style="padding:40px 0;text-align:center;color:var(--text-dim);">Not enough data yet — numbers appear as sweeps collect history.</div>';
      return;
    }
    var all = [];
    series.forEach(function (s) { all = all.concat(s.values); });
    var min = Math.min(0, Math.min.apply(null, all));
    var max = Math.max.apply(null, all);
    if (max === min) max = min + 1;
    var ticks = niceTicks(min, max, 4);
    min = Math.min(min, ticks[0]); max = Math.max(max, ticks[ticks.length - 1]);

    function X(i) { return PAD.l + i * (W - PAD.l - PAD.r) / (n - 1); }
    function Y(v) { return PAD.t + (1 - (v - min) / (max - min)) * (H - PAD.t - PAD.b); }

    var svgNS = "http://www.w3.org/2000/svg";
    var svg = document.createElementNS(svgNS, "svg");
    svg.setAttribute("viewBox", "0 0 " + W + " " + H);
    svg.setAttribute("width", "100%");
    svg.style.display = "block";

    function add(parent, tag, attrs, text) {
      var node = document.createElementNS(svgNS, tag);
      for (var k in attrs) node.setAttribute(k, attrs[k]);
      if (text != null) node.textContent = text;
      parent.appendChild(node);
      return node;
    }

    // gridlines + y ticks (hairline, recessive; clean numbers)
    ticks.forEach(function (t) {
      add(svg, "line", { x1: PAD.l, y1: Y(t), x2: W - PAD.r, y2: Y(t),
                         stroke: "var(--border-soft)", "stroke-width": 1 });
      add(svg, "text", { x: PAD.l - 8, y: Y(t) + 3.5, "text-anchor": "end",
                         "font-size": 11, fill: "var(--text-dim)",
                         style: "font-variant-numeric: tabular-nums;" }, fmtMoney(t));
    });
    // x labels: first, middle, last (dates don't need every tick)
    [0, Math.floor((n - 1) / 2), n - 1].forEach(function (i) {
      add(svg, "text", { x: X(i), y: H - 8, "text-anchor": i === 0 ? "start" : (i === n - 1 ? "end" : "middle"),
                         "font-size": 11, fill: "var(--text-dim)" }, labels[i].slice(5));
    });

    series.forEach(function (s) {
      var pts = s.values.map(function (v, i) { return X(i) + "," + Y(v); }).join(" ");
      // area wash at ~10% opacity
      add(svg, "polygon", { points: X(0) + "," + Y(Math.max(min, 0)) + " " + pts + " " +
                            X(n - 1) + "," + Y(Math.max(min, 0)),
                            fill: s.color, opacity: 0.08 });
      add(svg, "polyline", { points: pts, fill: "none", stroke: s.color,
                             "stroke-width": 2, "stroke-linejoin": "round",
                             "stroke-linecap": "round" });
      // end marker: r4 with a 2px surface ring, plus a direct end label
      var lx = X(n - 1), ly = Y(s.values[n - 1]);
      add(svg, "circle", { cx: lx, cy: ly, r: 6, fill: "var(--panel)" });
      add(svg, "circle", { cx: lx, cy: ly, r: 4, fill: s.color });
      add(svg, "text", { x: lx + 9, y: ly + 4, "font-size": 11.5, "font-weight": 700,
                         fill: "var(--text-soft)" }, fmtMoney(s.values[n - 1]));
    });

    // crosshair + hover dots
    var cross = add(svg, "line", { y1: PAD.t, y2: H - PAD.b, stroke: "var(--border-strong)",
                                   "stroke-width": 1, visibility: "hidden" });
    var hoverDots = series.map(function (s) {
      var g = add(svg, "g", { visibility: "hidden" });
      add(g, "circle", { r: 6, fill: "var(--panel)" });
      add(g, "circle", { r: 4, fill: s.color });
      return g;
    });

    el.appendChild(svg);

    // legend with line keys (>=2 series always gets one)
    if (series.length >= 2) {
      var legend = document.createElement("div");
      legend.style.cssText = "display:flex;gap:18px;justify-content:center;padding-top:6px;font-size:12px;font-weight:600;color:var(--text-soft);";
      series.forEach(function (s) {
        var item = document.createElement("span");
        item.style.cssText = "display:inline-flex;align-items:center;gap:6px;";
        var key = document.createElement("span");
        key.style.cssText = "width:16px;height:2px;border-radius:1px;background:" + s.color + ";";
        item.appendChild(key);
        item.appendChild(document.createTextNode(s.name));
        legend.appendChild(item);
      });
      el.appendChild(legend);
    }

    // tooltip: values lead, one readout for every series at the snapped X
    var tip = document.createElement("div");
    tip.style.cssText = "position:absolute;pointer-events:none;display:none;background:var(--panel);" +
      "border:1px solid var(--border);border-radius:8px;box-shadow:var(--shadow);" +
      "padding:8px 10px;font-size:12px;z-index:10;min-width:130px;";
    el.appendChild(tip);

    svg.addEventListener("pointermove", function (ev) {
      var rect = svg.getBoundingClientRect();
      var px = (ev.clientX - rect.left) * (W / rect.width);
      var i = Math.round((px - PAD.l) / ((W - PAD.l - PAD.r) / (n - 1)));
      i = Math.max(0, Math.min(n - 1, i));
      cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i));
      cross.setAttribute("visibility", "visible");
      tip.textContent = "";
      var head = document.createElement("div");
      head.style.cssText = "color:var(--text-dim);font-weight:600;margin-bottom:4px;";
      head.textContent = labels[i];
      tip.appendChild(head);
      series.forEach(function (s, si) {
        hoverDots[si].setAttribute("transform", "translate(" + X(i) + "," + Y(s.values[i]) + ")");
        hoverDots[si].setAttribute("visibility", "visible");
        var row = document.createElement("div");
        row.style.cssText = "display:flex;align-items:center;gap:6px;margin-top:2px;";
        var key = document.createElement("span");
        key.style.cssText = "width:12px;height:2px;border-radius:1px;flex:none;background:" + s.color + ";";
        var val = document.createElement("strong");
        val.style.cssText = "font-variant-numeric:tabular-nums;color:var(--text);";
        val.textContent = fmtMoney(s.values[i]);
        var name = document.createElement("span");
        name.style.cssText = "color:var(--text-dim);";
        name.textContent = s.name;
        row.appendChild(key); row.appendChild(val); row.appendChild(name);
        tip.appendChild(row);
      });
      var tipX = (X(i) / W) * rect.width;
      tip.style.display = "block";
      tip.style.left = Math.min(Math.max(tipX + 12, 0), rect.width - 150) + "px";
      tip.style.top = "10px";
    });
    svg.addEventListener("pointerleave", function () {
      cross.setAttribute("visibility", "hidden");
      hoverDots.forEach(function (g) { g.setAttribute("visibility", "hidden"); });
      tip.style.display = "none";
    });
  };
})();
