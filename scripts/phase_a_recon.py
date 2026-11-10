"""Phase A: reconnaissance only. Reads data, changes nothing, writes results/phase_a/.

Run: /usr/local/bin/python3 scripts/phase_a_recon.py [--no-network]

A1 how pre-2016 detections are labelled, and whether MedWatch was ingested
A2 data budget at horizons 12 / 24 / 36 months
A3 whether a stable ingredient or drug id exists per row
A4 whether the current test period was used in tuning or selection
A5 DailyMed access test (network)

Every number printed here is written to results/phase_a/phase_a.json so the report can cite it.
"""
import sys, json, time, hashlib, subprocess, platform
from pathlib import Path
import duckdb, pandas as pd

ROOT = Path(__file__).resolve().parents[1]
P, L = ROOT / "data/processed", ROOT / "data/processed/labels"
OUT = ROOT / "results/phase_a"
OUT.mkdir(parents=True, exist_ok=True)
NET = "--no-network" not in sys.argv
DATA_END_Q = "2026Q2"                      # last FAERS quarter in the counting universe
RULES = ("T1", "T2", "T3")
R = {}


def q_end(q):
    return pd.Timestamp(int(q[:4]), int(q[-1]) * 3, 1) + pd.offsets.MonthEnd(0)


def sha(p, cap=50_000_000):
    h, n = hashlib.sha256(), 0
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk); n += len(chunk)
            if n >= cap:
                return h.hexdigest() + f"(first {cap // 10**6}MB)"
    return h.hexdigest()


con = duckdb.connect()

