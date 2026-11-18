# -*- coding: utf-8 -*-
"""P1-5：overrides.json 人工覆盖表——classify 最高优先级路径覆盖。"""
import json

import pytest

from mianjing_kb.kb import config
from mianjing_kb.classify import Rules


@pytest.fixture()
def rules_with_override(tmp_path, monkeypatch):
    ov = {
        r"11-面经与真题\11.3-专题总结包\高频面试题-笔记资料01-34\06、为什么在Java面试中一定会深入考察HashMap？.doc":
            {"category": "题库", "topic": "Java基础", "sub_topic": "集合"},
    }
    p = tmp_path / "overrides.json"
    p.write_text(json.dumps(ov, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "OVERRIDES_FILE", p)
    return Rules(), list(ov)[0].replace("\\", "/")


def test_override_wins_over_rules(rules_with_override):
    rules, rel = rules_with_override
    res = rules.classify(rel)
    assert res["category"] == "题库"
    assert res["topic"] == "Java基础"
    assert res["sub_topic"] == "集合"
    assert res["topic_source"] == "override"
    assert res["source_trust"] == "high"


def test_override_key_accepts_backslash(rules_with_override):
    rules, _ = rules_with_override
    rel_bs = r"11-面经与真题\11.3-专题总结包\高频面试题-笔记资料01-34\06、为什么在Java面试中一定会深入考察HashMap？.doc"
    assert rules.classify(rel_bs)["topic_source"] == "override"


def test_non_override_file_uses_rules(rules_with_override):
    rules, _ = rules_with_override
    res = rules.classify("12-综合题库/MySQL面试题大全.pdf")
    assert res["topic_source"] != "override"
