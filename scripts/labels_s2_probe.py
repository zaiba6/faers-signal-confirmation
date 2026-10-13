"""SrLC probe (NOT the full scrape). Time-gated to 23:00-05:00 America/New_York, 30 s between requests, robots-checked.

Run: nohup /usr/local/bin/python3 scripts/labels_s2_probe.py &      (sleeps until the window opens)
Queries (POST event=searchResult.page, all 7 `section` boxes like the form):
  - every month of 2016 and of 2025 (compare sparsity / cap), every full year 2016..2026, one full-range query 2016-01-01..today
  - 5 detail pages (event=searchdetail.page&DrugNameID=N) taken from the 2016 results
Writes raw HTML to data/raw/labels/s2_q_*.html, s2_detail_*.html; summary to data/processed/labels/s2_probe_counts.csv.
"""
import sys, json, calendar, datetime as dt, re
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from labels_polite import *
from bs4 import BeautifulSoup
import pandas as pd

BASE = "https://www.accessdata.fda.gov/scripts/cder/safetylabelingchanges/index.cfm"
WINDOW = (23, 5, "America/New_York")
SECTIONS = ["BW", "C", "WP", "AR", "DI", "USP", "MG"]
sess = requests.Session()
sess.headers["Referer"] = BASE


def query(tag, d1, d2):
    out = RAW / f"s2_q_{tag}.html"
    data = [("date_range_from", d1.strftime("%m/%d/%Y")), ("date_range_to", d2.strftime("%m/%d/%Y"))] + \
           [("section", s) for s in SECTIONS] + [("Datesearch", "")]
    fetch(BASE + "?event=searchResult.page", out, method="POST", data=data, sess=sess, window=WINDOW)
    meta = json.load(open(str(out) + ".meta.json"))
    ok = len(meta["chain"]) == 1          # a redirect back to index.cfm means the form was rejected
    soup = BeautifulSoup(out.read_text(encoding="utf-8", errors="replace"), "html.parser")
    tab = soup.find(id="example")
    rows = []
    if tab:
        for tr in tab.find_all("tr")[1:]:
            tds = tr.find_all("td")
            a = tr.find("a", href=True)
            if len(tds) >= 6:
                rows.append(dict(tag=tag, drug_name=tds[0].get_text(" ", strip=True), active_ingredient=tds[1].get_text(" ", strip=True),
                                 application_number=tds[2].get_text(" ", strip=True), application_type=tds[3].get_text(" ", strip=True),
                                 supplement_date=tds[4].get_text(" ", strip=True), database_updated=tds[5].get_text(" ", strip=True),
                                 detail_href=a["href"] if a else "", source_file=str(out.relative_to(ROOT))))
    return ok, rows


def main():
    # session cookies (CFID/CFTOKEN) come from the first GET
    fetch(BASE, RAW / "s2_session_init.html", sess=sess, window=WINDOW)
    counts, allrows = [], []
    today = dt.date.today()
    jobs = []
    for y in (2016, 2025):
        for m in range(1, 13):
            last = calendar.monthrange(y, m)[1]
            jobs.append((f"{y}-{m:02d}", dt.date(y, m, 1), dt.date(y, m, last)))
    for y in range(2016, 2027):
        jobs.append((f"year{y}", dt.date(y, 1, 1), min(dt.date(y, 12, 31), today)))
    jobs.append(("full_2016-01-01_to_today", dt.date(2016, 1, 1), today))
    for tag, d1, d2 in jobs:
        ok, rows = query(tag, d1, d2)
        n_ids = len({re.search(r"DrugNameID=(\d+)", r["detail_href"]).group(1) for r in rows if "DrugNameID" in r["detail_href"]})
        counts.append(dict(tag=tag, date_from=d1, date_to=d2, accepted=ok, n_rows=len(rows), n_unique_detail_ids=n_ids))
        allrows += rows
        print(counts[-1], flush=True)
        pd.DataFrame(counts).to_csv(ROOT / "data/processed/labels/s2_probe_counts.csv", index=False)
        pd.DataFrame(allrows).to_csv(ROOT / "data/processed/labels/s2_probe_rows.csv", index=False)
    # 5 detail pages spread over the 2016 results
    r16 = [r for r in allrows if r["tag"].startswith("2016-") and r["detail_href"]]
    pick = [r16[i] for i in sorted({0, len(r16) // 4, len(r16) // 2, 3 * len(r16) // 4, len(r16) - 1})] if r16 else []
    for r in pick:
        did = re.search(r"DrugNameID=(\d+)", r["detail_href"]).group(1)
        fetch("https://www.accessdata.fda.gov" + r["detail_href"], RAW / f"s2_detail_{did}.html", sess=sess, window=WINDOW)
        print("detail", did, r["drug_name"], flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
