# -*- coding: utf-8 -*-
"""llm_client：OpenAI 兼容 LLM 客户端（docs/15 §3.4——一份代码，两种 provider）。

环境变量路由（key 一配置就可以用，全部可覆盖）：
    LLM_PROVIDER     local | online（默认 local；只决定 base/并发/超时的缺省，不决定是否外发）
    LLM_BASE         local: http://127.0.0.1:11434/v1 | online: https://api.deepseek.com/v1
                     非回环地址一律按"内容离开本机"处理：需 LLM_ALLOW_CONTENT_UPLOAD=1 且上传前脱敏
    LLM_KEY          online 必填（用户级环境变量，绝不进 git；local 留空）
    LLM_ALLOW_CONTENT_UPLOAD=1  显式同意把已脱敏文本发送到非本地 endpoint；默认拒绝
    LLM_MODEL        local: qwen3-4b | online: deepseek-flash
    LLM_CONCURRENCY  local: 1 | online: 8
    LLM_PRICE        输入价,输出价（¥/百万 token；如 "1,4"）
    LLM_JSON_MODE    覆盖 provider 默认的 json_object 支持开关（1/0 true/false yes/no on/off）
    LLM_NUM_CTX      上下文窗口透传（正整数，如 8192；缺省不透传——docs/13 §5）
                     keep_alive 是服务端部署项（OLLAMA_KEEP_ALIVE），客户端不实现
    LLM_THINKING     思考模式 enabled|disabled（缺省不传；V4 系列结构化输出需 disabled）
    LLM_RPM          全局请求/分钟上限（正整数；缺省不限。网关限流时设 5 等低值）

降级策略（docs/15 §3.4）：429/5xx/超时 → 指数退避重试 3 次；单题 3 次失败由
batch_label 落 failed.jsonl 不阻塞整批。JSON 解析失败 → 重试时强化"只输出 JSON"。

用法：
    from mianjing_kb.llm_client import resolve_config, chat_json
    cfg = resolve_config()                 # online 缺 key → ConfigError（带配置指引）
    obj = chat_json(cfg, system, user)     # 严格 JSON：response_format + 围栏/前后缀宽松解析
"""
from __future__ import annotations

import ipaddress
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mianjing_kb.probe_provider import parse_json_loose  # noqa: E402  探针同款宽松解析（围栏/前后缀/嵌套抠取）


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


def _endpoint_host(base: str) -> str:
    """校验 LLM_BASE 并取出 host。

    只看真正要连的地址，不看 LLM_PROVIDER：provider 只是缺省值选择器，用户可以
    把 LLM_BASE 指到任何地方，用 provider 名判断"是否外发"会留后门。
    """
    raw = _as_str("LLM_BASE", base).strip()
    try:
        parts = urlsplit(raw)
    except ValueError:
        raise ConfigError(f"LLM_BASE 无法解析，当前: {raw!r}") from None
    if parts.scheme not in ("http", "https"):
        raise ConfigError(f"LLM_BASE 只允许 http/https，当前: {raw!r}")
    if "@" in parts.netloc:
        raise ConfigError("LLM_BASE 不允许带 user:pass@ 形式的凭证，请改用 LLM_KEY")
    if not parts.hostname:
        raise ConfigError(f"LLM_BASE 缺少主机名，当前: {raw!r}")
    if any(c.isspace() for c in raw):
        raise ConfigError(f"LLM_BASE 不允许包含空白字符，当前: {raw!r}")
    return parts.hostname


_LOOPBACK_NAMES = {"localhost", "localhost."}


def is_local_endpoint(base: str) -> bool:
    """目标地址是否为本机回环（localhost / 127.0.0.0/8 / ::1）。

    判定以实际地址为准：非回环一律按"内容会离开本机"处理，与 provider 叫什么无关。
    """
    host = _endpoint_host(base).strip("[]").lower()
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:              # 域名（api.deepseek.com 等）→ 远程
        return False


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
        # 在线 provider 不因配置 API key 就默认获得题库内容外发授权。
        "allow_content_upload": _parse_bool("LLM_ALLOW_CONTENT_UPLOAD", e["LLM_ALLOW_CONTENT_UPLOAD"])
        if e.get("LLM_ALLOW_CONTENT_UPLOAD") else False,
        # 批次三 R4：上下文窗口透传（docs/13 §5：批量打标 8192）；缺省 None 不透传
        "num_ctx": _parse_positive_int("LLM_NUM_CTX", e["LLM_NUM_CTX"])
        if e.get("LLM_NUM_CTX") else None,
        # DeepSeek/Qiniu V4 系列默认开思考，会把 max_tokens 吃光并返回空 content；
        # 显式 disabled 用于结构化打标。留空则不传，保持各家默认。
        "thinking": _as_str("LLM_THINKING", e.get("LLM_THINKING") or "").strip().lower() or None,
        # 网关 RPM 限流（七牛实测 5 次/分钟即触发 429）：>0 时按最小间隔 60/rpm 全局节流
        "rpm": _parse_positive_float("LLM_RPM", e["LLM_RPM"]) if e.get("LLM_RPM") else 0.0,
    }
    if cfg["thinking"] not in (None, "enabled", "disabled"):
        raise ConfigError(
            f"LLM_THINKING 只允许 enabled|disabled，当前: {cfg['thinking']!r}")
    # 外发边界以实际 endpoint 判定：local + 远程 LLM_BASE 同样属于外发。
    cfg["local_endpoint"] = is_local_endpoint(cfg["base"])
    if provider == "online" and not cfg["key"]:
        raise ConfigError(
            "online provider 需要 API key：请设用户级环境变量 LLM_KEY "
            "（PowerShell: [Environment]::SetEnvironmentVariable('LLM_KEY','sk-xxx','User')），"
            "或临时 $env:LLM_KEY='sk-xxx'。key 绝不进 git。")
    return cfg


