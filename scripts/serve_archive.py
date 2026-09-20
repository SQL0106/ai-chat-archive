#!/usr/bin/env python3
"""AI 聊天归档的本地浏览/搜索服务（纯标准库，无第三方依赖）。

用法:
    python3 scripts/serve_archive.py                 # 0.0.0.0:8765
    python3 scripts/serve_archive.py --port 9000
    python3 scripts/serve_archive.py --host 127.0.0.1
    python3 scripts/serve_archive.py --db out/archive.sqlite --web web

提供:
    GET /                     前端页面
    GET /style.css /app.js    静态资源
    GET /api/stats            统计数据
    GET /api/conversations    对话列表 (q/from/to/sort/offset/limit)
    GET /api/conversation     单个对话全文 (id, 可选 format=md 下载)
    GET /api/search           FTS5 全文搜索 (q/from/to/role/offset/limit)
    GET /api/timeline         按天/月聚合 (from/to/bucket)
    GET /api/day              某天活跃的对话 (date)
    GET /api/jump             按日期时间定位到最近的消息 (ts)

时区: 所有时间戳在库里是 UTC，本服务按 UTC+8 展示/过滤（可用 --tz-offset 改）。
"""

import argparse
import json
import mimetypes
import os
import re
import socket
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "out" / "archive.sqlite"
DEFAULT_WEB = ROOT / "web"

sys.path.insert(0, str(ROOT / "tools"))
import arclib  # noqa: E402  (共享数据层，CLI 与 Web 共用)

DB_PATH = DEFAULT_DB
WEB_DIR = DEFAULT_WEB
TZ = timezone(timedelta(hours=8))

MAX_LIMIT = 200
FTS_OPERATORS = re.compile(r'["()*]|\b(AND|OR|NOT|NEAR)\b', re.IGNORECASE)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def local_tz_offset():
    return TZ.utcoffset(None).total_seconds() / 3600.0


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sql_str(s):
    """把字符串变成安全的 SQL 字面量（用于无法参数化的 strftime 格式）。"""
    return "'" + str(s).replace("'", "''") + "'"


def parse_date(s):
    """'YYYY-MM-DD' -> 当天 00:00 的 UTC ISO 字符串。"""
    try:
        d = datetime.strptime(s.strip(), "%Y-%m-%d")
    except (ValueError, AttributeError):
        return None
    return d.replace(tzinfo=TZ).astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_datetime_local(s):
    """'YYYY-MM-DDTHH:MM' (浏览器 datetime-local) -> UTC ISO。"""
    s = (s or "").strip().replace(" ", "T")
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            d = datetime.strptime(s, fmt)
            return d.replace(tzinfo=TZ).astimezone(timezone.utc).isoformat(timespec="seconds")
        except ValueError:
            continue
    return None


def build_fts_query(q):
    """把用户输入变成安全的 FTS5 查询串。"""
    q = (q or "").strip()
    if not q:
        return None
    if FTS_OPERATORS.search(q):
        return q
    tokens = q.split()
    terms = ['"%s"' % t.replace('"', '""') for t in tokens[:-1]]
    terms.append('"%s"*' % tokens[-1].replace('"', '""'))
    return " AND ".join(terms)


def connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = 1")
    return conn


def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def md_escape(text):
    return (text or "").replace("\\", "\\\\").replace("`", "\\`")


def render_markdown(conv, messages):
    lines = [
        "---",
        "source: %s" % conv["source"],
        "conversation_id: %s" % conv["conversation_id"],
        "title: %s" % (conv["title"] or ""),
        "created_at: %s" % (conv["created_at"] or ""),
        "updated_at: %s" % (conv["updated_at"] or ""),
        "messages: %d" % len(messages),
        "---",
        "",
        "# %s" % (conv["title"] or conv["conversation_id"]),
        "",
    ]
    for m in messages:
        ts = m["timestamp"]
        local = ""
        if ts:
            try:
                dt = datetime.fromisoformat(ts).astimezone(TZ)
                local = dt.strftime("%Y-%m-%d %H:%M:%S")
            except ValueError:
                local = ts
        lines.append("## [%s] %s — %s (%s)" % (
            m["message_index"], m["role"], local or "无时间",
            m["timestamp_source"] or "none"))
        lines.append("")
        if m["model"]:
            lines.append("> model: %s" % m["model"])
            lines.append("")
        lines.append(m["text"] or "")
        lines.append("")
        if m["thinking"]:
            lines.append("<details><summary>thinking</summary>")
            lines.append("")
            lines.append(m["thinking"])
            lines.append("")
            lines.append("</details>")
            lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

