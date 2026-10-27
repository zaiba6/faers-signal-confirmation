#!/bin/bash
# One night of the SrLC detail-page scrape. Safe to run any time: the scraper
# sleeps until the 23:00-05:00 America/New_York window, exits when it closes,
# refuses to start if another instance holds the lock, and never re-fetches a
# page already saved in data/raw/labels/.
set -u
cd "$(dirname "$0")/.." || exit 1
PY=/usr/local/bin/python3
LOG=data/raw/labels/s2_scrape.log
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') start ===" >> "$LOG"
# caffeinate -i: no idle sleep while we are fetching (does not wake a sleeping Mac).
caffeinate -i "$PY" scripts/labels_s2_scrape.py >> "$LOG" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M:%S %Z') exit $? ===" >> "$LOG"
