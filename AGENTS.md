# 开发与维护说明

## 代码结构

- `scripts/archive_ai_chats.py`：ChatGPT、DeepSeek、Gemini 导出发现、解析和标准化；
  生成 JSONL、Markdown 和含全文索引的 SQLite 归档。
- `scripts/incremental.py`：日常增量导入入口，使用输入哈希和解析器版本判断是否补导入；
  优先从 JSONL 恢复已有归档，以来源和对话 ID 合并，防止只有提问的数据覆盖完整问答。
- `scripts/export_monthly.py`：按北京时间逐条消息分月，生成全部月份文件、目录、校验统计和可选 ZIP；
  输出到新目录后原子发布。日期回退、未知日期、跨月拆分和无正文占位由测试覆盖。
- `tools/export.py`、`tools/archive_cli.py`：全部、指定月份、ISO 周的文字合集导出。
- `scripts/serve_archive.py` 与 `web/`：本地浏览器界面和 API。
- `tools/arclib.py`：CLI 与 Web 共用的数据库访问层。
- `tools/analysis.py`、`tools/analyze.py`、`tools/heur.py`、`tools/llm.py`：可选分析工具；
  仅在用户明确要求时运行 LLM 分析，不读取或打印凭据值。

## 输入兼容与缓存

- ChatGPT 聊天文件必须按 basename 严格匹配 `conversations(?:-[0-9]+)?\.json`，
  支持 ZIP/TAR 内分片，排除 shared/sectioned 等元数据。
- ChatGPT 与 DeepSeek 使用内容嗅探区分。DeepSeek 全量导出替换旧快照时，避免多份旧快照重复导入。
- Gemini 活动 JSON 按 header 过滤产品，排除 AI Mode；支持 `Prompted`、`Branched`、
  `Imported prompt`、`Added chat from link:` 四类问答，反馈和独立 Canvas 活动不计入问答。
- 用户附件保存在消息的 attachments 字段；纯附件提问和纯图片回答不可静默丢弃。
  Takeout 中上传过的其它平台聊天附件不能当成独立原始导出重复导入。
- 当前 `parser_versions`：`chatgpt=2`、`activity=2`。修复会影响旧输入解析时应更新相应版本，
  并覆盖旧缓存补导入及再次导入跳过的测试。
- 保留 SQLite 和导出文件的原子替换机制；日常导入使用 incremental，不无故全量重建。

## 验证与数据隔离

- Python 功能仅使用标准库。测试使用合成数据，禁止读取开发者的真实聊天或绝对个人目录。
- 测试前创建 `.agent/`，运行 `python3 -m unittest discover -s tests -v`；
  前端变更额外运行 `node --check web/app.js`。
- 将解压和测试中间文件放入仓库被忽略的 `.agent/` 或 `work/`，避免把重要文件写到临时系统目录。
  在资源受限环境中，可使用 `systemd-run --user -p CPUQuota=10%` 与 `nice -n 19` 限制负载。
- 不提交 `raw/`、`out/`、`work/`、`.agent/`、数据库、日志、环境文件、密钥或真实聊天导出。
  文档使用通用相对路径和虚构示例，不记录个人路径、设备信息、归档统计或真实对话 ID。
- 仅提交经过验证、与任务相关的代码；保留其它未提交工作。提交、推送和发布范围以用户要求为准。

## Gemini 兼容修复的回归范围

`tests/test_gemini_import.py` 覆盖四类问答、排序、纯附件与纯图片、HTML 表格/代码、
Takeout 压缩包发现与上传附件隔离，以及解析器升级补导入和北京时间月界。
`tests/test_monthly_export.py` 覆盖跨月、日期回退、未知日期、正文/媒体处理、ZIP 校验和已有目录保护。
