# -*- coding: utf-8 -*-
"""probe_provider：验一家 OpenAI 兼容 provider 能不能拿来打标（4 项探针）。

为什么要探：SCNet（超算互联网）与 DeepSeek 官方虽然都称 OpenAI 兼容，但坑不一样——
  - SCNet 要求系统提示词走独立 `system` 字段，而不是 messages 里的 system 角色；
  - SCNet 的 `max_tokens` 必填；
  - SCNet 未声明支持 `response_format={"type":"json_object"}`。
任何一条不满足，T2 打标就会静默跑偏（提示词失效 / json 解析全失败），所以必须先探再批。

探针四项：
  P1 models      列模型，确认目标 model id 存在
  P2 text        一条普通对话，确认鉴权/端点通
  P3 system      system 字段是否被遵守（回复必须复述指定暗号）
  P4 json        json_object 是否被遵守（返回必须能被 json.loads）

用法：
    $env:SCNET_KEY="sk-..."    # 或用 --key
    python -m mianjing_kb.probe_provider --provider scnet --model <控制台id>
    python -m mianjing_kb.probe_provider --provider deepseek --model deepseek-flash
    python -m mianjing_kb.probe_provider --provider local --model qwen3-4b      # 本地 ollama
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from datetime import datetime

# provider 默认配置：base_url / key 环境变量名 / 三个兼容性开关
PROVIDERS: dict[str, dict] = {
    "scnet": {
        "base": "https://api.scnet.cn/api/llm/v1",
        "key_env": "SCNET_KEY",
        "system_field": True,      # 系统提示词走独立字段
        "max_tokens_required": True,
        "json_mode": "unknown",     # 待实测
        "price": (1.0, 4.0),        # ¥/百万 token（闲时口径，仅估算用）
    },
    "deepseek": {
        "base": "https://api.deepseek.com/v1",
        "key_env": "DEEPSEEK_API_KEY",
        "system_field": False,
        "max_tokens_required": False,
        "json_mode": "supported",
        "price": (1.0, 4.0),
    },
    "local": {
        "base": "http://127.0.0.1:11434/v1",
        "key_env": "",
        "system_field": False,
        "max_tokens_required": False,
        "json_mode": "unsupported",  # ollama 不支持强制 json schema
        "price": (0.0, 0.0),
    },
}

DARK_MARK = "QWEN3-LABEL-PROBE-42"   # P3 暗号：system 里给定，回复必须复述


# ---------------- 纯函数（可离线单测） ----------------
def build_chat_payload(cfg: dict, model: str, system: str, user: str,
                       json_mode: bool = False, max_tokens: int = 64,
                       temperature: float = 0.1) -> dict:
    """按 provider 兼容性开关拼 payload。"""
    payload: dict = {"model": model, "messages": [], "temperature": temperature}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    if cfg.get("max_tokens_required") or max_tokens:
        payload["max_tokens"] = max_tokens
    if cfg.get("system_field"):
        payload["system"] = system                      # SCNet：独立字段
        payload["messages"] = [{"role": "user", "content": user}]
    else:
        payload["messages"] = [{"role": "system", "content": system},
                               {"role": "user", "content": user}]
    return payload


def parse_json_loose(text: str) -> dict | None:
    """从可能带 ```json 围栏/前后缀的回复里抠出第一个 JSON 对象。"""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"(?s)^```(?:json)?\s*", "", t)
    t = re.sub(r"(?s)\s*```$", "", t)
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    depth = 0
    start = -1
    for i, ch in enumerate(t):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                try:
                    v = json.loads(t[start:i + 1])
                    if isinstance(v, dict):
                        return v
                except json.JSONDecodeError:
                    start = -1
    return None


def estimate_cost(tokens_in: int, tokens_out: int, price: tuple[float, float]) -> float:
    """¥ 成本估算（price = 每百万 token 的输入/输出单价）。"""
    return round(tokens_in / 1e6 * price[0] + tokens_out / 1e6 * price[1], 4)


def resolve_key(cfg: dict, key: str | None) -> str:
    if key:
        return key
    env = cfg.get("key_env") or ""
    return os.environ.get(env, "") if env else ""


def scrub(text: str, key: str) -> str:
    """把 key 从任意输出里抹掉：provider 的错误体会回显 Authorization 前缀/整串。"""
    if key and text:
        return str(text).replace(key, "***")
    return str(text or "")


# ---------------- 网络层（单测里被 monkeypatch 掉） ----------------
def post(url: str, payload: dict | None, key: str, timeout: float = 30.0) -> tuple[int, dict]:
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="GET" if payload is None else "POST")
    req.add_header("Content-Type", "application/json")
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        # 错误体先脱敏再解析，避免 key/cookie 这类回显被原样打印到终端
        body = scrub(e.read().decode("utf-8", "replace"), key)
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body}
        return e.code, parsed
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": scrub(f"{type(e).__name__}: {e}", key)}


