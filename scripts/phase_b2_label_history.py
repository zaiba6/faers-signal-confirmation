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


def setid_to_apps():
    """set id -> the application numbers it is registered under."""
    out = {}
    for f in (RAW.parent / "spls_by_application").glob("*.json"):
        for r in json.loads(f.read_text()):
            out.setdefault(r["setid"], set()).add(f.stem)
    return out


def step_size():
    hist = load_histories()
    sid2app = setid_to_apps()
    import duckdb
    con = duckdb.connect()
    pairs = con.execute(f"""select drug_ai, detection_quarter, count(*) n
        from read_parquet('{ROOT}/data/processed/confirmation_target_T1.parquet')
        group by 1,2""").df()
    m = pd.read_csv(ROOT / "results/phase_b1/b1_drug_to_setid_clean.csv")
    d2s = {r.drug_ai: [x for x in str(r.setids).split(";") if x] for r in m.itertuples()}

    # One PRIMARY label per drug, not every set id. Checking all of them would need
    # 118,491 version downloads (33 hours at one request per second) to answer a yes/no
    # question that one label already answers: whether the event was in the product's
    # labelling before detection. The primary is the set id with the longest version
    # history, tie-broken by the earliest first version, which is the originator's own
    # label rather than a repackager's short-lived copy. Recorded as decision 80, with
    # the sensitivity check in decision 81.
    def primary(sids):
        cand = [(len(hist[s]), -hist[s][-1][0].value, s) for s in sids if hist.get(s)]
        return max(cand)[2] if cand else None

    prim = {d: primary(s) for d, s in d2s.items()}
    need, no_base, unmapped = set(), 0, 0
    for r in pairs.itertuples():
        s = prim.get(r.drug_ai)
        if s is None:
            unmapped += r.n          # drug is not in the clean mapping at all (decision 73)
            continue
        cut = q_end(r.detection_quarter)
        before = [(dt, v) for dt, v in hist[s] if dt <= cut]
        if not before:
            # The primary has no version predating detection. Rather than lose the pair to
            # decision 77, fall back to any other set id of the same drug that does. This
            # recovers 16,228 T1 pairs for 189 extra downloads, and it matters because the
            # loss is time-trended: without the fallback 18.6% of 2016 detections have no
            # baseline against 6.4% of 2023, which would thin the train period more than
            # the test period. With it, 11.3% against 4.7%. Chosen on coverage and
            # temporal balance, both independent of the outcome.
            # Restricted to set ids under the SAME application number as the primary.
            # A different application number is a different product registration, often a
            # different sponsor or an authorized generic on its own update schedule, and
            # taking the baseline from one registration while the confirmation comes from
            # another can make an event look newly added when the other label carried it
            # all along. Same principle as decisions 65 and 70, applied to the fallback.
            # Cost 4,109 T1 pairs; the no-baseline trend goes 11.3-4.7 to 13.6-4.8,
            # against 18.6-6.4 with no fallback at all.
            papps = sid2app.get(s, set())
            alt = [x for x in d2s[r.drug_ai]
                   if hist.get(x) and any(dt <= cut for dt, _ in hist[x])
                   and (sid2app.get(x, set()) & papps)]
            if alt:
                s = max(alt, key=lambda x: len(hist[x]))
                before = [(dt, v) for dt, v in hist[s] if dt <= cut]
        if before:
            need.add((s, max(before, key=lambda x: x[0])[1]))
        else:
            no_base += r.n
    rep = dict(histories_cached=len(hist),
               drug_quarter_combos=len(pairs),
               version_downloads_needed=len(need),
               pairs_with_no_baseline_version=int(no_base),
               pairs_drug_not_in_clean_mapping=unmapped,
               primary_setids=len({v for v in prim.values() if v}),
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


# ---------------------------------------------------------------- step 4: the baseline rule
# event_match takes a section_code and treats CONTRA specially: a PT inside a
# contraindication describes who must not take the drug, not an effect it caused.
SECTION_CODE = {"contraindications": "CONTRA"}


def baseline_for(drug, cut, hist, d2s, prim, sid2app):
    """(setid, version) of the label in force at `cut`, or None. Decisions 80, 89, 91."""
    s = prim.get(drug)
    if s is None:
        return None
    before = [(dt, v) for dt, v in hist.get(s, []) if dt <= cut]
    if not before:
        papps = sid2app.get(s, set())
        alt = [x for x in d2s[drug]
               if hist.get(x) and any(dt <= cut for dt, _ in hist[x])
               and (sid2app.get(x, set()) & papps)]
        if not alt:
            return None
        s = max(alt, key=lambda x: len(hist[x]))
        before = [(dt, v) for dt, v in hist[s] if dt <= cut]
    return s, max(before, key=lambda x: x[0])[1]


def step_rule(tag="T1"):
    """Was the event already in a counted section of the label in force at detection?"""
    import pandas as pd, duckdb
    sys.path.insert(0, str(Path(__file__).parent))
    from event_match import event_match, pt_regex

    hist, sid2app = load_histories(), setid_to_apps()
    m = pd.read_csv(ROOT / "results/phase_b1/b1_drug_to_setid_clean.csv")
    d2s = {r.drug_ai: [x for x in str(r.setids).split(";") if x] for r in m.itertuples()}

    def primary(sids):
        cand = [(len(hist[s]), -hist[s][-1][0].value, s) for s in sids if hist.get(s)]
        return max(cand)[2] if cand else None
    prim = {d: primary(s) for d, s in d2s.items()}

    con = duckdb.connect()
    pairs = con.execute(f"""select distinct drug_ai, pt_clean, detection_quarter
        from read_parquet('{ROOT}/data/processed/detected_pairs_{tag}.parquet')
        where status = 'in_window'""").df()

    rxc, seccache, out = {}, {}, []
    for r in pairs.itertuples():
        b = baseline_for(r.drug_ai, q_end(r.detection_quarter), hist, d2s, prim, sid2app)
        if b is None:
            out.append((r.drug_ai, r.pt_clean, r.detection_quarter, None, None, None, None))
            continue
        sid, ver = b
        if (sid, ver) not in seccache:
            f = SEC / f"{sid}_{ver}.json"
            seccache[(sid, ver)] = json.loads(f.read_text()) if f.exists() else None
        secs = seccache[(sid, ver)]
        # secs is None   -> the version text could not be fetched or parsed
        # secs is empty  -> an old pre-PLR label: everything sits in SPL UNCLASSIFIED
        #                   SECTION (42229-5) with no structured Warnings, Adverse
        #                   Reactions or Contraindications codes at all. We cannot tell
        #                   which section any text belongs to, so we cannot apply
        #                   decision 75. Scoring these 0 would say "not already labelled"
        #                   when the warning may well be there unclassified, handing the
        #                   pair a free pass into the cohort as a possible false Y=1.
        #                   Both are "cannot check", recorded as null, and both leave the
        #                   cohort at the baseline_text_unavailable step.
        if not secs:
            out.append((r.drug_ai, r.pt_clean, r.detection_quarter, sid, ver, None,
                        "no_counted_sections" if secs == {} else None))
            continue
        if r.pt_clean not in rxc:
            rxc[r.pt_clean] = pt_regex(r.pt_clean)
        hit, where = 0, None
        for name, text in secs.items():
            ok, _ = event_match(text, r.pt_clean, rxc[r.pt_clean], SECTION_CODE.get(name, ""))
            if ok:
                hit, where = 1, name
                break
        out.append((r.drug_ai, r.pt_clean, r.detection_quarter, sid, ver, hit, where))

    df = pd.DataFrame(out, columns=["drug_ai", "pt_clean", "detection_quarter",
                                    "baseline_setid", "baseline_version",
                                    "already_labelled", "baseline_section"])
    con.register("df", df)
    o = ROOT / f"data/processed/baseline_label_{tag}.parquet"
    con.execute(f"COPY df TO '{o}' (FORMAT PARQUET)")
    n = len(df)
    print(f"pairs {n:,}")
    print(f"  no baseline version      {df.baseline_setid.isna().sum():,}")
    print(f"  baseline text missing    {(df.baseline_setid.notna() & df.already_labelled.isna()).sum():,}")
    print(f"  already labelled         {int(df.already_labelled.fillna(0).sum()):,}")
    print(df[df.already_labelled == 1].baseline_section.value_counts().to_string())


if __name__ == "__main__" and STEP == "rule":
    for _tag in ("T1", "T2", "T3"):
        print(f"=== {_tag} ===", flush=True)
        step_rule(_tag)
