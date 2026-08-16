"""Task 5: mesh-level accessibility.

For every 500 m population mesh centre, the geodesic distance to the nearest
post office, for six series (2013/2026 x formal/effective, plus a
block-accuracy-only sensitivity cut for 2026).

Nearest neighbour: k=8 candidates from a KD-tree over ECEF coordinates, then
the exact geodesic distance (pyproj.Geod, GRS80) for each candidate.  EPSG:3857
is never used.

Inputs are the two sealed geocode bundles.  They are verified with
``verify_artifact`` and checked against the coordinate contract before a single
row is read: an unsealed, tampered or QA-failing bundle, a CRS other than
EPSG:6668, a coordinate outside Japan, an unknown ``accuracy`` value or a
repeated identifier stops the run.

Outputs (data/gold/accessibility/):
  distance_bands.csv         population by distance band x series x stratum
  coverage.csv               cumulative coverage shares and the 2013->2026 change
  municipality_access.csv    per-municipality population-weighted access metrics
  mesh_distances.csv.gz      per-mesh nearest distance for every series

Generation and sealing are one operation (``commit_sealed_directory``): the new
payload is staged, sealed, verified and only then swapped in, so the directory
never holds the previous run's seal beside this run's numbers.
"""
from __future__ import annotations
import csv, gzip, io, json, sys, time
from pathlib import Path
import numpy as np
from pyproj import Geod
from scipy.spatial import cKDTree

sys.path.insert(0, str(Path(__file__).parent))

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_paths import project_root

ROOT = project_root()
sys.path.insert(0, str(ROOT / "src"))
from postal_bias.artifacts import (ArtifactError, commit_sealed_directory,   # noqa: E402
                                   verify_artifact)
from postal_bias.geo_qa import (GeoInputError, check_coordinate_contract,    # noqa: E402
                                check_identifier_uniqueness, operating_state)
from postal_bias.geo_policy import build_conservative_inheritance             # noqa: E402

GEO = ROOT / "data/work/geo"
GOLD = ROOT / "data/gold/accessibility"
GOLD.mkdir(parents=True, exist_ok=True)
GEOD = Geod(ellps="GRS80")

BANDS = [500, 1000, 2000, 5000, 10000]
BAND_LABELS = ["<=500m", "500m-1km", "1-2km", "2-5km", "5-10km", ">10km"]

#: Sealed inputs, with the coordinate contract each one must satisfy.
GEOCODE_2013 = ROOT / "data/silver/geocode/2013-11-30"
GEOCODE_2026 = ROOT / "data/silver/geocode/2026-06-30"
#: ``unmatched`` is a legitimate outcome of the geocoding cascade and carries no
#: coordinate; any other value means the cascade changed without this script.
ACCURACY_VALUES_2026 = {"block", "town", "city", "unmatched"}

#: Unchanged from the bundle's first seal, so downstream references keep working.
ARTIFACT_TYPE = "accessibility_metrics"


def read_sealed_records(bundle: Path, *, expected_artifact_type: str | None = None,
                        **contract) -> tuple[list[dict], dict]:
    """Verify a sealed geocode bundle, then read its records.

    ``verify_artifact`` is what makes the bytes trustworthy; reading
    ``records.jsonl`` directly skipped it entirely, so an edited or half-written
    file was indistinguishable from the sealed one.  QA errors stop the run for
    the same reason the confirmation step refuses a defective anchor: a bundle
    that failed its own checks is not evidence.
    """
    try:
        result = verify_artifact(bundle, strict_code=True)
    except (ArtifactError, OSError, ValueError) as exc:
        # An unsealed directory, an unreadable manifest or a rejected root all
        # arrive here; none of them is a bundle this step may read.
        raise GeoInputError("sealed input rejected",
                            [{"code": "input_artifact_unverifiable", "bundle": bundle.name,
                              "detail": type(exc).__name__}]) from exc
    if not result.get("ok"):
        raise GeoInputError("sealed input rejected",
                            [{"code": "input_artifact_mismatch", "bundle": bundle.name,
                              "errors": sorted(result.get("errors", []))}])
    manifest = json.loads((bundle / "artifact.json").read_text(encoding="utf-8"))
    problems = []
    if expected_artifact_type and manifest.get("artifact_type") != expected_artifact_type:
        problems.append({"code": "input_artifact_type_unexpected", "bundle": bundle.name,
                         "expected": expected_artifact_type,
                         "actual": manifest.get("artifact_type")})
    if manifest.get("qa_error_count"):
        problems.append({"code": "input_artifact_qa_errors", "bundle": bundle.name,
                         "qa_error_count": manifest.get("qa_error_count"),
                         "qa_files": manifest.get("qa_files")})
    sealed = set(json.loads((bundle / "checksums.json").read_text(encoding="utf-8")).get("files", {}))
    if "records.jsonl" not in sealed:
        problems.append({"code": "input_artifact_incomplete", "bundle": bundle.name,
                         "missing": ["records.jsonl"]})
    if problems:
        raise GeoInputError("sealed input rejected", problems)
    records = [json.loads(line) for line
               in (bundle / "records.jsonl").read_text(encoding="utf-8").splitlines()
               if line.strip()]
    contract_report = check_coordinate_contract(records, **contract)
    return records, {"bundle": bundle.name, "artifact_id": manifest.get("artifact_id"),
                     "artifact_type": manifest.get("artifact_type"),
                     "qa_total_count": manifest.get("qa_total_count"),
                     "qa_error_count": manifest.get("qa_error_count"),
                     "coordinate_contract": contract_report}


