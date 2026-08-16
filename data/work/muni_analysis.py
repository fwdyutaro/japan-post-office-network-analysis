"""市区町村単位の集計と施行規則4条1項（各市町村に1以上）の充足検証.

法的単位の注意:
  施行規則4条1項の単位は「市町村（特別区を含む）」。
  - 東京都の特別区(13101-13123)は、それぞれが独立の単位。
  - 政令指定都市の行政区は独立の単位ではない。親市に集約する。
"""
from __future__ import annotations
import csv, json, re, sys, collections
from pathlib import Path

from project_paths import project_root

ROOT = project_root()
CODES = ROOT / "data/external/municipality/municipality_codes.csv"
P30 = ROOT / "data/silver/p30/2013-11-30/records.jsonl"
CUR = ROOT / "data/silver/current_list/2026-06-30/records.jsonl"
OUT = ROOT / "data/work/muni"
OUT.mkdir(parents=True, exist_ok=True)

TOKYO_WARDS = {f"131{n:02d}" for n in range(1, 24)}  # 13101-13123
PREF_RE = re.compile(r"^(北海道|東京都|(?:京都|大阪)府|.{2,3}県)")
CLOSED_RE = re.compile(r"[（(]一時閉鎖[）)]\s*$")
# A 郡 (district) or 島 (island) name can sit between the prefecture and the
# municipality; the code table omits it.
INTERVENING_RE = re.compile(r"^(.{1,6}?[郡島])")
# The notification list uses 旧字体 in places where the code table uses 新字体.
KYUJI = str.maketrans({"惠": "恵", "澤": "沢", "圓": "円", "榮": "栄", "邊": "辺",
                       "邉": "辺", "瀨": "瀬", "齋": "斎", "齊": "斉", "藪": "薮",
                       "曾": "曽", "德": "徳", "假": "仮", "眞": "真", "淺": "浅",
                       "檮": "梼"})

# Municipality codes present in P30-13 (2013) that no longer exist.  Mapped to
# the CURRENT legal unit so the two cross-sections are comparable on today's
# boundaries.  (§11.2 also asks for a 2013-boundary view; that is a separate cut.)
HISTORICAL_CODES = {
    "22131": "22130", "22132": "22130", "22133": "22130", "22134": "22130",
    "22135": "22130", "22136": "22130", "22137": "22130",  # 旧・浜松市の7行政区→浜松市
    "03305": "03216",  # 滝沢村 → 滝沢市 (2014)
    "04423": "04216",  # 富谷町 → 富谷市 (2016)
    "40305": "40231",  # 那珂川町 → 那珂川市 (2018)
    "09367": "09203",  # 岩舟町 → 栃木市へ編入 (2014)
}

# Under Russian administration; a post office cannot be established.
NORTHERN_TERRITORIES = {"01695", "01696", "01697", "01698", "01699", "01700"}


def norm(s: str) -> str:
    return (s or "").translate(KYUJI)


def load_codes():
    """Return (all_rows, designated_city_wards, legal_units)."""
    rows = []
    with CODES.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            code = (r["code"] or "").strip()
            if len(code) != 5 or not code.isdigit():
                continue
            rows.append({"code": code, "pref": (r["pref_name"] or "").strip(),
                         "city": (r["city_name"] or "").strip()})
    # Prefecture aggregate rows end with 000; designated-city parent rows end with 100..
    munis = [r for r in rows if not r["code"].endswith("000")]
    # Designated-city parent codes are NOT a fixed offset (静岡市=22100 but
    # 浜松市=22130), so derive the parent from the name: a ward row is named
    # "<parent city>…区".  Tokyo's special wards have no parent city row.
    by_name = {(r["pref"], r["city"]): r for r in munis}
    parents: dict[str, dict] = {}
    for r in munis:
        m = re.match(r"^(.+?市)(.+区)$", r["city"])
        if m:
            p = by_name.get((r["pref"], m.group(1)))
            if p:
                parents[r["code"]] = p
    return rows, munis, parents


def legal_unit(code: str, name: str, parents: dict) -> tuple[str, str]:
    """Map a 5-digit code to its 4条1項 legal unit (code, label)."""
    if code in TOKYO_WARDS:
        return code, name
    p = parents.get(code)
    if p:  # ward of a designated city -> roll up to the parent city
        return p["code"], p["city"]
    return code, name


def build_matcher(munis):
    """(pref, city) and longest-prefix city lookup per prefecture."""
    by_pref = collections.defaultdict(list)
    for m in munis:
        if m["city"]:
            by_pref[m["pref"]].append(m)
    for p in by_pref:
        by_pref[p].sort(key=lambda m: len(m["city"]), reverse=True)
    return by_pref