_STATS_CACHE = {}


def api_stats(conn):
    """统计数据全部来自 arclib.stats()，和 CLI 的 stats 同源。

    这里要做十几次全表聚合，实测约 2.3 秒，而归档文件基本是静态的，
    所以按库文件指纹缓存一份；重跑归档脚本后指纹变了会自动重算。
    """
    try:
        fi = os.stat(DB_PATH)
        key = (fi.st_mtime_ns, fi.st_size)
    except OSError:
        key = None
    if key is None or _STATS_CACHE.get("key") != key:
        data = arclib.stats(conn)
        data["generated_at"] = now_iso()
        _STATS_CACHE["key"] = key
        _STATS_CACHE["val"] = data
    return _STATS_CACHE["val"]


def api_conversations(conn, qs):
    offset = max(0, int(qs.get("offset", ["0"])[0] or 0))
    limit = min(MAX_LIMIT, max(1, int(qs.get("limit", ["30"])[0] or 30)))
    sort = qs.get("sort", ["updated"])[0]
    order = {
        "updated": "updated_at DESC",
        "created": "created_at DESC",
        "oldest": "created_at ASC",
        "messages": "message_count DESC",
        "title": "title COLLATE NOCASE ASC",
    }.get(sort, "updated_at DESC")

    where, args = [], []
    kw = (qs.get("q", [""])[0] or "").strip()
    if kw:
        where.append("(title LIKE ? OR conversation_id LIKE ?)")
        args += ["%" + kw + "%", "%" + kw + "%"]
    src = (qs.get("source", [""])[0] or "").strip()
    if src:
        where.append("source = ?")
        args.append(src)
    frm = parse_date(qs.get("from", [""])[0])
    if frm:
        where.append("updated_at >= ?")
        args.append(frm)
    to = qs.get("to", [""])[0]
    if to:
        d = datetime.strptime(to.strip(), "%Y-%m-%d") + timedelta(days=1)
        where.append("updated_at < ?")
        args.append(d.replace(tzinfo=TZ).astimezone(timezone.utc).isoformat(timespec="seconds"))

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    total = conn.execute("SELECT COUNT(*) FROM conversations" + clause, args).fetchone()[0]
    rows = conn.execute(
        "SELECT conversation_id, source, title, created_at, updated_at, message_count"
        " FROM conversations" + clause + " ORDER BY " + order + " LIMIT ? OFFSET ?",
        args + [limit, offset])
    items = rows_to_dicts(rows)
    for it in items:
        prev = conn.execute(
            "SELECT text FROM messages WHERE conversation_id = ? AND role = 'user'"
            " ORDER BY message_index LIMIT 1", (it["conversation_id"],)).fetchone()
        it["preview"] = (prev["text"] or "").strip().replace("\n", " ")[:160] if prev else ""
    return {"total": total, "offset": offset, "limit": limit, "items": items}


def api_conversation(conn, qs):
    cid = (qs.get("id", [""])[0] or "").strip()
    if not cid:
        return None, 400
    conv = conn.execute(
        "SELECT conversation_id, source, title, created_at, updated_at, message_count"
        " FROM conversations WHERE conversation_id = ?", (cid,)).fetchone()
    if not conv:
        return None, 404
    msgs = rows_to_dicts(conn.execute(
        "SELECT message_index, role, timestamp, timestamp_source, model, text, thinking,"
        " attachments FROM messages WHERE conversation_id = ? ORDER BY message_index", (cid,)))
    for m in msgs:
        try:
            m["attachments"] = json.loads(m["attachments"] or "[]")
        except (ValueError, TypeError):
            m["attachments"] = []
    return {"conversation": dict(conv), "messages": msgs}, 200


