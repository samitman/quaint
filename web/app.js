"use strict";
/* QUAINT: search, company breakdowns, charts and compare on top of the screener's database.
   Data: data/index.json (every company's latest screen numbers) + data/c_<letter>.json (history).
   When served by `python3 -m screener app`, api/ lets it look up any ticker and fetch live headlines. */

const $ = (s, el = document) => el.querySelector(s), $$ = (s, el = document) => [...el.querySelectorAll(s)];
const NS = "http://www.w3.org/2000/svg";
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const isNum = v => typeof v === "number" && isFinite(v);
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const DAY = 864e5;
const store = {
  get(k, d) { try { const v = localStorage.getItem("qd:" + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
  set(k, v) { try { localStorage.setItem("qd:" + k, JSON.stringify(v)); } catch (e) {} },
};

// ---------- formatting ----------
const F = {
  usd(v, d) { if (!isNum(v)) return "–"; const a = Math.abs(v), s = v < 0 ? "−$" : "$";
    if (a >= 1e12) return s + (a / 1e12).toFixed(d ?? 2) + "T"; if (a >= 1e9) return s + (a / 1e9).toFixed(d ?? 2) + "B";
    if (a >= 1e6) return s + (a / 1e6).toFixed(d ?? 0) + "M"; return s + (a / 1e3).toFixed(d ?? 0) + "K"; },
  px: v => isNum(v) ? "$" + (v >= 1000 ? v.toFixed(0) : v.toFixed(2)) : "–",
  pct: (v, d = 1) => isNum(v) ? (v * 100).toFixed(d) + "%" : "–",
  spct: (v, d = 1) => isNum(v) ? (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v * 100).toFixed(d) + "%" : "–",
  x: (v, d = 1) => isNum(v) ? v.toFixed(d) + "×" : "–",
  n: (v, d = 2) => isNum(v) ? v.toFixed(d) : "–",
  ps: (v, d = 2) => isNum(v) ? (v < 0 ? "−$" : "$") + Math.abs(v).toFixed(d) : "–",
  sh: v => isNum(v) ? (v / 1e6).toFixed(v >= 1e9 ? 0 : 1) + "M" : "–",
};
const cls = (v, s) => isNum(v) ? `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${s}</span>` : s;
const ymd = ms => new Date(ms).toISOString().slice(0, 10);
const mon = ms => new Date(ms).toLocaleDateString("en-US", {month: "short", year: "numeric", timeZone: "UTC"});
const median = a => { const b = a.filter(isNum).sort((x, y) => x - y); if (!b.length) return null; const m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; };

// ---------- tooltip ----------
const tip = $("#tip");
function showTip(html, ev) { tip.innerHTML = html; tip.style.display = "block";
  const w = tip.offsetWidth, h = tip.offsetHeight; let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14; if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  tip.style.left = Math.max(4, x) + "px"; tip.style.top = Math.max(4, y) + "px"; }
const hideTip = () => { tip.style.display = "none"; };

// ---------- data ----------
const D = {idx: {}, rows: [], asof: "", news: {}, chunks: {}, cos: {}, server: false, db: false};
const chunkOf = t => /^[A-Za-z]/.test(t) ? t[0].toUpperCase() : "0";
async function getJSON(u) { const r = await fetch(u); if (!r.ok) throw new Error(r.status + " " + u); return r.json(); }
async function loadCompany(t) {
  t = t.toUpperCase();
  if (D.cos[t]) return D.cos[t];
  let c = null;
  if (!D.db) {  // static export: one file per first letter
    const k = chunkOf(t);
    if (!D.chunks[k]) D.chunks[k] = getJSON(`data/c_${k}.json`).catch(() => ({}));
    c = (await D.chunks[k])[t];
  }
  if (!c && D.server) c = await getJSON(`api/company/${encodeURIComponent(t)}`).catch(() => null);
  if (c && c.row && !D.idx[t]) { D.idx[t] = c.row; }
  return (D.cos[t] = c ? derive(c) : null);
}

// Turn exported arrays into series the views use.
function derive(c) {
  const out = {t: c.t, ins: c.ins || [], raw: c};
  const px = c.px || {};
  if (px.d0) { const d0 = Date.parse(px.d0); out.price = px.dd.map((d, i) => [d0 + d * DAY, px.p[i]]).filter(p => isNum(p[1])); }
  else out.price = [];
  out.close = px.close ?? (out.price.length ? out.price[out.price.length - 1][1] : null);
  out.adv = px.adv;
  const q = c.q;
  if (q && q.d) {
    const n = q.d.length, Q = [];
    for (let i = 0; i < n; i++) { const r = {d: Date.parse(q.d[i])}; for (const k in q) if (k !== "d") r[k] = q[k][i]; Q.push(r); }
    for (let i = 0; i < n; i++) { const r = Q[i], p = Q[i - 4];
      r.eps = isNum(r.net_income) && r.shares ? r.net_income / r.shares : null;
      r.fcfps = isNum(r.fcf) && r.shares ? r.fcf / r.shares : null;
      r.rps = isNum(r.revenue) && r.shares ? r.revenue / r.shares : null;
      r.gm = div(r.gross_profit, r.revenue); r.om = div(r.ebit, r.revenue); r.nm = div(r.net_income, r.revenue); r.fm = div(r.fcf, r.revenue);
      r.rg = p && isNum(r.revenue) && isNum(p.revenue) && p.revenue > 0 ? r.revenue / p.revenue - 1 : null;
      r.epsg = p && isNum(r.eps) && isNum(p.eps) && p.eps > 0 ? r.eps / p.eps - 1 : null;
      r.roe = r.equity > 0 ? div(r.net_income, r.equity) : null;
      const ic = (r.equity || 0) + (r.debt || 0) - (r.cash || 0);
      r.roic = isNum(r.ebit) && ic > 0 ? r.ebit * 0.79 / ic : null;
      r.nd = isNum(r.debt) ? r.debt - (r.cash || 0) : null;
      const ebitda = isNum(r.ebit) ? r.ebit + Math.abs(r.dna || 0) : null;
      r.nde = ebitda > 0 && isNum(r.nd) ? r.nd / ebitda : null;
      r.cov = isNum(r.ebit) && r.interest_expense > 0 ? r.ebit / Math.abs(r.interest_expense) : null; }
    out.Q = Q.filter(r => isNum(r.revenue) || isNum(r.net_income));
  } else out.Q = [];
  // Valuation through time: each price point against the last quarter public ~45 days after it closed.
  const Qp = out.Q.filter(r => r.shares > 0);
  out.val = [];
  let j = -1;
  for (const [t, p] of out.price) {
    while (j + 1 < Qp.length && Qp[j + 1].d + 45 * DAY <= t) j++;
    if (j < 0) continue;
    const r = Qp[j], mc = p * r.shares, ev = mc + (r.nd ?? 0);
    out.val.push({t, pe: r.net_income > 0 ? mc / r.net_income : null, ps: r.revenue > 0 ? mc / r.revenue : null,
      evebit: r.ebit > 0 && ev > 0 ? ev / r.ebit : null, fy: isNum(r.fcf) ? r.fcf / mc : null, mc});
  }
  const L = out.Q[out.Q.length - 1];
  out.last = L || {};
  const row = D.idx[c.t] || c.row;
  out.row = row || null;
  // The screen's live price can be newer than the stored history: put it on the chart's end.
  const ta = Date.parse(D.asof || ""), lp = out.price[out.price.length - 1];
  if (row && isNum(row.px) && isNum(ta) && lp && ta > lp[0]) { out.price.push([ta, row.px]); out.close = row.px; }
  out.fin = row ? row.fin : false;
  out.mcap = row && isNum(row.mc) ? row.mc : (L && L.shares && out.close ? L.shares * out.close : null);
  return out;
}
const div = (a, b) => isNum(a) && isNum(b) && b !== 0 ? a / b : null;
function priceAt(price, t) { let lo = 0, hi = price.length - 1; if (hi < 0) return null;
  while (lo < hi) { const m = (lo + hi + 1) >> 1; if (price[m][0] <= t) lo = m; else hi = m - 1; } return price[lo][0] <= t ? price[lo][1] : null; }
function changeOver(price, days) { if (price.length < 2) return null; const [t, p] = price[price.length - 1];
  const b = priceAt(price, t - days * DAY); return b ? p / b - 1 : null; }

// ---------- charts ----------
function niceTicks(lo, hi, n = 5) { const span = hi - lo || Math.abs(hi) || 1, raw = span / n, mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(12)); return out; }
function logTicks(lo, hi) { const out = []; for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++)
  for (const m of [1, 2, 5]) { const v = m * 10 ** e; if (v >= lo && v <= hi) out.push(v); } return out.length >= 2 ? out : niceTicks(lo, hi, 4); }
function timeTicks(x0, x1, maxN) { const span = (x1 - x0) / DAY, out = [];
  if (span < 80) {  // short ranges (a new track record): day ticks
    const k = [1, 2, 7, 14].find(k => span / k <= maxN) || 28, d = new Date(x0); d.setUTCHours(0, 0, 0, 0);
    if (d.getTime() < x0) d.setUTCDate(d.getUTCDate() + 1);
    for (; d.getTime() <= x1; d.setUTCDate(d.getUTCDate() + k)) out.push(d.getTime());
    return {ticks: out, fmt: t => new Date(t).toLocaleDateString("en-US", {month: "short", day: "numeric", timeZone: "UTC"})}; }
  const steps = [[1, "m"], [2, "m"], [3, "m"], [6, "m"], [12, "m"], [24, "m"], [60, "m"]];
  const months = span / 30.4; const st = steps.find(([k]) => months / k <= maxN) || [120, "m"];
  const d = new Date(x0); d.setUTCDate(1); d.setUTCHours(0, 0, 0, 0);
  if (st[0] >= 12) { d.setUTCMonth(0); d.setUTCFullYear(d.getUTCFullYear() + 1); } else d.setUTCMonth(d.getUTCMonth() + 1);
  while (st[0] >= 12 && d.getUTCFullYear() % (st[0] / 12)) d.setUTCFullYear(d.getUTCFullYear() + 1);
  while (st[0] < 12 && st[0] > 1 && d.getUTCMonth() % st[0]) d.setUTCMonth(d.getUTCMonth() + 1);
  for (; d.getTime() <= x1; st[0] >= 12 ? d.setUTCFullYear(d.getUTCFullYear() + st[0] / 12) : d.setUTCMonth(d.getUTCMonth() + st[0])) out.push(d.getTime());
  return {ticks: out, fmt: st[0] >= 12 ? (t => String(new Date(t).getUTCFullYear())) : (t => new Date(t).toLocaleDateString("en-US", {month: "short", year: "2-digit", timeZone: "UTC"}))}; }
function svgEl(tag, a, p) { const e = document.createElementNS(NS, tag); for (const k in a) e.setAttribute(k, a[k]); if (p) p.appendChild(e); return e; }
function nearest(pts, t) { let lo = 0, hi = pts.length - 1; if (hi < 0) return -1;
  while (lo < hi) { const m = (lo + hi) >> 1; if (pts[m][0] < t) lo = m + 1; else hi = m; }
  return lo > 0 && Math.abs(pts[lo - 1][0] - t) < Math.abs(pts[lo][0] - t) ? lo - 1 : lo; }

/* Time-series line chart. series: [{name, color, pts:[[ms,v]], w, dash, area}], one y axis. */
function lineChart(box, o) {
  box.innerHTML = ""; const W = box.clientWidth || 600, H = o.h || 240;
  const m = {l: o.ml ?? 52, r: o.endLabels ? Math.min(150, W * .22) : 14, t: 10, b: 24};
  const S = o.series.filter(s => s.pts.length);
  const svg = svgEl("svg", {viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": o.label || "chart"}, box);
  if (!S.length) { svgEl("text", {x: W / 2, y: H / 2, "text-anchor": "middle"}, svg).textContent = "No data"; return; }
  let x0 = Infinity, x1 = -Infinity, y0 = Infinity, y1 = -Infinity;
  for (const s of S) for (const [x, y] of s.pts) { if (!isNum(y) || (o.log && y <= 0)) continue; x0 = Math.min(x0, x); x1 = Math.max(x1, x); y0 = Math.min(y0, y); y1 = Math.max(y1, y); }
  if (o.x0) x0 = o.x0; if (o.x1) x1 = o.x1;
  for (const r of o.refs || []) { y0 = Math.min(y0, r.y); y1 = Math.max(y1, r.y); }
  if (o.zero && !o.log) { y0 = Math.min(y0, 0); y1 = Math.max(y1, 0); }
  if (y0 === y1) { y0 -= 1; y1 += 1; }
  const lg = !!o.log && y0 > 0, tf = v => lg ? Math.log(v) : v;
  if (!lg) { const pad = (y1 - y0) * .06; if (!(o.zero && y0 === 0)) y0 -= pad; y1 += pad; } else { y0 /= 1.04; y1 *= 1.04; }
  const X = t => m.l + (t - x0) / ((x1 - x0) || 1) * (W - m.l - m.r), Y = v => H - m.b - (tf(v) - tf(y0)) / ((tf(y1) - tf(y0)) || 1) * (H - m.t - m.b);
  const yt = lg ? logTicks(y0, y1) : niceTicks(y0, y1, o.yn || 5);
  for (const v of yt) { svgEl("line", {x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), class: v === 0 && !lg ? "ax" : "gl"}, svg);
    svgEl("text", {x: m.l - 6, y: Y(v) + 4, "text-anchor": "end"}, svg).textContent = o.yfmt(v); }
  const tt = timeTicks(x0, x1, Math.max(3, Math.floor((W - m.l - m.r) / 80)));
  for (const t of tt.ticks) svgEl("text", {x: X(t), y: H - 6, "text-anchor": "middle"}, svg).textContent = tt.fmt(t);
  for (const r of o.refs || []) { svgEl("line", {x1: m.l, x2: W - m.r, y1: Y(r.y), y2: Y(r.y), stroke: css("--axis"), "stroke-dasharray": "4 4"}, svg);
    if (r.label) svgEl("text", {x: m.l + 6, y: Y(r.y) - 5, class: "lab"}, svg).textContent = r.label; }
  const g = svgEl("g", {}, svg);
  S.forEach((s, si) => { const pts = s.pts.filter(p => isNum(p[1]) && (!lg || p[1] > 0) && p[0] >= x0 && p[0] <= x1); s._p = pts; if (!pts.length) return;
    let d = "", gap = false; for (let i = 0; i < pts.length; i++) { const brk = i && o.gapDays && pts[i][0] - pts[i - 1][0] > o.gapDays * DAY;
      d += (i === 0 || brk ? "M" : "L") + X(pts[i][0]).toFixed(1) + "," + Y(pts[i][1]).toFixed(1); gap = gap || brk; }
    if (s.area && !gap) { const base = lg ? Y(y0) : Y(Math.max(y0, Math.min(0, y1)));
      const grad = "g" + Math.random().toString(36).slice(2), defs = svgEl("defs", {}, svg), lgd = svgEl("linearGradient", {id: grad, x1: 0, x2: 0, y1: 0, y2: 1}, defs);
      svgEl("stop", {offset: "0%", "stop-color": s.color, "stop-opacity": .22}, lgd); svgEl("stop", {offset: "100%", "stop-color": s.color, "stop-opacity": 0}, lgd);
      svgEl("path", {d: d + `L${X(pts[pts.length - 1][0]).toFixed(1)},${base}L${X(pts[0][0]).toFixed(1)},${base}Z`, fill: `url(#${grad})`}, g); }
    svgEl("path", {d, fill: "none", stroke: s.color, "stroke-width": s.w || 2, "stroke-linejoin": "round", "stroke-linecap": "round", "stroke-dasharray": s.dash || ""}, g);
    if (si === 0 || o.endDots) { const [lx, ly] = pts[pts.length - 1]; svgEl("circle", {cx: X(lx), cy: Y(ly), r: 3.5, fill: s.color, stroke: css("--panel"), "stroke-width": 2}, g); } });
  // end labels (only where there is room)
  if (o.endLabels) { const ends = S.filter(s => s._p && s._p.length).map(s => ({y: Y(s._p[s._p.length - 1][1]), t: `${s.name} ${o.yfmt(s._p[s._p.length - 1][1])}`, c: s.color}))
      .sort((a, b) => a.y - b.y); for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 14);
    for (const e of ends) svgEl("text", {x: W - m.r + 8, y: e.y + 4, class: "lab"}, svg).textContent = e.t; }
  // markers (insider trades)
  for (const mk of o.markers || []) { if (mk.t < x0 || mk.t > x1) continue; const pv = priceAt(S[0]._p || [], mk.t); if (!isNum(pv)) continue;
    const x = X(mk.t), y = Y(pv), up = mk.kind === "buy", yy = up ? y + 12 : y - 12;
    svgEl("path", {d: up ? `M${x},${yy - 5}L${x - 5},${yy + 4}L${x + 5},${yy + 4}Z` : `M${x},${yy + 5}L${x - 5},${yy - 4}L${x + 5},${yy - 4}Z`,
      fill: up ? css("--up") : css("--down"), stroke: css("--panel"), "stroke-width": 1}, g); }
  // crosshair
  const cross = svgEl("line", {y1: m.t, y2: H - m.b, stroke: css("--axis"), visibility: "hidden"}, svg);
  const dots = S.map(s => svgEl("circle", {r: 4, fill: s.color, stroke: css("--panel"), "stroke-width": 2, visibility: "hidden"}, svg));
  const hit = svgEl("rect", {x: m.l, y: 0, width: W - m.l - m.r, height: H - m.b, fill: "transparent"}, svg);
  const move = ev => { const b = svg.getBoundingClientRect(), pt = ev.touches ? ev.touches[0] : ev, x = (pt.clientX - b.left) * W / b.width;
    const t = x0 + (x - m.l) / (W - m.l - m.r) * (x1 - x0); let html = "", tx = null;
    S.forEach((s, i) => { const k = nearest(s._p || [], t); if (k < 0) { dots[i].setAttribute("visibility", "hidden"); return; }
      const [px, py] = s._p[k]; if (tx == null) tx = px; dots[i].setAttribute("cx", X(px)); dots[i].setAttribute("cy", Y(py)); dots[i].setAttribute("visibility", "visible");
      html += `<div><span style="color:${s.color}">●</span> ${esc(s.name)}: <b>${o.tfmt ? o.tfmt(py, s, px) : o.yfmt(py)}</b></div>`; });
    if (tx == null) return; cross.setAttribute("x1", X(tx)); cross.setAttribute("x2", X(tx)); cross.setAttribute("visibility", "visible");
    const mk = (o.markers || []).filter(k => Math.abs(k.t - tx) <= (o.markerWin || 4) * DAY);
    showTip(`<div class="m">${o.datefmt ? o.datefmt(tx) : ymd(tx)}</div>${html}${mk.map(k => `<div class="${k.kind === "buy" ? "pos" : "neg"}">${esc(k.html)}</div>`).join("")}`, pt); };
  hit.addEventListener("mousemove", move); hit.addEventListener("touchmove", move, {passive: true});
  const leave = () => { hideTip(); cross.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); };
  hit.addEventListener("mouseleave", leave); hit.addEventListener("touchend", leave);
}

