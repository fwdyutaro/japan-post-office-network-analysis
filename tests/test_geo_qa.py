"""Fail-closed geographic input checks and tri-state marks."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from postal_bias.geo_qa import (GeoInputError, check_identifier_uniqueness,
                                check_name_column_boundary, mark_state, mark_state_summary,
                                operating_state, tri_bool)


def record(identifier="000100", name="中央郵便局", **kw):
    row = {"official_identifier": identifier, "name": {"raw": name, "normalized": name},
           "simple_post_office": {"raw": "", "normalized": "blank"},
           "disadvantaged_area": {"raw": "○", "normalized": "yes"},
           "operating_status": {"raw": "営業中", "normalized": "operating"}}
    row.update(kw)
    return row


class MarkStateTests(unittest.TestCase):
    def test_known_marks(self):
        self.assertEqual(mark_state({"normalized": "yes"}), "yes")
        self.assertEqual(mark_state({"normalized": "no"}), "no")
        self.assertEqual(mark_state({"normalized": "not_applicable"}), "no")
        self.assertEqual(mark_state({"normalized": "blank"}), "no")

    def test_unrecognised_mark_stays_unknown_and_never_becomes_no(self):
        self.assertEqual(mark_state({"normalized": "unknown"}), "unknown")
        self.assertEqual(mark_state({"normalized": "☆"}), "unknown")
        self.assertIsNone(tri_bool("unknown"))
        self.assertIs(tri_bool("no"), False)
        self.assertIs(tri_bool("yes"), True)

    def test_operating_state_is_tri_valued(self):
        self.assertEqual(operating_state(record()), "yes")
        self.assertEqual(operating_state(record(operating_status={"normalized": "temporarily_closed"})), "no")
        self.assertEqual(operating_state(record(operating_status={"normalized": "???"})), "unknown")

    def test_summary_counts_unknowns_separately(self):
        rows = [record(), record(disadvantaged_area={"normalized": "unknown"})]
        summary = mark_state_summary(rows, ["disadvantaged_area"])
        self.assertEqual(summary["disadvantaged_area"], {"yes": 1, "no": 0, "unknown": 1})


class IdentifierTests(unittest.TestCase):
    def test_unique_identifiers_pass(self):
        out = check_identifier_uniqueness([record("000100"), record("000200")])
        self.assertEqual(out["unique_identifier_count"], 2)

    def test_duplicate_identifiers_stop_the_run_and_are_all_listed(self):
        rows = [record("000100"), record("000100"), record("000200"), record("000200")]
        with self.assertRaises(GeoInputError) as ctx:
            check_identifier_uniqueness(rows)
        detail = next(d for d in ctx.exception.details if d["code"] == "identifier_duplicated")
        self.assertEqual(detail["duplicate_identifier_count"], 2)

    def test_blank_identifier_stops_the_run(self):
        with self.assertRaises(GeoInputError) as ctx:
            check_identifier_uniqueness([record("")])
        self.assertIn("identifier_blank", {d["code"] for d in ctx.exception.details})


class NameBoundaryTests(unittest.TestCase):
    def test_clean_input_passes(self):
        rows = [record(name="中央郵便局"), record("000200", name="港分室"),
                record("000300", name="霞が関出張所")]
        kept, report = check_name_column_boundary(rows)
        self.assertEqual(len(kept), 3)
        self.assertEqual(report["anomaly_count"], 0)

    def test_column_split_anomaly_stops_the_run_by_default(self):
        rows = [record(name="中央郵便局"), record("000200", name="府中八幡宿郵便局 東")]
        with self.assertRaises(GeoInputError) as ctx:
            check_name_column_boundary(rows)
        self.assertEqual(ctx.exception.details[0]["anomaly_count"], 1)

    def test_explicit_exclusion_reports_the_count(self):
        rows = [record(name="中央郵便局"), record("000200", name="府中八幡宿郵便局 東")]
        kept, report = check_name_column_boundary(rows, exclude=True)
        self.assertEqual(len(kept), 1)
        self.assertEqual(report["anomaly_count"], 1)
        self.assertTrue(report["excluded"])
        self.assertEqual(report["anomalies"][0]["official_identifier"], "000200")


if __name__ == "__main__":
    unittest.main()
