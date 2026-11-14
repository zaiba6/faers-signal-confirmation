# Phase B1 gate report: mapping drug_ai to DailyMed set ids

All numbers from `scripts/phase_b1_map_setids.py` and `scripts/test_dailymed_filters.py`, saved
under `results/phase_b1/`. No label versions were downloaded.

## 0. API filter tests (new)

`scripts/test_dailymed_filters.py` asserts that each parameter the pipeline relies on actually
changes the result set, and exits non-zero if one does not.

| Filter | Relied on | Result |
|---|---|---|
| `spls.json?drug_name` | yes | PASS, 158,914 unfiltered vs 207 for PANTOPRAZOLE |
| `spls.json?application_number` | yes | PASS, NDA020987 returns 9 |
| `spls.json?unii_code` | yes | PASS, UNII D8TST4O562 returns 208 |
| `getFile.cfm?version` | yes | PASS, v1 effectiveTime 20160322, v15 effectiveTime 20261026 |
| `spls.json?marketing_category` | no | CONFIRMED NO-OP, NDA, ANDA and BLA all return 207, same as unfiltered |
| `downloadzipfile.cfm?version` | no | CONFIRMED NO-OP, asked for v1 and got effectiveTime 20261026 |

Both traps return HTTP 200, so anything built on them looks correct while using the wrong data.
Recorded as decisions 62 and 63.

## 1. Crawl size, two scopes

The application-number index holds 16,175 entries: ANDA 11,079, NDA 3,376, BLA 756, NADA 408,
ANADA 285, and a tail of monograph and other codes. Filtering to names starting NDA or BLA, which
excludes ANDA and ANADA, leaves **4,132 applications**.

Set ids per application were estimated from a random sample of 150 applications, reading only
`metadata.total_elements` so no set id lists were fetched.

| Scope | Requests to enumerate | Set ids |
|---|---|---|
| (a) all NDA/BLA set ids | 4,132 list calls, about 1.1 hours at 1 req/sec | about **10,192** (rough 95% interval 8,468 to 11,917) |
| (b) one set id per application, preferring the application holder | 4,132 list calls, same cost to enumerate | **4,132** |

Distribution per application: mean 2.47, median 1, p90 5, max 19, none empty.

Enumeration cost is identical for both scopes, because you must list an application's set ids
before you can choose one. The scopes differ in what Phase B2 then downloads: roughly 10,192
version histories against 4,132. At one history call plus one zip per needed quarter, scope (b) is
about 2.5 times cheaper.

Recommendation: **scope (b)**, one set id per application, preferring the application holder over
repackagers. Repackager labels are copies of the holder's label with a different labeler name in
the title, so they add requests without adding safety text. The holder is identified by preferring
the set id whose title has no bracketed repackager suffix, then the earliest published date.
Scope (a) remains available as a sensitivity run if a drug's coverage looks wrong.

## 2. Combination product rule

A label's safety sections describe one product as a whole, so a pair may only be checked against a
label for the same product.

- A **single-ingredient** `drug_ai` prefers set ids whose label lists that ingredient alone. If it
  appears only alongside other ingredients, the mapping is kept but flagged `combo_fallback`, not
  counted as a clean match.
- A **combination** `drug_ai` (backslash separated, for example `SACUBITRIL\VALSARTAN`) maps only
  to set ids whose label lists **every** component.
- Combinations are never silently merged into a single-ingredient label.

Why: mapping SACUBITRIL\VALSARTAN to a valsartan-only label would answer "was this event already
listed" from the wrong product, and a wrong "yes" removes a pair from the cohort entirely.

Scope: 776 of 3,275 cohort drugs are combinations, carrying 36,530 of 363,502 signals (10.0%).
Recorded as decision 64.

## 3. Coverage (crawl complete, 2026-11-14)

The application-number index returns 4,132 NDA/BLA rows but only **3,737 distinct**
application numbers (395 duplicate rows). All 3,737 were fetched; 54 BLAs failed on a
transient ConnectionError in the first pass and were picked up on the resume. The
resulting universe is **8,292 distinct set ids**.

Coverage is measured as the share of **cohort signals** (T1, 349,242 pairs) whose
`drug_ai` maps to at least one NDA/BLA set id.

| | drugs | signals |
|---|---|---|
| cohort | 3,198 | 349,242 |
| mapped | 2,093 (65.4%) | **305,275 (87.4%)** |

By match level:

| level | drugs | signals |
|---|---|---|
| single_exact | 1,629 | 274,407 |
| combo_all_components | 363 | 24,912 |
| combo_fallback (flagged, not clean) | 101 | 5,956 |
| none | 1,105 | 43,967 |

By detection year (`results/phase_b1/b1_coverage_by_year.csv`) coverage is flat, so the
mapping does not bias the time axis:

| year | signals | mapped % |
|---|---|---|
| 2016 | 52,206 | 87.8 |
| 2017 | 47,936 | 87.7 |
| 2018 | 51,504 | 88.3 |
| 2019 | 48,987 | 88.1 |
| 2020 | 48,422 | 87.7 |
| 2021 | 41,932 | 85.8 |
| 2022 | 39,156 | 86.2 |
| 2023 | 19,099 | 86.5 |

### Top 20 unmapped drugs by signal count

