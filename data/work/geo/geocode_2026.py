"""Geocode the 2026-06-30 post-office list with the ISJ position reference data.

Cascade (accuracy class recorded on every record):
  block  : exact (pref, city, oaza, block number) hit in the street-block product
  town   : mean of all street-block rows of the (pref, city, oaza), or, where the
           municipality has no street-block coverage, the 大字町丁目 representative
           point of the 19.0b product
  city   : mean of all 大字町丁目 representative points of the municipality
  unmatched

Two passes over the 1.76 GB street-block CSV are avoided for the town level
(pass A already aggregated it); this script performs the single remaining pass
for the block level, driven by a pre-computed key set.
"""
from __future__ import annotations
import argparse, csv, json, re, sys, time, collections
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from project_paths import project_root

ROOT = project_root()
sys.path.insert(0, str(ROOT / "src"))
from postal_bias.artifacts import (ArtifactError, commit_file_set,     # noqa: E402
                                   commit_sealed_directory, verify_artifact)
from postal_bias.geo_qa import (GeoInputError, check_identifier_uniqueness,  # noqa: E402
                                check_name_column_boundary, mark_state,
                                mark_state_summary, operating_state, tri_bool)

GEO = ROOT / "data/work/geo"
CUR_BUNDLE = ROOT / "data/silver/current_list/2026-06-30"
CUR = CUR_BUNDLE / "records.jsonl"
ISJ = ROOT / "data/external/isj/isj_blocks_all.csv"

# ---------------------------------------------------------------- normalising
KYUJI = str.maketrans({"惠": "恵", "澤": "沢", "圓": "円", "榮": "栄", "邊": "辺",
                       "邉": "辺", "瀨": "瀬", "齋": "斎", "齊": "斉", "藪": "薮",
                       "曾": "曽", "德": "徳", "假": "仮", "眞": "真", "淺": "浅",
                       "檮": "梼", "嶋": "島", "槇": "槙", "冨": "富", "髙": "高",
                       "祗": "祇", "櫻": "桜", "濵": "浜", "祐": "祐"})
ZEN = str.maketrans("０１２３４５６７８９（）　", "0123456789() ")
DASH = str.maketrans({c: "-" for c in "－‐‒–—―−ｰ"})
KEYFOLD = str.maketrans({"ヶ": "ケ", "ヵ": "カ", "ッ": "ツ", "ノ": "ノ"})

PREF_RE = re.compile(r"^(北海道|東京都|(?:京都|大阪)府|.{2,3}県)")
KANJI_D = "〇一二三四五六七八九"


def kanji_num(n: int) -> str:
    """Arabic -> the kanji form the ISJ uses for 丁目/条 (1..99)."""
    if n < 10:
        return KANJI_D[n]
    if n == 10:
        return "十"
    if n < 20:
        return "十" + KANJI_D[n % 10]
    t, o = divmod(n, 10)
    return KANJI_D[t] + "十" + (KANJI_D[o] if o else "")


def norm_addr(s: str) -> str:
    s = (s or "").translate(ZEN).translate(DASH).translate(KYUJI)
    s = re.sub(r"(?<=\d)ー(?=\d)", "-", s)
    return s.strip()


def keyfold(s: str) -> str:
    """Fold a town name to a comparison key.

    大字 is written inconsistently between the notification list and the
    reference data (嬉野市 has 嬉野町大字不動山 where the address says
    嬉野町不動山), so it is removed wherever it occurs."""
    s = s.translate(KYUJI).translate(KEYFOLD)
    s = s.replace("大字", "")
    s = re.sub(r"^字", "", s)
    return s


# Ordinal units the reference data spells in kanji: 三丁目 / 四条 / 一線 / 一番町
UNIT_RE = re.compile(r"(\d+)(丁目|条|線|番町)")


def kanjify_chome(s: str) -> str:
    return UNIT_RE.sub(lambda m: kanji_num(int(m.group(1))) + m.group(2), s)


