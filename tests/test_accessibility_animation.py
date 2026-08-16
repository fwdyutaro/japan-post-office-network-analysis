import json
import tempfile
import unittest
from pathlib import Path

from postal_bias.accessibility_animation import (
    AccessibilityAnimationError, build_accessibility_animation,
    write_accessibility_animation,
)
from postal_bias.artifacts import seal_artifact, verify_artifact


def _input_bundle(root: Path) -> Path:
    root.mkdir()
    payload = {
        "schema_version": "accessibility-recalculation-v1",
        "status": "not_evaluable",
        "reason_codes": ["confirmed_events_qa_error", "road_network_distance_missing"],
        "road_network_distance_status": "not_evaluable",
        "distance_metric": "GRS80_geodesic_straight_line",
        "p30_date": "2013-11-30", "anchor": "2026-06-30",
        "results": {
            "p30_2013": {"status": "computed", "mean_m": 653.08,
                          "cov": {"1000": 83.03}, "over2km": 100,
                          "over2km_65": 20, "n_offices": 10, "population": 1000},
            "inherit_all": {"status": "computed", "mean_m": 686.54,
                            "cov": {"1000": 81.69}, "over2km": 120,
                            "over2km_65": 25, "n_offices": 9, "population": 1000},
            "isj": {"status": "computed", "mean_m": 691.63,
                    "cov": {"1000": 81.31}, "over2km": 125,
                    "over2km_65": 26, "n_offices": 9, "population": 1000},
            "corrected": {"status": "not_evaluable",
                          "reason_codes": ["confirmed_events_qa_error"]},
        },
    }
    (root / "accessibility_recalculated.json").write_text(
        json.dumps(payload), encoding="utf-8")
    (root / "metadata.json").write_text("{}", encoding="utf-8")
    seal_artifact(root, artifact_type="accessibility_recalculation",
                  source_authority="auxiliary", evidence_status="effective",
                  release_classification="internal_only",
                  acknowledge_internal_use=True)
    return root


def _reseal(source: Path, payload: dict) -> None:
    (source / "accessibility_recalculated.json").write_text(
        json.dumps(payload), encoding="utf-8")
    seal_artifact(source, artifact_type="accessibility_recalculation",
                  source_authority="auxiliary", evidence_status="effective",
                  release_classification="internal_only", replace=True,
                  acknowledge_internal_use=True)


class AccessibilityAnimationTests(unittest.TestCase):
    def test_endpoint_frames_and_status(self):
        with tempfile.TemporaryDirectory() as td:
            result = build_accessibility_animation(_input_bundle(Path(td) / "source"))
            self.assertEqual([x["frame_id"] for x in result["frames"]],
                             ["p30_2013", "inherit_all", "isj"])
            self.assertTrue(result["endpoint_only"])
            self.assertFalse(result["intermediate_months_observed"])
            self.assertEqual(result["corrected_status"], "not_evaluable")

    def test_bundle_is_offline_safe_and_sealed(self):
        with tempfile.TemporaryDirectory() as td:
            source = _input_bundle(Path(td) / "source")
            result = build_accessibility_animation(source)
            out = Path(td) / "animation"
            published = write_accessibility_animation(
                result, source, out, acknowledge_internal_use=True)
            self.assertTrue(verify_artifact(out, strict_code=True)["ok"])
            artifact = json.loads((out / "artifact.json").read_text(encoding="utf-8"))
            source_id = json.loads((source / "artifact.json").read_text(
                encoding="utf-8"))["artifact_id"]
            self.assertEqual(artifact["input_artifact_ids"], [source_id])
            self.assertEqual(published["artifact_id"], artifact["artifact_id"])
            html = (out / "accessibility_animation.html").read_text(encoding="utf-8")
            self.assertNotIn("http://", html)
            self.assertNotIn("https://", html)
            self.assertEqual(html.count("</script>"), 1)
            self.assertIn("prefers-reduced-motion", html)
            self.assertIn("road_network_distance_missing", html)
            self.assertNotIn("draw(0);i=0", html)
            self.assertIn("next>=frames.length", html)

    def test_acknowledgement_and_model_mutation_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            source = _input_bundle(Path(td) / "source")
            result = build_accessibility_animation(source)
            with self.assertRaises(AccessibilityAnimationError):
                write_accessibility_animation(result, source, Path(td) / "no-ack")
            for mutate, name in (
                    (lambda value: value.__setitem__("status", "pass"), "status"),
                    (lambda value: value["frames"][0].__setitem__("mean_m", 999999), "value"),
                    (lambda value: value["frames"].append({"frame_id": "bad"}), "frames")):
                changed = build_accessibility_animation(source)
                mutate(changed)
                with self.subTest(name=name), self.assertRaises(AccessibilityAnimationError):
                    write_accessibility_animation(
                        changed, source, Path(td) / name, acknowledge_internal_use=True)

    def test_fractional_counts_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            source = _input_bundle(Path(td) / "source")
            payload = json.loads((source / "accessibility_recalculated.json").read_text(
                encoding="utf-8"))
            payload["results"]["p30_2013"]["n_offices"] = 1.9
            _reseal(source, payload)
            with self.assertRaises(AccessibilityAnimationError):
                build_accessibility_animation(source)

    def test_semantic_date_and_distance_metric_rejected(self):
        for key, value in (("anchor", "2026-99-99"),
                           ("distance_metric", "road_network_shortest_path")):
            with self.subTest(key=key), tempfile.TemporaryDirectory() as td:
                source = _input_bundle(Path(td) / "source")
                payload = json.loads((source / "accessibility_recalculated.json").read_text(
                    encoding="utf-8")); payload[key] = value
                _reseal(source, payload)
                with self.assertRaises(AccessibilityAnimationError):
                    build_accessibility_animation(source)


if __name__ == "__main__":
    unittest.main()
