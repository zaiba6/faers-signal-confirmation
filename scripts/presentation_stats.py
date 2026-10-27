"""Compile the numbers and figures for the class presentation. Reads committed outputs only; fits nothing.

Run: /usr/local/bin/python3 scripts/presentation_stats.py
Out: figures/*.png and figures/*.csv  (listed at the end of the run)
Headline rule is T1 (the broadest); T2/T3 appear wherever the comparison is cheap.
"""
import json, warnings
from pathlib import Path
import duckdb, numpy as np, pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

warnings.filterwarnings("ignore")
ROOT = Path(__file__).resolve().parents[1]
P, L, F = ROOT / "data/processed", ROOT / "data/processed/labels", ROOT / "figures"
F.mkdir(exist_ok=True)
CUT = "2020Q4"          # proposed temporal cutoff (end of train)
RULES = ("T1", "T2", "T3")
HEAD = "T1"
Y = "confirmed_event_3y"
con = duckdb.connect()
saved = []


def save_csv(df, name, index=False):
    df.to_csv(F / name, index=index); saved.append(name)


def save_fig(name):
    plt.tight_layout(); plt.savefig(F / name, dpi=150, bbox_inches="tight"); plt.close(); saved.append(name)


def load(t):
    return con.execute(f"select * from read_parquet('{P}/confirmation_target_{t}.parquet')").df()


tgt = {t: load(t) for t in RULES}
d = tgt[HEAD]

# ---------- B. dataset counts ----------
rows = []
for t in RULES:
    x = tgt[t]
    rows.append(dict(rule=t, signals=len(x), confirmed_event=int(x[Y].sum()),
                     unconfirmed=int((x[Y] == 0).sum()), pct_confirmed=round(100 * x[Y].mean(), 2),
                     confirmed_drug=int(x.confirmed_drug_3y.sum()),
                     pct_confirmed_drug=round(100 * x.confirmed_drug_3y.mean(), 1),
                     unique_drugs=int(x.drug_ai.nunique()), unique_events=int(x.pt_clean.nunique()),
                     first_signal_q=x.detection_quarter.min(), last_signal_q=x.detection_quarter.max()))
counts = pd.DataFrame(rows)
save_csv(counts, "B_dataset_counts.csv")

# confirmation by source (SrLC label change is the target; DSC/921 are drug-level flags)
src = pd.DataFrame([dict(
    rule=t,
    srlc_label_change_only=int(((tgt[t][Y] == 1) & (tgt[t].dsc_drug_3y == 0)).sum()),
    dsc_only=int(((tgt[t][Y] == 0) & (tgt[t].dsc_drug_3y == 1)).sum()),
    both=int(((tgt[t][Y] == 1) & (tgt[t].dsc_drug_3y == 1)).sum()),
    neither=int(((tgt[t][Y] == 0) & (tgt[t].dsc_drug_3y == 0)).sum())) for t in RULES])
save_csv(src, "B_confirmed_by_source.csv")

# right-censoring: pairs detected too late for a full 3-year window
cens = con.execute(" union all ".join(
    f"""select '{t}' as rule_name, status, count(*) as n from read_parquet('{P}/detected_pairs_{t}.parquet') group by status""" for t in RULES)).df()
cens = cens.pivot(index="rule_name", columns="status", values="n").reset_index()
save_csv(cens, "D_censoring_by_status.csv")

# ---------- C. EDA ----------
bal = d[Y].value_counts().rename({0: "unconfirmed", 1: "confirmed"})
plt.figure(figsize=(5, 4))
b = plt.bar(["unconfirmed", "confirmed"], [bal["unconfirmed"], bal["confirmed"]], color=["#9aa5b1", "#c0392b"])
for r, v in zip(b, [bal["unconfirmed"], bal["confirmed"]]):
    plt.text(r.get_x() + r.get_width() / 2, v, f"{v:,}\n({100*v/len(d):.2f}%)", ha="center", va="bottom", fontsize=9)
plt.ylabel("signals"); plt.title(f"Class balance, {HEAD}, confirmed_event_3y"); plt.ylim(0, len(d) * 1.15)
save_fig("C1_class_balance.png")

