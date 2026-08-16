import json,tempfile,unittest
from pathlib import Path
from postal_bias.visualization import *
class VTests(unittest.TestCase):
 def setUp(self):
  self.g=[{"grid_id":"g1","x":0,"y":0,"crs":"EPSG:6933"},{"grid_id":"g2","x":10,"y":10,"crs":"EPSG:6933"}]
  self.a=[{"month":"2020-01","grid_id":"g1","radius_m":2000,"raw_count":1},{"month":"2020-02","grid_id":"g1","radius_m":2000,"raw_count":3}]
  self.c={"months":["2020-01","2020-02"],"radius_m":2000,"metric":"raw_count","extent":[-20,-20,20,20]}
 def test_frames_global_scale(self):
  r=render_visualization(self.a,self.g,self.c); self.assertEqual(len(r["frames"]),2); self.assertEqual(r["frames"][0]["color_max"],3)
 def test_missing_frame(self):
  r=render_visualization(self.a[:1],self.g,self.c); self.assertEqual(r["frames"][1]["coverage_status"],"not_evaluable"); self.assertTrue(r["coverage_summary"]["reason_codes"])
 def test_xml_escape(self):
  g=[{"grid_id":"<x>","x":0,"y":0,"crs":"EPSG:6933"}]; self.assertNotIn("<x>",render_visualization(self.a,g,self.c)["frames"][0]["svg"])
 def test_html_offline(self):
  r=render_visualization(self.a,self.g,self.c)
  with tempfile.TemporaryDirectory() as d: write_visualization_bundle(r,Path(d)/"o",acknowledge_internal_use=True); t=(Path(d)/"o"/"animation.html").read_text(); self.assertNotIn("http://",t); self.assertNotIn("script src",t)
 def test_html_payload_escaped(self):
  r=render_visualization(self.a,self.g,self.c); r["frames"][0]["svg"]='</script><img/onerror="x">\u2028';
  with tempfile.TemporaryDirectory() as d:
   write_visualization_bundle(r,Path(d)/"o",acknowledge_internal_use=True); t=(Path(d)/"o"/"animation.html").read_text(); self.assertNotIn("</script><img",t); self.assertEqual(t.count("</script>"),1); self.assertIn("\\u003c",t)
 def test_bad_radius(self):
  c=dict(self.c,radius_m=1); self.assertRaises(VisualizationError,render_visualization,self.a,self.g,c)
 def test_not_eval_input(self): self.assertEqual(render_visualization([],[],self.c)["coverage_summary"]["status"],"not_evaluable")
 def test_ack(self):
  with self.assertRaises(VisualizationError): write_visualization_bundle(render_visualization(self.a,self.g,self.c),tempfile.mkdtemp())
if __name__=='__main__':unittest.main()
