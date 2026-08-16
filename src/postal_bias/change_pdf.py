"""Parser for Japan Post monthly change-notice PDFs (local input only)."""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import date
from pathlib import Path
from typing import Any

from .current_list import CIRCLES, DASHES, ID_RE, MARK_RE, PREFECTURES, _norm_mark, _norm_space, run_pdftotext, validate_pdf, source_document_id

PARSER_VERSION = "0.2.4"
DATE_RE = re.compile(r"(?P<era>[MTSHR])?\s*(?P<year>\d{1,4})[.．年](?P<month>\d{1,2})[.．月](?P<day>\d{1,2})日?", re.I)
ROW_RE = re.compile(r"^\s*(変更前|変更後)\s+(.*)$")
SIDE_RE = re.compile(r"変更前|変更後")
# Longest-first matching: a reason cell may combine several labels and the more
# specific label must win over a substring of itself.
REASONS = {
    "契約締結": "contract_concluded", "契約解除": "contract_terminated", "廃止": "abolished",
    "移転": "relocation", "住居表示変更": "address_change", "住所表示変更": "address_change",
    "改称": "renamed",
    "業務変更": "service_change", "局種変更": "office_type_change", "一時閉鎖": "temporarily_closed",
    "再開": "reopened",
    # A new directly-operated office opening.  Kept distinct from
    # ``contract_concluded`` (a simple post office contract) because the two
    # have different legal character under the universal-service rules.
    "新設": "established",
    # Change of the official "depopulated area" flag only; the facility itself
    # is unchanged.  Primary evidence for the area-by-area baseline dates in
    # the enforcement regulation.
    "過疎地区分の変更": "disadvantaged_area_change", "過疎地区分変更": "disadvantaged_area_change",
    "過疎地変更": "disadvantaged_area_change",
    # Errata: the previously published depopulated-area flag was wrong.  Kept
    # apart from a real-world change so analysis can exclude retroactive
    # record corrections from event counts.
    "過疎地該否の修正": "disadvantaged_area_correction",
}
SECTION_MAP = {"郵便局": "postal_office", "営業所": "company_office", "-": "blank"}


class ChangePdfError(ValueError):
    pass


#: Only an unambiguous calendar day is accepted as a date field.  ``date.
#: fromisoformat`` alone would also take ``20260730`` and ``2026-07-30T00:00``,
#: which are not the form the ledger (spec 6.1) records.
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def iso_date(value: Any) -> str | None:
    """Return the ISO day, or ``None`` when ``value`` is not one."""
    text = str(value or "").strip()
    if not ISO_DATE_RE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def normalize_era_date(raw: str) -> str | None:
    match = DATE_RE.search(raw)
    if not match:
        return None
    era = match.group("era").upper() if match.group("era") else None
    year = int(match.group("year")); month = int(match.group("month")); day = int(match.group("day"))
    if era == "R": year += 2018
    elif era == "H": year += 1988
    elif era == "S": year += 1925
    elif era == "T": year += 1911
    if era is None and year < 100:  # conservative fallback for two-digit western years
        year += 2000
    try:
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def _field(raw: str, kind: str = "text") -> dict[str, str]:
    raw = raw.strip()
    if kind == "mark":
        normalized = _norm_mark(raw)
    elif raw == "-":
        normalized = "deleted"
    elif not raw:
        normalized = "blank"
    else:
        normalized = _norm_space(raw)
    return {"raw": raw, "normalized": normalized}


def _find_address_start(text: str) -> int | None:
    # Same overlapping-prefecture hazard as the current-list parser: 東京都府中市
    # also matches 京都府 one character in.  See current_list._find_address_start.
    spans = sorted({(m.start(), m.end()) for pref in PREFECTURES for m in re.finditer(re.escape(pref), text)})
    outer = [start for index, (start, end) in enumerate(spans)
             if not any(other_start < start < other_end for other_start, other_end in spans[:index])]
    return max(outer) if outer else None


