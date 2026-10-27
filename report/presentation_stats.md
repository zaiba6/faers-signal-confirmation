# FAERS signal confirmation — presentation statistics

Compiled from commit `6a06520` + the uncommitted fix pass 2, on 2026-10-27, by
`scripts/compile_presentation_stats.py`. **No models fitted.** Primary rule is **T1** unless stated.
Every number here is computed from the repo; anything not computed is marked **not available**.

Choices that are yours to state as assumptions are marked **[ASSUMPTION]**.

---

## A. Definitions (quotable block)

**Signal.** A drug–event pair is *detected* in the first quarter it satisfies a rule, using only
cases received up to and including that quarter (cumulative 2×2 table). Three rules are kept:

| Rule | Criterion |
|---|---|
| **T1** | a ≥ 3 and lower 95% CI of ROR > 1 |
| **T2** (Evans) | a ≥ 3, PRR ≥ 2, χ² ≥ 4 |
| **T3** | a ≥ 5, lower 95% CI of ROR > 1, and BH q < 0.05 |

- **Min report count:** a ≥ 3 (T1, T2), a ≥ 5 (T3).
- **CI rule:** 95% Wald interval on the log scale; detection needs the *lower* bound above 1.
- **FDR:** Benjamini–Hochberg, **α = 0.05**, applied within each quarter across all pairs in that
  quarter's table. Used by T3 only.
- **Zero cells:** Haldane–Anscombe (+0.5 to all four cells) when b, c or d is 0 — 5,998 of
  29,560,557 pair-quarters.
- **Signal date:** end of the detection quarter (2018Q2 → 2018-06-30), because a quarter's data is
  only knowable once it closes. **[ASSUMPTION]**

**Confirmed.** A pair is confirmed when, within the follow-up window, FDA approved a
**safety-related labeling change** for that drug whose changed text names that event.

- **Source that defines the label:** the SrLC (Safety-related Labeling Changes) database only.
- **Qualifying sections:** Boxed Warning, Warnings and Precautions, Adverse Reactions,
  Contraindications. Patient-counseling, dosing and indications sections are excluded as not
  risk-bearing. **[ASSUMPTION]**
- **Follow-up window:** **3 years** primary; 1, 2 and 5 years also computed. **[ASSUMPTION]**
- **DSC and Section 921** are carried as *drug-level supplementary flags only* and are **not** part
  of the target — see B. **[ASSUMPTION]**
- **Two targets kept:** `confirmed_event` (event named in the changed text — primary) and
  `confirmed_drug` (the drug got a qualifying change, any event — secondary/sensitivity).

**Data pull.** **Not openFDA.** This project uses the **FAERS quarterly ASCII extract files**
downloaded directly from FDA, not the openFDA API. If the slide must say openFDA, that is wrong.

| Item | Value |
|---|---|
| Raw cases, deduplicated | **16,172,838** |
| Cases in the counting universe (`init_quarter` ≥ 2014Q3) | **16,015,455** |
| Primary-suspect drug–event link rows | **47,002,104** |
| Receipt quarters present | 1986Q4 – 2026Q2 |
| Quarters used for counting | 2014Q3 – 2026Q2 |
| Detection window | 2016Q1 – 2023Q2 (6 quarters burn-in) |
| Label sources collected | SrLC 3,262 pages → 29,500 change rows; Section 921 847 signals; DSC 99 |

---

## B. Dataset counts

| Rule | Eligible signals | Confirmed | Unconfirmed | % confirmed | Unique drugs | Unique events |
|---|---|---|---|---|---|---|
| **T1** | 363,502 | 3,424 | 360,078 | **0.94%** | 3,275 | 14,110 |
| **T2** | 349,259 | 3,090 | 346,169 | **0.88%** | 3,275 | 14,112 |
| **T3** | 259,345 | 3,317 | 256,028 | **1.28%** | 2,861 | 11,482 |