def api_search(conn, qs):
    raw = qs.get("q", [""])[0]
    if not raw.strip():
        return {"total": 0, "items": [], "query": raw, "error": "请输入搜索词"}
    offset = max(0, int(qs.get("offset", ["0"])[0] or 0))
    limit = min(MAX_LIMIT, max(1, int(qs.get("limit", ["30"])[0] or 30)))
    # 含中文走 LIKE 全扫（FTS5 的 unicode61 对中文几乎不可用），纯 ASCII 走 FTS5。
    res = arclib.search(
        conn, raw,
        role=(qs.get("role", [""])[0] or "").strip() or None,
        source=(qs.get("source", [""])[0] or "").strip() or None,
        date_from=qs.get("from", [""])[0],
        date_to=qs.get("to", [""])[0],
        limit=limit, offset=offset)
    if res.get("error"):
        return {"total": 0, "items": [], "query": raw, "error": res["error"]}
    return {"total": res["total"], "offset": res["offset"], "limit": res["limit"],
            "query": res["query"], "items": res["items"],
            "mode": res["mode"], "truncated": res["truncated"]}



def api_timeline(conn, qs):
    bucket = qs.get("bucket", ["day"])[0]
    fmt = {"day": "%Y-%m-%d", "month": "%Y-%m", "hour": "%Y-%m-%d %H"}.get(bucket, "%Y-%m-%d")
    where, args = ["timestamp IS NOT NULL"], []
    frm = parse_date(qs.get("from", [""])[0])
    if frm:
        where.append("timestamp >= ?")
        args.append(frm)
    to = qs.get("to", [""])[0]
    if to:
        try:
            d = datetime.strptime(to.strip(), "%Y-%m-%d") + timedelta(days=1)
            where.append("timestamp < ?")
            args.append(d.replace(tzinfo=TZ).astimezone(timezone.utc).isoformat(timespec="seconds"))
        except ValueError:
            pass
    sql = (
        "SELECT strftime(" + sql_str(fmt) + ", timestamp, ?) AS k, COUNT(*) AS n,"
        " COUNT(DISTINCT conversation_id) AS c"
        " FROM messages WHERE " + " AND ".join(where) +
        " GROUP BY k ORDER BY k")
    rows = conn.execute(sql, args + [TZ_MOD])
    return {"bucket": bucket, "items": rows_to_dicts(rows)}


def api_day(conn, qs):
    date = (qs.get("date", [""])[0] or "").strip()
    start = parse_date(date)
    if not start:
        return {"error": "date 参数格式应为 YYYY-MM-DD"}
    end = (datetime.strptime(date, "%Y-%m-%d") + timedelta(days=1)) \
        .replace(tzinfo=TZ).astimezone(timezone.utc).isoformat(timespec="seconds")
    items = rows_to_dicts(conn.execute(
        "SELECT c.conversation_id, c.title, c.source, COUNT(*) AS hits,"
        " MIN(m.timestamp) AS first_ts, MAX(m.timestamp) AS last_ts"
        " FROM messages m JOIN conversations c ON c.conversation_id = m.conversation_id"
        " WHERE m.timestamp >= ? AND m.timestamp < ?"
        " GROUP BY c.conversation_id ORDER BY first_ts", (start, end)))
    total = sum(i["hits"] for i in items)
    return {"date": date, "messages": total, "conversations": len(items), "items": items}


