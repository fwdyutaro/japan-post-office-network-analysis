"""Fail-closed input checks and tri-state marks for the geographic pipeline.

The geocoding and accessibility steps consume the current-list records and the
geocoded records produced from them.  Both used to carry on regardless of what
they found in the input: a name that did not end in a facility suffix (the
signature of a name/address column split landing one character late) was
printed and ignored, and a cell whose mark could not be recognised was folded
to ``False``, which is indistinguishable from a cell that genuinely said "no".

These helpers make both cases explicit.  Anomalies stop the run unless the
caller asks for them to be excluded, in which case the excluded records are
returned and counted; unknown marks stay ``unknown`` all the way to the output.
"""
from __future__ import annotations

import math
import re
from typing import Any, Iterable

#: Every official facility name ends in one of these.
NAME_TAIL_RE = re.compile(r"(郵便局|分室|出張所)$")

#: Tri-state cell value.  ``unknown`` is a real answer and is never collapsed.
YES, NO, UNKNOWN = "yes", "no", "unknown"

#: Normalized current-list mark values that mean "no / not applicable / blank".
_NEGATIVE = {"no", "not_applicable", "blank", ""}


class GeoInputError(ValueError):
    """Input rejected.  ``details`` carries structured, address-free reasons."""

    def __init__(self, message: str, details: list[dict[str, Any]] | None = None) -> None:
        super().__init__(message)
        self.details = details or []


def mark_state(value: Any) -> str:
    """Fold a current-list mark onto ``yes`` / ``no`` / ``unknown``.

    Accepts either the ``{"raw":..., "normalized":...}`` cell or a bare
    normalized string.  Anything unrecognised is ``unknown``, never ``no``.
    """
    if isinstance(value, dict):
        value = value.get("normalized")
    text = str(value or "").strip()
    if text == "yes":
        return YES
    if text in _NEGATIVE:
        return NO
    return UNKNOWN


def operating_state(record: dict[str, Any]) -> str:
    """``yes`` when open, ``no`` when temporarily closed, else ``unknown``."""
    value = record.get("operating_status")
    if isinstance(value, dict):
        value = value.get("normalized")
    text = str(value or "").strip()
    if text == "operating":
        return YES
    if text == "temporarily_closed":
        return NO
    return UNKNOWN


def tri_bool(state: str) -> bool | None:
    """``True`` / ``False`` / ``None``.  ``unknown`` must not read as ``False``."""
    return {YES: True, NO: False}.get(state)


def check_identifier_uniqueness(records: Iterable[dict[str, Any]], *,
                                key: str = "official_identifier") -> dict[str, Any]:
    """Reject blank or repeated identifiers.  Every collision is listed."""
    blank: list[int] = []
    seen: dict[str, list[int]] = {}
    for index, record in enumerate(records):
        ident = str(record.get(key, "") or "").strip()
        if not ident:
            blank.append(index)
            continue
        seen.setdefault(ident, []).append(index)
    duplicates = {k: v for k, v in seen.items() if len(v) > 1}
    problems: list[dict[str, Any]] = []
    if blank:
        problems.append({"code": "identifier_blank", "row_count": len(blank),
                         "row_indexes": blank[:50]})
    if duplicates:
        problems.append({"code": "identifier_duplicated",
                         "duplicate_identifier_count": len(duplicates),
                         "duplicates": [{"identifier": k, "row_indexes": v}
                                        for k, v in sorted(duplicates.items())]})
    if problems:
        raise GeoInputError("identifier uniqueness check failed", problems)
    return {"record_count": len(seen) + len(blank), "unique_identifier_count": len(seen)}


#: Bounding box of Japanese territory, generous by design: Okinotorishima in
#: the south (20.42N), Cape Soya in the north (45.52N), Yonaguni in the west
#: (122.93E) and Minamitorishima in the east (153.99E).
JAPAN_LAT_RANGE = (20.0, 46.0)
JAPAN_LON_RANGE = (122.0, 154.0)

#: The pipeline's single working CRS (spec 10.2).
DEFAULT_CRS = "EPSG:6668"

_EPSG_RE = re.compile(r"EPSG:\s*(\d+)", re.I)


def epsg_code(value: Any) -> str | None:
    """``"EPSG:6668 (JGD2011)"`` -> ``"EPSG:6668"``.  ``None`` when absent."""
    match = _EPSG_RE.search(str(value or ""))
    return f"EPSG:{int(match.group(1))}" if match else None


