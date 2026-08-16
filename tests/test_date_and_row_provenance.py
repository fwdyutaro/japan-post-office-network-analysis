"""Change-PDF date fields, and per-row source provenance in the current list."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from postal_bias.change_pdf import (ChangePdfError, iso_date, parse_change_text,
                                    write_change_outputs)
from postal_bias.current_list import (CurrentListError, parse_layout_text, source_document_id,
                                      write_outputs)


def state(side, section, ident, name, address, services="○ ○ ○ ○ ○", simple="○"):
    return f"{side} {section} {ident} {name} {simple} {address} - {services} -"


def sample(change_date="R8.6.27"):
    return "\n".join([
        state("変更前", "郵便局", "010160", "旧局", "東京都港区１－１"),
        f"{change_date} 移転",
        state("変更後", "郵便局", "010160", "旧局", "東京都港区２－２"),
    ])


class IsoDateTests(unittest.TestCase):
    def test_accepts_only_a_calendar_day(self):
        self.assertEqual(iso_date("2026-07-30"), "2026-07-30")
        for bad in ("not-a-date", "", None, "2026-07", "20260730", "2026-07-30T00:00",
                    "2026-13-01", "2026-02-30", " 2026-7-30", "2026/07/30"):
            self.assertIsNone(iso_date(bad), bad)


class ChangeCoverageDateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "notice.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\nnotice\n")
        self.digest = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def _metadata(self):
        return json.loads((self.out / "metadata.json").read_text(encoding="utf-8"))

    def test_non_iso_coverage_date_stops_the_write(self):
        result = parse_change_text(sample(), source_pdf_sha256=self.digest,
                                   source_document_date="2026-06-27")
        for bad in ("not-a-date", "2026-06", "20260627", "2026-06-31"):
            with self.assertRaises(ChangePdfError, msg=bad):
                write_change_outputs(result, self.pdf, bad, self.out,
                                     acknowledge_internal_use=True)
            self.assertFalse(self.out.exists())

    def test_non_iso_published_date_stops_the_write(self):
        result = parse_change_text(sample(), source_pdf_sha256=self.digest)
        with self.assertRaises(ChangePdfError):
            write_change_outputs(result, self.pdf, "2026-06-27", self.out,
                                 acknowledge_internal_use=True,
                                 document_published_date="not-a-date")
        self.assertFalse(self.out.exists())

    def test_the_three_dates_are_stored_apart(self):
        result = parse_change_text(sample(), source_pdf_sha256=self.digest,
                                   source_document_date="2026-06-27")
        write_change_outputs(result, self.pdf, "2026-06-27", self.out,
                             acknowledge_internal_use=True)
        meta = self._metadata()
        # Document-name date, publication date and the announced effective date
        # are separate facts; here the change takes effect after the notice.
        self.assertEqual(meta["coverage_date"], "2026-06-27")
        self.assertEqual(meta["coverage_date_basis"], "document_name_date")
        self.assertEqual(meta["document_published_date"], "2026-06-27")
        self.assertEqual(meta["change_effective_date_from"], "2026-06-27")
        self.assertEqual(meta["change_effective_date_to"], "2026-06-27")
        self.assertEqual(meta["change_effective_date_unknown_count"], 0)

    def test_effective_span_differs_from_the_document_date(self):
        text = "\n".join([sample("R8.9.1"),
                          state("変更前", "郵便局", "010170", "乙局", "東京都港区３－３"),
                          "R8.12.1 移転",
                          state("変更後", "郵便局", "010170", "乙局", "東京都港区４－４")])
        result = parse_change_text(text, source_pdf_sha256=self.digest,
                                   source_document_date="2026-06-27")
        write_change_outputs(result, self.pdf, "2026-06-27", self.out,
                             acknowledge_internal_use=True,
                             document_published_date="2026-06-27")
        meta = self._metadata()
        self.assertEqual(meta["coverage_date"], "2026-06-27")
        self.assertEqual(meta["change_effective_date_from"], "2026-09-01")
        self.assertEqual(meta["change_effective_date_to"], "2026-12-01")

    def test_published_date_defaults_to_the_parsed_document_date(self):
        result = parse_change_text(sample(), source_pdf_sha256=self.digest,
                                   source_document_date="2026-06-20")
        write_change_outputs(result, self.pdf, "2026-06-27", self.out,
                             acknowledge_internal_use=True)
        meta = self._metadata()
        self.assertEqual(meta["document_published_date"], "2026-06-20")
        self.assertEqual(meta["coverage_date"], "2026-06-27")

    def test_events_disagreeing_on_the_document_date_are_a_qa_error(self):
        result = parse_change_text(sample(), source_pdf_sha256=self.digest,
                                   source_document_date="2026-06-27")
        second = parse_change_text(sample("R8.7.1"), source_pdf_sha256=self.digest,
                                   source_document_date="2026-07-30")
        result["events"].extend(second["events"])
        write_change_outputs(result, self.pdf, "2026-06-27", self.out,
                             acknowledge_internal_use=True,
                             document_published_date="2026-06-27")
        qa = [json.loads(x) for x in
              (self.out / "qa.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        codes = {row.get("code") for row in qa}
        self.assertIn("source_document_date_conflict", codes)
        self.assertGreaterEqual(self._metadata()["qa_error_count"], 1)


def list_row(serial, ident, name, address, *, services=("○", "×", "-", "", "○"), status="営業中"):
    marks = " ".join(services)
    return f"{serial:>4} {ident:<7} {name:<14}{'':>2}  {address:<21}  {'':>1} {marks:<35}{'':<20}{status}"


class CurrentListRowProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pdf = Path(self.tmp.name) / "list.pdf"
        self.pdf.write_bytes(b"%PDF-1.7\ncurrent list\n")
        self.digest = hashlib.sha256(self.pdf.read_bytes()).hexdigest()
        self.document_id = source_document_id(self.digest)
        self.out = Path(self.tmp.name) / "out"

    def tearDown(self):
        self.tmp.cleanup()

    def _parsed(self, source_pdf_sha256=""):
        text = ("別記様式第一号（郵便局）\n通番 整理番号 名称 所在地\n"
                + list_row(1, "000010", "甲郵便局", "東京都港区１－１") + "\n"
                + list_row(2, "000020", "乙郵便局", "東京都港区２－２") + "\n")
        return parse_layout_text(text, source_pdf_sha256=source_pdf_sha256)

    def _rows(self):
        return [json.loads(x) for x in
                (self.out / "records.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]

    def _qa(self):
        return [json.loads(x) for x in
                (self.out / "qa.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]

    def test_rows_without_a_source_id_take_the_hashed_pdf(self):
        result = self._parsed()
        self.assertTrue(result["records"])
        write_outputs(result, self.pdf, "2026-06-30", self.out, acknowledge_internal_use=True)
        self.assertEqual({r["source_document_id"] for r in self._rows()}, {self.document_id})
        meta = json.loads((self.out / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["source_document_id"], self.document_id)
        self.assertEqual(meta["source_document_id_reassigned_count"], 0)

    def test_a_stale_row_id_is_reconciled_and_recorded_not_left_in_place(self):
        result = self._parsed()
        stale = source_document_id("a" * 64)
        for row in result["records"]:
            row["source_document_id"] = stale
        write_outputs(result, self.pdf, "2026-06-30", self.out, acknowledge_internal_use=True)
        rows = self._rows()
        self.assertEqual({r["source_document_id"] for r in rows}, {self.document_id})
        meta = json.loads((self.out / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["source_document_id_reassigned_count"], len(rows))
        reassigned = [q for q in self._qa() if q.get("code") == "source_document_id_reassigned"]
        self.assertEqual(len(reassigned), len(rows))
        self.assertEqual(reassigned[0]["previous_source_document_id"], stale)
        self.assertEqual(reassigned[0]["source_document_id"], self.document_id)
        self.assertEqual(meta["qa_error_count"],
                         sum(1 for q in self._qa() if q.get("kind") == "error"))
        self.assertGreaterEqual(meta["qa_error_count"], len(rows))

    def test_a_parse_from_another_pdf_stops_the_write(self):
        result = self._parsed(source_pdf_sha256="b" * 64)
        with self.assertRaises(CurrentListError):
            write_outputs(result, self.pdf, "2026-06-30", self.out, acknowledge_internal_use=True)
        self.assertFalse(self.out.exists())

    def test_a_parse_from_this_pdf_is_accepted_without_reassignment(self):
        result = self._parsed(source_pdf_sha256=self.digest)
        write_outputs(result, self.pdf, "2026-06-30", self.out, acknowledge_internal_use=True)
        meta = json.loads((self.out / "metadata.json").read_text(encoding="utf-8"))
        self.assertEqual(meta["source_document_id_reassigned_count"], 0)
        self.assertEqual({r["source_document_id"] for r in self._rows()}, {self.document_id})


if __name__ == "__main__":
    unittest.main()
