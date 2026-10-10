# AGENTS.md — ai-chat-archive 项目知识库

> 本文件是给 AI/开发者的接手文档：**先读这里，再动手**。README.md 讲"是什么/怎么用"，这里讲"代码在哪、机制是什么、坑在哪、正在做什么"。
> 新发现的结构/行号/机制必须及时补写进来，禁止反复重新探索。（行号基于 2026-10-10 实测，改动后要更新。）

## 项目速览

- 路径 `~/ai-chat-archive`（git 仓库，远程 https://github.com/SQL0106/ai-chat-archive.git，gh 认证；自动提交推送：中文提交信息，改完验证直接 push 不问）。
- 流程：`raw/`（原始导出）→ `scripts/archive_ai_chats.py` 或 `scripts/incremental.py` 解析 → `out/`（normalized.jsonl、md/、archive.sqlite）→ `tools/analyze.py` 分析 → `work/analysis.sqlite`。
- 网页：`scripts/serve_archive.py`（:8765 常驻，端口 `--port`）+ `web/`（原生 JS），systemd 单元 `ai-archive-web.service`。
- **只用 Python3 标准库**；**重要文件禁写 /tmp**（勘察产物放仓库 `.agent/`，临时文件脚本已重定向到 `work/tmp`）。
- **oneplus8（本机）供电弱、负载高会硬件复位**（断电曾把源码/库写坏）：重活一律放 **fedora** 跑（见「fedora 工作流」）；本机必须跑时用
  `systemd-run --user --collect --unit=<名> -p CPUQuota=10% nice -n 19 python3 <绝对路径>`（scope 模式不吃 `-p Nice=19`，用 `nice -n 19`）。**编辑完立即 commit**（断电会吃掉未提交编辑，曾发生 .git 对象损坏）。
- 时间线约定：旧导出退役 = 改名加 `.old` 后缀（discover 跳过非白名单扩展名）。

## raw/ 输入与导入惯例

- DeepSeek 官方导出 `deepseek_data-*.zip` 是**全量快照**（cumulative）：新版含旧版全部内容，导入新版前把旧 zip 改 `.old`。
- 当前 raw/：`deepseek_data-2026-09-17.zip.old`、`deepseek_data-2026-10-01.zip.old`、`deepseek_data-2026-10-08.zip`（最新）、ChatGPT `f0dd…-2026-09-15-….zip`、`Takeout/`（旧 140MB 活动 HTML）、`takeout-20261008T155525Z-1-001.tgz.old`（424MB 新 Takeout，已改 .old；其 147MB Gemini 活动 JSON 已抽出为独立文件参与导入）。
- **Gemini 活动两种格式**（信息等同，都解析）：旧 = Takeout HTML `我的活动记录.html`（140MB）；新 = Takeout JSON `我的活动记录.json`（147MB，条目 `{header:"Gemini Apps", title:"Prompted <提问>", time:ISO, details[].url 含 gemini.google.com/app/<hex cid>, safeHtmlItem[].html 回答}`）。**AI Mode 同名 JSON 必须排除**（`is_activity_json` 靠路径含 "ai mode" 过滤）。
- Gemini 备选：`myactivity.json`（仅提问时间回填）+ `gemini_chats.ndjson`（油猴导出，全文无时间）——与 HTML/JSON 共存互备。
- 上次导入日志（work/import.log）：三输入解析 5613 对话 → 合并 **新增 44、变更 11 → 5745 对话 / 75634 消息**（=75286+348）。
- manifest（out/manifest.json）：inputs 记 `str(path):sha256`，是增量跳过解析的依据。

## scripts/archive_ai_chats.py（1350 行，全量解析器）

