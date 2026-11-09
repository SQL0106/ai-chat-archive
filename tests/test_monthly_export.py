"""按月导出的跨月、时区、时间缺失与完整性回归。"""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from export_monthly import export_monthly


class MonthlyExportTests(unittest.TestCase):
    def test_month_boundary_fallback_and_zip(self):
        with tempfile.TemporaryDirectory(dir=ROOT / '.agent') as tmp:
            root = Path(tmp)
            db = root / 'archive.sqlite'
            with sqlite3.connect(db) as conn:
                conn.executescript('''
                    CREATE TABLE conversations (conversation_id TEXT, title TEXT, source TEXT,
                        created_at TEXT, updated_at TEXT);
                    CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id TEXT,
                        message_index INTEGER, role TEXT, timestamp TEXT, text TEXT,
                        thinking TEXT, attachments TEXT);
                ''')
                conn.executemany('INSERT INTO conversations VALUES (?,?,?,?,?)', [
                    ('cross', '跨月', 'chatgpt', '2026-01-01T00:00:00Z', None),
                    ('unknown', '未知日期', 'deepseek', None, None),
                    ('updated', '只有更新时间', 'deepseek', 'invalid', '2026-03-01'),
                ])
                conn.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)', [
                    (1, 'cross', 0, 'user', '2026-01-31T15:59:59Z', '一月正文', '', ''),
                    (2, 'cross', 1, 'assistant', '2026-01-31T16:00:00Z',
                     '二月正文\n```python\nprint(1)\n``` ![图](secret.png)', '秘密思维链', 'secret'),
                    (3, 'cross', 2, 'user', None, '回退正文', '', ''),
                    (4, 'unknown', 0, 'user', 'invalid', '', '', ''),
                    (5, 'updated', 0, 'user', None, '三月正文', '', ''),
                ])
            output, archive = root / 'monthly', root / 'monthly.zip'
            result = export_monthly(db, output, archive)
            self.assertEqual((result['conversations'], result['messages']), (3, 5))
            stats = {item['month']: item for item in result['months']}
            for month, item in stats.items():
                self.assertEqual(item['file'], month + '.md')
                self.assertIn('](%s.md)' % month, (output / '目录.md').read_text())
            self.assertEqual({key: val['messages'] for key, val in stats.items()},
                             {'2026-01': 2, '2026-02': 1, '2026-03': 1, '时间未知': 1})
            jan = (output / stats['2026-01']['file']).read_text()
            feb = (output / stats['2026-02']['file']).read_text()
            self.assertIn('一月正文', jan)
            self.assertIn('回退正文', jan)
            self.assertNotIn('二月正文', jan)
            self.assertNotIn('一月正文', feb)
            self.assertIn('2026-02-01T00:00:00+08:00', feb)
            self.assertIn('```python\nprint(1)\n```', feb)
            self.assertNotIn('secret', feb)
            self.assertNotIn('秘密思维链', feb)
            self.assertIn('无文字正文', (output / stats['时间未知']['file']).read_text())
            with zipfile.ZipFile(archive) as zipped:
                self.assertIsNone(zipped.testzip())
                for path in output.iterdir():
                    self.assertEqual(zipped.read(path.name), path.read_bytes())
            self.assertEqual(json.loads((output / '导出统计.json').read_text()), result)
            with self.assertRaises(FileExistsError):
                export_monthly(db, output)


if __name__ == '__main__':
    unittest.main()