stat = []
for col in ("ror", "prr", "a"):
    for lab, g in d.groupby(Y):
        s = g[col].replace([np.inf, -np.inf], np.nan).dropna()
        stat.append(dict(measure=col, label="confirmed" if lab else "unconfirmed", n=len(s),
                         median=round(s.median(), 3), q1=round(s.quantile(.25), 3), q3=round(s.quantile(.75), 3),
                         iqr=round(s.quantile(.75) - s.quantile(.25), 3),
                         min=round(s.min(), 3), max=round(s.max(), 3)))
save_csv(pd.DataFrame(stat), "C_ror_prr_by_label.csv")

fig, ax = plt.subplots(1, 2, figsize=(9, 4))
for i, col in enumerate(("ror", "prr")):
    data = [d.loc[d[Y] == v, col].replace([np.inf, -np.inf], np.nan).dropna() for v in (0, 1)]
    ax[i].boxplot(data, labels=["unconfirmed", "confirmed"], showfliers=False)
    ax[i].set_yscale("log"); ax[i].set_ylabel(col.upper()); ax[i].set_title(f"{col.upper()} at detection ({HEAD})")
    ax[i].grid(alpha=.3, axis="y")
save_fig("C2_ror_prr_boxplot.png")

yr = d.assign(year=d.detection_quarter.str[:4].astype(int)).groupby("year").agg(
    signals=(Y, "size"), confirmed=(Y, "sum"))
yr["rate_pct"] = (100 * yr.confirmed / yr.signals).round(2)
save_csv(yr.reset_index(), "C_signals_per_year.csv")

fig, ax1 = plt.subplots(figsize=(7.5, 4))
ax1.bar(yr.index, yr.signals, color="#9aa5b1", label="signals")
ax1.set_ylabel("signals detected"); ax1.set_xlabel("detection year")
ax2 = ax1.twinx(); ax2.plot(yr.index, yr.rate_pct, "o-", color="#c0392b", label="confirmed rate")
ax2.set_ylabel("confirmed rate (%)", color="#c0392b"); ax2.set_ylim(0, yr.rate_pct.max() * 1.25)
ax1.axvline(int(CUT[:4]) + .5, ls="--", c="k", lw=1)
ax1.text(int(CUT[:4]) + .6, ax1.get_ylim()[1] * .95, f"train/test cut\nend {CUT}", fontsize=8, va="top")
ax1.set_title(f"Signals and confirmed rate per detection year ({HEAD})")
save_fig("C3_signals_and_rate_per_year.png")

sc = d.sample(min(40000, len(d)), random_state=7)
plt.figure(figsize=(6.5, 4.5))
neg, pos = sc[sc[Y] == 0], sc[sc[Y] == 1]
plt.scatter(neg.a, neg.ror, s=4, alpha=.15, c="#9aa5b1", label="unconfirmed", rasterized=True)
plt.scatter(pos.a, pos.ror, s=9, alpha=.75, c="#c0392b", label="confirmed")
plt.xscale("log"); plt.yscale("log"); plt.xlabel("report count a (log)"); plt.ylabel("ROR (log)")
plt.title(f"Reports vs ROR at detection ({HEAD}, {len(sc):,} sampled)"); plt.legend(markerscale=2); plt.grid(alpha=.3)
save_fig("C4_a_vs_ror_scatter.png")

top_d = d.groupby("drug_ai").agg(signals=(Y, "size"), confirmed=(Y, "sum")).sort_values("signals", ascending=False).head(10)
top_e = d.groupby("pt_clean").agg(signals=(Y, "size"), confirmed=(Y, "sum")).sort_values("signals", ascending=False).head(10)
for x in (top_d, top_e):
    x["rate_pct"] = (100 * x.confirmed / x.signals).round(2)
save_csv(top_d.reset_index(), "C_top10_drugs.csv"); save_csv(top_e.reset_index(), "C_top10_events.csv")