def ecef(lat, lon):
    a = 6378137.0
    f = 1 / 298.257222101
    e2 = f * (2 - f)
    la = np.radians(lat); lo = np.radians(lon)
    s = np.sin(la)
    N = a / np.sqrt(1 - e2 * s * s)
    return np.column_stack([N * np.cos(la) * np.cos(lo),
                            N * np.cos(la) * np.sin(lo),
                            N * (1 - e2) * s])


def nearest(qlat, qlon, olat, olon, k=8):
    tree = cKDTree(ecef(olat, olon))
    _, idx = tree.query(ecef(qlat, qlon), k=min(k, len(olat)), workers=-1)
    if idx.ndim == 1:
        idx = idx[:, None]
    best = np.full(len(qlat), np.inf)
    bidx = np.zeros(len(qlat), dtype=np.int64)
    for c in range(idx.shape[1]):
        j = idx[:, c]
        _, _, d = GEOD.inv(qlon, qlat, olon[j], olat[j])
        upd = d < best
        best[upd] = d[upd]; bidx[upd] = j[upd]
    return best, bidx


def load_mesh():
    codes, lat, lon, pop, p65, muni, kaso = [], [], [], [], [], [], []
    with (GEO / "mesh500.csv").open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            codes.append(r["mesh"]); lat.append(float(r["lat"])); lon.append(float(r["lon"]))
            pop.append(int(r["pop"])); p65.append(int(r["p65"]))
            muni.append(r["muni_code"]); kaso.append(int(r["kaso"]))
    return (np.array(codes), np.array(lat), np.array(lon), np.array(pop),
            np.array(p65), np.array(muni, dtype=object), np.array(kaso))


def _open(record):
    """Tri-state operating flag.

    ``operating`` is True / False / None in the current geocode output and was
    a plain bool in older files.  ``None`` means the source cell was not
    recognised; such an office is left out of the effective series rather than
    counted as closed, and the count is reported by the caller.
    """
    if "operating_state" in record:
        return record["operating_state"]
    value = record.get("operating")
    if value is True:
        return "yes"
    if value is False:
        return "no"
    return "unknown"


