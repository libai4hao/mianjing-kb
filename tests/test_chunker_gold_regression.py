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
    # 真题"说一下Java对象的创建过程"应被捞回，但题干不能粘着说明尾巴。
    obj_q = [it for it in items if "对象的创建过程" in it["q"]]
    assert obj_q, "2.4.2 真题应被捞回而不是整节丢弃"
    assert "下图便是" not in obj_q[0]["q"], "说明句不应混进题干"
    assert "我建议" not in obj_q[0]["q"], "说明句不应混进题干"


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


# ---------------------------------------------------------------- T4 / F4
# 075 分⻚/分段：PDF 粘连“节标题+面试官问句”整行被丢弃（真问题丢失），
# 随后 “## 2. 区别 ：” 因顺延编号 bypass 成碎片题，偷走答案后半。

DOC_INTERVIEWER_GLUED = """# ⾯试题

1. 什么是虚拟内存？

虚拟内存是操作系统提供的⼀种内存抽象。

3.4 分⻚机制和分段机制的共同点和区别! ⾯试官 ： 分⻚机制和分段机制有哪些共同点和区别呢？

分⻚和分段都是内存管理⽅式。

## 1. 共同点 ：

都是连续的。

## 2. 区别 ：

⻚的⼤⼩是固定的，由操作系统决定；⽽段的⼤⼩不固定。
"""


def test_interviewer_glued_question_recovered():
    items = chunker.chunk_v2(DOC_INTERVIEWER_GLUED)["items"]
    iv = [it for it in items
          if "共同点和区别" in it["q"] and "⾯试官" not in it["q"]]
    assert iv, "粘连行里的面试官真问题应被剥出成题"
    a = iv[0]["a"]
    assert "都是连续的" in a, "共同点内容应留在该题答案"
    assert "⼤⼩是固定的" in a, "区别内容应留在该题答案（不被碎片题偷走）"
    assert not any(it["q"].strip() in ("区别 ：", "区别:", "区别") for it in items), \
        "碎片标签不应成题"
    vm = [it for it in items if "虚拟内存" in it["q"]]
    assert vm, "上一题应保留"
    assert "分⻚" not in vm[0]["a"], "粘连行不应被吸入上一题答案"


# ---------------------------------------------------------------- T5 / F5
# 075 S0517/S0533：无问句语义的 Markdown 编号标题（学习建议列表项、
# Demo 节头+说明句）被当题；047 S0340：“准备步骤：”引导的编号列表项
# 被当题；028 S0348：叙述行“1、上来照例还是问了问项目：”被当题。

DOC_HEADING_SKILL_ITEM = """# JavaGuide

## 1. Java 语⾔有哪些特点?

Java 语⾔的特点有封装、继承、多态。

## 5. 熟悉如何优化 Java 代码、有写出质量更⾼的代码的意识;

写代码时要注意可读性和可维护性。
"""

DOC_HEADING_DEMO_SECTION = """# JavaGuide

## 5. ⼀个简单的线程池Demo: Runnable + ThreadPoolExecutor为了让⼤家更清楚上⾯的⾯试题中的⼀些概念，我写了⼀个简单的线程池 Demo。

```java
public class MyRunnable implements Runnable {
    public void run() { }
}
```

## 6. 线程池原理分析

线程池通过 corePoolSize 等参数控制线程数量。
"""

DOC_HEADING_SHORT_NOUN = """# ⾯试题

## 11. Oracle JDK 和 OpenJDK 的对⽐

OpenJDK 是开源参考实现，Oracle JDK 提供商业⽀持。
"""

DOC_LIST_LEAD_STEPS = """# ⾯经

?我跟大家介绍一下一般面试前的准备步骤：

1、了解公司（公司历史、产品、⽂化）；

2、了解职位（⼯作职责、⼯作范畴、⼯作关系）。

3、你为什么想加⼊我们公司？

因为我认同贵公司的产品。
"""

DOC_NARRATIVE_LEAD = """# ⾯经

1、上来照例还是问了问项目：

答：介绍了⼀下项⽬的架构。

4、字符集和编码的区别？

答：字符集是⼀套符号的集合，编码是符号到字节的映射。
"""