def api_jump(conn, qs):
    iso = parse_datetime_local(qs.get("ts", [""])[0])
    if not iso:
        return {"error": "ts 参数格式应为 YYYY-MM-DDTHH:MM"}
    after = conn.execute(
        "SELECT m.id, m.conversation_id, m.message_index, m.role, m.timestamp,"
        " c.title, substr(m.text, 1, 240) AS preview"
        " FROM messages m JOIN conversations c ON c.conversation_id = m.conversation_id"
        " WHERE m.timestamp >= ? ORDER BY m.timestamp, m.message_index LIMIT 1", (iso,)).fetchone()
    before = conn.execute(
        "SELECT m.id, m.conversation_id, m.message_index, m.role, m.timestamp,"
        " c.title, substr(m.text, 1, 240) AS preview"
        " FROM messages m JOIN conversations c ON c.conversation_id = m.conversation_id"
        " WHERE m.timestamp < ? ORDER BY m.timestamp DESC, m.message_index DESC LIMIT 1",
        (iso,)).fetchone()
    return {"ts": iso, "after": dict(after) if after else None,
            "before": dict(before) if before else None}


# --------------------------------------------------------------------------
# 选集 / 导出
# --------------------------------------------------------------------------

EXPORT_CTYPES = {"md": "text/markdown", "html": "text/html",
                 "jsonl": "application/x-ndjson"}


def api_selection_list(conn, qs):
    coll = (qs.get("collection", ["default"])[0] or "default").strip() or "default"
    ids = arclib.selected_ids(coll)
    return {"collection": coll, "total": len(ids),
            "items": arclib.list_conversations(conn, ids)}


def api_selection_add(payload):
    action = (payload.get("action") or "add").strip()
    coll = (payload.get("collection") or "default").strip() or "default"
    if action not in ("add", "remove"):
        return {"error": "action 只能是 add 或 remove"}, 400
    cids = payload.get("conversation_id") or payload.get("ids") or []
    if isinstance(cids, str):
        cids = [cids]
    cids = [str(c).strip() for c in cids if str(c).strip()]
    if not cids:
        return {"error": "缺少 conversation_id"}, 400
    recs = [arclib.append_selection(cid, action=action, collection=coll,
                                    note=payload.get("note"))
            for cid in cids]
    return {"ok": True, "collection": coll, "action": action,
            "count": len(recs), "ids": [r["conversation_id"] for r in recs]}, 200


