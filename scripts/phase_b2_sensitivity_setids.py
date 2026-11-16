"""Decision 81: is the one-primary-label shortcut (decisions 80, 89, 91) costing us anything?

Run: /usr/local/bin/python3 scripts/phase_b2_sensitivity_setids.py [--size]
Out: results/phase_b2/b2_sensitivity_setids.json
     results/phase_b2/b2_sensitivity_disagreements.csv   one row per disagreeing pair

The rule reads the baseline from ONE label per drug. A drug can have many mapped set ids, and the
shortcut is only safe if the chosen one is not thinner than its siblings: if an event sits in a
sibling's Warnings but not in the primary's, the rule reads "not already labelled", the pair stays
in the cohort, and it becomes a candidate false positive - exactly the error the baseline rule was
built to remove.

Decision 81 fixed the protocol before any of this was measured: 100 random drugs with more than one
mapped set id, re-checked against ALL their set ids, disagreement reported, and the shortcut
abandoned for a full crawl if disagreement exceeds 10%. SEED and threshold are below and are the
ones pre-registered.

Two arms are reported, because decision 91 narrowed the fallback after decision 81 was written:

  all_setids    every set id mapped to the drug. The literal decision 81 check, and the
                conservative one - it will flag disagreements the live rule could never act on.
  same_app      only set ids sharing an application number with the primary. This is the set the
                live rule is actually allowed to draw a baseline from, so it is the operationally
                relevant number.

The 10% threshold is applied to `all_setids`, as pre-registered, and `same_app` is reported
alongside rather than substituted for it.

Disagreement is directional and both directions are counted separately:
  missed_by_primary   primary says 0, some sibling says 1  -> the pair wrongly stayed in the cohort
  extra_by_primary    primary says 1, every sibling says 0 -> the pair was wrongly excluded
Only `missed_by_primary` threatens the headline, but hiding the other direction would make the
shortcut look better than it is.
"""
import sys, json, io, time, zipfile
from pathlib import Path
import pandas as pd, duckdb, requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import phase_b2_label_history as B
from event_match import event_match, pt_regex

SEED, N_DRUGS, THRESHOLD_PCT = 81, 100, 10.0      # pre-registered in decision 81
OUT = ROOT / "results/phase_b2"
SIZE_ONLY = "--size" in sys.argv
_last = [0.0]


def fetch(setid, version):
    """Cache one version's counted sections. Same endpoint, delay and parser as step_text."""
    f = B.SEC / f"{setid}_{version}.json"
    if f.exists():
        return True
    w = B.DELAY - (time.monotonic() - _last[0])
    if w > 0:
        time.sleep(w)
    try:
        resp = requests.get(B.DL + "getFile.cfm", headers=B.UA, timeout=120,
                            params=dict(setid=setid, type="zip", version=int(version)))
        _last[0] = time.monotonic()
        resp.raise_for_status()
        z = zipfile.ZipFile(io.BytesIO(resp.content))
        xmls = [n for n in z.namelist() if n.lower().endswith(".xml")]
        if not xmls:
            raise ValueError("no xml in zip")
        f.write_text(json.dumps(B.counted_text(z.read(xmls[0]))))
        return True
    except Exception as e:
        _last[0] = time.monotonic()
        print(f"  ERR {setid} v{version}: {type(e).__name__}", flush=True)
        return False


hist, sid2app = B.load_histories(), B.setid_to_apps()
m = pd.read_csv(ROOT / "results/phase_b1/b1_drug_to_setid_clean.csv")
d2s = {r.drug_ai: [x for x in str(r.setids).split(";") if x] for r in m.itertuples()}
multi = {d: s for d, s in d2s.items() if len(s) > 1}

bl = duckdb.connect().execute(
    f"select * from read_parquet('{ROOT}/data/processed/baseline_label_T1.parquet')").df()
# Only pairs the rule could actually answer. A null already_labelled means the baseline text was
# unavailable, and those rows leave the cohort anyway; asking whether a sibling would have answered
# is a different question (coverage, not bias) and is not what decision 81 pre-registered.
pool = bl[bl.drug_ai.isin(multi) & bl.already_labelled.notna()]
drugs = sorted(pool.drug_ai.unique())
sample = pd.Series(drugs).sample(N_DRUGS, random_state=SEED).tolist()
sub = pool[pool.drug_ai.isin(sample)].copy()

# Which (setid, version) texts the check needs: for every sampled drug-quarter, the version of
# EVERY mapped set id that was in force at the cut.
need, inforce = set(), {}
for d, q in sub[["drug_ai", "detection_quarter"]].drop_duplicates().itertuples(index=False):
    cut = B.q_end(q)
    for s in d2s[d]:
        before = [(dt, v) for dt, v in hist.get(s, []) if dt <= cut]
        if before:
            v = max(before, key=lambda x: x[0])[1]
            inforce[(d, q, s)] = v
            need.add((s, v))