/* Bar chart over quarters (one series). bars: [{t, v, tip}] */
function barChart(box, o) {
  box.innerHTML = ""; const W = box.clientWidth || 500, H = o.h || 200, m = {l: 52, r: 8, t: 10, b: 24};
  const bars = o.bars.filter(b => isNum(b.v)); const svg = svgEl("svg", {viewBox: `0 0 ${W} ${H}`, height: H}, box);
  if (!bars.length) { svgEl("text", {x: W / 2, y: H / 2, "text-anchor": "middle"}, svg).textContent = "No data"; return; }
  let y0 = Math.min(0, ...bars.map(b => b.v)), y1 = Math.max(0, ...bars.map(b => b.v)); y1 += (y1 - y0) * .06 || 1;
  const Y = v => H - m.b - (v - y0) / (y1 - y0) * (H - m.t - m.b), bw = (W - m.l - m.r) / bars.length;
  for (const v of niceTicks(y0, y1, 4)) { svgEl("line", {x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v), class: v === 0 ? "ax" : "gl"}, svg);
    svgEl("text", {x: m.l - 6, y: Y(v) + 4, "text-anchor": "end"}, svg).textContent = o.yfmt(v); }
  const every = Math.ceil(bars.length / Math.max(2, Math.floor((W - m.l - m.r) / 64)));
  bars.forEach((b, i) => { const x = m.l + i * bw + 1, w = Math.max(1, bw - 2), ya = Y(Math.max(0, b.v)), yb = Y(Math.min(0, b.v)), h = Math.max(1, yb - ya);
    const r = Math.min(3, w / 2, h); const top = b.v >= 0;
    svgEl("path", {d: top ? `M${x},${yb}V${ya + r}Q${x},${ya} ${x + r},${ya}H${x + w - r}Q${x + w},${ya} ${x + w},${ya + r}V${yb}Z`
      : `M${x},${ya}V${yb - r}Q${x},${yb} ${x + r},${yb}H${x + w - r}Q${x + w},${yb} ${x + w},${yb - r}V${ya}Z`,
      fill: i === bars.length - 1 ? o.color : o.color2 || o.color, opacity: i === bars.length - 1 ? 1 : .78}, svg);
    if ((bars.length - 1 - i) % every === 0) svgEl("text", {x: x + w / 2, y: H - 6, "text-anchor": "middle"}, svg).textContent = o.xfmt(b.t);
    const hr = svgEl("rect", {x: m.l + i * bw, y: m.t, width: bw, height: H - m.t - m.b, fill: "transparent"}, svg);
    hr.addEventListener("mousemove", ev => showTip(b.tip, ev)); hr.addEventListener("mouseleave", hideTip); });
}

