"""Build the confirmation target: was a detected drug-event signal confirmed by an FDA action within N years?

Run: /usr/local/bin/python3 scripts/build_confirmation_target.py   (needs duckdb, pandas)
In : data/processed/detected_pairs_T{1,2,3}.parquet
     data/processed/labels/s2_label_changes.parquet   (SrLC, parsed label text)
     data/processed/labels/s1_signals.csv             (Section 921 postings)
     data/processed/labels/s3_dsc_index.csv           (Drug Safety Communications)
Out: data/processed/confirmation_target_T{1,2,3}.parquet / .csv  (one row per eligible pair)
     data/processed/labels/confirmation_target_report.json

DEFINITIONS (all recorded in the report; see DECISIONS.md)
- Clock starts at the END of the detection quarter: a signal detected in 2018Q2 starts its
  window on 2018-06-30, since an analyst could only know it once the quarter closed.
- Eligible pairs are status == 'in_window' only (detected 2016Q1-2023Q2). Earlier pairs predate
  SrLC coverage; later ones cannot be followed for a full N years, so including them would
  label right-censored signals as negatives.
- Two target flavours, both produced, because they answer different questions:
    confirmed_drug  : the DRUG got a safety-related label change in the window, any event.
    confirmed_event : as above AND the event term appears in the changed label text.
  confirmed_event is the stricter, more faithful target; confirmed_drug is its upper bound.
  The gap between them is reported, since event text matching is the weakest link here.
- "Safety-related" = SrLC section code in BW / WP / AR / CONTRA (boxed warning, warnings and
  precautions, adverse reactions, contraindications). PCI/PI/MG, dosing and the rest are excluded
  as not risk-bearing.
- Stub rows ("Extensive changes; please refer to label...") carry a valid date and section but no
  text, so they can support confirmed_drug and never confirmed_event. Counted separately.
- Drug matching requires the SAME product: equal component sets and equal component counts, after a
  salt normalisation that never deletes a whole component. component_any was removed after it was
  measured to confirm 763 of 4,285 positives (17.8%) from a different product's label.
- Event matching is a case-insensitive substring test of the MedDRA PT in the label text, also
  matched on word boundaries with an optional plural S, then vetoed where the PT names a patient
  population rather than an outcome (scripts/event_match.py). Measured precision 86% before the
  veto. This is high precision, low recall: label prose rarely
  uses the PT verbatim. Treat confirmed_event as a LOWER bound.
- Section 921 and DSC contribute drug-level flags only. 921 gives a posting quarter and an outcome
  class; DSC titles in the index are truncated, so DSC drug matching is name-in-title/URL and is
  reported as supplementary, not folded into the primary target.
"""
import re, json, sys
from pathlib import Path
import duckdb, pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from match_srlc_ingredients import comps, SALT_RE   # same normalisation as the drug-level match


def raw_components(s):
    """Split a product string into its ingredient components WITHOUT salt stripping."""
    return [c.strip() for c in re.split(r"[;\\/,]| AND ", str(s).upper()) if c.strip()]


def safe_components(s):
    """Normalised components, but a component is never allowed to vanish.

    comps() strips salt words, and the salt list contains SODIUM and BICARBONATE, so
    "SODIUM BICARBONATE" - a real active ingredient in ZEGERID - normalised to nothing and a
    two-ingredient product looked like plain omeprazole. Any component that empties out keeps its
    unstripped form instead.
    """
    out = []
    for c in raw_components(s):
        stripped = re.sub(r"[^A-Z0-9\- ]", " ", SALT_RE.sub(" ", c))
        stripped = re.sub(r"\s+", " ", stripped).strip()
        out.append(stripped or re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\- ]", " ", c)).strip())
    return sorted(set(c for c in out if c))
from event_match import event_match, pt_regex
from nonclinical_pts import is_nonclinical

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "data/processed"
L = P / "labels"
RISK = ("BW", "WP", "AR", "CONTRA")
YEARS = (1, 2, 3, 5)
PRIMARY = 3


def quarter_end(q):
    y, n = int(q[:4]), int(q[-1])
    return pd.Timestamp(y, n * 3, 1) + pd.offsets.MonthEnd(0)