def load_offices():
    # The 2013 cross-section is keyed by P30 record id and always carries a
    # coordinate; the 2026 cross-section is keyed by the official identifier and
    # may legitimately have none where the cascade reached ``unmatched``.
    o13, qa13 = read_sealed_records(
        GEOCODE_2013, expected_artifact_type="facility_geocode",
        identifier_key="p30_record_id", allow_missing_coordinates=False)
    o26, qa26 = read_sealed_records(
        GEOCODE_2026, expected_artifact_type="facility_geocode",
        identifier_key="official_identifier",
        accuracy_values=ACCURACY_VALUES_2026, allow_missing_coordinates=True)
    # A repeated identifier would make the P30 coordinate inheritance below
    # attach the same surveyed point to two different offices without saying so.
    check_identifier_uniqueness(o26)
    unknown_open = sum(1 for r in o26 if _open(r) == "unknown")
    if unknown_open:
        print(f"  WARNING: {unknown_open:,} 2026 offices have an unknown operating state; "
              f"they are excluded from every effective series")
    o26 = [r for r in o26 if r["lat"] is not None]

    # Conservative sensitivity variant.  The old run consumed an unsealed
    # qa_match.tsv and inherited points even when the two coordinates differed
    # by tens of kilometres.  Rebuild the mapping from the two verified bundles:
    # unique exact name + municipality only, with a 1 km discrepancy gate.
    inherit, inheritance_qa = build_conservative_inheritance(
        o13, o26, max_distance_m=1000.0,
        distance=lambda a, b, c, d: GEOD.inv(b, a, d, c)[2])
    n_inh = 0
    for r in o26:
        match = inherit.get(r["official_identifier"])
        if match is not None:
            r["lat_inh"], r["lon_inh"] = match["lat"], match["lon"]
            n_inh += 1
        else:
            r["lat_inh"], r["lon_inh"] = r["lat"], r["lon"]
    print(f"  P30 coordinate inherited for {n_inh:,} conservatively matched "
          "town/city-precision 2026 offices")

    series = {
        "2013_formal": [(r["lat"], r["lon"]) for r in o13],
        "2013_effective": [(r["lat"], r["lon"]) for r in o13 if _open(r) == "yes"],
        "2026_formal": [(r["lat"], r["lon"]) for r in o26],
        "2026_effective": [(r["lat"], r["lon"]) for r in o26 if _open(r) == "yes"],
        "2026_formal_blockonly": [(r["lat"], r["lon"]) for r in o26
                                  if r["accuracy"] == "block"],
        "2026_effective_blockonly": [(r["lat"], r["lon"]) for r in o26
                                     if r["accuracy"] == "block" and _open(r) == "yes"],
        "2026_formal_p30inherit": [(r["lat_inh"], r["lon_inh"]) for r in o26],
        "2026_effective_p30inherit": [(r["lat_inh"], r["lon_inh"]) for r in o26
                                      if _open(r) == "yes"],
    }
    return ({k: (np.array([p[0] for p in v]), np.array([p[1] for p in v]))
             for k, v in series.items()},
            {"offices_2013": len(o13), "offices_2026_with_coordinate": len(o26),
             "offices_2026_unknown_operating_state": unknown_open,
             "p30_coordinate_inherited": n_inh,
             "p30_inheritance_policy": inheritance_qa,
             "sealed_inputs": [qa13, qa26]},
            [qa13["artifact_id"], qa26["artifact_id"]])


def band_index(d):
    b = np.zeros(len(d), dtype=np.int64)
    for i, t in enumerate(BANDS):
        b[d > t] = i + 1
    return b


