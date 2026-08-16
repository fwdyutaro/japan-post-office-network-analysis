"""Candidate matching between a current-list anchor and local SRC-06 output."""
from __future__ import annotations
import hashlib, json, os, re, tempfile, unicodedata
from datetime import date
from pathlib import Path
from typing import Any

NORMALIZER_VERSION = "nfkc-casefold-space-hyphen-v1"
VALID_TYPES = {"temporarily_closed", "reopened"}
OFFICIAL_ID_RE = re.compile(r"^[0-9]{6}[a-zA-Z]?$")

class MatchingError(ValueError):
    pass

def normalize_text(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).casefold().replace("\u3000", " ")
    value = re.sub(r"[‐‑‒–—−－]", "-", value)
    return re.sub(r"\s+", " ", value).strip()

def _raw(state: dict[str, Any], key: str) -> str:
    v = state.get(key, "")
    return v.get("raw", "") if isinstance(v, dict) else str(v or "")

def _field_raw(row: dict[str, Any], key: str) -> str:
    """Read a current-list field while preserving its raw representation."""
    value = row.get(key, "")
    return value.get("raw", "") if isinstance(value, dict) else str(value or "")

def _official_id(value: Any) -> str:
    value = unicodedata.normalize("NFKC", str(value or "")).strip()
    return value.casefold() if OFFICIAL_ID_RE.fullmatch(value) else ""

def _rows(value: Any, key: str):
    if isinstance(value, list): return value, {}
    return list(value.get(key, [])), value.get("metadata", {})

def _source_rows(src: Any):
    if isinstance(src, (str, Path)):
        p = Path(src)
        meta = json.loads((p / "metadata.json").read_text(encoding="utf8"))
        events = [json.loads(x) for x in (p / "events.jsonl").read_text(encoding="utf8").splitlines() if x.strip()]
        obs = [json.loads(x) for x in (p / "current_closed_observations.jsonl").read_text(encoding="utf8").splitlines() if x.strip()]
        return events, obs, meta
    return list(src.get("events", [])), list(src.get("current_closed_observations", [])), src.get("metadata", {})

def _candidate_id(source_id: str, row_id: str, anchor_id: str) -> str:
    return hashlib.sha256(f"{source_id}|{row_id}|{anchor_id}".encode()).hexdigest()

def _valid_iso(value: Any) -> bool:
    try:
        date.fromisoformat(str(value or ""))
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(value)))
    except ValueError:
        return False

def _present(value: Any) -> bool:
    return value is not None and str(value).strip() != ""

def _review(row: dict[str, Any], source_doc: str, sid: str, decision: str, reason: str,
            count: int, conflicts: list[str]) -> dict[str, Any]:
    return {"source_row_id": sid, "decision": decision, "reason": reason,
            "candidate_count": count, "source_family": "SRC-06",
            "provenance": row.get("provenance", "unofficial"),
            "source_document_id": row.get("source_document_id") or source_doc,
            "source_page": row.get("source_page"), "source_line": row.get("source_line") or row.get("source_row_index"),
            "source_line_sha256": row.get("source_line_sha256") or row.get("source_row_sha256"),
            "normalizer_version": NORMALIZER_VERSION, "conflict_codes": conflicts}

