from __future__ import annotations

import email.message
import hashlib
import mimetypes
import os
import contextlib
import tempfile
import email.utils
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .config import AcquisitionConfig
from .manifest import Manifest, utcnow
from .policy import PolicyViolation, SitePolicy


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, policy: SitePolicy):
        self.policy = policy

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self.policy.validate_redirect(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Fetcher:
    def __init__(self, config: AcquisitionConfig | None = None, *, policy: SitePolicy | None = None,
                 manifest: Manifest | None = None, opener=None, sleep=time.sleep):
        self.config = config or AcquisitionConfig()
        self.policy = policy or SitePolicy(min_interval_seconds=self.config.min_interval_seconds,
                                           max_retries=self.config.max_retries, max_bytes=self.config.max_bytes,
                                           user_agent=self.config.user_agent)
        self._manifest = manifest
        self.opener = opener or urllib.request.build_opener(SafeRedirect(self.policy))
        self.sleep = sleep
        self._last_request = 0.0

    @property
    def manifest(self) -> Manifest:
        if self._manifest is None:
            self._manifest = Manifest(self.config.manifest_path)
        return self._manifest

    def close(self) -> None:
        if self._manifest is not None:
            self._manifest.close()

    def _require_live_identity(self) -> None:
        if (not self.config.contact or not self.policy.user_agent.strip() or
                "configure-contact" in self.policy.user_agent.lower() or
                self.config.contact.lower() not in self.policy.user_agent.lower()):
            raise PolicyViolation("live acquisition requires a non-placeholder User-Agent containing contact")

    @staticmethod
    def _status(response) -> int:
        status = getattr(response, "status", None)
        if status is not None:
            return int(status)
        return int(response.getcode())

    def _rate_limit(self) -> None:
        wait = self.policy.effective_interval() - (time.monotonic() - self._last_request)
        if wait > 0:
            self.sleep(wait)
        self._last_request = time.monotonic()

    @contextlib.contextmanager
    def _request_slot(self):
        """Cross-thread/process lease; held while waiting and making one request."""
        lock_path = Path(tempfile.gettempdir()) / "postal_bias_www_post_japanpost.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        acquired = False
        for _ in range(600):
            try:
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                acquired = True
                break
            except FileExistsError:
                self.sleep(0.1)
        if not acquired:
            raise PolicyViolation("could not acquire shared fetch lease")
        try:
            stamp_path = lock_path.with_suffix(".last")
            try:
                last = float(stamp_path.read_text(encoding="ascii"))
            except (FileNotFoundError, ValueError):
                last = 0.0
            wait = self.policy.effective_interval() - (time.time() - last)
            if wait > 0:
                self.sleep(wait)
            stamp_path.write_text(str(time.time()), encoding="ascii")
            yield
        finally:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def _raw_path(self, url: str, data: bytes, mime: str | None) -> Path:
        digest = hashlib.sha256(data).hexdigest()
        suffix = mimetypes.guess_extension((mime or "").split(";", 1)[0]) or Path(url.split("?", 1)[0]).suffix or ".bin"
        target = self.config.raw_dir / digest[:2] / f"{digest}{suffix}"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return target

    def fetch(self, url: str, *, source_type: str = "unknown", dry_run: bool = False,
              acknowledge_site_terms: bool = False,
              terms_url: str | None = None, notes: str | None = None,
              published_at: str | None = None, coverage_start: str | None = None,
              coverage_end: str | None = None) -> dict:
        self.policy.validate_url(url)
        allowed_types = {"official_index", "index", "monthly_change", "current_list", "pdf"}
        if source_type not in allowed_types and source_type != "unknown":
            raise PolicyViolation(f"unsupported source_type: {source_type}")
        if source_type == "unknown":
            path = urlparse(url).path.lower()
            source_type = "official_index" if path.endswith(("/", ".php", ".html")) else ("pdf" if path.endswith(".pdf") else "unknown")
        if source_type == "unknown":
            raise PolicyViolation("source_type must identify HTML index or PDF")
        if dry_run:
            return {"url": url, "status": "dry-run", "would_write": False, "network": False}
        if not acknowledge_site_terms:
            raise PolicyViolation("live acquisition requires --acknowledge-site-terms")
        self._require_live_identity()
        if self.policy.robots is None:
            self.fetch_robots(acknowledge_site_terms=True)
        # Revalidate after the live robots snapshot: a disallow must win over a prior check.
        self.policy.validate_url(url)
        current = self.manifest.get_current(url)
        headers = {"User-Agent": self.policy.user_agent, "Accept": "text/html,application/pdf;q=0.9,*/*;q=0.1"}
        if current:
            if current["etag"]:
                headers["If-None-Match"] = current["etag"]
            if current["last_modified"]:
                headers["If-Modified-Since"] = current["last_modified"]
        started = utcnow()
        try:
            req = urllib.request.Request(url, headers=headers)
            response, data = self._open_with_retries(req, reader=lambda r: self._read_response(r, url))
            status = self._status(response)
            final_url = response.geturl() if hasattr(response, "geturl") else url
            self.policy.validate_redirect(final_url)
            if status == 304:
                self.manifest.add_attempt(url=url, started_at=started, finished_at=utcnow(), status="not-modified", http_status=304)
                return {"url": url, "status": 304, "id": current["id"] if current else None}
            length_header = response.headers.get("Content-Length") if hasattr(response, "headers") else None
            self.policy.validate_content_length(int(length_header) if length_header else None)
            mime = response.headers.get_content_type() if hasattr(response.headers, "get_content_type") else response.headers.get("Content-Type")
            self._validate_content(source_type, mime, data)
            path = self._raw_path(url, data, mime)
            etag = response.headers.get("ETag")
            modified = response.headers.get("Last-Modified")
            sid = self.manifest.add_source(url=url, source_type=source_type, data=data, mime=mime,
                                           local_path=str(path), http_status=status, etag=etag,
                                           last_modified=modified, terms_url=terms_url,
                                           published_at=published_at, coverage_start=coverage_start, coverage_end=coverage_end,
                                           notes=notes, terms_review_status="unknown", public_release_allowed=False)
            self.manifest.add_attempt(url=url, started_at=started, finished_at=utcnow(), status="ok", http_status=status,
                                      bytes=len(data), etag=etag, last_modified=modified)
            return {"url": url, "status": status, "id": sid, "path": str(path), "sha256": Manifest.sha256(data)}
        except Exception as exc:
            self.manifest.add_attempt(url=url, started_at=started, finished_at=utcnow(), status="error", error=str(exc))
            raise

    def _read_response(self, response, requested_url):
        final_url = response.geturl() if hasattr(response, "geturl") else requested_url
        self.policy.validate_redirect(final_url)
        try:
            data = response.read(self.policy.max_bytes + 1)
        finally:
            if hasattr(response, "close"):
                response.close()
        if len(data) > self.policy.max_bytes:
            raise PolicyViolation("response exceeds max_bytes")
        return response, data

    def _read_robots_response(self, response):
        expected = self.policy.robots_url
        final_url = response.geturl() if hasattr(response, "geturl") else expected
        if final_url != expected:
            raise PolicyViolation("robots redirect target rejected")
        mime = response.headers.get_content_type() if hasattr(response.headers, "get_content_type") else response.headers.get("Content-Type")
        if (mime or "").split(";", 1)[0].strip().lower() != "text/plain":
            raise PolicyViolation("robots.txt MIME must be text/plain")
        try:
            data = response.read(self.policy.max_bytes + 1)
        finally:
            if hasattr(response, "close"):
                response.close()
        if len(data) > self.policy.max_bytes:
            raise PolicyViolation("robots.txt exceeds max_bytes")
        if not data or b"\x00" in data:
            raise PolicyViolation("robots.txt is empty or binary")
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise PolicyViolation("robots.txt is not valid text") from exc
        if not any(line.split(":", 1)[0].strip().lower() == "user-agent" for line in text.splitlines() if ":" in line):
            raise PolicyViolation("robots.txt has no User-agent directive")
        return response, data

    def _open_with_retries(self, req, reader=None):
        attempts = 0
        while True:
            try:
                with self._request_slot():
                    response = self.opener.open(req, timeout=30)
                    status = self._status(response)
                    if status in (403, 429):
                        if hasattr(response, "close"):
                            response.close()
                        raise PolicyViolation(f"safe stop on HTTP {status}")
                    if status >= 500:
                        headers = response.headers
                        if hasattr(response, "close"):
                            response.close()
                        raise urllib.error.HTTPError(req.full_url, status, "server error", headers, None)
                    return reader(response) if reader else response
            except urllib.error.HTTPError as exc:
                if exc.code in (403, 429):
                    raise PolicyViolation(f"safe stop on HTTP {exc.code}") from exc
                if exc.code < 500 or attempts >= self.policy.max_retries:
                    raise
                retry_after = self._retry_after(exc.headers.get("Retry-After") if exc.headers else None)
                delay = max(retry_after or 0.0, 2 ** attempts * self.policy.effective_interval())
                if delay > 120:
                    raise PolicyViolation("Retry-After exceeds safe maximum; acquisition deferred") from exc
                self.sleep(delay)
                attempts += 1

    @staticmethod
    def _retry_after(value: str | None) -> float | None:
        if not value:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            try:
                dt = email.utils.parsedate_to_datetime(value)
                return max(0.0, dt.timestamp() - time.time())
            except (TypeError, ValueError, OverflowError):
                return None

    @staticmethod
    def _validate_content(source_type: str, mime: str | None, data: bytes) -> None:
        ct = (mime or "").split(";", 1)[0].strip().lower()
        if source_type in {"official_index", "index"}:
            if ct not in {"text/html", "application/xhtml+xml"} or b"<html" not in data[:4096].lower():
                raise PolicyViolation("index response is not validated HTML")
        if source_type in {"monthly_change", "current_list", "pdf"}:
            if ct != "application/pdf" or not data.startswith(b"%PDF-"):
                raise PolicyViolation("PDF response MIME/magic validation failed")

    def fetch_robots(self, *, acknowledge_site_terms: bool = False, dry_run: bool = False) -> dict:
        url = self.policy.robots_url
        if dry_run:
            return {"url": url, "status": "dry-run", "network": False, "would_write": False}
        if not acknowledge_site_terms:
            raise PolicyViolation("live acquisition requires --acknowledge-site-terms")
        # robots is intentionally fetched before any page. It is the sole exception to the page path allowlist.
        self._require_live_identity()
        req = urllib.request.Request(url, headers={"User-Agent": self.policy.user_agent})
        started = utcnow()
        try:
            response, data = self._open_with_retries(req, reader=self._read_robots_response)
            status = self._status(response)
            if status in (403, 429):
                raise PolicyViolation(f"safe stop on robots HTTP {status}")
            if status != 200:
                raise PolicyViolation("robots.txt unavailable: fail closed")
        except Exception as exc:
            self.manifest.add_attempt(url=url, started_at=started, finished_at=utcnow(), status="error", error=str(exc))
            raise
        target = self.config.raw_dir / "robots" / f"{Manifest.sha256(data)}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        self.manifest.add_robots(url=url, data=data, local_path=str(target), http_status=200)
        self.manifest.add_attempt(url=url, started_at=started, finished_at=utcnow(), status="ok", http_status=200, bytes=len(data))
        self.policy.robots = SitePolicy.parse_robots(data.decode("utf-8", "replace"), fetched_at=utcnow(), sha256=Manifest.sha256(data), user_agent=self.policy.user_agent)
        return {"url": url, "status": 200, "path": str(target), "sha256": Manifest.sha256(data)}

    def can_check_index(self, url: str, *, min_days: int = 30) -> bool:
        current = self.manifest.get_current(url)
        attempt = self.manifest.latest_successful_attempt(url)
        if not current or not attempt or not attempt["finished_at"]:
            return True
        then = datetime.fromisoformat(attempt["finished_at"])
        return (datetime.now(timezone.utc) - then).total_seconds() >= min_days * 86400
