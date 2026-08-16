"""市区町村単位の不均等指標と過疎地比較（2013 vs 2026）.

前提: data/work/muni/unit_counts.csv が muni_analysis.py により生成済み。
"""
from __future__ import annotations
import csv, math, collections
from pathlib import Path

from project_paths import project_root

ROOT = project_root()
UNITS = ROOT / "data/work/muni/unit_counts.csv"
POP = ROOT / "data/external/population/municipality_population.csv"
KASO = ROOT / "data/external/kaso/kaso_municipalities.csv"
NORTHERN = {"01695", "01696", "01697", "01698", "01699", "01700"}


def load_units():
    rows = []
    with UNITS.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["unit_code"] in NORTHERN:
                continue
            rows.append({
                "code": r["unit_code"], "pref": r["pref"], "label": r["label"],
                "n13f": int(r["n2013_formal"]), "n13e": int(r["n2013_eff"]),
                "n26f": int(r["n2026_formal"]), "n26e": int(r["n2026_eff"]),
            })
    return rows


def load_pop():
    """Population by legal unit.  Ward rows are summed into the parent city."""
    per_code = {}
    with POP.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["census_year"] != "2020":
                continue
            try:
                tot = int(r["total_population"] or 0)
            except ValueError:
                continue
            old = int(r["pop_65plus"] or 0) if (r["pop_65plus"] or "").strip() else 0
            per_code[r["code"]] = (tot, old, r["city_name"])
    return per_code


def load_kaso():
    d = {}
    with KASO.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            d[r["code"]] = r["designation_type"]
    return d


def gini(pairs):
    """Gini of office counts against population share (Lorenz over population)."""
    pairs = [(p, n) for p, n in pairs if p > 0]
    pairs.sort(key=lambda t: t[1] / t[0])  # ascending offices-per-capita
    P = sum(p for p, _ in pairs)
    N = sum(n for _, n in pairs)
    if P == 0 or N == 0:
        return float("nan")
    cp = cn = 0.0
    area = 0.0
    for p, n in pairs:
        prev_cp, prev_cn = cp, cn
        cp += p / P
        cn += n / N
        area += (cp - prev_cp) * (cn + prev_cn) / 2
    return 1 - 2 * area


def theil(pairs):
    """Theil T of offices relative to population."""
    pairs = [(p, n) for p, n in pairs if p > 0 and n > 0]
    P = sum(p for p, _ in pairs)
    N = sum(n for _, n in pairs)
    t = 0.0
    for p, n in pairs:
        s = n / N
        t += s * math.log(s / (p / P))
    return t


def theil_decompose(rows, key, popmap, field):
    """Between/within decomposition by group key."""
    groups = collections.defaultdict(list)
    for r in rows:
        pop = popmap.get(r["code"], (0, 0, ""))[0]
        if pop > 0 and r[field] > 0:
            groups[key(r)].append((pop, r[field]))
    P = sum(p for g in groups.values() for p, _ in g)
    N = sum(n for g in groups.values() for _, n in g)
    between = 0.0
    within = 0.0
    for g in groups.values():
        gp = sum(p for p, _ in g)
        gn = sum(n for _, n in g)
        sn, sp = gn / N, gp / P
        between += sn * math.log(sn / sp)
        within += sn * theil(g)
    return between, within, between + within


def hoover(pairs):
    P = sum(p for p, _ in pairs)
    N = sum(n for _, n in pairs)
    return 0.5 * sum(abs(n / N - p / P) for p, n in pairs)


