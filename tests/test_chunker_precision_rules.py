# -*- coding: utf-8 -*-
"""切题精度规则回归（docs/61）：陈述句/结构标签/感叹式问号不成题，真题不误伤。

每条规则都带正反两例：反例是曾经成题的缺陷形态，正例是形态相近的真题。
"""
from mianjing_kb import chunker


def _items(md: str):
    return [(it["q"].strip(), it["a"]) for it in chunker.chunk_v2(md)["items"]]


def _stems(md: str):
    return [q for q, _ in _items(md)]


# ---- 句中问号：编号行没有空行前置也是新题 ----

def test_inline_question_mark_is_question_intent():
    assert chunker._has_question_intent(
        "一个类的构造方法的作用是什么？若一个类没有声明构造方法，则执行构造方法时会报错") is True
    assert chunker._has_question_intent("HashMap的底层实现原理？（高频问题）") is True


def test_inline_question_mark_starts_new_question():
    md = (
        "1. 什么是封装？\n\n"
        "封装是把数据和方法包装在一起。\n\n"
        "2. 一个类的构造方法的作用是什么？若一个类没有声明构造方法，则执行构造方法时会报错。\n\n"
        "构造方法用于初始化对象。\n"
    )
    stems = _stems(md)
    assert len(stems) == 2
    assert stems[1].startswith("一个类的构造方法的作用是什么？")
    # 问号后的尾巴可能是同一行的第二个问句（“A？B？”），不切，整行留在题干
    assert "若一个类没有声明构造方法" in stems[1]


def test_two_questions_in_one_line_are_both_kept():
    stem = "Math.round(11.5) 等于多少？Math.round(-11.5) 等于多少"
    md = f"1. {stem}\n\n前者是 12，后者是 -11。\n"
    assert _stems(md) == [stem]


def test_answer_lead_tail_moves_to_answer():
    md = "1. 查看文件内容有哪些命令可以使用？答案：cat、more、less\n"
    q, a = _items(md)[0]
    assert q == "查看文件内容有哪些命令可以使用？"
    assert a.startswith("答案：cat")


def test_rhetorical_question_mark_is_not_a_question():
    """“单点故障？不存在的！”是感叹，不是出题。"""
    s = "单点故障？不存在的！除非redis 集群或者所有server 全挂了。"
    assert chunker._has_question_intent(s) is False
    assert chunker._is_question_heading(s) is False
    # 疑问代词在问号前：仍是问句
    assert chunker._is_question_heading("MVC是什么？MVC设计模式的好处有哪些") is True
    # 问答同行 / 短题干 + 问句尾巴：问号前就是题干
    assert chunker._is_question_heading("SpringMVC 流程？答：") is True
    assert chunker._is_question_heading("线程池原理？各个参数的作用。") is True


# ---- 题号 + 两位数：`6.32位系统…` 不是小数 ----

def test_question_number_followed_by_number_with_unit():
    assert chunker._match_question("6.32位系统上有什么细微差别？") == (
        6, "32位系统上有什么细微差别？", "题号")
    assert chunker._match_question("3.14 是圆周率的近似值") is None


# ---- 框架问句：“回答…为什么…”不是在提问 ----

def test_framed_question_word_is_not_a_question_line():
    assert chunker._is_qline(
        "回答为什么要用什么方法这种问题的时候，通常首先要回答两个问题，"
        "第一个就是，我要做什么事情，第二个就是，不同方法的优劣是什么。") is False
    for real in (
        "142 为什么引入偏向锁、轻量级锁，介绍下升级流程",
        "面试官：那么，前面有提到三级缓存，三级缓存的作用是什么？",
        "那如何避免单点呢？",
    ):
        assert chunker._is_qline(real) is True, real


# ---- 陈述句标题闸：带句读的陈述长句不是标题问句 ----

def test_statement_heading_is_not_a_question():
    for s in (
        "与服务之间的心跳，当服务不 可用时，向注册中心注销服务。这种方式的缺点是",
        "记录的信息不同，binlog 是记录数据库的逻辑变化，它提供了三种日志格式分别是 statement，row 以及mixed；",
    ):
        assert chunker._is_question_heading(s) is False, s


