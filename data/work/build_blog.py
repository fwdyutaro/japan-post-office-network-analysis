"""ブログ記事版を生成する。

数値は build_report と同じ stats.json 由来（直書きしない）。
アクセシビリティの距離・カバー率は撤回済みのため一切載せない。
"""
from __future__ import annotations
import json, sys, html
from pathlib import Path

from project_paths import work_dir

W = work_dir()
sys.path.insert(0, str(W))
import build_report as B  # noqa: E402  (charts + stats)

S = B.S
OUT = W / "postal_blog.html"
ANIM_URL = "https://github.com/fwdyutaro/japan-post-office-network-analysis/releases/tag/v1.0.0"

RC = json.loads((W / "accessibility_recalculated.json").read_text(encoding="utf-8"))
RR, RS = RC["results"], RC["stratified"]
IP = RC["inheritance_policy"]
# 計算できた2系列。corrected は not_evaluable。
# 最寄局距離は複数局にわたる最小値なので、座標を一部だけ差し替えた混合系列が
# 両端の集計値の間に必ず収まる保証は無い。したがってこの2値は「試算2通り」であって
# 真値の上下限ではない。本文でもそのように書くこと。
LO, HI = RR["inherit_all"], RR["isj"]
B13 = RR["p30_2013"]

# ---------------------------------------------------------------- 法令
# 条文は e-Gov 法令API（日本郵便株式会社法施行規則・平成十九年総務省令第三十七号）から取得した原文。
LAW = {
    "法": "日本郵便株式会社法（平成十七年法律第百号）",
    "規則": "日本郵便株式会社法施行規則（平成十九年総務省令第三十七号）",
    "民営化法": "郵政民営化法（平成十七年法律第九十七号）",
    "郵便法": "郵便法（昭和二十二年法律第百六十五号）",
    "改正法2012": "郵政民営化法等の一部を改正する等の法律（平成二十四年法律第三十号）",
    "改正法2026民営化": "郵政民営化法等の一部を改正する法律（令和八年法律第五十号）",
    "改正法2026郵便": "郵便法及び民間事業者による信書の送達に関する法律の一部を改正する法律（令和八年法律第四十二号）",
    "機構法": "独立行政法人郵便貯金簡易生命保険管理・郵便局ネットワーク支援機構法（平成十七年法律第百一号）",
    "簡易局法": "簡易郵便局法（昭和二十四年法律第二百十三号）",
    "過疎法": "過疎地域の持続的発展の支援に関する特別措置法（令和三年法律第十九号）",
}

ART = {
    "法6-1": "会社は、総務省令で定めるところにより、あまねく全国において利用されることを旨として郵便局を設置しなければならない。",
    "規則4-1本文": "法第六条第一項の規定に基づく郵便局の設置については、会社は、いずれの市町村（特別区を含む。）においても、一以上の郵便局を設置しなければならないものとする。",
    "規則4-1但書": "ただし、郵便窓口業務及び保険窓口業務を行う会社の営業所（関連銀行の営業所が併設されている場合に限る。）が当該市町村（特別区を含む。）において一以上設置されている場合又は郵便窓口業務及び銀行窓口業務を行う会社の営業所（関連保険会社の営業所が併設されている場合に限る。）が当該市町村（特別区を含む。）において一以上設置されている場合その他の合理的な理由があると総務大臣が認める場合は、この限りでない。",
    "規則4-2柱書": "前項の基準によるほか、会社は、次に掲げる基準により、郵便局を設置しなければならない。",
    "規則4-2-1": "地域住民の需要に適切に対応することができるよう設置されていること。",
    "規則4-2-2": "交通、地理その他の事情を勘案して地域住民が容易に利用することができる位置に設置されていること。",
    "規則4-2-3": "過疎地においては、郵政民営化法等の一部を改正する等の法律（平成二十四年法律第三十号）の施行の際現に存する郵便局ネットワークの水準を維持することを旨とすること。",
    "規則4-3": "前二項の規定によるほか、会社は、会社の営業所であって郵便窓口業務を行うもののうち銀行窓口業務又は保険窓口業務を行わないものを郵便局に準ずるものとして前項に掲げる基準により設置しなければならない。",
}

_mf = {r["m"]: r["t"] for r in json.loads((W / "monthly_formal.json").read_text(encoding="utf-8"))}
BM = {"base": _mf["2012-10"], "p30": _mf["2013-11"], "last": _mf["2026-06"]}

K = S["kaso"]["all885"]
A = S["animation"]
M = S["monthly"]
OF, OL = S["official"]["first"], S["official"]["last"]
INQ = S["inequality"]
ratios = [S["kaso"][k]["ratio_eff"] for k in ("all885", "full_only", "full_plus_deemed", "partial_only")]


