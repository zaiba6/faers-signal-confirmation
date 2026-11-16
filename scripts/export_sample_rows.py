"""Read-only export of a slide-ready sample from the final modelling dataset.

Run: /usr/local/bin/python3 scripts/export_sample_rows.py
Out: figures/sample_rows.csv, figures/sample_rows.md, figures/sample_rows.png,
     results/sample_rows_checks.json

Reads data/processed/features_T1.parquet and joins confirmation_target_T1.parquet only to read
first_confirm_date, so that a confirmation dated before its own detection would be visible.
Nothing is written back to data/.
"""
import json
from pathlib import Path
import duckdb, pandas as pd, numpy as np
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "data/processed"
FIG = ROOT / "figures"; FIG.mkdir(exist_ok=True)
RES = ROOT / "results"; RES.mkdir(exist_ok=True)
SEED, MIN_A, N_PER = 42, 20, 4
# Product-use and circumstance PTs are valid MedDRA terms but are not adverse effects, and they
# almost never attract a label change: 320 such terms carry 15,589 signals at a 0.071% confirmation
# rate against 1.228% for clinical terms. They are excluded from the SAMPLE here; excluding them
# from the dataset is a separate decision. Keyword based, because the processed tables carry PT
# strings only and the MedDRA hierarchy (which would give the Product Issues and Social
# Circumstances SOCs directly) is a licensed file this project does not have.
NON_CLINICAL = (r"PRODUCT USE|OFF LABEL|DRUG INEFFECTIVE|WRONG |INCORRECT |ACCIDENTAL |DEVICE |"
                r"USER$|THERAPY |NO ADVERSE EVENT|DOSE OMI|EXPIRED PRODUCT|UNDERDOSE|"
                r"PRODUCT QUALITY|PRODUCT SUBSTITUT|MEDICATION ERROR|TREATMENT NONCOMP|"
                r"INTERCEPTED|PRESCRIBED|DISPENSING|LABEL|CONTRAINDICATED PRODUCT|"
                r"PRODUCT PREPARATION|PRODUCT STORAGE|UNEVALUABLE|SOCIAL |CIRCUMSTANCE|"
                r"TEST ABNORMAL|INVESTIGATION")
Y = "confirmed_event_3y"

con = duckdb.connect()
f = con.execute(f"select * from read_parquet('{P}/features_T1.parquet')").df()
tg = con.execute(f"""select drug_ai, pt_clean, detection_quarter, first_confirm_date, days_to_confirm
                     from read_parquet('{P}/confirmation_target_T1.parquet')""").df()
f = f.merge(tg, on=["drug_ai", "pt_clean", "detection_quarter"], how="left")

# ---------------------------------------------------------------- dataset level
checks = dict(
    dataset="data/processed/features_T1.parquet",
    rows=len(f), y1=int(f[Y].sum()), y0=int((f[Y] == 0).sum()),
    pct_confirmed=round(100 * f[Y].mean(), 3),
    detection_range=[f.detection_quarter.min(), f.detection_quarter.max()],
    duplicate_pairs=int(f.duplicated(["drug_ai", "pt_clean"]).sum()),
    columns=list(f.columns),
    nulls={c: int(f[c].isna().sum()) for c in f.columns if f[c].isna().any()},
    confirm_before_detection=int((f.days_to_confirm <= 0).sum()),
    rows_with_a_lt_5=int((f.a < MIN_A).sum()),
    missing_requested_column="years since approval - no approval-date field exists in any processed "
                             "table and no Drugs@FDA or other approval source has been collected")

# ---------------------------------------------------------------- sample
# Use the pre-registered rule (scripts/nonclinical_pts.py, decisions 68 and 79) rather
# than this file's own regex, so the slide shows the same cohort the pipeline built. The
# rebuild has since run, so this filter is now a no-op on features_T1 and is kept as a
# guard: if a later build stops applying it, the sample will not quietly start showing
# product-use terms. The NON_CLINICAL regex above is dead and stays only as a record of
# what the first, keyword-only attempt looked like.
import sys as _sys; _sys.path.insert(0, str(Path(__file__).parent))
from nonclinical_pts import is_nonclinical
pool = f[(f.a >= MIN_A) & (~f.pt_clean.map(is_nonclinical))].copy()
pool["year"] = pool.detection_quarter.str[:4]
rng = np.random.default_rng(SEED)


