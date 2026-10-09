#!/usr/bin/env python3
"""智能增量导入：解析 raw/ 新增或变更的导出，与 out/archive.sqlite 现有归档
按 (source, conversation_id) 合并，只重写变化部分，并可自动串联分析。

用法：
  python3 scripts/incremental.py             # 增量导入
  python3 scripts/incremental.py --dry-run   # 只解析合并，不写盘
  python3 scripts/incremental.py --analyze   # 导入后自动 heuristic+LLM 分析
"""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "scripts"))

import archive_ai_chats as aac  # noqa: E402

KIND_SOURCE = {"chatgpt": "chatgpt", "deepseek": "deepseek",
               "activity": "gemini", "gemini": "gemini"}
MD_SOURCES = ("chatgpt", "deepseek", "gemini")


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Runner:
    """进度写 work/import_progress.json（原子+20s 心跳），日志追加 work/import.log。"""

    def __init__(self, work):
        self.work = Path(work)
        self.work.mkdir(parents=True, exist_ok=True)
        self.path = self.work / "import_progress.json"
        self.log_path = self.work / "import.log"
        self.data = {
            "running": True, "finished": False, "ok": None, "phase": "init",
            "i": 0, "total": 0, "step": "", "started": now_iso(),
            "updated": now_iso(), "ended_at": "", "error": "", "stats": {},
        }
        self._stop = threading.Event()

    def _write(self):
        tmp = self.path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), "utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    def update(self, **kw):
        self.data.update(kw)
        self.data["updated"] = now_iso()
        self._write()

    def log(self, msg, **kw):
        line = "[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError:
            pass
        print(line, flush=True)
        kw.setdefault("step", msg)
        self.update(**kw)

    def start(self):
        self._write()

        def beat():
            while not self._stop.wait(20):
                self.update()
        threading.Thread(target=beat, daemon=True).start()

    def finish(self, ok, error="", stats=None):
        self._stop.set()
        self.data.update(running=False, finished=True, ok=bool(ok),
                         error=error or "", stats=stats or {},
                         ended_at=now_iso(), updated=now_iso())
        self._write()


def load_base(db_path):
    """从 out/archive.sqlite 读回全部对话，结构同 parse_* 输出。"""
    base = {}
    if not Path(db_path).exists():
        return base
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        conn.row_factory = sqlite3.Row
        # 空库/半截库（web 连接重建的 0 字节文件、复位残留）没有表 → 视为无基座
        has_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'"
        ).fetchone()
        if not has_table:
            conn.close()
            return base
    except sqlite3.Error:
        return base
    try:
        for r in conn.execute(
            "SELECT conversation_id, source, title, created_at, updated_at FROM conversations"
        ):
            key = ((r["source"] or ""), (r["conversation_id"] or ""))
            base[key] = {
                "source": key[0], "conversation_id": key[1],
                "title": r["title"] or "", "created_at": r["created_at"] or "",
                "updated_at": r["updated_at"] or "", "messages": [],
            }
        for r in conn.execute(
            "SELECT conversation_id, source, message_index, role, timestamp,"
            " timestamp_source, model, text, thinking, attachments"
            " FROM messages ORDER BY conversation_id, message_index, id"
        ):
            conv = base.get(((r["source"] or ""), (r["conversation_id"] or "")))
            if conv is None:
                continue
            att = r["attachments"]
            if isinstance(att, str):
                try:
                    att = json.loads(att) if att else []
                except ValueError:
                    att = []
            if not isinstance(att, list):
                att = []
            conv["messages"].append({
                "message_index": r["message_index"],
                "role": r["role"] or "", "timestamp": r["timestamp"] or "",
                "timestamp_source": r["timestamp_source"] or "",
                "text": r["text"] or "", "thinking": r["thinking"] or "",
                "model": r["model"] or "", "attachments": att,
            })
    finally:
        conn.close()
    return base


