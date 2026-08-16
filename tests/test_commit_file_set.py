"""All-or-nothing multi-file commit used by the geographic pipeline."""
from __future__ import annotations

import gzip
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import postal_bias.artifacts as artifacts
from postal_bias.artifacts import ArtifactError, commit_file_set


def writer(text):
    return lambda stream: stream.write(text)


class CommitFileSetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _old_set(self):
        for name in ("a.csv", "b.csv", "c.csv.gz"):
            (self.root / name).write_bytes(b"old-" + name.encode())

    def test_writes_the_whole_set(self):
        commit_file_set({self.root / "a.csv": writer("A"),
                         self.root / "b.csv": writer("B"),
                         self.root / "c.csv.gz": gzip.compress(b"C")})
        self.assertEqual((self.root / "a.csv").read_text(encoding="utf-8"), "A")
        self.assertEqual((self.root / "b.csv").read_text(encoding="utf-8"), "B")
        self.assertEqual(gzip.decompress((self.root / "c.csv.gz").read_bytes()), b"C")

    def test_existing_output_is_refused_without_replace(self):
        self._old_set()
        with self.assertRaises(ArtifactError):
            commit_file_set({self.root / "a.csv": writer("A")})
        self.assertEqual((self.root / "a.csv").read_bytes(), b"old-a.csv")

    def test_a_failing_writer_leaves_every_previous_output_untouched(self):
        self._old_set()

        def boom(stream):
            raise RuntimeError("writer failed")

        with self.assertRaises(RuntimeError):
            commit_file_set({self.root / "a.csv": writer("A"),
                             self.root / "b.csv": boom,
                             self.root / "c.csv.gz": gzip.compress(b"C")}, replace=True)
        for name in ("a.csv", "b.csv", "c.csv.gz"):
            self.assertEqual((self.root / name).read_bytes(), b"old-" + name.encode())
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["a.csv", "b.csv", "c.csv.gz"])

    def test_failure_during_the_replace_phase_restores_the_previous_set(self):
        self._old_set()
        real_replace = artifacts.os.replace
        state = {"fired": False}

        def flaky(src, dst):
            # Fail once, on the first destination replacement after the backups
            # were taken: that is the window in which a naive writer leaves a
            # directory mixing this run's files with the previous run's.
            if not state["fired"] and ".backup." not in Path(src).name:
                state["fired"] = True
                raise OSError("disk full")
            return real_replace(src, dst)

        with patch.object(artifacts.os, "replace", side_effect=flaky):
            with self.assertRaises(ArtifactError):
                commit_file_set({self.root / "a.csv": writer("A"),
                                 self.root / "b.csv": writer("B"),
                                 self.root / "c.csv.gz": gzip.compress(b"C")}, replace=True)
        for name in ("a.csv", "b.csv", "c.csv.gz"):
            self.assertEqual((self.root / name).read_bytes(), b"old-" + name.encode(), name)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),
                         ["a.csv", "b.csv", "c.csv.gz"])

    def test_empty_target_map_is_a_no_op(self):
        self.assertEqual(commit_file_set({}), [])


if __name__ == "__main__":
    unittest.main()
