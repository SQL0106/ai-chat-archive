#!/usr/bin/env python3
"""LLM 分析结果的存储、摘要构造与按周情绪聚合。

数据存在**独立的** work/analysis.sqlite 里，绝不碰 out/archive.sqlite。
每个对话一行；重跑时按 (conversation_id, prompt_version) 覆盖。

这里只负责「怎么问、怎么存、怎么算」，不负责真正调用模型——
调用在 tools/analyze.py 里，走 tools/llm.py 的适配层。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import arclib
import sanitize

ROOT = Path(__file__).resolve().parent.parent
ANALYSIS_DB = ROOT / "work" / "analysis.sqlite"

PROMPT_VERSION = "v1"

EMOTIONS = ["joy", "calm", "anxiety", "anger", "sadness", "fatigue"]
EMOTION_LABELS = {
    "joy": "喜悦", "calm": "平静", "anxiety": "焦虑",
    "anger": "愤怒", "sadness": "低落", "fatigue": "疲惫",
}
KINDS = ["技术", "学习", "工作", "生活", "情绪", "创作", "查询", "闲聊", "其他"]

DIGEST_MAX_CHARS = 6000
DIGEST_MAX_MSGS = 40
DIGEST_MSG_CHARS = 700

SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis (
    conversation_id TEXT PRIMARY KEY,
    source          TEXT,
    title           TEXT,
    conv_created_at TEXT,
    model           TEXT,
    prompt_version  TEXT,
    value           INTEGER,
    keep            INTEGER,
    value_reason    TEXT,
    kind            TEXT,
    topics          TEXT,
    sentiment       REAL,
    emotions        TEXT,
    intensity       REAL,
    summary         TEXT,
    analyzed_at     TEXT,
    prompt_tokens   INTEGER,
    completion_tokens INTEGER,
    cost            REAL
);
CREATE INDEX IF NOT EXISTS idx_analysis_week ON analysis(conv_created_at);
CREATE INDEX IF NOT EXISTS idx_analysis_value ON analysis(value);

CREATE TABLE IF NOT EXISTS analysis_errors (
    conversation_id TEXT PRIMARY KEY,
    model           TEXT,
    error           TEXT,
    attempts        INTEGER DEFAULT 1,
    tried_at        TEXT
);
"""


# --------------------------------------------------------------------------
# 存储
# --------------------------------------------------------------------------

def open_analysis_db(path=None, readonly=False):
    p = Path(path) if path else ANALYSIS_DB
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10.0)
    conn.row_factory = sqlite3.Row
    if readonly:
        conn.execute("PRAGMA query_only = 1")
    else:
        conn.executescript(SCHEMA)
    return conn


def _json_or(v, fallback):
    if v is None or v == "":
        return fallback
    if isinstance(v, (list, dict)):
        return v
    try:
        return json.loads(v)
    except (ValueError, TypeError):
        return fallback


def upsert(conn, rec):
    """写入/覆盖一条分析结果（rec 见 SCHEMA 的字段）。"""
    conn.execute(
        "INSERT OR REPLACE INTO analysis ("
        " conversation_id, source, title, conv_created_at, model, prompt_version,"
        " value, keep, value_reason, kind, topics, sentiment, emotions, intensity,"
        " summary, analyzed_at, prompt_tokens, completion_tokens, cost"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            rec.get("conversation_id"), rec.get("source"), rec.get("title"),
            rec.get("conv_created_at"), rec.get("model"), rec.get("prompt_version"),
            rec.get("value"), 1 if rec.get("keep") else 0, rec.get("value_reason"),
            rec.get("kind"),
            json.dumps(rec.get("topics") or [], ensure_ascii=False),
            rec.get("sentiment"),
            json.dumps(rec.get("emotions") or {}, ensure_ascii=False),
            rec.get("intensity"), rec.get("summary"), rec.get("analyzed_at"),
            rec.get("prompt_tokens"), rec.get("completion_tokens"), rec.get("cost"),
        ))
    conn.commit()


def record_error(conn, cid, model, error):
    conn.execute(
        "INSERT INTO analysis_errors (conversation_id, model, error, attempts, tried_at)"
        " VALUES (?,?,?,1,?)"
        " ON CONFLICT(conversation_id) DO UPDATE SET"
        "   model=excluded.model, error=excluded.error,"
        "   attempts=analysis_errors.attempts+1, tried_at=excluded.tried_at",
        (cid, model, str(error)[:1000], arclib.now_iso()))
    conn.commit()


