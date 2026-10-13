#!/usr/bin/env python3
"""Build demo_clean (one row per DEMO primaryid) in the local DuckDB from the raw all-VARCHAR demo table.

Rules (project decisions 9, 10, 12 + sex coalescing):
  age_years   : age -> number (non-numeric -> NULL), converted to years with age_cod; NULL when age_cod is blank/unknown;
                values outside 0..120 years -> NULL.
  sex         : coalesce(sex, gndr_cod); F / M kept; NS, P, I, T, UNK, blank -> 'UNK'.
  country     : trim + upper reporter_country; 'COUNTRY NOT SPECIFIED', 'EU', blank -> NULL; us_flag = 1 US / 0 other / NULL missing.
  occp_cod    : trim + upper; groups -> one 0/1 column each (blank -> NULL in all columns; unlisted code SALES -> all 0).
Run: python scripts/clean_demo.py   (needs data/interim/faers_raw.duckdb from scripts/profile_raw_data.py)
"""
import json
import sys
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "interim" / "faers_raw.duckdb"
OUT = ROOT / "references" / "profile" / "demo_clean_checks.json"

AGE_TO_YEARS = {"DEC": "a * 10", "YR": "a", "MON": "a / 12", "WK": "a * 7 / 365.25", "DY": "a / 365.25",
                "HR": "a / 8766", "MIN": "a / 525960", "SEC": "a / 31557600"}
OCCP_GROUPS = {"occp_physician": ["MD"], "occp_pharmacist": ["PH"], "occp_other_hcp": ["HP", "RN", "OT"],
               "occp_consumer": ["CN"], "occp_lawyer": ["LW"]}


def build(con):
    unit = "CASE upper(trim(age_cod)) " + " ".join(f"WHEN '{k}' THEN {v}" for k, v in AGE_TO_YEARS.items()) + " END"
    occp_cols = ",\n  ".join(
        f"CASE WHEN occp IS NULL THEN NULL WHEN occp IN ({', '.join(repr(c) for c in codes)}) THEN 1 ELSE 0 END AS {name}"
        for name, codes in OCCP_GROUPS.items())
    con.execute("DROP TABLE IF EXISTS demo_clean")
    con.execute(f"""
    CREATE TABLE demo_clean AS
    WITH x AS (
      SELECT primaryid, caseid, caseversion, _q, init_fda_dt, fda_dt, age_cod,
        CASE WHEN isfinite(TRY_CAST(trim(age) AS DOUBLE)) THEN TRY_CAST(trim(age) AS DOUBLE) END AS a,
        upper(coalesce(nullif(trim(sex), ''), nullif(trim(gndr_cod), ''))) AS sex_raw,
        upper(nullif(trim(reporter_country), '')) AS country_raw,
        upper(nullif(trim(occp_cod), '')) AS occp
      FROM demo)
    SELECT primaryid, caseid, caseversion, _q, init_fda_dt, fda_dt,
      CASE WHEN ({unit}) BETWEEN 0 AND 120 THEN ({unit}) END AS age_years,
      CASE WHEN sex_raw IN ('F', 'M') THEN sex_raw ELSE 'UNK' END AS sex,
      CASE WHEN country_raw IN ('COUNTRY NOT SPECIFIED', 'EU') THEN NULL ELSE country_raw END AS reporter_country,
      CASE WHEN country_raw IS NULL OR country_raw IN ('COUNTRY NOT SPECIFIED', 'EU') THEN NULL
           WHEN country_raw = 'US' THEN 1 ELSE 0 END AS us_flag,
      occp AS occp_cod,
      {occp_cols}
    FROM x""")


def checks(con):
    q = lambda s: con.execute(s).fetchall()
    n = q("SELECT count(*) FROM demo_clean")[0][0]
    out = {"rows": n, "rows_raw_demo": q("SELECT count(*) FROM demo")[0][0]}
    out["age"] = {
        "raw_non_blank": q("SELECT count(*) FROM demo WHERE trim(age) <> ''")[0][0],
        "non_numeric_to_null": q("SELECT count(*) FROM demo WHERE trim(age) <> '' AND NOT isfinite(coalesce(TRY_CAST(trim(age) AS DOUBLE), 'NaN'::DOUBLE))")[0][0],
        "numeric_but_no_valid_unit": q("""SELECT count(*) FROM demo WHERE isfinite(coalesce(TRY_CAST(trim(age) AS DOUBLE),'NaN'::DOUBLE))
            AND upper(trim(coalesce(age_cod,''))) NOT IN ('DEC','YR','MON','WK','DY','HR','MIN','SEC')""")[0][0],
        "out_of_range_to_null": q(f"""SELECT count(*) FROM demo WHERE isfinite(coalesce(TRY_CAST(trim(age) AS DOUBLE),'NaN'::DOUBLE))
            AND upper(trim(age_cod)) IN ('DEC','YR','MON','WK','DY','HR','MIN','SEC')
            AND NOT ((CASE upper(trim(age_cod)) {' '.join(f"WHEN '{k}' THEN {v.replace('a', 'TRY_CAST(trim(age) AS DOUBLE)')}" for k, v in AGE_TO_YEARS.items())} END) BETWEEN 0 AND 120)""")[0][0],
        "valid_age_years": q("SELECT count(age_years) FROM demo_clean")[0][0],
    }
    r = q("SELECT min(age_years), median(age_years), max(age_years), count(*) FILTER (WHERE age_years < 1) FROM demo_clean")[0]
    out["age"].update({"min": r[0], "median": r[1], "max": r[2], "under_1_year": r[3]})
    out["sex"] = dict(q("SELECT sex, count(*) FROM demo_clean GROUP BY 1 ORDER BY 2 DESC"))
    out["country"] = {"null": q("SELECT count(*) FROM demo_clean WHERE reporter_country IS NULL")[0][0],
                      "us": q("SELECT count(*) FROM demo_clean WHERE us_flag = 1")[0][0],
                      "non_us": q("SELECT count(*) FROM demo_clean WHERE us_flag = 0")[0][0],
                      "distinct_countries": q("SELECT count(DISTINCT reporter_country) FROM demo_clean")[0][0],
                      "us_flag_null_matches_country_null": q("SELECT count(*) FROM demo_clean WHERE (us_flag IS NULL) <> (reporter_country IS NULL)")[0][0] == 0}
    out["occp_cod"] = dict(q("SELECT coalesce(occp_cod, '<blank>'), count(*) FROM demo_clean GROUP BY 1 ORDER BY 2 DESC"))
    out["occp_groups"] = {k: dict(q(f"SELECT coalesce({k}::VARCHAR, 'NULL'), count(*) FROM demo_clean GROUP BY 1")) for k in OCCP_GROUPS}
    out["occp_row_sums"] = dict(q("SELECT coalesce((occp_physician + occp_pharmacist + occp_other_hcp + occp_consumer + occp_lawyer)::VARCHAR, 'NULL') s, count(*) FROM demo_clean GROUP BY 1"))
    out["occp_groups_def"] = OCCP_GROUPS
    return out


if __name__ == "__main__":
    con = duckdb.connect(str(DB))
    con.execute("SET memory_limit='8GB'")
    build(con)
    res = checks(con)
    OUT.write_text(json.dumps(res, indent=1, default=str))
    print(json.dumps(res, indent=1, default=str))
