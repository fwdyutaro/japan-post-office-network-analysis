from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from postal_bias.artifacts import seal_artifact
from postal_bias.geo_qa import GeoInputError


ROOT = Path(__file__).resolve().parents[1]


def _load(path: Path, name: str, modules: dict | None = None):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules or {}):
        spec.loader.exec_module(module)
    return module


class GeoPipelineBoundaryTests(unittest.TestCase):
    def test_corrected_access_rejects_qa_failing_sealed_input(self):
        fake_pyproj = types.SimpleNamespace(
            CRS=types.SimpleNamespace(from_proj4=lambda value: object()),
            Transformer=types.SimpleNamespace(
                from_crs=lambda *args, **kwargs: types.SimpleNamespace(
                    transform=lambda lon, lat: (lon, lat))),
            Geod=lambda **kwargs: types.SimpleNamespace(inv=lambda *args: (0, 0, 0)),
        )
        mod = _load(ROOT / "data/work/accessibility_corrected.py", "corrected_boundary",
                    {"pyproj": fake_pyproj})
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "bundle"; bundle.mkdir()
            bundle.joinpath("records.jsonl").write_text('{}\n', encoding="utf-8")
            bundle.joinpath("qa.jsonl").write_text(
                '{"kind":"error","code":"bad_input"}\n', encoding="utf-8")
            seal_artifact(bundle, artifact_type="facility_geocode",
                          source_authority="auxiliary", evidence_status="observed",
                          release_classification="internal_only", replace=True,
                          acknowledge_internal_use=True)
            with self.assertRaises(RuntimeError):
                mod._sealed_member(bundle, "records.jsonl", "facility_geocode")

    def test_geocoder_reads_only_a_strict_sealed_current_list(self):
        mod = _load(ROOT / "data/work/geo/geocode_2026.py", "geocode_boundary")
        row = {"official_identifier": "000001",
               "name": {"normalized": "甲郵便局"},
               "simple_post_office": {"normalized": "no"},
               "disadvantaged_area": {"normalized": "no"},
               "operating_status": {"normalized": "operating"}}
        with tempfile.TemporaryDirectory() as tmp:
            bundle = Path(tmp) / "current"; bundle.mkdir()
            bundle.joinpath("records.jsonl").write_text(
                json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
            bundle.joinpath("metadata.json").write_text(
                '{"counts":{"total":1},"qa_error_count":0}\n', encoding="utf-8")
            bundle.joinpath("qa.jsonl").write_text("", encoding="utf-8")
            seal_artifact(bundle, artifact_type="current_list", source_authority="official",
                          evidence_status="confirmed", release_classification="internal_only",
                          replace=True, acknowledge_internal_use=True)
            with patch.object(mod, "CUR_BUNDLE", bundle), patch.object(
                    mod, "CUR", bundle / "records.jsonl"):
                rows, report = mod.load_records()
                self.assertEqual(rows[0]["official_identifier"], "000001")
                self.assertTrue(report["anchor_artifact"]["strict_code_verified"])
                bundle.joinpath("records.jsonl").write_text(
                    json.dumps({**row, "official_identifier": "999999"}) + "\n",
                    encoding="utf-8")
                with self.assertRaises(GeoInputError):
                    mod.load_records()


if __name__ == "__main__":
    unittest.main()
