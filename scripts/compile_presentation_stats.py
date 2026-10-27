"""Compile presentation statistics from the current repo state. No modelling, no new data.

Run: /usr/local/bin/python3 scripts/compile_presentation_stats.py
Out: figures/*.csv, figures/*.png, and a printed summary.
Primary rule is T1 unless a table says otherwise; T2/T3 are carried alongside.
"""
import json
from pathlib import Path
import numpy as np, pandas as pd, duckdb
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

ROOT = Path(__file__).resolve().parents[1]
P, L, F = ROOT / "data/processed", ROOT / "data/processed/labels", ROOT / "figures"
# One consistent naming scheme, regenerated from scratch: a stale file from an earlier run (or from
# a forked session) must never sit next to a fresh one, and macOS is case-insensitive so b_x.csv and
# B_x.csv are the same file.
if F.exists():
    for old in list(F.glob("*.csv")) + list(F.glob("*.png")) + list(F.glob("*.json")):
        old.unlink()
F.mkdir(exist_ok=True)
CUT = "2020Q4"
RULES = ["T1", "T2", "T3"]
plt.rcParams.update({"figure.dpi": 150, "font.size": 9, "axes.grid": True,
                     "grid.alpha": .3, "axes.spines.top": False, "axes.spines.right": False})
out = {}


def save(fig, name):
    fig.tight_layout(); fig.savefig(F / name, bbox_inches="tight"); plt.close(fig)
    return name


con = duckdb.connect()
tgt = {t: pd.read_parquet(P / f"confirmation_target_{t}.parquet") if False else
       con.execute(f"select * from read_parquet('{P}/confirmation_target_{t}.parquet')").df() for t in RULES}
for t, d in tgt.items():
    d["year"] = d.detection_quarter.str[:4].astype(int)
    d["split"] = np.where(d.detection_quarter <= CUT, "train", "test")

# ---------- B. dataset counts ----------
rows = []
for t, d in tgt.items():
    det = con.execute(f"""select status, count(*) n from read_parquet('{P}/detected_pairs_{t}.parquet')
                          group by status""").df().set_index("status").n
    rows.append(dict(rule=t, eligible_pairs=len(d), confirmed=int(d.confirmed_event_3y.sum()),
                     unconfirmed=int((d.confirmed_event_3y == 0).sum()),
                     pct_confirmed=round(100 * d.confirmed_event_3y.mean(), 2),
                     unique_drugs=int(d.drug_ai.nunique()), unique_events=int(d.pt_clean.nunique()),
                     first_signal=d.detection_quarter.min(), last_signal=d.detection_quarter.max(),
                     excluded_too_recent=int(det.get("too_recent", 0)),
                     excluded_pre_window=int(det.get("pre_window", 0)),
                     never_detected=int(det.get("never", 0)),
                     drug_level_confirmed=int(d.confirmed_drug_3y.sum())))
counts = pd.DataFrame(rows); counts.to_csv(F / "B_dataset_counts.csv", index=False)
out["B_counts"] = counts

# confirmed split by source (T1): SrLC event-match is the target; DSC/921 are drug-level flags
d = tgt["T1"]
src = pd.DataFrame({
    "source": ["SrLC label change (event-matched, = the target)", "DSC within 3y (drug-level flag)",
               "Section 921 'labeling changed' (drug-level flag)",
               "SrLC event-match AND DSC", "SrLC event-match AND 921", "SrLC event-match only (neither flag)"],
    "n_pairs": [int(d.confirmed_event_3y.sum()), int(d.dsc_drug_3y.sum()), int(d.s921_drug_3y.sum()),
                int(((d.confirmed_event_3y == 1) & (d.dsc_drug_3y == 1)).sum()),
                int(((d.confirmed_event_3y == 1) & (d.s921_drug_3y == 1)).sum()),
                int(((d.confirmed_event_3y == 1) & (d.dsc_drug_3y == 0) & (d.s921_drug_3y == 0)).sum())]})
src["pct_of_eligible"] = (100 * src.n_pairs / len(d)).round(2)
src.to_csv(F / "B_confirmed_by_source.csv", index=False); out["B_sources"] = src

# first-confirming label section
sec = d[d.confirmed_event_3y == 1].first_confirm_section.value_counts().rename_axis("section").reset_index(name="n")
sec.to_csv(F / "B_first_confirm_section.csv", index=False); out["B_section"] = sec

# ---------- C. EDA ----------
fig, ax = plt.subplots(figsize=(4.2, 3))
b = [int((d.confirmed_event_3y == 0).sum()), int(d.confirmed_event_3y.sum())]
ax.bar(["unconfirmed", "confirmed"], b, color=["#9aa5b1", "#b4453c"])
for i, v in enumerate(b):
    ax.text(i, v, f"{v:,}\n({100*v/len(d):.2f}%)", ha="center", va="bottom", fontsize=8)
ax.set_ylabel("signals (drug-event pairs)"); ax.set_title(f"T1 class balance, 3-year window (n={len(d):,})")
ax.set_ylim(0, max(b) * 1.18); save(fig, "C1_class_balance.png")

