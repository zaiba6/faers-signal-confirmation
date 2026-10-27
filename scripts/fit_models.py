"""Baseline models for signal confirmation. Temporal split, expanding-window CV inside train.

Run: /usr/local/bin/python3 scripts/fit_models.py [T1] [confirmed_event_3y|confirmed_drug_3y]
Out: references/model_results_<rule>_<target>.json
     figures/model_pr_<rule>_<target>.png, figures/model_calibration_<rule>_<target>.png

Design notes
- Censoring: eligible pairs are already restricted to detection <= cutoff - 3 years (2023Q2 with
  SrLC data to 2026Q2), so no pair is labelled negative for want of follow-up. Asserted below.
- Split: train <= 2020Q4, test >= 2021Q1. Never random - a random split would let a drug's later
  label changes inform its earlier pairs.
- CV: expanding window inside train only (fit <=2017Q4 score 2018, <=2018Q4 score 2019,
  <=2019Q4 score 2020). Used for LightGBM early stopping and the logistic C, never for reporting.
- Prevalence shifts downward over time (1.88% in 2016 to 0.63% in 2021), so test AUPRC is
  compared against TEST prevalence, not train's, and calibration is reported rather than assumed.
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
from sklearn.metrics import (average_precision_score, roc_auc_score, precision_recall_curve)
from sklearn.calibration import calibration_curve
import lightgbm as lgb

ROOT = Path(__file__).resolve().parents[1]
P, REF, FIG = ROOT / "data/processed", ROOT / "references", ROOT / "figures"
FIG.mkdir(exist_ok=True)
RULE = (sys.argv[1] if len(sys.argv) > 1 else "T1").upper()
TARGET = sys.argv[2] if len(sys.argv) > 2 else "confirmed_event_3y"
SPLIT, CV_CUTS = "2020Q4", [("2017Q4", "2018"), ("2018Q4", "2019"), ("2019Q4", "2020")]
OUTCOME = ("confirmed_", "first_confirm", "days_to_confirm", "dsc_drug", "s921_drug")

f = duckdb.connect().execute(f"select * from read_parquet('{P}/features_{RULE}.parquet')").df()
drop = {"drug_ai", "pt_clean", "detection_quarter", "first_quarter_at_3"}
COLS = [c for c in f.columns if c not in drop and not any(p in c for p in OUTCOME)]
assert f.detection_quarter.max() <= "2023Q2", "censoring violated: a pair lacks 3 years of follow-up"

f["year"] = f.detection_quarter.str[:4]
tr, te = f[f.detection_quarter <= SPLIT], f[f.detection_quarter > SPLIT]
Xtr, ytr, Xte, yte = tr[COLS], tr[TARGET].values, te[COLS], te[TARGET].values
prev_tr, prev_te = ytr.mean(), yte.mean()


def p_at_k(y, s, k=100):
    idx = np.argsort(-s)[:k]
    return float(y[idx].mean())


# ---- expanding-window CV inside train: pick logistic C, and LightGBM's n_estimators -----------
def cv_score(make):
    """Mean average-precision over the expanding-window folds."""
    out = []
    for cut, yr in CV_CUTS:
        a, b = tr[tr.detection_quarter <= cut], tr[tr.year == yr]
        if b[TARGET].sum() < 5:
            continue
        m = make().fit(a[COLS], a[TARGET])
        out.append(average_precision_score(b[TARGET], m.predict_proba(b[COLS])[:, 1]))
    return out


def logit(C):
    return make_pipeline(SimpleImputer(strategy="median", add_indicator=True), StandardScaler(),
                         LogisticRegression(penalty="l2", C=C, max_iter=2000, class_weight="balanced"))


cv_log = {C: float(np.mean(cv_score(lambda C=C: logit(C)))) for C in (0.01, 0.1, 1.0)}
best_C = max(cv_log, key=cv_log.get)

pos_w = (len(ytr) - ytr.sum()) / max(ytr.sum(), 1)
def make_lgb(n=2000):
    return lgb.LGBMClassifier(n_estimators=n, learning_rate=0.05, num_leaves=31, min_child_samples=50,
                              subsample=0.8, subsample_freq=1, colsample_bytree=0.8,
                              scale_pos_weight=pos_w, verbose=-1, random_state=0)
# Early stopping on a single fold-year fired at iteration 1 (few positives, noisy AP), which is a
# tuning artefact rather than a result. Pick n_estimators by mean CV AP over an explicit grid.
cv_lgb = {n: float(np.mean(cv_score(lambda n=n: make_lgb(n)))) for n in (50, 150, 400, 800)}
best_iter = max(cv_lgb, key=cv_lgb.get)

# ---- fit on full train, score test ------------------------------------------------------------
models, scores = {}, {}
models["logistic_l2"] = logit(best_C).fit(Xtr, ytr)
models["lightgbm"] = make_lgb(best_iter).fit(Xtr, ytr)
for k, m in models.items():
    scores[k] = m.predict_proba(Xte)[:, 1]
rng = np.random.default_rng(0)
scores["prevalence_baseline"] = rng.random(len(yte))     # no information, by construction

res = {"rule": RULE, "target": TARGET, "n_train": len(tr), "n_test": len(te),
       "pos_train": int(ytr.sum()), "pos_test": int(yte.sum()),
       "prevalence_train_pct": round(100 * prev_tr, 3), "prevalence_test_pct": round(100 * prev_te, 3),
       "cv_logistic_ap_by_C": cv_log, "chosen_C": best_C,
       "cv_lightgbm_ap_by_n": {k: round(v, 4) for k, v in cv_lgb.items()}, "chosen_n_estimators": best_iter,
       "features": COLS, "models": {}}
for k, s in scores.items():
    ap = average_precision_score(yte, s)
    res["models"][k] = dict(
        auprc=round(float(ap), 4), auprc_lift_vs_prevalence=round(float(ap / prev_te), 2),
        auroc=round(float(roc_auc_score(yte, s)), 4),
        precision_at_100=round(p_at_k(yte, s), 3),
        precision_at_100_by_test_year={yr: round(p_at_k(te[te.year == yr][TARGET].values,
                                                       s[(te.year == yr).values]), 3)
                                       for yr in sorted(te.year.unique())})
res["models"]["prevalence_baseline"]["note"] = "random scores; AUPRC should sit at test prevalence"

# ---- figures ----------------------------------------------------------------------------------
plt.figure(figsize=(6, 4.5))
for k in ("lightgbm", "logistic_l2"):
    pr, rc, _ = precision_recall_curve(yte, scores[k])
    plt.plot(rc, pr, label=f"{k} (AP={res['models'][k]['auprc']:.3f})")
plt.axhline(prev_te, ls="--", c="grey", label=f"test prevalence ({100*prev_te:.2f}%)")
plt.xlabel("Recall"); plt.ylabel("Precision"); plt.yscale("log")
plt.title(f"Precision-recall, {RULE} {TARGET}\ntest = detection 2021Q1-2023Q2, n={len(te):,}")
plt.legend(fontsize=8); plt.tight_layout()
plt.savefig(FIG / f"model_pr_{RULE}_{TARGET}.png", dpi=150); plt.close()

plt.figure(figsize=(5.5, 5))
for k in ("lightgbm", "logistic_l2"):
    pt, pp = calibration_curve(yte, scores[k], n_bins=10, strategy="quantile")
    plt.plot(pp, pt, "o-", label=k)
plt.plot([0, max(scores['lightgbm'].max(), 0.01)], [0, max(scores['lightgbm'].max(), 0.01)], "--", c="grey", label="perfect")
plt.axhline(prev_te, ls=":", c="red", lw=1, label="test prevalence")
plt.xlabel("Mean predicted probability"); plt.ylabel("Observed fraction positive")
plt.title(f"Calibration, {RULE} {TARGET}"); plt.legend(fontsize=8); plt.tight_layout()
plt.savefig(FIG / f"model_calibration_{RULE}_{TARGET}.png", dpi=150); plt.close()

imp = sorted(zip(COLS, models["lightgbm"].feature_importances_), key=lambda x: -x[1])[:15]
res["lightgbm_top_features"] = [(c, int(v)) for c, v in imp]
(REF / f"model_results_{RULE}_{TARGET}.json").write_text(json.dumps(res, indent=1))
print(json.dumps({k: v for k, v in res.items() if k != "features"}, indent=1))
