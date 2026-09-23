#!/usr/bin/env python3
"""纯本地、不联网的启发式分类器。

它做和 LLM 分析**同一件事**——给每个对话打价值分、分类型、贴标签、
估一个粗略的情绪——但只用本地规则，不把任何内容发出去。

结果写进 work/analysis.sqlite（走 analysis.upsert），字段和 LLM 结果一模一样，
所以 Web 的分析页、MCP 工具、status/timeline/tiers/show 全都能直接复用。
区别只在 model='heuristic'、prompt_version='heur-v1'，一眼能看出来是本地规则。

情绪部分是一个很粗糙的词典法，只当个参考，别太当真。
"""
from __future__ import annotations

import re

import analysis
import arclib

MODEL = "heuristic"
PROMPT_VERSION = "heur-v1"

FILLER_RE = re.compile(
    r"^(继续|好的?|嗯+|哦+|谢谢|感谢|ok|okay|yes|no|对|对的|是的|可以|行|好|"
    r"在吗|你好|嗨|hi|hello|？+|\?+)[。！!~，,\s]*$", re.I)
ERROR_RE = re.compile(
    r"(报错|错误|失败|崩溃|异常|无法|不能|不对|报错|Error|Traceback|Exception|"
    r"failed|failure|error|bug)", re.I)
HEADING_RE = re.compile(r"(?m)^\s{0,3}#{1,6}\s+\S")
BULLET_RE = re.compile(r"(?m)^\s{0,3}[-*+]\s+\S")
NUM_RE = re.compile(r"(?m)^\s{0,3}\d+[.)、]\s*\S")
TABLE_RE = re.compile(r"(?m)^\s*\|.*\|\s*$")
INDENT_CODE_RE = re.compile(r"(?m)^(?: {4}|\t)\S")

CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,6}")
WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_+#.-]{2,}")

STOP = {
    "什么", "怎么", "可以", "这个", "那个", "现在", "如果", "就是", "没有", "但是",
    "因为", "所以", "一个", "我们", "你们", "他们", "自己", "这样", "那样", "还是",
    "已经", "应该", "需要", "可能", "知道", "问题", "使用", "进行", "通过", "以及",
    "或者", "然后", "时候", "一下", "不是", "不能", "如何", "哪些", "为什么", "请问",
    "帮我", "给我", "看看", "这里", "那里", "这些", "那些", "感觉", "觉得", "任何",
    "the", "and", "for", "you", "are", "with", "that", "this", "have", "from",
    "not", "but", "can", "will", "how", "what", "when", "was", "were", "has",
    "its", "it's", "about", "would", "could", "should", "there", "their", "been",
}

KIND_RULES = [
    ("技术", r"代码|编程|程序|python|java|javascript|typescript|node|rust|golang|"
             r"\bsql\b|linux|unix|bash|shell|docker|kubernetes|git|nginx|redis|"
             r"报错|错误|error|traceback|bug|编译|部署|服务器|接口|\bapi\b|函数|"
             r"脚本|正则|数据库|配置|安装|驱动|内核|终端|命令行|算法"),
    ("创作", r"写一首|写一段|写一个|写篇|小说|故事|诗|文案|剧本|起名|取名|翻译|"
             r"润色|改写|扩写|续写|生成一|标题|演讲稿|邮件|简历"),
    ("学习", r"学习|教程|原理|解释一下|解释下|为什么|怎么理解|概念|数学|证明|物理|"
             r"化学|历史|英语|单词|考试|复习|笔记|入门|区别|对比"),
    ("工作", r"工作|项目|需求|会议|汇报|方案|排期|加班|老板|领导|同事|面试|"
             r"求职|合同|工资|绩效|任务|计划|目标"),
    ("生活", r"天气|吃饭|吃什么|推荐|买|购物|旅行|旅游|电影|音乐|游戏|健康|"
             r"运动|锻炼|睡眠|失眠|菜谱|做饭|装修|手机|电脑|价格"),
    ("情绪", r"难过|焦虑|开心|郁闷|烦|压力|情绪|睡不着|孤独|害怕|生气|愤怒|"
             r"失望|崩溃|累|痛苦|心情|难受|想哭"),
    ("查询", r"是什么|多少|什么时候|哪里|查一下|搜索|谁是|几点|怎么查|有没有"),
    ("闲聊", r"你好|在吗|哈哈|谢谢|再见|无聊|聊天|晚安|早安|讲个笑话"),
]

