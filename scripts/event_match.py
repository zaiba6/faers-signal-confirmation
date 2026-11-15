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
]
RISK_CUES = [
    r"\b(?:risks? of|can cause|may cause|cause[sd]?|associated with|reports? of|reported"
    r"|resulting in|leading to|developed|develops?|occurr?ed|occurs?|experienced|incidence of"
    r"|cases of|secondary to|due to|precipitate[sd]?|induced?|worsening of|exacerbation of"
    r"|has been observed|were observed|led to)\b",
]
# "such as" introduces examples of whatever precedes it - a patient group ("disorders, such as
# myasthenia gravis") or an adverse-event list ("skin reactions, such as rash"). It counts as a
# population cue ONLY when a patient-subject phrase comes before it in the same sentence. Treating
# it as one unconditionally wrongly vetoed roughly 1,042 event-noun cases.
SUCH_AS = re.compile(r"\bsuch as\b", re.I)
SUBJECT_ONLY = re.compile(rf"\b{SUBJECTS}\s+(?:who\s+(?:have|had|are|were)\s+|with\s+|having\s+|without\s+)", re.I)
POP_C = [re.compile(p, re.I) for p in POP_CUES]
RISK_C = [re.compile(p, re.I) for p in RISK_CUES]
SENT_END = re.compile(r"[.;:]\s|\u2026")

# ---------------------------------------------------------------- hand-check fixes (decision 99)
# Both vetoes below come from the seed-2026 hand-check of the baseline rule (decision 95), where
# 3 of 30 sampled exclusions were wrong. Both error classes are OVER-exclusion, and both are
# matches on text that is not a statement about the drug at all.

# (a) A SECTION CROSS-REFERENCE. US labels thread "[see Warnings and Precautions (5.3)]" and
#     "(see Drug Interactions (7.3))" through every section. The bracketed text is a pointer to
#     another section, not a description of an event, so VOXELOTOR / DRUG INTERACTION was recorded
#     as already labelled on the strength of "[see Drug Interactions (7.3)]". A hit whose start
#     falls inside such a bracket is skipped.
XREF = re.compile(r"\[\s*see\b[^\]]{0,200}\]|\(\s*see\b[^)]{0,200}\)", re.I)

# (b) A SYSTEM ORGAN CLASS TABLE HEADING. Adverse-reaction tables are grouped under MedDRA SOC
#     names - "Eye disorders: conjunctivitis", "Gastrointestinal Disorders: fundic gland polyps".
#     The heading names the GROUP; the events are what follows it. TOCILIZUMAB / EYE DISORDER and
#     RABEPRAZOLE / GASTROINTESTINAL DISORDER both fired on the heading. The veto is deliberately
#     narrow: it applies only when the matched text is itself one of the 27 SOC names AND is
#     immediately followed by a colon. A subsection heading that names a real event, which is how
#     labels usually introduce one ("5.3 Increased Photosensitivity", "Hepatotoxicity:"), is
#     untouched, because a colon alone is not enough to trigger it.
SOC_NAMES = {
    "blood and lymphatic system disorders", "cardiac disorders", "congenital familial and genetic disorders",
    "ear and labyrinth disorders", "endocrine disorders", "eye disorders", "gastrointestinal disorders",
    "general disorders and administration site conditions", "hepatobiliary disorders",
    "immune system disorders", "infections and infestations",
    "injury poisoning and procedural complications", "investigations",
    "metabolism and nutrition disorders", "musculoskeletal and connective tissue disorders",
    "neoplasms benign malignant and unspecified", "nervous system disorders",
    "pregnancy puerperium and perinatal conditions", "product issues", "psychiatric disorders",
    "renal and urinary disorders", "reproductive system and breast disorders",
    "respiratory thoracic and mediastinal disorders", "skin and subcutaneous tissue disorders",
    "social circumstances", "surgical and medical procedures", "vascular disorders",
}
_SOC_NORM = re.compile(r"[^a-z ]+")


def _is_soc_heading(text, m):
    """The hit is a MedDRA SOC name used as a table heading: '<SOC name>:' ."""
    if _SOC_NORM.sub("", m.group(0).lower()).strip() not in SOC_NAMES:
        return False
    return bool(re.match(r"\s{0,3}:", text[m.end():m.end() + 4]))