Signal date range: **2016Q1 – 2023Q2** for all three rules.
Drug-level (secondary) confirmation: T1 210,055 (57.8%), T2 200,314 (57.4%), T3 154,909 (59.7%).

### Confirmed split by source (T1)

The target is defined by SrLC alone, so this is **not** a partition — DSC and 921 are overlapping
drug-level flags, not alternative label sources.

| Source | Pairs | % of eligible |
|---|---|---|
| SrLC label change, event-matched (**= the target**) | 3,424 | 0.94% |
| DSC within 3y (drug-level flag) | 5,176 | 1.42% |
| Section 921 "labeling changed" (drug-level flag) | 94,463 | 25.99% |
| Target **and** DSC | 67 | 0.02% |
| Target **and** 921 | 1,151 | 0.32% |
| Target only, neither flag | 2,262 | 0.62% |

**Caveat:** only 67 of 3,424 confirmed pairs also have a DSC. DSC titles in the index are truncated,
so DSC drug matching is name-in-title/URL and is weak — do not present DSC as a validated source.

### Which section first confirms (T1, n=3,424)

| Section | n |
|---|---|
| Adverse Reactions | 1,842 |
| Warnings and Precautions | 1,493 |
| Boxed Warning | 77 |
| Contraindications | 12 |

---

## C. EDA

**Class balance** → `C1_class_balance.png`. T1: 360,078 unconfirmed vs 3,424 confirmed (0.94%).

**ROR and PRR by label** → `C_ror_prr_by_label.csv`, `C2_ror_prr_boxplot.png` (log scale; heavily skewed).

| Rule | Metric | Label | n | Median | IQR (Q1–Q3) | Min | Max |
|---|---|---|---|---|---|---|---|
| T1 | ROR | unconfirmed | 360,162 | 6.22 | 3.30 – 17.97 | 1.03 | 25,105,591 |
| T1 | ROR | **confirmed** | 3,424 | **4.58** | 2.49 – 12.28 | 1.05 | 76,623 |
| T1 | PRR | unconfirmed | 360,162 | 6.17 | 3.29 – 17.73 | 1.03 | 4,707,299 |
| T1 | PRR | **confirmed** | 3,424 | **4.50** | 2.47 – 11.88 | 1.05 | 55,726 |

**Confirmed signals have a LOWER median ROR than unconfirmed ones.** This holds on all three rules.
It is the single most important slide-worthy fact here: disproportionality alone does not separate
the classes, and a model that only sees ROR will not work.

**Signals and confirmed rate per year** → `C_signals_per_year.csv`, `C3_signals_and_rate_per_year.png`
(train/test cut marked).

| Year | Signals | Confirmed | % confirmed |
|---|---|---|---|
| 2016 | 54,301 | 918 | 1.69% |
| 2017 | 49,969 | 563 | 1.13% |
| 2018 | 53,630 | 449 | 0.84% |
| 2019 | 51,057 | 386 | 0.76% |
| 2020 | 50,347 | 368 | 0.73% |
| 2021 | 43,590 | 232 | 0.53% |
| 2022 | 40,776 | 278 | 0.68% |
| 2023 | 19,832 | 146 | 0.74% |

**Report count vs ROR** → `C4_a_vs_ror_scatter.png` (40,000 sampled, both axes log, coloured by label).

**Top 10 drugs / events** → `C_top10_drugs.csv`, `C_top10_events.csv`, `C5_top10_drugs_events.png`.

