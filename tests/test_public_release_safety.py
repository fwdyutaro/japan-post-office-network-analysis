from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO = Path(__file__).resolve().parents[1]
WORK = REPO / "data" / "work"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PublicReleaseSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(WORK))

    @classmethod
    def tearDownClass(cls):
        sys.path.remove(str(WORK))

    def test_estat_fetcher_import_has_no_network_or_write_side_effect(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"POSTAL_BIAS_PROJECT_ROOT": tmp}):
            with mock.patch("requests.get", side_effect=AssertionError("network called")):
                load_module("public_estat_fetcher", WORK / "geo" / "fetch_estat_mesh.py")
            self.assertFalse((Path(tmp) / "data" / "work" / "estat").exists())

    def test_estat_fetcher_requires_explicit_acknowledgement(self):
        module = load_module("public_estat_fetcher_ack", WORK / "geo" / "fetch_estat_mesh.py")
        with mock.patch.object(module.requests, "get",
                               side_effect=AssertionError("network called")):
            with self.assertRaisesRegex(ValueError, "acknowledge"):
                module.fetch("researcher@example.org")

    def test_project_root_environment_override(self):
        module = load_module("public_project_paths", WORK / "project_paths.py")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
                os.environ, {"POSTAL_BIAS_PROJECT_ROOT": tmp}):
            self.assertEqual(module.project_root(), Path(tmp).resolve())


if __name__ == "__main__":
    unittest.main()
