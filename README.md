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
python3 -m pytest -q               # offline tests (+ database tests when DATABASE_URL is set)
```
In a cloud session, `bash scripts/db_local.sh` starts the container's Postgres and loads it (`sync`, ~40 min);
then prefix commands with the `DATABASE_URL` it prints.
The SEC contact goes in `sec_user_agent.txt` (gitignored) or `SEC_USER_AGENT`.

## Guiding principles
1. **Thesis first.** Small and mid-cap US companies that make money, grow profits and revenue per share, and keep
   debt under control, bought when cheap. Every rule serves that thesis.
2. **Rules earn their place.** A new gate or ranking factor goes in only if it beats the current default in the
   point-in-time backtest, in both halves, without a worse drawdown. Otherwise it is display-only. Core thesis gates
   (profitable, growing, managed debt) stay even when the backtest is neutral, as risk screens.
3. **Point-in-time, fail closed.** Only data public on the date (SEC filing dates). Missing or unverifiable = fail.
4. **Owner economics, per share.** Growth per share; free cash flow after stock-based pay.
5. **Simple beats clever.** One transparent rank, few parameters, round thresholds. Count the variants tried and
   discount the winner (best of 30 backtests is partly luck).
6. **Investigate anomalies, don't rank them.** Odd numbers get a flag and a manual check before money moves.
7. **Honest about uncertainty.** Report t-stats, survivorship bias, decay. t ~1 is a hypothesis, not proof.
8. **Live prices, tracked record.** Today's prices; every run saved; the forward record is the final judge. Rerun monthly.
9. **The screen finds candidates; you decide.** Read the filings; spread risk across many names.

The same list is on the explorer's Method tab (`screener/explorer.py: PRINCIPLES`); keep the two in sync.

## Explorer
`python3 -m screener screen` also writes `reports/explorer_<date>.html`: one self-contained page (no internet
needed) with filters (price, market cap, profit, growth, debt, insider buying, sector, search, near misses), a
valuation map, distribution and sector charts, a sortable table with Yahoo/SEC/Finviz links, a detail panel with
headlines, and the backtest (growth of $1, drawdowns, every variant). The filter state lives in the URL, so a view can
be bookmarked. `python3 -m screener explorer` rebuilds it from the last screen without fetching prices.

## QUAINT (the app)
Search any ticker for a full breakdown (price with 50/200-day averages and insider trades, key numbers, revenue,
profit, margins, valuation history, per-share and cash/debt charts, 11 years of quarterly financials), compare up to
four tickers, browse the screen with filters, and follow the **track record**: each month's first screen opens an
equal-weight paper book of its top 25 at that run's prices, held until the next month's list, against SPY, IJR
(S&P 600) and IJH (S&P 400). That forward record has no hindsight or survivorship bias; the backtest has both.

**Run it on your machine (Docker):**
```
cp .env.example .env        # set SEC_USER_AGENT="Your Name you@example.com"
docker compose up -d        # Postgres + app + nightly worker
docker compose logs -f worker   # first load pulls SEC filings, prices and insider data (~1 hour)
```
Then open http://localhost:8765. The worker refreshes every weekday at 18:30 New York time (new filings, prices,
screen, track record). To reach it from other devices, put it behind a password (`QUAINT_PASSWORD`) and a host
you trust; the same compose file runs on any small server.

**Without Docker:** Postgres 14+, `pip install -r requirements.txt`, `export DATABASE_URL=postgresql://...`, then
`python3 -m screener refresh` once and `python3 -m screener app` (and `worker` for the nightly refresh).
Without DATABASE_URL everything still works from files, and `app` serves the static export in `web/data/`.

**Database (`screener/db.py`, `screener/warehouse.py`):**
| Table | Contents |
|---|---|
| `facts` | every tracked XBRL fact (75 concepts, periodic reports only) with its filing date |
| `snapshots` | fundamentals inputs as of each filing date, tagged with a hash of `fundamentals.py` |
| `prices` | daily close, adjusted close, volume, splits (universe + benchmarks + looked-up tickers) |
| `trades` | insider open-market trades (Form 4) since 2016 |
| `screen_runs`, `screen_rows` | every live screen, every company's numbers, with the method's code hash |
| `books` | the monthly paper portfolio |