关键函数行号：
- `to_iso`47 `local_str`71 `norm_text`83 `sha256_file`92 `load_json_any`100 `extract_parts`119
- `parse_chatgpt`149 `parse_deepseek`238（mapping+fragments：REQUEST/RESPONSE/THINK/SEARCH/FILE/TOOL_*，thinking 单独字段，时间 inserted_at）
- Gemini turn：`normalize_gemini_turn`358 `gemini_turns_from_obj`391 `parse_gemini_ndjson`416
- Gemini HTML：`parse_activity_time`490 `html_to_text`504 `extract_html_attachments`518 `iter_activity_items`527 `parse_gemini_activity_items`550 `parse_gemini_activity_html`626 `is_activity_html`633
- **Gemini JSON（新）**：`ACTIVITY_JSON_NAMES`641 `is_activity_json`644 `parse_gemini_activity_json`652；`extract_members_to_temp`748 `parse_gemini_activity_file`798（.json/.html 直读，压缩包按成员 predicate 分派）
- Takeout 回填：`load_takeout`824 `build_takeout_index`861 `match_takeout`870 `backfill_gemini`904
- 输出：`flatten`954 `slugify`976 `write_jsonl`984 `write_markdown`990 `write_sqlite`1039
- 发现：`archive_member_names`1096 `sniff_conversations_kind`1122 `discover`1146 `main`1184

要点：
- ChatGPT/DeepSeek 都叫 `conversations.json`，靠嗅探区分（"fragments"→deepseek，"author"→chatgpt）。
- `parse_gemini_activity_json`：json.load(utf-8-sig，list 或 {items:[]}) → 滤 header 含 gemini、title 匹配 `^(Prompted|Branched)\s` → 抽 prompt/time/cid（details[].url 正则，无则 gemini-unknown）/safeHtmlItem 回答过 html_to_text → 按 cid 分组产 user+assistant 行（同 ts、ts_source=native）；无 assistant 行的 conv 标 `_prompt_only=True`。
- `write_sqlite`1039 **原子写（防断电）**：写 `path+".tmp"` 建全量三表（conversations/messages/messages_fts+索引）→ close → `os.replace(tmp, path)`；**不再先 unlink 正式库**。message id AUTOINCREMENT 不稳定，分析按 conversation_id 关联、不受影响。
- `write_markdown`990：`out/md/<source>/%Y-…_<slug>_<cid前8>.md`；frontmatter 含 `conversation_id:` 行（可建 cid→文件索引）。
- `discover`1146：递归 raw/；conversations.json→嗅探分 chatgpt/deepseek；`myactivity.json`→takeout（仅回填）；zip/tar 内按成员分派（activity html / is_activity_json / .ndjson / conversations.json）；**裸 .json 且 is_activity_json(完整路径)→activity**（判定先于 gemini 分支）；裸 activity html→activity；.ndjson 或名含 "gemini" 的 .json→gemini。
- `main`1184：全量重建参数 `--raw/--out/--chatgpt/--takeout/--takeout-html/--gemini/--deepseek/--match-threshold/--activity-tz-offset/--tz/--dry-run/--no-sqlite/--no-md/--nice/--throttle`。**日常导入不要跑它**（全量重建 5 分钟+），跑 incremental.py。

## scripts/incremental.py（663 行，智能增量导入——日常入口）

