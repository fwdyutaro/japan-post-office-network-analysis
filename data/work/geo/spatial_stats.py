"""Task 6: Global Moran's I and Getis-Ord Gi*.

Spatial weights (stated explicitly because the result depends on them):

  Municipality level -- units are the 1,7xx municipalities that carry
  population mesh, located at the area-weighted centroid of all their
  N03-20260101 polygons.  W is k = 6 nearest neighbours by geodesic distance,
  row-standardised, no self-neighbour.  k-NN rather than contiguity because
  Japan's island municipalities have no land neighbours and a contiguity
  matrix would leave them isolated (undefined I).

  Mesh level -- units are the populated 500 m meshes.  W is k = 8 nearest
  neighbours, row-standardised.

Gi* uses the same neighbour sets but WITH the self term, per Ord & Getis
(1995), and is reported as a z-score.  For a distance variable a positive z is
a cluster of LONG distances (poor access); a negative z is a cluster of SHORT
distances (good access).  Both tails are reported.
"""
from __future__ import annotations
import csv, gzip, json, sys, time
from pathlib import Path
import numpy as np
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).parent))
from geo_util import load_n03, polygon_area_centroid

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_paths import project_root

ROOT = project_root()
GOLD = ROOT / "data/gold/accessibility"
GEO = ROOT / "data/work/geo"
RNG = np.random.default_rng(20260809)


def ecef(lat, lon):
    a = 6378137.0; f = 1 / 298.257222101; e2 = f * (2 - f)
    la = np.radians(lat); lo = np.radians(lon)
    s = np.sin(la); N = a / np.sqrt(1 - e2 * s * s)
    return np.column_stack([N * np.cos(la) * np.cos(lo),
                            N * np.cos(la) * np.sin(lo), N * (1 - e2) * s])


def knn_weights(lat, lon, k):
    tree = cKDTree(ecef(lat, lon))
    _, idx = tree.query(ecef(lat, lon), k=k + 1, workers=-1)
    return idx[:, 1:]                       # drop self


def morans_i(x, nb, perms=999):
    n = len(x)
    z = x - x.mean()
    k = nb.shape[1]
    lag = z[nb].mean(axis=1)                # row-standardised
    S0 = float(n)                           # each row sums to 1
    I = (n / S0) * (z * lag).sum() / (z * z).sum()
    sim = np.empty(perms)
    for p in range(perms):
        zp = RNG.permutation(z)
        sim[p] = (n / S0) * (zp * zp[nb].mean(axis=1)).sum() / (zp * zp).sum()
    EI = -1.0 / (n - 1)
    pseudo = (1 + (np.abs(sim - EI) >= abs(I - EI)).sum()) / (perms + 1)
    return {"n": n, "I": float(I), "E_I": EI, "sim_mean": float(sim.mean()),
            "sim_sd": float(sim.std(ddof=1)),
            "z_perm": float((I - sim.mean()) / sim.std(ddof=1)),
            "p_perm_two_sided": float(pseudo), "k": int(k), "permutations": perms}


def getis_ord_gstar(x, nb):
    """z-scores of Gi* with binary weights over {self} u {k nearest}."""
    n = len(x)
    kk = nb.shape[1] + 1
    idx = np.column_stack([np.arange(n), nb])
    S = x[idx].sum(axis=1)
    xbar = x.mean()
    s = np.sqrt((x * x).mean() - xbar * xbar)
    num = S - xbar * kk
    den = s * np.sqrt((n * kk - kk * kk) / (n - 1))
    return num / den


def load_muni_centroids():
    polys = load_n03()
    acc = {}
    for code, pref, name, pts, parts, _bb in polys:
        c = polygon_area_centroid(pts, parts)
        if c is None:
            continue
        # weight each polygon by |signed area| in degree^2
        a = 0.0
        for i in range(len(parts) - 1):
            ring = pts[parts[i]:parts[i + 1]]
            if len(ring) < 4:
                continue
            x = ring[:, 0]; y = ring[:, 1]
            a += abs((x[:-1] * y[1:] - x[1:] * y[:-1]).sum() / 2.0)
        e = acc.setdefault(code, [0.0, 0.0, 0.0, pref + name])
        e[0] += a; e[1] += c[0] * a; e[2] += c[1] * a
    return {c: (v[1] / v[0], v[2] / v[0], v[3]) for c, v in acc.items() if v[0] > 0}


