"""Compute the single-source clustering statistics of decision 85, one row per detected pair.

Run: /usr/local/bin/python3 scripts/build_burst_stats.py   (BEFORE build_confirmation_target.py)
In : data/processed/detected_pairs_T{1,2,3}.parquet, ps_drug_event_cases.parquet
Out: data/processed/burst_stats_T{1,2,3}.parquet
       drug_ai, pt_clean, detection_quarter, burst_n, cases_at_detection, burst_share,
       source_clustered

Leakage: only cases with init_quarter <= detection_quarter are counted, the same freeze
rule the feature table uses, so a pair is judged on what was knowable at detection.

Reads detected_pairs, NOT confirmation_target: the target build consumes this file to
apply the exclusion of decision 87, so taking detection quarters from the target would
be circular.

This writes the statistic and the flag. The exclusion itself is applied in
build_confirmation_target.py, which keeps every drop in one place.
"""
import sys
from pathlib import Path
import duckdb

sys.path.insert(0, str(Path(__file__).parent))
from source_clustering import BURST_SHARE, BURST_MIN_N

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "data/processed"

SQL = """
copy (
  with t as (select drug_ai, pt_clean, detection_quarter
             from read_parquet('{tgt}')
             where status = 'in_window'),
       c as (select p.drug_ai, p.pt_clean, p.init_quarter, count(*) as n
             from read_parquet('{cases}') p
             join t on t.drug_ai = p.drug_ai and t.pt_clean = p.pt_clean
             where p.in_counting_universe and p.init_quarter <= t.detection_quarter
             group by 1, 2, 3),
       m as (select drug_ai, pt_clean, max(n) as burst_n, sum(n) as cases_at_detection
             from c group by 1, 2)
  select t.drug_ai, t.pt_clean, t.detection_quarter,
         m.burst_n, m.cases_at_detection,
         m.burst_n::double / nullif(m.cases_at_detection, 0) as burst_share,
         (m.burst_n::double / nullif(m.cases_at_detection, 0) >= {share}
          and m.burst_n >= {minn})::int as source_clustered
  from t left join m on m.drug_ai = t.drug_ai and m.pt_clean = t.pt_clean
) to '{out}' (format parquet)
"""

if __name__ == "__main__":
    con = duckdb.connect()
    for tag in ("T1", "T2", "T3"):
        out = P / f"burst_stats_{tag}.parquet"
        con.execute(SQL.format(tgt=P / f"detected_pairs_{tag}.parquet",
                               cases=P / "ps_drug_event_cases.parquet",
                               share=BURST_SHARE, minn=BURST_MIN_N, out=out))
        r = con.execute(f"""select count(*) n, sum(source_clustered) f,
                            round(100.0*sum(source_clustered)/count(*),2) pct
                            from read_parquet('{out}')""").fetchone()
        print(f"{tag}: {r[0]:,} pairs | flagged {r[1]:,} ({r[2]}%)", flush=True)
