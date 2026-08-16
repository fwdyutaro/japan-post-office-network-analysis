import io, tempfile, unittest
from contextlib import redirect_stderr
from pathlib import Path
from postal_bias.inukugi import Src06Error, parse_src06_html, write_src06_outputs
from postal_bias.cli import main

TYPE6 = '''<html><title>改廃情報 一時閉鎖 | inukugi web</title><table><tr><th>実施日</th><th>局名</th><th>住所</th><th>備考</th></tr>
<tr><td>2026年1月2日</td><td><a href="/~postal/detail.php?id=1">局A</a></td><td>東京都A</td><td>注</td></tr>
<tr><td>2026年1月2日</td><td><a href="/~postal/detail.php?id=2">局A</a></td><td>東京都B</td><td></td></tr>
<tr><td>2026年2月3日</td><td>局C</td><td>大阪府C</td><td></td></tr></table></html>'''
TYPE7 = TYPE6.replace('改廃情報 一時閉鎖 | inukugi web','改廃情報 再開 | inukugi web').replace('2026年1月2日','2026年3月2日',1).replace('2026年2月3日','2026年3月3日')
CLOSED = '''<html><title>一時閉鎖中の郵便局 | inukugi web</title><table><tr><th>局名</th><th>所在地</th><th>貯</th><th>閉鎖年月日</th><th>閉鎖期間</th><th>備考</th></tr><tr><td>局A</td><td>東京都A</td><td>○</td><td>2025年1月2日</td><td>長期</td><td>注</td></tr><tr><td>局B</td><td>大阪府B</td><td>-</td><td>2026年2月3日</td><td>1年</td><td></td></tr></table></html>'''

class Src06Tests(unittest.TestCase):
    def test_events_and_observations(self):
        a=parse_src06_html(TYPE6.encode(), 'type6', retrieved_at='2026-04-01T00:00:00Z')
        self.assertEqual(len(a['events']),3); self.assertEqual(a['metadata']['qa_error_count'],0)
        self.assertTrue(a['events'][0]['detail_key'].startswith('src06-')); self.assertIsNone(a['events'][0]['confirmed_effective_date']); self.assertNotIn('official_identifier',a['events'][0]['before_state'])
        b=parse_src06_html(TYPE7, 'type7', retrieved_at='2026-04-01T00:00:00Z'); self.assertEqual(len(b['events']),3)
        c=parse_src06_html(CLOSED, 'current_closed', retrieved_at='2026-04-01T00:00:00Z'); self.assertEqual(len(c['current_closed_observations']),2); self.assertFalse(c['events'])
        self.assertEqual(len(a['metadata']['source_content_sha256']), 64)
        self.assertEqual(c['current_closed_observations'][0]['provenance'], 'unofficial')

    def test_fail_closed_header_date_and_href(self):
        with self.assertRaises(Src06Error): parse_src06_html(TYPE6.replace('<title>改廃情報 一時閉鎖 | inukugi web</title>','<title>改廃情報 一時閉鎖 | inukugi webX</title>'), 'type6', retrieved_at='x')
        with self.assertRaises(Src06Error): parse_src06_html(TYPE6.replace('<title>改廃情報 一時閉鎖 | inukugi web</title>','<title>X改廃情報 一時閉鎖 | inukugi web</title>'), 'type6', retrieved_at='x')
        with self.assertRaises(Src06Error): parse_src06_html(TYPE6.replace('</title>','</title><title>改廃情報 一時閉鎖 | inukugi web</title>',1), 'type6', retrieved_at='x')
        bad=TYPE6.replace('2026年1月2日','bad',1); self.assertEqual(len(parse_src06_html(bad,'type6',retrieved_at='x')['events']),2)
        unsafe=TYPE6.replace('/~postal/detail.php?id=1','https://evil.example/x'); r=parse_src06_html(unsafe,'type6',retrieved_at='x'); self.assertTrue(any(q['code']=='unsafe_detail_url' for q in r['qa']))
        with self.assertRaises(Src06Error): parse_src06_html('<table><tr><th>実施日</th></tr></table>','type6',retrieved_at='x')

    def test_malformed_data_row_schema(self):
        short = TYPE6.replace('</table>', '<tr><td>2026年4月1日</td><td>局X</td><td>住所X</td></tr></table>')
        extra = TYPE6.replace('</table>', '<tr><td>2026年4月1日</td><td>局X</td><td>住所X</td><td></td><td>余剰</td></tr></table>')
        empty_name = TYPE6.replace('</table>', '<tr><td>2026年4月1日</td><td></td><td>住所X</td><td></td></tr></table>')
        empty_address = TYPE6.replace('</table>', '<tr><td>2026年4月1日</td><td>局X</td><td></td><td></td></tr></table>')
        for fixture in (short, extra, empty_name, empty_address):
            result = parse_src06_html(fixture, 'type6', retrieved_at='x')
            self.assertTrue(any(q['code'] == 'malformed_data_row' for q in result['qa']))
        closed_short = CLOSED.replace('</table>', '<tr><td>局X</td><td>住所X</td><td>-</td><td>2026年4月1日</td><td>長期</td></tr></table>')
        closed_extra = CLOSED.replace('</table>', '<tr><td>局X</td><td>住所X</td><td>-</td><td>2026年4月1日</td><td>長期</td><td></td><td>余剰</td></tr></table>')
        closed_empty = CLOSED.replace('</table>', '<tr><td></td><td>住所X</td><td>-</td><td>2026年4月1日</td><td>長期</td><td></td></tr></table>')
        closed_empty_address = CLOSED.replace('</table>', '<tr><td>局X</td><td></td><td>-</td><td>2026年4月1日</td><td>長期</td><td></td></tr></table>')
        for fixture in (closed_short, closed_extra, closed_empty, closed_empty_address):
            result = parse_src06_html(fixture, 'current_closed', retrieved_at='x')
            self.assertTrue(any(q['code'] == 'malformed_data_row' for q in result['qa']))

    def test_title_and_cell_script_spoof_and_string_limits(self):
        spoof = '<html><script>改廃情報 一時閉鎖 | inukugi web</script><title>other</title>' + TYPE6[TYPE6.index('<table>'):]
        with self.assertRaises(Src06Error): parse_src06_html(spoof, 'type6', retrieved_at='x')
        polluted = TYPE6.replace('<td>局C</td>', '<td><script>廃止</script>局C</td>')
        self.assertEqual(len(parse_src06_html(polluted, 'type6', retrieved_at='x')['events']), 3)
        with self.assertRaises(Src06Error): parse_src06_html('\x00' + TYPE6, 'type6', retrieved_at='x')
        with self.assertRaises(Src06Error): parse_src06_html('x' * (20 * 1024 * 1024 + 1), 'type6', retrieved_at='x')

    def test_atomic_ack_and_sanitized_cli(self):
        result=parse_src06_html(TYPE6,'type6',retrieved_at='x')
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(Src06Error): write_src06_outputs(result,d)
            paths=write_src06_outputs(result,d,acknowledge_unofficial_source=True,acknowledge_internal_use=True)
            self.assertTrue(Path(paths['events']).exists())
            with self.assertRaises(Src06Error): write_src06_outputs(result,d,acknowledge_unofficial_source=True,acknowledge_internal_use=True)
        err=io.StringIO()
        with redirect_stderr(err): code=main(['parse-src06',r'C:\private\missing.html','--source-kind','type6','--retrieved-at','x','--output-dir',r'C:\private\out'])
        self.assertEqual(code,2); self.assertNotIn('missing.html',err.getvalue()); self.assertNotIn('Traceback',err.getvalue())
