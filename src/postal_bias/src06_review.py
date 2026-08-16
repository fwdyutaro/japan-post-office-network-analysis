"""Fail-closed local review, promotion and validation ledgers for SRC-06."""
from __future__ import annotations
import hashlib, json, math, os, tempfile, re
from datetime import date
from pathlib import Path
from typing import Any

DECISIONS = {"accepted", "rejected", "deferred", "duplicate", "superseded"}

class Src06ReviewError(ValueError): pass

def _rows(value: Any, key: str | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(value, (str, Path)):
        p = Path(value); meta = {}
        if p.is_dir():
            if (p / "metadata.json").exists(): meta = json.loads((p / "metadata.json").read_text(encoding="utf-8"))
            names = {"candidates":"match_candidates.jsonl", "active_reviews":"active_reviews.jsonl", "events":"events.jsonl", "current_closed_observations":"current_closed_observations.jsonl", "official_identifier_history":"official_identifier_history.jsonl", "promoted_rows":"promoted_events.jsonl"}
            path = p / names.get(key or "", "records.jsonl")
        else: path = p
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()], meta
    if isinstance(value, list): return value, {}
    selected = value.get(key) if key and key in value else value.get("rows", [])
    return list(selected or []), value.get("metadata", {})

def _canonical(v: Any) -> bytes: return (json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode()
def _sha(v: Any) -> str: return hashlib.sha256(_canonical(v)).hexdigest()
def _raw(v: Any) -> str: return v.get("raw", "") if isinstance(v, dict) else str(v or "")
def _id_hash(row: dict[str, Any]) -> str: return row.get("candidate_id") or row.get("event_id") or row.get("observation_id") or _sha(row)

def _review_digest(row: dict[str, Any]) -> str:
    # Identity is derived from the decision payload, never caller-controlled.
    payload = {k: v for k, v in row.items() if k not in {"review_id", "candidate_sha256"}}
    return _sha(payload)

def apply_src06_review(candidates: Any, decisions: Any, *, candidate_artifact_id: str | None = None, replace: bool = False) -> dict[str, Any]:
    cands, cmeta = _rows(candidates, "candidates"); revs, _ = _rows(decisions, "reviews")
    id_counts={r.get("candidate_id"):sum(x.get("candidate_id")==r.get("candidate_id") for x in cands) for r in cands if r.get("candidate_id")}
    duplicate_ids={k for k,v in id_counts.items() if v>1}
    by_id = {r.get("candidate_id"): r for r in cands if r.get("candidate_id") and r.get("candidate_id") not in duplicate_ids}
    qa=[{"kind":"error","code":"duplicate_candidate_id","candidate_id":k} for k in sorted(duplicate_ids)]; seen={}; superseded=set(); valid=[]
    for r in revs:
        expected_rid = _review_digest(r)
        rid=r.get("review_id") or expected_rid
        if r.get("review_id") is not None and (not isinstance(r.get("review_id"), str) or r.get("review_id") != expected_rid):
            qa.append({"kind":"error","code":"invalid_review_id","review_id":r.get("review_id")}); continue
        cid=r.get("candidate_id"); decision=r.get("decision")
        if rid in seen: qa.append({"kind":"error","code":"duplicate_review_id","review_id":rid}); continue
        seen[rid]=r
        if cid in duplicate_ids:
            qa.append({"kind":"error","code":"duplicate_candidate_id","candidate_id":cid,"review_id":rid}); continue
        if decision not in DECISIONS or cid not in by_id: qa.append({"kind":"error","code":"review_candidate_mismatch","review_id":rid}); continue
        cand=by_id[cid]
        expected_artifact = candidate_artifact_id or cmeta.get("artifact_id")
        expected = {"candidate_artifact_id": expected_artifact or cand.get("candidate_artifact_id"),
                    "candidate_artifact_sha256": cand.get("candidate_artifact_sha256") or cmeta.get("candidate_artifact_sha256") or _sha(cand),
                    "candidate_sha256": _sha(cand),
                    "source_document_id": cand.get("source_document_id") or cmeta.get("source_document_id"),
                    "source_content_sha256": cand.get("source_content_sha256") or cmeta.get("source_content_sha256"),
                    "anchor_source_document_id": cand.get("anchor_source_document_id") or cmeta.get("anchor_source_document_id"),
                    "anchor_source_content_sha256": cand.get("anchor_source_content_sha256") or cmeta.get("anchor_source_content_sha256") or cmeta.get("source_pdf_sha256"),
                    "source_row_id": cand.get("source_row_id"), "target_official_identifier": cand.get("target_official_identifier"),
                    "target_line": cand.get("target_line"), "target_line_sha256": cand.get("target_line_sha256")}
        missing=[k for k,v in expected.items() if v in (None, "")]
        if missing: qa.append({"kind":"error","code":"review_binding_missing","review_id":rid,"fields":missing}); continue
        if any(r.get(k) != v for k,v in expected.items()): qa.append({"kind":"error","code":"review_binding_mismatch","review_id":rid}); continue
        else:
            row={**r,"review_id":rid,**expected}
            valid.append(row)
    graph={r["review_id"]:r.get("supersedes_review_id") for r in valid if r.get("supersedes_review_id")}
    def cycle(x, trail):
        if x in trail: return True
        return bool(graph.get(x) and cycle(graph[x], trail|{x}))
    for r in valid:
        parent=r.get("supersedes_review_id")
        if parent and (not any(v["review_id"] == parent for v in valid) or cycle(r["review_id"], set())): qa.append({"kind":"error","code":"review_chain_cycle_or_missing","review_id":r["review_id"]})
        if parent: superseded.add(parent)
    active=[r for r in valid if r["review_id"] not in superseded and not any(q.get("review_id")==r["review_id"] and q.get("kind")=="error" for q in qa)]
    duplicate_accepts=set()
    for cid in {r["candidate_id"] for r in active}:
        if sum(r["decision"]=="accepted" for r in active if r["candidate_id"]==cid)>1:
            qa.append({"kind":"error","code":"multiple_active_accepted","candidate_id":cid}); duplicate_accepts.add(cid)
    if duplicate_accepts: active=[r for r in active if r["candidate_id"] not in duplicate_accepts]
    metadata={"source_family":"SRC-06","public_release_allowed":False,"active_count":len(active),"qa_error_count":sum(q.get("kind")=="error" for q in qa),"candidate_artifact_id":candidate_artifact_id or cmeta.get("artifact_id"),"source_document_id":cmeta.get("source_document_id"),"source_content_sha256":cmeta.get("source_content_sha256"),"anchor_source_document_id":cmeta.get("anchor_source_document_id"),"anchor_source_content_sha256":cmeta.get("anchor_source_content_sha256") or cmeta.get("source_pdf_sha256")}
    return {"active_reviews": sorted(active,key=lambda r:r["review_id"]), "review_qa": qa, "metadata":metadata}

def promote_src06(active: Any, candidates: Any, source: Any, history: Any, *, disabled_source_families: set[str] | None = None) -> dict[str, Any]:
    reviews,_=_rows(active,"active_reviews"); cands,_=_rows(candidates,"candidates"); events, smeta=_rows(source,"events"); obs, _=_rows(source,"current_closed_observations"); hist,_=_rows(history,"official_identifier_history")
    cmap={r.get("candidate_id"):r for r in cands}; hmap={}
    for r in hist:
        ident=r.get("official_identifier") or r.get("identifier")
        if ident: hmap.setdefault(str(ident),[]).append(r.get("facility_entity_id"))
    all_source=events+obs; source_ids=[_id_hash(r) for r in all_source]; duplicate_source_ids={x for x in source_ids if source_ids.count(x)>1}
    source_rows={_id_hash(r):r for r in all_source}; out_events=[]; out_obs=[]; qa=[]; disabled=disabled_source_families or set(); excluded_disabled=0
    for review in reviews:
        if review.get("decision")!="accepted": continue
        cand=cmap.get(review.get("candidate_id")); target=str((cand or {}).get("target_official_identifier") or "")
        ids=sorted({str(x) for x in hmap.get(target,[]) if x})
        if len(ids)!=1: qa.append({"kind":"error","code":"unresolvable_target_entity","candidate_id":review.get("candidate_id")}); continue
        src=source_rows.get(review.get("source_row_id"));
        if not src:
            qa.append({"kind":"error","code":"source_row_not_found","candidate_id":review.get("candidate_id")}); continue
        if review.get("source_row_id") in duplicate_source_ids:
            qa.append({"kind":"error","code":"duplicate_source_row_id","candidate_id":review.get("candidate_id"),"source_row_id":review.get("source_row_id")}); continue
        if src.get("source_family") in disabled:
            excluded_disabled += 1; continue
        raw_date = src.get("observed_effective_date") if src.get("event_id") else src.get("closure_start_date")
        try: date.fromisoformat(str(raw_date))
        except (TypeError, ValueError):
            qa.append({"kind":"error","code":"source_date_invalid","candidate_id":review.get("candidate_id")}); continue
        if (not src.get("event_id") and not src.get("observation_id") or not src.get("source_document_id") or
                not src.get("source_line_sha256") and not src.get("source_row_sha256") or
                src.get("source_family") != "SRC-06" or src.get("provenance") != "unofficial" or
                src.get("event_status") != "announced" or any(src.get(k) not in (None, "") for k in ("confirmed_effective_date", "planned_effective_date", "notice_date"))):
            qa.append({"kind":"error","code":"source_status_conflict","candidate_id":review.get("candidate_id")}); continue
        promoted={**src,"facility_entity_id":ids[0],"source_family":"SRC-06","provenance":"unofficial","event_status":"announced","review_status":"accepted","identity_review_id":review.get("review_id"),"confirmed_effective_date":None}
        (out_events if src.get("event_id") else out_obs).append(promoted)
    return {"promoted_events":out_events,"promoted_observations":out_obs,"promotion_qa":qa,"metadata":{"source_family":"SRC-06","public_release_allowed":False,"promoted_count":len(out_events)+len(out_obs),"excluded_disabled_count":excluded_disabled,"qa_error_count":sum(q.get("kind")=="error" for q in qa),"source_document_id":smeta.get("source_document_id"),"source_content_sha256":smeta.get("source_content_sha256")}}

def reconcile_long_suspensions(anchor: Any, promoted: Any, *, as_of: str, tolerance_days: int=31) -> dict[str, Any]:
    anchor_rows, meta=_rows(anchor,"records")
    if isinstance(promoted, dict): rows=list(promoted.get("promoted_events",[]))+list(promoted.get("promoted_observations",[])); pmeta=promoted.get("metadata",{})
    else: rows,_=_rows(promoted,"promoted_rows"); pmeta={}
    note=meta.get("note_count",meta.get("unlisted_long_term_suspended_simple_post_offices")); classifications=[]
    if isinstance(tolerance_days, bool) or not isinstance(tolerance_days, int) or tolerance_days < 0:
        return {"gate":"not_evaluable","reason":"invalid_tolerance","classifications":[],"synthetic_653":False,"review_qa":[{"kind":"error","code":"invalid_tolerance"}]}
    try: asof=date.fromisoformat(as_of)
    except (TypeError, ValueError): return {"gate":"not_evaluable","reason":"invalid_as_of","classifications":[],"synthetic_653":False}
    if isinstance(note,bool) or not isinstance(note,int) or note < 0: return {"gate":"not_evaluable","reason":"note_count_invalid","classifications":[],"review_qa":[{"kind":"error","code":"note_count_invalid"}],"synthetic_653":False}
    seen=set(); unlisted=set(); stale=future=unresolved=conflict=0; listed_ids={str(r.get("facility_entity_id")) for r in anchor_rows if r.get("facility_entity_id")}
    for r in rows:
        eid=r.get("facility_entity_id"); raw=r.get("observed_effective_date") or r.get("closure_start_date")
        if (r.get("source_family") != "SRC-06" or r.get("provenance") != "unofficial" or
                r.get("event_status") != "announced" or r.get("review_status") != "accepted" or not r.get("identity_review_id")):
            classifications.append({"facility_entity_id":eid,"classification":"invalid_promoted_row"}); conflict += 1; seen.add(eid); continue
        try: d=date.fromisoformat(str(raw)[:10])
        except (TypeError, ValueError): d=None
        if not eid: cls="unresolved"; unresolved+=1
        elif eid in seen: cls="duplicate"; conflict+=1
        elif d is None: cls="invalid_temporal"; conflict+=1
        elif d and d>asof: cls="future"; future+=1
        elif d and (asof-d).days>tolerance_days: cls="stale"; stale+=1
        elif r.get("event_type")=="reopened" or r.get("operating_status")=="operating": cls="reopened"
        elif r.get("event_type") in {"terminated","abolished"}: cls="terminated"
        elif eid in listed_ids: cls="listed"
        else: cls="unlisted"; unlisted.add(eid)
        classifications.append({"facility_entity_id":eid,"classification":cls}); seen.add(eid)
    gate="pass" if len(unlisted)==note and not any((stale,future,unresolved,conflict)) else "fail"
    return {"gate":gate,"reason":"count_and_freshness_check" if gate=="pass" else "count_or_quality_failure","classifications":classifications,"note_count":note,"distinct_unlisted_closed_count":len(unlisted),"as_of":as_of,"tolerance_days":tolerance_days,"synthetic_653":False,"source_document_id":pmeta.get("source_document_id")}

def _id_gate(a: Any, b: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not isinstance(a, (list, tuple, set)) or not isinstance(b, (list, tuple, set)):
        return {"status":"not_evaluable"}, [{"kind":"error","code":"invalid_id_input"}]
    try:
        if any(isinstance(x, (dict, list, set, tuple)) for x in list(a)+list(b)): raise TypeError
        aa={str(x) for x in a}; bb={str(x) for x in b}; rate=len(aa^bb)/max(len(aa),len(bb),1)
    except (TypeError, ValueError): return {"status":"not_evaluable"}, [{"kind":"error","code":"invalid_id_input"}]
    return {"status":"pass" if rate<=.02 else "fail","symmetric_difference_rate":rate}, []

def validate_src06(*, v1_official_ids=None, v1_src06_ids=None, v2_official_ids=None, v2_src06_ids=None, v3_rows=None, total_event_population: int|None=None, min_sample: int|None=None) -> dict[str, Any]:
    result={"v1":{},"v2":{},"v3":{}}
    qa=[]
    if v1_official_ids is None or v1_src06_ids is None: result["v1"]={"status":"not_evaluable"}
    else:
        result["v1"], errors = _id_gate(v1_official_ids, v1_src06_ids); qa.extend(errors)
    if v2_official_ids is None or v2_src06_ids is None: result["v2"]={"status":"not_evaluable"}
    else:
        result["v2"], errors = _id_gate(v2_official_ids, v2_src06_ids); qa.extend(errors)
    if total_event_population is None or isinstance(total_event_population,bool) or not isinstance(total_event_population,int) or total_event_population < 0:
        result["v3"]={"status":"not_evaluable","sample_size":len(v3_rows or []) if isinstance(v3_rows,list) else 0}; qa.append({"kind":"error","code":"population_missing_or_invalid"})
    else:
        n=len(v3_rows or [])
        if (not isinstance(v3_rows,list) or any(not isinstance(r,dict) for r in v3_rows) or
                any(("missing" in r and not isinstance(r.get("missing"), bool)) or ("date_mismatch" in r and not isinstance(r.get("date_mismatch"), bool)) for r in (v3_rows if isinstance(v3_rows,list) else []))):
            result["v3"]={"status":"not_evaluable","sample_size":n}; qa.append({"kind":"error","code":"invalid_v3_rows"})
        else:
            required=min(total_event_population,max(200,math.ceil(.05*total_event_population)))
            if total_event_population <= 0: result["v3"]={"status":"not_evaluable","sample_size":n,"required":0}; qa.append({"kind":"error","code":"population_empty"})
            elif n<required: result["v3"]={"status":"not_evaluable","sample_size":n,"required":required}
            else:
                missing=sum(bool(r.get("missing")) for r in v3_rows); mismatch=sum(bool(r.get("date_mismatch")) for r in v3_rows); result["v3"]={"status":"pass" if missing/n<=.01 and mismatch/n<=.02 else "fail","sample_size":n,"required":required,"population":total_event_population,"missing_rate":missing/n,"date_mismatch_rate":mismatch/n}
    statuses=[x["status"] for x in result.values()]; overall="fail" if "fail" in statuses else ("not_evaluable" if "not_evaluable" in statuses else "pass"); result["status"]=overall; result["qa"]=qa; return result

def write_review_bundle(result: dict[str, Any], output_dir: str|Path, *, acknowledge_internal_use=False, replace=False) -> dict[str,str]:
    if not acknowledge_internal_use: raise Src06ReviewError("internal-use acknowledgement required")
    out=Path(output_dir); out.mkdir(parents=True,exist_ok=True)
    files={k:out/f"{k}.jsonl" for k,v in result.items() if k != "metadata" and isinstance(v,list)}
    scalar={k:v for k,v in result.items() if k != "metadata" and k not in files}
    if scalar: files["reconciliation"]=out/"reconciliation.jsonl"
    files["metadata"]=out/"metadata.json"
    if not replace and any(p.exists() for p in files.values()): raise Src06ReviewError("output exists; pass --replace")
    staged=[]; backups=[]; committed=[]
    try:
        for key,p in files.items():
            fd,n=tempfile.mkstemp(prefix=f".{p.name}.",dir=out); os.close(fd); t=Path(n); staged.append((p,t));
            with t.open("w",encoding="utf-8",newline="\n") as f:
                val=[result["metadata"]] if key=="metadata" else ([scalar] if key=="reconciliation" else result[key])
                for row in val: f.write(json.dumps(row,ensure_ascii=False,separators=(",",":"))+"\n")
        try:
            for p in files.values():
                if p.exists():
                    fd,n=tempfile.mkstemp(prefix=f".{p.name}.backup.",dir=out); os.close(fd); b=Path(n); b.unlink(); os.replace(p,b); backups.append((p,b))
            for p,t in staged: os.replace(t,p); committed.append(p)
        except Exception as exc:
            for p in committed:
                if p.exists(): p.unlink()
            for p,b in reversed(backups):
                if b.exists(): os.replace(b,p)
            raise Src06ReviewError("output commit failed") from exc
        cleanup_warnings=[]
        for _,b in backups:
            try:
                if b.exists(): b.unlink()
            except OSError as exc:
                cleanup_warnings.append({"path":b.name,"error":type(exc).__name__})
        if cleanup_warnings:
            # Commit is already durable; expose cleanup status without turning a
            # successful bundle into an exception or leaking local paths.
            try:
                meta_path=files.get("metadata")
                if meta_path and meta_path.exists():
                    meta=json.loads(meta_path.read_text(encoding="utf-8")); meta["cleanup_warnings"]=cleanup_warnings
                    tmp=meta_path.with_name("."+meta_path.name+".cleanup")
                    tmp.write_text(json.dumps(meta,ensure_ascii=False,separators=(",", ":"))+"\n",encoding="utf-8"); os.replace(tmp,meta_path)
            except (OSError, ValueError, json.JSONDecodeError):
                pass
    finally:
        for _,t in staged:
            if t.exists(): t.unlink()
    return {k:str(v) for k,v in files.items()}
