"""The single-source clustering guard. Written BEFORE applying it or looking at any result.

The problem (decision 84). LAMIVUDINE\\TENOFOVIR / CONGENITAL UMBILICAL HERNIA scores
ROR 34,406. That is not a deduplication failure: the pair has 38 rows, 38 distinct caseid
and 38 distinct primaryid, and build_case_table.py keeps one row per caseid by
caseversion. The cases are genuinely distinct. They are also genuinely one submission:
36 of the 38 reached FDA in a single quarter, and all 38 carry FOETAL EXPOSURE DURING
PREGNANCY, the signature of a pregnancy registry filing its cohort in one go.

A cumulative 2x2 cannot tell "many independent clinicians noticed this" from "one
registry sent its file". Disproportionality assumes the former. When the latter is true
the ROR describes a submission episode, not an association.

THE RULE, fixed before measurement:

    burst_share = (cases in the pair's single busiest init_quarter) / (cases so far)
    burst_n     = cases in that busiest quarter

    A pair is flagged source_clustered when  burst_share >= 0.80  AND  burst_n >= 10.

Both conditions are needed. burst_share alone would kill ordinary small pairs: a pair
with a = 3 that happens to have all three reports in one quarter is unremarkable, and at
the detection thresholds in use (a >= 3 for T1) most pairs are small. Requiring at least
10 cases in the burst restricts the rule to pairs where a single episode is large enough
to drive the statistic on its own.

0.80 and 10 are judgement calls, fixed here so they cannot be tuned until the numbers
look good. The sensitivity of the cohort to both is reported alongside the headline.

LEAKAGE. burst_share is computed ONLY from cases with init_quarter <= detection_quarter,
the same freeze rule the feature table uses. A pair is judged on what was knowable when
it was detected, never on reports that arrived later.

WHAT THIS DOES NOT CLAIM. Co-reporting of a shared PT (all 38 cases carrying FOETAL
EXPOSURE) is an even clearer registry signature, and is deliberately NOT in the rule:
it needs a second pass over the case table per pair and the quarter-concentration test
already catches the same pairs. It is reported as a diagnostic instead, so the choice
can be revisited on evidence rather than on assertion.

Flagged pairs are EXCLUDED from the cohort, not scored Y=0, for the reason in decision
73: the pair is not a question the FDA could sensibly act on, so a negative label would
be a misleading easy negative.
"""
BURST_SHARE = 0.80
BURST_MIN_N = 10


def is_source_clustered(burst_share, burst_n):
    return bool(burst_share >= BURST_SHARE and burst_n >= BURST_MIN_N)