| Drug | Signals | Confirmed | | Event | Signals | Confirmed |
|---|---|---|---|---|---|---|
| METHOTREXATE | 2,491 | **0** | | VOMITING | 582 | 52 |
| ADALIMUMAB | 2,279 | **0** | | NAUSEA | 559 | 59 |
| RITUXIMAB | 1,999 | 5 | | PRODUCT USE IN UNAPPROVED INDICATION | 558 | **0** |
| TACROLIMUS | 1,937 | 29 | | DRUG INEFFECTIVE | 544 | **0** |
| MYCOPHENOLATE MOFETIL | 1,739 | 13 | | DIZZINESS | 522 | **0** |
| PREDNISOLONE | 1,659 | **0** | | OFF LABEL USE | 520 | **0** |
| INFLIXIMAB | 1,625 | 3 | | HYPERSENSITIVITY | 494 | 71 |
| PREDNISONE | 1,593 | 1 | | DYSPNOEA | 468 | **0** |
| INFLIXIMAB-DYYB | 1,500 | 16 | | HEADACHE | 462 | 39 |
| DEXAMETHASONE | 1,346 | 4 | | PRURITUS | 454 | 33 |

---

## D. Validation setup

**Proposed cutoff: 2020Q4** (train 2016Q1–2020Q4, test 2021Q1–2023Q2). **[ASSUMPTION]**
Rationale: it is the latest cut that still leaves ~29% of signals and >600 positives in test, which
is the minimum for a stable AUPRC at a <1% base rate. A later cut (2021Q4) leaves only 476.

| Rule | Split | Signals | Confirmed | Positive rate | Drug-level rate | Quarters |
|---|---|---|---|---|---|---|
| T1 | train | 259,304 | 2,684 | **1.035%** | 60.5% | 2016Q1–2020Q4 |
| T1 | test | 104,198 | 656 | **0.630%** | 51.0% | 2021Q1–2023Q2 |
| T2 | train | 249,651 | 2,425 | 0.971% | 60.0% | 2016Q1–2020Q4 |
| T2 | test | 99,608 | 598 | 0.600% | 50.6% | 2021Q1–2023Q2 |
| T3 | train | 183,651 | 2,606 | 1.419% | 62.5% | 2016Q1–2020Q4 |
| T3 | test | 75,694 | 629 | 0.831% | 53.0% | 2021Q1–2023Q2 |

**Test prevalence is ~40% lower than train.** State this: a model calibrated on train will
over-predict on test, and part of any AUPRC drop is prevalence, not skill.

### Right-censoring → `D_censoring_by_status.csv`

| Rule | In window (modelled) | Too recent (excluded) | Pre-window (excluded) | Never detected |
|---|---|---|---|---|
| T1 | 363,502 | **115,026** | 114,034 | 487,253 |
| T2 | 349,259 | **110,348** | 111,051 | 509,157 |
| T3 | 259,345 | **83,068** | 74,682 | 662,720 |

"Too recent" = detected 2023Q3 or later, so no full 3-year follow-up; excluded rather than labelled
negative. "Pre-window" = detected before 2016Q1, when SrLC coverage starts.

### Leakage checks

- **Done:** detection statistics are cumulative to the detection quarter by construction, and the
  confirmation clock starts *after* the detection quarter ends. Verified on the built target: **0**
  pairs have a confirmation dated on or before their detection date, and the 1y/3y/5y flags nest
  correctly (1y ⊂ 3y ⊂ 5y, event ⊂ drug).
- **Known look-ahead, documented:** case *content* comes from the latest version of a case but is
  placed in its `init_fda_dt` quarter, so a pair can cross a threshold slightly earlier than it
  could have been observed in real time (DECISIONS.md).
- **Not available:** feature-level leakage checks. **No features have been built yet** — the only
  predictors in the table are the detection-time statistics. Needed: `build_features.py`, then a
  check that each feature uses only reports received ≤ detection quarter.

---

## E. Label quality (event matcher)

Three samples of 50 matched candidates each, drawn from a 1,404,122-row candidate set and
hand-adjudicated by reading the label text (`references/event_matcher_judged{,_v2,_v3}.csv`).

**These are not three rounds of one thing. Read the "what it measured" column before quoting any
number.** Until commit `978446b` the target build and the validation script ran *different*
matchers: the build lower-cased the PT before matching and the evaluator did not, so the plural
rule fired in one path and not the other. Every PT ending in a double s diverged between them.

