#!/usr/bin/env python3
"""可配置的 LLM 适配层（纯标准库，OpenAI 兼容的 /chat/completions）。

默认指向 DeepSeek：
    官方文档  https://api-docs.deepseek.com/zh-cn/
    API 根地址 https://api.deepseek.com/v1
    默认模型   deepseek-chat
    API key   取环境变量 DEEPSEEK_API_KEY

任何 OpenAI 兼容的服务（DeepSeek、OpenAI、Moonshot、智谱、vLLM、
Ollama 的 /v1 兼容层、各类中转）都能直接用，改配置即可。

内置两套预设，用 "preset" 选：
    deepseek（默认）  https://api.deepseek.com/v1  model=deepseek-chat  key=DEEPSEEK_API_KEY
    zhipu             https://open.bigmodel.cn/api/paas/v4  model=glm-4.7-flash（免费）
                      key=ZHIPU_API_KEY  文档 https://docs.bigmodel.cn/cn/api/introduction

配置优先级（高 -> 低）：
    1. 调用时显式传入的参数
    2. 环境变量 ARCHIVE_LLM_BASE_URL / ARCHIVE_LLM_API_KEY / ARCHIVE_LLM_MODEL / ARCHIVE_LLM_PRESET
    3. 配置文件 work/llm.json
    4. preset 预设（默认 deepseek）
    5. 内置默认值

work/llm.json 示例（用免费智谱 GLM-4.7-Flash）：
    {
      "preset": "zhipu",
      "api_key_env": "ZHIPU_API_KEY"
    }
其余字段都会从 preset 继承；想换模型只写 "model" 即可。

work/llm.json 示例（DeepSeek）：
    {
      "base_url": "https://api.deepseek.com/v1",
      "api_key_env": "DEEPSEEK_API_KEY",
      "model": "deepseek-chat",
      "timeout": 120,
      "temperature": 0.2,
      "max_tokens": 900,
      "retries": 3
    }
也可以直接写 "api_key": "sk-..."（不推荐，会被写进文件）。

extra_payload 会把内容合并进请求体，用来传各家私有参数，例如智谱关思考：
    "extra_payload": {"thinking": {"type": "disabled"}}

本模块导入时不会联网，只有调用 chat() / chat_json() 才会发请求。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "work" / "llm.json"

DOCS_URL = "https://api-docs.deepseek.com/zh-cn/"

DEFAULTS = {
    "preset": "deepseek",
    "base_url": "https://api.deepseek.com/v1",
    "api_key_env": "DEEPSEEK_API_KEY",
    "api_key": "",
    "model": "deepseek-chat",
    "timeout": 120,
    "temperature": 0.2,
    "max_tokens": 900,
    "retries": 3,
    "extra_headers": {},
    "extra_payload": {},
    "docs": DOCS_URL,
}

# 预设：选 preset 就能一键切换服务商，其余字段照常覆盖。
PRESETS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "model": "deepseek-chat",
        "docs": DOCS_URL,
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "api_key_env": "ZHIPU_API_KEY",
        "model": "glm-4.7-flash",
        "docs": "https://docs.bigmodel.cn/cn/api/introduction",
        "temperature": 0.3,
        # GLM-4.7 系列默认开思考；批处理分类任务关掉更快也更省 token
        "extra_payload": {"thinking": {"type": "disabled"}},
    },
}

# 每 1M token 的价格（人民币），仅用于粗略估算，可以自己在配置里覆盖。
# 0 表示免费；不在表里的模型会显示「未知」。
PRICES = {
    "deepseek-chat": {"in": 2.0, "out": 8.0},
    "deepseek-reasoner": {"in": 4.0, "out": 16.0},
    # 智谱
    "glm-4.7-flash": {"in": 0.0, "out": 0.0},
    "glm-4.7-flashx": {"in": 0.5, "out": 3.0},
    "glm-4.7": {"in": 3.0, "out": 14.0},
    "glm-4.5-air": {"in": 0.8, "out": 6.0},
    "glm-4-flash-250414": {"in": 0.0, "out": 0.0},
    "glm-z1-flash": {"in": 0.0, "out": 0.0},
    "glm-4-flashx-250414": {"in": 0.1, "out": 0.1},
    "glm-4-long": {"in": 1.0, "out": 1.0},
}


class LLMError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 配置
# --------------------------------------------------------------------------

def load_config(path=None, **overrides):
    """合并默认值、preset、配置文件、环境变量和显式参数，返回一个配置 dict。"""
    explicit = {}
    p = Path(path) if path else CONFIG_PATH
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise LLMError("配置文件读取失败 %s: %s" % (p, e))
        if isinstance(data, dict):
            explicit.update({k: v for k, v in data.items() if v is not None})
    env_map = {
        "preset": "ARCHIVE_LLM_PRESET",
        "base_url": "ARCHIVE_LLM_BASE_URL",
        "api_key": "ARCHIVE_LLM_API_KEY",
        "model": "ARCHIVE_LLM_MODEL",
    }
    for key, env in env_map.items():
        if os.environ.get(env):
            explicit[key] = os.environ[env]
    explicit.update({k: v for k, v in overrides.items() if v is not None})

    cfg = dict(DEFAULTS)
    preset = PRESETS.get((explicit.get("preset") or cfg.get("preset") or "").strip())
    if preset:
        cfg.update(preset)
    cfg.update(explicit)
    return cfg


def resolve_api_key(cfg):
    """返回可用的 API key；找不到就抛 LLMError（不打印 key 本身）。"""
    key = (cfg.get("api_key") or "").strip()
    if key:
        return key
    env = (cfg.get("api_key_env") or "").strip()
    if env and os.environ.get(env):
        return os.environ[env].strip()
    raise LLMError(
        "没有可用的 API key：请在 work/llm.json 里设置 api_key_env（默认 "
        "DEEPSEEK_API_KEY），或设置环境变量 ARCHIVE_LLM_API_KEY。"
    )


def has_key(cfg=None):
    try:
        resolve_api_key(cfg or load_config())
        return True
    except LLMError:
        return False


def describe(cfg=None):
    """给 CLI / MCP 用的一份「脱敏」配置说明。"""
    cfg = cfg or load_config()
    env = (cfg.get("api_key_env") or "").strip()
    return {
        "preset": cfg.get("preset"),
        "base_url": cfg.get("base_url"),
        "model": cfg.get("model"),
        "api_key_env": env,
        "api_key_present": has_key(cfg),
        "timeout": cfg.get("timeout"),
        "temperature": cfg.get("temperature"),
        "max_tokens": cfg.get("max_tokens"),
        "retries": cfg.get("retries"),
        "extra_payload": cfg.get("extra_payload") or {},
        "config_path": str(CONFIG_PATH),
        "config_exists": CONFIG_PATH.is_file(),
        "docs": cfg.get("docs") or DOCS_URL,
    }


# --------------------------------------------------------------------------
# 请求
# --------------------------------------------------------------------------

def _endpoint(base_url):
    base = (base_url or "").rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def chat(messages, cfg=None, json_mode=False, **overrides):
    """发一次 chat completion，返回 {text, usage, model, raw}。

    messages: [{"role": "system"|"user"|"assistant", "content": str}, ...]
    json_mode: 请求服务端返回 JSON 对象（DeepSeek / OpenAI 都支持）。
    失败会按 retries 重试（429 / 5xx / 网络错误），最后抛 LLMError。
    """
    cfg = load_config(**overrides) if cfg is None else cfg
    key = resolve_api_key(cfg)
    payload = {
        "model": cfg.get("model"),
        "messages": messages,
        "temperature": cfg.get("temperature", 0.2),
        "max_tokens": cfg.get("max_tokens", 900),
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    payload.update(cfg.get("extra_payload") or {})
    headers = {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        "User-Agent": "ai-chat-archive/llm",
    }
    headers.update(cfg.get("extra_headers") or {})
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    retries = max(0, int(cfg.get("retries") or 0))
    timeout = float(cfg.get("timeout") or 120)
    last = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(_endpoint(cfg.get("base_url")), data=body,
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read().decode("utf-8", "replace"))
            break
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            last = "HTTP %s: %s" % (e.code, detail)
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(min(30, 2 ** attempt))
                continue
            raise LLMError("调用失败 %s" % last)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last = "%s: %s" % (type(e).__name__, e)
            if attempt < retries:
                time.sleep(min(30, 2 ** attempt))
                continue
            raise LLMError("网络错误 %s" % last)

    try:
        choice = raw["choices"][0]
        message = choice.get("message") or {}
        text = message.get("content") or ""
        if not text:
            text = message.get("reasoning_content") or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError("响应结构不认识: %s" % json.dumps(raw, ensure_ascii=False)[:400])
    return {
        "text": text,
        "usage": raw.get("usage") or {},
        "model": raw.get("model") or cfg.get("model"),
        "raw": raw,
    }


def chat_json(messages, cfg=None, **overrides):
    """要求模型返回 JSON，并容错解析（会剥掉 ```json 围栏和前后废话）。"""
    res = chat(messages, cfg=cfg, json_mode=True, **overrides)
    return parse_json(res["text"]), res


def parse_json(text):
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1] if "\n" in s else s
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
    s = s.strip()
    try:
        return json.loads(s)
    except ValueError:
        pass
    start, end = s.find("{"), s.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(s[start:end + 1])
        except ValueError:
            pass
    raise LLMError("模型没有返回可解析的 JSON: %s" % s[:300])


def estimate_cost(usage, model=None):
    """按内置价目表粗略估算这次调用花了多少人民币。"""
    price = PRICES.get(model or "")
    if not price:
        return None
    pin = (usage or {}).get("prompt_tokens") or 0
    pout = (usage or {}).get("completion_tokens") or 0
    return (pin * price["in"] + pout * price["out"]) / 1_000_000.0


if __name__ == "__main__":  # 只做配置自检，不联网
    print(json.dumps(describe(), ensure_ascii=False, indent=2))