def _content(resp: dict) -> str:
    try:
        return resp["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        return ""


def _usage(resp: dict) -> tuple[int, int]:
    u = resp.get("usage") or {}
    return int(u.get("prompt_tokens") or 0), int(u.get("completion_tokens") or 0)


# ---------------- 四项探针 ----------------
def probe(provider: str, model: str, key: str | None = None,
          timeout: float = 30.0, skip_json: bool = False) -> dict:
    cfg = PROVIDERS[provider]
    k = resolve_key(cfg, key)
    base = cfg["base"].rstrip("/")
    out: dict = {"provider": provider, "model": model, "base": base, "at": datetime.now().isoformat(timespec="seconds")}
    if provider != "local" and not k:
        out["fatal"] = f"缺 key：请设环境变量 {cfg.get('key_env')} 或 --key"
        return out

    # P1 models
    st, resp = post(f"{base}/models", None, k, timeout)
    ids = []
    for m in (resp.get("data") or []):
        mid = m.get("id") or m.get("model") or ""
        if mid:
            ids.append(mid)
    out["p1_models"] = {"status": st, "ok": st == 200, "model_in_list": model in ids,
                        "sample": ids[:8], "total": len(ids)}

    # P2 text
    t0 = time.perf_counter()
    st, resp = post(f"{base}/chat/completions",
                    build_chat_payload(cfg, model, "你是测试助手，只回一个字。", "回复：好", max_tokens=8),
                    k, timeout)
    lat = round(time.perf_counter() - t0, 2)
    text = _content(resp)
    tin, tout = _usage(resp)
    out["p2_text"] = {"status": st, "ok": st == 200 and bool(text), "latency_s": lat,
                      "content_head": text[:30], "tokens": [tin, tout],
                      "error": scrub(resp.get("error") or resp.get("message") or "", k)}

    # P3 system 字段是否被遵守（回复里必须出现暗号）
    user_msg = "请只输出你收到的系统提示里的暗号，不要解释。"
    st, resp = post(f"{base}/chat/completions",
                    build_chat_payload(cfg, model, f"暗号是 {DARK_MARK}，记住它。", user_msg, max_tokens=32),
                    k, timeout)
    got = _content(resp)
    out["p3_system"] = {"status": st, "ok": DARK_MARK in got, "content_head": got[:60],
                        "hint": "False 表示 system 未生效，需改传独立 system 字段" }

    # P4 json_object 是否被遵守
    if skip_json or cfg.get("json_mode") == "unsupported":
        out["p4_json"] = {"skipped": True, "reason": "provider 不支持或用户跳过"}
    else:
        st, resp = post(f"{base}/chat/completions",
                        build_chat_payload(cfg, model,
                                           '只输出严格 JSON：{"qtype":"八股","difficulty":3}',
                                           "HashMap 扩容机制是什么？", json_mode=True, max_tokens=64),
                        k, timeout)
        got = _content(resp)
        parsed = parse_json_loose(got)
        out["p4_json"] = {"status": st, "ok": bool(parsed and "qtype" in parsed),
                          "content_head": got[:80], "parsed": parsed}

    out["verdict"] = {
        "可用": all([out["p1_models"]["ok"], out["p2_text"]["ok"],
                    out["p3_system"]["ok"], out.get("p4_json", {}).get("ok", True)]),
        "需要改配置": [k2 for k2, v in (
            ("system_field", out["p3_system"]["ok"]),
            ("json_mode", out.get("p4_json", {}).get("ok", True)),
        ) if not v],
    }
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="OpenAI 兼容 provider 探针")
    ap.add_argument("--provider", default="scnet", choices=sorted(PROVIDERS))
    ap.add_argument("--model", required=True, help="模型 id（以各平台控制台/模型列表为准）")
    ap.add_argument("--key", default=None)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--skip-json", action="store_true")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)
    r = probe(args.provider, args.model, args.key, args.timeout, args.skip_json)
    # 最后一道：整份 JSON 里任何位置出现的 key 都抹掉（provider 可能把凭据回显进错误体）
    key = resolve_key(PROVIDERS[args.provider], args.key)
    txt = scrub(json.dumps(r, ensure_ascii=False, indent=1), key)
    print(txt)
    if args.out:
        from pathlib import Path
        Path(args.out).write_text(txt, encoding="utf-8")
    return 0 if r.get("verdict", {}).get("可用") else 1


if __name__ == "__main__":
    from mianjing_kb.kb.stdio import configure_utf8_stdio
    configure_utf8_stdio()
    raise SystemExit(main())
