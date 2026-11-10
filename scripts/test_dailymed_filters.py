"""Assert that every DailyMed API filter parameter we rely on actually changes the result set.

Run: /usr/local/bin/python3 scripts/test_dailymed_filters.py
Out: results/phase_b1/api_filter_tests.json   (exit 1 if any relied-on filter is a no-op)

Why this exists. Two DailyMed parameters are accepted, return HTTP 200, and are silently ignored:

  downloadzipfile.cfm?version=N   returns the CURRENT label whatever version you ask for
  spls.json?marketing_category=X  returns the same set whatever category you ask for

Both fail open, so code built on them looks like it works. A filter is only trusted here if asking
for two different values gives two different answers, and in the version case only if the bytes we
get back actually carry the effectiveTime we asked for.
"""
import json, sys, time, io, re, zipfile
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "results/phase_b1"; OUT.mkdir(parents=True, exist_ok=True)
B = "https://dailymed.nlm.nih.gov/dailymed/"
UA = {"User-Agent": "faers-signal-confirmation-research/0.1 (academic research)"}
SETID = "6b5ed424-2203-482f-bb85-2c97d9c356f2"      # 15 versions, oldest 2016
DELAY = 1.0
res, fails = [], []


def get(url, **params):
    time.sleep(DELAY)
    return requests.get(url, params=params, headers=UA, timeout=90)


def total(**params):
    return get(B + "services/v2/spls.json", pagesize=1, **params).json()["metadata"]["total_elements"]


def effective_time(content):
    if content[:2] != b"PK":
        return None
    zf = zipfile.ZipFile(io.BytesIO(content))
    xml = [n for n in zf.namelist() if n.endswith(".xml")]
    if not xml:
        return None
    m = re.search(r'<effectiveTime value="(\d+)"', zf.read(xml[0]).decode("utf-8", "replace"))
    return m.group(1) if m else None


def check(name, relied_on, passed, detail):
    res.append(dict(filter=name, relied_on=relied_on, passed=passed, detail=detail))
    if relied_on and not passed:
        fails.append(name)
    print(f"{'PASS' if passed else 'FAIL'} {'[relied on]' if relied_on else '[not used] '} {name}: {detail}")


# 1. drug_name - relied on, must narrow the set
all_n = total()
pan = total(drug_name="PANTOPRAZOLE")
check("spls.json?drug_name", True, 0 < pan < all_n, f"all={all_n:,} PANTOPRAZOLE={pan:,}")

# 2. application_number - relied on for NDA/BLA scope
app = total(application_number="NDA020987")
check("spls.json?application_number", True, 0 < app < all_n, f"NDA020987={app}")

# 3. unii_code - relied on as a fallback mapping route
uni = total(unii_code="D8TST4O562")
check("spls.json?unii_code", True, 0 < uni < all_n, f"UNII D8TST4O562={uni}")

# 4. marketing_category - NOT relied on; documented no-op
mc = {c: total(drug_name="PANTOPRAZOLE", marketing_category=c) for c in ("NDA", "ANDA", "BLA")}
check("spls.json?marketing_category", False, len(set(mc.values())) > 1,
      f"{mc} vs unfiltered {pan} -> silently ignored, do not use")

# 5. getFile.cfm version - relied on, must return the version asked for
v1 = get(B + "getFile.cfm", setid=SETID, type="zip", version=1)
v15 = get(B + "getFile.cfm", setid=SETID, type="zip", version=15)
e1, e15 = effective_time(v1.content), effective_time(v15.content)
check("getFile.cfm?version", True, e1 is not None and e1 != e15,
      f"v1 effectiveTime={e1}, v15 effectiveTime={e15}")

# 6. downloadzipfile.cfm version - NOT relied on; documented no-op
bad = get(B + "downloadzipfile.cfm", setid=SETID, version=1)
eb = effective_time(bad.content)
check("downloadzipfile.cfm?version", False, eb == e1,
      f"asked v1, got effectiveTime={eb} (v1 is {e1}) -> silently ignored, do not use")

(OUT / "api_filter_tests.json").write_text(json.dumps(
    dict(checks=res, relied_on_failures=fails), indent=2))
print()
print("relied-on filters failing:", fails or "none")
sys.exit(1 if fails else 0)