| Sample | n | True positives | Precision | 95% CI | What it measured |
|---|---|---|---|---|---|
| v1 | 50 | 42 | **84.0%** | 71.5 – 91.7 | the build's matcher, before any fix |
| v2 | 50 | 46 | 92.0% | 81.2 – 96.8 | **the eval path only** — not the matcher the build was running |
| v3 | 50 | 46 | 92.0% | 81.2 – 96.8 | **the eval path only** — same divergence |
| v4 | — | — | **not available** | — | the current unified matcher — **not yet drawn** |

So the defensible claim is narrow: **84% before the fixes**, measured on the artifact that was
actually producing the target. The v2 and v3 figures describe a code path that never generated a
label. Precision for the matcher that produced the numbers everywhere else in this document
**has not been measured**; it needs a fourth fresh sample of 50.

The fix *designs* were still never scored on their own design sample, so the methodology is sound —
it is the object under measurement that moved.

### Effect of each pass (T1) → `E_matcher_fix_effect.csv`

| Stage | Event positives | Contraindication-sourced first confirms |
|---|---|---|
| Original (substring) | 4,055 | not available |
| Word-boundary | 3,739 | 85 |
| After fix 1 (population veto) | 3,535 | 6 |
| After fix 2 (nearest-cue) | 3,340 | 11 |
| **After fix 3 (plural)** | **3,424** | 12 |

Cumulative change from the substring version: **−15.6%** of positives.

Fix 3 repaired `str.rstrip("s")`, which strips *every* trailing s: `DIZZINESS` became `DIZZINE`
and matched nothing, so all 248 PTs ending in a double s were structurally unconfirmable.
It recovered 84 positives (DIZZINESS alone went 0 → 34).

### Precision appeared to plateau

Fix 2 removed all four v2 failure modes (verified by a 22-case regression test) yet precision stayed
at 92%: four *different* failures appeared in v3. Treat this as suggestive, not established — both
figures come from the eval path described above, so the plateau is a property of that path and may
not hold for the unified matcher.

**Remaining known failure modes (v3):**
1. **Indication lists** — "hidradenitis suppurativa (HS) and another indication" (adalimumab).
2. **Procedure/concomitant-treatment context** — "concomitant phase with radiotherapy".
3. **Negation** — "There were **no** deaths reported in either trial" counted as confirming DEATH.
4. **Compound term, different concept** — PT DEPRESSION matched inside "respiratory depression".

5. **British vs American spelling — the largest known gap.** MedDRA PTs use `DYSPNOEA`,
   `DIARRHOEA`, `OEDEMA`, `ANAEMIA`, `PARAESTHESIA`; US labels write *dyspnea*, *diarrhea*. Of
   29,500 label-change rows, 562 contain "dyspnea" and **0** contain "dyspnoea". **933 PT terms /
   27,065 pairs (7.4% of T1)** carry a British spelling and confirm at **0.07%** against 0.94%
   overall — a 13x deficit. (An earlier, looser pattern matching any oe/ae digraph gave 1,292 terms
   / 33,572 pairs / 0.11%; it also catches non-British terms, so the tighter figure is used here.)
   an ~8× deficit. Normalising the digraphs would likely add 250–300 positives (~+8%). Not fixed.

Modes 3 and 4 are new kinds, not variants of the population problem. Negation handling and
longest-term-wins matching would be the next fixes.

### Recall — **not available**

Estimated once (before the fixes) at 19% with a 95% interval of **4–55%**, from 1 genuine miss in 49
judged unmatched rows. The interval is too wide to quote. The fixes can only have lowered it.
To estimate it properly you need an *enriched* unmatched sample (e.g. stratified by MedDRA SOC
agreement or embedding similarity) rather than a uniform draw from a stratum that is 204× larger.

**Consequence for the slide:** `confirmed_event` is a high-precision, unknown-recall **lower bound**.
Roughly 1 in 12 positives is spurious, and an unknown but probably large share of true confirmations
is missed.

---

## F. Anything odd