Why history never needs a rebuild: a snapshot depends on the date only through "filed on or before", so it only
changes when a filing lands. Storing one per filing date makes any historical cross-section a lookup plus
`metrics.compute`; the backtest rebuilds its monthly history from them in minutes and caches it until new data
arrives. Changing metrics or scoring needs no rebuild; changing `fundamentals.py` or adding a concept does
(`sync --rebuild`). `sync` loads everything from the local caches; `refresh` tops it up.

**Hosted copy:** a private claude.ai artifact of the static export (universe only, refreshed when republished).
Valuation history uses each quarter's TTM figures against the price 45 days after quarter end (filing lag).

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
| Default (no P/S, no Piotroski) | 21.2% | 0.94 | +3.4%/yr (t 1.0); beats the old default in both halves at 1m, 3m and 12m holds |
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

### Healthy balance sheet and profit growth (tested 2026-10-27, history rebuilt)
Sam's definition: profitable, growing profits, managed debt. "Profitable" and "managed debt" were already gates.
Sam then asked for growth judged over time, not one year vs the last ("companies can have bad years"), so the
profit gate is: operating profit per share above 3 years ago (net income for financials); a dip in between is fine.
| Variant, CAGR (vs universe) | 1-month hold | 3-month hold | 12-month hold |
|---|---|---|---|
| With 3-year profit rule | 20.5% (+2.3) | 20.3% (+2.3) | 18.9% (+1.9) |
| Profit rule vs 1 year ago instead | 23.4% (+5.2) | 21.0% (+3.0) | 19.1% (+2.0) |
| **Default: no profit gate (revenue/share growth + margins holding)** | 24.8% (+6.6) | 21.4% (+3.4) | 19.4% (+2.3) |
| Small caps only ($0.3-2B), with 3-year rule | 22.5% (+4.3) | 23.8% (+5.9) | 21.3% (+4.2) |
| Cap up to $10B | 17.3% (-0.9) | 18.4% (+0.4) | 17.5% (+0.4) |
Read: no form of profit-growth gate adds return over 2017-2026; the 3-year rule costs ~1%/yr at a 3-month hold and
~4%/yr at 1 month (both halves). Revenue per share growing plus margins holding already implies profits growing,
so the explicit gate mostly removes cheap companies recovering from a dip. On 2021-2026 alone (first window with
3-year history in the quick test) the 3-year rule beat the 1-year rule. Sam chose (2026-10-27) to drop it as a gate: the 3-year profit trend is a column
and explorer filter; `--profit-growth` turns the gate back on. Also tested: net income growing (worse), ND/EBITDA < 0.5 and net cash only
(mixed across holds; net cash is an explorer filter). 42 of 553 investable names lack 3-year data and fail closed.
Interest coverage (operating profit / interest) is shown, not gated: ND/EBITDA < 1 already implies it, and it
isn't in the stored history.

### One-off tax benefits (adopted 2026-10-27)
A tax-benefit year (e.g. a valuation allowance released: QNST, RAMP) inflates net income and so the earnings yield.
When pretax profit is positive, tax is under 5% of it and net income exceeds pretax x 0.79, valuation uses pretax
profit taxed at 21% instead; the row is flagged "one-off tax benefit". Backtest vs earnings as reported: 24.8% vs
24.8% (1-month hold), 21.4% vs 21.4% (3-month), 19.8% vs 19.4% (12-month). Neutral on return, removes a known
distortion, so it's the default (`score.NORMALIZE_TAX`).

Options: `--rank-by {implied_return,bargain,blend}`, `--max-mcap`, `--max-ps` (optional ceiling), `--ps-vs-sector`,
`--min-net-margin`, `--max-nd-ebitda`, `--min-fscore` (optional Piotroski floor), `--profit-growth` (optional 3-year profit gate), `--min-adv`, `--include-foreign`, `--top`.

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
