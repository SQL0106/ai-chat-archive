#!/usr/bin/env python3
"""archive_ai_chats.py — 把 ChatGPT / Gemini 历史聊天记录统一归档。

用法:
  python3 archive_ai_chats.py --raw raw --out out [--dry-run] [--no-sqlite] [--no-md]

输入 (raw/ 下, 自动发现):
  conversations.json     ChatGPT 官方导出 ZIP 里的 conversations.json (也可直接放 .zip)
  我的活动记录.html      Google Takeout -> My Activity -> Gemini Apps (HTML, 含提问+回答+时间)
  MyActivity.json        Google Takeout -> My Activity -> Gemini Apps (JSON, 仅时间线)
  gemini_chats.ndjson    油猴脚本导出的 Gemini 全文 (可选, 无时间戳, 用 Takeout 回填)
  *.tgz / *.zip          Takeout 整包, 自动在内部查找上述文件

输出 (out/ 下):
  normalized.jsonl       统一 schema 的逐条消息 (UTC ISO 时间)
  md/chatgpt/*.md        人类可读副本 (本地时区)
  md/gemini/*.md
  archive.sqlite         含 FTS5 全文索引
  manifest.json          统计 / 回填率 / 未匹配清单 / 输入校验和

仅使用 Python 3 标准库, 无需 pip 安装。
"""

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
import time
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from difflib import get_close_matches
from html import unescape as html_unescape
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def to_iso(value):
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    try:
        return datetime.fromtimestamp(float(s), tz=timezone.utc).isoformat()
    except ValueError:
        pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def local_str(iso, tz):
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if tz is not None:
        dt = dt.astimezone(tz)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def norm_text(s):
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = s.replace("\u200b", "").replace("\ufeff", "")
    s = re.sub(r"\s+", " ", s)
    return s.strip().lower()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_json_any(path):
    path = Path(path)
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            names = [n for n in zf.namelist() if n.endswith("conversations.json")]
            if not names:
                raise SystemExit("错误: ZIP 内找不到 conversations.json: %s" % path)
            names.sort(key=len)
            with zf.open(names[0]) as fh:
                return json.loads(fh.read().decode("utf-8-sig"))
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except json.JSONDecodeError as e:
        raise SystemExit("错误: JSON 解析失败 %s: %s" % (path, e))
    except UnicodeDecodeError as e:
        raise SystemExit("错误: 文件编码异常 %s: %s" % (path, e))


def extract_parts(content):
    texts, attachments = [], []
    if isinstance(content, str):
        return content, attachments
    if not isinstance(content, dict):
        return "", attachments
    parts = content.get("parts")
    if parts is None:
        t = content.get("text")
        parts = [t] if t else []
    if not isinstance(parts, list):
        parts = [parts]
    for p in parts:
        if isinstance(p, str):
            texts.append(p)
        elif isinstance(p, dict):
            ct = p.get("content_type")
            if ct in ("image_asset_pointer", "audio_asset_pointer", "video_asset_pointer"):
                attachments.append(p.get("asset_pointer") or ct)
            elif "text" in p:
                texts.append(str(p["text"]))
            elif ct:
                attachments.append(ct)
            else:
                attachments.append("object")
        elif p is not None:
            texts.append(str(p))
    return "\n".join(t for t in texts if t), attachments


def parse_chatgpt(path):
    data = load_json_any(path)
    if isinstance(data, dict):
        data = data.get("conversations") or data.get("data") or []
    if not isinstance(data, list):
        raise SystemExit("错误: ChatGPT 导出格式无法识别 (期望顶层为数组): %s" % path)

    conversations = []
    for c in data:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or c.get("conversation_id") or "")
        title = str(c.get("title") or "(无标题)")
        mapping = c.get("mapping") or {}
        if not isinstance(mapping, dict):
            mapping = {}

        children, roots = {}, []
        for nid, node in mapping.items():
            if not isinstance(node, dict):
                continue
            parent = node.get("parent")
            if parent is None:
                roots.append(nid)
            else:
                children.setdefault(parent, []).append(nid)

        order, seq = {}, 0
        queue = list(roots) if roots else list(mapping.keys())
        while queue:
            nid = queue.pop(0)
            if nid in order:
                continue
            order[nid] = seq
            seq += 1
            for ch in children.get(nid, []):
                if ch not in order:
                    queue.append(ch)
        for nid in mapping:
            if nid not in order:
                order[nid] = seq
                seq += 1

        rows = []
        for nid, node in mapping.items():
            if not isinstance(node, dict):
                continue
            m = node.get("message")
            if not isinstance(m, dict):
                continue
            author = (m.get("author") or {}).get("role") or "unknown"
            if author in ("tool",) and m.get("recipient") not in (None, "all"):
                continue
            text, attachments = extract_parts(m.get("content"))
            raw_ts = m.get("create_time")
            if not text and not attachments:
                continue
            meta = m.get("metadata") or {}
            rows.append({
                "role": author,
                "text": text,
                "thinking": "",
                "model": meta.get("model_slug") or meta.get("model"),
                "attachments": attachments,
                "timestamp": to_iso(raw_ts),
                "timestamp_source": "native" if raw_ts else "none",
                "_has_ts": raw_ts is not None,
                "_raw_ts": raw_ts or 0,
                "_order": order.get(nid, 1 << 30),
            })
        rows.sort(key=lambda r: (0 if r["_has_ts"] else 1, r["_raw_ts"], r["_order"]))
        for i, r in enumerate(rows):
            r["message_index"] = i
            r.pop("_has_ts", None)
            r.pop("_raw_ts", None)
            r.pop("_order", None)

        ts_list = [r["timestamp"] for r in rows if r["timestamp"]]
        conversations.append({
            "source": "chatgpt",
            "conversation_id": cid,
            "title": title,
            "created_at": to_iso(c.get("create_time")) or (ts_list[0] if ts_list else None),
            "updated_at": to_iso(c.get("update_time")) or (ts_list[-1] if ts_list else None),
            "messages": rows,
        })
    return conversations


