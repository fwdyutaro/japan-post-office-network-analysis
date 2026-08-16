import tempfile
import unittest
from pathlib import Path
from postal_bias.cli import main


class CliTests(unittest.TestCase):
    def test_backfill_upper_bound_and_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            html = Path(d) / "index.html"
            html.write_text(''.join(f'<a href="pdf/2012{i:02d}01.pdf">2012年{i}月</a>' for i in range(1, 6)), encoding="utf-8")
            with self.assertRaises(Exception): main(["backfill", "--index", str(html), "--dry-run", "--limit", "4", "--base-dir", d])

    def test_backfill_valid_local_index_dry_run(self):
        with tempfile.TemporaryDirectory() as d:
            html = Path(d) / "index.html"
            html.write_text('<a href="pdf/20121001.pdf">2012年10月1日</a>', encoding="utf-8")
            self.assertEqual(main(["backfill", "--index", str(html), "--dry-run", "--limit", "1", "--base-dir", d]), 0)
