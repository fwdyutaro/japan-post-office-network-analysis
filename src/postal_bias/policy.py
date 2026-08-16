from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


class PolicyViolation(ValueError):
    pass


@dataclass
class RobotsRules:
    fetched_at: str
    body: str
    sha256: str
    crawl_delay: float | None = None
    disallow: list[str] = field(default_factory=list)
    groups: list[tuple[list[str], list[tuple[bool, str]]]] = field(default_factory=list)
    user_agent: str = "*"

    def allows(self, path: str) -> bool:
        token = self.user_agent.split("/", 1)[0].split(" ", 1)[0].lower()
        exact = [g for g in self.groups if any(token == x.lower() for x in g[0])]
        groups = exact or [g for g in self.groups if "*" in [x.lower() for x in g[0]]]
        matches: list[tuple[int, bool]] = []
        for uas, rules in groups:
            for allow, pattern in rules:
                regex = re.escape(pattern).replace(r"\*", ".*")
                if regex.endswith(r"\$"):
                    regex = regex[:-2] + "$"
                if re.match("^" + regex, path):
                    matches.append((len(pattern), allow))
        if not matches:
            return True
        longest = max(n for n, _ in matches)
        return all(allow for n, allow in matches if n == longest)


@dataclass
class SitePolicy:
    allowed_hosts: frozenset[str] = frozenset({"www.post.japanpost.jp"})
    allowed_prefixes: tuple[str, ...] = (
        "/newsrelease/storeinformation/",
    )
    forbidden_prefixes: tuple[str, ...] = ("/search/", "/cgi-bin/")
    min_interval_seconds: float = 10.0
    max_retries: int = 2
    max_bytes: int = 50 * 1024 * 1024
    user_agent: str = "postal-bias-research/0.1 (contact: configure-contact)"
    robots: RobotsRules | None = None

    def __post_init__(self) -> None:
        self.min_interval_seconds = max(10.0, float(self.min_interval_seconds))
        self.max_retries = min(2, max(0, int(self.max_retries)))

    def validate_url(self, url: str) -> str:
        p = urlparse(url)
        if p.scheme != "https" or p.hostname not in self.allowed_hosts or p.port not in (None, 443):
            raise PolicyViolation(f"host/scheme not allowed: {url}")
        if p.username is not None or p.password is not None or p.fragment or p.query:
            raise PolicyViolation(f"userinfo/query/fragment not allowed: {url}")
        if "\\" in p.path or "%" in p.path:
            raise PolicyViolation(f"suspicious encoded path: {url}")
        path = p.path or "/"
        if any(path.startswith(x) for x in self.forbidden_prefixes):
            raise PolicyViolation(f"forbidden path: {path}")
        if not any(path.startswith(x) for x in self.allowed_prefixes):
            raise PolicyViolation(f"path not allowlisted: {path}")
        # The index and the two published PDF families are the only bulk sources.
        if path.rstrip("/") not in {"/newsrelease/storeinformation", "/newsrelease/storeinformation/index.php",
                                     "/newsrelease/storeinformation/index02.html",
                                     "/newsrelease/storeinformation/pdf/ichiran.pdf"} and \
                not re.fullmatch(r"/newsrelease/storeinformation/pdf/20\d{6}\.pdf", path, re.I):
            raise PolicyViolation(f"path not in known source allowlist: {path}")
        if self.robots is not None and not self.robots.allows(path):
            raise PolicyViolation(f"robots disallow: {path}")
        return url

    def validate_redirect(self, url: str) -> str:
        return self.validate_url(url)

    @staticmethod
    def parse_robots(body: str, *, fetched_at: str, sha256: str, user_agent: str = "*") -> RobotsRules:
        disallow: list[str] = []
        delay: float | None = None
        groups: list[tuple[list[str], list[tuple[bool, str]]]] = []
        uas: list[str] = []
        rules: list[tuple[bool, str]] = []
        seen_directive = False
        wanted = user_agent.lower().split("/", 1)[0]
        for raw in body.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                if uas:
                    groups.append((uas, rules)); uas = []; rules = []; seen_directive = False
                continue
            if ":" not in line:
                continue
            key, value = (x.strip() for x in line.split(":", 1))
            if key.lower() == "user-agent":
                if seen_directive:
                    groups.append((uas, rules)); uas = []; rules = []
                uas.append(value)
            elif key.lower() in {"disallow", "allow"} and uas:
                seen_directive = True
                if value:
                    rules.append((key.lower() == "allow", value))
                    if key.lower() == "disallow": disallow.append(value)
            elif key.lower() == "crawl-delay" and uas and ("*" in [u.lower() for u in uas] or wanted in [u.lower() for u in uas]):
                seen_directive = True
                try:
                    delay = float(value)
                except ValueError:
                    pass
            elif uas:
                # Sitemap and other directives terminate the preceding UA group.
                seen_directive = True
        if uas:
            groups.append((uas, rules))
        return RobotsRules(fetched_at, body, sha256, delay, disallow, groups, user_agent)

    def effective_interval(self) -> float:
        return max(self.min_interval_seconds, self.robots.crawl_delay if self.robots and self.robots.crawl_delay else 0.0)

    @property
    def robots_url(self) -> str:
        return "https://www.post.japanpost.jp/robots.txt"

    def validate_content_length(self, length: int | None) -> None:
        if length is not None and length > self.max_bytes:
            raise PolicyViolation(f"response exceeds max_bytes ({length})")
