# Data dictionary: case table and PS drug-event link table

Counting universe: only cases with `init_quarter >= 2014Q3` are counted (flag `in_counting_universe`). Detection starts at 2016Q1 after an 18-month burn-in (2014Q3-2015Q4).

Built by `scripts/build_case_table.py` from the raw FAERS tables, applying every rule in `DECISIONS.md`. Build counts and checks: `references/profile/case_table_build_report.json`.

## `data/processed/case_table.parquet` - one row per case (16,172,838 rows, `caseid` unique)

Case set: latest `caseversion` per `caseid`, kept version in a file from 2014Q3 or later, deleted cases removed. Cases with no OUTC row have 0 in every outcome column.

| Column | Type | Meaning |
|---|---|---|
| caseid | string | FAERS case id (key) |
| primaryid | string | primaryid of the kept (latest) version; child tables are joined on (`primaryid`, `source_quarter`) |
| source_quarter | string | quarter of the file holding the kept version (2014Q3..2026Q2) |
| caseversion | int | version number of the kept version |
| init_fda_dt | string | date FDA first received the case, YYYYMMDD (0 invalid) |
| init_quarter | string | quarter of `init_fda_dt`, e.g. 2016Q3 (1986Q4..2026Q2); the time axis |
| age_yrs | double | age in years from `age` + `age_cod`; missing if non-numeric, no valid unit, or outside 0-120 (43% missing) |
| sex | string | F, M or UNK (`gndr_cod` coalesced into `sex`; NS, P, I, T, UNK, blank -> UNK) |
| reporter_country | string | trimmed, uppercased; `COUNTRY NOT SPECIFIED`, `EU` and blank -> missing |
| us_flag | tinyint | 1 = US, 0 = other country, missing where country is missing |
| reporter_group | string | hcp (MD, HP, PH, RN), consumer (CN), lawyer (LW), other (OT, SALES), missing (blank) |
| occp_hcp, occp_consumer, occp_lawyer, occp_other, occp_missing | tinyint | one 0/1 column per reporter group (sum to 1) |
| DE, HO, LT, DS, CA, RI, OT | tinyint | outcome flags: death, hospitalization, life-threatening, disability, congenital anomaly, required intervention, other serious. 0 means "no such outcome recorded" |
| has_outcome | tinyint | 1 if the case has any OUTC row. Means only that; not a severity flag, and 0 does not mean "non-serious" |
| in_counting_universe | tinyint | 1 if `init_quarter >= 2014Q3` (16,015,455 cases); 0 for 157,383 older cases kept for reference. **All counts use only rows with 1.** |
| has_ps_drug | tinyint | 1 if the case has at least one PS drug (0 for 1,822 cases) |

Not carried: weight, `dechal`, `rechal`.

## `data/processed/ps_drug_event_cases.parquet` - one row per (caseid, drug_ai, pt_clean) (47,002,104 rows)

PS drugs of the kept version joined to its reactions. A case counts once per drug-event pair.

| Column | Type | Meaning |
|---|---|---|
| caseid | string | FAERS case id |
| primaryid | string | primaryid of the kept version |
| source_quarter | string | file quarter of the kept version |
| init_quarter | string | quarter of `init_fda_dt` (same as in the case table) |
| drug_ai | string | `prod_ai`, trimmed, uppercased, whitespace collapsed; cleaned `drugname` when `prod_ai` is blank. One distinct string = one drug (6,715 distinct) |
| ai_source | string | `prod_ai` or `drugname_fallback` (254 link rows, 122 case-drug pairs) |
| is_combo | tinyint | 1 if `drug_ai` contains a backslash (multi-ingredient product); not split |
| pt_clean | string | `REAC.pt` trimmed and uppercased; the matching key (23,318 distinct) |
| pt | string | original `pt` (one of the originals if several differ only by case) |
| in_counting_universe | tinyint | copied from the case table; filter on 1 for counts (46,251,580 rows; 750,524 rows have 0) |

Cases with no PS drug (1,822) are not in this table. Every other case appears at least once.

## Pair tables (`scripts/build_pair_stats.py`)

Counting universe `init_quarter >= 2014Q3`; quarter t = cases with `init_quarter <= t`; one count per distinct `caseid` per (`drug_ai`, `pt_clean`). A pair appears from the first quarter its cumulative a >= 3.

| File | Rows | Columns |
|---|---|---|
| `pair_quarter_counts.parquet` | 29,560,557 | `drug_ai`, `pt_clean`, `quarter`, `a`, `b`, `c`, `d`, `is_combo` (a = cases with drug and event, b = drug without event, c = event without drug, d = neither; a+b+c+d = N cases as of the quarter) |
| `pair_quarter_stats.parquet` | 29,560,557 | the columns above plus `ror`, `ror_lcl`, `ror_ucl`, `prr`, `chi2`, `p_value` (one-sided), `q_value` (BH within quarter), `cell_corrected` (1 = 0.5 added to all four cells because b, c or d was 0) |
| `detected_pairs_T1/T2/T3.parquet` | 1,079,815 each (every pair that ever reaches a >= 3) | `drug_ai`, `pt_clean`, `is_combo`, `detection_quarter`, `status` (`pre_window`, `in_window`, `too_recent`, `never`), and a, b, c, d, ror, ror_lcl, prr, chi2, q_value at detection (empty for `never`) |

Thresholds: T1 a >= 3 and ror_lcl > 1; T2 a >= 3, PRR >= 2, chi2 >= 4; T3 a >= 5, ror_lcl > 1, q_value < 0.05.
