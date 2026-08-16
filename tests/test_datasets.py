import tempfile,unittest
from postal_bias.datasets import *
class DatasetTests(unittest.TestCase):
 def test_valid_and_public_gate(self):
  d={"dataset_id":"p","family":"p30","title":"P30","publisher":"MLIT","format":"zip","mime":"application/zip","coverage_date":"2013-11-30","granularity":"point","crs_epsg":4612,"geometry":"Point","field_schema":[{"name":"name","type":"string"}],"license":"noncommercial","terms":"https://example.invalid","public_release_allowed":False}
  self.assertEqual(validate_dataset(d)["dataset_id"],"p")
 def test_missing_crs(self):
  with self.assertRaises(DatasetError): validate_dataset({"dataset_id":"x"})