/* Tiny sparkline for lists. */
function spark(pts, w = 64, h = 20) { if (!pts || pts.length < 2) return ""; const ys = pts.map(p => p[1]), lo = Math.min(...ys), hi = Math.max(...ys);
  const X = i => (i / (pts.length - 1)) * (w - 2) + 1, Y = v => h - 2 - (v - lo) / ((hi - lo) || 1) * (h - 4);
  const c = ys[ys.length - 1] >= ys[0] ? css("--up") : css("--down");
  return `<svg class="spark" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><path d="${pts.map((p, i) => (i ? "L" : "M") + X(i).toFixed(1) + "," + Y(p[1]).toFixed(1)).join("")}" fill="none" stroke="${c}" stroke-width="1.5"/></svg>`; }

// ---------- common bits ----------
const GATES = {mcap: "market cap in range", price: "price over $5", liquidity: "daily trading over $2M", fresh_data: "recent filing",
  profitable: "profitable", fcf_positive: "free cash flow positive", low_leverage: "net debt under 1× EBITDA", no_red_flags: "no restatement or bankruptcy",
  price_to_sales: "P/S ceiling", net_margin: "net margin 5%+", us_company: "US headquarters", revenue_growing: "revenue per share growing",
  quality: "ROIC 10%+", margins_holding: "margins holding", debt_discipline: "debt not ballooning", business_model: "not a mortgage REIT",
  fscore: "Piotroski floor", profits_growing: "profit per share up over 3 years"};
const gname = g => GATES[g] || g.replace(/_/g, " ");
const isWarn = f => /FCF|CHECK|cyclical|tax benefit|auditor|listing|impairment|RED FLAG/.test(f);
const flagCls = f => /RED FLAG/.test(f) ? "bad" : /insider cluster/.test(f) ? "good" : isWarn(f) ? "warn" : "";
const pills = r => (r.flags || []).map(f => `<span class="pill ${flagCls(f)}">${esc(f)}</span>`).join("");
function statusPill(r) { if (!r) return `<span class="pill">Not in the screen universe</span>`;
  if (r.pass) return `<span class="pill ${r.top ? "top" : "good"}">${r.top ? "Top 25" : "Passes every gate"} · #${r.rank}${r.fin ? " financials" : ""}</span>`;
  if (!r.inv) return `<span class="pill">Outside screen size/liquidity range</span>`;
  return `<span class="pill warn">Fails: ${esc(r.failed.map(gname).join(", "))}</span>`; }
function irBar(r) { if (!isNum(r.ir)) return "–"; const sc = .4, a = Math.max(0, Math.min(r.cy ?? 0, sc)), b = Math.max(0, Math.min(r.g ?? 0, sc - a));
  return `<span class="irbar"><span class="b"><i style="width:${a / sc * 100}%;background:var(--s1)"></i><i style="width:${b / sc * 100}%;background:var(--s1b)"></i></span><b class="mono">${F.pct(r.ir)}</b></span>`; }
const tkLink = t => `<a class="tk" href="#t-${encodeURIComponent(t)}">${esc(t)}</a>`;
const watch = { list: () => store.get("watch", []), has(t) { return this.list().includes(t); },
  toggle(t) { const l = this.list(); store.set("watch", l.includes(t) ? l.filter(x => x !== t) : [t, ...l].slice(0, 50)); } };
function remember(t) { store.set("recent", [t, ...store.get("recent", []).filter(x => x !== t)].slice(0, 12)); }
const SERIES = ["--s1", "--s2", "--s3", "--s4"];

// ---------- search ----------
const qIn = $("#q"), sugg = $("#sugg");
let sel = -1, opts = [];
function matches(q, n = 10) { q = q.trim().toUpperCase(); if (!q) return [];
  const exact = [], pre = [], name = [];
  for (const r of D.rows) { if (r.t === q) exact.push(r); else if (r.t.startsWith(q)) pre.push(r); else if (r.n.toUpperCase().includes(q)) name.push(r); }
  const key = r => -(r.mc || 0); pre.sort((a, b) => a.t.length - b.t.length || key(a) - key(b)); name.sort((a, b) => key(a) - key(b));
  return [...exact, ...pre, ...name].slice(0, n); }
function renderSugg(onPick = go) {
  const q = qIn.value.trim(); opts = matches(q).map(r => ({t: r.t, n: r.n, s: r.pass ? "passes" : r.sec}));
  if (q && /^[A-Za-z.\-]{1,6}$/.test(q) && !opts.some(o => o.t === q.toUpperCase()))
    opts.push({t: q.toUpperCase(), n: D.server ? "Look up from SEC and Yahoo" : "Not in our database yet", s: ""});
  sugg.innerHTML = opts.map((o, i) => `<li role="option" aria-selected="${i === sel}" data-i="${i}"><span class="tk">${esc(o.t)}</span><span class="n">${esc(o.n)}</span><span class="mut">${esc(o.s)}</span></li>`).join("");
  sugg.hidden = !opts.length; sugg.parentElement.setAttribute("aria-expanded", String(!sugg.hidden));
  $$("li", sugg).forEach(li => li.addEventListener("mousedown", e => { e.preventDefault(); pick(+li.dataset.i, onPick); }));
}
function pick(i, onPick = go) { const o = opts[i]; if (!o) return; qIn.value = ""; sugg.hidden = true; sel = -1; qIn.blur(); onPick(o.t); }
const go = t => { location.hash = "t-" + t; };
qIn.addEventListener("input", () => { sel = 0; renderSugg(); });
qIn.addEventListener("keydown", e => { if (e.key === "ArrowDown") { sel = Math.min(opts.length - 1, sel + 1); renderSugg(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { sel = Math.max(0, sel - 1); renderSugg(); e.preventDefault(); }
  else if (e.key === "Enter") { pick(Math.max(0, sel)); } else if (e.key === "Escape") { sugg.hidden = true; qIn.blur(); } });
qIn.addEventListener("blur", () => setTimeout(() => { sugg.hidden = true; }, 120));
addEventListener("keydown", e => { if (e.key === "/" && document.activeElement !== qIn && !/INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) { e.preventDefault(); qIn.focus(); } });

// ---------- router ----------
const view = $("#view");
let RSEQ = 0;  // bumps on every navigation; async views bail out if the user moved on
function route() {
  RSEQ++; hideTip(); const h = decodeURIComponent(location.hash.slice(1));
  let nav = "home";
  if (h.startsWith("t-")) { nav = ""; viewTicker(h.slice(2).toUpperCase()); }
  else if (h === "screen") { nav = "screen"; viewScreen(); }
  else if (h === "record") { nav = "record"; viewRecord(); }
  else if (h === "about") { nav = "about"; viewAbout(); }
  else if (h === "compare" || h.startsWith("vs-")) { nav = "compare"; viewCompare(h.startsWith("vs-") ? h.slice(3).split("--").filter(Boolean).map(s => s.toUpperCase()) : null); }
  else viewHome();
  $$(".nav [data-nav]").forEach(a => a.toggleAttribute("aria-current", a.dataset.nav === nav) || a.dataset.nav === nav && a.setAttribute("aria-current", "page"));
  scrollTo(0, 0);
}
addEventListener("hashchange", route);
let rz; addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => window.__redraw && window.__redraw(), 150); });

// ---------- home ----------
function viewHome() {
  window.__redraw = null;
  const inv = D.rows.filter(r => r.inv), pass = inv.filter(r => r.pass && !r.fin);
  const top = pass.filter(r => r.top).sort((a, b) => a.rank - b.rank);
  const ins = inv.filter(r => r.ib >= 1).sort((a, b) => (b.ibu || 0) - (a.ibu || 0)).slice(0, 12);
  const dips = pass.filter(r => isNum(r.r1m)).sort((a, b) => a.r1m - b.r1m).slice(0, 8);
  const w = watch.list(), rec = store.get("recent", []).filter(t => !w.includes(t));
  const li = (r, v) => `<a class="li" href="#t-${encodeURIComponent(r.t)}"><span class="tk">${esc(r.t)}</span><span class="n">${esc(r.n)}</span><span class="v">${v}</span><span class="v sp mut">${F.usd(r.mc, 1)}</span></a>`;
  const mini = t => { const r = D.idx[t]; return r ? li(r, cls(r.r1m, F.spct(r.r1m)) + ' <span class="mut">1M</span>') :
    `<a class="li" href="#t-${encodeURIComponent(t)}"><span class="tk">${esc(t)}</span><span class="n mut">not in database</span><span></span><span></span></a>`; };
  view.innerHTML = `
  <div><div class="eyebrow">Prices as of ${esc(D.asof)}</div><h1 style="font-size:24px;margin-top:4px">Growth at a bargain price</h1>
   <p class="mut" style="margin:4px 0 0;max-width:70ch">Profitable, growing US small and mid caps with debt under control, ranked by implied return (cash yield + growth per share). Search any ticker above for the full breakdown.</p></div>
  <div class="tiles">
   <div class="tile"><div class="v">${pass.length}</div><div class="k">companies pass every gate</div></div>
   <div class="tile"><div class="v">${inv.length}</div><div class="k">investable in the universe</div></div>
   <div class="tile"><div class="v">${F.pct(median(pass.map(r => r.ir)))}</div><div class="k">median implied return, passing</div></div>
   <div class="tile"><div class="v">${F.x(median(pass.map(r => r.pe)))}</div><div class="k">median P/E, passing</div></div>
   <div class="tile"><div class="v">${inv.filter(r => r.ib >= 2).length}</div><div class="k">insider cluster buys (6M)</div></div>
  </div>
  ${changesCard()}
  <div class="grid2">
   <section class="card"><div class="card-h"><h2>Top 25 bargains</h2><a href="#screen" class="note">Full screen →</a></div>
    <div class="list">${top.map(r => li(r, `<b>${F.pct(r.ir)}</b> <span class="mut">IR</span>`)).join("")}</div></section>
   <div style="display:flex;flex-direction:column;gap:14px;min-width:0">
    <section class="card"><div class="card-h"><h2>Watchlist</h2><span class="note">Star a company to add it</span></div>
     <div class="list">${w.length ? w.map(mini).join("") : `<div class="mut" style="padding:8px 0">Nothing yet. Open a company and press ☆ Watch.</div>`}</div>
     ${rec.length ? `<div class="eyebrow" style="margin-top:12px">Recently viewed</div><div class="list">${rec.slice(0, 6).map(mini).join("")}</div>` : ""}</section>
    <section class="card"><div class="card-h"><h2>Insiders buying</h2><span class="note">Open-market buys, last 6 months</span></div>
     <div class="list">${ins.map(r => li(r, `${r.ib}× ${F.usd(r.ibu, 1)}`)).join("") || '<div class="mut">None</div>'}</div></section>
    <section class="card"><div class="card-h"><h2>Passing names that fell most this month</h2></div>
     <div class="list">${dips.map(r => li(r, cls(r.r1m, F.spct(r.r1m)))).join("")}</div></section>
   </div>
  </div>`;
}

