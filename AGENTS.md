# AGENTS.md — ai-chat-archive 项目知识库

> 本文件是给 AI/开发者的接手文档：**先读这里，再动手**。README.md 讲"是什么/怎么用"，这里讲"代码在哪、机制是什么、坑在哪、正在做什么"。
> 新发现的结构/行号/机制必须及时补写进来，禁止反复重新探索。

## 项目速览

- 路径 `~/ai-chat-archive`（git 仓库，远程 https + gh 认证，自动提交推送：中文提交信息，改完验证直接 push 不问）。
- 流程：`raw/`（原始导出）→ `scripts/archive_ai_chats.py` 解析 → `out/`（normalized.jsonl、md/、archive.sqlite）→ `tools/analyze.py` 分析 → `work/analysis.sqlite`。
- 网页：`scripts/serve_archive.py` + `web/`（原生 JS：index.html / app.js / style.css），systemd 单元 `ai-archive-web.service`（限负载）。
- **只用 Python3 标准库**；**重要文件禁写 /tmp**（写仓库内路径）。
- **机器供电弱、负载高会复位**：重活必须
  `systemd-run --user --scope -p CPUQuota=40% --collect nice -n 19 <cmd>`。
- 时间线约定：旧导出退役 = 改名加 `.old` 后缀（discover 跳过非白名单扩展名）。

## raw/ 输入与导入惯例

- DeepSeek 官方导出 `deepseek_data-*.zip` 是**全量快照**（cumulative）：新导出包含旧导出全部内容，导入新版前把旧 zip 改 `.old` 退役，避免重复。
- ChatGPT zip：`f0dd…-2026-09-15-….zip`。
- Gemini：`raw/Takeout/我的活动/Gemini Apps/我的活动记录.html`（134MB Takeout，首选、native 时间）；备选 `myactivity.json`（仅提问时间）+ `gemini_chats.ndjson`（全文无时间）合并（README 有细节）。
- 当前状态（2026-10-09）：`deepseek_data-2026-10-08.zip` **新增待导入**；`10-01.zip` 待退役改 `.old`；`09-17.zip.old` 已退役。
- 上次导入（10-01，work/import.log）：chatgpt 94 对话/4090 消息，deepseek 1558/10380，gemini 3937/58963。
- out/manifest.json **陈旧**（generated_at 2026-09-17，inputs 只记到 09-17），与 10-01 实际重建不一致 → 增量方案要以 sha256 manifest 为准做引导。

## scripts/archive_ai_chats.py（1227 行，解析器）

关键函数行号（文件变了要更新）：
- `to_iso`47 `local_str`71 `norm_text`83 `sha256_file`92 `load_json_any`100
- `parse_chatgpt`149 `parse_deepseek`238（mapping+fragments：REQUEST/RESPONSE/THINK/SEARCH/FILE/TOOL_*，thinking 单独字段，时间 inserted_at）
- Gemini：`normalize_gemini_turn`358 `gemini_turns_from_obj`391 `parse_gemini_ndjson`416
- Gemini HTML 活动：`parse_activity_time`490 `html_to_text`504 `iter_activity_items`527 `parse_gemini_activity_items`550 `parse_gemini_activity_html`626 `is_activity_html`633 `parse_gemini_activity_file`691
- Takeout 回填：`load_takeout`710 `build_takeout_index`747 `match_takeout`756 `backfill_gemini`790
- 输出：`flatten`840 `slugify`862 `write_jsonl`870 `write_markdown`876 `write_sqlite`925
- 发现：`archive_member_names`978 `_sniff_conversations_bytes`996 `sniff_conversations_kind`1004 `discover`1028 `main`1061

要点：
- ChatGPT/DeepSeek 都叫 `conversations.json`，靠嗅探区分（"fragments"→deepseek，"author"→chatgpt）。
- `parse_gemini_activity_items`550：抓 `content-cell mdl-cell--6-col` div，CJK 日期正则取时间，URL 正则 `gemini.google.com/app/([0-9a-fA-F]{8,})` 取 cid，每事件产 user+assistant 两行，`gemini-unknown` 兜底 cid（会把无 URL 的并成一个对话，坑）；ndjson 兜底 id 是 `gemini-%04d` **按顺序编号、对输入顺序敏感**（唯一不稳定 id）。
- `discover`1028：递归 raw/；conversations.json→嗅探分 chatgpt/deepseek；`myactivity.json`→takeout（**目前仅时间回填，未解析正文**）；zip/tar 内按成员分派；裸 activity html→activity；.ndjson 或名含 "gemini" 的 .json→gemini。
- `write_sqlite`925：**先 unlink 再全量重建**（conversations/messages/messages_fts + 索引，executemany，每 2000 条 commit+throttle）→ 增量改造的对立面；message id AUTOINCREMENT **不稳定**，但没关系，分析按 conversation_id 关联。
- `write_markdown`876：`out/md/<source>/%Y-…_<slug>_<cid前8>.md`。
- `main`1061：argparse `--raw/--out/--chatgpt/--takeout/--takeout-html/--gemini/--deepseek/--match-threshold/--activity-tz-offset/--tz/--dry-run/--no-sqlite/--no-md/--nice/--throttle`；manifest inputs 记 `str(path):sha256`。
- 全量重建 ~5 分钟（限速 throttle 0.05），幂等覆盖 out/；**Gemini 140MB HTML 解析是大头**（增量的价值 = 跳过它）。

## scripts/serve_archive.py（1329 行，Web 服务）

