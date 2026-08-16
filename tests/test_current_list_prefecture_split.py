"""The overlapping-prefecture name/address split, fixed in parser 0.2.0.

東京都府中市 contains 京都府 one character in.  Taking the rightmost prefecture
match put the name/address boundary one character late for the 府中市 offices:
the name kept a trailing 東 and the address began 京都府中市, which relocates the
office from Tokyo to Kyoto for every downstream join.  These cases are parsed
correctly now, so no post-parse repair pass is needed.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from postal_bias.current_list import PARSER_VERSION, _find_address_start, parse_layout_text


#: Depopulated-area cell then the five service cells, at the fixed offsets the
#: parser derives from the table header (kaso at anchor-4; services at
#: anchor+0/+6/+12/+17/+27).
MARKS = "○" + " " * 3 + "○" + " " * 5 + "○" + " " * 5 + "○" + " " * 4 + "○" + " " * 9 + "○"


def row(serial, ident, name, address, *, status="営業中"):
    return f"{serial:>4} {ident:<7} {name:<20}  {address:<24}  {MARKS}{'':<12}{status}"


FUCHU = [("001340", "府中八幡宿郵便局", "東京都府中市八幡町１－１３－１"),
         ("003870", "府中美好郵便局", "東京都府中市美好町２－１２－５"),
         ("004350", "府中北山郵便局", "東京都府中市北山町２－８－１１")]


class PrefectureSplitTests(unittest.TestCase):
    def test_parser_version_matches_the_fixed_behaviour(self):
        self.assertEqual(PARSER_VERSION, "0.2.0")

    def test_address_start_is_the_outer_prefecture_match(self):
        body = "府中八幡宿郵便局    東京都府中市八幡町１－１３－１"
        self.assertEqual(body[_find_address_start(body):], "東京都府中市八幡町１－１３－１")

    def test_other_overlapping_prefecture_names_are_unaffected(self):
        for body, expected in (("京都中央郵便局   京都府京都市下京区", "京都府京都市下京区"),
                               ("大阪府中央郵便局   大阪府大阪市北区", "大阪府大阪市北区"),
                               ("石川県立中央病院内簡易郵便局  石川県金沢市鞍月東", "石川県金沢市鞍月東")):
            self.assertEqual(body[_find_address_start(body):], expected, body)

    def test_fuchu_rows_parse_without_any_repair_pass(self):
        text = ("別記様式第一号（郵便局）\n通番 整理番号 名称 所在地\n"
                + "\n".join(row(i, ident, name, address)
                            for i, (ident, name, address) in enumerate(FUCHU, 1))
                + "\n注　長期に営業を休止している簡易郵便局が653局ある。\n")
        result = parse_layout_text(text)
        self.assertEqual(len(result["records"]), len(FUCHU))
        for record, (ident, name, address) in zip(result["records"], FUCHU):
            self.assertEqual(record["official_identifier"], ident)
            self.assertEqual(record["name"]["normalized"], name)
            self.assertEqual(record["address"]["normalized"], address)
            self.assertFalse(record["address"]["normalized"].startswith("京都府中市"))
        self.assertNotIn("name_column_boundary_suspect", {q["code"] for q in result["qa"]})
        self.assertEqual(result["metadata"]["qa_error_count"], 0)
        self.assertEqual(result["metadata"]["parser_version"], "0.2.0")

    def test_a_late_boundary_would_still_be_caught_as_a_qa_error(self):
        # Guard the guard: the suspect-name check must fire if a split ever
        # regresses, rather than letting the record through silently.
        text = ("別記様式第一号（郵便局）\n通番 整理番号 名称 所在地\n"
                + row(1, "001340", "府中八幡宿郵便局 東", "京都府中市八幡町１－１３－１") + "\n")
        result = parse_layout_text(text)
        self.assertIn("name_column_boundary_suspect", {q["code"] for q in result["qa"]})


if __name__ == "__main__":
    unittest.main()
