"""研究成果レポート（HTML）を実測値から生成する。

設計方針:
  見出しは明朝（法令文書の語感）、本文はゴシック、数値はタブular numerals。
  形式系列＝ink blue / 実効系列＝crimson の対で「届出上」と「実際」の乖離を一貫して示す。
"""
from __future__ import annotations
import csv, json, html
from pathlib import Path

from project_paths import project_root

ROOT = project_root()
W = ROOT / "data/work"
OUT = W / "postal_bias_report.html"

S = json.loads((W / "stats.json").read_text(encoding="utf-8"))
pref = S["prefectures"]
lor = json.loads((W / "lorenz.json").read_text(encoding="utf-8"))
cho = json.loads((W / "choropleth.json").read_text(encoding="utf-8"))

OFF_FIRST, OFF_LAST = S["official"]["first"], S["official"]["last"]
P13 = S["p30_2013"]
# 2013年は国土数値情報、2026年は公式局数表。いずれも休止局を含む同一定義。
F13, E13 = P13["total"], P13["operating"]
F26, E26 = int(OFF_LAST["total"]), int(OFF_LAST["operating"])
CLOSED13 = P13["total"] - P13["operating"]
CLOSED26 = F26 - E26
SIMPLE13, SIMPLE13C = P13["simple"], P13["simple_closed"]
SIMPLE26C = int(OFF_LAST["cl_simple"])
SIMPLE26 = int(OFF_LAST["op_simple"]) + SIMPLE26C


def pct(a, b):
    return (b - a) / a * 100


# 減少率の階級。ほぼ全団体が減少側なので、増加のみ別色を当てる。
BINS = [(0.001, None, "var(--inc)", "増加"),
        (-5.0, 0.001, "var(--d1)", "0〜−5%"),
        (-10.0, -5.0, "var(--d2)", "−5〜−10%"),
        (-20.0, -10.0, "var(--d3)", "−10〜−20%"),
        (None, -20.0, "var(--d4)", "−20%超の減少")]


def bin_color(chg):
    if chg is None:
        return "var(--nodata)"
    for lo, hi, col, _ in BINS:
        if (lo is None or chg > lo) and (hi is None or chg <= hi):
            return col
    return "var(--nodata)"


def map_chart() -> str:
    m, o, U = cho["main"], cho["oki"], cho["units"]
    # 本土パスは丸め・線幅で数px はみ出すことがあるので余白を足す
    vw, vh = m["w"] + 6, m["h"] + 6
    g = [f'<svg viewBox="0 0 {vw} {vh}" role="img" '
         f'aria-label="市区町村別の実効局数増減率のコロプレス図">']
    g.append('<g class="mapg">')
    for unit, d in m["paths"].items():
        u = U.get(unit)
        col = bin_color(u["chg"] if u else None)
        g.append(f'<path d="{d}" fill="{col}"/>')
    g.append("</g>")
    # 沖縄・先島の別枠。左下に置くと九州に重なるため、右下（本土の描画が無い太平洋側）へ。
    ox, oy = vw - o["w"] - 24, vh - o["h"] - 26
    g.append(f'<g transform="translate({ox},{oy})">')
    g.append(f'<rect x="-6" y="-6" width="{o["w"]+12}" height="{o["h"]+12}" class="inset"/>')
    for unit, d in o["paths"].items():
        u = U.get(unit)
        g.append(f'<path d="{d}" fill="{bin_color(u["chg"] if u else None)}"/>')
    g.append(f'<text x="0" y="{o["h"]+4}" class="sub">沖縄（別縮尺）</text></g>')
    # 凡例。右上は北海道、左下は九州に重なるので、日本海側（本土の描画が無い）に置く。
    lx, ly = 10, 206
    g.append(f'<g transform="translate({lx},{ly})">')
    g.append('<text x="0" y="0" class="sub">実効局数の増減 2013→2026</text>')
    for i, (_, _, col, lab) in enumerate(BINS):
        y = 12 + i * 17
        g.append(f'<rect x="0" y="{y}" width="15" height="11" fill="{col}"/>')
        g.append(f'<text x="22" y="{y+9.5}" class="tick">{lab}</text>')
    g.append("</g></svg>")
    return "".join(g)


ANCHOR_MONTH = "2026-06"  # 履歴アンカー 2026-06-30。これより後は持越しなので出さない。
_monthly_all = json.loads((W / "monthly_formal.json").read_text(encoding="utf-8"))
monthly = [r for r in _monthly_all if r["m"] <= ANCHOR_MONTH]
MONTHS_DROPPED = len(_monthly_all) - len(monthly)


def monthly_chart() -> str:
    w, h, pl, pr, pt, pb = 700, 300, 62, 132, 22, 40
    lo, hi = 23300, 24450
    n = len(monthly)
    def x(i): return pl + i / (n - 1) * (w - pl - pr)
    def y(v): return pt + (hi - v) / (hi - lo) * (h - pt - pb)
    g = [f'<svg viewBox="0 0 {w} {h}" role="img" '
         f'aria-label="{monthly[0]["m"]}から{monthly[-1]["m"]}までの届出上の局数の月次推移">']
    for gv in range(23400, 24401, 200):
        g.append(f'<line x1="{pl}" y1="{y(gv):.1f}" x2="{w-pr}" y2="{y(gv):.1f}" class="grid"/>')
        g.append(f'<text x="{pl-9}" y="{y(gv)+4:.1f}" class="tick end">{gv:,}</text>')
    for i, r in enumerate(monthly):
        if r["m"].endswith("-04") and int(r["m"][:4]) % 2 == 0:
            g.append(f'<text x="{x(i):.1f}" y="{h-16}" class="tick mid">{r["m"][:4]}</text>')
    pts = " ".join(f"{x(i):.1f},{y(r['t']):.1f}" for i, r in enumerate(monthly))
    g.append(f'<polyline points="{pts}" fill="none" stroke="var(--statute)" stroke-width="2"/>')
    a, b = monthly[0], monthly[-1]
    g.append(f'<circle cx="{x(0):.1f}" cy="{y(a["t"]):.1f}" r="4" fill="var(--statute)"/>')
    g.append(f'<circle cx="{x(n-1):.1f}" cy="{y(b["t"]):.1f}" r="4" fill="var(--statute)"/>')
    # 端のラベルは固定文字列にせず、実データの月から作る。
    def jp(mm):
        y_, m_ = mm.split("-")
        return f"{y_}年{int(m_)}月"
    g.append(f'<text x="{w-pr+11}" y="{y(b["t"])+4:.1f}" class="lbl" fill="var(--statute)">{b["t"]:,}</text>')
    g.append(f'<text x="{w-pr+11}" y="{y(b["t"])+21:.1f}" class="sub">{jp(b["m"])}</text>')
    g.append(f'<text x="{x(0)+8:.1f}" y="{y(a["t"])-11:.1f}" class="lbl" fill="var(--statute)">{a["t"]:,}</text>')
    g.append(f'<text x="{x(0)+8:.1f}" y="{y(a["t"])-27:.1f}" class="sub">{jp(a["m"])}</text>')
    g.append("</svg>")
    return "".join(g)


recon = json.loads((W / "reconcile.json").read_text(encoding="utf-8"))


