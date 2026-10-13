"""FDA Drug Safety Communications (DSC): parse the two index pages, then fetch each DSC page (robots-checked, 30 s delay, cached).

Run: /usr/local/bin/python3 scripts/labels_s3_dsc.py [--index-only]
Index sources (saved in data/raw/labels/):
  live  s3_dsc_index.html                      table (Date | Title) 2020-01-14 .. latest   (www.fda.gov)
  wayback s3_dsc_index_wayback_20200105.html   <li>date <a> list, snapshot 2020-01-09, DSCs dated <= 2019-12-19
None of the indexes lists DSCs FDA later archived/superseded (the page says so); those are NOT recoverable here.
  wayback year s3_dsc_year_{2016,2017,2018}.html  per-year archive pages (snapshot 2019-02-07)
  wayback legacy s3_dsc_index_wayback_legacy_2018.html  www.fda.gov/Drugs/DrugSafety/ucm199082.htm snapshot 2018-12-07 (Current + Previous sections)
Output: data/processed/labels/s3_dsc_index.csv  (dsc_date, title_raw, dsc_url, source_url, snapshot_date, source_file, page_file, notes)
"""
import sys, re, json, hashlib
from pathlib import Path
from urllib.parse import urljoin
sys.path.insert(0, str(Path(__file__).parent))
from labels_polite import *
from bs4 import BeautifulSoup
import pandas as pd

OUT = ROOT / "data/processed/labels"
DATE = re.compile(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4}|\d{2})\b")


def iso(s):
    m = DATE.search(s)
    if not m: return ""
    y = m.group(3)
    if len(y) == 2: y = "20" + y     # source gave a 2-digit year; flagged in notes by parse_index
    return f"{y}-{int(m.group(1)):02d}-{int(m.group(2)):02d}"


def norm(s): return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def snap(u):
    m = re.search(r"web\.archive\.org/web/(\d{14})/", u)
    return f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}" if m else ""


def parse_index():
    rows = []
    # live table
    f = RAW / "s3_dsc_index.html"
    base = "https://www.fda.gov"
    s = BeautifulSoup(f.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for tr in (s.find("main") or s).find("table").find_all("tr")[1:]:
        tds = tr.find_all("td"); a = tr.find("a", href=True)
        if len(tds) < 2 or not a: continue
        rows.append(dict(dsc_date_raw=norm(tds[0].get_text()), dsc_date=iso(tds[0].get_text()), title_raw=norm(tds[1].get_text(" ")),
                         dsc_url=urljoin(base, a["href"]), source_url="https://www.fda.gov/drugs/drug-safety-and-availability/drug-safety-communications",
                         snapshot_date="", source_file=str(f.relative_to(ROOT)),
                         notes="two_digit_year_in_source" if re.search(r"\d-\d{1,2}-\d{2}$", norm(tds[0].get_text())) else ""))
    # wayback list
    f = RAW / "s3_dsc_index_wayback_20200105.html"
    meta = json.load(open(str(f) + ".meta.json"))
    s = BeautifulSoup(f.read_text(encoding="utf-8", errors="replace"), "html.parser")
    h2 = s.find("h2", string=re.compile("Current Drug Safety Communications"))
    for li in h2.find_next("ul").find_all("li"):
        a = li.find("a", href=True)
        if not a: continue
        d = li.get_text(" ").replace(a.get_text(" "), "")
        href = urljoin("https://web.archive.org", a["href"])
        rows.append(dict(dsc_date_raw=norm(d), dsc_date=iso(d), title_raw=norm(a.get_text(" ")), dsc_url=href,
                         source_url=meta["final_url"], snapshot_date=snap(meta["final_url"]),
                         source_file=str(f.relative_to(ROOT)), notes="" if iso(d) else "date_missing"))
    # legacy (Dec 2018 snapshot): <li><a><linktitle>title</linktitle></a><br/>date</li>, sections Current / Previous
    f = RAW / "s3_dsc_index_wayback_legacy_2018.html"
    meta = json.load(open(str(f) + ".meta.json"))
    s = BeautifulSoup(f.read_text(encoding="utf-8", errors="replace"), "html.parser")
    for h2 in s.find_all("h2"):
        sec = norm(h2.get_text())
        if sec not in ("Current Drug Safety Communications", "Previous Drug Safety Communications"): continue
        box = h2.find_parent("div", class_="panel")
        for li in box.find_all("li"):
            a = li.find("a", href=True)
            if not a: continue
            title = norm(a.get_text(" "))
            if re.match(r"^20\d\d Drug Safety Communications$", title): continue   # per-year index link, not a DSC
            d = li.get_text(" ").replace(a.get_text(" "), "")
            rows.append(dict(dsc_date_raw=norm(d), dsc_date=iso(d), title_raw=title, dsc_url=urljoin("https://web.archive.org", a["href"]),
                             source_url=meta["final_url"], snapshot_date=snap(meta["final_url"]), source_file=str(f.relative_to(ROOT)),
                             notes=("section=" + sec.split()[0]) + ("" if iso(d) else "; date_missing")))
    # per-year archive pages (2016-2018), same <li> layout
    for yr in (2016, 2017, 2018):
        f = RAW / f"s3_dsc_year_{yr}.html"
        meta = json.load(open(str(f) + ".meta.json"))
        s = BeautifulSoup(f.read_text(encoding="utf-8", errors="replace"), "html.parser")
        for li in s.select("div.panel-body li"):
            a = li.find("a", href=True)
            if not a or not li.find("linktitle"): continue
            d = li.get_text(" ").replace(a.get_text(" "), "")
            rows.append(dict(dsc_date_raw=norm(d), dsc_date=iso(d), title_raw=norm(a.get_text(" ")), dsc_url=urljoin("https://web.archive.org", a["href"]),
                             source_url=meta["final_url"], snapshot_date=snap(meta["final_url"]), source_file=str(f.relative_to(ROOT)),
                             notes=f"year_page={yr}" + ("" if iso(d) else "; date_missing")))
    df = pd.DataFrame(rows)
    df["page_file"] = df.dsc_url.map(lambda u: f"data/raw/labels/s3_dsc_{hashlib.md5(u.encode()).hexdigest()[:10]}.html")
    return df


def main():
    df = parse_index()
    dup = df.duplicated(["dsc_date", "title_raw"], keep=False)
    df.loc[dup, "notes"] = (df.loc[dup, "notes"] + "; in_both_indexes").str.strip("; ")
    df.to_csv(OUT / "s3_dsc_index.csv", index=False)
    print(len(df), df.dsc_date.min(), df.dsc_date.max(), "rows 2016+:", (df.dsc_date >= "2016-01-01").sum(), flush=True)
    if "--index-only" in sys.argv: return
    sess = requests.Session()
    sub = df[(df.dsc_date >= "2016-01-01") & ~df.duplicated(["dsc_date", "title_raw"])]
    for _, r in sub.iterrows():
        try:
            fetch(r.dsc_url, ROOT / r.page_file, sess=sess)
            print("ok", r.dsc_date, r.dsc_url[-70:], flush=True)
        except RobotsDisallowed as e:
            print("ROBOTS", e, flush=True)
        except Exception as e:
            print("ERR", r.dsc_url, str(e)[:100], flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
