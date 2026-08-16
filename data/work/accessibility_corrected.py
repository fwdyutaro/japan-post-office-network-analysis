"""レビュー指摘 P1-5 への対応: 座標継承方針を3系列で比較する。

問題: 低精度(town/city)の2026年座標を一律に2013年P30座標へ置換すると、
      実際に移転した局の移動が機械的に消え、変化量を過小評価する。

対応: 同一市区町村・同一名称の一意候補で、現在座標との差が1km以内の局だけを
      P30座標へ継承する。さらに、実施日を独立に確認できた移転だけを継承対象から
      外す。予定日は実施日として扱わない。ISJのみの系列も感度分析として併記する。
"""
from __future__ import annotations
import csv, gzip, io, json, math, re, collections, sys
from pathlib import Path
import pyproj

from project_paths import project_root

ROOT = project_root()
sys.path.insert(0, str(ROOT / "src"))
from postal_bias.artifacts import ArtifactError, commit_file_set, verify_artifact  # noqa: E402
from postal_bias.geo_policy import (build_conservative_inheritance,  # noqa: E402
                                    relative_change_percent,
                                    select_date_confirmed_relocations,
                                    summarize_distance_strata,
                                    summarize_population_access)

W = ROOT / "data/work"
GEO_BUNDLE = ROOT / "data/silver/geocode/2026-06-30"
P30_BUNDLE = ROOT / "data/silver/geocode/2013-11-30"
EVENT_BUNDLE = ROOT / "data/silver/confirmed_events/2026-06-30"
MESH_BUNDLE = ROOT / "data/gold/accessibility"
OUT = W / "accessibility_corrected.json"

CLOSED = re.compile(r"[（(]一時閉鎖[）)]\s*$")
LAEA = pyproj.CRS.from_proj4("+proj=laea +lat_0=36 +lon_0=138 +ellps=GRS80 +units=m +no_defs")
TO_M = pyproj.Transformer.from_crs("EPSG:6668", LAEA, always_xy=True)
GEOD = pyproj.Geod(ellps="GRS80")
CELL = 20_000.0


ANCHOR = "2026-06-30"
P30_DATE = "2013-11-30"