def _row_to_rec(row):
    d = dict(row)
    d["topics"] = _json_or(d.get("topics"), [])
    d["emotions"] = _json_or(d.get("emotions"), {})
    d["keep"] = bool(d.get("keep"))
    return d


def get(conn, cid):
    row = conn.execute("SELECT * FROM analysis WHERE conversation_id = ?", (cid,)).fetchone()
    return _row_to_rec(row) if row else None


def analyzed_ids(conn, prompt_version=None):
    if prompt_version:
        rows = conn.execute("SELECT conversation_id FROM analysis WHERE prompt_version = ?",
                            (prompt_version,)).fetchall()
    else:
        rows = conn.execute("SELECT conversation_id FROM analysis").fetchall()
    return {r["conversation_id"] for r in rows}


def all_records(conn):
    return [_row_to_rec(r) for r in conn.execute(
        "SELECT * FROM analysis ORDER BY conv_created_at").fetchall()]


def status(conn):
    n = conn.execute("SELECT COUNT(*) c FROM analysis").fetchone()["c"]
    err = conn.execute("SELECT COUNT(*) c FROM analysis_errors").fetchone()["c"]
    agg = conn.execute(
        "SELECT AVG(value) v, SUM(prompt_tokens) pt, SUM(completion_tokens) ct,"
        " SUM(cost) cost, MIN(analyzed_at) first, MAX(analyzed_at) last"
        " FROM analysis").fetchone()
    buckets = {i: 0 for i in range(6)}
    for r in conn.execute("SELECT value, COUNT(*) c FROM analysis GROUP BY value"):
        if r["value"] is not None:
            buckets[int(r["value"])] = r["c"]
    total = conn.execute("SELECT COUNT(*) c FROM conversations").fetchone()["c"] \
        if _has_conversations(conn) else None
    return {
        "analyzed": n,
        "errors": err,
        "avg_value": round(agg["v"], 2) if agg["v"] is not None else None,
        "value_buckets": buckets,
        "keep": conn.execute("SELECT COUNT(*) c FROM analysis WHERE keep=1").fetchone()["c"],
        "prompt_tokens": agg["pt"] or 0,
        "completion_tokens": agg["ct"] or 0,
        "cost": round(agg["cost"], 4) if agg["cost"] is not None else None,
        "first": agg["first"],
        "last": agg["last"],
        "total_conversations": total,
    }


def _has_conversations(conn):
    try:
        conn.execute("SELECT 1 FROM conversations LIMIT 1").fetchone()
        return True
    except sqlite3.Error:
        return False


# --------------------------------------------------------------------------
# 摘要构造
# --------------------------------------------------------------------------

def _clip(s, n):
    s = (s or "").strip()
    return s if len(s) <= n else s[:n] + "…"


