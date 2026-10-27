# Decisions log

FAERS signal confirmation classifier. Evidence: `references/faers_data_dictionary.md` (profile of all 55 quarters, 2012Q4-2026Q2), `references/spot_checks_6_7_8.md` (raw-row spot checks), `references/sanity_check_1.csv` and `sanity_check_2.csv` (demographic rules vs raw rows), `references/profile/case_table_build_report.json` (build counts). Last updated 2026-10-07.

## Decisions

### Case set
| # | Topic | Decision |
|---|---|---|
| 1 | Case version | One row per `caseid`: the latest `caseversion`. Drugs, reactions and outcomes come from that version only. If a `primaryid` is published in two quarters, the later quarter file is the kept version. |
| 2 | Quarter cutoff and counting universe | The kept version's file quarter must be 2014Q3 or later (first quarter with `prod_ai`); 2012Q4-2014Q2 stay loaded but are excluded. **Counting universe (2026-10-07):** drug-event counts use only cases with `init_quarter >= 2014Q3`. Cases first received earlier (157,383 cases, `init_quarter` 1986Q4-2014Q2, whose latest version sits in a 2014Q3+ file) stay in the processed files for reference but are flagged `in_counting_universe = 0` and are **dropped from all counts** (earlier decision to keep them with their original `init_fda_dt` is superseded). Universe: 16,015,455 cases, 46,251,580 link rows, 6,683 drugs, 23,279 event terms, 2,686,044 distinct drug-event pairs, 48 quarters (2014Q3-2026Q2). Detection starts at **2016Q1**, after a burn-in of 2014Q3-2015Q4 (6 quarters, 18 months), so cumulative counts at the first detection quarter already hold 18 months of data. Because every universe case was first received in 2014Q3 or later, the file-quarter cutoff is now implied (0 universe cases have a kept version before 2014Q3). |
| 3 | Deleted cases | The `deleted/` / `Deleted/` lists (2019Q1 onward, 31 files, one id per line) are matched on `caseid`; matching cases are removed. Unambiguous: 0 ids match a `primaryid`, 98,102 match a `caseid`. |
| 4 | Joins | Child tables (DRUG, REAC, OUTC) join on (`primaryid`, quarter of the kept version), never on `primaryid` alone. 707 `primaryid`s exist in two quarters, so a `primaryid`-only join adds rows from the older copy (627 spurious PS rows in the spot check). |
| 5 | Time axis | A case belongs to the quarter of its `init_fda_dt` (`init_quarter`; 0 invalid dates). Detection date = first quarter a drug-event pair crosses the threshold, counting only cases with `init_fda_dt` on or before that quarter. |
| 6 | Encoding | UTF-8 with a latin-1 fallback (per line, only when a file is not valid UTF-8). Applied to the DuckDB copy: 38 files with non-ASCII bytes were reloaded, no mojibake remains. |
| 7 | Duplicates | Drop exact duplicate rows; count `distinct caseid` per drug-event pair. |

