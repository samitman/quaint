# Stock screener

Ranks profitable, cheap, financially clean S&P 500 / S&P 400 / Nasdaq-100 companies.
Fundamentals come from SEC EDGAR XBRL (official, point-in-time). Prices come from Yahoo via yfinance.

## Setup
```
pip install -r requirements.txt
export SEC_USER_AGENT="Your Name you@example.com"   # or put it in sec_user_agent.txt
export SCREENER_DATA=/fast/local/disk              # optional: cache location (~1.5GB), default ./data
```

## Run
```
python -m screener universe        # index members -> CIK + exchange (NYSE/Nasdaq/Cboe)
python -m screener fetch           # cache EDGAR facts (~900 companies) and 10y of prices
python -m screener screen          # ranked report -> reports/screen_<date>.html + .csv
python -m screener lookup KO SOFI  # one stock's rank and full breakdown; works outside the universe
python -m screener backtest        # point-in-time monthly backtest -> reports/backtest.html
python -m screener audit           # data-quality flags (missing tags, stale filings, share counts)
python -m pytest -q                # offline tests
```
Weights: `--w-value 0.5 --w-quality 0.25 --w-trajectory 0.25` on screen/lookup/backtest.

## Method
1. **Hard filters** (all must pass; missing data fails): mcap > $2B, price > $5, 3-month median
   dollar volume > $10M, TTM data < 200 days old, TTM net income > 0, FCF > 0, net debt/EBITDA < 1.5,
   Piotroski F >= 6 (rescaled over available signals), not a mortgage REIT. Financials skip FCF and
   leverage tests.
2. **Scores** are percentiles vs. investable peers in the same GICS sector:
   value (earnings yield, FCF yield, EBIT/EV, book/price), quality (ROIC, operating margin, FCF
   conversion), trajectory (revenue growth TTM and 3y CAGR, margin change, NI growth).
   Financials: E/P + B/P, ROE, growth + ROE change.
3. **Backtest**: month-end rebalance, fundamentals rebuilt from filings public on each date,
   equal weight, 10 bps one-way costs. Universe is today's members (survivorship bias; see report).

## XBRL handling worth knowing
- Only 10-K/10-Q-family facts are used; proxy-statement re-tags (e.g. SCHW, in thousands) are ignored.
- Quarters may be 12 or 16 weeks (COST, PEP); Q4 is derived as FY minus 9M YTD.
- Tags that stop being used are treated as stale, not current. Debt tagged only in 10-Ks is carried
  forward up to a year and flagged. Debt that exists only in company-specific tags is estimated from
  interest expense and flagged.
- Share count = whichever of cover page, diluted average and FY NI/EPS two sources agree on
  (MCD tags diluted shares in millions; GOOGL's cover shows one class). STZ/BRK fall back to Yahoo.
- Holding-company reorgs (XOM 2026) merge the predecessor CIK's history: `edgar.PREDECESSORS`.
- Known gap: SEC companyfacts lags for ~70 filers (latest quarter missing); flagged "Q1 data".
