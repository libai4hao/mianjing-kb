# -*- coding: utf-8 -*-
"""F4 题干清洗：只洗显示文本，代码/指针写法原样保留。"""
from mianjing_kb.kb.qtext import clean_question_text


def test_unwraps_bold_and_leading_number():
    assert clean_question_text("**1** 、什么是 **Redis** ?") == "什么是 Redis?"
    assert clean_question_text("**2** 、 **Redis** 相比 **memcached** 有哪些优势?") == \
        "Redis 相比 memcached 有哪些优势?"


def test_number_forms():
    assert clean_question_text("**10.** Redis 集群方案应该怎么做？") == "Redis 集群方案应该怎么做？"
    assert clean_question_text("第5题：Spring 是什么？") == "Spring 是什么？"
    assert clean_question_text("（3）什么是索引？") == "什么是索引？"


def test_decimal_version_is_not_a_question_number():
    """`1.5 版本` 不能把 1. 当题号剥掉，否则会把小数拆开。"""
    assert clean_question_text("1.5 版本有什么变化？") == "1.5 版本有什么变化？"
    assert clean_question_text("2n 个整数如何求和？") == "2n 个整数如何求和？"


def test_pointer_and_code_questions_untouched():
    """代码警告：`int** p` 一个字符都不能动，题干含代码标记时整段跳过。"""
    assert clean_question_text("int** p 的输出是什么？") == "int** p 的输出是什么？"
    assert clean_question_text("int** 和 int* 的区别？") == "int** 和 int* 的区别？"
    assert clean_question_text("以下代码 `int** p` 的输出？") == "以下代码 `int** p` 的输出？"
    code = "代码如下：\nint main() { return 0; }"
    assert clean_question_text(code) == code


def test_dunder_names_and_plain_text_untouched():
    assert clean_question_text("__init__ 是什么") == "__init__ 是什么"
    assert clean_question_text("什么是 JVM？") == "什么是 JVM？"
    assert clean_question_text("") == ""