JOY = ["开心", "高兴", "喜欢", "谢谢", "感谢", "太好了", "成功", "搞定", "解决",
       "不错", "哈哈", "满意", "顺利", "期待", "幸福", "棒", "爽", "舒服", "加油"]
SAD = ["难过", "低落", "失望", "孤独", "绝望", "想哭", "伤心", "委屈", "郁闷",
       "沮丧", "心痛", "痛苦"]
ANG = ["生气", "愤怒", "烦", "恶心", "讨厌", "气死", "烦躁", "恼火", "无语",
       "火大"]
ANX = ["焦虑", "担心", "害怕", "紧张", "压力", "着急", "急", "慌", "不安",
       "怕", "愁"]
FAT = ["累", "疲惫", "困", "熬夜", "加班", "忙", "乏力", "没精神", "疲倦"]


def _count_any(text, words):
    n = 0
    for w in words:
        n += text.count(w)
    return n


def _tokens(*texts):
    counts = {}
    for t in texts:
        t = t or ""
        for m in CJK_RE.findall(t) + WORD_RE.findall(t):
            w = m.lower()
            if w in STOP or len(w) < 2:
                continue
            counts[w] = counts.get(w, 0) + 1
    return counts


def _topics(title, user_text, kind, limit=5):
    counts = _tokens(title, user_text)
    if not counts:
        return [kind] if kind and kind != "其他" else []
    title_low = (title or "").lower()
    ranked = sorted(counts.items(),
                    key=lambda kv: (kv[1] + (3 if kv[0] in title_low else 0), kv[0]),
                    reverse=True)
    out = []
    for w, _c in ranked:
        if w not in out:
            out.append(w[:24])
        if len(out) >= limit:
            break
    return out


def _kind(text, title):
    hay = (title or "") + "\n" + (text or "")
    best, best_n = "其他", 0
    for name, pat in KIND_RULES:
        n = len(re.findall(pat, hay, re.I))
        if n > best_n:
            best, best_n = name, n
    return best


def _emotions(user_text):
    j = _count_any(user_text, JOY)
    s = _count_any(user_text, SAD)
    a = _count_any(user_text, ANG)
    x = _count_any(user_text, ANX)
    f = _count_any(user_text, FAT)
    total = j + s + a + x + f
    pos, neg = j, s + a + x + f
    if total == 0:
        sentiment, intensity = 0.0, 0.0
    else:
        sentiment = (pos - neg) / float(total)
        intensity = min(1.0, total / 6.0)
    def nz(v):
        return round(min(1.0, v / 3.0), 3)
    raw = {"joy": nz(j), "sadness": nz(s), "anger": nz(a),
           "anxiety": nz(x), "fatigue": nz(f)}
    raw["calm"] = round(max(0.0, 1.0 - max(raw.values())), 3)
    return round(max(-1.0, min(1.0, sentiment)), 3), round(intensity, 3), raw


