"""Deterministic, pure history ledger and replay helpers."""
from __future__ import annotations

import calendar
import hashlib
import json
from datetime import date
from typing import Any, Iterable

HISTORY_VERSION = "0.2.0"
SERIES = {"formal", "effective_official", "effective_with_unofficial"}
STRUCTURAL = {"contract_concluded", "contract_terminated", "abolished", "relocation",
               "address_change", "renamed", "service_change", "office_type_change",
               # A directly-operated office opening, distinct from a simple-office
               # contract but equally a change in the facility population.
               "established",
               # These change only the official depopulated-area flag.  They are
               # structural for replay purposes because their before/after states
               # are complete records: skipping them would break the state chain.
               "disadvantaged_area_change", "disadvantaged_area_correction"}
CLOSURES = {"temporarily_closed", "reopened"}


class HistoryError(ValueError):
    pass


def _id(prefix: str, *parts: Any) -> str:
    raw = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"{prefix}-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _raw(state: dict[str, Any] | None, key: str) -> str:
    value = (state or {}).get(key, "")
    return value.get("raw", "") if isinstance(value, dict) else str(value or "")


def _section(state: dict[str, Any] | None) -> str:
    """Section in the normalized vocabulary.

    Anchor rows already carry ``postal_office``/``company_office``; change-event
    states carry ``{"raw": "郵便局", "normalized": "postal_office"}``.  Reading
    the raw side would make a replayed entity's section flip to Japanese as soon
    as any event applied to it, splitting every by-section count in two.
    """
    value = (state or {}).get("section", "")
    if isinstance(value, dict):
        return value.get("normalized") or value.get("raw", "")
    return str(value or "")


def _payload(state: dict[str, Any] | None) -> dict[str, str]:
    payload = {k: _raw(state, k) for k in ("section", "official_identifier", "name", "address", "operating_status")}
    payload["section"] = _section(state)
    return payload


