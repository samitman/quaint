"""Ranked CSV + compact, self-contained HTML report from a scored cross-section."""
from __future__ import annotations

import html
import re
import urllib.parse
from pathlib import Path

import pandas as pd

from . import score

COLS = [  # (column, header, format)
    ("sec_name", "Company", "s"), ("sector", "Sector", "s"), ("price", "Price", "$"),
    ("mcap", "Mkt cap", "$B"), ("revenue", "Revenue", "$B"), ("net_income", "Net income", "$B"),
    ("implied_return", "Implied return", "%b"),
    ("cash_yield", "= Cash yield", "%"), ("growth_rate", "+ Growth/sh", "%"), ("pe", "P/E", "x"),
    ("ps", "P/S", "x"), ("net_margin", "Net mgn", "%"),
    ("return_on_capital", "ROIC/ROE", "%"), ("net_debt", "Net debt", "nd"),
    ("nd_ebitda", "ND/EBITDA", "x"), ("ret_1y", "1Y", "%±"), ("off_high", "vs 52w hi", "%"),
    ("ins_net_usd", "Insiders 6M", "ins"), ("why", "Why it's here", "s"),
    ("flags", "Flags", "s"),
]
CSV_COLS = ["rank", "ticker", "sec_name", "sector", "industry", "exchange", "price", "mcap",
            "implied_return", "cash_yield", "growth_rate", "pe", "ps", "revenue", "net_margin", "pb", "ev_ebit",
            "earnings_yield",
            "fcf_yield", "net_income", "fcf", "sbc", "roic", "roe", "op_margin", "gross_margin",
            "fcf_conversion", "rev_growth", "rev_cagr_3y", "rev_ps_growth", "rev_ps_cagr_3y",
            "ni_growth", "margin_change", "nd_ebitda", "debt_equity", "total_debt", "debt_prev",
            "debt_yoy", "cash", "net_debt", "ev", "fscore", "ret_1m", "ret_ytd", "ret_1y", "off_high",
            "ins_buyers", "ins_buy_usd", "ins_sell_usd", "ins_net_usd", "ins_net_pct", "red_flags",
            "warnings", "events",
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
    if kind == "nd":
        return "net cash " + _fmt(-v, "$B") if v < 0 else _fmt(v, "$B")
    if kind == "x":
        return f"{v:.1f}" if abs(v) < 1000 else "≫"
    if kind in ("%", "%b", "%±"):
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
    if r.get("red_flags"):
        out.append(f"RED FLAG: {r['red_flags']}")
    if (r.get("ins_buyers") or 0) >= 2:
        out.append("insider cluster buy")
    for k in ("warnings", "events"):
        out += [e for e in str(r.get(k) or "").split(", ") if e]
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
    return "; ".join(out)


def _ins_cell(r) -> str:
    """Insider buying in green, notable net selling (over 0.5% of market cap) muted."""
    b, net = r.get("ins_buyers"), r.get("ins_net_usd")
    if _na(b):
        return "<td>–</td>"
    from .insider import _usd
    heavy_sell = (r.get("ins_net_pct") or 0) < -0.005
    sold = f"sold {_usd(r.get('ins_sell_usd'))}"
    if b:
        return (f'<td class=up data-v="{r.get("ins_buy_usd")}">{int(b)} bought {_usd(r.get("ins_buy_usd"))}'
                f'{"<br><span class=s>" + sold + "</span>" if heavy_sell else ""}</td>')
    if heavy_sell:
        return f'<td class=s data-v="{net}">{sold}</td>'
    return '<td data-v="0">–</td>'


SECTOR_SHORT = {"Communication Services": "Comm. Services", "Consumer Discretionary": "Cons. Discretionary",
                "Information Technology": "Info. Tech", "Consumer Staples": "Cons. Staples"}
_WARN = ("FCF", "CHECK", "cyclical", "auditor change", "listing notice", "impairment")  # look before buying
_GOOD = ("insider cluster buy",)
_BAD = ("RED FLAG",)


_WORDS = {"INC", "CO", "LTD", "THE", "AND", "OF", "LLC", "NEW", "LP", "PLC", "DE"}


def _name(v) -> str:
    """SEC names are often in caps; title-case capitalized words, keeping short acronyms (ANI, LPG)."""
    def word(w):
        core = w.strip(".,/()&")
        if not core.isupper():
            return w
        return w if len(core) <= 3 and core not in _WORDS else w.title()
    name = re.sub(r"\s*/[A-Za-z]{2}/?\s*$", "", str(v))  # SEC's state-of-incorporation suffix: "CORP /DE/"
    return " ".join(word(w) for w in name.split())


def _ir_cell(r, scale: float) -> str:
    """Implied return with a two-part bar: dark = cash yield, light = growth per share."""
    ir, cy, g = r.get("implied_return"), r.get("cash_yield"), r.get("growth_rate")
    if _na(ir):
        return "<td class=b>–</td>"
    w = lambda x: 0 if _na(x) or x <= 0 else min(100.0, 100 * x / scale)
    bar = (f'<span class=bar><i class=cy style="width:{w(cy):.1f}%"></i>'
           f'<i class=gr style="width:{min(w(g), 100 - w(cy)):.1f}%"></i></span>')
    return f'<td class=b data-v="{ir}">{bar}{_fmt(ir, "%b")}</td>'


def _flag_cell(v) -> str:
    if _na(v) or not v:
        return "<td class=f></td>"
    def kind(f):
        return " bad" if f.startswith(_BAD) else " good" if f.startswith(_GOOD) else \
            " warn" if f.startswith(_WARN) else ""
    pills = "".join(f'<span class="pill{kind(f)}">{html.escape(f)}</span>' for f in str(v).split("; "))
    return f"<td class=f>{pills}</td>"


def _table(df: pd.DataFrame, tid: str, extra: str | None = None) -> str:
    heads = "".join(f"<th>{h}</th>" for h in ["#", "Ticker"] + [h for _, h, _ in COLS]
                    + ([extra] if extra else []))
    scale = max(0.2, float(df["implied_return"].max())) if len(df) else 0.2
    rows = []
    for i, (t, r) in enumerate(df.iterrows(), 1):
        url = f"https://finance.yahoo.com/quote/{urllib.parse.quote(str(t))}"
        cells = [f"<td class=k>{i}</td>",
                 f'<td class=t><a href="{url}" target=_blank rel=noopener>{html.escape(t)}</a></td>']
        for c, _, k in COLS:
            v = r.get(c)
            if c == "implied_return":
                cells.append(_ir_cell(r, scale))
                continue
            if c == "flags":
                cells.append(_flag_cell(v))
                continue
            if c == "ins_net_usd":
                cells.append(_ins_cell(r))
                continue
            if c == "sec_name" and not _na(v):
                v = _name(v)
            if c == "sector" and not _na(v):
                v = SECTOR_SHORT.get(v, v)
            raw = "" if _na(v) or k == "s" else f' data-v="{v}"'
            cls = {"s": " class=s"}.get(k, "")
            if c in ("sec_name", "sector"):
                cls = " class=n"
            elif k in ("%±", "nd") and not _na(v):  # direction at a glance: up/net cash vs down/net debt
                good = v > 0 if k == "%±" else v < 0
                cls = " class=up" if good else (" class=dn" if k == "%±" else "")
            cells.append(f"<td{cls}{raw}>{_fmt(v, k)}</td>")
        if extra:
            cells.append(f"<td class=s>{html.escape(str(r.get('failed', '')).replace(',', ', '))}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<div class=wrap><table id={tid}><thead><tr>{heads}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--mut:#5f5f5f;--line:#e2e2e0;--head:#eef1ef;--stripe:#f1f4f2;--hover:#e6f2ec;
--acc:#0b6e4f;--acc2:#8fcfb5;--up:#1a7f37;--dn:#b42318;--card:#f8f9f8;--warn:#8a5a00;--warnbg:#fdf0d5;--goodbg:#e3f3e8;--badbg:#fde4e1}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141414;--fg:#e8e8e8;--mut:#a3a3a3;
--line:#2c2c2c;--head:#1f2522;--stripe:#1d201f;--hover:#1f2e27;--acc:#4fc79a;--acc2:#1f6b51;--up:#5cc98a;--dn:#ff8a7a;--card:#1a1c1b;--warn:#f5c26b;--warnbg:#3a2e14;--goodbg:#173322;--badbg:#3d1d1a}}
:root[data-theme="dark"]{--bg:#141414;--fg:#e8e8e8;--mut:#a3a3a3;--line:#2c2c2c;--head:#1f2522;--stripe:#1d201f;
--hover:#1f2e27;--acc:#4fc79a;--acc2:#1f6b51;--up:#5cc98a;--dn:#ff8a7a;--card:#1a1c1b;--warn:#f5c26b;--warnbg:#3a2e14;--goodbg:#173322;--badbg:#3d1d1a}
body{background:var(--bg);color:var(--fg);font:13px/1.45 -apple-system,system-ui,Segoe UI,sans-serif;margin:24px 16px}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px}p{color:var(--mut);max-width:1100px}
.legend{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 16px;max-width:1100px;margin:12px 0}
.legend summary{cursor:pointer;font-weight:600}
.legend dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 16px;margin:10px 0 4px}
.legend dt{font-weight:600}.legend dd{margin:0;color:var(--mut)}
.legend .formula{font-weight:600;color:var(--acc)}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px}table{border-collapse:collapse;width:100%}
th,td{padding:6px 9px;text-align:right;white-space:nowrap}
th{background:var(--head);position:sticky;top:0;cursor:pointer;font-weight:600;border-bottom:1px solid var(--line)}
tbody tr:nth-child(even){background:var(--stripe)}tbody tr:hover{background:var(--hover)}
td.t{font-weight:700;text-align:left}td.t a{color:var(--acc);text-decoration:none}
td.t a:hover{text-decoration:underline}td.t a::after{content:" ↗";font-size:10px;font-weight:400;color:var(--mut)}td.n,td.s{text-align:left}
td.n{max-width:220px;overflow:hidden;text-overflow:ellipsis}td.s{color:var(--mut);white-space:normal;min-width:220px}
td.b{font-weight:700;color:var(--fg)}td.up{color:var(--up)}td.dn{color:var(--dn)}
td.s{font-size:12px;max-width:300px}
th:nth-child(1),td.k{position:sticky;left:0;z-index:1;background:inherit;width:24px}
th:nth-child(2),td.t{position:sticky;left:34px;z-index:1;background:inherit}
th:nth-child(1),th:nth-child(2){z-index:2;background:var(--head)}
tbody tr{background:var(--bg)}
.bar{display:inline-flex;width:64px;height:8px;margin-right:8px;vertical-align:middle;border-radius:4px;
background:var(--line);overflow:hidden}
.bar i{display:block;height:100%}.bar i.cy{background:var(--acc)}.bar i.gr{background:var(--acc2)}
td.f{text-align:left;white-space:normal;min-width:140px}
.pill{display:inline-block;font-size:11px;padding:1px 7px;margin:1px 3px 1px 0;border-radius:10px;
background:var(--line);color:var(--mut);white-space:nowrap}
.pill.warn{background:var(--warnbg);color:var(--warn)}
.pill.good{background:var(--goodbg);color:var(--up)}.pill.bad{background:var(--badbg);color:var(--dn);font-weight:600}
.news{max-width:1100px}.news h3{font-size:13px;margin:14px 0 4px}.news ul{margin:0;padding-left:18px}
.news li{margin:2px 0;color:var(--mut)}.news a{color:var(--fg)}.news .src{font-size:12px}
@media (max-width:640px){.legend dl{grid-template-columns:1fr;gap:0 0}.legend dd{margin-bottom:8px}}
.key{display:inline-flex;align-items:center;gap:6px;margin-right:14px}
.key i{display:inline-block;width:14px;height:8px;border-radius:4px}
"""
JS = """
document.querySelectorAll('th').forEach((th,i)=>th.onclick=()=>{const tb=th.closest('table').tBodies[0];
const rows=[...tb.rows];const d=th.dataset.d=th.dataset.d==='a'?'d':'a';
const key=r=>{const c=r.cells[i];return c.dataset.v!==undefined?parseFloat(c.dataset.v):c.textContent};
rows.sort((a,b)=>{const x=key(a),y=key(b);const r=typeof x==='number'&&typeof y==='number'?x-y:String(x).localeCompare(String(y));return d==='a'?r:-r});
rows.forEach(r=>tb.appendChild(r))});
"""


def _news(news: dict, names: dict) -> str:
    parts = []
    for t, items in (news or {}).items():
        if not items:
            continue
        lis = "".join(
            f'<li><a href="{html.escape(h["link"])}" target=_blank rel=noopener>{html.escape(h["title"])}</a>'
            f' <span class=src>· {html.escape(h["source"])}'
            f'{", " + format(h["date"], "%b %d") if pd.notna(h["date"]) else ""}</span></li>' for h in items)
        parts.append(f"<h3>{html.escape(t)} · {html.escape(_name(names.get(t, '')))}</h3><ul>{lis}</ul>")
    if not parts:
        return ""
    return ('<h2>Recent headlines on the top picks (30 days)</h2><div class=news>' + "".join(parts)
            + "<p>Context only; headlines don't affect the ranking.</p></div>")


def write(scored: pd.DataFrame, out_dir: Path, top: int = 25, note: str = "",
          news: dict | None = None) -> tuple[Path, Path]:
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
<p>{len(core)} companies pass every gate, plus {len(fins)} financials ranked on their own list. Ranked by implied return.
Universe: S&amp;P 600, 400, 500 and Nasdaq-100. Click a ticker to open it on Yahoo Finance, or any column header to sort. {html.escape(note)}</p>
<details class=legend open><summary>How to read this report</summary><dl>
<dt>Implied return</dt><dd><span class=formula>= Cash yield + Growth/sh.</span> Roughly what an owner earns per year if the
valuation multiple never changes. Higher is cheaper for what you get. Bar:
<span class=key><i style="background:var(--acc)"></i>cash yield</span><span class=key><i style="background:var(--acc2)"></i>growth</span></dd>
<dt>Cash yield</dt><dd>Average of earnings yield (net income ÷ market cap) and free-cash-flow yield. Free cash flow =
operating cash flow − capital spending − stock-based pay, and counts for at most {score.FCF_CAP:g}× earnings.
Financials use earnings yield only.</dd>
<dt>Growth/sh</dt><dd>Revenue per share growth: the lower of the last 12 months and the 3-year average, between 0% and
{score.MAX_GROWTH:.0%}. Per share, so growth bought by issuing stock doesn't count.</dd>
<dt>Revenue, Net income</dt><dd>Last 12 months, in dollars, from SEC filings.</dd>
<dt>P/E, P/S</dt><dd>Market cap ÷ net income and ÷ revenue (last 12 months).</dd>
<dt>Net mgn</dt><dd>Net income ÷ revenue.</dd>
<dt>ROIC/ROE</dt><dd>After-tax operating profit ÷ (equity + debt − cash). Financials show return on equity.</dd>
<dt>Net debt</dt><dd>Total debt − cash. <span style="color:var(--up)">Net cash</span> means more cash than debt.</dd>
<dt>ND/EBITDA</dt><dd>Net debt ÷ operating profit before depreciation: years of profit to pay off debt. Negative = net cash.</dd>
<dt>1Y, vs 52w hi</dt><dd>Price change over 1 year, and distance below the 52-week high. Context only, not used in the ranking.</dd>
<dt>Insiders 6M</dt><dd>Open-market buys by officers and directors in the last 6 months (SEC Form 4), or net
selling when it exceeds 0.5% of market cap. Grants, option exercises and pre-scheduled (10b5-1) sales are ignored.
Context only: in our 2017–2026 backtest insider buying did not improve the picks.</dd>
<dt>Flags</dt><dd><span class="pill good">green</span> = 2+ insiders bought. <span class="pill warn">amber</span> = check
before buying (incl. auditor change or impairment filed in an 8-K). Grey chips note recent 8-K events (major agreement,
acquisition/sale) and data caveats. A restatement or bankruptcy filing in the last year excludes a company. <i>cyclical</i>: earnings may be at a cycle peak. <i>FCF N× earnings</i>: cash flow and profit disagree;
check why. <i>Yahoo-filled</i>, <i>debt from 10-K</i>, <i>lagged filing</i>: some data is older or from Yahoo.</dd>
<dt>Gates</dt><dd>Every ranked company passes all of these: US headquarters; market cap ${score.MIN_MCAP / 1e9:g}B–${score.MAX_MCAP / 1e9:g}B;
price over $5; median daily trading over ${score.MIN_ADV / 1e6:g}M; profitable; positive free cash flow;
net margin ≥ {score.MIN_NET_MARGIN:.0%}{f"; P/S under {score.MAX_PS:g}" if score.MAX_PS else ""}{"; P/S below sector median" if score.PS_VS_SECTOR else ""};
revenue per share up over 1 and 3 years;{" operating profit per share up over 3 years;" if score.PROFITS_GROWING else ""} ROIC ≥ {score.MIN_RETURN_ON_CAPITAL:.0%}; margins not down more than
{score.MAX_MARGIN_DROP * 100:.0f} points; net debt/EBITDA under {score.MAX_ND_EBITDA:g}{f"; Piotroski score ≥ {score.MIN_FSCORE:.0f}" if score.MIN_FSCORE else ""}.
Financials skip the cash-flow, margin and debt tests.</dd>
</dl></details>
<h2>Top {min(top, len(core))} picks (max {score.MAX_PER_SECTOR} per sector)</h2>{_table(score.top_n(core, "composite", top), "top")}
{_news(news, s["sec_name"].to_dict())}
<h2>Financials: top {min(10, len(fins))} (banks, insurers, lenders)</h2>{_table(fins.head(10), "fin")}
<h2>Near misses: high implied return, one gate failed</h2>{_table(near, "near", "Failed gate")}
<h2>Definitely not bargains: lowest implied return</h2>{_table(dear, "dear")}
<p>Fundamentals: SEC EDGAR filings (trailing 12 months, as filed). Prices: Yahoo, live. All fields incl. sub-scores
and sector percentiles: {csv.name}. Ask Claude about any ticker for a full breakdown.</p>
<script>{JS}</script></body></html>"""
    htm = out_dir / f"screen_{d}.html"
    htm.write_text(page)
    return htm, csv
