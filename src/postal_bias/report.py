"""Conservative final quality/legal gate report (descriptive, not legal advice)."""
from __future__ import annotations
import json, tempfile, os
from pathlib import Path
from typing import Any
from .artifacts import verify_artifact, seal_artifact

class ReportError(ValueError): pass

def build_final_quality_report(artifacts: dict[str,str|Path], gates: dict[str,Any]|None=None)->dict[str,Any]:
    raw_gates=gates or {}; gates={str(k).casefold():v for k,v in raw_gates.items()}; checks={}; reasons=[]; artifact_ids=[]
    for name,path in artifacts.items():
        try: checks[name]=verify_artifact(path,strict_code=True)
        except Exception: checks[name]={"ok":False,"errors":["verification_failed"]}
        if not checks[name].get("ok"): reasons.append(name+":verification_failed")
        else:
            try:
                root=Path(path); art=json.loads((root/"artifact.json").read_text(encoding="utf8")); artifact_ids.append(art.get("artifact_id")); cov=root/"coverage_summary.jsonl"; status=None; cr=[]
                if cov.exists():
                    row=json.loads(cov.read_text(encoding="utf8").splitlines()[0]); status=row.get("status") or row.get("overall_status"); cr=row.get("reason_codes",[])
                checks[name]={"ok":True,"artifact_id":art.get("artifact_id"),"artifact_type":art.get("artifact_type"),"source_authority":art.get("source_authority"),"release_classification":art.get("release_classification"),"status":status,"reason_codes":cr}
                if status=="not_evaluable": reasons.append(name+":not_evaluable:"+",".join(sorted(map(str,cr))))
                elif status=="fail": reasons.append(name+":fail")
            except Exception: reasons.append(name+":coverage_unreadable")
    for key,val in gates.items():
        checks[key]=val if isinstance(val,dict) else {"status":str(val)}
        if isinstance(val,dict) and val.get("status") not in ("pass",None): reasons.append(key+":"+str(val.get("status")))
    mandatory=("source_coverage","current","history","653","v1","v2","v3","p30_join","population","grid","analysis","visualization")
    for gate in mandatory:
        if gate not in checks and gate not in gates: reasons.append(gate+":not_evaluable:missing_gate")
    status="fail" if any(":fail" in x or "verification_failed" in x for x in reasons) else ("pass" if checks and not reasons else "not_evaluable")
    return {"status":status,"checks":checks,"artifact_ids":sorted(set(x for x in artifact_ids if x)),"missing_inputs":sorted(set(reasons)),"next_actions":["supply missing/verified local artifacts and legal evidence" ] if reasons else [],"legal_screening_status":"descriptive_only" if status=="pass" else "not_evaluable","legal_disclaimer":"This report does not determine legal compliance or absence of geographic bias.","source_authority":"mixed; see checks","release_classification":"internal_only","public_release_allowed":False}

def write_report_bundle(report:dict[str,Any],outdir:str|Path,*,acknowledge_internal_use=False,replace=False):
    if not acknowledge_internal_use: raise ReportError("internal-use acknowledgement required")
    out=Path(outdir); parent=out.parent; parent.mkdir(parents=True,exist_ok=True)
    if out.exists() and any(out.iterdir()) and not replace: raise ReportError("output exists")
    stage=Path(tempfile.mkdtemp(prefix=".report-",dir=parent))
    try:
        (stage/"final_quality_report.json").write_text(json.dumps(report,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf8")
        (stage/"final_quality_report.md").write_text("# Final quality report\n\nStatus: **"+str(report.get("status"))+"**\n\n"+str(report.get("legal_disclaimer"))+"\n",encoding="utf8")
        seal=seal_artifact(stage,artifact_type="postal_bias_final_report",source_authority="auxiliary",evidence_status="effective",release_classification="internal_only",input_artifact_ids=report.get("artifact_ids",[]),acknowledge_internal_use=True)
        if not verify_artifact(stage,strict_code=True).get("ok"): raise ReportError("report staging verification failed")
        backup=None
        if out.exists() and any(out.iterdir()): backup=parent/("."+out.name+".backup"); os.replace(out,backup)
        elif out.exists(): out.rmdir()
        os.replace(stage,out)
        if not verify_artifact(out,strict_code=True).get("ok"):
            try:
                import shutil
                if out.exists(): shutil.rmtree(out,ignore_errors=False)
                if backup and backup.exists(): os.replace(backup,out); backup=None
            except Exception as rollback_exc: raise ReportError("report rollback failed") from rollback_exc
            raise ReportError("report verification failed")
        warnings=[]
        if backup and backup.exists():
            try: import shutil; shutil.rmtree(backup,ignore_errors=False); backup=None
            except Exception as cleanup_exc: warnings.append(f"backup_cleanup_failed:{backup.name}:{type(cleanup_exc).__name__}")
        result={"output_dir":str(out),"artifact_id":seal["artifact_id"]}
        if warnings: result["cleanup_warnings"]=warnings
        return result
    except Exception as exc:
        if stage.exists(): import shutil; shutil.rmtree(stage,ignore_errors=True)
        if backup is not None and backup.exists() and not out.exists(): os.replace(backup,out)
        raise ReportError("report output rejected") from exc