# ---------------------------------------------------------------- ISJ indices
def load_indices():
    town: dict[tuple[str, str], dict[str, tuple[str, float, float]]] = {}
    for line in (GEO / "isj_town.tsv").open(encoding="utf-8"):
        p, c, o, n, la, lo = line.rstrip("\n").split("\t")
        town.setdefault((p, c), {})[keyfold(o)] = (o, float(la), float(lo))
    oaza: dict[tuple[str, str], dict[str, tuple[str, float, float]]] = {}
    citypt: dict[tuple[str, str], list] = {}
    citycode: dict[tuple[str, str], str] = {}
    for line in (GEO / "isj_oaza.tsv").open(encoding="utf-8"):
        p, c, code, o, la, lo = line.rstrip("\n").split("\t")
        la, lo = float(la), float(lo)
        oaza.setdefault((p, c), {}).setdefault(keyfold(o), (o, la, lo))
        citycode[(p, c)] = code
        e = citypt.setdefault((p, c), [0, 0.0, 0.0])
        e[0] += 1; e[1] += la; e[2] += lo
    citypoint = {k: (v[1] / v[0], v[2] / v[0]) for k, v in citypt.items()}
    return town, oaza, citypoint, citycode


# ---------------------------------------------------------------- record prep
MARK_FIELDS = ("simple_post_office", "disadvantaged_area")


