#!/usr/bin/env python3
"""Read-only spot checks against the raw FAERS files (nothing under data/raw/ or the DuckDB is modified).

  python scripts/spot_checks.py sanity1    -> references/sanity_check_1.csv   (1 random DEMO row per quarter: age, sex, country)
  python scripts/spot_checks.py rules      -> references/spot_checks_6_7_8.md (drug / event term / outcome rules + counts)
Every sampled row is shown next to the original raw text line, found by searching the raw file itself.
"""
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "interim" / "faers_raw.duckdb"
INV = json.loads((ROOT / "references" / "profile" / "profile_all.json").read_text())["inventory"]["files"]
SEED = 0.42
CODES = ["DE", "HO", "LT", "DS", "CA", "RI", "OT"]


def clean(x):
    """trim, uppercase, collapse internal whitespace (decision 6)"""
    return f"upper(trim(regexp_replace({x}, '\\s+', ' ', 'g')))"


def raw_lines(requests, multi=False):
    """requests: [(key, table, quarter, predicate(line) -> bool)]. Search the raw files (latin-1 decoded).
    Returns {key: first matching line} or, with multi=True, {key: [all matching lines]}."""
    by_file = defaultdict(list)
    for key, t, q, pred in requests:
        by_file[INV[q][t]].append((key, pred))
    out = defaultdict(list) if multi else {}
    for rel, items in by_file.items():
        pending = dict(items)
        with open(RAW / rel, "rb") as f:
            for line in f:
                s = line.decode("latin-1").rstrip("\r\n")
                for key, pred in list(pending.items()):
                    if pred(s):
                        if multi:
                            out[key].append(s)
                        else:
                            out[key] = s
                            del pending[key]
                if not pending:
                    break
    return out


def connect():
    con = duckdb.connect(str(DB), read_only=True)
    con.execute("SET memory_limit='8GB'")
    con.execute(f"SELECT setseed({SEED})")
    return con


def md_table(header, rows):
    esc = lambda v: str(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return "\n".join(lines + ["| " + " | ".join(esc(v) for v in r) + " |" for r in rows])


def starts(*parts):
    prefix = "$".join(str(p) for p in parts) + "$"
    return lambda s: s.startswith(prefix)


# --------------------------------------------------------------------------- sanity check 1
def sanity1():
    con = connect()
    rows = con.execute("""
      WITH pick AS (SELECT primaryid, _q, row_number() OVER (PARTITION BY _q ORDER BY hash(primaryid, 42)) rn FROM demo_clean)
      SELECT p._q, d.primaryid, d.caseid, d.caseversion, d.age, d.age_cod, c.age_years, d.sex, d.gndr_cod, c.sex,
             d.reporter_country, c.reporter_country, c.us_flag
      FROM pick p JOIN demo d ON d.primaryid = p.primaryid AND d._q = p._q
                  JOIN demo_clean c ON c.primaryid = p.primaryid AND c._q = p._q
      WHERE p.rn = 1 ORDER BY p._q""").fetchall()
    cols = ["quarter", "primaryid", "caseid", "caseversion", "age_raw", "age_cod_raw", "age_years_clean", "sex_raw", "gndr_cod_raw",
            "sex_clean", "country_raw", "country_clean", "us_flag"]
    raw = raw_lines([((r[0], r[1]), "demo", r[0], starts(r[1])) for r in rows])
    out = ROOT / "references" / "sanity_check_1.csv"
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols + ["source_file", "raw_line"])
        for r in rows:
            w.writerow(list(r) + [INV[r[0]]["demo"], raw.get((r[0], r[1]), "NOT FOUND")])
    print(f"wrote {out} ({len(rows)} rows); raw lines found: {sum(1 for r in rows if (r[0], r[1]) in raw)}")



# --------------------------------------------------------------------------- sanity check 2 (targeted rows)
UNIT_FACTOR = {"DEC": 10.0, "YR": 1.0, "MON": 1 / 12, "WK": 7 / 365.25, "DY": 1 / 365.25, "HR": 1 / 8766, "MIN": 1 / 525960, "SEC": 1 / 31557600}


