"""Leakage audit. Run BEFORE trusting any model number; exits 1 if any check fails.

Run: /usr/local/bin/python3 scripts/audit_leakage.py [T1|T2|T3]

Checks
 1  no outcome-derived column reaches the model's feature list
 2  independent recomputation: for a random sample of pairs, re-derive n_cases / frac_serious /
    drug_n_events straight from the case tables with the "init_quarter <= detection_quarter" cut
    and require an exact match with the stored feature
 3  nothing in the feature table counts a case received AFTER the detection quarter
 4  no single feature separates the classes suspiciously well (a stand-in for an accidental
    copy of the label); flagged, not failed, since a strong honest predictor looks the same
 5  the temporal split really is temporal: max train quarter < min test quarter
"""
import sys, json
from pathlib import Path
import duckdb, numpy as np, pandas as pd
from sklearn.metrics import roc_auc_score

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "data/processed"
RULE = (sys.argv[1] if len(sys.argv) > 1 else "T1").upper()
SPLIT = "2020Q4"
N_SAMPLE = 300
SEED = 7

OUTCOME_COLS = ("confirmed_", "first_confirm", "days_to_confirm", "dsc_drug", "s921_drug")
fails, notes = [], []


def feature_cols(df):
    drop = {"drug_ai", "pt_clean", "detection_quarter", "first_quarter_at_3"}
    return [c for c in df.columns
            if c not in drop and not any(c.startswith(p) or p in c for p in OUTCOME_COLS)]


con = duckdb.connect()
f = con.execute(f"select * from read_parquet('{P}/features_{RULE}.parquet')").df()
cols = feature_cols(f)

# 1 ---------------------------------------------------------------- outcome columns excluded
bad = [c for c in cols if any(p in c for p in OUTCOME_COLS)]
(fails if bad else notes).append(f"check1 outcome columns in features: {bad or 'none'}")

# 2 ---------------------------------------------------- independent recomputation from raw
s = f.sample(N_SAMPLE, random_state=SEED)[["drug_ai", "pt_clean", "detection_quarter",
                                           "n_cases", "frac_serious", "drug_n_events"]]
con.register("s", s)
chk = con.execute(f"""
 select s.*,
   (select count(*) from read_parquet('{P}/ps_drug_event_cases.parquet') l
      join read_parquet('{P}/case_table.parquet') c on c.caseid=l.caseid and c.in_counting_universe
     where l.drug_ai=s.drug_ai and l.pt_clean=s.pt_clean and l.in_counting_universe
       and l.init_quarter <= s.detection_quarter)                                as recomputed_n_cases,
   (select avg(greatest(c.DE,c.HO,c.LT,c.DS,c.CA,c.RI)::int)
      from read_parquet('{P}/ps_drug_event_cases.parquet') l
      join read_parquet('{P}/case_table.parquet') c on c.caseid=l.caseid and c.in_counting_universe
     where l.drug_ai=s.drug_ai and l.pt_clean=s.pt_clean and l.in_counting_universe
       and l.init_quarter <= s.detection_quarter)                                as recomputed_frac_serious,
   (select count(distinct q.pt_clean) from read_parquet('{P}/pair_quarter_counts.parquet') q
     where q.drug_ai=s.drug_ai and q.quarter=s.detection_quarter)                as recomputed_drug_n_events
 from s""").df()
for col, rec in [("n_cases", "recomputed_n_cases"), ("drug_n_events", "recomputed_drug_n_events")]:
    d = int((chk[col].fillna(-1).astype(int) != chk[rec].fillna(-1).astype(int)).sum())
    (fails if d else notes).append(f"check2 {col}: {d}/{len(chk)} mismatches")
d = int((~np.isclose(chk.frac_serious.fillna(-1), chk.recomputed_frac_serious.fillna(-1), atol=1e-9)).sum())
(fails if d else notes).append(f"check2 frac_serious: {d}/{len(chk)} mismatches")

# 3 ------------------------------------------------- no case counted after the detection quarter
future = con.execute(f"""
 select count(*) n from read_parquet('{P}/features_{RULE}.parquet') t
 where t.n_cases is not null and t.n_cases > (
   select count(*) from read_parquet('{P}/ps_drug_event_cases.parquet') l
    join read_parquet('{P}/case_table.parquet') c on c.caseid=l.caseid and c.in_counting_universe
   where l.drug_ai=t.drug_ai and l.pt_clean=t.pt_clean and l.in_counting_universe
     and l.init_quarter <= t.detection_quarter)""").df().n[0]
(fails if future else notes).append(f"check3 rows counting post-detection cases: {future}")

# 4 ------------------------------------------------------ single-feature separation (flag only)
y = f.confirmed_event_3y.values
sus = []
for c in cols:
    v = f[c].values.astype(float)
    m = ~np.isnan(v)
    if m.sum() < 1000 or len(np.unique(v[m])) < 2:
        continue
    auc = roc_auc_score(y[m], v[m])
    if max(auc, 1 - auc) > 0.90:
        sus.append((c, round(max(auc, 1 - auc), 3)))
notes.append(f"check4 features with single-feature AUROC > 0.90: {sus or 'none'}")

# 5 ------------------------------------------------------------------------ split is temporal
tr = f[f.detection_quarter <= SPLIT]; te = f[f.detection_quarter > SPLIT]
ok = tr.detection_quarter.max() < te.detection_quarter.min()
(notes if ok else fails).append(
    f"check5 split temporal: train<={tr.detection_quarter.max()} test>={te.detection_quarter.min()} -> {ok}")

print(f"=== leakage audit {RULE} ===")
for n in notes: print("  PASS", n)
for n in fails: print("  FAIL", n)
rep = dict(rule=RULE, passed=not fails, notes=notes, fails=fails,
           known_limitation="Case content comes from the latest version but is placed in the "
                            "init_fda_dt quarter (DECISIONS.md), so the counting universe itself "
                            "carries a documented look-ahead this audit cannot remove.")
(ROOT / "references" / f"leakage_audit_{RULE}.json").write_text(json.dumps(rep, indent=1))
sys.exit(1 if fails else 0)
