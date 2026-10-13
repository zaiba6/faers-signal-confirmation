#!/bin/bash
# Polite cached fetch: labels_fetch.sh URL OUTFILE   (never re-fetches; 30s delay per robots.txt Crawl-Delay)
UA="faers-signal-confirmation-research/0.1 (academic research; amla.z@northeastern.edu)"
URL="$1"; OUT="$2"
if [ -s "$OUT" ]; then echo "CACHED $OUT"; exit 0; fi
sleep 30
curl -sS -m 60 -L -A "$UA" -D "$OUT.headers" -o "$OUT" -w "%{http_code} %{url_effective} %{size_download}B\n" "$URL"