def api_export(conn, qs):
    import export as export_mod  # 同目录 tools/ 下的导出器
    coll = (qs.get("collection", ["default"])[0] or "default").strip() or "default"
    fmt = (qs.get("format", ["md"])[0] or "md").strip().lower()
    if fmt not in EXPORT_CTYPES:
        return {"error": "format 只能是 md/html/jsonl"}, 400
    include_thinking = (qs.get("with_thinking", ["0"])[0] or "") in ("1", "true", "yes", "on")
    ids = arclib.selected_ids(coll)
    if not ids:
        return {"error": "选集为空: %s" % coll}, 404
    if fmt == "md":
        text, used = export_mod.render_md(coll, ids, conn, include_thinking=include_thinking)
    elif fmt == "html":
        text, used = export_mod.render_html(coll, ids, conn, include_thinking=include_thinking)
    else:
        text, used = export_mod.render_jsonl(coll, ids, conn, include_thinking=include_thinking)
    if used == 0:
        return {"error": "选集里的对话都找不到: %s" % coll}, 404
    return {"text": text, "used": used, "fmt": fmt, "collection": coll}, 200


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "ArchiveViewer/1.0"

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_text(self, text, status=200, ctype="text/plain; charset=utf-8",
                  extra=None, download=None):
        body = text.encode("utf-8") if isinstance(text, str) else text
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if download:
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % download)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        qs = parse_qs(parsed.query)
        try:
            if path.startswith("/api/"):
                return self.handle_api(path, qs)
            return self.handle_static(path)
        except BrokenPipeError:
            return
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def handle_api(self, path, qs):
        conn = connect()
        try:
            if path == "/api/stats":
                return self.send_json(api_stats(conn))
            if path == "/api/conversations":
                return self.send_json(api_conversations(conn, qs))
            if path == "/api/conversation":
                data, status = api_conversation(conn, qs)
                if data is None:
                    return self.send_json({"error": "对话不存在"}, status)
                if (qs.get("format", [""])[0] == "md"):
                    md = render_markdown(data["conversation"], data["messages"])
                    cid = data["conversation"]["conversation_id"][:8]
                    return self.send_text(md, ctype="text/markdown; charset=utf-8",
                                          download="%s.md" % cid)
                return self.send_json(data)
            if path == "/api/search":
                return self.send_json(api_search(conn, qs))
            if path == "/api/timeline":
                return self.send_json(api_timeline(conn, qs))
            if path == "/api/day":
                return self.send_json(api_day(conn, qs))
            if path == "/api/jump":
                return self.send_json(api_jump(conn, qs))
            if path == "/api/selection":
                return self.send_json(api_selection_list(conn, qs))
            if path == "/api/export":
                data, status = api_export(conn, qs)
                if status != 200:
                    return self.send_json(data, status)
                fmt = data["fmt"]
                return self.send_text(
                    data["text"], ctype=EXPORT_CTYPES[fmt] + "; charset=utf-8",
                    download="selection-%s.%s" % (data["collection"], fmt))
            return self.send_json({"error": "未知接口: %s" % path}, 404)
        finally:
            conn.close()

    def do_POST(self):
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        try:
            if path == "/api/selection":
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = 0
                raw = self.rfile.read(length) if length > 0 else b""
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                except (ValueError, UnicodeDecodeError) as e:
                    return self.send_json({"error": "请求体不是合法 JSON: %s" % e}, 400)
                if not isinstance(payload, dict):
                    return self.send_json({"error": "请求体必须是 JSON 对象"}, 400)
                data, status = api_selection_add(payload)
                return self.send_json(data, status)
            return self.send_json({"error": "未知接口: %s" % path}, 404)
        except BrokenPipeError:
            return
        except Exception as e:  # noqa: BLE001
            self.send_json({"error": "%s: %s" % (type(e).__name__, e)}, 500)

    def handle_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (WEB_DIR / rel).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            return self.send_text("404 Not Found", 404)
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript",):
            ctype += "; charset=utf-8"
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, verbose=False):
        self.verbose = verbose
        super().__init__(addr, handler)


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def main():
    global DB_PATH, WEB_DIR, TZ_MOD
    ap = argparse.ArgumentParser(description="AI 聊天归档浏览服务")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="archive.sqlite 路径")
    ap.add_argument("--web", default=str(DEFAULT_WEB), help="前端静态目录")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8765, help="端口 (默认 8765)")
    ap.add_argument("--tz-offset", type=float, default=8.0, help="展示时区偏移 (默认 8)")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印访问日志")
    args = ap.parse_args()

    DB_PATH = Path(args.db).resolve()
    WEB_DIR = Path(args.web).resolve()
    TZ = timezone(timedelta(hours=args.tz_offset))
    TZ_MOD = "%+g hours" % args.tz_offset

    if not DB_PATH.is_file():
        print("错误: 数据库不存在: %s" % DB_PATH, file=sys.stderr)
        print("先运行: python3 scripts/archive_ai_chats.py --raw raw --out out", file=sys.stderr)
        return 1
    if not (WEB_DIR / "index.html").is_file():
        print("错误: 前端目录缺少 index.html: %s" % WEB_DIR, file=sys.stderr)
        return 1

    srv = Server((args.host, args.port), Handler, verbose=args.verbose)
    ip = lan_ip()
    print("AI 聊天归档服务已启动")
    print("  本机:   http://127.0.0.1:%d" % args.port)
    if args.host == "0.0.0.0":
        print("  局域网: http://%s:%d" % (ip, args.port))
    print("  数据库: %s" % DB_PATH)
    print("  时区:   UTC%+g" % args.tz_offset)
    print("  停止:   Ctrl+C")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        srv.server_close()
    return 0


TZ_MOD = "+8 hours"

if __name__ == "__main__":
    sys.exit(main())