# ---------------- 脱敏（docs/15 §3.7：上传前必做） ----------------
_RE_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_RE_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_RE_WECHAT = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(?:"
    r"微信(?:号码?)?\s*[:：]?\s*"
    r"|(?:weixin|wx|qq)(?:(?:号|号码)\s*[:：]?\s*|\s*[:：]\s*|\s+|(?=[0-9_-]))"
    r")[A-Za-z0-9_-]{5,20}(?![A-Za-z0-9_-])"
)
_RE_IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_RE_NAMED_PERSON = re.compile(
    r"(?i)(?:姓名|联系人|收件人|候选人|面试者|作者|负责人|户主)\s*[:：=]\s*"
    r"[\u4e00-\u9fff·]{2,20}"
)
_RE_ADDRESS = re.compile(
    r"(?i)(?:家庭住址|联系地址|通信地址|住址|地址)\s*[:：=]\s*[^\r\n，。；;]{4,100}"
)
_RE_PASSPORT = re.compile(
    r"(?i)(?:护照(?:号码|号)?|passport(?:\s*(?:no|number))?)\s*[:：#=]?\s*[A-Z0-9]{6,20}"
)
_RE_BANK_ACCOUNT = re.compile(
    r"(?i)(?:银行卡号|银行账号|银行账户|bank\s*account|account\s*(?:no|number))"
    r"\s*[:：#=]?\s*[0-9 -]{10,30}"
)
REDACT_MARK = "[联系方式]"
PERSON_MARK = "[个人信息]"
PATH_MARK = "[本机路径]"
KEY_MARK = "[LLM_KEY]"
# 本机绝对路径（ISSUE-05）：题干/答案里常带盘符路径（`<盘符>:\<用户目录>\…`）或 POSIX 家目录前缀。
# 这类路径既是个人信息（用户名）又是本机目录结构，外发前必须遮罩。
# 前一个字符不能是字母/数字/下划线/斜杠/冒号——避免把 URL 里同名家目录片段当成路径；
# 中文字符不在排除集里，所以"路径是" + 盘符开头仍能命中。带空格的路径需原文加引号。
_RE_LOCAL_PATH_QUOTED = re.compile(r'''(["'])((?:[A-Za-z]:\\|\\\\)[^\r\n]*?)\1''')
_RE_LOCAL_PATH = re.compile(
    r"""(?<![A-Za-z0-9_/\\:])"""
    r"""(?:[A-Za-z]:\\[^\s<>"'，,。；;、）)】]+"""
    r"""|\\\\[^\s\\/]+\\[^\s\\/]+\\[^\s<>"'，,。；;、）)】]+"""
    r"""|/(?:home|Users|mnt/[a-z]/Users)/[^\s<>"'，,。；;、）)】]+)""")
# 技术文档 URL 是题干上下文，保留 host/path；只清洗 userinfo 和敏感 query。
_RE_URL = re.compile(r"https?://[^\s，。；）)】]+")
_RE_SENSITIVE_QUERY = re.compile(
    r"(?i)^(?:access[_-]?token|token|api[_-]?key|apikey|key|secret|sign|signature|"
    r"auth|authorization|password|passwd|pwd|session|sessionid|sig|x-amz-signature)$"
)
_URL_SECRET_MARK = "REDACTED"


