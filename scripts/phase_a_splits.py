"""Phase A addendum: what split designs are affordable at H = 36 months. Read-only.

Run: /usr/local/bin/python3 scripts/phase_a_splits.py
Out: results/phase_a/a6_split_options.csv / .json

A training label is only safe if it has resolved before the test period starts, so train must end at
least H months before test begins. That gap is pure cost: detections inside it are used by nobody.
"""
import json
from pathlib import Path
import duckdb, pandas as pd

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/phase_a"; OUT.mkdir(parents=True, exist_ok=True)
H_MONTHS = 36
TARGET = "confirmed_event_3y"

con = duckdb.connect()
d = con.execute(f"""select detection_quarter, {TARGET} as y
                    from read_parquet('{ROOT}/data/processed/confirmation_target_T1.parquet')""").df()
d["qi"] = d.detection_quarter.str[:4].astype(int) * 4 + d.detection_quarter.str[-1].astype(int)
QMIN, QMAX = d.qi.min(), d.qi.max()
GAP_Q = H_MONTHS // 3


def block(lo, hi):
    g = d[(d.qi >= lo) & (d.qi <= hi)]
    return dict(n=len(g), pos=int(g.y.sum()), prev_pct=round(100 * g.y.mean(), 3) if len(g) else None)


def qlabel(qi):
    return f"{(qi - 1) // 4}Q{((qi - 1) % 4) + 1}"


rows = []
# Design 1: expanding folds, gap = H, test = 1 year, no lock-box
for train_end in range(QMIN + 3, QMAX + 1):
    ts = train_end + GAP_Q + 1
    te = ts + 3
    if te > QMAX:
        break
    rows.append(dict(design="expanding_gap36_1yr_test", train=f"{qlabel(QMIN)}-{qlabel(train_end)}",
                     test=f"{qlabel(ts)}-{qlabel(te)}", **{f"train_{k}": v for k, v in block(QMIN, train_end).items()},
                     **{f"test_{k}": v for k, v in block(ts, te).items()}))
# Design 2: same but last 4 quarters reserved as an untouched lock-box
LOCK_LO = QMAX - 3
for train_end in range(QMIN + 3, QMAX + 1):
    ts = train_end + GAP_Q + 1
    te = ts + 3
    if te >= LOCK_LO:
        break
    rows.append(dict(design="expanding_gap36_1yr_test_plus_lockbox", train=f"{qlabel(QMIN)}-{qlabel(train_end)}",
                     test=f"{qlabel(ts)}-{qlabel(te)}", **{f"train_{k}": v for k, v in block(QMIN, train_end).items()},
                     **{f"test_{k}": v for k, v in block(ts, te).items()}))
# Design 3: single split with the full gap, lock-box = last 4 quarters
for train_end in (QMIN + 11, QMIN + 15):
    ts = train_end + GAP_Q + 1
    if ts > LOCK_LO - 1:
        continue
    rows.append(dict(design="single_split_gap36_plus_lockbox", train=f"{qlabel(QMIN)}-{qlabel(train_end)}",
                     test=f"{qlabel(ts)}-{qlabel(LOCK_LO - 1)}",
                     **{f"train_{k}": v for k, v in block(QMIN, train_end).items()},
                     **{f"test_{k}": v for k, v in block(ts, LOCK_LO - 1).items()}))
# Design 4: current design, no gap at all, for comparison
rows.append(dict(design="current_no_gap_no_lockbox", train=f"{qlabel(QMIN)}-2020Q4", test="2021Q1-2023Q2",
                 **{f"train_{k}": v for k, v in block(QMIN, 2020 * 4 + 4).items()},
                 **{f"test_{k}": v for k, v in block(2021 * 4 + 1, QMAX).items()}))

df = pd.DataFrame(rows)
df.to_csv(OUT / "a6_split_options.csv", index=False)
summary = dict(horizon_months=H_MONTHS, gap_quarters=GAP_Q,
               cohort=f"{qlabel(QMIN)}-{qlabel(QMAX)}", cohort_n=len(d), cohort_pos=int(d.y.sum()),
               lockbox_if_last_4q=dict(period=f"{qlabel(LOCK_LO)}-{qlabel(QMAX)}", **block(LOCK_LO, QMAX)),
               designs={k: dict(folds=int((df.design == k).sum()),
                                total_test_positives=int(df[df.design == k].test_pos.sum()),
                                min_test_positives=int(df[df.design == k].test_pos.min()) if (df.design == k).any() else 0)
                        for k in df.design.unique()})
(OUT / "a6_split_options.json").write_text(json.dumps(summary, indent=1))
print(json.dumps(summary, indent=1))
print()
print(df.to_string(index=False))
