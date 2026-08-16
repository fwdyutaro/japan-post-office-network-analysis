"""Local-only Wayback CDX parsing with fail-closed URL and provenance checks."""
from __future__ import annotations
import hashlib, json, os, tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any

class WaybackError(ValueError): pass
ALLOW_HOSTS={"www.post.japanpost.jp","post.japanpost.jp"}
ALLOW_PREFIX="/newsrelease/storeinformation/"
MAX_INPUT_BYTES=20*1024*1024; MAX_ROWS=200000
def _sha(v): return hashlib.sha256(v).hexdigest()
def _json(v): return (json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(",",":"))+"\n").encode()
def _url(u):
    if not isinstance(u,str): raise WaybackError("invalid URL")
    p=urlsplit(u)
    if p.scheme.lower()!="https" or p.hostname not in ALLOW_HOSTS or p.username or p.password or p.port or p.fragment or p.query or "%" in p.path or not p.path.startswith(ALLOW_PREFIX): raise WaybackError("URL outside allowlist")
    return "https://"+p.hostname.lower()+p.path
def _iso(ts):
    if not isinstance(ts,str) or len(ts)!=14 or not ts.isdigit(): raise WaybackError("invalid capture timestamp")
    try: return datetime.strptime(ts,"%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).isoformat().replace("+00:00","Z")
    except ValueError as e: raise WaybackError("invalid capture timestamp") from e
def _load(value):
    if isinstance(value,(str,Path)):
        p=Path(value); st=p.lstat()
        if p.is_symlink() or getattr(st,"st_file_attributes",0)&0x400: raise WaybackError("input path rejected")
        b=p.read_bytes()
    else: b=value if isinstance(value,bytes) else None
    if b is None or len(b)>MAX_INPUT_BYTES or b'\0' in b: raise WaybackError("input rejected")
    try:
        obj=json.loads(b.decode("utf-8"))
        if isinstance(obj,list) and (not obj or isinstance(obj[0],dict)): return obj,b
    except (UnicodeDecodeError,json.JSONDecodeError): pass
    rows=[]
    for line in b.decode("utf-8").splitlines():
        if line.strip(): rows.append(json.loads(line))
    if len(rows)>MAX_ROWS: raise WaybackError("row limit exceeded")
    return rows,b
def parse_wayback_cdx(value, *, coverage_date=None, snapshot_bytes=None):
    rows, raw=_load(value); qa=[]; records=[]; seen=set(); by_url={}; redirects={}
    if rows and isinstance(rows[0],list):
        header=rows[0]; rows=[dict(zip(header,r)) for r in rows[1:]]
    for idx,row in enumerate(rows,1):
        if not isinstance(row,dict): qa.append({"kind":"error","code":"malformed_row","row":idx}); continue
        try:
            original=_url(row.get("original") or row.get("url")); ts=row.get("timestamp"); cap=_iso(ts)
            digest=row.get("digest"); length=row.get("length")
            mime=row.get("mime"); status=row.get("status")
            if not isinstance(digest,str) or not digest or not isinstance(mime,str) or not mime or not isinstance(status,(str,int)) or length is None: raise WaybackError("required CDX fields missing")
            length=int(length)
            if length<0: raise WaybackError("invalid length")
            cov=row.get("coverage_date") or coverage_date
            try: datetime.strptime(cov,"%Y-%m-%d")
            except (TypeError,ValueError) as e: raise WaybackError("coverage date required") from e
            key=(original,digest)
            if key in seen: qa.append({"kind":"info","code":"duplicate_capture","row":idx}); continue
            seen.add(key); rec={"canonical_url":original,"capture_timestamp":ts,"capture_at":cap,"coverage_date":cov,"mime":row.get("mime"),"status":str(row.get("status") or ""),"digest":digest,"length":length,"redirect":row.get("redirect"),"source_row":idx,"source_row_sha256":_sha(_json(row))}
            if row.get("redirect"):
                if not str(status).startswith("3"): raise WaybackError("redirect status invalid")
                target=_url(row["redirect"]); redirects[original]=target; rec["redirect"]=target
            records.append(rec); by_url.setdefault((original,cov),[]).append(rec)
        except (WaybackError,TypeError,ValueError) as exc: qa.append({"kind":"error","code":"invalid_row","row":idx,"reason":str(exc)})
    conflicts=set()
    for k,vals in by_url.items():
        by_time={}
        for v in vals: by_time.setdefault(v["capture_timestamp"],set()).add(v["digest"])
        for ts,ds in by_time.items():
            if len(ds)>1:
                conflicts.update(id(v) for v in vals if v["capture_timestamp"]==ts); qa.append({"kind":"error","code":"conflicting_capture","url":k[0],"coverage_date":k[1],"timestamp":ts})
        vals.sort(key=lambda x:x["capture_timestamp"])
        for old,new in zip(vals,vals[1:]):
            if old["digest"]!=new["digest"]: new["supersedes_capture_timestamp"]=old["capture_timestamp"]
    for src in list(redirects):
        seen_r=set(); cur=src
        while cur in redirects:
            if cur in seen_r: qa.append({"kind":"error","code":"redirect_cycle","url":src}); break
            seen_r.add(cur); cur=redirects[cur]
        if cur not in {r["canonical_url"] for r in records}: qa.append({"kind":"error","code":"redirect_target_missing","url":src})
    records=[r for r in records if id(r) not in conflicts]
    records.sort(key=lambda r:(r["canonical_url"],r["coverage_date"],r["capture_timestamp"],r["digest"]))
    qa.sort(key=lambda q:(q.get("code",""),q.get("url",""),q.get("timestamp",""),q.get("row",0)))
    if snapshot_bytes is None:
        for r in records: r["snapshot_validation_status"]="not_evaluable/missing_snapshot"
    metadata={"source_content_sha256":_sha(raw),"coverage_date":coverage_date,"public_release_allowed":False,"qa_error_count":sum(q["kind"]=="error" for q in qa),"record_count":len(records)}
    if not records:
        metadata.update({"validation_status":"not_evaluable","reason_codes":["missing_wayback_capture"],"required_inputs":["Wayback CDX capture rows"]}); qa.append({"kind":"warning","code":"missing_wayback_capture"})
    if snapshot_bytes is not None:
        metadata["snapshot_sha256"]=_sha(snapshot_bytes); metadata["snapshot_bytes"]=len(snapshot_bytes)
    return {"records":records,"qa":qa,"metadata":metadata}

def write_wayback_bundle(result, output_dir, *, acknowledge_internal_use=False, replace=False):
    if not acknowledge_internal_use: raise WaybackError("internal-use acknowledgement required")
    out=Path(output_dir); out.mkdir(parents=True,exist_ok=True); targets={"metadata":out/"metadata.json","records":out/"records.jsonl","qa":out/"qa.jsonl"}
    if not replace and any(p.exists() for p in targets.values()): raise WaybackError("output exists")
    staged=[]; backups=[]; committed=[]
    try:
        for key,p in targets.items():
            fd,n=tempfile.mkstemp(prefix="."+p.name+".",dir=out); os.close(fd); t=Path(n); staged.append((p,t))
            with t.open("w",encoding="utf8") as f:
                if key=="metadata": f.write(json.dumps(result["metadata"],ensure_ascii=False,indent=2)+"\n")
                else:
                    for row in result[key]: f.write(json.dumps(row,ensure_ascii=False,separators=(",",":"))+"\n")
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
            raise WaybackError("output commit failed") from exc
        warnings=[]
        for _,b in backups:
            try:
                if b.exists(): b.unlink()
            except OSError: warnings.append(b.name)
        if warnings:
            try:
                p=targets["metadata"]; m=json.loads(p.read_text(encoding="utf8")); m["cleanup_warnings"]=warnings; p.write_text(json.dumps(m,ensure_ascii=False)+"\n",encoding="utf8")
            except (OSError,ValueError): pass
    finally:
        for _,t in staged:
            if t.exists(): t.unlink()
    return {k:str(v) for k,v in targets.items()}
