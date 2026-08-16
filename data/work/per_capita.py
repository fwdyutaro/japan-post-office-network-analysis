"""過疎地／非過疎地の人口当たり局数を、集計値と分布の両方で出す。

集計比（総人口÷総局数）は大きな自治体に引きずられるため、
市区町村単位の分布（中央値・四分位）も併せて見る。
"""
from __future__ import annotations
import csv, json, statistics, collections
from pathlib import Path

from project_paths import project_root

ROOT = project_root()
W = ROOT / "data/work"
NORTH = {"01695", "01696", "01697", "01698", "01699", "01700"}

units = [r for r in csv.DictReader((W / "muni/unit_counts.csv").open(encoding="utf-8"))
         if r["unit_code"] not in NORTH]
pop = {}
for r in csv.DictReader((ROOT / "data/external/population/municipality_population.csv").open(encoding="utf-8")):
    if r["census_year"] == "2020":
        try:
            pop[r["code"]] = int(r["total_population"] or 0)
        except ValueError:
            pass
kaso = {r["code"]: r["designation_type"]
        for r in csv.DictReader((ROOT / "data/external/kaso/kaso_municipalities.csv").open(encoding="utf-8"))}

DEFS = {
    "全885団体（全部＋一部＋みなし）": set(kaso),
    "全部過疎のみ": {c for c, t in kaso.items() if t == "全部過疎"},
    "全部過疎＋みなし": {c for c, t in kaso.items() if t in ("全部過疎", "みなし過疎")},
}


def summarise(members, field):
    g = collections.defaultdict(lambda: {"n": 0, "pop": 0, "off": 0, "rates": []})
    for u in units:
        code = u["unit_code"]
        p = pop.get(code, 0)
        n = int(u[field])
        d = g["過疎地" if code in members else "非過疎地"]
        d["n"] += 1
        d["pop"] += p
        d["off"] += n
        if p > 0:
            d["rates"].append(n / p * 10000)   # 人口1万人あたり局数
    return g


def line(lbl, d):
    r = sorted(d["rates"])
    q1 = statistics.quantiles(r, n=4)[0]
    q3 = statistics.quantiles(r, n=4)[2]
    agg = d["off"] / d["pop"] * 10000
    ppo = d["pop"] / d["off"]
    return (f"  {lbl:<8}団体{d['n']:>5,}  人口{d['pop']:>13,}  局{d['off']:>7,}  "
            f"集計{agg:>6.2f}局/万人  1局あたり{ppo:>8,.0f}人  "
            f"中央値{statistics.median(r):>6.2f}  四分位{q1:>5.2f}–{q3:>5.2f}")


print("=" * 132)
print("人口1万人あたりの実効局数（2026年6月、2020年国勢調査人口）")
print("=" * 132)
for name, members in DEFS.items():
    g = summarise(members, "n2026_eff")
    print(f"\n【{name}】")
    for k in ("過疎地", "非過疎地"):
        print(line(k, g[k]))
    ka, no = g["過疎地"], g["非過疎地"]
    print(f"  → 集計比 {(ka['off']/ka['pop'])/(no['off']/no['pop']):.2f}倍   "
          f"中央値比 {statistics.median(ka['rates'])/statistics.median(no['rates']):.2f}倍")

print("\n" + "=" * 132)
print("2013年との比較（全885団体ベース、実効）")
print("=" * 132)
members = set(kaso)
for year, field in (("2013", "n2013_eff"), ("2026", "n2026_eff")):
    g = summarise(members, field)
    print(f"\n[{year}]")
    for k in ("過疎地", "非過疎地"):
        print(line(k, g[k]))

# 分布の重なり具合
g26 = summarise(set(kaso), "n2026_eff")
ka = sorted(g26["過疎地"]["rates"])
no = sorted(g26["非過疎地"]["rates"])
below = sum(1 for x in ka if x <= statistics.median(no))
print(f"\n過疎地のうち、非過疎地の中央値({statistics.median(no):.2f})以下しか局が無い団体: "
      f"{below} / {len(ka)} ({below/len(ka)*100:.1f}%)")
above = sum(1 for x in no if x >= statistics.median(ka))
print(f"非過疎地のうち、過疎地の中央値({statistics.median(ka):.2f})以上ある団体: "
      f"{above} / {len(no)} ({above/len(no)*100:.1f}%)")