stat = []
for t, dd in tgt.items():
    for col in ["ror", "prr"]:
        for lab, g in dd.groupby("confirmed_event_3y")[col]:
            stat.append(dict(rule=t, metric=col.upper(), label="confirmed" if lab else "unconfirmed", n=len(g),
                             median=round(g.median(), 3), q1=round(g.quantile(.25), 3), q3=round(g.quantile(.75), 3),
                             iqr=round(g.quantile(.75) - g.quantile(.25), 3),
                             min=round(g.min(), 3), max=round(g.max(), 1)))
stats_df = pd.DataFrame(stat); stats_df.to_csv(F / "C_ror_prr_by_label.csv", index=False); out["C_stats"] = stats_df

fig, axes = plt.subplots(1, 2, figsize=(7.4, 3.2))
for ax, col in zip(axes, ["ror", "prr"]):
    data = [d.loc[d.confirmed_event_3y == 0, col].clip(.01, 1e4), d.loc[d.confirmed_event_3y == 1, col].clip(.01, 1e4)]
    bp = ax.boxplot(data, labels=["unconf.", "conf."], showfliers=False, patch_artist=True, widths=.55)
    for p, c in zip(bp["boxes"], ["#9aa5b1", "#b4453c"]): p.set_facecolor(c); p.set_alpha(.85)
    ax.set_yscale("log"); ax.set_ylabel(col.upper()); ax.set_title(f"{col.upper()} at detection (T1, log scale)")
save(fig, "C2_ror_prr_boxplot.png")

yr = d.groupby("year").agg(signals=("confirmed_event_3y", "size"), confirmed=("confirmed_event_3y", "sum"))
yr["pct_confirmed"] = (100 * yr.confirmed / yr.signals).round(2)
yr.reset_index().to_csv(F / "C_signals_per_year.csv", index=False); out["C_year"] = yr.reset_index()
fig, ax = plt.subplots(figsize=(6.4, 3.2))
ax.bar(yr.index, yr.signals, color="#9aa5b1", label="signals detected")
ax.set_ylabel("signals"); ax.set_xlabel("detection year")
ax2 = ax.twinx(); ax2.plot(yr.index, yr.pct_confirmed, color="#b4453c", marker="o", lw=2, label="% confirmed")
ax2.set_ylabel("% confirmed (3y, event-matched)", color="#b4453c"); ax2.grid(False); ax2.set_ylim(0, yr.pct_confirmed.max() * 1.3)
ax.axvline(2020.5, color="#2b6cb0", ls="--", lw=1.5)
ax.text(2020.42, ax.get_ylim()[1] * .52, f"train/test cut  {CUT}", color="#2b6cb0", fontsize=8,
        va="center", ha="right", rotation=90)
ax.set_title("Signals per year and confirmed rate (T1)")
h1, l1 = ax.get_legend_handles_labels(); h2, l2 = ax2.get_legend_handles_labels()
ax.legend(h1 + h2, l1 + l2, loc="lower left", fontsize=8, framealpha=.9)
save(fig, "C3_signals_and_rate_per_year.png")

s = d.sample(min(40000, len(d)), random_state=7)
fig, ax = plt.subplots(figsize=(5.4, 3.6))
for lab, c, a, z in [(0, "#9aa5b1", .25, 1), (1, "#b4453c", .8, 2)]:
    g = s[s.confirmed_event_3y == lab]
    ax.scatter(g.a.clip(1, 1e5), g.ror.clip(.05, 1e4), s=5, c=c, alpha=a, lw=0, zorder=z,
               label=("confirmed" if lab else "unconfirmed") + f" (n={len(g):,})")
ax.set_xscale("log"); ax.set_yscale("log"); ax.axhline(1, color="k", lw=.7, ls=":")
ax.set_xlabel("cases for the pair at detection (cell a)"); ax.set_ylabel("ROR at detection")
ax.set_title(f"Report count vs ROR (T1, {len(s):,} sampled)"); ax.legend(fontsize=7, markerscale=2)
save(fig, "C4_a_vs_ror_scatter.png")

top_d = d.drug_ai.value_counts().head(10).rename_axis("drug_ai").reset_index(name="signals")
top_d["confirmed"] = [int(d[d.drug_ai == x].confirmed_event_3y.sum()) for x in top_d.drug_ai]
top_e = d.pt_clean.value_counts().head(10).rename_axis("pt_clean").reset_index(name="signals")
top_e["confirmed"] = [int(d[d.pt_clean == x].confirmed_event_3y.sum()) for x in top_e.pt_clean]
top_d.to_csv(F / "C_top10_drugs.csv", index=False); top_e.to_csv(F / "C_top10_events.csv", index=False)
out["C_top_drugs"], out["C_top_events"] = top_d, top_e
fig, axes = plt.subplots(1, 2, figsize=(9.5, 3.4))
for ax, tbl, key, ttl in [(axes[0], top_d, "drug_ai", "drugs"), (axes[1], top_e, "pt_clean", "events")]:
    lab = [x[:34] + ("..." if len(x) > 34 else "") for x in tbl[key]][::-1]
    ax.barh(lab, tbl.signals[::-1], color="#9aa5b1"); ax.set_title(f"Top 10 {ttl} by signal count (T1)")
    ax.set_xlabel("signals"); ax.grid(axis="y", alpha=0)