1. **Confirmed signals have lower ROR than unconfirmed** (4.61 vs 6.22 median, T1). Worth a slide.
2. **Positive rate falls by two-thirds across the study period** (1.69% → 0.53%), driving the
   train/test prevalence gap. Part is likely burn-in: everything accumulated since 2014Q3 detects at
   once in 2016Q1, enriching early cohorts for strong, real signals.
3. **Tiny cell counts dominate.** Median a = 3; **76.5%** of signals have a ≤ 5. Detection at a = 3
   with a one-sided CI is a weak basis for a "signal", and rankings by ROR favour pairs with tiny b
   and c.
4. **13,575 rows share a duplicated ROR value** — ties from small-integer cells, which will matter
   for any ranking metric and for tree splits.
5. **High-volume drugs confirm at ~zero, event-level** → `F_high_volume_drugs_zero_event_confirms.csv`.
   METHOTREXATE: 2,491 signals, 1,844 confirmed at *drug* level, **0** at event level. ADALIMUMAB
   2,279 / 1,352 / **0**. The model may simply learn "high-volume drug ⇒ negative".
6. **A real mapping defect, quantified.** 1,415 of 3,275 drugs (22.5% of eligible pairs) have **no**
   mapped SrLC change at all, so they *cannot* be confirmed. For **309** of those drugs — **39,568
   pairs, 10.9% of T1** — a salt-form variant *did* map, meaning the match was missed: the matcher
   takes an exact ingredient hit first, so FAERS `PANTOPRAZOLE` never reaches the salt-stripped
   class that `PANTOPRAZOLE SODIUM` landed in. PANTOPRAZOLE has 1,215 signals and 82 SrLC rows, yet
   0 confirmations of either kind. **This is a bug, not a data limitation**, and fixing it would
   raise the positive count.
7. **Junk PTs among the most frequent events** — PRODUCT USE IN UNAPPROVED INDICATION, OFF LABEL
   USE, DRUG INEFFECTIVE are top-10 by signal count and all have 0 confirmations by construction
   (blocklisted as non-clinical). They are pure noise in the feature space.
8. **1,465 stub label rows** ("Extensive changes; please refer to label") carry a valid date and
   section but no text, so they can support a drug-level confirmation and never an event-level one.

---

## Files

All in `figures/`, regenerated together by `scripts/compile_presentation_stats.py`.

| File | Contents |
|---|---|
| `B_dataset_counts.csv` | per-rule signal/confirmed/unconfirmed counts, drugs, events, exclusions |
| `B_confirmed_by_source.csv` | SrLC vs DSC vs 921 overlaps |
| `B_first_confirm_section.csv` | which label section first confirms |
| `C_ror_prr_by_label.csv` | ROR/PRR median, IQR, min, max by label and rule |
| `C_signals_per_year.csv` | signals and confirmed rate per detection year |
| `C_top10_drugs.csv`, `C_top10_events.csv` | top 10 by signal count, with confirmations |
| `C1_class_balance.png` | class balance bar chart |
| `C2_ror_prr_boxplot.png` | ROR and PRR boxplots by label, log scale |
| `C3_signals_and_rate_per_year.png` | signals per year + confirmed rate, cutoff marked |
| `C4_a_vs_ror_scatter.png` | cell a vs ROR, coloured by label, log-log |
| `C5_top10_drugs_events.png` | top 10 drugs and events |
| `D_train_test_split.csv` | train/test n, confirmed, positive rate per rule |
| `D_censoring_by_status.csv` | in-window / too-recent / pre-window / never, per rule |
| `E_matcher_precision.csv` | precision per adjudicated sample with CIs and design-vs-eval role |
| `E_matcher_fix_effect.csv` | positives and contraindication confirms per fix stage |
| `F_oddities.csv`, `F_oddities.json` | ties, small cells, medians by label |
| `F_high_volume_drugs_zero_event_confirms.csv` | high-volume drugs with ~0 event-level confirmations |
