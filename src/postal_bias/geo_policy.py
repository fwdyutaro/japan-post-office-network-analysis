"""Conservative, dependency-free policy helpers for geographic sensitivity runs."""
from __future__ import annotations

import math
import unicodedata
from collections import Counter, defaultdict
from datetime import date
from typing import Any, Callable, Iterable


NAME_TRANSLATION = str.maketrans({
    "ヶ": "ケ", "ヵ": "カ", "髙": "高", "﨑": "崎", "德": "徳", "邊": "辺",
    "邉": "辺", "澤": "沢", "齋": "斎", "齊": "斉", "惠": "恵", "檮": "梼",
    "嶋": "島", "冨": "富", "櫻": "桜", "瀨": "瀬", "曾": "曽",
})


def fold_name(value: Any) -> str:
    return "".join(unicodedata.normalize("NFKC", str(value or "")).translate(
        NAME_TRANSLATION).split()).casefold()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    values = (lat1, lon1, lat2, lon2)
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or
           not math.isfinite(float(v)) for v in values):
        raise ValueError("coordinate rejected")
    radius = 6_371_008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1; dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


def build_conservative_inheritance(
        historical_rows: Iterable[dict[str, Any]], current_rows: Iterable[dict[str, Any]], *,
        max_distance_m: float = 1000.0,
        distance: Callable[[float, float, float, float], float] = haversine_m,
) -> tuple[dict[str, dict[str, Any]], dict[str, int | float | str]]:
    """Match surveyed historical points without name-only or prefecture fallback.

    Only a unique normalized name within the same municipality is eligible.  A
    low-precision current point more than ``max_distance_m`` from the historic
    point is left untouched: the old implementation inherited even 40–50 km
    discrepancies and thereby erased plausible moves.
    """
    if (isinstance(max_distance_m, bool) or not isinstance(max_distance_m, (int, float)) or
            not math.isfinite(float(max_distance_m)) or max_distance_m <= 0):
        raise ValueError("inheritance distance threshold rejected")
    index: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in historical_rows:
        municipality = str(row.get("muni_code") or row.get("muni_code_2013") or "")
        name = fold_name(row.get("_name") or row.get("name") or row.get("name_raw"))
        if municipality and name and row.get("lat") is not None and row.get("lon") is not None:
            index[(municipality, name)].append(row)

    result: dict[str, dict[str, Any]] = {}
    stats: Counter[str] = Counter()
    seen_ids: set[str] = set()
    for row in current_rows:
        if row.get("accuracy") not in {"town", "city"}:
            continue
        identifier = str(row.get("official_identifier") or "")
        if not identifier or identifier in seen_ids:
            raise ValueError("current identifier rejected")
        seen_ids.add(identifier)
        municipality = str(row.get("muni_code") or "")
        name = fold_name(row.get("name") or row.get("name_raw"))
        candidates = index.get((municipality, name), [])
        if not candidates:
            stats["no_exact_name_municipality_match"] += 1
            continue
        if len(candidates) != 1:
            stats["ambiguous_exact_name_municipality_match"] += 1
            continue
        historical = candidates[0]
        try:
            gap = float(distance(float(row["lat"]), float(row["lon"]),
                                 float(historical["lat"]), float(historical["lon"])))
        except (KeyError, TypeError, ValueError, OverflowError):
            stats["invalid_coordinate"] += 1
            continue
        if not math.isfinite(gap) or gap > max_distance_m:
            stats["distance_gate_rejected"] += 1
            continue
        result[identifier] = {
            "lat": float(historical["lat"]), "lon": float(historical["lon"]),
            "distance_m": gap,
            "historical_record_id": historical.get("p30_record_id"),
            "match_method": "exact_name_municipality_with_distance_gate",
        }
        stats["accepted"] += 1
    return result, {**dict(stats), "max_distance_m": float(max_distance_m),
                    "match_method": "exact_name_municipality_with_distance_gate"}


def summarize_distance_strata(
        baseline: Iterable[float], current: Iterable[float], population: Iterable[float],
        strata: dict[str, Iterable[bool]], *, threshold_m: float = 1000.0,
) -> dict[str, dict[str, float | int | str | list[str]]]:
    """Return population-weighted distance changes with fail-closed inputs.

    Missing or non-finite distances are never converted to zero and a stratum
    containing one is marked ``not_evaluable``.  This keeps the report from
    silently publishing a partial-population denominator.
    """
    before = list(baseline); after = list(current); weights = list(population)
    if not before or len(before) != len(after) or len(before) != len(weights):
        raise ValueError("distance inputs rejected")
    if (isinstance(threshold_m, bool) or not isinstance(threshold_m, (int, float)) or
            not math.isfinite(float(threshold_m)) or threshold_m <= 0):
        raise ValueError("distance threshold rejected")
    for weight in weights:
        if (isinstance(weight, bool) or not isinstance(weight, (int, float)) or
                not math.isfinite(float(weight)) or weight < 0):
            raise ValueError("population input rejected")

    result: dict[str, dict[str, float | int | str | list[str]]] = {}
    for name, raw_mask in strata.items():
        if not isinstance(name, str) or not name:
            raise ValueError("stratum name rejected")
        mask = list(raw_mask)
        if len(mask) != len(before) or any(type(value) is not bool for value in mask):
            raise ValueError("stratum mask rejected")
        indices = [index for index, keep in enumerate(mask) if keep]
        total = sum(float(weights[index]) for index in indices)
        if total <= 0:
            result[name] = {"population": 0, "status": "not_evaluable",
                            "reason_codes": ["zero_population"]}
            continue
        invalid = [index for index in indices
                   if (isinstance(before[index], bool) or
                       not isinstance(before[index], (int, float)) or
                       not math.isfinite(float(before[index])) or float(before[index]) < 0 or
                       isinstance(after[index], bool) or
                       not isinstance(after[index], (int, float)) or
                       not math.isfinite(float(after[index])) or float(after[index]) < 0)]
        if invalid:
            result[name] = {"population": int(total), "status": "not_evaluable",
                            "reason_codes": ["missing_or_invalid_distance"],
                            "invalid_distance_count": len(invalid)}
            continue
        mean_before = sum(float(before[index]) * float(weights[index])
                          for index in indices) / total
        mean_after = sum(float(after[index]) * float(weights[index])
                         for index in indices) / total
        share_before = sum(float(weights[index]) for index in indices
                           if float(before[index]) <= threshold_m) / total * 100
        share_after = sum(float(weights[index]) for index in indices
                          if float(after[index]) <= threshold_m) / total * 100
        result[name] = {
            "population": int(total), "mean_2013_m": mean_before,
            "mean_2026_m": mean_after, "delta_mean_m": mean_after - mean_before,
            "share_within_1km_2013": share_before,
            "share_within_1km_2026": share_after,
            "delta_cov1km_pt": share_after - share_before, "status": "computed",
        }
    return result


