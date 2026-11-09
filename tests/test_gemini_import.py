"""Gemini Takeout 问答类型、附件和月度归档回归测试；仅用合成数据。"""
from collections import Counter
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import archive_ai_chats as archive
import incremental
from export_monthly import export_monthly

CID = '0123456789abcdef'


def event(title, timestamp='2026-01-31T16:00:00Z', response='<p>回答 &lt;code&gt;</p>'):
    return dict(header='Gemini Apps', title=title, time=timestamp,
                details=[dict(url='https://gemini.google.com/app/' + CID)],
                safeHtmlItem=[dict(html=response)])


class GeminiImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.agent')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.raw = self.base / 'raw'
        self.raw.mkdir()
        self.path = self.raw / '我的活动记录.json'

    def parse(self, items):
        self.path.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8-sig')
        return archive.parse_gemini_activity_json(self.path)

    def test_all_chat_actions_and_chronological_order(self):
        convs = self.parse([
            event('Added chat from link: 来自链接', '2026-02-04T00:00:00Z'),
            event('Imported prompt 导入问题', '2026-02-03T00:00:00Z'),
            event('Branched 分支', '2026-02-02T00:00:00Z'),
            event('Prompted 原始问题', '2026-02-01T00:00:00Z'),
            event('Gave feedback: Good response'),
            event('Created Gemini Canvas titled demo'),
            dict(event('Prompted 其它产品'), header='AI Mode')])
        self.assertEqual(len(convs), 1)
        conv = convs[0]
        self.assertEqual(conv['conversation_id'], CID)
        self.assertEqual(conv['title'], '原始问题')
        self.assertEqual([m['text'] for m in conv['messages'] if m['role'] == 'user'],
                         ['原始问题', '分支', '导入问题', '来自链接'])
        self.assertEqual(len(conv['messages']), 8)
        self.assertEqual(conv['messages'][1]['text'], '回答 <code>')

    def test_media_only_messages_remain_in_archive(self):
        item = event('Prompted \n', response='<img src="image.png">')
        item['attachedFiles'] = ['附件.pdf']
        conv = self.parse([item])[0]
        self.assertEqual(len(conv['messages']), 2)
        self.assertEqual(conv['messages'][0]['attachments'], ['附件.pdf'])
        self.assertEqual(conv['messages'][1]['attachments'], ['image.png'])
        self.assertEqual(conv['messages'][1]['text'], '')

    def test_table_columns_and_code_are_separated(self):
        html = '<table><tr><th>名称</th><th>值</th></tr><tr><td>甲</td><td>1</td></tr></table><pre>x &lt; 2</pre>'
        text = self.parse([event('Prompted 表格', response=html)])[0]['messages'][1]['text']
        self.assertIn('名称\t值', text)
        self.assertIn('甲\t1', text)
        self.assertIn('x < 2', text)

    def test_takeout_discovery_does_not_import_uploaded_conversations(self):
        self.parse([event('Prompted 问题')])
        (self.raw / 'conversations-abcdef0123456789.json').write_text('[]')
        groups = archive.discover(self.raw)
        self.assertEqual(groups[0], [])
        self.assertEqual(groups[4], [])
        self.assertEqual(groups[3], [self.path])
        self.assertFalse(archive.is_activity_json('Takeout/AI Mode/我的活动记录.json'))
        zip_path = self.raw / 'takeout.zip'
        with zipfile.ZipFile(zip_path, 'w') as zf:
            zf.write(self.path, 'Takeout/我的活动/Gemini Apps/我的活动记录.json')
            zf.writestr('Takeout/我的活动/Gemini Apps/conversations-abcdef0123456789.json', '[]')
        convs = archive.parse_gemini_activity_file(zip_path)
        self.assertEqual(len(convs), 1)

    def test_old_parser_cache_reimports_and_month_boundary_is_local(self):
        self.parse([event('Prompted 一月', '2026-01-31T15:59:59Z'),
                    event('Imported prompt 二月', '2026-01-31T16:00:00Z')])
        out = self.base / 'out'
        out.mkdir()
        old = archive.parse_gemini_activity_json(self.path)[0]
        old['messages'] = old['messages'][:2]
        archive.write_jsonl(archive.flatten([old]), out / 'normalized.jsonl')
        (out / 'manifest.json').write_text(json.dumps({
            'inputs': {str(self.path): archive.sha256_file(self.path)},
            'parser_versions': {'chatgpt': 2}}))
        args = incremental.build_parser().parse_args([
            '--raw', str(self.raw), '--out', str(out), '--work', str(self.base / 'work'), '--no-md'])
        with patch('builtins.print'):
            first = incremental.run_import(args, incremental.Runner(args.work))
            second = incremental.run_import(args, incremental.Runner(args.work))
        self.assertEqual((first['parsed'], first['changed'], first['messages']), (1, 1, 4))
        self.assertEqual((second['parsed'], second['skipped'], second['wrote']), (0, 1, False))
        manifest = export_monthly(out / 'archive.sqlite', self.base / 'monthly')
        self.assertEqual({m['month']: m['messages'] for m in manifest['months']},
                         {'2026-01': 2, '2026-02': 2})
        self.assertEqual(Counter({m['month']: m['sources']['gemini'] for m in manifest['months']}),
                         Counter({'2026-01': 2, '2026-02': 2}))


if __name__ == '__main__':
    unittest.main()
