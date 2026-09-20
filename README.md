# AI 聊天记录归档

把 ChatGPT、DeepSeek 和 Gemini 的历史聊天记录统一拉成本地可搜索的归档。

## 目录

```
~/ai-chat-archive/
├── raw/        放原始导出文件（你手动下载的）
├── scripts/
│   ├── archive_ai_chats.py   解析 → out/
│   └── serve_archive.py      只读 HTTP 服务 + Web API
├── tools/      查询 / 选集 / 导出 / MCP（全部只用标准库）
│   ├── arclib.py             共享数据层（CLI 与 Web 共用）
│   ├── archive_cli.py        命令行：search / stats / terms / cooccur / select / export …
│   ├── export.py             选集导出（md / html / jsonl）
│   └── mcp_server.py         MCP 接口（stdio JSON-RPC，留给自己接 LLM）
├── web/        Web 前端（原生 JS，无框架）
├── work/       选集状态（selection.jsonl，追加写入）
└── out/        脚本生成的结果
    ├── normalized.jsonl    统一格式，逐条消息（机器用，UTC 时间）
    ├── md/chatgpt/*.md     人类可读（本地时间）
    ├── md/deepseek/*.md
    ├── md/gemini/*.md
    ├── archive.sqlite      含 FTS5 全文索引
    └── manifest.json       统计 / 回填率 / 未匹配清单 / 输入校验和
```

## 第一步：准备原始文件（放进 raw/）

**ChatGPT** — 官方导出
1. 打开 https://chatgpt.com/#settings/DataControls
2. 点 **Export data**，等邮箱收到 ZIP
3. 解压，把 `conversations.json` 放进 `raw/`（整个 ZIP 直接放 `raw/` 也行）

**DeepSeek** — 官方导出

1. 打开 https://chat.deepseek.com → 设置 → **导出数据**，等邮箱收到 `deepseek_data-*.zip`
2. 直接把 ZIP 丢进 `raw/`（或解压后放 `conversations.json`）

DeepSeek 的 `mapping` 里每个节点是 `{model, inserted_at, fragments[]}`，fragment 类型有
`REQUEST`（用户）、`RESPONSE`（回答）、`THINK`（思维链）、`SEARCH` / `FILE` / `TOOL_*`（附件与工具调用）。
脚本把它们合并成一条消息，思维链存进 `thinking` 字段，附件只记名称/数量。时间来自 `inserted_at`，`timestamp_source=native`。

> ChatGPT 和 DeepSeek 的导出 ZIP 里都有 `conversations.json`，脚本靠内容嗅探
> （`"author"` → ChatGPT，`"fragments"` → DeepSeek）自动区分，不用手动指定。

**Gemini** — 首选 Takeout 的 HTML，一步到位

| 文件 | 来源 | 提供什么 |
|------|------|----------|
| `我的活动记录.html` | https://takeout.google.com → 只勾 `My Activity` → 格式选 **HTML** | 完整正文 + 时间戳 + 对话 ID |

导出后解压，把 `Takeout/我的活动/Gemini Apps/我的活动记录.html` 放进 `raw/`（或者把整个 `.tgz`/`.zip` 直接丢进 `raw/`，脚本会自动解压读取）。
脚本会流式解析这个 140MB 的 HTML，提取每轮问答的完整正文和原生时间，`timestamp_source=native`。

> 实测这个 HTML 里 prompt 和回答都全，且带时间，所以**不需要**再跑油猴脚本。

**备选方案（仅当 HTML 里没有回答时）**

| 文件 | 来源 | 提供什么 |
|------|------|----------|
| `MyActivity.json` | Takeout → `My Activity` → 格式选 **JSON** | 时间戳（只有提问） |
| `gemini_chats.ndjson` | Tampermonkey 脚本 `davidmalko87/gemini-chat-exporter` 的 "GCE: Export all" | 完整正文（无时间） |

两者合并：正文来自 ndjson，时间用 Takeout 回填（`timestamp_source=takeout`）。

> 注意：Takeout 里那个独立的 **Gemini** 产品导的是 Gems，不是聊天记录，别勾。

## 第二步：运行

```bash
cd ~/ai-chat-archive
python3 scripts/archive_ai_chats.py --raw raw --out out
```

先看统计不写文件：

```bash
python3 scripts/archive_ai_chats.py --raw raw --out out --dry-run
```

常用参数：