# MedDRA writes PTs in British English; US prescribing information does not. Without this,
# DYSPNOEA can never match "dyspnea", DIARRHOEA never matches "diarrhea" and OEDEMA
# PERIPHERAL never matches "peripheral edema". Measured before the fix: 1,383 PTs carrying
# 34,393 T1 signals (9.8% of the cohort) confirmed at 0.12% against 1.23% for the rest,
# a tenfold deficit on some of the most commonly labelled events there are.
# Each entry maps a British fragment to a pattern accepting both spellings. Applied to the
# ESCAPED pattern, longest first, so the digraph rules cannot cut a word-level rule in half.
SPELLING = [
    ("oesophag", "(?:oe|e)sophag"), ("anaesth", "an(?:ae|e)sth"), ("gynaec", "gyn(?:ae|e)c"),
    ("orthopaed", "orthop(?:ae|e)d"), ("paediatr", "p(?:ae|e)diatr"), ("coeliac", "c(?:oe|e)liac"),
    ("oestr", "(?:oe|e)str"), ("caecum", "c(?:ae|e)cum"), ("tumour", "tumou?r"),
    ("leuco", "leu[ck]o"), ("leuko", "leu[ck]o"), ("sulph", "sul(?:ph|f)"),
    ("ae", "(?:ae|e)"), ("oe", "(?:oe|e)"),
]


def _spell(escaped):
    out, i = [], 0
    low = escaped.lower()
    while i < len(low):
        for frag, pat in SPELLING:
            if low.startswith(frag, i):
                out.append(pat); i += len(frag); break
        else:
            out.append(escaped[i]); i += 1
    return "".join(out)


def pt_regex(pt):
    """Word-boundary matcher for a PT: singular or plural, British or US spelling.

    str.rstrip("s") strips EVERY trailing s, so DIZZINESS became DIZZINE and matched nothing.
    Terms ending in a double s take -es; a single trailing s is a plural to be made optional.
    """
    t = pt.strip()
    low = t.lower()
    # Decision 99. MedDRA often appends a qualifier noun the label leaves off: the PT is
    # PHOTOSENSITIVITY REACTION where a Warnings heading reads "Increased Photosensitivity",
    # so the event was on the label and the rule could not see it. Only REACTION is made
    # optional, and only as a trailing word. DISORDER and SYNDROME are NOT, because dropping
    # them changes the concept - SLEEP DISORDER would match bare "sleep", SEROTONIN SYNDROME
    # bare "serotonin".
    tail = ""
    if low.endswith(" reaction"):
        base = t[:-len(" reaction")].rstrip()
        # The base has to be able to stand for the event BY ITSELF, or dropping the noun
        # changes what is being matched. Measured on the 155 rows this relaxation first
        # moved: a short or multi-word base decays into a location, a route or a substance
        # and matches text about neither the drug nor an event - SKIN REACTION fired on
        # "Candida skin test", INFUSION SITE REACTION on "monitor the ABRAXANE infusion
        # site", CONTRAST MEDIA REACTION on the name of the contrast agent, NONSPECIFIC
        # REACTION on "nonspecific symptoms". So: one word, at least 8 characters, and not
        # a word naming the ACT of giving the drug rather than an effect of it (the same
        # distinction decision 68 draws). PHOTOSENSITIVITY, ANAPHYLACTIC and ANAPHYLACTOID
        # pass; SKIN, LOCAL, PANIC, INJECTION SITE and TRANSFUSION do not.
        GIVING = {"transfusion", "injection", "infusion", "application", "administration",
                  "vaccination", "implantation", "immunisation", "immunization"}
        if " " not in base and len(base) >= 8 and base.lower() not in GIVING:
            t, tail = base, r"(?:\s+reactions?)?"
            low = t.lower()
    if len(t) > 4 and low.endswith("ss"):
        body = _spell(re.escape(t)) + r"(?:es)?"
    elif len(t) > 4 and low.endswith("s"):
        body = _spell(re.escape(t[:-1])) + r"s?"
    else:
        body = _spell(re.escape(t)) + r"s?"
    return re.compile(r"\b" + body + tail + r"\b", re.I)


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
    xref = [(x.start(), x.end()) for x in XREF.finditer(text)]
    seen = False
    for m in rx.finditer(text):
        if any(a <= m.start() < b for a, b in xref):
            continue                                # pointer to another section, not an event
        if _is_soc_heading(text, m):
            continue                                # names the table group, not a reaction in it
        seen = True
        before = text[max(0, m.start() - lookback):m.start()]
        cut = None                                  # only cues in the PT's own sentence count
        for b in SENT_END.finditer(before):
            cut = b.end()
        if cut:
            before = before[cut:]
        risk, pop = _nearest(RISK_C, before), _nearest(POP_C, before)
        sa = _nearest([SUCH_AS], before)
        if sa >= 0 and SUBJECT_ONLY.search(before[:sa]):
            pop = max(pop, sa)                      # "patients with ..., such as X" -> population
        if risk > pop:
            return True, "risk_context"
        if pop >= 0:
            continue                                # patient group, not an outcome
        if section_code == "CONTRA":
            continue                                # a contraindication without risk wording
        return True, "plain"
    return False, ("population_or_contra_only" if seen else "no_hit")
