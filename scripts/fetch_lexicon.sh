#!/usr/bin/env bash
# 拉取 / 更新敏感词库（konsheng/Sensitive-lexicon，MIT）。
#
# 词库体积较大，且属于第三方数据，不随本仓库提交（见 .gitignore 的 third_party/）。
# 本脚本只克隆/更新到 third_party/Sensitive-lexicon，并打印文件数与行数，
# 不会输出任何词条内容。
#
# 用法：
#   scripts/fetch_lexicon.sh            # 克隆或更新
#   scripts/fetch_lexicon.sh --check    # 只报告状态，不联网
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/third_party/Sensitive-lexicon"
REPO="${ARCHIVE_LEXICON_REPO:-https://github.com/konsheng/Sensitive-lexicon.git}"

if [[ "${1:-}" == "--check" ]]; then
  if [[ -d "$DEST/.git" ]]; then
    echo "词库已存在: $DEST"
    echo "Vocabulary 文件数: $(find "$DEST/Vocabulary" -maxdepth 1 -name '*.txt' 2>/dev/null | wc -l)"
  else
    echo "词库尚未拉取。运行 scripts/fetch_lexicon.sh"
  fi
  exit 0
fi

mkdir -p "$ROOT/third_party"
if [[ -d "$DEST/.git" ]]; then
  echo "更新词库 ..."
  git -C "$DEST" fetch --depth 1 origin
  git -C "$DEST" reset --hard origin/HEAD >/dev/null
else
  echo "克隆词库 ..."
  git clone --depth 1 "$REPO" "$DEST"
fi

echo "完成: $DEST"
echo "Vocabulary 文件数: $(find "$DEST/Vocabulary" -maxdepth 1 -name '*.txt' | wc -l)"
echo "（词条内容不在此显示；tools/sanitize.py status 可查看字数统计）"
