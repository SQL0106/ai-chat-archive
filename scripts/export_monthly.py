#!/usr/bin/env python3
"""按消息的北京时间逐月导出全部聊天正文；仅使用标准库。"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'tools'))
import arclib
from export import all_conversation_ids, text_without_media

TZ = timezone(timedelta(hours=8))


def local_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ)


def export_monthly(db, destination, zip_path=None):
    destination = Path(destination)
    if destination.exists():
        raise FileExistsError('输出目录已存在，请指定一个新目录：%s' % destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    counts = defaultdict(lambda: {'conversations': 0, 'messages': 0,
                                  'fallback_timestamps': 0, 'sources': Counter()})
    total_conversations = total_messages = empty_conversations = 0
    with tempfile.TemporaryDirectory(prefix='.monthly-', dir=destination.parent) as staging:
        stage = Path(staging)
        conn = arclib.open_db(db)
        try:
            conn.execute('BEGIN')
            for cid in all_conversation_ids(conn):
                conv = conn.execute('SELECT * FROM conversations WHERE conversation_id = ?',
                                    (cid,)).fetchone()
                grouped = defaultdict(list)
                fallback = local_time(conv['created_at']) or local_time(conv['updated_at'])
                for msg in conn.execute('SELECT * FROM messages WHERE conversation_id = ?'
                                        ' ORDER BY message_index, id', (cid,)):
                    native = local_time(msg['timestamp'])
                    effective = native or fallback
                    month = effective.strftime('%Y-%m') if effective else '时间未知'
                    grouped[month].append((msg, native, effective))
                total_conversations += 1
                if not grouped:
                    empty_conversations += 1
                for month, messages in grouped.items():
                    target = stage / ('%s.md' % month)
                    fresh = not target.exists()
                    with target.open('a', encoding='utf-8') as out:
                        if fresh:
                            out.write('# %s 聊天记录（仅文字）\n\n'
                                      '按消息时间划分月份，时区为北京时间（UTC+8）。'
                                      '跨月对话仅收录当月消息。\n\n' % month)
                        title = ' '.join((conv['title'] or '(无标题)').splitlines())
                        out.write('## %s\n\n来源：%s · 对话 ID：`%s`\n\n' %
                                  (title, conv['source'], cid))
                        for msg, native, effective in messages:
                            stamp = effective.isoformat(timespec='seconds') if effective else '时间未知'
                            if native is None and effective is not None:
                                stamp += '（消息时间缺失，按对话时间归档）'
                            body = text_without_media(msg['text'] or '')
                            if not body.strip():
                                body = '（此消息无文字正文）'
                            out.write('### %s · %s\n\n%s\n\n' % (msg['role'], stamp, body))
                        out.write('---\n\n')
                    stats = counts[month]
                    stats['conversations'] += 1
                    stats['messages'] += len(messages)
                    stats['sources'][conv['source']] += len(messages)
                    stats['fallback_timestamps'] += sum(native is None for _, native, _ in messages)
                    total_messages += len(messages)
            expected = conn.execute('SELECT COUNT(*) FROM messages').fetchone()[0]
            if total_messages != expected:
                raise ValueError('消息总数不一致：%d != %d' % (total_messages, expected))
        finally:
            conn.close()
        months = []
        for month in sorted(counts):
            path = stage / ('%s.md' % month)
            months.append(dict(month=month, file=path.name,
                               sha256=hashlib.sha256(path.read_bytes()).hexdigest(), **counts[month]))
        manifest = dict(timezone='Asia/Shanghai (UTC+08:00)',
                        grouping='message_timestamp; fallback=conversation_created_at,updated_at',
                        conversations=total_conversations, messages=total_messages,
                        empty_conversations=empty_conversations, months=months)
        (stage / '导出统计.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n',
                                           encoding='utf-8')
        lines = ['# 按月聊天记录（仅文字）', '',
                 '共 %d 个对话，%d 条消息。每月一份 Markdown，包含当前已导入的全部来源。' %
                 (total_conversations, total_messages), '',
                 '按每条消息的北京时间（UTC+8）划分月份。跨月对话会出现在多个月份，'
                 '每份只含当月消息，因此每月对话数之和可能大于总对话数。', '',
                 '消息时间缺失时回退到对话创建时间、更新时间；仍无时间的放入“时间未知”。'
                 '省略思维链、附件字段和媒体引用；无文字正文的消息保留占位。正文不总结、不截断。', '',
                 '| 月份 | 涉及对话 | 消息数 | 时间回退/未知 |',
                 '| --- | ---: | ---: | ---: |']
        for item in months:
            lines.append('| [%s](%s) | %d | %d | %d |' %
                         (item['month'], item['file'], item['conversations'],
                          item['messages'], item['fallback_timestamps']))
        (stage / '目录.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
        os.replace(stage, destination)
    if zip_path is not None:
        zip_path = Path(zip_path)
        zip_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = zip_path.with_name(zip_path.name + '.tmp')
        with zipfile.ZipFile(tmp, 'w', zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            for path in sorted(destination.iterdir()):
                archive.write(path, path.name)
        os.replace(tmp, zip_path)
    return manifest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=ROOT / 'out/archive.sqlite')
    parser.add_argument('--out', type=Path, default=ROOT / 'out/按月导出_仅文字')
    parser.add_argument('--zip', type=Path, dest='zip_path')
    args = parser.parse_args()
    result = export_monthly(args.db, args.out, args.zip_path)
    print(json.dumps(result, ensure_ascii=False, indent=2))
