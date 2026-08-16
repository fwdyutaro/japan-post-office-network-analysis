"""報告書が使う全数値を実データから算出し stats.json に落とす。

レビュー指摘 P1-1 への対応。build_report.py 側での数値の直書きを廃止し、
修正後データから常に再生成できるようにする。
"""
from __future__ import annotations
import csv, json, math, collections, re
from pathlib import Path

from project_paths import project_root

ROOT = project_root()
W = ROOT / "data/work"
OUT = W / "stats.json"
NORTH = {"01695", "01696", "01697", "01698", "01699", "01700"}
CLOSED = re.compile(r"[（(]一時閉鎖[）)]\s*$")


def load_units():
    rows = []
    for r in csv.DictReader((W / "muni/unit_counts.csv").open(encoding="utf-8")):
        if r["unit_code"] in NORTH:
            continue
        rows.append({"code": r["unit_code"], "pref": r["pref"], "label": r["label"],
                     "n13f": int(r["n2013_formal"]), "n13e": int(r["n2013_eff"]),
                     "n26f": int(r["n2026_formal"]), "n26e": int(r["n2026_eff"])})
    return rows


def load_pop():
    d = {}
    for r in csv.DictReader((ROOT / "data/external/population/municipality_population.csv").open(encoding="utf-8")):
        if r["census_year"] == "2020":
            try:
                d[r["code"]] = int(r["total_population"] or 0)
            except ValueError:
                pass
    return d


def load_kaso():
    return {r["code"]: r["designation_type"]
            for r in csv.DictReader((ROOT / "data/external/kaso/kaso_municipalities.csv").open(encoding="utf-8"))}


def gini(pairs):
    pairs = sorted([(p, n) for p, n in pairs if p > 0], key=lambda t: t[1] / t[0])
    P, N = sum(p for p, _ in pairs), sum(n for _, n in pairs)
    cp = cn = area = 0.0
    for p, n in pairs:
        pcp, pcn = cp, cn
        cp += p / P
        cn += n / N
        area += (cp - pcp) * (cn + pcn) / 2
    return 1 - 2 * area


def hoover(pairs):
    P, N = sum(p for p, _ in pairs), sum(n for _, n in pairs)
    return 0.5 * sum(abs(n / N - p / P) for p, n in pairs)


def theil(pairs):
    pairs = [(p, n) for p, n in pairs if p > 0 and n > 0]
    P, N = sum(p for p, _ in pairs), sum(n for _, n in pairs)
    return sum((n / N) * math.log((n / N) / (p / P)) for p, n in pairs)


def theil_decomp(rows, popmap, field):
    groups = collections.defaultdict(list)
    for r in rows:
        p = popmap.get(r["code"], 0)
        if p > 0 and r[field] > 0:
            groups[r["pref"]].append((p, r[field]))
    P = sum(p for g in groups.values() for p, _ in g)
    N = sum(n for g in groups.values() for _, n in g)
    between = within = 0.0
    for g in groups.values():
        gp, gn = sum(p for p, _ in g), sum(n for _, n in g)
        sn, sp = gn / N, gp / P
        between += sn * math.log(sn / sp)
        within += sn * theil(g)
    return between, within, between + within


