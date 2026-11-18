# -*- coding: utf-8 -*-
"""llm_client：OpenAI 兼容 LLM 客户端（docs/15 §3.4——一份代码，两种 provider）。

环境变量路由（key 一配置就可以用，全部可覆盖）：
    LLM_PROVIDER     local | online（默认 local；决定 base/并发/超时的缺省）
    LLM_BASE         local: http://127.0.0.1:11434/v1 | online: https://api.deepseek.com/v1
    LLM_KEY          online 必填（用户级环境变量，绝不进 git；local 留空）
    LLM_MODEL        local: qwen3:4b | online: deepseek-flash
    LLM_CONCURRENCY  local: 1 | online: 8
    LLM_PRICE        输入价,输出价（¥/百万 token；如 "1,4"）
    LLM_JSON_MODE    覆盖 provider 默认的 json_object 支持开关（1/0 true/false yes/no on/off）

降级策略（docs/15 §3.4）：429/5xx/超时 → 指数退避重试 3 次；单题 3 次失败由
batch_label 落 failed.jsonl 不阻塞整批。JSON 解析失败 → 重试时强化"只输出 JSON"。

用法：
    from llm_client import resolve_config, chat_json
    cfg = resolve_config()                 # online 缺 key → ConfigError（带配置指引）
    obj = chat_json(cfg, system, user)     # 严格 JSON：response_format + 围栏/前后缀宽松解析
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probe_provider import parse_json_loose  # noqa: E402  探针同款宽松解析（围栏/前后缀/嵌套抠取）


class ConfigError(Exception):
    """配置缺失/非法——消息带可执行的配置指引。"""


DEFAULTS = {
    "local": {
        "base": "http://127.0.0.1:11434/v1",
        "model": "qwen3-4b",      # ollama 实际模型名（连字符；docs/12 冒烟同款）
        "concurrency": 1,
        "timeout": 300.0,          # 本地小模型慢
        "json_mode": False,        # ollama 不支持强制 json schema，靠提示词锁枚举
    },
    "online": {
        "base": "https://api.deepseek.com/v1",
        "model": "deepseek-flash",
        "concurrency": 8,
        "timeout": 60.0,
        "json_mode": True,         # 原生 response_format json_object
        "price": (1.0, 4.0),       # ¥/百万 token（闲时口径，预算估算用）
    },
}


def _as_str(name: str, raw) -> str:
    """配置值统一转字符串（os.environ 本就是 str；编程式调用传 int 等不得裸 AttributeError）。"""
    if not isinstance(raw, str):
        raise ConfigError(f"{name} 须为字符串，当前: {raw!r}（类型 {type(raw).__name__}）")
    return raw


def _parse_bool(name: str, raw: str) -> bool:
    """配置布尔解析（1/0 true/false yes/no on/off，大小写不敏感）；非法抛 ConfigError。"""
    v = _as_str(name, raw).strip().lower()
    if v in ("1", "true", "yes", "on"):
        return True
    if v in ("0", "false", "no", "off"):
        return False
    raise ConfigError(f"{name} 只接受 1/0 true/false yes/no on/off，当前: {raw!r}")


def _parse_positive_int(name: str, raw: str) -> int:
    """正整数配置（并发数）；非法/非正数抛 ConfigError 带指引。"""
    v = _as_str(name, raw).strip()
    try:
        n = int(v)
    except ValueError:
        raise ConfigError(f"{name} 须为正整数，当前: {raw!r}") from None
    if n <= 0:
        raise ConfigError(f"{name} 须为正整数，当前: {raw!r}")
    return n


def _parse_positive_float(name: str, raw: str) -> float:
    """正浮点配置（超时秒）；非法/非正数抛 ConfigError 带指引。"""
    v = _as_str(name, raw).strip()
    try:
        f = float(v)
    except ValueError:
        raise ConfigError(f"{name} 须为正数，当前: {raw!r}") from None
    if f <= 0:
        raise ConfigError(f"{name} 须为正数，当前: {raw!r}")
    return f


def _parse_price(raw: str) -> tuple[float, float]:
    """配置价格解析：必须恰好"输入价,输出价"两个非负浮点数；非法抛 ConfigError。"""
    parts = [p.strip() for p in _as_str("LLM_PRICE", raw).split(",")]
    if len(parts) != 2:
        raise ConfigError(f'LLM_PRICE 格式须为 "输入价,输出价"（如 "1,4"），当前: {raw!r}')
    try:
        pin, pout = float(parts[0]), float(parts[1])
    except ValueError:
        raise ConfigError(f'LLM_PRICE 须为数字对（如 "1,4"），当前: {raw!r}') from None
    if pin < 0 or pout < 0:
        raise ConfigError(f"LLM_PRICE 不可为负数，当前: {raw!r}")
    return pin, pout


def resolve_config(env: dict | None = None) -> dict:
    """环境变量 → 完整配置。online 缺 key 抛 ConfigError（带指引，不静默降级烧本地 GPU）。"""
    e = env if env is not None else os.environ
    provider = _as_str("LLM_PROVIDER", e.get("LLM_PROVIDER") or "local").strip().lower()
    if provider not in DEFAULTS:
        raise ConfigError(f"LLM_PROVIDER 只允许 local|online，当前: {provider!r}")
    d = DEFAULTS[provider]
    # P2 修复：json_mode 可由环境变量覆盖——换不支持 response_format 的兼容端点时可关；
    # 本地端点（如 llama.cpp server）支持时也可开。默认仍由 provider 类型决定。
    json_mode = _parse_bool("LLM_JSON_MODE", e["LLM_JSON_MODE"]) \
        if e.get("LLM_JSON_MODE") else bool(d["json_mode"])
    cfg = {
        "provider": provider,
        "base": _as_str("LLM_BASE", e.get("LLM_BASE") or d["base"]).rstrip("/"),
        "model": _as_str("LLM_MODEL", e.get("LLM_MODEL") or d["model"]),
        "key": _as_str("LLM_KEY", e.get("LLM_KEY") or ""),
        "concurrency": _parse_positive_int(
            "LLM_CONCURRENCY", e.get("LLM_CONCURRENCY") or str(d["concurrency"])),
        "timeout": _parse_positive_float(
            "LLM_TIMEOUT", e.get("LLM_TIMEOUT") or str(d["timeout"])),
        "json_mode": json_mode,
        "price": _parse_price(e["LLM_PRICE"]) if e.get("LLM_PRICE") else d.get("price", (0.0, 0.0)),
    }
    if provider == "online" and not cfg["key"]:
        raise ConfigError(
            "online provider 需要 API key：请设用户级环境变量 LLM_KEY "
            "（PowerShell: [Environment]::SetEnvironmentVariable('LLM_KEY','sk-xxx','User')），"
            "或临时 $env:LLM_KEY='sk-xxx'。key 绝不进 git。")
    return cfg


# ---------------- 脱敏（docs/15 §3.7：上传前必做） ----------------
_RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_RE_WECHAT = re.compile(r"(微信|weixin|wx|qq)\s*[:：号]?\s*[A-Za-z0-9_-]{5,20}", re.I)
_RE_IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_RE_URL = re.compile(r"https?://[^\s，。；）)】]+")
REDACT_MARK = "[联系方式]"


def redact(text: str) -> tuple[str, bool]:
    """遮罩手机号/邮箱/微信QQ/身份证/URL → [联系方式]。返回 (新文本, 是否命中)。"""
    hit = False

    def _sub(rx: re.Pattern) -> None:
        nonlocal text, hit
        new = rx.sub(REDACT_MARK, text)
        if new != text:
            hit = True
            text = new

    for rx in (_RE_IDCARD, _RE_PHONE, _RE_EMAIL, _RE_WECHAT, _RE_URL):
        _sub(rx)
    return text, hit


# ---------------- 网络层 ----------------
def _post(cfg: dict, payload: dict) -> tuple[int, dict]:
    """POST /chat/completions。返回 (status, resp)；网络层异常归一为 (0, {"error": ...})。"""
    req = urllib.request.Request(
        cfg["base"] + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": f"Bearer {cfg['key']}"} if cfg["key"] else {})},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:  # noqa: BLE001
            body = {"raw": str(e)}
        return e.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def chat(cfg: dict, system: str, user: str, *, json_mode: bool = False,
         max_tokens: int = 128, temperature: float = 0.1,
         retries: int = 3, sleep: float = 1.0) -> dict:
    """一次对话（含重试退避）。返回 {text, latency_ms, tokens}；重试耗尽抛 RuntimeError。

    降级链：429/5xx/超时/网络 → 指数退避重试；JSON 模式下解析失败也重试
    （第二次起在 system 末尾强化"只输出严格 JSON，不要任何其他文字"）。
    """
    sys_prompt = system
    last_err = ""
    for attempt in range(1, retries + 1):
        if attempt > 1 and json_mode:
            sys_prompt = system + "\n只输出严格 JSON，不要任何其他文字、围栏或解释。"
        # Qwen3 默认开思考链（吃光 max_tokens 且污染 JSON）：本地（无 response_format
        # 支持）追加 /no_think 关闭——docs/12 冒烟同款坑
        u_text = user if cfg["json_mode"] else user + "\n/no_think"
        payload = {
            "model": cfg["model"],
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": u_text}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode and cfg["json_mode"]:
            payload["response_format"] = {"type": "json_object"}
        t0 = time.perf_counter()
        st, resp = _post(cfg, payload)
        latency = round((time.perf_counter() - t0) * 1000)
        if st == 200:
            try:
                text = resp["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError):
                text = ""
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()  # 思考链兜底剥离
            u = resp.get("usage") or {}
            return {"text": text, "latency_ms": latency,
                    "tokens": int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)}
        last_err = f"status={st} {str(resp.get('error') or resp)[:160]}"
        if st and st not in (429, 500, 502, 503, 504) and st != 0:
            break               # 4xx（鉴权/参数）重试无意义，快速失败
        time.sleep(sleep * (2 ** (attempt - 1)))
    raise RuntimeError(f"LLM 调用重试 {retries} 次耗尽: {last_err}")


def chat_json(cfg: dict, system: str, user: str, **kw) -> dict | None:
    """JSON 对话：chat + 宽松解析。解析失败返回 None（调用方落 failed 队列）。"""
    r = chat(cfg, system, user, json_mode=True, **kw)
    obj = parse_json_loose(r["text"])
    if obj is None:
        r["parse_failed"] = True
    r["obj"] = obj
    return r
