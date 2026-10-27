"""SrLC detail-page scrape. Time-gated (23:00-05:00 America/New_York), 30 s/request, robots-checked, cached => resume-safe.

Run: scripts/s2_nightly.sh  (or, for one manual night: nohup caffeinate -i /usr/local/bin/python3 scripts/labels_s2_scrape.py >> data/raw/labels/s2_scrape.log 2>&1 &)
Only ONE instance can run at a time (flock on data/raw/labels/s2_scrape.lock); a second one exits 0 immediately.
Exits 0 when the 05:00 window closes, so it is meant to be re-launched each night (see scripts/com.zaiba.faers.srlc.plist).
Queue order: strict ingredient matches (exact, salt_stripped), then component_any, then none, then blank_ingredient; within each, by supplement date desc.
Stops (exit 2, writes data/raw/labels/s2_scrape_STOPPED.txt with the reason) on: robots disallow, HTTP 403/429/5xx-after-retries, captcha/challenge text,
a page without the expected SrLC detail markers, or 5 consecutive failures. It never works around a block.
Use --dry-run to print the queue without any network access.
"""
import sys, re, json, time, fcntl, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0, str(Path(__file__).parent))
from labels_polite import *
from labels_polite import _gate
import pandas as pd

WINDOW = (23, 5, "America/New_York")
BASE = "https://www.accessdata.fda.gov"
STOP = RAW / "s2_scrape_STOPPED.txt"
LOCK = RAW / "s2_scrape.lock"
ORDER = {"exact": 0, "salt_stripped": 0, "component_any": 1, "none": 2, "blank_ingredient": 3}


def queue():
    m = pd.read_csv(ROOT / "data/processed/labels/s2_ingredient_match.csv", keep_default_na=False)
    m["did"] = m.detail_href.str.extract(r"DrugNameID=(\d+)")[0]
    m["pri"] = m.match_level.map(ORDER)
    m["sd"] = pd.to_datetime(m.supplement_date, format="%m/%d/%Y")
    return m.sort_values(["pri", "sd"], ascending=[True, False]).drop_duplicates("did")


def valid(body):
    return "Download Data" in body and "Safety-related Labeling Changes Approved by FDA" in body


def window_open():
    s, e, tz = WINDOW
    h = dt.datetime.now(ZoneInfo(tz)).hour
    return (s <= h or h < e) if s > e else (s <= h < e)


def acquire_lock():
    """Single-instance guard: keeps the fd open for the life of the process."""
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another instance is already running; exiting", flush=True); sys.exit(0)
    fh.write(f"{dt.datetime.now().isoformat()} pid={__import__('os').getpid()}\n"); fh.flush()
    return fh


def stop(reason):
    STOP.write_text(f"{dt.datetime.now().isoformat()}  {reason}\n")
    print("STOPPED:", reason, flush=True); sys.exit(2)


def main():
    q = queue()
    todo = [r for _, r in q.iterrows() if not (RAW / f"s2_detail_{r.did}.html").exists()]
    print(f"queue {len(q)}, already saved {len(q) - len(todo)}, to fetch {len(todo)}", flush=True)
    if "--dry-run" in sys.argv:
        print(q.pri.value_counts().sort_index().to_dict(), q.head(3)[["drug_name", "match_level"]].to_dict("records")); return
    if STOP.exists(): STOP.unlink()
    _lock = acquire_lock()
    _gate(WINDOW)   # if launched during the day, wait here for 23:00 rather than exiting
    sess = requests.Session(); sess.headers["Referer"] = BASE + "/scripts/cder/safetylabelingchanges/index.cfm"
    fails, done = 0, 0
    for r in todo:
        if not window_open():
            print(f"{dt.datetime.now():%m-%d %H:%M} window closed; fetched {done} this run, {len(todo) - done} left", flush=True)
            sys.exit(0)
        out = RAW / f"s2_detail_{r.did}.html"
        for attempt in range(3):
            try:
                fetch(BASE + r.detail_href, out, sess=sess, window=WINDOW)
                body = out.read_text(encoding="utf-8", errors="replace")
                if re.search(r"captcha|access denied|unusual traffic|verify you are", body[:5000], re.I): out.unlink(); stop(f"challenge page at DrugNameID={r.did}")
                if not valid(body):
                    Path(str(out) + ".meta.json").rename(RAW / f"s2_detail_{r.did}.invalid.meta.json"); out.rename(RAW / f"s2_detail_{r.did}.invalid.html")
                    stop(f"unexpected page content at DrugNameID={r.did} (saved as .invalid.html)")
                fails = 0; done += 1; break
            except RobotsDisallowed as e: stop(str(e))
            except RuntimeError as e:
                if re.search(r"HTTP (403|429)", str(e)): stop(str(e))
                print("ERR", r.did, str(e)[:120], flush=True); time.sleep(60)
            except Exception as e:
                print("ERR", r.did, str(e)[:120], flush=True); time.sleep(60)
        else:
            fails += 1
            if fails >= 5: stop("5 consecutive failures")
        if done and done % 25 == 0: print(f"{dt.datetime.now():%m-%d %H:%M} fetched {done}/{len(todo)}", flush=True)
    print("DONE", done, flush=True)


if __name__ == "__main__":
    main()
