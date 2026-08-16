"""月次アニメーションのHTMLプレイヤーを生成する（仕様書§14.2）。"""
from __future__ import annotations
import json
from pathlib import Path

from project_paths import work_dir

W = work_dir()
DATA = json.loads((W / "animation_data.json").read_text(encoding="utf-8"))
OUT = W / "postal_animation.html"

# 発散配色。全期間固定。減少側を赤、増加側を青に。
# 末尾2階級は「基準月に局なし」と「データ欠損」。
LIGHT = ["#7E2226", "#B45B58", "#D39A94", "#EBD9D6", "#E4E7EB", "#9CB9D2", "#4E7FA8", "#D8DDE3", "#F0C674"]
DARK = ["#EE9296", "#BC6265", "#8A4C4E", "#4A3B3E", "#333A44", "#3F5A75", "#5C93C4", "#2B323C", "#8A6A2A"]

P = DATA["params"]
nat = DATA["national"]
def safe_json(obj) -> str:
    """インラインスクリプトに埋め込める JSON。`</script>` 等での脱出を防ぐ。

    `\\uXXXX` は JSON 文字列として妥当なので値は変わらない。
    """
    s = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    for ch, esc in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"),
                    (" ", "\\u2028"), (" ", "\\u2029")):
        s = s.replace(ch, esc)
    return s


CS = DATA["coord_sources"]
TOTAL_VERSIONS = sum(CS.values())
UNRESOLVED = CS.get("unresolved", 0)
POSMOVES = sum(DATA["position_changes"])
FIRST_NOCOORD = nat[0]["nocoord"]
LAST_NOCOORD = nat[-1]["nocoord"]

CSS = """
:root{
  --paper:#F7F8FA; --raise:#FFFFFF; --ink:#171A20; --body:#333B47; --muted:#64707F;
  --rule:#DDE2E8; --seal:#A62B31; --statute:#2C4A6E; --canvasbg:#FFFFFF;
}
@media (prefers-color-scheme:dark){:root{
  --paper:#11141A; --raise:#171B22; --ink:#E8EBF0; --body:#C2C9D3; --muted:#93A0B0;
  --rule:#262D38; --seal:#E2757A; --statute:#86AEDA; --canvasbg:#0D1015;}}
:root[data-theme="dark"]{
  --paper:#11141A; --raise:#171B22; --ink:#E8EBF0; --body:#C2C9D3; --muted:#93A0B0;
  --rule:#262D38; --seal:#E2757A; --statute:#86AEDA; --canvasbg:#0D1015;}
:root[data-theme="light"]{
  --paper:#F7F8FA; --raise:#FFFFFF; --ink:#171A20; --body:#333B47; --muted:#64707F;
  --rule:#DDE2E8; --seal:#A62B31; --statute:#2C4A6E; --canvasbg:#FFFFFF;}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--body);
  font-family:"Hiragino Kaku Gothic ProN","Yu Gothic",YuGothic,"Noto Sans JP","Segoe UI",sans-serif;
  font-size:15px;line-height:1.8}
.wrap{max-width:920px;margin:0 auto;padding:40px 22px 70px}
h1{font-family:"Hiragino Mincho ProN","Yu Mincho",YuMincho,"Noto Serif JP",serif;
  color:var(--ink);font-size:26px;line-height:1.45;margin:0 0 10px;font-weight:600;text-wrap:balance}
.eyebrow{font-size:11.5px;letter-spacing:.15em;text-transform:uppercase;color:var(--muted);
  margin:0 0 14px;font-weight:600}
p{max-width:66ch;margin:0 0 14px}
.stage{margin:26px 0 0;background:var(--raise);border:1px solid var(--rule);border-radius:4px;
  padding:16px;display:grid;grid-template-columns:1fr 210px;gap:18px}
@media(max-width:760px){.stage{grid-template-columns:1fr}}
canvas{width:100%;height:auto;display:block;background:var(--canvasbg);border-radius:2px}
.side{display:flex;flex-direction:column;gap:16px}
.month{font-family:"Hiragino Mincho ProN","Yu Mincho",YuMincho,serif;font-size:30px;
  color:var(--ink);font-variant-numeric:tabular-nums;line-height:1.1}
.sub{font-size:12px;color:var(--muted)}
.kv{font-size:13px;display:flex;justify-content:space-between;gap:10px;
  font-variant-numeric:tabular-nums;border-bottom:1px solid var(--rule);padding:5px 0}
.kv b{color:var(--ink);font-weight:600;font-family:ui-monospace,Consolas,monospace}
.legend{display:flex;flex-direction:column;gap:4px;font-size:11.5px;color:var(--muted)}
.legend div{display:flex;align-items:center;gap:7px}
.legend i{width:15px;height:11px;display:inline-block;border-radius:1px;flex:none}
.controls{display:flex;flex-wrap:wrap;align-items:center;gap:9px;margin-top:14px}
button{font:inherit;font-size:13px;padding:6px 13px;border:1px solid var(--rule);
  background:var(--raise);color:var(--body);border-radius:3px;cursor:pointer}
button:hover{border-color:var(--statute);color:var(--ink)}
button:focus-visible,input:focus-visible,select:focus-visible{outline:2px solid var(--statute);outline-offset:2px}
#play{background:var(--statute);color:#fff;border-color:var(--statute);min-width:74px;font-weight:600}
input[type=range]{flex:1;min-width:180px;accent-color:var(--seal)}
select{font:inherit;font-size:13px;padding:5px 8px;background:var(--raise);color:var(--body);
  border:1px solid var(--rule);border-radius:3px}
.warn{margin:18px 0 0;padding:14px 17px;background:var(--raise);border:1px solid var(--rule);
  border-left:3px solid var(--seal);border-radius:3px;font-size:13.5px;max-width:70ch}
.warn b{display:block;color:var(--seal);font-size:11.5px;letter-spacing:.12em;
  text-transform:uppercase;margin-bottom:6px}
footer{margin-top:34px;padding-top:18px;border-top:1px solid var(--rule);font-size:12.5px;color:var(--muted)}
footer p{max-width:none}
code{font-family:ui-monospace,Consolas,monospace;font-size:.9em}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

BINS = P["bins"]
legend_html = "".join(
    f'<div><i style="background:{LIGHT[i]}" data-l="{LIGHT[i]}" data-d="{DARK[i]}"></i>{b}</div>'
    for i, b in enumerate(BINS))

HTML = f"""<title>郵便局ネットワークの月次推移 2012–2026</title>
<style>{CSS}</style>
<div class="wrap">
<p class="eyebrow">日本郵便株式会社法6条2項 届出データによる復元 · 形式系列</p>
<h1>郵便局ネットワークの月次推移<br>{DATA['months'][0].replace('-','年')}月 — {DATA['months'][-1].replace('-','年')}月</h1>
<p>各セルは10km四方の固定格子。色は<strong>そのセル中心から半径20km以内にある届出上の郵便局数が、
{DATA['baseline_month'].replace('-','年')}月と比べて何パーセント変化したか</strong>を示す。
格子・投影法・検索半径・色階級・地図範囲は全期間で固定してあり、期間ごとの再スケーリングは行っていない。</p>