def load_base_jsonl(jsonl_path):
    """从 out/normalized.jsonl 读回全部对话（断电续跑：jsonl 先于 md/sqlite 落盘）。"""
    base = {}
    p = Path(jsonl_path)
    if not p.exists() or p.stat().st_size == 0:
        return base
    try:
        with p.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                src = r.get("source") or ""
                cid = r.get("conversation_id") or ""
                if not cid:
                    continue
                key = (src, cid)
                conv = base.get(key)
                if conv is None:
                    conv = {
                        "source": src, "conversation_id": cid,
                        "title": r.get("title") or "",
                        "created_at": r.get("created_at") or "",
                        "updated_at": r.get("updated_at") or "",
                        "messages": [],
                    }
                    base[key] = conv
                att = r.get("attachments")
                if not isinstance(att, list):
                    att = []
                conv["messages"].append({
                    "message_index": r.get("message_index"),
                    "role": r.get("role") or "", "timestamp": r.get("timestamp") or "",
                    "timestamp_source": r.get("timestamp_source") or "",
                    "text": r.get("text") or "", "thinking": r.get("thinking") or "",
                    "model": r.get("model") or "", "attachments": att,
                })
    except OSError:
        return {}
    for conv in base.values():
        conv["messages"].sort(key=lambda m: (m.get("message_index") if m.get("message_index") is not None else 0))
    return base


