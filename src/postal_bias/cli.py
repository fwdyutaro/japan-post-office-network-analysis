from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import sys
from pathlib import Path

from .config import AcquisitionConfig
from .discovery import discover_file
from .fetch import Fetcher
from .manifest import Manifest
from .p30 import inspect_p30
from .policy import PolicyViolation, SitePolicy
from .current_list import (PROFILES, CurrentListError, parse_layout_text,
                           run_pdftotext, write_outputs)
from .change_pdf import ChangePdfError, parse_change_text, write_change_outputs
from .history import (HistoryError, build_history, effective_date, monthly_snapshots,
                      validate_snapshot_cutoff)
from .confirm import (ConfirmError, confirm_events, load_change_events,
                      load_sealed_anchor, write_confirm_bundle)
from .inukugi import Src06Error, parse_src06_file, write_src06_outputs
from .matching import MatchingError, match_src06, write_match_outputs
from .p30_extract import P30ExtractError, parse_p30, write_p30_outputs
from .artifacts import ArtifactError, seal_artifact, verify_artifact
from .src06_review import (Src06ReviewError, apply_src06_review, promote_src06,
                           reconcile_long_suspensions, validate_src06, write_review_bundle)
from .wayback import WaybackError, parse_wayback_cdx, write_wayback_bundle
from .datasets import DatasetError, register_dataset, validate_dataset, write_dataset_bundle
from .p30_join import (P30JoinError, join_p30, write_p30_join_bundle,
                        build_p30_join_candidates, apply_p30_join_reviews)
from .analysis import AnalysisError, analyze_monthly, write_analysis_bundle
from .visualization import VisualizationError, render_visualization, write_visualization_bundle
from .research_outputs import ResearchOutputError, package_research_outputs
from .accessibility_animation import (AccessibilityAnimationError,
                                      build_accessibility_animation,
                                      write_accessibility_animation)