# ---------------------------------------------------------------- provenance
R["provenance"] = dict(
    git_commit=subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip(),
    git_dirty=bool(subprocess.run(["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True).stdout.strip()),
    python=platform.python_version(),
    packages={m: __import__(m).__version__ for m in ("duckdb", "pandas", "numpy", "sklearn", "lightgbm")},
    files={f.name: dict(bytes=f.stat().st_size, sha256=sha(f)) for f in sorted(
        [P / f"detected_pairs_{t}.parquet" for t in RULES] +
        [P / f"confirmation_target_{t}.parquet" for t in RULES] +
        [L / "s2_label_changes.parquet"])})

# ---------------------------------------------------------------- A1
a1 = {}
for t in RULES:
    d = con.execute(f"""select status, substr(detection_quarter,1,4) yr, count(*) n
        from read_parquet('{P}/detected_pairs_{t}.parquet') where status <> 'never'
        group by 1,2 order by 2,1""").df()
    a1[t] = dict(by_status=d.groupby("status").n.sum().to_dict(),
                 pre_window_by_year=d[d.status == "pre_window"].set_index("yr").n.to_dict(),
                 in_window_by_year=d[d.status == "in_window"].set_index("yr").n.to_dict())
lc = con.execute(f"select min(change_date_iso) a, max(change_date_iso) b, count(*) n from read_parquet('{L}/s2_label_changes.parquet')").df()
rows_pre2016 = con.execute(f"select count(*) from read_parquet('{L}/s2_label_changes.parquet') where change_date_iso < '2016-01-01'").fetchone()[0]
medwatch = sorted(str(p.relative_to(ROOT)) for p in (ROOT / "data/raw/labels").glob("*medwatch*")) if (ROOT / "data/raw/labels").exists() else []
a1["srlc_coverage"] = dict(first_change=lc.a[0], last_change=lc.b[0], rows=int(lc.n[0]), rows_before_2016=int(rows_pre2016))
a1["medwatch_files_found"] = medwatch
a1["dsc_date_range"] = con.execute(f"select min(dsc_date), max(dsc_date) from read_csv_auto('{L}/s3_dsc_index.csv')").fetchone()
a1["s921_quarter_range"] = con.execute(f"select min(posting_quarter), max(posting_quarter) from read_csv_auto('{L}/s1_signals.csv')").fetchone()
R["A1"] = a1

# ---------------------------------------------------------------- A2
end = q_end(DATA_END_Q)
budget = {}
for t in RULES:
    d = con.execute(f"""select drug_ai, pt_clean, detection_quarter, status
        from read_parquet('{P}/detected_pairs_{t}.parquet') where status <> 'never'""").df()
    d["dq_end"] = d.detection_quarter.map(q_end)
    d["yr"] = d.detection_quarter.str[:4]
    tg = con.execute(f"select drug_ai, pt_clean, detection_quarter, confirmed_event_1y, confirmed_event_2y, confirmed_event_3y from read_parquet('{P}/confirmation_target_{t}.parquet')").df()
    d = d.merge(tg, on=["drug_ai", "pt_clean", "detection_quarter"], how="left")
    rows = []
    for H, col in ((12, "confirmed_event_1y"), (24, "confirmed_event_2y"), (36, "confirmed_event_3y")):
        d["followable"] = d.dq_end + pd.DateOffset(months=H) <= end
        for yr, g in d.groupby("yr"):
            f = g[g.followable]
            rows.append(dict(rule=t, H_months=H, year=yr, signals=len(g),
                             followable=int(g.followable.sum()), too_recent=int((~g.followable).sum()),
                             positives=int(f[col].fillna(0).sum()) if len(f) else 0,
                             prevalence_pct=round(100 * f[col].fillna(0).mean(), 3) if len(f) else None,
                             distinct_ingredients=int(g.drug_ai.nunique())))
    budget[t] = rows
    pd.DataFrame(rows).to_csv(OUT / f"a2_budget_{t}.csv", index=False)
R["A2"] = {t: dict(usable_window={H: dict(
    last_followable_quarter=max([r["year"] for r in budget[t] if r["H_months"] == H and r["followable"] > 0], default=None),
    total_followable=sum(r["followable"] for r in budget[t] if r["H_months"] == H),
    total_positives=sum(r["positives"] for r in budget[t] if r["H_months"] == H))
    for H in (12, 24, 36)}) for t in RULES}

# ---------------------------------------------------------------- A3
a3 = con.execute(f"""select count(*) as n_rows, count(distinct drug_ai) as distinct_drug_ai,
    sum(is_combo::int) as combo_rows from read_parquet('{P}/detected_pairs_T1.parquet')""").df().to_dict("records")[0]
a3["distinct_drug_ai_in_window"] = con.execute(f"select count(distinct drug_ai) from read_parquet('{P}/confirmation_target_T1.parquet')").fetchone()[0]
a3["external_id_columns_present"] = [c for c in con.execute(f"select * from read_parquet('{P}/detected_pairs_T1.parquet') limit 0").df().columns
                                     if any(k in c.lower() for k in ("rxcui", "setid", "unii", "ndc", "appl"))]
a3["pairs_per_ingredient"] = con.execute(f"""select median(n), max(n), avg(n) from
    (select drug_ai, count(*) n from read_parquet('{P}/confirmation_target_T1.parquet') group by 1)""").fetchone()
R["A3"] = a3

# ---------------------------------------------------------------- A4
log = subprocess.run(["git", "log", "--oneline", "--all"], cwd=ROOT, capture_output=True, text=True).stdout
R["A4"] = dict(
    commits_mentioning_tuning=[l for l in log.splitlines() if any(k in l.lower() for k in ("tune", "tuning", "n_estimators", "artefact", "artifact"))],
    split_constant_in_fit_models=subprocess.run(["grep", "-n", "SPLIT, CV_CUTS", "scripts/fit_models.py"], cwd=ROOT, capture_output=True, text=True).stdout.strip(),
    cv_uses_test=bool(subprocess.run(["grep", "-nE", r"cv_score\(.*te\b|eval_set=\[\(Xte|fit\(Xte", "scripts/fit_models.py"], cwd=ROOT, capture_output=True, text=True).stdout.strip()),
    notebooks=[str(p.relative_to(ROOT)) for p in (ROOT / "notebooks").glob("*.ipynb")] if (ROOT / "notebooks").exists() else [])

# ---------------------------------------------------------------- A5
a5 = dict(attempted=NET)
if NET:
    import requests
    UA = {"User-Agent": "faers-signal-confirmation-research/0.1 (academic research)"}
    probe = ROOT / "data/raw/dailymed/_probe"; probe.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    try:
        r = requests.get("https://dailymed.nlm.nih.gov/dailymed/services/v2/spls.json",
                         params={"drug_name": "PANTOPRAZOLE", "pagesize": 5}, headers=UA, timeout=30)
        a5["search_status"] = r.status_code
        a5["search_seconds"] = round(time.time() - t0, 2)
        setids = [d["setid"] for d in r.json().get("data", [])][:5] if r.ok else []
        a5["setids_found"] = setids
        hist = []
        for sid in setids:
            time.sleep(1.0)
            t1 = time.time()
            h = requests.get(f"https://dailymed.nlm.nih.gov/dailymed/services/v2/spls/{sid}/history.json", headers=UA, timeout=30)
            v = h.json().get("data", {}).get("history", []) if h.ok else []
            hist.append(dict(setid=sid, status=h.status_code, versions=len(v), seconds=round(time.time() - t1, 2),
                             # history comes back newest-first, so v[-1] is version 1
                             newest=(v[0].get("spl_version"), v[0].get("published_date")) if v else None,
                             oldest=(v[-1].get("spl_version"), v[-1].get("published_date")) if v else None))
        a5["history"] = hist
        got = max((h for h in hist if h["versions"] > 1), key=lambda h: h["versions"], default=None)
        if got:
            # downloadzipfile.cfm IGNORES the version parameter and always returns the current label.
            # getFile.cfm?type=zip&version=N returns the requested historical version. Verified by
            # reading <effectiveTime> out of the SPL xml inside each zip.
            import io, zipfile, re as _re
            def probe_version(sid, ver):
                time.sleep(1.2); t = time.time()
                r = requests.get("https://dailymed.nlm.nih.gov/dailymed/getFile.cfm",
                                 params={"setid": sid, "type": "zip", "version": ver}, headers=UA, timeout=90)
                eff = None
                if r.ok and r.content[:2] == b"PK":
                    zf = zipfile.ZipFile(io.BytesIO(r.content))
                    xml = [n for n in zf.namelist() if n.endswith(".xml")]
                    if xml:
                        m = _re.search(r'<effectiveTime value="(\d+)"', zf.read(xml[0]).decode("utf-8", "replace"))
                        eff = m.group(1) if m else None
                return dict(version=ver, status=r.status_code, bytes=len(r.content),
                            effective_time=eff, seconds=round(time.time() - t, 2), content=r.content)
            sid = got["setid"]
            oldest_v = got["oldest"][0]; newest_v = got["newest"][0]
            v_old, v_new = probe_version(sid, oldest_v), probe_version(sid, newest_v)
            f = probe / f"{sid}_v{oldest_v}.zip"
            if v_old["status"] == 200 and v_old["content"][:2] == b"PK":
                f.write_bytes(v_old["content"])
            a5["version_retrieval"] = dict(
                setid=sid, endpoint="getFile.cfm?type=zip&version=N",
                oldest={k: v for k, v in v_old.items() if k != "content"},
                newest={k: v for k, v in v_new.items() if k != "content"},
                versions_differ=v_old["effective_time"] != v_new["effective_time"],
                saved=str(f.relative_to(ROOT)) if f.exists() else None)
            # the endpoint the brief named for comparison
            time.sleep(1.2)
            bad = requests.get("https://dailymed.nlm.nih.gov/dailymed/downloadzipfile.cfm",
                               params={"setid": sid, "version": oldest_v}, headers=UA, timeout=90)
            a5["downloadzipfile_cfm_honours_version"] = (len(bad.content) == v_old["bytes"])
        a5["rate_limit_headers"] = {k: v for k, v in r.headers.items() if "rate" in k.lower() or "retry" in k.lower()}
    except Exception as e:
        a5["error"] = f"{type(e).__name__}: {e}"
R["A5"] = a5

(OUT / "phase_a.json").write_text(json.dumps(R, indent=1, default=str))
print(json.dumps({k: v for k, v in R.items() if k != "provenance"}, indent=1, default=str)[:7000])
