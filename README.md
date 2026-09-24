# AI 聊天记录归档

把 ChatGPT、DeepSeek 和 Gemini 的历史聊天记录统一拉成本地可搜索的归档。

## 目录

```
~/ai-chat-archive/
├── raw/        放原始导出文件（你手动下载的）
├── scripts/
│   ├── archive_ai_chats.py   解析 → out/
│   ├── serve_archive.py      只读 HTTP 服务 + Web API
│   └── fetch_lexicon.sh      拉取敏感词库到 third_party/（不进仓库）
├── tools/      查询 / 选集 / 导出 / 分析 / MCP（全部只用标准库）
│   ├── arclib.py             共享数据层（CLI 与 Web 共用）
│   ├── archive_cli.py        命令行：search / stats / terms / cooccur / select / export …
│   ├── export.py             选集导出（md / html / jsonl）
│   ├── llm.py                可配置的 OpenAI 兼容 LLM 适配层（默认 DeepSeek）
│   ├── analysis.py           分析结果库 + 摘要构造 + 评分规则 + 按周聚合
│   ├── analyze.py            分析命令行：config / estimate / run / heuristic / status / timeline / tiers
│   ├── heur.py               纯本地规则分类器（不联网、不需要 key）
│   ├── sanitize.py           敏感词 + 个人信息脱敏（等长 * 替换）
│   └── mcp_server.py         MCP 接口（stdio JSON-RPC，留给自己接 LLM）
├── web/        Web 前端（原生 JS，无框架）
├── third_party/  敏感词库（git 忽略，用 scripts/fetch_lexicon.sh 拉取）
├── work/       选集状态（selection.jsonl，追加写入）
│   ├── llm.json              可选：自定义 LLM 接口配置
│   ├── reports.json          网页「智能解读」保存的报告
│   └── analysis.sqlite       LLM 分析结果（价值 / 情绪 / 类型 / 摘要）
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

浏览器打开 `http://127.0.0.1:8765`。六个主页面：**统计 / 浏览 / 搜索 / 时间线 / 分析 / 智能**，外加 **选集**。

- **浏览**：左边列表 + 右边阅读器，各自独立滚动；列表显示消息数与两行预览，支持来源/时间/排序过滤，滚到底自动加载更多。
- **搜索**：中文走 `LIKE` 全表扫描（FTS5 的 `unicode61` 分词器对中文几乎不可用），纯 ASCII 走 FTS5，结果按词频+标题加权排序；命中处高亮，点进去可以上下跳（`Enter` / `Shift+Enter`）。
- **时间线**：手写 SVG 时间序列，按天/按月，悬停出提示，点柱子下钻到当天。
- **分析**：LLM 分析结果的可视化（见下节）；没有分析数据时给提示，不影响其它页面。
- **智能**：在浏览器里配置 LLM 接口、选取素材、流式解读、保存报告（见「智能解读」一节）。
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

## LLM 分析（价值分层 / 情绪时间线）

把每个对话打分，分出**有价值 / 一般 / 噪声**，并按周聚合出**情绪曲线**。
打分方式二选一：交给 LLM（下面 1–4），或用**纯本地规则**（第 5 节，不联网不花钱）。
分析结果存在 `work/analysis.sqlite`，与原始归档完全分离——不跑分析也能正常用其它功能。

### 1. 配置（默认 DeepSeek，可换成任意 OpenAI 兼容接口）

```bash
python3 tools/analyze.py config                     # 看当前配置（不会联网）
python3 tools/analyze.py config --init              # 生成 work/llm.json 模板（DeepSeek）
python3 tools/analyze.py config --init --preset zhipu --force   # 生成智谱模板
```

默认读取环境变量 `DEEPSEEK_API_KEY`，调用 `https://api.deepseek.com/v1` 的 `deepseek-chat`。
要换成别的服务，改 `work/llm.json` 里的 `base_url` / `model` / `api_key`，
或用环境变量 `ARCHIVE_LLM_BASE_URL` / `ARCHIVE_LLM_API_KEY` / `ARCHIVE_LLM_MODEL` 覆盖。
配置优先级：命令行参数 > 环境变量 > `work/llm.json` > preset 预设 > 内置默认值。
接口文档：<https://api-docs.deepseek.com/zh-cn/>