def _sanitize_url(raw: str) -> tuple[str, bool]:
    """保留技术 URL，但去掉 user:pass@ 与 token/signature 等敏感 query。"""
    try:
        parts = urlsplit(raw)
    except ValueError:
        return raw, False
    changed = False
    netloc = parts.netloc
    if "@" in netloc:
        netloc = netloc.rsplit("@", 1)[1]       # 去掉 userinfo，保留 host:port
        changed = True
    pairs = parse_qsl(parts.query, keep_blank_values=True)
    query = parts.query
    if pairs:
        kept = []
        for k, v in pairs:
            if _RE_SENSITIVE_QUERY.match(k):
                kept.append((k, _URL_SECRET_MARK))
                changed = True
            else:
                kept.append((k, v))
        if changed:
            query = urlencode(kept, doseq=True)
    if not changed:
        return raw, False
    clean = urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
    return clean, True


def scrub_secret(text: str, cfg: dict | None = None) -> str:
    """异常/日志出口统一清洗密钥：配置里的原值、`sk-...`、Bearer 头都不留。

    key 只应出现在 Authorization 请求头里；HTTPError 响应体、URLError 文本、重试
    汇总都可能把它带回来（有的网关会把请求头回显到错误 JSON），所以在**拼错误
    字符串时**清洗，而不是指望调用方记得脱敏。
    """
    if text is None:
        return ""
    s = str(text)
    key = (cfg or {}).get("key") or ""
    if key:
        s = s.replace(key, KEY_MARK)
    s = re.sub(r"sk-[A-Za-z0-9_\-]{6,}", KEY_MARK, s)
    s = re.sub(r"(?i)(authorization\s*:\s*bearer\s*)\S+", r"\1" + KEY_MARK, s)
    s = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{6,}", r"\1" + KEY_MARK, s)
    return s



def redact(text: str) -> tuple[str, bool]:
    """遮罩本机绝对路径及常见结构化联系方式、姓名/地址标签、护照和银行账号。

    这不是通用 NER：无法保证识别没有字段标签的自由文本姓名/住址；online 请求另需显式
    LLM_ALLOW_CONTENT_UPLOAD=1，授权后仍只发送本函数处理过的文本。
    """
    hit = False

    def _sub(rx: re.Pattern) -> None:
        nonlocal text, hit
        new = rx.sub(REDACT_MARK, text)
        if new != text:
            hit = True
            text = new

    for rx in (_RE_NAMED_PERSON, _RE_ADDRESS, _RE_PASSPORT, _RE_BANK_ACCOUNT):
        new_text = rx.sub(PERSON_MARK, text)
        if new_text != text:
            hit = True
            text = new_text

    # 先遮引号路径（允许空格），再遮裸路径；都保留原引号/边界字符，避免把句子吞掉。
    def _quoted(m: re.Match) -> str:
        nonlocal hit
        hit = True
        return f"{m.group(1)}{PATH_MARK}{m.group(1)}"

    text = _RE_LOCAL_PATH_QUOTED.sub(_quoted, text)
    new_text = _RE_LOCAL_PATH.sub(PATH_MARK, text)
    if new_text != text:
        hit = True
        text = new_text

    for rx in (_RE_IDCARD, _RE_PHONE, _RE_EMAIL, _RE_WECHAT):
        _sub(rx)

    def _url_sub(m: re.Match) -> str:
        nonlocal hit
        clean, changed = _sanitize_url(m.group(0))
        if changed:
            hit = True
        return clean

    # 2026-11-20：URL 不再整体遮罩——技术题常带文档/仓库链接，整段删掉会丢上下文；
    # 只清洗 userinfo 与 token/signature 等敏感 query，host/path 保留。
    text = _RE_URL.sub(_url_sub, text)
    return text, hit


# ---------------- 网络层 ----------------
_RATE_LOCK = threading.Lock()
_RATE_NEXT = 0.0


def _rate_wait(cfg: dict) -> None:
    """按 cfg["rpm"] 全局节流（跨线程共享下一次可发时刻）。429 网关多为滑动窗口，
    单纯并发 1 仍会超；必须保证相邻请求间隔 >= 60/rpm。"""
    rpm = float(cfg.get("rpm") or 0)
    if rpm <= 0:
        return
    global _RATE_NEXT
    with _RATE_LOCK:
        now = time.monotonic()
        wait = max(0.0, _RATE_NEXT - now)
        _RATE_NEXT = max(now, _RATE_NEXT) + 60.0 / rpm
    if wait > 0:
        time.sleep(wait)


def _post(cfg: dict, payload: dict) -> tuple[int, dict]:
    """POST /chat/completions。返回 (status, resp)；网络层异常归一为 (0, {"error": ...})。

    网络出口处的最后一道闸：非回环地址且没有内容外发授权，直接拒绝，不发一个字节。
    """
    if not is_local_endpoint(cfg["base"]) and not cfg.get("allow_content_upload", False):
        raise ConfigError(
            f"LLM_BASE 指向非本地地址（{cfg['base']}），上传题库内容需显式授权："
            "确认风险后设置 LLM_ALLOW_CONTENT_UPLOAD=1，再重新启动命令。")
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
            body = {"raw": scrub_secret(str(e), cfg)}
        if isinstance(body, dict) and body.get("error"):
            body["error"] = scrub_secret(body["error"], cfg)
        return e.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": scrub_secret(f"{type(e).__name__}: {e}", cfg)}


