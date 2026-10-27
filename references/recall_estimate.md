# Event-matcher recall — enriched estimate (2026-10-27)

Produced by `scripts/eval_event_recall.py` against the matcher at commit `1699ab8`.
Judgements: `references/recall_judged.csv`. Raw numbers: `references/recall_eval.json`.

## Why the first attempt failed

A uniform draw from the unmatched side of a 1,404,122-row candidate set found 1 true miss in 49
rows. Extrapolated over a stratum 200× larger, that gave recall 19% with a 95% interval of 4–55% —
too wide to quote.

## Design

Unmatched candidates were split by how many of the PT's content tokens (4+ letters, stop-words
removed) appear in the label text:

| Stratum | Rule | Size |
|---|---|---|
| matched | the matcher said yes | 6,868 |
| **near** | ≥ half the PT's content tokens present | 69,960 |
| **far** | some present, fewer than half | 47,189 |
| **none** | no content token present | 1,280,105 |

40 / 25 / 25 were sampled from near / far / none, adjudicated by reading the label text, and
reweighted by stratum size. The `none` stratum is not assumed empty — it is sampled, so the
assumption that a confirmation needs *some* shared vocabulary is measured rather than waived.

## Result

| Stratum | Judged | True confirmations | Rate | Implied misses | 95% CI on misses |
|---|---|---|---|---|---|
| near | 40 | 6 | 15.0% | 10,494 | 4,939 – 20,339 |
| far | 25 | 1 | 4.0% | 1,887 | 334 – 9,222 |
| none | 25 | 0 | 0% | 0 | 0 – 170,505 |

**Recall ≈ 32–34%**, depending on which precision is used for the true-positive count
(84% → 31.8%; 92% → 33.8%). 95% interval **3 – 55%**.

## What this does and does not settle

**Settled: the upper bound.** Recall is very unlikely to exceed ~55%. The matcher misses roughly
two-thirds of confirmations. `confirmed_event` is a lower bound, as documented.

**Settled: where the misses are.** 85% of estimated misses sit in `near` — cases where the event's
words *are* in the label text and the matcher still said no. That is a tractable engineering
problem, not a vocabulary problem.

**Not settled: the lower bound.** 0 of 25 in `none` cannot rule out a few percent of 1.28M rows.
More adjudication there, still finding nothing, would tighten it:

| `none` sample | Upper bound on its rate | Recall lower bound |
|---|---|---|
| 25 (current) | 13.3% | 3.1% |
| 100 | 3.7% | 7.6% |
| 300 | 1.3% | 12.1% |
| 1,000 | 0.4% | 15.5% |
| 3,000 | 0.1% | 16.8% |

Diminishing fast: `none` is 91% of the candidate set, so even 3,000 adjudications only lift the
lower bound to ~17%. Quote the point estimate with the upper bound, not the full interval.

## The six near-stratum misses, by cause

Each is a concrete matcher defect, not a vocabulary gap:

1. **Stop-word inside the term.** PT `OSTEONECROSIS OF JAW` vs label "Osteonecrosis of **the** Jaw"
   (SUTENT). Exact-phrase matching breaks on an inserted article.
2. **False population veto.** SYMTUZA / `AUTOIMMUNE HEPATITIS` — the phrase is present verbatim in
   "Autoimmune disorders (such as Graves' disease … and autoimmune hepatitis)", and the veto fired
   anyway. This one is a regression risk introduced by the population fix.
3. **Synonym tail.** `NEUROPSYCHIATRIC SYMPTOMS` vs "neuropsychiatric **events**" (SINGULAIR).
4. **Site subset.** `GASTRIC PERFORATION` vs "gastrointestinal perforation" (RINVOQ).
5. **Hierarchy.** `LUNG DISORDER` vs "interstitial lung disease" (FENOGLIDE) — the label names a
   child of the PT.
6. **Scattered phrase.** `INJECTION SITE EXTRAVASATION` vs "injection site recall reaction … at the
   site of previous extravasation" (TAXOTERE).

Causes 1 and 2 are straightforward fixes. 3–5 need MedDRA structure (synonym lists or a PT→HLT
rollup), which this project does not currently load.

The single `far` miss: LEVEMIR / `WRONG PRODUCT ADMINISTERED` vs "other insulins … accidentally
administered instead of LEVEMIR".

## Caveats

- One non-clinician adjudicator, judging from an evidence window rather than the whole change.
- Precision for the *current* unified matcher is unmeasured (sample 4 is pending), so the
  true-positive count is carried as a sensitivity range rather than a point.
- Stratum sizes are those of commit `1699ab8`. Spelling normalisation and the ingredient-matching
  fix will move the matched/unmatched boundary; `references/recall_judged.csv` keeps `drug_ai`,
  `pt_clean`, change date and section per row so the weights can be recomputed without
  re-adjudicating.
