from __future__ import annotations

import hashlib
import json
import posixpath
import struct
import zipfile
from pathlib import Path


REQUIRED_EXTENSIONS = {".dbf", ".prj"}
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED = 500 * 1024 * 1024
MAX_MEMBER_UNCOMPRESSED = 100 * 1024 * 1024
MAX_COMPRESSION_RATIO = 1000.0
MAX_MEMBERS = 1000


def inspect_p30(path: str | Path) -> dict:
    path = Path(path)
    archive_bytes = path.stat().st_size
    out = {"path": str(path), "sha256": None, "bytes": archive_bytes, "members": [], "errors": [],
           "terms_review_status": "non_commercial_research_only_pending_page_terms_review",
           "public_release_allowed": False}
    if archive_bytes > MAX_ARCHIVE_BYTES:
        out["errors"].append("archive exceeds compressed-size limit")
        out["ok"] = False
        return out
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    out["sha256"] = hasher.hexdigest()
    with zipfile.ZipFile(path) as zf:
        names = zf.namelist()
        if len(names) > MAX_MEMBERS:
            out["errors"].append(f"archive has too many members ({len(names)})")
            out["ok"] = False
            return out
        total_uncompressed = 0
        safe_names: list[str] = []
        for info in zf.infolist():
            name = info.filename
            if name.startswith("/") or any(part == ".." for part in name.replace("\\", "/").split("/")):
                out["errors"].append(f"zip path traversal: {name}")
                continue
            safe_names.append(name)
            total_uncompressed += info.file_size
            if info.file_size > MAX_MEMBER_UNCOMPRESSED:
                out["errors"].append(f"member exceeds uncompressed-size limit: {name}")
            if info.compress_size and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
                out["errors"].append(f"suspicious compression ratio: {name}")
            lower = name.lower()
            if lower.endswith((".dbf", ".prj", ".xml", ".txt", ".html")):
                out["members"].append({"name": name, "bytes": info.file_size})
        out["total_uncompressed_bytes"] = total_uncompressed
        if total_uncompressed > MAX_TOTAL_UNCOMPRESSED:
            out["errors"].append("archive exceeds total uncompressed-size limit")
        dbfs = [n for n in safe_names if n.lower().endswith(".dbf")]
        prjs = [n for n in safe_names if n.lower().endswith(".prj")]
        if not dbfs:
            out["errors"].append("missing DBF")
        if not prjs:
            out["errors"].append("missing PRJ/coordinate metadata")
        for ext in (".shp", ".shx"):
            if not any(n.lower().endswith(ext) for n in safe_names):
                out["errors"].append(f"missing {ext} geometry companion")
        if not any(n.lower().endswith(".xml") for n in safe_names):
            out["errors"].append("missing XML metadata")
        records = {}
        for name in dbfs:
            with zf.open(name) as stream:
                raw = stream.read(32)
            if len(raw) < 32 or raw[:1] != b"\x03":
                out["errors"].append(f"invalid DBF header: {name}")
                continue
            records[name] = struct.unpack_from("<I", raw, 4)[0]
        out["dbf_record_counts"] = records
    out["ok"] = not out["errors"]
    return out