<div class="warn">
<b>各月の位置づけについて</b>
届出は「変更予定」を含む。本図は、同一施設の事象を連鎖させて<strong>状態が現況一覧（{DATA['anchor_month']}末）と整合することを確認したうえで</strong>採用しているが、
<strong>各変更が実際にどの月に実施されたかは確認できていない</strong>。終端一致が立証するのは「遅くともアンカー時点までにその状態になった」ことだけである。
月への配置は<strong>届出上の予定日に基づく仮置き（date_basis = {DATA['params']['effective_date_basis']}）</strong>であり、確定値ではない。
アンカーより後の月は表示しない。</div>

<div class="stage">
  <div>
    <canvas id="cv" width="{DATA['w']}" height="{DATA['h']}"
      role="img" aria-label="郵便局密度の月次変化を示すアニメーション地図"></canvas>
    <div class="controls">
      <button id="play" aria-label="再生／一時停止">再生</button>
      <button data-step="-12">◀ 1年</button>
      <button data-step="-1">◀ 1月</button>
      <button data-step="1">1月 ▶</button>
      <button data-step="12">1年 ▶</button>
      <input type="range" id="sl" min="0" max="{len(DATA['months'])-1}" value="0" aria-label="表示する年月">
      <select id="sp" aria-label="再生速度">
        <option value="3">0.5×</option><option value="6" selected>1×</option>
        <option value="12">2×</option><option value="24">4×</option>
      </select>
    </div>
  </div>
  <div class="side">
    <div>
      <div class="month" id="mo">2012-10</div>
      <div class="sub">基準月 {DATA['baseline_month']}</div>
    </div>
    <div>
      <div class="kv"><span>届出上の局数</span><b id="tot">—</b></div>
      <div class="kv"><span>基準比</span><b id="chg">—</b></div>
      <div class="kv"><span>座標付与</span><b id="plc">—</b></div>
      <div class="kv"><span>前月から位置変化</span><b id="mv">—</b></div>
    </div>
    <div class="legend" id="lg">{legend_html}</div>
  </div>
</div>