| 参数 | 说明 |
|------|------|
| `--dry-run` | 只打印统计，不写文件 |
| `--no-sqlite` | 不生成 SQLite |
| `--no-md` | 不生成 Markdown |
| `--match-threshold 0.90` | Takeout 模糊匹配阈值（0–1） |
| `--tz Asia/Shanghai` | Markdown 里显示的本地时区 |
| `--takeout-html FILE` | 手动指定活动记录 HTML 或 Takeout 压缩包（可重复） |
| `--chatgpt FILE` | 手动指定 ChatGPT 的 `conversations.json` / 导出 ZIP |
| `--deepseek FILE` | 手动指定 DeepSeek 的 `conversations.json` / 导出 ZIP |
| `--gemini FILE` | 手动指定 `gemini_chats.ndjson` |
| `--activity-tz-offset 8.0` | HTML 里时区缩写无法识别时的兜底偏移 |
| `--nice 19` | 降低进程优先级 |
| `--throttle 0.05` | 每批写入后暂停秒数（降负载） |

重复运行会覆盖 `out/`，是幂等的。

### 低功耗运行（这台机器供电弱，会因负载过高复位）

全量解析 + 写 5479 个 Markdown + 71572 行 SQLite 有 CPU/IO 压力，建议限速跑：

```bash
systemd-run --user --scope -p CPUQuota=40% --collect \
  nice -n 19 python3 scripts/archive_ai_chats.py --raw raw --out out --throttle 0.05
```

实测约 5 分钟、平均 15% CPU。也可以拆开跑：`--no-md` 先出 SQLite/JSONL，再单独补 Markdown。

## 时间戳是怎么来的

| `timestamp_source` | 含义 |
|--------------------|------|
| `native` | 导出文件自带（ChatGPT / DeepSeek / Gemini 活动 HTML 全部） |
| `takeout` | 从 Takeout 精确/模糊匹配到的提问时间 |
| `takeout-inferred` | Gemini 的回答没有独立时间，沿用上一条提问的时间（近似） |
| `none` | 没匹配到，`timestamp` 为空 |

走活动 HTML 时全部是 `native`；只有在用 ndjson + Takeout 的备选方案时才会出现 `takeout` / `takeout-inferred`。匹配不上的提问会列在 `manifest.json` 的 `unmatched_gemini_prompts` 里，可以人工核对。

## Web 查看器

```bash
python3 scripts/serve_archive.py --db out/archive.sqlite --web web --port 8765
```

浏览器打开 `http://127.0.0.1:8765`。四个主页面：**统计 / 浏览 / 搜索 / 时间线**，外加 **选集**。

- **浏览**：左边列表 + 右边阅读器，各自独立滚动；列表显示消息数与两行预览，支持来源/时间/排序过滤，滚到底自动加载更多。
- **搜索**：中文走 `LIKE` 全表扫描（FTS5 的 `unicode61` 分词器对中文几乎不可用），纯 ASCII 走 FTS5，结果按词频+标题加权排序；命中处高亮，点进去可以上下跳（`Enter` / `Shift+Enter`）。
- **时间线**：手写 SVG 时间序列，按天/按月，悬停出提示，点柱子下钻到当天。
- 阅读器顶部有 **☆ 加入选集** 和 **下载**（Markdown）。URL 可直接分享：`#c=<对话ID>&i=<消息序号>&q=<搜索词>`。

静态文件是每次从磁盘读、带 `Cache-Control: no-cache`，所以改前端只要刷新浏览器，不用重启服务。

### 作为 systemd 服务常驻

```bash
sudo cp /tmp/ai-archive-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ai-archive-web.service
```

服务单元里已经限了负载（`CPUQuota=50%`、`Nice=19`、`IOSchedulingClass=idle`），
因为这台机器供电弱、负载一高就复位。开机自启，日志用 `journalctl -u ai-archive-web.service -f`。

## 命令行工具

`tools/archive_cli.py` 默认只打印**片段和统计**，不会把正文灌进终端；要看全文得显式加 `--full`。
（这一点是刻意的：整库正文约 4800 万字符，约 3000 万 token，别一不小心全读进来。）

```bash
cd ~/ai-chat-archive
python3 tools/archive_cli.py stats
python3 tools/archive_cli.py search "传送带" --limit 10
python3 tools/archive_cli.py search "装饰器" --source deepseek --link
python3 tools/archive_cli.py terms --top 40 --min-count 20        # 高频词（含中文 2-gram）
python3 tools/archive_cli.py cooccur 学习 编程 --samples 5         # 同时出现的消息/对话
python3 tools/archive_cli.py length --order chars --limit 20      # 最长的对话
python3 tools/archive_cli.py show <对话ID>                         # 摘要；--full 才出全文
```