def test_skill_item_heading_not_question():
    items = chunker.chunk_v2(DOC_HEADING_SKILL_ITEM)["items"]
    assert not any("熟悉如何优化" in it["q"] for it in items), \
        "学习建议列表项（技能罗列，无问句语义）不应成题（金标 S0517）"
    body = [it for it in items if "Java 语⾔有哪些特点" in it["q"]]
    assert body, "正文真题应保留"


def test_demo_section_heading_not_question():
    items = chunker.chunk_v2(DOC_HEADING_DEMO_SECTION)["items"]
    assert not any("线程池Demo" in it["q"] for it in items), \
        "节头+说明句的编号标题不应成题（金标 S0533）"
    assert not any("MyRunnable" in it["a"] for it in items), \
        "节内代码应归 leftovers，不挂在任何题的答案上"


def test_short_noun_heading_still_question():
    items = chunker.chunk_v2(DOC_HEADING_SHORT_NOUN)["items"]
    jdk = [it for it in items if "OpenJDK 的对⽐" in it["q"]]
    assert jdk, "短名词短语编号标题（030 型）仍应成题"
    assert "开源" in jdk[0]["a"]


def test_steps_list_lead_arms_list():
    items = chunker.chunk_v2(DOC_LIST_LEAD_STEPS)["items"]
    assert not any("了解公司" in it["q"] for it in items), \
        "“准备步骤：”引导的列表项不应成题（金标 S0340）"
    assert not any("了解职位" in it["q"] for it in items)
    why = [it for it in items if "为什么想加⼊" in it["q"]]
    assert why, "引导列表后的真问句应成题"


def test_narrative_lead_not_question():
    items = chunker.chunk_v2(DOC_NARRATIVE_LEAD)["items"]
    assert not any("问了问项目" in it["q"] for it in items), \
        "叙述行（问了问项目：）不应成题（金标 S0348）"
    enc = [it for it in items if "字符集和编码" in it["q"]]
    assert enc, "后面的真问句应成题"
    assert "符号的集合" in enc[0]["a"]


# --------------------------------------------------- 原始文档小规模测试 / R1-R3
# 2026-11-22 用 5 篇 raw_docs 实跑发现（见 .review_mianjing/原始文档小规模测试报告.md）：
# 答案正文行/回答行/叙述行被独立问句通道误当成题。

DOC_METHOD_CALL_ANSWER = """# Java题

43. 线程的 run() 和 start() 有什么区别？

start() 方法用于启动线程，run() 方法用于执行线程的运行时代码。run() 可以重复调用，而 start()

只能调用一次。

44. 创建线程池有哪几种方式？

线程池创建有七种方式。
"""

DOC_ANSWER_LINE = """# 面经

1、上来照例还是问了问项目：

答：我介绍了自己的项目背景，项目的整个流程，由于是一个多人合作的项目，还介绍了自己负责项目的哪个模块。

2、项目亮点

我介绍了项目的亮点。

4、字符集和编码的区别？

字符集是符号的集合。
"""

DOC_NARRATIVE_SEMICOLON = """# 面经

三面

1、三面也是上来聊了聊项目以及项目亮点；

2、然后就出了一道场景图，就是对于一个上线的系统，它会周期性地出现卡顿，这是怎么回事，让我分析原因.
"""


def test_method_call_answer_not_question():
    items = chunker.chunk_v2(DOC_METHOD_CALL_ANSWER)["items"]
    run_start = [it for it in items if "run() 和 start()" in it["q"]]
    assert run_start, "真问题应成题"
    assert "只能调用一次" in run_start[0]["a"], \
        "答案正文（以方法调用 start() 结尾）应挂到该题，不能被当成独立题"
    assert not any(it["q"].startswith("start() 方法用于启动线程") for it in items), \
        "以方法调用结尾的答案行不是选择题空括号，不应成题"


def test_answer_line_not_question():
    items = chunker.chunk_v2(DOC_ANSWER_LINE)["items"]
    assert not any(it["q"].startswith("答：") for it in items), \
        "“答：…”回答行不应被当成独立问句题"
    enc = [it for it in items if "字符集和编码" in it["q"]]
    assert enc, "后面的真问句仍应成题"
    assert "符号的集合" in enc[0]["a"]


def test_narrative_semicolon_not_question():
    items = chunker.chunk_v2(DOC_NARRATIVE_SEMICOLON)["items"]
    assert not any("聊了聊项目" in it["q"] for it in items), \
        "“聊了聊项目；”叙述行（；结尾）不应成题，与 S0348（：结尾）同口径"
