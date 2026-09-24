#!/usr/bin/env python3
"""敏感词脱敏：把命中词替换成等长的 *，纯标准库，不联网。

词库来自开源项目 konsheng/Sensitive-lexicon（MIT），本仓库用脚本拉取到
    third_party/Sensitive-lexicon/Vocabulary/*.txt
一个词一行；空行、# / // 开头的注释行会被忽略。

默认只用「政治 / 风险」相关的几本词库（见 DEFAULT_FILES）。想换词库：
    环境变量 ARCHIVE_SENSITIVE_FILES="all"          用 Vocabulary 下全部 .txt
    环境变量 ARCHIVE_SENSITIVE_FILES="none"         关闭脱敏
    环境变量 ARCHIVE_SENSITIVE_FILES="a.txt,b.txt"  指定文件（相对词库根目录）
也可以写 work/sensitive_files.txt（每行一个文件名），优先级低于环境变量。
词库根目录可用 ARCHIVE_LEXICON_DIR 指定。

匹配用 Aho-Corasick 自动机，长词优先、不重叠，命中处替换成等长 '＊'。

同时做「个人信息（PII）」脱敏：手机号 / 座机 / 身份证 / 邮箱 / 银行卡 /
IPv4 / 常见 API key，命中处同样替换成等长 '*'。可用环境变量
ARCHIVE_MASK_PII=none 关闭 PII 脱敏（默认开启）。

本模块只做「读词库 + 正则替换」，永远不会把命中的内容打印出来。
"""
from __future__ import annotations

import os
import re
from collections import deque
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 词库根目录候选（按顺序找第一个存在的）
DEFAULT_DIRS = [
    ROOT / "third_party" / "Sensitive-lexicon",
    ROOT / "third_party" / "敏感词库",
]
VOCAB_SUBDIR = "Vocabulary"

# 默认启用的词库文件（相对词库根目录）。偏「政治 / 高风险」。
DEFAULT_FILES = [
    "Vocabulary/政治类型.txt",
    "Vocabulary/反动词库.txt",
    "Vocabulary/新思想启蒙.txt",
    "Vocabulary/民生词库.txt",
    "Vocabulary/GFW补充词库.txt",
    "Vocabulary/贪腐词库.txt",
    "Vocabulary/补充词库.txt",
]

CONFIG_FILE = ROOT / "work" / "sensitive_files.txt"

# 太短的词容易误伤（尤其纯 ASCII），按长度过滤
MIN_LEN = 2
MIN_LEN_ASCII = 3
_ASCII_RE = re.compile(r"^[\x00-\x7f]+$")