def _parse_state_line(line: str, page: int, line_no: int) -> tuple[str, dict[str, Any], list[dict[str, Any]]]:
    match = ROW_RE.match(line)
    if not match:
        raise ChangePdfError("not a state row")
    side = "before" if match.group(1) == "変更前" else "after"
    tokens = match.group(2).split(None, 2)
    if len(tokens) < 3:
        raise ChangePdfError("state row has fewer than type/id/name columns")
    section_raw, identifier, body = tokens
    if section_raw not in SECTION_MAP:
        raise ChangePdfError("unknown office section")
    if identifier != "-" and not ID_RE.fullmatch(identifier):
        raise ChangePdfError("invalid six-digit identifier")
    if identifier == "-":
        state = {
            "section": {"raw": section_raw, "normalized": SECTION_MAP[section_raw]}, "official_identifier": _field("-"),
            "name": _field("-"), "simple_post_office": _field("-", "mark"),
            "address": _field("-"), "disadvantaged_area": _field("-", "mark"),
            "services": [_field("-", "mark") for _ in range(5)],
            "related_office": _field("-"), "notes": _field("-")
        }
        return side, state, []
    address_start = _find_address_start(body)
    if address_start is None:
        gap = re.search(r"\s{2,}", body)
        if not gap:
            raise ChangePdfError("address start cannot be determined")
        address_start = gap.end()
    before_address = body[:address_start].rstrip()
    simple_raw = ""
    if before_address.endswith((*CIRCLES, "-")):
        simple_raw = before_address[-1]; before_address = before_address[:-1].rstrip()
    marks = list(MARK_RE.finditer(body[address_start:]))
    # One depopulated-area mark plus five service marks are required; indexing
    # ``marks[5]`` below needs six, not five.
    if len(marks) < 6:
        raise ChangePdfError("fewer than five service markers")
    first_marker = address_start + marks[0].start()
    address_raw = body[address_start:first_marker].rstrip()
    address_raw = re.sub(r"([\-‐‑‒–—−－])\s+(?=[0-9０-９])", r"\1", address_raw)
    marker_values = [m.group(0) for m in marks]
    # First marker is disadvantaged-area, next five are services; later marks
    # are related-office/notes column values.
    disadvantaged_raw = marker_values[0]
    service_values = marker_values[1:6]
    trailing = body[address_start + marks[5].end():].strip()
    related_raw, notes_raw = (trailing.split("|", 1) + [""])[:2] if "|" in trailing else (trailing, "")
    state = {
        "section": {"raw": section_raw, "normalized": SECTION_MAP[section_raw]}, "official_identifier": _field(identifier),
        "name": _field(before_address), "simple_post_office": _field(simple_raw, "mark"),
        "address": _field(address_raw), "disadvantaged_area": _field(disadvantaged_raw, "mark"),
        "services": [_field(v, "mark") for v in service_values],
        "related_office": _field(related_raw), "notes": _field(notes_raw),
    }
    return side, state, []


def _event_type(reason: str) -> str:
    for label, value in REASONS.items():
        if label in reason:
            return value
    return "unknown"


def _event_types(reason: str) -> list[str]:
    """Every reason label present in the cell.

    A single cell often combines labels (``局種変更・移転・業務変更``).
    ``_event_type`` keeps the historical single-value behaviour; this returns
    the full set so downstream analysis is not forced to guess.
    """
    return sorted({value for label, value in REASONS.items() if label in reason})


