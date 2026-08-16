"""Assemble the national 500 m mesh population table from the e-Stat downloads.

Columns kept:  mesh code, centre lat/lon (computed from the code, JGD2011),
population total, 0-14, 15-64, 65+, 75+, households.
Each mesh centre is assigned to a municipality by point-in-polygon against
N03-20260101, then tagged 過疎 / 非過疎.
Output: data/work/geo/mesh500.csv
"""
from __future__ import annotations
import csv, io, json, sys, time, zipfile
from pathlib import Path
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from geo_util import mesh_center, load_n03, assign_points_to_municipality

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_paths import project_root

ROOT = project_root()
SRC = ROOT / "data/work/estat/mesh500"
GEO = ROOT / "data/work/geo"
KASO = ROOT / "data/external/kaso/kaso_municipalities.csv"

# T001141 column positions (see the header shipped with the product)
COLS = {"pop": "T001141001", "p0_14": "T001141004", "p15_64": "T001141010",
        "p65": "T001141022", "p75": "T001141025", "households": "T001141048"}


def load_pref(path: Path):
    zf = zipfile.ZipFile(path)
    name = [n for n in zf.namelist() if n.lower().endswith(".txt")][0]
    raw = zf.read(name)
    try:
        txt = raw.decode("cp932")
    except UnicodeDecodeError:
        txt = raw.decode("utf-8", "replace")
    rd = csv.reader(io.StringIO(txt))
    hdr = next(rd)
    next(rd)                                     # japanese label row
    ix = {k: hdr.index(v) for k, v in COLS.items() if v in hdr}
    key = hdr.index("KEY_CODE")
    out = []
    for row in rd:
        if not row or not row[key].strip():
            continue
        code = row[key].strip()
        vals = {}
        for k, j in ix.items():
            v = row[j].strip()
            vals[k] = int(v) if v.lstrip("-").isdigit() else 0
        out.append((code, vals))
    return out


def main():
    t0 = time.time()
    rows = []
    seen = set()
    files = sorted(SRC.glob("tblT001141H*.zip"))
    for p in files:
        for code, vals in load_pref(p):
            if code in seen:
                continue
            seen.add(code)
            rows.append((code, vals))
    print(f"{len(files)} prefecture files, {len(rows):,} mesh records ({time.time()-t0:.0f}s)")

    codes = [r[0] for r in rows]
    cen = [mesh_center(c) for c in codes]
    lats = np.array([c[0] for c in cen]); lons = np.array([c[1] for c in cen])
    pops = np.array([r[1].get("pop", 0) for r in rows])
    print(f"total population on the mesh: {pops.sum():,}")
    print(f"mesh code lengths: {sorted({len(c) for c in codes})}")

    polys = load_n03()
    muni = assign_points_to_municipality(lats, lons, polys)
    print(f"municipality assigned: {(muni != '').sum():,} / {len(muni):,} "
          f"({time.time()-t0:.0f}s)")
    unass_pop = pops[muni == ""].sum()
    print(f"population in meshes whose centre falls in no N03 polygon: {unass_pop:,}")

    # Coastal / reclaimed / 境界未定 cells: fall back to the municipality of the
    # nearest mesh centre that a polygon did claim.
    from scipy.spatial import cKDTree
    src = np.full(len(muni), "polygon", dtype=object)
    miss = np.where(muni == "")[0]
    have = np.where(muni != "")[0]
    if len(miss) and len(have):
        def ecef(la, lo):
            a = 6378137.0; f = 1 / 298.257222101; e2 = f * (2 - f)
            la = np.radians(la); lo = np.radians(lo); s = np.sin(la)
            N = a / np.sqrt(1 - e2 * s * s)
            return np.column_stack([N * np.cos(la) * np.cos(lo),
                                    N * np.cos(la) * np.sin(lo), N * (1 - e2) * s])
        tree = cKDTree(ecef(lats[have], lons[have]))
        _, j = tree.query(ecef(lats[miss], lons[miss]), workers=-1)
        for a, b in zip(miss, have[j]):
            muni[a] = muni[b]
            src[a] = "nearest_assigned_mesh"
    n_fb = int((src == "nearest_assigned_mesh").sum())
    print(f"fallback assignments: {n_fb:,} meshes, {int(pops[src=='nearest_assigned_mesh'].sum()):,} people")

    kaso = set()
    with KASO.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            kaso.add(r["code"].strip())
    # designated-city wards inherit their parent city's 過疎 status only if the
    # ward code itself is listed; the kaso table is at municipality level.
    with (GEO / "mesh500.csv").open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["mesh", "lat", "lon", "pop", "p0_14", "p15_64", "p65", "p75",
                    "households", "muni_code", "muni_source", "kaso"])
        for i, (code, vals) in enumerate(rows):
            w.writerow([code, f"{lats[i]:.6f}", f"{lons[i]:.6f}",
                        vals.get("pop", 0), vals.get("p0_14", 0), vals.get("p15_64", 0),
                        vals.get("p65", 0), vals.get("p75", 0), vals.get("households", 0),
                        muni[i], src[i], 1 if muni[i] in kaso else 0])
    print(f"wrote {GEO/'mesh500.csv'} ({time.time()-t0:.0f}s)")
    meta = {"source": "e-Stat 統計GIS 国勢調査2020 4次メッシュ(500m) 人口及び世帯 JGD2011 statsId=T001141",
            "prefecture_files": len(files), "mesh_records": len(rows),
            "population_total": int(pops.sum()),
            "population_in_meshes_without_polygon_hit": int(unass_pop),
            "meshes_assigned_by_nearest_mesh_fallback": n_fb}
    (GEO / "mesh500_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1),
                                           encoding="utf-8")


if __name__ == "__main__":
    main()
