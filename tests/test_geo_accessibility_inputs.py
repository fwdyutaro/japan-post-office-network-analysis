"""Coordinate contract, and the sealed-input gate in front of the geo step."""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from postal_bias.artifacts import seal_artifact
from postal_bias.geo_qa import GeoInputError, check_coordinate_contract, epsg_code


def _load_accessibility():
    """Import ``data/work/geo/accessibility.py`` by path (not an installed module)."""
    spec = importlib.util.spec_from_file_location(
        "geo_accessibility", ROOT / "data" / "work" / "geo" / "accessibility.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def record(**kw):
    row = {"official_identifier": "000010", "lat": 35.7, "lon": 139.6,
           "crs": "EPSG:6668 (JGD2011)", "accuracy": "block"}
    row.update(kw)
    return row


class CoordinateContractTests(unittest.TestCase):
    def test_valid_records_pass_and_report(self):
        report = check_coordinate_contract(
            [record(), record(official_identifier="000020", accuracy="town")],
            accuracy_values={"block", "town", "city", "unmatched"})
        self.assertEqual(report["record_count"], 2)
        self.assertEqual(report["without_coordinate"], 0)
        self.assertEqual(report["crs"], "EPSG:6668")
        self.assertEqual(report["accuracy_counts"], {"block": 1, "town": 1})

    def test_epsg_code_extraction(self):
        self.assertEqual(epsg_code("EPSG:6668 (JGD2011)"), "EPSG:6668")
        self.assertEqual(epsg_code("EPSG:4612"), "EPSG:4612")
        self.assertIsNone(epsg_code("JGD2011"))

    def test_wrong_crs_is_rejected(self):
        with self.assertRaises(GeoInputError) as ctx:
            check_coordinate_contract([record(crs="EPSG:4612 (JGD2000)")])
        detail = next(d for d in ctx.exception.details if d["code"] == "coordinate_crs_mismatch")
        self.assertEqual(detail["expected_crs"], "EPSG:6668")
        self.assertEqual(detail["found"], {"EPSG:4612": 1})

    def test_missing_crs_is_rejected_when_a_coordinate_is_present(self):
        with self.assertRaises(GeoInputError):
            check_coordinate_contract([record(crs=None)])

    def test_a_row_with_no_coordinate_need_not_declare_a_crs(self):
        report = check_coordinate_contract(
            [record(lat=None, lon=None, crs=None, accuracy="unmatched")],
            accuracy_values={"block", "unmatched"}, allow_missing_coordinates=True)
        self.assertEqual(report["without_coordinate"], 1)

    def test_a_half_missing_row_must_still_declare_a_crs(self):
        with self.assertRaises(GeoInputError) as ctx:
            check_coordinate_contract([record(lon=None, crs=None)],
                                      allow_missing_coordinates=True)
        codes = {d["code"] for d in ctx.exception.details}
        self.assertEqual(codes, {"coordinate_crs_mismatch", "coordinate_half_missing"})

    def test_coordinate_outside_japan_is_rejected(self):
        # Swapped lat/lon: a plausible-looking pair that is not in Japan.
        with self.assertRaises(GeoInputError) as ctx:
            check_coordinate_contract([record(lat=139.6, lon=35.7)])
        self.assertIn("coordinate_outside_japan", {d["code"] for d in ctx.exception.details})

    def test_non_finite_coordinate_is_rejected(self):
        with self.assertRaises(GeoInputError) as ctx:
            check_coordinate_contract([record(lat=float("nan"))])
        self.assertIn("coordinate_not_finite", {d["code"] for d in ctx.exception.details})

    def test_half_missing_coordinate_is_rejected_even_when_gaps_are_allowed(self):
        with self.assertRaises(GeoInputError) as ctx:
            check_coordinate_contract([record(lat=None)], allow_missing_coordinates=True)
        self.assertIn("coordinate_half_missing", {d["code"] for d in ctx.exception.details})

    def test_absent_coordinate_pair_only_allowed_when_asked(self):
        rows = [record(lat=None, lon=None, accuracy="unmatched")]
        with self.assertRaises(GeoInputError) as ctx:
            check_coordinate_contract(rows)
        self.assertIn("coordinate_missing", {d["code"] for d in ctx.exception.details})
        report = check_coordinate_contract(rows, allow_missing_coordinates=True)
        self.assertEqual(report["without_coordinate"], 1)

    def test_unknown_accuracy_is_rejected(self):
        with self.assertRaises(GeoInputError) as ctx:
            check_coordinate_contract([record(accuracy="rooftop")],
                                      accuracy_values={"block", "town", "city", "unmatched"})
        detail = next(d for d in ctx.exception.details
                      if d["code"] == "coordinate_accuracy_unknown")
        self.assertEqual(detail["found"], {"rooftop": 1})

    def test_duplicate_and_blank_identifiers_are_rejected(self):
        with self.assertRaises(GeoInputError) as ctx:
            check_coordinate_contract([record(), record()])
        self.assertIn("identifier_duplicated", {d["code"] for d in ctx.exception.details})
        with self.assertRaises(GeoInputError) as ctx:
            check_coordinate_contract([record(official_identifier="")])
        self.assertIn("identifier_blank", {d["code"] for d in ctx.exception.details})

    def test_all_problems_are_reported_together(self):
        with self.assertRaises(GeoInputError) as ctx:
            check_coordinate_contract([record(crs="EPSG:4612", lat=1.0, accuracy="rooftop")],
                                      accuracy_values={"block"})
        codes = {d["code"] for d in ctx.exception.details}
        self.assertEqual(codes, {"coordinate_crs_mismatch", "coordinate_outside_japan",
                                 "coordinate_accuracy_unknown"})


class SealedGeocodeInputTests(unittest.TestCase):
    """``accessibility.py`` must not read ``records.jsonl`` off an unchecked bundle."""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_accessibility()

    def _bundle(self, root, records, *, qa_rows=(), input_qa_rows=(), seal=True):
        root.mkdir(parents=True, exist_ok=True)
        root.joinpath("records.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        root.joinpath("metadata.json").write_text('{"dataset":"test"}\n', encoding="utf-8")
        root.joinpath("qa.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in qa_rows), encoding="utf-8")
        if input_qa_rows:
            root.joinpath("input_qa.jsonl").write_text(
                "".join(json.dumps(r) + "\n" for r in input_qa_rows), encoding="utf-8")
        if seal:
            seal_artifact(root, artifact_type="geocode", source_authority="official",
                          evidence_status="effective", release_classification="internal_only",
                          replace=True, acknowledge_internal_use=True)
        return root

    def test_sealed_and_valid_bundle_is_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._bundle(Path(d) / "geocode", [record(), record(official_identifier="000020")])
            rows, report = self.mod.read_sealed_records(
                root, expected_artifact_type="geocode",
                accuracy_values=self.mod.ACCURACY_VALUES_2026)
            self.assertEqual(len(rows), 2)
            self.assertEqual(report["qa_error_count"], 0)
            self.assertEqual(report["coordinate_contract"]["with_coordinate"], 2)
            self.assertTrue(report["artifact_id"])

    def test_unsealed_bundle_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._bundle(Path(d) / "geocode", [record()], seal=False)
            with self.assertRaises(GeoInputError):
                self.mod.read_sealed_records(root)

    def test_tampered_records_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._bundle(Path(d) / "geocode", [record()])
            root.joinpath("records.jsonl").write_text(
                json.dumps(record(lat=0.0, lon=0.0)) + "\n", encoding="utf-8")
            with self.assertRaises(GeoInputError) as ctx:
                self.mod.read_sealed_records(root)
            self.assertIn("input_artifact_mismatch", {d_["code"] for d_ in ctx.exception.details})

    def test_qa_errors_in_the_input_bundle_stop_the_run(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._bundle(Path(d) / "geocode", [record()],
                                input_qa_rows=[{"kind": "error", "code": "input_rejected"}])
            with self.assertRaises(GeoInputError) as ctx:
                self.mod.read_sealed_records(root)
            detail = next(x for x in ctx.exception.details
                          if x["code"] == "input_artifact_qa_errors")
            self.assertEqual(detail["qa_error_count"], 1)
            self.assertIn("input_qa.jsonl", detail["qa_files"])

    def test_unexpected_artifact_type_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._bundle(Path(d) / "geocode", [record()])
            with self.assertRaises(GeoInputError) as ctx:
                self.mod.read_sealed_records(root, expected_artifact_type="p30")
            self.assertIn("input_artifact_type_unexpected",
                          {x["code"] for x in ctx.exception.details})

    def test_coordinate_contract_is_applied_to_sealed_rows(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._bundle(Path(d) / "geocode", [record(crs="EPSG:4612 (JGD2000)")])
            with self.assertRaises(GeoInputError) as ctx:
                self.mod.read_sealed_records(root)
            self.assertIn("coordinate_crs_mismatch", {x["code"] for x in ctx.exception.details})


if __name__ == "__main__":
    unittest.main()
