"""月次アニメーション（仕様書§14.2）のデータを生成する。

レビュー指摘への対応:
  [High] 移転を時点別座標で描く。整理番号ごとに単一座標を使うのをやめ、
         facility_state_history の住所バージョン（valid_from/valid_to）ごとに座標を持つ。
  [High] 履歴アンカー（2026-06-30）より後の月は出さない。
  [Med]  欠損月を検出し、黙って詰めずに明示のギャップフレームにする。

仕様の固定条件:
  計算単位は固定グリッド（市区町村ポリゴンは使わない。§14.2.1）。
  全期間で 格子・投影法・検索半径・色階級・地図範囲 を固定（§14.2.3）。

指標: セル中心から半径20km以内の届出上の局数の、基準月比の変化率。
投影: ランベルト正積方位図法（+proj=laea +lat_0=36 +lon_0=138）。
"""
from __future__ import annotations
import gzip, json, math, re, sys, collections
from pathlib import Path
import pyproj

from project_paths import project_root

ROOT = project_root()
W = ROOT / "data/work"
sys.path.insert(0, str(W / "geo"))
import geocode_2026 as G  # noqa: E402  (helpers + prebuilt ISJ indices)

SNAP = ROOT / "data/silver/monthly_snapshots/2026-06-30/formal"
SNAPMETA = ROOT / "data/silver/monthly_snapshots/2026-06-30/metadata.json"
GEO = ROOT / "data/silver/geocode/2026-06-30/records.jsonl"
P30 = ROOT / "data/silver/p30/2013-11-30/records.jsonl"
HIST = ROOT / "data/silver/history/2026-06-30/facility_state_history.jsonl"
MESH = ROOT / "data/gold/accessibility/mesh_distances.csv.gz"
OUT = W / "animation_data.json"

ANCHOR_MONTH = "2026-06"   # 履歴アンカー 2026-06-30。これより後は出さない。
CELL = 10_000.0
RADIUS = 20_000.0
CLOSED = re.compile(r"[（(]一時閉鎖[）)]\s*$")

LAEA = pyproj.CRS.from_proj4("+proj=laea +lat_0=36 +lon_0=138 +ellps=GRS80 +units=m +no_defs")
TO_M = pyproj.Transformer.from_crs("EPSG:6668", LAEA, always_xy=True)


# ---------------------------------------------------------------- coordinates
def geocode_many(addresses: set[str]) -> dict[str, tuple]:
    """住所文字列 -> (lat, lon, accuracy)。10kmセルなので町丁目精度で足りる。"""
    town, oaza, citypoint, citycode = G.load_indices()
    by_pref = G.build_city_matcher(citycode)
    out = {}
    for raw in addresses:
        a = G.norm_addr(raw)
        pref, city, rest, err = G.resolve_city(a, by_pref)
        if not city:
            continue
        tdict, odict = town.get((pref, city), {}), oaza.get((pref, city), {})
        head, nums = G.split_head_nums(G.keyfold(rest))
        hk = G.keyfold(G.kanjify_chome(head))
        hit = None
        for tk, _blocks in G.town_candidates(head, nums):
            for d in (tdict, odict):
                if tk in d:
                    hit = (d[tk], "town"); break
            if hit:
                break
        if hit is None:
            for d in (tdict, odict):
                s = G.longest_prefix(hk, d) or G.longest_suffix(hk, d)
                if s:
                    hit = (d[s], "town_approx"); break
        if hit is None:
            cp = citypoint.get((pref, city))
            if cp:
                out[raw] = (float(cp[0]), float(cp[1]), "city")
            continue
        e, acc = hit
        # 索引の値は (名称, 緯度, 経度)。座標は index 1,2。
        out[raw] = (float(e[1]), float(e[2]), acc)
    return out


