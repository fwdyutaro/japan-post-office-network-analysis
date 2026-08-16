import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from postal_bias.change_pdf import normalize_era_date, parse_change_text, write_change_outputs, ChangePdfError
from postal_bias.cli import main


def state(side, section, ident, name, address, services="○ ○ ○ ○ ○", simple="○"):
    return f"{side} {section} {ident} {name} {simple} {address} - {services} -"


class ChangePdfTests(unittest.TestCase):
    def test_era_dates(self):
        self.assertEqual(normalize_era_date("H24.10.19"), "2012-10-19")
        self.assertEqual(normalize_era_date("R8.6.27"), "2026-06-27")

    def test_creation_deletion_and_blank_semantics(self):
        text = "\n".join([
            state("変更前", "営業所", "-", "-", "-", "- - - - -", "-"),
            "R8.6.27 契約締結",
            state("変更後", "営業所", "010160a", "新簡易局", "東京都港区１－１", "○ ○ × ○ ○"),
            state("変更前", "営業所", "010160a", "旧簡易局", "東京都港区１－１", "○ ○ × ○ ○"),
            "R8.7.1 契約解除",
            state("変更後", "-", "-", "-", "-", "- - - - -", "-"),
        ])
        result = parse_change_text(text, source_pdf_sha256="abc")
        self.assertEqual(len(result["events"]), 2)
        self.assertEqual(result["events"][0]["event_type"], "contract_concluded")
        self.assertIsNone(result["events"][0]["confirmed_effective_date"])
        self.assertEqual(result["events"][0]["after_state"]["official_identifier"]["raw"], "010160a")
        self.assertEqual(result["events"][1]["event_type"], "contract_terminated")
        self.assertEqual(result["events"][1]["after_state"]["official_identifier"]["normalized"], "deleted")

    def test_unknown_reason_is_qa_error(self):
        text = "\n".join([state("変更前", "郵便局", "000010", "局", "東京都港区１－１", "○ ○ ○ ○ ○", "-"),
                            "R8.6.27 未知変更", state("変更後", "郵便局", "000010", "局", "東京都港区１－２", "○ ○ ○ ○ ○", "-")])
        result = parse_change_text(text)
        self.assertEqual(result["events"][0]["event_type"], "unknown")
        self.assertTrue(any(x.get("code") == "unknown_reason" for x in result["qa"]))

    def test_real_samples(self):
        # Optional integration corpus is intentionally external to this public tree.
        exe = Path("pdftotext")
        samples = [("20121228", 26), ("20260730", 27)]
        root = Path(tempfile.gettempdir()) / "postal_bias_private_samples"
        if not exe.exists() or not all((root / f"{name}.pdf").exists() for name, _ in samples):
            self.skipTest("local integration sample unavailable")
        from postal_bias.current_list import run_pdftotext
        for name, expected in samples:
            text, _ = run_pdftotext(root / f"{name}.pdf", exe)
            result = parse_change_text(text, source_pdf_sha256=hashlib.sha256((root / f"{name}.pdf").read_bytes()).hexdigest())
            self.assertEqual(result["metadata"]["event_count"], expected)
            self.assertEqual(result["metadata"]["qa_error_count"], 0)
            self.assertEqual(len({x["event_id"] for x in result["events"]}), expected)
            # Corpus-wide fail-closed checks for wrapped simple-office rows and
            # page chrome.  A simple/disadvantaged marker belongs in its own
            # column, never in an address; deletion sentinels remain hyphen-only.
            for event in result["events"]:
                for side in ("before_state", "after_state"):
                    current = event[side]
                    address = current["address"]["raw"]
                    self.assertNotIn("○", address)
                    self.assertNotIn("×", address)
                    serialized = json.dumps(current, ensure_ascii=False)
                    self.assertNotIn("当該郵便局又は当該営業所", serialized)
                    self.assertNotIn("関連銀行又は関連保険会", serialized)
                    if current["official_identifier"]["raw"] == "-":
                        for key, value in current.items():
                            if key == "services":
                                self.assertTrue(all(x["raw"] in ("", "-") for x in value))
                            elif key != "section" and isinstance(value, dict) and "raw" in value:
                                self.assertIn(value["raw"], ("", "-"))
            if name == "20260730":
                selected = [x for x in result["events"] if x["before_state"]["official_identifier"]["raw"] == "247090"]
                self.assertEqual(len(selected), 1)
                for event in selected:
                    self.assertEqual(event["before_state"]["simple_post_office"]["normalized"], "yes")
                    self.assertEqual(event["after_state"]["simple_post_office"]["normalized"], "yes")
                    self.assertEqual(event["before_state"]["address"]["raw"], "岐阜県高山市奥飛騨温泉郷平湯湯の平７６３－１９６")
                    self.assertEqual(event["after_state"]["address"]["raw"], "岐阜県高山市奥飛騨温泉郷平湯湯の平７６３－１９６")
                    self.assertNotIn("○", event["before_state"]["address"]["raw"] + event["after_state"]["address"]["raw"])

    def test_output_ack_and_atomic_no_overwrite(self):
        text = "\n".join([state("変更前", "営業所", "-", "-", "-", "- - - - -", "-"),
                            "R8.6.27 契約締結", state("変更後", "営業所", "010160a", "新簡易局", "東京都港区１－１")])
        result = parse_change_text(text)
        pdf = Path(tempfile.mktemp(suffix=".pdf")); pdf.write_bytes(b"%PDF-1.7\n")
        try:
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ChangePdfError): write_change_outputs(result, pdf, "2026-07-30", d)
                paths = write_change_outputs(result, pdf, "2026-07-30", d, acknowledge_internal_use=True)
                self.assertTrue(Path(paths["events"]).exists())
                with self.assertRaises(ChangePdfError): write_change_outputs(result, pdf, "2026-07-30", d, acknowledge_internal_use=True)
        finally:
            pdf.unlink()

    def test_parse_change_cli_hides_missing_and_pdftotext_failures(self):
        for pdf_arg, exe_arg, create_pdf in [
            (r"C:\private\missing-change.pdf", None, False),
            (None, r"C:\private\missing-pdftotext.exe", True),
        ]:
            pdf = Path(tempfile.mktemp(suffix=".pdf")) if create_pdf else None
            if pdf is not None:
                pdf.write_bytes(b"%PDF-1.7\n")
                pdf_arg = str(pdf)
            err = io.StringIO()
            try:
                with redirect_stderr(err):
                    code = main(["parse-change", pdf_arg, "--coverage-date", "2026-07-30",
                                 "--output-dir", r"C:\private\out", "--pdftotext", exe_arg or ""])
            finally:
                if pdf is not None:
                    pdf.unlink()
            self.assertEqual(code, 2)
            self.assertNotIn("missing-change.pdf", err.getvalue())
            self.assertNotIn("missing-pdftotext.exe", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue())
