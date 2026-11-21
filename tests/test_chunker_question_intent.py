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
