#!/usr/bin/env bash
# Start the container's Postgres, create the quaint database and load it from the local caches.
# Usage: bash scripts/db_local.sh   (then export the DATABASE_URL it prints)
set -e
cd "$(dirname "${BASH_SOURCE[0]}")/.."
pg_ctlcluster 16 main start 2>/dev/null || true
su postgres -c "psql -qtc \"select 1 from pg_roles where rolname='quaint'\" | grep -q 1 || psql -qc \"create role quaint login password 'quaint'\" -c 'create database quaint owner quaint'"
export DATABASE_URL=postgresql://quaint:quaint@localhost:5432/quaint
python3 -m screener sync
echo "export DATABASE_URL=$DATABASE_URL"