def build_versions():
    """整理番号 -> [(valid_from, valid_to, lat, lon, src)] を時系列順で返す。"""
    cur = {}
    for line in GEO.open(encoding="utf-8"):
        r = json.loads(line)
        if r.get("lat") is not None:
            cur[(r["official_identifier"], r["address"])] = (r["lat"], r["lon"], "isj_" + str(r.get("accuracy")))

    p30 = collections.defaultdict(list)
    for line in P30.open(encoding="utf-8"):
        r = json.loads(line)
        p30[CLOSED.sub("", r.get("name_raw") or "").strip()].append(r)
    p30u = {k: v[0] for k, v in p30.items() if len(v) == 1}

    versions = collections.defaultdict(list)
    need: set[str] = set()
    for line in HIST.open(encoding="utf-8"):
        r = json.loads(line)
        st = r.get("state") or {}
        i, ad = st.get("official_identifier"), st.get("address")
        if not i or not ad:
            continue
        versions[i].append({"vf": r.get("valid_from"), "vt": r.get("valid_to"),
                            "addr": ad, "name": st.get("name")})
        if (i, ad) not in cur:
            need.add(ad)
    print(f"住所バージョン {sum(len(v) for v in versions.values()):,} / 整理番号 {len(versions):,}")
    print(f"追加ジオコーディングが必要な住所: {len(need):,}")

    geo = geocode_many(need) if need else {}
    print(f"  うち解決: {len(geo):,}")

    src_count = collections.Counter()
    for i, vs in versions.items():
        for v in vs:
            key = (i, v["addr"])
            if key in cur:
                lat, lon, s = cur[key]
            elif v["addr"] in geo:
                lat, lon, s = geo[v["addr"]]
            else:
                p = p30u.get(v["name"])
                if p:
                    lat, lon, s = p["latitude"], p["longitude"], "p30_name"
                else:
                    v["lat"] = v["lon"] = None
                    v["src"] = "unresolved"
                    src_count["unresolved"] += 1
                    continue
            v["lat"], v["lon"], v["src"] = lat, lon, s
            src_count[s.split("_")[0]] += 1
        vs.sort(key=lambda x: (x["vf"] or "0000-00-00"))
    print("座標の出所:", dict(src_count))
    return versions, src_count


def coord_at(vs, month_end: str):
    """月末時点で有効な住所バージョンの座標。"""
    best = None
    for v in vs:
        vf, vt = v["vf"], v["vt"]
        if vf and vf > month_end:
            continue
        if vt and vt <= month_end:
            continue
        best = v
        break
    if best is None:                    # 区間が閉じている等で該当なし → 直近の過去版
        past = [v for v in vs if not v["vf"] or v["vf"] <= month_end]
        best = past[-1] if past else vs[0]
    return (best["lat"], best["lon"]) if best.get("lat") is not None else None


