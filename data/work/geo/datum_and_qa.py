"""Task 2 + 3.

2.  Transform the 2013 P30 coordinates from JGD2000 (EPSG:4612) to
    JGD2011 (EPSG:6668) with pyproj and measure the shift actually applied.
3.  Name+municipality join of the 2026 geocoded list against the 2013 P30 list
    and measure the geodesic distance between the two coordinate sets for the
    offices present in both.

Outputs
  data/silver/geocode/2013-11-30/records.jsonl
  data/work/geo/datum_report.json
  data/work/geo/qa_match.tsv          one row per matched office
  data/work/geo/qa_outliers.tsv       matches further apart than 1 km
"""
from __future__ import annotations
import collections, json, re, statistics
from pathlib import Path

import pyproj
from pyproj import Geod
from pyproj.transformer import Transformer, TransformerGroup

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_paths import project_root

ROOT = project_root()
GEO = ROOT / "data/work/geo"
P30 = ROOT / "data/silver/p30/2013-11-30/records.jsonl"
G26 = ROOT / "data/silver/geocode/2026-06-30/records.jsonl"
CODES = ROOT / "data/external/municipality/municipality_codes.csv"

GEOD = Geod(ellps="GRS80")
CLOSED_RE = re.compile(r"[（(]一時閉鎖[）)]\s*$")

HISTORICAL_CODES = {
    "22131": "22130", "22132": "22130", "22133": "22130", "22134": "22130",
    "22135": "22130", "22136": "22130", "22137": "22130",
    "03305": "03216", "04423": "04216", "40305": "40231", "09367": "09203",
}

# 2013 -> 2026 municipality mergers / renames beyond the plain code changes are
# handled by falling back to a prefecture-wide name match.


