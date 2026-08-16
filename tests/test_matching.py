import io, tempfile, unittest, json
from contextlib import redirect_stderr
from pathlib import Path
from postal_bias.matching import MatchingError, match_src06, normalize_text, write_match_outputs
from postal_bias.cli import main

SRC_SHA = "a" * 64

def rec(i, name, address):
    return {"official_identifier": i, "name": {"raw": name}, "address": {"raw": address}, "source_page": 1, "source_line": int(i), "source_line_sha256": "x"}

def ev(i, name, address, kind="temporarily_closed", official_id=None):
    state = {"name": {"raw": name}, "address": {"raw": address}}
    if official_id is not None:
        state["official_identifier"] = {"raw": official_id}
    return {"event_id": i, "event_type": kind, "source_family": "SRC-06", "provenance": "unofficial", "event_status": "announced", "source_document_id": "src06", "observed_effective_date": "2026-01-02", "after_state": state}

class MatchingTests(unittest.TestCase):
    def test_unique_auto_tie_manual_and_unmatched(self):
        anchor = {"records": [rec("000001","局A","東京都A"), rec("000002","局B","東京都B"), rec("000003","局B","旧住所")], "metadata":{"source_document_id":"src-anchor"}}
        src = {"events":[ev("a","局A","東京都A"), ev("d","局B","東京都B", official_id="000002"), ev("b","局B",""), ev("c","不存在","東京都C")], "current_closed_observations":[], "metadata":{"source_document_id":"src06", "source_family":"SRC-06", "source_content_sha256":SRC_SHA}}
        r=match_src06(anchor,src)
        self.assertEqual(sum(x["decision"]=="auto_accept" for x in r["match_candidates"]),2)
        self.assertTrue(any(x["decision"]=="manual_review" for x in r["match_reviews"]))
        self.assertTrue(any(x["decision"]=="unmatched" for x in r["match_reviews"]))
        self.assertNotIn("detail_key", json.dumps(r))
        self.assertEqual(r["metadata"]["source_content_sha256"], SRC_SHA)
        self.assertTrue({"source_name_raw", "source_address_raw", "candidate_count", "identity_confidence", "review_status", "name_score", "address_score", "id_score", "conflict_codes"}.issubset(r["match_candidates"][0]))

    def test_normalizer_and_invalid_source(self):
        self.assertEqual(normalize_text(" 簡易郵便局　－　Ａ "), "簡易郵便局 - a")
        self.assertEqual(normalize_text("センター"), "センター")
        bad=ev("bad","局A","東京都A"); bad["provenance"]="official"
        r=match_src06({"records":[rec("000001","局A","東京都A")]},{"events":[bad],"current_closed_observations":[],"metadata":{"source_document_id":"src06", "source_family":"SRC-06", "source_content_sha256":SRC_SHA}})
        self.assertTrue(r["qa"])

    def test_duplicate_anchor_id_never_auto_accepts(self):
        anchor = {"records": [rec("000001", "A", "Tokyo A"), rec("000001", "A", "Tokyo Old")]}
        result = match_src06(anchor, {"events": [ev("x", "anything", "anything", official_id="000001")], "current_closed_observations": [], "metadata":{"source_document_id":"src06", "source_family":"SRC-06", "source_content_sha256":SRC_SHA}})
        self.assertEqual(len(result["match_candidates"]), 2)
        self.assertTrue(all(c["decision"] == "manual_review" for c in result["match_candidates"]))
        self.assertEqual(len({c["candidate_id"] for c in result["match_candidates"]}), 2)
        self.assertEqual(result["match_reviews"][0]["decision"], "manual_review")

    def test_observation_and_metadata_validation(self):
        anchor = {"records": [rec("000001", "局A", "東京都A")]}
        obs = {"observation_id": "o", "source_family": "SRC-06", "provenance": "unofficial", "source_document_id": "src06", "closure_start_date": "2026-02-30", "is_currently_closed": True, "name_raw": "局A", "address_raw": "東京都A"}
        result = match_src06(anchor, {"events": [], "current_closed_observations": [obs], "metadata": {"source_document_id": "src06", "source_family": "SRC-06", "source_content_sha256": SRC_SHA}})
        self.assertTrue(any(q["code"] == "invalid_current_closed_observation" for q in result["qa"]))
        obs["closure_start_date"] = "2026-02-28"; obs["address_raw"] = ""; obs["source_row_index"] = 7; obs["source_row_sha256"] = "b" * 64
        result = match_src06(anchor, {"events": [], "current_closed_observations": [obs], "metadata": {"source_document_id": "src06", "source_family": "SRC-06", "source_content_sha256": SRC_SHA}})
        self.assertEqual(result["match_reviews"][0]["source_line"], 7)
        self.assertEqual(result["match_reviews"][0]["source_line_sha256"], "b" * 64)
        with self.assertRaises(MatchingError):
            match_src06(anchor, {"events": [], "current_closed_observations": [], "metadata": {"source_document_id": "src06", "source_family": "spoof", "source_content_sha256": SRC_SHA}})

    def test_id_conflicts_invalid_dates_and_duplicate_rows_fail_closed(self):
        anchor = {"records": [rec("000001", "局A", "東京都A")]}
        meta = {"source_document_id": "src06", "source_family": "SRC-06", "source_content_sha256": SRC_SHA}
        invalid = ev("bad-id", "局A", "東京都A", official_id="oops")
        dated = ev("dated", "局A", "東京都A"); dated["planned_effective_date"] = "2026-02-01"
        unknown = ev("unknown", "局A", "東京都A", official_id="999999")
        conflict = ev("conflict", "別名", "別住所", official_id="000001")
        duplicate = ev("dup", "局A", "東京都A")
        result = match_src06(anchor, {"events": [invalid, dated, unknown, conflict, duplicate, duplicate.copy()], "current_closed_observations": [], "metadata": meta})
        codes = {q["code"] for q in result["qa"]}
        self.assertIn("invalid_source_official_identifier", codes); self.assertIn("invalid_src06_event", codes); self.assertIn("duplicate_source_row_id", codes)
        self.assertEqual(len(result["match_candidates"]), 2)
        self.assertTrue(any("official_identifier_not_found" in r["conflict_codes"] for r in result["match_reviews"]))
        self.assertTrue(any("official_identifier_field_conflict" in r["conflict_codes"] for r in result["match_reviews"]))
        self.assertEqual(result["match_candidates"][0]["review_status"], "manual")

    def test_atomic_and_cli_safe(self):
        r=match_src06({"records":[rec("000001","局A","東京都A")]},{"events":[ev("a","局A","東京都A")],"current_closed_observations":[],"metadata":{"source_document_id":"src06", "source_family":"SRC-06", "source_content_sha256":SRC_SHA}})
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(MatchingError): write_match_outputs(r,d)
            write_match_outputs(r,d,acknowledge_internal_use=True)
            with self.assertRaises(MatchingError): write_match_outputs(r,d,acknowledge_internal_use=True)
        err=io.StringIO()
        with redirect_stderr(err): code=main(["match-src06","--anchor",r"C:\private\missing.jsonl","--src06-dir",r"C:\private\src","--output-dir",r"C:\private\out"])
        self.assertEqual(code,2); self.assertNotIn("missing.jsonl",err.getvalue()); self.assertNotIn("Traceback",err.getvalue())
