"""QA aggregation contract, and generation/seal/publish as one operation."""
from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import postal_bias.artifacts as mod
from postal_bias.artifacts import (ArtifactError, commit_sealed_directory, seal_artifact,
                                   verify_artifact)
from postal_bias.cli import main


def _reid(manifest: dict) -> dict:
    """Re-stamp a hand-edited manifest so only the QA claim is under test."""
    recomputed = dict(manifest)
    recomputed.pop("artifact_id", None)
    manifest["artifact_id"] = mod._sha_bytes(mod._canonical(recomputed))
    return manifest


class QaAggregationTests(unittest.TestCase):
    """Every ``*qa*.jsonl`` in the bundle counts towards the sealed QA figures."""

    def _seal(self, root, **kwargs):
        options = {"artifact_type": "test", "source_authority": "official",
                   "evidence_status": "confirmed", "release_classification": "internal_only",
                   "acknowledge_internal_use": True}
        options.update(kwargs)
        return seal_artifact(root, **options)

    def _bundle(self, root):
        (root / "records.jsonl").write_text('{"x":1}\n', encoding="utf-8")
        (root / "qa.jsonl").write_text('{"kind":"info","code":"a"}\n', encoding="utf-8")
        (root / "input_qa.jsonl").write_text(
            '{"kind":"error","code":"input_rejected"}\n{"kind":"info","code":"b"}\n',
            encoding="utf-8")

    def test_errors_in_input_qa_are_counted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root)
            self._seal(root)
            manifest = json.loads((root / "artifact.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["qa_error_count"], 1)
            self.assertEqual(manifest["qa_total_count"], 3)
            self.assertEqual(manifest["qa_files"], ["input_qa.jsonl", "qa.jsonl"])
            self.assertTrue(verify_artifact(root)["ok"])

    def test_nested_qa_file_is_counted(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root)
            nested = root / "sub"
            nested.mkdir()
            (nested / "geo_qa.jsonl").write_text('{"kind":"error","code":"c"}\n', encoding="utf-8")
            self._seal(root)
            manifest = json.loads((root / "artifact.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["qa_error_count"], 2)
            self.assertIn("sub/geo_qa.jsonl", manifest["qa_files"])

    def test_explicit_list_must_name_every_qa_file(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root)
            with self.assertRaises(ArtifactError):
                self._seal(root, qa_files=["qa.jsonl"])
            self._seal(root, qa_files=["qa.jsonl", "input_qa.jsonl"])
            manifest = json.loads((root / "artifact.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["qa_error_count"], 1)

    def test_explicit_list_rejects_missing_and_escaping_paths(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root)
            with self.assertRaises(ArtifactError):
                self._seal(root, qa_files=["qa.jsonl", "input_qa.jsonl", "absent_qa.jsonl"])
            with self.assertRaises(ArtifactError):
                self._seal(root, qa_files=["../qa.jsonl"])

    def test_manifest_qa_counts_are_reverified(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root)
            self._seal(root)
            manifest = json.loads((root / "artifact.json").read_text(encoding="utf-8"))
            manifest["qa_error_count"] = 0
            (root / "artifact.json").write_text(json.dumps(_reid(manifest), ensure_ascii=False),
                                                encoding="utf-8")
            result = verify_artifact(root)
            self.assertFalse(result["ok"])
            self.assertIn("qa_summary_mismatch", result["errors"])

    def test_manifest_omitting_a_qa_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root)
            self._seal(root)
            manifest = json.loads((root / "artifact.json").read_text(encoding="utf-8"))
            manifest["qa_files"] = ["qa.jsonl"]
            manifest["qa_total_count"] = 1
            manifest["qa_error_count"] = 0
            (root / "artifact.json").write_text(json.dumps(_reid(manifest), ensure_ascii=False),
                                                encoding="utf-8")
            result = verify_artifact(root)
            self.assertFalse(result["ok"])
            self.assertIn("qa_file_set_mismatch", result["errors"])

    def test_cli_rejects_an_incomplete_explicit_qa_list(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self._bundle(root)
            err = io.StringIO()
            with redirect_stderr(err):
                code = main(["seal-artifact", str(root), "--artifact-type", "test",
                             "--source-authority", "official", "--evidence-status", "confirmed",
                             "--release-classification", "internal_only",
                             "--acknowledge-internal-use", "--qa-file", "qa.jsonl"])
            self.assertEqual(code, 2)
            self.assertFalse((root / "artifact.json").exists())


class SealedDirectoryCommitTests(unittest.TestCase):
    """Payload and seal move together; a half-applied run cannot be published."""

    OPTIONS = {"artifact_type": "test_bundle", "source_authority": "auxiliary",
               "evidence_status": "effective", "release_classification": "internal_only",
               "acknowledge_internal_use": True}

    def _payloads(self, marker):
        return {"result.csv": (lambda s, m=marker: s.write(f"value\n{m}\n")),
                "qa.jsonl": (lambda s: s.write('{"kind":"info","code":"ok"}\n')),
                "blob.bin": b"\x00\x01\x02"}

    def test_publish_seals_and_verifies_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "bundle"
            result = commit_sealed_directory(out, self._payloads("first"),
                                             strict_code=True, **self.OPTIONS)
            self.assertTrue(verify_artifact(out, strict_code=True)["ok"])
            self.assertEqual((out / "result.csv").read_text(encoding="utf-8"), "value\nfirst\n")
            self.assertEqual((out / "blob.bin").read_bytes(), b"\x00\x01\x02")
            receipt = json.loads((out / "run_receipt.json").read_text(encoding="utf-8"))
            self.assertEqual(Path(receipt["workspace"]).name, "bundle")
            self.assertEqual(result["output_dir"], str(out))
            # No staging or backup directory survives a good run.
            self.assertEqual([p.name for p in Path(d).iterdir()], ["bundle"])

    def test_regeneration_never_leaves_the_previous_seal(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "bundle"
            first = commit_sealed_directory(out, self._payloads("first"), **self.OPTIONS)
            second = commit_sealed_directory(out, self._payloads("second"), **self.OPTIONS)
            self.assertNotEqual(first["artifact_id"], second["artifact_id"])
            self.assertEqual((out / "result.csv").read_text(encoding="utf-8"), "value\nsecond\n")
            self.assertTrue(verify_artifact(out, strict_code=True)["ok"])

    def test_files_written_by_other_generators_are_preserved_and_sealed(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "bundle"
            out.mkdir()
            (out / "other_stats.json").write_text('{"kept":true}', encoding="utf-8")
            commit_sealed_directory(out, self._payloads("first"), **self.OPTIONS)
            self.assertEqual((out / "other_stats.json").read_text(encoding="utf-8"), '{"kept":true}')
            checks = json.loads((out / "checksums.json").read_text(encoding="utf-8"))["files"]
            self.assertIn("other_stats.json", checks)

    def test_write_failure_restores_the_previous_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "bundle"
            before = commit_sealed_directory(out, self._payloads("first"), **self.OPTIONS)

            def boom(stream):
                stream.write("partial")
                raise OSError("disk full")

            with self.assertRaises(ArtifactError):
                commit_sealed_directory(out, {**self._payloads("second"), "result.csv": boom},
                                        **self.OPTIONS)
            self.assertEqual((out / "result.csv").read_text(encoding="utf-8"), "value\nfirst\n")
            check = verify_artifact(out, strict_code=True)
            self.assertTrue(check["ok"])
            self.assertEqual(check["artifact_id"], before["artifact_id"])
            self.assertEqual([p.name for p in Path(d).iterdir()], ["bundle"])

    def test_verification_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "bundle"
            before = commit_sealed_directory(out, self._payloads("first"), **self.OPTIONS)
            with patch.object(mod, "verify_artifact", return_value={"ok": False, "errors": ["x"]}):
                with self.assertRaises(ArtifactError):
                    commit_sealed_directory(out, self._payloads("second"), **self.OPTIONS)
            self.assertEqual((out / "result.csv").read_text(encoding="utf-8"), "value\nfirst\n")
            self.assertEqual(verify_artifact(out)["artifact_id"], before["artifact_id"])

    def test_first_publish_verification_failure_leaves_no_bundle(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "bundle"
            real_verify = mod.verify_artifact
            calls = 0

            def fail_published(root, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 1:
                    return real_verify(root, **kwargs)
                return {"ok": False, "errors": ["injected"]}

            with patch.object(mod, "verify_artifact", side_effect=fail_published):
                with self.assertRaises(ArtifactError):
                    commit_sealed_directory(out, self._payloads("first"), **self.OPTIONS)
            self.assertFalse(out.exists())
            self.assertEqual(list(Path(d).iterdir()), [])

    def test_escaping_payload_name_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "bundle"
            with self.assertRaises(ArtifactError):
                commit_sealed_directory(out, {"../escape.csv": lambda s: s.write("x")},
                                        **self.OPTIONS)
            self.assertFalse(out.exists())
            self.assertFalse((Path(d) / "escape.csv").exists())


if __name__ == "__main__":
    unittest.main()
