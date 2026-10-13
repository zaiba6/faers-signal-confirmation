# Data Plan: FAERS Signal Confirmation Classifier

Goal: predict whether a statistically detected drug-adverse event signal in FAERS will later be confirmed by an FDA label change or Drug Safety Communication (DSC).

## The data in plain English

Three ingredients, glued into one row per drug-event pair.

| Ingredient | What it is | Source |
|---|---|---|
| **1. Reports (raw material)** | Who took what drug, what went wrong, when FDA received it | FAERS quarterly ASCII files (DEMO, DRUG, REAC, OUTC tables) |
| **2. Names (translator)** | Maps messy drug names to one active ingredient; events are MedDRA Preferred Terms | FAERS `prod_ai` (FDA-supplied active ingredient); DiAna / RxNorm only if needed later. REAC already holds MedDRA PTs |
| **3. Labels (answer key)** | Did FDA later add this drug-event pair to the label or issue a DSC? | Section 921 postings, SrLC database, DSC archive, DailyMed SPL history |

### Final dataset shape

One row per drug-event pair, frozen at its detection date.

- **Features (X):** report counts, ROR/PRR, FDR-adjusted p-value, report growth rate, serious/death share, reporter type mix. All computed only from data received before detection.
- **Target (y):** 1 if confirmed within the confirmation window (about 3 years), 0 if not.
- **Split column:** detection date, used for the temporal train/test split.

## Data sources

| Purpose | Source | Link |
|---|---|---|
| Raw reports | FAERS / AEMS quarterly extract files | https://fis.fda.gov/extensions/FPD-QDE-FAERS/FPD-QDE-FAERS.html |
| Drug name harmonization | FAERS `prod_ai` (FDA-supplied); openFDA / RxNorm / DiAna only if needed later | https://open.fda.gov/apis/drug/event/ |
| Labels (best ready-made) | FDA Section 921 "Potential Signals of Serious Risks" | https://www.fda.gov/drugs/fdas-adverse-event-reporting-system-faers/potential-signals-serious-risksnew-safety-information-identified-fda-adverse-event-reporting-system |
| Labels (section-level, 2016+) | Drug Safety-related Labeling Changes (SrLC) | https://www.accessdata.fda.gov/scripts/cder/safetylabelingchanges/index.cfm |
| Labels (high severity) | Drug Safety Communications | https://www.fda.gov/drugs/drug-safety-and-availability/drug-safety-communications |
| Exact label dates | DailyMed SPL version history | https://dailymed.nlm.nih.gov/dailymed/app-support-web-services.cfm |
| Label supplement dates | Drugs@FDA data files | https://www.fda.gov/drugs/drug-approvals-and-databases/drugsfda-data-files |

## Action items (in order)

- [x] 1. **Download FAERS quarterly files**, 2012Q4 to 2026Q2 (55 quarters, none missing), into `data/raw/`. The legacy AERS quarters before 2012Q4 have a different structure and are not used.
- [x] 2. **Load and join** the tables on `primaryid` / `caseid`. Always join child tables on (`primaryid`, quarter of the kept version); `primaryid` alone is ambiguous for 707 re-published cases.
- [x] 3. **Deduplicate.** Latest `caseversion` per case, exact duplicate rows dropped, deleted-case lists removed (92,758 cases). No secondary dedup (DiAna / `faers` R package) for now.
- [x] 4. **Normalize drug names.** Use FDA's `prod_ai` (2014Q3 onward), cleaned; `drugname` as fallback. Combination products are flagged (`is_combo`), not split; no brand-to-generic mapping.
- [ ] 5. **Build drug-event counts per quarter** from `ps_drug_event_cases.parquet` (PS drugs only, counted by distinct `caseid`, quarter = `init_quarter`, only rows with `in_counting_universe = 1`, i.e. `init_quarter` 2014Q3 onward; first 6 quarters are burn-in), then cumulative 2x2 tables for each pair.
- [ ] 6. **Compute ROR/PRR + FDR** each quarter using only data received up to that quarter.
- [ ] 7. **Define detection date** as the first quarter a pair crosses the threshold, with a minimum report count, using only cases whose `init_fda_dt` falls on or before that quarter. Detection starts at 2016Q1.
- [ ] 8. **Collect labels.** Pull Section 921 postings, scrape SrLC by date range (2016+), and gather DSCs.
- [ ] 9. **Get exact dates** from DailyMed SPL history and match label changes to drug-event pairs.
- [ ] 10. **Filter out leaks.** Drop pairs already on the label at detection and pairs whose label change came before detection.
- [ ] 11. **Assign the target** using a fixed confirmation window (about 3 years). Drop recent pairs without enough follow-up time.
- [ ] 12. **Engineer features**, frozen at the detection date.
- [ ] 13. **Split by detection date** into train and test. No random shuffling.
- [ ] 14. **Save the final modeling dataset to `data/processed/`** and write its data dictionary. (The case table and link table are already there, with a dictionary in `references/case_tables_data_dictionary.md`.)

## Known pitfalls

- **Duplicates and case versions:** dedup within the data available at time t, not on the final snapshot.
- **Time leakage:** use FDA receipt date (`init_fda_dt`, not event date) for detection, and compute features only from pre-detection data. Case content comes from the latest version, so a pair can look detectable earlier than it really was (see `DECISIONS.md`, limitations).
- **Right-censoring:** recent signals have not had time to be confirmed, so the test set's detection dates must end at least one confirmation window before the data cutoff.
- **Stimulated reporting:** add a feature for whether a DSC or media spike happened before detection.
- **Drug overlap across splits:** keep the same drug out of both train and test, or report results with and without the overlap.