def map_drugs(con):
    """SrLC label-change rows -> FAERS drug_ai. Returns a long table (one row per change x drug_ai)."""
    fdrugs = con.execute(" union ".join(
        f"select distinct drug_ai from read_parquet('{P}/detected_pairs_{t}.parquet')" for t in ("T1", "T2", "T3")
    )).df().drug_ai.tolist()
    # Index FAERS drugs by their component SET. A label may only confirm a pair when it describes
    # the same product, so the match requires the same components and the same component count.
    ex, nrm = {}, {}
    for d in fdrugs:
        ex.setdefault(d.upper().replace("\\", ";").replace(" ;", ";"), []).append(d)
        nrm.setdefault("|".join(safe_components(d)), []).append(d)

    lc = con.execute(f"select * from read_parquet('{L}/s2_label_changes.parquet')").df()
    out = []
    for ai, grp in lc.groupby("active_ingredient", sort=False):
        if not str(ai).strip():
            levels = {}
        else:
            k_ex = ";".join(p.strip() for p in re.split(r"[;\\]", ai.upper())).replace(" ;", ";")
            key = "|".join(safe_components(ai))
            n_src = len(raw_components(ai))
            hits_ex = {d for d in ex.get(k_ex, [])}
            hits_n = {d for d in nrm.get(key, []) if len(raw_components(d)) == n_src}
            levels = {d: ("exact" if d in hits_ex else "salt_stripped") for d in hits_ex | hits_n}
            # component_any is gone on purpose: it matched a single-ingredient drug to any label
            # that happened to contain that ingredient, which is how a ZEGERID
            # (omeprazole + sodium bicarbonate) label confirmed plain OMEPRAZOLE MAGNESIUM.
        for d, lvl in levels.items():
            g = grp.copy(); g["drug_ai"] = d; g["match_level"] = lvl
            out.append(g)
    m = pd.concat(out, ignore_index=True) if out else pd.DataFrame()
    m["is_risk"] = m.section_code.isin(RISK)
    m["strict"] = m.match_level.isin(["exact", "salt_stripped"])
    return m, lc


