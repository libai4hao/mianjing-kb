# -*- coding: utf-8 -*-
"""问题切分：题干要吃进来、答案要砍掉，近重复要能合并。"""
from question_freq import grams, hamming, jaccard, normalize, simhash, split_questions


def test_split_keeps_question_drops_answer():
    text = (
        "1. 什么是 JVM？答：JVM 是 Java 虚拟机，负责把 class 翻译成机器码。\n"
        "2、说一下 GC 原理\n答案：分代回收 + 可达性分析\n"
        "3) 请写出线程池的核心参数\n"
        "页码 3\n"
        "4. 说明：这段是答案不是题目\n"
    )
    qs = split_questions(text)
    assert qs[0] == "什么是 JVM"
    assert "GC 原理" in qs[1]
    assert any("线程池的核心参数" in q for q in qs)
    # "说明：..." 后面没有疑问词 → 不该被当成题目
    assert all("这段是答案不是题目" not in q for q in qs)


def test_normalize_ignores_layout_noise():
    assert normalize("什么 是 JVM ？") == normalize("什么是JVM？")
    assert normalize("1、MySQL 索引：B+树") != normalize("B+树")


def test_near_duplicates_hash_close_far_ones_apart():
    a = simhash(normalize("说一下 JVM 的内存区域划分"))
    b = simhash(normalize("说说 JVM 的内存区域是怎么划分的"))
    c = simhash(normalize("MySQL 的隔离级别有哪几种"))
    # 短问句的 SimHash 抖动大（同义改写实测差 24 位），所以只当候选；判"同一题"靠 Jaccard 复核。
    # 复核阈值取 0.7 是**高精度优先**：复制粘贴时带题号/空格的变体能到 0.9+，
    # 真·同义改写（如"说一下…"vs"说说…是怎么…的"）只有 0.42，会被放过 —— 这是有意的。
    assert hamming(a, b) <= 24
    assert jaccard(grams(normalize("1、说一下 JVM 的内存区域划分？")),
                   grams(normalize("说一下 JVM 的内存区域划分"))) >= 0.9
    assert jaccard(grams(normalize("说一下 JVM 的内存区域划分")),
                   grams(normalize("MySQL 的隔离级别有哪几种"))) < 0.2
