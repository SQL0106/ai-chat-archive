#!/usr/bin/env python3
"""选集导出：Markdown 合集 / 单文件 HTML / JSONL。

Markdown 直接复用 out/md/ 里已经生成好的文件（文件名带 conversation_id 前 8 位），
找不到时回退到数据库渲染。HTML 和 JSONL 从数据库生成。
"""
import html
import json
import re
import sys
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
           md_root=None):
    conn = arclib.open_db(db)
    if fmt == "md":
        text, used = render_md(collection, ids, conn, md_root, include_thinking)
    elif fmt == "html":
        text, used = render_html(collection, ids, conn, include_thinking)
    elif fmt == "jsonl":
        text, used = render_jsonl(collection, ids, conn, include_thinking)
    else:
        raise ValueError("未知格式: %s" % fmt)
    if out_path:
        p = Path(out_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    return used


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="导出选集")
    ap.add_argument("--collection", default="default")
    ap.add_argument("--format", choices=["md", "html", "jsonl"], default="md")
    ap.add_argument("-o", "--out")
    ap.add_argument("--with-thinking", action="store_true")
    ap.add_argument("--work")
    ap.add_argument("--db")
    a = ap.parse_args()
    _ids = arclib.selected_ids(a.collection, arclib.selection_path(a.work))
    if not _ids:
        print("选集 [%s] 是空的" % a.collection, file=sys.stderr)
        sys.exit(1)
    _n = export(a.collection, _ids, fmt=a.format, out_path=a.out, db=a.db,
                include_thinking=a.with_thinking)
    if a.out:
        print("已导出 %d 个对话 -> %s" % (_n, a.out), file=sys.stderr)
