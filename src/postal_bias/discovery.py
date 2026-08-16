from __future__ import annotations

import re
from datetime import date as date_cls
from dataclasses import dataclass, asdict
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
import json


@dataclass(frozen=True)
class DiscoveredDocument:
    url: str
    kind: str
    link_text: str
    date: str | None = None
    href: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href, self._text = None, []


def discover_index(html: str | bytes, base_url: str = "https://www.post.japanpost.jp/newsrelease/storeinformation/") -> list[DiscoveredDocument]:
    parser = _Links()
    parser.feed(html.decode("utf-8", "replace") if isinstance(html, bytes) else html)
    seen: set[str] = set()
    result: list[DiscoveredDocument] = []
    date_re = re.compile(r"(?<!\d)(20\d{2})[-/.年]?(\d{1,2})[-/.月]?(\d{1,2})日?\b")
    for href, text in parser.links:
        if not href or not re.search(r"\.pdf(?:$|[?#])", href, re.I):
            continue
        absolute = urljoin(base_url, href)
        p = urlparse(absolute)
        if p.hostname != "www.post.japanpost.jp" or p.query or p.fragment or not p.path.startswith("/newsrelease/storeinformation/"):
            continue
        filename = p.path.rsplit("/", 1)[-1].lower()
        if filename == "ichiran.pdf":
            kind = "current_list"
        elif re.fullmatch(r"\d{8}\.pdf", filename):
            kind = "monthly_change"
        else:
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        m = re.search(r"(20\d{2})年\s*(\d{1,2})月\s*(\d{1,2})日?(?:時点|現在)?", text)
        if m is None:
            m = re.fullmatch(r"(20\d{2})(\d{2})(\d{2})\.pdf", filename)
            if m is None:
                m = date_re.search(filename)
        date = None
        if m:
            try:
                date = date_cls(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
            except ValueError:
                date = None
        if kind == "monthly_change" and date is not None and date < "2012-10-01":
            continue
        result.append(DiscoveredDocument(absolute, kind, text, date, href))
    return result


def discover_file(path: str | Path, base_url: str = "https://www.post.japanpost.jp/newsrelease/storeinformation/") -> list[dict]:
    return [d.as_dict() for d in discover_index(Path(path).read_bytes(), base_url)]
