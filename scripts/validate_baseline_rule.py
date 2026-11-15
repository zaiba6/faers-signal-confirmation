"""Hand-check sample for the baseline label rule (decisions 74-78).

Run: /usr/local/bin/python3 scripts/validate_baseline_rule.py   (after --step rule)
Out: results/phase_b2/b2_rule_handcheck.csv   one row per sampled pair, with the matched
     sentence in context so each call can be judged without reopening the label.

Same protocol as the B1 gate: one FRESH random sample plus targeted strata, scored with
Wilson intervals. Strata, fixed here before any of them is read:

  random        50  any pair the rule could evaluate
  kept_Y1       30  rule says NOT already labelled and SrLC confirms -> survives as Y=1.
                    The costly error: a false Y=1 is what this whole rule exists to stop.
  excluded_prev 30  rule says already labelled -> dropped by decision 76. The other
                    costly error: dropping a genuine new addition.
  fallback      30  baseline came from a non-primary set id (decisions 89, 91). An
                    adviser asked for these as their own stratum, because a baseline read
                    from a different registration is the likeliest source of a fake Y=1.

A sampled row is CORRECT when the matched sentence genuinely shows the event being
described as an effect of the drug in a counted section (for already_labelled = 1), or
when no such sentence exists in the counted sections (for already_labelled = 0).
"""
import sys, json, re
from pathlib import Path
import duckdb, pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import phase_b2_label_history as B
from event_match import event_match, pt_regex

SEED, OUT = 2026, ROOT / "results/phase_b2/b2_rule_handcheck.csv"
STRATA = {"random": 50, "kept_Y1": 30, "excluded_prevalent": 30, "fallback": 30}


def context(sid, ver, pt, width=170):
    """The sentence the matcher fired on, so the call can be judged from the CSV alone."""
    # baseline_version arrives as a float because the parquet column carries nulls, and
    # "..._1.0.json" matches no cached file, which silently blanked every context cell.
    if pd.isna(sid) or pd.isna(ver):
        return "", ""
    ver = int(ver)
    f = B.SEC / f"{sid}_{ver}.json"
    if not f.exists():
        return "", ""
    secs = json.loads(f.read_text())
    rx = pt_regex(pt)
    for name, text in secs.items():
        ok, _ = event_match(text, pt, rx, B.SECTION_CODE.get(name, ""))
        if ok:
            m = rx.search(text)
            if m:
                s = max(0, m.start() - width // 2)
                return name, re.sub(r"\s+", " ", text[s:m.end() + width // 2])
    return "", ""


if __name__ == "__main__":
    con = duckdb.connect()
    bl = ROOT / "data/processed/baseline_label_T1.parquet"
    if not bl.exists():
        raise SystemExit(f"missing {bl}: run phase_b2_label_history.py --step rule first")
    # LEFT JOIN, not an inner one. The rows the rule EXCLUDES (already_labelled = 1) are
    # by construction absent from confirmation_target, so an inner join emptied the
    # excluded_prevalent stratum - the one stratum that checks the 18,198 exclusions.
    # a and ror therefore come from detected_pairs, which still holds every pair; y is
    # null for an excluded row and that is the honest value.
    d = con.execute(f"""
        select b.*, t.confirmed_event_3y as y, p.a, p.ror
        from read_parquet('{bl}') b
        join (select drug_ai, pt_clean, detection_quarter, max(a) as a, max(ror) as ror
              from read_parquet('{ROOT}/data/processed/detected_pairs_T1.parquet')
              where status = 'in_window' group by 1,2,3) p
          on p.drug_ai = b.drug_ai and p.pt_clean = b.pt_clean
         and p.detection_quarter = b.detection_quarter
        left join read_parquet('{ROOT}/data/processed/confirmation_target_T1.parquet') t
          on t.drug_ai = b.drug_ai and t.pt_clean = b.pt_clean
         and t.detection_quarter = b.detection_quarter
        where b.already_labelled is not null""").df()

    # which pairs used a non-primary (fallback) baseline
    hist, sid2app = B.load_histories(), B.setid_to_apps()
    m = pd.read_csv(ROOT / "results/phase_b1/b1_drug_to_setid_clean.csv")
    d2s = {r.drug_ai: [x for x in str(r.setids).split(";") if x] for r in m.itertuples()}
    def primary(s):
        c = [(len(hist[x]), -hist[x][-1][0].value, x) for x in s if hist.get(x)]
        return max(c)[2] if c else None
    prim = {k: primary(v) for k, v in d2s.items()}
    d["is_fallback"] = d.baseline_setid != d.drug_ai.map(prim)

    pools = {
        "random": d,
        "kept_Y1": d[(d.already_labelled == 0) & (d.y == 1)],
        "excluded_prevalent": d[d.already_labelled == 1],
        "fallback": d[d.is_fallback],
    }
    rows = []
    for name, n in STRATA.items():
        p = pools[name]
        if not len(p):
            print(f"  {name}: pool empty, skipped")
            continue
        s = p.sample(min(n, len(p)), random_state=SEED)
        for r in s.itertuples():
            sec, ctx = context(r.baseline_setid, r.baseline_version, r.pt_clean)
            rows.append(dict(stratum=name, drug_ai=r.drug_ai, pt_clean=r.pt_clean,
                             detection_quarter=r.detection_quarter, a=r.a,
                             already_labelled=r.already_labelled, y_current=r.y,
                             is_fallback=int(r.is_fallback),
                             baseline_setid=r.baseline_setid, baseline_version=None if pd.isna(r.baseline_version) else int(r.baseline_version),
                             matched_section=sec, context=ctx, verdict="", reason=""))
        print(f"  {name}: {min(n, len(p))} of {len(p):,} available")
    pd.DataFrame(rows).to_csv(OUT, index=False)
    print(f"\n{len(rows)} rows -> {OUT}")
