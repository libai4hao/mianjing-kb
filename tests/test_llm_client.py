# -*- coding: utf-8 -*-
"""llm_client 离线测试：配置路由 / 缺 key 指引 / 重试退避 / JSON 宽松解析 / 脱敏。"""

import pytest
from mianjing_kb.llm_client import (ConfigError, chat, chat_json, redact,  # noqa: E402
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


def test_config_thinking_disabled_for_structured_output():
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k",
                              "LLM_THINKING": "disabled"})
    assert cfg["thinking"] == "disabled"


def test_config_thinking_invalid_rejected():
    with pytest.raises(ConfigError, match="LLM_THINKING"):
        resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k",
                            "LLM_THINKING": "maybe"})


def test_config_rpm_parsed_and_invalid_rejected():
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k", "LLM_RPM": "5"})
    assert cfg["rpm"] == 5.0
    assert resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k"})["rpm"] == 0.0
    with pytest.raises(ConfigError, match="LLM_RPM"):
        resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k", "LLM_RPM": "0"})


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
    assert resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k", "LLM_ALLOW_CONTENT_UPLOAD": "1"})["price"] == (1.0, 4.0)
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
    # 技术文档 URL 是题干上下文，不遮罩（2026-11-20 修订）
    assert "https://blog.example.com/post" in t
    assert t.count("[联系方式]") == 4


def test_redact_clean_text_noop():
    t, hit = redact("什么是 HashMap 的扩容机制？默认容量 16。")
    assert not hit and "HashMap" in t


def test_redact_url_keeps_path_but_strips_credentials_and_token():
    """URL 不整段删；但 user:pass@ 和 token/signature query 不能发给模型。"""
    t, hit = redact("参考 https://user:pass@host/docs/a?token=abc&x=1 与 "
                    "https://blog.example.com/post?x=a%2Fb")
    assert hit
    assert "user:pass@" not in t and "token=abc" not in t
    assert "https://host/docs/a?token=REDACTED&x=1" in t
    assert "https://blog.example.com/post?x=a%2Fb" in t


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
    from mianjing_kb import llm_client as lc
    fake, calls = _fake_post_factory([
        (429, {"error": "rate"}),
        (503, {"error": "unavailable"}),
        (200, {"choices": [{"message": {"content": "好"}}], "usage": {"prompt_tokens": 3}}),
    ])
    monkeypatch.setattr(lc, "_post", fake)
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k", "LLM_ALLOW_CONTENT_UPLOAD": "1"})
    r = chat(cfg, "s", "u", retries=3, sleep=0)
    assert r["text"] == "好" and r["tokens"] == 3 and len(calls) == 3


def test_chat_returns_split_token_counts(monkeypatch):
    """F5/bug4：chat 返回 prompt/completion 拆分计数（tokens 保留合计，向后兼容）。"""
    from mianjing_kb import llm_client as lc
    fake, _ = _fake_post_factory([
        (200, {"choices": [{"message": {"content": "好"}}],
               "usage": {"prompt_tokens": 420, "completion_tokens": 40}}),
    ])
    monkeypatch.setattr(lc, "_post", fake)
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k", "LLM_ALLOW_CONTENT_UPLOAD": "1"})
    r = chat(cfg, "s", "u", retries=1, sleep=0)
    assert r["prompt_tokens"] == 420 and r["completion_tokens"] == 40
    assert r["tokens"] == 460


def test_chat_fast_fail_on_401(monkeypatch):
    from mianjing_kb import llm_client as lc
    fake, calls = _fake_post_factory([(401, {"error": "bad key"})])
    monkeypatch.setattr(lc, "_post", fake)
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k", "LLM_ALLOW_CONTENT_UPLOAD": "1"})
    with pytest.raises(RuntimeError, match="401"):
        chat(cfg, "s", "u", retries=3, sleep=0)
    assert len(calls) == 1          # 4xx 不重试


def test_chat_json_loose_parse_and_retry_hint(monkeypatch):
    from mianjing_kb import llm_client as lc
    fake, calls = _fake_post_factory([
        (200, {"choices": [{"message": {"content": "废话前缀 {\"a\": 1} 尾巴"}}]}),
    ])
    monkeypatch.setattr(lc, "_post", fake)
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k", "LLM_ALLOW_CONTENT_UPLOAD": "1"})
    r = chat_json(cfg, "s", "u", retries=1, sleep=0)
    assert r["obj"] == {"a": 1}          # 宽松解析抠出 JSON
    assert cfg["json_mode"] is True and calls[0]["response_format"] == {"type": "json_object"}


