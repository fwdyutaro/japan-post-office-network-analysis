"""Anchor preconditions and the state / effective-date separation."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from postal_bias.artifacts import seal_artifact
from postal_bias.confirm import (ConfirmError, confirm_events, disadvantaged_area_history,
                                 load_sealed_anchor, validate_anchor)


def cell(value):
    return {"raw": value, "normalized": value}


def state(identifier, name, address, section="郵便局"):
    return {"section": {"raw": section, "normalized": "postal_office"},
            "official_identifier": cell(identifier), "name": cell(name),
            "address": cell(address), "simple_post_office": cell(""),
            "disadvantaged_area": cell(""), "services": [cell("○")] * 5}


def event(event_id, date, before, after, **kw):
    row = {"event_id": event_id, "planned_effective_date": date, "before_state": before,
           "after_state": after, "event_type": "relocation", "event_status": "announced",
           "confirmed_effective_date": None, "provenance": "official",
           "source_document_date": date, "source_page": 1, "source_line": 1}
    row.update(kw)
    return row


def anchor_record(identifier, name, address, section="postal_office"):
    return {"section": section, "official_identifier": identifier, "name": name,
            "address": address, "simple_post_office": "", "disadvantaged_area": "",
            "services": ["○"] * 5, "source_page": 1, "source_line": 1,
            "source_line_sha256": "0" * 64, "operating_status": "営業中"}


def anchor(records, *, total=None, qa_errors=0):
    return {"records": records,
            "metadata": {"coverage_date": "2026-06-30",
                         "source_document_id": "src-test",
                         "counts": {"total": len(records) if total is None else total},
                         "qa_error_count": qa_errors}}


def run(events, records, **kw):
    kw.setdefault("allow_unsealed_anchor", True)
    return confirm_events(events, anchor(records), **kw)


class AnchorPreconditionTests(unittest.TestCase):
    def test_duplicate_identifier_stops_the_run(self):
        records = [anchor_record("000100", "甲郵便局", "東京都港区１"),
                   anchor_record("000100", "乙郵便局", "東京都港区２")]
        with self.assertRaises(ConfirmError) as ctx:
            confirm_events([], anchor(records), allow_unsealed_anchor=True)
        codes = {d["code"] for d in ctx.exception.details}
        self.assertIn("anchor_identifier_duplicated", codes)

    def test_every_duplicate_is_listed_not_just_the_first(self):
        records = [anchor_record(i, f"{i}局", "東京都港区")
                   for i in ("000100", "000100", "000200", "000200", "000300")]
        with self.assertRaises(ConfirmError) as ctx:
            confirm_events([], anchor(records), allow_unsealed_anchor=True)
        detail = next(d for d in ctx.exception.details if d["code"] == "anchor_identifier_duplicated")
        self.assertEqual(detail["duplicate_identifier_count"], 2)
        self.assertEqual({d["official_identifier"] for d in detail["duplicates"]},
                         {"000100", "000200"})

    def test_blank_identifier_stops_the_run(self):
        records = [anchor_record("", "甲郵便局", "東京都港区１")]
        with self.assertRaises(ConfirmError) as ctx:
            confirm_events([], anchor(records), allow_unsealed_anchor=True)
        self.assertIn("anchor_identifier_blank", {d["code"] for d in ctx.exception.details})

    def test_record_count_must_match_metadata(self):
        records = [anchor_record("000100", "甲郵便局", "東京都港区１")]
        with self.assertRaises(ConfirmError) as ctx:
            confirm_events([], anchor(records, total=23459), allow_unsealed_anchor=True)
        self.assertIn("anchor_record_count_mismatch", {d["code"] for d in ctx.exception.details})

    def test_anchor_qa_errors_stop_the_run(self):
        records = [anchor_record("000100", "甲郵便局", "東京都港区１")]
        with self.assertRaises(ConfirmError) as ctx:
            confirm_events([], anchor(records, qa_errors=3), allow_unsealed_anchor=True)
        self.assertIn("anchor_qa_error_count_nonzero", {d["code"] for d in ctx.exception.details})

    def test_unsealed_anchor_is_rejected_by_default(self):
        records = [anchor_record("000100", "甲郵便局", "東京都港区１")]
        with self.assertRaises(ConfirmError) as ctx:
            confirm_events([], anchor(records))
        self.assertIn("anchor_artifact_not_supplied", {d["code"] for d in ctx.exception.details})

    def test_sealed_anchor_passes_and_tampered_anchor_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "anchor"
            d.mkdir()
            records = [anchor_record("000100", "甲郵便局", "東京都港区１")]
            meta = anchor(records)["metadata"]
            d.joinpath("records.jsonl").write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
            d.joinpath("metadata.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            seal_artifact(d, artifact_type="current_list", source_authority="official",
                          evidence_status="observed", release_classification="internal_only",
                          acknowledge_internal_use=True)
            summary = validate_anchor(records, meta, anchor_dir=d)
            self.assertEqual(summary["anchor_artifact_state"], "verified")
            d.joinpath("records.jsonl").write_text("{}\n", encoding="utf-8")
            with self.assertRaises(ConfirmError) as ctx:
                validate_anchor(records, meta, anchor_dir=d)
            self.assertIn("anchor_artifact_mismatch", {x["code"] for x in ctx.exception.details})


class SealedAnchorBindingTests(unittest.TestCase):
    """The sealed bundle must describe the rows actually corroborated against."""

    def _seal(self, directory, records, meta):
        directory.mkdir(parents=True, exist_ok=True)
        directory.joinpath("records.jsonl").write_text(
            "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records), encoding="utf-8")
        directory.joinpath("metadata.json").write_text(
            json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        seal_artifact(directory, artifact_type="current_list", source_authority="official",
                      evidence_status="observed", release_classification="internal_only",
                      replace=True, acknowledge_internal_use=True)

    def test_records_other_than_the_sealed_ones_stop_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "anchor"
            sealed_records = [anchor_record("000100", "甲郵便局", "東京都港区１")]
            meta = anchor(sealed_records)["metadata"]
            self._seal(d, sealed_records, meta)
            # A different identifier entirely: the bundle verifies, the rows do not.
            other = [anchor_record("000999", "乙郵便局", "東京都港区９")]
            with self.assertRaises(ConfirmError) as ctx:
                validate_anchor(other, meta, anchor_dir=d)
            detail = next(x for x in ctx.exception.details
                          if x["code"] == "anchor_input_not_from_sealed_artifact")
            payloads = {m["payload"] for m in detail["mismatched"]}
            self.assertEqual(payloads, {"records.jsonl"})
            self.assertNotEqual(detail["mismatched"][0]["sealed_sha256"],
                                detail["mismatched"][0]["supplied_sha256"])

    def test_extra_row_appended_to_the_supplied_records_stops_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "anchor"
            sealed_records = [anchor_record("000100", "甲郵便局", "東京都港区１")]
            self._seal(d, sealed_records, anchor(sealed_records)["metadata"])
            supplied = sealed_records + [anchor_record("000200", "乙郵便局", "東京都港区２")]
            meta = anchor(supplied)["metadata"]
            with self.assertRaises(ConfirmError) as ctx:
                validate_anchor(supplied, meta, anchor_dir=d)
            codes = {x["code"] for x in ctx.exception.details}
            self.assertIn("anchor_input_not_from_sealed_artifact", codes)

    def test_metadata_other_than_the_sealed_one_stops_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "anchor"
            records = [anchor_record("000100", "甲郵便局", "東京都港区１")]
            meta = anchor(records)["metadata"]
            self._seal(d, records, meta)
            forged = {**meta, "source_document_id": "src-someone-elses-pdf"}
            with self.assertRaises(ConfirmError) as ctx:
                validate_anchor(records, forged, anchor_dir=d)
            detail = next(x for x in ctx.exception.details
                          if x["code"] == "anchor_input_not_from_sealed_artifact")
            self.assertEqual({m["payload"] for m in detail["mismatched"]}, {"metadata.json"})

    def test_confirm_events_refuses_events_against_an_unbound_anchor(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "anchor"
            sealed_records = [anchor_record("000100", "甲郵便局", "東京都港区２")]
            self._seal(d, sealed_records, anchor(sealed_records)["metadata"])
            swapped = [anchor_record("000100", "甲郵便局", "東京都港区９")]
            before = state("000100", "甲郵便局", "東京都港区１")
            after = state("000100", "甲郵便局", "東京都港区９")
            with self.assertRaises(ConfirmError) as ctx:
                confirm_events([event("e1", "2020-05-01", before, after)],
                               anchor(swapped), anchor_dir=d)
            self.assertIn("anchor_input_not_from_sealed_artifact",
                          {x["code"] for x in ctx.exception.details})

    def test_records_loaded_from_the_seal_are_bound_and_corroborate(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "anchor"
            records = [anchor_record("000100", "甲郵便局", "東京都港区２")]
            self._seal(d, records, anchor(records)["metadata"])
            loaded = load_sealed_anchor(d)
            summary = validate_anchor(loaded["records"], loaded["metadata"], anchor_dir=d)
            self.assertEqual(summary["anchor_payload_binding"], "bound_to_sealed_payload")
            self.assertEqual(summary["anchor_artifact_state"], "verified")
            before = state("000100", "甲郵便局", "東京都港区１")
            after = state("000100", "甲郵便局", "東京都港区２")
            out = confirm_events([event("e1", "2020-05-01", before, after)], loaded, anchor_dir=d)
            self.assertTrue(out["events"][0]["state_corroborated"])
            self.assertEqual(out["metadata"]["anchor_payload_binding"], "bound_to_sealed_payload")


class StateVersusDateTests(unittest.TestCase):
    def test_single_event_corroborates_state_but_not_date(self):
        before = state("000100", "甲郵便局", "東京都港区１")
        after = state("000100", "甲郵便局", "東京都港区２")
        records = [anchor_record("000100", "甲郵便局", "東京都港区２")]
        out = run([event("e1", "2020-05-01", before, after)], records)
        row = out["events"][0]
        self.assertTrue(row["state_corroborated"])
        self.assertFalse(row["effective_date_confirmed"])
        self.assertEqual(row["date_basis"], "planned")
        self.assertIsNone(row["confirmed_effective_date"])

    def test_round_trip_chain_leaves_the_date_unconfirmed(self):
        a = state("000100", "甲郵便局", "東京都港区１")
        b = state("000100", "甲郵便局", "東京都港区２")
        records = [anchor_record("000100", "甲郵便局", "東京都港区１")]
        out = run([event("e1", "2020-05-01", a, b), event("e2", "2021-05-01", b, a)], records)
        self.assertEqual({e["event_id"] for e in out["events"]}, {"e1", "e2"})
        for row in out["events"]:
            # The terminal state equals the anchor whatever the real dates were.
            self.assertTrue(row["state_corroborated"], row["event_id"])
            self.assertFalse(row["effective_date_confirmed"], row["event_id"])
            self.assertEqual(row["date_basis"], "planned")
            self.assertIsNone(row["confirmed_effective_date"])
        self.assertEqual(out["metadata"]["effective_date_confirmed_event_count"], 0)
        self.assertEqual(out["metadata"]["date_basis_counts"]["planned"], 2)

    def test_matching_later_observation_confirms_the_date(self):
        before = state("000100", "甲郵便局", "東京都港区１")
        after = state("000100", "甲郵便局", "東京都港区２")
        records = [anchor_record("000100", "甲郵便局", "東京都港区２")]
        out = run([event("e1", "2020-05-01", before, after)], records,
                  date_observations=[{"event_id": "e1", "observed_effective_date": "2020-05-01",
                                      "observation_kind": "current_list_snapshot",
                                      "source_document_id": "src-later"}])
        row = out["events"][0]
        self.assertTrue(row["effective_date_confirmed"])
        self.assertEqual(row["date_basis"], "observed")
        self.assertEqual(row["confirmed_effective_date"], "2020-05-01")
        self.assertEqual(out["metadata"]["effective_date_confirmed_event_count"], 1)

    def test_disagreeing_observation_does_not_confirm_and_raises_qa(self):
        before = state("000100", "甲郵便局", "東京都港区１")
        after = state("000100", "甲郵便局", "東京都港区２")
        records = [anchor_record("000100", "甲郵便局", "東京都港区２")]
        out = run([event("e1", "2020-05-01", before, after)], records,
                  date_observations=[{"event_id": "e1", "observed_effective_date": "2020-07-01",
                                      "observation_kind": "official_notice"}])
        row = out["events"][0]
        self.assertFalse(row["effective_date_confirmed"])
        self.assertEqual(row["date_basis"], "unknown")
        self.assertIsNone(row["confirmed_effective_date"])
        self.assertIn("date_observation_conflict", {q["code"] for q in out["qa"]})

    def test_observation_of_an_unknown_kind_is_rejected(self):
        before = state("000100", "甲郵便局", "東京都港区１")
        after = state("000100", "甲郵便局", "東京都港区２")
        records = [anchor_record("000100", "甲郵便局", "東京都港区２")]
        out = run([event("e1", "2020-05-01", before, after)], records,
                  date_observations=[{"event_id": "e1", "observed_effective_date": "2020-05-01",
                                      "observation_kind": "blog_post"}])
        self.assertFalse(out["events"][0]["effective_date_confirmed"])
        self.assertIn("date_observation_kind_rejected", {q["code"] for q in out["qa"]})

    def test_uncorroborated_chain_reports_unknown_basis(self):
        before = state("000100", "甲郵便局", "東京都港区１")
        after = state("000100", "甲郵便局", "東京都港区２")
        records = [anchor_record("000100", "甲郵便局", "東京都港区９")]
        out = run([event("e1", "2020-05-01", before, after)], records)
        row = out["events"][0]
        self.assertFalse(row["state_corroborated"])
        self.assertEqual(row["date_basis"], "unknown")
        self.assertEqual(row["event_status"], "announced")

    def test_disadvantaged_area_history_carries_the_basis(self):
        before = state("000100", "甲郵便局", "東京都港区１")
        after = state("000100", "甲郵便局", "東京都港区１")
        records = [anchor_record("000100", "甲郵便局", "東京都港区１")]
        out = run([event("e1", "2020-05-01", before, after,
                         event_type="disadvantaged_area_change")], records)
        rows = disadvantaged_area_history(out["events"])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["state_corroborated"])
        self.assertEqual(rows[0]["date_basis"], "planned")


class InvalidDateTests(unittest.TestCase):
    def test_unparseable_planned_date_is_a_qa_error_not_a_future_event(self):
        before = state("000100", "甲郵便局", "東京都港区１")
        after = state("000100", "甲郵便局", "東京都港区２")
        records = [anchor_record("000100", "甲郵便局", "東京都港区２")]
        out = run([event("e1", "R8.13.99", before, after)], records)
        codes = [q["code"] for q in out["qa"]]
        self.assertIn("invalid_planned_date", codes)
        self.assertNotIn("future_event", codes)
        self.assertEqual(out["metadata"]["invalid_planned_date_count"], 1)
        self.assertFalse(out["events"][0]["state_corroborated"])

    def test_genuinely_future_event_is_still_reported_as_future(self):
        before = state("000100", "甲郵便局", "東京都港区１")
        after = state("000100", "甲郵便局", "東京都港区２")
        records = [anchor_record("000100", "甲郵便局", "東京都港区１")]
        out = run([event("e1", "2030-01-01", before, after)], records)
        self.assertIn("future_event", {q["code"] for q in out["qa"]})
        self.assertEqual(out["metadata"]["invalid_planned_date_count"], 0)


if __name__ == "__main__":
    unittest.main()