save(fig, "C5_top10_drugs_events.png")

# ---------- D. split ----------
sp = []
for t, dd in tgt.items():
    for name, g in dd.groupby("split"):
        sp.append(dict(rule=t, split=name, n_signals=len(g), n_confirmed=int(g.confirmed_event_3y.sum()),
                       positive_rate_pct=round(100 * g.confirmed_event_3y.mean(), 3),
                       drug_level_rate_pct=round(100 * g.confirmed_drug_3y.mean(), 1),
                       quarters=f"{g.detection_quarter.min()}-{g.detection_quarter.max()}"))
split = pd.DataFrame(sp).sort_values(["rule", "split"], ascending=[True, False])
split.to_csv(F / "D_train_test_split.csv", index=False); out["D_split"] = split

cen = []
for t in RULES:
    g = con.execute(f"""select status, count(*) n from read_parquet('{P}/detected_pairs_{t}.parquet')
                        group by status""").df().set_index("status").n
    cen.append(dict(rule=t, in_window=int(g.get("in_window", 0)), too_recent=int(g.get("too_recent", 0)),
                    pre_window=int(g.get("pre_window", 0)), never=int(g.get("never", 0))))
cen = pd.DataFrame(cen); cen.to_csv(F / "D_censoring_by_status.csv", index=False); out["D_censoring"] = cen

# ---------- E. matcher quality ----------
prec = pd.DataFrame([
    dict(sample="v1 (pre-fix)", n=50, true_positives=42, precision_pct=84.0, ci95_low=71.5, ci95_high=91.7,
         role="measured the original matcher; used to DESIGN fix pass 1"),
    dict(sample="v2 (fresh, after fix 1)", n=50, true_positives=46, precision_pct=92.0, ci95_low=81.2, ci95_high=96.8,
         role="EVALUATED fix 1; its 4 failures were used to DESIGN fix pass 2"),
    dict(sample="v3 (fresh, after fix 2)", n=50, true_positives=46, precision_pct=92.0, ci95_low=81.2, ci95_high=96.8,
         role="EVALUATED fix 2; independent of both designs")])
prec.to_csv(F / "E_matcher_precision.csv", index=False); out["E_precision"] = prec
eff = pd.DataFrame([
    dict(stage="original (substring)", T1_event_positives=4055, T1_contra_first_confirms=None),
    dict(stage="word-boundary", T1_event_positives=3739, T1_contra_first_confirms=85),
    dict(stage="after fix 1 (population veto)", T1_event_positives=3535, T1_contra_first_confirms=6),
    dict(stage="after fix 2 (nearest-cue)", T1_event_positives=int(tgt["T1"].confirmed_event_3y.sum()),
         T1_contra_first_confirms=int((tgt["T1"].first_confirm_section == "CONTRA").sum()))])
eff.to_csv(F / "E_matcher_fix_effect.csv", index=False); out["E_effect"] = eff

# ---------- F. oddities ----------
odd = dict(
    ties_on_ror=int((d.ror.round(6).duplicated(keep=False)).sum()),
    pairs_with_a_le_5=int((d.a <= 5).sum()), pct_a_le_5=round(100 * (d.a <= 5).mean(), 1),
    median_a=float(d.a.median()), median_a_confirmed=float(d.loc[d.confirmed_event_3y == 1, "a"].median()),
    median_ror_confirmed=round(float(d.loc[d.confirmed_event_3y == 1, "ror"].median()), 2),
    median_ror_unconfirmed=round(float(d.loc[d.confirmed_event_3y == 0, "ror"].median()), 2),
    duplicate_pair_rows=int(d.duplicated(["drug_ai", "pt_clean"]).sum()),
    stub_label_rows=1465, pairs_confirmed_by_stub_impossible=True)
json.dump(odd, open(F / "F_oddities.json", "w"), indent=1); out["F"] = odd
pd.DataFrame([odd]).T.rename(columns={0: "value"}).to_csv(F / "F_oddities.csv")
zero = d.groupby("drug_ai").agg(signals=("confirmed_event_3y", "size"), drug_level=("confirmed_drug_3y", "sum"),
                                event_level=("confirmed_event_3y", "sum"))
zero = zero[zero.signals >= 500].sort_values("signals", ascending=False).head(15).reset_index()
zero.to_csv(F / "F_high_volume_drugs_zero_event_confirms.csv", index=False); out["F_zero"] = zero

for k, v in out.items():
    print(f"\n===== {k} =====")
    print(v.to_string(index=False) if isinstance(v, pd.DataFrame) else json.dumps(v, indent=1, default=str))
print("\nfigures written to", F)
