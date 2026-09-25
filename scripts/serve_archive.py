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
    GET /api/analysis         LLM 分析结果 (id / ids / tiers / list，默认返回进度+配置)
    GET /api/emotion          按周的情绪/价值时间线 (from/to)
    GET /api/llm/config       LLM 接口配置（脱敏，不含 key）+ 预设列表
    POST /api/llm/config      保存 LLM 接口配置到 work/llm.json
    GET /api/interpret        装配「值得分析的情感」语料（脱敏，供解读用）
    POST /api/llm             转发到上游 /chat/completions（stream=true 时 SSE 流式）
    GET  /api/summary         主题阶段总结（按主题口径聚合分析结果，确定性生成）
    GET  /api/reports         已保存的智能解读列表
    POST /api/reports         保存 / 删除一条智能解读 (remove=id)

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
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit, urlparse

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "out" / "archive.sqlite"
DEFAULT_WEB = ROOT / "web"
DEFAULT_WORK = ROOT / "work"

sys.path.insert(0, str(ROOT / "tools"))
import arclib  # noqa: E402  (共享数据层，CLI 与 Web 共用)
import analysis  # noqa: E402  (LLM 分析结果库)
import llm  # noqa: E402  (只读配置用于展示，不会联网)
import sanitize  # noqa: E402  (送 LLM 前的敏感词脱敏)

DB_PATH = DEFAULT_DB
WEB_DIR = DEFAULT_WEB
WORK_DIR = DEFAULT_WORK
ANALYSIS_DB = ROOT / "work" / "analysis.sqlite"
ANALYSIS_DB_PATH = ANALYSIS_DB
TZ = timezone(timedelta(hours=8))

MAX_LIMIT = 200
REPORT_MAX = 60
LLM_TIMEOUT = 600
INTERPRET_MAX = 40000
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
# LLM 分析 / 情绪时间线
# --------------------------------------------------------------------------

def _analysis_conn():
    """打开分析库；还没跑过 analyze.py 时返回 None。"""
    if not ANALYSIS_DB_PATH.is_file():
        return None
    return analysis.open_analysis_db(str(ANALYSIS_DB_PATH), readonly=True)