| 子命令 | 作用 |
|--------|------|
| `search` | 混合检索（中文 LIKE / ASCII FTS5），`--json` / `--link` / `--source` / `--role` / `--from` / `--to` |
| `stats` | 总量、来源、时间戳来源 |
| `terms` | 词频统计，`--ngram`（默认 2）、`--top`、`--min-count`、`--with-thinking` |
| `cooccur A B` | 两个词共现的消息数/对话数 + 样例片段 |
| `length` | 按字符数/消息数/更新时间排的对话长度榜 |
| `show` | 单个对话的目录；`--full` 打印正文 |
| `select` | `add` / `rm` / `ls` 选集 |
| `export` | 把选集导出成 md / html / jsonl |

## 选集与导出

选集是**追加写入**的 `work/selection.jsonl`，一行一个操作：

```json
{"ts": "2026-09-17T15:02:41+00:00", "conversation_id": "…", "action": "add", "collection": "default"}
```

`action` 是 `add` / `remove`，回放整个文件就得到当前选集（所以断电写坏最后一行也不会丢数据）。
对话 ID 来自导出文件本身，重建 `out/` 也不会变，所以选集跨重建依然有效。

```bash
python3 tools/archive_cli.py select add <对话ID> --collection 读书笔记
python3 tools/archive_cli.py select ls --collection 读书笔记
python3 tools/archive_cli.py export --collection 读书笔记 --format md -o 读书笔记.md
python3 tools/archive_cli.py export --collection 读书笔记 --format html --with-thinking -o 读书笔记.html
```

Web 端也能加/删选集（列表和阅读器里的 ☆），导出按钮直接下载。
导出复用 `out/md/` 里已经写好的 Markdown（找不到时才从 SQLite 现场渲染），思维链默认省略，`--with-thinking` 才带上。

## MCP 接口（给以后的 LLM 工具用）

`tools/mcp_server.py` 是一个 stdio JSON-RPC 的 MCP 服务器，把归档包成 7 个工具：
`search` / `conversation` / `stats` / `selection_list` / `selection_add` / `selection_remove` / `export_text`。

```bash
python3 tools/mcp_server.py --db out/archive.sqlite
```

它**不调用任何 LLM**，只是把数据层暴露出去，方便以后接自己的分析工具。
所有工具都有硬上限（最多 30 条结果、片段 300 字符、单条消息 4000 字符、单次导出 20 万字符），
就是为了防止把整库灌进上下文。

## 查询归档（直接写 SQL）

```bash
python3 -c "import sqlite3; ..."   # 这台机器没装 sqlite3 命令行
```

`out/archive.sqlite` 里有 `conversations` / `messages` / `messages_fts` 三张表。
注意 `messages_fts` 用的是 `unicode61` 分词器，**中文会被当成一个整词**，所以中文别用 `MATCH`：

```sql
-- 中文：用 LIKE（全表 0.3 秒左右，可接受）
SELECT timestamp, role, substr(text,1,80) FROM messages
WHERE text LIKE '%装饰器%' ORDER BY timestamp;

-- 英文/代码：FTS5 正常
SELECT timestamp, role, substr(text,1,80) FROM messages
WHERE id IN (SELECT rowid FROM messages_fts WHERE messages_fts MATCH 'decorator')
ORDER BY timestamp;
```

`tools/arclib.py` 里的 `search()` 已经按这个规则自动分流，直接用它比手写 SQL 省事。

## 已知限制

- ChatGPT 官方导出不含已删除的对话，图片/附件多数只有引用不是文件本体
- DeepSeek 的思维链（`THINK`）单独存进 `thinking`，极少数被中断的回答会成为独立的 assistant 消息
- DeepSeek 导出的 `SEARCH` / `FILE` / `TOOL_*` 只记录名称与数量，附件本体未导入
- Gemini 活动 HTML 里只记录文字和附件文件名，图片本体在 Takeout 压缩包的 `Gemini Apps/` 目录里，未导入归档
- Gemini 的 Canvas / Gems 内容不在活动记录里
- 活动 HTML 里少数条目（Created/Selected/Cleared 等 UI 事件）不是问答，已跳过；因此对话数会比唯一 ID 数略少
- 脚本只用 Python 3 标准库，不需要 pip 安装任何东西
- `messages_fts` 的 `unicode61` 分词器对中文基本无效（一个词命中率只有 3%–9%），
  所以中文检索走 `LIKE` 全表扫描（约 0.3 秒），代价是没有 BM25 排序、只能按词频+标题加权。
  换成 `trigram` 分词器要重建几百 MB 索引，这台机器空间和供电都不划算，暂时不动。
