# -*- coding: utf-8 -*-
"""F6：短题拉丁实体闸门——不同英文实体不得靠 Jaccard 并成一题。"""
from mianjing_kb.question_freq import cluster, short_latin_conflict


def test_conflict_unit_cases():
    assert short_latin_conflict("什么是springmvc", "什么是spring")          # 0.727 过合用例
    assert short_latin_conflict("java和c++的区别", "java和c#的区别")
    assert short_latin_conflict("spring", "springboot")                     # 更短的拉丁实体
    # 实体相同 / 无拉丁词 / 长题：都不算冲突
    assert not short_latin_conflict("什么是spring", "什么是spring?")
    assert not short_latin_conflict("什么是jvm", "什么是jvm内存模型")
    assert not short_latin_conflict("==和equals的区别", "==和equals的区别是什么")
    assert not short_latin_conflict(
        "springboot自动装配的原理是什么请详细说明", "springmvc的请求处理流程是怎样的请详细说明")


def test_cluster_does_not_merge_short_entity_variants():
    """文本对 Jaccard 0.727 ≥ 0.7，旧口径会并；闸门必须把它们留在两簇。"""
    texts = ["什么是spring", "什么是springmvc"]
    dsu = cluster([(t, 0) for t in texts], max_dist=24, min_jac=0.7)
    assert dsu.find(0) != dsu.find(1)


def test_cluster_still_merges_same_entity_rewrites():
    texts = ["什么是spring框架", "什么是spring框架?"]
    dsu = cluster([(t, 0) for t in texts], max_dist=24, min_jac=0.7)
    assert dsu.find(0) == dsu.find(1)
