# -*- coding: utf-8 -*-
"""review2 问句语义收紧回归：弱问词/语气词不误判，真题不误伤。

P0-1：`呢` 是歧义语气词（"他还没来呢"是陈述句、"为什么呢"是问句），
      只有句中带疑问代词才按问句；`吗` 是可靠疑问语气词，无问号也仍是题。
P0-2：`区别/比较/分析/判断/你知道/你了解` 需后接对象才是题，
      裸词（答案小节标题）不误判为新题。
"""
from mianjing_kb import chunker


# ---- P0-1：语气词 `呢` 收紧 ----

def test_ne_statement_not_question():
    """陈述句结尾的 `呢`（还没来呢）不是问句。"""
    assert chunker._has_question_intent("他还没来呢") is False
    assert chunker._is_qline("他还没来呢") is False


def test_ne_with_interrogative_still_question():
    """句中带疑问代词的 `呢`（为什么呢 / 怎么处理呢）仍是问句。"""
    assert chunker._has_question_intent("为什么呢") is True
    assert chunker._has_question_intent("怎么处理呢") is True


def test_ma_question_kept():
    """`吗` 是可靠疑问语气词，无问号也仍是题（7.linux常用吗 回归）。"""
    assert chunker._has_question_intent("linux常用吗") is True
    assert chunker._is_qline("linux常用吗") is True


# ---- P0-2：弱问词 `区别/比较/分析/判断/你知道/你了解` 收紧 ----

def test_bare_weak_intent_word_not_question():
    """裸弱问词（答案小节标题）不判为题。"""
    for w in ("区别", "区别：", "比较", "分析", "判断", "你知道", "你了解"):
        assert chunker._has_question_intent(w) is False, w


def test_weak_intent_with_object_still_question():
    """弱问词后接对象时仍是题。"""
    assert chunker._has_question_intent("分析以下代码的输出结果") is True
    assert chunker._has_question_intent("比较两个字符串的不同") is True
    assert chunker._has_question_intent("判断以下代码是否会内存泄漏") is True


def test_answer_explanations_with_question_words_are_not_questions():
    for text in (
        "如果这个 Bean 关联了 BeanPostProcessor 接口，如何调用",
        "最大连接数，表示同时最多能支持多少连接，如果连接数不够，后续要获取连接的线程会阻塞。",
        "服务发现-服务发现工具管理群集中的流程和服务如何查找和互相交谈。",
        "最后，基于wait/notify 的特性，为什么适合实现生产者消费者的模型",
    ):
        assert chunker._has_question_intent(text) is False, text


def test_real_question_forms_without_question_mark_are_kept():
    for text in (
        "Kafka 创建 Topic 时如何将分区放置到不同的 Broker 中",
        "那么花费800秒，大概可以对多少个单词进行排序",
        "请问 Redis 最多可以保存多少个键",
        "检查程序，是否存在问题，如果存在指出问题所在",
    ):
        assert chunker._has_question_intent(text) is True, text


def test_unnumbered_quantity_and_comma_questions_start_new_blocks():
    quantity_q = "Redis 最多可以保存多少个键"
    comma_q = ("在基于 Spring Cloud 搭建的微服务系统中，当某个服务实例因为网络故障突然宕机时，"
               "Nacos 注册中心如何及时发现并将其从健康列表中剔除")
    assert chunker._is_standalone_question_intent(quantity_q)
    assert chunker._is_standalone_question_intent(comma_q)
    md = ("1. 什么是线程池？\n\n线程池用于复用线程。\n\n"
          f"{quantity_q}\n\nRedis 是内存数据库。\n\n"
          f"{comma_q}\n\nNacos 会更新健康实例列表。\n\n"
          "2. 什么是 JVM？\n\nJVM 是 Java 虚拟机。\n")
    items = chunker.chunk_v2(md)["items"]
    assert [it["q"] for it in items] == [
        "什么是线程池？", quantity_q, comma_q, "什么是 JVM？"]
    assert items[1]["a"].lstrip().startswith("Redis 是内存数据库")
    assert items[2]["a"].lstrip().startswith("Nacos 会更新健康实例列表")


def test_numbered_answer_explanations_stay_in_parent_answer():
    md = ("1. 什么是线程池？\n\n线程池用于复用线程。\n\n"
          "最大连接数，表示同时最多能支持多少连接，如果连接数不够，后续要获取连接的线程会阻塞。\n\n"
          "2. 什么是 JVM？\n\nJVM 是 Java 虚拟机。\n")
    items = chunker.chunk_v2(md)["items"]
    assert [it["q"] for it in items] == ["什么是线程池？", "什么是 JVM？"]
    assert "最大连接数" in items[0]["a"]


def test_unnumbered_answer_explanations_stay_in_parent_answer():
    explanations = [
        "如果这个 Bean 关联了 BeanPostProcessor 接口，如何调用",
        "最大连接数，表示同时最多能支持多少连接，如果连接数不够，后续要获取连接的线程会阻塞。",
        "服务发现-服务发现工具管理群集中的流程和服务如何查找和互相交谈。",
        "最后，基于wait/notify 的特性，为什么适合实现生产者消费者的模型",
    ]
    md = ("1. 什么是线程池？\n\n线程池用于复用线程。\n\n"
          + "\n\n".join(explanations)
          + "\n\n2. 什么是 JVM？\n\nJVM 是 Java 虚拟机。\n")
    items = chunker.chunk_v2(md)["items"]
    assert [it["q"] for it in items] == ["什么是线程池？", "什么是 JVM？"]
    assert all(text in items[0]["a"] for text in explanations)


def test_numbered_bare_weak_word_stays_in_answer():
    """`1. 区别` 编号答案小节不另起新题，留在父答案。"""
    md = ("1. 什么是HashMap？\n\nHashMap 允许 null。\n\n"
          "1. 区别\n\nHashtable 不允许 null。\n\n"
          "2. 什么是ConcurrentHashMap？\n\n线程安全。\n")
    v2 = chunker.chunk_v2(md)
    qs = [it["q"] for it in v2["items"]]
    assert qs == ["什么是HashMap？", "什么是ConcurrentHashMap？"], qs
    assert any("区别" in it["a"] for it in v2["items"]), \
        [it["a"] for it in v2["items"]]
