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
| `db/screener.sqlite` | `screens`: every screen run, timestamped (forward track record); `history`: point-in-time monthly cross-sections behind the backtest |

`fetch`, `screen` and `backtest` push to `db/` when they finish; `fetch` pulls from it when the local
cache is empty. To see how past picks did: `select * from screens where rank <= 25` and join to prices.

## Method
Universe: S&P 600 + 400 + 500 + Nasdaq-100, NYSE/Nasdaq/Cboe listed.

1. **Gates** (all must pass; missing data fails): US headquarters; market cap $0.3B to $5B; price
   over $5; median daily dollar volume over $2M; filing under 200 days old; TTM net income and FCF
   positive; net margin at least 5%; P/S below its sector median and under 4x; revenue per share up
   on TTM and 3-year; ROIC at least 10% (ROE for financials); margins not down more than 2pp;
   debt not up more than 25% of assets; net debt/EBITDA under 1.0; Piotroski at least 6; not a
   mortgage REIT. Financials skip FCF and leverage gates and are ranked on their own list.
2. **Rank: implied return** = cash yield (mean of earnings yield and FCF yield) + growth (the lower
   of TTM and 3-year revenue-per-share growth, clipped to 0 to 20%). It approximates an owner's
   annual return if the multiple never changes. Max 7 names per sector in the top 25.
3. **Context columns** (not ranked): sector-percentile sub-scores (value, profitability, balance
   sheet, growth), the Bargain residual, and price action (1M, YTD, 1Y, off 52-week high).
   Flags: cyclical (earnings may be at peak), debt estimated, Yahoo-filled, lagged filing.
4. **Backtest**: month-end rebalance on S&P 400 + 600 names, fundamentals rebuilt from filings
   public on each date, equal weight, 10 bps one-way costs, overlapping 1/3/12-month holds, vs SPY,
   an FCF-yield baseline and the equal-weight universe. Universe is today's members, so results
   carry survivorship bias.

Options: `--rank-by {implied_return,bargain,blend}`, `--max-mcap`, `--max-ps`, `--ps-absolute`,
`--min-net-margin`, `--max-nd-ebitda`, `--min-adv`, `--include-foreign`, `--top`.

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