def _analysis_run():
    """读 tools/analyze.py 写的 work/analysis_progress.json，描述后台分析任务。

    running=true 但超过 90 秒没更新 → alive=False（心跳 20 秒一次，说明任务挂了）。
    """
    p = WORK_DIR / "analysis_progress.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    age = None
    try:
        ts = datetime.fromisoformat(str(d.get("updated") or ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = max(0.0, (datetime.now(timezone.utc) - ts).total_seconds())
    except ValueError:
        pass
    d["age_s"] = round(age, 1) if age is not None else None
    running = bool(d.get("running"))
    d["alive"] = running and (age is None or age <= 90)
    d["stale"] = running and age is not None and age > 90
    return d


def api_analysis(conn, qs):
    """LLM 分析结果查询。

    ?id=<cid>     单个对话的分析结果
    ?ids=a,b,c    批量（用于列表页显示价值/情绪）
    ?tiers=1      高/中/低三档 id 列表
    ?list=1       所有分析结果（分页截断）
    默认          分析进度 + LLM 配置（不联网）
    """
    adb = _analysis_conn()
    if adb is None:
        return {"available": False,
                "error": "还没有分析数据，先跑 python3 tools/analyze.py run"}
    try:
        cid = (qs.get("id", [""])[0] or "").strip()
        ids = (qs.get("ids", [""])[0] or "").strip()
        if cid:
            return {"available": True, "record": analysis.get(adb, cid)}
        if ids:
            wanted = [x.strip() for x in ids.split(",") if x.strip()][:MAX_LIMIT]
            recs = {}
            for c in wanted:
                r = analysis.get(adb, c)
                if r:
                    recs[c] = r
            return {"available": True, "records": recs}
        if qs.get("tiers", [""])[0]:
            limit = min(MAX_LIMIT, max(1, int(qs.get("limit", ["30"])[0] or 30)))
            tiers = analysis.value_tiers(adb)
            return {"available": True,
                    "counts": {k: len(v) for k, v in tiers.items()},
                    "high": tiers["high"][:limit], "mid": tiers["mid"][:limit],
                    "low": tiers["low"][:limit]}
        if qs.get("list", [""])[0]:
            limit = min(MAX_LIMIT, max(1, int(qs.get("limit", ["50"])[0] or 50)))
            return {"available": True, "records": analysis.all_records(adb)[:limit]}
        st = analysis.status(adb)
        total = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        st["total_conversations"] = total
        st["pending"] = max(0, total - (st.get("analyzed") or 0))
        st["by_model"] = {r["model"] or "?": r["c"] for r in adb.execute(
            "SELECT model, COUNT(*) c FROM analysis GROUP BY model")}
        st["llm_analyzed"] = adb.execute(
            "SELECT COUNT(*) c FROM analysis WHERE model<>'heuristic'").fetchone()["c"]
        return {"available": True, "status": st, "run": _analysis_run(),
                "config": llm.describe(llm.load_config())}
    finally:
        adb.close()


def api_emotion(conn, qs):
    """按周聚合的情绪/价值时间线（from/to 可选，格式 YYYY-MM-DD）。"""
    adb = _analysis_conn()
    if adb is None:
        return {"available": False, "bucket": "week", "weeks": [],
                "emotion_labels": analysis.EMOTION_LABELS,
                "error": "还没有分析数据，先跑 python3 tools/analyze.py run"}
    try:
        weeks = analysis.weekly(
            adb,
            date_from=(qs.get("from", [""])[0] or "").strip() or None,
            date_to=(qs.get("to", [""])[0] or "").strip() or None,
            prompt_version=(qs.get("prompt_version", [""])[0] or "").strip() or None)
        return {"available": True, "bucket": "week", "count": len(weeks),
                "emotion_labels": analysis.EMOTION_LABELS, "weeks": weeks}
    finally:
        adb.close()


# --------------------------------------------------------------------------
# 主题阶段总结（确定性统计聚合，不联网）
# --------------------------------------------------------------------------

SUMMARY_TOPICS = {
    "self-psych": "心理与自我主题",
}
SUMMARY_TITLE_KWS = ["抑郁", "分析", "存在", "哲学", "睡不着", "情绪",
                     "死", "焦虑", "自我", "失眠", "创伤", "压力"]
SUMMARY_REASON_KWS = ["心理", "深度", "知识", "哲学", "防御", "创伤",
                      "失眠", "情绪宣泄", "一次"]
SUMMARY_SLEEP_KWS = ["睡眠", "睡不着", "失眠"]


def _topic_rows(conn, adb, topic):
    """与 tools/analyze.py 同口径：标题+首条用户消息命中主题词，或 kind='情绪'。"""
    if topic not in SUMMARY_TOPICS:
        return None, "未知主题: %s" % topic
    try:
        import analyze as analyze_cli  # noqa: PLC0415  (tools/analyze.py)
        pattern = analyze_cli.TOPIC_PRESETS.get(topic)
    except Exception:  # noqa: BLE001
        pattern = None
    if not pattern:
        return None, "未知主题: %s" % topic
    pat = re.compile(pattern, re.I)
    first = {}
    for cid, text in conn.execute(
            "SELECT conversation_id, text FROM messages WHERE role = 'user'"
            " ORDER BY conversation_id, message_index"):
        if cid not in first:
            first[cid] = text or ""
    try:
        emo = {r[0] for r in adb.execute(
            "SELECT conversation_id FROM analysis WHERE kind = '情绪'")}
    except Exception:  # noqa: BLE001
        emo = set()
    ids, hit_kw, hit_emo = [], 0, 0
    for cid, title in conn.execute(
            "SELECT conversation_id, title FROM conversations ORDER BY created_at"):
        kw = bool(pat.search((title or "") + "\n" + first.get(cid, "")[:800]))
        eh = cid in emo
        if kw or eh:
            ids.append(cid)
            hit_kw += int(kw)
            hit_emo += int(eh)
    return {"ids": ids, "hit_kw": hit_kw, "hit_emo": hit_emo}, None


def _topic_summary_text(topic, stats, extra):
    """把统计结果渲染成一段可直接阅读/保存的中文解读。"""
    label = SUMMARY_TOPICS[topic]
    L = []
    add = L.append
    add("【%s · 阶段总结】" % label)
    add("口径：按「%s」主题口径（标题/首条消息关键词命中，或启发式分类为情绪类）"
        "从分析库聚合，共命中 %d 条对话，完成 LLM 分析 %d 条，未分析 %d 条。"
        % (topic, stats["selected"], stats["n"], stats["pending"]))
    add("")
    add("一、总量与性质")
    add("· 价值均值 %.2f / 5；值得保留 %d 条（%.0f%%）。"
        % (stats["value_mean"], stats["keep_n"], stats["keep_rate"] * 100))
    add("· 价值分布：5 分 %d、4 分 %d、3 分 %d、2 分 %d、1 分 %d。"
        % tuple(stats["value_dist"][v] for v in (5, 4, 3, 2, 1)))
    add("· 类型分布：%s。" % "、".join(
        "%s %d（%s）" % (k, c, ("%.1f%%" if c * 100.0 / stats["n"] < 1 else "%.0f%%")
                          % (c * 100.0 / stats["n"])) for k, c in stats["kinds"]))
    add("· 按类型均值：%s。" % "、".join(stats["kind_means"]))
    add("· 评分理由高频词：%s → 主体是「把自身状态理论化」的认知性自我剖析，"
        "而非单纯宣泄。" % "、".join(
            "%s %d" % (k, c) for k, c in stats["reason_kws"]))
    add("")
    add("二、情绪指标（0-1 均值）")
    add("· %s。" % stats["emo_line"])
    add("· 强度均值 %.2f（最高 %.2f），总体 sentiment %+.2f。"
        % (stats["intensity_mean"], stats["intensity_max"], stats["sentiment_mean"]))
    add("· 情绪类对话 sentiment %+.2f，非情绪类 %+.2f —— 谈情绪时明显更低落。"
        % (stats["emo_kind_sent"], stats["other_kind_sent"]))
    add("")
    add("三、时间趋势")
    for line in stats["trend_lines"]:
        add("· " + line)
    add("· 月度条数：%s。" % stats["month_counts_line"])
    add("· 低落对话（sentiment < -0.3）共 %d 条，%d 条集中在 %s，%s 单月最多（%d 条）。"
        % (stats["low_n"], stats["low_in_span"], stats["low_span"],
           stats["low_peak_month"], stats["low_peak_n"]))
    add("")
    add("四、反复出现的主题")
    add("· 标签 Top：%s。" % "、".join("%s %d" % (k, c) for k, c in stats["topics"]))
    add("· 标题高频词：%s。" % "、".join(
        "%s %d" % (k, c) for k, c in stats["title_kws"]))
    add("")
    add("五、注意点")
    for i, line in enumerate(stats["notes"], 1):
        add("%d. %s" % (i, line))
    add("")
    add("六、口径说明")
    add("· 模型打分，只反映对话内容的自我剖析密度与情绪倾向，不构成任何临床判断。")
    add("· 反复分析自身状态可能自我强化焦虑，建议留白、把结论落到具体行动上。")
    add("· 生成于 %s（服务端统计，不联网）。" % extra["now"])
    return "\n".join(L)


def api_topic_summary(conn, qs):
    """主题阶段总结：按主题口径聚合分析结果，生成文字解读（确定性、不联网）。

    ?topic=self-psych   主题（默认）
    """
    from collections import Counter  # noqa: PLC0415

    topic = (qs.get("topic", [""])[0] or "self-psych").strip()
    adb = _analysis_conn()
    if adb is None:
        return {"ok": False,
                "error": "还没有分析数据，先跑 python3 tools/analyze.py run"}
    try:
        sel, err = _topic_rows(conn, adb, topic)
        if err:
            return {"ok": False, "error": err}
        ids = sel["ids"]
        if not ids:
            return {"ok": False, "error": "主题 %s 没有命中任何对话" % topic}
        ph = ",".join("?" * len(ids))
        rows = [dict(r) for r in adb.execute(
            "SELECT * FROM analysis WHERE conversation_id IN (%s)" % ph, ids)]
        for r in rows:  # topics / emotions 在库里是 JSON 字符串
            for k, fb in (("topics", []), ("emotions", {})):
                v = r.get(k)
                if isinstance(v, str):
                    try:
                        r[k] = json.loads(v)
                    except ValueError:
                        r[k] = fb
        recs = [r for r in rows if (r.get("model") or "") != "heuristic"]
        pending = len(ids) - len(recs)
        if not recs:
            return {"ok": False, "error": "该主题还没有 LLM 分析结果"}
        n = len(recs)
        vals = [int(r.get("value") or 0) for r in recs]
        vdist = Counter(vals)
        keep_n = sum(1 for r in recs if r.get("keep"))
        kind_c = Counter((r.get("kind") or "其他") for r in recs)
        kinds = kind_c.most_common(6)
        kind_means = []
        for k, _c in kinds:
            kv = [int(r.get("value") or 0) for r in recs
                  if (r.get("kind") or "其他") == k]
            kind_means.append("%s %.2f" % (k, sum(kv) / len(kv)))
        emo_kind = [r for r in recs if (r.get("kind") or "") == "情绪"]
        other_kind = [r for r in recs if (r.get("kind") or "") != "情绪"]
        sent = lambda rs: (sum(float(r.get("sentiment") or 0) for r in rs) / len(rs)
                           if rs else 0.0)  # noqa: E731
        emo_means = []
        for e in analysis.EMOTIONS:
            tot = c = 0.0
            for r in recs:
                v = (r.get("emotions") or {}).get(e)
                if v is not None:
                    tot += float(v)
                    c += 1
            emo_means.append((e, tot / c if c else 0.0))
        order = sorted(emo_means, key=lambda x: -x[1])
        emo_line = " > ".join("%s %.2f" % (analysis.EMOTION_LABELS[e], m)
                              for e, m in order)
        intens = [float(r.get("intensity") or 0) for r in recs]

        months = {}
        for r in recs:
            months.setdefault((r.get("conv_created_at") or "")[:7], []).append(r)
        def month_mean(key, rs):
            return (sum(float(r.get(key) or 0) for r in rs) / len(rs)) if rs else 0.0
        dense = {k: v for k, v in months.items() if k and len(v) >= 5}
        def emean(month_key, e):
            rs = dense[month_key]
            vals2 = [float((r.get("emotions") or {}).get(e) or 0) for r in rs]
            return sum(vals2) / len(vals2) if vals2 else 0.0
        first_m = min(dense) if dense else None
        last_m = max(dense) if dense else None
        trend = []
        for e, lab in (("anxiety", "焦虑"), ("fatigue", "疲惫")):
            if dense:
                pk = max(dense, key=lambda k: emean(k, e))
                trend.append("%s：%s %.2f → 峰值 %s %.2f → 最近 %s %.2f"
                             % (lab, first_m, emean(first_m, e), pk, emean(pk, e),
                                last_m, emean(last_m, e)))
        if dense:
            pk_s = min(dense, key=lambda k: month_mean("sentiment", dense[k]))
            later = {k: v for k, v in dense.items() if k > pk_s} or dense
            up = max(later, key=lambda k: month_mean("sentiment", later[k]))
            trend.append("心情（sentiment）：最早 %s %+.2f → 低谷 %s %+.2f"
                         " → 回升高点 %s %+.2f → 最近 %s %+.2f"
                         % (first_m, month_mean("sentiment", dense[first_m]),
                            pk_s, month_mean("sentiment", dense[pk_s]),
                            up, month_mean("sentiment", dense[up]),
                            last_m, month_mean("sentiment", dense[last_m])))
        mcount = sorted((k, len(v)) for k, v in months.items() if k)
        month_counts_line = "、".join("%s %d" % (k, c) for k, c in mcount)

        low = [r for r in recs if float(r.get("sentiment") or 0) < -0.3]
        low_months = Counter((r.get("conv_created_at") or "")[:7] for r in low)
        if low_months:
            peak_m = low_months.most_common(1)[0][0]
            near = [m for m in low_months
                    if abs(int(m[5:7]) - int(peak_m[5:7])) <= 2
                    and m[:4] == peak_m[:4]] or [peak_m]
            span = "%s ~ %s" % (min(near), max(near))
            in_span = sum(low_months[m] for m in near)
        else:
            peak_m = span = "-"
            in_span = 0

        topics = Counter()
        for r in recs:
            for t in (r.get("topics") or []):
                topics[t] += 1
        title_kws = [(k, sum(1 for r in recs if k in (r.get("title") or "")))
                     for k in SUMMARY_TITLE_KWS]
        title_kws = [(k, c) for k, c in title_kws if c]
        title_kws.sort(key=lambda x: -x[1])
        reason_kws = [(k, sum(1 for r in recs
                              if k in (r.get("value_reason") or "")))
                      for k in SUMMARY_REASON_KWS]
        reason_kws = [(k, c) for k, c in reason_kws if c]
        reason_kws.sort(key=lambda x: -x[1])
        sleep_n = sum(1 for r in recs
                      if any(k in (r.get("title") or "") for k in SUMMARY_SLEEP_KWS))
        psych = re.compile(r"心理|自我|存在|抑郁|焦虑|哲学|情绪|创伤|人格")
        v5 = [r for r in recs if int(r.get("value") or 0) >= 5]
        v5.sort(key=lambda r: (0 if psych.search(r.get("title") or "") else 1,
                               -(int(r.get("value") or 0))))
        lowv = [r for r in recs if int(r.get("value") or 0) <= 2]
        try:
            filtered = adb.execute(
                "SELECT COUNT(*) c FROM analysis_errors WHERE conversation_id"
                " IN (%s)" % ph, ids).fetchone()["c"]
        except Exception:  # noqa: BLE001
            filtered = 0
        try:
            rows_err = adb.execute(
                "SELECT error FROM analysis_errors WHERE conversation_id IN (%s)"
                % ph, ids).fetchall()
            filt_kw = sum(1 for r in rows_err
                          if re.search(r"1301|内容|content", r["error"] or "", re.I))
        except Exception:  # noqa: BLE001
            filt_kw = 0

        notes = []
        if sleep_n:
            notes.append("睡眠相关约 %d 条（标题含睡眠/睡不着/失眠），多为低价值求助型话题，"
                         "情绪上以疲惫为主。" % sleep_n)
        if dense and low_months:
            notes.append("%s 是低谷期（低落对话 %d 条集中于此），%s 起心情回升（%s %+.2f → %s %+.2f）。"
                         % (span, in_span, up,
                            pk_s, month_mean("sentiment", dense[pk_s]),
                            up, month_mean("sentiment", dense[up])))
        if v5:
            notes.append("高分段集中在「把自身状态理论化」的 %d 条 5 分对话，例如：%s。"
                         % (len(v5), "、".join(
                             (r.get("title") or "?")[:24] for r in v5[:8])))
        if lowv:
            notes.append("低价值 %d 条（≤2 分）多为失眠求助 / 琐碎查询 / 纯宣泄，"
                         "例如：%s。" % (len(lowv), "、".join(
                             (r.get("title") or "?")[:24] for r in lowv[:5])))
        if filt_kw:
            notes.append("%d 条对话因上游内容过滤（HTTP 400 code 1301）未能分析，未纳入本总结。"
                         % filt_kw)
        elif filtered:
            notes.append("%d 条对话分析出错未纳入本总结。" % filtered)

        stats = {
            "selected": len(ids), "n": n, "pending": pending,
            "value_mean": sum(vals) / n, "value_dist": vdist,
            "keep_n": keep_n, "keep_rate": keep_n / n,
            "kinds": kinds, "kind_means": kind_means,
            "reason_kws": reason_kws[:8],
            "emo_line": emo_line,
            "intensity_mean": sum(intens) / n,
            "intensity_max": max(intens),
            "sentiment_mean": sent(recs),
            "emo_kind_sent": sent(emo_kind), "other_kind_sent": sent(other_kind),
            "trend_lines": trend,
            "month_counts_line": month_counts_line,
            "low_n": len(low), "low_in_span": in_span, "low_span": span,
            "low_peak_month": peak_m,
            "low_peak_n": low_months.get(peak_m, 0) if low_months else 0,
            "topics": topics.most_common(12),
            "title_kws": title_kws[:12],
            "notes": notes,
        }
        extra = {"now": datetime.now(TZ).strftime("%Y-%m-%d %H:%M")}
        text = _topic_summary_text(topic, stats, extra)
        return {"ok": True, "topic": topic, "label": SUMMARY_TOPICS[topic],
                "count": n, "selected": len(ids), "pending": pending,
                "title": "%s · 阶段总结" % SUMMARY_TOPICS[topic],
                "content": text,
                "stats": {"n": n, "value_mean": round(stats["value_mean"], 2),
                          "keep_rate": round(stats["keep_rate"], 3),
                          "low_n": len(low), "topics": stats["topics"][:8]}}
    finally:
        adb.close()


# --------------------------------------------------------------------------
# LLM 接口配置 / 智能解读 / 已存报告
# --------------------------------------------------------------------------

def api_llm_config(conn, qs):
    """LLM 接口配置（脱敏）。永远不返回 key 本身。"""
    out = llm.describe(llm.load_config())
    out.update({"ok": True, "presets": llm.presets(),
                "work_config": str(llm.CONFIG_PATH)})
    return out


def api_llm_config_save(payload):
    """保存 LLM 接口配置到 work/llm.json（原子写入，权限 600）。"""
    payload = payload or {}
    updates = {}
    for k in ("preset", "base_url", "model", "api_key_env", "temperature",
              "max_tokens", "timeout"):
        if k in payload:
            updates[k] = payload[k]
    if payload.get("removeKey"):
        updates["api_key"] = ""
    elif payload.get("api_key"):
        updates["api_key"] = str(payload["api_key"]).strip()

    base = (updates.get("base_url") or "").strip()
    if base:
        if not (base.startswith("http://") or base.startswith("https://")):
            return {"ok": False, "error": "base_url 需以 http:// 或 https:// 开头"}, 400
        try:
            base.encode("ascii")
        except UnicodeEncodeError:
            return {"ok": False, "error": "base_url 只能包含 ASCII 字符"}, 400
    for k in ("temperature", "max_tokens", "timeout"):
        if k in updates and updates[k] != "" and updates[k] is not None:
            try:
                updates[k] = float(updates[k]) if k == "temperature" else int(updates[k])
            except (TypeError, ValueError):
                return {"ok": False, "error": "%s 不是合法数字" % k}, 400
    if "temperature" in updates and not (0 <= updates["temperature"] <= 2):
        return {"ok": False, "error": "temperature 需在 0~2 之间"}, 400
    if "max_tokens" in updates and updates["max_tokens"] < 1:
        return {"ok": False, "error": "max_tokens 需 >= 1"}, 400

    try:
        llm.save_config(updates)
    except OSError as e:
        return {"ok": False, "error": "写入配置失败: %s" % e}, 500
    out = llm.describe(llm.load_config())
    out.update({"ok": True, "saved": True, "presets": llm.presets()})
    return out, 200


def _reports_path():
    return WORK_DIR / "reports.json"


def _load_reports():
    p = _reports_path()
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save_reports(items):
    p = _reports_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items[:REPORT_MAX], ensure_ascii=False, indent=2),
                   encoding="utf-8")
    os.replace(tmp, p)


