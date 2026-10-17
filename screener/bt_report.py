"""Backtest HTML report: equity curves, stats, IC, quintile spread."""
from __future__ import annotations

import html
import json
from pathlib import Path

import numpy as np
import pandas as pd

from . import backtest

SERIES = [("Screen top 25", "--s1"), ("Equal-weight universe", "--s2"), ("SPY", "--s3")]

CSS = """
:root{color-scheme:light;--bg:#fcfcfb;--fg:#0b0b0b;--mut:#52514e;--line:#e4e3df;--head:#f3f2ef;
--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--bg:#1a1a19;
--fg:#f0efec;--mut:#c3c2b7;--line:#33322f;--head:#232321;--s1:#3987e5;--s2:#d95926;--s3:#199e70}}
:root[data-theme="dark"]{color-scheme:dark;--bg:#1a1a19;--fg:#f0efec;--mut:#c3c2b7;--line:#33322f;
--head:#232321;--s1:#3987e5;--s2:#d95926;--s3:#199e70}
body{background:var(--bg);color:var(--fg);font:13px/1.45 -apple-system,system-ui,Segoe UI,sans-serif;margin:24px;max-width:1100px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px}p,li{color:var(--mut)}
table{border-collapse:collapse}th,td{padding:4px 10px;border-bottom:1px solid var(--line);text-align:right}
th{background:var(--head)}td:first-child,th:first-child{text-align:left}
.legend span{display:inline-flex;align-items:center;gap:6px;margin-right:16px;color:var(--fg)}
.legend i{width:14px;height:2px;display:inline-block}
#chart{position:relative}#tip{position:absolute;pointer-events:none;background:var(--bg);border:1px solid var(--line);
padding:6px 8px;border-radius:6px;display:none;white-space:nowrap;font-variant-numeric:tabular-nums}
svg text{fill:var(--mut);font-size:11px}
"""


def _fmt_table(df: pd.DataFrame, pct_cols=(), num_cols=()) -> str:
    h = "<tr><th></th>" + "".join(f"<th>{html.escape(str(c))}</th>" for c in df.columns) + "</tr>"
    rows = []
    for k, r in df.iterrows():
        cells = []
        for c, v in r.items():
            if v is None or (isinstance(v, float) and np.isnan(v)):
                cells.append("<td>–</td>")
            elif c in pct_cols:
                cells.append(f"<td>{100 * v:.1f}%</td>")
            else:
                cells.append(f"<td>{v:.2f}</td>")
        rows.append(f"<tr><td>{html.escape(str(k))}</td>{''.join(cells)}</tr>")
    return f"<table>{h}{''.join(rows)}</table>"


