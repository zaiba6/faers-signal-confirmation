#!/usr/bin/env python3
"""Build the case table and the PS drug-event link table from the raw FAERS tables (all decisions in DECISIONS.md).

Inputs : data/interim/faers_raw.duckdb (all-VARCHAR copy of the raw tables; built by scripts/profile_raw_data.py,
         re-loaded here for any quarter that is missing), plus the deleted-case lists under data/raw/*/[Dd]eleted/.
Outputs: data/processed/case_table.parquet            one row per case (latest version)
         data/processed/ps_drug_event_cases.parquet   one row per (caseid, drug_ai, pt_clean)
         references/profile/case_table_build_report.json  row counts after every filter + checks
Nothing under data/raw/ is written, the DuckDB file is only appended to with temp tables (never deleted), and the full
DRUG / REAC / OUTC tables in the DuckDB stay untouched.

Run: python scripts/build_case_table.py [--no-verify]
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

import duckdb

sys.path.insert(0, str(Path(__file__).resolve().parent))
import profile_raw_data as P  # noqa: E402
import spot_checks as S  # noqa: E402

ROOT = P.ROOT
DB = ROOT / "data" / "interim" / "faers_raw.duckdb"
OUT_DIR = ROOT / "data" / "processed"
REPORT = ROOT / "references" / "profile" / "case_table_build_report.json"
FIRST_QUARTER = "2014Q3"          # first file quarter that carries prod_ai
COUNT_START = "2014Q3"            # counting universe: cases with init_quarter >= this (decision 2); detection then starts 2016Q1 after 18 months burn-in
AGE_FACTOR = " ".join(f"WHEN '{k}' THEN {v!r}" for k, v in S.UNIT_FACTOR.items())
OUTCOMES = ["DE", "HO", "LT", "DS", "CA", "RI", "OT"]
REPORTER_GROUPS = {"hcp": ["MD", "HP", "PH", "RN"], "consumer": ["CN"], "lawyer": ["LW"], "other": ["OT", "SALES"]}


def clean(x):
    """trim, uppercase, collapse internal whitespace"""
    return f"upper(trim(regexp_replace({x}, '\\s+', ' ', 'g')))"


def ensure_loaded(con):
    inv = P.inventory()
    heads, _ = P.header_scan(inv)
    P.load_all(con, inv, heads, inv["quarters"])  # skips quarters already loaded


def load_deleted(con):
    """deleted/ lists: one numeric id per line, no header (blank lines possible). Returns summary."""
    rows, bad = [], 0
    files = []
    for d in sorted(P.RAW.glob("faers_ascii_*/*")):
        if d.is_dir() and d.name.lower() == "deleted":
            m = P.DIR_RE.match(d.parent.name)
            q = f"{m.group(1)}Q{m.group(2)}"
            for f in sorted(d.glob("*.txt")):
                files.append((q, f.name))
                for line in f.read_bytes().decode("latin-1").splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    if re.fullmatch(r"\d+", s):
                        rows.append((q, f.name, s))
                    else:
                        bad += 1
    con.execute("CREATE OR REPLACE TEMP TABLE deleted_raw(quarter VARCHAR, file VARCHAR, id VARCHAR)")
    con.executemany("INSERT INTO deleted_raw VALUES (?, ?, ?)", rows)
    con.execute("CREATE OR REPLACE TEMP TABLE deleted_ids AS SELECT DISTINCT id AS caseid FROM deleted_raw")
    return {"files": len(files), "first": files[0], "last": files[-1], "lines_with_id": len(rows), "non_numeric_lines": bad,
            "distinct_ids": con.execute("SELECT count(*) FROM deleted_ids").fetchone()[0]}


def build(con, verify=True):
    q1 = lambda sql: con.execute(sql).fetchone()[0]
    rep = {"disk_free_before_gb": round(shutil.disk_usage(ROOT).free / 1e9, 2)}
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ case set
    rep["demo_rows"] = q1("SELECT count(*) FROM demo")
    rep["demo_distinct_caseid"] = q1("SELECT count(DISTINCT caseid) FROM demo")
    con.execute("""CREATE OR REPLACE TEMP TABLE kept_all AS SELECT primaryid, caseid, caseversion, _q, init_fda_dt FROM (
        SELECT primaryid, caseid, caseversion, _q, init_fda_dt, row_number() OVER (PARTITION BY caseid ORDER BY TRY_CAST(caseversion AS INT) DESC, _q DESC) rn FROM demo) WHERE rn = 1""")
    rep["after_version_filter"] = q1("SELECT count(*) FROM kept_all")
    con.execute(f"CREATE OR REPLACE TEMP TABLE kept_q AS SELECT * FROM kept_all WHERE _q >= '{FIRST_QUARTER}'")
    rep["after_2014Q3_cutoff"] = q1("SELECT count(*) FROM kept_q")
    rep["dropped_by_cutoff"] = rep["after_version_filter"] - rep["after_2014Q3_cutoff"]
    rep["retained_first_received_before_2014_07_01"] = q1("SELECT count(*) FROM kept_q WHERE init_fda_dt < '20140701'")
    rep["retained_first_received_before_faers_2012_08_28"] = q1("SELECT count(*) FROM kept_q WHERE init_fda_dt < '20120828'")

    rep["deleted_lists"] = load_deleted(con)
    dm = {
        "matching_any_demo_caseid": q1("SELECT count(*) FROM deleted_ids WHERE caseid IN (SELECT caseid FROM demo)"),
        "matching_primaryid_instead": q1("SELECT count(*) FROM deleted_ids WHERE caseid IN (SELECT primaryid FROM demo)"),
        "matching_kept_version_cases": q1("SELECT count(*) FROM deleted_ids WHERE caseid IN (SELECT caseid FROM kept_all)"),
        "matching_cases_after_2014Q3_cutoff": q1("SELECT count(*) FROM deleted_ids WHERE caseid IN (SELECT caseid FROM kept_q)"),
    }
    rep["deleted_matches"] = dm
    con.execute("CREATE OR REPLACE TEMP TABLE case_set AS SELECT * FROM kept_q WHERE caseid NOT IN (SELECT caseid FROM deleted_ids)")
    rep["after_deleted_removal"] = q1("SELECT count(*) FROM case_set")
    rep["removed_as_deleted"] = rep["after_2014Q3_cutoff"] - rep["after_deleted_removal"]

    # ------------------------------------------------------------------ dates
    d8 = "regexp_full_match(init_fda_dt, '\\d{8}')"
    ok = f"({d8} AND try_strptime(init_fda_dt, '%Y%m%d') IS NOT NULL)"
    rep["invalid_init_fda_dt_in_case_set"] = q1(f"SELECT count(*) FROM case_set WHERE init_fda_dt IS NULL OR NOT {ok}")
    rep["invalid_init_fda_dt_in_all_demo"] = q1(f"SELECT count(*) FROM demo WHERE init_fda_dt IS NULL OR NOT ({d8} AND try_strptime(init_fda_dt, '%Y%m%d') IS NOT NULL)")

    # ------------------------------------------------------------------ outcomes, PS drugs (also needed for the case table flags)
    con.execute("""CREATE OR REPLACE TEMP TABLE oc AS SELECT o.primaryid, o._q, """ + ", ".join(
        f"max(CASE WHEN o.outc_cod = '{c}' THEN 1 ELSE 0 END) AS {c}" for c in OUTCOMES) + """
        FROM (SELECT DISTINCT primaryid, _q, outc_cod FROM outc) o JOIN case_set c ON c.primaryid = o.primaryid AND c._q = o._q GROUP BY 1, 2""")
    rep["outc_rows_raw"] = q1("SELECT count(*) FROM outc")
    rep["outc_rows_after_dedup"] = q1("SELECT count(*) FROM (SELECT DISTINCT primaryid, _q, outc_cod FROM outc)")
    rep["outc_codes_outside_seven"] = q1(f"SELECT count(*) FROM outc WHERE outc_cod NOT IN ({', '.join(repr(c) for c in OUTCOMES)}) OR outc_cod IS NULL")

    con.execute(f"""CREATE OR REPLACE TEMP TABLE ps_rows AS
        SELECT c.caseid, c.primaryid, c._q AS source_quarter, d.drug_seq,
               nullif({clean('d.prod_ai')}, '') AS ai_clean, nullif({clean('d.drugname')}, '') AS dn_clean
        FROM drug d JOIN case_set c ON c.primaryid = d.primaryid AND c._q = d._q WHERE d.role_cod = 'PS'""")
    rep["ps_rows"] = q1("SELECT count(*) FROM ps_rows")
    rep["ps_rows_exact_duplicates_dropped"] = rep["ps_rows"] - q1("SELECT count(*) FROM (SELECT DISTINCT caseid, primaryid, drug_seq, ai_clean, dn_clean FROM ps_rows)")
    rep["ps_rows_prod_ai_blank_fallback_to_drugname"] = q1("SELECT count(*) FROM ps_rows WHERE ai_clean IS NULL AND dn_clean IS NOT NULL")
    rep["ps_rows_no_drug_name_at_all"] = q1("SELECT count(*) FROM ps_rows WHERE ai_clean IS NULL AND dn_clean IS NULL")
    con.execute("""CREATE OR REPLACE TEMP TABLE ps AS
        SELECT caseid, any_value(primaryid) AS primaryid, any_value(source_quarter) AS source_quarter, coalesce(ai_clean, dn_clean) AS drug_ai,
               max(CASE WHEN ai_clean IS NOT NULL THEN 'prod_ai' ELSE 'drugname_fallback' END) AS ai_source
        FROM ps_rows WHERE coalesce(ai_clean, dn_clean) IS NOT NULL GROUP BY caseid, coalesce(ai_clean, dn_clean)""")

    # ------------------------------------------------------------------ case table
    occ = "upper(nullif(trim(d.occp_cod), ''))"
    grp = "CASE " + " ".join(f"WHEN {occ} IN ({', '.join(repr(c) for c in codes)}) THEN '{g}'" for g, codes in REPORTER_GROUPS.items()) + \
          f" WHEN {occ} IS NULL THEN 'missing' ELSE 'other' END"
    age_num = "CASE WHEN isfinite(TRY_CAST(trim(d.age) AS DOUBLE)) THEN TRY_CAST(trim(d.age) AS DOUBLE) END"
    yrs = f"({age_num} * CASE upper(trim(d.age_cod)) {AGE_FACTOR} END)"
    sex = "upper(coalesce(nullif(trim(d.sex), ''), nullif(trim(d.gndr_cod), '')))"
    cty = "upper(nullif(trim(d.reporter_country), ''))"
    cty_ok = f"CASE WHEN {cty} IN ('COUNTRY NOT SPECIFIED', 'EU') THEN NULL ELSE {cty} END"
    pivot = ", ".join(f"coalesce(oc.{c}, 0)::TINYINT AS {c}" for c in OUTCOMES)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE case_tbl AS
        SELECT c.caseid, c.primaryid, c._q AS source_quarter, TRY_CAST(c.caseversion AS INT) AS caseversion,
               c.init_fda_dt,
               CASE WHEN {ok.replace('init_fda_dt', 'c.init_fda_dt')} THEN substr(c.init_fda_dt, 1, 4) || 'Q' || CAST((CAST(substr(c.init_fda_dt, 5, 2) AS INT) + 2) // 3 AS VARCHAR) END AS init_quarter,
               CASE WHEN ({yrs}) BETWEEN 0 AND 120 THEN ({yrs}) END AS age_yrs,
               CASE WHEN {sex} IN ('F', 'M') THEN {sex} ELSE 'UNK' END AS sex,
               {cty_ok} AS reporter_country,
               CASE WHEN {cty_ok} IS NULL THEN NULL WHEN {cty_ok} = 'US' THEN 1 ELSE 0 END::TINYINT AS us_flag,
               {grp} AS reporter_group,
               {", ".join(f"({grp} = '{g}')::TINYINT AS occp_{g}" for g in list(REPORTER_GROUPS) + ['missing'])},
               {pivot},
               (oc.primaryid IS NOT NULL)::TINYINT AS has_outcome,
               (CASE WHEN {ok.replace('init_fda_dt', 'c.init_fda_dt')} THEN substr(c.init_fda_dt, 1, 4) || 'Q' || CAST((CAST(substr(c.init_fda_dt, 5, 2) AS INT) + 2) // 3 AS VARCHAR) END >= '{COUNT_START}')::TINYINT AS in_counting_universe,
               (ps.caseid IS NOT NULL)::TINYINT AS has_ps_drug
        FROM case_set c JOIN demo d ON d.primaryid = c.primaryid AND d._q = c._q
        LEFT JOIN oc ON oc.primaryid = c.primaryid AND oc._q = c._q
        LEFT JOIN (SELECT DISTINCT caseid FROM ps) ps ON ps.caseid = c.caseid""")
    cpath = OUT_DIR / "case_table.parquet"
    con.execute(f"COPY (SELECT * FROM case_tbl ORDER BY caseid) TO '{cpath}' (FORMAT parquet, COMPRESSION zstd)")
    rep["case_table_rows"] = q1(f"SELECT count(*) FROM '{cpath}'")
    rep["case_table_has_ps_drug"] = q1("SELECT sum(has_ps_drug) FROM case_tbl")
    rep["case_table_no_ps_drug"] = rep["case_table_rows"] - rep["case_table_has_ps_drug"]
    rep["case_table_has_outcome"] = q1("SELECT sum(has_outcome) FROM case_tbl")

    # ------------------------------------------------------------------ PS drug-event link table
    con.execute("""CREATE OR REPLACE TEMP TABLE rc AS
        SELECT c.caseid, upper(trim(r.pt)) AS pt_clean, min(r.pt) AS pt
        FROM reac r JOIN case_set c ON c.primaryid = r.primaryid AND c._q = r._q WHERE trim(coalesce(r.pt, '')) <> '' GROUP BY 1, 2""")
    rep["reac_rows_in_case_set"] = q1("SELECT count(*) FROM reac r JOIN case_set c ON c.primaryid = r.primaryid AND c._q = r._q")
    rep["reac_pairs_after_dedup"] = q1("SELECT count(*) FROM rc")
    rep["reac_repeated_primaryid_pt_dropped"] = rep["reac_rows_in_case_set"] - rep["reac_pairs_after_dedup"]
    lpath = OUT_DIR / "ps_drug_event_cases.parquet"
    con.execute(f"""COPY (SELECT ps.caseid, ps.primaryid, ps.source_quarter, ct.init_quarter, ps.drug_ai, ps.ai_source,
                                 (ps.drug_ai LIKE '%\\%')::TINYINT AS is_combo, rc.pt_clean, rc.pt, ct.in_counting_universe
                          FROM ps JOIN rc USING (caseid) JOIN case_tbl ct ON ct.caseid = ps.caseid ORDER BY ps.caseid)
                    TO '{lpath}' (FORMAT parquet, COMPRESSION zstd)""")
    L = f"read_parquet('{lpath}')"
    rep["link_rows"] = q1(f"SELECT count(*) FROM {L}")
    rep["link_distinct_cases"] = q1(f"SELECT count(DISTINCT caseid) FROM {L}")
    rep["link_distinct_drugs"] = q1(f"SELECT count(DISTINCT drug_ai) FROM {L}")
    rep["link_distinct_pts"] = q1(f"SELECT count(DISTINCT pt_clean) FROM {L}")
    rep["link_distinct_drug_event_pairs"] = q1(f"SELECT count(*) FROM (SELECT DISTINCT drug_ai, pt_clean FROM {L})")
    rep["link_duplicate_keys"] = rep["link_rows"] - q1(f"SELECT count(*) FROM (SELECT DISTINCT caseid, drug_ai, pt_clean FROM {L})")
    U = f"(SELECT * FROM {L} WHERE in_counting_universe = 1)"
    rep["counting_universe"] = {
        "rule": f"init_quarter >= {COUNT_START}",
        "cases_before": rep["case_table_rows"],
        "cases_in_universe": q1("SELECT sum(in_counting_universe) FROM case_tbl"),
        "cases_dropped_older": q1("SELECT count(*) FROM case_tbl WHERE in_counting_universe = 0"),
        "link_rows_in_universe": q1(f"SELECT count(*) FROM {U}"),
        "link_rows_dropped": q1(f"SELECT count(*) FROM {L} WHERE in_counting_universe = 0"),
        "link_cases_in_universe": q1(f"SELECT count(DISTINCT caseid) FROM {U}"),
        "link_drugs_in_universe": q1(f"SELECT count(DISTINCT drug_ai) FROM {U}"),
        "link_pts_in_universe": q1(f"SELECT count(DISTINCT pt_clean) FROM {U}"),
        "link_pairs_in_universe": q1(f"SELECT count(*) FROM (SELECT DISTINCT drug_ai, pt_clean FROM {U})"),
        "init_quarter_range": list(con.execute(f"SELECT min(init_quarter), max(init_quarter) FROM {U}").fetchone()),
        "cases_per_init_quarter": {k: v for k, v in con.execute("SELECT init_quarter, count(*) FROM case_tbl WHERE in_counting_universe = 1 GROUP BY 1 ORDER BY 1").fetchall()},
        "dropped_cases_init_quarter_range": list(con.execute("SELECT min(init_quarter), max(init_quarter) FROM case_tbl WHERE in_counting_universe = 0").fetchone()),
        "universe_cases_with_kept_version_before_cutoff": q1(f"SELECT count(*) FROM case_tbl WHERE in_counting_universe = 1 AND source_quarter < '{FIRST_QUARTER}'"),
    }
    rep["link_null_init_quarter"] = q1(f"SELECT count(*) FROM {L} WHERE init_quarter IS NULL")
    # why cases drop out of the link table
    rep["cases_in_case_set"] = rep["after_deleted_removal"]
    rep["cases_with_no_PS_row"] = q1("SELECT count(*) FROM case_set WHERE caseid NOT IN (SELECT caseid FROM ps_rows)")
    rep["cases_PS_but_no_drug_name"] = q1("SELECT count(*) FROM (SELECT DISTINCT caseid FROM ps_rows) WHERE caseid NOT IN (SELECT caseid FROM ps)")
    rep["cases_PS_but_no_reaction"] = q1("SELECT count(*) FROM (SELECT DISTINCT caseid FROM ps) WHERE caseid NOT IN (SELECT caseid FROM rc)")
    rep["cases_dropped_total_from_link"] = rep["cases_in_case_set"] - rep["link_distinct_cases"]
    # combination / fallback stats
    rep["link_rows_is_combo"] = q1(f"SELECT sum(is_combo) FROM {L}")
    rep["link_rows_combo_share_pct"] = round(100 * rep["link_rows_is_combo"] / rep["link_rows"], 3)
    ps_n = q1("SELECT count(*) FROM ps")
    rep["ps_case_drug_pairs"] = ps_n
    rep["ps_case_drug_pairs_combo"] = q1("SELECT count(*) FROM ps WHERE drug_ai LIKE '%\\%'")
    rep["ps_case_drug_pairs_combo_share_pct"] = round(100 * rep["ps_case_drug_pairs_combo"] / ps_n, 3)
    rep["distinct_drug_ai"] = q1("SELECT count(DISTINCT drug_ai) FROM ps")
    rep["distinct_drug_ai_combo"] = q1("SELECT count(DISTINCT drug_ai) FROM ps WHERE drug_ai LIKE '%\\%'")
    rep["ps_pairs_comma_or_semicolon_only"] = q1("SELECT count(*) FROM ps WHERE drug_ai NOT LIKE '%\\%' AND (drug_ai LIKE '%,%' OR drug_ai LIKE '%;%')")
    rep["ps_pairs_comma_or_semicolon_any"] = q1("SELECT count(*) FROM ps WHERE drug_ai LIKE '%,%' OR drug_ai LIKE '%;%'")
    rep["ps_pairs_ai_source_fallback"] = q1("SELECT count(*) FROM ps WHERE ai_source = 'drugname_fallback'")
    rep["link_rows_ai_source_fallback"] = q1(f"SELECT count(*) FROM {L} WHERE ai_source = 'drugname_fallback'")
    rep["link_rows_by_ai_source"] = dict(con.execute(f"SELECT ai_source, count(*) FROM {L} GROUP BY 1").fetchall())
    rep["case_table_reporter_group"] = dict(con.execute("SELECT reporter_group, count(*) FROM case_tbl GROUP BY 1 ORDER BY 2 DESC").fetchall())
    rep["case_table_sex"] = dict(con.execute("SELECT sex, count(*) FROM case_tbl GROUP BY 1 ORDER BY 2 DESC").fetchall())
    rep["case_table_age_missing"] = q1("SELECT count(*) FROM case_tbl WHERE age_yrs IS NULL")
    rep["case_table_us_flag"] = {str(k): v for k, v in con.execute("SELECT us_flag, count(*) FROM case_tbl GROUP BY 1 ORDER BY 1").fetchall()}
    rep["case_table_null_init_quarter"] = q1("SELECT count(*) FROM case_tbl WHERE init_quarter IS NULL")
    rep["init_quarter_range"] = list(con.execute("SELECT min(init_quarter), max(init_quarter) FROM case_tbl").fetchone())
    rep["case_table_caseid_unique"] = rep["case_table_rows"] == q1("SELECT count(DISTINCT caseid) FROM case_tbl")
    cur = con.execute(f"SELECT * FROM '{cpath}' ORDER BY hash(caseid, 11) LIMIT 10")
    rep["samples"] = {"case_table": [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]}
    cur = con.execute(f"SELECT * FROM {L} ORDER BY hash(caseid, drug_ai, pt_clean, 11) LIMIT 10")
    rep["samples"]["ps_drug_event_cases"] = [dict(zip([d[0] for d in cur.description], r)) for r in cur.fetchall()]
    rep["parquet_bytes"] = {"case_table": cpath.stat().st_size, "ps_drug_event_cases": lpath.stat().st_size}

    if verify:
        rep["verification"] = verify_against_raw(con, cpath)
    rep["disk_free_after_gb"] = round(shutil.disk_usage(ROOT).free / 1e9, 2)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(rep, indent=1, default=str))
    return rep


