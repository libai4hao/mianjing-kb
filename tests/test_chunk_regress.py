# -*- coding: utf-8 -*-
"""切题回归基线（docs/47 P0-3）：计量与容差判定。"""
from mianjing_kb import chunk_regress as cr


MD = """# 面经

## 1. 什么是 JVM 的类加载？
类加载分为加载、验证、准备、解析、初始化五步。

## 2. Redis 为什么快？
纯内存 + 单线程 + IO 多路复用。

## 知识点汇总
本节只是答案小节，不是题。
"""


def test_measure_reports_four_metrics():
    m = cr.measure(MD)
    assert set(m) == {"questions", "empty_answers", "weak_questions", "probe"}
    assert m["questions"] >= 2
    assert m["empty_answers"] >= 0
    assert m["weak_questions"] >= 0


def test_compare_passes_on_identical():
    base = {"total": {"questions": 100, "empty_answers": 10, "weak_questions": 3},
            "per_doc": {"h1": {"questions": 100}}}
    cur = {"total": {"questions": 101, "empty_answers": 10, "weak_questions": 3},
           "per_doc": {"h1": {"questions": 101}}}
    assert cr.compare(cur, base)["verdict"] == "PASS"


def test_compare_fails_on_question_jump():
    base = {"total": {"questions": 100, "empty_answers": 10, "weak_questions": 3},
            "per_doc": {"h1": {"questions": 100}}}
    cur = {"total": {"questions": 140, "empty_answers": 10, "weak_questions": 3},
           "per_doc": {"h1": {"questions": 140}}}
    v = cr.compare(cur, base)
    assert v["verdict"] == "FAIL" and any("questions" in f for f in v["fails"])


def test_compare_fails_when_weak_questions_grow():
    base = {"total": {"questions": 100, "empty_answers": 10, "weak_questions": 3},
            "per_doc": {"h1": {"questions": 100}}}
    cur = {"total": {"questions": 100, "empty_answers": 10, "weak_questions": 4},
           "per_doc": {"h1": {"questions": 100}}}
    v = cr.compare(cur, base)
    assert v["verdict"] == "FAIL" and any("weak_questions" in f for f in v["fails"])


def test_compare_fails_on_missing_mirror():
    base = {"total": {"questions": 10, "empty_answers": 0, "weak_questions": 0},
            "per_doc": {"h1": {"questions": 10}}}
    cur = {"total": {"questions": 0, "empty_answers": 0, "weak_questions": 0},
           "per_doc": {"h1": {"missing": True}}}
    v = cr.compare(cur, base)
    assert v["verdict"] == "FAIL" and any("镜像缺失" in f for f in v["fails"])
