"""ISJ street-block file, pass A.

Stream the 19.6M-row street-block position reference file once and build a
compact town (大字・丁目) level index:  (pref, city, oaza) -> mean lat/lon, n.
Per the product documentation the history flags mean 1=新規作成, 2=名称変更,
3=削除, 0=変更なし.  Rows whose 更新後履歴フラグ is 3 are records deleted in
the current edition and are dropped (22,377 of 19,589,219).

Output: data/work/geo/isj_town.tsv  (tab separated, UTF-8)
        data/work/geo/isj_city.tsv
        data/work/geo/isj_pass_a_stats.json
"""
from __future__ import annotations
import csv, json, sys, time
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_paths import project_root

ROOT = project_root()
SRC = ROOT / "data/external/isj/isj_blocks_all.csv"
OUT = ROOT / "data/work/geo"
OUT.mkdir(parents=True, exist_ok=True)

town: dict[tuple[str, str, str], list] = {}
city: dict[tuple[str, str], list] = {}
stats = {"rows": 0, "dropped_deleted": 0, "dropped_badcoord": 0, "kept": 0}

t0 = time.time()
with SRC.open(encoding="utf-8", newline="", buffering=1 << 22) as fh:
    rd = csv.reader(fh)
    next(rd)
    for row in rd:
        stats["rows"] += 1
        if row[13] == "3":               # 更新後履歴フラグ: 3 = 削除
            stats["dropped_deleted"] += 1
            continue
        try:
            lat = float(row[8]); lon = float(row[9])
        except ValueError:
            stats["dropped_badcoord"] += 1
            continue
        if not (20.0 < lat < 46.5 and 122.0 < lon < 154.0):
            stats["dropped_badcoord"] += 1
            continue
        stats["kept"] += 1
        k = (row[0], row[1], row[2])
        e = town.get(k)
        if e is None:
            town[k] = [1, lat, lon]
        else:
            e[0] += 1; e[1] += lat; e[2] += lon
        k2 = (row[0], row[1])
        e2 = city.get(k2)
        if e2 is None:
            city[k2] = [1, lat, lon]
        else:
            e2[0] += 1; e2[1] += lat; e2[2] += lon
        if stats["rows"] % 2_000_000 == 0:
            print(f"  {stats['rows']:,} rows  {time.time()-t0:.0f}s  towns={len(town):,}",
                  file=sys.stderr, flush=True)

stats["distinct_town"] = len(town)
stats["distinct_city"] = len(city)
stats["elapsed_s"] = round(time.time() - t0, 1)

with (OUT / "isj_town.tsv").open("w", encoding="utf-8", newline="\n") as fh:
    for (p, c, o), (n, sla, slo) in town.items():
        fh.write(f"{p}\t{c}\t{o}\t{n}\t{sla/n:.6f}\t{slo/n:.6f}\n")
with (OUT / "isj_city.tsv").open("w", encoding="utf-8", newline="\n") as fh:
    for (p, c), (n, sla, slo) in city.items():
        fh.write(f"{p}\t{c}\t{n}\t{sla/n:.6f}\t{slo/n:.6f}\n")
(OUT / "isj_pass_a_stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
print(json.dumps(stats, ensure_ascii=False))
