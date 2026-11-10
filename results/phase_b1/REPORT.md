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

## 3 and 4. Coverage and hand-checked precision: NOT YET PRODUCED

These two items need the actual name to set id mapping, which needs the 4,132 list calls above.
The brief says not to fetch set ids yet, so they are deliberately not in this report.

Producing them costs about **1.1 hours** of crawling at one request per second, cached and
resumable, and downloads no label versions. Once that completes this report gains:

- coverage as the share of cohort signals with at least one mapped NDA/BLA set id, overall and by
  detection year
- the top 20 unmapped drugs by signal count
- a hand-checked random sample of 50 name to set id mappings with precision and the wrong ones listed

## Known risks before the crawl

- Ingredient names are parsed from SPL titles, which follow "BRAND (INGREDIENT) FORM [LABELER]" but
  are not guaranteed to. Titles that omit the parenthesised ingredient fall back to the leading
  token, which will be wrong for some. The hand-check in item 4 is what measures this.
- `drug_ai` keeps salt variants separate (PANTOPRAZOLE and PANTOPRAZOLE SODIUM are two drugs), so
  salt-stripped matching is needed on both sides and will pull in more set ids than a strict match.
- Biologics named with a suffix (DENOSUMAB-BMWO) may not match the DailyMed ingredient string.