def official_chart() -> str:
    """公式局数表の 総計 / 営業中 と、閉鎖中の内訳。"""
    w, h, pl, pr, pt, pb = 700, 330, 62, 146, 22, 42
    lo, hi = 23100, 24500
    n = len(recon)
    def x(i): return pl + i / (n - 1) * (w - pl - pr)
    def y(v): return pt + (hi - v) / (hi - lo) * (h - pt - pb)
    g = [f'<svg viewBox="0 0 {w} {h}" role="img" '
         f'aria-label="公式局数表による総局数と営業中局数の月次推移、2018年10月から2026年6月">']
    for gv in range(23200, 24501, 200):
        g.append(f'<line x1="{pl}" y1="{y(gv):.1f}" x2="{w-pr}" y2="{y(gv):.1f}" class="grid"/>')
        g.append(f'<text x="{pl-9}" y="{y(gv)+4:.1f}" class="tick end">{gv:,}</text>')
    for i, r in enumerate(recon):
        if r["m"].endswith("-04"):
            g.append(f'<text x="{x(i):.1f}" y="{h-16}" class="tick mid">{r["m"][:4]}</text>')
    # 乖離部分を塗る
    band = ([f"{x(i):.1f},{y(r['official_total']):.1f}" for i, r in enumerate(recon)]
            + [f"{x(i):.1f},{y(r['operating']):.1f}" for i, r in reversed(list(enumerate(recon)))])
    g.append(f'<polygon points="{" ".join(band)}" fill="var(--seal)" opacity=".14"/>')
    for key, col, lab in (("official_total", "var(--statute)", "届出上（形式）"),
                          ("operating", "var(--seal)", "営業中（実効）")):
        pts = " ".join(f"{x(i):.1f},{y(r[key]):.1f}" for i, r in enumerate(recon))
        g.append(f'<polyline points="{pts}" fill="none" stroke="{col}" stroke-width="2.2"/>')
        last = recon[-1][key]
        g.append(f'<text x="{w-pr+10}" y="{y(last)+4:.1f}" class="lbl" fill="{col}">{last:,}</text>')
        g.append(f'<text x="{w-pr+10}" y="{y(last)+20:.1f}" class="sub" fill="{col}">{lab}</text>')
    mid = len(recon) // 2
    g.append(f'<text x="{x(mid):.1f}" y="{(y(recon[mid]["official_total"])+y(recon[mid]["operating"]))/2+4:.1f}" '
             f'class="sub mid" fill="var(--seal)">この幅が閉鎖中の局</text>')
    g.append("</svg>")
    return "".join(g)


def closed_chart() -> str:
    w, h, pl, pr, pt, pb = 700, 210, 62, 128, 18, 40
    n = len(recon)
    hi = 900
    def x(i): return pl + i / (n - 1) * (w - pl - pr)
    def y(v): return pt + (hi - v) / hi * (h - pt - pb)
    g = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="閉鎖中の局数の推移、簡易郵便局と直営の内訳">']
    for gv in (0, 200, 400, 600, 800):
        g.append(f'<line x1="{pl}" y1="{y(gv):.1f}" x2="{w-pr}" y2="{y(gv):.1f}" class="grid"/>')
        g.append(f'<text x="{pl-9}" y="{y(gv)+4:.1f}" class="tick end">{gv}</text>')
    for i, r in enumerate(recon):
        if r["m"].endswith("-04"):
            g.append(f'<text x="{x(i):.1f}" y="{h-14}" class="tick mid">{r["m"][:4]}</text>')
    simple = [f"{x(i):.1f},{y(r['cl_simple']):.1f}" for i, r in enumerate(recon)]
    base = f"{x(n-1):.1f},{y(0):.1f} {x(0):.1f},{y(0):.1f}"
    g.append(f'<polygon points="{" ".join(simple)} {base}" fill="var(--seal)" opacity=".22"/>')
    g.append(f'<polyline points="{" ".join(simple)}" fill="none" stroke="var(--seal)" stroke-width="2"/>')
    total = [f"{x(i):.1f},{y(r['official_total']-r['operating']):.1f}" for i, r in enumerate(recon)]
    g.append(f'<polyline points="{" ".join(total)}" fill="none" stroke="var(--statute)" '
             f'stroke-width="1.6" stroke-dasharray="4 3"/>')
    g.append(f'<text x="{w-pr+10}" y="{y(recon[-1]["cl_simple"])+4:.1f}" class="lbl" fill="var(--seal)">672</text>')
    g.append(f'<text x="{w-pr+10}" y="{y(recon[-1]["cl_simple"])+20:.1f}" class="sub" fill="var(--seal)">簡易郵便局</text>')
    g.append(f'<text x="{w-pr+10}" y="{y(827)-10:.1f}" class="lbl" fill="var(--statute)">827</text>')
    g.append(f'<text x="{w-pr+10}" y="{y(827)+6:.1f}" class="sub" fill="var(--statute)">閉鎖中 計</text>')
    g.append("</svg>")
    return "".join(g)


SENS_LABEL = {"all885": "全885団体（全部＋一部＋みなし）", "full_only": "全部過疎のみ",
              "full_plus_deemed": "全部過疎＋みなし過疎", "partial_only": "一部過疎のみ"}


ANIM_LABEL = [("0", "−20%以下", "seal"), ("1", "−20〜−10%", "seal"), ("2", "−10〜−5%", ""),
              ("3", "−5〜0%", ""), ("4", "変化なし", ""), ("5", "0〜+5%", "statute"),
              ("6", "+5%超", "statute"), ("7", "基準月に局なし", "")]


def anim_rows() -> str:
    A = S["animation"]
    out = []
    for k, lab, cls in ANIM_LABEL:
        n = A["hist"][k]
        if k == "7" and n == 0:
            continue
        c = f' class="n {cls}"' if cls else ' class="n"'
        out.append(f'<tr><td>{lab}</td><td{c}>{n:,}</td><td class="n">{A["pct"][k]:.1f}%</td></tr>')
    out.append(f'<tr class="total"><td>5%以上の減少があったセル</td>'
               f'<td class="n">{A["decline5plus"]:,}</td>'
               f'<td class="n">{A["decline5plus"]/A["cells"]*100:.1f}%</td></tr>')
    return "".join(out)


def sens_rows() -> str:
    out = []
    for key, lab in SENS_LABEL.items():
        k = S["kaso"][key]
        out.append(f'<tr><td>{html.escape(lab)}</td><td class="n">{k["kaso"]["n"]:,}</td>'
                   f'<td class="n seal">{k["kaso"]["rate_eff"]:.1f}%</td>'
                   f'<td class="n">{k["other"]["n"]:,}</td>'
                   f'<td class="n">{k["other"]["rate_eff"]:.1f}%</td>'
                   f'<td class="n"><strong>{k["ratio_eff"]:.2f}倍</strong></td></tr>')
    return "".join(out)


ACCESSIBILITY_DATA = S.get("accessibility", {})
ACCESSIBILITY_READY = (
    ACCESSIBILITY_DATA.get("status") == "computed"
    and
    ACCESSIBILITY_DATA.get("relocation_selection", {}).get("date_policy")
    == "effective_date_confirmed_only"
    and ACCESSIBILITY_DATA.get("inheritance_policy", {}).get("match_method")
    == "exact_name_municipality_with_distance_gate"
    and bool(ACCESSIBILITY_DATA.get("input_artifacts"))
    and bool(ACCESSIBILITY_DATA.get("stratified", {}).get("corrected"))
    and all(row.get("status") == "computed"
            for row in ACCESSIBILITY_DATA.get("results", {}).values())
    and all(row.get("status") == "computed"
            for group in ACCESSIBILITY_DATA.get("stratified", {}).values()
            for row in group.values())
)
ACC = ACCESSIBILITY_DATA.get("results", {}) if ACCESSIBILITY_READY else {}
ACC_ROWS = [("距離1kmゲート付き継承（保守系列）", "inherit_all"),
            ("実施日確認済み移転のみ継承しない", "corrected"),
            ("継承しない（変化を最大に見積もる）", "isj")]
STRAT = ACCESSIBILITY_DATA.get("stratified", {}).get("corrected") if ACCESSIBILITY_READY else None


def access_rows() -> str:
    b = ACC["p30_2013"]
    out = [f'<tr><td>2013年 実効（実測点）</td><td class="n">{b["mean_m"]:.1f}m</td>'
           f'<td class="n">{b["cov"]["1000"]:.2f}%</td><td class="n">{b["over2km"]:,}</td>'
           f'<td class="n">—</td></tr>']
    for lab, key in ACC_ROWS:
        r = ACC[key]
        cls = ' class="total"' if key == "corrected" else ""
        out.append(f'<tr{cls}><td>2026年 実効 · {html.escape(lab)}</td><td class="n">{r["mean_m"]:.1f}m</td>'
                   f'<td class="n">{r["cov"]["1000"]:.2f}%</td><td class="n">{r["over2km"]:,}</td>'
                   f'<td class="n seal">{r["delta_over2km_pct"]:+.1f}%</td></tr>')
    return "".join(out)