def test_question_heading_with_punctuation_is_kept():
    for s in (
        "Mysql怎么分表，以及分表后如果想按条件分页查询怎么办 (如果不是按分表字段来查询的话，几乎效率低下，无解)",
        "简单描述MySQL 中，索引，主键，唯一索引，联合索引的区别，对数据",
        "sleep方法和wait方法有什么区别",
    ):
        assert chunker._is_question_heading(s) is True, s


# ---- 编号陈述句：无提问语义的编号句留在父题答案里 ----

def test_declarative_numbered_item_detection():
    assert chunker._is_declarative_item("PriorityBlockingQueue：支持优先级排序的无界阻塞队列。") is True
    assert chunker._is_declarative_item("1、这是我人生第一场群面，真的慌，全程头都是懵的。") is True
    for real in (
        "排名前 4 个国内视频网站，国外最好的视频网站",
        "Redis 的两种持久化方式。",
        "简单介绍了一下自己做过的项目，然后面试官开始提问？",
    ):
        assert chunker._is_declarative_item(real) is False, real


def test_declarative_numbered_items_stay_in_answer():
    md = (
        "1. 什么是 Java 的优先级阻塞队列？\n\n"
        "PriorityBlockingQueue 是一个支持优先级的无界阻塞队列。\n\n"
        "2. PriorityBlockingQueue：支持优先级排序的无界阻塞队列。\n\n"
        "3. 它的 take 方法在队列为空时会阻塞。\n"
    )
    items = _items(md)
    assert [q for q, _ in items] == ["什么是 Java 的优先级阻塞队列？"]
    assert "支持优先级排序的无界阻塞队列" in items[0][1]


def test_declarative_gate_keeps_mid_sentence_task_semantics():
    """问词/任务词不在行首的试卷题干不是陈述句（真题整篇被降级成 0 题的回归）。"""
    for stem in (
        "最后我来问你一个智力题吧，80 个球里面有一个球有问题，如何用天平最快称出来。",
        "（机器学习理论）请描述 K-means 的原理，说明选择聚类中心的方法。",
        "任举一例，说明您在某项税金处理方面的心得和体会。",
        "一道算法题: 给三个数 N,S,T，1 到 N 组成一个序列，从 S 出发。",
    ):
        assert chunker._is_declarative_item(stem) is False, stem


def test_declarative_gate_needs_an_earlier_question():
    """文档还没有任何题时不降级：没有父题可挂，降级等于整篇 0 题。"""
    md = (
        "1. 你有一个带有四个圆形拨轮的转盘锁。每个拨轮都有 10 个数字，初始数字都是 0。\n\n"
        "返回打开转盘锁的最少次数。\n\n"
        "2. 再给一个字符串，目标也是要转开这把锁，求最少的转动次数。\n\n"
        "输入：deadends = ['0201']\n"
    )
    items = _items(md)
    assert len(items) == 2
    assert items[0][0].startswith("你有一个带有四个圆形拨轮的转盘锁")
    assert "返回打开转盘锁的最少次数" in items[0][1]


# ---- 目录行尾全角页码 ----

def test_full_width_page_number_stripped_from_stem():
    assert chunker.RE_TOC_PAGE_TAIL.sub("", "简述栈和队列的区别并解释含义１１０") == (
        "简述栈和队列的区别并解释含义")


# ---- 结构标签 / 代码形态标题 ----

def test_structural_label_heading_is_not_a_question():
    for s in ("问题解答", "问题分析", "4 小结", "题目描述", "min()", "</servlet>", "面试准备"):
        assert chunker._heading_questionish(s) is False, s
    for real in ("CDN(内容分发网络)", "Oracle JDK 和 OpenJDK 的对比", "什么是 JVM？"):
        assert chunker._heading_questionish(real) is True, real


# ---- 标题探针：被硬换行截断的标题问句 ----

def test_truncated_heading_question_is_rejoined():
    md = (
        "#### 29.1 面试官：那么，前面有提到三级缓存，三级缓存的作用\n\n"
        "是什么？\n\n"
        "三级缓存用来存储代理Bean。\n"
    )
    stems = _stems(md)
    assert len(stems) == 1
    assert stems[0].endswith("面试官：那么，前面有提到三级缓存，三级缓存的作用是什么？")


def test_label_heading_before_independent_question_is_not_promoted():
    md = (
        "#### 缓存穿透\n\n"
        "**什么是缓存穿透？**\n\n"
        "指查询一个不存在的数据。\n"
    )
    assert _stems(md) == ["什么是缓存穿透？"]
