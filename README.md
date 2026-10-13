# FAERS Signal Confirmation Classifier

Will a statistical drug safety signal turn into a real FDA label change, or fade into noise?

This project builds a supervised ML classifier that predicts whether a drug-adverse event signal detected in FDA adverse event reports (FAERS) will later be **confirmed** by an FDA label change or Drug Safety Communication (DSC), versus staying an unconfirmed signal. Built for an applied statistics course project.

> **Status:** raw FAERS data profiled and cleaned into a case table and a PS drug-event link table (2014Q3-2026Q2). Next: disproportionality (ROR/PRR + FDR), labels, features, models. Results, app link, and report will be added as the project moves forward.

## Motivation

Spontaneous adverse event databases produce a flood of statistical signals, and most never lead to regulatory action. Prior work has often used logistic regression without temporal validation or AUROC reporting, and no free public tool combines FAERS disproportionality features, supervised ML, and a time-based split to predict FDA regulatory confirmation. This project aims to fill that gap.

## Research question

Using only information available at the time a signal is first detected, can we predict whether it will be confirmed by an FDA label change or DSC within a fixed follow-up window?

## Approach

1. **Detect signals** with disproportionality analysis (ROR and PRR with FDR correction), computed quarter by quarter using only data received up to that quarter.
2. **Label outcomes** using FDA Section 921 postings, the Safety-related Labeling Changes (SrLC) database, Drug Safety Communications, and DailyMed label version history.
3. **Engineer features** frozen at the detection date to avoid leakage.
4. **Model** with a logistic regression baseline, then XGBoost / LightGBM.
5. **Validate** with a temporal train/test split by detection date.
6. **Evaluate** with AUROC and AUPRC, and explain predictions with SHAP.

## Data sources

| Purpose | Source |
|---|---|
| Adverse event reports | [FAERS / AEMS quarterly extract files](https://fis.fda.gov/extensions/FPD-QDE-FAERS/FPD-QDE-FAERS.html) |
| Drug names | FAERS `prod_ai` (FDA-supplied active ingredient), used as is; no brand-to-generic mapping for now (see `DECISIONS.md`) |
| Confirmation labels | [FDA Section 921 potential signals](https://www.fda.gov/drugs/fdas-adverse-event-reporting-system-faers/potential-signals-serious-risksnew-safety-information-identified-fda-adverse-event-reporting-system), [SrLC database](https://www.accessdata.fda.gov/scripts/cder/safetylabelingchanges/index.cfm), [Drug Safety Communications](https://www.fda.gov/drugs/drug-safety-and-availability/drug-safety-communications) |
| Label dates | [DailyMed SPL history](https://dailymed.nlm.nih.gov/dailymed/app-support-web-services.cfm), [Drugs@FDA](https://www.fda.gov/drugs/drug-approvals-and-databases/drugsfda-data-files) |

See [`DATA_PLAN.md`](DATA_PLAN.md) for the full data breakdown, action items, and known pitfalls, and [`DECISIONS.md`](DECISIONS.md) for every data-cleaning decision with its evidence.

## Methods at a glance

- **Disproportionality:** Reporting Odds Ratio (ROR), Proportional Reporting Ratio (PRR), FDR correction
- **Models:** logistic regression (baseline), XGBoost, LightGBM
- **Validation:** temporal split by detection date, fixed confirmation window to handle right-censoring
- **Metrics:** AUROC, AUPRC
- **Interpretability:** SHAP feature importance

## Repository structure

```
faers-signal-confirmation/
    README.md
    DATA_PLAN.md        # plan, action items, pitfalls
    DECISIONS.md        # every data decision, limitations, cleanup status
    data/
        raw/            # FAERS quarterly files 2012Q4-2026Q2 (not committed)
        interim/        # local DuckDB copy of the raw tables (not committed, rebuildable)
        processed/      # case_table.parquet, ps_drug_event_cases.parquet
    scripts/
        profile_raw_data.py   # inventory, schema comparison, per-column profile, DuckDB load
        spot_checks.py        # raw-row sanity checks
        build_case_table.py   # builds the two processed tables
        clean_demo.py         # early demographic cleaning prototype (superseded by build_case_table.py)
    references/         # data dictionaries, spot checks, profile outputs
    notebooks/          # EDA and modeling
    app/                # interactive app
    figures/
    report/
    slides/
```

## Getting started

```bash
git clone https://github.com/<your-username>/faers-signal-confirmation.git
cd faers-signal-confirmation
python -m venv .venv && source .venv/bin/activate
pip install duckdb numpy pandas scipy
```

Raw FAERS files are large and are not stored in this repo. Download the quarterly ASCII files from the link above into `data/raw/` (one `faers_ascii_YYYYqQ` folder per quarter), then:

```bash
python scripts/profile_raw_data.py        # loads every table into data/interim/faers_raw.duckdb and profiles it (30+ minutes)
python scripts/build_case_table.py        # writes data/processed/case_table.parquet and ps_drug_event_cases.parquet
```

Output columns are described in `references/case_tables_data_dictionary.md`.

## Interactive app

An interactive app for exploring the data and results will live in `app/`.

*Link and run instructions coming soon.*

## Deliverables roadmap

- [x] Project proposal
- [x] Data collection and cleaning (case table and PS drug-event link table built; see `DECISIONS.md`)
- [ ] EDA and baseline model
- [ ] Advanced modeling and validation
- [ ] Interactive app
- [ ] Final report
- [ ] Presentation and reflection

## Limitations

FAERS is a spontaneous reporting system, so reports do not prove causation and reporting rates are affected by media attention, drug age, and reporter behavior. Signals confirmed by FDA are a proxy for "true" safety signals, not ground truth. Results should be read as a prioritization aid, not a clinical conclusion.

## References

Reference list will be added with the final report.

## Author

Zaiba

Built for an applied statistics course project.