def access_results_section() -> str:
    if not ACCESSIBILITY_READY:
        return (
            '<h3>アクセシビリティ変化量は未評価</h3>'
            '<p class="caveat">旧計算は未封印の照合表と距離制限のないP30座標継承に依存したため撤回した。'
            '現行方針は、封印済み2013/2026座標の同一自治体・同一名称・一意照合と1km距離ゲートを要求し、'
            '予定日を移転実施日として扱わない。新しい gold 成果物と層別分母の検証が完了するまで、'
            '平均距離・カバー率・2km超人口・地域差の数値を掲載しない。</p>')
    relocation = ACCESSIBILITY_DATA["relocation_selection"]
    stats = ACCESSIBILITY_DATA["stats"]
    candidates = stats.get("inherit_all_applied", 0) + stats.get("withheld_due_to_relocation", 0)
    low = min(ACC[k]["cov"]["1000"] for _, k in ACC_ROWS)
    high = max(ACC[k]["cov"]["1000"] for _, k in ACC_ROWS)
    over_low = min(ACC[k]["over2km"] for _, k in ACC_ROWS)
    over_high = max(ACC[k]["over2km"] for _, k in ACC_ROWS)
    pct_low = min(ACC[k]["delta_over2km_pct"] for _, k in ACC_ROWS)
    pct_high = max(ACC[k]["delta_over2km_pct"] for _, k in ACC_ROWS)
    age_low = min(ACC[k]["delta_over2km65_pct"] for _, k in ACC_ROWS)
    age_high = max(ACC[k]["delta_over2km65_pct"] for _, k in ACC_ROWS)
    return f'''<h3>保守的な座標方針による感度分析</h3>
<p>継承候補は同一自治体・同一名称・一意・座標差1km以内に限定した。移転除外は独立確認済みの実施日だけを使い、
予定日は実施日として扱わない。期間内の実施日確認済み移転は{relocation.get("accepted_events", 0):,}件、
除外した局は{stats.get("withheld_due_to_relocation", 0):,}局である。</p>
<div class="tw"><table>
<thead><tr><th>系列（実効）</th><th class="n">平均距離</th><th class="n">1km圏内</th><th class="n">2km超人口</th><th class="n">2013年比</th></tr></thead>
<tbody>{access_rows()}</tbody></table></div>
<p>1km圏内カバー率は{ACC["p30_2013"]["cov"]["1000"]:.2f}%から
<strong>{low:.2f}〜{high:.2f}%</strong>、2km超人口は{ACC["p30_2013"]["over2km"]:,}人から
<strong>{over_low:,}〜{over_high:,}人</strong>となる。増加率は{pct_low:+.1f}%〜{pct_high:+.1f}%、
65歳以上では{age_low:+.1f}%〜{age_high:+.1f}%である。</p>
<p class="caveat">これらは道路距離・所要時間ではなく、座標方針の感度分析である。候補{candidates:,}局の
移転不存在を証明しておらず、単一の法的評価値として扱わない。</p>'''


def strat_chart() -> str:
    """≤1km カバー率の変化（pt）を層別に。"""
    if not STRAT:
        return ('<svg viewBox="0 0 620 100" role="img" '
                'aria-label="層別アクセシビリティは未評価">'
                '<rect x="1" y="1" width="618" height="98" class="frame"/>'
                '<text x="310" y="54" class="sub mid">層別値は再計算待ち（not evaluable）</text></svg>')
    specs = [("過疎地", "kaso", "var(--seal)"),
             ("非過疎地", "non_kaso", "var(--statute)"),
             ("", None, ""),
             ("人口密度 高（DID相当）", "did_like_ge4000", "var(--statute)"),
             ("人口密度 中", "mid_1000_4000", "var(--d2)"),
             ("人口密度 低", "sparse_lt1000", "var(--seal)")]
    rows = [(label, 0 if key is None else STRAT[key]["delta_cov1km_pt"], color)
            for label, key, color in specs]
    w, pl, bh, gap = 620, 168, 22, 11
    h = 26 + len(rows) * (bh + gap)
    zero = pl + 60
    max_abs = max(abs(v) for label, v, _ in rows if label) or 1.0
    scale = (w - zero - 120) / max_abs
    g = [f'<svg viewBox="0 0 {w} {h}" role="img" '
         f'aria-label="1km圏内カバー率の変化、過疎地別と人口密度階級別">']
    g.append(f'<line x1="{zero}" y1="8" x2="{zero}" y2="{h-20}" class="grid"/>')
    for i, (lab, v, col) in enumerate(rows):
        if not lab:
            continue
        y = 12 + i * (bh + gap)
        g.append(f'<text x="{pl-6}" y="{y+bh-6}" class="tick end">{lab}</text>')
        ln = abs(v) * scale
        x = zero - ln if v < 0 else zero
        g.append(f'<rect x="{x:.1f}" y="{y}" width="{max(ln,1):.1f}" height="{bh}" fill="{col}" rx="1.5"/>')
        tx = (x - 8) if v < 0 else (zero + ln + 8)
        anc = "end" if v < 0 else "start"
        g.append(f'<text x="{tx:.1f}" y="{y+bh-6}" class="num" text-anchor="{anc}">{v:+.2f}pt</text>')
    g.append(f'<text x="{zero}" y="{h-4}" class="sub mid">0</text>')
    g.append("</svg>")
    return "".join(g)


def strat_narrative() -> str:
    if not STRAT:
        return ('<p class="caveat">層別アクセシビリティは、封印済みメッシュ距離からの'
                '再計算が未完了であるため数値を掲載しない。</p>')
    k = STRAT["kaso"]; n = STRAT["non_kaso"]
    high = STRAT["did_like_ge4000"]; mid = STRAT["mid_1000_4000"]
    low = STRAT["sparse_lt1000"]
    distance_ratio = (abs(k["delta_mean_m"] / n["delta_mean_m"])
                      if n["delta_mean_m"] else None)
    coverage_ratio = (abs(k["delta_cov1km_pt"] / n["delta_cov1km_pt"])
                      if n["delta_cov1km_pt"] else None)
    ratios = (f"距離で{distance_ratio:.2f}倍、カバー率で{coverage_ratio:.2f}倍"
              if distance_ratio is not None and coverage_ratio is not None
              else "比率は分母0のため算出不能")
    values = (high["delta_cov1km_pt"], mid["delta_cov1km_pt"], low["delta_cov1km_pt"])
    gradient = "単調な勾配" if values[0] >= values[1] >= values[2] else "非単調な差"
    return (f'<p>過疎地の平均距離変化は{k["delta_mean_m"]:+.2f}m'
            f'（{k["delta_cov1km_pt"]:+.3f}ポイント）、非過疎地は'
            f'{n["delta_mean_m"]:+.2f}m（{n["delta_cov1km_pt"]:+.3f}ポイント）で、'
            f'<strong>{ratios}</strong>である。人口密度階級の1km圏カバー率変化は、'
            f'高密度{values[0]:+.2f}、中密度{values[1]:+.2f}、低密度{values[2]:+.2f}'
            f'ポイントで、<strong>{gradient}</strong>を示す。</p>')


# ---------------------------------------------------------------- charts
def slope_chart() -> str:
    """形式 vs 実効の 2013→2026。"""
    series = [("形式（届出上存在）", F13, F26, "var(--statute)"),
              ("実効（実際に稼働）", E13, E26, "var(--seal)")]
    w, h, pl, pr, pt, pb = 620, 300, 120, 130, 28, 34
    lo, hi = 22800, 24800
    def y(v): return pt + (hi - v) / (hi - lo) * (h - pt - pb)
    x0, x1 = pl, w - pr
    g = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="形式局数と実効局数の2013年から2026年への変化">']
    for gv in range(23000, 24801, 600):
        g.append(f'<line x1="{x0}" y1="{y(gv):.1f}" x2="{x1}" y2="{y(gv):.1f}" class="grid"/>')
        g.append(f'<text x="{x0-10}" y="{y(gv)+4:.1f}" class="tick end">{gv:,}</text>')
    for label, a, b, col in series:
        g.append(f'<line x1="{x0}" y1="{y(a):.1f}" x2="{x1}" y2="{y(b):.1f}" stroke="{col}" stroke-width="2.5"/>')
        g.append(f'<circle cx="{x0}" cy="{y(a):.1f}" r="4.5" fill="{col}"/>')
        g.append(f'<circle cx="{x1}" cy="{y(b):.1f}" r="4.5" fill="{col}"/>')
        g.append(f'<text x="{x0-10}" y="{y(a)-12:.1f}" class="lbl end" fill="{col}">{a:,}</text>')
        g.append(f'<text x="{x1+12}" y="{y(b)+4:.1f}" class="lbl" fill="{col}">{b:,}</text>')
        g.append(f'<text x="{x1+12}" y="{y(b)+21:.1f}" class="sub" fill="{col}">{label}</text>')
    g.append(f'<text x="{x0}" y="{h-10}" class="tick mid">2013年11月</text>')
    g.append(f'<text x="{x1}" y="{h-10}" class="tick mid">2026年6月</text>')
    g.append("</svg>")
    return "".join(g)


