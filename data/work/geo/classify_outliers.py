"""Classify the >1 km 2013/2026 coordinate discrepancies.

Discriminator: does each coordinate fall inside the N03 polygon of the
municipality its own record claims?  A point outside its own municipality is
wrong regardless of which side produced it.
"""
from __future__ import annotations
import csv, json
from pathlib import Path
import numpy as np
import sys
sys.path.insert(0, str(Path(__file__).parent))
from geo_util import load_n03, assign_points_to_municipality

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_paths import project_root

ROOT = project_root()
GEO = ROOT / "data/work/geo"

rows = list(csv.DictReader((GEO / "qa_match.tsv").open(encoding="utf-8"), delimiter="\t"))
for r in rows:
    r["dist_m"] = float(r["dist_m"])
out = [r for r in rows if r["dist_m"] > 1000]
print(f"matched={len(rows)}  >1km={len(out)}")

polys = load_n03()
n03_codes = {p[0] for p in polys}

lats = [float(r["lat26"]) for r in out] + [float(r["lat13"]) for r in out]
lons = [float(r["lon26"]) for r in out] + [float(r["lon13"]) for r in out]
assigned = assign_points_to_municipality(np.array(lats), np.array(lons), polys)
n = len(out)
for i, r in enumerate(out):
    r["muni_of_26"] = assigned[i]
    r["muni_of_13"] = assigned[n + i]
    own = r["muni"]
    r["code_in_n03"] = own in n03_codes
    r["ok26"] = (r["muni_of_26"] == own)
    r["ok13"] = (r["muni_of_13"] == own)
    if not r["code_in_n03"]:
        r["verdict"] = "municipality_code_absent_from_2026_boundaries"
    elif r["ok26"] and not r["ok13"]:
        r["verdict"] = "p30_2013_point_outside_its_own_municipality"
    elif r["ok13"] and not r["ok26"]:
        r["verdict"] = "geocoded_2026_point_outside_its_own_municipality"
    elif not r["ok13"] and not r["ok26"]:
        r["verdict"] = "both_outside_own_municipality"
    else:
        r["verdict"] = "both_inside_same_municipality_indeterminate"

import collections
for acc in ("block", "town", "city"):
    c = collections.Counter(r["verdict"] for r in out if r["accuracy"] == acc)
    print(acc, sum(c.values()), dict(c))

with (GEO / "qa_outliers_classified.tsv").open("w", encoding="utf-8", newline="\n") as fh:
    w = csv.writer(fh, delimiter="\t", lineterminator="\n")
    w.writerow(["id", "name", "pref", "muni", "accuracy", "source", "dist_m",
                "lat26", "lon26", "muni_of_26", "lat13", "lon13", "muni_of_13",
                "verdict", "addr"])
    for r in sorted(out, key=lambda x: -x["dist_m"]):
        w.writerow([r["id"], r["name"], r["pref"], r["muni"], r["accuracy"], r["source"],
                    round(r["dist_m"], 1), r["lat26"], r["lon26"], r["muni_of_26"],
                    r["lat13"], r["lon13"], r["muni_of_13"], r["verdict"], r["addr"]])

summary = {"n_matched": len(rows), "n_over_1km": len(out),
           "by_accuracy": {a: dict(collections.Counter(
               r["verdict"] for r in out if r["accuracy"] == a))
               for a in ("block", "town", "city")}}
(GEO / "qa_outlier_verdicts.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                              encoding="utf-8")
print("\n--- block-accuracy outliers, individually ---")
for r in sorted((x for x in out if x["accuracy"] == "block"), key=lambda x: -x["dist_m"]):
    print(f"{r['dist_m']:8.0f}m  {r['verdict']:52s} {r['pref']}{r['name']}  {r['addr']}")