INDEX_URL = "https://www.post.japanpost.jp/newsrelease/storeinformation/"


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--base-dir", default=".")
    parser.add_argument("--contact", help="research contact (required for live acquisition)")
    parser.add_argument("--user-agent", default=None)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="postal-bias")
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("inspect-p30")
    q.add_argument("zip_path")
    q = sub.add_parser("parse-p30", help="extract records from a local P30-13 ZIP")
    q.add_argument("zip_path")
    q.add_argument("--coverage-date", required=True)
    q.add_argument("--output-dir", required=True)
    q.add_argument("--acknowledge-noncommercial-use", action="store_true")
    q.add_argument("--acknowledge-internal-use", action="store_true")
    q.add_argument("--replace", action="store_true")
    q = sub.add_parser("seal-artifact", help="seal a local artifact directory")
    q.add_argument("directory")
    q.add_argument("--artifact-type", required=True)
    q.add_argument("--source-authority", required=True)
    q.add_argument("--evidence-status", required=True)
    q.add_argument("--release-classification", required=True)
    q.add_argument("--input-artifact-id", action="append", default=[])
    q.add_argument("--qa-file", action="append", default=[],
                   help="relative path of a QA payload file; repeatable.  Omit to aggregate "
                        "every *qa*.jsonl in the bundle.  An explicit list must name them all.")
    q.add_argument("--config")
    q.add_argument("--acknowledge-internal-use", action="store_true")
    q.add_argument("--acknowledge-public-release-reviewed", action="store_true")
    q.add_argument("--replace", action="store_true")
    q = sub.add_parser("verify-artifact", help="verify a local artifact bundle")
    q.add_argument("directory")
    q.add_argument("--strict-code", action="store_true")
    q.add_argument("--strict-config")
    q = sub.add_parser("apply-src06-review"); q.add_argument("--candidates", required=True); q.add_argument("--decisions", required=True); q.add_argument("--candidate-artifact-id"); q.add_argument("--output-dir", required=True); q.add_argument("--acknowledge-internal-use", action="store_true"); q.add_argument("--replace", action="store_true")
    q = sub.add_parser("promote-src06"); q.add_argument("--active-reviews", required=True); q.add_argument("--candidates", required=True); q.add_argument("--source", required=True); q.add_argument("--history", required=True); q.add_argument("--output-dir", required=True); q.add_argument("--disable-source-family", action="append", default=[]); q.add_argument("--acknowledge-internal-use", action="store_true"); q.add_argument("--replace", action="store_true")
    q = sub.add_parser("reconcile-long-suspensions"); q.add_argument("--anchor", required=True); q.add_argument("--promoted", required=True); q.add_argument("--as-of", required=True); q.add_argument("--tolerance", type=int, default=31); q.add_argument("--output-dir", required=True); q.add_argument("--acknowledge-internal-use", action="store_true"); q.add_argument("--replace", action="store_true")
    q = sub.add_parser("validate-src06"); q.add_argument("--v1-official"); q.add_argument("--v1-src06"); q.add_argument("--v2-official"); q.add_argument("--v2-src06"); q.add_argument("--v3"); q.add_argument("--v3-population", type=int)
    q = sub.add_parser("parse-wayback-cdx"); q.add_argument("input"); q.add_argument("--coverage-date"); q.add_argument("--output-dir",required=True); q.add_argument("--acknowledge-internal-use",action="store_true"); q.add_argument("--replace",action="store_true")
    q = sub.add_parser("register-dataset"); q.add_argument("descriptor"); q.add_argument("--registry"); q.add_argument("--output-dir",required=True); q.add_argument("--acknowledge-internal-use",action="store_true"); q.add_argument("--replace",action="store_true")
    q = sub.add_parser("validate-dataset"); q.add_argument("descriptor")
    q = sub.add_parser("join-p30"); q.add_argument("p30"); q.add_argument("--snapshot"); q.add_argument("--output-dir",required=True); q.add_argument("--acknowledge-internal-use",action="store_true"); q.add_argument("--acknowledge-noncommercial-use",action="store_true"); q.add_argument("--replace",action="store_true")
    q = sub.add_parser("apply-p30-review"); q.add_argument("--candidates",required=True); q.add_argument("--decisions",required=True); q.add_argument("--output-dir",required=True); q.add_argument("--acknowledge-internal-use",action="store_true"); q.add_argument("--acknowledge-noncommercial-use",action="store_true"); q.add_argument("--replace",action="store_true")
    q = sub.add_parser("analyze-monthly"); q.add_argument("--snapshots",required=True); q.add_argument("--grid",required=True); q.add_argument("--population",required=True); q.add_argument("--config",required=True); q.add_argument("--output-dir",required=True); q.add_argument("--acknowledge-internal-use",action="store_true"); q.add_argument("--replace",action="store_true")
    q = sub.add_parser("render-visualization"); q.add_argument("--analysis",required=True); q.add_argument("--grid",required=True); q.add_argument("--config",required=True); q.add_argument("--output-dir",required=True); q.add_argument("--acknowledge-internal-use",action="store_true"); q.add_argument("--replace",action="store_true")
    q = sub.add_parser("render-accessibility-animation"); q.add_argument("--recalculation",required=True); q.add_argument("--output-dir",required=True); q.add_argument("--acknowledge-internal-use",action="store_true"); q.add_argument("--replace",action="store_true")
    q = sub.add_parser(
        "seal-research-outputs",
        help="atomically seal the local research report, map and animation",
    )
    q.add_argument("--work-dir", required=True)
    q.add_argument("--output-dir", required=True)
    q.add_argument("--input-artifact", action="append", required=True)
    q.add_argument("--unresolved-input", action="append", default=[])
    q.add_argument("--acknowledge-internal-use", action="store_true")
    q.add_argument("--replace", action="store_true")
    q = sub.add_parser("discover-index")
    q.add_argument("html_path")
    q.add_argument("--base-url", default=INDEX_URL)
    q = sub.add_parser("fetch-index")
    _common(q)
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--acknowledge-site-terms", action="store_true")
    q = sub.add_parser("backfill")
    _common(q)
    q.add_argument("--index", help="local HTML; live mode otherwise fetches the official index")
    q.add_argument("--limit", type=int, default=1)
    q.add_argument("--dry-run", action="store_true")
    q.add_argument("--acknowledge-site-terms", action="store_true")
    q = sub.add_parser("inspect-current-list", help="inspect a locally supplied current-list PDF")
    q.add_argument("pdf_path")
    q.add_argument("--coverage-date", required=True)
    q.add_argument("--pdftotext")
    q = sub.add_parser("parse-current-list", help="parse a locally supplied current-list PDF")
    q.add_argument("pdf_path")
    q.add_argument("--coverage-date", required=True)
    q.add_argument("--output-dir", required=True)
    q.add_argument("--pdftotext")
    q.add_argument("--profile", choices=sorted(PROFILES))
    q.add_argument("--acknowledge-internal-use", action="store_true")
    q.add_argument("--replace", action="store_true")
    q = sub.add_parser("build-history", help="build an internal history ledger from local JSON outputs")
    q.add_argument("--anchor", required=True)
    q.add_argument("--events", required=True)
    q.add_argument("--coverage-date", required=True)
    q.add_argument("--output-dir", required=True)
    q.add_argument("--acknowledge-internal-use", action="store_true")
    q.add_argument("--replace", action="store_true")
    q.add_argument("--series", choices=("formal", "effective_official", "effective_with_unofficial"), default="formal")
    q.add_argument("--disable-source", action="append", default=[])
    q.add_argument("--disable-source-family", action="append", default=[])
    q = sub.add_parser("confirm-events", help="promote announced change events that the evidence supports")
    q.add_argument("--events", action="append", required=True,
                   help="events.jsonl file, or a directory scanned for */events.jsonl")
    q.add_argument("--anchor", required=True, help="current-list records.jsonl")
    q.add_argument("--anchor-date")
    q.add_argument("--date-observations", action="append", default=[],
                   help="JSONL of later observations that fix an event's effective date")
    q.add_argument("--exclude-source-date", action="append", default=[])
    q.add_argument("--output-dir", required=True)
    q.add_argument("--acknowledge-internal-use", action="store_true")
    q.add_argument("--replace", action="store_true")
    q = sub.add_parser("parse-src06", help="parse a local SRC-06 HTML file")
    q.add_argument("html_path"); q.add_argument("--source-kind", choices=("type6", "type7", "current_closed"), required=True)
    q.add_argument("--retrieved-at", required=True); q.add_argument("--output-dir", required=True)
    q.add_argument("--acknowledge-unofficial-source", action="store_true"); q.add_argument("--acknowledge-internal-use", action="store_true"); q.add_argument("--replace", action="store_true")
    q = sub.add_parser("match-src06", help="match local SRC-06 rows to a current-list anchor")
    q.add_argument("--anchor", required=True); q.add_argument("--src06-dir", required=True); q.add_argument("--output-dir", required=True); q.add_argument("--acknowledge-internal-use", action="store_true"); q.add_argument("--replace", action="store_true")
    q = sub.add_parser("parse-change", help="parse a locally supplied monthly change PDF")
    q.add_argument("pdf_path")
    q.add_argument("--coverage-date", required=True,
                   help="ISO date in the document's name; the bundle partition key")
    q.add_argument("--document-published-date",
                   help="ISO date the notice was published (spec 6.1 published_at); "
                        "defaults to --coverage-date")
    q.add_argument("--output-dir", required=True)
    q.add_argument("--pdftotext")
    q.add_argument("--acknowledge-internal-use", action="store_true")
    q.add_argument("--replace", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "inspect-p30":
        print(json.dumps(inspect_p30(args.zip_path), ensure_ascii=False, indent=2))
        return 0
    if args.command == "parse-p30":
        try:
            result = parse_p30(args.zip_path, coverage_date=args.coverage_date)
            paths = write_p30_outputs(result, args.output_dir, replace=args.replace,
                                       acknowledge_noncommercial_use=args.acknowledge_noncommercial_use,
                                       acknowledge_internal_use=args.acknowledge_internal_use)
            print(json.dumps({"paths": paths, "record_count": result["metadata"]["record_count"],
                              "qa_error_count": result["metadata"]["qa_error_count"],
                              "public_release_allowed": False}, ensure_ascii=False, indent=2))
            return 0
        except (P30ExtractError, OSError, ValueError, KeyError):
            print(json.dumps({"error": "P30 input or output rejected"}, ensure_ascii=False), file=sys.stderr)
            return 2
    if args.command == "seal-artifact":
        try:
            result = seal_artifact(args.directory, artifact_type=args.artifact_type,
                                   source_authority=args.source_authority, evidence_status=args.evidence_status,
                                   release_classification=args.release_classification,
                                   input_artifact_ids=args.input_artifact_id, config_path=args.config,
                                   qa_files=args.qa_file or None,
                                   replace=args.replace, acknowledge_internal_use=args.acknowledge_internal_use,
                                   acknowledge_public_release_reviewed=args.acknowledge_public_release_reviewed)
            print(json.dumps(result, ensure_ascii=False, indent=2)); return 0
        except (ArtifactError, OSError, ValueError):
            print(json.dumps({"error": "artifact input or output rejected"}, ensure_ascii=False), file=sys.stderr); return 2
    if args.command == "verify-artifact":
        try:
            result = verify_artifact(args.directory, strict_code=args.strict_code, strict_config=args.strict_config)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get("ok") else 2
        except (ArtifactError, OSError, ValueError, json.JSONDecodeError):
            print(json.dumps({"error": "artifact verification rejected"}, ensure_ascii=False), file=sys.stderr); return 2
    if args.command in {"apply-src06-review", "promote-src06", "reconcile-long-suspensions", "validate-src06"}:
        def _read_local(path):
            p = Path(path)
            if p.is_dir():
                meta = json.loads((p / "metadata.json").read_text(encoding="utf-8")) if (p / "metadata.json").exists() else {}
                names = {"candidates":"match_candidates.jsonl", "active_reviews":"active_reviews.jsonl", "source":"events.jsonl", "history":"official_identifier_history.jsonl", "promoted":"promoted_events.jsonl", "anchor":"records.jsonl"}
                result = {"metadata": meta}
                for key, name in names.items():
                    f = p / name
                    if f.exists(): result[key if key not in {"source","history","promoted","anchor"} else ({"source":"events","history":"official_identifier_history","promoted":"promoted_events","anchor":"records"}[key])] = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
                obs = p / "current_closed_observations.jsonl"
                if obs.exists(): result["current_closed_observations"] = [json.loads(x) for x in obs.read_text(encoding="utf-8").splitlines() if x.strip()]
                return result
            if not p.is_file(): raise Src06ReviewError("input rejected")
            if p.suffix.lower() == ".jsonl":
                rows = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
                meta = p.parent / "metadata.json"
                return {"rows": rows, "metadata": json.loads(meta.read_text(encoding="utf-8"))} if meta.exists() else rows
            return json.loads(p.read_text(encoding="utf-8"))
        try:
            if args.command == "apply-src06-review":
                if not args.acknowledge_internal_use: raise Src06ReviewError("acknowledgement required")
                result = apply_src06_review(_read_local(args.candidates), _read_local(args.decisions), candidate_artifact_id=args.candidate_artifact_id)
                paths = write_review_bundle(result, args.output_dir, acknowledge_internal_use=True, replace=args.replace)
                print(json.dumps({"paths": paths, "active_count": result["metadata"]["active_count"], "qa_error_count": result["metadata"]["qa_error_count"], "public_release_allowed": False}, ensure_ascii=False)); return 0
            if args.command == "promote-src06":
                if not args.acknowledge_internal_use: raise Src06ReviewError("acknowledgement required")
                result = promote_src06(_read_local(args.active_reviews), _read_local(args.candidates), _read_local(args.source), _read_local(args.history), disabled_source_families=set(args.disable_source_family))
                paths = write_review_bundle(result, args.output_dir, acknowledge_internal_use=True, replace=args.replace)
                print(json.dumps({"paths": paths, "promoted_count": result["metadata"]["promoted_count"], "qa_error_count": result["metadata"]["qa_error_count"], "public_release_allowed": False}, ensure_ascii=False)); return 0
            if args.command == "reconcile-long-suspensions":
                if not args.acknowledge_internal_use: raise Src06ReviewError("acknowledgement required")
                result = reconcile_long_suspensions(_read_local(args.anchor), _read_local(args.promoted), as_of=args.as_of, tolerance_days=args.tolerance)
                result["metadata"] = {"source_family":"SRC-06", "public_release_allowed":False, "gate":result.get("gate"), "as_of":args.as_of}
                paths = write_review_bundle(result, args.output_dir, acknowledge_internal_use=True, replace=args.replace)
                print(json.dumps({"paths": paths, "gate": result["gate"], "public_release_allowed": False}, ensure_ascii=False)); return 0
            def _ids(path):
                if not path: return None
                value = _read_local(path)
                if isinstance(value, dict): value = value.get("rows", value.get("ids", []))
                return value
            result = validate_src06(v1_official_ids=_ids(args.v1_official), v1_src06_ids=_ids(args.v1_src06), v2_official_ids=_ids(args.v2_official), v2_src06_ids=_ids(args.v2_src06), v3_rows=_ids(args.v3), total_event_population=args.v3_population)
            print(json.dumps(result, ensure_ascii=False, indent=2)); return {"pass":0,"fail":4,"not_evaluable":3}[result["status"]]
        except (Src06ReviewError, OSError, ValueError, KeyError, json.JSONDecodeError, TypeError):
            print(json.dumps({"error": "SRC-06 review input or output rejected"}, ensure_ascii=False), file=sys.stderr); return 2
    if args.command == "parse-wayback-cdx":
        try:
            r=parse_wayback_cdx(args.input,coverage_date=args.coverage_date); paths=write_wayback_bundle(r,args.output_dir,acknowledge_internal_use=args.acknowledge_internal_use,replace=args.replace); print(json.dumps({"paths":paths,"record_count":len(r["records"]),"qa_error_count":r["metadata"]["qa_error_count"]},ensure_ascii=False)); return 3 if r["metadata"].get("validation_status")=="not_evaluable" else 0
        except (WaybackError,OSError,ValueError,json.JSONDecodeError): print(json.dumps({"error":"Wayback input or output rejected"}),file=sys.stderr); return 2
    if args.command in {"register-dataset","validate-dataset"}:
        try:
            d=json.loads(Path(args.descriptor).read_text(encoding="utf8"));
            if args.command=="validate-dataset": validate_dataset(d); print(json.dumps({"status":"valid","public_release_allowed":d.get("public_release_allowed",False)})); return 0
            r=register_dataset(d,args.registry); paths=write_dataset_bundle(r,args.output_dir,acknowledge_internal_use=args.acknowledge_internal_use,replace=args.replace); print(json.dumps({"paths":paths,"dataset_count":len(r["datasets"])},ensure_ascii=False)); return 0
        except (DatasetError,OSError,ValueError,json.JSONDecodeError): print(json.dumps({"error":"dataset descriptor rejected"}),file=sys.stderr); return 2
    if args.command == "join-p30":
        try:
            r=build_p30_join_candidates(args.p30,args.snapshot); paths=write_p30_join_bundle(r,args.output_dir,acknowledge_internal_use=args.acknowledge_internal_use,acknowledge_noncommercial_use=args.acknowledge_noncommercial_use,replace=args.replace); print(json.dumps({"paths":paths,"coverage_status":r["coverage_summary"]["status"]},ensure_ascii=False)); return 3 if r["coverage_summary"]["status"]=="not_evaluable" else 0
        except (P30JoinError,OSError,ValueError,json.JSONDecodeError): print(json.dumps({"error":"P30 join input or output rejected"}),file=sys.stderr); return 2
    if args.command == "apply-p30-review":
        try:
            if not args.acknowledge_internal_use or not args.acknowledge_noncommercial_use:
                raise P30JoinError("acknowledgements required")
            r=apply_p30_join_reviews(args.candidates,args.decisions); paths=write_p30_join_bundle(r,args.output_dir,acknowledge_internal_use=True,acknowledge_noncommercial_use=True,replace=args.replace); print(json.dumps({"paths":paths,"coverage_status":r["coverage_summary"]["overall_status"]},ensure_ascii=False)); return 3 if r["coverage_summary"]["overall_status"]=="not_evaluable" else (4 if any(q.get("kind")=="error" for q in r.get("qa",[])) else 0)
        except (P30JoinError,OSError,ValueError,json.JSONDecodeError): print(json.dumps({"error":"P30 review input or output rejected"}),file=sys.stderr); return 2
    if args.command == "analyze-monthly":
        try:
            cfg=json.loads(Path(args.config).read_text(encoding="utf8")); r=analyze_monthly(args.snapshots,args.grid,args.population,cfg); paths=write_analysis_bundle(r,args.output_dir,acknowledge_internal_use=args.acknowledge_internal_use,replace=args.replace); print(json.dumps({"paths":paths,"coverage_status":r["coverage_summary"]["status"]},ensure_ascii=False)); return 3 if r["coverage_summary"]["status"]=="not_evaluable" else 0
        except (AnalysisError,OSError,ValueError,json.JSONDecodeError): print(json.dumps({"error":"analysis input or output rejected"}),file=sys.stderr); return 2
    if args.command == "render-visualization":
        try:
            cfg=json.loads(Path(args.config).read_text(encoding="utf8")); r=render_visualization(args.analysis,args.grid,cfg); paths=write_visualization_bundle(r,args.output_dir,acknowledge_internal_use=args.acknowledge_internal_use,replace=args.replace); print(json.dumps({"paths":paths,"coverage_status":r["coverage_summary"]["status"]},ensure_ascii=False)); return 3 if r["coverage_summary"]["status"]=="not_evaluable" else 0
        except (VisualizationError,OSError,ValueError,json.JSONDecodeError): print(json.dumps({"error":"visualization input or output rejected"}),file=sys.stderr); return 2
    if args.command == "render-accessibility-animation":
        try:
            result = build_accessibility_animation(args.recalculation)
            paths = write_accessibility_animation(
                result, args.recalculation, args.output_dir,
                acknowledge_internal_use=args.acknowledge_internal_use,
                replace=args.replace)
            print(json.dumps({"paths": paths, "coverage_status": result["status"]},
                             ensure_ascii=False))
            return 3 if result["status"] == "not_evaluable" else 0
        except (AccessibilityAnimationError, ArtifactError, OSError, ValueError,
                KeyError, json.JSONDecodeError):
            print(json.dumps({"error": "accessibility animation input or output rejected"},
                             ensure_ascii=False), file=sys.stderr)
            return 2
    if args.command == "seal-research-outputs":
        try:
            result = package_research_outputs(
                args.work_dir, args.output_dir, args.input_artifact,
                unresolved_inputs=args.unresolved_input,
                acknowledge_internal_use=args.acknowledge_internal_use,
                replace=args.replace,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 3 if result.get("research_status") == "not_evaluable" else 0
        except (ResearchOutputError, ArtifactError, OSError, ValueError,
                KeyError, json.JSONDecodeError):
            print(json.dumps({"error": "research output input or output rejected"},
                             ensure_ascii=False), file=sys.stderr)
            return 2
    if args.command == "discover-index":
        print(json.dumps(discover_file(args.html_path, args.base_url), ensure_ascii=False, indent=2))
        return 0
    if args.command in {"inspect-current-list", "parse-current-list"}:
        try:
            text, version = run_pdftotext(args.pdf_path, args.pdftotext)
            profile = PROFILES.get(args.profile) if getattr(args, "profile", None) else None
            result = parse_layout_text(text, expected_profile=profile)
            if args.command == "inspect-current-list":
                errors = [q for q in result["qa"] if q.get("kind") == "error"]
                summary = {
                    "coverage_date": args.coverage_date,
                    "pdftotext_version": version,
                    "counts": result["metadata"]["counts"],
                    "qa_error_count": len(errors),
                    "qa_count": len(result["qa"]),
                    "qa_codes": sorted({q.get("code") for q in result["qa"]}),
                    "terms_review_status": "unverified_detail_pdf",
                    "public_release_allowed": False,
                }
                print(json.dumps(summary, ensure_ascii=False, indent=2))
                return 0
            paths = write_outputs(result, args.pdf_path, args.coverage_date, args.output_dir,
                                  replace=args.replace,
                                  acknowledge_internal_use=args.acknowledge_internal_use,
                                  pdftotext_version=version, expected_profile=profile)
            print(json.dumps({"paths": paths, "counts": result["metadata"]["counts"],
                              "qa_error_count": result["metadata"]["qa_error_count"],
                              "public_release_allowed": False}, ensure_ascii=False, indent=2))
            return 0
        except CurrentListError:
            # Do not expose local PDF/output paths or a traceback in a CLI
            # summary; detailed diagnostics remain available to the caller API.
            print(json.dumps({"error": "current-list input or output rejected"}, ensure_ascii=False), file=sys.stderr)
            return 2
    if args.command == "parse-change":
        try:
            text, version = run_pdftotext(args.pdf_path, args.pdftotext)
            digest = hashlib.sha256(Path(args.pdf_path).read_bytes()).hexdigest()
            published = args.document_published_date or args.coverage_date
            result = parse_change_text(text, source_pdf_sha256=digest, source_document_date=published)
            paths = write_change_outputs(result, args.pdf_path, args.coverage_date, args.output_dir,
                                         replace=args.replace, acknowledge_internal_use=args.acknowledge_internal_use,
                                         document_published_date=published)
            meta_path = Path(paths["metadata"])
            written_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            print(json.dumps({"paths": paths, "event_count": result["metadata"]["event_count"],
                              "qa_error_count": written_meta["qa_error_count"],
                              "coverage_date": written_meta["coverage_date"],
                              "document_published_date": written_meta["document_published_date"],
                              "change_effective_date_from": written_meta["change_effective_date_from"],
                              "change_effective_date_to": written_meta["change_effective_date_to"],
                              "public_release_allowed": False}, ensure_ascii=False, indent=2))
            return 0
        except (ChangePdfError, CurrentListError):
            print(json.dumps({"error": "change-PDF input or output rejected"}, ensure_ascii=False), file=sys.stderr)
            return 2
    if args.command == "confirm-events":
        try:
            if not args.acknowledge_internal_use:
                raise ConfirmError("internal-use acknowledgement required")
            anchor_path = Path(args.anchor)
            if not anchor_path.is_file():
                raise ConfirmError("anchor input rejected")
            # Read the anchor out of the sealed bundle rather than from whatever
            # path was typed, so the records corroborated against are the ones
            # the artifact ID covers.  A ``--anchor`` pointing somewhere else in
            # the bundle is rejected by the binding check in validate_anchor.
            sealed = load_sealed_anchor(anchor_path.parent)
            records, anchor_meta = sealed["records"], sealed["metadata"]
            events, sources = load_change_events(args.events, exclude_dates=args.exclude_source_date)
            observations = []
            for path in args.date_observations or []:
                p = Path(path)
                if not p.is_file():
                    raise ConfirmError("date-observation input rejected")
                observations.extend(json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip())
            result = confirm_events(events, {"records": records, "metadata": anchor_meta},
                                    anchor_date=args.anchor_date,
                                    anchor_dir=anchor_path.parent,
                                    date_observations=observations)
            result["metadata"]["source_documents"] = sources
            result["metadata"]["source_document_count"] = sum(1 for s in sources if s.get("included"))
            result["metadata"]["excluded_source_documents"] = [s for s in sources if not s.get("included")]
            paths = write_confirm_bundle(result, args.output_dir, replace=args.replace,
                                         acknowledge_internal_use=args.acknowledge_internal_use)
            summary = {k: result["metadata"][k] for k in
                       ("anchor_date", "input_event_count", "state_corroborated_event_count",
                        "effective_date_confirmed_event_count", "date_basis_counts",
                        "announced_event_count", "chain_count", "promoted_chain_count",
                        "invalid_planned_date_count",
                        "not_promoted_reason_counts", "qa_error_count")}
            print(json.dumps({"paths": paths, **summary, "public_release_allowed": False},
                             ensure_ascii=False, indent=2))
            return 0
        except ConfirmError as exc:
            # Precondition failures name the offending identifiers; they carry
            # no addresses and no local paths, so they are safe to print and
            # useless to withhold.
            print(json.dumps({"error": "confirmation input or output rejected",
                              "details": exc.details}, ensure_ascii=False), file=sys.stderr)
            return 2
        except (OSError, ValueError, json.JSONDecodeError):
            print(json.dumps({"error": "confirmation input or output rejected"}, ensure_ascii=False), file=sys.stderr)
            return 2
    if args.command == "build-history":
        try:
            if not args.acknowledge_internal_use:
                raise HistoryError("internal-use acknowledgement required")

            def read_json_input(path: str):
                p = Path(path)
                if not p.is_file():
                    raise HistoryError("input rejected")
                if p.suffix.lower() == ".jsonl":
                    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
                    metadata_path = p.parent / "metadata.json"
                    if metadata_path.exists():
                        return {"records" if p.name.startswith("records") else "events": rows,
                                "metadata": json.loads(metadata_path.read_text(encoding="utf-8"))}
                    return rows
                return json.loads(p.read_text(encoding="utf-8"))

            anchor = read_json_input(args.anchor)
            events = read_json_input(args.events)
            # ``--coverage-date`` accepts both ``YYYY-MM`` and ``YYYY-MM-DD``.
            # Passing the raw value as the anchor date left ``_day`` unable to
            # parse the month form, so the anchor date silently became None and
            # every historical entity lost its cutoff.  Use the normalized
            # month-end throughout, including in the metadata.
            cutoff = validate_snapshot_cutoff(args.coverage_date)
            history = build_history(anchor, events, anchor_date=cutoff,
                                    disabled_sources=args.disable_source, disabled_source_families=args.disable_source_family)
            snapshots = monthly_snapshots(history, [cutoff[:7]], series=args.series)
            out = Path(args.output_dir)
            targets = {"metadata": out / "metadata.json", "entities": out / "entities.jsonl",
                       "official_identifier_history": out / "official_identifier_history.jsonl",
                       "facility_state_history": out / "facility_state_history.jsonl",
                       "ledger": out / "ledger.jsonl", "snapshots": out / "snapshots.jsonl", "qa": out / "qa.jsonl"}
            if not args.replace and any(p.exists() for p in targets.values()):
                raise HistoryError("output exists; pass --replace")
            out.mkdir(parents=True, exist_ok=True)
            staged: list[tuple[str, Path]] = []
            payloads = {
                "metadata": {"history_version": history["history_version"], "anchor_date": cutoff,
                             "coverage_date_input": args.coverage_date,
                             "source_document_id": history["source_document_id"], "public_release_allowed": False,
                             "contains_detailed_addresses": True, "processing_display": "internal research only",
                             "event_count": len(history["ledger"]), "series": args.series, "snapshot_cutoff": cutoff,
                             "qa_error_count": history["qa_error_count"], "provenance_mix": sorted({e.get("event", {}).get("provenance", "official") for e in history["ledger"]}),
                             "effective_date_basis": history["effective_date_basis"],
                             "effective_date_basis_counts": history["effective_date_basis_counts"],
                             "effective_date_basis_note": (
                                 "Applied events whose date_basis is 'planned' are replayed on the "
                                 "planned_effective_date printed in the change notice.  Their state change "
                                 "is corroborated against the anchor cross-section, but the day itself is "
                                 "not independently observed; treat monthly placement of those changes as "
                                 "plan-based."),
                             "excluded_unconfirmed_event_count": sum(
                                 not effective_date(r["event"]) for r in history["ledger"]),
                             "excluded_counts_by_reason": {
                                 "unconfirmed_or_missing_date": sum(not effective_date(r["event"]) for r in history["ledger"]),
                                 "disabled_source": sum(bool(r.get("disabled")) for r in history["ledger"]),
                                 "superseded_or_cancelled": sum(bool(r.get("superseded_at")) for r in history["ledger"]),
                             }},
                "entities": history["entities"], "official_identifier_history": history["official_identifier_history"],
                "facility_state_history": history["facility_state_history"], "ledger": history["ledger"], "snapshots": snapshots, "qa": history["qa"],
            }
            try:
                for key, target in targets.items():
                    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=out)
                    os.close(fd); temp = Path(name); staged.append((key, temp))
                    with temp.open("w", encoding="utf-8", newline="\n") as stream:
                        value = payloads[key]
                        if key == "metadata":
                            json.dump(value, stream, ensure_ascii=False, indent=2); stream.write("\n")
                        else:
                            for row in value:
                                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
                backups: list[tuple[Path, Path]] = []
                committed: list[Path] = []
                try:
                    for target in targets.values():
                        if target.exists():
                            fd, name = tempfile.mkstemp(prefix=f".{target.name}.backup.", dir=out)
                            os.close(fd); backup = Path(name); backup.unlink()
                            os.replace(target, backup); backups.append((target, backup))
                    for key, temp in staged:
                        os.replace(temp, targets[key]); committed.append(targets[key])
                except Exception as exc:
                    for target in committed:
                        if target.exists(): target.unlink()
                    for target, backup in reversed(backups):
                        if backup.exists(): os.replace(backup, target)
                    raise HistoryError("history output commit failed") from exc
                else:
                    for _, backup in backups:
                        if backup.exists(): backup.unlink()
            finally:
                for _, temp in staged:
                    if temp.exists(): temp.unlink()
            print(json.dumps({"paths": {k: str(v) for k, v in targets.items()}, "event_count": len(history["ledger"]),
                              "excluded_unconfirmed_event_count": payloads["metadata"]["excluded_unconfirmed_event_count"],
                              "qa_error_count": history["qa_error_count"], "public_release_allowed": False}, ensure_ascii=False, indent=2))
            return 0
        except (HistoryError, OSError, ValueError, json.JSONDecodeError):
            print(json.dumps({"error": "history input or output rejected"}, ensure_ascii=False), file=sys.stderr)
            return 2
    if args.command == "parse-src06":
        try:
            result = parse_src06_file(args.html_path, args.source_kind, retrieved_at=args.retrieved_at)
            paths = write_src06_outputs(result, args.output_dir, replace=args.replace,
                                        acknowledge_unofficial_source=args.acknowledge_unofficial_source,
                                        acknowledge_internal_use=args.acknowledge_internal_use)
            print(json.dumps({"paths": paths, "event_count": result["metadata"]["event_count"], "observation_count": result["metadata"]["observation_count"], "qa_error_count": result["metadata"]["qa_error_count"], "public_release_allowed": False}, ensure_ascii=False, indent=2)); return 0
        except (Src06Error, OSError, ValueError, json.JSONDecodeError):
            print(json.dumps({"error": "SRC-06 input or output rejected"}, ensure_ascii=False), file=sys.stderr); return 2
    if args.command == "match-src06":
        try:
            anchor_path = Path(args.anchor)
            if not anchor_path.is_file(): raise MatchingError("input rejected")
            anchor = {"records": [json.loads(x) for x in anchor_path.read_text(encoding="utf8").splitlines() if x.strip()]}
            meta = anchor_path.parent / "metadata.json"
            if meta.exists(): anchor["metadata"] = json.loads(meta.read_text(encoding="utf8"))
            result = match_src06(anchor, args.src06_dir)
            paths = write_match_outputs(result, args.output_dir, replace=args.replace, acknowledge_internal_use=args.acknowledge_internal_use)
            print(json.dumps({"paths": paths, "candidate_count": result["metadata"]["candidate_count"], "review_count": result["metadata"]["review_count"], "qa_error_count": result["metadata"]["qa_error_count"], "public_release_allowed": False}, ensure_ascii=False, indent=2)); return 0
        except (MatchingError, OSError, ValueError, json.JSONDecodeError):
            print(json.dumps({"error": "SRC-06 matching input or output rejected"}, ensure_ascii=False), file=sys.stderr); return 2
    cfg = AcquisitionConfig(base_dir=Path(args.base_dir), contact=args.contact,
                            user_agent=args.user_agent or "postal-bias-research/0.1 (contact: configure-contact)")
    policy = SitePolicy(user_agent=cfg.user_agent)
    fetcher = Fetcher(cfg, policy=policy, manifest=Manifest(":memory:") if args.dry_run else None)
    try:
        if args.command == "fetch-index":
            if not args.dry_run and not fetcher.can_check_index(INDEX_URL):
                current = fetcher.manifest.get_current(INDEX_URL)
                if current and current["local_path"] and Path(current["local_path"]).exists():
                    print(json.dumps({"url": INDEX_URL, "status": "last-checked-gate", "network": False, "path": current["local_path"]}, ensure_ascii=False))
                else:
                    raise PolicyViolation("index gate active but no local cached HTML")
            else:
                result = fetcher.fetch(INDEX_URL, source_type="official_index", dry_run=args.dry_run,
                                       acknowledge_site_terms=args.acknowledge_site_terms)
                if result.get("path"):
                    docs = discover_file(result["path"])
                    current = next((d for d in docs if d["kind"] == "current_list"), None)
                    if current and result.get("id"):
                        fetcher.manifest.update_source_metadata(result["id"], published_at=current.get("date"), coverage_start=current.get("date"), coverage_end=current.get("date"))
                print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0
        # backfill: use local index where available; otherwise obtain it once (the same gate applies).
        if args.limit < 1 or args.limit > 3:
            raise PolicyViolation("backfill --limit must be 1..3")
        if args.index:
            docs = discover_file(args.index)
        else:
            if args.dry_run:
                docs = []
            else:
                if not fetcher.can_check_index(INDEX_URL):
                    current = fetcher.manifest.get_current(INDEX_URL)
                    if not current or not current["local_path"] or not Path(current["local_path"]).exists():
                        raise PolicyViolation("index gate active but no local cached HTML")
                    idx_path = current["local_path"]
                else:
                    idx = fetcher.fetch(INDEX_URL, source_type="official_index", acknowledge_site_terms=args.acknowledge_site_terms)
                    idx_path = idx["path"]
                docs = discover_file(idx_path)
        candidates = [d for d in docs if d["kind"] == "monthly_change"]
        new = [d for d in candidates if fetcher.manifest.get_current(d["url"]) is None]
        selected = new[:args.limit]
        results = []
        for doc in selected:
            results.append(fetcher.fetch(doc["url"], source_type="monthly_change", dry_run=args.dry_run,
                                         acknowledge_site_terms=args.acknowledge_site_terms,
                                         terms_url=INDEX_URL, published_at=doc.get("date"), coverage_start=doc.get("date"),
                                         coverage_end=doc.get("date"), notes=f"link_text={doc.get('link_text','')}"))
        print(json.dumps({"selected": selected, "results": results, "limit": args.limit}, ensure_ascii=False, indent=2))
        return 0
    finally:
        fetcher.manifest.close()


if __name__ == "__main__":
    raise SystemExit(main())