def short_drug(name):
    """FAERS writes combinations with a backslash; render as " + ".

    Every ingredient is named. "(+1 more)" hid which product the row is actually about,
    which matters when the whole point of decision 70 is that a combination's label is a
    different label. The Drug column is widened to take it.
    """
    return " + ".join(p.strip().title() for p in name.split("\\") if p.strip())


def pick(df, n):
    """Greedy pick spreading across distinct drugs and years."""
    idx = rng.permutation(len(df))
    out, drugs, years = [], set(), set()
    for pas in (0, 1):                      # pass 0 insists on a new year, pass 1 relaxes it
        for i in idx:
            r = df.iloc[i]
            if len(out) == n:
                break
            if r.drug_ai in drugs:
                continue
            if pas == 0 and r.year in years:
                continue
            out.append(r); drugs.add(r.drug_ai); years.add(r.year)
    return pd.DataFrame(out)


samp = pd.concat([pick(pool[pool[Y] == 1], N_PER), pick(pool[pool[Y] == 0], N_PER)], ignore_index=True)
samp = samp.sort_values([Y, "detection_quarter"], ascending=[False, True]).reset_index(drop=True)

tbl = pd.DataFrame({
    "Drug": samp.drug_ai.map(short_drug),
    "Event (PT)": samp.pt_clean.str.title(),
    "Detected": samp.detection_quarter,
    "Reports (a)": samp.a.astype(int),
    "ROR": samp.ror.round(2),
    "ROR 95% LCL": samp.ror_lcl.round(2),
    "PRR": samp.prr.round(2),
    "% serious": (100 * samp.frac_serious).round(0).astype("Int64"),
    "Y": samp[Y].astype(int)})

tbl.to_csv(FIG / "sample_rows.csv", index=False)   # CSV keeps the raw numbers

# Fixed 2 decimals for every RENDERED output: .round(2) still prints 1.0 next to 1.34,
# which reads as sloppy on a slide. The CSV above is untouched.
tbl_fmt = tbl.copy()
for _c in ("ROR", "ROR 95% LCL", "PRR"):
    tbl_fmt[_c] = [f"{v:,.2f}" for v in tbl[_c]]
def to_md(df):
    """Plain markdown table; avoids adding a tabulate dependency to this environment."""
    cols = list(df.columns)
    head = "| " + " | ".join(cols) + " |"
    rule = "|" + "|".join("---" for _ in cols) + "|"
    body = ["| " + " | ".join(str(v) for v in row) + " |" for row in df.astype(str).values]
    return "\n".join([head, rule] + body) + "\n"


(FIG / "sample_rows.md").write_text(to_md(tbl_fmt))

# ---------------------------------------------------------------- png
import textwrap

disp = tbl_fmt.copy()
# Long drug and event names overflow their cell and collide with neighbours, so wrap them.
disp["Drug"] = [textwrap.fill(v, 32) for v in disp["Drug"]]
disp["Event (PT)"] = [textwrap.fill(v, 26) for v in disp["Event (PT)"]]
widths = [0.25, 0.20, 0.08, 0.09, 0.08, 0.10, 0.08, 0.08, 0.04]

# Layout computed in inches so the table gets exactly the room it needs. Row heights
# follow the wrapped line count: with a fixed height a 3-line cell pushes the table past
# its axes.
lines = [1] + [max(str(v).count("\n") + 1 for v in row) for row in disp.astype(str).values]
row_in = [0.30 * l + 0.20 for l in lines]          # inches per row
table_in = sum(row_in)

# One caption line, on the figure rather than only in speaker notes: what Y means is the
# most important thing on this table, and "labelling change" is the honest wording, not
# "confirmed". The longer caveats stay in sample_rows_caption.txt.
CAP = ("Y = 1: the event was absent from the drug's label at detection, and a safety labelling "
       "change named it within 36 months. "
       f"Rows drawn with a \u2265 {MIN_A}; the cohort threshold is unchanged.")
cap_in = 0.46
fig_h = table_in + cap_in + 0.12
fig = plt.figure(figsize=(15, fig_h))
ax = fig.add_axes([0.01, (cap_in + 0.06) / fig_h, 0.98, table_in / fig_h])
ax.axis("off")

t = ax.table(cellText=disp.astype(str).values, colLabels=disp.columns,
             colWidths=widths, cellLoc="center", loc="upper center")