def main():
    versions, src_count = build_versions()

    # --- 月リスト（アンカーまで、欠損検出つき） -----------------------------
    have = sorted(p.stem for p in SNAP.glob("*.jsonl"))
    have = [m for m in have if m <= ANCHOR_MONTH]
    y0, m0 = map(int, have[0].split("-"))
    y1, m1 = map(int, have[-1].split("-"))
    expected, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        expected.append(f"{y}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    missing = sorted(set(expected) - set(have))
    print(f"月: 期待{len(expected)} 実在{len(have)} 欠損{len(missing)} {missing[:6]}")
    print(f"アンカー {ANCHOR_MONTH} より後は出力しない")

    import calendar
    def month_end(mm):
        y, m = map(int, mm.split("-"))
        return f"{mm}-{calendar.monthrange(y, m)[1]:02d}"

    # --- 固定グリッド（人口メッシュのある区画のみ） -------------------------
    cells: dict[tuple[int, int], int] = {}
    with gzip.open(MESH, "rt", encoding="utf-8") as f:
        next(f)
        for line in f:
            p = line.split(",", 4)
            x, y_ = TO_M.transform(float(p[2]), float(p[1]))
            k = (int(math.floor(x / CELL)), int(math.floor(y_ / CELL)))
            cells[k] = cells.get(k, 0) + int(p[3])
    keys = sorted(cells)
    centers = [((k[0] + 0.5) * CELL, (k[1] + 0.5) * CELL) for k in keys]
    reach = int(math.ceil(RADIUS / CELL))
    print(f"固定格子セル数: {len(keys):,}")

    counts_by_month, national, moved = [], [], []
    prev_pos: dict[str, tuple] = {}
    for mi, mm in enumerate(expected):
        if mm in missing:
            counts_by_month.append(None)
            national.append({"m": mm, "total": None, "placed": None, "nocoord": None, "missing": True})
            moved.append(0)
            continue
        me = month_end(mm)
        pts, n_tot, n_no = [], 0, 0
        pos_now = {}
        for line in (SNAP / f"{mm}.jsonl").open(encoding="utf-8"):
            r = json.loads(line)
            n_tot += 1
            vs = versions.get(r["i"])
            c = coord_at(vs, me) if vs else None
            if c is None:
                n_no += 1
                continue
            x, y_ = TO_M.transform(c[1], c[0])
            pts.append((x, y_))
            pos_now[r["i"]] = (round(x), round(y_))
        moved.append(sum(1 for k, v in pos_now.items() if k in prev_pos and prev_pos[k] != v))
        prev_pos = pos_now

        buck = collections.defaultdict(list)
        for x, y_ in pts:
            buck[(int(math.floor(x / CELL)), int(math.floor(y_ / CELL)))].append((x, y_))
        r2 = RADIUS * RADIUS
        vals = []
        for (cx, cy), k in zip(centers, keys):
            n = 0
            for dx in range(-reach, reach + 1):
                for dy in range(-reach, reach + 1):
                    for (x, y_) in buck.get((k[0] + dx, k[1] + dy), ()):
                        if (x - cx) ** 2 + (y_ - cy) ** 2 <= r2:
                            n += 1
            vals.append(n)
        counts_by_month.append(vals)
        national.append({"m": mm, "total": n_tot, "placed": len(pts), "nocoord": n_no, "missing": False})
        if mi % 24 == 0:
            print(f"  {mm}: 局{n_tot} 配置{len(pts)} 座標なし{n_no} 位置変化{moved[-1]}", flush=True)

    base = counts_by_month[0]
    if base is None:
        raise SystemExit("基準月が欠損しているため比較できない")

    BINS = [(-100, -20), (-20, -10), (-10, -5), (-5, -0.001), (-0.001, 0.001), (0.001, 5), (5, 1000)]
    def cls(pct):
        if pct is None:
            return 7
        for i, (lo, hi) in enumerate(BINS):
            if lo < pct <= hi:
                return i
        return 6 if pct > 0 else 0

    frames = []
    for vals in counts_by_month:
        if vals is None:
            frames.append("8" * len(keys))      # 欠損月は専用階級
            continue
        frames.append("".join(str(cls(((v - b) / b * 100) if b else None)) for v, b in zip(vals, base)))

    xs = [c[0] for c in centers]; ys = [c[1] for c in centers]
    x0, x1, y0_, y1_ = min(xs) - CELL, max(xs) + CELL, min(ys) - CELL, max(ys) + CELL
    Wpx = 720
    scale = Wpx / (x1 - x0)
    Hpx = int((y1_ - y0_) * scale)
    px = [[round((cx - x0) * scale, 1), round((y1_ - cy) * scale, 1)] for cx, cy in centers]

    snapmeta = json.loads(SNAPMETA.read_text(encoding="utf-8"))
    data = {
        "months": expected,
        "frames": frames,
        "cells": px,
        "cell_px": round(CELL * scale, 2),
        "w": Wpx, "h": Hpx,
        "national": national,
        "position_changes": moved,
        "baseline_month": expected[0],
        "anchor_month": ANCHOR_MONTH,
        "missing_months": missing,
        "params": {"cell_m": CELL, "radius_m": RADIUS,
                   "projection": "LAEA lat_0=36 lon_0=138 GRS80",
                   "metric": f"半径20km以内の届出上の局数の、{expected[0]}比の変化率",
                   "effective_date_basis": snapmeta.get("effective_date_basis", "planned"),
                   "series": "formal",
                   "bins": ["≤−20%", "−20〜−10%", "−10〜−5%", "−5〜0%", "変化なし",
                            "0〜+5%", ">+5%", "基準月に局なし", "データ欠損"]},
        "coord_sources": dict(src_count),
        "source_snapshot_metadata": {k: snapmeta.get(k) for k in
                                     ("anchor_date", "history_version", "entity_count", "effective_date_basis")},
    }
    OUT.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"\nwrote {OUT}  {OUT.stat().st_size/1e6:.2f} MB")
    print(f"位置が動いた局の総数（月次合計）: {sum(moved):,}")
    print(f"最終月 {expected[-1]}: 局{national[-1]['total']} 座標なし{national[-1]['nocoord']}")


if __name__ == "__main__":
    main()
