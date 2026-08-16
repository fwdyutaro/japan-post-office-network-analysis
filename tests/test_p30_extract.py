import io, json, os, struct, tempfile, unittest, zipfile
from unittest.mock import patch
from contextlib import redirect_stderr
from pathlib import Path

from postal_bias.cli import main
from postal_bias.p30_extract import EXPECTED_PRJ, P30ExtractError, parse_p30, write_p30_outputs
import postal_bias.p30_extract as p30_extract


def _dbf(count):
    fields = [(f"P30_{idx:03d}", ("N" if idx == 7 else "C"), 10 if idx <= 4 else (254 if idx <= 6 else 4)) for idx in range(1, 8)]
    header_len, row_len = 32 + 32 * len(fields) + 1, sum(f[2] for f in fields)
    out = bytearray(b"\x03\x00\x00\x00" + struct.pack("<IHH", count, header_len, row_len) + b"\x00" * 20)
    for name, typ, length in fields:
        d = bytearray(32); d[:len(name)] = name.encode(); d[11] = ord(typ); d[16] = length; out += d
    out += b"\x0d" + b" " * (count * row_len) + b"\x1a"
    return bytes(out)


def _shp(points):
    body = bytearray()
    for idx, (lat, lon) in enumerate(points, 1):
        payload = struct.pack("<idd", 1, lon, lat)
        body += struct.pack(">ii", idx, len(payload) // 2) + payload
    size_words = (100 + len(body)) // 2
    xs = [p[1] for p in points]; ys = [p[0] for p in points]
    header = bytearray(100); struct.pack_into(">i", header, 0, 9994); struct.pack_into(">i", header, 24, size_words)
    struct.pack_into("<i", header, 28, 1000); struct.pack_into("<i", header, 32, 1)
    struct.pack_into("<4d", header, 36, min(xs), min(ys), max(xs), max(ys))
    return bytes(header) + bytes(body)


def _xml(points, offices):
    chunks = ['<?xml version="1.0"?><ksj:Dataset xmlns:ksj="http://nlftp.mlit.go.jp/ksj/schemas/ksj-app" xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:xlink="http://www.w3.org/1999/xlink">']
    for idx, (lat, lon) in enumerate(points, 1):
        chunks.append(f'<gml:Point gml:id="n{idx}"><gml:pos>{lat} {lon}</gml:pos></gml:Point>')
    for idx, office in enumerate(offices, 1):
        oid, pid = office.get("id", f"po{idx}"), office.get("point", idx)
        chunks.append(f'<ksj:PostOffice gml:id="{oid}"><ksj:position xlink:href="#n{pid}"/><ksj:administrativeArea>13</ksj:administrativeArea><ksj:publicFacilityLargeClassification>18</ksj:publicFacilityLargeClassification><ksj:publicFacilitySmallClassification>18003</ksj:publicFacilitySmallClassification><ksj:type>18006</ksj:type><ksj:name>{office.get("name", "局")}</ksj:name><ksj:address>{office.get("address", "東京都")}</ksj:address><ksj:administrativeCode>0</ksj:administrativeCode></ksj:PostOffice>')
    return ("".join(chunks) + "</ksj:Dataset>").encode()


def make_zip(path, *, points=None, offices=None, dbf_count=None, extra=None, omit=None):
    points = points or [(35.0, 139.0), (36.0, 140.0), (34.0, 135.0)]
    offices = offices or [{}, {}, {}]
    members = {"P30-13/P30-13.xml": _xml(points, offices), "P30-13/P30-13.dbf": _dbf(len(offices) if dbf_count is None else dbf_count), "P30-13/P30-13.shp": _shp(points), "P30-13/P30-13.prj": EXPECTED_PRJ.encode()}
    members["P30-13/P30-13.shx"] = b"x"
    for name in omit or []: members.pop(name, None)
    members.update(extra or {})
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, value in members.items(): zf.writestr(name, value)


def patch_method(path, method):
    data = bytearray(Path(path).read_bytes()); pos = 0
    while True:
        local = data.find(b"PK\x03\x04", pos)
        central = data.find(b"PK\x01\x02", pos)
        if local < 0 and central < 0:
            break
        if local >= 0 and (central < 0 or local < central):
            struct.pack_into("<H", data, local + 8, method); pos = local + 4
        else:
            struct.pack_into("<H", data, central + 10, method); pos = central + 4
    Path(path).write_bytes(data)


class P30ExtractTests(unittest.TestCase):
    def test_valid_three_records_and_schema(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.zip"; make_zip(path)
            result = parse_p30(path)
            self.assertEqual(len(result["records"]), 3); self.assertEqual(result["metadata"]["qa_error_count"], 0)
            self.assertEqual(result["records"][0]["coordinate_crs"], "EPSG:4612 (JGD2000)")
            self.assertFalse(result["metadata"]["public_release_allowed"])

    def test_fail_closed_member_and_row_errors(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "missing.zip"; make_zip(path, omit=["P30-13/P30-13.prj"])
            with self.assertRaises(P30ExtractError): parse_p30(path)
            malformed = Path(d) / "bad.zip"; make_zip(malformed, offices=[{"point": 9, "id": "poX"}, {}, {"id": "po2"}])
            result = parse_p30(malformed)
            codes = {q["code"] for q in result["qa"]}
            self.assertIn("unresolved_point", codes)
            duplicate = Path(d) / "duplicate.zip"; make_zip(duplicate, offices=[{}, {}, {"id": "po1"}])
            self.assertIn("duplicate_or_missing_office_id", {q["code"] for q in parse_p30(duplicate)["qa"]})
            out = Path(d) / "unsafe.zip"; make_zip(out, extra={"../escape": b"x"})
            with self.assertRaises(P30ExtractError): parse_p30(out)

    def test_count_mismatch_atomic_and_cli_safe(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "count.zip"; make_zip(path, dbf_count=2)
            result = parse_p30(path); self.assertTrue(any(q["code"] == "dbf_xml_count_mismatch" for q in result["qa"]))
            out = Path(d) / "out"
            with self.assertRaises(P30ExtractError): write_p30_outputs(result, out)
            write_p30_outputs(result, out, acknowledge_noncommercial_use=True, acknowledge_internal_use=True)
            with self.assertRaises(P30ExtractError): write_p30_outputs(result, out, acknowledge_noncommercial_use=True, acknowledge_internal_use=True)
            err = io.StringIO()
            with redirect_stderr(err): code = main(["parse-p30", r"C:\private\missing.zip", "--coverage-date", "2013-11-30", "--output-dir", r"C:\private\out", "--acknowledge-noncommercial-use", "--acknowledge-internal-use"])
            self.assertEqual(code, 2); self.assertNotIn("missing.zip", err.getvalue()); self.assertNotIn("Traceback", err.getvalue())

    def test_adversarial_xml_prj_shp_dbf_and_case_duplicates(self):
        with tempfile.TemporaryDirectory() as d:
            base = Path(d) / "base.zip"; make_zip(base)
            doctored = Path(d) / "doctype.zip"
            xml = _xml([(35.0, 139.0)], [{}]).replace(b'<?xml version="1.0"?>', b'<!DOCTYPE Dataset [<!ENTITY x "bad">]><?xml version="1.0"?>')
            make_zip(doctored, points=[(35.0, 139.0)], offices=[{}], extra={"P30-13/P30-13.xml": xml})
            with self.assertRaises(P30ExtractError): parse_p30(doctored)
            wrong_prj = Path(d) / "prj.zip"; make_zip(wrong_prj, extra={"P30-13/P30-13.prj": b'GEOGCS["WGS 84"]'})
            with self.assertRaises(P30ExtractError): parse_p30(wrong_prj)
            dup_case = Path(d) / "case.zip"; make_zip(dup_case, extra={"p30-13/P30-13.XML": b"x"})
            with self.assertRaises(P30ExtractError): parse_p30(dup_case)
            bad_dbf = Path(d) / "dbf.zip"; make_zip(bad_dbf, extra={"P30-13/P30-13.dbf": b"bad"})
            with self.assertRaises(P30ExtractError): parse_p30(bad_dbf)
            bad_shp = Path(d) / "shp.zip"; raw = bytearray(_shp([(35.0, 139.0), (36.0, 140.0), (34.0, 135.0)])); struct.pack_into(">i", raw, 24, 1); make_zip(bad_shp, extra={"P30-13/P30-13.shp": bytes(raw)})
            result = parse_p30(bad_shp); self.assertTrue(any(q["code"] == "shp_declared_length_mismatch" for q in result["qa"]))

    def test_atomic_cleanup_warning_does_not_fail_success(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "x.zip"; make_zip(path); result = parse_p30(path); out = Path(d) / "out"
            write_p30_outputs(result, out, acknowledge_noncommercial_use=True, acknowledge_internal_use=True)
            original = os.unlink
            calls = [0]
            def fail_backup(name):
                if ".backup." in str(name):
                    calls[0] += 1
                    if calls[0] > 3:
                        raise OSError("cleanup")
                return original(name)
            with patch.object(p30_extract.os, "unlink", side_effect=fail_backup):
                paths = write_p30_outputs(result, out, replace=True, acknowledge_noncommercial_use=True, acknowledge_internal_use=True)
            self.assertTrue(Path(paths["records"]).exists()); self.assertIn("cleanup_warnings", paths)

    def test_unsupported_and_corrupt_compression_are_sanitized(self):
        with tempfile.TemporaryDirectory() as d:
            unsupported = Path(d) / "method.zip"; make_zip(unsupported); patch_method(unsupported, 99)
            with self.assertRaises(P30ExtractError): parse_p30(unsupported)
            corrupt = Path(d) / "corrupt.zip"; make_zip(corrupt)
            with zipfile.ZipFile(corrupt) as zf:
                info = zf.getinfo("P30-13/P30-13.xml")
            raw = bytearray(corrupt.read_bytes()); start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
            raw[start + 5] ^= 0xFF; corrupt.write_bytes(raw)
            with self.assertRaises(P30ExtractError): parse_p30(corrupt)

    def test_truncated_dbf_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "truncated.zip"
            full = _dbf(3)
            make_zip(path, extra={"P30-13/P30-13.dbf": full[:-10]})
            with self.assertRaises(P30ExtractError):
                parse_p30(path)

    def test_invalid_first_ids_still_make_all_duplicates_excluded(self):
        with tempfile.TemporaryDirectory() as d:
            point_dup = Path(d) / "point-dup.zip"
            xml = _xml([(999.0, 139.0), (35.0, 140.0), (34.0, 135.0)], [{"point": 1}, {}, {}]).replace(b'gml:id="n2"', b'gml:id="n1"')
            make_zip(point_dup, extra={"P30-13/P30-13.xml": xml})
            result = parse_p30(point_dup)
            self.assertTrue(any(q["code"] == "duplicate_or_missing_point_id" for q in result["qa"]))
            self.assertFalse(any(r["source_feature_id"] == "po1" for r in result["records"]))
            office_dup = Path(d) / "office-dup.zip"
            xml = _xml([(35.0, 139.0), (36.0, 140.0), (34.0, 135.0)], [{"id": "po1", "name": ""}, {"id": "po1", "name": "valid"}, {}])
            make_zip(office_dup, extra={"P30-13/P30-13.xml": xml})
            result = parse_p30(office_dup)
            self.assertTrue(any(q["code"] == "duplicate_or_missing_office_id" for q in result["qa"]))
            self.assertFalse(any(r["source_feature_id"] == "po1" for r in result["records"]))
            field_dup = Path(d) / "field-dup.zip"
            xml = _xml([(35.0, 139.0), (36.0, 140.0), (34.0, 135.0)], [{}]).replace(b"</ksj:name>", b"</ksj:name><ksj:name>duplicate</ksj:name>", 1)
            make_zip(field_dup, extra={"P30-13/P30-13.xml": xml})
            result = parse_p30(field_dup)
            self.assertTrue(any(q["code"] == "duplicate_office_field" for q in result["qa"]))
