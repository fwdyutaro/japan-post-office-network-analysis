import tempfile,unittest
from pathlib import Path
from postal_bias.report import *
class RTests(unittest.TestCase):
 def test_not_eval_disclaimer(self):
  r=build_final_quality_report({},{}); self.assertEqual(r["status"],"not_evaluable"); self.assertIn("legal",r["legal_disclaimer"])
 def test_pass_gate(self): self.assertEqual(build_final_quality_report({}, {k:{"status":"pass"} for k in ("source_coverage","current","history","653","V1","V2","V3","P30_join","population","grid","analysis","visualization")})["status"],"pass")
 def test_writer_ack(self):
  with tempfile.TemporaryDirectory() as d: self.assertRaises(ReportError,write_report_bundle,build_final_quality_report({},{}),Path(d)/"o")
if __name__=='__main__':unittest.main()