// What moved since the previous price date: names that entered or left the top 25 or the passing list.
const CHG = {in_top: ["Entered top 25", "good"], in_pass: ["Now passes", "good"], out_top: ["Left top 25", "warn"], out_pass: ["Now fails", "bad"]};
function changesCard() {
  const C = D.changes; if (!C) return "";
  const since = new Date(C.since + "T12:00:00Z").toLocaleDateString("en-US", {weekday: "short", month: "short", day: "numeric", timeZone: "UTC"});
  const why = c => {
    const out = [];
    if (c.kind === "in_top" && c.rank) out.push(`#${c.rank}`);
    if (c.cleared.length) out.push("cleared " + c.cleared.map(gname).join(", "));
    if (c.failing.length) out.push("fails " + c.failing.map(gname).join(", "));
    if (c.filing) out.push(`new filing (quarter to ${c.filing})`);
    if (isNum(c.px_chg) && Math.abs(c.px_chg) >= .02) out.push(`price ${F.spct(c.px_chg)}`);
    if (isNum(c.ir) && isNum(c.ir_prev) && Math.abs(c.ir - c.ir_prev) >= .005) out.push(`implied return ${F.pct(c.ir_prev)} → ${F.pct(c.ir)}`);
    if (c.kind === "out_top" && !c.failing.length) out.push("outranked by cheaper names");
    return out.join(" · ");
  };
  const rows = C.items.slice(0, 12).map(c => { const r = D.idx[c.t] || {}, [lab, k] = CHG[c.kind];
    return `<a class="chg" href="#t-${encodeURIComponent(c.t)}"><span class="tk">${esc(c.t)}</span><span class="n">${esc(r.n || "")}${c.fin ? ' <span class="mut">(financial)</span>' : ""}<small>${why(c)}</small></span><span class="pill ${k}">${lab}</span></a>`; }).join("");
  return `<section class="card"><div class="card-h"><h2>New since ${esc(since)}</h2><span class="note">${C.items.length ? `${C.items.length} change${C.items.length === 1 ? "" : "s"} to the passing list` : ""}</span></div>
   <div class="list">${rows || '<div class="mut" style="padding:6px 0">No changes: the same names pass and the same 25 lead.</div>'}</div>${C.items.length > 12 ? `<div class="mut" style="font-size:12px;margin-top:6px">+${C.items.length - 12} more</div>` : ""}</section>`;
}

// ---------- track record ----------
async function viewRecord() {
  window.__redraw = null;
  view.innerHTML = `<div class="card empty">Loading the track record…</div>`;
  const my = RSEQ;
  let R; try { R = await getJSON("api/scorecard"); } catch (e) { if (my === RSEQ) view.innerHTML = `<div class="card empty">No track record yet. ${esc(e.message)}</div>`; return; }
  if (my !== RSEQ) return;
  if (!R.series.length) { view.innerHTML = `<div class="card empty">No paper portfolio yet: the first screen of each month opens one.</div>`; return; }
  const S = R.summary, B = R.bench, last = R.books[R.books.length - 1];
  const col = {v: css("--hero"), IJR: css("--s2"), IJH: css("--s3"), SPY: css("--s4")};
  const ser = [{k: "v", name: "QUAINT top 25"}, ...Object.keys(B).map(k => ({k, name: B[k]}))].map(o => ({...o, color: col[o.k]}));
  view.innerHTML = `
  <div><div class="eyebrow">Forward record since ${esc(S.since)} · no hindsight, no survivorship bias</div><h1 style="font-size:22px;margin-top:4px">Track record</h1>
   <p class="mut" style="margin:4px 0 0;max-width:72ch">Each month's first screen buys its top 25 in equal weight at that run's prices and holds until the next month's list. Dividends included. This is the honest test of the method; the backtest flatters it.</p></div>
  <div class="tiles">
   <div class="tile"><div class="v">${cls(S.return, F.spct(S.return))}</div><div class="k">QUAINT top 25 since ${esc(S.since)}</div></div>
   ${Object.keys(B).map(k => `<div class="tile"><div class="v">${cls(S["vs_" + k], F.spct(S["vs_" + k]))}</div><div class="k">vs ${esc(B[k])}</div></div>`).join("")}
   <div class="tile"><div class="v">${R.books.length}</div><div class="k">monthly ${R.books.length === 1 ? "book" : "books"}${S.hit_rate != null ? `, ${F.pct(S.hit_rate, 0)} beat the S&amp;P 600` : ""}</div></div>
  </div>
  ${S.days < 90 ? `<div class="card" style="margin:0 0 14px"><b>Too early to judge.</b> <span class="mut">${S.days} days is noise; give it a year of monthly books before reading anything into it.</span></div>` : ""}
  <section class="card" style="margin-bottom:14px"><div class="card-h"><h2>Growth of $100</h2></div>
   <div class="legend">${ser.map(s => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("")}</div><div class="chart" id="rc"></div></section>
  <div class="grid2">
   <section class="card"><div class="card-h"><h2>Monthly books</h2></div><div class="tw"><table><thead><tr><th class="l">Opened</th><th class="l">Held to</th><th>Return</th><th>S&amp;P 600</th><th>Edge</th></tr></thead><tbody>
    ${[...R.books].reverse().map(b => `<tr><td class="l mono">${esc(b.start)}</td><td class="l mono">${esc(b.end)}</td><td>${cls(b.ret, F.spct(b.ret))}</td><td>${cls(b.bench.IJR, F.spct(b.bench.IJR))}</td><td>${cls(b.ret - b.bench.IJR, F.spct(b.ret - b.bench.IJR))}</td></tr>`).join("")}
   </tbody></table></div></section>
   <section class="card"><div class="card-h"><h2>Current book</h2><span class="note">opened ${esc(last.start)}</span></div><div class="tw"><table><thead><tr><th>#</th><th class="l">Ticker</th><th>Entry</th><th>Return</th></tr></thead><tbody>
    ${[...last.names].sort((a, b) => (b.ret ?? -9) - (a.ret ?? -9)).map(n => `<tr><td>${n.rank}</td><td class="l">${tkLink(n.t)}</td><td class="mono">${F.px(n.entry)}</td><td>${cls(n.ret, F.spct(n.ret))}</td></tr>`).join("")}
   </tbody></table></div></section>
  </div>`;
  const draw = () => lineChart($("#rc"), {series: ser.map(s => ({name: s.name, color: s.color, w: s.k === "v" ? 2.5 : 1.5, pts: R.series.filter(p => isNum(p[s.k])).map(p => [Date.parse(p.d), p[s.k] * 100])})),
    h: innerWidth < 700 ? 240 : 300, yfmt: v => "$" + v.toFixed(0), refs: [{y: 100}], endDots: true, endLabels: innerWidth > 700,
    tfmt: v => `$${v.toFixed(1)} <span class="m">(${F.spct(v / 100 - 1)})</span>`});
  window.__redraw = draw; draw();
}

// ---------- about ----------
const AB_GATES = [
  ["Size and liquidity", "US headquarters, $0.3B to $5B market cap, price over $5, at least $2M traded a day"],
  ["Profitable", "Net income and free cash flow positive, net margin 5%+, return on capital 10%+"],
  ["Growing", "Revenue per share up over the last year and over 3 years; margins not slipping more than 2 points"],
  ["Balance sheet", "Net debt under 1× EBITDA, and debt not ballooning"],
  ["Clean record", "No restatement or bankruptcy filing in the past year; no mortgage REITs"]];
const AB_STEPS = [
  ["Collect", "Every 10-K and 10-Q from SEC EDGAR, today's prices from Yahoo, insider trades from Form 4. Each number is stored with the date it went public, so history never sees the future."],
  ["Filter", "About 1,500 companies in the S&amp;P 500, 400, 600 and Nasdaq-100 run through hard gates. Fail one, or have missing data, and you're out."],
  ["Rank", "Survivors are ranked by implied return: what an owner earns each year from cash yield plus growth."],
  ["Track", "Each month the top 25 become an equal-weight paper portfolio, measured against the S&amp;P 500, 400 and 600."]];
function viewAbout() {
  window.__redraw = null;
  const inv = D.rows.filter(r => r.inv), pass = inv.filter(r => r.pass && !r.fin), top = pass.filter(r => r.top);
  view.innerHTML = `<div class="about">
  <section class="ab-hero">
   <div class="eyebrow">About QU<span class="acc">AI</span>NT</div>
   <h1>Growth at a bargain price, found by rules and checked against data.</h1>
   <p>QUAINT screens US small and mid caps every weekday evening for one thing: businesses that make real money, grow it per share and carry little debt, yet still trade cheap. It ranks them by the return an owner would earn if the price never re-rated, and keeps a live record of how its picks do.</p>
   <div class="ab-live"><span><b>${inv.length}</b> investable</span><span><b>${pass.length}</b> pass every gate</span><span><b>${F.pct(median(top.map(r => r.ir)))}</b> median implied return, top 25</span><span class="mut">as of ${esc(D.asof)}</span></div>
  </section>
  <section><h2 class="ab-h">How it works</h2>
   <ol class="ab-steps">${AB_STEPS.map((s, i) => `<li><span class="n">0${i + 1}</span><b>${s[0]}</b><p>${s[1]}</p></li>`).join("")}</ol></section>
  <section class="grid2">
   <div class="card"><h2 class="ab-h">The gates <span class="mut">every one must pass</span></h2>
    <ul class="ab-gates">${AB_GATES.map(g => `<li><b>${g[0]}</b><span>${g[1]}</span></li>`).join("")}</ul></div>
   <div class="card"><h2 class="ab-h">The rank</h2>
    <div class="ab-formula"><div><b>Implied return</b> = cash yield + growth</div>
     <div><span class="acc">cash yield</span> = average of earnings yield and free-cash-flow yield (after stock pay, capped at 2× earnings)</div>
     <div><span class="acc">growth</span> = the slower of 1-year and 3-year revenue-per-share growth, held between 0 and 20%</div></div>
    <p>Read it as the yearly return an owner earns if the valuation multiple never changes. A company at 8× earnings growing 5% scores about 17.5%; one at 40× growing 10% scores about 12.5%. Cheap and growing wins; expensive growth has to grow into its price.</p>
    <p class="mut">At most 7 names per sector in the top 25. Banks and insurers are ranked on their own list. Momentum, insider buying, headlines and price action are shown but never scored: each was tested and none improved the result.</p></div>
  </section>
  <section><h2 class="ab-h">Does it work?</h2>
   <div class="tiles">
    <div class="tile hero"><div class="v">21.4%</div><div class="k">QUAINT top 25, per year</div></div>
    <div class="tile"><div class="v">18.0%</div><div class="k">all eligible small and mid caps, equal weight</div></div>
    <div class="tile"><div class="v">14.9%</div><div class="k">S&amp;P 500 (SPY)</div></div>
   </div>
   <p class="ab-note">Backtest 2017 to 2026, rebalanced monthly, 3-month holds, trading costs included. The edge is about 3 points a year and isn't statistically proven. The backtest uses today's index members, which flatters it, and the edge fades after about a month, so the list refreshes monthly. The live ${D.db ? '<a href="#record">track record</a>' : "track record"}, which has neither problem, is the real test. Every variant tried is in the <a href="explorer.html" target="_blank" rel="noopener">backtest explorer</a>.</p></section>
  <section class="card"><h2 class="ab-h">Principles</h2>
   <ul class="ab-pr">
    <li><b>Rules earn their place.</b> A new factor goes in only if it beats the backtest. Otherwise it's shown, not scored.</li>
    <li><b>Point in time, fail closed.</b> Only data that was public on the date. Missing or unverifiable means fail.</li>
    <li><b>Think like an owner.</b> Growth per share, and cash flow after stock-based pay.</li>
    <li><b>Simple beats clever.</b> One transparent rank, round thresholds, few knobs to overfit.</li>
    <li><b>The screen finds candidates; you decide.</b> Odd numbers get a flag, not a rank. Read the filings, spread the risk.</li>
   </ul></section></div>`;
}

