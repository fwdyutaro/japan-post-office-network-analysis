import json, tempfile, unittest
from pathlib import Path
from postal_bias.src06_review import (Src06ReviewError, apply_src06_review,
    promote_src06, reconcile_long_suspensions, validate_src06, write_review_bundle)
from postal_bias.src06_review import _sha, _review_digest
from postal_bias.cli import main

def candidate(cid="c1", target="000001", row="e1"):
    return {"candidate_id":cid,"source_row_id":row,"source_document_id":"src06","target_official_identifier":target,
            "source_name_raw":"A","source_address_raw":"Tokyo","method":"official_identifier","candidate_artifact_id":"art",
            "source_content_sha256":"b"*64,"anchor_source_document_id":"anchor","anchor_source_content_sha256":"c"*64,
            "target_line":1,"target_line_sha256":"d"*64}
def decision(c, **extra):
    r={"candidate_id":c["candidate_id"],"decision":"accepted",**{k:c[k] for k in ("candidate_artifact_id","source_document_id","source_content_sha256","anchor_source_document_id","anchor_source_content_sha256","source_row_id","target_official_identifier","target_line","target_line_sha256")},"candidate_sha256":_sha(c),"candidate_artifact_sha256":_sha(c)}
    r.update(extra); r["review_id"]=_review_digest(r); return r
def source_event(i="e1"):
    return {"event_id":i,"event_type":"temporarily_closed","source_family":"SRC-06","provenance":"unofficial","event_status":"announced",
            "source_document_id":"src06","source_line_sha256":"e"*64,"observed_effective_date":"2026-01-01","confirmed_effective_date":None,"planned_effective_date":None}

class Src06ReviewTests(unittest.TestCase):
    def test_apply_hash_chain_and_multiple(self):
        c=[candidate()]; good=decision(c[0])
        r=apply_src06_review(c,[good]); self.assertEqual(len(r["active_reviews"]),1)
        bad={**good,"candidate_sha256":"0"*64}; bad["review_id"]=_review_digest(bad); self.assertTrue(apply_src06_review(c,[bad])["review_qa"])
        self.assertTrue(any(q["code"]=="invalid_review_id" for q in apply_src06_review(c,[{**good,"review_id":"foo"}])["review_qa"]))

    def test_promote_resolution_and_conflict(self):
        active=apply_src06_review([candidate()],[decision(candidate())])
        src={"events":[source_event()],"current_closed_observations":[],"metadata":{"source_document_id":"src06","source_content_sha256":"a"*64}}
        hist={"official_identifier_history":[{"official_identifier":"000001","facility_entity_id":"ent-1"}]}
        out=promote_src06(active,[candidate()],src,hist); self.assertEqual(out["metadata"]["promoted_count"],1); self.assertEqual(out["promoted_events"][0]["facility_entity_id"],"ent-1")
        src["events"][0]["confirmed_effective_date"]="2026-01-01"; self.assertTrue(promote_src06(active,[candidate()],src,hist)["promotion_qa"])

    def test_reconcile_and_validation_gates(self):
        anchor={"records":[],"metadata":{"note_count":2}}
        p={"promoted_events":[{ "facility_entity_id":"e1","observed_effective_date":"2026-08-01","source_family":"SRC-06","provenance":"unofficial","event_status":"announced","review_status":"accepted","identity_review_id":"r1"},{"facility_entity_id":"e2","observed_effective_date":"2026-08-02","source_family":"SRC-06","provenance":"unofficial","event_status":"announced","review_status":"accepted","identity_review_id":"r2"}]}
        self.assertEqual(reconcile_long_suspensions(anchor,p,as_of="2026-08-08")["gate"],"pass")
        self.assertEqual(validate_src06(v1_official_ids={"1"},v1_src06_ids={"1"})["v1"]["status"],"pass")
        self.assertEqual(validate_src06()["status"],"not_evaluable")
        self.assertEqual(validate_src06(v3_rows=[{}]*199)["v3"]["status"],"not_evaluable")
        self.assertEqual(reconcile_long_suspensions(anchor,p,as_of="2026-08-08",tolerance_days=-1)["gate"],"not_evaluable")
        self.assertEqual(reconcile_long_suspensions({"records":[],"metadata":{"note_count":True}},p,as_of="2026-08-08")["gate"],"not_evaluable")

    def test_bundle_atomic_and_cli_validation(self):
        result=validate_src06(v1_official_ids={"1"},v1_src06_ids={"2"})
        with tempfile.TemporaryDirectory() as td:
            write_review_bundle({"metadata":{"public_release_allowed":False},"validation":[result]},td,acknowledge_internal_use=True)
            with self.assertRaises(Src06ReviewError): write_review_bundle({"metadata":{}},td,acknowledge_internal_use=True)
            p=Path(td)/"ids.json"; p.write_text("[\"1\"]",encoding="utf8")
            self.assertEqual(main(["validate-src06","--v1-official",str(p),"--v1-src06",str(p)]),3)

if __name__ == "__main__": unittest.main()
