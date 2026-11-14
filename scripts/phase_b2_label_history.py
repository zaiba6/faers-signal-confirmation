"""Phase B2 step 1: fetch DailyMed version history for every set id in the CLEAN mapping.

Run: /usr/local/bin/python3 scripts/phase_b2_label_history.py --step history

History only, no version downloads. One request per set id, cached one JSON per set id, so
a rerun resumes. The histories tell us which versions exist and when each took effect,
which is what sizes the (much larger) version-text download that follows.

Why the clean mapping only: decision 71. A flagged level points at a different product, so
its history would answer "was this already listed" from the wrong label.
"""
import sys, json, re, time
from pathlib import Path
import requests, pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/dailymed/history"; RAW.mkdir(parents=True, exist_ok=True)
OUT = ROOT / "results/phase_b2"; OUT.mkdir(parents=True, exist_ok=True)
B = "https://dailymed.nlm.nih.gov/dailymed/services/v2/"
UA = {"User-Agent": "faers-signal-confirmation-research/0.1 (academic research; contact via repo)"}
DELAY = 1.0
_last = [0.0]


def get(path, **params):
    w = DELAY - (time.monotonic() - _last[0])
    if w > 0:
        time.sleep(w)
    try:
        r = requests.get(B + path, params=params, headers=UA, timeout=60)
    finally:
        _last[0] = time.monotonic()
    r.raise_for_status()
    return r.json()


def setids():
    m = pd.read_csv(ROOT / "results/phase_b1/b1_drug_to_setid_clean.csv")
    ids = set()
    for s in m.setids.dropna():
        ids.update(x for x in str(s).split(";") if x)
    return sorted(ids)


def step_history(ids):
    err = 0
    for i, sid in enumerate(ids, 1):
        f = RAW / f"{sid}.json"
        if f.exists():
            continue
        try:
            j = get(f"spls/{sid}/history.json")
        except Exception as e:
            print(f"  ERR {sid}: {type(e).__name__}", flush=True)
            err += 1
            continue
        f.write_text(json.dumps(j.get("data", {})))
        if i % 200 == 0:
            print(f"  {i}/{len(ids)} set ids ({err} errors)", flush=True)
    print(f"done: {len(list(RAW.glob('*.json')))}/{len(ids)} cached, {err} errors this pass", flush=True)


STEP = sys.argv[sys.argv.index("--step") + 1] if "--step" in sys.argv else "history"

if __name__ == "__main__" and STEP == "history":
    ids = setids()
    print(f"clean mapping covers {len(ids):,} distinct set ids", flush=True)
    step_history(ids)


# ---------------------------------------------------------------- step 2: sizing
# Decision 75: only these sections count. A mention in Indications, Dosage or Clinical
# Pharmacology is not the label warning about the event, and is the likely route by which
# a term like PAIN entered as a positive.
COUNTED_LOINC = {
    "34066-1": "boxed_warning",
    "43685-7": "warnings_and_precautions",
    "34071-1": "warnings",
    "42232-9": "precautions",
    "34070-3": "contraindications",
    "34084-4": "adverse_reactions",
}


def load_histories():
    """setid -> list of (date, version), newest first, one row per version."""
    out = {}
    for f in RAW.glob("*.json"):
        d = json.loads(f.read_text())
        seen, rows = set(), []
        for h in d.get("history", []):
            v = h.get("spl_version")
            if v in seen:
                continue            # the feed repeats a version when it is republished;
            seen.add(v)             # keep the earliest-listed row, which is its own date
            rows.append((pd.to_datetime(h["published_date"], format="mixed"), int(v)))
        out[f.stem] = sorted(rows, key=lambda r: r[0], reverse=True)
    return out


def q_end(q):
    """'2019Q3' -> the last day of that quarter."""
    y, n = int(q[:4]), int(q[-1])
    return pd.Timestamp(y, n * 3, 1) + pd.offsets.MonthEnd(0)


