# Confirmation-label sources: collection notes (data collection only; no matching, no target)

Collected 2026-10-07. Scripts: `scripts/labels_fetch.sh` (cached, 30 s delay), `scripts/labels_s1_fetch_all.sh`, `scripts/parse_labels_s1.py`
(run with `/usr/local/bin/python3`; needs pandas, bs4, duckdb). Raw: `data/raw/labels/` (4.2 MB). Parsed: `data/processed/labels/` (0.7 MB).

## Politeness
- robots.txt: www.fda.gov `Crawl-Delay: 30` for `*`; accessdata `Hit-rate: 30`, `Visiting-hours: 23:00-05:00 EDT`. We used 30 s between requests (stricter than the 2 s requested).
- UA: `faers-signal-confirmation-research/0.1 (academic research; <email>)`. Every page saved once, never re-fetched.
- **Violation to review:** FDA redirects 28 older quarterly URLs to `wayback.archive-it.org/7993/...`. That host's robots.txt
  (`Disallow: /` + short allowlist, `Crawl-delay: 10`) does not allow collection 7993; the redirect target was not checked before fetching.
  Files moved to `data/raw/labels/_quarantine_robots_disallowed/` and **excluded from parsing** pending a decision.
  14 other pages redirect to `web.archive.org` (robots.txt 404 = no restrictions).

## Source 1: Section 921 postings
- The FAERS-era URL 301-redirects to the AEMS URL. Byte-identical content. Same page, renamed (FAERS -> AEMS).
- Index = main page (24 current quarters, 2015Q2-2026Q2, with gaps) + "Archived Quarterly Reports" page (46 quarters, 2008Q1-2022Q1). 70 pages total.
- Index lists no page for these quarters (not fabricated; may have had no postings or are unlinked): 2010Q2, 2012Q3, 2014Q1, 2015Q1.
- Page layout: one table, 3 columns: product, potential signal, "Additional Information (as of <date>)". There is **no separate status column**; the action text is the third column. The "as of" date is in the header and is the same for every row of a quarter's page.
- Parsed: 734 rows, 42 quarters (2009Q4-2026Q2). Row counts verified against raw `<tr>` counts for every parsed quarter. Rowspans expanded (1 row inherits signal+action; flagged in `notes`). Product lists keep `<p>/<li>` boundaries as ` | `. The page's "Updated" badge appears as a leading `Updated |` in raw text.
- **Not parsed (quarantined, 28 quarters):** 2008Q1-2009Q3, 2010Q1, Q3, Q4, 2011Q1-Q3, 2013Q1-Q4, 2014Q3, Q4, 2015Q4, 2016Q3, Q4, 2017Q1, Q3, Q4, 2018Q1-Q3.
- Retries: 2019Q1/Q2 (DNS error) and 2019Q3 (timeout, truncated) re-fetched once.

### outcome_class keyword rule (on lowercased fda_action_raw)
- `labeling_changed`: "was/were updated|revised|added|modified...", "sections of the labeling ... updated", "added to the <section>", "labeling change(s) approved", "FDA has required ... label", "section(s) updated to include".
- `no_action`: "no (further/regulatory) action is/was necessary|needed|planned", "decided/determined that no action".
- `still_evaluating`: "is/are being evaluated/reviewed/assessed", "evaluation ... ongoing", "continues to evaluate", "under review".
- `other_unclear`: none matched, **or more than one class matched** (e.g. "label updated for X; no action for Y"), or blank. `outcome_matches` shows which rules fired.
- Result: labeling_changed 365, still_evaluating 182, no_action 111, other_unclear 76.
- Distinct raw action strings: 456 (443 occur once; free text, not a controlled status field). Full list with counts and assigned class: `data/processed/labels/s1_outcome_mapping_audit.csv`.

## Source 2: SrLC (inspection only; nothing scraped at scale)
- No bulk export. Table has client-side DataTables CSV/Excel/PDF/print buttons (built in the browser from the same HTML; no server export URL).
- Search = `POST /scripts/cder/safetylabelingchanges/index.cfm?event=searchResult.page`, fields `date_range_from`, `date_range_to` (MM/DD/YYYY), `section` (repeat for BW,C,WP,AR,DI,USP,MG; all checked by default), `Datesearch`. **Omitting `section` makes the server 302 back to the blank form.** Session cookies (CFID/CFTOKEN) are set on first GET.
- Result: one server-rendered table, all rows at once (client-side pagination, 30/page): Drug Name, Active Ingredient, Application Number, Application Type, Supplement Date, Database Updated, hidden Link column -> `index.cfm?event=searchdetail.page&DrugNameID=N` (per-drug detail page).
- Page says: pre-2016 changes are on the older MedWatch Safety Labeling page.
- Test saved: `data/raw/labels/s2_test3_2016-01.html` (Jan 2016 -> 3 rows, "Database Updated" 07/11/2016). `s2_test_*`/`s2_test2_*` are the failed attempts (302), kept for traceability.

## Update 2026-10-08 (after owner decisions)
- Fetching now goes through `scripts/labels_polite.py`: manual redirect following, robots.txt of **every** hop's host checked before the request, 30 s per host, cache, `.meta.json` with the full redirect chain, optional time gate.
- 28 quarantined archive-it files: kept, never parsed. 8 in-window quarters (2016Q3-2018Q3) re-fetched from web.archive.org (robots.txt = 404, no restrictions). 2017Q1/2018Q2/2018Q3: the newest captures of the FDA legacy URLs are FDA error redirects, so 2019-04-23 captures were used (earlier rejected attempts left as `*.rejected_*.meta.json`/bodies deleted).
- **Staleness caveat:** every Source 1 row's action text is as of its page's snapshot/“as of” date (2018Q2/Q3 = snapshot 2019-04-23, so "FDA is evaluating" may have been resolved later). `snapshot_date`, `source_url`, `action_asof_date_raw` are on every row (`listed_url` = the URL the index linked).
- Source 1 now 847 rows / 50 quarters; Source 3 (DSC): `data/processed/labels/s3_dsc_index.csv` 99 index rows (95 unique), 2016-01-04..2026-09-15, pages saved in `data/raw/labels/s3_dsc_*.html`.
- Source 2 probe (ran 23:00-05:00 EDT only): 3,262 unique detail links 2016-01-01..2026-10-07; no result cap seen. Earlier daytime inspection (5 requests on 2026-10-07 ~16:00-16:10 EDT) was outside the robots "visiting hours" (non-standard directive aimed at indexers); disclosed, not repeated.
- Detail page (per DrugNameID): product, application no., ingredient, then one block per supplement: date, SUPPL-n, link to approved-label PDF, and the changed section(s) with the label text; added/revised text is underlined in the HTML (`<u>`/underline style). Full scrape (3,262 pages at 30 s) ~27 h of requests = ~5 nights inside the 6-h window; sample page sizes 32-38 KB and one 296 KB (opioid), so roughly 110-260 MB.
- Sanity check: `references/sanity_check_4.md` (`scripts/sanity_check_4.py`).
