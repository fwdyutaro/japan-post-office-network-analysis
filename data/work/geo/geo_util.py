"""Shared geometry helpers: N03 municipality polygons, point-in-polygon,
mesh-code arithmetic, geodesic nearest-neighbour search."""
from __future__ import annotations
import collections
from pathlib import Path

import numpy as np
import shapefile
from pyproj import Geod

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_paths import project_root

ROOT = project_root()
N03 = ROOT / "data/external/boundary/N03-20260101.shp"
GEOD = Geod(ellps="GRS80")


# --------------------------------------------------------------- mesh codes
def mesh_center(code: str):
    """Centre of a standard grid-square code (8=1 km, 9=500 m, 10=250 m)."""
    lat = int(code[0:2]) / 1.5
    lon = int(code[2:4]) + 100.0
    dlat, dlon = 2 / 3, 1.0
    if len(code) >= 6:
        lat += int(code[4]) * dlat / 8
        lon += int(code[5]) * dlon / 8
        dlat, dlon = dlat / 8, dlon / 8
    if len(code) >= 8:
        lat += int(code[6]) * dlat / 10
        lon += int(code[7]) * dlon / 10
        dlat, dlon = dlat / 10, dlon / 10
    for i in (8, 9):
        if len(code) >= i + 1:
            q = int(code[i]) - 1
            lat += (q // 2) * dlat / 2
            lon += (q % 2) * dlon / 2
            dlat, dlon = dlat / 2, dlon / 2
    return lat + dlat / 2, lon + dlon / 2, dlat, dlon


# --------------------------------------------------------------- N03 polygons
def load_n03():
    """[(code, pref, name, lonlat ndarray, part offsets, bbox)]"""
    r = shapefile.Reader(str(N03), encoding="utf-8")
    polys = []
    for sh, rec in zip(r.iterShapes(), r.iterRecords()):
        code = rec["N03_007"]
        if not code:
            continue
        pts = np.asarray(sh.points, dtype=np.float64)
        if len(pts) < 4:
            continue
        parts = list(sh.parts) + [len(pts)]
        name = (rec["N03_003"] or "") + (rec["N03_004"] or "")
        polys.append((code, rec["N03_001"], name, pts, parts,
                      (pts[:, 0].min(), pts[:, 1].min(), pts[:, 0].max(), pts[:, 1].max())))
    return polys


def _ring_contains(px, py, ring):
    """Vectorised even-odd test of many points against one ring."""
    x1 = ring[:-1, 0]; y1 = ring[:-1, 1]
    x2 = ring[1:, 0];  y2 = ring[1:, 1]
    inside = np.zeros(px.shape, dtype=bool)
    for a in range(len(x1)):
        cond = ((y1[a] > py) != (y2[a] > py))
        if not cond.any():
            continue
        xin = (x2[a] - x1[a]) * (py - y1[a]) / (y2[a] - y1[a]) + x1[a]
        inside ^= cond & (px < xin)
    return inside


def assign_points_to_municipality(lats, lons, polys, cell=0.1):
    """Return an array of municipality codes ('' where no polygon contains
    the point).  Inverted loop: for each polygon, test only the points whose
    bounding-box cell overlaps it."""
    lats = np.asarray(lats); lons = np.asarray(lons)
    out = np.full(len(lats), "", dtype=object)
    grid = collections.defaultdict(list)
    for i in range(len(lats)):
        grid[(int(lons[i] / cell), int(lats[i] / cell))].append(i)
    for code, _pref, _name, pts, parts, (x0, y0, x1, y1) in polys:
        cand = []
        for gx in range(int(x0 / cell), int(x1 / cell) + 1):
            for gy in range(int(y0 / cell), int(y1 / cell) + 1):
                cand.extend(grid.get((gx, gy), ()))
        if not cand:
            continue
        cand = np.fromiter((i for i in cand if out[i] == ""), dtype=np.int64)
        if cand.size == 0:
            continue
        px = lons[cand]; py = lats[cand]
        box = (px >= x0) & (px <= x1) & (py >= y0) & (py <= y1)
        if not box.any():
            continue
        cand = cand[box]; px = px[box]; py = py[box]
        inside = np.zeros(cand.shape, dtype=bool)
        for a in range(len(parts) - 1):
            ring = pts[parts[a]:parts[a + 1]]
            if len(ring) < 4:
                continue
            inside ^= _ring_contains(px, py, ring)
        for i in cand[inside]:
            out[i] = code
    return out


def polygon_area_centroid(pts, parts):
    """Area-weighted centroid of a (multi-)ring polygon in degrees."""
    cx = cy = a_tot = 0.0
    for a in range(len(parts) - 1):
        ring = pts[parts[a]:parts[a + 1]]
        if len(ring) < 4:
            continue
        x = ring[:, 0]; y = ring[:, 1]
        cross = x[:-1] * y[1:] - x[1:] * y[:-1]
        A = cross.sum() / 2.0
        if A == 0:
            continue
        cx += ((x[:-1] + x[1:]) * cross).sum() / 6.0
        cy += ((y[:-1] + y[1:]) * cross).sum() / 6.0
        a_tot += A
    if a_tot == 0:
        return None
    return cy / a_tot, cx / a_tot          # lat, lon


# --------------------------------------------------------------- nearest office
def nearest_geodesic(qlat, qlon, olat, olon, cell=0.05):
    """Nearest office for every query point.

    Candidates are gathered from an expanding ring of a lat/lon bucket grid;
    the final distance is the true geodesic distance (pyproj.Geod), never a
    projected one.  The bucket search radius is converted to metres with a
    conservative bound so the answer is exact, not approximate.
    """
    qlat = np.asarray(qlat); qlon = np.asarray(qlon)
    olat = np.asarray(olat); olon = np.asarray(olon)
    grid = collections.defaultdict(list)
    for j in range(len(olat)):
        grid[(int(olon[j] / cell), int(olat[j] / cell))].append(j)
    for k in grid:
        grid[k] = np.array(grid[k], dtype=np.int64)

    best = np.full(len(qlat), np.inf)
    idx = np.full(len(qlat), -1, dtype=np.int64)
    M_PER_DEG_LAT = 110574.0
    for i in range(len(qlat)):
        gx = int(qlon[i] / cell); gy = int(qlat[i] / cell)
        # metres per degree of longitude at this latitude (lower bound)
        mlon = 111320.0 * np.cos(np.radians(min(abs(qlat[i]) + cell, 89.0)))
        r = 1
        found = -1; fd = np.inf
        while True:
            cand = []
            for dx in range(-r, r + 1):
                for dy in range(-r, r + 1):
                    c = grid.get((gx + dx, gy + dy))
                    if c is not None:
                        cand.append(c)
            if cand:
                c = np.concatenate(cand)
                _, _, d = GEOD.inv(np.full(c.shape, qlon[i]), np.full(c.shape, qlat[i]),
                                   olon[c], olat[c])
                m = d.argmin()
                if d[m] < fd:
                    fd = d[m]; found = c[m]
            # a hit is only certain once the guaranteed-covered radius exceeds it
            guaranteed = (r - 1) * cell * min(M_PER_DEG_LAT, mlon)
            if found >= 0 and fd <= guaranteed:
                break
            r += 1
            if r > 400:
                break
        best[i] = fd; idx[i] = found
    return best, idx