t.auto_set_font_size(False); t.set_fontsize(10.5)
for (r, c), cell in t.get_celld().items():
    cell.set_edgecolor("#cccccc")
    cell.set_height(row_in[r] / table_in)          # axes fraction: table fills the axes
    if r == 0:
        cell.set_facecolor("#e8eef4"); cell.set_text_props(weight="bold")
    elif tbl.iloc[r - 1]["Y"] == 1:
        cell.set_facecolor("#fdf3e3")
fig.patch.set_facecolor("white")
fig.text(0.5, (cap_in - 0.14) / fig_h, CAP, ha="center", va="top",
         fontsize=9.6, color="#444444")
plt.savefig(FIG / "sample_rows.png", dpi=200, facecolor="white", bbox_inches="tight")
plt.close()

# The caveats are no longer drawn on the figure (the slide carries the table alone), so
# they are written out separately for speaker notes. They still matter: a >= MIN_A is a
# readability filter for this table only, and Y is the pre-decision-74 definition.
(FIG / "sample_rows_caption.txt").write_text(
    f"Illustrative rows only, drawn with a >= {MIN_A} so \"% serious\" is a meaningful "
    f"fraction. The cohort threshold is unchanged (T1: a >= 3).\n\n"
    "Y = 1 means the event was ABSENT from the drug's label at the detection quarter and a "
    "safety labelling change named it within the next 36 months. The baseline check "
    "(decisions 74-78, 99-101) reads the DailyMed label version in force at detection; pairs "
    "whose label already carried the event are dropped from the cohort rather than scored, so "
    "no row here is a restatement of something already known. Applying that rule cut T1 from "
    "349,242 rows and 3,912 positives to 241,443 and 966 (1.12% -> 0.400%): 69.7% of the old "
    "positives were events the label already carried.\n\n"
    "Two known limits of the baseline check, both measured: its hand-checked accuracy is 28/30 "
    "on exclusions with no miss found in 30 kept positives, and where a drug's primary "
    "registration has no usable history the baseline falls back to another set id for that "
    "ingredient, which is a median 6.9 years stale.\n")

# ---------------------------------------------------------------- per-row checks
rowchecks = []
for i, r in samp.iterrows():
    issues = []
    if not (r.ror_lcl > 1):
        issues.append(f"ror_lcl={r.ror_lcl:.2f} is not > 1, which the T1 rule requires")
    if r.a < MIN_A:
        issues.append("a below the floor")
    if not (r.ror >= r.ror_lcl):
        issues.append("ROR below its own lower bound")
    if r.prr < 1:
        issues.append(f"PRR={r.prr:.2f} below 1 while ROR is above 1")
    if pd.isna(r.frac_serious):
        issues.append("frac_serious missing")
    if r[Y] == 1 and pd.notna(r.days_to_confirm) and r.days_to_confirm <= 0:
        issues.append("confirmation dated on or before detection")
    if r[Y] == 1 and pd.isna(r.first_confirm_date):
        issues.append("Y=1 but no confirmation date")
    if r[Y] == 0 and pd.notna(r.first_confirm_date):
        issues.append("Y=0 but a confirmation date exists")
    rowchecks.append(dict(drug=r.drug_ai, event=r.pt_clean, quarter=r.detection_quarter,
                          y=int(r[Y]), a=int(r.a), ror_lcl=round(float(r.ror_lcl), 2),
                          days_to_confirm=None if pd.isna(r.days_to_confirm) else int(r.days_to_confirm),
                          ok=not issues, issues=issues))
checks["row_checks"] = rowchecks
checks["seed"] = SEED
checks["sample_excludes_non_clinical_pts"] = True
checks["pool_rows_after_filters"] = int(len(pool))
(RES / "sample_rows_checks.json").write_text(json.dumps(checks, indent=1, default=str))

print(json.dumps({k: v for k, v in checks.items() if k not in ("columns", "row_checks")}, indent=1, default=str))
print()
print(to_md(tbl_fmt))
print()
for c in rowchecks:
    print(("OK   " if c["ok"] else "FLAG ") + f"{c['drug'][:26]:28} {c['event'][:30]:32} Y={c['y']} a={c['a']:>4} lcl={c['ror_lcl']:>6} "
          + ("; ".join(c["issues"]) if c["issues"] else ""))