def expected_demo(raw_line, cols):
    """Independent re-implementation of the demographic rules, working only from the raw text line."""
    f = dict(zip(cols, raw_line.split("$")))
    out = {}
    age, cod = f.get("age", "").strip(), f.get("age_cod", "").strip().upper()
    try:
        a = float(age)
        a = a if a == a and abs(a) != float("inf") else None
    except ValueError:
        a = None
    yrs = a * UNIT_FACTOR[cod] if a is not None and cod in UNIT_FACTOR else None
    out["age_years_clean"] = yrs if yrs is not None and 0 <= yrs <= 120 else None
    sx = (f.get("sex", "").strip() or f.get("gndr_cod", "").strip()).upper()
    out["sex_clean"] = sx if sx in ("F", "M") else "UNK"
    c = f.get("reporter_country", "").strip().upper()
    c = None if c in ("", "COUNTRY NOT SPECIFIED", "EU") else c
    out["country_clean"] = c
    out["us_flag"] = None if c is None else int(c == "US")
    return out


def same(exp, got):
    if exp is None or got in (None, ""):
        return exp is None and got in (None, "")
    try:
        return abs(float(exp) - float(got)) < 1e-9
    except (TypeError, ValueError):
        return str(exp) == str(got)


def check_rows(rows, cols_by_q, cleaned):
    """rows: [(quarter, raw_line)]; cleaned: [dict of cleaned values]. Returns list of 'OK' / mismatch text."""
    res = []
    for (q, raw), got in zip(rows, cleaned):
        exp = expected_demo(raw, cols_by_q[q])
        bad = [k for k in exp if not same(exp[k], got[k])]
        res.append("OK" if not bad else "MISMATCH " + ", ".join(f"{k}: expected {exp[k]!r} got {got[k]!r}" for k in bad))
    return res


def sanity2(table="demo_clean", out_name="sanity_check_2.csv"):
    con = connect()
    hdr = json.loads((ROOT / "references" / "profile" / "profile_all.json").read_text())["header_columns"]["demo"]
    cols_by_q = {q: [c for c in cs] for q, cs in hdr.items()}
    fac = " ".join(f"WHEN '{k}' THEN {v!r}" for k, v in UNIT_FACTOR.items())
    yrs = f"(TRY_CAST(trim(d.age) AS DOUBLE) * CASE upper(trim(d.age_cod)) {fac} END)"
    num = "isfinite(coalesce(TRY_CAST(trim(d.age) AS DOUBLE), 'NaN'::DOUBLE))"
    cats = [(f"age_cod={u}", f"{num} AND upper(trim(d.age_cod)) = '{u}'", 5) for u in ("DEC", "MON", "WK", "DY", "HR")]
    cats += [("age present, age_cod blank", f"{num} AND trim(coalesce(d.age_cod, '')) = ''", 5),
             ("age out of range: negative", f"{yrs} < 0", 3), ("age out of range: above 120", f"{yrs} > 120", 2),
             ("age non-numeric", f"trim(coalesce(d.age, '')) <> '' AND NOT {num}", 5),
             ("sex NS (gndr_cod)", "upper(trim(d.gndr_cod)) = 'NS'", 5)]
    cats += [(f"sex {c}", f"upper(trim(d.sex)) = '{c}'", 5) for c in ("P", "I", "T")]
    cats += [("reporter_country EU", "upper(trim(d.reporter_country)) = 'EU'", 5),
             ("reporter_country blank", "trim(coalesce(d.reporter_country, '')) = ''", 5),
             ("reporter_country COUNTRY NOT SPECIFIED (extra)", "upper(trim(d.reporter_country)) = 'COUNTRY NOT SPECIFIED'", 2)]
    header = ["category", "quarter", "primaryid", "caseid", "caseversion", "age_raw", "age_cod_raw", "age_years_clean", "sex_raw", "gndr_cod_raw",
              "sex_clean", "country_raw", "country_clean", "us_flag"]
    picked, notes = [], []
    for name, where, n in cats:
        rows = con.execute(f"""SELECT '{name}', d._q, d.primaryid, d.caseid, d.caseversion, d.age, d.age_cod, c.age_years, d.sex, d.gndr_cod, c.sex,
              d.reporter_country, c.reporter_country, c.us_flag
            FROM demo d JOIN {table} c ON c.primaryid = d.primaryid AND c._q = d._q WHERE {where} ORDER BY hash(d.primaryid, 7) LIMIT {n}""").fetchall()
        tot = con.execute(f"SELECT count(*) FROM demo d WHERE {where}").fetchone()[0]
        notes.append((name, len(rows), tot))
        picked += rows
    raw = raw_lines([((r[1], r[2]), "demo", r[1], starts(r[2])) for r in picked])
    raws = [(r[1], raw.get((r[1], r[2]), "")) for r in picked]
    cleaned = [{"age_years_clean": r[7], "sex_clean": r[10], "country_clean": r[12], "us_flag": r[13]} for r in picked]
    checks = check_rows(raws, cols_by_q, cleaned)
    out = ROOT / "references" / out_name
    with open(out, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header + ["source_file", "raw_line", "code_check"])
        for r, (q, rl), ck in zip(picked, raws, checks):
            w.writerow(list(r) + [INV[q]["demo"], rl or "NOT FOUND", ck])
    print(f"wrote {out}: {len(picked)} rows, OK {checks.count('OK')}, mismatches {len(checks) - checks.count('OK')}")
    for name, got, tot in notes:
        print(f"  {name}: {got} rows (exist in total: {tot:,})")
    for r, ck in zip(picked, checks):
        if ck != "OK":
            print("  ", r[0], r[1], r[2], ck)


