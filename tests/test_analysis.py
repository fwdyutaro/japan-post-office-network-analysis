import hashlib, json, tempfile, unittest
from pathlib import Path
from postal_bias.analysis import *
from postal_bias.artifacts import seal_artifact

def h(r):
    x=dict(r); x.pop("row_sha256",None); return hashlib.sha256(json.dumps(x,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
def cfg(): return {"months":["2020-01"],"radii_m":[2000,5000,10000,20000],"extent":[0,0,10000,10000],"grid_id_field":"grid_id","population_series":"census","service_filter":"all","status_filter":"all","closure_filter":"all","section_filter":"all"}
class AnalysisTests(unittest.TestCase):
 def setUp(self):
  self.grid=[]
  for gid,x in (("g1",0),("g2",3000)):
   r={"grid_id":gid,"x":x,"y":0,"crs":"EPSG:6933","window_area_km2":1.0,"extent":[-1000,-1000,5000,1000]}; r["row_sha256"]=h(r); self.grid.append(r)
  self.snap=[]
  for i,(x,status) in enumerate(((0,"open"),(1000,"open"),(4000,"open"))):
   r={"month":"2020-01","entity_id":"e"+str(i),"x":x,"y":0,"crs":"EPSG:6933","status":status,"section":"post","series":"formal","provenance":"official"}; r["row_sha256"]=h(r); self.snap.append(r)
  self.pop=[]
  for g in self.grid:
   r={"grid_id":g["grid_id"],"month":"2020-01","series":"census","unit":"persons","population":1000,"source":"fixture"}; r["row_sha256"]=h(r); self.pop.append(r)
 def test_exact_inequality(self): self.assertAlmostEqual(gini([0,1,2,3]),5/12); self.assertAlmostEqual(theil_t([0,1,2,3]),.3748900964); self.assertAlmostEqual(hoover([0,1,2,3]),1/3)
 def test_metrics_radii(self):
  r=analyze_monthly(self.snap,self.grid,self.pop,cfg()); self.assertEqual(len(r["cell_metrics"]),8); self.assertEqual(r["cell_metrics"][0]["raw_count"],2)
 def test_nearest_none(self):
  s=[]; r=analyze_monthly(s,self.grid,self.pop,cfg()); self.assertEqual(r["coverage_summary"]["status"],"not_evaluable")
 def test_zero_population(self):
  p=[dict(x, population=0, row_sha256=h(dict(x, population=0))) for x in self.pop]; r=analyze_monthly(self.snap,self.grid,p,cfg()); self.assertIsNone(r["cell_metrics"][0]["count_per_100k"])
 def test_missing_population(self): self.assertEqual(analyze_monthly(self.snap,self.grid,[],cfg())["coverage_summary"]["status"],"not_evaluable")
 def test_duplicate_snapshot(self): self.assertEqual(analyze_monthly(self.snap+self.snap[:1],self.grid,self.pop,cfg())["coverage_summary"]["status"],"not_evaluable")
 def test_mixed_crs(self): s=list(self.snap); s[0]=dict(s[0],crs="EPSG:4326"); s[0]["row_sha256"]=h(s[0]); self.assertEqual(analyze_monthly(s,self.grid,self.pop,cfg())["coverage_summary"]["status"],"not_evaluable")
 def test_mixed_unit(self): p=list(self.pop); p[0]=dict(p[0],unit="households"); p[0]["row_sha256"]=h(p[0]); self.assertEqual(analyze_monthly(self.snap,self.grid,p,cfg())["coverage_summary"]["status"],"not_evaluable")
 def test_bad_config_radii(self):
  c=cfg(); c["radii_m"]=[1]; self.assertRaises(AnalysisError,validate_config,c)
 def test_unsorted_months_rejected(self):
  c=cfg(); c["months"]=["2020-02","2020-01"]; self.assertRaises(AnalysisError,validate_config,c)
 def test_grid_nan_rejected(self):
  g=list(self.grid); g[0]=dict(g[0],x=float("nan")); g[0]["row_sha256"]=h(g[0]); self.assertEqual(analyze_monthly(self.snap,g,self.pop,cfg())["coverage_summary"]["status"],"not_evaluable")
 def test_bool_weight_rejected(self): self.assertIsNone(gini([1,2],[True,1]))
 def test_filter_type_rejected(self):
  c=cfg(); c["status_filter"]=42; self.assertRaises(AnalysisError,validate_config,c)
 def test_wrong_sealed_p30_type_rejected(self):
  with tempfile.TemporaryDirectory() as d:
   p=Path(d); p.joinpath("records.jsonl").write_text("\n",encoding="utf8"); p.joinpath("metadata.json").write_text("{}\n",encoding="utf8"); p.joinpath("qa.jsonl").write_text("",encoding="utf8"); seal_artifact(p,artifact_type="p30",source_authority="official",evidence_status="effective",release_classification="internal_only",acknowledge_internal_use=True); self.assertEqual(analyze_monthly(p,self.grid,self.pop,cfg())["coverage_summary"]["status"],"not_evaluable")
 def test_gini_empty(self): self.assertIsNone(gini([])); self.assertIsNone(theil_t([0,0])); self.assertIsNone(hoover([0,0]))
 def test_weighted_gini(self): self.assertAlmostEqual(gini([0,1],[1,1]),.5)
 def test_moran_invalid(self): self.assertIsNone(global_moran([1,1],[])); self.assertIsNone(global_moran([1,1,1],[(0,1,1)]))
 def test_moran_known(self): self.assertAlmostEqual(global_moran([1,2,3],[(0,1,1),(1,2,1)]),0.0)
 def test_determinism(self): self.assertEqual(analyze_monthly(self.snap,self.grid,self.pop,cfg()),analyze_monthly(list(reversed(self.snap)),list(reversed(self.grid)),self.pop,cfg()))
 def test_writer_ack(self):
  r=analyze_monthly([],self.grid,self.pop,cfg())
  with tempfile.TemporaryDirectory() as d: self.assertRaises(AnalysisError,write_analysis_bundle,r,Path(d)/"x")
 def test_writer_sealed(self):
  r=analyze_monthly(self.snap,self.grid,self.pop,cfg())
  with tempfile.TemporaryDirectory() as d:
   out=Path(d)/"x"; write_analysis_bundle(r,out,acknowledge_internal_use=True); self.assertTrue((out/"artifact.json").exists())
if __name__=='__main__': unittest.main()
