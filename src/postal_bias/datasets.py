"""Versioned local dataset descriptor registry; descriptors never copy payloads."""
from __future__ import annotations
import hashlib, json, os, tempfile, copy
from datetime import date
from pathlib import Path
from typing import Any
class DatasetError(ValueError): pass
FAMILIES={"population_mesh","admin_boundary","depop_designation","road","rail","bus","p30"}
REQUIRED={"dataset_id","family","title","publisher","format","mime","coverage_date","granularity","crs_epsg","geometry","field_schema","license","terms","public_release_allowed"}
def _sha(b): return hashlib.sha256(b).hexdigest()
def _artifact(path):
    p=Path(path); st=p.lstat()
    if p.is_symlink() or getattr(st,"st_file_attributes",0)&0x400 or not p.is_file(): raise DatasetError("artifact path rejected")
    return _sha(p.read_bytes()),st.st_size
def validate_dataset(desc: dict[str,Any], *, existing=None):
    desc=copy.deepcopy(desc)
    if not isinstance(desc,dict) or not REQUIRED.issubset(desc): raise DatasetError("descriptor fields missing")
    if not isinstance(desc["dataset_id"],str) or not desc["dataset_id"] or desc["family"] not in FAMILIES: raise DatasetError("descriptor identity invalid")
    if isinstance(desc["crs_epsg"],bool) or not isinstance(desc["crs_epsg"],int) or desc["crs_epsg"]<=0 or not isinstance(desc.get("geometry"),str) or not desc.get("geometry") or not isinstance(desc.get("granularity"),str) or not desc.get("granularity"): raise DatasetError("CRS/geometry/schema required")
    fs=desc.get("field_schema")
    if not isinstance(fs,list) or any(not isinstance(f,dict) or not isinstance(f.get("name"),str) or not isinstance(f.get("type"),str) or ("nullable" in f and not isinstance(f.get("nullable"),bool)) for f in fs): raise DatasetError("field schema invalid")
    for k in ("coverage_date","reference_date","start_date","end_date"):
        if desc.get(k):
            try: date.fromisoformat(str(desc[k]))
            except ValueError as e: raise DatasetError("date invalid") from e
    if desc["family"]=="population_mesh" and not desc.get("units"): raise DatasetError("population units required")
    if desc["family"] in {"population_mesh","admin_boundary","depop_designation"} and not (desc.get("reference_date") or desc.get("coverage_date")): raise DatasetError("reference date required")
    if not isinstance(desc.get("public_release_allowed"),bool): raise DatasetError("public release flag invalid")
    if not isinstance(desc.get("license"),str) or not desc.get("license") or not isinstance(desc.get("terms"),str) or not desc.get("terms"): raise DatasetError("license/terms required")
    if desc.get("public_release_allowed") and not desc.get("release_review_id"): raise DatasetError("public release review required")
    if existing and any(d.get("dataset_id")==desc["dataset_id"] for d in existing): raise DatasetError("duplicate dataset id")
    if desc.get("artifact_path"):
        digest,size=_artifact(desc["artifact_path"])
        if desc.get("artifact_sha256") not in (None,digest) or desc.get("artifact_bytes") not in (None,size): raise DatasetError("artifact hash mismatch")
        desc["artifact_sha256"],desc["artifact_bytes"]=digest,size
    desc["public_release_allowed"]=bool(desc.get("public_release_allowed",False))
    return desc
def register_dataset(descriptor, registry=None):
    d=json.loads(Path(descriptor).read_text(encoding="utf8")) if isinstance(descriptor,(str,Path)) else dict(descriptor)
    rows=[]
    if registry:
        p=Path(registry); rows=[json.loads(x) for x in p.read_text(encoding="utf8").splitlines() if x.strip()] if p.exists() else []
    d=validate_dataset(d,existing=rows)
    combined=rows+[d]; ids=[x.get("dataset_id") for x in combined]
    if len(set(ids))!=len(ids): raise DatasetError("duplicate dataset id")
    graph={x.get("dataset_id"):x.get("supersedes") for x in combined if x.get("dataset_id")}
    if any(t not in set(ids) for t in graph.values() if t): raise DatasetError("dataset supersession target invalid")
    def visit(node, trail):
        if node in trail: raise DatasetError("dataset supersession cycle")
        target=graph.get(node)
        if target: visit(target, trail|{node})
    for node in ids: visit(node,set())
    rows.append(d); return {"datasets":rows,"metadata":{"dataset_count":len(rows),"public_release_allowed":False}}
def write_dataset_bundle(result, output_dir, *, acknowledge_internal_use=False, replace=False):
    if not acknowledge_internal_use: raise DatasetError("internal-use acknowledgement required")
    out=Path(output_dir); out.mkdir(parents=True,exist_ok=True); target=out/"datasets.jsonl"; meta=out/"metadata.json"
    if not replace and (target.exists() or meta.exists()): raise DatasetError("output exists")
    staged=[]
    try:
      for p,val in ((target,result["datasets"]),(meta,result["metadata"])):
        fd,n=tempfile.mkstemp(prefix="."+p.name+".",dir=out); os.close(fd); t=Path(n)
        staged.append((p,t))
        with t.open("w",encoding="utf8") as f:
            if p==meta: f.write(json.dumps(val,ensure_ascii=False,indent=2)+"\n")
            else:
                for row in val:f.write(json.dumps(row,ensure_ascii=False,separators=(",",":"))+"\n")
      backups=[]; committed=[]
      try:
        for p,_ in staged:
          if p.exists():
            b=p.with_name("."+p.name+".backup"); os.replace(p,b); backups.append((p,b))
        for p,t in staged: os.replace(t,p); committed.append(p)
      except Exception as exc:
        for p in committed:
          if p.exists(): p.unlink()
        for p,b in reversed(backups):
          if b.exists(): os.replace(b,p)
        raise DatasetError("output commit failed") from exc
      warnings=[]
      for _,b in backups:
        try:
          if b.exists(): b.unlink()
        except OSError as exc: warnings.append({"path":b.name,"error":type(exc).__name__})
      if warnings:
        try:
          m=json.loads(meta.read_text(encoding="utf8")); m["cleanup_warnings"]=warnings
          t=meta.with_name("."+meta.name+".cleanup"); t.write_text(json.dumps(m,ensure_ascii=False)+"\n",encoding="utf8"); os.replace(t,meta)
        except (OSError,ValueError): pass
    finally:
      for _,t in staged:
        if t.exists(): t.unlink()
    return {"datasets":str(target),"metadata":str(meta)}