- 顶部：ROOT=上级，sys.path 插 tools 后 import arclib/analysis/llm/sanitize；全局 DB_PATH/WEB_DIR/WORK_DIR/ANALYSIS_DB_PATH（main 1278 重赋值）。MAX_LIMIT=200, LLM_TIMEOUT=600, INTERPRET_MAX=40000。
- api_* 行号：stats190 conversations209 conversation255 search275 timeline297 day322 jump339 selection366/373 export391 `_analysis_conn`416 `_analysis_run`423（读 analysis_progress.json，running+90s 心跳判 alive/stale）analysis452 emotion505 topic_summary627 llm_config830/838 reports879-907 interpret933/951。
- Handler1014：`send_json`1021 `send_text`1030 `_same_origin`1044（Origin 检查，无 Origin 放行）`_read_json`1056（Content-Length JSON 体）`post_llm`1072（OpenAI 兼容转发，SSE 流式）。
- do_GET1136 → handle_api1149：/api/stats|conversations|conversation(format=md)|search|timeline|day|jump|summary|analysis|emotion|llm/config|interpret|reports|selection|export。
- do_POST1200：`/api/llm` 走 post_llm；其余 `_read_json` 后路由 selection|llm/config|reports。**当前无文件上传接口、无导入接口**（待加 /api/upload + /api/import）。
- handle_static1231：限 WEB_DIR 内，gzip（>1024），no-cache → 改前端刷新即可。
- 中文搜索用 LIKE（FTS5 unicode61 对中文无效）。

## 分析机制（tools/analyze.py + analysis.py + heur.py）

- `work/analysis.sqlite` 独立于归档库，主键 **conversation_id**（analysis.py:38，每对话一行 upsert；analysis_errors / analysis_excluded 两张辅表）。归档重建不影响已有分析结果。
- 当前状态：归档 5589 对话，已分析 5502（heur 4918 + glm-4.7-flash 584），pending 87。
- 命令：`python3 tools/analyze.py {config|estimate|run|heuristic|status|timeline|tiers|show|mark-excluded}`；全局 `--db/--work/--analysis-db/--config`。
- 过滤参数（run/estimate/heuristic 共用，analyze.py:683-691）：`--collection/--source/--from/--to/--min-msgs/--topic`。**没有 --ids/--new**（只有 mark-excluded 有 --ids）。
- 自动跳过已分析（`_select_pending`139-158，`--force` 才重跑）；候选 ORDER BY created_at **升序**，`--limit` 优先最老 → 增量补析用 **`--from <日期>`** 圈时间窗。
- **坑1**：非 --topic 模式下 heuristic 结果也算"已分析"，先跑 heuristic 再 run 不会用 LLM 升级（需 `--force` 或 `--topic`）。
- **坑2**：run 的进度文件 `work/analysis_progress.json` **只有 cmd_run 写**（20s 心跳，字段 running/finished/started/i/total/ok/fail/cost/model…）；heuristic 不写进度。网页进度卡读的就是它（serve_archive `_analysis_run`423）。
- heuristic 纯本地，全量 ~4.6 秒、0 token；run 走 work/llm.json（zhipu glm-4.7-flash 免费，key 已配，**值禁读禁打印**）。
- `selection.jsonl` 是人工选集，**分析默认不读**（只有 --collection 才用）→ 导入后自动分析不需要动它。
- 结论：导入后最小闭环 = `analyze.py heuristic --from <日>`（秒级垫底）+ `analyze.py run --from <日>`（LLM 补新对话）。

## 当前任务（进行中，完成后把状态改到这里）

用户需求（原话）：
1. 「现在有deepseek 10.08的导出，把新增的记录导进去，然后照惯例，分析心理内容。」
2. 「我希望这个脚本可以智能一点，然后能够在网页端上传然后完成分析，把新增的内容搞进去，另外，很快gemini的json也要导入进来，所以写好功能，旧版本是html的，智能的点在于，能够把新的东西放进来。」

拆解：
- [ ] A. 智能增量导入：新模块 `scripts/incremental.py`（拟）——manifest sha256 跳过未变输入（省掉 Gemini 140MB 解析）；DB 里已有对话做基座，新解析的按 (source, conversation_id) 合并（指纹相同跳过、变了替换、DB 独有保留）；多份 deepseek 并存时按 cid 去重取消息多的；只重写变更对话的 md，jsonl 整体重写，sqlite 重建（幂等，analysis 靠 cid 不受影响）。
- [ ] B. 网页上传闭环：serve_archive 加 `POST /api/upload`（字节体存 raw/，文件名消毒）+ `POST /api/import`（后台线程跑增量导入→自动 heuristic→LLM run）+ `GET /api/import`（进度，可复用 analysis_progress + import 日志）；web 前端加上传区+进度显示。
- [ ] C. Gemini JSON：写 `parse_gemini_activity_json`（兼容 Takeout My Activity JSON 结构，抽 prompt/时间/cid，产出与 HTML 版同构 convs）；discover 把 myactivity.json 从"仅回填"升级为可解析正文，与 HTML 共存。
- [ ] D. 导入 10-08 deepseek（10-01 改 .old）→ 跑分析 → status 验证 → commit+push。

## 环境与命令备忘

- 限速跑：`systemd-run --user --scope -p CPUQuota=40% --collect nice -n 19 python3 scripts/archive_ai_chats.py --raw raw --out out --throttle 0.05`
- 本机测试服务：`python3 scripts/serve_archive.py -v`（端口参数 `--port`；DB 缺失会提示先跑归档）。
- 网页 systemd：`ai-archive-web.service`（改 serve_archive.py 后才需 restart）。
- 自动提交推送：验证过就 `git add <相关文件> && git commit -m <中文> && git push`，不问；不提交密钥。
- web 静态无缓存：改前端只刷新；带 `?v=` 的资源需硬刷的坑是 psy-scales 的，这里没有。
