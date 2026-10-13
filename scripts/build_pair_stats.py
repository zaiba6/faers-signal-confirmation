#!/usr/bin/env python3
"""Steps 5-7: cumulative 2x2 counts, ROR / PRR / chi2 / FDR, detection dates under three thresholds.

Input : data/processed/ps_drug_event_cases.parquet (counting universe = init_quarter >= 2014Q3, flag in_counting_universe = 1)
Output: data/processed/pair_quarter_counts.parquet   drug_ai, pt_clean, quarter, a, b, c, d, is_combo
        data/processed/pair_quarter_stats.parquet    counts + ROR/CI, PRR, chi2, p_value, q_value, cell_corrected
        data/processed/detected_pairs_{T1,T2,T3}.parquet
        references/sanity_check_3.md                 checks + numbers + samples
Nothing else is touched (no labels, no modeling, DuckDB raw copy untouched).
Run: python scripts/build_pair_stats.py
"""
import json
import math
import shutil
import sys
import time
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
PROC = ROOT / "data" / "processed"
TMP = ROOT / "data" / "interim" / "pair_parts"
LINK = PROC / "ps_drug_event_cases.parquet"
CASES = PROC / "case_table.parquet"
FIRST_Q = "2014Q3"
N_Q = 48                      # 2014Q3 .. 2026Q2
MIN_A = 3                     # pairs enter the table once cumulative a >= 3
WINDOW_START, WINDOW_END = "2016Q1", "2023Q2"   # in_window detection range
Z95 = 1.959963984540054
try:
    from scipy.special import ndtr
    SF = lambda z: ndtr(-z)           # P(Z > z)
except ImportError:                   # fall back to math.erfc
    _erfc = np.vectorize(math.erfc)
    SF = lambda z: 0.5 * _erfc(z / math.sqrt(2))


def qlabel(qi):
    return f"{2014 + (qi + 2) // 4}Q{(qi + 2) % 4 + 1}"


def qindex_sql(col):
    return f"((CAST(substr({col}, 1, 4) AS INT) - 2014) * 4 + CAST(substr({col}, 6, 1) AS INT) - 3)"


QLAB_SQL = "(CAST(2014 + (qi + 2) // 4 AS VARCHAR) || 'Q' || CAST((qi + 2) % 4 + 1 AS VARCHAR))"
THRESHOLDS = {
    "T1": ("lenient: a >= 3, ror_lcl > 1", "a >= 3 AND ror_lcl > 1"),
    "T2": ("Evans: a >= 3, PRR >= 2, chi2 >= 4", "a >= 3 AND prr >= 2 AND chi2 >= 4"),
    "T3": ("strict: a >= 5, ror_lcl > 1, q_value < 0.05", "a >= 5 AND ror_lcl > 1 AND q_value < 0.05"),
}


def gb_free():
    return round(shutil.disk_usage(ROOT).free / 1e9, 2)


def md_table(df, floats=4):
    def f(v):
        if isinstance(v, (float, np.floating)):
            return "" if v != v else (f"{v:.{floats}g}" if abs(v) < 1e5 else f"{v:,.0f}")
        if isinstance(v, (int, np.integer)):
            return f"{v:,}"
        return "" if v is None else str(v)
    head = "| " + " | ".join(map(str, df.columns)) + " |\n|" + "---|" * len(df.columns)
    return "\n".join([head] + ["| " + " | ".join(f(v) for v in row) + " |" for row in df.itertuples(index=False)])