关键函数行号：
- `Runner`40：进度写 `work/import_progress.json`（原子+20s 心跳线程，字段 running/finished/phase/i/total/step/started/updated/ended_at/error/stats）+ 追加 `work/import.log`（带时间戳，grep -a 防 NUL）
- `load_base`95（读 archive.sqlite；**sqlite_master 无表/损坏→返回 {}**，0 字节库不崩）；`load_base_jsonl`150（**优先读 out/normalized.jsonl**——jsonl 是解析完成标志、先于 md/sqlite 落盘 → 断电可续跑；jsonl 空才回退 DB）
- `fingerprint`198（sha1 of 全部消息结构，不含 `_from`/`_prompt_only` 键）；`pick_conv`217（取消息多者，prompt_only 不覆盖富内容）；`merge`226：新增/变更判定 + **活动源保留规则：同 cid 且 new 是 activity 源且消息数≤base → keep base 不标 changed**（"只加新的"，不让 JSON 重解析覆盖旧 HTML 版）
- `lookup_hash`256（manifest inputs 哈希比对，按精确路径+文件名后缀兜底）；`parse_one`267（kind 分派，conv 打 `_from` 标记；gemini 的 .json→parse_gemini_json/activity）
- `build_md_index`289 / `write_md_delta`306（只重写新增/变更对话的 md，先删 `_{cid8}.md` 旧文件）
- `llm_ready`333（llm.has_key()，**绝不打印 key**）；`run_cmd`341（subprocess，stdout→import.log）
- `_analyzed_ids`350 / `_all_conv_ids`365；`run_analysis`377：pending=全 ids−done → fresh(=ids−done) 分块(400) `heuristic --ids` + `run --ids --force --topic self-psych`；stale(=changed∩done) `run --force --topic self-psych`；ready 时全局 pending 分块同样跑；**LLM 全部带 `--topic self-psych`（约定：只分析心理相关）**
- `run_import`429：load_base(jsonl 优先) → discover 五元组(chatgpt,takeout,gemini,activity,deepseek) → 逐输入 sha256 与 manifest 比对跳过未变 → 解析变更 → merge → 条件 backfill → 写 jsonl → md 增量 → sqlite 原子重建 → manifest 合并
- `build_parser`612：`--raw/--out/--work/--throttle/--tz/--match-threshold/--activity-tz-offset/--force-full/--analyze/--no-md/--no-sqlite/--dry-run/--parse-pause N`
- `main`631：chdir(ROOT)、`tempfile.tempdir=work/tmp`（防解压进 /tmp）、Runner 起停、`--analyze` 串 run_analysis

用法：`python3 scripts/incremental.py [--analyze] [--dry-run]`；断电后续跑=直接重跑（jsonl base+哈希跳过）。

## scripts/serve_archive.py（1329 行，Web 服务）

- 顶部：ROOT=上级，sys.path 插 tools 后 import arclib/analysis/llm/sanitize；全局 DB_PATH/WEB_DIR/WORK_DIR/ANALYSIS_DB_PATH（main 1278 重赋值）。MAX_LIMIT=200, LLM_TIMEOUT=600, INTERPRET_MAX=40000。
- api_* 行号：stats190 conversations209 conversation255 search275 timeline297 day322 jump339 selection366/373 export391 `_analysis_conn`416 `_analysis_run`423（读 analysis_progress.json，running+90s 心跳判 alive/stale）analysis452 emotion505 topic_summary627 llm_config830/838 reports879-907 interpret933/951。
- Handler1014：`send_json`1021 `send_text`1030 `_same_origin`1044（Origin 检查，无 Origin 放行）`_read_json`1056（Content-Length JSON 体）`post_llm`1072（OpenAI 兼容转发，SSE 流式）。
- do_GET1136 → handle_api1149：/api/stats|conversations|conversation(format=md)|search|timeline|day|jump|summary|analysis|emotion|llm/config|interpret|reports|selection|export。
- do_POST1200：`/api/llm` 走 post_llm；其余 `_read_json` 后路由 selection|llm/config|reports。**尚无文件上传/导入接口**（任务 B 待做：/api/upload + /api/import）。
- handle_static1231：限 WEB_DIR 内，gzip（>1024），no-cache → 改前端刷新即可。
- 中文搜索用 LIKE（FTS5 unicode61 对中文无效）。

## 分析机制（tools/analyze.py 704 行 + analysis.py 507 行 + heur.py 309 行）

