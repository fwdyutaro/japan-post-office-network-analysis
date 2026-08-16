"""施行規則4条2項3号の基準時（2012年10月1日）と、本稿が使う2013年11月末との差を測る。

問い: 実効局数の比較を2013年断面で代用してよいか。
"""
from __future__ import annotations
import json
from pathlib import Path

from project_paths import work_dir

W = work_dir()
mf = json.loads((W / "monthly_formal.json").read_text(encoding="utf-8"))
by = {r["m"]: r for r in mf}

BASE = "2012-10"   # 平成24年改正法の施行（2012-10-01）を含む月
P30 = "2013-11"    # 国土数値情報P30の基準（2013-11-30）
LAST = "2026-06"   # 履歴アンカー

print("=== 形式系列（復元、届出一覧の本体ベース）===")
for m in (BASE, P30, LAST):
    r = by[m]
    print(f"  {m}: 総{r['t']:,}  郵便局{r['p']:,}  会社の営業所{r['c']:,}")

a, b, c = by[BASE], by[P30], by[LAST]
print(f"\n基準時({BASE}) → P30断面({P30}) の14か月:")
print(f"  総数 {a['t']:,} → {b['t']:,}  差 {b['t']-a['t']:+,}  ({(b['t']-a['t'])/a['t']*100:+.2f}%)")
print(f"\nP30断面({P30}) → アンカー({LAST}) の151か月:")
print(f"  総数 {b['t']:,} → {c['t']:,}  差 {c['t']-b['t']:+,}  ({(c['t']-b['t'])/b['t']*100:+.2f}%)")
print(f"\n基準時({BASE}) → アンカー({LAST}) の全期間:")
print(f"  総数 {a['t']:,} → {c['t']:,}  差 {c['t']-a['t']:+,}  ({(c['t']-a['t'])/a['t']*100:+.2f}%)")

gap = abs(b["t"] - a["t"])
total = abs(c["t"] - a["t"])
print(f"\n基準時とP30断面のずれ {gap} 局は、基準時からの総変化 {total} 局の {gap/total*100:.1f}%")

# 月次の増減が最大だった月（基準時〜P30断面の間に大きな段差が無いか）
seg = [r for r in mf if BASE <= r["m"] <= P30]
deltas = [(seg[i]["m"], seg[i]["t"] - seg[i-1]["t"]) for i in range(1, len(seg))]
deltas.sort(key=lambda x: x[1])
print(f"\n{BASE}〜{P30} の月次増減（大きい順に3件・小さい順に3件）:")
for m, d in deltas[:3] + deltas[-3:]:
    print(f"    {m}: {d:+d}")

print("\n=== 実効系列で2012年10月を出せるか ===")
print("  出せない。一時閉鎖は法6条2項の届出事項ではないため変更届に記録が無く、")
print("  休止状態が分かるのは次の3系統のみ:")
print("    - 2013-11-30: 国土数値情報P30の名称末尾『（一時閉鎖）』表記")
print("    - 2018-10以降: 日本郵便『郵便局局数表』の閉鎖中欄（都道府県別の集計値）")
print("    - 2026-06-30: 現況一覧の営業状態欄＋脚注653局")
print("  したがって2012年10月時点の実効局数を復元する材料は無い。")
