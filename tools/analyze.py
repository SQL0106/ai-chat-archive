#!/usr/bin/env python3
"""LLM 分析的命令行入口。

  python3 tools/analyze.py config            # 看当前用的是哪家模型（脱敏）
  python3 tools/analyze.py config --init     # 生成 work/llm.json 模板
  python3 tools/analyze.py estimate          # 抽样估算 token 与花费（不联网）
  python3 tools/analyze.py run --limit 100   # 真正开始分析（联网，可断点续跑）
  python3 tools/analyze.py status            # 分析进度
  python3 tools/analyze.py timeline          # 按周的情绪/价值表
  python3 tools/analyze.py show <id>         # 看某个对话的分析结果
  python3 tools/analyze.py tiers             # 高/中/低价值分档
  python3 tools/analyze.py heuristic --all   # 纯本地规则分类（不联网、不要 key）

默认全部走 nice -n 19 之外的轻量逻辑，真正跑 run 时建议：
  systemd-run --user --scope -p CPUQuota=40% --collect nice -n 19 \
      python3 tools/analyze.py run --limit 200
"""
from __future__ import annotations

import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import analysis  # noqa: E402
import arclib  # noqa: E402
import heur  # noqa: E402
import llm  # noqa: E402

BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"

CONFIG_TEMPLATE = {
    "preset": "deepseek",
    "base_url": "https://api.deepseek.com/v1",
    "api_key_env": "DEEPSEEK_API_KEY",
    "model": "deepseek-chat",
    "timeout": 120,
    "temperature": 0.2,
    "max_tokens": 900,
    "retries": 3,
    "_comment": "官方文档 https://api-docs.deepseek.com/zh-cn/ ；换成任何 OpenAI 兼容服务都行",
}

ZHIPU_TEMPLATE = {
    "preset": "zhipu",
    "api_key_env": "ZHIPU_API_KEY",
    "model": "glm-4.7-flash",
    "_comment": ("智谱 GLM-4.7-Flash 目前免费；文档 "
                 "https://docs.bigmodel.cn/cn/api/introduction ；"
                 "key 在 https://bigmodel.cn/usercenter/proj-mgmt/apikeys 创建后设为环境变量"),
}


def _bar(n, top, width=24):
    if top <= 0:
        return ""
    filled = int(round(width * n / top))
    return "█" * filled + "·" * (width - filled)


def _targets(args):
    """挑出这次要分析哪些对话。"""
    conn = arclib.open_db(args.db)
    try:
        if args.collection:
            ids = arclib.selected_ids(args.collection, path=args.work)
            rows = arclib.list_conversations(conn, ids)
        else:
            sql = ["SELECT conversation_id, source, title, created_at, message_count"
                   " FROM conversations WHERE 1=1"]
            params = []
            if args.source:
                sql.append(" AND source = ?")
                params.append(args.source)
            if args.from_date:
                sql.append(" AND created_at >= ?")
                params.append(arclib.parse_date(args.from_date))
            if args.to_date:
                sql.append(" AND created_at <= ?")
                params.append(arclib.parse_date(args.to_date))
            sql.append(" ORDER BY created_at")
            rows = [dict(r) for r in conn.execute(" ".join(sql), params).fetchall()]
        if args.min_msgs:
            rows = [r for r in rows if (r.get("message_count") or 0) >= args.min_msgs]
        return conn, rows
    except Exception:
        conn.close()
        raise