def verify_against_raw(con, cpath, n=150):
    """Re-derive age / sex / country for sampled cases from the raw DEMO text line with independent code and compare."""
    hdr = json.loads((ROOT / "references" / "profile" / "profile_all.json").read_text())["header_columns"]["demo"]
    rows = con.execute(f"SELECT caseid, primaryid, source_quarter, age_yrs, sex, reporter_country, us_flag FROM '{cpath}' ORDER BY hash(caseid, 5) LIMIT {n}").fetchall()
    raw = S.raw_lines([((r[0],), "demo", r[2], S.starts(r[1])) for r in rows])
    cleaned = [{"age_years_clean": r[3], "sex_clean": r[4], "country_clean": r[5], "us_flag": r[6]} for r in rows]
    res = S.check_rows([(r[2], raw.get((r[0],), "")) for r in rows], hdr, cleaned)
    return {"cases_checked": len(rows), "ok": res.count("OK"), "mismatches": [x for x in res if x != "OK"][:10]}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--mem", default="10GB")
    a = ap.parse_args()
    con = P.connect(DB, a.mem)
    con.execute("SET threads=4")
    ensure_loaded(con)
    r = build(con, verify=not a.no_verify)
    print(json.dumps({k: v for k, v in r.items() if k != "samples"}, indent=1, default=str))
