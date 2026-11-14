"""The non-clinical PT exclusion rule. Written BEFORE applying it or looking at any result.

Target: MedDRA System Organ Classes that do not describe a clinical effect of a drug, mainly
"Product issues" and "Social circumstances", plus the medication-error part of "Injury, poisoning
and procedural complications".

Why this is a keyword rule and not an SOC lookup. The SOC route needs the MedDRA PT-to-SOC
mapping, which ships as a licensed MSSO dictionary this project does not have, and FAERS does not
record the MedDRA version its PTs were coded against. The patterns below approximate those SOCs on
the PT string alone. The full list of terms each pattern catches is exported to
references/nonclinical_pts_excluded.csv so the rule can be audited term by term rather than taken
on trust.

BORDERLINE CATEGORIES, decided before seeing any result:

  Investigations (lab abnormalities)            KEPT IN.
      "Alanine aminotransferase increased" or "Blood test abnormal" can be the way a real
      hepatotoxicity or myelosuppression signal presents, and labels do add lab findings to
      Warnings and Adverse Reactions. Excluding them would remove genuine safety signal. The cost
      is that vague ones such as "Blood test abnormal" stay in.

  Injury, poisoning and procedural complications  PARTLY EXCLUDED.
      Only the medication-error and wrong-use side is removed (wrong technique, dispensing error,
      incorrect dose administered). Real injuries in this SOC, such as fractures, contusions,
      overdose and toxicity, are kept, because a drug causing falls and fractures is exactly the
      kind of signal this project is about.

  Deliberate self-harm and misuse terms          KEPT IN.
      Intentional overdose and drug abuse are clinically meaningful for controlled substances and
      have driven real label changes.

  Non-specific terms (added 2026-11-14, decision 79)   EXCLUDED BY EXACT MATCH ONLY.
      A bare symptom term with no localisation, no mechanism and no laboratory referent
      carries no information about what the drug did, so a label change naming it is not
      evidence about the signal. The match is EXACT on the whole PT, never substring, so
      CHEST PAIN, ABDOMINAL PAIN, INJECTION SITE INFLAMMATION and SWELLING FACE all survive
      and only the bare term goes. Terms fixed below before the post-filter counts were read.

      Kept in deliberately, though they are also general: FATIGUE, MALAISE, PYREXIA, ASTHENIA,
      PRURITUS, RASH, OEDEMA PERIPHERAL. These name a recognised distinct clinical state and
      appear in real labelling changes, so excluding them would remove genuine signal.
"""
import re

PATTERNS = {
    "product_use_and_quality": r"\bPRODUCT (USE|QUALITY|SUBSTITUT|PREPARATION|STORAGE|DISPENSING|"
                               r"PACKAGING|LABEL|AVAILABILITY|DELIVERY|TAMPERING|CONTAMINATION)",
    "off_label_and_indication": r"\bOFF LABEL USE\b|\bUNAPPROVED INDICATION\b|\bPRODUCT USE IN UNAPPROVED",
    "efficacy_not_safety": r"\bDRUG INEFFECTIVE\b|\bTHERAPY NON-RESPONDER\b|\bTREATMENT FAILURE\b|"
                           r"\bDRUG EFFECT (DECREASED|INCOMPLETE)\b|\bTHERAPEUTIC RESPONSE (DECREASED|UNEXPECTED|SHORTENED)\b",
    "medication_error": r"\bMEDICATION ERROR\b|\bWRONG (TECHNIQUE|DRUG|DOSE|ROUTE|PATIENT|PRODUCT)\b|"
                        r"\bINCORRECT (DOSE|ROUTE|TECHNIQUE|PRODUCT|DRUG)\b|\bDISPENSING ERROR\b|"
                        r"\bPRESCRIBED (UNDERDOSE|OVERDOSE)\b|\bDOSE OMISSION\b|\bACCIDENTAL EXPOSURE\b|"
                        r"\bDRUG ADMINISTRATION ERROR\b|\bINTERCEPTED (MEDICATION|PRODUCT)\b",
    "adherence_and_circumstance": r"\bTREATMENT NONCOMPLIANCE\b|\bDRUG DOSE OMISSION\b|\bNO ADVERSE EVENT\b|"
                                  r"\bUNEVALUABLE EVENT\b|\bSOCIAL\b|\bCIRCUMSTANCE\b|\bCORRECTIVE LENS USER\b|"
                                  r"\bDEVICE (USE|MALFUNCTION|FAILURE|ISSUE|DIFFICULTY)\b|\bEXPIRED PRODUCT\b",
}
# Decision 79. Exact match on the whole PT, never substring.
NONSPECIFIC = {
    "PAIN", "INFLAMMATION", "SWELLING", "DISCOMFORT", "ILL-DEFINED DISORDER",
    "ILLNESS", "SYMPTOM", "GENERAL SYMPTOM", "SIGNS AND SYMPTOMS",
    "CLINICAL SIGNS AND SYMPTOMS", "COMPLICATION", "DISORDER",
    "ADVERSE EVENT", "ADVERSE REACTION", "ADVERSE DRUG REACTION",
    "CONDITION AGGRAVATED", "DISEASE PROGRESSION",
}

RULE = re.compile("|".join(f"(?:{p})" for p in PATTERNS.values()), re.I)


def is_nonclinical(pt):
    s = str(pt).upper().strip()
    return s in NONSPECIFIC or bool(RULE.search(s))


def which(pt):
    s = str(pt).upper()
    return [name for name, pat in PATTERNS.items() if re.search(pat, s, re.I)]