def check_coordinate_contract(records: list[dict[str, Any]], *,
                              expected_crs: str = DEFAULT_CRS,
                              identifier_key: str = "official_identifier",
                              accuracy_key: str = "accuracy",
                              accuracy_values: Iterable[str] | None = None,
                              allow_missing_coordinates: bool = False) -> dict[str, Any]:
    """Fail-closed contract on a geocode record set (spec 7.5 / 10.2 / 15.3).

    Checks, in one pass and reporting all of them together: identifier presence
    and uniqueness, a declared CRS that really is ``expected_crs``, latitude and
    longitude that are finite and inside Japan, coordinates present or absent as
    a pair rather than half of one, and an ``accuracy`` drawn from a known set.

    A consumer that reads ``records.jsonl`` without this cannot tell a surveyed
    point from a swapped lat/lon or a metre-grid value that happens to parse as
    a float, and a nearest-neighbour distance computed over such a set is wrong
    without ever raising.
    """
    problems: list[dict[str, Any]] = []
    try:
        identifiers = check_identifier_uniqueness(records, key=identifier_key)
    except GeoInputError as exc:
        identifiers = {}
        problems.extend(exc.details)

    allowed_accuracy = set(accuracy_values) if accuracy_values is not None else None
    expected = epsg_code(expected_crs) or str(expected_crs)
    bad_crs: dict[str, int] = {}
    bad_accuracy: dict[str, int] = {}
    out_of_range: list[dict[str, Any]] = []
    non_finite: list[dict[str, Any]] = []
    half_missing: list[dict[str, Any]] = []
    missing = 0
    accuracy_counts: dict[str, int] = {}

    for index, record in enumerate(records):
        ident = str(record.get(identifier_key, "") or "").strip()
        if allowed_accuracy is not None:
            value = record.get(accuracy_key)
            accuracy_counts[str(value)] = accuracy_counts.get(str(value), 0) + 1
            if value not in allowed_accuracy:
                bad_accuracy[str(value)] = bad_accuracy.get(str(value), 0) + 1
        lat, lon = record.get("lat"), record.get("lon")
        # A CRS names the datum of a coordinate.  A row that carries no
        # coordinate at all has nothing to declare, so the check applies from
        # the moment either ordinate is present - including the half-missing
        # case, which is a defect in its own right.
        if lat is not None or lon is not None:
            found = epsg_code(record.get("crs"))
            if found != expected:
                bad_crs[str(found)] = bad_crs.get(str(found), 0) + 1
        if lat is None and lon is None:
            missing += 1
            continue
        if lat is None or lon is None:
            half_missing.append({"row_index": index, identifier_key: ident})
            continue
        try:
            flat, flon = float(lat), float(lon)
        except (TypeError, ValueError):
            non_finite.append({"row_index": index, identifier_key: ident})
            continue
        if not (math.isfinite(flat) and math.isfinite(flon)):
            non_finite.append({"row_index": index, identifier_key: ident})
            continue
        if not (JAPAN_LAT_RANGE[0] <= flat <= JAPAN_LAT_RANGE[1]
                and JAPAN_LON_RANGE[0] <= flon <= JAPAN_LON_RANGE[1]):
            out_of_range.append({"row_index": index, identifier_key: ident,
                                 "lat": flat, "lon": flon})

    if bad_crs:
        problems.append({"code": "coordinate_crs_mismatch", "expected_crs": expected,
                         "found": dict(sorted(bad_crs.items()))})
    if bad_accuracy:
        problems.append({"code": "coordinate_accuracy_unknown",
                         "allowed": sorted(allowed_accuracy or ()),
                         "found": dict(sorted(bad_accuracy.items()))})
    if half_missing:
        problems.append({"code": "coordinate_half_missing", "row_count": len(half_missing),
                         "rows": half_missing[:50]})
    if non_finite:
        problems.append({"code": "coordinate_not_finite", "row_count": len(non_finite),
                         "rows": non_finite[:50]})
    if out_of_range:
        problems.append({"code": "coordinate_outside_japan", "row_count": len(out_of_range),
                         "lat_range": list(JAPAN_LAT_RANGE), "lon_range": list(JAPAN_LON_RANGE),
                         "rows": out_of_range[:50]})
    if missing and not allow_missing_coordinates:
        problems.append({"code": "coordinate_missing", "row_count": missing})
    if problems:
        raise GeoInputError("coordinate contract failed", problems)
    return {"record_count": len(records),
            "unique_identifier_count": identifiers.get("unique_identifier_count"),
            "crs": expected,
            "with_coordinate": len(records) - missing,
            "without_coordinate": missing,
            "accuracy_counts": dict(sorted(accuracy_counts.items())) or None}


def check_name_column_boundary(records: list[dict[str, Any]], *,
                               exclude: bool = False) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Detect names that do not end in 郵便局 / 分室 / 出張所.

    That is the fingerprint of the overlapping-prefecture split bug (東京都府中市
    read as 京都府中市), which moves the record to the wrong prefecture.  By
    default the run stops.  With ``exclude=True`` the offending records are
    removed from the returned list and reported, so a caller that has decided
    to proceed still cannot mistake them for geocoded data.
    """
    bad = [r for r in records if not NAME_TAIL_RE.search(_name(r))]
    report = {"checked_record_count": len(records), "anomaly_count": len(bad),
              "anomalies": [{"official_identifier": r.get("official_identifier"),
                             "source_page": r.get("source_page"),
                             "source_line": r.get("source_line")} for r in bad[:200]],
              "excluded": bool(bad) and exclude}
    if bad and not exclude:
        raise GeoInputError("name column boundary anomalies detected", [report])
    if bad:
        keep = [r for r in records if NAME_TAIL_RE.search(_name(r))]
        return keep, report
    return list(records), report


def _name(record: dict[str, Any]) -> str:
    value = record.get("name")
    if isinstance(value, dict):
        value = value.get("normalized") or value.get("raw")
    return str(value or "").strip()


def mark_state_summary(records: Iterable[dict[str, Any]], fields: Iterable[str]) -> dict[str, dict[str, int]]:
    """Count yes/no/unknown per mark field so unknowns cannot pass unnoticed."""
    fields = list(fields)
    out = {field: {YES: 0, NO: 0, UNKNOWN: 0} for field in fields}
    for record in records:
        for field in fields:
            out[field][mark_state(record.get(field))] += 1
    return out
