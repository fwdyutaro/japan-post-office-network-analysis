"""Sealed, offline endpoint animation for accessibility sensitivity results."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, commit_sealed_directory, verify_artifact

INPUT_ARTIFACT_TYPE = "accessibility_recalculation"
OUTPUT_ARTIFACT_TYPE = "accessibility_summary_animation"
INPUT_SCHEMA = "accessibility-recalculation-v1"
OUTPUT_SCHEMA = "accessibility-summary-animation-v1"
DISTANCE_METRIC = "GRS80_geodesic_straight_line"


class AccessibilityAnimationError(RuntimeError):
    pass


def _finite(value: Any, label: str, *, minimum: float = 0.0) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(float(value)) or float(value) < minimum):
        raise AccessibilityAnimationError(f"{label} rejected")
    return float(value)


def _integer(value: Any, label: str, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AccessibilityAnimationError(f"{label} rejected")
    return value


def _date(value: Any) -> str:
    if not isinstance(value, str):
        raise AccessibilityAnimationError("frame date rejected")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise AccessibilityAnimationError("frame date rejected") from exc
    if parsed.isoformat() != value:
        raise AccessibilityAnimationError("frame date rejected")
    return value


def _reasons(value: Any) -> list[str]:
    if (not isinstance(value, list) or any(
            not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,96}", code)
            for code in value)):
        raise AccessibilityAnimationError("reason codes rejected")
    return sorted(set(value))


def _series(row: Any, label: str) -> dict[str, Any]:
    if not isinstance(row, dict) or row.get("status") != "computed":
        raise AccessibilityAnimationError(f"{label} series rejected")
    coverage = row.get("cov")
    if not isinstance(coverage, dict):
        raise AccessibilityAnimationError(f"{label} coverage rejected")
    cov1000 = _finite(coverage.get("1000", coverage.get(1000)), f"{label} coverage")
    if cov1000 > 100:
        raise AccessibilityAnimationError(f"{label} coverage rejected")
    return {
        "n_offices": _integer(row.get("n_offices"), f"{label} office count"),
        "population": _integer(row.get("population"), f"{label} population", minimum=1),
        "mean_m": _finite(row.get("mean_m"), f"{label} mean"),
        "cov1km_pct": cov1000,
        "over2km": _integer(row.get("over2km"), f"{label} over2km"),
        "over2km_65": _integer(row.get("over2km_65"), f"{label} over2km65"),
    }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _digest(model: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical({k: v for k, v in model.items()
                                      if k != "model_digest"})).hexdigest()


def _load_recalculation(source: str | Path) -> tuple[dict[str, Any], str, bool]:
    root = Path(source)
    try:
        verified = verify_artifact(root, strict_code=False)
        strict = verify_artifact(root, strict_code=True).get("ok", False)
        artifact = json.loads((root / "artifact.json").read_text(encoding="utf-8"))
        data = json.loads((root / "accessibility_recalculated.json").read_text(
            encoding="utf-8"))
    except (ArtifactError, OSError, ValueError, json.JSONDecodeError) as exc:
        raise AccessibilityAnimationError("recalculation artifact rejected") from exc
    if (not verified.get("ok") or artifact.get("artifact_type") != INPUT_ARTIFACT_TYPE or
            data.get("schema_version") != INPUT_SCHEMA or data.get("status") != "not_evaluable"):
        raise AccessibilityAnimationError("recalculation contract rejected")
    artifact_id = artifact.get("artifact_id")
    if not isinstance(artifact_id, str) or not re.fullmatch(r"[0-9a-f]{64}", artifact_id):
        raise AccessibilityAnimationError("recalculation artifact ID rejected")
    return data, artifact_id, strict


def build_accessibility_animation(source: str | Path) -> dict[str, Any]:
    data, artifact_id, strict = _load_recalculation(source)
    if data.get("distance_metric") != DISTANCE_METRIC:
        raise AccessibilityAnimationError("distance metric rejected")
    reasons = _reasons(data.get("reason_codes"))
    if (data.get("road_network_distance_status") != "not_evaluable" or
            "road_network_distance_missing" not in reasons or
            (not strict and "upstream_code_version_mismatch" not in reasons)):
        raise AccessibilityAnimationError("input status gates rejected")
    results = data.get("results")
    if not isinstance(results, dict):
        raise AccessibilityAnimationError("recalculation results rejected")
    baseline = _series(results.get("p30_2013"), "p30_2013")
    conservative = _series(results.get("inherit_all"), "inherit_all")
    sensitivity = _series(results.get("isj"), "isj")
    if len({baseline["population"], conservative["population"], sensitivity["population"]}) != 1:
        raise AccessibilityAnimationError("series population mismatch")
    corrected = results.get("corrected")
    if not isinstance(corrected, dict) or corrected.get("status") != "not_evaluable":
        raise AccessibilityAnimationError("corrected status rejected")
    corrected_reasons = _reasons(corrected.get("reason_codes"))
    if "confirmed_events_qa_error" not in corrected_reasons:
        raise AccessibilityAnimationError("corrected reason rejected")
    frames = [
        {"frame_id": "p30_2013", "date": _date(data.get("p30_date")),
         "label": "2013\u5e74 P30\u57fa\u6e96", "series": "baseline", **baseline},
        {"frame_id": "inherit_all", "date": _date(data.get("anchor")),
         "label": "2026\u5e74 \u4fdd\u5b88\u76841km\u30b2\u30fc\u30c8",
         "series": "conservative", **conservative},
        {"frame_id": "isj", "date": _date(data.get("anchor")),
         "label": "2026\u5e74 ISJ\u306e\u307f\uff08\u611f\u5ea6\uff09",
         "series": "sensitivity", **sensitivity},
    ]
    model = {
        "schema_version": OUTPUT_SCHEMA,
        "animation_kind": "accessibility_endpoint_summary",
        "endpoint_only": True,
        "intermediate_months_observed": False,
        "distance_metric": DISTANCE_METRIC,
        "status": "not_evaluable",
        "reason_codes": reasons,
        "corrected_status": "not_evaluable",
        "corrected_reason_codes": corrected_reasons,
        "road_network_distance_status": "not_evaluable",
        "input_strict_code_current": strict,
        "source_artifact_id": artifact_id,
        "frames": frames,
    }
    model["model_digest"] = _digest(model)
    _validate_model(model)
    return model


def _validate_model(model: Any) -> None:
    if (not isinstance(model, dict) or model.get("schema_version") != OUTPUT_SCHEMA or
            model.get("animation_kind") != "accessibility_endpoint_summary" or
            model.get("endpoint_only") is not True or
            model.get("intermediate_months_observed") is not False or
            model.get("distance_metric") != DISTANCE_METRIC or
            model.get("status") != "not_evaluable" or
            model.get("corrected_status") != "not_evaluable" or
            model.get("road_network_distance_status") != "not_evaluable" or
            type(model.get("input_strict_code_current")) is not bool):
        raise AccessibilityAnimationError("animation model rejected")
    reasons = _reasons(model.get("reason_codes"))
    corrected_reasons = _reasons(model.get("corrected_reason_codes"))
    if ("road_network_distance_missing" not in reasons or
            "confirmed_events_qa_error" not in corrected_reasons or
            (not model["input_strict_code_current"] and
             "upstream_code_version_mismatch" not in reasons)):
        raise AccessibilityAnimationError("animation status gates rejected")
    source_id = model.get("source_artifact_id")
    if not isinstance(source_id, str) or not re.fullmatch(r"[0-9a-f]{64}", source_id):
        raise AccessibilityAnimationError("source artifact ID rejected")
    frames = model.get("frames")
    expected = (("p30_2013", "baseline"), ("inherit_all", "conservative"),
                ("isj", "sensitivity"))
    if not isinstance(frames, list) or len(frames) != 3:
        raise AccessibilityAnimationError("animation frames rejected")
    populations: set[int] = set()
    for frame, (frame_id, series) in zip(frames, expected):
        if (not isinstance(frame, dict) or frame.get("frame_id") != frame_id or
                frame.get("series") != series or not isinstance(frame.get("label"), str)):
            raise AccessibilityAnimationError("animation frame rejected")
        _date(frame.get("date"))
        checked = _series({"status": "computed", "n_offices": frame.get("n_offices"),
                           "population": frame.get("population"), "mean_m": frame.get("mean_m"),
                           "cov": {"1000": frame.get("cov1km_pct")},
                           "over2km": frame.get("over2km"),
                           "over2km_65": frame.get("over2km_65")}, frame_id)
        populations.add(checked["population"])
    if len(populations) != 1 or model.get("model_digest") != _digest(model):
        raise AccessibilityAnimationError("animation model integrity rejected")


def _embedded_json(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (text.replace("&", "\\u0026").replace("<", "\\u003c")
            .replace(">", "\\u003e").replace("\u2028", "\\u2028")
            .replace("\u2029", "\\u2029"))


def render_accessibility_animation(model: dict[str, Any]) -> str:
    _validate_model(model)
    payload = _embedded_json(model)
    return f"""<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>&#37109;&#20415;&#23616;&#12450;&#12463;&#12475;&#12471;&#12499;&#12522;&#12486;&#12451;&#31471;&#28857;&#27604;&#36611;</title><style>
