"""Build the town (大字町丁目) level index from the 47 prefecture ISJ zips.

The 19.0b product carries the 5-digit municipality code, which the street-block
product does not, so this file doubles as the authoritative
(prefecture name, ISJ city name) -> municipality code table.

Output: data/work/geo/isj_oaza.tsv   pref \t city \t code \t oaza \t lat \t lon
        data/work/geo/isj_citycode.tsv  pref \t city \t code \t n_oaza
"""
from __future__ import annotations
import csv, io, json, zipfile
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_paths import project_root

ROOT = project_root()
SRC = ROOT / "data/external/isj"
OUT = ROOT / "data/work/geo"

rows = []
citycode: dict[tuple[str, str], tuple[str, int]] = {}
nfiles = 0
for z in sorted(SRC.glob("*-19.0b.zip")):
    zf = zipfile.ZipFile(z)
    for name in zf.namelist():
        if not name.lower().endswith(".csv"):
            continue
        nfiles += 1
        raw = zf.read(name)
        try:
            txt = raw.decode("cp932")
        except UnicodeDecodeError:
            txt = raw.decode("utf-8", "replace")
        rd = csv.reader(io.StringIO(txt))
        next(rd)
        for r in rd:
            pref, code, city, oaza = r[1], r[2], r[3], r[5]
            try:
                lat = float(r[6]); lon = float(r[7])
            except ValueError:
                continue
            if not (20.0 < lat < 46.5 and 122.0 < lon < 154.0):
                continue
            rows.append((pref, city, code, oaza, lat, lon))
            k = (pref, city)
            c, n = citycode.get(k, (code, 0))
            citycode[k] = (code, n + 1)

with (OUT / "isj_oaza.tsv").open("w", encoding="utf-8", newline="\n") as fh:
    for pref, city, code, oaza, lat, lon in rows:
        fh.write(f"{pref}\t{city}\t{code}\t{oaza}\t{lat:.6f}\t{lon:.6f}\n")
with (OUT / "isj_citycode.tsv").open("w", encoding="utf-8", newline="\n") as fh:
    for (pref, city), (code, n) in sorted(citycode.items(), key=lambda x: x[1][0]):
        fh.write(f"{pref}\t{city}\t{code}\t{n}\n")
print(json.dumps({"csv_files": nfiles, "oaza_rows": len(rows),
                  "cities": len(citycode)}, ensure_ascii=False))
