# -*- coding: utf-8 -*-
"""probe_provider 的离线单测：不联网，用假的 post 覆盖 4 项探针与兼容性开关。

覆盖点：
  1. build_chat_payload —— SCNet 的 system 独立字段 / max_tokens 必填；DeepSeek 的 messages system
  2. parse_json_loose —— 带围栏、带前后缀、嵌套、坏 JSON
  3. estimate_cost —— 价格换算
  4. probe —— P1..P4 全通过 / system 不生效 / json 不生效 / 缺 key / 网络错
  5. resolve_key —— --key 优先于环境变量
"""
from __future__ import annotations


from mianjing_kb import probe_provider as pp  # noqa: E402


# ---------- 1. payload 兼容性 ----------
def test_scnet_payload_uses_system_field_and_max_tokens():
    p = pp.build_chat_payload(pp.PROVIDERS["scnet"], "deepseek-v4.1-flash",
                              "系统提示", "用户问题", max_tokens=64)
    assert p["system"] == "系统提示"
    assert p["messages"] == [{"role": "user", "content": "用户问题"}]
    assert p["max_tokens"] == 64
    assert "response_format" not in p


def test_deepseek_payload_keeps_system_message():
    p = pp.build_chat_payload(pp.PROVIDERS["deepseek"], "deepseek-flash",
                              "系统提示", "用户问题", max_tokens=64)
    assert "system" not in p
    assert p["messages"][0] == {"role": "system", "content": "系统提示"}
    assert p["messages"][1]["role"] == "user"


def test_json_mode_adds_response_format():
    p = pp.build_chat_payload(pp.PROVIDERS["scnet"], "m", "s", "u", json_mode=True)
    assert p["response_format"] == {"type": "json_object"}


# ---------- 2. JSON 抠取 ----------
def test_parse_json_plain():
    assert pp.parse_json_loose('{"qtype":"八股"}') == {"qtype": "八股"}


def test_parse_json_with_fence():
    txt = "```json\n{\"qtype\":\"算法\",\"difficulty\":4}\n```"
    assert pp.parse_json_loose(txt)["difficulty"] == 4


def test_parse_json_with_prefix_and_suffix():
    txt = '好的，结果如下：{"a":1} 希望有帮助'
    assert pp.parse_json_loose(txt) == {"a": 1}


def test_parse_json_nested_braces():
    txt = 'prefix {"a":{"b":2}} suffix'
    assert pp.parse_json_loose(txt)["a"]["b"] == 2


def test_parse_json_broken_returns_none():
    assert pp.parse_json_loose('{"a":') is None
    assert pp.parse_json_loose("") is None
    assert pp.parse_json_loose("[1,2]") is None      # 只接受对象


# ---------- 3. 成本 ----------
def test_estimate_cost():
    assert pp.estimate_cost(1_000_000, 1_000_000, (1.0, 4.0)) == 5.0
    assert pp.estimate_cost(420, 40, (1.0, 4.0)) == 0.0006
    assert pp.estimate_cost(1000, 1000, (0.0, 0.0)) == 0.0


# ---------- 4. key 解析 ----------
def test_resolve_key_prefers_arg(monkeypatch):
    monkeypatch.setenv("SCNET_KEY", "from-env")
    assert pp.resolve_key(pp.PROVIDERS["scnet"], "from-arg") == "from-arg"


def test_resolve_key_env_fallback(monkeypatch):
    monkeypatch.setenv("SCNET_KEY", "from-env")
    assert pp.resolve_key(pp.PROVIDERS["scnet"], None) == "from-env"


def test_resolve_key_missing(monkeypatch):
    monkeypatch.delenv("SCNET_KEY", raising=False)
    assert pp.resolve_key(pp.PROVIDERS["scnet"], None) == ""


# ---------- 5. probe 全链路（假 post） ----------
def _fake_ok(url, payload, key, timeout=30.0):
    if url.endswith("/models"):
        return 200, {"data": [{"id": "deepseek-v4.1-flash"}, {"id": "qwen-plus"}]}
    system = ""
    if payload and payload.get("system"):
        system = payload["system"]
    elif payload and payload["messages"][0]["role"] == "system":
        system = payload["messages"][0]["content"]
    user = payload["messages"][-1]["content"]
    if "暗号" in user or "暗号" in system:
        content = pp.DARK_MARK if pp.DARK_MARK in system else "我不知道"
    elif payload.get("response_format"):
        content = '{"qtype":"八股","difficulty":3}'
    else:
        content = "好"
    return 200, {"choices": [{"message": {"content": content}}],
                 "usage": {"prompt_tokens": 11, "completion_tokens": 2}}


def test_probe_all_green(monkeypatch):
    monkeypatch.setattr(pp, "post", _fake_ok)
    r = pp.probe("scnet", "deepseek-v4.1-flash", key="k")
    assert r["p1_models"]["ok"] and r["p1_models"]["model_in_list"]
    assert r["p2_text"]["ok"] and r["p2_text"]["tokens"] == [11, 2]
    assert r["p3_system"]["ok"]
    assert r["p4_json"]["ok"] and r["p4_json"]["parsed"]["qtype"] == "八股"
    assert r["verdict"]["可用"] is True


def test_probe_detects_system_not_honored(monkeypatch):
    def fake(url, payload, key, timeout=30.0):
        if url.endswith("/models"):
            return 200, {"data": [{"id": "m"}]}
        # provider 忽略独立 system 字段：回固定话术
        return 200, {"choices": [{"message": {"content": '{"qtype":"八股"}'}}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 5}}
    monkeypatch.setattr(pp, "post", fake)
    r = pp.probe("scnet", "m", key="k")
    assert r["p3_system"]["ok"] is False
    assert "system_field" in r["verdict"]["需要改配置"]
    assert r["verdict"]["可用"] is False


def test_probe_detects_json_not_honored(monkeypatch):
    def fake(url, payload, key, timeout=30.0):
        if url.endswith("/models"):
            return 200, {"data": [{"id": "m"}]}
        if "暗号" in payload["messages"][-1]["content"]:
            return 200, {"choices": [{"message": {"content": pp.DARK_MARK}}]}
        if payload.get("response_format"):
            return 200, {"choices": [{"message": {"content": "抱歉，我不能输出 JSON。"}}]}
        return 200, {"choices": [{"message": {"content": "好"}}]}
    monkeypatch.setattr(pp, "post", fake)
    r = pp.probe("deepseek", "m", key="k")
    assert r["p4_json"]["ok"] is False
    assert "json_mode" in r["verdict"]["需要改配置"]


def test_probe_missing_key():
    r = pp.probe("scnet", "m", key="")
    assert "缺 key" in r["fatal"]


def test_probe_network_error(monkeypatch):
    monkeypatch.setattr(pp, "post", lambda *a, **k: (0, {"error": "URLError: timeout"}))
    r = pp.probe("deepseek", "m", key="k")
    assert r["p1_models"]["ok"] is False
    assert r["p2_text"]["status"] == 0
    assert r["verdict"]["可用"] is False


def test_local_provider_skips_json():
    r = pp.probe("local", "qwen3-4b", key="", skip_json=False)
    # local 的 json_mode=unsupported，应为 skipped；无 key 也允许（非 local 才要求 key）
    assert r.get("p4_json", {}).get("skipped") is True
