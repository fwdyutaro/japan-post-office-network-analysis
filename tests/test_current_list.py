import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stdout

from postal_bias.current_list import (
    CurrentListError,
    PROFILES,
    PdfTextError,
    parse_layout_text,
    run_pdftotext,
    write_outputs,
)
from postal_bias.cli import main


def row(serial, ident, name, address, *, simple="", disadvantaged="", services=("○", "×", "-", "", "○"),
        related="", notes="", status="営業中"):
    marks = " ".join(services)
    tail = related + ("|" + notes if notes else "")
    return f"{serial:>4} {ident:<7} {name:<14}{simple:>2}  {address:<21}  {disadvantaged:>1} {marks:<35}{tail:<20}{status}"


class FakePipe:
    def __init__(self, chunks):
        self.chunks = list(chunks)
    def read(self, _size=-1):
        return self.chunks.pop(0) if self.chunks else b""
    def close(self):
        return None


class FakeProcess:
    def __init__(self, chunks, returncode=0):
        self.stdout = FakePipe(chunks)
        self.stderr = FakePipe([b""])
        self.returncode = returncode
        self.killed = False
    def wait(self, timeout=None):
        return self.returncode
    def kill(self):
        self.killed = True
        self.returncode = -9


class CurrentListParserTests(unittest.TestCase):
    def test_parse_all_fields_sections_and_page_provenance(self):
        text = (
            "別記様式第一号（郵便局）\n通番 整理番号 名称 所在地\n" +
            row(1, "000010", "東京郵便局", "東京都千代田区１－１", simple="○", disadvantaged="○",
                services=("○", "×", "-", "", "○"), related="関連銀行", notes="注記") + "\n\f" +
            "別記様式第一号（会社の営業所）\n通番 整理番号 名称 所在地\n" +
            row(1, "010160a", "会社営業所", "東京都港区２－２", status="一時閉鎖") + "\n"
        )
        result = parse_layout_text(text)
        self.assertEqual(len(result["records"]), 2)
        a, b = result["records"]
        self.assertEqual(a["section"], "postal_office")
        self.assertEqual(a["simple_post_office"]["normalized"], "yes")
        self.assertEqual(a["disadvantaged_area"]["normalized"], "yes")
        self.assertEqual([x["normalized"] for x in a["services"]], ["yes", "no", "not_applicable", "blank", "yes"])
        self.assertEqual(a["related_office"]["raw"], "関連銀行")
        self.assertEqual(a["notes"]["raw"], "注記")
        self.assertEqual(b["section"], "company_office")
        self.assertEqual(b["official_identifier"], "010160a")
        self.assertEqual(b["operating_status"]["normalized"], "temporarily_closed")
        self.assertEqual(a["source_page"], 1)
        self.assertEqual(a["source_line_sha256"], hashlib.sha256((text.split("\n")[2]).encode()).hexdigest())

    def test_note_and_profile_and_sequence(self):
        text = "（郵便局）\n" + row(1, "000010", "局", "東京都千代田区１") + "\n" + \
               "※このほか、長期に営業を休止している簡易郵便局が６５３局ある。\n"
        result = parse_layout_text(text, expected_profile={"postal_office": 1, "company_office": 0, "total": 1, "long-term": 653})
        self.assertEqual(result["metadata"]["counts"]["long_term_suspended_simple_post_offices"], 653)
        self.assertEqual(result["metadata"]["counts"]["total"], 1)

    def test_sequence_gap_and_bad_note_are_qa(self):
        text = "（郵便局）\n" + row(2, "000010", "局", "東京都千代田区１") + "\n" + \
               "※このほか、長期に営業を休止している簡易郵便局が６５３局ある。\n" + \
               "※このほか、長期に営業を休止している簡易郵便局が６５４局ある。\n"
        result = parse_layout_text(text)
        codes = {q.get("code") for q in result["qa"]}
        self.assertIn("sequence_gap_or_duplicate", codes)
        self.assertIn("long_term_note_missing_or_conflicting", codes)

    def test_chrome_is_ignored_and_unknown_line_is_fail_closed(self):
        text = "別記様式第一号（第五条関係） （郵便局）\n" + row(1, "００００１０", "局", "東京都千代田区１－１") + "\n予期しない継続行"
        result = parse_layout_text(text)
        self.assertEqual(result["records"][0]["official_identifier"], "000010")
        self.assertEqual(result["records"][0]["official_identifier_raw"], "００００１０")
        errors = [q for q in result["qa"] if q.get("code") == "unparsed_line"]
        self.assertEqual(len(errors), 1)
        self.assertIn("line_sha256", errors[0])

    def test_prefecture_in_office_name_does_not_leak_marker_or_name_into_address(self):
        line = " 869 317810 石川県立中央病院内簡易郵便局    ○ 石川県金沢市鞍月東２－１                ○     ○     ×     ○     ○                         営業中"
        result = parse_layout_text("（会社の営業所）\n" + line + "\n")
        self.assertEqual(result["metadata"]["counts"]["total"], 1)
        record = result["records"][0]
        self.assertEqual(record["name"]["raw"], "石川県立中央病院内簡易郵便局")
        self.assertEqual(record["simple_post_office"]["normalized"], "yes")
        self.assertEqual(record["address"]["raw"], "石川県金沢市鞍月東２－１")
        self.assertNotIn("○", record["address"]["raw"])

    def test_irregular_spacing_keeps_all_five_services(self):
        line = "908 010540 檜原郵便局            東京都西多摩郡檜原村４６７       ○   ○     ○     ○    ○         ○                      営業中"
        result = parse_layout_text("（郵便局）\n" + line + "\n")
        record = result["records"][0]
        self.assertEqual([x["normalized"] for x in record["services"]], ["yes"] * 5)
        self.assertEqual(record["address"]["raw"], "東京都西多摩郡檜原村４６７")

    @patch("postal_bias.current_list.subprocess.run")
    def test_pdftotext_failure_timeout_and_output_limit(self, run):
        p = Path(tempfile.mktemp(suffix=".pdf")); p.write_bytes(b"%PDF-1.7\n")
        try:
            import subprocess
            with patch("postal_bias.current_list.subprocess.Popen", return_value=FakeProcess([b"bad"], returncode=1)):
                with self.assertRaises(PdfTextError): run_pdftotext(p)
            with patch("postal_bias.current_list.subprocess.Popen", side_effect=subprocess.TimeoutExpired("pdftotext", 1)):
                with self.assertRaises(PdfTextError): run_pdftotext(p)
            process = FakeProcess([b"x" * 8, b"y" * 8, b"z" * 8])
            with patch("postal_bias.current_list.subprocess.Popen", return_value=process):
                with self.assertRaises(PdfTextError): run_pdftotext(p, max_text_bytes=20)
            self.assertTrue(process.killed)
        finally:
            p.unlink()

    def test_atomic_output_requires_ack_and_no_overwrite(self):
        result = parse_layout_text("（郵便局）\n" + row(1, "000010", "局", "東京都千代田区１") + "\n" +
                                  "※このほか、長期に営業を休止している簡易郵便局が６５３局ある。\n")
        pdf = Path(tempfile.mktemp(suffix=".pdf")); pdf.write_bytes(b"%PDF-1.7\n")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(CurrentListError): write_outputs(result, pdf, "2026-06-30", d)
            paths = write_outputs(result, pdf, "2026-06-30", d, acknowledge_internal_use=True)
            self.assertTrue(Path(paths["metadata"]).exists())
            metadata = json.loads(Path(paths["metadata"]).read_text(encoding="utf-8"))
            self.assertFalse(metadata["public_release_allowed"])
            with self.assertRaises(CurrentListError): write_outputs(result, pdf, "2026-06-30", d, acknowledge_internal_use=True)
        pdf.unlink()

    def test_atomic_output_rolls_back_on_late_commit_failure(self):
        result = parse_layout_text("（郵便局）\n" + row(1, "000010", "局", "東京都千代田区１") + "\n" +
                                  "※このほか、長期に営業を休止している簡易郵便局が６５３局ある。\n")
        pdf = Path(tempfile.mktemp(suffix=".pdf")); pdf.write_bytes(b"%PDF-1.7\n")
        try:
            with tempfile.TemporaryDirectory() as d:
                paths = write_outputs(result, pdf, "2026-06-30", d, acknowledge_internal_use=True)
                before = {k: Path(v).read_bytes() for k, v in paths.items()}
                import postal_bias.current_list as module
                original_replace = module.os.replace
                def fail_late(src, dst):
                    if str(dst).endswith("qa.jsonl") and str(src).startswith(d) and ".backup." not in str(src):
                        raise OSError("simulated commit failure")
                    return original_replace(src, dst)
                with patch.object(module.os, "replace", side_effect=fail_late):
                    with self.assertRaises(CurrentListError):
                        write_outputs(result, pdf, "2026-06-30", d, replace=True, acknowledge_internal_use=True)
                self.assertEqual({k: Path(v).read_bytes() for k, v in paths.items()}, before)
        finally:
            pdf.unlink()

    def test_inspect_cli_does_not_print_detailed_address(self):
        pdf = Path(tempfile.mktemp(suffix=".pdf")); pdf.write_bytes(b"%PDF-1.7\n")
        try:
            text = "（郵便局）\n" + row(1, "000010", "局", "東京都千代田区１") + "\n" + \
                   "※このほか、長期に営業を休止している簡易郵便局が６５３局ある。\n"
            fake = FakeProcess([text.encode()])
            with patch("postal_bias.current_list.subprocess.Popen", return_value=fake):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    self.assertEqual(main(["inspect-current-list", str(pdf), "--coverage-date", "2026-06-30"]), 0)
            self.assertNotIn("東京都千代田区", buf.getvalue())
            self.assertIn('"counts"', buf.getvalue())
        finally:
            pdf.unlink()

    def test_cli_rejects_input_without_traceback_or_local_path(self):
        import sys
        buf = io.StringIO()
        old = sys.stderr
        try:
            sys.stderr = buf
            code = main(["inspect-current-list", "C:\\private\\missing.pdf", "--coverage-date", "2026-06-30"])
        finally:
            sys.stderr = old
        self.assertEqual(code, 2)
        self.assertNotIn("missing.pdf", buf.getvalue())
        self.assertNotIn("Traceback", buf.getvalue())
