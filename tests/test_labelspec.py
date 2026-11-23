# -*- coding: utf-8 -*-
"""labelspec（T21）：schema 读取 / 降级 / 字段校验 / 标签同义词归一。"""
import json

import pytest
from mianjing_kb.kb import config, labelspec as ls


def _clear():
    ls.load_schema.cache_clear()
    ls.tag_synonyms.cache_clear()
    ls.recommended_tags.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_cache():
    _clear()
    yield
    _clear()


def test_schema_from_repo_yaml():
    schema = ls.load_schema()
    assert "position" in schema["fields"] and "canonical_q" in schema["fields"]
    assert "后端" in ls.positions() and "前端" in ls.positions()
    assert ls.quality_flags() == ("重复", "残缺", "过时", "广告", "无答案")
    assert ls.qtypes() == ("八股", "算法", "HR", "场景", "项目")
    assert ls.canonical_max_len() == 60
    assert ls.tag_limits() == (4, 24)
    assert "JVM" in ls.recommended_tags()


def test_missing_yaml_degrades_to_defaults(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LABEL_SCHEMA_FILE", tmp_path / "nope.yaml")
    monkeypatch.setattr(config, "TAG_SYNONYMS_FILE", tmp_path / "nope2.yaml")
    monkeypatch.setattr(config, "TAG_VOCAB_FILE", tmp_path / "nope3.yaml")
    assert ls.qtypes() == ("八股", "算法", "HR", "场景", "项目")
    assert ls.tag_synonyms() == {} and ls.recommended_tags() == ()


def test_validate_drops_bad_multi_and_flags_pending():
    spec = ls.validate_label({"topic": "玄学", "qtype": "八股", "difficulty": 3,
                              "position": ["后端", "火星"], "quality": ["重复"],
                              "canonical_q": "", "tags": ["HashMap"]},
                             vocab=("JVM", "Redis", "其他"), q_text="什么是 HashMap？")
    assert spec["topic"] == "其他" and spec["review_state"] == "pending"
    assert spec["position"] == ["后端"] and spec["quality"] == ["重复"]
    assert "position_invalid" in spec["issues"]
    # 词表外必须记住模型原话，人工终审才知道该归哪个桶
    assert "topic_out_of_vocab:玄学" in spec["issues"]
    assert spec["canonical_q"] == "什么是 HashMap？"          # 空 → 回退题干


def test_validate_tags_synonym_dedupe_and_cap():
    spec = ls.validate_label({"topic": "JVM", "qtype": "八股", "difficulty": 2,
                              "tags": ["JVM GC", "jvm gc", "gc", "HashMap", "哈希表"]},
                             vocab=("JVM",))
    tags = spec["tags"]
    assert "JVM" in tags and "垃圾回收" in tags and "集合" in tags
    assert tags.count("JVM") == 1                             # 同义 + 大小写去重
    assert spec["tag_norm_hits"] >= 2
    many = [f"t{i}" for i in range(12)]
    spec2 = ls.validate_label({"topic": "JVM", "qtype": "八股", "difficulty": 2, "tags": many},
                              vocab=("JVM",))
    assert len(spec2["tags"]) == 4


def test_tag_stopwords_drop_dimension_duplicates():
    """tags 里重复 topic/qtype/岗位没有检索价值：传 stopwords 时必须剔除并留计数。"""
    spec = ls.validate_label({"topic": "JVM", "qtype": "八股", "difficulty": 2,
                              "position": ["后端"],
                              "tags": ["JVM", "八股", "后端", "垃圾回收", "类加载"]},
                             vocab=("JVM",), tag_stopwords=frozenset({"JVM", "八股", "后端"}))
    assert spec["tags"] == ["垃圾回收", "类加载"]
    assert spec["tag_dim_dropped"] == 3
    assert "tag_dimension_dropped:3" in spec["issues"]


def test_validate_rejects_bad_qtype_and_difficulty():
    with pytest.raises(ValueError, match="qtype"):
        ls.validate_label({"topic": "JVM", "qtype": "玄学", "difficulty": 3})
    with pytest.raises(ValueError, match="difficulty"):
        ls.validate_label({"topic": "JVM", "qtype": "八股", "difficulty": 9})


def test_dump_multi_roundtrip():
    assert ls.dump_multi(["后端", "前端"]) == json.dumps(["后端", "前端"], ensure_ascii=False)
    assert ls.dump_multi([]) is None and ls.dump_multi(None) is None