def match_src06(anchor: Any, source: Any) -> dict[str, Any]:
    records, anchor_meta = _rows(anchor, "records")
    events, observations, src_meta = _source_rows(source)
    source_doc = src_meta.get("source_document_id")
    source_sha = src_meta.get("source_content_sha256")
    if (src_meta.get("source_family") != "SRC-06" or not source_doc or
            not isinstance(source_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", source_sha)):
        raise MatchingError("SRC-06 metadata rejected")
    qa, candidates, reviews = [], [], []
    raw_ids = {}
    for raw_row, raw_kind in [(r, "event") for r in events] + [(r, "observation") for r in observations]:
        raw_id = raw_row.get("event_id") if raw_kind == "event" else raw_row.get("observation_id")
        raw_id = raw_id or hashlib.sha256(json.dumps(raw_row, sort_keys=True).encode()).hexdigest()
        raw_ids[raw_id] = raw_ids.get(raw_id, 0) + 1
    duplicate_raw_ids = {k for k, v in raw_ids.items() if v > 1}
    by_id = {}
    valid_records = []
    for r in records:
        rid = _official_id(_field_raw(r, "official_identifier"))
        if not rid:
            qa.append({"kind": "error", "code": "invalid_anchor_official_identifier",
                       "source_page": r.get("source_page"), "source_line": r.get("source_line"),
                       "source_line_sha256": r.get("source_line_sha256")})
            continue
        valid_records.append(r)
        if rid:
            by_id.setdefault(rid, []).append(r)
    by_pair = {}
    for r in valid_records:
        name, address = normalize_text(_field_raw(r, "name")), normalize_text(_field_raw(r, "address"))
        if name and address: by_pair.setdefault((name, address), []).append(r)
    valid = []
    for row in events:
        row_id = row.get("event_id") or hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        if row_id in duplicate_raw_ids:
            qa.append({"kind": "error", "code": "duplicate_source_row_id", "source_row_id": row_id}); continue
        if (row.get("event_type") not in VALID_TYPES or row.get("source_family") != "SRC-06" or
                row.get("provenance") != "unofficial" or row.get("event_status") != "announced" or
                row.get("source_document_id") != source_doc or
                not _valid_iso(row.get("observed_effective_date")) or
                any(_present(row.get(k)) for k in ("confirmed_effective_date", "planned_effective_date", "notice_date"))):
            qa.append({"kind": "error", "code": "invalid_src06_event", "event_id": row.get("event_id")}); continue
        valid.append((row, "event"))
    for row in observations:
        row_id = row.get("observation_id") or hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        if row_id in duplicate_raw_ids:
            qa.append({"kind": "error", "code": "duplicate_source_row_id", "source_row_id": row_id}); continue
        if (row.get("is_currently_closed") is not True or row.get("source_family") != "SRC-06" or
                row.get("provenance") != "unofficial" or row.get("source_document_id") != source_doc or
                not _valid_iso(row.get("closure_start_date"))):
            qa.append({"kind": "error", "code": "invalid_current_closed_observation", "observation_id": row.get("observation_id")}); continue
        valid.append((row, "observation"))
    identified = []
    id_counts = {}
    for row, kind in valid:
        sid = row.get("event_id") or row.get("observation_id") or hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest()
        identified.append((row, kind, sid)); id_counts[sid] = id_counts.get(sid, 0) + 1
    unique_rows = []
    for row, kind, sid in identified:
        if id_counts[sid] > 1:
            qa.append({"kind": "error", "code": "duplicate_source_row_id", "source_row_id": sid})
        else:
            unique_rows.append((row, kind, sid))
    for row, kind, sid in unique_rows:
        state = row.get("after_state", {}) if kind == "event" else {}
        name = _raw(state, "name") or row.get("name_raw", "")
        address = _raw(state, "address") or row.get("address_raw", "")
        official_id_raw = _raw(state, "official_identifier")
        invalid_official_id = bool(official_id_raw and not _official_id(official_id_raw))
        if invalid_official_id:
            qa.append({"kind": "error", "code": "invalid_source_official_identifier", "source_row_id": sid})
            continue
        official_id = _official_id(official_id_raw)
        id_not_found = bool(official_id and official_id not in by_id)
        method, matches = "unmatched", []
        if official_id and official_id in by_id:
            matches, method = by_id[official_id], "official_identifier"
        elif normalize_text(name) and normalize_text(address):
            matches = by_pair.get((normalize_text(name), normalize_text(address)), [])
            method = "exact_name_address"
            if not matches:
                matches = [r for r in valid_records if normalize_text(_field_raw(r, "name")) == normalize_text(name)]
                method = "name_address_mismatch" if matches else "unmatched"
        elif normalize_text(name):
            matches, method = [r for r in valid_records if normalize_text(_field_raw(r, "name")) == normalize_text(name)], "exact_name_only"
        id_field_conflict = False
        if official_id and official_id in by_id and len(matches) == 1:
            target = matches[0]
            id_field_conflict = ((normalize_text(name) and normalize_text(_field_raw(target, "name")) and normalize_text(name) != normalize_text(_field_raw(target, "name"))) or
                                 (normalize_text(address) and normalize_text(_field_raw(target, "address")) and normalize_text(address) != normalize_text(_field_raw(target, "address"))))
        decision = "auto_accept" if len(matches) == 1 and method in {"official_identifier", "exact_name_address"} and not id_field_conflict and not id_not_found else ("manual_review" if matches else "unmatched")
        if len(matches) > 1: decision = "manual_review"
        if not normalize_text(address) and method != "official_identifier": decision = "manual_review" if matches else "unmatched"
        conflicts = []
        if method == "name_address_mismatch": conflicts.append("address_mismatch")
        if len(matches) > 1: conflicts.append("multiple_candidates")
        if id_not_found: conflicts.append("official_identifier_not_found")
        if id_field_conflict: conflicts.append("official_identifier_field_conflict")
        if decision == "manual_review": reviews.append(_review(row, source_doc, sid, decision, method, len(matches), conflicts))
        if not matches: reviews.append(_review(row, source_doc, sid, "unmatched", "no_candidate", 0, conflicts or ["no_candidate"]))
        for rank, target in enumerate(sorted(matches, key=lambda x: _field_raw(x, "official_identifier")), 1):
            tid = _official_id(_field_raw(target, "official_identifier"))
            target_name = normalize_text(_field_raw(target, "name")); target_address = normalize_text(_field_raw(target, "address"))
            source_name = normalize_text(name); source_address = normalize_text(address)
            target_line = target.get("source_line"); target_hash = target.get("source_line_sha256")
            cid = hashlib.sha256(f"{source_doc}|{sid}|{tid}|{target_line}|{target_hash}|{rank}|{NORMALIZER_VERSION}".encode()).hexdigest()
            candidates.append({"candidate_id": cid, "source_row_id": sid, "source_kind": kind,
                               "source_name_raw": name, "source_address_raw": address,
                               "target_official_identifier": tid, "target_facility_entity_id": target.get("facility_entity_id"),
                               "target_source_document_id": target.get("source_document_id") or anchor_meta.get("source_document_id"),
                               "target_page": target.get("source_page"), "target_line": target_line,
                               "target_line_sha256": target_hash, "method": method,
                               "score": 1.0 if method in {"official_identifier", "exact_name_address"} else .5,
                               "name_score": 1.0 if source_name and source_name == target_name else 0.0,
                               "address_score": 1.0 if source_address and source_address == target_address else 0.0,
                               "id_score": 1.0 if method == "official_identifier" else 0.0,
                               "identity_confidence": ("confirmed" if method == "official_identifier" and decision == "auto_accept" else ("probable" if method == "exact_name_address" and decision == "auto_accept" else "possible")),
                               "review_status": "auto_candidate" if decision == "auto_accept" else "manual",
                               "candidate_count": len(matches), "rank": rank, "decision": decision,
                               "conflict_codes": conflicts, "normalizer_version": NORMALIZER_VERSION,
                               "source_document_id": row.get("source_document_id") or source_doc,
                               "source_page": row.get("source_page"), "source_line": row.get("source_line") or row.get("source_row_index"),
                               "source_line_sha256": row.get("source_line_sha256") or row.get("source_row_sha256")})
    return {"match_candidates": candidates, "match_reviews": reviews, "qa": qa, "metadata": {"normalizer_version": NORMALIZER_VERSION, "source_document_id": source_doc, "source_content_sha256": src_meta.get("source_content_sha256") or src_meta.get("content_sha256"), "anchor_source_document_id": anchor_meta.get("source_document_id"), "anchor_source_content_sha256": anchor_meta.get("source_pdf_sha256") or anchor_meta.get("source_content_sha256"), "source_family": "SRC-06", "public_release_allowed": False, "unofficial_source_reservation": True, "v1_v3_classification": "not_performed", "long_term_653_individualization": False, "candidate_count": len(candidates), "review_count": len(reviews), "qa_error_count": sum(q.get("kind") == "error" for q in qa)}}

def write_match_outputs(result: dict[str, Any], output_dir: str | Path, *, replace=False, acknowledge_internal_use=False) -> dict[str, str]:
    if not acknowledge_internal_use: raise MatchingError("internal-use acknowledgement required")
    out = Path(output_dir); files = {"metadata": out / "metadata.json", "match_candidates": out / "match_candidates.jsonl", "match_reviews": out / "match_reviews.jsonl", "qa": out / "qa.jsonl"}
    if not replace and any(p.exists() for p in files.values()): raise MatchingError("output exists; pass --replace")
    out.mkdir(parents=True, exist_ok=True); staged = []
    try:
        values = {"metadata": result["metadata"], "match_candidates": result["match_candidates"], "match_reviews": result["match_reviews"], "qa": result["qa"]}
        for key, path in files.items():
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=out); os.close(fd); temp = Path(name); staged.append((key, temp))
            with temp.open("w", encoding="utf8", newline="\n") as stream:
                if key == "metadata": json.dump(values[key], stream, ensure_ascii=False, indent=2); stream.write("\n")
                else:
                    for row in values[key]: stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        backups, committed = [], []
        try:
            for path in files.values():
                if path.exists():
                    fd, name = tempfile.mkstemp(prefix=f".{path.name}.backup.", dir=out); os.close(fd); backup = Path(name); backup.unlink(); os.replace(path, backup); backups.append((path, backup))
            for key, temp in staged: os.replace(temp, files[key]); committed.append(files[key])
        except Exception as exc:
            for path in committed:
                if path.exists(): path.unlink()
            for path, backup in reversed(backups):
                if backup.exists(): os.replace(backup, path)
            raise MatchingError("match output commit failed") from exc
        for _, backup in backups:
            if backup.exists(): backup.unlink()
    finally:
        for _, temp in staged:
            if temp.exists(): temp.unlink()
    return {key: str(path) for key, path in files.items()}
