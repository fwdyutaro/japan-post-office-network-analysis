from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class Manifest:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA user_version")
        self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sources (
              id INTEGER PRIMARY KEY, url TEXT NOT NULL, source_type TEXT NOT NULL,
              fetched_at TEXT, published_at TEXT, coverage_start TEXT, coverage_end TEXT,
              sha256 TEXT, bytes INTEGER, mime TEXT, etag TEXT, last_modified TEXT,
              http_status INTEGER, local_path TEXT, terms_url TEXT, notes TEXT,
              is_current INTEGER NOT NULL DEFAULT 1, supersedes INTEGER,
              terms_review_status TEXT NOT NULL DEFAULT 'unknown',
              public_release_allowed INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS fetch_attempts (
              id INTEGER PRIMARY KEY, url TEXT, started_at TEXT, finished_at TEXT,
              status TEXT, http_status INTEGER, error TEXT, bytes INTEGER, etag TEXT,
              last_modified TEXT
            );
            CREATE TABLE IF NOT EXISTS robots_snapshots (
              id INTEGER PRIMARY KEY, url TEXT, fetched_at TEXT, sha256 TEXT,
              bytes INTEGER, local_path TEXT, http_status INTEGER, is_current INTEGER DEFAULT 1
            );
            """
        )
        try:
            self._migrate_schema()
        except Exception:
            self.db.rollback()
            raise
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_sources_url_current ON sources(url, is_current)")
        self.db.commit()

    def _migrate_schema(self) -> None:
        """Upgrade early manifests (including the URL/hash UNIQUE schema) atomically."""
        desired = ["id", "url", "source_type", "fetched_at", "published_at", "coverage_start", "coverage_end",
                   "sha256", "bytes", "mime", "etag", "last_modified", "http_status", "local_path", "terms_url",
                   "notes", "is_current", "supersedes", "terms_review_status", "public_release_allowed"]
        cols = [r[1] for r in self.db.execute("PRAGMA table_info(sources)").fetchall()]
        unique = any(r[2] for r in self.db.execute("PRAGMA index_list(sources)").fetchall())
        if cols and (set(cols) != set(desired) or unique):
            self.db.execute("BEGIN")
            self.db.execute("ALTER TABLE sources RENAME TO sources_legacy")
            self.db.execute("""CREATE TABLE sources (
                id INTEGER PRIMARY KEY, url TEXT NOT NULL, source_type TEXT NOT NULL,
                fetched_at TEXT, published_at TEXT, coverage_start TEXT, coverage_end TEXT,
                sha256 TEXT, bytes INTEGER, mime TEXT, etag TEXT, last_modified TEXT,
                http_status INTEGER, local_path TEXT, terms_url TEXT, notes TEXT,
                is_current INTEGER NOT NULL DEFAULT 1, supersedes INTEGER,
                terms_review_status TEXT NOT NULL DEFAULT 'unknown',
                public_release_allowed INTEGER NOT NULL DEFAULT 0)""")
            old = set(cols)
            expressions = []
            for col in desired:
                if col in old:
                    expressions.append(col)
                elif col == "source_type":
                    expressions.append("'unknown'")
                elif col == "is_current":
                    expressions.append("1")
                elif col == "terms_review_status":
                    expressions.append("'unknown'")
                elif col == "public_release_allowed":
                    expressions.append("0")
                else:
                    expressions.append("NULL")
            self.db.execute(f"INSERT INTO sources({','.join(desired)}) SELECT {','.join(expressions)} FROM sources_legacy")
            self.db.execute("DROP TABLE sources_legacy")
            self.db.execute("CREATE INDEX IF NOT EXISTS idx_sources_url_current ON sources(url, is_current)")
        for table, columns in {
            "fetch_attempts": ["url", "started_at", "finished_at", "status", "http_status", "error", "bytes", "etag", "last_modified"],
            "robots_snapshots": ["url", "fetched_at", "sha256", "bytes", "local_path", "http_status", "is_current"],
        }.items():
            existing = {r[1] for r in self.db.execute(f"PRAGMA table_info({table})").fetchall()}
            for col in columns:
                if col not in existing:
                    self.db.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        self.db.execute("PRAGMA user_version=2")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def close(self) -> None:
        self.db.close()

    @staticmethod
    def sha256(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def get_current(self, url: str):
        return self.db.execute("SELECT * FROM sources WHERE url=? AND is_current=1 ORDER BY id DESC LIMIT 1", (url,)).fetchone()

    def latest_successful_attempt(self, url: str):
        return self.db.execute("SELECT * FROM fetch_attempts WHERE url=? AND status IN ('ok','not-modified') ORDER BY id DESC LIMIT 1", (url,)).fetchone()

    def add_source(self, *, url: str, source_type: str, data: bytes, mime: str | None,
                   local_path: str, http_status: int = 200, etag: str | None = None,
                   last_modified: str | None = None, published_at: str | None = None,
                   coverage_start: str | None = None, coverage_end: str | None = None,
                   terms_url: str | None = None, notes: str | None = None,
                   terms_review_status: str = "unknown", public_release_allowed: bool = False) -> int:
        digest = self.sha256(data)
        old = self.get_current(url)
        if old and old["sha256"] == digest:
            return int(old["id"])
        try:
            self.db.execute("BEGIN")
            if old:
                self.db.execute("UPDATE sources SET is_current=0 WHERE id=?", (old["id"],))
            cur = self.db.execute(
                """INSERT INTO sources(url,source_type,fetched_at,published_at,coverage_start,coverage_end,
               sha256,bytes,mime,etag,last_modified,http_status,local_path,terms_url,notes,is_current,supersedes,
                terms_review_status,public_release_allowed) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (url, source_type, utcnow(), published_at, coverage_start, coverage_end, digest, len(data), mime,
                 etag, last_modified, http_status, local_path, terms_url, notes, 1, old["id"] if old else None,
                 terms_review_status, int(public_release_allowed)),
            )
            self.db.commit()
            return int(cur.lastrowid)
        except Exception:
            self.db.rollback()
            raise

    def add_attempt(self, **kwargs) -> int:
        fields = ["url", "started_at", "finished_at", "status", "http_status", "error", "bytes", "etag", "last_modified"]
        vals = [kwargs.get(k) for k in fields]
        cur = self.db.execute(f"INSERT INTO fetch_attempts({','.join(fields)}) VALUES({','.join('?' for _ in fields)})", vals)
        self.db.commit()
        return int(cur.lastrowid)

    def update_source_metadata(self, source_id: int, **fields) -> None:
        allowed = {"published_at", "coverage_start", "coverage_end", "notes"}
        chosen = {k: v for k, v in fields.items() if k in allowed}
        if not chosen:
            return
        self.db.execute("UPDATE sources SET " + ",".join(f"{k}=?" for k in chosen) + " WHERE id=?",
                        tuple(chosen.values()) + (source_id,))
        self.db.commit()

    def add_robots(self, *, url: str, data: bytes, local_path: str, http_status: int = 200) -> int:
        self.db.execute("UPDATE robots_snapshots SET is_current=0 WHERE url=?", (url,))
        cur = self.db.execute("INSERT INTO robots_snapshots(url,fetched_at,sha256,bytes,local_path,http_status,is_current) VALUES(?,?,?,?,?,?,1)",
                              (url, utcnow(), self.sha256(data), len(data), local_path, http_status))
        self.db.commit()
        return int(cur.lastrowid)
