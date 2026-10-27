"""Interactive explorer: one self-contained HTML page to filter, chart and drill into a screen.

Everything (data, charts, styles) is inlined, so the file opens offline and can be emailed.
Charts are hand-built SVG: no CDN, no build step.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from . import report, score, universe

# Guiding principles for the analysis (also in README). Shown on the Method tab.
PRINCIPLES = [
    ("Thesis first",
     "We hunt small and mid-cap US companies that make money, are growing profits and revenue per share, and keep "
     "debt under control, bought when they are cheap. Every rule has to serve that thesis."),
    ("Rules earn their place",
     "A new gate or ranking factor goes in only if it beats the current default in a point-in-time backtest, in both "
     "halves of the period, without a worse drawdown. Otherwise it is shown as context, not used to rank. Core thesis "
     "gates (profitable, growing, managed debt) stay even when the backtest is neutral, as risk screens."),
    ("Point-in-time, fail closed",
     "Only data that was public on the date counts (SEC filing dates). If a number is missing or can't be verified, "
     "the company fails that gate."),
    ("Owner economics, per share",
     "Growth is measured per share, so growth bought by issuing stock doesn't count. Free cash flow is after "
     "stock-based pay."),
    ("Simple beats clever",
     "One transparent ranking (implied return), few parameters, round-number thresholds. We count how many variants "
     "we have tried and discount the winner, because the best of 30 backtests is partly luck."),
    ("Investigate anomalies, don't rank them",
     "Numbers that look off (cash flow far from earnings, stale filings, Yahoo-filled data) get a flag and a manual "
     "check before any money moves."),
    ("Honest about uncertainty",
     "Report t-stats, survivorship bias and how fast the edge decays. An edge with a t-stat near 1 is a hypothesis, "
     "not proof."),
    ("Live prices, tracked record",
     "Screens use today's prices and every run is saved, so the forward (out-of-sample) record is the final judge. "
     "Rerun monthly; the edge fades after about a month."),
    ("The screen finds candidates; you decide",
     "It puts names on the radar. Read the filings before buying, and spread risk across many names."),
]

GATES = {  # filter column -> plain words (also used for near misses)
    "mcap": "market cap in range", "price": "price over $5", "liquidity": "daily trading over $2M",
    "fresh_data": "recent filing", "profitable": "profitable", "fcf_positive": "free cash flow positive",
    "low_leverage": "net debt under 1x EBITDA", "no_red_flags": "no restatement or bankruptcy filing",
    "price_to_sales": "P/S ceiling", "net_margin": "net margin 5%+", "us_company": "US headquarters",
    "revenue_growing": "revenue per share growing", "quality": "ROIC 10%+", "margins_holding": "margins holding",
    "debt_discipline": "debt not ballooning", "business_model": "not a mortgage REIT", "fscore": "Piotroski floor",
    "profits_growing": "profit per share up over 3 years",
}


def _clean(v):
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return None
    if isinstance(v, (bool,)):
        return v
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, float):
        return float(f"{v:.5g}")
    return v


def _records(s: pd.DataFrame, news: dict, ciks: pd.Series) -> list[dict]:
    fin = s["is_financial"].astype(bool)
    passed = s[s["passes"]]
    core, fins = passed[~fin.loc[passed.index]], passed[fin.loc[passed.index]]
    rank = {}
    for grp in (core, fins):
        rank.update({t: i + 1 for i, t in enumerate(grp.sort_values("composite", ascending=False).index)})
    top = set(score.top_n(core, "composite", 25).index)
    cols = {"px": "price", "mc": "mcap", "ir": "implied_return", "cy": "cash_yield", "g": "growth_rate",
            "pe": "pe", "ps": "ps", "rev": "revenue", "ni": "net_income", "nm": "net_margin", "rg": "rev_growth",
            "rg3": "rev_cagr_3y", "nig": "ni_growth", "nd": "net_debt", "nde": "nd_ebitda",
            "cov": "interest_coverage", "fcf": "fcf", "fy": "fcf_yield", "ey": "earnings_yield", "r1y": "ret_1y",
            "off": "off_high", "ib": "ins_buyers", "ibu": "ins_buy_usd", "isu": "ins_sell_usd",
            "inp": "ins_net_pct", "mg": "margin_change", "pg3": "profit_ps_cagr_3y"}
    out = []
    for t, r in s.iterrows():
        rec = {"t": t, "n": report._name(r.get("sec_name") or t), "sec": r.get("sector") or "",
               "ind": r.get("industry") or ""}
        for k, c in cols.items():
            rec[k] = _clean(r.get(c))
        rec["roic"] = _clean(r.get("roe") if fin[t] else r.get("roic"))
        rec["fin"] = bool(fin[t])
        rec["pass"] = bool(r.get("passes"))
        rec["top"] = t in top
        rec["rank"] = rank.get(t)
        rec["failed"] = [f for f in str(r.get("failed") or "").split(",") if f.strip()]
        rec["failed"] = [f.strip() for f in rec["failed"]]
        rec["why"] = report.why(r)
        rec["flags"] = [f for f in report._flags(r).split("; ") if f]
        rec["cyc"] = bool(r.get("cyclical"))
        c = ciks.get(t)
        rec["cik"] = int(c) if c is not None and not pd.isna(c) else None
        if news and news.get(t):
            rec["news"] = [{"title": h["title"], "src": h["source"], "link": h["link"],
                            "date": f"{h['date']:%b %d}" if pd.notna(h["date"]) else ""} for h in news[t]]
        out.append(rec)
    return out


def _backtest(out_dir: Path) -> dict | None:
    holds = {}
    for h in (1, 3, 12):
        f = out_dir / f"backtest_returns_hold{h}m.csv"
        if not f.exists():
            continue
        r = pd.read_csv(f, index_col=0, parse_dates=True)
        holds[h] = {"dates": [f"{d:%Y-%m}" for d in r.index],
                    "series": {c: [None if pd.isna(v) else round(float(v), 5) for v in r[c]] for c in r.columns}}
    return holds or None


def write(scored: pd.DataFrame, out_dir: Path, news: dict | None = None) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    d = pd.Timestamp(scored["date"].iloc[0]).date()
    s = scored[scored["investable"]].sort_values("composite", ascending=False)
    ciks = universe.load().set_index("ticker")["cik"]
    data = {
        "asof": str(d),
        "rows": _records(s, news or {}, ciks),
        "bt": _backtest(out_dir),
        "principles": [{"h": h, "b": b} for h, b in PRINCIPLES],
        "gates": GATES,
        "settings": {"min_mcap": score.MIN_MCAP, "max_mcap": score.MAX_MCAP, "min_nm": score.MIN_NET_MARGIN,
                     "max_nde": score.MAX_ND_EBITDA, "min_roic": score.MIN_RETURN_ON_CAPITAL,
                     "fcf_cap": score.FCF_CAP, "max_growth": score.MAX_GROWTH},
    }
    blob = json.dumps(data, separators=(",", ":"), default=str).replace("</", "<\\/")
    page = PAGE.replace("__ASOF__", str(d)).replace("__DATA__", blob)
    p = out_dir / f"explorer_{d}.html"
    p.write_text(page)
    return p


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bargain Screen Explorer</title>
<style>
:root{--page:#f4f3ef;--surface:#fcfcfb;--ink:#0b0b0b;--ink2:#52514e;--muted:#6f6e69;--grid:#e1e0d9;--axis:#c3c2b7;
--border:#e1e0d9;--stripe:#f6f5f1;--hover:#eceae4;--s1:#2a78d6;--s1b:#9ec5f4;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;
--neutral:#c3c2b7;--good:#0ca30c;--goodbg:#e3f3e1;--warn:#a86f00;--warnbg:#fdf0d2;--crit:#d03b3b;--critbg:#fbe4e2;
--up:#0a7d0a;--down:#c0392b;color-scheme:light}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--page:#121211;--surface:#1a1a19;--ink:#f0efec;
--ink2:#c3c2b7;--muted:#9a9890;--grid:#2c2c2a;--axis:#383835;--border:#2f2f2c;--stripe:#1f1f1d;--hover:#292927;
--s1:#3987e5;--s1b:#1c5cab;--s2:#d95926;--s3:#199e70;--s4:#c98500;--neutral:#55544f;--goodbg:#14301a;--warn:#fab219;
--warnbg:#3a2d0c;--critbg:#3d1716;--up:#3cc13c;--down:#f07167;color-scheme:dark}}
:root[data-theme="dark"]{--page:#121211;--surface:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--muted:#9a9890;--grid:#2c2c2a;
--axis:#383835;--border:#2f2f2c;--stripe:#1f1f1d;--hover:#292927;--s1:#3987e5;--s1b:#1c5cab;--s2:#d95926;--s3:#199e70;
--s4:#c98500;--neutral:#55544f;--goodbg:#14301a;--warn:#fab219;--warnbg:#3a2d0c;--critbg:#3d1716;--up:#3cc13c;
--down:#f07167;color-scheme:dark}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
a{color:var(--s1)}
header{padding:18px 24px 0;max-width:1440px;margin:0 auto}
h1{font-size:22px;margin:0 0 2px;font-weight:650;letter-spacing:-.01em}
.sub{color:var(--ink2);margin:0 0 12px}
.tabs{display:flex;gap:4px;border-bottom:1px solid var(--border);margin-top:8px;overflow-x:auto}
.tabs button{white-space:nowrap}
.tabs button{background:none;border:0;border-bottom:2px solid transparent;padding:8px 14px;font:inherit;color:var(--ink2);cursor:pointer}
.tabs button[aria-selected=true]{color:var(--ink);border-bottom-color:var(--s1);font-weight:600}
.theme{margin-left:auto;align-self:center}
main{max-width:1440px;margin:0 auto;padding:16px 24px 60px}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:14px 16px}
.filters{display:flex;flex-direction:column;gap:10px;margin-bottom:14px}
.row{display:flex;flex-wrap:wrap;gap:8px;align-items:center}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:8px;overflow:hidden}
.seg button{background:var(--surface);border:0;border-right:1px solid var(--border);padding:6px 11px;font:inherit;font-size:13px;color:var(--ink2);cursor:pointer}
.seg button:last-child{border-right:0}
.seg button[aria-pressed=true]{background:var(--s1);color:#fff}
input[type=search],input[type=number],select{font:inherit;font-size:13px;padding:6px 8px;border:1px solid var(--border);border-radius:7px;background:var(--surface);color:var(--ink)}
input[type=search]{min-width:220px}
input[type=number]{width:74px}
.chip{border:1px solid var(--border);background:var(--surface);color:var(--ink2);border-radius:999px;padding:4px 10px;font-size:12.5px;cursor:pointer}
.chip[aria-pressed=true]{background:var(--ink);color:var(--surface);border-color:var(--ink)}
label.tg{display:inline-flex;gap:5px;align-items:center;font-size:13px;color:var(--ink2);cursor:pointer;padding:4px 2px}
.btn{font:inherit;font-size:13px;padding:6px 12px;border:1px solid var(--border);border-radius:7px;background:var(--surface);color:var(--ink);cursor:pointer}
.btn:hover,.chip:hover{border-color:var(--axis)}
details.more summary{cursor:pointer;color:var(--ink2);font-size:13px}
.ranges{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px 18px;margin-top:10px}
.rg{display:flex;align-items:center;gap:6px;font-size:13px}
.rg span.l{flex:1;color:var(--ink2)}
.count{margin-left:auto;color:var(--ink2);font-size:13px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.kpi .v{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums;letter-spacing:-.01em}
.kpi .k{color:var(--ink2);font-size:12.5px}
.grid{display:grid;grid-template-columns:2fr 1fr;gap:14px;margin-bottom:14px}
.stack{display:flex;flex-direction:column;gap:14px}
.ch h3{margin:0 0 2px;font-size:14px;font-weight:600}
.ch .note{color:var(--muted);font-size:12px;margin:0 0 6px}
.ch .ctl{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:12.5px;color:var(--ink2);margin-bottom:6px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12.5px;color:var(--ink2);margin:4px 0}
.legend i{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:5px;vertical-align:-1px}
.legend i.ln{border-radius:2px;height:3px;width:14px;vertical-align:3px}
svg{display:block;width:100%;overflow:visible}
svg text{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
svg .lab{fill:var(--ink2);font-size:11.5px}
svg .lab2{fill:var(--ink);font-size:12px;font-weight:600}
.gl{stroke:var(--grid);stroke-width:1}
.ax{stroke:var(--axis);stroke-width:1}
.tip{position:fixed;pointer-events:none;background:var(--surface);color:var(--ink);border:1px solid var(--border);border-radius:8px;padding:8px 10px;font-size:12.5px;box-shadow:0 4px 18px rgba(0,0,0,.14);z-index:50;max-width:280px;display:none}
.tip b{font-weight:650}
.tip .m{color:var(--ink2)}
.tblwrap{overflow:auto;max-height:78vh;border:1px solid var(--border);border-radius:10px;background:var(--surface)}
table{border-collapse:separate;border-spacing:0;width:100%;font-variant-numeric:tabular-nums;font-size:13px}
th{position:sticky;top:0;background:var(--surface);z-index:2;text-align:right;font-weight:600;color:var(--ink2);padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap;cursor:pointer;user-select:none}
th.l,td.l{text-align:left}
th[aria-sort]::after{content:" ▾";color:var(--s1)}
th[aria-sort=ascending]::after{content:" ▴"}
td{padding:7px 10px;text-align:right;white-space:nowrap;border-bottom:1px solid var(--border)}
tbody tr:nth-child(even) td{background:var(--stripe)}
tbody tr:hover td{background:var(--hover)}
tbody tr{cursor:pointer}
tbody tr.sel td{background:var(--hover);box-shadow:inset 0 0 0 0 var(--s1)}
td.tk,th.tk{position:sticky;left:0;z-index:1;background:inherit}
th.tk{z-index:3}
tbody tr td.tk{background:var(--surface)}
tbody tr:nth-child(even) td.tk{background:var(--stripe)}
td.tk a{font-weight:650;text-decoration:none}
td.nm{max-width:210px;overflow:hidden;text-overflow:ellipsis}
.irbar{display:inline-flex;align-items:center;gap:6px}
.irbar .b{display:inline-flex;width:70px;height:8px;border-radius:4px;background:var(--grid);overflow:hidden}
.irbar .b i{display:block;height:100%}
.pos{color:var(--up)}.neg{color:var(--down)}.mut{color:var(--muted)}
.pill{display:inline-block;border-radius:999px;padding:1px 8px;font-size:11.5px;margin:1px 2px 1px 0;background:var(--hover);color:var(--ink2);white-space:nowrap}
.pill.good{background:var(--goodbg);color:var(--up)}
.pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.bad{background:var(--critbg);color:var(--down)}
.pill.top{background:var(--s1);color:#fff}
.drawer{position:fixed;top:0;right:0;height:100%;width:min(440px,100%);background:var(--surface);border-left:1px solid var(--border);box-shadow:-8px 0 30px rgba(0,0,0,.12);transform:translateX(105%);transition:transform .18s ease;z-index:40;overflow:auto;padding:18px 20px 40px}
.drawer.open{transform:none}
.drawer h2{margin:0;font-size:20px}
.drawer .x{position:absolute;top:12px;right:14px}
.drawer .links{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}
.drawer .links a{font-size:13px;border:1px solid var(--border);border-radius:7px;padding:4px 9px;text-decoration:none}
.mgrid{display:grid;grid-template-columns:1fr 1fr;gap:6px 16px;margin:12px 0}
.mgrid div{display:flex;justify-content:space-between;border-bottom:1px dashed var(--border);padding:3px 0;font-size:13px}
.mgrid div span:first-child{color:var(--ink2)}
.mgrid div span:last-child{font-variant-numeric:tabular-nums;font-weight:550}
.drawer h4{margin:16px 0 6px;font-size:13px;color:var(--ink2);font-weight:600;text-transform:uppercase;letter-spacing:.04em}
.news a{display:block;text-decoration:none;color:var(--ink);margin-bottom:8px}
.news a small{display:block;color:var(--muted)}
.hidden{display:none!important}
.method{max-width:900px}
.method ol{padding-left:20px}
.method li{margin-bottom:10px}
.method li b{display:block}
.method dl{display:grid;grid-template-columns:200px 1fr;gap:6px 14px}
.method dt{font-weight:600}
.method dd{margin:0;color:var(--ink2)}
.grp td{background:var(--page)!important;font-weight:600;color:var(--ink2);text-align:left;cursor:default}
tr.cur td{box-shadow:inset 3px 0 0 var(--s4)}
.caveat{font-size:12.5px;color:var(--ink2);margin:10px 0 0}
@media (max-width:900px){.grid{grid-template-columns:1fr}header,main{padding-left:16px;padding-right:16px}
input[type=search]{min-width:0;flex:1}.method dl{grid-template-columns:1fr}.mgrid{grid-template-columns:1fr}
.count{margin-left:0;width:100%}}
</style></head>
<body>
<header>
<h1>Bargain screen explorer</h1>
<p class="sub" id="sub"></p>
<div class="tabs" role="tablist">
<button role="tab" aria-selected="true" data-tab="screen">Screen</button>
<button role="tab" aria-selected="false" data-tab="bt">Backtest</button>
<button role="tab" aria-selected="false" data-tab="method">Method &amp; principles</button>
<button class="btn theme" id="theme" title="Switch light / dark">◐ Theme</button>
</div>
</header>
<main>
<section id="tab-screen">
 <div class="card filters">
  <div class="row">
   <input type="search" id="q" placeholder="Search ticker, company or industry">
   <div class="seg" id="mode"></div>
   <div class="seg" id="group"></div>
   <button class="btn" id="reset">Reset</button>
   <span class="count" id="count"></span>
  </div>
  <div class="row" id="toggles"></div>
  <div class="row" id="sectors"></div>
  <details class="more" id="more"><summary>Ranges: price, market cap, profit, growth, debt and more</summary><div class="ranges" id="ranges"></div></details>
 </div>
 <div class="kpis" id="kpis"></div>
 <div class="grid">
  <div class="card ch"><h3 id="sc-title">Valuation map</h3>
   <div class="ctl">x <select id="sx"></select> y <select id="sy"></select></div>
   <div class="legend" id="sc-leg"></div>
   <div id="scatter"></div><p class="note" id="sc-note"></p></div>
  <div class="stack">
   <div class="card ch"><h3>Implied return, distribution</h3><p class="note">Companies shown, by implied return.</p><div id="hist"></div></div>
   <div class="card ch"><h3>By sector</h3><p class="note">Click a bar to filter. Counts ignore the sector filter.</p><div id="sect"></div></div>
  </div>
 </div>
 <div class="tblwrap"><table id="tbl"><thead></thead><tbody></tbody></table></div>
</section>
<section id="tab-bt" class="hidden">
 <div class="card filters"><div class="row">
  <span style="color:var(--ink2);font-size:13px">Hold each monthly pick for</span><div class="seg" id="hold"></div>
  <span style="color:var(--ink2);font-size:13px">Compare</span><select id="cmp"></select>
  <div class="seg" id="scale"></div>
 </div></div>
 <div class="kpis" id="btk"></div>
 <div class="card ch" style="margin-bottom:14px"><h3>Growth of $1</h3><p class="note" id="bt-note"></p>
  <div class="legend" id="bt-leg"></div><div id="eq"></div></div>
 <div class="card ch" style="margin-bottom:14px"><h3>Drawdown from peak</h3><p class="note">How far each strategy fell from its prior high.</p>
  <div id="dd"></div></div>
 <div class="tblwrap" style="max-height:none"><table id="vt"><thead></thead><tbody></tbody></table></div>
 <p class="caveat">Backtest: month-end rebalance on today's S&amp;P 400 + 600 members (survivorship bias inflates every absolute number), fundamentals as filed at each date, equal weight, 10 bps costs. Excess is vs the equal-weight investable universe; t below about 2 means the gap could be luck. Click a row to plot it.</p>
</section>
<section id="tab-method" class="hidden"><div class="card method" id="method"></div></section>
</main>
<div class="tip" id="tip"></div>
<aside class="drawer" id="drawer" aria-label="Company detail"></aside>
<script>
const D = __DATA__;
const $ = s => document.querySelector(s), $$ = s => [...document.querySelectorAll(s)];
const NS = "http://www.w3.org/2000/svg";
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const isNum = v => typeof v === "number" && isFinite(v);
const F = {
  pct: (v, d = 1) => isNum(v) ? (v * 100).toFixed(d) + "%" : "–",
  spct: (v, d = 0) => isNum(v) ? (v > 0 ? "+" : "") + (v * 100).toFixed(d) + "%" : "–",
  usd: v => { if (!isNum(v)) return "–"; const a = Math.abs(v), s = v < 0 ? "−$" : "$";
    return a >= 1e9 ? s + (a / 1e9).toFixed(2) + "B" : a >= 1e6 ? s + (a / 1e6).toFixed(0) + "M" : s + (a / 1e3).toFixed(0) + "K"; },
  x: (v, d = 1) => isNum(v) ? v.toFixed(d) + "×" : "–",
  n: (v, d = 1) => isNum(v) ? v.toFixed(d) : "–",
  px: v => isNum(v) ? "$" + v.toFixed(2) : "–",
};
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const median = a => { const b = a.filter(isNum).sort((x, y) => x - y); if (!b.length) return null;
  const m = b.length >> 1; return b.length % 2 ? b[m] : (b[m - 1] + b[m]) / 2; };
const q = (a, p) => { const b = a.filter(isNum).sort((x, y) => x - y); if (!b.length) return null;
  const i = (b.length - 1) * p, lo = Math.floor(i); return b[lo] + (b[Math.min(lo + 1, b.length - 1)] - b[lo]) * (i - lo); };
function el(tag, attrs = {}, parent) { const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]); if (parent) parent.appendChild(e); return e; }
function ticks(lo, hi, n = 5) { const span = hi - lo || 1, step0 = span / n, mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(s => span / s <= n) || 10 * mag;
  const out = []; for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-12; v += step) out.push(+v.toFixed(10)); return out; }
const tip = $("#tip");
function showTip(html, ev) { tip.innerHTML = html; tip.style.display = "block";
  const w = tip.offsetWidth, h = tip.offsetHeight; let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14; if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  tip.style.left = x + "px"; tip.style.top = y + "px"; }
const hideTip = () => tip.style.display = "none";

const ROWS = D.rows;
const nPass = ROWS.filter(r => r.pass && !r.fin).length, nFin = ROWS.filter(r => r.pass && r.fin).length;
$("#sub").innerHTML = `Prices as of ${D.asof}. ${nPass} companies pass every gate (plus ${nFin} financials, ranked separately) out of ${ROWS.length} investable US small and mid caps. Ranked by implied return.`;

// ---------- filter state (kept in the URL so a view can be shared) ----------
const RANGES = [
  {k: "px", l: "Price", u: "$", s: 1}, {k: "mc", l: "Market cap", u: "$B", s: 1e9},
  {k: "ni", l: "Net income", u: "$M", s: 1e6}, {k: "rev", l: "Revenue", u: "$M", s: 1e6},
  {k: "pg3", l: "Profit/share growth, 3Y per yr", u: "%", s: .01}, {k: "nig", l: "Net income growth, 1Y", u: "%", s: .01},
  {k: "rg", l: "Revenue growth", u: "%", s: .01},
  {k: "nm", l: "Net margin", u: "%", s: .01}, {k: "roic", l: "ROIC (ROE for banks)", u: "%", s: .01},
  {k: "pe", l: "P/E", u: "×", s: 1}, {k: "ps", l: "P/S", u: "×", s: 1},
  {k: "nde", l: "Net debt / EBITDA", u: "×", s: 1}, {k: "cov", l: "Interest coverage", u: "×", s: 1},
  {k: "ir", l: "Implied return", u: "%", s: .01}, {k: "r1y", l: "1-year price change", u: "%", s: .01},
  {k: "off", l: "Below 52-week high", u: "%", s: .01},
];
const TOGGLES = [
  {k: "ins", l: "Insiders bought (6M)", f: r => r.ib >= 1},
  {k: "cluster", l: "2+ insiders bought", f: r => r.ib >= 2},
  {k: "nigrow", l: "Net income growing", f: r => r.nig > 0},
  {k: "netcash", l: "Net cash", f: r => isNum(r.nd) && r.nd <= 0},
  {k: "cov5", l: "Interest covered 5×+", f: r => r.cov == null || r.cov >= 5},
  {k: "noamber", l: "Hide amber flags", f: r => !r.flags.some(isWarn)},
  {k: "nocyc", l: "Hide cyclicals", f: r => !r.cyc},
];
const MODES = [["top", "Top 25"], ["pass", "All passing"], ["near", "Near misses"], ["all", "All investable"]];
const GROUPS = [["co", "Companies"], ["fin", "Financials"], ["both", "Both"]];
const DEF = {q: "", mode: "pass", group: "co", tg: [], sec: [], r: {}, sort: "ir", dir: -1, sx: "cy", sy: "g"};
let S = JSON.parse(JSON.stringify(DEF));
try { const h = decodeURIComponent(location.hash.slice(1)); if (h.startsWith("{")) S = Object.assign(S, JSON.parse(h)); } catch (e) {}
const save = () => { try { history.replaceState(null, "", "#" + encodeURIComponent(JSON.stringify(S))); } catch (e) {} };

const isWarn = f => /FCF|CHECK|cyclical|auditor|listing|impairment|RED FLAG/.test(f);
const flagCls = f => /RED FLAG/.test(f) ? "bad" : /insider cluster/.test(f) ? "good" : isWarn(f) ? "warn" : "";

function base(r) {
  if (S.group === "co" && r.fin) return false;
  if (S.group === "fin" && !r.fin) return false;
  if (S.mode === "top" && !(r.top || (r.fin && r.pass && r.rank <= 10))) return false;
  if (S.mode === "pass" && !r.pass) return false;
  if (S.mode === "near" && !(r.failed.length === 1 && !["business_model", "no_red_flags", "us_company"].includes(r.failed[0]))) return false;
  if (S.q) { const t = S.q.toLowerCase(); if (!(r.t.toLowerCase().includes(t) || r.n.toLowerCase().includes(t) || r.ind.toLowerCase().includes(t))) return false; }
  for (const k of S.tg) { const t = TOGGLES.find(x => x.k === k); if (t && !t.f(r)) return false; }
  for (const [k, [lo, hi]] of Object.entries(S.r)) {
    const R = RANGES.find(x => x.k === k), v = r[k];
    if (lo !== "" && lo != null) { if (!isNum(v) || v < lo * R.s) return false; }
    if (hi !== "" && hi != null) { if (!isNum(v) || v > hi * R.s) return false; }
  }
  return true;
}
const visible = () => ROWS.filter(r => base(r) && (!S.sec.length || S.sec.includes(r.sec)));

// ---------- controls ----------
function seg(id, opts, key, cb) {
  const box = $(id); box.innerHTML = "";
  for (const [v, l] of opts) { const b = document.createElement("button"); b.textContent = l;
    b.setAttribute("aria-pressed", S[key] === v); b.onclick = () => { S[key] = v; cb(); }; box.appendChild(b); }
}
function buildControls() {
  seg("#mode", MODES, "mode", update); seg("#group", GROUPS, "group", update);
  $("#q").value = S.q;
  const tg = $("#toggles"); tg.innerHTML = "";
  for (const t of TOGGLES) { const lab = document.createElement("label"); lab.className = "tg";
    lab.innerHTML = `<input type="checkbox" ${S.tg.includes(t.k) ? "checked" : ""}> ${t.l}`;
    lab.querySelector("input").onchange = e => { S.tg = e.target.checked ? [...S.tg, t.k] : S.tg.filter(x => x !== t.k); update(); };
    tg.appendChild(lab); }
  const rg = $("#ranges"); rg.innerHTML = "";
  for (const R of RANGES) { const [lo, hi] = S.r[R.k] || ["", ""]; const d = document.createElement("div"); d.className = "rg";
    d.innerHTML = `<span class="l">${R.l} <span class="mut">(${R.u})</span></span><input type="number" step="any" placeholder="min" value="${lo ?? ""}"><input type="number" step="any" placeholder="max" value="${hi ?? ""}">`;
    const [a, b] = d.querySelectorAll("input");
    const on = () => { const v = [a.value === "" ? "" : +a.value, b.value === "" ? "" : +b.value];
      if (v[0] === "" && v[1] === "") delete S.r[R.k]; else S.r[R.k] = v; update(); };
    a.oninput = on; b.oninput = on; rg.appendChild(d); }
  if (Object.keys(S.r).length) $("#more").open = true;
  const ax = {cy: "Cash yield", pe: "P/E", ps: "P/S", nde: "Net debt / EBITDA", mc: "Market cap (log)", r1y: "1-year price change", off: "Below 52-week high"};
  const ay = {g: "Growth per share", pg3: "Profit/share growth, 3Y", nig: "Net income growth, 1Y", rg: "Revenue growth", roic: "ROIC", nm: "Net margin", ir: "Implied return"};
  $("#sx").innerHTML = Object.entries(ax).map(([k, l]) => `<option value="${k}" ${S.sx === k ? "selected" : ""}>${l}</option>`).join("");
  $("#sy").innerHTML = Object.entries(ay).map(([k, l]) => `<option value="${k}" ${S.sy === k ? "selected" : ""}>${l}</option>`).join("");
}
$("#q").oninput = e => { S.q = e.target.value; update(); };
$("#sx").onchange = e => { S.sx = e.target.value; update(); };
$("#sy").onchange = e => { S.sy = e.target.value; update(); };
$("#reset").onclick = () => { S = JSON.parse(JSON.stringify(DEF)); buildControls(); update(); };

function sectorChips(rows) {
  const box = $("#sectors"); const secs = [...new Set(ROWS.map(r => r.sec))].filter(Boolean).sort();
  box.innerHTML = `<span style="font-size:13px;color:var(--ink2)">Sectors</span>`;
  for (const s of secs) { const b = document.createElement("button"); b.className = "chip"; b.textContent = s;
    b.setAttribute("aria-pressed", S.sec.includes(s)); b.onclick = () => toggleSec(s); box.appendChild(b); }
}
const toggleSec = s => { S.sec = S.sec.includes(s) ? S.sec.filter(x => x !== s) : [...S.sec, s]; update(); };

// ---------- KPIs ----------
function kpis(rows) {
  const sum = rows.reduce((a, r) => a + (isNum(r.ni) ? r.ni : 0), 0);
  const k = [
    [rows.length, "companies shown"],
    [F.pct(median(rows.map(r => r.ir))), "median implied return"],
    [F.x(median(rows.map(r => r.pe))), "median P/E"],
    [F.spct(median(rows.map(r => r.pg3))), "median profit/share growth, 3Y per yr"],
    [F.x(median(rows.map(r => r.nde))), "median net debt / EBITDA"],
    [rows.filter(r => isNum(r.nd) && r.nd <= 0).length, "with net cash"],
    [rows.filter(r => r.ib >= 1).length, "with insider buying (6M)"],
  ];
  $("#kpis").innerHTML = k.map(([v, l]) => `<div class="card kpi"><div class="v">${v}</div><div class="k">${l}</div></div>`).join("");
}

// ---------- scatter ----------
const AXF = {cy: F.pct, g: F.pct, nig: v => F.spct(v), rg: v => F.spct(v), roic: F.pct, nm: F.pct, ir: F.pct,
  pg3: v => F.spct(v), pe: v => F.x(v, 0), ps: v => F.x(v, 1), nde: v => F.x(v, 1), mc: F.usd, r1y: v => F.spct(v), off: v => F.pct(v, 0)};
function cat(r) { return r.top ? 0 : r.pass ? 1 : 2; }
const CATS = ["Top 25", "Passes all gates", "Fails a gate"];
function scatter(rows) {
  const box = $("#scatter"); box.innerHTML = "";
  const W = box.clientWidth || 700, H = Math.max(300, Math.min(440, W * .58)), m = {l: 52, r: 18, t: 10, b: 38};
  const kx = S.sx, ky = S.sy, logx = kx === "mc";
  const pts = rows.filter(r => isNum(r[kx]) && isNum(r[ky]) && (!logx || r[kx] > 0));
  const colors = [css("--s1"), css("--s2"), css("--neutral")];
  $("#sc-leg").innerHTML = CATS.map((c, i) => `<span><i style="background:${colors[i]}"></i>${c}</span>`).join("") +
    `<span class="mut">Dot size = market cap</span>`;
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, height: H, role: "img", "aria-label": "Scatter of companies"}, box);
  if (!pts.length) { el("text", {x: W / 2, y: H / 2, "text-anchor": "middle"}, svg).textContent = "No companies match the filters"; $("#sc-note").textContent = ""; return; }
  const tx = v => logx ? Math.log10(v) : v;
  let xs = pts.map(r => tx(r[kx])), ys = pts.map(r => r[ky]);
  let x0 = q(xs, .02), x1 = q(xs, .98), y0 = q(ys, .02), y1 = q(ys, .98);
  if (!logx) { x0 = Math.min(x0, 0); } y0 = Math.min(y0, 0);
  const px = (x1 - x0) * .06 || 1, py = (y1 - y0) * .06 || .01; x0 -= px; x1 += px; y0 -= py; y1 += py;
  const X = v => m.l + (tx(v) - x0) / (x1 - x0) * (W - m.l - m.r), Y = v => H - m.b - (v - y0) / (y1 - y0) * (H - m.t - m.b);
  const xt = logx ? [3e8, 5e8, 1e9, 2e9, 5e9, 1e10, 2e10].filter(v => Math.log10(v) >= x0 && Math.log10(v) <= x1) : ticks(x0, x1, 6);
  for (const t of ticks(y0, y1, 5)) { el("line", {x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t), class: t === 0 ? "ax" : "gl"}, svg);
    el("text", {x: m.l - 6, y: Y(t) + 4, "text-anchor": "end"}, svg).textContent = AXF[ky](t); }
  for (const t of xt) { const xx = logx ? m.l + (Math.log10(t) - x0) / (x1 - x0) * (W - m.l - m.r) : X(t);
    el("line", {x1: xx, x2: xx, y1: m.t, y2: H - m.b, class: (!logx && t === 0) ? "ax" : "gl"}, svg);
    el("text", {x: xx, y: H - m.b + 16, "text-anchor": "middle"}, svg).textContent = AXF[kx](t); }
  el("text", {x: W - m.r, y: H - 4, "text-anchor": "end", class: "lab"}, svg).textContent = $("#sx").selectedOptions[0].text + " →";
  el("text", {x: m.l, y: m.t + 2, class: "lab", "text-anchor": "start", dx: 6, dy: 8}, svg).textContent = "↑ " + $("#sy").selectedOptions[0].text;
  const clipId = "c" + Math.random().toString(36).slice(2);
  const cp = el("clipPath", {id: clipId}, el("defs", {}, svg)); el("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b}, cp);
  const g = el("g", {"clip-path": `url(#${clipId})`}, svg);
  if (kx === "cy" && ky === "g") { // lines of equal implied return (cash yield + growth)
    for (const c of [.1, .2, .3, .4]) { const a = Math.max(x0, c - y1), b = Math.min(x1, c - y0); if (a >= b) continue;
      el("line", {x1: X(a), y1: Y(c - a), x2: X(b), y2: Y(c - b), stroke: css("--axis"), "stroke-dasharray": "4 4"}, g);
      const lx = Math.min(b, c - y0 - (y1 - y0) * .04);
      const t = el("text", {x: X(Math.max(a, c - y1 + (y1 - y0) * .1)) + 4, y: Y(Math.min(y1, c - a) - (y1 - y0) * .1) , class: "lab"}, g);
      t.textContent = `${Math.round(c * 100)}% implied`; }
  }
  const mx = Math.max(...pts.map(r => r.mc || 0));
  const R = r => 3 + 8 * Math.sqrt((r.mc || 0) / mx);
  const order = [...pts].sort((a, b) => cat(b) - cat(a) || (b.mc || 0) - (a.mc || 0));
  const surf = css("--surface");
  for (const r of order) { const c = el("circle", {cx: X(r[kx]), cy: Y(r[ky]), r: R(r), fill: colors[cat(r)], "fill-opacity": cat(r) === 2 ? .7 : .9, stroke: surf, "stroke-width": 1.5}, g);
    if (r.t === SEL) { c.setAttribute("stroke", css("--ink")); c.setAttribute("stroke-width", 2.5); } }
  // selective direct labels: highest implied return among the coloured dots, skipping collisions
  const boxes = []; const lab = [...pts].filter(r => cat(r) < 2 || r.t === SEL).sort((a, b) => (b.t === SEL) - (a.t === SEL) || (b.ir ?? -1) - (a.ir ?? -1)).slice(0, 10);
  for (const r of lab) { const x = X(r[kx]) + R(r) + 3, y = Y(r[ky]) + 4, w = r.t.length * 7 + 2;
    if (x < m.l || x + w > W - m.r || y < m.t + 8 || y > H - m.b) continue;
    if (boxes.some(b => x < b[0] + b[2] && x + w > b[0] && Math.abs(y - b[1]) < 12)) continue;
    boxes.push([x, y, w]); el("text", {x, y, class: "lab2"}, g).textContent = r.t; }
  const out = pts.filter(r => { const a = tx(r[kx]), b = r[ky]; return a < x0 || a > x1 || b < y0 || b > y1; }).length;
  $("#sc-note").textContent = (out ? `${out} outlier${out > 1 ? "s" : ""} outside the view (still in the table). ` : "") + "Hover for details, click a dot to open the company.";
  const hit = el("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent"}, svg);
  const near = ev => { const b = svg.getBoundingClientRect(), sx = W / b.width, ex = (ev.clientX - b.left) * sx, ey = (ev.clientY - b.top) * sx;
    let best = null, bd = 16 * 16; for (const r of pts) { const dx = X(r[kx]) - ex, dy = Y(r[ky]) - ey, d = dx * dx + dy * dy; if (d < Math.max(bd, 0) && d < (R(r) + 10) ** 2) { bd = d; best = r; } } return best; };
  hit.onmousemove = ev => { const r = near(ev); hit.style.cursor = r ? "pointer" : "default";
    if (!r) return hideTip(); showTip(`<b>${esc(r.t)}</b> ${esc(r.n)}<br><span class="m">${esc(r.sec)}</span><br>${$("#sx").selectedOptions[0].text}: <b>${AXF[kx](r[kx])}</b><br>${$("#sy").selectedOptions[0].text}: <b>${AXF[ky](r[ky])}</b><br>Implied return <b>${F.pct(r.ir)}</b> · Mkt cap ${F.usd(r.mc)}<br><span class="m">${CATS[cat(r)]}${r.failed.length && !r.pass ? ": fails " + r.failed.map(gname).join(", ") : ""}</span>`, ev); };
  hit.onmouseleave = hideTip;
  hit.onclick = ev => { const r = near(ev); if (r) openDetail(r.t); };
}
const gname = g => D.gates[g] || g.replace(/_/g, " ");

// ---------- histogram ----------
function hist(rows) {
  const box = $("#hist"); box.innerHTML = ""; const W = box.clientWidth || 360, H = 170, m = {l: 30, r: 8, t: 14, b: 26};
  const v = rows.map(r => r.ir).filter(isNum); const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, height: H}, box);
  if (!v.length) return; const step = .02, lo = Math.min(0, Math.floor(Math.min(...v) / step) * step), hiCap = .4;
  const nb = Math.round((hiCap - lo) / step) + 1, bins = Array(nb).fill(0);
  for (const x of v) bins[Math.min(nb - 1, Math.max(0, Math.floor((x - lo) / step)))]++;
  const ymax = Math.max(...bins), bw = (W - m.l - m.r) / nb, Y = c => H - m.b - c / ymax * (H - m.t - m.b);
  for (const t of ticks(0, ymax, 3)) { el("line", {x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t), class: t ? "gl" : "ax"}, svg);
    el("text", {x: m.l - 5, y: Y(t) + 4, "text-anchor": "end"}, svg).textContent = t; }
  const col = css("--s1");
  bins.forEach((c, i) => { const x = m.l + i * bw + 1, w = Math.max(1, bw - 2), y = Y(c), h = H - m.b - y;
    if (c) { const r = Math.min(4, w / 2, h); el("path", {d: `M${x},${H - m.b}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${H - m.b}Z`, fill: col}, svg); }
    const hr = el("rect", {x: m.l + i * bw, y: m.t, width: bw, height: H - m.t - m.b, fill: "transparent"}, svg);
    const a = lo + i * step; hr.onmousemove = ev => showTip(`${i === nb - 1 ? (a * 100).toFixed(0) + "% and up" : (a * 100).toFixed(0) + "–" + ((a + step) * 100).toFixed(0) + "%"}: <b>${c}</b> compan${c === 1 ? "y" : "ies"}`, ev);
    hr.onmouseleave = hideTip; });
  for (const t of [0, .1, .2, .3, .4]) { if (t < lo) continue; const x = m.l + (t - lo) / step * bw;
    el("text", {x, y: H - m.b + 15, "text-anchor": "middle"}, svg).textContent = (t * 100) + "%" + (t === .4 ? "+" : ""); }
  const md = median(v), mxp = m.l + (Math.min(md, hiCap) - lo) / step * bw;
  el("line", {x1: mxp, x2: mxp, y1: m.t - 4, y2: H - m.b, stroke: css("--ink"), "stroke-dasharray": "3 3"}, svg);
  el("text", {x: mxp + 4, y: m.t + 4, class: "lab"}, svg).textContent = `median ${F.pct(md)}`;
}

// ---------- sector bars ----------
function sectors() {
  const box = $("#sect"); box.innerHTML = ""; const rows = ROWS.filter(base);
  const c = {}; for (const r of rows) c[r.sec] = (c[r.sec] || 0) + 1;
  const e = Object.entries(c).sort((a, b) => b[1] - a[1]); if (!e.length) return;
  const W = box.clientWidth || 360, bh = 20, H = e.length * bh + 4, lw = Math.min(150, W * .45), mx = e[0][1];
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, height: H}, box), on = css("--s1"), off = css("--neutral");
  e.forEach(([s, n], i) => { const y = i * bh + 2, w = Math.max(2, (W - lw - 34) * n / mx), act = !S.sec.length || S.sec.includes(s);
    el("text", {x: lw - 6, y: y + 13, "text-anchor": "end", class: "lab"}, svg).textContent = s.length > 24 ? s.slice(0, 22) + "…" : s;
    el("path", {d: `M${lw},${y + 2}H${lw + w - 4}Q${lw + w},${y + 2} ${lw + w},${y + 6}V${y + bh - 6}Q${lw + w},${y + bh - 2} ${lw + w - 4},${y + bh - 2}H${lw}Z`, fill: act ? on : off}, svg);
    el("text", {x: lw + w + 5, y: y + 13}, svg).textContent = n;
    const hr = el("rect", {x: 0, y, width: W, height: bh, fill: "transparent", style: "cursor:pointer"}, svg);
    hr.onclick = () => toggleSec(s); hr.onmousemove = ev => showTip(`<b>${esc(s)}</b>: ${n} compan${n === 1 ? "y" : "ies"}<br><span class="m">Click to ${S.sec.includes(s) ? "remove" : "add"} filter</span>`, ev); hr.onmouseleave = hideTip; });
}

// ---------- table ----------
const COLS = [
  {k: "rank", l: "#", f: (v, r) => r.pass ? v : "", t: "Rank by implied return among companies passing every gate"},
  {k: "t", l: "Ticker", c: "l tk", f: (v, r) => `<a href="https://finance.yahoo.com/quote/${encodeURIComponent(v)}" target="_blank" rel="noopener" onclick="event.stopPropagation()">${esc(v)}</a>`, t: "Opens Yahoo Finance"},
  {k: "n", l: "Company", c: "l nm", f: (v, r) => `<span title="${esc(r.ind)}">${esc(v)}</span>`},
  {k: "sec", l: "Sector", c: "l", f: v => `<span class="mut">${esc(v)}</span>`},
  {k: "px", l: "Price", f: F.px}, {k: "mc", l: "Mkt cap", f: F.usd},
  {k: "rev", l: "Revenue", f: F.usd, t: "Last 12 months"}, {k: "ni", l: "Net income", f: F.usd, t: "Last 12 months"},
  {k: "pg3", l: "Profit/sh 3Y", f: v => cls(v, F.spct(v)), t: "Operating profit per share (net income for financials), growth per year over 3 years"},
  {k: "nig", l: "NI growth 1Y", f: v => cls(v, v > 9.99 ? ">+999%" : F.spct(v)), t: "Net income vs a year earlier"},
  {k: "ir", l: "Implied return", f: (v, r) => irCell(r), t: "Cash yield + growth per share"},
  {k: "cy", l: "Cash yield", f: F.pct}, {k: "g", l: "Growth/sh", f: F.pct},
  {k: "pe", l: "P/E", f: v => F.x(v, 1)}, {k: "nm", l: "Net mgn", f: F.pct}, {k: "roic", l: "ROIC", f: v => F.pct(v, 0), t: "ROE for financials"},
  {k: "nd", l: "Net debt", f: v => isNum(v) && v <= 0 ? `<span class="pos">${F.usd(-v)} cash</span>` : F.usd(v)},
  {k: "nde", l: "ND/EBITDA", f: v => F.x(v, 1)}, {k: "cov", l: "Int. cover", f: v => isNum(v) ? F.x(Math.min(v, 999), 0) : `<span class="mut" title="No interest expense reported">–</span>`, t: "Operating profit ÷ interest expense"},
  {k: "r1y", l: "1Y", f: v => cls(v, F.spct(v))},
  {k: "ibu", l: "Insiders 6M", f: (v, r) => insCell(r), t: "Open-market buys by officers and directors (SEC Form 4)"},
  {k: "flags", l: "Flags", c: "l", f: (v, r) => (r.pass ? "" : `<span class="pill">fails ${esc(r.failed.map(gname).join(", "))}</span>`) + v.map(x => `<span class="pill ${flagCls(x)}">${esc(x)}</span>`).join(""), sort: r => r.flags.length},
];
const cls = (v, s) => isNum(v) ? `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${s}</span>` : s;
function irCell(r) { if (!isNum(r.ir)) return "–"; const sc = .4, a = Math.max(0, Math.min(r.cy ?? 0, sc)), b = Math.max(0, Math.min(r.g ?? 0, sc - a));
  return `<span class="irbar"><span class="b"><i style="width:${a / sc * 100}%;background:var(--s1)"></i><i style="width:${b / sc * 100}%;background:var(--s1b)"></i></span><b>${F.pct(r.ir)}</b></span>`; }
function insCell(r) { if (r.ib >= 1) return `<span class="pos">${r.ib} bought ${F.usd(r.ibu)}</span>`;
  if (isNum(r.inp) && r.inp < -.005) return `<span class="mut">sold ${F.usd(r.isu)}</span>`; return `<span class="mut">–</span>`; }
let SEL = null;
function table(rows) {
  const th = $("#tbl thead"); th.innerHTML = "<tr>" + COLS.map(c => `<th class="${c.c || ""}" data-k="${c.k}" title="${esc(c.t || "")}" ${S.sort === c.k ? `aria-sort="${S.dir > 0 ? "ascending" : "descending"}"` : ""}>${c.l}</th>`).join("") + "</tr>";
  th.querySelectorAll("th").forEach(h => h.onclick = () => { const k = h.dataset.k; S.dir = S.sort === k ? -S.dir : (["t", "n", "sec", "rank"].includes(k) ? 1 : -1); S.sort = k; update(); });
  const col = COLS.find(c => c.k === S.sort), key = col && col.sort ? col.sort : r => r[S.sort];
  const sorted = [...rows].sort((a, b) => { const x = key(a), y = key(b);
    if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    return (typeof x === "string" ? x.localeCompare(y) : x - y) * S.dir; });
  $("#tbl tbody").innerHTML = sorted.map(r => `<tr data-t="${esc(r.t)}" class="${r.t === SEL ? "sel" : ""}">` + COLS.map(c => `<td class="${c.c || ""}">${c.f(r[c.k], r)}</td>`).join("") + "</tr>").join("");
  $$("#tbl tbody tr").forEach(tr => tr.onclick = () => openDetail(tr.dataset.t));
}

// ---------- detail drawer ----------
function openDetail(t) {
  const r = ROWS.find(x => x.t === t); if (!r) return; SEL = t; const dr = $("#drawer");
  const m = [["Price", F.px(r.px)], ["Market cap", F.usd(r.mc)], ["Revenue (12M)", F.usd(r.rev)], ["Net income (12M)", F.usd(r.ni)],
    ["Profit/share, 3Y per yr", F.spct(r.pg3)], ["Net income growth, 1Y", F.spct(r.nig)], ["Revenue growth", F.spct(r.rg)], ["Revenue growth, 3Y/yr", F.spct(r.rg3)], ["Net margin", F.pct(r.nm)],
    ["Implied return", F.pct(r.ir)], ["Cash yield", F.pct(r.cy)], ["Growth/sh (used)", F.pct(r.g)], ["P/E", F.x(r.pe)],
    ["P/S", F.x(r.ps)], [r.fin ? "ROE" : "ROIC", F.pct(r.roic)], ["Free cash flow", F.usd(r.fcf)], ["Net debt", isNum(r.nd) && r.nd <= 0 ? F.usd(-r.nd) + " net cash" : F.usd(r.nd)],
    ["Net debt / EBITDA", F.x(r.nde)], ["Interest coverage", isNum(r.cov) ? F.x(r.cov, 0) : "none reported"], ["Margin change", isNum(r.mg) ? (r.mg * 100 > 0 ? "+" : "") + (r.mg * 100).toFixed(1) + " pts" : "–"],
    ["1-year price", F.spct(r.r1y)], ["Below 52w high", F.pct(r.off, 0)], ["Insiders 6M", r.ib >= 1 ? `${r.ib} bought ${F.usd(r.ibu)}` : r.isu ? `sold ${F.usd(r.isu)}` : "none"]];
  const status = r.pass ? `<span class="pill ${r.top ? "top" : "good"}">${r.top ? "Top 25" : "Passes all gates"} · rank #${r.rank}${r.fin ? " (financials)" : ""}</span>`
    : `<span class="pill warn">Fails: ${esc(r.failed.map(gname).join(", "))}</span>`;
  const cik = r.cik ? `<a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${r.cik}&type=&dateb=&owner=include&count=40" target="_blank" rel="noopener">SEC filings</a>
    <a href="https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${r.cik}&type=4&dateb=&owner=only&count=40" target="_blank" rel="noopener">Insider Form 4s</a>` : "";
  dr.innerHTML = `<button class="btn x" aria-label="Close">✕</button><h2>${esc(r.t)}</h2><div>${esc(r.n)}</div>
    <div class="mut" style="font-size:13px">${esc(r.sec)} · ${esc(r.ind)}</div><div style="margin-top:8px">${status}</div>
    <div class="links"><a href="https://finance.yahoo.com/quote/${encodeURIComponent(r.t)}" target="_blank" rel="noopener">Yahoo Finance</a>${cik}
    <a href="https://finviz.com/quote.ashx?t=${encodeURIComponent(r.t)}" target="_blank" rel="noopener">Finviz</a>
    <a href="https://news.google.com/search?q=${encodeURIComponent('"' + r.n + '"')}" target="_blank" rel="noopener">News</a></div>
    ${r.why ? `<h4>Why it's here</h4><div>${esc(r.why)}</div>` : ""}
    <div class="mgrid">${m.map(([k, v]) => `<div><span>${k}</span><span>${v}</span></div>`).join("")}</div>
    ${r.flags.length ? `<h4>Flags</h4>${r.flags.map(x => `<span class="pill ${flagCls(x)}">${esc(x)}</span>`).join("")}` : ""}
    ${r.news ? `<h4>Recent headlines</h4><div class="news">${r.news.map(h => `<a href="${esc(h.link)}" target="_blank" rel="noopener">${esc(h.title)}<small>${esc(h.src)} · ${esc(h.date)}</small></a>`).join("")}</div>` : ""}`;
  dr.classList.add("open"); dr.querySelector(".x").onclick = closeDetail; update(false);
}
function closeDetail() { $("#drawer").classList.remove("open"); SEL = null; update(false); }
addEventListener("keydown", e => { if (e.key === "Escape") closeDetail(); });

function update(persist = true) {
  buildSegs(); const rows = visible();
  $("#count").textContent = `${rows.length} shown`;
  sectorChips(); kpis(rows); scatter(rows); hist(rows); sectors(); table(rows);
  if (persist) save();
}
const buildSegs = () => { seg("#mode", MODES, "mode", update); seg("#group", GROUPS, "group", update); };

// ---------- backtest ----------
const DEFAULT = "Implied return top 25", BENCH = "Equal-weight universe";
const BT = {h: 3, cmp: "Healthy: net income growing", log: true};
function stats(r, b) {
  const ok = r.map((v, i) => [v, b ? b[i] : 0]).filter(([v, w]) => isNum(v) && (!b || isNum(w)));
  const n = ok.length; if (!n) return {};
  const rr = ok.map(x => x[0]); const eq = rr.reduce((a, v) => a * (1 + v), 1);
  const mean = rr.reduce((a, v) => a + v, 0) / n, sd = Math.sqrt(rr.reduce((a, v) => a + (v - mean) ** 2, 0) / (n - 1));
  let pk = 1, e = 1, mdd = 0; for (const v of rr) { e *= 1 + v; pk = Math.max(pk, e); mdd = Math.min(mdd, e / pk - 1); }
  const o = {cagr: eq ** (12 / n) - 1, vol: sd * Math.sqrt(12), sharpe: mean / sd * Math.sqrt(12), mdd, n};
  if (b) { const bb = ok.map(x => x[1]), ex = ok.map(x => x[0] - x[1]), me = ex.reduce((a, v) => a + v, 0) / n;
    const se = Math.sqrt(ex.reduce((a, v) => a + (v - me) ** 2, 0) / (n - 1));
    o.excess = o.cagr - (bb.reduce((a, v) => a * (1 + v), 1) ** (12 / n) - 1); o.t = me / se * Math.sqrt(n); o.hit = ex.filter(v => v > 0).length / n; }
  return o;
}
const group = k => k === DEFAULT ? "Default" : /^(Healthy|Size):/.test(k) ? "Healthy balance sheet and size (tested 2026-10-27)" :
  k.startsWith("Without ") ? "Default with one gate removed" : /^Q\d$/.test(k) ? "Implied return quintiles (all investable)" :
  [BENCH, "SPY", "Baseline: top 25 FCF yield", "All gated names"].includes(k) ? "Benchmarks" : "Other variants tested";
const GORDER = ["Default", "Healthy balance sheet and size (tested 2026-10-27)", "Benchmarks", "Other variants tested", "Default with one gate removed", "Implied return quintiles (all investable)"];
const label = k => k === DEFAULT ? "Default: implied return top 25" : k.startsWith("Without ") ? "Without " + gname(k.slice(8)) : /^Q\d$/.test(k) ? `Quintile ${k[1]}${k === "Q5" ? " (cheapest)" : k === "Q1" ? " (dearest)" : ""}` : k;
function btRender() {
  if (!D.bt) { $("#tab-bt").innerHTML = `<div class="card">No backtest results found next to this file. Run <code>python3 -m screener backtest</code>.</div>`; return; }
  const H = D.bt[BT.h] || D.bt[Object.keys(D.bt)[0]], ser = H.series, names = Object.keys(ser);
  if (!ser[BT.cmp]) BT.cmp = "";
  seg("#hold", Object.keys(D.bt).map(h => [+h, h + (h == 1 ? " month" : " months")]), null, null);
  $("#hold").innerHTML = Object.keys(D.bt).map(h => `<button aria-pressed="${+h === BT.h}" data-h="${h}">${h} month${h == 1 ? "" : "s"}</button>`).join("");
  $$("#hold button").forEach(b => b.onclick = () => { BT.h = +b.dataset.h; btRender(); });
  $("#scale").innerHTML = `<button aria-pressed="${BT.log}" data-v="1">Log scale</button><button aria-pressed="${!BT.log}" data-v="0">Linear</button>`;
  $$("#scale button").forEach(b => b.onclick = () => { BT.log = b.dataset.v === "1"; btRender(); });
  $("#cmp").innerHTML = `<option value="">(none)</option>` + GORDER.map(g => `<optgroup label="${esc(g)}">` + names.filter(k => group(k) === g && ![DEFAULT, BENCH, "SPY"].includes(k)).map(k => `<option value="${esc(k)}" ${k === BT.cmp ? "selected" : ""}>${esc(label(k))}</option>`).join("") + "</optgroup>").join("");
  $("#cmp").onchange = e => { BT.cmp = e.target.value; btRender(); };
  const b = ser[BENCH], st = stats(ser[DEFAULT], b);
  $("#btk").innerHTML = [[F.pct(st.cagr), "default, per year"], [F.spct(st.excess, 1), "vs equal-weight universe, per year"], [F.n(st.t, 1), "t-stat of the excess"],
    [F.n(st.sharpe, 2), "Sharpe ratio"], [F.pct(st.mdd, 0), "worst drawdown"], [H.dates[0] + " → " + H.dates[H.dates.length - 1], `${st.n} months`]]
    .map(([v, l]) => `<div class="card kpi"><div class="v" style="${l.includes("months") ? "font-size:17px" : ""}">${v}</div><div class="k">${l}</div></div>`).join("");
  const lines = [[DEFAULT, "--s1"], [BENCH, "--s2"], ["SPY", "--s3"]]; if (BT.cmp) lines.push([BT.cmp, "--s4"]);
  $("#bt-leg").innerHTML = lines.map(([k, c]) => `<span><i class="ln" style="background:${css(c)}"></i>${esc(label(k))}</span>`).join("");
  $("#bt-note").textContent = `Each month buys the strategy's picks and holds them ${BT.h} month${BT.h > 1 ? "s" : ""} (overlapping), after costs.`;
  lineChart($("#eq"), H.dates, lines, "eq"); lineChart($("#dd"), H.dates, lines.slice(0, BT.cmp ? 4 : 2).filter(l => l[0] !== "SPY" || true), "dd");
  // variants table
  const rows = names.map(k => ({k, g: group(k), ...stats(ser[k], k === BENCH ? null : b)}));
  const cols = [["Strategy", r => esc(label(r.k)), "l"], ["CAGR", r => F.pct(r.cagr)], ["vs universe /yr", r => r.k === BENCH ? "–" : cls(r.excess, F.spct(r.excess, 1))],
    ["t-stat", r => F.n(r.t, 1)], ["Beats universe, % of months", r => F.pct(r.hit, 0)], ["Sharpe", r => F.n(r.sharpe, 2)], ["Volatility", r => F.pct(r.vol, 0)], ["Worst drawdown", r => F.pct(r.mdd, 0)]];
  $("#vt thead").innerHTML = "<tr>" + cols.map(c => `<th class="${c[2] || ""}" style="cursor:default">${c[0]}</th>`).join("") + "</tr>";
  let html = ""; for (const g of GORDER) { const rs = rows.filter(r => r.g === g); if (!rs.length) continue;
    if (g !== "Implied return quintiles (all investable)") rs.sort((a, b) => (b.cagr ?? -9) - (a.cagr ?? -9));
    html += `<tr class="grp"><td colspan="${cols.length}">${esc(g)}</td></tr>` + rs.map(r => `<tr data-k="${esc(r.k)}" class="${r.k === BT.cmp ? "cur" : ""}">` + cols.map(c => `<td class="${c[2] || ""}">${c[1](r)}</td>`).join("") + "</tr>").join(""); }
  $("#vt tbody").innerHTML = html;
  $$("#vt tbody tr[data-k]").forEach(tr => tr.onclick = () => { const k = tr.dataset.k; if ([DEFAULT, BENCH, "SPY"].includes(k)) return; BT.cmp = k; btRender(); scrollTo({top: 0, behavior: "smooth"}); });
}
function lineChart(box, dates, lines, kind) {
  box.innerHTML = ""; const W = box.clientWidth || 900, H = kind === "eq" ? 340 : 180, m = {l: 50, r: 150, t: 10, b: 26};
  if (W < 560) m.r = 12;
  const ser = (D.bt[BT.h] || {}).series; const vals = lines.map(([k]) => { let e = 1, pk = 1;
    return ser[k].map(v => { if (isNum(v)) e *= 1 + v; pk = Math.max(pk, e); return kind === "eq" ? e : e / pk - 1; }); });
  const all = vals.flat(); let y0 = Math.min(...all), y1 = Math.max(...all); const log = kind === "eq" && BT.log;
  const tf = v => log ? Math.log(v) : v; if (kind === "dd") y1 = 0; if (kind === "eq" && !log) y0 = Math.min(y0, 1);
  const n = dates.length, X = i => m.l + i / (n - 1) * (W - m.l - m.r), Y = v => H - m.b - (tf(v) - tf(y0)) / (tf(y1) - tf(y0)) * (H - m.t - m.b);
  const svg = el("svg", {viewBox: `0 0 ${W} ${H}`, height: H}, box);
  const yt = log ? [.5, 1, 1.5, 2, 3, 4, 5, 6, 8, 10].filter(v => v >= y0 * .98 && v <= y1 * 1.02) : ticks(y0, y1, 5);
  for (const t of yt) { el("line", {x1: m.l, x2: W - m.r, y1: Y(t), y2: Y(t), class: (kind === "eq" ? t === 1 : t === 0) ? "ax" : "gl"}, svg);
    el("text", {x: m.l - 6, y: Y(t) + 4, "text-anchor": "end"}, svg).textContent = kind === "eq" ? "$" + t : F.pct(t, 0); }
  dates.forEach((d, i) => { if (d.endsWith("-01") && (+d.slice(0, 4)) % (W < 560 ? 3 : 1) === 0) el("text", {x: X(i), y: H - 8, "text-anchor": "middle"}, svg).textContent = d.slice(0, 4); });
  const ends = [];
  lines.forEach(([k, c], j) => { const p = vals[j].map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
    el("path", {d: p, fill: "none", stroke: css(c), "stroke-width": k === DEFAULT ? 2.5 : 2, "stroke-linejoin": "round"}, svg);
    if (kind === "eq" && m.r > 100) ends.push({y: Y(vals[j][n - 1]), t: `${label(k).replace("Default: ", "").slice(0, 22)} $${vals[j][n - 1].toFixed(2)}`, c}); });
  ends.sort((a, b) => a.y - b.y); for (let i = 1; i < ends.length; i++) ends[i].y = Math.max(ends[i].y, ends[i - 1].y + 14);
  for (const e of ends) { el("circle", {cx: W - m.r + 6, cy: e.y - 4, r: 3, fill: e.c}, svg); el("text", {x: W - m.r + 13, y: e.y, class: "lab"}, svg).textContent = e.t; }
  const cross = el("line", {y1: m.t, y2: H - m.b, stroke: css("--axis"), visibility: "hidden"}, svg);
  const dots = lines.map(([, c]) => el("circle", {r: 4, fill: css(c), stroke: css("--surface"), "stroke-width": 2, visibility: "hidden"}, svg));
  const hit = el("rect", {x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent"}, svg);
  hit.onmousemove = ev => { const b = svg.getBoundingClientRect(), x = (ev.clientX - b.left) * W / b.width;
    const i = Math.max(0, Math.min(n - 1, Math.round((x - m.l) / (W - m.l - m.r) * (n - 1))));
    cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i)); cross.setAttribute("visibility", "visible");
    dots.forEach((d, j) => { d.setAttribute("cx", X(i)); d.setAttribute("cy", Y(vals[j][i])); d.setAttribute("visibility", "visible"); });
    showTip(`<b>${dates[i]}</b><br>` + lines.map(([k, c], j) => `<span style="color:${css(c)}">●</span> ${esc(label(k))}: <b>${kind === "eq" ? "$" + vals[j][i].toFixed(2) : F.pct(vals[j][i], 0)}</b>`).join("<br>"), ev); };
  hit.onmouseleave = () => { hideTip(); cross.setAttribute("visibility", "hidden"); dots.forEach(d => d.setAttribute("visibility", "hidden")); };
}

// ---------- method ----------
function method() {
  const s = D.settings;
  $("#method").innerHTML = `<h2 style="margin-top:0">Guiding principles</h2><ol>${D.principles.map(p => `<li><b>${esc(p.h)}</b>${esc(p.b)}</li>`).join("")}</ol>
  <h2>What a company must pass</h2><dl>
  <dt>Size and trading</dt><dd>US headquarters, market cap $${s.min_mcap / 1e9}B to $${s.max_mcap / 1e9}B, price over $5, over $2M traded a day, latest filing under 200 days old.</dd>
  <dt>Profitable</dt><dd>Net income and free cash flow (after stock-based pay) positive over the last 12 months; net margin at least ${s.min_nm * 100}%; ROIC at least ${s.min_roic * 100}%.</dd>
  <dt>Growing</dt><dd>Revenue per share up over the last 12 months and over 3 years; operating profit per share above 3 years ago, so one bad year doesn't fail it (net income for financials); operating margin not down more than 2 points.</dd>
  <dt>Debt managed</dt><dd>Net debt under ${s.max_nde}× EBITDA; debt not up more than 25% in a year unless near net cash.</dd>
  <dt>Clean record</dt><dd>No restatement or bankruptcy 8-K in the last year; no mortgage REITs. Financials skip the cash-flow, margin and debt tests and are ranked on their own list.</dd>
  </dl><h2>How the rank works</h2><dl>
  <dt>Implied return</dt><dd>Cash yield + growth per share. Roughly what an owner earns per year if the valuation multiple never changes.</dd>
  <dt>Cash yield</dt><dd>Average of earnings yield and free-cash-flow yield (free cash flow counts for at most ${s.fcf_cap}× earnings).</dd>
  <dt>Growth per share</dt><dd>The lower of 12-month and 3-year revenue-per-share growth, clipped to 0–${s.max_growth * 100}%.</dd>
  <dt>Context only</dt><dd>Insider buying, headlines, 8-K events, price momentum, interest coverage and net income growth are shown but don't move the rank unless a backtest earns them a place.</dd></dl>`;
}

// ---------- tabs & theme ----------
$$(".tabs [data-tab]").forEach(b => b.onclick = () => { $$(".tabs [data-tab]").forEach(x => x.setAttribute("aria-selected", x === b));
  for (const t of ["screen", "bt", "method"]) $("#tab-" + t).classList.toggle("hidden", t !== b.dataset.tab);
  if (b.dataset.tab === "bt") btRender(); else if (b.dataset.tab === "screen") update(false); });
$("#theme").onclick = () => { const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = cur === "dark" ? "light" : "dark"; rerender(); };
const rerender = () => { if (!$("#tab-bt").classList.contains("hidden")) btRender(); else update(false); };
let rt; addEventListener("resize", () => { clearTimeout(rt); rt = setTimeout(rerender, 150); });
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", rerender);
buildControls(); method(); update(false);
</script></body></html>
"""