def parse_deepseek(path):
    data = load_json_any(path)
    if isinstance(data, dict):
        data = data.get("conversations") or data.get("data") or []
    if not isinstance(data, list):
        raise SystemExit("错误: DeepSeek 导出格式无法识别 (期望顶层为数组): %s" % path)

    conversations = []
    for c in data:
        if not isinstance(c, dict):
            continue
        cid = str(c.get("id") or c.get("conversation_id") or "")
        title = str(c.get("title") or "(无标题)")
        mapping = c.get("mapping") or {}
        if not isinstance(mapping, dict):
            mapping = {}

        children, roots = {}, []
        for nid, node in mapping.items():
            if not isinstance(node, dict):
                continue
            parent = node.get("parent")
            if parent is None:
                roots.append(nid)
            else:
                children.setdefault(parent, []).append(nid)

        order, seq = {}, 0
        queue = list(roots) if roots else list(mapping.keys())
        while queue:
            nid = queue.pop(0)
            if nid in order:
                continue
            order[nid] = seq
            seq += 1
            for ch in children.get(nid, []):
                if ch not in order:
                    queue.append(ch)
        for nid in mapping:
            if nid not in order:
                order[nid] = seq
                seq += 1

        rows = []
        for nid, node in mapping.items():
            if not isinstance(node, dict):
                continue
            m = node.get("message")
            if not isinstance(m, dict):
                continue
            frags = m.get("fragments")
            if not isinstance(frags, list):
                frags = []
            user_text, asst_text, think_parts, attachments = [], [], [], []
            has_request = False
            for fr in frags:
                if not isinstance(fr, dict):
                    continue
                ft = fr.get("type")
                if ft == "REQUEST":
                    has_request = True
                    if fr.get("content"):
                        user_text.append(str(fr["content"]))
                elif ft == "RESPONSE":
                    if fr.get("content"):
                        asst_text.append(str(fr["content"]))
                elif ft == "THINK":
                    if fr.get("content"):
                        think_parts.append(str(fr["content"]))
                elif ft == "SEARCH":
                    res = fr.get("results")
                    attachments.append("search:%d" % (len(res) if isinstance(res, list) else 0))
                elif ft == "FILE":
                    files = fr.get("files")
                    if isinstance(files, list):
                        for f in files:
                            if isinstance(f, dict):
                                attachments.append(f.get("file_name") or f.get("name") or f.get("id") or "file")
                            elif isinstance(f, str):
                                attachments.append(f)
                    else:
                        attachments.append("file")
                elif isinstance(ft, str) and ft.startswith("TOOL_"):
                    attachments.append(ft.lower())
            parts = user_text + asst_text
            text = "\n\n".join(p for p in parts if p)
            if not text and not think_parts and not attachments:
                continue
            raw_ts = m.get("inserted_at")
            rows.append({
                "role": "user" if has_request else "assistant",
                "text": text,
                "thinking": "\n\n".join(think_parts),
                "model": m.get("model"),
                "attachments": attachments,
                "timestamp": to_iso(raw_ts),
                "timestamp_source": "native" if raw_ts else "none",
                "_has_ts": raw_ts is not None,
                "_raw_ts": str(raw_ts or ""),
                "_order": order.get(nid, 1 << 30),
            })
        rows.sort(key=lambda r: (0 if r["_has_ts"] else 1, r["_raw_ts"], r["_order"]))
        for i, r in enumerate(rows):
            r["message_index"] = i
            r.pop("_has_ts", None)
            r.pop("_raw_ts", None)
            r.pop("_order", None)

        ts_list = [r["timestamp"] for r in rows if r["timestamp"]]
        conversations.append({
            "source": "deepseek",
            "conversation_id": cid,
            "title": title,
            "created_at": to_iso(c.get("inserted_at")) or (ts_list[0] if ts_list else None),
            "updated_at": to_iso(c.get("updated_at")) or (ts_list[-1] if ts_list else None),
            "messages": rows,
        })
    return conversations