def lorenz_chart() -> str:
    w = h = 340
    pad = 44
    s = w - pad * 2
    def pts(arr):
        return " ".join(f"{pad+a*s:.1f},{h-pad-b*s:.1f}" for a, b in arr)
    g = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="人口に対する局数のローレンツ曲線、2013年と2026年">']
    g.append(f'<line x1="{pad}" y1="{h-pad}" x2="{w-pad}" y2="{pad}" class="grid" stroke-dasharray="3 3"/>')
    g.append(f'<rect x="{pad}" y="{pad}" width="{s}" height="{s}" class="frame"/>')
    g.append(f'<polyline points="{pts(lor["l13"])}" fill="none" stroke="var(--muted)" stroke-width="2"/>')
    g.append(f'<polyline points="{pts(lor["l26"])}" fill="none" stroke="var(--seal)" stroke-width="2.2"/>')
    g.append(f'<text x="{pad}" y="{h-14}" class="tick">累積人口比 →</text>')
    g.append(f'<text x="{pad-8}" y="{pad-14}" class="tick">累積局数比</text>')
    # 曲線と同じ系列（実効）の算出値を表示する。固定値は使わない。
    g13 = S["inequality"]["2013_effective"]["gini"]
    g26 = S["inequality"]["2026_effective"]["gini"]
    g.append(f'<text x="{w-pad-6}" y="{pad+38}" class="sub end" fill="var(--muted)">2013 G={g13:.4f}</text>')
    g.append(f'<text x="{w-pad-6}" y="{pad+56}" class="sub end" fill="var(--seal)">2026 G={g26:.4f}</text>')
    g.append("</svg>")
    return "".join(g)


def pref_chart() -> str:
    rows = pref[:12] + pref[-6:]
    bh, gap, pl, pr, pt = 17, 6, 74, 58, 16
    h = pt + len(rows) * (bh + gap) + 26
    w = 620
    span = w - pl - pr
    worst = min(r["chg_pct"] for r in pref)
    g = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="都道府県別 実効局数の増減率">']
    for i, r in enumerate(rows):
        y = pt + i * (bh + gap)
        ln = abs(r["chg_pct"]) / abs(worst) * span
        col = "var(--seal)" if r["chg_pct"] <= -5 else ("var(--statute)" if r["chg_pct"] > -2 else "var(--mid)")
        g.append(f'<text x="{pl-9}" y="{y+bh-4}" class="tick end">{html.escape(r["pref"])}</text>')
        g.append(f'<rect x="{pl}" y="{y}" width="{ln:.1f}" height="{bh}" fill="{col}" rx="1.5"/>')
        g.append(f'<text x="{pl+ln+7:.1f}" y="{y+bh-4}" class="num">{r["chg_pct"]:.1f}%</text>')
    y = pt + 12 * (bh + gap) - gap / 2
    g.append(f'<line x1="{pl-70}" y1="{y:.1f}" x2="{w-8}" y2="{y:.1f}" class="grid" stroke-dasharray="2 4"/>')
    g.append(f'<text x="{pl-70}" y="{h-6}" class="sub">上位12県と下位6県。全47県中。</text>')
    g.append("</svg>")
    return "".join(g)


def kaso_chart() -> str:
    k = S["kaso"]["all885"]
    data = [("過疎地", k["kaso"]["n"], k["kaso"]["n13e"], k["kaso"]["n26e"], round(k["kaso"]["rate_eff"], 1)),
            ("非過疎地", k["other"]["n"], k["other"]["n13e"], k["other"]["n26e"], round(k["other"]["rate_eff"], 1))]
    w, h = 620, 150
    pl, pt, bh = 96, 30, 30
    span = w - pl - 150
    g = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="過疎地と非過疎地の実効局数減少率の比較">']
    mx = 6.0
    for i, (lab, n, a, b, pct) in enumerate(data):
        y = pt + i * 58
        ln = abs(pct) / mx * span
        col = "var(--seal)" if lab == "過疎地" else "var(--statute)"
        g.append(f'<text x="{pl-10}" y="{y+bh-9}" class="tick end">{lab}</text>')
        g.append(f'<text x="{pl-10}" y="{y+bh+6}" class="sub end">{n}団体</text>')
        g.append(f'<rect x="{pl}" y="{y}" width="{ln:.1f}" height="{bh}" fill="{col}" rx="2"/>')
        g.append(f'<text x="{pl+ln+10:.1f}" y="{y+bh-15}" class="num big">{pct}%</text>')
        g.append(f'<text x="{pl+ln+10:.1f}" y="{y+bh+2}" class="sub">{a:,} → {b:,}局</text>')
    g.append("</svg>")
    return "".join(g)