def load_records(*, exclude_anomalies: bool = False):
    """Load the anchor, fail-closed.

    Two input defects used to be printed and then ignored.  A duplicate
    official identifier makes every downstream join ambiguous, and a name that
    does not end in a facility suffix means the name/address split landed one
    character late, which silently relocates the office to another prefecture.
    Both now stop the run; ``--exclude-name-anomalies`` swaps the stop for an
    explicit exclusion whose count is reported and written to the QA output.

    The in-script 府中 repair is gone.  Patching parser output here hid the
    parser defect from every other consumer of the same file; the split is
    fixed in ``current_list._find_address_start`` and verified here instead.
    """
    try:
        verification = verify_artifact(CUR_BUNDLE, strict_code=True)
        manifest = json.loads((CUR_BUNDLE / "artifact.json").read_text(encoding="utf-8"))
        checks = json.loads((CUR_BUNDLE / "checksums.json").read_text(encoding="utf-8"))["files"]
    except (ArtifactError, OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise GeoInputError("sealed current-list input rejected", [{
            "code": "current_list_artifact_unverifiable", "detail": type(exc).__name__}]) from exc
    problems = []
    if not verification.get("ok"):
        problems.append({"code": "current_list_artifact_mismatch",
                         "errors": sorted(verification.get("errors", []))})
    if manifest.get("artifact_type") != "current_list":
        problems.append({"code": "current_list_artifact_type_unexpected",
                         "actual": manifest.get("artifact_type")})
    if manifest.get("qa_error_count"):
        problems.append({"code": "current_list_artifact_qa_errors",
                         "qa_error_count": manifest.get("qa_error_count")})
    if "records.jsonl" not in checks:
        problems.append({"code": "current_list_artifact_incomplete"})
    if problems:
        raise GeoInputError("sealed current-list input rejected", problems)
    with CUR.open(encoding="utf-8") as source:
        recs = [json.loads(line) for line in source if line.strip()]
    ident_report = check_identifier_uniqueness(recs)
    recs, name_report = check_name_column_boundary(recs, exclude=exclude_anomalies)
    marks = mark_state_summary(recs, MARK_FIELDS)
    marks["operating_status"] = collections.Counter(operating_state(r) for r in recs)
    return recs, {"anchor_artifact": {"artifact_id": manifest["artifact_id"],
                                      "artifact_type": manifest["artifact_type"],
                                      "strict_code_verified": True},
                  "identifiers": ident_report, "name_column_boundary": name_report,
                  "mark_states": {k: dict(v) for k, v in marks.items()}}


# ---------------------------------------------------------------- city lookup
def build_city_matcher(citycode):
    by_pref = collections.defaultdict(list)
    for (p, c) in citycode:
        by_pref[p].append((keyfold(c), c))
    for p in by_pref:
        by_pref[p].sort(key=lambda x: -len(x[0]))
    return by_pref


def resolve_city(addr, by_pref):
    m = PREF_RE.match(addr)
    if not m:
        return None, None, addr, "no_pref"
    pref = m.group(1)
    rest = addr[len(pref):]
    fr = keyfold(rest)
    # keyfold substitutes characters 1:1 (the 大字/字 strip cannot fire at the
    # start of a city name), so offsets in the folded and original strings agree.
    for k, orig in by_pref.get(pref, []):
        if fr.startswith(k):
            return pref, orig, rest[len(k):], None
    # A 郡 or 島 token can sit between the prefecture and the municipality and be
    # absent from the reference table (東京都三宅島三宅村 / 東京都八丈島八丈町).
    cuts = sorted((i + 1 for i, ch in enumerate(fr[:8]) if ch in "郡島"), reverse=True)
    for cut in cuts:
        fr2 = fr[cut:]
        for k, orig in by_pref.get(pref, []):
            if fr2.startswith(k):
                return pref, orig, rest[cut + len(k):], None
    return pref, None, rest, "no_city"


# ---------------------------------------------------------------- town / block
NUM_TAIL = re.compile(r"\d")


def split_head_nums(rest: str):
    m = NUM_TAIL.search(rest)
    if not m:
        return rest, []
    head = rest[:m.start()]
    tail = rest[m.start():]
    nums = re.findall(r"\d+", tail)
    return head, nums


def town_candidates(head: str, nums: list[str]):
    """(town_key, block_candidates) in priority order, exact-match forms only.

    The 街区符号・地番 column of the reference data never contains a hyphen, so
    only a single number can ever be a key.

    For a 丁目 town the address is 住居表示: 丁目-街区-住居番号, and the second
    number is the street block.
    For a town without 丁目 the numbers are a 地番.  Two numbers ("3896-3") are
    the ordinary 本番-枝番 form and the first number is the key.  Three or more
    numbers are the 岩手 地割 form (甲子町1-54-1 = 甲子町第1地割54-1) or a
    similar compound; the first number is then NOT a 地番 and a lookup on it
    lands on an unrelated parcel, so no block match is attempted.
    """
    out = []
    hk = keyfold(kanjify_chome(head))
    if nums:
        n1 = int(nums[0])
        if 1 <= n1 <= 99:
            b = nums[1:2]
            # Some municipalities (堺市 etc.) spell the unit 丁 rather than 丁目.
            out.append((hk + kanji_num(n1) + "丁目", b))
            out.append((hk + kanji_num(n1) + "丁", b))
            out.append((hk + "第" + kanji_num(n1) + "地割", nums[1:2]))
        out.append((hk, nums[0:1] if len(nums) <= 2 else []))
    else:
        out.append((hk, []))
    return out


def longest_prefix(hk: str, tdict: dict, minlen: int = 2):
    """Longest oaza that is a prefix of hk.  Fires where the address carries a
    小字 / 通称 after the 大字 (common outside 住居表示 areas)."""
    for i in range(len(hk), minlen - 1, -1):
        if hk[:i] in tdict:
            return hk[:i]
    return None


def longest_substring(rk: str, tdict: dict, minlen: int = 3):
    """Longest oaza (>= minlen) occurring anywhere in the post-city remainder.
    Last resort for Kyoto 通り名 addresses such as
    「中立売通千本東入2-田丸町379-7」."""
    best = None
    for i in range(len(rk)):
        for j in range(len(rk), i + minlen - 1, -1):
            s = rk[i:j]
            if s in tdict and (best is None or len(s) > len(best)):
                best = s
                break
    return best


def longest_suffix(hk: str, tdict: dict):
    """Longest oaza that is a suffix of hk (Kyoto-style 通り名 prefixes)."""
    for i in range(1, len(hk) - 1):
        s = hk[i:]
        if len(s) >= 2 and s in tdict:
            return s
    return None


# ---------------------------------------------------------------- main
def main(argv: list[str] | None = None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--exclude-name-anomalies", action="store_true",
                    help="exclude records whose name/address split is suspect instead of stopping")
    args = ap.parse_args(argv)
    t0 = time.time()
    town, oaza, citypoint, citycode = load_indices()
    by_pref = build_city_matcher(citycode)
    try:
        recs, input_qa = load_records(exclude_anomalies=args.exclude_name_anomalies)
    except GeoInputError as exc:
        print("input rejected:", json.dumps(exc.details, ensure_ascii=False)[:2000], file=sys.stderr)
        return 2
    name_report = input_qa["name_column_boundary"]
    print(f"records={len(recs)}  unique ids={input_qa['identifiers']['unique_identifier_count']}  "
          f"name-tail anomalies={name_report['anomaly_count']} "
          f"(excluded={name_report['excluded']})")
    print("mark states:", json.dumps(input_qa["mark_states"], ensure_ascii=False))

    resolved = []
    need: dict[tuple[str, str, str], set] = {}
    for r in recs:
        addr = norm_addr(r["address"]["normalized"])
        pref, city, rest, err = resolve_city(addr, by_pref)
        info = {"rec": r, "addr": addr, "pref": pref, "city": city,
                "code": citycode.get((pref, city)) if city else None,
                "err": err, "town": None, "town_src": None, "town_method": None,
                "blocks": []}
        if city:
            tdict = town.get((pref, city), {})
            odict = oaza.get((pref, city), {})
            head, nums = split_head_nums(keyfold(rest))
            hk = keyfold(kanjify_chome(head))
            for ci, (tk, blocks) in enumerate(town_candidates(head, nums)):
                for d, tag in ((tdict, "block_mean"), (odict, "oaza_19b")):
                    if tk in d:
                        e = d[tk]
                        info["town"] = (e[0], e[1], e[2]); info["town_src"] = tag
                        info["town_method"] = "exact_chome" if tk != hk else "exact"
                        info["blocks"] = blocks
                        break
                if info["town"]:
                    break
            # Inferred town names.  The address then carries a 小字 / 通称 that the
            # reference data does not model, so the trailing number is a 地番
            # inside that 小字, NOT a street block of the 大字 as a whole; a block
            # lookup on it lands on an unrelated parcel.  Block matching is
            # therefore disabled for every inferred path (verified against the
            # 2013 P30 coordinates: it produced multi-kilometre errors).
            if info["town"] is None:
                for d, tag in ((tdict, "block_mean_prefix"), (odict, "oaza_19b_prefix")):
                    s = longest_prefix(hk, d)
                    if s:
                        e = d[s]
                        info["town"] = (e[0], e[1], e[2]); info["town_src"] = tag
                        info["town_method"] = "prefix"
                        break
            if info["town"] is None:
                for d, tag in ((tdict, "block_mean_suffix"), (odict, "oaza_19b_suffix")):
                    s = longest_suffix(hk, d)
                    if s:
                        e = d[s]
                        info["town"] = (e[0], e[1], e[2]); info["town_src"] = tag
                        info["town_method"] = "suffix"
                        break
            if info["town"] is None:
                rk = keyfold(kanjify_chome(rest))
                for d, tag in ((tdict, "block_mean_substr"), (odict, "oaza_19b_substr")):
                    s = longest_substring(rk, d)
                    if s:
                        e = d[s]
                        info["town"] = (e[0], e[1], e[2]); info["town_src"] = tag
                        info["town_method"] = "substr"
                        break
            if info["town"] is None:
                # single-character 大字 (新潟市西蒲区巻 etc.)
                for d, tag in ((tdict, "block_mean_prefix1"), (odict, "oaza_19b_prefix1")):
                    s = longest_prefix(hk, d, minlen=1)
                    if s:
                        e = d[s]
                        info["town"] = (e[0], e[1], e[2]); info["town_src"] = tag
                        info["town_method"] = "prefix1"
                        break
            if (info["town"] and info["town_src"] == "block_mean"
                    and info["town_method"] in ("exact", "exact_chome") and info["blocks"]):
                need.setdefault((pref, city, info["town"][0]), set()).update(info["blocks"])
        resolved.append(info)

    print(f"city resolved={sum(1 for i in resolved if i['city'])}  "
          f"town resolved={sum(1 for i in resolved if i['town'])}  "
          f"block keys needed={len(need)}  ({time.time()-t0:.0f}s)")

    # ---- single streaming pass for the wanted street-block rows
    want_town = set(need)
    hits: dict[tuple[str, str, str, str], list] = {}
    nrows = 0
    with ISJ.open(encoding="utf-8", newline="", buffering=1 << 22) as fh:
        rd = csv.reader(fh)
        next(rd)
        for row in rd:
            nrows += 1
            k3 = (row[0], row[1], row[2])
            if k3 not in want_town:
                continue
            if row[13] == "3":       # 更新後履歴フラグ: 3 = 削除
                continue
            b = row[4]
            if b not in need[k3]:
                continue
            try:
                lat = float(row[8]); lon = float(row[9])
            except ValueError:
                continue
            e = hits.setdefault(k3 + (b,), [0, 0.0, 0.0])
            e[0] += 1; e[1] += lat; e[2] += lon
    print(f"street-block pass: {nrows:,} rows, {len(hits):,} distinct block keys hit "
          f"({time.time()-t0:.0f}s)")

    # ---- assemble
    out = []
    cls = collections.Counter()
    for i in resolved:
        r = i["rec"]
        lat = lon = None; acc = "unmatched"; src = None; detail = None
        if i["town"] and i["blocks"]:
            for b in i["blocks"]:
                k = (i["pref"], i["city"], i["town"][0], b)
                if k in hits:
                    n, sla, slo = hits[k]
                    lat, lon = sla / n, slo / n
                    acc, src = "block", "isj_block_24.0a"
                    detail = f"{i['town'][0]}/{b}(n={n})"
                    break
        if lat is None and i["town"]:
            lat, lon = i["town"][1], i["town"][2]
            acc = "town"; src = "isj_" + i["town_src"]; detail = i["town"][0]
        if lat is None and i["city"]:
            p = citypoint.get((i["pref"], i["city"]))
            if p:
                lat, lon = p
                acc = "city"; src = "isj_city_mean"; detail = i["city"]
        cls[acc] += 1
        # Tri-state is preserved end to end.  ``*_state`` is the answer;
        # the boolean beside it is None (not False) where the cell is unknown,
        # so a consumer that reads the boolean cannot turn "we do not know"
        # into "no" without noticing.
        simple, kaso, live = (mark_state(r.get("simple_post_office")),
                              mark_state(r.get("disadvantaged_area")),
                              operating_state(r))
        out.append({
            "official_identifier": r["official_identifier"],
            "section": r["section"],
            "name": r["name"]["normalized"],
            "address": r["address"]["normalized"],
            "address_normalized_for_geocoding": i["addr"],
            "pref": i["pref"], "isj_city": i["city"], "muni_code": i["code"],
            "lat": None if lat is None else round(lat, 6),
            "lon": None if lon is None else round(lon, 6),
            "crs": None if lat is None else "EPSG:6668 (JGD2011)",
            "accuracy": acc, "source": src, "match_detail": detail,
            "town_method": i["town_method"],
            "error": i["err"],
            "simple_post_office_state": simple,
            "disadvantaged_area_state": kaso,
            "operating_state": live,
            "simple_post_office": tri_bool(simple),
            "disadvantaged_area": tri_bool(kaso),
            "operating": tri_bool(live),
        })

    dst = ROOT / "data/silver/geocode/2026-06-30"
    um = [o for o in out if o["accuracy"] == "unmatched"]
    qa_rows = [{"kind": "info", "code": "input_qa", **input_qa}]
    for state_field in ("simple_post_office_state", "disadvantaged_area_state", "operating_state"):
        unknown = [o["official_identifier"] for o in out if o[state_field] == "unknown"]
        if unknown:
            qa_rows.append({"kind": "error", "code": "unknown_cell_state_preserved",
                            "field": state_field, "count": len(unknown),
                            "official_identifiers": unknown[:200]})
    summary = {"record_count": len(out), "accuracy": dict(cls), "unmatched": len(um),
               "input_qa": input_qa,
               "state_counts": {f: dict(collections.Counter(o[f] for o in out))
                                for f in ("simple_post_office_state",
                                          "disadvantaged_area_state", "operating_state")}}

    def _jsonl(rows):
        return lambda s: [s.write(json.dumps(x, ensure_ascii=False) + "\n") for x in rows]

    # Work diagnostics are auxiliary.  The formal records and QA below are
    # published as one sealed directory, so a rerun cannot leave new records
    # beside stale artifact manifests.
    commit_file_set({
        GEO / "geocode_classes.json": lambda s: s.write(json.dumps(dict(cls), ensure_ascii=False)),
        GEO / "geocode_input_qa.json": lambda s: s.write(json.dumps(summary, ensure_ascii=False, indent=1)),
        GEO / "unmatched.tsv": lambda s: [
            s.write(f"{o['official_identifier']}\t{o['name']}\t{o['address']}\t{o['error']}\n") for o in um],
    }, replace=True)
    try:
        commit_sealed_directory(
            dst, {"records.jsonl": _jsonl(out), "input_qa.jsonl": _jsonl(qa_rows)},
            artifact_type="facility_geocode", source_authority="auxiliary",
            evidence_status="observed", release_classification="internal_only",
            input_artifact_ids=[input_qa["anchor_artifact"]["artifact_id"]],
            preserve_existing=True, acknowledge_internal_use=True, strict_code=True)
    except ArtifactError as exc:
        print("output rejected:", type(exc).__name__, file=sys.stderr)
        return 2
    print("accuracy:", dict(cls))
    print(f"unmatched={len(um)}; wrote {dst/'records.jsonl'}  ({time.time()-t0:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