def main():
    units = load_units()
    popmap = load_pop()
    kaso = load_kaso()
    covered = [r for r in units if popmap.get(r["code"], 0) > 0]
    S: dict = {"generated_from": "muni/unit_counts.csv + external + kyokusu_national.csv"}

    # --- 全国計（市区町村割当ベース） ---
    S["muni"] = {k: sum(r[k] for r in units) for k in ("n13f", "n13e", "n26f", "n26e")}
    S["legal_units"] = len(units)
    S["zero_units"] = {k: sum(1 for r in units if r[k] == 0) for k in ("n13f", "n13e", "n26f", "n26e")}
    S["zero_2013_eff_list"] = [f'{r["pref"]}{r["label"]}' for r in units if r["n13e"] == 0]
    S["one_office_2026"] = sum(1 for r in units if r["n26e"] == 1)

    # --- 公式局数表（実効系列の権威） ---
    off = list(csv.DictReader((W / "kyokusu_national.csv").open(encoding="utf-8")))
    S["official"] = {"first": off[0], "last": off[-1], "months": len(off)}

    # --- 2013年断面（P30 原データ） ---
    p13 = {"total": 0, "operating": 0, "simple": 0, "simple_closed": 0, "nonsimple_closed": 0}
    for line in (ROOT / "data/silver/p30/2013-11-30/records.jsonl").open(encoding="utf-8"):
        r = json.loads(line)
        nm = r.get("name_raw") or ""
        cl = bool(CLOSED.search(nm))
        simple = (r.get("type") == "18004")
        p13["total"] += 1
        p13["operating"] += 0 if cl else 1
        p13["simple"] += 1 if simple else 0
        if cl:
            p13["simple_closed" if simple else "nonsimple_closed"] += 1
    S["p30_2013"] = p13

    # --- 不均等指標 ---
    ineq = {}
    for f, lab in (("n13f", "2013_formal"), ("n13e", "2013_effective"),
                   ("n26f", "2026_formal"), ("n26e", "2026_effective")):
        pairs = [(popmap[r["code"]], r[f]) for r in covered]
        ineq[lab] = {"gini": gini(pairs), "hoover": hoover(pairs), "theil": theil(pairs)}
    for f, lab in (("n13e", "2013_effective"), ("n26e", "2026_effective")):
        b, w, t = theil_decomp(covered, popmap, f)
        ineq[lab].update({"theil_between": b, "theil_within": w,
                          "between_share": b / t * 100, "within_share": w / t * 100})
    S["inequality"] = ineq

    # --- 過疎地比較（4定義） ---
    defs = {"all885": set(kaso),
            "full_only": {c for c, t in kaso.items() if t == "全部過疎"},
            "full_plus_deemed": {c for c, t in kaso.items() if t in ("全部過疎", "みなし過疎")},
            "partial_only": {c for c, t in kaso.items() if t == "一部過疎"}}
    S["kaso"] = {}
    for key, members in defs.items():
        g = collections.defaultdict(lambda: dict.fromkeys(("n", "pop", "n13f", "n13e", "n26f", "n26e"), 0))
        for r in units:
            d = g["kaso" if r["code"] in members else "other"]
            d["n"] += 1
            d["pop"] += popmap.get(r["code"], 0)
            for f in ("n13f", "n13e", "n26f", "n26e"):
                d[f] += r[f]
        rec = {}
        for side in ("kaso", "other"):
            d = g[side]
            rec[side] = {**d,
                         "rate_formal": (d["n26f"] - d["n13f"]) / d["n13f"] * 100,
                         "rate_eff": (d["n26e"] - d["n13e"]) / d["n13e"] * 100,
                         "pop_per_office_13": d["pop"] / d["n13e"],
                         "pop_per_office_26": d["pop"] / d["n26e"]}
        rec["ratio_eff"] = rec["kaso"]["rate_eff"] / rec["other"]["rate_eff"]
        S["kaso"][key] = rec
    S["kaso_counts"] = collections.Counter(kaso.values())

    # --- 都道府県 ---
    pref = collections.defaultdict(lambda: {"n13e": 0, "n26e": 0, "pop": 0, "units": 0, "kaso": 0})
    for r in units:
        d = pref[r["pref"]]
        d["n13e"] += r["n13e"]; d["n26e"] += r["n26e"]
        d["pop"] += popmap.get(r["code"], 0); d["units"] += 1
        if r["code"] in kaso:
            d["kaso"] += 1
    plist = [{"pref": p, **d, "chg_pct": (d["n26e"] - d["n13e"]) / d["n13e"] * 100,
              "per10k_26": d["n26e"] / d["pop"] * 10000 if d["pop"] else 0,
              "kaso_share": d["kaso"] / d["units"] * 100}
             for p, d in pref.items() if d["n13e"]]
    plist.sort(key=lambda x: x["chg_pct"])
    S["prefectures"] = plist

    # --- 減少上位 ---
    dec = sorted(units, key=lambda r: r["n26e"] - r["n13e"])[:12]
    S["top_decliners"] = [{"label": f'{r["pref"]}{r["label"]}', "n13e": r["n13e"], "n26e": r["n26e"],
                           "delta": r["n26e"] - r["n13e"], "pop": popmap.get(r["code"], 0),
                           "kaso": r["code"] in kaso} for r in dec]

    # --- アニメーションのセル階級分布 ---
    anim = json.loads((W / "animation_data.json").read_text(encoding="utf-8"))
    last = anim["frames"][-1]
    hist = collections.Counter(last)
    n = len(last)
    S["animation"] = {"cells": n, "months": len(anim["months"]),
                      "hist": {k: hist.get(str(k), 0) for k in range(8)},
                      "pct": {k: hist.get(str(k), 0) / n * 100 for k in range(8)},
                      "decline5plus": sum(hist.get(str(k), 0) for k in (0, 1, 2)),
                      "decline10plus": sum(hist.get(str(k), 0) for k in (0, 1))}

    # --- 月次復元 ---
    mf = json.loads((W / "monthly_formal.json").read_text(encoding="utf-8"))
    S["monthly"] = {"first": mf[0], "last": mf[-1], "n": len(mf)}
    rec = json.loads((W / "reconcile.json").read_text(encoding="utf-8"))
    S["reconcile"] = {"first": rec[0], "last": rec[-1], "n": len(rec),
                      "max_unlisted": max(r["unlisted"] for r in rec),
                      "violations": sum(1 for r in rec if r["unlisted"] > r["cl_simple"])}

    # --- アクセシビリティ（3系列の幅） ---
    S["accessibility"] = json.loads((W / "accessibility_corrected.json").read_text(encoding="utf-8"))

    # --- 確定統計 ---
    cm = json.loads((ROOT / "data/silver/confirmed_events/2026-06-30/metadata.json").read_text(encoding="utf-8"))
    S["confirm"] = {k: cm.get(k) for k in
                    ("input_event_count", "chain_count", "promoted_chain_count",
                     "confirmed_event_count", "announced_event_count", "future_event_count",
                     "not_promoted_reason_counts", "qa_error_count")}

    OUT.write_text(json.dumps(S, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"wrote {OUT}")
    print(f"  法的単位 {S['legal_units']}  局数0: 2026形式{S['zero_units']['n26f']} 実効{S['zero_units']['n26e']}"
          f" / 2013実効{S['zero_units']['n13e']}")
    print(f"  市区町村計 2013 {S['muni']['n13f']:,}/{S['muni']['n13e']:,}"
          f"  2026 {S['muni']['n26f']:,}/{S['muni']['n26e']:,}")
    print(f"  公式 最終 {S['official']['last']['month']} 総計{S['official']['last']['total']}"
          f" 営業中{S['official']['last']['operating']}")
    print(f"  Gini 2013実効 {ineq['2013_effective']['gini']:.4f} / 2026実効 {ineq['2026_effective']['gini']:.4f}")
    print(f"  過疎比(all885) {S['kaso']['all885']['ratio_eff']:.2f}")
    print(f"  アニメ 5%以上減 {S['animation']['decline5plus']}/{S['animation']['cells']}")


if __name__ == "__main__":
    main()
