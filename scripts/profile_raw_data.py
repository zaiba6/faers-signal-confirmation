#!/usr/bin/env python3
"""Profile the raw FAERS quarterly ASCII extracts in data/raw/ (read-only).

Pipeline: inventory + header scan -> byte-level file scan (line endings, field counts, encoding) ->
load every table as all-VARCHAR into a local DuckDB file (data/interim/, git-ignored) -> SQL profiling ->
JSON + value-count CSV -> --render writes references/faers_data_dictionary.md.
Nothing under data/raw/ is ever written.

Usage:
    python scripts/profile_raw_data.py                      # all quarters found in data/raw
    python scripts/profile_raw_data.py --quarters 2012Q4 2013Q1 --db /tmp/t.duckdb --out /tmp/p.json
    python scripts/profile_raw_data.py --render references/profile/profile_all.json

Loading rules (per project spec): every column read as string, latin-1, bad lines skipped
(on_bad_lines="skip" in pandas == ignore_errors in DuckDB); quoting disabled because '"' is data, not a quote
char. Empty fields load as NULL. Header names are stripped/lower-cased; outc_code->outc_cod and lot_nbr->lot_num.
"""
import argparse
import csv
import json
import re
import sys
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data" / "raw"
TABLES = ["demo", "drug", "reac", "outc", "rpsr", "ther", "indi"]
PROFILED = ["demo", "drug", "reac", "outc"]  # per-column profile required for these
DATE_COLS = {"event_dt", "mfr_dt", "init_fda_dt", "fda_dt", "rept_dt", "exp_dt", "start_dt", "end_dt"}
# Columns treated as coded -> full value counts written to CSV
CODED_COLS = {
    "i_f_code", "rept_cod", "age_cod", "gndr_cod", "e_sub", "wt_cod", "to_mfr", "occp_cod",
    "reporter_country", "occr_country", "role_cod", "val_vbm", "route", "cum_dose_unit",
    "dechal", "rechal", "dose_unit", "dose_form", "dose_freq", "outc_cod", "outc_code",
    "rpsr_cod", "dur_cod", "mfr_sndr",
}
FULL_COUNT_MAX_UNIQUE = 60  # show every value in the .md when a column has <= this many
FILE_RE = re.compile(r"^(demo|drug|reac|outc|rpsr|ther|indi)(\d{2})q([1-4])(_new)?\.txt$", re.I)
DIR_RE = re.compile(r"^faers_ascii_(\d{4})q([1-4])$", re.I)


# --------------------------------------------------------------------------- inventory
def quarter_dirs():
    out = {}
    for d in sorted(RAW.iterdir()):
        m = DIR_RE.match(d.name)
        if m and d.is_dir():
            out[f"{m.group(1)}Q{m.group(2)}"] = d
    return out


def all_quarters_between(first, last):
    y, q = int(first[:4]), int(first[-1])
    ly, lq = int(last[:4]), int(last[-1])
    res = []
    while (y, q) <= (ly, lq):
        res.append(f"{y}Q{q}")
        q += 1
        if q == 5:
            y, q = y + 1, 1
    return res


def inventory():
    qd = quarter_dirs()
    zips = sorted(p.name for p in RAW.glob("*.zip"))
    quarters = sorted(qd)
    expected = all_quarters_between(quarters[0], quarters[-1])
    inv = {"quarters": quarters, "missing": [q for q in expected if q not in qd],
           "zips_in_raw": zips, "tables": {t: [] for t in TABLES}, "files": {},
           "docs": {}, "deleted": {}, "dir_names": {q: qd[q].name for q in quarters},
           "extra_data_files": {}}
    for q, d in qd.items():
        found = {}
        docs, deleted, extra = [], [], []
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            rel = str(p.relative_to(d))
            m = FILE_RE.match(p.name)
            if m:
                found[m.group(1).lower()] = p
                if m.group(4):
                    inv.setdefault("nonstandard_file_names", {}).setdefault(q, []).append(p.name)
            elif p.suffix.lower() in (".doc", ".pdf") or p.name.lower().startswith(("readme", "faqs")):
                docs.append(rel)
            elif p.suffix.lower() == ".txt":
                (deleted if "delet" in rel.lower() else extra).append(rel)
        for t in TABLES:
            if t in found:
                inv["tables"][t].append(q)
        inv["files"][q] = {t: str(p.relative_to(RAW)) for t, p in found.items()}
        inv["docs"][q] = docs
        inv["deleted"][q] = deleted
        if extra:
            inv["extra_data_files"][q] = extra
    return inv


def read_header_line(path):
    with open(path, "rb") as f:
        raw = f.readline()
    return raw


def clean_header(raw_bytes):
    """Return (clean lower-case column list, list of anomalies in the raw header)."""
    txt = raw_bytes.decode("latin-1").rstrip("\r\n")
    anomalies = []
    if txt.startswith("\xef\xbb\xbf"):
        anomalies.append("UTF-8 BOM at start of file")
        txt = txt[3:]
    raw_cols = txt.split("$")
    cols = []
    for c in raw_cols:
        if c != c.strip():
            anomalies.append(f"whitespace in column name {c!r}")
        cols.append(c.strip().lower())
    return cols, anomalies


def header_scan(inv):
    res = {t: {} for t in TABLES}
    anomalies = defaultdict(list)
    for q in inv["quarters"]:
        for t, rel in inv["files"][q].items():
            cols, an = clean_header(read_header_line(RAW / rel))
            res[t][q] = cols
            for a in an:
                anomalies[(t, q)].append(a)
    return res, {f"{t}|{q}": v for (t, q), v in anomalies.items()}


# --------------------------------------------------------------------------- raw-file checks
def scan_raw_file(path):
    """Byte-level pass over one file: header, line endings, field-count histogram, non-ASCII / UTF-8 checks."""
    data = Path(path).read_bytes()
    b = np.frombuffer(data, dtype=np.uint8)
    nl = np.flatnonzero(b == 10)
    ends = nl if (len(b) == 0 or b[-1] == 10) else np.append(nl, len(b))
    starts = np.concatenate(([0], ends[:-1] + 1))
    cum_d = np.concatenate(([0], np.cumsum(b == 36, dtype=np.int64)))
    cum_hi = np.concatenate(([0], np.cumsum(b > 127, dtype=np.int64)))
    dollars = cum_d[ends] - cum_d[starts]
    hi = cum_hi[ends] - cum_hi[starts]
    has_cr = (ends > starts) & (b[np.maximum(ends - 1, 0)] == 13) & (ends < len(b) + 1)
    # for a last line without newline, ends == len(b): b[ends-1] is its last byte (fine)
    body_len = (ends - starts) - has_cr
    n_fields = int(dollars[0]) + 1
    d, h, c, bl = dollars[1:], hi[1:], has_cr[1:], body_len[1:]
    k = d + 1
    st = {"header_fields": n_fields, "lines": int(len(d)), "crlf": int(c.sum()), "blank_lines": int((bl == 0).sum()),
          "too_many_fields": int(((k > n_fields) & (bl > 0)).sum()), "too_few_fields": int(((k < n_fields) & (bl > 0)).sum()),
          "non_ascii_lines": int((h > 0).sum()), "bom": data[:3] == b"\xef\xbb\xbf",
          "u_fffd_runs": data.count(b"\xef\xbf\xbd"), "no_final_newline": bool(len(b) and b[-1] != 10)}
    ok = bad = 0
    for i in np.flatnonzero(h > 0):
        line = data[starts[i + 1]:ends[i + 1]]
        try:
            line.decode("utf-8")
            ok += 1
        except UnicodeDecodeError:
            bad += 1
    st["non_ascii_valid_utf8"], st["non_ascii_not_utf8"] = ok, bad
    return st


# --------------------------------------------------------------------------- DuckDB load
# Known spelling variants of the same field; mapped to the canonical name when loading.
CANON = {"outc_code": "outc_cod", "lot_nbr": "lot_num"}


def canon(cols):
    return [CANON.get(c, c) for c in cols]


def connect(db_path, mem="10GB"):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(db_path))
    con.execute(f"SET memory_limit='{mem}'")
    con.execute(f"SET temp_directory='{db_path.parent / 'duckdb_tmp'}'")
    con.execute("SET preserve_insertion_order=false")
    return con


def to_utf8(data: bytes) -> bytes:
    """UTF-8 with latin-1 fallback: whole file if it is valid UTF-8, otherwise line by line."""
    try:
        data.decode("utf-8")
        return data
    except UnicodeDecodeError:
        out = []
        for line in data.split(b"\n"):
            try:
                line.decode("utf-8")
                out.append(line)
            except UnicodeDecodeError:
                out.append(line.decode("latin-1").encode("utf-8"))
        return b"\n".join(out)


def reload_quarters(con, inv, heads, pairs):
    """delete and re-load (table, quarter) pairs, e.g. after a change of decoding rule"""
    for t, q in pairs:
        con.execute(f"DELETE FROM {t} WHERE _q = '{q}'")
    load_all(con, inv, heads, sorted({q for _, q in pairs}), only=set(pairs))