# ----------------------------------------------------------------------------- step 5
def build_counts(con, rep):
    t0 = time.time()
    TMP.mkdir(parents=True, exist_ok=True)
    for f in TMP.glob("*.parquet"):
        f.unlink()
    all_rows = con.execute(f"SELECT count(*), count(DISTINCT caseid) FROM read_parquet('{LINK}')").fetchone()
    con.execute(f"CREATE TEMP TABLE link_u AS SELECT caseid, drug_ai, pt_clean, is_combo, init_quarter FROM read_parquet('{LINK}') WHERE init_quarter >= '{FIRST_Q}'")
    uni = con.execute("SELECT count(*), count(DISTINCT caseid), count(DISTINCT drug_ai), count(DISTINCT pt_clean) FROM link_u").fetchone()
    rep["universe"] = {"link_rows_all": all_rows[0], "link_cases_all": all_rows[1], "link_rows_universe": uni[0], "cases_universe": uni[1],
                       "drugs_universe": uni[2], "pts_universe": uni[3], "rows_removed": all_rows[0] - uni[0], "cases_removed": all_rows[1] - uni[1]}
    con.execute("CREATE TEMP TABLE dim_drug AS SELECT row_number() OVER (ORDER BY drug_ai) - 1 AS did, drug_ai, max(is_combo) AS is_combo FROM link_u GROUP BY drug_ai")
    con.execute("CREATE TEMP TABLE dim_pt AS SELECT row_number() OVER (ORDER BY pt_clean) - 1 AS pid, pt_clean FROM (SELECT DISTINCT pt_clean FROM link_u)")
    rep["drugs_with_inconsistent_is_combo"] = con.execute("SELECT count(*) FROM (SELECT drug_ai FROM link_u GROUP BY 1 HAVING count(DISTINCT is_combo) > 1)").fetchone()[0]
    con.execute(f"""CREATE TEMP TABLE u AS SELECT CAST(l.caseid AS BIGINT) AS caseid, d.did, p.pid, CAST({qindex_sql('l.init_quarter')} AS UTINYINT) AS qi
                    FROM link_u l JOIN dim_drug d USING (drug_ai) JOIN dim_pt p USING (pt_clean)""")
    con.execute("DROP TABLE link_u")
    con.execute("CREATE TEMP TABLE pair_inc AS SELECT did, pid, qi, count(*)::INTEGER AS n FROM u GROUP BY 1, 2, 3")
    con.execute("CREATE TEMP TABLE drug_inc AS SELECT did, qi, count(DISTINCT caseid)::INTEGER AS n FROM u GROUP BY 1, 2")
    con.execute("CREATE TEMP TABLE event_inc AS SELECT pid, qi, count(DISTINCT caseid)::INTEGER AS n FROM u GROUP BY 1, 2")
    con.execute("CREATE TEMP TABLE n_inc AS SELECT qi, count(DISTINCT caseid)::INTEGER AS n FROM u GROUP BY 1")
    rep["pair_inc_rows"] = con.execute("SELECT count(*) FROM pair_inc").fetchone()[0]
    rep["case_pair_rows_in_u_duplicates"] = con.execute("SELECT count(*) - count(DISTINCT (caseid, did, pid)) FROM u").fetchone()[0]
    con.execute("DROP TABLE u")
    print(f"[counts] prepared in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    for qi in range(N_Q):
        con.execute(f"""COPY (
            WITH cur AS (SELECT did, pid, sum(n)::INTEGER AS a FROM pair_inc WHERE qi <= {qi} GROUP BY did, pid HAVING sum(n) >= {MIN_A}),
                 dc AS (SELECT did, sum(n)::INTEGER AS nd FROM drug_inc WHERE qi <= {qi} GROUP BY did),
                 ec AS (SELECT pid, sum(n)::INTEGER AS ne FROM event_inc WHERE qi <= {qi} GROUP BY pid),
                 nt AS (SELECT sum(n)::INTEGER AS n FROM n_inc WHERE qi <= {qi})
            SELECT cur.did, cur.pid, {qi}::UTINYINT AS qi, cur.a, (dc.nd - cur.a) AS b, (ec.ne - cur.a) AS c,
                   (nt.n - dc.nd - ec.ne + cur.a) AS d, nt.n AS n_total
            FROM cur JOIN dc USING (did) JOIN ec USING (pid), nt) TO '{TMP}/counts_{qi:02d}.parquet' (FORMAT parquet, COMPRESSION zstd)""")
    print(f"[counts] 48 quarters done in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    return t0


def write_counts_final(con, rep):
    out = PROC / "pair_quarter_counts.parquet"
    con.execute(f"""COPY (SELECT d.drug_ai, p.pt_clean, {QLAB_SQL} AS quarter, c.a, c.b, c.c, c.d, d.is_combo::TINYINT AS is_combo
                         FROM read_parquet('{TMP}/counts_*.parquet') c JOIN dim_drug d USING (did) JOIN dim_pt p USING (pid)
                         ORDER BY c.qi, d.drug_ai, p.pt_clean) TO '{out}' (FORMAT parquet, COMPRESSION zstd)""")
    rep["counts_rows"] = con.execute(f"SELECT count(*) FROM '{out}'").fetchone()[0]
    rep["counts_bytes"] = out.stat().st_size
    rep["counts_rows_per_quarter"] = {r[0]: r[1] for r in con.execute(f"SELECT quarter, count(*) FROM '{out}' GROUP BY 1 ORDER BY 1").fetchall()}
    return out


# ----------------------------------------------------------------------------- step 6
def stats_for_quarter(df):
    """df has columns a, b, c, d (ints). Returns the stats columns. Haldane-Anscombe +0.5 on all four cells for rows with any zero cell."""
    a, b, c, d = (df[x].to_numpy(dtype="float64") for x in "abcd")
    corr = (a == 0) | (b == 0) | (c == 0) | (d == 0)
    k = np.where(corr, 0.5, 0.0)
    a2, b2, c2, d2 = a + k, b + k, c + k, d + k
    n2 = a2 + b2 + c2 + d2
    ror = (a2 * d2) / (b2 * c2)
    se = np.sqrt(1 / a2 + 1 / b2 + 1 / c2 + 1 / d2)
    lnr = np.log(ror)
    prr = (a2 / (a2 + b2)) / (c2 / (c2 + d2))
    chi2 = n2 * (a2 * d2 - b2 * c2) ** 2 / ((a2 + b2) * (c2 + d2) * (a2 + c2) * (b2 + d2))
    p = SF(lnr / se)                                    # one-sided Wald test, H1: ROR > 1
    # Benjamini-Hochberg across all tested pairs in this quarter (ties handled with the largest rank of the tie group)
    m = len(p)
    order = np.argsort(p, kind="stable")
    ps = p[order]
    rank = np.searchsorted(ps, ps, side="right")        # number of p-values <= this one
    adj = np.minimum.accumulate((ps * m / rank)[::-1])[::-1]
    q = np.empty(m)
    q[order] = np.minimum(adj, 1.0)
    return pd.DataFrame({"ror": ror, "ror_lcl": np.exp(lnr - Z95 * se), "ror_ucl": np.exp(lnr + Z95 * se), "prr": prr, "chi2": chi2,
                         "p_value": p, "q_value": q, "cell_corrected": corr.astype("int8")})


def build_stats(con, rep):
    t0 = time.time()
    parts = []
    ncorr = 0
    for qi in range(N_Q):
        df = con.execute(f"SELECT * FROM '{TMP}/counts_{qi:02d}.parquet'").df()
        st = stats_for_quarter(df)
        out = pd.concat([df[["did", "pid", "qi", "a", "b", "c", "d"]].reset_index(drop=True), st], axis=1)
        f = TMP / f"stats_{qi:02d}.parquet"
        con.register("stats_df", out)
        con.execute(f"COPY (SELECT * FROM stats_df) TO '{f}' (FORMAT parquet, COMPRESSION zstd)")
        con.unregister("stats_df")
        parts.append(f)
        ncorr += int(st["cell_corrected"].sum())
    rep["stats_cell_corrected_rows"] = ncorr
    out = PROC / "pair_quarter_stats.parquet"
    con.execute(f"""COPY (SELECT d.drug_ai, p.pt_clean, {QLAB_SQL} AS quarter, d.is_combo::TINYINT AS is_combo, s.a, s.b, s.c, s.d,
                            s.ror, s.ror_lcl, s.ror_ucl, s.prr, s.chi2, s.p_value, s.q_value, s.cell_corrected
                         FROM read_parquet('{TMP}/stats_*.parquet') s JOIN dim_drug d USING (did) JOIN dim_pt p USING (pid)
                         ORDER BY s.qi, d.drug_ai, p.pt_clean) TO '{out}' (FORMAT parquet, COMPRESSION zstd)""")
    rep["stats_rows"] = con.execute(f"SELECT count(*) FROM '{out}'").fetchone()[0]
    rep["stats_bytes"] = out.stat().st_size
    print(f"[stats] done in {time.time() - t0:.0f}s", file=sys.stderr, flush=True)
    return out


# ----------------------------------------------------------------------------- step 7
def build_detection(con, rep, stats):
    rep["detection"] = {}
    con.execute(f"CREATE TEMP TABLE st AS SELECT * FROM read_parquet('{stats}')")
    con.execute("CREATE TEMP TABLE all_pairs AS SELECT drug_ai, pt_clean, max(is_combo) AS is_combo FROM st GROUP BY 1, 2")
    rep["pairs_ever_a_ge_3"] = con.execute("SELECT count(*) FROM all_pairs").fetchone()[0]
    for name, (desc, rule) in THRESHOLDS.items():
        out = PROC / f"detected_pairs_{name}.parquet"
        con.execute(f"""COPY (
            WITH first_hit AS (SELECT * EXCLUDE (rn) FROM (SELECT *, row_number() OVER (PARTITION BY drug_ai, pt_clean ORDER BY quarter) rn FROM st WHERE {rule}) WHERE rn = 1)
            SELECT p.drug_ai, p.pt_clean, p.is_combo::TINYINT AS is_combo, h.quarter AS detection_quarter,
                   CASE WHEN h.quarter IS NULL THEN 'never' WHEN h.quarter < '{WINDOW_START}' THEN 'pre_window'
                        WHEN h.quarter <= '{WINDOW_END}' THEN 'in_window' ELSE 'too_recent' END AS status,
                   h.a, h.b, h.c, h.d, h.ror, h.ror_lcl, h.prr, h.chi2, h.q_value
            FROM all_pairs p LEFT JOIN first_hit h USING (drug_ai, pt_clean) ORDER BY p.drug_ai, p.pt_clean) TO '{out}' (FORMAT parquet, COMPRESSION zstd)""")
        D = f"'{out}'"
        rep["detection"][name] = {
            "rule": desc, "rows": con.execute(f"SELECT count(*) FROM {D}").fetchone()[0], "bytes": out.stat().st_size,
            "status": {r[0]: r[1] for r in con.execute(f"SELECT status, count(*) FROM {D} GROUP BY 1 ORDER BY 2 DESC").fetchall()},
            "status_combo": {f"{r[0]}|combo={r[1]}": r[2] for r in con.execute(f"SELECT status, is_combo, count(*) FROM {D} GROUP BY 1, 2 ORDER BY 1, 2").fetchall()},
            "per_year": {r[0]: r[1] for r in con.execute(f"SELECT substr(detection_quarter, 1, 4), count(*) FROM {D} WHERE status = 'in_window' GROUP BY 1 ORDER BY 1").fetchall()},
            "pre_window_by_quarter": {r[0]: r[1] for r in con.execute(f"SELECT detection_quarter, count(*) FROM {D} WHERE status = 'pre_window' GROUP BY 1 ORDER BY 1").fetchall()},
        }
    # overlap between thresholds among in_window pairs
    con.execute(f"""CREATE TEMP TABLE ov AS SELECT a.drug_ai, a.pt_clean, a.status s1, b.status s2, c.status s3
        FROM '{PROC}/detected_pairs_T1.parquet' a JOIN '{PROC}/detected_pairs_T2.parquet' b USING (drug_ai, pt_clean) JOIN '{PROC}/detected_pairs_T3.parquet' c USING (drug_ai, pt_clean)""")
    rep["detection_overlap_in_window"] = {
        "T1 only": con.execute("SELECT count(*) FROM ov WHERE s1='in_window' AND s2<>'in_window' AND s3<>'in_window'").fetchone()[0],
        "T2 only": con.execute("SELECT count(*) FROM ov WHERE s2='in_window' AND s1<>'in_window' AND s3<>'in_window'").fetchone()[0],
        "T3 only": con.execute("SELECT count(*) FROM ov WHERE s3='in_window' AND s1<>'in_window' AND s2<>'in_window'").fetchone()[0],
        "T1&T2&T3": con.execute("SELECT count(*) FROM ov WHERE s1='in_window' AND s2='in_window' AND s3='in_window'").fetchone()[0],
        "T3 subset of T1": con.execute("SELECT count(*) FROM ov WHERE s3<>'never' AND s1='never'").fetchone()[0],
    }


# ----------------------------------------------------------------------------- checks
def checks(con, rep):
    C, S = PROC / "pair_quarter_counts.parquet", PROC / "pair_quarter_stats.parquet"
    chk = {}
    T = "2020Q2"
    # independent reference: straight from the link table, strings and DISTINCT, no ids / increments
    def ref(drug, pt, t):
        w = f"init_quarter >= '{FIRST_Q}' AND init_quarter <= '{t}'"
        r = con.execute(f"""SELECT (SELECT count(DISTINCT caseid) FROM read_parquet('{LINK}') WHERE {w} AND drug_ai = ? AND pt_clean = ?),
                                   (SELECT count(DISTINCT caseid) FROM read_parquet('{LINK}') WHERE {w} AND drug_ai = ?),
                                   (SELECT count(DISTINCT caseid) FROM read_parquet('{LINK}') WHERE {w} AND pt_clean = ?),
                                   (SELECT count(DISTINCT caseid) FROM read_parquet('{LINK}') WHERE {w})""", [drug, pt, drug, pt]).fetchone()
        a, nd, ne, n = r
        return {"a": a, "b": nd - a, "c": ne - a, "d": n - nd - ne + a}
    common = con.execute(f"SELECT drug_ai, pt_clean FROM '{C}' WHERE quarter = '{T}' ORDER BY a DESC, drug_ai LIMIT 1").fetchone()
    rare = con.execute(f"SELECT drug_ai, pt_clean FROM '{C}' WHERE quarter = '{T}' AND a = 3 ORDER BY hash(drug_ai, pt_clean) LIMIT 1").fetchone()
    combo = con.execute(f"SELECT drug_ai, pt_clean FROM '{C}' WHERE quarter = '{T}' AND is_combo = 1 AND a >= 40 ORDER BY a DESC, drug_ai LIMIT 1").fetchone()
    rows = []
    for label, (drug, pt) in (("common", common), ("rare", rare), ("combination", combo)):
        mine = con.execute(f"SELECT a, b, c, d FROM '{C}' WHERE quarter = '{T}' AND drug_ai = ? AND pt_clean = ?", [drug, pt]).fetchone()
        theirs = ref(drug, pt, T)
        rows.append({"kind": label, "drug_ai": drug, "pt_clean": pt, "quarter": T, "table (a,b,c,d)": str(tuple(mine)),
                     "independent (a,b,c,d)": str(tuple(theirs.values())), "match": "YES" if tuple(mine) == tuple(theirs.values()) else "NO"})
    chk["check1"] = pd.DataFrame(rows)
    # check 2: a+b+c+d = N, with N from an independent per-quarter distinct count of cases
    nq = con.execute(f"""SELECT init_quarter AS quarter, sum(count(DISTINCT caseid)) OVER (ORDER BY init_quarter) AS n FROM read_parquet('{LINK}')
                         WHERE init_quarter >= '{FIRST_Q}' GROUP BY init_quarter""").df()
    con.register("nq", nq)
    chk["check2"] = con.execute(f"""SELECT count(*) AS rows_checked, count(*) FILTER (WHERE c.a + c.b + c.c + c.d <> nq.n) AS rows_where_sum_ne_N,
                                           count(*) FILTER (WHERE c.a < 0 OR c.b < 0 OR c.c < 0 OR c.d < 0) AS rows_with_negative_cell
                                    FROM '{C}' c JOIN nq USING (quarter)""").df()
    chk["N_by_quarter"] = nq
    # check 3: monotone cumulative counts and no gaps in quarters
    chk["check3"] = con.execute(f"""
        WITH x AS (SELECT drug_ai, pt_clean, quarter, a, a + b AS n_drug, a + c AS n_event, a + b + c + d AS n,
                          lag(a) OVER w pa, lag(a + b) OVER w pd, lag(a + c) OVER w pe, lag(a + b + c + d) OVER w pn, lag(quarter) OVER w pq
                   FROM '{C}' WINDOW w AS (PARTITION BY drug_ai, pt_clean ORDER BY quarter))
        SELECT count(*) AS rows_with_a_previous_quarter,
               count(*) FILTER (WHERE a < pa) AS a_decreased, count(*) FILTER (WHERE n_drug < pd) AS n_drug_decreased,
               count(*) FILTER (WHERE n_event < pe) AS n_event_decreased, count(*) FILTER (WHERE n < pn) AS N_decreased,
               count(*) FILTER (WHERE pq IS NOT NULL AND pq >= quarter) AS order_problems FROM x WHERE pa IS NOT NULL""").df()
    chk["check3_gaps"] = con.execute(f"""
        WITH x AS (SELECT quarter, lag(quarter) OVER (PARTITION BY drug_ai, pt_clean ORDER BY quarter) pq FROM '{C}'),
             idx AS (SELECT quarter, CAST(substr(quarter,1,4) AS INT) * 4 + CAST(substr(quarter,6,1) AS INT) i FROM (SELECT DISTINCT quarter FROM '{C}'))
        SELECT count(*) AS consecutive_rows, count(*) FILTER (WHERE a.i - b.i <> 1) AS gaps
        FROM x JOIN idx a ON a.quarter = x.quarter JOIN idx b ON b.quarter = x.pq""").df()
    # extra: stats sanity
    chk["stats_sanity"] = con.execute(f"""SELECT count(*) AS rows, count(*) FILTER (WHERE ror IS NULL OR ror <> ror OR prr <> prr OR chi2 <> chi2 OR p_value <> p_value OR q_value <> q_value) AS nan_rows,
                                                 count(*) FILTER (WHERE q_value < p_value - 1e-12) AS q_below_p, count(*) FILTER (WHERE q_value > 1) AS q_above_1,
                                                 count(*) FILTER (WHERE cell_corrected = 1) AS cell_corrected_rows,
                                                 count(*) FILTER (WHERE cell_corrected = 1 AND b = 0) AS b_zero, count(*) FILTER (WHERE cell_corrected = 1 AND c = 0) AS c_zero,
                                                 count(*) FILTER (WHERE cell_corrected = 1 AND d = 0) AS d_zero FROM '{S}'""").df()
    # recompute one stats row independently in plain python
    r = con.execute(f"SELECT * FROM '{S}' WHERE quarter = '{T}' AND drug_ai = ? AND pt_clean = ?", [common[0], common[1]]).df().iloc[0]
    a, b, c, d = (float(r[x]) for x in "abcd")
    ror = a * d / (b * c); se = math.sqrt(1 / a + 1 / b + 1 / c + 1 / d)
    prr = (a / (a + b)) / (c / (c + d)); n = a + b + c + d
    chi2 = n * (a * d - b * c) ** 2 / ((a + b) * (c + d) * (a + c) * (b + d))
    chk["check_stats_row"] = pd.DataFrame([{"pair": f"{common[0]} / {common[1]} @ {T}", "ROR table": r["ror"], "ROR python": ror,
                                            "ror_lcl table": r["ror_lcl"], "ror_lcl python": math.exp(math.log(ror) - Z95 * se),
                                            "PRR table": r["prr"], "PRR python": prr, "chi2 table": r["chi2"], "chi2 python": chi2}])
    return chk


def sample(con, path, n=10):
    return con.execute(f"SELECT * FROM '{path}' ORDER BY hash(drug_ai, pt_clean, 7) LIMIT {n}").df()


def write_md(con, rep, chk):
    L = []
    w = L.append
    w("# Sanity check 3: cumulative counts, ROR/PRR/FDR, detection dates\n")
    w("Generated by `scripts/build_pair_stats.py` (steps 5-7). Counting universe = distinct cases in `ps_drug_event_cases` with `init_quarter >= 2014Q3`; quarters 2014Q3..2026Q2; counts for quarter t use only cases with `init_quarter <= t`; one count per distinct `caseid` per (drug_ai, pt_clean).\n")
    u = rep["universe"]
    w("## Setup\n")
    w(f"- Link table before the cutoff: {u['link_rows_all']:,} rows, {u['link_cases_all']:,} cases. Universe: {u['link_rows_universe']:,} rows, {u['cases_universe']:,} cases, {u['drugs_universe']:,} drugs, {u['pts_universe']:,} event terms.")
    w(f"- **Removed by the cutoff: {u['rows_removed']:,} rows and {u['cases_removed']:,} cases** (first received before 2014Q3; their latest versions sit in 2014Q3+ files, so the pre-2014Q3 part of the data is a biased subset).")
    w(f"- Rows (case, drug, event) duplicated inside the universe: {rep['case_pair_rows_in_u_duplicates']} (a case counts once per pair). Drugs whose `is_combo` is inconsistent across rows: {rep['drugs_with_inconsistent_is_combo']}.")
    w(f"- Disk free: before {rep['disk_free_before_gb']} GB, after {rep['disk_free_after_gb']} GB.\n")
    w("## Output files\n")
    w("| File | Rows | Size |\n|---|---|---|")
    w(f"| pair_quarter_counts.parquet | {rep['counts_rows']:,} | {rep['counts_bytes'] / 1e6:,.0f} MB |")
    w(f"| pair_quarter_stats.parquet | {rep['stats_rows']:,} | {rep['stats_bytes'] / 1e6:,.0f} MB |")
    for k, v in rep["detection"].items():
        w(f"| detected_pairs_{k}.parquet | {v['rows']:,} | {v['bytes'] / 1e6:,.0f} MB |")
    w(f"\nPairs that ever reach cumulative a >= 3: {rep['pairs_ever_a_ge_3']:,}. Rows per quarter in the counts file: first {list(rep['counts_rows_per_quarter'].items())[0]}, last {list(rep['counts_rows_per_quarter'].items())[-1]}.\n")
    w("## Zero-cell rule (Step 6)\n")
    w(f"Haldane-Anscombe: for any row with a zero in b, c or d, 0.5 is added to **all four** cells before computing ROR, its CI, PRR and chi2 (those rows only; flag `cell_corrected` = 1). a >= 3 so a is never zero. Rows corrected: {rep['stats_cell_corrected_rows']:,} of {rep['stats_rows']:,}. p-value: one-sided Wald z on log ROR (H1: ROR > 1). q_value: Benjamini-Hochberg within each quarter across every pair in that quarter's table (ties get the largest rank of the tie group; monotone adjustment; capped at 1). chi2 = Pearson, no continuity correction.\n")
    w("## Check 1: three pairs recomputed independently (from `ps_drug_event_cases`, distinct caseid, string filters)\n")
    w(md_table(chk["check1"]) + "\n")
    w("## Check 2: a + b + c + d = N (N from an independent per-quarter distinct-case count)\n")
    w(md_table(chk["check2"]) + "\n")
    w(f"N by quarter (first 3 / last 3): " + ", ".join(f"{r.quarter} {int(r.n):,}" for r in pd.concat([chk['N_by_quarter'].head(3), chk['N_by_quarter'].tail(3)]).itertuples()) + "\n")
    w("## Check 3: cumulative counts never decrease\n")
    w(md_table(chk["check3"]) + "\n")
    w(md_table(chk["check3_gaps"]) + "\n")
    w("## Stats sanity\n")
    w(md_table(chk["stats_sanity"]) + "\n")
    w("One row recomputed in plain Python from its a, b, c, d:\n")
    w(md_table(chk["check_stats_row"], floats=8) + "\n")
    w("## Check 4: detection under the three thresholds\n")
    for k, v in rep["detection"].items():
        w(f"### {k} ({v['rule']})\n")
        w("Status: " + ", ".join(f"{s} {n:,}" for s, n in v["status"].items()) + f" (total {v['rows']:,}).\n")
        w("Detected per year (`in_window`, 2016Q1-2023Q2): " + ", ".join(f"{y}: {n:,}" for y, n in v["per_year"].items()) + "\n")
        pw = v["pre_window_by_quarter"]
        w("Pre-window detections by quarter (burn-in, excluded): " + (", ".join(f"{q}: {n:,}" for q, n in pw.items()) or "none") + "\n")
        w(f"Top 20 `in_window` pairs by `ror_lcl` at detection:\n")
        w(md_table(chk[f"top20_{k}"]) + "\n")
    ov = rep["detection_overlap_in_window"]
    w("### Overlap of `in_window` pairs across thresholds\n")
    w(", ".join(f"{k}: {v:,}" for k, v in ov.items()) + "\n")
    w("## Samples (10 rows per output file)\n")
    for name, df in chk["samples"].items():
        w(f"### {name}\n")
        w(md_table(df) + "\n")
    (ROOT / "references" / "sanity_check_3.md").write_text("\n".join(L))


def main():
    rep = {"disk_free_before_gb": gb_free()}
    if rep["disk_free_before_gb"] < 8:
        sys.exit(f"only {rep['disk_free_before_gb']} GB free - stopping")
    con = duckdb.connect()
    con.execute("SET memory_limit='9GB'")
    con.execute("SET threads=4")
    con.execute(f"SET temp_directory='{ROOT / 'data' / 'interim' / 'duckdb_tmp'}'")
    con.execute("SET preserve_insertion_order=false")
    build_counts(con, rep)
    write_counts_final(con, rep)
    st = build_stats(con, rep)
    build_detection(con, rep, st)
    chk = checks(con, rep)
    for k in rep["detection"]:
        chk[f"top20_{k}"] = con.execute(f"""SELECT drug_ai, pt_clean, is_combo, detection_quarter, a, b, c, d, ror, ror_lcl, prr, chi2, q_value
                                            FROM '{PROC}/detected_pairs_{k}.parquet' WHERE status = 'in_window' ORDER BY ror_lcl DESC LIMIT 20""").df()
    chk["samples"] = {f: sample(con, PROC / f"{f}.parquet") for f in ["pair_quarter_counts", "pair_quarter_stats", "detected_pairs_T1", "detected_pairs_T2", "detected_pairs_T3"]}
    for f in TMP.glob("*.parquet"):
        f.unlink()
    try:
        TMP.rmdir()
    except OSError:
        pass
    rep["disk_free_after_gb"] = gb_free()
    write_md(con, rep, chk)
    (ROOT / "references" / "profile" / "pair_stats_report.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps({k: v for k, v in rep.items() if k not in ("counts_rows_per_quarter",)}, indent=1, default=str))


if __name__ == "__main__":
    main()