# ---------------------------------------------------------------- page
CSS = """
:root{
  --paper:#F7F8FA; --raise:#FFFFFF; --ink:#171A20; --body:#333B47;
  --muted:#64707F; --mid:#8A94A2; --rule:#DDE2E8; --rule-soft:#E9EDF2;
  --seal:#A62B31; --statute:#2C4A6E; --amber:#8A6114; --ok:#2F6B4F;
  --seal-wash:#A62B310F; --statute-wash:#2C4A6E0F;
  --inc:#4E7FA8; --d1:#EBD9D6; --d2:#D39A94; --d3:#B45B58; --d4:#7E2226;
  --nodata:#D8DDE3; --mapline:#FFFFFF;
}
@media (prefers-color-scheme:dark){
  :root{
    --paper:#11141A; --raise:#171B22; --ink:#E8EBF0; --body:#C2C9D3;
    --muted:#93A0B0; --mid:#6E7B8B; --rule:#262D38; --rule-soft:#1E242D;
    --seal:#E2757A; --statute:#86AEDA; --amber:#D2A854; --ok:#68B98F;
    --seal-wash:#E2757A17; --statute-wash:#86AEDA17;
    --inc:#5C93C4; --d1:#4A3B3E; --d2:#8A4C4E; --d3:#BC6265; --d4:#EE9296;
    --nodata:#2B323C; --mapline:#11141A;
  }
}
:root[data-theme="dark"]{
  --paper:#11141A; --raise:#171B22; --ink:#E8EBF0; --body:#C2C9D3;
  --muted:#93A0B0; --mid:#6E7B8B; --rule:#262D38; --rule-soft:#1E242D;
  --seal:#E2757A; --statute:#86AEDA; --amber:#D2A854; --ok:#68B98F;
  --seal-wash:#E2757A17; --statute-wash:#86AEDA17;
  --inc:#5C93C4; --d1:#4A3B3E; --d2:#8A4C4E; --d3:#BC6265; --d4:#EE9296;
  --nodata:#2B323C; --mapline:#11141A;
}
:root[data-theme="light"]{
  --paper:#F7F8FA; --raise:#FFFFFF; --ink:#171A20; --body:#333B47;
  --muted:#64707F; --mid:#8A94A2; --rule:#DDE2E8; --rule-soft:#E9EDF2;
  --seal:#A62B31; --statute:#2C4A6E; --amber:#8A6114; --ok:#2F6B4F;
  --seal-wash:#A62B310F; --statute-wash:#2C4A6E0F;
  --inc:#4E7FA8; --d1:#EBD9D6; --d2:#D39A94; --d3:#B45B58; --d4:#7E2226;
  --nodata:#D8DDE3; --mapline:#FFFFFF;
}
*{box-sizing:border-box}
body{
  margin:0; background:var(--paper); color:var(--body);
  font-family:"Hiragino Kaku Gothic ProN","Yu Gothic",YuGothic,"Noto Sans JP","Segoe UI",sans-serif;
  font-size:15.5px; line-height:1.85; -webkit-font-smoothing:antialiased;
}
.wrap{max-width:840px; margin:0 auto; padding:0 24px 96px}
h1,h2,h3,.mincho{
  font-family:"Hiragino Mincho ProN","Yu Mincho",YuMincho,"Noto Serif JP",serif;
  color:var(--ink); font-weight:600; text-wrap:balance;
}
header{padding:64px 0 34px; border-bottom:2px solid var(--ink); margin-bottom:8px}
.eyebrow{
  font-size:11.5px; letter-spacing:.16em; text-transform:uppercase;
  color:var(--muted); margin:0 0 16px; font-weight:600;
}
h1{font-size:32px; line-height:1.4; margin:0 0 14px; letter-spacing:.01em}
.standfirst{font-size:16.5px; color:var(--body); margin:0; max-width:64ch}
.meta{
  display:flex; flex-wrap:wrap; gap:8px 26px; margin-top:26px;
  font-size:12.5px; color:var(--muted);
}
.meta b{color:var(--body); font-weight:600}
h2{
  font-size:21px; margin:64px 0 6px; padding-top:22px;
  border-top:1px solid var(--rule); display:flex; align-items:baseline; gap:12px;
}
h2 .rq{
  font-family:ui-monospace,"SF Mono",Consolas,monospace; font-size:11px;
  letter-spacing:.06em; color:var(--muted); font-weight:400; flex:none;
}
h3{font-size:16px; margin:34px 0 8px}
p{margin:0 0 15px; max-width:66ch}
a{color:var(--statute)}
strong{color:var(--ink); font-weight:600}
.lede{font-size:16px; color:var(--body)}

/* verdict chips encode the spec's five screening categories */
.verdict{
  display:inline-flex; align-items:center; gap:8px; margin:14px 0 4px;
  padding:7px 15px 7px 13px; border-radius:2px; font-size:13.5px; font-weight:600;
  border-left:3px solid currentColor;
}
.v-ok{color:var(--ok); background:#2F6B4F12}
.v-check{color:var(--seal); background:var(--seal-wash)}
.v-na{color:var(--muted); background:#64707F12}
.verdict span{color:var(--body); font-weight:400}

figure{margin:26px 0 8px; padding:0}
figure svg{width:100%; height:auto; display:block}
figcaption{font-size:12.5px; color:var(--muted); margin-top:10px; line-height:1.7}
svg text{font-family:"Hiragino Kaku Gothic ProN","Yu Gothic","Noto Sans JP",sans-serif}
svg .tick{font-size:11px; fill:var(--muted)}
svg .sub{font-size:10.5px; fill:var(--muted)}
svg .lbl{font-size:13px; font-weight:600; font-variant-numeric:tabular-nums}
svg .num{font-size:11.5px; fill:var(--body); font-variant-numeric:tabular-nums;
  font-family:ui-monospace,Consolas,monospace}
svg .num.big{font-size:15px; font-weight:600; fill:var(--ink)}
svg .end{text-anchor:end}
svg .mid{text-anchor:middle}
svg .grid{stroke:var(--rule); stroke-width:1}
svg .frame{fill:none; stroke:var(--rule); stroke-width:1}
svg .mapg path{stroke:var(--mapline); stroke-width:.25; vector-effect:non-scaling-stroke}
svg .inset{fill:none; stroke:var(--rule); stroke-width:1}
svg g[transform] path{stroke:var(--mapline); stroke-width:.25}

.tw{overflow-x:auto; margin:22px 0 6px; border:1px solid var(--rule); border-radius:3px}
table{border-collapse:collapse; width:100%; font-size:13.5px; background:var(--raise)}
th,td{padding:9px 14px; text-align:left; border-bottom:1px solid var(--rule-soft); white-space:nowrap}
thead th{
  background:var(--paper); font-size:11px; letter-spacing:.05em; color:var(--muted);
  font-weight:600; border-bottom:1px solid var(--rule);
}
tbody tr:last-child td{border-bottom:none}
td.n,th.n{text-align:right; font-variant-numeric:tabular-nums;
  font-family:ui-monospace,Consolas,monospace}
tr.total td{font-weight:600; color:var(--ink); background:var(--paper)}
.seal{color:var(--seal)} .statute{color:var(--statute)}

.callout{
  margin:26px 0; padding:20px 24px; background:var(--raise);
  border:1px solid var(--rule); border-left:3px solid var(--seal); border-radius:3px;
}
.callout p:last-child{margin-bottom:0}
.callout .hd{
  font-size:11.5px; letter-spacing:.13em; text-transform:uppercase;
  color:var(--seal); font-weight:700; margin-bottom:9px;
}
.callout.q{border-left-color:var(--statute)}
.callout.q .hd{color:var(--statute)}

.pair{display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:2px;
  margin:26px 0 6px; background:var(--rule); border:1px solid var(--rule); border-radius:3px}
.pair div{background:var(--raise); padding:17px 19px}
.pair .k{font-size:11.5px; color:var(--muted); letter-spacing:.05em; margin-bottom:7px}
.pair .v{font-size:25px; font-weight:600; color:var(--ink);
  font-variant-numeric:tabular-nums; line-height:1.25;
  font-family:"Hiragino Mincho ProN","Yu Mincho",YuMincho,serif}
.pair .d{font-size:12.5px; margin-top:3px}

ul,ol{max-width:66ch; padding-left:1.3em; margin:0 0 15px}
li{margin-bottom:7px}
.caveat{font-size:13.5px; color:var(--muted)}
.caveat strong{color:var(--body)}
footer{margin-top:72px; padding-top:22px; border-top:1px solid var(--rule);
  font-size:12.5px; color:var(--muted)}
code{font-family:ui-monospace,Consolas,monospace; font-size:.9em;
  background:var(--rule-soft); padding:1px 5px; border-radius:2px}
@media (max-width:640px){
  header{padding-top:40px} h1{font-size:25px} body{font-size:15px}
  .wrap{padding:0 17px 64px}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""


def prefrows() -> str:
    out = []
    for r in pref[:8]:
        out.append(f'<tr><td>{html.escape(r["pref"])}</td><td class="n">{r["n13e"]:,}</td>'
                   f'<td class="n">{r["n26e"]:,}</td><td class="n seal">{r["chg_pct"]:.1f}%</td>'
                   f'<td class="n">{r["per10k_26"]:.2f}</td><td class="n">{r["kaso_share"]:.0f}%</td></tr>')
    return "".join(out)


BODY = f"""
<div class="wrap">
<header>
  <p class="eyebrow">日本郵便株式会社法 6条・同法施行規則 4条 に基づく実証検証</p>
  <h1>郵便局の地理的偏在に関する検証<br>—— 形式的な維持と実質的な利用可能性の乖離</h1>
  <p class="standfirst">日本郵便が法6条2項に基づき届け出た全局一覧（2026年6月30日現在）と、
  国土数値情報P30（2013年11月末）を突合し、市区町村単位で法定設置基準の充足と分布の変化を測定した。</p>
  <div class="meta">
    <span><b>対象</b> 全1,741市区町村（北方領土6村を除く）</span>
    <span><b>2013年</b> 24,526局</span>
    <span><b>2026年</b> 24,112局</span>
    <span><b>人口</b> 500mメッシュ 466,145区画</span>
    <span><b>算出</b> 2026年8月9日</span>
  </div>
</header>

<h2><span class="rq">要旨</span>三つの所見</h2>
<p class="lede">市区町村単位で見るかぎり、法が定める最低設置基準は満たされている。
しかし届出上の局数と実際に開いている局数の差は13年間で2.7倍に広がり、
その差はほぼ全て簡易郵便局の休止に由来する。
そして減少は、維持義務が課されている過疎地でこそ速く進んでいる。</p>

<div class="pair">
  <div><div class="k">形式ネットワーク</div><div class="v">{pct(F13,F26):.1f}%</div>
    <div class="d statute">{F13:,} → {F26:,}局</div></div>
  <div><div class="k">実効ネットワーク</div><div class="v seal">{pct(E13,E26):.1f}%</div>
    <div class="d seal">{E13:,} → {E26:,}局</div></div>
  <div><div class="k">簡易郵便局の休止率</div><div class="v seal">{SIMPLE26C/SIMPLE26*100:.1f}%</div>
    <div class="d">2013年は{SIMPLE13C/SIMPLE13*100:.1f}%</div></div>
  <div><div class="k">周辺20km圏の局が5%以上減った区画</div><div class="v seal">{S['animation']['decline5plus']/S['animation']['cells']*100:.1f}%</div>
    <div class="d">有人4,366区画のうち{S['animation']['decline5plus']:,}</div></div>
</div>

<figure>{slope_chart()}
<figcaption>届出上存在する局（青）と実際に窓口を利用できる局（赤）。
形式ネットワークの縮小は1.8%にとどまるが、実効ネットワークは3.9%縮小しており、乖離が拡大している。
2026年の形式には一覧の脚注に記載された長期休止の簡易郵便局653局を含む。</figcaption></figure>

