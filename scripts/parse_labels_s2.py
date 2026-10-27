"""Parse saved SrLC detail pages -> data/processed/labels/s2_label_changes.parquet/csv.

Run: /usr/local/bin/python3 scripts/parse_labels_s2.py   (needs pandas, bs4, duckdb)
Reads only local files in data/raw/labels/s2_detail_*.html (no network).
One row per (DrugNameID, approval date, supplement, changed label section).
Label text is kept verbatim (whitespace-normalised only); every row carries source_file.
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

# FDA's own legend on every detail page (see the "Sections" table there).
SECTION_CODES = [
    (r"box(ed)? warning", "BW"),
    (r"warnings and precautions|^warnings$|^precautions$", "WP"),
    (r"adverse reactions", "AR"),
    (r"drug interactions", "DI"),
    (r"use in specific populations", "USP"),
    (r"pci|patient counseling|patient information|medication guide", "PCI/PI/MG"),
    (r"contraindications", "CONTRA"),
    (r"dosage and administration", "DOSAGE"),
    (r"indications and usage", "INDIC"),
    (r"overdosage", "OVERDOSE"),
    (r"clinical pharmacology", "CLINPHARM"),
    (r"drug abuse and dependence", "ABUSE"),
    (r"^other$", "OTHER"),
]
SECTION_C = [(re.compile(p, re.I), c) for p, c in SECTION_CODES]
# FDA writes this instead of reproducing the change; the section/date are valid but the text is absent.
STUB_RE = re.compile(r"extensive changes;?\s*please refer to (the )?label", re.I)


def norm(s):
    return re.sub(r"\s+", " ", (s or "").replace("\xa0", " ")).strip()


def block_text(nodes):
    """Verbatim text of a run of nodes, with <br>/<p>/<li> boundaries kept as spaces."""
    html = "".join(str(n) for n in nodes)
    soup = BeautifulSoup(html, "html.parser")
    return norm(soup.get_text(" "))


def section_code(name):
    for rx, c in SECTION_C:
        if rx.search(name):
            return c
    return ""


def parse_page(f):
    """Return (rows, notes) for one detail page."""
    soup = BeautifulSoup(f.read_text(encoding="utf-8", errors="replace"), "html.parser")
    did = re.search(r"s2_detail_(\d+)\.html", f.name).group(1)
    notes = []

    head = soup.select_one("div.filter-REMS")
    if head is None:
        return [], [(did, "no_header_block")]
    h3, h5 = head.find("h3"), head.find("h5")
    small = h3.find("small") if h3 else None
    app_raw = norm(small.get_text(" ")).strip("()") if small else ""
    if small:
        small.extract()
    drug = norm(h3.get_text(" ")) if h3 else ""
    ingr = norm(h5.get_text(" ")).strip("()") if h5 else ""
    if not ingr:
        notes.append((did, "blank_active_ingredient"))
    m = re.match(r"([A-Z]+)-(\d+)", app_raw)
    app_type, app_num = (m.group(1), m.group(2)) if m else ("", "")

    rows = []
    for hdr in soup.select("h3.accordion-header"):
        date_m = re.search(r"\d{2}/\d{2}/\d{4}", hdr.get_text(" "))
        suppl_el = hdr.find("small")
        suppl = norm(suppl_el.get_text(" ")).strip("()") if suppl_el else ""
        if not date_m:
            notes.append((did, f"accordion_header_without_date: {norm(hdr.get_text(' '))[:60]}"))
            continue
        date = date_m.group(0)
        body = hdr.find_next_sibling("div")
        if body is None:
            notes.append((did, f"no_content_div_for_{date}"))
            continue
        pdf = body.find("a", href=re.compile(r"lbl\.pdf"))
        # Only h4s that are direct children of the content div are section headings; FDA also uses
        # <h4> for sub-headings INSIDE a section's body (e.g. "Risk Summary"), which must not split it.
        h4s = [h for h in body.find_all("h4") if h.parent is body]
        if not h4s and body.find("h4"):
            notes.append((did, f"only_nested_h4_for_{date}"))
        if not h4s:
            notes.append((did, f"no_sections_listed_for_{date}"))
            continue
        for h4 in h4s:
            raw_name = norm(h4.get_text(" "))
            nm = re.match(r"([\d.]+)\s+(.*)", raw_name)
            num, name = (nm.group(1), nm.group(2)) if nm else ("", raw_name)
            run = []
            for sib in h4.next_siblings:
                if getattr(sib, "name", None) in ("h4", "h3"):
                    break
                run.append(sib)
            text = block_text(run)
            sub = next((norm(n.get_text(" ")) for n in BeautifulSoup("".join(str(x) for x in run), "html.parser")
                        .find_all(["strong", "b"])), "")
            rows.append(dict(
                drug_name_id=did, drug_name=drug, active_ingredient=ingr,
                application_type=app_type, application_number=app_num,
                change_date=date, supplement=suppl,
                section_number=num, section_name=name, section_code=section_code(raw_name),
                subsection=sub, label_text=text, label_text_chars=len(text),
                is_stub=bool(STUB_RE.search(text)), label_pdf_url=pdf["href"] if pdf else "",
                source_file=str(f.relative_to(ROOT))))
    if not rows:
        notes.append((did, "no_rows_extracted"))
    return rows, notes


def main():
    files = sorted(RAW.glob("s2_detail_*.html"), key=lambda p: int(re.search(r"(\d+)", p.name).group(1)))
    files = [f for f in files if "invalid" not in f.name]
    rows, notes = [], []
    for f in files:
        r, n = parse_page(f)
        rows += r; notes += n
    df = pd.DataFrame(rows)
    df["change_date_iso"] = pd.to_datetime(df.change_date, format="%m/%d/%Y", errors="coerce").dt.strftime("%Y-%m-%d")
    bad = int(df.change_date_iso.isna().sum())

    duckdb.sql(f"COPY (SELECT * FROM df) TO '{OUT / 's2_label_changes.parquet'}' (FORMAT PARQUET)")
    df.to_csv(OUT / "s2_label_changes.csv", index=False, quoting=csv.QUOTE_MINIMAL)

    rep = dict(
        n_pages=len(files), n_pages_with_rows=int(df.drug_name_id.nunique()), n_rows=len(df),
        n_distinct_changes=int(df.groupby(["drug_name_id", "change_date", "supplement"]).ngroups),
        n_drugs=int(df.drug_name.nunique()), n_ingredients=int(df.active_ingredient.nunique()),
        date_range=[df.change_date_iso.min(), df.change_date_iso.max()],
        unparseable_dates=bad,
        rows_per_year=df.change_date_iso.str[:4].value_counts().sort_index().to_dict(),
        section_code_counts=df.section_code.replace("", "(uncoded)").value_counts().to_dict(),
        top_section_names=df.section_name.value_counts().head(25).to_dict(),
        n_stub_rows=int(df.is_stub.sum()),
        n_empty_text=int((df.label_text_chars == 0).sum()),
        label_text_chars=dict(median=int(df.label_text_chars.median()), p95=int(df.label_text_chars.quantile(.95)),
                              max=int(df.label_text_chars.max())),
        notes=Counter(n[1].split(":")[0] for n in notes),
        note_examples=[f"{d}: {t}" for d, t in notes[:25]])
    (OUT / "s2_parse_report.json").write_text(json.dumps(rep, indent=1, default=str))
    print(json.dumps({k: v for k, v in rep.items() if k not in ("top_section_names", "note_examples")}, indent=1, default=str))


if __name__ == "__main__":
    main()
