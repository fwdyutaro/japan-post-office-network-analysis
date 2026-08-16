import io, json, tempfile, unittest
from types import SimpleNamespace
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from postal_bias.artifacts import ArtifactError, seal_artifact, verify_artifact
from postal_bias.cli import main


class ArtifactTests(unittest.TestCase):
    def _seal(self, root, **kwargs):
        options = {"artifact_type": "test", "source_authority": "official", "evidence_status": "confirmed", "release_classification": "internal_only", "acknowledge_internal_use": True}
        options.update(kwargs)
        return seal_artifact(root, **options)

    def test_determinism_tamper_and_receipt(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d) / "a", Path(d) / "b"; a.mkdir(); b.mkdir()
            for root in (a, b):
                (root / "records.jsonl").write_text('{"x":1}\n', encoding="utf-8")
            ra, rb = self._seal(a), self._seal(b)
            self.assertEqual(ra["artifact_id"], rb["artifact_id"])
            self.assertEqual((a / "checksums.json").read_bytes(), (b / "checksums.json").read_bytes())
            self.assertNotEqual((a / "run_receipt.json").read_bytes(), (b / "run_receipt.json").read_bytes())
            self.assertTrue(verify_artifact(a)["ok"])
            (a / "records.jsonl").write_text("tampered", encoding="utf-8")
            self.assertFalse(verify_artifact(a)["ok"])

    def test_enums_ack_and_temp_rejection(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "x").write_text("x", encoding="utf-8")
            with self.assertRaises(ArtifactError): self._seal(root, release_classification="public_allowed")
            (root / ".tmp").write_text("x", encoding="utf-8")
            with self.assertRaises(ArtifactError): self._seal(root)
            with self.assertRaises(ArtifactError): self._seal(root, input_artifact_ids=[1])

    def test_nested_manifest_is_payload_and_tamper_detected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); nested = root / "nested"; nested.mkdir()
            (nested / "artifact.json").write_text('{"nested":true}\n', encoding="utf-8")
            self._seal(root)
            self.assertTrue(verify_artifact(root)["ok"])
            (nested / "artifact.json").write_text("tampered\n", encoding="utf-8")
            self.assertFalse(verify_artifact(root)["ok"])

    def test_final_snapshot_race_is_rejected_and_verify_enums(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); target = root / "x"; target.write_text("x", encoding="utf-8")
            import postal_bias.artifacts as mod
            original = mod._payload_checksums; calls = [0]
            def race(path, *args, **kwargs):
                value = original(path, *args, **kwargs); calls[0] += 1
                if calls[0] == 3:
                    target.write_text("changed", encoding="utf-8")
                return value
            with patch.object(mod, "_payload_checksums", side_effect=race):
                with self.assertRaises(ArtifactError): self._seal(root)
            self._seal(root, replace=True)
            doc = json.loads((root / "artifact.json").read_text(encoding="utf-8")); doc["source_authority"] = "made_up"
            (root / "artifact.json").write_bytes((json.dumps(doc, sort_keys=True, separators=(",", ":")) + "\n").encode())
            self.assertFalse(verify_artifact(root)["ok"])

    def test_post_commit_race_rolls_back_manifests(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); target = root / "x"; target.write_text("x", encoding="utf-8")
            import postal_bias.artifacts as mod
            original = mod._payload_checksums; calls = [0]
            def race(path, *args, **kwargs):
                value = original(path, *args, **kwargs); calls[0] += 1
                if calls[0] == 4:
                    target.write_text("post-commit-change", encoding="utf-8")
                return value
            with patch.object(mod, "_payload_checksums", side_effect=race):
                with self.assertRaises(ArtifactError): self._seal(root)
            self.assertFalse((root / "artifact.json").exists())

    def test_malformed_checksums_is_fail_closed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "x").write_text("x", encoding="utf-8"); self._seal(root)
            (root / "checksums.json").write_text('{"schema_version":"artifact-v1","files":[]}', encoding="utf-8")
            result = verify_artifact(root)
            self.assertFalse(result["ok"])
            err = io.StringIO()
            with redirect_stderr(err): code = main(["verify-artifact", str(root)])
            self.assertEqual(code, 2); self.assertNotIn(str(root), err.getvalue()); self.assertNotIn("Traceback", err.getvalue())

    def test_lock_cleanup_failure_is_not_reported_success(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "x").write_text("x", encoding="utf-8")
            import postal_bias.artifacts as mod
            def fail_lock(*args, **kwargs):
                raise OSError("lock cleanup")
            with patch.object(mod.Path, "unlink", side_effect=fail_lock):
                with self.assertRaises(ArtifactError): self._seal(root)
            self.assertTrue((root / "artifact.json").exists())
            with self.assertRaises(ArtifactError): self._seal(root, replace=True)

    def test_receipt_is_required_but_not_hashed(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "x").write_text("x", encoding="utf-8"); self._seal(root)
            (root / "run_receipt.json").unlink()
            result = verify_artifact(root)
            self.assertFalse(result["ok"]); self.assertIn("run_receipt_missing_or_invalid", result["errors"])

    def test_root_reparse_attribute_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "x").write_text("x", encoding="utf-8")
            with patch.object(Path, "lstat", return_value=SimpleNamespace(st_file_attributes=0x400)), patch.object(Path, "is_symlink", return_value=False):
                with self.assertRaises(ArtifactError): self._seal(root)
                with self.assertRaises(ArtifactError): verify_artifact(root)

    def test_rollback_cleanup_warning_and_cli_safety(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); (root / "x").write_text("x", encoding="utf-8")
            self._seal(root)
            import postal_bias.artifacts as mod
            with patch.object(mod.os, "unlink", side_effect=OSError("cleanup")):
                # Existing manifests are preserved and failure is sanitized by API.
                with self.assertRaises(ArtifactError): self._seal(root, replace=True)
            err = io.StringIO()
            with redirect_stderr(err): code = main(["verify-artifact", r"C:\private\missing"])
            self.assertEqual(code, 2); self.assertNotIn("missing", err.getvalue()); self.assertNotIn("Traceback", err.getvalue())