# 个人信息（PII）正则：命中处替换成等长 '*'
PII_PATTERNS = [
    ("email", re.compile(
        r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    ("idcard", re.compile(
        r"(?<![0-9A-Za-z])[1-9]\d{5}(?:19|20)\d{2}"
        r"(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![0-9A-Za-z])")),
    ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("landline", re.compile(r"(?<!\d)0\d{2,3}[-\s]?\d{7,8}(?!\d)")),
    ("bankcard", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("ipv4", re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")),
    ("secret", re.compile(
        r"\b(?:sk-[A-Za-z0-9_\-]{10,}|ghp_[A-Za-z0-9]{20,}"
        r"|github_pat_[A-Za-z0-9_]{20,}|AKIA[0-9A-Z]{12,}"
        r"|xox[baprs]-[A-Za-z0-9\-]{10,}|AIza[0-9A-Za-z_\-]{20,})\b")),
]


# --------------------------------------------------------------------------
# 词库装载
# --------------------------------------------------------------------------

def lexicon_dir():
    """返回词库根目录，找不到返回 None。"""
    env = (os.environ.get("ARCHIVE_LEXICON_DIR") or "").strip()
    if env:
        p = Path(env)
        return p if p.is_dir() else None
    for d in DEFAULT_DIRS:
        if d.is_dir():
            return d
    return None


def _configured_files():
    """返回要加载的文件列表（相对词库根目录），或特殊标记 all/none。"""
    raw = (os.environ.get("ARCHIVE_SENSITIVE_FILES") or "").strip()
    if not raw and CONFIG_FILE.is_file():
        try:
            raw = CONFIG_FILE.read_text(encoding="utf-8")
        except OSError:
            raw = ""
    items = [x.strip() for x in raw.replace(",", "\n").splitlines() if x.strip()
             and not x.strip().startswith("#")]
    if not items:
        return list(DEFAULT_FILES)
    if len(items) == 1 and items[0].lower() in ("all", "全部", "*"):
        return "all"
    if len(items) == 1 and items[0].lower() in ("none", "off", "0", "无"):
        return "none"
    return items


def _resolve_files(root):
    wanted = _configured_files()
    if wanted == "none":
        return []
    if wanted == "all":
        base = root / VOCAB_SUBDIR
        if not base.is_dir():
            base = root
        return sorted(base.glob("*.txt"))
    out = []
    for name in wanted:
        p = root / name
        if p.is_file():
            out.append(p)
    return out


def _accept(word):
    if not word:
        return False
    n = len(word)
    if n < MIN_LEN:
        return False
    if _ASCII_RE.match(word) and n < MIN_LEN_ASCII:
        return False
    return True


def load_words(paths=None):
    """读取词库，返回去重后的词集合（绝不打印词条）。"""
    if paths is None:
        root = lexicon_dir()
        if root is None:
            return set()
        paths = _resolve_files(root)
    words = set()
    for p in paths:
        try:
            text = Path(p).read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            w = line.strip().lstrip("\ufeff")
            if not w or w.startswith("#") or w.startswith("//"):
                continue
            if "\t" in w:
                w = w.split("\t", 1)[0].strip()
            if _accept(w):
                words.add(w)
    return words


# --------------------------------------------------------------------------
# Aho-Corasick
# --------------------------------------------------------------------------

class _Automaton:
    __slots__ = ("goto", "fail", "out")

    def __init__(self):
        self.goto = [{}]
        self.fail = [0]
        self.out = [None]

    def add(self, word):
        node = 0
        for ch in word:
            nxt = self.goto[node].get(ch)
            if nxt is None:
                nxt = len(self.goto)
                self.goto[node][ch] = nxt
                self.goto.append({})
                self.fail.append(0)
                self.out.append(None)
            node = nxt
        self.out[node] = word

    def build(self):
        q = deque()
        for ch, n in self.goto[0].items():
            self.fail[n] = 0
            q.append(n)
        while q:
            r = q.popleft()
            for ch, n in self.goto[r].items():
                f = self.fail[r]
                while f and ch not in self.goto[f]:
                    f = self.fail[f]
                self.fail[n] = self.goto[f].get(ch, 0)
                if self.fail[n] == n:
                    self.fail[n] = 0
                if self.out[n] is None:
                    self.out[n] = self.out[self.fail[n]]
                q.append(n)

    def mask(self, text, char="*"):
        if not self.goto or not text:
            return text
        goto, fail, out = self.goto, self.fail, self.out
        res = []
        last = 0
        node = 0
        for i, ch in enumerate(text):
            while node and ch not in goto[node]:
                node = fail[node]
            node = goto[node].get(ch, 0)
            w = out[node]
            if w is not None:
                start = i - len(w) + 1
                if start >= last:
                    res.append(text[last:start])
                    res.append(char * len(w))
                    last = i + 1
        res.append(text[last:])
        return "".join(res)


_CACHE = {"key": None, "auto": None, "files": (), "count": 0}


def _signature(root, paths, extra):
    parts = [str(extra)]
    for p in paths:
        try:
            st = Path(p).stat()
            parts.append("%s:%d:%d" % (p, st.st_mtime_ns, st.st_size))
        except OSError:
            parts.append(str(p))
    if root is None:
        parts.append("no-root")
    return "|".join(parts)


def _matcher():
    root = lexicon_dir()
    paths = []
    if root is not None:
        paths = _resolve_files(root)
    if not paths:
        paths = []
    key = _signature(root, paths, os.environ.get("ARCHIVE_SENSITIVE_FILES") or "")
    if _CACHE["auto"] is not None and _CACHE["key"] == key:
        return _CACHE["auto"], _CACHE["files"], _CACHE["count"]
    words = load_words(paths)
    auto = _Automaton()
    for w in words:
        auto.add(w)
    auto.build()
    names = tuple(Path(p).name for p in paths)
    _CACHE.update({"key": key, "auto": auto, "files": names, "count": len(words)})
    return auto, names, len(words)


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def enabled():
    raw = (os.environ.get("ARCHIVE_SENSITIVE_FILES") or "").strip()
    if raw.lower() in ("none", "off", "0", "无"):
        return False
    return bool(_matcher()[2])


def pii_enabled():
    """个人信息脱敏是否开启（默认开）。"""
    raw = (os.environ.get("ARCHIVE_MASK_PII") or "").strip().lower()
    return raw not in ("none", "off", "0", "无")


def active():
    """mask() 是否会产生任何替换。"""
    return pii_enabled() or enabled()


def mask_pii(text, char="*"):
    """把手机号/身份证/邮箱等个人信息替换成等长的 char。"""
    if not text or not pii_enabled():
        return text
    for _name, rx in PII_PATTERNS:
        text = rx.sub(lambda m: char * len(m.group(0)), text)
    return text


def mask(text, char="*"):
    """先做敏感词替换，再做个人信息脱敏；都没有时原样返回。"""
    if not text:
        return text
    auto, _files, count = _matcher()
    if count:
        text = auto.mask(text, char)
    return mask_pii(text, char)


def mask_fields(obj, keys):
    """就地脱敏 obj 里指定的若干字符串字段，返回 obj。"""
    if not isinstance(obj, dict):
        return obj
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str):
            obj[k] = mask(v)
    return obj


def status():
    """给人看的状态（只含数量与文件名，绝不含词条）。"""
    root = lexicon_dir()
    auto, names, count = _matcher()
    return {
        "enabled": enabled(),
        "pii_enabled": pii_enabled(),
        "pii_patterns": [name for name, _rx in PII_PATTERNS],
        "lexicon_dir": str(root) if root else None,
        "files": list(names),
        "word_count": count,
        "config_file": str(CONFIG_FILE),
        "note": "词库来自 konsheng/Sensitive-lexicon（MIT），用脚本 scripts/fetch_lexicon.sh 拉取",
    }


if __name__ == "__main__":
    import json
    print(json.dumps(status(), ensure_ascii=False, indent=2))