// ---------- screen ----------
const SDEF = {mode: "pass", group: "co", sec: "", q: "", r: {}, tg: [], sort: "ir", dir: -1};
const RANGES = [["mc", "Mkt cap $B", 1e9], ["pe", "P/E", 1], ["ps", "P/S", 1], ["nm", "Net margin %", .01], ["nde", "ND/EBITDA", 1], ["ir", "Implied return %", .01], ["pg3", "Profit/sh 3Y %", .01], ["r1y", "1Y price %", .01]];
const TOGGLES = [["ins", "Insiders bought", r => r.ib >= 1], ["cash", "Net cash", r => isNum(r.nd) && r.nd <= 0], ["pg", "Profit/share up over 3Y", r => r.pg3 > 0],
  ["calm", "Hide amber flags", r => !(r.flags || []).some(isWarn)]];
const SGROUPS = [  // [group, columns]; each column is [key, header, cell, class]
  ["", [["rank", "#", r => r.pass ? r.rank : ""], ["t", "Ticker", r => tkLink(r.t), "l"], ["n", "Company", r => esc(r.n), "l nm"], ["sec", "Sector", r => `<span class="mut">${esc(r.sec)}</span>`, "l"]]],
  ["Market", [["px", "Price", r => F.px(r.px)], ["mc", "Mkt cap", r => F.usd(r.mc, 2)], ["r1y", "1Y", r => cls(r.r1y, F.spct(r.r1y, 0))]]],
  ["Valuation", [["ir", "Implied return", irBar], ["pe", "P/E", r => F.x(r.pe)], ["ps", "P/S", r => F.x(r.ps)]]],
  ["Fundamentals", [["rev", "Revenue", r => F.usd(r.rev)], ["ni", "Net income", r => F.usd(r.ni)], ["nm", "Net mgn", r => F.pct(r.nm)],
    ["nd", "Net debt", r => isNum(r.nd) && r.nd <= 0 ? `<span class="pos">${F.usd(-r.nd)} cash</span>` : F.usd(r.nd)], ["nde", "ND/EBITDA", r => F.x(r.nde)]]],
  ["Quality and growth", [["roic", "ROIC", r => F.pct(r.fin ? r.roe : r.roic, 0)], ["pg3", "Profit/sh 3Y", r => cls(r.pg3, F.spct(r.pg3))]]],
  ["Signals", [["ibu", "Insiders 6M", r => r.ib >= 1 ? `<span class="pos">${r.ib} bought ${F.usd(r.ibu, 1)}</span>` : '<span class="mut">–</span>'],
    ["flags", "Flags", r => (r.pass ? "" : `<span class="pill">fails ${esc(r.failed.map(gname).join(", "))}</span>`) + pills(r), "l"]]]];
const SCOLS = SGROUPS.flatMap(([, cols], gi) => cols.map((c, i) => i === 0 && gi ? [c[0], c[1], c[2], ((c[3] || "") + " gs").trim()] : c));
const SHEAD = `<tr class="grp">${SGROUPS.map(([g, cols], gi) => `<th colspan="${cols.length}" class="l${gi ? " gs" : ""}">${esc(g)}</th>`).join("")}</tr>`;
function viewScreen() {
  let S = Object.assign({}, SDEF, store.get("screen", {}));
  const secs = [...new Set(D.rows.map(r => r.sec).filter(Boolean))].sort();
  view.innerHTML = `<div class="card-h"><div><div class="eyebrow">Prices as of ${esc(D.asof)}</div><h1 style="font-size:22px">Screen</h1></div><span class="mut" id="cnt"></span></div>
   <div class="card filters">
    <div class="row"><input type="search" id="sq" placeholder="Filter by ticker, name or industry" style="flex:1;min-width:180px">
     <div class="seg" id="smode"></div><div class="seg" id="sgroup"></div>
     <select id="ssec"><option value="">All sectors</option>${secs.map(s => `<option>${esc(s)}</option>`).join("")}</select>
     <button class="btn" id="sreset">Reset</button></div>
    <div class="row">${TOGGLES.map(([k, l]) => `<label class="tg"><input type="checkbox" data-tg="${k}"> ${l}</label>`).join("")}</div>
    <div class="row">${RANGES.map(([k, l]) => `<span class="rg">${l} <input type="number" step="any" data-r="${k}" data-b="0" placeholder="min"><input type="number" step="any" data-r="${k}" data-b="1" placeholder="max"></span>`).join("")}</div>
   </div>
   <div class="tw" style="max-height:75vh"><table><thead></thead><tbody></tbody></table></div>`;
  const segs = () => {
    $("#smode").innerHTML = [["top", "Top 25"], ["pass", "Passing"], ["near", "Near misses"], ["inv", "All investable"], ["all", "Everything"]].map(([v, l]) => `<button data-v="${v}" aria-pressed="${S.mode === v}">${l}</button>`).join("");
    $("#sgroup").innerHTML = [["co", "Companies"], ["fin", "Financials"], ["both", "Both"]].map(([v, l]) => `<button data-v="${v}" aria-pressed="${S.group === v}">${l}</button>`).join("");
    $$("#smode button").forEach(b => b.onclick = () => { S.mode = b.dataset.v; draw(); });
    $$("#sgroup button").forEach(b => b.onclick = () => { S.group = b.dataset.v; draw(); }); };
  const fill = () => { $("#sq").value = S.q; $("#ssec").value = S.sec; $$("[data-tg]").forEach(c => c.checked = S.tg.includes(c.dataset.tg));
    $$("[data-r]").forEach(i => { const v = (S.r[i.dataset.r] || [])[+i.dataset.b]; i.value = v ?? ""; }); };
  const keep = r => {
    if (S.group === "co" && r.fin || S.group === "fin" && !r.fin) return false;
    if (S.mode === "top" && !(r.top || r.fin && r.pass && r.rank <= 10)) return false;
    if (S.mode === "pass" && !r.pass) return false;
    if (S.mode === "near" && !(r.inv && !r.pass && r.failed.length === 1 && !["business_model", "no_red_flags", "us_company"].includes(r.failed[0]))) return false;
    if (S.mode === "inv" && !r.inv) return false;
    if (S.sec && r.sec !== S.sec) return false;
    if (S.q) { const q = S.q.toLowerCase(); if (!(r.t.toLowerCase().includes(q) || r.n.toLowerCase().includes(q) || r.ind.toLowerCase().includes(q))) return false; }
    for (const k of S.tg) { const t = TOGGLES.find(x => x[0] === k); if (t && !t[2](r)) return false; }
    for (const [k, b] of Object.entries(S.r)) { const sc = RANGES.find(x => x[0] === k)[2], v = k === "roic" && r.fin ? r.roe : r[k];
      if (isNum(b[0]) && !(isNum(v) && v >= b[0] * sc)) return false; if (isNum(b[1]) && !(isNum(v) && v <= b[1] * sc)) return false; }
    return true; };
  const draw = () => {
    store.set("screen", S); segs();
    const rows = D.rows.filter(keep), key = S.sort;
    rows.sort((a, b) => { const x = key === "flags" ? (a.flags || []).length : a[key], y = key === "flags" ? (b.flags || []).length : b[key];
      if (x == null && y == null) return 0; if (x == null || x === "") return 1; if (y == null || y === "") return -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * S.dir; });
    $("#cnt").textContent = `${rows.length} shown`;
    $("thead", view).innerHTML = SHEAD + "<tr>" + SCOLS.map(([k, l, , c]) => `<th class="sort ${c || ""}" data-k="${k}" ${S.sort === k ? `aria-sort="${S.dir > 0 ? "ascending" : "descending"}"` : ""}>${l}</th>`).join("") + "</tr>";
    $$("th[data-k]", view).forEach(th => th.onclick = () => { const k = th.dataset.k; S.dir = S.sort === k ? -S.dir : (["t", "n", "sec", "rank"].includes(k) ? 1 : -1); S.sort = k; draw(); });
    $("tbody", view).innerHTML = rows.slice(0, 600).map(r => `<tr class="click" data-t="${esc(r.t)}">` + SCOLS.map(([, , f, c]) => `<td class="${c || ""}">${f(r)}</td>`).join("") + "</tr>").join("")
      + (rows.length > 600 ? `<tr><td colspan="${SCOLS.length}" class="l mut">Showing the first 600. Narrow the filters to see the rest.</td></tr>` : "");
    $$("tbody tr[data-t]", view).forEach(tr => tr.onclick = e => { if (!e.target.closest("a")) go(tr.dataset.t); }); };
  $("#sq").oninput = e => { S.q = e.target.value; draw(); };
  $("#ssec").onchange = e => { S.sec = e.target.value; draw(); };
  $$("[data-tg]").forEach(c => c.onchange = () => { S.tg = $$("[data-tg]").filter(x => x.checked).map(x => x.dataset.tg); draw(); });
  $$("[data-r]").forEach(i => i.oninput = () => { const k = i.dataset.r, b = S.r[k] ? [...S.r[k]] : [null, null]; b[+i.dataset.b] = i.value === "" ? null : +i.value;
    if (b[0] == null && b[1] == null) delete S.r[k]; else S.r[k] = b; draw(); });
  $("#sreset").onclick = () => { S = JSON.parse(JSON.stringify(SDEF)); fill(); draw(); };
  fill(); draw(); window.__redraw = null;
}

