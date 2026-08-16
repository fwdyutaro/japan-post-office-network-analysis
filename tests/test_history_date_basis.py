"""Replay uses a planned date only for state-corroborated events, and says so.

Also covers the ``build-history`` coverage-date normalisation: ``YYYY-MM`` used
to reach ``build_history`` unparsed, which silently produced ``anchor_date=None``.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from postal_bias.cli import main
from postal_bias.history import build_history, date_basis, effective_date, replay_history


def _field(value):
    return {"raw": value, "normalized": value}


def _record(identifier="000001", address="東京都千代田区１－１"):
    return {"section": "postal_office", "official_identifier": identifier,
            "name": _field("中央郵便局"), "address": _field(address),
            "operating_status": {"raw": "営業中", "normalized": "operating"}}


def _state(identifier, address):
    return {"section": _field("postal_office"), "official_identifier": _field(identifier),
            "name": _field("中央郵便局"), "address": _field(address)}


def _event(eid, *, planned=None, confirmed=None, corroborated=None, address_after="東京都千代田区２－２"):
    row = {"event_id": eid, "event_type": "relocation", "event_status": "confirmed",
           "planned_effective_date": planned, "confirmed_effective_date": confirmed,
           "provenance": "official", "source_document_id": "src-events",
           "before_state": _state("000001", "東京都千代田区１－１"),
           "after_state": _state("000001", address_after)}
    if corroborated is not None:
        row["state_corroborated"] = corroborated
    return row


class DateBasisTests(unittest.TestCase):
    def test_corroborated_event_replays_on_the_planned_date(self):
        e = _event("e1", planned="2026-02-01", corroborated=True)
        self.assertEqual(effective_date(e), "2026-02-01")
        self.assertEqual(date_basis(e), "planned")

    def test_observed_date_wins_and_is_labelled_observed(self):
        e = _event("e1", planned="2026-02-01", confirmed="2026-02-01", corroborated=True)
        self.assertEqual(effective_date(e), "2026-02-01")
        self.assertEqual(date_basis(e), "observed")

    def test_event_without_the_flag_is_not_replayed(self):
        e = _event("e1", planned="2026-02-01")
        self.assertIsNone(effective_date(e))
        self.assertEqual(date_basis(e), "unknown")

    def test_history_replays_planned_and_reports_the_basis(self):
        h = build_history({"records": [_record()], "metadata": {"coverage_date": "2026-01-31"}},
                          [_event("e1", planned="2026-02-01", corroborated=True)])
        self.assertEqual(h["effective_date_basis"], "planned")
        self.assertEqual(h["effective_date_basis_counts"]["planned"], 1)
        self.assertEqual(replay_history(h, "2026-03-01")[0]["applied_event_ids"], ["e1"])

    def test_history_ignores_an_event_missing_the_flag(self):
        h = build_history({"records": [_record()], "metadata": {"coverage_date": "2026-01-31"}},
                          [_event("e1", planned="2026-02-01")])
        self.assertEqual(h["effective_date_basis"], "none_applied")
        self.assertEqual(replay_history(h, "2026-03-01")[0]["applied_event_ids"], [])


class BuildHistoryCoverageDateTests(unittest.TestCase):
    def _inputs(self, base: Path):
        anchor_dir = base / "anchor"
        anchor_dir.mkdir()
        anchor_dir.joinpath("records.jsonl").write_text(
            json.dumps(_record(), ensure_ascii=False) + "\n", encoding="utf-8")
        anchor_dir.joinpath("metadata.json").write_text(
            json.dumps({"coverage_date": "2026-06-30", "source_document_id": "src-anchor"}),
            encoding="utf-8")
        events = base / "events.jsonl"
        events.write_text(json.dumps(_event("e1", planned="2026-02-01", corroborated=True),
                                     ensure_ascii=False) + "\n", encoding="utf-8")
        return anchor_dir / "records.jsonl", events

    def _run(self, coverage_date: str):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            records, events = self._inputs(base)
            out = base / "out"
            code = main(["build-history", "--anchor", str(records), "--events", str(events),
                         "--coverage-date", coverage_date, "--output-dir", str(out),
                         "--acknowledge-internal-use"])
            self.assertEqual(code, 0)
            meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
            snapshots = [json.loads(x) for x in
                         (out / "snapshots.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
            return meta, snapshots

    def test_month_form_coverage_date_normalises_to_the_month_end(self):
        meta, snapshots = self._run("2026-06")
        # Before the fix this reached build_history as "2026-06", which _day
        # could not parse, so the anchor date became None.
        self.assertEqual(meta["anchor_date"], "2026-06-30")
        self.assertEqual(meta["coverage_date_input"], "2026-06")
        self.assertEqual(meta["snapshot_cutoff"], "2026-06-30")
        self.assertEqual({s["snapshot_cutoff"] for s in snapshots}, {"2026-06-30"})

    def test_day_form_and_month_form_agree(self):
        month_meta, month_rows = self._run("2026-06")
        day_meta, day_rows = self._run("2026-06-30")
        self.assertEqual(month_meta["anchor_date"], day_meta["anchor_date"])
        self.assertEqual([r["state"] for r in month_rows], [r["state"] for r in day_rows])

    def test_metadata_states_the_effective_date_basis(self):
        meta, _ = self._run("2026-06")
        self.assertEqual(meta["effective_date_basis"], "planned")
        self.assertEqual(meta["effective_date_basis_counts"]["planned"], 1)
        self.assertIn("planned", meta["effective_date_basis_note"])


if __name__ == "__main__":
    unittest.main()
