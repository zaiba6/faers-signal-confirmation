# SrLC detail-page scrape — how to run

One request per 30 s inside the 23:00–05:00 America/New_York window that accessdata asks for,
so ~650 pages a night. Run `scripts/s2_status.sh` for how many are left.

Everything is resume-safe: a page already in `data/raw/labels/s2_detail_<id>.html` is never
re-fetched, and only one instance can run at a time (flock on `s2_scrape.lock`).

## How it is run: manually, one night at a time

```bash
nohup scripts/s2_nightly.sh > /dev/null 2>&1 &
```

Safe to start any time of day: it waits for 23:00, fetches, exits at 05:00. The Mac must be
plugged in and the lid open through the window - nothing wakes a sleeping Mac.

A launchd agent was tried and dropped. `com.zaiba.faers.srlc.plist` is kept here as a dormant
template only; it is NOT installed. Two things to know if you ever install it again: macOS TCC
blocks launchd jobs from reading anything under ~/Documents (exit 126), which is why this repo
now lives in ~/Developer, and launchd still cannot wake a sleeping Mac.

## Check progress

```bash
scripts/s2_status.sh
```

## If it stops early

The scraper refuses to work around a block. It writes `data/raw/labels/s2_scrape_STOPPED.txt`
and exits 2 on: robots.txt disallow, HTTP 403/429, a captcha/challenge page, an unexpected
page shape, or 5 consecutive failures. Read that file before restarting — do not just relaunch.

## Do not run two at once

Each process keeps its own 30-second timer, so N processes = N× the request rate against FDA.
The lock now prevents this, but check `scripts/s2_status.sh` before launching anything.
