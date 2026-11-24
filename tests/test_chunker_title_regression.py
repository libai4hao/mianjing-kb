# -*- coding: utf-8 -*-
"""章节标题/真题边界的固定回归（docs/52 §3）。

已覆盖的标题：专题 / 进阶篇 / 面试题汇总（词表 + 三道闸）。
未覆盖的标题（Java基础、JVM篇、项目经验、Spring基础知识、网络编程、集合框架）标 xfail：
扩词表会让 20 篇回归语料的弱题从 477 涨到 490（每条 +9~14），在拿到更好的边界信号前不扩。
"""
import pytest

from mianjing_kb import chunker

# 词表能识别、必须不当题的章节标题
SUPPORTED_HEADINGS = ["数据库专题", "Redis进阶篇", "面试题汇总", "算法专题"]
# 已知未覆盖（扩词会回归，见 docs/52 §3）
KNOWN_HEADINGS = ["Java基础", "JVM篇", "项目经验", "Spring基础知识", "网络编程", "集合框架"]


@pytest.mark.parametrize("heading", SUPPORTED_HEADINGS)
def test_supported_chapter_headings_are_not_emitted(heading):
    md = f"## 1. {heading}\n\n本节介绍相关知识。"
    items = chunker.chunk_v2(md)["items"]
    assert not any(item["q"] == heading for item in items), [x["q"] for x in items]


@pytest.mark.xfail(strict=False, reason="词表未扩：扩了会让 20 篇语料弱题 +13（docs/52 §3）")
@pytest.mark.parametrize("heading", KNOWN_HEADINGS)
def test_known_uncovered_headings(heading):
    md = f"## 1. {heading}\n\n本节介绍相关知识。"
    items = chunker.chunk_v2(md)["items"]
    assert not any(item["q"] == heading for item in items), [x["q"] for x in items]


@pytest.mark.parametrize("heading", [
    "Oracle JDK 和 OpenJDK 的对比",
    "什么是 JVM？",
    "HashMap 扩容机制",
    "线程池的工作原理",
    "Redis 持久化",
    "Java 垃圾回收机制",
    "MySQL 联合索引",
    "Spring Bean 生命周期",
    "HTTP 和 HTTPS 的区别",
    "TCP 三次握手",
])
def test_short_question_titles_are_not_filtered_as_chapters(heading):
    md = f"## 1. {heading}\n\n这是对应的解释内容。"
    items = chunker.chunk_v2(md)["items"]
    assert [item["q"] for item in items] == [heading]
    assert "对应的解释内容" in items[0]["a"]


@pytest.mark.parametrize("q", [
    "为什么要使用Executor 线程池框架",
    "你了解业界哪些大公司的分布式锁框架",
    "为什么要使用并发编程",
    "学习过哪些开源的框架",
])
def test_question_with_section_word_suffix_survives(q):
    """词尾是"框架/编程"，但带提问语义——必须保留（ISSUE-01 的误杀反例）。"""
    assert chunker._is_section_title(q) is False
    items = chunker.chunk_v2(f"## 1. {q}\n\n线程池复用的核心是队列与拒绝策略。")["items"]
    assert [item["q"] for item in items] == [q]