### Drug and event
| # | Topic | Decision |
|---|---|---|
| 8 | Drug role | `role_cod` = `PS` only. |
| 9 | Drug identity | `drug_ai` = `prod_ai` trimmed, uppercased, whitespace collapsed. If blank, the same cleaning of `drugname` with `ai_source = "drugname_fallback"` (else `"prod_ai"`). One distinct string = one drug. No brand-to-generic mapping, no DiAna. |
| 10 | Combinations | `is_combo` = 1 when `drug_ai` contains a backslash (the file's multi-ingredient separator). Not split. A comma/semicolon test is reported only as a secondary note: commas are mostly part of one ingredient (`ESTROGENS, CONJUGATED`). |
| 11 | Event term | `REAC.pt` trimmed and uppercased as `pt_clean` for matching, original `pt` kept. Repeated (`primaryid`, `pt`) pairs dropped. No MedDRA rollup. |
| 12 | Outcomes | OUTC deduplicated on (`primaryid`, `outc_cod`), pivoted to 0/1 columns DE, HO, LT, DS, CA, RI, OT, joined on the kept (`primaryid`, quarter). No OUTC row -> 0 everywhere ("no serious outcome recorded", not "non-serious"). `has_outcome` = has any OUTC row; it carries no severity information. Not aggregated to pair level here (feature step, using only cases received on or before the detection date). |
| 13 | `dechal` / `rechal` | Excluded. Fill rates flip around 2015Q4 (through 2015Q3 `dechal` ~3% filled, `rechal` ~90%; from 2015Q4 `rechal` ~20%, `dechal` 50-60%) and the docs do not explain it, so the meaning is not stable across years. |

### Demographics and reporter
| # | Topic | Decision |
|---|---|---|
| 14 | Age | `age` -> number (non-numeric -> missing), converted to years with `age_cod` (DEC x10, YR x1, MON /12, WK x7/365.25, DY /365.25, HR /8766, MIN, SEC); outside 0-120 years -> missing. A number with a blank/unknown unit is missing. (The instruction read "between 0 to 120 as missing"; applied as *outside*.) |
| 15 | Sex | `gndr_cod` coalesced into `sex`; F, M kept; NS, P, I, T, UNK, blank -> UNK. |
| 16 | Country | `reporter_country` trimmed, uppercased; `COUNTRY NOT SPECIFIED`, `EU`, blank -> missing. `us_flag` = 1 for US, 0 for any other country, missing stays missing. (Blank never occurs in the data; `COUNTRY NOT SPECIFIED` is the placeholder.) |
| 17 | Reporter group | From `occp_cod`: hcp (MD, HP, PH, RN), consumer (CN), lawyer (LW), other (OT, SALES), missing (blank); one 0/1 column per group (they sum to 1). |
| 18 | Weight | Dropped. |

All demographic rules were verified against raw lines by separate code (sanity_check_2.csv: 67 targeted rows, 0 mismatches; case table: 150 random cases, 0 mismatches).

### Pair counts, statistics and detection (steps 5-7, 2026-10-07)
| # | Topic | Decision |
|---|---|---|
| 19 | Counting universe | Distinct cases in `ps_drug_event_cases` with `init_quarter >= 2014Q3` (removes 750,524 link rows and 157,381 cases; the case table only contains cases whose latest version is 2014Q3+, so earlier quarters are a biased subset). Quarters t = 2014Q3..2026Q2; counts for t use only cases with `init_quarter <= t`. N = all universe cases as of t (16,013,635 at 2026Q2; this is the link-table case count, 1,820 fewer than the case table because cases without a PS drug are not counted). |
| 20 | Pair table | A case counts once per (`drug_ai`, `pt_clean`). A pair enters `pair_quarter_counts` from the first quarter its cumulative a >= 3 and stays in every later quarter (even without new reports, because b, c, d change as N grows). `is_combo` kept as a flag, combinations not split. b = N_drug - a, c = N_event - a, d = N - N_drug - N_event + a. |
| 21 | Zero cells | Haldane-Anscombe: for rows with a zero in b, c or d, add 0.5 to all four cells before ROR, CI, PRR and chi2; flag `cell_corrected`. 5,998 of 29,560,557 rows (b = 0: 1,581; c = 0: 4,420; d = 0: none). |
| 22 | Statistics | ROR with 95% Wald CI on the log scale (`ror_lcl`, `ror_ucl`); PRR; Pearson chi2 without continuity correction; one-sided Wald p-value for ROR > 1; Benjamini-Hochberg q_value within each quarter across all pairs in that quarter's table (pairs with cumulative a >= 3 - not every possible pair - ties use the largest rank). |
| 23 | Detection thresholds (all three kept until one is chosen) | **T1** a >= 3 and `ror_lcl` > 1; **T2** (Evans) a >= 3, PRR >= 2, chi2 >= 4; **T3** a >= 5, `ror_lcl` > 1, `q_value` < 0.05. `detection_quarter` = first quarter t the rule is met. |
| 24 | Detection status | Detected before 2016Q1 -> `pre_window` (excluded from modeling, not reassigned); 2016Q1-2023Q2 -> `in_window`; 2023Q3 or later -> `too_recent` (no full 3-year label window); never -> `never`. Detection starts at 2016Q1 after 18 months of burn-in. |

## Outputs

`data/processed/case_table.parquet` (16,172,838 cases) and `data/processed/ps_drug_event_cases.parquet` (47,002,104 rows), built by `scripts/build_case_table.py`. Both carry `in_counting_universe` (1 when `init_quarter >= 2014Q3`); filter on it for any count. Columns: `references/case_tables_data_dictionary.md`.

Pair-level outputs (`scripts/build_pair_stats.py`): `pair_quarter_counts.parquet` (29,560,557 rows), `pair_quarter_stats.parquet` (same rows plus statistics), `detected_pairs_T1/T2/T3.parquet` (1,079,815 pairs each). Checks and samples: `references/sanity_check_3.md`.

### SrLC label-text parse and confirmation target (steps 8-9, 2026-10-26)
| # | Topic | Decision |
|---|---|---|
| 25 | SrLC parse unit | One row per (DrugNameID, approval date, supplement, changed label section): 29,500 rows from all 3,262 pages, 11,102 distinct approved changes, 2014-12-19 to 2026-10-06. A page carries a drug's whole change history, so pages are not rows. |
| 26 | What counts as a section | Only `<h4>` elements that are direct children of the accordion content div. FDA also uses `<h4>` for sub-headings *inside* a section body (e.g. "Risk Summary"), which would otherwise split one change into several rows and truncate the first. Fixing this removed 60 spurious rows and 8 of 10 empty-text rows. |
| 27 | Section codes | Mapped to FDA's own legend on the page: BW, WP, AR, DI, USP, PCI/PI/MG, CONTRA, plus OTHER. All 29,500 rows code; no uncoded bucket. |
| 28 | Stub rows | 1,307 rows read "Extensive changes; please refer to label for complete information." FDA writes this instead of reproducing the change. Date and section are valid, the text is absent; flagged `is_stub`, so they can support a drug-level confirmation but never an event-level one. |
| 29 | Confirmation clock | Starts at the END of the detection quarter (2018Q2 -> 2018-06-30), since a signal is only knowable once the quarter closes. Windows of 1, 2, 3 and 5 years are all computed; 3 years is primary. |
| 30 | Eligible pairs | `status == 'in_window'` only (detected 2016Q1-2023Q2). Earlier pairs predate SrLC coverage; later ones cannot be followed a full 3 years, so counting them as negatives would mislabel right-censored signals. |
| 31 | Safety-related change | SrLC section code in BW / WP / AR / CONTRA. PCI/PI/MG, dosing, indications and the rest are excluded as not risk-bearing. 22,027 of 41,200 drug-mapped rows qualify. |
| 32 | Two targets, both kept | `confirmed_drug` (the drug got a risk-section change in the window, any event) and `confirmed_event` (as above AND the event term appears in the changed text). They bracket the truth: 57.8% vs 1.0% at 3 years on T1. Neither is "the" answer; the modeling step chooses, and the gap is the main threat to validity. |
| 33 | Event matching | Case-insensitive word-boundary match of the MedDRA PT in the label text, singular or plural. A plain substring test was tried first and rejected: it matched TENSION inside "hypertension". Word boundaries cut T1 event positives from 4,055 to 3,739 (-7.8%). |
| 34 | Drug matching | Reuses `match_srlc_ingredients.py` (exact / salt_stripped / component_any). 41,200 drug-mapped rows over 2,088 FAERS drugs. A strict-only variant (exact + salt_stripped) is written alongside every primary flag. |
| 35 | 921 and DSC | Drug-level supplementary flags only, not folded into the primary target. DSC titles in the index are truncated, so DSC drug matching is name-in-title/URL and is weaker than the SrLC match. |

## Limitations (for the report)

- `prod_ai` is FDA-supplied and can contain combination products (7.3% of link rows) and spelling variants of one ingredient; each distinct string is one drug, so a combination and its components are different "drugs".
- The `drugname` fallback affects only 122 case-drug pairs (mostly `Samsca` and `PLACEBO` in 2017Q2).
- MedDRA version is not recorded in the files and changed over the study period; `pt` is used without rollup. Uppercasing merges only one pair of terms.
- "No serious outcome recorded" is not "non-serious": 7.2M of the retained cases had no OUTC row. Using the latest version only resets 17,512 cases that had an outcome in an older version to 0.
- Case content comes from the latest version but is placed in the `init_fda_dt` quarter, so a pair can appear to cross the threshold earlier than it could have been observed (look-ahead).
- Counting universe starts at 2014Q3: 1,418,268 cases with a latest version in 2012Q4-2014Q2 and 157,383 cases first received before 2014Q3 are excluded from counts, and 92,758 cases are removed as deleted. Detection starts at 2016Q1, so the first 6 quarters are burn-in only and no pair can be detected before 2016Q1.
- Cases first received before 2014Q3 may have follow-up versions in the universe period; their reports are not counted, so counts for long-marketed drugs are lower than the true cumulative reporting history (the cumulative 2x2 tables start from zero at 2014Q3).
- Age is missing for 43% of cases; `reporter_country` for 4.5% (placeholders).

- The confirmation target is bracketed, not pinned down. `confirmed_drug` at 3 years is 57.8% of T1 pairs, which is close to "did this drug change its label at all"; `confirmed_event` is 1.0%, because label prose rarely uses the MedDRA PT verbatim. The true confirmation rate lies between, and the choice of target will dominate any AUROC reported.
- Event matching is lexical, not semantic. A label that adds "severe hepatic injury" does not match the PT LIVER INJURY, and no MedDRA synonym or PT-to-HLT rollup is applied. `confirmed_event` is therefore a lower bound.
- SrLC covers 2016 onward (plus 4 stray 2014 rows), so a pair detected in 2016Q1 can only be confirmed by a change recorded from 2016 on. Earlier regulatory action is invisible to this target.
- At detection, median ROR is slightly *lower* for confirmed pairs (4.21) than unconfirmed (5.04) on T3. Disproportionality alone does not separate the classes, which is the point of the modeling step but also a warning that a weak model is a plausible outcome.
- The stricter detection rule enriches for confirmation: T3 confirms at 1.39% versus T1 at 1.03%, on 29% fewer pairs. This is the first evidence that the detection rules differ in a way that matters.

- Pair statistics are computed on cases with a PS drug and use cumulative counts from zero at 2014Q3, so pairs whose reports mostly predate 2014Q3 look weaker than they are. Counts per pair can be dominated by a few bulk submissions (e.g. ADAPALENE / DRY SKIN has 46,649 cases, 79% of them reported in 2018Q2-2020Q2; ADAPALENE as a whole has 184,206).
- A detected pair is not a confirmed signal: with thresholds this lenient 33% (T1) of all pairs ever reaching a >= 3 are `in_window`, so base rates in the label step will matter. Rankings by `ror_lcl` at detection favor pairs with tiny b and c (a = 3 to 13) in a large N.

## Cleanup

- **Done 2026-10-07:** removed 498 unused files (61 MB) from `data/raw/` (per-table PDFs, Readme/FAQs, `.DS_Store`/`Thumbs.db`) and the 165 RPSR / THER / INDI table files (3.7 GB; copies are on the owner's Google Drive and the case-table build never used them). Kept in `data/raw/`: the DEMO / DRUG / REAC / OUTC tables, the `deleted/` lists and the 55 ASC_NTS documents (source of the field meanings). The DuckDB copy still contains RPSR / THER / INDI, but a rebuild from `data/raw/` would not.
- **Pending:** delete `data/interim/faers_raw.duckdb` and `faers_raw.filescan.json` (6.6 GB) only after the pipeline has run end to end and the final Parquet outputs are verified.