fig, ax = plt.subplots(1, 2, figsize=(12, 4))
for i, (x, t) in enumerate(((top_d, "drugs"), (top_e, "events"))):
    lbl = [s[:34] + ("..." if len(s) > 34 else "") for s in x.index]
    ax[i].barh(lbl[::-1], x.signals.values[::-1], color="#4a6fa5")
    ax[i].set_title(f"Top 10 {t} by signal count ({HEAD})"); ax[i].set_xlabel("signals"); ax[i].tick_params(labelsize=8)
save_fig("C5_top10_drugs_events.png")

# ---------- D. train / test ----------
sp = []
for t in RULES:
    x = tgt[t]
    for name, s in (("train", x[x.detection_quarter <= CUT]), ("test", x[x.detection_quarter > CUT])):
        sp.append(dict(rule=t, split=name, quarters=f"{s.detection_quarter.min()}-{s.detection_quarter.max()}",
                       n_signals=len(s), n_confirmed_event=int(s[Y].sum()),
                       positive_rate_pct=round(100 * s[Y].mean(), 2),
                       n_confirmed_drug=int(s.confirmed_drug_3y.sum()),
                       drug_rate_pct=round(100 * s.confirmed_drug_3y.mean(), 1)))
save_csv(pd.DataFrame(sp), "D_train_test_split.csv")

# ---------- E. matcher ----------
ev = []
for tag, path, n_yes, n in (("v1 (design sample)", "event_matcher_judged.csv", 42, 50),
                            ("v2 (fresh, post-fix)", "event_matcher_judged_v2.csv", 46, 50)):
    p = n_yes / n; z = 1.96; dd = 1 + z * z / n
    c = (p + z * z / (2 * n)) / dd
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** .5) / dd
    ev.append(dict(sample=tag, n=n, true_positives=n_yes, precision_pct=round(100 * p, 1),
                   ci95_low=round(100 * (c - h), 1), ci95_high=round(100 * (c + h), 1),
                   role="designed the fix" if tag.startswith("v1") else "evaluated the fix"))
save_csv(pd.DataFrame(ev), "E_matcher_precision.csv")

before = dict(T1=3739, T2=3384, T3=3595)                 # word-boundary matcher, pre-population-veto
contra_before, contra_after = dict(T1=85, T2=74, T3=92), {}
eff = []
for t in RULES:
    after = int(tgt[t][Y].sum())
    contra_after[t] = int((tgt[t].first_confirm_section == "CONTRA").sum())
    eff.append(dict(rule=t, event_positives_before=before[t], event_positives_after=after,
                    change_pct=round(100 * (after - before[t]) / before[t], 1),
                    contra_first_confirm_before=contra_before[t], contra_first_confirm_after=contra_after[t]))
save_csv(pd.DataFrame(eff), "E_matcher_fix_effect.csv")

# ---------- F. oddities ----------
odd = dict(
    pct_pairs_with_a_le_5=round(100 * (d.a <= 5).mean(), 1),
    median_a=int(d.a.median()), pct_a_eq_3=round(100 * (d.a == 3).mean(), 1),
    median_ror_confirmed=round(d.loc[d[Y] == 1, "ror"].median(), 2),
    median_ror_unconfirmed=round(d.loc[d[Y] == 0, "ror"].median(), 2),
    n_duplicate_pairs=int(d.duplicated(["drug_ai", "pt_clean"]).sum()),
    rate_2016_pct=float(yr.loc[2016, "rate_pct"]), rate_2021_pct=float(yr.loc[2021, "rate_pct"]),
    pct_signals_in_top10_drugs=round(100 * top_d.signals.sum() / len(d), 1),
    n_stub_label_rows=int(json.loads((L / "s2_parse_report.json").read_text())["n_stub_rows"]),
    median_days_to_confirm=int(d.days_to_confirm.median()) if d.days_to_confirm.notna().any() else None)
save_csv(pd.DataFrame([odd]).T.rename(columns={0: "value"}), "F_oddities.csv", index=True)

print(json.dumps(odd, indent=1, default=str))
print("\nsaved to figures/:")
for s in saved:
    print("  ", s)
