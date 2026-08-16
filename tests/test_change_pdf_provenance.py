"""Source-PDF digest verification, document-id unification and bad dates."""
from __future__ import annotations

import hashlib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from postal_bias.change_pdf import ChangePdfError, parse_change_text, write_change_outputs
from postal_bias.current_list import source_document_id


def state(side, section, ident, name, address, services="○ ○ ○ ○ ○", simple="○"):
    return f"{side} {section} {ident} {name} {simple} {address} - {services} -"


def sample(change_date="R8.6.27"):
    return "\n".join([
        state("変更前", "郵便局", "010160", "旧局", "東京都港区１－１"),
        f"{change_date} 移転",
        state("変更後", "郵便局", "010160", "旧局", "東京都港区２－２"),
    ])


class DigestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "notice.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nnotice\n")
        self.digest = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def test_declared_digest_that_does_not_match_the_file_stops_the_write(self):
        result = parse_change_text(sample(), source_pdf_sha256=self.digest)
        with self.assertRaises(ChangePdfError):
            write_change_outputs(result, self.pdf, "2026-06-27", self.out,
                                 acknowledge_internal_use=True, source_pdf_sha256="f" * 64)
        self.assertFalse(self.out.exists())

    def test_events_parsed_from_a_different_pdf_are_refused(self):
        result = parse_change_text(sample(), source_pdf_sha256="a" * 64)
        with self.assertRaises(ChangePdfError):
            write_change_outputs(result, self.pdf, "2026-06-27", self.out,
                                 acknowledge_internal_use=True)

    def test_matching_digest_is_accepted_and_recorded_as_verified(self):
        import json
        result = parse_change_text(sample(), source_pdf_sha256=self.digest)
        paths = write_change_outputs(result, self.pdf, "2026-06-27", self.out,
                                     acknowledge_internal_use=True,
                                     source_pdf_sha256=self.digest.upper())
        meta = json.loads(Path(paths["metadata"]).read_text(encoding="utf-8"))
        self.assertEqual(meta["source_pdf_sha256"], self.digest)
        self.assertTrue(meta["source_pdf_sha256_verified"])

    def test_divergent_event_document_ids_are_unified_and_reported(self):
        import json
        result = parse_change_text(sample(), source_pdf_sha256=self.digest)
        self.assertTrue(result["events"])
        result["events"][0]["source_document_id"] = "src-somewhere-else"
        result["events"][0]["source_pdf_sha256"] = "b" * 64
        paths = write_change_outputs(result, self.pdf, "2026-06-27", self.out,
                                     acknowledge_internal_use=True)
        events = [json.loads(x) for x in
                  Path(paths["events"]).read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual({e["source_document_id"] for e in events},
                         {source_document_id(self.digest)})
        self.assertEqual({e["source_pdf_sha256"] for e in events}, {self.digest})
        qa = [json.loads(x) for x in
              Path(paths["qa"]).read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertIn("source_document_id_reassigned", {q["code"] for q in qa})
        meta = json.loads(Path(paths["metadata"]).read_text(encoding="utf-8"))
        self.assertEqual(meta["source_document_id_reassigned_count"], 1)


class InvalidChangeDateTests(unittest.TestCase):
    def test_unparseable_change_date_is_a_qa_error(self):
        result = parse_change_text(sample("R8.13.99"))
        self.assertIn("invalid_change_date", {q["code"] for q in result["qa"]})
        self.assertEqual(result["metadata"]["invalid_change_date_count"], 1)
        self.assertEqual(result["metadata"]["qa_error_count"], 1)
        self.assertFalse(result["events"][0]["planned_effective_date_valid"])
        self.assertIsNone(result["events"][0]["planned_effective_date"])

    def test_valid_change_date_raises_nothing(self):
        result = parse_change_text(sample("R8.6.27"))
        self.assertEqual(result["metadata"]["invalid_change_date_count"], 0)
        self.assertEqual(result["events"][0]["planned_effective_date"], "2026-06-27")
        self.assertTrue(result["events"][0]["planned_effective_date_valid"])


if __name__ == "__main__":
    unittest.main()