def main():
    rows = load_units()
    popmap = load_pop()
    kaso = load_kaso()

    covered = [r for r in rows if popmap.get(r["code"], (0,))[0] > 0]
    print(f"法的単位 {len(rows)} / 人口データあり {len(covered)}")
    missing = [r for r in rows if popmap.get(r["code"], (0,))[0] == 0]
    if missing:
        print(f"  人口欠測 {len(missing)}件 例: " + ", ".join(f"{m['pref']}{m['label']}" for m in missing[:8]))

    print("\n=== 全国計（北方領土6村を除く） ===")
    for f, lbl in (("n13f", "2013形式"), ("n13e", "2013実効"), ("n26f", "2026形式"), ("n26e", "2026実効")):
        print(f"  {lbl}: {sum(r[f] for r in rows):,}")

    print("\n=== 不均等指標（人口2020を分母、市区町村単位） ===")
    print(f"{'指標':<12}{'2013形式':>12}{'2013実効':>12}{'2026形式':>12}{'2026実効':>12}")
    for name, fn in (("Gini", gini), ("Hoover", hoover), ("Theil", theil)):
        vals = []
        for f in ("n13f", "n13e", "n26f", "n26e"):
            pairs = [(popmap[r["code"]][0], r[f]) for r in covered]
            vals.append(fn(pairs))
        print(f"{name:<12}" + "".join(f"{v:>12.4f}" for v in vals))

    print("\n=== Theil分解（都道府県間 / 県内） ===")
    for f, lbl in (("n13e", "2013実効"), ("n26e", "2026実効")):
        b, w, t = theil_decompose(covered, lambda r: r["pref"], popmap, f)
        print(f"  {lbl}: 全体 {t:.4f} = 県間 {b:.4f} ({b/t*100:.1f}%) + 県内 {w:.4f} ({w/t*100:.1f}%)")

    print("\n=== 過疎地（過疎法指定・現行）vs 非過疎地 ===")
    print("  ※ 現行の過疎法指定を両時点に適用した比較。施行規則4条5項の公式『過疎地』は")
    print("     離島・山村・半島等7類型を含むため、この区分とは一致しない。")
    grp = collections.defaultdict(lambda: {"n": 0, "n13f": 0, "n13e": 0, "n26f": 0, "n26e": 0, "pop": 0})
    for r in rows:
        g = "過疎地" if r["code"] in kaso else "非過疎地"
        d = grp[g]
        d["n"] += 1
        d["pop"] += popmap.get(r["code"], (0,))[0]
        for f in ("n13f", "n13e", "n26f", "n26e"):
            d[f] += r[f]
    print(f"{'区分':<10}{'団体数':>8}{'人口2020':>14}{'2013形式':>10}{'2026形式':>10}{'増減率':>9}{'2013実効':>10}{'2026実効':>10}{'増減率':>9}")
    for g in ("過疎地", "非過疎地"):
        d = grp[g]
        rf = (d["n26f"] - d["n13f"]) / d["n13f"] * 100
        re_ = (d["n26e"] - d["n13e"]) / d["n13e"] * 100
        print(f"{g:<10}{d['n']:>8,}{d['pop']:>14,}{d['n13f']:>10,}{d['n26f']:>10,}{rf:>8.1f}%{d['n13e']:>10,}{d['n26e']:>10,}{re_:>8.1f}%")

    # 局あたり人口
    print("\n=== 1局あたり人口（2020年人口 / 実効局数） ===")
    for g in ("過疎地", "非過疎地"):
        d = grp[g]
        print(f"  {g}: 2013 {d['pop']/d['n13e']:,.0f}人/局 → 2026 {d['pop']/d['n26e']:,.0f}人/局")

    # 減少が大きい市区町村
    print("\n=== 実効局数の減少が大きい市区町村 上位15 ===")
    dec = sorted(rows, key=lambda r: r["n26e"] - r["n13e"])[:15]
    print(f"{'団体':<22}{'2013実効':>9}{'2026実効':>9}{'増減':>7}{'人口2020':>11}{'過疎':>6}")
    for r in dec:
        pop = popmap.get(r["code"], (0,))[0]
        print(f"{r['pref']+r['label']:<22}{r['n13e']:>9}{r['n26e']:>9}{r['n26e']-r['n13e']:>7}{pop:>11,}{('○' if r['code'] in kaso else ''):>6}")

    # 実効ゼロ・1局の団体
    z26 = [r for r in rows if r["n26e"] == 0]
    o26 = [r for r in rows if r["n26e"] == 1]
    print(f"\n2026 実効0の団体: {len(z26)}  / 実効1局のみの団体: {len(o26)}")
    for r in o26[:20]:
        pop = popmap.get(r["code"], (0,))[0]
        print(f"    {r['pref']}{r['label']}  人口{pop:,}  過疎{'○' if r['code'] in kaso else '−'}")


if __name__ == "__main__":
    main()
