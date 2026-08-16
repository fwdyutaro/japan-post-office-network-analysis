"""Seal the human-facing research report, map and animation as one release unit.

The exploratory producers under ``data/work`` intentionally remain ordinary
scripts.  This module is the promotion boundary: it validates their outputs,
records the exact source bytes and verified upstream artifacts, and publishes
all three presentation components atomically in one internal-only bundle.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, commit_sealed_directory, verify_artifact


ARTIFACT_TYPE = "postal_bias_research_outputs"
MAX_SOURCE_BYTES = 100 * 1024 * 1024
MONTH_RE = re.compile(r"^[0-9]{4}-(0[1-9]|1[0-2])$")
MAP_SVG_RE = re.compile(
    r'(<svg\b[^>]*aria-label="市区町村別の実効局数増減率のコロプレス図"[^>]*>.*?</svg>)',
    re.DOTALL,
)
ANIMATION_DATA_RE = re.compile(r"const D=(\{.*?\});\s*const LIGHT=", re.DOTALL)
ACTIVE_CONTENT_RE = re.compile(
    r"<\s*(?:iframe|object|embed|base)\b|\son[a-z0-9_-]+\s*=|javascript\s*:|"
    r"<script\b[^>]*\bsrc\s*=",
    re.IGNORECASE,
)
MAP_BINS = ((0.001, None, "var(--inc)", "増加"),
            (-5.0, 0.001, "var(--d1)", "0〜−5%"),
            (-10.0, -5.0, "var(--d2)", "−5〜−10%"),
            (-20.0, -10.0, "var(--d3)", "−10〜−20%"),
            (None, -20.0, "var(--d4)", "−20%超の減少"))


class ResearchOutputError(ValueError):
    pass


def _css_has_active_reference(value: str) -> bool:
    value = re.sub(r"\\(?:\r\n|[\n\r\f])", "", value)

    def decode_escape(match: re.Match[str]) -> str:
        if match.group(1):
            codepoint = int(match.group(1), 16)
            return chr(codepoint) if 0 < codepoint <= 0x10FFFF else "\ufffd"
        return match.group(2) or ""

    value = re.sub(r"\\([0-9a-fA-F]{1,6})(?:[ \t\r\n\f])?|\\(.)",
                   decode_escape, value, flags=re.DOTALL)
    value = re.sub(r"/\*.*?\*/", "", value, flags=re.DOTALL)
    compact = re.sub(r"\s+", "", value).casefold()
    return any(token in compact for token in ("url(", "@import", "expression("))


class _HtmlSafetyParser(HTMLParser):
    def __init__(self, *, allow_inline_script: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.allow_inline_script = allow_inline_script
        self.script_count = 0
        self.style_depth = 0
        self.style_parts: list[str] = []

    def handle_startendtag(self, tag: str,
                           attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)

    def handle_starttag(self, tag: str,
                        attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in {"iframe", "object", "embed", "base"}:
            raise ResearchOutputError("research HTML active element rejected")
        if tag == "script":
            if not self.allow_inline_script or attrs:
                raise ResearchOutputError("research HTML script rejected")
            self.script_count += 1
        if tag == "style":
            self.style_depth += 1
        values = {name.casefold(): value for name, value in attrs}
        if tag == "meta" and str(values.get("http-equiv", "")).casefold() == "refresh":
            raise ResearchOutputError("research HTML refresh rejected")
        for name, value in attrs:
            name = name.casefold()
            decoded = html.unescape(value or "").strip().casefold()
            normalized_url = re.sub(r"[\x00-\x20\x7f]+", "", decoded)
            if name.startswith("on"):
                raise ResearchOutputError("research HTML event handler rejected")
            if name in {"href", "src", "xlink:href", "action", "formaction"} and decoded.startswith(
                    ("javascript:", "vbscript:", "data:")):
                raise ResearchOutputError("research HTML URL rejected")
            if name in {"href", "src", "xlink:href", "action", "formaction"} and normalized_url.startswith(
                    ("javascript:", "vbscript:", "data:")):
                raise ResearchOutputError("research HTML URL rejected")
            if name == "style" and _css_has_active_reference(decoded):
                raise ResearchOutputError("research HTML style rejected")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "style" and self.style_depth:
            self.style_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.style_depth:
            self.style_parts.append(data)


def _validate_html_safety(doc: str, *, allow_inline_script: bool) -> None:
    decoded = html.unescape(doc)
    if ACTIVE_CONTENT_RE.search(decoded):
        raise ResearchOutputError("research HTML active content rejected")
    parser = _HtmlSafetyParser(allow_inline_script=allow_inline_script)
    try:
        parser.feed(doc)
        parser.close()
    except ResearchOutputError:
        raise
    except (ValueError, TypeError) as exc:
        raise ResearchOutputError("research HTML rejected") from exc
    expected = 1 if allow_inline_script else 0
    if parser.script_count != expected:
        raise ResearchOutputError("research HTML script count rejected")
    if _css_has_active_reference("".join(parser.style_parts)):
        raise ResearchOutputError("research HTML stylesheet rejected")


SOURCE_MEMBERS = {
    "report/postal_bias_report.html": "postal_bias_report.html",
    "report/FINDINGS.md": "FINDINGS.md",
    "report/ACCESSIBILITY_FINDINGS.md": "ACCESSIBILITY_FINDINGS.md",
    "report/stats.json": "stats.json",
    "map/choropleth.json": "choropleth.json",
    "animation/animation_data.json": "animation_data.json",
    "animation/postal_animation.html": "postal_animation.html",
    "supporting/accessibility_corrected.json": "accessibility_corrected.json",
    "supporting/lorenz.json": "lorenz.json",
    "supporting/monthly_formal.json": "monthly_formal.json",
    "supporting/reconcile.json": "reconcile.json",
    "reproducibility/build_report.py": "build_report.py",
    "reproducibility/compute_stats.py": "compute_stats.py",
    "reproducibility/build_map.py": "build_map.py",
    "reproducibility/build_animation.py": "build_animation.py",
    "reproducibility/build_animation_html.py": "build_animation_html.py",
    "reproducibility/accessibility_corrected.py": "accessibility_corrected.py",
}


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def _read_source(root: Path, name: str) -> bytes:
    path = root / name
    try:
        root_resolved = root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        resolved.relative_to(root_resolved)
        stat = path.lstat()
        if path.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400:
            raise ResearchOutputError("research source path rejected")
        if not path.is_file() or stat.st_size > MAX_SOURCE_BYTES:
            raise ResearchOutputError("research source file rejected")
        return path.read_bytes()
    except ResearchOutputError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResearchOutputError("research source file rejected") from exc


def _validate_work_root(root: Path) -> None:
    try:
        stat = root.lstat()
        if (root.is_symlink() or getattr(stat, "st_file_attributes", 0) & 0x400 or
                not root.is_dir() or root.resolve(strict=True) != root.absolute()):
            raise ResearchOutputError("research source root rejected")
    except ResearchOutputError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise ResearchOutputError("research source root rejected") from exc


def _json_object(data: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchOutputError(f"{label} JSON rejected") from exc
    if not isinstance(value, dict):
        raise ResearchOutputError(f"{label} JSON rejected")
    return value


def _map_color(change: Any) -> str:
    if change is None:
        return "var(--nodata)"
    if isinstance(change, bool) or not isinstance(change, (int, float)):
        raise ResearchOutputError("choropleth value rejected")
    for lower, upper, color, _ in MAP_BINS:
        if (lower is None or change > lower) and (upper is None or change <= upper):
            return color
    return "var(--nodata)"


def _expected_map_svg(doc: dict[str, Any]) -> str:
    main = doc["main"]; oki = doc["oki"]; units = doc["units"]
    parts = [f'<svg viewBox="0 0 {main["w"]} {main["h"]}" role="img" '
             f'aria-label="市区町村別の実効局数増減率のコロプレス図">',
             '<g class="mapg">']
    for unit, path_data in main["paths"].items():
        row = units.get(unit)
        color = _map_color(row.get("chg") if isinstance(row, dict) else None)
        parts.append(f'<path d="{path_data}" fill="{color}"/>')
    parts.append("</g>")
    ox, oy = 18, main["h"] - oki["h"] - 18
    parts.append(f'<g transform="translate({ox},{oy})">')
    parts.append(f'<rect x="-6" y="-6" width="{oki["w"]+12}" '
                 f'height="{oki["h"]+12}" class="inset"/>')
    for unit, path_data in oki["paths"].items():
        row = units.get(unit)
        color = _map_color(row.get("chg") if isinstance(row, dict) else None)
        parts.append(f'<path d="{path_data}" fill="{color}"/>')
    parts.append(f'<text x="0" y="{oki["h"]+4}" class="sub">沖縄（別縮尺）</text></g>')
    lx, ly = main["w"] - 168, 26
    parts.append(f'<g transform="translate({lx},{ly})">')
    parts.append('<text x="0" y="0" class="sub">実効局数の増減 2013→2026</text>')
    for index, (_, _, color, label) in enumerate(MAP_BINS):
        y = 12 + index * 17
        parts.append(f'<rect x="0" y="{y}" width="15" height="11" fill="{color}"/>')
        parts.append(f'<text x="22" y="{y+9.5}" class="tick">{label}</text>')
    parts.append("</g></svg>")
    result = "".join(parts)
    if ACTIVE_CONTENT_RE.search(result) or "<script" in result.casefold():
        raise ResearchOutputError("research map active content rejected")
    return result


def _validate_animation(doc: dict[str, Any]) -> dict[str, Any]:
    months = doc.get("months")
    if (not isinstance(months, list) or not months or
            any(not isinstance(m, str) or not MONTH_RE.fullmatch(m) for m in months) or
            months != sorted(set(months))):
        raise ResearchOutputError("animation month contract rejected")
    baseline = doc.get("baseline_month")
    anchor = doc.get("anchor_month")
    if baseline != months[0] or not isinstance(anchor, str) or anchor not in months:
        raise ResearchOutputError("animation temporal contract rejected")
    if any(m > anchor for m in months):
        raise ResearchOutputError("animation contains a post-anchor frame")
    missing = doc.get("missing_months")
    if (not isinstance(missing, list) or
            any(not isinstance(m, str) or not MONTH_RE.fullmatch(m) for m in missing) or
            missing != sorted(set(missing)) or set(missing) & set(months)):
        raise ResearchOutputError("animation missing-month contract rejected")
    expected: list[str] = []
    year, month = map(int, baseline.split("-"))
    anchor_year, anchor_number = map(int, anchor.split("-"))
    while (year, month) <= (anchor_year, anchor_number):
        expected.append(f"{year:04d}-{month:02d}")
        month += 1
        if month == 13:
            year += 1; month = 1
    if sorted(months + missing) != expected:
        raise ResearchOutputError("animation month coverage rejected")
    if (not isinstance(doc.get("frames"), list) or
            not isinstance(doc.get("national"), list) or
            len(doc["frames"]) != len(months) or len(doc["national"]) != len(months)):
        raise ResearchOutputError("animation frame contract rejected")
    return {"month_count": len(months), "baseline_month": baseline,
            "anchor_month": anchor, "missing_month_count": len(missing)}


def _verified_inputs(paths: list[str | Path]) -> tuple[list[str], list[dict[str, Any]]]:
    if not paths:
        raise ResearchOutputError("at least one upstream artifact is required")
    ids: list[str] = []
    rows: list[dict[str, Any]] = []
    for raw in paths:
        path = Path(raw)
        try:
            result = verify_artifact(path, strict_code=False)
            artifact = json.loads((path / "artifact.json").read_text(encoding="utf-8"))
        except (ArtifactError, OSError, ValueError, json.JSONDecodeError) as exc:
            raise ResearchOutputError("upstream artifact rejected") from exc
        if not result.get("ok"):
            raise ResearchOutputError("upstream artifact rejected")
        try:
            strict_current = bool(verify_artifact(path, strict_code=True).get("ok"))
        except (ArtifactError, OSError, ValueError, json.JSONDecodeError):
            strict_current = False
        artifact_id = result.get("artifact_id")
        if not isinstance(artifact_id, str) or len(artifact_id) != 64:
            raise ResearchOutputError("upstream artifact ID rejected")
        ids.append(artifact_id)
        rows.append({"artifact_id": artifact_id,
                     "artifact_type": artifact.get("artifact_type"),
                     "evidence_status": artifact.get("evidence_status"),
                     "source_authority": artifact.get("source_authority"),
                     "payload_verified": True,
                     "strict_code_current": strict_current})
    if len(ids) != len(set(ids)):
        raise ResearchOutputError("duplicate upstream artifact rejected")
    rows.sort(key=lambda r: r["artifact_id"])
    return sorted(ids), rows


def build_research_bundle(work_dir: str | Path, upstream_artifacts: list[str | Path],
                          *, unresolved_inputs: list[str] | None = None) -> dict[str, Any]:
    """Validate work outputs and return the complete payload for one bundle."""
    work = Path(work_dir)
    _validate_work_root(work)
    source_bytes = {target: _read_source(work, source)
                    for target, source in SOURCE_MEMBERS.items()}
    stats = _json_object(source_bytes["report/stats.json"], "stats")
    choropleth = _json_object(source_bytes["map/choropleth.json"], "choropleth")
    animation = _json_object(source_bytes["animation/animation_data.json"], "animation")
    animation_summary = _validate_animation(animation)
    main_map = choropleth.get("main")
    oki_map = choropleth.get("oki")
    units = choropleth.get("units")
    if (not isinstance(main_map, dict) or not isinstance(oki_map, dict) or
            not isinstance(units, dict) or
            any(not isinstance(part.get("paths"), dict) or
                not isinstance(part.get("w"), (int, float)) or
                not isinstance(part.get("h"), (int, float))
                for part in (main_map, oki_map))):
        raise ResearchOutputError("choropleth schema rejected")
    if not isinstance(stats.get("generated_from"), str):
        raise ResearchOutputError("stats provenance rejected")

    try:
        report_html = source_bytes["report/postal_bias_report.html"].decode("utf-8")
        animation_html = source_bytes["animation/postal_animation.html"].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ResearchOutputError("research HTML encoding rejected") from exc
    _validate_html_safety(report_html, allow_inline_script=False)
    maps = MAP_SVG_RE.findall(report_html)
    expected_map = _expected_map_svg(choropleth)
    if len(maps) != 1 or maps[0] != expected_map:
        raise ResearchOutputError("research map extraction rejected")
    _validate_html_safety(animation_html, allow_inline_script=True)
    embedded_match = ANIMATION_DATA_RE.search(animation_html)
    if embedded_match is None:
        raise ResearchOutputError("animation HTML contract rejected")
    try:
        embedded = json.loads(embedded_match.group(1))
    except json.JSONDecodeError as exc:
        raise ResearchOutputError("animation embedded data rejected") from exc
    animation_keys = ("months", "frames", "cells", "cell_px", "w", "h", "national",
                      "position_changes", "missing_months")
    if embedded != {key: animation.get(key) for key in animation_keys}:
        raise ResearchOutputError("animation HTML/data mismatch")
    source_bytes["map/postal_bias_map.svg"] = expected_map.encode("utf-8")

    input_ids, input_rows = _verified_inputs(upstream_artifacts)
    unresolved_raw = unresolved_inputs or []
    if any(not isinstance(x, str) or not x.strip() or len(x) > 1024 or
           any(ord(ch) < 32 for ch in x) for x in unresolved_raw):
        raise ResearchOutputError("unresolved input description rejected")
    unresolved = sorted(set(x.strip() for x in unresolved_raw))
    file_rows = [{"payload": target, "source_name": SOURCE_MEMBERS[target],
                  "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                 for target, data in sorted(source_bytes.items())
                 if target in SOURCE_MEMBERS]
    limitations = [
        "road_network_distance_not_computed; geographic access remains straight-line",
        "legal conclusions are not made by this bundle",
    ]
    provenance = {
        "schema_version": "research-output-provenance-v1",
        "components": ["report", "map", "animation"],
        "upstream_artifacts": input_rows,
        "source_files": file_rows,
        "derived_files": [{"payload": "map/postal_bias_map.svg",
                           "derived_from": "report/postal_bias_report.html",
                           "sha256": hashlib.sha256(
                               source_bytes["map/postal_bias_map.svg"]).hexdigest()}],
        "unresolved_inputs": unresolved,
        "limitations": limitations,
        "release_classification": "internal_only",
        "public_release_allowed": False,
    }
    component_manifest = {
        "schema_version": "research-output-components-v1",
        "report": {"entrypoint": "report/postal_bias_report.html"},
        "map": {"entrypoint": "map/postal_bias_map.svg",
                "data": "map/choropleth.json"},
        "animation": {"entrypoint": "animation/postal_animation.html",
                      "data": "animation/animation_data.json", **animation_summary},
    }
    qa = [{"kind": "warning", "code": "research_input_unresolved", "detail": item}
          for item in unresolved]
    stale_code_inputs = sorted(row["artifact_id"] for row in input_rows
                               if not row["strict_code_current"])
    qa.extend({"kind": "warning", "code": "upstream_code_version_mismatch",
               "artifact_id": artifact_id} for artifact_id in stale_code_inputs)
    reasons = (["unresolved_research_inputs"] if unresolved else []) + [
        "road_network_distance_missing"]
    if stale_code_inputs:
        reasons.append("upstream_code_version_mismatch")
    coverage = {
        "packaging_status": "pass",
        "research_status": "not_evaluable" if unresolved or limitations else "pass",
        "reason_codes": reasons,
        "public_release_allowed": False,
    }
    coverage["status"] = coverage["research_status"]
    payloads: dict[str, bytes] = dict(source_bytes)
    payloads.update({"provenance.json": _canonical(provenance),
                     "component_manifest.json": _canonical(component_manifest),
                     "coverage_summary.jsonl": _canonical(coverage),
                     "qa.jsonl": b"".join(_canonical(row) for row in qa)})
    return {"payloads": payloads, "input_artifact_ids": input_ids,
            "provenance": provenance, "component_manifest": component_manifest,
            "coverage_summary": coverage, "qa": qa}


def write_research_bundle(result: dict[str, Any], output_dir: str | Path, *,
                          acknowledge_internal_use: bool = False,
                          replace: bool = False) -> dict[str, Any]:
    if not acknowledge_internal_use:
        raise ResearchOutputError("internal-use acknowledgement required")
    output = Path(output_dir)
    try:
        if output.exists() and any(output.iterdir()) and not replace:
            raise ResearchOutputError("research output already exists")
    except ResearchOutputError:
        raise
    except OSError as exc:
        raise ResearchOutputError("research output rejected") from exc
    try:
        committed = commit_sealed_directory(
            output, result["payloads"], artifact_type=ARTIFACT_TYPE,
            source_authority="auxiliary", evidence_status="observed",
            release_classification="internal_only",
            input_artifact_ids=result["input_artifact_ids"], qa_files=["qa.jsonl"],
            preserve_existing=False, acknowledge_internal_use=True, strict_code=True,
        )
    except (ArtifactError, OSError, ValueError, KeyError) as exc:
        raise ResearchOutputError("research output rejected") from exc
    return {**committed, "packaging_status": result["coverage_summary"]["packaging_status"],
            "research_status": result["coverage_summary"]["research_status"]}


def package_research_outputs(work_dir: str | Path, output_dir: str | Path,
                             upstream_artifacts: list[str | Path], *,
                             unresolved_inputs: list[str] | None = None,
                             acknowledge_internal_use: bool = False,
                             replace: bool = False) -> dict[str, Any]:
    result = build_research_bundle(work_dir, upstream_artifacts,
                                   unresolved_inputs=unresolved_inputs)
    return write_research_bundle(result, output_dir,
                                 acknowledge_internal_use=acknowledge_internal_use,
                                 replace=replace)