def build_digest(conn, cid, max_chars=DIGEST_MAX_CHARS, max_msgs=DIGEST_MAX_MSGS,
                 msg_chars=DIGEST_MSG_CHARS):
    """把一个对话压成「够判断价值」的摘要，控制总长度，不把全文塞进去。"""
    data = arclib.get_conversation(conn, cid, with_thinking=False)
    if not data:
        return None
    conv = data["conversation"]
    msgs = [m for m in data["messages"] if (m.get("text") or "").strip()]
    picked = []
    if len(msgs) <= max_msgs:
        picked = msgs
    else:
        # 第一条（定主题）+ 其余均匀采样 + 最后一条（看结论）
        head = msgs[:1]
        tail = msgs[-1:]
        middle = msgs[1:-1]
        room = max_msgs - 2
        if room > 0 and middle:
            step = max(1, len(middle) // room)
            picked = head + middle[::step][:room] + tail
        else:
            picked = head + tail

    lines = []
    used = 0
    for m in picked:
        role = "用户" if m.get("role") == "user" else "助手"
        text = sanitize.mask(_clip(m.get("text"), msg_chars))
        line = "[%s] %s" % (role, text)
        if used + len(line) > max_chars:
            lines.append("…（后续内容已省略）")
            break
        lines.append(line)
        used += len(line)

    return {
        "conversation_id": cid,
        "source": conv.get("source"),
        "title": sanitize.mask(conv.get("title")),
        "created_at": conv.get("created_at"),
        "message_count": conv.get("message_count"),
        "sampled": len(picked),
        "transcript": "\n\n".join(lines),
        "chars": used,
    }


SYSTEM_PROMPT = """你是一个聊天记录归档的评估员。用户会给你一段「用户与 AI 的对话摘要」。
请判断这段对话对用户是否**值得长期保留**，并分析对话里反映出的用户情绪。

价值评分 value（0-5 整数）：
  5 = 深度原创思考 / 长期项目方案 / 可复用的方法论 / 重要决策
  4 = 有实质技术或知识产出，以后可能复用
  3 = 一般性问答，有信息量但比较通用
  2 = 琐碎查询、一次性事实、简单翻译或改写
  1 = 寒暄、确认、「继续」、试错噪声
  0 = 空对话、纯报错粘贴、几乎没有内容
keep：value >= 3 时为 true，否则 false。

kind：从 技术 / 学习 / 工作 / 生活 / 情绪 / 创作 / 查询 / 闲聊 / 其他 里选一个。
topics：1-5 个短标签。
sentiment：整段对话的整体情绪基调，-1.0（很消极）到 1.0（很积极）。
intensity：情绪强度 0.0-1.0（0 表示平淡无情绪）。
emotions：下列每个维度给 0.0-1.0 的分值——
  joy 喜悦, calm 平静, anxiety 焦虑, anger 愤怒, sadness 低落, fatigue 疲惫
summary：一句话概括对话内容（不超过 60 字）。

只输出一个 JSON 对象，不要任何解释、不要 markdown 代码块。格式：
{"value":0,"keep":false,"value_reason":"","kind":"","topics":[],
 "sentiment":0.0,"intensity":0.0,
 "emotions":{"joy":0,"calm":0,"anxiety":0,"anger":0,"sadness":0,"fatigue":0},
 "summary":""}"""


def build_messages(digest):
    user = (
        "来源：%s\n标题：%s\n时间：%s\n消息数：%s（本次采样 %s 条）\n\n"
        "===== 对话摘要 =====\n%s"
        % (digest.get("source"), digest.get("title"), digest.get("created_at"),
           digest.get("message_count"), digest.get("sampled"), digest.get("transcript"))
    )
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user}]


def normalize_result(obj, cid, meta):
    """把模型返回的 JSON 洗成规范的存储记录，越界值一律夹紧。"""
    def num(v, lo, hi, default=0.0):
        try:
            f = float(v)
        except (TypeError, ValueError):
            return default
        return max(lo, min(hi, f))

    emo_in = obj.get("emotions") if isinstance(obj.get("emotions"), dict) else {}
    emo = {k: round(num(emo_in.get(k), 0.0, 1.0), 3) for k in EMOTIONS}
    try:
        value = max(0, min(5, int(obj.get("value"))))
    except (TypeError, ValueError):
        value = 3
    topics = obj.get("topics")
    if not isinstance(topics, list):
        topics = []
    topics = [str(t)[:24] for t in topics[:5] if str(t).strip()]
    return {
        "conversation_id": cid,
        "source": meta.get("source"),
        "title": meta.get("title"),
        "conv_created_at": meta.get("created_at"),
        "prompt_version": PROMPT_VERSION,
        "value": value,
        "keep": bool(obj.get("keep")) if isinstance(obj.get("keep"), bool) else value >= 3,
        "value_reason": str(obj.get("value_reason") or "")[:200],
        "kind": str(obj.get("kind") or "")[:16],
        "topics": topics,
        "sentiment": round(num(obj.get("sentiment"), -1.0, 1.0), 3),
        "emotions": emo,
        "intensity": round(num(obj.get("intensity"), 0.0, 1.0), 3),
        "summary": str(obj.get("summary") or "")[:200],
    }


# --------------------------------------------------------------------------
# 按周聚合
# --------------------------------------------------------------------------

def _parse_iso(s):
    if not s:
        return None
    t = str(s).strip().replace("Z", "+00:00")
    try:
        d = datetime.fromisoformat(t)
    except ValueError:
        try:
            d = datetime.strptime(t[:19], "%Y-%m-%dT%H:%M:%S")
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(arclib.TZ)


def week_key(dt):
    """返回该时间所在周的周一（本地时区）。"""
    d = dt - timedelta(days=dt.weekday())
    return d.replace(hour=0, minute=0, second=0, microsecond=0)