<h2><span class="rq">推移</span>2012年10月からの月次系列</h2>
<p>会社が法6条2項により総務大臣に届け出た変更届177本（2012年10月4日〜2026年7月30日）を解析し、
6,053件の変更事象を抽出した。届出は「変更予定」を含むため、そのまま履歴に適用してはならない。
同一施設の事象を実施予定日順に連鎖させ、連鎖内で変更前後の状態が接続すること、
かつ連鎖の終端状態が現況一覧と一致することを条件に採用した。
{S["confirm"]["chain_count"]:,}連鎖のうち{S["confirm"]["promoted_chain_count"]:,}（{S["confirm"]["promoted_chain_count"]/S["confirm"]["chain_count"]*100:.1f}%）、
事象単位では<strong>{S["confirm"]["confirmed_event_count"]:,}件（{S["confirm"]["confirmed_event_count"]/S["confirm"]["input_event_count"]*100:.1f}%）</strong>が採用された。
採用しなかったものは連鎖不整合{S["confirm"]["not_promoted_reason_counts"]["chain_inconsistent"]}件・
終端不一致{S["confirm"]["not_promoted_reason_counts"]["anchor_mismatch"]}件で、いずれも理由を記録している。</p>

<div class="callout q">
<div class="hd">「状態の裏づけ」と「実施日の確定」は別である</div>
<p>終端一致が立証するのは<strong>「遅くともアンカー日（2026年6月30日）までにその状態になった」</strong>ことであって、
各変更が届出上の予定日に実施されたことではない。単一の事象や、A→B→Aのように元に戻る連鎖では特にそうである。
本稿の月次系列は<strong>実施日を届出上の予定日と仮置きして復元したもの</strong>であり、
各月末の値は確定値ではない。個別の実施日を確定するには後続の断面観測か公式の個別告知が要る。</p>
<p>ただし、後述する公式集計との突合により、この仮置きによる系列全体のずれは
<strong>月あたり数局の水準にとどまる</strong>ことが確認できている。</p>
</div>

<figure>{monthly_chart()}
<figcaption>届出上存在する局数の月次推移（{len(monthly)}か月）。現況一覧を起点に事象を逆算して復元した。
履歴アンカーは2026年6月30日で、それより後の月は状態の持越しにすぎないため表示していない。
一覧の脚注に総数のみ記載される長期休止の簡易郵便局653局は、個体が特定できないため含まない。</figcaption></figure>

<p>減少は特定の時期に集中せず、13年9か月にわたり<strong>ほぼ一定の速度で継続している</strong>。
{monthly[0]["t"]:,}局（2012年10月末）から{monthly[-1]["t"]:,}局（2026年6月末）へ、
{monthly[0]["t"]-monthly[-1]["t"]:,}局・{abs(pct(monthly[0]["t"], monthly[-1]["t"])):.1f}%の減少である。
内訳では郵便局が{monthly[0]["p"]:,}→{monthly[-1]["p"]:,}（{pct(monthly[0]["p"], monthly[-1]["p"]):.1f}%）、
会社の営業所が{monthly[0]["c"]:,}→{monthly[-1]["c"]:,}（{pct(monthly[0]["c"], monthly[-1]["c"]):.1f}%）で、
<strong>減少は会社の営業所（その大半は簡易郵便局）に偏っている</strong>。</p>

<h3>全国の3.5%減は、地域差を覆い隠している</h3>
<p>復元した{S["animation"]["months"]}か月を10km四方の固定格子（人口のある{S["animation"]["cells"]:,}区画）に載せ、
各セル中心から半径20km以内の局数が2012年10月からどれだけ変化したかを月次で追った。
格子・投影法・検索半径・色階級は全期間で固定し、期間ごとの再スケーリングは行っていない。
座標は整理番号ごとに固定せず、履歴の住所バージョンごとに持たせているため、移転は該当月に位置が動く。</p>

<div class="tw"><table>
<thead><tr><th>2026年6月時点の変化</th><th class="n">セル数</th><th class="n">構成比</th></tr></thead>
<tbody>{anim_rows()}</tbody></table></div>

<p>全国の局数は{abs(pct(monthly[0]["t"], monthly[-1]["t"])):.1f}%しか減っていないが、
<strong>人口のある区画の{S["animation"]["decline5plus"]/S["animation"]["cells"]*100:.1f}%では周辺20km圏の局が5%以上減り、
{S["animation"]["decline10plus"]/S["animation"]["cells"]*100:.1f}%では10%以上減っている。</strong>
一方で{S["animation"]["pct"]["4"]:.1f}%の区画は変化しておらず、
{(S["animation"]["hist"]["5"]+S["animation"]["hist"]["6"])/S["animation"]["cells"]*100:.1f}%はむしろ増えている。
平均値は、この二極化を打ち消して見せてしまう。</p>

<p class="caveat">この推移は<a href="https://github.com/fwdyutaro/japan-post-office-network-analysis/releases/tag/v1.0.0">月次アニメーション</a>で
2012年10月から1か月ずつ確認できる。なお基準時点で半径20km以内に1局も無い有人区画が7つある。</p>

<h3>復元結果は公式集計と整合する</h3>
<p>日本郵便は都道府県別の「郵便局局数表」を月次で公開しており、
営業中・閉鎖中を直営郵便局・分室・簡易郵便局別に収録している。
2018年10月から2026年6月までの93か月分を取得し、全国計で突合した。</p>

<p>復元系列は届出一覧の本体を対象とするため、一覧に掲載されない長期休止の簡易郵便局を含まない。
したがって<strong>公式総計 −  復元値 = 一覧外の長期休止簡易局数</strong>となるはずである。
その差は2018年10月の282から2026年6月の653まで単調に近い増加を示し、
最終月の653は<strong>一覧の脚注が記載する653局と正確に一致する</strong>。
さらに、この差が公式の「閉鎖中・簡易郵便局」数を上回る月は<strong>93か月中0</strong>であり、
論理的な包含関係（一覧外の長期休止局は閉鎖中の簡易局の部分集合）が全期間で保たれている。</p>

<p class="caveat">ただし月次の純増減は、復元値と公式総計とで±3局以内に収まるのが92か月中49か月、
差の中央値は−2局である。水準は検証されているが、月ごとの増減には数局の差が残る。
基準日のずれ、未確定の222件、一覧外集合の変動が原因と考えられる。</p>

<h2><span class="rq">中核</span>形式と実効の乖離は拡大し続けている</h2>
<p>一時閉鎖と再開は法6条2項の届出事項の変更にあたらないため、<strong>変更届には一切記録されていない</strong>
（実PDFを全文検索し該当0件を確認）。個票単位の閉鎖履歴は本稿では復元できていない。
しかし公式局数表は<strong>集計値としての実効系列を月次で与える</strong>。これを用いる。</p>

<figure>{official_chart()}
<figcaption>青が届出上存在する局の総数、赤が実際に営業している局の数。
両者の幅が閉鎖中の局にあたる。日本郵便「郵便局局数表」全国計、2018年10月〜2026年6月。</figcaption></figure>

<p>総局数は24,376から24,112へ264局・<strong>1.1%</strong>しか減っていない。
一方で営業中の局は24,012から23,285へ727局・<strong>3.0%</strong>減っている。
<strong>減少の実勢は形式値の約2.7倍である。</strong></p>

<figure>{closed_chart()}
<figcaption>閉鎖中の局数。破線が総数、塗りが簡易郵便局分。2018年10月の364局から2026年6月の827局へ2.3倍。
増加分463局のうち383局（83%）が簡易郵便局である。</figcaption></figure>

<div class="callout">
<div class="hd">届出上の維持と、実際の利用可能性</div>
<p>一時閉鎖は「廃止」ではないため、閉鎖中の局も届出一覧に計上され続ける（長期休止の簡易郵便局は
脚注の総数のみとなり、本体からも外れる）。形式ネットワークの水準はほぼ保たれているように見える。
しかし実際に窓口を開けている局は、その2.7倍の速さで減っている。
<strong>施行規則4条2項3号の「水準を維持する」を届出上の局数で測るか、
実際に利用できる局数で測るかによって、評価は大きく変わる。</strong></p>
</div>

