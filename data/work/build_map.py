"""N03 行政区域界から市区町村単位のコロプレス用 SVG パスを生成する。

geopandas が無いため pyshp で直接ストリーム処理する。
微小リングを落としたうえで、投影後の画素座標で Douglas-Peucker 簡略化をかける。
投影は緯度補正付き正距円筒（全国縮尺の主題図には十分）。
"""
from __future__ import annotations
import csv, json, math, re, sys
from pathlib import Path
import shapefile

from project_paths import project_root

ROOT = project_root()
SHP = ROOT / "data/external/boundary/N03-20260101.shp"
UNITS = ROOT / "data/work/muni/unit_counts.csv"
OUT = ROOT / "data/work/choropleth.json"

NORTH = {"01695", "01696", "01697", "01698", "01699", "01700"}
MAIN = (128.0, 30.5, 146.5, 45.8)   # 本土。沖縄・先島は別枠
OKI = (122.8, 24.0, 131.4, 27.9)

MIN_RING_AREA = 3.0e-5   # deg^2 未満のリングは読み飛ばす
KEEP_EVERY = 2           # 粗間引き（本簡略化は投影後の Douglas-Peucker）
COORD_DP = 1             # 出力画素座標の小数桁
DP_TOL = 0.45            # Douglas-Peucker 許容誤差（画素）
MIN_PX_AREA = 0.15       # これ未満の画素面積のリングは描かない


def dp_simplify(pts, tol):
    """Douglas-Peucker。閉リングは始点・終点を固定して処理する。"""
    if len(pts) < 4:
        return pts
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    t2 = tol * tol
    while stack:
        i, j = stack.pop()
        if j <= i + 1:
            continue
        x1, y1 = pts[i]
        x2, y2 = pts[j]
        dx, dy = x2 - x1, y2 - y1
        den = dx * dx + dy * dy
        best, bi = -1.0, -1
        for k in range(i + 1, j):
            px, py = pts[k]
            if den == 0:
                d2 = (px - x1) ** 2 + (py - y1) ** 2
            else:
                t = ((px - x1) * dx + (py - y1) * dy) / den
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                d2 = (px - x1 - t * dx) ** 2 + (py - y1 - t * dy) ** 2
            if d2 > best:
                best, bi = d2, k
        if best > t2:
            keep[bi] = True
            stack.append((i, bi))
            stack.append((bi, j))
    return [p for p, k in zip(pts, keep) if k]


def shoelace(pts):
    a = 0.0
    for i in range(len(pts) - 1):
        a += pts[i][0] * pts[i + 1][1] - pts[i + 1][0] * pts[i][1]
    return abs(a) / 2


def load_units():
    """legal unit -> metrics（政令市の区は親市に集約済みの表）。"""
    d = {}
    for r in csv.DictReader(UNITS.open(encoding="utf-8")):
        d[r["unit_code"]] = {
            "pref": r["pref"], "label": r["label"],
            "e13": int(r["n2013_eff"]), "e26": int(r["n2026_eff"]),
        }
    return d


def parent_map():
    """5桁コード -> legal unit（政令市の行政区を親市へ集約）。"""
    path = ROOT / "data/external/municipality/municipality_codes.csv"
    rows = list(csv.DictReader(path.open(encoding="utf-8")))
    munis = [r for r in rows if len(r["code"]) == 5 and not r["code"].endswith("000")]
    by_name = {(r["pref_name"], r["city_name"]): r for r in munis}
    tokyo = {f"131{n:02d}" for n in range(1, 24)}
    out = {}
    for r in munis:
        code = r["code"]
        if code in tokyo:
            out[code] = code
            continue
        m = re.match(r"^(.+?市)(.+区)$", r["city_name"])
        p = by_name.get((r["pref_name"], m.group(1))) if m else None
        out[code] = p["code"] if p else code
    return out


def main():
    units = load_units()
    pmap = parent_map()

    sf = shapefile.Reader(str(SHP), encoding="utf-8")
    fields = [f[0] for f in sf.fields[1:]]
    if "N03_007" not in fields:
        print("fields:", fields)
        sys.exit(1)
    ci = fields.index("N03_007")
    print(f"shapes: {len(sf)}  code field idx {ci}")

    geo: dict[str, list] = {}
    n_kept = n_drop = 0
    for i, sr in enumerate(sf.iterShapeRecords()):
        code = str(sr.record[ci] or "")
        if len(code) != 5 or code in NORTH:
            continue
        unit = pmap.get(code, code)
        if unit not in units:
            continue
        shp = sr.shape
        parts = list(shp.parts) + [len(shp.points)]
        for a, b in zip(parts, parts[1:]):
            ring = shp.points[a:b]
            if len(ring) < 4:
                continue
            if shoelace(ring) < MIN_RING_AREA:
                n_drop += 1
                continue
            thin = ring[::KEEP_EVERY]
            if thin[-1] != ring[-1]:
                thin.append(ring[-1])
            if len(thin) >= 4:
                geo.setdefault(unit, []).append(thin)
                n_kept += 1
        if i % 20000 == 0:
            print(f"  {i}...", flush=True)
    print(f"rings kept {n_kept} / dropped(tiny) {n_drop}  units with geometry {len(geo)}")
    missing = [u for u in units if u not in geo]
    print(f"units without geometry: {len(missing)} {[units[u]['label'] for u in missing[:6]]}")

    def project(lon, lat, box, w, h):
        x0, y0, x1, y1 = box
        k = math.cos(math.radians((y0 + y1) / 2))
        s = min(w / ((x1 - x0) * k), h / (y1 - y0))
        return ((lon - x0) * k * s, (y1 - lat) * s)

    def emit(box, w, h):
        paths = {}
        for unit, rings in geo.items():
            segs = []
            for ring in rings:
                if not any(box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3] for p in ring[::7]):
                    continue
                pts = dp_simplify([project(p[0], p[1], box, w, h) for p in ring], DP_TOL)
                r = [(round(x, COORD_DP), round(y, COORD_DP)) for x, y in pts]
                ded = [r[0]]
                for p in r[1:]:
                    if p != ded[-1]:
                        ded.append(p)
                if len(ded) < 4 or shoelace(ded) < MIN_PX_AREA:
                    continue
                segs.append("M" + " ".join(f"{x:g},{y:g}" for x, y in ded) + "Z")
            if segs:
                paths[unit] = "".join(segs)
        return paths

    result = {
        "main": {"w": 700, "h": 640, "paths": emit(MAIN, 700, 640)},
        "oki": {"w": 200, "h": 150, "paths": emit(OKI, 200, 150)},
        "units": {u: {"pref": v["pref"], "label": v["label"], "e13": v["e13"], "e26": v["e26"],
                      "chg": ((v["e26"] - v["e13"]) / v["e13"] * 100) if v["e13"] else None}
                  for u, v in units.items()},
    }
    OUT.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT}  {OUT.stat().st_size/1e6:.2f} MB  main {len(result['main']['paths'])} / oki {len(result['oki']['paths'])}")


if __name__ == "__main__":
    main()
