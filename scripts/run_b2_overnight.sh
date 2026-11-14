#!/bin/bash
# Phase B2 unattended chain: wait for the history crawl, size the version download, then
# run it. Every step is resumable and caches to disk, so this can be killed and restarted.
cd "$(dirname "$0")/.."
PY=/usr/local/bin/python3
log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

log "waiting for the history crawl to finish"
while pgrep -f "phase_b2_label_history.py --step history" >/dev/null || pgrep -f "phase_b2_label_history.py$" >/dev/null; do
  sleep 60
done
log "history cached: $(ls data/raw/dailymed/history | wc -l | tr -d ' ')/4315"

log "resuming any history gaps"
$PY -u scripts/phase_b2_label_history.py --step history
log "history cached after resume: $(ls data/raw/dailymed/history | wc -l | tr -d ' ')/4315"

log "sizing the version download"
$PY -u scripts/phase_b2_label_history.py --step size || { log "SIZING FAILED"; exit 1; }

log "downloading version text"
$PY -u scripts/phase_b2_label_history.py --step text
log "version text cached: $(ls data/raw/dailymed/sections 2>/dev/null | wc -l | tr -d ' ')"
log "DONE"
