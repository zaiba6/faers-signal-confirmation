"""Export the raw FAERS tables out of the interim DuckDB into portable parquet.

Run: /usr/local/bin/python3 scripts/archive_raw_faers.py [DEST]
     DEST defaults to ~/Developer/faers-raw-archive

Why this exists. data/raw/ no longer holds the FAERS source tables: the October cleanup
removed them and they are not anywhere in the working tree. data/interim/faers_raw.duckdb
is the only local copy of the raw data, and it also still holds RPSR / THER / INDI, which
the raw files did not keep. See the Cleanup section of DECISIONS.md.

Why parquet rather than copying the .duckdb file. A DuckDB database is tied to its
storage format version, so a file written by one release can stop opening on a later one.
That is a poor property for the only copy of your source data. Parquet is a stable,
widely readable format: pandas, duckdb, polars, R and Spark all read it without this
project's code, and zstd keeps it small.

demo_clean is deliberately NOT exported: it is derived from demo by scripts/clean_demo.py
and is rebuildable. Everything else is source.
"""
import sys, json, time
from pathlib import Path
import duckdb

SRC = Path(__file__).resolve().parents[1] / "data/interim/faers_raw.duckdb"
DEST = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.home() / "Developer/faers-raw-archive"
SKIP = {"demo_clean"}

if __name__ == "__main__":
    DEST.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(SRC), read_only=True)
    tables = [r[0] for r in con.execute(
        "select table_name from duckdb_tables() order by table_name").fetchall()]
    manifest = {"source": str(SRC), "duckdb_version": duckdb.__version__,
                "exported_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tables": {}}
    for t in tables:
        if t in SKIP:
            print(f"  skip {t} (derived, rebuildable)", flush=True)
            continue
        out = DEST / f"{t}.parquet"
        con.execute(f"COPY (SELECT * FROM {t}) TO '{out}' (FORMAT PARQUET, COMPRESSION zstd)")
        rows = con.execute(f"select count(*) from {t}").fetchone()[0]
        mb = out.stat().st_size / 1048576
        manifest["tables"][t] = {"rows": rows, "mb": round(mb, 1)}
        print(f"  {t:12s} {rows:>12,d} rows -> {mb:8.1f} MB", flush=True)
    (DEST / "MANIFEST.json").write_text(json.dumps(manifest, indent=1))
    total = sum(v["mb"] for v in manifest["tables"].values())
    print(f"\n{len(manifest['tables'])} tables, {total/1024:.2f} GB in {DEST}")