<h2><span class="rq">RQ-01</span>各市町村に1局以上あるか</h2>
<p>施行規則4条1項は、会社はいずれの市町村（特別区を含む）においても1以上の郵便局を設置しなければならないと定める。
全1,741の法的単位について集計した。政令指定都市の行政区は独立の単位ではないため親市に集約し、
東京都特別区23区はそれぞれ独立の単位として扱った。</p>

<div class="verdict v-ok">適合確認 <span>2026年6月30日時点で、局数0の市町村は存在しない</span></div>

<p>ただし4条1項は最低基準にすぎない。1局しかない団体は<strong>105</strong>あり、
その多くは過疎地域に指定された小規模自治体である（北海道音威子府村・人口706人、福島県檜枝岐村・人口504人など）。
市町村単位で基準を満たすことは、域内に空白域がないことを意味しない。</p>

<h3>2013年には、形式的には適合しながら実際には1局も使えない町村があった</h3>
<p>2013年11月末時点で、届出上は郵便局が存在するにもかかわらず、
全局が一時閉鎖で実効局数が0だった団体が<strong>6</strong>ある。</p>

<div class="tw"><table>
<thead><tr><th>団体</th><th class="n">形式局数</th><th class="n">実効局数</th><th>事由</th></tr></thead>
<tbody>
<tr><td>福島県楢葉町</td><td class="n">1以上</td><td class="n seal">0</td><td>避難指示区域</td></tr>
<tr><td>福島県富岡町</td><td class="n">1以上</td><td class="n seal">0</td><td>避難指示区域</td></tr>
<tr><td>福島県双葉町</td><td class="n">1以上</td><td class="n seal">0</td><td>避難指示区域</td></tr>
<tr><td>福島県浪江町</td><td class="n">1以上</td><td class="n seal">0</td><td>避難指示区域</td></tr>
<tr><td>福島県葛尾村</td><td class="n">1以上</td><td class="n seal">0</td><td>避難指示区域</td></tr>
<tr><td>高知県大川村</td><td class="n">1以上</td><td class="n seal">0</td><td>—</td></tr>
</tbody></table></div>

<div class="callout">
<div class="hd">この6件が示すこと</div>
<p>一時閉鎖は「廃止」ではないため、届出上は局が存続し、4条1項は形式的に充足され続ける。
しかし住民にとっては1局も利用できない。
<strong>形式系列だけを見る検証は、この状態を「適合」と判定してしまう。</strong>
本検証が形式と実効を分離して集計する理由はここにある。
なお2026年時点では、これらの町村はいずれも1局まで回復している。</p>
</div>

<h2><span class="rq">RQ-04</span>過疎地の水準は維持されているか</h2>
<p>施行規則4条2項3号は、過疎地について
「郵政民営化法等の一部を改正する等の法律の施行の際現に存する郵便局ネットワークの水準を維持することを旨とする」と定める。
維持義務は過疎地にのみ課されている。したがって、過疎地の減少率は非過疎地より小さいと予測される。</p>

<figure>{kaso_chart()}
<figcaption>実効局数の減少率。過疎法指定885団体と非指定856団体の比較。</figcaption></figure>

<p><strong>実測は予測と逆である。</strong>過疎地の実効局数は5.4%減少しており、
非過疎地の2.8%に対して約2倍の速さで減っている。形式局数で見ても6.2%対3.1%で同じ傾向である。</p>

<h3>過疎地の定義を変えても結論は動かない</h3>
<p>過疎法の指定には、市町村全域が対象の「全部過疎」713団体のほか、
合併前の旧市町村区域のみが対象の「一部過疎」158団体と「みなし過疎」14団体がある。
一部過疎は全域が過疎地域ではないため、885団体を一括して扱うと過大評価になりうる。
そこで区分ごとに再計算した。</p>

<div class="tw"><table>
<thead><tr><th>過疎地の定義</th><th class="n">団体</th><th class="n">過疎側</th><th class="n">対照</th><th class="n">対照側</th><th class="n">減少率の比</th></tr></thead>
<tbody>{sens_rows()}</tbody></table></div>

<p>過疎側の減少率は定義によらず<strong>−5.3%から−5.6%の範囲に収まり</strong>、
対照群との比も1.58〜1.97倍で安定している。
最も保守的な「全部過疎のみ」の定義でも1.61倍であり、
<strong>この所見は過疎地区分の取り方に対して頑健である</strong>。</p>

<h3>過疎地の指定はこの期間に拡大している</h3>
<p>変更届には、施設の構造変更とは別に<strong>過疎地フラグの変更が431件</strong>記録されていた。
内訳は過疎地になったものが429件、過疎地でなくなったものが2件で、
2014年（170件）・2021年（143件）・2017年（95件）に集中する。過疎法の改正・追加指定の時期と対応する。</p>

<p>これは施行規則附則4条の適用上、重要な意味を持つ。
同条は、平成24年改正法の施行後に過疎地となった区域については、
基準時を「過疎地に該当することとなった時において」と読み替えると定める。
<strong>この431件は、区域ごとの基準日を特定する一次証拠として利用できる。</strong>
7本の法律の指定沿革を個別に再構成する作業の相当部分を代替しうる。</p>

<p>なお指定が拡大しているという事実は、前掲の所見を弱めるものではない。
維持義務の対象は期間を通じて増えており、それにもかかわらず過疎地の減少が速い。</p>

<div class="verdict v-check">要精査 <span>維持義務の不履行と断定するには、以下の留保の解消が必要</span></div>

<ul class="caveat">
<li><strong>過疎地の定義が施行規則と異なる。</strong>本節は過疎法指定を用いたが、施行規則4条5項の「過疎地」は
離島振興法・山村振興法・半島振興法・奄美・小笠原・沖縄離島を含む7類型である。公式フラグでは8,150局が該当する。</li>
<li><strong>基準時が揃っていない。</strong>附則4条は区域ごとに基準日を定める（施行時、またはその後に過疎地となった時点）が、
本節は現行指定を両時点に一律適用している。なお変更届には過疎地フラグの変更が約420件記録されており、
区域別基準日を特定する一次証拠として利用できる見込みである。</li>
<li><strong>「水準」を局数で測っている。</strong>配置やカバー人口による測定は未実施である。</li>
<li><strong>2026年側が過小の可能性。</strong>長期休止の簡易郵便局653局は個体が特定できず市区町村に割り当てられていない。
簡易局は過疎地に多いと見込まれるため、割当が進めば過疎地の形式局数は増える方向に働く。</li>
<li><strong>一部過疎の区域が団体全域ではない。</strong>上表の「一部過疎のみ」は団体単位の集計であり、
指定対象である旧市町村区域に限定した集計ではない。</li>
</ul>

<h2><span class="rq">RQ-05</span>偏在は拡大したか</h2>
<p>2020年国勢調査人口を分母として、市区町村単位の不均等指標を算出した。</p>

<div class="tw"><table>
<thead><tr><th>指標</th><th class="n">2013 形式</th><th class="n">2013 実効</th><th class="n">2026 形式</th><th class="n">2026 実効</th></tr></thead>
<tbody>
{"".join(f'<tr><td>{nm}</td>' + "".join(f'<td class="n">{S["inequality"][k][key]:.4f}</td>' for k in ("2013_formal","2013_effective","2026_formal","2026_effective")) + "</tr>" for nm, key in (("ジニ係数","gini"),("フーバー指数","hoover"),("タイル指数","theil")))}
</tbody></table></div>

<figure style="max-width:400px">{lorenz_chart()}
<figcaption>人口に対する局数のローレンツ曲線。2本はほぼ重なっており、分布形状は13年間で大きく変わっていない。</figcaption></figure>

<div class="callout q">
<div class="hd">指標の低下を改善と読んではならない</div>
<p>3指標はいずれも僅かに低下している。しかしこれは配置が改善したことを意味しない。
過疎地は1局あたり人口が2,230人（2013年）と、非過疎地の7,237人に比べて人口対比で厚く配置されている。
その<strong>厚い側で局が速く減ったため、機械的に不均等度が下がった</strong>のである。
指標の低下は「手厚い側が削られたことによる収束」であり、アクセスの改善ではない。</p>
</div>

<p>タイル指数を分解すると、不均等の<strong>{S["inequality"]["2026_effective"]["within_share"]:.0f}%は都道府県内の格差</strong>であり、
都道府県間の格差（{S["inequality"]["2026_effective"]["between_share"]:.0f}%）を上回る。
市区町村単位の集計では捉えきれない域内の偏在が残ることを示唆しており、次節のメッシュ単位の分析につながる。</p>

