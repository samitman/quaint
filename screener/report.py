"""Ranked CSV + compact, self-contained HTML report from a scored cross-section."""
from __future__ import annotations

import html
from pathlib import Path

import pandas as pd

from . import score

COLS = [  # (column, header, format)
    ("sec_name", "Company", "s"), ("sector", "Sector", "s"), ("price", "Price", "$"),
    ("mcap", "Mkt cap", "$B"), ("revenue", "Revenue", "$B"), ("net_income", "Net income", "$B"),
    ("implied_return", "Implied return", "%b"),
    ("cash_yield", "= Cash yield", "%"), ("growth_rate", "+ Growth/sh", "%"), ("pe", "P/E", "x"),
    ("ps", "P/S", "x"), ("net_margin", "Net mgn", "%"),
    ("return_on_capital", "ROIC/ROE", "%"), ("nd_ebitda", "ND/EBITDA", "x"),
    ("ret_1y", "1Y", "%"), ("off_high", "vs 52w hi", "%"), ("why", "Why it's here", "s"),
    ("flags", "Flags", "s"),
]
CSV_COLS = ["rank", "ticker", "sec_name", "sector", "industry", "exchange", "price", "mcap",
            "implied_return", "cash_yield", "growth_rate", "pe", "ps", "revenue", "net_margin", "pb", "ev_ebit",
            "earnings_yield",
            "fcf_yield", "net_income", "fcf", "sbc", "roic", "roe", "op_margin", "gross_margin",
            "fcf_conversion", "rev_growth", "rev_cagr_3y", "rev_ps_growth", "rev_ps_cagr_3y",
            "ni_growth", "margin_change", "nd_ebitda", "debt_equity", "total_debt", "debt_prev",
            "debt_yoy", "cash", "ev", "fscore", "ret_1m", "ret_ytd", "ret_1y", "off_high",
            "value", "profitability", "balance_sheet", "growth", "price_action", "bargain", "blend",
            "passes", "failed", "why", "flags", "cyclical", "period_end", "data_age_days",
            "shares", "shares_source", "debt_stale", "yahoo_fields", "adv_usd", "index"]


def _na(v):
    return v is None or (isinstance(v, float) and pd.isna(v))


def _fmt(v, kind):
    if _na(v):
        return "–"
    if kind == "s":
        return html.escape(str(v))
    if kind == "$":
        return f"{v:,.2f}"
    if kind == "B":
        return f"{v / 1e9:,.1f}B"
    if kind == "$B":  # dollars, scaled: $812M, $1.25B, -$40M
        a, sign = abs(v), "-" if v < 0 else ""
        return f"{sign}${a / 1e9:,.2f}B" if a >= 1e9 else f"{sign}${a / 1e6:,.0f}M"
    if kind == "x":
        return f"{v:.1f}" if abs(v) < 1000 else "≫"
    if kind in ("%", "%b"):
        return f"{100 * v:.1f}%"
    return str(v)


def why(r) -> str:
    """One line a reader can act on: where the return comes from and the balance sheet."""
    parts = []
    y = "earnings yield" if r.get("is_financial") else "cash yield"
    parts.append(f"{100 * r['cash_yield']:.0f}% {y} + {100 * r['growth_rate']:.0f}% growth/share")
    nd = r.get("nd_ebitda")
    if r.get("is_financial"):
        parts.append(f"ROE {100 * r['roe']:.0f}%" if not _na(r.get("roe")) else "")
    elif not _na(nd):
        parts.append("net cash" if nd < 0 else f"{nd:.1f}x net debt/EBITDA")
    dy = r.get("debt_yoy")
    if not _na(dy) and abs(dy) >= 0.10 and (r.get("total_debt") or 0) > 0:
        parts.append(f"debt {'up' if dy > 0 else 'down'} {abs(100 * dy):.0f}%")
    mc = r.get("margin_change") if not r.get("is_financial") else r.get("roe_change")
    if not _na(mc) and abs(mc) >= 0.01:
        parts.append(f"{'margins' if not r.get('is_financial') else 'ROE'} {100 * mc:+.0f}pp")
    oh = r.get("off_high")
    if not _na(oh) and oh <= -0.15:
        parts.append(f"{abs(100 * oh):.0f}% below 52w high")
    return "; ".join(p for p in parts if p)