def classify_conversation(conn, cid):
    """对一个对话跑本地规则，返回可直接 analysis.upsert 的记录（没有则 None）。"""
    data = arclib.get_conversation(conn, cid, with_thinking=True)
    if not data:
        return None
    conv = data["conversation"]
    msgs = data["messages"]

    n_total = len(msgs)
    n_user = n_asst = 0
    user_chars = asst_chars = 0
    user_texts = []
    asst_texts = []
    has_code = structured = attachments = thinking = False
    err_hits = 0

    for m in msgs:
        text = m.get("text") or ""
        role = m.get("role")
        if role == "user":
            n_user += 1
            user_chars += len(text)
            user_texts.append(text)
        else:
            n_asst += 1
            asst_chars += len(text)
            asst_texts.append(text)
            if HEADING_RE.search(text) or BULLET_RE.search(text) \
                    or NUM_RE.search(text) or TABLE_RE.search(text):
                structured = True
        if "```" in text or INDENT_CODE_RE.search(text):
            has_code = True
        if m.get("attachments"):
            attachments = True
        if (m.get("thinking") or "").strip():
            thinking = True
        err_hits += len(ERROR_RE.findall(text))

    user_all = "\n".join(user_texts)
    asst_all = "\n".join(asst_texts)

    fillers = sum(1 for t in user_texts
                  if not t.strip() or FILLER_RE.match(t.strip()))
    filler_ratio = (fillers / n_user) if n_user else 0.0

    # ---- 打分（每条规则都记进 value_reason，方便看懂为什么）----
    value = 1
    reasons = []
    if has_code and asst_chars >= 1200:
        value += 2
        reasons.append("含代码且产出较多+2")
    if user_chars >= 1500:
        value += 1
        reasons.append("用户输入量大+1")
    if structured:
        value += 1
        reasons.append("结构化输出+1")
    if attachments:
        value += 1
        reasons.append("带附件+1")
    elif thinking:
        value += 1
        reasons.append("模型深度思考+1")
    if n_total >= 20 and asst_chars >= 4000:
        value += 1
        reasons.append("长对话+1")
    if n_total <= 2:
        value -= 1
        reasons.append("仅1-2条-1")
    if n_user >= 3 and filler_ratio >= 0.5:
        value -= 1
        reasons.append("多为填充语-1")
    if user_chars < 40 and n_user <= 2:
        value -= 1
        reasons.append("用户几乎没输入-1")
    value = max(0, min(5, value))

    kind = _kind(user_all, conv.get("title"))
    sentiment, intensity, emotions = _emotions(user_all)
    topics = _topics(conv.get("title"), user_all, kind)

    first = ""
    for t in user_texts:
        if t.strip() and not FILLER_RE.match(t.strip()):
            first = " ".join(t.strip().split())
            break
    if not first and user_texts:
        first = " ".join(user_texts[0].split())
    first = first[:42]
    summary = ("%s · %d条" % (first, n_total)) if first else ("%d 条消息" % n_total)

    return {
        "conversation_id": cid,
        "source": conv.get("source"),
        "title": conv.get("title"),
        "conv_created_at": conv.get("created_at"),
        "model": MODEL,
        "prompt_version": PROMPT_VERSION,
        "value": value,
        "keep": value >= 3,
        "value_reason": "、".join(reasons) or "无突出信号",
        "kind": kind,
        "topics": topics,
        "sentiment": sentiment,
        "emotions": emotions,
        "intensity": intensity,
        "summary": summary[:60],
        "analyzed_at": arclib.now_iso(),
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost": 0.0,
    }


def _main(argv=None):
    import json
    import sys
    import time
    from pathlib import Path

    db = arclib.DEFAULT_DB
    adb = analysis.ANALYSIS_DB
    limit = 0
    force = False
    args = list(sys.argv[1:] if argv is None else argv)
    while args:
        a = args.pop(0)
        if a == "--db":
            db = Path(args.pop(0))
        elif a == "--analysis-db":
            adb = Path(args.pop(0))
        elif a == "--limit":
            limit = int(args.pop(0))
        elif a == "--force":
            force = True
    conn = arclib.open_db(db)
    aconn = analysis.open_analysis_db(adb)
    done = set() if force else analysis.analyzed_ids(aconn)
    rows = conn.execute(
        "SELECT conversation_id FROM conversations ORDER BY created_at").fetchall()
    ids = [r["conversation_id"] for r in rows
           if r["conversation_id"] not in done]
    if limit:
        ids = ids[:limit]
    print("本地启发式分类：%d 个对话" % len(ids))
    buckets, kinds = {i: 0 for i in range(6)}, {}
    t0 = time.time()
    for i, cid in enumerate(ids, 1):
        rec = classify_conversation(conn, cid)
        if not rec:
            continue
        analysis.upsert(aconn, rec)
        buckets[rec["value"]] += 1
        kinds[rec["kind"]] = kinds.get(rec["kind"], 0) + 1
    conn.close()
    aconn.close()
    print("完成 %d 个，用时 %.1fs" % (len(ids), time.time() - t0))
    print("价值分布 " + "  ".join("%d:%d" % (v, c) for v, c in sorted(buckets.items())))
    print("类型分布 " + json.dumps(kinds, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
