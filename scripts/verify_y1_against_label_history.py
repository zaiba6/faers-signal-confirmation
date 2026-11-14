"""For each Y=1 row, was the event ABSENT from the label before detection and present after?

Run: /usr/local/bin/python3 scripts/verify_y1_against_label_history.py
Out: results/y1_label_history_check.json

For each pair it fetches the DailyMed SPL version in force at the detection quarter end and the
version in force after the confirming change, and looks for the PT in each. Absent-then-present is
a true confirmation. Present-in-both means the event was already labelled, so the "confirmation"
was a revision of existing text, not a new risk.
"""
import json, re, io, time, zipfile, sys
from pathlib import Path
import requests, duckdb, pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results"; OUT.mkdir(exist_ok=True)
B = "https://dailymed.nlm.nih.gov/dailymed/"
UA = {"User-Agent": "faers-signal-confirmation-research/0.1 (academic research)"}
DELAY = 1.0

PAIRS = [  # (faers drug, PT, brand to search, detection quarter)
    ("SOFOSBUVIR\\VELPATASVIR", "HEADACHE", "EPCLUSA", "2016Q4"),
    ("ELBASVIR\\GRAZOPREVIR", "ASCITES", "ZEPATIER", "2017Q4"),
    ("SELINEXOR", "NEUTROPENIA", "XPOVIO", "2019Q3"),
    ("URSODIOL", "DYSPEPSIA", "URSODIOL", "2021Q4"),
]


def get(path, **params):
    time.sleep(DELAY)
    return requests.get(B + path, params=params, headers=UA, timeout=90)


def qend(q):
    return pd.Timestamp(int(q[:4]), int(q[-1]) * 3, 1) + pd.offsets.MonthEnd(0)


def label_text(setid, version):
    r = get("getFile.cfm", setid=setid, type="zip", version=version)
    if not r.ok or r.content[:2] != b"PK":
        return None, None
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml = [n for n in zf.namelist() if n.endswith(".xml")]
    if not xml:
        return None, None
    body = zf.read(xml[0]).decode("utf-8", "replace")
    eff = re.search(r'<effectiveTime value="(\d+)"', body)
    txt = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", body))
    return (eff.group(1) if eff else None), txt


con = duckdb.connect()
out = []
for drug, pt, brand, dq in PAIRS:
    row = con.execute(f"""select first_confirm_date, a from read_parquet(
        '{ROOT}/data/processed/confirmation_target_T1.parquet')
        where drug_ai = ? and pt_clean = ?""", [drug, pt]).df()
    confirm = pd.to_datetime(row.first_confirm_date[0]) if len(row) else None
    det_end = qend(dq)
    rec = dict(drug=drug, pt=pt, detection_quarter=dq, detection_end=str(det_end.date()),
               confirm_date=str(confirm.date()) if confirm is not None else None)
    try:
        j = get("services/v2/spls.json", drug_name=brand, pagesize=20).json()
        sets = [d["setid"] for d in j.get("data", [])]
        best, hist = None, []
        for sid in sets[:4]:
            h = get(f"services/v2/spls/{sid}/history.json").json().get("data", {}).get("history", [])
            if len(h) > len(hist):
                best, hist = sid, h
        rec["setid"], rec["versions"] = best, len(hist)
        if not hist:
            rec["error"] = "no history"; out.append(rec); continue
        H = pd.DataFrame(hist)
        H["pub"] = pd.to_datetime(H.published_date, format="mixed")
        before = H[H.pub <= det_end].sort_values("pub").tail(1)
        after = H[H.pub >= confirm].sort_values("pub").head(1) if confirm is not None else H.head(1)
        rx = re.compile(r"\b" + re.escape(pt.lower().rstrip("s")) + r"s?\b", re.I)
        for tag, sel in (("before", before), ("after", after)):
            if len(sel) == 0:
                rec[tag] = None; continue
            v = int(sel.spl_version.iloc[0])
            eff, txt = label_text(best, v)
            rec[tag] = dict(version=v, published=str(sel.pub.iloc[0].date()), effective_time=eff,
                            pt_found=bool(rx.search(txt)) if txt else None,
                            occurrences=len(rx.findall(txt)) if txt else None)
        b, a = rec.get("before"), rec.get("after")
        if b and a and b["pt_found"] is not None:
            rec["verdict"] = ("TRUE ADDITION - absent before, present after" if (not b["pt_found"] and a["pt_found"])
                              else "ALREADY LABELLED before detection" if b["pt_found"]
                              else "absent in both - matched text is not in this label")
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
    out.append(rec)
    print(json.dumps(rec, indent=1, default=str), flush=True)

(OUT / "y1_label_history_check.json").write_text(json.dumps(out, indent=1, default=str))
print("\n=== VERDICTS ===")
for r in out:
    print(f"  {r['drug'][:28]:30} {r['pt']:14} {r.get('verdict') or r.get('error')}")