missing = [x for x in sorted(need) if not (B.SEC / f"{x[0]}_{x[1]}.json").exists()]
print(f"{len(sample)} drugs, {len(sub):,} pairs, {len(need):,} version texts "
      f"({len(missing):,} to download, ~{len(missing)/60:.0f} min)", flush=True)
if SIZE_ONLY:
    raise SystemExit(0)

for i, (s, v) in enumerate(missing, 1):
    fetch(s, v)
    if i % 100 == 0:
        print(f"  {i}/{len(missing)}", flush=True)

# ---------------------------------------------------------------- re-check
secs_cache, rxc = {}, {}


def sections(setid, version):
    k = (setid, version)
    if k not in secs_cache:
        f = B.SEC / f"{setid}_{version}.json"
        secs_cache[k] = json.loads(f.read_text()) if f.exists() else None
    return secs_cache[k]


def labelled(setid, version, pt):
    """1 / 0 / None, with None meaning the same 'cannot check' as in step_rule."""
    secs = sections(setid, version)
    if not secs:
        return None
    if pt not in rxc:
        rxc[pt] = pt_regex(pt)
    for name, text in secs.items():
        ok, _ = event_match(text, pt, rxc[pt], B.SECTION_CODE.get(name, ""))
        if ok:
            return 1
    return 0


def primary_of(d):
    cand = [(len(hist[s]), -hist[s][-1][0].value, s) for s in d2s[d] if hist.get(s)]
    return max(cand)[2] if cand else None


prim = {d: primary_of(d) for d in sample}
rows, arms = [], {a: dict(checked=0, missed=0, extra=0) for a in ("all_setids", "same_app")}

for r in sub.itertuples():
    papps = sid2app.get(prim.get(r.drug_ai), set())
    sibs = {}
    for s in d2s[r.drug_ai]:
        if s == r.baseline_setid:
            continue
        v = inforce.get((r.drug_ai, r.detection_quarter, s))
        if v is None:
            continue
        ans = labelled(s, v, r.pt_clean)
        if ans is not None:
            sibs[s] = (v, ans)
    if not sibs:
        continue
    same = {s: x for s, x in sibs.items() if sid2app.get(s, set()) & papps}
    for arm, group in (("all_setids", sibs), ("same_app", same)):
        if not group:
            continue
        arms[arm]["checked"] += 1
        any_sib = max(x[1] for x in group.values())
        if r.already_labelled == 0 and any_sib == 1:
            arms[arm]["missed"] += 1
        elif r.already_labelled == 1 and any_sib == 0:
            arms[arm]["extra"] += 1
    if not same:
        continue
    any_same = max(x[1] for x in same.values())
    if int(r.already_labelled) != any_same:
        who = [s for s, x in same.items() if x[1] != r.already_labelled]
        rows.append(dict(drug_ai=r.drug_ai, pt_clean=r.pt_clean,
                         detection_quarter=r.detection_quarter,
                         primary_setid=r.baseline_setid, primary_version=r.baseline_version,
                         primary_says=int(r.already_labelled), siblings_say=any_same,
                         direction="missed_by_primary" if r.already_labelled == 0 else "extra_by_primary",
                         disagreeing_setids=";".join(who[:5]), n_siblings_checked=len(same)))

res = dict(decision="81", seed=SEED, n_drugs_requested=N_DRUGS,
           n_drugs_sampled=len(sample), threshold_pct=THRESHOLD_PCT,
           threshold_applies_to="all_setids",
           drugs_with_multiple_setids=len(multi), pairs_in_sample=len(sub),
           version_texts_used=len(need), version_texts_downloaded_now=len(missing), arms={})
for a, c in arms.items():
    n = c["checked"]
    res["arms"][a] = dict(
        pairs_with_a_comparable_sibling=n,
        missed_by_primary=c["missed"], extra_by_primary=c["extra"],
        disagreement_pct=round(100 * (c["missed"] + c["extra"]) / n, 2) if n else None,
        missed_pct=round(100 * c["missed"] / n, 2) if n else None)
pct = res["arms"]["all_setids"]["disagreement_pct"]
res["verdict"] = ("shortcut KEPT" if pct is not None and pct <= THRESHOLD_PCT
                  else "shortcut ABANDONED: run the full crawl")
res["verdict_basis"] = f"all_setids disagreement {pct}% against a {THRESHOLD_PCT}% threshold"

pd.DataFrame(rows).to_csv(OUT / "b2_sensitivity_disagreements.csv", index=False)
(OUT / "b2_sensitivity_setids.json").write_text(json.dumps(res, indent=1))
print(json.dumps(res, indent=1))
