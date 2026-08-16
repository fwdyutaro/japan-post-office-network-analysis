"""metadata.json + qa.jsonl for data/silver/geocode/2026-06-30 and 2013-11-30."""
from __future__ import annotations
import collections, csv, hashlib, json, sys
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_paths import project_root

ROOT = project_root()
sys.path.insert(0, str(ROOT / "src"))
from postal_bias.artifacts import commit_file_set                       # noqa: E402

GEO = ROOT / "data/work/geo"
D26 = ROOT / "data/silver/geocode/2026-06-30"
D13 = ROOT / "data/silver/geocode/2013-11-30"


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for b in iter(lambda: fh.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


recs = [json.loads(l) for l in (D26 / "records.jsonl").open(encoding="utf-8")]
acc = collections.Counter(r["accuracy"] for r in recs)
src = collections.Counter(r["source"] for r in recs)
tm = collections.Counter(r["town_method"] for r in recs)

qa_dist = {}
for r in csv.DictReader((GEO / "qa_match.tsv").open(encoding="utf-8"), delimiter="\t"):
    qa_dist[r["id"]] = float(r["dist_m"])
verdict = {}
for r in csv.DictReader((GEO / "qa_outliers_classified.tsv").open(encoding="utf-8"),
                        delimiter="\t"):
    verdict[r["id"]] = r["verdict"]

qa_rows = []
for r in recs:
    flags = []
    if r["accuracy"] == "unmatched":
        flags.append("unmatched_no_coordinate")
    if r["accuracy"] == "city":
        flags.append("municipality_representative_point_only")
    if r["town_method"] in ("prefix", "suffix", "substr", "prefix1"):
        flags.append("town_name_inferred_" + r["town_method"])
    d = qa_dist.get(r["official_identifier"])
    if d is None:
        flags.append("no_unique_name_match_in_p30_2013")
    elif d > 1000:
        flags.append("p30_2013_discrepancy_over_1km")
    if flags:
        qa_rows.append({"official_identifier": r["official_identifier"],
                        "name": r["name"], "address": r["address"],
                        "accuracy": r["accuracy"], "source": r["source"],
                        "town_method": r["town_method"],
                        "p30_2013_distance_m": None if d is None else round(d, 1),
                        "outlier_verdict": verdict.get(r["official_identifier"]),
                        "flags": flags})
qa_summary = json.loads((GEO / "qa_summary.json").read_text(encoding="utf-8"))
datum = json.loads((GEO / "datum_report.json").read_text(encoding="utf-8"))
passa = json.loads((GEO / "isj_pass_a_stats.json").read_text(encoding="utf-8"))

meta26 = {
    "dataset": "post office coordinates, notification list cross-section 2026-06-30",
    "generated_at": "2026-08-09",
    "input": {
        "records": str(ROOT / "data/silver/current_list/2026-06-30/records.jsonl"),
        "n_records": len(recs),
        "note_on_the_name_address_column_split":
            "The overlapping-prefecture split defect (東京都府中市 also matches 京都府 one "
            "character in, which put the boundary one character late for 24 東京都府中市 "
            "records) is fixed in current_list._find_address_start, parser_version "
            "0.2.0.  geocode_2026.py no longer repairs anything in place: it checks the "
            "input fail-closed and stops if any name does not end in 郵便局/分室/出張所.  "
            "See input_qa.jsonl in this directory for the check result.",
        "unlisted_long_term_suspended_simple_post_offices": 653,
        "unlisted_treatment": "not geocodable - the footnote gives only a count, no "
                              "identities.  These 653 offices are excluded from every "
                              "coordinate-based series and are NOT treated as "
                              "non-existent.",
    },
    "reference_data": {
        "street_block": "国土交通省 街区レベル位置参照情報 2025年版 (24.0a), "
                        "data/external/isj/isj_blocks_all.csv, 19,589,219 rows",
        "street_block_rows_dropped_as_deleted": passa["dropped_deleted"],
        "town_level": "国土交通省 大字・町丁目レベル位置参照情報 19.0b, 47 prefecture "
                      "archives, 191,106 town records",
        "crs": "EPSG:6668 (JGD2011).  The reference product publishes 十進経緯度 on the "
               "Japanese geodetic system 2011; no transformation was applied.",
    },
    "cascade": {
        "block": "exact (prefecture, city, 大字・丁目, 街区符号・地番) hit; the town key "
                 "must have matched exactly, and for a non-丁目 town the address must "
                 "carry at most two numbers",
        "town": "mean of the street-block rows of the 大字・丁目, or the 19.0b "
                "representative point where the municipality has no street-block cover",
        "city": "mean of all 19.0b town representative points of the municipality",
        "unmatched": "no prefecture or no municipality could be parsed",
    },
    "counts_by_accuracy": dict(acc),
    "counts_by_source": dict(src),
    "counts_by_town_match_method": {str(k): v for k, v in tm.items()},
    "independent_accuracy_check": qa_summary,
    "qa_flagged_records": len(qa_rows),
    "records_sha256": sha256(D26 / "records.jsonl"),
}

meta13 = {
    "dataset": "post office coordinates, P30 cross-section 2013-11-30, datum shifted",
    "generated_at": "2026-08-09",
    "input": str(ROOT / "data/silver/p30/2013-11-30/records.jsonl"),
    "source_crs": "EPSG:4612 (JGD2000)", "target_crs": "EPSG:6668 (JGD2011)",
    "transformation": datum,
    "limitation": "PROJ selected the only locally available operation, "
                  "'JGD2000 to JGD2011 (2)', a geocentric translation with null "
                  "parameters and a declared accuracy of 1 m.  The grid-based "
                  "operation 'JGD2000 to JGD2011 (1)' needs "
                  "touhokutaiheiyouoki2011.gsb, which the PROJ CDN does not carry "
                  "(no Japanese horizontal grid is published there; only "
                  "jp_gsi_gsigeo2011.tif and jp_gsi_jpgeo2024.tif geoid models).  "
                  "The applied shift is therefore exactly 0 m for all records.",
    "records_sha256": sha256(D13 / "records.jsonl"),
}

# Three outputs across two artifact directories: commit them together so a
# failure cannot leave 2026's metadata refreshed beside 2013's stale copy.
commit_file_set({
    D26 / "qa.jsonl": lambda s: [s.write(json.dumps(q, ensure_ascii=False) + "\n") for q in qa_rows],
    D26 / "metadata.json": lambda s: s.write(json.dumps(meta26, ensure_ascii=False, indent=1)),
    D13 / "metadata.json": lambda s: s.write(json.dumps(meta13, ensure_ascii=False, indent=1)),
}, replace=True)
print(json.dumps({"accuracy": dict(acc), "town_method": {str(k): v for k, v in tm.items()},
                  "qa_rows": len(qa_rows)}, ensure_ascii=False, indent=1))
