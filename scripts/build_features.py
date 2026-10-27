"""Features for the confirmation classifier, every one frozen at the pair's detection quarter.

Run: /usr/local/bin/python3 scripts/build_features.py [T1|T2|T3]   (default T1)
Out: data/processed/features_<rule>.parquet  + references/features_data_dictionary.md

THE FREEZE RULE: a feature may use a case only if its init_quarter <= the pair's detection
quarter. Nothing about the future - not the label, not later reports, not the drug's later
history - may enter. Every query below carries that cut explicitly; scripts/audit_leakage.py
re-derives a sample of these values from raw cases and fails if any disagree.
"""
import sys
from pathlib import Path
import duckdb

ROOT = Path(__file__).resolve().parents[1]
P = ROOT / "data/processed"
RULE = (sys.argv[1] if len(sys.argv) > 1 else "T1").upper()
OUT = P / f"features_{RULE}.parquet"

SQL = f"""
with tgt as (
    select drug_ai, pt_clean, detection_quarter, a, b, c, d, ror, ror_lcl, prr, chi2, q_value,
           confirmed_event_3y, confirmed_drug_3y, confirmed_event_strict
    from read_parquet('{P}/confirmation_target_{RULE}.parquet')
),
-- the pair's own count history, never past its detection quarter
hist as (
    select t.drug_ai, t.pt_clean, t.detection_quarter,
           count(*)                                              as n_quarters_observed,
           min(q.quarter)                                        as first_quarter_at_3,
           max(case when q.quarter = t.detection_quarter then q.a end)                      as a_now,
           max(case when q.quarter = t.detection_quarter then q.is_combo::int end)          as is_combo,
           max(case when q.quarter = (select max(x.quarter) from read_parquet('{P}/pair_quarter_counts.parquet') x
                                      where x.drug_ai=t.drug_ai and x.pt_clean=t.pt_clean
                                        and x.quarter < t.detection_quarter) then q.a end)  as a_prev1
    from tgt t
    join read_parquet('{P}/pair_quarter_counts.parquet') q
      on q.drug_ai = t.drug_ai and q.pt_clean = t.pt_clean and q.quarter <= t.detection_quarter
    group by 1,2,3
),
-- how broad is the DRUG at detection: distinct events, total reports
drug_breadth as (
    select q.drug_ai, q.quarter,
           count(distinct q.pt_clean) as drug_n_events,
           sum(q.a)                   as drug_sum_a
    from read_parquet('{P}/pair_quarter_counts.parquet') q group by 1,2
),
-- how broad is the EVENT at detection: distinct drugs reporting it
event_breadth as (
    select q.pt_clean, q.quarter,
           count(distinct q.drug_ai) as event_n_drugs,
           sum(q.a)                  as event_sum_a
    from read_parquet('{P}/pair_quarter_counts.parquet') q group by 1,2
),
-- case mix for the pair, over cases RECEIVED UP TO the detection quarter only
mix as (
    select t.drug_ai, t.pt_clean, t.detection_quarter,
           count(*)                                   as n_cases,
           avg(c.DE::int)                             as frac_death,
           avg(greatest(c.DE,c.HO,c.LT,c.DS,c.CA,c.RI)::int) as frac_serious,
           avg(c.occp_hcp::int)                       as frac_hcp,
           avg(c.occp_consumer::int)                  as frac_consumer,
           avg(c.occp_lawyer::int)                    as frac_lawyer,
           avg(c.us_flag::int)                        as frac_us,
           median(c.age_yrs)                          as median_age,
           avg(case when c.sex='F' then 1 when c.sex='M' then 0 end) as frac_female
    from tgt t
    join read_parquet('{P}/ps_drug_event_cases.parquet') l
      on l.drug_ai = t.drug_ai and l.pt_clean = t.pt_clean
     and l.in_counting_universe and l.init_quarter <= t.detection_quarter
    join read_parquet('{P}/case_table.parquet') c
      on c.caseid = l.caseid and c.in_counting_universe
    group by 1,2,3
)
select t.*,
       h.n_quarters_observed, h.first_quarter_at_3, h.is_combo,
       coalesce(h.a_now - h.a_prev1, 0)                                  as a_growth_1q,
       case when h.a_prev1 > 0 then (h.a_now - h.a_prev1)*1.0/h.a_prev1 end as a_growth_1q_rel,
       db.drug_n_events, db.drug_sum_a, eb.event_n_drugs, eb.event_sum_a,
       t.a*1.0/nullif(t.a+t.b,0)   as drug_event_share,
       t.a*1.0/nullif(t.a+t.c,0)   as event_drug_share,
       m.n_cases, m.frac_death, m.frac_serious, m.frac_hcp, m.frac_consumer,
       m.frac_lawyer, m.frac_us, m.median_age, m.frac_female,
       ln(nullif(t.ror,0))         as log_ror,
       ln(nullif(t.prr,0))         as log_prr,
       ln(t.a+0.5)                 as log_a
from tgt t
left join hist h  on h.drug_ai=t.drug_ai and h.pt_clean=t.pt_clean and h.detection_quarter=t.detection_quarter
left join drug_breadth  db on db.drug_ai=t.drug_ai  and db.quarter=t.detection_quarter
left join event_breadth eb on eb.pt_clean=t.pt_clean and eb.quarter=t.detection_quarter
left join mix m   on m.drug_ai=t.drug_ai and m.pt_clean=t.pt_clean and m.detection_quarter=t.detection_quarter
"""

con = duckdb.connect()
con.execute(f"COPY ({SQL}) TO '{OUT}' (FORMAT PARQUET)")
df = con.execute(f"select * from read_parquet('{OUT}')").df()
print(f"{OUT.name}: {len(df):,} rows x {df.shape[1]} cols")
print("positives event_3y:", int(df.confirmed_event_3y.sum()), "| drug_3y:", int(df.confirmed_drug_3y.sum()))
nulls = df.isna().mean().sort_values(ascending=False)
print("\ncolumns with nulls:"); print((100*nulls[nulls > 0]).round(1).to_string())