**免费方案：智谱 GLM-4.7-Flash（推荐先拿它试跑）**

`glm-4.7-flash` 目前免费、200K 上下文，接口同样是 OpenAI 兼容的。在
<https://bigmodel.cn/usercenter/proj-mgmt/apikeys> 建一个 key 后：

```bash
export ZHIPU_API_KEY=你的key
python3 tools/analyze.py config --init --preset zhipu --force   # 写入 work/llm.json
python3 tools/analyze.py estimate                               # 应显示「预估花费 ¥0」
python3 tools/analyze.py run --limit 50                         # 试跑 50 个
```

也可以完全不建文件，直接用环境变量选预设：

```bash
ARCHIVE_LLM_PRESET=zhipu ZHIPU_API_KEY=你的key python3 tools/analyze.py run --limit 50
```

智谱文档：<https://docs.bigmodel.cn/cn/api/introduction> 。
注意 GLM-4.7 系列默认**开启思考**，本项目的 zhipu 预设已通过
`extra_payload` 里的 `{"thinking": {"type": "disabled"}}` 关掉，批量分类更快也更省 token。

### 2. 先估个价，再跑

```bash
python3 tools/analyze.py estimate --sample 20   # 抽样估算 token 与花费
python3 tools/analyze.py run --limit 200        # 只分析前 200 个未分析的对话
python3 tools/analyze.py run --dry-run          # 只构造摘要、不调接口（看效果）
```

整库（约 5500 个对话）用 `deepseek-chat` 估算约 1200 万输入 token、100 万输出 token，
折合人民币 30 元出头——所以**默认不会自动跑**，请自己按需限量执行。
这台机器供电弱，建议这样跑：

```bash
systemd-run --user --scope -p CPUQuota=40% --collect nice -n 19 \
  python3 tools/analyze.py run --limit 200
```

### 3. 看结果

```bash
python3 tools/analyze.py status          # 进度 / 平均价值 / 花费
python3 tools/analyze.py timeline -v     # 按周的情绪时间线（-v 展开六种情绪）
python3 tools/analyze.py show <对话ID>    # 单个对话的评分与摘要
python3 tools/analyze.py tiers           # 价值分层：高价值 / 一般 / 低价值
python3 tools/analyze.py tiers --dump work/tiers   # 导出成三个 ID 清单
```

评分规则（写在 `tools/analysis.py` 的 `SYSTEM_PROMPT` 里）：
`value` 0–5，`keep` = `value ≥ 3`；另外给出 `kind`（技术/学习/工作/…）、
`topics`、`sentiment`（-1~+1）、`intensity`（0~1）、六种情绪强度
（joy / calm / anxiety / anger / sadness / fatigue）和一句话摘要。
情绪时间线按**周**聚合（周一起算），每条曲线取该周的平均值。

### 5. 不想联网 / 不想花钱：纯本地规则分类

`tools/heur.py` 是一个**完全本地、不联网、不需要 key** 的启发式分类器。它用可解释的规则
（代码块、对话长度、结构化输出、附件、填充语比例、错误关键词……）给对话打 0–5 价值分、
分类型、贴标签，并用一个小词典粗略估情绪。结果写进同一个 `work/analysis.sqlite`，
所以上面的 `status` / `timeline` / `tiers` / Web 分析页全都能直接看。

```bash
python3 tools/analyze.py heuristic --all        # 全部分类，几秒钟跑完
python3 tools/analyze.py heuristic --limit 100 -v   # 只跑 100 个并逐条打印
python3 tools/analyze.py heuristic --force      # 已分析的也重跑
```

也可以用筛选参数只跑某个来源 / 时间段 / 选集（`--source` / `--from` / `--to` / `--collection`）。
标记为 `model='heuristic'`、`prompt_version='heur-v1'`，与 LLM 结果一眼可分；
每条记录的 `value_reason` 会写清楚是哪几条规则命中，方便你调 `tools/heur.py` 里的阈值。
**注意**：情绪是词典法的粗估（尤其技术类对话几乎都判成「平静」），只当参考。

### 6. Web 里看

「分析」页有：进度卡片、六种情绪的周线图、心情走势、价值走势、每周明细、价值分层三列。
浏览列表里每个对话会带上 `vN` 价值徽标，阅读器标题下方显示该对话的价值/类型/情绪/摘要。

## 智能解读（网页里直接问）

