"""Phase B1: map cohort drug_ai strings to DailyMed set ids, NDA and BLA only. No version downloads.

Run: /usr/local/bin/python3 scripts/phase_b1_map_setids.py [--step index|spls|match|all]

Why this route. The v2 API accepts `marketing_category` and IGNORES it: PANTOPRAZOLE returns the
same 207 SPLs whether you ask for NDA, ANDA or BLA, and that equals the unfiltered total. The only
honest way to restrict to NDA and BLA is to start from the application-number index, which carries
a marketing category per application number, and pull the set ids registered under each one. Those
responses also carry the SPL title, which holds the brand and the ingredient, so the ingredient
match is done locally with no extra requests.

Politeness: one request per second, every response cached under data/raw/dailymed/ so a rerun
resumes rather than refetches.
"""
import sys, json, re, time
from pathlib import Path
import requests, duckdb, pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
from match_srlc_ingredients import comps

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/dailymed"; RAW.mkdir(parents=True, exist_ok=True)
OUT = ROOT / "results/phase_b1"; OUT.mkdir(parents=True, exist_ok=True)
B = "https://dailymed.nlm.nih.gov/dailymed/services/v2/"
UA = {"User-Agent": "faers-signal-confirmation-research/0.1 (academic research; contact via repo)"}
DELAY = 1.0
KEEP = re.compile(r"^(NDA|BLA)\d", re.I)          # excludes ANDA and ANADA, which start with A
_last = [0.0]


def get(path, **params):
    w = DELAY - (time.monotonic() - _last[0])
    if w > 0:
        time.sleep(w)
    try:
        r = requests.get(B + path, params=params, headers=UA, timeout=60)
    finally:
        _last[0] = time.monotonic()
    r.raise_for_status()
    return r.json()


def step_index():
    """Page the whole application-number index once."""
    f = RAW / "application_numbers.json"
    if f.exists():
        return json.loads(f.read_text())
    out, page = [], 1
    while True:
        j = get("applicationnumbers.json", pagesize=100, page=page)
        out += j["data"]
        md = j["metadata"]
        print(f"  index page {page}/{md['total_pages']} ({len(out)} rows)", flush=True)
        if page >= md["total_pages"]:
            break
        page += 1
    f.write_text(json.dumps(out))
    return out


def step_spls(apps):
    """For each NDA/BLA application number, list its set ids. Cached one file per application."""
    d = RAW / "spls_by_application"; d.mkdir(exist_ok=True)
    for i, a in enumerate(apps, 1):
        f = d / f"{re.sub(r'[^A-Za-z0-9]', '_', a)}.json"
        if f.exists():
            continue
        rows, page = [], 1
        while True:
            try:
                j = get("spls.json", application_number=a, pagesize=100, page=page)
            except Exception as e:
                print(f"  ERR {a}: {type(e).__name__}", flush=True)
                rows = None
                break
            rows += j["data"]
            md = j["metadata"]
            if page >= md["total_pages"] or not j["data"]:
                break
            page += 1
        if rows is not None:
            f.write_text(json.dumps(rows))
        if i % 100 == 0:
            print(f"  {i}/{len(apps)} applications", flush=True)


# Fix A, error class A of the B1 gate: the shared salt stripper removes ester and prodrug
# moieties as if they were counterions. They are not. METOPROLOL SUCCINATE and METOPROLOL
# TARTRATE, BETAMETHASONE SODIUM PHOSPHATE and BETAMETHASONE DIPROPIONATE, HYDROCORTISONE
# VALERATE and HYDROCORTISONE ACETATE are different products with different labels, so
# collapsing them answers "was this event already listed" from the wrong label.
# Counterions (sodium, hydrochloride, maleate, ...) stay strippable: they do not change the
# active moiety and the label may write the name either way.
ESTER = re.compile(r"\b(ACETATE|DIACETATE|PHOSPHATE|VALERATE|DIPROPIONATE|PROPIONATE|"
                   r"DECANOATE|ENANTHATE|CYPIONATE|UNDECANOATE|PALMITATE|CAPROATE|HEXANOATE|"
                   r"BUTYRATE|PIVALATE|FUROATE|ETABONATE|AXETIL)\b")


