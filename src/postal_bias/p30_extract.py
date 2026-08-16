"""Bounded extraction of P30-13 postal-office records from a local ZIP."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import tempfile
import zipfile
import zlib
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

from .p30 import inspect_p30

PARSER_VERSION = "p30-extract-v1"
P30_COVERAGE_DATE = "2013-11-30"
SOURCE_CRS = "EPSG:4612 (JGD2000)"
TERMS_URL = "https://nlftp.mlit.go.jp/ksj/gml/datalist/KsjTmplt-P30.html"
REQUIRED_MEMBERS = ("P30-13/P30-13.xml", "P30-13/P30-13.shp", "P30-13/P30-13.shx", "P30-13/P30-13.dbf", "P30-13/P30-13.prj")
EXPECTED_PRJ = 'GEOGCS["GCS_JGD_2000",DATUM["D_JGD_2000",SPHEROID["GRS_1980",6378137.0,298.257222101]],PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
GML_NS = "http://www.opengis.net/gml/3.2"
XLINK_NS = "http://www.w3.org/1999/xlink"
KSJ_NS = "http://nlftp.mlit.go.jp/ksj/schemas/ksj-app"
KNOWN_OFFICE_FIELDS = {"position", "administrativeArea", "publicFacilityLargeClassification",
                        "publicFacilitySmallClassification", "type", "name", "address", "administrativeCode"}


class P30ExtractError(ValueError):
    pass


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(elem: ET.Element) -> str:
    return "".join(elem.itertext()).strip()


def _sha_row(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _parse_pos(raw: str) -> tuple[float, float] | None:
    parts = raw.split()
    if len(parts) != 2:
        return None
    try:
        lat, lon = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not (math.isfinite(lat) and math.isfinite(lon) and 20.0 <= lat <= 46.0 and 122.0 <= lon <= 154.0):
        return None
    return lat, lon


def _shp_points(stream, member_size: int) -> tuple[int, list[tuple[float, float]], list[str]]:
    header = stream.read(100)
    errors: list[str] = []
    if len(header) != 100:
        return 0, [], ["shp_header_short"]
    count = 0
    coords: list[tuple[float, float]] = []
    if struct.unpack_from(">i", header, 0)[0] != 9994:
        errors.append("shp_file_code_invalid")
    if struct.unpack_from(">i", header, 24)[0] * 2 != member_size:
        errors.append("shp_declared_length_mismatch")
    if struct.unpack_from("<i", header, 28)[0] != 1000:
        errors.append("shp_version_invalid")
    if struct.unpack_from("<i", header, 32)[0] != 1:
        errors.append("shp_shape_type_not_point")
    bbox = struct.unpack_from("<4d", header, 36)
    if not all(math.isfinite(x) for x in bbox) or not (122.0 <= bbox[0] <= 154.0 and 122.0 <= bbox[2] <= 154.0 and 20.0 <= bbox[1] <= 46.0 and 20.0 <= bbox[3] <= 46.0):
        errors.append("shp_bbox_invalid")
    while True:
        rec = stream.read(8)
        if not rec:
            break
        if len(rec) != 8:
            errors.append("shp_record_header_short"); break
        size = struct.unpack_from(">i", rec, 4)[0] * 2
        if size < 20:
            errors.append("shp_record_size_invalid"); break
        if size > 100 * 1024 * 1024:
            errors.append("shp_record_size_exceeds_limit"); break
        body = stream.read(size)
        if len(body) != size:
            errors.append("shp_record_short"); break
        rec_no = struct.unpack_from(">i", rec, 0)[0]
        if rec_no != count + 1:
            errors.append("shp_record_number_noncontiguous")
        if struct.unpack_from("<i", body, 0)[0] != 1 or size < 20:
            errors.append("shp_record_shape_not_point"); continue
        x, y = struct.unpack_from("<dd", body, 4)
        if not (math.isfinite(x) and math.isfinite(y) and 122.0 <= x <= 154.0 and 20.0 <= y <= 46.0):
            errors.append("shp_coordinate_invalid"); continue
        coords.append((x, y))
        count += 1
    return count, coords, errors


def _fixed_members(zf: zipfile.ZipFile) -> dict[str, str]:
    names = zf.namelist()
    normalized = [n.casefold() for n in names]
    if len(normalized) != len(set(normalized)):
        raise P30ExtractError("duplicate ZIP member names")
    resolved: dict[str, str] = {}
    for member in REQUIRED_MEMBERS:
        candidates = [name for name in names if name.casefold() == member.casefold()]
        if len(candidates) != 1:
            raise P30ExtractError("required P30 member missing or duplicated")
        resolved[member] = candidates[0]
    return resolved


def _read_prj(zf: zipfile.ZipFile, member: str) -> str:
    info = zf.getinfo(member)
    if info.file_size > 4096:
        raise P30ExtractError("PRJ exceeds bounded size")
    raw = zf.read(member)
    try:
        text = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise P30ExtractError("PRJ encoding rejected") from exc
    if text != EXPECTED_PRJ:
        raise P30ExtractError("PRJ definition rejected")
    return hashlib.sha256(raw).hexdigest()


def _dbf_contract(zf: zipfile.ZipFile, member: str, member_size: int) -> tuple[int, dict[str, dict[str, Any]], str]:
    with zf.open(member) as stream:
        header = stream.read(32)
        if len(header) != 32:
            raise P30ExtractError("DBF header rejected")
        count, header_len, row_len = struct.unpack_from("<IHH", header, 4)
        raw_fields = stream.read(max(0, header_len - 32))
    if header_len < 33 or row_len < 1 or member_size < header_len + count * row_len:
        raise P30ExtractError("DBF size contract rejected")
    if len(raw_fields) < 1 or raw_fields[-1] != 0x0D:
        raise P30ExtractError("DBF field descriptor rejected")
    fields: dict[str, dict[str, Any]] = {}
    for offset in range(0, len(raw_fields) - 1, 32):
        desc = raw_fields[offset:offset + 32]
        if len(desc) < 32:
            break
        name = desc[:11].split(b"\0", 1)[0].decode("ascii", "replace")
        if not name:
            break
        fields[name] = {"type": chr(desc[11]), "length": desc[16], "offset": offset}
    expected = [(f"P30_{idx:03d}", ("N" if idx == 7 else "C"), 10 if idx <= 4 else (254 if idx <= 6 else 4)) for idx in range(1, 8)]
    for name, typ, length in expected:
        got = fields.get(name)
        if not got or got["type"] != typ or got["length"] != length:
            raise P30ExtractError("DBF P30 field contract rejected")
    with zf.open(member) as stream:
        if len(stream.read(header_len)) != header_len:
            raise P30ExtractError("DBF header truncated")
        for _ in range(count):
            row = stream.read(row_len)
            if len(row) != row_len:
                raise P30ExtractError("DBF row truncated")
            if row[0] not in (0x20, 0x2A):
                raise P30ExtractError("DBF deletion flag invalid")
    mapping_json = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return count, fields, hashlib.sha256(mapping_json.encode()).hexdigest()


def _xml_preflight(zf: zipfile.ZipFile, member: str) -> None:
    with zf.open(member) as stream:
        tail = b""
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            probe = (tail + chunk).upper()
            if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
                raise P30ExtractError("XML DTD/entity rejected")
            tail = probe[-16:]


def parse_p30(path: str | Path, *, coverage_date: str = P30_COVERAGE_DATE) -> dict[str, Any]:
    if coverage_date != P30_COVERAGE_DATE:
        raise P30ExtractError("unsupported P30 coverage date")
    try:
        inspection = inspect_p30(path)
    except (OSError, zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error) as exc:
        raise P30ExtractError("P30 archive safety inspection failed") from exc
    if not inspection.get("ok"):
        raise P30ExtractError("P30 archive safety inspection failed")
    archive_sha = inspection.get("sha256")
    source_document_id = "p30-" + str(archive_sha)
    qa: list[dict[str, Any]] = []
    points: dict[str, tuple[float, float]] = {}
    point_order: list[str] = []
    seen_point_ids: set[str] = set()
    duplicate_points: set[str] = set()
    offices: list[dict[str, Any]] = []
    duplicate_offices: set[str] = set()
    seen_office_ids: set[str] = set()
    office_ids: set[str] = set()
    try:
        with zipfile.ZipFile(path) as zf:
            members = _fixed_members(zf)
            xml_member, shp_member, shx_member, dbf_member, prj_member = (members[k] for k in REQUIRED_MEMBERS)
            prj_sha = _read_prj(zf, prj_member)
            dbf_count, dbf_fields, dbf_mapping_sha = _dbf_contract(zf, dbf_member, zf.getinfo(dbf_member).file_size)
            _xml_preflight(zf, xml_member)
            with zf.open(xml_member) as stream:
                try:
                    root_seen = False
                    for event, elem in ET.iterparse(stream, events=("start", "end")):
                        if event == "start":
                            ns = elem.tag[1:].split("}", 1)[0] if elem.tag.startswith("{") else ""
                            local = _local(elem.tag)
                            if not root_seen:
                                root_seen = True
                                if ns != KSJ_NS or local != "Dataset":
                                    raise P30ExtractError("XML root namespace rejected")
                            elif local == "EnvelopeWithTimePeriod" and ns == GML_NS and elem.attrib.get("srsName") != "JGD2000 / (B, L)":
                                raise P30ExtractError("XML envelope CRS rejected")
                            elif local in {"Point", "pos", "PostOffice"} and local == "Point" and ns != GML_NS:
                                raise P30ExtractError("XML Point namespace rejected")
                            continue
                        local = _local(elem.tag)
                        if local == "Point":
                            if not elem.tag.startswith("{" + GML_NS + "}"):
                                raise P30ExtractError("XML Point namespace rejected")
                            if any(k not in {f"{{{GML_NS}}}id"} for k in elem.attrib):
                                raise P30ExtractError("XML Point attributes rejected")
                            pos_children = [c for c in elem if _local(c.tag) == "pos"]
                            if len(pos_children) != 1 or not pos_children[0].tag.startswith("{" + GML_NS + "}") or any(_local(c.tag) != "pos" or not c.tag.startswith("{" + GML_NS + "}") for c in elem):
                                raise P30ExtractError("XML Point structure rejected")
                            pid = elem.attrib.get(f"{{{GML_NS}}}id") or elem.attrib.get("id")
                            pos = _text(pos_children[0])
                            if not pid or pid in seen_point_ids:
                                if pid: duplicate_points.add(pid)
                                qa.append({"kind": "error", "code": "duplicate_or_missing_point_id", "point_id": pid})
                            else:
                                seen_point_ids.add(pid)
                                parsed = _parse_pos(pos)
                                if parsed is None:
                                    qa.append({"kind": "error", "code": "invalid_point", "point_id": pid})
                                else:
                                    points[pid] = parsed; point_order.append(pid)
                            elem.clear()
                        elif local == "PostOffice":
                            if not elem.tag.startswith("{" + KSJ_NS + "}"):
                                raise P30ExtractError("XML PostOffice namespace rejected")
                            fid = elem.attrib.get(f"{{{GML_NS}}}id") or elem.attrib.get("id")
                            fields: dict[str, str] = {}
                            unknown = any(k not in {f"{{{GML_NS}}}id", "id"} for k in elem.attrib)
                            duplicate_field = False
                            seen_fields: set[str] = set()
                            for child in elem:
                                key = _local(child.tag)
                                if not child.tag.startswith("{" + KSJ_NS + "}"):
                                    raise P30ExtractError("XML office child namespace rejected")
                                if key not in KNOWN_OFFICE_FIELDS:
                                    unknown = True; continue
                                if key in seen_fields:
                                    duplicate_field = True; continue
                                seen_fields.add(key)
                                if key == "position":
                                    if any(k != f"{{{XLINK_NS}}}href" for k in child.attrib):
                                        raise P30ExtractError("XML position attributes rejected")
                                    fields[key] = child.attrib.get(f"{{{XLINK_NS}}}href", "")
                                else:
                                    if any(k != "codeSpace" for k in child.attrib):
                                        raise P30ExtractError("XML office child attributes rejected")
                                    fields[key] = _text(child)
                            if not fid or fid in seen_office_ids:
                                if fid: duplicate_offices.add(fid)
                                qa.append({"kind": "error", "code": "duplicate_or_missing_office_id", "source_feature_id": fid})
                            else:
                                seen_office_ids.add(fid)
                                if duplicate_field:
                                    qa.append({"kind": "error", "code": "duplicate_office_field", "source_feature_id": fid})
                                    elem.clear(); continue
                                if unknown:
                                    qa.append({"kind": "error", "code": "unknown_office_structure", "source_feature_id": fid})
                                    elem.clear(); continue
                                if any(not fields.get(k) for k in KNOWN_OFFICE_FIELDS) or not fields.get("position", "").lstrip("#"):
                                    qa.append({"kind": "error", "code": "missing_required_field", "source_feature_id": fid})
                                    elem.clear(); continue
                                office_ids.add(fid)
                                offices.append({"source_feature_id": fid, **fields})
                            elem.clear()
                except ET.ParseError as exc:
                    raise P30ExtractError("P30 XML parse failed") from exc
            with zf.open(shp_member) as stream:
                shp_count, shp_coords, shp_errors = _shp_points(stream, zf.getinfo(shp_member).file_size)
            for code in shp_errors:
                qa.append({"kind": "error", "code": code})
    except (OSError, KeyError, zipfile.BadZipFile, NotImplementedError, RuntimeError, EOFError, zlib.error) as exc:
        raise P30ExtractError("P30 archive read failed") from exc
    records: list[dict[str, Any]] = []
    for row_index, office in enumerate(offices, 1):
        if office["source_feature_id"] in duplicate_offices:
            continue
        pid = office["position"].lstrip("#")
        if pid in duplicate_points or pid not in points:
            qa.append({"kind": "error", "code": "unresolved_point", "source_feature_id": office["source_feature_id"], "position_id": pid}); continue
        lat, lon = points[pid]
        record = {"p30_record_id": hashlib.sha256(f"{source_document_id}|{office['source_feature_id']}".encode()).hexdigest(),
                  "source_feature_id": office["source_feature_id"], "position_id": pid, "coverage_date": coverage_date,
                  "administrative_area": office["administrativeArea"], "public_facility_large_classification": office["publicFacilityLargeClassification"],
                  "public_facility_small_classification": office["publicFacilitySmallClassification"], "type": office["type"],
                  "name_raw": office["name"], "address_raw": office["address"], "administrative_code": office["administrativeCode"],
                  "latitude": lat, "longitude": lon, "coordinate_crs": SOURCE_CRS, "coordinate_quality": "source_reported",
                  "coordinate_validity": "valid_range", "coordinate_precision": "unknown",
                  "source_document_id": source_document_id, "source_member": members[REQUIRED_MEMBERS[0]], "source_row_index": row_index}
        record["source_row_sha256"] = _sha_row(record)
        records.append(record)
    if dbf_count != len(offices):
        qa.append({"kind": "error", "code": "dbf_xml_count_mismatch", "dbf_count": dbf_count, "xml_count": len(offices)})
    if shp_count != len(offices) or len(points) != shp_count:
        qa.append({"kind": "error", "code": "shp_xml_count_mismatch", "shp_count": shp_count, "point_count": len(points), "xml_count": len(offices)})
    if len(shp_coords) == len(point_order):
        for idx, pid in enumerate(point_order):
            lat, lon = points[pid]; x, y = shp_coords[idx]
            if abs(lon - x) > 1e-6 or abs(lat - y) > 1e-6:
                qa.append({"kind": "error", "code": "shp_xml_coordinate_mismatch", "index": idx + 1}); break
    metadata = {"parser_version": PARSER_VERSION, "source_document_id": source_document_id, "source_zip_sha256": archive_sha,
                "source_xml_sha256": _member_sha(path, members[REQUIRED_MEMBERS[0]]), "source_prj_sha256": prj_sha,
                "source_member": members[REQUIRED_MEMBERS[0]], "dbf_field_mapping": dbf_fields, "dbf_field_mapping_sha256": dbf_mapping_sha,
                "coverage_date": coverage_date, "source_crs": SOURCE_CRS, "source_crs_name": "JGD2000 / (B, L)", "coordinate_transform": "none",
                "terms_review_status": "non_commercial_research_only_pending_page_terms_review", "source_terms": "non-commercial", "terms_url": TERMS_URL,
                "raw_redistribution_allowed": False,
                "public_release_allowed": False, "record_count": len(records), "xml_office_count": len(offices),
                "xml_point_count": len(points), "dbf_record_count": dbf_count, "shp_record_count": shp_count,
                "qa_error_count": sum(q.get("kind") == "error" for q in qa)}
    metadata["counts"] = {"records": len(records), "xml_offices": len(offices), "xml_points": len(points), "dbf": dbf_count, "shp": shp_count}
    return {"records": records, "metadata": metadata, "qa": qa}


def _member_sha(path: str | Path, member: str) -> str:
    digest = hashlib.sha256()
    with zipfile.ZipFile(path) as zf, zf.open(member) as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_p30_outputs(result: dict[str, Any], output_dir: str | Path, *, replace: bool = False,
                      acknowledge_noncommercial_use: bool = False, acknowledge_internal_use: bool = False) -> dict[str, str]:
    if not acknowledge_noncommercial_use or not acknowledge_internal_use:
        raise P30ExtractError("P30 acknowledgements required")
    out = Path(output_dir); files = {"records": out / "records.jsonl", "metadata": out / "metadata.json", "qa": out / "qa.jsonl"}
    if not replace and any(p.exists() for p in files.values()):
        raise P30ExtractError("output exists; pass --replace")
    out.mkdir(parents=True, exist_ok=True); staged = []; cleanup_warnings: list[str] = []
    try:
        for key, path in files.items():
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=out); os.close(fd); temp = Path(name); staged.append(temp)
            with temp.open("w", encoding="utf-8", newline="\n") as stream:
                values = result[key] if key != "metadata" else [result[key]]
                for value in values:
                    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        backups = []; committed = []
        try:
            for path in files.values():
                if path.exists():
                    fd, name = tempfile.mkstemp(prefix=f".{path.name}.backup.", dir=out); os.close(fd); backup = Path(name); backup.unlink(); os.replace(path, backup); backups.append((path, backup))
            for (key, path), temp in zip(files.items(), staged):
                os.replace(temp, path); committed.append(path)
        except Exception as exc:
            for path in committed:
                if path.exists(): path.unlink()
            for path, backup in reversed(backups):
                if backup.exists(): os.replace(backup, path)
            raise P30ExtractError("P30 atomic commit failed") from exc
        for _, backup in backups:
            if backup.exists():
                try:
                    backup.unlink()
                except OSError:
                    cleanup_warnings.append("backup cleanup warning")
    finally:
        for temp in staged:
            if temp.exists():
                try:
                    temp.unlink()
                except OSError:
                    cleanup_warnings.append("temporary cleanup warning")
    result_paths = {key: str(path) for key, path in files.items()}
    if cleanup_warnings:
        result_paths["cleanup_warnings"] = cleanup_warnings
    return result_paths