<footer>
<p><strong>作り方.</strong> 変更届177本から抽出した6,053件の事象のうち、連鎖整合性と現況一覧での終端一致により
<strong>状態の裏づけが取れた</strong>5,831件を適用して各月末の状態を復元した。
実施日は届出上の予定日に置いており（観測により実施日を確定できた事象は0件）、月への配置は仮置きである。</p>
<p><strong>移転の扱い.</strong> 座標は整理番号ごとに固定せず、履歴の<strong>住所バージョン（valid_from / valid_to）ごとに</strong>持たせている。
全{sum(1 for _ in [0]) and ''}{TOTAL_VERSIONS:,}版のうち、2026年時点の住所は位置参照情報の街区レベル、
それ以外の過去住所は同じ位置参照情報の町丁目レベル、なお当たらないものは国土数値情報P30（2013年）の実測点で解決した。
未解決は{UNRESOLVED}版。この結果、全期間で延べ{POSMOVES:,}回の位置変化が描画に反映されている。
座標を付与できなかった局は初月{FIRST_NOCOORD}／最終月{LAST_NOCOORD}。</p>
<p><strong>読み方の注意.</strong> これは<strong>形式系列</strong>であり、届出上存在する局を数えている。
一時閉鎖は届出事項ではないため月次では追えず、実際に営業している局の数はこれより少ない。
また一覧の脚注にのみ記載される長期休止の簡易郵便局（2026年6月時点653局）は個体が特定できないため含まない。
20km・10kmという数値は法令上の基準ではなく分析上の設定である。
欠損月があれば専用の階級で明示する（現在の期間に欠損は{len(DATA['missing_months'])}件）。</p>
<p>出典: 日本郵便株式会社「日本郵便株式会社法第6条第2項の規定による届出」／
国土数値情報 郵便局データP30（非商用条件）／国土交通省 位置参照情報／
総務省統計局 令和2年国勢調査500mメッシュ（格子の陸域・人口マスクに使用）。加工のうえ利用。</p>
</footer>
</div>
<script>
const D={safe_json({k: DATA[k] for k in ('months','frames','cells','cell_px','w','h','national','position_changes','missing_months')})};
const LIGHT={json.dumps(LIGHT)}, DARK={json.dumps(DARK)};
const cv=document.getElementById('cv'), cx=cv.getContext('2d');
const sl=document.getElementById('sl'), mo=document.getElementById('mo');
const tot=document.getElementById('tot'), chg=document.getElementById('chg'), plc=document.getElementById('plc');
const play=document.getElementById('play'), sp=document.getElementById('sp');
const S=D.cell_px, base=D.national[0].total;
if(D.missing_months.length) console.warn('missing months:', D.missing_months);
let i=0, timer=null, playing=false;

function pal(){{
  const r=document.documentElement.getAttribute('data-theme');
  const dark = r ? r==='dark' : matchMedia('(prefers-color-scheme: dark)').matches;
  return dark?DARK:LIGHT;
}}
function paintLegend(){{
  const p=pal();
  document.querySelectorAll('#lg i').forEach((el,k)=>{{el.style.background=p[k];}});
}}
function draw(){{
  const p=pal(), f=D.frames[i];
  cx.clearRect(0,0,cv.width,cv.height);
  for(let k=0;k<D.cells.length;k++){{
    cx.fillStyle=p[+f[k]];
    cx.fillRect(D.cells[k][0], D.cells[k][1]-S, S, S);
  }}
  const n=D.national[i];
  mo.textContent=D.months[i];
  if(n.missing){{
    tot.textContent='—'; chg.textContent='データ欠損'; plc.textContent='—';
    document.getElementById('mv').textContent='—';
  }} else {{
    tot.textContent=n.total.toLocaleString();
    const d=(n.total-base)/base*100;
    chg.textContent=(d>=0?'+':'')+d.toFixed(2)+'%';
    plc.textContent=n.placed.toLocaleString()+' / '+n.total.toLocaleString();
    document.getElementById('mv').textContent=(D.position_changes[i]||0).toLocaleString();
  }}
  if(+sl.value!==i) sl.value=i;
}}
function step(n){{ i=Math.max(0,Math.min(D.months.length-1,i+n)); draw(); }}
function tick(){{
  if(i>=D.months.length-1){{ stop(); return; }}
  const prevYear=D.months[i].slice(0,4);
  i++; draw();
  let d=1000/(+sp.value);
  if(D.months[i].slice(0,4)!==prevYear) d+=300;              // 年替わりで停止
  if(i===D.months.length-1){{ stop(); setTimeout(()=>{{}},2000); return; }}
  timer=setTimeout(tick,d);
}}
function start(){{ if(i>=D.months.length-1) i=0; playing=true; play.textContent='一時停止'; tick(); }}
function stop(){{ playing=false; play.textContent='再生'; clearTimeout(timer); }}
play.onclick=()=>playing?stop():start();
sl.oninput=()=>{{ stop(); i=+sl.value; draw(); }};
sp.onchange=()=>{{ if(playing){{ clearTimeout(timer); tick(); }} }};
document.querySelectorAll('[data-step]').forEach(b=>b.onclick=()=>{{stop();step(+b.dataset.step);}});
addEventListener('keydown',e=>{{
  if(e.key==='ArrowRight'){{stop();step(1);}} else if(e.key==='ArrowLeft'){{stop();step(-1);}}
  else if(e.key===' '){{e.preventDefault();playing?stop():start();}}
}});
new MutationObserver(()=>{{paintLegend();draw();}}).observe(
  document.documentElement,{{attributes:true,attributeFilter:['data-theme']}});
matchMedia('(prefers-color-scheme: dark)').addEventListener('change',()=>{{paintLegend();draw();}});
paintLegend(); draw();
if(!matchMedia('(prefers-reduced-motion: reduce)').matches) start();
</script>
"""

OUT.write_text(HTML, encoding="utf-8")
print(f"wrote {OUT}  {OUT.stat().st_size/1e6:.2f} MB")
