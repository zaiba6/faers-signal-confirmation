"""Drug-level (ingredient) match: SrLC list rows  <->  FAERS drugs in detected pairs. No network. Does NOT touch events or build the target.

Run: /usr/local/bin/python3 scripts/match_srlc_ingredients.py
In : data/processed/labels/s2_probe_rows.csv (full-range query rows), data/processed/detected_pairs_T{1,2,3}.parquet (drug_ai)
Out: data/processed/labels/s2_ingredient_match.csv   one row per SrLC list row: match_level, matched faers drugs, in_scope flag
     data/processed/labels/s2_ingredient_match_summary.json
match_level (best first): exact | salt_stripped | component_any | none | blank_ingredient
  exact          full ingredient string equal
  salt_stripped  equal after removing salt/ester words and sorting components
  component_any  >=1 normalized component shared (combos / brand-vs-combo); weaker, flagged
"""
import re, json
import duckdb, pandas as pd
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "data/processed"
SALTS = r"""HYDROCHLORIDE|HYDROBROMIDE|HCL|SODIUM|POTASSIUM|CALCIUM|MAGNESIUM|SULFATE|SULPHATE|MESYLATE|MALEATE|FUMARATE|TARTRATE|SUCCINATE|BESYLATE|
ACETATE|PHOSPHATE|CITRATE|BROMIDE|CHLORIDE|NITRATE|HYDRATE|MONOHYDRATE|DIHYDRATE|ANHYDROUS|TOSYLATE|MESILATE|BITARTRATE|DIPROPIONATE|PROPIONATE|VALERATE|
DECANOATE|AXETIL|ARGININE|LYSINE|MEGLUMINE|OXALATE|LACTATE|GLUCONATE|HEMIHYDRATE|TROMETHAMINE|DISODIUM|HYDROXIDE|CARBONATE|BICARBONATE|PAMOATE|SUCCINATE|ESYLATE|HEMIFUMARATE|ETABONATE|FUROATE|RECOMBINANT|SYNTHETIC|KIT|HUMAN"""
SALT_RE = re.compile(r"\b(" + SALTS.replace("\n", "") + r")\b")


def comps(s, strip):
    s = (s or "").upper().strip()
    parts = [p.strip() for p in re.split(r"[;\\/,]|\bAND\b", s) if p.strip()]
    out = []
    for p in parts:
        if strip:
            p = SALT_RE.sub(" ", p); p = re.sub(r"\(.*?\)", " ", p)
        p = re.sub(r"[^A-Z0-9\- ]", " ", p); p = re.sub(r"\s+", " ", p).strip()
        if p: out.append(p)
    return out


def main():
    con = duckdb.connect()
    fa = con.sql(" union ".join(f"select drug_ai, '{t}' t from '{P}/detected_pairs_{t}.parquet'" for t in ("T1", "T2", "T3"))).df()
    fdrugs = fa.groupby("drug_ai").t.agg(lambda x: ",".join(sorted(set(x)))).reset_index()
    ex, nrm, comp_ix = {}, {}, {}
    for d in fdrugs.drug_ai:
        ex.setdefault(d.upper().replace("\\", ";").replace(" ;", ";"), []).append(d)
        nrm.setdefault("|".join(sorted(comps(d, True))), []).append(d)
        for c in comps(d, True): comp_ix.setdefault(c, []).append(d)
    s = pd.read_csv(P / "labels/s2_probe_rows.csv", keep_default_na=False)
    s = s[s.tag.str.startswith("full")].copy()
    res = []
    for _, r in s.iterrows():
        ai = r.active_ingredient
        if not ai.strip(): res.append(("blank_ingredient", "")); continue
        k_ex = ";".join(p.strip() for p in re.split(r"[;\\]", ai.upper())).replace(" ;", ";")
        k_n = "|".join(sorted(comps(ai, True)))
        if k_ex in ex: res.append(("exact", " || ".join(sorted(set(ex[k_ex]))))); continue
        if k_n in nrm: res.append(("salt_stripped", " || ".join(sorted(set(nrm[k_n]))))); continue
        hit = sorted({d for c in comps(ai, True) for d in comp_ix.get(c, [])})
        res.append(("component_any", " || ".join(hit[:15])) if hit else ("none", ""))
    s["match_level"], s["matched_faers_drugs"] = zip(*res)
    s["in_scope"] = s.match_level.isin(["exact", "salt_stripped", "component_any"])
    s.to_csv(P / "labels/s2_ingredient_match.csv", index=False)
    summ = dict(srlc_rows=len(s), srlc_distinct_ingredient=int(s.active_ingredient.nunique()), faers_drugs_in_detected=len(fdrugs),
                rows_by_level=s.match_level.value_counts().to_dict(),
                rows_in_scope=int(s.in_scope.sum()), pct_rows_in_scope=round(100 * s.in_scope.mean(), 1),
                rows_strict_only=int(s.match_level.isin(["exact", "salt_stripped"]).sum()),
                by_year_in_scope=s.groupby(s.supplement_date.str[-4:]).in_scope.agg(["sum", "size"]).rename(columns={"sum": "in_scope", "size": "rows"}).to_dict("index"),
                faers_drugs_matched=int(len({d for v in s.matched_faers_drugs for d in v.split(" || ") if d})))
    (P / "labels/s2_ingredient_match_summary.json").write_text(json.dumps(summ, indent=1))
    print(json.dumps({k: v for k, v in summ.items() if k != "by_year_in_scope"}, indent=1))
    print(s[s.match_level == "none"].active_ingredient.value_counts().head(25).to_string())


if __name__ == "__main__":
    main()
