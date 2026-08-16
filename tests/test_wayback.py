import json, tempfile, unittest
from postal_bias.wayback import *
class WaybackTests(unittest.TestCase):
 def test_parse_json_and_supersede(self):
  rows=[{"urlkey":"x","timestamp":"20200101000000","original":"https://www.post.japanpost.jp/newsrelease/storeinformation/a.pdf","mime":"application/pdf","status":"200","digest":"a","length":"2"},{"timestamp":"20210101000000","original":"https://www.post.japanpost.jp/newsrelease/storeinformation/a.pdf","mime":"application/pdf","status":"200","digest":"b","length":"2"}]
  r=parse_wayback_cdx(json.dumps(rows).encode(),coverage_date="2020-01-01"); self.assertEqual(len(r["records"]),2); self.assertIn("supersedes_capture_timestamp",r["records"][1])
 def test_reject_url_and_timestamp(self):
  r=parse_wayback_cdx(json.dumps([{"timestamp":"bad","original":"https://evil.example/a","digest":"a","length":"1"}]).encode(),coverage_date="2020-01-01"); self.assertTrue(r["qa"])
 def test_same_capture_conflict_is_order_independent(self):
  a={"timestamp":"20200101000000","original":"https://www.post.japanpost.jp/newsrelease/storeinformation/a.pdf","mime":"application/pdf","status":"200","digest":"a","length":"1"}; b=dict(a,digest="b")
  x=parse_wayback_cdx(json.dumps([a,b]).encode(),coverage_date="2020-01-01"); y=parse_wayback_cdx(json.dumps([b,a]).encode(),coverage_date="2020-01-01")
  self.assertEqual(x["records"],y["records"]); self.assertTrue(any(q["code"]=="conflicting_capture" for q in x["qa"]))
 def test_empty_is_not_evaluable(self):
  r=parse_wayback_cdx(b"[]"); self.assertEqual(r["metadata"]["validation_status"],"not_evaluable"); self.assertTrue(r["qa"])