def clean(s):
    """Punctuation-normalised, nothing removed."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9\- ]", " ", (s or "").upper())).strip()


# Counterions the shared SALTS list does not carry. Added locally rather than to
# match_srlc_ingredients.SALTS so this fix cannot move the confirmation target.
# Only true counterions: DERUXTECAN, CARBOXYMALTOSE, IMMUNE GLOBULIN and the like are
# part of the drug name and are deliberately absent.
EXTRA_SALT = re.compile(r"\b(S-MALATE|MALATE|CAMSYLATE|DITOSYLATE|DIASPARTATE|DIETHYLAMINE)\b")


def strip_counterions(p):
    """comps() stripping, but any ester/prodrug token it would have removed is put back."""
    p = EXTRA_SALT.sub(" ", p.upper())
    keep = ESTER.findall(p)
    out = comps(p, True)
    base = out[0] if out else clean(p)
    for e in keep:
        if e not in base.split():
            base = f"{base} {e}"
    return base.strip()


def norm_key(s):
    return "|".join(sorted(strip_counterions(p) for p in re.split(r"[;\\/,]| AND ", str(s).upper()) if p.strip()))


def step_match():
    con = duckdb.connect()
    cohort = con.execute(f"""select drug_ai, count(*) as signals
        from read_parquet('{ROOT}/data/processed/confirmation_target_T1.parquet')
        group by 1 order by signals desc""").df()

    apps = {a["application_number"]: a["marketing_category_code"] for a in step_index()}
    d = RAW / "spls_by_application"
    recs = []
    for f in d.glob("*.json"):
        app = f.stem
        for r in json.loads(f.read_text()):
            recs.append(dict(application=app, setid=r["setid"], title=r.get("title", "")))
    spl = pd.DataFrame(recs).drop_duplicates("setid")

    # The title looks like "BRAND (INGREDIENT) FORM [LABELER]"; the parenthesised part is the
    # ingredient list. Fall back to the leading token when there are no parentheses.
    def ingredients(t):
        m = re.search(r"\(([^)]*)\)", t or "")
        base = m.group(1) if m else (t or "").split("[")[0]
        return [p.strip() for p in re.split(r"[;,/]| AND ", base.upper()) if p.strip()]

    idx_exact, idx_norm, idx_comp = {}, {}, {}
    for r in spl.itertuples():
        for ing in ingredients(r.title):
            idx_exact.setdefault(ing, set()).add(r.setid)
            idx_norm.setdefault(norm_key(ing), set()).add(r.setid)
            for c in comps(ing, True):
                idx_comp.setdefault(c, set()).add(r.setid)

    # Combination rule. A label's safety sections describe one product as a whole, so a pair may
    # only be checked against a label for the SAME product. Concretely:
    #   single-ingredient drug_ai -> labels whose ingredient list contains that ingredient,
    #                                preferring labels that list it alone
    #   combination drug_ai       -> only labels that list EVERY component (component_all)
    # A combination is never silently merged into a single-ingredient label, and a single
    # ingredient is only mapped to a combination label when no single-ingredient label exists,
    # which is flagged as combo_fallback rather than counted as a clean match. Mapping
    # SACUBITRIL\\VALSARTAN to a valsartan-only label would answer "was this already listed"
    # from the wrong product.
    # Fix B, error class B of the B1 gate: requiring every component of the pair's drug to
    # appear in the label did not forbid the label carrying EXTRA ingredients, so
    # ABACAVIR\\LAMIVUDINE mapped onto TRIUMEQ (adds dolutegravir), FEXOFENADINE onto
    # ALLEGRA-D (adds pseudoephedrine) and AMLODIPINE\\TELMISARTAN onto WIDAPLIK (adds
    # indapamide). A larger fixed-dose product has its own label and its own safety sections.
    # by_set keys a label by its WHOLE normalised ingredient set, so equality can be required.
    single_only, single_raw, by_set = {}, {}, {}
    for r in spl.itertuples():
        ings = ingredients(r.title)
        by_set.setdefault(frozenset(norm_key(i) for i in ings), set()).add(r.setid)
        if len(ings) == 1:
            single_only.setdefault(norm_key(ings[0]), set()).add(r.setid)
            single_raw.setdefault(clean(ings[0]), set()).add(r.setid)

    rows = []
    for r in cohort.itertuples():
        name = r.drug_ai.upper()
        parts = [p for p in name.split("\\") if p.strip()]
        is_combo = len(parts) > 1
        hits, level = set(), "none"
        if is_combo:
            want = frozenset(norm_key(p) for p in parts)
            if want in by_set:
                hits, level = by_set[want], "combo_exact_set"
            else:
                # no label lists exactly these ingredients; fall back to a superset label but
                # flag it, never counting it as a clean match
                sets = [idx_norm.get(norm_key(p), set()) for p in parts]
                hits = set.intersection(*sets) if sets and all(sets) else set()
                level = "combo_superset" if hits else "none"
        else:
            # Fix A, tier order: a label that spells the salt exactly as FAERS does beats a
            # label reached only by stripping. Without this, METOPROLOL SUCCINATE is normalised
            # to METOPROLOL and collects LOPRESSOR (tartrate) alongside TOPROL XL (succinate).
            if clean(name) in single_raw:
                hits, level = single_raw[clean(name)], "single_exact_salt"
            elif norm_key(name) in single_only:
                hits, level = single_only[norm_key(name)], "single_exact"
            elif name in idx_exact:
                hits, level = idx_exact[name], "exact_in_any_label"
            elif norm_key(name) in idx_norm:
                hits, level = idx_norm[norm_key(name)], "salt_stripped"
            if hits and level in ("exact_in_any_label", "salt_stripped"):
                # it only appears alongside other ingredients
                if not (norm_key(name) in single_only):
                    level = "combo_fallback"
        rows.append(dict(drug_ai=r.drug_ai, signals=r.signals, is_combo=int(is_combo),
                         match_level=level, n_setids=len(hits),
                         setids=";".join(sorted(hits)[:50])))
    m = pd.DataFrame(rows)
    # A clean mapping is one where the label is for the SAME product. combo_superset and
    # combo_fallback reach a different product and are flagged, not counted clean.
    CLEAN = {"single_exact_salt", "single_exact", "combo_exact_set"}
    m["is_clean"] = m.match_level.isin(CLEAN).astype(int)
    m.to_csv(OUT / "b1_drug_to_setid.csv", index=False)

    tot_sig = int(m.signals.sum()); tot_drug = len(m)
    mapped = m[m.n_setids > 0]
    summary = dict(
        cohort_signals=tot_sig, cohort_drugs=tot_drug,
        nda_bla_applications=len([a for a in apps if KEEP.match(a)]),
        setids_in_universe=int(len(spl)),
        drugs_mapped=int(len(mapped)), drugs_mapped_pct=round(100 * len(mapped) / tot_drug, 1),
        signals_mapped=int(mapped.signals.sum()),
        signals_mapped_pct=round(100 * mapped.signals.sum() / tot_sig, 1),
        by_level={k: dict(drugs=int(v.drug_ai.count()), signals=int(v.signals.sum()))
                  for k, v in m.groupby("match_level")},
        combos=dict(drugs=int(m.is_combo.sum()), signals=int(m[m.is_combo == 1].signals.sum()),
                    mapped_signals=int(m[(m.is_combo == 1) & (m.n_setids > 0)].signals.sum())),
        coverage_by_detection_year="see b1_coverage_by_year.csv",
        top20_unmapped=m[m.n_setids == 0].nlargest(20, "signals")[["drug_ai", "signals"]].to_dict("records"))
    # coverage by detection year, weighted by signals
    cov = con.execute(f"""select substr(detection_quarter,1,4) as year, drug_ai, count(*) as signals
        from read_parquet('{ROOT}/data/processed/confirmation_target_T1.parquet')
        group by 1,2""").df().merge(m[["drug_ai", "n_setids"]], on="drug_ai", how="left")
    cov["mapped"] = (cov.n_setids.fillna(0) > 0).astype(int)
    by_year = cov.groupby("year").apply(
        lambda g: pd.Series(dict(signals=int(g.signals.sum()),
                                 mapped_signals=int((g.signals * g.mapped).sum()),
                                 mapped_pct=round(100 * (g.signals * g.mapped).sum() / g.signals.sum(), 1))),
        include_groups=False).reset_index()
    by_year.to_csv(OUT / "b1_coverage_by_year.csv", index=False)
    print(by_year.to_string(index=False))
    (OUT / "b1_summary.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    step = sys.argv[sys.argv.index("--step") + 1] if "--step" in sys.argv else "all"
    if step in ("index", "all"):
        apps = step_index()
        keep = sorted(a["application_number"] for a in apps if KEEP.match(a["application_number"]))
        print(f"application numbers: {len(apps):,} total, {len(keep):,} NDA/BLA", flush=True)
        (OUT / "b1_applications.json").write_text(json.dumps(keep))
    if step in ("spls", "all"):
        keep = json.loads((OUT / "b1_applications.json").read_text())
        step_spls(keep)
    if step in ("match", "all"):
        step_match()