// ---------- ticker ----------
const RANGE_D = {"1M": 31, "3M": 92, "6M": 183, "YTD": null, "1Y": 366, "3Y": 1096, "5Y": 1827, "10Y": 3653};
async function viewTicker(t) {
  view.innerHTML = `<div class="empty">Loading ${esc(t)}…</div>`;
  const my = RSEQ, c = await loadCompany(t);
  if (my !== RSEQ) return;
  if (!c || (!c.price.length && !c.Q.length)) {
    view.innerHTML = `<div class="card empty"><h2>${esc(t)} isn't in the database yet</h2><p>${D.server ? "SEC and Yahoo had nothing usable for it. Check the symbol." :
      "This page covers the S&amp;P 500, 400, 600 and Nasdaq-100. Run <span class=\"mono\">python3 -m screener app</span> on your computer to look up any US-listed ticker, or ask Claude to add it."}</p></div>`; return; }
  remember(t);
  const r = c.row, L = c.last, fin = c.fin, P = c.price;
  const last = P.length ? P[P.length - 1] : null, prev = P.length > 1 ? P[P.length - 2] : null;
  const dchg = last && prev && last[0] - prev[0] < 5 * DAY ? last[1] / prev[1] - 1 : null;
  const st = {range: store.get("range", "1Y"), log: store.get("log", false), ma: store.get("ma", true), val: store.get("valm", "pe")};
  const ytd = last ? Date.UTC(new Date(last[0]).getUTCFullYear(), 0, 1) : 0;
  const ev = isNum(c.mcap) ? c.mcap + (L.nd ?? 0) : null;
  const pe = r?.pe ?? (L.net_income > 0 && c.mcap ? c.mcap / L.net_income : null);
  const q4 = c.Q[c.Q.length - 5], q12 = c.Q[c.Q.length - 13];
  const cagr3 = (a, b) => isNum(a) && isNum(b) && a > 0 && b > 0 ? (a / b) ** (1 / 3) - 1 : null;
  const shChg = q4 && L.shares && q4.shares ? L.shares / q4.shares - 1 : null;
  const payout = (Math.abs(L.dividends || 0) + Math.abs(L.buybacks || 0));
  const kv = (k, v, title) => `<div class="kv"${title ? ` title="${esc(title)}"` : ""}><span>${k}</span><span>${v}</span></div>`;
  const news = D.news[t] || [];
  view.innerHTML = `
  <section>
   <div class="th"><div><div class="meta">${esc(r?.sec || "")}${r?.ind ? " · " + esc(r.ind) : ""}${r?.idx ? " · " + esc(r.idx.replace(/SP/g, "S&P ").replace("NDX", "Nasdaq-100")) : ""}</div>
     <h1>${esc(t)}</h1><div class="nm">${esc(r?.n || c.raw.name || "")}</div></div>
    <div class="price"><div class="p">${F.px(last?.[1])}</div>
     <div class="ch">${cls(dchg, F.spct(dchg, 2))} <span class="mut">day</span> · ${cls(changeOver(P, 366), F.spct(changeOver(P, 366)))} <span class="mut">1Y</span></div>
     <div class="mut" style="font-size:12px">${last ? "close " + ymd(last[0]) : ""}</div></div></div>
   <div class="actions">${statusPill(r)}
    <button class="btn" id="watch">${watch.has(t) ? "★ Watching" : "☆ Watch"}</button>
    <a href="#vs-${encodeURIComponent(t)}">+ Compare</a>
    <a href="https://finance.yahoo.com/quote/${encodeURIComponent(t)}" target="_blank" rel="noopener">Yahoo ↗</a>
    ${r?.cik ? `<a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${r.cik}&type=&dateb=&owner=include&count=40" target="_blank" rel="noopener">SEC filings ↗</a>` : ""}
    <a href="https://finviz.com/quote.ashx?t=${encodeURIComponent(t)}" target="_blank" rel="noopener">Finviz ↗</a>
    <a href="https://news.google.com/search?q=${encodeURIComponent((r?.n || t) + " stock")}" target="_blank" rel="noopener">News ↗</a></div>
  </section>
  ${r?.why || (r?.flags || []).length ? `<div class="card" style="display:flex;gap:10px;flex-wrap:wrap;align-items:center">${r.why ? `<span><b>Why it's here:</b> ${esc(r.why)}</span>` : ""}<span>${pills(r)}</span></div>` : ""}
  <section class="card"><div class="card-h"><h2>Price</h2>
    <div class="ctl"><div class="seg" id="rng">${Object.keys(RANGE_D).map(k => `<button data-k="${k}" aria-pressed="${st.range === k}">${k}</button>`).join("")}</div>
     <label class="tg"><input type="checkbox" id="lg" ${st.log ? "checked" : ""}> Log</label>
     <label class="tg" title="50- and 200-day moving averages (ranges up to 1 year)"><input type="checkbox" id="ma" ${st.ma ? "checked" : ""}> 50/200-day</label></div></div>
   <div class="legend" id="pleg"></div><div class="chart" id="pc"></div></section>
  <section class="card"><div class="card-h"><h2>Key numbers</h2><span class="note">Last 12 months${L.d ? " to " + ymd(L.d) : ""}, from SEC filings</span></div>
   <div class="stats">
    <section><h3>Valuation</h3>${kv("Market cap", F.usd(c.mcap))}${kv("Enterprise value", fin ? "n/m" : F.usd(ev))}${kv("P/E", F.x(pe))}
     ${kv("P/S", F.x(r?.ps ?? div(c.mcap, L.revenue)))}${kv("EV/EBIT", fin ? "n/m" : F.x(r?.evebit ?? (L.ebit > 0 ? ev / L.ebit : null)))}
     ${kv("FCF yield", fin ? "n/m" : F.pct(r?.fy ?? div(L.fcf, c.mcap)), "Free cash flow after stock-based pay ÷ market cap")}${kv("Implied return", r ? `<b>${F.pct(r.ir)}</b>` : "–", "Cash yield + growth per share")}</section>
    <section><h3>Profitability</h3>${kv("Revenue", F.usd(L.revenue))}${kv("Net income", F.usd(L.net_income))}${kv("Free cash flow", fin ? "n/m" : F.usd(L.fcf))}
     ${kv("Gross margin", F.pct(L.gm))}${kv("Operating margin", F.pct(L.om))}${kv("Net margin", F.pct(L.nm))}${kv(fin ? "ROE" : "ROIC", F.pct(fin ? L.roe : (r?.roic ?? L.roic)))}</section>
    <section><h3>Growth</h3>${kv("Revenue, 1Y", cls(L.rg, F.spct(L.rg)))}${kv("Revenue, 3Y per yr", cls(cagr3(L.revenue, q12?.revenue), F.spct(cagr3(L.revenue, q12?.revenue))))}
     ${kv("EPS, 1Y", L.epsg != null ? cls(L.epsg, F.spct(L.epsg)) : (q4?.eps <= 0 && L.eps > 0 ? "from a loss" : "–"))}${kv("Profit/share, 3Y per yr", cls(r?.pg3, F.spct(r?.pg3)), "Operating profit per share vs 3 years ago")}
     ${kv("Op. income, 3Y per yr", cls(cagr3(L.ebit, q12?.ebit), F.spct(cagr3(L.ebit, q12?.ebit))))}${kv("Shares, 1Y", cls(-shChg, F.spct(shChg)), "Negative = buybacks shrinking the share count")}${kv("Price, 1Y", cls(changeOver(P, 366), F.spct(changeOver(P, 366))))}</section>
    <section><h3>Balance sheet</h3>${kv("Cash", F.usd(L.cash))}${kv("Debt", F.usd(L.debt))}${kv("Net debt", isNum(L.nd) && L.nd <= 0 ? `<span class="pos">${F.usd(-L.nd)} cash</span>` : F.usd(L.nd))}
     ${kv("Net debt / EBITDA", fin ? "n/m" : F.x(L.nde))}${kv("Interest coverage", isNum(L.cov) ? F.x(L.cov, 0) : "none reported")}${kv("Payout yield", F.pct(div(payout, c.mcap)), "Dividends + buybacks ÷ market cap")}
     ${kv("Insiders, 6M", r?.ib >= 1 ? `<span class="pos">${r.ib} bought ${F.usd(r.ibu, 1)}</span>` : r?.isu ? `sold ${F.usd(r.isu, 1)}` : "none")}</section>
   </div></section>
  <div class="grid2">
   <section class="card"><div class="card-h"><h2>Revenue</h2><span class="note">Trailing 12 months at each quarter</span></div><div class="chart" id="c-rev"></div></section>
   <section class="card"><div class="card-h"><h2>Profit and cash flow</h2><span class="note">Trailing 12 months</span></div><div class="legend" id="l-prof"></div><div class="chart" id="c-prof"></div></section>
   <section class="card"><div class="card-h"><h2>Margins</h2></div><div class="legend" id="l-mar"></div><div class="chart" id="c-mar"></div></section>
   <section class="card"><div class="card-h"><h2>Valuation history</h2>
     <div class="seg" id="valm">${[["pe", "P/E"], ["ps", "P/S"], ["evebit", "EV/EBIT"], ["fy", "FCF yield"]].map(([k, l]) => `<button data-k="${k}" aria-pressed="${st.val === k}">${l}</button>`).join("")}</div></div>
     <p class="mut" id="valnote" style="margin:0 0 4px;font-size:12.5px"></p><div class="chart" id="c-val"></div></section>
   <section class="card"><div class="card-h"><h2>Per share</h2><span class="note">Split-adjusted</span></div><div class="legend" id="l-ps"></div><div class="chart" id="c-ps"></div></section>
   <section class="card"><div class="card-h"><h2>Cash and debt</h2></div><div class="legend" id="l-bs"></div><div class="chart" id="c-bs"></div></section>
  </div>
  <section><div class="card-h"><h2>Financials</h2><div class="seg" id="fper"><button data-k="q" aria-pressed="true">Every quarter</button><button data-k="y" aria-pressed="false">Yearly</button></div></div>
   <div class="tw" id="ftab"></div><p class="mut" style="font-size:12px;margin:6px 0 0">Each column is the trailing 12 months ending that quarter, as filed with the SEC (later restatements win).</p></section>
  <div class="grid2">
   <section class="card"><div class="card-h"><h2>Insider trades</h2><span class="note">Open-market, officers and directors</span></div><div class="tw" style="max-height:340px;border:0">${insTable(c.ins)}</div></section>
   <section class="card"><div class="card-h"><h2>Headlines</h2></div><div class="news" id="news">${newsList(news) || `<div class="mut">${D.server ? "Loading…" : "Headlines are fetched for the top 25 when the screen runs. Use News ↗ above for this company."}</div>`}</div></section>
  </div>`;
  $("#watch").onclick = e => { watch.toggle(t); e.target.textContent = watch.has(t) ? "★ Watching" : "☆ Watch"; };
  $$("#rng button").forEach(b => b.onclick = () => { st.range = b.dataset.k; store.set("range", st.range); $$("#rng button").forEach(x => x.setAttribute("aria-pressed", x === b)); drawPrice(); });
  $("#lg").onchange = e => { st.log = e.target.checked; store.set("log", st.log); drawPrice(); };
  $("#ma").onchange = e => { st.ma = e.target.checked; store.set("ma", st.ma); drawPrice(); };
  $$("#valm button").forEach(b => b.onclick = () => { st.val = b.dataset.k; store.set("valm", st.val); $$("#valm button").forEach(x => x.setAttribute("aria-pressed", x === b)); drawVal(); });
  let per = "q"; $$("#fper button").forEach(b => b.onclick = () => { per = b.dataset.k; $$("#fper button").forEach(x => x.setAttribute("aria-pressed", x === b)); $("#ftab").innerHTML = finTable(c, per); });
  $("#ftab").innerHTML = finTable(c, per);
  if (D.server && !news.length) getJSON(`api/news/${encodeURIComponent(t)}`).then(n => { $("#news") && ($("#news").innerHTML = newsList(n) || '<div class="mut">No recent headlines.</div>'); }).catch(() => {});

  function drawPrice() {
    if (!P.length) { $("#pc").innerHTML = '<div class="empty">No price history</div>'; return; }
    const end = last[0], days = RANGE_D[st.range], x0 = st.range === "YTD" ? ytd : end - days * DAY;
    const pts = P.filter(p => p[0] >= x0);
    const S = [{name: t, color: css("--hero"), pts, area: true}];
    const daily = P.filter(p => p[0] >= end - 2 * 366 * DAY);
    if (st.ma && days !== null && days <= 366 || st.ma && st.range === "YTD") {
      const ma = n => { const out = []; let sum = 0; for (let i = 0; i < daily.length; i++) { sum += daily[i][1]; if (i >= n) sum -= daily[i - n][1]; if (i >= n - 1 && daily[i][0] >= x0) out.push([daily[i][0], sum / n]); } return out; };
      S.push({name: "50-day", color: css("--s2"), pts: ma(50), w: 1.5}, {name: "200-day", color: css("--s4"), pts: ma(200), w: 1.5}); }
    const chg = pts.length > 1 ? pts[pts.length - 1][1] / pts[0][1] - 1 : null;
    $("#pleg").innerHTML = S.map(s => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join("") +
      `<span class="mut">${st.range}: ${cls(chg, F.spct(chg))}</span>` + (c.ins.length ? `<span><span class="pos">▲</span> insider buy <span class="neg">▼</span> sell</span>` : "");
    const mk = c.ins.map(([d, code, v, p, title]) => ({t: Date.parse(d), kind: code === "P" ? "buy" : "sell", html: `${code === "P" ? "Bought" : "Sold"} ${F.usd(v, 1)} at ${F.px(p)} (${title})`}));
    lineChart($("#pc"), {series: S, h: innerWidth < 700 ? 240 : 320, log: st.log, yfmt: v => F.px(v), x0, markers: mk, markerWin: days && days > 1100 ? 10 : 4,
      tfmt: (v, s) => s.name === t ? F.px(v) + (pts.length ? ` <span class="m">${F.spct(v / pts[0][1] - 1)} since ${ymd(pts[0][0])}</span>` : "") : F.px(v), label: `${t} price`});
  }
  function drawVal() {
    const key = st.val, pts = c.val.filter(v => isNum(v[key]) && (key === "fy" || v[key] > 0) && (key !== "pe" || v[key] < 300)).map(v => [v.t, v[key]]);
    const fmt = key === "fy" ? (v => F.pct(v)) : (v => F.x(v, 1));
    const recent = pts.filter(p => p[0] >= (last ? last[0] : 0) - 5 * 366 * DAY), md = median(recent.map(p => p[1])), now = pts.length ? pts[pts.length - 1][1] : null;
    $("#valnote").innerHTML = isNum(md) && isNum(now) ? `Now ${fmt(now)} vs a 5-year median of ${fmt(md)}: ` + (key === "fy" ? (now > md ? '<b class="pos">cheaper than usual</b>' : '<b>pricier than usual</b>')
      : (now < md ? '<b class="pos">cheaper than usual</b>' : '<b>pricier than usual</b>')) : (fin && key !== "pe" ? "Not meaningful for financials." : "Not enough history.");
    lineChart($("#c-val"), {series: [{name: {pe: "P/E", ps: "P/S", evebit: "EV/EBIT", fy: "FCF yield"}[key], color: css("--hero"), pts}], h: 220, yfmt: fmt,
      refs: isNum(md) ? [{y: md, label: "5-year median"}] : [], gapDays: 60});
  }
  const Q = c.Q, qd = r => r.d;
  const lp = (k, f = v => v) => Q.filter(r => isNum(r[k])).map(r => [qd(r), f(r[k])]);
  function drawFin() {
    barChart($("#c-rev"), {bars: Q.map((r, i) => ({t: r.d, v: r.revenue, tip: `<div class="m">12 months to ${ymd(r.d)}</div>Revenue <b>${F.usd(r.revenue)}</b><br>vs a year earlier ${cls(r.rg, F.spct(r.rg))}`})),
      color: css("--s1"), color2: css("--s1"), yfmt: v => F.usd(v, 1), xfmt: t => "'" + String(new Date(t).getUTCFullYear()).slice(2), h: 210});
    const prof = [{name: "Operating income", color: css("--s1"), pts: lp("ebit")}, {name: "Net income", color: css("--s2"), pts: lp("net_income")}];
    if (!fin) prof.push({name: "Free cash flow", color: css("--s3"), pts: lp("fcf")});
    legend("#l-prof", prof); lineChart($("#c-prof"), {series: prof, h: 210, yfmt: v => F.usd(v, 1), zero: true, endDots: true, datefmt: t => "12 months to " + ymd(t)});
    const mar = [{name: "Gross", color: css("--s1"), pts: lp("gm")}, {name: "Operating", color: css("--s2"), pts: lp("om")}, {name: "Net", color: css("--s3"), pts: lp("nm")}];
    if (!fin) mar.push({name: "Free cash flow", color: css("--s4"), pts: lp("fm")});
    const mar2 = mar.filter(s => s.pts.length); legend("#l-mar", mar2);
    lineChart($("#c-mar"), {series: mar2, h: 210, yfmt: v => F.pct(v, 0), zero: true, endDots: true, datefmt: t => "12 months to " + ymd(t)});
    const ps = [{name: "EPS", color: css("--s1"), pts: lp("eps")}]; if (!fin) ps.push({name: "FCF per share", color: css("--s3"), pts: lp("fcfps")});
    legend("#l-ps", ps); lineChart($("#c-ps"), {series: ps, h: 210, yfmt: v => F.ps(v, Math.abs(v) < 10 ? 2 : 0), zero: true, endDots: true, datefmt: t => "12 months to " + ymd(t)});
    const bs = [{name: "Cash + short-term investments", color: css("--s3"), pts: lp("cash")}, {name: "Debt", color: css("--s2"), pts: lp("debt")}];
    legend("#l-bs", bs); lineChart($("#c-bs"), {series: bs, h: 210, yfmt: v => F.usd(v, 1), zero: true, endDots: true, datefmt: t => "Quarter ended " + ymd(t)});
  }
  window.__redraw = () => { drawPrice(); drawVal(); drawFin(); };
  window.__redraw();
}
function legend(sel, S) { const el = $(sel); if (el) el.innerHTML = S.map(s => `<span><i style="background:${s.color}"></i>${esc(s.name)}</span>`).join(""); }
function newsList(n) { return (n || []).map(h => `<a href="${esc(h.link)}" target="_blank" rel="noopener">${esc(h.title)}<small>${esc(h.src)} · ${esc(h.date)}</small></a>`).join(""); }
function insTable(ins) {
  if (!ins.length) return '<div class="mut" style="padding:6px 0">No open-market trades on file since 2016.</div>';
  return `<table><thead><tr><th class="l">Date</th><th class="l">Insider</th><th class="l">Trade</th><th>Value</th><th>Price</th></tr></thead><tbody>` +
    [...ins].reverse().map(([d, code, v, p, title]) => `<tr><td class="l mono">${esc(d)}</td><td class="l">${esc(title)}</td><td class="l ${code === "P" ? "pos" : "neg"}">${code === "P" ? "Buy" : "Sell"}</td><td>${F.usd(v, 1)}</td><td>${F.px(p)}</td></tr>`).join("") + "</tbody></table>";
}
function finTable(c, per) {
  let Q = [...c.Q].reverse(); if (per === "y") Q = Q.filter((_, i) => i % 4 === 0); Q = Q.slice(0, 12);
  if (!Q.length) return '<div class="empty">No SEC financials on file.</div>';
  const rows = [["Revenue", r => F.usd(r.revenue)], ["Revenue growth, 1Y", r => cls(r.rg, F.spct(r.rg))], ["Gross margin", r => F.pct(r.gm)],
    ["Operating income", r => F.usd(r.ebit)], ["Operating margin", r => F.pct(r.om)], ["Net income", r => F.usd(r.net_income)], ["Net margin", r => F.pct(r.nm)],
    ["EPS", r => isNum(r.eps) ? F.ps(r.eps) : "–"], ["Free cash flow", r => c.fin ? "n/m" : F.usd(r.fcf)], ["Stock-based pay", r => F.usd(r.sbc)],
    ["Diluted shares", r => F.sh(r.shares)], ["Cash", r => F.usd(r.cash)], ["Debt", r => F.usd(r.debt)], ["Equity", r => F.usd(r.equity)],
    [c.fin ? "ROE" : "ROIC", r => F.pct(c.fin ? r.roe : r.roic)], ["Net debt / EBITDA", r => c.fin ? "n/m" : F.x(r.nde)]];
  const shown = rows.filter(([, f]) => Q.some(r => f(r) !== "–"));  // skip lines a filer never reports
  return `<table><thead><tr><th class="l" style="position:sticky;left:0;z-index:2">12 months to</th>${Q.map(r => `<th>${mon(r.d)}</th>`).join("")}</tr></thead><tbody>` +
    shown.map(([l, f]) => `<tr><td class="l" style="position:sticky;left:0;background:var(--panel)">${l}</td>${Q.map(r => `<td class="mono">${f(r)}</td>`).join("")}</tr>`).join("") + "</tbody></table>";
}

// ---------- compare ----------
async function viewCompare(list) {
  if (!list) list = store.get("cmp", []);
  list = [...new Set(list)].slice(0, 4); store.set("cmp", list);
  const setList = l => { location.hash = l.length ? "vs-" + l.join("--") : "compare"; };
  view.innerHTML = `<div><div class="eyebrow">Side by side</div><h1 style="font-size:22px">Compare</h1></div>
   <div class="card cmp-chips" id="chips"></div><div id="cmpbody"><div class="empty">Loading…</div></div>`;
  const my = RSEQ, cos = (await Promise.all(list.map(loadCompany))).map((c, i) => c && Object.assign(c, {color: css(SERIES[i])}));
  if (my !== RSEQ) return;
  const ok = cos.filter(Boolean);
  $("#chips").innerHTML = list.map((t, i) => `<span class="cmp-chip"><i style="background:${css(SERIES[i])}"></i>${esc(t)}${cos[i] ? "" : ' <span class="mut">(no data)</span>'}<button data-x="${esc(t)}" aria-label="Remove ${esc(t)}">✕</button></span>`).join("") +
    (list.length < 4 ? `<span class="search" style="max-width:260px"><input id="cq" type="search" placeholder="Add a ticker" autocomplete="off"><ul id="csugg" class="sugg" hidden></ul></span>` : "") +
    (!list.length ? `<span class="mut">Add up to four companies. Try ${D.rows.filter(r => r.top).slice(0, 3).map(r => `<a href="#vs-${r.t}">${r.t}</a>`).join(", ")}.</span>` : "");
  $$("#chips [data-x]").forEach(b => b.onclick = () => setList(list.filter(x => x !== b.dataset.x)));
  const cq = $("#cq");
  if (cq) { const cs = $("#csugg"); let ci = 0, co = [];
    const show = () => { co = matches(cq.value, 8); cs.innerHTML = co.map((r, i) => `<li role="option" aria-selected="${i === ci}" data-t="${esc(r.t)}"><span class="tk">${esc(r.t)}</span><span class="n">${esc(r.n)}</span><span></span></li>`).join("");
      if (!co.length && /^[A-Za-z.\-]{1,6}$/.test(cq.value.trim())) cs.innerHTML = `<li data-t="${esc(cq.value.trim().toUpperCase())}"><span class="tk">${esc(cq.value.trim().toUpperCase())}</span><span class="n">Try this ticker</span><span></span></li>`;
      cs.hidden = !cs.innerHTML; $$("li", cs).forEach(li => li.onmousedown = e => { e.preventDefault(); setList([...list, li.dataset.t]); }); };
    cq.oninput = () => { ci = 0; show(); };
    cq.onkeydown = e => { if (e.key === "ArrowDown") { ci = Math.min(co.length - 1, ci + 1); show(); e.preventDefault(); } else if (e.key === "ArrowUp") { ci = Math.max(0, ci - 1); show(); e.preventDefault(); }
      else if (e.key === "Enter") { const t = co[ci]?.t || cq.value.trim().toUpperCase(); if (t) setList([...list, t]); } };
    cq.onblur = () => setTimeout(() => { cs.hidden = true; }, 120); }
  if (!ok.length) { $("#cmpbody").innerHTML = ""; window.__redraw = null; return; }
  const st = {range: store.get("crange", "3Y")};
  const M = [
    ["Price", c => F.px(c.close)], ["Market cap", c => F.usd(c.mcap), c => c.mcap],
    ["Implied return", c => F.pct(c.row?.ir), c => c.row?.ir, 1], ["P/E", c => F.x(c.row?.pe ?? div(c.mcap, c.last.net_income)), c => c.row?.pe, -1],
    ["P/S", c => F.x(c.row?.ps ?? div(c.mcap, c.last.revenue)), c => c.row?.ps, -1], ["FCF yield", c => c.fin ? "n/m" : F.pct(c.row?.fy ?? div(c.last.fcf, c.mcap)), c => c.fin ? null : (c.row?.fy ?? div(c.last.fcf, c.mcap)), 1],
    ["Revenue", c => F.usd(c.last.revenue)], ["Revenue growth, 1Y", c => cls(c.last.rg, F.spct(c.last.rg)), c => c.last.rg, 1],
    ["Profit/share, 3Y per yr", c => cls(c.row?.pg3, F.spct(c.row?.pg3)), c => c.row?.pg3, 1], ["Net income", c => F.usd(c.last.net_income)],
    ["Gross margin", c => F.pct(c.last.gm), c => c.last.gm, 1], ["Operating margin", c => F.pct(c.last.om), c => c.last.om, 1], ["Net margin", c => F.pct(c.last.nm), c => c.last.nm, 1],
    ["ROIC (ROE for banks)", c => F.pct(c.fin ? c.last.roe : (c.row?.roic ?? c.last.roic)), c => c.fin ? c.last.roe : (c.row?.roic ?? c.last.roic), 1],
    ["Net debt / EBITDA", c => c.fin ? "n/m" : F.x(c.last.nde), c => c.fin ? null : c.last.nde, -1], ["Interest coverage", c => isNum(c.last.cov) ? F.x(c.last.cov, 0) : "none"],
    ["Price, 1Y", c => cls(changeOver(c.price, 366), F.spct(changeOver(c.price, 366)))], ["Insiders, 6M", c => c.row?.ib >= 1 ? `<span class="pos">${c.row.ib} bought</span>` : "–"],
    ["Screen", c => statusPill(c.row)]];
  const best = (f, dir) => { const v = ok.map(c => f(c)).filter(isNum); return v.length > 1 ? (dir > 0 ? Math.max(...v) : Math.min(...v)) : null; };
  $("#cmpbody").innerHTML = `
   <section class="card" style="margin-bottom:14px"><div class="card-h"><h2>Price, rebased to 100</h2>
     <div class="seg" id="crng">${["6M", "1Y", "3Y", "5Y", "10Y"].map(k => `<button data-k="${k}" aria-pressed="${st.range === k}">${k}</button>`).join("")}</div></div>
    <div class="legend">${ok.map(c => `<span><i style="background:${c.color}"></i>${esc(c.t)}</span>`).join("")}</div><div class="chart" id="cp"></div></section>
   <div class="tw" style="margin-bottom:14px"><table><thead><tr><th class="l"></th>${ok.map(c => `<th><a class="tk" href="#t-${encodeURIComponent(c.t)}" style="color:${c.color}">${esc(c.t)}</a></th>`).join("")}</tr></thead><tbody>
    ${M.map(([l, f, g, dir]) => { const b = g && dir ? best(g, dir) : null; return `<tr><td class="l">${l}</td>${ok.map(c => `<td class="mono ${b != null && g(c) === b ? "best" : ""}">${f(c)}${b != null && g(c) === b ? " ✓" : ""}</td>`).join("")}</tr>`; }).join("")}
   </tbody></table></div>
   <div class="grid3">
    <section class="card"><div class="card-h"><h2>Revenue growth, 1Y</h2></div><div class="chart" id="cc1"></div></section>
    <section class="card"><div class="card-h"><h2>Operating margin</h2></div><div class="chart" id="cc2"></div></section>
    <section class="card"><div class="card-h"><h2>P/E</h2></div><div class="chart" id="cc3"></div></section>
   </div><p class="mut" style="font-size:12px">✓ marks the best value in a row where higher or lower is clearly better.</p>`;
  $$("#crng button").forEach(b => b.onclick = () => { st.range = b.dataset.k; store.set("crange", st.range); $$("#crng button").forEach(x => x.setAttribute("aria-pressed", x === b)); draw(); });
  const draw = () => {
    const end = Math.max(...ok.map(c => c.price.length ? c.price[c.price.length - 1][0] : 0)), x0 = end - {"6M": 183, "1Y": 366, "3Y": 1096, "5Y": 1827, "10Y": 3653}[st.range] * DAY;
    const S = ok.map(c => { const p = c.price.filter(q => q[0] >= x0); const b = p.length ? p[0][1] : 1; return {name: c.t, color: c.color, pts: p.map(q => [q[0], q[1] / b * 100])}; });
    lineChart($("#cp"), {series: S, h: innerWidth < 700 ? 240 : 300, yfmt: v => v.toFixed(0), endLabels: innerWidth > 700, endDots: true, x0, refs: [{y: 100}], tfmt: v => `${v.toFixed(1)} <span class="m">(${F.spct(v / 100 - 1)})</span>`});
    const ser = (k, f = v => v) => ok.map(c => ({name: c.t, color: c.color, pts: c.Q.filter(r => isNum(r[k])).map(r => [r.d, f(r[k])])}));
    lineChart($("#cc1"), {series: ser("rg"), h: 190, yfmt: v => F.pct(v, 0), zero: true, endDots: true, datefmt: t => "12 months to " + ymd(t)});
    lineChart($("#cc2"), {series: ser("om"), h: 190, yfmt: v => F.pct(v, 0), zero: true, endDots: true, datefmt: t => "12 months to " + ymd(t)});
    lineChart($("#cc3"), {series: ok.map(c => ({name: c.t, color: c.color, pts: c.val.filter(v => v.pe > 0 && v.pe < 200).map(v => [v.t, v.pe])})), h: 190, yfmt: v => F.x(v, 0), gapDays: 60});
  };
  window.__redraw = draw; draw();
}

// ---------- theme ----------
$("#theme").onclick = () => { const cur = document.documentElement.dataset.theme || "dark";
  document.documentElement.dataset.theme = cur === "dark" ? "light" : "dark"; store.set("theme", document.documentElement.dataset.theme); window.__redraw && window.__redraw(); };
{ const th = store.get("theme", null); if (th) document.documentElement.dataset.theme = th; }
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => window.__redraw && window.__redraw());

// ---------- boot ----------
(async function boot() {
  try { const p = await fetch("api/ping"); const j = p.ok ? await p.json() : {}; D.server = j.ok === true; D.db = j.db === true; } catch (e) { D.server = false; }
  try {
    const idx = await getJSON(D.server ? "api/index" : "data/index.json");
    D.asof = idx.asof; D.rows = idx.rows; D.news = idx.news || {}; D.changes = idx.changes || null;
    for (const r of D.rows) D.idx[r.t] = r;
  } catch (e) { view.innerHTML = `<div class="card empty">${D.server ? "No screen in the database yet. If this is a new install, the first refresh is still loading (it takes about an hour); reload when it's done." : "Couldn't load the data."} <span class="mut">${esc(e.message)}</span></div>`; return; }
  $$("[data-nav=record]").forEach(a => a.hidden = !D.db);
  $("#foot").innerHTML = `Fundamentals from SEC EDGAR filings; prices from Yahoo Finance, as of ${esc(D.asof)}. ${D.server ? "Live mode: any US ticker can be looked up." : "Covers the S&amp;P 500, 400, 600 and Nasdaq-100."}${D.db ? " Database refreshed every weekday evening." : ""} Not investment advice.`;
  route();
})();