def normalize_gemini_turn(t):
    if isinstance(t, str):
        return {"role": "unknown", "text": t, "thinking": "", "model": None,
                "attachments": [], "timestamp": None}
    if not isinstance(t, dict):
        return None
    role = t.get("role") or t.get("author") or t.get("sender") or "unknown"
    if isinstance(role, dict):
        role = role.get("role") or role.get("name") or "unknown"
    role = str(role).lower()
    if role in ("model", "ai", "bot", "assistant", "gemini", "response"):
        role = "assistant"
    elif role in ("human", "me", "user", "prompt"):
        role = "user"
    text = t.get("text") or t.get("content") or t.get("markdown") or t.get("body") or ""
    if isinstance(text, list):
        text = "\n".join(str(x) for x in text)
    thinking = t.get("thinking") or t.get("thoughts") or t.get("reasoning") or ""
    if isinstance(thinking, list):
        thinking = "\n".join(str(x) for x in thinking)
    atts = t.get("attachments") or t.get("images") or []
    if isinstance(atts, str):
        atts = [atts]
    return {
        "role": role,
        "text": str(text),
        "thinking": str(thinking),
        "model": t.get("model"),
        "attachments": atts if isinstance(atts, list) else [],
        "timestamp": to_iso(t.get("timestamp") or t.get("time") or t.get("create_time")),
    }


def gemini_turns_from_obj(obj):
    raw = None
    for key in ("turns", "messages", "conversation", "chat", "content"):
        if isinstance(obj.get(key), (list, dict)):
            raw = obj[key]
            break
    turns = []
    if isinstance(raw, list):
        for t in raw:
            n = normalize_gemini_turn(t)
            if n:
                turns.append(n)
    elif isinstance(raw, dict):
        keys = sorted(raw.keys(), key=lambda x: int(x) if str(x).isdigit() else 0)
        for k in keys:
            n = normalize_gemini_turn(raw[k])
            if n:
                turns.append(n)
    elif "role" in obj or "text" in obj or "content" in obj:
        n = normalize_gemini_turn(obj)
        if n:
            turns.append(n)
    return [t for t in turns if t["text"] or t["thinking"] or t["attachments"]]


def parse_gemini_ndjson(path):
    path = Path(path)
    grouped, order = {}, []
    with open(path, "r", encoding="utf-8-sig") as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as e:
                raise SystemExit("错误: %s 第 %d 行不是合法 JSON: %s" % (path, lineno, e))
            if not isinstance(obj, dict):
                continue
            cid = str(obj.get("id") or obj.get("conversation_id") or obj.get("chat_id") or "")
            if not cid:
                cid = "gemini-%04d" % (len(order) + 1)
            if cid not in grouped:
                grouped[cid] = {
                    "source": "gemini",
                    "conversation_id": cid,
                    "title": str(obj.get("title") or obj.get("name") or "(无标题)"),
                    "created_at": None,
                    "updated_at": None,
                    "messages": [],
                    "_meta": obj,
                }
                order.append(cid)
            turns = gemini_turns_from_obj(obj)
            grouped[cid]["messages"].extend(turns)

    conversations = []
    for cid in order:
        conv = grouped[cid]
        meta = conv.pop("_meta")
        rows = []
        for i, t in enumerate(conv["messages"]):
            rows.append({
                "role": t["role"],
                "text": t["text"],
                "thinking": t["thinking"],
                "model": t["model"],
                "attachments": t["attachments"],
                "timestamp": t["timestamp"],
                "timestamp_source": "native" if t["timestamp"] else "none",
                "message_index": i,
            })
        if not rows:
            continue
        if not conv["title"] or conv["title"] == "(无标题)":
            for r in rows:
                if r["role"] == "user" and r["text"]:
                    conv["title"] = r["text"].strip().splitlines()[0][:80]
                    break
        conv["messages"] = rows
        conversations.append(conv)
    return conversations


TZ_OFFSETS = {
    "CST": 8.0, "UTC": 0.0, "GMT": 0.0,
    "PST": -8.0, "PDT": -7.0, "MST": -7.0, "MDT": -6.0,
    "EST": -5.0, "EDT": -4.0, "JST": 9.0, "KST": 9.0,
    "IST": 5.5, "CET": 1.0, "CEST": 2.0,
}

CJK_DATE_RE = re.compile(
    r"(\d{4})年(\d{1,2})月(\d{1,2})日\s+(\d{1,2}):(\d{2}):(\d{2})\s*([A-Za-z]{2,5})?"
)

_BLOCK_TAGS = ("p", "div", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6",
               "tr", "table", "blockquote", "pre", "section", "figure")


def parse_activity_time(m, default_offset=8.0):
    try:
        y, mo, d, h, mi, se = (int(m.group(i)) for i in range(1, 7))
    except (TypeError, ValueError):
        return None
    name = (m.group(7) or "").upper()
    off = TZ_OFFSETS.get(name, default_offset)
    try:
        dt = datetime(y, mo, d, h, mi, se, tzinfo=timezone(timedelta(hours=off)))
    except ValueError:
        return None
    return dt.astimezone(timezone.utc).isoformat()


