#!/usr/bin/env python3
"""归档命令行工具。

设计约束：默认只输出片段和统计，绝不整篇打印正文（省 token）。
要完整正文必须显式加 --show / --full。

用法示例：
    python3 tools/archive_cli.py search 学习 --source deepseek --limit 5
    python3 tools/archive_cli.py stats
    python3 tools/archive_cli.py terms --top 40 --source chatgpt
    python3 tools/archive_cli.py cooccur 睡眠 咖啡
    python3 tools/archive_cli.py length --order chars --limit 20
    python3 tools/archive_cli.py show 6a2598d4-ba50-8320-9973-1cff0b77350c
    python3 tools/archive_cli.py select add <cid> --collection 论文
    python3 tools/archive_cli.py select ls
    python3 tools/archive_cli.py export --collection 论文 --format md -o /tmp/out.md
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import arclib  # noqa: E402

COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
MARK_OPEN = "\033[1;33m" if COLOR else "⟦"
MARK_CLOSE = "\033[0m" if COLOR else "⟧"
DIM = "\033[2m" if COLOR else ""
BOLD = "\033[1m" if COLOR else ""
RESET = "\033[0m" if COLOR else ""

WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_'\-]{1,}")
CJK_RUN_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")

ASCII_STOP = {
    "the", "and", "for", "you", "are", "not", "but", "with", "this", "that",
    "have", "can", "will", "from", "what", "how", "why", "use", "using", "its",
    "it's", "was", "were", "has", "had", "been", "they", "them", "their", "there",
    "then", "than", "when", "where", "which", "who", "your", "our", "about",
    "would", "could", "should", "does", "did", "just", "like", "make", "made",
    "get", "got", "one", "two", "all", "any", "some", "more", "most", "also",
    "into", "out", "over", "only", "very", "much", "many", "such", "other",
    "these", "those", "因为", "所以", "但是", "如果", "已经", "可以", "这个",
    "那个", "一个", "没有", "就是", "还是", "什么", "怎么", "为什么",
}


def strip_marks(s):
    return s.replace("<mark>", MARK_OPEN).replace("</mark>", MARK_CLOSE)


def out_json(obj):
    json.dump(obj, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def fmt_ts(ts):
    if not ts:
        return "?"
    return ts.replace("T", " ")[:16].replace("+00:00", "")


def link(cid, idx=None, q=None):
    frag = "#c=" + str(cid)
    if idx is not None:
        frag += "&i=%d" % int(idx)
    if q:
        from urllib.parse import quote
        frag += "&q=" + quote(q)
    return frag


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def cmd_search(a):
    conn = arclib.open_db(a.db)
    res = arclib.search(conn, a.query, role=a.role, source=a.source,
                        date_from=a.date_from, date_to=a.date_to,
                        limit=a.limit, offset=a.offset)
    if a.json:
        out_json(res)
        return 0
    if res.get("error"):
        print("错误:", res["error"], file=sys.stderr)
        return 2
    head = "共 %d 条（模式 %s%s），显示 %d–%d" % (
        res["total"], res["mode"],
        "，已截断候选集" if res.get("truncated") else "",
        res["offset"], res["offset"] + len(res["items"]) - 1)
    print(BOLD + head + RESET)
    for it in res["items"]:
        print("\n%s %s [%s] %s" % (
            DIM + fmt_ts(it["timestamp"]) + RESET,
            BOLD + (it["source"] or "?") + RESET,
            it["role"],
            it["title"] or "(无标题)"))
        print("  " + strip_marks(it["snip"]))
        if a.link:
            print(DIM + "  " + link(it["conversation_id"], it["message_index"], a.query) + RESET)
    return 0


_WD = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"]


def _bar(n, top, width=24):
    if not top:
        return ""
    return "█" * max(1, int(round(n * width / top))) if n else ""


def cmd_stats(a):
    conn = arclib.open_db(a.db)
    st = arclib.stats(conn)
    if a.json:
        out_json(st)
        return 0
    t = st["totals"]
    print(BOLD + "总计" + RESET)
    print("  对话 %d · 消息 %d（用户 %d / 助手 %d）" % (
        t["conversations"], t["messages"], t["user_messages"], t["assistant_messages"]))
    print("  正文 %.1f 万字符 · 思维链 %.1f 万字符（%d 条消息带思维链）" % (
        t["chars"] / 10000.0, t["thinking_chars"] / 10000.0, t["thinking_messages"]))
    print("  带附件 %d 条 · 平均每条 %.0f 字（我 %.0f / AI %.0f）" % (
        t["attachment_messages"], t["avg_chars_message"],
        t["avg_chars_user"], t["avg_chars_assistant"]))
    print("  时间范围 %s → %s" % (fmt_ts(st["range"]["min"]), fmt_ts(st["range"]["max"])))

    print(BOLD + "\n活跃度" + RESET)
    print("  活跃 %d 天 / 跨度 %d 天（%.0f%%）· 日均 %.1f 条 · 平均每对话 %.1f 条" % (
        t["active_days"], t["span_days"],
        100.0 * t["active_days"] / t["span_days"] if t["span_days"] else 0,
        t["avg_per_day"], t["avg_per_conversation"]))
    print("  最长连续 %d 天 · 最近连续 %d 天" % (t["max_streak"], t["last_streak"]))
    bd, bm = st["busiest_day"], st["busiest_month"]
    bh, bw = st["busiest_hour"], st["busiest_weekday"]
    if bd:
        print("  最忙一天 %s（%d 条）" % (bd["k"], bd["n"]))
    if bm:
        print("  最忙一月 %s（%d 条 / %d 对话）" % (bm["k"], bm["n"], bm["c"]))
    if bh and bw:
        print("  最忙时段 %s 点（%d 条）· 最忙星期 %s（%d 条）" % (
            bh["k"], bh["n"], _WD[int(bw["k"])], bw["n"]))

    print(BOLD + "\n按年" + RESET)
    for y in st["by_year"]:
        print("  %s  %6d 条 / %4d 对话" % (y["k"], y["n"], y["c"]))

    print(BOLD + "\n来源" + RESET)
    for s in st["sources"]:
        print("  %-10s %5d 对话 / %6d 消息 / %.1f 万字符（平均 %.1f 条）" % (
            s["source"], s["conversations"], s["messages"],
            s["chars"] / 10000.0, s["avg_messages"]))

    print(BOLD + "\n模型" + RESET)
    top = st["models"][0]["n"] if st["models"] else 0
    for m in st["models"][:8]:
        print("  %-20s %6d  %s" % (m["k"], m["n"], _bar(m["n"], top)))

    print(BOLD + "\n对话长度（按消息数）" + RESET)
    top = max((c["n"] for c in st["conv_hist"]), default=0)
    for c in st["conv_hist"]:
        print("  %-8s %6d  %s" % (c["k"], c["n"], _bar(c["n"], top)))

    print(BOLD + "\n消息长度（按字符数）" + RESET)
    top = max((c["n"] for c in st["msg_hist"]), default=0)
    for c in st["msg_hist"]:
        print("  %-8s %6d  %s" % (c["k"], c["n"], _bar(c["n"], top)))

    lm = st["longest_message"]
    if lm:
        print(BOLD + "\n最长单条消息" + RESET)
        print("  %d 字符 · %s · %s" % (lm["n"], lm["role"], lm["conversation_id"]))
        print(DIM + "  " + lm["s"].replace("\n", " ")[:120] + RESET)

    print(BOLD + "\n时间戳来源" + RESET)
    for k, v in st["timestamp_sources"].items():
        print("  %-18s %6d" % (k, v))
    return 0


def _iter_texts(conn, source=None, with_thinking=False, max_msgs=0):
    cols = "text, thinking" if with_thinking else "text"
    where, args = [], []
    if source:
        where.append("source = ?")
        args.append(source)
    sql = "SELECT %s FROM messages" % cols
    if where:
        sql += " WHERE " + " AND ".join(where)
    if max_msgs:
        sql += " LIMIT %d" % int(max_msgs)
    for row in conn.execute(sql, args):
        yield row["text"] or ""
        if with_thinking:
            yield row["thinking"] or ""


def _count_terms(conn, source, with_thinking, ngram, max_msgs):
    counts = {}
    for text in _iter_texts(conn, source, with_thinking, max_msgs):
        if not text:
            continue
        for w in WORD_RE.findall(text):
            w = w.lower()
            if w in ASCII_STOP:
                continue
            counts[w] = counts.get(w, 0) + 1
        for run in CJK_RUN_RE.findall(text):
            if len(run) < ngram:
                continue
            for i in range(len(run) - ngram + 1):
                g = run[i:i + ngram]
                if g in ASCII_STOP:
                    continue
                counts[g] = counts.get(g, 0) + 1
    return counts


def cmd_terms(a):
    conn = arclib.open_db(a.db)
    counts = _count_terms(conn, a.source, a.with_thinking, a.ngram, a.max_msgs)
    items = [(k, v) for k, v in counts.items() if v >= a.min_count]
    items.sort(key=lambda x: -x[1])
    items = items[:a.top]
    if a.json:
        out_json([{"term": k, "count": v} for k, v in items])
        return 0
    print(BOLD + "高频词（%d-gram%s，共 %d 个不同词）" % (
        a.ngram, "，含思维链" if a.with_thinking else "", len(counts)) + RESET)
    for k, v in items:
        print("  %8d  %s" % (v, k))
    return 0


def cmd_cooccur(a):
    conn = arclib.open_db(a.db)
    terms = [a.term_a, a.term_b]
    where, args = [], []
    for t in terms:
        pat = "%" + t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where.append("(m.text LIKE ? ESCAPE '\\' OR m.thinking LIKE ? ESCAPE '\\')")
        args.extend([pat, pat])
    if a.source:
        where.append("m.source = ?")
        args.append(a.source)
    clause = " AND ".join(where)
    base = ("FROM messages m JOIN conversations c"
            " ON c.conversation_id = m.conversation_id WHERE " + clause)
    row = conn.execute(
        "SELECT COUNT(*) n, COUNT(DISTINCT m.conversation_id) c " + base, args).fetchone()
    res = {"terms": terms, "messages": row["n"], "conversations": row["c"], "samples": []}
    if a.samples:
        rows = conn.execute(
            "SELECT m.conversation_id, m.message_index, m.role, m.timestamp,"
            " c.title, c.source, m.text, m.thinking " + base +
            " ORDER BY m.timestamp DESC LIMIT ?", args + [a.samples]).fetchall()
        for r in rows:
            src = r["text"] if arclib.first_hit(r["text"] or "", terms)[0] >= 0 else (r["thinking"] or "")
            res["samples"].append({
                "conversation_id": r["conversation_id"], "message_index": r["message_index"],
                "role": r["role"], "timestamp": r["timestamp"], "title": r["title"],
                "source": r["source"], "snip": arclib.make_snippet(src, terms),
            })
    if a.json:
        out_json(res)
        return 0
    print("%s + %s → %d 条消息 / %d 个对话" % (terms[0], terms[1], res["messages"], res["conversations"]))
    for s in res["samples"]:
        print("\n%s %s [%s] %s" % (DIM + fmt_ts(s["timestamp"]) + RESET, BOLD + s["source"] + RESET,
                                   s["role"], s["title"] or "(无标题)"))
        print("  " + strip_marks(s["snip"]))
    return 0


def cmd_length(a):
    conn = arclib.open_db(a.db)
    rows = arclib.conversation_lengths(conn, limit=a.limit, order=a.order)
    if a.json:
        out_json(rows)
        return 0
    print(BOLD + "对话长度（按%s）" % {"chars": "字符数", "messages": "消息数",
                                   "updated": "更新时间"}.get(a.order, a.order) + RESET)
    for r in rows:
        print("  %9d 字符 %5d 条  %-9s %s  %s" % (
            r["chars"], r["n"], r["source"], fmt_ts(r["updated_at"]),
            (r["title"] or "(无标题)")[:48]))
        if a.link:
            print(DIM + "      " + link(r["conversation_id"]) + RESET)
    return 0


def cmd_show(a):
    conn = arclib.open_db(a.db)
    d = arclib.get_conversation(conn, a.cid)
    if not d:
        print("找不到对话:", a.cid, file=sys.stderr)
        return 1
    if a.json:
        out_json(d)
        return 0
    c = d["conversation"]
    total = sum(len(m["text"] or "") for m in d["messages"])
    print(BOLD + (c["title"] or "(无标题)") + RESET)
    print("%s · %d 条消息 · %.1f 万字符 · %s → %s" % (
        c["source"], c["message_count"], total / 10000.0,
        fmt_ts(c["created_at"]), fmt_ts(c["updated_at"])))
    print(DIM + c["conversation_id"] + RESET)
    print(DIM + link(c["conversation_id"]) + RESET)
    print()
    for m in d["messages"]:
        t = m["text"] or ""
        print("%s#%d %s %s" % (BOLD, m["message_index"], m["role"].upper(), RESET),
              DIM + fmt_ts(m["timestamp"]) + RESET)
        if a.full:
            print(t)
        else:
            print("  " + arclib.make_snippet(t, [], width=200, html_out=False))
        if m.get("thinking"):
            print(DIM + "  💭 %d 字符思维链（--full 显示）" % len(m["thinking"]) + RESET)
        print()
    return 0


def cmd_select(a):
    path = arclib.selection_path(a.work)
    if a.action == "ls":
        data = arclib.read_selection(a.collection, path)
        if a.json:
            out_json(data)
            return 0
        if not data:
            print("（空）")
            return 0
        conn = arclib.open_db(a.db)
        for col, ids in data.items():
            print(BOLD + "[%s] %d 个对话" % (col, len(ids)) + RESET)
            for c in arclib.list_conversations(conn, ids):
                print("  %-9s %5d 条  %s" % (c["source"], c["message_count"],
                                             (c["title"] or "(无标题)")[:52]))
                print(DIM + "      " + c["conversation_id"] + RESET)
        return 0
    action = "remove" if a.action == "rm" else a.action
    for cid in a.ids:
        rec = arclib.append_selection(cid, action=action, collection=a.collection,
                                      note=a.note, path=path)
        print("%s %s -> %s" % (a.action, rec["conversation_id"], rec["collection"]))
    return 0


def cmd_export(a):
    import export as export_mod
    ids = arclib.selected_ids(a.collection, arclib.selection_path(a.work))
    if not ids:
        print("选集 [%s] 是空的" % a.collection, file=sys.stderr)
        return 1
    n = export_mod.export(a.collection, ids, fmt=a.format, out_path=a.out,
                          db=a.db, include_thinking=a.with_thinking)
    print("已导出 %d 个对话 -> %s" % (n, a.out or "(stdout)"))
    return 0


# --------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="archive_cli.py", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--db", help="archive.sqlite 路径")
    p.add_argument("--work", help="work 目录（默认 <项目>/work）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("search", help="混合检索（中文 LIKE，英文 FTS）")
    s.add_argument("query")
    s.add_argument("--role", choices=["user", "assistant", "system"])
    s.add_argument("--source", choices=["chatgpt", "deepseek", "gemini"])
    s.add_argument("--from", dest="date_from", metavar="YYYY-MM-DD")
    s.add_argument("--to", dest="date_to", metavar="YYYY-MM-DD")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--offset", type=int, default=0)
    s.add_argument("--json", action="store_true")
    s.add_argument("--link", action="store_true", help="同时打印网页跳转链接")
    s.set_defaults(func=cmd_search)

    s = sub.add_parser("stats", help="总量统计")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_stats)

    s = sub.add_parser("terms", help="词频统计（n-gram）")
    s.add_argument("--top", type=int, default=40)
    s.add_argument("--ngram", type=int, default=2)
    s.add_argument("--min-count", type=int, default=20)
    s.add_argument("--source", choices=["chatgpt", "deepseek", "gemini"])
    s.add_argument("--with-thinking", action="store_true")
    s.add_argument("--max-msgs", type=int, default=0, help="只统计前 N 条消息（0=全部）")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_terms)

    s = sub.add_parser("cooccur", help="两个词同现的统计")
    s.add_argument("term_a")
    s.add_argument("term_b")
    s.add_argument("--source", choices=["chatgpt", "deepseek", "gemini"])
    s.add_argument("--samples", type=int, default=5)
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_cooccur)

    s = sub.add_parser("length", help="按长度排列对话")
    s.add_argument("--order", choices=["chars", "messages", "updated"], default="chars")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--link", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_length)

    s = sub.add_parser("show", help="看一个对话（默认只给片段）")
    s.add_argument("cid")
    s.add_argument("--full", action="store_true", help="打印完整正文")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("select", help="维护选集")
    s.add_argument("action", choices=["add", "rm", "ls"])
    s.add_argument("ids", nargs="*")
    s.add_argument("--collection", default="default")
    s.add_argument("--note")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_select)

    s = sub.add_parser("export", help="导出选集")
    s.add_argument("--collection", default="default")
    s.add_argument("--format", choices=["md", "html", "jsonl"], default="md")
    s.add_argument("-o", "--out")
    s.add_argument("--with-thinking", action="store_true")
    s.set_defaults(func=cmd_export)

    return p


def main(argv=None):
    a = build_parser().parse_args(argv)
    if a.cmd == "select" and a.action != "ls" and not a.ids:
        print("select %s 需要至少一个 conversation_id" % a.action, file=sys.stderr)
        return 2
    return a.func(a)


if __name__ == "__main__":
    sys.exit(main())