def parse_change_text(text: str, *, source_pdf_sha256: str = "", source_document_date: str | None = None) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    qa: list[dict[str, Any]] = []
    pending_before: dict[str, Any] | None = None
    pending_after: dict[str, Any] | None = None
    pending_before_prov: tuple[int, int] | None = None
    pending_after_prov: tuple[int, int] | None = None
    planned_raw: str | None = None
    reason_raw: str | None = None
    reason_prov: tuple[int, int] | None = None
    raw_pending: tuple[str, str, int, int] | None = None
    orphan_continuation: list[str] = []
    # The reason cell wraps across physical lines while the date sits alone on
    # its own line.  Text left of the 変更前/変更後 column on the surrounding
    # lines is the remainder of that cell.
    reason_fragments: list[str] = []
    side_column: int | None = None
    address_column: int | None = None

    def append_continuation(state: dict[str, Any], line: str) -> None:
        # A hyphen-only state is an explicit deletion sentinel; following
        # footer/header text must never be folded into its address.
        if state.get("official_identifier", {}).get("raw") == "-":
            return
        value = _norm_space(line)
        if value:
            old = state["address"]["raw"]
            state["address"]["raw"] = (old + value).strip() if old not in ("", "-") else value
            state["address"]["normalized"] = _norm_space(state["address"]["raw"])

    def address_needs_continuation(state: dict[str, Any]) -> bool:
        value = state["address"]["raw"]
        # ``-`` by itself denotes a deleted state and must not consume a
        # subsequent footer/header as a wrapped address.
        if value == "-" or state.get("official_identifier", {}).get("raw") == "-":
            return False
        return value == "" or value.endswith(("-", "‐", "‑", "‒", "–", "—", "−", "－"))

    def is_address_tail(line: str, column: int | None) -> bool:
        """A short remainder of an address that wrapped without a trailing dash.

        Long addresses overflow the fixed-width address cell and the overflow
        is emitted on its own line, indented into the address column.  Without
        this the stored address is silently truncated (…４６６６−３５５ for
        …４６６６−３５５０), which then fails the anchor comparison.  The
        indent and character-class tests keep footers and notes out.
        """
        if column is None:
            return False
        body = line.strip()
        indent = len(line) - len(line.lstrip())
        if not body or len(body) > 12 or indent < column - 2:
            return False
        return all(ch.isdigit() or ch in DASHES or ord(ch) > 0x2E80 for ch in body)

    def is_document_chrome(line: str) -> bool:
        """Identify repeated table headers and explanatory footnotes."""
        stripped = line.strip()
        if not stripped:
            return True
        phrases = (
            "６条第２項", "第６条第２項", "郵便局及び法", "変更年月日",
            "郵便局又は当該営業所", "当該郵便局又は当該営業所",
            "関連銀行又は関連保険会", "業所の別", "業所の", "社の営", "局・会",
            "(1)郵便", "整理", "簡易郵便局」の欄",
        )
        return stripped.startswith("別") or any(p in stripped for p in phrases)

    def finalize() -> None:
        nonlocal pending_before, pending_after, planned_raw, reason_raw, pending_before_prov, pending_after_prov, reason_prov, reason_fragments
        if pending_before is None or pending_after is None:
            return
        if planned_raw is None or reason_raw is None:
            qa.append({"kind": "error", "code": "unmatched_pair_date_or_reason", "page": (pending_before_prov or (0, 0))[0], "line": (pending_before_prov or (0, 0))[1]})
        else:
            page, line = pending_before_prov or pending_after_prov or (0, 0)
            reason_source = "date_line"
            # The date line carries no reason text when the cell wrapped above
            # and below it; rebuild from the reason-column fragments instead of
            # emitting an unclassifiable event.
            if not reason_raw.strip(" " + DASHES) and reason_fragments:
                rebuilt = "".join(reason_fragments).strip()
                if rebuilt:
                    reason_raw = rebuilt
                    reason_source = "reason_column_fragments"
                    qa.append({"kind": "info", "code": "reason_reconstructed_from_column",
                               "reason_raw": reason_raw, "page": page, "line": line})
            ident = pending_before["official_identifier"]["raw"] if pending_before["official_identifier"]["raw"] != "-" else pending_after["official_identifier"]["raw"]
            basis = "|".join([source_pdf_sha256, str(page), str(line), planned_raw, reason_raw, ident])
            event_id = hashlib.sha256(basis.encode("utf-8")).hexdigest()
            event_kind = _event_type(reason_raw)
            if event_kind == "unknown":
                qa.append({"kind": "error", "code": "unknown_reason", "reason_raw": reason_raw, "page": page, "line": line})
            planned_iso = normalize_era_date(planned_raw)
            if planned_iso is None:
                # A change-date cell that will not normalize is a parse defect.
                # Downstream it would sort as "far future" and be filed as a
                # not-yet-happened event, so it is raised here instead.
                qa.append({"kind": "error", "code": "invalid_change_date",
                           "change_date_raw": planned_raw[:32], "page": page, "line": line})
            events.append({
                "event_id": event_id, "source_pdf_sha256": source_pdf_sha256,
                "source_document_id": source_document_id(source_pdf_sha256),
                "source_document_date": source_document_date, "source_page": page,
                "notice_date": source_document_date,
                "source_line": line, "change_date_raw": planned_raw,
                "planned_effective_date": planned_iso,
                "planned_effective_date_valid": planned_iso is not None,
                "confirmed_effective_date": None, "event_status": "announced",
                "state_corroborated": False, "effective_date_confirmed": False,
                "date_basis": "unknown",
                "reason_raw": reason_raw, "event_type": event_kind,
                "event_types": _event_types(reason_raw), "reason_source": reason_source,
                "before_state": pending_before, "after_state": pending_after,
                "parse_confidence": "high" if event_kind != "unknown" else "low",
                "facility_entity_id": None, "provenance": "official",
                "corroborated_by": [], "revision_of_event_id": None,
                "cancelled_by_event_id": None, "review_status": "unmatched",
            })
        pending_before = pending_after = None
        planned_raw = reason_raw = None
        pending_before_prov = pending_after_prov = reason_prov = None
        reason_fragments = []

    for page_no, page_text in enumerate(text.split("\f"), 1):
        for line_no, raw_line in enumerate(page_text.splitlines(), 1):
            line = raw_line.rstrip("\r")
            if is_document_chrome(line):
                # Preserve an incomplete physical row while skipping the
                # repeated header/footer; it will be completed by the next
                # continuation/record line.  Parsed deletion/complete rows
                # are finalized before the chrome is ignored.
                if raw_pending is None and pending_after is not None and not address_needs_continuation(pending_after):
                    finalize()
                orphan_continuation = []
                continue
            if not line.strip() or "変更年月日" in line or "法第６条" in line or "別紙" in line:
                orphan_continuation = []
                continue
            if line.lstrip().startswith("注"):
                orphan_continuation = []
                continue
            date_match = DATE_RE.search(line)
            if date_match:
                if raw_pending is not None:
                    raw_side, raw_value, raw_page, raw_line_no = raw_pending
                    try:
                        parsed_side, state, _ = _parse_state_line(raw_value, raw_page, raw_line_no)
                        if parsed_side == "before":
                            if pending_after is not None:
                                finalize()
                            pending_before, pending_before_prov = state, (raw_page, raw_line_no)
                        else:
                            pending_after, pending_after_prov = state, (raw_page, raw_line_no)
                        orphan_continuation = []
                    except ChangePdfError as exc:
                        qa.append({"kind": "error", "code": "state_row_parse_error", "page": raw_page, "line": raw_line_no, "detail": str(exc)})
                    raw_pending = None
                planned_raw = date_match.group(0).strip()
                reason_raw = line[date_match.end():].strip()
                reason_prov = (page_no, line_no)
                # Text banked before the date line is a wrapped cell of the
                # 変更前 row above it.  Carrying it past the date line splices
                # it into the 変更後 row and destroys that row's columns.
                orphan_continuation = []
                continue
            if ROW_RE.match(line):
                if raw_pending is not None:
                    raw_side, raw_value, raw_page, raw_line_no = raw_pending
                    try:
                        parsed_side, old_state, _ = _parse_state_line(raw_value, raw_page, raw_line_no)
                        if parsed_side == "before":
                            if pending_after is not None:
                                finalize()
                            pending_before, pending_before_prov = old_state, (raw_page, raw_line_no)
                        else:
                            pending_after, pending_after_prov = old_state, (raw_page, raw_line_no)
                        orphan_continuation = []
                    except ChangePdfError as exc:
                        qa.append({"kind": "error", "code": "state_row_unmatched_continuation", "page": raw_page, "line": raw_line_no, "detail": str(exc)})
                    raw_pending = None
                candidate_line = line
                if orphan_continuation and _find_address_start(line) is None:
                    first_mark = next(MARK_RE.finditer(line), None)
                    if first_mark:
                        insert_at = first_mark.end()
                    else:
                        insert_at = len(line)
                    orphan_text = "".join(part.strip() for part in orphan_continuation)
                    candidate_line = line[:insert_at] + orphan_text + line[insert_at:]
                try:
                    side, state, _ = _parse_state_line(candidate_line, page_no, line_no)
                except ChangePdfError as exc:
                    if "fewer than five service markers" in str(exc):
                        if orphan_continuation:
                            first_mark = next(MARK_RE.finditer(line), None)
                            if first_mark and _find_address_start(line) is None:
                                insert_at = first_mark.end()
                            else:
                                insert_at = first_mark.start() if first_mark else len(line)
                            orphan_text = "".join(part.strip() for part in orphan_continuation)
                            candidate_line = line[:insert_at] + orphan_text + line[insert_at:]
                            try:
                                side, state, _ = _parse_state_line(candidate_line, page_no, line_no)
                            except ChangePdfError:
                                raw_pending = ("", candidate_line, page_no, line_no)
                                continue
                        else:
                            raw_pending = ("", line, page_no, line_no)
                            continue
                    else:
                        qa.append({"kind": "error", "code": "state_row_parse_error", "page": page_no, "line": line_no, "detail": str(exc)})
                        continue
                side_match = SIDE_RE.search(line)
                if side_match:
                    side_column = side_match.start()
                address_column = _find_address_start(candidate_line)
                if side == "before":
                    if pending_after is not None or pending_before is not None:
                        finalize()
                    reason_fragments = []
                    pending_before, pending_before_prov = state, (page_no, line_no)
                else:
                    if pending_before is None:
                        qa.append({"kind": "error", "code": "after_without_before", "page": page_no, "line": line_no})
                    else:
                        pending_after, pending_after_prov = state, (page_no, line_no)
                orphan_continuation = []
                continue
            if side_column:
                fragment = line[:side_column].strip()
                if fragment and not DATE_RE.search(fragment):
                    reason_fragments.append(fragment)
            if raw_pending is not None:
                raw_pending = (raw_pending[0], raw_pending[1] + " " + line.strip(), raw_pending[2], raw_pending[3])
                continue
            if pending_after is not None:
                if address_needs_continuation(pending_after) or is_address_tail(line, address_column):
                    append_continuation(pending_after, line)
                else:
                    finalize()
                    orphan_continuation.append(line.strip())
                    orphan_continuation = orphan_continuation[-3:]
            elif pending_before is not None and planned_raw is None and address_needs_continuation(pending_before):
                append_continuation(pending_before, line)
            elif line.strip():
                orphan_continuation.append(line.strip())
                orphan_continuation = orphan_continuation[-3:]
    if pending_before is not None or pending_after is not None:
        finalize()
    seen: set[str] = set()
    pair_fingerprints: dict[tuple[str | None, str, str], str] = {}
    for event in events:
        if event["event_id"] in seen:
            qa.append({"kind": "error", "code": "duplicate_event_id", "event_id": event["event_id"]})
        seen.add(event["event_id"])
        pair_key = (event["planned_effective_date"], event["before_state"]["official_identifier"]["raw"], event["after_state"]["official_identifier"]["raw"])
        fingerprint = hashlib.sha256(json.dumps((event["before_state"], event["after_state"]), ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        if pair_key in pair_fingerprints:
            code = "duplicate_event_pair" if pair_fingerprints[pair_key] == fingerprint else "contradictory_event_pair"
            qa.append({"kind": "error", "code": code, "event_id": event["event_id"]})
        else:
            pair_fingerprints[pair_key] = fingerprint
    invalid_dates = sum(1 for e in events if not e["planned_effective_date_valid"])
    return {"events": events, "qa": qa, "metadata": {"parser_version": PARSER_VERSION,
            "source_document_id": source_document_id(source_pdf_sha256),
            "parsed_source_pdf_sha256": source_pdf_sha256,
            "event_count": len(events), "invalid_change_date_count": invalid_dates,
            "qa_error_count": sum(x.get("kind") == "error" for x in qa)}}


def write_change_outputs(result: dict[str, Any], pdf_path: str | Path, coverage_date: str,
                         output_dir: str | Path, *, replace: bool = False,
                         acknowledge_internal_use: bool = False, source_pdf_sha256: str = "",
                         document_published_date: str | None = None) -> dict[str, str]:
    """Write the change-event bundle.

    Three dates that spec 6.2 keeps apart used to arrive as one ``coverage_date``
    string that was stored unchecked, so ``"not-a-date"`` reached the metadata:

    ``coverage_date``
        The date in the document's name, used as the bundle's partition key.
    ``document_published_date``
        When the notice itself was published (spec 6.1 ``published_at``).
        Defaults to the ``source_document_date`` the events were parsed with.
    ``change_effective_date_from`` / ``_to``
        The span of *planned effective* dates the notice announces - a change
        notice dated October may only take effect months later.

    Every one of them must be an ISO calendar day; anything else stops the run.
    """
    from .current_list import _stage_text
    if not acknowledge_internal_use:
        raise ChangePdfError("writing detailed change events requires --acknowledge-internal-use")
    coverage_day = iso_date(coverage_date)
    if coverage_day is None:
        raise ChangePdfError("coverage date must be an ISO calendar day (YYYY-MM-DD)")
    pdf = validate_pdf(pdf_path)
    # The caller-supplied digest is a claim about the file, not evidence.
    # Recompute it and refuse to seal provenance that does not match the bytes
    # actually on disk.
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    if source_pdf_sha256 and source_pdf_sha256.strip().lower() != digest:
        raise ChangePdfError("declared source PDF SHA-256 does not match the file")
    # ``parse_change_text`` may legitimately be called without a digest (the
    # text is then self-describing only).  A digest that *is* present must be
    # the one this file hashes to.
    parsed_digest = str(result.get("metadata", {}).get("parsed_source_pdf_sha256") or "").strip().lower()
    if parsed_digest and parsed_digest != digest:
        raise ChangePdfError("parsed events were produced from a different source PDF")
    document_id = source_document_id(digest)
    out = Path(output_dir); paths = {k: str(out / f) for k, f in (("metadata", "metadata.json"), ("events", "events.jsonl"), ("qa", "qa.jsonl"))}
    if not replace:
        for p in paths.values():
            if Path(p).exists(): raise ChangePdfError("output exists; pass --replace")
    qa_rows = list(result["qa"])
    # 文書公表日: taken from the events unless the caller states it.  Events that
    # disagree with each other are a defect, not something to average over.
    event_published = sorted({str(e.get("source_document_date"))
                              for e in result["events"] if e.get("source_document_date")})
    if len(event_published) > 1:
        qa_rows.append({"kind": "error", "code": "source_document_date_conflict",
                        "dates": event_published[:10]})
    declared_published = (document_published_date if document_published_date is not None
                          else (event_published[0] if len(event_published) == 1 else None))
    published_day: str | None = None
    if declared_published is not None:
        published_day = iso_date(declared_published)
        if published_day is None:
            raise ChangePdfError("document published date must be an ISO calendar day (YYYY-MM-DD)")
    # 変更実施日: the span the notice actually announces, kept apart from both
    # dates above.  Unparseable planned dates are counted, never guessed.
    planned_days = sorted(d for d in (iso_date(e.get("planned_effective_date"))
                                      for e in result["events"]) if d)
    planned_unknown = sum(1 for e in result["events"]
                          if iso_date(e.get("planned_effective_date")) is None)
    # Every event in this bundle comes from this one document.  Keeping a
    # differing per-event id left two provenance chains for one PDF, so the
    # value is unified and the divergence recorded rather than preserved.
    events_for_output = []
    reassigned = 0
    for event in result["events"]:
        prior_id = event.get("source_document_id")
        prior_sha = event.get("source_pdf_sha256")
        if (prior_id and prior_id != document_id) or (prior_sha and prior_sha != digest):
            reassigned += 1
            qa_rows.append({"kind": "error", "code": "source_document_id_reassigned",
                            "event_id": event.get("event_id"),
                            "previous_source_document_id": prior_id,
                            "source_document_id": document_id})
        events_for_output.append({**event, "source_pdf_sha256": digest,
                                  "source_document_id": document_id})
    metadata = {**result["metadata"], "source_pdf_sha256": digest, "source_document_id": document_id,
                "source_pdf_sha256_verified": True, "coverage_date": coverage_day,
                "coverage_date_basis": "document_name_date",
                "document_published_date": published_day,
                "change_effective_date_from": planned_days[0] if planned_days else None,
                "change_effective_date_to": planned_days[-1] if planned_days else None,
                "change_effective_date_unknown_count": planned_unknown,
                "date_field_note": ("coverage_date is the date in the document name and is the "
                                    "bundle partition key; document_published_date is when the "
                                    "notice was published; change_effective_date_from/to span the "
                                    "planned effective dates the notice announces.  The three are "
                                    "different facts and are never merged."),
                "source_document_id_reassigned_count": reassigned,
                "qa_error_count": sum(x.get("kind") == "error" for x in qa_rows),
                "terms_review_status": "unverified_detail_pdf", "public_release_allowed": False,
                "contains_detailed_addresses": True, "processing_display": "internal research only; source PDF not copied"}
    staged: list[str] = []
    try:
        staged.append(_stage_text(Path(paths["metadata"]), lambda s: (json.dump(metadata, s, ensure_ascii=False, indent=2), s.write("\n"))))
        staged.append(_stage_text(Path(paths["events"]), lambda s: [s.write(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n") for x in events_for_output]))
        staged.append(_stage_text(Path(paths["qa"]), lambda s: [s.write(json.dumps(x, ensure_ascii=False, separators=(",", ":")) + "\n") for x in qa_rows]))
    except Exception as exc:
        for temp in staged:
            if Path(temp).exists(): Path(temp).unlink()
        raise ChangePdfError("cannot stage change-event output files") from exc
    targets = [Path(paths["metadata"]), Path(paths["events"]), Path(paths["qa"])]
    backups: list[tuple[Path, str]] = []; committed: list[Path] = []
    try:
        for target in targets:
            if target.exists():
                fd, backup = tempfile.mkstemp(prefix=f".{target.name}.backup.", dir=target.parent)
                os.close(fd); os.unlink(backup)
                os.replace(target, backup); backups.append((target, backup))
        for target, temp in zip(targets, staged):
            os.replace(temp, target); committed.append(target)
    except Exception as exc:
        for target in committed:
            if target.exists() and target.is_file(): target.unlink()
        for target, backup in reversed(backups):
            if os.path.exists(backup): os.replace(backup, target)
        for temp in staged:
            if Path(temp).exists(): Path(temp).unlink()
        raise ChangePdfError("change-event output commit failed; previous set restored") from exc
    else:
        for _, backup in backups:
            if os.path.exists(backup): os.unlink(backup)
    return paths