def _records(anchor: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    return anchor if isinstance(anchor, list) else list(anchor.get("records", []))


def _events(value: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    return value if isinstance(value, list) else list(value.get("events", []))


def _effective_date(event: dict[str, Any]) -> str | None:
    """Date at which the event is replayed, or ``None`` when it cannot be.

    ``confirmed_effective_date`` is set by ``confirm`` only where a separate
    later observation fixed the date (``date_basis="observed"``).  With a
    single anchor cross-section almost every corroborated event instead carries
    ``state_corroborated=True`` and no confirmed date: the state change is
    established, only its exact day rests on the notice's plan.  Refusing to
    replay those would empty the monthly series, so the planned date is used
    and :func:`date_basis` labels it ``planned`` for every downstream metadata
    block.  The fallback is deliberately gated on ``state_corroborated is
    True``: an event that merely lacks the flag stays inapplicable.
    """
    if event.get("provenance", "official") == "unofficial":
        return event.get("observed_effective_date") or event.get("planned_effective_date")
    if event.get("event_status") in {"confirmed", "effective"}:
        confirmed = event.get("confirmed_effective_date")
        if confirmed:
            return confirmed
        if event.get("state_corroborated") is True:
            return event.get("planned_effective_date")
    return None


def date_basis(event: dict[str, Any]) -> str:
    """``observed`` / ``planned`` / ``unknown`` for a replayed event's date."""
    if event.get("provenance", "official") == "unofficial":
        return "observed" if event.get("observed_effective_date") else "planned"
    if event.get("confirmed_effective_date"):
        return "observed"
    if event.get("state_corroborated") is True and event.get("planned_effective_date"):
        return "planned"
    return "unknown"


effective_date = _effective_date


def _applicable_any(event: dict[str, Any], disabled: set[str], invalid_ids: set[str] | None = None, disabled_families: set[str] | None = None) -> bool:
    if event.get("source_document_id") in disabled or event.get("source_family") in (disabled_families or set()) or event.get("event_id") in (invalid_ids or set()):
        return False
    kind = event.get("event_type")
    if event.get("provenance", "official") == "official":
        return event.get("event_status") in {"confirmed", "effective"} and bool(_effective_date(event)) and kind not in {None, "", "unknown"}
    return kind in CLOSURES and bool(event.get("observed_effective_date") or event.get("planned_effective_date"))


def _month_end(value: str) -> date:
    if len(value) == 7:
        year, month = map(int, value.split("-")); return date(year, month, calendar.monthrange(year, month)[1])
    day = _day(value)
    if day is None or day.day != calendar.monthrange(day.year, day.month)[1]:
        raise HistoryError("snapshot cutoff must be a calendar month-end")
    return day


def validate_snapshot_cutoff(value: str) -> str:
    return _month_end(value).isoformat()


def _union_graph(events: list[dict[str, Any]]) -> tuple[dict[str, str], list[dict[str, Any]]]:
    parent: dict[str, str] = {}
    def find(x: str) -> str:
        parent.setdefault(x, x)
        if parent[x] != x: parent[x] = find(parent[x])
        return parent[x]
    def union(a: str, b: str) -> None:
        a, b = find(a), find(b)
        if a != b: parent[b] = a
    for e in events:
        before, after = _raw(e.get("before_state"), "official_identifier"), _raw(e.get("after_state"), "official_identifier")
        if before and before != "-": find(before)
        if after and after != "-": find(after)
        if before and after and before != "-" and after != "-": union(before, after)
    return {k: find(k) for k in parent}, []


def validate_event_chains(events: Iterable[dict[str, Any]], *, disabled_sources: Iterable[str] = ()) -> list[dict[str, Any]]:
    rows = list(events); qa: list[dict[str, Any]] = []; ids = [e.get("event_id") for e in rows]
    seen: set[str] = set()
    for eid in ids:
        if eid and eid in seen: qa.append({"kind": "error", "code": "duplicate_event_id", "event_id": eid})
        if eid: seen.add(eid)
    by_id = {e.get("event_id"): e for e in rows}
    for e in rows:
        eid = e.get("event_id")
        for key in ("revision_of_event_id", "cancelled_by_event_id"):
            ref = e.get(key)
            if ref and ref not in by_id: qa.append({"kind": "error", "code": "unresolved_event_reference", "event_id": eid, "field": key})
            if ref == eid: qa.append({"kind": "error", "code": "event_reference_cycle", "event_id": eid})
        if e.get("provenance", "official") != "official" and e.get("event_type") not in CLOSURES:
            qa.append({"kind": "error", "code": "unofficial_structural_event", "event_id": eid})
    # Revision and cancellation chains must be acyclic.
    for field in ("revision_of_event_id", "cancelled_by_event_id"):
        for start in by_id:
            path: set[str] = set(); cur = start
            while cur and cur in by_id:
                if cur in path:
                    qa.append({"kind": "error", "code": "event_reference_cycle", "event_id": start, "field": field}); break
                path.add(cur); cur = by_id[cur].get(field)
    return qa


def build_history(anchor: dict[str, Any] | list[dict[str, Any]], events: dict[str, Any] | list[dict[str, Any]], *,
                  anchor_date: str | None = None, source_pdf_sha256: str = "",
                  disabled_sources: Iterable[str] = (), disabled_source_families: Iterable[str] = (), recorded_at: str = "") -> dict[str, Any]:
    records, notices = _records(anchor), _events(events)
    meta = anchor.get("metadata", {}) if isinstance(anchor, dict) else {}
    anchor_date = anchor_date or meta.get("coverage_date") or ""
    anchor_day = _day(anchor_date)
    source_id = meta.get("source_document_id") or _id("src", source_pdf_sha256 or "unknown")
    disabled = set(disabled_sources); disabled_families = set(disabled_source_families); qa = validate_event_chains(notices, disabled_sources=disabled)
    duplicate_ids = {e.get("event_id") for e in notices if e.get("event_id") and sum(x.get("event_id") == e.get("event_id") for x in notices) > 1}
    referenced = {e.get("revision_of_event_id") for e in notices if e.get("revision_of_event_id")}
    cancelled = {e.get("cancelled_by_event_id") for e in notices if e.get("cancelled_by_event_id")} | {e.get("event_id") for e in notices if e.get("cancelled_by_event_id")}
    graph_events = [e for e in notices if _applicable_any(e, disabled, duplicate_ids, disabled_families) and e.get("event_id") not in referenced and e.get("event_id") not in cancelled]
    graph, _ = _union_graph(graph_events)
    # Include anchor identifiers as isolated graph nodes.
    for r in records:
        ident = str(r.get("official_identifier", "")); graph.setdefault(ident, ident)
    groups: dict[str, list[str]] = {}
    for ident, root in graph.items(): groups.setdefault(root, []).append(ident)
    id_to_entity: dict[str, str] = {}; entities: dict[str, dict[str, Any]] = {}
    anchor_by_id = {str(r.get("official_identifier", "")): r for r in records}
    active_structural = [e for e in graph_events if _effective_date(e) and e.get("provenance", "official") == "official" and e.get("event_type") in STRUCTURAL]
    for root, identifiers in sorted(groups.items()):
        eid = _id("facility", source_id, sorted(identifiers));
        for ident in identifiers: id_to_entity[ident] = eid
        rec = next((anchor_by_id[x] for x in identifiers if x in anchor_by_id), None)
        prior = [e for e in active_structural if _raw(e.get("before_state"), "official_identifier") in identifiers or _raw(e.get("after_state"), "official_identifier") in identifiers]
        prior.sort(key=lambda e: (_effective_date(e) or "", e.get("event_id", "")))
        state = rec or (prior[0].get("before_state", {}) if prior else {})
        if not rec and anchor_day:
            for e in prior:
                if (_day(_effective_date(e)) or date.max) <= anchor_day: state = e.get("after_state", state)
        ident = _raw(state, "official_identifier") or (identifiers[0] if identifiers else "")
        exists = ident not in {"", "-"}
        if rec is None and prior and (_day(_effective_date(prior[-1])) or date.max) > (anchor_day or date.min): exists = False
        before_ids = {x for e in prior for x in [_raw(e.get("before_state"), "official_identifier")] if x and x != "-"}
        after_ids = {x for e in prior for x in [_raw(e.get("after_state"), "official_identifier")] if x and x != "-"}
        predecessors = sorted(before_ids - after_ids - {ident})
        successors = sorted(after_ids - before_ids - {ident})
        entity = {"facility_entity_id": eid, "entity_type": _section(state), "official_identifier": ident,
                  "section": _section(state), "name": _raw(state, "name"), "address": _raw(state, "address"),
                  "exists": exists, "available": _raw(state, "operating_status") not in {"一時閉鎖", "temporarily_closed"},
                  "state": _payload(state), "lineage_status": "active" if exists else "historical_or_future",
                  "predecessor_entity_ids": [], "successor_entity_ids": [], "predecessor_identifiers": predecessors,
                  "successor_identifiers": successors, "identity_confidence": "high" if prior else ("medium" if rec else "low"),
                  "valid_from": anchor_date if rec else None, "valid_to": None, "recorded_at": recorded_at,
                  "superseded_at": None, "provenance": "official", "source_document_id": source_id,
                  "source_page": (rec or {}).get("source_page"), "source_line": (rec or {}).get("source_line"),
                  "source_line_sha256": (rec or {}).get("source_line_sha256")}
        entities[eid] = entity
    ledger: list[dict[str, Any]] = []; identifier_history: list[dict[str, Any]] = []; state_history: list[dict[str, Any]] = []
    ledger_ids: set[str] = set()
    for e in notices:
        eid = e.get("event_id") or _id("event", e); before_id = _raw(e.get("before_state"), "official_identifier"); after_id = _raw(e.get("after_state"), "official_identifier")
        if eid in ledger_ids: qa.append({"kind": "error", "code": "duplicate_event_id", "event_id": eid})
        ledger_ids.add(eid)
        applicable = _applicable_any(e, disabled, duplicate_ids, disabled_families) and eid not in referenced and eid not in cancelled
        facility = (e.get("facility_entity_id") or id_to_entity.get(before_id) or id_to_entity.get(after_id)) if applicable else None
        if applicable and facility is None and (before_id not in {"", "-"} or after_id not in {"", "-"}): qa.append({"kind": "error", "code": "unresolved_event_entity", "event_id": eid})
        source_event = dict(e); source_event.update(event_id=eid, facility_entity_id=facility, provenance=e.get("provenance", "official"), review_status=e.get("review_status", "unmatched"))
        eff = _effective_date(e)
        row = {"event_id": eid, "facility_entity_id": facility, "event": source_event, "effective_date": eff,
               "valid_from": eff, "valid_to": None, "recorded_at": e.get("recorded_at", recorded_at), "superseded_at": None,
               "source_document_id": e.get("source_document_id", source_id), "source_page": e.get("source_page"), "source_line": e.get("source_line"),
               "source_line_sha256": e.get("source_line_sha256"), "disabled": e.get("source_document_id") in disabled or e.get("source_family") in disabled_families or eid in duplicate_ids, "applied": False}
        if eid in referenced or eid in cancelled or eid in duplicate_ids: row["superseded_at"] = e.get("notice_date") or e.get("recorded_at") or recorded_at
        ledger.append(row)
    # Rebuild bitemporal intervals from earliest transition forward.  The
    # anchor is an observation, not a new interval that can close backward.
    structural = [r for r in ledger if not r.get("disabled") and not r.get("superseded_at") and r.get("facility_entity_id") and r.get("event", {}).get("provenance", "official") == "official" and r.get("event", {}).get("event_type") in STRUCTURAL and r.get("effective_date")]
    for fid, entity in entities.items():
        transitions = sorted([r for r in structural if r["facility_entity_id"] == fid], key=lambda r: (r["effective_date"], r["event_id"]))
        states: list[tuple[str | None, dict[str, Any], dict[str, Any]]] = []
        if transitions:
            first = transitions[0]
            states.append((None, first["event"].get("before_state", {}), first))
            for row in transitions:
                states.append((row["effective_date"], row["event"].get("after_state", {}), row))
        else:
            states.append((entity.get("valid_from"), entity.get("state", {}), {"source_document_id": entity.get("source_document_id"), "source_page": entity.get("source_page"), "source_line": entity.get("source_line"), "source_line_sha256": entity.get("source_line_sha256"), "recorded_at": entity.get("recorded_at", "")}))
        for index, (start, state, provenance_row) in enumerate(states):
            end = states[index + 1][0] if index + 1 < len(states) else None
            ident = _raw(state, "official_identifier")
            exists = ident not in {"", "-"}
            state_out = _payload(state); state_out["exists"] = exists
            if not exists: state_out["official_identifier"] = ""
            common = {"facility_entity_id": fid, "valid_from": start, "valid_to": end, "recorded_at": provenance_row.get("recorded_at", ""), "superseded_at": provenance_row.get("recorded_at") if end else None,
                      "source_document_id": provenance_row.get("source_document_id", entity.get("source_document_id")), "source_page": provenance_row.get("source_page"), "source_line": provenance_row.get("source_line"), "source_line_sha256": provenance_row.get("source_line_sha256")}
            state_history.append({**common, "state": state_out})
            if ident and ident != "-": identifier_history.append({**common, "official_identifier": ident})
    for entity in entities.values():
        entity["predecessor_entity_ids"] = sorted({id_to_entity[x] for x in entity.get("predecessor_identifiers", []) if x in id_to_entity and id_to_entity[x] != entity["facility_entity_id"]})
        entity["successor_entity_ids"] = sorted({id_to_entity[x] for x in entity.get("successor_identifiers", []) if x in id_to_entity and id_to_entity[x] != entity["facility_entity_id"]})
    basis_counts = {key: 0 for key in ("observed", "planned", "unknown")}
    for row in ledger:
        if row.get("disabled") or row.get("superseded_at") or not row.get("effective_date"):
            continue
        basis_counts[date_basis(row["event"])] += 1
    if basis_counts["planned"] and not basis_counts["observed"]:
        basis_summary = "planned"
    elif basis_counts["observed"] and not basis_counts["planned"]:
        basis_summary = "observed"
    elif basis_counts["observed"] or basis_counts["planned"]:
        basis_summary = "mixed"
    else:
        basis_summary = "none_applied"
    return {"history_version": HISTORY_VERSION, "anchor_date": anchor_date, "source_document_id": source_id, "entities": list(entities.values()),
            "official_identifier_history": identifier_history, "facility_state_history": state_history, "ledger": ledger, "qa": qa,
            "effective_date_basis": basis_summary, "effective_date_basis_counts": basis_counts,
            "qa_error_count": sum(x.get("kind") == "error" for x in qa)}


def _active(history: dict[str, Any], series: str) -> list[dict[str, Any]]:
    if series not in SERIES: raise HistoryError("unknown history series")
    out = []; superseded = {r["event"].get("revision_of_event_id") for r in history.get("ledger", [])}; cancelled = {r["event"].get("cancelled_by_event_id") for r in history.get("ledger", [])} | {r["event_id"] for r in history.get("ledger", []) if r["event"].get("cancelled_by_event_id")}
    for row in history.get("ledger", []):
        e = row["event"]; status = e.get("event_status"); prov = e.get("provenance", "official"); kind = e.get("event_type")
        if row.get("disabled") or row.get("superseded_at") or row["event_id"] in superseded or row["event_id"] in cancelled or kind in {None, "", "unknown"}: continue
        if prov == "official":
            if status not in {"confirmed", "effective"} or not _effective_date(e): continue
            if series == "formal" and kind in CLOSURES: continue
        else:
            if kind not in CLOSURES or series != "effective_with_unofficial": continue
        if row.get("effective_date"): out.append(row)
    return sorted(out, key=lambda r: (r["effective_date"], r["event_id"]))


def _apply(entity: dict[str, Any], row: dict[str, Any], reverse: bool = False) -> None:
    kind = row["event"].get("event_type")
    if kind == "temporarily_closed": entity["available"] = reverse; return
    if kind == "reopened": entity["available"] = not reverse; return
    state = row["event"].get("before_state" if reverse else "after_state", {}); entity["state"] = _payload(state); entity["official_identifier"] = _raw(state, "official_identifier") or entity["official_identifier"]; entity["name"] = _raw(state, "name"); entity["address"] = _raw(state, "address"); entity["section"] = _section(state) or entity["section"]; entity["exists"] = entity["official_identifier"] not in {"", "-"}


def replay_history(history: dict[str, Any], target_date: str, *, series: str = "formal") -> list[dict[str, Any]]:
    target = _day(target_date)
    if target is None or series not in SERIES: raise HistoryError("invalid replay cutoff or series")
    anchor = _day(history.get("anchor_date")) or target; entities = {e["facility_entity_id"]: dict(e) for e in history.get("entities", [])}; rows = _active(history, series)
    for e in entities.values(): e["applied_event_ids"] = []; e["applied_source_document_ids"] = []; e["applied_provenance"] = []
    selected = [(r, _day(r["effective_date"])) for r in rows]
    if target >= anchor:
        selected = [(r, d) for r, d in selected if d and anchor < d <= target]
        direction = False
    else:
        selected = [(r, d) for r, d in selected if d and target < d <= anchor][::-1]; direction = True
    for row, _ in selected:
        if row.get("facility_entity_id") not in entities: continue
        _apply(entities[row["facility_entity_id"]], row, reverse=direction); entities[row["facility_entity_id"]]["applied_event_ids"].append(row["event_id"]); entities[row["facility_entity_id"]]["applied_source_document_ids"].append(row.get("source_document_id")); entities[row["facility_entity_id"]]["applied_provenance"].append(row["event"].get("provenance", "official"))
    return sorted(entities.values(), key=lambda e: e["facility_entity_id"])


def monthly_snapshots(history: dict[str, Any], months: Iterable[str], *, series: str = "formal") -> list[dict[str, Any]]:
    rows = []
    for month in months:
        cutoff = _month_end(month).isoformat()
        for e in replay_history(history, cutoff, series=series):
            sources = [x for x in e.get("applied_source_document_ids", []) if x]
            kinds = set(e.get("applied_provenance", [])) | {"official"}
            prov = "mixed" if len(kinds) > 1 else next(iter(kinds))
            rows.append({"series": series, "snapshot_month": month, "snapshot_cutoff": cutoff, "facility_entity_id": e["facility_entity_id"], "exists": e["exists"], "available": e["available"], "state": e["state"], "applied_event_ids": e.get("applied_event_ids", []), "applied_source_document_ids": sources, "ledger_version": history.get("history_version", HISTORY_VERSION), "provenance": prov, "source_document_id": history.get("source_document_id")})
    return rows


generate_monthly_snapshots = monthly_snapshots
