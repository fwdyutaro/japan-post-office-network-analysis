"""Event confirmation: corroborate announced change events against an anchor.

The monthly change notices publish *planned* changes, so ``change_pdf`` emits
every event as ``event_status="announced"`` with no
``confirmed_effective_date`` (spec 9.1).  Nothing may enter the effective
series on the strength of a plan alone.

This module supplies the missing evidence step of spec 9.2.  Two independent
lines of evidence are required before a chain's state change is corroborated:

(a) **Chain integrity** - within a facility (identifiers joined transitively by
    before/after pairs), events sorted by planned effective date must form a
    continuous chain: ``event[i].after_state`` equals ``event[i+1].before_state``.

(b) **Anchor terminal match** - the state produced by the last event dated on or
    before the anchor date must equal what the anchor current list actually
    records.  A chain ending in deletion must leave no trace in the anchor; a
    chain ending in a live facility must be present in the anchor with matching
    section, name and address.

**What that does and does not prove.**  A terminal match shows only that the
facility *had reached* the terminal state by the anchor date.  It does not show
that each intermediate change happened on the date the notice planned: a single
event, or a round trip A->B->A, produces exactly the same terminal state
whatever the real dates were.  The two claims are therefore recorded as
separate fields and never conflated:

``state_corroborated``
    Chain integrity plus anchor terminal match.  True means "the facility was
    in this state by the anchor date".

``effective_date_confirmed``
    True only when a *separate, individual* later observation (a subsequent
    current-list cross-section, or an official per-office notice) puts the
    change on the planned date.  ``confirmed_effective_date`` is populated only
    in that case.

``date_basis``
    ``observed`` when a later observation fixed the date, ``planned`` when the
    state is corroborated but the date rests on the notice's plan alone, and
    ``unknown`` otherwise.

With a single anchor cross-section the expected outcome for nearly every event
is ``state_corroborated=true, effective_date_confirmed=false,
date_basis="planned"``.  That is the correct answer, not a shortfall to be
engineered away.  Absence of evidence is never confirmation.

The anchor itself is checked before anything is corroborated (spec 15.1/15.2):
non-empty and unique official identifiers, a record count that agrees with the
anchor metadata, zero anchor QA errors, and sealed-artifact checksums that
still match the files on disk.  The supplied records and metadata are then
bound to that seal - they must be byte-for-byte the sealed payload, so a run
cannot cite a sealed artifact ID while corroborating against different rows.
Any failure stops the run.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from .artifacts import ArtifactError, verify_artifact

CONFIRM_VERSION = "0.2.0"

#: Fields compared when testing whether two states describe the same facility.
#: ``related_office`` and ``notes`` are free-text columns whose PDF layout is
#: unstable, so they are recorded but never decide a promotion.
CHAIN_FIELDS = ("section", "official_identifier", "name", "address",
                "simple_post_office", "disadvantaged_area", "services")
#: Fields compared against the anchor current list.
ANCHOR_FIELDS = ("section", "name", "address")

DELETED = {"-", "", "―", "─", "－", "−"}
# U+30FC is a katakana prolonged-sound mark, but Japan Post's address cells also
# use it where a hyphen is meant (東住吉１１ー１).  Folding it to a hyphen only
# for comparison is safe: it can merge two spellings of the same string, never
# two different ones, and the stored values keep the original codepoints.
_DASHES = "-‐‑‒–—−－―─ー"
_CIRCLES = "○〇"

#: Reasons an event's state change was not corroborated.
NOT_PROMOTED = {
    "chain_inconsistent",
    "anchor_mismatch",
    "future_event",
    "missing_planned_date",
    "invalid_planned_date",
    "no_anchor_evidence",
}

#: Kinds of later observation that can fix an event's effective date.  A
#: cross-section of the current list published *after* the change, or an
#: official per-office notice, are the two evidence types spec 9.2 allows.
DATE_OBSERVATION_KINDS = {"current_list_snapshot", "official_notice"}

DATE_BASIS = {"observed", "planned", "unknown"}


class ConfirmError(ValueError):
    """Safe error.  ``details`` carries structured, address-free diagnostics."""

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except (ValueError, TypeError):
        return None


def _text(value: Any) -> str:
    """Comparable form of a source cell.

    Change notices and the current list are produced by different templates:
    they differ in padding, in full/half width, and in which of the many dash
    codepoints they use.  Those differences are typographic, not substantive.
    """
    if isinstance(value, dict):
        value = value.get("normalized") or value.get("raw") or ""
    text = unicodedata.normalize("NFKC", str(value or ""))
    for dash in _DASHES:
        text = text.replace(dash, "-")
    for circle in _CIRCLES:
        text = text.replace(circle, "○")
    return "".join(text.split())


def _ident(state: dict[str, Any] | None, key: str = "official_identifier") -> str:
    raw = (state or {}).get(key)
    if isinstance(raw, dict):
        raw = raw.get("raw", "")
    return str(raw or "").strip()


def _is_deleted(state: dict[str, Any] | None) -> bool:
    return _ident(state) in DELETED


def _state_key(state: dict[str, Any] | None) -> tuple[Any, ...]:
    values: list[Any] = []
    for field in CHAIN_FIELDS:
        raw = (state or {}).get(field)
        if isinstance(raw, list):
            values.append(tuple(_text(x) for x in raw))
        else:
            values.append(_text(raw))
    return tuple(values)


def _state_diff(left: dict[str, Any] | None, right: dict[str, Any] | None) -> list[str]:
    out = []
    for field in CHAIN_FIELDS:
        a, b = (left or {}).get(field), (right or {}).get(field)
        if isinstance(a, list) or isinstance(b, list):
            av = tuple(_text(x) for x in (a or []))
            bv = tuple(_text(x) for x in (b or []))
        else:
            av, bv = _text(a), _text(b)
        if av != bv:
            out.append(field)
    return out


def _anchor_state(record: dict[str, Any]) -> dict[str, Any]:
    """Project an anchor current-list row onto the change-event state shape."""
    return {
        "section": record.get("section"),
        "official_identifier": record.get("official_identifier"),
        "name": record.get("name"),
        "address": record.get("address"),
        "simple_post_office": record.get("simple_post_office"),
        "disadvantaged_area": record.get("disadvantaged_area"),
        "services": record.get("services") or [],
    }


def _sort_key(event: dict[str, Any]) -> tuple[Any, ...]:
    return (event.get("planned_effective_date") or "",
            event.get("source_document_date") or "",
            event.get("source_page") or 0,
            event.get("source_line") or 0,
            event.get("event_id") or "")


def _union_identifiers(events: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Join identifiers that the events themselves say are the same facility."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for event in events:
        before, after = _ident(event.get("before_state")), _ident(event.get("after_state"))
        live = [x for x in (before, after) if x not in DELETED]
        for x in live:
            find(x)
        if len(live) == 2:
            union(live[0], live[1])
    return {k: find(k) for k in parent}


def _order_chain(events: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    """Order same-date events so consecutive states join up.

    Several changes to one facility can share an effective date and the notice
    gives no ordering between them.  Try to recover the order by threading
    after-state into before-state; return ``None`` when no such thread exists.
    """
    if len(events) < 2:
        return list(events)
    remaining = list(events)
    heads = {_state_key(e.get("before_state")) for e in remaining}
    starts = [e for e in remaining if _state_key(e.get("after_state")) not in heads]
    if len(starts) != 1:
        return None
    ordered = [starts[0]]
    remaining.remove(starts[0])
    while remaining:
        tail = _state_key(ordered[-1].get("after_state"))
        nxt = [e for e in remaining if _state_key(e.get("before_state")) == tail]
        if len(nxt) != 1:
            return None
        ordered.append(nxt[0])
        remaining.remove(nxt[0])
    return ordered


def _dedupe(events: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drop re-announcements of an identical change.

    A change can be printed in more than one monthly notice.  Two events with
    the same effective date and byte-identical before/after states are one real
    change; the earliest notice is kept and the later ones are recorded.
    """
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for event in sorted(events, key=_sort_key):
        key = (event.get("planned_effective_date"),
               _state_key(event.get("before_state")), _state_key(event.get("after_state")))
        if key in seen:
            dropped.append({"event_id": event.get("event_id"),
                            "duplicate_of": seen[key].get("event_id"),
                            "source_document_date": event.get("source_document_date")})
            continue
        seen[key] = event
        kept.append(event)
    return kept, dropped


