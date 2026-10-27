"""Draw a stratified sample of (detected pair x label change) candidates for hand-adjudication
of the event matcher, and - once judgements are filled in - score precision and recall.

Run: /usr/local/bin/python3 scripts/eval_event_matcher.py sample   -> writes the worksheet
     /usr/local/bin/python3 scripts/eval_event_matcher.py score    -> reads judgements, scores

Candidate set = every (pair, risk-section SrLC change for that drug within 3 years of detection)
for rule T1, which is the widest. The matcher's job on each candidate is a yes/no: does this label
change address THIS pair's event? Strata are the matcher's own answer, so precision comes from the
matched stratum and recall needs both, reweighted by stratum size.
"""
import re, sys, json
from pathlib import Path
import duckdb, pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from match_srlc_ingredients import comps

ROOT = Path(__file__).resolve().parents[1]
P, L = ROOT / "data/processed", ROOT / "data/processed/labels"
REF = ROOT / "references"
WORK = REF / "event_matcher_sample.csv"
JUDGED = REF / "event_matcher_judged.csv"
N_PER_STRATUM = 50
SEED = 20261026
RISK = ("BW", "WP", "AR", "CONTRA")


def quarter_end(q):
    return pd.Timestamp(int(q[:4]), int(q[-1]) * 3, 1) + pd.offsets.MonthEnd(0)


def candidates(con):
    from build_confirmation_target import map_drugs
    changes, _ = map_drugs(con)
    con.register("ch", changes)
    pairs = con.execute(f"""select drug_ai, pt_clean, detection_quarter
        from read_parquet('{P}/detected_pairs_T1.parquet') where status='in_window'""").df()
    pairs["detect_dt"] = pairs.detection_quarter.map(quarter_end)
    con.register("pr", pairs)
    j = con.execute("""
        select pr.drug_ai, pr.pt_clean, pr.detection_quarter, ch.change_date_iso cd,
               ch.section_code, ch.drug_name, ch.is_stub, ch.label_text
        from pr join ch on ch.drug_ai = pr.drug_ai and ch.is_risk
        where ch.change_date_iso::date > pr.detect_dt
          and ch.change_date_iso::date <= pr.detect_dt + interval '3 years'""").df()
    pt_rx = {t: re.compile(r"\b" + re.escape(t.rstrip("s") if len(t) > 4 and t.endswith("s") else t) + r"s?\b", re.I)
             for t in j.pt_clean.unique()}
    j["matched"] = [bool(pt_rx[p].search(x)) for p, x in zip(j.pt_clean, j.label_text)]
    j["rx"] = j.pt_clean.map(lambda t: pt_rx[t])
    return j


def window(row, width=260):
    """Text around the match (matched rows) or the opening of the change (unmatched)."""
    m = row.rx.search(row.label_text)
    if m:
        s = max(0, m.start() - width // 2)
        return ("..." if s else "") + row.label_text[s:m.end() + width // 2] + "..."
    return row.label_text[:width * 2] + ("..." if len(row.label_text) > width * 2 else "")


def do_sample():
    con = duckdb.connect()
    j = candidates(con)
    sizes = dict(matched=int(j.matched.sum()), unmatched=int((~j.matched).sum()), total=len(j))
    rows = []
    for stratum, sub in [("matched", j[j.matched]), ("unmatched", j[~j.matched])]:
        s = sub.sample(min(N_PER_STRATUM, len(sub)), random_state=SEED).copy()
        s["stratum"] = stratum
        s["evidence"] = [window(r) for r in s.itertuples()]
        rows.append(s)
    out = pd.concat(rows, ignore_index=True)[
        ["stratum", "drug_ai", "drug_name", "pt_clean", "detection_quarter", "cd", "section_code", "is_stub", "evidence"]]
    out["judgement"] = ""          # to fill: yes | no | unclear
    out["judge_note"] = ""
    out.insert(0, "id", range(1, len(out) + 1))
    REF.mkdir(exist_ok=True)
    out.to_csv(WORK, index=False)
    (REF / "event_matcher_sample_sizes.json").write_text(json.dumps(sizes, indent=1))
    print(json.dumps(sizes, indent=1))
    print("worksheet:", WORK.relative_to(ROOT), len(out), "rows")


def wilson(k, n, z=1.96):
    if n == 0:
        return (None, None)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** .5) / d
    return round(100 * max(0, c - h), 1), round(100 * min(1, c + h), 1)


def do_score():
    d = pd.read_csv(JUDGED, keep_default_na=False)
    sizes = json.loads((REF / "event_matcher_sample_sizes.json").read_text())
    d = d[d.judgement.isin(["yes", "no"])]
    m, u = d[d.stratum == "matched"], d[d.stratum == "unmatched"]
    tp_r, fn_r = (m.judgement == "yes").mean(), (u.judgement == "yes").mean()
    TP, FP = tp_r * sizes["matched"], (1 - tp_r) * sizes["matched"]
    FN = fn_r * sizes["unmatched"]
    prec, rec = TP / (TP + FP), TP / (TP + FN) if (TP + FN) else float("nan")
    res = dict(
        judged=dict(matched=len(m), unmatched=len(u)),
        stratum_sizes=sizes,
        matched_stratum_true_rate=round(100 * tp_r, 1),
        unmatched_stratum_true_rate=round(100 * fn_r, 2),
        precision_pct=round(100 * prec, 1), precision_ci95=wilson((m.judgement == "yes").sum(), len(m)),
        recall_pct=round(100 * rec, 1),
        estimated_true_positives_in_candidate_set=int(TP + FN),
        note="Recall is extrapolated: the unmatched stratum is ~%.0fx larger than the matched one, so each "
             "missed case in the sample stands for many." % (sizes["unmatched"] / max(sizes["matched"], 1)))
    # Propagate the uncertainty in the unmatched stratum through to recall.
    lo, hi = wilson(int((u.judgement == "yes").sum()), len(u))
    rec_hi = TP / (TP + (lo / 100) * sizes["unmatched"])          # few misses -> high recall
    rec_lo = TP / (TP + (hi / 100) * sizes["unmatched"])
    res["unmatched_true_rate_ci95"] = [lo, hi]
    res["recall_ci95"] = [round(100 * rec_lo, 1), round(100 * rec_hi, 1)]
    res["verdict"] = ("Precision is well estimated. Recall is NOT: it rests on %d positive(s) in %d judged "
                      "unmatched rows, and the interval spans %.0f-%.0f%%, which is too wide to act on."
                      % (int((u.judgement == "yes").sum()), len(u), 100 * rec_lo, 100 * rec_hi))
    (REF / "event_matcher_eval.json").write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    {"sample": do_sample, "score": do_score}[sys.argv[1] if len(sys.argv) > 1 else "sample"]()
