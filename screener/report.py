"""Ranked CSV + self-contained HTML report from a scored cross-section."""
from __future__ import annotations

import html
from pathlib import Path

import pandas as pd

from . import score

GROUPS = [  # (group header, [(column, header, format), ...])
    ("", [("sec_name", "Company", "s"), ("sector", "Sector", "s"), ("price", "Price", "$"),
          ("mcap", "Mkt cap", "B")]),
    ("Valuation", [("pe", "P/E", "x"), ("pb", "P/B", "x"), ("fcf_yield", "FCF yld", "%")]),
    ("Profitability", [("net_income", "Net inc", "B"), ("roic", "ROIC", "%"), ("op_margin", "Op mgn", "%")]),
    ("Balance sheet", [("nd_ebitda", "ND/EBITDA", "x"), ("debt_yoy", "Debt y/y", "%")]),
    ("Growth", [("rev_growth", "Rev y/y", "%"), ("margin_change", "Mgn Δ pp", "pp")]),
    ("Price action", [("ret_1m", "1M", "%"), ("ret_ytd", "YTD", "%"), ("ret_1y", "1Y", "%"),
                      ("off_high", "vs 52w hi", "%")]),
    ("Scores (sector percentile)", [("value", "Val", "p"), ("profitability", "Prof", "p"),
                                    ("balance_sheet", "BS", "p"), ("growth", "Grow", "p"),
                                    ("price_action", "Price", "p"), ("bargain", "Bargain", "p")]),
    ("", [("flags", "Data flags", "s")]),
]
COLS = [c for _, cols in GROUPS for c in cols]
CSV_COLS = ["rank", "ticker"] + [c for c, _, _ in COLS] + [
    "blend", "ev_ebit", "roe", "rev_cagr_3y", "fscore", "debt_change", "debt_prev",
    "passes", "failed", "fcf", "ebit", "equity", "total_debt", "cash", "ev",
    "earnings_yield", "ebit_ev", "book_yield", "gross_margin", "fcf_conversion", "ni_growth",
    "fscore_n", "period_end", "data_age_days", "shares", "shares_source", "debt_stale",
    "adv_usd", "industry", "index", "yahoo_fields"]


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
    top = "<th colspan=2></th>" + "".join(
        f"<th colspan={len(cols)} class=g>{g}</th>" for g, cols in GROUPS) + ("<th></th>" if extra else "")
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
    return f'<table id={tid}><thead><tr class=grp>{top}</tr><tr>{heads}</tr></thead><tbody>{"".join(rows)}</tbody></table>'


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
tr.grp th{position:static;font-weight:500;color:var(--mut);border-bottom:none;cursor:default}
th.g{border-left:2px solid var(--line);text-align:center}
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
                                            (f"yahoo: {r.get('yahoo_fields')}", bool(r.get("yahoo_fields"))),
                                            ("lagged data", (r.get("data_age_days") or 0) > 150)) if on)
                  for _, r in s.iterrows()]
    passed = s[s["passes"]]
    s["rank"] = None
    s.loc[passed.index, "rank"] = range(1, len(passed) + 1)
    csv = out_dir / f"screen_{d}.csv"
    s[[c for c in CSV_COLS if c in s.columns]].to_csv(csv, index_label="ticker_id")

    inv = s[s["investable"]]
    near = inv[~inv["passes"] & (inv["failed"].str.count(",") == 0)
               & (inv["failed"] != "business_model")].head(25)
    dear = inv.sort_values("value").head(20)
    page = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Bargain screen {d}</title>
<style>{CSS}</style></head><body>
<h1>Bargain screen · {d}</h1>
<p><b>Bargain</b> = how much cheaper a stock is than its profitability, balance sheet and growth
would justify, vs. sector peers (100 = biggest bargain). Ranked list = names passing every hard filter
({len(passed)} of {len(inv)} investable): profitable, FCF &gt; 0, net debt/EBITDA &lt; {score.MAX_ND_EBITDA},
Piotroski ≥ {score.MIN_FSCORE:.0f}, mcap &gt; $2B, price &gt; $5, ADV &gt; $10M (financials exempt from FCF/leverage).
Sub-scores are sector percentiles, 100 = best; Price 100 = most beaten down (context only, not ranked on).
Returns include dividends. Click a header to sort. {html.escape(note)}</p>
<h2>Ranked bargains (top {min(top, len(passed))} of {len(passed)})</h2><div class=wrap>{_table(passed.head(top), "top")}</div>
<h2>Near misses (fail exactly one hard filter)</h2><div class=wrap>{_table(near, "near", "Failed")}</div>
<h2>Definitely not bargains (most expensive vs. sector peers)</h2><div class=wrap>{_table(dear, "dear")}</div>
<p>Fundamentals: SEC EDGAR XBRL, trailing twelve months, as filed. Prices: Yahoo. Full data: {csv.name}.</p>
<script>{JS}</script></body></html>"""
    htm = out_dir / f"screen_{d}.html"
    htm.write_text(page)
    return htm, csv