def api_reports(conn, qs):
    return {"ok": True, "reports": _load_reports()}


def api_reports_save(payload):
    payload = payload or {}
    items = _load_reports()
    if payload.get("remove"):
        rid = str(payload["remove"])
        items = [r for r in items if str(r.get("id")) != rid]
        _save_reports(items)
        return {"ok": True, "removed": rid, "reports": items}, 200
    content = (payload.get("content") or "").strip()
    if not content:
        return {"ok": False, "error": "content 不能为空"}, 400
    rec = {
        "id": uuid.uuid4().hex[:12],
        "time": now_iso(),
        "title": (payload.get("title") or "").strip()[:200] or "未命名解读",
        "model": (payload.get("model") or "").strip()[:100],
        "scope": (payload.get("scope") or "").strip()[:40],
        "count": payload.get("count"),
        "prompt": (payload.get("prompt") or "").strip()[:500],
        "content": content,
    }
    items.insert(0, rec)
    _save_reports(items)
    return {"ok": True, "report": rec, "reports": items}, 200


def _interpret_ids(conn, adb, scope, limit, collection):
    """按数据源挑对话 id。emotion = 只挑「值得分析的情感」。"""
    if scope == "emotion":
        return [r["conversation_id"]
                for r in analysis.emotional_records(adb, limit=limit)]
    if scope == "high":
        return analysis.value_tiers(adb)["high"][:limit]
    if scope == "collection":
        ids = arclib.selected_ids(collection or "default")
        return ids[:limit]
    if scope == "recent":
        rows = conn.execute(
            "SELECT conversation_id FROM conversations "
            "ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [r[0] for r in rows]
    return [r["conversation_id"] for r in analysis.all_records(adb)[:limit]]


def api_interpret(conn, qs):
    """装配给 LLM 的语料：默认只含「值得分析的情感」，且已做敏感词脱敏。"""
    adb = _analysis_conn()
    if adb is None:
        return {"ok": False,
                "error": "还没有分析数据，先跑 python3 tools/analyze.py heuristic --all"}
    try:
        scope = (qs.get("scope", ["emotion"])[0] or "emotion").strip()
        collection = (qs.get("collection", ["default"])[0] or "default").strip()
        limit = min(60, max(1, int(qs.get("limit", ["20"])[0] or 20)))
        max_chars = min(INTERPRET_MAX, max(1000, int(qs.get("max_chars", ["12000"])[0] or 12000)))
        cids = _interpret_ids(conn, adb, scope, limit, collection)

        parts, items, used = [], [], 0
        for i, cid in enumerate(cids, 1):
            d = analysis.build_digest(conn, cid, max_chars=800, max_msgs=12, msg_chars=240)
            if not d:
                continue
            rec = analysis.get(adb, cid) or {}
            meta = []
            if d.get("created_at"):
                meta.append(str(d["created_at"])[:10])
            if rec.get("value") is not None:
                meta.append("价值%d" % rec["value"])
            if rec.get("kind"):
                meta.append(str(rec["kind"]))
            if rec.get("sentiment") is not None:
                meta.append("基调%+.2f" % float(rec["sentiment"]))
            if rec.get("intensity") is not None:
                meta.append("强度%.2f" % float(rec["intensity"]))
            head = "#%d %s" % (i, d.get("title") or cid)
            if meta:
                head += "（%s）" % "，".join(meta)
            block = head + "\n" + (d.get("transcript") or "")
            if used + len(block) > max_chars:
                parts.append("…（已达长度上限，共选取 %d 个对话）" % len(items))
                break
            parts.append(block)
            used += len(block)
            items.append({
                "conversation_id": cid,
                "title": d.get("title"),
                "created_at": d.get("created_at"),
                "value": rec.get("value"),
                "kind": rec.get("kind"),
                "sentiment": rec.get("sentiment"),
                "intensity": rec.get("intensity"),
                "summary": rec.get("summary"),
            })

        return {
            "ok": True, "scope": scope, "count": len(items), "chars": used,
            "masked": sanitize.active(),
            "context": "\n\n".join(parts), "items": items,
        }
    finally:
        adb.close()


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

    def _same_origin(self):
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            netloc = urlsplit(origin).netloc
        except ValueError:
            return False
        if netloc in (self.headers.get("Host", ""), "localhost", "127.0.0.1"):
            return True
        return netloc.split(":")[0] in ("localhost", "127.0.0.1")

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(length) if length > 0 else b""
        if not raw:
            return {}
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise ValueError("请求体不是合法 JSON: %s" % e)
        if not isinstance(payload, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return payload

    def post_llm(self, payload):
        """转发到上游 OpenAI 兼容 /chat/completions；stream=true 时走 SSE。"""
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            return self.send_json({"ok": False, "error": "messages 不能为空"}, 400)
        cfg = llm.load_config()
        if not llm.has_key(cfg):
            return self.send_json(
                {"ok": False, "error": "没有可用的 API key",
                 "hint": "在「接口配置」里填 key，或设置环境变量 %s"
                         % cfg.get("api_key_env")}, 503)

        stream = bool(payload.get("stream"))
        body = {"model": payload.get("model") or cfg.get("model"),
                "messages": messages, "stream": stream}
        for k in ("temperature", "max_tokens"):
            v = payload.get(k)
            if v is None:
                v = cfg.get(k)
            if v is not None and v != "":
                body[k] = v
        body.update(cfg.get("extra_payload") or {})

        url = llm._endpoint(cfg["base_url"])
        req = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     "Accept": "text/event-stream" if stream else "application/json",
                     "Authorization": "Bearer %s" % llm.resolve_api_key(cfg)})
        try:
            resp = urllib.request.urlopen(req, timeout=LLM_TIMEOUT)
        except urllib.error.HTTPError as e:
            try:
                detail = e.read().decode("utf-8", "replace")[:2000]
            except Exception:  # noqa: BLE001
                detail = ""
            return self.send_json(
                {"ok": False, "error": "上游返回 %s" % e.code,
                 "detail": detail, "url": url}, 502)
        except Exception as e:  # noqa: BLE001
            return self.send_json(
                {"ok": False, "error": "%s: %s" % (type(e).__name__, e),
                 "url": url}, 502)

        with resp:
            if not stream:
                data = resp.read()
                return self.send_text(data, ctype="application/json; charset=utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

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
            if path == "/api/summary":
                return self.send_json(api_topic_summary(conn, qs))
            if path == "/api/analysis":
                return self.send_json(api_analysis(conn, qs))
            if path == "/api/emotion":
                return self.send_json(api_emotion(conn, qs))
            if path == "/api/llm/config":
                return self.send_json(api_llm_config(conn, qs))
            if path == "/api/interpret":
                return self.send_json(api_interpret(conn, qs))
            if path == "/api/reports":
                return self.send_json(api_reports(conn, qs))
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
            if not self._same_origin():
                return self.send_json({"error": "跨站请求被拒绝"}, 403)
            if path == "/api/llm":
                try:
                    payload = self._read_json()
                except ValueError as e:
                    return self.send_json({"error": str(e)}, 400)
                return self.post_llm(payload)
            try:
                payload = self._read_json()
            except ValueError as e:
                return self.send_json({"error": str(e)}, 400)
            if path == "/api/selection":
                data, status = api_selection_add(payload)
                return self.send_json(data, status)
            if path == "/api/llm/config":
                data, status = api_llm_config_save(payload)
                return self.send_json(data, status)
            if path == "/api/reports":
                data, status = api_reports_save(payload)
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
    global DB_PATH, WEB_DIR, WORK_DIR, TZ_MOD, ANALYSIS_DB_PATH
    ap = argparse.ArgumentParser(description="AI 聊天归档浏览服务")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="archive.sqlite 路径")
    ap.add_argument("--analysis-db", default=str(ANALYSIS_DB),
                    help="analysis.sqlite 路径 (默认 work/analysis.sqlite)")
    ap.add_argument("--work", default=str(DEFAULT_WORK),
                    help="工作目录 (reports.json 等，默认 work)")
    ap.add_argument("--web", default=str(DEFAULT_WEB), help="前端静态目录")
    ap.add_argument("--host", default="0.0.0.0", help="监听地址 (默认 0.0.0.0)")
    ap.add_argument("--port", type=int, default=8765, help="端口 (默认 8765)")
    ap.add_argument("--tz-offset", type=float, default=8.0, help="展示时区偏移 (默认 8)")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印访问日志")
    args = ap.parse_args()

    DB_PATH = Path(args.db).resolve()
    WEB_DIR = Path(args.web).resolve()
    WORK_DIR = Path(args.work).resolve()
    ANALYSIS_DB_PATH = Path(args.analysis_db).resolve()
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
