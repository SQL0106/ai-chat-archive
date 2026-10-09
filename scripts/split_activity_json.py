#!/usr/bin/env python3
"""把大体积 Takeout「我的活动记录.json」按顶层数组切成多个小片。

每个切片写成独立目录，保证 basename 仍是「我的活动记录.json」，
这样 scripts/archive_ai_chats.py 的 discover/is_activity_json 能直接识别，
避免一次性解析 140MB+ JSON 造成长时间满载（本机易高温复位）。

用法：
  python3 scripts/split_activity_json.py <input.json> <outdir> [--chunk-mb 24]
"""
import argparse
import json
import sys
from pathlib import Path


def iter_top_items(text):
    """按位置切出顶层 [...] 数组的每个元素（不重建对象，零拷贝切片）。"""
    n = len(text)
    i = text.find("[")
    if i < 0:
        raise SystemExit("输入不是 JSON 数组")
    i += 1
    dec = json.JSONDecoder()
    while i < n:
        while i < n and text[i] in " \t\r\n,":
            i += 1
        if i >= n or text[i] == "]":
            break
        _obj, end = dec.raw_decode(text, i)
        yield i, end
        i = end


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input")
    ap.add_argument("outdir")
    ap.add_argument("--chunk-mb", type=float, default=24.0)
    ap.add_argument("--name", default="我的活动记录.json")
    args = ap.parse_args()

    src = Path(args.input)
    outdir = Path(args.outdir)
    limit = int(args.chunk_mb * 1024 * 1024)
    text = src.read_text("utf-8")
    print("读入 %s (%.1f MB)" % (src, len(text.encode("utf-8")) / 1048576), flush=True)

    parts = []
    buf, size, first = [], 0, True
    for start, end in iter_top_items(text):
        piece = text[start:end]
        if not first:
            piece = "," + piece
            size += 1
        buf.append(piece)
        size += len(piece)
        first = False
        if size >= limit:
            parts.append("".join(buf))
            buf, size, first = [], 0, True
    if buf:
        parts.append("".join(buf))

    for idx, body in enumerate(parts, 1):
        d = outdir / ("part%d" % idx) / "Gemini Apps"
        d.mkdir(parents=True, exist_ok=True)
        f = d / args.name
        f.write_text("[" + body + "]", encoding="utf-8")
        print("写出 %s (%.1f MB)" % (f, f.stat().st_size / 1048576), flush=True)
    print("共 %d 片" % len(parts), flush=True)


if __name__ == "__main__":
    sys.exit(main())
