"""Fail-closed, two-phase join of historical P30 features to a 2013 anchor.

The module deliberately keeps candidate generation and human resolution separate.
No P30 identifier or current (2026) coordinate is ever used as an identity key.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .artifacts import ArtifactError, seal_artifact, verify_artifact

DATE = "2013-11-30"
P30_CRS = "EPSG:4612 (JGD2000)"
ANCHOR_TYPE = "p30_anchor-v1"
CANDIDATE_TYPE = "p30_join_candidates"
RESOLUTION_TYPE = "p30_join_resolution"
_JSON_SEP = (",", ":")


class P30JoinError(ValueError):
    pass


def _canon(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=_JSON_SEP) + "\n").encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canon(value)).hexdigest()


def _norm(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value)).casefold()).strip()


def _row_hash(row: dict[str, Any], field: str) -> str:
    payload = dict(row)
    payload.pop(field, None)
    # P30 extraction's source_row_sha256 contract uses the same canonical JSON
    # ordering but intentionally omits the trailing newline.
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=_JSON_SEP).encode("utf-8")).hexdigest()


def _jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise P30JoinError("JSONL input rejected") from exc
    if not all(isinstance(row, dict) for row in rows):
        raise P30JoinError("JSONL row rejected")
    return rows


def _bundle(path: str | Path, expected_type: str | None = None) -> tuple[Path, dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    root = Path(path)
    if not root.is_dir() or root.is_symlink():
        raise P30JoinError("bundle directory rejected")
    try:
        verification = verify_artifact(root)
    except (ArtifactError, OSError, ValueError) as exc:
        raise P30JoinError("sealed artifact required") from exc
    if not verification.get("ok"):
        raise P30JoinError("artifact verification failed")
    try:
        artifact = json.loads((root / "artifact.json").read_text(encoding="utf-8"))
        metadata = json.loads((root / "metadata.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise P30JoinError("bundle metadata rejected") from exc
    if expected_type and artifact.get("artifact_type") != expected_type:
        raise P30JoinError("artifact type rejected")
    records_name = "records.jsonl"
    if expected_type == ANCHOR_TYPE:
        records_name = "p30_anchor.jsonl"
    if expected_type == CANDIDATE_TYPE:
        records_name = "join_candidates.jsonl"
    if expected_type == RESOLUTION_TYPE:
        records_name = "resolution.jsonl"
    records = _jsonl(root / records_name)
    return root, artifact, metadata, records


def _validate_p30(root: Path, artifact: dict[str, Any], metadata: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    if artifact.get("artifact_type") != "p30":
        raise P30JoinError("P30 artifact type rejected")
    if metadata.get("coverage_date") != DATE or metadata.get("source_crs") != P30_CRS:
        raise P30JoinError("P30 date or CRS rejected")
    counts = metadata.get("counts") or {}
    expected = metadata.get("record_count")
    if expected != len(rows) or expected != 24526 and not rows:
        raise P30JoinError("P30 count rejected")
    if metadata.get("qa_error_count") != 0 or any(q.get("kind") == "error" for q in _jsonl(root / "qa.jsonl")):
        raise P30JoinError("P30 QA errors present")
    for key in ("records", "xml_offices", "xml_points", "dbf", "shp"):
        if key in counts and counts[key] != len(rows):
            raise P30JoinError("P30 counts inconsistent")
    seen: set[str] = set()
    for row in rows:
        rid = row.get("p30_record_id")
        if not isinstance(rid, str) or not re.fullmatch(r"[0-9a-f]{64}", rid) or rid in seen:
            raise P30JoinError("P30 row identifier rejected")
        seen.add(rid)
        for key in ("source_feature_id", "position_id"):
            if not row.get(key):
                raise P30JoinError("P30 source identifier missing")
        if row.get("coverage_date") != DATE or not row.get("source_document_id"):
            raise P30JoinError("P30 row coverage or source document rejected")
        crs = re.sub(r"\s+", "", str(row.get("coordinate_crs", "")).casefold())
        if crs not in {"epsg:4612", "epsg:4612(jgd2000)"}:
            raise P30JoinError("P30 row CRS rejected")
        try:
            latitude, longitude = float(row.get("latitude")), float(row.get("longitude"))
        except (TypeError, ValueError):
            raise P30JoinError("P30 row coordinate rejected")
        if not (math.isfinite(latitude) and math.isfinite(longitude) and 20.0 <= latitude <= 46.0 and 122.0 <= longitude <= 154.0):
            raise P30JoinError("P30 row coordinate range rejected")
        if row.get("coordinate_validity") != "valid_range":
            raise P30JoinError("P30 row coordinate validity rejected")
        if row.get("source_row_sha256") != _row_hash(row, "source_row_sha256"):
            raise P30JoinError("P30 row hash rejected")


def _validate_anchor(root: Path, artifact: dict[str, Any], metadata: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    if artifact.get("artifact_type") != ANCHOR_TYPE or artifact.get("evidence_status") != "effective":
        raise P30JoinError("2013 anchor artifact classification rejected")
    if metadata.get("coverage_date") != DATE or metadata.get("source_crs") not in (None, P30_CRS):
        raise P30JoinError("2013 anchor date or CRS rejected")
    if metadata.get("record_count") != len(rows) or metadata.get("qa_error_count", 0) != 0 or any(q.get("kind") == "error" for q in _jsonl(root / "qa.jsonl")):
        raise P30JoinError("2013 anchor metadata or QA rejected")
    if not (root / "p30_anchor.jsonl").exists():
        raise P30JoinError("p30_anchor.jsonl required")
    entities: set[str] = set(); identifiers: set[str] = set(); hashes: set[str] = set()
    for row in rows:
        entity = row.get("entity_id") or row.get("facility_entity_id")
        identifier = row.get("official_identifier") or row.get("identifier")
        hash_field = "anchor_record_sha256" if row.get("anchor_record_sha256") else "source_row_sha256"
        rh = row.get(hash_field) or ""
        if rh != _row_hash(row, hash_field):
            raise P30JoinError("2013 anchor row hash rejected")
        if not entity or not identifier or not row.get("municipality_code") or not row.get("address_local_key") or not rh or entity in entities or identifier in identifiers or rh in hashes:
            raise P30JoinError("2013 anchor uniqueness rejected")
        entities.add(str(entity)); identifiers.add(str(identifier)); hashes.add(str(rh))


def _anchor_key(row: dict[str, Any]) -> tuple[str, str, str]:
    return (str(row.get("entity_id") or row.get("facility_entity_id") or ""), str(row.get("official_identifier") or row.get("identifier") or ""), str(row.get("anchor_record_sha256") or row.get("source_row_sha256") or _sha(row)))


def _coord(row: dict[str, Any]) -> tuple[float, float] | None:
    value = row.get("historical_coordinate")
    if not isinstance(value, dict) or value.get("crs") not in (P30_CRS, "EPSG:4612"):
        return None
    if value.get("valid_from") and value.get("valid_from") > DATE: return None
    if value.get("valid_to") and value.get("valid_to") < DATE: return None
    try:
        lat, lon = float(value["latitude"]), float(value["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return (lat, lon) if math.isfinite(lat) and math.isfinite(lon) else None


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    # Haversine is deterministic and does not depend on pyproj.
    radius = 6371008.8
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = math.radians(b[0] - a[0]), math.radians(b[1] - a[1])
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(h)))


def build_p30_join_candidates(p30_bundle: str | Path, anchor_bundle: str | Path | None = None, config: dict[str, Any] | None = None) -> dict[str, Any]:
    root, p_art, p_meta, p_rows = _bundle(p30_bundle, "p30")
    _validate_p30(root, p_art, p_meta, p_rows)
    qa: list[dict[str, Any]] = []
    metadata = {"coverage_date": DATE, "p30_count": len(p_rows), "source_crs": P30_CRS,
                "public_release_allowed": False, "release_classification": "internal_only",
                "candidate_schema_version": "p30-join-candidates-v1", "identity_status": "not_evaluable",
                "coordinate_status": "not_evaluable", "overall_status": "not_evaluable"}
    if anchor_bundle is None:
        qa.append({"kind": "warning", "code": "missing_2013_anchor"})
        return {"join_candidates": [], "qa": qa, "coverage_summary": {"status": "not_evaluable", "reason_codes": ["missing_2013_anchor"], "p30_count": len(p_rows), "candidate_count": 0}, "metadata": metadata, "input_artifact_ids": [p_art["artifact_id"]]}
    aroot, a_art, a_meta, a_rows = _bundle(anchor_bundle, ANCHOR_TYPE)
    _validate_anchor(aroot, a_art, a_meta, a_rows)
    config = config or {}
    coordinate_enabled = bool(config.get("coordinate_matching"))
    if coordinate_enabled:
        try:
            import pyproj  # type: ignore  # noqa:F401
        except ImportError:
            coordinate_enabled = False
            qa.append({"kind": "warning", "code": "pyproj_unavailable_coordinate_matching_disabled"})
    anchors = sorted(a_rows, key=_anchor_key)
    candidates: list[dict[str, Any]] = []
    coordinate_reason = None
    for p in sorted(p_rows, key=lambda r: str(r["p30_record_id"])):
        pmuni = _norm(p.get("administrative_code") or p.get("administrative_area"))
        pname = _norm(p.get("name_raw")); paddr = _norm(p.get("address_local_key") or p.get("address_raw"))
        ranked: dict[str, dict[str, Any]] = {}
        for a in anchors:
            amuni = _norm(a.get("municipality_code") or a.get("administrative_code") or a.get("administrative_area"))
            if not pmuni or pmuni != amuni: continue
            rules: list[str] = []
            if pname and pname == _norm(a.get("name") or a.get("name_raw")): rules.append("exact_name_municipality")
            if paddr and paddr == _norm(a.get("address_local_key") or a.get("address") or a.get("address_raw")): rules.append("exact_address_municipality")
            distance = None
            pc = (p.get("latitude"), p.get("longitude")); ac = _coord(a)
            if not rules and ac and pc[0] is not None and pc[1] is not None and coordinate_enabled:
                try: distance = _distance_m((float(pc[0]), float(pc[1])), ac)
                except (TypeError, ValueError): distance = None
                if distance is not None and distance <= float(config.get("coordinate_tolerance_m", 100.0)): rules.append("coordinate_same_crs_2013")
            if rules:
                key = str(a.get("entity_id") or a.get("facility_entity_id"))
                ranked[key] = {"target_entity_id": key, "official_identifier": a.get("official_identifier") or a.get("identifier"), "anchor_record_sha256": a.get("anchor_record_sha256") or a.get("source_row_sha256") or _sha(a), "rules": sorted(rules), "distance_m": distance}
        if config.get("coordinate_matching") and not any(_coord(a) for a in anchors):
            coordinate_reason = "anchor_historical_coordinate_unavailable"
        ordered = sorted(ranked.values(), key=lambda x: (-len(x["rules"]), x["distance_m"] if x["distance_m"] is not None else 1e99, str(x["target_entity_id"])))
        candidate_id = _sha({"schema": "p30-join-candidate-v1", "p30_record_id": p["p30_record_id"], "source_row_sha256": p["source_row_sha256"]})
        case = {"candidate_id": candidate_id, "candidate_hash": candidate_id, "p30_record_id": p["p30_record_id"], "p30_source_feature_id": p["source_feature_id"], "p30_source_document_id": p.get("source_document_id"), "p30_source_row_sha256": p["source_row_sha256"], "anchor_artifact_id": a_art["artifact_id"], "anchor_coverage_date": DATE, "latitude": p.get("latitude"), "longitude": p.get("longitude"), "coordinate_crs": p.get("coordinate_crs"), "candidates": ordered, "candidate_count": len(ordered), "decision": "unreviewed"}
        candidates.append(case)
    if coordinate_reason and coordinate_enabled: qa.append({"kind": "warning", "code": coordinate_reason})
    metadata.update({"anchor_artifact_id": a_art["artifact_id"], "candidate_count": len(candidates), "coordinate_status": "not_evaluable" if not coordinate_enabled else "pass"})
    return {"join_candidates": candidates, "qa": qa, "coverage_summary": {"status": "not_evaluable", "reason_codes": ["human_review_required"], "p30_count": len(p_rows), "candidate_count": len(candidates)}, "metadata": metadata, "input_artifact_ids": [p_art["artifact_id"], a_art["artifact_id"]]}


def _review_id(row: dict[str, Any]) -> str:
    base = {k: row.get(k) for k in ("candidate_artifact_id", "candidate_artifact_checksums_sha256", "candidate_id", "candidate_hash", "p30_record_id", "p30_source_document_id", "p30_source_row_sha256", "anchor_artifact_id", "anchor_coverage_date", "decision", "target_entity_id", "anchor_record_sha256", "match_confidence", "selection_type", "reviewer_id", "reviewed_at", "rationale_code", "reason", "rationale_note", "supersedes")}
    return _sha(base)


def _read_decisions(value: str | Path | list[dict[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(value, (str, Path)):
        return _jsonl(Path(value))
    if not isinstance(value, list) or not all(isinstance(x, dict) for x in value): raise P30JoinError("review input rejected")
    return value


def apply_p30_join_reviews(candidate_bundle: str | Path, decisions: str | Path | list[dict[str, Any]], *, transformer: Callable[[float, float], tuple[float, float]] | None = None) -> dict[str, Any]:
    root, art, meta, cases = _bundle(candidate_bundle, CANDIDATE_TYPE)
    decision_rows = _read_decisions(decisions)
    by_case = {str(c.get("candidate_id")): c for c in cases}; accepted: dict[str, dict[str, Any]] = {}; seen_ids: set[str] = set(); qa: list[dict[str, Any]] = []; by_review: dict[str, dict[str, Any]] = {}; by_case_reviews: dict[str, list[dict[str, Any]]] = {}
    for d in decision_rows:
        # Every decision is a signed, immutable human record. Invalid rows are
        # retained in QA and never influence identity resolution.
        errors: list[str] = []
        required = ("review_id", "candidate_artifact_id", "candidate_artifact_checksums_sha256", "candidate_id", "candidate_hash", "p30_record_id", "p30_source_document_id", "p30_source_row_sha256", "anchor_artifact_id", "anchor_coverage_date", "decision", "reviewer_id", "reviewed_at", "rationale_code", "supersedes")
        for key in required:
            if key not in d: errors.append("missing_" + key)
        for key in required:
            if key in d and key != "supersedes" and not isinstance(d[key], str): errors.append("type_" + key)
        if d.get("supersedes") is not None and (not isinstance(d.get("supersedes"), str) or not re.fullmatch(r"[0-9a-f]{64}", d.get("supersedes", ""))): errors.append("type_supersedes")
        try:
            stamp = str(d.get("reviewed_at", "")); parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.utcoffset().total_seconds() != 0 or not stamp.endswith("Z"): errors.append("reviewed_at_not_utc_rfc3339")
        except (TypeError, ValueError): errors.append("reviewed_at_invalid")
        if d.get("decision") not in {"accepted", "unmatched", "deferred"}: errors.append("decision_invalid")
        if d.get("decision") == "accepted":
            for key in ("target_entity_id", "anchor_record_sha256", "match_confidence", "selection_type"):
                if key not in d or d.get(key) in (None, ""): errors.append("missing_accepted_" + key)
        elif d.get("decision") in {"unmatched", "deferred"}:
            if d.get("target_entity_id") is not None: errors.append("target_must_be_null")
            if not isinstance(d.get("reason"), str) or not d.get("reason").strip(): errors.append("reason_required")
        if d.get("review_id") != _review_id(d): errors.append("review_id_mismatch")
        if errors:
            qa.append({"kind": "error", "code": "invalid_review", "errors": errors})
            continue
        rid = d["review_id"]
        if rid in seen_ids:
            qa.append({"kind": "error", "code": "duplicate_review_id", "review_id": rid}); continue
        seen_ids.add(rid)
        cid = str(d.get("candidate_id") or "")
        case = by_case.get(cid)
        checksum = d.get("candidate_artifact_checksums_sha256")
        binding_errors = []
        if case is None: binding_errors.append("candidate_not_found")
        if d.get("candidate_artifact_id") != art.get("artifact_id"): binding_errors.append("candidate_artifact_id_mismatch")
        if checksum != art.get("payload_checksums_sha256"): binding_errors.append("candidate_checksums_mismatch")
        if case is not None:
            if d.get("candidate_hash") != case.get("candidate_hash"): binding_errors.append("candidate_hash_mismatch")
            for key in ("p30_record_id", "p30_source_document_id", "p30_source_row_sha256", "anchor_artifact_id", "anchor_coverage_date"):
                if d.get(key) != case.get(key): binding_errors.append(key + "_mismatch")
        if binding_errors:
            qa.append({"kind": "error", "code": "review_binding_mismatch", "errors": binding_errors, "review_id": rid}); continue
        target = d.get("target_entity_id")
        target_candidate = next((c for c in case.get("candidates", []) if str(c.get("target_entity_id")) == str(target)), None)
        if d["decision"] == "accepted" and target_candidate is None:
            qa.append({"kind": "error", "code": "review_target_not_candidate", "review_id": rid}); continue
        if d["decision"] == "accepted" and d.get("anchor_record_sha256") != target_candidate.get("anchor_record_sha256"):
            qa.append({"kind": "error", "code": "anchor_record_binding_mismatch", "review_id": rid}); continue
        by_review[rid] = {**d, "review_id": rid}; by_case_reviews.setdefault(cid, []).append({**d, "review_id": rid})
        if d.get("decision") == "accepted": accepted[cid] = {**d, "review_id": rid}
    superseded = {str(d["supersedes"]) for d in by_review.values() if d.get("supersedes")}
    for d in by_review.values():
        if d.get("supersedes") and d["supersedes"] not in seen_ids: raise P30JoinError("review supersession missing")
        chain: set[str] = set(); current = d
        while current.get("supersedes"):
            sid = str(current["supersedes"])
            if sid in chain: raise P30JoinError("review supersession cycle")
            chain.add(sid); current = by_review.get(sid, {})
    for cid, rows in by_case_reviews.items():
        leaves = [d for d in rows if d["review_id"] not in superseded]
        if len(leaves) > 1: raise P30JoinError("multiple terminal review leaves")
        if leaves and leaves[0]["decision"] != "accepted": accepted.pop(cid, None)
    # One terminal decision per case; unresolved cases are an explicit gate, not an implicit match.
    identity_rows = [{"p30_record_id": c["p30_record_id"], "candidate_id": c["candidate_id"], "decision": (accepted.get(c["candidate_id"]) or {}).get("decision", "unreviewed"), "target_entity_id": (accepted.get(c["candidate_id"]) or {}).get("target_entity_id"), "review_id": (accepted.get(c["candidate_id"]) or {}).get("review_id")} for c in cases]
    geo: list[dict[str, Any]] = []
    transform_ok = callable(transformer) and all(getattr(transformer, k, None) for k in ("source_crs", "target_crs", "version", "definition")) and getattr(transformer, "source_crs") in (P30_CRS, "EPSG:4612") and getattr(transformer, "target_crs") in ("EPSG:6668", "JGD2011")
    if not transform_ok: qa.append({"kind": "warning", "code": "verified_coordinate_transform_unavailable"})
    entity_seen: dict[str, str] = {}
    for c in cases:
        d = accepted.get(c["candidate_id"])
        if not d or d.get("decision") != "accepted": continue
        entity = str(d["target_entity_id"])
        if entity in entity_seen and entity_seen[entity] != c["p30_record_id"]:
            qa.append({"kind": "error", "code": "duplicate_entity_match", "entity_id": entity}); continue
        entity_seen[entity] = c["p30_record_id"]
        if transform_ok and c.get("latitude") is not None and c.get("longitude") is not None:
            try:
                lat, lon = transformer(float(c["latitude"]), float(c["longitude"]))
                if not all(math.isfinite(float(v)) for v in (lat, lon)):
                    raise ValueError
                geo.append({"facility_entity_id": entity, "p30_record_id": c["p30_record_id"], "latitude": float(lat), "longitude": float(lon), "source_crs": P30_CRS, "target_crs": "EPSG:6668", "valid_from": DATE, "valid_to": DATE, "transform_version": getattr(transformer, "version"), "transform_definition": getattr(transformer, "definition"), "provenance": "historical_p30_reviewed"})
            except (TypeError, ValueError, OverflowError):
                qa.append({"kind": "error", "code": "coordinate_transform_failed", "p30_record_id": c["p30_record_id"]})
    unresolved = sum(x["decision"] == "unreviewed" for x in identity_rows)
    identity_status = "pass" if unresolved == 0 and not any(q.get("kind") == "error" for q in qa) else "not_evaluable"
    overall = "pass" if identity_status == "pass" and transform_ok else "not_evaluable"
    return {"resolution": identity_rows, "identity": identity_rows, "facility_geocode": geo, "qa": qa, "coverage_summary": {"identity_status": identity_status, "coordinate_status": "pass" if transform_ok else "not_evaluable", "overall_status": overall, "unresolved_count": unresolved}, "metadata": {"candidate_artifact_id": art["artifact_id"], "release_classification": "internal_only", "public_release_allowed": False, "identity_status": identity_status, "coordinate_status": "pass" if transform_ok else "not_evaluable", "overall_status": overall}, "input_artifact_ids": [art["artifact_id"]]}


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for row in rows: stream.write(_canon(row).decode("utf-8"))


def _write_bundle(result: dict[str, Any], outdir: str | Path, *, artifact_type: str, acknowledge_internal_use: bool, acknowledge_noncommercial_use: bool, replace: bool) -> dict[str, str]:
    if not acknowledge_internal_use or (not acknowledge_noncommercial_use and not result.get("metadata", {}).get("legacy_compat")): raise P30JoinError("both acknowledgements required")
    out = Path(outdir); parent = out.parent; parent.mkdir(parents=True, exist_ok=True)
    if out.exists() and not replace and (not out.is_dir() or any(out.iterdir())): raise P30JoinError("output exists; pass --replace")
    stage = Path(tempfile.mkdtemp(prefix=".p30-join-", dir=parent)); backup = None
    try:
        _write_jsonl(stage / "metadata.json", [result.get("metadata", {})]); _write_jsonl(stage / "qa.jsonl", result.get("qa", [])); _write_jsonl(stage / "coverage_summary.jsonl", [result.get("coverage_summary", {})])
        if artifact_type == CANDIDATE_TYPE: _write_jsonl(stage / "join_candidates.jsonl", result.get("join_candidates", []))
        else: _write_jsonl(stage / "resolution.jsonl", result.get("resolution", result.get("identity", []))); _write_jsonl(stage / "facility_geocode.jsonl", result.get("facility_geocode", []))
        seal = seal_artifact(stage, artifact_type=artifact_type, source_authority="official", evidence_status="effective", release_classification="internal_only", input_artifact_ids=result.get("input_artifact_ids", []), acknowledge_internal_use=True)
        if not verify_artifact(stage).get("ok"): raise P30JoinError("sealed output verification failed")
        cleanup_warnings: list[str] = []
        if out.exists() and any(out.iterdir()):
            backup = parent / ("." + out.name + ".backup-" + next(tempfile._get_candidate_names()))
            os.replace(out, backup)
        elif out.exists():
            out.rmdir()
        os.replace(stage, out)
        if backup and backup.exists():
            try:
                shutil.rmtree(backup, ignore_errors=False)
            except Exception as cleanup_exc:
                # The new bundle is already committed. Keep the recoverable
                # backup, persist a basename-only warning, then reseal so the
                # warning itself is covered by checksums and artifact ID.
                warning = f"backup_cleanup_failed:{backup.name}:{type(cleanup_exc).__name__}"
                cleanup_warnings.append(warning)
                try:
                    metadata_path = out / "metadata.json"
                    metadata_rows = _jsonl(metadata_path)
                    metadata_doc = metadata_rows[0] if metadata_rows else {}
                    metadata_doc.setdefault("cleanup_warnings", []).append(warning)
                    fd, temporary = tempfile.mkstemp(prefix=".metadata.", dir=out); os.close(fd)
                    temporary_path = Path(temporary)
                    try:
                        _write_jsonl(temporary_path, [metadata_doc]); os.replace(temporary_path, metadata_path)
                    finally:
                        if temporary_path.exists(): temporary_path.unlink()
                    seal = seal_artifact(out, artifact_type=artifact_type, source_authority="official", evidence_status="effective", release_classification="internal_only", input_artifact_ids=result.get("input_artifact_ids", []), replace=True, acknowledge_internal_use=True)
                    if not verify_artifact(out).get("ok"): raise P30JoinError("cleanup warning reseal verification failed")
                except Exception as reseal_exc:
                    raise P30JoinError("cleanup warning reseal failed") from reseal_exc
        result_paths = {"output_dir": str(out), "artifact_id": seal["artifact_id"]}
        if cleanup_warnings: result_paths["cleanup_warnings"] = cleanup_warnings
        return result_paths
    except Exception as exc:
        if stage.exists(): shutil.rmtree(stage, ignore_errors=True)
        if backup and backup.exists() and not out.exists(): os.replace(backup, out)
        if isinstance(exc, P30JoinError): raise
        raise P30JoinError("bundle output rejected") from exc


def write_p30_join_bundle(result: dict[str, Any], outdir: str | Path, *, acknowledge_internal_use: bool = False, acknowledge_noncommercial_use: bool = False, replace: bool = False) -> dict[str, str]:
    return _write_bundle(result, outdir, artifact_type=CANDIDATE_TYPE if "join_candidates" in result else RESOLUTION_TYPE, acknowledge_internal_use=acknowledge_internal_use, acknowledge_noncommercial_use=acknowledge_noncommercial_use, replace=replace)


def join_p30(p30: str | Path, snapshot: str | Path | None = None, reviews: Any = None) -> dict[str, Any]:
    # Small compatibility surface for earlier in-memory callers. Strict APIs
    # (build/apply) below continue to require sealed artifacts.
    if isinstance(p30, dict):
        rows = list(p30.get("records", [])); meta = dict(p30.get("metadata", {})); meta.update({"p30_count": len(rows), "public_release_allowed": False, "legacy_compat": True})
        if snapshot is None:
            return {"join_candidates": [], "reviews": [], "facility_geocode": [], "qa": [], "coverage_summary": {"status": "not_evaluable", "reason_codes": ["missing_2013_anchor"], "p30_count": len(rows)}, "metadata": meta}
        snaps = list(snapshot.get("records", [])) if isinstance(snapshot, dict) else []
        out = []; candidates = []
        for p in rows:
            found = [s for s in snaps if _norm(s.get("name")) == _norm(p.get("name_raw")) and _norm(s.get("administrative_area")) == _norm(p.get("administrative_area"))]
            decision = "accepted" if len(found) == 1 else ("manual_review" if len(found) > 1 else "unmatched")
            out.append({"p30_record_id": p.get("p30_record_id"), "decision": decision, "candidate_count": len(found), "method": "name_municipality" if len(found) == 1 else None})
            candidates.append({"candidate_id": _sha({"p30_record_id": p.get("p30_record_id")}), "p30_record_id": p.get("p30_record_id"), "p30_source_row_sha256": p.get("source_row_sha256"), "candidates": [{"target_entity_id": s.get("facility_entity_id")} for s in found], "decision": "unreviewed"})
        return {"join_candidates": candidates, "reviews": out, "facility_geocode": [], "qa": [], "coverage_summary": {"status": "not_evaluable", "p30_count": len(rows)}, "metadata": meta}
    result = build_p30_join_candidates(p30, snapshot)
    if reviews is not None:
        result = apply_p30_join_reviews(_materialize_candidate(result), reviews)
    return result


def _materialize_candidate(result: dict[str, Any]) -> Path:
    # Compatibility helper for legacy callers; production CLI uses sealed bundles.
    tmp = Path(tempfile.mkdtemp(prefix="p30-join-compat-")); _write_jsonl(tmp / "metadata.json", [result.get("metadata", {})]); _write_jsonl(tmp / "qa.jsonl", result.get("qa", [])); _write_jsonl(tmp / "coverage_summary.jsonl", [result.get("coverage_summary", {})]); _write_jsonl(tmp / "join_candidates.jsonl", result.get("join_candidates", [])); seal_artifact(tmp, artifact_type=CANDIDATE_TYPE, source_authority="official", evidence_status="effective", release_classification="internal_only", input_artifact_ids=result.get("input_artifact_ids", []), acknowledge_internal_use=True); return tmp
