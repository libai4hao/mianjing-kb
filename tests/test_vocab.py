# -*- coding: utf-8 -*-
"""vocab 封闭词表（批次三 R2）：yaml 三段并集 / 剔除未分类 / 兜底其他 / 缺失降级。"""

import pytest
from mianjing_kb.kb import config  # noqa: E402
from mianjing_kb.kb.vocab import OTHER_TOPIC, topic_vocab  # noqa: E402

YAML = """
version: 1
rules:
  - {re: "Redis", topic: Redis}
  - {re: "MySQL", topic: MySQL}
title_only_rules:
  - {re: "Java", topic: Java基础}
folder_topics:
  07-消息队列与分布式: 消息队列
  12-综合题库: 未分类
"""


@pytest.fixture()
def vocab_yaml(tmp_path, monkeypatch):
    p = tmp_path / "topic_rules.yaml"
    p.write_text(YAML, encoding="utf-8")
    monkeypatch.setattr(config, "TOPIC_RULES_FILE", p)
    topic_vocab.cache_clear()
    yield p
    topic_vocab.cache_clear()


def test_topic_vocab_from_yaml(vocab_yaml):
    """三段 topic 并集，剔除未分类，补"其他"，排序稳定（prompt 枚举可复现）。"""
    v = topic_vocab()
    assert isinstance(v, tuple)
    assert {"Redis", "MySQL", "Java基础", "消息队列", OTHER_TOPIC} <= set(v)
    assert "未分类" not in v          # 待复核标记不是主题
    assert list(v) == sorted(v)


def test_topic_vocab_missing_file_degrades(tmp_path, monkeypatch):
    """yaml 缺失 → 空元组宽松降级（G6/label_one 走旧口径，不因依赖崩）。"""
    monkeypatch.setattr(config, "TOPIC_RULES_FILE", tmp_path / "nope.yaml")
    topic_vocab.cache_clear()
    assert topic_vocab() == ()
    topic_vocab.cache_clear()


def test_topic_vocab_structural_corruption_degrades(tmp_path, monkeypatch):
    """评审 Minor#1：yaml 结构损坏（顶层 list / folder_topics 非 dict）→ 空元组不裸崩。"""
    p1 = tmp_path / "broken1.yaml"
    p1.write_text("- a\n- b\n", encoding="utf-8")      # 顶层是 list → data.get 会 AttributeError
    monkeypatch.setattr(config, "TOPIC_RULES_FILE", p1)
    topic_vocab.cache_clear()
    assert topic_vocab() == ()
    p2 = tmp_path / "broken2.yaml"
    p2.write_text("rules: [Redis]\nfolder_topics: [7, 12]\n", encoding="utf-8")
    monkeypatch.setattr(config, "TOPIC_RULES_FILE", p2)
    topic_vocab.cache_clear()
    assert topic_vocab() == ()
    topic_vocab.cache_clear()
