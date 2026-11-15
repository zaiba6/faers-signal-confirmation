"""Phase C: fit under the pre-registered gapped splits, and build the lock-box WITHOUT opening it.

Run: /usr/local/bin/python3 scripts/phase_c_splits.py [T1] [confirmed_event_3y]
Out: references/phase_c_<rule>_<target>.json
     figures/phase_c_pr_<rule>_<target>.png

Design is decision 60, fixed before any of these numbers were seen:

  fold 1   train 2016Q1-2016Q4   gap 12 quarters   test 2020Q1-2020Q4
  fold 2   train 2016Q1-2017Q4   gap 12 quarters   test 2021Q1-2021Q4
  lock-box 2022Q3-2023Q2, evaluated ONCE at the very end of the project.

The gap exists so that every training label has resolved before its test window opens. At H = 36
months that is 12 quarters, and the script asserts it per fold rather than trusting the arithmetic:
a training row's label is decided by detection + 36 months (negatives) or by its first confirmation
date (positives), and both must fall strictly before the first day of the test window.

HYPERPARAMETERS ARE NOT TUNED HERE, and that is deliberate. An inner expanding-window CV would have
to carry the same 12-quarter gap to be honest, and fold 1's train is four quarters long, so there is
no room for one. Borrowing the values `fit_models.py` chose is worse than useless: its CV scored
2018, 2019 and 2020, and 2020 IS fold 1's test year, so those values were partly chosen on the data
they would be tested against. The only choice with no such path is to fix the hyperparameters a
priori at the library defaults (logistic C = 1.0, LightGBM n_estimators = 100) and say so. Both
models are therefore untuned on both folds, which is a fair comparison between them and a pessimistic
one against the no-gap numbers in `model_results_*.json`.

The lock-box is loaded only to assert that it was not used. No model sees it and no metric is
computed on it. Phase G opens it.
"""
import sys, json
from pathlib import Path
import numpy as np, pandas as pd, duckdb
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import make_pipeline
from sklearn.impute import SimpleImputer
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import average_precision_score, roc_auc_score, precision_recall_curve
import lightgbm as lgb

ROOT = Path(__file__).resolve().parents[1]
P, REF, FIG = ROOT / "data/processed", ROOT / "references", ROOT / "figures"
RULE = (sys.argv[1] if len(sys.argv) > 1 else "T1").upper()
TARGET = sys.argv[2] if len(sys.argv) > 2 else "confirmed_event_3y"
OUTCOME = ("confirmed_", "first_confirm", "days_to_confirm", "dsc_drug", "s921_drug")
DROP = {"drug_ai", "pt_clean", "detection_quarter", "first_quarter_at_3"}
H_MONTHS, GAP_Q = 36, 12

# decision 60, verbatim. Never recomputed from the data.
FOLDS = [dict(name="fold1", train=("2016Q1", "2016Q4"), test=("2020Q1", "2020Q4")),
         dict(name="fold2", train=("2016Q1", "2017Q4"), test=("2021Q1", "2021Q4"))]
LOCKBOX = ("2022Q3", "2023Q2")
C_FIXED, N_EST_FIXED = 1.0, 100          # library defaults; see the module docstring


def qi(q):
    """2016Q1 -> an integer quarter index, so ranges and gaps are plain arithmetic."""
    return int(q[:4]) * 4 + int(q[-1])


def qstart(q):
    """First day of a quarter, as a Timestamp."""
    return pd.Timestamp(int(q[:4]), 1 + 3 * (int(q[-1]) - 1), 1)


f = duckdb.connect().execute(f"select * from read_parquet('{P}/features_{RULE}.parquet')").df()
tgt = duckdb.connect().execute(f"""
    select drug_ai, pt_clean, detection_quarter, first_confirm_date as first_confirm
    from read_parquet('{P}/confirmation_target_{RULE}.parquet')""").df()
f = f.merge(tgt, on=["drug_ai", "pt_clean", "detection_quarter"], how="left")
f["qi"] = f.detection_quarter.map(qi)

COLS = [c for c in f.columns
        if c not in DROP | {"qi", "first_confirm"} and not any(p in c for p in OUTCOME)]
CONST = [c for c in COLS if f[c].nunique(dropna=False) < 2]
if CONST:
    print(f"dropping zero-variance features: {CONST}")
    COLS = [c for c in COLS if c not in CONST]


def logit():
    return make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(),
                         LogisticRegression(penalty="l2", C=C_FIXED, max_iter=2000,
                                            class_weight="balanced"))


def p_at_k(y, s, k=100):
    return float(y[np.argsort(-s)[:k]].mean())


def check_gap(tr, test_lo):
    """Every training label must be decided before the test window opens.

    Positive  -> decided on first_confirm.
    Negative  -> decided when the 36-month window shuts, i.e. detection quarter start + 36 months.
    Returns the latest resolution date found, which the caller records next to the test start so a
    reader can see the margin rather than taking the assertion's word for it.
    """
    opens = qstart(test_lo)
    res = (qstart_series(tr.detection_quarter) + pd.DateOffset(months=H_MONTHS))
    res = res.where(tr[TARGET].values == 0, pd.to_datetime(tr.first_confirm))
    bad = int((res >= opens).sum())
    assert bad == 0, f"{bad} training labels unresolved when {test_lo} opens"
    return str(res.max().date()), str(opens.date())


def qstart_series(s):
    return pd.to_datetime(dict(year=s.str[:4].astype(int),
                               month=1 + 3 * (s.str[-1].astype(int) - 1), day=1))


