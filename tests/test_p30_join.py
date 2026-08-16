import hashlib, json, tempfile, unittest
from unittest.mock import patch
import shutil
from pathlib import Path

from postal_bias.artifacts import seal_artifact, verify_artifact
from postal_bias.p30_join import (P30JoinError, _review_id, apply_p30_join_reviews,
                                  build_p30_join_candidates, write_p30_join_bundle)


def j(v): return json.dumps(v, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
def rh(v): return hashlib.sha256(j(v).encode()).hexdigest()


class P30JoinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        row = {"p30_record_id": "a" * 64, "source_feature_id": "f1", "position_id": "p1", "source_document_id":"doc-2013", "coverage_date": "2013-11-30", "administrative_area": "001", "administrative_code": "001", "name_raw": "中央局", "address_raw": "東京都千代田区", "latitude": 35.0, "longitude": 139.0, "coordinate_crs": "EPSG:4612 (JGD2000)", "coordinate_validity":"valid_range"}
        row["source_row_sha256"] = rh(row); self.p30 = self._seal_p30(row)

    def tearDown(self): self.tmp.cleanup()

    def _seal_p30(self, row):
        d = self.base / ("p30-" + row["p30_record_id"][:4]); d.mkdir()
        d.joinpath("records.jsonl").write_text(j(row) + "\n", encoding="utf8")
        d.joinpath("metadata.json").write_text(j({"coverage_date":"2013-11-30","source_crs":"EPSG:4612 (JGD2000)","record_count":1,"qa_error_count":0,"counts":{"records":1,"xml_offices":1,"xml_points":1,"dbf":1,"shp":1}}) + "\n", encoding="utf8")
        d.joinpath("qa.jsonl").write_text("", encoding="utf8")
        seal_artifact(d, artifact_type="p30", source_authority="official", evidence_status="effective", release_classification="internal_only", acknowledge_internal_use=True)
        return d

    def _anchor(self, name="中央局"):
        d = self.base / ("anchor-" + name + "-" + next(tempfile._get_candidate_names())); d.mkdir()
        row = {"entity_id":"e1","official_identifier":"0001","name":name,"municipality_code":"001","address_local_key":"東京都千代田区","historical_coordinate":{"latitude":35.0,"longitude":139.0,"crs":"EPSG:4612 (JGD2000)","valid_from":"2013-11-30","valid_to":"2013-11-30"}}
        row["source_row_sha256"] = rh(row)
        d.joinpath("p30_anchor.jsonl").write_text(j(row)+"\n",encoding="utf8"); d.joinpath("metadata.json").write_text(j({"coverage_date":"2013-11-30","source_crs":"EPSG:4612 (JGD2000)","record_count":1,"qa_error_count":0})+"\n",encoding="utf8"); d.joinpath("qa.jsonl").write_text("",encoding="utf8")
        seal_artifact(d, artifact_type="p30_anchor-v1", source_authority="official", evidence_status="effective", release_classification="internal_only", acknowledge_internal_use=True); return d

    def test_missing_anchor_is_not_evaluable(self):
        r = build_p30_join_candidates(self.p30); self.assertEqual(r["coverage_summary"]["reason_codes"], ["missing_2013_anchor"]); self.assertEqual(r["coverage_summary"]["candidate_count"], 0)

    def test_candidate_name_and_address_are_ranked(self):
        r = build_p30_join_candidates(self.p30, self._anchor()); self.assertEqual(len(r["join_candidates"]), 1); self.assertEqual(r["join_candidates"][0]["candidates"][0]["target_entity_id"], "e1")

    def test_candidate_is_never_accepted(self): self.assertEqual(build_p30_join_candidates(self.p30, self._anchor())["join_candidates"][0]["decision"], "unreviewed")
    def test_input_order_is_deterministic(self):
        a = build_p30_join_candidates(self.p30, self._anchor()); b = build_p30_join_candidates(self.p30, self._anchor()); self.assertEqual(a["join_candidates"], b["join_candidates"])
    def test_p30_id_not_match_key(self):
        a = self._anchor(); r = build_p30_join_candidates(self.p30, a); self.assertNotIn("a" * 64, {c["target_entity_id"] for c in r["join_candidates"][0]["candidates"]})
    def test_current_coordinate_not_used(self):
        a = self._anchor(); p = build_p30_join_candidates(self.p30, a); self.assertEqual(p["metadata"]["coordinate_status"], "not_evaluable")
    def test_coordinate_disabled_without_pyproj_reason(self):
        # Simulate pyproj being absent rather than depending on the environment:
        # the assertion must hold on machines that do have pyproj installed.
        import builtins
        real_import = builtins.__import__

        def no_pyproj(name, *a, **kw):
            if name == "pyproj":
                raise ImportError("simulated: pyproj unavailable")
            return real_import(name, *a, **kw)

        builtins.__import__ = no_pyproj
        try:
            r = build_p30_join_candidates(self.p30, self._anchor(), {"coordinate_matching": True})
        finally:
            builtins.__import__ = real_import
        self.assertTrue(any("pyproj" in q["code"] for q in r["qa"]))

    def test_coordinate_matching_enabled_when_pyproj_present(self):
        pyproj = __import__("importlib").util.find_spec("pyproj")
        if pyproj is None:
            self.skipTest("pyproj not installed")
        r = build_p30_join_candidates(self.p30, self._anchor(), {"coordinate_matching": True})
        self.assertFalse(any("pyproj" in q["code"] for q in r["qa"]))
    def test_unsealed_rejected(self):
        d = self.base / "unsealed"; d.mkdir(); d.joinpath("records.jsonl").write_text("",encoding="utf8"); self.assertRaises(P30JoinError, build_p30_join_candidates, d)
    def test_tampered_p30_rejected(self):
        self.p30.joinpath("records.jsonl").write_text("{}\n",encoding="utf8"); self.assertRaises(P30JoinError, build_p30_join_candidates, self.p30)
    def test_p30_mutate_rehash_reseal_range_rejected(self):
        row = json.loads(self.p30.joinpath("records.jsonl").read_text(encoding="utf8").splitlines()[0]); row["latitude"] = 80.0; row["source_row_sha256"] = rh({k:v for k,v in row.items() if k != "source_row_sha256"}); self.p30.joinpath("records.jsonl").write_text(j(row)+"\n", encoding="utf8"); seal_artifact(self.p30, artifact_type="p30", source_authority="official", evidence_status="effective", release_classification="internal_only", replace=True, acknowledge_internal_use=True); self.assertRaises(P30JoinError, build_p30_join_candidates, self.p30)
    def test_wrong_anchor_date_rejected(self):
        d = self._anchor(); d.joinpath("metadata.json").write_text(j({"coverage_date":"2020-01-01"})+"\n",encoding="utf8"); self.assertRaises(P30JoinError, build_p30_join_candidates, self.p30, d)
    def test_anchor_mutate_then_reseal_hash_rejected(self):
        d = self._anchor(); row = json.loads(d.joinpath("p30_anchor.jsonl").read_text(encoding="utf8").splitlines()[0]); row["name"] = "改変"; d.joinpath("p30_anchor.jsonl").write_text(j(row)+"\n",encoding="utf8"); seal_artifact(d, artifact_type="p30_anchor-v1", source_authority="official", evidence_status="effective", release_classification="internal_only", replace=True, acknowledge_internal_use=True); self.assertRaises(P30JoinError, build_p30_join_candidates, self.p30, d)
    def test_review_target_must_be_candidate(self):
        c = build_p30_join_candidates(self.p30, self._anchor())["join_candidates"][0]; d={"candidate_id":c["candidate_id"],"candidate_artifact_id":"x","decision":"accepted","target_entity_id":"no","p30_record_id":c["p30_record_id"]}; d["review_id"]=_review_id(d); self.assertTrue(any(q["code"]=="invalid_review" for q in apply_p30_join_reviews(self._seal_candidates(build_p30_join_candidates(self.p30,self._anchor())), [d])["qa"]))
    def _seal_candidates(self, result):
        d=self.base/("candidates-"+next(tempfile._get_candidate_names())); d.mkdir(); d.joinpath("join_candidates.jsonl").write_text("".join(j(x)+"\n" for x in result["join_candidates"]),encoding="utf8"); d.joinpath("metadata.json").write_text(j(result["metadata"])+"\n",encoding="utf8"); d.joinpath("qa.jsonl").write_text("",encoding="utf8"); seal_artifact(d,artifact_type="p30_join_candidates",source_authority="official",evidence_status="effective",release_classification="internal_only",acknowledge_internal_use=True); return d
    def test_review_hash_binding(self):
        c=self._seal_candidates(build_p30_join_candidates(self.p30,self._anchor())); row=json.loads(c.joinpath("join_candidates.jsonl").read_text(encoding="utf8")); d={"candidate_id":row["candidate_id"],"candidate_artifact_id":"0"*64,"decision":"deferred"}; d["review_id"]=_review_id(d); self.assertTrue(any(q["code"]=="invalid_review" for q in apply_p30_join_reviews(c,[d])["qa"]))
    def test_unresolved_is_not_evaluable(self):
        c=self._seal_candidates(build_p30_join_candidates(self.p30,self._anchor())); r=apply_p30_join_reviews(c,[]); self.assertEqual(r["coverage_summary"]["overall_status"],"not_evaluable")
    def test_verified_transform_emits_historical_point(self):
        c=self._seal_candidates(build_p30_join_candidates(self.p30,self._anchor())); row=json.loads(c.joinpath("join_candidates.jsonl").read_text(encoding="utf8").splitlines()[0]); ad=json.loads(c.joinpath("artifact.json").read_text(encoding="utf8")); d={"candidate_artifact_id":ad["artifact_id"],"candidate_artifact_checksums_sha256":ad["payload_checksums_sha256"],"candidate_id":row["candidate_id"],"candidate_hash":row["candidate_hash"],"p30_record_id":row["p30_record_id"],"p30_source_document_id":row["p30_source_document_id"],"p30_source_row_sha256":row["p30_source_row_sha256"],"anchor_artifact_id":row["anchor_artifact_id"],"anchor_coverage_date":row["anchor_coverage_date"],"decision":"accepted","target_entity_id":"e1","anchor_record_sha256":row["candidates"][0]["anchor_record_sha256"],"match_confidence":1.0,"selection_type":"exact_name_municipality","reviewer_id":"r","reviewed_at":"2026-01-01T00:00:00Z","rationale_code":"exact","supersedes":None}; d["review_id"]=_review_id(d)
        class T:
            source_crs="EPSG:4612 (JGD2000)"; target_crs="EPSG:6668"; version="t1"; definition="test"
            def __call__(self,lat,lon): return lat+0.001,lon+0.001
        r=apply_p30_join_reviews(c,[d],transformer=T()); self.assertEqual(r["coverage_summary"]["coordinate_status"],"pass"); self.assertEqual(r["facility_geocode"][0]["valid_to"],"2013-11-30")
    def test_writer_requires_acknowledgements(self):
        with self.assertRaises(P30JoinError): write_p30_join_bundle(build_p30_join_candidates(self.p30), self.base/"out", acknowledge_internal_use=True)
    def test_writer_seals_and_no_overwrite(self):
        out=self.base/"out"; write_p30_join_bundle(build_p30_join_candidates(self.p30),out,acknowledge_internal_use=True,acknowledge_noncommercial_use=True); self.assertTrue(verify_artifact(out)["ok"])
        with self.assertRaises(P30JoinError):
            write_p30_join_bundle(build_p30_join_candidates(self.p30),out,acknowledge_internal_use=True,acknowledge_noncommercial_use=True)

    def test_backup_cleanup_fault_is_resealed_and_reported(self):
        out=self.base/"out"; result=build_p30_join_candidates(self.p30); write_p30_join_bundle(result,out,acknowledge_internal_use=True,acknowledge_noncommercial_use=True)
        original=shutil.rmtree; state={"raised":False}
        def flaky(path, *args, **kwargs):
            if not state["raised"]:
                state["raised"] = True; raise OSError("locked")
            return original(path, *args, **kwargs)
        with patch("postal_bias.p30_join.shutil.rmtree", side_effect=flaky):
            paths=write_p30_join_bundle(result,out,acknowledge_internal_use=True,acknowledge_noncommercial_use=True,replace=True)
        self.assertTrue(paths.get("cleanup_warnings")); self.assertTrue(verify_artifact(out)["ok"])
        metadata=json.loads(out.joinpath("metadata.json").read_text(encoding="utf8")); self.assertTrue(metadata["cleanup_warnings"])


if __name__ == "__main__": unittest.main()