def fingerprint(conv):
    msgs = sorted(conv.get("messages") or [], key=lambda m: (m.get("message_index") or 0))
    payload = {
        "source": conv.get("source") or "",
        "title": conv.get("title") or "",
        "created_at": conv.get("created_at") or "",
        "updated_at": conv.get("updated_at") or "",
        "messages": [
            [m.get("message_index"), m.get("role") or "", m.get("timestamp") or "",
             m.get("timestamp_source") or "", m.get("text") or "",
             m.get("thinking") or "", m.get("model") or "",
             m.get("attachments") or []]
            for m in msgs
        ],
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def pick_conv(a, b):
    pa, pb = bool(a.get("_prompt_only")), bool(b.get("_prompt_only"))
    if pa != pb:
        return a if not pa else b
    la = len(a.get("messages") or [])
    lb = len(b.get("messages") or [])
    return a if la >= lb else b


def merge(base, new_convs):
    """new 覆盖 base 同 key（指纹不同才覆盖）；prompt_only 不覆盖富内容；
    活动记录源（activity）消息数不超 base 时不覆盖（旧的已经在里面，只加新的）。"""
    merged = dict(base)
    new_best = {}
    for conv in new_convs:
        key = ((conv.get("source") or ""), (conv.get("conversation_id") or ""))
        if not key[1]:
            continue
        prev = new_best.get(key)
        new_best[key] = conv if prev is None else pick_conv(prev, conv)
    added, changed = {}, {}
    for key, conv in new_best.items():
        old = merged.get(key)
        if old is None:
            merged[key] = conv
            added[key] = conv
            continue
        if conv.get("_prompt_only") and not old.get("_prompt_only"):
            continue
        if (conv.get("_from") == "activity"
                and len(conv.get("messages") or []) <= len(old.get("messages") or [])):
            continue
        if fingerprint(old) != fingerprint(conv):
            merged[key] = conv
            changed[key] = conv
            added.pop(key, None)
    return merged, added, changed


def lookup_hash(old_inputs, path):
    s = str(path)
    if s in old_inputs:
        return old_inputs[s]
    name = Path(s).name
    for k, v in old_inputs.items():
        if k.endswith("/" + name) or k == name:
            return v
    return ""


def parse_one(kind, path, args):
    if kind == "chatgpt":
        convs = aac.parse_chatgpt(path)
    elif kind == "deepseek":
        convs = aac.parse_deepseek(path)
    elif kind == "activity":
        convs = aac.parse_gemini_activity_file(path, args.activity_tz_offset)
    elif kind == "gemini":
        if path.suffix.lower() == ".json":
            fn = getattr(aac, "parse_gemini_json", None)
            if fn is None:
                raise SystemExit("Gemini JSON 解析器尚未实现: %s" % path)
            convs = fn(path)
        else:
            convs = aac.parse_gemini_ndjson(path)
    else:
        raise SystemExit("未知输入类型: %s" % kind)
    for c in convs:
        c["_from"] = kind
    return convs


def build_md_index(out):
    idx = {}
    for src in MD_SOURCES:
        d = out / "md" / src
        if not d.is_dir():
            continue
        for f in d.glob("*.md"):
            try:
                head = f.read_text("utf-8")[:600]
            except OSError:
                continue
            m = re.search(r"^conversation_id:\s*(\S+)", head, re.M)
            if m:
                idx.setdefault((src, m.group(1)), []).append(f)
    return idx


def write_md_delta(added, changed, out, tz, throttle):
    targets = {}
    for key, conv in list(added.items()) + list(changed.items()):
        if conv.get("messages"):
            targets[key] = conv
    if not targets:
        return 0
    idx = build_md_index(out)
    for key in targets:
        for f in idx.get(key, ()):
            try:
                f.unlink()
            except OSError:
                pass
    by_src = {}
    for (src, _cid), conv in targets.items():
        if src in MD_SOURCES:
            by_src.setdefault(src, []).append(conv)
    n = 0
    for src, convs in by_src.items():
        d = out / "md" / src
        d.mkdir(parents=True, exist_ok=True)
        written = aac.write_markdown(convs, d, tz, throttle)
        n += len(written) if isinstance(written, list) else int(written or 0)
    return n


def llm_ready():
    try:
        import llm
        return bool(llm.has_key())
    except Exception:
        return False


def run_cmd(cmd, runner, phase):
    runner.log("$ " + " ".join(cmd), phase=phase)
    with open(runner.log_path, "a", encoding="utf-8") as logf:
        rc = subprocess.call(cmd, cwd=str(ROOT), stdout=logf, stderr=subprocess.STDOUT)
    if rc != 0:
        runner.log("[!] 退出码 %d: %s" % (rc, " ".join(cmd[1:])))
    return rc


def _analyzed_ids(work):
    db = Path(work) / "analysis.sqlite"
    if not db.exists():
        return set()
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
        try:
            return {r[0] for r in conn.execute(
                "SELECT conversation_id FROM analysis")}
        finally:
            conn.close()
    except sqlite3.Error:
        return set()


def run_analysis(runner, added_ids, changed_ids, work):
    """只对未分析的 id 跑 heuristic+LLM（--force 仅限 fresh/stale 集合），
    防止 DB 重跑后 added 覆盖全量导致把已分析的 5500+ 全部重跑。"""
    done = _analyzed_ids(work)
    ids = sorted(set(added_ids) | set(changed_ids))
    fresh = [i for i in ids if i not in done]
    stale = sorted(set(changed_ids) & done)
    ready = llm_ready()
    runner.log("分析范围: 新增未析 %d，变更需重析 %d，已析跳过 %d"
               % (len(fresh), len(stale), len(ids) - len(fresh)))

    def chunks(seq, n=400):
        return [seq[i:i + n] for i in range(0, len(seq), n)]

    steps = []
    for ch in chunks(fresh):
        steps.append(("analyze:heur",
                      "heuristic 分析 %d 个新对话" % len(ch),
                      [sys.executable, "tools/analyze.py", "heuristic",
                       "--ids", ",".join(ch)]))
        if ready:
            steps.append(("analyze:llm",
                          "LLM 分析 %d 个新对话" % len(ch),
                          [sys.executable, "tools/analyze.py", "run",
                           "--ids", ",".join(ch), "--force"]))
    if ready:
        for ch in chunks(stale):
            steps.append(("analyze:llm",
                          "LLM 重析 %d 个变更对话" % len(ch),
                          [sys.executable, "tools/analyze.py", "run",
                           "--ids", ",".join(ch), "--force"]))
        steps.append(("analyze:pending", "补析全局 pending 对话",
                      [sys.executable, "tools/analyze.py", "run"]))
    else:
        runner.log("LLM key 不可用，跳过 LLM 分析")
    for i, (phase, msg, cmd) in enumerate(steps):
        runner.log(msg, phase=phase, i=i, total=len(steps))
        run_cmd(cmd, runner, phase)
    runner.update(phase="analyze", step="分析完成")


def run_import(args, runner):
    raw, out, work = Path(args.raw), Path(args.out), Path(args.work)
    db_path = out / "archive.sqlite"
    manifest_path = out / "manifest.json"

    runner.update(phase="base", step="读取现有归档")
    base = load_base_jsonl(out / "normalized.jsonl")
    if base:
        runner.log("现有归档 %d 对话（来自 normalized.jsonl）" % len(base))
    else:
        base = load_base(db_path)
        runner.log("现有归档 %d 对话（来自 archive.sqlite）" % len(base))

    chatgpt_files, takeout_files, gemini_files, activity_files, deepseek_files = aac.discover(raw)
    groups = ([("chatgpt", p) for p in chatgpt_files]
              + [("deepseek", p) for p in deepseek_files]
              + [("activity", p) for p in activity_files]
              + [("gemini", p) for p in gemini_files])
    todo = groups + [("takeout", p) for p in takeout_files]
    if not todo:
        raise SystemExit("在 %s 未发现任何导出文件" % raw)

    old_manifest = {}
    if manifest_path.exists():
        try:
            old_manifest = json.loads(manifest_path.read_text("utf-8"))
        except ValueError:
            old_manifest = {}
    old_inputs = old_manifest.get("inputs") or {}

    runner.update(phase="hash", i=0, total=len(todo), step="计算输入哈希")
    all_inputs = {}
    plan, skipped = [], []
    takeout_changed = False
    base_sources = {k[0] for k in base}
    for i, (kind, p) in enumerate(todo):
        try:
            h = aac.sha256_file(p)
        except OSError as e:
            runner.log("[!] 读取失败 %s: %s" % (p, e))
            continue
        all_inputs[str(p)] = h
        unchanged = (not args.force_full) and lookup_hash(old_inputs, p) == h
        if kind == "takeout":
            if not unchanged:
                takeout_changed = True
        elif unchanged and KIND_SOURCE[kind] in base_sources:
            skipped.append((kind, p))
        else:
            plan.append((kind, p))
        runner.update(i=i + 1)
    runner.log("输入 %d 个：跳过 %d（未变更），待解析 %d%s"
               % (len(todo), len(skipped), len(plan),
                  "，takeout 有变更" if takeout_changed else ""))
    inputs_changed = all_inputs != old_inputs

    runner.update(phase="parse", i=0, total=max(len(plan), 1), step="解析")
    new_convs = []
    gemini_reparsed = False
    for i, (kind, p) in enumerate(plan):
        runner.log("解析[%s]: %s" % (kind, p), phase="parse", i=i)
        new_convs.extend(parse_one(kind, p, args))
        if kind in ("activity", "gemini"):
            gemini_reparsed = True
        runner.update(i=i + 1)
        if i < len(plan) - 1 and args.parse_pause > 0:
            runner.update(step="解析完成，散热等待 %.0fs" % args.parse_pause)
            time.sleep(args.parse_pause)
    runner.log("新解析 %d 对话" % len(new_convs))

    merged, added, changed = merge(base, new_convs)
    runner.log("合并: 新增 %d，变更 %d，总计 %d 对话"
               % (len(added), len(changed), len(merged)), phase="merge")

    takeout_stats = old_manifest.get("takeout") or {}
    unmatched = old_manifest.get("unmatched_gemini_prompts") or []
    if takeout_files and (takeout_changed or gemini_reparsed):
        runner.update(phase="backfill", step="Gemini 时间回填")
        try:
            items = aac.load_takeout(takeout_files)
        except Exception as e:
            runner.log("[!] load_takeout 失败: %s" % e)
            items = []
        if items:
            gkeys = [k for k in merged if k[0] == "gemini"]
            before = {k: fingerprint(merged[k]) for k in gkeys}
            stats, unm = aac.backfill_gemini([merged[k] for k in gkeys],
                                             items, args.match_threshold)
            takeout_stats = stats or takeout_stats
            unmatched = unm or unmatched
            for k in gkeys:
                if fingerprint(merged[k]) != before[k] and k not in added:
                    changed[k] = merged[k]
            runner.log("回填: %s" % stats)

    n_msg = sum(len(c.get("messages") or []) for c in merged.values())
    summary = {
        "added": len(added), "changed": len(changed),
        "conversations": len(merged), "messages": n_msg,
        "parsed": len(plan), "skipped": len(skipped),
        "added_ids": sorted(k[1] for k in added),
        "changed_ids": sorted(k[1] for k in changed),
        "wrote": False,
    }
    runner.log("合并结果: 新增 %d，变更 %d，总计 %d 对话 / %d 消息"
               % (summary["added"], summary["changed"],
                  summary["conversations"], summary["messages"]))

    if not added and not changed:
        if inputs_changed and not args.dry_run:
            old_manifest["inputs"] = all_inputs
            old_manifest["generated_at"] = now_iso()
            old_manifest["last_import"] = {
                "mode": "incremental", "at": now_iso(), "parsed": len(plan),
                "skipped": len(skipped), "added": 0, "changed": 0,
            }
            tmp = manifest_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(old_manifest, ensure_ascii=False, indent=2), "utf-8")
            os.replace(tmp, manifest_path)
            runner.log("无对话变更，仅更新 manifest 输入清单")
        else:
            runner.log("无变化，未写盘")
        return summary

    runner.update(phase="write", step="重写归档")
    records = aac.flatten(list(merged.values()))
    if args.dry_run:
        runner.log("dry-run：不写盘")
        return summary

    out.mkdir(parents=True, exist_ok=True)
    tz = None
    if getattr(aac, "ZoneInfo", None) is not None:
        try:
            tz = aac.ZoneInfo(args.tz)
        except Exception:
            tz = None
    jsonl_path = out / "normalized.jsonl"
    runner.log("[+] %s" % jsonl_path)
    aac.write_jsonl(records, jsonl_path)
    if not args.no_md:
        runner.log("[+] Markdown 更新 %d 个" % write_md_delta(added, changed, out, tz, args.throttle))
    if not args.no_sqlite:
        runner.log("[+] %s" % db_path)
        aac.write_sqlite(list(merged.values()), records, db_path, args.throttle)

    by_source = {}
    for (src, _cid), conv in merged.items():
        d = by_source.setdefault(src, {"conversations": 0, "messages": 0})
        d["conversations"] += 1
        d["messages"] += len(conv.get("messages") or [])
    ts_stats = {}
    for r in records:
        k = r.get("timestamp_source") or "none"
        ts_stats[k] = ts_stats.get(k, 0) + 1
    g_total = by_source.get("gemini", {}).get("messages", 0)
    g_dated = sum(1 for r in records
                  if r.get("source") == "gemini" and r.get("timestamp"))
    md_total = sum(len(list((out / "md" / s).glob("*.md"))) for s in MD_SOURCES)
    manifest = {
        "generated_at": now_iso(),
        "inputs": all_inputs,
        "by_source": by_source,
        "timestamp_sources": ts_stats,
        "gemini_dated_ratio": (g_dated / g_total) if g_total else 0.0,
        "takeout": takeout_stats,
        "unmatched_gemini_prompts": unmatched[:500],
        "unmatched_truncated": len(unmatched) > 500,
        "md_total": md_total,
        "outputs": {"jsonl": str(jsonl_path), "markdown": str(out / "md"),
                    "sqlite": str(db_path)},
        "last_import": {"mode": "incremental", "at": now_iso(),
                        "parsed": len(plan), "skipped": len(skipped),
                        "added": len(added), "changed": len(changed)},
    }
    tmp = manifest_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    os.replace(tmp, manifest_path)
    runner.log("[+] %s" % manifest_path)
    summary["wrote"] = True
    return summary