def step_size():
    hist = load_histories()
    import duckdb
    con = duckdb.connect()
    pairs = con.execute(f"""select drug_ai, detection_quarter, count(*) n
        from read_parquet('{ROOT}/data/processed/confirmation_target_T1.parquet')
        group by 1,2""").df()
    m = pd.read_csv(ROOT / "results/phase_b1/b1_drug_to_setid_clean.csv")
    d2s = {r.drug_ai: [x for x in str(r.setids).split(";") if x] for r in m.itertuples()}

    need, no_base, no_hist = set(), 0, 0
    for r in pairs.itertuples():
        sids = d2s.get(r.drug_ai)
        if not sids:
            continue
        cut = q_end(r.detection_quarter)
        got = False
        for s in sids:
            h = hist.get(s)
            if not h:
                no_hist += 1
                continue
            before = [(dt, v) for dt, v in h if dt <= cut]
            if before:
                need.add((s, max(before, key=lambda x: x[0])[1]))
                got = True
        if not got:
            no_base += r.n
    rep = dict(histories_cached=len(hist),
               drug_quarter_combos=len(pairs),
               version_downloads_needed=len(need),
               pairs_with_no_baseline_version=int(no_base),
               setids_with_no_history=no_hist,
               est_hours_at_1_per_sec=round(len(need) / 3600, 1))
    (OUT / "b2_sizing.json").write_text(json.dumps(rep, indent=1))
    pd.DataFrame(sorted(need), columns=["setid", "version"]).to_csv(OUT / "b2_versions_needed.csv", index=False)
    print(json.dumps(rep, indent=1))


if __name__ == "__main__" and STEP == "size":
    step_size()


# ---------------------------------------------------------------- step 3: version text
SEC = ROOT / "data/raw/dailymed/sections"; SEC.mkdir(parents=True, exist_ok=True)
DL = "https://dailymed.nlm.nih.gov/dailymed/"


def counted_text(xml_bytes):
    """Plain text of the counted sections only (decision 75), keyed by section name.

    Nested children are kept: Postmarketing Experience sits inside Adverse Reactions, and
    decision 75 counts it.
    """
    import xml.etree.ElementTree as ET
    ns = {"h": "urn:hl7-org:v3"}
    root = ET.fromstring(xml_bytes)
    out = {}
    for sec in root.iter("{urn:hl7-org:v3}section"):
        c = sec.find("h:code", ns)
        name = COUNTED_LOINC.get(c.get("code")) if c is not None else None
        if not name:
            continue
        txt = " ".join(t.strip() for t in sec.itertext() if t and t.strip())
        out[name] = (out.get(name, "") + " " + txt).strip()
    return out


def step_text():
    import io, zipfile
    need = pd.read_csv(OUT / "b2_versions_needed.csv")
    err = 0
    for i, r in enumerate(need.itertuples(), 1):
        f = SEC / f"{r.setid}_{r.version}.json"
        if f.exists():
            continue
        w = DELAY - (time.monotonic() - _last[0])
        if w > 0:
            time.sleep(w)
        try:
            resp = requests.get(DL + "getFile.cfm", headers=UA, timeout=120,
                                params=dict(setid=r.setid, type="zip", version=int(r.version)))
            _last[0] = time.monotonic()
            resp.raise_for_status()
            z = zipfile.ZipFile(io.BytesIO(resp.content))
            xmls = [n for n in z.namelist() if n.lower().endswith(".xml")]
            if not xmls:
                raise ValueError("no xml in zip")
            f.write_text(json.dumps(counted_text(z.read(xmls[0]))))
        except Exception as e:
            _last[0] = time.monotonic()
            err += 1
            print(f"  ERR {r.setid} v{r.version}: {type(e).__name__}", flush=True)
        if i % 200 == 0:
            print(f"  {i}/{len(need)} versions ({err} errors)", flush=True)
    print(f"done: {len(list(SEC.glob('*.json')))} cached, {err} errors this pass", flush=True)


if __name__ == "__main__" and STEP == "text":
    step_text()