def _chart(rets: pd.DataFrame) -> str:
    eq = (1 + rets[[s for s, _ in SERIES]]).cumprod()
    first = eq.index[0] - pd.offsets.MonthEnd(1)
    eq.loc[first] = 1.0
    eq = eq.sort_index()
    W, H, L, R, T, B = 1000, 360, 48, 16, 12, 28
    lo, hi = np.log(eq.min().min() * 0.95), np.log(eq.max().max() * 1.05)
    xs = np.linspace(L, W - R, len(eq))
    y = lambda v: T + (hi - np.log(v)) / (hi - lo) * (H - T - B)
    grid = []
    for g in [0.5, 0.75, 1, 1.5, 2, 3, 4, 5, 6, 8, 10]:
        if lo <= np.log(g) <= hi:
            grid.append(f'<line x1={L} x2={W - R} y1={y(g):.1f} y2={y(g):.1f} stroke="var(--line)"/>'
                        f'<text x={L - 6} y={y(g) + 4:.1f} text-anchor=end>{g:g}×</text>')
    for i, d in enumerate(eq.index):
        if d.month == 1 and i:
            grid.append(f'<text x={xs[i]:.1f} y={H - 8} text-anchor=middle>{d.year}</text>')
    lines = []
    for s, var in SERIES:
        pts = " ".join(f"{xs[i]:.1f},{y(v):.1f}" for i, v in enumerate(eq[s]))
        lines.append(f'<polyline points="{pts}" fill=none stroke="var({var})" stroke-width=2 '
                     f'stroke-linejoin=round />')
    data = {"x": [round(float(v), 1) for v in xs], "d": [f"{d:%Y-%m}" for d in eq.index],
            "s": {s: [round(float(v), 3) for v in eq[s]] for s, _ in SERIES},
            "y": {s: [round(float(y(v)), 1) for v in eq[s]] for s, _ in SERIES},
            "c": {s: v for s, v in SERIES}}
    legend = "".join(f'<span><i style="background:var({v})"></i>{html.escape(s)}</span>' for s, v in SERIES)
    return f"""<div class=legend>{legend}</div><div id=chart>
<svg viewBox="0 0 {W} {H}" width=100% role=img aria-label="Growth of $1, log scale">
{''.join(grid)}{''.join(lines)}<line id=xh y1={T} y2={H - B} stroke="var(--mut)" stroke-dasharray="3 3" visibility=hidden />
<g id=dots></g><rect x={L} y={T} width={W - L - R} height={H - T - B} fill=transparent id=hit /></svg>
<div id=tip></div></div>
<script>
const D={json.dumps(data)};const svg=document.querySelector('#chart svg'),tip=document.getElementById('tip'),
xh=document.getElementById('xh'),dots=document.getElementById('dots');
document.getElementById('hit').addEventListener('mousemove',e=>{{const p=svg.createSVGPoint();p.x=e.clientX;p.y=e.clientY;
const q=p.matrixTransform(svg.getScreenCTM().inverse());let i=0,b=1e9;D.x.forEach((x,j)=>{{const d=Math.abs(x-q.x);if(d<b){{b=d;i=j}}}});
xh.setAttribute('x1',D.x[i]);xh.setAttribute('x2',D.x[i]);xh.setAttribute('visibility','visible');
dots.innerHTML=Object.keys(D.s).map(s=>`<circle cx="${{D.x[i]}}" cy="${{D.y[s][i]}}" r=4 fill="var(${{D.c[s]}})" stroke="var(--bg)" stroke-width=2 />`).join('');
tip.innerHTML='<b>'+D.d[i]+'</b><br>'+Object.keys(D.s).map(s=>`<span style="color:var(${{D.c[s]}})">●</span> ${{s}}: ${{D.s[s][i].toFixed(2)}}×`).join('<br>');
const r=svg.getBoundingClientRect();tip.style.display='block';const left=(D.x[i]/{W})*r.width;
tip.style.left=(left>r.width*0.6?left-tip.offsetWidth-12:left+12)+'px';tip.style.top='8px'}});
document.getElementById('hit').addEventListener('mouseleave',()=>{{tip.style.display='none';xh.setAttribute('visibility','hidden');dots.innerHTML=''}});
</script>"""


def write(res: dict, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    rets = res["returns"]
    rets.to_csv(out_dir / "backtest_monthly_returns.csv")
    res["ic"].to_csv(out_dir / "backtest_ic.csv")
    hold = pd.DataFrame([{"date": d, "tickers": " ".join(w.index)} for d, w in res["holdings"].items()])
    hold.to_csv(out_dir / "backtest_holdings.csv", index=False)
    c = res["counts"]
    t = res["table"]
    q = t.loc[[f"Q{i}" for i in range(1, 6)], ["CAGR", "Excess/yr", "Sharpe"]]
    q.index = [f"Q{i}{' (worst)' if i == 1 else ' (best)' if i == 5 else ''}" for i in range(1, 6)]
    page = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Screener backtest</title>
<style>{CSS}</style></head><body>
<h1>Point-in-time backtest · {rets.index[0]:%Y-%m} to {rets.index[-1]:%Y-%m}</h1>
<p>Monthly rebalance, equal weight, {backtest.COST_BPS} bps one-way costs on turnover. Fundamentals use only
filings public on each rebalance date. Average {c['investable'].mean():.0f} investable names, {c['passers'].mean():.0f}
passing the hard filters (min {c['passers'].min()}, max {c['passers'].max()}).</p>
<p><b>Bias warning:</b> the universe is today's index members, so delisted, acquired and bankrupt names are missing.
That inflates absolute returns for every line here; compare strategies with the equal-weight universe, which shares the bias.
Value strategies are flattered most, since the cheap stocks that subsequently died are exactly the ones absent.</p>
<h2>Growth of $1 (log scale)</h2>{_chart(rets)}
<h2>Performance</h2>{_fmt_table(t, pct_cols=("CAGR", "Vol", "MaxDD", "Excess/yr", "Hit rate"))}
<p>Excess/yr and hit rate are vs. the equal-weight universe. Excess t is the t-stat of the monthly excess return.</p>
<h2>Composite quintiles (all investable names)</h2>{_fmt_table(q, pct_cols=("CAGR", "Excess/yr"))}
<h2>Stability: excess return per year vs. equal-weight universe, by half</h2>{_fmt_table(res["halves"], pct_cols=tuple(res["halves"].columns))}
<h2>Information coefficient (rank correlation of score with next-month return)</h2>
{_fmt_table(res["ic_summary"], pct_cols=("% months > 0",))}
<p>Files: backtest_monthly_returns.csv, backtest_ic.csv, backtest_holdings.csv.</p>
</body></html>"""
    p = out_dir / "backtest.html"
    p.write_text(page)
    return p
