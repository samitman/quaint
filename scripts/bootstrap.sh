#!/usr/bin/env bash
# Resume the screener in a fresh cloud session: deps, local cache seeded from db/, data refreshed.
# Usage: bash scripts/bootstrap.sh
set -e
cd "$(dirname "${BASH_SOURCE[0]}")/.."
git config --global --add safe.directory '*' 2>/dev/null || true
python3 -m pip install -q -r requirements.txt
python3 -m screener fetch   # pulls db/ into ~/screener-data, tops up anything stale, pushes back
