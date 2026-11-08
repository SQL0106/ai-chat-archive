"""全部聊天纯文字合并导出：排序、正文完整性与接口回归。"""
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'tools'), str(ROOT / 'scripts')]
import export
import serve_archive


class TextExportTests(unittest.TestCase):
    def setUp(self):
        (ROOT / '.agent').mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / '.agent')
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'archive.sqlite'
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        self.addCleanup(self.conn.close)
        self.conn.executescript('''
            CREATE TABLE conversations (conversation_id TEXT, title TEXT, source TEXT,
                created_at TEXT, updated_at TEXT, message_count INTEGER);
            CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id TEXT,
                message_index INTEGER, role TEXT, timestamp TEXT, text TEXT,
                thinking TEXT, attachments TEXT);
        ''')
        self.conn.executemany('INSERT INTO conversations VALUES (?,?,?,?,?,?)', [
            ('new', '较新对话', 'chatgpt', '2026-02-01', None, 2),
            ('old', '较早对话', 'deepseek', '2026-01-01', None, 1),
        ])
        self.conn.executemany('INSERT INTO messages VALUES (?,?,?,?,?,?,?,?)', [
            (1, 'new', 1, 'assistant', None, '完整回答\n```python\nprint(1)\n```', '秘密思维链', '["secret.png"]'),
            (2, 'new', 0, 'user', None, '问题 ![图片](asset://image.png) <img src="secret.png">', '', '[]'),
            (3, 'old', 0, 'user', None, '早期正文', '', '[]'),
        ])
        self.conn.commit()

    def test_all_order_and_text_only(self):
        ids = export.all_conversation_ids(self.conn)
        self.assertEqual(ids, ['old', 'new'])
        text, used = export.render_text_md('全部聊天记录', ids, self.conn)
        self.assertEqual(used, 2)
        self.assertLess(text.index('早期正文'), text.index('问题'))
        self.assertLess(text.index('问题'), text.index('完整回答'))
        self.assertIn('```python\nprint(1)\n```', text)
        for unwanted in ['secret.png', '秘密思维链', 'asset://', '<img']:
            self.assertNotIn(unwanted, text)

    def test_export_all_without_selection_or_markdown_cache(self):
        target = Path(self.tmp.name) / 'merged.md'
        with patch.object(export.arclib, 'selected_ids', side_effect=AssertionError), \
                patch.object(export, 'index_md_files', side_effect=AssertionError):
            used = export.export('unused', [], db=self.db, out_path=target,
                                 all_chats=True, text_only=True)
        self.assertEqual(used, 2)
        self.assertIn('# 全部聊天记录', target.read_text())
        self.assertFalse(target.with_name('merged.md.tmp').exists())

    def test_web_all_bypasses_empty_selection(self):
        with patch.object(serve_archive.arclib, 'selected_ids', return_value=[]):
            data, status = serve_archive.api_export(self.conn, {
                'scope': ['all'], 'text_only': ['1'], 'format': ['md']})
        self.assertEqual(status, 200)
        self.assertEqual(data['used'], 2)
        self.assertEqual(data['filename'], 'all-chats.md')
        self.assertNotIn('秘密思维链', data['text'])

    def test_reject_text_only_html(self):
        with self.assertRaises(ValueError):
            export.export('', [], fmt='html', text_only=True, db=self.db)
        _, status = serve_archive.api_export(self.conn, {
            'scope': ['all'], 'text_only': ['1'], 'format': ['html']})
        self.assertEqual(status, 400)

    def test_media_cleanup_keeps_links_and_words(self):
        text = '正文 [链接](https://example.com) ![图](file(a).png) ![图][ref]'
        self.assertEqual(export.text_without_media(text), '正文 [链接](https://example.com)  ')

    def add_message(self, cid, index, timestamp, text):
        self.conn.execute(
            'INSERT INTO messages (conversation_id, message_index, role, timestamp, text)'
            ' VALUES (?, ?, ?, ?, ?)', (cid, index, 'user', timestamp, text))
        self.conn.commit()

    def test_month_filters_messages_not_conversation_creation(self):
        self.add_message('old', 1, '2026-01-31T15:59:59Z', '上月消息')
        self.add_message('old', 2, '2026-01-31T16:00:00Z', '月初消息')
        self.add_message('old', 3, '2026-02-28T15:59:59Z', '月末消息')
        self.add_message('old', 4, '2026-02-28T16:00:00Z', '下月消息')
        period = export.export_period(month='2026-02')
        ids = export.all_conversation_ids(self.conn, period)
        text, used = export.render_text_md('月合集', ids, self.conn, period)
        self.assertEqual(used, 2)
        for included in ['月初消息', '月末消息', '完整回答']:
            self.assertIn(included, text)
        for excluded in ['上月消息', '下月消息', '早期正文']:
            self.assertNotIn(excluded, text)

    def test_week_is_monday_to_sunday(self):
        self.add_message('old', 1, '2026-02-01T15:59:59Z', '周日前消息')
        self.add_message('old', 2, '2026-02-01T16:00:00Z', '周一消息')
        self.add_message('old', 3, '2026-02-08T15:59:59Z', '周日消息')
        self.add_message('old', 4, '2026-02-08T16:00:00Z', '下周消息')
        period = export.export_period(week='2026-W06')
        text, used = export.render_text_md('周合集', export.all_conversation_ids(
            self.conn, period), self.conn, period)
        self.assertEqual(used, 1)
        self.assertIn('周一消息', text)
        self.assertIn('周日消息', text)
        self.assertNotIn('周日前消息', text)
        self.assertNotIn('下周消息', text)

    def test_iso_week_year_and_leap_month(self):
        period = export.export_period(week='2020-W53')
        self.assertEqual(period['start'], '2020-12-27T16:00:00+00:00')
        self.assertEqual(period['end'], '2021-01-03T16:00:00+00:00')
        period = export.export_period(month='2024-02')
        self.assertEqual(period['end'], '2024-02-29T16:00:00+00:00')
        with self.assertRaises(ValueError):
            export.export_period(week='2021-W53')

    def test_missing_message_time_falls_back_to_conversation_time(self):
        self.conn.execute('UPDATE conversations SET created_at=NULL, updated_at=?'
                          ' WHERE conversation_id=?', ('2026-02-10', 'old'))
        self.conn.commit()
        period = export.export_period(month='2026-02')
        text, _ = export.render_text_md('月合集', ['old'], self.conn, period)
        self.assertIn('早期正文', text)
        self.conn.execute('UPDATE conversations SET updated_at=NULL WHERE conversation_id=?', ('old',))
        text, used = export.render_text_md('月合集', ['old'], self.conn, period)
        self.assertEqual(used, 0)
        self.assertNotIn('早期正文', text)

    def test_web_month_and_configured_timezone(self):
        self.add_message('old', 1, '2026-01-31T16:00:00Z', '时区边界')
        params = {'scope': ['all'], 'month': ['2026-02']}
        with patch.object(serve_archive, 'TZ_MOD', '+8 hours'):
            data, status = serve_archive.api_export(self.conn, params)
        self.assertEqual(status, 200)
        self.assertEqual(data['filename'], 'chats-2026-02.md')
        self.assertIn('时区边界', data['text'])
        with patch.object(serve_archive, 'TZ_MOD', '+0 hours'):
            data, status = serve_archive.api_export(self.conn, params)
        self.assertEqual(status, 200)
        self.assertNotIn('时区边界', data['text'])

    def test_invalid_and_empty_periods(self):
        for invalid in [{'month': ['2026-13']}, {'week': ['2026-W54']},
                        {'month': ['2026-2']}, {'week': ['invalid']},
                        {'month': ['2026-02'], 'week': ['2026-W06']},
                        {'month': ['2026-02'], 'format': ['html']}]:
            _, status = serve_archive.api_export(self.conn, {'scope': ['all'], **invalid})
            self.assertEqual(status, 400, invalid)
        _, status = serve_archive.api_export(self.conn, {'month': ['2026-02']})
        self.assertEqual(status, 400)
        _, status = serve_archive.api_export(self.conn, {
            'scope': ['all'], 'month': ['2020-01']})
        self.assertEqual(status, 404)
        target = Path(self.tmp.name) / 'existing.md'
        target.write_text('原有文件', encoding='utf-8')
        with self.assertRaises(ValueError):
            export.export('', [], db=self.db, out_path=target, month='2020-01')
        self.assertEqual(target.read_text(encoding='utf-8'), '原有文件')

    def test_cli_entrypoints_accept_period_without_selection(self):
        for entry, subcommand in [('export.py', []), ('archive_cli.py', ['export'])]:
            target = Path(self.tmp.name) / (entry + '.md')
            result = subprocess.run([
                sys.executable, str(ROOT / 'tools' / entry), '--db', str(self.db),
                *subcommand, '--month', '2026-02', '-o', str(target)],
                capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('完整回答', target.read_text(encoding='utf-8'))
            self.assertNotIn('早期正文', target.read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
