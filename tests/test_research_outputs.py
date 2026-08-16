"""Formal promotion of the human-facing report, map and animation."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from postal_bias.artifacts import seal_artifact, verify_artifact
from postal_bias.cli import main
from postal_bias.research_outputs import (ResearchOutputError, SOURCE_MEMBERS,
                                          build_research_bundle,
                                          package_research_outputs)
from postal_bias.report import build_final_quality_report


class ResearchOutputTests(unittest.TestCase):
    def _work(self, root: Path, *, post_anchor: bool = False) -> Path:
        work = root / "work"
        work.mkdir()
        for source in SOURCE_MEMBERS.values():
            (work / source).write_text("# reproducibility fixture\n", encoding="utf-8")
        (work / "stats.json").write_text(
            json.dumps({"generated_from": "sealed fixture"}), encoding="utf-8")
        (work / "choropleth.json").write_text(json.dumps({
            "main": {"w": 10, "h": 10, "paths": {}},
            "oki": {"w": 10, "h": 10, "paths": {}}, "units": {},
        }), encoding="utf-8")
        months = ["2020-01", "2020-02"] + (["2020-03"] if post_anchor else [])
        animation = {
            "months": months, "baseline_month": "2020-01",
            "anchor_month": "2020-02", "missing_months": [], "frames": ["0"] * len(months),
            "cells": [], "cell_px": 1.0, "w": 1, "h": 1,
            "national": [{"total": 1}] * len(months), "position_changes": [0] * len(months),
        }
        (work / "animation_data.json").write_text(json.dumps(animation), encoding="utf-8")
        embedded = {key: animation.get(key) for key in
                    ("months", "frames", "cells", "cell_px", "w", "h", "national",
                     "position_changes", "missing_months")}
        (work / "postal_animation.html").write_text(
            "<!doctype html><script>const D=" + json.dumps(embedded, separators=(",", ":")) +
            ";const LIGHT=[];</script>", encoding="utf-8")
        (work / "postal_bias_report.html").write_text(
            '<!doctype html><figure><svg viewBox="0 0 10 10" role="img" '
            'aria-label="市区町村別の実効局数増減率のコロプレス図">'
            '<g class="mapg"></g><g transform="translate(18,-18)">'
            '<rect x="-6" y="-6" width="22" height="22" class="inset"/>'
            '<text x="0" y="14" class="sub">沖縄（別縮尺）</text></g>'
            '<g transform="translate(-158,26)"><text x="0" y="0" class="sub">'
            '実効局数の増減 2013→2026</text>'
            '<rect x="0" y="12" width="15" height="11" fill="var(--inc)"/>'
            '<text x="22" y="21.5" class="tick">増加</text>'
            '<rect x="0" y="29" width="15" height="11" fill="var(--d1)"/>'
            '<text x="22" y="38.5" class="tick">0〜−5%</text>'
            '<rect x="0" y="46" width="15" height="11" fill="var(--d2)"/>'
            '<text x="22" y="55.5" class="tick">−5〜−10%</text>'
            '<rect x="0" y="63" width="15" height="11" fill="var(--d3)"/>'
            '<text x="22" y="72.5" class="tick">−10〜−20%</text>'
            '<rect x="0" y="80" width="15" height="11" fill="var(--d4)"/>'
            '<text x="22" y="89.5" class="tick">−20%超の減少</text></g>'
            '</svg></figure>', encoding="utf-8")
        for name in ("accessibility_corrected.json", "lorenz.json",
                     "monthly_formal.json", "reconcile.json"):
            (work / name).write_text("{}\n", encoding="utf-8")
        return work

    def _upstream(self, root: Path) -> Path:
        upstream = root / "upstream"
        upstream.mkdir()
        (upstream / "records.jsonl").write_text('{"id":"x"}\n', encoding="utf-8")
        (upstream / "qa.jsonl").write_text("", encoding="utf-8")
        seal_artifact(upstream, artifact_type="fixture", source_authority="official",
                      evidence_status="confirmed", release_classification="internal_only",
                      acknowledge_internal_use=True)
        return upstream

    def test_report_map_animation_are_one_verified_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); work = self._work(root); upstream = self._upstream(root)
            out = root / "research"
            result = package_research_outputs(
                work, out, [upstream], unresolved_inputs=["road_network_distance"],
                acknowledge_internal_use=True)
            self.assertEqual(result["research_status"], "not_evaluable")
            self.assertTrue(verify_artifact(out, strict_code=True)["ok"])
            for member in ("report/postal_bias_report.html", "map/postal_bias_map.svg",
                           "animation/postal_animation.html", "provenance.json",
                           "coverage_summary.jsonl"):
                self.assertTrue((out / member).is_file(), member)
            manifest = json.loads((out / "artifact.json").read_text(encoding="utf-8"))
            upstream_manifest = json.loads(
                (upstream / "artifact.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["input_artifact_ids"],
                             [upstream_manifest["artifact_id"]])

    def test_acknowledgement_and_temporal_contract_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); upstream = self._upstream(root)
            work = self._work(root)
            result = build_research_bundle(work, [upstream])
            with self.assertRaises(ResearchOutputError):
                package_research_outputs(work, root / "out", [upstream])
            self.assertEqual(result["component_manifest"]["animation"]["anchor_month"],
                             "2020-02")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); upstream = self._upstream(root)
            with self.assertRaises(ResearchOutputError):
                build_research_bundle(self._work(root, post_anchor=True), [upstream])

    def test_tampered_upstream_and_cli_errors_are_sanitized(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); work = self._work(root); upstream = self._upstream(root)
            (upstream / "records.jsonl").write_text('{"id":"tampered"}\n', encoding="utf-8")
            with self.assertRaises(ResearchOutputError):
                build_research_bundle(work, [upstream])
            err = io.StringIO(); out = io.StringIO()
            with redirect_stderr(err), redirect_stdout(out):
                code = main(["seal-research-outputs", "--work-dir", str(work),
                             "--output-dir", str(root / "out"), "--input-artifact",
                             str(upstream), "--acknowledge-internal-use"])
            self.assertEqual(code, 2)
            self.assertIn("research output input or output rejected", err.getvalue())

    def test_html_data_mismatch_and_active_report_content_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); work = self._work(root); upstream = self._upstream(root)
            html = (work / "postal_animation.html").read_text(encoding="utf-8")
            (work / "postal_animation.html").write_text(
                html.replace('"months":["2020-01","2020-02"]',
                             '"months":["1999-01","2020-02"]'), encoding="utf-8")
            with self.assertRaises(ResearchOutputError):
                build_research_bundle(work, [upstream])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); work = self._work(root); upstream = self._upstream(root)
            report = (work / "postal_bias_report.html").read_text(encoding="utf-8")
            (work / "postal_bias_report.html").write_text(
                report + '<a href="java&#x73;cript:alert(1)">x</a>', encoding="utf-8")
            with self.assertRaises(ResearchOutputError):
                build_research_bundle(work, [upstream])
        for escaped_css in (r"u\72l(https://evil.test/x)",
                            r'@\69mport "https://evil.test/x"'):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d); work = self._work(root); upstream = self._upstream(root)
                report = (work / "postal_bias_report.html").read_text(encoding="utf-8")
                (work / "postal_bias_report.html").write_text(
                    report + f"<style>a{{background:{escaped_css}}}</style>",
                    encoding="utf-8")
                with self.assertRaises(ResearchOutputError):
                    build_research_bundle(work, [upstream])
        for controlled_url in ("java\nscript:alert(1)", "java\tscript:alert(1)"):
            with tempfile.TemporaryDirectory() as d:
                root = Path(d); work = self._work(root); upstream = self._upstream(root)
                report = (work / "postal_bias_report.html").read_text(encoding="utf-8")
                (work / "postal_bias_report.html").write_text(
                    report + f'<a href="{controlled_url}">x</a>', encoding="utf-8")
                with self.assertRaises(ResearchOutputError):
                    build_research_bundle(work, [upstream])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); work = self._work(root); upstream = self._upstream(root)
            report = (work / "postal_bias_report.html").read_text(encoding="utf-8")
            (work / "postal_bias_report.html").write_text(
                report + "<script>alert(1)</script>", encoding="utf-8")
            with self.assertRaises(ResearchOutputError):
                build_research_bundle(work, [upstream])

    def test_final_report_observes_research_not_evaluable_status(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); work = self._work(root); upstream = self._upstream(root)
            out = root / "research"
            package_research_outputs(work, out, [upstream],
                                     acknowledge_internal_use=True)
            report = build_final_quality_report({"research_outputs": out}, {})
            self.assertEqual(report["checks"]["research_outputs"]["status"],
                             "not_evaluable")
            self.assertTrue(any(item.startswith("research_outputs:not_evaluable:")
                                for item in report["missing_inputs"]))

    def test_existing_output_requires_explicit_replace(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); work = self._work(root); upstream = self._upstream(root)
            out = root / "research"
            first = package_research_outputs(work, out, [upstream],
                                             acknowledge_internal_use=True)
            with self.assertRaises(ResearchOutputError):
                package_research_outputs(work, out, [upstream],
                                         acknowledge_internal_use=True)
            second = package_research_outputs(work, out, [upstream],
                                              acknowledge_internal_use=True, replace=True)
            self.assertEqual(first["artifact_id"], second["artifact_id"])
            self.assertTrue(verify_artifact(out, strict_code=True)["ok"])


if __name__ == "__main__":
    unittest.main()