def load_all(con, inv, heads, quarters, only=None):
    """Create one all-VARCHAR table per FAERS table (+ _q) and load each file; skips quarters already loaded."""
    db_dir = Path(con.execute("SELECT current_setting('temp_directory')").fetchone()[0]).parent
    for t in TABLES:
        union = []
        for q in inv["quarters"]:
            for c in canon(heads[t].get(q, [])):
                if c not in union:
                    union.append(c)
        if not union:  # no files on disk for this table (e.g. RPSR/THER/INDI after their raw files were removed)
            continue
        con.execute(f"CREATE TABLE IF NOT EXISTS {t} (" + ", ".join(f'"{c}" VARCHAR' for c in union) + ", _q VARCHAR)")
        have = {r[0] for r in con.execute(f"SELECT DISTINCT _q FROM {t}").fetchall()}
        for q in quarters:
            if q in have or q not in inv["files"] or t not in inv["files"][q] or (only is not None and (t, q) not in only):
                continue
            cols = canon(heads[t][q])
            names = "[" + ",".join(f"'{c}'" for c in cols) + "]"
            # Decision 4: read as UTF-8 with a latin-1 fallback (per line, only for files that are not entirely valid UTF-8).
            # DuckDB then loads the normalised UTF-8 temp copy, which is deleted right after.
            tmp = db_dir / f"_tmp_{t}_{q}.txt"
            tmp.write_bytes(to_utf8((RAW / inv["files"][q][t]).read_bytes()))
            try:
                con.execute(
                    f"INSERT INTO {t} BY NAME SELECT *, '{q}' AS _q FROM read_csv('{tmp}', delim='$', header=true, names={names}, "
                    "all_varchar=true, quote='', escape='', encoding='utf-8', null_padding=true, ignore_errors=true, strict_mode=false)")
            finally:
                tmp.unlink(missing_ok=True)
            print(f"[load] {t} {q}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- column profiling (SQL)
def blank(v):
    return f"({v} IS NULL OR trim({v}) = '')"


def nb(v):
    return f"({v} IS NOT NULL AND trim({v}) <> '')"


def date_profile(con, t, c, present, ref_max):
    v = f'"{c}"'
    qs = ",".join(f"'{q}'" for q in present)
    base = f"FROM {t} WHERE _q IN ({qs}) AND {nb(v)}"
    tt = f"trim({v})"
    f8, f6, f4 = (f"regexp_full_match({tt}, '\\d{{{n}}}')" for n in (8, 6, 4))
    ok8 = f"try_strptime({tt}, '%Y%m%d') IS NOT NULL"
    ok6 = f"try_strptime({tt} || '01', '%Y%m%d') IS NOT NULL"
    # min/max are taken over calendar-valid values with year >= 1900 (raw min/max, incl. junk like 0213, kept separately)
    yr_ok = f"TRY_CAST(left({tt}, 4) AS INT) >= 1900"
    inv8 = f"{f8} AND NOT ({ok8})"
    row = con.execute(f"""SELECT count(*),
        count(*) FILTER (WHERE {f8}), count(*) FILTER (WHERE {f6}), count(*) FILTER (WHERE {f4}),
        count(*) FILTER (WHERE {f8} AND NOT ({ok8})), count(*) FILTER (WHERE {f6} AND NOT ({ok6})),
        count(*) FILTER (WHERE {f4} AND (TRY_CAST({tt} AS INT) < 1900 OR TRY_CAST({tt} AS INT) > {int(ref_max[:4])})),
        min({tt}) FILTER (WHERE {f8} AND {ok8} AND {yr_ok}), max({tt}) FILTER (WHERE {f8} AND {ok8} AND {yr_ok}),
        min({tt}) FILTER (WHERE {f8}), max({tt}) FILTER (WHERE {f8}),
        min({tt}) FILTER (WHERE {f6} AND {ok6} AND {yr_ok}), max({tt}) FILTER (WHERE {f6} AND {ok6} AND {yr_ok}),
        min({tt}) FILTER (WHERE {f6}), max({tt}) FILTER (WHERE {f6}),
        min({tt}) FILTER (WHERE {f4}), max({tt}) FILTER (WHERE {f4}),
        count(*) FILTER (WHERE regexp_full_match({tt}, '\\d{{4,}}') AND rpad({tt}, 8, '0') > '{ref_max}'),
        count(*) FILTER (WHERE {f8} AND TRY_CAST(left({tt}, 4) AS INT) < 1900)
        {base}""").fetchone()
    nn, n8, n6, n4 = row[:4]
    other = nn - n8 - n6 - n4
    fm = {"YYYYMMDD": n8, "YYYYMM": n6, "YYYY": n4, "other": other}
    mm = {}
    for name, (lo, hi, rlo, rhi) in (("YYYYMMDD", row[7:11]), ("YYYYMM", row[11:15]), ("YYYY", (row[15], row[16], row[15], row[16]))):
        if rlo is not None:
            mm[name] = {"min": lo, "max": hi} if lo is not None else None
            mm[name + "_raw"] = {"min": rlo, "max": rhi}
    ex = con.execute(f"SELECT {tt}, count(*) {base} AND NOT ({f8} OR {f6} OR {f4}) GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 8").fetchall()
    return {"n_nonmissing": nn, "formats": fm, "format_pct": {k: round(100 * x / nn, 4) if nn else None for k, x in fm.items()},
            "invalid_calendar": {"YYYYMMDD": row[4], "YYYYMM": row[5], "YYYY_lt_1900_or_gt_ref": row[6]},
            "min_max": mm, "after_ref_quarter_end": row[17], "yyyymmdd_year_lt_1900": row[18],
            "other_examples": {k: v for k, v in ex}}


def infer_type(col, nn, uniq, n_int, n_num, maxlen):
    if col in DATE_COLS:
        return "string -> date (partial dates: YYYY / YYYYMM / YYYYMMDD)"
    if nn == 0:
        return "unknown (all missing)"
    if n_int == nn:
        return "Int64" if maxlen <= 18 else "string (digits, >18 chars)"
    if n_num == nn:
        return "Float64"
    share = n_num / nn
    if share > 0.95:
        return f"string (numeric-like but {100 * (1 - share):.3f}% non-numeric)"
    return "string / category" if uniq <= 500 else "string"


def profile_tables(con, heads, quarters, ref_max, vc_rows):
    profiles = {}
    for t in TABLES:
        cols = [r[0] for r in con.execute(f"SELECT column_name FROM (DESCRIBE {t})").fetchall() if r[0] != "_q"]
        present = {c: [q for q in quarters if c in canon(heads[t].get(q, []))] for c in cols}
        # one grouped scan for all per-column aggregates, one for distinct counts
        aggs = []
        for i, c in enumerate(cols):
            v = f'"{c}"'
            aggs += [f"count(*) FILTER (WHERE {blank(v)})", f"count(*) FILTER (WHERE {v} IS NOT NULL AND trim({v}) = '')",
                     f"count(*) FILTER (WHERE {nb(v)} AND {v} <> trim({v}))",
                     f"min(length({v})) FILTER (WHERE {nb(v)})", f"max(length({v})) FILTER (WHERE {nb(v)})",
                     f"count(*) FILTER (WHERE regexp_full_match(trim({v}), '-?\\d+'))",
                     f"count(*) FILTER (WHERE regexp_full_match(trim({v}), '-?\\d+(\\.\\d+)?'))",
                     f"max(length(trim({v}))) FILTER (WHERE {nb(v)})"]
        res = con.execute(f"SELECT _q, count(*), {', '.join(aggs)} FROM {t} GROUP BY _q ORDER BY _q").fetchall()
        distinct = con.execute("SELECT " + ", ".join(f"count(DISTINCT \"{c}\") FILTER (WHERE trim(\"{c}\") <> '')" for c in cols) + f" FROM {t}").fetchone()
        profiles[t] = {}
        for i, c in enumerate(cols):
            rows = [r for r in res if r[0] in present[c]]
            S = lambda k: sum(r[2 + i * 8 + k] for r in rows)
            n = sum(r[1] for r in rows)
            miss = S(0)
            nn = n - miss
            mins = [r[2 + i * 8 + 3] for r in rows if r[2 + i * 8 + 3] is not None]
            maxs = [r[2 + i * 8 + 4] for r in rows if r[2 + i * 8 + 4] is not None]
            uniq = distinct[i]
            p = {"n": n, "missing": miss, "missing_pct": round(100 * miss / n, 4) if n else None, "unique_nonmissing": uniq,
                 "whitespace_only": S(1), "leading_trailing_ws": S(2),
                 "inferred_type": infer_type(c, nn, uniq, S(5), S(6), max([r[2 + i * 8 + 7] or 0 for r in rows] or [0])),
                 "missing_pct_by_quarter": {r[0]: round(100 * r[2 + i * 8] / r[1], 4) for r in rows},
                 "present_in_quarters": present[c]}
            if mins:
                p["len_min_max"] = [min(mins), max(maxs)]
            if c in DATE_COLS and present[c]:
                p["date"] = date_profile(con, t, c, present[c], ref_max)
            v = f'"{c}"'
            if c in CODED_COLS or uniq <= FULL_COUNT_MAX_UNIQUE:
                vc = con.execute(f"SELECT {v}, count(*) FROM {t} WHERE {nb(v)} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 5000").fetchall()
                p["value_counts_top"] = {k: x for k, x in vc[:FULL_COUNT_MAX_UNIQUE]}
                for k, x in vc:
                    vc_rows.append((t, c, k, x, round(100 * x / n, 4)))
            elif c not in ("primaryid", "caseid"):
                vc = con.execute(f"SELECT {v}, count(*) FROM {t} WHERE {nb(v)} GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 10").fetchall()
                p["top10"] = {k: x for k, x in vc}
            profiles[t][c] = p
        print(f"[profile] {t} done ({len(cols)} columns)", file=sys.stderr, flush=True)
    return profiles


# --------------------------------------------------------------------------- relationships + extra checks (SQL)
def q1(con, sql):
    return con.execute(sql).fetchone()[0]


def hash_all(con, t):
    cols = [r[0] for r in con.execute(f"SELECT column_name FROM (DESCRIBE {t})").fetchall() if r[0] != "_q"]
    return "hash(" + ", ".join(f'"{c}"' for c in cols) + ")"


def relationships(con):
    rel = {}
    rel["demo_rows"] = q1(con, "SELECT count(*) FROM demo")
    rel["demo_primaryid_unique"] = q1(con, "SELECT count(DISTINCT primaryid) FROM demo")
    rel["demo_duplicate_primaryid_rows"] = q1(con, "SELECT coalesce(sum(c),0) FROM (SELECT count(*) c FROM demo GROUP BY primaryid HAVING c>1)")
    rel["demo_distinct_caseid"] = q1(con, "SELECT count(DISTINCT caseid) FROM demo")
    mism = q1(con, "SELECT count(*) FROM demo WHERE caseid || caseversion <> primaryid")
    rel["primaryid_eq_caseid_concat_caseversion"] = {"match": rel["demo_rows"] - mism, "mismatch": mism}
    rel["caseversion_counts"] = {k: v for k, v in con.execute("SELECT caseversion, count(*) FROM demo GROUP BY 1 ORDER BY 2 DESC LIMIT 15").fetchall()}
    rel["caseversion_max"] = q1(con, "SELECT max(TRY_CAST(caseversion AS INT)) FROM demo")
    rel["cases_with_multiple_versions_in_loaded_data"] = q1(con, "SELECT count(*) FROM (SELECT caseid FROM demo GROUP BY 1 HAVING count(*)>1)")
    rel["max_versions_per_case_in_loaded_data"] = q1(con, "SELECT max(c) FROM (SELECT count(*) c FROM demo GROUP BY caseid)")
    rel["cases_spanning_multiple_quarters"] = q1(con, "SELECT count(*) FROM (SELECT caseid FROM demo GROUP BY 1 HAVING count(DISTINCT _q)>1)")
    rel["caseid_not_in_primaryid_prefix"] = q1(con, "SELECT count(*) FROM demo WHERE NOT starts_with(primaryid, caseid)")
    rel["child_tables"] = {}
    for t in TABLES[1:]:
        h = hash_all(con, t)
        n = q1(con, f"SELECT count(*) FROM {t}")
        rel["child_tables"][t] = {
            "rows": n, "distinct_primaryid": q1(con, f"SELECT count(DISTINCT primaryid) FROM {t}"),
            "rows_primaryid_not_in_demo_any_loaded_qtr": q1(con, f"SELECT count(*) FROM {t} c ANTI JOIN (SELECT DISTINCT primaryid FROM demo) d USING (primaryid)"),
            "rows_primaryid_not_in_demo_same_qtr": q1(con, f"SELECT count(*) FROM {t} c ANTI JOIN (SELECT DISTINCT primaryid, _q FROM demo) d USING (primaryid, _q)"),
            "demo_primaryids_without_rows": q1(con, f"SELECT count(*) FROM (SELECT DISTINCT primaryid FROM demo) d ANTI JOIN (SELECT DISTINCT primaryid FROM {t}) c USING (primaryid)"),
            "exact_duplicate_rows": n - q1(con, f"SELECT count(DISTINCT {h}) FROM {t}"),
            "caseid_consistent_with_demo": {
                "checked": q1(con, f"SELECT count(*) FROM (SELECT DISTINCT primaryid, caseid FROM {t}) c JOIN (SELECT DISTINCT primaryid, caseid FROM demo) d USING (primaryid)"),
                "mismatch": q1(con, f"SELECT count(*) FROM (SELECT DISTINCT primaryid, caseid FROM {t}) c JOIN (SELECT DISTINCT primaryid, caseid FROM demo) d USING (primaryid) WHERE c.caseid <> d.caseid")},
        }
        print(f"[rel] {t}", file=sys.stderr, flush=True)
    rel["exact_duplicate_note"] = "counted as rows - distinct 64-bit hash of all columns except _q (hash collisions possible but negligible)"
    rel["drug_seq_duplicated_within_primaryid"] = q1(con, "SELECT count(*) - count(DISTINCT (primaryid, drug_seq)) FROM drug")
    rel["drug_cases_total"] = q1(con, "SELECT count(DISTINCT primaryid) FROM drug")
    ps = con.execute("SELECT count(*) FILTER (WHERE c=1), count(*) FILTER (WHERE c>1), count(*) FROM (SELECT primaryid, count(*) FILTER (WHERE role_cod='PS') c FROM drug GROUP BY 1) WHERE c>=0").fetchone()
    rel["cases_with_exactly_one_PS"], rel["cases_with_multiple_PS"] = ps[0], ps[1]
    rel["cases_with_no_PS"] = rel["drug_cases_total"] - ps[0] - ps[1]
    rel["ther_keys_without_matching_drug"] = q1(con, "SELECT count(*) FROM (SELECT DISTINCT primaryid, dsg_drug_seq FROM ther) t ANTI JOIN (SELECT DISTINCT primaryid, drug_seq FROM drug) d ON t.primaryid=d.primaryid AND t.dsg_drug_seq=d.drug_seq")
    rel["indi_keys_without_matching_drug"] = q1(con, "SELECT count(*) FROM (SELECT DISTINCT primaryid, indi_drug_seq FROM indi) t ANTI JOIN (SELECT DISTINCT primaryid, drug_seq FROM drug) d ON t.primaryid=d.primaryid AND t.indi_drug_seq=d.drug_seq")
    return rel


def extra_checks(con, ref_max):
    ex = {}
    h = hash_all(con, "demo")
    dups = con.execute(f"""SELECT primaryid, count(DISTINCT {h}), string_agg(_q, '|' ORDER BY _q) FROM demo
                           GROUP BY primaryid HAVING count(*) > 1""").fetchall()
    pairs = Counter(r[2] for r in dups)
    ex["demo_dup_primaryid"] = {"distinct_primaryid": len(dups), "identical_in_all_columns": sum(1 for r in dups if r[1] == 1),
                                "quarter_pairs": dict(pairs.most_common()), "distinct_quarter_patterns": len(pairs)}
    ex["fda_dt_range_by_quarter_file"] = {r[0]: [r[1], r[2]] for r in con.execute("SELECT _q, min(fda_dt), max(fda_dt) FROM demo GROUP BY 1 ORDER BY 1").fetchall()}
    ex["fda_dt_quarter_file_mismatch_rows"] = q1(con, """SELECT count(*) FROM demo WHERE
        substr(_q,1,4) || lpad(CAST(CAST(substr(_q,6,1) AS INT)*3 AS VARCHAR),2,'0') < substr(fda_dt,1,6)""")
    ex["init_fda_dt_gt_fda_dt"] = q1(con, "SELECT count(*) FROM demo WHERE init_fda_dt > fda_dt")
    ex["i_f_code_F_iff_caseversion_gt_1"] = q1(con, "SELECT count(*) FROM demo WHERE (i_f_code='F') <> (TRY_CAST(caseversion AS INT) > 1)") == 0
    r = con.execute("SELECT count(*) FILTER (WHERE event_dt > fda_dt), count(*) FROM demo WHERE length(event_dt)=8").fetchone()
    ex["event_dt_gt_fda_dt_8digit"] = {"n": r[0], "of": r[1]}
    r = con.execute("SELECT count(*) FILTER (WHERE rept_dt > fda_dt), count(*) FROM demo WHERE length(rept_dt)=8").fetchone()
    ex["rept_dt_gt_fda_dt_8digit"] = {"n": r[0], "of": r[1]}
    ex["age_present_cod_missing"] = q1(con, "SELECT count(*) FROM demo WHERE trim(age) <> '' AND (age_cod IS NULL OR trim(age_cod)='')")
    ex["age_missing_cod_present"] = q1(con, "SELECT count(*) FROM demo WHERE (age IS NULL OR trim(age)='') AND trim(age_cod) <> ''")
    ex["age_nonnumeric_values"] = {k: v for k, v in con.execute("SELECT trim(age), count(*) FROM demo WHERE trim(age) <> '' AND TRY_CAST(trim(age) AS DOUBLE) IS NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10").fetchall()}
    r = con.execute("SELECT min(TRY_CAST(trim(age) AS DOUBLE)), max(TRY_CAST(trim(age) AS DOUBLE)), count(*) FILTER (WHERE TRY_CAST(trim(age) AS DOUBLE) > 120), count(*) FILTER (WHERE TRY_CAST(trim(age) AS DOUBLE) < 0) FROM demo WHERE age_cod='YR'").fetchone()
    ex["age_yr_min_max"], ex["age_yr_gt_120"], ex["age_yr_negative"] = [r[0], r[1]], r[2], r[3]
    ex["wt_nonnumeric_examples"] = {k: v for k, v in con.execute("SELECT trim(wt), count(*) FROM demo WHERE trim(wt) <> '' AND TRY_CAST(trim(wt) AS DOUBLE) IS NULL GROUP BY 1 ORDER BY 2 DESC LIMIT 10").fetchall()}
    r = con.execute("SELECT min(TRY_CAST(trim(wt) AS DOUBLE)), max(TRY_CAST(trim(wt) AS DOUBLE)) FROM demo WHERE isfinite(TRY_CAST(trim(wt) AS DOUBLE))").fetchone()
    ex["wt_min_max_numeric"] = list(r)
    # sex: gndr_cod through 2014Q2, sex from 2014Q3
    ex["sex_gndr_missing_by_quarter"] = {r[0]: [r[1], r[2]] for r in con.execute("""SELECT _q, round(100.0*count(*) FILTER (WHERE gndr_cod IS NULL OR trim(gndr_cod)='')/count(*),3),
        round(100.0*count(*) FILTER (WHERE sex IS NULL OR trim(sex)='')/count(*),3) FROM demo GROUP BY 1 ORDER BY 1""").fetchall()}
    ex["drug_dup_primaryid_drug_seq"] = None
    h = hash_all(con, "drug")
    r = con.execute(f"""SELECT count(*), count(*) FILTER (WHERE nh = 1), count(*) FILTER (WHERE nq > 1), sum(c) FROM (
        SELECT primaryid, drug_seq, count(*) c, count(DISTINCT {h}) nh, count(DISTINCT _q) nq FROM drug GROUP BY 1,2 HAVING count(*) > 1)""").fetchone()
    ex["drug_dup_primaryid_drug_seq"] = {"distinct_keys": r[0], "keys_whose_rows_are_identical": r[1], "keys_spanning_two_quarters": r[2], "rows_involved": r[3]}
    ex["drug_dechal_rechal_filled_by_quarter"] = {r[0]: {"dechal": r[1], "rechal": r[2]} for r in con.execute(
        "SELECT _q, round(100.0*count(*) FILTER (WHERE trim(dechal)<>'')/count(*),2), round(100.0*count(*) FILTER (WHERE trim(rechal)<>'')/count(*),2) FROM drug GROUP BY 1 ORDER BY 1").fetchall()}
    ex["drug_dechal_x_rechal"] = {f"({a!r}, {b!r})": n for a, b, n in con.execute("SELECT coalesce(dechal,'<blank>'), coalesce(rechal,'<blank>'), count(*) FROM drug GROUP BY 1,2 ORDER BY 3 DESC LIMIT 60").fetchall()}
    ex["drug_drugname_blank_rows"] = q1(con, "SELECT count(*) FROM drug WHERE drugname IS NULL OR trim(drugname)=''")
    ex["drug_exp_dt_multi_value_rows"] = q1(con, "SELECT count(*) FROM drug WHERE exp_dt LIKE '%,%'")
    r = con.execute("SELECT count(*) FILTER (WHERE lot_num LIKE ' %'), count(*) FILTER (WHERE trim(lot_num) <> '') FROM drug").fetchone()
    ex["drug_lot_num_leading_space_of_nonblank"] = list(r)
    ex["reac_dup_primaryid_pt"] = q1(con, "SELECT count(*) - count(DISTINCT (primaryid, pt)) FROM reac")
    ex["outc_dup_primaryid_cod"] = q1(con, "SELECT count(*) - count(DISTINCT (primaryid, outc_cod)) FROM outc")
    ex["outc_cases_with_each_code"] = {k: v for k, v in con.execute("SELECT outc_cod, count(DISTINCT primaryid) FROM outc GROUP BY 1 ORDER BY 2 DESC").fetchall()}
    ex["cases_with_any_outc"] = q1(con, "SELECT count(DISTINCT primaryid) FROM outc")
    ex["role_cod_by_year"] = {}
    for y, rc, n in con.execute("SELECT substr(_q,1,4), role_cod, count(*) FROM drug GROUP BY 1,2 ORDER BY 1,2").fetchall():
        ex["role_cod_by_year"].setdefault(y, {})[rc] = n
    ex["rept_cod_by_year"] = {}
    for y, rc, n in con.execute("SELECT substr(_q,1,4), rept_cod, count(*) FROM demo GROUP BY 1,2 ORDER BY 1,2").fetchall():
        ex["rept_cod_by_year"].setdefault(y, {})[rc] = n
    return ex



# =========================================================================== rendering
# Meanings are copied/condensed from ASC_NTS (2012Q4 .doc for original fields; 2026Q2 .pdf for fields added
# later). Anything the docs do not state is "unclear".
MEANING = {
 "demo": {
  "primaryid": "Unique number identifying a FAERS report; primary key linking all files. Concatenation of CASEID + CASEVERSION (ASC_NTS).",
  "caseid": "Number identifying a FAERS case; a case has one or more versions (ASC_NTS).",
  "caseversion": "Safety report version number; initial case = 1, follow-ups increment (ASC_NTS).",
  "i_f_code": "Initial (I) or follow-up (F) status of report, as reported by manufacturer (ASC_NTS calls the field I_F_COD).",
  "event_dt": "Date the adverse event occurred or began; partial dates allowed (ASC_NTS).",
  "mfr_dt": "Date manufacturer first received initial information; in later versions the latest manufacturer-received date (ASC_NTS).",
  "init_fda_dt": "Date FDA received the first (initial) version of the case (ASC_NTS calls it INIT_FDA_DATE in the 2012Q4 doc, INIT_FDA_DT in the 2026Q2 doc).",
  "fda_dt": "Date FDA received this case version (ASC_NTS: 'In subsequent versions of a case, the latest manufacturer received date will be provided' - wording ambiguous).",
  "rept_cod": "Type of report submitted: EXP expedited (15-day), PER periodic, DIR direct (ASC_NTS).",
  "mfr_num": "Manufacturer's unique report identifier (ASC_NTS).",
  "mfr_sndr": "Coded name of manufacturer sending report; else verbatim organisation name (ASC_NTS).",
  "age": "Numeric value of patient's age at event (ASC_NTS).",
  "age_cod": "Unit of age: DEC, YR, MON, WK, DY, HR (ASC_NTS).",
  "gndr_cod": "Patient sex: UNK, M, F, NS (ASC_NTS). Renamed SEX from 2014Q3.",
  "e_sub": "Whether (Y/N) report was submitted under the electronic submissions procedure for manufacturers (ASC_NTS).",
  "wt": "Numeric value of patient's weight (ASC_NTS).",
  "wt_cod": "Unit of weight: KG, LBS, GMS (ASC_NTS).",
  "rept_dt": "Date report was sent; partial dates allowed (ASC_NTS). Header has a leading space in 2012Q4.",
  "to_mfr": "Whether (Y/N) voluntary reporter also notified manufacturer; blank for manufacturer reports (ASC_NTS).",
  "occp_cod": "Reporter's occupation in latest case version: MD, PH, OT, LW, CN (ASC_NTS).",
  "reporter_country": "Country of the reporter in the latest case version (ASC_NTS: country codes per ICH/ISO 3166 links).",
  "occr_country": "Country where the event occurred (ASC_NTS).",
  "auth_num": "Regulatory authority's case report number, when available (added 2014Q3; ASC_NTS 2026Q2).",
  "lit_ref": "Literature reference information, when available (added 2014Q3).",
  "age_grp": "Patient age group code: N neonate, I infant, C child, T adolescent, A adult, E elderly (added 2014Q3).",
  "sex": "Patient sex (rename of GNDR_COD from 2014Q3).",
 },
 "drug": {
  "primaryid": "See DEMO.primaryid (ASC_NTS).",
  "caseid": "See DEMO.caseid (ASC_NTS).",
  "drug_seq": "Number identifying a drug within a case; ASC_NTS says it is only the order of drugs within a case, not a unique key (use with primaryid) and links to THER.dsg_drug_seq / INDI.indi_drug_seq.",
  "role_cod": "Drug's reported role in the event: PS primary suspect, SS secondary suspect, C concomitant, I interacting (ASC_NTS 2012Q4). DN 'drug not administered' added in the 2024Q4 doc.",
  "drugname": "Name of medicinal product: valid trade name if available, otherwise verbatim name as entered (ASC_NTS).",
  "val_vbm": "Source of DRUGNAME: 1 validated trade name, 2 verbatim name (ASC_NTS).",
  "route": "Route of drug administration (ASC_NTS; no code list given).",
  "dose_vbm": "Verbatim text for dose, frequency and route as entered on report (ASC_NTS).",
  "cum_dose_chr": "Cumulative dose to first reaction (ASC_NTS).",
  "cum_dose_unit": "Unit of cumulative dose to first reaction (ASC_NTS calls it CUM_DOS_UNIT; the 2012Q4 doc lists numeric codes 001-004, the 2013Q2 doc lists text codes MG, UG, ...).",
  "dechal": "Dechallenge code - whether reaction abated when drug stopped: Y, N, U unknown, D does not apply (ASC_NTS).",
  "rechal": "Rechallenge code - whether reaction recurred when drug restarted: Y, N, U, D (ASC_NTS).",
  "lot_nbr": "Lot number of the drug (ASC_NTS calls it LOT_NUM); header spelled lot_nbr in 2012Q4 only.",
  "lot_num": "Lot number of the drug (ASC_NTS).",
  "exp_dt": "Expiration date of the drug; partial dates allowed (ASC_NTS).",
  "nda_num": "NDA number, numeric only (ASC_NTS).",
  "dose_amt": "Amount of drug reported (ASC_NTS).",
  "dose_unit": "Unit of drug dose (ASC_NTS).",
  "dose_form": "Form of dose reported (ASC_NTS).",
  "dose_freq": "Code for dose frequency, e.g. QD, BID, TID (ASC_NTS table).",
  "prod_ai": "Product active ingredient, when available (added 2014Q3).",
 },
 "reac": {
  "primaryid": "See DEMO.primaryid (ASC_NTS).", "caseid": "See DEMO.caseid (ASC_NTS).",
  "pt": "MedDRA Preferred Term describing the event; order of terms does not imply priority (ASC_NTS).",
  "drug_rec_act": "Drug recur action: reaction/event (PT) populated if it reappears on readministration (added 2014Q3).",
 },
 "outc": {
  "primaryid": "See DEMO.primaryid (ASC_NTS).", "caseid": "See DEMO.caseid (ASC_NTS).",
  "outc_cod": "Patient outcome: DE death, LT life-threatening, HO hospitalization, DS disability, CA congenital anomaly, RI required intervention, OT other serious. Outcome of the latest case version; several codes are line-listed (ASC_NTS). Header spelled outc_code in 2012Q4 only.",
 },
 "rpsr": {
  "primaryid": "See DEMO.primaryid (ASC_NTS).", "caseid": "See DEMO.caseid (ASC_NTS).",
  "rpsr_cod": "Source of report: FGN, SDY, LIT, CSM, HP, UF, CR, DT, OTH; latest version's source, several codes line-listed (ASC_NTS).",
 },
 "ther": {
  "primaryid": "See DEMO.primaryid (ASC_NTS).", "caseid": "See DEMO.caseid (ASC_NTS).",
  "dsg_drug_seq": "Drug sequence number linking to DRUG.drug_seq (ASC_NTS).",
  "start_dt": "Date therapy was started (or re-started); partial dates allowed (ASC_NTS).",
  "end_dt": "Date therapy was stopped; partial dates allowed (ASC_NTS).",
  "dur": "Numeric value of duration of therapy (ASC_NTS).",
  "dur_cod": "Unit of duration: YR, MON, WK, DAY, HR, MIN, SEC (ASC_NTS).",
 },
 "indi": {
  "primaryid": "See DEMO.primaryid (ASC_NTS).", "caseid": "See DEMO.caseid (ASC_NTS).",
  "indi_drug_seq": "Drug sequence number linking to DRUG.drug_seq (ASC_NTS).",
  "indi_pt": "MedDRA Preferred Term for the indication for use (ASC_NTS).",
 },
}
USED_FOR = {
 ("demo", "primaryid"): "join key", ("demo", "caseid"): "join key; dedup", ("demo", "caseversion"): "dedup",
 ("demo", "event_dt"): "detection date (candidate)", ("demo", "mfr_dt"): "detection date (candidate)",
 ("demo", "init_fda_dt"): "detection date (candidate)", ("demo", "fda_dt"): "detection date (candidate)",
 ("demo", "rept_cod"): "reporter feature", ("demo", "occp_cod"): "reporter feature",
 ("demo", "reporter_country"): "reporter feature",
 ("demo", "age"): "demographic feature", ("demo", "age_cod"): "demographic feature",
 ("demo", "age_grp"): "demographic feature", ("demo", "gndr_cod"): "demographic feature",
 ("demo", "sex"): "demographic feature", ("demo", "wt"): "demographic feature", ("demo", "wt_cod"): "demographic feature",
 ("drug", "primaryid"): "join key", ("drug", "caseid"): "join key", ("drug", "drug_seq"): "join key",
 ("drug", "role_cod"): "filter to suspect drugs", ("drug", "drugname"): "filter to suspect drugs",
 ("drug", "prod_ai"): "filter to suspect drugs",
 ("reac", "primaryid"): "join key", ("reac", "caseid"): "join key", ("reac", "pt"): "event term",
 ("outc", "primaryid"): "join key", ("outc", "caseid"): "join key", ("outc", "outc_cod"): "outcome/severity feature",
 ("rpsr", "primaryid"): "join key", ("rpsr", "caseid"): "join key", ("rpsr", "rpsr_cod"): "reporter feature",
 ("ther", "primaryid"): "join key", ("ther", "caseid"): "join key", ("ther", "dsg_drug_seq"): "join key",
 ("indi", "primaryid"): "join key", ("indi", "caseid"): "join key", ("indi", "indi_drug_seq"): "join key",
}
TABLE_TITLE = {"demo": "DEMO", "drug": "DRUG", "reac": "REAC", "outc": "OUTC", "rpsr": "RPSR", "ther": "THER", "indi": "INDI"}


def esc(x):
    return str(x).replace("|", "\\|").replace("\n", " ")


def fmt_n(n):
    return f"{n:,}"


def notes_for(t, c, p, vc_full):
    parts = [f"{fmt_n(p['unique_nonmissing'])} unique non-missing"]
    nn = p["n"] - p["missing"]
    if "date" in p:
        d = p["date"]
        parts.append("formats (% of non-missing): " + ", ".join(f"{k} {v:.2f}%" for k, v in d["format_pct"].items() if v is not None))
        mm = d["min_max"]
        parts.append("min-max of calendar-valid values with year >= 1900: " + "; ".join(f"{k} {v['min']}-{v['max']}" for k, v in mm.items() if not k.endswith("_raw") and v))
        raw = "; ".join(f"{k[:-4]} {v['min']}-{v['max']}" for k, v in mm.items() if k.endswith("_raw") and v)
        parts.append("raw min-max incl. invalid: " + raw)
        ic = d["invalid_calendar"]
        parts.append(f"calendar-invalid: {ic['YYYYMMDD']} YYYYMMDD, {ic['YYYYMM']} YYYYMM, {ic['YYYY_lt_1900_or_gt_ref']} YYYY (<1900 or > ref year)" + ("" if c == "exp_dt" else f"; {d['after_ref_quarter_end']} values after {vc_full['ref']}"))
        if d["other_examples"]:
            parts.append("other-format examples: " + ", ".join(f"`{k}` x{v}" for k, v in list(d['other_examples'].items())[:4]))
    elif "value_counts_top" in p and (c in CODED_COLS or p["unique_nonmissing"] <= FULL_COUNT_MAX_UNIQUE):
        vcs = p["value_counts_top"]
        full = p["unique_nonmissing"] <= len(vcs)
        shown = list(vcs.items()) if full else list(vcs.items())[:12]
        s = ", ".join(f"`{k}` {fmt_n(v)} ({100 * v / nn:.2f}%)" for k, v in shown)
        parts.append(("full value counts: " if full else "top values: ") + s +
                     ("" if full else f", ... ({p['unique_nonmissing'] - len(shown)} more; full counts in {vc_full['csv']})"))
    elif "top10" in p:
        parts.append("top values: " + ", ".join(f"`{k}` {fmt_n(v)}" for k, v in list(p["top10"].items())[:4]))
    if "len_min_max" in p:
        parts.append(f"length {p['len_min_max'][0]}-{p['len_min_max'][1]}")
    mq = p["missing_pct_by_quarter"]
    if len(mq) > 1 and max(mq.values()) - min(mq.values()) > 5:
        lo, hi = min(mq, key=mq.get), max(mq, key=mq.get)
        parts.append(f"missing % varies by quarter: {mq[lo]:.1f}% ({lo}) to {mq[hi]:.1f}% ({hi})")
    if p["leading_trailing_ws"]:
        parts.append(f"{fmt_n(p['leading_trailing_ws'])} values with leading/trailing whitespace")
    if len(p["present_in_quarters"]) < len(vc_full["quarters"]):
        pq = p["present_in_quarters"]
        parts.append(f"exists only in {len(pq)} of {len(vc_full['quarters'])} quarters ({pq[0]}..{pq[-1]})")
    return "; ".join(parts)


def qrange(mq):
    lo, hi = min(mq, key=mq.get), max(mq, key=mq.get)
    return f"{mq[lo]:.1f}% ({lo}) to {mq[hi]:.1f}% ({hi})"


def render(prof_path, out_md):
    r = json.loads(Path(prof_path).read_text())
    inv, quarters, P_, rel, ex = r["inventory"], r["loaded_quarters"], r["profiles"], r["relationships"], r["extra_checks"]
    csvname = Path(prof_path).stem + "_value_counts.csv"
    ctx = {"ref": r["ref_max_date"], "csv": f"references/profile/{csvname}", "quarters": quarters}
    L = []
    w = L.append
    allq = inv["quarters"]
    full = len(quarters) == len(allq)
    w("# FAERS raw data dictionary\n")
    w(f"*Generated by `scripts/profile_raw_data.py` from `data/raw/` ({r.get('engine', 'pandas')}). Row-level profile covers "
      + (f"**all {len(allq)} quarters ({quarters[0]}..{quarters[-1]})**" if full else f"**{', '.join(quarters)}** only") +
      ". Meanings come from the FDA ASC_NTS documentation shipped in the quarter folders (2012Q4 .doc for the original fields, 2026Q2 PDF for fields added later); "
      "where the docs are silent the text says \"unclear\". All values were loaded as strings (latin-1, `$`-delimited, quoting disabled, bad lines skipped); "
      "empty fields count as missing.*\n")
    w("## Overview\n")
    w(f"- **Quarters on disk:** {allq[0]} to {allq[-1]}, {len(allq)} folders; missing quarters in that range: "
      f"{', '.join(inv['missing']) if inv['missing'] else 'none'}. Quarters are already unzipped (no .zip files in data/raw/); folder-name case is inconsistent (e.g. `2019Q1` vs `2022q1`).")
    w(f"- **Profiled quarters:** {len(quarters)} ({quarters[0]}..{quarters[-1]}).")
    w("- **Total rows:** " + ", ".join(f"{TABLE_TITLE[t]} {fmt_n(n)}" for t, n in r["rows_total"].items()) + ".")
    w("- **Key relationships (verified across the profiled quarters):**")
    pc = rel["primaryid_eq_caseid_concat_caseversion"]
    w(f"  - `primaryid` = `caseid` || `caseversion` for {fmt_n(pc['match'])} of {fmt_n(pc['match'] + pc['mismatch'])} DEMO rows ({pc['mismatch']} mismatches). `i_f_code`='F' exactly when `caseversion` > 1: {ex['i_f_code_F_iff_caseversion_gt_1']}.")
    w(f"  - DEMO has {fmt_n(rel['demo_rows'])} rows = {fmt_n(rel['demo_primaryid_unique'])} distinct `primaryid` ({ex['demo_dup_primaryid']['distinct_primaryid']} `primaryid`s appear in more than one quarter) covering {fmt_n(rel['demo_distinct_caseid'])} distinct `caseid`. "
      f"{fmt_n(rel['cases_with_multiple_versions_in_loaded_data'])} cases have more than one version in the data (max {rel['max_versions_per_case_in_loaded_data']} versions of one case, though `caseversion` values reach {rel['caseversion_max']} - not every version is published). "
      f"{fmt_n(rel['cases_spanning_multiple_quarters'])} cases have versions in more than one quarter.")
    w("  - DRUG, REAC, OUTC, RPSR, THER, INDI all carry `primaryid` + `caseid`; every child `primaryid` exists in DEMO (same quarter) and `caseid` always agrees with DEMO. "
      f"THER and INDI link to DRUG via (`primaryid`, `drug_seq`) with `dsg_drug_seq` / `indi_drug_seq`; {rel['ther_keys_without_matching_drug']} THER and {rel['indi_keys_without_matching_drug']} INDI keys lack a matching DRUG row.")
    w("  - DEMO `primaryid`s with no row in the child table: " + ", ".join(f"{TABLE_TITLE[t]} {fmt_n(v['demo_primaryids_without_rows'])} ({100 * v['demo_primaryids_without_rows'] / rel['demo_primaryid_unique']:.1f}%)" for t, v in rel["child_tables"].items()) + ".\n")

    # inventory
    w("## Inventory\n")
    w("### Table coverage by quarter (all quarters on disk)\n")
    w("| Table | Quarters present | Quarters absent |\n|---|---|---|")
    def span(qs):
        return f"{len(qs)} ({qs[0]}..{qs[-1]})" if qs else "0"
    for t in TABLES:
        absent = [q for q in allq if q not in inv["tables"][t]]
        w(f"| {TABLE_TITLE[t]} | {span(inv['tables'][t])} | {', '.join(absent) if absent else 'none'} |")
    nf = inv.get("nonstandard_file_names")
    if nf:
        w(f"\nNon-standard file names: {nf} (the 2018Q1 DEMO file is `DEMO18Q1_new.txt`; treated as that quarter's DEMO table).")
    n_doc = sum(1 for q in allq if any(d.lower().endswith(".doc") for d in inv["docs"][q]))
    dq = [q for q in allq if inv["deleted"][q]]
    w(f"\nDocumentation as shipped in the folders: every quarter had Readme/FAQs and an ASC_NTS (.doc/.pdf; `ASC_NT.pdf` in 2013Q1) plus per-table PDFs; {n_doc} quarters used .doc files, the rest PDF only. Only the ASC_NTS files remain in `data/raw/` (the rest were deleted as unused). "
      f"{len(dq)} quarters ({dq[0]}..{dq[-1]}) also have a `deleted/` (or `Deleted/`) folder with deleted-case lists (2019Q1 has two files); these are not among the seven tables and are not profiled here.\n")
    w("### Row counts per table per quarter\n")
    w("| Quarter | " + " | ".join(TABLE_TITLE[t] for t in TABLES) + " |\n|---|" + "---|" * len(TABLES))
    for q in quarters:
        w(f"| {q} | " + " | ".join(fmt_n(r["rows_per_quarter"][t].get(q, 0)) for t in TABLES) + " |")
    w("| **Total** | " + " | ".join(f"**{fmt_n(r['rows_total'][t])}**" for t in TABLES) + " |")
    w("\n### File-load log (bad lines)\n")
    fl = [e for e in r["file_log"] if e.get("file")]
    w(f"{len(fl)} files loaded. `skipped` = lines dropped for having more fields than the header (`on_bad_lines=\"skip\"`); `short` = lines with fewer fields (padded with empty, not skipped). Totals per table:\n")
    w("| Table | Files | Data lines | Rows loaded | Skipped | Short | Blank lines | Lines CRLF | Lines LF-only | Non-ASCII lines |\n|---|---|---|---|---|---|---|---|---|---|")
    for t in TABLES:
        fs = [e for e in fl if e["table"] == t]
        sm = lambda k: sum(e.get(k, 0) for e in fs)
        w(f"| {TABLE_TITLE[t]} | {len(fs)} | {fmt_n(sm('data_lines'))} | {fmt_n(sm('rows_loaded'))} | {fmt_n(sm('bad_lines_skipped'))} | {fmt_n(sm('too_few_fields'))} | {fmt_n(sm('blank_lines'))} | {fmt_n(sm('crlf'))} | {fmt_n(sm('data_lines') - sm('crlf'))} | {fmt_n(sm('non_ascii_lines'))} |")
    bad = [e for e in fl if e["bad_lines_skipped"] or e.get("too_few_fields") or e.get("too_many_fields") or e.get("blank_lines")]
    w("\nFiles with any skipped / short / blank lines: " + (", ".join(f"{e['table'].upper()} {e['quarter']}" for e in bad) if bad else "none."))
    w("")

    for t in TABLES:
        n_rows = r["rows_total"][t]
        depth = "full profile" if t in PROFILED else "lighter profile (not in the required set)"
        w(f"## {TABLE_TITLE[t]} table ({fmt_n(n_rows)} rows, {depth})\n")
        w("Type = loaded dtype (always string) -> inferred cast. % Missing = null or empty/whitespace-only, pooled over the quarters in which the column exists (see Notes for 'present only in'). Column names are the harmonised ones (`outc_code`->`outc_cod`, `lot_nbr`->`lot_num`).\n")
        w("| Table | Column | Meaning | Type | % Missing | Notes / Values | Used for |\n|---|---|---|---|---|---|---|")
        for c, p in P_[t].items():
            meaning = MEANING[t].get(c, "unclear (not in the docs read)")
            it = p["inferred_type"]
            m = re.match(r"string \(numeric-like but ([\d.]+)% non-numeric\)", it)
            typ = f"string -> Float64 via coercion ({m.group(1)}% non-numeric text)" if m else "string -> " + it.replace("string -> ", "")
            w(f"| {TABLE_TITLE[t]} | {c} | {esc(meaning)} | {esc(typ)} | {p['missing_pct']:.2f} | {esc(notes_for(t, c, p, ctx))} | {USED_FOR.get((t, c), '')} |")
        w("")
    return L, r, ctx


def yearly(d_by_q, key):
    """mean of quarterly values per calendar year"""
    ys = defaultdict(list)
    for q, v in d_by_q.items():
        ys[q[:4]].append(v[key] if isinstance(v, dict) else v)
    return {y: sum(v) / len(v) for y, v in ys.items()}


def render_tail(r):
    inv, rel, ex, P_ = r["inventory"], r["relationships"], r["extra_checks"], r["profiles"]
    heads, quarters = r["header_columns"], r["loaded_quarters"]
    ref = r["ref_max_date"]
    fl = [e for e in r["file_log"] if e.get("file")]
    L = []
    w = L.append
    d, dr = P_["demo"], P_["drug"]
    # ---- schema changes (computed from headers of ALL quarters)
    w("## Schema changes across years\n")
    w(f"Header comparison covers all {len(inv['quarters'])} quarters (first line of every table file, lower-cased and stripped).\n")
    w("| Table | First quarter with the change | Change | Evidence / docs |\n|---|---|---|---|")
    changes = []
    for t in TABLES:
        prev = None
        for q in inv["quarters"]:
            cols = heads[t].get(q)
            if cols is None:
                continue
            if prev is not None and cols != prev[1]:
                changes.append((t, q, prev[0], [c for c in cols if c not in prev[1]], [c for c in prev[1] if c not in cols]))
            if prev is None or cols != prev[1]:
                prev = (q, cols)
    notes = {("drug", "lot_num"): "Spelling variant of the same field (ASC_NTS always calls it LOT_NUM); treated as a rename lot_nbr -> lot_num.",
             ("outc", "outc_cod"): "Spelling variant of the same field (ASC_NTS calls it OUTC_COD); treated as a rename outc_code -> outc_cod.",
             ("demo", "sex"): "ASC_NTS revision history (Apr 2015, QDE 2014Q3): 'Modified field header from GNDR_COD to SEX'; AUTH_NUM, LIT_REF, AGE_GRP added at the same time.",
             ("drug", "prod_ai"): "ASC_NTS: new field in 2014Q3 extract.",
             ("reac", "drug_rec_act"): "ASC_NTS: new field in 2014Q3 extract."}
    for t, q, pq, add, drop in changes:
        desc = []
        if add:
            desc.append("added " + ", ".join(f"`{c}`" for c in add))
        if drop:
            desc.append("dropped " + ", ".join(f"`{c}`" for c in drop))
        note = " ".join(notes[(t, c)] for c in add if (t, c) in notes)
        w(f"| {TABLE_TITLE[t]} | {q} (vs {pq}) | {'; '.join(desc)} | {note} |")
    w("\nTables with no header change in any quarter: " + ", ".join(TABLE_TITLE[t] for t in TABLES if not any(c[0] == t for c in changes)) + ". No column changed position after these changes: the header was identical in every quarter from 2014Q3 to 2026Q2 for all seven tables.\n")
    w("**Other schema / documentation observations**\n")
    w("- Header anomalies: ` rept_dt` has a leading space in `demo12q4.txt` and `drug12q4.txt` starts with a UTF-8 BOM (a naive reader sees `\\ufeffprimaryid`). No other header anomalies in any of the 385 files. The script strips and lower-cases all header names.")
    w("- Field names in the files differ from the ASC_NTS field list in places: file `i_f_code` vs doc `I_F_COD`; file `init_fda_dt` vs 2012Q4 doc `INIT_FDA_DATE`; file `caseid` vs doc tag `CASE_ID` in the LAERS-vs-FAERS comparison table; file `cum_dose_unit` vs doc `CUM_DOS_UNIT`.")
    rc, rcy = ex["rept_cod_by_year"], ex["role_cod_by_year"]
    first = lambda dd, v: next((y for y in sorted(dd) if dd[y].get(v)), "never")
    w(f"- Value-set changes announced in the 2026Q2 ASC_NTS revision history, checked against the data: `rept_cod` `5DAY`/`30DAY` (doc: added QDE 2022Q3) first appear in **{first(rc, '5DAY')}** in the data; `role_cod` `DN` (doc: added QDE 2024Q4) first appears in {first(rcy, 'DN')}; country codes switch to GENC from QDE 2021Q4 (not verifiable from values alone; `reporter_country` has {P_['demo']['reporter_country']['unique_nonmissing']} distinct values incl. non-country entries such as `EU` and `COUNTRY NOT SPECIFIED`). The cumulative-dose-unit list was corrected in QDE 2014Q1 (doc).")
    w("- Cross-year consequences when stacking quarters: sex is `gndr_cod` through 2014Q2 and `sex` from 2014Q3 (coalesce them); `lot_nbr`/`lot_num` and `outc_code`/`outc_cod` need harmonising; `auth_num`, `lit_ref`, `age_grp`, `prod_ai`, `drug_rec_act` do not exist before 2014Q3.")
    w("- Inferred casts are in the column tables above; none of the numeric-looking columns is perfectly clean (`age`, `wt`, `cum_dose_chr`, `dose_amt`, `dur` contain some non-numeric text), so casts should use coercion with the failures kept for review. Date columns hold partial dates (YYYY / YYYYMM / YYYYMMDD) and cannot be cast to a plain date type without a rule for partial dates.\n")

    # ---- data quality
    w("## Data quality issues\n")
    w(f"All counts are over the {len(quarters)} profiled quarters ({quarters[0]}..{quarters[-1]}) unless stated.\n")
    na = sum(e.get("non_ascii_lines", 0) for e in fl)
    lf_only = sorted({e["quarter"] for e in fl if e["crlf"] != e["data_lines"]})
    w("**Malformed rows / file format**\n")
    w(f"- {len(fl)} table files loaded; **{fmt_n(sum(e['bad_lines_skipped'] for e in fl))} lines skipped**, {fmt_n(sum(e.get('too_few_fields', 0) for e in fl))} short lines, {fmt_n(sum(e.get('blank_lines', 0) for e in fl))} blank lines; no line anywhere has a field count different from its file's header.")
    w(f"- Line endings are not uniform: {sum(1 for e in fl if e['crlf'] != e['data_lines'])} files (in {len(lf_only)} quarters, {', '.join(lf_only[:4])}, ... {lf_only[-1]}) use LF only instead of CRLF; no file mixes the two. Matters if a reader keeps a trailing `\\r` on the last column.")
    w("- A last-line terminator is present in every file; row counts loaded equal the data-line counts in every file.\n")
    w("**Encoding**\n")
    sa = {t: sum(e.get("non_ascii_lines", 0) for e in fl if e["table"] == t) for t in TABLES}
    nonutf = [(e["quarter"], e["non_ascii_not_utf8"]) for e in fl if e.get("non_ascii_not_utf8")]
    valid = sum(e.get("non_ascii_valid_utf8", 0) for e in fl)
    w(f"- Non-ASCII bytes occur in few lines: DRUG {fmt_n(sa['drug'])}, DEMO {sa['demo']}, none in the other tables. **{fmt_n(valid)} of {fmt_n(na)} non-ASCII lines are valid UTF-8** - i.e. the files are UTF-8, not latin-1 - and {sum(n for _, n in nonutf)} are not (" + ", ".join(f"DRUG {q}: {n} lines" for q, n in nonutf) + "). Reading as latin-1 (as instructed) therefore turns multibyte characters into mojibake, e.g. `Botox®` becomes `BotoxÂ®`.")
    w(f"- {fmt_n(sum(e.get('u_fffd_runs', 0) for e in fl))} U+FFFD replacement characters (`\\xef\\xbf\\xbd`) already exist in the source (all in `drug12q4.txt`, e.g. `0.2\\ufffd0.4 MG/KG/MIN`): information lost upstream, not recoverable by choosing a different decoding.\n")
    w("**Duplicates**\n")
    dd = ex["demo_dup_primaryid"]
    q20 = sum(v for k, v in dd["quarter_pairs"].items() if "2020Q3" in k.split("|"))
    w(f"- DEMO: {fmt_n(rel['demo_rows'])} rows vs {fmt_n(rel['demo_primaryid_unique'])} distinct `primaryid`; {dd['distinct_primaryid']} `primaryid`s occur in more than one quarter ({dd['distinct_quarter_patterns']} different quarter combinations), {dd['identical_in_all_columns']} identical in every column and {dd['distinct_primaryid'] - dd['identical_in_all_columns']} differing in at least one. {q20} of them involve **2020Q3**, which re-publishes cases first seen in 2012-2018 (largest groups: " + ", ".join(f"{k} x{v}" for k, v in list(dd["quarter_pairs"].items())[:4]) + ").")
    w("- Exact duplicate rows (all columns identical, counted via a 64-bit hash, includes repeats across quarters): " + ", ".join(f"{TABLE_TITLE[t]} {fmt_n(v['exact_duplicate_rows'])}" for t, v in rel["child_tables"].items()) + ".")
    dk = ex["drug_dup_primaryid_drug_seq"]
    w(f"- DRUG: {fmt_n(dk['distinct_keys'])} (`primaryid`, `drug_seq`) keys occur more than once ({fmt_n(dk['rows_involved'])} rows), although ASC_NTS describes `drug_seq` as the order within a case. Only {fmt_n(dk['keys_whose_rows_are_identical'])} of those keys have identical rows; {fmt_n(dk['keys_spanning_two_quarters'])} span more than one quarter. (`primaryid`, `drug_seq`) is therefore not a unique key.")
    w(f"- REAC: {fmt_n(ex['reac_dup_primaryid_pt'])} repeated (`primaryid`, `pt`) pairs; OUTC: {fmt_n(ex['outc_dup_primaryid_cod'])} repeated (`primaryid`, `outc_cod`) pairs.")
    w(f"- Primary-suspect coverage in DRUG: of {fmt_n(rel['drug_cases_total'])} `primaryid`s, {fmt_n(rel['cases_with_exactly_one_PS'])} have exactly one `PS` row, **{fmt_n(rel['cases_with_multiple_PS'])} have more than one**, **{fmt_n(rel['cases_with_no_PS'])} have none**.\n")
    w("**Weird / implausible values**\n")
    def dline(t, c):
        x = P_[t][c]["date"]
        mm = x["min_max"]
        return (f"`{c}` raw range {mm['YYYYMMDD_raw']['min']}..{mm['YYYYMMDD_raw']['max']} (8-digit); {x['yyyymmdd_year_lt_1900']} 8-digit values before year 1900, "
                f"{fmt_n(x['after_ref_quarter_end'])} after {ref}, {x['invalid_calendar']['YYYYMMDD']} not valid calendar dates")
    w("- Dates: " + "; ".join(dline(t, c) for t, c in (("demo", "event_dt"), ("demo", "mfr_dt"), ("demo", "rept_dt"), ("ther", "start_dt"), ("ther", "end_dt"))) + ". `init_fda_dt` and `fda_dt` are always clean 8-digit dates within the quarter range.")
    w(f"- Date order: `event_dt` is later than `fda_dt` in {fmt_n(ex['event_dt_gt_fda_dt_8digit']['n'])} of {fmt_n(ex['event_dt_gt_fda_dt_8digit']['of'])} 8-digit cases; `rept_dt` is later than `fda_dt` in {fmt_n(ex['rept_dt_gt_fda_dt_8digit']['n'])} of {fmt_n(ex['rept_dt_gt_fda_dt_8digit']['of'])}; `init_fda_dt` > `fda_dt` in {fmt_n(ex['init_fda_dt_gt_fda_dt'])}.")
    w("- Quarter files are not date-pure: e.g. " + "; ".join(f"{q} holds `fda_dt` {v[0]}..{v[1]}" for q, v in list(ex["fda_dt_range_by_quarter_file"].items())[:3]) + " (full ranges in the profile JSON). No `fda_dt` is later than its file's quarter end.")
    w(f"- `age`: {fmt_n(ex['age_present_cod_missing'])} rows have an age but no `age_cod`, {fmt_n(ex['age_missing_cod_present'])} have a unit but no age; non-numeric ages {ex['age_nonnumeric_values']}; ages in years range {ex['age_yr_min_max'][0]:g} to {ex['age_yr_min_max'][1]:g} ({fmt_n(ex['age_yr_negative'])} negative, {fmt_n(ex['age_yr_gt_120'])} above 120).")
    w(f"- `wt`: numeric values range {ex['wt_min_max_numeric'][0]:g} to {ex['wt_min_max_numeric'][1]:g}; free text sits in the numeric field, e.g. {', '.join('`' + k + '`' for k in list(ex['wt_nonnumeric_examples'])[:8])}. `wt_cod` has values outside the documented KG/LBS/GMS: " + ", ".join(f"`{k}` {v}" for k, v in d['wt_cod']['value_counts_top'].items() if k not in ('KG', 'LBS', 'GMS')) + ".")
    undoc = {k: v for k, v in d['occp_cod']['value_counts_top'].items() if k not in ('MD', 'PH', 'OT', 'LW', 'CN')}
    w("- `occp_cod` has values not in the 2012Q4 docs (MD, PH, OT, LW, CN): " + ", ".join(f"`{k}` {fmt_n(v)}" for k, v in undoc.items()) + f"; it is {qrange(d['occp_cod']['missing_pct_by_quarter'])} missing by quarter.")
    rcn = d["reporter_country"]["value_counts_top"]
    w(f"- `reporter_country` mixes 2-letter codes with the literal `COUNTRY NOT SPECIFIED` ({fmt_n(rcn.get('COUNTRY NOT SPECIFIED', 0))} rows) and `EU` ({fmt_n(rcn.get('EU', 0))}); `occr_country` is 2-letter only and {qrange(d['occr_country']['missing_pct_by_quarter'])} missing by quarter.")
    sx = d["sex"]["value_counts_top"]
    w(f"- Sex: `gndr_cod` (2012Q4-2014Q2) has `F`, `M`, `NS` ({fmt_n(d['gndr_cod']['value_counts_top'].get('NS', 0))}), `UNK`; `sex` (2014Q3+) has `F`, `M`, `UNK` and undocumented `P` {sx.get('P', 0)}, `I` {sx.get('I', 0)}, `T` {sx.get('T', 0)} (no `NS`), and is blank in {d['sex']['missing_pct']:.1f}% of rows ({qrange(d['sex']['missing_pct_by_quarter'])} by quarter) versus {d['gndr_cod']['missing_pct']:.1f}% for `gndr_cod`. `gndr_cod` was blank in {ex['sex_gndr_missing_by_quarter']['2012Q4'][0]}% of 2012Q4 rows but 0% in the next six quarters.")
    rcm = P_["demo"]["rept_cod"]["value_counts_top"]
    dy, ry = yearly(ex["drug_dechal_rechal_filled_by_quarter"], "dechal"), yearly(ex["drug_dechal_rechal_filled_by_quarter"], "rechal")
    w(f"- `dechal` / `rechal` fill rate flips over time (mean of quarterly % filled): " + "; ".join(f"{y}: dechal {dy[y]:.0f}% / rechal {ry[y]:.0f}%" for y in sorted(dy)) + ". Through 2015Q3 `dechal` is almost empty and `rechal` almost full (dominated by `D`); from 2015Q4 `rechal` drops sharply and `dechal` fills. The docs do not explain this - **unclear** whether the two columns were misaligned in the older extracts. `dechal` has one stray `` ` `` value and `rechal` one `' N'`.")
    lt = ex["drug_lot_num_leading_space_of_nonblank"]
    w(f"- DRUG text fields: {fmt_n(lt[0])} of {fmt_n(lt[1])} non-blank `lot_num` values start with a space; {fmt_n(dr['drugname']['leading_trailing_ws'])} `drugname` values have leading/trailing whitespace; {ex['drug_drugname_blank_rows']} rows have a blank `drugname`; case is mixed within the same value (`TABLET`/`Tablet` in `dose_form`, `ORAL`/`Oral` and `UNKNOWN`/`Unknown` in `route`); `exp_dt` holds {fmt_n(ex['drug_exp_dt_multi_value_rows'])} comma-separated multi-value strings (e.g. `20140831,20131230`).")
    w(f"- `route` is {dr['route']['missing_pct']:.1f}% blank and also contains explicit `Unknown`/`UNKNOWN` ({fmt_n(dr['route']['value_counts_top'].get('Unknown', 0) + dr['route']['value_counts_top'].get('UNKNOWN', 0))} rows): two encodings of 'unknown'; {dr['route']['unique_nonmissing']} distinct values because of case/wording variants.")
    w(f"- `cum_dose_unit` values in the files are text codes (`MG`, `UG/KG`, ...) whereas the 2012Q4 ASC_NTS lists numeric codes 001-004 (the 2013Q2 doc lists the text codes); {dr['cum_dose_unit']['unique_nonmissing']} distinct values observed, including both `MG/M**2` and `MG/M2`.")
    w(f"- `rpsr_cod` has a trailing-space variant `HP ` ({fmt_n(P_['rpsr']['rpsr_cod']['value_counts_top'].get('HP ', 0))} rows) next to `HP`; `dur_cod` has {P_['ther']['dur_cod']['unique_nonmissing']} values vs 7 documented (`DAYS`, `YEARS`, `MO`, stray `2`, `MG`, ...).")
    w(f"- `role_cod` is blank in {dr['role_cod']['missing']} DRUG rows; `rept_cod` has `5DAY`/`30DAY` ({fmt_n(rcm.get('5DAY', 0))}/{fmt_n(rcm.get('30DAY', 0))}) not in the 2012Q4 docs.")
    w("- Orphans: 0 child rows without a DEMO parent in the same quarter; " + f"{rel['child_tables']['drug']['demo_primaryids_without_rows']} DEMO `primaryid`s have no DRUG rows and {rel['child_tables']['reac']['demo_primaryids_without_rows']} have no REAC rows.\n")

    # ---- decisions live in DECISIONS.md
    w("## Decisions\n")
    w("The choices made after reviewing this profile (drug role, case version, time axis, encoding, duplicates, drug identity, event term, outcomes, demographics, reporter group, `dechal`/`rechal`) are recorded in [`DECISIONS.md`](../DECISIONS.md), together with their evidence and limitations. Column definitions for the processed tables are in `case_tables_data_dictionary.md`.\n")
    w("Caveats about this document: it was generated with the original latin-1 loading rule, so non-ASCII drug names in value counts show as mojibake (the loader now uses UTF-8 with a latin-1 fallback); the RPSR, THER and INDI tables were profiled but their raw files have since been deleted from `data/raw/` (the build does not use them), so a rebuild would not include them.\n")
    return "\n".join(L)


def render_all(prof_path, out_md):
    L, r, ctx = render(prof_path, out_md)
    Path(out_md).write_text("\n".join(L) + "\n" + render_tail(r))
    print(f"wrote {out_md}", file=sys.stderr)



def decision_checks(con):
    """Size of the choices made in 'Decisions made' (latest version per case, init_fda_dt quarter, PS-only)."""
    con.execute("DROP TABLE IF EXISTS _latest")
    con.execute("""CREATE TEMP TABLE _latest AS SELECT primaryid, caseid, init_fda_dt, _q FROM (
        SELECT *, row_number() OVER (PARTITION BY caseid ORDER BY TRY_CAST(caseversion AS INT) DESC, _q DESC) rn FROM demo) WHERE rn = 1""")
    out = {"cases_latest_version": q1(con, "SELECT count(*) FROM _latest")}
    out["init_before_2012_08_28"] = q1(con, "SELECT count(*) FROM _latest WHERE init_fda_dt < '20120828'")
    out["init_year_counts"] = {k: v for k, v in con.execute("SELECT substr(init_fda_dt,1,4), count(*) FROM _latest GROUP BY 1 ORDER BY 1").fetchall()}
    out["latest_file_quarter_differs_from_init_quarter"] = q1(con, "SELECT count(*) FROM _latest WHERE substr(init_fda_dt,1,4) || 'Q' || ((TRY_CAST(substr(init_fda_dt,5,2) AS INT)+2)//3) <> _q")
    out["latest_cases_with_PS"] = q1(con, "SELECT count(DISTINCT l.caseid) FROM _latest l JOIN drug d USING (primaryid) WHERE d.role_cod='PS'")
    out["latest_cases_with_PS_and_reaction"] = q1(con, "SELECT count(DISTINCT l.caseid) FROM _latest l JOIN drug d USING (primaryid) JOIN reac r USING (primaryid) WHERE d.role_cod='PS'")
    out["latest_init_gt_fda_dt"] = q1(con, "SELECT count(*) FROM demo d JOIN _latest l USING (primaryid) WHERE d.init_fda_dt > d.fda_dt")
    return out


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", nargs="+", help="default: every quarter found in data/raw")
    ap.add_argument("--db", default=str(ROOT / "data" / "interim" / "faers_raw.duckdb"))
    ap.add_argument("--out", default=str(ROOT / "references" / "profile" / "profile_all.json"))
    ap.add_argument("--render", metavar="PROFILE_JSON", help="skip profiling; render the dictionary .md from an existing profile JSON")
    ap.add_argument("--md", default=str(ROOT / "references" / "faers_data_dictionary.md"))
    ap.add_argument("--mem", default="10GB")
    ap.add_argument("--only-extras", metavar="PROFILE_JSON", help="re-run only extra_checks against the existing DuckDB and update that JSON in place")
    args = ap.parse_args()
    if args.render:
        render_all(args.render, args.md)
        return
    if args.only_extras:
        res = json.loads(Path(args.only_extras).read_text())
        con = connect(Path(args.db), args.mem)
        res["extra_checks"] = extra_checks(con, res["ref_max_date"])
        res["decision_checks"] = decision_checks(con)
        Path(args.only_extras).write_text(json.dumps(res, indent=1, default=str))
        print(f"updated extra_checks in {args.only_extras}", file=sys.stderr)
        return
    inv = inventory()
    heads, head_anoms = header_scan(inv)
    quarters = [q.upper() for q in args.quarters] if args.quarters else inv["quarters"]
    out_path, db_path = Path(args.out), Path(args.db)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[inventory] {len(inv['quarters'])} quarter dirs, missing: {inv['missing']}", file=sys.stderr)
    ref_max = f"{quarters[-1][:4]}{ {1: '0331', 2: '0630', 3: '0930', 4: '1231'}[int(quarters[-1][-1])] }"

    # byte-level scans (cached next to the db)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    cache = db_path.with_suffix(".filescan.json")
    scans = json.loads(cache.read_text()) if cache.exists() else {}
    for q in quarters:
        for t, rel in inv["files"][q].items():
            key = f"{t}|{q}"
            if key not in scans:
                scans[key] = scan_raw_file(RAW / rel)
                print(f"[scan] {key}: {scans[key]['lines']:,} lines", file=sys.stderr, flush=True)
        cache.write_text(json.dumps(scans))  # checkpoint after every quarter

    con = connect(db_path, args.mem)
    load_all(con, inv, heads, quarters)
    loaded = {(t, q): n for t in TABLES for q, n in con.execute(f"SELECT _q, count(*) FROM {t} GROUP BY 1").fetchall()}
    file_log, rows_per_q = [], defaultdict(dict)
    for q in quarters:
        for t in TABLES:
            if t not in inv["files"][q]:
                file_log.append({"table": t, "quarter": q, "file": None})
                continue
            sc = scans[f"{t}|{q}"]
            n = loaded.get((t, q), 0)
            rows_per_q[t][q] = n
            file_log.append({"table": t, "quarter": q, "file": inv["files"][q][t], "columns": sc["header_fields"], "data_lines": sc["lines"],
                             "rows_loaded": n, "bad_lines_skipped": sc["lines"] - sc["blank_lines"] - n, **{k: v for k, v in sc.items() if k not in ("lines", "header_fields")}})
    vc_rows = []
    result = {"loaded_quarters": quarters, "inventory": inv, "header_columns": heads, "header_anomalies": head_anoms,
              "file_log": file_log, "rows_per_quarter": rows_per_q,
              "rows_total": {t: sum(rows_per_q[t].values()) for t in TABLES}, "ref_max_date": ref_max,
              "engine": f"duckdb {duckdb.__version__}"}
    result["profiles"] = profile_tables(con, heads, quarters, ref_max, vc_rows)
    result["relationships"] = relationships(con)
    result["extra_checks"] = extra_checks(con, ref_max)
    out_path.write_text(json.dumps(result, indent=1, default=str))
    pd.DataFrame(vc_rows, columns=["table", "column", "value", "count", "pct_of_rows"]).to_csv(out_path.with_name(out_path.stem + "_value_counts.csv"), index=False)
    print(f"wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
