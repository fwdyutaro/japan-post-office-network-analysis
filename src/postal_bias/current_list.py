"""Local, auditable parser for Japan Post's current-list PDF.

The module deliberately has no network access.  A caller supplies a PDF that it
has already obtained under the acquisition policy, and this module keeps only
structured records plus provenance (never a copy of the source PDF).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

MAX_PDF_BYTES = 50 * 1024 * 1024
MAX_TEXT_BYTES = 20 * 1024 * 1024
DEFAULT_TIMEOUT = 180
# 0.2.0 fixed the overlapping-prefecture name/address split (東京都府中市 also
# matches 京都府 one character in); see ``_find_address_start``.  The constant
# had been left at 0.1.0 while the behaviour had already changed.
PARSER_VERSION = "0.2.0"
ID_RE = re.compile(r"^[0-9]{6}[A-Za-z]?$")
ROW_RE = re.compile(r"^\s*(\d+)\s+(\S+)\s+(.*)$")
STATUS_RE = re.compile(r"(一時閉鎖|営業中)\s*$")
NAME_TAIL_RE = re.compile(r"(郵便局|分室|出張所)$")
# Marks in the service columns are standalone cells.  A hyphen embedded in an
# address (e.g. 4－3－1) must not be mistaken for a column marker.  Older change
# notices (2013-2017) render the "not applicable" cell as U+2015 HORIZONTAL BAR
# or U+2500 BOX DRAWINGS LIGHT HORIZONTAL rather than a hyphen variant.
DASHES = "-‐‑‒–—−－―─"
# The circle mark is rendered as U+25CB or, in some producers, U+3007.
CIRCLES = "○〇"
# ``re.escape`` keeps the literal hyphen from forming a character range.
MARK_RE = re.compile(r"(?<=\s)[" + CIRCLES + "×" + re.escape(DASHES) + "]")
PREFECTURES = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "茨城県", "栃木県", "群馬県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
)


class CurrentListError(ValueError):
    """Safe, user-actionable input or parser error."""


class PdfTextError(CurrentListError):
    pass


def display_width(value: str) -> int:
    """Return terminal-style CJK display width without external dependencies."""
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in value)


def slice_display(value: str, start: int, end: int | None = None) -> str:
    """Slice by display cells, preserving a deterministic fixed-width layout."""
    out: list[str] = []
    pos = 0
    limit = end if end is not None else 10**9
    for ch in value:
        w = 2 if unicodedata.east_asian_width(ch) in "WF" else 1
        if pos >= limit:
            break
        if pos >= start and pos + w <= limit:
            out.append(ch)
        pos += w
    return "".join(out)


def _norm_space(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\u3000", " ")).strip()


def _norm_mark(value: str, *, allow_blank: bool = True) -> str:
    raw = _norm_space(value)
    if not raw and allow_blank:
        return "blank"
    if any(ch in raw for ch in CIRCLES):
        return "yes"
    if "×" in raw:
        return "no"
    if any(ch in raw for ch in DASHES):
        return "not_applicable"
    return "unknown"


def _raw_norm(raw: str, kind: str) -> dict[str, str]:
    return {"raw": raw, "normalized": _norm_mark(raw) if kind == "mark" else _norm_space(raw)}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def source_document_id(source_pdf_sha256: str) -> str:
    """Stable source identifier derived only from the source digest."""
    return "src-" + _sha(source_pdf_sha256 or "unknown")


def validate_pdf(path: str | Path, max_bytes: int = MAX_PDF_BYTES) -> Path:
    path = Path(path)
    if not path.is_file():
        raise PdfTextError(f"PDF does not exist: {path}")
    size = path.stat().st_size
    if size > max_bytes:
        raise PdfTextError(f"PDF exceeds {max_bytes} byte limit")
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise PdfTextError("input is not a PDF (magic mismatch)")
    return path


def run_pdftotext(path: str | Path, executable: str | Path | None = None,
                  *, timeout: int = DEFAULT_TIMEOUT,
                  max_text_bytes: int = MAX_TEXT_BYTES) -> tuple[str, str]:
    path = validate_pdf(path)
    exe = Path(executable) if executable else Path("pdftotext")
    if executable and (not exe.is_file() or not exe.is_absolute()):
        raise PdfTextError(f"pdftotext executable must be an existing absolute file: {exe}")
    args = [str(exe), "-layout", "-enc", "UTF-8", str(path), "-"]
    proc = None
    stdout_data = bytearray()
    stderr_data = bytearray()
    limit_event = threading.Event()

    def drain_stdout() -> None:
        try:
            while True:
                chunk = proc.stdout.read(64 * 1024)
                if not chunk:
                    break
                stdout_data.extend(chunk)
                if len(stdout_data) > max_text_bytes:
                    limit_event.set()
                    try:
                        proc.kill()
                    except OSError:
                        pass
                    break
        finally:
            pass

    def drain_stderr() -> None:
        try:
            while True:
                chunk = proc.stderr.read(4096)
                if not chunk:
                    break
                if len(stderr_data) < 8192:
                    stderr_data.extend(chunk[:8192 - len(stderr_data)])
        finally:
            pass

    try:
        proc = subprocess.Popen(args, shell=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout_thread = threading.Thread(target=drain_stdout, daemon=True)
        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stdout_thread.start(); stderr_thread.start()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            try:
                proc.kill(); proc.wait(timeout=5)
            except Exception:
                pass
            stdout_thread.join(timeout=2); stderr_thread.join(timeout=2)
            for stream in (proc.stdout, proc.stderr):
                try:
                    stream.close()
                except Exception:
                    pass
            raise PdfTextError("pdftotext timed out") from exc
        stdout_thread.join(timeout=5); stderr_thread.join(timeout=5)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except Exception:
                pass
        if limit_event.is_set():
            raise PdfTextError(f"pdftotext output exceeds {max_text_bytes} byte limit")
        raw = bytes(stdout_data)
    except subprocess.TimeoutExpired as exc:
        raise PdfTextError("pdftotext timed out") from exc
    except OSError as exc:
        raise PdfTextError(f"cannot execute pdftotext: {exc}") from exc
    if proc.returncode != 0:
        err = bytes(stderr_data).decode("utf-8", errors="replace")[:500]
        raise PdfTextError(f"pdftotext failed ({proc.returncode}): {err}")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PdfTextError("pdftotext output is not UTF-8") from exc
    version = "unknown"
    try:
        v = subprocess.run([str(exe), "-v"], shell=False, capture_output=True, timeout=10)
        version = (v.stderr or v.stdout).decode("utf-8", errors="replace").splitlines()[0][:200]
    except Exception:
        pass
    return text, version


def _find_address_start(value: str) -> int | None:
    spans = sorted({(match.start(), match.end()) for pref in PREFECTURES
                    for match in re.finditer(re.escape(pref), value)})
    # Prefecture names can overlap: 東京都府中市 contains 京都府 one character
    # into 東京都.  A candidate that begins inside an earlier candidate is that
    # spurious tail, never the real start of the address.
    outer = [start for index, (start, end) in enumerate(spans)
             if not any(other_start < start < other_end for other_start, other_end in spans[:index])]
    # Office names may themselves contain a prefecture name (e.g. 石川県立
    # 中央病院内簡易郵便局).  The address occurrence is the rightmost one.
    return max(outer) if outer else None


def _field(raw: str, normalized: str | None = None) -> dict[str, str]:
    return {"raw": raw, "normalized": _norm_space(raw) if normalized is None else normalized}


def _parse_row(line: str, page: int, line_no: int, section: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    match = ROW_RE.match(line)
    if not match:
        return None, None
    serial = int(match.group(1))
    identifier_raw = match.group(2)
    identifier = unicodedata.normalize("NFKC", identifier_raw).lower()
    if not ID_RE.fullmatch(identifier):
        return None, {"kind": "error", "code": "invalid_identifier", "page": page, "line": line_no}
    body = match.group(3)
    status_match = STATUS_RE.search(body)
    if not status_match:
        return None, {"kind": "error", "code": "missing_status", "page": page, "line": line_no}
    status_raw = status_match.group(1)
    body_without_status = body[:status_match.start()].rstrip()
    address_start = _find_address_start(body_without_status)
    if address_start is None:
        # A small number of official addresses intentionally omit the
        # prefecture (for example a land-readjustment project name).  Fall
        # back to the first multi-space column gap rather than dropping it.
        gap = re.search(r"\s{2,}", body_without_status)
        if gap:
            address_start = gap.end()
        else:
            return None, {"kind": "error", "code": "missing_address", "page": page, "line": line_no}
    before_address = body_without_status[:address_start].rstrip()
    # A simple-office mark is the final mark immediately before the address.
    simple_raw = ""
    if before_address.endswith(tuple(CIRCLES)):
        simple_raw = "○"
        before_address = before_address[:-1].rstrip()
    name_raw = before_address
    # The PDF keeps service cells at stable offsets from the right-aligned
    # status column.  This is more reliable than counting marks because blank
    # cells contain no glyph at all.  Company-office tables shift only the last
    # two service columns by one cell.
    status_local = status_match.start()
    service_offsets = (-50, -44, -38, -33, -23) if section == "postal_office" else (-50, -44, -38, -32, -26)
    service_targets = [status_local + x for x in service_offsets]

    def cell_mark(target: int) -> str:
        for pos in range(max(0, target - 2), min(len(body_without_status), target + 3)):
            ch = body_without_status[pos]
            if ch in "○×-‐‑‒–—−－":
                return ch
        return ""

    post_address_marks = list(MARK_RE.finditer(body_without_status[address_start:]))
    trailing_start = service_targets[-1] + 4
    # Service columns are a fixed header-derived pattern.  Fit observed glyphs
    # to these cells with a small layout tolerance; never derive a pitch from
    # adjacent glyphs because blank cells intentionally create larger gaps.
    if section == "postal_office":
        service_pattern = (0, 6, 12, 17, 27)
    else:
        service_pattern = (0, 6, 12, 18, 24)
    marker_positions = [address_start + item.start() for item in post_address_marks]
    address_end = marker_positions[0] if marker_positions else service_targets[0] - 3
    assigned: dict[str, str] = {}
    unmatched_marks = list(marker_positions)
    if marker_positions:
        expected_names = [("disadvantaged_area", -4)] + [(f"service_{i}", x) for i, x in enumerate(service_pattern)]
        best: tuple[int, int, dict[str, str], list[int]] | None = None
        for anchor in range(min(marker_positions) - 8, max(marker_positions) + 9):
            used: set[int] = set(); got: dict[str, str] = {}; distance = 0
            for name, offset in expected_names:
                candidates = [(abs(pos - (anchor + offset)), idx, pos) for idx, pos in enumerate(marker_positions)
                              if idx not in used and abs(pos - (anchor + offset)) <= 2]
                if candidates:
                    d, idx, pos = min(candidates)
                    used.add(idx); got[name] = body_without_status[pos]; distance += d
            score = (len(used), -distance, got, [pos for idx, pos in enumerate(marker_positions) if idx not in used])
            if best is None or score[:2] > best[:2]:
                best = score
        if best:
            _, _, assigned, unmatched_marks = best
    disadvantaged_raw = assigned.get("disadvantaged_area", "")
    service_values = [assigned.get(f"service_{i}", "") for i in range(5)]
    if unmatched_marks:
        # Keep a deterministic right-column fallback for unusual synthetic
        # layouts, but mark it untrusted so it cannot enter analysis silently.
        disadvantaged_raw = cell_mark(status_local - 54)
        service_values = [cell_mark(target) for target in service_targets]
        remaining = marker_positions[1:] if disadvantaged_raw and marker_positions else marker_positions
        if marker_positions:
            trailing_start = marker_positions[-1] + 1
        if remaining:
            gaps = [b - a for a, b in zip(remaining, remaining[1:])]
            if gaps and max(gaps) <= 3:
                origin = remaining[0]
                service_values = ["" for _ in range(5)]
                for pos in remaining:
                    slot = round((pos - origin) / 2)
                    if 0 <= slot < 5:
                        service_values[slot] = body_without_status[pos]
    # An address ends before the first service cell, with the fixed-width
    # padding removed.  The fallback prefecture/gap split above still handles
    # addresses that have no prefecture name.
    address_raw = body_without_status[address_start:max(address_start, address_end)].rstrip()
    trailing = body_without_status[trailing_start:status_local].strip()
    expected_cells = [status_local - 54, *service_targets]
    if not post_address_marks:
        unmatched_marks = []
    # Related-office and notes are separate source columns, but the PDF may
    # collapse blank columns.  A pipe is accepted in synthetic/QA fixtures;
    # otherwise all non-status trailing text is conservatively related-office.
    if "|" in trailing:
        related_raw, notes_raw = (trailing.split("|", 1) + [""])[:2]
    else:
        related_raw, notes_raw = trailing, ""
    raw_fields = {
        "name": name_raw, "simple_post_office": simple_raw, "address": address_raw,
        "disadvantaged_area": disadvantaged_raw, "related_office": related_raw,
        "notes": notes_raw, "operating_status": status_raw,
    }
    record = {
        "section": section,
        "serial": serial,
        "official_identifier": identifier,
        "official_identifier_raw": identifier_raw,
        "name": _field(name_raw),
        "simple_post_office": _raw_norm(simple_raw, "mark"),
        "address": _field(address_raw),
        "disadvantaged_area": _raw_norm(disadvantaged_raw, "mark"),
        "services": [_raw_norm(v, "mark") for v in service_values],
        "related_office": _field(related_raw),
        "notes": _field(notes_raw),
        "operating_status": {"raw": status_raw, "normalized": "temporarily_closed" if status_raw == "一時閉鎖" else "operating"},
        "source_page": page,
        "source_line": line_no,
        "source_line_sha256": _sha(line),
        "parse_confidence": "low" if unmatched_marks else "high",
        "_unmatched_layout_marks": unmatched_marks,
        "_raw_fields": raw_fields,
    }
    return record, None


def parse_layout_text(text: str, *, expected_profile: dict[str, int] | None = None,
                      source_pdf_sha256: str = "") -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    qa: list[dict[str, Any]] = []
    section: str | None = None
    note_counts: list[tuple[int, int, int]] = []
    for page_no, page_text in enumerate(text.split("\f"), 1):
        page_header = False
        lines = page_text.splitlines()
        for line_no, line in enumerate(lines, 1):
            if "（郵便局）" in line or "(郵便局)" in line:
                section = "postal_office"
            elif "（会社の営業所）" in line or "(会社の営業所)" in line:
                section = "company_office"
            if "別記様式" in line:
                page_header = True
            if page_header and not ROW_RE.match(line):
                continue
            if ROW_RE.match(line):
                page_header = False
            if "（郵便局）" in line or "(郵便局)" in line:
                section = "postal_office"
            elif "（会社の営業所）" in line or "(会社の営業所)" in line:
                section = "company_office"
            fullwidth_digits = str.maketrans("０１２３４５６７８９", "0123456789")
            note_match = re.search(r"長期に営業を休止している簡易郵便局.*?([０-９0-9,，]+)局", line)
            if note_match:
                try:
                    n = int(note_match.group(1).translate(fullwidth_digits).replace(",", "").replace("，", ""))
                    note_counts.append((n, page_no, line_no))
                except ValueError:
                    qa.append({"kind": "error", "code": "invalid_long_term_note", "page": page_no, "line": line_no})
            if ("通番" in line or "法第６条" in line or "別記様式" in line or
                    "番号" in line or "中の別" in line or not line.strip()):
                continue
            if line.lstrip().startswith(("注", "※", "「関連銀行」")) or any(
                    phrase in line for phrase in ("当該郵便局又は当該営業所", "関連銀行又は関連保険会社")):
                continue
            if section is None:
                if line.strip():
                    qa.append({"kind": "error", "code": "outside_section", "page": page_no,
                               "line": line_no, "line_sha256": _sha(line)})
                continue
            if ROW_RE.match(line):
                record, error = _parse_row(line, page_no, line_no, section)
                if record:
                    unmatched_layout_marks = record.pop("_unmatched_layout_marks", [])
                    record.pop("_raw_fields", None)
                    if source_pdf_sha256:
                        record["source_document_id"] = source_document_id(source_pdf_sha256)
                    records.append(record)
                    if unmatched_layout_marks:
                        qa.append({"kind": "error", "code": "layout_untrusted",
                                   "page": page_no, "line": line_no,
                                   "identifier": record["official_identifier"],
                                   "positions": unmatched_layout_marks})
                    # Never silently coerce a new glyph/value.  Keep the row
                    # for auditability, but put an explicit QA error beside it.
                    normalized_values = [record["simple_post_office"]["normalized"],
                                         record["disadvantaged_area"]["normalized"],
                                         record["operating_status"]["normalized"]]
                    normalized_values.extend(item["normalized"] for item in record["services"])
                    if "unknown" in normalized_values:
                        qa.append({"kind": "error", "code": "unknown_cell_value",
                                   "page": page_no, "line": line_no,
                                   "identifier": record["official_identifier"]})
                elif error:
                    qa.append(error)
            elif line.strip() and not any(token in line for token in ("整理", "名称", "所在地", "業務", "営業中", "一時閉鎖")):
                qa.append({"kind": "error", "code": "unparsed_line", "page": page_no,
                           "line": line_no, "line_sha256": _sha(line)})
    # Section and integrity checks.
    for sec in ("postal_office", "company_office"):
        rows = [r for r in records if r["section"] == sec]
        serials = [r["serial"] for r in rows]
        if serials and serials != list(range(1, len(serials) + 1)):
            qa.append({"kind": "error", "code": "sequence_gap_or_duplicate", "section": sec,
                       "first": serials[:3], "last": serials[-3:]})
    seen: set[str] = set()
    for record in records:
        ident = record["official_identifier"]
        if ident in seen:
            qa.append({"kind": "error", "code": "duplicate_identifier", "identifier": ident})
        seen.add(ident)
        if len(record["services"]) != 5:
            qa.append({"kind": "error", "code": "invalid_service_count", "identifier": ident})
        # Every official facility name ends in 郵便局 / 分室 / 出張所.  A name
        # that does not is the signature of a name/address column split that
        # landed one character late, which silently reassigns the prefecture
        # (東京都府中市 read as 京都府中市) and cannot be caught downstream.
        if not NAME_TAIL_RE.search(record["name"]["normalized"]):
            qa.append({"kind": "error", "code": "name_column_boundary_suspect",
                       "identifier": ident, "page": record["source_page"], "line": record["source_line"]})
    # A PDF producer may encode the final explanatory note on a page whose
    # repeated header marker is retained in the text stream.  Recover the
    # aggregate independently of row/header state while preserving provenance.
    if not note_counts:
        for pageno, page_text in enumerate(text.split("\f"), 1):
            for lno, candidate in enumerate(page_text.splitlines(), 1):
                if "長期" not in candidate:
                    continue
                m = re.search(r"([０-９0-9,，]+)局", candidate)
                if not m:
                    continue
                digits = m.group(1).translate(str.maketrans("０１２３４５６７８９", "0123456789")).replace(",", "").replace("，", "")
                try:
                    note_counts.append((int(digits), pageno, lno))
                except ValueError:
                    pass
    distinct_note_values = {x[0] for x in note_counts}
    if not note_counts or len(distinct_note_values) != 1:
        qa.append({"kind": "error", "code": "long_term_note_missing_or_conflicting", "occurrences": len(note_counts)})
    counts = {
        "postal_office": sum(r["section"] == "postal_office" for r in records),
        "company_office": sum(r["section"] == "company_office" for r in records),
        "total": len(records),
        "long_term_suspended_simple_post_offices": next(iter(distinct_note_values)) if len(distinct_note_values) == 1 else None,
    }
    if expected_profile:
        for key, expected in expected_profile.items():
            actual_key = "long_term_suspended_simple_post_offices" if key == "long-term" else key
            if counts.get(actual_key) != expected:
                qa.append({"kind": "error", "code": "profile_count_mismatch", "field": actual_key,
                           "expected": expected, "actual": counts.get(actual_key)})
    return {
        "records": records,
        "qa": qa,
        "metadata": {
            "parser_version": PARSER_VERSION,
            "source_document_id": source_document_id(source_pdf_sha256),
            # The digest the rows were stamped from, so ``write_outputs`` can
            # refuse to seal a parse that came from a different PDF instead of
            # quietly relabelling it.  Empty when the caller supplied none.
            "parsed_source_pdf_sha256": (source_pdf_sha256 or "").strip().lower(),
            "counts": counts,
            "unlisted_long_term_suspended_simple_post_offices": counts["long_term_suspended_simple_post_offices"],
            "unlisted_scope": "unlisted_aggregate_only",
            "note_source": ({"page": note_counts[0][1], "line": note_counts[0][2]} if note_counts else None),
            "qa_error_count": sum(x.get("kind") == "error" for x in qa),
        },
    }


def _atomic_json(path: Path, value: Any, *, replace: bool) -> None:
    if path.exists() and not replace:
        raise CurrentListError(f"output exists; pass --replace explicitly: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def _stage_text(path: Path, writer: Any) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            writer(stream)
            stream.flush(); os.fsync(stream.fileno())
    except Exception:
        if os.path.exists(tmp_name): os.unlink(tmp_name)
        raise
    return tmp_name


def write_outputs(result: dict[str, Any], pdf_path: str | Path, coverage_date: str,
                  output_dir: str | Path, *, replace: bool = False,
                  acknowledge_internal_use: bool = False,
                  pdftotext_version: str = "unknown",
                  expected_profile: dict[str, int] | None = None) -> dict[str, str]:
    if not acknowledge_internal_use:
        raise CurrentListError("writing detailed addresses requires --acknowledge-internal-use")
    pdf = validate_pdf(pdf_path)
    digest = hashlib.sha256(pdf.read_bytes()).hexdigest()
    document_id = source_document_id(digest)
    # A parse that declares a different source PDF is not relabelled: the rows
    # in hand were produced from other bytes and must not be sealed as this
    # document's evidence.
    parsed_digest = str(result.get("metadata", {}).get("parsed_source_pdf_sha256") or "").strip().lower()
    if parsed_digest and parsed_digest != digest:
        raise CurrentListError("parsed records were produced from a different source PDF")
    # ``setdefault`` used to leave a stale per-row ``source_document_id`` in
    # place while the metadata carried the real PDF's id, so one bundle claimed
    # two provenance chains.  The row-level value is reconciled against the PDF
    # actually hashed here, and every divergence is a QA error rather than a
    # silent survival.
    qa_rows = list(result.get("qa", []))
    records_for_output: list[dict[str, Any]] = []
    reassigned = 0
    for record in result.get("records", []):
        prior_id = record.get("source_document_id")
        if prior_id and prior_id != document_id:
            reassigned += 1
            qa_rows.append({"kind": "error", "code": "source_document_id_reassigned",
                            "official_identifier": record.get("official_identifier"),
                            "source_page": record.get("source_page"),
                            "source_line": record.get("source_line"),
                            "previous_source_document_id": prior_id,
                            "source_document_id": document_id})
        records_for_output.append({**record, "source_document_id": document_id})
    outdir = Path(output_dir)
    metadata = {
        **result["metadata"],
        "source_pdf_sha256": digest,
        "source_pdf_sha256_verified": True,
        "source_document_id": document_id,
        "source_document_id_reassigned_count": reassigned,
        "qa_error_count": sum(x.get("kind") == "error" for x in qa_rows),
        "source_pdf_name": pdf.name,
        "coverage_date": coverage_date,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pdftotext_version": pdftotext_version,
        "terms_review_status": "unverified_detail_pdf",
        "public_release_allowed": False,
        "contains_detailed_addresses": True,
        "processing_display": "internal research only; source PDF not copied",
        "expected_profile": expected_profile,
    }
    paths = {"metadata": str(outdir / "metadata.json"), "records": str(outdir / "records.jsonl"), "qa": str(outdir / "qa.jsonl")}
    if not replace:
        existing = [p for p in paths.values() if Path(p).exists()]
        if existing:
            raise CurrentListError(f"output exists; pass --replace explicitly: {existing[0]}")
    targets = [Path(paths["metadata"]), Path(paths["records"]), Path(paths["qa"])]
    staged: list[str] = []
    try:
        staged.append(_stage_text(targets[0], lambda s: (json.dump(metadata, s, ensure_ascii=False, indent=2), s.write("\n"))))
        for target, rows in ((targets[1], records_for_output), (targets[2], qa_rows)):
            staged.append(_stage_text(target, lambda s, rows=rows: [s.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n") for row in rows]))
    except OSError as exc:
        for temp in staged:
            if os.path.exists(temp): os.unlink(temp)
        raise CurrentListError("cannot stage current-list output files") from exc
    backups: list[tuple[Path, str]] = []
    committed: list[Path] = []
    try:
        for target in targets:
            if target.exists():
                bfd, backup = tempfile.mkstemp(prefix=f".{target.name}.backup.", dir=target.parent)
                os.close(bfd); os.unlink(backup)
                os.replace(target, backup)
                backups.append((target, backup))
        for target, temp in zip(targets, staged):
            os.replace(temp, target)
            committed.append(target)
    except Exception as exc:
        for target in committed:
            if target.exists() and target.is_file():
                target.unlink()
        for target, backup in reversed(backups):
            if os.path.exists(backup): os.replace(backup, target)
        for temp in staged:
            if os.path.exists(temp): os.unlink(temp)
        raise CurrentListError(f"atomic output commit failed; previous set restored: {exc}") from exc
    else:
        for _, backup in backups:
            if os.path.exists(backup):
                if Path(backup).is_dir():
                    import shutil; shutil.rmtree(backup)
                else:
                    os.unlink(backup)
    return paths


PROFILES = {"2026-06-30": {"postal_office": 20087, "company_office": 3372, "total": 23459, "long-term": 653}}