def html_to_text(h):
    if not h:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", h, flags=re.I)
    s = re.sub(r"<li[^>]*>", "- ", s, flags=re.I)
    for tag in _BLOCK_TAGS:
        s = re.sub(r"</%s\s*>" % tag, "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_unescape(s).replace("\xa0", " ")
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def extract_html_attachments(h):
    if not h:
        return []
    return [m.group(1) for m in re.finditer(r'<img[^>]+src="([^"]+)"', h, re.I)]


ACTIVITY_ITEM_MARKER = '<div class="outer-cell'


def iter_activity_items(path, chunk_size=1 << 20):
    """流式读取活动记录 HTML, 逐条 yield, 避免把整个 140MB 文件读进内存。"""
    buf = ""
    started = False
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        while True:
            block = fh.read(chunk_size)
            if not block:
                break
            buf += block
            if ACTIVITY_ITEM_MARKER not in buf:
                continue
            parts = buf.split(ACTIVITY_ITEM_MARKER)
            buf = parts.pop()
            if not started:
                parts = parts[1:]
                started = True
            for p in parts:
                yield p
    if started and buf:
        yield buf


def parse_gemini_activity_items(chunks, tz_offset=8.0):
    """解析 Google Takeout 的 '我的活动记录.html' (Gemini Apps 活动记录)。

    每条记录形如:
      <div class="outer-cell ..."> ... <div class="content-cell ...body-1">
      Prompted <提问><br><日期> CST<br><回答 HTML></div> ...
      <a href="https://gemini.google.com/app/<会话ID>">
    """
    cell_re = re.compile(
        r'<div class="content-cell mdl-cell mdl-cell--6-col mdl-typography--body-1">'
        r"(.*?)<div class=\"content-cell", re.S)

    convs, order = {}, []
    for ch in chunks:
        m = cell_re.search(ch)
        if not m:
            continue
        cell = m.group(1)
        dm = CJK_DATE_RE.search(cell)
        if not dm:
            continue
        ts = parse_activity_time(dm, tz_offset)
        lm = re.search(r"gemini\.google\.com/app/([0-9a-fA-F]{8,})", ch)
        cid = lm.group(1) if lm else "gemini-unknown"

        pre = re.sub(r"(?:<br\s*/?>\s*)+$", "", cell[:dm.start()])
        am = re.match(r"\s*([A-Za-z]+)[\s\xa0]*", pre)
        action = am.group(1) if am else ""
        body_html = pre[am.end():] if am else pre
        prompt = html_to_text(body_html)
        resp_html = cell[dm.end():]
        resp = html_to_text(resp_html)

        if cid not in convs:
            convs[cid] = {"source": "gemini", "conversation_id": cid,
                          "title": "(无标题)", "created_at": None,
                          "updated_at": None, "messages": []}
            order.append(cid)
        conv = convs[cid]
        if action in ("Prompted", "Branched") and (prompt or resp):
            conv["messages"].append({
                "ts": ts, "prompt": prompt, "resp": resp,
                "atts": extract_html_attachments(resp_html), "action": action,
            })

    conversations = []
    for cid in order:
        conv = convs[cid]
        events = sorted(conv["messages"], key=lambda x: (x["ts"] or "", x["action"]))
        rows = []
        for e in events:
            src = "native" if e["ts"] else "none"
            if e["prompt"]:
                rows.append({"role": "user", "text": e["prompt"], "thinking": "",
                             "model": None, "attachments": [], "timestamp": e["ts"],
                             "timestamp_source": src})
            if e["resp"]:
                rows.append({"role": "assistant", "text": e["resp"], "thinking": "",
                             "model": None, "attachments": e["atts"],
                             "timestamp": e["ts"], "timestamp_source": src})
        if not rows:
            continue
        for i, r in enumerate(rows):
            r["message_index"] = i
        for r in rows:
            if r["role"] == "user" and r["text"]:
                conv["title"] = r["text"].strip().splitlines()[0][:80]
                break
        ts_list = [r["timestamp"] for r in rows if r["timestamp"]]
        conv["created_at"] = ts_list[0] if ts_list else None
        conv["updated_at"] = ts_list[-1] if ts_list else None
        conv["messages"] = rows
        conversations.append(conv)
    return conversations


def parse_gemini_activity_html(text, tz_offset=8.0):
    return parse_gemini_activity_items(text.split(ACTIVITY_ITEM_MARKER)[1:], tz_offset)


ACTIVITY_HTML_NAMES = ("我的活动记录.html", "my activity.html", "myactivity.html")


def is_activity_html(name):
    low = name.lower()
    if low.endswith(ACTIVITY_HTML_NAMES):
        return True
    base = low.rsplit("/", 1)[-1]
    return base.endswith(".html") and ("activity" in base or "活动" in base)


def extract_members_to_temp(path, predicate):
    """把压缩包内匹配的成员流式解压到临时文件, 逐个 yield (name, tmp_path)。"""
    path = Path(path)
    low = path.name.lower()
    if low.endswith((".tgz", ".tar.gz", ".tar", ".tbz2", ".txz")):
        with tarfile.open(path, "r:*") as tf:
            for m in tf.getmembers():
                if not (m.isfile() and predicate(m.name)):
                    continue
                fh = tf.extractfile(m)
                if fh is None:
                    continue
                fd, tmp = tempfile.mkstemp(suffix=".html")
                try:
                    with os.fdopen(fd, "wb") as out:
                        while True:
                            b = fh.read(1 << 20)
                            if not b:
                                break
                            out.write(b)
                    yield m.name, tmp
                except BaseException:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise
        return
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(path) as zf:
            for n in zf.namelist():
                if not predicate(n):
                    continue
                fd, tmp = tempfile.mkstemp(suffix=".html")
                try:
                    with os.fdopen(fd, "wb") as out, zf.open(n) as src:
                        while True:
                            b = src.read(1 << 20)
                            if not b:
                                break
                            out.write(b)
                    yield n, tmp
                except BaseException:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise


def parse_gemini_activity_file(path, tz_offset=8.0):
    path = Path(path)
    if path.suffix.lower() == ".html":
        print("[*] 解析 Gemini 活动记录 HTML: %s" % path.name)
        return parse_gemini_activity_items(iter_activity_items(path), tz_offset)
    conversations = []
    for name, tmp in extract_members_to_temp(path, is_activity_html):
        print("[*] 解析 Gemini 活动记录 HTML: %s" % name)
        try:
            conversations.extend(
                parse_gemini_activity_items(iter_activity_items(tmp), tz_offset))
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return conversations


def load_takeout(paths):
    items = []
    for p in paths:
        data = load_json_any(p)
        if isinstance(data, dict):
            for key in ("items", "activity", "data", "locations"):
                if isinstance(data.get(key), list):
                    data = data[key]
                    break
        if not isinstance(data, list):
            raise SystemExit("错误: Takeout MyActivity 格式无法识别: %s" % p)
        for it in data:
            if not isinstance(it, dict):
                continue
            prompt = it.get("title") or ""
            extra = []
            subs = it.get("subtitles")
            if isinstance(subs, list):
                for s in subs:
                    if isinstance(s, dict) and s.get("name"):
                        extra.append(str(s["name"]))
                    elif isinstance(s, str):
                        extra.append(s)
            det = it.get("details")
            if isinstance(det, list):
                for d in det:
                    if isinstance(d, dict) and d.get("name"):
                        extra.append(str(d["name"]))
            items.append({
                "prompt": str(prompt),
                "time": to_iso(it.get("time") or it.get("timestamp")),
                "extra": extra,
                "file": str(p),
            })
    return items


def build_takeout_index(items):
    index = {}
    for i, it in enumerate(items):
        n = norm_text(it["prompt"])
        if n:
            index.setdefault(n, []).append(i)
    return index


def match_takeout(text, index, items, used, threshold):
    n = norm_text(text)
    if not n:
        return None, None, 0.0
    if n in index:
        for i in index[n]:
            if i not in used:
                used.add(i)
                return items[i]["time"], "exact", 1.0
        i = index[n][0]
        return items[i]["time"], "exact", 1.0
    cands = []
    lo, hi = len(n) * 0.7, len(n) * 1.4
    for key in index:
        if lo <= len(key) <= hi:
            cands.append(key)
    if not cands:
        return None, None, 0.0
    hits = get_close_matches(n, cands, n=5, cutoff=threshold)
    best, best_ratio = None, 0.0
    for h in hits:
        for i in index[h]:
            if i not in used:
                from difflib import SequenceMatcher
                r = SequenceMatcher(None, n, h).ratio()
                if r > best_ratio:
                    best, best_ratio = i, r
                break
    if best is None:
        return None, None, 0.0
    used.add(best)
    return items[best]["time"], "fuzzy", round(best_ratio, 3)


def backfill_gemini(conversations, items, threshold):
    index = build_takeout_index(items)
    used = set()
    unmatched = []
    exact = fuzzy = inferred = 0

    for conv in conversations:
        for row in conv["messages"]:
            if row["timestamp"]:
                continue
            if row["role"] == "user" and row["text"]:
                ts, how, ratio = match_takeout(row["text"], index, items, used, threshold)
                if ts:
                    row["timestamp"] = ts
                    row["timestamp_source"] = "takeout"
                    if how == "exact":
                        exact += 1
                    else:
                        fuzzy += 1
                else:
                    unmatched.append({
                        "conversation_id": conv["conversation_id"],
                        "title": conv["title"],
                        "message_index": row["message_index"],
                        "role": row["role"],
                        "text": row["text"][:200],
                    })
        prev_user_ts = None
        for row in conv["messages"]:
            if row["timestamp"] and row["role"] == "user":
                prev_user_ts = row["timestamp"]
            elif not row["timestamp"] and row["role"] == "assistant" and prev_user_ts:
                row["timestamp"] = prev_user_ts
                row["timestamp_source"] = "takeout-inferred"
                inferred += 1
        ts_list = [r["timestamp"] for r in conv["messages"] if r["timestamp"]]
        if ts_list:
            conv["created_at"] = conv["created_at"] or ts_list[0]
            conv["updated_at"] = ts_list[-1]

    stats = {
        "takeout_items": len(items),
        "exact": exact,
        "fuzzy": fuzzy,
        "assistant_inferred": inferred,
        "unmatched_user_prompts": len(unmatched),
    }
    return stats, unmatched


def flatten(conversations):
    records = []
    for conv in conversations:
        for row in conv["messages"]:
            records.append({
                "source": conv["source"],
                "conversation_id": conv["conversation_id"],
                "title": conv["title"],
                "created_at": conv["created_at"],
                "updated_at": conv["updated_at"],
                "message_index": row["message_index"],
                "role": row["role"],
                "timestamp": row["timestamp"],
                "timestamp_source": row["timestamp_source"],
                "text": row["text"],
                "thinking": row["thinking"],
                "model": row["model"],
                "attachments": row["attachments"],
            })
    return records


def slugify(s, maxlen=60):
    s = unicodedata.normalize("NFKC", s or "")
    s = re.sub(r'[\\/:*?"<>|\n\r\t]+', " ", s)
    s = re.sub(r"\s+", "-", s.strip())
    s = s.strip("-.")
    return s[:maxlen] or "untitled"


def write_jsonl(records, path):
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def write_markdown(conversations, base, tz, throttle=0.0):
    written = []
    for conv in conversations:
        if throttle:
            time.sleep(throttle)
        if not conv["messages"]:
            continue
        date = (conv["created_at"] or "")[:10] or "undated"
        name = "%s_%s_%s.md" % (date, slugify(conv["title"]), conv["conversation_id"][:8] or "noid")
        out = base / name
        lines = [
            "---",
            "source: %s" % conv["source"],
            "conversation_id: %s" % conv["conversation_id"],
            "title: %s" % json.dumps(conv["title"], ensure_ascii=False),
            "created_at: %s" % (conv["created_at"] or ""),
            "updated_at: %s" % (conv["updated_at"] or ""),
            "messages: %d" % len(conv["messages"]),
            "---",
            "",
            "# %s" % conv["title"],
            "",
        ]
        for row in conv["messages"]:
            when = local_str(row["timestamp"], tz) or "时间未知"
            src = row["timestamp_source"]
            head = "## [%d] %s — %s (%s)" % (row["message_index"], row["role"], when, src)
            lines.append(head)
            if row["model"]:
                lines.append("")
                lines.append("> model: `%s`" % row["model"])
            lines.append("")
            lines.append(row["text"] or "_(空)_")
            if row["thinking"]:
                lines.append("")
                lines.append("<details><summary>thinking</summary>")
                lines.append("")
                lines.append(row["thinking"])
                lines.append("")
                lines.append("</details>")
            if row["attachments"]:
                lines.append("")
                lines.append("attachments: " + ", ".join(str(a) for a in row["attachments"]))
            lines.append("")
        out.write_text("\n".join(lines), encoding="utf-8")
        written.append(str(out))
    return written


def write_sqlite(conversations, records, path, throttle=0.0):
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(str(path))
    cur = conn.cursor()
    cur.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE conversations (
            conversation_id TEXT PRIMARY KEY,
            source TEXT, title TEXT,
            created_at TEXT, updated_at TEXT,
            message_count INTEGER
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT, source TEXT, message_index INTEGER,
            role TEXT, timestamp TEXT, timestamp_source TEXT,
            model TEXT, text TEXT, thinking TEXT, attachments TEXT
        );
        CREATE INDEX idx_msg_conv ON messages(conversation_id);
        CREATE INDEX idx_msg_ts ON messages(timestamp);
        CREATE VIRTUAL TABLE messages_fts USING fts5(
            text, title, conversation_id UNINDEXED, source UNINDEXED,
            role UNINDEXED, timestamp UNINDEXED, tokenize='unicode61'
        );
    """)
    cur.executemany(
        "INSERT INTO conversations VALUES (?,?,?,?,?,?)",
        [(c["conversation_id"], c["source"], c["title"], c["created_at"],
          c["updated_at"], len(c["messages"])) for c in conversations],
    )
    batch = 2000
    for i in range(0, len(records), batch):
        chunk = records[i:i + batch]
        cur.executemany(
            "INSERT INTO messages (conversation_id, source, message_index, role, timestamp,"
            " timestamp_source, model, text, thinking, attachments) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [(r["conversation_id"], r["source"], r["message_index"], r["role"], r["timestamp"],
              r["timestamp_source"], r["model"], r["text"], r["thinking"],
              json.dumps(r["attachments"], ensure_ascii=False)) for r in chunk],
        )
        cur.executemany(
            "INSERT INTO messages_fts (text, title, conversation_id, source, role, timestamp)"
            " VALUES (?,?,?,?,?,?)",
            [(r["text"], r["title"], r["conversation_id"], r["source"], r["role"], r["timestamp"])
             for r in chunk],
        )
        conn.commit()
        if throttle:
            time.sleep(throttle)
    conn.close()


def archive_member_names(path):
    path = Path(path)
    low = path.name.lower()
    if low.endswith((".tgz", ".tar.gz", ".tar", ".tbz2", ".txz")):
        try:
            with tarfile.open(path, "r:*") as tf:
                return [m.name for m in tf.getmembers() if m.isfile()]
        except (tarfile.TarError, OSError):
            return []
    if path.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(path) as zf:
                return zf.namelist()
        except (zipfile.BadZipFile, OSError):
            return []
    return []


def _sniff_conversations_bytes(buf):
    if b'"fragments"' in buf:
        return "deepseek"
    if b'"author"' in buf:
        return "chatgpt"
    return None


def sniff_conversations_kind(path):
    path = Path(path)
    low = path.name.lower()
    try:
        if low.endswith((".tgz", ".tar.gz", ".tar", ".tbz2", ".txz")):
            with tarfile.open(path, "r:*") as tf:
                for m in tf.getmembers():
                    if m.isfile() and m.name.lower().endswith("conversations.json"):
                        with tf.extractfile(m) as fh:
                            return _sniff_conversations_bytes(fh.read(1 << 21))
        elif path.suffix.lower() == ".zip":
            with zipfile.ZipFile(path) as zf:
                for n in zf.namelist():
                    if n.lower().endswith("conversations.json"):
                        with zf.open(n) as fh:
                            return _sniff_conversations_bytes(fh.read(1 << 21))
        else:
            with open(path, "rb") as fh:
                return _sniff_conversations_bytes(fh.read(1 << 21))
    except (OSError, tarfile.TarError, zipfile.BadZipFile, KeyError):
        return None
    return None


def discover(raw):
    chatgpt, takeout, gemini, activity, deepseek = [], [], [], [], []
    for p in sorted(raw.rglob("*")):
        if not p.is_file():
            continue
        low = p.name.lower()
        suf = p.suffix.lower()
        if low == "conversations.json":
            (deepseek if sniff_conversations_kind(p) == "deepseek" else chatgpt).append(p)
            continue
        if low == "myactivity.json":
            takeout.append(p)
            continue
        if suf == ".zip" or low.endswith((".tgz", ".tar.gz", ".tar", ".tbz2", ".txz")):
            names = archive_member_names(p)
            if any(n.lower().endswith("conversations.json") for n in names):
                kind = sniff_conversations_kind(p)
                (deepseek if kind == "deepseek" else chatgpt).append(p)
            elif any(is_activity_html(n) for n in names):
                activity.append(p)
            elif any(n.lower().endswith("myactivity.json") for n in names):
                takeout.append(p)
            elif any(n.lower().endswith(".ndjson") for n in names):
                gemini.append(p)
            continue
        if suf == ".html" and is_activity_html(p.name):
            activity.append(p)
            continue
        if low.endswith(".ndjson") or (suf == ".json" and "gemini" in low):
            gemini.append(p)
    return chatgpt, takeout, gemini, activity, deepseek


def main():
    ap = argparse.ArgumentParser(description="归档 ChatGPT / Gemini 聊天记录")
    ap.add_argument("--raw", default="raw", help="原始文件目录 (默认 raw)")
    ap.add_argument("--out", default="out", help="输出目录 (默认 out)")
    ap.add_argument("--chatgpt", action="append", default=[], help="手动指定 conversations.json")
    ap.add_argument("--takeout", action="append", default=[], help="手动指定 MyActivity.json")
    ap.add_argument("--takeout-html", action="append", default=[], help="手动指定 我的活动记录.html 或 Takeout 整包")
    ap.add_argument("--gemini", action="append", default=[], help="手动指定 gemini_chats.ndjson")
    ap.add_argument("--deepseek", action="append", default=[], help="手动指定 DeepSeek conversations.json 或导出 ZIP")
    ap.add_argument("--match-threshold", type=float, default=0.90, help="Takeout 模糊匹配阈值")
    ap.add_argument("--activity-tz-offset", type=float, default=8.0,
                    help="活动记录 HTML 中时区缩写无法识别时的默认 UTC 偏移 (默认 8)")
    ap.add_argument("--tz", default="Asia/Shanghai", help="Markdown 显示用本地时区")
    ap.add_argument("--dry-run", action="store_true", help="只统计不写文件")
    ap.add_argument("--no-sqlite", action="store_true", help="不生成 SQLite")
    ap.add_argument("--no-md", action="store_true", help="不生成 Markdown")
    ap.add_argument("--nice", type=int, default=0, help="进程优先级增量 (0-19, 越大越让路)")
    ap.add_argument("--throttle", type=float, default=0.0,
                    help="每批写入后休眠秒数, 用于降低功耗/负载 (如 0.05)")
    args = ap.parse_args()

    if args.nice:
        try:
            os.nice(args.nice)
        except OSError:
            pass

    raw = Path(args.raw)
    out = Path(args.out)
    if not raw.exists():
        raise SystemExit("错误: 原始目录不存在: %s" % raw)

    tz = None
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(args.tz)
        except Exception:
            tz = timezone.utc

    d_chatgpt, d_takeout, d_gemini, d_activity, d_deepseek = discover(raw)
    chatgpt_files = [Path(p) for p in args.chatgpt] + d_chatgpt
    takeout_files = [Path(p) for p in args.takeout] + d_takeout
    gemini_files = [Path(p) for p in args.gemini] + d_gemini
    activity_files = [Path(p) for p in args.takeout_html] + d_activity
    deepseek_files = [Path(p) for p in args.deepseek] + d_deepseek

    if not (chatgpt_files or gemini_files or takeout_files or activity_files or deepseek_files):
        raise SystemExit(
            "错误: 在 %s 下没找到任何输入。\n"
            "  ChatGPT : conversations.json (官方导出 ZIP 解压)\n"
            "  Gemini  : 我的活动记录.html (Takeout -> My Activity -> Gemini Apps, HTML)\n"
            "            或 MyActivity.json (JSON 时间线)\n"
            "            或 gemini_chats.ndjson (油猴脚本全文)\n"
            "            或 Takeout 整包 (.zip/.tgz)\n"
            "  DeepSeek: conversations.json / deepseek_data-*.zip (官方导出)" % raw
        )

    inputs = {}
    conversations = []

    for p in chatgpt_files:
        print("[*] 解析 ChatGPT: %s" % p)
        inputs[str(p)] = sha256_file(p)
        conversations.extend(parse_chatgpt(p))

    for p in deepseek_files:
        print("[*] 解析 DeepSeek: %s" % p)
        inputs[str(p)] = sha256_file(p)
        conversations.extend(parse_deepseek(p))

    gemini_convs = []
    for p in activity_files:
        print("[*] 解析 Gemini 活动记录: %s" % p)
        inputs[str(p)] = sha256_file(p)
        gemini_convs.extend(parse_gemini_activity_file(p, args.activity_tz_offset))

    for p in gemini_files:
        print("[*] 解析 Gemini 全文: %s" % p)
        inputs[str(p)] = sha256_file(p)
        gemini_convs.extend(parse_gemini_ndjson(p))

    takeout_stats, unmatched = None, []
    if takeout_files:
        for p in takeout_files:
            print("[*] 读取 Takeout 时间线: %s" % p)
            inputs[str(p)] = sha256_file(p)
        items = load_takeout(takeout_files)
        takeout_stats, unmatched = backfill_gemini(gemini_convs, items, args.match_threshold)
    elif gemini_convs and any(not r["timestamp"] for c in gemini_convs for r in c["messages"]):
        print("[!] 没有 Takeout 时间线, 部分 Gemini 消息将没有时间戳")
    conversations.extend(gemini_convs)

    records = flatten(conversations)

    by_source = {}
    for r in records:
        by_source.setdefault(r["source"], {"messages": 0, "conversations": 0})
    for conv in conversations:
        by_source.setdefault(conv["source"], {"messages": 0, "conversations": 0})
        by_source[conv["source"]]["conversations"] += 1
    for r in records:
        by_source[r["source"]]["messages"] += 1

    ts_stats = {}
    for r in records:
        ts_stats[r["timestamp_source"]] = ts_stats.get(r["timestamp_source"], 0) + 1

    gemini_total = sum(1 for r in records if r["source"] == "gemini")
    gemini_dated = sum(1 for r in records if r["source"] == "gemini" and r["timestamp"])

    print("\n===== 统计 =====")
    for src, s in sorted(by_source.items()):
        print("  %-8s 对话 %d, 消息 %d" % (src, s["conversations"], s["messages"]))
    print("  时间戳来源: %s" % ", ".join("%s=%d" % kv for kv in sorted(ts_stats.items())))
    if gemini_total:
        print("  Gemini 有时间的消息: %d/%d (%.1f%%)"
              % (gemini_dated, gemini_total, 100.0 * gemini_dated / gemini_total))
    if takeout_stats:
        print("  Takeout: 精确 %d, 模糊 %d, 助手时间推断 %d, 未匹配 prompt %d"
              % (takeout_stats["exact"], takeout_stats["fuzzy"],
                 takeout_stats["assistant_inferred"], takeout_stats["unmatched_user_prompts"]))

    if args.dry_run:
        print("\n[dry-run] 未写任何文件。")
        return

    out.mkdir(parents=True, exist_ok=True)
    write_jsonl(records, out / "normalized.jsonl")
    print("\n[+] %s" % (out / "normalized.jsonl"))

    md_files = []
    if not args.no_md:
        for src in ("chatgpt", "gemini", "deepseek"):
            sub = [c for c in conversations if c["source"] == src]
            if sub:
                d = out / "md" / src
                d.mkdir(parents=True, exist_ok=True)
                md_files.extend(write_markdown(sub, d, tz, args.throttle))
        print("[+] %d 个 Markdown 文件 -> %s" % (len(md_files), out / "md"))

    if not args.no_sqlite:
        write_sqlite(conversations, records, out / "archive.sqlite", args.throttle)
        print("[+] %s" % (out / "archive.sqlite"))

    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "inputs": inputs,
        "by_source": by_source,
        "timestamp_sources": ts_stats,
        "gemini_dated_ratio": round(gemini_dated / gemini_total, 4) if gemini_total else None,
        "takeout": takeout_stats,
        "unmatched_gemini_prompts": unmatched[:500],
        "unmatched_truncated": max(0, len(unmatched) - 500),
        "outputs": {
            "jsonl": str(out / "normalized.jsonl"),
            "markdown": len(md_files),
            "sqlite": None if args.no_sqlite else str(out / "archive.sqlite"),
        },
    }
    (out / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print("[+] %s" % (out / "manifest.json"))
    print("\n完成。共 %d 条消息 / %d 个对话。" % (len(records), len(conversations)))


if __name__ == "__main__":
    main()