| drug_ai | signals | | drug_ai | signals |
|---|---|---|---|---|
| PREDNISONE | 1,585 | | CORTICOTROPIN | 480 |
| OXALIPLATIN | 889 | | AMOXICILLIN\\CLAVULANIC ACID | 443 |
| ONDANSETRON HYDROCHLORIDE | 816 | | FLUDARABINE PHOSPHATE | 435 |
| RAMIPRIL | 752 | | RIFAMPIN | 426 |
| VALPROATE SODIUM | 698 | | ALENDRONIC ACID | 402 |
| HUMAN IMMUNOGLOBULIN G\\HYALURONIDASE | 586 | | RUCAPARIB CAMSYLATE | 378 |
| VALPROIC ACID | 552 | | RIBAVIRIN | 347 |
| CABOZANTINIB S-MALATE | 539 | | LAPINE T-LYMPHOCYTE IMMUNE GLOBULIN | 321 |
| ONDANSETRON | 534 | | MYCOPHENOLATE SODIUM | 313 |
| ETANERCEPT-SZZS | 487 | | OMBITASVIR\\PARITAPREVIR\\RITONAVIR | 309 |

The 1,105 unmapped drugs split into two causes
(`results/phase_b1/b1_unmapped_diagnosis.csv`):

| cause | drugs | signals | % of cohort |
|---|---|---|---|
| absent from the NDA/BLA set-id universe | 636 | 28,359 | 8.1 |
| all components present, matcher did not join | 325 | 12,535 | 3.6 |
| some components present | 144 | 3,073 | 0.9 |

The first group is a real limit of the NDA/BLA-only route, not a bug. Probing the
universe directly: ZOFRAN, ELOXATIN, ALTACE, RIFADIN and any PREDNISONE product return
**zero** titles. These are old molecules whose currently posted SPLs are ANDA generics;
the originator NDA label is no longer listed. Restricting to NDA/BLA buys a clean
marketing category at the cost of most pre-2000 drugs.

The second group is fixable: CABOMETYX is in the universe but `CABOZANTINIB S-MALATE`
did not join to it, so the salt list is missing entries.

## 4. Hand-checked precision of the mapping

A random sample of 50 mapped drugs (seed 42, `results/phase_b1/b1_handcheck_sample.csv`)
was checked by reading the SPL titles of its set ids against the `drug_ai` string.

**Precision 42/50 = 84%, Wilson 95% CI [71%, 92%].**

Caveat on independence: the matcher keys on the parenthesised ingredient list inside the
title, and the check reads the same titles. The check therefore catches wrong-product and
wrong-salt joins, which is what it is for, but it cannot catch a title that is itself
wrong about its product.

The 8 errors fall into three classes, none random:

**A. Salt and ester conflation (3 of 8).** The salt stripper treats chemically distinct
esters as one moiety, so it joins products that have separate labels:

- `METOPROLOL SUCCINATE` → LOPRESSOR (metoprolol **tartrate**)
- `BETAMETHASONE SODIUM PHOSPHATE` → DIPROLENE (betamethasone **dipropionate**)
- `HYDROCORTISONE VALERATE` → CORTIFOAM (hydrocortisone **acetate**)

**B. Superset contamination (4 of 8).** The combination rule requires every component of
`drug_ai` to appear in the label, but does not forbid the label carrying **extra**
ingredients, so a pair maps onto a larger fixed-dose product:

- `ABACAVIR SULFATE\\LAMIVUDINE` → TRIUMEQ (adds dolutegravir)
- `FEXOFENADINE` → ALLEGRA-D (adds pseudoephedrine)
- `CIPROFLOXACIN` → CIPRO HC (adds hydrocortisone, benzyl alcohol)
- `AMLODIPINE\\TELMISARTAN` → WIDAPLIK (adds indapamide)

Sized across the whole mapping: **199 mapped drugs, 13,596 signals (3.9% of the cohort)**
have at least one strict-superset set id, and 80 of those already have an exact-set label
available, so most of the damage is reversible by preferring the exact set.

**C. combo_fallback (1 of 8).** `GLYBURIDE` → GLUCOVANCE. This is wrong as a product
match, but the pipeline already labels it `combo_fallback` rather than counting it clean
(101 drugs, 5,956 signals, 1.7%). Dropping this level outright costs 1.7% of signals and
removes the class.

### What this means for the gate

87.4% signal coverage at 84% mapping precision is usable but not yet clean. Three fixes,
in order of value per unit of work:

1. Require the label ingredient set to **equal** the `drug_ai` component set, falling back
   to superset only when no exact-set label exists and flagging it. Removes class B.
2. Remove ester-type salts (succinate, tartrate, valerate, dipropionate, acetate,
   phosphate) from the stripper where they distinguish products. Removes class A, and
   should also recover part of the 3.6% matcher-gap group.
3. Drop `combo_fallback` from the clean mapping. Removes class C at 1.7% of signals.

None of these is applied yet. Stopping here per the gate instruction.

## Known risks before the crawl

- Ingredient names are parsed from SPL titles, which follow "BRAND (INGREDIENT) FORM [LABELER]" but
  are not guaranteed to. Titles that omit the parenthesised ingredient fall back to the leading
  token, which will be wrong for some. The hand-check in item 4 is what measures this.
- `drug_ai` keeps salt variants separate (PANTOPRAZOLE and PANTOPRAZOLE SODIUM are two drugs), so
  salt-stripped matching is needed on both sides and will pull in more set ids than a strict match.
- Biologics named with a suffix (DENOSUMAB-BMWO) may not match the DailyMed ingredient string.