def summarize_population_access(
        distances: Iterable[float], population: Iterable[float],
        population_65: Iterable[float], *, bands_m: tuple[int, ...] = (500, 1000, 2000, 5000),
) -> dict[str, Any]:
    """Summarize one access series without partial-population denominators."""
    values = list(distances); weights = list(population); elderly = list(population_65)
    if not values or len(values) != len(weights) or len(values) != len(elderly):
        raise ValueError("access inputs rejected")
    for sequence, label in ((weights, "population"), (elderly, "population_65")):
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
               not math.isfinite(float(value)) or value < 0 for value in sequence):
            raise ValueError(f"{label} input rejected")
    invalid = [index for index, value in enumerate(values)
               if isinstance(value, bool) or not isinstance(value, (int, float)) or
               not math.isfinite(float(value)) or value < 0]
    total = sum(float(value) for value in weights)
    if total <= 0:
        return {"status": "not_evaluable", "reason_codes": ["zero_population"],
                "population": 0}
    if invalid:
        return {"status": "not_evaluable",
                "reason_codes": ["missing_or_invalid_distance"],
                "population": int(total), "invalid_distance_count": len(invalid)}
    if (not bands_m or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0
                           for value in bands_m) or tuple(sorted(set(bands_m))) != bands_m):
        raise ValueError("access bands rejected")
    mean = sum(float(distance) * float(weight)
               for distance, weight in zip(values, weights)) / total
    coverage = {band: sum(float(weight) for distance, weight in zip(values, weights)
                          if float(distance) <= band) / total * 100 for band in bands_m}
    over2km = sum(float(weight) for distance, weight in zip(values, weights)
                  if float(distance) > 2000)
    over2km_65 = sum(float(weight) for distance, weight in zip(values, elderly)
                     if float(distance) > 2000)
    return {"status": "computed", "population": int(total), "mean_m": mean,
            "cov": coverage, "over2km": int(over2km), "over2km_65": int(over2km_65)}


def relative_change_percent(before: float, after: float) -> float | None:
    """Return a percentage only when the baseline is a valid positive denominator."""
    values = (before, after)
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) or
           not math.isfinite(float(value)) or value < 0 for value in values):
        raise ValueError("relative change input rejected")
    if before == 0:
        return None
    return (float(after) - float(before)) / float(before) * 100


def select_date_confirmed_relocations(
        events: Iterable[dict[str, Any]], *, start_exclusive: str, end_inclusive: str,
) -> tuple[set[str], dict[str, int | str]]:
    """Return only relocations whose implementation date is independently observed."""
    try:
        start = date.fromisoformat(start_exclusive); end = date.fromisoformat(end_inclusive)
    except (TypeError, ValueError) as exc:
        raise ValueError("relocation window rejected") from exc
    if start >= end:
        raise ValueError("relocation window rejected")
    identifiers: set[str] = set(); stats: Counter[str] = Counter()
    for event in events:
        if event.get("event_type") != "relocation":
            continue
        if event.get("provenance") != "official" or not event.get("state_corroborated"):
            stats["not_state_corroborated"] += 1
            continue
        if event.get("effective_date_confirmed") is not True:
            stats["effective_date_unconfirmed"] += 1
            continue
        raw_day = event.get("confirmed_effective_date")
        try:
            effective = date.fromisoformat(raw_day)
        except (TypeError, ValueError):
            stats["confirmed_date_invalid"] += 1
            continue
        if effective <= start:
            stats["before_or_at_start"] += 1
            continue
        if effective > end:
            stats["after_end"] += 1
            continue
        stats["accepted_events"] += 1
        for state_name in ("before_state", "after_state"):
            value = (event.get(state_name) or {}).get("official_identifier")
            if isinstance(value, dict):
                value = value.get("normalized") or value.get("raw")
            if value and value != "-":
                identifiers.add(str(value))
    return identifiers, {**dict(stats),
                         "date_policy": "effective_date_confirmed_only"}