def assign_2026(by_pref, parents, code_name):
    recs = [json.loads(l) for l in CUR.open(encoding="utf-8")]
    out, unmatched = [], []
    for r in recs:
        addr = r["address"]["normalized"]
        m = PREF_RE.match(addr)
        if not m:
            unmatched.append((r["name"]["normalized"], addr, "no_pref"))
            continue
        pref = m.group(1)
        rest = norm(addr[len(pref):])
        hit = None
        # Candidates are sorted longest-city-name first, so the first hit wins.
        for cand in by_pref.get(pref, []):
            if rest.startswith(norm(cand["city"])):
                hit = cand
                break
        if hit is None:
            # An intervening 郡/島 token may sit before the municipality name.
            # Try every possible strip point, longest first, because e.g.
            # "鹿児島郡三島村" must strip "鹿児島郡", not "鹿児島".
            cuts = [i + 1 for i, ch in enumerate(rest[:8]) if ch in "郡島"]
            for cut in sorted(cuts, reverse=True):
                rest2 = rest[cut:]
                for cand in by_pref.get(pref, []):
                    if rest2.startswith(norm(cand["city"])):
                        hit = cand
                        break
                if hit:
                    break
        if hit is None:
            unmatched.append((r["name"]["normalized"], addr, "no_city"))
            continue
        unit, label = legal_unit(hit["code"], hit["city"], parents)
        out.append({
            "name": r["name"]["normalized"], "identifier": r["official_identifier"],
            "raw_code": hit["code"], "unit_code": unit, "pref": pref, "unit_label": label,
            "section": r["section"],
            "simple": r["simple_post_office"]["normalized"] == "yes",
            "kaso": r["disadvantaged_area"]["normalized"] == "yes",
            "operating": r["operating_status"]["normalized"] == "operating",
        })
    return out, unmatched


def assign_2013(parents, code_name):
    recs = [json.loads(l) for l in P30.open(encoding="utf-8")]
    out, unmatched = [], []
    for r in recs:
        code = str(r.get("administrative_area") or "")
        code = HISTORICAL_CODES.get(code, code)
        if len(code) != 5 or code not in code_name:
            unmatched.append((r.get("name_raw"), code, "unknown_code"))
            continue
        unit, label = legal_unit(code, code_name[code]["city"], parents)
        nm = r.get("name_raw") or ""
        out.append({
            "name": CLOSED_RE.sub("", nm).strip(), "raw_code": code, "unit_code": unit,
            "pref": code_name[code]["pref"], "unit_label": label,
            "simple": r.get("type") == "18004",
            "operating": not CLOSED_RE.search(nm),
            "lat": r.get("latitude"), "lon": r.get("longitude"),
        })
    return out, unmatched


def main():
    rows, munis, parents = load_codes()
    code_name = {m["code"]: m for m in munis}
    by_pref = build_matcher(munis)

    # The universe of 4条1項 legal units
    units = {}
    for m in munis:
        u, label = legal_unit(m["code"], m["city"], parents)
        # skip designated-city parent rows being double counted by their own wards
        units[u] = {"code": u, "pref": m["pref"], "label": label}
    print(f"法的単位（市町村＋特別区、政令市の区は親市に集約）: {len(units)}")

    a26, u26 = assign_2026(by_pref, parents, code_name)
    a13, u13 = assign_2013(parents, code_name)
    print(f"2026 割当: {len(a26)} / 未割当 {len(u26)}")
    for x in u26[:5]:
        print("   未割当:", x)
    print(f"2013 割当: {len(a13)} / 未割当 {len(u13)}")
    for x in u13[:5]:
        print("   未割当:", x)

    def counts(recs, key):
        c = collections.Counter()
        for r in recs:
            if key(r):
                c[r["unit_code"]] += 1
        return c

    c26_formal = counts(a26, lambda r: True)
    c26_eff = counts(a26, lambda r: r["operating"])
    c13_formal = counts(a13, lambda r: True)
    c13_eff = counts(a13, lambda r: r["operating"])

    def zeros(c):
        """Zero-office legal units, excluding the Northern Territories."""
        return sorted(u for u in units if c.get(u, 0) == 0 and u not in NORTHERN_TERRITORIES)

    z26f, z26e = zeros(c26_formal), zeros(c26_eff)
    z13f, z13e = zeros(c13_formal), zeros(c13_eff)
    print("\n=== 施行規則4条1項: 局数0の法的単位 ===")
    print(f"2026 形式（一覧本体23,459のみ、653は個体不明のため未算入）: {len(z26f)}")
    for u in z26f:
        print(f"    {u} {units[u]['pref']}{units[u]['label']}  (実効でも0: {u in z26e})")
    print(f"2026 実効（一時閉鎖を除く）: {len(z26e)}")
    for u in z26e:
        if u not in z26f:
            print(f"    形式1以上だが実効0 → {u} {units[u]['pref']}{units[u]['label']}")
    print(f"2013 形式: {len(z13f)} / 2013 実効: {len(z13e)}")
    for u in z13e:
        print(f"    2013実効0: {u} {units[u]['pref']}{units[u]['label']} (形式でも0: {u in z13f})")

    # persist
    with (OUT / "assign_2026.jsonl").open("w", encoding="utf-8") as fh:
        for r in a26:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with (OUT / "assign_2013.jsonl").open("w", encoding="utf-8") as fh:
        for r in a13:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with (OUT / "unit_counts.csv").open("w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["unit_code", "pref", "label", "n2013_formal", "n2013_eff", "n2026_formal", "n2026_eff"])
        for u in sorted(units):
            w.writerow([u, units[u]["pref"], units[u]["label"],
                        c13_formal.get(u, 0), c13_eff.get(u, 0),
                        c26_formal.get(u, 0), c26_eff.get(u, 0)])
    print(f"\n書き出し: {OUT}")

    # headline aggregates
    print("\n=== 全国計 ===")
    print(f"2013 形式 {sum(c13_formal.values())} / 実効 {sum(c13_eff.values())}")
    print(f"2026 形式 {sum(c26_formal.values())} / 実効 {sum(c26_eff.values())}")
    kaso = collections.Counter()
    for r in a26:
        kaso[(r["kaso"], r["operating"])] += 1
    print("2026 過疎地×稼働:", dict(kaso))


if __name__ == "__main__":
    main()
