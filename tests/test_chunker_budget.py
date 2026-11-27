# -*- coding: utf-8 -*-
"""chunker 资源边界回归（审计候选 #82/#83）：连续空行、行内题号密度、嵌套编号。

只断言"有界时间内完成 + 语义不回归"，不做精确计时断言（避免机器差异抖动）。
"""
import time

from mianjing_kb import chunker


def test_long_blank_run_is_linear_and_keeps_semantics():
    """#82：B 条连续空行只扫一次 run；题块语义与少量空行时一致。"""
    body = "1. 什么是 JVM？\n\n虚拟机。\n"
    tail = "\n2. 什么是 GC？\n\n垃圾回收。"
    small = chunker.chunk_v2(body + "\n\n" + tail)["items"]
    big = chunker.chunk_v2(body + "\n" * 20000 + tail)["items"]
    assert [it["q"] for it in small] == [it["q"] for it in big]
    t0 = time.perf_counter()
    chunker.chunk_v2("1. 什么是 JVM？\n\n虚拟机。\n" + "\n" * 60000 + tail)
    assert time.perf_counter() - t0 < 10      # 二次方实现在此规模会明显超时


def test_dense_inline_numbering_line_is_bounded():
    """#83：单行上千个题号候选 → 整行不拆（上限闸），有界时间完成。"""
    line = "前文。" + "2. 测试？" * 1500
    t0 = time.perf_counter()
    out = chunker.chunk_v2(line)
    assert time.perf_counter() - t0 < 10
    assert isinstance(out["items"], list)     # 不崩溃、不挂起


def test_nested_numbering_strip_is_bounded():
    """#83：极深嵌套编号前缀的 _strip_all_numbering 用偏移推进，不再逐层整串复制。"""
    text = "1. " * 5000 + "int x = 0;"
    t0 = time.perf_counter()
    assert chunker._strip_all_numbering(text) == "int x = 0;"
    assert time.perf_counter() - t0 < 5


def test_normal_inline_split_still_works():
    """上限闸不影响正常行内题号拆分（密度远低于闸值）。"""
    md = "使用命令查看配置。 11.3.1 什么是复制？\n\n主从复制。"
    out = chunker.chunk_v2(md)
    assert any("什么是复制" in it["q"] for it in out["items"])