「智能」页把这套流程搬到浏览器里，不用命令行也能配置和调用 LLM：

- **接口配置**：选预设（DeepSeek / 智谱 GLM-4.7-Flash 免费）或自己填 `base_url` / `model` / `api_key_env`，
  可临时填一个 API key（保存到 `work/llm.json`，权限 600；接口只回显「是否已配置」，绝不回传 key）。
  保存即时生效，无需重启。
- **取材**：数据源可选
  - **值得分析的情感**：只挑经过分类、确实带情绪信号的对话（`kind=情绪`，或情绪强度 / 正负基调超过阈值）；
  - **高价值**（value ≥ 4）/ **当前选集** / **最近** / **全部**；
  再限个条数。取材结果会做**脱敏**后再送出去（见下）。
- **流式解读**：输入提示词（默认「总结我在这段时间里的情绪变化、反复出现的主题和压力来源」），
  点「开始解读」后逐字流式返回，可随时停止；结果可一键保存。
- **已保存的解读**：存在 `work/reports.json`（最多 60 条），可删除。

### 敏感词 / 个人信息脱敏

送进 LLM 之前，正文会先过一遍 **`tools/sanitize.py`**，把两类内容等长替换成 `*`：

- **敏感词**：词库取自开源的 [konsheng/Sensitive-lexicon](https://github.com/konsheng/Sensitive-lexicon)（MIT）。
  运行 `scripts/fetch_lexicon.sh` 拉取到 `third_party/Sensitive-lexicon/`（**不进仓库**）。
  默认只用其中的政治 / 反动 / 民生 / 贪腐 / GFW 等中文词表（可用 `work/sensitive_files.txt`
  写 `all` / `none` / 自定义文件名，或用环境变量 `ARCHIVE_SENSITIVE_FILES` / `ARCHIVE_LEXICON_DIR` 覆盖）。
- **个人信息**：手机号、座机、身份证号、邮箱、银行卡号、IPv4、常见 API key（`sk-…` / `ghp_…` / `AKIA…` 等）。
  用 `ARCHIVE_MASK_PII=none` 可以只脱敏敏感词、保留个人信息。

脱敏是**等长**替换（原字符数 = `*` 的个数），送出去的 token 量基本不变，上下文长度可控。
命令行同理：`analysis.build_digest` 在拼摘要时就会脱敏，`analyze.py run` 送出去的已经是处理过的文本。
查看当前状态（只打印词库 / 规则概况，不打印任何词）：

```bash
python3 tools/sanitize.py              # 当前词库与 PII 规则概况
python3 scripts/fetch_lexicon.sh --check
```

## MCP 接口（给以后的 LLM 工具用）

`tools/mcp_server.py` 是一个 stdio JSON-RPC 的 MCP 服务器，把归档包成 **17 个工具**：

- 检索类：`search` / `conversation` / `stats` / `selection_list` / `selection_add` / `selection_remove` / `export_text`
- 分析类：`analysis_config` / `analysis_pending` / `analysis_digest` / `analysis_save` /
  `analyze_conversation` / `analysis_get` / `analysis_status` / `emotion_timeline` / `value_tiers`
- 本地分类：`classify_conversation`（纯本地规则，不联网、不需要 key）

```bash
python3 tools/mcp_server.py --db out/archive.sqlite
```

**两种分析姿势**，看你把 LLM 放在哪一边：

- `analysis_digest`：**服务端不调 LLM**。它把某个对话压成一段摘要（首尾+等距采样，默认上限 6000 字符）
  连同评分用的 system prompt 和输出 schema 一起返回，交给 **MCP 客户端自己的 LLM** 去打分，
  再用 `analysis_save` 把 JSON 结果存回 `work/analysis.sqlite`。
- `analyze_conversation`：**服务端直接调 LLM**（用 `work/llm.json` / 环境变量里的配置），
  返回并保存分析结果。没有可用 API key 时会明确报错，不会偷偷联网。

其余分析工具只读：`analysis_pending`（还有哪些没分析）、`analysis_status`（进度/花费）、
`analysis_get`（取结果）、`emotion_timeline`（按周情绪）、`value_tiers`（价值分层）。
检索类工具都有硬上限（最多 30 条结果、片段 300 字符、单条消息 4000 字符、单次导出 20 万字符），
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