def _sealed_member(bundle: Path, member: str, expected_type: str) -> tuple[bytes, dict]:
    try:
        verification = verify_artifact(bundle, strict_code=True)
        manifest = json.loads((bundle / "artifact.json").read_text(encoding="utf-8"))
        checks = json.loads((bundle / "checksums.json").read_text(encoding="utf-8"))["files"]
        if (not verification.get("ok") or manifest.get("artifact_type") != expected_type or
                manifest.get("qa_error_count") or member not in checks):
            raise ValueError("sealed member rejected")
        data = (bundle / member).read_bytes()
    except (ArtifactError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise RuntimeError("sealed accessibility input rejected") from exc
    return data, {"artifact_id": manifest["artifact_id"],
                  "artifact_type": manifest["artifact_type"],
                  "qa_error_count": manifest.get("qa_error_count", 0), "member": member}


def _jsonl(data: bytes) -> list[dict]:
    return [json.loads(line) for line in data.decode("utf-8").splitlines() if line.strip()]


def relocated_between_p30_and_anchor(events: list[dict]) -> tuple[set[str], dict]:
    """Use no planned date: only an independently observed effective date qualifies."""
    return select_date_confirmed_relocations(
        events, start_exclusive=P30_DATE, end_inclusive=ANCHOR)


def load_p30(rows):
    """名称 -> P30レコード（2013年の実測点）。一意な名称のみ採用。"""
    by_name = collections.defaultdict(list)
    prepared = []
    for source in rows:
        r = dict(source)
        nm = CLOSED.sub("", r.get("name_raw") or "").strip()
        r["_name"] = nm
        r["_closed"] = bool(CLOSED.search(r.get("name_raw") or ""))
        by_name[nm].append(r)
        prepared.append(r)
    uniq = {k: v[0] for k, v in by_name.items() if len(v) == 1}
    return prepared, uniq


def build_series():
    geo_bytes, geo_input = _sealed_member(GEO_BUNDLE, "records.jsonl", "facility_geocode")
    p30_bytes, p30_input = _sealed_member(P30_BUNDLE, "records.jsonl", "facility_geocode")
    event_bytes, event_input = _sealed_member(
        EVENT_BUNDLE, "events.jsonl", "confirmed_change_events")
    geo = _jsonl(geo_bytes)
    p30_rows, _ = load_p30(_jsonl(p30_bytes))
    relocated, relstats = relocated_between_p30_and_anchor(_jsonl(event_bytes))
    inheritance, inheritance_stats = build_conservative_inheritance(
        p30_rows, geo, max_distance_m=1000.0,
        distance=lambda a, b, c, d: GEOD.inv(b, a, d, c)[2])
    print(f"移転イベントの選別: {relstats}")
    print(f"  採用した移転整理番号: {len(relocated)}")

    stats = collections.Counter()
    series = {"isj": [], "inherit_all": [], "corrected": []}
    for r in geo:
        if r.get("lat") is None:
            stats["no_coord"] += 1
            continue
        eff = ((r.get("operating_state") == "yes") if "operating_state" in r
               else r.get("operating") is True)
        isj = (r["lat"], r["lon"])
        acc = r.get("accuracy")
        match = inheritance.get(r.get("official_identifier"))
        inh = (match["lat"], match["lon"]) if match else None

        series["isj"].append((isj, eff))
        # 保守的継承: 同一市区町村・同一名称の一意候補で距離差1km以内だけ置換。
        if acc in ("town", "city") and inh:
            series["inherit_all"].append((inh, eff))
            stats["inherit_all_applied"] += 1
        else:
            series["inherit_all"].append((isj, eff))
        # 補正: 実施日を独立に確認できた2013-12以降の移転だけを除外する。
        if acc in ("town", "city") and inh and r["official_identifier"] not in relocated:
            series["corrected"].append((inh, eff))
            stats["corrected_applied"] += 1
        else:
            series["corrected"].append((isj, eff))
            if acc in ("town", "city") and inh:
                stats["withheld_due_to_relocation"] += 1

    # 2013年（実効 = 一時閉鎖を除く）
    s13 = [((r["lat"], r["lon"]), not r["_closed"]) for r in p30_rows]
    series["p30_2013"] = s13
    inputs = [geo_input, p30_input, event_input]
    return series, stats, relstats, len(relocated), inheritance_stats, inputs


def nearest_distances(points, meshes):
    """meshes の各点から points への最近隣測地線距離 (m)。"""
    buck = collections.defaultdict(list)
    proj = []
    for (lat, lon) in points:
        x, y = TO_M.transform(lon, lat)
        proj.append((x, y, lat, lon))
        buck[(int(x // CELL), int(y // CELL))].append(len(proj) - 1)
    out = []
    for (mlat, mlon, mx, my) in meshes:
        bi, bj = int(mx // CELL), int(my // CELL)
        best = None
        ring = 0
        while True:
            cand = []
            for di in range(-ring, ring + 1):
                for dj in range(-ring, ring + 1):
                    if ring and max(abs(di), abs(dj)) != ring:
                        continue
                    cand.extend(buck.get((bi + di, bj + dj), ()))
            for k in cand:
                x, y, _, _ = proj[k]
                d2 = (x - mx) ** 2 + (y - my) ** 2
                if best is None or d2 < best[0]:
                    best = (d2, k)
            if best is not None and math.sqrt(best[0]) <= ring * CELL:
                break
            ring += 1
            if ring > 60:
                break
        if best is None:
            out.append(None)
            continue
        _, _, plat, plon = proj[best[1]]
        out.append(GEOD.inv(mlon, mlat, plon, plat)[2])
    return out


def main():
    series, stats, relstats, n_reloc, inheritance_stats, input_artifacts = build_series()
    print("座標方針の適用件数:", dict(stats))

    mesh_bytes, mesh_input = _sealed_member(
        MESH_BUNDLE, "mesh_distances.csv.gz", "accessibility_metrics")
    input_artifacts.append(mesh_input)
    meshes = []
    with gzip.GzipFile(fileobj=io.BytesIO(mesh_bytes), mode="rb") as compressed:
        with io.TextIOWrapper(compressed, encoding="utf-8", newline="") as text:
            for p in csv.DictReader(text):
                lat, lon, pop, pop65 = (float(p["lat"]), float(p["lon"]),
                                       int(p["pop"]), int(p["pop65"]))
                if pop <= 0:
                    continue
                x, y = TO_M.transform(lon, lat)
                meshes.append((lat, lon, x, y, pop, pop65, p["muni_code"],
                               int(p["kaso"]), p["density_class"],
                               float(p["d_2013_effective"])))
    print(f"有人メッシュ: {len(meshes):,}  人口 {sum(m[4] for m in meshes):,}")
    mpts = [(m[0], m[1], m[2], m[3]) for m in meshes]
    pops = [m[4] for m in meshes]
    p65 = [m[5] for m in meshes]
    kaso = [m[7] for m in meshes]
    density_class = [m[8] for m in meshes]
    baseline_distances = [m[9] for m in meshes]
    TP, T65 = sum(pops), sum(p65)

    results = {}; distance_results = {}
    for name in ("p30_2013", "inherit_all", "corrected", "isj"):
        pts = [c for c, eff in series[name] if eff]   # 実効系列
        d = nearest_distances(pts, mpts)
        distance_results[name] = d
        results[name] = {"n_offices": len(pts),
                         **summarize_population_access(d, pops, p65)}
        if results[name]["status"] == "computed":
            print(f"{name:14s} 局{len(pts):6,}  平均{results[name]['mean_m']:8.1f}m  "
                  f"≤1km {results[name]['cov'][1000]:6.2f}% 2km超 {results[name]['over2km']:,}  "
                  f"65歳以上2km超 {results[name]['over2km_65']:,}")
        else:
            print(f"{name:14s} not evaluable: {results[name]['reason_codes']}")

    b = results["p30_2013"]
    if b["status"] == "computed":
        for name in ("inherit_all", "corrected", "isj"):
            r = results[name]
            if r["status"] != "computed":
                continue
            r["delta_mean_m"] = r["mean_m"] - b["mean_m"]
            r["delta_cov1km_pt"] = r["cov"][1000] - b["cov"][1000]
            r["delta_over2km"] = r["over2km"] - b["over2km"]
            r["delta_over2km_pct"] = relative_change_percent(
                b["over2km"], r["over2km"])
            r["delta_over2km65_pct"] = relative_change_percent(
                b["over2km_65"], r["over2km_65"])
            if r["delta_over2km_pct"] is None or r["delta_over2km65_pct"] is None:
                r["status"] = "not_evaluable"
                r["reason_codes"] = ["zero_baseline_denominator"]
                for key in ("delta_mean_m", "delta_cov1km_pt", "delta_over2km",
                            "delta_over2km_pct", "delta_over2km65_pct"):
                    r.pop(key, None)

    stratified = {}
    for series_name in ("inherit_all", "corrected", "isj"):
        current = distance_results[series_name]
        stratified[series_name] = summarize_distance_strata(
            baseline_distances, current, pops,
            {"kaso": [v == 1 for v in kaso],
             "non_kaso": [v == 0 for v in kaso],
             "did_like_ge4000": [v == "did_like_ge4000" for v in density_class],
             "mid_1000_4000": [v == "mid_1000_4000" for v in density_class],
             "sparse_lt1000": [v == "sparse_lt1000" for v in density_class]})

    print("\n=== 2013実効 からの変化 ===")
    for name, lab in (("inherit_all", "距離ゲート付き継承"),
                      ("corrected", "実施日確認済み移転のみ継承せず"),
                      ("isj", "ISJのみ（感度上限）")):
        r = results[name]
        if r.get("status") == "computed" and "delta_mean_m" in r:
            print(f"{lab:26s} 平均{r['delta_mean_m']:+7.2f}m  ≤1km{r['delta_cov1km_pt']:+6.3f}pt  "
                  f"2km超{r['delta_over2km']:+9,}人 ({r['delta_over2km_pct']:+.2f}%)  65歳以上{r['delta_over2km65_pct']:+.2f}%")
        else:
            print(f"{lab:26s} not evaluable")

    output = {"status": ("computed" if all(r.get("status") == "computed"
                                             for r in results.values()) and
                                      all(row.get("status") == "computed"
                                          for group in stratified.values()
                                          for row in group.values()) else "not_evaluable"),
              "stats": dict(stats), "relocation_selection": relstats,
              "relocated_identifier_count": n_reloc,
              "inheritance_policy": inheritance_stats,
              "input_artifacts": input_artifacts,
              "anchor": ANCHOR, "p30_date": P30_DATE,
              "results": results, "stratified": stratified,
              "total_pop": TP, "total_65": T65}
    commit_file_set({OUT: lambda stream: (json.dump(output, stream, ensure_ascii=False, indent=1),
                                          stream.write("\n"))}, replace=True)
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
