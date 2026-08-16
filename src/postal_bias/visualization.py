"""Deterministic offline SVG/HTML visualization of analysis artifacts."""
from __future__ import annotations
import html, json, math, os, tempfile, hashlib, re
from pathlib import Path
from typing import Any
from .artifacts import ArtifactError, seal_artifact, verify_artifact
from .analysis import CRS, ANALYSIS_ARTIFACT_TYPE

class VisualizationError(ValueError): pass

def _canon(v): return (json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode()
def _sha(v): return hashlib.sha256(_canon(v)).hexdigest()
def _rows(v,name,atype=None):
    if isinstance(v,(str,Path)):
        p=Path(v)
        try: ver=verify_artifact(p,strict_code=True)
        except Exception as e: raise VisualizationError("sealed input rejected") from e
        if not ver.get("ok"): raise VisualizationError("artifact verification failed")
        a=json.loads((p/"artifact.json").read_text(encoding="utf8"));
        if atype and a.get("artifact_type")!=atype: raise VisualizationError("artifact type rejected")
        f=p/name
        if not f.exists(): raise VisualizationError("payload missing")
        return [json.loads(x) for x in f.read_text(encoding="utf8").splitlines() if x.strip()],a.get("artifact_id")
    if isinstance(v,dict): return list(v.get("rows",v.get("records",[]))),v.get("artifact_id")
    if isinstance(v,list): return v,None
    raise VisualizationError("fixture rejected")

def _finite(v):
    if isinstance(v,bool): return False
    try:return math.isfinite(float(v))
    except (TypeError,ValueError):return False

def render_visualization(analysis, grid, config):
    if not isinstance(config,dict) or not isinstance(config.get("months"),list) or not config["months"]: raise VisualizationError("config invalid")
    months=config["months"]; radius=int(config.get("radius_m",2000)); metric=str(config.get("metric","raw_count")); extent=config.get("extent")
    if radius not in (2000,5000,10000,20000) or not isinstance(extent,list) or len(extent)!=4 or not all(_finite(x) for x in extent) or not (float(extent[0])<float(extent[2]) and float(extent[1])<float(extent[3])): raise VisualizationError("fixed visualization config invalid")
    analysis_missing = analysis is None or (isinstance(analysis,(str,Path)) and not Path(analysis).exists())
    grid_missing = grid is None or (isinstance(grid,(str,Path)) and not Path(grid).exists())
    if analysis_missing: cells=[]; aid=None
    else: cells,aid=_rows(analysis,"cell_metrics.jsonl",ANALYSIS_ARTIFACT_TYPE)
    if grid_missing: grids=[]; gid=None
    else: grids,gid=_rows(grid,"grid.jsonl","analysis_grid")
    seen_cells=set()
    for r in cells:
        key=(r.get("month"),r.get("grid_id"),r.get("radius_m"),r.get("metric",metric),json.dumps(r.get("filter"),sort_keys=True,ensure_ascii=False))
        if key in seen_cells: raise VisualizationError("analysis cell duplicate")
        seen_cells.add(key)
        if r.get("radius_m") not in (2000,5000,10000,20000) or not isinstance(r.get("grid_id"),str) or (r.get(metric) is not None and not _finite(r.get(metric))): raise VisualizationError("analysis cell contract invalid")
    missing="missing_analysis_grid" if not cells or not grids else None
    for r in grids:
        if r.get("crs")!=CRS or not all(_finite(r.get(k)) for k in ("x","y")): raise VisualizationError("grid contract invalid")
    available=[r for r in cells if r.get("month") in months and r.get("radius_m")==radius and _finite(r.get(metric))]
    lo=min((float(r[metric]) for r in available),default=0.0); hi=max((float(r[metric]) for r in available),default=1.0)
    if "color_min" in config: lo=float(config["color_min"])
    if "color_max" in config: hi=float(config["color_max"])
    if not (_finite(lo) and _finite(hi) and hi>=lo): raise VisualizationError("color scale invalid")
    by={(r.get("month"),r.get("grid_id")):r for r in available}; frames=[]
    reason_set=set()
    grid_ids={g.get("grid_id") for g in grids}
    if any(r.get("grid_id") not in grid_ids for r in cells): reason_set.add("unknown_grid")
    xmin,ymin,xmax,ymax=map(float,extent); w=800; h=500
    def sx(x):return (float(x)-xmin)/(xmax-xmin)*w
    def sy(y):return h-(float(y)-ymin)/(ymax-ymin)*h
    for month in months:
        month_rows=[r for r in cells if r.get("month")==month and r.get("radius_m")==radius and r.get("grid_id") in {g.get("grid_id") for g in grids}]
        missing_month=not month_rows or any(not any(x.get("grid_id")==g.get("grid_id") for x in month_rows) for g in grids) or not any(_finite(x.get(metric)) for x in month_rows)
        body=[]
        for g in sorted(grids,key=lambda x:str(x.get("grid_id"))):
            row=by.get((month,g.get("grid_id"))); x,y=sx(g["x"]),sy(g["y"]); val=None if row is None else row.get(metric)
            color="#bdbdbd" if val is None else "#%02x%02x%02x"%(int(255-180*((float(val)-lo)/(hi-lo) if hi>lo else 0)),80,int(80+170*((float(val)-lo)/(hi-lo) if hi>lo else 0)))
            body.append(f'<circle cx="{x:.3f}" cy="{y:.3f}" r="8" fill="{color}" data-grid="{html.escape(str(g["grid_id"]),quote=True)}"/>')
        label="missing_source" if missing_month or not grids else f"{metric} {radius}m"
        if missing_month: reason_set.add("missing_analysis_grid" if not grids else "no_matching_analysis_rows")
        svg=f'<svg viewBox="0 0 {w} {h}" role="img" aria-label="{html.escape(str(month)+" "+label,quote=True)}"><rect width="100%" height="100%" fill="#fff"/><text x="12" y="24">{html.escape(str(month)+" "+label)}</text>{"".join(body)}</svg>'
        manifest={"month":month,"metric":metric,"radius_m":radius,"extent":extent,"color_min":lo,"color_max":hi,"source_artifact_ids":[x for x in (aid,gid) if x],"coverage_status":"not_evaluable" if missing_month or not grids else "pass","qa":[label] if label=="missing_source" else []}
        frames.append({"frame_id":_sha(manifest),**manifest,"svg":svg})
    reasons=sorted(reason_set)
    return {"frames":frames,"metadata":{"public_release_allowed":False,"release_classification":"internal_only","metric":metric,"radius_m":radius,"global_color_min":lo,"global_color_max":hi,"source_artifact_ids":[x for x in (aid,gid) if x],"coverage_reason_codes":reasons},"qa":[],"coverage_summary":{"status":"not_evaluable" if any(f["coverage_status"]!="pass" for f in frames) else "pass","reason_codes":reasons}}

def write_visualization_bundle(result,outdir,*,acknowledge_internal_use=False,replace=False):
    if not acknowledge_internal_use: raise VisualizationError("internal-use acknowledgement required")
    out=Path(outdir); parent=out.parent; parent.mkdir(parents=True,exist_ok=True)
    if out.exists() and any(out.iterdir()) and not replace: raise VisualizationError("output exists")
    stage=Path(tempfile.mkdtemp(prefix=".visualization-",dir=parent))
    try:
        seen=set()
        for row in result.get("frames",[]):
            fid=row.get("frame_id")
            if not isinstance(fid,str) or not re.fullmatch(r"[0-9a-f]{64}",fid) or fid in seen: raise VisualizationError("frame identifier rejected")
            manifest={k:v for k,v in row.items() if k not in {"frame_id","svg"}}
            if _sha(manifest)!=fid: raise VisualizationError("frame hash mismatch")
            seen.add(fid)
            target=(stage/(fid+".svg")).resolve(); target.relative_to(stage.resolve()); target.write_text(str(row.get("svg","")),encoding="utf8")
        (stage/"frame_manifest.jsonl").write_text("".join(_canon({k:v for k,v in x.items() if k!="svg"}).decode() for x in result.get("frames",[])),encoding="utf8")
        (stage/"metadata.json").write_bytes(_canon(result.get("metadata",{}))); (stage/"qa.jsonl").write_text("",encoding="utf8"); (stage/"coverage_summary.jsonl").write_bytes(_canon(result.get("coverage_summary",{})))
        embedded=json.dumps([x["svg"] for x in result.get("frames",[])],ensure_ascii=False,separators=(",",":"))
        for old,new in (("<","\\u003c"),(">","\\u003e"),("&","\\u0026"),("\u2028","\\u2028"),("\u2029","\\u2029")): embedded=embedded.replace(old,new)
        html_doc='<html><body><h1>Offline visualization</h1><input type="range" min="0" max="'+str(max(0,len(result.get("frames",[]))-1))+'" id="s" aria-label="month"><div id="f"></div><script>const F='+embedded+';const s=document.getElementById("s"),f=document.getElementById("f");function u(){f.innerHTML=F[s.value]||""}s.oninput=u;u();</script></body></html>'
        (stage/"animation.html").write_text(html_doc,encoding="utf8")
        seal=seal_artifact(stage,artifact_type="postal_bias_visualization",source_authority="auxiliary",evidence_status="effective",release_classification="internal_only",input_artifact_ids=result.get("metadata",{}).get("source_artifact_ids",[]),acknowledge_internal_use=True)
        if not verify_artifact(stage,strict_code=True).get("ok"): raise VisualizationError("visualization staging verification failed")
        backup=None
        if out.exists() and any(out.iterdir()): backup=parent/("."+out.name+".backup"); os.replace(out,backup)
        elif out.exists(): out.rmdir()
        os.replace(stage,out)
        if not verify_artifact(out,strict_code=True).get("ok"):
            try:
                import shutil
                if out.exists(): shutil.rmtree(out,ignore_errors=False)
                if backup and backup.exists(): os.replace(backup,out); backup=None
            except Exception as rollback_exc: raise VisualizationError("visualization rollback failed") from rollback_exc
            raise VisualizationError("visualization verification failed")
        warnings=[]
        if backup and backup.exists():
            try: import shutil; shutil.rmtree(backup,ignore_errors=False); backup=None
            except Exception as cleanup_exc: warnings.append(f"backup_cleanup_failed:{backup.name}:{type(cleanup_exc).__name__}")
        result_paths={"output_dir":str(out),"artifact_id":seal["artifact_id"]}
        if warnings: result_paths["cleanup_warnings"]=warnings
        return result_paths
    except Exception as exc:
        if stage.exists(): import shutil; shutil.rmtree(stage,ignore_errors=True)
        if backup is not None and backup.exists() and not out.exists(): os.replace(backup,out)
        raise VisualizationError("visualization output rejected") from exc