def build_parser():
    ap = argparse.ArgumentParser(description="智能增量导入（合并到现有 out/，只写变化）")
    ap.add_argument("--raw", default="raw", help="原始导出目录")
    ap.add_argument("--out", default="out", help="归档输出目录")
    ap.add_argument("--work", default="work", help="工作目录（进度/日志/分析）")
    ap.add_argument("--throttle", type=float, default=0.02, help="写库节流秒数")
    ap.add_argument("--tz", default="Asia/Shanghai", help="Markdown 本地时区")
    ap.add_argument("--match-threshold", type=float, default=0.90)
    ap.add_argument("--activity-tz-offset", type=float, default=8.0)
    ap.add_argument("--force-full", action="store_true", help="忽略哈希缓存全量重解析")
    ap.add_argument("--parse-pause", type=float, default=0.0,
                    help="每个输入解析完后的散热等待秒数（防高温复位）")
    ap.add_argument("--analyze", action="store_true", help="导入后自动跑 heuristic+LLM 分析")
    ap.add_argument("--no-md", action="store_true", help="不写 Markdown")
    ap.add_argument("--no-sqlite", action="store_true", help="不写 sqlite")
    ap.add_argument("--dry-run", action="store_true", help="只解析合并并打印统计，不写盘")
    return ap


def main():
    args = build_parser().parse_args()
    os.chdir(ROOT)
    # 临时文件一律落仓库内 work/tmp（防 147MB 成员解压进 /tmp tmpfs）
    try:
        (ROOT / "work" / "tmp").mkdir(parents=True, exist_ok=True)
        tempfile.tempdir = str(ROOT / "work" / "tmp")
    except OSError:
        pass
    runner = Runner(Path(args.work))
    runner.start()
    try:
        summary = run_import(args, runner)
        if args.analyze and not args.dry_run:
            run_analysis(runner, summary.get("added_ids") or [],
                         summary.get("changed_ids") or [], args.work)
        pub = {k: v for k, v in summary.items()
               if k not in ("added_ids", "changed_ids")}
        runner.log("导入完成: %s" % json.dumps(pub, ensure_ascii=False))
        runner.finish(True, stats=pub)
    except BaseException as e:
        runner.finish(False, error=traceback.format_exc())
        if isinstance(e, KeyboardInterrupt):
            raise SystemExit(130)
        if isinstance(e, SystemExit):
            raise
        traceback.print_exc()
        raise SystemExit(1)


if __name__ == "__main__":
    main()
