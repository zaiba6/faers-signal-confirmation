#!/bin/bash
# Progress of the SrLC detail-page scrape.
set -u
cd "$(dirname "$0")/.." || exit 1
TOTAL=$(( $(wc -l < data/processed/labels/s2_ingredient_match.csv) - 1 ))
HAVE=$(ls data/raw/labels/s2_detail_*.html 2>/dev/null | grep -vc invalid)
echo "saved $HAVE / ~$TOTAL detail pages  (~$(( (TOTAL - HAVE) / 120 )) h of fetching left, ~$(( ((TOTAL - HAVE) / 120 + 5) / 6 )) nights)"
if pgrep -f labels_s2_scrape.py > /dev/null; then echo "status: RUNNING (pid $(pgrep -f labels_s2_scrape.py | tr '\n' ' '))"; else echo "status: not running"; fi
if [ -f data/raw/labels/s2_scrape_STOPPED.txt ]; then echo "!! STOPPED: $(cat data/raw/labels/s2_scrape_STOPPED.txt)"; fi
echo "--- last 8 log lines ---"; grep -v '^\[gate\]' data/raw/labels/s2_scrape.log | tail -8
