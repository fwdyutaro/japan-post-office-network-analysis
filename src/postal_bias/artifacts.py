"""Deterministic, local-only artifact sealing and verification."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "artifact-v1"
AUTHORITIES = {"official", "archived_official", "unofficial", "auxiliary"}
EVIDENCE = {"announced", "observed", "confirmed", "effective", "conflicted"}
RELEASES = {"internal_only", "aggregate_only", "review_required", "public_allowed"}
MAX_FILES = 10000
MAX_FILE_BYTES = 100 * 1024 * 1024
MAX_TOTAL_BYTES = 500 * 1024 * 1024
MANIFEST_NAMES = {"artifact.json", "checksums.json", "run_receipt.json", ".artifact.lock"}


class ArtifactError(ValueError):
    pass


def _safe_root(root: Path) -> Path:
    try:
        st = root.lstat()
        if root.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
            raise ArtifactError("artifact root symlink or reparse path rejected")
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ArtifactError("artifact root rejected") from exc
    if not resolved.is_dir():
        raise ArtifactError("artifact root directory rejected")
    return resolved


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _sha_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_file(path: Path, root: Path) -> None:
    try:
        st = path.lstat()
    except OSError as exc:
        raise ArtifactError("artifact path rejected") from exc
    if path.is_symlink() or getattr(st, "st_file_attributes", 0) & 0x400:
        raise ArtifactError("symlink or reparse path rejected")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise ArtifactError("artifact path traversal rejected") from exc


def _payload_files(root: Path, ignore: set[Path] | None = None) -> list[Path]:
    _safe_root(root)
    files: list[Path] = []
    ignored = {p.resolve() for p in (ignore or set())}
    for path in root.rglob("*"):
        if path.resolve() in ignored:
            continue
        _safe_file(path, root)
        if path.is_dir():
            continue
        name = path.name
        if path.parent == root and name in MANIFEST_NAMES:
            continue
        if path.parent == root and any(name.startswith(f".{manifest}.backup.") for manifest in ("artifact.json", "checksums.json", "run_receipt.json")):
            continue
        lower = name.casefold()
        if name.startswith(".") or "backup" in lower or lower.endswith((".tmp", ".temp", ".lock")):
            raise ArtifactError("temporary or hidden artifact file rejected")
        files.append(path)
    files.sort(key=lambda p: p.relative_to(root).as_posix())
    if len(files) > MAX_FILES:
        raise ArtifactError("artifact file count exceeds limit")
    total = 0
    for path in files:
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            raise ArtifactError("artifact file exceeds size limit")
        total += size
    if total > MAX_TOTAL_BYTES:
        raise ArtifactError("artifact total size exceeds limit")
    return files


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256(); size = 0
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk); size += len(chunk)
    return digest.hexdigest(), size


def _payload_checksums(root: Path, ignore: set[Path] | None = None) -> dict[str, dict[str, Any]]:
    checks: dict[str, dict[str, Any]] = {}
    for path in _payload_files(root, ignore=ignore):
        digest, size = _hash_file(path)
        checks[path.relative_to(root).as_posix()] = {"bytes": size, "sha256": digest}
    return checks


def _code_sha256() -> str:
    base = Path(__file__).parent
    digest = hashlib.sha256()
    for path in sorted(base.rglob("*.py"), key=lambda p: p.relative_to(base).as_posix()):
        if "__pycache__" in path.parts or "egg-info" in path.parts:
            continue
        rel = path.relative_to(base).as_posix().encode()
        data = path.read_bytes()
        digest.update(len(rel).to_bytes(8, "big")); digest.update(rel)
        digest.update(len(data).to_bytes(8, "big")); digest.update(data)
    return digest.hexdigest()


def _config_hash(path: str | Path | None) -> str | None:
    if path is None:
        return None
    p = Path(path)
    if not p.is_file() or p.is_symlink() or p.stat().st_size > MAX_FILE_BYTES:
        raise ArtifactError("config file rejected")
    return _sha_bytes(p.read_bytes())


def _is_qa_name(name: str) -> bool:
    """A payload file that carries QA rows.

    Only ``qa.jsonl`` used to be aggregated, so a bundle whose defects were
    recorded in ``input_qa.jsonl`` (or any other ``*qa*.jsonl``) could be sealed
    advertising ``qa_error_count=0``.  Membership is decided by name so no QA
    file can be added later without changing the sealed file set.
    """
    lower = name.casefold()
    return lower.endswith(".jsonl") and "qa" in lower


def _discover_qa_files(root: Path) -> list[str]:
    """Every QA payload file in the bundle, as sorted relative POSIX paths."""
    return sorted(p.relative_to(root).as_posix() for p in _payload_files(root)
                  if _is_qa_name(p.name))


def _resolve_qa_files(root: Path, qa_files: list[str] | None) -> list[str]:
    """Validate the declared QA file list against what the bundle contains.

    ``qa_files=None`` means "every QA file in the payload".  An explicit list is
    allowed but must be complete: naming a subset would reinstate exactly the
    undercount this contract exists to prevent.
    """
    discovered = _discover_qa_files(root)
    if qa_files is None:
        return discovered
    if not isinstance(qa_files, (list, tuple)):
        raise ArtifactError("qa file list rejected")
    declared: list[str] = []
    for entry in qa_files:
        if not isinstance(entry, str) or not entry:
            raise ArtifactError("qa file list rejected")
        rel = Path(entry)
        if rel.is_absolute() or ".." in rel.parts:
            raise ArtifactError("qa file list rejected")
        normalized = rel.as_posix()
        path = root / rel
        _safe_file(path, root)
        if not path.is_file():
            raise ArtifactError("declared qa file missing")
        declared.append(normalized)
    if len(set(declared)) != len(declared):
        raise ArtifactError("qa file list rejected")
    missing = sorted(set(discovered) - set(declared))
    if missing:
        raise ArtifactError("qa file list incomplete")
    return sorted(set(declared))


def _qa_summary(root: Path, qa_files: list[str]) -> dict[str, int]:
    total = errors = 0
    for rel in qa_files:
        path = root / Path(rel)
        if not path.is_file():
            raise ArtifactError("declared qa file missing")
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip(): continue
            total += 1
            try:
                if json.loads(line).get("kind") == "error": errors += 1
            except json.JSONDecodeError:
                errors += 1
    return {"qa_total_count": total, "qa_error_count": errors}


def _validate_enums(authority: str, evidence: str, release: str, internal: bool, public: bool) -> None:
    if authority not in AUTHORITIES or evidence not in EVIDENCE or release not in RELEASES:
        raise ArtifactError("artifact classification rejected")
    if not internal:
        raise ArtifactError("internal-use acknowledgement required")
    if release == "public_allowed" and not public:
        raise ArtifactError("public-release review acknowledgement required")


def _acquire_lock(root: Path):
    lock = root / ".artifact.lock"
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError as exc:
        raise ArtifactError("artifact lock unavailable") from exc
    os.write(fd, b"sealed\n"); os.close(fd)
    return lock


def seal_artifact(root: str | Path, *, artifact_type: str, source_authority: str,
                  evidence_status: str, release_classification: str,
                  input_artifact_ids: list[str] | None = None, config_path: str | Path | None = None,
                  qa_files: list[str] | None = None,
                  replace: bool = False, acknowledge_internal_use: bool = False,
                  acknowledge_public_release_reviewed: bool = False) -> dict[str, Any]:
    root = Path(root)
    _safe_root(root)
    if not isinstance(artifact_type, str) or not artifact_type or len(artifact_type) > 128:
        raise ArtifactError("artifact type rejected")
    _validate_enums(source_authority, evidence_status, release_classification, acknowledge_internal_use, acknowledge_public_release_reviewed)
    lock = _acquire_lock(root)
    try:
        for name in {"artifact.json", "checksums.json", "run_receipt.json"}:
            if (root / name).exists() and not replace:
                raise ArtifactError("artifact already sealed; pass --replace")
        raw_ids = input_artifact_ids or []
        if any(not isinstance(x, str) or not re.fullmatch(r"[0-9a-f]{64}", x) for x in raw_ids):
            raise ArtifactError("input artifact IDs rejected")
        ids = sorted(set(raw_ids))
        checks = _payload_checksums(root)
        checks_doc = {"schema_version": SCHEMA_VERSION, "files": checks}
        checks_bytes = _canonical(checks_doc)
        checks_sha = _sha_bytes(checks_bytes)
        config_sha = _config_hash(config_path)
        resolved_qa_files = _resolve_qa_files(root, qa_files)
        artifact = {"artifact_type": artifact_type, "schema_version": SCHEMA_VERSION,
                    "source_authority": source_authority, "evidence_status": evidence_status,
                    "release_classification": release_classification, "input_artifact_ids": ids,
                    "public_release_reviewed": bool(acknowledge_public_release_reviewed),
                    "config_sha256": config_sha, "code_sha256": _code_sha256(),
                    "payload_checksums_sha256": checks_sha, "payload_file_count": len(checks),
                    "payload_total_bytes": sum(v["bytes"] for v in checks.values()),
                    "qa_files": resolved_qa_files, **_qa_summary(root, resolved_qa_files)}
        artifact_id = _sha_bytes(_canonical(artifact))
        artifact["artifact_id"] = artifact_id
        receipt = {"generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                   "python": sys.version, "platform": platform.platform(), "workspace": str(root.resolve())}
        checks_bytes = _canonical(checks_doc); artifact_bytes = _canonical(artifact); receipt_bytes = _canonical(receipt)
        # Detect source changes before committing manifests.
        if _payload_checksums(root) != checks:
            raise ArtifactError("artifact changed during hashing")
        staged: list[tuple[Path, Path]] = []
        try:
            for name, data in (("checksums.json", checks_bytes), ("artifact.json", artifact_bytes), ("run_receipt.json", receipt_bytes)):
                fd, tmp = tempfile.mkstemp(prefix=f".{name}.", dir=root); os.close(fd); p = Path(tmp); p.write_bytes(data); staged.append((root / name, p))
            final_snapshot = _payload_checksums(root, ignore={tmp for _, tmp in staged})
            if final_snapshot != checks or _payload_checksums(root, ignore={tmp for _, tmp in staged}) != final_snapshot:
                raise ArtifactError("artifact changed during final hashing")
            backups: list[tuple[Path, Path]] = []; committed: list[Path] = []
            try:
                for target, _ in staged:
                    if target.exists():
                        fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.backup.", dir=root); os.close(fd); backup = Path(tmp); backup.unlink(); os.replace(target, backup); backups.append((target, backup))
                for target, tmp in staged:
                    os.replace(tmp, target); committed.append(target)
            except Exception as exc:
                for target in committed:
                    if target.exists(): target.unlink()
                for target, backup in reversed(backups):
                    if backup.exists(): os.replace(backup, target)
                raise ArtifactError("artifact commit failed") from exc
            if _payload_checksums(root) != checks:
                try:
                    for target in committed:
                        if target.exists(): target.unlink()
                    for target, backup in reversed(backups):
                        if backup.exists(): os.replace(backup, target)
                except OSError as exc:
                    raise ArtifactError("artifact post-commit rollback failed") from exc
                raise ArtifactError("artifact changed after commit")
            warnings: list[str] = []
            for _, backup in backups:
                try:
                    if backup.exists(): backup.unlink()
                except OSError:
                    warnings.append("backup cleanup warning")
            result = {"artifact_id": artifact_id, "paths": {"artifact": str(root / "artifact.json"), "checksums": str(root / "checksums.json"), "receipt": str(root / "run_receipt.json")}}
            if warnings: result["cleanup_warnings"] = warnings
            return result
        finally:
            for _, tmp in staged:
                try:
                    if tmp.exists(): tmp.unlink()
                except OSError:
                    pass
    finally:
        try:
            lock.unlink()
        except OSError as exc:
            raise ArtifactError("artifact lock cleanup failed") from exc


def commit_file_set(targets: dict[str | Path, Any], *, replace: bool = False) -> list[str]:
    """Write a whole output set or none of it.

    ``targets`` maps each destination path either to a writer callable that
    receives an open UTF-8 text stream, or to a ``bytes`` payload written
    verbatim (for compressed or otherwise binary members).  Every file is
    written to a sibling temporary first; only once all of them exist are the
    destinations replaced, and a failure part-way through the replacement
    restores the previous versions.  A run that dies between two
    ``open(...,"w")`` calls otherwise leaves a directory holding a new version
    of some files and last week's version of the rest, which is
    indistinguishable from a good run once the process is gone.

    Returns the committed paths.  With ``replace=False`` an existing destination
    is refused before anything is written.
    """
    items = [(Path(p), writer) for p, writer in targets.items()]
    if not items:
        return []
    if not replace:
        existing = [str(p) for p, _ in items if p.exists()]
        if existing:
            raise ArtifactError("output exists; pass replace=True")
    staged: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    committed: list[Path] = []
    try:
        for target, writer in items:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
            os.close(fd)
            tmp = Path(name)
            staged.append((target, tmp))
            if isinstance(writer, (bytes, bytearray)):
                with tmp.open("wb") as raw:
                    raw.write(writer)
                    raw.flush()
                    os.fsync(raw.fileno())
            else:
                with tmp.open("w", encoding="utf-8", newline="\n") as stream:
                    writer(stream)
                    stream.flush()
                    os.fsync(stream.fileno())
        try:
            for target, _ in staged:
                if target.exists():
                    fd, name = tempfile.mkstemp(prefix=f".{target.name}.backup.", dir=target.parent)
                    os.close(fd)
                    backup = Path(name)
                    backup.unlink()
                    os.replace(target, backup)
                    backups.append((target, backup))
            for target, tmp in staged:
                os.replace(tmp, target)
                committed.append(target)
        except Exception as exc:
            for target in committed:
                if target.exists():
                    target.unlink()
            for target, backup in reversed(backups):
                if backup.exists():
                    os.replace(backup, target)
            raise ArtifactError("output commit failed; previous set restored") from exc
        for _, backup in backups:
            if backup.exists():
                backup.unlink()
    finally:
        for _, tmp in staged:
            if tmp.exists():
                tmp.unlink()
    return [str(p) for p, _ in items]


def commit_sealed_directory(target: str | Path, payloads: dict[str, Any], *,
                            artifact_type: str, source_authority: str, evidence_status: str,
                            release_classification: str,
                            input_artifact_ids: list[str] | None = None,
                            config_path: str | Path | None = None,
                            qa_files: list[str] | None = None,
                            preserve_existing: bool = True,
                            acknowledge_internal_use: bool = False,
                            strict_code: bool = True) -> dict[str, Any]:
    """Generate, seal and publish a bundle as one operation.

    ``commit_file_set`` makes the *payload* swap atomic, but a generator that
    writes into an already-sealed directory still leaves the previous run's
    ``artifact.json`` sitting beside the new files: verification then reports a
    mismatch, and anything reading the directory between the two steps sees a
    seal that describes bytes which are gone.  Here the whole bundle is built in
    a sibling staging directory, sealed and verified there, swapped into place
    under a backup, then re-sealed in place so the receipt names the published
    location.  Any failure restores the previous directory untouched.

    ``payloads`` maps a relative name to a writer callable or a ``bytes``
    payload, exactly as :func:`commit_file_set` does.  With
    ``preserve_existing`` the files already in ``target`` that this run does not
    generate are carried across byte-for-byte, so a bundle shared by several
    generators does not lose the other generators' outputs.
    """
    out = Path(target)
    parent = out.parent
    parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        _safe_root(out)
        if not out.is_dir():
            raise ArtifactError("artifact target is not a directory")
    for rel in payloads:
        p = Path(rel)
        if p.is_absolute() or ".." in p.parts or not p.parts:
            raise ArtifactError("artifact payload name rejected")
    stage = Path(tempfile.mkdtemp(prefix="." + out.name + ".stage-", dir=parent))
    backup: Path | None = None
    try:
        if preserve_existing and out.is_dir():
            generated = {Path(rel).as_posix() for rel in payloads}
            for path in sorted(out.rglob("*")):
                if path.is_dir():
                    continue
                rel = path.relative_to(out).as_posix()
                if rel in generated or (path.parent == out and path.name in MANIFEST_NAMES):
                    continue
                if path.parent == out and any(
                        path.name.startswith(f".{m}.backup.")
                        for m in ("artifact.json", "checksums.json", "run_receipt.json")):
                    continue
                _safe_file(path, out)
                destination = stage / Path(rel)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(path.read_bytes())
        commit_file_set({stage / Path(rel): writer for rel, writer in payloads.items()},
                        replace=True)
        seal_options = {"artifact_type": artifact_type, "source_authority": source_authority,
                        "evidence_status": evidence_status,
                        "release_classification": release_classification,
                        "input_artifact_ids": input_artifact_ids, "config_path": config_path,
                        "qa_files": qa_files, "replace": True,
                        "acknowledge_internal_use": acknowledge_internal_use}
        seal_artifact(stage, **seal_options)
        staged_check = verify_artifact(stage, strict_code=strict_code)
        if not staged_check.get("ok"):
            raise ArtifactError("staged artifact verification failed")
        if out.exists():
            backup = parent / ("." + out.name + ".backup-" + next(tempfile._get_candidate_names()))
            os.replace(out, backup)
        os.replace(stage, out)
        stage = out  # the staging directory no longer exists under its old name
        # Re-seal in place: the receipt written in the staging directory names a
        # path that no longer exists, and a bundle must not describe itself as
        # having been produced somewhere it is not.
        seal = seal_artifact(out, **seal_options)
        published = verify_artifact(out, strict_code=strict_code)
        if not published.get("ok"):
            raise ArtifactError("published artifact verification failed")
    except Exception as exc:
        rollback_error: Exception | None = None
        try:
            if stage != out and stage.exists():
                shutil.rmtree(stage, ignore_errors=False)
            # Once stage has been renamed to out, a first publication has no
            # backup to restore.  It still must be removed if resealing or the
            # published verification fails; otherwise a failed call leaves a
            # directory that looks successfully published.
            if stage == out and out.exists():
                shutil.rmtree(out, ignore_errors=False)
            if backup is not None and backup.exists():
                if out.exists():
                    shutil.rmtree(out, ignore_errors=False)
                os.replace(backup, out)
        except Exception as cleanup_exc:
            rollback_error = cleanup_exc
        if rollback_error is not None:
            raise ArtifactError("sealed directory rollback failed") from rollback_error
        if isinstance(exc, ArtifactError):
            raise
        raise ArtifactError("sealed directory commit failed") from exc
    warnings: list[str] = []
    if backup is not None and backup.exists():
        try:
            shutil.rmtree(backup, ignore_errors=False)
        except OSError as cleanup_exc:
            warnings.append(f"backup_cleanup_failed:{backup.name}:{type(cleanup_exc).__name__}")
    result = {"artifact_id": seal["artifact_id"], "output_dir": str(out),
              "verified": published.get("ok"), "strict_code": strict_code}
    if warnings:
        result["cleanup_warnings"] = warnings
    return result


def verify_artifact(root: str | Path, *, strict_code: bool = False, strict_config: str | Path | None = None) -> dict[str, Any]:
    root = Path(root)
    _safe_root(root)
    try:
        artifact = json.loads((root / "artifact.json").read_text(encoding="utf-8"))
        checks_doc = json.loads((root / "checksums.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ArtifactError("artifact manifests rejected") from exc
    if not isinstance(artifact, dict) or not isinstance(checks_doc, dict):
        raise ArtifactError("artifact manifest schema rejected")
    checks = checks_doc.get("files", {}); errors: list[str] = []
    try:
        receipt = json.loads((root / "run_receipt.json").read_text(encoding="utf-8"))
        if not isinstance(receipt, dict) or not all(receipt.get(k) for k in ("generated_at", "python", "platform", "workspace")):
            errors.append("run_receipt_schema_invalid")
    except (OSError, json.JSONDecodeError):
        errors.append("run_receipt_missing_or_invalid")
    if (artifact.get("schema_version") != SCHEMA_VERSION or not isinstance(artifact.get("artifact_type"), str) or not artifact.get("artifact_type") or len(artifact.get("artifact_type", "")) > 128 or
            artifact.get("source_authority") not in AUTHORITIES or artifact.get("evidence_status") not in EVIDENCE or
            artifact.get("release_classification") not in RELEASES):
        errors.append("artifact_schema_or_classification_invalid")
    ids = artifact.get("input_artifact_ids")
    if (not isinstance(ids, list) or ids != sorted(set(ids)) or
            any(not isinstance(x, str) or not re.fullmatch(r"[0-9a-f]{64}", x) for x in ids)):
        errors.append("input_artifact_ids_invalid")
    if artifact.get("release_classification") == "public_allowed" and artifact.get("public_release_reviewed") is not True:
        errors.append("public_release_review_evidence_missing")
    if checks_doc.get("schema_version") != SCHEMA_VERSION or not isinstance(checks, dict):
        errors.append("checksums_schema_invalid")
        checks = {} if not isinstance(checks, dict) else checks
    for rel, expected in checks.items():
        if (not isinstance(rel, str) or not rel or Path(rel).is_absolute() or ".." in Path(rel).parts or
                not isinstance(expected, dict) or not isinstance(expected.get("bytes"), int) or expected.get("bytes") < 0 or
                not isinstance(expected.get("sha256"), str) or not re.fullmatch(r"[0-9a-f]{64}", expected.get("sha256", ""))):
            errors.append("checksums_entry_invalid")
    if _sha_bytes(_canonical(checks_doc)) != artifact.get("payload_checksums_sha256"):
        errors.append("checksums_digest_mismatch")
    if artifact.get("payload_file_count") != len(checks) or artifact.get("payload_total_bytes") != sum(int(v.get("bytes", -1)) for v in checks.values()):
        errors.append("payload_summary_mismatch")
    try:
        actual_paths = {p.relative_to(root).as_posix() for p in _payload_files(root)}
        if actual_paths != set(checks):
            errors.append("payload_file_set_mismatch")
    except ArtifactError:
        errors.append("payload_path_rejected")
    # The QA aggregation is part of the seal, not a convenience field: a
    # manifest that omits a QA payload file, or that carries counts which do not
    # follow from the sealed bytes, is rejected.
    declared_qa = artifact.get("qa_files")
    if (not isinstance(declared_qa, list) or declared_qa != sorted(set(declared_qa)) or
            any(not isinstance(x, str) or not x or Path(x).is_absolute() or ".." in Path(x).parts
                for x in declared_qa)):
        errors.append("qa_files_invalid")
    else:
        try:
            if set(declared_qa) != set(_discover_qa_files(root)) or not set(declared_qa) <= set(checks):
                errors.append("qa_file_set_mismatch")
            elif _qa_summary(root, declared_qa) != {"qa_total_count": artifact.get("qa_total_count"),
                                                    "qa_error_count": artifact.get("qa_error_count")}:
                errors.append("qa_summary_mismatch")
        except (ArtifactError, OSError, UnicodeDecodeError):
            errors.append("qa_files_unreadable")
    for rel, expected in checks.items():
        path = root / Path(rel)
        try: _safe_file(path, root)
        except ArtifactError: errors.append("payload_path_rejected"); continue
        if not path.is_file(): errors.append(f"missing:{rel}"); continue
        digest, size = _hash_file(path)
        if digest != expected.get("sha256") or size != expected.get("bytes"): errors.append(f"payload_mismatch:{rel}")
    recomputed = dict(artifact); actual_id = recomputed.pop("artifact_id", None)
    if _sha_bytes(_canonical(recomputed)) != actual_id: errors.append("artifact_id_mismatch")
    if strict_code and artifact.get("code_sha256") != _code_sha256(): errors.append("code_sha256_mismatch")
    if strict_config is not None and artifact.get("config_sha256") != _config_hash(strict_config): errors.append("config_sha256_mismatch")
    return {"ok": not errors, "artifact_id": actual_id, "errors": errors, "public_release_allowed": artifact.get("release_classification") == "public_allowed"}
