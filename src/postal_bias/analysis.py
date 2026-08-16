"""Deterministic local monthly density and inequality analysis.

All metric coordinates are already projected to EPSG:6933 (NSIDC EASE equal
area).  The module never downloads, imputes, carries observations forward, or
infers status from P30.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from .artifacts import ArtifactError, seal_artifact, verify_artifact

CRS = "EPSG:6933"
ANALYSIS_ARTIFACT_TYPE = "analysis_monthly"
RADII_M = (2000, 5000, 10000, 20000)
UNIT_PEOPLE = "persons"


class AnalysisError(ValueError):
    pass


def _canon(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _hash(row: dict[str, Any], field: str = "row_sha256") -> str:
    v = dict(row); v.pop(field, None)
    # External row contracts use canonical JSON without a trailing newline.
    return hashlib.sha256(json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _finite(v: Any) -> bool:
    if isinstance(v, bool): return False
    try: return math.isfinite(float(v))
    except (TypeError, ValueError): return False


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try: rows = [json.loads(x) for x in path.read_text(encoding="utf8").splitlines() if x.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc: raise AnalysisError("JSONL input rejected") from exc
    if not all(isinstance(x, dict) for x in rows): raise AnalysisError("row schema rejected")
    return rows


def _input(value: Any, names: tuple[str, ...], artifact_type: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any], str | None]:
    if isinstance(value, (str, Path)):
        root = Path(value)
        if not root.is_dir(): raise AnalysisError("sealed input directory required")
        try: ver = verify_artifact(root, strict_code=True)
        except (ArtifactError, OSError, ValueError) as exc: raise AnalysisError("sealed input rejected") from exc
        if not ver.get("ok"): raise AnalysisError("strict artifact verification failed")
        art = json.loads((root / "artifact.json").read_text(encoding="utf8"))
        if artifact_type and art.get("artifact_type") != artifact_type: raise AnalysisError("artifact type rejected")
        for name in names:
            p = root / name
            if p.exists():
                meta = json.loads((root / "metadata.json").read_text(encoding="utf8")) if (root / "metadata.json").exists() else {}
                return _jsonl(p), meta, art.get("artifact_id")
        raise AnalysisError("required input payload missing")
    if isinstance(value, dict):
        meta=dict(value.get("metadata", {}))
        if meta.get("schema_version") not in (None, "analysis-input-v1"): raise AnalysisError("fixture metadata schema rejected")
        return list(value.get("rows", value.get("records", []))), meta, value.get("artifact_id")
    if isinstance(value, list): return value, {}, None
    raise AnalysisError("fixture adapter rejected")


def _month(v: Any) -> str:
    s = str(v)
    try: date.fromisoformat(s + "-01" if re.fullmatch(r"\d{4}-\d{2}", s) else s)
    except ValueError as exc: raise AnalysisError("month invalid") from exc
    return s[:7]


def validate_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict): raise AnalysisError("config rejected")
    if tuple(config.get("radii_m", ())) != RADII_M: raise AnalysisError("radii must be fixed 2/5/10/20km")
    required = ("months", "extent", "grid_id_field", "population_series", "service_filter", "status_filter", "closure_filter", "section_filter")
    if any(k not in config for k in required): raise AnalysisError("analysis config fields missing")
    if not isinstance(config["months"], list) or not config["months"] or any(not isinstance(x,str) or not re.fullmatch(r"\d{4}-\d{2}",x) for x in config["months"]): raise AnalysisError("months invalid")
    months = list(config["months"])
    if len(set(months)) != len(months) or months != sorted(months): raise AnalysisError("months must be unique and sorted")
    if not isinstance(config["extent"], (list, tuple)) or len(config["extent"]) != 4 or not all(_finite(x) for x in config["extent"]): raise AnalysisError("extent invalid")
    if not (config["extent"][0] < config["extent"][2] and config["extent"][1] < config["extent"][3]): raise AnalysisError("extent ordering invalid")
    for name in ("status_filter","section_filter","closure_filter","service_filter"):
        value=config[name]
        if value == "all": continue
        if isinstance(value,bool) or not isinstance(value,list) or not value or any(not isinstance(x,str) or not x for x in value) or len(set(value)) != len(value): raise AnalysisError(name + " filter invalid")
    return {**config, "months": months, "radii_m": list(RADII_M)}


def _filter_matches(value: Any, selected: Any) -> bool:
    if selected == "all" or selected is None: return True
    if not isinstance(selected, list): return False
    return isinstance(value, str) and value in selected


def _validate_snapshots(rows: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str]] = set()
    for r in rows:
        m = _month(r.get("month") or r.get("cutoff")); entity = r.get("entity_id")
        if not isinstance(entity, str) or not entity or (m, entity) in seen: raise AnalysisError("snapshot duplicate or entity missing")
        seen.add((m, entity))
        if r.get("crs") != CRS or not _finite(r.get("x")) or not _finite(r.get("y")): raise AnalysisError("snapshot CRS/coordinate invalid")
        for k in ("status", "section", "series", "provenance", "row_sha256"):
            if not isinstance(r.get(k), str) or not r[k]: raise AnalysisError("snapshot field missing")
        if r["row_sha256"] != _hash(r): raise AnalysisError("snapshot row hash mismatch")


def _validate_grid(rows: list[dict[str, Any]]) -> None:
    seen: set[str] = set()
    for r in rows:
        gid = r.get("grid_id")
        if not isinstance(gid, str) or not gid or gid in seen: raise AnalysisError("grid duplicate or id missing")
        seen.add(gid)
        extent=r.get("extent")
        if r.get("crs") != CRS or not _finite(r.get("x")) or not _finite(r.get("y")) or not _finite(r.get("window_area_km2")) or float(r["window_area_km2"]) <= 0 or not isinstance(extent,(list,tuple)) or len(extent)!=4 or not all(_finite(v) for v in extent) or not (extent[0] < extent[2] and extent[1] < extent[3]) or not (extent[0] <= float(r["x"]) <= extent[2] and extent[1] <= float(r["y"]) <= extent[3]): raise AnalysisError("grid contract invalid")
        if r.get("row_sha256") != _hash(r): raise AnalysisError("grid row hash mismatch")


def _validate_population(rows: list[dict[str, Any]]) -> None:
    seen: set[tuple[str, str, str, str]] = set()
    for r in rows:
        key = (str(r.get("grid_id")), _month(r.get("month")), str(r.get("series")), str(r.get("unit")))
        if key in seen: raise AnalysisError("population duplicate")
        seen.add(key)
        if not r.get("grid_id") or not r.get("source") or r.get("unit") != UNIT_PEOPLE or not _finite(r.get("population")) or float(r["population"]) < 0 or r.get("row_sha256") != _hash(r): raise AnalysisError("population contract invalid")


def gini(values: list[float], weights: list[float] | None = None) -> float | None:
    if not values or any(not _finite(v) or float(v) < 0 for v in values): return None
    if weights is None: weights = [1.0] * len(values)
    if not isinstance(weights,list) or len(weights) != len(values) or any(not _finite(w) or float(w) < 0 for w in weights) or sum(weights) <= 0: return None
    pairs = sorted((float(v), float(w)) for v, w in zip(values, weights)); total = sum(v*w for v,w in pairs); wsum = sum(weights)
    if total <= 0: return None
    cumw = cum = 0.0
    for v,w in pairs: cumw += w; cum += v*w
    # Weighted Gini via pairwise absolute differences.
    return sum(wi*wj*abs(vi-vj) for vi,wi in pairs for vj,wj in pairs) / (2 * wsum * total)


def theil_t(values: list[float], weights: list[float] | None = None) -> float | None:
    if not values or any(not _finite(v) or float(v) < 0 for v in values): return None
    weights = [1.0] * len(values) if weights is None else weights
    if not isinstance(weights,list) or len(weights) != len(values) or any(not _finite(w) or float(w)<0 for w in weights) or sum(weights) <= 0: return None
    mean = sum(float(v)*float(w) for v,w in zip(values,weights))/sum(weights)
    if mean <= 0: return None
    return sum(float(w)*(float(v)/mean)*math.log(float(v)/mean) for v,w in zip(values,weights) if float(v)>0)/sum(weights)


def hoover(values: list[float], weights: list[float] | None = None) -> float | None:
    if not values or any(not _finite(v) or float(v) < 0 for v in values): return None
    weights = [1.0] * len(values) if weights is None else weights; total=sum(float(v) for v in values)
    if not isinstance(weights,list) or total <= 0 or len(weights)!=len(values) or any(not _finite(w) or float(w)<0 for w in weights) or sum(weights)<=0: return None
    mean = total/sum(weights)
    return sum(abs(float(v)-mean*float(w)) for v,w in zip(values,weights))/(2*total)


def global_moran(values: list[float], edges: list[tuple[int,int,float]]) -> float | None:
    if len(values) < 3 or any(not _finite(v) for v in values): return None
    if not isinstance(edges,list): return None
    seen=set()
    for edge in edges:
        if not isinstance(edge,(tuple,list)) or len(edge)!=3: return None
        i,j,w=edge
        if isinstance(i,bool) or isinstance(j,bool) or not isinstance(i,int) or not isinstance(j,int) or i<0 or j<0 or i>=len(values) or j>=len(values) or i==j or (i,j) in seen or not _finite(w) or float(w)<=0: return None
        seen.add((i,j))
    mean=sum(values)/len(values); dev=[float(v)-mean for v in values]; variance=sum(x*x for x in dev)
    wsum=sum(float(w) for _,_,w in edges)
    if variance<=0 or wsum<=0: return None
    num=sum(float(w)*dev[i]*dev[j] for i,j,w in edges if 0<=i<len(values) and 0<=j<len(values))
    return len(values)/wsum*num/variance


def analyze_monthly(snapshots: Any, grid: Any, population: Any, config: dict[str, Any]) -> dict[str, Any]:
    cfg=validate_config(config); qa=[]; reasons=[]
    try: snap,_,sid=_input(snapshots,("snapshots.jsonl","records.jsonl"),"monthly_facility_snapshot"); grids,_,gid=_input(grid,("grid.jsonl","records.jsonl"),"analysis_grid"); pops,_,pid=_input(population,("population.jsonl","records.jsonl"),"population_grid"); _validate_snapshots(snap); _validate_grid(grids); _validate_population(pops)
    except AnalysisError as exc:
        return {"cell_metrics":[],"inequality":[],"qa":[{"kind":"error","code":"input_invalid"}],"metadata":{"public_release_allowed":False,"release_classification":"internal_only","required_inputs":["snapshots","grid","population"]},"coverage_summary":{"status":"not_evaluable","reason_codes":[str(exc)]}}
    if not grids:
        return {"cell_metrics":[],"inequality":[],"qa":[{"kind":"error","code":"required_input_empty"}],"metadata":{"public_release_allowed":False,"release_classification":"internal_only","required_inputs":["snapshots","grid","population"]},"coverage_summary":{"status":"not_evaluable","reason_codes":["required_input_empty"]}}
    snap=sorted(snap,key=lambda r:(str(r.get("month") or r.get("cutoff")),str(r.get("entity_id")))); grids=sorted(grids,key=lambda r:str(r.get("grid_id")))
    by_month={m:[r for r in snap if _month(r.get("month") or r.get("cutoff"))==m] for m in cfg["months"]}; popmap={(str(r["grid_id"]),_month(r["month"]),str(r["series"])):float(r["population"]) for r in pops}
    cells=[]; inequalities=[]
    reasons.extend(["missing_snapshot_month"] if any(not any(_month(r.get("month") or r.get("cutoff"))==m for r in snap) for m in cfg["months"]) else [])
    for month in cfg["months"]:
        offices=[] if any(_month(r.get("month") or r.get("cutoff"))==month for r in snap) else None
        for r in by_month.get(month,[]):
            if offices is None: break
            if cfg.get("population_series") and r.get("series") not in {cfg.get("population_series"),"formal"}: continue
            if r.get("series")=="formal" and (r.get("provenance")=="unofficial" or r.get("evidence_status") in {"unconfirmed","announced"} or r.get("status") in {"unconfirmed","announced"}): continue
            if not _filter_matches(r.get("status"), cfg.get("status_filter")): continue
            if not _filter_matches(r.get("section"), cfg.get("section_filter")): continue
            if not _filter_matches(r.get("closure_status"), cfg.get("closure_filter")): continue
            if not _filter_matches(r.get("service"), cfg.get("service_filter")): continue
            offices.append(r)
        for gr in grids:
            pop=popmap.get((gr["grid_id"],month,str(cfg["population_series"])))
            if pop is None and "missing_population_month" not in reasons: reasons.append("missing_population_month")
            for radius in RADII_M:
                in_range=[] if offices is not None else None
                if offices is not None: in_range=[o for o in offices if math.hypot(float(o["x"])-float(gr["x"]),float(o["y"])-float(gr["y"]))<=radius]
                count=len(in_range) if in_range is not None else None; nearest=min((math.hypot(float(o["x"])-float(gr["x"]),float(o["y"])-float(gr["y"])) for o in in_range), default=None) if in_range is not None else None
                row={"month":month,"grid_id":gr["grid_id"],"radius_m":radius,"raw_count":count,"window_area_km2":float(gr["window_area_km2"]),"density_km2":None if count is None else count/float(gr["window_area_km2"]),"population":pop,"count_per_100k":None if count is None or pop in (None,0) else count/pop*100000,"nearest_distance_m":nearest,"numerator":"office_count","denominator":"window_area_km2","unit":"offices/km2","filter":{"status":cfg.get("status_filter"),"section":cfg.get("section_filter"),"series":cfg.get("population_series")},"provenance":"official_formal","qa":[]}
                if pop is None: row["qa"].append("population_missing")
                if count is None: row["qa"].append("snapshot_missing")
                elif pop==0: row["qa"].append("population_zero")
                cells.append(row)
            vals=[x["raw_count"] for x in cells if x["month"]==month and x["grid_id"]==gr["grid_id"] and x["raw_count"] is not None]
            inequalities.append({"month":month,"grid_id":gr["grid_id"],"gini":gini(vals),"theil_t":theil_t(vals),"hoover":hoover(vals),"provenance":"computed"})
    status="pass" if cells and not reasons else "not_evaluable"
    return {"cell_metrics":cells,"inequality":inequalities,"qa":qa,"metadata":{"public_release_allowed":False,"release_classification":"internal_only","required_inputs":["snapshots","grid","population"],"input_artifact_ids":[x for x in (sid,gid,pid) if x]},"coverage_summary":{"status":status,"reason_codes":reasons}}


def write_analysis_bundle(result: dict[str, Any], output_dir: str | Path, *, acknowledge_internal_use: bool=False, replace: bool=False) -> dict[str,str]:
    if not acknowledge_internal_use: raise AnalysisError("internal-use acknowledgement required")
    out=Path(output_dir); parent=out.parent; parent.mkdir(parents=True,exist_ok=True)
    if out.exists() and any(out.iterdir()) and not replace: raise AnalysisError("output exists")
    stage=Path(tempfile.mkdtemp(prefix=".analysis-",dir=parent)); backup=None
    try:
        for name,rows in (("cell_metrics.jsonl",result.get("cell_metrics",[])),("inequality.jsonl",result.get("inequality",[])),("qa.jsonl",result.get("qa",[])),("coverage_summary.jsonl",[result.get("coverage_summary",{})])):
            with (stage/name).open("w",encoding="utf8",newline="\n") as f:
                for row in rows:f.write(_canon(row).decode())
        with (stage/"metadata.json").open("w",encoding="utf8") as f:f.write(_canon(result.get("metadata",{})).decode())
        seal=seal_artifact(stage,artifact_type=ANALYSIS_ARTIFACT_TYPE,source_authority="auxiliary",evidence_status="effective",release_classification="internal_only",input_artifact_ids=result.get("metadata",{}).get("input_artifact_ids",[]),acknowledge_internal_use=True)
        if out.exists() and any(out.iterdir()):
            backup=parent/("."+out.name+".backup"); os.replace(out,backup); os.replace(stage,out)
        elif out.exists():
            out.rmdir(); os.replace(stage,out)
        else: os.replace(stage,out)
        if not verify_artifact(out).get("ok"):
            try:
                import shutil
                if out.exists(): shutil.rmtree(out,ignore_errors=False)
                if backup is not None and backup.exists(): os.replace(backup,out); backup=None
            except Exception as rollback_exc:
                raise AnalysisError("analysis output verification rollback failed") from rollback_exc
            raise AnalysisError("analysis output verification failed")
        cleanup_warnings=[]
        if backup is not None and backup.exists():
            try:
                import shutil; shutil.rmtree(backup,ignore_errors=False); backup=None
            except Exception as cleanup_exc:
                cleanup_warnings.append(f"backup_cleanup_failed:{backup.name}:{type(cleanup_exc).__name__}")
        result_paths={"output_dir":str(out),"artifact_id":seal["artifact_id"]}
        if cleanup_warnings: result_paths["cleanup_warnings"]=cleanup_warnings
        return result_paths
    except Exception as exc:
        if stage.exists():
            import shutil; shutil.rmtree(stage,ignore_errors=True)
        if backup is not None and backup.exists() and not out.exists(): os.replace(backup,out)
        raise AnalysisError("analysis output rejected") from exc
