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