analyze.py 行号：`_targets`71（SQL 过滤 + `--ids` 逗号 set 过滤 93-95 + min_msgs，ORDER BY created_at 升序）；`TOPIC_PRESETS`104（self-psych 正则：心理|情绪|焦虑|抑郁|自卑|内耗|社恐|社交恐惧|强迫|创伤|原生家庭|潜意识|咨询师|孤独|崩溃|躁郁|双相|精神科|自我怀疑|讨好型|安全感|想哭|难过|痛苦|委屈|沮丧|空虚|迷茫|失眠|睡不着|压力|害怕|不安）；**`_topic_rows`113（已修：扫 title+该对话全部消息全文，不再只扫首条用户消息前 800 字——旧版缺陷导致 0 命中，2026-10-10 修复，实测 166 pending→91 命中）**，含启发式 kind='情绪' 并集；`_select_pending`142（topic 模式 done 只算 model<>'heuristic'（heur 行不算已析→LLM 可覆盖）；excluded 任何模式排除；--force 绕过 done 不绕过 excluded）；`cmd_run`244（进度文件 `_progress_path`227=analysis_db 同目录 analysis_progress.json，20s 心跳，字段 running/finished/i/total/ok/fail/cost/model…，**只有 cmd_run 写**→网页进度卡读它）；`cmd_heuristic`386（本地秒级不写进度）；`build_parser`609；`_add_filter_args`685（`--ids/--collection/--source/--from/--to/--min-msgs/--topic`）。

- `work/analysis.sqlite` 独立于归档库，主键 **conversation_id**（analysis.py:38 upsert INSERT OR REPLACE；analysis_errors / analysis_excluded 辅表）。归档重建不影响已有分析。
- digest（analysis.py build_digest 251）：40 条采样首+均匀+尾、每条 700 字、总 6000 字、sanitize.mask 脱敏；SYSTEM_PROMPT 298（value 0-5、kind 九类、topics、sentiment、intensity、六情绪、summary≤60字，纯 JSON）。
- heur.py：纯本地规则（KIND_RULES 含「情绪」正则、词典六情绪、CJK 2gram topics），0 token 全量 ~5 秒，`model='heuristic'` `prompt_version='heur-v1'`。
- **约定：LLM 分析只跑心理相关**（`--topic self-psych`）；heuristic 可全量垫底。`selection.jsonl` 只有 `--collection` 才读，导入后自动分析不用动。
- zhipu glm-4.7-flash 免费（work/llm.json，key 已配，**值禁读禁打印**）；`llm.has_key()` 判可用。
- 增量补析：候选按 created_at 升序，`--limit` 优先最老 → 用 `--ids`（逗号）或 `--from <日期>` 圈定。

## 当前状态（2026-10-10 晚）

- **数据**：归档 5745 对话 / 75634 消息（jsonl、archive.sqlite 原子重建版、md 同步）；oneplus8 与 fedora 的 out/ 同源；manifest 三输入哈希匹配（增量 skipped 3 实测 8 秒完成）。
- **分析**：**5745/5745 全部分析完毕（pending=0）** / 失败 14（历史 error 计数）/ keep 2465 / avg 2.40 / ¥0。fedora 心理批完成：stale 6 + 关键词 91（90 成功、1 内容过滤排除），analysis.sqlite 已回传本机。
- **git**：HEAD 见 `git log`，最新含 46480fd（网页上传导入闭环）。历史：8902f35→97f233f→b32ca13→2ce1bed→(本轮)。本地分支 master，push 用 `git push origin HEAD:main`。
- **fedora 工作流**：`export SSH_ASKPASS=~/.ssh/askpass.sh SSH_ASKPASS_REQUIRE=force; ssh -o BatchMode=no fedora '<cmd>'`；fedora `~/ai-chat-archive`（非 git）。**systemd-run 不继承 shell 的 cd → 必须加 `-p WorkingDirectory=<repo>`**（psych 首启曾因 cwd=$HOME 失败 exit 2）；stdout 落文件用 `-p StandardOutput=append:<绝对路径>`（重定向符只会捕到 systemd-run 自己的输出）。
- **冰箱压测结论（2026-10-10）**：新电池（健康 97.5%，4160/4270mAh）+ 冰箱冷机（电池 20°C、CPU 33-39°C）下复现 import4-7 致死负载（.agent/stress_test.py，tgz→147MB JSON 解析），**<5 秒即复位**（.agent/stress.log 连一条心跳都没写完）→ **高温虚焊排除，供电问题坐实**：主线内核缺厂商电源管理协调（PMIC OCP/充电限流/瞬态电流预算），负载尖峰撞硬件保护硬断电，与温度无关；pstore 恒空。对策：重活只放 fedora；本机跑重活用 CPUQuota/降频拖延（10% 配额也死过，只是更久），治本需调内核电源参数。
- **web**：serve 常驻 **系统级** `/etc/systemd/system/ai-archive-web.service`（不是 --user！），改 serve_archive.py 后 `echo "$SUDO_PASS" | sudo -S systemctl restart ai-archive-web`；静态无缓存、改前端刷新即生效；库每请求新连接，换库即生效。
- 轮询：work/import_progress.json + work/import.log（grep -a 防 NUL）；**import 状态接口自带 180s 心跳时效判断**（陈旧 running:true 自动判 stale）。工具超时 900s 杀前台杀不掉 --unit。

