# Stock screener

Ranks profitable, cheap, financially clean S&P 500 / S&P 400 / Nasdaq-100 companies.
Fundamentals come from SEC EDGAR XBRL (official, point-in-time). Prices come from Yahoo via yfinance.

## Setup
```
pip install -r requirements.txt
export SEC_USER_AGENT="Your Name you@example.com"   # SEC rejects anonymous requests
```

## Run
```
python -m screener universe        # index members -> CIK + exchange (NYSE/Nasdaq only)
python -m screener fetch           # cache EDGAR facts (~1k companies) and 10y of prices
python -m screener check AAPL KO   # sanity-check derived fundamentals
python -m pytest -q                # offline tests of the TTM / point-in-time math
```
Caches live in `data/` (EDGAR refreshed weekly, prices every 12h).