def test_chat_json_parse_fail_returns_none(monkeypatch):
    from mianjing_kb import llm_client as lc
    fake, _ = _fake_post_factory([(200, {"choices": [{"message": {"content": "完全不是JSON"}}]})])
    monkeypatch.setattr(lc, "_post", fake)
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k", "LLM_ALLOW_CONTENT_UPLOAD": "1"})
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
    from mianjing_kb import llm_client as lc
    seen = []

    def fake(cfg, payload):
        seen.append(payload)
        return 200, {"choices": [{"message": {"content": "ok"}}]}

    monkeypatch.setattr(lc, "_post", fake)
    online = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k", "LLM_ALLOW_CONTENT_UPLOAD": "1"})
    lc.chat({**online, "num_ctx": 8192}, "s", "u", retries=1)
    assert "options" not in seen[-1]
    local = resolve_config(env={})
    lc.chat({**local, "num_ctx": 8192}, "s", "u", retries=1)
    assert seen[-1]["options"] == {"num_ctx": 8192}


def test_no_sleep_after_final_attempt(monkeypatch):
    """末次失败后不再空等退避：retries=2 只该睡 1 次。"""
    from mianjing_kb import llm_client as lc
    monkeypatch.setattr(lc, "_post", lambda cfg, payload: (0, {"error": "boom"}))
    sleeps: list[float] = []
    monkeypatch.setattr(lc.time, "sleep", lambda s: sleeps.append(s))
    with pytest.raises(RuntimeError, match="boom"):
        lc.chat(resolve_config(env={}), "s", "u", retries=2, sleep=1.0)
    assert len(sleeps) == 1