<h3>減少は日本海側に集中している</h3>

<figure>{map_chart()}
<figcaption>市区町村別の実効局数の増減率（2013年11月→2026年6月）。
1,741の法的単位のうち地図上に描画したのは1,683（本土）＋44（沖縄）。
全国縮尺で画素以下となる小規模市区町村は描画から外れている。
北方領土6村は境界データ上は存在するが対象外とした。
東京都府中市は後述の解析不具合により暫定値。</figcaption></figure>

<p>日本海側、東北北部、南九州から南西諸島にかけて濃い階級が連なる一方、
首都圏・中京圏・近畿の平野部は薄い。
都道府県に集約すると傾向はより明瞭になる。</p>

<figure>{pref_chart()}
<figcaption>都道府県別の実効局数増減率。赤は5%以上の減少、青は2%未満の減少。
石川・福井・富山の北陸3県と、鳥取・島根の山陰2県が上位に並ぶ。</figcaption></figure>

<div class="tw"><table>
<thead><tr><th>都道府県</th><th class="n">2013 実効</th><th class="n">2026 実効</th><th class="n">増減率</th><th class="n">万人当り</th><th class="n">過疎団体率</th></tr></thead>
<tbody>{prefrows()}</tbody></table></div>

<h2><span class="rq">RQ-02</span>容易に利用できない地域はあるか</h2>
<p>2026年の23,459局の住所を国土交通省の位置参照情報でジオコーディングし、
令和2年国勢調査の500mメッシュ人口（466,145メッシュ、125,747,793人。全国値の99.68%）に対して
最寄局までの測地線距離を算出した。2013年は国土数値情報の実測座標をそのまま用いる。</p>

<h3>座標の精度をP30との突合で検証した</h3>
<p>ジオコーディングの精度区分は街区レベル一致15,720件（67.0%）、町丁目レベル7,599件（32.4%）、
市区町村代表点139件（0.6%）、不一致1件である。
2013年と2026年の両方に存在する23,019局について座標差を測ると、
街区レベルの局は<strong>中央値45.0m・90パーセンタイル133.7m</strong>で、同一施設として妥当な範囲に収まる。
一方、町丁目レベルの局は中央値785.9mで、これは代表点誤差であり実際の移転ではない。</p>

{access_results_section()}

<h3>悪化は過疎地と低密度地域に集中している</h3>
<figure>{strat_chart()}
<figcaption>1km圏内カバー率の変化（2013→2026、実効系列）。人口密度階級はDID境界が入手できなかったためメッシュ人口密度で代用した。</figcaption></figure>

{strat_narrative()}

<h3>空間統計も再計算待ち</h3>
<p>旧Global Moran's I・Gi*は撤回した座標系列に基づくため、現行結論には使用しない。
保守的継承による gold 成果物を再生成した後、同じ重み行列・置換回数・固定範囲で再計算する必要がある。
アクセス不良クラスタの居住人口は1,394,336人（全人口の1.11%）。</p>

<p>逆に、2013年に最悪だった福島県双葉郡は<strong>大幅に改善している</strong>
（浪江町 7,965m→1,339m、富岡町 8,712m→1,277m、葛尾村 5,902m→2,288m）。
避難指示区域の郵便局再開が数値に現れている。</p>

<div class="verdict v-check">要精査 <span>直線距離では4条2項2号の「交通の事情」を評価できない</span></div>

<p class="caveat">施行規則4条2項2号は「<strong>交通、地理その他の事情を勘案して</strong>地域住民が容易に利用することができる位置」を求める。
本稿の距離はすべて<strong>直線距離（測地線）</strong>であり、道路ネットワーク距離も公共交通所要時間も算出していない。
山地・離島・半島では直線距離と実際の到達時間の乖離が大きく、
悪化が集中している地域はまさにそうした地形である。したがって本節の数値は<strong>実態を過小評価している可能性が高い</strong>。
2km・1kmといった距離帯は法令上の基準ではなく分析シナリオである。</p>

<h2><span class="rq">留保</span>この検証で答えられないこと</h2>
<ul>
<li><strong>道路距離・所要時間。</strong>4条2項2号の中核である「交通の事情」は直線距離では評価できない。
道路ネットワークとGTFSによる到達圏分析が必要である。</li>
<li><strong>測地系変換が未適用。</strong>2013年座標はJGD2000のままである。
JGD2011への変換に必要な地殻変動グリッドがPROJの配布物に含まれず、
pyprojが選択したのはパラメータ0の恒等変換だった（全24,526件で移動量0.000m）。
東日本で最大4m級のずれが残るが、町丁目代表点の誤差（中央値786m）に比べれば小さい。</li>
<li><strong>変化量の点推定は未評価。</strong>旧計算は未封印の照合表と距離制限のない座標継承に依存したため撤回した。
封印済み座標間の同一自治体・同一名称・一意照合と1km距離ゲートによる再計算が完了するまで、数値幅も引用してはならない。</li>
<li><strong>層別結果と空間統計は再計算待ち。</strong>旧値は旧座標系列に基づく。低精度座標は農山村に偏るため、
過疎／非過疎の比、密度勾配、Moran's I を現行結論に使用しない。</li>
<li><strong>メッシュ人口は2020年で固定。</strong>人口移動の効果は分離していない。</li>
<li><strong>個票単位の閉鎖履歴。</strong>一時閉鎖・再開は届出事項ではないため、
どの局がいつ閉鎖したかは復元できていない。月次の実効系列は全国・都道府県の集計値としてのみ得られる。
市区町村単位・メッシュ単位で実効値を扱えるのは2013年と2026年の2断面に限られる。</li>
<li><strong>2018年9月以前の実効系列。</strong>公式局数表は2018年10月分より前がこの形式で取得できず、
実効系列の起点は2018年10月である。形式系列は2012年10月まで遡れる。</li>
<li><strong>653局の所在。</strong>長期休止の簡易郵便局は一覧に脚注の総数しか記載がなく、個体を特定できていない。
座標を付与できないため全系列から「割当不能」として除外した。
全て長期休止であるため実効系列には元々含まれず、実効ベースの結論は影響を受けない。
形式系列では2026年側が過小に出る（＝2026年が実際より悪く見える）方向に働く。</li>
<li><strong>個票の閉鎖履歴。</strong>市区町村単位・メッシュ単位で実効値を扱えるのは2013年と2026年の2断面に限られる。</li>
</ul>

<p class="caveat">なお、解析初期に現況一覧の東京都府中市24局で名称と所在地の列境界が1文字ずれる不具合があった
（住所が「京都府中市…」となり、京都府で始まる正当な住所として検証を通過してしまう）。
これは解析器の修正により解消済みで、本稿の数値は修正後のものである
（名称が郵便局・分室・出張所で終わらないレコードは現在0件）。</p>

<div class="callout q">
<div class="hd">法的評価との距離</div>
<p>本稿の数値は、法令上の基準への適合性と、分析上の不利とを区別して提示している。
ジニ係数の水準や局数の減少それ自体は違法性を意味しない。
施行規則4条2項1号が求めるのは「地域住民の需要への適切な対応」であって均等配置ではなく、
人口が偏在する以上、局が偏在することは当然に予期される。
明確に指摘できるのは、<strong>4条1項という一義的な基準への適合</strong>と、
<strong>4条2項3号が名宛人とする過疎地でこそ減少が速いという事実</strong>である。
後者の法的評価には、上記の留保の解消を要する。</p>
</div>

<footer>
<p>出典: 日本郵便株式会社「日本郵便株式会社法第6条第2項の規定による届出」一覧（2026年6月30日現在）／
国土数値情報 郵便局データ P30（2013年度、国土交通省。非商用利用条件）／
総務省 全国地方公共団体コード・過疎地域市町村等一覧／総務省統計局 令和2年国勢調査。
いずれも出典を明示のうえ加工して利用した。</p>
<p>距離・比率の閾値および指標の選択は分析上のシナリオであり、法令上の数値基準ではない。
本稿は法令適合性の判定を行うものではない。</p>
</footer>
</div>
"""

OUT.write_text(f"<title>郵便局の地理的偏在に関する検証</title>\n<style>{CSS}</style>\n{BODY}",
               encoding="utf-8")
print(f"wrote {OUT}  ({OUT.stat().st_size:,} bytes)")
