"""Does this label-change text confirm THIS adverse event? Shared by the target builder and the evaluator.

A word-boundary hit on the MedDRA PT is necessary but not sufficient. Two rounds of hand-adjudication
(references/event_matcher_judged{,_v2}.csv) found one dominant failure mode: the PT naming WHO was
treated rather than what happened to them.

    "Among the 259 patients with gastric cancer enrolled in KEYNOTE-059"
    "Thyroid Storm in Patients with Hyperthyroidism"
    "may aggravate muscle weakness in patients with ... disorders, such as myasthenia gravis"
    "such as those with heart failure, recent myocardial infarction"

So each hit is judged by the wording closest to it, inside its own sentence:

    nearest cue is RISK ("risk of", "can cause", "cases of", "developed")  -> confirms the event
    nearest cue is POPULATION ("patients with", "those with", "history of") -> does not
    no cue                                                                  -> confirms, unless the
                                                                               section is a contraindication

Nearest wins because both can appear in one sentence: "may cause parkinsonism in patients with
Huntington's disease or tardive dyskinesia" is a risk statement about parkinsonism and a population
statement about tardive dyskinesia, and which PT matched decides the answer. Looking only for a risk
cue anywhere got that backwards.

Contraindications are population by default: a contraindication says who must not take the drug, not
what the drug did. They count only when phrased as risk ("contraindicated due to the risk of ...").
"""
import re

# PTs that cannot be confirmed by label text: no clinical event, or a procedure/population word.
GENERIC_PT = {
    "ADVERSE REACTION", "ADVERSE EVENT", "ADVERSE DRUG REACTION", "ADVERSE EVENT FOLLOWING",
    "ILLNESS", "DISEASE", "SYMPTOM", "CONDITION AGGRAVATED", "DRUG INEFFECTIVE",
    "DRUG EFFECT DECREASED", "THERAPEUTIC RESPONSE DECREASED", "THERAPEUTIC RESPONSE UNEXPECTED",
    "NO ADVERSE EVENT", "OFF LABEL USE", "PRODUCT USE IN UNAPPROVED INDICATION",
    "PRODUCT USE ISSUE", "INTENTIONAL PRODUCT MISUSE", "TREATMENT FAILURE", "DRUG USE DISORDER",
    # procedure / care-setting terms: they describe what was done, not what went wrong
    "TRANSPLANT", "SURGERY", "HOSPITALISATION", "HOSPITALIZATION", "THERAPY INTERRUPTED",
    "TREATMENT NONCOMPLIANCE", "DRUG ADMINISTRATION ERROR",
}

SUBJECTS = r"(?:patients?|subjects?|women|men|individuals?|participants?|recipients?|those|cases|persons?|people)"
POP_CUES = [
    rf"\b{SUBJECTS}\s+(?:who\s+(?:have|had|are|were)\s+|with\s+|having\s+|without\s+)",
    r"\b(?:history|diagnos[ei]s)\s+of\s+",
    r"\b(?:among|a total of|enrolled in|indicated for|indication[s]?:|treatment of|management of)\b",
    r"\bcontraindicated in\b",
    r"\bsuch as\b",
]
RISK_CUES = [
    r"\b(?:risks? of|can cause|may cause|cause[sd]?|associated with|reports? of|reported"
    r"|resulting in|leading to|developed|develops?|occurr?ed|occurs?|experienced|incidence of"
    r"|cases of|secondary to|due to|precipitate[sd]?|induced?|worsening of|exacerbation of"
    r"|has been observed|were observed|led to)\b",
]
POP_C = [re.compile(p, re.I) for p in POP_CUES]
RISK_C = [re.compile(p, re.I) for p in RISK_CUES]
SENT_END = re.compile(r"[.;:]\s|\u2026")


def pt_regex(pt):
    """Word-boundary matcher for a PT, singular or plural.

    str.rstrip("s") strips EVERY trailing s, so DIZZINESS became DIZZINE and matched nothing.
    Terms ending in a double s take -es; a single trailing s is a plural to be made optional.
    """
    t = pt.strip()
    low = t.lower()
    if len(t) > 4 and low.endswith("ss"):
        body = re.escape(t) + r"(?:es)?"
    elif len(t) > 4 and low.endswith("s"):
        body = re.escape(t[:-1]) + r"s?"
    else:
        body = re.escape(t) + r"s?"
    return re.compile(r"\b" + body + r"\b", re.I)


def _nearest(patterns, text):
    """End position of the cue closest to the end of `text`, or -1."""
    best = -1
    for c in patterns:
        for m in c.finditer(text):
            best = max(best, m.end())
    return best


def event_match(text, pt, rx=None, section_code="", lookback=140):
    """True when `text` plausibly confirms `pt` as an adverse event. Returns (bool, reason)."""
    if pt.upper() in GENERIC_PT:
        return False, "generic_pt"
    rx = rx or pt_regex(pt)
    seen = False
    for m in rx.finditer(text):
        seen = True
        before = text[max(0, m.start() - lookback):m.start()]
        cut = None                                  # only cues in the PT's own sentence count
        for b in SENT_END.finditer(before):
            cut = b.end()
        if cut:
            before = before[cut:]
        risk, pop = _nearest(RISK_C, before), _nearest(POP_C, before)
        if risk > pop:
            return True, "risk_context"
        if pop >= 0:
            continue                                # patient group, not an outcome
        if section_code == "CONTRA":
            continue                                # a contraindication without risk wording
        return True, "plain"
    return False, ("population_or_contra_only" if seen else "no_hit")