def test_chat_payload_num_ctx_passthrough(monkeypatch):
    """num_ctx 设置时 payload 带 options.num_ctx；None 时不带 options 键。"""
    from mianjing_kb import llm_client as lc
    seen = []

    def fake_post(cfg, payload):
        seen.append(payload)
        return 200, {"choices": [{"message": {"content": "ok"}}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(lc, "_post", fake_post)
    base = {"base": "http://127.0.0.1:11434/v1", "key": "", "model": "m", "timeout": 5, "json_mode": False}
    lc.chat({**base, "num_ctx": 8192}, "s", "u", retries=1)
    assert seen[-1]["options"] == {"num_ctx": 8192}
    lc.chat({**base, "num_ctx": None}, "s", "u", retries=1)
    assert "options" not in seen[-1]


def test_rate_wait_spaces_requests(monkeypatch):
    """rpm=5 时第二次请求至少等 12 秒；rpm=0 不等待。"""
    from mianjing_kb import llm_client as lc
    sleeps = []
    lc._RATE_NEXT = 0.0
    monkeypatch.setattr(lc.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(lc.time, "sleep", lambda s: sleeps.append(s))
    lc._rate_wait({"rpm": 5})
    lc._rate_wait({"rpm": 5})
    assert sleeps == [12.0]
    sleeps.clear()
    lc._rate_wait({"rpm": 0})
    lc._rate_wait({"rpm": 0})
    assert sleeps == []
    lc._RATE_NEXT = 0.0


def test_chat_payload_thinking_disabled_passthrough(monkeypatch):
    """DeepSeek/Qiniu V4 默认思考会吃光 max_tokens；disabled 必须原样进 payload。"""
    from mianjing_kb import llm_client as lc
    seen = []

    def fake_post(cfg, payload):
        seen.append(payload)
        return 200, {"choices": [{"message": {"content": "ok"}}],
                     "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

    monkeypatch.setattr(lc, "_post", fake_post)
    base = {"base": "http://127.0.0.1:11434/v1", "key": "", "model": "m", "timeout": 5, "json_mode": False}
    lc.chat({**base, "thinking": "disabled"}, "s", "u", retries=1)
    assert seen[-1]["thinking"] == {"type": "disabled"}
    lc.chat({**base, "thinking": None}, "s", "u", retries=1)
    assert "thinking" not in seen[-1]

# ---------------- 密钥脱敏（README 承诺：日志/重试/异常不留 LLM_KEY） ----------------
def test_scrub_secret_masks_config_key_and_patterns():
    from mianjing_kb.llm_client import scrub_secret
    cfg = {"key": "sk-live-abcdef123456"}
    assert scrub_secret("bad key sk-live-abcdef123456 here", cfg) == "bad key [LLM_KEY] here"
    assert "sk-othersecret" not in scrub_secret("got sk-othersecret", cfg)
    assert "Authorization: Bearer [LLM_KEY]" == scrub_secret(
        "Authorization: Bearer abc.def-123456", cfg)


def test_runtime_error_never_contains_key(monkeypatch):
    """重试耗尽的 RuntimeError 必须清洗服务端回显的 key。"""
    from mianjing_kb import llm_client as lc
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "sk-live-abcdef123456", "LLM_ALLOW_CONTENT_UPLOAD": "1"})
    monkeypatch.setattr(lc, "_post", lambda c, p: (
        500, {"error": "invalid Authorization: Bearer sk-live-abcdef123456"}))
    with pytest.raises(RuntimeError) as ei:
        lc.chat(cfg, "s", "u", retries=1)
    assert "sk-live-abcdef123456" not in str(ei.value)
    assert "[LLM_KEY]" in str(ei.value)


def test_online_content_upload_requires_explicit_opt_in(monkeypatch):
    from mianjing_kb import llm_client as lc
    calls = []
    monkeypatch.setattr(lc, "_post", lambda cfg, payload: calls.append(payload) or (200, {}))
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k"})
    assert cfg["allow_content_upload"] is False
    with pytest.raises(ConfigError, match="LLM_ALLOW_CONTENT_UPLOAD"):
        chat(cfg, "system", "question", retries=1)
    assert calls == []


def test_online_content_upload_explicit_opt_in(monkeypatch):
    from mianjing_kb import llm_client as lc
    calls = []
    monkeypatch.setattr(lc, "_post", lambda cfg, payload: calls.append(payload) or
                        (200, {"choices": [{"message": {"content": "ok"}}], "usage": {}}))
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k",
                              "LLM_ALLOW_CONTENT_UPLOAD": "1"})
    assert cfg["allow_content_upload"] is True
    assert chat(cfg, "system", "question", retries=1)["text"] == "ok"
    assert len(calls) == 1


def test_online_chat_redacts_at_final_network_boundary(monkeypatch):
    from mianjing_kb import llm_client as lc
    calls = []
    monkeypatch.setattr(lc, "_post", lambda cfg, payload: calls.append(payload) or
                        (200, {"choices": [{"message": {"content": "ok"}}], "usage": {}}))
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k",
                              "LLM_ALLOW_CONTENT_UPLOAD": "1"})
    phone = "138" + "12345678"
    lc.chat(cfg, "联系人：李四", f"姓名：张三；地址：上海市浦东新区测试路 1 号；手机号：{phone}",
            retries=1)
    sent = calls[0]["messages"]
    wire = "\n".join(item["content"] for item in sent)
    for secret in ("李四", "张三", "上海市浦东新区", phone):
        assert secret not in wire
    assert wire.count("[个人信息]") >= 3


def test_online_chat_retry_keeps_redaction(monkeypatch):
    """JSON 重试会改写 system，不能把未脱敏的原文重新拼回去。"""
    from mianjing_kb import llm_client as lc
    calls = []

    def fake(cfg, payload):
        calls.append(payload)
        if len(calls) == 1:
            return 500, {"error": "temporary"}
        return 200, {"choices": [{"message": {"content": '{"ok": true}'}}], "usage": {}}

    monkeypatch.setattr(lc, "_post", fake)
    cfg = resolve_config(env={"LLM_PROVIDER": "online", "LLM_KEY": "k",
                              "LLM_ALLOW_CONTENT_UPLOAD": "1"})
    phone = "138" + "12345678"
    lc.chat_json(cfg, "联系人：李四", f"姓名：张三；手机号：{phone}", retries=2, sleep=0)
    wire = "\n".join(m["content"] for c in calls for m in c["messages"])
    for secret in ("李四", "张三", phone):
        assert secret not in wire


