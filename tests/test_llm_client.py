# -*- coding: utf-8 -*-
"""llm_client 离线测试：配置路由 / 缺 key 指引 / 重试退避 / JSON 宽松解析 / 脱敏。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from llm_client import (ConfigError, chat, chat_json, redact,  # noqa: E402
                        resolve_config)


# ---------------- 配置路由 ----------------
def test_config_local_defaults():
    cfg = resolve_config(env={})
    assert cfg["provider"] == "local" and cfg["base"].endswith("/v1")
    assert cfg["concurrency"] == 1 and cfg["json_mode"] is False


def test_config_online_requires_key_with_guidance():
    with pytest.raises(ConfigError) as ei:
        resolve_config(env={"LLM_PROVIDER": "online"})
    assert "LLM_KEY" in str(ei.value) and "SetEnvironmentVariable" in str(ei.value)


def test_config_online_with_key_and_overrides():
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "sk-x",
                              "LLM_MODEL": "deepseek-flash", "LLM_CONCURRENCY": "8"})
    assert cfg["key"] == "sk-x" and cfg["concurrency"] == 8
    assert cfg["json_mode"] is True and cfg["price"] == (1.0, 4.0)


def test_config_bad_provider():
    with pytest.raises(ConfigError):
        resolve_config(env={"LLM_PROVIDER": "midway"})


# ---------------- P2：LLM_JSON_MODE 覆盖 ----------------
def test_json_mode_override_off_for_compatible_endpoint():
    """换用不支持 response_format 的在线端点（vLLM/部分通义）→ 手动关掉 json_mode。"""
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k", "LLM_JSON_MODE": "0"})
    assert cfg["json_mode"] is False


def test_json_mode_override_on_for_local():
    """本地端点若支持 json_object（如 llama.cpp server）→ 手动打开。"""
    cfg = resolve_config(env={"LLM_JSON_MODE": "true"})
    assert cfg["json_mode"] is True


@pytest.mark.parametrize("val", ["1", "yes", "ON", "false", "no", "Off"])
def test_json_mode_accepts_common_boolean_spellings(val):
    cfg = resolve_config(env={"LLM_JSON_MODE": val})
    assert isinstance(cfg["json_mode"], bool)


def test_json_mode_invalid_raises_config_error():
    with pytest.raises(ConfigError, match="LLM_JSON_MODE"):
        resolve_config(env={"LLM_JSON_MODE": "maybe"})


# ---------------- P2：LLM_PRICE 校验 ----------------
def test_price_valid_pair():
    cfg = resolve_config(env={"LLM_PRICE": "1,4"})
    assert cfg["price"] == (1.0, 4.0)


def test_price_bad_value_raises_config_error_not_value_error():
    with pytest.raises(ConfigError, match="LLM_PRICE") as ei:
        resolve_config(env={"LLM_PRICE": "bad"})
    assert "输入价,输出价" in str(ei.value)


def test_price_single_value_rejected():
    """单值会让预算只算输入价、低估成本——明确拒绝而非静默补齐。"""
    with pytest.raises(ConfigError, match="LLM_PRICE"):
        resolve_config(env={"LLM_PRICE": "1"})


def test_price_three_values_rejected():
    with pytest.raises(ConfigError, match="LLM_PRICE"):
        resolve_config(env={"LLM_PRICE": "1,4,9"})


def test_price_negative_rejected():
    with pytest.raises(ConfigError, match="LLM_PRICE"):
        resolve_config(env={"LLM_PRICE": "-1,4"})


def test_price_empty_uses_provider_default():
    assert resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k"})["price"] == (1.0, 4.0)
    assert resolve_config(env={})["price"] == (0.0, 0.0)


# ---------------- 配置解析一致性（2026-11-18 review：裸 ValueError/AttributeError） ----------------
@pytest.mark.parametrize("key,val", [("LLM_CONCURRENCY", "bad"), ("LLM_TIMEOUT", "bad")])
def test_numeric_config_bad_value_raises_config_error(key, val):
    """并发/超时坏值须与 LLM_PRICE 同为 ConfigError（带变量名），不裸 ValueError 崩栈。"""
    with pytest.raises(ConfigError, match=key) as ei:
        resolve_config(env={key: val})
    assert "当前" in str(ei.value)


@pytest.mark.parametrize("key", ["LLM_CONCURRENCY", "LLM_TIMEOUT", "LLM_JSON_MODE", "LLM_PRICE"])
def test_config_non_string_value_raises_config_error(key):
    """编程式调用传非字符串（int）不得裸 AttributeError，须 ConfigError。"""
    with pytest.raises(ConfigError, match=key):
        resolve_config(env={key: 123})


@pytest.mark.parametrize("key,val,expected", [
    ("LLM_CONCURRENCY", "4", 4),
    ("LLM_TIMEOUT", "12.5", 12.5),
])
def test_numeric_config_valid_values(key, val, expected):
    assert resolve_config(env={key: val})[key.lower().replace("llm_", "")] == expected


@pytest.mark.parametrize("key,val", [("LLM_CONCURRENCY", "0"), ("LLM_CONCURRENCY", "-1"),
                                     ("LLM_TIMEOUT", "0"), ("LLM_TIMEOUT", "-5")])
def test_numeric_config_rejects_nonpositive(key, val):
    with pytest.raises(ConfigError, match=key):
        resolve_config(env={key: val})


# ---------------- 脱敏 ----------------
def test_redact_all_kinds():
    t, hit = redact("联系 13812345678 或 me@qq.com，微信 wx_abc123，"       # privacy-scan: allow 合成样例
                    "身份证 11010119900307867X，见 https://blog.example.com/post")
    assert hit
    assert "13812345678" not in t and "me@qq.com" not in t                 # privacy-scan: allow
    assert "wx_abc123" not in t and "11010119900307867X" not in t
    assert "https://" not in t
    assert t.count("[联系方式]") == 5


def test_redact_clean_text_noop():
    t, hit = redact("什么是 HashMap 的扩容机制？默认容量 16。")
    assert not hit and "HashMap" in t


# ---------------- chat：重试退避 / 4xx 快速失败 ----------------
def _fake_post_factory(script):
    """script = [(status, resp), ...]，耗尽后抛 AssertionError。"""
    calls = []

    def fake_post(cfg, payload):
        calls.append(payload)
        if not script:
            raise AssertionError("脚本耗尽")
        return script.pop(0)

    return fake_post, calls


def test_chat_retries_on_529_then_succeeds(monkeypatch):
    import llm_client as lc
    fake, calls = _fake_post_factory([
        (429, {"error": "rate"}),
        (503, {"error": "unavailable"}),
        (200, {"choices": [{"message": {"content": "好"}}], "usage": {"prompt_tokens": 3}}),
    ])
    monkeypatch.setattr(lc, "_post", fake)
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k"})
    r = chat(cfg, "s", "u", retries=3, sleep=0)
    assert r["text"] == "好" and r["tokens"] == 3 and len(calls) == 3


def test_chat_returns_split_token_counts(monkeypatch):
    """F5/bug4：chat 返回 prompt/completion 拆分计数（tokens 保留合计，向后兼容）。"""
    import llm_client as lc
    fake, _ = _fake_post_factory([
        (200, {"choices": [{"message": {"content": "好"}}],
               "usage": {"prompt_tokens": 420, "completion_tokens": 40}}),
    ])
    monkeypatch.setattr(lc, "_post", fake)
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k"})
    r = chat(cfg, "s", "u", retries=1, sleep=0)
    assert r["prompt_tokens"] == 420 and r["completion_tokens"] == 40
    assert r["tokens"] == 460


def test_chat_fast_fail_on_401(monkeypatch):
    import llm_client as lc
    fake, calls = _fake_post_factory([(401, {"error": "bad key"})])
    monkeypatch.setattr(lc, "_post", fake)
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k"})
    with pytest.raises(RuntimeError, match="401"):
        chat(cfg, "s", "u", retries=3, sleep=0)
    assert len(calls) == 1          # 4xx 不重试


def test_chat_json_loose_parse_and_retry_hint(monkeypatch):
    import llm_client as lc
    fake, calls = _fake_post_factory([
        (200, {"choices": [{"message": {"content": "废话前缀 {\"a\": 1} 尾巴"}}]}),
    ])
    monkeypatch.setattr(lc, "_post", fake)
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k"})
    r = chat_json(cfg, "s", "u", retries=1, sleep=0)
    assert r["obj"] == {"a": 1}          # 宽松解析抠出 JSON
    assert cfg["json_mode"] is True and calls[0]["response_format"] == {"type": "json_object"}


def test_chat_json_parse_fail_returns_none(monkeypatch):
    import llm_client as lc
    fake, _ = _fake_post_factory([(200, {"choices": [{"message": {"content": "完全不是JSON"}}]})])
    monkeypatch.setattr(lc, "_post", fake)
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k"})
    r = chat_json(cfg, "s", "u", retries=1, sleep=0)
    assert r["obj"] is None and r.get("parse_failed") is True


# ---------------- 批次三 R4：num_ctx 透传（docs/13 §5 接口契约） ----------------
def test_num_ctx_parsed_and_default_none():
    """LLM_NUM_CTX 正整数透传，缺省 None（不设不透传）。"""
    assert resolve_config(env={"LLM_NUM_CTX": "8192"})["num_ctx"] == 8192
    assert resolve_config(env={})["num_ctx"] is None


def test_num_ctx_invalid_raises_config_error():
    """非法 num_ctx（0/负数/非数字）→ ConfigError，不静默。"""
    for bad in ("0", "-4", "abc"):
        with pytest.raises(ConfigError):
            resolve_config(env={"LLM_NUM_CTX": bad})


def test_num_ctx_only_passed_for_local(monkeypatch):
    """options 是 Ollama 原生字段：online 端点带上去可能直接 400 → 只对 local 透传。"""
    import llm_client as lc
    seen = []

    def fake(cfg, payload):
        seen.append(payload)
        return 200, {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(lc, "_post", fake)
    online = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k"})
    lc.chat({**online, "num_ctx": 8192}, "s", "u", retries=1)
    assert "options" not in seen[-1]
    local = resolve_config(env={})
    lc.chat({**local, "num_ctx": 8192}, "s", "u", retries=1)
    assert seen[-1]["options"] == {"num_ctx": 8192}


def test_no_sleep_after_final_attempt(monkeypatch):
    """末次失败后不再空等退避：retries=2 只该睡 1 次。"""
    import llm_client as lc
    monkeypatch.setattr(lc, "_post", lambda cfg, payload: (0, {"error": "boom"}))
    sleeps: list[float] = []
    monkeypatch.setattr(lc.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(RuntimeError, match="boom"):
        lc.chat(resolve_config(env={}), "s", "u", retries=2, sleep=1.0)
    assert len(sleeps) == 1


def test_chat_payload_num_ctx_passthrough(monkeypatch):
    """num_ctx 设置时 payload 带 options.num_ctx；None 时不带 options 键。"""
    import llm_client as lc
    seen = []

    def fake_post(cfg, payload):
        seen.append(payload)
        return 200, {"choices": [{"message": {"content": "ok"}}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(lc, "_post", fake_post)
    base = {"base": "http://x", "key": "", "model": "m", "timeout": 5, "json_mode": False}
    lc.chat({**base, "num_ctx": 8192}, "s", "u", retries=1)
    assert seen[-1]["options"] == {"num_ctx": 8192}
    lc.chat({**base, "num_ctx": None}, "s", "u", retries=1)
    assert "options" not in seen[-1]