lock = f[(f.qi >= qi(LOCKBOX[0])) & (f.qi <= qi(LOCKBOX[1]))]
rng = np.random.default_rng(0)
out = {"rule": RULE, "target": TARGET, "design": "decision 60: gap-36m folds + lock-box",
       "horizon_months": H_MONTHS, "gap_quarters": GAP_Q,
       "hyperparameters": {"logistic_C": C_FIXED, "lightgbm_n_estimators": N_EST_FIXED,
                           "tuned": False, "why": "no gapped inner CV fits in a 4-quarter train; "
                                                  "fit_models.py's CV scored 2020, which is fold 1's test"},
       "features": COLS, "folds": [], "lockbox": {"period": "-".join(LOCKBOX), "n": len(lock),
                                                  "positives": int(lock[TARGET].sum()),
                                                  "opened": False,
                                                  "note": "built and counted only; no model was fitted "
                                                          "or scored on these rows. Phase G opens it."}}
curves = {}

for fd in FOLDS:
    trlo, trhi = fd["train"]; telo, tehi = fd["test"]
    assert qi(telo) - qi(trhi) > GAP_Q, f"{fd['name']}: gap is {qi(telo) - qi(trhi) - 1} quarters"
    assert qi(tehi) < qi(LOCKBOX[0]), f"{fd['name']}: test window reaches into the lock-box"
    tr = f[(f.qi >= qi(trlo)) & (f.qi <= qi(trhi))]
    te = f[(f.qi >= qi(telo)) & (f.qi <= qi(tehi))]
    last_resolved, opens = check_gap(tr, telo)

    ytr, yte = tr[TARGET].values, te[TARGET].values
    prev = yte.mean()
    pos_w = (len(ytr) - ytr.sum()) / max(ytr.sum(), 1)
    models = {
        "logistic_l2": logit().fit(tr[COLS], ytr),
        "lightgbm": lgb.LGBMClassifier(n_estimators=N_EST_FIXED, learning_rate=0.05, num_leaves=31,
                                       min_child_samples=50, subsample=0.8, subsample_freq=1,
                                       colsample_bytree=0.8, scale_pos_weight=pos_w, verbose=-1,
                                       random_state=0).fit(tr[COLS], ytr),
    }
    scores = {k: m.predict_proba(te[COLS])[:, 1] for k, m in models.items()}
    # The ror_lcl ranking is the disproportionality statistic used as a score on its own: the
    # thing a pharmacovigilance team already has. A model that cannot beat it has earned nothing.
    scores["ror_lcl_baseline"] = te.ror_lcl.fillna(te.ror_lcl.min()).values
    scores["random_baseline"] = rng.random(len(yte))

    row = dict(name=fd["name"], train=f"{trlo}-{trhi}", test=f"{telo}-{tehi}",
               gap_quarters=qi(telo) - qi(trhi) - 1,
               n_train=len(tr), pos_train=int(ytr.sum()),
               prevalence_train_pct=round(100 * ytr.mean(), 3),
               n_test=len(te), pos_test=int(yte.sum()), prevalence_test_pct=round(100 * prev, 3),
               last_train_label_resolved=last_resolved, test_opens=opens, models={})
    for k, s in scores.items():
        ap = average_precision_score(yte, s)
        row["models"][k] = dict(auprc=round(float(ap), 4),
                                auprc_lift_vs_prevalence=round(float(ap / prev), 2),
                                auroc=round(float(roc_auc_score(yte, s)), 4),
                                precision_at_100=round(p_at_k(yte, s), 3))
    # With 63 positives a single random baseline is not a yardstick: ITS own lift moves by more
    # than the gap being argued about. 200 independent random scorings give the null distribution
    # of the lift, so "beat the baseline" has a number attached instead of a shrug. This is a
    # property of the test set, not a tuning knob - no model is refitted and nothing is selected.
    null = np.array([average_precision_score(yte, rng.random(len(yte))) / prev for _ in range(200)])
    row["random_lift_null"] = dict(median=round(float(np.median(null)), 2),
                                   p95=round(float(np.percentile(null, 95)), 2),
                                   max=round(float(null.max()), 2), draws=200)
    out["folds"].append(row)
    curves[fd["name"]] = (yte, scores, prev)

plt.figure(figsize=(10, 4.5))
for i, (name, (yte, scores, prev)) in enumerate(curves.items(), 1):
    plt.subplot(1, 2, i)
    for k in ("lightgbm", "logistic_l2", "ror_lcl_baseline"):
        pr, rc, _ = precision_recall_curve(yte, scores[k])
        ap = average_precision_score(yte, scores[k])
        plt.plot(rc, pr, label=f"{k} (AP={ap:.3f})")
    plt.axhline(prev, ls="--", c="grey", label=f"prevalence ({100*prev:.2f}%)")
    fd = next(x for x in out["folds"] if x["name"] == name)
    plt.title(f"{name}: train {fd['train']}, test {fd['test']}\n{fd['pos_test']} positives of {fd['n_test']:,}",
              fontsize=9)
    plt.xlabel("Recall"); plt.ylabel("Precision"); plt.yscale("log"); plt.legend(fontsize=7)
plt.tight_layout()
plt.savefig(FIG / f"phase_c_pr_{RULE}_{TARGET}.png", dpi=150); plt.close()

(REF / f"phase_c_{RULE}_{TARGET}.json").write_text(json.dumps(out, indent=1))
print(json.dumps({k: v for k, v in out.items() if k != "features"}, indent=1))
