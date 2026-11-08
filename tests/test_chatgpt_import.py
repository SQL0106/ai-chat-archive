"""ChatGPT 分片导出及旧版缓存的回归测试（只用合成数据）。"""
import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import archive_ai_chats as archive
import incremental


def conversation(cid):
    return {'id': cid, 'title': cid, 'mapping': {'n': {
        'parent': None, 'message': {'author': {'role': 'user'},
                                  'content': {'parts': ['test']},
                                  'create_time': 1}}}}


class ChatGPTImportTests(unittest.TestCase):
    def setUp(self):
        (ROOT / ".agent").mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.agent')
        self.addCleanup(self.tmp.cleanup)
        self.raw = Path(self.tmp.name) / 'raw'
        self.raw.mkdir()

    def write_zip(self, members):
        path = self.raw / 'export.zip'
        with zipfile.ZipFile(path, 'w') as zf:
            for name, data in members:
                zf.writestr(name, json.dumps(data))
        return path

    def test_partitioned_zip_ignores_shared_metadata(self):
        path = self.write_zip([
            ('shared_conversations.json', [{'id': 'shared-only'}]),
            ('sectioned_conversations.json', []),
            ('export/conversations-001.json', [conversation('second')]),
            ('export/conversations-000.json', [conversation('first')]),
        ])
        self.assertEqual(archive.discover(self.raw)[0], [path])
        self.assertEqual(archive.sniff_conversations_kind(path), 'chatgpt')
        convs = archive.parse_chatgpt(path)
        self.assertEqual([c['conversation_id'] for c in convs], ['first', 'second'])
        self.assertEqual([len(c['messages']) for c in convs], [1, 1])

    def test_shared_metadata_alone_is_not_an_export(self):
        path = self.write_zip([('shared_conversations.json', [{'id': 'shared-only'}])])
        self.assertEqual(archive.discover(self.raw)[0], [])
        with self.assertRaises(SystemExit):
            archive.parse_chatgpt(path)

    def test_legacy_zip(self):
        path = self.write_zip([('conversations.json', [conversation('legacy')])])
        self.assertEqual(archive.parse_chatgpt(path)[0]['conversation_id'], 'legacy')

    def test_deepseek_detection_ignores_shared_metadata(self):
        path = self.write_zip([
            ('shared_conversations.json', [conversation('metadata')]),
            ('conversations.json', [{'id': 'deepseek', 'mapping': {
                'n': {'fragments': [{'type': 'REQUEST', 'content': 'test'}]}}}]),
        ])
        self.assertEqual(archive.sniff_conversations_kind(path), 'deepseek')
        self.assertEqual(archive.discover(self.raw)[4], [path])

    def test_loose_partitions_are_discovered(self):
        for i in range(2):
            (self.raw / ('conversations-%03d.json' % i)).write_text(
                json.dumps([conversation(str(i))]))
        paths = archive.discover(self.raw)[0]
        self.assertEqual(len(paths), 2)
        self.assertEqual(sum(len(archive.parse_chatgpt(p)) for p in paths), 2)

    def test_tar_partitions(self):
        path = self.raw / 'export.tgz'
        with tarfile.open(path, 'w:gz') as tf:
            for i in range(2):
                data = json.dumps([conversation(str(i))]).encode()
                member = tarfile.TarInfo('export/conversations-%03d.json' % i)
                member.size = len(data)
                tf.addfile(member, io.BytesIO(data))
        self.assertEqual(archive.discover(self.raw)[0], [path])
        self.assertEqual(len(archive.parse_chatgpt(path)), 2)

    def test_old_manifest_reimports_then_skips(self):
        path = self.write_zip([('conversations-000.json', [conversation('new')])])
        out = Path(self.tmp.name) / 'out'
        out.mkdir()
        # 旧缓存已有同源对话，文件哈希未变，但新分片此前未导入。
        old = archive.parse_chatgpt(path)[0]
        old['conversation_id'] = 'old'
        archive.write_jsonl(archive.flatten([old]), out / 'normalized.jsonl')
        (out / 'manifest.json').write_text(json.dumps({
            'inputs': {str(path): archive.sha256_file(path)}}))
        args = incremental.build_parser().parse_args([
            '--raw', str(self.raw), '--out', str(out),
            '--work', str(Path(self.tmp.name) / 'work'), '--no-md', '--no-sqlite'])
        runner = incremental.Runner(args.work)
        with patch('builtins.print'):
            first = incremental.run_import(args, runner)
            second = incremental.run_import(args, runner)
        self.assertEqual((first['parsed'], first['added']), (1, 1))
        self.assertEqual((second['parsed'], second['skipped'], second['wrote']), (0, 1, False))


if __name__ == '__main__':
    unittest.main()
