# -*- coding: utf-8 -*-
"""llm_client 离线测试：配置路由 / 缺 key 指引 / 重试退避 / JSON 宽松解析 / 脱敏。"""
import json
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


# ---------------- 脱敏 ----------------
def test_redact_all_kinds():
    t, hit = redact("联系 13812345678 或 me@qq.com，微信 wx_abc123，"
                    "身份证 11010119900307867X，见 https://blog.example.com/post")
    assert hit
    assert "13812345678" not in t and "me@qq.com" not in t
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