## 当前任务（2026-10-10 晚更新）

用户需求（原话）：
1. 「现在有deepseek 10.08的导出，把新增的记录导进去，然后照惯例，分析心理内容。」
2. 「我希望这个脚本可以智能一点，然后能够在网页端上传然后完成分析……很快gemini的json也要导入进来……智能的点在于，能够把新的东西放进来。」
3. 「把本地分析放fedora跑，谢谢，我要睡了，你继续，不要再把这台机器搞崩了。」
4. 「你根本没对新内容跑主题分析……好好读一遍整个项目，重新写好md再开工」
5. 约定：「只跑心理相关的」（LLM 只跑 --topic self-psych）。
6. 「我放冰箱了，看看到底是供电问题还是这台机子高温虚焊」→ 已测，结论见上（供电问题）。

拆解：
- [x] A. 智能增量导入 scripts/incremental.py（sha256 跳过、jsonl 优先 base、指纹合并、活动源只加新的、原子 sqlite、--parse-pause）
- [x] C. Gemini Takeout JSON 解析 parse_gemini_activity_json + discover 路由（AI Mode 排除）
- [x] D. 导入 10-08 + 新 Takeout → 5745/75634 ✓
- [x] _topic_rows 全文扫描修复（实测 91 命中）+ 心理分析全部跑完（5745/5745，fedora 执行回传）
- [x] B. 网页上传闭环：POST /api/upload（原始字节体+X-Filename→raw/）、POST/GET /api/import（后台线程跑 incremental --analyze、防重入、180s 心跳时效）、智能页上传导入卡+2.5s 轮询；py_compile+node --check+接口实测全过（upload 落盘、import 8 秒完成 skipped 3）
- [x] 冰箱压测（冷机秒死 → 供电问题结论，.agent/stress_test.py + stress.log）
- [x] README 补 incremental/上传导入/--topic 章节（2026-10-10 完成，全部任务闭环）

## 环境与命令备忘

- 限速跑（本机）：`systemd-run --user --collect --unit=ai-archive-importN -p CPUQuota=10% nice -n 19 python3 /home/SQL916/ai-chat-archive/scripts/incremental.py --analyze`
- 状态：`python3 tools/analyze.py status`（轻，可本机跑）。
- 自动提交推送：验证过就 `git add <相关文件> && git commit -m <中文> && git push`（本地分支 master，push 用 `git push origin HEAD:main`），不问；不提交密钥。
- 凭据：SUDO_PASS/GH_TOKEN/SSH_PASS/LLM key 一律不打印不入文件；sudo 用 `echo "$SUDO_PASS" | sudo -S`。
