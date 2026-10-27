"""Does this label-change text confirm THIS adverse event? Shared by the target builder and the evaluator.

A plain word-boundary hit on the MedDRA PT is necessary but not sufficient. Hand-adjudication of 50
matched candidates (references/event_matcher_judged.csv, 2026-10-26) put precision at 86%, and every
false positive was one of two kinds:

  population / indication   the PT names WHO was treated, not what happened to them
                            "Among the 259 patients with gastric cancer enrolled in KEYNOTE-059"
                            "Adverse Reactions in Patients with Ankylosing Spondylitis"
                            "Thyroid Storm in Patients with Hyperthyroidism"
                            "in patients with longstanding diabetes ... diabetic neuropathy"
  vacuous PT                the term carries no clinical content: ADVERSE REACTION, ILLNESS

So a hit is vetoed when the words just before it mark a patient group, unless risk wording
("risk of", "can cause", "cases of", ...) is also nearby - that phrasing means the label is
describing harm, not selecting patients.

Contraindications get the same treatment by default: a contraindication says who must not take the
drug, which is a statement about the patient, not a confirmed adverse effect. It counts only when
phrased as risk ("contraindicated due to the risk of hepatic failure").
"""
import re

# MedDRA PTs that cannot be confirmed by label text because they name no clinical event.
GENERIC_PT = {
    "ADVERSE REACTION", "ADVERSE EVENT", "ADVERSE DRUG REACTION", "ADVERSE EVENT FOLLOWING",
    "ILLNESS", "DISEASE", "SYMPTOM", "CONDITION AGGRAVATED", "DRUG INEFFECTIVE",
    "DRUG EFFECT DECREASED", "THERAPEUTIC RESPONSE DECREASED", "THERAPEUTIC RESPONSE UNEXPECTED",
    "NO ADVERSE EVENT", "OFF LABEL USE", "PRODUCT USE IN UNAPPROVED INDICATION",
    "PRODUCT USE ISSUE", "INTENTIONAL PRODUCT MISUSE", "TREATMENT FAILURE", "DRUG USE DISORDER",
}

_W = r"(?:[\w\-,'’()]+\s+){0,4}"
POP_CUES = [
    rf"\b(?:patients?|subjects?|women|men|individuals?|participants?|recipients?|cases)\s+"
    rf"(?:who\s+(?:have|had|are|were)\s+|with\s+|having\s+|without\s+){_W}$",
    rf"\b(?:history|diagnos[ei]s)\s+of\s+{_W}$",
    rf"\b(?:among|a total of|enrolled in|indicated for|indication[s]?[:\s]|treatment of|management of)\b[^.]{{0,70}}$",
    rf"\bcontraindicated in\b[^.]{{0,90}}$",
    rf"\bin\s+(?:adult |pediatric |elderly )?(?:patients?|subjects?)\s+with\s+{_W}$",
]
RISK_CUES = [
    r"\b(?:risk of|risks of|can cause|may cause|cause[sd]?|associated with|reports? of|reported"
    r"|resulting in|leading to|developed|occurr?ed|experienced|incidence of|cases of|secondary to"
    r"|due to|precipitate[sd]?|induced?|worsening of|exacerbation of)\b[^.]{0,70}$",
]
POP_C = [re.compile(p, re.I) for p in POP_CUES]
RISK_C = [re.compile(p, re.I) for p in RISK_CUES]


def pt_regex(pt):
    """Word-boundary matcher for a PT, singular or plural."""
    base = pt.rstrip("s") if len(pt) > 4 and pt.endswith("s") else pt
    return re.compile(r"\b" + re.escape(base) + r"s?\b", re.I)


def event_match(text, pt, rx=None, section_code="", lookback=110):
    """True when `text` plausibly confirms `pt` as an adverse event. Returns (bool, reason)."""
    if pt.upper() in GENERIC_PT:
        return False, "generic_pt"
    rx = rx or pt_regex(pt)
    seen = False
    for m in rx.finditer(text):
        seen = True
        before = text[max(0, m.start() - lookback):m.start()]
        risk = any(c.search(before) for c in RISK_C)
        if risk:
            return True, "risk_context"
        if any(c.search(before) for c in POP_C):
            continue                                  # patient group, not an outcome
        if section_code == "CONTRA":
            continue                                  # a contraindication without risk wording
        return True, "plain"
    return False, ("population_or_contra_only" if seen else "no_hit")
