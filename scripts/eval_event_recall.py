"""Estimate event-matcher RECALL with a lexically enriched sample.

Run: /usr/local/bin/python3 scripts/eval_event_recall.py sample   -> worksheet
     /usr/local/bin/python3 scripts/eval_event_recall.py score    -> reads judgements, scores

Why not a uniform draw. The candidate set is ~1.4M (signal x risk-section label change within 3
years) of which only ~0.5% match. A uniform sample of the unmatched side finds at most one or two
true misses, and extrapolating over a stratum 200x larger gives an interval (4-55%) too wide to use.

The design. Split the UNMATCHED candidates by how many of the PT's content tokens occur in the
label text:
    near   >= half the PT's content tokens present (the match should plausibly have fired)
    far    at least one present, but fewer than half
    none   no content token present
Sample each, adjudicate, and reweight by stratum size. Positives concentrate in `near`, which is
small enough to sample usefully; `none` is large but is measured rather than assumed to be empty.

Recall = TP / (TP + FN), TP from the matched stratum's precision, FN summed over the three
unmatched strata. Reads only; writes nothing that the target build owns.
"""
import re, sys, json
from pathlib import Path
import duckdb, pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from event_match import event_match, pt_regex, GENERIC_PT
from match_srlc_ingredients import comps

ROOT = Path(__file__).resolve().parents[1]
P, L, REF = ROOT / "data/processed", ROOT / "data/processed/labels", ROOT / "references"
WORK, JUDGED = REF / "recall_sample.csv", REF / "recall_judged.csv"
SIZES, OUT = REF / "recall_stratum_sizes.json", REF / "recall_eval.json"
SEED, RISK = 20261027, ("BW", "WP", "AR", "CONTRA")
N = dict(near=40, far=25, none=25)
STOP = {"of", "the", "and", "in", "to", "with", "a", "an", "or", "for", "on", "at", "by",
        "increased", "decreased", "abnormal", "disorder", "disease", "syndrome", "reaction"}


def tokens(pt):
    """Content tokens of a PT: words of 4+ characters that are not generic connectives."""
    return [w for w in re.findall(r"[a-z]{4,}", pt.lower()) if w not in STOP]


def quarter_end(q):
    return pd.Timestamp(int(q[:4]), int(q[-1]) * 3, 1) + pd.offsets.MonthEnd(0)


def candidates(con):
    from build_confirmation_target import map_drugs          # read-only use
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
    rx = {t: pt_regex(t) for t in j.pt_clean.unique()}
    j["matched"] = [event_match(x, p, rx[p], sc)[0]
                    for p, x, sc in zip(j.pt_clean, j.label_text, j.section_code)]
    return j


def stratify(j):
    toks = {t: tokens(t) for t in j.pt_clean.unique()}
    frac = []
    for pt, txt in zip(j.pt_clean, j.label_text):
        tk = toks[pt]
        if not tk:
            frac.append(0.0); continue
        low = txt.lower()
        hit = sum(1 for w in tk if re.search(r"\b" + re.escape(w) + r"\w*", low))
        frac.append(hit / len(tk))
    j = j.assign(tok_frac=frac)
    j["stratum"] = "matched"
    u = ~j.matched
    j.loc[u & (j.tok_frac >= .5), "stratum"] = "near"
    j.loc[u & (j.tok_frac > 0) & (j.tok_frac < .5), "stratum"] = "far"
    j.loc[u & (j.tok_frac == 0), "stratum"] = "none"
    return j


def evidence(row, width=300):
    """Text around the best token hit, else the opening of the change."""
    tk = tokens(row.pt_clean)
    low = row.label_text.lower()
    for w in sorted(tk, key=len, reverse=True):
        m = re.search(r"\b" + re.escape(w) + r"\w*", low)
        if m:
            s = max(0, m.start() - width // 2)
            return ("..." if s else "") + row.label_text[s:m.end() + width] + "..."
    return row.label_text[:width * 2] + ("..." if len(row.label_text) > width * 2 else "")


def do_sample():
    con = duckdb.connect()
    j = stratify(candidates(con))
    sizes = {k: int((j.stratum == k).sum()) for k in ("matched", "near", "far", "none")}
    sizes["total"] = len(j)
    rows = []
    for st, n in N.items():
        sub = j[j.stratum == st]
        if not len(sub):
            continue
        s = sub.sample(min(n, len(sub)), random_state=SEED).copy()
        s["evidence"] = [evidence(r) for r in s.itertuples()]
        rows.append(s)
    out = pd.concat(rows, ignore_index=True)[
        ["stratum", "tok_frac", "drug_name", "pt_clean", "detection_quarter", "cd",
         "section_code", "is_stub", "evidence"]]
    out["judgement"] = ""          # yes = this change really does confirm this event
    out["judge_note"] = ""
    out.insert(0, "id", range(1, len(out) + 1))
    out.to_csv(WORK, index=False)
    SIZES.write_text(json.dumps(sizes, indent=1))
    print(json.dumps(sizes, indent=1))
    print("sampled:", out.stratum.value_counts().to_dict(), "->", WORK.relative_to(ROOT))


def wilson(k, n, z=1.96):
    if not n:
        return (0.0, 0.0)
    p = k / n; dd = 1 + z * z / n
    c = (p + z * z / (2 * n)) / dd
    h = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** .5) / dd
    return max(0.0, c - h), min(1.0, c + h)


def do_score():
    d = pd.read_csv(JUDGED, keep_default_na=False)
    d = d[d.judgement.isin(["yes", "no"])]
    sizes = json.loads(SIZES.read_text())
    prec = float(json.loads((REF / "event_matcher_eval_v2.json").read_text())["precision_pct"]) / 100 \
        if (REF / "event_matcher_eval_v2.json").exists() else 0.92
    TP = prec * sizes["matched"]
    fn, lo_fn, hi_fn, per = 0.0, 0.0, 0.0, {}
    for st in ("near", "far", "none"):
        sub = d[d.stratum == st]
        k, n = int((sub.judgement == "yes").sum()), len(sub)
        r = k / n if n else 0.0
        lo, hi = wilson(k, n)
        fn += r * sizes[st]; lo_fn += lo * sizes[st]; hi_fn += hi * sizes[st]
        per[st] = dict(judged=n, true_confirmations=k, rate_pct=round(100 * r, 1),
                       stratum_size=sizes[st], implied_misses=int(r * sizes[st]),
                       implied_misses_ci=[int(lo * sizes[st]), int(hi * sizes[st])])
    res = dict(
        method="lexically enriched stratified sample; strata by fraction of PT content tokens present",
        precision_used_for_TP=round(100 * prec, 1), stratum_sizes=sizes, per_stratum=per,
        estimated_TP=int(TP), estimated_FN=int(fn),
        recall_pct=round(100 * TP / (TP + fn), 1) if TP + fn else None,
        recall_ci95=[round(100 * TP / (TP + hi_fn), 1) if TP + hi_fn else None,
                     round(100 * TP / (TP + lo_fn), 1) if TP + lo_fn else None],
        note="CI propagates only the per-stratum binomial uncertainty; it does not cover "
             "adjudication error or the assumption that strata are exhaustive.")
    OUT.write_text(json.dumps(res, indent=1))
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    {"sample": do_sample, "score": do_score}[sys.argv[1] if len(sys.argv) > 1 else "sample"]()
