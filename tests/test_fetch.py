import email.message
import inspect
import tempfile
import unittest
from pathlib import Path
from postal_bias.config import AcquisitionConfig
from postal_bias.fetch import Fetcher
from postal_bias.manifest import Manifest
from postal_bias.policy import PolicyViolation, SitePolicy


class Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.headers = email.message.Message()
        for k, v in (headers or {}).items(): self.headers[k] = v
        self._body = body
    def read(self, n=-1): return self._body[:n]


class Opener:
    def __init__(self, responses): self.responses = list(responses); self.calls = []
    def open(self, req, timeout=30):
        self.calls.append(req)
        return self.responses.pop(0)


class FetchTests(unittest.TestCase):
    def make(self, d, opener):
        c = AcquisitionConfig(base_dir=Path(d), contact="research@example.org", user_agent="PostalBiasResearch/0.1 (research@example.org)")
        return Fetcher(c, policy=SitePolicy(user_agent=c.user_agent), manifest=Manifest(Path(d) / "m.sqlite3"), opener=opener, sleep=lambda _: None)

    def test_dry_run_has_no_network_or_disk(self):
        with tempfile.TemporaryDirectory() as d:
            f = self.make(d, Opener([]))
            result = f.fetch("https://www.post.japanpost.jp/newsrelease/storeinformation/index.php", dry_run=True)
            self.assertFalse(result["network"])
            self.assertFalse((Path(d) / "data").exists())
            f.close()

    def test_conditional_304(self):
        with tempfile.TemporaryDirectory() as d:
            op = Opener([Response(200, b"<html>x</html>", {"ETag": '"a"', "Content-Type": "text/html"}), Response(304)])
            f = self.make(d, op)
            f.policy.robots = f.policy.parse_robots("User-agent: *\n", fetched_at="now", sha256="x")
            url = "https://www.post.japanpost.jp/newsrelease/storeinformation/index.php"
            f.fetch(url, acknowledge_site_terms=True)
            result = f.fetch(url, acknowledge_site_terms=True)
            self.assertEqual(result["status"], 304)
            self.assertEqual(op.calls[1].get_header("If-none-match"), '"a"')
            f.close()

    def test_forbidden_live_stops(self):
        with tempfile.TemporaryDirectory() as d:
            f = self.make(d, Opener([]))
            with self.assertRaises(PolicyViolation): f.fetch("https://www.post.japanpost.jp/search/x", dry_run=True)
            f.close()

    def test_metadata_and_public_api_has_no_bypass_argument(self):
        self.assertNotIn("ensure_robots", inspect.signature(Fetcher.fetch).parameters)
        self.assertNotIn("force", inspect.signature(Fetcher.fetch).parameters)
        with tempfile.TemporaryDirectory() as d:
            op = Opener([Response(200, b"<html>x</html>", {"Content-Type": "text/html"})])
            f = self.make(d, op)
            f.policy.robots = f.policy.parse_robots("User-agent: *\n", fetched_at="now", sha256="x")
            url = "https://www.post.japanpost.jp/newsrelease/storeinformation/index.php"
            result = f.fetch(url, acknowledge_site_terms=True, published_at="2026-06-30", coverage_start="2026-06-30", coverage_end="2026-06-30")
            row = f.manifest.get_current(url)
            self.assertEqual(row["published_at"], "2026-06-30")
            self.assertEqual(result["status"], 200)
            f.close()

    def test_fetch_robots_requires_identity(self):
        with tempfile.TemporaryDirectory() as d:
            c = AcquisitionConfig(base_dir=Path(d), contact="research@example.org", user_agent="placeholder")
            f = Fetcher(c, policy=SitePolicy(user_agent="placeholder"), manifest=Manifest(Path(d) / "m.sqlite3"), opener=Opener([]), sleep=lambda _: None)
            with self.assertRaises(PolicyViolation): f.fetch_robots(acknowledge_site_terms=True)
            f.close()

    def test_retry_after_parsing(self):
        self.assertEqual(Fetcher._retry_after("3"), 3)
        self.assertIsNotNone(Fetcher._retry_after("Wed, 21 Oct 2015 07:28:00 GMT"))

    def test_integrated_robots_then_index(self):
        with tempfile.TemporaryDirectory() as d:
            robots = Response(200, b"User-agent: *\n", {"Content-Type": "text/plain"})
            page = Response(200, "<html><a href='pdf/ichiran.pdf'>2026年6月30日現在</a></html>".encode(), {"Content-Type": "text/html"})
            op = Opener([robots, page])
            f = self.make(d, op)
            url = "https://www.post.japanpost.jp/newsrelease/storeinformation/index.php"
            result = f.fetch(url, acknowledge_site_terms=True)
            self.assertEqual(result["status"], 200)
            self.assertEqual(len(op.calls), 2)
            self.assertIsNotNone(f.manifest.get_current(url))
            self.assertIsNotNone(f.manifest.latest_successful_attempt(f.policy.robots_url))
            f.close()

    def test_integrated_robots_then_pdf_with_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            robots = Response(200, b"User-agent: *\n", {"Content-Type": "text/plain"})
            pdf = Response(200, b"%PDF-1.7\nfixture", {"Content-Type": "application/pdf"})
            op = Opener([robots, pdf])
            f = self.make(d, op)
            url = "https://www.post.japanpost.jp/newsrelease/storeinformation/pdf/20121001.pdf"
            result = f.fetch(url, source_type="monthly_change", acknowledge_site_terms=True,
                             published_at="2012-10-01", coverage_start="2012-10-01", coverage_end="2012-10-01",
                             terms_url="https://www.post.japanpost.jp/newsrelease/storeinformation/")
            row = f.manifest.get_current(url)
            self.assertEqual(row["coverage_start"], "2012-10-01")
            self.assertEqual(row["public_release_allowed"], 0)
            self.assertEqual(row["terms_review_status"], "unknown")
            self.assertTrue(Path(row["local_path"]).exists())
            f.close()

    def test_oversize_body_is_rejected_for_page_and_robots(self):
        with tempfile.TemporaryDirectory() as d:
            c = AcquisitionConfig(base_dir=Path(d), max_bytes=5, contact="research@example.org", user_agent="PostalBias/0.1 (research@example.org)")
            page_f = Fetcher(c, policy=SitePolicy(max_bytes=5, user_agent=c.user_agent), manifest=Manifest(Path(d) / "m.sqlite3"), opener=Opener([Response(200, b"<html>oversize</html>", {"Content-Type": "text/html"})]), sleep=lambda _: None)
            page_f.policy.robots = page_f.policy.parse_robots("User-agent: *\n", fetched_at="now", sha256="x")
            with self.assertRaises(PolicyViolation): page_f.fetch("https://www.post.japanpost.jp/newsrelease/storeinformation/index.php", acknowledge_site_terms=True)
            page_f.close()
            robot_f = Fetcher(c, policy=SitePolicy(max_bytes=5, user_agent=c.user_agent), manifest=Manifest(Path(d) / "r.sqlite3"), opener=Opener([Response(200, b"User-agent: *\nlong", {"Content-Type": "text/plain"})]), sleep=lambda _: None)
            with self.assertRaises(PolicyViolation): robot_f.fetch_robots(acknowledge_site_terms=True)
            robot_f.close()

    def test_robots_mime_and_content_fail_closed(self):
        for body, mime in ((b"User-agent: *\n", "text/html"), (b"", "text/plain"), (b"\x00User-agent: *", "text/plain")):
            with tempfile.TemporaryDirectory() as d:
                c = AcquisitionConfig(base_dir=Path(d), contact="research@example.org", user_agent="PostalBias/0.1 (research@example.org)")
                f = Fetcher(c, policy=SitePolicy(user_agent=c.user_agent), manifest=Manifest(Path(d) / "m.sqlite3"), opener=Opener([Response(200, body, {"Content-Type": mime})]), sleep=lambda _: None)
                with self.assertRaises(PolicyViolation): f.fetch_robots(acknowledge_site_terms=True)
                f.close()