def chat(cfg: dict, system: str, user: str, *, json_mode: bool = False,
         max_tokens: int = 128, temperature: float = 0.1,
         retries: int = 3, sleep: float = 1.0) -> dict:
    """一次对话（含重试退避）。返回 {text, latency_ms, tokens, prompt_tokens,
    completion_tokens}（F5：in/out 拆分，tokens 为两者合计）；重试耗尽抛 RuntimeError。
    cfg["num_ctx"] 设置时 payload 透传 options.num_ctx（Ollama 上下文窗口，R4）。

    降级链：429/5xx/超时/网络 → 指数退避重试；JSON 模式下解析失败也重试
    （第二次起在 system 末尾强化"只输出严格 JSON，不要任何其他文字"）。
    """
    remote = not is_local_endpoint(cfg["base"])
    if remote and not cfg.get("allow_content_upload", False):
        raise ConfigError(
            f"LLM_BASE 指向非本地地址（{cfg['base']}），题库内容会离开本机；"
            "确认外发风险后显式设置 LLM_ALLOW_CONTENT_UPLOAD=1，再重新启动命令。"
            "（只看实际地址，不看 LLM_PROVIDER）")
    # 最终网络边界再次脱敏，避免新增调用方绕过 batch_label 的预处理。
    # 该规则只覆盖可识别格式，不构成自由文本匿名化保证。
    outbound_system = redact(system)[0] if remote else system
    outbound_user = redact(user)[0] if remote else user
    try:
        ollama_endpoint = urlsplit(cfg["base"]).port == 11434
    except ValueError:
        raise ConfigError(f"LLM_BASE 端口无法解析，当前: {cfg['base']!r}") from None
    sys_prompt = outbound_system
    last_err = ""
    for attempt in range(1, retries + 1):
        if attempt > 1 and json_mode:
            sys_prompt = outbound_system + "\n只输出严格 JSON，不要任何其他文字、围栏或解释。"
        # /no_think 是 Ollama 专用控制标记；只有本次 JSON 调用且服务端支持时可省略。
        u_text = (outbound_user if (json_mode and cfg.get("json_mode")) or not ollama_endpoint
                  else outbound_user + "\n/no_think")
        payload = {
            "model": cfg["model"],
            "messages": [{"role": "system", "content": sys_prompt},
                         {"role": "user", "content": u_text}],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode and cfg.get("json_mode"):
            payload["response_format"] = {"type": "json_object"}
        if cfg.get("thinking"):
            payload["thinking"] = {"type": cfg["thinking"]}
        # R4：num_ctx 设置时透传（Ollama 上下文窗口）；keep_alive 是服务端部署项
        # （OLLAMA_KEEP_ALIVE），客户端不实现——docs/13:76
        # options 是 Ollama 原生字段，OpenAI 兼容端点遇未知键可能直接 400 → 只对 local 透传
        if cfg.get("num_ctx") and cfg.get("provider", "local") == "local":
            payload["options"] = {"num_ctx": cfg["num_ctx"]}
        t0 = time.perf_counter()
        _rate_wait(cfg)
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
                    "prompt_tokens": int(u.get("prompt_tokens") or 0),
                    "completion_tokens": int(u.get("completion_tokens") or 0),
                    "tokens": int(u.get("prompt_tokens") or 0) + int(u.get("completion_tokens") or 0)}
        last_err = scrub_secret(
            f"status={st} {str(resp.get('error') or resp)[:160]}", cfg)
        if st and st not in (429, 500, 502, 503, 504) and st != 0:
            break               # 4xx（鉴权/参数）重试无意义，快速失败
        if attempt < retries:   # 末次失败不再空等（随后就 raise）
            time.sleep(sleep * (2 ** (attempt - 1)) + random.uniform(0, sleep / 2))
    raise RuntimeError(scrub_secret(f"LLM 调用重试 {retries} 次耗尽: {last_err}", cfg))


def chat_json(cfg: dict, system: str, user: str, **kw) -> dict:
    """JSON 对话：chat + 宽松解析。解析失败返回 r（parse_failed=True, obj=None），
    调用方按 r.get("obj") 判（obj=None 即失败，落 failed 队列）。"""
    r = chat(cfg, system, user, json_mode=True, **kw)
    obj = parse_json_loose(r["text"])
    if obj is None:
        r["parse_failed"] = True
    r["obj"] = obj
    return r
