#!/usr/bin/env python3
"""选集或全部聊天导出：Markdown 合集 / 单文件 HTML / JSONL。

Markdown 直接复用 out/md/ 里已经生成好的文件（文件名带 conversation_id 前 8 位），
找不到时回退到数据库渲染。HTML 和 JSONL 从数据库生成。
--all --text-only 从数据库合并全部文字正文，排除思维链与媒体引用。
"""
import html
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import arclib  # noqa: E402

MD_ROOT = ROOT / "out" / "md"
FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)
HEADING_RE = re.compile(r"^(#{1,6}) ", re.MULTILINE)

HTML_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; background: #0e1116; color: #e7eaf0;
  font: 15px/1.7 -apple-system, "Segoe UI", "Noto Sans CJK SC", sans-serif; }
.wrap { max-width: 860px; margin: 0 auto; padding: 32px 20px 120px; }
h1 { font-size: 26px; border-bottom: 1px solid #2a323d; padding-bottom: 12px; }
h2 { font-size: 20px; margin-top: 40px; padding-top: 16px; border-top: 1px solid #2a323d; }
h3 { font-size: 16px; margin: 22px 0 6px; color: #c3cbd8; }
.meta { color: #8b95a5; font-size: 12px; }
.toc a { color: #9dc4ff; text-decoration: none; }
.toc li { margin: 3px 0; }
.src { display: inline-block; padding: 1px 7px; border-radius: 999px; font-size: 11px; }
.src.chatgpt { background: #1d3f34; color: #86efc4; }
.src.deepseek { background: #33294f; color: #c4b5fd; }
.src.gemini { background: #22344f; color: #9dc4ff; }
.msg { white-space: pre-wrap; overflow-wrap: anywhere; margin: 6px 0 0; }
.msg.user { border-left: 3px solid #4c9aff; padding-left: 12px; }
.msg.assistant { border-left: 3px solid #6ee7b7; padding-left: 12px; }
details { margin: 10px 0; color: #8b95a5; }
details pre { white-space: pre-wrap; overflow-wrap: anywhere; font-size: 13px; }
code { background: #1c232d; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
pre code { display: block; padding: 10px; overflow-x: auto; }
.top { position: fixed; right: 18px; bottom: 18px; background: #232c38;
  color: #e7eaf0; border: 1px solid #38424f; border-radius: 8px;
  padding: 6px 12px; text-decoration: none; font-size: 13px; }
"""


def index_md_files(md_root=None):
    """扫描 out/md/<source>/*.md，返回 {conversation_id前8位: Path}。"""
    root = Path(md_root) if md_root else MD_ROOT
    idx = {}
    if not root.exists():
        return idx
    for src_dir in root.iterdir():
        if not src_dir.is_dir():
            continue
        for f in src_dir.glob("*.md"):
            stem = f.stem
            if len(stem) >= 8:
                idx.setdefault(stem[-8:], f)
    return idx


def find_md(cid, idx):
    return idx.get(str(cid)[:8])


def demote_headings(text):
    return HEADING_RE.sub(lambda m: "#" + m.group(1) + " ", text)


def _md_body(md_path):
    txt = md_path.read_text(encoding="utf-8")
    return demote_headings(FRONTMATTER_RE.sub("", txt, count=1)).strip()


def _md_fallback(conn, cid):
    d = arclib.get_conversation(conn, cid)
    if not d:
        return None
    c = d["conversation"]
    lines = ["## %s" % (c["title"] or "(无标题)"), "",
             "> %s · %d 条 · `%s`" % (c["source"], c["message_count"], cid), ""]
    for m in d["messages"]:
        lines.append("### [%d] %s — %s" % (m["message_index"], m["role"], m["timestamp"] or "?"))
        lines.append("")
        lines.append(m["text"] or "_(空)_")
        if m.get("thinking"):
            lines += ["", "<details><summary>thinking</summary>", "", m["thinking"], "", "</details>"]
        lines.append("")
    return "\n".join(lines)


PERIOD_TIMESTAMP = "COALESCE(NULLIF(m.timestamp, ''), NULLIF(c.created_at, ''), NULLIF(c.updated_at, ''))"


def export_period(month=None, week=None, tz_offset=8):
    if month and week:
        raise ValueError("月份与周不能同时指定")
    if not month and not week:
        return None
    tz = timezone(timedelta(hours=tz_offset))
    if month:
        if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", month):
            raise ValueError("月份格式应为 YYYY-MM")
        year, number = map(int, month.split("-"))
        start = datetime(year, number, 1, tzinfo=tz)
        end = datetime(year + (number == 12), number % 12 + 1, 1, tzinfo=tz)
        label = month
    else:
        if not re.fullmatch(r"[0-9]{4}-W[0-9]{2}", week):
            raise ValueError("周格式应为 YYYY-Www（ISO 周，周一至周日）")
        year, number = map(int, week.split("-W"))
        monday = date.fromisocalendar(year, number, 1)
        start = datetime.combine(monday, datetime.min.time(), tzinfo=tz)
        end = start + timedelta(days=7)
        label = week
    return {"label": label, "start": start.astimezone(timezone.utc).isoformat(),
            "end": end.astimezone(timezone.utc).isoformat(),
            "description": "%s 至 %s（不含结束时间，UTC%+g）" % (
                start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d"), tz_offset)}


def period_filter(period):
    if not period:
        return "", []
    return (" AND julianday(%s) >= julianday(?) AND julianday(%s) < julianday(?)"
            % (PERIOD_TIMESTAMP, PERIOD_TIMESTAMP), [period["start"], period["end"]])


def all_conversation_ids(conn, period=None):
    clause, params = period_filter(period)
    where = (" WHERE EXISTS (SELECT 1 FROM messages m"
             " WHERE m.conversation_id = c.conversation_id" + clause + ")") if period else ""
    return [row[0] for row in conn.execute(
        "SELECT c.conversation_id FROM conversations c" + where +
        " ORDER BY COALESCE(c.created_at, c.updated_at, ''), c.source, c.conversation_id", params)]


def text_without_media(text):
    text = re.sub(r'!\[(?:\\.|[^\]\\])*\]\((?:\\.|[^()\\]|\([^()]*\))*\)', '', text)
    text = re.sub(r'!\[(?:\\.|[^\]\\])*\](?:\[[^\]]*\])?', '', text)
    text = re.sub(r'<(video|audio|picture)\b[^>]*>.*?</\1\s*>', '', text,
                  flags=re.IGNORECASE | re.DOTALL)
    return re.sub(r'<(?:img|source)\b[^>]*>', '', text, flags=re.IGNORECASE)


def render_text_md(collection, ids, conn, period=None):
    parts = ["# %s" % collection, ""]
    if period:
        parts.extend([period["description"], "", "仅包含该时段的消息；无消息时间时回退对话创建/更新时间。", ""])
    sections = []
    used = 0
    clause, params = period_filter(period)
    for cid in ids:
        conv = conn.execute(
            "SELECT title, source, created_at FROM conversations WHERE conversation_id = ?",
            (cid,)).fetchone()
        if conv is None:
            continue
        title = " ".join((conv["title"] or "(无标题)").splitlines())
        messages = []
        for message in conn.execute(
                "SELECT m.role, m.timestamp, m.text FROM messages m"
                " JOIN conversations c ON c.conversation_id = m.conversation_id"
                " WHERE m.conversation_id = ?" + clause +
                " ORDER BY m.message_index, m.id", [cid] + params):
            text = text_without_media(message["text"] or "")
            if not text.strip():
                continue
            messages.extend(["### %s · %s" % (message["role"], message["timestamp"] or "时间未知"),
                             "", text, ""])
        if period and not messages:
            continue
        sections.extend(["## %s" % title, "",
                         "%s · %s" % (conv["source"], conv["created_at"] or "时间未知"), ""])
        sections.extend(messages)
        sections.extend(["---", ""])
        used += 1
    parts.extend(["%d 个对话" % used, ""])
    parts.extend(sections)
    return "\n".join(parts), used


def render_md(collection, ids, conn, md_root=None, include_thinking=False):
    idx = index_md_files(md_root)
    by_id = {c["conversation_id"]: c for c in arclib.list_conversations(conn, ids)}
    parts = ["# 选集：%s" % collection, "",
             "%d 个对话" % len(ids), "", "## 目录", ""]
    for n, cid in enumerate(ids, 1):
        conv = by_id.get(cid)
        title = (conv["title"] if conv else "") or "(无标题)"
        src = conv["source"] if conv else "?"
        parts.append("%d. %s  <sub>%s</sub>" % (n, title, src))
    parts += ["", "---", ""]
    used = 0
    for cid in ids:
        md = find_md(cid, idx)
        body = _md_body(md) if md else _md_fallback(conn, cid)
        if not body:
            continue
        if not include_thinking:
            body = re.sub(r"\n<details><summary>thinking</summary>.*?</details>\n",
                          "\n_(思维链已省略)_\n", body, flags=re.DOTALL)
        parts += [body, "", "---", ""]
        used += 1
    return "\n".join(parts), used


def render_html(collection, ids, conn, include_thinking=False):
    convs = arclib.list_conversations(conn, ids)
    by_id = {c["conversation_id"]: c for c in convs}
    out = ["<!doctype html>", '<html lang="zh"><head><meta charset="utf-8">',
           '<meta name="viewport" content="width=device-width,initial-scale=1">',
           "<title>选集：%s</title><style>%s</style></head><body>" % (
               html.escape(collection), HTML_CSS),
           '<div class="wrap">', "<h1>选集：%s</h1>" % html.escape(collection),
           '<p class="meta">%d 个对话</p>' % len(ids), '<ol class="toc">']
    for n, cid in enumerate(ids, 1):
        c = by_id.get(cid)
        title = (c["title"] if c else "") or "(无标题)"
        src = c["source"] if c else "?"
        out.append('<li><a href="#c%d">%s</a> <span class="src %s">%s</span></li>' % (
            n, html.escape(title), html.escape(src), html.escape(src)))
    out.append("</ol>")
    used = 0
    for n, cid in enumerate(ids, 1):
        d = arclib.get_conversation(conn, cid)
        if not d:
            continue
        c = d["conversation"]
        out.append('<h2 id="c%d">%s <span class="src %s">%s</span></h2>' % (
            n, html.escape(c["title"] or "(无标题)"), html.escape(c["source"]),
            html.escape(c["source"])))
        out.append('<p class="meta">%s · %d 条 · %s → %s</p>' % (
            html.escape(cid), c["message_count"],
            html.escape(c["created_at"] or "?"), html.escape(c["updated_at"] or "?")))
        for m in d["messages"]:
            out.append('<h3>[%d] %s — %s</h3>' % (
                m["message_index"], html.escape(m["role"]), html.escape(m["timestamp"] or "?")))
            if m.get("model"):
                out.append('<p class="meta">model: <code>%s</code></p>' % html.escape(m["model"]))
            out.append('<div class="msg %s">%s</div>' % (
                html.escape(m["role"]), html.escape(m["text"] or "_(空)_")))
            if m.get("thinking") and include_thinking:
                out.append("<details><summary>thinking · %d 字符</summary><pre>%s</pre></details>" % (
                    len(m["thinking"]), html.escape(m["thinking"])))
            if m.get("attachments"):
                out.append('<p class="meta">attachments: %s</p>' % html.escape(
                    ", ".join(str(a) for a in m["attachments"])))
        used += 1
    out += ["</div>", '<a class="top" href="#">↑</a>', "</body></html>"]
    return "\n".join(out), used


def render_jsonl(collection, ids, conn, include_thinking=False):
    lines = []
    used = 0
    for cid in ids:
        d = arclib.get_conversation(conn, cid)
        if not d:
            continue
        for m in d["messages"]:
            if not include_thinking:
                m.pop("thinking", None)
        lines.append(json.dumps({"collection": collection,
                                 "conversation": d["conversation"],
                                 "messages": d["messages"]}, ensure_ascii=False))
        used += 1
    return "\n".join(lines) + ("\n" if lines else ""), used


def export(collection, ids, fmt="md", out_path=None, db=None, include_thinking=False,
           md_root=None, all_chats=False, text_only=False, month=None, week=None):
    period = export_period(month, week, arclib.local_tz_offset())
    if period:
        if fmt != "md":
            raise ValueError("按月/周导出只支持 Markdown")
        all_chats = text_only = True
    if text_only and fmt != "md":
        raise ValueError("仅文字模式只支持 Markdown")
    conn = arclib.open_db(db)
    try:
        if all_chats:
            ids = all_conversation_ids(conn, period)
            collection = "全部聊天记录"
            if period:
                collection = "聊天记录 · %s" % period["label"]
        if text_only:
            text, used = render_text_md(collection, ids, conn, period)
            if period and used == 0:
                raise ValueError("所选时段没有可导出的文字消息")
        elif fmt == "md":
            text, used = render_md(collection, ids, conn, md_root, include_thinking)
        elif fmt == "html":
            text, used = render_html(collection, ids, conn, include_thinking)
        elif fmt == "jsonl":
            text, used = render_jsonl(collection, ids, conn, include_thinking)
        else:
            raise ValueError("未知格式: %s" % fmt)
    finally:
        conn.close()
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    else:
        sys.stdout.write(text)
    return used


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="导出选集或全部聊天记录")
    ap.add_argument("--collection", default="default")
    ap.add_argument("--format", choices=["md", "html", "jsonl"], default="md")
    ap.add_argument("-o", "--out")
    ap.add_argument("--with-thinking", action="store_true")
    ap.add_argument("--all", action="store_true", help="导出全部来源的对话，按时间升序")
    ap.add_argument("--text-only", action="store_true", help="Markdown 只保留正文和标题/角色/时间，不含附件信息或思维链")
    period_args = ap.add_mutually_exclusive_group()
    period_args.add_argument("--month", metavar="YYYY-MM", help="导出所选月份的消息（UTC+8，仅文字 MD）")
    period_args.add_argument("--week", metavar="YYYY-Www", help="导出 ISO 周的消息（周一至周日，UTC+8，仅文字 MD）")
    ap.add_argument("--work")
    ap.add_argument("--db")
    a = ap.parse_args()
    if (a.text_only or a.month or a.week) and a.format != "md":
        ap.error("仅文字和按月/周导出只支持 --format md")
    _all = a.all or bool(a.month or a.week)
    _ids = [] if _all else arclib.selected_ids(a.collection, arclib.selection_path(a.work))
    if not _ids and not _all:
        print("选集 [%s] 是空的" % a.collection, file=sys.stderr)
        sys.exit(1)
    try:
        _n = export(a.collection, _ids, fmt=a.format, out_path=a.out, db=a.db,
                    include_thinking=a.with_thinking, all_chats=_all, text_only=a.text_only,
                    month=a.month, week=a.week)
    except (ValueError, OverflowError) as error:
        ap.error(str(error))
    if a.out:
        print("已导出 %d 个对话 -> %s" % (_n, a.out), file=sys.stderr)