def _flags(r) -> str:
    out = []
    if r.get("cyclical"):
        out.append("cyclical")
    if r.get("debt_estimated"):
        out.append("debt est.")
    if r.get("debt_stale"):
        out.append("debt from 10-K")
    if r.get("yahoo_fields"):
        out.append("Yahoo-filled")
    if (r.get("data_age_days") or 0) > 150:
        out.append("lagged filing")
    if r.get("shares_source") not in ("cover", "diluted", None):
        out.append(f"shares: {r.get('shares_source')}")
    fcf, ni = r.get("fcf"), r.get("net_income")
    if not r.get("is_financial") and not _na(fcf) and not _na(ni) and ni > 0:
        k = fcf / ni
        if k > score.FCF_CAP or k < 1 / score.FCF_CAP:
            out.append(f"FCF {k:.1f}x earnings")
    if r.get("xcheck"):
        out.append(f"CHECK: {r['xcheck']}")
    return ", ".join(out)


def _table(df: pd.DataFrame, tid: str, extra: str | None = None) -> str:
    heads = "".join(f"<th>{h}</th>" for h in ["#", "Ticker"] + [h for _, h, _ in COLS]
                    + ([extra] if extra else []))
    rows = []
    for i, (t, r) in enumerate(df.iterrows(), 1):
        cells = [f"<td>{i}</td>", f"<td class=t>{html.escape(t)}</td>"]
        for c, _, k in COLS:
            v = r.get(c)
            raw = "" if _na(v) or k == "s" else f' data-v="{v}"'
            cls = {"%b": " class=b", "s": " class=s"}.get(k, "")
            if c in ("sec_name", "sector"):
                cls = " class=n"
            cells.append(f"<td{cls}{raw}>{_fmt(v, k)}</td>")
        if extra:
            cells.append(f"<td class=s>{html.escape(str(r.get('failed', '')).replace(',', ', '))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<div class=wrap><table id={tid}><thead><tr>{heads}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#666;--line:#e3e3e3;--head:#f5f5f3;--acc:#0b6e4f}
@media (prefers-color-scheme:dark){:root{--bg:#141414;--fg:#e8e8e8;--mut:#a0a0a0;--line:#2c2c2c;--head:#1e1e1e;--acc:#4fc79a}}
body{background:var(--bg);color:var(--fg);font:13px/1.4 -apple-system,system-ui,Segoe UI,sans-serif;margin:24px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 6px}p{color:var(--mut);max-width:1000px}
.wrap{overflow-x:auto}table{border-collapse:collapse}
th,td{padding:4px 8px;border-bottom:1px solid var(--line);text-align:right;white-space:nowrap}
th{background:var(--head);position:sticky;top:0;cursor:pointer;font-weight:600}
td.t{font-weight:700;color:var(--acc);text-align:left}td.n,td.s{text-align:left}
td.n{max-width:220px;overflow:hidden;text-overflow:ellipsis}td.s{color:var(--mut);white-space:normal;min-width:200px}
td.b{font-weight:700}
"""
JS = """
document.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>{const tb=th.closest('table').tBodies[0];
const rows=[...tb.rows];const d=th.dataset.d=th.dataset.d==='a'?'d':'a';
const key=r=>{const c=r.cells[i];return c.dataset.v!==undefined?parseFloat(c.dataset.v):c.textContent};
rows.sort((a,b)=>{const x=key(a),y=key(b);const r=typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y));return d==='a'?r:-r});
rows.forEach(r=>tb.appendChild(r))});
"""


def write(scored: pd.DataFrame, out_dir: Path, top: int = 25, note: str = "") -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    d = pd.Timestamp(scored["date"].iloc[0]).date()
    s = scored.sort_values("composite", ascending=False).copy()
    fin = s["is_financial"].astype(bool)
    s["return_on_capital"] = s["roic"].where(~fin, s["roe"])
    s["why"] = [why(r) for _, r in s.iterrows()]
    s["flags"] = [_flags(r) for _, r in s.iterrows()]
    passed = s[s["passes"]]
    is_fin = passed["is_financial"].astype(bool)
    core, fins = passed[~is_fin], passed[is_fin]
    s["rank"] = None
    for grp in (core, fins):  # financials are ranked on their own list
        s.loc[grp.index, "rank"] = range(1, len(grp) + 1)
    csv = out_dir / f"screen_{d}.csv"
    s[[c for c in CSV_COLS if c in s.columns]].to_csv(csv, index_label="ticker_id")

    inv = s[s["investable"]]
    nonfin = ~inv["is_financial"].astype(bool)
    near = inv[nonfin & ~inv["passes"] & (inv["failed"].str.count(",") == 0)
               & (inv["failed"] != "business_model")].head(15)
    dear = inv[nonfin].sort_values("implied_return").head(10)
    page = f"""<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><title>Bargain screen {d}</title>
