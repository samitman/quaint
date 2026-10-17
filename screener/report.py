"""Ranked CSV + self-contained HTML report from a scored cross-section."""
from __future__ import annotations

import html
from pathlib import Path

import pandas as pd

from . import score

COLS = [  # (column, header, format)
    ("sec_name", "Company", "s"), ("sector", "Sector", "s"), ("exchange", "Exch", "s"),
    ("price", "Price", "$"), ("mcap", "Mkt cap", "B"), ("pe", "P/E", "x"), ("pb", "P/B", "x"),
    ("ev_ebit", "EV/EBIT", "x"), ("fcf_yield", "FCF yld", "%"), ("roic", "ROIC", "%"),
    ("roe", "ROE", "%"), ("op_margin", "Op mgn", "%"), ("rev_growth", "Rev g", "%"),
    ("rev_cagr_3y", "Rev 3y", "%"), ("margin_change", "Mgn Δ", "pp"), ("nd_ebitda", "ND/EBITDA", "x"),
    ("fscore", "F", "f"), ("value", "Value", "p"), ("quality", "Qual", "p"),
    ("trajectory", "Traj", "p"), ("composite", "Score", "p"), ("flags", "Data flags", "s"),
]
CSV_COLS = ["rank", "ticker"] + [c for c, _, _ in COLS] + [
    "passes", "failed", "net_income", "fcf", "ebit", "equity", "total_debt", "cash", "ev",
    "earnings_yield", "ebit_ev", "book_yield", "gross_margin", "fcf_conversion", "ni_growth",
    "fscore_n", "period_end", "data_age_days", "shares", "shares_source", "debt_stale",
    "adv_usd", "industry", "index"]


def _fmt(v, kind):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "–"
    if kind == "s":
        return html.escape(str(v))
    if kind == "$":
        return f"{v:,.2f}"
    if kind == "B":
        return f"{v / 1e9:,.1f}B"
    if kind == "x":
        return f"{v:.1f}" if abs(v) < 1000 else "≫"
    if kind == "%":
        return f"{100 * v:.1f}%"
    if kind == "pp":
        return f"{100 * v:+.1f}"
    if kind == "f":
        return f"{v:.0f}"
    if kind == "p":
        return f"{100 * v:.0f}"
    return str(v)


def _table(df: pd.DataFrame, tid: str, extra: str | None = None) -> str:
    heads = "".join(f"<th>{h}</th>" for h in ["#", "Ticker"] + [h for _, h, _ in COLS]
                    + ([extra] if extra else []))
    rows = []
    for i, (t, r) in enumerate(df.iterrows(), 1):
        cells = [f"<td>{i}</td>", f"<td class=t>{html.escape(t)}</td>"]
        for c, _, k in COLS:
            v = r.get(c)
            raw = "" if v is None or (isinstance(v, float) and pd.isna(v)) or k == "s" else f' data-v="{v}"'
            cells.append(f"<td{raw}>{_fmt(v, k)}</td>")
        if extra:
            cells.append(f"<td class=s>{html.escape(str(r.get('failed', '')))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f'<table id={tid}><thead><tr>{heads}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e3e3e3;--head:#f5f5f3;--acc:#0b6e4f}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--mut:#999;--line:#2c2c2c;--head:#1e1e1e;--acc:#4fc79a}}
body{background:var(--bg);color:var(--fg);font:13px/1.4 -apple-system,system-ui,Segoe UI,sans-serif;margin:24px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px}p{color:var(--mut);max-width:900px}
.wrap{overflow-x:auto}table{border-collapse:collapse;white-space:nowrap}
th,td{padding:4px 8px;border-bottom:1px solid var(--line);text-align:right}
th{background:var(--head);position:sticky;top:0;cursor:pointer;font-weight:600}
td:nth-child(3),td:nth-child(4),td:nth-child(5),th:nth-child(3),th:nth-child(4){text-align:left}
td.t{font-weight:700;color:var(--acc);text-align:left}td.s{text-align:left;color:var(--mut)}
td:nth-last-child(2){font-weight:700}
"""
JS = """
document.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>{const tb=th.closest('table').tBodies[0];
const rows=[...tb.rows];const d=th.dataset.d=th.dataset.d==='a'?'d':'a';
const key=r=>{const c=r.cells[i];return c.dataset.v!==undefined?parseFloat(c.dataset.v):c.textContent};
rows.sort((a,b)=>{const x=key(a),y=key(b);const r=typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y));return d==='a'?r:-r});
rows.forEach(r=>tb.appendChild(r))});
"""


def write(scored: pd.DataFrame, out_dir: Path, top: int = 50, note: str = "") -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    d = pd.Timestamp(scored["date"].iloc[0]).date()
    s = scored.sort_values("composite", ascending=False).copy()
    s["flags"] = [", ".join(f for f, on in (("debt est.", r.get("debt_estimated")),
                                            ("debt from 10-K", r.get("debt_stale")),
                                            (f"shares: {r.get('shares_source')}",
                                             r.get("shares_source") not in ("cover", "diluted")),
                                            ("Q1 data", (r.get("data_age_days") or 0) > 150)) if on)
                  for _, r in s.iterrows()]
    passed = s[s["passes"]]
    s["rank"] = None
    s.loc[passed.index, "rank"] = range(1, len(passed) + 1)
    csv = out_dir / f"screen_{d}.csv"
    s[[c for c in CSV_COLS if c in s.columns]].to_csv(csv, index_label="ticker_id")

    inv = s[s["investable"]]
    near = inv[~inv["passes"] & (inv["failed"].str.count(",") == 0)].head(25)
    dear = inv.sort_values("value").head(20)
    w = score.DEFAULT_WEIGHTS
    page = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Bargain screen {d}</title>
<style>{CSS}</style></head><body>
<h1>Bargain screen · {d}</h1>
<p>{len(inv)} investable names (S&amp;P 500/400 + Nasdaq-100, mcap &gt; $2B, price &gt; $5, ADV &gt; $10M);
<b>{len(passed)}</b> pass every hard filter (profitable, FCF &gt; 0, net debt/EBITDA &lt; {score.MAX_ND_EBITDA},
Piotroski ≥ {score.MIN_FSCORE:.0f}; financials exempt from FCF/leverage). Scores are percentiles vs. sector peers
(100 = cheapest/best in sector). Composite = {w['value']:.0%} value + {w['quality']:.0%} quality +
{w['trajectory']:.0%} trajectory. Click a header to sort. {html.escape(note)}</p>
<h2>Ranked bargains (top {min(top, len(passed))} of {len(passed)})</h2><div class=wrap>{_table(passed.head(top), "top")}</div>
<h2>Near misses (fail exactly one filter)</h2><div class=wrap>{_table(near, "near", "Failed")}</div>
<h2>Definitely not bargains (lowest sector-relative value)</h2><div class=wrap>{_table(dear, "dear")}</div>
<p>Fundamentals: SEC EDGAR XBRL, trailing twelve months, as filed. Prices: Yahoo. Full data: {csv.name}.</p>
<script>{JS}</script></body></html>"""
    htm = out_dir / f"screen_{d}.html"
    htm.write_text(page)
    return htm, csv
