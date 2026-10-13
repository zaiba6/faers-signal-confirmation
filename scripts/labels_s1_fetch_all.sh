#!/bin/bash
# Fetch all Section 921 quarterly pages listed in data/raw/labels/s1_manifest.tsv (cached, 30s delay)
cd "$(dirname "$0")/.."
tail -n +2 data/raw/labels/s1_manifest.tsv | while IFS=$'\t' read q l u s; do echo -n "$q "; scripts/labels_fetch.sh "$u" "data/raw/labels/s1_$q.html"; done
