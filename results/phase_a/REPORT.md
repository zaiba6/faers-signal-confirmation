# Phase A: reconnaissance report

All numbers come from `scripts/phase_a_recon.py` and are saved in `results/phase_a/phase_a.json`
and `results/phase_a/a2_budget_T{1,2,3}.csv`. Nothing in the modeling pipeline was changed.

Provenance: commit, package versions and SHA-256 checksums of every input parquet are recorded in
the `provenance` block of `phase_a.json`.

## A1. How are pre-2016 detections labelled?

They are not labelled at all. They are excluded from the cohort before labelling, as `pre_window`.

| Rule | pre_window (2014-2015) | in_window (2016Q1-2023Q2) | too_recent |
|---|---|---|---|
| T1 | 114,034 | 363,502 | 115,026 |
| T2 | 111,051 | 349,259 | 110,348 |
| T3 | 74,682 | 259,345 | 83,068 |

T1 pre_window splits 44,503 in 2014 and 69,531 in 2015.

Why this matters: the SrLC table holds 29,500 label changes running 2014-12-19 to 2026-10-06, but
only **4 rows** predate 2016-01-01. A pair detected in 2014 or 2015 whose label was changed before
2016 would look unconfirmed, because the evidence of the change is not in our data. The current
design already avoids that trap by excluding those pairs rather than labelling them negative.

MedWatch archive changes were **not ingested**. No file matching `*medwatch*` exists under
`data/raw/labels/`. The other two label sources run wider than SrLC: Section 921 covers 2009Q4
onward and Drug Safety Communications cover 2016-01-04 to 2026-09-15, but neither is folded into
the primary event-level label.

Proposed fix, in preference order:

1. **Keep the cohort starting at 2016Q1** (current behaviour). Costs 114,034 detections, about 24
   percent of ever-detected pairs. No new data collection. The limitation is already documented.
2. Ingest MedWatch archive label changes for 2014-2015 and extend the cohort back. This adds a new
   source with its own parsing and matching work, and MedWatch summaries are less structured than
   SrLC, so the matcher would need separate validation.

Recommendation: option 1, stated as a limitation. The 2014-2015 window is burn-in for the
cumulative counts anyway, so those pairs have the least reliable statistics in the dataset.

## A2. Data budget at horizons 12, 24 and 36 months

Full per-year tables are in `a2_budget_T{1,2,3}.csv`. T1 summary:

| H | Followable signals | Positives | Prevalence | Last fully followable detection year |
|---|---|---|---|---|
| 12 months | 556,317 | 1,858 | 0.334% | 2025 (partial) |
| 24 months | 518,063 | 3,219 | 0.621% | 2024 (partial) |
| 36 months | 477,536 | 4,285 | 0.897% | 2023 (partial) |

**Important caveat on this table.** The `positives` column is only a measurement for detection
years 2016 through 2023. The confirmation target was computed only for `in_window` pairs, so 2014,
2015 and 2024 onward show zero positives because nothing was computed for them, not because nothing
was confirmed. The `signals`, `followable` and `too_recent` columns are correct for every year.

What that means for the usable window, with FAERS data ending 2026Q2 and SrLC to 2026-10-06:

- At H = 36 the last fully followable detection quarter is **2023Q2**, which is the current cohort end.
- At H = 24 the cohort could extend to **2024Q2**, adding roughly 20,000 more signals per year.
- At H = 12 it could extend to **2025Q2**, adding roughly 40,000 per year.

Shortening H buys more recent signals and more total rows, but cuts the positive count and the
prevalence by a lot: 4,285 positives at 36 months against 1,858 at 12 months. With a cluster
bootstrap and fold-level metrics both wanted, positive count is the binding constraint.

Prevalence falls steadily by detection year at every horizon (H = 36: 2.158% in 2016 down to 0.668%
in 2021), so any train and test split will have different prevalence on each side.

Distinct ingredients per detection year rises from 1,916 in 2016 to 2,413 in 2023.

## A3. Is there an ingredient or drug id per row?

There is a drug identifier, but it is a **name string, not a stable id**.

