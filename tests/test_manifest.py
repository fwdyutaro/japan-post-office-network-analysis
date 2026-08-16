import tempfile
import unittest
import sqlite3
from pathlib import Path
from postal_bias.manifest import Manifest


class ManifestTests(unittest.TestCase):
    def test_version_chain_and_same_hash_dedup(self):
        with tempfile.TemporaryDirectory() as d:
            m = Manifest(Path(d) / "m.sqlite3")
            a = m.add_source(url="https://www.post.japanpost.jp/newsrelease/storeinformation/x.pdf", source_type="pdf", data=b"a", mime="application/pdf", local_path="a", public_release_allowed=False)
            self.assertEqual(m.add_source(url="https://www.post.japanpost.jp/newsrelease/storeinformation/x.pdf", source_type="pdf", data=b"a", mime="application/pdf", local_path="a", public_release_allowed=False), a)
            b = m.add_source(url="https://www.post.japanpost.jp/newsrelease/storeinformation/x.pdf", source_type="pdf", data=b"b", mime="application/pdf", local_path="b", public_release_allowed=False)
            self.assertNotEqual(a, b)
            row = m.get_current("https://www.post.japanpost.jp/newsrelease/storeinformation/x.pdf")
            self.assertEqual(row["id"], b)
            self.assertEqual(row["supersedes"], a)
            self.assertEqual(row["terms_review_status"], "unknown")
            self.assertEqual(row["public_release_allowed"], 0)
            m.close()

    def test_rollback_keeps_previous_current(self):
        with tempfile.TemporaryDirectory() as d:
            m = Manifest(Path(d) / "m.sqlite3")
            url = "https://www.post.japanpost.jp/newsrelease/storeinformation/x.pdf"
            a = m.add_source(url=url, source_type="pdf", data=b"a", mime="application/pdf", local_path="a")
            m.db.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON sources BEGIN SELECT RAISE(ABORT, 'injected'); END")
            with self.assertRaises(Exception):
                m.add_source(url=url, source_type="pdf", data=b"b", mime="application/pdf", local_path="b")
            m.db.execute("DROP TRIGGER fail_insert")
            self.assertEqual(m.get_current(url)["id"], a)
            m.close()

    def test_a_b_a_is_new_current_version(self):
        with tempfile.TemporaryDirectory() as d:
            m = Manifest(Path(d) / "m.sqlite3")
            url = "https://www.post.japanpost.jp/newsrelease/storeinformation/x.pdf"
            a1 = m.add_source(url=url, source_type="pdf", data=b"a", mime="application/pdf", local_path="a1")
            b = m.add_source(url=url, source_type="pdf", data=b"b", mime="application/pdf", local_path="b")
            a2 = m.add_source(url=url, source_type="pdf", data=b"a", mime="application/pdf", local_path="a2")
            current = m.get_current(url)
            self.assertEqual(current["id"], a2)
            self.assertEqual(current["supersedes"], b)
            self.assertNotEqual(a1, a2)
            m.close()

    def test_migrates_old_unique_and_missing_columns(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "old.sqlite3"
            db = sqlite3.connect(path)
            db.executescript("CREATE TABLE sources(id INTEGER PRIMARY KEY,url TEXT,source_type TEXT,sha256 TEXT,is_current INTEGER, UNIQUE(url,sha256)); INSERT INTO sources(url,source_type,sha256,is_current) VALUES('u','pdf','a',1);")
            db.commit(); db.close()
            m = Manifest(path)
            row = m.get_current("u")
            self.assertEqual(row["sha256"], "a")
            self.assertEqual(m.db.execute("PRAGMA user_version").fetchone()[0], 2)
            m.close()

    def test_migrates_minimal_sources_without_is_current(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "minimal.sqlite3"
            db = sqlite3.connect(path)
            db.execute("CREATE TABLE sources(id INTEGER PRIMARY KEY, url TEXT)")
            db.execute("INSERT INTO sources(url) VALUES('u')")
            db.commit(); db.close()
            m = Manifest(path)
            row = m.get_current("u")
            self.assertEqual(row["source_type"], "unknown")
            self.assertEqual(row["is_current"], 1)
            m.close()