def main():
    t0 = time.time()
    codes, mlat, mlon, pop, p65, muni, kaso = load_mesh()
    print(f"meshes={len(codes):,}  population={pop.sum():,}  ({time.time()-t0:.0f}s)")
    try:
        series, input_qa, input_artifact_ids = load_offices()
    except GeoInputError as exc:
        print("input rejected:", json.dumps(exc.details, ensure_ascii=False)[:2000], file=sys.stderr)
        return 2
    for k, (a, b) in series.items():
        print(f"  {k}: {len(a):,} offices")

    dens = pop / 0.25                                  # persons per km^2 (nominal)
    strat_density = np.where(dens >= 4000, "did_like_ge4000",
                     np.where(dens >= 1000, "mid_1000_4000", "sparse_lt1000"))

    dist = {}
    for name, (olat, olon) in series.items():
        d, _ = nearest(mlat, mlon, olat, olon)
        dist[name] = d
        print(f"  nearest {name} done ({time.time()-t0:.0f}s)")

    # ---------------- distance bands
    rows = []
    def emit(series_name, stratum_kind, stratum, mask):
        d = dist[series_name][mask]; p = pop[mask]; e = p65[mask]
        bi = band_index(d)
        tot = p.sum()
        for i, lab in enumerate(BAND_LABELS):
            sel = bi == i
            rows.append({"series": series_name, "stratum_kind": stratum_kind,
                         "stratum": stratum, "band": lab,
                         "meshes": int(sel.sum()), "population": int(p[sel].sum()),
                         "pop65": int(e[sel].sum()),
                         "share": (p[sel].sum() / tot) if tot else 0.0})

    for s in series:
        emit(s, "all", "all", np.ones(len(pop), dtype=bool))
        emit(s, "kaso", "kaso", kaso == 1)
        emit(s, "kaso", "non_kaso", kaso == 0)
        for lv in ("did_like_ge4000", "mid_1000_4000", "sparse_lt1000"):
            emit(s, "density", lv, strat_density == lv)

    # ---------------- cumulative coverage
    cov = []
    for s in series:
        for kind, name, mask in (("all", "all", np.ones(len(pop), bool)),
                                 ("kaso", "kaso", kaso == 1),
                                 ("kaso", "non_kaso", kaso == 0),
                                 ("density", "did_like_ge4000", strat_density == "did_like_ge4000"),
                                 ("density", "mid_1000_4000", strat_density == "mid_1000_4000"),
                                 ("density", "sparse_lt1000", strat_density == "sparse_lt1000")):
            d = dist[s][mask]; p = pop[mask]
            tot = p.sum()
            rec = {"series": s, "stratum_kind": kind, "stratum": name,
                   "population": int(tot),
                   "mean_dist_m": float((d * p).sum() / tot) if tot else None,
                   "median_dist_m": float(weighted_median(d, p)) if tot else None}
            for t in BANDS:
                rec[f"within_{t}m"] = int(p[d <= t].sum())
                rec[f"share_within_{t}m"] = float(p[d <= t].sum() / tot) if tot else None
            rec["beyond_10000m"] = int(p[d > 10000].sum())
            cov.append(rec)

    # ---------------- per-municipality
    order = {}
    for i, m in enumerate(muni):
        order.setdefault(m, []).append(i)
    mrows = []
    for m, ix in order.items():
        ix = np.array(ix)
        p = pop[ix]; tot = p.sum()
        rec = {"muni_code": m, "population": int(tot), "meshes": len(ix),
               "kaso": int(kaso[ix][0]) if len(ix) else 0}
        for s in series:
            d = dist[s][ix]
            rec[f"mean_{s}"] = float((d * p).sum() / tot) if tot else None
            rec[f"share_gt2km_{s}"] = float(p[d > 2000].sum() / tot) if tot else None
        mrows.append(rec)

    # ---------------- commit
    # Four outputs used to be opened and overwritten one after another.  A
    # failure in the middle (or an input rejected on the next run) left a
    # directory that mixed this run's coverage.csv with the previous run's
    # mesh_distances.csv.gz, and nothing downstream could tell.  Everything is
    # staged first and committed together; a failure restores the old set.
    # Sealing is part of the same operation, so the published directory is never
    # this run's payload under the previous run's seal.
    def _dictcsv(data):
        def write(stream):
            w = csv.DictWriter(stream, fieldnames=list(data[0]), lineterminator="\n")
            w.writeheader(); w.writerows(data)
        return write

    def _mesh_gzip_bytes():
        # Built in memory so it can be staged as a byte payload alongside the
        # text members; mtime=0 keeps the member byte-reproducible.
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
            text = io.TextIOWrapper(gz, encoding="utf-8", newline="\n")
            w = csv.writer(text, lineterminator="\n")
            w.writerow(["mesh", "lat", "lon", "pop", "pop65", "muni_code", "kaso",
                        "density_class"] + [f"d_{s}" for s in series])
            for i in range(len(codes)):
                w.writerow([codes[i], f"{mlat[i]:.6f}", f"{mlon[i]:.6f}", pop[i], p65[i],
                            muni[i], kaso[i], strat_density[i]]
                           + [f"{dist[s][i]:.1f}" for s in series])
            text.flush(); text.detach()
        return buf.getvalue()

    manifest = {"generated_by": "data/work/geo/accessibility.py",
                "mesh_count": int(len(codes)), "population": int(pop.sum()),
                "series": {k: int(len(v[0])) for k, v in series.items()},
                "input_qa": input_qa,
                "input_artifact_ids": sorted(set(input_artifact_ids)),
                "bands_m": BANDS, "band_labels": BAND_LABELS,
                "nearest_neighbour": "k=8 KD-tree over ECEF, exact geodesic on GRS80",
                "commit": "payload staged, sealed and verified, then published in one operation"}

    try:
        sealed = commit_sealed_directory(GOLD, {
            "distance_bands.csv": _dictcsv(rows),
            "coverage.csv": _dictcsv(cov),
            "municipality_access.csv": _dictcsv(mrows),
            "mesh_distances.csv.gz": _mesh_gzip_bytes(),
            "run_manifest.json": lambda s: s.write(json.dumps(manifest, ensure_ascii=False, indent=1)),
        }, artifact_type=ARTIFACT_TYPE, source_authority="auxiliary",
            evidence_status="effective", release_classification="internal_only",
            input_artifact_ids=sorted(set(input_artifact_ids)),
            acknowledge_internal_use=True, strict_code=True)
    except ArtifactError as exc:
        print("output rejected:", exc, file=sys.stderr)
        return 2
    print(f"written and sealed to {GOLD} artifact_id={sealed['artifact_id']} "
          f"({time.time()-t0:.0f}s)")
    if sealed.get("cleanup_warnings"):
        print("  cleanup warnings:", sealed["cleanup_warnings"])
    return 0


def weighted_median(d, w):
    o = np.argsort(d)
    d = d[o]; w = w[o]
    c = np.cumsum(w)
    if c[-1] == 0:
        return float("nan")
    return d[np.searchsorted(c, c[-1] / 2.0)]


if __name__ == "__main__":
    raise SystemExit(main())
