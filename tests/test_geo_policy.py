from __future__ import annotations

import unittest

from postal_bias.geo_policy import (build_conservative_inheritance, fold_name,
                                    relative_change_percent,
                                    select_date_confirmed_relocations,
                                    summarize_distance_strata,
                                    summarize_population_access)


class GeoPolicyTests(unittest.TestCase):
    def test_inheritance_requires_exact_municipality_name_and_distance(self):
        historical = [
            {"p30_record_id": "p1", "name": "髙田 郵便局", "muni_code": "001",
             "lat": 35.0, "lon": 139.0},
            {"p30_record_id": "p2", "name": "同名局", "muni_code": "001",
             "lat": 35.0, "lon": 139.0},
            {"p30_record_id": "p3", "name": "同名局", "muni_code": "001",
             "lat": 35.001, "lon": 139.001},
        ]
        current = [
            {"official_identifier": "1", "name": "高田郵便局", "muni_code": "001",
             "accuracy": "town", "lat": 35.0001, "lon": 139.0001},
            {"official_identifier": "2", "name": "高田郵便局", "muni_code": "002",
             "accuracy": "town", "lat": 35.0, "lon": 139.0},
            {"official_identifier": "3", "name": "同名局", "muni_code": "001",
             "accuracy": "city", "lat": 35.0, "lon": 139.0},
            {"official_identifier": "4", "name": "高田郵便局", "muni_code": "001",
             "accuracy": "city", "lat": 36.0, "lon": 140.0},
        ]
        matched, stats = build_conservative_inheritance(historical, current,
                                                         max_distance_m=1000)
        self.assertEqual(set(matched), {"1"})
        self.assertEqual(fold_name("髙田 郵便局"), fold_name("高田郵便局"))
        self.assertEqual(stats["no_exact_name_municipality_match"], 1)
        self.assertEqual(stats["ambiguous_exact_name_municipality_match"], 1)
        self.assertEqual(stats["distance_gate_rejected"], 1)

    def test_relocation_never_uses_planned_date_as_confirmed(self):
        base = {"event_type": "relocation", "provenance": "official",
                "state_corroborated": True,
                "before_state": {"official_identifier": {"normalized": "1"}},
                "after_state": {"official_identifier": {"normalized": "1"}}}
        planned_only = {**base, "planned_effective_date": "2020-01-01",
                        "confirmed_effective_date": None,
                        "effective_date_confirmed": False}
        observed = {**base, "planned_effective_date": "2020-01-01",
                    "confirmed_effective_date": "2020-02-01",
                    "effective_date_confirmed": True}
        ids, stats = select_date_confirmed_relocations(
            [planned_only, observed], start_exclusive="2013-11-30",
            end_inclusive="2026-06-30")
        self.assertEqual(ids, {"1"})
        self.assertEqual(stats["effective_date_unconfirmed"], 1)
        self.assertEqual(stats["accepted_events"], 1)
        self.assertEqual(stats["date_policy"], "effective_date_confirmed_only")

    def test_relocation_window_and_provenance_fail_closed(self):
        events = [{"event_type": "relocation", "provenance": "unofficial",
                   "state_corroborated": True, "effective_date_confirmed": True,
                   "confirmed_effective_date": "2020-01-01"},
                  {"event_type": "relocation", "provenance": "official",
                   "state_corroborated": True, "effective_date_confirmed": True,
                   "confirmed_effective_date": "bad"}]
        ids, stats = select_date_confirmed_relocations(
            events, start_exclusive="2013-11-30", end_inclusive="2026-06-30")
        self.assertFalse(ids)
        self.assertEqual(stats["not_state_corroborated"], 1)
        self.assertEqual(stats["confirmed_date_invalid"], 1)

    def test_stratified_summary_is_weighted_and_fail_closed(self):
        result = summarize_distance_strata(
            [500.0, 1500.0, 800.0], [600.0, 900.0, None], [100, 300, 50],
            {"complete": [True, True, False], "missing": [False, False, True],
             "empty": [False, False, False]})
        self.assertEqual(result["complete"]["status"], "computed")
        self.assertAlmostEqual(result["complete"]["mean_2013_m"], 1250.0)
        self.assertAlmostEqual(result["complete"]["mean_2026_m"], 825.0)
        self.assertAlmostEqual(result["complete"]["delta_cov1km_pt"], 75.0)
        self.assertEqual(result["missing"]["status"], "not_evaluable")
        self.assertEqual(result["missing"]["invalid_distance_count"], 1)
        self.assertEqual(result["empty"]["reason_codes"], ["zero_population"])

    def test_stratified_summary_rejects_bad_weights_and_masks(self):
        with self.assertRaises(ValueError):
            summarize_distance_strata([1.0], [2.0], [-1], {"x": [True]})
        with self.assertRaises(ValueError):
            summarize_distance_strata([1.0], [2.0], [1], {"x": [1]})

    def test_population_access_never_publishes_partial_denominators(self):
        missing = summarize_population_access([100.0, None], [100, 100], [20, 20])
        self.assertEqual(missing["status"], "not_evaluable")
        self.assertEqual(missing["invalid_distance_count"], 1)
        self.assertNotIn("mean_m", missing)
        computed = summarize_population_access([500.0, 1500.0], [100, 300], [20, 60])
        self.assertEqual(computed["status"], "computed")
        self.assertAlmostEqual(computed["mean_m"], 1250.0)
        self.assertAlmostEqual(computed["cov"][1000], 25.0)

    def test_relative_change_has_no_zero_denominator(self):
        self.assertIsNone(relative_change_percent(0, 1))
        self.assertAlmostEqual(relative_change_percent(4, 5), 25.0)
        with self.assertRaises(ValueError):
            relative_change_percent(-1, 1)


if __name__ == "__main__":
    unittest.main()