def _group_id(identifiers: list[str]) -> str:
    raw = json.dumps(sorted(identifiers), ensure_ascii=False, separators=(",", ":"))
    return "chain-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


ANCHOR_RECORDS_NAME = "records.jsonl"
ANCHOR_METADATA_NAME = "metadata.json"


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _rows_digest(rows: Iterable[dict[str, Any]]) -> str:
    """Order-sensitive digest of a normalized row sequence."""
    digest = hashlib.sha256()
    for row in rows:
        payload = _canonical_bytes(row)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()


def _object_digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def load_sealed_anchor(anchor_dir: str | Path) -> dict[str, Any]:
    """Read the anchor current list out of its sealed bundle.

    Callers that use this cannot present records the seal does not cover; the
    binding check in :func:`validate_anchor` then passes by construction.  The
    bundle itself is not verified here - :func:`validate_anchor` does that, and
    doing it twice would invite a caller to treat this function as sufficient.
    """
    root = Path(anchor_dir)
    try:
        records = [json.loads(line) for line
                   in (root / ANCHOR_RECORDS_NAME).read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        metadata = json.loads((root / ANCHOR_METADATA_NAME).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfirmError("sealed anchor payload unreadable",
                           [{"code": "anchor_payload_unreadable",
                             "detail": type(exc).__name__}]) from exc
    if not isinstance(metadata, dict) or any(not isinstance(r, dict) for r in records):
        raise ConfirmError("sealed anchor payload rejected",
                           [{"code": "anchor_payload_schema_rejected"}])
    return {"records": records, "metadata": metadata}


def validate_anchor(records: list[dict[str, Any]], metadata: dict[str, Any], *,
                    anchor_dir: str | Path | None = None,
                    allow_unsealed_anchor: bool = False) -> dict[str, Any]:
    """Fail-closed preconditions on the anchor current list.

    Corroboration reads the anchor by official identifier.  A duplicate
    identifier silently overwrote the earlier row, so two rows sharing an
    identifier made every chain terminating there agree with whichever row came
    last in the file.  A defective anchor (non-zero QA errors, a record count
    that disagrees with its own metadata, or payload bytes that no longer match
    the sealed checksums) is likewise unusable as evidence.

    Returns a summary on success; raises :class:`ConfirmError` otherwise, with
    the offending identifiers on ``.details``.  Never returns a partial pass.
    """
    problems: list[dict[str, Any]] = []

    blank: list[int] = []
    counts: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        ident = str(record.get("official_identifier", "")).strip()
        if not ident:
            blank.append(index)
            continue
        counts.setdefault(ident, []).append(index)
    if blank:
        problems.append({"code": "anchor_identifier_blank", "row_count": len(blank),
                         "row_indexes": blank[:50]})
    duplicates = {k: v for k, v in counts.items() if len(v) > 1}
    if duplicates:
        problems.append({"code": "anchor_identifier_duplicated",
                         "duplicate_identifier_count": len(duplicates),
                         "affected_row_count": sum(len(v) for v in duplicates.values()),
                         # Every duplicate is listed; a truncated list would let
                         # the tail of a large collision set pass unseen.
                         "duplicates": [{"official_identifier": k, "row_indexes": v}
                                        for k, v in sorted(duplicates.items())]})

    declared = ((metadata or {}).get("counts") or {}).get("total")
    if declared is None:
        problems.append({"code": "anchor_metadata_count_missing"})
    elif not isinstance(declared, int) or declared != len(records):
        problems.append({"code": "anchor_record_count_mismatch",
                         "metadata_counts_total": declared, "actual_record_count": len(records)})

    qa_errors = (metadata or {}).get("qa_error_count")
    if qa_errors is None:
        problems.append({"code": "anchor_qa_error_count_missing"})
    elif not isinstance(qa_errors, int) or qa_errors != 0:
        problems.append({"code": "anchor_qa_error_count_nonzero", "qa_error_count": qa_errors})

    artifact_id = None
    payload_binding = "not_verified"
    records_digest = _rows_digest(records)
    metadata_digest = _object_digest(metadata or {})
    if anchor_dir is None:
        if not allow_unsealed_anchor:
            problems.append({"code": "anchor_artifact_not_supplied"})
        artifact_state = "not_verified"
    else:
        try:
            verified = verify_artifact(anchor_dir, strict_code=True)
        except (ArtifactError, OSError, ValueError) as exc:
            problems.append({"code": "anchor_artifact_unverifiable", "detail": type(exc).__name__})
            artifact_state = "unverifiable"
        else:
            artifact_id = verified.get("artifact_id")
            artifact_state = "verified" if verified.get("ok") else "mismatched"
            if not verified.get("ok"):
                problems.append({"code": "anchor_artifact_mismatch",
                                 "errors": sorted(verified.get("errors", []))})
            else:
                sealed = set(json.loads((Path(anchor_dir) / "checksums.json")
                                        .read_text(encoding="utf-8")).get("files", {}))
                missing = sorted({ANCHOR_RECORDS_NAME, ANCHOR_METADATA_NAME} - sealed)
                if missing:
                    problems.append({"code": "anchor_artifact_incomplete", "missing": missing})
                else:
                    # Verifying the bundle says the *files on disk* are intact.
                    # It says nothing about the records the caller handed in.
                    # Without this binding a caller could seal one cross-section
                    # and corroborate events against a different, unsealed set
                    # of rows while the report cites the sealed artifact ID.
                    try:
                        sealed_anchor = load_sealed_anchor(anchor_dir)
                    except ConfirmError as exc:
                        problems.extend(exc.details or
                                        [{"code": "anchor_payload_unreadable"}])
                        payload_binding = "unreadable"
                    else:
                        sealed_records_digest = _rows_digest(sealed_anchor["records"])
                        sealed_metadata_digest = _object_digest(sealed_anchor["metadata"])
                        mismatched = []
                        if sealed_records_digest != records_digest:
                            mismatched.append(
                                {"payload": ANCHOR_RECORDS_NAME,
                                 "sealed_record_count": len(sealed_anchor["records"]),
                                 "supplied_record_count": len(records),
                                 "sealed_sha256": sealed_records_digest,
                                 "supplied_sha256": records_digest})
                        if sealed_metadata_digest != metadata_digest:
                            mismatched.append(
                                {"payload": ANCHOR_METADATA_NAME,
                                 "sealed_sha256": sealed_metadata_digest,
                                 "supplied_sha256": metadata_digest})
                        if mismatched:
                            payload_binding = "mismatched"
                            problems.append({"code": "anchor_input_not_from_sealed_artifact",
                                             "mismatched": mismatched})
                        else:
                            payload_binding = "bound_to_sealed_payload"

    if problems:
        raise ConfirmError("anchor preconditions failed", problems)
    return {"anchor_record_count": len(records),
            "anchor_unique_identifier_count": len(counts),
            "anchor_artifact_state": artifact_state,
            "anchor_artifact_id": artifact_id,
            "anchor_payload_binding": payload_binding,
            "anchor_records_sha256": records_digest,
            "anchor_metadata_sha256": metadata_digest}


def _date_observation_index(observations: Iterable[dict[str, Any]] | None,
                            qa: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Validate later date observations and index them by event id."""
    index: dict[str, dict[str, Any]] = {}
    for row in observations or ():
        eid = str(row.get("event_id") or "").strip()
        kind = row.get("observation_kind")
        observed = _day(row.get("observed_effective_date"))
        if not eid:
            qa.append({"kind": "error", "code": "date_observation_missing_event_id"})
            continue
        if kind not in DATE_OBSERVATION_KINDS:
            qa.append({"kind": "error", "code": "date_observation_kind_rejected",
                       "event_id": eid, "observation_kind": kind})
            continue
        if observed is None:
            qa.append({"kind": "error", "code": "date_observation_date_invalid", "event_id": eid})
            continue
        entry = {"observed_effective_date": observed.isoformat(), "observation_kind": kind,
                 "source_document_id": row.get("source_document_id"),
                 "observed_at": row.get("observed_at")}
        prior = index.get(eid)
        if prior and prior["observed_effective_date"] != entry["observed_effective_date"]:
            qa.append({"kind": "error", "code": "date_observation_conflicting_dates",
                       "event_id": eid,
                       "dates": sorted({prior["observed_effective_date"],
                                        entry["observed_effective_date"]})})
            index[eid] = {**entry, "conflicting": True}
            continue
        if prior and prior.get("conflicting"):
            continue
        index[eid] = entry
    return index


def confirm_events(events: Iterable[dict[str, Any]], anchor: dict[str, Any] | list[dict[str, Any]], *,
                   anchor_date: str | None = None, anchor_document_id: str = "",
                   anchor_dir: str | Path | None = None,
                   allow_unsealed_anchor: bool = False,
                   date_observations: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Corroborate announced events against the anchor.

    Returns the full event list, a QA list explaining every non-corroboration,
    per-chain reports and counts.  Corroborated events carry
    ``state_corroborated=True`` plus a ``confirmation_evidence`` block;
    ``confirmed_effective_date`` is populated only for the events whose date a
    later observation in ``date_observations`` independently fixed.

    The anchor preconditions of :func:`validate_anchor` are enforced first and
    a failure raises rather than degrading the result.
    """
    rows = [dict(e) for e in events]
    records = anchor if isinstance(anchor, list) else list(anchor.get("records", []))
    meta = anchor.get("metadata", {}) if isinstance(anchor, dict) else {}
    anchor_date = anchor_date or meta.get("coverage_date") or ""
    anchor_day = _day(anchor_date)
    if anchor_day is None:
        raise ConfirmError("anchor date required", [{"code": "anchor_date_missing_or_invalid"}])
    anchor_document_id = anchor_document_id or meta.get("source_document_id") or ""
    anchor_check = validate_anchor(records, meta, anchor_dir=anchor_dir,
                                   allow_unsealed_anchor=allow_unsealed_anchor)
    anchor_by_id = {str(r.get("official_identifier", "")).strip(): r for r in records}

    qa: list[dict[str, Any]] = []
    observed_dates = _date_observation_index(date_observations, qa)
    kept, duplicates = _dedupe(rows)
    for row in duplicates:
        qa.append({"kind": "info", "code": "duplicate_announcement", **row})
    dropped_ids = {d["event_id"] for d in duplicates}

    undated = [e for e in kept if not e.get("planned_effective_date")]
    for event in undated:
        qa.append({"kind": "error", "code": "missing_planned_date", "event_id": event.get("event_id"),
                   "source_document_date": event.get("source_document_date")})
    # A planned date that is present but not an ISO date is a parse defect, not
    # a change scheduled for the future.  Sorting it as ``date.max`` used to
    # file it under "future_event" (an info-level row) and hide it.
    invalid_dated: list[dict[str, Any]] = []
    dated: list[dict[str, Any]] = []
    for event in kept:
        planned = event.get("planned_effective_date")
        if not planned:
            continue
        if _day(planned) is None:
            invalid_dated.append(event)
            qa.append({"kind": "error", "code": "invalid_planned_date",
                       "event_id": event.get("event_id"),
                       "planned_effective_date": str(planned)[:32],
                       "change_date_raw": str(event.get("change_date_raw") or "")[:32],
                       "source_document_date": event.get("source_document_date")})
        else:
            dated.append(event)
    invalid_dated_ids = {id(e) for e in invalid_dated}

    graph = _union_identifiers(dated)
    groups: dict[str, list[dict[str, Any]]] = {}
    ungrouped: list[dict[str, Any]] = []
    for event in dated:
        before, after = _ident(event.get("before_state")), _ident(event.get("after_state"))
        root = graph.get(before) or graph.get(after)
        if root is None:
            ungrouped.append(event)
            continue
        groups.setdefault(root, []).append(event)
    for event in ungrouped:
        qa.append({"kind": "error", "code": "unresolvable_identifier", "event_id": event.get("event_id")})

    promoted_ids: dict[str, dict[str, Any]] = {}
    group_reports: list[dict[str, Any]] = []

    for root, members in sorted(groups.items()):
        identifiers = sorted({x for e in members
                              for x in (_ident(e.get("before_state")), _ident(e.get("after_state")))
                              if x not in DELETED})
        chain_id = _group_id(identifiers)
        # Order by date, then thread same-date events through their states.
        by_date: dict[str, list[dict[str, Any]]] = {}
        for event in members:
            by_date.setdefault(event["planned_effective_date"], []).append(event)
        ordered: list[dict[str, Any]] = []
        chain_ok = True
        reasons: list[dict[str, Any]] = []
        for day in sorted(by_date):
            block = _order_chain(sorted(by_date[day], key=_sort_key))
            if block is None:
                chain_ok = False
                reasons.append({"code": "chain_inconsistent", "detail": "same_date_events_do_not_thread",
                                "effective_date": day,
                                "event_ids": sorted(e.get("event_id") for e in by_date[day])})
                ordered.extend(sorted(by_date[day], key=_sort_key))
            else:
                ordered.extend(block)
        for left, right in zip(ordered, ordered[1:]):
            diff = _state_diff(left.get("after_state"), right.get("before_state"))
            if diff:
                chain_ok = False
                reasons.append({"code": "chain_inconsistent", "detail": "after_state_does_not_match_next_before_state",
                                "event_ids": [left.get("event_id"), right.get("event_id")],
                                "mismatched_fields": diff})

        # Every member reached this point with a parseable planned date, so no
        # ``date.max`` fallback is needed and none can drift into ``future``.
        past = [e for e in ordered if _day(e["planned_effective_date"]) <= anchor_day]
        future = [e for e in ordered if e not in past]

        anchor_ok = False
        anchor_evidence: dict[str, Any] = {}
        if not past:
            reasons.append({"code": "no_past_events", "detail": "chain has no event on or before the anchor date"})
        else:
            terminal = past[-1]
            terminal_state = terminal.get("after_state")
            terminal_id = _ident(terminal_state)
            if terminal_id in DELETED:
                # The chain ends in deletion: the anchor must not list it.
                lingering = sorted(x for x in identifiers if x in anchor_by_id)
                if lingering:
                    reasons.append({"code": "anchor_mismatch", "detail": "deleted_chain_still_present_in_anchor",
                                    "identifiers": lingering, "terminal_event_id": terminal.get("event_id")})
                else:
                    anchor_ok = True
                    anchor_evidence = {"terminal_kind": "absent_from_anchor",
                                       "checked_identifiers": identifiers}
            else:
                record = anchor_by_id.get(terminal_id)
                if record is None:
                    reasons.append({"code": "anchor_mismatch", "detail": "terminal_identifier_absent_from_anchor",
                                    "official_identifier": terminal_id,
                                    "terminal_event_id": terminal.get("event_id")})
                else:
                    diff = [f for f in ANCHOR_FIELDS
                            if _text(_anchor_state(record).get(f)) != _text((terminal_state or {}).get(f))]
                    if diff:
                        reasons.append({"code": "anchor_mismatch", "detail": "terminal_state_differs_from_anchor",
                                        "official_identifier": terminal_id,
                                        "mismatched_fields": diff,
                                        "terminal_event_id": terminal.get("event_id"),
                                        "anchor_source_page": record.get("source_page"),
                                        "anchor_source_line": record.get("source_line")})
                    else:
                        anchor_ok = True
                        anchor_evidence = {"terminal_kind": "present_in_anchor",
                                           "official_identifier": terminal_id,
                                           "anchor_source_page": record.get("source_page"),
                                           "anchor_source_line": record.get("source_line"),
                                           "anchor_source_line_sha256": record.get("source_line_sha256"),
                                           "matched_fields": list(ANCHOR_FIELDS)}

        # ``state_corroborated`` is the whole of what the terminal match proves:
        # the facility was in the terminal state by the anchor date.  Nothing
        # here says the intermediate changes happened on their planned dates.
        state_corroborated = chain_ok and anchor_ok
        promote = state_corroborated
        if state_corroborated:
            evidence_base = {"method": "chain_integrity_and_anchor_terminal_match",
                             "proves": "facility_reached_terminal_state_by_anchor_date",
                             "does_not_prove": "that_each_change_took_effect_on_its_planned_date",
                             "confirm_version": CONFIRM_VERSION,
                             "anchor_date": anchor_date,
                             "anchor_document_id": anchor_document_id,
                             "chain_id": chain_id,
                             "chain_identifiers": identifiers,
                             "chain_event_ids": [e.get("event_id") for e in ordered],
                             "chain_length": len(ordered),
                             "terminal_event_id": past[-1].get("event_id"),
                             **anchor_evidence}
            for event in past:
                promoted_ids[event["event_id"]] = evidence_base
        for event in future:
            qa.append({"kind": "info", "code": "future_event", "event_id": event.get("event_id"),
                       "chain_id": chain_id, "planned_effective_date": event.get("planned_effective_date"),
                       "anchor_date": anchor_date})
        if not promote:
            for reason in reasons:
                # A chain made only of future-dated events is not a defect;
                # there is simply nothing to confirm yet.
                kind = "info" if reason["code"] == "no_past_events" else "error"
                qa.append({"kind": kind, "chain_id": chain_id, "chain_identifiers": identifiers, **reason})
            for event in past:
                qa.append({"kind": "info", "code": "not_promoted", "event_id": event.get("event_id"),
                           "chain_id": chain_id,
                           "reasons": sorted({r["code"] for r in reasons}) or ["unknown"]})
        group_reports.append({"chain_id": chain_id, "identifiers": identifiers,
                              "event_count": len(ordered), "past_event_count": len(past),
                              "future_event_count": len(future), "chain_consistent": chain_ok,
                              "anchor_terminal_match": anchor_ok, "promoted": promote,
                              "state_corroborated": state_corroborated,
                              "reason_codes": sorted({r["code"] for r in reasons})})

    used_observation_ids: set[str] = set()
    out_events: list[dict[str, Any]] = []
    for event in sorted(rows, key=_sort_key):
        event = dict(event)
        eid = event.get("event_id")
        corroborated = eid in promoted_ids
        observation = observed_dates.get(eid) if eid else None
        date_confirmed = False
        date_basis = "unknown"
        if corroborated:
            event["event_status"] = "confirmed"
            event["confirmation_evidence"] = promoted_ids[eid]
            event["review_status"] = "confirmed"
            date_basis = "planned"
            if observation is not None:
                used_observation_ids.add(eid)
                if observation.get("conflicting"):
                    date_basis = "unknown"
                    qa.append({"kind": "error", "code": "date_observation_conflict",
                               "event_id": eid, "detail": "observations_disagree"})
                elif observation["observed_effective_date"] == event.get("planned_effective_date"):
                    date_confirmed = True
                    date_basis = "observed"
                else:
                    # A later observation that puts the change on a different
                    # day disproves the planned date; it does not silently
                    # replace it, because a single observation cannot tell a
                    # postponement from a mis-parse.
                    date_basis = "unknown"
                    qa.append({"kind": "error", "code": "date_observation_conflict",
                               "event_id": eid,
                               "planned_effective_date": event.get("planned_effective_date"),
                               "observed_effective_date": observation["observed_effective_date"]})
        else:
            event["event_status"] = "announced"
            event.setdefault("confirmation_evidence", None)
            if eid in dropped_ids:
                event["superseded_reason"] = "duplicate_announcement"
            if observation is not None:
                used_observation_ids.add(eid)
                qa.append({"kind": "error", "code": "date_observation_for_uncorroborated_event",
                           "event_id": eid})
        event["state_corroborated"] = corroborated
        event["effective_date_confirmed"] = date_confirmed
        event["date_basis"] = date_basis
        event["confirmed_effective_date"] = (observation["observed_effective_date"]
                                             if date_confirmed else None)
        if date_confirmed:
            event["date_evidence"] = {"observation_kind": observation["observation_kind"],
                                      "source_document_id": observation.get("source_document_id"),
                                      "observed_at": observation.get("observed_at"),
                                      "observed_effective_date": observation["observed_effective_date"]}
        else:
            event.setdefault("date_evidence", None)
        out_events.append(event)

    for eid in sorted(set(observed_dates) - used_observation_ids):
        qa.append({"kind": "error", "code": "date_observation_unknown_event", "event_id": eid})

    corroborated_count = sum(e["state_corroborated"] for e in out_events)
    date_confirmed_count = sum(e["effective_date_confirmed"] for e in out_events)
    basis_counts = {key: sum(e["date_basis"] == key for e in out_events) for key in sorted(DATE_BASIS)}
    reason_counts: dict[str, int] = {}
    for row in qa:
        if row.get("code") == "not_promoted":
            for code in row.get("reasons", []):
                reason_counts[code] = reason_counts.get(code, 0) + 1
    future_count = sum(1 for row in qa if row.get("code") == "future_event")
    metadata = {
        "confirm_version": CONFIRM_VERSION,
        "anchor_date": anchor_date,
        "anchor_document_id": anchor_document_id,
        **anchor_check,
        "input_event_count": len(rows),
        "deduplicated_event_count": len(kept),
        "duplicate_announcement_count": len(duplicates),
        "chain_count": len(group_reports),
        "promoted_chain_count": sum(1 for g in group_reports if g["promoted"]),
        "state_corroborated_chain_count": sum(1 for g in group_reports if g["state_corroborated"]),
        # Retained under the historical name: the count of events whose state
        # change is corroborated.  It has never meant a confirmed *date*.
        "confirmed_event_count": corroborated_count,
        "state_corroborated_event_count": corroborated_count,
        "effective_date_confirmed_event_count": date_confirmed_count,
        "date_basis_counts": basis_counts,
        "date_observation_count": len(observed_dates),
        "date_evidence_note": (
            "state_corroborated proves only that the facility had reached the terminal "
            "state by the anchor date.  effective_date_confirmed is true only where a "
            "separate later observation fixed the date; otherwise the effective date "
            "used downstream is the notice's planned date (date_basis=planned)."),
        "announced_event_count": len(out_events) - corroborated_count,
        "future_event_count": future_count,
        "missing_planned_date_count": len(undated),
        "invalid_planned_date_count": len(invalid_dated),
        "unresolvable_identifier_count": len(ungrouped),
        "not_promoted_reason_counts": reason_counts,
        "qa_error_count": sum(1 for row in qa if row.get("kind") == "error"),
        "public_release_allowed": False,
        "contains_detailed_addresses": True,
        "processing_display": "internal research only; source PDF not copied",
    }
    return {"events": out_events, "qa": qa, "chains": group_reports, "metadata": metadata}


def disadvantaged_area_history(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Per-identifier timeline of the official depopulated-area flag.

    The 過疎地変更 notices are the primary evidence for when an area became a
    designated depopulated area, which the enforcement regulation ties the
    universal-service baseline to.  Corrections are kept but labelled so they
    are not mistaken for a real-world change.
    """
    out = []
    for event in events:
        if event.get("event_type") not in {"disadvantaged_area_change", "disadvantaged_area_correction"}:
            continue
        before, after = event.get("before_state") or {}, event.get("after_state") or {}
        out.append({
            "official_identifier": _ident(before) or _ident(after),
            "planned_effective_date": event.get("planned_effective_date"),
            "confirmed_effective_date": event.get("confirmed_effective_date"),
            "state_corroborated": bool(event.get("state_corroborated")),
            "effective_date_confirmed": bool(event.get("effective_date_confirmed")),
            "date_basis": event.get("date_basis", "unknown"),
            "event_status": event.get("event_status"),
            "event_type": event.get("event_type"),
            "is_correction": event.get("event_type") == "disadvantaged_area_correction",
            "disadvantaged_area_before": (before.get("disadvantaged_area") or {}).get("normalized"),
            "disadvantaged_area_after": (after.get("disadvantaged_area") or {}).get("normalized"),
            "name": (after.get("name") or before.get("name") or {}).get("normalized"),
            "address": (after.get("address") or before.get("address") or {}).get("normalized"),
            "event_id": event.get("event_id"),
            "source_document_id": event.get("source_document_id"),
            "source_document_date": event.get("source_document_date"),
        })
    out.sort(key=lambda r: (r["official_identifier"] or "", r["planned_effective_date"] or ""))
    return out


def load_change_events(paths: Iterable[str | Path], *, exclude_dates: Iterable[str] = ()) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load parsed change events from ``events.jsonl`` files or their parents.

    A directory is expanded to ``*/events.jsonl`` so a whole
    ``data/silver/change_events`` tree can be passed in one argument.  Sources
    listed in ``exclude_dates`` are skipped and reported, never dropped
    silently.
    """
    excluded = set(exclude_dates)
    files: list[Path] = []
    for entry in paths:
        p = Path(entry)
        if p.is_dir():
            files.extend(sorted(p.glob("*/events.jsonl")))
            if (p / "events.jsonl").is_file():
                files.append(p / "events.jsonl")
        elif p.is_file():
            files.append(p)
        else:
            raise ConfirmError("event input rejected")
    if not files:
        raise ConfirmError("no event files found")
    events: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for path in sorted(set(files)):
        label = path.parent.name
        if label in excluded:
            sources.append({"coverage_date": label, "path": str(path), "included": False,
                            "reason": "excluded_by_caller"})
            continue
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        events.extend(rows)
        sources.append({"coverage_date": label, "path": str(path), "included": True,
                        "event_count": len(rows)})
    return events, sources


def write_confirm_bundle(result: dict[str, Any], output_dir: str | Path, *,
                         acknowledge_internal_use: bool = False, replace: bool = False) -> dict[str, str]:
    if not acknowledge_internal_use:
        raise ConfirmError("writing confirmed events requires --acknowledge-internal-use")
    out = Path(output_dir)
    targets = {"metadata": out / "metadata.json", "events": out / "events.jsonl",
               "qa": out / "qa.jsonl", "chains": out / "chains.jsonl",
               "disadvantaged_area_history": out / "disadvantaged_area_history.jsonl"}
    if not replace and any(p.exists() for p in targets.values()):
        raise ConfirmError("output exists; pass --replace")
    out.mkdir(parents=True, exist_ok=True)
    payloads = {**result, "disadvantaged_area_history": disadvantaged_area_history(result["events"])}
    staged: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        for key, target in targets.items():
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=out)
            os.close(fd)
            temp = Path(name)
            staged.append((target, temp))
            with temp.open("w", encoding="utf-8", newline="\n") as stream:
                if key == "metadata":
                    json.dump(payloads["metadata"], stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                else:
                    for row in payloads[key]:
                        stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        try:
            for target, _ in staged:
                if target.exists():
                    fd, name = tempfile.mkstemp(prefix=f".{target.name}.backup.", dir=out)
                    os.close(fd)
                    backup = Path(name)
                    backup.unlink()
                    os.replace(target, backup)
                    backups.append((target, backup))
            for target, temp in staged:
                os.replace(temp, target)
                committed.append(target)
        except Exception as exc:
            for target in committed:
                if target.exists():
                    target.unlink()
            for target, backup in reversed(backups):
                if backup.exists():
                    os.replace(backup, target)
            raise ConfirmError("confirmed-event output commit failed") from exc
        for _, backup in backups:
            if backup.exists():
                backup.unlink()
    finally:
        for _, temp in staged:
            if temp.exists():
                temp.unlink()
    return {k: str(v) for k, v in targets.items()}
