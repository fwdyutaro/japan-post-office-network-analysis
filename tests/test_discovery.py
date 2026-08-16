import unittest
from postal_bias.discovery import discover_index


class DiscoveryTests(unittest.TestCase):
    def test_current_and_2012_documents_are_classified_and_deduped(self):
        html = '''<a href="pdf/ichiran.pdf">2026年6月30日現在</a>
        <a href="pdf/20121001.pdf">2012年10月 変更予定等</a>
        <a href="pdf/20121015.pdf">2012年10月15日</a>
        <a href="pdf/20121001.pdf">重複</a>'''
        docs = discover_index(html)
        self.assertEqual([d.kind for d in docs], ["current_list", "monthly_change", "monthly_change"])
        self.assertEqual(docs[1].date, "2012-10-01")
        self.assertEqual(len(docs), 3)