def _select_pending(conn, rows, args):
    aconn = analysis.open_analysis_db(args.analysis_db)
    try:
        done = analysis.analyzed_ids(aconn)
    finally:
        aconn.close()
    if getattr(args, "force", False):
        return rows
    return [r for r in rows if r["conversation_id"] not in done]


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def cmd_config(a):
    if a.init:
        p = Path(a.config) if a.config else llm.CONFIG_PATH
        if p.exists() and not a.force:
            print("已存在，未覆盖：%s（加 --force 覆盖）" % p)
            return 0
        p.parent.mkdir(parents=True, exist_ok=True)
        template = ZHIPU_TEMPLATE if (a.preset or "").strip() == "zhipu" else CONFIG_TEMPLATE
        p.write_text(json.dumps(template, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
        print("已写入模板：%s" % p)
    print(json.dumps(llm.describe(llm.load_config(a.config, preset=a.preset)),
                     ensure_ascii=False, indent=2))
    return 0


def cmd_estimate(a):
    conn, rows = _targets(a)
    aconn = analysis.open_analysis_db(a.analysis_db)
    try:
        pending = _select_pending(conn, rows, a)
        n = len(pending)
        sample_n = min(a.sample, n)
        rnd = random.Random(7)
        sample = rnd.sample(pending, sample_n) if sample_n else []
        chars = []
        for r in sample:
            d = analysis.build_digest(conn, r["conversation_id"])
            if d:
                chars.append(d["chars"])
    finally:
        aconn.close()
        conn.close()

    cfg = llm.load_config(a.config)
    sys_chars = len(analysis.SYSTEM_PROMPT) + 200
    avg = (sum(chars) / len(chars)) if chars else 0.0
    # 中文大致 1 token ≈ 1.4 字，取 0.7 token/字
    in_tok = (avg * 0.7 + sys_chars * 0.7) if avg else 0
    out_tok = 190
    price = llm.PRICES.get(cfg.get("model") or "", {"in": 0.0, "out": 0.0})
    cost = (n * in_tok * price["in"] + n * out_tok * price["out"]) / 1_000_000.0

    print("%s待分析对话%s   %d 个（抽样 %d 个估算长度）" % (BOLD, RESET, n, sample_n))
    print("平均摘要长度     %d 字（上限 %d）" % (int(avg), analysis.DIGEST_MAX_CHARS))
    print("模型             %s @ %s" % (cfg.get("model"), cfg.get("base_url")))
    print("key 可用         %s" % ("是" if llm.has_key(cfg) else "否（还没配）"))
    print("预估输入 token   ~%s" % f"{int(n * in_tok):,}")
    print("预估输出 token   ~%s" % f"{int(n * out_tok):,}")
    if (cfg.get("model") or "") not in llm.PRICES:
        print("预估花费         未知（价目表里没有 %s）" % cfg.get("model"))
    elif price["in"] or price["out"]:
        print("预估花费         ~¥%.2f（按 %s 的价目表）" % (cost, cfg.get("model")))
    else:
        print("预估花费         ¥0（%s 免费）" % cfg.get("model"))
    print("%s提示：run 支持断点续跑，中断了直接再跑一次即可。%s" % (DIM, RESET))
    return 0


def cmd_run(a):
    cfg = llm.load_config(a.config)
    if not llm.has_key(cfg):
        print("没有可用的 API key。先跑 `analyze.py config --init` 再填 key。")
        return 2
    conn, rows = _targets(a)
    aconn = analysis.open_analysis_db(a.analysis_db)
    pending = _select_pending(conn, rows, a)
    if a.limit:
        pending = pending[:a.limit]
    total = len(pending)
    print("%s开始分析 %d 个对话%s（模型 %s）" % (BOLD, total, RESET, cfg.get("model")))
    ok = fail = 0
    cost = 0.0
    t0 = time.time()
    try:
        for i, r in enumerate(pending, 1):
            cid = r["conversation_id"]
            digest = analysis.build_digest(conn, cid)
            if not digest:
                continue
            if a.dry_run:
                print(json.dumps(digest, ensure_ascii=False)[:800])
                print("--- dry-run，只看了第一个就停 ---")
                break
            try:
                obj, res = llm.chat_json(analysis.build_messages(digest), cfg=cfg)
                rec = analysis.normalize_result(obj, cid, digest)
                usage = res.get("usage") or {}
                rec["model"] = res.get("model")
                rec["analyzed_at"] = arclib.now_iso()
                rec["prompt_tokens"] = usage.get("prompt_tokens")
                rec["completion_tokens"] = usage.get("completion_tokens")
                c = llm.estimate_cost(usage, res.get("model"))
                rec["cost"] = c
                if c:
                    cost += c
                analysis.upsert(aconn, rec)
                ok += 1
                print("[%d/%d] v=%d %-6s %s  %s" % (
                    i, total, rec["value"], rec["kind"],
                    _clip(digest.get("title"), 28),
                    _clip(rec.get("summary"), 40)))
            except Exception as e:  # noqa: BLE001
                fail += 1
                analysis.record_error(aconn, cid, cfg.get("model"), e)
                print("[%d/%d] %s失败%s %s" % (i, total, DIM, RESET, _clip(str(e), 120)))
                if a.stop_after_errors and fail >= a.stop_after_errors:
                    print("连续失败达到上限，停止。")
                    break
            if a.sleep:
                time.sleep(a.sleep)
    except KeyboardInterrupt:
        print("\n已中断，下次直接重跑即可续上。")
    finally:
        conn.close()
        aconn.close()
    dt = time.time() - t0
    print("%s完成%s 成功 %d / 失败 %d，用时 %.1fs，本次花费 ~¥%.3f" % (
        BOLD, RESET, ok, fail, dt, cost))
    return 0


def cmd_heuristic(a):
    """纯本地规则分类：不联网、不要 key、几秒钟跑完。"""
    conn, rows = _targets(a)
    aconn = analysis.open_analysis_db(a.analysis_db)
    pending = _select_pending(conn, rows, a)
    if a.limit:
        pending = pending[:a.limit]
    total = len(pending)
    print("%s本地启发式分类%s %d 个对话（模型 %s，不联网）" % (
        BOLD, RESET, total, heur.MODEL))
    ok = 0
    buckets = {i: 0 for i in range(6)}
    kinds = {}
    t0 = time.time()
    try:
        for i, r in enumerate(pending, 1):
            cid = r["conversation_id"]
            rec = heur.classify_conversation(conn, cid)
            if not rec:
                continue
            analysis.upsert(aconn, rec)
            ok += 1
            buckets[rec["value"]] += 1
            kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
            if a.verbose:
                print("[%d/%d] v=%d %-4s %s  %s" % (
                    i, total, rec["value"], rec["kind"],
                    _clip(rec.get("title"), 28), _clip(rec.get("summary"), 40)))
    except KeyboardInterrupt:
        print("\n已中断，下次直接重跑即可续上。")
    finally:
        conn.close()
        aconn.close()
    print("%s完成%s 共 %d 个，用时 %.1fs" % (BOLD, RESET, ok, time.time() - t0))
    print("  价值分布 %s" % "  ".join(
        "%d:%d" % (v, c) for v, c in sorted(buckets.items())))
    print("  类型分布 %s" % "  ".join(
        "%s:%d" % (k, c) for k, c in sorted(kinds.items(), key=lambda kv: -kv[1])))
    print("%s情绪是词典法粗估，仅供参考。%s" % (DIM, RESET))
    return 0


def cmd_status(a):
    conn = analysis.open_analysis_db(a.analysis_db)
    try:
        st = analysis.status(conn)
    finally:
        conn.close()
    print(json.dumps(st, ensure_ascii=False, indent=2) if a.json else _fmt_status(st))
    return 0


def _fmt_status(st):
    tot = st.get("total_conversations")
    out = []
    out.append("%s分析进度%s" % (BOLD, RESET))
    if tot:
        out.append("  已分析   %d / %d（%.1f%%）" % (
            st["analyzed"], tot, 100.0 * st["analyzed"] / tot))
    else:
        out.append("  已分析   %d" % st["analyzed"])
    out.append("  失败     %d" % st["errors"])
    out.append("  建议保留 %d（value>=3）" % st["keep"])
    if st.get("avg_value") is not None:
        out.append("  平均价值 %.2f" % st["avg_value"])
    if st.get("first"):
        out.append("  时间范围 %s ~ %s" % (st["first"][:19], st["last"][:19]))
    out.append("  token    输入 %s / 输出 %s" % (
        f"{st['prompt_tokens']:,}", f"{st['completion_tokens']:,}"))
    if st.get("cost") is not None:
        out.append("  累计花费 ~¥%.3f" % st["cost"])
    out.append("  价值分布 %s" % "  ".join(
        "%d:%d" % (v, c) for v, c in sorted(st["value_buckets"].items())))
    return "\n".join(out)


def cmd_timeline(a):
    conn = analysis.open_analysis_db(a.analysis_db)
    try:
        weeks = analysis.weekly(conn, date_from=a.from_date, date_to=a.to_date)
    finally:
        conn.close()
    if not weeks:
        print("还没有分析数据。先跑 `analyze.py run`。")
        return 0
    if a.json:
        print(json.dumps(weeks, ensure_ascii=False, indent=2))
        return 0
    top = max(w["n"] for w in weeks)
    print("%s按周情绪 / 价值%s  （共 %d 周）" % (BOLD, RESET, len(weeks)))
    print("%-12s %4s %6s %7s %7s  %s" % ("周（周一）", "对话", "价值", "情绪", "强度", "对话量"))
    for w in weeks:
        print("%-12s %4d %6.2f %+7.2f %7.2f  %s" % (
            w["week"], w["n"], w["value"], w["sentiment"], w["intensity"],
            _bar(w["n"], top, 18)))
    print()
    print("%s情绪基调：+1 积极 / 0 平淡 / -1 消极；价值 0-5。%s" % (DIM, RESET))
    if a.verbose:
        print("\n%s各周情绪构成%s" % (BOLD, RESET))
        for w in weeks:
            parts = ["%s %.2f" % (analysis.EMOTION_LABELS[k], w["emotions"][k])
                     for k in analysis.EMOTIONS if w["emotions"][k] > 0.02]
            print("  %s  %s" % (w["week"], "  ".join(parts) or "—"))
    return 0


def cmd_show(a):
    conn = analysis.open_analysis_db(a.analysis_db)
    try:
        rec = analysis.get(conn, a.id)
    finally:
        conn.close()
    if not rec:
        print("没有这个对话的分析结果：%s" % a.id)
        return 1
    print(json.dumps(rec, ensure_ascii=False, indent=2) if a.json else _fmt_rec(rec))
    return 0


def _fmt_rec(r):
    lines = ["%s%s%s" % (BOLD, r.get("title") or "(无标题)", RESET),
             "  来源 %s   时间 %s" % (r.get("source"), r.get("conv_created_at")),
             "  价值 %s/5  保留 %s  类型 %s" % (
                 r.get("value"), "是" if r.get("keep") else "否", r.get("kind")),
             "  理由 %s" % (r.get("value_reason") or "—"),
             "  情绪 %+.2f  强度 %.2f" % (r.get("sentiment") or 0, r.get("intensity") or 0)]
    emo = "  ".join("%s %.2f" % (analysis.EMOTION_LABELS[k], r["emotions"].get(k, 0))
                    for k in analysis.EMOTIONS if r["emotions"].get(k, 0) > 0.02)
    if emo:
        lines.append("  构成 %s" % emo)
    if r.get("topics"):
        lines.append("  标签 %s" % "、".join(r["topics"]))
    if r.get("summary"):
        lines.append("  摘要 %s" % r["summary"])
    return "\n".join(lines)


def cmd_tiers(a):
    conn = analysis.open_analysis_db(a.analysis_db)
    try:
        tiers = analysis.value_tiers(conn)
    finally:
        conn.close()
    if a.json:
        print(json.dumps({k: len(v) for k, v in tiers.items()}, ensure_ascii=False))
        return 0
    print("%s价值分档%s" % (BOLD, RESET))
    print("  高价值 value>=4  %d" % len(tiers["high"]))
    print("  一般   value==3  %d" % len(tiers["mid"]))
    print("  低价值 value<=2  %d" % len(tiers["low"]))
    if a.dump:
        for name in ("high", "mid", "low"):
            p = Path(a.dump) / (name + ".txt")
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("\n".join(tiers[name]) + "\n", encoding="utf-8")
            print("  已写出 %s" % p)
    return 0


def _clip(s, n):
    s = str(s or "")
    return s if len(s) <= n else s[:n] + "…"


def build_parser():
    p = argparse.ArgumentParser(description="聊天记录的 LLM 分析工具")
    p.add_argument("--db", default=str(arclib.DEFAULT_DB), help="归档数据库")
    p.add_argument("--work", default=str(arclib.DEFAULT_WORK), help="选集目录")
    p.add_argument("--analysis-db", default=None, help="分析结果库（默认 work/analysis.sqlite）")
    p.add_argument("--config", default=None, help="LLM 配置文件（默认 work/llm.json）")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("config", help="查看/生成 LLM 配置")
    c.add_argument("--init", action="store_true")
    c.add_argument("--force", action="store_true")
    c.add_argument("--preset", default=None,
                   help="写模板时用哪套预设：deepseek / zhipu（智谱 GLM-4.7-Flash 免费）")
    c.set_defaults(func=cmd_config)

    e = sub.add_parser("estimate", help="估算 token 与花费（不联网）")
    _add_filter_args(e)
    e.add_argument("--sample", type=int, default=60, help="抽样多少个估算长度")
    e.set_defaults(func=cmd_estimate)

    r = sub.add_parser("run", help="真正开始分析（联网）")
    _add_filter_args(r)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--force", action="store_true", help="已分析的也重跑")
    r.add_argument("--sleep", type=float, default=0.0, help="每次调用之间停几秒")
    r.add_argument("--stop-after-errors", type=int, default=8)
    r.add_argument("--dry-run", action="store_true", help="只打印摘要，不联网")
    r.set_defaults(func=cmd_run)

    h = sub.add_parser("heuristic", help="纯本地规则分类（不联网，不要 key）")
    _add_filter_args(h)
    h.add_argument("--limit", type=int, default=0)
    h.add_argument("--all", action="store_true", help="全部分类（默认就是全部）")
    h.add_argument("--force", action="store_true", help="已分析的也重跑")
    h.add_argument("-v", "--verbose", action="store_true")
    h.set_defaults(func=cmd_heuristic)

    s = sub.add_parser("status", help="分析进度")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_status)

    t = sub.add_parser("timeline", help="按周的情绪/价值表")
    t.add_argument("--from", dest="from_date", default="")
    t.add_argument("--to", dest="to_date", default="")
    t.add_argument("--json", action="store_true")
    t.add_argument("-v", "--verbose", action="store_true")
    t.set_defaults(func=cmd_timeline)

    sh = sub.add_parser("show", help="看某个对话的分析结果")
    sh.add_argument("id")
    sh.add_argument("--json", action="store_true")
    sh.set_defaults(func=cmd_show)

    ti = sub.add_parser("tiers", help="高/中/低价值分档")
    ti.add_argument("--json", action="store_true")
    ti.add_argument("--dump", default="", help="把 id 列表写到这个目录")
    ti.set_defaults(func=cmd_tiers)
    return p


def _add_filter_args(sp):
    sp.add_argument("--collection", default="", help="只分析某个选集")
    sp.add_argument("--source", default="", help="只分析某个来源")
    sp.add_argument("--from", dest="from_date", default="")
    sp.add_argument("--to", dest="to_date", default="")
    sp.add_argument("--min-msgs", type=int, default=0)


def main(argv=None):
    a = build_parser().parse_args(argv)
    return a.func(a) or 0


if __name__ == "__main__":
    sys.exit(main())
