"""Parse saved FDA Section 921 quarterly signal pages -> data/processed/labels/s1_signals.parquet/csv.

Run: /usr/local/bin/python3 scripts/parse_labels_s1.py  (needs pandas, bs4, duckdb)
Reads only local files in data/raw/labels/ (no network). Every row carries source_file / source_url.
Raw text is kept verbatim (whitespace-normalised only). outcome_class is a keyword rule on fda_action_raw.
"""
import csv, re, json
from collections import Counter
from pathlib import Path
from bs4 import BeautifulSoup
import pandas as pd
import duckdb

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "data/raw/labels"
OUT = ROOT / "data/processed/labels"
OUT.mkdir(parents=True, exist_ok=True)

DATE_RE = re.compile(
    r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\.?\s+(?:\d{1,2},\s*)?\d{4}\b|\b\d{1,2}/\d{1,2}/\d{2,4}\b")
ASOF_RE = re.compile(r"as of\s*[:\-]?\s*(.+?)\)?\s*$", re.I)

# ---- outcome keyword rules (applied to lowercased fda_action_raw) ----
LABELING = [r"\b(was|were|has been|have been|had been)\s+(updated|revised|added|modified|strengthened|changed|approved)",
            r"\bsections? of (the )?(product )?label(ing)?\b.{0,80}\b(updated|revised|added|modified)",
            r"\blabel(ing)? (was|were|has been|have been) (updated|revised|changed|modified)",
            r"\b(added|adding) to the (boxed warning|warnings?|precautions|adverse reactions|contraindications|drug interactions|"
            r"warnings and precautions|use in specific populations|label)",
            r"\blabeling changes? (was|were|has been|have been|approved)", r"\bapproved (a )?(labeling|label) (change|supplement)",
            r"\brequired (a )?(class )?label(ing)? change", r"\bfda has required\b.{0,60}\blabel", r"\bsafety labeling change",
            r"\blabel(ing)? (was|were) updated", r"\bupdate[sd]? (the )?label",
            r"\bsections? (was |were |is |are )?(updated|revised) to (include|add)"]
NO_ACTION = [r"no (further )?(regulatory )?action (is|was|will be|has been) (necessary|needed|required|planned)",
             r"no (further )?(regulatory )?action (is )?(necessary|needed|required|planned)",
             r"decided that no (regulatory )?action", r"determined that no (regulatory )?action",
             r"does not (plan|intend) to (take|pursue) (any )?(further |regulatory )?action",
             r"no (additional|further) (regulatory )?action", r"no (regulatory )?action is (currently )?(warranted|indicated)",
             r"not (taking|planning) (any )?(further |regulatory )?action"]
EVALUATING = [r"\bis (currently )?(still )?(being )?(evaluat|review|assess|investigat)", r"\bare (currently )?(still )?(being )?(evaluat|review|assess)",
              r"\b(evaluation|review|assessment|investigation) (is|are|remains?) (ongoing|continuing|in progress|underway)",
              r"\bcontinues? to (evaluate|review|assess|monitor)", r"\bongoing (evaluation|review|assessment)",
              r"\bunder (evaluation|review)\b", r"\bfda is (continuing|currently)", r"\bbeing evaluated\b", r"\bfurther evaluation\b"]
_c = lambda L: [re.compile(p) for p in L]
LAB_C, NOA_C, EVA_C = _c(LABELING), _c(NO_ACTION), _c(EVALUATING)


def classify(text):
    t = re.sub(r"\s+", " ", (text or "").lower())
    if not t.strip():
        return "other_unclear", "blank"
    hits = {"labeling_changed": any(p.search(t) for p in LAB_C),
            "no_action": any(p.search(t) for p in NOA_C),
            "still_evaluating": any(p.search(t) for p in EVA_C)}
    on = [k for k, v in hits.items() if v]
    return (on[0] if len(on) == 1 else "other_unclear"), "+".join(on)   # none or conflicting signals -> unclear


def snap(url):
    m = re.search(r"web\.archive\.org/web/(\d{14})/", url)
    return f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:8]}" if m else ""


def norm(s):
    return re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()


def cell_text(td):
    """Cell text with <p>/<li>/<br>/<div> boundaries kept as ' | ' (so product lists stay readable)."""
    from bs4 import NavigableString
    td = BeautifulSoup(str(td), "html.parser")
    for t in td.find_all(["p", "li", "br", "div"]):
        t.insert_before(NavigableString(" | "))
    txt = norm(td.get_text(" "))
    txt = re.sub(r"(\s*\|\s*)+", " | ", txt).strip(" |")
    return txt