# --------------------------------------------------------------------------- rules 6, 7, 8
def rules():
    con = connect()
    o = []
    w = o.append
    q1 = lambda sql: con.execute(sql).fetchone()[0]
    con.execute("""CREATE TEMP TABLE kept AS SELECT primaryid, caseid, init_fda_dt, _q FROM (
        SELECT *, row_number() OVER (PARTITION BY caseid ORDER BY TRY_CAST(caseversion AS INT) DESC, _q DESC) rn FROM demo) WHERE rn = 1""")
    con.execute("CREATE TEMP TABLE kept_eff AS SELECT * FROM kept WHERE _q >= '2014Q3'")
    nk, ne = q1("SELECT count(*) FROM kept"), q1("SELECT count(*) FROM kept_eff")
    early = q1("SELECT count(*) FROM kept_eff WHERE init_fda_dt < '20140701'")
    w("# Spot checks: drug identity, event term, outcome rules (read-only; generated by scripts/spot_checks.py)\n")
    w("Every table row shows the original raw text line (latin-1 decoded, found by searching the raw file) next to what the rule produces.\n")
    w("These checks were run before the case-table build to validate decisions 8-12 (drug, event term, outcomes) in `DECISIONS.md`. Counts here use the case set before deleted-case removal (16,265,596 cases); the final numbers are in `references/profile/case_table_build_report.json`.\n")
    w("## Case universe used for the counts below\n")
    w(f"- Kept case list (latest `caseversion` per `caseid`): {nk:,} cases.")
    w(f"- {ne:,} of them have their kept version in a file from 2014Q3 or later (the quarters that carry `prod_ai`); {nk - ne:,} have their latest version in 2012Q4-2014Q2 and drop out of the drug-event counts.")
    w(f"- Of the {ne:,} retained cases, {early:,} have `init_fda_dt` before 2014-07-01 (first received earlier, latest version in 2014Q3+). They are retained and placed in their `init_fda_dt` quarter. **Assumption to confirm:** 'counts start at 2014Q3' is read as a filter on the *source file* of the kept version, not on `init_fda_dt`.\n")

    # ---------------- 6. drug identity
    con.execute(f"""CREATE TEMP TABLE ps AS
        SELECT primaryid, caseid, drug_seq, _q, prod_ai, drugname, ai_clean, dn_clean,
               coalesce(ai_clean, dn_clean) AS drug_name,
               CASE WHEN ai_clean IS NOT NULL THEN 'prod_ai' WHEN dn_clean IS NOT NULL THEN 'drugname_fallback' END AS ai_source
        FROM (SELECT d.primaryid, d.caseid, d.drug_seq, d._q, d.prod_ai, d.drugname,
                     nullif({clean('d.prod_ai')}, '') AS ai_clean, nullif({clean('d.drugname')}, '') AS dn_clean
              FROM drug d JOIN kept_eff k ON k.primaryid = d.primaryid AND k._q = d._q WHERE d.role_cod = 'PS')""")
    n_ps = q1("SELECT count(*) FROM ps")
    fb = q1("SELECT count(*) FROM ps WHERE ai_source = 'drugname_fallback'")
    nn = q1("SELECT count(*) FROM ps WHERE drug_name IS NULL")
    all_rows = q1("SELECT count(*) FROM drug WHERE _q >= '2014Q3'")
    all_blank = q1("SELECT count(*) FROM drug WHERE _q >= '2014Q3' AND (prod_ai IS NULL OR trim(prod_ai) = '')")
    comb = "(drug_name LIKE '%,%' OR drug_name LIKE '%;%')"
    cr = q1(f"SELECT count(*) FROM ps WHERE {comb}")
    w("## 6. Drug identity\n")
    w(f"- PS rows in retained cases: **{n_ps:,}** ({q1('SELECT count(DISTINCT primaryid) FROM ps'):,} cases).")
    w(f"- **`ai_source = drugname_fallback` rows: {fb:,}** ({100 * fb / n_ps:.2f}% of PS rows); `prod_ai` rows: {n_ps - fb - nn:,}; rows with neither (drug_name missing): {nn:,}.")
    w(f"- For comparison, all DRUG rows from 2014Q3 on (any role): {all_blank:,} of {all_rows:,} have blank `prod_ai` ({100 * all_blank / all_rows:.2f}%; the 'about 2%' in the instruction).")
    w(f"- Distinct cleaned drug strings among PS rows: {q1('SELECT count(DISTINCT drug_name) FROM ps'):,} (distinct raw `prod_ai` strings in the same rows: {q1('SELECT count(DISTINCT prod_ai) FROM ps'):,}).")
    w(f"- **Combination-looking values (contain `,` or `;`): {cr:,} PS rows ({100 * cr / n_ps:.2f}%), "
      f"{q1(f'SELECT count(DISTINCT drug_name) FROM ps WHERE {comb}'):,} distinct strings, {q1(f'SELECT count(DISTINCT primaryid) FROM ps WHERE {comb}'):,} cases.** "
      f"{q1(f'''SELECT count(*) FROM ps WHERE {comb} AND ai_source = 'drugname_fallback' '''):,} of those rows come from the drugname fallback. Not split.\n")
    bs = "(drug_name LIKE '%\\%')"
    br = q1(f"SELECT count(*) FROM ps WHERE {bs}")
    w(f"- **Rule mismatch:** the instruction's test (`,` or `;`) misses the separator the file actually uses for multi-ingredient products, a backslash (e.g. `ALBUTEROL SULFATE\\IPRATROPIUM BROMIDE`). PS rows containing a backslash: **{br:,}** ({100 * br / n_ps:.2f}%), {q1(f'SELECT count(DISTINCT drug_name) FROM ps WHERE {bs}'):,} distinct strings. Rows containing a backslash, comma or semicolon: **{q1(f'SELECT count(*) FROM ps WHERE {bs} OR {comb}'):,}** ({100 * q1(f'SELECT count(*) FROM ps WHERE {bs} OR {comb}') / n_ps:.2f}%). Conversely, a comma alone is often part of a single-ingredient name (`ESTROGENS, CONJUGATED`, `ANTIHEMOPHILIC FACTOR, HUMAN RECOMBINANT`), so the comma/semicolon list below is not a list of real combinations.")
    ch_ai = q1("SELECT count(*) FROM ps WHERE ai_source = 'prod_ai' AND prod_ai <> drug_name")
    ch_dn = q1("SELECT count(*) FROM ps WHERE ai_source = 'drugname_fallback' AND drugname <> drug_name")
    w(f"- Rows where cleaning changes `prod_ai` (case / whitespace): {ch_ai:,}; rows where cleaning changes the `drugname` fallback value: {ch_dn:,} of {fb:,}.\n")
    w("Top 20 combination-looking values (by PS rows):\n")
    top = con.execute(f"SELECT drug_name, count(*), count(DISTINCT primaryid), min(ai_source) FROM ps WHERE {comb} GROUP BY 1 ORDER BY 2 DESC LIMIT 20").fetchall()
    w(md_table(["#", "cleaned drug_name", "PS rows", "cases", "ai_source"], [(i + 1, *r) for i, r in enumerate(top)]))
    w("")

    def drug_rows(title, where, n):
        rows = con.execute(f"SELECT primaryid, caseid, drug_seq, _q, drug_name, ai_source FROM ps WHERE {where} ORDER BY random() LIMIT {n}").fetchall()
        raw = raw_lines([((r[0], r[2]), "drug", r[3], starts(r[0], r[1], r[2])) for r in rows])
        w(f"**{title}**\n")
        w(md_table(["quarter", "primaryid", "raw DRUG line (file text)", "-> drug_name", "ai_source"],
                   [(r[3], r[0], "`" + raw.get((r[0], r[2]), "NOT FOUND")[:230] + "`", r[4], r[5]) for r in rows]))
        w("")
    drug_rows("6a. 8 random PS rows where `prod_ai` is present", "ai_source = 'prod_ai'", 8)
    drug_rows("6b. 8 random PS rows where the rule changes `prod_ai` (case / whitespace); empty if cleaning is a no-op", "ai_source = 'prod_ai' AND prod_ai <> drug_name", 8)
    drug_rows("6b2. 8 random PS rows with a backslash-separated multi-ingredient `prod_ai`", bs, 8)
    drug_rows("6c. 8 random PS rows using the fallback (`prod_ai` blank)", "ai_source = 'drugname_fallback'", 8)
    drug_rows("6d. 6 random combination-looking PS rows", comb, 6)

    # ---------------- 7. event term
    w("## 7. Event term\n")
    con.execute("""CREATE TEMP TABLE rx AS SELECT r.primaryid, r.caseid, r._q, r.pt AS pt_original, upper(trim(r.pt)) AS pt_match
        FROM reac r JOIN kept_eff k ON k.primaryid = r.primaryid AND k._q = r._q""")
    nr = q1("SELECT count(*) FROM rx")
    dup = q1("SELECT count(*) - count(DISTINCT (primaryid, pt_match)) FROM rx")
    dup_raw = q1("SELECT count(*) - count(DISTINCT (primaryid, pt_original)) FROM rx")
    w(f"- REAC rows in retained cases: {nr:,}. `pt` is stored in sentence case, so uppercasing changes {q1('SELECT count(*) FROM rx WHERE pt_original <> pt_match'):,} rows; trimming changes {q1('SELECT count(*) FROM rx WHERE pt_original <> trim(pt_original)'):,} rows (no leading/trailing whitespace in `pt`).")
    coll = con.execute("SELECT pt_match, string_agg(DISTINCT pt_original, ' | '), count(*) FROM rx GROUP BY 1 HAVING count(DISTINCT pt_original) > 1").fetchall()
    w(f"- Terms that merge only because of uppercasing (same text, different case): {len(coll)}" + ("; " + "; ".join(f"`{a}` <- {b} ({c:,} rows)" for a, b, c in coll) if coll else "") + ".")
    w(f"- Distinct `pt` as stored: {q1('SELECT count(DISTINCT pt_original) FROM rx'):,}; distinct after trim+upper: {q1('SELECT count(DISTINCT pt_match) FROM rx'):,}.")
    w(f"- Repeated (`primaryid`, `pt`) pairs dropped: {dup:,} on the matching key (vs {dup_raw:,} on the original `pt`; the difference is repeats that differ only by case/whitespace).\n")

    def reac_rows(title, where, n, multi=False, having=""):
        grp = "GROUP BY ALL " + having if multi else ""
        rows = con.execute(f"SELECT primaryid, caseid, _q, pt_original, pt_match FROM rx WHERE {where} {grp} ORDER BY random() LIMIT {n}").fetchall()
        raw = raw_lines([((r[0], r[3]), "reac", r[2], (lambda a, b, c: lambda s: s.startswith(f"{a}${b}$") and s.split("$")[2] == c)(r[0], r[1], r[3])) for r in rows], multi=multi)
        w(f"**{title}**\n")
        fmt = (lambda v: " <br> ".join("`" + x + "`" for x in v)) if multi else (lambda v: "`" + v + "`")
        w(md_table(["quarter", "primaryid", "raw REAC line(s)", "pt_original", "pt_match"],
                   [(r[2], r[0], fmt(raw.get((r[0], r[3]), ["NOT FOUND"] if multi else "NOT FOUND")), r[3], r[4]) for r in rows]))
        w("")
    reac_rows("7a. 8 random REAC rows", "true", 8)
    reac_rows("7b. rows for the terms that merge under uppercasing (if any)", "pt_match IN (SELECT pt_match FROM rx GROUP BY 1 HAVING count(DISTINCT pt_original) > 1)", 6)
    w("**7c. 4 random repeated (`primaryid`, `pt`) pairs - both raw lines shown, one is kept**\n")
    rows = con.execute("SELECT primaryid, caseid, _q, pt_original, pt_match FROM rx GROUP BY ALL HAVING count(*) > 1 ORDER BY random() LIMIT 4").fetchall()
    raw = raw_lines([((r[0], r[3]), "reac", r[2], (lambda a, b, c: lambda s: s.startswith(f"{a}${b}$") and s.split("$")[2] == c)(r[0], r[1], r[3])) for r in rows], multi=True)
    w(md_table(["quarter", "primaryid", "raw REAC lines"], [(r[2], r[0], " <br> ".join("`" + x + "`" for x in raw[(r[0], r[3])])) for r in rows]))
    w("")

    # ---------------- 8. outcomes
    w("## 8. Outcome / severity\n")
    con.execute("CREATE TEMP TABLE ox AS SELECT DISTINCT primaryid, _q, outc_cod FROM outc")
    con.execute("CREATE TEMP TABLE oxk AS SELECT o.* FROM ox o JOIN kept_eff k ON k.primaryid = o.primaryid AND k._q = o._q")
    n_out_all, n_ox = q1("SELECT count(*) FROM outc"), q1("SELECT count(*) FROM ox")
    w(f"- OUTC rows {n_out_all:,} -> {n_ox:,} after dedup on (`primaryid`, `outc_cod`) ({n_out_all - n_ox:,} dropped).")
    w(f"- OUTC `primaryid`s that are not the kept version of their case (superseded versions, or the same `primaryid` re-published in another quarter; ignored by the join on (`primaryid`, quarter)): {q1('SELECT count(*) FROM (SELECT DISTINCT primaryid, _q FROM ox) o ANTI JOIN kept USING (primaryid, _q)'):,} (primaryid, quarter) groups.")
    w(f"- Retained cases with at least one OUTC row: {q1('SELECT count(DISTINCT primaryid) FROM oxk'):,} of {ne:,}; **no OUTC row -> all flags 0: {ne - q1('SELECT count(DISTINCT primaryid) FROM oxk'):,}** (documented as 'no serious outcome recorded').")
    older = q1("""SELECT count(DISTINCT k.caseid) FROM kept_eff k
                  WHERE NOT EXISTS (SELECT 1 FROM oxk x WHERE x.primaryid = k.primaryid)
                    AND EXISTS (SELECT 1 FROM demo d JOIN ox o ON o.primaryid = d.primaryid AND o._q = d._q WHERE d.caseid = k.caseid)""")
    w(f"- Retained cases whose kept version has no OUTC row although an *older* version of the same case had one: {older:,}; the 'latest version only' rule sets these to 0.")
    w("- Cases with the flag = 1 among retained cases: " + ", ".join(
        f"{c} {q1(f'''SELECT count(*) FROM oxk WHERE outc_cod = '{c}' '''):,}" for c in CODES)
      + f"; OUTC rows with a code outside the seven: {q1('''SELECT count(*) FROM ox WHERE outc_cod NOT IN ('DE','HO','LT','DS','CA','RI','OT') OR outc_cod IS NULL'''):,}.")
    w("- All seven codes are serious categories per ASC_NTS, so `any_serious` = 1 exactly when a case has any OUTC row; it carries no information beyond 'has an OUTC row'.\n")
    pivot = ", ".join(f"max(CASE WHEN outc_cod = '{c}' THEN 1 ELSE 0 END)" for c in CODES)
    multi_c = con.execute("SELECT primaryid, _q FROM oxk GROUP BY primaryid, _q HAVING count(*) >= 2 ORDER BY hash(primaryid) LIMIT 6").fetchall()
    none_c = con.execute("SELECT primaryid, _q FROM kept_eff WHERE primaryid NOT IN (SELECT primaryid FROM oxk) ORDER BY hash(primaryid) LIMIT 3").fetchall()
    raw = raw_lines([(pid, "outc", q, starts(pid)) for pid, q in multi_c + none_c], multi=True)
    rows = []
    for pid, q in multi_c + none_c:
        fl = dict(zip(CODES, [x or 0 for x in con.execute(f"SELECT {pivot} FROM oxk WHERE primaryid = '{pid}'").fetchone()]))
        rows.append((q, pid, " <br> ".join("`" + x + "`" for x in raw.get(pid, [])) or "(no OUTC line)", " ".join(f"{c}={v}" for c, v in fl.items()), int(any(fl.values()))))
    w("**8a. 6 random cases with 2+ outcome rows and 3 with none**\n")
    w(md_table(["quarter", "primaryid", "raw OUTC line(s)", "pivoted flags", "any_serious"], rows))
    w("")
    out = ROOT / "references" / "spot_checks_6_7_8.md"
    out.write_text("\n".join(o))
    print(f"wrote {out}")


if __name__ == "__main__":
    {"sanity1": sanity1, "sanity2": sanity2, "rules": rules}.get(sys.argv[1] if len(sys.argv) > 1 else "", lambda: sys.exit("usage: spot_checks.py sanity1|rules"))()