:root{{--bg:#f7f6f2;--fg:#17202a;--muted:#5d6670;--line:#d4d7da;--accent:#176b87;--warn:#7a5420}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font-family:system-ui,"Yu Gothic",sans-serif}}
main{{max-width:940px;margin:auto;padding:28px 20px}}h1{{font-size:clamp(1.35rem,3vw,2rem);font-weight:500;margin:0 0 4px}}
.sub,.note{{color:var(--muted)}}.controls{{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin:24px 0}}
button,input{{font:inherit}}button{{padding:7px 16px;border:1px solid var(--line);background:transparent;color:var(--fg);border-radius:4px}}
input[type=range]{{flex:1;min-width:220px}}.date{{font-variant-numeric:tabular-nums;color:var(--accent)}}
.metrics{{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px;margin:22px 0}}.metric{{border-top:2px solid var(--line);padding-top:9px}}
.value{{font-size:clamp(1.45rem,4vw,2.4rem);font-variant-numeric:tabular-nums}}.track{{height:8px;background:var(--line);margin-top:7px;overflow:hidden}}
.bar{{height:100%;background:var(--accent);width:0;transition:width .55s ease}}.status{{border-left:4px solid var(--warn);padding:8px 12px;margin-top:22px;color:var(--warn)}}
@media(max-width:580px){{.metrics{{grid-template-columns:1fr}}}}@media(prefers-reduced-motion:reduce){{.bar{{transition:none}}}}
</style></head><body><main>
<h1>&#37109;&#20415;&#23616;&#12450;&#12463;&#12475;&#12471;&#12499;&#12522;&#12486;&#12451;&#31471;&#28857;&#27604;&#36611;</h1>
<div class="sub">2013&#24180;&#12392;2026&#24180;&#12398;&#35251;&#28204;&#31471;&#28857;&#12398;&#12415;&#12290;&#20013;&#38291;&#26376;&#12399;&#25512;&#23450;&#12539;&#35036;&#38291;&#12375;&#12390;&#12356;&#12414;&#12379;&#12435;&#12290;</div>
<div class="controls"><button id="play" type="button" aria-label="&#20877;&#29983;">&#20877;&#29983;</button><input id="step" type="range" min="0" max="2" step="1" value="0" aria-label="&#27604;&#36611;&#12377;&#12427;&#31471;&#28857;"><strong id="label"></strong><span id="date" class="date"></span></div>
<section class="metrics" aria-live="polite">
<div class="metric"><div>&#20154;&#21475;&#21152;&#37325;&#24179;&#22343;&#36317;&#38626;</div><div id="mean" class="value"></div><div class="track"><div id="meanbar" class="bar"></div></div></div>
<div class="metric"><div>1km&#22287;&#20154;&#21475;&#27604;</div><div id="coverage" class="value"></div><div class="track"><div id="covbar" class="bar"></div></div></div>
<div class="metric"><div>2km&#36229;&#20154;&#21475;</div><div id="over" class="value"></div><div class="track"><div id="overbar" class="bar"></div></div></div>
<div class="metric"><div>65&#27507;&#20197;&#19978;&#12539;2km&#36229;&#20154;&#21475;</div><div id="over65" class="value"></div><div class="track"><div id="over65bar" class="bar"></div></div></div>
</section>
<div class="status">&#31227;&#36578;&#35036;&#27491;&#28168;&#12415;&#31995;&#21015;&#65306;&#26410;&#35413;&#20385;&#12290;&#36947;&#36335;&#12493;&#12483;&#12488;&#12527;&#12540;&#12463;&#36317;&#38626;&#65306;&#26410;&#35413;&#20385;&#12290;GRS80&#28204;&#22320;&#32218;&#12398;&#30452;&#32218;&#36317;&#38626;&#12395;&#12424;&#12427;&#20869;&#37096;&#21521;&#12369;&#24863;&#24230;&#34920;&#31034;&#12391;&#12377;&#12290;</div>
<p class="note">2026&#24180;&#12398;&#20445;&#23432;&#30340;1km&#12466;&#12540;&#12488;&#12364;&#20027;&#24863;&#24230;&#31995;&#21015;&#12289;ISJ&#12398;&#12415;&#12399;&#24231;&#27161;&#32153;&#25215;&#12434;&#34892;&#12431;&#12394;&#12356;&#24863;&#24230;&#31995;&#21015;&#12391;&#12377;&#12290;</p>
</main><script>const DATA={payload};
const $=id=>document.getElementById(id),frames=DATA.frames,maxima={{mean_m:Math.max(...DATA.frames.map(x=>x.mean_m)),over2km:Math.max(...DATA.frames.map(x=>x.over2km)),over2km_65:Math.max(...DATA.frames.map(x=>x.over2km_65))}};
let timer=null;const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;const pct=(v,m)=>Math.max(0,Math.min(100,v/m*100))+'%';
function draw(i){{const f=frames[i];$('label').textContent=f.label;$('date').textContent=f.date;$('mean').textContent=f.mean_m.toFixed(1)+' m';$('coverage').textContent=f.cov1km_pct.toFixed(2)+'%';$('over').textContent=f.over2km.toLocaleString('ja-JP')+' '+String.fromCodePoint(20154);$('over65').textContent=f.over2km_65.toLocaleString('ja-JP')+' '+String.fromCodePoint(20154);$('meanbar').style.width=pct(f.mean_m,maxima.mean_m);$('covbar').style.width=f.cov1km_pct+'%';$('overbar').style.width=pct(f.over2km,maxima.over2km);$('over65bar').style.width=pct(f.over2km_65,maxima.over2km_65);$('step').value=String(i)}}
function stop(){{if(timer)clearInterval(timer);timer=null;$('play').textContent=String.fromCodePoint(20877,29983);$('play').setAttribute('aria-label',String.fromCodePoint(20877,29983))}}
$('step').addEventListener('input',e=>{{stop();draw(Number(e.target.value))}});$('play').addEventListener('click',()=>{{if(timer){{stop();return}};let i=Number($('step').value);if(i>=frames.length-1){{stop();return}};if(reduced){{draw(Math.min(i+1,frames.length-1));return}};$('play').textContent=String.fromCodePoint(20572,27490);$('play').setAttribute('aria-label',String.fromCodePoint(20572,27490));timer=setInterval(()=>{{const next=Number($('step').value)+1;if(next>=frames.length){{stop();return}};draw(next)}},1800)}});draw(0);
</script></body></html>"""


def write_accessibility_animation(model: dict[str, Any], source: str | Path,
                                  output_dir: str | Path, *,
                                  acknowledge_internal_use: bool = False,
                                  replace: bool = False) -> dict[str, Any]:
    if not acknowledge_internal_use:
        raise AccessibilityAnimationError("internal-use acknowledgement required")
    expected = build_accessibility_animation(source)
    _validate_model(model)
    if _canonical(model) != _canonical(expected):
        raise AccessibilityAnimationError("animation model mutation rejected")
    source_id = model["source_artifact_id"]
    reasons = model["reason_codes"]
    payloads = {
        "accessibility_animation.html": render_accessibility_animation(model).encode("utf-8"),
        "animation_data.json": _canonical(model) + b"\n",
        "metadata.json": _canonical({
            "schema_version": OUTPUT_SCHEMA, "status": model["status"],
            "reason_codes": reasons, "animation_kind": model["animation_kind"],
            "endpoint_only": True, "intermediate_months_observed": False,
            "source_artifact_id": source_id, "public_release_allowed": False}) + b"\n",
        "coverage_summary.jsonl": _canonical(
            {"status": model["status"], "reason_codes": reasons}) + b"\n",
        "qa.jsonl": b"".join(_canonical({"kind": "warning", "code": code}) + b"\n"
                                for code in reasons),
    }
    try:
        return commit_sealed_directory(
            output_dir, payloads, artifact_type=OUTPUT_ARTIFACT_TYPE,
            source_authority="auxiliary", evidence_status="effective",
            release_classification="internal_only", input_artifact_ids=[source_id],
            qa_files=["qa.jsonl"], preserve_existing=False,
            acknowledge_internal_use=True, strict_code=True)
    except (ArtifactError, OSError, ValueError) as exc:
        raise AccessibilityAnimationError("animation output rejected") from exc