def expand_rows(trs):
    """Yield (cells, inherited_cols) per row, resolving rowspan/colspan."""
    pending = {}   # col -> [remaining_rows, text]
    for tr in trs:
        out, inh, col = [], [], 0
        tds = iter(tr.find_all(["td", "th"]))
        while True:
            if col in pending and pending[col][0] > 0:
                pending[col][0] -= 1; out.append(pending[col][1]); inh.append(col); col += 1; continue
            td = next(tds, None)
            if td is None: break
            txt = cell_text(td)
            rs, cs = int(td.get("rowspan", 1) or 1), int(td.get("colspan", 1) or 1)
            for _ in range(cs):
                out.append(txt)
                if rs > 1: pending[col] = [rs - 1, txt]
                col += 1
        while col in pending and pending[col][0] > 0:   # trailing spanned cells
            pending[col][0] -= 1; out.append(pending[col][1]); inh.append(col); col += 1
        yield out, inh


def main():
    final = dict(pd.read_csv(RAW / "s1_final_urls.tsv", sep="\t").values)
    man = pd.read_csv(RAW / "s1_manifest.tsv", sep="\t")
    rows, skipped = [], []
    for _, m in man.iterrows():
        f = RAW / f"s1_{m.quarter}.html"
        if not f.exists():
            q = RAW / "_quarantine_robots_disallowed" / f.name
            skipped.append((m.quarter, "quarantined_robots_disallowed" if q.exists() else "file_missing")); continue
        soup = BeautifulSoup(f.read_text(encoding="utf-8", errors="replace"), "html.parser")
        main_ = soup.find("main") or soup
        n_tab = 0
        for tab in main_.find_all("table"):
            trs = tab.find_all("tr")
            if not trs: continue
            hi = next((i for i, tr in enumerate(trs)
                       if any("signal" in norm(c.get_text(" ")).lower() for c in tr.find_all(["th", "td"]))), None)
            if hi is None: continue
            hdr = [norm(c.get_text(" ")) for c in trs[hi].find_all(["th", "td"])]
            n_tab += 1
            asof = ""
            for h in hdr:
                a = ASOF_RE.search(h)
                if a and "additional" in h.lower(): asof = a.group(1).strip()
            for cells, inh in expand_rows(trs[hi + 1:]):
                if not any(cells): continue
                note = []
                if len(cells) < 3:   # spacer / sub-header row, not a signal row; logged in report
                    skipped.append((m.quarter, f"non_signal_row[{len(cells)} cells]: {' | '.join(cells)[:80]}")); continue
                if len(cells) > 3: note.append(f"unexpected_cell_count={len(cells)}")
                if inh: note.append("cols_inherited_via_rowspan=" + ",".join(["drug", "signal", "action"][c] for c in inh if c < 3))
                drug, sig, act = cells[:3]
                oc, om = classify(act)
                if not act: note.append("fda_action_blank")
                rows.append(dict(posting_quarter=m.quarter, drug_name_raw=drug, potential_signal_raw=sig,
                                 fda_action_raw=act, action_asof_date_raw=asof,
                                 dates_in_action_text="; ".join(DATE_RE.findall(act)),
                                 outcome_class=oc, outcome_matches=om, source_url=final.get(m.quarter, m.url), snapshot_date=snap(final.get(m.quarter, m.url)), listed_url=m.url, source_file=str(f.relative_to(ROOT)),
                                 index_source=m.index_source, notes="; ".join(note)))
        if n_tab == 0: skipped.append((m.quarter, "no_signal_table_found"))
    df = pd.DataFrame(rows)
    duckdb.sql(f"COPY (SELECT * FROM df) TO '{OUT / 's1_signals.parquet'}' (FORMAT PARQUET)")
    df.to_csv(OUT / "s1_signals.csv", index=False, quoting=csv.QUOTE_MINIMAL)
    # status-string audit: every distinct raw action string with count and assigned class
    aud = (df.groupby(["fda_action_raw", "outcome_class"]).size().reset_index(name="n")
             .sort_values("n", ascending=False))
    aud.to_csv(OUT / "s1_outcome_mapping_audit.csv", index=False)
    rep = dict(n_rows=len(df), n_quarters=int(df.posting_quarter.nunique()), skipped=skipped,
               rows_per_quarter=df.posting_quarter.value_counts().sort_index().to_dict(),
               outcome_counts=df.outcome_class.value_counts().to_dict(),
               n_distinct_action_strings=int(df.fda_action_raw.nunique()),
               rules=dict(labeling_changed=LABELING, no_action=NO_ACTION, still_evaluating=EVALUATING,
                          other_unclear="none matched, or more than one of the three classes matched, or action text blank"))
    (OUT / "s1_parse_report.json").write_text(json.dumps(rep, indent=1))
    print(json.dumps({k: v for k, v in rep.items() if k != "rules"}, indent=1))


if __name__ == "__main__":
    main()
