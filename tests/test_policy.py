import unittest
from postal_bias.policy import PolicyViolation, SitePolicy


class PolicyTests(unittest.TestCase):
    def test_allowlist_and_forbidden_paths(self):
        p = SitePolicy()
        p.validate_url("https://www.post.japanpost.jp/newsrelease/storeinformation/index.php")
        for url in ("https://evil.example/newsrelease/storeinformation/x.pdf",
                    "https://www.post.japanpost.jp/search/x",
                    "https://www.post.japanpost.jp/cgi-bin/x",
                    "https://www.post.japanpost.jp/other/x.pdf"):
            with self.assertRaises(PolicyViolation): p.validate_url(url)

    def test_robots_and_crawl_delay(self):
        body = "User-agent: *\nDisallow: /search/\nCrawl-delay: 23\n"
        p = SitePolicy()
        p.robots = p.parse_robots(body, fetched_at="now", sha256="x")
        self.assertEqual(p.effective_interval(), 23)
        with self.assertRaises(PolicyViolation): p.validate_url("https://www.post.japanpost.jp/search/a")

    def test_redirect_validation(self):
        with self.assertRaises(PolicyViolation): SitePolicy().validate_redirect("https://www.post.japanpost.jp/search/x")

    def test_port_userinfo_and_encoded_path_rejected(self):
        p = SitePolicy()
        for url in ("https://www.post.japanpost.jp:8443/newsrelease/storeinformation/index.php",
                    "https://u:p@www.post.japanpost.jp/newsrelease/storeinformation/index.php",
                    "https://www.post.japanpost.jp/newsrelease/storeinformation/%2fsecret.pdf"):
            with self.assertRaises(PolicyViolation): p.validate_url(url)

    def test_wildcard_end_anchor_and_allow_longest_match(self):
        body = """User-agent: *
Disallow: /newsrelease/*
Allow: /newsrelease/storeinformation/index.php$
"""
        p = SitePolicy()
        p.robots = p.parse_robots(body, fetched_at="now", sha256="x")
        self.assertTrue(p.robots.allows("/newsrelease/storeinformation/index.php"))
        self.assertFalse(p.robots.allows("/newsrelease/storeinformation/index.php?x=1"))
        self.assertFalse(p.robots.allows("/newsrelease/storeinformation/a.pdf"))

    def test_safety_floor(self):
        p = SitePolicy(min_interval_seconds=0, max_retries=99)
        self.assertGreaterEqual(p.effective_interval(), 10)
        self.assertEqual(p.max_retries, 2)

    def test_blank_line_separates_user_agent_groups_but_contiguous_lines_do_not(self):
        separated = SitePolicy.parse_robots("User-agent: Foo\n\nUser-agent: *\nDisallow: /newsrelease/\n", fetched_at="now", sha256="x", user_agent="Foo/1")
        self.assertTrue(separated.allows("/newsrelease/a"))
        contiguous = SitePolicy.parse_robots("User-agent: Foo\nUser-agent: *\nDisallow: /newsrelease/\n", fetched_at="now", sha256="x", user_agent="Foo/1")
        self.assertFalse(contiguous.allows("/newsrelease/a"))

    def test_comment_only_line_is_group_boundary(self):
        rules = SitePolicy.parse_robots("User-agent: Foo\n# boundary\nUser-agent: *\nDisallow: /newsrelease/\n", fetched_at="now", sha256="x", user_agent="Foo/1")
        self.assertTrue(rules.allows("/newsrelease/a"))