def test_redact_structured_identity_address_passport_and_bank():
    raw = "姓名：张三；住址：上海市浦东新区示例路 1 号；护照号：E12345678；银行卡号：6222021234567890"
    clean, hit = redact(raw)
    assert hit
    for value in ("张三", "上海市浦东新区", "E12345678", "6222021234567890"):
        assert value not in clean
    assert clean.count("[个人信息]") >= 4


# ---------------- 外发边界按实际 endpoint 判定（provider 不再是通行证） ----------------

def test_is_local_endpoint_only_loopback():
    from mianjing_kb.llm_client import is_local_endpoint as loc
    for base in ("http://127.0.0.1:11434/v1", "http://localhost:1234/v1",
                 "http://127.8.9.10/v1", "http://[::1]:8080/v1"):
        assert loc(base), base
    for base in ("https://example.com/v1", "http://192.168.1.9:11434/v1",
                 "https://api.deepseek.com/v1", "http://10.0.0.5/v1"):
        assert not loc(base), base


def test_invalid_base_is_rejected():
    for bad in ("ftp://127.0.0.1/v1", "http://user:pass@example.com/v1",
                "http:///v1", "127.0.0.1:11434/v1"):
        with pytest.raises(ConfigError, match="LLM_BASE"):
            resolve_config(env={"LLM_BASE": bad, "LLM_PROVIDER": "local"})


def test_local_provider_remote_base_without_opt_in_is_blocked(monkeypatch):
    """P1：local + 远程 LLM_BASE 曾经绕过授权检查，这里必须一个请求都不发。"""
    from mianjing_kb import llm_client as lc
    calls = []
    monkeypatch.setattr(lc, "_post", lambda cfg, payload: calls.append(payload) or (200, {}))
    cfg = resolve_config(env={"LLM_PROVIDER": "local",
                              "LLM_BASE": "https://example.com/v1",
                              "LLM_KEY": "k"})
    assert cfg["local_endpoint"] is False and cfg["allow_content_upload"] is False
    with pytest.raises(ConfigError, match="LLM_ALLOW_CONTENT_UPLOAD"):
        chat(cfg, "system", "question", retries=1)
    assert calls == []


def test_local_provider_remote_base_with_opt_in_redacts(monkeypatch):
    from mianjing_kb import llm_client as lc
    calls = []
    monkeypatch.setattr(lc, "_post", lambda cfg, payload: calls.append(payload) or
                        (200, {"choices": [{"message": {"content": "ok"}}], "usage": {}}))
    phone = "138" + "12345678"
    cfg = resolve_config(env={"LLM_PROVIDER": "local", "LLM_BASE": "https://example.com/v1",
                              "LLM_KEY": "k", "LLM_ALLOW_CONTENT_UPLOAD": "1"})
    assert chat(cfg, "联系人：李四", f"姓名：张三；手机号：{phone}", retries=1)["text"] == "ok"
    wire = "\n".join(m["content"] for m in calls[0]["messages"])
    for secret in ("李四", "张三", phone):
        assert secret not in wire
    assert "[个人信息]" in wire


def test_post_refuses_remote_without_opt_in_even_if_chat_skipped():
    """网络层最后一道闸：直接调 _post 也发不出去，密钥不会随请求泄露。"""
    from mianjing_kb import llm_client as lc
    cfg = resolve_config(env={"LLM_PROVIDER": "local", "LLM_BASE": "https://example.com/v1",
                              "LLM_KEY": "sk-live-abcdef123456"})
    with pytest.raises(ConfigError, match="LLM_ALLOW_CONTENT_UPLOAD"):
        lc._post(cfg, {"model": "m", "messages": []})


def test_local_loopback_needs_no_opt_in(monkeypatch):
    from mianjing_kb import llm_client as lc
    calls = []
    monkeypatch.setattr(lc, "_post", lambda cfg, payload: calls.append(payload) or
                        (200, {"choices": [{"message": {"content": "ok"}}], "usage": {}}))
    cfg = resolve_config(env={})
    assert cfg["local_endpoint"] is True
    assert chat(cfg, "s", "u", retries=1)["text"] == "ok"
    assert len(calls) == 1 and calls[0]["messages"][1]["content"].endswith("/no_think")
