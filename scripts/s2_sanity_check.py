"""Sanity check: dump ONE saved SrLC detail page to CSV so the scrape output can be eyeballed.

Not the real parser (that comes later) - this just proves the pages contain the fields we expect.
Run: /usr/local/bin/python3 scripts/s2_sanity_check.py [DrugNameID]   (default 117)
Out: data/processed/labels/s2_sanity_check.csv, one row per changed label section.
"""
import re, sys, csv, html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
did = sys.argv[1] if len(sys.argv) > 1 else "117"
src = ROOT / f"data/raw/labels/s2_detail_{did}.html"
out = ROOT / "data/processed/labels/s2_sanity_check.csv"


def text(s):
    s = re.sub(r"<br[^>]*>|</p>|</li>", " ", s)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


h = src.read_text(encoding="utf-8", errors="replace")

head = re.search(r'<div class="form-inline filter-REMS">\s*<h3>(.*?)<small>\((.*?)\)</small></h3>\s*<h5>\((.*?)\)</h5>', h, re.S)
drug, app, ingr = (text(g) for g in head.groups())

# Each accordion header is one approved change (date + supplement), followed by its content div.
blocks = re.split(r'<h3 class="background_text accordion-header[^"]*">', h)[1:]
rows = []
for b in blocks:
    m = re.match(r"\s*(\d{2}/\d{2}/\d{4})</a>\s*<small>\((.*?)\)</small>", b)
    if not m:
        continue
    date, suppl = m.group(1), text(m.group(2))
    pdf = re.search(r'href="(http[^"]*lbl\.pdf)"', b)
    # <h4> = changed label section; the text that follows it, up to the next <h4>/<h3>, is the change.
    for sec in re.finditer(r"<h4>(.*?)</h4>(.*?)(?=<h4>|<h3|\Z)", b, re.S):
        name = text(sec.group(1))
        body = sec.group(2)
        sub = re.search(r"<strong>(.*?)</strong>", body, re.S)
        num = re.match(r"(\d+(?:\.\d+)*)\s+(.*)", name)
        rows.append(dict(
            drug_name_id=did, drug_name=drug, application=app, active_ingredient=ingr,
            change_date=date, supplement=suppl,
            section_number=num.group(1) if num else "",
            section_name=num.group(2) if num else name,
            subsection=text(sub.group(1)) if sub else "",
            label_text=text(body),
            label_text_chars=len(text(body)),
            label_pdf_url=pdf.group(1) if pdf else "",
            source_file=str(src.relative_to(ROOT)),
        ))

out.parent.mkdir(parents=True, exist_ok=True)
with out.open("w", newline="", encoding="utf-8") as f:
    w = csv.DictWriter(f, fieldnames=list(rows[0]))
    w.writeheader(); w.writerows(rows)
print(f"{src.name}: {drug} ({app}) -> {len(rows)} row(s) -> {out.relative_to(ROOT)}")
for r in rows:
    print(f"  {r['change_date']} {r['supplement']} | {r['section_number']} {r['section_name']} | {r['label_text_chars']} chars")