<style>{CSS}</style></head><body>
<h1>Bargain screen · {d}</h1>
<p><b>Implied return</b> = cash yield (average of earnings and free-cash-flow yield) + revenue-per-share growth
(the lower of the last 12 months and the 3-year rate, capped at {score.MAX_GROWTH:.0%}). Roughly what an owner earns
per year if the valuation multiple never changes. Only companies passing every gate are ranked:
US-headquartered, market cap ${score.MIN_MCAP / 1e9:g}B–${score.MAX_MCAP / 1e9:g}B, {f"P/S under {score.MAX_PS:g}, " if score.MAX_PS else ""}{"P/S below its sector median, " if score.PS_VS_SECTOR else ""}net margin ≥ {score.MIN_NET_MARGIN:.0%} (financials skip the margin test), FCF positive, revenue per share growing (1y and 3y),
ROIC ≥ {score.MIN_RETURN_ON_CAPITAL:.0%}, margins not down more than {score.MAX_MARGIN_DROP * 100:.0f}pp, debt under control
(net debt/EBITDA &lt; {score.MAX_ND_EBITDA:g}), Piotroski ≥ {score.MIN_FSCORE:.0f}, price &gt; $5, median daily volume
&gt; ${score.MIN_ADV / 1e6:g}M. Universe: S&amp;P 600, 400, 500 and Nasdaq-100. {len(core)} companies pass, plus {len(fins)} financials ranked separately
(their earnings are levered). Revenue and net income are the last 12 months. Flag <i>cyclical</i> = earnings may be at a cycle peak. Click any header to sort. {html.escape(note)}</p>
<h2>Top {min(top, len(core))} picks (max {score.MAX_PER_SECTOR} per sector)</h2>{_table(score.top_n(core, "composite", top), "top")}
<h2>Financials: top {min(10, len(fins))} (banks, insurers, lenders)</h2>{_table(fins.head(10), "fin")}
<h2>Near misses: high implied return, one gate failed</h2>{_table(near, "near", "Failed gate")}
<h2>Definitely not bargains: lowest implied return</h2>{_table(dear, "dear")}
<p>Fundamentals: SEC EDGAR filings (trailing 12 months, as filed). Prices: Yahoo, live. All fields incl. sub-scores
and sector percentiles: {csv.name}. Ask Claude about any ticker for a full breakdown.</p>
<script>{JS}</script></body></html>"""
    htm = out_dir / f"screen_{d}.html"
    htm.write_text(page)
    return htm, csv