- Column: `drug_ai`, derived from FAERS `prod_ai`, uppercased and whitespace collapsed.
- 3,925 distinct values across all detected pairs; 3,275 in the current cohort.
- 108,820 of 1,079,815 rows are combination products (`is_combo` = 1), identified by a backslash.
- No external identifier column exists anywhere in the processed tables: no RxCUI, UNII, NDC,
  application number or DailyMed set id.
- Pairs per ingredient in the cohort: median 37, mean 111, max 2,491.

This is enough for a cluster bootstrap and for ingredient-grouped splits, which only need a grouping
key. It is **not** enough for joining to DailyMed, which needs set ids, so Phase B needs a name to
set id mapping step and that mapping is where failures will concentrate. Salt variants remain
separate ingredients (PANTOPRAZOLE and PANTOPRAZOLE SODIUM are two groups), which will understate
cluster sizes slightly.

## A4. Was the test period used in any tuning or selection decision?

No evidence that it was, on the checks available.

- `scripts/fit_models.py` sets `SPLIT, CV_CUTS = "2020Q4", [("2017Q4","2018"), ("2018Q4","2019"), ("2019Q4","2020")]`.
  Every cross-validation fold ends in 2020 or earlier, inside train.
- A grep for the test matrices appearing in any `fit`, `eval_set` or cross-validation call returns nothing.
- No notebooks exist in the repository, so no untracked exploratory tuning is hidden there.
- No commit message mentions tuning against the test period.

Two things to disclose honestly even so:

1. The n_estimators grid was changed once after seeing that early stopping had selected 1 tree. That
   decision was made on the cross-validation folds, not on test, but it was made after test numbers
   had been printed in the session, so a reviewer could call it informal contamination.
2. There is currently **no lock-box**. The test period has been scored more than once across the
   rebuilds this project has been through. It should be treated as a development test set, and a
   genuinely untouched lock-box should be carved out in Phase C.

## A5. DailyMed feasibility

Network access works from this environment. All probes returned HTTP 200.

| Probe | Result |
|---|---|
| `services/v2/spls.json?drug_name=PANTOPRAZOLE` | 200 in 0.72 s, 5 set ids returned |
| `services/v2/spls/{SETID}/history.json` x 5 | 200, 0.15 to 0.28 s each, 1 to 15 versions per set id |
| `getFile.cfm?setid=...&type=zip&version=1` | 200, 771,405 bytes, SPL effectiveTime **20160322** |
| `getFile.cfm?setid=...&type=zip&version=15` | 200, 125,169 bytes, SPL effectiveTime **20261026** |
| `downloadzipfile.cfm?setid=...&version=1` | 200, but returns the **current** label, ignoring version |

Three findings that matter for Phase B:

1. **Historical versions are retrievable**, which is the thing Phase B depends on. Use
   `getFile.cfm?setid=...&type=zip&version=N`. Confirmed by reading `<effectiveTime>` out of the SPL
   xml inside each zip: version 1 gives a 2016 label, version 15 gives a 2026 label.
2. **`downloadzipfile.cfm` silently ignores the version parameter** and returns the current label.
   Using it would have quietly given every historical lookup today's label, which is exactly the
   leakage Phase B exists to prevent. This is the single most important finding of Phase A.
3. History responses are ordered **newest first**, so `history[0]` is the latest version and
   `history[-1]` is version 1.

Speed and limits: responses came back in 0.15 to 0.7 seconds. No rate limit headers are returned, so
there is no published budget to read. A conservative 1 request per second was used here and should
be kept. Section headings present in the retrieved SPL xml include Contraindications, Warnings and
Precautions, Adverse Reactions, and the legacy Warnings and Precautions headings, which supports the
Phase B parsing plan.

One probe zip is cached at `data/raw/dailymed/_probe/` with its checksum recorded.

## Scale estimate for Phase B

3,275 ingredients in the cohort. If an ingredient maps to a handful of set ids and each needs one
history call plus one zip per distinct detection quarter, the request count runs to tens of
thousands. At 1 request per second that is hours, not minutes, and it should be cached and
resumable in the same style as the SrLC scrape.