def main():
    t0 = time.time()
    cent = load_muni_centroids()
    rows = list(csv.DictReader((GOLD / "municipality_access.csv").open(encoding="utf-8")))
    rows = [r for r in rows if r["muni_code"] in cent and int(r["population"]) > 0]
    lat = np.array([cent[r["muni_code"]][0] for r in rows])
    lon = np.array([cent[r["muni_code"]][1] for r in rows])
    print(f"municipal units with population and geometry: {len(rows)}")

    nb6 = knn_weights(lat, lon, 6)
    out = {"weights": {"municipality": "k=6 nearest neighbours on N03 area centroids, "
                                        "row-standardised, no self",
                        "mesh": "k=8 nearest neighbours, row-standardised, no self"},
           "municipality": {}}
    # The primary 2026 variable is the P30-inherited series: the plain ISJ
    # series carries town-centroid coordinates for 7,603 offices, and in rural
    # municipalities that error (kilometres) swamps the real 2013->2026 change.
    vars_ = {
        "mean_dist_2013_effective": np.array([float(r["mean_2013_effective"]) for r in rows]),
        "mean_dist_2026_effective": np.array([float(r["mean_2026_effective_p30inherit"]) for r in rows]),
        "share_gt2km_2026_effective": np.array([float(r["share_gt2km_2026_effective_p30inherit"]) for r in rows]),
        "mean_dist_2026_effective_isj_only": np.array([float(r["mean_2026_effective"]) for r in rows]),
    }
    vars_["delta_mean_dist_2013_2026"] = (vars_["mean_dist_2026_effective"]
                                          - vars_["mean_dist_2013_effective"])
    for name, v in vars_.items():
        out["municipality"][name] = morans_i(v, nb6)
        print(name, out["municipality"][name]["I"], out["municipality"][name]["p_perm_two_sided"])

    # Gi* on the raw distance cannot reach the negative tail: distance is bounded
    # below by 0 and right-skewed, so the smallest attainable z is about
    # -sqrt(k+1)*mean/sd.  The log transform makes both tails attainable, so it is
    # reported alongside.
    g = getis_ord_gstar(vars_["mean_dist_2026_effective"], nb6)
    glog = getis_ord_gstar(np.log1p(vars_["mean_dist_2026_effective"]), nb6)
    gd = getis_ord_gstar(vars_["delta_mean_dist_2013_2026"], nb6)
    with (GOLD / "gistar_municipality.csv").open("w", encoding="utf-8", newline="\n") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["muni_code", "name", "population", "kaso",
                    "mean_dist_2013_effective_m", "mean_dist_2026_effective_m",
                    "delta_m", "gistar_z_dist2026", "gistar_z_logdist2026",
                    "gistar_z_delta"])
        for i, r in enumerate(rows):
            w.writerow([r["muni_code"], cent[r["muni_code"]][2], r["population"], r["kaso"],
                        round(vars_["mean_dist_2013_effective"][i], 1),
                        round(vars_["mean_dist_2026_effective"][i], 1),
                        round(vars_["delta_mean_dist_2013_2026"][i], 1),
                        round(float(g[i]), 4), round(float(glog[i]), 4),
                        round(float(gd[i]), 4)])
    out["municipality_gistar"] = {
        "hot_z_gt_1_96_dist2026": int((g > 1.96).sum()),
        "cold_z_lt_m1_96_dist2026": int((g < -1.96).sum()),
        "hot_z_gt_2_58_dist2026": int((g > 2.58).sum()),
        "cold_z_lt_m2_58_dist2026": int((g < -2.58).sum()),
        "hot_z_gt_1_96_logdist2026": int((glog > 1.96).sum()),
        "cold_z_lt_m1_96_logdist2026": int((glog < -1.96).sum()),
        "hot_z_gt_2_58_logdist2026": int((glog > 2.58).sum()),
        "cold_z_lt_m2_58_logdist2026": int((glog < -2.58).sum()),
        "hot_z_gt_1_96_delta": int((gd > 1.96).sum()),
        "cold_z_lt_m1_96_delta": int((gd < -1.96).sum()),
        "note": "positive z = cluster of LONG nearest-office distance (poor access)",
    }

    # ------------- mesh level
    mlat, mlon, d13, d26, pop = [], [], [], [], []
    with gzip.open(GOLD / "mesh_distances.csv.gz", "rt", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            mlat.append(float(r["lat"])); mlon.append(float(r["lon"]))
            d13.append(float(r["d_2013_effective"]))
            d26.append(float(r["d_2026_effective_p30inherit"]))
            pop.append(int(r["pop"]))
    mlat = np.array(mlat); mlon = np.array(mlon)
    d13 = np.array(d13); d26 = np.array(d26); pop = np.array(pop)
    sel = pop > 0
    print(f"mesh units with population > 0: {sel.sum():,} ({time.time()-t0:.0f}s)")
    nb8 = knn_weights(mlat[sel], mlon[sel], 8)
    out["mesh"] = {
        "n_population_positive": int(sel.sum()),
        "mean_dist_2026_effective": morans_i(d26[sel], nb8, perms=199),
        "delta_2013_2026": morans_i((d26 - d13)[sel], nb8, perms=199),
    }
    gm = getis_ord_gstar(d26[sel], nb8)
    gml = getis_ord_gstar(np.log1p(d26[sel]), nb8)
    out["mesh_gistar"] = {
        "raw_distance": {
            "hot_z_gt_2_58": int((gm > 2.58).sum()),
            "cold_z_lt_m2_58": int((gm < -2.58).sum()),
            "population_in_hot_z_gt_2_58": int(pop[sel][gm > 2.58].sum()),
            "min_z": float(gm.min()), "max_z": float(gm.max()),
            "note": "the negative tail is unreachable for a non-negative right-skewed variable",
        },
        "log1p_distance": {
            "hot_z_gt_2_58": int((gml > 2.58).sum()),
            "cold_z_lt_m2_58": int((gml < -2.58).sum()),
            "population_in_hot_z_gt_2_58": int(pop[sel][gml > 2.58].sum()),
            "population_in_cold_z_lt_m2_58": int(pop[sel][gml < -2.58].sum()),
            "min_z": float(gml.min()), "max_z": float(gml.max()),
        },
    }
    out["mesh"]["log1p_mean_dist_2026_effective"] = morans_i(np.log1p(d26[sel]), nb8, perms=199)
    (GOLD / "spatial_stats.json").write_text(json.dumps(out, ensure_ascii=False, indent=1),
                                             encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
