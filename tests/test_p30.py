import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from postal_bias.p30 import inspect_p30


class P30Tests(unittest.TestCase):
    def test_inspect_fixture(self):
        # The official P30 archive is not redistributable from this repository.
        # A minimal synthetic archive exercises the same inspection contract.
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "synthetic-p30.zip"
            dbf_header = bytearray(32)
            dbf_header[0] = 0x03
            struct.pack_into("<I", dbf_header, 4, 3)
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("P30.dbf", bytes(dbf_header))
                zf.writestr("P30.prj", "GEOGCS[\"JGD_2000\"]")
                zf.writestr("P30.shp", b"synthetic")
                zf.writestr("P30.shx", b"synthetic")
                zf.writestr("KS-META-P30.xml", "<metadata />")

            result = inspect_p30(archive)
            self.assertTrue(result["ok"])
            self.assertTrue(result["sha256"])
            self.assertEqual(sum(result["dbf_record_counts"].values()), 3)
            self.assertFalse(result["public_release_allowed"])