def timeline_chart() -> str:
    """制度と経営計画の年表。"""
    events = [
        ("2007.10", "郵政民営化", "郵便局会社が発足。設置基準は総務省令に委ねられる", "statute"),
        ("2012.10", "改正法施行", "郵便事業会社と統合し日本郵便に。<b>この時点の水準が過疎地の基準</b>", "seal"),
        ("2013.11", "国土数値情報 P30", "本稿が比較の起点に使う全国断面（24,526局）", "mid"),
        ("2019.04", "交付金・拠出金制度", "ネットワーク維持を金融2社の拠出で支える仕組みが稼働", "statute"),
        ("2026.05", "JPプラン2028", "集配拠点を約3,200→約2,700に集約する方針", "seal"),
        ("2026.06", "本稿の観測時点", "届出一覧の基準日（24,112局）", "mid"),
    ]
    w, h = 700, 258
    pl, pr = 8, 8
    y = 74
    n = len(events)
    step = (w - pl - pr) / (n - 1)
    g = [f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="郵政民営化から中期経営計画までの制度年表">']
    g.append(f'<line x1="{pl}" y1="{y}" x2="{w-pr}" y2="{y}" stroke="var(--rule)" stroke-width="2"/>')
    for i, (date, title, desc, col) in enumerate(events):
        x = pl + i * step
        c = f"var(--{col})" if col != "mid" else "var(--mid)"
        # 端の列は中央寄せだと描画域からはみ出すので、左右へ寄せる。
        if i == 0:
            anc, tx = "start", x - 6
        elif i == n - 1:
            anc, tx = "end", x + 6
        else:
            anc, tx = "middle", x
        g.append(f'<circle cx="{x:.1f}" cy="{y}" r="6" fill="{c}"/>')
        g.append(f'<text x="{tx:.1f}" y="{y-30}" text-anchor="{anc}" class="tl-date" fill="{c}">{date}</text>')
        g.append(f'<text x="{tx:.1f}" y="{y-14}" text-anchor="{anc}" class="tl-title">{title}</text>')
        # 説明は隣と重ならないよう上下2段に振り分ける
        ty = y + 24 + (0 if i % 2 == 0 else 58)
        words = desc.replace("<b>", "").replace("</b>", "")
        lines, cur = [], ""
        for ch in words:
            cur += ch
            if len(cur) >= 11:
                lines.append(cur); cur = ""
        if cur:
            lines.append(cur)
        # 段を下げた列は、線から説明までを細い縦線でつなぐ
        if i % 2:
            g.append(f'<line x1="{x:.1f}" y1="{y+8}" x2="{x:.1f}" y2="{ty-11}" '
                     f'stroke="var(--rule)" stroke-width="1"/>')
        for j, ln in enumerate(lines[:4]):
            g.append(f'<text x="{tx:.1f}" y="{ty + j*13}" text-anchor="{anc}" '
                     f'class="tl-desc">{html.escape(ln)}</text>')
    g.append("</svg>")
    return "".join(g)


def strat_bars() -> str:
    """層別の1km圏内カバー率の変化。2系列の幅を帯で示す。"""
    rows = [("過疎地", "kaso"), ("非過疎地", "non_kaso"), (None, None),
            ("人口密度 高（DID相当）", "did_like_ge4000"),
            ("人口密度 中", "mid_1000_4000"),
            ("人口密度 低", "sparse_lt1000")]
    vals = [(RS["inherit_all"][k]["delta_cov1km_pt"], RS["isj"][k]["delta_cov1km_pt"])
            for _, k in rows if k]
    worst = min(min(a, b) for a, b in vals)
    w, pl, bh, gap = 660, 172, 20, 12
    h = 30 + len(rows) * (bh + gap)
    zero = w - 118   # 右側に「-6.23 〜 -5.31pt」が収まる幅を確保する
    scale = (zero - pl - 14) / abs(worst)
    g = [f'<svg viewBox="0 0 {w} {h}" role="img" '
         f'aria-label="1km圏内カバー率の変化、過疎地別と人口密度階級別、2系列の幅">']
    g.append(f'<line x1="{zero}" y1="10" x2="{zero}" y2="{h-22}" class="grid"/>')
    for i, (lab, key) in enumerate(rows):
        if not lab:
            continue
        y = 14 + i * (bh + gap)
        a = RS["inherit_all"][key]["delta_cov1km_pt"]
        b = RS["isj"][key]["delta_cov1km_pt"]
        lo, hi = min(a, b), max(a, b)
        x1 = zero - abs(lo) * scale
        x2 = zero - abs(hi) * scale
        col = "var(--seal)" if lo < -3 else ("var(--d2)" if lo < -1 else "var(--statute)")
        g.append(f'<text x="{pl-10}" y="{y+bh-5}" class="tick end">{lab}</text>')
        g.append(f'<rect x="{x1:.1f}" y="{y}" width="{max(x2-x1,1.5):.1f}" height="{bh}" '
                 f'fill="{col}" rx="1.5"/>')
        g.append(f'<text x="{zero+9}" y="{y+bh-5}" class="num">{lo:+.2f} 〜 {hi:+.2f}pt</text>')
    g.append(f'<text x="{zero}" y="{h-6}" class="sub mid">0</text>')
    g.append("</svg>")
    return "".join(g)


def acc_rows() -> str:
    out = [f'<tr><td>2013年 実効（実測点）</td><td class="n">{B13["mean_m"]:.1f}m</td>'
           f'<td class="n">{B13["cov"]["1000"]:.2f}%</td><td class="n">{B13["over2km"]:,}</td>'
           f'<td class="n">—</td></tr>']
    for lab, r, cls in (("2026年 実効 · 継承を最大限に適用", LO, ""),
                        ("2026年 実効 · 継承なし", HI, "")):
        out.append(f'<tr{cls}><td>{lab}</td><td class="n">{r["mean_m"]:.1f}m</td>'
                   f'<td class="n">{r["cov"]["1000"]:.2f}%</td><td class="n">{r["over2km"]:,}</td>'
                   f'<td class="n seal">{r["delta_over2km_pct"]:+.1f}%</td></tr>')
    return "".join(out)


def percap_chart() -> str:
    """人口1万人あたり実効局数。集計値と市区町村分布（中央値・四分位）を並べる。"""
    rows = [("過疎地", 4.24, 5.85, 3.84, 8.98, "var(--seal)"),
            ("非過疎地", 1.34, 1.61, 1.07, 2.49, "var(--statute)")]
    w, h, pl, pr = 660, 156, 96, 118
    xmax = 10.0
    span = w - pl - pr
    def X(v): return pl + v / xmax * span
    g = [f'<svg viewBox="0 0 {w} {h}" role="img" '
         f'aria-label="人口1万人あたりの実効局数、過疎地と非過疎地の比較">']
    for gv in range(0, 11, 2):
        g.append(f'<line x1="{X(gv):.1f}" y1="16" x2="{X(gv):.1f}" y2="{h-34}" class="grid"/>')
        g.append(f'<text x="{X(gv):.1f}" y="{h-18}" class="tick mid">{gv}</text>')
    for i, (lab, agg, med, q1, q3, col) in enumerate(rows):
        y = 30 + i * 52
        g.append(f'<text x="{pl-12}" y="{y+14}" class="tick end">{lab}</text>')
        # 四分位範囲を帯、中央値を縦棒、集計値を菱形
        g.append(f'<rect x="{X(q1):.1f}" y="{y}" width="{X(q3)-X(q1):.1f}" height="22" '
                 f'fill="{col}" opacity=".25" rx="2"/>')
        g.append(f'<line x1="{X(med):.1f}" y1="{y-3}" x2="{X(med):.1f}" y2="{y+25}" '
                 f'stroke="{col}" stroke-width="2.5"/>')
        g.append(f'<polygon points="{X(agg):.1f},{y+4} {X(agg)+6:.1f},{y+11} '
                 f'{X(agg):.1f},{y+18} {X(agg)-6:.1f},{y+11}" fill="{col}"/>')
        g.append(f'<text x="{w-pr+10}" y="{y+9}" class="num">集計 {agg:.2f}</text>')
        g.append(f'<text x="{w-pr+10}" y="{y+22}" class="sub">中央値 {med:.2f}</text>')
    g.append(f'<text x="{pl}" y="{h-4}" class="sub">局／人口1万人（2026年6月・実効／2020年国勢調査人口）'
             f'　帯＝四分位範囲、縦棒＝中央値、◆＝集計値</text>')
    g.append("</svg>")
    return "".join(g)


def kpi(label, value, sub, cls=""):
    return (f'<div><div class="k">{label}</div><div class="v {cls}">{value}</div>'
            f'<div class="d">{sub}</div></div>')


CSS = B.CSS + """
.lede{font-size:18px; line-height:1.85; color:var(--ink); max-width:62ch}
.byline{font-size:12.5px; color:var(--muted); margin-top:20px}
h2{font-size:23px}
.q{margin:34px 0; padding:0 0 0 22px; border-left:3px solid var(--seal);
   font-family:"Hiragino Mincho ProN","Yu Mincho",YuMincho,serif;
   font-size:19px; line-height:1.75; color:var(--ink); max-width:60ch}
svg .tl-date{font-size:10.5px; font-weight:700; letter-spacing:.03em}
svg .tl-title{font-size:12px; fill:var(--ink); font-weight:600}
svg .tl-desc{font-size:10px; fill:var(--muted)}
.grid2{display:grid; grid-template-columns:1fr 1fr; gap:26px; margin:26px 0}
@media(max-width:700px){.grid2{grid-template-columns:1fr}}
.note{font-size:13px; color:var(--muted); background:var(--raise);
  border:1px solid var(--rule); border-radius:3px; padding:14px 18px; margin:22px 0; max-width:70ch}
.note b{color:var(--body)}
ol.method{counter-reset:st; list-style:none; padding-left:0; max-width:68ch}
ol.method>li{counter-increment:st; position:relative; padding-left:44px; margin-bottom:18px}
ol.method>li::before{content:counter(st); position:absolute; left:0; top:1px;
  width:28px; height:28px; border-radius:50%; background:var(--statute); color:#fff;
  font-size:13px; font-weight:700; display:flex; align-items:center; justify-content:center;
  font-family:ui-monospace,Consolas,monospace}
ol.method>li b{display:block; color:var(--ink); font-size:15.5px; margin-bottom:3px}
blockquote.stat{margin:22px 0; padding:18px 22px; background:var(--raise);
  border:1px solid var(--rule); border-left:3px solid var(--statute); border-radius:3px;
  font-family:"Hiragino Mincho ProN","Yu Mincho",YuMincho,"Noto Serif JP",serif;
  font-size:15px; line-height:1.9; color:var(--ink); max-width:68ch}
blockquote.stat p{margin:0 0 8px; max-width:none}
blockquote.stat p:last-of-type{margin-bottom:0}
blockquote.stat .proviso{text-indent:0; color:var(--body); font-size:14.5px}
blockquote.stat cite{display:block; margin-top:11px; font-style:normal; font-size:12px;
  letter-spacing:.04em; color:var(--muted);
  font-family:"Hiragino Kaku Gothic ProN","Yu Gothic",sans-serif}
blockquote.stat ol.items{margin:6px 0 0; padding-left:1.6em; max-width:none}
blockquote.stat ol.items li{margin-bottom:5px}
"""

BODY = f"""
<div class="wrap">
<header>
  <p class="eyebrow">データで読む郵政 · 全国24,112局を13年分たどる</p>
  <h1>郵便局は「減っていない」のか<br>——全国平均が隠す、局地的な空洞化</h1>
  <p class="lede">郵便局の総数は、この13年でわずか1.7%しか減っていない。
  では地域ごとに見ても同じか。届出データ165か月分と2013年の全国断面を突き合わせると、
  平均値の裏で起きていることは、かなり違って見えてくる。</p>
  <p class="byline">日本郵便株式会社法6条2項の届出一覧、国土数値情報P30、公式局数表、令和2年国勢調査に基づく独自集計。
  数値はすべて実データからの算出値。{S["legal_units"]:,}市区町村・{A["cells"]:,}区画・{A["months"]}か月。</p>
</header>

<h2>前提 制度はいま、どこに立っているか</h2>

<p>2026年の通常国会——第221回国会で、郵政に関する法律が二本改正された。
どちらも<strong>郵便局ネットワークをどう支えるか</strong>という同じ問題に向き合っている。</p>

<div class="grid2">
<div class="callout"><div class="hd">令和8年法律第42号（2026年6月19日公布）</div>
<p><strong>{LAW["改正法2026郵便"]}</strong>。内閣提出。
定形郵便物の料金上限について、<strong>総務省令で定める制度から、日本郵便の申請に基づき総務大臣が認可する制度へ</strong>改めた。
経営環境の変化に応じて機動的に料金を変更できるようにするものである。</p></div>
<div class="callout q"><div class="hd">令和8年法律第50号（2026年6月25日公布）</div>
<p><strong>{LAW["改正法2026民営化"]}</strong>。<strong>衆議院総務委員長提出</strong>（6月11日提出、6月19日成立）。
郵政民営化法・日本郵政株式会社法・日本郵便株式会社法・{LAW["機構法"]}を一括改正し、
<strong>郵便局ネットワークの維持・活用への交付金を拡充</strong>した。</p></div>
</div>

<p>後者はとくに立ち入っている。柱は三つある。
第一に<strong>自助努力による経営効率化等の義務付け</strong>——AI等のデジタル技術を活用した業務プロセス改善、
経営資源の有効活用、事業計画への経営の適正・効率的実施の方針の追加。
第二に<strong>ユニバーサルサービスの確保</strong>——日本郵政に当分の間ゆうちょ銀行・かんぽ生命株式の3分の1超の保有を義務付け、
銀行・保険の窓口業務契約を届出制から<strong>認可制</strong>に改めた。</p>

<p>第三が本稿に直接かかわる。<strong>郵便局ネットワーク等の活用による地域住民の生活の支援</strong>である。
委託を受けて郵便・銀行・保険の経営資源を活用し公共サービス等を提供する<strong>基盤的サービス提供業務</strong>を本来業務に加え、
<strong>地域貢献業務</strong>の努力義務と、その費用に充てる<strong>地域貢献基金</strong>の設置義務を新設した。
そのうえで、<strong>条件不利地域を含む全国の郵便事業等の安定的な業務遂行の確保に向けて</strong>、
郵便局ネットワークの維持・活用に要する費用の一部に充てる新たな交付金を設けている。
財源は政府保有の日本郵政株の配当減額相当額の拠出金と、権利消滅した旧郵便貯金である。
附則では、公布後2年を目途に<strong>日本郵政と日本郵便の合併</strong>を積極的に検討すること、
郵便局ネットワークの維持に要する費用負担の在り方を検討することまで書き込まれた。</p>

<p class="q">料金の決め方を変え、交付金を積み増し、基金を作り、合併まで検討する。
<strong>立法府がここまで手当てするのは、郵便局ネットワークの維持が課題になっているからである。</strong>
条文が「条件不利地域を含む全国の」と書いているのも、そのことの裏返しだろう。</p>

<p>では、その課題はどこに、どのくらいの大きさで現れているのか。
本題に入る前に、いま制度がどの地点にあるのかを整理しておきたい。</p>

<figure>{timeline_chart()}
<figcaption>郵政民営化から現行の中期経営計画まで。赤は本稿の分析に直接関わる時点。</figcaption></figure>

<h3>2012年の改正が「基準時」を作った</h3>
<p>2012年10月、{LAW["改正法2012"]}が施行され、郵便事業会社と郵便局会社が統合して日本郵便株式会社が発足した。
このとき同時に、郵便局の設置についての省令が改められている。ここが重要で、
<strong>施行規則は「過疎地においては、この改正法の施行の際現に存する郵便局ネットワークの水準を維持することを旨とする」と定めた</strong>。
つまり2012年10月時点の姿が、過疎地についてのベンチマークとして法令に書き込まれた。</p>

<h3>設置基準は、実は二段構えになっている</h3>
<p>{LAW["法"]}6条1項は、こう定める。</p>

<blockquote class="stat"><p>{ART["法6-1"]}</p><cite>{LAW["法"]}第六条第一項</cite></blockquote>

<p>具体的な基準は総務省令に委ねられている。それが{LAW["規則"]}4条である。
同条が置く基準は、性質の違う二種類が重なっている。まず1項。</p>

<blockquote class="stat"><p>{ART["規則4-1本文"]}</p>
<p class="proviso">{ART["規則4-1但書"]}</p>
<cite>{LAW["規則"]}第四条第一項</cite></blockquote>

<p>本文は<strong>数えられる基準</strong>である。市町村ごとに1以上あるかどうかは機械的に判定できる。
一方でただし書きは、併設営業所がある場合その他<strong>総務大臣が合理的な理由を認める場合</strong>を例外としている。
つまり0局であっても直ちに違反とはならない。本稿が「0局の団体は確認されなかった」までしか述べないのはこのためである。</p>

<p>続いて2項。</p>

<blockquote class="stat"><p>{ART["規則4-2柱書"]}</p>
<ol class="items">
<li>{ART["規則4-2-1"]}</li>
<li>{ART["規則4-2-2"]}</li>
<li><strong>{ART["規則4-2-3"]}</strong></li>
</ol>
<cite>{LAW["規則"]}第四条第二項</cite></blockquote>

<p>こちらは<strong>評価を要する基準</strong>で、いずれも数値閾値や算式は定められていない。
ただし3号は「{LAW["改正法2012"]}の施行の際現に存する」水準との比較を求めているので、
<strong>定量的な評価そのものは可能である</strong>。本稿が測ろうとしているのは、まさにこの3号の水準である。</p>

<p>なお3項は、銀行窓口業務も保険窓口業務も行わない営業所——実務上その多くは簡易郵便局である——について、こう定める。</p>

<blockquote class="stat"><p>{ART["規則4-3"]}</p>
<cite>{LAW["規則"]}第四条第三項</cite></blockquote>

<p>さらに4条5項は「過疎地」を定義しているが、これが{LAW["過疎法"]}だけを指すのではない点は見落とされやすい。
離島振興法（昭和二十八年法律第七十二号）、山村振興法（昭和四十年法律第六十四号）、
半島振興法（昭和六十年法律第六十三号）、奄美群島振興開発特別措置法（昭和二十九年法律第百八十九号）、
小笠原諸島振興開発特別措置法（昭和四十四年法律第七十九号）、
沖縄振興特別措置法（平成十四年法律第十四号）に定める離島を含む<strong>7類型</strong>である。
本稿で「過疎地」を扱うときは、どの定義かを毎回明示する。</p>

<p>また同規則の附則4条は、改正法の施行日に過疎地であった地域だけでなく、
<strong>その後に過疎地となった地域についても、その該当することとなった時点を基準時として読み替える</strong>と定めている。
基準時は全国一律ではなく、区域ごとに異なりうる。</p>

<h3>そして2026年5月、新しい中期経営計画が出た</h3>
<p>日本郵政グループは2026年5月15日、2026〜2028年度を計画期間とする「JPプラン2028」を公表した。
郵便物数の減少を背景に、<strong>集配拠点を約3,200から約2,700へ、約500拠点集約する</strong>方針が示されている
（コスト削減額 約50億円）。要員削減も併せて計画されている。</p>

<div class="note">
<b>ここは注意が要る。</b>「集配拠点の集約」は「郵便局の廃止」と同義ではない。
集配拠点は配達の作業拠点であり、設置基準が対象とする郵便局＝窓口とは別の概念である。
集配機能が近隣局に移っても、窓口としての郵便局は残りうる。
報道ではこの二つがしばしば書き分けられていないが、<b>法令上の設置基準が縛るのは後者</b>である。
本稿が数えるのも、届出上の郵便局（窓口）であって集配拠点ではない。
</div>

<h3>もう一つの前提——過疎地は元々「手厚い」</h3>
<p>これから過疎地と非過疎地を比べることになるので、出発点の差を先に押さえておきたい。
<strong>人口当たりで見ると、郵便局は過疎地に圧倒的に多く置かれている。</strong></p>

<figure>{percap_chart()}
<figcaption>人口1万人あたりの実効局数。集計値（総局数÷総人口）と、市区町村ごとの分布（中央値・四分位範囲）を併記した。</figcaption></figure>

<p>過疎地885団体は人口1万人あたり<strong>4.24局</strong>、非過疎地は<strong>1.34局</strong>で、<strong>集計比3.16倍</strong>。
1局あたりの人口で言えば2,359人対7,442人である。
これは大きな自治体に引きずられた数字ではない。市区町村ごとの中央値でも5.85対1.61（<strong>3.64倍</strong>）で、
四分位範囲もほとんど重ならない。非過疎地の中央値以下しか局がない過疎地は885団体中11、
逆に過疎地の中央値以上ある非過疎地は855団体中19しかない。</p>

<div class="note">
<b>この前提は、後の二つの結果の読み方を左右する。</b>
一つは不均等指標（結果5）。人口対比で厚い側が削られれば、指標は機械的に平準化する——<b>下がったから改善した、とは言えない</b>。
もう一つは「過疎地は元々手厚いのだから多少減ってもよいのでは」という見方。
一理あるが、施行規則4条2項3号が求めているのは<b>人口に見合った水準ではなく、2012年時点の水準の維持</b>である。
また過疎地は面積が広く人口密度が低いので、<b>人口当たりで多いことは距離の近さを意味しない</b>（結果8を参照）。
</div>

<h3>それで、実際に減っているのか</h3>
<p>結論から言えば、総数は大きくは減っていない。届出上存在する局は
<strong>{B.F13:,}局（2013年11月）から {B.F26:,}局（2026年6月）へ、{abs(B.pct(B.F13,B.F26)):.1f}%の減少</strong>にとどまる。
13年8か月でこの幅なら、「維持されている」と評価してよさそうに見える。</p>

<figure>{B.slope_chart()}
<figcaption>届出上存在する局（青）と、実際に窓口を開けている局（赤）。
2013年は国土数値情報、2026年は日本郵便の公式局数表による。いずれも休止中の局を含む同一定義で比較している。</figcaption></figure>

<p>ただし同じ図に、もう一本の線がある。<strong>実際に営業している局</strong>で数えると
{B.E13:,}局から{B.E26:,}局へ{abs(B.pct(B.E13,B.E26)):.1f}%の減少で、
減り方が倍以上になる。総数と実勢がずれ始めている。</p>

<p class="q">全国平均が動いていないことは、どの地域でも動いていないことを意味しない。
本稿が確かめたいのは、平均の裏で局地的な偏りが生じていないか、である。</p>

<h2>方法 何を、どう数えたか</h2>

<p>「郵便局が減った」という話は、数え方を決めないと成立しない。
本稿は次の手順を踏んだ。すべて公開資料である。</p>

<ol class="method">
<li><b>届出そのものを一次資料にする</b>
日本郵便は{LAW["法"]}6条2項に基づき、郵便局の名称・所在地を総務大臣に届け出ており、その内容を月次で公開している。
現況一覧（2026年6月30日基準、{B.F26 - 653:,}局＋脚注653局）と、2012年10月以降の変更届177本を取得した。
この一覧には<strong>過疎地フラグと営業中／一時閉鎖の別</strong>が公式に付いている。</li>

<li><b>変更届から月次の状態を復元する</b>
変更届は6,053件の事象を含む。ただし届出は「変更予定」を含むため、そのまま履歴には使えない。
同一施設の事象を連鎖させ、連鎖内で変更前後の状態が接続し、かつ終端が現況一覧と一致することを条件に採用した
（{S["confirm"]["confirmed_event_count"]:,}件、{S["confirm"]["confirmed_event_count"]/S["confirm"]["input_event_count"]*100:.1f}%）。
これで{M["n"]}か月分の断面ができる。</li>

<li><b>2013年の全国断面と突き合わせる</b>
国土数値情報P30（2013年11月末、{B.F13:,}局）は全件に緯度経度と市区町村コードを持つ。
さらに名称末尾の「（一時閉鎖）」表記により、<strong>2013年についても営業中と休止を分離できる</strong>。
法令上の基準時は2012年10月なので、この14か月のずれについては次項で確認した。</li>

<li><b>法的単位に集計する</b>
施行規則4条1項の単位は「市町村（特別区を含む）」である。政令指定都市の行政区は独立の単位ではないため親市に集約し、
東京都特別区23区はそれぞれ独立に扱った。ロシア統治下で設置不能な北方領土6村は対象外とし、
<strong>{S["legal_units"]:,}団体</strong>（公式の市町村数と一致）を母集団とした。</li>

<li><b>公式集計で答え合わせをする</b>
日本郵便は都道府県別の局数表も月次公開している。営業中・閉鎖中を直営・分室・簡易別に収録した表で、
{S["reconcile"]["n"]}か月分と突合した。</li>
</ol>

<h3>基準時は2012年10月。2013年の断面で代用してよいか</h3>
<p>施行規則4条2項3号が水準維持の基準時とするのは<strong>2012年10月</strong>であって、本稿が使うP30の2013年11月末ではない。
14か月のずれがどれだけ効くかを、復元した形式系列で確認した。</p>

<div class="tw"><table>
<thead><tr><th>区間</th><th class="n">総局数</th><th class="n">差</th><th class="n">率</th></tr></thead>
<tbody>
<tr><td>2012年10月（法令上の基準時）</td><td class="n">{BM["base"]:,}</td><td class="n">—</td><td class="n">—</td></tr>
<tr><td>2013年11月（P30断面）</td><td class="n">{BM["p30"]:,}</td><td class="n">{BM["p30"]-BM["base"]:+,}</td><td class="n">{(BM["p30"]-BM["base"])/BM["base"]*100:+.2f}%</td></tr>
<tr class="total"><td>2026年6月（アンカー）</td><td class="n">{BM["last"]:,}</td><td class="n">{BM["last"]-BM["base"]:+,}</td><td class="n">{(BM["last"]-BM["base"])/BM["base"]*100:+.2f}%</td></tr>
</tbody></table></div>

<p>基準時からP30断面までの14か月で、届出上の局数は<strong>わずか{abs(BM["p30"]-BM["base"])}局しか動いていない</strong>。
基準時からアンカーまでの全変化{abs(BM["last"]-BM["base"])}局に対して<strong>{abs(BM["p30"]-BM["base"])/abs(BM["last"]-BM["base"])*100:.1f}%</strong>にすぎず、
この期間の月次増減も最大10局である。<strong>形式系列については、2013年断面を基準時の代理として使って差し支えない。</strong></p>

<div class="note">
<b>ただし実効系列は2012年に遡れない。</b>一時閉鎖は法6条2項の届出事項ではないため変更届に記録が無く、
休止状態が分かるのは<b>2013年11月（P30の「（一時閉鎖）」表記）／2018年10月以降（局数表の閉鎖中欄・都道府県別）／
2026年6月（現況一覧＋脚注653局）</b>の3系統だけである。
2012年10月時点の実効局数を復元する材料は存在しない。
したがって本稿が「実効」で比較しているのは<b>2013年11月を起点とした変化</b>であり、
法令上の基準時からの変化ではない。この点は結果2・結果4の読み方に影響する。
</div>

<div class="note">
<b>結果1〜7は局数ベースの話である。</b>距離で測った結果は結果8にまとめた。
そちらは座標の照合方法に制約が残るため、単一の値ではなく<b>二通りの試算</b>として示している。
</div>

<h2>結果1 母集団の答え合わせが、1局の狂いもなく合った</h2>

<p>手順5の突合結果から示す。ここが合わないと、以降の議論はすべて宙に浮く。</p>

<div class="tw"><table>
<thead><tr><th>2026年6月時点</th><th class="n">公式局数表</th><th class="n">本稿の算出</th><th>一致</th></tr></thead>
<tbody>
<tr><td>営業中（直営{int(OL["op_direct"]):,}＋分室{int(OL["op_branch"]):,}＋簡易{int(OL["op_simple"]):,}）</td>
    <td class="n">{int(OL["operating"]):,}</td><td class="n">{int(OL["operating"]):,}</td><td>○</td></tr>
<tr><td>閉鎖中・直営郵便局</td><td class="n">{OL["cl_direct"]}</td><td class="n">{OL["cl_direct"]}</td><td>○</td></tr>
<tr><td>閉鎖中・簡易郵便局</td><td class="n">{OL["cl_simple"]}</td>
    <td class="n">{OL["cl_simple"]}（一覧本体19＋一覧外653）</td><td>○</td></tr>
<tr class="total"><td>総計</td><td class="n">{int(OL["total"]):,}</td><td class="n">{int(OL["total"]):,}</td><td>○</td></tr>
</tbody></table></div>

<p>この一致には副産物がある。現況一覧の本体は{B.F26 - 653:,}局だが、
その脚注に「長期に営業を休止している簡易郵便局が653局ある」と小さく書かれている。
公式局数表の閉鎖中簡易局{OL["cl_simple"]}局から本体掲載の19局を引くと、ちょうど653になる。
<strong>本体だけを見ていると、休止局の8割近くが視界から消える。</strong></p>

<h2>結果2 総数は横ばい、営業中は右肩下がり</h2>

<figure>{B.official_chart()}
<figcaption>公式局数表による全国計。青が届出上存在する局、赤が実際に営業している局。幅が閉鎖中の局にあたる。</figcaption></figure>

<p>総局数は{int(OF["total"]):,}から{int(OL["total"]):,}へ{abs((int(OL["total"])-int(OF["total"]))/int(OF["total"])*100):.1f}%しか減っていない。
一方で営業中の局は{int(OF["operating"]):,}から{int(OL["operating"]):,}へ
{abs((int(OL["operating"])-int(OF["operating"]))/int(OF["operating"])*100):.1f}%減った。
<strong>減少の実勢は形式値の約2.7倍</strong>である。</p>

<figure>{B.closed_chart()}
<figcaption>閉鎖中の局数。破線が総数、塗りが簡易郵便局分。</figcaption></figure>

<p>差がどこから来ているかは、この図で尽きている。閉鎖中の局は
{int(OF["closed"])}局から{int(OL["closed"])}局へ増え、
<strong>増加分の大半が簡易郵便局</strong>である。
簡易郵便局の休止率は2013年の{S["p30_2013"]["simple_closed"]/S["p30_2013"]["simple"]*100:.1f}%から
{int(OL["cl_simple"])/(int(OL["op_simple"])+int(OL["cl_simple"]))*100:.1f}%へ、およそ3倍になった。</p>

<div class="callout">
<div class="hd">一時閉鎖は「廃止」ではない</div>
<p>だから閉鎖中の局も届出一覧に計上され続ける（長期休止の簡易局は脚注の総数のみになる）。
形式上のネットワークはほぼ保たれているように見える。しかし窓口は開いていない。
<strong>施行規則4条2項3号の「水準を維持する」を、届出上の局数で測るか、実際に利用できる局数で測るかによって、評価は変わる。</strong></p>
</div>

<h2>結果3 各市町村に1局以上——これは満たされている</h2>

<p>数えられる基準である4条1項から確認する。</p>

<div class="verdict v-ok">事実 <span>集計対象{S["legal_units"]:,}団体で、郵便局が0局の団体は確認されなかった</span></div>

<p>形式でも実効でも、局数0の団体は{S["zero_units"]["n26f"]}である。
これは<strong>4条1項の原則的な数量基準と整合する</strong>。
ただし同項にはただし書きによる例外（3業務のうち2業務を行う営業所に関連銀行・関連保険会社の営業所が併設されている場合など、
総務大臣が合理的理由を認める場合）があるため、個々の団体について適法性を判定したものではない。
またこれは最低基準にすぎない。
実効で1局しかない団体は<strong>{S["one_office_2026"]}</strong>あり、その多くは小規模な過疎自治体である。</p>

<h3>2013年には、形式的には適合しながら1局も使えない町村があった</h3>
<p>同じ集計を2013年に当てると、形式局数0の団体はやはり0だが、
<strong>実効局数が0の団体が{S["zero_units"]["n13e"]}ある</strong>。</p>

<div class="tw"><table>
<thead><tr><th>団体</th><th class="n">形式局数</th><th class="n">実効局数</th><th>事由</th></tr></thead>
<tbody>
{"".join(f'<tr><td>{html.escape(n)}</td><td class="n">1以上</td><td class="n seal">0</td><td>{"避難指示区域" if n.startswith("福島") else "—"}</td></tr>' for n in S["zero_2013_eff_list"])}
</tbody></table></div>

<p>福島の5町村はいずれも東日本大震災・原発事故の避難指示区域である。
届出上は郵便局が存在し4条1項を形式的に充足しているが、全局が一時閉鎖で1局も利用できなかった。
<strong>形式系列だけを見る検証は、この状態を「適合」と判定してしまう。</strong>
なお2026年時点では、これらの町村はいずれも1局まで回復している。</p>

<h2>結果4 減少は、維持義務のある過疎地でこそ速い</h2>

<p>前掲のとおり、{LAW["規則"]}4条2項3号は過疎地についてのみ水準維持を求めている。
義務が課されている側の減り方が小さいはず——というのが素直な予測である。</p>

<figure>{B.kaso_chart()}
<figcaption>実効局数の減少率。過疎法指定{K["kaso"]["n"]}団体と非指定{K["other"]["n"]}団体の比較。</figcaption></figure>

<p><strong>実測は予測と逆である。</strong>過疎地の実効局数は{K["kaso"]["rate_eff"]:.1f}%減り、
非過疎地の{K["other"]["rate_eff"]:.1f}%に対して約{K["ratio_eff"]:.1f}倍の速さである。</p>

<p>{LAW["過疎法"]}の指定には、市町村全域が対象の「全部過疎」のほか、旧市町村区域のみが対象の「一部過疎」がある。
一括して扱うと過大評価になりうるので、区分ごとに再計算した。</p>

<div class="tw"><table>
<thead><tr><th>過疎地の定義</th><th class="n">団体</th><th class="n">過疎側</th><th class="n">対照</th><th class="n">対照側</th><th class="n">比</th></tr></thead>
<tbody>{B.sens_rows()}</tbody></table></div>

<p>過疎側の減少率は定義によらず狭い範囲に収まり、比も{min(ratios):.2f}〜{max(ratios):.2f}倍で安定している。
<strong>この所見は区分の取り方に対して頑健である。</strong></p>

<div class="verdict v-check">要精査 <span>ただし、これを維持義務の不履行と断定はできない</span></div>

<p>理由は三つある。第一に、本節の過疎地は過疎法ベースであり、施行規則4条5項の7類型ではない。
第二に、附則4条は区域ごとに基準日を定める（施行時、またはその後に過疎地となった時点）が、ここでは現行指定を両時点に一律適用している。
第三に、「水準」を局数で測る解釈を採っており、配置やカバー人口では測っていない。</p>

<div class="note">
<b>補助的な発見。</b>変更届には、施設の構造変更とは別に<b>過疎地フラグの変更が431件</b>記録されていた。
うち過疎地になったものが429件、でなくなったものが2件で、2014年・2017年・2021年に集中する。
附則4条が要求する<b>区域ごとの基準日を特定する一次証拠</b>として使える見込みがある。
なお指定が拡大しているという事実は、上の所見を弱めない。保護対象が増えているのに減少が速い、ということだからである。
</div>

<h2>結果5 不均等指標は下がった。しかしそれは改善ではない</h2>

<p>2020年国勢調査人口を分母に、市区町村単位の不均等度を測った。</p>

<div class="grid2">
<div>
<div class="tw"><table>
<thead><tr><th>指標</th><th class="n">2013 実効</th><th class="n">2026 実効</th></tr></thead>
<tbody>
{"".join(f'<tr><td>{nm}</td><td class="n">{INQ["2013_effective"][k]:.4f}</td><td class="n">{INQ["2026_effective"][k]:.4f}</td></tr>' for nm, k in (("ジニ係数","gini"),("フーバー指数","hoover"),("タイル指数","theil")))}
</tbody></table></div>
</div>
<div>{B.lorenz_chart()}</div>
</div>

<div class="callout q">
<div class="hd">指標の低下を改善と読んではならない</div>
<p>前提で見たとおり、過疎地は人口対比で厚く配置されている。
その厚さは13年で {K["kaso"]["pop_per_office_13"]:,.0f}人/局 → {K["kaso"]["pop_per_office_26"]:,.0f}人/局 と薄くなった一方、
非過疎地は {K["other"]["pop_per_office_13"]:,.0f}人/局 → {K["other"]["pop_per_office_26"]:,.0f}人/局 とほとんど動いていない。
<strong>厚い側だけが削られたのだから、人口対比の分布は機械的に平準化する。</strong>
指標の低下は「手厚い側が削られたことによる収束」であって、アクセスの改善ではない。</p>
</div>

<p>タイル指数を分解すると、不均等の<strong>{INQ["2026_effective"]["within_share"]:.0f}%は都道府県内の格差</strong>であり、
都道府県間（{INQ["2026_effective"]["between_share"]:.0f}%）を上回る。
都道府県単位で眺めているかぎり見えない偏りが、県の中に残っている。</p>

<h2>結果6 地図にすると、日本海側に帯ができる</h2>

<figure>{B.map_chart()}
<figcaption>市区町村別の実効局数の増減率（2013年11月→2026年6月）。
{S["legal_units"]:,}団体のうち地図上に描画したのは本土1,683＋沖縄44。全国縮尺で画素以下となる小規模市区町村は描画から外れている。</figcaption></figure>

<figure>{B.pref_chart()}
<figcaption>都道府県別の実効局数増減率。赤は5%以上の減少、青は2%未満の減少。上位12県と下位6県。</figcaption></figure>

<p>石川・福井・富山の北陸3県と、鳥取・島根の山陰2県が減少幅の上位に並ぶ。
一方で滋賀・京都・埼玉・兵庫・神奈川といった都市圏は1%前後にとどまる。
<strong>全国{abs(B.pct(B.E13,B.E26)):.1f}%という数字は、この差を平均して消してしまっている。</strong></p>

<h2>結果7 区画で見ると、四割が5%以上失っている</h2>

<p>市区町村は面積も人口も大きく違うので、単位を揃えて数え直す。
人口メッシュのある区画だけを残した10km四方の固定格子（{A["cells"]:,}区画）を作り、
各区画の中心から半径20km以内にある局数が2012年10月からどう変わったかを月次で追った。
格子・投影法・検索半径・色階級は全期間で固定してある。</p>

<div class="tw"><table>
<thead><tr><th>2026年6月時点の変化</th><th class="n">区画数</th><th class="n">構成比</th></tr></thead>
<tbody>{B.anim_rows()}</tbody></table></div>

<p class="q">全国の局数は{abs(B.pct(B.monthly[0]["t"], B.monthly[-1]["t"])):.1f}%しか減っていない。
しかし人口のある区画の{A["decline5plus"]/A["cells"]*100:.1f}%では、周辺20km圏の局が5%以上減っている。</p>

<p>同時に{A["pct"]["4"]:.1f}%の区画は変化しておらず、{(A["hist"]["5"]+A["hist"]["6"])/A["cells"]*100:.1f}%はむしろ増えている。
起きているのは一様な縮小ではなく、<strong>動かない地域と削られる地域への二極化</strong>である。</p>

<figure>{B.monthly_chart()}
<figcaption>届出上存在する局数の月次推移（{len(B.monthly)}か月）。現況一覧を起点に事象を逆算して復元した。
履歴アンカーは2026年6月30日で、それより後の月は状態の持越しにすぎないため表示していない。</figcaption></figure>

<p>時間方向で見ると、減少は特定の時期に集中せず、13年8か月にわたりほぼ一定の速度で続いている。
内訳では郵便局が{B.pct(B.monthly[0]["p"], B.monthly[-1]["p"]):.1f}%に対し、
会社の営業所（その大半は簡易郵便局）が{B.pct(B.monthly[0]["c"], B.monthly[-1]["c"]):.1f}%で、減少は後者に強く偏る。</p>

<p><a href="{ANIM_URL}">月次アニメーション</a>では、この165か月を1か月ずつ確認できる。</p>

<h2>結果8 距離で見ると、差はもっと大きい</h2>

<p>ここまでは局数の話だった。最後に、住民から見た距離で測り直す。
2026年の局の住所を国土交通省の位置参照情報でジオコーディングし、
令和2年国勢調査の500mメッシュ人口（{RC["mesh_count"]:,}区画・{RC["total_pop"]:,}人）から
最寄局までの測地線距離を求めた。2013年は国土数値情報の実測座標をそのまま使う。</p>

<div class="note">
<b>人口は両年とも2020年で固定している。</b>2013年の人口分布ではなく、
<b>同じ2020年の人口分布に対して、それぞれの年の郵便局網を当てはめている</b>。
これは意図した設計で、人口移動の効果を除いて<b>局側の変化だけを取り出す</b>ためである。
したがって以下の数値は「2013年と2026年で実際に何人増えたか」ではなく、
<b>「2020年の人口分布を固定したときの曝露人口相当がどれだけ変わるか」</b>を意味する。
実人口の増減を知りたい場合は別の集計が要る。
</div>

<div class="note">
<b>座標の扱いを厳しくした。</b>2026年の住所のうち街区レベルまで特定できないものは、
2013年の実測座標を借りる手が使える。ただし<b>無条件に借りると、その間に移転した局の移動が消えてしまう</b>。
そこで、同一市区町村・同一名称で一意に対応し、<b>かつ2013年の位置から1km以内</b>のものだけに継承を限った。
継承候補のうち採用は{IP["accepted"]:,}件、<b>1km超で棄却が{IP["distance_gate_rejected"]:,}件</b>、名称不一致が{IP["no_exact_name_municipality_match"]}件である。
棄却された{IP["distance_gate_rejected"]:,}件は、実際に動いた局か対応の誤りかのいずれかで、どちらにせよ借りてはいけない。
</div>

<div class="tw"><table>
<thead><tr><th>試算（実効・2020年人口分布を固定）</th><th class="n">平均距離</th><th class="n">1km圏内</th>
<th class="n">2km超の人口相当</th><th class="n">2013年網との差</th></tr></thead>
<tbody>{acc_rows()}</tbody></table></div>

<p>継承を最大限に適用した場合と、まったく適用しない場合の<strong>二通りを試算した</strong>。
2km超圏に入る人口相当は{B13["over2km"]:,}から
<strong>{LO["over2km"]:,}（{LO["delta_over2km_pct"]:+.1f}%）と {HI["over2km"]:,}（{HI["delta_over2km_pct"]:+.1f}%）</strong>。
1km圏内カバー率は{B13["cov"]["1000"]:.2f}%から{LO["cov"]["1000"]:.2f}%・{HI["cov"]["1000"]:.2f}%へ下がる。
65歳以上に限った増加率は{LO["delta_over2km65_pct"]:+.1f}%・{HI["delta_over2km65_pct"]:+.1f}%で、
<strong>どちらの試算でも全人口の増加率を上回る</strong>。</p>

<div class="note">
<b>この2値は「幅」ではない。</b>移転補正系列（中間の仮定）は未評価であり、
しかも最寄局距離は<b>複数の局にわたる最小値</b>なので、座標を一部だけ差し替えた系列が
両端の集計値の間に必ず収まるという数学的保証は無い。
<b>31.5%と33.6%は二つの座標仮定による試算値であって、真値の上下限とみなしてはならない。</b>
</div>

<p class="q">局数は{abs(B.pct(B.E13,B.E26)):.1f}%しか減っていない。
だが2020年の人口分布を固定して両年の郵便局網を当てはめると、
2km超圏の人口相当は二つの試算とも3割以上増える。</p>

<h3>そして差は、過疎地と低密度地域に集中している</h3>

<figure>{strat_bars()}
<figcaption>1km圏内カバー率の変化（2013→2026、実効系列）。帯は2系列の幅。
人口密度階級はDID境界が入手できなかったためメッシュ人口密度で代用した。</figcaption></figure>

<p>過疎地のカバー率低下は{RS["isj"]["kaso"]["delta_cov1km_pt"]:.2f}〜{RS["inherit_all"]["kaso"]["delta_cov1km_pt"]:.2f}ポイント、
非過疎地は{RS["isj"]["non_kaso"]["delta_cov1km_pt"]:.2f}〜{RS["inherit_all"]["non_kaso"]["delta_cov1km_pt"]:.2f}ポイントで、
<strong>およそ1桁の開きがある</strong>。平均距離では過疎地が
{RS["inherit_all"]["kaso"]["delta_mean_m"]:+.0f}〜{RS["isj"]["kaso"]["delta_mean_m"]:+.0f}m 伸びたのに対し、
非過疎地は{RS["inherit_all"]["non_kaso"]["delta_mean_m"]:+.0f}〜{RS["isj"]["non_kaso"]["delta_mean_m"]:+.0f}m にとどまる。</p>

<p>人口密度で切ると、勾配はさらにきれいに出る。
高密度（DID相当、{RS["inherit_all"]["did_like_ge4000"]["population"]:,}人）は
{RS["isj"]["did_like_ge4000"]["delta_cov1km_pt"]:.3f}〜{RS["inherit_all"]["did_like_ge4000"]["delta_cov1km_pt"]:.3f}ポイントとほぼ動かない。
中密度は{RS["isj"]["mid_1000_4000"]["delta_cov1km_pt"]:.2f}〜{RS["inherit_all"]["mid_1000_4000"]["delta_cov1km_pt"]:.2f}、
低密度（{RS["inherit_all"]["sparse_lt1000"]["population"]:,}人）は
<strong>{RS["isj"]["sparse_lt1000"]["delta_cov1km_pt"]:.2f}〜{RS["inherit_all"]["sparse_lt1000"]["delta_cov1km_pt"]:.2f}ポイント</strong>。
<strong>全国平均が小さく見えるのは、動かない都市部が人口の大半を占めているからである。</strong></p>

<div class="note">
<b>この節の限界。</b>三つある。
第一に、<b>中間の系列（移転が確認された局だけ継承を外す）は算出できていない</b>。
移転の実施日を独立に確認できたイベントが0件で、実施日を確定する材料がないためである。
その系列は継承を減らす方向＝「継承なし」寄りに動くと見込まれるが、
最寄局距離は複数局にわたる最小値なので、<b>両端の集計値の間に収まる保証は無い</b>。
上の2値は真値の上下限ではない。
第二に、ここで測っているのは<b>直線距離</b>である。施行規則4条2項2号が求める「交通の事情」は、
道路ネットワーク距離と公共交通の所要時間がなければ評価できない。山地・離島・半島ほど直線距離との乖離は大きく、
悪化が集中しているのはまさにそうした地形なので、<b>この数値は実態を過小評価している可能性が高い</b>。
第三に、計算全体のステータスは <code>not_evaluable</code> のままである。上記2点が解消していないためで、
<b>本節は確定値ではなく、幅としての暫定的な所見である</b>。
</div>

<h2>まとめ</h2>

<div class="pair">
{kpi("形式ネットワーク", f"{B.pct(B.F13,B.F26):.1f}%", f"{B.F13:,} → {B.F26:,}局", "")}
{kpi("実効ネットワーク", f"{B.pct(B.E13,B.E26):.1f}%", f"{B.E13:,} → {B.E26:,}局", "seal")}
{kpi("簡易郵便局の休止率", f"{int(OL['cl_simple'])/(int(OL['op_simple'])+int(OL['cl_simple']))*100:.1f}%", f"2013年は{S['p30_2013']['simple_closed']/S['p30_2013']['simple']*100:.1f}%", "seal")}
{kpi("2km超の人口相当（2020年人口固定）", f"{LO['delta_over2km_pct']:+.0f}%・{HI['delta_over2km_pct']:+.0f}%", f"座標仮定2通りの試算値", "seal")}
</div>

<p>問いは「郵便局は減っているか」だった。総数で見れば{abs(B.pct(B.F13,B.F26)):.1f}%で、大幅な減少とは言いがたい。
だが実際に開いている局で見ると減り方は倍以上になり、その差はほぼ簡易郵便局の休止で説明できる。
地域別に見れば日本海側と山陰に帯ができ、区画単位では四割が周辺の局を5%以上失っている。
距離で測れば、2020年の人口分布を固定した試算で、2km超圏の人口相当は二通りとも3割以上増える。
そして減少は、法が維持を求める過疎地でこそ速い。</p>

<p>「大幅な減少はない」という見立ては、全国の総数だけを見るかぎり正しい。
しかし<strong>その平均は、動かない都市部と削られる農山村を足して2で割った数字である</strong>。
人口の大半が前者に住んでいるぶん、平均は前者に引っ張られる。
偏在を見たければ、平均から降りて地域を見るしかない。</p>

<p>ただし本稿が言えるのはここまでである。<strong>4条1項については、0局の団体が確認されなかった</strong>——
原則的な数量基準とは整合するが、ただし書きの例外があるため適法性の判定ではない。
<strong>4条2項3号については要精査</strong>——数字の向きは明確だが、過疎地の定義と基準日の扱いに詰めるべき点が残る。
そして<strong>{LAW["規則"]}4条2項2号、すなわち「交通、地理その他の事情を勘案して容易に利用できる位置」かどうかは、依然として判定していない</strong>。
直線距離では「交通の事情」を測れないからである。道路ネットワーク距離と公共交通の所要時間が、次の宿題になる。</p>

<div class="note">
<b>この記事の数値について。</b>すべて公開資料からの独自集計であり、引用値ではない。
距離帯や比率の閾値は分析上の設定であって法令上の基準ではない。
本稿は法令適合性の判定を行うものではなく、公開資料に基づく予備的なスクリーニングである。
月次系列の各月への配置は届出上の予定日に基づく仮置きであり、各変更の実施日を確定したものではない。
</div>

<h2>出典と再現情報</h2>

<div class="tw"><table>
<thead><tr><th>資料</th><th>基準日／版</th><th>本稿での用途</th></tr></thead>
<tbody>
<tr><td><a href="https://www.post.japanpost.jp/newsrelease/storeinformation/">日本郵便「法第6条第2項の規定による届出」一覧・変更届</a></td>
    <td>一覧 2026-06-30／変更届 2012-10-04〜2026-07-30（177本）</td><td>局の名称・所在地・過疎地・営業状態、月次復元</td></tr>
<tr><td><a href="https://www.post.japanpost.jp/newsrelease/storeinformation/index02.html">日本郵便「郵便局局数情報〈オープンデータ〉」</a></td>
    <td>2018-10〜2026-06（93か月）</td><td>営業中／閉鎖中の月次系列、母集団の突合</td></tr>
<tr><td><a href="https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P30.html">国土数値情報 郵便局データ P30</a></td>
    <td>2013-11-30（2013年度版）</td><td>2013年断面・座標・一時閉鎖表記</td></tr>
<tr><td><a href="https://nlftp.mlit.go.jp/isj/">国土交通省 位置参照情報（街区・大字町丁目）</a></td>
    <td>街区 24.0a／大字町丁目 19.0b</td><td>2026年住所のジオコーディング</td></tr>
<tr><td><a href="https://www.e-stat.go.jp/gis/statmap-search?page=1&amp;type=1&amp;toukeiCode=00200521">総務省統計局 令和2年国勢調査（500mメッシュ・市区町村別）</a></td>
    <td>2020年</td><td>人口分母（両年とも固定）</td></tr>
<tr><td><a href="https://www.soumu.go.jp/main_sosiki/jichi_gyousei/c-gyousei/2001/kaso/kasomain0.htm">総務省 過疎地域市町村等一覧・全国地方公共団体コード</a></td>
    <td>過疎 2022-04-01現在版</td><td>過疎地区分、法的単位の確定</td></tr>
<tr><td><a href="https://laws.e-gov.go.jp/law/419M60000008037">{LAW["規則"]}（e-Gov）</a></td>
    <td>現行</td><td>4条の設置基準、附則4条の基準時</td></tr>
<tr><td><a href="https://laws.e-gov.go.jp/law/417AC0000000100">{LAW["法"]}（e-Gov）</a></td>
    <td>現行</td><td>6条1項の設置義務、6条2項の届出</td></tr>
<tr><td><a href="https://www.shugiin.go.jp/internet/itdb_annai.nsf/html/statics/housei/pdf/221hou13siryou.pdf/$File/221hou13siryou.pdf">{LAW["改正法2026民営化"]}</a></td>
    <td>衆法13号／2026-06-19成立・06-25公布</td><td>前提（交付金拡充・地域貢献基金・合併検討）</td></tr>
<tr><td><a href="https://www.sangiin.go.jp/japanese/joho1/kousei/gian/221/meisai/m221080221034.htm">{LAW["改正法2026郵便"]}</a></td>
    <td>2026-06-12成立／06-19公布</td><td>前提（料金上限の認可制化）</td></tr>
<tr><td><a href="https://www.japanpost.jp/news/pressrelease/20260515_04/">日本郵政「JPプラン2028」</a></td>
    <td>2026-05-15公表</td><td>前提（集配拠点約3,200→約2,700、約500拠点、約50億円）</td></tr>
</tbody></table></div>

<div class="note">
<b>再現情報。</b>距離の試算は封印済み成果物
<code>{RC["input_artifacts"][0]["artifact_id"][:12]}…</code>（2026年座標）、
<code>{RC["input_artifacts"][1]["artifact_id"][:12]}…</code>（2013年座標）、
<code>{RC["input_artifacts"][3]["artifact_id"][:12]}…</code>（メッシュ人口）を入力とする。
成果物全体のステータスは <code>status = {RC["status"]}</code>、
理由コードは <code>{", ".join(RC["reason_codes"])}</code>。
局数側の集計値は <code>data/work/stats.json</code> に一元化しており、本稿の数値はすべてそこから生成している。
<b>国土数値情報P30は非商用利用条件</b>であり、本稿は非商用の分析目的で出典を明示して利用した。
派生成果物の <code>public_release_allowed</code> は <code>false</code> のままである。
</div>

<footer>
<p>本稿の数値はすべて上記公開資料からの独自集計であり、引用値ではない。
距離帯や比率の閾値は分析上の設定であって法令上の基準ではない。
本稿は法令適合性の判定を行うものではなく、公開資料に基づく予備的なスクリーニングである。
月次系列の各月への配置は届出上の予定日に基づく仮置きであり、各変更の実施日を確定したものではない。</p>
</footer>
</div>
"""

DOC = f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>郵便局は「減っていない」のか</title>
<meta name="description" content="郵便局の総数は13年で1.7%しか減っていない。だが実効ベース、地域別、区画単位で見ると別の絵が出てくる。">
<style>{CSS}</style>
</head>
<body>
{BODY}
</body>
</html>
"""

OUT.write_text(DOC, encoding="utf-8")
print(f"wrote {OUT}  ({OUT.stat().st_size:,} bytes)")
