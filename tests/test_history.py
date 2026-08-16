import unittest
import io
from contextlib import redirect_stderr

from postal_bias.history import HistoryError, build_history, monthly_snapshots, replay_history, validate_event_chains, validate_snapshot_cutoff
from postal_bias.cli import main


def _field(value):
    return {"raw": value, "normalized": value}


def _record(identifier="000001"):
    return {"section": "postal_office", "official_identifier": identifier,
            "name": _field("中央郵便局"), "address": _field("東京都千代田区１－１"),
            "operating_status": {"raw": "営業中", "normalized": "operating"}}


def _event(eid, kind, date_value, *, provenance="official", status="confirmed", before="000001", after="000001"):
    def state(identifier):
        return {"section": _field("postal_office"), "official_identifier": _field(identifier),
                "name": _field("中央郵便局"), "address": _field("東京都千代田区１－１")}
    return {"event_id": eid, "event_type": kind, "event_status": status,
            "confirmed_effective_date": date_value, "provenance": provenance,
            "before_state": state(before), "after_state": state(after),
            "source_document_id": "src-events"}


class HistoryTests(unittest.TestCase):
    def test_unconfirmed_and_unofficial_structural_are_excluded_or_qa(self):
        events = [_event("ann", "relocation", "2026-02-01", status="announced"),
                  _event("bad", "relocation", "2026-02-01", provenance="unofficial")]
        h = build_history({"records": [_record()], "metadata": {"coverage_date": "2026-01-01"}}, events)
        self.assertTrue(any(x["code"] == "unofficial_structural_event" for x in h["qa"]))
        self.assertEqual(replay_history(h, "2026-03-01")[0]["address"], "東京都千代田区１－１")

    def test_official_closure_and_reopen_change_availability_only(self):
        events = [_event("close", "temporarily_closed", "2026-02-01"),
                  _event("open", "reopened", "2026-03-01")]
        h = build_history({"records": [_record()], "metadata": {"coverage_date": "2026-01-01"}}, events)
        self.assertFalse(replay_history(h, "2026-02-15", series="effective_official")[0]["available"])
        self.assertTrue(replay_history(h, "2026-04-01", series="effective_official")[0]["available"])
        self.assertTrue(replay_history(h, "2026-02-15", series="formal")[0]["available"])
        self.assertTrue(replay_history(h, "2026-02-15", series="effective_official")[0]["exists"])

    def test_revision_and_cancellation_chain(self):
        original = _event("old", "relocation", "2026-02-01")
        revised = _event("new", "relocation", "2026-03-01")
        revised["revision_of_event_id"] = "old"
        h = build_history({"records": [_record()], "metadata": {"coverage_date": "2026-01-01"}}, [original, revised])
        self.assertEqual(replay_history(h, "2026-02-15")[0]["applied_event_ids"], [])
        self.assertEqual(replay_history(h, "2026-04-01")[0]["applied_event_ids"], ["new"])

    def test_disabled_source_excludes_event(self):
        event = _event("disabled", "relocation", "2026-02-01")
        event["source_document_id"] = "src-disabled"
        h = build_history({"records": [_record()], "metadata": {"coverage_date": "2026-01-01"}}, [event], disabled_sources=["src-disabled"])
        self.assertEqual(replay_history(h, "2026-03-01")[0]["applied_event_ids"], [])

    def test_disabled_source_family_excludes_unofficial_closure(self):
        event = _event("src06", "temporarily_closed", "2026-02-01", provenance="unofficial", status="announced")
        event["planned_effective_date"] = "2026-02-01"; event["source_family"] = "SRC-06"
        h = build_history({"records": [_record()], "metadata": {"coverage_date": "2026-01-31"}}, [event], disabled_source_families=["SRC-06"])
        self.assertTrue(replay_history(h, "2026-02-28", series="effective_with_unofficial")[0]["available"])

    def test_type_change_keeps_entity_and_series_are_deterministic(self):
        event = _event("type", "office_type_change", "2026-02-01", before="000001", after="000002")
        h = build_history({"records": [_record()], "metadata": {"coverage_date": "2026-01-01"}}, [event])
        eid = h["entities"][0]["facility_entity_id"]
        self.assertEqual(replay_history(h, "2026-04-01")[0]["facility_entity_id"], eid)
        self.assertEqual(monthly_snapshots(h, ["2026-04"]), monthly_snapshots(h, ["2026-04"]))

    def test_invalid_series_and_reference(self):
        self.assertTrue(validate_event_chains([{"event_id": "x", "revision_of_event_id": "missing"}]))
        h = build_history({"records": [_record()], "metadata": {"coverage_date": "2026-01-01"}}, [])
        with self.assertRaises(HistoryError): replay_history(h, "2026-01-01", series="bad")

    def test_build_history_cli_sanitizes_missing_input(self):
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["build-history", "--anchor", r"C:\private\anchor.jsonl", "--events", r"C:\private\events.jsonl",
                         "--coverage-date", "2026-01-01", "--output-dir", r"C:\private\out",
                         "--acknowledge-internal-use"])
        self.assertEqual(code, 2)
        self.assertNotIn("C:\\private", err.getvalue())
        self.assertNotIn("Traceback", err.getvalue())

    def test_month_end_and_unofficial_series_semantics(self):
        self.assertEqual(validate_snapshot_cutoff("2024-02"), "2024-02-29")
        with self.assertRaises(HistoryError): validate_snapshot_cutoff("2024-02-28")
        unofficial = _event("uclose", "temporarily_closed", "2026-02-01", provenance="unofficial", status="announced")
        unofficial["planned_effective_date"] = "2026-02-01"
        h = build_history({"records": [_record()], "metadata": {"coverage_date": "2026-01-31"}}, [unofficial])
        self.assertTrue(replay_history(h, "2026-02-28", series="formal")[0]["available"])
        self.assertTrue(replay_history(h, "2026-02-28", series="effective_official")[0]["available"])
        self.assertFalse(replay_history(h, "2026-02-28", series="effective_with_unofficial")[0]["available"])
        self.assertEqual(monthly_snapshots(h, ["2024-02"], series="effective_with_unofficial")[0]["snapshot_cutoff"], "2024-02-29")
        self.assertEqual(monthly_snapshots(h, ["2026-02"], series="effective_with_unofficial")[0]["provenance"], "mixed")
