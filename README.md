# Stock screener

Finds small and mid-cap US companies that make money, are growing, have clean balance sheets and
trade cheaply relative to that ("growth at a bargain price"). Fundamentals come from SEC EDGAR XBRL
(as filed, point-in-time). Prices come from Yahoo via yfinance, live intraday for today.

## Resume in a new session
```
cd /mnt/project-files/stock-screener
bash scripts/bootstrap.sh          # deps + seed ~/screener-data from db/ + refresh stale data
python3 -m screener screen         # today's report -> reports/screen_<date>.html + .csv
python3 -m screener lookup KO SOFI # one stock's rank and full breakdown; works outside the universe
python3 -m screener backtest       # reuses the stored history; --rebuild recomputes it (~1h)
python3 -m pytest -q               # offline tests
```
The SEC contact goes in `sec_user_agent.txt` (gitignored) or `SEC_USER_AGENT`.

## Where data lives
Working caches sit on local disk (`~/screener-data`, override with `SCREENER_DATA`), because the
shared folder is a network mount and ~1,500 small files are slow there. `db/` in this folder is the
durable copy that survives sessions (`screener/store.py`):

| Path | Contents |
|---|---|
| `db/edgar/*.json.gz` | raw SEC companyfacts, one per company |
| `db/hq/*.txt` | HQ state/country per CIK (US-only gate) |
| `db/prices/*.parquet` | daily Close, Adj Close, Volume, Stock Splits (dates x tickers) |
| `db/universe.csv` | index membership, sector, CIK, exchange, HQ |
| `db/trades.parquet` | insider open-market trades from SEC Form 4, since 2016 |
| `db/screener.sqlite` | `screens`: every screen run, timestamped (forward track record); `history`: point-in-time monthly cross-sections behind the backtest |

`fetch`, `screen` and `backtest` push to `db/` when they finish; `fetch` pulls from it when the local
cache is empty. To see how past picks did: `select * from screens where rank <= 25` and join to prices.

## Method
Universe: S&P 600 + 400 + 500 + Nasdaq-100, NYSE/Nasdaq/Cboe listed.

1. **Gates** (all must pass; missing data fails): US headquarters; market cap $0.3B to $5B; price
   over $5; median daily dollar volume over $2M; filing under 200 days old; TTM net income and FCF
   positive; net margin at least 5% (financials skip it: bank revenue tags are unreliable); revenue per share up
   on TTM and 3-year; ROIC at least 10% (ROE for financials); margins not down more than 2pp;
   debt not up more than 25% of assets; net debt/EBITDA under 1.0; not a
   mortgage REIT; no restatement (8-K item 4.02) or bankruptcy (1.03) filed in the last year. Financials skip FCF and leverage gates and are ranked on their own list.
2. **Rank: implied return** = cash yield (mean of earnings yield and owner FCF yield, where FCF is
   after stock comp and counts for at most 2x earnings) + growth (the lower
   of TTM and 3-year revenue-per-share growth, clipped to 0 to 20%). It approximates an owner's
   annual return if the multiple never changes. Max 7 names per sector in the top 25.
3. **Context columns** (not ranked): sector-percentile sub-scores (value, profitability, balance
   sheet, growth), the Bargain residual, and price action (1M, YTD, 1Y, off 52-week high).
   Flags: cyclical (earnings may be at peak), debt estimated, Yahoo-filled, lagged filing.
   Insiders (`screener/insider.py`): open-market buys/sells by officers and directors in the last
   6 months from SEC Form 4 quarterly data sets (since 2016), topped up live from recent Form 4 XML;
   10b5-1 plan sales, grants and exercises ignored. 8-K events (`screener/events.py`): auditor change,
   listing notice, impairment (amber), major agreement, acquisition/sale (grey). Recent headlines
   (Google News RSS, filler filtered) for the top 25. None of these affect the rank.
4. **Backtest**: month-end rebalance on S&P 400 + 600 names, fundamentals rebuilt from filings
   public on each date, equal weight, 10 bps one-way costs, overlapping 1/3/12-month holds, vs SPY,
   an FCF-yield baseline and the equal-weight universe. Universe is today's members, so results
   carry survivorship bias.

## Chosen method and why (backtest 2017-02 to 2026-09, S&P 400 + 600, rerun 2026-10-17)
Default = gates above + implied return top 25, **no P/S gate, no Piotroski gate**, rebalanced monthly.
Numbers below are after fixing a share-count bug (year-ago diluted shares read as today's), which had
flattered the earlier run by ~3%/yr.
| Version (3-month hold) | CAGR | Sharpe | vs equal-weight universe |
|---|---|---|---|
| Default (no P/S, no Piotroski) | 21.2% | 0.94 | +3.4%/yr (t 1.0), ahead in both halves at 1m, 3m and 12m holds |
| With Piotroski >= 6 gate (old default) | 18.9% | 0.89 | +1.1%/yr |
| P/S under 4x / below sector median | 18.3% / 13.9% | 0.86 / 0.63 | +0.5% / -3.9%/yr |
| + FCF after acquisitions / no dilution >3% / excl. high accruals | 21.0% / 20.6% / 20.6% | ~0.94-0.98 | no gain; accruals filter trims drawdown |
| + shareholder yield / excl. fast asset growth | 19.7% / 17.2% | 0.90 / 0.86 | worse |
| + insider tilt / momentum gate / Bargain rank | 20.9% / 18.5% / 16.3% | | worse |
| Top 25 FCF yield (baseline) | 20.9% | 0.72 | max drawdown -60% |
| Equal-weight universe / SPY | 17.8% / 14.8% | 0.85 / 0.97 | |
Dropping one gate at a time: profit, FCF, ROIC, margin-trend and debt gates change returns by
<0.3%/yr each (redundant but harmless, kept as risk screens); dropping net margin hurts (-1.6%/yr);
dropping revenue growth helps (+1.5%/yr) but growth is a core requirement, and picking the best of
~25 variants overfits. Caveats: excess return is not statistically significant (t ~1); edge fades
with holding period; survivorship bias inflates all absolute numbers. Headline sentiment has no free
history to test, so it is context only; insider buying tested as no help, so display only.

Options: `--rank-by {implied_return,bargain,blend}`, `--max-mcap`, `--max-ps` (optional ceiling), `--ps-vs-sector`,
`--min-net-margin`, `--max-nd-ebitda`, `--min-fscore` (optional Piotroski floor), `--min-adv`, `--include-foreign`, `--top`.

## XBRL handling worth knowing
- Only 10-K/10-Q-family facts are used; proxy-statement re-tags (e.g. SCHW, in thousands) are ignored.
- Quarters may be 12 or 16 weeks (COST, PEP); Q4 is derived as FY minus 9M YTD.
- Tags that stop being used are treated as stale. Debt tagged only in 10-Ks is carried forward up to
  a year and flagged. Debt only in company-specific tags is estimated from interest expense, then
  replaced by Yahoo's balance sheet on live screens.
- Share count = whichever of cover page, diluted average and FY NI/EPS two sources agree on.
  Multi-class issuers without agreement fall back to Yahoo's market cap.
- Holding-company reorgs (XOM 2026) merge the predecessor CIK's history: `edgar.PREDECESSORS`.
- SEC companyfacts lags for ~70 filers by a quarter; live screens fill them from Yahoo and flag it.
- Sector overrides where GICS misleads: `universe.SECTOR_OVERRIDES` (FSLR -> Industrials).