def weekly(conn, date_from=None, date_to=None, tz_offset=None, prompt_version=None):
    """按周聚合情绪与价值。返回按时间升序的周列表。"""
    lo = arclib.parse_date(date_from) if date_from else None
    hi = arclib.parse_date(date_to) if date_to else None
    buckets = {}
    for rec in all_records(conn):
        if prompt_version and rec.get("prompt_version") != prompt_version:
            continue
        dt = _parse_iso(rec.get("conv_created_at"))
        if not dt:
            continue
        if lo and dt < _parse_iso(lo):
            continue
        if hi and dt > _parse_iso(hi) + timedelta(days=1):
            continue
        wk = week_key(dt)
        b = buckets.setdefault(wk, {
            "week": wk.strftime("%Y-%m-%d"), "n": 0, "value": 0.0,
            "sentiment": 0.0, "intensity": 0.0, "keep": 0,
            "emotions": {k: 0.0 for k in EMOTIONS}, "_topics": {}, "_top": [],
        })
        b["n"] += 1
        b["value"] += rec.get("value") or 0
        b["sentiment"] += rec.get("sentiment") or 0.0
        b["intensity"] += rec.get("intensity") or 0.0
        b["keep"] += 1 if rec.get("keep") else 0
        for k in EMOTIONS:
            b["emotions"][k] += (rec.get("emotions") or {}).get(k) or 0.0
        for t in rec.get("topics") or []:
            b["_topics"][t] = b["_topics"].get(t, 0) + 1
        b["_top"].append((rec.get("value") or 0, rec.get("sentiment") or 0.0,
                          rec.get("conversation_id"), rec.get("title")))

    out = []
    for wk in sorted(buckets):
        b = buckets[wk]
        n = b["n"]
        top = sorted(b["_top"], key=lambda x: (-x[0], x[1]))[:3]
        out.append({
            "week": b["week"],
            "week_end": (wk + timedelta(days=6)).strftime("%Y-%m-%d"),
            "n": n,
            "value": round(b["value"] / n, 2),
            "keep_ratio": round(b["keep"] / n, 3),
            "sentiment": round(b["sentiment"] / n, 3),
            "intensity": round(b["intensity"] / n, 3),
            "emotions": {k: round(v / n, 3) for k, v in b["emotions"].items()},
            "topics": [t for t, _ in sorted(b["_topics"].items(),
                                            key=lambda x: -x[1])[:6]],
            "top": [{"conversation_id": c, "title": t, "value": v}
                    for v, _s, c, t in top],
        })
    return out


def value_tiers(conn, prompt_version=None):
    """把对话分成 高价值 / 一般 / 低价值 三档，返回 id 列表。"""
    tiers = {"high": [], "mid": [], "low": []}
    for rec in all_records(conn):
        if prompt_version and rec.get("prompt_version") != prompt_version:
            continue
        v = rec.get("value")
        if v is None:
            continue
        key = "high" if v >= 4 else ("mid" if v == 3 else "low")
        tiers[key].append(rec["conversation_id"])
    return tiers


def records_by_ids(conn, ids):
    """按给定 id 顺序取记录，返回 {conversation_id: rec}，缺失的跳过。"""
    out = {}
    for cid in ids:
        rec = get(conn, cid)
        if rec:
            out[cid] = rec
    return out


def emotional_records(conn, min_intensity=0.3, min_sentiment=0.2,
                      kinds=("情绪",), prompt_version=None, limit=None):
    """挑出「值得做情绪分析」的记录。

    命中任一条件即算：类型属于情绪类；情绪强度 >= min_intensity；
    |整体基调| >= min_sentiment。按情绪强度从高到低排序。
    """
    picked = []
    for rec in all_records(conn):
        if prompt_version and rec.get("prompt_version") != prompt_version:
            continue
        inten = rec.get("intensity")
        senti = rec.get("sentiment")
        inten = float(inten) if inten is not None else 0.0
        senti = float(senti) if senti is not None else 0.0
        if rec.get("kind") in kinds or inten >= min_intensity or abs(senti) >= min_sentiment:
            picked.append(rec)
    picked.sort(key=lambda r: (float(r.get("intensity") or 0.0),
                               abs(float(r.get("sentiment") or 0.0))), reverse=True)
    return picked[:limit] if limit else picked


def emotional_ids(conn, **kw):
    """emotional_records 只要 id 集合。"""
    return {r["conversation_id"] for r in emotional_records(conn, **kw)}


if __name__ == "__main__":
    conn = open_analysis_db()
    print(json.dumps(status(conn), ensure_ascii=False, indent=2))
