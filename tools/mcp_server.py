#!/usr/bin/env python3
"""AI 聊天归档的 MCP 接口（骨架，纯标准库，stdio JSON-RPC 2.0）。

只把 arclib 的读取/选集能力包成 MCP 工具，**不调用任何 LLM**。
所有返回都带硬上限，避免把整个语料塞进上下文：
  - 搜索结果只给片段（默认 10 条，最多 30 条，片段最长 300 字符）
  - 对话正文默认截断到每条 1200 字符、最多 40 条消息
  - 导出文本默认截断到 200000 字符

用法（在 MCP 客户端里配置）：
    {"command": "python3", "args": ["/home/SQL916/ai-chat-archive/tools/mcp_server.py"]}

也支持 `--db` / `--work` 指定数据库与选集目录。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import analysis  # noqa: E402
import arclib  # noqa: E402
import heur  # noqa: E402
import llm  # noqa: E402

SERVER_NAME = "ai-chat-archive"
SERVER_VERSION = "0.1.0"
PROTOCOL_VERSION = "2024-11-05"

MAX_RESULTS = 30
MAX_SNIPPET = 300
MAX_MESSAGES = 200
MAX_MSG_CHARS = 4000
MAX_EXPORT_CHARS = 200_000
MAX_DIGEST_CHARS = 8000
MAX_WEEKS = 300

DB_PATH = arclib.DEFAULT_DB
WORK_DIR = arclib.DEFAULT_WORK


# --------------------------------------------------------------------------
# 工具实现
# --------------------------------------------------------------------------

def _conn():
    return arclib.open_db(str(DB_PATH))


def tool_search(args):
    q = (args.get("query") or args.get("q") or "").strip()
    if not q:
        return {"error": "缺少 query"}
    limit = min(MAX_RESULTS, max(1, int(args.get("limit") or 10)))
    conn = _conn()
    try:
        res = arclib.search(
            conn, q,
            role=(args.get("role") or "").strip() or None,
            source=(args.get("source") or "").strip() or None,
            date_from=args.get("date_from") or args.get("from") or "",
            date_to=args.get("date_to") or args.get("to") or "",
            limit=limit, offset=0)
    finally:
        conn.close()
    if res.get("error"):
        return {"error": res["error"]}
    items = []
    for it in res["items"]:
        snip = arclib.make_snippet(
            _plain(it.get("snip", "")), [], width=MAX_SNIPPET, html_out=False)
        items.append({
            "conversation_id": it["conversation_id"],
            "message_index": it["message_index"],
            "role": it["role"],
            "timestamp": it["timestamp"],
            "title": it["title"],
            "source": it["source"],
            "snippet": snip,
        })
    return {"query": q, "mode": res["mode"], "total": res["total"],
            "returned": len(items), "truncated": res["truncated"], "items": items}


def _plain(html):
    return (html.replace("<mark>", "").replace("</mark>", "")
                .replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#x27;", "'")
                .replace("&amp;", "&"))


def tool_conversation(args):
    cid = (args.get("conversation_id") or args.get("id") or "").strip()
    if not cid:
        return {"error": "缺少 conversation_id"}
    max_msgs = min(MAX_MESSAGES, max(1, int(args.get("max_messages") or 40)))
    max_chars = min(MAX_MSG_CHARS, max(80, int(args.get("max_chars_per_message") or 1200)))
    with_thinking = bool(args.get("with_thinking"))
    conn = _conn()
    try:
        d = arclib.get_conversation(conn, cid, with_thinking=with_thinking)
    finally:
        conn.close()
    if not d:
        return {"error": "找不到对话: %s" % cid}
    msgs = []
    for m in d["messages"][:max_msgs]:
        text = m.get("text") or ""
        cut = len(text) > max_chars
        row = {"message_index": m["message_index"], "role": m["role"],
               "timestamp": m["timestamp"], "model": m.get("model"),
               "text": text[:max_chars] + ("…" if cut else "")}
        if with_thinking and m.get("thinking"):
            row["thinking"] = m["thinking"][:max_chars] + ("…" if len(m["thinking"]) > max_chars else "")
        msgs.append(row)
    conv = dict(d["conversation"])
    conv["total_messages"] = len(d["messages"])
    conv["returned_messages"] = len(msgs)
    return {"conversation": conv, "messages": msgs}


def tool_stats(args):
    conn = _conn()
    try:
        return arclib.stats(conn)
    finally:
        conn.close()


def tool_selection_list(args):
    coll = (args.get("collection") or "default").strip() or "default"
    ids = arclib.selected_ids(coll, path=arclib.selection_path(str(WORK_DIR)))
    conn = _conn()
    try:
        items = arclib.list_conversations(conn, ids)
    finally:
        conn.close()
    return {"collection": coll, "total": len(ids), "items": items}


def tool_selection_add(args):
    return _selection_write(args, "add")


def tool_selection_remove(args):
    return _selection_write(args, "remove")


def _selection_write(args, action):
    cid = (args.get("conversation_id") or args.get("id") or "").strip()
    if not cid:
        return {"error": "缺少 conversation_id"}
    coll = (args.get("collection") or "default").strip() or "default"
    rec = arclib.append_selection(cid, action=action, collection=coll,
                                  path=arclib.selection_path(str(WORK_DIR)))
    return {"ok": True, "action": action, "collection": coll,
            "conversation_id": rec["conversation_id"]}


def tool_export_text(args):
    import export as export_mod
    coll = (args.get("collection") or "default").strip() or "default"
    fmt = (args.get("format") or "md").strip().lower()
    if fmt not in ("md", "html", "jsonl"):
        return {"error": "format 只能是 md/html/jsonl"}
    max_chars = min(MAX_EXPORT_CHARS, max(1000, int(args.get("max_chars") or MAX_EXPORT_CHARS)))
    ids = arclib.selected_ids(coll, path=arclib.selection_path(str(WORK_DIR)))
    if not ids:
        return {"error": "选集为空: %s" % coll}
    conn = _conn()
    try:
        inc = bool(args.get("with_thinking"))
        if fmt == "md":
            text, used = export_mod.render_md(coll, ids, conn, include_thinking=inc)
        elif fmt == "html":
            text, used = export_mod.render_html(coll, ids, conn, include_thinking=inc)
        else:
            text, used = export_mod.render_jsonl(coll, ids, conn, include_thinking=inc)
    finally:
        conn.close()
    cut = len(text) > max_chars
    return {"collection": coll, "format": fmt, "conversations": used,
            "chars": len(text), "truncated": cut,
            "text": text[:max_chars] + ("\n…（已截断）" if cut else "")}


# ---------------------------------------------------------------- 分析（LLM）

def _analysis_db(args=None):
    p = (args or {}).get("analysis_db")
    if p:
        return Path(p)
    return WORK_DIR / "analysis.sqlite"


def _meta_of(conn, cid):
    row = conn.execute(
        "SELECT source, title, created_at FROM conversations WHERE conversation_id = ?",
        (cid,)).fetchone()
    if row is None:
        return None
    return {"source": row["source"], "title": row["title"],
            "created_at": row["created_at"]}


def tool_analysis_config(args):
    return llm.describe(llm.load_config(args.get("config")))


def tool_analysis_pending(args):
    limit = min(MAX_RESULTS, max(1, int(args.get("limit") or 10)))
    conn = _conn()
    try:
        aconn = analysis.open_analysis_db(str(_analysis_db(args)))
        try:
            done = analysis.analyzed_ids(aconn)
        finally:
            aconn.close()
        rows = conn.execute(
            "SELECT conversation_id, source, title, created_at, message_count "
            "FROM conversations ORDER BY created_at").fetchall()
    finally:
        conn.close()
    pending = [dict(r) for r in rows if r["conversation_id"] not in done]
    return {"pending": len(pending), "total": len(rows), "items": pending[:limit]}


def tool_analysis_digest(args):
    cid = (args.get("conversation_id") or "").strip()
    if not cid:
        return {"error": "缺少 conversation_id"}
    max_chars = min(MAX_DIGEST_CHARS, max(500, int(args.get("max_chars") or 6000)))
    conn = _conn()
    try:
        d = analysis.build_digest(conn, cid, max_chars=max_chars)
    finally:
        conn.close()
    if d is None:
        return {"error": "找不到对话: %s" % cid}
    return {
        "digest": d,
        "system_prompt": analysis.SYSTEM_PROMPT,
        "output_schema": {
            "value": "0-5 整数", "keep": "布尔", "value_reason": "≤40字",
            "kind": " / ".join(analysis.KINDS), "topics": "1-5 个短标签",
            "sentiment": "-1..1", "intensity": "0..1",
            "emotions": {k: "0..1" for k in analysis.EMOTIONS},
            "summary": "≤60字",
        },
        "hint": "把 system_prompt 当作系统提示、digest 当作对话内容交给任意 LLM，"
                "让它只输出 JSON；再用 analysis_save 存回结果。",
    }


def tool_analysis_save(args):
    cid = (args.get("conversation_id") or "").strip()
    result = args.get("result")
    if not cid:
        return {"error": "缺少 conversation_id"}
    if not isinstance(result, dict):
        return {"error": "result 必须是 JSON 对象"}
    conn = _conn()
    try:
        meta = _meta_of(conn, cid)
    finally:
        conn.close()
    if meta is None:
        return {"error": "找不到对话: %s" % cid}
    rec = analysis.normalize_result(result, cid, meta)
    rec["model"] = (args.get("model") or "mcp-client").strip()
    rec["analyzed_at"] = arclib.now_iso()
    aconn = analysis.open_analysis_db(str(_analysis_db(args)))
    try:
        analysis.upsert(aconn, rec)
    finally:
        aconn.close()
    return {"saved": cid, "record": rec}


def tool_analyze_conversation(args):
    cid = (args.get("conversation_id") or "").strip()
    if not cid:
        return {"error": "缺少 conversation_id"}
    cfg = llm.load_config(args.get("config"))
    if not llm.has_key(cfg):
        return {"error": "没有可用的 API key（设置 %s，或在 work/llm.json 里填 api_key）"
                         % cfg.get("api_key_env")}
    max_chars = min(MAX_DIGEST_CHARS, max(500, int(args.get("max_chars") or 6000)))
    conn = _conn()
    try:
        d = analysis.build_digest(conn, cid, max_chars=max_chars)
    finally:
        conn.close()
    if d is None:
        return {"error": "找不到对话: %s" % cid}
    obj, res = llm.chat_json(analysis.build_messages(d), cfg=cfg)
    rec = analysis.normalize_result(obj, cid, d)
    rec["model"] = res.get("model") or cfg.get("model")
    rec["analyzed_at"] = arclib.now_iso()
    usage = res.get("usage") or {}
    rec["prompt_tokens"] = usage.get("prompt_tokens")
    rec["completion_tokens"] = usage.get("completion_tokens")
    rec["cost"] = llm.estimate_cost(usage, rec["model"])
    aconn = analysis.open_analysis_db(str(_analysis_db(args)))
    try:
        analysis.upsert(aconn, rec)
    finally:
        aconn.close()
    return rec


def tool_analysis_get(args):
    cid = (args.get("conversation_id") or "").strip()
    aconn = analysis.open_analysis_db(str(_analysis_db(args)), readonly=True)
    try:
        if cid:
            rec = analysis.get(aconn, cid)
            if rec is None:
                return {"error": "这个对话还没有分析结果: %s" % cid}
            return rec
        return {"records": analysis.all_records(aconn)}
    finally:
        aconn.close()


def tool_analysis_status(args):
    aconn = analysis.open_analysis_db(str(_analysis_db(args)), readonly=True)
    try:
        st = analysis.status(aconn)
    finally:
        aconn.close()
    if st.get("total_conversations") is None:
        conn = _conn()
        try:
            st["total_conversations"] = conn.execute(
                "SELECT COUNT(*) c FROM conversations").fetchone()["c"]
        finally:
            conn.close()
    st["pending"] = max(0, (st.get("total_conversations") or 0) - st["analyzed"])
    return st


def tool_emotion_timeline(args):
    date_from = args.get("date_from")
    date_to = args.get("date_to")
    aconn = analysis.open_analysis_db(str(_analysis_db(args)), readonly=True)
    try:
        weeks = analysis.weekly(aconn, date_from=date_from, date_to=date_to)
    finally:
        aconn.close()
    if len(weeks) > MAX_WEEKS:
        weeks = weeks[-MAX_WEEKS:]
    return {"weeks": weeks, "count": len(weeks),
            "emotion_labels": analysis.EMOTION_LABELS}


def tool_value_tiers(args):
    limit = min(MAX_RESULTS, max(1, int(args.get("limit") or 10)))
    aconn = analysis.open_analysis_db(str(_analysis_db(args)), readonly=True)
    try:
        tiers = analysis.value_tiers(aconn)
    finally:
        aconn.close()
    return {"counts": {k: len(v) for k, v in tiers.items()},
            "high": tiers["high"][:limit],
            "mid": tiers["mid"][:limit],
            "low": tiers["low"][:limit]}


def tool_classify_conversation(args):
    """纯本地规则分类一个对话并入库，不联网、不要 key。"""
    cid = (args.get("conversation_id") or "").strip()
    if not cid:
        return {"error": "需要 conversation_id"}
    conn = _conn()
    try:
        rec = heur.classify_conversation(conn, cid)
    finally:
        conn.close()
    if not rec:
        return {"error": "找不到这个对话: %s" % cid}
    aconn = analysis.open_analysis_db(str(_analysis_db(args)))
    try:
        analysis.upsert(aconn, rec)
    finally:
        aconn.close()
    return {"saved": cid, "record": rec}


TOOLS = [
    {
        "name": "search",
        "description": "在归档里搜索（中文走 LIKE 全扫，英文走 FTS5）。只返回片段，不返回全文。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索词"},
                "role": {"type": "string", "enum": ["user", "assistant", "system"]},
                "source": {"type": "string", "enum": ["chatgpt", "gemini", "deepseek"]},
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
                "limit": {"type": "integer", "default": 10, "maximum": MAX_RESULTS},
            },
            "required": ["query"],
        },
    },
    {
        "name": "conversation",
        "description": "读取一个对话的元数据和消息（正文按上限截断）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "max_messages": {"type": "integer", "default": 40, "maximum": MAX_MESSAGES},
                "max_chars_per_message": {"type": "integer", "default": 1200, "maximum": MAX_MSG_CHARS},
                "with_thinking": {"type": "boolean", "default": False},
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "stats",
        "description": "归档总量统计（对话数、消息数、来源分布、时间范围）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "selection_list",
        "description": "列出某个选集里的对话。",
        "inputSchema": {
            "type": "object",
            "properties": {"collection": {"type": "string", "default": "default"}},
        },
    },
    {
        "name": "selection_add",
        "description": "把一个对话加入选集（追加写入 work/selection.jsonl）。",
        "inputSchema": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string"},
                           "collection": {"type": "string", "default": "default"}},
            "required": ["conversation_id"],
        },
    },
    {
        "name": "selection_remove",
        "description": "把一个对话移出选集。",
        "inputSchema": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string"},
                           "collection": {"type": "string", "default": "default"}},
            "required": ["conversation_id"],
        },
    },
    {
        "name": "export_text",
        "description": "把选集导出为 md/html/jsonl 文本（默认截断到 200000 字符）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "collection": {"type": "string", "default": "default"},
                "format": {"type": "string", "enum": ["md", "html", "jsonl"], "default": "md"},
                "with_thinking": {"type": "boolean", "default": False},
                "max_chars": {"type": "integer", "default": MAX_EXPORT_CHARS},
            },
        },
    },
    {
        "name": "analysis_config",
        "description": "查看 LLM 分析配置（base_url / model / key 是否存在，key 只显示布尔值）。",
        "inputSchema": {
            "type": "object",
            "properties": {"config": {"type": "string", "description": "可选：llm.json 路径"}},
        },
    },
    {
        "name": "analysis_pending",
        "description": "列出还没做 LLM 分析的对话（按时间升序），供逐个分析。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10, "maximum": MAX_RESULTS}},
        },
    },
    {
        "name": "analysis_digest",
        "description": "取一个对话的压缩摘要 + 评分规则（系统提示 + 输出 schema）。"
                       "不调用任何 API：把 system_prompt 和 digest 交给 MCP 客户端自己的 LLM 分析即可。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "max_chars": {"type": "integer", "default": 6000, "maximum": MAX_DIGEST_CHARS},
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "analysis_save",
        "description": "保存一个对话的分析结果（配合 analysis_digest 使用，字段见 output_schema）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "result": {"type": "object", "description": "含 value/keep/kind/topics/sentiment/emotions/summary 等"},
                "model": {"type": "string", "description": "谁做的分析，默认 mcp-client"},
            },
            "required": ["conversation_id", "result"],
        },
    },
    {
        "name": "analyze_conversation",
        "description": "服务端直接调用配置好的 LLM API 分析一个对话并入库（需要 API key）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "max_chars": {"type": "integer", "default": 6000, "maximum": MAX_DIGEST_CHARS},
                "config": {"type": "string"},
            },
            "required": ["conversation_id"],
        },
    },
    {
        "name": "analysis_get",
        "description": "读取分析结果：给 conversation_id 取单条，不给则取全部。",
        "inputSchema": {
            "type": "object",
            "properties": {"conversation_id": {"type": "string"}},
        },
    },
    {
        "name": "analysis_status",
        "description": "分析进度统计（已分析 / 失败 / 平均价值 / 价值分布 / token / 花费）。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "emotion_timeline",
        "description": "按周的情绪/价值时间线（六种情绪均值、情感、强度、高频主题）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "YYYY-MM-DD"},
                "date_to": {"type": "string", "description": "YYYY-MM-DD"},
            },
        },
    },
    {
        "name": "value_tiers",
        "description": "按价值分档：high(>=4) / mid(=3) / low(<=2)。",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 10, "maximum": MAX_RESULTS}},
        },
    },
    {
        "name": "classify_conversation",
        "description": ("纯本地启发式规则给一个对话打价值分/类型/标签/粗估情绪并入库，"
                        "不联网、不需要 API key。"),
        "inputSchema": {
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string"},
                "analysis_db": {"type": "string"},
            },
            "required": ["conversation_id"],
        },
    },
]

HANDLERS = {
    "search": tool_search,
    "conversation": tool_conversation,
    "stats": tool_stats,
    "selection_list": tool_selection_list,
    "selection_add": tool_selection_add,
    "selection_remove": tool_selection_remove,
    "export_text": tool_export_text,
    "analysis_config": tool_analysis_config,
    "analysis_pending": tool_analysis_pending,
    "analysis_digest": tool_analysis_digest,
    "analysis_save": tool_analysis_save,
    "analyze_conversation": tool_analyze_conversation,
    "analysis_get": tool_analysis_get,
    "analysis_status": tool_analysis_status,
    "emotion_timeline": tool_emotion_timeline,
    "value_tiers": tool_value_tiers,
    "classify_conversation": tool_classify_conversation,
}


# --------------------------------------------------------------------------
# JSON-RPC / MCP 传输
# --------------------------------------------------------------------------

def _reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error is not None:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _text_result(obj):
    return {"content": [{"type": "text",
                         "text": json.dumps(obj, ensure_ascii=False, indent=2)}],
            "isError": bool(isinstance(obj, dict) and obj.get("error"))}


def handle(msg):
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _reply(msg_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method in ("notifications/initialized", "initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return _reply(msg_id, {})
    if method == "tools/list":
        return _reply(msg_id, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        fn = HANDLERS.get(name)
        if fn is None:
            return _reply(msg_id, error={"code": -32601, "message": "未知工具: %s" % name})
        try:
            return _reply(msg_id, _text_result(fn(args)))
        except Exception as e:  # noqa: BLE001
            return _reply(msg_id, _text_result({"error": "%s: %s" % (type(e).__name__, e)}))
    if msg_id is None:
        return None
    return _reply(msg_id, error={"code": -32601, "message": "未知方法: %s" % method})


def main(argv=None):
    global DB_PATH, WORK_DIR
    ap = argparse.ArgumentParser(description="AI 聊天归档 MCP 服务（检索 + 选集 + LLM 分析）")
    ap.add_argument("--db", default=str(arclib.DEFAULT_DB))
    ap.add_argument("--work", default=str(arclib.DEFAULT_WORK))
    a = ap.parse_args(argv)
    DB_PATH = Path(a.db)
    WORK_DIR = Path(a.work)
    if not DB_PATH.exists():
        sys.stderr.write("数据库不存在: %s\n" % DB_PATH)
        return 1
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, list):
            for m in msg:
                if isinstance(m, dict):
                    handle(m)
            continue
        if isinstance(msg, dict):
            handle(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