def main():
    con = duckdb.connect()
    changes, lc_raw = map_drugs(con)
    con.register("ch", changes)

    s1 = pd.read_csv(L / "s1_signals.csv", keep_default_na=False)
    s1 = s1[s1.outcome_class == "labeling_changed"].copy()
    s1["drug_key"] = s1.drug_name_raw.str.upper()
    con.register("s1", s1[["posting_quarter", "drug_key", "potential_signal_raw"]])

    s3 = pd.read_csv(L / "s3_dsc_index.csv", keep_default_na=False)
    s3["blob"] = (s3.title_raw + " " + s3.dsc_url).str.upper()
    s3["dsc_dt"] = pd.to_datetime(s3.dsc_date, errors="coerce")
    con.register("s3", s3[["dsc_dt", "blob"]].dropna())

    report = {"definitions": dict(risk_sections=list(RISK), primary_years=PRIMARY, years=list(YEARS),
                                 eligible_status="in_window", clock="end of detection quarter")}
    for t in ("T1", "T2", "T3"):
        pairs = con.execute(f"""
            select drug_ai, pt_clean, detection_quarter, a, b, c, d, ror, ror_lcl, prr, chi2, q_value
            from read_parquet('{P}/detected_pairs_{t}.parquet') where status = 'in_window'""").df()
        # Non-clinical PTs leave the cohort entirely (scripts/nonclinical_pts.py, decision 68).
        # They are product-use, efficacy and medication-error terms, not clinical effects of a drug,
        # and they confirm at 0.028% against 1.120% for the rest, so they would act as free
        # negatives that the model could separate on event type rather than on safety.
        n_before = len(pairs)
        drop = pairs.pt_clean.map(is_nonclinical)
        pairs = pairs[~drop].reset_index(drop=True)
        report.setdefault("nonclinical_excluded", {})[t] = dict(
            before=n_before, removed=int(drop.sum()), after=len(pairs),
            removed_pct=round(100 * drop.sum() / n_before, 2))
        pairs["detect_dt"] = pairs.detection_quarter.map(quarter_end)
        con.register("pr", pairs)

        # one row per (pair, qualifying label change in its widest window)
        joined = con.execute(f"""
            select pr.rowid_ rid, ch.change_date_iso::date cd, ch.section_code, ch.strict, ch.is_stub,
                   lower(ch.label_text) txt, lower(pr.pt_clean) pt, pr.detect_dt
            from (select *, row_number() over () - 1 as rowid_ from pr) pr
            join ch on ch.drug_ai = pr.drug_ai and ch.is_risk
            where ch.change_date_iso::date > pr.detect_dt
              and ch.change_date_iso::date <= pr.detect_dt + interval '{max(YEARS)} years'""").df()
        joined["cd"] = pd.to_datetime(joined.cd)
        # See scripts/event_match.py: word-boundary hit, minus population/indication context,
        # contraindication-only mentions, and PTs that name no clinical event.
        rxs = {t: pt_regex(t) for t in joined.pt.unique()}
        joined["ev"] = [event_match(x, p, rxs[p], sc)[0]
                        for x, p, sc in zip(joined.txt, joined.pt, joined.section_code)]
        joined["yrs"] = ((joined.cd - joined.detect_dt).dt.days / 365.25)

        for n in YEARS:
            w = joined[joined.yrs <= n]
            pairs[f"confirmed_drug_{n}y"] = pairs.index.isin(w.rid).astype(int)
            pairs[f"confirmed_event_{n}y"] = pairs.index.isin(w[w.ev].rid).astype(int)
            if n == PRIMARY:
                pairs["confirmed_drug_strict"] = pairs.index.isin(w[w.strict].rid).astype(int)
                pairs["confirmed_event_strict"] = pairs.index.isin(w[w.ev & w.strict].rid).astype(int)
                first = w[w.ev].groupby("rid").cd.min()
                pairs["first_confirm_date"] = pairs.index.map(first).astype("datetime64[ns]")
                sec = w[w.ev].sort_values("cd").groupby("rid").section_code.first()
                pairs["first_confirm_section"] = pairs.index.map(sec).fillna("")
        pairs["days_to_confirm"] = (pairs.first_confirm_date - pairs.detect_dt).dt.days

        # supplementary drug-level flags (not part of the primary target)
        pairs["dsc_drug_3y"] = con.execute(f"""
            select coalesce(max(case when s3.blob like '%' || upper(pr.drug_ai) || '%' then 1 else 0 end), 0) f
            from (select *, row_number() over () - 1 as rowid_ from pr) pr
            left join s3 on s3.dsc_dt > pr.detect_dt and s3.dsc_dt <= pr.detect_dt + interval '3 years'
            group by pr.rowid_ order by pr.rowid_""").df().f.values
        pairs["s921_drug_3y"] = con.execute(f"""
            select coalesce(max(case when s1.drug_key like '%' || upper(pr.drug_ai) || '%' then 1 else 0 end), 0) f
            from (select *, row_number() over () - 1 as rowid_ from pr) pr
            left join s1 on s1.posting_quarter > pr.detection_quarter
            group by pr.rowid_ order by pr.rowid_""").df().f.values

        out = pairs.drop(columns=["detect_dt"])
        con.register("out", out)                       # duckdb writes the parquet (no pyarrow in this env)
        con.execute(f"COPY out TO '{P}/confirmation_target_{t}.parquet' (FORMAT PARQUET)")
        out.to_csv(P / f"confirmation_target_{t}.csv", index=False)

        report[t] = dict(
            n_pairs=len(pairs), n_drugs=int(pairs.drug_ai.nunique()), n_events=int(pairs.pt_clean.nunique()),
            detection_range=[pairs.detection_quarter.min(), pairs.detection_quarter.max()],
            positives={f"{k}_{n}y": int(pairs[f"confirmed_{k}_{n}y"].sum()) for n in YEARS for k in ("drug", "event")},
            rates={f"{k}_{n}y": round(100 * pairs[f"confirmed_{k}_{n}y"].mean(), 2) for n in YEARS for k in ("drug", "event")},
            strict_only_3y=dict(drug=int(pairs.confirmed_drug_strict.sum()), event=int(pairs.confirmed_event_strict.sum())),
            median_days_to_confirm=(None if pairs.days_to_confirm.notna().sum() == 0
                                    else int(pairs.days_to_confirm.median())),
            first_confirm_section=pairs.first_confirm_section.replace("", "(none)").value_counts().to_dict(),
            supplementary=dict(dsc_drug_3y=int(pairs.dsc_drug_3y.sum()), s921_drug_3y=int(pairs.s921_drug_3y.sum())))
        print(t, json.dumps(report[t], indent=1, default=str))

    report["label_changes_mapped"] = dict(
        rows_in=len(lc_raw), rows_mapped_to_faers_drug=len(changes),
        distinct_faers_drugs=int(changes.drug_ai.nunique()),
        risk_section_rows=int(changes.is_risk.sum()), stub_rows=int(changes.is_stub.sum()),
        by_match_level=changes.drop_duplicates(["drug_name_id", "change_date", "section_name"]).match_level.value_counts().to_dict())
    (L / "confirmation_target_report.json").write_text(json.dumps(report, indent=1, default=str))
    print(json.dumps(report["label_changes_mapped"], indent=1, default=str))


if __name__ == "__main__":
    main()
