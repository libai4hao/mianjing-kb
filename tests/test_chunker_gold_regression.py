# -*- coding: utf-8 -*-
"""金标对账回归测试（2026-11-22 code review 5 类 Must fix 的最小复现）。

来源：5 篇金标原文（047/074/075/009/028）与 20 条人工金标对账中仍复现的错误，
fixture 均按真实文档形态裁剪。每类错误对应一个 F 编号（见
docs/superpowers/plans/2026-11-22-chunker-gold-fixes.md）。
"""
from mianjing_kb import chunker


# ---------------------------------------------------------------- T1 / F3
# 075 S0549/S0537：PDF 行粘连的层级节标题混入上一题答案（E_ANSWER_BLEED）。

DOC_GLUED_MIDLINE = """# 面试题

1. redis 过期 key 的删除策略是什么？

redis 采用定期删除和惰性删除结合的策略。

关于Redis事务不是原⼦性问题 ）4.2.9 缓存雪崩和缓存穿透问题解决方案缓存雪崩

缓存雪崩指缓存同一时间大面积失效。

2. 什么是缓存穿透？

缓存穿透指查询根本不存在的数据。
"""

DOC_GLUED_LINESTART = """# 面试题

1. 为什么要将永久代(PermGen)替换为元空间(MetaSpace)呢?

永久代存放类元数据，字符串常量池在 JDK1.7 移到了堆。
2.4.2 说⼀下Java对象的创建过程下图便是 Java 对象的创建过程，我建议你先了解每一步。
类加载检查通过以后对象就可以正常使用了。
"""

DOC_HIER_QUESTION = """# 面试题

1. 什么是 JVM？

JVM 是 Java 虚拟机。
2.4.6 HashMap 的底层实现原理
HashMap 底层是数组加链表加红黑树。
"""


def test_glued_hier_midline_bleed():
    items = chunker.chunk_v2(DOC_GLUED_MIDLINE)["items"]
    redis_q = [it for it in items if "过期" in it["q"]]
    assert redis_q, "redis 过期 key 题应存在"
    a = redis_q[0]["a"]
    assert "4.2.9" not in a, "行中粘连的层级节号不应混入答案"
    assert "缓存雪崩" not in a, "行中粘连的下一节内容不应混入答案"


def test_glued_hier_linestart_bleed():
    items = chunker.chunk_v2(DOC_GLUED_LINESTART)["items"]
    permgen_q = [it for it in items if "永久代" in it["q"]]
    assert permgen_q, "永久代题应存在"
    a = permgen_q[0]["a"]
    assert "2.4.2" not in a, "行首粘连的层级节号不应混入答案"
    assert "类加载检查" not in a, "下一节内容不应混入答案"


def test_hier_question_still_binds():
    items = chunker.chunk_v2(DOC_HIER_QUESTION)["items"]
    hier_q = [it for it in items if "HashMap" in it["q"]]
    assert hier_q, "层级号开头的标题语义题不应被吞进上一题答案"
    assert "红黑树" in hier_q[0]["a"]


# ---------------------------------------------------------------- T2 / F1
# 075 S0496：目录自带 "## N." 条目，_toc_mask 在首个 H12 处截断，
# 其余 ~900 行目录裸奔，条目成题（"7. 线程池原理分析" a 空）。

DOC_TOC_H12_ENTRIES = """# JavaGuide

⽬录

⼀ ⾯向对象和⾯向过程的区别

1.1 什么是⾯向对象

1.2 封装继承多态

## 1. ⾯向对象和⾯向过程的区别

## 2. Java 语⾔有哪些特点?

## 7. 线程池原理分析

2.4 JVM

2.4.1 介绍下 Java 内存区域(运⾏时数据区)程序计数器

## 1. ⾯向对象和⾯向过程的区别

⾯向对象是把数据和对数据的操作封装在⼀起，⽐⾯向过程更易维护、易复⽤。

## 2. Java 语⾔有哪些特点?

Java 语⾔的特点有封装、继承、多态。
"""


def test_toc_h12_entries_masked():
    items = chunker.chunk_v2(DOC_TOC_H12_ENTRIES)["items"]
    qs = [it["q"].strip() for it in items]
    assert not any("线程池原理分析" in q for q in qs), \
        "目录条目不应成题（金标 S0496）"
    assert not any("介绍下 Java 内存区域" in q for q in qs), \
        "目录层级条目不应成题"
    assert not any("程序计数器" in it["a"] for it in items), \
        "目录条目不应串入任何答案"
    body_q = [it for it in items if "Java 语⾔有哪些特点" in it["q"]]
    assert body_q, "正文题应保留"
    assert "封装" in body_q[0]["a"], "正文题答案应完整"


# ---------------------------------------------------------------- T3 / F2
# 评审评论2：同题干去重不吞目录幽灵。TOC 逃逸条目（答案=目录邻行垃圾，
# 如“运⾏时常量池\\n直接内存”）与正文真身并存时，去重应保留实质答案。

DOC_GHOST_DUP = """# ⾯试题

1. 什么是 JVM？

运⾏时常量池
直接内存

2. 什么是 JVM？

JVM 是 Java 虚拟机，负责把字节码解释或编译成机器码执⾏。
"""

DOC_GHOST_DUP_REVERSE = """# ⾯试题

1. 什么是 JVM？

JVM 是 Java 虚拟机，负责把字节码解释或编译成机器码执⾏。

2. 什么是 JVM？

运⾏时常量池
直接内存
"""


def test_ghost_answer_replaced_by_substantive():
    items = chunker.chunk_v2(DOC_GHOST_DUP)["items"]
    jvm = [it for it in items if "JVM" in it["q"]]
    assert len(jvm) == 1, "幽灵答案与实质答案同题干时应合并为一条"
    assert "虚拟机" in jvm[0]["a"], "应保留实质答案"
    assert "运⾏时常量池" not in jvm[0]["a"], "目录邻行垃圾不应留为答案"


def test_substantive_answer_not_replaced_by_ghost():
    items = chunker.chunk_v2(DOC_GHOST_DUP_REVERSE)["items"]
    jvm = [it for it in items if "JVM" in it["q"]]
    assert len(jvm) == 1, "实质答案在前、幽灵在后时应丢弃幽灵"
    assert "虚拟机" in jvm[0]["a"], "实质答案应保留"
    assert "运⾏时常量池" not in jvm[0]["a"], "幽灵答案不应替换实质答案"
