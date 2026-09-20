#!/usr/bin/env python3
"""AI 聊天归档的共享数据层（纯标准库）。

CLI（tools/archive_cli.py）和 Web 服务（scripts/serve_archive.py）都通过这里
访问 out/archive.sqlite，保证两边行为一致。

设计要点
--------
* 默认只读：open_db() 带 PRAGMA query_only。
* 中文检索：FTS5 用的是 unicode61 分词器，会把一整串中文当成一个词，
  所以「学习」这类词几乎搜不到（实测漏检 91%）。这里对含中文的查询走
  LIKE 全表扫描 + Python 打分（实测 48M 字符全表约 0.24s，零额外磁盘），
  纯 ASCII 查询才走 FTS5（保留 BM25 排序）。
* 选集：append-only JSONL（work/selection.jsonl），掉电最多丢最后一行。
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "out" / "archive.sqlite"
DEFAULT_WORK = ROOT / "work"
SELECTION_FILE = DEFAULT_WORK / "selection.jsonl"

TZ = timezone(timedelta(hours=8))

# LIKE 路径一次最多取回多少条候选做打分（防止「的」这种 5 万命中打爆内存）
CANDIDATE_CAP = 4000

# 含这些范围的字符就认为需要走 LIKE（CJK 汉字/假名/谚文/兼容区）
_CJK = re.compile(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff\uff66-\uff9f]")

_QUOTED = re.compile(r'"([^"]+)"')
_FIELD = re.compile(r"^[A-Za-z_]+:")
_STOP = {"AND", "OR", "NOT", "NEAR"}


# --------------------------------------------------------------------------
# 基础设施
# --------------------------------------------------------------------------

def open_db(path=None, readonly=True):
    conn = sqlite3.connect(str(path or DEFAULT_DB), timeout=10.0)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only = 1")
    return conn


def local_tz_offset():
    return TZ.utcoffset(None).total_seconds() / 3600.0


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(s):
    """'YYYY-MM-DD' -> 当天 00:00 的 UTC ISO 字符串（本地时区 UTC+8）。"""
    try:
        d = datetime.strptime((s or "").strip(), "%Y-%m-%d")
    except (ValueError, AttributeError):
        return None
    return d.replace(tzinfo=TZ).astimezone(timezone.utc).isoformat(timespec="seconds")


def _date_lt(s):
    """'YYYY-MM-DD' -> 次日 00:00 的 UTC ISO（用于 to 的右开区间）。"""
    try:
        d = datetime.strptime((s or "").strip(), "%Y-%m-%d") + timedelta(days=1)
    except (ValueError, AttributeError):
        return None
    return d.replace(tzinfo=TZ).astimezone(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# 查询串解析
# --------------------------------------------------------------------------

def has_cjk(s):
    return bool(_CJK.search(s or ""))


def parse_terms(q):
    """把用户输入拆成词条列表。

    支持 "带空格的短语"；忽略 field: 前缀、开头的 -/+、结尾的 *；
    丢掉 AND/OR/NOT；去重且保持顺序。
    """
    q = (q or "").strip()
    if not q:
        return []
    terms = [m.group(1).strip() for m in _QUOTED.finditer(q)]
    for tok in _QUOTED.sub(" ", q).split():
        tok = _FIELD.sub("", tok).lstrip("-+").rstrip("*")
        if not tok or tok.upper() in _STOP:
            continue
        terms.append(tok)
    out, seen = [], set()
    for t in terms:
        if not t:
            continue
        k = t.lower()
        if k not in seen:
            seen.add(k)
            out.append(t)
    return out


def _like_escape(s):
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# --------------------------------------------------------------------------
# 片段 / 高亮
# --------------------------------------------------------------------------

def _esc(s):
    return html.escape(s or "", quote=False)


def mark_html(seg, terms):
    """在已截断的纯文本里把词条包成 <mark>，其余部分做 HTML 转义。"""
    if not seg:
        return ""
    pats = sorted({re.escape(t) for t in terms if t}, key=len, reverse=True)
    if not pats:
        return _esc(seg)
    rx = re.compile("|".join(pats), re.IGNORECASE)
    out, last = [], 0
    for m in rx.finditer(seg):
        out.append(_esc(seg[last:m.start()]))
        out.append("<mark>%s</mark>" % _esc(m.group(0)))
        last = m.end()
    out.append(_esc(seg[last:]))
    return "".join(out)


def first_hit(text, terms):
    """返回 (位置, 命中的词) —— 取所有词条里最靠前的一个；没有则 (-1, None)。"""
    if not text:
        return -1, None
    low = text.lower()
    best, bt = -1, None
    for t in terms:
        if not t:
            continue
        p = low.find(t.lower())
        if p >= 0 and (best < 0 or p < best):
            best, bt = p, t
    return best, bt


def make_snippet(text, terms, width=180, html_out=True):
    """围绕第一个命中词截一段，可选输出带 <mark> 的 HTML。"""
    text = text or ""
    if not text.strip():
        return ""
    pos, _ = first_hit(text, terms)
    if pos < 0:
        pos = 0
    start = max(0, pos - width // 3)
    end = min(len(text), start + width)
    seg = text[start:end]
    if start > 0:
        seg = "…" + seg
    if end < len(text):
        seg = seg + "…"
    seg = re.sub(r"\s+", " ", seg).strip()
    return mark_html(seg, terms) if html_out else seg


# --------------------------------------------------------------------------
# 搜索
# --------------------------------------------------------------------------

def _where_common(role=None, source=None, date_from=None, date_to=None):
    where, args = [], []
    if role in ("user", "assistant", "system"):
        where.append("m.role = ?")
        args.append(role)
    if source:
        where.append("c.source = ?")
        args.append(source)
    frm = parse_date(date_from)
    if frm:
        where.append("m.timestamp >= ?")
        args.append(frm)
    to = _date_lt(date_to)
    if to:
        where.append("m.timestamp < ?")
        args.append(to)
    return where, args


def _fts_query(terms):
    parts = []
    for i, t in enumerate(terms):
        esc = t.replace('"', '""')
        if i == len(terms) - 1 and t.isascii() and len(t) >= 2:
            parts.append('"%s"*' % esc)
        else:
            parts.append('"%s"' % esc)
    return " AND ".join(parts)


def _score_row(row, terms):
    text = row["text"] or ""
    think = row["thinking"] or ""
    title = row["title"] or ""
    low, lowt, lowtitle = text.lower(), think.lower(), title.lower()
    score = 0.0
    for t in terms:
        lt = t.lower()
        n = low.count(lt)
        if n:
            score += n
        nt = lowt.count(lt)
        if nt:
            score += nt * 0.5
        if lt in lowtitle:
            score += 3.0
    return score


def search(conn, q, role=None, source=None, date_from=None, date_to=None,
           limit=30, offset=0, cap=CANDIDATE_CAP):
    """混合检索。返回 {total, offset, limit, query, items, mode, truncated}。

    items 与旧 /api/search 契约一致：
        {id, conversation_id, message_index, role, timestamp, timestamp_source,
         title, source, snip}
    """
    raw = (q or "").strip()
    terms = parse_terms(raw)
    if not terms:
        return {"total": 0, "offset": 0, "limit": limit, "query": raw,
                "items": [], "mode": "none"}

    where, args = _where_common(role, source, date_from, date_to)
    use_fts = not any(has_cjk(t) for t in terms)

    sel = ("m.id, m.conversation_id, m.message_index, m.role, m.timestamp,"
           " m.timestamp_source, c.title, c.source, m.text, m.thinking")

    if use_fts:
        fts = _fts_query(terms)
        clause = " AND ".join(["messages_fts MATCH ?"] + where)
        base = ("FROM messages_fts"
                " JOIN messages m ON m.id = messages_fts.rowid"
                " JOIN conversations c ON c.conversation_id = m.conversation_id"
                " WHERE " + clause)
        fargs = [fts] + args
        try:
            total = conn.execute("SELECT COUNT(*) " + base, fargs).fetchone()[0]
            rows = conn.execute(
                "SELECT " + sel + " " + base +
                " ORDER BY messages_fts.rank, m.id DESC LIMIT ? OFFSET ?",
                fargs + [limit, offset]).fetchall()
        except sqlite3.OperationalError as e:
            return {"total": 0, "offset": offset, "limit": limit, "query": raw,
                    "items": [], "mode": "fts", "error": "查询语法错误: %s" % e}
        items = [_row_to_item(r, terms) for r in rows]
        return {"total": total, "offset": offset, "limit": limit,
                "query": raw, "items": items, "mode": "fts", "truncated": False}

    # ---- LIKE 路径（含中文）----
    for t in terms:
        pat = "%" + _like_escape(t) + "%"
        where.append("(m.text LIKE ? ESCAPE '\\' OR m.thinking LIKE ? ESCAPE '\\')")
        args.extend([pat, pat])
    base = ("FROM messages m"
            " JOIN conversations c ON c.conversation_id = m.conversation_id"
            " WHERE " + " AND ".join(where))
    rows = conn.execute(
        "SELECT " + sel + " " + base +
        " ORDER BY m.timestamp DESC, m.id DESC LIMIT ?", args + [cap]).fetchall()
    truncated = len(rows) >= cap
    if truncated:
        total = conn.execute("SELECT COUNT(*) " + base, args).fetchone()[0]
    else:
        total = len(rows)

    scored = [(_score_row(r, terms), r["timestamp"] or "", r) for r in rows]
    scored.sort(key=lambda x: (-x[0], x[1]))
    page = [r for _, _, r in scored[offset:offset + limit]]
    items = [_row_to_item(r, terms) for r in page]
    return {"total": total, "offset": offset, "limit": limit, "query": raw,
            "items": items, "mode": "like", "truncated": truncated}


def _row_to_item(row, terms):
    text = row["text"] or ""
    think = row["thinking"] or ""
    pos, _t = first_hit(text, terms)
    if pos >= 0:
        snip = make_snippet(text, terms)
    elif first_hit(think, terms)[0] >= 0:
        snip = "💭 " + make_snippet(think, terms)
    else:
        snip = make_snippet(text, terms)
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "message_index": row["message_index"],
        "role": row["role"],
        "timestamp": row["timestamp"],
        "timestamp_source": row["timestamp_source"],
        "title": row["title"],
        "source": row["source"],
        "snip": snip,
    }


# --------------------------------------------------------------------------
# 对话 / 统计
# --------------------------------------------------------------------------

def get_conversation(conn, cid, with_thinking=True):
    conv = conn.execute(
        "SELECT conversation_id, source, title, created_at, updated_at, message_count"
        " FROM conversations WHERE conversation_id = ?", (cid,)).fetchone()
    if not conv:
        return None
    cols = "message_index, role, timestamp, timestamp_source, model, text, thinking, attachments"
    msgs = conn.execute(
        "SELECT " + cols + " FROM messages WHERE conversation_id = ?"
        " ORDER BY message_index", (cid,)).fetchall()
    out = []
    for m in msgs:
        d = dict(m)
        try:
            d["attachments"] = json.loads(d.get("attachments") or "[]")
        except (ValueError, TypeError):
            d["attachments"] = []
        if not with_thinking:
            d.pop("thinking", None)
        out.append(d)
    return {"conversation": dict(conv), "messages": out}


def list_conversations(conn, ids):
    """按给定 id 列表取对话元信息（保持传入顺序）。"""
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT conversation_id, source, title, created_at, updated_at, message_count"
        " FROM conversations WHERE conversation_id IN (" + marks + ")", list(ids)).fetchall()
    by_id = {r["conversation_id"]: dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def stats(conn, tz_offset=None):
    """归档总览。CLI 的 stats 和 Web 的 /api/stats 共用这一份，避免两边算得不一样。

    一次调用会做几次全表聚合（LENGTH/strftime），48M 字符的库实测约 1 秒。
    """
    tz = local_tz_offset() if tz_offset is None else tz_offset
    mod = "%+g hours" % tz

    def one(sql, args=()):
        return conn.execute(sql, args).fetchone()

    def rows(sql, args=()):
        return [dict(r) for r in conn.execute(sql, args).fetchall()]

    base = one("SELECT COUNT(*) n, SUM(LENGTH(text)) chars,"
               " SUM(LENGTH(COALESCE(thinking, ''))) thinking_chars,"
               " MIN(timestamp) mn, MAX(timestamp) mx FROM messages")
    roles = dict(conn.execute("SELECT role, COUNT(*) FROM messages GROUP BY role").fetchall())
    role_chars = dict(conn.execute(
        "SELECT role, SUM(LENGTH(text)) FROM messages GROUP BY role").fetchall())
    convs = one("SELECT COUNT(*) FROM conversations")[0]

    sources = rows("SELECT source, COUNT(DISTINCT conversation_id) AS conversations,"
                   " COUNT(*) AS messages, COALESCE(SUM(LENGTH(text)), 0) AS chars"
                   " FROM messages GROUP BY source ORDER BY messages DESC")
    for s in sources:
        s["avg_messages"] = round(s["messages"] / s["conversations"], 1) if s["conversations"] else 0

    ts_sources = dict(conn.execute(
        "SELECT timestamp_source, COUNT(*) FROM messages GROUP BY timestamp_source").fetchall())

    # 时间分布（都按本地时区切）
    by_month = rows("SELECT strftime('%Y-%m', timestamp, ?) AS k, COUNT(*) AS n,"
                    " COUNT(DISTINCT conversation_id) AS c FROM messages"
                    " WHERE timestamp IS NOT NULL GROUP BY k ORDER BY k", (mod,))
    by_year = rows("SELECT strftime('%Y', timestamp, ?) AS k, COUNT(*) AS n,"
                   " COUNT(DISTINCT conversation_id) AS c FROM messages"
                   " WHERE timestamp IS NOT NULL GROUP BY k ORDER BY k", (mod,))
    by_hour = rows("SELECT strftime('%H', timestamp, ?) AS k, COUNT(*) AS n FROM messages"
                   " WHERE timestamp IS NOT NULL GROUP BY k ORDER BY k", (mod,))
    by_weekday = rows("SELECT strftime('%w', timestamp, ?) AS k, COUNT(*) AS n FROM messages"
                      " WHERE timestamp IS NOT NULL GROUP BY k ORDER BY k", (mod,))
    by_day = rows("SELECT strftime('%Y-%m-%d', timestamp, ?) AS k, COUNT(*) AS n FROM messages"
                  " WHERE timestamp IS NOT NULL GROUP BY k ORDER BY k", (mod,))

    # 星期 × 小时 热力图，7 行 24 列
    grid = [[0] * 24 for _ in range(7)]
    for r in conn.execute(
            "SELECT strftime('%w', timestamp, ?) w, strftime('%H', timestamp, ?) h,"
            " COUNT(*) n FROM messages WHERE timestamp IS NOT NULL GROUP BY w, h", (mod, mod)):
        grid[int(r["w"])][int(r["h"])] = r["n"]
    heatmap = grid

    # 长度直方图（Python 侧固定顺序，SQL 的 GROUP BY 顺序是按字符串排的）
    def bucket(buckets, rows_):
        m = {r["k"]: r["n"] for r in rows_}
        return [{"k": k, "n": m.get(k, 0)} for k, _ in buckets]

    conv_hist = bucket(
        [("1", 1), ("2-5", 5), ("6-20", 20), ("21-50", 50), ("51-200", 200), ("200+", 0)],
        rows("SELECT CASE WHEN message_count <= 1 THEN '1'"
             " WHEN message_count <= 5 THEN '2-5' WHEN message_count <= 20 THEN '6-20'"
             " WHEN message_count <= 50 THEN '21-50' WHEN message_count <= 200 THEN '51-200'"
             " ELSE '200+' END AS k, COUNT(*) AS n FROM conversations GROUP BY k"))
    msg_hist = bucket(
        [("0-100", 100), ("100-500", 500), ("500-2k", 2000), ("2k-10k", 10000), ("10k+", 0)],
        rows("SELECT CASE WHEN LENGTH(text) <= 100 THEN '0-100'"
             " WHEN LENGTH(text) <= 500 THEN '100-500' WHEN LENGTH(text) <= 2000 THEN '500-2k'"
             " WHEN LENGTH(text) <= 10000 THEN '2k-10k' ELSE '10k+' END AS k,"
             " COUNT(*) AS n FROM messages GROUP BY k"))

    # 只有 DeepSeek 每条都带 model；Gemini 的活动记录 HTML 根本没有模型字段，
    # 所以空 model 就退回来源名，避免整张图只有一根「未知」的柱子。
    models = rows("SELECT CASE WHEN COALESCE(model, '') = '' THEN"
                  " CASE source WHEN 'gemini' THEN 'Gemini' WHEN 'chatgpt' THEN 'ChatGPT'"
                  " WHEN 'deepseek' THEN 'DeepSeek' ELSE source END || ' (unspecified)'"
                  " ELSE model END AS k, COUNT(*) AS n FROM messages"
                  " GROUP BY k ORDER BY n DESC")
    thinking_msgs = one("SELECT COUNT(*) FROM messages WHERE COALESCE(thinking, '') != ''")[0]
    attach_msgs = one("SELECT COUNT(*) FROM messages WHERE attachments IS NOT NULL"
                      " AND attachments NOT IN ('', '[]', 'null')")[0]

    lm = rows("SELECT conversation_id, message_index, role, source, LENGTH(text) AS n,"
              " substr(text, 1, 140) AS s FROM messages ORDER BY LENGTH(text) DESC LIMIT 1")
    longest_message = lm[0] if lm else None

    # 连续活跃天数：by_day 已按日期升序
    max_streak = last_streak = 0
    prev = None
    for row in by_day:
        d = datetime.strptime(row["k"], "%Y-%m-%d").date()
        last_streak = last_streak + 1 if prev and (d - prev).days == 1 else 1
        max_streak = max(max_streak, last_streak)
        prev = d

    active_days = len(by_day)
    span_days = 0
    if base["mn"] and base["mx"]:
        span_days = max(1, (datetime.fromisoformat(base["mx"])
                            - datetime.fromisoformat(base["mn"])).days + 1)
    n = base["n"] or 0
    chars = base["chars"] or 0
    avg = lambda total, cnt: round(total / cnt, 1) if cnt else 0

    return {
        "tz_offset": tz,
        "totals": {
            "conversations": convs,
            "messages": n,
            "user_messages": roles.get("user", 0),
            "assistant_messages": roles.get("assistant", 0),
            "system_messages": roles.get("system", 0),
            "chars": chars,
            "user_chars": role_chars.get("user", 0) or 0,
            "assistant_chars": role_chars.get("assistant", 0) or 0,
            "avg_chars_message": avg(chars, n),
            "avg_chars_user": avg(role_chars.get("user", 0) or 0, roles.get("user", 0)),
            "avg_chars_assistant": avg(role_chars.get("assistant", 0) or 0,
                                       roles.get("assistant", 0)),
            "thinking_messages": thinking_msgs,
            "thinking_chars": base["thinking_chars"] or 0,
            "attachment_messages": attach_msgs,
            "active_days": active_days,
            "span_days": span_days,
            "avg_per_day": avg(n, span_days),
            "avg_per_active_day": avg(n, active_days),
            "avg_per_conversation": avg(n, convs),
            "convs_per_active_day": round(convs / active_days, 2) if active_days else 0,
            "max_streak": max_streak,
            "last_streak": last_streak,
        },
        "range": {"min": base["mn"], "max": base["mx"]},
        "busiest_day": max(by_day, key=lambda x: x["n"]) if by_day else None,
        "busiest_month": max(by_month, key=lambda x: x["n"]) if by_month else None,
        "busiest_hour": max(by_hour, key=lambda x: x["n"]) if by_hour else None,
        "busiest_weekday": max(by_weekday, key=lambda x: x["n"]) if by_weekday else None,
        "sources": sources,
        "timestamp_sources": ts_sources,
        "by_year": by_year,
        "by_month": by_month,
        "by_hour": by_hour,
        "by_weekday": by_weekday,
        "by_day": by_day,
        "heatmap": heatmap,
        "conv_hist": conv_hist,
        "msg_hist": msg_hist,
        "models": models,
        "longest_message": longest_message,
        "top_conversations": rows(
            "SELECT conversation_id, title, source, message_count, created_at, updated_at"
            " FROM conversations ORDER BY message_count DESC LIMIT 12"),
        "recent_conversations": rows(
            "SELECT conversation_id, title, source, message_count, created_at, updated_at"
            " FROM conversations ORDER BY updated_at DESC LIMIT 12"),
        "longest_conversations": rows(
            "SELECT c.conversation_id, c.title, c.source, c.message_count, c.created_at,"
            " c.updated_at, COALESCE(SUM(LENGTH(m.text)), 0) AS chars FROM conversations c"
            " LEFT JOIN messages m ON m.conversation_id = c.conversation_id"
            " GROUP BY c.conversation_id ORDER BY chars DESC LIMIT 12"),
        "span_conversations": rows(
            "SELECT conversation_id, title, source, message_count, created_at, updated_at"
            " FROM conversations"
            " ORDER BY (julianday(updated_at) - julianday(created_at)) DESC LIMIT 12"),
    }


def conversation_lengths(conn, limit=None, order="chars"):
    """每个对话的总字符数 / 消息数。order: chars | messages | updated"""
    order_by = {
        "chars": "chars DESC",
        "messages": "n DESC",
        "updated": "updated_at DESC",
    }.get(order, "chars DESC")
    sql = ("SELECT c.conversation_id, c.title, c.source, c.updated_at,"
           " COUNT(m.id) n, COALESCE(SUM(LENGTH(m.text)),0) chars"
           " FROM conversations c LEFT JOIN messages m"
           " ON m.conversation_id = c.conversation_id"
           " GROUP BY c.conversation_id ORDER BY " + order_by)
    if limit:
        sql += " LIMIT %d" % int(limit)
    return [dict(r) for r in conn.execute(sql).fetchall()]


# --------------------------------------------------------------------------
# 选集（append-only）
# --------------------------------------------------------------------------

_lock = threading.Lock()


def selection_path(work=None):
    return Path(work) / "selection.jsonl" if work else SELECTION_FILE


def append_selection(conversation_id, action="add", collection="default",
                     note=None, path=None):
    """只追加一行，flush + fsync。掉电最多丢最后一行。"""
    if action not in ("add", "remove"):
        raise ValueError("action 必须是 add 或 remove")
    rec = {"ts": now_iso(), "conversation_id": str(conversation_id),
           "action": action, "collection": collection or "default"}
    if note:
        rec["note"] = note
    p = Path(path) if path else SELECTION_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(rec, ensure_ascii=False) + "\n"
    with _lock:
        with open(p, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
    return rec


def read_selection(collection=None, path=None):
    """重放 JSONL，返回 {collection: [conversation_id, ...]}（按加入顺序）。"""
    p = Path(path) if path else SELECTION_FILE
    result = {}
    if not p.exists():
        return result
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue  # 掉电截断的残行，跳过
            col = rec.get("collection") or "default"
            if collection and col != collection:
                continue
            cid = rec.get("conversation_id")
            if not cid:
                continue
            lst = result.setdefault(col, [])
            if rec.get("action") == "add":
                if cid not in lst:
                    lst.append(cid)
            else:
                if cid in lst:
                    lst.remove(cid)
    return result


def selected_ids(collection="default", path=None):
    return read_selection(collection, path).get(collection, [])