# ----------------------------------------------------------------- task 2
def transform_2013():
    grp = TransformerGroup("EPSG:4612", "EPSG:6668", always_xy=True)
    used = grp.transformers[0]
    unavailable = []
    for u in grp.unavailable_operations:
        unavailable.append({"name": u.name,
                            "grids": [g.short_name for g in u.grids]})
    tr = Transformer.from_crs("EPSG:4612", "EPSG:6668", always_xy=True)

    recs = [json.loads(l) for l in P30.open(encoding="utf-8")]
    deltas = []
    out = []
    by_pref_delta = collections.defaultdict(list)
    for r in recs:
        lat0, lon0 = r["latitude"], r["longitude"]
        lon1, lat1 = tr.transform(lon0, lat0)
        d = GEOD.inv(lon0, lat0, lon1, lat1)[2]
        deltas.append(d)
        code = str(r.get("administrative_area") or "")
        code = HISTORICAL_CODES.get(code, code)
        by_pref_delta[code[:2]].append(d)
        nm = r.get("name_raw") or ""
        out.append({
            "p30_record_id": r["p30_record_id"],
            "name": CLOSED_RE.sub("", nm).strip(),
            "name_raw": nm,
            "muni_code_2013": str(r.get("administrative_area") or ""),
            "muni_code": code,
            "type": r.get("type"),
            "simple_post_office": r.get("type") == "18004",
            "operating": not CLOSED_RE.search(nm),
            "lat_jgd2000": lat0, "lon_jgd2000": lon0,
            "lat": round(lat1, 7), "lon": round(lon1, 7),
            "crs": "EPSG:6668 (JGD2011)",
            "datum_shift_m": round(d, 4),
        })
    dst = ROOT / "data/silver/geocode/2013-11-30"
    dst.mkdir(parents=True, exist_ok=True)
    with (dst / "records.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for o in out:
            fh.write(json.dumps(o, ensure_ascii=False) + "\n")

    rep = {
        "pyproj_version": pyproj.__version__,
        "proj_version": pyproj.proj_version_str,
        "operation_used": used.description,
        "operation_accuracy_m": used.accuracy,
        "operation_method": [op.method_name for op in used.operations],
        "unavailable_operations": unavailable,
        "network_enabled": pyproj.network.is_network_enabled(),
        "n": len(deltas),
        "shift_max_m": max(deltas), "shift_median_m": statistics.median(deltas),
        "shift_mean_m": sum(deltas) / len(deltas), "shift_min_m": min(deltas),
        "shift_nonzero_count": sum(1 for d in deltas if d > 1e-6),
        "by_pref_max_m": {k: max(v) for k, v in sorted(by_pref_delta.items())},
    }
    (GEO / "datum_report.json").write_text(json.dumps(rep, ensure_ascii=False, indent=1),
                                           encoding="utf-8")
    return out, rep


# ----------------------------------------------------------------- task 3
NAMEFOLD = str.maketrans({"ヶ": "ケ", "ヵ": "カ", "髙": "高", "﨑": "崎", "德": "徳",
                          "邊": "辺", "邉": "辺", "澤": "沢", "齋": "斎", "齊": "斉",
                          "惠": "恵", "檮": "梼", "嶋": "島", "冨": "富", "櫻": "桜",
                          "瀨": "瀬", "曾": "曽", " ": "", "　": ""})


def nfold(s: str) -> str:
    return (s or "").translate(NAMEFOLD).replace(" ", "")


def qa(p13):
    g26 = [json.loads(l) for l in G26.open(encoding="utf-8")]
    idx13_code = collections.defaultdict(list)
    idx13_pref = collections.defaultdict(list)
    for r in p13:
        idx13_code[(r["muni_code"], nfold(r["name"]))].append(r)
        idx13_pref[(r["muni_code"][:2], nfold(r["name"]))].append(r)

    rows, unmatched = [], 0
    for r in g26:
        if r["lat"] is None or not r["muni_code"]:
            unmatched += 1
            continue
        key = (r["muni_code"], nfold(r["name"]))
        cands = idx13_code.get(key)
        how = "code+name"
        if not cands:
            cands = idx13_pref.get((r["muni_code"][:2], nfold(r["name"])))
            how = "pref+name"
        if not cands or len(cands) != 1:
            unmatched += 1
            continue
        m = cands[0]
        d = GEOD.inv(r["lon"], r["lat"], m["lon"], m["lat"])[2]
        rows.append({"id": r["official_identifier"], "name": r["name"],
                     "pref": r["pref"], "muni": r["muni_code"],
                     "accuracy": r["accuracy"], "source": r["source"],
                     "how": how, "dist_m": d,
                     "lat26": r["lat"], "lon26": r["lon"],
                     "lat13": m["lat"], "lon13": m["lon"],
                     "addr": r["address"], "addr13": ""})
    return rows, unmatched, len(g26)


def pct(v, p):
    v = sorted(v)
    if not v:
        return None
    i = min(len(v) - 1, int(round((len(v) - 1) * p)))
    return v[i]


def main():
    p13, rep = transform_2013()
    print("DATUM:", json.dumps({k: rep[k] for k in
          ("operation_used", "operation_accuracy_m", "shift_max_m", "shift_median_m",
           "shift_nonzero_count", "n")}, ensure_ascii=False))
    print("unavailable:", rep["unavailable_operations"])

    rows, unmatched, n26 = qa(p13)
    d = [r["dist_m"] for r in rows]
    summary = {
        "n_2026": n26, "n_matched": len(rows), "n_unmatched": unmatched,
        "median_m": statistics.median(d), "mean_m": sum(d) / len(d),
        "p75_m": pct(d, .75), "p90_m": pct(d, .90), "p95_m": pct(d, .95),
        "p99_m": pct(d, .99), "max_m": max(d),
        "over_200m": sum(1 for x in d if x > 200),
        "over_500m": sum(1 for x in d if x > 500),
        "over_1km": sum(1 for x in d if x > 1000),
        "over_5km": sum(1 for x in d if x > 5000),
    }
    by_acc = {}
    for a in ("block", "town", "city"):
        v = [r["dist_m"] for r in rows if r["accuracy"] == a]
        if v:
            by_acc[a] = {"n": len(v), "median_m": statistics.median(v),
                         "p90_m": pct(v, .90), "p99_m": pct(v, .99),
                         "over_1km": sum(1 for x in v if x > 1000)}
    summary["by_accuracy"] = by_acc
    by_src = {}
    for s in sorted({r["source"] for r in rows}):
        v = [r["dist_m"] for r in rows if r["source"] == s]
        by_src[s] = {"n": len(v), "median_m": round(statistics.median(v), 1),
                     "p90_m": round(pct(v, .90), 1),
                     "over_1km": sum(1 for x in v if x > 1000)}
    summary["by_source"] = by_src
    print("QA:", json.dumps(summary, ensure_ascii=False, indent=1))
    (GEO / "qa_summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                         encoding="utf-8")
    with (GEO / "qa_match.tsv").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("id\tname\tpref\tmuni\taccuracy\tsource\thow\tdist_m\tlat26\tlon26\tlat13\tlon13\taddr\n")
        for r in sorted(rows, key=lambda x: -x["dist_m"]):
            fh.write("\t".join(str(x) for x in (r["id"], r["name"], r["pref"], r["muni"],
                     r["accuracy"], r["source"], r["how"], round(r["dist_m"], 1),
                     r["lat26"], r["lon26"], r["lat13"], r["lon13"], r["addr"])) + "\n")
    out = [r for r in rows if r["dist_m"] > 1000]
    with (GEO / "qa_outliers.tsv").open("w", encoding="utf-8", newline="\n") as fh:
        fh.write("id\tname\tpref\tmuni\taccuracy\tsource\tdist_m\tlat26\tlon26\tlat13\tlon13\taddr\n")
        for r in sorted(out, key=lambda x: -x["dist_m"]):
            fh.write("\t".join(str(x) for x in (r["id"], r["name"], r["pref"], r["muni"],
                     r["accuracy"], r["source"], round(r["dist_m"], 1),
                     r["lat26"], r["lon26"], r["lat13"], r["lon13"], r["addr"])) + "\n")
    print("outliers >1km:", len(out))


if __name__ == "__main__":
    main()
